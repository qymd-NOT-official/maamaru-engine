import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from touken.telemetry import LEDGER_RESOURCES, TelemetryStore

# Asia/Shanghai 无夏令时，测试里用固定 +8 拼时间戳
SH = timezone(timedelta(hours=8))


def sh(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


class ResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TelemetryStore(Path(self.temp.name) / "telemetry.db")

    def test_receipt_keeps_recorded_workflow_name(self):
        self.store.start_run("named", "workflow", started_at=100, label="晚间收菜")
        self._event(110, "resource.change", {"resource": "小判", "delta": 500,
                    "source": "inbox", "label": "领取收信箱"}, run_id="named", script="workflow")
        ledger = self.store.resource_ledger(100, 120)
        self.assertEqual(ledger["attributions"][0]["run_label"], "晚间收菜")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _event(self, ts, event_type, payload, run_id="run-1", script="osaka"):
        cursor = self.store._conn().execute(
            "INSERT INTO events(ts, run_id, script, event_type, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, run_id, script, event_type, json.dumps(payload, ensure_ascii=False)))
        self.store._conn().commit()
        return cursor.lastrowid

    def _captured(self, ts, resources, phase=None, run_id="run-1", script="daily"):
        return self._event(ts, "inventory.captured",
                           {"captured_at": "", "phase": phase, "resources": resources,
                            "doko": None, "furnaces": []}, run_id=run_id, script=script)

    @staticmethod
    def _res(ledger, name):
        return next(r for r in ledger["per_resource"] if r["resource"] == name)

    @staticmethod
    def _day(ledger, date, resource):
        return next((d for d in ledger["daily_series"]
                     if d["date"] == date and d["resource"] == resource), None)

    def test_client_items_reserve_is_not_income_and_zero_is_known(self):
        t = sh("2026-09-30 10:00:00")
        self._captured(t, {"小判": 1000, "小判箱·小": 2, "小判箱·中": 1, "小判箱·大": 0,
                           "堆肥": 0, "暖心福袋": 3}, script="youzu_log")
        self._captured(t + 10, {"小判": 1100}, script="youzu_log")
        stock = self.store.client_item_inventory(t + 20)
        self.assertEqual(stock["koban_reserve"], 800)
        self.assertEqual(stock["items"]["堆肥"]["count"], 0)
        self.assertEqual(stock["items"]["暖心福袋"]["observed_at"], t)
        ledger = self.store.resource_ledger(t, t + 20)
        self.assertEqual(self._res(ledger, "小判")["total_delta"], 100)

    def test_missing_box_inventory_is_unknown(self):
        t = sh("2026-09-30 10:00:00")
        self._captured(t, {"小判箱·小": 2}, script="youzu_log")
        self.assertIsNone(self.store.client_item_inventory(t + 1)["koban_reserve"])

    def test_sparse_client_baselines_remain_per_resource(self):
        t = sh("2026-09-30 10:00:00")
        self._captured(t - 120, {"木炭": 100, "小判": 1000}, script="youzu_log")
        self._captured(t - 60, {"小判": 1100}, script="youzu_log")
        self._captured(t + 60, {"木炭": 120, "小判": 1150}, script="youzu_log")
        ledger = self.store.resource_ledger(t, t + 120)
        self.assertEqual(self._res(ledger, "木炭")["total_delta"], 20)
        self.assertEqual(self._res(ledger, "小判")["total_delta"], 50)

    def test_game_and_script_same_receipt_count_once_and_link_run(self):
        t = sh("2026-09-30 10:00:00")
        self._captured(t - 60, {"小判": 1000}, phase="before")
        script_id = self._event(t, "resource.change", {"resource": "小判", "delta": -500,
            "before": 1000, "after": 500, "source": "yosari.ticket_refill"}, script="yosari")
        game_id = self._event(t + 1, "resource.change", {"resource": "小判", "delta": -500,
            "before": 1000, "after": 500, "source": "youzu_log.sally/parallelpastrecovercost",
            "note": "sally/parallelpastrecovercost 小判 -500"}, run_id=None, script="youzu_log")
        self._captured(t + 60, {"小判": 500}, phase="after")
        ledger = self.store.resource_ledger(t - 60, t + 60)
        attrs = [a for a in ledger["attributions"] if a["resource"] == "小判"]
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0]["source"], "yosari.youzu_log.sally/parallelpastrecovercost")
        self.assertEqual(attrs[0]["run_id"], "run-1")
        self.assertEqual(attrs[0]["evidence_ids"], [game_id, script_id])
        self.assertFalse(any(g["reason"] == "conflicting_evidence" for g in ledger["gaps"]))
        self.assertEqual(self._res(ledger, "小判")["unattributed_delta"], 0)

    def test_game_receipt_alone_prevents_duplicate_run_net_delta(self):
        t = sh("2026-09-30 10:00:00")
        self._captured(t, {"木炭": 100}, phase="before")
        self._event(t + 10, "resource.change", {"resource": "木炭", "delta": 15,
            "before": 100, "after": 115, "source": "youzu_log.home/get_all_activity",
            "note": "远征完成·一队·A1 维新 木炭 +15"}, run_id=None, script="youzu_log")
        self._captured(t + 60, {"木炭": 115}, phase="after")
        ledger = self.store.resource_ledger(t, t + 60)
        self.assertEqual(len(ledger["attributions"]), 1)
        self.assertEqual(ledger["attributions"][0]["source"], "expedition.youzu_log.conquest/complete")

    def test_unknown_game_action_remains_unattributed(self):
        t = sh("2026-09-30 10:00:00")
        self._event(t, "resource.change", {"resource": "小判", "delta": -500,
            "before": 1000, "after": 500, "source": "youzu_log.unknown",
            "note": "来源待确认 小判 -500"}, run_id=None, script="youzu_log")
        ledger = self.store.resource_ledger(t - 1, t + 1)
        self.assertEqual(ledger["attributions"], [])
        self.assertEqual(ledger["unresolved_changes"][0]["label"], "来源待确认")
        self.assertEqual(self._res(ledger, "小判")["unattributed_delta"], -500)

    def test_contradicting_balances_do_not_merge_similar_amounts(self):
        t = sh("2026-09-30 10:00:00")
        for stamp, before, script, source in [(t, 1000, "yosari", "yosari.ticket_refill"),
                (t + 1, 500, "youzu_log", "youzu_log.sally/parallelpastrecovercost")]:
            self._event(stamp, "resource.change", {"resource": "小判", "delta": -500,
                "before": before, "after": before - 500, "source": source}, run_id=None, script=script)
        ledger = self.store.resource_ledger(t - 1, t + 2)
        self.assertEqual(len(ledger["attributions"]), 2)

    def test_koban_session_reconciles_against_captured(self):
        # 8/20 实例：captured 745056 → 挖地收场 788506（session 开工 745656）
        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 745056, "木炭": 5000}, phase="before", run_id="run-a")
        self._event(t0 + 600, "osaka.koban_session",
                    {"before": 745656, "after": 788506, "delta": 42850,
                     "floors": 10, "target_floor": 99}, run_id="run-b")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 3600)

        koban = self._res(ledger, "小判")
        self.assertEqual(koban["total_delta"], 43450)
        self.assertEqual(koban["attributed_delta"], 42850)
        self.assertEqual(koban["unattributed_delta"], 600)
        self.assertEqual(koban["observation_count"], 3)
        self.assertEqual(koban["confidence"], "high")

    def test_negative_unattributed_is_not_clamped(self):
        # confirmed +100、净变化 +10 → 未归因 -90，负残差保留符号
        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 1000}, phase="before", run_id="run-a")
        self._event(t0 + 600, "osaka.koban_session",
                    {"before": 1000, "after": 1100, "delta": 100,
                     "floors": 1, "target_floor": 99}, run_id="run-b")
        self._captured(t0 + 1200, {"小判": 1010}, phase="after", run_id="run-b")

        koban = self._res(self.store.resource_ledger(t0 - 60, t0 + 1800), "小判")

        self.assertEqual(koban["total_delta"], 10)
        self.assertEqual(koban["attributed_delta"], 100)
        self.assertEqual(koban["unattributed_delta"], -90)

    def test_confirmed_attribution_survives_missing_observations(self):
        # 有 confirmed 支出但加速符零观察：total 是 None 不是 0，明细仍在
        t0 = sh("2026-08-20 09:00:00")
        self._event(t0, "repair.session_completed",
                    {"repaired": 2, "speedups": 3}, script="repair")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 600)

        speedup = self._res(ledger, "加速符")
        self.assertIsNone(speedup["total_delta"])
        self.assertEqual(speedup["attributed_delta"], -3)
        self.assertIsNone(speedup["unattributed_delta"])
        self.assertEqual(speedup["confidence"], "low")
        attr = next(a for a in ledger["attributions"] if a["resource"] == "加速符")
        self.assertEqual(attr["delta"], -3)
        self.assertEqual(attr["confidence"], "confirmed")
        day = self._day(ledger, "2026-08-20", "加速符")
        self.assertIsNone(day["total_delta"])
        self.assertEqual(day["attributed_delta"], -3)

    def test_edocastle_ticket_refills_include_legacy_events(self):
        # v0.4.1 只记了补票事实，没带金额；当前江户城旧事件固定按 300/张补账。
        t0 = sh("2026-08-28 20:00:00")
        for offset in (0, 600, 1200, 1800):
            self._event(t0 + offset, "ticket.refilled", {"source": "江户城"},
                        run_id=f"run-{offset}", script="edocastle")
        # 新事件自身带金额；未知活动的无金额事件不能瞎猜。
        self._event(t0 + 2400, "ticket.refilled",
                    {"source": "江户城", "resource": "小判", "delta": -300,
                     "ticket_price": 300}, run_id="run-new", script="edocastle")
        linked_id = self._event(
            t0 + 2700, "ticket.refilled",
            {"source": "江户城", "resource": "小判", "delta": -300,
             "ticket_price": 300}, run_id="run-linked", script="edocastle")
        self._event(t0 + 2701, "resource.change",
                    {"resource": "小判", "delta": -300,
                     "source": "ticket.refilled", "source_event_id": linked_id,
                     "attribution": "confirmed", "evidence": "confirmed_refill_flow"},
                    run_id="run-linked", script="edocastle")
        self._event(t0 + 3000, "ticket.refilled", {"source": "RAID"},
                    run_id="run-raid", script="raid")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 3600)

        koban = self._res(ledger, "小判")
        self.assertIsNone(koban["total_delta"])
        self.assertEqual(koban["attributed_delta"], -1800)
        attrs = [a for a in ledger["attributions"] if a["resource"] == "小判"]
        self.assertEqual(len(attrs), 6)
        self.assertTrue(all(a["confidence"] == "confirmed" for a in attrs))

    def test_duplicate_task_reward_resource_is_not_falsely_attributed(self):
        # 加速符撞委托符模板的真机形态：同一 claimed 事件下冒出两笔委托符。
        # 奖励弹窗会合并同种资源，所以两笔都应视为类别冲突，不挑一笔猜。
        t0 = sh("2026-08-29 11:00:00")
        claimed_id = self._event(
            t0, "task_rewards.claimed", {"tab": "日常"}, script="daily")
        for delta in (4, 3):
            self._event(
                t0 + 1, "resource.change",
                {"resource": "委托符", "delta": delta,
                 "source": "task_rewards.reward_popup",
                 "source_event_id": claimed_id, "attribution": "confirmed"},
                script="daily")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 600)

        token = self._res(ledger, "委托符")
        self.assertEqual(token["attributed_delta"], 0)
        self.assertFalse(any(
            a["resource"] == "委托符" for a in ledger["attributions"]))

    def test_cross_midnight_run_booked_by_observation_day(self):
        # 跨日 run：收益按观察发生日记账，首日只有单点观察无法结账
        t0 = sh("2026-08-20 23:30:00")
        t1 = sh("2026-08-21 01:00:00")
        self._captured(t0, {"小判": 1000}, phase="before")
        self._captured(t1, {"小判": 1300}, phase="after")

        ledger = self.store.resource_ledger(t0 - 60, t1 + 60)

        first = self._day(ledger, "2026-08-20", "小判")
        second = self._day(ledger, "2026-08-21", "小判")
        self.assertIsNone(first["total_delta"])
        self.assertEqual(second["opening"], 1000)
        self.assertEqual(second["closing"], 1300)
        self.assertEqual(second["total_delta"], 300)
        # 窗口级仍按首末观察结总账
        self.assertEqual(self._res(ledger, "小判")["total_delta"], 300)

    def test_peek_never_touches_koban(self):
        # peek 只认顶栏五资源：即使脏 payload 带了小判也不许污染小判观察链
        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 1000, "木炭": 5000}, phase="before")
        self._event(t0 + 300, "inventory.peek",
                    {"tag": "raid", "木炭": 5100, "玉钢": 200, "冷却材": 300,
                     "砥石": 400, "甲州金": 50, "小判": 99999}, script="raid")
        self._captured(t0 + 600, {"小判": 1100, "木炭": 5150}, phase="after")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 900)

        koban = self._res(ledger, "小判")
        self.assertEqual(koban["total_delta"], 100)
        self.assertEqual(koban["observation_count"], 2)
        charcoal = self._res(ledger, "木炭")
        self.assertEqual(charcoal["total_delta"], 150)
        self.assertEqual(charcoal["observation_count"], 3)

    def test_duplicate_closing_value_is_deduped(self):
        # koban_session.after 与紧随的 captured 同值同刻：只算一次观察、一次收益
        t0 = sh("2026-08-20 09:00:00")
        t1 = t0 + 600
        self._captured(t0, {"小判": 745056}, phase="before", run_id="run-a")
        self._event(t1, "osaka.koban_session",
                    {"before": 745656, "after": 788506, "delta": 42850,
                     "floors": 10, "target_floor": 99}, run_id="run-b")
        self._captured(t1 + 2, {"小判": 788506}, phase="after", run_id="run-b")

        ledger = self.store.resource_ledger(t0 - 60, t1 + 900)

        koban = self._res(ledger, "小判")
        self.assertEqual(koban["observation_count"], 3)
        self.assertEqual(koban["total_delta"], 43450)
        self.assertEqual(koban["attributed_delta"], 42850)
        koban_attrs = [a for a in ledger["attributions"] if a["resource"] == "小判"]
        self.assertEqual(len(koban_attrs), 1)

    def test_resource_change_before_after_feeds_observation_chain(self):
        # 2026-08-23 事故：异去补充提灯的 resource.change 带了精确 before/after，
        # 却只算归因不进观察链 → 次日日榜 opening 跳过整条补充链，
        # -8000 支出漏成次日「不知道谁干的」。修后补充链必须接住日界。
        d1 = sh("2026-08-21 10:00:00")
        d2 = sh("2026-08-22 14:00:00")
        d3 = sh("2026-08-23 06:00:00")
        self._event(d1, "osaka.koban_session",
                    {"before": 900, "after": 1000, "delta": 100,
                     "floors": 2, "target_floor": 99}, run_id="run-d1")
        self._event(d2, "resource.change",
                    {"resource": "小判", "delta": -300, "before": 1050, "after": 750,
                     "source": "yosari.ticket_refill", "attribution": "confirmed",
                     "evidence": "purchase_preview_balances", "note": "异去归城提灯补充"},
                    run_id="run-d2", script="yosari")
        self._event(d3, "osaka.koban_session",
                    {"before": 750, "after": 850, "delta": 100,
                     "floors": 2, "target_floor": 99}, run_id="run-d3")

        ledger = self.store.resource_ledger(d1 - 60, d3 + 600)

        koban = self._res(ledger, "小判")
        self.assertEqual(koban["observation_count"], 6)
        self.assertEqual(koban["total_delta"], -50)
        self.assertEqual(koban["attributed_delta"], -100)
        self.assertEqual(koban["unattributed_delta"], 50)
        # 补充当天：opening 接窗前基线 1000，closing 是补充后余额 750，
        # 未归因只剩补充前真实多出来的 +50
        day2 = self._day(ledger, "2026-08-22", "小判")
        self.assertEqual(day2["opening"], 1000)
        self.assertEqual(day2["closing"], 750)
        self.assertEqual(day2["total_delta"], -250)
        self.assertEqual(day2["attributed_delta"], -300)
        self.assertEqual(day2["unattributed_delta"], 50)
        # 次日：opening 必须接补充链末尾 750，不再用前天的旧值
        day3 = self._day(ledger, "2026-08-23", "小判")
        self.assertEqual(day3["opening"], 750)
        self.assertEqual(day3["closing"], 850)
        self.assertEqual(day3["total_delta"], 100)
        self.assertEqual(day3["unattributed_delta"], 0)
        self.assertEqual(day3["confidence"], "high")

    def test_resource_change_baseline_scans_back_for_balances(self):
        # 窗前基线：resource.change 不是每条都带 before/after（锻刀等没有），
        # 基线要向前翻找最近一条带余额的，而不是抓到无余额的就放弃
        t0 = sh("2026-08-20 09:00:00")
        self._event(t0, "resource.change",
                    {"resource": "小判", "delta": -500, "before": 1000, "after": 500,
                     "source": "yosari.ticket_refill", "attribution": "confirmed"},
                    script="yosari")
        self._event(t0 + 10, "resource.change",
                    {"resource": "木炭", "delta": -700, "source": "forge.started",
                     "attribution": "confirmed"}, script="forge")
        self._captured(t0 + 3600, {"小判": 800}, phase="after", run_id="run-b")

        koban = self._res(self.store.resource_ledger(t0 + 1800, t0 + 7200), "小判")

        self.assertEqual(koban["opening"], 500)
        self.assertEqual(koban["closing"], 800)
        self.assertEqual(koban["total_delta"], 300)

    def test_resource_change_shadows_legacy_event(self):
        # 双写兼容：resource.change 用 source_event_id 指向旧事件，旧的那份不重复聚合
        t0 = sh("2026-08-20 09:00:00")
        legacy_id = self._event(t0, "osaka.koban_session",
                                {"before": 745656, "after": 788506, "delta": 42850,
                                 "floors": 10, "target_floor": 99})
        self._event(t0 + 1, "resource.change",
                    {"resource": "小判", "delta": 42850, "before": 745656,
                     "after": 788506, "source": "osaka.koban_session",
                     "source_event_id": legacy_id, "attribution": "confirmed",
                     "evidence": "direct_before_after"})

        ledger = self.store.resource_ledger(t0 - 60, t0 + 600)

        koban_attrs = [a for a in ledger["attributions"] if a["resource"] == "小判"]
        self.assertEqual(len(koban_attrs), 1)
        self.assertEqual(koban_attrs[0]["source"], "osaka.koban_session")
        self.assertNotEqual(koban_attrs[0]["event_id"], legacy_id)
        koban = self._res(ledger, "小判")
        self.assertEqual(koban["attributed_delta"], 42850)
        self.assertEqual(koban["unattributed_delta"], 0)

    def test_large_window_is_not_capped_by_recent_events_limit(self):
        # 30 天 1440 条 peek（超过 recent_events 的 1000 上限）仍完整聚合
        base = sh("2026-07-22 00:00:00")
        rows = [(base + i * 1800, "run-x", "raid", "inventory.peek",
                 json.dumps({"tag": "raid", "木炭": 1000 + i}, ensure_ascii=False))
                for i in range(1440)]
        self.store._conn().executemany(
            "INSERT INTO events(ts, run_id, script, event_type, payload) "
            "VALUES (?, ?, ?, ?, ?)", rows)
        self.store._conn().commit()

        ledger = self.store.resource_ledger(base - 10, base + 1440 * 1800 + 10)

        charcoal = self._res(ledger, "木炭")
        self.assertEqual(charcoal["observation_count"], 1440)
        self.assertEqual(charcoal["total_delta"], 1439)
        self.assertEqual(ledger["window"]["days"], 30.0)

    def test_shanghai_day_boundary(self):
        # UTC 16:00 = 上海次日 00:00：分桶按上海日期，不按 UTC
        t1 = datetime(2026, 8, 20, 15, 30, tzinfo=timezone.utc).timestamp()
        t2 = datetime(2026, 8, 20, 16, 30, tzinfo=timezone.utc).timestamp()
        self._captured(t1, {"小判": 100}, phase="before")
        self._captured(t2, {"小判": 200}, phase="after")

        ledger = self.store.resource_ledger(t1 - 60, t2 + 60)

        self.assertEqual(ledger["window"]["timezone"], "Asia/Shanghai")
        first = self._day(ledger, "2026-08-20", "小判")
        second = self._day(ledger, "2026-08-21", "小判")
        self.assertEqual(first["observation_count"], 1)
        self.assertIsNone(first["total_delta"])
        self.assertEqual(second["total_delta"], 100)

    def test_human_report_without_resource_is_archive_only(self):
        # 旧式无资源报备只留档：gap 照常列出，但不再波及任何资源的置信度
        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 1000}, phase="before")
        self._captured(t0 + 3600, {"小判": 1200}, phase="after")

        before_report = self.store.resource_ledger(t0 - 60, t0 + 7200)
        self.assertEqual(self._res(before_report, "小判")["confidence"], "medium")

        report = self.store.add_human_report(
            occurred_at=t0 + 1800, activities=["领邮箱", "手动打了一轮"])
        after_report = self.store.resource_ledger(t0 - 60, t0 + 7200)

        koban = self._res(after_report, "小判")
        self.assertEqual(koban["total_delta"], 200)
        self.assertEqual(koban["confidence"], "medium")
        gap = next(g for g in after_report["gaps"] if g["reason"] == "human_reported")
        self.assertEqual(gap["resources"], {})
        self.assertEqual(gap["human_report_ids"], [report["id"]])

    def test_cross_run_gap_links_human_report(self):
        # 跨 run 快照差值 = no_observation 缺口；报备按时间/gap_key 挂上，不改数值
        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 1000, "木炭": 500}, phase="after", run_id="run-a")
        self._captured(t0 + 3600, {"小判": 1200, "木炭": 480},
                       phase="before", run_id="run-b")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 7200)
        gap = next(g for g in ledger["gaps"] if g["reason"] == "no_observation")
        self.assertEqual(gap["resources"], {"小判": 200, "木炭": -20})
        self.assertEqual(gap["human_report_ids"], [])

        report = self.store.add_human_report(
            occurred_at=t0 + 1800, activities=["领了任务奖励"])
        ledger = self.store.resource_ledger(t0 - 60, t0 + 7200)
        gap = next(g for g in ledger["gaps"] if g["reason"] == "no_observation")
        self.assertEqual(gap["human_report_ids"], [report["id"]])
        self.assertEqual(self._res(ledger, "小判")["total_delta"], 200)
        self.assertEqual(self._res(ledger, "小判")["confidence"], "low")

    def test_gap_lowers_only_named_resources_confidence(self):
        # 缺口只波及小判时，木炭观察链完整（首末相等也算配对），不跟着躺枪；
        # 人工报备范围未知（resources 为空），仍把所有资源一起降到 low
        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 1000, "木炭": 500}, phase="after", run_id="run-a")
        self._captured(t0 + 3600, {"小判": 1200, "木炭": 500},
                       phase="before", run_id="run-b")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 7200)
        gap = next(g for g in ledger["gaps"] if g["reason"] == "no_observation")
        self.assertEqual(gap["resources"], {"小判": 200})
        self.assertEqual(self._res(ledger, "小判")["confidence"], "low")
        self.assertEqual(self._res(ledger, "木炭")["confidence"], "medium")
        day = self._day(ledger, "2026-08-20", "木炭")
        self.assertEqual(day["confidence"], "medium")
        self.assertEqual(day["gap_ids"], [])
        day = self._day(ledger, "2026-08-20", "小判")
        self.assertEqual(day["confidence"], "low")
        self.assertEqual(day["gap_ids"], [gap["id"]])

        # 报备时间落在缺口外 → 单独成条（旧式无资源报备只留档），不再波及任何资源
        self.store.add_human_report(occurred_at=t0 + 5400, activities=["手动出阵"])
        ledger = self.store.resource_ledger(t0 - 60, t0 + 7200)
        self.assertEqual(self._res(ledger, "木炭")["confidence"], "medium")
        self.assertTrue(any(g["reason"] == "human_reported" for g in ledger["gaps"]))

    def test_window_baseline_comes_from_before_window(self):
        # 窗前最近一次观察当 opening 基线：窗口内没有首观察也能结账
        t0 = sh("2026-08-19 09:00:00")
        t1 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 900}, phase="after", run_id="run-a")
        self._captured(t1, {"小判": 950}, phase="after", run_id="run-b")

        ledger = self.store.resource_ledger(t1 - 3600, t1 + 600)

        koban = self._res(ledger, "小判")
        self.assertEqual(koban["opening"], 900)
        self.assertEqual(koban["closing"], 950)
        self.assertEqual(koban["total_delta"], 50)

    def test_same_run_snapshot_residual_is_attributed(self):
        # 收杂物箱场景：run 自带 before/after 快照但没有逐笔记账，
        # 净差要认到这个 run 头上（inferred），不再整笔落进「不知道谁干的」
        t0 = sh("2026-08-26 18:50:00")
        self._captured(t0, {"小判": 891256, "木炭": 521000},
                       phase="before", run_id="run-inbox", script="inbox_supplies")
        self._captured(t0 + 180, {"小判": 1002256, "木炭": 521792},
                       phase="after", run_id="run-inbox", script="inbox_supplies")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 600)

        koban = self._res(ledger, "小判")
        self.assertEqual(koban["total_delta"], 111000)
        self.assertEqual(koban["attributed_delta"], 111000)
        self.assertEqual(koban["unattributed_delta"], 0)
        attr = next(a for a in ledger["attributions"] if a["resource"] == "小判")
        self.assertEqual(attr["source"], "inventory.run_delta")
        self.assertEqual(attr["confidence"], "inferred")
        self.assertEqual(attr["run_id"], "run-inbox")
        # inferred 不把置信度顶到 high：快照净差是推断不是逐笔实证
        self.assertEqual(koban["confidence"], "medium")
        day = self._day(ledger, "2026-08-26", "小判")
        self.assertEqual(day["unattributed_delta"], 0)
        # 同 run 前后差也不算缺口
        self.assertEqual(ledger["gaps"], [])

    def test_same_run_residual_netts_itemized_changes(self):
        # run 内有逐笔记账时残差只认差额：快照 +1300，远征已记 +1000 → 残差 +300
        t0 = sh("2026-08-26 09:00:00")
        self._captured(t0, {"小判": 5000}, phase="before", run_id="run-exp")
        self._event(t0 + 60, "resource.change",
                    {"resource": "小判", "delta": 1000, "source": "expedition.great_success",
                     "attribution": "confirmed", "note": "远征大成功"},
                    run_id="run-exp", script="expedition")
        self._captured(t0 + 600, {"小判": 6300}, phase="after", run_id="run-exp")

        ledger = self.store.resource_ledger(t0 - 60, t0 + 900)

        koban_attrs = [a for a in ledger["attributions"] if a["resource"] == "小判"]
        self.assertEqual(sorted(a["delta"] for a in koban_attrs), [300, 1000])
        residual = next(a for a in koban_attrs if a["source"] == "inventory.run_delta")
        self.assertEqual(residual["delta"], 300)
        self.assertEqual(self._res(ledger, "小判")["unattributed_delta"], 0)

    def test_claim_isolates_confidence_to_named_resource(self):
        # 带 resource+claimed_delta 的认领只波及点名资源，其他资源不背锅
        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 1000, "木炭": 500}, phase="before")
        self._captured(t0 + 3600, {"小判": 1200, "木炭": 700}, phase="after")

        report = self.store.add_human_report(
            occurred_at=t0 + 1800, activities=["领邮箱"],
            resource="小判", claimed_delta=200)
        self.assertEqual(report["resource"], "小判")
        self.assertEqual(report["claimed_delta"], 200)

        ledger = self.store.resource_ledger(t0 - 60, t0 + 7200)
        gap = next(g for g in ledger["gaps"] if g["reason"] == "human_reported")
        self.assertEqual(gap["resources"], {"小判": 200})
        self.assertEqual(self._res(ledger, "小判")["confidence"], "low")
        self.assertEqual(self._res(ledger, "木炭")["confidence"], "medium")
        # 认领不改写库存总变化，也不产生归因
        self.assertEqual(self._res(ledger, "小判")["total_delta"], 200)
        self.assertEqual(self._res(ledger, "小判")["attributed_delta"], 200)  # 残差推断
        self.assertFalse(any(a["source"] != "inventory.run_delta"
                             for a in ledger["attributions"]))

    def test_claim_roundtrip_via_store(self):
        item = self.store.add_human_report(
            occurred_at=sh("2026-08-20 09:00:00"), activities=["万屋购买"],
            resource="小判", claimed_delta=-4000)
        found = next(r for r in self.store.human_reports() if r["id"] == item["id"])
        self.assertEqual(found["resource"], "小判")
        self.assertEqual(found["claimed_delta"], -4000)
        # 旧式报备两个字段保持 None
        legacy = self.store.add_human_report(
            occurred_at=sh("2026-08-20 10:00:00"), activities=["记不清了"])
        found_legacy = next(r for r in self.store.human_reports()
                            if r["id"] == legacy["id"])
        self.assertIsNone(found_legacy["resource"])
        self.assertIsNone(found_legacy["claimed_delta"])

    def test_multi_resource_claim_group_roundtrip_and_delete(self):
        items = self.store.add_human_report_group(
            occurred_at=sh("2026-08-20 09:00:00"), activities=["领邮箱"],
            note="一起领的", entries={"小判": 300, "木炭": 500, "加速符": 2})
        self.assertEqual(len(items), 3)
        self.assertEqual(len({item["group_id"] for item in items}), 1)
        rows = [row for row in self.store.human_reports()
                if row["group_id"] == items[0]["group_id"]]
        self.assertEqual({row["resource"]: row["claimed_delta"] for row in rows},
                         {"小判": 300, "木炭": 500, "加速符": 2})
        self.assertTrue(self.store.delete_human_report_group(items[0]["group_id"]))
        self.assertEqual(self.store.human_reports(), [])
        with self.assertRaises(ValueError):
            self.store.add_human_report_group(
                occurred_at=sh("2026-08-20 10:00:00"), activities=["领邮箱"],
                entries={"小判": 100, "元宝": 1})
        self.assertEqual(self.store.human_reports(), [])

    def test_manual_claim_group_can_be_corrected_atomically(self):
        items = self.store.add_human_report_group(
            occurred_at=sh("2026-08-20 09:00:00"), activities=["领邮箱"],
            note="第一次", entries={"小判": 300, "木炭": 500})
        group_id = items[0]["group_id"]

        updated = self.store.update_human_report_group(
            group_id, occurred_at=sh("2026-08-20 10:00:00"),
            activities=["手动领奖"], note="改好了",
            entries={"小判": 200, "加速符": 2})
        self.assertEqual({item["resource"]: item["claimed_delta"] for item in updated},
                         {"小判": 200, "加速符": 2})
        rows = [row for row in self.store.human_reports()
                if row["group_id"] == group_id]
        self.assertEqual({row["resource"] for row in rows}, {"小判", "加速符"})
        self.assertTrue(all(row["activities"] == ["手动领奖"] for row in rows))
        self.assertTrue(all(row["note"] == "改好了" for row in rows))

        with self.assertRaises(ValueError):
            self.store.update_human_report_group(
                group_id, occurred_at=sh("2026-08-20 11:00:00"),
                activities=[], entries={"元宝": 1})
        rows_after_failure = [row for row in self.store.human_reports()
                              if row["group_id"] == group_id]
        self.assertEqual({row["resource"] for row in rows_after_failure},
                         {"小判", "加速符"})

    def test_claim_validation_rejects_bad_input(self):
        t0 = sh("2026-08-20 09:00:00")
        bad_calls = [
            dict(resource="小判"),                          # 缺数额
            dict(claimed_delta=100),                      # 缺资源
            dict(resource="元宝", claimed_delta=100),     # 资源不在白名单
            dict(resource="小判", claimed_delta=0),       # 零数额没意义
            dict(resource="小判", claimed_delta=float("nan")),
            dict(resource="小判", claimed_delta=float("inf")),
            dict(resource="小判", claimed_delta=True),    # bool 不是数额
            dict(resource="小判", claimed_delta="一百"),  # 字符串不是数额
            dict(resource="小判", claimed_delta=1.5),     # 资源数不能是小数
            dict(resource="小判", claimed_delta=2 ** 63), # SQLite 整数边界
            dict(gap_key="1:2", resource="小判", claimed_delta=100),  # 缺口报备不许自带数额
        ]
        for kwargs in bad_calls:
            with self.assertRaises(ValueError, msg=str(kwargs)):
                self.store.add_human_report(
                    occurred_at=t0, activities=["其他操作"], **kwargs)
        self.assertEqual(self.store.human_reports(), [])

    def test_claim_is_not_linked_to_other_resource_gap(self):
        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 1000, "木炭": 500}, phase="after", run_id="r0")
        self._captured(t0 + 3600, {"小判": 1000, "木炭": 700}, phase="before", run_id="r1")
        report = self.store.add_human_report(
            occurred_at=t0 + 1800, activities=["领邮箱"],
            resource="小判", claimed_delta=100)

        ledger = self.store.resource_ledger(t0 - 60, t0 + 7200)
        stock_gap = next(g for g in ledger["gaps"] if g["reason"] == "no_observation")
        claim_gap = next(g for g in ledger["gaps"] if g["reason"] == "human_reported")
        self.assertNotIn(report["id"], stock_gap["human_report_ids"])
        self.assertEqual(claim_gap["resources"], {"小判": 100})
        self.assertEqual(self._res(ledger, "小判")["confidence"], "low")

    def test_old_database_migrates_in_place(self):
        # 旧库没有 resource/claimed_delta 列：原地补列，旧记录原样保留
        self.store.close()
        db_path = Path(self.temp.name) / "telemetry.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE human_reports")
        conn.execute(
            "CREATE TABLE human_reports ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, "
            "occurred_at REAL NOT NULL, source TEXT NOT NULL, gap_key TEXT, "
            "activities TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')")
        conn.execute(
            "INSERT INTO human_reports(created_at, occurred_at, source, gap_key, "
            "activities, note) VALUES (1, 1700000000, 'proactive', NULL, '[\"领邮箱\"]', '')")
        conn.commit()
        conn.close()

        store = TelemetryStore(db_path)
        rows = store.human_reports()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["activities"], ["领邮箱"])
        self.assertIsNone(rows[0]["resource"])
        self.assertIsNone(rows[0]["claimed_delta"])
        self.assertIsNone(rows[0]["group_id"])
        # 迁移后新认领正常工作
        item = store.add_human_report(
            occurred_at=1700000100, activities=["领邮箱"],
            resource="小判", claimed_delta=500)
        self.assertEqual(item["claimed_delta"], 500)
        store.close()
        self.store = TelemetryStore(db_path)  # tearDown 里统一关

    def test_api_endpoint_aggregates_server_side(self):
        from panel.server import api_data_resource_ledger

        t0 = sh("2026-08-20 09:00:00")
        self._captured(t0, {"小判": 1000}, phase="before")
        self._captured(t0 + 600, {"小判": 1100}, phase="after")

        with patch("touken.telemetry._store", self.store):
            by_from_to = asyncio.run(
                api_data_resource_ledger(days=7, from_ts=t0 - 60, to=t0 + 900))
            by_days = asyncio.run(api_data_resource_ledger(days=7, from_ts=None, to=None))

        self.assertEqual(by_from_to["schema_version"], 3)
        self.assertEqual(by_from_to["window"]["from"], t0 - 60)
        self.assertEqual(by_from_to["window"]["to"], t0 + 900)
        self.assertEqual([r["resource"] for r in by_from_to["per_resource"]],
                         list(LEDGER_RESOURCES))
        self.assertEqual(self._res(by_from_to, "小判")["total_delta"], 100)
        # days 分支：窗口 7 天、八资源齐全（空窗期也返回完整结构）
        self.assertEqual(by_days["window"]["days"], 7.0)
        self.assertEqual(len(by_days["per_resource"]), 8)

    def test_balance_series_collects_captured_readings(self):
        t0 = sh("2026-10-01 08:00:00")
        t1 = sh("2026-10-02 08:00:00")
        t2 = sh("2026-10-02 20:00:00")
        self._captured(t0, {"小判": 1000, "木炭": 500}, script="youzu_log")
        self._captured(t1, {"小判": 800, "木炭": 700, "玉钢": 60}, script="youzu_log")
        self._captured(t2, {"小判": 900}, script="youzu_log")

        ledger = self.store.resource_ledger(t0 - 60, t2 + 60)
        points = ledger["balance_series"]
        self.assertEqual([p["ts"] for p in points], [t0, t1, t2])
        self.assertEqual(points[0]["date"], "2026-10-01")
        self.assertEqual(points[1]["date"], "2026-10-02")
        self.assertEqual(points[0]["values"], {"小判": 1000, "木炭": 500})
        # 缺的资源不补零，前端画成断点
        self.assertEqual(points[2]["values"], {"小判": 900})

    def test_balance_series_merges_sources_and_keeps_latest_at_same_ts(self):
        t0 = sh("2026-10-03 09:00:00")
        # 大阪城同刻 before/after：留 after；同刻 peek 的顶栏资源并进同一个点
        self._event(t0, "osaka.koban_session", {"before": 5000, "after": 5600})
        self._event(t0, "inventory.peek", {"木炭": 100, "玉钢": 200})

        ledger = self.store.resource_ledger(t0 - 60, t0 + 60)
        points = ledger["balance_series"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["values"],
                         {"小判": 5600, "木炭": 100, "玉钢": 200})

    def test_balance_series_empty_without_readings(self):
        ledger = self.store.resource_ledger(1000, 2000)
        self.assertEqual(ledger["balance_series"], [])

    def test_api_claim_roundtrip_and_validation(self):
        from panel.server import api_add_human_report, api_human_reports

        class _Req:
            def __init__(self, body):
                self._body = body

            async def json(self):
                return self._body

        t0 = sh("2026-08-20 09:00:00")
        with patch("touken.telemetry._store", self.store):
            ok = asyncio.run(api_add_human_report(_Req({
                "occurred_at": t0, "activities": ["领邮箱"],
                "resource": "小判", "claimed_delta": 111000})))
            self.assertTrue(ok["ok"])
            self.assertEqual(ok["item"]["resource"], "小判")
            self.assertEqual(ok["item"]["claimed_delta"], 111000)
            precise_only = asyncio.run(api_add_human_report(_Req({
                "occurred_at": t0 + 1, "resource": "加速符", "claimed_delta": -2})))
            self.assertTrue(precise_only["ok"])
            bad = asyncio.run(api_add_human_report(_Req({
                "occurred_at": t0, "activities": ["领邮箱"],
                "resource": "元宝", "claimed_delta": 100})))
            self.assertEqual(bad.status_code, 400)
            half = asyncio.run(api_add_human_report(_Req({
                "occurred_at": t0, "activities": ["领邮箱"],
                "claimed_delta": 100})))
            self.assertEqual(half.status_code, 400)
            listing = asyncio.run(api_human_reports(limit=10))

        items = listing["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["resource"], "加速符")
        self.assertEqual(items[0]["claimed_delta"], -2)
        self.assertEqual(items[1]["resource"], "小判")
        self.assertEqual(items[1]["claimed_delta"], 111000)

    def test_api_multi_resource_claim_group(self):
        from panel.server import (api_add_human_report_batch,
                                  api_delete_human_report_group,
                                  api_human_reports)

        class _Req:
            async def json(self):
                return {"occurred_at": sh("2026-08-20 09:00:00"),
                        "activities": ["领邮箱"], "note": "邮箱一锅端",
                        "entries": {"小判": 200, "玉钢": 450, "加速符": 3}}

        with patch("touken.telemetry._store", self.store):
            created = asyncio.run(api_add_human_report_batch(_Req()))
            self.assertTrue(created["ok"])
            self.assertEqual(len(created["items"]), 3)
            listing = asyncio.run(api_human_reports(limit=10))
            self.assertEqual({row["resource"] for row in listing["items"]},
                             {"小判", "玉钢", "加速符"})
            deleted = asyncio.run(api_delete_human_report_group(created["group_id"]))
            self.assertTrue(deleted["ok"])
            self.assertEqual(asyncio.run(api_human_reports(limit=10))["items"], [])


if __name__ == "__main__":
    unittest.main()
