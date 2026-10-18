"""Battle decisions and modal clicks, with no device interaction."""
from unittest.mock import Mock, patch

import pytest

from touken.flow_control import FlowAborted
from touken.flows.battle import BattleMixin
from touken.flows.sortie import SortieMixin
from touken.maa_adapter import Point
from test_battle_components import FakeMaa
from test_osaka_flow import OsakaFlow
from test_game_state import team_events, battle_result


WARNINGS = ["队中有重伤的刀剑，确认继续进军吗？点击否可以取消",
            "刀剑破坏的后果非常严重，您可能会永远失去忠诚的刀剑男士，确认继续进军吗？"]
NO = Point(785, 468)
YES = Point(495, 468)
HOME = Point(1207, 448)
MARCH = Point(1125, 592)


def tokens(text, point=Point(600, 320)):
    return [(text, point)] if text else []


class ReturnFlow(SortieMixin, BattleMixin):
    def __init__(self, maa):
        self.maa = maa
        self._watch_battle_frame = Mock()


RETURN_CFG = {"march_stop_button": {"template": "return"},
              "return_home_confirm": {"template": "yes"},
              "home_ui": {"template": "home"}}


@pytest.mark.parametrize("warning", WARNINGS)
def test_warning_cancellation_never_uses_background_yes_or_return(warning):
    maa = FakeMaa(templates={"return": HOME, "yes": YES},
                  ocr_tokens=[tokens(warning), tokens("否", NO), []])
    flow = ReturnFlow(maa)
    with patch("touken.flows.battle.time.sleep"):
        assert flow._deny_heavy_injury_warning({})
    assert maa.clicks == [NO]


@pytest.mark.parametrize("button", [[], tokens("是", YES), tokens("否", YES)])
def test_unreadable_or_wrong_side_cancel_stops_without_clicking(button):
    maa = FakeMaa(templates={"yes": YES}, ocr_tokens=[tokens(WARNINGS[1]), button])
    with pytest.raises(FlowAborted, match="停止点击"):
        ReturnFlow(maa)._deny_heavy_injury_warning({})
    assert maa.clicks == []


def test_warning_that_does_not_close_has_bounded_cancel_attempts():
    maa = FakeMaa(ocr_tokens=[tokens(WARNINGS[0])] +
                  [part for _ in range(3) for part in (tokens("否", NO), tokens(WARNINGS[1]))])
    with patch("touken.flows.battle.time.sleep"), pytest.raises(FlowAborted):
        ReturnFlow(maa)._deny_heavy_injury_warning({})
    assert maa.clicks == [NO] * 3


def test_return_home_cancels_warning_before_touching_background_button():
    maa = FakeMaa(templates={"return": HOME, "yes": YES, "home": Point(10, 10)},
                  ocr_tokens=[tokens(WARNINGS[1]), tokens("否", NO), [],
                              tokens("是否返回本丸？"), tokens("是", YES)])
    with patch("touken.flows.sortie.time.sleep"):
        assert ReturnFlow(maa)._return_home_from_march(RETURN_CFG)
    assert maa.clicks == [NO, HOME, YES]


@pytest.mark.parametrize("text", ["", "是否使用资源？", WARNINGS[0], WARNINGS[1]])
def test_return_home_never_confirms_a_different_or_unknown_dialog(text):
    maa = FakeMaa(templates={"return": HOME, "yes": YES},
                  ocr_tokens=[[], tokens(text), tokens(text), tokens("否", NO), []])
    with patch("touken.flows.sortie.time.sleep"):
        assert not ReturnFlow(maa)._return_home_from_march(RETURN_CFG)
    assert YES not in maa.clicks


@pytest.mark.parametrize("tokens_list,expected", [
    ([("重", Point(270, 310)), ("伤", Point(270, 330))], "重伤"),
    ([("重", Point(270, 310)), ("伤", Point(270, 430))], None),
    ([("重", Point(100, 310)), ("伤", Point(270, 330))], None),
    ([("伤", Point(270, 330))], None),
])
def test_fragmented_injury_stamp_requires_two_adjacent_characters(tokens_list, expected):
    assert ReturnFlow(FakeMaa(ocr_tokens=[tokens_list]))._team_injury_status({}) == expected


@pytest.mark.parametrize("at_floor_end", [False, True])
def test_osaka_uses_current_battle_heavy_status_when_screen_misses_it(at_floor_end):
    flow = OsakaFlow([at_floor_end])
    flow._combined_injury_status = BattleMixin._combined_injury_status.__get__(flow)
    flow._log_injury_status = BattleMixin._log_injury_status.__get__(flow)
    flow._injury_reaches_threshold = BattleMixin._injury_reaches_threshold
    flow._team_injury_status = Mock(side_effect=AssertionError("fresh facts must take priority"))
    flow._return_home_from_march = Mock(return_value=True)
    flow.find_march_seq = [MARCH]
    events = team_events()
    events[0]["payload"]["party"]["3"] = events[0]["payload"]["party"].pop("1")
    events.append(battle_result(hp=9, maximum=45, status=3))
    with patch("touken.game_state.read_events", return_value=events), \
         patch("touken.game_state.time.time", return_value=1002), \
         patch("touken.flows.osaka.time.sleep"), \
         patch("touken.injury_recovery.current_team_injury", return_value=(True, "重伤")):
        list(flow._osaka_stream_impl(max_floors=3, injury_action="stop"))
    flow._return_home_from_march.assert_called_once()
    assert flow.maa.clicks == 0


def test_osaka_each_decision_gets_a_new_observation_cache():
    flow = OsakaFlow([])
    caches = []
    def observe(cfg, team_no, cache):
        caches.append(cache)
        assert "events" not in cache
        cache["events"] = ["current battle"]
        return (None, "全员满血") if len(caches) == 1 else ("重伤", "不动行光重伤(hp9/45)")
    flow._combined_injury_status = observe
    assert flow._osaka_injury_status({}, 3) is None
    assert flow._osaka_injury_status({}, 3) == "重伤"
    assert caches[0] is not caches[1]


@pytest.mark.parametrize("repeated_floor", [False, True])
def test_osaka_rechecks_after_waiting_and_before_repeated_floor_click(repeated_floor):
    flow = OsakaFlow([True, True])
    flow._injury_reaches_threshold = BattleMixin._injury_reaches_threshold
    flow._combined_injury_status = Mock(side_effect=
        [(None, "全员满血")] * (2 if repeated_floor else 1) + [("重伤", "不动行光重伤(hp9/45)")])
    flow.wait_march_result = MARCH
    flow.find_march_seq = [MARCH]
    flow._return_home_from_march = Mock(return_value=True)
    with patch("touken.flows.osaka.time.sleep"), patch(
            "touken.injury_recovery.current_team_injury", return_value=(True, "重伤")):
        list(flow._osaka_stream_impl(max_floors=3, injury_action="stop"))
    assert flow.maa.clicks == int(repeated_floor)
    flow._return_home_from_march.assert_called_once()
    assert len([e for e in flow.events if e["event_type"] == "osaka.floor_completed"]) == 1


@pytest.mark.parametrize("before_march", [False, True])
def test_osaka_intercepted_warning_is_never_reported_as_complete(before_march):
    flow = OsakaFlow([])
    flow._deny_heavy_injury_warning = Mock(side_effect=[False, True] if before_march else [True])
    flow._return_home_from_march = Mock(return_value=True)
    flow.find_march_seq = [MARCH]
    with patch("touken.flows.osaka.time.sleep"), pytest.raises(FlowAborted, match="剩余 3 层未完成"):
        list(flow._osaka_stream_impl(max_floors=3))
    assert flow.maa.clicks == 0
    flow._return_home_from_march.assert_called_once()
