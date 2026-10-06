import json

from touken.sword_receipts import build_receipts, write_receipts
from touken.telemetry import TelemetryStore


def event(endpoint, payload, second=0, direction="S->C", status=200):
    return {"endpoint": endpoint, "payload": payload, "direction": direction,
            "ts": f"2026-10-01 12:00:{second:02d}", "status": status}


def forge(second=0):
    return [event("/forge/completemultiple", {"slot_no": "1"}, second, "C->S"),
            event("/forge/completemultiple", {
                "finish_count": 2, "sword": [
                    {"sword_id": 99, "serial_id": n} for n in range(1, 11)]}, second)]


def test_batch_preserves_ten_instances_and_ignores_remaining_slots():
    receipt = build_receipts(forge())[0]
    assert receipt["payload"]["count"] == 10
    assert len({s["serial_id"] for s in receipt["payload"]["swords"]}) == 10
    assert receipt["payload"]["slot"] == 1


def test_accelerated_collection_preserves_new_swords_and_deduplicates(tmp_path):
    events = forge()
    for item in events:
        item['endpoint'] = '/forge/fastmultiple'
    events[1]['payload']['sword'][0]['is_first_get_sword'] = True
    receipt, = build_receipts(events)
    assert receipt['event_type'] == 'forge.collected'
    assert receipt['payload']['count'] == 10
    assert receipt['payload']['swords'][0]['is_first_get_sword'] is True
    store = TelemetryStore(tmp_path / 'events.db')
    assert write_receipts(store, [receipt]) == {'written': 1, 'reconciled': 0}
    assert write_receipts(store, [receipt, receipt]) == {'written': 0, 'reconciled': 0}
    assert len(store.recent_events()) == 1
    events[1]['payload']['status'] = 1
    assert build_receipts(events) == []


def test_drops_use_route_and_no_party_member_serial():
    events = [event("/sally/sally", {"episode_id": "8", "field_id": "2", "party_no": "4"}, direction="C->S"),
              event("/sally/sally", {}), event("/sally/forward", {"square_id": 17}),
              event("/battle/battle", {"result": {"get_sword_id": "81", "player": {"serial_id": 123}}}),
              event("/battle/battle", {"result": {"get_sword_id": 0}})]
    receipt, = [r for r in build_receipts(events) if r["event_type"] == "sword.obtained"]
    p = receipt["payload"]
    assert (p["chapter"], p["map_no"], p["team_no"], p["square_id"]) == (8, 2, 4, 17)
    assert p["name"] == "宗三左文字"
    assert "serial_id" not in p
    events += [event("/sally/eventsally", {}, direction="C->S"), events[3]]
    drop = [r for r in build_receipts(events) if r["event_type"] == "sword.obtained"][-1]
    assert "chapter" not in drop["payload"]


def test_failed_and_non_drop_responses_are_ignored():
    assert build_receipts([event("/forge/completemultiple", forge()[1]["payload"], status=500),
                           event("/battle/battle", {"status": 1, "result": {"get_sword_id": 99}})]) == []


def test_raid_drop_does_not_reuse_normal_map():
    receipt, = [r for r in build_receipts([event("/battle/alloutbattle", {"result": {"get_sword_id": 99}})])
                if r["event_type"] == "sword.obtained"]
    assert receipt["payload"]["source"] == "raid.drop"
    assert "chapter" not in receipt["payload"]


def test_idempotence_and_ocr_upgrade_preserve_run(tmp_path):
    store = TelemetryStore(tmp_path / "events.db")
    receipts = build_receipts(forge())
    conn = store._conn()
    conn.execute("INSERT INTO events(ts,run_id,script,event_type,payload) VALUES (?,NULL,'forge','forge.collected',?)",
                 (receipts[0]["ts"] + 3, json.dumps({"slot": 1, "name": "堀川国广", "duration": "01:30"})))
    conn.commit()
    assert write_receipts(store, receipts) == {"written": 0, "reconciled": 1}
    assert write_receipts(store, receipts) == {"written": 0, "reconciled": 0}
    row = store.recent_events()[0]
    assert row["payload"]["count"] == 10
    assert row["payload"]["ocr_evidence"]["duration"] == "01:30"
    assert row["payload"]["execution_script"] == "forge"
    # 名册翻译调整不改变一笔游戏领取的身份。
    receipts[0]["payload"]["swords"][0]["name"] = "更新的译名"
    assert write_receipts(store, receipts) == {"written": 0, "reconciled": 0}


def test_two_nearby_same_swords_are_not_collapsed(tmp_path):
    store = TelemetryStore(tmp_path / "events.db")
    receipts = [r for r in build_receipts(
        [event("/battle/battle", {"result": {"get_sword_id": 99}}, second=n) for n in (1, 30)])
        if r["event_type"] == "sword.obtained"]
    assert write_receipts(store, receipts)["written"] == 2
    assert write_receipts(store, receipts)["written"] == 0
    assert len(store.recent_events()) == 2


# ---------------------------------------------------------------- 周回分母/履历

def _routed_battle(second=0):
    return [event("/sally/sally", {"episode_id": "8", "field_id": "2", "party_no": "4",
                                   "user_id": "u1", "session": "s"}, second, "C->S"),
            event("/sally/sally", {}), event("/sally/forward", {"square_id": 17}),
            event("/battle/battle", {"result": {"get_sword_id": 0}})]


def test_battle_completed_is_emitted_for_every_successful_battle():
    events = _routed_battle() + [event("/battle/alloutbattle", {"result": {}})]
    battles = [r for r in build_receipts(events) if r["event_type"] == "battle.completed"]
    assert len(battles) == 2
    p = battles[0]["payload"]
    assert (p["chapter"], p["map_no"], p["team_no"], p["square_id"]) == (8, 2, 4, 17)
    assert p["endpoint"] == "/battle/battle"
    # 白名单：分母只带路线事实，不夹带 result 细节和请求凭证
    assert "result" not in p and "user_id" not in p and "session" not in p
    raid = battles[1]["payload"]
    assert (raid["chapter"], raid["map_no"], raid["team_no"], raid["square_id"]) \
        == (None, None, None, None)


def test_battle_completed_falls_back_to_null_route():
    receipt, = [r for r in build_receipts([event("/battle/battle", {"result": {}})])
                if r["event_type"] == "battle.completed"]
    assert (receipt["payload"]["chapter"], receipt["payload"]["square_id"]) == (None, None)


def test_battle_completed_is_idempotent_across_repulls(tmp_path):
    store = TelemetryStore(tmp_path / "events.db")
    receipts = [r for r in build_receipts(_routed_battle()) if r["event_type"] == "battle.completed"]
    assert len(receipts) == 1
    assert write_receipts(store, receipts) == {"written": 1, "reconciled": 0}
    assert write_receipts(store, receipts) == {"written": 0, "reconciled": 0}
    assert len(store.recent_events()) == 1


def test_forge_started_expands_slots_and_whitelists_recipe():
    events = [event("/forge/startmultiple",
                    {"charcoal": "50", "steel": "60", "coolant": "70", "file": "80",
                     "slot_no": "9", "user_id": "u1", "session": "s"},
                    direction="C->S"),
              event("/forge/startmultiple", {"multiple": ["3", "4"], "status": 0})]
    started = [r for r in build_receipts(events) if r["event_type"] == "forge.started"]
    assert [r["payload"]["slot_no"] for r in started] == [3, 4]
    p = started[0]["payload"]
    assert (p["charcoal"], p["steel"], p["coolant"], p["file"]) == (50, 60, 70, 80)
    assert "user_id" not in p and "session" not in p


def test_forge_started_accepts_dict_slots_and_null_recipe():
    events = [event("/forge/startmultiple", {}, direction="C->S"),
              event("/forge/startmultiple", {"multiple": [{"slot_no": "2"}], "status": 0})]
    receipt, = [r for r in build_receipts(events) if r["event_type"] == "forge.started"]
    assert receipt["payload"]["slot_no"] == 2
    assert receipt["payload"]["charcoal"] is None  # 请求没配方就 null，不猜


def test_forge_started_requires_slot_list():
    events = [event("/forge/startmultiple", {"charcoal": "50"}, direction="C->S"),
              event("/forge/startmultiple", {"status": 0})]
    assert build_receipts(events) == []


def test_secretary_observed_from_login_start():
    events = [event("/login/start", {"secretary": "5", "name": "不许入库",
                                     "user_code": "1w-xxx", "status": 0})]
    receipt, = [r for r in build_receipts(events) if r["event_type"] == "secretary.observed"]
    assert receipt["payload"]["sword_id"] == 5
    assert "name" not in receipt["payload"] and "user_code" not in receipt["payload"]


def test_secretary_observed_requires_secretary_field():
    assert build_receipts([event("/login/start", {"level": "10", "status": 0})]) == []


def test_activity_calendar_payload_dedup(tmp_path):
    store = TelemetryStore(tmp_path / "events.db")
    body = {"event": {"1": {"event_id": "10031", "type": "1",
                            "start_at": "2026-10-01 00:00:00",
                            "end_at": "2026-10-08 23:59:59"}},
            "status": 0}
    events = [event("/home/get_all_activity", body)]
    receipt, = [r for r in build_receipts(events) if r["event_type"] == "activity.calendar"]
    assert receipt["payload"]["events"] == [
        {"event_id": "10031", "type": 1,
         "start_at": "2026-10-01 00:00:00", "end_at": "2026-10-08 23:59:59"}]
    assert write_receipts(store, [receipt]) == {"written": 1, "reconciled": 0}
    assert write_receipts(store, [receipt]) == {"written": 0, "reconciled": 0}
    # 日历内容变了（新事实）→ 再记一条；空日历不产事件
    body["event"]["1"]["end_at"] = "2026-10-09 23:59:59"
    changed, = [r for r in build_receipts(events) if r["event_type"] == "activity.calendar"]
    assert write_receipts(store, [changed]) == {"written": 1, "reconciled": 0}
    assert len(store.recent_events(event_type="activity.calendar")) == 2
    assert build_receipts([event("/home/get_all_activity", {"event": {}, "status": 0})]) == []


def test_kiwame_returned_and_departed_share_leave_response():
    events = [event("/home/leave", {"serial_id": "777", "token": "t"}, direction="C->S"),
              event("/home/leave", {"status": 0, "evolution": {"back": {"1": {
                  "serial_id": "888", "finished_at": "2026-10-02 08:00:00"}}}})]
    receipts = build_receipts(events)
    returned, = [r for r in receipts if r["event_type"] == "kiwame.returned"]
    assert (returned["payload"]["serial_id"],
            returned["payload"]["finished_at"]) == (888, "2026-10-02 08:00:00")
    departed, = [r for r in receipts if r["event_type"] == "kiwame.departed"]
    assert departed["payload"]["serial_id"] == 777
    assert "token" not in departed["payload"]


def test_kiwame_departed_not_produced_when_request_field_unverified():
    # 请求 payload 没有白名单候选键（字段名未实测）：不产 departed，
    # 但响应里的归来块照常记。
    events = [event("/home/leave", {"foo": "1"}, direction="C->S"),
              event("/home/leave", {"status": 0,
                                    "evolution": {"back": {"1": {"serial_id": "888"}}}})]
    receipts = build_receipts(events)
    assert [r for r in receipts if r["event_type"] == "kiwame.departed"] == []
    returned, = [r for r in receipts if r["event_type"] == "kiwame.returned"]
    assert returned["payload"]["serial_id"] == 888
