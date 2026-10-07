# -*- coding: utf-8 -*-
"""touken/jp_ledger.py：日服抓包事务 -> 账房事件。

payload 全手工构造；落账后用 daily_report 直接读，验证格式兼容。
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from touken import daily_report, jp_ledger, sword_db
from touken.netlog import Transaction
from touken.telemetry import TelemetryStore

SH = timezone(timedelta(hours=8))
NOW_JST = "2026-10-07 03:48:16"          # 报文里的服务器时间（UTC+9）
NOW_EPOCH = datetime(2026, 10, 7, 3, 48, 16,
                     tzinfo=jp_ledger.JST).timestamp()
# 换算成北京时刻是同日 02:48，日报应记在北京时间的 2026-10-07
DAY_SH = "2026-10-07"


@pytest.fixture
def store():
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    handle = TelemetryStore(Path(temp.name) / "telemetry.db")
    yield handle
    handle.close()
    temp.cleanup()


def _tx(path: str, payload, body: bytes | None = None,
        ts: float = NOW_EPOCH) -> Transaction:
    raw = payload if isinstance(payload, bytes) else json.dumps(
        payload).encode("utf-8")
    return netlog_tx(path, raw, body, ts)


def netlog_tx(path, raw, body, ts):
    return Transaction(
        url=f"https://w007.example.jp{path}?uid=42",
        method="POST", path=f"{path}?uid=42",
        request_line=f"POST {path}?uid=42 HTTP/1.1",
        response_body=raw, ts=ts, request_body=body)


def _home(charcoal=100, steel=50):
    return {"now": NOW_JST, "resource": {
        "charcoal": charcoal, "steel": steel, "coolant": 30,
        "file": 20, "bill": 5, "max_resource": 13800,
        "recovered_at": NOW_JST}}


def test_inventory_snapshot_lands_with_server_time(store):
    stats = jp_ledger.import_transactions(store, [_tx("/home/index",
                                                      _home())])
    assert stats["inventory.captured"] == 1
    row = store._conn().execute(
        "SELECT ts, event_type, payload FROM events").fetchone()
    assert row["event_type"] == "inventory.captured"
    assert abs(row["ts"] - NOW_EPOCH) < 1  # 用服务器时间，不用导入时刻
    payload = json.loads(row["payload"])
    assert payload["resources"]["木炭"] == 100
    assert payload["resources"]["小判"] == 5
    assert "max_resource" not in payload["resources"]  # 非资源键不进快照


def test_identical_snapshots_deduped(store):
    txs = [_tx("/home/index", _home()),
           _tx("/party/list", {**_home(), "sword": {}}),
           _tx("/home/index", _home(charcoal=200))]
    stats = jp_ledger.import_transactions(store, txs)
    assert stats["inventory.captured"] == 2
    assert stats["dedup_snapshots"] == 1


def test_training_snapshot_and_daily_report_compatible(store):
    swords = {
        "900001": {"serial_id": 900001, "sword_id": 3, "level": 90,
                   "exp": 1275576, "ranbu_level": 2, "ranbu_exp": 200,
                   "fatigue": 46, "hp_up": 7, "atk_up": 16, "def_up": 14,
                   "mobile_up": 0, "back_up": 8, "scout_up": 3,
                   "hide_up": 0},
    }
    jp_ledger.import_transactions(
        store, [_tx("/party/list", {"now": NOW_JST, "sword": swords})])
    # 日报直接读得到练度快照（北京时间 02:48，记在前一天）
    report = daily_report.build_daily_report(store, DAY_SH)
    assert report["training"] is not None
    assert report["training"]["sword_count"] == 1


def test_forge_recipe_and_result(store):
    body = (b"sword=abc&t=def&slot_no=1&charcoal=350&steel=350"
            b"&coolant=350&file=350")
    start = _tx("/forge", {"now": NOW_JST, "status": 0}, body=body)
    done = _tx("/forge/complete", {
        "now": NOW_JST, "sword_id": 3, "serial_id": 900999,
        "is_new_sword": False, "is_first_get_sword": False})
    stats = jp_ledger.import_transactions(store, [start, done])
    assert stats["resource.change"] == 4
    assert stats["forge.started"] == 1
    assert stats["forge.collected"] == 1

    rows = store._conn().execute(
        "SELECT event_type, payload FROM events ORDER BY id").fetchall()
    changes = [json.loads(r["payload"]) for r in rows
               if r["event_type"] == "resource.change"]
    assert all(c["delta"] == -350 for c in changes)
    assert {c["resource"] for c in changes} == {"木炭", "玉钢", "冷却材", "砥石"}
    assert all(c["attribution"] == "confirmed" for c in changes)

    started = [json.loads(r["payload"]) for r in rows
               if r["event_type"] == "forge.started"][0]
    assert started["slot_no"] == 1
    assert started["charcoal"] == 350

    collected = [json.loads(r["payload"]) for r in rows
                 if r["event_type"] == "forge.collected"][0]
    sword = collected["swords"][0]
    expected = sword_db.find_by_id(3)[1]
    assert sword["name"] == (expected.get("name_zh") or expected["name"])
    assert sword["is_first_get_sword"] is False

    # 端到端：日报掉落小节直接读得出这振锻刀
    report = daily_report.build_daily_report(store, DAY_SH)
    assert report["drops"] is not None


def test_encrypted_battle_skipped(store):
    stats = jp_ledger.import_transactions(
        store, [_tx("/battle/battle", b'{"data":"aa55","now":"%s"}'
                    % NOW_JST.encode())])
    assert stats["skipped_encrypted"] == 1
    assert store._conn().execute(
        "SELECT COUNT(*) c FROM events").fetchone()["c"] == 0


def _roster_payload(count, level=10):
    return {"now": NOW_JST, "sword": {
        str(serial): {"serial_id": serial, "sword_id": 3, "level": level,
                      "exp": level * 100, "ranbu_level": 1, "ranbu_exp": 0}
        for serial in range(1, count + 1)}}


def test_partial_updates_preserve_full_roster_and_same_name_instances(store):
    from touken.training_view import build_training_overview, build_internal_affairs
    session = jp_ledger.JpLedgerSession(store, script="jp_listener")
    session.feed(_tx("/party/list", _roster_payload(150)))
    session.feed(_tx("/party/getpartyinfo", _roster_payload(6, level=20)))
    result = build_training_overview(store)
    assert result["sword_count"] == 150
    assert result["roster_complete"] is True
    assert result["swords"][0]["level"] == 20
    assert result["swords"][6]["level"] == 10
    assert build_internal_affairs(store)["sword_count"] == 150
    assert daily_report.build_daily_report(store, DAY_SH)["training"]["sword_count"] == 150
    # 新完整名单仍能确认刀解或离开所持名单的成员。
    session.feed(_tx("/party/list", _roster_payload(149)))
    assert build_training_overview(store)["sword_count"] == 149
    session.feed(_tx("/party/getpartyinfo", _roster_payload(150, level=30)))
    assert build_training_overview(store)["sword_count"] == 149


def test_legacy_snapshots_recovered_without_rewriting_history(store):
    from touken.training_view import build_training_overview
    for count, level in ((150, 10), (6, 20)):
        swords = jp_ledger.jp_import.sword_roster(_roster_payload(count, level))
        jp_ledger._record(store, NOW_EPOCH, "training.captured",
                          {"source": "jp_netlog", "swords": swords}, "jp_netlog")
    result = build_training_overview(store)
    assert result["sword_count"] == 150
    assert result["roster_complete"] is False
    assert result["swords"][0]["level"] == 20
    assert result["swords"][6]["level"] == 10
    raw = store._conn().execute(
        "SELECT payload FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert len(json.loads(raw["payload"])["swords"]) == 6


def test_complete_scope_is_not_deduplicated_with_identical_partial(store):
    from touken.training_view import build_training_overview
    session = jp_ledger.JpLedgerSession(store)
    session.feed(_tx("/party/getpartyinfo", _roster_payload(6)))
    assert build_training_overview(store)["roster_complete"] is False
    session.feed(_tx("/party/list", _roster_payload(6)))
    assert session.stats["training.captured"] == 2
    assert build_training_overview(store)["roster_complete"] is True
    session.feed(_tx("/party/list", _roster_payload(0)))
    assert build_training_overview(store)["sword_count"] == 0


def test_partial_missing_fields_preserve_previous_reading(store):
    from touken.training_view import build_internal_affairs
    session = jp_ledger.JpLedgerSession(store)
    full = _roster_payload(1)
    full["sword"]["1"]["hp_up"] = 7
    session.feed(_tx("/party/list", full))
    partial = _roster_payload(1)
    session.feed(_tx("/party/getpartyinfo", partial))
    assert build_internal_affairs(store)["swords"][0]["hp_up"] == 7
    partial["sword"]["1"]["fatigue"] = 80
    session.feed(_tx("/party/getpartyinfo", partial))
    assert session.stats["training.captured"] == 3


def test_payload_without_now_not_recorded(store):
    payload = {"resource": {"charcoal": 1}}  # 没有 now 字段
    stats = jp_ledger.import_transactions(
        store, [_tx("/home/index", payload)])
    assert stats["inventory.captured"] == 0
    assert stats["skipped_no_now"] == 1
