"""江户城委托开关：完整出发准备、只读监控、圈数和记账，以及面板接线。"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_edocastle_confirmation import Host, OLD_CONFIG
from touken.device_adapter import Point
from touken.flows.edocastle import EdocastleMixin, EDO_AUTO_MARCH_SWITCHES
from touken.flows.report_judge import _is_fail
from touken.auto_march_settings import apply_details


def drain(flow):
    messages = []
    while True:
        try:
            messages.append(next(flow))
        except StopIteration as stop:
            return messages, stop.value


class PreparingHost(Host):
    def __init__(self):
        super().__init__()
        self._configure_auto_march_settings = Mock(return_value=True)
        self._enable_auto_march = Mock(return_value=True)
        self._disable_auto_march = Mock(return_value=True)
        self.equipment_restored = False

    def _safe_depart_stream(self, cfg, *args, **kwargs):
        self.departure_cfg = cfg
        if not (yield from kwargs['prepare_team_stream']()):
            return False, False
        if self.equipment_restored and not (yield from kwargs['after_equipment_stream']()):
            return False, False
        return True, False


def enter(host, monkeypatch, auto=True):
    monkeypatch.setattr('touken.flows.edocastle.time.sleep', lambda _: None)
    return drain(host._enter_map_stream(deepcopy(OLD_CONFIG), 2, [775, 695],
                                       'heavy', False, False, auto_march=auto))


def test_enable_configures_edocastle_four_switches_before_departure(monkeypatch):
    host = PreparingHost()
    assert enter(host, monkeypatch)[1][0]
    host._configure_auto_march_settings.assert_called_once_with(
        {'heavy': False, 'equipment': False, 'fatigue': True,
         'presentation': False, 'formation': '鱼鳞阵'}, switches=EDO_AUTO_MARCH_SWITCHES)
    host._enable_auto_march.assert_called_once()
    host._disable_auto_march.assert_not_called()
    host._wait_entry_map_or_formation.assert_not_called()


def test_equipment_recovery_reapplies_delegation(monkeypatch):
    host = PreparingHost()
    host.equipment_restored = True
    assert enter(host, monkeypatch)[1][0]
    assert host._configure_auto_march_settings.call_count == 2
    assert host._enable_auto_march.call_count == 2


@pytest.mark.parametrize('failed', ['_configure_auto_march_settings', '_enable_auto_march'])
def test_unconfirmed_delegation_never_confirms_departure(monkeypatch, failed):
    host = PreparingHost()
    getattr(host, failed).return_value = False
    messages, result = enter(host, monkeypatch)
    assert not result[0]
    assert host.device.clicks == []
    assert any(_is_fail(message) for message in messages)


def test_disabled_clears_stale_game_delegation_and_keeps_manual_entry(monkeypatch):
    host = PreparingHost()
    assert enter(host, monkeypatch, auto=False)[1][0]
    host._disable_auto_march.assert_called_once()
    host._configure_auto_march_settings.assert_not_called()
    host._wait_entry_map_or_formation.assert_called_once()
    host._disable_auto_march.return_value = False
    host.device.clicks.clear()
    assert not enter(host, monkeypatch, auto=False)[1][0]
    assert host.device.clicks == []


class MonitorDevice:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.state = set()
        self.click = Mock(side_effect=AssertionError('自动行军监控不能点地图或阵形'))

    def screenshot(self, force=False):
        self.state = next(self.frames, self.state)

    def template_match(self, template, **kwargs):
        return Point(10, 10) if template in self.state else None

    def ocr(self, text, *args, **kwargs):
        return Point(10, 10) if 'stopped' in self.state else None


@pytest.mark.parametrize('frames,ok', [
    ([set(), {'end'}], True),
    ([{'stopped'}], False),
    ([{'entry'}], False),
    ([set()], False),
])
def test_monitor_requires_completion_evidence_and_never_clicks(monkeypatch, frames, ok):
    host = EdocastleMixin()
    host.config = {}
    host.device = MonitorDevice(frames)
    host._deny_heavy_injury_warning = Mock(return_value=False)
    monkeypatch.setattr('touken.flows.edocastle.time.monotonic', iter(range(100)).__next__)
    monkeypatch.setattr('touken.flows.edocastle.time.sleep', lambda _: None)
    messages, result = drain(host._watch_edocastle_auto_march({
        'round_end': {'template': 'end'}, 'ui_title': {'template': 'entry'}}, timeout_s=4))
    assert result == (0, ok)
    host.device.click.assert_not_called()
    assert any(_is_fail(message) for message in messages) is (not ok)


@pytest.mark.parametrize('succeeded', [True, False])
def test_full_auto_path_skips_old_archive_and_manual_route(monkeypatch, succeeded):
    host = EdocastleMixin()
    host.config = {'edocastle': {'ui_title': {'template': 'entry'}},
                   'team_select': {'teams': {'2': [10, 20]}}}
    host.device = SimpleNamespace(screenshot=lambda **kw: None,
                                  template_match=lambda *a, **kw: Point(1, 1))
    host.current_location = '出阵'
    host.navigate_to_stream = lambda _: iter(())
    host.set_progress = Mock()
    host._expedition_takeover_requested = lambda: False
    host._read_key_total = Mock(side_effect=[20, 27, 27, 35])
    host._wait_entry_screen = Mock(return_value=True)
    host._map_run_stream = Mock(side_effect=AssertionError('不能调用旧巡游'))
    host.record_event = Mock()

    def entered(*a, **kw):
        assert kw['auto_march'] is True
        yield '出阵已确认'
        return True, True

    def watched(*a):
        yield '调查完成' if succeeded else '调查未确认完成'
        return 0, succeeded

    host._enter_map_stream = entered
    host._watch_edocastle_auto_march = watched
    messages = list(host.edocastle_stream(team_no=2, max_runs=2, auto_march=True))
    host._map_run_stream.assert_not_called()
    if succeeded:
        assert [call.kwargs['keys'] for call in host.record_event.call_args_list] == [7, 8]
        assert not any(_is_fail(message) for message in messages)
    else:
        host.record_event.assert_not_called()
        host._wait_entry_screen.assert_not_called()
        assert any(_is_fail(message) for message in messages)


@pytest.mark.parametrize('value', [True, False])
def test_panel_and_workflow_expose_and_pass_auto_march(monkeypatch, tmp_path, value):
    monkeypatch.setenv('MAAMARU_DATA_DIR', str(tmp_path))
    from panel import server, workflow
    fields = {field['key']: field for field in server._SCRIPTS['edocastle']['params']}
    assert fields['auto_march']['default'] is True
    assert fields['formation_mode']['visibleWhen'] == {'key': 'auto_march', 'is': 'false'}
    assert any(field['key'] == 'auto_march' for field in workflow.NODE_REGISTRY['edocastle']['params'])
    host = Mock()
    host.edocastle_stream.return_value = iter(())
    list(server._build_edocastle(host, 'unused', {'team_no': '2', 'auto_march': value}))
    assert host.edocastle_stream.call_args.kwargs['auto_march'] is value


@pytest.mark.parametrize('presentation', [False, None])
def test_four_setting_readbacks_include_presentation_and_accept_title_variant(monkeypatch, presentation):
    device = Mock()
    device.ocr.side_effect = lambda text, *a, **k: Point(1, 1) if text == '详细设置' else None
    policy = {'heavy': False, 'fatigue': True, 'equipment': False,
              'presentation': False, 'formation': '鱼鳞阵'}
    reads = []

    def read(device, key, switches):
        assert switches is EDO_AUTO_MARCH_SWITCHES
        reads.append(key)
        return presentation if key == 'presentation' else policy[key]

    monkeypatch.setattr('touken.auto_march_settings.read_switch', read)
    monkeypatch.setattr('touken.auto_march_settings.selected_formation', lambda *a: True)
    assert apply_details(device, policy, switches=EDO_AUTO_MARCH_SWITCHES) is (presentation is not None)
    assert 'presentation' in reads
    device.click.assert_not_called()
