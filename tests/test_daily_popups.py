"""登录启动期的界面识别与扫地分支（issue#7 用户现场）。

- 游戏开着但停在签到页/公告时，_ensure_game_started 以前当成"没开"，
  去桌面找图标找不到就误判「游戏没有启动」——现在认出来直接接管
- 扫地新增：签到领奖（一轮只领一次，防灰按钮死循环）、关道具详情窗、
  「修行启程」申请窗点【取消】婉拒（可逆；确认才烧道具送刀 96 小时，
  不替主人决定）
"""
import unittest
from unittest.mock import patch

from touken.flows.daily import DailyMixin
from touken.maa_adapter import Point


def _gen_result(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


class _SweepMaa:
    """按 stage 出字：signin=启动签到日历，item_detail=道具详情窗，
    training_req=修行申请弹窗，home=本丸"""

    def __init__(self, stage):
        self.stage = stage
        self.clicks = []

    def screenshot(self, force=False):
        pass

    def exists(self, template, roi=None, threshold=0.7):
        return template == "目录.png" and self.stage in {
            "home", "training_letter_notice", "training_letter_after",
            "training_letter_archive",
        }

    def template_match(self, template, roi=None, threshold=0.7):
        return None

    def ocr(self, expected, roi, match_mode="exact"):
        if self.stage == "signin" and expected == "领取奖励":
            return Point(900, 600)
        if self.stage == "item_detail" and expected == "道具详情":
            return Point(640, 110)
        if self.stage == "training_req":
            if expected == "修行启程":
                return Point(640, 75)
            if expected == "取消" and match_mode == "exact":
                return Point(496, 614)
        if (self.stage == "training_letter_page" and expected == "致主人"
                and match_mode == "exact"):
            return Point(155, 130)
        if self.stage in {"training_letter_notice", "training_letter_after",
                          "training_letter_archive"} and expected == "书信":
            return Point(580, 490)
        return None

    def ocr_all(self, roi):
        return []

    def click(self, pt):
        pos = (pt.x, pt.y)
        self.clicks.append(pos)
        if self.stage == "signin" and pos == (900, 600):
            self.stage = "home"
        elif self.stage == "item_detail" and pos == (945, 105):
            self.stage = "home"
        elif self.stage == "training_req" and pos == (496, 614):
            self.stage = "home"  # 婉拒后回本丸
        elif pos == (993, 690):
            self.stage = {
                "training_letter_notice": "training_letter_page",
                "training_letter_page": "training_letter_after",
                "training_letter_after": "training_letter_archive",
                "training_letter_archive": "home",
            }.get(self.stage, self.stage)
        return True


class _SweepFlow(DailyMixin):
    def __init__(self, stage):
        self.maa = _SweepMaa(stage)

    # 生产上由 LoginMixin / Naihanka 提供；扫地测试默认没有续打弹窗
    def _network_resume_visible(self):
        return False

    def _collect_report_gains(self):
        return []

    def _probe_nav_ready(self):
        return self.maa.stage == "home"


class EnsureGameStartedTests(unittest.TestCase):
    def test_signin_page_is_taken_over_instead_of_hunting_desktop_icon(self):
        flow = _SweepFlow("signin")
        flow._launch_game_via_adb = lambda: (_ for _ in ()).throw(
            AssertionError("游戏明明开着，不该去启动"))

        messages = list(flow._ensure_game_started())
        # 生成器已耗尽即返回 True（return 在 yield 分支里）
        self.assertTrue(any("签到页" in m for m in messages))

    def test_announcement_popup_counts_as_game_running(self):
        class Maa(_SweepMaa):
            def template_match(self, template, roi=None, threshold=0.7):
                if template == "通用_关闭.png":
                    return Point(1200, 30)
                return None

        flow = _SweepFlow("nowhere")
        flow.maa = Maa("nowhere")
        flow._launch_game_via_adb = lambda: (_ for _ in ()).throw(
            AssertionError("游戏明明开着，不该去启动"))

        messages = list(flow._ensure_game_started())
        self.assertTrue(any("公告/弹窗" in m for m in messages))

    def test_unknown_screen_still_falls_back_to_launch(self):
        flow = _SweepFlow("nowhere")
        flow._launch_game_via_adb = lambda: False  # ADB 也不通

        result = _gen_result(flow._ensure_game_started())
        self.assertFalse(result)  # 找不到图标，安全报没启动


class PopupSweepNewBranchTests(unittest.TestCase):
    def test_closing_clears_returns_without_second_home_navigation(self):
        flow = _SweepFlow("training_letter_notice")
        flow._probe_nav_ready = unittest.mock.Mock(side_effect=AssertionError("收尾不再开目录回本丸"))
        with patch("touken.flows.daily.time.sleep"):
            self.assertTrue(flow._popup_sweep(max_rounds=8, verify_navigation=False))
        flow._probe_nav_ready.assert_not_called()
        self.assertEqual(flow.maa.clicks, [(993, 690)] * 4)

    def test_login_still_checks_navigation_by_default(self):
        flow = _SweepFlow("home")
        flow._probe_nav_ready = unittest.mock.Mock(return_value=True)
        with patch("touken.flows.daily.time.sleep"):
            self.assertTrue(flow._popup_sweep(max_rounds=4))
        flow._probe_nav_ready.assert_called_once()

    def test_signin_calendar_is_claimed_once_then_home(self):
        flow = _SweepFlow("signin")
        with patch("touken.flows.daily.time.sleep"):
            arrived = flow._popup_sweep(max_rounds=8)
        self.assertTrue(arrived)
        self.assertEqual(flow.maa.clicks, [(900, 600)])

    def test_gray_claim_button_is_not_clicked_twice(self):
        # 领完按钮变灰但字还在：一轮扫地只许领一次，剩下靠别的分支推进
        class Maa(_SweepMaa):
            def click(self, pt):
                self.clicks.append((pt.x, pt.y))  # 点了也不切 stage（灰按钮没反应）
                return True

        flow = _SweepFlow("signin")
        flow.maa = Maa("signin")
        with patch("touken.flows.daily.time.sleep"):
            arrived = flow._popup_sweep(max_rounds=4)
        self.assertFalse(arrived)
        self.assertEqual(flow.maa.clicks.count((900, 600)), 1)

    def test_item_detail_popup_is_closed_by_its_own_x(self):
        flow = _SweepFlow("item_detail")
        with patch("touken.flows.daily.time.sleep"):
            arrived = flow._popup_sweep(max_rounds=6)
        self.assertTrue(arrived)
        self.assertEqual(flow.maa.clicks, [(945, 105)])

    def test_training_request_popup_is_declined_with_cancel(self):
        # 「修行启程」确认窗：取消在左、确认在右（2026-09-11 真机取帧）。
        # 点【取消】婉拒可逆（随时能在强化/组织界面再派），绝不点确认烧道具。
        flow = _SweepFlow("training_req")
        with patch("touken.flows.daily.time.sleep"):
            arrived = flow._popup_sweep(max_rounds=6)
        self.assertTrue(arrived)
        self.assertEqual(flow.maa.clicks, [(496, 614)])

    def test_training_letter_is_advanced_before_home_probe(self):
        flow = _SweepFlow("training_letter_notice")
        with patch("touken.flows.daily.time.sleep"):
            arrived = flow._popup_sweep(max_rounds=8)
        self.assertTrue(arrived)
        self.assertEqual(flow.maa.clicks, [(993, 690)] * 4)


if __name__ == "__main__":
    unittest.main()
