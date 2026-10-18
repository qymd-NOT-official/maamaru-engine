"""Floor-end upkeep uses current game facts and preserves the running goal."""
from unittest.mock import Mock, patch

import pytest

from touken.flow_control import FlowAborted
from touken.flows.osaka import OsakaMixin, _OsakaResume, maintenance_reasons
from touken.game_state import injury_report, team_maintenance_facts
from test_game_state import event
from test_osaka_flow import OsakaFlow


def battle_facts(*, complete=True, enemy=False, equipment=True):
    swords = {str(slot): {"sword_id": 118, "hp": 45, "hp_max": 45,
                          "fatigue": 80, "equip_serial_id1": slot * 10,
                          "equip_serial_id2": None, "equip_serial_id3": None}
              for slot in range(1, 7)}
    roster = {str(slot): {"serial_id": str(slot)} for slot in range(1, 7)}
    first = event({"sword": swords, "party": {"3": {"slot": roster}}}, "/party/list", 1000)
    slots = {slot: {**row, **swords[slot], "status": 0} for slot, row in roster.items()}
    slots["2"]["fatigue"] = 30
    slots["4"]["equip_serial_id1"] = None
    if not equipment:
        for row in slots.values():
            for number in (1, 2, 3):
                row.pop(f"equip_serial_id{number}")
    if not complete:
        del slots["6"]
    latest = event({"result": {"enemy" if enemy else "player": {"party": {"slot": slots}}}},
                   "/battle/battle", 2000)
    return [first, latest]


def snapshot(*, low=False, broken=False):
    return {"fatigue": {1: 80, 2: 30 if low else 80},
            "equipment": {"11": (not broken, True, False), "22": (True, False, False)}}


def test_complete_current_battle_renews_team_after_a_long_sortie():
    events = battle_facts()
    assert injury_report(events, 3, now=2001) is not None
    facts = team_maintenance_facts(events, 3, now=2001)
    assert facts["fatigue"] == {1: 80, 2: 30, 3: 80, 4: 80, 5: 80, 6: 80}
    assert facts["equipment"]["4"] == (False, False, False)


@pytest.mark.parametrize("kwargs", [{"complete": False}, {"enemy": True}])
def test_partial_or_enemy_roster_cannot_refresh_old_team_binding(kwargs):
    assert team_maintenance_facts(battle_facts(**kwargs), 3, now=2001) is None


def test_missing_current_equipment_never_reuses_old_equipped_slots():
    facts = team_maintenance_facts(battle_facts(equipment=False), 3, now=2001)
    assert facts["equipment"] is None
    assert facts["fatigue"][2] == 30


def test_new_battle_request_invalidates_previous_maintenance_observation():
    events = battle_facts() + [event({}, "/battle/battle", 2002)]
    assert team_maintenance_facts(events, 3, now=2003) is None


@pytest.mark.parametrize("rotate,equip,expected", [
    (True, True, ["换队长", "补刀装"]), (False, True, ["补刀装"]),
    (True, False, ["换队长"]), (False, False, []),
])
def test_only_enabled_maintenance_is_requested(rotate, equip, expected):
    assert maintenance_reasons(snapshot(low=True, broken=True), rotate_captain=rotate,
        margin=10, auto_equip=equip, equipment_baseline=snapshot()["equipment"]) == expected


def test_small_fatigue_gap_and_intact_equipment_need_no_trip_home():
    facts = snapshot()
    facts["fatigue"][2] = 75
    assert maintenance_reasons(facts, rotate_captain=True, margin=10,
        auto_equip=True, equipment_baseline=snapshot()["equipment"]) == []


def test_unknown_facts_and_changed_roster_cannot_invent_equipment_loss():
    for facts in (None, {"fatigue": None, "equipment": None},
                  {"fatigue": None, "equipment": {"different": (False, False, False)}}):
        assert maintenance_reasons(facts, rotate_captain=True, margin=10,
            auto_equip=True, equipment_baseline=snapshot()["equipment"]) == []


@pytest.mark.parametrize("select_floor", [False, True])
def test_floor_maintenance_returns_home_and_finishes_only_remaining_floors(select_floor):
    flow = OsakaFlow([True, True])
    flow._read_osaka_floor = Mock(return_value=81)
    flow._select_osaka_floor = Mock(return_value=81)
    flow._return_home_from_march = Mock(return_value=True)
    calls = []
    def depart(cfg, team_no, tag, **kwargs):
        calls.append(kwargs)
        yield from ()
        return True, True
    flow._safe_depart_stream = depart
    with patch("touken.flows.osaka.time.sleep"), patch(
            "touken.flows.osaka.team_maintenance_facts",
            side_effect=[snapshot(), snapshot(low=True, broken=True), snapshot()]):
        logs = list(flow.osaka_stream(max_floors=2, select_floor=select_floor,
                                     target_floor=81, rotate_captain=True))
    assert len(calls) == 2
    assert all(call["rotate_captain"] for call in calls)
    assert calls[1]["team_record_saved"]
    assert flow._osaka_floors_total == 2
    assert flow._return_home_from_march.call_count == 2
    assert flow.maa.clicks == 0
    assert any("继续剩余 1 层" in message for message in logs)
    assert [e["payload"]["completed"] for e in flow.events if e["event_type"] == "osaka.floor_completed"] == [1, 2]
    assert flow._select_osaka_floor.call_count == (2 if select_floor else 0)


@pytest.mark.parametrize("after", [None, snapshot(low=True, broken=True)])
def test_unconfirmed_maintenance_stops_before_confirming_departure(after):
    flow = OsakaFlow([True])
    flow._return_home_from_march = Mock(return_value=True)
    flow._confirm_departure = Mock(return_value=True)
    with patch("touken.flows.osaka.time.sleep"), patch(
            "touken.flows.osaka.team_maintenance_facts",
            side_effect=[snapshot(), snapshot(low=True, broken=True), after]), \
         pytest.raises(FlowAborted, match="不重复往返"):
        list(flow.osaka_stream(max_floors=3, rotate_captain=True))
    flow._return_home_from_march.assert_called_once()
    flow._confirm_departure.assert_called_once()


def test_no_maintenance_after_last_requested_floor():
    flow = OsakaFlow([True])
    flow._return_home_from_march = Mock(return_value=True)
    with patch("touken.flows.osaka.time.sleep"), patch(
            "touken.flows.osaka.team_maintenance_facts", return_value=snapshot(low=True)) as read:
        list(flow.osaka_stream(max_floors=1, rotate_captain=True))
    assert read.call_count == 1
    flow._return_home_from_march.assert_called_once()


def test_continuations_keep_selected_floor_and_do_not_grow_the_python_stack():
    flow = OsakaFlow([])
    continuation = _OsakaResume({"max_floors": 1, "_completed_floors": 1})
    flow._osaka_stream_impl = Mock(side_effect=[iter_return(continuation) for _ in range(1100)] + [iter_return(None)])
    list(flow.osaka_stream(max_floors=2, select_floor=True, target_floor=81))
    assert flow._osaka_stream_impl.call_count == 1101
    last = flow._osaka_stream_impl.call_args.kwargs
    assert last["target_floor"] == 81 and last["select_floor"]
    assert last["max_floors"] == 1 and last["_completed_floors"] == 1


def iter_return(value):
    yield from ()
    return value


def test_equipment_restore_rotates_again_and_updates_record_after_order_reset():
    flow = OsakaFlow([])
    flow._rotate_captain_here = Mock(return_value=iter_return(None))
    flow._save_team_record = Mock(return_value=True)
    def depart(cfg, team_no, tag, **kwargs):
        assert (yield from kwargs["after_equipment_stream"]())
        return False, True
    flow._safe_depart_stream = depart
    list(flow.osaka_stream(rotate_captain=True))
    flow._rotate_captain_here.assert_called_once_with(10, team_no=3)
    flow._save_team_record.assert_called_once()
