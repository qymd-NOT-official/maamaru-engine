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
    host = jp_edocastle.JpEdocastle(FakeDevice(), params or {}, log=logs.append)
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
