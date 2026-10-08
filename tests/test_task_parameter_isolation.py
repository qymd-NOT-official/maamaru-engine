"""独立任务参数的迁移、回滚与执行隔离；不连接游戏。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_workflow import _FakeAgent, workflow
from panel import server
from panel.task_parameters import expedition_routes, isolate_presets


class IsolationTests(unittest.TestCase):
    def test_existing_overrides_win_and_migration_is_idempotent(self):
        registry = {"practice": {"snapshot_saved": True, "params": [{"key": "formation", "default": "逆行阵"}]}}
        original = [{"id": "old", "nodes": [{"type": "practice", "params": {"team_no": "3"}}]}]
        first = isolate_presets(original, registry, {"practice": {"team_no": "4", "formation": "横队阵"}}, {}, {})
        self.assertEqual(first[0]["nodes"][0]["params"], {"team_no": "3", "formation": "横队阵"})
        self.assertEqual(original[0]["nodes"][0]["params"], {"team_no": "3"})
        self.assertEqual(isolate_presets(first, registry, {"practice": {"formation": "鱼鳞阵"}}, {}, {}), first)

    def test_daily_and_workflow_practice_never_read_solo_settings(self):
        for daily in (False, True):
            agent = _FakeAgent()
            definition = workflow.NODE_REGISTRY["practice"]
            run = definition["daily_run"] if daily else definition["run"]
            with patch.object(server, "_load_panel_settings", side_effect=AssertionError("读取了单跑配置")):
                list(run(agent, {"team_no": "3", "formation": "横队阵"}, "fake.json"))
            call = next(c for c in agent.calls if c[0] == "practice_stream")
            self.assertEqual(call[2]["team_no"], 3)
            self.assertEqual(call[2]["formation"], "横队阵")

    def test_expedition_uses_own_route_and_keeps_scheduler_guard(self):
        values = {"enabled_2": True, "map_2": "A1", "enabled_4": True, "map_4": "D4",
                  "sakura_before_dispatch": True, "repair_threshold": "medium"}
        lookup = lambda code: {"era": 1, "slot": 2, "name": code}
        routes = expedition_routes(values, lookup, {4})
        self.assertEqual([r["team_no"] for r in routes], [2])
        self.assertEqual(routes[0]["repair_threshold"], "medium")
        with self.assertRaises(ValueError):
            expedition_routes(values, lambda _: None, set())
        agent = Mock()
        agent._daily_expedition_step.return_value = iter(())
        with patch("panel.scheduler.find_map", side_effect=lookup), patch("panel.scheduler.managed_teams", return_value={4}), \
             patch.object(server, "_load_panel_settings", side_effect=AssertionError("读取了单跑配置")):
            list(workflow.NODE_REGISTRY["expedition"]["daily_run"](agent, values, "fake.json"))
        agent._daily_expedition_step.assert_called_once_with(routes)

    def test_upgrade_backup_restart_and_rollback_in_isolated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "panel_settings.json"
            presets_path = root / "workflows.json"
            settings = {"params": {"practice": {"team_no": "4", "formation": "横队阵"}, "daily": {"steps": ["演练", "远征"]}}}
            presets = {"presets": [{"id": "old", "name": "旧安排", "nodes": [{"type": "practice", "params": {}, "on_error": "stop"}]}]}
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
            presets_path.write_text(json.dumps(presets), encoding="utf-8")
            before_settings, before_presets = settings_path.read_bytes(), presets_path.read_bytes()
            schedule = {"common_plan": [{"team_no": 4, "enabled": True, "map_code": "D4"}], "automation": {}}
            with patch.object(workflow, "STATUS_DIR", root), patch.object(server, "_panel_settings_path", return_value=settings_path), \
                 patch("panel.scheduler.load_config", return_value=schedule):
                server._migrate_task_parameters()
                first = workflow.find_preset("old")
                daily = workflow.find_preset("builtin-daily")
                self.assertEqual(first["nodes"][0]["params"]["team_no"], "4")
                self.assertEqual(next(n for n in daily["nodes"] if n["type"] == "expedition")["params"]["map_4"], "D4")
                modified = json.loads(settings_path.read_text(encoding="utf-8"))
                modified["params"]["practice"]["team_no"] = "2"
                settings_path.write_text(json.dumps(modified), encoding="utf-8")
                schedule["common_plan"][0]["map_code"] = "A1"
                server._migrate_task_parameters()
                self.assertEqual(workflow.find_preset("old"), first)
                self.assertEqual(workflow.find_preset("builtin-daily"), daily)
                self.assertEqual(settings_path.with_suffix(".json.parameters-v0.bak").read_bytes(), before_settings)
                self.assertEqual(presets_path.with_suffix(".parameters-v0.bak").read_bytes(), before_presets)
                settings_path.write_bytes(before_settings)
                presets_path.write_bytes(before_presets)
                server._migrate_task_parameters()
                self.assertEqual(workflow.find_preset("old"), first)
