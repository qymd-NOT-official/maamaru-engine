# -*- coding: utf-8 -*-
"""touken/jp_listener.py：URL 过滤（钉游戏域名）+ 实时喂账的去重与计数。

websocket/CDP 连接本身不进单测（要真浏览器）；这里测纯函数和
JpListener._ingest 的喂账路径。
"""

import json
import tempfile
from pathlib import Path

import pytest

from touken import jp_import, jp_ledger, jp_listener
from touken.netlog import Transaction
from touken.telemetry import TelemetryStore

NOW_JST = "2026-10-07 03:48:16"


@pytest.fixture
def store():
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    handle = TelemetryStore(Path(temp.name) / "telemetry.db")
    yield handle
    handle.close()
    temp.cleanup()


def _home_tx(charcoal=100):
    payload = {"now": NOW_JST, "resource": {
        "charcoal": charcoal, "steel": 50, "coolant": 30,
        "file": 20, "bill": 5}}
    return Transaction(
        url="https://w007.touken-ranbu.jp/home/index?uid=42",
        method="POST", path="/home/index?uid=42",
        request_line=None, response_body=json.dumps(payload).encode(),
        ts=0.0, request_body=None)


def test_interesting_pins_game_host():
    card = jp_import.load_card()
    hit = jp_listener._interesting(
        "https://w007.touken-ranbu.jp/home/index?uid=42", card)
    assert hit == "/home/index"
    # 同路径但不在游戏域名：不收
    assert jp_listener._interesting(
        "https://evil.example.com/home/index", card) is None
    # 游戏域名但数据卡之外的端点：不收
    assert jp_listener._interesting(
        "https://w007.touken-ranbu.jp/no/such/endpoint", card) is None


def test_session_dedupes_across_separate_feeds(store):
    """实时听包一条一条喂：连续相同快照跨 feed 也要去重。"""
    session = jp_ledger.JpLedgerSession(store, script="jp_listener")
    session.feed(_home_tx())
    session.feed(_home_tx())          # 同一份资源，第二条去重
    session.feed(_home_tx(charcoal=200))
    assert session.stats["inventory.captured"] == 2
    assert session.stats["dedup_snapshots"] == 1


def test_listener_ingest_counts(store):
    """_ingest：听到一条计一条，落账条数单独计（去重的不算入账）。"""
    listener = jp_listener.JpListener(launch=False)
    listener._ledger = jp_ledger.JpLedgerSession(store, script="jp_listener")
    listener._ingest(_home_tx())
    listener._ingest(_home_tx())      # 去重，不入账
    listener._ingest(_home_tx(charcoal=300))
    status = listener.status()
    assert status["transactions"] == 3
    assert status["events_written"] == 2
    assert status["last_capture_at"] is not None
    # 账房里确实是两条资源快照
    rows = store._conn().execute(
        "SELECT payload FROM events WHERE event_type='inventory.captured'"
    ).fetchall()
    assert len(rows) == 2
    assert json.loads(rows[-1]["payload"])["resources"]["木炭"] == 300
