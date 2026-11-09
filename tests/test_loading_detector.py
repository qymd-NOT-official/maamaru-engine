import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from touken.device_adapter import DeviceAdapter
from touken.navigator import NavigationMixin


def _make_adapter_with_image(image):
    adapter = DeviceAdapter.__new__(DeviceAdapter)
    adapter._last_image = None
    adapter._initialized = True
    adapter.screenshot = lambda force=False: image
    return adapter


def _loading_frame():
    """黑屏 + 左下樱花亮斑"""
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    img[650:710, 8:58] = 230  # 一朵常亮的花
    return img


class LoadingDetectorTests(unittest.TestCase):
    def test_black_screen_with_corner_sakura_is_loading(self):
        adapter = _make_adapter_with_image(_loading_frame())
        self.assertTrue(adapter.looks_like_loading())

    def test_normal_screen_is_not_loading(self):
        img = np.random.default_rng(7).integers(
            40, 220, (720, 1280, 3), dtype=np.uint8)
        adapter = _make_adapter_with_image(img)
        self.assertFalse(adapter.looks_like_loading())

    def test_pure_black_screen_without_sakura_is_not_loading(self):
        adapter = _make_adapter_with_image(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertFalse(adapter.looks_like_loading())

    def test_bright_corner_but_normal_screen_is_not_loading(self):
        """弹窗/立绘压在画面上时，角落再亮也不算加载态。"""
        img = _loading_frame()
        img[100:400, 400:900] = 180  # 中央一大片内容
        adapter = _make_adapter_with_image(img)
        self.assertFalse(adapter.looks_like_loading())

    def test_no_screenshot_is_not_loading(self):
        adapter = _make_adapter_with_image(None)
        self.assertFalse(adapter.looks_like_loading())


class _NavFlow(NavigationMixin):
    def __init__(self, device):
        self.device = device
        self.config = {"navigation": {
            "本丸": {
                "from": "通用入口",
                "primary": {"type": "TemplateMatch", "template": "menu/本丸.png"},
                "verify": {"type": "TemplateMatch", "template": "ui本丸.png"},
            },
            "通用入口": {"target": [993, 690]},
        }}
        self.current_location = None
        self.clicks = []

    def _click_point(self, target):
        self.clicks.append(target)
        return True


class _NavMaa:
    """导航假设备：目录模板直接命中（跳过开目录），目标页地标按剧本出现。"""

    def __init__(self, loading_seq=(), verify_after=1):
        self.loading_seq = list(loading_seq)
        self.verify_after = verify_after
        self.verify_calls = 0

    def screenshot(self, force=False):
        return object()

    def exists(self, template, roi=None, threshold=0.7):
        return True  # 目录始终视为已展开

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "menu/本丸.png":
            return SimpleNamespace(x=100, y=100)
        if template == "ui本丸.png":
            self.verify_calls += 1
            if self.verify_calls > self.verify_after:
                return SimpleNamespace(x=640, y=24)
        return None

    def click(self, point):
        return True

    def looks_like_loading(self):
        return self.loading_seq.pop(0) if self.loading_seq else False


class NavLoadingTests(unittest.TestCase):
    def test_loading_waits_instead_of_failing(self):
        """转樱花时不报超时，加载结束后正常到达。"""
        device = _NavMaa(loading_seq=[True, True], verify_after=3)
        flow = _NavFlow(device)
        flow.current_location = "通用入口"  # 跳过开目录，专验导航等待
        with patch("touken.navigator.time.sleep"):
            messages = list(flow.navigate_to_stream("本丸"))
        self.assertIn("[NAV] 成功到达: 本丸", messages)
        self.assertTrue(any("转樱花" in m for m in messages))

    def test_loading_forever_reports_failure_honestly(self):
        """转圈超过耐心上限：如实报失败（断网/炸服），不傻等不误点。"""
        device = _NavMaa(loading_seq=[True] * 200, verify_after=10**9)
        flow = _NavFlow(device)
        flow.current_location = "通用入口"
        with patch("touken.navigator.time.sleep"), \
                patch.object(NavigationMixin, "LOADING_PATIENCE_S", 0.0):
            messages = list(flow.navigate_to_stream("本丸"))
        self.assertTrue(any("没能到达" in m for m in messages))
        self.assertNotIn("[NAV] 成功到达: 本丸", messages)

    def test_open_menu_waits_out_loading_without_burning_attempts(self):
        """开目录撞上加载：不占尝试次数，转完照常打开。"""
        device = _NavMaa(loading_seq=[True, True, True])
        flow = _NavFlow(device)
        with patch("touken.navigator.time.sleep"):
            self.assertTrue(flow._open_menu())

    def test_open_menu_gives_up_when_loading_outlasts_patience(self):
        device = _NavMaa(loading_seq=[True] * 400)
        flow = _NavFlow(device)
        with patch("touken.navigator.time.sleep"), \
                patch.object(NavigationMixin, "LOADING_PATIENCE_S", 0.0):
            self.assertFalse(flow._open_menu())
        # 耐心上限挡着，前两轮就该放弃，不会真把 400 轮剧本耗完
        self.assertTrue(len(device.loading_seq) > 300)


class _PopupMaa:
    """弹窗救援假设备：目录按钮被盖住，通用_关闭按剧本命中，
    画面指纹可控制不变（模拟系统模态框吃点击）或逐帧变化。"""

    def __init__(self, frames, close_after=None, open_after_safe=False):
        self.frames = frames          # True=每次返回新帧，False=永远同一帧
        self.close_after = close_after  # 点 N 次后弹窗关掉（通用_关闭不再命中）
        self.open_after_safe = open_after_safe  # 安全点一点后菜单展开
        self.clicks = []
        self.close_clicks = 0
        self.menu_open = False
        self._tick = 0

    def screenshot(self, force=False):
        self._tick += 1
        return f"frame{self._tick}".encode() if self.frames else b"sameframe"

    def exists(self, template, roi=None, threshold=0.7):
        return self.menu_open if template == "menu/ui目录.png" else False

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "通用_关闭.png" and (
                self.close_after is None or self.close_clicks < self.close_after):
            return SimpleNamespace(x=1254, y=25)
        return None

    def click(self, point):
        self.clicks.append(point)
        if getattr(point, "x", None) == 1254:
            self.close_clicks += 1
        elif self.open_after_safe and getattr(point, "x", None) == 993:
            self.menu_open = True
        return True

    def looks_like_loading(self):
        return False


class PopupRescueStuckTests(unittest.TestCase):
    """2026-11-05 现场：MuMu 游戏中心 ANR 对话框压住碎片弹窗，救援连点
    同一个 X 三十次全被吃掉。同点同画面第 3 次必须如实放弃。"""

    def test_same_close_button_same_frame_gives_up_fast(self):
        device = _PopupMaa(frames=False)
        flow = _NavFlow(device)
        with patch("touken.navigator.time.sleep"):
            self.assertFalse(flow._open_menu())
        self.assertEqual(len(device.clicks), 2)  # 第 3 次识别出卡死，不再点

    def test_changing_frames_means_clicks_work_keep_going(self):
        """多层弹窗逐层关：每击画面都变，不算卡死，按原节奏用满尝试次数。"""
        device = _PopupMaa(frames=True)
        flow = _NavFlow(device)
        with patch("touken.navigator.time.sleep"):
            self.assertFalse(flow._open_menu())
        self.assertEqual(len(device.clicks), 10)

    def test_popup_actually_closes_then_menu_opens(self):
        device = _PopupMaa(frames=True, close_after=2, open_after_safe=True)
        flow = _NavFlow(device)
        with patch("touken.navigator.time.sleep"):
            self.assertTrue(flow._open_menu())
        self.assertEqual(device.close_clicks, 2)


if __name__ == "__main__":
    unittest.main()
