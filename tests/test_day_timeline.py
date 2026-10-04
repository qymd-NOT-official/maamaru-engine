# -*- coding: utf-8 -*-
"""仪表盘 24 小时时间轴（panel/day_timeline.py）测试。

全部依赖注入：cfg 手工构造、store 用临时库、labels/active 直接传，
不碰真实用户数据。
"""

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from panel import day_timeline as dtl
from panel import scheduler
from touken.telemetry import TelemetryStore


def _today_at(hour, minute=0):
    return time.mktime(time.strptime(
        f"{time.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}:00",
        "%Y-%m-%d %H:%M:%S"))


def _today_at_shanghai(hour, minute=0):
    """按上海时间造时间戳：游戏换日逻辑（_raid_active_plan）走 SHANGHAI_TZ，
    相关测试的时间戳必须同口径，否则 UTC 的 CI 上整天平移 8 小时变抽奖。"""
    now = datetime.now(dtl.SHANGHAI_TZ)
    return now.replace(hour=hour, minute=minute, second=0,
                       microsecond=0).timestamp()


_FAKE_MAPS = [
    {"code": "B3", "era": 2, "slot": 3, "name": "B3", "duration_min": 90,
     "duration_text": "1h30分"},
    {"code": "E2", "era": 5, "slot": 2, "name": "E2", "duration_min": 600,
     "duration_text": "10h00分"},
]


def _cfg(entries, *, mode="custom", enabled=True):
    return {
        "entries": entries,
        "automation": {"enabled": enabled, "mode": mode,
                       "preset": "日课三班", "teams": [2, 3, 4],
                       "start_time": "08:00"},
    }


def _forced_record(team, map_code, planned_ts, *, start_min=None,
                   duration_min=None):
    record = {"team_no": team, "map_code": map_code,
              "planned_at": planned_ts}
    if start_min is not None:
        record["start_min"] = start_min
    if duration_min is not None:
        record["duration_min"] = duration_min
    return record


class DayTimelineExpeditionTests(unittest.TestCase):
    """v2：上轴的只有 forced 班（含自描述班）和远征中/待收；
    preset/custom 死班表不再投影。"""

    def setUp(self):
        patcher = patch.object(scheduler, "map_options", lambda: _FAKE_MAPS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unforced_entry_is_not_projected(self):
        """没点名的排班条目不上轴（preset/custom 投影已撤）。"""
        now = _today_at(6, 0)
        cfg = _cfg([{"time": "08:30", "team_no": 2, "map_code": "B3",
                     "enabled": True}])
        out = dtl.build_day_timeline(now, cfg=cfg, store=None,
                                     script_labels={},
                                     expedition_forced={},
                                     expedition_records={},
                                     expedition_help={"rounds_per_team": 0,
                                                      "available_teams": []})
        self.assertEqual(out["expeditions"], [])

    def test_forced_entry_lands_on_axis(self):
        now = _today_at(6, 0)
        day_start = _today_at(0, 0)
        key = f"{time.strftime('%Y-%m-%d')}:custom:0:08:30"
        cfg = _cfg([{"time": "08:30", "team_no": 2, "map_code": "B3",
                     "enabled": True}], enabled=False)
        forced = {key: _forced_record(2, "B3", _today_at(8, 30))}
        out = dtl.build_day_timeline(now, cfg=cfg, store=None,
                                     script_labels={},
                                     expedition_forced=forced,
                                     expedition_records={},
                                     expedition_help={"rounds_per_team": 0,
                                                      "available_teams": []})
        self.assertEqual(len(out["expeditions"]), 1)
        item = out["expeditions"][0]
        self.assertEqual(item["kind"], "forced")
        self.assertEqual(item["time_min"], 8 * 60 + 30)
        self.assertEqual(item["duration_min"], 90)
        self.assertEqual(item["team_no"], 2)
        self.assertEqual(item["map_code"], "B3")
        self.assertEqual(item["state"], "pending")
        self.assertTrue(item["will_run"])
        self.assertTrue(item["forced_today"])
        self.assertTrue(item["toggleable"])

    def test_adhoc_forced_uses_its_own_duration(self):
        """自描述班时长以记录为准，不查收益表。"""
        now = _today_at(6, 0)
        day_start = _today_at(0, 0)
        key = f"{time.strftime('%Y-%m-%d')}:adhoc:1:700"
        forced = {key: _forced_record(1, "ZZ9", _today_at(11, 40),
                                      start_min=700, duration_min=45)}
        items = dtl._expedition_items(_cfg([]), now, day_start, forced=forced)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["duration_min"], 45)
        self.assertEqual(items[0]["map_code"], "ZZ9")  # 图查不到也照画

    def test_legacy_forced_unknown_map_duration_zero(self):
        now = _today_at(6, 0)
        day_start = _today_at(0, 0)
        forced = {"k": _forced_record(2, "ZZ9", _today_at(8, 30))}
        items = dtl._expedition_items(_cfg([]), now, day_start, forced=forced)
        self.assertEqual(items[0]["duration_min"], 0)

    def test_garbage_forced_records_not_crash(self):
        now = _today_at(6, 0)
        day_start = _today_at(0, 0)
        forced = {"a": None, "b": {"team_no": "x"},
                  "c": _forced_record(2, "B3", "垃圾")}
        items = dtl._expedition_items(_cfg([]), now, day_start, forced=forced)
        self.assertEqual(items, [])

    def test_yesterday_forced_not_shown(self):
        now = _today_at(6, 0)
        day_start = _today_at(0, 0)
        forced = {"old": _forced_record(2, "B3", day_start - 3600)}
        items = dtl._expedition_items(_cfg([]), now, day_start, forced=forced)
        self.assertEqual(items, [])

    def test_near_start_forced_not_toggleable(self):
        now = _today_at(8, 0)
        day_start = _today_at(0, 0)
        forced = {"k": _forced_record(2, "B3", now + 30)}
        items = dtl._expedition_items(_cfg([]), now, day_start, forced=forced)
        self.assertFalse(items[0]["toggleable"])

    def test_running_record_shows_as_running_block(self):
        now = _today_at(12, 0)
        records = {"4": {"map_code": "B3", "duration_min": 90,
                         "dispatched_at": time.strftime(
                             "%Y-%m-%d %H:%M:%S",
                             time.localtime(_today_at(11, 0)))}}
        out = dtl.build_day_timeline(now, cfg=_cfg([]), store=None,
                                     script_labels={},
                                     expedition_forced={},
                                     expedition_records=records)
        self.assertEqual(len(out["expeditions"]), 1)
        item = out["expeditions"][0]
        self.assertEqual(item["kind"], "running")
        self.assertEqual(item["state"], "running")
        self.assertEqual(item["time_min"], 660)
        self.assertEqual(item["duration_min"], 90)
        self.assertFalse(item["toggleable"])

    def test_overdue_record_shows_awaiting_collect(self):
        now = _today_at(12, 0)
        records = {"4": {"map_code": "B3", "duration_min": 90,
                         "dispatched_at": time.strftime(
                             "%Y-%m-%d %H:%M:%S",
                             time.localtime(_today_at(9, 0)))}}
        items = dtl._expedition_items(_cfg([]), now, _today_at(0, 0),
                                      records=records)
        self.assertEqual(items[0]["state"], "awaiting_collect")

    def test_dispatched_forced_hidden_behind_running(self):
        """已确认派出且队伍在外的 forced 班由「远征中」块代言，不画两遍。"""
        now = _today_at(12, 0)
        day_start = _today_at(0, 0)
        key = f"{time.strftime('%Y-%m-%d')}:adhoc:4:660"
        cfg = _cfg([])
        cfg["automation"].setdefault("slot_states", {})[key] = {
            "state": "dispatched", "blocked_reason": "",
            "dispatched_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(_today_at(11, 0)))}
        forced = {key: _forced_record(4, "B3", _today_at(11, 0),
                                      start_min=660, duration_min=90)}
        records = {"4": {"map_code": "B3", "duration_min": 90,
                         "dispatched_at": time.strftime(
                             "%Y-%m-%d %H:%M:%S",
                             time.localtime(_today_at(11, 0)))}}
        items = dtl._expedition_items(cfg, now, day_start, forced=forced,
                                      records=records)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "running")

    def test_cross_midnight_running_clamped_to_day_start(self):
        now = _today_at(0, 30)
        records = {"2": {"map_code": "B3", "duration_min": 90,
                         "dispatched_at": time.strftime(
                             "%Y-%m-%d %H:%M:%S",
                             time.localtime(_today_at(0, 0) - 1800))}}
        items = dtl._expedition_items(_cfg([]), now, _today_at(0, 0),
                                      records=records)
        self.assertEqual(items[0]["time_min"], 0)
        self.assertEqual(items[0]["duration_min"], 60)
        self.assertEqual(items[0]["state"], "running")


class DayTimelineRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TelemetryStore(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_runs_land_with_label_and_tone(self):
        day_start = _today_at(0, 0)
        self.store.start_run("r1", "daily", started_at=day_start + 3600)
        self.store.finish_run("r1", "completed", ended_at=day_start + 5400)
        self.store.start_run("r2", "sortie", started_at=day_start + 7200)
        self.store.finish_run("r2", "failed", ended_at=day_start + 7800)
        out = dtl.build_day_timeline(
            _today_at(12, 0), cfg=_cfg([]), store=self.store,
            script_labels={"daily": "日课", "sortie": "出阵"})
        daily = next(r for r in out["runs"] if r["script"] == "daily")
        sortie = next(r for r in out["runs"] if r["script"] == "sortie")
        self.assertEqual(daily["label"], "日课")
        self.assertEqual(daily["tone"], "ok")
        self.assertEqual(sortie["tone"], "failed")
        self.assertEqual(sortie["label"], "出阵")

    def test_unknown_script_falls_back_to_key(self):
        day_start = _today_at(0, 0)
        self.store.start_run("r1", "secret_thing", started_at=day_start + 60)
        self.store.finish_run("r1", "stopped", ended_at=day_start + 600)
        out = dtl.build_day_timeline(
            _today_at(12, 0), cfg=_cfg([]), store=self.store,
            script_labels={})
        self.assertEqual(out["runs"][0]["label"], "secret_thing")
        self.assertEqual(out["runs"][0]["tone"], "stopped")

    def test_workflow_uses_name_snapshot_from_run(self):
        day_start = _today_at(0, 0)
        self.store.start_run(
            "wf1", "workflow", started_at=day_start + 7200,
            label="活动+异去")
        self.store.finish_run("wf1", "completed", ended_at=day_start + 9000)
        out = dtl.build_day_timeline(
            _today_at(12, 0), cfg=_cfg([]), store=self.store,
            script_labels={"workflow": "自定义工作流"})
        self.assertEqual(out["runs"][0]["label"], "活动+异去")

    def test_active_run_forced_running(self):
        day_start = _today_at(0, 0)
        started = day_start + 7200
        self.store.start_run("r1", "dispatch", started_at=started)
        out = dtl.build_day_timeline(
            _today_at(12, 0), cfg=_cfg([]), store=self.store,
            script_labels={"dispatch": "远征"},
            active={"script": "dispatch", "started": started})
        run = out["runs"][0]
        self.assertEqual(run["tone"], "running")
        self.assertIsNone(run["ended_at"])

    def test_old_zombie_run_is_omitted_and_today_zombie_is_stopped(self):
        """往日尸体不挤进今天零点；今日尸体也不许画成「正在跑」。"""
        day_start = _today_at(0, 0)
        self.store.start_run("z1", "osaka", started_at=day_start - 3600)
        self.store.start_run("z2", "daily", started_at=day_start + 7200)
        out = dtl.build_day_timeline(
            _today_at(12, 0), cfg=_cfg([]), store=self.store,
            script_labels={}, active=None)
        tones = {r["script"]: r["tone"] for r in out["runs"]}
        self.assertNotIn("osaka", tones)
        self.assertEqual(tones["daily"], "stopped")

    def test_active_cross_midnight_run_is_kept(self):
        day_start = _today_at(0, 0)
        started = day_start - 3600
        self.store.start_run("r1", "dispatch", started_at=started)
        out = dtl.build_day_timeline(
            _today_at(1, 0), cfg=_cfg([]), store=self.store,
            script_labels={"dispatch": "远征"},
            active={"script": "dispatch", "started": started})
        self.assertEqual(len(out["runs"]), 1)
        self.assertEqual(out["runs"][0]["tone"], "running")

    def test_cross_midnight_run_included(self):
        day_start = _today_at(0, 0)
        self.store.start_run("r1", "daily", started_at=day_start - 3600)
        self.store.finish_run("r1", "completed", ended_at=day_start + 600)
        self.store.start_run("r2", "daily", started_at=day_start - 72000)
        self.store.finish_run("r2", "completed", ended_at=day_start - 70000)
        out = dtl.build_day_timeline(
            _today_at(12, 0), cfg=_cfg([]), store=self.store,
            script_labels={})
        self.assertEqual(len(out["runs"]), 1)


class DayTimelineMiscTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TelemetryStore(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_empty_inputs_not_crash(self):
        with patch.object(scheduler, "map_options", lambda: []):
            out = dtl.build_day_timeline(
                _today_at(9, 0), cfg={}, store=self.store, script_labels={},
                expedition_forced={},
                expedition_records={},
                expedition_help={"rounds_per_team": 0, "available_teams": []})
        self.assertEqual(out["expeditions"], [])
        self.assertEqual(out["runs"], [])
        self.assertIsNone(out["hint"])

    def test_daily_reset_marker(self):
        with patch.object(scheduler, "map_options", lambda: []):
            out = dtl.build_day_timeline(
                _today_at(9, 0), cfg={}, store=self.store, script_labels={})
        marker = next(m for m in out["markers"] if m["kind"] == "daily_reset")
        self.assertEqual(marker["time_min"], 1680)
        self.assertEqual(marker["label"], "日课刷新")

    def test_hint_none_when_advisor_blows_up(self):
        with patch("touken.advisor.load_event_cards",
                   side_effect=RuntimeError("boom")):
            plan = dtl._hanafuda_active_plan(_today_at(12, 0), self.store)
        self.assertIsNone(plan)
        self.assertIsNone(dtl._hanafuda_hint(plan))

    def test_hint_text_when_hanafuda_active(self):
        fake_plan = {"estimated_seconds": 5400, "seconds_to_end": 99999,
                     "tama_remaining": 300}
        with patch("touken.advisor.load_event_cards",
                   return_value={"秘宝之里": {"mechanics": "hanafuda"}}), \
             patch("touken.advisor.hanafuda_plan", return_value=fake_plan):
            plan = dtl._hanafuda_active_plan(_today_at(12, 0), self.store)
        self.assertEqual(plan, fake_plan)
        self.assertIn("秘宝之里", dtl._hanafuda_hint(plan))

    def test_hint_none_when_event_over(self):
        fake_plan = {"estimated_seconds": 5400, "seconds_to_end": 0,
                     "tama_remaining": 300}
        with patch("touken.advisor.load_event_cards",
                   return_value={"秘宝之里": {"mechanics": "hanafuda"}}), \
             patch("touken.advisor.hanafuda_plan", return_value=fake_plan):
            plan = dtl._hanafuda_active_plan(_today_at(12, 0), self.store)
        self.assertIsNone(plan)

    def test_raid_counts_only_this_game_day_rounds(self):
        now = _today_at_shanghai(12, 0)
        plan = {"runs_needed": 302, "seconds_per_loop": 420,
                "seconds_to_end": 18 * 86400, "tama_remaining": 280000}
        events = [(_today_at_shanghai(3, 59), {}), (_today_at_shanghai(4, 1), {}),
                  (_today_at_shanghai(9, 0), {})]
        with patch("touken.advisor.load_event_cards",
                   return_value={"联队战": {"mechanics": "raid"}}), \
             patch("touken.advisor.currency_plan", return_value=plan), \
             patch("touken.advisor._currency_period_events", return_value=events):
            result = dtl._raid_active_plan(now, self.store)
        self.assertEqual(result["completed_today"], 2)
        self.assertEqual(dtl._raid_daily_runs(result), 15)


class DayTimelineSuggestWindowsTests(unittest.TestCase):
    """suggest_windows 纯函数：占用段手工注入。"""

    def test_fills_earliest_free_window(self):
        blocks, shortfall = dtl.suggest_windows(480, [], 3600)
        self.assertEqual(shortfall, 0)
        self.assertEqual(blocks, [{"start_min": 480, "duration_min": 60,
                                   "note": ""}])

    def test_avoids_action_window_and_splits(self):
        occupied = [{"start_min": 540, "end_min": 547,
                     "label": "10:00 部队二派遣"}]
        blocks, shortfall = dtl.suggest_windows(480, occupied, 90 * 60)
        self.assertEqual(shortfall, 0)
        self.assertEqual(len(blocks), 2)
        self.assertEqual((blocks[0]["start_min"], blocks[0]["duration_min"]),
                         (480, 60))
        self.assertIn("部队二派遣", blocks[0]["note"])
        self.assertEqual((blocks[1]["start_min"], blocks[1]["duration_min"]),
                         (547, 30))

    def test_fragment_shorter_than_30min_skipped(self):
        occupied = [{"start_min": 480, "end_min": 605, "label": "a"},
                    {"start_min": 630, "end_min": 1680, "label": "b"}]
        blocks, shortfall = dtl.suggest_windows(480, occupied, 3600)
        self.assertEqual(blocks, [])
        self.assertEqual(shortfall, 3600)

    def test_max_two_blocks_then_shortfall(self):
        occupied = [{"start_min": 60, "end_min": 120, "label": "a"},
                    {"start_min": 300, "end_min": 360, "label": "b"}]
        blocks, shortfall = dtl.suggest_windows(0, occupied, 10 * 3600)
        self.assertEqual(len(blocks), 2)
        self.assertEqual((blocks[0]["start_min"], blocks[0]["duration_min"]), (0, 60))
        self.assertEqual((blocks[1]["start_min"], blocks[1]["duration_min"]), (120, 180))
        self.assertGreater(shortfall, 0)

    def test_shortfall_honest_when_no_room(self):
        blocks, shortfall = dtl.suggest_windows(27 * 60, [], 2 * 3600)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(shortfall, 3600)

    def test_zero_needed_no_blocks(self):
        blocks, shortfall = dtl.suggest_windows(480, [], 0)
        self.assertEqual(blocks, [])
        self.assertEqual(shortfall, 0)

    def test_raid_windows_fit_whole_rounds_around_dispatch(self):
        occupied = [{"start_min": 538, "end_min": 545,
                     "label": "09:00 部队三派遣"}]
        blocks, missing = dtl.suggest_round_windows(480, occupied, 18, 420)
        self.assertEqual([(b["start_min"], b["runs"], b["duration_min"])
                          for b in blocks], [(480, 8, 56), (545, 10, 70)])
        self.assertEqual(missing, 0)

    def test_raid_windows_report_rounds_that_cannot_fit(self):
        occupied = [{"start_min": 535, "end_min": 1680,
                     "label": "活动收摊"}]
        blocks, missing = dtl.suggest_round_windows(480, occupied, 10, 420)
        self.assertEqual(blocks[0]["runs"], 7)
        self.assertEqual(missing, 3)

    def test_raid_long_window_uses_two_task_sized_batches(self):
        blocks, missing = dtl.suggest_round_windows(0, [], 205, 120)
        self.assertEqual([b["runs"] for b in blocks], [99, 99])
        self.assertEqual(missing, 7)


class DayTimelineSuggestionIntegrationTests(unittest.TestCase):
    """build_day_timeline 的建议层：占用段组装 + 每日配额口径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TelemetryStore(Path(self.tmp.name) / "t.db")
        patcher = patch.object(scheduler, "map_options", lambda: _FAKE_MAPS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _plan(estimated, seconds_to_end):
        return {"estimated_seconds": estimated,
                "seconds_to_end": seconds_to_end,
                "tama_remaining": 300}

    def _build(self, now, cfg, plan, team_no=None, forced=None):
        with patch.object(dtl, "_hanafuda_active_plan", return_value=plan):
            return dtl.build_day_timeline(now, cfg=cfg, store=self.store,
                                          script_labels={},
                                          hanafuda_team_no=team_no,
                                          expedition_forced=forced or {},
                                          expedition_records={},
                                          expedition_help={
                                              "rounds_per_team": 0,
                                              "available_teams": []})

    def test_daily_quota_spread_over_days_left(self):
        """口径：estimated_seconds 按剩余天数平摊（和活动卡前端一致）。"""
        plan = self._plan(7200, 2 * 86400)  # 摊 2 天 → 今天 1 小时
        out = self._build(_today_at(8, 0), _cfg([]), plan)
        self.assertEqual(out["shortfall_seconds"], 0)
        self.assertEqual(out["suggestions"],
                         [{"start_min": 480, "duration_min": 60, "note": ""}])

    def test_quota_capped_by_estimated_seconds(self):
        plan = self._plan(7200, 12 * 3600)  # 只剩半天 → 平摊超标，卡回 7200
        out = self._build(_today_at(8, 0), _cfg([]), plan)
        self.assertEqual(sum(b["duration_min"] for b in out["suggestions"]), 120)

    def test_no_plan_no_suggestions(self):
        out = self._build(_today_at(8, 0), _cfg([]), None)
        self.assertIsNone(out["suggestions"])
        self.assertIsNone(out["shortfall_seconds"])

    def test_skips_daily_reset_window(self):
        plan = self._plan(3600, 86400)
        out = self._build(_today_at(3, 30), _cfg([]), plan)
        # 次日 03:30 到刷新仅剩 20 分钟空窗，不能塞下一小时。
        self.assertEqual(out["suggestions"], [])
        self.assertEqual(out["shortfall_seconds"], 1800)

    def test_managed_hanafuda_team_blocks_whole_shift(self):
        """活动队有 forced 班要跑：它的远征时段整段避让，不只是动作窗口。"""
        forced = {"k": _forced_record(3, "B3", _today_at(10, 0))}  # B3 = 90 分钟
        plan = self._plan(4 * 3600, 86400)
        out = self._build(_today_at(8, 0), _cfg([]), plan, team_no=3,
                          forced=forced)
        blocks = out["suggestions"]
        self.assertEqual(out["shortfall_seconds"], 0)
        self.assertEqual(len(blocks), 2)
        self.assertEqual((blocks[0]["start_min"], blocks[0]["duration_min"]),
                         (480, 118))  # 08:00 → 09:58 动作窗口前
        # 只避让动作窗口的话第二块会从 10:05 开始；整段避让必须等 11:30
        self.assertEqual(blocks[1]["start_min"], 690)

    def test_unmanaged_hanafuda_team_only_action_window(self):
        """forced 班不是活动队的：只占动作窗口。"""
        forced = {"k": _forced_record(3, "B3", _today_at(10, 0))}
        plan = self._plan(4 * 3600, 86400)
        out = self._build(_today_at(8, 0), _cfg([]), plan, team_no=4,
                          forced=forced)
        blocks = out["suggestions"]
        self.assertEqual(blocks[1]["start_min"], 605)  # 10:05 就能续

    def test_expired_shift_not_avoided(self):
        """过点废弃的 forced 班（终态 expired）不再占窗。"""
        today = time.strftime("%Y-%m-%d")
        key = f"{today}:adhoc:3:600"
        cfg = _cfg([])
        cfg["automation"].setdefault("slot_states", {})[key] = {
            "state": scheduler.SLOT_EXPIRED, "blocked_reason": ""}
        forced = {key: _forced_record(3, "B3", _today_at(10, 0))}
        plan = self._plan(2 * 3600, 86400)
        out = self._build(_today_at(8, 0), cfg, plan, team_no=3,
                          forced=forced)
        self.assertEqual(out["suggestions"],
                         [{"start_min": 480, "duration_min": 120, "note": ""}])

    def _raid_build(self, now, plan, *, raid_team_no=None, forced=None,
                    active=None, player_tasks=None):
        with patch.object(dtl, "_raid_active_plan", return_value=plan), \
             patch.object(dtl, "_hanafuda_active_plan", return_value=None):
            return dtl.build_day_timeline(
                now, cfg=_cfg([]), store=self.store,
                script_labels={}, raid_team_no=raid_team_no,
                expedition_forced=forced or {},
                expedition_records={},
                expedition_help={"rounds_per_team": 0, "available_teams": []},
                active=active, player_tasks=player_tasks)

    def test_raid_suggestions_wait_for_booked_daily(self):
        plan = {"runs_needed": 36, "seconds_per_loop": 420,
                "seconds_to_end": 2 * 86400, "tama_remaining": 10000}
        out = self._raid_build(_today_at(8, 0), plan,
            player_tasks=[{"start_min": 480, "end_min": 510, "label": "一键日课"}])
        self.assertEqual(out['suggestions'][0]['start_min'], 510)

    def test_raid_daily_rounds_split_around_same_team_expedition(self):
        forced = {"k": _forced_record(3, "B3", _today_at(10, 0))}
        plan = {"runs_needed": 36, "seconds_per_loop": 420,
                "seconds_to_end": 2 * 86400, "tama_remaining": 10000}
        out = self._raid_build(_today_at(8, 0), plan, raid_team_no=3,
                               forced=forced)
        self.assertEqual(out["activity"]["target_runs"], 18)
        self.assertEqual(out["activity"]["planned_runs"], 18)
        self.assertEqual([(b["start_min"], b["runs"]) for b in out["suggestions"]],
                         [(480, 16), (690, 2)])
        self.assertEqual(out["shortfall_seconds"], 0)

    def test_unknown_raid_team_avoids_every_expedition_shift(self):
        forced = {"k": _forced_record(3, "B3", _today_at(10, 0))}
        plan = {"runs_needed": 36, "seconds_per_loop": 420,
                "seconds_to_end": 2 * 86400, "tama_remaining": 10000}
        out = self._raid_build(_today_at(9, 55), plan, forced=forced)
        self.assertEqual(out["suggestions"][0]["start_min"], 690)

    def test_raid_without_measured_pace_makes_no_schedule(self):
        out = self._raid_build(_today_at(8, 0), None)
        self.assertIsNone(out["activity"])
        self.assertIsNone(out["suggestions"])

    def test_running_task_waits_for_fresh_raid_progress(self):
        plan = {"runs_needed": 36, "seconds_per_loop": 420,
                "seconds_to_end": 2 * 86400, "tama_remaining": 10000}
        out = self._raid_build(_today_at(8, 0), plan,
                               active={"script": "raid"})
        self.assertIsNone(out["suggestions"])
        self.assertIn("收工", out["hint"])


if __name__ == "__main__":
    unittest.main()


def test_timetable_keeps_same_anchor_through_midnight_until_four():
    evening = _today_at(23, 59)
    day_start, end = dtl._day_window(evening)
    assert dtl._day_window(day_start + 86400) == (day_start, end)
    assert dtl._day_window(day_start + 28 * 3600 - 1) == (day_start, end)
    assert dtl._day_window(end)[0] == day_start + 86400


def test_running_expedition_survives_four_oclock_rollover():
    day_start, end = dtl._day_window(_today_at(23, 59))
    records = {"4": {"map_code": "B3", "duration_min": 90,
        "dispatched_at": datetime.fromtimestamp(end - 60).isoformat(sep=" ")}}
    items = dtl._running_expedition_items(records, end + 60, day_start + 86400, {"B3": 90})
    assert len(items) == 1
    assert items[0]["state"] == "running"
