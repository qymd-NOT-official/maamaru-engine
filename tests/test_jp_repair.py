# -*- coding: utf-8 -*-
"""日服手入（jp_repair）：fake device + fake watch + fake nav。

2026-11-22 真机标定后：模板与常量已入库（resource/base/image/jp/repair/），
preflight 在真盘全绿。识别层（_count_free_slots/_repair_one_member/
_await_data_since 等）仍用 monkeypatch 顶替，专测 jp_repair 自己的
编排与裁决语义：伤员名单只认数据通道、逐个修、完事回本丸、札不足安全停、
验证失败如实报（绝不假绿）、认不出画面停车、札确认窗身份核验、
体检缺件逐条列出（模拟素材缺失路径）。
"""
import time

import pytest

from touken import jp_repair, jp_sally, jp_vision
from touken.device_adapter import Point


class FakeDevice:
    def __init__(self):
        self.clicks = []

    def screenshot(self, force=False):
        return object()

    def click(self, point):
        self.clicks.append(point)
        return True


class FakeWatch:
    """PartyWatch 的假：latest() 返回 scripted (时间戳, payload)。"""

    def __init__(self, latest=None):
        self._latest = latest

    def latest(self):
        return self._latest


class FakeNav:
    """Sally 宿主的最小假：记录导航/点击/存证/账房，classify 可脚本化。"""

    def __init__(self):
        self.logs = []
        self.device = FakeDevice()
        self.poll = 0.01
        self.navigate_calls = []
        self.debug_saved = []
        self.events = []
        self.clicks_hit = []
        self.classify = lambda image: 'repair'
        self._fetch_party_fresh = None

    def log(self, msg):
        self.logs.append(msg)

    def _save_debug(self, image):
        self.debug_saved.append(image)

    def _navigate(self, route, targets, what):
        self.navigate_calls.append((route, frozenset(targets), what))
        return next(iter(targets))

    def _await_party_data(self, watch):
        return watch.latest()

    def _click_hit(self, template, what, image=None):
        self.clicks_hit.append((template, what))

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))


def _payload(teams=None):
    """拼 /sally 风格 payload：{部队号: [(slot, serial, hp, hp_max, sword_id)]}。"""
    teams = teams or {1: [(1, 101, 45, 45, 3), (2, 102, 30, 45, 5)]}
    party, swords = {}, {}
    for team_no, members in teams.items():
        slots = {}
        for slot, serial, hp, hp_max, sword_id in members:
            slots[str(slot)] = {'serial_id': serial}
            swords[str(serial)] = {'serial_id': serial, 'sword_id': sword_id,
                                   'hp': hp, 'hp_max': hp_max, 'fatigue': 50}
        party[str(team_no)] = {'status': 1, 'slot': slots}
    return {'party': party, 'sword': {'sword': swords}}


@pytest.fixture
def calibrated(monkeypatch):
    """假装标定已完成：体检全绿、sleep 提速（与 test_jp_edocastle 同款 idiom）。"""
    monkeypatch.setattr(jp_repair, 'preflight', lambda: [])
    monkeypatch.setattr(jp_repair.time, 'sleep', lambda s: None)


# ---- 体检与探针 ----

def test_preflight_lists_missing_templates(monkeypatch):
    def _boom(name):
        raise jp_vision.TemplateNotFound(f'{name}（模拟缺失）')
    monkeypatch.setattr(jp_vision, 'load_template', _boom)
    missing = jp_repair.preflight()
    # 硬清单模板逐条列出；札确认窗有 OCR 兜底链（SPEEDUP_CONFIRM_ROI
    # 已配置）不算缺；常量已真机标定，不再列缺
    assert len(missing) == len(jp_repair._REQUIRED_TEMPLATES)
    assert any('repair/label' in item for item in missing)
    assert not any('REPAIR_SLOTS' in item for item in missing)


def test_preflight_flags_uncalibrated_constants(monkeypatch):
    # 模板在磁盘上也不算完：常量被清空同样要逐条列出（2026-11-22 骨架期
    # 就吃过这个亏——只查模板会让空常量漏检）
    monkeypatch.setattr(jp_repair, 'REPAIR_SLOTS', ())
    monkeypatch.setattr(jp_repair, 'SELECT_NAME_ROIS', ())
    monkeypatch.setattr(jp_repair, 'SELECT_BTN_OFFSET', None)
    missing = jp_repair.preflight()
    assert any('REPAIR_SLOTS' in item for item in missing)
    assert any('SELECT_NAME_ROIS' in item for item in missing)
    assert any('SELECT_BTN_OFFSET' in item for item in missing)


def test_preflight_green_on_calibrated_templates():
    # 真机标定入库的模板与常量齐全时体检全绿（防回归：谁删模板谁翻车）
    assert jp_repair.preflight() == []


def test_read_repair_state_probe_is_honest_unknown():
    # 探针不可用（0 伤刀实证不了手入中字段，见探针 docstring）：返回
    # None（调用方按「没能确认」如实报），绝不猜
    assert jp_repair._read_repair_state(
        {'sword': {'sword': {'101': {'serial_id': 101}}}}) is None


def test_missing_calibration_lists_and_stops_before_touching_device(monkeypatch):
    def _boom(name):
        raise jp_vision.TemplateNotFound(f'{name}（模拟缺失）')
    monkeypatch.setattr(jp_vision, 'load_template', _boom)
    nav = FakeNav()
    watch = FakeWatch((time.time(), _payload()))
    with pytest.raises(jp_sally.SallyStop, match='没标定全'):
        jp_repair.repair_team_stream(nav, watch, 1)
    assert nav.device.clicks == []
    assert nav.navigate_calls == []
    assert any('✗' in line for line in nav.logs)


# ---- 主流程编排 ----

def test_no_injury_returns_without_navigation(calibrated):
    payload = _payload(teams={1: [(1, 101, 45, 45, 3), (2, 102, 45, 45, 5)]})
    nav = FakeNav()
    report = jp_repair.repair_team_stream(
        nav, FakeWatch((time.time(), payload)), 1)
    assert report.repaired == () and report.unconfirmed == ()
    assert report.slow_running == ()
    # 无伤：一个像素都不点、一步路都不走
    assert nav.navigate_calls == []
    assert nav.device.clicks == []
    assert any('没有受伤' in line for line in nav.logs)


def test_repairs_only_hurt_members_one_by_one_and_goes_home(calibrated,
                                                            monkeypatch):
    pre = _payload(teams={
        1: [(1, 101, 45, 45, 3),
            (2, 102, 30, 45, 5),      # 中伤
            (3, 103, 10, 45, 7)],     # 重伤
        2: [(1, 201, 10, 45, 9)],     # 二队也伤着——绝不能顺手修
    })
    post = _payload(teams={
        1: [(1, 101, 45, 45, 3), (2, 102, 45, 45, 5), (3, 103, 45, 45, 7)],
        2: [(1, 201, 10, 45, 9)],
    })
    nav = FakeNav()
    monkeypatch.setattr(jp_repair, '_count_free_slots', lambda nav: 4)
    repaired_serials = []

    def _fake_one(nav, member, use_speedup, tickets_needed, occurrence):
        repaired_serials.append(member.serial_id)
        return True

    t0 = time.time()
    # 数据包时间戳必须晚于修复开工时刻（PartyWatch 按到达时刻盖章，真机
    # 修一刀要好几秒；别用 time.time() 现取——同一时钟嘀嗒内 since 会相等）
    monkeypatch.setattr(jp_repair, '_repair_one_member', _fake_one)
    monkeypatch.setattr(jp_repair, '_await_data_since',
                        lambda nav, watch, since, timeout: (t0 + 60, post))

    report = jp_repair.repair_team_stream(
        nav, FakeWatch((time.time(), pre)), 1, use_speedup=True)

    # 只修了一队的两振伤刀，逐个修（二队的 201 没被点名）
    assert repaired_serials == ['102', '103']
    assert len(report.repaired) == 2 and report.unconfirmed == ()
    # 行进路线：先去手入，完事回本丸（调用契约）
    assert nav.navigate_calls[0][0] is jp_repair.REPAIR_ROUTE
    assert nav.navigate_calls[0][1] == frozenset({'repair'})
    assert nav.navigate_calls[-1][0] is jp_repair.HOME_ROUTE
    assert nav.navigate_calls[-1][1] == frozenset({'home'})
    # 账房：两笔手入 + 两张札
    queued = [p for e, p in nav.events if e == 'repair.queued']
    assert sorted(q['serial_id'] for q in queued) == ['102', '103']
    assert all(q['speedup'] is True for q in queued)
    tickets = [p for e, p in nav.events
               if e == 'resource.change' and p['resource'] == '手伝い札']
    assert len(tickets) == 2 and all(t['delta'] == -1 for t in tickets)
    assert any(e == 'repair.team_result' and p['repaired'] == 2
               for e, p in nav.events)


def test_verify_failure_reported_honestly_no_fake_green(calibrated,
                                                        monkeypatch):
    """修后的数据到了、但数据说伤还在：如实报没能确认，绝不假绿。"""
    pre = _payload(teams={1: [(1, 101, 45, 45, 3), (2, 102, 30, 45, 5)]})
    post_still_hurt = _payload(teams={
        1: [(1, 101, 45, 45, 3), (2, 102, 28, 45, 5)]})
    nav = FakeNav()
    monkeypatch.setattr(jp_repair, '_count_free_slots', lambda nav: 4)
    monkeypatch.setattr(jp_repair, '_repair_one_member',
                        lambda nav, m, spd, need, occ: True)
    t0 = time.time()
    monkeypatch.setattr(jp_repair, '_await_data_since',
                        lambda nav, watch, since, timeout:
                        (t0 + 60, post_still_hurt))

    report = jp_repair.repair_team_stream(
        nav, FakeWatch((time.time(), pre)), 1, use_speedup=True)

    assert report.repaired == ()
    assert len(report.unconfirmed) == 1
    assert any('没能确认' in line for line in nav.logs)
    # 假绿防线：账房里的结果同样如实
    assert any(e == 'repair.team_result' and p['repaired'] == 0
               and p['unconfirmed'] == 1 for e, p in nav.events)


def test_team_away_stops_before_navigating(calibrated):
    payload = _payload()
    payload['party']['1']['status'] = 3          # 出阵中
    nav = FakeNav()
    with pytest.raises(jp_sally.SallyStop, match='不在本丸待命'):
        jp_repair.repair_team_stream(
            nav, FakeWatch((time.time(), payload)), 1)
    assert nav.navigate_calls == []


def test_bad_arguments_rejected():
    nav = FakeNav()
    with pytest.raises(ValueError):
        jp_repair.repair_team_stream(nav, FakeWatch(), 0)
    with pytest.raises(ValueError):
        jp_repair.repair_team_stream(nav, FakeWatch(), 6)
    with pytest.raises(ValueError):
        jp_repair.repair_team_stream(nav, None, 1)


# ---- 安全停车 ----

def test_speedup_ticket_shortage_stops_before_starting(calibrated,
                                                       monkeypatch):
    pre = _payload(teams={1: [(1, 101, 30, 45, 3),
                              (2, 102, 20, 45, 5),
                              (3, 103, 10, 45, 7)]})
    nav = FakeNav()
    monkeypatch.setattr(jp_repair, '_count_free_slots', lambda nav: 2)
    monkeypatch.setattr(jp_repair, '_locate_member_row',
                        lambda nav, m, image, occ: Point(10, 20))
    monkeypatch.setattr(jp_repair, '_await_select_detail',
                        lambda nav, label: None)
    monkeypatch.setattr(jp_repair, '_read_speedup_count', lambda nav: 0)
    with pytest.raises(jp_sally.SallyStop, match='手伝い札只剩 0 张'):
        jp_repair.repair_team_stream(
            nav, FakeWatch((time.time(), pre)), 1, use_speedup=True)


def test_repair_slot_full_stops(calibrated, monkeypatch):
    pre = _payload(teams={1: [(1, 101, 30, 45, 3)]})
    nav = FakeNav()
    monkeypatch.setattr(jp_repair, '_count_free_slots', lambda nav: 0)
    with pytest.raises(jp_sally.SallyStop, match='手入位全满'):
        jp_repair.repair_team_stream(
            nav, FakeWatch((time.time(), pre)), 1, use_speedup=True)
    assert nav.device.clicks == []


def test_unknown_screen_during_select_stops(calibrated, monkeypatch):
    """给成员选人时撞上认不出的画面（如战斗屏）：停车存证，绝不盲点。"""
    pre = _payload(teams={1: [(1, 101, 30, 45, 3)]})
    nav = FakeNav()
    nav.classify = lambda image: 'battle'
    monkeypatch.setattr(jp_repair, '_count_free_slots', lambda nav: 4)
    monkeypatch.setattr(jp_repair, '_locate_member_row',
                        lambda nav, m, image, occ: Point(1, 2))
    with pytest.raises(jp_sally.SallyStop, match='不认识的画面'):
        jp_repair.repair_team_stream(
            nav, FakeWatch((time.time(), pre)), 1, use_speedup=True)
    assert nav.debug_saved                      # 现场存证


# ---- 札确认窗：唯一准许点的弹窗，身份核验不过就停 ----

def test_speedup_confirm_accepts_verified_dialog(calibrated, monkeypatch):
    nav = FakeNav()
    screens = iter(['confirm', 'repair'])
    nav.classify = lambda image: next(screens, 'repair')
    monkeypatch.setattr(jp_repair, '_is_speedup_confirm',
                        lambda nav, image: True)
    jp_repair._await_speedup_confirm(nav, '测试刀')
    assert ('dialog/accept', '手伝い札确认（はい）') in nav.clicks_hit


def test_speedup_confirm_rejects_unverified_dialog(calibrated, monkeypatch):
    nav = FakeNav()
    nav.classify = lambda image: 'dialog'
    monkeypatch.setattr(jp_repair, '_is_speedup_confirm',
                        lambda nav, image: False)
    with pytest.raises(jp_sally.SallyStop, match='认不出的弹窗'):
        jp_repair._await_speedup_confirm(nav, '测试刀')
    assert nav.debug_saved
    assert nav.clicks_hit == []
