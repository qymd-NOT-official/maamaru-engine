# -*- coding: utf-8 -*-
"""日课手入步：步骤顺序、零成本默认、位满/没伤刀如实跳过、翻车判分。

真机动作全部替身：repair_stream 用 Mock 隔离日课编排，
再用 DailyMixin+RepairLoopMixin 的合成宿主把真实手入循环跑一遍，
证明「配置里写了加速部队，日课手入也不烧符」。
"""
import unittest
from unittest.mock import Mock, patch

from touken.flows.daily import DailyMixin
from touken.flows.repair_loop import RepairLoopMixin
from touken.flows.report_judge import (
    _is_fail,
    _is_success_status,
    _repair_report_status,
)


class DailyHost(DailyMixin):
    """只喂日课手入步所需的最小宿主；其余流程一律 Mock。"""

    config = {}

    def __init__(self):
        self.flushed = []

    def set_progress(self, step):
        pass

    def _daily_update_gate(self):
        if False:
            yield None
        return True

    def _flush_report(self, report, finished):
        self.flushed.append((list(report), finished))
        return None

    def _repair_config(self):
        return {"blacklist": [], "speedup_teams": []}


class RepairJudgeTests(unittest.TestCase):
    """翻车词表：位满/没伤刀如实记成跳过，真翻车照旧判红。"""

    def test_slot_full_message_is_fail_but_detail_skips_green(self):
        msg = "[手入] 没有空闲修复槽（都在修？）或选人界面没打开，停"
        self.assertTrue(_is_fail(msg))
        status = _repair_report_status(msg)
        self.assertEqual(status, "✓ 手入位全占用，本步跳过")
        self.assertTrue(_is_success_status(status))

    def test_no_injured_swords_reported_as_skip(self):
        msg = "[手入] ✅ 完事：修了 0 把，黑名单跳过 0 把"
        status = _repair_report_status(msg)
        self.assertEqual(status, "✓ 没有伤刀要修，本步跳过")
        self.assertTrue(_is_success_status(status))

    def test_blacklist_only_skip_needs_no_detail_and_stays_green(self):
        msg = "[手入] ✅ 完事：修了 0 把，黑名单跳过 2 把"
        self.assertIsNone(_repair_report_status(msg))
        self.assertFalse(_is_fail(msg))

    def test_real_failures_stay_red(self):
        for msg in ("[手入] 到达修复失败",
                    "[手入] 修完后没能重新打开选人界面，停",
                    # 有刀身份对不上：「无法」在翻车词表里，不许洗绿
                    "[手入] ✗ 修了 0 把，另有 1 行无法确认身份，已跳过"):
            with self.subTest(msg=msg):
                self.assertTrue(_is_fail(msg))
                self.assertIsNone(_repair_report_status(msg))

    def test_repaired_message_needs_no_detail(self):
        msg = "[手入] ✅ 完事：修了 2 把，黑名单跳过 0 把"
        self.assertIsNone(_repair_report_status(msg))
        self.assertFalse(_is_fail(msg))


class DailyRepairStepTests(unittest.TestCase):
    def run_daily(self, host, only):
        with patch("touken.flows.daily.time.sleep"), \
             patch("touken.notify.notify_destination", return_value=""):
            return list(host.daily_stream(only=only))

    def test_step_runs_after_sortie_before_rewards(self):
        host = DailyHost()
        calls = []
        host.repair_stream = Mock(side_effect=lambda **kw: (
            calls.append(("repair", kw)) or
            iter(["[手入] ✅ 完事：修了 1 把，黑名单跳过 0 把"])))
        host.claim_task_rewards_stream = Mock(side_effect=lambda: (
            calls.append(("rewards",)) or iter(["[任务] ✓ 领完了"])))
        messages = self.run_daily(host, ["出阵", "手入", "任务奖励"])
        # 手入用零成本默认调用：一张加速符都不许勾
        self.assertEqual(calls[0], ("repair", {"use_speedup": False}))
        self.assertEqual([name for name, *_ in calls], ["repair", "rewards"])
        sortie_at = messages.index("========== ⑩ 出阵 ==========")
        repair_at = messages.index("========== ⑫ 手入（伤刀当天修，不烧加速符） ==========")
        rewards_at = messages.index("========== ⑬ 领任务奖励 ==========")
        self.assertLess(sortie_at, repair_at)
        self.assertLess(repair_at, rewards_at)
        self.assertIn(("手入", "✓"), host.flushed[-1][0])

    def test_unconfigured_repair_skips_honestly_without_calling_flow(self):
        host = DailyHost()
        host._repair_config = lambda: None
        host.repair_stream = Mock(return_value=iter(()))
        messages = self.run_daily(host, ["手入"])
        host.repair_stream.assert_not_called()
        skip = next(m for m in messages if "手入名单还没配置" in m)
        self.assertFalse(_is_fail(skip))
        self.assertEqual(host.flushed[-1][0], [("手入", "✓")])

    def test_full_slot_reported_as_green_skip(self):
        host = DailyHost()
        host.repair_stream = Mock(return_value=iter(
            ["[手入] 没有空闲修复槽（都在修？）或选人界面没打开，停"]))
        self.run_daily(host, ["手入"])
        self.assertEqual(host.flushed[-1][0],
                         [("手入", "✓ 手入位全占用，本步跳过")])

    def test_no_injured_swords_reported_as_green_skip(self):
        host = DailyHost()
        host.repair_stream = Mock(return_value=iter(
            ["[手入] ✅ 完事：修了 0 把，黑名单跳过 0 把"]))
        self.run_daily(host, ["手入"])
        self.assertEqual(host.flushed[-1][0],
                         [("手入", "✓ 没有伤刀要修，本步跳过")])

    def test_real_failure_reaches_report_card_red(self):
        host = DailyHost()
        host.repair_stream = Mock(return_value=iter(["[手入] 到达修复失败"]))
        self.run_daily(host, ["手入"])
        self.assertEqual(host.flushed[-1][0], [("手入", "✗")])

    def test_unresolved_identity_reaches_report_card_red(self):
        host = DailyHost()
        host.repair_stream = Mock(return_value=iter(
            ["[手入] ✗ 修了 0 把，另有 1 行无法确认身份，已跳过"]))
        self.run_daily(host, ["手入"])
        self.assertEqual(host.flushed[-1][0], [("手入", "✗")])

    def test_old_saved_steps_without_repair_do_not_run_it(self):
        # 存量用户：已保存的步骤清单（老面板存的，没有「手入」）原样传给
        # only=，不强行加手入，repair_stream 一次都不许被叫到。
        host = DailyHost()
        host.repair_stream = Mock(return_value=iter(()))
        host.claim_task_rewards_stream = Mock(return_value=iter(["[任务] ✓"]))
        self.run_daily(host, ["出阵", "任务奖励"])
        host.repair_stream.assert_not_called()


_NUMERAL = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}


def _row(name, prefix="一之一", y=200, injury=None):
    return {"prefix": prefix, "name": name, "y": y, "injury": injury}


def _fact(hp=20, hp_max=45, injury="中伤", team_no=1, serial=101, catalog=10):
    return {"serial_id": serial, "catalog_id": catalog, "hp": hp,
            "hp_max": hp_max, "injury": injury, "team_no": team_no,
            "memberships": [(team_no, "1")]}


class DailyRepairHost(DailyMixin, RepairLoopMixin):
    """真实手入循环 + 假画面钩子：验证日课默认值端到端不烧符。"""

    def __init__(self, rcfg, pages, facts, row_facts, lookup,
                 repair_results=()):
        self.rcfg = rcfg
        self.pages = list(pages)
        self.facts = facts
        self.row_facts = row_facts or {}
        self.lookup = lookup or {}
        self.repair_results = list(repair_results)
        self.repair_calls = []

    def _repair_config(self):
        return self.rcfg

    def _repair_sword_lookup(self, name):
        return self.lookup.get(name)

    def _repair_speedup_mark(self, team_no):
        numeral = _NUMERAL.get(team_no)
        return f"{numeral}之" if numeral else None

    def _repair_goto_room(self):
        yield "[手入] 正在导航到修复..."
        return True

    def _repair_refresh_membership(self, std_name):
        yield "[手入] 刷新编队状态"
        return True

    def _repair_open_select_screen(self):
        return True

    def _repair_scan_page(self):
        if self.pages:
            return self.pages.pop(0)
        return [], []

    def _repair_scroll_list(self):
        pass

    def _repair_game_facts(self):
        return self.facts or None

    def _repair_row_fact(self, row, facts):
        if facts is None:
            return None
        return self.row_facts.get((row["prefix"], row["name"]))

    def _repair_one(self, name_y, need_speed):
        self.repair_calls.append({"name_y": name_y, "need_speed": need_speed})
        result = self.repair_results.pop(0) if self.repair_results else True
        if result is True:
            yield "[手入] 已开工"
        return result

    def record_event(self, event_type, **payload):
        pass


class DailyRepairLoopIntegrationTests(unittest.TestCase):
    def test_daily_step_never_uses_speedup_even_for_speed_team(self):
        # 配置写着部队三修时勾加速符（单独手入的规矩），但日课手入步
        # 传 use_speedup=False：中伤的部队三也不烧符，只用空闲位慢修。
        host = DailyRepairHost(
            rcfg={"blacklist": ["碰瓷刀"], "speedup_teams": [3]},
            pages=[([_row("碰瓷刀", prefix="三之二"),
                     _row("五虎退", prefix="三之一")], [])],
            facts=["facts"],
            row_facts={
                ("三之一", "五虎退"): _fact(injury="中伤", team_no=3),
                ("三之二", "碰瓷刀"): _fact(injury="中伤", team_no=3,
                                            serial=102, catalog=99),
            },
            lookup={"五虎退": (10, "五虎退"), "碰瓷刀": (99, "碰瓷刀")})
        with patch("time.sleep"):
            messages = list(host._daily_repair_step())
        self.assertEqual(host.repair_calls,
                         [{"name_y": 200, "need_speed": False}])
        self.assertEqual(host.last_repair_stats["speedups"], 0)
        self.assertTrue(any("黑名单（碰瓷队带伤上班），跳过" in m
                            for m in messages))

    def test_blacklist_respected_in_daily_step(self):
        host = DailyRepairHost(
            rcfg={"blacklist": ["碰瓷刀"], "speedup_teams": []},
            pages=[([_row("碰瓷刀")], [])],
            facts=["facts"],
            row_facts={("一之一", "碰瓷刀"): _fact(catalog=99)},
            lookup={"碰瓷刀": (99, "碰瓷刀")})
        with patch("time.sleep"):
            list(host._daily_repair_step())
        self.assertEqual(host.repair_calls, [])
        self.assertEqual(host.last_repair_stats["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
