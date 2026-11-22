"""凌晨日课自动预排：04:00 换日后自动把「一键日课」排进今日时段表。"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import daily_autoschedule as das
from panel import day_conductor as dc
from panel import day_plan as dp
from panel import day_timeline as dt
from panel import expedition_choices as ec
from panel import server  # noqa: F401 - register gameplay workflow nodes
from tests.test_day_conductor import FakeRunner


def _ts(hour, minute):
    return datetime(2026, 11, 18, hour, minute).timestamp()


NOW = _ts(4, 30)  # 换日后半小时
DAY_START = dt._day_window(NOW)[0]
DAY_END = DAY_START + dt.DAY_MINUTES * 60

DAILY_PRESET = {"id": "builtin-daily", "name": "一键日课", "after": "none",
                "daily_mode": True,
                "nodes": [{"type": "signin", "params": {}}],
                "daily_ui": {"rows": [], "startTime": "03:20",
                             "autoSchedule": True}}


def timeline(*, activity=False, now=NOW):
    card = ({"name": "联队战", "seconds_per_loop": 420, "remaining_runs": 36,
             "event_end_at": DAY_START + 24 * 3600, "occupied": []}
            if activity else None)
    return {"day_start": DAY_START, "now": now, "expeditions": [],
            "activity": card}


class DailyAutoscheduleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state_path = root / "autoschedule.json"
        self.plan_path = root / "plan.json"
        self.conductor_path = root / "conductor.json"
        self.messages = []
        self.timeline = timeline()
        self.team_patch = patch.object(dc, "_for_team",
                                       side_effect=lambda tl, team: tl)
        self.team_patch.start()
        self.addCleanup(self.team_patch.stop)

    def schedule(self, now=NOW, preset=DAILY_PRESET, touched=False):
        return das.maybe_schedule(
            now, emit=lambda script, msg: self.messages.append(msg),
            timeline_fn=lambda: self.timeline,
            raid_settings_fn=lambda: {"team_no": "3"},
            preset_fn=lambda _id: preset,
            touched_fn=lambda day_start, day_end, name: touched,
            state_path=self.state_path, plan_path=self.plan_path,
            conductor_path=self.conductor_path)

    def test_creates_block_after_rollover_with_dawn_time(self):
        # 换日后无 plan：自动建块；窗口原点 00:00，03:20 排在窗口尾部
        # (3+24)*60+20 = 1640。
        self.assertEqual(self.schedule(), "armed")
        plan = dp.load_plan(self.plan_path)
        self.assertEqual(plan["blocks"], [{"start_min": 1640, "kind": "workflow",
                                           "workflow_id": "builtin-daily"}])
        state = dc.load_state(self.conductor_path)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["blocks"][0]["status"], "pending")
        self.assertTrue(any("日课预排" in m for m in self.messages))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["armed"])
        self.assertEqual(saved["day_start"], DAY_START)
        # 再来一次：块已在，不重复排
        self.assertEqual(self.schedule(), "present")
        self.assertEqual(len(dp.load_plan(self.plan_path)["blocks"]), 1)

    def test_past_start_time_reschedules_at_now(self):
        # startTime 04:10 已过（now 04:30）：补排用 now，口径同「过点块尽快开工」。
        preset = {**DAILY_PRESET,
                  "daily_ui": {"rows": [], "startTime": "04:10",
                               "autoSchedule": True}}
        self.assertEqual(self.schedule(preset=preset), "armed")
        block = dp.load_plan(self.plan_path)["blocks"][0]
        self.assertEqual(block["start_min"], 270)  # now 04:30 = 窗口第 270 分钟

    def test_stop_today_prevents_reschedule(self):
        dc._save({"version": 2, "enabled": False, "day_start": DAY_START,
                  "cancelled_at": NOW, "plan_id": "x", "plan_signature": "y",
                  "blocks": []}, self.conductor_path)
        self.assertEqual(self.schedule(), "cancelled")
        self.assertFalse(self.plan_path.exists())

    def test_deleted_block_is_dismissed_for_the_day(self):
        self.assertEqual(self.schedule(), "armed")
        self.plan_path.unlink()  # 她把预排的块删了
        self.assertEqual(self.schedule(), "dismissed")
        self.assertFalse(self.plan_path.exists())
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["dismissed"])
        self.assertEqual(self.schedule(), "dismissed")
        self.assertFalse(self.plan_path.exists())
        self.assertEqual(sum("删掉了" in m for m in self.messages), 1)

    def test_touched_daily_is_not_rescheduled(self):
        self.assertEqual(self.schedule(touched=True), "touched")
        self.assertFalse(self.plan_path.exists())

    def test_daily_touched_recognizes_workflow_form_at_any_status(self):
        # 手动从工作流页/统一今日安排跑的 builtin-daily 记为 script=="workflow"、
        # label=开工时预设名；running/completed/stopped 任一状态都罢手。
        for status in ("running", "completed", "stopped"):
            with self.subTest(status=status):
                for path in (self.plan_path, self.state_path,
                             self.conductor_path):
                    if path.exists():
                        path.unlink()
                row = {"script": "workflow", "label": "一键日课",
                       "status": status, "started_at": DAY_START + 100,
                       "ended_at": DAY_START + 200}
                with patch("touken.telemetry.get_telemetry_store") as store:
                    store.return_value.runs_between.return_value = [row]
                    result = das.maybe_schedule(
                        NOW, emit=lambda s, m: self.messages.append(m),
                        timeline_fn=lambda: self.timeline,
                        raid_settings_fn=lambda: {"team_no": "3"},
                        preset_fn=lambda _id: DAILY_PRESET,
                        state_path=self.state_path, plan_path=self.plan_path,
                        conductor_path=self.conductor_path)
                self.assertEqual(result, "touched")
                self.assertFalse(self.plan_path.exists())

    def test_daily_touched_ignores_other_workflows_and_yesterday(self):
        rows = [{"script": "workflow", "label": "杂物工作流",
                 "status": "completed", "started_at": DAY_START + 100},
                {"script": "daily", "status": "completed",
                 "started_at": DAY_START - 100}]
        with patch("touken.telemetry.get_telemetry_store") as store:
            store.return_value.runs_between.return_value = rows
            self.assertFalse(das._daily_touched(DAY_START, DAY_END, "一键日课"))
        # 她给日课改了名字：按当前预设名也认得出来
        with patch("touken.telemetry.get_telemetry_store") as store:
            store.return_value.runs_between.return_value = [
                {"script": "workflow", "label": "早课", "status": "failed",
                 "started_at": DAY_START + 100}]
            self.assertTrue(das._daily_touched(DAY_START, DAY_END, "早课"))

    def test_switch_off_by_default_and_invalid_time(self):
        no_switch = {**DAILY_PRESET,
                     "daily_ui": {"rows": [], "startTime": "03:20"}}
        self.assertEqual(self.schedule(preset=no_switch), "off")
        no_time = {**DAILY_PRESET,
                   "daily_ui": {"rows": [], "startTime": "",
                                "autoSchedule": True}}
        self.assertEqual(self.schedule(preset=no_time), "off")
        bad_time = {**DAILY_PRESET,
                    "daily_ui": {"rows": [], "startTime": "25:99",
                                 "autoSchedule": True}}
        self.assertEqual(self.schedule(preset=bad_time), "off")
        self.assertFalse(self.plan_path.exists())

    def test_appends_to_existing_plan_and_waits_for_raids(self):
        # 已有 plan 缺日课块：追加且含 raid 块时 after_raids=true，
        # 并贯穿 plan 文件与大总管状态（load_plan 规整不丢字段、签名不漂）。
        self.timeline = timeline(activity=True)
        dp.save_plan(DAY_START, DAY_START + 24 * 3600,
                     [{"start_min": 600, "kind": "raid", "runs": 4}],
                     self.plan_path)
        self.assertEqual(self.schedule(), "armed")
        plan = dp.load_plan(self.plan_path)
        self.assertEqual([b["kind"] for b in plan["blocks"]], ["raid", "workflow"])
        self.assertTrue(plan["blocks"][1]["after_raids"])
        state = dc.load_state(self.conductor_path)
        self.assertTrue(state["blocks"][1]["after_raids"])

        runner = FakeRunner()
        silence = lambda *args: None
        # 联队战到点先开工。
        dc.tick(DAY_START + 600 * 60, runner, lambda: self.timeline,
                lambda: {"team_no": "3"}, "config.json", silence,
                self.conductor_path, self.plan_path)
        self.assertEqual(runner.calls[0][0], "workflow")
        self.assertIn("scheduled_raid_runs", runner.calls[0][2])
        # raid 块还在跑（任务流 parked）且已到日课点：after_raids 不抢跑。
        runner.is_running = False
        with patch("panel.workflow_waits.resume_due"), patch(
                "panel.workflow_waits.load",
                return_value={"run-1": {"status": "waiting"}}):
            dc.tick(DAY_START + 1640 * 60, runner, lambda: self.timeline,
                    lambda: {"team_no": "3"}, "config.json", silence,
                    self.conductor_path, self.plan_path)
        self.assertEqual(len(runner.calls), 1)
        # raid 收工：日课立即接班（实际完成优先于时钟）。
        with patch("panel.workflow_waits.resume_due"), patch(
                "panel.workflow_waits.load", return_value={}):
            dc.tick(DAY_START + 1640 * 60 + 5, runner, lambda: self.timeline,
                    lambda: {"team_no": "3"}, "config.json", silence,
                    self.conductor_path, self.plan_path)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[1][2]["workflow_id"], "builtin-daily")
        self.assertNotIn("scheduled_raid_runs", runner.calls[1][2])

    def test_after_raids_daily_fires_when_raid_blocked(self):
        # 联队战开不了工（blocked）不是「还在跑」：日课不陪绑死等。
        self.timeline = timeline(activity=True)
        dp.save_plan(DAY_START, DAY_START + 24 * 3600,
                     [{"start_min": 600, "kind": "raid", "runs": 4}],
                     self.plan_path)
        self.assertEqual(self.schedule(), "armed")
        state = dc.load_state(self.conductor_path)
        state["blocks"][0].update(status="blocked", reason="会撞上10:00 部队三派遣")
        dc._save(state, self.conductor_path)
        runner = FakeRunner()
        with patch("panel.workflow_waits.resume_due"), patch(
                "panel.workflow_waits.load", return_value={}):
            dc.tick(DAY_START + 1640 * 60, runner, lambda: self.timeline,
                    lambda: {"team_no": "3"}, "config.json",
                    lambda *args: None, self.conductor_path, self.plan_path)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][2]["workflow_id"], "builtin-daily")

    def test_existing_daily_block_is_left_alone(self):
        # 她自己排了日课块：不动她的时间。
        dp.save_plan(DAY_START, None,
                     [{"start_min": 1300, "kind": "workflow",
                       "workflow_id": "builtin-daily"}], self.plan_path)
        before = self.plan_path.read_bytes()
        self.assertEqual(self.schedule(), "present")
        self.assertEqual(self.plan_path.read_bytes(), before)
        self.assertFalse(self.conductor_path.exists())

    def test_permanent_review_failure_gives_up_until_inputs_change(self):
        # 跟她手排的 raid 块撞审核（没有联队战进度）：校验类失败当天不再
        # 反复 arm/写盘，同一错误只报一次；进度同步后（输入变了）自动恢复。
        self.timeline = timeline(activity=False)
        dp.save_plan(DAY_START, DAY_START + 24 * 3600,
                     [{"start_min": 600, "kind": "raid", "runs": 4}],
                     self.plan_path)
        before = self.plan_path.read_bytes()
        self.assertEqual(self.schedule(), "gave_up")
        self.assertEqual(self.plan_path.read_bytes(), before)
        self.assertFalse(self.conductor_path.exists())
        self.assertEqual(sum("日课预排" in m for m in self.messages), 1)
        self.assertEqual(self.schedule(), "gave_up")
        self.assertEqual(sum("日课预排" in m for m in self.messages), 1)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["armed"])
        self.assertTrue(saved["give_up"])
        self.timeline = timeline(activity=True)
        self.assertEqual(self.schedule(), "armed")
        self.assertEqual([b["kind"] for b in dp.load_plan(self.plan_path)["blocks"]],
                         ["raid", "workflow"])

    def test_transient_arm_failure_retries_next_minute(self):
        # 昨天的尸体块还在 running：arm 竞争类失败 → 下分钟再试，只报一次。
        dc._save({"version": 2, "enabled": True, "day_start": DAY_START - 86400,
                  "plan_id": "x", "plan_signature": "y",
                  "blocks": [{"start_min": 10, "kind": "raid", "runs": 4,
                              "label": "联队战 4 圈", "status": "running",
                              "run_id": "old", "workflow_signature": "sig"}]},
                 self.conductor_path)
        self.assertEqual(self.schedule(), "error")
        self.assertEqual(self.schedule(), "error")
        self.assertEqual(sum("下分钟再试" in m for m in self.messages), 1)
        self.assertFalse(self.plan_path.exists())

    def test_busy_block_skips_this_tick(self):
        dc._save({"version": 2, "enabled": True, "day_start": DAY_START,
                  "plan_id": "x", "plan_signature": "y",
                  "blocks": [{"start_min": 10, "kind": "raid", "runs": 4,
                              "label": "联队战 4 圈", "status": "running",
                              "run_id": "run-1", "workflow_signature": "sig"}]},
                 self.conductor_path)
        self.assertEqual(self.schedule(), "busy")
        self.assertFalse(self.plan_path.exists())

    def test_no_reschedule_inside_daily_reset_window(self):
        # 03:50–04:10 是游戏换日特殊窗口：此刻补排等于让日课骑着换日点跑。
        reset_now = _ts(3, 55)
        self.assertEqual(self.schedule(now=reset_now), "reset_window")
        self.assertFalse(self.plan_path.exists())
        self.assertEqual(sum("刷新窗口" in m for m in self.messages), 1)
        self.assertEqual(self.schedule(now=reset_now), "reset_window")
        self.assertEqual(sum("刷新窗口" in m for m in self.messages), 1)

    def test_corrupted_state_rebuilds_stop_evidence(self):
        # 状态文件损坏：备份后按「conductor state → run 记录 → plan 现状」
        # 重建罢手证据——conductor 同日已有 ended 的日课块，当天不补。
        self.state_path.write_text("{损坏", encoding="utf-8")
        dc._save({"version": 2, "enabled": True, "day_start": DAY_START,
                  "plan_id": "x", "plan_signature": "y",
                  "blocks": [{"start_min": 1640, "kind": "workflow",
                              "workflow_id": "builtin-daily", "label": "一键日课",
                              "status": "ended", "run_id": "r1",
                              "workflow_signature": "sig"}]},
                 self.conductor_path)
        self.assertEqual(self.schedule(), "recovered")
        self.assertFalse(self.plan_path.exists())
        self.assertTrue(list(self.state_path.parent.glob("autoschedule.json.bad-*")))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["dismissed"])
        self.assertEqual(self.schedule(), "dismissed")
        self.assertFalse(self.plan_path.exists())

    def test_corrupted_state_with_touched_run_does_not_reschedule(self):
        self.state_path.write_text("not json", encoding="utf-8")
        self.assertEqual(self.schedule(touched=True), "touched")
        self.assertFalse(self.plan_path.exists())

    def test_holds_conductor_lock_across_save_and_arm(self):
        owned = []
        real_arm, real_save = dc.arm, dp.save_plan

        def spy_arm(plan, timeline, workflow_id, raid_settings, *args, **kwargs):
            owned.append(dc._LOCK._is_owned())
            return real_arm(plan, timeline, workflow_id, raid_settings,
                            self.conductor_path, **kwargs)

        def spy_save(day_start, event_end_at, blocks, *args, **kwargs):
            owned.append(dc._LOCK._is_owned())
            return real_save(day_start, event_end_at, blocks, self.plan_path)

        with patch.object(dc, "arm", side_effect=spy_arm), \
                patch.object(dp, "save_plan", side_effect=spy_save):
            self.assertEqual(self.schedule(), "armed")
        self.assertTrue(owned)
        self.assertTrue(all(owned))


class EndpointLockTests(unittest.TestCase):
    """保存/开启通道与自动预排线程同锁：save 与 arm 之间不留窗口。"""

    def setUp(self):
        self.canned = {"day_start": DAY_START, "now": NOW, "activity": None,
                       "conductor": {"enabled": True, "blocks": []},
                       "booking": {"version": 2, "day_start": DAY_START,
                                   "event_end_at": None, "blocks": []}}
        self.team_patch = patch.object(dc, "_for_team",
                                       side_effect=lambda tl, team: tl)
        self.team_patch.start()
        self.addCleanup(self.team_patch.stop)
        self.owned = []

    def test_schedule_endpoint_holds_lock_across_save_and_arm(self):
        def spy_arm(*args, **kwargs):
            self.owned.append(dc._LOCK._is_owned())
            return {}

        def spy_save(day_start, event_end_at, blocks, *args, **kwargs):
            self.owned.append(dc._LOCK._is_owned())
            return {"day_start": day_start, "event_end_at": event_end_at,
                    "blocks": blocks}

        client = TestClient(server.app)
        with patch.object(server, "_day_timeline_payload",
                          return_value=self.canned), \
                patch.object(server, "_load_panel_settings",
                             return_value={"params": {"raid": {}}}), \
                patch.object(dc, "arm", side_effect=spy_arm), \
                patch.object(dp, "save_plan", side_effect=spy_save):
            response = client.put("/api/day-timeline/schedule", json={
                "blocks": [{"start_min": 600, "kind": "daily"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.owned), 3)  # arm(预检) + save_plan + arm
        self.assertTrue(all(self.owned))

    def test_day_conductor_enable_holds_lock_across_load_and_arm(self):
        plan = {"version": 2, "day_start": DAY_START, "event_end_at": None,
                "blocks": [{"start_min": 600, "kind": "daily"}]}

        def spy_load(*args, **kwargs):
            self.owned.append(dc._LOCK._is_owned())
            return plan

        def spy_arm(*args, **kwargs):
            self.owned.append(dc._LOCK._is_owned())
            return {}

        client = TestClient(server.app)
        with patch.object(server, "_day_timeline_payload",
                          return_value=self.canned), \
                patch.object(server, "_load_panel_settings",
                             return_value={"params": {"raid": {}}}), \
                patch.object(dp, "load_plan", side_effect=spy_load), \
                patch.object(dc, "arm", side_effect=spy_arm):
            response = client.put("/api/day-conductor", json={
                "enabled": True, "workflow_id": "builtin-scheduled-raid"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.owned), 2)  # load_plan + arm
        self.assertTrue(all(self.owned))


class AutoExecutePlanTests(unittest.TestCase):
    """autoExecutePlan 开关：到点不只预排日课块，直接收编整份今日安排。

    收编本体必须是 today_execution.execute_today 本尊（与按
    「一键执行今日安排」按钮同一条逻辑），这里只验收口与闸门。
    """

    EXEC_PRESET = {"id": "builtin-daily", "name": "一键日课", "after": "none",
                   "daily_mode": True,
                   "nodes": [{"type": "signin", "params": {}}],
                   "daily_ui": {"rows": [], "startTime": "03:20",
                                "autoSchedule": True, "autoExecutePlan": True}}

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state_path = root / "autoschedule.json"
        self.plan_path = root / "plan.json"
        self.conductor_path = root / "conductor.json"
        self.choices_path = root / "choices.json"
        self.messages = []
        self.timeline = timeline()
        self.runner = FakeRunner()
        self.executes = []
        self.team_patch = patch.object(dc, "_for_team",
                                       side_effect=lambda tl, team: tl)
        self.team_patch.start()
        self.addCleanup(self.team_patch.stop)
        self.cfg_patch = patch("panel.scheduler.load_config",
                               return_value={"automation": {}, "state": {}})
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)
        self.find_patch = patch.object(dc.workflow, "find_preset",
                                       return_value=self.EXEC_PRESET)
        self.find_patch.start()
        self.addCleanup(self.find_patch.stop)

    def schedule(self, now=NOW, preset=None, touched=False, execute_fn=None):
        return das.maybe_schedule(
            now, emit=lambda script, msg: self.messages.append(msg),
            timeline_fn=lambda: self.timeline,
            raid_settings_fn=lambda: {"team_no": "3"},
            preset_fn=lambda _id: preset if preset is not None else self.EXEC_PRESET,
            touched_fn=lambda day_start, day_end, name: touched,
            runner=self.runner, execute_fn=execute_fn,
            choices_path=self.choices_path,
            state_path=self.state_path, plan_path=self.plan_path,
            conductor_path=self.conductor_path)

    def _spy(self, error=None):
        def _execute(runner, timeline_fn, raid_settings_fn, **kwargs):
            self.executes.append({"runner": runner, "kwargs": kwargs})
            if error is not None:
                raise ValueError(error)
            return {"ok": True, "message": "今日安排已接班，将按设置的开始时间执行。"}
        return _execute

    def _clear_day_files(self):
        for path in (self.plan_path, self.state_path, self.conductor_path,
                     self.choices_path):
            path.unlink(missing_ok=True)

    def test_switch_off_keeps_today_behavior(self):
        # 开关缺省/显式 false：维持现状——只预排日课块，收编不被调用
        variants = ({"rows": [], "startTime": "03:20", "autoSchedule": True},
                    {"rows": [], "startTime": "03:20",
                     "autoSchedule": True, "autoExecutePlan": False})
        for ui in variants:
            with self.subTest(ui=ui):
                spy = self._spy()
                self.assertEqual(self.schedule(preset={**self.EXEC_PRESET,
                                                       "daily_ui": ui},
                                               execute_fn=spy), "armed")
                self.assertEqual(self.executes, [])
                self.assertEqual(dp.load_plan(self.plan_path)["blocks"],
                                 [{"start_min": 1640, "kind": "workflow",
                                   "workflow_id": "builtin-daily"}])
                self._clear_day_files()

    def test_inert_when_autoschedule_off(self):
        # autoSchedule 关着（含没填/非法开始时间）：autoExecutePlan 不生效
        variants = ({"rows": [], "autoExecutePlan": True},
                    {"rows": [], "autoSchedule": True, "autoExecutePlan": True},
                    {"rows": [], "autoSchedule": True, "startTime": "25:99",
                     "autoExecutePlan": True})
        for ui in variants:
            with self.subTest(ui=ui):
                spy = self._spy()
                self.assertEqual(self.schedule(preset={**self.EXEC_PRESET,
                                                       "daily_ui": ui},
                                               execute_fn=spy), "off")
                self.assertEqual(self.executes, [])
                self.assertFalse(self.plan_path.exists())

    def test_execute_branch_calls_today_execution_equivalent(self):
        spy = self._spy()
        self.assertEqual(self.schedule(execute_fn=spy), "armed")
        self.assertEqual(len(self.executes), 1)
        call = self.executes[0]
        self.assertIs(call["runner"], self.runner)
        self.assertEqual(call["kwargs"]["daily_preset"], self.EXEC_PRESET)
        self.assertEqual(call["kwargs"]["state_path"], self.conductor_path)
        self.assertEqual(call["kwargs"]["plan_path"], self.plan_path)
        self.assertEqual(call["kwargs"]["choices_path"], self.choices_path)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["armed"])
        self.assertTrue(any("收编" in m for m in self.messages))

    def test_execute_branch_matches_button_end_to_end(self):
        # 收编等价物（本尊）跑通：plan/conductor 状态与按钮按下的产物一致
        self.assertEqual(self.schedule(), "armed")
        plan = dp.load_plan(self.plan_path)
        self.assertEqual(plan["blocks"], [{"kind": "workflow",
                                           "workflow_id": "builtin-daily",
                                           "start_min": 1640}])
        state = dc.load_state(self.conductor_path)
        self.assertTrue(state["enabled"])
        self.assertTrue(state["today_execution"])
        self.assertTrue(state["unified_today"])
        self.assertEqual(state["blocks"][0]["kind"], "workflow")
        self.assertEqual(state["blocks"][0]["status"], "pending")
        # 第二分钟：块已在安排里，不重复收编
        self.assertEqual(self.schedule(), "present")
        self.assertEqual(self.executes, [])

    def test_execute_branch_adopts_expedition_suggestions(self):
        self.timeline["expedition_suggestions"] = [
            {"team_no": 4, "map_code": "B2", "start_min": 600,
             "duration_min": 180}]
        self.assertEqual(self.schedule(), "armed")
        forced = ec.load_choice_sets(self.choices_path)[1]
        self.assertEqual(len(forced), 1)
        record = next(iter(forced.values()))
        self.assertEqual(record["map_code"], "B2")
        self.assertEqual(record["plan_id"],
                         dc.load_state(self.conductor_path)["plan_id"])

    def test_cancelled_gate_still_blocks_adoption(self):
        dc._save({"version": 2, "enabled": False, "day_start": DAY_START,
                  "cancelled_at": NOW, "plan_id": "x", "plan_signature": "y",
                  "blocks": []}, self.conductor_path)
        spy = self._spy()
        self.assertEqual(self.schedule(execute_fn=spy), "cancelled")
        self.assertEqual(self.executes, [])
        self.assertFalse(self.plan_path.exists())

    def test_touched_gate_still_blocks_adoption(self):
        spy = self._spy()
        self.assertEqual(self.schedule(touched=True, execute_fn=spy), "touched")
        self.assertEqual(self.executes, [])
        self.assertFalse(self.plan_path.exists())

    def test_deleted_block_dismisses_adoption_for_the_day(self):
        counting = self._spy()
        real = das._execute_today

        def wrapped(runner, timeline_fn, raid_settings_fn, **kwargs):
            self.executes.append({"runner": runner, "kwargs": kwargs})
            return real(runner, timeline_fn, raid_settings_fn, **kwargs)

        self.assertEqual(self.schedule(execute_fn=wrapped), "armed")
        self.assertEqual(len(self.executes), 1)
        self.plan_path.unlink()  # 她把收编进来的日课块删了
        self.assertEqual(self.schedule(execute_fn=wrapped), "dismissed")
        self.assertEqual(len(self.executes), 1)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["dismissed"])

    def test_corrupted_state_rebuilds_stop_evidence(self):
        self.state_path.write_text("{损坏", encoding="utf-8")
        dc._save({"version": 2, "enabled": True, "day_start": DAY_START,
                  "plan_id": "x", "plan_signature": "y",
                  "blocks": [{"start_min": 1640, "kind": "workflow",
                              "workflow_id": "builtin-daily", "label": "一键日课",
                              "status": "ended", "run_id": "r1",
                              "workflow_signature": "sig"}]},
                 self.conductor_path)
        spy = self._spy()
        self.assertEqual(self.schedule(execute_fn=spy), "recovered")
        self.assertEqual(self.executes, [])
        self.assertFalse(self.plan_path.exists())

    def test_busy_runner_waits_and_retries_next_minute(self):
        spy = self._spy()
        self.runner.is_running = True
        self.assertEqual(self.schedule(execute_fn=spy), "busy")
        self.assertEqual(self.executes, [])
        self.assertEqual(self.messages, [])
        self.runner.is_running = False
        self.assertEqual(self.schedule(execute_fn=spy), "armed")
        self.assertEqual(len(self.executes), 1)

    def test_running_conductor_block_waits(self):
        dc._save({"version": 2, "enabled": True, "day_start": DAY_START,
                  "plan_id": "x", "plan_signature": "y",
                  "blocks": [{"start_min": 10, "kind": "raid", "runs": 4,
                              "label": "联队战 4 圈", "status": "running",
                              "run_id": "run-1", "workflow_signature": "sig"}]},
                 self.conductor_path)
        spy = self._spy()
        self.assertEqual(self.schedule(execute_fn=spy), "busy")
        self.assertEqual(self.executes, [])
        self.assertFalse(self.plan_path.exists())

    def test_adoption_validation_failure_gives_up_once_and_recovers(self):
        spy = self._spy(error="今天已经收摊了，刷新后再安排")
        self.assertEqual(self.schedule(execute_fn=spy), "gave_up")
        self.assertEqual(self.schedule(execute_fn=spy), "gave_up")
        self.assertEqual(sum("没接成" in m for m in self.messages), 1)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["armed"])
        self.assertTrue(saved["give_up"])
        # 情况变化（重审通过）自动接上
        self.assertEqual(self.schedule(execute_fn=self._spy()), "armed")
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["armed"])
        self.assertIsNone(saved["give_up"])

    def test_reset_window_blocks_adoption(self):
        reset_now = _ts(3, 55)
        spy = self._spy()
        self.assertEqual(self.schedule(now=reset_now, execute_fn=spy),
                         "reset_window")
        self.assertEqual(self.executes, [])
        self.assertFalse(self.plan_path.exists())
        self.assertEqual(sum("刷新窗口" in m for m in self.messages), 1)


if __name__ == "__main__":
    unittest.main()
