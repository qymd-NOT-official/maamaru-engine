"""江户城新出阵窗：正文与按钮同帧验收，兼容旧配置，不点击调查道具。"""
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from touken.device_adapter import Point
from touken.flows.battle import BattleMixin
from touken.flows.edocastle import EdocastleMixin


OLD_CONFIG = {
    'difficulty_card': {'target': [1118, 328]},
    'confirm_ui': {'template': 'team/ui出阵确认.png'},
    'confirm_button': {'template': '通用_确定.png', 'roi': [536, 565, 742, 637]},
}


class DialogDevice:
    def __init__(self, states=(('body', 'button'),)):
        self.states = states
        self.frame = -1
        self.clicks = []
        self.reads = []

    def screenshot(self, force=False):
        if force:
            self.frame += 1

    def ocr(self, expected, roi=None, match_mode='contains'):
        self.reads.append((expected, roi.to_list(), match_mode))
        state = self.states[min(self.frame, len(self.states) - 1)]
        if expected == '出阵会消耗通行令牌' and 'body' in state:
            assert roi.to_list() == [340, 150, 620, 55]
            return Point(640, 182)
        if expected == '确定' and 'button' in state:
            assert roi.to_list() == [540, 580, 200, 75]
            assert match_mode == 'exact'
            return Point(640, 614)
        return None

    def template_match(self, *args, **kwargs):
        raise AssertionError('新确认识别不应再使用旧弹窗或按钮模板')

    def click(self, point):
        self.clicks.append(point)


class Host(EdocastleMixin, BattleMixin):
    def __init__(self, device=None, safe=True):
        self.device = device or DialogDevice()
        self.config = {}
        self.taps = []
        self.safe = safe
        self._wait_for_team_select = Mock(return_value=True)
        self._wait_entry_map_or_formation = Mock(return_value='map')

    def _click_point(self, point):
        self.taps.append(point)

    def _safe_depart_stream(self, cfg, *args, **kwargs):
        self.departure_cfg = cfg
        yield '安全出阵检查'
        return self.safe, False


def enter(host):
    cfg = deepcopy(OLD_CONFIG)
    before = deepcopy(cfg)
    with patch('touken.flows.edocastle.time.sleep'):
        flow = host._enter_map_stream(cfg, 2, [640, 400], 'heavy', False, False)
        messages = []
        while True:
            try:
                messages.append(next(flow))
            except StopIteration as stop:
                result = stop.value
                break
    assert cfg == before
    return result, messages


def test_old_config_recognizes_new_dialog_and_only_clicks_confirm_button():
    host = Host()
    result, _ = enter(host)
    assert result[0]
    assert host.taps == [[1118, 328]]
    assert host.device.clicks == [Point(640, 614)]
    host._wait_entry_map_or_formation.assert_called_once()
    assert host._match_departure_prompt(host.departure_cfg) == Point(640, 614)


@pytest.mark.parametrize('states', [
    (('body',),),                       # 只有正文，没有按钮
    (('button',),),                     # 只有确定，属于别的弹窗
    (('body',), ('button',)),           # 转场前后的识别不能拼成同一帧证据
    ((),),
])
def test_unconfirmed_dialog_never_clicks_or_enters_map(states):
    host = Host(DialogDevice(states))
    result, messages = enter(host)
    assert not result[0]
    assert host.device.clicks == []
    assert host.taps == [[1118, 328]]
    host._wait_entry_map_or_formation.assert_not_called()
    assert any('停止点击' in message for message in messages)


def test_shared_safety_rejection_never_reaches_confirmation():
    host = Host(safe=False)
    assert not enter(host)[0][0]
    assert host.device.reads == []
    assert host.device.clicks == []
    host._wait_entry_map_or_formation.assert_not_called()


def test_ocr_button_failure_never_uses_old_coordinate_fallback():
    host = Host(DialogDevice((('body',),)))
    enter(host)
    cfg = deepcopy(host.departure_cfg)
    cfg['confirm_button']['target'] = [640, 600]
    with patch('touken.flows.battle.time.sleep'):
        assert not host._confirm_departure(cfg)
    assert host.taps == [[1118, 328]]
    assert host.device.clicks == []
