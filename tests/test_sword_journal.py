# -*- coding: utf-8 -*-
"""入手履历 API：/api/data/sword-journal/{serial_id}。

断言口径（契约见 docs/telemetry-data.md「入手履历」与 touken/sword_journal.py
docstring）：入手=刀帐档案 created_at + sword.obtained 收据合并、修行
departed/returned、首次观测满级复用练度快照链；缺哪段就没有哪段不编；
一条都没有 404；acquired_at 字符串/数字都认，解析失败退化事件 ts。
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from touken import telemetry
from touken.telemetry import TelemetryStore

SH = timezone(timedelta(hours=8))
SERIAL = 2077


def sh(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


def _event(store, ts, event_type, payload):
    store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) VALUES (?,?,?,?,?)",
        (ts, None, "youzu_log", event_type,
         json.dumps(payload, ensure_ascii=False)))
    store._conn().commit()


def _obtained(store, ts, acquired_at, source="forge", chapter=None, map_no=None,
              first=True):
    _event(store, ts, "sword.obtained", {
        "name": "今剑", "sword_id": 11, "serial_id": SERIAL, "source": source,
        "chapter": chapter, "map_no": map_no,
        "is_first_get_sword": first, "acquired_at": acquired_at})


def _departed(store, ts):
    _event(store, ts, "kiwame.departed", {"serial_id": SERIAL})


def _returned(store, ts, finished_at="2026-09-30 05:00:00"):
    _event(store, ts, "kiwame.returned",
           {"serial_id": SERIAL, "finished_at": finished_at})


def _training(store, ts, level):
    _event(store, ts, "training.captured", {
        "captured_at": datetime.fromtimestamp(ts, SH).strftime("%Y-%m-%d %H:%M:%S"),
        "swords": [{"serial_id": SERIAL, "sword_id": 11, "level": level,
                    "exp": 100, "ranbu_level": 1, "ranbu_exp": 0}]})


def _write_archive(store, created_at):
    """刀帐档案侧写：临时库的档案路径是 <db>.swords.json。"""
    from touken.game_sword_archive import archive_path
    path = archive_path(store)
    path.write_text(json.dumps({
        "schema": 1, "complete_at": sh("2026-10-01 08:00:00"),
        "swords": {str(SERIAL): {"serial_id": SERIAL, "sword_id": 11,
                                 "created_at": created_at,
                                 "observed_at": sh("2026-10-01 08:00:00")}}},
        ensure_ascii=False), encoding="utf-8")


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


def test_full_timeline_all_kinds_sorted(client, store):
    _write_archive(store, "2026-06-21 12:34:56")
    _obtained(store, sh("2026-06-21 12:34:56"), acquired_at="2026-06-21 12:34:56")
    _training(store, sh("2026-07-01 08:00:00"), level=80)
    _departed(store, sh("2026-08-01 10:00:00"))
    _training(store, sh("2026-08-05 08:00:00"), level=99)   # 首次观测满级
    _returned(store, sh("2026-09-01 10:00:00"))
    _training(store, sh("2026-09-05 08:00:00"), level=99)   # 第二次观测不再出条

    body = client.get(f"/api/data/sword-journal/{SERIAL}").json()
    assert body["serial_id"] == SERIAL
    assert [(e["kind"], e["ts"]) for e in body["timeline"]] == [
        ("obtained", sh("2026-06-21 12:34:56")),
        ("departed", sh("2026-08-01 10:00:00")),
        ("max_level_observed", sh("2026-08-05 08:00:00")),
        ("returned", sh("2026-09-01 10:00:00")),
    ]
    obtained = body["timeline"][0]["detail"]
    assert obtained["archive_created_at"] == "2026-06-21 12:34:56"
    assert obtained["source"] == "forge"
    assert obtained["is_first_get_sword"] is True
    returned = body["timeline"][3]["detail"]
    assert returned == {"finished_at": "2026-09-30 05:00:00"}
    max_level = body["timeline"][2]["detail"]
    assert max_level == {"captured_at": "2026-08-05 08:00:00", "level": 99}


def test_receipt_only_without_archive(client, store):
    # 档案缺这一段：obtained 照常出，archive_created_at 给 null 不编
    _obtained(store, sh("2026-06-21 12:34:56"),
              acquired_at=sh("2026-06-21 12:34:56"), source="sortie.drop",
              chapter=8, map_no=2, first=False)
    body = client.get(f"/api/data/sword-journal/{SERIAL}").json()
    entry = body["timeline"][0]
    assert entry["kind"] == "obtained"
    assert entry["detail"]["archive_created_at"] is None
    assert entry["detail"]["chapter"] == 8
    assert entry["detail"]["map_no"] == 2


def test_archive_only_without_receipt(client, store):
    # 收据白名单漏抓，只有档案：出一条 obtained，收据字段全 null
    _write_archive(store, "2026-06-21 12:34:56")
    body = client.get(f"/api/data/sword-journal/{SERIAL}").json()
    entry = body["timeline"][0]
    assert entry["kind"] == "obtained"
    assert entry["ts"] == sh("2026-06-21 12:34:56")
    assert entry["detail"]["source"] is None
    assert entry["detail"]["sword_id"] == 11
    assert entry["detail"]["archive_created_at"] == "2026-06-21 12:34:56"


def test_missing_segments_not_fabricated(client, store):
    # 只有修行进出：时间线就只有这两段，入手/满级不编
    _departed(store, sh("2026-08-01 10:00:00"))
    _returned(store, sh("2026-09-01 10:00:00"))
    body = client.get(f"/api/data/sword-journal/{SERIAL}").json()
    assert [e["kind"] for e in body["timeline"]] == ["departed", "returned"]


def test_acquired_at_numeric_and_unparseable_fallback(client, store):
    # 数字 acquired_at 直接用；解析不出的字符串退化为事件 ts，原文保留
    _obtained(store, sh("2026-06-21 12:34:56"),
              acquired_at=sh("2026-06-21 12:34:56"), first=True)
    _obtained(store, sh("2026-07-01 08:00:00"), acquired_at="不知道",
              first=False)
    body = client.get(f"/api/data/sword-journal/{SERIAL}").json()
    entries = [e for e in body["timeline"] if e["kind"] == "obtained"]
    assert entries[0]["ts"] == sh("2026-06-21 12:34:56")
    assert entries[1]["ts"] == sh("2026-07-01 08:00:00")  # 事件观测时刻
    assert entries[1]["detail"]["acquired_at"] == "不知道"


def test_other_serial_events_ignored(client, store):
    _obtained(store, sh("2026-06-21 12:34:56"),
              acquired_at=sh("2026-06-21 12:34:56"))
    _event(store, sh("2026-08-01 10:00:00"), "kiwame.departed",
           {"serial_id": 9999})
    body = client.get(f"/api/data/sword-journal/{SERIAL}").json()
    assert [e["kind"] for e in body["timeline"]] == ["obtained"]


def test_404_without_any_record(client, store):
    resp = client.get(f"/api/data/sword-journal/{SERIAL}")
    assert resp.status_code == 404
    resp = client.get("/api/data/sword-journal/abc")
    assert resp.status_code == 422
