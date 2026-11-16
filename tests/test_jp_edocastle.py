# -*- coding: utf-8 -*-
"""日服江户城宿主（jp_edocastle）：参数校验、画面分类表、素材体检。

画面导航/点击不走真设备，素材体检用 monkeypatch 模拟模板缺失
（模板已于 2026-11-10 标定入仓）——宿主必须把缺什么列清楚然后停，绝不盲点。
"""
import pytest

from touken import jp_edocastle, jp_sally, jp_vision


class FakeDevice:
    base_size = (1508, 1088)

    def __init__(self):
        self.clicks = []

    def screenshot(self, force=False):
        return object()

    def click(self, point):
        self.clicks.append(point)
        return True


def make_host(params=None):
    logs = []
    host = jp_edocastle.JpEdocastle(FakeDevice(), params or {}, log=logs.append,
                                    save_debug=lambda img: None)
    return host, logs


def test_screens_table_inserts_event_screens_and_swaps_team_anchor():
    names = [name for name, _ in jp_edocastle.SCREENS]
    # 覆盖层优先级保持 Sally 前 6 条
    assert names[:6] == [name for name, _ in jp_sally.SCREENS[:6]]
    # 活动页、出阵确认窗、错误页（弹窗版+整页版）、分歧点都在表里
    assert 'edo' in names and 'edo_go' in names and 'error' in names
    assert 'error_full' in names
    assert 'edo_branch' in names
    # team 屏锚点已换成江户城自己的标题
    team = dict(jp_edocastle.SCREENS)['team']
    assert team == ('edo/team-screen',)
    # 出阵确认窗是双模板强信号
    assert dict(jp_edocastle.SCREENS)['edo_go'] == (
        'edo/confirm-go', 'edo/confirm-text')


def test_run_validates_params_before_touching_device():
    host, _ = make_host()
    with pytest.raises(ValueError):
        host.run(team_no=0, difficulty=1, rounds=1)
    with pytest.raises(ValueError):
        host.run(team_no=6, difficulty=1, rounds=1)
    with pytest.raises(ValueError):
        host.run(team_no=1, difficulty=0, rounds=1)
    with pytest.raises(ValueError):
        host.run(team_no=1, difficulty=5, rounds=1)
    with pytest.raises(ValueError):
        host.run(team_no=1, difficulty=1, rounds=0)
    with pytest.raises(ValueError):
        host.run(team_no=1, difficulty=1, rounds=51)


@pytest.fixture
def templates_missing(monkeypatch):
    def _boom(name):
        raise jp_vision.TemplateNotFound(f'{name}（模拟缺失）')
    monkeypatch.setattr(jp_vision, 'load_template', _boom)


def test_preflight_lists_every_missing_template(templates_missing):
    host, _ = make_host()
    missing = host._preflight()
    assert len(missing) == len(jp_edocastle._REQUIRED_TEMPLATES)
    assert all(('edo/' in item or 'error/' in item) for item in missing)


def test_run_reports_missing_templates_and_stops(templates_missing):
    host, logs = make_host()
    with pytest.raises(jp_sally.SallyStop):
        host.run(team_no=1, difficulty=1, rounds=1)
    text = "\n".join(logs)
    assert '没标定' in text
    for name in jp_edocastle._REQUIRED_TEMPLATES:
        assert name in text
    # 模板不全时一个像素都不许点
    assert host.device.clicks == []


def test_run_script_rejects_bad_params():
    with pytest.raises(ValueError):
        list(jp_edocastle.run_script(params={'team_no': 'abc'}))
    with pytest.raises(ValueError):
        list(jp_edocastle.run_script(params={'difficulty': 'x'}))
    with pytest.raises(ValueError):
        list(jp_edocastle.run_script(params={'rounds': 'many'}))


# ---- 行军盯梢：新刀获得剧情帮点翻页（2026-11-17 卡 8 分钟事故的修复）----

@pytest.fixture
def fast_watch(monkeypatch):
    monkeypatch.setattr(jp_vision, 'base_size', lambda: (1508, 1088))
    monkeypatch.setattr(jp_sally.time, 'sleep', lambda s: None)
    monkeypatch.setattr(jp_edocastle.time, 'sleep', lambda s: None)


def test_watch_run_taps_through_story(fast_watch):
    """行军中插入新刀剧情：帮点翻页，剧情过后到分歧点/活动页正常收圈。"""
    host, logs = make_host()
    host.poll = 0.01
    host._settle_branch = lambda: None     # 分歧点收圈另测，这里只盯翻页
    screens = iter(['story', 'story', 'story', 'edo_branch'])
    host.classify = lambda image: next(screens, 'edo_branch')
    host._watch_run()
    assert len(host.device.clicks) == 3
    expected = (int(1508 * jp_sally.STORY_TAP_AT[0]),
                int(1088 * jp_sally.STORY_TAP_AT[1]))
    assert all((p.x, p.y) == expected for p in host.device.clicks)
    assert any('帮点翻页' in line for line in logs)


def test_watch_run_never_ending_story_stops(fast_watch, monkeypatch):
    """剧情点了很多下都翻不完：安全停车，不无限点下去。"""
    monkeypatch.setattr(jp_sally, 'STORY_TAP_LIMIT', 3)
    host, _ = make_host()
    host.poll = 0.01
    host.classify = lambda image: 'story'
    with pytest.raises(jp_sally.SallyStop, match='翻不完'):
        host._watch_run()
    assert len(host.device.clicks) == 3


# ---- 分歧点收圈：はい 被画布重绘吞掉就补点（2026-11-17 实测连吞两次）----

def _stub_settle(host):
    from touken.device_adapter import Point
    host._click_hit = lambda *a, **k: host.device.click(Point(0, 0))
    host._wait = lambda *a, **k: None


def test_settle_branch_retries_eaten_yes_click(fast_watch):
    """はい 第一次没点上（确认窗还在）：补点一次，正常收圈。"""
    host, logs = make_host()
    _stub_settle(host)
    screens = iter(['dialog', 'edo'])   # 第一次还在，第二次没了
    host.classify = lambda image: next(screens, 'edo')
    host._settle_branch()
    # 回城按钮 1 次 + はい 2 次
    assert len(host.device.clicks) == 3
    assert any('补点はい' in line for line in logs)


def test_settle_branch_gives_up_after_three_misses(fast_watch):
    """はい 连点三次确认窗都不消：安全停车，不无限点下去。"""
    host, _ = make_host()
    _stub_settle(host)
    host.classify = lambda image: 'dialog'
    with pytest.raises(jp_sally.SallyStop, match='没消'):
        host._settle_branch()
    assert len(host.device.clicks) == 4  # 回城 1 次 + はい 3 次
