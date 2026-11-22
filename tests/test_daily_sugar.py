# -*- coding: utf-8 -*-
"""日课炼糖步：步骤顺序（手入后、任务奖励前）、空邮箱/没可喂如实报绿、
真翻车判红、老用户已保存步骤清单不被强塞。

真机动作全部替身：sugar_stream 用 Mock 隔离日课编排，再用真实 sugar_stream
+ 脚本化收件/习合循环证明「没狗粮就跳过且如实报绿」。
"""
import unittest
from unittest.mock import Mock, patch

from touken.flows.daily import DailyMixin
from touken.flows.sugar import SugarMixin
from touken.flows.report_judge import (
    _is_fail,
    _is_success_status,
    _sugar_report_status,
)


class DailyHost(DailyMixin):
    """只喂日课炼糖步所需的最小宿主；其余流程一律 Mock。"""

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


class SugarJudgeTests(unittest.TestCase):
    """翻车词表专项：空邮箱/没可喂判绿跳过，停滞/被拒如实标出。"""

    def test_empty_inbox_is_green(self):
        status = _sugar_report_status("[炼糖] 邮件里没刀了，收工")
        self.assertEqual(status, "✓ 邮箱已清空")
        self.assertTrue(_is_success_status(status))
        self.assertFalse(_is_fail("[炼糖] 邮件里没刀了，收工"))

    def test_stalled_exit_is_not_green(self):
        msg = "[炼糖] 持续没有进展：连续 3 圈未完成习合，停止操作，邮箱未确认清完"
        status = _sugar_report_status(msg)
        self.assertFalse(_is_success_status(status))
        self.assertTrue(_is_fail(msg))


class DailySugarStepTests(unittest.TestCase):
    def run_daily(self, host, only):
        with patch("touken.flows.daily.time.sleep"), \
             patch("touken.notify.notify_destination", return_value=""):
            return list(host.daily_stream(only=only))

    def test_step_runs_after_repair_before_rewards(self):
        host = DailyHost()
        calls = []
        host.repair_stream = Mock(side_effect=lambda **kw: (
            calls.append(("repair",)) or
            iter(["[手入] ✅ 完事：修了 1 把，黑名单跳过 0 把"])))
        host.sugar_stream = Mock(side_effect=lambda: (
            calls.append(("sugar",)) or iter(["[炼糖] 完成第 1 轮习合"])))
        host.claim_task_rewards_stream = Mock(side_effect=lambda: (
            calls.append(("rewards",)) or iter(["[任务] ✓ 领完了"])))
        messages = self.run_daily(host, ["手入", "炼糖", "任务奖励"])
        self.assertEqual([name for name, *_ in calls],
                         ["repair", "sugar", "rewards"])
        repair_at = messages.index(
            "========== ⑫ 手入（伤刀当天修，不烧加速符） ==========")
        sugar_at = messages.index(
            "========== ⑬ 炼糖（收件箱清狗粮 + 习合喂重刀） ==========")
        rewards_at = messages.index("========== ⑭ 领任务奖励 ==========")
        self.assertLess(repair_at, sugar_at)
        self.assertLess(sugar_at, rewards_at)
        self.assertIn(("炼糖", "✓"), host.flushed[-1][0])

    def test_empty_inbox_reported_as_green_skip(self):
        host = DailyHost()
        host.sugar_stream = Mock(return_value=iter(
            ["[炼糖] 邮件里没刀了，收工"]))
        self.run_daily(host, ["炼糖"])
        self.assertEqual(host.flushed[-1][0], [("炼糖", "✓ 邮箱已清空")])

    def test_no_feedable_reported_as_green_skip(self):
        host = DailyHost()
        host.sugar_stream = Mock(return_value=iter(
            ["[炼糖·习合] 本轮没有可继续喂的本体"
             "（未锁乱舞七级及以上已跳过）",
             "[炼糖] 邮件里没刀了，收工"]))
        self.run_daily(host, ["炼糖"])
        status = host.flushed[-1][0][0][1]
        self.assertTrue(_is_success_status(status))

    def test_stalled_reaches_report_card_not_green(self):
        host = DailyHost()
        host.sugar_stream = Mock(return_value=iter(
            ["[炼糖] 持续没有进展：连续 3 圈未完成习合，停止操作，"
             "邮箱未确认清完"]))
        self.run_daily(host, ["炼糖"])
        self.assertEqual(host.flushed[-1][0],
                         [("炼糖", "⚠ 炼糖停滞：未确认清完，请检查游戏画面")])

    def test_flow_abort_reaches_report_card_red(self):
        from touken.flow_control import FlowAborted
        host = DailyHost()
        host.sugar_stream = Mock(
            side_effect=FlowAborted("炼糖：到达强化失败，停止操作"))
        messages = self.run_daily(host, ["炼糖"])
        self.assertEqual(host.flushed[-1][0], [("炼糖", "✗")])
        self.assertTrue(any("炼糖翻车" in m for m in messages))

    def test_old_saved_steps_without_sugar_do_not_run_it(self):
        # 存量用户：已保存的步骤清单（老面板存的，没有「炼糖」）原样传给
        # only=，不强行加炼糖，sugar_stream 一次都不许被叫到。
        host = DailyHost()
        host.sugar_stream = Mock(return_value=iter(()))
        host.claim_task_rewards_stream = Mock(return_value=iter(["[任务] ✓"]))
        self.run_daily(host, ["出阵", "任务奖励"])
        host.sugar_stream.assert_not_called()


class ScriptedSugarHost(DailyMixin, SugarMixin):
    """真实 sugar_stream + 脚本化收件/习合循环：验证日课炼糖步端到端
    走完「邮箱空了」的如实绿灯，全程不碰真机。"""

    config = {}

    def __init__(self, inbox_results):
        self._inbox_results = list(inbox_results)

    def _inbox_claim_stream(self, dry_run, filter_mode="swords"):
        yield from ()
        return self._inbox_results.pop(0) if self._inbox_results else "empty"

    def _shugo_loop_stream(self, dry_run, *, skip_unlocked_seven=False):
        # 日课炼糖必须沿用玩法的七级跳过逻辑（上锁保护不加新参数）
        assert skip_unlocked_seven is True
        yield from ()
        return 0


class DailySugarLoopIntegrationTests(unittest.TestCase):
    def test_empty_inbox_end_to_end_green(self):
        host = ScriptedSugarHost(inbox_results=["empty"])
        messages = list(host._daily_sugar_step())
        self.assertIn("[炼糖] 邮件里没刀了，收工", messages)
        self.assertFalse(any(_is_fail(m) for m in messages))

    def test_claimed_but_nothing_feedable_still_ends_green(self):
        # 收件箱领过一刀（claimed）但一圈习合都没喂成：下一圈收件转 empty，
        # 最终落在「邮件里没刀了」的如实绿灯上，不算翻车。
        host = ScriptedSugarHost(inbox_results=["claimed", "empty"])
        messages = list(host._daily_sugar_step())
        self.assertIn("[炼糖] 邮件里没刀了，收工", messages)
        self.assertEqual(_sugar_report_status(messages[-1]), "✓ 邮箱已清空")


if __name__ == "__main__":
    unittest.main()
