# -*- coding: utf-8 -*-
"""签到试点：官方流程 JSON 与旧 Python 实现的行为对等测试。

旧实现（已删除，见 git 历史）的关键动作序列逐行推自 touken/flows/signin.py：
  signin.py:28-39  打开目录(_open_menu) → 睡 1s → 强刷截图 → OCR「公告」(1100,100,1280,680)
                   → 点公告 → 睡 3s
  signin.py:42-52  _find_claim_button（强刷截图 + OCR「领取奖励」(750,550,1100,670)）；
                   没找到 → 点顶部签到标签 (550,55) → 睡 2s → 再找；
                   仍没有 → 播报跳过 + _close_all
  signin.py:55-69  点领取按钮 → 睡 2.5s → 强刷截图 → OCR「道具详情」(450,80,830,140)
                   → 命中点弹窗 X (945,105) 睡 1.5s 播报「奖励到手」，
                     没命中播报「按钮是灰的」→ 点穿公告页 (640,400) → 睡 1s
  signin.py:78-84  _close_all：点公告 X (1195,30) → 睡 1.5s → current_location=None

本测试对**新实现**（signin_stream 薄壳 → run_flow 跑 resource/base/flows/signin.json）
断言 device 调用序列与上面旧实现完全一致（五个分支全覆）。
"""

import unittest
from unittest.mock import patch

from touken.flows.signin import SigninMixin
from touken.device_adapter import Point
from touken.navigator import NavigationMixin

ANNOUNCE = Point(1150, 300)   # OCR「公告」认到的入口
CLAIM = Point(900, 600)       # OCR「领取奖励」认到的按钮
DETAIL = Point(640, 110)      # OCR「道具详情」认到的标题

OPEN_MENU_CALLS = [
    ("screenshot", True),                  # navigator._open_menu 强制刷新
    ("exists", "menu/ui目录.png"),          # 菜单已展开探针
]


class ScriptedMaa:
    """按剧本回应识别的假 DeviceAdapter，同时记录每次调用。"""

    def __init__(self, ocr_plan):
        self.calls = []
        self.ocr_plan = list(ocr_plan)

    def screenshot(self, force=False):
        self.calls.append(("screenshot", force))
        return None

    def click(self, point):
        self.calls.append(("click", point.x, point.y))
        return True

    def swipe(self, *args, **kwargs):
        self.calls.append(("swipe",))
        return True

    def ocr(self, expected, roi=None, match_mode="contains"):
        self.calls.append(("ocr", expected, roi.to_tuple() if roi else None))
        return self.ocr_plan.pop(0)

    def ocr_all(self, roi, image=None):
        return []

    def template_match(self, template, roi=None, threshold=0.7):
        self.calls.append(("template_match", template))
        return None

    def exists(self, template, roi=None, threshold=0.7):
        self.calls.append(("exists", template))
        return True


class SigninAgent(NavigationMixin, SigninMixin):
    """真导航层 + 薄壳签到的最小宿主。"""

    def __init__(self, device):
        self.device = device
        self.config = {}
        self.current_location = None


def run_signin(plan):
    device = ScriptedMaa(plan)
    agent = SigninAgent(device)
    with patch("touken.flow_engine._sleep"):
        messages = list(agent.signin_stream())
    return device.calls, messages, agent


class SigninFlowParityTests(unittest.TestCase):
    def test_normal_claim_with_popup(self):
        # 旧代码路径：signin.py:33-69 全段直通 + _close_all
        calls, messages, agent = run_signin([ANNOUNCE, CLAIM, DETAIL])
        self.assertEqual(calls, OPEN_MENU_CALLS + [
            ("screenshot", True),                       # :33 强刷
            ("ocr", "公告", (1100, 100, 180, 580)),       # :34 OCR 公告
            ("click", 1150, 300),                       # :38 点公告
            ("screenshot", True),                       # :75 强刷（_find_claim_button）
            ("ocr", "领取奖励", (750, 550, 350, 120)),     # :76
            ("click", 900, 600),                        # :56 点领取按钮
            ("screenshot", True),                       # :60 强刷
            ("ocr", "道具详情", (450, 80, 380, 60)),       # :61
            ("click", 945, 105),                        # :62 点弹窗 X
            ("click", 640, 400),                        # :68 点穿公告页
            ("click", 1195, 30),                        # :80 关公告
        ])
        self.assertTrue(any("奖励到手" in m for m in messages))
        self.assertIsNone(agent.current_location)       # :84 位置副作用

    def test_grey_button_already_signed(self):
        # 旧代码路径：按钮灰 → 道具详情没认到 → 跳过 X → 仍点穿公告 + _close_all
        calls, messages, _agent = run_signin([ANNOUNCE, CLAIM, None])
        self.assertEqual(calls, OPEN_MENU_CALLS + [
            ("screenshot", True),
            ("ocr", "公告", (1100, 100, 180, 580)),
            ("click", 1150, 300),
            ("screenshot", True),
            ("ocr", "领取奖励", (750, 550, 350, 120)),
            ("click", 900, 600),
            ("screenshot", True),
            ("ocr", "道具详情", (450, 80, 380, 60)),
            ("click", 640, 400),
            ("click", 1195, 30),
        ])
        self.assertTrue(any("今天已经签过了" in m for m in messages))
        self.assertFalse(any("奖励到手" in m for m in messages))

    def test_click_signin_tab_then_claim(self):
        # 旧代码路径：:42-47 首次没找到 → 点标签 (550,55) → 睡 2s → 再找 → 领
        calls, _messages, _agent = run_signin([ANNOUNCE, None, CLAIM, DETAIL])
        self.assertEqual(calls, OPEN_MENU_CALLS + [
            ("screenshot", True),
            ("ocr", "公告", (1100, 100, 180, 580)),
            ("click", 1150, 300),
            ("screenshot", True),
            ("ocr", "领取奖励", (750, 550, 350, 120)),
            ("click", 550, 55),                         # :45 顶部「签到」标签
            ("screenshot", True),
            ("ocr", "领取奖励", (750, 550, 350, 120)),
            ("click", 900, 600),
            ("screenshot", True),
            ("ocr", "道具详情", (450, 80, 380, 60)),
            ("click", 945, 105),
            ("click", 640, 400),
            ("click", 1195, 30),
        ])

    def test_no_claim_button_skips_claim_section(self):
        # 旧代码路径：:49-52 两次都没找到 → 播报跳过 → _close_all（不点 640,400）
        calls, messages, _agent = run_signin([ANNOUNCE, None, None])
        self.assertEqual(calls, OPEN_MENU_CALLS + [
            ("screenshot", True),
            ("ocr", "公告", (1100, 100, 180, 580)),
            ("click", 1150, 300),
            ("screenshot", True),
            ("ocr", "领取奖励", (750, 550, 350, 120)),
            ("click", 550, 55),
            ("screenshot", True),
            ("ocr", "领取奖励", (750, 550, 350, 120)),
            ("click", 1195, 30),
        ])
        self.assertTrue(any("跳过" in m for m in messages))

    def test_announce_not_found_aborts(self):
        # 旧代码路径：:35-37 目录里没公告 → 放弃（不点任何坐标）
        calls, messages, _agent = run_signin([None])
        self.assertEqual(calls, OPEN_MENU_CALLS + [
            ("screenshot", True),
            ("ocr", "公告", (1100, 100, 180, 580)),
        ])
        # check 步骤本身不翻车（认不到是正常结论），停在下一点击步：
        # 「点公告」发现上一步没认到位置 → 按设定停止，效果等同旧的放弃
        self.assertTrue(any("「点公告」翻车" in m for m in messages))

    def test_open_menu_failure_stops(self):
        # 目录没打开：旧 :28-30 放弃。exists/模板全 False 让 _open_menu
        # 转满 10 圈自救（期间的救援点击新旧完全一致，同属 _open_menu），
        # 关键是：从没 OCR 过公告 = 流程停在开目录这一步
        device = ScriptedMaa([])
        device.exists = lambda template, roi=None, threshold=0.7: (
            device.calls.append(("exists", template)), False)[1]
        device.template_match = lambda template, roi=None, threshold=0.7: (
            device.calls.append(("template_match", template)), None)[1]
        agent = SigninAgent(device)
        with patch("touken.flow_engine._sleep"):
            messages = list(agent.signin_stream())
        self.assertEqual(device.calls[0], ("screenshot", True))
        self.assertFalse(any(c[0] == "ocr" for c in device.calls))
        self.assertTrue(any("目录没打开" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
