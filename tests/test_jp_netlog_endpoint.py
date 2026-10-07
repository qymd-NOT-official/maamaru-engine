# -*- coding: utf-8 -*-
"""日服抓包导入端点 + server=jp 账房分流。

netlog 报文全手工构造（假域名/假数据）；monkeypatch 把日服库换到
临时目录，不碰真实用户数据。
"""

import base64
import gzip
import json
import tempfile
import time
from pathlib import Path

import pytest

from touken.telemetry import TelemetryStore

NOW_JST = "2026-10-07 03:48:16"


@pytest.fixture
def jp_store():
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    handle = TelemetryStore(Path(temp.name) / "telemetry.db")
    yield handle
    handle.close()
    temp.cleanup()


def _netlog_bytes(host: str = "w007.touken-ranbu.jp") -> bytes:
    """手工攒一份最小合法 net-export：一条 /home/index 往返。"""
    body = json.dumps({"now": NOW_JST, "resource": {
        "charcoal": 100, "steel": 50, "coolant": 30, "file": 20,
        "bill": 5}}).encode()
    req = (f"POST /home/index?uid=42 HTTP/1.1\r\nHost: {host}\r\n\r\n"
           .encode())
    doc = {"constants": {"logEventTypes": {
        "URL_REQUEST_START_JOB": 901,
        "URL_REQUEST_JOB_FILTERED_BYTES_READ": 902,
        "SSL_SOCKET_BYTES_SENT": 903,
    }}, "events": [
        {"time": "100", "type": 901, "source": {"id": 10},
         "params": {"url": f"https://{host}/home/index?uid=42"}},
        {"time": "99", "type": 903, "source": {"id": 500},
         "params": {"bytes": base64.b64encode(req).decode()}},
        {"time": "101", "type": 902, "source": {"id": 10},
         "params": {"bytes": base64.b64encode(
             gzip.compress(body)).decode()}},
    ]}
    return json.dumps(doc).encode()


def _client(jp_store, monkeypatch):
    from fastapi.testclient import TestClient
    from panel import server
    from touken import telemetry
    monkeypatch.setattr(
        telemetry, "get_jp_telemetry_store", lambda: jp_store)
    return TestClient(server.app)


def test_import_endpoint_lands_events_in_jp_store(jp_store, monkeypatch):
    client = _client(jp_store, monkeypatch)
    resp = client.post("/api/data/jp-netlog-import?filename=netlog.json",
                       content=_netlog_bytes(),
                       headers={"Content-Type": "application/octet-stream"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["stats"]["inventory.captured"] == 1
    assert data["summary"]["resource"]["木炭"] == 100

    # 落进的是日服库，日报 server=jp 直接读得到
    report = client.get("/api/daily_report?server=jp&date=2026-10-07")
    assert report.status_code == 200
    assert report.json()["resources"] is not None


def test_import_rejects_garbage(jp_store, monkeypatch):
    client = _client(jp_store, monkeypatch)
    resp = client.post("/api/data/jp-netlog-import",
                       content=b"not a netlog",
                       headers={"Content-Type": "application/octet-stream"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_import_rejects_log_without_game_traffic(jp_store, monkeypatch):
    client = _client(jp_store, monkeypatch)
    resp = client.post("/api/data/jp-netlog-import",
                       content=_netlog_bytes(host="example.com"),
                       headers={"Content-Type": "application/octet-stream"})
    assert resp.status_code == 400
    assert "没找到" in resp.json()["reason"]


def test_cn_report_unaffected_by_jp_import(jp_store, monkeypatch):
    # 国服库也换成空的临时库，隔离本机真实数据
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    cn_store = TelemetryStore(Path(temp.name) / "telemetry.db")
    from touken import telemetry
    monkeypatch.setattr(
        telemetry, "get_telemetry_store", lambda: cn_store)
    client = _client(jp_store, monkeypatch)
    try:
        client.post("/api/data/jp-netlog-import", content=_netlog_bytes(),
                    headers={"Content-Type": "application/octet-stream"})
        # 国服日报（默认 server）读的是国服库，不会被日服数据污染
        report = client.get("/api/daily_report?date=2026-10-07")
        assert report.status_code == 200
        assert report.json()["resources"] is None
    finally:
        cn_store.close()
        temp.cleanup()


def _seed_jp_training(jp_store):
    jp_store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) "
        "VALUES (?, NULL, 'jp_netlog', 'training.captured', ?)",
        (1790000000.0, json.dumps({
            "captured_at": "2026-10-07 12:00:00",
            "swords": [{"serial_id": 123, "sword_id": 3, "level": 50,
                        "exp": 123456, "ranbu_level": 2,
                        "ranbu_exp": 150}],
        }, ensure_ascii=False)))
    jp_store._conn().commit()


def test_warehouse_reads_only_selected_server(jp_store, monkeypatch):
    from touken import telemetry
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    cn_store = TelemetryStore(Path(temp.name) / "telemetry.db")
    monkeypatch.setattr(telemetry, "get_telemetry_store", lambda: cn_store)
    client = _client(jp_store, monkeypatch)
    try:
        client.post("/api/data/jp-netlog-import", content=_netlog_bytes(),
                    headers={"Content-Type": "application/octet-stream"})
        jp = client.get("/api/data/client-inventory?server=jp").json()
        assert jp["resources"]["木炭"]["count"] == 100
        assert jp["resources"]["小判"]["count"] == 5
        assert "委托符" not in jp["resources"]
        assert "source" not in jp["resources"]["木炭"]
        assert client.get("/api/data/client-inventory").json()["resources"] == {}
        assert client.get("/api/data/events?server=jp").json()["items"]
        assert client.get("/api/data/events").json()["items"] == []
        cn_store.start_run("cn-only", "daily")
        jp_store.start_run("jp-only", "jp_listener")
        cn_store.finish_run("cn-only", "success")
        jp_store.finish_run("jp-only", "success")
        assert [row["run_id"] for row in client.get(
            "/api/data/runs?server=jp").json()["items"]] == ["jp-only"]
        assert [row["run_id"] for row in client.get(
            "/api/data/runs").json()["items"]] == ["cn-only"]
        for store, slot in ((cn_store, 1), (jp_store, 2)):
            store._conn().execute(
                "INSERT INTO events(ts, run_id, script, event_type, payload) "
                "VALUES (?, NULL, 'fixture', 'forge.started', ?)",
                (time.time(), json.dumps({"slot_no": slot})))
            store._conn().commit()
        assert client.get("/api/data/forge-history?server=jp").json()["forges"][0]["slot_no"] == 2
        assert client.get("/api/data/forge-history").json()["forges"][0]["slot_no"] == 1
        for endpoint in ("runs", "drop-stats", "forge-history"):
            response = client.get(f"/api/data/{endpoint}?server=jp")
            assert response.status_code == 200
    finally:
        cn_store.close()
        temp.cleanup()


def test_training_overview_follows_server(jp_store, monkeypatch):
    """练度总览按 server 分流：日服库的快照只在 server=jp 时出现。"""
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    cn_store = TelemetryStore(Path(temp.name) / "telemetry.db")
    from touken import telemetry
    monkeypatch.setattr(
        telemetry, "get_telemetry_store", lambda: cn_store)
    client = _client(jp_store, monkeypatch)
    try:
        _seed_jp_training(jp_store)
        jp = client.get("/api/data/training/overview?server=jp")
        assert jp.status_code == 200
        body = jp.json()
        assert body["sword_count"] == 1
        assert body["swords"][0]["serial_id"] == 123
        assert body["swords"][0]["level"] == 50
        # 默认（国服）库没有这条快照；日服数据漏不进国服视图
        cn = client.get("/api/data/training/overview")
        assert cn.status_code == 404
    finally:
        cn_store.close()
        temp.cleanup()
