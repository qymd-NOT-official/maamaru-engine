# -*- coding: utf-8 -*-
"""活动点数历史 API：/api/data/event-points。

断言口径（契约见 docs/telemetry-data.md「活动点数历史」与
touken/event_points.py docstring）：列表只认最新一条 activity.calendar、
时间线切片窗 [start_at, end_at+1天]（结束后 1 天内算收尾读数，start 前
无宽限）、非数字读数跳过不补 0、event_id 原文匹配、起止时间两种写法都
认/都认不出 400、日历里没有该活动 404。
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from touken import telemetry
from touken.telemetry import TelemetryStore

SH = timezone(timedelta(hours=8))
START = "2026-10-01 00:00:00"
END = "2026-10-08 23:59:59"


def sh(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


def _event(store, ts, event_type, payload):
    store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) VALUES (?,?,?,?,?)",
        (ts, None, "youzu_log", event_type,
         json.dumps(payload, ensure_ascii=False)))
    store._conn().commit()


def _calendar(store, ts, events):
    _event(store, ts, "activity.calendar", {"events": events})


def _inventory(store, ts, resources):
    _event(store, ts, "inventory.captured",
           {"captured_at": datetime.fromtimestamp(ts, SH).strftime("%Y-%m-%d %H:%M:%S"),
            "resources": resources})


def _point_reading(store, text, event_id="10031", points=100):
    ts = sh(text)
    _inventory(store, ts, {f"活动点数·{event_id}": points})
    return ts


@pytest.fixture
def store():
    # TestClient 会把路由跑进工作线程，那边的 sqlite 连接不在本线程的
    # thread-local 里关不掉；Windows 锁文件，清理临时目录时允许留残渣。
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    handle = TelemetryStore(temp.name + "/telemetry.db")
    yield handle
    handle.close()
    temp.cleanup()


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(telemetry, "get_telemetry_store", lambda: store)
    from panel import server
    return TestClient(server.app)


def _seed_calendar(store):
    _calendar(store, sh("2026-09-28 08:00:00"), [
        {"event_id": "10030", "type": 1, "start_at": "2026-09-01 00:00:00",
         "end_at": "2026-09-07 23:59:59"}])
    _calendar(store, sh("2026-10-01 08:00:00"), [
        {"event_id": "10031", "type": 1, "start_at": START, "end_at": END},
        {"event_id": "10032", "type": 2, "start_at": "2026-10-10 00:00:00",
         "end_at": "2026-10-12 23:59:59"}])


def test_list_returns_latest_calendar_only(client, store):
    _seed_calendar(store)
    body = client.get("/api/data/event-points").json()
    assert body["observed_at"] == sh("2026-10-01 08:00:00")
    assert [e["event_id"] for e in body["events"]] == ["10031", "10032"]
    assert body["events"][0] == {"event_id": "10031", "type": 1,
                                 "start_at": START, "end_at": END}
    assert body["schema_version"] == telemetry.TELEMETRY_SCHEMA_VERSION


def test_list_empty_without_calendar(client, store):
    body = client.get("/api/data/event-points").json()
    assert body["observed_at"] is None
    assert body["events"] == []


def test_timeline_slicing_with_end_grace(client, store):
    _seed_calendar(store)
    before = _point_reading(store, "2026-09-30 23:59:59")      # start 前：无宽限，剔除
    at_start = _point_reading(store, "2026-10-01 00:00:00", points=100)
    at_end = _point_reading(store, "2026-10-08 23:59:59", points=500)
    in_grace = _point_reading(store, "2026-10-09 12:00:00", points=700)
    after_grace = _point_reading(store, "2026-10-10 00:00:00", points=800)
    body = client.get("/api/data/event-points?event_id=10031").json()
    assert body["event_id"] == "10031"
    assert body["window"]["grace_seconds"] == 86400
    assert body["window"]["from_ts"] == sh(START)
    assert body["window"]["to_ts"] == sh(END) + 86400
    assert [(p["ts"], p["points"]) for p in body["timeline"]] == [
        (at_start, 100), (at_end, 500), (in_grace, 700)]
    assert before not in [p["ts"] for p in body["timeline"]]
    assert after_grace not in [p["ts"] for p in body["timeline"]]
    assert body["timeline"][0]["captured_at"] == "2026-10-01 00:00:00"


def test_timeline_skips_unreadable_points(client, store):
    _seed_calendar(store)
    _inventory(store, sh("2026-10-02 08:00:00"),
               {"活动点数·10031": 300})
    _inventory(store, sh("2026-10-03 08:00:00"),
               {"活动点数·10031": "abc"})       # 怪值：跳过不编
    _inventory(store, sh("2026-10-04 08:00:00"),
               {"活动点数·10031": -5})          # 负数：跳过
    _inventory(store, sh("2026-10-05 08:00:00"),
               {"木炭": 999})                    # 没点数键：跳过
    body = client.get("/api/data/event-points?event_id=10031").json()
    assert [(p["ts"], p["points"]) for p in body["timeline"]] == [
        (sh("2026-10-02 08:00:00"), 300)]


def test_timeline_unknown_event_id_404(client, store):
    _seed_calendar(store)
    resp = client.get("/api/data/event-points?event_id=99999")
    assert resp.status_code == 404


def test_timeline_unparseable_times_400(client, store):
    _calendar(store, sh("2026-10-01 08:00:00"), [
        {"event_id": "10031", "type": 1, "start_at": "不是时间", "end_at": END}])
    resp = client.get("/api/data/event-points?event_id=10031")
    assert resp.status_code == 400


def test_timeline_iso_times_accepted(client, store):
    _calendar(store, sh("2026-10-01 08:00:00"), [
        {"event_id": "10031", "type": 1,
         "start_at": "2026-10-01T00:00:00+08:00",
         "end_at": "2026-10-08T23:59:59+08:00"}])
    _point_reading(store, "2026-10-05 08:00:00", points=42)
    body = client.get("/api/data/event-points?event_id=10031").json()
    assert [p["points"] for p in body["timeline"]] == [42]
