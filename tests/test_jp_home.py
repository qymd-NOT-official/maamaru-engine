"""两服首页隔离与日服状态字段校准；记录均为合成数据。"""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from panel.honmaru_home import HomeStore, create_home_router
from touken.jp_home import home_observation, current_situation
from touken.jp_ledger import import_transactions
from touken.netlog import Transaction
from touken.telemetry import TelemetryStore


def test_independent_profile_notes_backup_and_old_install(tmp_path):
    cn_path = tmp_path / "state" / "honmaru_home.json"
    jp_path = tmp_path / "jp" / "state" / "honmaru_home.json"
    cn = HomeStore(cn_path)
    cn.save_profile({"honmaru_name": "国服本丸", "attendant": "加州清光"})
    cn_note = cn.save_note("国服的小记")
    original = cn_path.read_bytes()
    app = FastAPI()
    app.include_router(create_home_router(cn_path, jp_path))
    client = TestClient(app)
    assert client.get("/api/honmaru-home?server=jp").json()["profile"] == {}
    assert not jp_path.exists()
    assert client.put("/api/honmaru-home/profile?server=jp", json={"honmaru_name": "日服本丸"}).status_code == 200
    jp_note = client.post("/api/honmaru-home/notes?server=jp", json={"body": "日服的小记"}).json()["note"]
    assert client.put(f"/api/honmaru-home/notes/{cn_note['id']}?server=jp", json={"body": "不能串用"}).status_code == 404
    assert client.put(f"/api/honmaru-home/notes/{jp_note['id']}?server=jp", json={"body": "修改日服小记"}).status_code == 200
    assert cn_path.read_bytes() == original
    assert client.get("/api/honmaru-home").json()["notes"][0] == cn_note
    assert HomeStore(jp_path).read()["notes"][0]["body"] == "修改日服小记"
    assert json.loads(jp_path.with_suffix(".json.bak").read_text(encoding="utf-8"))["notes"][0]["body"] == "日服的小记"
    assert client.get("/api/honmaru-home?server=unknown").status_code == 400


def tx(path, payload):
    return Transaction(url=path, path=path, method="POST", request_line=None,
                       response_body=json.dumps(payload).encode())


def test_selective_state_and_jst_times_do_not_leak_identity(tmp_path):
    store = TelemetryStore(tmp_path / "jp.db")
    assert current_situation(store) is None
    import_transactions(store, [
        tx("/login/start", {"now": "2026-10-07 15:00:00", "secretary": 118,
            "name": "private-name", "user_id": 123, "t": "private-token"}),
        tx("/home/index", {"now": "2026-10-07 15:01:00", "situation": {
            "conquest": {"2": {"finished_at": "2026-10-07 17:00:00"}},
            "forge": {"1": {"finished_at": "2026-10-07 18:00:00"}},
            "repair": {"2": {"finished_at": "2026-10-07 19:00:00"}},
            "duty": {"finished_at": "2026-10-07 23:49:26"}}}),
    ])
    state = current_situation(store)
    assert state["secretary"]["name"] == "压切长谷部"
    assert state["secretary"]["observed_at"] == "2026-10-07 14:00:00"
    assert state["duty"]["finished_at"] == "2026-10-07 22:49:26"
    assert state["parties"][0]["finished_at"] == "2026-10-07 16:00:00"
    assert state["forge_slots"][0]["finished_at"] == "2026-10-07 17:00:00"
    assert state["repair"][0]["finished_at"] == "2026-10-07 18:00:00"
    raw = " ".join(row[0] for row in store._conn().execute("SELECT payload FROM events"))
    assert "private" not in raw and "user_id" not in raw
    import_transactions(store, [tx("/home/index", {"now": "2026-10-07 15:02:00",
        "situation": {"forge": {}, "conquest": {}}})])
    state = current_situation(store)
    assert state["forge_slots"] == [] and state["parties"] == []
    assert state["duty"]["finished_at"] == "2026-10-07 22:49:26"
    assert home_observation({"now": "invalid", "secretary": 118}) is None
    store.close()


def test_jp_situation_api_never_invokes_cn_runner(tmp_path, monkeypatch):
    from panel import server
    store = TelemetryStore(tmp_path / "jp.db")
    monkeypatch.setattr(server, "_telemetry_store_for", lambda value: store if value == "jp" else None)
    def forbidden():
        raise AssertionError("日服首页不得连接国服运行器")
    monkeypatch.setattr(server, "get_runner", forbidden)
    client = TestClient(server.app)
    assert client.get("/api/honmaru-home/situation?server=jp").json() == {"situation": None}
    assert client.post("/api/honmaru-home/situation/refresh?server=jp").json() == {"situation": None}
    store.close()
