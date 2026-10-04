# -*- coding: utf-8 -*-
"""远征排班占用状态机测试（slot_states / 接管门卫 / 安全插队旗标）。

红线遵守：
- 全部在临时目录跑（patch panel.scheduler 的 _SCHED_PATH/STATE_DIR、
  panel.server 的 STATUS_DIR、touken.flows.battle 的 STATE_DIR），
  绝不碰真实用户数据。
- 所有正常播报逐条过 report_judge 翻车词表不判红；failed_unknown 必须判红。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from panel import scheduler as s
from panel import expedition_choices as ec
from touken.flows.battle import BattleMixin
from touken.flows.raid import RaidMixin
from touken.flows.report_judge import _is_fail


def _today_at(hour, minute=0, second=0):
    return time.mktime(time.strptime(
        f"{time.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}:{second:02d}",
        "%Y-%m-%d %H:%M:%S"))


def _ts_text(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _custom_cfg(**auto_over):
    cfg = s._defaults()
    cfg["automation"]["enabled"] = True
    cfg["automation"]["mode"] = "custom"
    cfg["automation"].update(auto_over)
    cfg["entries"] = [dict(time="08:00", team_no=2, map_code="B1")]
    return cfg


def _due(cfg, now_min=490):
    return s._custom_due(cfg, now_min, time.strftime("%Y-%m-%d"))


class SlotFsmTests(unittest.TestCase):
    """验收 1/2/3/4/6：到点派遣、忙碌挂旗、门卫冷却、过期终态、重启不重复派。"""

    def test_idle_due_goes_ready_then_starts(self):
        cfg = _custom_cfg()
        now = _today_at(8, 10)
        due = _due(cfg)
        self.assertEqual(len(due), 1)
        out = s.tick(cfg, due, now, runner_busy=False, emulator_ok=True, records={})
        slot = cfg["automation"]["slot_states"][due[0]["key"]]
        self.assertEqual(slot["state"], "ready")
        self.assertIsNone(out["start"])  # 15 秒预告窗口内不起子进程
        out = s.tick(cfg, due, now + 16, runner_busy=False, emulator_ok=True, records={})
        self.assertIsNotNone(out["start"])
        self.assertEqual(out["start"]["key"], due[0]["key"])

    def test_busy_writes_flag_and_clears_after_dispatch(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(s, "STATE_DIR", Path(d)):
            cfg = _custom_cfg()
            now = _today_at(8, 10)
            due = _due(cfg)
            out = s.tick(cfg, due, now, runner_busy=True,
                         emulator_ok=False, records={})
            slot = cfg["automation"]["slot_states"][due[0]["key"]]
            self.assertEqual(slot["state"], "waiting_busy")
            self.assertTrue(s.sync_takeover_flag(cfg, now))
            flag = json.loads((Path(d) / "expedition_takeover.json")
                              .read_text(encoding="utf-8"))
            self.assertEqual(flag["key"], due[0]["key"])
            self.assertEqual(flag["team_no"], 2)
            for _, msg in out["events"]:
                self.assertFalse(_is_fail(msg), msg)

            # 忙完了：宽限内补派成功
            out = s.tick(cfg, due, now + 600, runner_busy=False,
                         emulator_ok=True, records={})
            self.assertEqual(slot["state"], "ready")
            out = s.tick(cfg, due, now + 620, runner_busy=False,
                         emulator_ok=True, records={})
            self.assertIsNotNone(out["start"])
            records = {"2": {"map_code": "B1",
                             "dispatched_at": _ts_text(now + 620),
                             "duration_min": 60}}
            events, changed = s.resolve_inflight(
                cfg, due[0]["key"], None, now + 625, records,
                {"key": due[0]["key"], "outcome": "done"})
            self.assertTrue(changed)
            self.assertEqual(slot["state"], "dispatched")
            self.assertIn(due[0]["key"], cfg["automation"]["last_runs"])
            for _, msg in events:
                self.assertFalse(_is_fail(msg), msg)
            # 没有 waiting_busy 班次 → 旗标清除
            self.assertFalse(s.sync_takeover_flag(cfg, now + 625))
            self.assertFalse((Path(d) / "expedition_takeover.json").exists())

    def test_refused_goes_waiting_unknown_with_300s_cooldown(self):
        cfg = _custom_cfg()
        now = _today_at(8, 10)
        due = _due(cfg)
        s.tick(cfg, due, now, runner_busy=False, emulator_ok=True, records={})
        key = due[0]["key"]
        slot = cfg["automation"]["slot_states"][key]
        events, changed = s.resolve_inflight(
            cfg, key, None, now + 20, {},
            {"key": key, "outcome": "refused", "detail": "画面不在本丸，像主人在手动玩"})
        self.assertTrue(changed)
        self.assertEqual(slot["state"], "waiting_unknown")
        self.assertEqual(slot["attempts"], 0)  # 门卫没动手，不算派遣尝试
        self.assertAlmostEqual(slot["next_retry_at"], now + 320, delta=1)
        for _, msg in events:
            self.assertFalse(_is_fail(msg), msg)
        # 冷却内不猛试
        out = s.tick(cfg, due, now + 100, runner_busy=False,
                     emulator_ok=True, records={})
        self.assertIsNone(out["start"])
        self.assertEqual(slot["state"], "waiting_unknown")
        # 冷却过了才重新预告接管
        out = s.tick(cfg, due, now + 330, runner_busy=False,
                     emulator_ok=True, records={})
        self.assertEqual(slot["state"], "ready")

    def test_expired_is_terminal_and_never_revives(self):
        cfg = _custom_cfg()  # 默认 max_delay 30 → 08:30 过期
        now = _today_at(8, 10)
        due = _due(cfg)
        s.tick(cfg, due, now, runner_busy=True, emulator_ok=False, records={})
        key = due[0]["key"]
        slot = cfg["automation"]["slot_states"][key]
        out = s.tick(cfg, [], _today_at(8, 31), runner_busy=False,
                     emulator_ok=True, records={})
        self.assertEqual(slot["state"], "expired")
        for _, msg in out["events"]:
            self.assertFalse(_is_fail(msg), msg)
        # 之后再多 tick 也不派；就算它又出现在 due 里，终态不重建不复活
        out = s.tick(cfg, [], _today_at(12, 0), runner_busy=False,
                     emulator_ok=True, records={})
        self.assertIsNone(out["start"])
        out = s.tick(cfg, [dict(due[0])], _today_at(8, 32), runner_busy=False,
                     emulator_ok=True, records={})
        self.assertIsNone(out["start"])
        self.assertEqual(out["events"], [])
        self.assertEqual(slot["state"], "expired")

    def test_slot_states_persist_and_terminal_never_redispatches(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(s, "_SCHED_PATH", Path(d) / "expedition.json"):
            cfg = _custom_cfg()
            now = _today_at(8, 10)
            due = _due(cfg)
            s.tick(cfg, due, now, runner_busy=False, emulator_ok=True, records={})
            key = due[0]["key"]
            slot = cfg["automation"]["slot_states"][key]
            slot["state"] = "dispatched"
            slot["dispatched_at"] = _ts_text(now)
            cfg["automation"]["last_runs"][key] = slot["dispatched_at"]
            s.save_config(cfg)
            # 模拟重启：从磁盘重载后再 tick，终态班次绝不重复派
            cfg2 = s.load_config()
            self.assertEqual(
                cfg2["automation"]["slot_states"][key]["state"], "dispatched")
            out = s.tick(cfg2, [dict(due[0])], now + 120, runner_busy=False,
                         emulator_ok=True, records={})
            self.assertIsNone(out["start"])
            self.assertEqual(out["events"], [])

    def test_legacy_config_gets_new_defaults(self):
        """迁移：老 expedition.json 没有 slot_states/max_delay_min 也正常。"""
        with tempfile.TemporaryDirectory() as d, \
                patch.object(s, "_SCHED_PATH", Path(d) / "expedition.json"):
            legacy = {"version": 2,
                      "automation": {"enabled": True, "mode": "custom",
                                     "teams": [2, 3, 4], "last_runs": {"k": "v"}},
                      "entries": []}
            (Path(d) / "expedition.json").write_text(
                json.dumps(legacy), encoding="utf-8")
            cfg = s.load_config()
            self.assertEqual(cfg["automation"]["max_delay_min"], 30)
            self.assertEqual(cfg["automation"]["slot_states"], {})
            self.assertEqual(cfg["automation"]["last_runs"], {"k": "v"})


class CapitalistAndDelayTests(unittest.TestCase):
    """验收 5/7：资本家 4 倍窗口 + 顺延、max_delay_min 默认值与覆盖。"""

    def test_max_delay_default_and_override(self):
        self.assertEqual(s._defaults()["automation"]["max_delay_min"], 30)
        cfg = _custom_cfg()
        self.assertEqual(s._grace_min(cfg["automation"]), 30)
        cfg["automation"]["max_delay_min"] = 45
        self.assertEqual(s._grace_min(cfg["automation"]), 45)
        cfg["automation"]["capitalist"] = True
        self.assertEqual(s._grace_min(cfg["automation"]), 180)
        now = _today_at(8, 10)
        due = _due(cfg)
        s.tick(cfg, due, now, runner_busy=False, emulator_ok=True, records={})
        slot = cfg["automation"]["slot_states"][due[0]["key"]]
        self.assertAlmostEqual(slot["expires_at"],
                               _today_at(8, 0) + 180 * 60, delta=1)

    def test_capitalist_window_quadruples_with_hard_cap(self):
        cfg = _custom_cfg(capitalist=True, max_delay_min=30)
        self.assertEqual(len(_due(cfg, 8 * 60 + 119)), 1)  # 119 分钟还在窗内
        self.assertEqual(_due(cfg, 8 * 60 + 121), [])      # 超 4 倍硬封顶
        plain = _custom_cfg(capitalist=False)
        self.assertEqual(len(_due(plain, 8 * 60 + 29)), 1)
        self.assertEqual(_due(plain, 8 * 60 + 31), [])

    def test_capitalist_late_confirm_shifts_lane(self):
        cfg = _custom_cfg(capitalist=True)
        now = _today_at(9, 40)  # 晚 100 分钟，仍在 4 倍窗内
        job = {"key": "k1", "team_no": 2, "map_code": "B1", "late_min": 100,
               "shift_key": "lane:0",
               "planned_at": time.strftime("%Y-%m-%dT08:00:00")}
        s.tick(cfg, [job], now, runner_busy=False, emulator_ok=True, records={})
        slot = cfg["automation"]["slot_states"]["k1"]
        records = {"2": {"map_code": "B1", "dispatched_at": _ts_text(now),
                         "duration_min": 60}}
        events, changed = s.resolve_inflight(cfg, "k1", None, now + 600,
                                             records, None)
        self.assertTrue(changed)
        self.assertEqual(slot["state"], "dispatched")
        # 顺延 = 到点时已晚 100 分钟 + 等待 10 分钟（lane_shifts 现有语义）
        self.assertEqual(cfg["automation"]["lane_shifts"]["lane:0"], 110)
        for _, msg in events:
            self.assertFalse(_is_fail(msg), msg)

    def test_preset_due_reads_max_delay(self):
        now = time.localtime()
        now_min = now.tm_hour * 60 + now.tm_min
        today = time.strftime("%Y-%m-%d")
        # 合成 preset：单段 600 分钟，让「晚了多久」可控可断言
        fake = {"小判": {"lanes": [[{"offset_min": 0, "map_code": "A1",
                                     "duration_min": 600}]], "totals": ""}}
        cfg = s._defaults()
        auto = cfg["automation"]
        auto.update(enabled=True, mode="preset", preset="小判", teams=[2, 3, 4])
        with patch.object(s, "preset_payload", return_value=fake):
            start = time.localtime(time.time() - 60 * 60)
            auto["start_time"] = f"{start.tm_hour:02d}:{start.tm_min:02d}"
            # 晚 60 分钟：普通（30 分钟宽限）跳过，资本家（4×30=120）保留
            self.assertEqual(s._preset_due(cfg, now_min, today), [])
            auto["capitalist"] = True
            due = s._preset_due(cfg, now_min, today)
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["late_min"], 60)
            self.assertTrue(due[0]["planned_at"])
            # 晚 121 分钟：超 4 倍硬顶，资本家也不派
            start = time.localtime(time.time() - 121 * 60)
            auto["start_time"] = f"{start.tm_hour:02d}:{start.tm_min:02d}"
            self.assertEqual(s._preset_due(cfg, now_min, today), [])


class TakeoverFlagTests(unittest.TestCase):
    """验收 8/9：旗标新鲜度判定、玩法循环顶部命中即安全收工。"""

    def test_flag_fresh_stale_missing(self):
        from touken.flows import battle
        mixin = BattleMixin()
        with tempfile.TemporaryDirectory() as d, \
                patch.object(battle, "STATE_DIR", Path(d)):
            flag = Path(d) / "expedition_takeover.json"
            self.assertFalse(mixin._expedition_takeover_requested())  # 不存在
            now = time.time()
            flag.write_text(json.dumps(
                {"key": "k", "team_no": 2, "map_code": "B1",
                 "requested_at": now}), encoding="utf-8")
            self.assertTrue(mixin._expedition_takeover_requested(now=now + 60))
            self.assertTrue(mixin._expedition_takeover_requested(
                now=now + 2 * 3600))
            flag.write_text(json.dumps(
                {"key": "k", "team_no": 2, "map_code": "B1",
                 "requested_at": now - 3 * 3600}), encoding="utf-8")
            self.assertFalse(mixin._expedition_takeover_requested(now=now))

    def test_clear_takeover_flag(self):
        """排班暂停/停用时必须清旗，不然玩法循环为不会来的排班白收工。"""
        with tempfile.TemporaryDirectory() as d, \
                patch.object(s, "STATE_DIR", Path(d)):
            flag = Path(d) / "expedition_takeover.json"
            flag.write_text("{}", encoding="utf-8")
            s.clear_takeover_flag()
            self.assertFalse(flag.exists())
            s.clear_takeover_flag()  # 没旗也不许炸

    def test_raid_loop_top_honors_takeover_flag(self):
        agent = Mock()
        agent.config = {"raid": {"ui_title": {"template": "x"},
                                 "activity_entry": {"template": "y"}},
                        "team_select": {"teams": {"3": {}}}}
        agent.navigate_to_stream.return_value = iter([])
        agent.current_location = "出阵"
        agent.maa.template_match.return_value = True
        agent._expedition_takeover_requested.return_value = True
        messages = list(RaidMixin.raid_stream(agent, max_rounds=3, team_no=3))
        hit = [m for m in messages if "🚩 远征排班请求接管" in m]
        self.assertEqual(len(hit), 1)
        self.assertFalse(_is_fail(hit[0]), hit[0])
        agent._click_point.assert_not_called()  # 没开新圈


class GatekeeperTests(unittest.TestCase):
    """验收 10：接管门卫只读观察，认不出本丸就写 refused、不导航不点击。"""

    def _params(self, key):
        return {"map_code": "B1", "team_no": 2, "scheduled": True,
                "slot_key": key}

    def test_refuses_when_not_honmaru(self):
        from panel import server
        with tempfile.TemporaryDirectory() as d, \
                patch.object(server, "STATUS_DIR", Path(d)):
            agent = Mock()
            agent.maa.exists.return_value = False
            messages = list(server._build_dispatch(
                agent, "unused", self._params("k9")))
            result = json.loads((Path(d) / "dispatch_result.json")
                                .read_text(encoding="utf-8"))
            self.assertEqual(result["outcome"], "refused")
            self.assertEqual(result["key"], "k9")
            agent.collect_expedition_stream.assert_not_called()
            agent.expedition_stream.assert_not_called()
            agent.navigate_to_stream.assert_not_called()
            agent.maa.click.assert_not_called()
            refuse = [m for m in messages if "交回排班" in m]
            self.assertTrue(refuse)
            self.assertFalse(_is_fail(refuse[0]), refuse[0])

    def test_passes_and_writes_done(self):
        from panel import server
        with tempfile.TemporaryDirectory() as d, \
                patch.object(server, "STATUS_DIR", Path(d)):
            agent = Mock()
            agent.maa.exists.return_value = True
            agent.collect_expedition_stream.return_value = iter(["收菜"])
            agent.expedition_stream.return_value = iter([
                "[远征] ✅ 部队2已出发：时代2，收工"])
            messages = list(server._build_dispatch(
                agent, "unused", self._params("k10")))
            self.assertEqual(messages, [
                "收菜", "[远征] ✅ 部队2已出发：时代2，收工"])
            result = json.loads((Path(d) / "dispatch_result.json")
                                .read_text(encoding="utf-8"))
            self.assertEqual(result["outcome"], "done")
            self.assertEqual(result["key"], "k10")
            agent.expedition_stream.assert_called_once()

    def test_dispatch_flow_failure_is_written_with_reason(self):
        from panel import server
        with tempfile.TemporaryDirectory() as d, \
                patch.object(server, "STATUS_DIR", Path(d)):
            agent = Mock()
            agent.maa.exists.return_value = True
            agent.collect_expedition_stream.return_value = iter(["收菜"])
            agent.expedition_stream.return_value = iter([
                "[远征] 找不到可用的远征开始按钮；这支部队可能不满足该图条件，"
                "或仍在远征，停止派遣"])
            list(server._build_dispatch(
                agent, "unused", self._params("k11")))
            result = json.loads((Path(d) / "dispatch_result.json")
                                .read_text(encoding="utf-8"))
            self.assertEqual(result["outcome"], "failed")
            self.assertEqual(result["key"], "k11")
            self.assertIn("不满足该图条件", result["detail"])

    def test_explicit_dispatch_failure_is_terminal_without_retry(self):
        cfg = _custom_cfg()
        now = _today_at(8, 10)
        due = _due(cfg)
        s.tick(cfg, due, now, runner_busy=False, emulator_ok=True, records={})
        key = due[0]["key"]
        slot = cfg["automation"]["slot_states"][key]

        events, changed = s.resolve_inflight(
            cfg, key, None, now + 20, {},
            {"key": key, "outcome": "failed",
             "detail": "这支部队可能不满足该图条件"})

        self.assertTrue(changed)
        self.assertEqual(slot["state"], s.SLOT_FAILED)
        self.assertEqual(slot["next_retry_at"], 0)
        self.assertIn("不再自动重试", events[0][1])
        self.assertTrue(_is_fail(events[0][1]), events[0][1])


class MessageWordlistTests(unittest.TestCase):
    """红线 1：正常播报逐条过翻车词表不判红；failed_unknown 必须判红。"""

    def test_normal_messages_never_judged_fail(self):
        cfg = _custom_cfg()
        now = _today_at(8, 10)
        due = _due(cfg)
        key = due[0]["key"]
        normal = []
        # waiting_busy
        normal += [m for _, m in s.tick(
            cfg, due, now, runner_busy=True, emulator_ok=False,
            records={})["events"]]
        # waiting_unknown：游戏没在跑
        normal += [m for _, m in s.tick(
            cfg, due, now + 30, runner_busy=False, emulator_ok=False,
            records={})["events"]]
        # waiting_unknown：部队还在外面（map 不同，避免触发记录确认）
        away = {"2": {"map_code": "C1", "dispatched_at": _ts_text(now + 40),
                      "duration_min": 600}}
        normal += [m for _, m in s.tick(
            cfg, due, now + 40, runner_busy=False, emulator_ok=True,
            records=away)["events"]]
        # ready ⏳ 预告
        normal += [m for _, m in s.tick(
            cfg, due, now + 50, runner_busy=False, emulator_ok=True,
            records={})["events"]]
        # 门卫拒绝
        events, _ = s.resolve_inflight(cfg, key, None, now + 60, {},
                                       {"key": key, "outcome": "refused",
                                        "detail": "画面不在本丸"})
        normal += [m for _, m in events]
        # 点了但没确认到 → 冷却重试（两次都还是正常播报）
        events, _ = s.resolve_inflight(cfg, key, None, now + 400, {}, None)
        normal += [m for _, m in events]
        events, _ = s.resolve_inflight(cfg, key, None, now + 800, {}, None)
        normal += [m for _, m in events]
        # 确认派出（另一班， dispatched 话术）
        cfg2 = _custom_cfg()
        due2 = _due(cfg2)
        s.tick(cfg2, due2, now, runner_busy=False, emulator_ok=True, records={})
        key2 = due2[0]["key"]
        records = {"2": {"map_code": "B1", "dispatched_at": _ts_text(now),
                         "duration_min": 60}}
        events, _ = s.resolve_inflight(cfg2, key2, None, now + 30, records,
                                       {"key": key2, "outcome": "done"})
        normal += [m for _, m in events]
        # 过期跳过
        cfg3 = _custom_cfg()
        due3 = _due(cfg3)
        s.tick(cfg3, due3, now, runner_busy=True, emulator_ok=False, records={})
        normal += [m for _, m in s.tick(
            cfg3, [], _today_at(8, 31), runner_busy=False, emulator_ok=True,
            records={})["events"]]

        self.assertTrue(normal, "测试应实际收集到正常播报")
        for msg in normal:
            self.assertFalse(_is_fail(msg), msg)

    def test_failed_unknown_is_judged_fail(self):
        cfg = _custom_cfg()
        now = _today_at(8, 10)
        due = _due(cfg)
        s.tick(cfg, due, now, runner_busy=False, emulator_ok=True, records={})
        key = due[0]["key"]
        slot = cfg["automation"]["slot_states"][key]
        events = []
        for i in range(3):
            events, _ = s.resolve_inflight(
                cfg, key, None, now + 400 * (i + 1), {}, None)
        self.assertEqual(slot["state"], "failed_unknown")
        self.assertEqual(slot["attempts"], 3)
        self.assertTrue(all(_is_fail(m) for _, m in events))
        # 终态：不再起子进程
        out = s.tick(cfg, due, now + 2000, runner_busy=False,
                     emulator_ok=True, records={})
        self.assertIsNone(out["start"])


class ProjectionTests(unittest.TestCase):
    """时间表实况投影：preset/custom 各班状态、pending/missed 区分。"""

    def test_projection_marks_states(self):
        cfg = _custom_cfg()
        now = _today_at(8, 10)
        due = _due(cfg)
        s.tick(cfg, due, now, runner_busy=True, emulator_ok=False, records={})
        cfg["entries"].append(dict(time="23:59", team_no=3, map_code="C1"))
        cfg["entries"].append(dict(time="00:01", team_no=4, map_code="C2"))
        proj = s.today_projection(cfg, now=now)
        by_index = {i["index"]: i for i in proj["custom"]}
        self.assertEqual(by_index[0]["state"], "waiting_busy")
        self.assertEqual(by_index[0]["time"], "08:00")
        self.assertEqual(by_index[0]["late_min"], 10)
        self.assertEqual(by_index[1]["state"], "pending")  # 还没到点
        self.assertEqual(by_index[2]["state"], "missed")   # 过去但面板没见过
        self.assertIn("preset", proj)


class AdhocDispatchTests(unittest.TestCase):
    """自描述 forced 班（建议引擎采纳的班）：到点走同一条状态机路，
    排班总开关关着也照常（forced_only 天然放行）。"""

    def _cfg(self, **auto_over):
        cfg = s._defaults()
        cfg["automation"]["enabled"] = True
        cfg["automation"]["mode"] = "custom"
        cfg["automation"].update(auto_over)
        cfg["entries"] = []
        return cfg

    def _record(self, team=4, map_code="B3", planned_ts=None,
                start_min=600, duration_min=90):
        planned_ts = _today_at(10, 0) if planned_ts is None else planned_ts
        return {"team_no": team, "map_code": map_code,
                "planned_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                            time.localtime(planned_ts)),
                "start_min": start_min, "duration_min": duration_min}

    def test_due_at_planned_time_within_grace(self):
        today = time.strftime("%Y-%m-%d")
        key = ec.adhoc_key(today, 4, 600)
        forced = {key: self._record()}
        due = s.adhoc_due(self._cfg(), forced, _today_at(10, 5), today)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["key"], key)
        self.assertEqual((due[0]["team_no"], due[0]["map_code"]), (4, "B3"))
        self.assertEqual(due[0]["late_min"], 5)
        # 提前不产班
        self.assertEqual(s.adhoc_due(self._cfg(), forced,
                                     _today_at(9, 59), today), [])

    def test_grace_cutoff(self):
        today = time.strftime("%Y-%m-%d")
        forced = {ec.adhoc_key(today, 4, 600): self._record()}
        self.assertEqual(
            s.adhoc_due(self._cfg(), forced, _today_at(10, 31), today), [])
        cfg = self._cfg(capitalist=True)  # 资本家 4 倍宽限一样罩 adhoc
        self.assertEqual(
            len(s.adhoc_due(cfg, forced, _today_at(11, 59), today)), 1)

    def test_done_or_stale_records_not_due(self):
        today = time.strftime("%Y-%m-%d")
        key = ec.adhoc_key(today, 4, 600)
        cfg = self._cfg()
        # 已派过（last_runs 有账）不重复产班
        cfg["automation"]["last_runs"][key] = _ts_text(_today_at(10, 5))
        self.assertEqual(s.adhoc_due(cfg, {key: self._record()},
                                     _today_at(10, 5), today), [])
        # 昨天的残留、引用排班条目的 legacy forced 都不参与
        yesterday = _today_at(10, 0) - 86400
        mixed = {
            "old": self._record(planned_ts=yesterday),
            "legacy": {"team_no": 4, "map_code": "B3",
                       "planned_at": _today_at(10, 0)},
        }
        self.assertEqual(s.adhoc_due(self._cfg(), mixed,
                                     _today_at(10, 5), today), [])

    def test_forced_only_passes_adhoc_when_switch_off(self):
        today = time.strftime("%Y-%m-%d")
        key = ec.adhoc_key(today, 4, 600)
        forced = {key: self._record()}
        due = s.adhoc_due(self._cfg(enabled=False), forced,
                          _today_at(10, 5), today)
        self.assertEqual(s.forced_only(due, forced), due)

    def test_tick_to_confirmed_dispatch_round_trip(self):
        """到点 → ready → 起派遣（队/图正确）→ expeditions.json 确认 → 终态+last_runs。"""
        today = time.strftime("%Y-%m-%d")
        key = ec.adhoc_key(today, 4, 600)
        forced = {key: self._record()}
        cfg = self._cfg()
        now = _today_at(10, 5)
        due = s.adhoc_due(cfg, forced, now, today)
        out = s.tick(cfg, due, now, runner_busy=False, emulator_ok=True,
                     records={})
        slot = cfg["automation"]["slot_states"][key]
        self.assertEqual(slot["state"], "ready")
        self.assertIsNone(out["start"])
        out = s.tick(cfg, due, now + 16, runner_busy=False, emulator_ok=True,
                     records={})
        self.assertIsNotNone(out["start"])
        # runner.start("dispatch", ...) 的班：队和图就是 forced 记录里那套
        self.assertEqual(out["start"]["team_no"], 4)
        self.assertEqual(out["start"]["map_code"], "B3")
        records = {"4": {"map_code": "B3",
                         "dispatched_at": _ts_text(now + 16),
                         "duration_min": 90}}
        events, changed = s.resolve_inflight(
            cfg, key, None, now + 20, records,
            {"key": key, "outcome": "done"})
        self.assertTrue(changed)
        self.assertEqual(slot["state"], "dispatched")
        self.assertIn(key, cfg["automation"]["last_runs"])
        for _, msg in out["events"] + events:
            self.assertFalse(_is_fail(msg), msg)
        # 落了 last_runs 的班不再产班：重启不重复派
        self.assertEqual(s.adhoc_due(cfg, forced, now + 20, today), [])


if __name__ == "__main__":
    unittest.main()


def test_next_shift_waits_for_confirmed_collection(tmp_path):
    from panel import server
    from panel import expedition_observation
    agent = Mock()
    agent.maa.exists.return_value = True
    agent.collect_expedition_stream.return_value = iter(["没有读到结算"])
    record = {"4": {"map_code": "C1", "dispatched_at": "2026-10-02 08:00:00", "duration_min": 60}}
    with patch.object(server, "STATUS_DIR", tmp_path), \
         patch.object(server, "_read_expedition_records", return_value=record), \
         patch.object(expedition_observation, "load_observations", return_value={}):
        messages = list(server._build_dispatch(agent, "unused", {"team_no": 4,
            "map_code": "C2", "scheduled": True, "slot_key": "second"}))
    agent.expedition_stream.assert_not_called()
    assert "尚未确认" in messages[-1]
    assert json.loads((tmp_path / "dispatch_result.json").read_text(encoding="utf-8"))["outcome"] == "refused"


def test_next_shift_collects_then_departs(tmp_path):
    from panel import server
    agent = Mock()
    agent.maa.exists.return_value = True
    agent.collect_expedition_stream.return_value = iter(["收菜成功"])
    agent.expedition_stream.return_value = iter(["[远征] ✅ 部队4已出发"])
    record = {"4": {"map_code": "C1", "dispatched_at": "2026-10-02 08:00:00", "duration_min": 60}}
    with patch.object(server, "STATUS_DIR", tmp_path), \
         patch.object(server, "_read_expedition_records", side_effect=[record, {}]):
        messages = list(server._build_dispatch(agent, "unused", {"team_no": 4,
            "map_code": "C2", "scheduled": True, "slot_key": "second"}))
    assert messages == ["收菜成功", "[远征] ✅ 部队4已出发"]
    agent.expedition_stream.assert_called_once()


def test_dispatch_does_not_depart_after_preset_failure(tmp_path):
    from panel import server, expedition_advisor as ea
    from touken import custom_formations as cf
    agent = Mock()
    agent.maa.exists.return_value = True
    agent.collect_expedition_stream.return_value = iter(["收菜"])
    preset = {"formation_id": "f5", "formation_signature": "sig",
              "party": {"sum": 600, "max": 99, "names": ["药研藤四郎"]}, "record": {}}
    def fail(*args, **kwargs):
        yield "套预设失败"
        return False
    with patch.object(server, "STATUS_DIR", tmp_path), patch.object(server, "_read_expedition_records", return_value={}), \
         patch.object(ea, "expedition_formation_options", return_value=[preset]), \
         patch.object(cf, "apply_formation_preset_stream", side_effect=fail):
        list(server._build_dispatch(agent, "unused", {"team_no": 5, "map_code": "B1",
             "scheduled": True, "slot_key": "k5", "formation_id": "f5", "formation_signature": "sig"}))
    agent.expedition_stream.assert_not_called()
    assert json.loads((tmp_path / "dispatch_result.json").read_text(encoding="utf-8"))["outcome"] == "failed"


def test_dispatch_changed_preset_does_not_touch_formation(tmp_path):
    from panel import server, expedition_advisor as ea
    from touken import custom_formations as cf
    agent = Mock()
    agent.maa.exists.return_value = True
    agent.collect_expedition_stream.return_value = iter([])
    with patch.object(server, "STATUS_DIR", tmp_path), patch.object(server, "_read_expedition_records", return_value={}), \
         patch.object(ea, "expedition_formation_options", return_value=[]), \
         patch.object(cf, "apply_formation_preset_stream") as apply:
        list(server._build_dispatch(agent, "unused", {"team_no": 5, "map_code": "B1",
             "scheduled": True, "slot_key": "k5", "formation_id": "deleted", "formation_signature": "old"}))
    apply.assert_not_called()
    agent.expedition_stream.assert_not_called()
    assert json.loads((tmp_path / "dispatch_result.json").read_text(encoding="utf-8"))["outcome"] == "failed"


def test_dispatch_applies_preset_before_departure(tmp_path):
    from panel import server, expedition_advisor as ea
    from touken import custom_formations as cf
    agent = Mock()
    agent.maa.exists.return_value = True
    agent.collect_expedition_stream.return_value = iter(["collect"])
    agent.expedition_stream.return_value = iter(["[远征] ✅ 部队5已出发"])
    preset = {"formation_id": "f5", "formation_signature": "sig",
              "party": {"sum": 600, "max": 99, "names": ["药研藤四郎"]}, "record": {"target_team": 5}}
    def apply(*args):
        yield "preset applied"
        return True
    with patch.object(server, "STATUS_DIR", tmp_path), patch.object(server, "_read_expedition_records", return_value={}), \
         patch.object(ea, "expedition_formation_options", return_value=[preset]), \
         patch.object(cf, "apply_formation_preset_stream", side_effect=apply) as applying:
        messages = list(server._build_dispatch(agent, "unused", {"team_no": 5, "map_code": "B1",
             "scheduled": True, "slot_key": "k5", "formation_id": "f5", "formation_signature": "sig"}))
    assert messages == ["collect", "preset applied", "[远征] ✅ 部队5已出发"]
    applying.assert_called_once_with(agent, preset["record"])
    assert json.loads((tmp_path / "dispatch_result.json").read_text(encoding="utf-8"))["outcome"] == "done"


def test_timeline_dispatch_inherits_training_switch_and_injury(tmp_path):
    from panel import server
    from touken import expedition_sakura
    for enabled in (False, True):
        agent = Mock()
        agent.maa.exists.return_value = True
        agent.collect_expedition_stream.return_value = iter(['收菜成功'])
        agent.expedition_stream.return_value = iter(['[远征补花] ✓ 原队伍和装备已核对恢复', '[远征] ✅ 部队4已出发'])
        def recovered(_agent):
            if False: yield
            return True
        with patch.object(server, 'STATUS_DIR', tmp_path), \
             patch.object(server, '_read_expedition_records', return_value={}), \
             patch.object(s, 'load_config', return_value={'automation': {'sakura_before_dispatch': enabled}}), \
             patch.object(server, '_load_panel_settings', return_value={'params': {'sakura': {'repair_threshold': 'heavy'}}}), \
             patch.object(expedition_sakura, 'recover_stream', side_effect=recovered):
            messages = list(server._build_dispatch(agent, 'unused', {
                'team_no': 4, 'map_code': 'C2', 'scheduled': True, 'slot_key': 'today:adhoc:4:960'}))
        args = agent.expedition_stream.call_args.kwargs
        assert args['sakura_before_dispatch'] is enabled
        assert args['repair_threshold'] == 'heavy'
        assert args['team_no'] == 4
        assert ('这班先补花' in '\n'.join(messages)) is enabled
        assert json.loads((tmp_path / 'dispatch_result.json').read_text(encoding='utf-8'))['outcome'] == 'done'


def test_timeline_training_failure_is_terminal_not_departure(tmp_path):
    from panel import server
    from touken import expedition_sakura
    agent = Mock()
    agent.maa.exists.return_value = True
    agent.collect_expedition_stream.return_value = iter([])
    agent.expedition_stream.return_value = iter(['[远征补花] ✗ 补花未完成，本次暂不续派'])
    def recovered(_agent):
        if False: yield
        return True
    with patch.object(server, 'STATUS_DIR', tmp_path), \
         patch.object(server, '_read_expedition_records', return_value={}), \
         patch.object(s, 'load_config', return_value={'automation': {'sakura_before_dispatch': True}}), \
         patch.object(server, '_load_panel_settings', return_value={}), \
         patch.object(expedition_sakura, 'recover_stream', side_effect=recovered):
        list(server._build_dispatch(agent, 'unused', {
            'team_no': 4, 'map_code': 'C2', 'scheduled': True, 'slot_key': 'today:adhoc:4:960'}))
    result = json.loads((tmp_path / 'dispatch_result.json').read_text(encoding='utf-8'))
    assert result['outcome'] == 'failed'
    assert '补花未完成' in result['detail']
