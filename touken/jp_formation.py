# -*- coding: utf-8 -*-
"""日服编成画面流程（jp_formation）：出阵前的队伍整备——换队长 + 补刀装。

给 jp_sortie（日服合战场）出阵前调用：经 PartyWatch 的被动队伍数据决策
（不注入脚本、不改请求内容、不向服务器多发请求），需要动队伍时才去编成页
操作，完事回到出阵部队屏交还宿主。导航/认屏/点击全部复用 Sally 底座
（jp_sally），不手搓零件。

对齐国服语义（flows/sakura.py 的 _rotate_captain_here、flows/battle.py 的
_refill_equipment_from_warning_stream、team_state.captain_rotation_slot）：

- 换队长：全队气力数据齐全且新鲜时才决策（不猜不盲点）；最低气力成员
  比队长（1 号位）低过 margin 才换位；换位后经新的队伍数据验收
  （成员编号核对），验不过就安全停车。
- 补刀装：按快照 equipment + 刀种容量（sword_db.slots，短刀 1 槽、太刀 3
  槽，不能拿三元组当真三槽）找真空槽，去编成页逐空槽补；只用库存现成
  刀装，绝不碰刀装作成（「刀装おまかせ」「一括装備」经真机实测是**全槽
  替换**语义，会毁现有刀装，本模块绝不用）；补完经新数据复核满槽。
- 数据缺失/不新鲜：本轮跳过（「本轮不换/不补」），交还宿主继续出阵。

【真机标定状态】（2026-11-22 本机专用 Chrome 实机标定 + 干跑验证）：

- 进编成：本丸右侧「結成」按钮（home/formation 模板），点击落点基准
  (1405,600)；出阵各屏经目録中转（HOME_ROUTE 语义）。
- 部队页签：中心相对 formation/label 锚点 (97,59)+（n-1)*(125,0)；
  真机账号部队四/五未解锁（party status=0），页签死位，四/五偏移按
  规律外推未实测（点不动会被 _ensure_team_tab 拦住如实报）。
- 换位语义：拖拽（合成鼠标事件带不动 HTML5 DnD）与队内直接交换都不存
  在；官方路径是每行「入替」→ 部隊員入替选刀列表（只列不在本部队的刀，
  被替换槽位的 current 刀置灰）。队内两员互换 = 用**空格部队当草稿**的
  三步舞（B→草稿槽1, B→a槽, A→b槽），只动本队与草稿队（净复原），
  真机已验证（小夜↔一期互换成功且数据复核一致）。
- 选刀列表：滚轮（CDP mouseWheel）逐屏 OCR 找刀名（jp_ocr），決定列
  x≈1363、刀名 y−31；点決定后列表自动关闭回本队列表，无确认弹窗。
- 补刀装：点行「装備」进成员装备面板（equip/panel 模板）；逐空槽点槽
  → 库存选择面板（equip/inventory 模板，**本账号满装未实测**，素材缺失时
  在点选库存前如实停车）。
- 数据回流：编成页编辑**不会**自己重报 /sally；返回出阵部队屏游戏重报
  （验收链路靠这个，已实测）。

安全底线（同 jp_sally / jp_sortie）：

- 只点认得出的画面；认不出就 SallyStop（_save_debug 存证），绝不盲点。
- 编成途中任何确认弹窗一律停——上游 _navigate 见弹窗即停。
- 进编成前确认队伍数据新鲜（沿用 jp_sally.PARTY_DATA_MAX_AGE 语义）。
- 回城途中可能撞见近侍全屏问候（无 UI）：过渡点统一走 _settle_unknown
  点中央跳过，仍认不出就安全停车。

集成契约（供 jp_sortie）：

    for line in jp_formation.rotate_captain_stream(self, watch, team_no, margin):
        self.log(line)
    for line in jp_formation.auto_equip_stream(self, watch, team_no):
        self.log(line)

- nav 传 JpSortie 自身（Sally 底座 + record_event 账房）。
- 无需动作时（差不到 margin / 队长就是最低 / 刀装齐整 / 数据不新鲜）
  原样返回，画面停在哪算哪，宿主接着走。
- 成功收场时画面停在出阵部队屏（team），且已拿到标定后新的队伍数据——
  宿主可继续 _select_team → 委托自动行军 → 出阵；建议调用后照旧
  _pick_map(chapter, map_no) 一次确认选图未被编成页行程带偏。
- 任一步认不出画面/弹窗/素材未标定/验收不过 → SallyStop 向上抛，
  宿主按既有安全停止路径收尾。
"""
from __future__ import annotations

import time

from . import jp_ocr, jp_party, jp_sally, jp_vision, sword_db
from .device_adapter import Point
from .team_state import captain_rotation_slot

# 进编成导航：从出阵各屏/本丸/目録任意一层接续（_navigate 认屏驱动）。
# 真机实测路径：本丸直点「結成」；出阵各屏先经目録按钮（home/catalog）
# 回目録大页，点「本丸」瓦回本丸，再点「結成」。
FORMATION_ROUTE = {
    'home': ('hit', 'home/formation', '本丸结成入口'),
    'directory': ('hit', 'directory/home', '目录本丸瓦'),
    'team': ('hit', 'home/catalog', '部队屏目录按钮'),
    'era': ('hit', 'home/catalog', '时代屏目录按钮'),
    'map': ('hit', 'home/catalog', '选图屏目录按钮'),
}
# 回程复用 Sally 出阵路线表（编成页返回箭头本就在其中），到部队屏收工。

# 体检要验的模板（缺了逐条列出再停，绝不静默、绝不给假值）。
# home/catalog、directory/home、formation/label 是 Sally/既有已标定的，
# 不在列。命名规范：formation/* 编成页画面元素，equip/* 刀装栏交互。
REQUIRED_TEMPLATES = (
    'home/formation',           # 本丸 → 编成入口（右侧「結成」按钮）
    'formation/picker-title',   # 部隊員入替选刀列表标题（打开/关闭判定）
    'equip/panel',              # 成员装备面板标题（装備变更）
)
# 仅当存在真空槽才需要的素材（满装账号无需补装，不拦体检）。
_EQUIP_FILL_TEMPLATES = (
    'equip/inventory',          # 点空槽后弹出的库存刀装选择面板（真机未实测）
)

# ==================== 真机标定常量（2026-11-22，基准空间像素） ====================
# 均相对 formation/label 锚点中心（点击目标一律锚点 + 偏移，窗口高度
# 变化只是视口裁剪，锚点命中偏移就对——同 Sally 的老规矩）。
# 部队页签中心（选中变白/灰，未选橙色）。1-3 已实测；4/5 本账号未解锁
# （party status=0，页签点击无反应），按间距外推、未实测。
FORMATION_TEAM_TAB_OFFSETS = {
    1: (97, 59), 2: (222, 59), 3: (347, 59), 4: (472, 59), 5: (597, 59),
}
# 成员行「入替」「装備」按钮中心：行距 95，行 1 中心 y=+120。
FORMATION_ROW_DY = 95
FORMATION_ROW_Y0 = 120
IREKAE_DX = 1099          # 入替（队内换人 → 选刀列表）
EQUIP_DX = 1007           # 装備（成员装备面板）
# 部隊員入替选刀列表：決定列绝对 x；刀名 token 中心 y − 31 = 同行決定中心 y。
PICKER_DECIDE_X = 1363
PICKER_NAME_DECIDE_DY = 31
PICKER_LIST_ROI = (100, 220, 340, 880)     # 刀名列 + 決定列 OCR 区域
PICKER_WHEEL_AT = (700, 500)               # 滚轮悬停点（列表区）
PICKER_WHEEL_DY = 1200                     # 每滚一下的 deltaY（约一屏）
PICKER_SCROLL_SETTLE = 0.8
PICKER_MAX_SCREENS = 30                    # 搜刀最多滚屏数（≈180 行容量）
# 成员装备面板：兵槽行中心（行 1/2/3），点槽补库存。
EQUIP_SLOT_X = 700
EQUIP_SLOT_Y0 = 255
EQUIP_SLOT_DY = 98


def missing_templates() -> list[str]:
    """素材体检：缺的逐条列出（照 jp_sortie._preflight 同款语义）。"""
    missing = []
    for name in REQUIRED_TEMPLATES:
        try:
            jp_vision.load_template(name)
        except jp_vision.TemplateNotFound as exc:
            missing.append(str(exc))
    return missing


# ---- 队伍数据（复用 Sally 底座语义，不手搓） ----

def _fresh_snapshot(nav, watch, team_no: int):
    """等一份新鲜队伍数据并转成目标部队快照；拿不到返回 (None, None)。

    复用 Sally._await_party_data（沿用 PARTY_DATA_MAX_AGE 语义）。
    返回的 baseline 是原始 (捕获时间, 载荷)，供验收链路比对「有没有更新的」。"""
    latest = nav._await_party_data(watch)
    if latest is None:
        return None, None
    captured_at, payload = latest
    return jp_party.build_team_snapshot(payload, team_no, now=captured_at), \
        latest


def _await_newer_party_data(nav, watch, baseline):
    """等一份比 baseline 更新的队伍数据（标定后验收用）。

    「新」的判定：捕获时间更晚，或载荷已不是 baseline 那一份——游戏重报
    /sally 就算内容一字不差，也是编辑之后新的服务器读数，验的就是它。
    真机实测：编成页编辑不触发重报，返回出阵部队屏才重报；这里只收不催，
    宽限内没等到返回 None，由调用方安全停车。"""
    base_at, base_payload = baseline
    deadline = time.monotonic() + jp_sally.PARTY_DATA_WAIT
    while True:
        latest = watch.latest()
        if latest and (latest[0] > base_at or latest[1] is not base_payload) \
                and 0 <= time.time() - latest[0] <= jp_sally.PARTY_DATA_MAX_AGE:
            return latest
        if time.monotonic() >= deadline:
            return None
        time.sleep(nav.poll)


def _check_calibration(nav) -> None:
    """要动队伍前先过素材体检；缺的逐条报出来再停。"""
    missing = missing_templates()
    if missing:
        nav.log('日服画面素材没标定，先干不了活。缺这些模板：')
        for item in missing:
            nav.log(f'  ✗ {item}')
        raise jp_sally.SallyStop('模板没标定全，已停止')


def _name(member) -> str:
    return member.name or f'#{member.serial_id}'


def _label_hit(nav, image):
    """找编成页結成标签锚点；不在编成页返回 None。"""
    try:
        hit = jp_vision.find(image, 'formation/label')
    except jp_vision.TemplateNotFound:
        return None
    return hit


def _settle_unknown(nav) -> str:
    """过渡后认不出画面时：可能是近侍全屏问候（无 UI），点中央跳过。

    回 'unknown' 以外的画面名，或超时仍 unknown（调用方照常安全停车）。"""
    for _ in range(2):
        image = nav.device.screenshot(force=True)
        screen = nav.classify(image)
        if screen != 'unknown':
            return screen
        w, h = image.size
        nav.device.click(Point(int(w * 0.5), int(h * 0.5)))
        time.sleep(2.5)
    return nav.classify(nav.device.screenshot(force=True))


def _navigate_resilient(nav, route, targets, what):
    """_navigate + 近侍全屏问候兜底：撞 unknown 先点中央跳过再重走一次。

    真机实测编成页返回箭头后常插一段无 UI 的近侍问候（_navigate 会当
    陌生画面停车），跳过后续走一次即可；第二次仍不认识才如实停车。"""
    for attempt in (1, 2):
        try:
            return nav._navigate(route, targets, what)
        except jp_sally.SallyStop as exc:
            if '不认识的画面' not in str(exc) or attempt == 2:
                raise
            nav.log('撞见过渡画面（近侍问候？），点中央跳过后重走…')
            _settle_unknown(nav)
    raise jp_sally.SallyStop(f'{what}反复走不通，已停止；请人工查看游戏')


def _goto_formation(nav) -> None:
    """走到编成页；撞见问候点中央跳过，仍认不出由导航兜底停车。"""
    _navigate_resilient(nav, FORMATION_ROUTE, frozenset({'formation'}),
                        '前往编成')
    _settle_unknown(nav)


def _leave_to_team(nav) -> None:
    """回出阵部队屏（顺路逼游戏重报 /sally 供验收）。"""
    _navigate_resilient(nav, jp_sally.SALLY_ROUTE, frozenset({'team'}),
                        '返回出阵')
    _settle_unknown(nav)


def _ensure_team_tab(nav, team_no: int) -> None:
    """确认编成页停在目标部队页签；没选中就点，点不动如实停车。

    选中页签变白/灰（蓝道高），未选是橙色——用锚点偏移算页签中心采样
    亮度区分。部队未解锁（如本账号四/五 status=0）页签点不动，这里拦下。"""
    image = nav.device.screenshot(force=True)
    hit = _label_hit(nav, image)
    if hit is None:
        nav._save_debug(image)
        raise jp_sally.SallyStop('编成页没认出来，已停止（截图已存 debug）')
    w, h = image.size
    ax, ay = hit.nx * w, hit.ny * h
    dx, dy = FORMATION_TEAM_TAB_OFFSETS[team_no]
    tx, ty = int(ax + dx), int(ay + dy)
    if _tab_selected(nav.device.screenshot(force=True), tx, ty):
        return
    nav.device.click(Point(tx, ty))
    time.sleep(2.0)
    if not _tab_selected(nav.device.screenshot(force=True), tx, ty):
        nav._save_debug(nav.device.screenshot(force=True))
        raise jp_sally.SallyStop(
            f'部队{"一二三四五"[team_no - 1]}页签点不动（未解锁？），'
            '已停止；请人工查看游戏（截图已存 debug）')


def _tab_selected(image, tx: int, ty: int) -> bool:
    """页签中心采样：选中（白/灰）蓝道高，未选橙色（B 通道分差最大）。
    真机实测：选中 ≈(180,178,172)，未选 ≈(189,146,92)，阈值取 130。"""
    px = image.crop((max(0, tx - 6), max(0, ty - 6), tx + 6, ty + 6))
    r, g, b = px.resize((1, 1)).getpixel((0, 0))
    return b > 130


# ---- 部隊員入替选刀列表 ----

def _open_picker(nav, slot: int) -> None:
    """点指定行的「入替」，确认选刀列表打开。"""
    image = nav.device.screenshot(force=True)
    hit = _label_hit(nav, image)
    if hit is None:
        nav._save_debug(image)
        raise jp_sally.SallyStop('入替时编成页没认出来，已停止（截图已存 debug）')
    w, h = image.size
    nav.device.click(Point(
        int(hit.nx * w + IREKAE_DX),
        int(hit.ny * h + FORMATION_ROW_Y0 + (slot - 1) * FORMATION_ROW_DY)))
    time.sleep(1.8)
    image = nav.device.screenshot(force=True)
    if jp_vision.find(image, 'formation/picker-title') is None:
        nav._save_debug(image)
        raise jp_sally.SallyStop(
            '选刀列表没打开（行是空的？画面不对？），已停止（截图已存 debug）')


def _picker_visible(nav) -> bool:
    image = nav.device.screenshot(force=True)
    return jp_vision.find(image, 'formation/picker-title') is not None


def _wheel(browser, dy: int) -> None:
    """选刀列表滚轮（JpBrowser 无现成方法，真机实测列表只吃滚轮；
    走 browser 私有通道发 CDP mouseWheel，只发不收游戏数据）。"""
    view = browser.check_view()
    scale = view['clientWidth'] / 1508.0
    browser._send('Input.dispatchMouseEvent',
                  {'x': PICKER_WHEEL_AT[0] * scale, 'y': PICKER_WHEEL_AT[1] * scale,
                   'type': 'mouseWheel', 'deltaX': 0, 'deltaY': dy})


def _picker_pick(nav, name: str) -> None:
    """在选刀列表里滚屏 OCR 找刀名并点同行決定；找不到/点完列表没关都停。"""
    browser = nav.device._browser
    for _ in range(PICKER_MAX_SCREENS):
        image = nav.device.screenshot(force=True)
        crop = image.crop(PICKER_LIST_ROI)
        found_y = None
        for text, _s, cx, cy in jp_ocr.read_tokens(crop):
            if str(text).strip() == name and cx + PICKER_LIST_ROI[0] < 400:
                found_y = cy + PICKER_LIST_ROI[1]
                break
        if found_y is not None:
            nav.device.click(Point(
                PICKER_DECIDE_X, int(found_y - PICKER_NAME_DECIDE_DY)))
            # 选中后列表带关闭动画，轮询等它关（真机实测有时 >2.2s）；
            # 等不到就是另有弹窗或点空，如实停车
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
                if not _picker_visible(nav):
                    return
            nav._save_debug(nav.device.screenshot(force=True))
            raise jp_sally.SallyStop(
                f'选了{name}但选刀列表没关上（出现弹窗？），已停止；'
                '请人工查看游戏（截图已存 debug）')
        _wheel(browser, PICKER_WHEEL_DY)
        time.sleep(PICKER_SCROLL_SETTLE)
    nav._save_debug(nav.device.screenshot(force=True))
    raise jp_sally.SallyStop(
        f'选刀列表里翻到底也没找到{name}，已停止；请人工查看游戏（截图已存 debug）')


# ---- 编成页内交互（真机标定实现） ----

def _scratch_team(payload: dict, own_team: int):
    """挑一支空格部队当换位草稿：status=1（已解锁待命）且编制全空。

    只用数据判（PartyWatch 被动数据），不猜画面；找不到返回 None。"""
    party = payload.get('party')
    if not isinstance(party, dict):
        return None
    for key in sorted(party, key=lambda k: int(k) if str(k).isdigit() else 99):
        if int(key) == own_team:
            continue
        entry = party.get(key)
        if not isinstance(entry, dict) or entry.get('status') != 1:
            continue
        slots = entry.get('slot')
        if isinstance(slots, dict) and not any(
                isinstance(s, dict) and s.get('serial_id') for s in slots.values()):
            return int(key)
    return None


def _swap_members(nav, a_slot: int, b_slot: int, snapshot, watch) -> None:
    """把 a_slot 与 b_slot 两名成员换位（真机语义：入替选刀三步舞）。

    队内两员不能直接互换（选刀列表只列不在本部队的刀），用空格部队当
    草稿：B→草稿1、B→a槽（A 已被挤出到刀帐）、A→b槽。只动本队与草稿队，
    草稿队净复原；真机验证过小夜↔一期互换 + 数据复核一致。
    """
    latest = nav._await_party_data(watch)
    payload = latest[1] if latest else None
    scratch = _scratch_team(payload, snapshot.team_no) if payload else None
    if scratch is None:
        raise jp_sally.SallyStop(
            '换位需要一支空格部队当草稿（本队之外的待命空队），没找到；'
            '请人工换位或调整编制后重试')
    member_a = next(m for m in snapshot.members if m.slot == a_slot)
    member_b = next(m for m in snapshot.members if m.slot == b_slot)
    name_a, name_b = _name(member_a), _name(member_b)
    nav.log(f'[编成] 换位草稿：部队{"一二三四五"[scratch - 1]}')

    _ensure_team_tab(nav, scratch)
    _open_picker(nav, 1)
    _picker_pick(nav, name_b)
    nav.log(f'[编成] {name_b} 暂存草稿队')

    _ensure_team_tab(nav, snapshot.team_no)
    _open_picker(nav, a_slot)
    _picker_pick(nav, name_b)
    _ensure_team_tab(nav, snapshot.team_no)
    _open_picker(nav, b_slot)
    _picker_pick(nav, name_a)
    nav.log(f'[编成] {name_b} 与 {name_a} 已换位')


def _equip_capacity(member) -> int | None:
    """刀种刀装槽容量（sword_db.slots；短刀 1、太刀 3…）；查不到 None。

    快照三元组不是「三槽都在用」：槽数随刀种变，缺口判定必须按容量来。"""
    if not member.name:
        return None
    found = sword_db.find_by_name(member.name)
    if not found:
        return None
    slots = found[1].get('slots')
    return slots if type(slots) is int and slots > 0 else None


def _equip_gaps(member) -> list[int]:
    """真空槽的 1-based 槽位列表（超出容量的槽位不算缺口）。"""
    capacity = _equip_capacity(member)
    if capacity is None or member.equipment is None:
        return []
    return [i for i in range(1, capacity + 1)
            if i > len(member.equipment) or not member.equipment[i - 1]]


def _open_equip_panel(nav, slot: int) -> None:
    """点指定行「装備」，确认成员装备面板打开。"""
    image = nav.device.screenshot(force=True)
    hit = _label_hit(nav, image)
    if hit is None:
        nav._save_debug(image)
        raise jp_sally.SallyStop('补刀装时编成页没认出来，已停止（截图已存 debug）')
    w, h = image.size
    nav.device.click(Point(
        int(hit.nx * w + EQUIP_DX),
        int(hit.ny * h + FORMATION_ROW_Y0 + (slot - 1) * FORMATION_ROW_DY)))
    time.sleep(2.0)
    image = nav.device.screenshot(force=True)
    if jp_vision.find(image, 'equip/panel') is None:
        nav._save_debug(image)
        raise jp_sally.SallyStop(
            '成员装备面板没打开，已停止（截图已存 debug）')


def _fill_member_equipment(nav, member) -> None:
    """逐真空槽补库存刀装（绝不碰刀装作成/おまかせ/一括）。

    装备面板的「刀装おまかせ」「一括装備」真机实测是**全槽替换**（会毁掉
    现有刀装），本函数只点真空槽 → 库存选择面板点选。空槽点击序列按
    面板布局标定；库存面板（equip/inventory）在本账号（满装）未实测，
    素材缺失时在点选库存前如实停车。
    """
    gaps = _equip_gaps(member)
    if not gaps:
        return
    _open_equip_panel(nav, member.slot)
    for slot_no in gaps:
        nav.device.click(Point(
            EQUIP_SLOT_X, EQUIP_SLOT_Y0 + (slot_no - 1) * EQUIP_SLOT_DY))
        time.sleep(1.5)
        image = nav.device.screenshot(force=True)
        try:
            found = jp_vision.find(image, 'equip/inventory') is not None
        except jp_vision.TemplateNotFound:
            found = False
        if not found:
            nav._save_debug(image)
            raise jp_sally.SallyStop(
                '点空槽后库存刀装面板没认出来（素材未标定？），已停止；'
                '请人工查看游戏（截图已存 debug）')
        # 库存面板点选序列真机未实测（本账号满装无缺口），标定后补全：
        # 从库存列表点一件适合刀种的现成刀装（equip/inventory-item），
        # 出现任何确认弹窗一律停。
        raise jp_sally.SallyStop('库存刀装点选序列尚未标定（本账号满装未实测），已停止')
    # 补完关面板回队列表
    image = nav.device.screenshot(force=True)
    hit = _label_hit(nav, image)
    if hit is not None:
        w, h = image.size
        nav.device.click(Point(
            int(hit.nx * w + jp_sally.BACK_ARROW_OFFSET[0]),
            int(hit.ny * h + jp_sally.BACK_ARROW_OFFSET[1])))
        time.sleep(1.5)


# ==================== 公开流程 ====================

def rotate_captain_stream(nav, watch, team_no: int, margin: int):
    """出阵前换队长：最低气力成员比队长（1 号位）低过 margin 才换位。

    决策只吃 PartyWatch 的新鲜队伍数据（全队气力齐全才动，不猜）；
    到线才进编成页换位（入替选刀三步舞），回部队屏拿新数据验收
    （成员编号核对）后收工。
    Yields 日志；无需动作时原样返回；成功收场画面在出阵部队屏；
    认不出画面/弹窗/素材未标定/验收不过 → SallyStop。
    """
    if type(team_no) is not int or team_no not in range(1, 6):
        raise ValueError('部队番号应在 1-5 之间')
    try:
        margin = int(margin)
    except (TypeError, ValueError):
        raise ValueError('换队长气力差阈值应是 1-99 的整数')
    if not 1 <= margin <= 99:
        raise ValueError('换队长气力差阈值应是 1-99 的整数')

    snapshot, baseline = _fresh_snapshot(nav, watch, team_no)
    if snapshot is None:
        yield '[换队长] 拿不到新鲜的队伍数据，本轮不换（不猜）'
        return
    fatigue = {m.slot: m.fatigue for m in snapshot.members}
    if any(value is None for value in fatigue.values()):
        yield '[换队长] 全队气力没读齐，本轮不换'
        return
    yield '[换队长] 全队气力：' + '、'.join(
        f'{slot}号位{value}' for slot, value in sorted(fatigue.items()))
    lowest = min(fatigue, key=fatigue.get)
    if lowest == 1:
        yield '[换队长] 队长自己就是全队最低，位置没毛病，收工'
        return
    if captain_rotation_slot(fatigue, margin) is None:
        yield (f'[换队长] 全队最低才 {fatigue[lowest]}，跟队长（{fatigue[1]}）'
               f'差不到 {margin}，不值得折腾，收工')
        return
    captain = next(m for m in snapshot.members if m.slot == 1)
    low = next(m for m in snapshot.members if m.slot == lowest)
    yield (f'[换队长] {_name(low)}（{fatigue[lowest]}）比队长'
           f'{_name(captain)}（{fatigue[1]}）低过 {margin}，去编成页换位')

    _check_calibration(nav)
    before = {m.slot: m.serial_id for m in snapshot.members}
    _goto_formation(nav)
    _ensure_team_tab(nav, team_no)
    _swap_members(nav, 1, lowest, snapshot, watch)
    yield '[换队长] 已换位，回部队屏拿新数据验收…'
    _leave_to_team(nav)
    newer = _await_newer_party_data(nav, watch, baseline)
    if newer is None:
        nav._save_debug(nav.device.screenshot(force=True))
        raise jp_sally.SallyStop(
            '换位后等不到新的队伍数据来验收，已停止；请人工查看游戏')
    new_snapshot = jp_party.build_team_snapshot(
        newer[1], team_no, now=newer[0])
    after = ({m.slot: m.serial_id for m in new_snapshot.members}
             if new_snapshot else {})
    if (after.get(1) != before.get(lowest)
            or after.get(lowest) != before.get(1)):
        nav._save_debug(nav.device.screenshot(force=True))
        raise jp_sally.SallyStop(
            '换位后队伍数据跟预期对不上（可能拖动没生效），已停止；'
            '请人工查看游戏')
    record = getattr(nav, 'record_event', None)
    if record:
        record('formation.captain_rotated', server='jp', team_no=team_no,
               low_slot=lowest, margin=margin)
    yield f'[换队长] ✓ 数据已确认：{_name(low)} 上任队长（编号核对一致）'


def auto_equip_stream(nav, watch, team_no: int):
    """出阵前补刀装：按快照 + 刀种容量找真空槽，去编成页用库存补齐。

    只吃 PartyWatch 的新鲜队伍数据；只用库存现成刀装，绝不碰刀装作成
    （おまかせ/一括实测是全槽替换，不用）。补完回部队屏拿新数据复核满槽；
    仍有缺口（库存不足/未标定）安全停车。
    Yields 日志；无需动作时原样返回；成功收场画面在出阵部队屏；
    认不出画面/弹窗/素材未标定/复核不过 → SallyStop。
    """
    if type(team_no) is not int or team_no not in range(1, 6):
        raise ValueError('部队番号应在 1-5 之间')

    snapshot, baseline = _fresh_snapshot(nav, watch, team_no)
    if snapshot is None:
        yield '[补刀装] 拿不到新鲜的队伍数据，本轮不补（不猜）'
        return
    gapped = [(m, _equip_gaps(m)) for m in snapshot.members]
    gapped = [(m, gaps) for m, gaps in gapped if gaps]
    if not gapped:
        yield '[补刀装] 刀装齐整，不用补'
        return
    yield '[补刀装] ' + '、'.join(
        f'{_name(m)}缺{len(gaps)}槽（槽位{"、".join(map(str, gaps))}）'
        for m, gaps in gapped) + '，去编成页补齐（只用库存，不做新刀装）'

    _check_calibration(nav)
    missing_fill = []
    for name in _EQUIP_FILL_TEMPLATES:
        try:
            jp_vision.load_template(name)
        except jp_vision.TemplateNotFound as exc:
            missing_fill.append(str(exc))
    if missing_fill:
        nav.log('补刀装还缺这些素材（真机无缺口账号未标定）：')
        for item in missing_fill:
            nav.log(f'  ✗ {item}')
    _goto_formation(nav)
    _ensure_team_tab(nav, team_no)
    for member, _gaps in gapped:
        _fill_member_equipment(nav, member)
    yield '[补刀装] 已补齐，回部队屏拿新数据复核…'
    _leave_to_team(nav)
    newer = _await_newer_party_data(nav, watch, baseline)
    if newer is None:
        nav._save_debug(nav.device.screenshot(force=True))
        raise jp_sally.SallyStop(
            '补刀装后等不到新的队伍数据来复核，已停止；请人工查看游戏')
    new_snapshot = jp_party.build_team_snapshot(
        newer[1], team_no, now=newer[0])
    still = []
    if new_snapshot:
        for m in new_snapshot.members:
            if _equip_gaps(m):
                still.append(m)
    if still:
        nav._save_debug(nav.device.screenshot(force=True))
        raise jp_sally.SallyStop(
            '、'.join(_name(m) for m in still) +
            '的刀装还是缺（库存可能不足），已停止；请人工整备刀装')
    record = getattr(nav, 'record_event', None)
    if record:
        record('formation.equipment_refilled', server='jp', team_no=team_no)
    yield '[补刀装] ✓ 刀装已补齐（数据复核满槽）'


# ==================== 【待标定清单】（剩余项） ====================
# 截图同源铁律：模板只准从 JpBrowser 实跑帧裁剪，存 resource/base/image/jp/
# <命名空间>/<名字>.png。已标定：home/formation、formation/picker-title、
# equip/panel（2026-11-22 实跑帧，见 JP_DATA_DIR/debug/frames/fcal-*.png）。
#
# 剩余待标定（都需要一把「真有真空槽」的刀，本测试账号满装无从实测）：
#  1. equip/inventory：点空兵槽后弹出的库存刀装选择面板标题/锚点。
#     标定法：找一把缺装刀 → 装備面板 → 点空槽 → 裁面板锚点 → 再裁一件
#     库存刀装列表项（equip/inventory-item）→ 真机点一件补上空槽 → 核对
#     /sally 的 equip_serial_id 补上、其余槽位原封不动（绝不替换）。
#  2. 空兵槽外观：缺装刀的装备面板上，空兵槽长什么样（与馬/守/宝的空槽
#     是否同款）；若同款可裁 equip/empty 做点槽前像素守卫（更稳）。
#  3. 部队四/五页签：本账号未解锁（party status=0，页签死位）；解锁后按
#     FORMATION_TEAM_TAB_OFFSETS 的 4/5 偏移实测一遍即可。
# 标定后：把库存点选序列补进 _fill_member_equipment，跑 tests/test_jp_formation
# 与 test_jp_sortie 全绿，再在有缺口的账号上真机补一次。
