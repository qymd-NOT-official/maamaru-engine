"""新版江户城结算与过渡页：不把活动标题当回城，不把钥匙总数当单圈收益。"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from touken.device_adapter import Point
from touken.flows.edocastle import EdocastleMixin
from touken.flow_control import FlowAborted
from test_edocastle_auto_march import drain


ROWS = {
    (800, 285, 480, 80): [('所持钥匙', Point(916, 323)), ('39', Point(1131, 324))],
    (1040, 505, 180, 185): [('继续', Point(1126, 566)), ('调查', Point(1127, 620))],
    (1145, 380, 135, 145): [('返回', Point(1207, 428)), ('本丸', Point(1209, 471))],
    (980, 380, 140, 145): [('部队', Point(1044, 429)), ('恢复', Point(1043, 470))],
}


class SettlementDevice:
    def __init__(self, rows=None, state='settlement', yes=True, home=True):
        self.rows = deepcopy(ROWS if rows is None else rows)
        self.state = state
        self.yes = yes
        self.home = home
        self.clicks = []

    def screenshot(self, force=False):
        pass

    def ocr_all(self, roi):
        return self.rows.get(tuple(roi.to_list()), []) if self.state == 'settlement' else []

    def ocr(self, text, roi=None, match_mode='contains'):
        if self.state == 'return-confirm':
            if text == '确认返回本丸':
                return Point(640, 323)
            if text == '是' and self.yes:
                assert roi.to_list() == [395, 430, 205, 80]
                assert match_mode == 'exact'
                return Point(496, 468)
        return None

    def click(self, point):
        self.clicks.append(point)
        if self.state == 'settlement':
            assert point == Point(1208, 449)
            self.state = 'return-confirm'
        elif self.state == 'return-confirm':
            assert point == Point(496, 468)
            self.state = 'home' if self.home else 'unknown'
        else:
            raise AssertionError('未知页面不许点击')

    def template_match(self, template, **kwargs):
        return Point(42, 53) if self.state == 'home' and template == 'home' else None


def host(device=None):
    result = EdocastleMixin()
    result.device = device or SettlementDevice()
    result.config = {'sortie': {'home_ui': {'template': 'home'}}}
    result.current_location = '出阵'
    return result


def test_new_settlement_reads_total_and_returns_via_confirmed_buttons(monkeypatch):
    flow = host()
    monkeypatch.setattr('touken.flows.edocastle.time.sleep', lambda _: None)
    assert flow._read_edocastle_settlement()['keys'] == 39
    assert flow._return_from_edocastle_settlement({})
    assert flow.device.clicks == [Point(1208, 449), Point(496, 468)]
    assert flow.current_location == '本丸'


@pytest.mark.parametrize('missing', list(ROWS))
def test_incomplete_settlement_is_not_accepted_or_clicked(monkeypatch, missing):
    rows = deepcopy(ROWS)
    del rows[missing]
    flow = host(SettlementDevice(rows))
    assert flow._read_edocastle_settlement() is None
    assert not flow._return_from_edocastle_settlement({})
    assert flow.device.clicks == []


@pytest.mark.parametrize('yes,home', [(False, True), (True, False)])
def test_return_must_confirm_yes_and_actual_home(monkeypatch, yes, home):
    flow = host(SettlementDevice(yes=yes, home=home))
    monkeypatch.setattr('touken.flows.edocastle.time.sleep', lambda _: None)
    assert not flow._return_from_edocastle_settlement({})
    assert flow.current_location == '出阵'
    assert len(flow.device.clicks) == (1 if not yes else 2)


def test_unclear_key_number_stays_unknown_but_can_return():
    rows = deepcopy(ROWS)
    rows[(800, 285, 480, 80)] = [('所持钥匙', Point(916, 323)), ('?', Point(1131, 324))]
    assert host(SettlementDevice(rows))._read_edocastle_settlement()['keys'] is None


def test_transition_title_is_ignored_until_new_settlement(monkeypatch):
    flow = host(SettlementDevice(state='entry'))
    frames = iter(['entry', 'map', 'old-end', 'settlement'])
    flow.device.screenshot = lambda **kwargs: setattr(flow.device, 'state', next(frames))
    # 起手旧活动标题即使命中，也不应被当成任务已经结束。
    flow.device.template_match = lambda template: (
        Point(570, 85) if template in ('entry', flow.device.state) else None)
    flow._deny_heavy_injury_warning = lambda cfg: False
    monkeypatch.setattr('touken.flows.edocastle.time.sleep', lambda _: None)
    _, result = drain(flow._watch_edocastle_auto_march({
        'round_end': {'template': 'old-end'}, 'ui_title': {'template': 'entry'}}))
    assert result == (0, True)
    assert flow._edocastle_settlement['keys'] == 39
    assert flow.device.clicks == []


@pytest.mark.parametrize('auto_march', [True, False])
def test_new_settlement_accounting_reopens_entry_for_next_round(monkeypatch, auto_march):
    flow = host()
    flow.config.update({'edocastle': {'skip_tap': [775, 695], 'map_archive': 'unused.json'},
                        'team_select': {'teams': {'2': [274, 91]}}})
    monkeypatch.setattr('touken.flows.edocastle.load_archive', lambda _: {})
    openings = []

    def opened(cfg):
        openings.append(1)
        flow.current_location = '出阵'
        yield '入口已确认'
        return True

    def entered(*args, **kwargs):
        yield '出阵已确认'
        return True, False

    totals = iter([39, 54])

    def watched(cfg):
        flow._edocastle_settlement = {'keys': next(totals), 'home': Point(1208, 449)}
        yield '结算已确认'
        return 0, True

    def returned(cfg):
        flow.current_location = '本丸'
        return True

    flow._open_edocastle_entry = opened
    flow._enter_map_stream = entered
    flow._watch_edocastle_auto_march = watched
    flow._map_run_stream = lambda *args: iter_manual()

    def iter_manual():
        yield '脚本巡游完成'
        return 0, True

    def wait_manual(*args, **kwargs):
        drain(watched({}))
        return True
    flow._return_from_edocastle_settlement = returned
    flow.set_progress = Mock()
    flow._expedition_takeover_requested = lambda: False
    flow._read_key_total = Mock(side_effect=[22, 39])
    flow.record_event = Mock()
    flow._wait_entry_screen = Mock(side_effect=(
        AssertionError('自动行军应直接验收新结算') if auto_march else wait_manual))
    list(flow.edocastle_stream(team_no=2, max_runs=2, auto_march=auto_march))
    assert len(openings) == 2
    assert [call.kwargs['keys'] for call in flow.record_event.call_args_list] == [17, 15]
    assert flow._wait_entry_screen.call_count == (0 if auto_march else 2)
    assert flow._preserve_game_scene is False


def test_manual_end_banner_waits_for_new_settlement_without_skipping(monkeypatch):
    flow = host(SettlementDevice(state='old-end'))
    frames = iter(['old-end', 'settlement'])
    flow.device.screenshot = lambda **kwargs: setattr(flow.device, 'state', next(frames))
    monkeypatch.setattr('touken.flows.edocastle.time.sleep', lambda _: None)
    assert flow._wait_entry_screen({'ui_title': {'template': 'entry'}}, [775, 695])
    assert flow._edocastle_settlement['keys'] == 39
    assert flow.device.clicks == []


def test_exception_during_auto_departure_already_protects_scene():
    flow = host()
    flow.config.update({'edocastle': {'skip_tap': [775, 695]},
                        'team_select': {'teams': {'2': [274, 91]}}})

    def opened(cfg):
        yield '入口已确认'
        return True

    flow._open_edocastle_entry = opened
    flow.set_progress = Mock()
    flow._expedition_takeover_requested = lambda: False
    flow._read_key_total = lambda cfg: 39
    flow._enter_map_stream = Mock(side_effect=RuntimeError('frame unavailable'))
    with pytest.raises(RuntimeError, match='frame unavailable'):
        list(flow.edocastle_stream(team_no=2, max_runs=1, auto_march=True))
    assert flow._preserve_game_scene is True


def test_abort_propagates_through_cleanup_without_navigation(monkeypatch, tmp_path):
    monkeypatch.setenv('MAAMARU_DATA_DIR', str(tmp_path))
    from panel import server
    agent = SimpleNamespace(navigate_to_stream=Mock(), current_location='出阵')

    def runner(agent, *args):
        agent._preserve_game_scene = True
        yield '结果未确认，停止任务'
        raise FlowAborted('保留现场')

    with patch.object(server, '_make_agent', return_value=agent), \
            patch.object(server, '_load_panel_settings', return_value={}):
        with pytest.raises(FlowAborted):
            list(server._wrap_inventory('江户城', runner)('unused', {}))
    agent.navigate_to_stream.assert_not_called()
