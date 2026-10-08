"""今日时段表大总管：到点排队开工（runner 忙就等），换日才算错过。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import day_conductor as dc
from panel import day_plan as dp
from panel import server  # noqa: F401 - register gameplay workflow nodes
from panel.day_plan import load_plan, save_plan
from touken.flow_control import FlowAborted


DAY = 2_000_000_000


def timeline(*, occupied=None, now=DAY + 8 * 3600, activity=True):
    card = ({"name": "联队战", "seconds_per_loop": 420,
             "remaining_runs": 36, "event_end_at": DAY + 24 * 3600,
             "occupied": occupied or []} if activity else None)
    return {"day_start": DAY, "now": now, "expeditions": [], "activity": card}


WF_PRESET = {"id": "wf1", "name": "杂物工作流", "after": "none",
             "daily_mode": False,
             "nodes": [{"type": "wait_until", "params": {"time": "04:05"}}]}
WF_PRESET_EDITED = {"id": "wf1", "name": "杂物工作流", "after": "none",
                    "daily_mode": False,
                    "nodes": [{"type": "wait_until", "params": {"time": "04:06"}}]}


class FakeRunner:
    def __init__(self):
        self.is_running = False
        self.current_run_id = None
        self.last_status = "completed"
        self.calls = []

    @property
    def last_run_result(self):
        return self.current_run_id, self.last_status

    def start(self, script, config_path, params):
        self.calls.append((script, config_path, params))
        self.is_running = True
        self.current_run_id = "run-1"
        return "run-1"


class DayConductorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state_path = Path(self.folder.name) / "conductor.json"
        self.plan_path = Path(self.folder.name) / "plan.json"
        self.plan = save_plan(DAY, DAY + 24 * 3600,
                              [{"start_min": 600, "kind": "raid", "runs": 8}],
                              self.plan_path)
        self.runner = FakeRunner()
        self.messages = []
        self.team_patch = patch.object(dc, "_for_team", side_effect=lambda tl, team: tl)
        self.team_patch.start()
        self.addCleanup(self.team_patch.stop)

    def arm(self):
        return dc.arm(self.plan, timeline(), dc.BUILTIN_ID, {}, self.state_path)

    def tick(self, now, tl=None):
        dc.tick(now, self.runner, lambda: tl or timeline(), lambda: {},
                "config.json", lambda script, msg: self.messages.append(msg),
                self.state_path, self.plan_path)

    def test_arm_starts_workflow_once_with_exact_block_runs(self):
        state = self.arm()
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["blocks"][0]["label"], "联队战 8 圈")
        self.tick(DAY + 600 * 60 - 1)
        self.assertEqual(self.runner.calls, [])
        self.tick(DAY + 600 * 60)
        self.assertEqual(len(self.runner.calls), 1)
        script, _, params = self.runner.calls[0]
        self.assertEqual(script, "workflow")
        self.assertEqual(params["scheduled_raid_runs"], 8)
        self.assertEqual(params["workflow_id"], dc.BUILTIN_ID)
        self.tick(DAY + 600 * 60 + 5)
        self.assertEqual(len(self.runner.calls), 1)
        self.runner.is_running = False
        self.tick(DAY + 600 * 60 + 10)
        self.assertEqual(dc.load_state(self.state_path)["blocks"][0]["status"], "ended")
        self.tick(DAY + 600 * 60 + 15)
        self.assertEqual(len(self.runner.calls), 1)

    def test_adding_workflow_keeps_finished_raid_and_only_starts_new_workflow(self):
        self.arm()
        self.tick(DAY + 600 * 60)
        self.runner.is_running = False
        self.tick(DAY + 660 * 60)
        finished = dc.load_state(self.state_path)["blocks"][0]
        plan = save_plan(DAY, DAY + 24 * 3600,
                         [*self.plan["blocks"], {"start_min": 670, "kind": "workflow",
                                                "workflow_id": "wf1"}], self.plan_path)
        with patch.object(dc.workflow, "find_preset", return_value=WF_PRESET):
            state = dc.arm(plan, timeline(), dc.BUILTIN_ID, {}, self.state_path)
            self.assertEqual(state["blocks"][0], finished)
            self.tick(DAY + 670 * 60)
        self.assertEqual(len(self.runner.calls), 2)
        params = self.runner.calls[-1][2]
        self.assertEqual(params, {"workflow_id": "wf1"})
        self.assertNotIn("scheduled_raid_runs", params)

    def test_rearm_preserves_interrupted_run_even_when_settings_change(self):
        self.arm()
        state = dc.load_state(self.state_path)
        state["blocks"][0].update(status="interrupted", run_id="stopped-run",
                                   reason="手动停止", finished_at=DAY + 601 * 60)
        dc._save(state, self.state_path)
        rearmed = dc.arm(self.plan, timeline(), dc.BUILTIN_ID,
                         {"rotate_captain": True}, self.state_path)
        self.assertEqual(rearmed["blocks"][0], state["blocks"][0])
        self.tick(DAY + 610 * 60)
        self.assertEqual(self.runner.calls, [])

    def test_changed_block_is_new_authorization_and_next_day_is_not_reused(self):
        self.arm()
        state = dc.load_state(self.state_path)
        state["blocks"][0].update(status="ended", run_id="finished-run")
        dc._save(state, self.state_path)
        changed = {**self.plan, "blocks": [{"start_min": 670, "kind": "raid", "runs": 8}]}
        self.assertEqual(dc.arm(changed, timeline(), dc.BUILTIN_ID, {},
                               self.state_path)["blocks"][0]["status"], "pending")
        dc._save({**state, "day_start": DAY - 86400}, self.state_path)
        self.assertEqual(self.arm()["blocks"][0]["status"], "pending")

    def test_busy_at_due_stays_pending_then_starts_when_free(self):
        # 新语义：到点时 runner 忙，块保持 pending 排队等——60 秒 missed 枪毙已删除；
        # runner 空出来就开工。错过不补跑只在换日结算。
        self.arm()
        self.runner.is_running = True
        self.runner.current_run_id = "other"
        self.tick(DAY + 600 * 60)
        self.assertEqual(dc.load_state(self.state_path)["blocks"][0]["status"],
                         "pending")
        self.tick(DAY + 600 * 60 + 600)
        self.assertEqual(self.runner.calls, [])
        self.runner.is_running = False
        self.runner.current_run_id = None
        self.tick(DAY + 600 * 60 + 605)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(dc.load_state(self.state_path)["blocks"][0]["status"],
                         "running")

    def test_past_due_block_arms_and_starts_immediately(self):
        # 新语义：开工时间过去不再拒绝 arm，tick 时按「到点」处理立刻排队开工。
        plan = save_plan(DAY, DAY + 24 * 3600,
                         [{"start_min": 300, "kind": "raid", "runs": 8}],
                         self.plan_path)
        dc.arm(plan, timeline(), dc.BUILTIN_ID, {}, self.state_path)
        self.tick(DAY + 8 * 3600)
        self.assertEqual(len(self.runner.calls), 1)

    def test_new_expedition_collision_blocks_at_start(self):
        self.arm()
        blocked = timeline(occupied=[{"start_min": 598, "end_min": 605,
                                      "label": "10:00 部队三派遣"}])
        self.tick(DAY + 600 * 60, blocked)
        state = dc.load_state(self.state_path)
        self.assertEqual(state["blocks"][0]["status"], "blocked")
        self.assertIn("派遣", state["blocks"][0]["reason"])
        self.assertEqual(self.runner.calls, [])

    def test_changed_plan_disarms_before_start(self):
        self.arm()
        save_plan(DAY, DAY + 24 * 3600,
                  [{"start_min": 610, "kind": "raid", "runs": 8}],
                  self.plan_path)
        self.tick(DAY + 600 * 60)
        self.assertFalse(dc.load_state(self.state_path)["enabled"])
        self.assertEqual(self.runner.calls, [])

    def test_changed_raid_settings_no_longer_disarm(self):
        # 新语义：联队战设置/任务流在 arm 后被改，不再让大总管当场停用——
        # projection 挂「请重新开启」提醒，开工时工人侧还有签名兜底。
        self.arm()
        dc.tick(DAY + 600 * 60, self.runner, timeline,
                lambda: {"team_no": "4"}, "config.json",
                lambda script, msg: self.messages.append(msg),
                self.state_path, self.plan_path)
        state = dc.load_state(self.state_path)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["blocks"][0]["status"], "running")
        proj = dc.projection(self.plan, timeline(), {"team_no": "4"},
                             self.state_path)
        self.assertIn("改过了", "".join(proj["issues"]))

    def test_interrupted_run_disarms_next_block(self):
        self.arm()
        self.tick(DAY + 600 * 60)
        self.runner.is_running = False
        self.runner.current_run_id = None  # 服务重启后不再掌握旧工人
        self.tick(DAY + 600 * 60 + 10)
        state = dc.load_state(self.state_path)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["blocks"][0]["status"], "interrupted")

    def test_failed_worker_disarms_later_blocks(self):
        self.arm()
        self.tick(DAY + 600 * 60)
        self.runner.is_running = False
        self.runner.last_status = "failed"
        self.tick(DAY + 600 * 60 + 10)
        state = dc.load_state(self.state_path)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["blocks"][0]["status"], "interrupted")
        self.assertEqual(state["blocks"][0]["reason"], "任务流失败，后续已停用")

    def test_disarm_preserves_current_run_but_prevents_next_block(self):
        self.arm()
        self.tick(DAY + 600 * 60)
        dc.disarm(self.state_path)
        self.assertFalse(dc.load_state(self.state_path)["enabled"])
        self.assertTrue(self.runner.is_running)
        self.assertEqual(len(self.runner.calls), 1)

    def test_midnight_and_0359_tasks_start_before_rollover(self):
        for minute in (1440, 1679):
            with self.subTest(minute=minute):
                self.runner = FakeRunner()
                self.state_path = Path(self.folder.name) / f"conductor-{minute}.json"
                plan = save_plan(DAY, None, [{"start_min": minute,
                    "kind": "workflow", "workflow_id": "wf1"}], self.plan_path)
                with patch.object(dc.workflow, "find_preset", return_value=WF_PRESET):
                    dc.arm(plan, timeline(activity=False), dc.BUILTIN_ID, {}, self.state_path)
                    self.tick(DAY + minute * 60)
                self.assertEqual(len(self.runner.calls), 1)
                self.assertTrue(dc.load_state(self.state_path)["enabled"])
                self.tick(DAY + 28 * 3600)
                self.assertTrue(self.runner.is_running)
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(dc.load_state(self.state_path)["blocks"][0]["status"], "running")

    def test_rollover_marks_pending_blocks_missed(self):
        # missed 的唯一出口：换日时仍 pending 的块，标 missed 并停用。
        self.arm()
        self.tick(DAY + 28 * 3600)
        state = dc.load_state(self.state_path)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["blocks"][0]["status"], "missed")
        self.assertEqual(self.runner.calls, [])

    def test_v1_plan_and_state_migrate(self):
        self.plan_path.write_text(json.dumps({
            "version": 1, "day_start": DAY, "event_end_at": DAY + 24 * 3600,
            "blocks": [{"start_min": 600, "runs": 8}]}), encoding="utf-8")
        plan = load_plan(self.plan_path)
        self.assertEqual(plan["version"], 2)
        self.assertEqual(plan["blocks"][0]["kind"], "raid")
        self.arm()
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 2)
        self.assertTrue(raw["blocks"][0]["workflow_signature"])
        # 旧面板留下的 v1 state：读入内存即 v2，顶层签名搬进块里。
        v1_state = {"version": 1, "enabled": True, "day_start": DAY,
                    "plan_signature": "x", "workflow_id": dc.BUILTIN_ID,
                    "workflow_name": "按联队战设置开工",
                    "workflow_signature": "sig-1",
                    "blocks": [{"start_min": 600, "runs": 8,
                                "status": "pending"}]}
        self.state_path.write_text(json.dumps(v1_state), encoding="utf-8")
        state = dc.load_state(self.state_path)
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["workflow_id"], dc.BUILTIN_ID)
        block = state["blocks"][0]
        self.assertEqual(block["kind"], "raid")
        self.assertEqual(block["label"], "联队战 8 圈")
        self.assertEqual(block["workflow_signature"], "sig-1")

    def test_workflow_block_arms_and_starts_with_own_id(self):
        with patch.object(dc.workflow, "find_preset", return_value=WF_PRESET):
            plan = save_plan(DAY, None,  # 纯 workflow 安排 event_end_at 可为 None
                             [{"start_min": 600, "kind": "workflow",
                               "workflow_id": "wf1"}], self.plan_path)
            state = dc.arm(plan, timeline(), dc.BUILTIN_ID, {}, self.state_path)
        self.assertEqual(state["blocks"][0]["label"], "杂物工作流")
        with patch.object(dc.workflow, "find_preset", return_value=WF_PRESET):
            self.tick(DAY + 600 * 60)
        script, _, params = self.runner.calls[0]
        self.assertEqual(script, "workflow")
        self.assertEqual(params, {"workflow_id": "wf1"})
        self.assertEqual(dc.load_state(self.state_path)["blocks"][0]["status"],
                         "running")

    def test_workflow_block_blocked_after_preset_edited(self):
        # 预检失败标 blocked 且不阻塞后面的块：后面的 daily 到点照常开工。
        plan = save_plan(DAY, None,
                         [{"start_min": 600, "kind": "workflow",
                           "workflow_id": "wf1"},
                          {"start_min": 630, "kind": "daily"}],
                         self.plan_path)
        with patch.object(dc.workflow, "find_preset", return_value=WF_PRESET):
            dc.arm(plan, timeline(), dc.BUILTIN_ID, {}, self.state_path)
        self.assertEqual(self.runner.calls, [])
        with patch.object(dc.workflow, "find_preset",
                          return_value=WF_PRESET_EDITED):
            self.tick(DAY + 630 * 60)
        state = dc.load_state(self.state_path)
        self.assertEqual(state["blocks"][0]["status"], "blocked")
        self.assertIn("改过了", state["blocks"][0]["reason"])
        self.assertEqual(state["blocks"][1]["status"], "running")
        self.assertEqual(self.runner.calls[0][0], "daily")
        self.assertIn("一键日课开工", self.messages[-1])

    def test_workflow_block_arm_requires_existing_preset(self):
        with patch.object(dc.workflow, "find_preset", return_value=None):
            plan = save_plan(DAY, None,
                             [{"start_min": 600, "kind": "workflow",
                               "workflow_id": "ghost"}], self.plan_path)
            with self.assertRaisesRegex(ValueError, "找不到"):
                dc.arm(plan, timeline(), dc.BUILTIN_ID, {}, self.state_path)

    def test_daily_block_starts_daily_script(self):
        plan = save_plan(DAY, None, [{"start_min": 600, "kind": "daily"}],
                         self.plan_path)
        dc.arm(plan, timeline(), dc.BUILTIN_ID, {}, self.state_path)
        self.tick(DAY + 600 * 60)
        script, _, params = self.runner.calls[0]
        self.assertEqual(script, "daily")
        self.assertEqual(params, {})
        self.assertIn("一键日课开工", self.messages[-1])
        self.assertEqual(dc.load_state(self.state_path)["blocks"][0]["label"],
                         "一键日课")

    def test_unknown_version_and_backup_preserve_previous_file(self):
        self.state_path.write_text('{"version":0,"old":"kept"}', encoding="utf-8")
        self.assertIsNone(dc.load_state(self.state_path))
        self.arm()
        backup = json.loads(self.state_path.with_suffix(".json.bak").read_text(
            encoding="utf-8"))
        self.assertEqual(backup["old"], "kept")
        before = self.state_path.read_text(encoding="utf-8")
        with patch.object(Path, "replace", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                dc.disarm(self.state_path)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_mixed_flow_keeps_other_steps_and_requires_one_raid(self):
        with patch.object(dc.workflow, "find_preset", return_value={
            "id": "mixed", "name": "混合", "after": "none", "daily_mode": False,
            "nodes": [{"type": "raid", "params": {}},
                      {"type": "wait_until", "params": {"time": "04:05"}}],
        }):
            spec = dc.workflow_spec("mixed", {})
            self.assertEqual([node["type"] for node in spec["nodes"]], ["raid", "wait_until"])
            self.assertEqual(spec["raid_index"], 0)
        for nodes in ([{"type": "signin", "params": {}}],
                      [{"type": "raid", "params": {}}, {"type": "raid", "params": {}}],
                      [{"type": "raid", "params": {}, "on_error": "continue"}]):
            with patch.object(dc.workflow, "find_preset", return_value={"name": "mixed", "nodes": nodes}):
                with self.assertRaisesRegex(ValueError, "一个联队战"):
                    dc.workflow_spec("mixed", {})

    def test_recommendation_overrides_only_raid_in_full_flow(self):
        preset = {"id": "full", "name": "整套", "after": "shutdown", "daily_mode": False,
                  "nodes": [{"type": "boot_emulator", "params": {}, "on_error": "stop"},
                            {"type": "login", "params": {}, "on_error": "stop"},
                            {"type": "yosari", "params": {"runs": 3}, "on_error": "continue"},
                            {"type": "raid", "params": {"rounds": 99, "team_no": "4"}, "on_error": "stop"},
                            {"type": "snapshot", "params": {}, "on_error": "continue"}]}
        before = json.dumps(preset, sort_keys=True)
        with patch.object(dc.workflow, "find_preset", return_value=preset), \
             patch.object(server, "_load_panel_settings", return_value={"params": {}}), \
             patch.object(server._workflow, "run_workflow", side_effect=lambda *a, **k: iter(["ok"])) as run:
            signature = dc.workflow_spec("full", {})["signature"]
            self.assertEqual(list(server._build_workflow("cfg", {
                "workflow_id": "full", "scheduled_raid_runs": 8,
                "scheduled_workflow_signature": signature})), ["ok"])
            plan = run.call_args.args[1]
            self.assertEqual([node["type"] for node in plan],
                             ["boot_emulator", "login", "yosari", "raid", "snapshot"])
            self.assertEqual(plan[2]["params"], {"runs": 3})
            self.assertEqual(plan[3]["params"]["rounds"], 8)
            self.assertEqual(plan[3]["params"]["team_no"], "4")
            self.assertEqual(run.call_args.kwargs["after"], "shutdown")
            self.assertEqual(json.dumps(preset, sort_keys=True), before)
            preset["nodes"][2]["params"]["runs"] = 4
            run.reset_mock()
            with self.assertRaises(FlowAborted):
                list(server._build_workflow("cfg", {"workflow_id": "full", "scheduled_raid_runs": 8,
                    "scheduled_workflow_signature": signature}))
            run.assert_not_called()

    def test_full_flow_signature_covers_after_and_daily_mode(self):
        preset = {"name": "整套", "nodes": [{"type": "signin", "params": {}},
                                               {"type": "raid", "params": {}}]}
        with patch.object(dc.workflow, "find_preset", return_value=preset):
            signature = dc.workflow_spec("full", {})["signature"]
            preset["after"] = "shutdown"
            self.assertNotEqual(dc.workflow_spec("full", {})["signature"], signature)
            preset["after"] = "none"
            preset["daily_mode"] = True
            self.assertNotEqual(dc.workflow_spec("full", {})["signature"], signature)

    def test_worker_uses_booked_runs_without_changing_saved_settings(self):
        settings = {"team_no": "3", "runs": 1, "rounds": 99, "auto_refill": True}
        signature = dc.workflow_spec(dc.BUILTIN_ID, settings)["signature"]
        with patch.object(server, "_load_panel_settings", return_value={
            "params": {"raid": settings}}), \
             patch.object(server._workflow, "run_workflow",
                          side_effect=lambda *args, **kwargs: iter(["ok"])) as run:
            messages = list(server._build_workflow("config.json", {
                "workflow_id": dc.BUILTIN_ID, "scheduled_raid_runs": 24,
                "scheduled_workflow_signature": signature}))
            self.assertEqual(messages, ["ok"])
            plan = run.call_args.args[1]
            self.assertEqual([node["type"] for node in plan],
                             ["boot_emulator", "login", "raid"])
            self.assertTrue(all(node["on_error"] == "stop" for node in plan))
            self.assertEqual(plan[2]["params"]["rounds"], 24)
            self.assertEqual(plan[2]["params"]["runs"], 24)
            # 走真实玩法入口，验证最终交给 raid_stream 的圈数，而非只看中间字段。
            from unittest.mock import Mock
            agent = Mock()
            agent.raid_stream.return_value = iter([])
            def selected(*args):
                yield "ready"
                return 3
            with patch.object(server, "_team_with_preset_stream", selected):
                list(server._build_raid(agent, "cfg", plan[2]["params"]))
            self.assertEqual(agent.raid_stream.call_args.kwargs["max_rounds"], 24)
            self.assertEqual(settings["runs"], 1)
            self.assertEqual(plan[2]["params"]["team_no"], "3")
            self.assertTrue(plan[2]["params"]["auto_refill"])
            self.assertEqual(settings["rounds"], 99)
            run.reset_mock()
            with self.assertRaises(FlowAborted):
                list(server._build_workflow("config.json", {
                    "workflow_id": dc.BUILTIN_ID, "scheduled_raid_runs": 24,
                    "scheduled_workflow_signature": "old"}))
            run.assert_not_called()

    def test_worker_accepts_signature_from_already_running_panel(self):
        # 两版面板分别签预设原文/补入设置后的节点；两者都是同一份授权。
        settings = {"team_no": "3", "rounds": 99, "auto_refill": True}
        old_panel_signature = (
            "70c79a20a34a1f93ff96673d53d559cd252d167bbb75e6d6f0ab694ddc07a4b1")
        running_panel_signature = (
            "30d8f7bae3f19f1c9ca0887a6bf1c1ae570f5e5246a156ec278fbd497c56f884")
        self.assertTrue({old_panel_signature, running_panel_signature}.issubset(
            dc.workflow_spec(dc.BUILTIN_ID, settings)["compatible_signatures"]))
        with patch.object(server, "_load_panel_settings", return_value={
            "params": {"raid": settings}}), \
             patch.object(server._workflow, "run_workflow",
                          side_effect=lambda *args, **kwargs: iter(["ok"])) as run:
            for signature in (old_panel_signature, running_panel_signature):
                self.assertEqual(list(server._build_workflow("config.json", {
                    "workflow_id": dc.BUILTIN_ID, "scheduled_raid_runs": 8,
                    "scheduled_workflow_signature": signature})), ["ok"])
                self.assertEqual(run.call_args.args[1][2]["params"]["rounds"], 8)
            changed_settings = {**settings, "team_no": "4"}
            with patch.object(server, "_load_panel_settings", return_value={
                "params": {"raid": changed_settings}}):
                with self.assertRaises(FlowAborted):
                    list(server._build_workflow("config.json", {
                        "workflow_id": dc.BUILTIN_ID, "scheduled_raid_runs": 8,
                        "scheduled_workflow_signature": running_panel_signature}))

    def test_failed_workflow_step_exits_as_failed_worker(self):
        def failed_flow(*args, **kwargs):
            yield "【工作流】这块翻车了"
            return False

        signature = dc.workflow_spec(dc.BUILTIN_ID, {})["signature"]
        with patch.object(server, "_load_panel_settings", return_value={"params": {}}), \
             patch.object(server._workflow, "run_workflow", side_effect=failed_flow):
            with self.assertRaises(FlowAborted):
                list(server._build_workflow("config.json", {
                    "workflow_id": dc.BUILTIN_ID, "scheduled_raid_runs": 8,
                    "scheduled_workflow_signature": signature}))

    def test_ledger_mode_cannot_arm_through_api(self):
        client = TestClient(server.app)
        with patch.object(server, "_ledger_mode", return_value=True):
            response = client.put("/api/day-conductor", json={
                "enabled": True, "workflow_id": dc.BUILTIN_ID})
        self.assertEqual(response.status_code, 403)


class ScheduleEndpointTests(unittest.TestCase):
    """PUT /api/day-timeline/schedule：保存即开工。落盘路径全部引到临时目录。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state_path = Path(self.folder.name) / "conductor.json"
        self.plan_path = Path(self.folder.name) / "plan.json"
        self.canned = {
            "day_start": DAY, "now": DAY + 8 * 3600,
            "activity": {"name": "联队战", "seconds_per_loop": 420,
                         "remaining_runs": 36, "event_end_at": DAY + 24 * 3600,
                         "occupied": []},
            "conductor": {"enabled": True, "blocks": []},
            "booking": {"version": 2, "day_start": DAY,
                        "event_end_at": DAY + 24 * 3600,
                        "blocks": [{"start_min": 600, "kind": "raid",
                                    "runs": 8}]},
        }
        self.team_patch = patch.object(dc, "_for_team", side_effect=lambda tl, team: tl)
        self.team_patch.start()
        self.addCleanup(self.team_patch.stop)

    def _patches(self, find_preset=None):
        real_arm = dc.arm
        real_disarm = dc.disarm
        real_save = dp.save_plan

        def arm_to_temp(plan, timeline, workflow_id, raid_settings, **kwargs):
            return real_arm(plan, timeline, workflow_id, raid_settings,
                            self.state_path, **kwargs)

        def save_to_temp(day_start, event_end_at, blocks):
            return real_save(day_start, event_end_at, blocks, self.plan_path)

        stack = [
            patch.object(server, "_day_timeline_payload",
                         return_value=self.canned),
            patch.object(server, "_load_panel_settings",
                         return_value={"params": {"raid": {}}}),
            patch.object(dc, "arm", side_effect=arm_to_temp),
            patch.object(dc, "disarm", side_effect=lambda: real_disarm(self.state_path)),
            patch.object(dp, "save_plan", side_effect=save_to_temp),
        ]
        if find_preset is not None:
            stack.append(patch.object(dc.workflow, "find_preset",
                                      return_value=find_preset))
        return stack

    def test_schedule_saves_and_arms_in_one_put(self):
        client = TestClient(server.app)
        stacks = self._patches()
        for item in stacks:
            item.start()
        try:
            response = client.put("/api/day-timeline/schedule", json={
                "blocks": [{"start_min": 600, "kind": "raid", "runs": 8}]})
        finally:
            for item in stacks:
                item.stop()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["conductor"]["enabled"])
        self.assertEqual(body["booking"]["blocks"][0]["kind"], "raid")
        saved = load_plan(self.plan_path)
        self.assertEqual(saved["blocks"][0]["kind"], "raid")
        state = dc.load_state(self.state_path)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["blocks"][0]["label"], "联队战 8 圈")

    def test_schedule_clears_empty_blocks(self):
        dc.arm(self.canned["booking"], self.canned, dc.BUILTIN_ID, {}, self.state_path)
        client = TestClient(server.app)
        stacks = self._patches()
        for item in stacks:
            item.start()
        try:
            response = client.put("/api/day-timeline/schedule", json={"blocks": []})
        finally:
            for item in stacks:
                item.stop()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(load_plan(self.plan_path))
        self.assertFalse(dc.load_state(self.state_path)["enabled"])

    def test_schedule_forbidden_in_ledger_mode(self):
        client = TestClient(server.app)
        with patch.object(server, "_ledger_mode", return_value=True):
            response = client.put("/api/day-timeline/schedule", json={
                "blocks": [{"start_min": 600, "kind": "daily"}]})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()


def test_builtin_scheduled_raid_refills_without_changing_solo_setting():
    settings = {"team_no": "3", "auto_refill": False}
    spec = dc.workflow_spec(dc.BUILTIN_ID, settings)
    assert spec["nodes"][0]["params"]["auto_refill"] is True
    assert settings["auto_refill"] is False


def test_daily_raid_refills_even_with_old_disabled_setting():
    from panel.server import _daily_plan_inputs
    plan = _daily_plan_inputs({'sortie_mode': 'raid', 'raid_rounds': 21,
                               'raid_auto_refill': False})[2]
    assert plan['auto_buy_ticket'] is True
    assert plan['max_buys'] == 21


class RaidRecoveryTests(unittest.TestCase):
    setUp = DayConductorTests.setUp
    arm = DayConductorTests.arm

    def interrupted(self):
        state = self.arm()
        state["enabled"] = False
        state["blocks"][0].update(status="interrupted", run_id="old-run",
                                  reason="执行状态不明，后续已停用")
        dc._save(state, self.state_path)
        return state

    def test_legacy_progress_counts_confirmed_rounds_and_uncertain_one(self):
        block = self.interrupted()["blocks"][0]
        messages = ["[RAID] ===== 第 1/8 圈 =====", "[RAID] 第 1 圈结束",
                    "[RAID] ===== 第 2/8 圈 ====="]
        with patch("panel.log_store.get_store") as store:
            store.return_value.raid_progress_messages.return_value = messages
            self.assertEqual(dc.raid_recovery(block),
                             {"completed": 1, "remaining": 7, "uncertain_round": True})
            store.return_value.raid_progress_messages.return_value += ["[脚本] 已手动停止"]
            self.assertIsNone(dc.raid_recovery(block))

    def test_resume_preserves_original_target_and_backup_after_restart(self):
        old = self.interrupted()
        with patch.object(dc, "raid_recovery", return_value={"completed": 2, "remaining": 6, "uncertain_round": True}), patch.object(dc.time, "time", return_value=DAY + 610 * 60):
            dc.resume_raid("old-run", True, self.runner, lambda: timeline(), lambda: {},
                           "config.json", lambda *args: None, self.state_path, self.plan_path)
        restored = dc.load_state(self.state_path)
        block = restored["blocks"][0]
        self.assertEqual(block["runs"], 8)
        self.assertEqual(block["completed_base"], 3)
        self.assertEqual(self.runner.calls[0][2]["scheduled_raid_runs"], 5)
        self.assertFalse(restored["enabled"])
        self.assertEqual(json.loads(self.state_path.with_suffix('.json.bak').read_text(encoding='utf-8')), old)
        with patch("panel.log_store.get_store") as store:
            store.return_value.raid_progress_messages.return_value = ["[RAID] 第 1 圈结束", "[RAID] ===== 第 2/5 圈 ====="]
            self.assertEqual(dc.raid_recovery({**block, "status": "interrupted", "reason": "执行状态不明，后续已停用"})["completed"], 4)

    def test_changed_settings_and_expired_day_do_not_launch(self):
        self.interrupted()
        with patch.object(dc, "raid_recovery", return_value={"completed": 2, "remaining": 6, "uncertain_round": False}):
            with patch.object(dc.time, "time", return_value=DAY + 29 * 3600):
                with self.assertRaisesRegex(ValueError, "已结束"):
                    dc.resume_raid("old-run", False, self.runner, lambda: timeline(), lambda: {}, "config.json", lambda *a: None, self.state_path, self.plan_path)
            with patch.object(dc.time, "time", return_value=DAY + 610 * 60):
                with self.assertRaisesRegex(ValueError, "设置已变化"):
                    dc.resume_raid("old-run", False, self.runner, lambda: timeline(), lambda: {"auto_refill": True}, "config.json", lambda *a: None, self.state_path, self.plan_path)
        self.assertEqual(self.runner.calls, [])


    def test_progress_query_is_not_truncated_by_battle_logs(self):
        from panel.log_store import LogStore
        store = LogStore(Path(self.folder.name) / "logs.db")
        conn = store._get_conn()
        self.addCleanup(conn.close)
        conn.executemany("INSERT INTO logs(ts,run_id,script,message) VALUES(?,?,?,?)",
                         [(1, "old-run", "workflow", "battle") for _ in range(6000)])
        store.append("old-run", "workflow", "[RAID] 第 7 圈结束")
        store.append("old-run", "workflow", "[RAID] ===== 第 8/26 圈 =====")
        block = self.interrupted()["blocks"][0]
        with patch("panel.log_store.get_store", return_value=store):
            self.assertEqual(dc.raid_recovery(block)["completed"], 7)

    def test_resume_endpoint_rejects_ledger_and_malformed_confirmation(self):
        client = TestClient(server.app)
        with patch.object(server, "get_runner", return_value=self.runner):
            with patch.object(server, "_ledger_mode", return_value=True):
                self.assertEqual(client.post("/api/day-conductor/resume-raid", json={}).status_code, 403)
            with patch.object(server, "_ledger_mode", return_value=False):
                self.assertEqual(client.post("/api/day-conductor/resume-raid", json={"run_id": "old-run", "finished_round": "yes"}).status_code, 400)
                with patch.object(dc, "resume_raid", side_effect=ValueError("还有任务正在执行")):
                    self.assertEqual(client.post("/api/day-conductor/resume-raid", json={"run_id": "old-run", "finished_round": False}).status_code, 409)


    def test_busy_runner_does_not_change_saved_state(self):
        old = self.interrupted()
        self.runner.is_running = True
        with self.assertRaisesRegex(ValueError, "还有任务"):
            dc.resume_raid("old-run", False, self.runner, lambda: timeline(), lambda: {}, "config.json", lambda *a: None, self.state_path, self.plan_path)
        self.assertEqual(dc.load_state(self.state_path), old)
        self.assertEqual(self.runner.calls, [])

    def test_last_uncertain_round_confirmed_finished_does_not_spawn(self):
        self.interrupted()
        with patch.object(dc, "raid_recovery", return_value={"completed": 7, "remaining": 1, "uncertain_round": True}), patch.object(dc.time, "time", return_value=DAY + 610 * 60):
            dc.resume_raid("old-run", True, self.runner, lambda: timeline(), lambda: {}, "config.json", lambda *a: None, self.state_path, self.plan_path)
        self.assertEqual(dc.load_state(self.state_path)["blocks"][0]["status"], "ended")
        self.assertEqual(self.runner.calls, [])
