"""Current opponent receipts and data-first snapshots, with explicit unknowns."""
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from touken import game_state as gs
from touken.flows.practice import PracticeMixin
from touken.flows.snapshot import SnapshotMixin
from touken.maa_adapter import Point


def event(endpoint, payload, at=1000, direction="S->C"):
    return {"endpoint": endpoint, "direction": direction, "status": 200,
            "ts": "2026-10-12 12:00:00", "payload": {"now_time": at, **payload}}


def opponents(at=1000):
    return event("/practice", {"status": 0, "summary": {
        str(i): {"enemy_id": i+100, "rank": 0, "name": "测试对手"} for i in range(1, 6)}}, at)


def battle(enemy=101, rank=2, at=1001):
    return [event("/battle/practicebattle", {"enemy_user_id": enemy}, at, "C->S"),
            event("/battle/practicebattle", {"status": 0, "result": {"rank": rank}}, at+1)]


def test_list_counts_all_current_opponents_battles_not_just_last_one():
    rows = gs.practice_list([opponents(), *battle(), *battle(102, at=1003)], now=1005)
    assert rows[0]["rank"] == rows[1]["rank"] == 2
    assert rows[2]["rank"] == 0


def test_list_is_invalid_after_daily_refresh_or_stale_observation():
    now = datetime(2026, 10, 12, 15, 0, 1, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert gs.practice_list([opponents(now-2)], now=now) is None
    assert gs.practice_list([opponents()], now=1700) is None


def test_receipt_rejects_wrong_opponent_and_excludes_preclick_request():
    events = battle()
    boundary = {"after": 1000, "requests": set()}
    assert gs.practice_receipt(events, boundary, "/battle/practicebattle", 101, "enemy_user_id")["result"]["rank"] == 2
    assert gs.practice_receipt(events, boundary, "/battle/practicebattle", 102, "enemy_user_id") is False
    boundary["requests"].add(gs._request_key(events[0]))
    assert gs.practice_receipt(events, boundary, "/battle/practicebattle", 101, "enemy_user_id") is None
    events[1]["payload"]["status"] = 1
    assert gs.practice_receipt(events, {"after":1000,"requests":set()}, "/battle/practicebattle", 101, "enemy_user_id") is False


@pytest.mark.parametrize("clock_offset", [-3600, -5, 5, 3600])
@pytest.mark.parametrize("endpoint,parameter", [
    ("/practice/offer", "enemy_id"), ("/battle/practicebattle", "enemy_user_id")])
def test_practice_current_receipt_uses_preclick_snapshot_across_clocks(
        monkeypatch, clock_offset, endpoint, parameter):
    host = Mock(adb_address="device", adb_path="adb")
    old = event(endpoint, {parameter: 101}, 900, "C->S")
    old_response = event(endpoint, {"status": 0, "result": {"rank": 2}}, 901)
    monkeypatch.setattr(gs, "read_events", lambda *_: [old, old_response])
    monkeypatch.setattr(gs.time, "time", lambda: 1000 + clock_offset)
    boundary = gs.begin_operation(host)
    current = event(endpoint, {parameter: 101}, 1000, "C->S")
    response = event(endpoint, {"status": 0, "result": {"rank": 9}}, 1004)
    assert gs.practice_receipt([old, old_response], boundary, endpoint, 101, parameter) is None
    assert gs.practice_receipt([old, old_response, current, response], boundary,
                               endpoint, 101, parameter) == response["payload"]
    assert gs.practice_receipt([current, response], boundary, endpoint, 102, parameter) is False
    assert gs.practice_receipt([old_response], boundary, endpoint, 101, parameter) is None


def test_failed_preclick_read_does_not_authorize_historical_receipts(monkeypatch):
    host = Mock(adb_address="device", adb_path="adb")
    def fail(*_):
        raise OSError("unavailable")
    monkeypatch.setattr(gs, "read_events", fail)
    monkeypatch.setattr(gs.time, "time", lambda: 2000)
    boundary = gs.begin_operation(host)
    assert boundary["request_snapshot"] is False
    assert gs.practice_receipt(battle(), boundary, "/battle/practicebattle", 101,
                               "enemy_user_id") is None


def offer():
    return {"enemy_party": {"1": {"slot": {str(i): {"serial_id": 1 if i == 1 else None} for i in range(1, 7)}}},
            "enemy_sword": {"1": {"sword_id": 118, "level": 90, "mobile": 40}}}


def test_member_count_uses_slots_and_form_uses_identity_not_mobility(monkeypatch):
    from touken import sword_db
    monkeypatch.setattr(sword_db, "find_game_sword", lambda _: ("known", {"name": "测试刀", "type": "短刀"}, "kiwame"))
    members = gs.practice_members(offer())
    assert len(members) == 1 and members[0]["extreme"] is True
    host = PracticeMixin()
    assert host._strength_key(members)[1] == 1
    members[0]["mobility"] = None
    assert host._judge(members)[0] == "打"
    body = offer()
    body["enemy_sword"] = {}
    assert gs.practice_members(body) is None


def test_unknown_game_member_and_unreadable_screen_never_become_empty(monkeypatch):
    from touken import sword_db
    monkeypatch.setattr(sword_db, "find_game_sword", lambda _: None)
    host = PracticeMixin()
    assert host._judge(gs.practice_members(offer()))[0] == "躲"
    host.maa = Mock()
    host.maa.ocr_all.return_value = []
    member = host._scan_member(152)
    assert member["type"] == "未知"
    assert host._judge([member])[0] == "躲"


@pytest.mark.parametrize("count", [1, 3, 5, 6])
def test_confirmed_empty_slots_are_not_unknown_members(count):
    body = offer()
    body["enemy_party"]["1"]["slot"] = {
        str(i): {"serial_id": i if i <= count else None} for i in range(1, 7)}
    body["enemy_sword"] = {str(i): {"serial_id": i, "sword_id": 202,
                                    "level": 82} for i in range(1, count + 1)}
    members = gs.practice_members(body)
    assert len(members) == count
    assert all(m["id"] is not None for m in members)
    assert PracticeMixin()._judge(members)[0] == "打"


def test_unknown_members_are_still_rejected_without_repeated_or_false_roster_reason():
    member = {"name": "未识别成员", "type": "未知", "id": None}
    assert PracticeMixin()._judge([member] * 3) == ("躲", ["未识别成员身份未确认"])


def test_known_list_results_and_order_do_not_read_stamps():
    host = PracticeMixin()
    host._practice_rows = gs.practice_list([opponents(), *battle()], now=1003)
    host.maa = Mock()
    host.maa.ocr_all.side_effect = AssertionError("known outcome needs no stamp")
    assert host._scan_existing_results() == {0:"win"}
    assert host._scan_list_order() == [1,2,3,4]
    assert gs.practice_rank(9) is None


def test_offer_reads_only_the_new_matching_response(monkeypatch):
    from touken.flows import practice as module
    host = PracticeMixin()
    host.maa = Mock()
    host.maa.ocr.return_value = Point(640,20)
    host.maa.ocr_all.side_effect = AssertionError("game roster needs no OCR")
    host._practice_rows = gs.practice_list([opponents()], now=1001)
    request = event("/practice/offer", {"enemy_id": 101}, direction="C->S")
    response = event("/practice/offer", {"status":0, **offer()}, 1001)
    monkeypatch.setattr(module, "begin_operation", lambda _: {"after":1000,"requests":set()})
    monkeypatch.setattr(module, "available_events", lambda _: [request,response])
    monkeypatch.setattr(module, "practice_members", lambda _: [{"name":"测试刀","id":"known","type":"打刀"}])
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert host._inspect_opponent(0)["members"][0]["id"] == "known"


def balances(at=1000):
    return event("/home", {"status":0, "resource":{"charcoal":11,"steel":22,"coolant":33,"file":44,"bill":55},
        "currency":{"point":66,"point_free":0,"money":77}, "assist_item_id":8,"assist_item_num":88},at)


def test_complete_snapshot_needs_no_navigation_screen_or_menu(monkeypatch,tmp_path):
    from touken.flows import snapshot as module
    host = SnapshotMixin()
    host.maa = Mock()
    host.navigate_to_stream = Mock(side_effect=AssertionError("data requires no navigation"))
    host.maa.screenshot.side_effect = AssertionError("data requires no screen")
    monkeypatch.setattr(module,"_STATUS_DIR",tmp_path)
    monkeypatch.setattr(module,"available_events",lambda _:[balances()])
    monkeypatch.setattr(gs.time,"time",lambda:1001)
    list(host.status_snapshot_stream(phase="after"))
    saved=json.loads((tmp_path/"inventory.json").read_text(encoding="utf-8"))
    assert saved["resources"]["木炭"]==11 and saved["resources"]["小判"]==77
    assert host._last_full_snapshot_complete is True
    list(host._capture_inventory())
    assert host.quick_peek(force=True)
    assert host._read_koban()==77


def test_partial_snapshot_keeps_json_and_unreadable_furnaces_unknown(monkeypatch,tmp_path):
    from touken.flows import snapshot as module
    host=SnapshotMixin()
    host.maa=Mock()
    def ocr(roi):
        if roi.x==400:return [(str(i*100),Point(400+i*100,20)) for i in range(1,6)]
        return []
    host.maa.ocr_all.side_effect=ocr
    host._read_koban=lambda:77
    monkeypatch.setattr(module,"_STATUS_DIR",tmp_path)
    monkeypatch.setattr(module,"available_events",lambda _:[event("/home",{"status":0,"resource":{"charcoal":11}})])
    monkeypatch.setattr(gs.time,"time",lambda:1001)
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    list(host._capture_inventory())
    saved=json.loads((tmp_path/"inventory.json").read_text(encoding="utf-8"))
    assert saved["resources"]["木炭"]==11 and saved["resources"]["玉钢"]==200
    assert all(f["state"]=="未知" for f in saved["furnaces"])


def test_furnace_states_follow_deadlines_and_changes_invalidate_old_slots():
    now=datetime(2026,10,12,12,tzinfo=timezone(timedelta(hours=8))).timestamp()
    e=event("/home/situation",{"status":0,"forge":{
        "1":{"slot_no":1,"finished_at":None},
        "2":{"slot_no":2,"finished_at":"2026-10-12 11:00:00"},
        "3":{"slot_no":3,"finished_at":"2026-10-12 13:00:00"}}},now)
    assert gs.furnace_facts([e],now=now)==[
        {"slot":1,"state":"空闲中","remain":None},
        {"slot":2,"state":"完成","remain":None},
        {"slot":3,"state":"锻造中","remain":"1:00:00"}]
    assert gs.furnace_facts([e,event("/forge/complete",{"status":0},now+1)],now=now+2) is None
    assert gs.furnace_facts([e],now=now+601) is None



def test_current_battle_receipt_precedes_screen_result(monkeypatch):
    from touken.flows import practice as module
    host=PracticeMixin()
    host.maa=Mock()
    host.maa.ocr.return_value=Point(640,20)
    host._practice_enemy_id=101
    host._formation_name=lambda name:name
    host._formation_mode_state=lambda **kwargs:None
    host._on_list=lambda:True
    host._read_result=Mock(side_effect=AssertionError("known current battle needs no stamp"))
    monkeypatch.setattr(module,"begin_operation",lambda _: {"after":1000,"requests":set()})
    monkeypatch.setattr(module,"available_events",lambda _:battle())
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    stream=host._fight_one({},0)
    while True:
        try:next(stream)
        except StopIteration as stop:
            assert stop.value=="win"
            break
    host._read_result.assert_not_called()
