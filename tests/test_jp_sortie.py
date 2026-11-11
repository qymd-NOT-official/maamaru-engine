# -*- coding: utf-8 -*-
"""日服普通出阵刷图（jp_sortie）：参数校验、认屏分派、圈循环、错误自愈分派。

画面导航/点击不走真设备：classify 用脚本化假序列，_click_hit/_click_near
记录调用；素材体检用 monkeypatch 模拟模板缺失——宿主必须把缺什么列清楚
然后停，绝不盲点。
"""
import pytest

from touken import jp_sally, jp_sortie, jp_vision


class FakeDevice:
    base_size = (1508, 1088)

    def __init__(self):
        self.clicks = []
        self.reloaded = 0

    def screenshot(self, force=False):
        return object()

    def click(self, point):
        self.clicks.append(point)
        return True

    def reload(self):
        self.reloaded += 1


def make_host(params=None):
    logs = []
    host = jp_sortie.JpSortie(FakeDevice(), params or {}, log=logs.append,
                              save_debug=lambda image: None)
    return host, logs


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(jp_sortie.time, 'sleep', lambda *_: None)


def _party_payload(fatigue):
    return {
        'party': {'1': {'status': 1, 'slot': {'1': {'serial_id': 100}}}},
        'sword': {'100': {'serial_id': 100, 'sword_id': 12345,
                          'hp': 40, 'hp_max': 40, 'fatigue': fatigue}},
    }


# ==================== 画面分类表 / 路线表 ====================

def test_screens_table_keeps_overlays_and_adds_error_screens():
    names = [name for name, _ in jp_sortie.SCREENS]
    assert names[:6] == [name for name, _ in jp_sally.SCREENS[:6]]
    assert 'error' in names and 'error_full' in names
    # 时代/小图/部队屏锚点沿用 Sally 的
    for kept in ('era', 'map', 'team', 'home'):
        assert kept in names


def test_routes():
    # 进图路线到时代屏就停：不许拿游戏记忆的选择自动决定
    assert 'era' not in jp_sortie.SORTIE_ROUTE
    assert 'map' not in jp_sortie.SORTIE_ROUTE
    assert jp_sortie.SORTIE_ROUTE['directory'] == jp_sally.SALLY_ROUTE['directory']
    # 回本丸走目録大页「本丸」瓦，不是 common/back 瞎点
    assert jp_sortie.HOME_ROUTE['directory'][1] == 'directory/home'
    for screen in ('era', 'map', 'team'):
        assert jp_sortie.HOME_ROUTE[screen][1] == 'home/catalog'


# ==================== 参数校验 ====================

def test_run_validates_params_before_touching_device():
    host, _ = make_host()
    with pytest.raises(ValueError):
        host.run(chapter=0, map_no=1, team_no=1, rounds=1)
    with pytest.raises(ValueError):
        host.run(chapter=9, map_no=1, team_no=1, rounds=1)
    with pytest.raises(ValueError):
        host.run(chapter=1, map_no=0, team_no=1, rounds=1)
    with pytest.raises(ValueError):
        host.run(chapter=1, map_no=5, team_no=1, rounds=1)
    with pytest.raises(ValueError):
        host.run(chapter=1, map_no=1, team_no=0, rounds=1)
    with pytest.raises(ValueError):
        host.run(chapter=1, map_no=1, team_no=6, rounds=1)
    with pytest.raises(ValueError):
        host.run(chapter=1, map_no=1, team_no=1, rounds=0)
    with pytest.raises(ValueError):
        host.run(chapter=1, map_no=1, team_no=1, rounds=201)
    with pytest.raises(ValueError):
        host.run(chapter=1, map_no=1, team_no=1, rounds=1, fatigue_threshold=0)


def test_run_script_rejects_bad_params():
    with pytest.raises(ValueError):
        list(jp_sortie.run_script(params={'chapter': 'abc'}))
    with pytest.raises(ValueError):
        list(jp_sortie.run_script(params={'map_no': 'x'}))
    with pytest.raises(ValueError):
        list(jp_sortie.run_script(params={'team_no': 'many'}))
    with pytest.raises(ValueError):
        list(jp_sortie.run_script(params={'rounds': 'lots'}))


# ==================== 素材体检 ====================

@pytest.fixture
def templates_missing(monkeypatch):
    def _boom(name):
        raise jp_vision.TemplateNotFound(f'{name}（模拟缺失）')
    monkeypatch.setattr(jp_vision, 'load_template', _boom)


def test_preflight_lists_every_missing_template(templates_missing):
    host, _ = make_host()
    missing = host._preflight()
    assert len(missing) == len(jp_sortie._REQUIRED_TEMPLATES)


def test_run_reports_missing_templates_and_stops(templates_missing):
    host, logs = make_host()
    with pytest.raises(jp_sally.SallyStop):
        host.run(chapter=1, map_no=1, team_no=1, rounds=1)
    text = "\n".join(logs)
    assert '没标定' in text
    for name in jp_sortie._REQUIRED_TEMPLATES:
        assert name in text
    # 模板不全时一个像素都不许点
    assert host.device.clicks == []


# ==================== 选图认屏分派 ====================

def _scripted(host, screens):
    answers = iter(screens)
    host.classify = lambda image: next(answers)


def _record_clicks(host):
    calls = []
    host._click_near = lambda anchor, offset, what, image=None: calls.append(
        ('near', anchor, offset))
    host._click_hit = lambda template, what, image=None: calls.append(
        ('hit', template))
    return calls


def test_pick_map_walks_era_then_map_then_team():
    host, _ = make_host()
    _scripted(host, ['era', 'era', 'map', 'map', 'team'])
    calls = _record_clicks(host)
    host._pick_map(chapter=2, map_no=1)
    assert calls == [
        ('near', 'era/decide', jp_sortie.ERA_OFFSETS[2]),   # 点时代卡
        ('hit', 'era/decide'),                              # 仍在时代屏，补决定
        ('near', 'map/team-select', jp_sortie.MAP_OFFSETS[1]),  # 点小图卡
        ('hit', 'map/team-select'),                         # 部队选择入口
    ]


def test_pick_map_era_card_jumps_so_no_decide_click():
    # 点已记住的时代卡直接跳到小图屏：不许补点决定
    host, _ = make_host()
    _scripted(host, ['era', 'map', 'map', 'team'])
    calls = _record_clicks(host)
    host._pick_map(chapter=1, map_no=2)
    assert calls == [
        ('near', 'era/decide', jp_sortie.ERA_OFFSETS[1]),
        ('near', 'map/team-select', jp_sortie.MAP_OFFSETS[2]),
    ]


def test_pick_map_landing_on_team_returns_without_clicks():
    host, _ = make_host()
    _scripted(host, ['team'])
    calls = _record_clicks(host)
    host._pick_map(chapter=1, map_no=1)
    assert calls == []


def test_pick_map_dialog_stops():
    host, _ = make_host()
    _scripted(host, ['dialog'])
    _record_clicks(host)
    with pytest.raises(jp_sally.SallyStop):
        host._pick_map(chapter=1, map_no=1)


def test_pick_map_error_screen_raises_comm_error():
    host, _ = make_host()
    _scripted(host, ['error'])
    _record_clicks(host)
    with pytest.raises(jp_sortie._CommError):
        host._pick_map(chapter=1, map_no=1)


# ==================== 圈循环 ====================

def _stub_run_one(host, behavior):
    rounds_run = []

    def _fake(round_no, rounds, chapter, map_no, team_no, watch,
              stop_on_fatigue, fatigue_threshold):
        rounds_run.append(round_no)
        behavior(round_no)
    host._run_one = _fake
    host._preflight = lambda: []
    host.record_event = lambda *a, **k: None
    host.classify = lambda image: 'home'   # 起跑画面在本丸，跳过启动接续守卫
    return rounds_run


def test_run_loops_all_rounds():
    host, logs = make_host()
    rounds_run = _stub_run_one(host, lambda round_no: None)
    host.run(chapter=1, map_no=1, team_no=1, rounds=3)
    assert rounds_run == [1, 2, 3]
    text = "\n".join(logs)
    assert '第 3/3 圈完成' in text
    assert '共跑 3 圈' in text


def test_run_retries_round_once_after_comm_error():
    host, _ = make_host()
    failed = {'done': False}

    def _behavior(round_no):
        if not failed['done']:
            failed['done'] = True
            raise jp_sortie._CommError()
    rounds_run = _stub_run_one(host, _behavior)
    host._recover_error = lambda: False    # 自愈后本轮重跑
    host.run(chapter=1, map_no=1, team_no=1, rounds=1)
    assert rounds_run == [1, 1]


def test_run_skips_retry_when_error_recovery_already_collected_round():
    host, _ = make_host()
    def _behavior(round_no):
        raise jp_sortie._CommError()
    rounds_run = _stub_run_one(host, _behavior)
    host._recover_error = lambda: True     # 瞬断回本丸，圈已收完
    host.run(chapter=1, map_no=1, team_no=1, rounds=1)
    assert rounds_run == [1]


def test_run_gives_up_when_comm_error_repeats():
    host, _ = make_host()
    def _behavior(round_no):
        raise jp_sortie._CommError()
    _stub_run_one(host, _behavior)
    host._recover_error = lambda: False
    with pytest.raises(jp_sally.SallyStop, match='通信错误反复出现'):
        host.run(chapter=1, map_no=1, team_no=1, rounds=1)


def test_run_boots_first_when_starting_on_boot_screen():
    """起跑撞上启动途中（音量注意/标题页/读条）先走启动序列接续。"""
    host, _ = make_host()
    _stub_run_one(host, lambda round_no: None)
    host.classify = lambda image: 'boot_arrow'
    booted = []
    host._boot_to_home = lambda: booted.append(True)
    host.run(chapter=1, map_no=1, team_no=1, rounds=1)
    assert booted == [True]


# ==================== 委托幂等 ====================

def _stub_departure(host, automarch_active):
    calls = {'enable': 0}
    host._goto_sortie = lambda: 'team'
    host._pick_map = lambda chapter, map_no: None
    host._select_team = lambda team_no: None
    host._automarch_active = lambda image: automarch_active
    host._enable_automarch = lambda: calls.__setitem__(
        'enable', calls['enable'] + 1)
    host._depart = lambda: None
    host._watch_march = lambda: None
    return calls


def test_run_one_skips_enable_when_automarch_already_active():
    host, logs = make_host()
    calls = _stub_departure(host, automarch_active=True)
    host._run_one(1, 1, 1, 1, 2, None, True, 30)
    assert calls['enable'] == 0
    assert '自动行军委托已开着' in '\n'.join(logs)


def test_run_one_enables_automarch_when_not_active():
    host, _ = make_host()
    calls = _stub_departure(host, automarch_active=False)
    host._run_one(1, 1, 1, 1, 2, None, True, 30)
    assert calls['enable'] == 1


# ==================== 通信错误自愈分派 ====================

def test_recover_error_home_means_round_collected():
    host, _ = make_host()
    host.classify = lambda image: 'home'
    assert host._recover_error() is True


def test_recover_error_popup_clicks_reload_and_reboots(monkeypatch):
    host, _ = make_host()
    host.classify = lambda image: 'error'
    clicked = []
    host._click_hit = lambda template, what, image=None: clicked.append(template)
    booted = []
    host._boot_to_home = lambda: booted.append(True)
    assert host._recover_error() is False
    assert clicked == ['error/reload']
    assert booted == [True]
    assert host.device.reloaded == 0   # 弹窗版错误页点按钮，不整页刷新


def test_recover_error_full_page_reloads_and_reboots():
    host, _ = make_host()
    host.classify = lambda image: 'error_full'
    booted = []
    host._boot_to_home = lambda: booted.append(True)
    assert host._recover_error() is False
    assert host.device.reloaded == 1   # 整页版没有按钮，只能整页刷新
    assert booted == [True]


def test_recover_error_unknown_landing_stops():
    host, _ = make_host()
    host.classify = lambda image: 'map'
    with pytest.raises(jp_sally.SallyStop):
        host._recover_error()


# ==================== 疲劳停止 ====================

def test_check_fatigue_stops_below_threshold():
    host, _ = make_host()
    latest = (1000.0, _party_payload(fatigue=20))
    with pytest.raises(jp_sally.SallyStop, match='气力'):
        host._check_fatigue(latest, team_no=1, threshold=30)


def test_check_fatigue_passes_above_threshold():
    host, _ = make_host()
    latest = (1000.0, _party_payload(fatigue=20))
    host._check_fatigue(latest, team_no=1, threshold=10)   # 不该停


def test_check_fatigue_unknown_fatigue_does_not_block():
    host, _ = make_host()
    latest = (1000.0, _party_payload(fatigue=None))
    host._check_fatigue(latest, team_no=1, threshold=30)   # 读不到不拦


def test_check_fatigue_unrecognizable_payload_does_not_block():
    host, _ = make_host()
    host._check_fatigue((1000.0, {'party': {}}), team_no=1, threshold=30)
