import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from panel import scheduler as s


class CoordinationTests(unittest.TestCase):
    def config(self):
        cfg = s._defaults()
        cfg["automation"]["enabled"] = True
        return cfg

    def job(self, key="first"):
        return dict(key=key, team_no=2, map_code="B1", late_min=0)

    def test_busy_job_survives_grace_window(self):
        queue = s.DeferredDispatches()
        cfg = self.config()
        queue.update(cfg, [self.job()], 100)
        jobs = queue.update(cfg, [], 4000)
        self.assertEqual(jobs[0]["key"], "first")
        self.assertEqual(jobs[0]["observed_at"], 100)

    def test_preset_keeps_first_missed_departure(self):
        queue = s.DeferredDispatches()
        cfg = self.config()
        queue.update(cfg, [self.job()], 100)
        self.assertEqual(queue.update(cfg, [self.job("next")], 1000)[0]["key"], "first")

    def test_edit_invalidates_held_jobs(self):
        queue = s.DeferredDispatches()
        cfg = self.config()
        queue.update(cfg, [self.job()], 100)
        cfg["automation"]["teams"] = [3, 4, 5]
        self.assertEqual(queue.update(cfg, [], 200), [])

    def test_custom_uses_latest_not_backlog(self):
        cfg = self.config()
        cfg["automation"].update(mode="custom", capitalist=True)
        cfg["entries"] = [dict(time="08:00", team_no=2, map_code="B1"),
                          dict(time="09:00", team_no=2, map_code="B2")]
        jobs = s._custom_due(cfg, 600, "2026-09-05")
        self.assertEqual([j["map_code"] for j in jobs], ["B2"])
        cfg["automation"]["last_runs"][jobs[0]["key"]] = "done"
        self.assertEqual(s._custom_due(cfg, 600, "2026-09-05"), [])

    def test_old_countdown_does_not_block_next_dispatch(self):
        now = time.time()
        records = {"2": dict(map_code="C1", dispatched_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), duration_min=30)}
        cfg = self.config()
        due = [self.job()]
        s.tick(cfg, due, now, runner_busy=False, emulator_ok=True, records=records)
        out = s.tick(cfg, due, now + 16, runner_busy=False, emulator_ok=True, records=records)
        self.assertEqual(out["start"]["key"], "first")

    def test_disabled_scheduler_releases_teams(self):
        cfg = self.config()
        self.assertEqual(s.managed_teams(cfg), {2, 3, 4})
        cfg["automation"]["enabled"] = False
        self.assertEqual(s.managed_teams(cfg), set())

    def test_closed_game_never_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(dict(adb_path="adb", adb_address="device", daily=dict(logout=dict(package="game.pkg")))))
            with patch("touken.emulator._run", return_value=Mock(returncode=1, stdout="")) as run:
                self.assertFalse(s._emulator_ready(str(path)))
                self.assertEqual(run.call_args.args[0][-2:], ["pidof", "game.pkg"])

    def test_common_plan_does_not_redispatch_owned_team(self):
        from panel.server import _build_expedition_manager
        cfg = self.config()
        cfg["common_plan"] = [dict(team_no=2, map_code="B1", enabled=True)]
        agent = Mock()
        agent.collect_expedition_stream.return_value = iter(["collected"])
        with patch.object(s, "load_config", return_value=cfg):
            messages = list(_build_expedition_manager(agent, "unused", {}))
        agent.collect_expedition_stream.assert_called_once_with(redispatch=None)
        agent.expedition_stream.assert_not_called()
        self.assertIn("collected", messages)

    def test_dispatch_failure_not_marked_done(self):
        cfg = self.config()
        job = {**self.job(), "shift_key": "lane", "observed_at": 100}
        records = {"2": dict(map_code="B1", dispatched_at="old")}
        self.assertFalse(s.record_completed_dispatch(cfg, job, "old", records, 700))
        self.assertEqual(cfg["automation"]["last_runs"], {})
        self.assertEqual(cfg["automation"]["lane_shifts"], {})

    def test_verified_departure_shifts_following_lane(self):
        cfg = self.config()
        cfg["automation"]["lane_shifts"]["lane"] = 5
        job = {**self.job(), "shift_key": "lane", "observed_at": 100}
        records = {"2": dict(map_code="B1", dispatched_at="new")}
        self.assertTrue(s.record_completed_dispatch(cfg, job, "old", records, 700))
        self.assertEqual(cfg["automation"]["lane_shifts"]["lane"], 15)
        self.assertEqual(cfg["automation"]["last_runs"]["first"], "new")

    def test_forced_bypasses_skip_in_custom_due(self):
        cfg = self.config()
        cfg["automation"].update(mode="custom")
        cfg["entries"] = [dict(time="08:00", team_no=2, map_code="B1")]
        key = "2026-09-05:custom:0:08:00"
        skipped = {key: {"team_no": 2, "map_code": "B1", "planned_at": 1}}
        self.assertEqual(s._custom_due(cfg, 490, "2026-09-05",
                                       choices=skipped), [])
        forced = {key: {"team_no": 2, "map_code": "B1", "planned_at": 1}}
        due = s._custom_due(cfg, 490, "2026-09-05",
                            choices=skipped, forced=forced)
        self.assertEqual([j["key"] for j in due], [key])

    def test_forced_only_filters_due_when_automation_off(self):
        """总开关关着：forced_only 只留下被强制启用的班，其余照旧不跑。"""
        cfg = self.config()
        cfg["automation"].update(mode="custom", enabled=False)
        cfg["entries"] = [dict(time="07:00", team_no=2, map_code="B1"),
                          dict(time="07:20", team_no=3, map_code="B2")]
        due = s._custom_due(cfg, 450, "2026-09-05")
        self.assertEqual(len(due), 2)
        forced = {due[0]["key"]: {"team_no": 2, "map_code": "B1",
                                  "planned_at": 1}}
        kept = s.forced_only(due, forced)
        self.assertEqual([j["key"] for j in kept], [due[0]["key"]])
        # 强制班照常走状态机到 ready → start
        cfg2 = self.config()
        cfg2["automation"].update(mode="custom", enabled=False)
        cfg2["entries"] = [dict(time="07:00", team_no=2, map_code="B1")]
        kept = s.forced_only(
            s._custom_due(cfg2, 450, "2026-09-05"), forced)
        base = s._planned_ts(kept[0])
        out = s.tick(cfg2, kept, base + 10, runner_busy=False,
                     emulator_ok=True, records={})
        out = s.tick(cfg2, kept, base + 26, runner_busy=False,
                     emulator_ok=True, records={})
        self.assertIsNotNone(out["start"])

    def test_forced_unskips_in_today_projection(self):
        cfg = self.config()
        cfg["automation"].update(mode="custom")
        cfg["entries"] = [dict(time="08:00", team_no=2, map_code="B1")]
        key = "2026-09-05:custom:0:08:00"
        choices = {key: {"team_no": 2, "map_code": "B1", "planned_at": 1}}
        forced = {key: {"team_no": 2, "map_code": "B1", "planned_at": 1}}
        now = 1757133600 + 4 * 3600  # 2026-09-05 中午
        proj = s.today_projection(cfg, now=now, choices=choices, forced=forced)
        item = proj["custom"][0]
        self.assertFalse(item["skipped_today"])
        self.assertNotEqual(item["state"], "skipped")
