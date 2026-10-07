"""两服共用刀帐契约，日服名单及标注独立保存。"""
import json

from fastapi.testclient import TestClient

from touken.telemetry import TelemetryStore
from touken.sword_archive import build_jp_sword_archive


def record(store, swords, scope="full", ts=1):
    store._conn().execute(
        "INSERT INTO events(ts, script, event_type, payload) VALUES (?, ?, ?, ?)",
        (ts, "jp_listener", "training.captured", json.dumps({
            "roster_scope": scope, "swords": swords})))
    store._conn().commit()


def sword(serial, sid=3, level=20):
    return {"serial_id": serial, "sword_id": sid, "level": level,
            "exp": 100, "ranbu_level": 2, "ranbu_exp": 200}


def test_shared_contract_and_serial_marks(tmp_path, monkeypatch):
    from panel import server
    jp = TelemetryStore(tmp_path / "jp.db")
    cn = TelemetryStore(tmp_path / "cn.db")
    monkeypatch.setattr(server, "_telemetry_store_for", lambda s="": jp if s == "jp" else cn)
    record(jp, [sword(1), sword(2), sword(3, 4)])
    record(jp, [sword(1, level=22)], "partial", 2)
    client = TestClient(server.app)
    result = client.get("/api/data/sword-archive?server=jp").json()
    assert result["summary"]["total"] == 3
    assert result["roster_complete"] is True
    rows = result["entries"]
    assert rows[0]["sword_type"] == "太刀"
    assert rows[0]["level"] == 22
    assert rows[2]["form_status"] == "kiwame"
    assert rows[0]["kiwame_date"] is None
    body = {"serial_id": 1, "sword_catalog_id": rows[0]["sword_catalog_id"],
            "kiwame_date": None, "keeper": True, "watch": True, "favorite": True}
    response = client.post("/api/data/sword-archive/annotations?server=jp", json=body)
    assert response.status_code == 200
    assert cn.sword_annotations() == []
    result = client.get("/api/data/sword-archive?server=jp").json()
    assert result["entries"][0]["human"]["watch"] is True
    assert result["entries"][1]["human"] is None
    assert result["summary"]["keepers"] == 1
    annotation_id = response.json()["annotation"]["id"]
    assert client.delete(f"/api/data/sword-archive/annotations/{annotation_id}?server=jp").status_code == 200
    assert jp.sword_annotations() == []
    assert len(jp.sword_annotations(include_revoked=True)) == 1
    jp.close()
    cn.close()


def test_partial_unknown_empty_and_historical(tmp_path):
    store = TelemetryStore(tmp_path / "jp.db")
    assert build_jp_sword_archive(store)["done"] is False
    record(store, [sword(1, 99999)], "partial")
    result = build_jp_sword_archive(store)
    assert result["roster_complete"] is False
    assert result["entries"][0]["sword_type"] is None
    assert result["attention"][0]["reasons"] == ["form_unknown"]
    store.save_sword_annotation("jp_99999", None, serial_id=1, watch=True)
    record(store, [], "full", 2)
    result = build_jp_sword_archive(store)
    assert result["summary"]["total"] == 0
    assert len(result["historical_annotations"]) == 1
    assert result["attention"] == []
    store.close()
