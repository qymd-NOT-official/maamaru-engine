# -*- coding: utf-8 -*-
"""预设编队面板层：CRUD 端点、部队选项动态注入、玩法 builder 接线。

隔离纪律（2026-09-22 全量跑翻车实录）：模块级 env 补丁只在「本文件第一个
import runtime_paths」时有效，全量跑时别处的 import 会把它钉到真实数据
目录——所以每个用例 setUp 里逐用例 patch cf.STATE_DIR / scheduler 的
存储路径（照 tests/test_custom_formations.py StorageTests 的口径），
绝不碰真实用户数据。话术纪律钉死：拒绝/失败话术必须命中翻车词表，
正常通过不许命中。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_data = tempfile.TemporaryDirectory(prefix="custom_formations_panel_",
                                    ignore_cleanup_errors=True)
with patch.dict("os.environ", {"MAAMARU_DATA_DIR": _data.name}):
    from fastapi.testclient import TestClient
    from panel import scheduler
    from panel import server
    from panel.script_runner import _SCRIPTS
    from touken import custom_formations as cf
    from touken.flows.report_judge import _is_fail


def _slot(name="三日月宗近"):
    catalog = {"三日月宗近": "touken_003_mikazuki_munechika",
               "小狐丸": "touken_005_kogitsunemaru"}.get(name, f"touken_{name}")
    return {"sword_catalog_id": catalog,
            "name_zh": name, "level": 99}


def _candidate_pool():
    entries = []
    for index, name in enumerate(("三日月宗近", "小狐丸"), 1):
        entry = _slot(name)
        entry.update({"observation_id": f"9:{index}",
                      "same_team_exclusion_key": entry["sword_catalog_id"]})
        entries.append(entry)
    return {"done": True, "observed_at": 1700000000, "entries": entries}


def _body(**over):
    body = {"name": "刷图队", "target_team": 3,
            "slots": {"1": _slot(), "2": _slot("小狐丸")}}
    body.update(over)
    return body


def _record(fid="pf1", **over):
    record = {"id": fid, "name": "刷图队", "target_team": 3,
              "slots": {"1": _slot()},
              "created_at": "2026-09-21 08:00:00",
              "updated_at": "2026-09-21 08:00:00"}
    record.update(over)
    return record


class PanelTestCase(unittest.TestCase):
    def setUp(self):
        # 真正的隔离靠这里，不靠模块级 env 补丁（全量跑时别处的 import
        # 可能已经把 runtime_paths 钉到真实数据目录，照 test_custom_formations.py
        # 的 StorageTests 口径逐用例 patch 存储路径）
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state = Path(self._tmp.name) / "state"
        self._state.mkdir(parents=True)
        self._sched_dir = Path(self._tmp.name) / "config"
        self._sched_dir.mkdir()
        # 排班配置是 config/expedition.json，不是 state 里的文件，分开指
        patches = ((cf, "STATE_DIR", self._state),
                   (scheduler, "STATE_DIR", self._state),
                   (scheduler, "_SCHED_PATH", self._sched_dir / "expedition.json"),
                   (cf, "_current_candidate_pool", _candidate_pool))
        for target, attr, value in patches:
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        cf.save_formations([])  # 每个用例都从空库出发
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)


class CrudTests(PanelTestCase):
    def test_create_get_update_delete_roundtrip(self):
        resp = self.client.post("/api/custom-formations", json=_body())
        self.assertEqual(resp.status_code, 200)
        created = resp.json()["formation"]
        self.assertEqual(created["id"], "pf1")
        self.assertTrue(created["created_at"])
        self.assertTrue(created["updated_at"])

        got = self.client.get("/api/custom-formations").json()["formations"]
        self.assertEqual(got, [created])

        resp = self.client.put(
            f"/api/custom-formations/{created['id']}",
            json=_body(name="改名人", target_team="5", slots={"1": _slot()}))
        self.assertEqual(resp.status_code, 200)
        updated = resp.json()["formation"]
        self.assertEqual(updated["name"], "改名人")
        self.assertEqual(updated["target_team"], 5)  # 字符串也能落 int
        self.assertEqual(updated["created_at"], created["created_at"])
        self.assertEqual(len(updated["slots"]), 1)

        self.assertEqual(self.client.delete(
            f"/api/custom-formations/{created['id']}").json(), {"ok": True})
        self.assertEqual(
            self.client.get("/api/custom-formations").json()["formations"], [])

    def test_id_from_path_wins_over_body(self):
        self.client.post("/api/custom-formations", json=_body())
        resp = self.client.put("/api/custom-formations/pf1",
                               json=_body(id="pf9", name="改名"))
        self.assertEqual(resp.json()["formation"]["id"], "pf1")

    def test_update_skips_self_id_collision(self):
        self.client.post("/api/custom-formations", json=_body())
        # 原样保存自己不能撞「id 已被占用」
        resp = self.client.put("/api/custom-formations/pf1", json=_body())
        self.assertEqual(resp.status_code, 200)

    def test_full_five_is_400(self):
        for i in range(5):
            self.assertEqual(self.client.post(
                "/api/custom-formations",
                json=_body(name=f"第{i}套")).status_code, 200)
        resp = self.client.post("/api/custom-formations", json=_body())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("预设编队最多 5 套", resp.json()["reason"])

    def test_validate_failure_is_400_with_message(self):
        resp = self.client.post(
            "/api/custom-formations", json=_body(target_team=9))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("目标部队", resp.json()["reason"])

        resp = self.client.post(
            "/api/custom-formations",
            json=_body(slots={"1": {"level": 99}}))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("缺身份", resp.json()["reason"])

        resp = self.client.put("/api/custom-formations/pf1",
                               json=_body(name=""))
        self.assertEqual(resp.status_code, 404)  # 库空：先撞 404
        self.client.post("/api/custom-formations", json=_body())
        resp = self.client.put("/api/custom-formations/pf1", json=_body(name=""))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("名字", resp.json()["reason"])

    def test_missing_record_is_404(self):
        resp = self.client.put("/api/custom-formations/pf9", json=_body())
        self.assertEqual(resp.status_code, 404)
        resp = self.client.delete("/api/custom-formations/pf9")
        self.assertEqual(resp.status_code, 404)


class SchemaInjectionTests(PanelTestCase):
    def _team_options(self, script):
        body = self.client.get("/api/scripts").json()
        fields = {f["key"]: f for f in body["scripts"][script]["params"]}
        return fields["team_no"]["options"]

    def test_preset_appended_to_team_options_tail(self):
        cf.save_formations([_record(name="预设编队一", target_team=3)])
        self.assertEqual(self._team_options("sortie")[-1],
                         ["preset:pf1", "预设编队一（覆盖部队三）"])

    def test_no_preset_leaves_options_untouched(self):
        self.assertEqual(self._team_options("sortie"),
                         [list(o) for o in server._TEAM_OPTIONS])

    def test_repeated_requests_do_not_duplicate(self):
        cf.save_formations([_record(name="预设编队一")])
        self.assertEqual(len(self._team_options("sortie")),
                         len(server._TEAM_OPTIONS) + 1)
        self.assertEqual(len(self._team_options("osaka")),
                         len(server._TEAM_OPTIONS) + 1)

    def test_scripts_registry_body_untouched(self):
        cf.save_formations([_record(name="预设编队一")])
        self.client.get("/api/scripts")
        fields = {f["key"]: f for f in _SCRIPTS["sortie"]["params"]}
        self.assertEqual(fields["team_no"]["options"],
                         server._TEAM_OPTIONS)

    def test_workflow_preset_node_receives_current_options(self):
        cf.save_formations([_record(name="远征轮换", target_team=5)])
        nodes = self.client.get("/api/workflows/nodes").json()["nodes"]
        node = next(item for item in nodes
                    if item["type"] == "apply_formation_preset")
        field = next(item for item in node["params"]
                     if item["key"] == "preset_id")
        self.assertEqual(field["options"], [["pf1", "远征轮换（覆盖部队五）"]])


class AgentStub:
    """假 Agent：记 apply 调用与玩法流 kwargs；apply 可按需返回 False。"""

    def __init__(self, apply_ok=True):
        self.apply_ok = apply_ok
        self.apply_calls = []
        self.calls = []

    def apply_preset_formation_stream(self, team_no, slots,
                                      name="预设编队"):
        self.apply_calls.append((team_no, slots, name))
        if self.apply_ok:
            yield f"[预设编队] 已套用「{name}」"
            return True
        # 照真实契约：被拦下时最后一条是翻车话术
        yield "[预设编队] 没能应用完，停"
        return False

    def _play(self, stream, **kw):
        self.calls.append((stream, kw))
        yield f"[玩法] {stream} 干活"

    def sortie_stream(self, **kw):
        return self._play("sortie", **kw)

    def yosari_stream(self, **kw):
        return self._play("yosari", **kw)

    def osaka_stream(self, **kw):
        return self._play("osaka", **kw)

    def hanafuda_stream(self, **kw):
        return self._play("hanafuda", **kw)

    def edocastle_stream(self, **kw):
        return self._play("edocastle", **kw)

    def raid_stream(self, **kw):
        return self._play("raid", **kw)

    def pumpkin_stream(self, **kw):
        return self._play("pumpkin", **kw)

    def practice_stream(self, **kw):
        return self._play("practice", **kw)

    def collect_expedition_stream(self, **kw):
        return self._play("collect_expedition", **kw)

    def expedition_stream(self, **kw):
        return self._play("expedition", **kw)


class BuilderTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        cf.save_formations([_record(name="刷图队", target_team=5)])

    def _run(self, builder, params, agent=None):
        agent = agent or AgentStub()
        messages = list(builder(agent, "cfg", params))
        return agent, messages

    def test_preset_applied_before_battle_with_target_team(self):
        agent, messages = self._run(
            server._build_sortie, {"team_no": "preset:pf1", "chapter": "2"})
        self.assertEqual(agent.apply_calls,
                         [(5, {"1": _candidate_pool()["entries"][0]}, "刷图队")])
        stream, kw = agent.calls[0]
        self.assertEqual(stream, "sortie")
        self.assertEqual(kw["team_no"], 5)  # 玩法流拿到的是预设指向的队
        # 先套编队、再进玩法
        self.assertLess(messages.index("[预设编队] 已套用「刷图队」"),
                        messages.index("[玩法] sortie 干活"))
        self.assertFalse(any(_is_fail(m) for m in messages))

    def test_workflow_node_uses_the_same_executor(self):
        agent = AgentStub()
        messages = list(server._workflow.NODE_REGISTRY[
            "apply_formation_preset"]["run"](
                agent, {"preset_id": "pf1"}, "cfg"))
        self.assertEqual(len(agent.apply_calls), 1)
        self.assertEqual(agent.apply_calls[0][0], 5)
        self.assertFalse(any(_is_fail(message) for message in messages))

    def test_all_battle_builders_are_wired(self):
        pairs = [("sortie", server._build_sortie),
                 ("yosari", server._build_yosari),
                 ("osaka", server._build_osaka),
                 ("hanafuda", server._build_hanafuda),
                 ("edocastle", server._build_edocastle),
                 ("raid", server._build_raid),
                 ("pumpkin", server._build_pumpkin),
                 ("practice", server._build_practice)]
        for stream, builder in pairs:
            agent, _ = self._run(builder, {"team_no": "preset:pf1"})
            self.assertEqual([c[0] for c in agent.calls], [stream], stream)
            self.assertEqual(agent.calls[0][1]["team_no"], 5, stream)
            self.assertEqual(len(agent.apply_calls), 1, stream)

    def test_apply_false_stops_before_battle(self):
        agent, messages = self._run(
            server._build_sortie, {"team_no": "preset:pf1"},
            agent=AgentStub(apply_ok=False))
        self.assertEqual(agent.calls, [])
        self.assertTrue(_is_fail(messages[-1]))  # 被拦下必须能判红

    def test_deleted_preset_rejected(self):
        cf.save_formations([])
        agent, messages = self._run(
            server._build_sortie, {"team_no": "preset:pf1"})
        self.assertEqual(agent.calls, [])
        self.assertEqual(agent.apply_calls, [])
        self.assertIn("重新选一个", messages[-1])
        self.assertTrue(_is_fail(messages[-1]))

    def test_plain_team_numbers_untouched(self):
        agent, messages = self._run(server._build_sortie, {"team_no": "2"})
        self.assertEqual(agent.apply_calls, [])
        self.assertEqual(agent.calls[0][1]["team_no"], 2)

        agent, _ = self._run(server._build_practice, {})
        self.assertEqual(agent.apply_calls, [])
        self.assertEqual(agent.calls[0][1]["team_no"], 2)  # 演练默认队

        agent, messages = self._run(server._build_sortie, {})
        self.assertEqual(agent.calls[0][1]["team_no"], 3)  # 出阵默认队
        self.assertFalse(any(_is_fail(m) for m in messages))


class ScheduleConflictTests(PanelTestCase):
    """预设要覆盖的队正被排班托管且还在远征 → 动不得。"""

    def setUp(self):
        super().setUp()
        cf.save_formations([_record(name="刷图队", target_team=3)])

    def _occupy_team3(self):
        (self._sched_dir / "expedition.json").write_text(json.dumps(
            {"automation": {"enabled": True, "mode": "preset",
                            "teams": [3]}}, ensure_ascii=False),
            encoding="utf-8")
        (self._state / "expeditions.json").write_text(json.dumps(
            {"3": {"dispatched_at": "2099-01-01 00:00:00",
                   "duration_min": 30}}, ensure_ascii=False),
            encoding="utf-8")

    def test_old_expedition_countdown_does_not_block_manual_preset(self):
        self._occupy_team3()
        agent = AgentStub()
        messages = list(server._build_sortie(
            agent, "cfg", {"team_no": "preset:pf1"}))
        self.assertEqual(len(agent.apply_calls), 1)
        self.assertEqual(agent.calls[0][1]["team_no"], 3)
        self.assertFalse(any(_is_fail(m) for m in messages))

    def test_managed_but_available_team_proceeds(self):
        self._occupy_team3()
        # 记录已过期 → 队已回来，排班没占着，可以覆盖
        (self._state / "expeditions.json").write_text(json.dumps(
            {"3": {"dispatched_at": "2020-01-01 00:00:00",
                   "duration_min": 30}}, ensure_ascii=False),
            encoding="utf-8")
        agent = AgentStub()
        messages = list(server._build_sortie(
            agent, "cfg", {"team_no": "preset:pf1"}))
        self.assertEqual(len(agent.apply_calls), 1)
        self.assertEqual(agent.calls[0][1]["team_no"], 3)
        self.assertFalse(any(_is_fail(m) for m in messages))

    def test_unmanaged_team_proceeds(self):
        # 排班没启用：管不到任何队
        agent = AgentStub()
        list(server._build_sortie(agent, "cfg", {"team_no": "preset:pf1"}))
        self.assertEqual(len(agent.apply_calls), 1)


class ExpeditionPresetTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        cf.save_formations([_record(name="远征轮换", target_team=2)])

    def test_schedule_keeps_only_a_preset_for_the_same_team(self):
        body = {"common_plan": [
            {"team_no": 2, "map_code": "B2", "enabled": True,
             "formation_id": "pf1"},
            {"team_no": 3, "map_code": "C1", "enabled": True,
             "formation_id": "pf1"},
        ]}
        self.assertEqual(self.client.post(
            "/api/expedition-schedule", json=body).status_code, 200)
        rows = scheduler.load_config()["common_plan"]
        self.assertEqual(rows[0]["formation_id"], "pf1")
        self.assertEqual(rows[1]["formation_id"], "")

    def test_common_expedition_applies_preset_before_dispatch(self):
        agent = AgentStub()
        schedule = {"common_plan": [{
            "team_no": 2, "map_code": "B2", "enabled": True,
            "formation_id": "pf1",
        }]}
        with patch("panel.scheduler.load_config", return_value=schedule), \
             patch("panel.scheduler.managed_teams", return_value=set()), \
             patch("panel.scheduler.find_map", return_value={
                "code": "B2", "era": 2, "slot": 2, "name": "测试图"}), \
             patch.object(server, "_read_expedition_records", return_value={}):
            messages = list(server._build_expedition_manager(
                agent, "cfg", {}))
        self.assertEqual([call[0] for call in agent.calls],
                         ["collect_expedition", "expedition"])
        self.assertEqual(len(agent.apply_calls), 1)
        self.assertLess(messages.index("[预设编队] 已套用「远征轮换」"),
                        messages.index("[玩法] expedition 干活"))


if __name__ == "__main__":
    unittest.main()
