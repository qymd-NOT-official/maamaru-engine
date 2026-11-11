"""日服功能页的脚本列表：日服只露日服脚本，国服永不露日服脚本。"""
from fastapi.testclient import TestClient
from panel import server


def test_jp_scripts_list_only_jp_scripts():
    client = TestClient(server.app)
    data = client.get("/api/scripts?server=jp").json()
    assert set(data["scripts"]) == {"jp-sally", "jp-expedition", "jp-pumpkin",
                                    "jp-edocastle", "jp-sortie"}
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
    pumpkin = data["scripts"]["jp-pumpkin"]
    assert pumpkin["label"] == "日服南瓜"
    assert {field["key"] for field in pumpkin["params"]} == {
        "team_no", "difficulty", "max_skips"}
    edocastle = data["scripts"]["jp-edocastle"]
    assert edocastle["label"] == "日服江户城"
    assert {field["key"] for field in edocastle["params"]} == {
        "team_no", "difficulty", "rounds"}
    sortie = data["scripts"]["jp-sortie"]
    assert sortie["label"] == "日服刷图"
    assert {field["key"] for field in sortie["params"]} == {
        "chapter", "map_no", "team_no", "rounds"}


def test_default_scripts_list_never_includes_jp_scripts():
    client = TestClient(server.app)
    data = client.get("/api/scripts").json()
    assert "jp-sally" not in data["scripts"]
    assert "jp-expedition" not in data["scripts"]
    assert "jp-pumpkin" not in data["scripts"]
    assert "jp-edocastle" not in data["scripts"]
    assert "jp-sortie" not in data["scripts"]
    assert "daily" in data["scripts"]


def test_scripts_list_rejects_unknown_server():
    client = TestClient(server.app)
    assert client.get("/api/scripts?server=tw").status_code == 400
