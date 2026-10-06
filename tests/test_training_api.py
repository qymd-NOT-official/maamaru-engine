# -*- coding: utf-8 -*-
"""练度档案 API：/api/data/training/overview 与 /api/data/training/history/{serial_id}。

直接写 training.captured 事件（与收账管线落库同结构），TestClient 打路由；
乱舞换算断言对照 data/ranbu_rules.json（髭切/膝丸特例、满级 null、
100 习合值/振未实测的估算口径）。
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from touken import telemetry
from touken.telemetry import TelemetryStore

SH = timezone(timedelta(hours=8))


def sh(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


def _sw(serial, sword_id, level=1, exp=0, ranbu_level=1, ranbu_exp=0):
    return {"serial_id": serial, "sword_id": sword_id, "level": level, "exp": exp,
            "ranbu_level": ranbu_level, "ranbu_exp": ranbu_exp}


def _training(store, ts, swords):
    captured_at = datetime.fromtimestamp(ts, SH).strftime("%Y-%m-%d %H:%M:%S")
    store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) VALUES (?, NULL, ?, ?, ?)",
        (ts, "youzu_log", "training.captured",
         json.dumps({"captured_at": captured_at, "source": "youzu_log",
                     "swords": swords}, ensure_ascii=False)))
    store._conn().commit()
    return captured_at


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


def test_overview_404_without_any_snapshot(client):
    resp = client.get("/api/data/training/overview")
    assert resp.status_code == 404


def test_overview_latest_snapshot_with_ranbu_next(client, store):
    _training(store, sh("2026-10-01 20:00:00"),
              [_sw(1, 3, level=99, exp=12345678, ranbu_level=1, ranbu_exp=150),
               _sw(2, 4, level=80, exp=1000),          # 三日月·极
               _sw(3, 108, level=50, exp=500, ranbu_level=1, ranbu_exp=0),  # 髭切
               _sw(4, 65, level=30, exp=100, ranbu_level=2, ranbu_exp=700),  # 蜻蛉切
               _sw(5, 3, level=99, exp=999, ranbu_level=10, ranbu_exp=3900)])  # 乱舞满级
    captured = _training(store, sh("2026-10-02 20:00:00"),
                         [_sw(1, 3, level=99, exp=12345679, ranbu_level=1, ranbu_exp=250),
                          {"sword_id": 3, "level": 1},  # 缺 serial_id，跳过
                          _sw(6, 99999, level=1, exp=0, ranbu_level=1, ranbu_exp=0)])
    resp = client.get("/api/data/training/overview")
    assert resp.status_code == 200
    body = resp.json()
    # 只取最新一条快照
    assert body["captured_at"] == captured
    assert body["ts"] == sh("2026-10-02 20:00:00")
    assert body["sword_count"] == 2
    assert [row["serial_id"] for row in body["swords"]] == [1, 6]

    first = body["swords"][0]
    assert first["name"] == "三日月宗近"
    assert first["level"] == 99 and first["exp"] == 12345679
    # 稀有 5 到 Lv2 阈值 200，已有 250 → 钳 0
    assert first["ranbu_next"] == {"need_exp": 0, "need_swords_est": 0}
    assert first["captured_at"] == captured

    unknown = body["swords"][1]
    assert unknown["name"] == "刀帐99999"
    assert unknown["ranbu_next"] is None  # 名册查不到稀有度，诚实 null


def test_overview_resolves_kiwame_and_genji_exception(client, store):
    _training(store, sh("2026-10-02 20:00:00"),
              [_sw(2, 4, level=80, exp=1000),           # 三日月·极（极化番号）
               _sw(3, 108, level=50, exp=500, ranbu_level=1, ranbu_exp=0),   # 髭切
               _sw(4, 111, level=50, exp=500, ranbu_level=4, ranbu_exp=500),  # 髭切·极
               _sw(5, 112, level=50, exp=500, ranbu_level=1, ranbu_exp=0)])   # 膝丸
    rows = {row["serial_id"]: row for row in
            client.get("/api/data/training/overview").json()["swords"]}
    assert rows[2]["name"] == "三日月宗近·极"
    # 髭切：名册稀有 2，乱舞特例按稀有 4 → 到 Lv2 差 200（est 2 振）
    assert rows[3]["name"] == "髭切"
    assert rows[3]["ranbu_next"] == {"need_exp": 200, "need_swords_est": 2}
    # 髭切·极：极化映射回基础目录，特例同样生效；稀有 4 Lv4→Lv5 阈值 1100
    assert rows[4]["name"] == "髭切·极"
    assert rows[4]["ranbu_next"] == {"need_exp": 600, "need_swords_est": 6}
    # 膝丸同特例
    assert rows[5]["name"] == "膝丸"
    assert rows[5]["ranbu_next"] == {"need_exp": 200, "need_swords_est": 2}


def test_overview_max_level_and_null_fields(client, store):
    _training(store, sh("2026-10-02 20:00:00"),
              [_sw(1, 3, level=99, exp=1, ranbu_level=10, ranbu_exp=3900),
               _sw(2, 65, level=None, exp=None, ranbu_level=None, ranbu_exp=None)])
    rows = {row["serial_id"]: row for row in
            client.get("/api/data/training/overview").json()["swords"]}
    assert rows[1]["ranbu_next"] is None  # 乱舞 Lv10 满级
    assert rows[2]["level"] is None and rows[2]["exp"] is None
    assert rows[2]["ranbu_next"] is None  # 等级读不出，诚实 null


def test_history_timeline_and_first_max_observed(client, store):
    first = _training(store, sh("2026-10-01 20:00:00"),
                      [_sw(7, 3, level=97, exp=100, ranbu_level=1, ranbu_exp=0),
                       _sw(8, 3, level=99, exp=200, ranbu_level=2, ranbu_exp=200)])
    second = _training(store, sh("2026-10-02 20:00:00"),
                       [_sw(7, 3, level=98, exp=150),
                        _sw(8, 3, level=99, exp=260, ranbu_level=3, ranbu_exp=400)])
    third = _training(store, sh("2026-10-03 20:00:00"),
                      [_sw(7, 3, level=99, exp=180),
                       _sw(9, 65, level=10, exp=10)])  # 新刀，history 里没它

    resp = client.get("/api/data/training/history/7")
    assert resp.status_code == 200
    body = resp.json()
    assert body["serial_id"] == 7
    assert [row["ts"] for row in body["timeline"]] == [
        sh("2026-10-01 20:00:00"), sh("2026-10-02 20:00:00"),
        sh("2026-10-03 20:00:00")]
    assert [row["level"] for row in body["timeline"]] == [97, 98, 99]
    assert body["timeline"][0]["captured_at"] == first
    # 首次「观测到」99 是第三次快照，不是实际达成那次（无从考证）
    assert body["first_max_level_observed_at"] == third

    eight = client.get("/api/data/training/history/8").json()
    assert len(eight["timeline"]) == 2
    # 第一帧就观测到 99：首次观测 = 第一帧（实际达成只可能更早，无从考证）
    assert eight["first_max_level_observed_at"] == first

    nine = client.get("/api/data/training/history/9").json()
    assert len(nine["timeline"]) == 1
    assert nine["first_max_level_observed_at"] is None
    assert client.get("/api/data/training/history/40404").status_code == 404
