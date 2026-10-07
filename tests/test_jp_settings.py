"""日服外观设置隔离，不迁移国服参数。"""
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from panel import server


def test_independent_settings_and_old_install(tmp_path, monkeypatch):
    cn = tmp_path / "cn.json"
    cn.write_text(json.dumps({"theme": "pixel", "params": {"daily": {"enabled": True}}}), encoding="utf-8")
    original = cn.read_bytes()
    monkeypatch.setattr(server, "_SETTINGS_FILE", cn)
    monkeypatch.setattr(server, "JP_DATA_DIR", tmp_path / "jp")
    client = TestClient(server.app)
    assert client.get("/api/saved-settings?server=jp").json() == {}
    for body in [{"scenery": "winter"}, {"companion": "hasebe"}, {"theme": "washi"}, {"backdrop": "#AABBCC"}]:
        assert client.post("/api/saved-settings?server=jp", json=body).status_code == 200
    saved = client.get("/api/saved-settings?server=jp").json()
    assert saved["scenery"] == "winter" and saved["companion"] == "hasebe"
    assert saved["backdrop"] == "#aabbcc" and "params" not in saved
    assert cn.read_bytes() == original
    assert {k: v for k, v in server._load_panel_settings("jp").items() if k != "_saved_at"} == saved
    assert client.post("/api/saved-settings?server=jp", json={"params": {"daily": {}}}).status_code == 400
    assert client.get("/api/saved-settings?server=other").status_code == 400
    assert client.post("/api/saved-settings?server=jp", json=[]).status_code == 400
    assert client.get("/api/saved-settings").json()["theme"] == "pixel"
    assert json.loads((tmp_path / "jp/state/panel_settings.json.bak").read_text(encoding="utf-8"))["theme"] == "washi"


def test_failed_replace_preserves_settings_and_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JP_DATA_DIR", tmp_path)
    server._save_panel_settings({"scenery": "winter"}, "jp")
    path = tmp_path / "state/panel_settings.json"
    original = path.read_bytes()
    def failed_replace(self, target):
        raise OSError("synthetic failure")
    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(OSError):
        server._save_panel_settings({"scenery": "spring"}, "jp")
    assert path.read_bytes() == original
    assert path.with_suffix(".json.bak").read_bytes() == original
