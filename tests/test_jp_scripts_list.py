"""日服功能页的脚本列表：日服只露日服脚本，国服永不露日服脚本。"""
from fastapi.testclient import TestClient
from panel import server


def test_jp_scripts_list_only_jp_scripts():
    client = TestClient(server.app)
    data = client.get("/api/scripts?server=jp").json()
    assert set(data["scripts"]) == {"jp-sally", "jp-expedition"}
    sally = data["scripts"]["jp-sally"]
    assert sally["label"] == "日服出阵"
    assert {field["key"] for field in sally["params"]} == {"team_no", "rounds"}
    expedition = data["scripts"]["jp-expedition"]
    assert expedition["label"] == "日服远征"
    assert {field["key"] for field in expedition["params"]} == {
        "team_no", "era", "map_slot", "map_name"}
    fields = {field["key"]: field for field in expedition["params"]}
    assert fields["map_name"]["default"] == "鳥羽・伏見"
    for key in ("team_no", "era", "map_slot"):
        assert [option[0] for option in fields[key]["options"]] == (
            ["1", "2", "3", "4", "5"] if key != "map_slot" else ["1", "2", "3", "4"])


def test_default_scripts_list_never_includes_jp_scripts():
    client = TestClient(server.app)
    data = client.get("/api/scripts").json()
    assert "jp-sally" not in data["scripts"]
    assert "jp-expedition" not in data["scripts"]
    assert "daily" in data["scripts"]


def test_scripts_list_rejects_unknown_server():
    client = TestClient(server.app)
    assert client.get("/api/scripts?server=tw").status_code == 400
