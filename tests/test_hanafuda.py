# -*- coding: utf-8 -*-
"""秘宝之里（花牌收集）：配置契约、补键迁移、面板注册与图内监控测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from touken.event_timeline import SCRIPT_EVENT_MAP
from touken.flows.battle import BattleMixin
from touken.flows.hanafuda import HanafudaMixin
from touken.maa_adapter import Point
from touken.runtime_paths import _fill_missing_config_keys

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "touken_config.example.json"
IMAGE_DIR = Path(__file__).resolve().parent.parent / "resource" / "base" / "image"


class HanafudaConfigTests(unittest.TestCase):
    def test_example_config_has_hanafuda_section(self):
        cfg = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        self.assertIn("hanafuda", cfg)
        hana = cfg["hanafuda"]
        self.assertEqual(hana["difficulty"], 4)
        self.assertEqual(hana["max_runs"], 6)
        self.assertEqual(hana["repair_threshold"], "heavy")  # 虚拟伤害，中伤照跑
        self.assertIn("auto_equip", hana)
        self.assertEqual(set(hana["difficulty_cards"].keys()),
                         {"1", "2", "3", "4"})
        self.assertEqual(hana["ui_title"]["template"], "花札/ui秘宝之里.png")
        self.assertEqual(hana["confirm_ui"]["template"], "花札/ui出阵确认.png")
        self.assertIn("target", hana["confirm_button"])  # 模板失配时的实测坐标兜底
        self.assertIn("map_hud_ocr", hana)
        self.assertIn("bubble_ocr", hana)
        self.assertIn("bubble_tap", hana)
        for key in ("injury_deny_button", "injury_stamps",
                    "injury_stamp_roi", "injury_status_roi"):
            self.assertIn(key, hana, f"example 配置缺 hanafuda.{key}")

    def test_templates_exist_on_disk(self):
        for name in ("花札/ui秘宝之里.png", "花札/ui出阵确认.png"):
            self.assertTrue((IMAGE_DIR / name).is_file(), f"缺模板 {name}")

    def test_keyfill_covers_old_installs(self):
        """老配置没有 hanafuda 段：补键必须整段补上，且不动用户已有值。"""
        template = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        old = {k: v for k, v in template.items() if k != "hanafuda"}
        old["pumpkin"]["team_no"] = 9  # 用户改过的值
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "touken_config.json"
            target.write_text(json.dumps(old, ensure_ascii=False),
                              encoding="utf-8")
            added = _fill_missing_config_keys(EXAMPLE_PATH, target,
                                              Path(tmp) / "backup")
            self.assertIn("hanafuda", added)
            merged = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(merged["hanafuda"]["ui_title"]["template"],
                             "花札/ui秘宝之里.png")
            self.assertEqual(merged["pumpkin"]["team_no"], 9)

    def test_slider_ticket_recover_config(self):
        """花札补充页是滑条式（2026-09-20 真机）：补充→确认，默认补 1 个，
        没有联队战的“恢复一个”按钮。"""
        cfg = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        hana = cfg["hanafuda"]
        self.assertEqual(hana["ticket_price"], 300)
        recover = hana["ticket_recover"]
        self.assertNotIn("recover_button", recover)
        self.assertEqual(recover["popup_button"]["template"], "team/补充.png")
        self.assertEqual(recover["confirm_button"]["template"],
                         "花札/令牌补充确认.png")
        self.assertEqual(recover["close_button"]["template"],
                         "花札/令牌补充关闭.png")
        self.assertIn("quantity_ocr", recover)
        for button in ("confirm_button", "close_button"):
            template = IMAGE_DIR / recover[button]["template"]
            self.assertTrue(template.is_file(), f"缺模板 {template}")

    def test_keyfill_adds_ticket_recover_into_existing_hanafuda(self):
        """老配置已有 hanafuda 段但没有 ticket_recover：递归补键要补进去，
        且不动用户改过的值。"""
        template = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        old = json.loads(json.dumps(template))
        del old["hanafuda"]["ticket_recover"]
        del old["hanafuda"]["ticket_price"]
        old["hanafuda"]["max_runs"] = 99  # 用户改过的值
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "touken_config.json"
            target.write_text(json.dumps(old, ensure_ascii=False),
                              encoding="utf-8")
            added = _fill_missing_config_keys(EXAMPLE_PATH, target,
                                              Path(tmp) / "backup")
            self.assertIn("hanafuda.ticket_recover", added)
            self.assertIn("hanafuda.ticket_price", added)
            merged = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(merged["hanafuda"]["max_runs"], 99)
            self.assertEqual(
                merged["hanafuda"]["ticket_recover"]["confirm_button"]
                ["template"], "花札/令牌补充确认.png")

    def test_event_timeline_hides_hanafuda_off_season(self):
        self.assertEqual(SCRIPT_EVENT_MAP.get("hanafuda"), ["秘宝之里"])

    def test_agent_mro_resolves_hanafuda_methods(self):
        """Mixin 同名方法会被排前面的 mixin 盖掉（edocastle._enter_map_stream
        曾截胡花札的入图流程）——组装后的 ToukenAgent 必须解析到花札版。"""
        from touken.agent import ToukenAgent
        for name in ("hanafuda_stream", "_enter_hanafuda_map_stream",
                     "_watch_round_stream", "_save_hanafuda_shot"):
            self.assertIs(getattr(ToukenAgent, name),
                          getattr(HanafudaMixin, name), name)


class HanafudaPanelTests(unittest.TestCase):
    def test_script_registered_with_expected_fields(self):
        from panel.server import list_scripts
        info = list_scripts()["hanafuda"]
        keys = [field.get("key") for field in info["params"]]
        self.assertEqual(keys, ["difficulty", "team_no", "runs",
                                "use_koban_refill", "rotate_captain",
                                "rotate_captain_margin"])
        self.assertEqual(info["params"][2]["min"], 1)
        self.assertNotIn("help", info["params"][2])
        difficulty = info["params"][0]
        self.assertEqual(difficulty["options"][-1], ["4", "难度·超难"])
        self.assertEqual(difficulty["default"], "4")

    def test_build_passes_params_through(self):
        from panel.server import _build_hanafuda, _wrap_inventory

        class FakeAgent:
            hanafuda_args = None

            def hanafuda_stream(self, **kwargs):
                self.hanafuda_args = kwargs
                yield "hana"

        agent = FakeAgent()
        with patch("panel.server._make_agent", return_value=agent):
            list(_wrap_inventory("花札", _build_hanafuda)(
                "config.json", {"difficulty": "2", "team_no": "4",
                                "runs": "5",
                                "use_koban_refill": True,
                                "rotate_captain": True,
                                "rotate_captain_margin": "20"}))
        self.assertEqual(agent.hanafuda_args["difficulty"], 2)
        self.assertEqual(agent.hanafuda_args["team_no"], 4)
        self.assertEqual(agent.hanafuda_args["max_runs"], 5)
        self.assertTrue(agent.hanafuda_args["auto_refill"])
        self.assertTrue(agent.hanafuda_args["rotate_captain"])
        self.assertEqual(agent.hanafuda_args["rotate_captain_margin"], 20)

        agent.hanafuda_args = None
        with patch("panel.server._make_agent", return_value=agent):
            list(_wrap_inventory("花札", _build_hanafuda)(
                "config.json", {"difficulty": "4", "team_no": "3",
                                "max_runs": "0", "use_koban_refill": False}))
        self.assertEqual(agent.hanafuda_args["max_runs"], 6)
        self.assertFalse(agent.hanafuda_args["auto_refill"])
        self.assertFalse(agent.hanafuda_args["rotate_captain"])
        self.assertEqual(agent.hanafuda_args["rotate_captain_margin"], 10)


class HanafudaTelemetryTests(unittest.TestCase):
    def test_completed_run_records_current_total_tama(self):
        class _Maa:
            def screenshot(self, force=False):
                return None

            def template_match(self, template):
                return template if template == "title.png" else None

        class _Host(HanafudaMixin):
            def __init__(self):
                self.maa = _Maa()
                self.current_location = "出阵"
                self.events = []
                self.tama = iter([100, 777])
                self.config = {
                    "team_select": {"teams": {"3": [0, 0]}},
                    "hanafuda": {
                        "team_no": 3, "difficulty": 4, "max_runs": 1,
                        "ui_title": {"template": "title.png"},
                        "activity_entry": {"template": "entry.png"},
                        "difficulty_cards": {"4": [0, 0]},
                    },
                }

            def navigate_to_stream(self, target):
                return iter(())

            def _expedition_takeover_requested(self, now=None):
                return False

            def set_progress(self, value):
                pass

            def _read_tama_total(self, cfg):
                return next(self.tama)

            def _enter_hanafuda_map_stream(self, *args, **kwargs):
                if False:
                    yield None
                return True, True

            def _watch_round_stream(self, *args, **kwargs):
                if False:
                    yield None
                return True

            def record_event(self, event_type, **payload):
                self.events.append((event_type, payload))

        host = _Host()
        list(host.hanafuda_stream(max_runs=1))
        self.assertEqual(host.events[0][0], "hanafuda.run_completed")
        self.assertEqual(host.events[0][1]["tama"], 677)
        self.assertEqual(host.events[0][1]["tama_total"], 777)


class _WatchMaa:
    """按帧出题的识别假人：每帧给出模板集/OCR 命中集/对话条文字。"""

    def __init__(self, frames):
        self.frames = iter(frames)
        self.frame = {"templates": set(), "ocr": set(), "texts": []}

    def screenshot(self, force=False):
        self.frame = next(self.frames, self.frame)
        return None

    def template_match(self, template, roi=None, threshold=0.7):
        return template if template in self.frame["templates"] else None

    def ocr(self, expected, roi=None, **kwargs):
        return expected if expected in self.frame["ocr"] else None

    def ocr_all(self, roi, image=None):
        return list(self.frame["texts"])

    def save_screenshot(self, path):
        return True

    def click(self, point):
        return True


_CFG = {
    "ui_title": {"template": "花札/ui秘宝之里.png"},
    "map_hud_ocr": {"expected": "剩余行动次数", "roi": [20, 5, 180, 45]},
    "bubble_ocr": {"roi": [380, 420, 960, 500]},
    "bubble_tap": [870, 394],
    "injury_deny_button": {"template": "否.png"},
    "skip_tap": [775, 695],
}


class _WatchHost(HanafudaMixin, BattleMixin):
    def __init__(self, frames, net_result=False):
        self.maa = _WatchMaa(frames)
        self.config = {}
        self.clicked = []
        self._net_result = net_result
        self._root = tempfile.mkdtemp()

    def _click_point(self, point):
        self.clicked.append(list(point))

    def _find_deploy_button(self, cfg):
        return (1200, 645) if self.maa.frame.get("deploy") else None

    def recover_network_stream(self):
        return self._net_result
        yield


def _watch(flow, cfg=_CFG, timeout_s=5.0):
    gen = flow._watch_round_stream(cfg, timeout_s=timeout_s)
    msgs = []
    while True:
        try:
            msgs.append(next(gen))
        except StopIteration as stop:
            return msgs, stop.value


_HUD = "剩余行动次数"
_TITLE = "花札/ui秘宝之里.png"


@patch("touken.flows.hanafuda.time.sleep", lambda *_a, **_k: None)
class HanafudaWatchTests(unittest.TestCase):
    def test_round_ends_when_ui_title_returns(self):
        host = _WatchHost([
            {"templates": set(), "ocr": {_HUD}, "texts": []},      # 进图上膛
            {"templates": set(), "ocr": {_HUD}, "texts": []},      # 跑图中
            {"templates": {_TITLE}, "ocr": set(), "texts": [],
             "deploy": True},                                      # 回主界面
        ])
        msgs, ok = _watch(host)
        self.assertTrue(ok)
        self.assertEqual(host.clicked, [])

    def test_entry_splash_title_is_not_round_end(self):
        """进图过场重播活动横幅：没见过地图 HUD 之前，ui_title 不算圈结束
        （2026-09-11 真机翻车：过场帧 1.000 假命中，脚本差点在出发点收工）。"""
        host = _WatchHost([
            {"templates": {_TITLE}, "ocr": set(), "texts": [],
             "deploy": True},                                      # 过场假横幅
            {"templates": set(), "ocr": {_HUD}, "texts": []},      # 真进图
            {"templates": {_TITLE}, "ocr": set(), "texts": [],
             "deploy": True},                                      # 真回主界面
        ])
        msgs, ok = _watch(host)
        self.assertTrue(ok)
        self.assertTrue(any("进图了" in m for m in msgs))

    def test_title_without_deploy_button_is_not_round_end(self):
        """只有横幅、部队选择按钮不在 = 结算过场，不算回主界面。"""
        host = _WatchHost([
            {"templates": set(), "ocr": {_HUD}, "texts": []},
            {"templates": {_TITLE}, "ocr": set(), "texts": []},    # 无 deploy
            {"templates": {_TITLE}, "ocr": set(), "texts": [],
             "deploy": True},
        ])
        msgs, ok = _watch(host)
        self.assertTrue(ok)

    def test_bubble_tapped_only_with_hud_and_dialog_text(self):
        host = _WatchHost([
            {"templates": set(), "ocr": {_HUD}, "texts": []},      # 进图上膛
            # 战斗画面：对话条有字（刀光剑影里的字幕）但 HUD 不在 → 不许点
            {"templates": set(), "ocr": set(), "texts": [("白刃战", None)]},
            # 地图 HUD 在 + 对话条有字 = 狐之助气泡 → 点掉
            {"templates": set(), "ocr": {_HUD},
             "texts": [("选择直接挑战BOSS或继续前进", None)]},
            # 气泡点完：HUD 在、对话条空了 → 不点
            {"templates": set(), "ocr": {_HUD}, "texts": []},
            {"templates": {_TITLE}, "ocr": set(), "texts": [],
             "deploy": True},
        ])
        msgs, ok = _watch(host)
        self.assertTrue(ok)
        self.assertEqual(host.clicked, [[870, 394]])

    def test_heavy_injury_warning_denied_and_stops(self):
        """重伤警告走通用弹窗守卫：读到弹窗正文和【否】按钮才点，
        点完弹窗关闭后停手让人看（e239e3d 起不再认 否.png 模板）。"""
        host = _WatchHost([
            {"templates": set(), "ocr": set(), "texts": [
                ("队中有重伤的刀剑，确认继续进军吗？点击否可以取消",
                 Point(600, 320)),
                ("否", Point(785, 468)),
            ]},
            {"templates": set(), "ocr": set(), "texts": []},        # 点否后弹窗关闭
        ])
        with patch("touken.flows.battle.time.sleep", lambda *_a, **_k: None):
            msgs, ok = _watch(host)
        self.assertFalse(ok)
        self.assertTrue(any("重伤" in m for m in msgs))

    def test_network_home_landing_aborts_round(self):
        host = _WatchHost([
            {"templates": set(), "ocr": set(), "texts": []},
        ], net_result="home")
        msgs, ok = _watch(host)
        self.assertFalse(ok)
        self.assertTrue(any("本丸" in m for m in msgs))

    def test_never_entering_map_is_reported(self):
        """确认出阵后迟迟见不到地图 HUD：如实报没进图，不装跑完。"""
        host = _WatchHost([
            {"templates": set(), "ocr": set(), "texts": []},
        ])
        with patch("touken.flows.hanafuda.time.monotonic",
                   side_effect=[0, 0, 1, 95, 95, 95]):
            msgs, ok = _watch(host, timeout_s=200.0)
        self.assertFalse(ok)
        self.assertTrue(any("没见到地图" in m for m in msgs))

    def test_watchdog_stops_when_stuck(self):
        host = _WatchHost([
            {"templates": set(), "ocr": set(), "texts": []},
        ])
        with patch("touken.flows.hanafuda.time.monotonic",
                   side_effect=[0, 0, 100, 100, 100, 100, 100]):
            msgs, ok = _watch(host, timeout_s=5.0)
        self.assertFalse(ok)
        self.assertTrue(any("疑似卡住" in m for m in msgs))


class HanafudaEnterMapTests(unittest.TestCase):
    class _EnterHost(HanafudaMixin):
        def __init__(self, auto_march_ok):
            import types
            self.maa = types.SimpleNamespace(
                screenshot=lambda force=False: None,
                save_screenshot=lambda path: True,
            )
            self.config = {"raid": {"ticket_recover": {}}}
            self.clicked = []
            self._auto_march_ok = auto_march_ok
            self.safe_depart_called = False
            self.safe_depart_kwargs = None
            self._root = tempfile.mkdtemp()

        def _click_point(self, point):
            self.clicked.append(list(point))

        def skip_safe(self, times, interval=0.8, point=None):
            pass

        def _wait_for_team_select(self, cfg, attempts=10, open_after=None):
            return True

        def _enable_auto_march(self):
            return self._auto_march_ok

        def _safe_depart_stream(self, *a, **k):
            self.safe_depart_called = True
            self.safe_depart_kwargs = k
            return True, False
            yield

        def _confirm_departure(self, cfg):
            return True

    def test_auto_march_failure_blocks_departure(self):
        host = self._EnterHost(auto_march_ok=False)
        gen = host._enter_hanafuda_map_stream(_CFG | {
            "team_ui_ocr": {"expected": "部队选择", "roi": [506, 1, 774, 77]},
        }, 3, [1082, 300], "heavy", False, False)
        msgs = list(gen)
        self.assertFalse(host.safe_depart_called)
        self.assertTrue(any("委托" in m for m in msgs))

    def test_happy_path_enters_map(self):
        host = self._EnterHost(auto_march_ok=True)
        gen = host._enter_hanafuda_map_stream(_CFG | {
            "team_ui_ocr": {"expected": "部队选择", "roi": [506, 1, 774, 77]},
        }, 3, [1082, 300], "heavy", False, False,
           rotate_captain=True, rotate_captain_margin=20)
        msgs = list(gen)
        self.assertTrue(host.safe_depart_called)
        self.assertTrue(host.safe_depart_kwargs["rotate_captain"])
        self.assertEqual(host.safe_depart_kwargs["rotate_captain_margin"], 20)
        self.assertFalse(any("没挂上" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()
