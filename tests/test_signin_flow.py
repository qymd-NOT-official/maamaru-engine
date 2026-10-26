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

双服化改造后流程单全部走素材包逻辑名（@signin/*，cn 包解析回来与旧原始值
逐一同值），设备调用序列不变；翻车策略不再逐步手写 on_fail——「没公告」
从旧的「点公告翻车即停」改为条件跳转收工（缺省 recover 收拾不会乱点坐标）。
收工播报（sign-off）是所有分支的共同终点，文案只准通用真实（如「签到收工」），
「没公告」这类分支结论只能出现在该分支自己的播报链里（判定+条件跳转消息）。
"""

import unittest
from unittest.mock import patch

from touken.flows.signin import SigninMixin
from touken.flow_engine import load_official_flows
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
        # 正常签到路径：收工播报是通用文案，绝不误播「没公告」
        self.assertTrue(any("签到收工" in m for m in messages))
        self.assertFalse(any("没公告" in m for m in messages))

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
        # 灰按钮也是正常分支：收工播报通用、无「没公告」误播
        self.assertTrue(any("签到收工" in m for m in messages))
        self.assertFalse(any("没公告" in m for m in messages))

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
        # 没领取按钮也是正常分支：收工播报通用、无「没公告」误播
        self.assertTrue(any("签到收工" in m for m in messages))
        self.assertFalse(any("没公告" in m for m in messages))

    def test_announce_not_found_skips_cleanly(self):
        # 旧代码路径：:35-37 目录里没公告 → 放弃（不点任何坐标）。
        # 双服化后这是条件跳转的正常分支（不再靠点公告翻车即停），
        # 效果对齐：一个坐标都不点，更不触发回本丸收拾。
        # 跳过提示由分支链如实播报：没认到「公告」→ 条件成立跳到收工。
        calls, messages, _agent = run_signin([None])
        self.assertEqual(calls, OPEN_MENU_CALLS + [
            ("screenshot", True),
            ("ocr", "公告", (1100, 100, 180, 580)),
        ])
        self.assertTrue(any("没认到「公告」" in m for m in messages))
        self.assertTrue(any("跳到步骤 sign-off" in m for m in messages))
        self.assertTrue(any("签到收工" in m for m in messages))
        self.assertFalse(any("回本丸收拾残局" in m for m in messages))
        self.assertTrue(any("全部跑完，全绿" in m for m in messages))

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
        # 开目录是引擎级不可恢复积木：不触发回本丸收拾
        self.assertFalse(any("回本丸收拾残局" in m for m in messages))


class SigninOfficialFlowShapeTests(unittest.TestCase):
    """签到流程单本身的双服化形状：全 @逻辑名、无手写 on_fail、官方收编。"""

    def _signin(self):
        flows = {f["id"]: f for f in load_official_flows()}
        self.assertIn("builtin-signin", flows)
        return flows["builtin-signin"]

    def test_official_flag_and_normalizes(self):
        # load_official_flows 内部过 normalize_flow（官方限制：只准 @逻辑名），
        # 能进列表就说明 signin.json 完全逻辑素材化、校验通过
        flow = self._signin()
        self.assertIs(flow["official"], True)
        self.assertEqual(flow["name"], "签到")

    def test_all_refs_are_logical(self):
        flow = self._signin()
        for step in flow["steps"]:
            params = step["params"]
            for key in ("template", "ocr_expected", "roi", "point"):
                value = params.get(key)
                if value is None:
                    continue
                self.assertIsInstance(value, str, (step["id"], key))
                self.assertTrue(value.startswith("@"), (step["id"], key, value))
            # jump_if 的 target 是流程内步骤 id；navigate 的 target 才是界面引用
            if step["type"] == "navigate":
                self.assertTrue(params["target"].startswith("@"),
                                (step["id"], params["target"]))
            self.assertNotIn("x", params, step["id"])

    def test_no_explicit_on_fail(self):
        # 翻车收拾上交引擎：流程单不逐步手写 on_fail，缺省 recover
        flow = self._signin()
        for step in flow["steps"]:
            self.assertEqual(step["on_fail"], "recover", step["id"])
            self.assertEqual(step["retry_times"], 2, step["id"])

    def test_sign_off_note_is_truthful_for_every_branch(self):
        # 收工播报是流程最后一步、所有分支都会播：文案必须通用真实，
        # 不得带「没公告」这类只在某个分支成立的结论（正常签到误播冤案）。
        # 没公告分支照旧条件跳转到 sign-off，不另起炉灶。
        flow = self._signin()
        steps = {s["id"]: s for s in flow["steps"]}
        self.assertEqual(steps["no-announce"]["params"]["target"], "sign-off")
        self.assertNotIn("没公告", steps["sign-off"]["params"]["text"])
        self.assertIn("签到收工", steps["sign-off"]["params"]["text"])

    def test_signin_pack_entries_resolve(self):
        # cn 素材包里签到条目齐全（load_server_pack 加载期已全量校验）
        from touken.server_pack import load_server_pack
        pack = load_server_pack("cn")
        flow = self._signin()
        refs = set()
        for step in flow["steps"]:
            for key in ("template", "ocr_expected", "roi", "point"):
                value = step["params"].get(key)
                if isinstance(value, str) and value.startswith("@"):
                    refs.add(value[1:])
            if step["type"] == "navigate":
                target = step["params"].get("target", "")
                if target.startswith("@"):
                    refs.add(target[1:])
        self.assertTrue(refs)
        for name in sorted(refs):
            self.assertTrue(name.startswith("signin/"), name)
        self.assertEqual(pack.resolve_ocr("@signin/announce"), "公告")
        self.assertEqual(pack.resolve_ocr("@signin/claim"), "领取奖励")
        self.assertEqual(pack.resolve_ocr("@signin/detail"), "道具详情")
        self.assertEqual(pack.resolve_point("@signin/tab"), (550, 55))
        self.assertEqual(pack.resolve_point("@signin/detail-close"), (945, 105))
        self.assertEqual(pack.resolve_point("@signin/click-through"), (640, 400))
        self.assertEqual(pack.resolve_point("@signin/close"), (1195, 30))


if __name__ == "__main__":
    unittest.main()
