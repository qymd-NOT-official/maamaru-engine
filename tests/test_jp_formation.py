# -*- coding: utf-8 -*-
"""日服编成画面流程（jp_formation）：换队长/补刀装的决策、导航与验收。

真机标定（2026-11-22）后 _swap_members 是「入替选刀三步舞」、
_fill_member_equipment 走「装备面板逐空槽」；流测试把这两个交互
monkeypatch 成数据桩驱动验收分支，helper（页签/选刀/草稿队/缺口）
用假 nav 单测。模板素材真机已标定的三个在 REQUIRED_TEMPLATES。
"""
import copy
import time

import pytest
from PIL import Image

from touken import jp_formation, jp_ocr, jp_party, jp_sally, jp_sortie, jp_vision


class FakeDevice:
    base_size = (1508, 1088)

    def __init__(self):
        self.clicks = []
        self.swipes = []

    def screenshot(self, force=False):
        return object()

    def click(self, point):
        self.clicks.append(point)
        return True

    def swipe(self, *args, **kwargs):
        self.swipes.append(args)
        return True


class FakeWatch:
    """party_watch 桩：latest() 返回最后一次 push 的数据。"""

    def __init__(self):
        self._latest = None

    def latest(self):
        return self._latest

    def push(self, payload, at=None):
        self._latest = (at if at is not None else time.time(), payload)
        return self._latest


def make_nav():
    logs = []
    nav = jp_sortie.JpSortie(FakeDevice(), {}, log=logs.append,
                             save_debug=lambda image: None)
    return nav, logs


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(jp_formation.time, 'sleep', lambda *_: None)
    # 等数据的宽限压到近乎为零：桩数据要么立刻有，要么立刻算等不到
    monkeypatch.setattr(jp_sally, 'PARTY_DATA_WAIT', 0.01)
    # 模板素材真机标定前一律缺失；默认视为体检已过，
    # 「缺素材停车」的用例再各自把 missing_templates 改回非空
    monkeypatch.setattr(jp_formation, 'missing_templates', lambda: [])
    # 流测试不认屏：过渡/页签 helper 另行单测
    monkeypatch.setattr(jp_formation, '_settle_unknown', lambda nav: 'home')
    monkeypatch.setattr(jp_formation, '_ensure_team_tab',
                        lambda nav, team_no: None)


# ==================== 数据合成 ====================
# 素材体检/页签/问候的真身（autouse fixture 会把模块属性换成桩，留底用于还原）
_REAL_MISSING_TEMPLATES = jp_formation.missing_templates
_REAL_ENSURE_TEAM_TAB = jp_formation._ensure_team_tab
_REAL_SETTLE_UNKNOWN = jp_formation._settle_unknown


def _party_payload(members, team_no=1):
    """合成 /sally 响应：members = [(slot, serial, sword_id, hp, hp_max,
    fatigue, equip3)]；equip3 为三槽刀装有/无布尔三元组。"""
    slot = {str(i): {'serial_id': None} for i in range(1, 7)}
    sword = {}
    for slot_no, serial, sword_id, hp, hp_max, fatigue, equip in members:
        slot[str(slot_no)] = {'serial_id': serial}
        record = {'serial_id': serial, 'sword_id': sword_id,
                  'hp': hp, 'hp_max': hp_max, 'fatigue': fatigue}
        for i, ok in enumerate(equip, start=1):
            record[f'equip_serial_id{i}'] = (9000 + i) if ok else None
        sword[str(serial)] = record
    return {'party': {str(team_no): {'status': 1, 'slot': slot}},
            'sword': sword}


def _two_members(f1=80, f2=60):
    return _party_payload([(1, 101, 3, 40, 40, f1, (True, True, True)),
                           (2, 102, 3, 40, 40, f2, (True, True, True))])


def _swap_slots(payload, team_no, a, b):
    out = copy.deepcopy(payload)
    slot = out['party'][str(team_no)]['slot']
    slot[str(a)], slot[str(b)] = slot[str(b)], slot[str(a)]
    return out


def _record_navigate(nav):
    calls = []

    def fake(route, targets, what):
        calls.append((route, set(targets), what))
        return next(iter(targets))

    nav._navigate = fake
    return calls


def _stub_navigate(nav):
    """导航桩：不录调用点，只保证走得到（停车类用例用）。"""
    nav._navigate = lambda route, targets, what: next(iter(targets))


# ==================== 素材体检 / 路线表 ====================

def test_missing_templates_lists_everything(monkeypatch):
    monkeypatch.setattr(jp_formation, 'missing_templates',
                        _REAL_MISSING_TEMPLATES)

    def _boom(name):
        raise jp_vision.TemplateNotFound(f'{name}（模拟缺失）')
    monkeypatch.setattr(jp_vision, 'load_template', _boom)
    missing = jp_formation.missing_templates()
    assert len(missing) == len(jp_formation.REQUIRED_TEMPLATES)


def test_route_tables_follow_sally_conventions():
    # 进编成：本丸直点「結成」；出阵各屏经目録中转，目录回本丸走本丸瓦
    assert jp_formation.FORMATION_ROUTE['home'][1] == 'home/formation'
    assert jp_formation.FORMATION_ROUTE['team'][1] == 'home/catalog'
    assert jp_formation.FORMATION_ROUTE['directory'][1] == 'directory/home'
    # 回程复用 Sally 出阵路线表：编成页返回箭头已在其中
    assert jp_sally.SALLY_ROUTE['formation'][1] == 'formation/label'
    assert jp_sally.SALLY_ROUTE['formation'][2] == jp_sally.BACK_ARROW_OFFSET
    # 模板命名空间规范：编成页元素 formation/*，刀装交互 equip/*
    for name in jp_formation.REQUIRED_TEMPLATES:
        assert name.startswith(('home/', 'directory/', 'menu/',
                                'formation/', 'equip/'))


# ==================== 换队长：决策 ====================

def test_rotate_skips_when_gap_below_margin(monkeypatch):
    nav, _ = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    watch.push(_two_members(f1=80, f2=65), at=time.time())
    swapped = []
    monkeypatch.setattr(jp_formation, '_swap_members',
                        lambda *a: swapped.append(a))
    lines = list(jp_formation.rotate_captain_stream(nav, watch, 1, 20))
    assert swapped == [] and calls == []
    assert nav.device.clicks == []
    assert '差不到 20' in '\n'.join(lines)


def test_rotate_swaps_when_gap_reaches_margin(monkeypatch):
    nav, _ = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    # 时间戳一律用真实时刻：验收链路还查「数据不晚于现在」
    watch.push(_two_members(f1=80, f2=60))
    swapped = []
    events = []
    nav.record_event = lambda et, **kw: events.append((et, kw))

    def _fake_swap(n, a_slot, b_slot, snapshot, watch):
        swapped.append((a_slot, b_slot, snapshot.team_no))
        # 换位后游戏重报 /sally：编号跟着换（气力不变）
        watch.push(_swap_slots(_two_members(f1=80, f2=60), 1, 1, 2))

    monkeypatch.setattr(jp_formation, '_swap_members', _fake_swap)
    lines = list(jp_formation.rotate_captain_stream(nav, watch, 1, 15))
    assert swapped == [(1, 2, 1)]         # 队长(1号位)与最低气力(2号位)换位
    assert [c[1] for c in calls] == [{'formation'}, {'team'}]
    assert calls[0][0] is jp_formation.FORMATION_ROUTE
    assert calls[1][0] is jp_sally.SALLY_ROUTE
    assert '✓' in '\n'.join(lines)
    assert events == [('formation.captain_rotated',
                       {'server': 'jp', 'team_no': 1, 'low_slot': 2,
                        'margin': 15})]
    # 决策/导航/验收层不直接点像素（交互全在 _swap_members 里）
    assert nav.device.clicks == [] and nav.device.swipes == []


def test_rotate_skips_when_captain_already_lowest():
    nav, _ = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    watch.push(_two_members(f1=40, f2=90), at=time.time())
    lines = list(jp_formation.rotate_captain_stream(nav, watch, 1, 10))
    assert calls == [] and '位置没毛病' in '\n'.join(lines)


def test_rotate_skips_when_fatigue_incomplete():
    nav, _ = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    payload = _party_payload([(1, 101, 3, 40, 40, 80, (True,) * 3),
                              (2, 102, 3, 40, 40, None, (True,) * 3)])
    watch.push(payload, at=time.time())
    lines = list(jp_formation.rotate_captain_stream(nav, watch, 1, 10))
    assert calls == [] and '没读齐' in '\n'.join(lines)


def test_rotate_skips_when_data_stale():
    nav, _ = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    watch.push(_two_members(), at=time.time() - 1000)   # 远超 MAX_AGE
    lines = list(jp_formation.rotate_captain_stream(nav, watch, 1, 10))
    assert calls == [] and nav.device.clicks == []
    assert '本轮不换' in '\n'.join(lines)


# ==================== 换队长：安全停车 ====================

def test_rotate_missing_templates_stops_before_navigating(monkeypatch):
    nav, logs = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    watch.push(_two_members(f1=80, f2=60), at=time.time())
    monkeypatch.setattr(jp_formation, 'missing_templates',
                        lambda: ['formation/picker-title（模拟缺失）'])
    with pytest.raises(jp_sally.SallyStop, match='没标定'):
        list(jp_formation.rotate_captain_stream(nav, watch, 1, 15))
    text = '\n'.join(logs)
    assert 'formation/picker-title' in text
    # 素材不全：一个像素都不许点，编成页也不许进
    assert calls == [] and nav.device.clicks == []


def test_rotate_unknown_screen_stops_with_debug(monkeypatch):
    saved = []
    nav = jp_sortie.JpSortie(FakeDevice(), {}, log=lambda m: None,
                             save_debug=saved.append)
    watch = FakeWatch()
    watch.push(_two_members(f1=80, f2=60), at=time.time())

    def _lost(route, targets, what):
        # 照真实 _navigate 的行为：存证后停车
        nav._save_debug(object())
        raise jp_sally.SallyStop(f'{what}时遇到了不认识的画面，已停止')
    nav._navigate = _lost
    monkeypatch.setattr(jp_formation, '_settle_unknown', lambda n: 'unknown')
    with pytest.raises(jp_sally.SallyStop, match='不认识的画面'):
        list(jp_formation.rotate_captain_stream(nav, watch, 1, 15))
    assert saved                      # 存证落盘（截图已存 debug）


def test_rotate_unverified_swap_stops(monkeypatch):
    saved = []
    nav = jp_sortie.JpSortie(FakeDevice(), {}, log=lambda m: None,
                             save_debug=saved.append)
    _stub_navigate(nav)
    watch = FakeWatch()
    watch.push(_two_members(f1=80, f2=60))
    # 换位后没有任何新数据回流：验收等不到，停车
    monkeypatch.setattr(jp_formation, '_swap_members', lambda *a: None)
    with pytest.raises(jp_sally.SallyStop, match='验收'):
        list(jp_formation.rotate_captain_stream(nav, watch, 1, 15))
    assert saved


def test_rotate_mismatched_data_stops(monkeypatch):
    saved = []
    nav = jp_sortie.JpSortie(FakeDevice(), {}, log=lambda m: None,
                             save_debug=saved.append)
    _stub_navigate(nav)
    watch = FakeWatch()
    watch.push(_two_members(f1=80, f2=60))

    def _fake_swap(n, a, b, snapshot, watch):
        # 数据回流了但编号没变（拖动被吞）：与预期对不上，停车
        watch.push(_two_members(f1=80, f2=60))

    monkeypatch.setattr(jp_formation, '_swap_members', _fake_swap)
    with pytest.raises(jp_sally.SallyStop, match='对不上'):
        list(jp_formation.rotate_captain_stream(nav, watch, 1, 15))
    assert saved


def test_rotate_validates_params():
    nav, _ = make_nav()
    watch = FakeWatch()
    with pytest.raises(ValueError):
        list(jp_formation.rotate_captain_stream(nav, watch, 6, 10))
    with pytest.raises(ValueError):
        list(jp_formation.rotate_captain_stream(nav, watch, 1, 0))
    with pytest.raises(ValueError):
        list(jp_formation.rotate_captain_stream(nav, watch, 1, 'x'))


# ==================== 补刀装 ====================

def test_equip_full_no_trip():
    nav, _ = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    watch.push(_two_members(), at=time.time())
    lines = list(jp_formation.auto_equip_stream(nav, watch, 1))
    assert calls == [] and nav.device.clicks == []
    assert '不用补' in '\n'.join(lines)


def test_equip_fills_gaps_and_verifies(monkeypatch):
    nav, _ = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    gapped = _party_payload(
        [(1, 101, 3, 40, 40, 80, (True, True, True)),
         (2, 102, 3, 40, 40, 60, (True, False, False))])   # 三日月 slots=3
    watch.push(gapped)
    filled = []
    events = []
    nav.record_event = lambda et, **kw: events.append((et, kw))

    def _fake_fill(n, member):
        filled.append(member.slot)
        healed = copy.deepcopy(gapped)
        healed['sword']['102']['equip_serial_id2'] = 9002
        healed['sword']['102']['equip_serial_id3'] = 9003
        watch.push(healed)

    monkeypatch.setattr(jp_formation, '_fill_member_equipment', _fake_fill)
    lines = list(jp_formation.auto_equip_stream(nav, watch, 1))
    assert filled == [2]              # 只补有缺口的成员
    assert [c[1] for c in calls] == [{'formation'}, {'team'}]
    assert '✓' in '\n'.join(lines)
    assert events == [('formation.equipment_refilled',
                       {'server': 'jp', 'team_no': 1})]
    assert nav.device.clicks == []


def test_equip_gapped_tantou_is_full_house():
    # 短刀只有 1 槽（sword_db.slots）：(True,False,False) 是满装不是缺口
    payload = _party_payload([(1, 101, 83, 36, 36, 92, (True, False, False))])
    snap = jp_party.build_team_snapshot(payload, 1)
    assert snap is not None
    member = snap.members[0]
    assert member.name == '小夜左文字'
    assert jp_formation._equip_gaps(member) == []
    # 空槽才算缺口
    payload2 = _party_payload([(1, 101, 83, 36, 36, 92, (False, False, False))])
    member2 = jp_party.build_team_snapshot(payload2, 1).members[0]
    assert jp_formation._equip_gaps(member2) == [1]


def test_equip_still_gapped_stops(monkeypatch):
    saved = []
    nav = jp_sortie.JpSortie(FakeDevice(), {}, log=lambda m: None,
                             save_debug=saved.append)
    _stub_navigate(nav)
    watch = FakeWatch()
    gapped = _party_payload(
        [(1, 101, 3, 40, 40, 80, (True, True, True)),
         (2, 102, 3, 40, 40, 60, (True, False, False))])
    watch.push(gapped)
    # 补完缺口还在（库存不足）：游戏重报后仍旧缺（深拷贝=新的服务器读数），复核不过，停车
    monkeypatch.setattr(jp_formation, '_fill_member_equipment',
                        lambda n, m: watch.push(copy.deepcopy(gapped)))
    with pytest.raises(jp_sally.SallyStop, match='还是缺'):
        list(jp_formation.auto_equip_stream(nav, watch, 1))
    assert saved


def test_equip_skips_when_data_stale():
    nav, _ = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    watch.push(_two_members(), at=time.time() - 1000)
    lines = list(jp_formation.auto_equip_stream(nav, watch, 1))
    assert calls == [] and '本轮不补' in '\n'.join(lines)


def test_equip_missing_templates_stops_before_navigating(monkeypatch):
    nav, logs = make_nav()
    calls = _record_navigate(nav)
    watch = FakeWatch()
    gapped = _party_payload(
        [(1, 101, 3, 40, 40, 80, (False, True, True))])
    watch.push(gapped, at=time.time())
    monkeypatch.setattr(jp_formation, 'missing_templates',
                        lambda: ['equip/panel（模拟缺失）'])
    with pytest.raises(jp_sally.SallyStop, match='没标定'):
        list(jp_formation.auto_equip_stream(nav, watch, 1))
    assert 'equip/panel' in '\n'.join(logs)
    assert calls == [] and nav.device.clicks == []


def test_equip_validates_team_no():
    nav, _ = make_nav()
    watch = FakeWatch()
    with pytest.raises(ValueError):
        list(jp_formation.auto_equip_stream(nav, watch, 0))


# ==================== 草稿队挑选（换位舞的数据基础） ====================

def _teams_payload(teams):
    """teams = {team_no: [serial 或 None ×6]}，status 默认 1。"""
    party = {}
    sword = {}
    for no, members in teams.items():
        slots = {}
        for i, serial in enumerate(members, start=1):
            slots[str(i)] = {'serial_id': serial}
            if serial:
                sword[str(serial)] = {
                    'serial_id': serial, 'sword_id': 3,
                    'hp': 40, 'hp_max': 40, 'fatigue': 80,
                    'equip_serial_id1': 9001,
                    'equip_serial_id2': None, 'equip_serial_id3': None}
        party[str(no)] = {'status': 1, 'slot': slots}
    return {'party': party, 'sword': sword}


def test_scratch_team_picks_empty_unlocked_team():
    payload = _teams_payload({1: [101, 102, None, None, None, None],
                              2: [201, 202, 203, 204, 205, 206],
                              3: [None] * 6})
    assert jp_formation._scratch_team(payload, own_team=1) == 3
    # 本队自己空格时不拿自己当草稿
    assert jp_formation._scratch_team(payload, own_team=3) is None
    # 没有空格 → None
    payload2 = _teams_payload({1: [101, 102, None, None, None, None],
                               2: [201, 202, 203, 204, 205, 206]})
    assert jp_formation._scratch_team(payload2, own_team=1) is None


def test_scratch_team_skips_locked_team():
    payload = _teams_payload({1: [101, 102, None, None, None, None],
                              3: [None] * 6})
    payload['party']['3']['status'] = 0          # 未解锁（本账号部队四/五实测如此）
    assert jp_formation._scratch_team(payload, own_team=1) is None


# ==================== 页签确认 / 问候跳过 / 选刀（假 nav 单测） ====================

class _MiniNav:
    """极简 nav：只装 helper 需要的零件。"""

    def __init__(self, screens):
        self._screens = list(screens)
        self.device = self
        self.clicks = []
        self.log = lambda msg: None
        self._save_debug = lambda image: None

    def screenshot(self, force=False):
        return self._screens.pop(0) if self._screens else Image.new(
            'RGB', (1508, 1003), (250, 250, 250))

    def click(self, point):
        self.clicks.append(point)
        return True


def _hit(nx, ny, score=0.95):
    return jp_vision.Match(name='formation/label', score=score,
                           nx=nx, ny=ny, nw=100 / 1508, nh=120 / 1003)


def _tab_image(bright):
    img = Image.new('RGB', (1508, 1003), (240, 230, 200))
    color = (225, 222, 215) if bright else (226, 160, 60)   # 选中白灰/橙
    for dx in range(-6, 7):
        for dy in range(-6, 7):
            img.putpixel((200 + dx, 201 + dy), color)
    return img


def test_ensure_team_tab_clicks_only_when_unselected(monkeypatch):
    monkeypatch.setattr(jp_formation, '_ensure_team_tab', _REAL_ENSURE_TEAM_TAB)
    # 锚点 (103,142) + 页签1 偏移 (97,59) = (200,201)
    monkeypatch.setattr(jp_vision, 'find',
                        lambda img, name: _hit(103 / 1508, 142 / 1003))
    # 函数取帧两次（锚点帧 + 页签判定帧），已选中：两帧都白
    nav = _MiniNav([_tab_image(bright=True), _tab_image(bright=True)])
    jp_formation._ensure_team_tab(nav, 1)
    assert nav.clicks == []                      # 已选中不点

    # 未选中：锚点帧任意、判定帧先橙（点）后白（复选通过）
    frames = [_tab_image(bright=True), _tab_image(bright=False),
              _tab_image(bright=True)]
    nav2 = _MiniNav(frames)
    jp_formation._ensure_team_tab(nav2, 1)
    assert len(nav2.clicks) == 1
    assert (nav2.clicks[0].x, nav2.clicks[0].y) == (200, 201)


def test_ensure_team_tab_stops_when_click_fails(monkeypatch):
    monkeypatch.setattr(jp_formation, '_ensure_team_tab', _REAL_ENSURE_TEAM_TAB)
    saved = []
    monkeypatch.setattr(jp_vision, 'find',
                        lambda img, name: _hit(103 / 1508, 142 / 1003))
    monkeypatch.setattr(jp_formation, '_tab_selected',
                        lambda img, tx, ty: False)
    nav = _MiniNav([_tab_image(bright=False), _tab_image(bright=False)])
    nav._save_debug = saved.append
    with pytest.raises(jp_sally.SallyStop, match='点不动'):
        jp_formation._ensure_team_tab(nav, 4)    # 部队四未解锁场景


def test_settle_unknown_taps_center_then_gives_up(monkeypatch):
    monkeypatch.setattr(jp_formation, '_settle_unknown', _REAL_SETTLE_UNKNOWN)

    class _C:
        def __init__(self):
            self.device = self
            self.calls = 0
            self.taps = 0

        def classify(self, image):
            self.calls += 1
            return 'unknown' if self.calls <= 2 else 'home'

        def screenshot(self, force=False):
            return Image.new('RGB', (1508, 1003))

        def click(self, point):
            self.taps += 1
            return True

    nav = _C()
    result = jp_formation._settle_unknown(nav)
    assert result == 'home'
    assert nav.taps == 2                  # 两次 unknown，各点一次中央


def test_picker_pick_scrolls_until_name_then_clicks_decide(monkeypatch):
    sent = []

    class _B:
        def check_view(self):
            return {'clientWidth': 1215, 'clientHeight': 803}

        def _send(self, _method, params):
            sent.append(params['deltaY'])

    class _D(_MiniNav):
        @property
        def _browser(self):
            return _B()

    seen = {'ocr': 0}

    def _tokens(crop):
        seen['ocr'] += 1
        if seen['ocr'] == 1:
            return [('後藤藤四郎', 1.0, 120.0, 40.0)]
        return [('小夜左文字', 1.0, 120.0, 577.5)]   # ROI 内坐标

    monkeypatch.setattr(jp_ocr, 'read_tokens', _tokens)
    closed = {'v': False}
    monkeypatch.setattr(jp_formation, '_picker_visible',
                        lambda nav: closed['v'])
    nav = _D([Image.new('RGB', (1508, 1003)), Image.new('RGB', (1508, 1003))])
    nav.device = nav
    jp_formation._picker_pick(nav, '小夜左文字')
    assert sent == [jp_formation.PICKER_WHEEL_DY]     # 第一屏没有，滚了一次
    # 名字 token cy 577.5 + ROI 顶 220 = 797.5；決定 = 797.5 - 31
    assert nav.clicks[-1].x == jp_formation.PICKER_DECIDE_X
    assert nav.clicks[-1].y == 797 - 31


def test_picker_pick_stops_when_name_never_found(monkeypatch):
    monkeypatch.setattr(jp_ocr, 'read_tokens',
                        lambda crop: [('別の刀', 1.0, 120.0, 40.0)])
    saved = []

    class _B:
        def check_view(self):
            return {'clientWidth': 1215, 'clientHeight': 803}

        def _send(self, _m, _p):
            pass

    class _D(_MiniNav):
        @property
        def _browser(self):
            return _B()

    nav = _D([Image.new('RGB', (1508, 1003))])
    nav.device = nav
    nav._save_debug = saved.append
    with pytest.raises(jp_sally.SallyStop, match='翻到底也没找到'):
        jp_formation._picker_pick(nav, '一期一振')


# ==================== 换位舞编排（假 nav 单测） ====================

class _DanceNav(_MiniNav):
    def __init__(self, payload):
        super().__init__([Image.new('RGB', (1508, 1003))])
        self.calls = []
        self._payload = payload

    def _await_party_data(self, watch):
        return (time.time(), self._payload)


def _dance_snapshot():
    payload = _party_payload([(1, 101, 3, 40, 40, 100, (True,) * 3),
                              (2, 102, 83, 36, 36, 60, (True,) * 3)])
    return jp_party.build_team_snapshot(payload, 1, now=time.time())


def test_swap_members_three_step_dance(monkeypatch):
    teams = _teams_payload({1: [101, 102, None, None, None, None],
                            3: [None] * 6})
    nav = _DanceNav(teams)
    seq = []
    monkeypatch.setattr(jp_formation, '_ensure_team_tab',
                        lambda n, t: seq.append(('tab', t)))
    monkeypatch.setattr(jp_formation, '_open_picker',
                        lambda n, s: seq.append(('open', s)))
    monkeypatch.setattr(jp_formation, '_picker_pick',
                        lambda n, name: seq.append(('pick', name)))
    snapshot = _dance_snapshot()
    jp_formation._swap_members(nav, 1, 2, snapshot, FakeWatch())
    assert seq == [
        ('tab', 3), ('open', 1), ('pick', '小夜左文字'),      # B → 草稿
        ('tab', 1), ('open', 1), ('pick', '小夜左文字'),      # B → 队长槽
        ('tab', 1), ('open', 2), ('pick', '三日月宗近'),      # A → 原槽
    ]


def test_swap_members_stops_without_scratch(monkeypatch):
    teams = _teams_payload({1: [101, 102, None, None, None, None],
                            2: [201, 202, 203, 204, 205, 206]})
    nav = _DanceNav(teams)
    snapshot = _dance_snapshot()
    with pytest.raises(jp_sally.SallyStop, match='空格部队'):
        jp_formation._swap_members(nav, 1, 2, snapshot, FakeWatch())
