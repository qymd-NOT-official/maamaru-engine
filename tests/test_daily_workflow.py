"""默认日课、旧流程投影/回滚和结束动作契约。所有游戏和电源动作均使用替身。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_workflow import _FakeAgent, _node, workflow
from panel import server
from panel.daily_workflow import make_template
from touken.flows.daily import DailyMixin


class DailyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.status = patch.object(workflow, "STATUS_DIR", self.root)
        self.status.start()
        self.addCleanup(self.status.stop)
        self.settings = patch.object(server, "_load_panel_settings", return_value={})
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def run_plan(self, nodes, after="none", agent=None, daily_mode=False):
        agent = agent or _FakeAgent()
        with patch.object(workflow, "_finale", return_value=iter(())):
            messages = list(workflow.run_workflow("fake.json", nodes, lambda _: agent,
                                                 after=after, daily_mode=daily_mode))
        return agent, messages

    def test_builtin_is_read_only_until_saved_and_copy_keeps_daily_behavior(self):
        default = workflow.list_presets()[0]
        self.assertEqual(default["id"], workflow.DAILY_PRESET_ID)
        self.assertEqual(default["after"], "none")
        self.assertTrue(default["daily_mode"])
        self.assertFalse((self.root / "workflows.json").exists())
        types = [n["type"] for n in default["nodes"]]
        self.assertEqual(types, ["login", "signin", "free_gift", "practice", "expedition",
                                 "naihanka", "forge", "dismantle", "synthesize", "daily_sortie",
                                 "task_rewards", "snapshot", "ledger_sync"])
        copy = workflow.create_preset({**default, "name": "我的日课"})
        self.assertNotEqual(copy["id"], default["id"])
        self.assertTrue(copy["daily_mode"])
        workflow.update_preset(default["id"], {**default, "name": "早课", "after": "logout"})
        self.assertEqual(workflow.find_preset(default["id"])["after"], "logout")
        self.assertEqual(workflow.find_preset(copy["id"])["after"], "none")
        with self.assertRaises(workflow.WorkflowError):
            workflow.delete_preset(default["id"])

    def test_daily_checklist_retains_disabled_parameters_and_discards_stale_projection(self):
        nodes = [_node("practice")]
        ui = {"startTime": "08:00", "rows": [
            {"id": "practice", "label": "演练", "enabled": True, "nodes": nodes},
            {"id": "sugar", "label": "炼糖", "enabled": False, "nodes": [_node("sugar")]},
        ]}
        saved = workflow.create_preset({"name": "日课设置", "nodes": nodes, "daily_ui": ui})
        self.assertEqual(workflow.find_preset(saved["id"])["daily_ui"], ui)
        renamed = workflow.update_preset(saved["id"], {"name": "早课"})
        self.assertEqual(renamed["daily_ui"], ui)
        changed = workflow.update_preset(saved["id"], {"nodes": [_node("signin")]})
        self.assertIsNone(changed["daily_ui"])

    def test_template_imports_selected_steps_and_day_specific_parameters(self):
        saved = {"params": {"daily": {"steps": ["演练", "锻刀", "出阵"], "after": "shutdown",
                    "sortie_mode": "yosari", "yosari_runs": 8, "team_no": "2"}}}
        template = make_template(saved, {"daily": {"forge_times": 5}}, server._DAILY_STEPS)
        self.assertEqual([n["type"] for n in template["nodes"]],
                         ["practice", "forge", "daily_sortie", "ledger_sync"])
        self.assertEqual(template["nodes"][0]["params"], {})
        self.assertEqual(template["nodes"][1]["params"], {"times": 5})
        self.assertEqual(template["nodes"][2]["params"]["yosari_runs"], 8)
        self.assertEqual(template["after"], "shutdown")

    def test_ledger_sync_appended_last_and_can_opt_out(self):
        template = make_template({"params": {"daily": {"steps": ["登录"]}}},
                                 {}, server._DAILY_STEPS)
        self.assertEqual(template["nodes"][-1]["type"], "ledger_sync")
        self.assertEqual(template["nodes"][-1]["on_error"], "continue")

        off = make_template({"params": {"daily": {"steps": ["登录"],
                                                  "ledger_sync": False}}},
                            {}, server._DAILY_STEPS)
        self.assertNotIn("ledger_sync",
                         [n["type"] for n in off["nodes"]])

    def test_ledger_sync_node_writes_then_burns(self):
        from touken import youzu_log
        run = server._workflow.NODE_REGISTRY["ledger_sync"]["run"]
        log_file = self.root / "pulled.log"
        log_file.write_text("x", encoding="utf-8")
        agent = Mock()
        agent.maa.adb_path = "adb"
        agent.maa.adb_address = "127.0.0.1:16384"
        with patch.object(youzu_log, "pull_log", return_value=log_file), \
             patch.object(youzu_log, "write_ledger",
                          return_value={"observations_written": 2,
                                        "changes_written": 5}) as write:
            messages = list(run(agent, {}, None))
        write.assert_called_once()
        self.assertFalse(log_file.exists())  # 原档阅后即焚
        self.assertTrue(any("收支 5 条" in m for m in messages))

    def test_ledger_sync_node_failure_never_breaks_daily(self):
        from touken import youzu_log
        run = server._workflow.NODE_REGISTRY["ledger_sync"]["run"]
        agent = Mock()
        agent.maa.adb_path = "adb"
        agent.maa.adb_address = "127.0.0.1:16384"
        with patch.object(youzu_log, "pull_log",
                          side_effect=RuntimeError("adb 炸了")):
            messages = list(run(agent, {}, None))
        self.assertTrue(any("账本同步失败" in m and "adb 炸了" in m
                            for m in messages))

    def test_template_uses_saved_forge_values_without_leaking_them_into_sortie(self):
        saved = {"params": {"daily": {
            "steps": ["锻刀", "出阵"], "forge_times": 7,
            "recipe_charcoal": 333, "recipe_steel": 444,
            "recipe_coolant": 555, "recipe_whetstone": 666,
            "sortie_mode": "sortie", "chapter": 2, "map_no": 3,
        }}}
        template = make_template(
            saved, {"daily": {"forge_times": 5}}, server._DAILY_STEPS)
        forge = next(n for n in template["nodes"] if n["type"] == "forge")
        sortie = next(n for n in template["nodes"]
                      if n["type"] == "daily_sortie")
        self.assertEqual(forge["params"], {
            "times": 7, "recipe_charcoal": 333, "recipe_steel": 444,
            "recipe_coolant": 555, "recipe_whetstone": 666,
        })
        self.assertEqual(sortie["params"], {
            "sortie_mode": "sortie", "chapter": 2, "map_no": 3,
        })

    def test_legacy_last_logout_projects_without_writing_then_backs_up_on_save(self):
        path = self.root / "workflows.json"
        original = json.dumps({"presets": [{"id": "old", "name": "旧流程", "nodes": [
            _node("signin"), _node("logout", params={"mode": "sleep"})]}]})
        path.write_text(original, encoding="utf-8")
        projected = workflow.find_preset("old")
        self.assertEqual(projected["after"], "sleep")
        self.assertEqual([n["type"] for n in projected["nodes"]], ["signin"])
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        workflow.update_preset("old", projected)
        self.assertEqual(path.with_suffix(".json.bak").read_text(encoding="utf-8"), original)
        # Restoring the backup re-opens with the same meaning and order.
        path.write_bytes(path.with_suffix(".json.bak").read_bytes())
        self.assertEqual(workflow.find_preset("old"), projected)

    def test_middle_logout_is_never_reordered_and_conflicting_finish_is_rejected(self):
        nodes = [_node("logout"), _node("signin")]
        old = workflow.present_preset({"id": "old", "name": "旧", "nodes": nodes})
        self.assertEqual(old["nodes"], nodes)
        self.assertEqual(old["after"], "none")
        with self.assertRaises(workflow.WorkflowError):
            workflow.create_preset({"name": "重复下班", "nodes": nodes, "after": "sleep"})
        with self.assertRaises(workflow.WorkflowError):
            workflow.create_preset({"name": "非法", "nodes": [_node("signin")], "after": "shutdown_pc"})

    def test_failed_atomic_replace_leaves_original_and_backup_intact(self):
        preset = workflow.create_preset({"name": "原版", "nodes": [_node("signin")]})
        path = self.root / "workflows.json"
        original = path.read_bytes()
        with patch.object(workflow.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                workflow.update_preset(preset["id"], {"name": "新版"})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.with_suffix(".json.bak").read_bytes(), original)

    def test_finish_modes_call_exact_flags_and_skip_navigation(self):
        for mode in ("none", "logout", "shutdown", "sleep"):
            with self.subTest(mode=mode):
                agent, messages = self.run_plan([_node("signin")], after=mode)
                calls = [c for c in agent.calls if c[0] == "logout_stream"]
                if mode == "none":
                    self.assertFalse(calls)
                    self.assertTrue(any(c[0] == "quick_peek" for c in agent.calls))
                else:
                    self.assertEqual(calls[0][2], {"kill_game": True,
                        "close_emulator": mode in ("shutdown", "sleep"), "sleep_pc": False})
                    if mode == "sleep":
                        self.assertEqual(calls[1][2], {"kill_game": False, "close_emulator": False, "sleep_pc": True})
                    else:
                        self.assertEqual(len(calls), 1)
                    self.assertFalse(any(c[0] == "quick_peek" for c in agent.calls))

    def test_stopped_failure_does_not_exit_but_continue_policy_reaches_finish(self):
        for policy, should_exit in (("stop", False), ("continue", True)):
            agent = _FakeAgent({"signin_stream": ["[fake] ✗ 失败"]})
            self.run_plan([_node("signin", on_error=policy)], after="sleep", agent=agent)
            self.assertEqual(any(c[0] == "logout_stream" for c in agent.calls), should_exit)

    def test_daily_uses_own_practice_settings_expedition_and_snapshot(self):
        agent = _FakeAgent()
        agent._daily_expedition_step = Mock(return_value=iter(["✓"]))
        agent._closing_snapshot_stream = Mock(return_value=iter(["✓"]))
        agent._dismantle_step = Mock(return_value=iter(["✓"]))
        with patch.object(server, "_load_panel_settings", return_value={"params": {
                "practice": {"team_no": "4", "formation_mode": "auto", "formation": "横队阵"}}}), \
             patch("panel.scheduler.load_config", return_value={"common_plan": []}):
            self.run_plan([_node("practice", params={"team_no": "4", "formation_mode": "auto", "formation": "横队阵"}), _node("expedition"), _node("forge", params={"times": 5}),
                           _node("dismantle"), _node("snapshot")], agent=agent, daily_mode=True)
        call = next(c for c in agent.calls if c[0] == "practice_stream")
        self.assertEqual(call[2], {"dry_run": False, "team_no": 4, "formation_mode": "auto", "formation": "横队阵"})
        agent._daily_expedition_step.assert_called_once_with([])
        agent._closing_snapshot_stream.assert_called_once_with(True)
        agent._dismantle_step.assert_called_once_with()

    def test_daily_practice_reuses_the_normal_preset_team_resolver(self):
        agent = _FakeAgent()

        def choose_preset(_agent, params, default=3):
            self.assertEqual(params["team_no"], "preset:pf1")
            yield "[部队预设] 已套用"
            return 4

        with patch.object(server, "_load_panel_settings", return_value={
                "params": {"practice": {"team_no": "preset:pf1"}}}), \
             patch.object(server, "_team_with_preset_stream", choose_preset):
            _, messages = self.run_plan(
                [_node("practice", params={"team_no": "preset:pf1"})], agent=agent, daily_mode=True)
        call = next(c for c in agent.calls if c[0] == "practice_stream")
        self.assertEqual(call[2]["team_no"], 4)
        self.assertIn("[部队预设] 已套用", messages)

    def test_daily_sortie_does_not_depart_when_preset_application_fails(self):
        class Host(DailyMixin):
            pass

        host = Host()
        host.raid_stream = Mock(side_effect=AssertionError(
            "预设失败后不应进入活动"))

        def rejected(*args, **kwargs):
            yield "[部队预设] ✗ 档案有歧义，没有动游戏"
            return False

        report = []
        with patch("touken.custom_formations.apply_formation_preset_by_id_stream",
                   rejected):
            messages = list(host._sortie_step({"sortie": {
                "mode": "raid", "team_no": 3, "formation_id": "pf1",
            }}, report))
        host.raid_stream.assert_not_called()
        self.assertEqual(report, [("出阵", "✗ 部队预设未套用")])
        self.assertTrue(any("本次出阵不开始" in message for message in messages))

    def test_report_precedes_pc_sleep(self):
        agent = _FakeAgent()
        def finale(report, payload):
            self.assertTrue(payload["finished"])
            self.assertFalse(any(c[2].get("sleep_pc") for c in agent.calls))
            yield "已记录成绩单"
        with patch.object(workflow, "_finale", side_effect=finale):
            messages = list(workflow.run_workflow("fake.json", [_node("signin")], lambda _: agent, after="sleep"))
        self.assertLess(messages.index("已记录成绩单"), messages.index("【工作流】成绩单已记录，准备休眠电脑"))

    def test_daily_sortie_uses_own_battle_settings_not_config_page(self):
        # issue#7：日课与「配置」页彻底切割。配置页的值不再继承；
        # 战斗行为只认日课节点自己的参数，缺键回落硬默认。
        agent = _FakeAgent()
        agent._sortie_step = Mock(return_value=iter(["✓"]))
        with patch.object(server, "_load_panel_settings", return_value={"params": {
                "yosari": {"formation": "横队阵", "repair_on_injury": "return"}}}), \
             patch("panel.scheduler.load_config", return_value={"common_plan": []}):
            self.run_plan([_node("daily_sortie", params={"sortie_mode": "yosari",
                "team_no": "2", "yosari_runs": 6})], agent=agent, daily_mode=True)
        plan = agent._sortie_step.call_args.args[0]["sortie"]
        self.assertEqual((plan["team_no"], plan["loops"]), (2, 6))
        # 配置页存的 横队阵/return 不再漏进日课
        self.assertEqual((plan["formation"], plan["repair_on_injury"]), ("鱼鳞阵", "continue"))

        agent2 = _FakeAgent()
        agent2._sortie_step = Mock(return_value=iter(["✓"]))
        with patch("panel.scheduler.load_config", return_value={"common_plan": []}):
            self.run_plan([_node("daily_sortie", params={"sortie_mode": "yosari",
                "formation": "横队阵", "repair_on_injury": "repair_stop"})],
                agent=agent2, daily_mode=True)
        plan2 = agent2._sortie_step.call_args.args[0]["sortie"]
        self.assertEqual((plan2["formation"], plan2["repair_on_injury"]),
                         ("横队阵", "repair_stop"))

    def test_no_sortie_is_reported_as_planned_skip(self):
        agent = _FakeAgent()
        agent._sortie_step = Mock(side_effect=AssertionError("不出阵不能调用玩法"))
        self.run_plan([_node("daily_sortie", params={"sortie_mode": "none"})], agent=agent)
        agent._sortie_step.assert_not_called()
        report = json.loads((self.root / "latest_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["steps"][0]["status"], "⏭ 按安排不出阵")

    def test_daily_login_failure_and_update_gate_stop_even_with_continue(self):
        agent = _FakeAgent()
        agent._popup_sweep = lambda *a, **kw: False
        self.run_plan([_node("login", "continue"), _node("signin")], "sleep", agent, True)
        self.assertFalse(any(c[0] in ("signin_stream", "logout_stream") for c in agent.calls))
        agent = _FakeAgent()
        agent._daily_update_gate = lambda: iter(())
        self.run_plan([_node("signin", "continue")], "sleep", agent, True)
        self.assertFalse(any(c[0] in ("signin_stream", "logout_stream") for c in agent.calls))

    def test_legacy_daily_after_overrides_conflicting_old_logout_config(self):
        class Host(DailyMixin):
            config = {"emulator_manager": "fake-manager", "daily": {"logout": {
                "kill_game": False, "close_emulator": True, "sleep_pc": True}}}
            def _daily_update_gate(self):
                yield from ()
                return True
            def _flush_report(self, *args, **kwargs):
                return None
        for mode in ("none", "logout", "shutdown", "sleep"):
            host = Host()
            host.logout_stream = Mock(return_value=iter(()))
            with patch("touken.notify.notify_destination", return_value=""), \
                 patch("touken.emulator.shutdown_emulator", return_value=True) as shutdown, \
                 patch("touken.emulator.sleep_computer", return_value=True) as sleep_pc, \
                 patch("touken.flows.daily.time.sleep"), \
                 patch("subprocess.Popen", side_effect=AssertionError("测试禁止启动真实进程")) as process:
                list(host.daily_stream(only=["不运行任务"], after=mode))
                process.assert_not_called()
            self.assertEqual(shutdown.call_count, int(mode in ("shutdown", "sleep")))
            self.assertEqual(sleep_pc.call_count, int(mode == "sleep"))
            if mode == "none":
                host.logout_stream.assert_not_called()
            else:
                host.logout_stream.assert_called_once_with(kill_game=True,
                    close_emulator=False, sleep_pc=False)


if __name__ == "__main__":
    unittest.main()
