"""日服余额解析、备份校正及隔离升级演练，全部使用合成数据。"""
import json
import sqlite3

import pytest

from touken.jp_import import resource_reading
from touken.jp_resource_repair import repair_legacy_resources
from touken.telemetry import TelemetryStore


def test_all_balances_and_sparse_fields():
    assert resource_reading({"resource": {"bill": 54, "charcoal": 95519},
        "currency": {"money": "185304", "point": 0, "point_free": 0},
        "item": {"8": {"consumable_id": 8, "num": 19}}}) == {
        "委托符": 54, "木炭": 95519, "小判": 185304, "甲州金": 0, "加速符": 19}
    assert resource_reading({"currency": {"point": 3}}) is None
    assert resource_reading({"item": [{"consumable_id": 8, "num": 19}]}) is None
    assert resource_reading({"item": {}}) is None
    assert resource_reading({"resource": {"bill": True, "steel": -1, "coolant": 1.5}}) is None
    assert resource_reading({"currency": {"money": 0}}) == {"小判": 0}
    assert resource_reading({"assist_item_id": 8, "assist_item_num": 0}) == {"加速符": 0}


def test_currency_only_response_and_current_inventory(tmp_path):
    from touken.jp_ledger import JpLedgerSession
    from touken.netlog import Transaction
    store = TelemetryStore(tmp_path / "jp.db")
    session = JpLedgerSession(store)
    for path, payload in [
        ("/login/start", {"currency": {"money": 185304, "point": 0, "point_free": 0},
                          "item": {"8": {"consumable_id": 8, "num": 19}}}),
        ("/home/index", {"resource": {"bill": 54, "charcoal": 95519, "steel": 91867,
                                        "coolant": 94682, "file": 94984}}),
    ]:
        payload["now"] = "2026-10-07 15:33:45"
        session.feed(Transaction(url=path, path=path, method="POST", request_line=None,
                                 response_body=json.dumps(payload).encode()))
    resources = store.client_item_inventory()["resources"]
    assert len(resources) == 8
    assert resources["小判"]["count"] == 185304
    assert resources["委托符"]["count"] == 54
    assert resources["加速符"]["count"] == 19
    assert resources["甲州金"]["count"] == 0
    assert repair_legacy_resources(store)["updated"] == 0
    store.close()


def insert(store, script, resources, version=None):
    payload = {"resources": resources, "captured_at": "2026-10-07 14:30:00"}
    if version is not None:
        payload["resource_schema"] = version
    store._conn().execute(
        "INSERT INTO events(ts, script, event_type, payload) VALUES (1, ?, 'inventory.captured', ?)",
        (script, json.dumps(payload)))
    store._conn().commit()


def test_upgrade_backup_and_rollback(tmp_path):
    store = TelemetryStore(tmp_path / "jp.db")
    insert(store, "jp_listener", {"小判": 54, "木炭": 95519})
    insert(store, "jp_netlog", {"小判": 49})
    insert(store, "jp_listener", {"小判": 185304}, 2)
    insert(store, "youzu_log", {"小判": 123})
    result = repair_legacy_resources(store)
    assert result["updated"] == 2
    rows = [json.loads(r[0])["resources"] for r in store._conn().execute("SELECT payload FROM events ORDER BY id")]
    assert rows == [{"委托符": 54, "木炭": 95519}, {"委托符": 49}, {"小判": 185304}, {"小判": 123}]
    assert repair_legacy_resources(store) == {"updated": 0, "backup": None}
    with sqlite3.connect(result["backup"]) as original:
        assert json.loads(original.execute("SELECT payload FROM events ORDER BY id").fetchone()[0])["resources"]["小判"] == 54
        # 在隔离库演练完整恢复，不覆盖正在运行的库。
        with sqlite3.connect(tmp_path / "rollback.db") as recovered:
            original.backup(recovered)
    with sqlite3.connect(tmp_path / "rollback.db") as recovered:
        assert recovered.execute("SELECT count(*) FROM events").fetchone()[0] == 4
    store.close()


def test_conflicting_legacy_labels_stop_without_changes(tmp_path):
    store = TelemetryStore(tmp_path / "jp.db")
    insert(store, "jp_netlog", {"小判": 54, "委托符": 12})
    with pytest.raises(ValueError):
        repair_legacy_resources(store)
    assert not list(tmp_path.glob("*.bak"))
    assert json.loads(store._conn().execute("SELECT payload FROM events").fetchone()[0])["resources"]["小判"] == 54
    store.close()


def test_failed_update_rolls_back_and_keeps_backup(tmp_path):
    store = TelemetryStore(tmp_path / "jp.db")
    insert(store, "jp_listener", {"小判": 54})
    insert(store, "jp_netlog", {"小判": 49})
    store._conn().execute("CREATE TRIGGER stop_second BEFORE UPDATE ON events WHEN OLD.id=2 BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repair_legacy_resources(store)
    assert all("小判" in json.loads(row[0])["resources"] for row in store._conn().execute("SELECT payload FROM events"))
    assert len(list(tmp_path.glob("*.bak"))) == 1
    store.close()
