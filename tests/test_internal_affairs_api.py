# -*- coding: utf-8 -*-
"""内番养成视图 API：/api/data/training/internal-affairs。

plateau 三态（契约见 docs/telemetry-data.md「内番养成视图」）：快照链上
最近连续 K（=3）条不增长 → true；不足 K 条有效读数 → null（判不出）；
最近还在涨 → false。true 是平台期启发式（可能喂满也可能没喂），不是
「到官方上限」。
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


def _sw(serial, sword_id=3, hp_up=None, scout_up=None):
    row = {"serial_id": serial, "sword_id": sword_id, "level": 1, "exp": 0,
           "ranbu_level": 1, "ranbu_exp": 0}
    if hp_up is not None:
        row["hp_up"] = hp_up
    if scout_up is not None:
        row["scout_up"] = scout_up
    return row


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


def _five_frames(store):
    """5 帧快照铺出 plateau 各态（字段缺读数的帧不进该指标的比较链）：
    1 号 hp 涨→平（末 3 条 10,10,10 → true）、scout 一路涨（false）；
    2 号只有 2 帧（null）；3 号 hp 早平（true）、scout 缺帧凑链（false）；
    4 号 hp 全缺（null）、scout 恰 K 条仍在涨（false）；5 号中途离开刀池。
    """
    frames = [
        [_sw(1, hp_up=5, scout_up=1), _sw(3, hp_up=1, scout_up=2),
         _sw(4, sword_id=65), _sw(5, hp_up=1)],
        [_sw(1, hp_up=8, scout_up=2), _sw(3, hp_up=2),
         _sw(4, sword_id=65, scout_up=1), _sw(5, hp_up=1)],
        [_sw(1, hp_up=10, scout_up=3), _sw(3, hp_up=2, scout_up=5),
         _sw(4, sword_id=65, scout_up=2)],
        [_sw(1, hp_up=10, scout_up=4), _sw(2, hp_up=3, scout_up=1),
         _sw(3, hp_up=2), _sw(4, sword_id=65, scout_up=3)],
        [_sw(1, hp_up=10, scout_up=5), _sw(2, hp_up=4, scout_up=2),
         _sw(3, hp_up=2, scout_up=6), _sw(4, sword_id=65)],
    ]
    for i, swords in enumerate(frames):
        _training(store, sh(f"2026-10-0{i+1} 20:00:00"), swords)


def test_plateau_three_states(client, store):
    _five_frames(store)
    body = client.get("/api/data/training/internal-affairs").json()
    assert body["plateau_k"] == 3
    assert body["sword_count"] == 4
    rows = {row["serial_id"]: row for row in body["swords"]}
    assert sorted(rows) == [1, 2, 3, 4]  # serial 升序，5 号已离开刀池不出

    one = rows[1]
    assert one["hp_up"] == 10 and one["scout_up"] == 5
    assert one["hp_plateau"] is True      # 5→8→10→10→10，末 3 条不增长
    assert one["scout_plateau"] is False  # 1→2→3→4→5 还在涨
    assert one["name"] == "三日月宗近"

    two = rows[2]
    assert two["hp_up"] == 4
    assert two["hp_plateau"] is None      # 只有 2 帧快照，判定不出
    assert two["scout_plateau"] is None

    three = rows[3]
    assert three["hp_plateau"] is True     # 1→2→2→2→2，末 3 条 2,2,2
    # scout 缺 2 帧，比较链只收非空读数 [2,5,6]：还在涨 → false；
    # 最新值照实展示 6（缺读数的帧不当 0 凑数）
    assert three["scout_up"] == 6
    assert three["scout_plateau"] is False

    four = rows[4]
    assert four["name"] == "蜻蛉切"
    assert four["hp_up"] is None           # 从来没读到过，诚实 null 不编 0
    assert four["hp_plateau"] is None
    # scout 最新帧没带读数 → 展示 null；比较链 [1,2,3]（第 2~4 帧）恰够
    # K=3 且仍在涨 → false
    assert four["scout_up"] is None
    assert four["scout_plateau"] is False


def test_growth_after_plateau_is_not_plateau(client, store):
    # 停 3 条之后又涨了：末 3 条 5,6,7 → false（平台期被新增长打破）
    for i, value in enumerate([9, 5, 5, 5, 6, 7]):
        _training(store, sh(f"2026-10-{i+1:02d} 20:00:00"),
                  [_sw(1, hp_up=value, scout_up=value)])
    row = client.get("/api/data/training/internal-affairs").json()["swords"][0]
    assert row["hp_plateau"] is False
    assert row["scout_plateau"] is False


def test_departed_sword_not_in_roster(client, store):
    # 1 号中途离开刀池（最新快照没有它）：不出现在视图里
    _training(store, sh("2026-10-01 20:00:00"), [_sw(1, hp_up=1)])
    _training(store, sh("2026-10-02 20:00:00"), [_sw(2, hp_up=2)])
    body = client.get("/api/data/training/internal-affairs").json()
    assert [row["serial_id"] for row in body["swords"]] == [2]
    assert body["sword_count"] == 1


def test_response_shape(client, store):
    captured = _training(store, sh("2026-10-02 20:00:00"),
                         [_sw(1, hp_up=3)])
    body = client.get("/api/data/training/internal-affairs").json()
    assert body["captured_at"] == captured
    assert body["ts"] == sh("2026-10-02 20:00:00")
    row = body["swords"][0]
    assert set(row) == {"serial_id", "sword_id", "name", "hp_up", "scout_up",
                        "hp_plateau", "scout_plateau", "captured_at"}
    assert row["scout_up"] is None and row["scout_plateau"] is None
    assert row["hp_plateau"] is None  # 单帧快照，判定不出


def test_404_without_any_snapshot(client):
    resp = client.get("/api/data/training/internal-affairs")
    assert resp.status_code == 404
