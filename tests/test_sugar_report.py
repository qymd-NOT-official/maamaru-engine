import unittest

from touken.flows.report_judge import (
    _is_fail,
    _is_success_status,
    _sugar_report_status,
)
from touken.flows.sugar import SugarMixin


class SugarReportStatusTests(unittest.TestCase):
    def test_real_empty_inbox_is_green(self):
        status = _sugar_report_status("[炼糖] 邮件里没刀了，收工")
        self.assertEqual(status, "✓ 邮箱已清空")
        self.assertTrue(_is_success_status(status))

    def test_nothing_feedable_is_green(self):
        status = _sugar_report_status("[炼糖] 领到的刀都没重刀可喂，收工")
        self.assertTrue(_is_success_status(status))

    def test_cap_exit_is_not_green(self):
        msg = ("[炼糖] 到安全上限 10 圈收工：邮箱还压着邮件，刀位一直满员是瓶颈，"
               "这次只消化了 20 轮；先去刀解腾位置，或者有空多跑几趟")
        status = _sugar_report_status(msg)
        self.assertEqual(status, "⚠ 邮箱没清完：刀位太满，先刀解腾位置再多跑几趟")
        self.assertFalse(_is_success_status(status))
        # 上限收工本身不算翻车词，红绿由专项判分接管
        self.assertFalse(_is_fail(msg))

    def test_blocked_without_fodder_is_not_green(self):
        msg = "[炼糖] 所持满了领不动、也没重刀可喂腾位置，收工（去刀解或氪刀位吧）"
        status = _sugar_report_status(msg)
        self.assertFalse(_is_success_status(status))


class ScriptedFlow(SugarMixin):
    def __init__(self, feeds):
        self.feeds = iter(feeds)
        self.rounds = 0

    def _inbox_claim_stream(self, dry_run):
        yield from ()
        return "blocked"

    def _shugo_loop_stream(self, dry_run, *, skip_unlocked_seven=False):
        yield from ()
        self.rounds += 1
        return next(self.feeds)


class SugarStreamTests(unittest.TestCase):
    def test_progress_runs_past_ten_rounds(self):
        flow = ScriptedFlow([2] * 15 + [0] * 3)
        messages = list(flow.sugar_stream())
        self.assertEqual(flow.rounds, 18)
        self.assertIn("持续没有进展", messages[-1])
        self.assertFalse(_is_success_status(_sugar_report_status(messages[-1])))

    def test_success_resets_stall_counter(self):
        flow = ScriptedFlow([0, 0, 1, 0, 0, 1, 0, 0, 0])
        list(flow.sugar_stream())
        self.assertEqual(flow.rounds, 9)
