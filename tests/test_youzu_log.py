# -*- coding: utf-8 -*-
"""youzu_log：国服 HttpRequestCollect 日志解析测试（全部合成数据）"""

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from touken import youzu_log


def _c2s(ts, method, url, data=""):
    return f"【{ts}】【C->S】[{method}] {url} Data:[{data}]"


def _s2c(ts, url, payload):
    return (f"【{ts}】【S->C】{url} readyState:4 status:200 "
            f"data:{json.dumps(payload, ensure_ascii=False)}")


SAMPLE = "\n".join([
    _c2s("2026-09-28 11:34:37", "GET",
         "https://mp-djlw.youzu.com/enter",
         "op_id=2106&opgame_id=2297"),
    _s2c("2026-09-28 11:34:38", "https://s39-ios-djlw.youzu.com/login/start?uid=1",
         {"user_id": 1, "name": "测试婶", "level": "290", "exp": "100",
          "server_name": "备前国", "created_at": "2017-06-03 22:50:37",
          "secretary": "119", "status": 0, "t": "x" * 128}),
    _s2c("2026-09-28 11:34:39", "https://s39-ios-djlw.youzu.com/home?uid=1",
         {"resource": {"charcoal": 100, "steel": 200, "coolant": 300,
                       "file": 400, "bill": 5},
          "currency": {"money": "999"},
          "duty": {"type": "3"}, "season_id": "25",
          "season_end_at": "2026-12-17 10:00:00",
          "status": 0, "now": "2026-09-28 11:34:39"}),
    _s2c("2026-09-28 11:34:40", "https://s39-ios-djlw.youzu.com/party/list?uid=1",
         {"sword": {"111": {"serial_id": "111", "sword_id": "118",
                            "rarity": "2", "level": "99", "hp": "45",
                            "hp_max": "45", "fatigue": "100",
                            "evol_num": "0", "protect": "1"}},
          "party": {"1": {"party_no": "1", "status": "1",
                          "party_name": "主力",
                          "slot": {"1": {"serial_id": "111"},
                                   "2": {"serial_id": None}},
                          "finished_at": None}},
          "status": 0}),
    _s2c("2026-09-28 11:34:41", "https://s39-ios-djlw.youzu.com/home/situation?uid=1",
         {"party": {"1": {"status": "1", "equip_full": 1, "member_full": 1}},
          "repair": [], "forge": {"1": {"slot_no": "1",
                                        "finished_at": "2026-09-28 12:00:00"}},
          "status": 0}),
    # 后来的 /home 覆盖先前的（latest-wins）
    _s2c("2026-09-28 11:35:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
         {"resource": {"charcoal": 50, "steel": 60, "coolant": 70,
                       "file": 80, "bill": 6},
          "currency": {"money": "1000"}, "status": 0,
          "now": "2026-09-28 11:35:00"}),
    "这不是日志行，应该被计进坏行",
])


def test_parse_events(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(SAMPLE, encoding="utf-8")
    events = youzu_log.parse_events(f)
    meta = events[-1]["payload"]
    assert meta["bad_lines"] == 1
    assert meta["event_count"] == 6  # 6 条收发，META 条不计入

    c2s = events[0]
    assert c2s["direction"] == "C->S"
    assert c2s["method"] == "GET"
    assert c2s["endpoint"] == "/enter"
    assert c2s["payload"] == {"op_id": "2106", "opgame_id": "2297"}

    s2c = events[1]
    assert s2c["endpoint"] == "/login/start"
    assert s2c["payload"]["name"] == "测试婶"


def test_snapshot_latest_wins(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(SAMPLE, encoding="utf-8")
    snap = youzu_log.build_snapshot(youzu_log.parse_events(f))

    # 敏感字段一律不进快照（盗号两件套防御）
    assert "name" not in snap["profile"]
    assert "server_name" not in snap["profile"]
    assert "user_id" not in snap["profile"]
    assert snap["profile"]["level"] == 290
    # 后一条 /home 的资源覆盖前一条
    assert snap["resources"]["charcoal"] == 50
    assert snap["resources"]["koban"] == 1000

    assert snap["sword_count"] == 1
    party1 = snap["parties"][0]
    assert party1["party_name"] == "主力"
    assert party1["status"] == 1
    assert len(party1["members"]) == 1
    assert party1["members"][0]["serial_id"] == 111
    assert party1["members"][0]["level"] == 99

    assert snap["forge_slots"] == [
        {"slot_no": 1, "finished_at": "2026-09-28 12:00:00"}]
    assert snap["season"]["end_at"] == "2026-12-17 10:00:00"
    assert snap["server_time"] == "2026-09-28 11:35:00"


def test_snapshot_without_swords(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(SAMPLE, encoding="utf-8")
    snap = youzu_log.build_snapshot(youzu_log.parse_events(f),
                                    with_swords=False)
    assert "swords" not in snap
    assert snap["sword_count"] == 1


def test_home_situation_keeps_source_times_and_excludes_credentials(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(SAMPLE, encoding="utf-8")
    path = tmp_path / "state" / "youzu_home_situation.json"
    situation = youzu_log.save_home_situation(youzu_log.parse_events(f), path)
    saved = path.read_text(encoding="utf-8")
    assert situation["secretary"]["observed_at"] == "2026-09-28 11:34:38"
    assert situation["parties_observed_at"] == "2026-09-28 11:34:40"
    assert situation["parties"][0]["members"][0]["name"] == "压切长谷部"
    assert situation["forge_slots"][0]["finished_at"] == "2026-09-28 12:00:00"
    # /home 的资源读数进近况，时间取最新一条 /home（不许拿 pull 时间冒充）
    assert situation["resources"]["charcoal"] == 50
    assert situation["resources"]["koban"] == 1000
    assert situation["resources"]["bill"] == 6
    assert situation["resources_observed_at"] == "2026-09-28 11:35:00"
    member = situation["parties"][0]["members"][0]
    assert member["hp"] == 45 and member["hp_max"] == 45
    assert member["fatigue"] == 100
    assert member["injury"] is None  # 满血
    assert json.loads(saved) == situation
    for secret in ("测试婶", "user_id", "uid=1", '"t"', "serial_id"):
        assert secret not in saved


def test_empty_home_situation_preserves_previous_record(tmp_path):
    path = tmp_path / "youzu_home_situation.json"
    path.write_text('{"schema":1}', encoding="utf-8")
    assert youzu_log.save_home_situation([], path) is None
    assert path.read_text(encoding="utf-8") == '{"schema":1}'


def test_home_refresh_preserves_party_section_and_backup(tmp_path):
    path = tmp_path / "situation.json"
    full = tmp_path / "full.log"
    full.write_text(SAMPLE, encoding="utf-8")
    previous = youzu_log.save_home_situation(youzu_log.parse_events(full), path)
    original = path.read_bytes()
    home = tmp_path / "home.log"
    home.write_text(_s2c("2026-09-28 12:00:00", "https://s39-ios-djlw.youzu.com/home",
                        {"status": 0, "resource": {"file": 500}}), encoding="utf-8")
    current = youzu_log.save_home_situation(youzu_log.parse_events(home), path)
    assert current["parties"] == previous["parties"]
    assert current["parties_observed_at"] == previous["parties_observed_at"]
    assert current["resources"]["whetstone"] == 500
    assert path.with_suffix(".json.bak").read_bytes() == original
    # 备份可恢复旧快照，读取没有改写备份。
    assert json.loads(path.with_suffix(".json.bak").read_text(encoding="utf-8")) == previous


def test_login_party_can_use_full_sword_data_and_latest_party_wins(tmp_path):
    path = tmp_path / "log.txt"
    party = {"4": {"slot": {"1": {"serial_id": "111"}}, "status": "1", "finished_at": None}}
    path.write_text(SAMPLE + "\n" + _s2c("2026-09-28 12:00:00",
        "https://s39-ios-djlw.youzu.com/login/start", {"status": 0, "party": party}), encoding="utf-8")
    current = youzu_log.build_home_situation(youzu_log.parse_events(path))
    assert current["parties_observed_at"] == "2026-09-28 12:00:00"
    assert current["parties"][0]["party_no"] == 4
    assert current["parties"][0]["members"][0]["level"] == 99


def test_preparation_pages_supply_parties_and_partial_state_does_not_erase_them(tmp_path):
    path = tmp_path / "log.txt"
    sword = {"111": {"serial_id": "111", "sword_id": "118", "level": "99"}}
    party = {"4": {"slot": {"1": {"serial_id": "111"}}, "status": "1", "finished_at": None}}
    for endpoint, field in [("/sally", "sword_all"), ("/conquest", "sword")]:
        path.write_text(_s2c("2026-09-28 12:00:00", "https://s39-ios-djlw.youzu.com" + endpoint,
            {"status": 0, "party": party, field: sword}) + "\n" +
            _s2c("2026-09-28 12:01:00", "https://s39-ios-djlw.youzu.com/conquest/complete",
                 {"status": 0, "party": {"4": {"status": "1"}}}), encoding="utf-8")
        current = youzu_log.build_home_situation(youzu_log.parse_events(path))
        assert current["parties_observed_at"] == "2026-09-28 12:00:00"
        assert current["parties"][0]["members"][0]["level"] == 99


def test_home_situation_api_refresh_uses_private_state_and_burns_log(tmp_path):
    from panel import server
    from touken.telemetry import TelemetryStore
    test_store = TelemetryStore(tmp_path / "telemetry.db")
    raw = tmp_path / "pulled.log"
    raw.write_text(SAMPLE, encoding="utf-8")
    config = tmp_path / "touken.json"
    config.write_text('{"adb_path":"adb","adb_address":"127.0.0.1:16384"}',
                      encoding="utf-8")
    with patch.object(server, "STATUS_DIR", tmp_path / "state"), \
         patch("touken.game_state.DEBUG_DIR", tmp_path / "debug"), \
         patch.object(server, "_CONFIG_PATH", config), \
         patch("touken.telemetry.TelemetryStore", return_value=test_store), \
         patch.object(youzu_log, "pull_log", return_value=raw) as pull:
        client = TestClient(server.app)
        assert client.get("/api/honmaru-home/situation").json() == {"situation": None}
        response = client.post("/api/honmaru-home/situation/refresh")
        assert response.status_code == 200
        assert response.json()["situation"]["parties"][0]["members"][0]["name"]
        assert not raw.exists()
        assert pull.call_args.kwargs["dest_dir"] == tmp_path / "debug"
        assert client.get("/api/honmaru-home/situation").json() == response.json()
        saved = tmp_path / "state" / "youzu_home_situation.json"
        saved.write_text('{"schema":2}', encoding="utf-8")
        assert client.get("/api/honmaru-home/situation").status_code == 503
        assert saved.read_text(encoding="utf-8") == '{"schema":2}'


def test_format_summary_runs(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(SAMPLE, encoding="utf-8")
    snap = youzu_log.build_snapshot(youzu_log.parse_events(f))
    text = youzu_log.format_summary(snap)
    assert "测试婶" not in text
    assert "第1部队" in text
    assert "Lv.290" in text


LEDGER_SAMPLE = "\n".join([
    _s2c("2026-09-28 11:00:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
         {"resource": {"charcoal": 1000, "steel": 2000, "coolant": 3000,
                       "file": 4000, "bill": 10},
          "currency": {"money": "500", "point": "100", "point_free": "50"},
          "status": 0, "now_time": 1790545200}),
    # 远征完成：收入木炭
    _c2s("2026-09-28 11:05:00", "POST",
         "https://s39-ios-djlw.youzu.com/conquest/complete?uid=1", "party_no=2"),
    _s2c("2026-09-28 11:05:01",
         "https://s39-ios-djlw.youzu.com/conquest/complete?uid=1",
         {"resource": {"charcoal": 1250, "steel": 2000, "coolant": 3000,
                       "file": 4000, "bill": 10},
          "currency": {"money": "500"}, "status": 0,
          "now_time": 1790545501}),
    # 锻刀开炉：四项资源 + 委托符一起扣
    _c2s("2026-09-28 11:06:00", "POST",
         "https://s39-ios-djlw.youzu.com/forge/startmultiple?uid=1", "slot_no=1"),
    _s2c("2026-09-28 11:06:01",
         "https://s39-ios-djlw.youzu.com/forge/startmultiple?uid=1",
         {"resource": {"charcoal": 550, "steel": 1300, "coolant": 2300,
                       "file": 3300, "bill": 9},
          "currency": {"money": "500"}, "status": 0,
          "now_time": 1790545561}),
    # keepalive 不带资源块，不该产生读数
    _s2c("2026-09-28 11:06:30", "https://s39-ios-djlw.youzu.com/keepalive?uid=1",
         {"status": 0, "now_time": 1790545590}),
])


def test_build_ledger(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(LEDGER_SAMPLE, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))

    # 三次带资源的响应 → 三条读数（keepalive 不算）
    assert len(ledger["observations"]) == 3
    # 首条读数含甲州金合并（100+50）与小判
    first = ledger["observations"][0]["reading"]
    assert first["甲州金"] == 150
    assert first["小判"] == 500
    assert first["委托符"] == 10

    assert len(ledger["changes"]) == 2
    c1, c2 = ledger["changes"]
    assert c1["delta"] == {"木炭": 250}
    assert c1["via"] == ["远征完成"]
    assert c2["delta"] == {"木炭": -700, "玉钢": -700, "冷却材": -700,
                           "砥石": -700, "委托符": -1}
    assert c2["via"] == ["锻刀开炉"]
    # 时间戳用的是响应体里的服务器 now_time
    assert c1["ts"] == 1790545501


def test_ledger_attribution_prefers_labeled_endpoints(tmp_path):
    """轮询请求和动作请求夹在一起时，只归因给已知动作端点。"""
    sample = "\n".join([
        _s2c("2026-09-28 13:00:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"resource": {"charcoal": 100, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "status": 0, "now_time": 1790542800}),
        _c2s("2026-09-28 13:01:00", "GET",
             "https://s39-ios-djlw.youzu.com/home/info?uid=1", ""),
        _c2s("2026-09-28 13:01:01", "POST",
             "https://s39-ios-djlw.youzu.com/mission/rewards?uid=1", "id=1"),
        _s2c("2026-09-28 13:01:02",
             "https://s39-ios-djlw.youzu.com/mission/rewards?uid=1",
             {"resource": {"charcoal": 500, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "status": 0, "now_time": 1790542862}),
    ])
    f = tmp_path / "log.txt"
    f.write_text(sample, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))
    assert ledger["changes"][0]["via"] == ["任务奖励"]
    # via_endpoints 保留全部原始请求留证
    assert ledger["changes"][0]["via_endpoints"] == ["/home/info",
                                                     "/mission/rewards"]


def test_format_ledger_runs(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(LEDGER_SAMPLE, encoding="utf-8")
    text = youzu_log.format_ledger(
        youzu_log.build_ledger(youzu_log.parse_events(f)))
    assert "远征完成" in text
    assert "木炭+250" in text
    assert "2 笔收支" in text


def test_conquest_complete_gets_detailed_label(tmp_path):
    """远征完成的响应自带 party_no/field_id，归因要细分到队和图。

    complete 响应顶层的 field_id 是连排口径（B1=5，2026-09-28 实测）。
    """
    sample = "\n".join([
        _s2c("2026-09-28 12:00:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"resource": {"charcoal": 100, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "status": 0, "now_time": 1790546400}),
        _c2s("2026-09-28 12:37:47", "POST",
             "https://s39-ios-djlw.youzu.com/conquest/complete?uid=1",
             "party_no=4"),
        _s2c("2026-09-28 12:37:48",
             "https://s39-ios-djlw.youzu.com/conquest/complete?uid=1",
             {"success": 2, "field_id": "5", "party_no": "4",
              "conquest": {"field_id": "21"},
              "resource": {"charcoal": 235, "steel": 0, "coolant": 135,
                           "file": 135, "bill": 0},
              "status": 0, "now_time": 1790548668}),
    ])
    f = tmp_path / "log.txt"
    f.write_text(sample, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))
    label = ledger["changes"][0]["via"][0]
    assert label.startswith("远征完成·四队·B1"), label


def test_conquest_start_label_from_request(tmp_path):
    """start 的响应不带 party_no/field_id，细分标签要从请求原文拿。"""
    sample = "\n".join([
        _s2c("2026-09-28 12:00:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"resource": {"charcoal": 100, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "status": 0, "now_time": 1790546400}),
        _c2s("2026-09-28 12:10:00", "POST",
             "https://s39-ios-djlw.youzu.com/conquest/start?uid=1",
             "consumable_id=0&field_id=1&party_no=1"),
        _s2c("2026-09-28 12:10:01",
             "https://s39-ios-djlw.youzu.com/conquest/start?uid=1",
             {"summary": {"1": {"party_no": "1", "field_id": "1"}},
              "resource": {"charcoal": 50, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "status": 0, "now_time": 1790547001}),
    ])
    f = tmp_path / "log.txt"
    f.write_text(sample, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))
    label = ledger["changes"][0]["via"][0]
    assert label.startswith("远征派遣·一队·A1"), label


def test_conquest_complete_koban_reward(tmp_path):
    """complete 的小判走 reward 数组（响应没有 currency 块）：按原文记
    一笔并推高水位线，后续轮询读到新余额时不许再报一次。"""
    sample = "\n".join([
        _s2c("2026-09-28 12:00:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"resource": {"charcoal": 100, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "currency": {"money": "570"},
              "status": 0, "now_time": 1790546400}),
        _c2s("2026-09-28 12:37:47", "POST",
             "https://s39-ios-djlw.youzu.com/conquest/complete?uid=1",
             "party_no=4"),
        _s2c("2026-09-28 12:37:48",
             "https://s39-ios-djlw.youzu.com/conquest/complete?uid=1",
             {"success": 2, "field_id": "5", "party_no": "4",
              "resource": {"charcoal": 100, "steel": 0, "coolant": 135,
                           "file": 135, "bill": 0},
              "reward": [{"item_type": "4", "item_id": "0", "item_num": 200},
                         {"item_type": "5", "item_id": "4", "item_num": 135},
                         {"item_type": "5", "item_id": "5", "item_num": 135}],
              "status": 0, "now_time": 1790548668}),
        # 之后某个轮询端点带回新的小判余额 770：不许再报 +200
        _s2c("2026-09-28 12:38:10", "https://s39-ios-djlw.youzu.com/party/list?uid=1",
             {"resource": {"charcoal": 100, "steel": 0, "coolant": 135,
                           "file": 135, "bill": 0},
              "currency": {"money": "770"},
              "status": 0, "now_time": 1790548690}),
    ])
    f = tmp_path / "log.txt"
    f.write_text(sample, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))

    koban_changes = [c for c in ledger["changes"] if "小判" in c["delta"]]
    assert len(koban_changes) == 1
    ch = koban_changes[0]
    assert ch["delta"] == {"小判": 200}
    assert ch["before"] == {"小判": None}
    assert ch["after"] == {"小判": None}
    assert ch["via"][0].startswith("远征完成·四队·B1")
    # 资源差值照常走 resource 块，不受影响
    res = [c for c in ledger["changes"] if "冷却材" in c["delta"]]
    assert sum(c["delta"].get("冷却材", 0) for c in ledger["changes"]) == 135
    assert sum(c["delta"].get("砥石", 0) for c in ledger["changes"]) == 135


def test_mission_rewards_koban_from_item_list(tmp_path):
    """mission/rewards 的小判在 item 数组里（item_type=4），响应没有
    currency 块——要按原文记账，不许拖到下一次轮询才爆出来。"""
    sample = "\n".join([
        _s2c("2026-09-28 13:00:00", "https://s39-ios-djlw.youzu.com/sign?uid=1",
             {"resource": {"charcoal": 0, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "currency": {"money": "1000"}, "status": 0,
              "now_time": 1790542800}),
        _c2s("2026-09-28 13:03:04", "POST",
             "https://s39-ios-djlw.youzu.com/mission/rewards?uid=1", "id=1"),
        _s2c("2026-09-28 13:03:06",
             "https://s39-ios-djlw.youzu.com/mission/rewards?uid=1",
             {"item": [{"item_type": 5, "item_id": 2, "item_num": 400},
                       {"item_type": 4, "item_id": 0, "item_num": 250}],
              "resource": {"charcoal": 400, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "status": 0, "now_time": 1790542986}),
        # 两个钟头后的轮询带回新余额：不许再报 +250
        _s2c("2026-09-28 15:00:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"currency": {"money": "1250"}, "status": 0,
              "now_time": 1790550000}),
    ])
    f = tmp_path / "log.txt"
    f.write_text(sample, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))

    koban = [c for c in ledger["changes"] if "小判" in c["delta"]]
    assert len(koban) == 1
    assert koban[0]["delta"] == {"小判": 250}
    assert koban[0]["before"] == {"小判": None}
    assert koban[0]["after"] == {"小判": None}
    assert koban[0]["via"] == ["任务奖励"]
    # item 里的资源条目不重复计（resource 块差值已覆盖）
    res = [c for c in ledger["changes"] if "木炭" in c["delta"]]
    assert len(res) == 1 and res[0]["delta"] == {"木炭": 400}


def test_reading_event_points_and_consumables():
    """活动点数（point 块）和道具库存（item 为 dict）都进读数。"""
    reading = youzu_log._reading_from_payload({
        "resource": {"charcoal": 100},
        "point": {"10031": 20496},
        "item": {"1_0": {"consumable_id": "1", "num": "32"},
                 "8_0": {"consumable_id": "8", "num": "388"},
                 "6103_0": {"consumable_id": "6103", "num": "2"},
                 "99999_0": {"consumable_id": "99999", "num": "7"}},
    })
    assert reading["活动点数·10031"] == 20496
    # ITEM_NAMES 已校准的用真名（2026-09-28 CU 道具页逐页对上 +
    # 三次 diff 实验，当日 117 种全部锤死）
    assert reading["御守"] == 32
    assert reading["加速符"] == 388
    assert reading["狮子螺钿鞍碎片"] == 2
    # 没校准的新道具保持「道具#N」，不硬猜
    assert reading["道具#99999"] == 7


def test_item_list_is_not_a_reading():
    """mission/rewards 的 item 是 list（奖励清单），不是库存读数。"""
    reading = youzu_log._reading_from_payload({
        "item": [{"item_type": 4, "item_id": 0, "item_num": 250}],
    })
    assert reading is None or not any(k.startswith("道具#") for k in reading)


def test_event_points_delta_attribution(tmp_path):
    """活动点数差值走同一条归因链：归给夹在中间的出阵请求。"""
    sample = "\n".join([
        _s2c("2026-09-28 20:00:00", "https://s39-ios-djlw.youzu.com/sally?uid=1",
             {"point": {"10031": 20000}, "status": 0,
              "now_time": 1790568000}),
        _c2s("2026-09-28 20:10:00", "POST",
             "https://s39-ios-djlw.youzu.com/sally/start?uid=1", "field_id=1"),
        _s2c("2026-09-28 20:10:30", "https://s39-ios-djlw.youzu.com/sally?uid=1",
             {"point": {"10031": 20496}, "status": 0,
              "now_time": 1790568630}),
    ])
    f = tmp_path / "log.txt"
    f.write_text(sample, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))
    ch = [c for c in ledger["changes"] if "活动点数·10031" in c["delta"]]
    assert len(ch) == 1
    assert ch[0]["delta"] == {"活动点数·10031": 496}
    assert ch[0]["via"] == ["sally/start"]  # 未收录端点，老实报原名


def test_conquest_complete_exp_ledger(tmp_path):
    """远征完成记两笔经验：审神者（before/after 可反推）+ 刀剑合计。"""
    sample = "\n".join([
        _s2c("2026-09-28 12:00:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"resource": {"charcoal": 100, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "status": 0, "now_time": 1790546400}),
        _c2s("2026-09-28 12:37:47", "POST",
             "https://s39-ios-djlw.youzu.com/conquest/complete?uid=1",
             "party_no=4"),
        _s2c("2026-09-28 12:37:48",
             "https://s39-ios-djlw.youzu.com/conquest/complete?uid=1",
             {"success": 2, "field_id": "5", "party_no": "4",
              "result": {"user_exp": 110, "exp": 41589884, "level": 290},
              "sword": {"1": {"serial_id": "1", "get_exp": 285},
                        "2": {"serial_id": "2", "get_exp": 285}},
              "resource": {"charcoal": 100, "steel": 0, "coolant": 0,
                           "file": 0, "bill": 0},
              "status": 0, "now_time": 1790548668}),
    ])
    f = tmp_path / "log.txt"
    f.write_text(sample, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))
    user = [c for c in ledger["changes"] if "审神者经验" in c["delta"]]
    sword = [c for c in ledger["changes"] if "刀剑经验" in c["delta"]]
    assert len(user) == 1 and user[0]["delta"] == {"审神者经验": 110}
    assert user[0]["before"] == {"审神者经验": 41589774}
    assert user[0]["after"] == {"审神者经验": 41589884}
    assert user[0]["via"][0].startswith("远征完成·四队·B1")
    assert len(sword) == 1 and sword[0]["delta"] == {"刀剑经验": 570}


def test_snapshot_missions_kiwame_events(tmp_path):
    """快照带任务进度、修行归来倒计时、活动日历。"""
    sample = "\n".join([
        _s2c("2026-09-28 11:34:38", "https://s39-ios-djlw.youzu.com/login/start?uid=1",
             {"user_id": 1, "level": "290", "status": 0}),
        _s2c("2026-09-28 11:34:40", "https://s39-ios-djlw.youzu.com/party/list?uid=1",
             {"sword": {"111": {"serial_id": "111", "sword_id": "118",
                                "level": "99"}},
              "party": {}, "status": 0}),
        _s2c("2026-09-28 11:34:45", "https://s39-ios-djlw.youzu.com/home/leave?uid=1",
             {"evolution": {"back": {"0": {"serial_id": 111,
                                           "finished_at": "2026-10-02 11:34:55"}}},
              "status": 0}),
        _s2c("2026-09-28 11:34:46",
             "https://s39-ios-djlw.youzu.com/home/get_all_activity?uid=1",
             {"event": {"0": {"type": 4, "event_id": 14030,
                              "start_at": "2026-09-24 10:00:00",
                              "end_at": "2026-10-15 05:00:00"}},
              "status": 0}),
        _s2c("2026-09-28 13:03:04", "https://s39-ios-djlw.youzu.com/mission/index?uid=1",
             {"mission": {"1": {"mission_id": "1", "value": "2", "status": "3"},
                          "4375": {"mission_id": "4375", "value": "0",
                                   "status": "1"}},
              "status": 0}),
    ])
    f = tmp_path / "log.txt"
    f.write_text(sample, encoding="utf-8")
    snap = youzu_log.build_snapshot(youzu_log.parse_events(f))
    assert snap["missions"] == [
        {"mission_id": 1, "value": 2, "status": 3},
        {"mission_id": 4375, "value": 0, "status": 1}]
    assert snap["kiwame_return"] == [
        {"serial_id": 111, "name": "压切长谷部",
         "finished_at": "2026-10-02 11:34:55"}]
    assert snap["events_calendar"] == [
        {"event_id": 14030, "type": 4,
         "start_at": "2026-09-24 10:00:00", "end_at": "2026-10-15 05:00:00"}]
    # 展示层：修行倒计时和活动日历进文本摘要
    text = youzu_log.format_summary(snap)
    assert "修行中：压切长谷部" in text
    assert "活动 14030" in text


def test_expedition_map_label_fallback():
    # sequential 连排口径（真实报文唯一在用的）：1=A1、5=B1、20=E4
    assert youzu_log._expedition_map_label("1").startswith("A1")
    assert youzu_log._expedition_map_label("5").startswith("B1")
    assert youzu_log._expedition_map_label("20").startswith("E4")
    # 连排口径下 21 超出 E4=20，宁可显示原值也不硬猜
    assert youzu_log._expedition_map_label("21") == "field#21"
    # era_slot 口径仅用于解读那个恒为 21 的 conquest 粘性子对象
    assert youzu_log._expedition_map_label("21", scheme="era_slot") \
        .startswith("B1")
    # 妖魔鬼怪的 field_id 不硬猜，老实显示原值
    assert youzu_log._expedition_map_label("99") == "field#99"


def test_write_ledger_idempotent(tmp_path):
    from touken.telemetry import TelemetryStore
    f = tmp_path / "log.txt"
    f.write_text(LEDGER_SAMPLE, encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))

    store = TelemetryStore(tmp_path / "telemetry.db")
    state = tmp_path / "state.json"
    r1 = youzu_log.write_ledger(store, ledger, state_path=state)
    assert r1["observations_written"] == 3
    # 远征 +1 资源、锻刀 -5 资源 → 6 条 resource.change
    assert r1["changes_written"] == 6

    # 同一份账本再写一遍：全部跳过
    r2 = youzu_log.write_ledger(store, ledger, state_path=state)
    assert r2["observations_written"] == 0
    assert r2["changes_written"] == 0

    # 账房聚合视角：归因都在，且是 confirmed
    agg = store.resource_ledger(0, 2_000_000_000)
    labels = [a["label"] for a in agg["attributions"]]
    assert any("远征完成" in x and "木炭" in x for x in labels)
    assert any("锻刀开炉" in x and "委托符" in x for x in labels)
    assert all(a["confidence"] == "confirmed" for a in agg["attributions"])


# ---------------------------------------------------------------- 验伤/消歧

def _injury_log(tmp_path, ts, members_hp, slots=("111", "222")):
    """造一份带 party/list 的日志：members_hp = {serial: (hp, hp_max, sword_id)}"""
    sword = {sid: {"serial_id": sid, "sword_id": sid_cfg[2],
                   "level": "99", "hp": sid_cfg[0], "hp_max": sid_cfg[1],
                   "fatigue": "100", "protect": "1", "item_id": "1"}
             for sid, sid_cfg in members_hp.items()}
    slot = {str(i + 1): {"serial_id": sid} for i, sid in enumerate(slots)}
    lines = [
        _s2c(ts, "https://x/party/list?uid=1",
             {"sword": sword,
              "party": {"1": {"party_no": "1", "status": "1",
                              "party_name": "主力", "slot": slot,
                              "finished_at": None}},
              "status": 0}),
    ]
    f = tmp_path / "injury.log"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


def test_injury_tier_boundaries():
    """分档阈值（2026-09-28 CU 编队页实拍校准：60%/61% 中伤，94%/96% 无章）。"""
    t = youzu_log.injury_tier
    assert t(79, 79) is None        # 满血
    assert t(53, 79) == "轻伤"      # 67% > 2/3
    assert t(52, 79) == "中伤"      # 66% ≤ 2/3
    assert t(27, 79) == "中伤"      # 34%（实拍：莺丸 26/77=34% 挂中伤）
    assert t(26, 79) == "重伤"      # ≤ 1/3 保守线
    assert t(0, 79) == "重伤"
    assert t(-1, 79) is None and t(10, 0) is None


def test_party_injury_report(tmp_path):
    f = _injury_log(tmp_path, "2026-09-28 18:00:00",
                    {"111": ("45", "45", "118"), "222": ("30", "79", "118")})
    report = youzu_log.party_injury_report(youzu_log.parse_events(f), 1)
    assert report["max_injury"] == "中伤"
    assert report["observed_at"] == "2026-09-28 18:00:00"
    hurt = [m for m in report["members"] if m["injury"]]
    assert len(hurt) == 1 and hurt[0]["serial_id"] == 222
    assert hurt[0]["hp"] == 30 and hurt[0]["omamori"] == 1
    assert hurt[0]["label"]  # 同名两振，必须给消歧标签
    # 不存在的部队
    assert youzu_log.party_injury_report(youzu_log.parse_events(f), 9) is None


def test_party_injury_report_missing_member_is_unknown(tmp_path):
    """成员在刀池里查不到 = 数据不全，返回 None 回退视觉链，不放行。"""
    f = _injury_log(tmp_path, "2026-09-28 18:00:00",
                    {"111": ("45", "45", "118")}, slots=("111", "999"))
    assert youzu_log.party_injury_report(youzu_log.parse_events(f), 1) is None


def test_dup_labels():
    swords = [
        {"serial_id": 20475841, "name": "压切长谷部", "level": 99},
        {"serial_id": 31970026, "name": "压切长谷部", "level": 1},
        {"serial_id": 31970076, "name": "压切长谷部", "level": 1},
        {"serial_id": 31959534, "name": "蜂须贺虎彻", "level": 1},
    ]
    labels = youzu_log.dup_labels(swords)
    assert labels[31959534] == "蜂须贺虎彻"          # 独占名字用原名
    assert labels[20475841] == "压切长谷部·Lv99"     # 等级能区分
    assert labels[31970026] == "压切长谷部·2号机"    # 等级也撞按 serial 排序
    assert labels[31970076] == "压切长谷部·3号机"


def test_home_situation_members_carry_label(tmp_path):
    """主页快照的成员带消歧 label 和 serial_tail，name/level 原样不动。"""
    lines = [
        _s2c("2026-09-28 18:00:00", "https://x/login/start?uid=1",
             {"level": "290", "secretary": "118", "status": 0}),
        _s2c("2026-09-28 18:00:01", "https://x/party/list?uid=1",
             {"sword": {"111": {"serial_id": "111", "sword_id": "118",
                                "level": "99", "hp": "45", "hp_max": "45",
                                "fatigue": "100", "protect": "1"},
                        "222": {"serial_id": "222", "sword_id": "118",
                                "level": "99", "hp": "30", "hp_max": "79",
                                "fatigue": "60", "protect": "1"}},
              "party": {"1": {"party_no": "1", "status": "1",
                              "party_name": "主力",
                              "slot": {"1": {"serial_id": "111"},
                                       "2": {"serial_id": "222"}},
                              "finished_at": None}},
              "status": 0}),
    ]
    f = tmp_path / "home.log"
    f.write_text("\n".join(lines), encoding="utf-8")
    situation = youzu_log.build_home_situation(youzu_log.parse_events(f))
    members = situation["parties"][0]["members"]
    assert members[0]["label"].endswith("号机")  # 同名同等级 → 号机
    assert members[0]["serial_tail"] == "111"
    assert members[0]["name"] and members[0]["level"] == 99
    # 血量/疲劳/伤势档位随成员走，主页渲染直接可用
    assert members[0]["hp"] == 45 and members[0]["injury"] is None
    assert members[1]["hp"] == 30 and members[1]["hp_max"] == 79
    assert members[1]["fatigue"] == 60
    assert members[1]["injury"] == "中伤"
    # 这份日志没进过本丸（无 /home），资源宁可缺省不冒充
    assert situation["resources"] is None
    assert situation["resources_observed_at"] is None
    # 无 /sally、无手入、无内番：一律空而不编
    assert situation["event_points"] == []
    assert situation["event_points_observed_at"] is None
    assert situation["repair"] == []
    assert situation["duty"] is None


def test_home_situation_collects_repair_duty_and_event_points(tmp_path):
    """手入/内番/活动点数进近况；手入名字走消歧标签。"""
    lines = [
        _s2c("2026-09-28 18:00:00", "https://x/login/start?uid=1",
             {"level": "290", "secretary": "118", "status": 0}),
        _s2c("2026-09-28 18:00:01", "https://x/home?uid=1",
             {"resource": {"charcoal": 100}, "currency": {"money": "500"},
              "duty": {"type": "3", "finished_at": "2026-09-28 21:00:00"},
              "status": 0}),
        _s2c("2026-09-28 18:00:02", "https://x/party/list?uid=1",
             {"sword": {"111": {"serial_id": "111", "sword_id": "118",
                                "level": "99", "hp": "30", "hp_max": "79"}},
              "party": {"1": {"party_no": "1", "status": "1",
                              "slot": {"1": {"serial_id": "111"}},
                              "finished_at": None}},
              "status": 0}),
        _s2c("2026-09-28 18:00:03", "https://x/home/situation?uid=1",
             {"party": {}, "forge": {},
              "repair": [{"slot_no": "1", "serial_id": "111",
                          "finished_at": "2026-09-28 20:30:00"}],
              "status": 0}),
        _s2c("2026-09-28 18:00:04", "https://x/sally?uid=1",
             {"point": {"10031": 20496, "99999": 0}, "status": 0}),
    ]
    f = tmp_path / "home.log"
    f.write_text("\n".join(lines), encoding="utf-8")
    situation = youzu_log.build_home_situation(youzu_log.parse_events(f))
    assert situation["repair"] == [
        {"slot_no": 1, "finished_at": "2026-09-28 20:30:00",
         "name": "压切长谷部"}]
    assert situation["repair_observed_at"] == "2026-09-28 18:00:03"
    assert situation["duty"] == {"finished_at": "2026-09-28 21:00:00"}
    assert situation["duty_observed_at"] == "2026-09-28 18:00:01"
    # 0 点的活动不上主页
    assert situation["event_points"] == [{"event_id": "10031",
                                          "points": 20496}]
    assert situation["event_points_observed_at"] == "2026-09-28 18:00:04"


# ---------------------------------------------------------------- 出阵链日志验伤

from datetime import datetime as _dt

from touken.flows.battle import BattleMixin


class _FakeMaa:
    adb_path = "fake-adb"
    adb_address = "127.0.0.1:0"


class _Host(BattleMixin):
    pass


def _host(cfg=None):
    h = _Host()
    h.config = cfg if cfg is not None else {
        "injury_check": {"use_youzu_log": True, "max_age_sec": 600}}
    h.maa = _FakeMaa()
    return h


def _fake_pull(path):
    def pull(*a, **kw):
        pull.calls += 1
        return path
    pull.calls = 0
    return pull


def test_log_injury_status_fresh(tmp_path, monkeypatch):
    ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    f = _injury_log(tmp_path, ts,
                    {"111": ("45", "45", "118"), "222": ("30", "79", "118")})
    monkeypatch.setattr(youzu_log, "pull_log", _fake_pull(f))
    h = _host()
    cache = {}
    injury, detail = h._log_injury_status(1, cache)
    assert injury == "中伤"
    assert "hp30/79" in detail
    # 同一条链内复检复用缓存，不重复 pull
    h._log_injury_status(1, cache)
    assert youzu_log.pull_log.calls == 1
    # 原始日志阅后即焚
    assert not f.exists()


def test_log_injury_status_stale_falls_back(tmp_path, monkeypatch):
    f = _injury_log(tmp_path, "2020-01-01 00:00:00",
                    {"111": ("45", "45", "118"), "222": ("30", "79", "118")})
    monkeypatch.setattr(youzu_log, "pull_log", _fake_pull(f))
    h = _host()
    assert h._log_injury_status(1, {}) == (None, None)


def test_log_injury_status_pull_failure_falls_back(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("adb 不在")
    monkeypatch.setattr(youzu_log, "pull_log", boom)
    h = _host()
    assert h._log_injury_status(1, {}) == (None, None)


def test_log_injury_status_disabled_by_config(monkeypatch):
    monkeypatch.setattr(youzu_log, "pull_log", _fake_pull(None))
    h = _host({"injury_check": {"use_youzu_log": False}})
    assert h._log_injury_status(1, {}) == (None, None)
    assert youzu_log.pull_log.calls == 0


def test_combined_injury_uses_current_game_hp(tmp_path, monkeypatch):
    """完整新鲜血量作准，画面误识别不能覆盖。"""
    ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    f = _injury_log(tmp_path, ts,
                    {"111": ("45", "45", "118"), "222": ("45", "45", "118")})
    monkeypatch.setattr(youzu_log, "pull_log", _fake_pull(f))
    h = _host()
    monkeypatch.setattr(h, "_team_injury_status", lambda cfg: "重伤")
    injury, detail = h._combined_injury_status({}, 1, {})
    assert injury is None
    assert detail == "全员满血"


def test_noise_request_cannot_own_direct_receipt(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join([
        _s2c("2026-09-28 13:00:00", "https://example.test/home", {"currency": {"money": "1000"}}),
        _c2s("2026-09-28 13:01:00", "POST", "https://example.test/party/setsword", ""),
        _c2s("2026-09-28 13:01:01", "POST", "https://example.test/sally/parallelpastrecovercost", ""),
        _s2c("2026-09-28 13:01:02", "https://example.test/sally/parallelpastrecovercost", {"currency": {"money": "500"}}),
    ]), encoding="utf-8")
    change = youzu_log.build_ledger(youzu_log.parse_events(f))["changes"][0]
    assert change["source_endpoint"] == "/sally/parallelpastrecovercost"
    assert change["via"] == ["异去恢复探索次数"]


def test_sparse_resource_reads_preserve_all_intervening_actions(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join([
        _s2c("2026-09-28 13:00:00", "https://example.test/home", {"currency": {"money": "1000"}, "resource": {"charcoal": 100}}),
        _c2s("2026-09-28 13:01:00", "POST", "https://example.test/artifact/buybindingagent", ""),
        _s2c("2026-09-28 13:01:01", "https://example.test/artifact/buybindingagent", {"resource": {"charcoal": 100}}),
        _c2s("2026-09-28 13:02:00", "POST", "https://example.test/sally/parallelpastrecovercost", ""),
        _s2c("2026-09-28 13:02:01", "https://example.test/sally/parallelpastrecovercost", {"currency": {"money": "500"}}),
    ]), encoding="utf-8")
    change = youzu_log.build_ledger(youzu_log.parse_events(f))["changes"][0]
    assert change["source_endpoint"] is None
    assert change["attribution"] == "inferred"
    assert change["candidate_endpoints"] == ["/artifact/buybindingagent", "/sally/parallelpastrecovercost"]


def test_reward_without_balance_is_still_an_exact_receipt(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(_s2c("2026-09-28 13:00:00", "https://example.test/mission/rewards",
        {"item": [{"item_type": 4, "item_num": 250}]}), encoding="utf-8")
    change = youzu_log.build_ledger(youzu_log.parse_events(f))["changes"][0]
    assert change["delta"] == {"小判": 250}
    assert change["source_endpoint"] == "/mission/rewards"
    assert change["before"]["小判"] is None


def test_legacy_conflicting_signin_label_is_not_reassigned_as_fact():
    source, label = youzu_log.translate_ledger_source(
        "youzu_log.sally/parallelpastsally", "签到 三所物·狮子碎片 +1")
    assert source == "unknown.youzu_log"
    assert label == "来源待确认"


def test_explicit_reward_keeps_mixed_balance_remainder_unknown(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join([
        _s2c("2026-09-28 13:00:00", "https://example.test/home", {"resource": {"charcoal": 100}}),
        _c2s("2026-09-28 13:01:00", "POST", "https://example.test/composition/compose", ""),
        _s2c("2026-09-28 13:02:00", "https://example.test/mission/rewards", {
            "resource": {"charcoal": 140},
            "item": [{"item_type": 5, "item_id": 2, "item_num": 50}]}),
        _s2c("2026-09-28 13:03:00", "https://example.test/home", {"resource": {"charcoal": 140}}),
    ]), encoding="utf-8")
    changes = youzu_log.build_ledger(youzu_log.parse_events(f))["changes"]
    receipt = next(c for c in changes if c["source_endpoint"] == "/mission/rewards")
    remainder = next(c for c in changes if c["source_endpoint"] is None)
    assert receipt["delta"] == {"木炭": 50}
    assert receipt["evidence"] == "client_reward_list"
    assert remainder["delta"] == {"木炭": -10}
    assert remainder["attribution"] == "inferred"
    assert len(changes) == 2


def test_calibrated_operation_labels():
    for endpoint in ("/sally/recovercost", "/sword/dismantle_many", "/shop/buy", "/sign"):
        source = youzu_log.ledger_change_source(endpoint, [{"endpoint": endpoint}])
        assert source["attribution"] == "confirmed"
        assert source["source_endpoint"] == endpoint


def test_speedup_inventory_and_reward_are_one_resource(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join([
        _s2c("2026-09-28 13:00:00", "https://example.test/forge", {
            "item": {"8": {"consumable_id": 8, "num": 390}}}),
        _c2s("2026-09-28 13:01:00", "POST", "https://example.test/forge/fastmultiple", "slot_no=1"),
        _s2c("2026-09-28 13:01:01", "https://example.test/forge/fastmultiple", {
            "assist_item_id": 8, "assist_item_num": "380"}),
        _s2c("2026-09-28 13:02:00", "https://example.test/mission/rewards", {
            "item": [{"item_type": 1, "item_id": 8, "item_num": 3}]}),
        _s2c("2026-09-28 13:03:00", "https://example.test/forge", {
            "item": {"8": {"consumable_id": 8, "num": 383}}}),
    ]), encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))
    changes = [c for c in ledger["changes"] if "加速符" in c["delta"]]
    assert [c["delta"]["加速符"] for c in changes] == [-10, 3]
    assert changes[0]["source_endpoint"] == "/forge/fastmultiple"
    assert not any(o["reading"].get("加速符") == 383 for o in ledger["observations"][:-1])


def test_inbox_receipt_uses_only_confirmed_serials_and_keeps_remainder(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join([
        _s2c("2026-09-28 13:00:00", "https://example.test/home", {"resource": {"charcoal": 100}}),
        _s2c("2026-09-28 13:00:10", "https://example.test/receive/list", {"receive": {
            "a": {"serial_id": "a", "item_type": 5, "item_id": 2, "item_num": 1000},
            "b": {"serial_id": "b", "item_type": 5, "item_id": 2, "item_num": 2000}}}),
        _c2s("2026-09-28 13:01:00", "POST", "https://example.test/composition/compose", ""),
        _s2c("2026-09-28 13:02:00", "https://example.test/receive/get", {
            "serial_ids": ["a"], "resource": {"charcoal": 1090}}),
        _s2c("2026-09-28 13:03:00", "https://example.test/receive/get", {
            "serial_ids": ["a"], "resource": {"charcoal": 1090}}),
    ]), encoding="utf-8")
    changes = youzu_log.build_ledger(youzu_log.parse_events(f))["changes"]
    assert len(changes) == 2
    assert changes[0]["delta"] == {"木炭": 1000}
    assert changes[0]["evidence"] == "client_inbox_receipt"
    assert changes[1]["delta"] == {"木炭": -10}
    assert changes[1]["source_endpoint"] is None


def test_forge_receipt_materials_do_not_infer_discounted_bills(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join([
        _s2c("2026-09-28 13:00:00", "https://example.test/home", {
            "resource": {"charcoal": 10000, "bill": 100}}),
        _c2s("2026-09-28 13:00:20", "POST", "https://example.test/forge/fastmultiple", ""),
        _c2s("2026-09-28 13:01:00", "POST", "https://example.test/forge/startmultiple", "charcoal=700"),
        _s2c("2026-09-28 13:01:01", "https://example.test/forge/startmultiple", {
            "multiple": [{"finished_at": "later"}] * 10,
            "resource": {"charcoal": 3000, "bill": 91}}),
    ]), encoding="utf-8")
    changes = youzu_log.build_ledger(youzu_log.parse_events(f))["changes"]
    assert changes[0]["delta"] == {"木炭": -7000}
    assert changes[0]["source_endpoint"] == "/forge/startmultiple"
    assert changes[0]["evidence"] == "client_forge_recipe"
    assert changes[1]["delta"] == {"委托符": -9}
    assert changes[1]["source_endpoint"] is None


def test_observations_do_not_retimestamp_sparse_old_balances(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join([
        _s2c("2026-09-28 13:00:00", "https://example.test/home", {
            "resource": {"charcoal": 100}, "currency": {"money": 1000}}),
        _s2c("2026-09-28 13:01:00", "https://example.test/sally", {"currency": {"money": 1000}}),
    ]), encoding="utf-8")
    obs = youzu_log.build_ledger(youzu_log.parse_events(f))["observations"]
    assert len(obs) == 2
    assert obs[-1]["reading"] == {"小判": 1000}


def test_client_assets_and_complete_item_inventory_are_sanitized(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(_s2c("2026-09-28 13:00:00", "https://example.test/sally", {
        "item": {"1": {"consumable_id": 1, "num": 2}},
        "equip": {"e": {"serial_id": "e", "equip_id": 1, "soldier": 10, "secret": "never-store"}},
        "artifact": {"a": {"serial_id": "a", "artifact_id": 1, "level": 3, "usage_score": 100}},
        "sword": {"s": {"serial_id": "s", "horse_serial_id": "h", "item_id": 1}},
    }), encoding="utf-8")
    ledger = youzu_log.build_ledger(youzu_log.parse_events(f))
    assert ledger["observations"][0]["reading"]["小判箱·大"] == 0
    assert ledger["observations"][0]["reading"]["御守"] == 2
    assert ledger["assets"][0]["equip"][0] == {"serial_id": "e", "equip_id": 1, "soldier": 10}
    assert ledger["assets"][0]["sword"][0]["horse_serial_id"] == "h"


def test_new_asset_observations_backfill_without_rewriting_ledger(tmp_path):
    from touken.telemetry import TelemetryStore
    import json
    store = TelemetryStore(tmp_path / "db")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_ts": 1000}), encoding="utf-8")
    ledger = {"observations": [], "changes": [], "assets": [{"ts": 900, "equip": [{"serial_id": 1}]}]}
    youzu_log.write_ledger(store, ledger, state)
    youzu_log.write_ledger(store, ledger, state)
    assert store._conn().execute("SELECT count(*) FROM events WHERE event_type='game_assets.captured'").fetchone()[0] == 1
    assert json.loads(state.read_text(encoding="utf-8"))["last_ts"] == 1000
    assert store.client_item_inventory()["assets"]["equip"] == [{"serial_id": 1}]
    store.close()


def test_old_unknown_source_keeps_explicit_operation():
    for endpoint, label in [("sword/dismantle_many", "刀解"), ("shop/buy", "万屋购买"), ("sally/recovercost", "补充活动手形")]:
        source, note = youzu_log.translate_ledger_source("youzu_log.unknown", endpoint + " 木炭 +4")
        assert not source.startswith("unknown")
        assert note.startswith(label)
    source, _ = youzu_log.translate_ledger_source("youzu_log.unknown", "来源待确认 木炭 +1550")
    assert source.startswith("unknown")


def test_yosari_daily_ticket_flow_is_one_source(tmp_path):
    f = tmp_path / "log.txt"
    lines = [_s2c("2026-10-03 03:23:00", "https://example.test/sally", {
        "item": {"ticket": {"consumable_id": 6005, "num": 3}}})]
    for index, endpoint in enumerate(["parallelpastsally", "parallelpaststartup", "parallelpastforward"]):
        lines.append(_c2s(f"2026-10-03 03:23:0{index + 1}", "POST", "https://example.test/sally/" + endpoint, ""))
    lines.append(_s2c("2026-10-03 03:24:00", "https://example.test/sally", {
        "item": {"ticket": {"consumable_id": 6005, "num": 2}}}))
    f.write_text("\n".join(lines), encoding="utf-8")
    changes = youzu_log.build_ledger(youzu_log.parse_events(f))["changes"]
    ticket = next(c for c in changes if c["delta"].get("归城提灯五"))
    assert ticket["source_endpoint"] == "/sally/parallelpastsally"
    assert ticket["delta"]["归城提灯五"] == -1


def test_receipt_source_repair_is_backed_up_and_idempotent(tmp_path):
    import json, sqlite3
    from touken.telemetry import TelemetryStore
    store = TelemetryStore(tmp_path / "telemetry.db")
    conn = store._conn()
    old = {"resource": "木炭", "delta": 1550, "source": "youzu_log.unknown", "before": 10, "after": 1560}
    conn.execute("INSERT INTO events(ts,run_id,script,event_type,payload) VALUES(100,'kept','youzu_log','resource.change',?)", (json.dumps(old),))
    conn.commit()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_ts": 200}), encoding="utf-8")
    ledger = {"observations": [], "changes": [{"ts": 100, "delta": {"木炭": 1550}, "source_endpoint": "/mission/rewards", "via": ["任务奖励"], "attribution": "confirmed", "evidence": "client_reward_list"}]}
    assert youzu_log.write_ledger(store, ledger, state)["sources_repaired"] == 1
    assert youzu_log.write_ledger(store, ledger, state)["sources_repaired"] == 0
    rows = conn.execute("SELECT run_id,payload FROM events").fetchall()
    assert len(rows) == 1 and rows[0]["run_id"] == "kept"
    updated = json.loads(rows[0]["payload"])
    assert updated["source"] == "youzu_log.mission/rewards"
    assert updated["delta"] == 1550 and updated["before"] == 10 and updated["after"] == 1560
    backups = list((tmp_path / "backups").glob("*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert json.loads(backup.execute("SELECT payload FROM events").fetchone()[0])["source"] == "youzu_log.unknown"
    store.close()


def test_raid_points_flow_has_gameplay_source(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("\n".join([
        _s2c("2026-10-03 03:00:00", "https://example.test/sally", {"point": {"10031": 1000}}),
        _c2s("2026-10-03 03:00:01", "POST", "https://example.test/sally/eventsally", ""),
        _c2s("2026-10-03 03:00:02", "POST", "https://example.test/battle/alloutbattle", ""),
        _s2c("2026-10-03 03:03:00", "https://example.test/sally", {"point": {"10031": 1756}}),
    ]), encoding="utf-8")
    changes = youzu_log.build_ledger(youzu_log.parse_events(f))["changes"]
    reward = next(c for c in changes if "活动点数·10031" in c["delta"])
    assert reward["source_endpoint"] == "/battle/alloutbattle"
    assert reward["delta"]["活动点数·10031"] == 756
