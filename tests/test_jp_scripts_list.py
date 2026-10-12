"""日服功能页的脚本列表：日服只露日服脚本，国服永不露日服脚本。"""
from fastapi.testclient import TestClient
from panel import server


def test_jp_scripts_list_only_jp_sally():
    client = TestClient(server.app)
    data = client.get("/api/scripts?server=jp").json()
    assert set(data["scripts"]) == {"jp-sally"}
    info = data["scripts"]["jp-sally"]
    assert info["label"] == "日服出阵"
    assert {field["key"] for field in info["params"]} == {"team_no", "rounds"}


def test_default_scripts_list_never_includes_jp_sally():
    client = TestClient(server.app)
    data = client.get("/api/scripts").json()
    assert "jp-sally" not in data["scripts"]
    assert "daily" in data["scripts"]


def test_scripts_list_rejects_unknown_server():
    client = TestClient(server.app)
    assert client.get("/api/scripts?server=tw").status_code == 400
