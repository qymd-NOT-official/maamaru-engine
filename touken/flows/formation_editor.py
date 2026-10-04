# -*- coding: utf-8 -*-
"""共用编队执行器 v1（"听命令的手"）：把指定部队的指定槽位换成明确目标。

只做换人。绝不选人（目标由调用方从当前本丸共用档案里挑好
给进来），绝不点击"即刻出阵/远征派遣/演练开始"——出发安全流程永远归
各玩法的 BattleMixin._safe_depart_stream + _confirm_departure。

为什么是一条共用链路（老大现场确认）：
  从目录→编队，以及出阵/远征/演练前的部队选择进入，每个槽位点"替换"
  后进入的是同一套"刀剑男士选择"列表：结构、筛选/排序、翻页、决定均
  相同，只是入口主题字样和配色不同（编队橙/出阵红）。所以：
  - 本执行器同时服务"部队编成"与"部队选择"两种外壳（entry_context）；
  - 页面识别只用标题/布局 OCR，绝不依赖主题色，不维护两套模板。

坐标（1280x720，全部沿用既有同源校准，本模块不新拍脑袋标定）：
  部队标签/六行行心：与 team_roster.py、sakura.py 相同（真机校准）；
  行内"替换"按钮 x=1033（sakura 实测）；
  "刀剑男士选择"列表：标题 OCR ROI、整列 OCR、决定按钮 x=1197、
  翻页 swipe(640,550→640,200,800ms)、8 页上限，均来自 sakura 换人链路
  与 repair 选择列表的真机用法。
  ⚠️ 列表行内目前只有"名字+疲劳"有真机校准（sakura 在用）；等级列
  ROI 是按布局推的待校准通道，读不出就 None，绝不硬猜——见
  parse_selection_rows 的 unknown_fields 与汇报盲区。

身份证据纪律（与 honmaru_profile / expedition_planner 同一套铁律）：
  - sword_catalog_id 是刀种/同位键，不是本丸实例 ID；observation_id
    只作档案引用随结果带回，不能单独拿来声称"页面上找到了那一振"；
  - 跨页面定位只用候选页实际能看到、且本轮仍可靠的证据（默认
    match_fields=("name","form","level")）。疲劳会自然恢复，不是身份
    证据，默认不参与匹配；
  - 「零冲突」只是候选，「证据充分」才放行：对 match_fields 里 target
    有值的身份字段，候选行缺值时不得 unique——target.form 已知而页面
    形态未知、target.level 已知而等级没读到，都只凭同位名字点不得；
    同名多振拉不开、有证据不足的同名行、或存在读不清的行，一律
    ambiguous 并附 missing_evidence，绝不点第一条；
  - 换前槽位观察只用于避免重复点击；证据不足就继续开名单，不据此猜测；
  - 当前选择列表没有形态直读通道（行 form 恒 None）：普通/极化同名
    在无法由其他可靠身份证据区分时应诚实停住，不装能认。调用方若从
    档案确知同名唯一，可显式收窄 match_fields（如 ("name","level")）
    来证明证据链，而不是让执行器默认放宽。

扫描完整度契约：
  - 翻页停滞只是"没翻动"（可能被吞），不等于到底；到底须经多阶段
    核验（回翻复归+向前探测+独立末端视觉证据），另有绕圈/截断/
    失明结局；max_pages=60 只是防死循环安全阀，不是"全表"的同义词；
  - 只有 complete（确认到底）的扫描才允许裁决 unique 或确定
    not_found；stalled/truncated/loop/blind 一律 screen_unrecognized +
    scan_incomplete 原因，绝不点击。

失败边界：
  - 找不到列表 / 切队未确认 → screen_unrecognized；
  - 目标整表不存在（可能被别队/手入/修行/互斥隐藏，或档案陈旧）
    → not_found，不换相似候选；
  - 决定已点但列表不关闭（游戏禁用该目标）→ unavailable；
  - 点「决定」后只确认选择列表已经关闭，不再 OCR 回读编队，也不根据
    回读结果改判成败或写入编队档案；若后续实测需要，再单独设计验收；
  - 翻页有指纹停滞/绕圈检测与页数上限，不在死循环里翻名单。
"""

import json
import re
import time

import numpy as np

from .. import sword_db
from ..maa_adapter import roi_4to4, Point
from ..roi_overrides import get_roi
from ..runtime_paths import STATE_DIR
from .team_roster import _match_name, _ROW_CY, _TEAM_TAB, link_visible_slot

RESULT_SCHEMA_VERSION = 1

# 结果枚举（机器可读契约）
ALREADY_CORRECT = "already_correct"
CHANGED = "changed"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"
SCREEN_UNRECOGNIZED = "screen_unrecognized"
INVALID_REQUEST = "invalid_request"

_SWAP_X = 1033            # 行内"替换"按钮（sakura 真机校准）
_DECIDE_X = 1197          # 列表行内"决定"按钮（sakura 真机校准）
_DECIDE_DY = -22          # 决定中心 ≈ 名字行中心上方 22px（sakura: 疲劳行-40）
# 读取型 ROI 走注册表 + 覆盖（面板模板工坊「代码 ROI」页可临时改，
# 存 DEBUG_DIR/template_lab/code-rois.json）：工人进程一跑一 import，
# 改完覆盖下次跑任务生效；坏覆盖静默回落这里的默认。点击坐标
# （_SWAP_X/_DECIDE_X）与滚动条到底证据按纪律不进注册表。
_LIST_TITLE = ("刀剑男士选择",
               get_roi("formation_editor.list_title", (450, 0, 830, 110)))
_FORMATION_TITLE = ("部队编成",
                    get_roi("formation_editor.title", (480, 0, 800, 60)))
_TEAM_SELECT_TITLE = ("部队选择",
                      get_roi("formation_editor.team_select_title",
                              (506, 1, 774, 55)))
_LIST_ROI = get_roi("formation_editor.list", (60, 40, 1240, 700))
                                              # 列表整列 OCR；y0=40：首行名字
                                              # y≈140 时其刀剑等级 y≈56，
                                              # y0=100 会把首行等级切掉
                                              # （2026-09-22 真机）
# 三条竖带注册成矩形只为面板里能框能调；代码只用 x 分量，y 跨度无语义
_NAME_BAND = get_roi("formation_editor.name_band", (100, 40, 300, 700))
_LEVEL_BAND = get_roi("formation_editor.level_band", (440, 40, 620, 700))
_FATIGUE_BAND = get_roi("formation_editor.fatigue_band", (440, 40, 640, 700))
_NAME_X = (_NAME_BAND[0], _NAME_BAND[2])
                                    # 姓名带（sakura 名字 ROI x[100,265] 同源）；
                                    # x<100 是位置标记/锁图标区，直接排除
_LEVEL_X = (_LEVEL_BAND[0], _LEVEL_BAND[2])
                                    # 等级/疲劳小字带，按内容格式区分
_FATIGUE_X = (_FATIGUE_BAND[0], _FATIGUE_BAND[2])
                                    # 疲劳值（含「疲劳 N/M」合体 token x≈529）
# 2026-09-22 真机校准（preset_list_probe，运行帧通道）：行内四行小字
# 「刀剑 N级 / 乱舞 N级 / 生存 N/N / 疲劳 N/M」同在 x≈492~580 一列——
# 旧 _LEVEL_X(300,460) 是布局推算，真机全落空，等级永远读 None。
_LEVEL_ABOVE_NAME = (40, 99)        # 刀剑等级在名字上方 40~99px（实测 ~86）；
                                    # 乱舞等级同格式（名字上方 ~63），靠
                                    # 「刀剑」label 同 y 配对排除，不靠 y 硬切
_POS_MARK = re.compile(r"^[一二三四五]\s*之\s*[一二三四五六]?$")  # "N之M"位置标记
_EDGE_ROW_Y = 640                   # 页缘行阈值（ROI 底 700）：底部行及其
                                    # 小字（y≈640~685）翻页后必然重现于页
                                    # 中部复核，乱码留待那时裁决
_TOP_EDGE_ROW_Y = 180               # 顶缘截断行：首屏完整姓名从 y≈196 起；
                                    # 翻页后 y<180 的半行在前一页已出现
_ROW_MERGE_DY = 25                  # 同一行碎 token 归并的 y 容差
_ROW_ATTACH_DY = 40                 # 疲劳/等级 token 归属名字行的 y 容差
_MAX_PAGES = 60                     # 翻页安全阀（防死循环），不是"全表"同义词；
                                    # 全局唯一只允许在 reached_end 后声称
_STALL_LIMIT = 2                    # 指纹连续不动触发「到底核验」（不等于到底）
_BOTTOM_PROOF_STAGES = 2            # 到底核验的独立阶段数：每阶段都重新正面
                                    # 验证滑块贴底+反滑回落+恢复贴底；恢复
                                    # 欠程帧不比指纹，贴底帧指纹与候选页
                                    # 一致才计有效阶段
_SWIPE_NEXT = (640, 550, 640, 200, 800)   # 原精确预设翻页手势
_SWIPE_PREV = (640, 200, 640, 550, 800)
_SWIPE_RANKED_NEXT = (640, 500, 640, 330, 800)  # 2026-09-24 真机：内容
_SWIPE_RANKED_PREV = (640, 330, 640, 500, 800)  # 移约 308px，重叠完整行
_GOTO_MAX_SWIPES = 12                   # 重定位安全阀：滑块地标导航正常
                                        # 几次就到；翻满还没落地如实失败
# 选择列表右缘滚动条（_list_end_sighted 的独立末端证据通道，
# 校准口径见该函数 docstring；改动这些数必须重新从运行帧取样验证）
_SCROLLBAR_BAND_X = (1262, 1270)          # 滑轨体列带
_SCROLLBAR_TRACK_Y = (124, 690)           # 滑轨纵向范围
_SCROLLBAR_BOTTOM_Y = 689                 # 滑块到底时底缘 y（钳在轨底，
                                          # 阈值 180~235 读数恒定 689）
_SCROLLBAR_BOTTOM_TOL = 0                 # 贴底零容差：真机证据只有
                                          # 「到底恒为 689」，没有任何
                                          # 687/688 的抖动样本；而滑块
                                          # 4px≈内容 62px，1px 也够藏住
                                          # 一行姓名，故只有明确读到 689
                                          # 才算到底，其余宁可 stalled
_SCROLLBAR_THUMB_BRIGHT = 200             # 滑块亮 ~243 / 轨道灰 ~113
_SCROLLBAR_TRACK_DARK = 150               # 轨道灰必须成段存在（防无滑轨
                                          # 页面的亮背景冒充满轨滑块）
_SCROLLBAR_REVERSE_MIN_DY = 30            # 反滑生效的最小底缘回落
                                          # （2026-09-22 探针实测一次反滑
                                          # 689→482；欠程/抖动远小于 30）
_CONFIRM_POPUP_TEMPLATE = "通用_确定.png"

# ── 筛选/排序面板：按刀种（+形态）预筛名单，把全表扫描缩到几页 ──
# 按钮全部 OCR 文字定位（exact），不写死点击坐标——面板内按钮文字
# 唯一（「太刀」exact 防误中「大太刀」）。筛选只决定「名单里有什么」，
# 裁决仍按行证据：筛错了顶多 not_found/ambiguous，不会换错人。
# 游戏会记住上次筛选条件，故每次先「取消筛选」重置再点目标刀种。
# 按钮查找范围是读取型 ROI，走注册表+覆盖（2026-09-22 真机校准：
# 刀种行 y≈226/300、全刀剑 y≈372、初/极行 y≈450、确定 y≈623——
# 「初/极」比布局推算低 ~70px，查找范围下缘千万别卡在 440）。
_FILTER_OPEN_TEXT = "筛选/排序"
_FILTER_OPEN_ROI = get_roi("formation_editor.filter_open",
                           (600, 60, 1100, 140))
_FILTER_PANEL_TITLE = ("筛选", get_roi("formation_editor.filter_title",
                                       (400, 60, 640, 130)))
_FILTER_PANEL_ROI = get_roi("formation_editor.filter_panel",
                            (100, 50, 1180, 670))
_FILTER_TYPE_ROI = get_roi("formation_editor.filter_types",
                           (130, 140, 1140, 470))
_FILTER_FORM_ROI = get_roi("formation_editor.filter_form_row",
                           (130, 410, 1140, 500))
_FILTER_CONFIRM_ROI = get_roi("formation_editor.filter_confirm",
                              (400, 540, 880, 680))
_FILTER_RESET_TEXT = "取消筛选"
_FILTER_CONFIRM_TEXT = "确定"
_FILTER_TYPES = frozenset(
    ("短刀", "胁差", "打刀", "太刀", "大太刀", "枪", "薙刀", "剑"))
_FILTER_FORM_TEXT = {"normal": "初", "kiwame": "极"}
_FILTER_CLOSE_X = (1149, 47)        # 面板右上 X（2026-09-22 运行帧实测）；
                                    # 只用于失败兜底回名单页，点击坐标不进注册表
DEFAULT_MATCH_FIELDS = ("name", "form", "level")

# 安全声明：本执行器的点击只允许落在以下目标上——部队标签、行内"替换"
# (1033)、列表行内"决定"(1197)、确认弹窗"通用_确定"、翻页 swipe。
# 出发类按钮的实测坐标钉在这里，永不点击；测试据此断言。
FORBIDDEN_DEPART_CLICKS = frozenset({
    (1198, 625),   # team_select.depart「即刻出阵」（touken_config.example.json）
    (1200, 630),   # 演练「演练开始」（practice.py）
})

_LEVEL_TOKEN = re.compile(r"(?:Lv\.?|刀剑)?\s*(\d{1,3})\s*级?$")
_FATIGUE_PAIR = re.compile(r"(\d{1,3})\D{0,2}/\D{0,2}(\d{1,3})")


# ==================== 纯函数：目标 / 行解析 / 匹配判定 ====================

def normalize_target(target):
    """把候选池条目（或等价 dict）规范化成执行目标。

    必填身份：sword_catalog_id 或 name（能过名册校正）；observation_id
    仅作档案引用随结果带回。form 只认两条来路：调用方显式给的
    form（normal/kiwame），或档案候选条目的 form_status 结论
    （ambiguous/unknown 一律落 None）。kiwame_date 是「显现日期」，
    每振刀都有，永远不参与形态推断（2026-09-15 P0 修正：旧版拿它
    推形态，把 185/196 振全误判成极）。
    Returns: (normalized, error)；error 非 None 表示输入不可用。
    """
    if not isinstance(target, dict):
        return None, "target 必须是候选池条目 dict"
    sid = target.get("sword_catalog_id")
    name = target.get("name") or target.get("name_zh")
    if not sid and name:
        sid = _match_name(name)
    if not sid:
        return None, "target 缺身份（sword_catalog_id/name 都没有或认不出）"
    info = sword_db.all_swords().get(sid) or {}
    if not name:
        name = info.get("name_zh") or info.get("name")
    form = target.get("form")
    if form not in ("normal", "kiwame"):
        status = target.get("form_status")
        form = status if status in ("normal", "kiwame") else None
    out = {"observation_id": target.get("observation_id"),
           "selection_policy": target.get("selection_policy"),
           "sword_catalog_id": sid,
           "name": name,
           "form": form,
           "sword_type": {"脇差": "胁差", "槍": "枪", "薙刀": "薙刀", "剣": "剑"}.get(
               info.get("type"), info.get("type")),
           "level": target.get("level"),
           "tou_level": target.get("tou_level"),
           "survival": target.get("survival"),
           "survival_max": target.get("survival_max")}
    return out, None


def _parse_level_token(text):
    m = _LEVEL_TOKEN.search(str(text or "").strip())
    return int(m.group(1)) if m else None


def _parse_fatigue_token(text):
    for cur, mx in _FATIGUE_PAIR.findall(str(text or "")):
        if mx == "100":
            return int(cur)
    return None


def parse_selection_rows(tokens):
    """列表整列 OCR token → 行列表（纯函数，可测）。

    姓名带只收 x∈[100,300) 的 token：左侧 x<100 是"N之M"位置标记和
    锁图标区（真机布局），落进姓名带的"N之M"标记也按格式排除——
    两者都不算"读不清的名字"。碎 token 按 y 归并成文本行再过名册
    校正；归并行整体校正不上时，恰好一个碎 token 能过名册则采纳它
    （其余是小字误读噪声，真机常客）。姓名带里的其他乱码仍计入
    unreadable（保守阻断），唯二例外：行内小字区误读（下方 50px
    内紧跟可校正名字行，丢弃不计）与页缘乱码（y<_TOP_EDGE_ROW_Y
    或 y>_EDGE_ROW_Y，
    翻页后必然送回页中部复核，留待那时裁决）。

    等级/疲劳（2026-09-22 真机校准）：行内四行小字同在 x∈[440,640)
    一列，按内容格式区分（疲劳=N/M 且上限 100；等级=N级）。「刀剑」
    与「乱舞」等级格式相同，刀剑行靠自体前缀或同 y 的「刀剑」label
    配对认领，乱舞等级不得冒充刀剑等级；刀剑等级在名字上方
    _LEVEL_ABOVE_NAME 区间内就近归属，疲劳在名字上方 ±_ROW_ATTACH_DY。

    行内小字区（名字上方 ~28~45px）的 OCR 误读会落进姓名带、自成
    乱码行——它们不是名字：仅当其下方 50px 内紧跟一行能过名册的
    名字时，按小字垃圾丢弃（不计 unreadable）；名字位本身的乱码
    （下方无紧邻名字行）仍保守计入 unreadable。

    Returns: (rows, unreadable_rows)。
    rows 每项: {"y", "name_raw", "name", "sword_catalog_id", "level",
    "fatigue", "form": None, "unknown_fields": [...]}。
    form 恒为 None——选择列表目前没有形态直读通道（盲区，如实标注）。
    """
    name_band, level_band, fatigue_band = [], [], []
    sword_label_ys = []
    for text, pt in tokens or []:
        text = str(text or "").strip()
        if not text:
            continue
        x, y = getattr(pt, "x", 0) or 0, getattr(pt, "y", 0) or 0
        if x < _NAME_X[0]:
            continue                # 位置标记/锁图标区：不是名字，不是乱码
        if x < _NAME_X[1]:
            if _POS_MARK.match(text):
                continue            # 落进姓名带的"N之M"标记：排除，不算乱码
            name_band.append((x, y, text))
        elif _FATIGUE_X[0] <= x < _FATIGUE_X[1]:
            fv = _parse_fatigue_token(text)
            if fv is not None:
                fatigue_band.append((y, fv))
                continue
            lv = _parse_level_token(text)
            if lv is not None and x < _LEVEL_X[1]:
                # 自体带「刀剑」前缀（合体 token）直接可信；分体值 token
                # 需同 y 有「刀剑」label 配对，防「乱舞 N级」冒充刀剑等级
                level_band.append((y, lv, text.startswith("刀剑")))
            elif text == "刀剑":
                sword_label_ys.append(y)

    # 名字碎 token 归行：按 y 排序后 proximity 归并，同组按 x 拼接
    name_band.sort(key=lambda t: (t[1], t[0]))
    lines = []
    for x, y, text in name_band:
        if lines and abs(y - lines[-1][0]) <= _ROW_MERGE_DY:
            lines[-1][1].append((x, text))
        else:
            lines.append([y, [(x, text)]])

    def _sid_of(parts):
        return _match_name("".join(t for _x, t in sorted(parts)))

    rows, unreadable = [], 0
    for idx, (y, parts) in enumerate(lines):
        name_raw = "".join(t for _x, t in sorted(parts))
        sid = _match_name(name_raw)
        if sid is None and len(parts) > 1:
            # 小字垃圾与名字同 y 被归并进一行（'今剑AN'）：恰好一个
            # 碎 token 能过名册时采纳它，其余是误读噪声
            hits = [t for _x, t in sorted(parts)
                    if _match_name(t) is not None]
            if len(hits) == 1:
                sid = _match_name(hits[0])
        if sid is None and idx + 1 < len(lines):
            gap = lines[idx + 1][0] - y
            if 0 < gap <= 50 and _sid_of(lines[idx + 1][1]) is not None:
                continue            # 行内小字区误读：丢弃，不算读不清的名字
        if sid is None:
            # 上下页缘的半截行不计入全表 unreadable：顶缘已在前一页
            # 露出，底缘会在下一页出现。首屏完整姓名始于 y≈196。
            # 只忽略乱码计数，不凭半行作点击证据。
            if _TOP_EDGE_ROW_Y <= y <= _EDGE_ROW_Y:
                unreadable += 1
        info = sword_db.all_swords().get(sid) if sid else None
        level = fatigue = None
        lo, hi = _LEVEL_ABOVE_NAME
        for ty, lv, self_labeled in sorted(level_band):
            if not (y - hi <= ty <= y - lo):
                continue
            if not self_labeled and not any(
                    abs(sy - ty) <= 10 for sy in sword_label_ys):
                continue            # 没有「刀剑」label 配对：疑是乱舞等级
            level = lv
            break
        for ty, fv in fatigue_band:
            if abs(ty - y) <= _ROW_ATTACH_DY:
                fatigue = fv
                break
        unknown = []
        if sid is None:
            unknown.append("name")
        if level is None:
            unknown.append("level")
        if fatigue is None:
            unknown.append("fatigue")
        unknown.append("form")   # 页面无形态通道，永远算缺口
        rows.append({"y": y, "name_raw": name_raw,
                     "name": (info.get("name_zh") or info["name"])
                             if info else None,
                     "sword_catalog_id": sid, "level": level,
                     "fatigue": fatigue, "form": None,
                     "unknown_fields": unknown})
    return rows, unreadable


def recognize_selection_lock(image, name_y):
    """从列表左侧底色与白锁区确认 locked/unlocked；其余 unknown。

    ROI 以刀名 y 动态定位，不把连续滚动名单伪装成固定行。阈值来自
    2026-09-24 MuMu 显存帧：黄底闭锁与灰底开锁同屏取样；半截行不取证。
    """
    if image is None or getattr(image, "shape", None) != (720, 1280, 3):
        return "unknown"
    y = int(name_y)
    if not 180 <= y <= 620:
        return "unknown"
    gold = image[y - 25:y - 5, 18:23]
    icon = image[y - 30:y + 15, 26:52]
    if gold.shape != (20, 5, 3) or icon.shape != (45, 26, 3):
        return "unknown"
    gold_pixels = ((gold[:, :, 0] < 70) & (gold[:, :, 1] > 130)
                   & (gold[:, :, 2] > 180))
    gray_pixels = ((gold.min(axis=2) > 130) & (gold.max(axis=2) < 210)
                   & ((gold.max(axis=2).astype("int16")
                       - gold.min(axis=2).astype("int16")) <= 3))
    white_pixels = ((icon[:, :, 0] > 225) & (icon[:, :, 1] > 225)
                    & (icon[:, :, 2] > 225))
    if white_pixels.mean() < 0.45:
        return "unknown"
    if gold_pixels.mean() >= 0.85:
        return "locked"
    if gray_pixels.mean() >= 0.85:
        return "unlocked"
    return "unknown"


def page_fingerprint(rows):
    """一页的指纹：翻页停滞/绕圈检测与回退定位用。"""
    parts = ((r["sword_catalog_id"] or r["name_raw"] or "?",
              r["level"], r["fatigue"]) for r in rows)
    # 同名多振有的行等级/疲劳漏读时，Python 不能直接比较 None 与整数；
    # 保留 None 作为证据缺口，只用排序键稳定化页指纹。
    return tuple(sorted(parts, key=lambda item: (
        item[0], -1 if item[1] is None else item[1],
        -1 if item[2] is None else item[2])))


def row_conflicts_target(row, target, match_fields):
    """行与目标在某字段上两边都有值且不一致 → True（冲突，排除）。
    任何一边缺值都不构成冲突（缺证据 ≠ 反证）。"""
    for field in match_fields:
        tv = target.get(field)
        if tv is None:
            continue
        if field == "name":
            rv = row.get("sword_catalog_id")
            if rv is not None and rv != target["sword_catalog_id"]:
                return True
        else:
            rv = row.get(field)
            if rv is not None and rv != tv:
                return True
    return False


def row_evidence_gaps(row, target, match_fields):
    """目标有值但行读不出的字段（唯一匹配时也要如实带出）。"""
    gaps = []
    for field in match_fields:
        if target.get(field) is None:
            continue
        if field == "name":
            if row.get("sword_catalog_id") is None:
                gaps.append("name")
        elif row.get(field) is None:
            gaps.append(field)
    return gaps


def decide_match(pages, target, match_fields=DEFAULT_MATCH_FIELDS,
                 unreadable_rows=0):
    """全表扫描后的裁决（纯函数）。「零冲突」只是候选，「证据充分」才放行。

    对调用方要求参与 match_fields 且 target 有值的身份字段，候选行缺值
    时不得 unique——只凭同位名字点击会把"没证据反对"误当"有证据确认"。
    只有满足以下全部条件才 unique：全表恰好一条零冲突且证据充分的行、
    没有证据不足的同名行、没有读不清的行、扫描域完整（由调用方保证，
    截断扫描不得进入本裁决）。

    Returns:
        {"status": "unique", "page", "row", "evidence_gaps": []}
        {"status": "ambiguous", "candidates", "reason", "missing_evidence"}
        {"status": "not_found", "reason"}
    """
    confirmed, unconfirmed = [], []
    for page_idx, rows in enumerate(pages):
        for row in rows:
            sid = row.get("sword_catalog_id")
            if sid is None or sid != target["sword_catalog_id"]:
                continue  # 读不清的行单独算 unreadable，别家的刀无关
            if row_conflicts_target(row, target, match_fields):
                continue
            missing = row_evidence_gaps(row, target, match_fields)
            if missing:
                unconfirmed.append((page_idx, row, missing))
            else:
                confirmed.append((page_idx, row))

    # 跨页弱副本剔除（2026-09-22 真机）：翻页重叠区里同一物理行会
    # 出现两次——页缘半遮行（刀剑等级在可视区外，level=None）和页中部
    # 完整行。半遮行缺证据但不构成反证；若它的已知字段与某 confirmed
    # 行逐项一致（缺值视为相容），视为该确认行的弱读取副本，剔除。
    # 安全边界：剔除只发生在「已知字段全一致」时——同名两振若疲劳或
    # 等级任一不同，半遮副本不会被剔，照样 ambiguous 停下；裁决点击
    # 永远只落在证据充分的确认行上。
    if confirmed and unconfirmed and not any(
            field in match_fields for field in ('tou_level', 'survival_max', 'recon')):
        def _weak_copy_of_confirmed(item):
            _p, r, _m = item
            for _cp, c in confirmed:
                if r.get("sword_catalog_id") != c.get("sword_catalog_id"):
                    continue
                if r.get("level") is not None \
                        and r.get("level") != c.get("level"):
                    continue
                if r.get("fatigue") is not None \
                        and r.get("fatigue") != c.get("fatigue"):
                    continue
                return True
            return False
        unconfirmed = [item for item in unconfirmed
                       if not _weak_copy_of_confirmed(item)]

    if len(confirmed) == 1 and not unconfirmed and not unreadable_rows:
        page_idx, row = confirmed[0]
        return {"status": "unique", "page": page_idx, "row": row,
                "evidence_gaps": []}

    if not confirmed and not unconfirmed:
        return {"status": "not_found",
                "reason": ("整份列表没有与目标零冲突的行：目标可能被游戏"
                           "隐藏/禁用（在别队、手入/修行、同位互斥、远征中），"
                           "或档案字段已陈旧（等级变了会对不上）")}

    missing_evidence = sorted({f for _p, _r, m in unconfirmed for f in m})
    candidates = [_row_summary(r) for _p, r in
                  [(p, r) for p, r in confirmed]
                  + [(p, r) for p, r, _m in unconfirmed]]
    if len(confirmed) > 1:
        reason = (f"同名同型候选 {len(confirmed)} 振在可观察字段上"
                  "拉不开，拒绝点第一条（不伪造一号/二号）")
    elif unconfirmed:
        reason = (f"同名候选缺身份证据（{'、'.join(missing_evidence)}）："
                  "缺证据不等于没冲突，不能排除是另一振，拒绝下点")
    else:  # 唯一确认行，但有读不清的行不能排除
        reason = (f"有 {unreadable_rows} 行名字读不清，不能排除"
                  "目标是其一，拒绝下点")
    return {"status": "ambiguous", "candidates": candidates,
            "missing_evidence": missing_evidence, "reason": reason}


def decide_locked_highest(pages, target, unreadable_rows=0):
    """完整名单中只选确认上锁的同名刀，等级最高必须唯一。"""
    # 连续滚动的相邻视口会重叠。只有至少两行独立锚点给出同一位移，
    # 才合并跨页副本；半截行可缺字段，但已读字段不得互相矛盾。
    # 同一页的同名行永不合并，证据不足时宁可判并列。
    duplicates = set()
    def compatible(before, after):
        if (before.get("sword_catalog_id") is None or
                before.get("sword_catalog_id") != after.get("sword_catalog_id")):
            return False
        for field in ("level", "fatigue", "lock_status"):
            left, right = before.get(field), after.get(field)
            if field == "lock_status":
                left = None if left == "unknown" else left
                right = None if right == "unknown" else right
            if left is not None and right is not None and left != right:
                return False
        return True
    def evidence(row):
        return (sum(row.get(field) is not None for field in ("level", "fatigue"))
                + (row.get("lock_status") in ("locked", "unlocked")),
                _TOP_EDGE_ROW_Y <= (row.get("y") or 0) <= _EDGE_ROW_Y)
    for p in range(1, len(pages)):
        prev, cur = pages[p - 1], pages[p]
        shifts = {after["y"] - before["y"]
                  for before in prev for after in cur
                  if before.get("y") is not None and after.get("y") is not None
                  and after["y"] < before["y"]
                  and compatible(before, after)
                  and (before.get("level") is not None or
                       after.get("level") is not None)}
        mappings = set()
        for shift in shifts:
            matches = []
            for i, before in enumerate(prev):
                if before.get("y") is None:
                    continue
                forward = [j for j, after in enumerate(cur)
                           if after.get("y") is not None
                           and abs(after["y"] - before["y"] - shift) <= 4
                           and compatible(before, after)]
                if len(forward) != 1:
                    continue
                j = forward[0]
                reverse = [k for k, other in enumerate(prev)
                           if other.get("y") is not None
                           and abs(cur[j]["y"] - other["y"] - shift) <= 4
                           and compatible(other, cur[j])]
                if reverse == [i]:
                    matches.append((i, j))
            if len(matches) >= 2 and len({j for _, j in matches}) == len(matches):
                # 两个不同 y 的物理行支持同一位移；已读字段相容。
                # 同名并列也能作锚点，但竞争位移同样有两行时不猜。
                mappings.add(frozenset(matches))
        if not mappings:
            continue
        best_size = max(len(mapping) for mapping in mappings)
        best = [mapping for mapping in mappings if len(mapping) == best_size]
        if len(best) != 1:
            continue
        for i, j in best[0]:
            if evidence(cur[j]) > evidence(prev[i]):
                duplicates.add((p - 1, i))
            else:
                duplicates.add((p, j))
    candidates = [(p, r) for p, rows in enumerate(pages) for j, r in enumerate(rows)
                  if (p, j) not in duplicates
                  and r.get("sword_catalog_id") == target["sword_catalog_id"]]
    if not candidates:
        return {"status": "not_found", "reason": "名单里没有这位刀剑"}
    locked = [(p, r) for p, r in candidates if r.get("lock_status") == "locked"
              and r.get("level") is not None]
    highest = max((r["level"] for _, r in locked), default=None)
    uncertain = [(p, r) for p, r in candidates
                 if r.get("lock_status") != "unlocked" and
                 (r.get("level") is None or
                  (r.get("lock_status") != "locked" and
                   (highest is None or r["level"] >= highest)))]
    if unreadable_rows or uncertain:
        return {"status": "ambiguous", "candidates": [_row_summary(r) for _, r in candidates],
                "missing_evidence": ["锁/等级/名字"],
                "reason": "名单有锁、等级或名字没认清，无法证明上锁刀中的最高级"}
    if highest is None:
        return {"status": "not_found", "reason": "没有确认上锁的同名刀"}
    top = [(p, r) for p, r in locked if r["level"] == highest]
    if len(top) != 1:
        return {"status": "ambiguous", "candidates": [_row_summary(r) for _, r in top],
                "missing_evidence": [],
                "reason": f"上锁刀中 Lv{highest} 有 {len(top)} 条，无法唯一确定"}
    page, row = top[0]
    return {"status": "unique", "page": page, "row": row, "evidence_gaps": []}


def _row_summary(row):
    return {"name": row.get("name") or row.get("name_raw"),
            "level": row.get("level"), "fatigue": row.get("fatigue"),
            "form": row.get("form"),
            "unknown_fields": list(row.get("unknown_fields") or [])}


def slot_matches_target(slot, target, match_fields=DEFAULT_MATCH_FIELDS):
    """编队槽观察 vs 目标身份：三态 True / False / None（证据不足）。

    刀种目录必须一致（不一致=False）。对 match_fields 里 target 有值的
    身份字段：槽位有值且冲突 → False；槽位缺值 → None（证据不足，
    不算确认也不算排除）。目标 form 已知而槽位形态读不出时绝不通过——
    换前不能零点击宣称 already_correct。
    疲劳不在默认 match_fields 里（自然恢复，不是身份证据）。
    """
    status = slot.get("slot_status")
    if status == "empty":
        return False
    if status != "occupied":
        return None
    sid = slot.get("sword_catalog_id")
    if not sid:
        return None
    if sid != target["sword_catalog_id"]:
        return False
    for field in match_fields:
        if field == "name":
            continue
        tv = target.get(field)
        if tv is None:
            continue
        if field == "form":
            sv = slot.get("kiwame_status")
            if sv in ("kiwame", "normal"):
                if sv != tv:
                    return False
            else:
                return None        # 形态证据不足
        else:
            sv = slot.get(field)
            if sv is None:
                return None        # 字段证据不足
            if sv != tv:
                return False
    return True


# ==================== 远征占用预检（预设编队用） ====================

def _expedition_busy(team_no):
    """目标队此刻是否还在远征（读 STATE_DIR/expeditions.json）。
    口径同 panel.scheduler.team_available：dispatched_at + duration_min
    没过完就算占用；文件缺失/损坏/字段缺一律当作没占用，不拦。
    （touken 层不 import panel，故口径在此复写一份，别反向依赖。）"""
    try:
        records = json.loads(
            (STATE_DIR / "expeditions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    record = records.get(str(team_no), {}) if isinstance(records, dict) else {}
    try:
        end = time.mktime(time.strptime(record["dispatched_at"],
                                        "%Y-%m-%d %H:%M:%S"))
        end += int(record["duration_min"]) * 60
        return time.time() < end
    except (KeyError, TypeError, ValueError):
        return False


def _slot_no(key):
    """槽位键转 int；认不出的键返回 0——ensure 会按 INVALID_REQUEST 收拾它。"""
    try:
        return int(key)
    except (TypeError, ValueError):
        return 0


# ==================== 执行器 ====================

class FormationEditorMixin:
    """共用编队执行器。依赖宿主：maa、config、record_event、
    navigate_to_stream、current_location，以及 TeamRosterMixin 的
    _read_team_page / _read_row_label（经 _formation_* 注入缝调用）。"""

    # ---- 注入缝（测试/未来页面差异收口处） ----

    def _formation_read_team(self):
        """当前帧读六槽（默认复用 TeamRosterMixin 的只读点名）。"""
        return self._read_team_page()

    def _formation_row_label(self, cy):
        """读行首"N之M"位置标签（切队正面确认）。"""
        return self._read_row_label(cy)

    def _formation_link_visible_slot(self, slot_no, slot):
        """预设槽位零点击前，用最新完整刀账核对当前这振的可见指纹。"""
        from ..custom_formations import _current_candidate_pool
        try:
            pool = _current_candidate_pool()
        except Exception as exc:
            return {"status": "unavailable", "observation_id": None,
                    "reason": f"读取完整刀账失败：{exc}"}
        if not pool.get("done"):
            return {"status": "insufficient", "observation_id": None}
        observed = dict(slot)
        observed["stats"] = self._read_slot_stats(slot_no)
        from ..formation_identity import cultivation_pairs
        from .naihanka import _load_naihanka_state
        cultivation = cultivation_pairs(_load_naihanka_state(),
                                        pool.get("observed_at"),
                                        (pool.get("source") or {}).get("snapshot_id"))
        return link_visible_slot(observed, pool.get("entries") or [], cultivation)

    # ---- 筛选/排序面板 ----

    def _open_filter_panel(self):
        """点「筛选/排序」开面板，标题 OCR 正面确认。Returns bool。"""
        text, roi = _FILTER_PANEL_TITLE
        for _ in range(2):
            self.maa.screenshot(force=True)
            if self.maa.ocr(text, roi_4to4(*roi)):
                return True             # 已经开着
            pt = self.maa.ocr(_FILTER_OPEN_TEXT,
                              roi_4to4(*_FILTER_OPEN_ROI))
            if not pt:
                # 已套过筛选时入口文字变成「筛选中」。同一按钮、同一 ROI。
                pt = self.maa.ocr("筛选中", roi_4to4(*_FILTER_OPEN_ROI))
            if not pt:
                # 真机上「筛选中」的细白字偶发完全读不出。列表标题正面
                # 命中、且同源帧按钮内部是深灰时，才用标定过的按钮中心。
                image = self.maa.screenshot(force=True)
                title, title_roi = _LIST_TITLE
                if (getattr(image, "shape", None) == (720, 1280, 3)
                        and self.maa.ocr(title, roi_4to4(*title_roi))
                        and max(int(v) for v in image[100, 850]) < 75
                        and max(int(v) for v in image[85, 830]) < 75):
                    pt = Point(850, 100)
            if not pt:
                time.sleep(0.5)
                continue
            self.maa.click(pt)
            time.sleep(0.8)
        self.maa.screenshot(force=True)
        return bool(self.maa.ocr(text, roi_4to4(*roi)))

    def _click_panel_button(self, text, roi=None, require_selected=False):
        """面板里按文字（exact）找按钮并点。Returns 找没找到。"""
        self.maa.screenshot(force=True)
        pt = self.maa.ocr(text, roi_4to4(*(roi or _FILTER_PANEL_ROI)),
                          match_mode="exact")
        if not pt:
            return False
        self.maa.click(pt)
        time.sleep(0.5)
        if require_selected:
            image = self.maa.screenshot(force=True)
            if image is None or getattr(image, "shape", None) != (720, 1280, 3):
                return False
            # 实际运行帧：已选按钮为绿底，未选为深灰。点在文字中心，
            # 左上偏 45×14px 取底色，避开白色字和按钮右侧斜角。
            x, y = int(pt.x) - 45, int(pt.y) - 14
            if not (0 <= x < 1280 and 0 <= y < 720):
                return False
            b, g, r = (int(v) for v in image[y, x])
            if not (b < 70 and g > 125 and r > 90):
                return False
        return True

    def _apply_list_filter(self, tgt):
        """筛选包装：失败兜底点面板 X 回名单页——筛选面板不在 navigator
        页面地图里，留着它开着会把收尾导航卡死（2026-09-22 真机实锤）。
        点不上也不追加尝试，结果已定。"""
        ok = yield from self._apply_list_filter_flow(tgt)
        if not ok:
            self.maa.click(Point(*_FILTER_CLOSE_X))
            time.sleep(0.8)
            self._wait_list_open(attempts=4)
        return ok

    def _apply_list_filter_flow(self, tgt):
        """按目标刀种（+形态）预筛名单。Returns（yield from 接）bool。

        流程：开面板 →「取消筛选」重置（游戏记住上次条件）→ 点刀种 →
        目标形态已知时点「初/极」→「确定」→ 确认回到名单。任何一步
        认不到就如实 False：此时列表开着、未点决定，队伍无变化。
        目标没刀种信息时返回 True 不筛（退化全表扫，由扫描纪律兜底）。
        """
        stype = tgt.get("sword_type")
        if not stype or stype not in _FILTER_TYPES:
            return True
        form_text = _FILTER_FORM_TEXT.get(tgt.get("form"))
        yield (f"[编队] 先筛名单：{stype}"
               + (f"＋{form_text}" if form_text else ""))
        if not self._open_filter_panel():
            yield "[编队] 筛选面板打不开（名单未动，未做任何变更），停"
            return False
        if not self._click_panel_button(_FILTER_RESET_TEXT):
            yield ("[编队] 「取消筛选」认不到：不敢带着未知的上次筛选"
                   "条件扫名单，停")
            return False
        # 「取消筛选」可能顺手关了面板：关了就重开再选
        text, roi = _FILTER_PANEL_TITLE
        self.maa.screenshot(force=True)
        if not self.maa.ocr(text, roi_4to4(*roi)):
            if not self._open_filter_panel():
                yield "[编队] 重置筛选后叫不回筛选面板，停"
                return False
        ranked = tgt.get("selection_policy") == "locked_highest_level"
        if not self._click_panel_button(stype, _FILTER_TYPE_ROI,
                                        require_selected=ranked):
            yield f"[编队] 筛选面板里认不到「{stype}」按钮，停"
            return False
        if form_text and not self._click_panel_button(form_text,
                                                      _FILTER_FORM_ROI,
                                                      require_selected=ranked):
            yield f"[编队] 筛选面板里认不到「{form_text}」按钮，停"
            return False
        if not self._click_panel_button(_FILTER_CONFIRM_TEXT,
                                        _FILTER_CONFIRM_ROI):
            yield "[编队] 筛选「确定」认不到，停"
            return False
        if not self._wait_list_open():
            yield "[编队] 筛选确定后回不到名单页，停"
            return False
        return True

    # ---- 对外契约 ----

    def ensure_team_member(self, team_no, slot_no, target, **kw):
        """非流式便捷入口：跑完返回机器可读结果 dict。"""
        gen = self.ensure_team_member_stream(team_no, slot_no, target, **kw)
        while True:
            try:
                next(gen)
            except StopIteration as stop:
                return stop.value

    def ensure_team_member_from_honmaru_stream(self, team_no, slot_no,
                                               target, **kw):
        """standalone 入口：从本丸安全进入目录→编队后执行（供前端调用）。
        目录路径不是玩法自动化的必经路；已在部队选择页的调用方直接用
        ensure_team_member_stream(entry_context="team_select")。"""
        for msg in self.navigate_to_stream("编队"):
            yield msg
        kw["entry_context"] = "formation"
        return (yield from self.ensure_team_member_stream(
            team_no, slot_no, target, **kw))

    def ensure_team_member_stream(self, team_no, slot_no, target,
                                  entry_context="auto",
                                  match_fields=None, max_pages=None):
        """把部队 team_no(1~5) 的 slot_no(1~6) 换成明确目标。

        entry_context:
          "auto"       已在编队/部队选择页则原地执行；都不在 → 导航去编队；
          "formation"  要求在/导航到"部队编成"页；
          "team_select" 要求当前已在"部队选择"页，不在就如实失败（不乱逛）。
        Returns（yield from 接）: 机器可读结果 dict（RESULT_SCHEMA_VERSION）。
        """
        cfg = self.config.get("formation_editor", {}) \
            if isinstance(getattr(self, "config", None), dict) else {}
        match_fields = tuple(match_fields
                             or cfg.get("match_fields")
                             or DEFAULT_MATCH_FIELDS)
        max_pages = int(max_pages or cfg.get("max_pages") or _MAX_PAGES)

        tgt, err = normalize_target(target)
        self._selection_identity_target = None
        observation = str((tgt or {}).get("observation_id") or "")
        client_serial = int(observation[6:]) if re.fullmatch(r"youzu:\d+", observation) else None
        ranked = bool(tgt and tgt.get("selection_policy") == "locked_highest_level")
        if ranked and tgt.get("form") not in ("normal", "kiwame"):
            err = "按等级选人需要明确普通/极形态"
        if err or not isinstance(team_no, int) or team_no not in _TEAM_TAB \
                or not isinstance(slot_no, int) or not 1 <= slot_no <= 6:
            reason = err or f"team_no/slot_no 越界（{team_no}/{slot_no}）"
            yield f"[编队] 请求无效：{reason}"
            return self._finish(INVALID_REQUEST, team_no, slot_no, target,
                                reason)

        yield (f"[编队] 目标：部队{team_no} {slot_no}号位 ← "
               f"{tgt['name']}（{tgt['sword_catalog_id']}，"
               f"形态 {tgt['form'] or '未知'}，Lv {tgt['level'] or '?'}）")

        # 1) 站上共用编队表面（两种外壳同一条链路）
        shell = yield from self._ensure_surface(entry_context)
        if shell is None:
            yield "[编队] 既不在部队编成也不在部队选择页，停"
            return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                "无法确认处于共用编队表面（编成/部队选择）")
        yield f"[编队] 当前外壳：{'部队编成' if shell == 'formation' else '部队选择'}"

        # 2) 切队正面确认（位置标签必须显示请求的队号）
        if not (yield from self._select_team_confirmed(team_no)):
            yield f"[编队] 部队{team_no}切队未确认，停"
            return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                f"部队{team_no}标签切队未确认，不落任何点击",
                                entry_shell=shell)

        # 3) 换前观察：三态验收——确认是目标才零点击；证据不足不冒充正确，
        #    但可以继续打开名单寻找明确目标
        team_before = self._formation_read_team()
        slot_before = team_before[slot_no - 1] if team_before else None
        if slot_before is None:
            yield f"[编队] {slot_no}号位读不出（失明页？），停"
            return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                "换前观察失败：整页读不出", entry_shell=shell)
        if client_serial is not None and not ranked:
            from ..selection_identity import client_events, identity_target, selected_serial, identity_unique
            events = client_events(self.maa)
            evidence = identity_target(events, client_serial, time.time() - 300)
            if (not evidence or evidence['level'] is None or evidence['tou_level'] is None
                    or evidence['sword_catalog_id'] != tgt['sword_catalog_id']):
                yield "[编队] 没有读到近期编队的目标编号和数值，未换人"
                return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                    "本次客户端身份资料不完整", entry_shell=shell)
            tgt.update(evidence)
            match_fields = tuple(field for field in
                                 ('name', 'level', 'tou_level', 'survival_max', 'recon')
                                 if field == 'name' or tgt.get(field) is not None)
            self._selection_identity_target = tgt
            # 进入编队时的完整回传已能确认现有成员；同一振再次点决定
            # 不会产生 setsword，不能要求它晚于本槽换人流程的开始时间。
            if selected_serial(events, team_no, slot_no, time.time() - 300) == client_serial:
                yield f"[编队] {slot_no}号位客户端编号已是目标，无需换人"
                return self._finish(ALREADY_CORRECT, team_no, slot_no, tgt,
                                    "本次客户端部队编号与目标一致", entry_shell=shell)
            if not identity_unique(events, client_serial, time.time() - 300):
                yield '[编队] 客户端同名刀的可见数值无法区分，本次跳过，不扫描到底'
                return self._finish(AMBIGUOUS, team_no, slot_no, tgt,
                                    '客户端身份数值不能唯一对应列表中的刀', entry_shell=shell)
        m = slot_matches_target(slot_before, tgt, match_fields)
        nonlevel_fields = tuple(field for field in match_fields if field != "level")
        nonlevel_match = slot_matches_target(slot_before, tgt, nonlevel_fields)
        if nonlevel_match is not False and tgt.get("observation_id"):
            link = self._formation_link_visible_slot(slot_no, slot_before)
            slot_before["visible_link"] = link
            m = (True if link.get("status") == "linked" and
                 link.get("observation_id") == tgt["observation_id"]
                 else None)
            # 刀剑等级增长可与旧目标不同；其余可见指纹在完整刀账中唯一
            # 才能证明是同一振。形态章漏识别不能单独否决这条实例证据。
        if m is True and not ranked and client_serial is None:
            yield f"[编队] {slot_no}号位已确认是目标，零点击收工"
            return self._finish(ALREADY_CORRECT, team_no, slot_no, tgt,
                                "换人前已确认目标就在该槽位，未做任何换人点击",
                                entry_shell=shell,
                                before=slot_before,
                                team_before=team_before)
        if m is None:
            yield (f"[编队] {slot_no}号位读数与目标不足以互相确认"
                   "（形态/等级/实例证据缺口），不能零点击宣称正确——"
                   "打开名单寻找明确目标")

        # 4) 点"替换"，确认进入"刀剑男士选择"
        cy = _ROW_CY[slot_no - 1]
        self.maa.click(Point(_SWAP_X, cy))
        if not self._wait_list_open():
            yield "[编队] 刀剑男士选择列表没打开，停（未做任何变更）"
            return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                "点了替换但选择列表未出现", entry_shell=shell,
                                before=slot_before, team_before=team_before)

        # 4.5) 按刀种（+形态）预筛名单：全表几百振缩到几页，同名多振
        #      筛「初/极」后只剩目标形态。筛选只决定名单里有什么，
        #      裁决仍按行证据——筛错顶多 not_found，不会换错人
        if not (yield from self._apply_list_filter(tgt)):
            return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                "筛选名单失败（面板识别中断），未点决定",
                                entry_shell=shell,
                                before=slot_before, team_before=team_before)

        # 5) 全表扫描（指纹停滞=到底 / 绕圈 / 截断三种结局分明），
        #    只有确定扫到底的完整扫描才允许裁决唯一——截断名单上的
        #    "唯一"既找不到后段目标，也证明不了全局唯一
        swipe_next, swipe_prev = (
            (_SWIPE_RANKED_NEXT, _SWIPE_RANKED_PREV) if ranked else
            (_SWIPE_NEXT, _SWIPE_PREV))
        direct_client = client_serial is not None and not ranked
        if direct_client:
            row, target_page = yield from self._find_client_target_stream(tgt, match_fields, max_pages)
            pages, bars, unreadable, scan_status = [[]] * (target_page + 1), [], 0, 'complete'
            verdict = ({'status': 'unique', 'row': row, 'page': target_page} if row else
                       {'status': 'not_found', 'reason': '当前筛选名单没读到目标的完整数值'})
        else:
            pages, fps, bars, current_idx, unreadable, scan_status = \
                yield from self._scan_selection_list(
                    max_pages, swipe_next=swipe_next, swipe_prev=swipe_prev)
        if scan_status != "complete":
            why = {"truncated": "触达安全上限仍未到底",
                   "loop": "翻页指纹绕圈，页序异常",
                   "stalled": "连续滑动无响应且无法证明到底"
                              "（滑动可能被模拟器吞掉）",
                   "blind": "整页 OCR 失明，一行都读不出（页面识别失败）",
                   }.get(scan_status, scan_status)
            yield (f"[编队] 扫描未到底（scan_incomplete={scan_status}）："
                   f"已扫 {len(pages)} 页，{why}，不能证明全局唯一，拒绝下点")
            return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                f"scan_incomplete：{why}（已扫 {len(pages)} 页），"
                                "不点击、不裁决", entry_shell=shell,
                                before=slot_before, team_before=team_before,
                                pages_scanned=len(pages),
                                scan_status=scan_status)
        if not direct_client:
            verdict = (decide_locked_highest(pages, tgt, unreadable) if ranked
                       else decide_match(pages, tgt, match_fields, unreadable))
        if verdict["status"] == "not_found":
            yield f"[编队] 翻遍 {len(pages)} 页没找到目标：{verdict['reason']}"
            return self._finish(NOT_FOUND, team_no, slot_no, tgt,
                                verdict["reason"], entry_shell=shell,
                                before=slot_before, team_before=team_before,
                                pages_scanned=len(pages))
        if verdict["status"] == "ambiguous":
            yield f"[编队] 目标不唯一：{verdict['reason']}"
            return self._finish(AMBIGUOUS, team_no, slot_no, tgt,
                                verdict["reason"], entry_shell=shell,
                                before=slot_before, team_before=team_before,
                                candidates=verdict["candidates"],
                                missing_evidence=verdict.get(
                                    "missing_evidence", []),
                                pages_scanned=len(pages))

        # 6) 把目标行翻回视野：列表是连续滚动不吸附整页，不以扫描页
        #    指纹为落点——方向由滑块底缘地标给出，落地条件就是目标行
        #    在帧内且通过行证据裁决（与扫描同一道闸），坐标以落地帧
        #    实读为准
        target_page = verdict["page"]
        row = verdict["row"]
        if ranked:
            tgt["level"] = row["level"]
            match_fields = ("name", "level")
        yield (f"[编队] 唯一匹配在第 {target_page + 1} 页："
               f"{row['name']} Lv{row.get('level') or '?'}")
        if not direct_client:
            row = yield from self._goto_page(
                bars, target_page, tgt, row, match_fields,
                swipe_next=swipe_next, swipe_prev=swipe_prev)
        if row is None:
            yield "[编队] 无法在列表里重新定位目标行，停（未点决定）"
            return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                "列表连续滚动不吸附，目标行重新定位失败",
                                entry_shell=shell, before=slot_before,
                                team_before=team_before)

        selected_at = time.time() - 1
        self.maa.click(Point(_DECIDE_X, row["y"] + _DECIDE_DY))
        if not self._wait_list_closed():
            yield ("[编队] 决定已点但列表不关闭：目标可能被游戏规则禁用"
                   "（手入/修行/同位互斥/远征中），停")
            return self._finish(UNAVAILABLE, team_no, slot_no, tgt,
                                "决定未生效：目标不可选或已被占用",
                                entry_shell=shell, before=slot_before,
                                team_before=team_before)

        if client_serial is not None and not ranked:
            from ..selection_identity import client_events, selected_serial
            actual = selected_serial(client_events(self.maa), team_no, slot_no, selected_at)
            if actual != client_serial:
                yield "[编队] 选入后客户端编号未核对成功，停止；不继续装备或出发"
                return self._finish(SCREEN_UNRECOGNIZED, team_no, slot_no, tgt,
                                    "已点决定，但实际编号未确认是目标；队伍可能已变化",
                                    entry_shell=shell, before=slot_before,
                                    team_before=team_before)
        # 「决定」后列表已关闭就结束。本阶段不再 OCR 回读当前队伍：回读
        # 误识别不能反过来把一次正常换人报成失败，也不拿未经独立盘点的
        # 读数刷新编队档案。
        verification = "客户端编号已确认" if client_serial is not None and not ranked else "未做编队回读"
        yield (f"[编队] ✓ 已决定：部队{team_no} {slot_no}号位 ← "
               f"{tgt['name']}（{verification}）")
        return self._finish(CHANGED, team_no, slot_no, tgt,
                            f"已点击决定且选择列表正常关闭；{verification}",
                            entry_shell=shell, before=slot_before,
                            team_before=team_before,
                            pages_scanned=len(pages))

    def apply_preset_formation_stream(self, team_no: int, slots: dict,
                                      name: str = "预设编队"):
        """把预设编队套到游戏内部队 team_no(1~5)：逐槽 ensure 换人。

        先预检目标队远征占用（占用直接停，不做还原），导航到编队页切队后
        按槽位号升序逐槽 ensure（entry_context="formation"）。任何一槽结果
        不是 changed/already_correct 就停：队伍是半套状态，如实汇报，
        不还原、不装绿。
        Returns（yield from 接）: True 全部落妥 / False 没应用完。
        """
        if not isinstance(team_no, int) or team_no not in _TEAM_TAB:
            yield f"[预设编队] 部队编号 {team_no} 不认识，无法换人（只支持 1~5）"
            return False
        if not isinstance(slots, dict) or not slots:
            yield ("[预设编队] 这套预设一个位置都没指定，"
                   "无法应用：去编队页编辑一下")
            return False
        if _expedition_busy(team_no):
            yield (f"[预设编队] 部队{team_no}还在远征没回来，"
                   "换不了人，等收远征再说")
            return False

        for msg in self.navigate_to_stream("编队"):
            yield msg
        if self.current_location != "编队":
            yield "[预设编队] 无法到编队页，预设没应用完"
            return False
        self.maa.click(Point(*_TEAM_TAB[team_no]))
        time.sleep(1.5)

        changed = already = 0
        for slot_key in sorted(slots, key=_slot_no):
            slot_no = _slot_no(slot_key)
            # 选择列表没有形态直读通道（行 form 恒 None）：预设槽位里的
            # form_status 是档案结论，放进 match_fields 只会让每行都背上
            # 证据缺口而必判 ambiguous。收窄到列表真正能出示证据的字段；
            # 同名多振靠等级拉开，等级也拉不开就如实 ambiguous 停下。
            result = yield from self.ensure_team_member_stream(
                team_no, slot_no, slots[slot_key], entry_context="formation",
                match_fields=("name", "level"))
            verdict = result.get("result") if isinstance(result, dict) else None
            if verdict == ALREADY_CORRECT:
                already += 1
                continue
            if verdict == CHANGED:
                changed += 1
                continue
            reason = result.get("reason") if isinstance(result, dict) \
                else f"ensure 没返回结果 dict（{result!r}）"
            yield (f"[预设编队] 卡在{slot_no}号位：{reason}，"
                   "预设没应用完，队伍现在是半套，去看看")
            return False
        equipment_slots = [(int(key), entry)
                          for key, entry in slots.items()
                          if isinstance(entry, dict)
                          and any(entry.get(field) for field in
                                  ("troops", "horse", "charm", "treasure"))]
        if equipment_slots:
            from .formation_treasure import equip_preset_treasure_stream
            from .formation_troops import equip_preset_troop_stream
            from .formation_accessory import equip_preset_accessory_stream
            for slot_no, equipment in equipment_slots:
                current_team = self._formation_read_team()
                current = (current_team[slot_no - 1]
                           if current_team and len(current_team) >= slot_no else {})
                expected = slots[str(slot_no)]
                expected_id = expected.get("sword_catalog_id")
                if (current.get("slot_status") != "occupied"
                        or not expected_id
                        or current.get("sword_catalog_id") != expected_id):
                    yield (f"[预设编队] {slot_no}号位的刀剑未能重新确认，"
                           "装备不换，预设没应用完")
                    return False
                for position, name in sorted(equipment.get("troops", {}).items()):
                    equipped = yield from equip_preset_troop_stream(
                        self, slot_no, int(position), name)
                    if not equipped:
                        yield (f"[预设编队] {slot_no}号位第{position}格刀装没装妥，"
                               "预设没应用完，绝不继续出发")
                        return False
                for kind, label in (("horse", "马"), ("charm", "御守")):
                    if equipment.get(kind):
                        equipped = yield from equip_preset_accessory_stream(
                            self, slot_no, kind, equipment[kind])
                        if not equipped:
                            yield (f"[预设编队] {slot_no}号位的{label}没装妥，"
                                   "预设没应用完，绝不继续出发")
                            return False
                if equipment.get("treasure"):
                    equipped = yield from equip_preset_treasure_stream(
                        self, slot_no, equipment["treasure"])
                    if not equipped:
                        yield (f"[预设编队] {slot_no}号位的宝物没装妥，"
                               "预设没应用完，绝不继续出发")
                        return False
        yield (f"[预设编队] ✓ 『{name}』已覆盖部队{team_no}："
               f"换好 {changed} 位，{already} 位本来就在")
        return True

    # ---- 外壳与切队 ----

    def _detect_shell(self):
        """认当前编队外壳：标题 OCR 判定，不看主题色。"""
        self.maa.screenshot(force=True)
        text, roi = _FORMATION_TITLE
        if self.maa.ocr(text, roi_4to4(*roi)):
            return "formation"
        text, roi = _TEAM_SELECT_TITLE
        if self.maa.ocr(text, roi_4to4(*roi)):
            return "team_select"
        return None

    def _ensure_surface(self, entry_context):
        if entry_context in ("auto", "formation", "team_select"):
            shell = self._detect_shell()
            if shell and (entry_context == "auto" or shell == entry_context):
                return shell
            if entry_context == "team_select":
                return None  # 指定了部队选择却不在：如实失败，不乱逛
            # auto/formation：导航到编队（navigate_to_stream 自带 verify）
            for _msg in self.navigate_to_stream("编队"):
                yield _msg
            return self._detect_shell()
        return None

    def _select_team_confirmed(self, team_no):
        """点部队标签并用位置标签正面确认（team_roster 同款纪律）。"""
        for attempt in (1, 2, 3):
            self.maa.click(Point(*_TEAM_TAB[team_no]))
            time.sleep(1.0)
            self.maa.screenshot(force=True)
            seen = (self._formation_row_label(_ROW_CY[0]),
                    self._formation_row_label(_ROW_CY[5]))
            if seen == (team_no, team_no):
                return True
            yield f"[编队] 切部队{team_no}第 {attempt}/3 次未确认，重试"
        return False

    # ---- 列表交互 ----

    def _wait_list_open(self, attempts=10):
        text, roi = _LIST_TITLE
        for _ in range(attempts):
            self.maa.screenshot(force=True)
            if self.maa.ocr(text, roi_4to4(*roi)):
                return True
            time.sleep(0.5)
        return False

    def _wait_list_closed(self, attempts=8):
        """决定/弹窗处理：列表标题消失才算生效；期间兜底确认弹窗。"""
        text, roi = _LIST_TITLE
        for _ in range(attempts):
            time.sleep(0.5)
            self.maa.screenshot(force=True)
            pt = self.maa.template_match(_CONFIRM_POPUP_TEMPLATE,
                                         threshold=0.7)
            if pt:
                self.maa.click(pt)
                time.sleep(0.8)
                self.maa.screenshot(force=True)
            if not self.maa.ocr(text, roi_4to4(*roi)):
                return True
        return False

    def _parse_selection_rows(self, tokens):
        """注入缝：默认调纯函数 parse_selection_rows；未来真机校准出
        形态/其他行内通道时在这里给行补证据（测试也经此注入剧本证据）。"""
        return parse_selection_rows(tokens)

    def _read_list_page(self):
        """当前帧读列表页。Returns (rows, unreadable)。"""
        image = self.maa.screenshot(force=True)
        tokens = self.maa.ocr_all(roi_4to4(*_LIST_ROI)) or []
        rows, unreadable = self._parse_selection_rows(tokens)
        for row in rows:
            row["lock_status"] = recognize_selection_lock(image, row["y"])
            target = getattr(self, '_selection_identity_target', None)
            if target and row.get('sword_catalog_id') == target['sword_catalog_id']:
                y = row['y']
                if not 170 < y < 650:
                    continue  # 裁掉的上下边缘行没有完整数值证据。
                tokens = self.maa.ocr_all(roi_4to4(470, y - 48, 591, y - 27)) or []
                text = ''.join(t for t, _ in tokens)
                levels = re.findall(r'乱舞\s*(\d+)\s*级', text)
                row['tou_level'] = int(levels[0]) if len(levels) == 1 else None
                for field, x in (('survival_max', 596), ('recon', 873)):
                    if target.get(field) is None:
                        continue
                    tokens = self.maa.ocr_all(roi_4to4(x + 3, y - 21, x + 52, y + 13)) or []
                    numbers = [int(t.strip()) for t, _ in tokens
                               if re.fullmatch(r'\d{1,3}', t.strip())]
                    row[field] = numbers[0] if len(numbers) == 1 else None
        return rows, unreadable

    def _find_client_target_stream(self, target, match_fields, max_pages):
        """Find a roster-proven unique instance; click on the page where found."""
        yield '[编队] 按客户端身份找人，找到后直接选入'
        seen = set()
        for index in range(max_pages):
            rows, _ = self._read_list_page()
            row = self._match_target_row(rows, target, target, match_fields)
            if row is not None:
                return row, index
            fingerprint = page_fingerprint(rows)
            bottom = self._scrollbar_bottom()
            if not rows or fingerprint in seen or bottom is None or bottom >= _SCROLLBAR_BOTTOM_Y:
                break
            seen.add(fingerprint)
            self.maa.swipe(*_SWIPE_NEXT)
            time.sleep(1.2)
        return None, index

    def _scan_selection_list(self, max_pages, *, swipe_next=_SWIPE_NEXT,
                             swipe_prev=_SWIPE_PREV):
        """逐页 OCR 全表。Returns (pages, fps, current_idx, unreadable, status)。

        status 四态分明——「滑不动」和「确认到底」是两件事：
          complete  —— 停滞后通过多阶段到底核验（主仪器是右缘滑块底缘
                      的绝对位置：候选页没贴底说明停滞是前滑被吞，续滑
                      把新页找回来交还本循环；候选页贴底则逐阶段正面
                      验证反滑回落+恢复贴底，贴底帧指纹与候选页一致
                      才计一个有效阶段，连续 _BOTTOM_PROOF_STAGES 个
                      阶段全过才允许称底。滑动不吸附整页，欠程恢复帧
                      不比指纹。OCR 行数不足不是独立到底证据，绝不使用）；
          stalled   —— 连续滑动无响应且拿不出到底证据（滑动可能被吞、
                      滑块读数缺失、或单页名单无从回翻验证，同样保守
                      stalled——这是 honest stop）；
          blind     —— 任何一页 OCR 一行都读不出（整页失明，识别失败）；
          truncated —— 触达 max_pages 安全阀仍未到底；
          loop      —— 指纹绕回已见过的页（页序异常，不等于到底）。
        只有 complete 允许裁决唯一/确定 not_found，其余一律拒绝下点。
        """
        pages, fps, bars, unreadable = [], [], [], 0
        current_idx, stalls, status = 0, 0, "complete"
        pending = None          # 到底核验探出的新页，交还循环当当前页处理
        while True:
            if pending is not None:
                rows, bad = pending
                pending = None
            else:
                rows, bad = self._read_list_page()
            if fps and not rows:
                status = "blind"       # 翻页后整页失明
                break
            fp = page_fingerprint(rows)
            bar = self._scrollbar_bottom()
            # 同一滑块位置不算新页：真机 OCR 偶发把同一帧的等级/名字
            # 读出不同指纹；位置没变时继续走到底核验，而不是伪造页序。
            same_position = (fps and bar is not None
                             and bars[-1] is not None and bar == bars[-1])
            if fps and (fp == fps[-1] or same_position):
                stalls += 1
                if stalls >= _STALL_LIMIT:
                    outcome, recovered = self._verify_bottom(
                        fps, swipe_next=swipe_next, swipe_prev=swipe_prev)
                    if outcome != "advanced":
                        status = outcome           # complete / stalled
                        break
                    pending = recovered            # 候选页之后还有页：继续扫
                    continue
                self.maa.swipe(*swipe_next)
                time.sleep(1.2)
                continue
            if fp in fps:
                status = "loop"
                break
            if not rows:
                status = "blind"       # 首页就一行都读不出
                break
            pages.append(rows)
            fps.append(fp)
            # 每页记下右缘滑块底缘（绝对位置），给重定位当导航地标；
            # 读不出就存 None，目标页没地标时重定位如实失败
            bars.append(bar)
            unreadable += bad
            current_idx = len(pages) - 1
            stalls = 0
            if len(pages) >= max_pages:
                status = "truncated"
                break
            self.maa.swipe(*swipe_next)
            time.sleep(1.2)
        yield (f"[编队] 列表扫描 {len(pages)} 页"
               f"（{'已到底' if status == 'complete' else '未到底：' + status}，"
               f"读不清 {unreadable} 行）")
        return pages, fps, bars, current_idx, unreadable, status

    def _verify_bottom(self, fps, *, swipe_next=_SWIPE_NEXT,
                       swipe_prev=_SWIPE_PREV):
        """停滞后的「到底」多阶段核验，主仪器是右缘滑块底缘的绝对位置
        （免疫滑动欠程/被吞，2026-09-22 探针标定：贴底恒 689；一次反滑
        689→482；单次恢复滑欠程只回 588，再滑一次才重新钳到 689——
        欠程帧内容位移、指纹失真，故只在贴底帧比对指纹）。
        Returns (outcome, recovered)：
          ("stalled",  None)       证据不足（只有一页无从回翻，或滑块
                                   读数缺失，或反滑/恢复/探测任一环
                                   失效，或贴底帧指纹与候选页矛盾）
                                   ——绝不称底；
          ("complete", None)       连续 _BOTTOM_PROOF_STAGES 个阶段，每阶段
                                   候选页贴底、反滑回落生效、恢复后重新
                                   贴底且指纹与候选页一致；
          ("advanced", (rows,bad)) 候选页滑块没贴底（停滞是前滑被吞），
                                   续滑探出新页——交还扫描循环继续
                                   （由主循环判 loop/blind）。

        纪律：滑块底缘是绝对位置证据，与滑动是否被执行无关——没贴底
        就一定不在底，探测滑被吞也骗不出 689；贴底帧指纹与候选页一致
        则排除「滑块误读」。任何一环证据不足都只报 stalled，绝不
        not_found。
        """
        if len(fps) < 2:
            # 单页名单（筛选后常态）：回翻无从谈起，只能靠独立末端证据。
            # 标定未完成前保守 stalled（honest stop）。
            if self._list_single_page_sighted():
                return "complete", None
            return "stalled", None      # 单页无从回翻：honest stop
        candidate_fp = fps[-1]
        bottom = _SCROLLBAR_BOTTOM_Y - _SCROLLBAR_BOTTOM_TOL
        for _stage in range(_BOTTOM_PROOF_STAGES):
            self.maa.screenshot(force=True)
            b0 = self._scrollbar_bottom()
            if b0 is None:
                return "stalled", None          # 滑块读数缺失：证据不足
            if b0 < bottom:
                # 滑块没贴底：候选页不是底，停滞是前滑被吞——续滑把
                # 新页找回来交还主循环（找不回/失明就 stalled）。
                for _retry in range(2):
                    self.maa.swipe(*swipe_next)
                    time.sleep(1.2)
                    rows, bad = self._read_list_page()
                    if rows and page_fingerprint(rows) != candidate_fp:
                        return "advanced", (rows, bad)
                return "stalled", None
            # 候选页自称贴底：先正面验证反向滑动有效……
            self.maa.swipe(*swipe_prev)
            time.sleep(1.2)
            rows, _bad = self._read_list_page()
            b1 = self._scrollbar_bottom()
            if b1 is None or b0 - b1 < _SCROLLBAR_REVERSE_MIN_DY:
                return "stalled", None          # 反滑无效：机制不可信
            if rows and page_fingerprint(rows) == candidate_fp:
                return "stalled", None          # 反滑后内容没变：滑动被吞
            # ……再续滑恢复贴底。滑动不吸附整页，单次恢复可能欠程
            # （探针实测恢复后底缘 588，再滑一次才回 689）；欠程帧
            # 内容位移、指纹失真，不比指纹直接再滑，只有重新贴底的
            # 帧才与候选页比对。
            restored = False
            for _retry in range(3):
                self.maa.swipe(*swipe_next)
                time.sleep(1.2)
                rows, bad = self._read_list_page()
                b2 = self._scrollbar_bottom()
                if b2 is None:
                    return "stalled", None
                if b2 < bottom:
                    continue                    # 欠程过渡帧：再滑一次
                if not rows:
                    return "stalled", None      # 贴底帧失明：证据不足
                if page_fingerprint(rows) != candidate_fp:
                    return "stalled", None      # 贴底但内容变了：证据矛盾
                restored = True
                break
            if not restored:
                return "stalled", None
        return "complete", None

    def _scrollbar_bottom(self):
        """读选择列表右缘滑块的底缘 y。Returns int 或 None（读帧失败、
        看不到滑轨、找不到滑块——一律当证据不足，调用方只能 stalled）。

        校准来源（2026-09-14 运行帧通道逐页取样 30 页 + 2026-09-22
        filter_bottom_probe 复测）：滑轨体 x[1262,1270]、轨道
        y[124,689]；滑块亮 ~243 / 轨道灰 ~113；滑块高约 103px。
        到底时滑块被轨道物理钳住，底缘读数恒定 689；一次反滑
        689→482；恢复滑欠程可只回 588（不吸附整页）。测试经子类
        注入剧本读数。
        """
        img = self.maa.screenshot()     # 复用核验刚读过的那一帧，不再截
        if img is None or img.shape[0] < 690 or img.shape[1] < 1270:
            return None
        x0, x1 = _SCROLLBAR_BAND_X
        y0, y1 = _SCROLLBAR_TRACK_Y
        band = np.asarray(img[y0:y1, x0:x1], dtype=np.int32).mean(axis=(1, 2))
        hot = band > _SCROLLBAR_THUMB_BRIGHT
        if (band < _SCROLLBAR_TRACK_DARK).sum() < 100:
            return None                 # 看不到灰色滑轨：不在选择列表上
        best_len = best_end = cur = 0
        for i, h in enumerate(hot):
            cur = cur + 1 if h else 0
            if cur > best_len:
                best_len, best_end = cur, i
        if best_len < 20:               # 滑块实测高 ~103px，太短当噪声
            return None
        return y0 + best_end

    def _list_end_sighted(self):
        """独立末端视觉证据：滑块底缘贴上滑轨底部。回答「当前位置是不是
        列表末尾」——与滑动是否被执行无关的绝对位置证据，探测滑被吞
        也不影响读数。读不出滑块时保守 False（证据不足）。

        零容差（2026-09-14 校准）：离底一页的过渡帧底缘 685，滑块每
        4px≈内容 62px，1~2px 的「差不多贴底」就够藏住一行姓名——
        只有明确读到 689 才算到底，687/688 一律不放行。
        """
        b = self._scrollbar_bottom()
        return b is not None and b >= _SCROLLBAR_BOTTOM_Y - _SCROLLBAR_BOTTOM_TOL

    def _list_single_page_sighted(self):
        """单页名单（筛选后常态）的到底证据通道。

        真机 2026-09-24「剑＋初」短名单：前三行从 y≈195 排到
        y≈394，下方是连续灰色空白。扫描已先确认连续前滑
        不再改变行指纹，此处只接受顶部可见行加下方大面积同色空白；
        滑轨仍显示短滑块，不能把它当到底证据。测试经子类注入。
        """
        rows, unreadable = self._read_list_page()
        if unreadable or not 1 <= len(rows) <= 4:
            return False
        if not (180 <= min(r["y"] for r in rows) <= 220
                and max(r["y"] for r in rows) <= 510):
            return False
        image = self.maa.screenshot(force=True)
        if image is None or getattr(image, "shape", None) != (720, 1280, 3):
            return False
        start = max(r["y"] for r in rows) + 65
        band = image[start:680, 100:1200].astype("int16")
        if band.shape[0] < 80:
            return False
        median = np.median(band.reshape(-1, 3), axis=0)
        return bool((np.abs(band - median).max(axis=2) < 8).mean() >= 0.99)

    def _goto_page(self, bars, target_idx, target, scanned_row,
                   match_fields, *, swipe_next=_SWIPE_NEXT,
                   swipe_prev=_SWIPE_PREV):
        """把目标行翻回视野，返回落地帧实读的目标行（点决定的坐标
        以它为准）；证据不足/找不到就返回 None，绝不乱点。

        列表是连续滚动、不吸附整页（2026-09-22 真机：回翻落点停在
        两页之间，任何记录页的指纹都复现不了），故不以扫描页指纹为
        落点。方向由滑块底缘与扫描记录的页底缘对比给出（绝对位置，
        滑动被吞也骗不了）；每一帧直接拿目标行证据裁决当落地条件，
        首次命中后再强制重读一帧复核——两帧证据都过才算落地。
        单页名单（筛选后常态）根本不用导航：首帧就该命中。"""
        b_target = bars[target_idx] if target_idx < len(bars) else None
        for _attempt in range(_GOTO_MAX_SWIPES):
            rows, _bad = self._read_list_page()
            hit = self._match_target_row(rows, target, scanned_row,
                                         match_fields)
            if hit is not None:
                # 落地复核：强制重读一帧，证据要连续两帧都成立
                rows2, _bad2 = self._read_list_page()
                hit2 = self._match_target_row(rows2, target, scanned_row,
                                              match_fields)
                if hit2 is None:
                    yield "[编队] 目标行读数不稳定（复核帧证据不成立），停"
                    return None
                return hit2
            b = self._scrollbar_bottom()
            if b is None or b_target is None:
                yield "[编队] 重定位途中没有滑块地标读数，无法导航，停"
                return None
            if b == b_target:
                # 地标到了但行不在视野/证据不过：多为 OCR 偶发漏行，
                # 原地重读一次还不行就如实失败
                rows3, _bad3 = self._read_list_page()
                hit3 = self._match_target_row(rows3, target, scanned_row,
                                              match_fields)
                if hit3 is not None:
                    return hit3
                yield "[编队] 已到目标页位置但目标行读不出来，停"
                return None
            self.maa.swipe(*(swipe_next if b < b_target else swipe_prev))
            time.sleep(1.2)
        yield f"[编队] 重定位翻满 {_GOTO_MAX_SWIPES} 次仍未找到目标行，停"
        return None

    def _match_target_row(self, rows, target, scanned_row, match_fields):
        """在一帧已解析的行里找目标：与扫描裁决同一套「证据充分」
        条件——零冲突且无缺证据的唯一行（落地阶段不比扫描阶段宽）。"""
        hits = [r for r in rows
                if r.get("sword_catalog_id") == target["sword_catalog_id"]
                and (target.get("selection_policy") != "locked_highest_level"
                     or r.get("lock_status") == "locked")
                and not row_conflicts_target(r, target, match_fields)
                and not row_evidence_gaps(r, target, match_fields)]
        if len(hits) != 1:
            return None
        # 与扫描时的行特征一致才认（y 允许漂移——连续滚动落点不吸附，
        # 行坐标以落地帧实读为准，由调用方拿去点决定）
        if scanned_row.get("level") is not None \
                and hits[0].get("level") != scanned_row["level"]:
            return None
        return hits[0]

    def _relocate_row(self, target, scanned_row, match_fields):
        """重读当前帧找目标行（_match_target_row 的读帧包装）。"""
        rows, _bad = self._read_list_page()
        return self._match_target_row(rows, target, scanned_row,
                                      match_fields)

    # ---- 结果组装 ----

    def _finish(self, result, team_no, slot_no, tgt, reason, **extra):
        out = {"schema_version": RESULT_SCHEMA_VERSION,
               "result": result, "team_no": team_no, "slot_no": slot_no,
               "target": tgt if isinstance(tgt, dict) else {"raw": tgt},
               "reason": reason}
        out.update(extra)
        if hasattr(self, "record_event"):
            payload = {"team_no": team_no, "slot_no": slot_no,
                       "result": result, "reason": reason,
                       "entry_shell": extra.get("entry_shell"),
                       "target": out["target"],
                       "before": extra.get("before"),
                       "candidates": extra.get("candidates")}
            try:
                self.record_event("formation.member_selected", **payload)
            except Exception:
                pass  # 记账失败不阻塞执行结果
        return out
