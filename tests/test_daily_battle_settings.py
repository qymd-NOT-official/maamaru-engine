"""一键日课战斗行为字段与「配置」页切割（issue#7：日课没安排修刀却自动修）。

- _daily_plan_inputs 只读日课自己的参数，缺键回落硬默认，不再继承配置页
- _migrate_daily_battle_settings 把配置页现状一次性快照进日课参数，
  保住老用户现状；键已存在（用户改过/迁移过）就绝不动
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panel import server
from panel.daily_workflow import make_template


class PlanInputsCutTests(unittest.TestCase):
    def test_daily_params_win_and_defaults_are_hardcoded(self):
        params = {"sortie_mode": "sortie", "repair_threshold": "medium",
                  "repair_on_injury": "stop"}
        plan = server._daily_plan_inputs(params)[2]
        self.assertEqual(plan["repair_threshold"], "medium")
        self.assertEqual(plan["repair_on_injury"], "stop")

        # 缺键回落硬默认——不是配置页的值
        plan = server._daily_plan_inputs({"sortie_mode": "sortie"})[2]
        self.assertEqual(plan["repair_threshold"], "light")
        self.assertEqual(plan["repair_on_injury"], "continue")
        self.assertTrue(plan["auto_march"])
        self.assertFalse(plan["retreat_before_boss"])

    def test_yosari_and_osaka_also_self_contained(self):
        plan = server._daily_plan_inputs({"sortie_mode": "yosari"})[2]
        self.assertEqual(plan["repair_threshold"], "light")
        self.assertFalse(plan["rotate_captain"])
        plan = server._daily_plan_inputs(
            {"sortie_mode": "osaka", "repair_on_injury": "repair_stop"})[2]
        self.assertEqual(plan["repair_on_injury"], "repair_stop")
        self.assertFalse(plan["rotate_captain"])
        self.assertEqual(plan["rotate_captain_margin"], 10)

    def test_osaka_rotation_settings_reach_daily_and_standalone_execution(self):
        from unittest.mock import Mock
        params = {"sortie_mode": "osaka", "rotate_captain": True,
                  "rotate_captain_margin": "20"}
        plan = server._daily_plan_inputs(params)[2]
        self.assertTrue(plan["rotate_captain"])
        self.assertEqual(plan["rotate_captain_margin"], 20)
        agent = Mock()
        agent.osaka_stream.return_value = iter(())
        def team(*args):
            yield from ()
            return 3
        with patch.object(server, "_team_with_preset_stream", team):
            list(server._build_osaka(agent, "fake.json", params))
        self.assertTrue(agent.osaka_stream.call_args.kwargs["rotate_captain"])
        self.assertEqual(agent.osaka_stream.call_args.kwargs["rotate_captain_margin"], 20)

    def test_osaka_rotation_fields_are_available_in_daily_and_workflows(self):
        for fields in (server._SCRIPTS["osaka"]["params"],
                       server._workflow.NODE_REGISTRY["osaka"]["params"]):
            self.assertTrue({"rotate_captain", "rotate_captain_margin"}.issubset({field["key"] for field in fields}))
        daily = next(field for field in server._SCRIPTS["daily"]["params"] if field["key"] == "rotate_captain")
        self.assertIn("osaka", daily["visibleWhen"]["is_any"])

    def test_daily_sortie_carries_preset_to_the_execution_step(self):
        with patch.object(server, "_resolve_team",
                          return_value=(4, {"id": "pf2"}, None)):
            plan = server._daily_plan_inputs({
                "sortie_mode": "sortie", "team_no": "preset:pf2"})[2]
        self.assertEqual(plan["team_no"], 4)
        self.assertEqual(plan["formation_id"], "pf2")

    def test_deleted_daily_preset_is_not_silently_changed_to_a_team(self):
        with patch.object(server, "_resolve_team",
                          return_value=(None, None, "预设已删除")):
            plan = server._daily_plan_inputs({
                "sortie_mode": "sortie", "team_no": "preset:gone"})[2]
        self.assertIsNone(plan["team_no"])
        self.assertEqual(plan["formation_error"], "预设已删除")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "panel_settings.json"
        self.patcher = patch.object(server, "_SETTINGS_FILE", self.file)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _write(self, settings):
        self.file.write_text(json.dumps(settings, ensure_ascii=False),
                             encoding="utf-8")

    def _read(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def test_snapshots_config_page_values_for_current_mode(self):
        self._write({"params": {
            "daily": {"sortie_mode": "sortie", "loops": 5},
            "sortie": {"repair_threshold": "heavy", "repair_on_injury": "stop",
                       "formation": "方阵", "irrelevant": "x"},
        }})
        server._migrate_daily_battle_settings()
        daily = self._read()["params"]["daily"]
        self.assertEqual(daily["repair_threshold"], "heavy")
        self.assertEqual(daily["repair_on_injury"], "stop")
        self.assertEqual(daily["formation"], "方阵")
        self.assertNotIn("irrelevant", daily)  # 非战斗行为键不拷
        self.assertEqual(daily["loops"], 5)

    def test_never_overwrites_existing_daily_values(self):
        self._write({"params": {
            "daily": {"sortie_mode": "sortie", "repair_threshold": "medium"},
            "sortie": {"repair_threshold": "heavy"},
        }})
        server._migrate_daily_battle_settings()
        server._migrate_daily_battle_settings()  # 跑两遍也幂等
        self.assertEqual(self._read()["params"]["daily"]["repair_threshold"],
                         "medium")

    def test_no_daily_or_unknown_mode_is_noop(self):
        self._write({"params": {"sortie": {"repair_threshold": "heavy"}}})
        server._migrate_daily_battle_settings()
        self.assertNotIn("daily", self._read()["params"])

        self._write({"params": {"daily": {"sortie_mode": "none"},
                                "sortie": {"repair_threshold": "heavy"}}})
        server._migrate_daily_battle_settings()
        self.assertNotIn("repair_threshold", self._read()["params"]["daily"])

    def test_template_flows_battle_keys_into_daily_sortie_node(self):
        settings = {"params": {"daily": {
            "steps": ["出阵"], "sortie_mode": "yosari",
            "repair_threshold": "heavy", "repair_on_injury": "stop"}}}
        template = make_template(settings, {}, ["登录", "出阵"])
        node = next(n for n in template["nodes"] if n["type"] == "daily_sortie")
        self.assertEqual(node["params"]["repair_threshold"], "heavy")
        self.assertEqual(node["params"]["repair_on_injury"], "stop")

    def test_daily_sortie_catalog_has_no_forge_fields(self):
        fields = server._workflow.NODE_REGISTRY["daily_sortie"]["params"]
        keys = {field["key"] for field in fields}
        self.assertNotIn("forge_times", keys)
        self.assertFalse(keys.intersection({
            "recipe_charcoal", "recipe_steel", "recipe_coolant",
            "recipe_whetstone",
        }))


if __name__ == "__main__":
    unittest.main()
