import unittest
from unittest.mock import patch

from touken.flows.login import LoginMixin
from touken.flows.daily import DailyMixin
from touken.device_adapter import Point


class UpdateMaa:
    def __init__(self, line="线路一"):
        self.stage = "prompt"
        self.line = line
        self.clicks = []

    def screenshot(self, force=False):
        return object()

    def ocr_all(self, roi):
        if self.stage == "prompt":
            return [("检测到更新，请重新启动游戏进行更新", Point(640, 320))]
        return []

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "通用_确定.png" and self.stage == "prompt":
            return Point(640, 468)
        if template == "登录.png" and self.stage == "login":
            return Point(640, 600)
        return None

    def exists(self, template, roi=None, threshold=0.7):
        return template == "目录.png" and self.stage == "home"

    def ocr(self, expected, roi, match_mode="exact"):
        if self.stage == "line" and expected == self.line:
            return Point(640, 420)
        return None

    def click(self, point):
        self.clicks.append(point)
        if self.stage == "prompt":
            self.stage = "login"
        elif self.stage == "login":
            self.stage = "line"
        elif self.stage == "line":
            self.stage = "home"


class UpdateFlow(LoginMixin):
    def __init__(self, device):
        self.device = device
        self.current_location = None


class DailyUpdateFlow(DailyMixin, LoginMixin):
    def __init__(self, device):
        self.device = device
        self.current_location = None
        self.sweeps = 0

    def _popup_sweep(self, max_rounds=30):
        self.sweeps += 1
        return True


class GameLaunchMaa:
    def __init__(self, adb_ok=True, safe_icon=False):
        self.adb_ok = adb_ok
        self.safe_icon = safe_icon
        self.stage = "desktop"
        self.adb_calls = []
        self.clicks = []

    def screenshot(self, force=False):
        return object()

    def exists(self, template, roi=None, threshold=0.7):
        if template == "目录.png":
            return self.stage == "home"
        if template == "登录.png":
            return self.stage == "login"
        return False

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "刀剑乱舞.png" and self.safe_icon:
            return Point(220, 220)
        return None

    def ocr(self, expected, roi, match_mode="exact"):
        if expected == "刀剑乱舞" and self.safe_icon:
            return Point(220, 270)
        return None

    def click(self, point):
        self.clicks.append(point)
        self.stage = "login"

    def _adb_run(self, args, timeout=15.0, binary=False):
        self.adb_calls.append(args)
        if not self.adb_ok:
            return None
        if "resolve-activity" in args:
            return b"com.youzu.djlw/org.cocos2dx.lua.AppActivity\n"
        if "start" in args:
            self.stage = "login"
            return b"Status: ok\n"
        return b""


class GameLaunchFlow(DailyMixin):
    def __init__(self, device):
        self.device = device
        self.config = {"daily": {"logout": {"package": "com.youzu.djlw"}}}


class DailyLaunchFailureFlow(DailyMixin):
    def __init__(self):
        self.config = {"daily": {}}
        self.login_called = False
        self.reports = []

    def _daily_update_gate(self):
        if False:
            yield None
        return False

    def _ensure_game_started(self):
        if False:
            yield None
        return False

    def login(self):
        self.login_called = True

    def _flush_report(self, report, finished):
        self.reports.append((list(report), finished))
        return {}


def generator_result(generator):
    while True:
        try:
            next(generator)
        except StopIteration as stop:
            return stop.value


class GameUpdateRecoveryTests(unittest.TestCase):
    def test_prompt_guard_requires_update_and_restart_words(self):
        device = UpdateMaa()
        flow = UpdateFlow(device)
        self.assertTrue(flow._game_update_prompt_visible())
        device.stage = "login"
        self.assertFalse(flow._game_update_prompt_visible())

    def test_recovers_through_login_and_primary_line(self):
        device = UpdateMaa("线路一")
        flow = UpdateFlow(device)
        with patch("touken.flows.login.time.sleep"):
            self.assertTrue(generator_result(flow.recover_game_update_stream()))
        self.assertEqual(len(device.clicks), 3)
        self.assertEqual(flow.current_location, "本丸")

    def test_falls_back_to_secondary_line(self):
        device = UpdateMaa("线路二")
        flow = UpdateFlow(device)
        with patch("touken.flows.login.time.sleep"):
            self.assertTrue(generator_result(flow.recover_game_update_stream()))
        self.assertEqual(len(device.clicks), 3)
        self.assertEqual(flow.current_location, "本丸")

    def test_daily_gate_recovers_before_navigation_and_cleans_popups(self):
        device = UpdateMaa("线路一")
        flow = DailyUpdateFlow(device)
        with patch("touken.flows.login.time.sleep"):
            self.assertTrue(generator_result(flow._daily_update_gate()))
        self.assertEqual(flow.sweeps, 1)
        self.assertEqual(flow.current_location, "本丸")

    def test_game_launch_prefers_adb_and_never_clicks_desktop(self):
        device = GameLaunchMaa(adb_ok=True, safe_icon=True)
        flow = GameLaunchFlow(device)
        with patch("touken.flows.daily.time.sleep"):
            messages = []
            generator = flow._ensure_game_started()
            while True:
                try:
                    messages.append(next(generator))
                except StopIteration as stop:
                    result = stop.value
                    break
        self.assertTrue(result)
        self.assertFalse(device.clicks)
        self.assertTrue(any("resolve-activity" in call for call in device.adb_calls))
        self.assertTrue(any("start" in call for call in device.adb_calls))
        self.assertTrue(any("绕过 MuMu 桌面遮挡" in msg for msg in messages))

    def test_game_launch_accepts_existing_login_page_without_adb(self):
        device = GameLaunchMaa(adb_ok=True, safe_icon=False)
        device.stage = "login"
        flow = GameLaunchFlow(device)
        self.assertTrue(generator_result(flow._ensure_game_started()))
        self.assertFalse(device.adb_calls)
        self.assertFalse(device.clicks)

    def test_game_launch_falls_back_only_to_verified_icon(self):
        device = GameLaunchMaa(adb_ok=False, safe_icon=True)
        flow = GameLaunchFlow(device)
        with patch("touken.flows.daily.time.sleep"):
            result = generator_result(flow._ensure_game_started())
        self.assertTrue(result)
        self.assertEqual(device.clicks, [Point(220, 220)])

    def test_game_launch_stops_safely_when_ad_covers_icon(self):
        device = GameLaunchMaa(adb_ok=False, safe_icon=False)
        flow = GameLaunchFlow(device)
        messages = list(flow._ensure_game_started())
        self.assertFalse(device.clicks)
        self.assertTrue(any("没有在当前画面盲点" in msg for msg in messages))

    def test_daily_stops_before_login_when_game_did_not_start(self):
        flow = DailyLaunchFailureFlow()
        messages = list(flow.daily_stream(only=["登录"], after="none"))
        self.assertFalse(flow.login_called)
        self.assertIn("[日课] 没有确认游戏成功启动，本次日课停止", messages)
        self.assertEqual(flow.reports[-1], ([('登录', '✗ 游戏没有启动')], True))


if __name__ == "__main__":
    unittest.main()
