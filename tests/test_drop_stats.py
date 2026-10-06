# -*- coding: utf-8 -*-
"""掉落统计 API：/api/data/drop-stats。

断言口径（契约见 docs/telemetry-data.md「掉落统计」与 touken/drop_stats.py
docstring）：双来源分母合并、not_observed 圈剔除、分组键、窗口过滤、
手动侧 boss_reached 一律 null、分子不拿 drops_recognized 重复计、
进不了组的事件进 unattributed。
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


def sh(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


def _event(store, ts, event_type, payload, run_id=None, script="youzu_log"):
    store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) VALUES (?,?,?,?,?)",
        (ts, run_id, script, event_type,
         json.dumps(payload, ensure_ascii=False)))
    store._conn().commit()


def _battle(store, ts, chapter, map_no):
    _event(store, ts, "battle.completed",
           {"chapter": chapter, "map_no": map_no, "team_no": 1,
            "square_id": "A"})


def _drop(store, ts, name, source, *, chapter=None, map_no=None,
          first_get=False):
    _event(store, ts, "sword.obtained",
           {"name": name, "sword_id": 5, "source": source,
            "chapter": chapter, "map_no": map_no,
            "is_first_get_sword": first_get})


def _loop(store, ts, run_id, seq, outcome="completed", chapter=8, map_no=2,
          attempt=1, battle_count=4, drop_observation="confirmed_none",
          end_type="sortie.completed"):
    """一圈 = 一条出发 + 一条结束（end_type/seq 可为 None 测老数据）。"""
    if seq is not None:
        _event(store, ts - 300, "sortie.loop_started",
               {"mode": "sortie", "chapter": chapter, "map_no": map_no,
                "team_no": 1, "sequence": seq, "attempt": attempt},
               run_id=run_id, script="sortie")
    if end_type is not None:
        _event(store, ts, end_type,
               {"mode": "sortie", "chapter": chapter, "map_no": map_no,
                "team_no": 1, "sequence": seq, "attempt": attempt,
                "outcome": outcome, "battle_count": battle_count,
                "drop_observation": drop_observation},
               run_id=run_id, script="sortie")


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


def _group(body, key):
    return next((g for g in body["groups"] if g["key"] == key), None)


def test_manual_battles_and_drops_grouped_by_map(client, store):
    for day in (1, 2, 3):
        _battle(store, sh(f"2026-10-0{day} 20:00:00"), 8, 2)
    _drop(store, sh("2026-10-01 21:00:00"), "太郎太刀", "sortie.drop",
          chapter=8, map_no=2)
    _drop(store, sh("2026-10-02 21:00:00"), "太郎太刀", "sortie.drop",
          chapter=8, map_no=2, first_get=True)
    _drop(store, sh("2026-10-03 21:00:00"), "次郎太刀", "battle.drop",
          chapter=8, map_no=2)

    body = client.get("/api/data/drop-stats?days=0").json()
    group = _group(body, "8-2")
    assert group["kind"] == "map"
    assert group["battles"] == 3
    # 纯手动组：王点未校准，一律 null（不猜）
    assert group["boss_reached"] is None
    assert group["drop_total"] == 3
    # 按 count 降序、name 兜底排序
    assert group["drops"] == [
        {"name": "太郎太刀", "count": 2, "first_get_count": 1},
        {"name": "次郎太刀", "count": 1, "first_get_count": 0},
    ]
    assert body["unattributed"] == {"battles": 0, "drop_total": 0}


def test_panel_loops_merge_and_not_observed_excluded(client, store):
    # 圈 1：completed 4 场、认到 1 掉；drops_recognized 只是数量，分子只认
    # sword.obtained，不许两条口径重复计
    _loop(store, sh("2026-10-05 20:00:00"), "run-a", seq=1, battle_count=4)
    _event(store, sh("2026-10-05 19:40:00"), "sword.obtained",
           {"name": "今剑", "sword_id": 11, "source": "sortie.drop",
            "chapter": 8, "map_no": 2, "sequence": 1, "attempt": 1,
            "is_first_get_sword": False}, run_id="run-a", script="sortie")
    # 圈 2：委托自动行军跳过获得动画 → not_observed，整圈剔除
    _loop(store, sh("2026-10-05 21:00:00"), "run-a", seq=2, battle_count=4,
          drop_observation="not_observed")
    # 手动 1 场同图
    _battle(store, sh("2026-10-06 20:00:00"), 8, 2)

    body = client.get("/api/data/drop-stats?days=0").json()
    group = _group(body, "8-2")
    assert group["battles"] == 5  # 4 + 1，not_observed 圈的 4 场被剔除
    assert group["boss_reached"] == 1  # 只算 completed 圈（剔除的圈不算）
    assert group["drop_total"] == 1  # 分子没有因 drops_recognized 翻倍


def test_retreated_and_blind_loops_battle_basis(client, store):
    _loop(store, sh("2026-10-05 20:00:00"), "run-b", seq=1,
          battle_count=2)                                        # completed 2 场
    _loop(store, sh("2026-10-05 21:00:00"), "run-b", seq=2,
          outcome="retreated_before_boss", battle_count=3,
          end_type="sortie.retreated_before_boss")               # 王点前撤 3 场
    _loop(store, sh("2026-10-05 22:00:00"), "run-b", seq=3,
          outcome="interrupted", battle_count=None,
          end_type="sortie.interrupted")                         # 场数读不出 → 0
    # completed 但 battle_count 数到 0 场 = 锚点失明（写库是 None + note）
    _loop(store, sh("2026-10-05 22:05:00"), "run-b", seq=4,
          battle_count=None)

    body = client.get("/api/data/drop-stats?days=0").json()
    group = _group(body, "8-2")
    assert group["battles"] == 2 + 3 + 0 + 1  # 失明 completed 圈按王点战记 1
    assert group["boss_reached"] == 2  # 两条 completed，含锚点失明那条


def test_boss_reached_zero_when_panel_loops_all_retreated(client, store):
    _loop(store, sh("2026-10-05 20:00:00"), "run-c", seq=1,
          outcome="retreated_before_boss", battle_count=3,
          end_type="sortie.retreated_before_boss")
    body = client.get("/api/data/drop-stats?days=0").json()
    group = _group(body, "8-2")
    assert group["battles"] == 3
    assert group["boss_reached"] == 0  # 观测过圈、0 次到王点：给 0 不给 null


def test_raid_and_osaka_playstyle_groups(client, store):
    _event(store, sh("2026-10-05 20:00:00"), "raid.round_completed",
           {"difficulty": 3, "sequence": 1, "battle_taps": 6, "triple": False},
           run_id="run-r", script="raid")
    _drop(store, sh("2026-10-05 19:50:00"), "物吉贞宗", "raid.drop",
          first_get=True)
    _event(store, sh("2026-10-06 20:00:00"), "osaka.floor_completed",
           {"completed": 2, "target": 50, "selected_floor": 88},
           run_id="run-o", script="osaka")
    _event(store, sh("2026-10-06 21:00:00"), "osaka.floor_completed",
           {"completed": 3, "target": 50, "selected_floor": 88},
           run_id="run-o", script="osaka")
    _drop(store, sh("2026-10-06 20:30:00"), "博多藤四郎", "osaka.drop",
          first_get=True)
    # 非掉落来源的 sword.obtained 不进分子
    _drop(store, sh("2026-10-06 22:00:00"), "宗三左文字", "forge")

    body = client.get("/api/data/drop-stats?days=0").json()
    raid = _group(body, "联队战")
    assert raid["kind"] == "raid"
    assert raid["battles"] == 6  # battle_taps，一戳一场
    assert raid["boss_reached"] is None  # 联队战无王点概念
    assert raid["drop_total"] == 1
    assert raid["drops"] == [{"name": "物吉贞宗", "count": 1, "first_get_count": 1}]
    osaka = _group(body, "大阪城")
    assert osaka["kind"] == "osaka"
    assert osaka["battles"] == 2  # 两层各记 1 场
    assert osaka["boss_reached"] is None
    assert osaka["drop_total"] == 1
    assert body["unattributed"] == {"battles": 0, "drop_total": 0}
    assert _group(body, "8-2") is None  # 没有地图组被锻刀来源顶出来


def test_window_filtering(client, store):
    old = sh("2026-08-01 20:00:00")   # 70 天前
    recent = sh("2026-10-05 20:00:00")
    _battle(store, old, 8, 2)
    _battle(store, old, 5, 4)
    _battle(store, recent, 8, 2)
    _drop(store, old, "老掉", "sortie.drop", chapter=5, map_no=4)

    body = client.get("/api/data/drop-stats?days=30").json()
    group = _group(body, "8-2")
    assert group["battles"] == 1  # 老的 8-2 被窗口切掉
    assert _group(body, "5-4") is None  # 整张地图都掉了出去
    assert body["unattributed"] == {"battles": 0, "drop_total": 0}

    body_all = client.get("/api/data/drop-stats?days=0").json()
    assert _group(body_all, "8-2")["battles"] == 2
    g54 = _group(body_all, "5-4")
    assert g54["battles"] == 1 and g54["drop_total"] == 1


def test_unattributed_keeps_ungrouped_honest(client, store):
    # 缺 chapter/map_no 的收据进不了组：分母分子都记账，不静默丢
    _battle(store, time.time() - 100, None, None)
    _battle(store, time.time() - 90, 8, None)  # 半个坐标同样归不进组
    _drop(store, time.time() - 80, "谁", "sortie.drop")
    _drop(store, time.time() - 70, "谁", "battle.drop", chapter=8, map_no=None)
    body = client.get("/api/data/drop-stats?days=0").json()
    assert body["groups"] == []
    assert body["unattributed"] == {"battles": 2, "drop_total": 2}


def test_cross_run_pairing_does_not_mix(store):
    # 两个 run 用同一 (sequence, attempt) 键：必须各配各的，不许跨 run 借圈
    _loop(store, sh("2026-10-05 20:00:00"), "run-1", seq=1,
          battle_count=None, end_type=None)  # 出发没闭合 → 结果未知，0 场
    _loop(store, sh("2026-10-05 21:00:00"), "run-2", seq=1, battle_count=2)
    from touken.drop_stats import build_drop_stats
    body = build_drop_stats(store, days=0, now=NOW)
    group = _group(body, "8-2")
    assert group["battles"] == 2  # run-1 的未闭合圈 0 场 + run-2 的 2 场
    assert group["boss_reached"] == 1


def test_loop_without_map_info_goes_unattributed(store):
    # 圈缺地图信息：按它贡献的战斗数记进 unattributed.battles
    _loop(store, sh("2026-10-05 20:00:00"), "run-m", seq=1,
          chapter=None, map_no=None, battle_count=4)
    from touken.drop_stats import build_drop_stats
    body = build_drop_stats(store, days=0, now=NOW)
    assert body["groups"] == []
    assert body["unattributed"]["battles"] == 4


def test_group_sorting_maps_first_then_playstyles(store):
    _battle(store, NOW - 100, 8, 2)
    _battle(store, NOW - 90, 5, 4)
    _battle(store, NOW - 80, 10, 1)
    _event(store, NOW - 70, "raid.round_completed",
           {"sequence": 1, "battle_taps": 3}, run_id="r", script="raid")
    _event(store, NOW - 60, "osaka.floor_completed",
           {"completed": 1, "target": 9}, run_id="o", script="osaka")
    from touken.drop_stats import build_drop_stats
    body = build_drop_stats(store, days=0, now=NOW)
    assert [(g["kind"], g["key"]) for g in body["groups"]] == [
        ("map", "5-4"), ("map", "8-2"), ("map", "10-1"),
        ("raid", "联队战"), ("osaka", "大阪城"),
    ]
    assert body["window"] == {"days": 0, "from_ts": None, "to_ts": NOW}


def test_api_shape_and_default_window(client, store):
    _battle(store, time.time() - 3600, 8, 2)
    resp = client.get("/api/data/drop-stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"]["days"] == 30
    assert body["window"]["from_ts"] is not None
    assert body["schema_version"] == telemetry.TELEMETRY_SCHEMA_VERSION
    group = _group(body, "8-2")
    assert set(group) == {"key", "kind", "battles", "boss_reached",
                          "drops", "drop_total"}
