import json

from touken.telemetry import TelemetryStore


def test_screen_fills_stale_json_but_does_not_override_current_json(tmp_path):
    store = TelemetryStore(tmp_path / "stock.db")
    for ts, script, resources in [(1000, "youzu_log", {"小判": 100, "木炭": 50}),
                                  (1650, "youzu_log", {"木炭": 60}),
                                  (1700, "game_inventory", {"小判": 200, "木炭": 999})]:
        store._conn().execute("INSERT INTO events(ts,script,event_type,payload) VALUES(?,?,'inventory.captured',?)",
                              (ts, script, json.dumps({"resources": resources})))
    store._conn().commit()
    stock = store.client_item_inventory(1701)["resources"]
    assert stock["小判"] == {"count": 200, "observed_at": 1700, "source": "screen"}
    assert stock["木炭"] == {"count": 60, "observed_at": 1650}
    store.close()


def test_stock_uses_latest_actual_client_count_without_window_or_manual_override(tmp_path):
    store = TelemetryStore(tmp_path / "stock.db")
    for ts, script, resources in [(10, "youzu_log", {"小判": 100, "木炭": 25}),
                                  (20, "youzu_log", {"小判": 0}),
                                  (30, "manual", {"小判": 999})]:
        store._conn().execute("INSERT INTO events(ts,script,event_type,payload) VALUES(?,?,'inventory.captured',?)",
                              (ts, script, json.dumps({"resources": resources})))
    store._conn().commit()
    stock = store.client_item_inventory(10000000)
    assert stock["resources"] == {"小判": {"count": 0, "observed_at": 20},
                                   "木炭": {"count": 25, "observed_at": 10}}
    assert "玉钢" not in stock["resources"]
    assert store.client_item_inventory(15)["resources"]["小判"]["count"] == 100
    store.close()


def test_missing_client_resource_uses_actual_screen_read_only(tmp_path):
    store = TelemetryStore(tmp_path / "stock.db")
    for ts, script, resources in [(10, "youzu_log", {"小判": 100}),
                                  (20, "game_inventory", {"小判": 999, "加速符": 211}),
                                  (30, "manual", {"加速符": 555})]:
        store._conn().execute("INSERT INTO events(ts,script,event_type,payload) VALUES(?,?,'inventory.captured',?)",
                              (ts, script, json.dumps({"resources": resources})))
    store._conn().commit()
    stock = store.client_item_inventory(100)
    assert stock["resources"]["小判"]["count"] == 100
    assert stock["resources"]["加速符"] == {"count": 211, "observed_at": 20, "source": "screen"}
    assert "玉钢" not in stock["resources"]
    store.close()


def test_old_client_speedup_name_remains_client_fact(tmp_path):
    store = TelemetryStore(tmp_path / "stock.db")
    for ts, script, resources in [(10, "youzu_log", {"加速符·极": 207}),
                                  (20, "game_inventory", {"加速符": 211})]:
        store._conn().execute("INSERT INTO events(ts,script,event_type,payload) VALUES(?,?,'inventory.captured',?)",
                              (ts, script, json.dumps({"resources": resources})))
    store._conn().commit()
    assert store.client_item_inventory(30)["resources"]["加速符"] == {"count": 207, "observed_at": 10}
    row = next(r for r in store.resource_ledger(15, 30)["per_resource"] if r["resource"] == "加速符")
    assert (row["opening"], row["closing"]) == (207, 211)
    store.close()
