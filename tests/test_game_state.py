"""Synthetic facts: sparse updates, current identities and departure outcomes."""
from unittest.mock import Mock

import pytest

from touken import game_state, youzu_log


def event(body, endpoint="/home", at=1000, status=200):
    return {"direction": "S->C", "status": status, "endpoint": endpoint,
            "payload": {"status": 0, "now_time": at, **body}}


def team_events(hp=45):
    return [event({"party": {"1": {"slot": {"1": {"serial_id": "111"}}}},
                   "sword": {"111": {"sword_id": 118, "hp": hp, "hp_max": 45}}},
                  "/party/list")]


def battle_result(hp=25, maximum=77, status=2, at=1001, serial="111"):
    return event({"result": {"player": {"party": {"slot": {
        "5": {"serial_id": serial, "sword_id": 118, "hp": hp,
              "hp_max": maximum, "status": status}}}}}}, "/battle/battle", at)


def test_battle_status_is_used_by_instance_and_refreshes_post_battle_hp():
    report = game_state.injury_report(team_events() + [battle_result()], 1, now=1002)
    member = report["members"][0]
    assert member["hp"] == 25 and member["hp_max"] == 77
    assert member["injury"] == "中伤"
    assert member["injury_source"] == "battle_status"


def test_battle_code_is_not_overwritten_by_roster_status():
    events = team_events() + [battle_result(hp=34, maximum=45)]
    fresh = event({"sword": {"111": {"hp": 34, "hp_max": 45, "status": "0"}}}, "/home", 1002)
    report = game_state.injury_report(events + [fresh], 1, now=1003)
    assert report["max_injury"] == "中伤"  # HP alone would say light.
    assert report["members"][0]["injury_source"] == "battle_status"


def test_roster_status_alone_never_becomes_an_injury_code():
    events = team_events(34)
    events[0]["payload"]["sword"]["111"]["status"] = 2
    assert game_state.injury_report(events, 1, now=1001)["max_injury"] == "轻伤"


def test_hp_change_invalidates_old_code_even_if_hp_later_returns():
    events = team_events() + [battle_result(hp=34, maximum=45),
        event({"sword": {"111": {"hp": 45}}}, "/repair/complete", 1002),
        event({"sword": {"111": {"hp": 34}}}, "/home", 1003)]
    report = game_state.injury_report(events, 1, now=1004)
    assert report["max_injury"] == "轻伤"
    assert report["members"][0]["injury_source"] == "hp"


def test_other_instances_enemy_results_and_missing_serial_cannot_supply_status():
    events = team_events(34) + [battle_result(serial="222")]
    assert game_state.injury_report(events, 1, now=1002) is None
    bad = battle_result()
    del bad["payload"]["result"]["player"]["party"]["slot"]["5"]["serial_id"]
    assert game_state.injury_report(team_events() + [bad], 1, now=1002) is None
    enemy = battle_result()
    enemy["payload"]["result"]["enemy"] = enemy["payload"]["result"].pop("player")
    assert game_state.injury_report(team_events() + [enemy], 1, now=1002) is None


@pytest.mark.parametrize("status,hp,expected,source", [
    (0, 45, None, "battle_status"), (0, 10, "重伤", "hp"),
    (2, 10, "重伤", "hp"), (99, 34, "轻伤", "hp"),
    (None, 34, "轻伤", "hp"), (3, 10, "重伤", "hp"),
])
def test_unverified_or_conflicting_codes_do_not_bypass_hp_protection(status, hp, expected, source):
    report = game_state.injury_report(team_events() + [battle_result(hp=hp, maximum=45, status=status)], 1, now=1002)
    assert report["max_injury"] == expected
    assert report["members"][0]["injury_source"] == source


def test_new_battle_or_expired_evidence_cannot_reuse_old_status():
    events = team_events() + [battle_result(hp=34, maximum=45)]
    state = game_state.sword_facts(events)
    assert game_state.sword_injury(state, "111", now=1700) == ("轻伤", "hp")
    events.append(event({}, "/battle/battle", 1002))
    assert game_state.injury_report(events, 1, now=1003) is None


def test_return_then_fresh_identical_hp_keeps_battle_status():
    events = team_events() + [battle_result(hp=34, maximum=45),
                             event({}, "/sally/return", 1002)]
    assert game_state.injury_report(events, 1, now=1003) is None
    fresh = team_events(34)[0]
    fresh["endpoint"] = "/sally"
    fresh["payload"]["now_time"] = 1003
    report = game_state.injury_report(events + [fresh], 1, now=1004)
    assert report["max_injury"] == "中伤"
    assert report["members"][0]["injury_source"] == "battle_status"


def test_older_hp_update_does_not_erase_newer_battle_evidence():
    events = team_events() + [battle_result(hp=34, maximum=45, at=1002),
        event({"sword": {"111": {"hp": 45}}}, "/home", 1001)]
    assert game_state.injury_report(events, 1, now=1003)["members"][0]["injury_source"] == "battle_status"


def test_repair_and_roster_use_the_same_battle_status_as_departure():
    from touken import sword_db
    from touken.flows.repair import RepairMixin
    from touken.flows.team_roster import _roster_facts_from_events
    now = __import__("time").time()
    first = team_events()[0]
    first["payload"]["now_time"] = now - 2
    events = [first, battle_result(hp=34, maximum=45, at=now - 1)]
    state = game_state.sword_facts(events)
    name = sword_db.find_game_sword(118)[1]["name_zh"]
    repair = RepairMixin._repair_row_fact({"name": name, "prefix": "一之一"}, state)
    assert repair["injury"] == "中伤"
    roster = _roster_facts_from_events(events, 1, now=now)
    assert roster[1]["injury"] == "medium"


def test_sparse_fields_keep_individual_observation_times():
    facts = game_state.resource_facts([
        event({"resource": {"charcoal": 100}, "currency": {"money": 50}}, at=1000),
        event({"currency": {"money": 0}}, at=1601),
        event({"currency": {"money": 999}}, at=1602, status=500),
    ])
    assert facts["木炭"] == {"count": 100, "observed_at": 1000}
    assert game_state.fresh_resources(facts, now=1601) == {"小判": 0}
    assert game_state.fresh_resources(facts, now=900) == {}


@pytest.mark.parametrize("hp,expected", [(45, None), (10, "重伤")])
def test_complete_hp_is_authoritative(hp, expected):
    assert game_state.injury_report(team_events(hp), 1, now=1001)["max_injury"] == expected


def test_actual_uguisumaru_hp_does_not_block_heavy_only_departure():
    from touken.flows.battle import BattleMixin
    events = team_events(25)
    events[0]["payload"]["sword"]["111"]["hp_max"] = 77
    report = game_state.injury_report(events, 1, now=1001)
    assert report["members"][0]["hp"] == 25
    assert report["max_injury"] == "中伤"
    assert not BattleMixin._injury_reaches_threshold(report["max_injury"], "heavy")
    assert BattleMixin._injury_reaches_threshold(report["max_injury"], "medium")
    events[0]["payload"]["sword"]["111"]["hp"] = 23
    report = game_state.injury_report(events, 1, now=1001)
    assert report["max_injury"] == "重伤"
    assert BattleMixin._injury_reaches_threshold(report["max_injury"], "heavy")


@pytest.mark.parametrize("body,endpoint", [
    ({"1": {"slot": {"1": {"serial_id": "222"}}}}, "/party/setsword"),
    ({}, "/sally/start"),
])
def test_changes_after_snapshot_require_new_hp(body, endpoint):
    assert game_state.injury_report(team_events() + [event(body, endpoint, 1001)], 1, now=1002) is None


def test_incomplete_failed_or_stale_hp_is_unknown():
    assert game_state.injury_report(team_events(), 1, now=1700) is None
    events = team_events()
    del events[0]["payload"]["sword"]["111"]["hp"]
    assert game_state.injury_report(events, 1, now=1001) is None
    events = team_events()
    events[0]["payload"]["status"] = 1
    assert game_state.injury_report(events, 1, now=1001) is None


def test_old_party_cannot_borrow_fresh_hp_time():
    events = team_events() + [event({"sword": team_events()[0]["payload"]["sword"]}, "/sally", 1700)]
    assert game_state.injury_report(events, 1, now=1701) is None


def test_departure_requires_current_matching_team_and_map():
    proof = event({"summary": {"2": {"party_no": "2", "field_id": "5"}}}, "/conquest/start", 1001)
    assert game_state.expedition_started([proof], 2, 5, 1000) is proof
    assert game_state.expedition_started([proof], 3, 5, 1000) is False
    assert game_state.expedition_started([proof], 2, 6, 1000) is False
    assert game_state.expedition_started([proof], 2, 5, 1002) is None
    proof["payload"]["status"] = 1
    assert game_state.expedition_started([proof], 2, 5, 1000) is None


def test_matching_request_with_explicit_failure_blocks_visual_success():
    request = {"direction": "C->S", "endpoint": "/conquest/start",
               "payload": {"party_no": "2", "field_id": "5", "now_time": 1000}}
    response = event({"status": 1}, "/conquest/start", 1001)
    assert game_state.expedition_started([request, response], 2, 5, 1000) is False
    response["payload"]["status"] = 0
    assert game_state.expedition_started([request, response], 2, 5, 1000) is response


def test_sally_roster_refresh_replaces_old_party_snapshot():
    fresh = team_events()[0]
    fresh["endpoint"] = "/sally"
    fresh["payload"]["now_time"] = 1700
    assert game_state.injury_report(team_events() + [fresh], 1, now=1701)["max_injury"] is None


def test_partial_hp_update_preserves_instance_and_maximum():
    events = team_events() + [event({"sword": {"111": {"hp": 1}}}, "/repair/complete", 1001)]
    report = game_state.injury_report(events, 1, now=1002)
    assert report["members"][0]["hp_max"] == 45
    assert report["max_injury"] == "重伤"
    assert report["observed_epoch"] == 1001


def test_same_second_changes_use_event_order():
    change = event({"1": {"slot": {"1": {"serial_id": "222"}}}}, "/party/setsword")
    assert game_state.injury_report([change] + team_events(), 1, now=1001) is not None
    assert game_state.injury_report(team_events() + [change], 1, now=1001) is None


def test_unknown_json_still_uses_heavy_injury_protection():
    from touken.flows.battle import BattleMixin
    host = BattleMixin()
    host._log_injury_status = Mock(return_value=(None, None))
    host._team_injury_status = Mock(return_value="重伤")
    assert host._combined_injury_status({}, 1, {}) == ("重伤", None)
    host._log_injury_status.return_value = ("重伤", "hp1/45")
    host._team_injury_status.return_value = None
    assert host._combined_injury_status({}, 1, {}) == ("重伤", "hp1/45")


def test_identity_reader_uses_shared_cleanup(monkeypatch, tmp_path):
    from touken.selection_identity import client_events
    path = tmp_path / "synthetic.log"
    path.write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(youzu_log, "pull_log", Mock(return_value=path))
    monkeypatch.setattr(youzu_log, "parse_events", Mock(side_effect=ValueError()))
    assert client_events(Mock(adb_path="adb", adb_address="address")) == []
    assert not path.exists()
