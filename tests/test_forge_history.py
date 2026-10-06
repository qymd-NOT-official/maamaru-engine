# -*- coding: utf-8 -*-
"""锻刀串链 API：/api/data/forge-history。

断言口径（契约见 docs/telemetry-data.md「锻刀串链」与 touken/forge_history.py
docstring）：slot_no+时间序配对、配不上的两边老实列出（started 无 collected
给 null；collected 无 started 进 orphan_collected）、近侍取开炉前最近一条
登录观测、cost_est 十连×count 且不含委托符/加速符、面板锻刀流 payload
（slot 键无配方）不拿默认配方填、days 窗口按事件 ts 过滤。
"""

import json
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from touken import telemetry
from touken.telemetry import TelemetryStore

SH = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=SH).timestamp()
RECIPE = {"charcoal": 350, "steel": 350, "coolant": 350, "file": 350}


def sh(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


def _event(store, ts, event_type, payload):
    store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) VALUES (?,?,?,?,?)",
        (ts, None, "youzu_log", event_type,
         json.dumps(payload, ensure_ascii=False)))
    store._conn().commit()


def _started(store, ts, slot=1, **recipe):
    _event(store, ts, "forge.started", {"slot_no": slot, **recipe})

def _started_panel(store, ts, slot=1):
    # 面板锻刀流：slot 键、无配方
    _event(store, ts, "forge.started", {"slot": slot, "sequence": 1})

def _collected(store, ts, slot=1, swords=None, count=None):
    payload = {"source": "forge", "slot": slot}
    if swords is not None:
        payload["swords"] = swords
    if count is not None:
        payload["count"] = count
    _event(store, ts, "forge.collected", payload)

def _collected_panel(store, ts, slot=1, name=None, sword_id=None):
    payload = {"slot": slot}
    if name:
        payload["name"] = name
    if sword_id:
        payload["sword_id"] = sword_id
    _event(store, ts, "forge.collected", payload)

def _secretary(store, ts, sword_id):
    _event(store, ts, "secretary.observed", {"sword_id": sword_id})


def _sword(name, sword_id, serial_id, first=False):
    return {"name": name, "sword_id": sword_id, "serial_id": serial_id,
            "is_first_get_sword": first}


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


def test_pairs_by_slot_time_order(client, store):
    _secretary(store, sh("2026-10-01 08:00:00"), 3)      # 三日月
    _started(store, sh("2026-10-01 09:00:00"), slot=1, **RECIPE)
    _started(store, sh("2026-10-01 10:00:00"), slot=2, **RECIPE)
    # 槽 1 收完再点第二次火，槽 2 一直烧着
    _collected(store, sh("2026-10-01 18:00:00"), slot=1,
               swords=[_sword("今剑", 11, 101)], count=1)
    _started(store, sh("2026-10-01 19:00:00"), slot=1, **RECIPE)
    _collected(store, sh("2026-10-02 09:00:00"), slot=1,
               swords=[_sword("小狐丸", 5, 102, first=True)], count=1)

    body = client.get("/api/data/forge-history?days=0").json()
    assert body["window"]["days"] == 0
    assert [f["slot_no"] for f in body["forges"]] == [1, 2, 1]
    assert [f["started_at"] for f in body["forges"]] == sorted(
        f["started_at"] for f in body["forges"])
    first = body["forges"][0]
    assert first["recipe"] == RECIPE
    assert first["collected_at"] == sh("2026-10-01 18:00:00")
    assert first["swords"] == [_sword("今剑", 11, 101)]
    assert first["cost_est"] == RECIPE  # 单把 ×1
    # 开炉前最近的登录观测是三日月
    assert first["secretary"] == {"sword_id": 3, "name": "三日月宗近",
                                  "observed_at": sh("2026-10-01 08:00:00")}
    # 槽 2 没收 → 结果未知，不编
    assert body["forges"][1]["collected_at"] is None
    assert body["forges"][1]["swords"] is None
    assert body["forges"][2]["swords"] == [_sword("小狐丸", 5, 102, first=True)]
    assert body["orphan_collected"] == []


def test_orphan_collected_before_any_started(client, store):
    _collected(store, sh("2026-10-01 18:00:00"), slot=3,
               swords=[_sword("岩融", 9, 103)], count=1)
    _started(store, sh("2026-10-02 09:00:00"), slot=1, **RECIPE)
    body = client.get("/api/data/forge-history?days=0").json()
    assert len(body["forges"]) == 1
    assert body["forges"][0]["collected_at"] is None
    # 配不上的领取老实列出，不硬撮合
    assert body["orphan_collected"] == [
        {"slot_no": 3, "collected_at": sh("2026-10-01 18:00:00"), "count": 1,
         "swords": [_sword("岩融", 9, 103)]}]


def test_secretary_window_nearest_before_start(client, store):
    _secretary(store, sh("2026-10-01 08:00:00"), 3)   # 登录观测：三日月
    _started(store, sh("2026-10-01 09:00:00"), slot=1, **RECIPE)
    _secretary(store, sh("2026-10-01 12:00:00"), 5)   # 换人登录：小狐丸
    _started(store, sh("2026-10-01 13:00:00"), slot=1, **RECIPE)
    # 开炉比任何观测都早 → null，不编
    _started(store, sh("2026-09-01 09:00:00"), slot=2, **RECIPE)
    body = client.get("/api/data/forge-history?days=0").json()
    by_started = {f["started_at"]: f for f in body["forges"]}
    assert by_started[sh("2026-10-01 09:00:00")]["secretary"]["sword_id"] == 3
    assert by_started[sh("2026-10-01 13:00:00")]["secretary"]["sword_id"] == 5
    assert by_started[sh("2026-09-01 09:00:00")]["secretary"] is None


def test_ten_forge_cost_est_multiplies_recipe(client, store):
    _started(store, sh("2026-10-01 09:00:00"), slot=1, **RECIPE)
    _collected(store, sh("2026-10-01 19:00:00"), slot=1,
               swords=[_sword(f"刀{i}", 11, 100 + i) for i in range(10)],
               count=10)
    body = client.get("/api/data/forge-history?days=0").json()
    assert body["forges"][0]["cost_est"] == {k: v * 10 for k, v in RECIPE.items()}


def test_panel_flow_payload_without_recipe(client, store):
    # 面板锻刀流：slot 键、无配方、领取是内联单刀 → 配方 null 不拿默认填
    _started_panel(store, sh("2026-10-01 09:00:00"), slot=1)
    _collected_panel(store, sh("2026-10-01 19:00:00"), slot=1,
                     name="今剑", sword_id=11)
    body = client.get("/api/data/forge-history?days=0").json()
    forge = body["forges"][0]
    assert forge["recipe"] == {"charcoal": None, "steel": None,
                               "coolant": None, "file": None}
    assert forge["cost_est"] is None
    assert forge["swords"] == [{"name": "今剑", "sword_id": 11,
                                "serial_id": None, "is_first_get_sword": None}]


def test_window_filters_events_by_ts(client, store):
    # 炉跨窗口边界：started 在窗外（40 天前），领取在窗内（1 小时前）
    # → 窗内配不成对，领取落 orphan_collected；days=0 全量时配成炉
    _started(store, time.time() - 40 * 86400, slot=1, **RECIPE)
    _collected(store, time.time() - 3600, slot=1,
               swords=[_sword("跨窗刀", 11, 1)], count=1)
    body = client.get("/api/data/forge-history?days=30").json()
    assert body["forges"] == []
    assert len(body["orphan_collected"]) == 1
    assert body["orphan_collected"][0]["swords"] == [_sword("跨窗刀", 11, 1)]
    body_all = client.get("/api/data/forge-history?days=0").json()
    assert len(body_all["forges"]) == 1
    assert body_all["forges"][0]["collected_at"] is not None
    assert body_all["orphan_collected"] == []


def test_cross_slot_pairing_does_not_mix(store):
    _started(store, NOW - 7200, slot=1, **RECIPE)
    _collected(store, NOW - 3600, slot=2,
               swords=[_sword("别槽", 5, 9)], count=1)
    from touken.forge_history import build_forge_history
    body = build_forge_history(store, days=0, now=NOW)
    assert len(body["forges"]) == 1
    assert body["forges"][0]["collected_at"] is None
    assert body["orphan_collected"][0]["slot_no"] == 2


def test_one_started_pairs_only_first_collected(store):
    # 2026-10-06 串炉事故回归：旧数据缺开炉事件，一条 started 不能吞掉
    # 之后全部领取（一炉一领，领取即空槽）；第二条起进 orphan_collected
    _started(store, NOW - 30 * 86400, slot=1, **RECIPE)
    _collected(store, NOW - 20 * 86400, slot=1,
               swords=[_sword("第一炉", 11, 1)], count=1)
    _collected(store, NOW - 10 * 86400, slot=1,
               swords=[_sword(f"串炉刀{i}", 5, 10 + i) for i in range(10)],
               count=10)
    _collected(store, NOW - 5 * 86400, slot=1,
               swords=[_sword(f"串炉刀x{i}", 3, 20 + i) for i in range(20)],
               count=20)
    from touken.forge_history import build_forge_history
    body = build_forge_history(store, days=0, now=NOW)
    forge, = body["forges"]
    assert forge["swords"] == [_sword("第一炉", 11, 1)]
    assert forge["cost_est"] == RECIPE  # ×1，没被串炉放大
    assert [row["count"] for row in body["orphan_collected"]] == [10, 20]


def test_api_shape_and_default_window(client, store):
    _started(store, time.time() - 3600, slot=1, **RECIPE)
    resp = client.get("/api/data/forge-history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"]["days"] == 30
    assert body["window"]["from_ts"] is not None
    assert body["schema_version"] == telemetry.TELEMETRY_SCHEMA_VERSION
    assert set(body) == {"schema_version", "generated_at", "window",
                         "forges", "orphan_collected"}
    assert set(body["forges"][0]) == {"slot_no", "started_at", "recipe",
                                      "secretary", "collected_at", "swords",
                                      "cost_est"}
