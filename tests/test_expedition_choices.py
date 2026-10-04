# -*- coding: utf-8 -*-
"""今天单班选择真正影响派遣，并能随时还原当天未来班次。

skipped：今天这班别跑。forced：今天这班一定要跑——排班总开关关着也单独
走状态机派出（排班开着时等价于「不跳过」）。两者互斥，后写的赢；
forced 不 lifted 自定义排班里被关掉的条目。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import day_timeline, scheduler
from panel import expedition_choices as ec
from panel import server
from panel.expedition_choices import is_skipped, load_choices, set_skipped


def _today_at(hour, minute=0):
    return time.mktime(time.strptime(
        f"{time.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}:00",
        "%Y-%m-%d %H:%M:%S"))


def _key():
    return "2026-09-29:custom:0:08:00"


class ExpeditionChoiceTests(unittest.TestCase):
    def test_custom_skip_prevents_due_and_restore_reenables(self):
        cfg = scheduler._defaults()
        cfg["automation"].update(enabled=True, mode="custom")
        cfg["entries"] = [{"time": "10:00", "team_no": 2,
                           "map_code": "B3", "enabled": True}]
        today = time.strftime("%Y-%m-%d")
        job = scheduler._custom_due(cfg, 600, today)[0]
        projected = scheduler.today_projection(cfg, now=_today_at(9, 0))["custom"][0]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "choices.json"
            set_skipped(key=projected["key"], team_no=2, map_code="B3",
                        planned_at=projected["planned_at"], skipped=True, path=path)
            choices = load_choices(path)
            self.assertEqual(scheduler._custom_due(cfg, 600, today, choices), [])
            self.assertEqual(scheduler.today_projection(
                cfg, now=_today_at(9, 0), choices=choices)["custom"][0]["state"], "skipped")
            self.assertTrue(is_skipped(choices, key=job["key"], team_no=2,
                                       map_code="B3", planned_at=projected["planned_at"]))
            self.assertTrue(is_skipped(choices, key=job["key"], team_no=2,
                                       map_code="B3", planned_at=projected["planned_at"] + 1200))
            self.assertFalse(is_skipped(choices, key=job["key"], team_no=3,
                                        map_code="B3", planned_at=projected["planned_at"]))
            set_skipped(key=projected["key"], team_no=2, map_code="B3",
                        planned_at=projected["planned_at"], skipped=False, path=path)
            self.assertEqual(load_choices(path), {})
            self.assertIn(projected["key"], json.loads(
                path.with_suffix(".json.bak").read_text(encoding="utf-8"))["skipped"])
            self.assertEqual(len(scheduler._custom_due(cfg, 600, today, load_choices(path))), 1)

    def test_unknown_choice_version_is_preserved_as_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "choices.json"
            path.write_text('{"version":0,"old":"kept"}', encoding="utf-8")
            self.assertEqual(load_choices(path), {})
            set_skipped(key="today:custom:0:10:00", team_no=2,
                        map_code="B3", planned_at=123, skipped=True, path=path)
            self.assertEqual(json.loads(path.with_suffix(".json.bak").read_text(
                encoding="utf-8"))["old"], "kept")

    def test_preset_skip_only_affects_same_shift(self):
        cfg = scheduler._defaults()
        cfg["automation"].update(enabled=True, mode="preset", start_time="08:00",
                                 preset="试排", teams=[2, 3, 4])
        preset = {"试排": {"lanes": [[{"offset_min": 0, "map_code": "B3",
                                          "duration_min": 90},
                                         {"offset_min": 90, "map_code": "B4",
                                          "duration_min": 90}]]}}
        with patch.object(scheduler, "preset_payload", return_value=preset):
            first = scheduler.today_projection(cfg, now=_today_at(8, 0))["preset"][0]
            choices = {first["key"]: {"team_no": 2, "map_code": "B3",
                                       "planned_at": first["planned_at"]}}
            self.assertEqual(scheduler._preset_due(cfg, 480, time.strftime("%Y-%m-%d"), choices), [])
            later = scheduler._preset_due(cfg, 570, time.strftime("%Y-%m-%d"), choices)
            self.assertEqual(len(later), 1)
            self.assertEqual(later[0]["map_code"], "B4")

    def test_timeline_shows_forced_on_both_sides_of_midnight(self):
        """v2：preset 投影已撤；forced 班跨午夜两侧都上轴。"""
        cfg = scheduler._defaults()
        cfg["automation"].update(enabled=False, mode="preset", start_time="20:00",
                                 preset="跨日", teams=[2, 3, 4])
        forced = {
            "k1": {"team_no": 3, "map_code": "B4",
                   "planned_at": _today_at(1, 0)},
            "k2": {"team_no": 2, "map_code": "B3",
                   "planned_at": _today_at(20, 0)},
        }
        with patch.object(scheduler, "map_options", return_value=[
                {"code": "B3", "duration_min": 90},
                {"code": "B4", "duration_min": 90}]):
            timeline = day_timeline.build_day_timeline(
                _today_at(12), cfg=cfg, store=object(),
                expedition_forced=forced, expedition_records={},
                expedition_help={"rounds_per_team": 0, "available_teams": []})
        self.assertEqual([(item["time_min"], item["map_code"])
                          for item in timeline["expeditions"]],
                         [(60, "B4"), (1200, "B3")])
        # forced 班排班总开关关着也照跑
        self.assertTrue(all(item["will_run"] for item in timeline["expeditions"]))

    def test_cancelling_forced_shift_frees_raid_window(self):
        """v2：点掉的 forced 班（记录删掉）不再占联队战窗口。"""
        cfg = scheduler._defaults()
        maps = [{"code": "B3", "duration_min": 90}]
        plan = {"runs_needed": 36, "seconds_per_loop": 420,
                "seconds_to_end": 2 * 86400, "tama_remaining": 10000}
        now = _today_at(8, 0)
        forced = {"k": {"team_no": 3, "map_code": "B3",
                        "planned_at": _today_at(10, 0)}}
        with patch.object(scheduler, "map_options", return_value=maps), \
             patch.object(day_timeline, "_raid_active_plan", return_value=plan), \
             patch.object(day_timeline, "_hanafuda_active_plan", return_value=None):
            before = day_timeline.build_day_timeline(
                now, cfg=cfg, store=object(), raid_team_no=3,
                expedition_forced=forced, expedition_records={},
                expedition_help={"rounds_per_team": 0, "available_teams": []})
            after = day_timeline.build_day_timeline(
                now, cfg=cfg, store=object(), raid_team_no=3,
                expedition_forced={}, expedition_records={},
                expedition_help={"rounds_per_team": 0, "available_teams": []})
        self.assertEqual([(b["start_min"], b["runs"])
                          for b in before["suggestions"]],
                         [(480, 16), (690, 2)])
        self.assertEqual([(b["start_min"], b["runs"])
                          for b in after["suggestions"]],
                         [(480, 18)])

    def test_api_checks_live_slot_before_changing_it(self):
        """端点先核对实时班次再落盘；body 用目标态 will_run（新契约：
        排班开着点「不跑」记 skipped，关着点「跑」记 forced）。"""
        slot = {"key": "today:custom:0:10:00", "team_no": 2,
                "map_code": "B3", "planned_at": _today_at(10),
                "toggleable": True, "base_enabled": True,
                "entry_enabled": True}
        timeline = {"expeditions": [slot]}
        client = TestClient(server.app)
        with patch.object(server, "_day_timeline_payload", return_value=timeline), \
             patch.object(ec, "set_slot_intention") as writer:
            response = client.put("/api/day-timeline/expedition-slot",
                                  json={"key": slot["key"], "will_run": False})
            self.assertEqual(response.status_code, 200)
            writer.assert_called_once_with(
                key=slot["key"], team_no=2, map_code="B3",
                planned_at=slot["planned_at"], will_run=False,
                base_enabled=True)
            slot["toggleable"] = False
            blocked = client.put("/api/day-timeline/expedition-slot",
                                 json={"key": slot["key"], "will_run": True})
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(writer.call_count, 1)


class ChoiceSetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "choices.json"

    def test_forced_roundtrip(self):
        ec.set_forced(key=_key(), team_no=2, map_code="B1", planned_at=1,
                      forced=True, path=self.path)
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertEqual(skipped, {})
        self.assertTrue(ec.is_forced(forced, key=_key(), team_no=2, map_code="B1"))
        self.assertFalse(ec.is_forced(forced, key=_key(), team_no=3, map_code="B1"))

    def test_skipped_and_forced_are_mutually_exclusive(self):
        ec.set_skipped(key=_key(), team_no=2, map_code="B1", planned_at=1,
                       skipped=True, path=self.path)
        ec.set_forced(key=_key(), team_no=2, map_code="B1", planned_at=1,
                      forced=True, path=self.path)
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertEqual(skipped, {})
        self.assertIn(_key(), forced)
        # 反过来再记跳过，强制被清掉
        ec.set_skipped(key=_key(), team_no=2, map_code="B1", planned_at=1,
                       skipped=True, path=self.path)
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertIn(_key(), skipped)
        self.assertEqual(forced, {})

    def test_legacy_file_without_forced_key(self):
        self.path.write_text(
            '{"version":1,"skipped":{"k":{"team_no":2,"map_code":"B1",'
            '"planned_at":1}}}', encoding="utf-8")
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertIn("k", skipped)
        self.assertEqual(forced, {})
        self.assertEqual(ec.load_choices(self.path), skipped)

    def test_set_slot_intention_by_base_state(self):
        # 排班开着点「会跑」：只清选择，不记 forced
        out = ec.set_slot_intention(key=_key(), team_no=2, map_code="B1",
                                    planned_at=1, will_run=True,
                                    base_enabled=True, path=self.path)
        self.assertEqual(out["skipped"], {})
        self.assertEqual(out["forced"], {})
        # 排班关着点「会跑」：记 forced
        out = ec.set_slot_intention(key=_key(), team_no=2, map_code="B1",
                                    planned_at=1, will_run=True,
                                    base_enabled=False, path=self.path)
        self.assertIn(_key(), out["forced"])
        # 点「不跑」：清 forced 记 skipped
        out = ec.set_slot_intention(key=_key(), team_no=2, map_code="B1",
                                    planned_at=1, will_run=False,
                                    base_enabled=False, path=self.path)
        self.assertIn(_key(), out["skipped"])
        self.assertEqual(out["forced"], {})


class AdhocChoiceTests(unittest.TestCase):
    """choices v2：自描述 forced 记录 + v1 老文件迁移（读取兼容、写回升级）。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "choices.json"

    def test_v1_file_loads_and_upgrades_on_write(self):
        self.path.write_text(json.dumps({
            "version": 1,
            "skipped": {"k1": {"team_no": 2, "map_code": "B1",
                               "planned_at": 1}},
            "forced": {"k2": {"team_no": 3, "map_code": "B3",
                              "planned_at": 2}},
        }, ensure_ascii=False), encoding="utf-8")
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertIn("k1", skipped)
        self.assertIn("k2", forced)
        ec.set_skipped(key="k3", team_no=4, map_code="B4", planned_at=3,
                       skipped=True, path=self.path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 2)
        self.assertIn("k1", payload["skipped"])  # 旧条目跟着升级，不丢
        self.assertIn("k2", payload["forced"])
        backup = json.loads(self.path.with_suffix(".json.bak")
                            .read_text(encoding="utf-8"))
        self.assertEqual(backup["version"], 1)  # 备份留着老版本

    def test_is_adhoc_record(self):
        self.assertTrue(ec.is_adhoc_record(
            {"start_min": 600, "duration_min": 90}))
        self.assertFalse(ec.is_adhoc_record({"team_no": 2}))
        self.assertFalse(ec.is_adhoc_record(
            {"start_min": True, "duration_min": 90}))  # 布尔拒收
        self.assertFalse(ec.is_adhoc_record(None))

    def test_adhoc_planned_date(self):
        self.assertEqual(ec.adhoc_planned_date(
            {"planned_at": "2026-09-29T08:00:00"}), "2026-09-29")
        ts = time.mktime(time.strptime("2026-09-29 08:00:00",
                                       "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(ec.adhoc_planned_date({"planned_at": ts}),
                         "2026-09-29")
        self.assertIsNone(ec.adhoc_planned_date({"planned_at": "垃圾"}))
        self.assertIsNone(ec.adhoc_planned_date({}))
        self.assertIsNone(ec.adhoc_planned_date(None))

    def test_set_forced_adhoc_roundtrip_and_stale_cleanup(self):
        today = time.strftime("%Y-%m-%d")
        yesterday = time.strftime("%Y-%m-%d",
                                  time.localtime(time.time() - 86400))
        stale_key = ec.adhoc_key(yesterday, 2, 480)
        ec.set_forced_adhoc(
            key=stale_key, team_no=2, map_code="B1", start_min=480,
            duration_min=30, planned_at=f"{yesterday}T08:00:00",
            path=self.path)
        key = ec.adhoc_key(today, 4, 600)
        ec.set_forced_adhoc(
            key=key, team_no=4, map_code="B3", start_min=600,
            duration_min=90, planned_at=f"{today}T10:00:00", path=self.path)
        _, forced = ec.load_choice_sets(self.path)
        self.assertNotIn(stale_key, forced)  # 昨天的残留被顺手清掉
        self.assertEqual(forced[key]["duration_min"], 90)
        self.assertTrue(ec.is_adhoc_record(forced[key]))
        # 点掉 = 删记录
        ec.set_forced_adhoc(key=key, team_no=4, map_code="B3",
                            start_min=600, duration_min=90,
                            planned_at=f"{today}T10:00:00", forced=False,
                            path=self.path)
        self.assertNotIn(key, ec.load_choice_sets(self.path)[1])


class ExpeditionSlotEndpointTests(unittest.TestCase):
    """PUT /api/day-timeline/expedition-slot：will_run 目标态三态落盘。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "choices.json"
        self.slot = {"key": _key(), "team_no": 2, "map_code": "B1",
                     "planned_at": 1759094400.0, "toggleable": True,
                     "base_enabled": True, "entry_enabled": True}

    def _put(self, payload):
        real = ec.set_slot_intention

        def to_temp(**kwargs):
            kwargs["path"] = self.path
            return real(**kwargs)

        with patch.object(server, "_day_timeline_payload",
                          return_value={"expeditions": [self.slot]}), \
             patch.object(ec, "set_slot_intention", side_effect=to_temp):
            return TestClient(server.app).put("/api/day-timeline/expedition-slot",
                                              json=payload)

    def test_will_run_false_records_skip(self):
        response = self._put({"key": _key(), "will_run": False})
        self.assertEqual(response.status_code, 200)
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertIn(_key(), skipped)
        self.assertEqual(forced, {})

    def test_will_run_true_with_automation_off_records_force(self):
        self.slot["base_enabled"] = False
        response = self._put({"key": _key(), "will_run": True})
        self.assertEqual(response.status_code, 200)
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertEqual(skipped, {})
        self.assertIn(_key(), forced)

    def test_will_run_true_with_automation_on_clears_both(self):
        response = self._put({"key": _key(), "will_run": True})
        self.assertEqual(response.status_code, 200)
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertEqual(skipped, {})
        self.assertEqual(forced, {})

    def test_untoggleable_slot_rejected(self):
        self.slot["toggleable"] = False
        response = self._put({"key": _key(), "will_run": False})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.path.exists())

    def test_entry_disabled_cannot_be_forced(self):
        self.slot["base_enabled"] = False
        self.slot["entry_enabled"] = False
        response = self._put({"key": _key(), "will_run": True})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.path.exists())

    def test_adhoc_key_goes_to_set_forced_adhoc(self):
        """自描述班（建议采纳的）走 set_forced_adhoc：点掉删记录、点回重写。"""
        key = ec.adhoc_key(time.strftime("%Y-%m-%d"), 4, 600)
        slot = {"key": key, "team_no": 4, "map_code": "B3",
                "planned_at": _today_at(10), "time_min": 600,
                "duration_min": 90, "toggleable": True}
        client = TestClient(server.app)
        with patch.object(server, "_day_timeline_payload",
                          return_value={"expeditions": [slot]}), \
             patch.object(ec, "set_forced_adhoc") as writer, \
             patch.object(ec, "set_slot_intention") as legacy:
            response = client.put("/api/day-timeline/expedition-slot",
                                  json={"key": key, "will_run": False})
            self.assertEqual(response.status_code, 200)
            legacy.assert_not_called()
            kwargs = writer.call_args.kwargs
            self.assertEqual(kwargs["key"], key)
            self.assertEqual(kwargs["team_no"], 4)
            self.assertEqual(kwargs["map_code"], "B3")
            self.assertEqual(kwargs["start_min"], 600)
            self.assertEqual(kwargs["duration_min"], 90)
            self.assertFalse(kwargs["forced"])


if __name__ == "__main__":
    unittest.main()


def test_adhoc_training_settings_survive_storage_and_due_projection(tmp_path):
    from panel import scheduler
    today = time.strftime('%Y-%m-%d')
    now = time.time()
    key = ec.adhoc_key(today, 4, 600)
    path = tmp_path / 'choices.json'
    ec.set_forced_adhoc(key=key, team_no=4, map_code='B3', start_min=600,
        duration_min=90, planned_at=time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now)),
        sakura_before_dispatch=True, repair_threshold='heavy', path=path)
    _, forced = ec.load_choice_sets(path)
    jobs = scheduler.adhoc_due({'automation': {'last_runs': {}}}, forced, now, today)
    assert jobs[0]['sakura_before_dispatch'] is True
    assert jobs[0]['repair_threshold'] == 'heavy'
    slot = scheduler._new_slot(jobs[0], {'automation': {}}, now)
    assert slot['sakura_before_dispatch'] is True
    assert slot['repair_threshold'] == 'heavy'
    forced[key].pop('sakura_before_dispatch')
    forced[key].pop('repair_threshold')
    old_job = scheduler.adhoc_due({'automation': {'last_runs': {}}}, forced, now, today)[0]
    assert old_job['sakura_before_dispatch'] is False
    assert old_job['repair_threshold'] == 'light'
