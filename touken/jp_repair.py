# -*- coding: utf-8 -*-
"""日服手入（修复）：把指定部队的受伤成员送进手入，可用「手伝い札」秒修。

骨架版（云端骨架 → 待真机标定）：流程编排按标定后能直接填常量的方式写，
画面/常量全部走本模块顶部的 REPAIR_* 常量与 repair/ 命名空间模板，
没标定全就逐条列出缺什么然后安全停车，绝不盲点。导航/点击模式/弹窗处理/
卡死熔断全部复用 Sally 底座（jp_sally.JpNavigator），不手搓。

数据通道只走 PartyWatch 被动拦截（与账房同一红线：不注入脚本、不改请求
内容、不向服务器多发请求）；伤员名单与修复验证全部以游戏自己拉取的
/sally、/conquest 响应为准。

安全底线（在 Sally 底座之上追加的手入专属规矩）：

- 只修调用方指定部队里「数据通道认定受伤」（injury 非 None）的成员，
  绝不顺手修别人；黑名单语义以后再说。
- 只点认得出的画面：手入位认不出、选人列表认不出、弹窗认不出 → 一律
  安全停车（SallyStop）并存证 debug。
- 手伝い札不够就如实报并停，绝不碰小判买札。
- 全模块唯一准许点击的弹窗是「身份核验过的手伝い札确认窗」（模板或
  OCR 双保险核验）；其它任何弹窗一律停。
- 修完必须经数据通道验证：札秒修确认 hp 回满/伤势消失，慢修确认进了
  手入中状态（_read_repair_state 探针）；验证不了就如实报
  「修复结果没能确认」，绝不假绿。
- 完事必回本丸（无论修成什么样，这是调用契约）。

调用契约（jp_sortie 集成时照此接线）：

- nav：Sally 子类实例（JpSortie）。classify 必须能分出 'repair' /
  'repair_select' 两种画面——把本模块 REPAIR_SCREENS 并入 JpSortie
  .SCREENS（照 error 屏的插法，覆盖层之后）。带 _fetch_party_fresh
  时验证阶段借远征画面重拉 /conquest；没有也能跑（验证降级为如实报）。
- watch：已启动的 PartyWatch。
- 返回值 RepairReport 区分 已确认修好 / 慢修已开工 / 没能确认；
  没能确认的成员调用方必须按「仍带伤」处理（复查 _check_party /
  _check_injury），绝不按修复数量猜测可以出阵（复刻国服
  injury_recovery 的 stop 语义）。
- 对应国服 repair_on_injury 三档：continue = 修+札秒修后复查接着跑
  （use_speedup=True）；repair_stop = 修+慢修完收工（use_speedup=
  False）；stop = 不调本模块直接收工。

【真机标定进度】（2026-11-22，本丸 Hikoki 李服 51 级账号）：

- 已标定并入库（模板均 1.0 回验、跨屏实证）：directory/repair
  （目録手入瓦）、repair/label（手入部屋标题）、repair/slot-empty
  （「未使用」黑牌，一图通吃）、repair/select-label（手入刀剣男士選択
  标题）、repair/start（灰化态）、repair/speedup（使う+复选框）；
  常量 REPAIR_SLOTS（2 槽建成 + 2 槽未增築的账号实测 2 空位）、
  SPEEDUP_COUNT_ROI（读数「30 枚」实证）。
- 已实证的结构事实：手入部屋无目録按钮只有返回箭头（common/back
  0.99+ 同源）；本丸指揮浮层没有手入入口（只有内番/演練/遠征/出陣）；
  进手入室不触发任何队伍数据拉取（验证只能借远征/出阵画面）。
- 待复核（今日账号 0 伤刀，空态推定）：选人列表卡片布局与
  SELECT_NAME_ROIS/SELECT_BTN_OFFSET、激活态 repair/start 配色、
  札确认窗真身（repair/speedup-confirm + SPEEDUP_CONFIRM_ROI）、
  手入中数据字段（_read_repair_state 探针）。
"""
from __future__ import annotations

import re
import time
from typing import NamedTuple

from . import jp_party, jp_sally, jp_vision
from .device_adapter import Point

NUMERAL = '一二三四五'

NAV_TIMEOUT = jp_sally.NAV_TIMEOUT          # 每个子步骤认画面的上限
POST_REPAIR_WAIT = 12.0                     # 修完等游戏自己重拉 /sally 的宽限（重拉时机待标定）

# 画面分类：Sally 表基础上插入手入室两条（覆盖层之后、shiki_menu 之前）。
# repair_select 必须在 repair 前面：选人面板会盖住手入室标题锚点，
# 先认 repair 会永远看不见选人界面（shiki_menu 排在 home 前的同款原因）。
REPAIR_SCREENS = (
    ('repair_select', ('repair/select-label',)),   # 点空位后的刀剑选择界面
    ('repair', ('repair/label',)),                 # 手入室本体
)
SCREENS = (jp_sally.SCREENS[:9] + REPAIR_SCREENS + jp_sally.SCREENS[9:])

# 进手入导航：本丸一律走目録大页进出阵（同 Sally 底座的路数，目录页
# 不透明、景趣影响不到）。2026-11-22 真机实证：本丸「指揮」浮层只有
# 内番/演練/遠征/出陣四个入口，没有手入——REPAIR_ROUTE 不放 shiki_menu；
# 出阵各屏接续则先回目録大页再进手入。
REPAIR_ROUTE = {
    'home': ('hit', 'home/catalog', '目录按钮'),
    'directory': ('hit', 'directory/repair', '目录手入入口'),
    'formation': ('near', 'formation/label', jp_sally.BACK_ARROW_OFFSET,
                  '编成页返回箭头'),
    'era': ('hit', 'home/catalog', '时代屏目录按钮'),
    'map': ('hit', 'home/catalog', '选图屏目录按钮'),
    'team': ('hit', 'home/catalog', '部队屏目录按钮'),
}

# 回本丸导航：手入室/手入选人屏都没有目録按钮（2026-11-22 真机实证），
# 只有左上角返回箭头（common/back，跨屏 0.99+ 同源已验）；返回箭头在
# 选人屏会先落回手入室，_navigate 下一拍再点一次即回本丸。
HOME_ROUTE = {
    'directory': ('hit', 'directory/home', '目录本丸瓦'),
    'formation': ('near', 'formation/label', jp_sally.BACK_ARROW_OFFSET,
                  '编成页返回箭头'),
    'repair': ('hit', 'common/back', '手入室返回箭头'),
    'repair_select': ('hit', 'common/back', '手入选人屏返回箭头'),
}

# ==================== 标定常量（2026-11-22 真机标定；来源帧与复核状态见【待标定清单】） ====================

# 手入位：REPAIR_SLOTS 是四个槽位的判定区（相对 repair/label 锚点中心的
# 基准空间像素 rect (dx, dy, w, h)）；槽位判空靠 SLOT_EMPTY_TEMPLATE
# （「未使用」内框）在各判定区内匹配。2026-11-22 实测：行距 147px，
# 该账号手入部屋 2 槽建成（均空）+ 2 槽未增築（匹配不上 → 不占空位数）。
REPAIR_SLOTS: tuple[tuple[int, int, int, int], ...] = (
    (-270, 94, 210, 90),
    (-270, 241, 210, 90),
    (-270, 388, 210, 90),
    (-270, 535, 210, 90),
)
SLOT_EMPTY_TEMPLATE = 'repair/slot-empty'

# 选人列表：灰色列表面板的 OCR 区（基准空间像素 rect，可多段）+ 行内点选
# 相对名字命中点的偏移。【待复核】今日无伤员，列表为空态——卡片布局、
# 行高、偏移都按空板面推定；首批伤员出现时按【待标定清单】第 3 步复核。
# 同名重复刀按出现次序依次点第 N 行。
SELECT_NAME_ROIS: tuple[tuple[int, int, int, int], ...] = (
    (545, 220, 710, 615),
)
SELECT_BTN_OFFSET: tuple[int, int] | None = (0, 0)

# 手伝い札：复选框模板（使う + 框）、确认窗身份核验（模板优先，缺失/未中
# 再 OCR 窗区校验词）、持有数 OCR 区（实证读数「30 枚」）。
# SPEEDUP_CONFIRM_ROI 为推断位（弹窗出现在画面中央一带）——弹窗真身
# 今日不可见（无伤员点不了開始），首批伤员时复核。
SPEEDUP_CHECKBOX_TEMPLATE = 'repair/speedup'
SPEEDUP_CONFIRM_TEMPLATE = 'repair/speedup-confirm'
SPEEDUP_CONFIRM_TEXT = '手伝い札'
SPEEDUP_CONFIRM_ROI: tuple[int, int, int, int] | None = (500, 350, 508, 280)
SPEEDUP_COUNT_ROI: tuple[int, int, int, int] | None = (1295, 545, 143, 55)

# 开工按钮（2026-11-22 裁自灰化态——无伤时它是灰的；激活态配色不同，
# 灰度相关匹配实测仍 1.0，首批伤员时裁一次激活态替换更稳）。
START_TEMPLATE = 'repair/start'

# 体检要验的模板（Sally 底座已验过的不在列）。
# repair/speedup-confirm 不在硬清单：今日实证裁不到（见上），身份核验
# 走 OCR 兜底链（SPEEDUP_CONFIRM_ROI），模板就位后自动升级为主链。
_REQUIRED_TEMPLATES = (
    'directory/repair',
    'repair/label', 'repair/slot-empty', 'repair/select-label',
    'repair/speedup', 'repair/start',
)


class RepairReport(NamedTuple):
    """repair_team_stream 的结果；三个名单全是面向玩家的显示名。"""
    team_no: int
    repaired: tuple[str, ...]          # 数据确认已修好（hp 回满/伤势消失）
    slow_running: tuple[str, ...]      # 慢修确认开工（回满要现实时间）
    unconfirmed: tuple[str, ...]       # 修复结果没能确认——按仍带伤处理，绝不假绿


def preflight() -> list[str]:
    """手入素材体检：缺的模板和没填的标定常量逐条列出（绝不静默）。"""
    missing = []
    for name in _REQUIRED_TEMPLATES:
        try:
            jp_vision.load_template(name)
        except jp_vision.TemplateNotFound as exc:
            missing.append(str(exc))
    # 札确认窗身份核验：模板与 OCR 窗区二选一即算齐（模板就位后 _is_speedup_confirm 自动先用模板）
    try:
        jp_vision.load_template(SPEEDUP_CONFIRM_TEMPLATE)
    except jp_vision.TemplateNotFound:
        if SPEEDUP_CONFIRM_ROI is None:
            missing.append(f'缺少日服模板 {SPEEDUP_CONFIRM_TEMPLATE} 且 '
                           'SPEEDUP_CONFIRM_ROI 未配置（札确认窗身份核验双链皆空）')
    if not REPAIR_SLOTS:
        missing.append('标定常量 REPAIR_SLOTS（四个手入位判定区）还没填值')
    if not SELECT_NAME_ROIS:
        missing.append('标定常量 SELECT_NAME_ROIS（选人列表名字 OCR 区）还没填值')
    if SELECT_BTN_OFFSET is None:
        missing.append('标定常量 SELECT_BTN_OFFSET（行内选择按钮偏移）还没填值')
    return missing


def repair_team_stream(nav, watch, team_no: int,
                       use_speedup: bool = True) -> RepairReport:
    """把部队 team_no 里数据通道认定受伤的成员逐振送进手入并验证结果。

    nav：Sally 子类实例（JpSortie，classify 需认识 repair/repair_select）；
    watch：已启动的 PartyWatch。日志经 nav.log 输出。
    返回 RepairReport；任何安全停车抛 SallyStop（文案面向玩家）。
    """
    if team_no not in jp_sally.TEAM_TAB_OFFSETS:
        raise ValueError('部队番号应在 1-5 之间')
    if watch is None:
        raise ValueError('手入必须有队伍数据通道（PartyWatch）才能验伤与验证')

    missing = preflight()
    if missing:
        nav.log('日服手入素材没标定，先干不了活。缺这些：')
        for item in missing:
            nav.log(f'  ✗ {item}')
        raise jp_sally.SallyStop('手入素材没标定全，已停止')

    # ---- 1. 新鲜队伍数据 → 伤员名单 ----
    latest = nav._await_party_data(watch)
    if latest is None:
        fetch_fresh = getattr(nav, '_fetch_party_fresh', None)
        if fetch_fresh is not None:
            nav.log('起手没有队伍数据，先去拿一次…')
            latest = fetch_fresh(watch)
    if latest is None:
        raise jp_sally.SallyStop(
            '拿不到队伍数据，没法确认谁受了伤，已停止；请刷新游戏页面后重试')
    captured_at, payload = latest
    status = jp_party.party_status(payload, team_no)
    if status is None:
        raise jp_sally.SallyStop(
            f'没能读出部队{NUMERAL[team_no - 1]}的编制，已停止；请人工查看游戏')
    if status != 1:
        raise jp_sally.SallyStop(
            f'部队{NUMERAL[team_no - 1]}不在本丸待命（可能在远征或出阵中），'
            '手入够不着，已停止')
    snapshot = jp_party.build_team_snapshot(payload, team_no, now=captured_at)
    if snapshot is None or not snapshot.injury_known:
        raise jp_sally.SallyStop(
            f'没能确认部队{NUMERAL[team_no - 1]}的成员状态，已停止；请人工查看游戏')
    hurt = [m for m in snapshot.members if m.injury is not None]
    if not hurt:
        nav.log(f'部队{NUMERAL[team_no - 1]}没有受伤的成员，不用修')
        return RepairReport(team_no=team_no, repaired=(), slow_running=(),
                            unconfirmed=())
    nav.log('待修：' + '、'.join(
        f'{_label(m)}（{m.injury}）' for m in hurt))

    # ---- 2. 导航到手入室 ----
    nav._navigate(REPAIR_ROUTE, frozenset({'repair'}), '前往手入')

    # ---- 3. 逐振修（只修名单内的伤员，绝不顺手点别人） ----
    since = time.time()                    # 验证只认这个时刻之后到的数据包
    started: dict[str, str] = {}           # serial_id → 显示名（已送进手入）
    occurrence: dict[str, int] = {}        # 同名第几行（同名重复刀依次点）
    record = getattr(nav, 'record_event', None)
    try:
        for index, member in enumerate(hurt):
            if use_speedup or index == 0:
                free = _count_free_slots(nav)
                if free <= 0:
                    raise jp_sally.SallyStop(
                        '手入位全满了，已停止；请等正在修的刀出炉，或手动腾出空位')
                if not use_speedup and free < len(hurt):
                    raise jp_sally.SallyStop(
                        f'空的手入位只有 {free} 个，装不下 {len(hurt)} 把伤刀，'
                        '又不能秒修腾位，已停止；请先手动安排（只修指定成员，'
                        '绝不挑人修一半）')
            begun = _repair_one_member(nav, member, use_speedup,
                                       len(hurt) - index, occurrence)
            if not begun:
                # 选人列表里没找到这振：可能已被手动修好。跳过，最后以
                # 数据验证为准（验伤复核交给调用方，不在这里下结论）。
                continue
            started[member.serial_id] = _label(member)
            if record:
                record('repair.queued', server='jp', serial_id=member.serial_id,
                       name=_label(member), team_no=team_no,
                       injury=member.injury, speedup=use_speedup)
                if use_speedup:
                    # 札是我们亲手勾的、确认窗身份核过才点的 はい，-1 是确定事实
                    record('resource.change', resource='手伝い札', delta=-1,
                           source='repair.speedup', attribution='confirmed')
    except jp_sally.SallyStop as exc:
        if started:
            done = '、'.join(started.values())
            raise jp_sally.SallyStop(
                f'{exc}（这轮已经送进手入的：{done}，结果请以游戏画面为准）'
            ) from exc
        raise

    # ---- 4. 回本丸 + 数据验证（绝不假绿） ----
    return _verify_repairs(nav, watch, team_no, started, use_speedup, since)


def _label(member) -> str:
    return member.name or f'#{member.serial_id}'


def _count_free_slots(nav) -> int:
    """数空的手入位。手入室认不出就安全停车（截图存证在调用方语境里）。"""
    image = nav.device.screenshot(force=True)
    normalized = jp_vision.normalize_frame(image)
    anchor = jp_vision.match(normalized,
                             jp_vision.load_template('repair/label'),
                             name='repair/label')
    if anchor is None:
        nav._save_debug(image)
        raise jp_sally.SallyStop(
            '手入室画面没认出来，槽位数不了，已停止（截图已存到 debug）')
    fw, fh = normalized.size
    ax, ay = anchor.nx * fw, anchor.ny * fh
    free = 0
    for dx, dy, w, h in REPAIR_SLOTS:
        roi = ((ax + dx) / fw, (ay + dy) / fh, w / fw, h / fh)
        if jp_vision.match(normalized, jp_vision.load_template(SLOT_EMPTY_TEMPLATE),
                           roi=roi):
            free += 1
    return free


def _repair_one_member(nav, member, use_speedup: bool, tickets_needed: int,
                       occurrence: dict) -> bool:
    """把一振伤刀送进手入；返回 True=已开工，False=列表里没找到（跳过）。

    任何认不出的画面/弹窗 → SallyStop。只点名单内这一振，绝不顺手点别人。
    """
    label = _label(member)
    nav.log(f'送 {label}（{member.injury}）进手入…')
    image = nav.device.screenshot(force=True)
    row_btn = _locate_member_row(nav, member, image, occurrence)
    if row_btn is None:
        nav.log(f'选人列表里没找到 {label}，可能已被手动修好；跳过这振，'
                '最后以数据验证为准')
        return False
    nav.device.click(row_btn)
    time.sleep(0.8)
    _await_select_detail(nav, label)

    if use_speedup:
        tickets = _read_speedup_count(nav)
        if tickets is not None:
            if tickets < tickets_needed:
                raise jp_sally.SallyStop(
                    f'手伝い札只剩 {tickets} 张，修不完剩下的 {tickets_needed} '
                    '把伤刀，已停止；请补充手伝い札或手动处理（绝不碰小判买札）')
            nav.log(f'手伝い札还剩 {tickets} 张')
        image = nav.device.screenshot(force=True)
        box = jp_vision.find(image, SPEEDUP_CHECKBOX_TEMPLATE)
        if box is None:
            nav._save_debug(image)
            raise jp_sally.SallyStop(
                f'手伝い札的勾选框没认出来，已停止（截图已存到 debug）；'
                f'{label} 还没开工，请人工查看游戏')
        w, h = jp_vision.normalize_frame(image).size
        nav.device.click(Point(int(box.nx * w), int(box.ny * h)))
        time.sleep(0.5)

    _click_start(nav, label, use_speedup)
    return True


def _locate_member_row(nav, member, image, occurrence: dict) -> Point | None:
    """选人列表里定位成员行的「選択」按钮；找不到该行返回 None。

    策略（待标定确认）：名字 OCR 命中点 + 固定行内偏移。同名重复刀按
    出现次序点第 N 行——修好一振后它若仍在列表，第二振会自然落到下一行。
    """
    if not member.name:
        raise jp_sally.SallyStop(
            f'#{member.serial_id} 在刀剑字典里认不出名字，没法在列表里定位，'
            '已停止；请人工查看游戏')
    if not SELECT_NAME_ROIS or SELECT_BTN_OFFSET is None:
        raise jp_sally.SallyStop(
            '选人列表还没标定（见 jp_repair【待标定清单】），已停止')
    from . import cn_ocr, jp_ocr
    normalized = jp_vision.normalize_frame(image)
    fw, fh = normalized.size
    nth = occurrence.get(member.name, 0)
    for x, y, w, h in SELECT_NAME_ROIS:
        crop = normalized.crop((x, y, min(fw, x + w), min(fh, y + h)))
        tokens = jp_ocr.read_tokens(crop)
        hits = [(cx + x, cy + y) for text, _score, cx, cy in tokens
                if cn_ocr.text_matches(text, member.name, 'contains')]
        if len(hits) > nth:
            occurrence[member.name] = nth + 1
            return Point(hits[nth][0] + SELECT_BTN_OFFSET[0],
                         hits[nth][1] + SELECT_BTN_OFFSET[1])
    return None


def _await_select_detail(nav, label: str) -> None:
    """等选人详情出现（手入開始按钮可见）；弹窗/陌生画面即停。"""
    deadline = time.monotonic() + NAV_TIMEOUT
    while time.monotonic() < deadline:
        image = nav.device.screenshot(force=True)
        screen = nav.classify(image)
        if screen in ('dialog', 'confirm', 'automarch_dialog'):
            nav._save_debug(image)
            raise jp_sally.SallyStop(
                f'给{label}选人时出现了确认弹窗，已停止；'
                f'请人工查看游戏（截图已存到 debug）')
        if screen not in ('repair', 'repair_select', 'loading'):
            nav._save_debug(image)
            raise jp_sally.SallyStop(
                f'给{label}选人时遇到了不认识的画面，已停止（截图已存到 debug）')
        if jp_vision.find(image, START_TEMPLATE):
            return
        time.sleep(nav.poll)
    nav._save_debug(nav.device.screenshot(force=True))
    raise jp_sally.SallyStop(
        f'给{label}的选人界面没打开（手入開始按钮一直没出现），已停止')


def _click_start(nav, label: str, use_speedup: bool) -> None:
    image = nav.device.screenshot(force=True)
    start = jp_vision.find(image, START_TEMPLATE)
    if start is None:
        nav._save_debug(image)
        raise jp_sally.SallyStop(
            f'手入開始按钮没认出来，已停止（截图已存到 debug）；'
            f'{label} 还没开工，请人工查看游戏')
    w, h = jp_vision.normalize_frame(image).size
    nav.device.click(Point(int(start.nx * w), int(start.ny * h)))
    if use_speedup:
        _await_speedup_confirm(nav, label)
    else:
        _await_back_to_room(nav, label)


def _await_speedup_confirm(nav, label: str) -> None:
    """等手伝い札确认窗并点 はい（身份核验过的唯一准许点击的弹窗）。

    上下文收敛：刚勾札点了手入開始，唯一预期的弹窗就是札确认。但弹窗
    身份必须过 _is_speedup_confirm 核验（模板/OCR 双保险），核验不过
    一律按陌生弹窗停——绝不盲点同意。若短窗内根本没出现弹窗就回到了
    手入室（与预期不符），记日志放行，交给数据验证裁决（玩家优先，
    以现实为准，不靠预期卡死）。
    """
    deadline = time.monotonic() + NAV_TIMEOUT
    confirm_clicks = 0          # はい 可能被画布重绘吞掉（江户城实测），限次补点
    while time.monotonic() < deadline:
        image = nav.device.screenshot(force=True)
        screen = nav.classify(image)
        if screen in ('confirm', 'dialog'):
            if not _is_speedup_confirm(nav, image):
                nav._save_debug(image)
                raise jp_sally.SallyStop(
                    f'给{label}秒修时出现了认不出的弹窗，已停止；'
                    f'请人工查看游戏（截图已存到 debug）')
            if confirm_clicks < 3:
                confirm_clicks += 1
                yes_tpl = 'dialog/accept' if screen == 'confirm' else 'dialog/yes'
                nav._click_hit(yes_tpl, '手伝い札确认（はい）', image=image)
        elif screen in ('repair', 'repair_select'):
            if confirm_clicks:
                return
            nav.log(f'{label} 秒修没有等到确认窗就回了手入室（与预期不符）；'
                    '不猜结果，交给数据验证')
            return
        elif screen == 'loading':
            pass
        else:
            nav._save_debug(image)
            raise jp_sally.SallyStop(
                f'给{label}秒修时遇到了不认识的画面，已停止（截图已存到 debug）')
        time.sleep(nav.poll)
    nav._save_debug(nav.device.screenshot(force=True))
    raise jp_sally.SallyStop(f'给{label}秒修的确认窗一直没出现，已停止')


def _await_back_to_room(nav, label: str) -> None:
    """慢修开工后等回手入室。

    保守策略：开工后预期直接回手入室，见到任何弹窗一律停（日服慢修开工
    有无确认窗待标定，若有预期窗照 _depart 的注释在此加白名单）。
    """
    deadline = time.monotonic() + NAV_TIMEOUT
    while time.monotonic() < deadline:
        image = nav.device.screenshot(force=True)
        screen = nav.classify(image)
        if screen in ('repair', 'repair_select'):
            return
        if screen in ('dialog', 'confirm', 'automarch_dialog'):
            nav._save_debug(image)
            raise jp_sally.SallyStop(
                f'给{label}开工慢修时出现了确认弹窗，已停止；'
                f'请人工查看游戏（截图已存到 debug）')
        if screen != 'loading':
            nav._save_debug(image)
            raise jp_sally.SallyStop(
                f'给{label}开工慢修时遇到了不认识的画面，已停止'
                f'（截图已存到 debug）')
        time.sleep(nav.poll)
    nav._save_debug(nav.device.screenshot(force=True))
    raise jp_sally.SallyStop(f'给{label}开工慢修后没能回到手入室，已停止')


def _is_speedup_confirm(nav, image) -> bool:
    """核验弹窗确实是手伝い札确认窗（绝不盲点同意）。

    优先 repair/speedup-confirm 模板；模板缺失/未中时 OCR 窗区找
    「手伝い札」校验词。两条都不可用 → 返回 False（宁可停，不可猜）。
    """
    try:
        if jp_vision.find(image, SPEEDUP_CONFIRM_TEMPLATE, threshold=0.8):
            return True
    except jp_vision.TemplateNotFound:
        pass
    if SPEEDUP_CONFIRM_ROI is not None:
        from . import cn_ocr, jp_ocr
        normalized = jp_vision.normalize_frame(image)
        fw, fh = normalized.size
        x, y, w, h = SPEEDUP_CONFIRM_ROI
        crop = normalized.crop((x, y, min(fw, x + w), min(fh, y + h)))
        tokens = jp_ocr.read_tokens(crop)
        return any(cn_ocr.text_matches(text, SPEEDUP_CONFIRM_TEXT, 'contains')
                   for text, _score, _cx, _cy in tokens)
    return False


def _read_speedup_count(nav) -> int | None:
    """读手伝い札持有数（OCR）；读不到返回 None（绝不猜）。"""
    if SPEEDUP_COUNT_ROI is None:
        return None
    from . import jp_ocr
    normalized = jp_vision.normalize_frame(nav.device.screenshot(force=True))
    fw, fh = normalized.size
    x, y, w, h = SPEEDUP_COUNT_ROI
    crop = normalized.crop((x, y, min(fw, x + w), min(fh, y + h)))
    for text, _score, _cx, _cy in jp_ocr.read_tokens(crop):
        m = re.search(r'\d+', text)
        if m:
            return int(m.group(0))
    return None


def _await_data_since(nav, watch, since: float, timeout: float):
    """等一份 since 时刻之后才到的队伍数据；宽限内没有返回 None。"""
    deadline = time.monotonic() + timeout
    while True:
        latest = watch.latest()
        if (latest and latest[0] > since
                and 0 <= time.time() - latest[0] <= jp_sally.PARTY_DATA_MAX_AGE):
            return latest
        if time.monotonic() >= deadline:
            return None
        time.sleep(nav.poll)


def _verify_repairs(nav, watch, team_no: int, started: dict[str, str],
                    use_speedup: bool, since: float) -> RepairReport:
    """回本丸 → 拉修后的队伍数据 → 逐振裁决。验证不了如实报，绝不假绿。"""
    # 契约：完事必回本丸（验证导航的落点就是本丸）。
    nav._navigate(HOME_ROUTE, frozenset({'home'}), '修复后回本丸')
    latest = _await_data_since(nav, watch, since, POST_REPAIR_WAIT)
    if latest is None:
        fetch_fresh = getattr(nav, '_fetch_party_fresh', None)
        if fetch_fresh is not None:
            nav.log('等不到修后的队伍数据，去远征画面拉一次来验证…')
            latest = fetch_fresh(watch)
    if latest is not None and latest[0] <= since:
        latest = None            # 拿到的是修复前的旧包，不算数

    repaired: list[str] = []
    slow_running: list[str] = []
    unconfirmed: list[str] = []
    if latest is None:
        unconfirmed.extend(started.values())
        nav.log('⚠ 修复结果没能确认（拿不到修后的队伍数据）：'
                + '、'.join(unconfirmed) + '；请以游戏画面为准')
    else:
        captured_at, payload = latest
        snapshot = jp_party.build_team_snapshot(payload, team_no,
                                                now=captured_at)
        probe = None if use_speedup else _read_repair_state(payload)
        for serial, name in started.items():
            member = next((m for m in snapshot.members
                           if m.serial_id == serial), None) if snapshot else None
            if member is None:
                unconfirmed.append(f'{name}（修后名单都对不上）')
                continue
            if use_speedup:
                if member.injury is None and member.hp == member.hp_max:
                    repaired.append(name)
                else:
                    # 数据说还没好：玩家优先，以现实为准，如实报
                    unconfirmed.append(name)
            elif probe is not None and probe.get(serial):
                slow_running.append(name)
            else:
                unconfirmed.append(name)
        if repaired:
            nav.log('✅ 确认已修好：' + '、'.join(repaired))
        if slow_running:
            nav.log('🩹 慢修已开工（回满还要现实时间）：' + '、'.join(slow_running))
        if unconfirmed:
            nav.log('⚠ 修复结果没能确认（按仍带伤处理）：' + '、'.join(unconfirmed))

    record = getattr(nav, 'record_event', None)
    if record:
        record('repair.team_result', server='jp', team_no=team_no,
               use_speedup=use_speedup, repaired=len(repaired),
               slow_running=len(slow_running), unconfirmed=len(unconfirmed))
    return RepairReport(team_no=team_no, repaired=tuple(repaired),
                        slow_running=tuple(slow_running),
                        unconfirmed=tuple(unconfirmed))


def _read_repair_state(payload: dict) -> dict[str, dict] | None:
    """【数据通道探针·待标定】从 /sally 全量刀帐里读「手入中」状态。

    2026-11-22 真机实证（/conquest 全量包 145KB、150 振 dump 于仓库
    debug/jp_repair_payload.json，进手入室不触发任何拉取，数据只能靠
    远征/出阵画面逼出来）：刀剑记录 41 个字段，候选只有两个——
      - status：150 振健康刀全为 0（0=正常坐实；手入中的取值未知，
        疑似 1，待实证）；
      - recovered_at：ISO 时间字符串，健康刀全是过去时刻（语义疑似
        「上次回满/手入完成时刻」；手入中是否为未来时刻=预定完成时刻，
        待实证）；
      - burnout_status/burnout_recovery_at 是気力透支机制，与手入无关。
    今日账号 0 伤刀，两种待实证状态都看不到 → 探针保持返回 None
    （调用方按「没能确认」如实报），绝不猜。

    TODO（首批伤员时一局定音）：让一刀进自然手入 → 进远征画面逼
    /conquest 重报（或出阵菜单逼 /sally）→ dump 对比手入前后记录：
    status 是否翻转、recovered_at 是否变为未来时刻。哪个字段翻转
    就用哪个实现本函数：返回 {serial_id: {...}}，没在修的刀不出现；
    两字段都没信号就保持 None 并在注释里记录新证据。
    """
    return None


# ==================== 【待标定清单】 ====================
#
# 【已标定·2026-11-22 真机】（截图同源铁律遵守：全部裁自 JpBrowser
# 实跑帧 JP_DATA_DIR/debug/frames/rcal-*.png，1508x1088 基准空间）
#   模板（resource/base/image/jp/repair/ 与 directory/repair.png）：
#     repair/label          ← rcal-05 手入部屋标题（score 1.0）
#     repair/slot-empty     ← rcal-05「未使用」黑牌（一图通吃已验）
#     repair/select-label   ← rcal-06 手入刀剣男士選択标题（1.0）
#     repair/start          ← rcal-06 灰化态按钮（1.0；激活态待复核）
#     repair/speedup        ← rcal-06 使う+复选框（1.0）
#     directory/repair      ← rcal-02 目録手入瓦（1.0）
#   常量：REPAIR_SLOTS 四槽判定区（锚点相对量，2 建成 + 2 未增築实证）、
#         SPEEDUP_COUNT_ROI（读数「30 枚」实证）
#   结构实证：手入部屋无目録只有返回箭头（HOME_ROUTE 用 common/back）；
#         指揮浮层没有手入入口（REPAIR_ROUTE 无 shiki_menu 支线）；
#         进手入室不触发 /sally//conquest（验证必借远征/出阵画面）。
#
# 【待复核·首批伤员时一局定音】（今日 0 伤刀只能空态标定）：
#   1. 选人列表：真实卡片布局/行高/点选偏移 → 复核 SELECT_NAME_ROIS、
#      SELECT_BTN_OFFSET（现值为空板面推定）；同名重复刀「按出现次序
#      点第 N 行」在真列表上验证。
#   2. repair/start：裁一次激活态替换灰化态模板（灰度匹配跨态 1.0 已验，
#      但激活态更稳）；顺带看列表排序规则（按伤势？按入手？）。
#   3. 札确认窗：勾使う → 手入開始 → 弹窗采帧（窗文案、按钮对、归属
#      confirm 还是 dialog）→ 裁 repair/speedup-confirm、校
#      SPEEDUP_CONFIRM_ROI；点「いいえ」取消（不烧札）即可。
#   4. 慢修开工有无确认窗（决定 _await_back_to_room 保守停车加不加
#      白名单）；开工后画面变化（槽位变 repairing 态 → slot-empty
#      之外的占用态模板）。
#   5. _read_repair_state 探针：手入前后各 dump 一份 /conquest（或
#      /sally）对比 status/recovered_at 哪个翻转（实证记录已写进探针
#      docstring，payload 样例在仓库 debug/jp_repair_payload.json）。
#   6. 全流程实跑：repair_team_stream 修一刀（continue 档用札秒修，
#      消耗 1 枚写清日志；repair_stop 档慢修），盯每个裁决点；
#      E2E 首修完成前模块日志必须带「首修待验证」心态，成绩单不许假绿。
#
# 标定工具：debug/jp_repair_cal.py（snap/watch/repair/slot/shiki/back/
# home 子命令；帧存 JP_DATA_DIR/debug/frames/rcal-*.png）。
