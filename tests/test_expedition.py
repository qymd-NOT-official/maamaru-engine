import json
import unittest
from unittest.mock import patch

import numpy as np

from touken.flows.daily import DailyMixin
from touken.flows.expedition import ExpeditionMixin
from touken.flows.report_judge import _is_fail
from touken.maa_adapter import Point, roi_4to4
from touken.navigator import NavigationMixin


class ExpeditionRewardTests(unittest.TestCase):
    def test_reads_positive_resources_and_success_result(self):
        class Maa:
            def __init__(self):
                self.responses = iter([
                    [("15", Point(900, 500))],
                    [("2", Point(890, 550)), ("2", Point(905, 550))],
                    [("0", Point(900, 600))],
                    [("0", Point(900, 650))],
                    [("大成功", Point(800, 90))],
                ])

            def ocr_all(self, roi):
                return next(self.responses)

        flow = ExpeditionMixin()
        flow.maa = Maa()
        rewards, status, result = flow._read_settlement_rewards({})

        self.assertEqual(rewards, {"木炭": 15, "玉钢": 22})
        self.assertEqual(status, {"木炭": "ok", "玉钢": "ok",
                                  "冷却材": "zero", "砥石": "zero"})
        self.assertEqual(result, "大成功")

    def test_unreadable_row_is_unknown_not_silently_dropped(self):
        class Maa:
            def __init__(self):
                self.responses = iter([
                    [("15", Point(900, 500))], [],
                    [("8", Point(900, 600))],
                    [("0", Point(900, 650))],
                    [("成功", Point(800, 90))],
                ])

            def ocr_all(self, roi):
                return next(self.responses)

        flow = ExpeditionMixin()
        flow.maa = Maa()
        rewards, status, result = flow._read_settlement_rewards({})

        self.assertEqual(rewards, {"木炭": 15, "冷却材": 8})
        self.assertEqual(status["玉钢"], "unknown")  # 读不清照实标 unknown
        self.assertEqual(result, "成功")

    def test_unreadable_result_is_unknown_not_assumed_success(self):
        class Maa:
            def __init__(self):
                self.responses = iter([
                    [("15", Point(900, 500))],
                    [("22", Point(900, 550))],
                    [("0", Point(900, 600))],
                    [("0", Point(900, 650))],
                    [],  # 结果字样一个都没读出来
                ])

            def ocr_all(self, roi):
                return next(self.responses)

        flow = ExpeditionMixin()
        flow.maa = Maa()
        _rewards, _status, result = flow._read_settlement_rewards({})
        self.assertEqual(result, "unknown")

    def test_failure_result_is_recorded_as_failure(self):
        class Maa:
            def __init__(self):
                self.responses = iter([
                    [("0", Point(900, 500))],
                    [("0", Point(900, 550))],
                    [("0", Point(900, 600))],
                    [("0", Point(900, 650))],
                    [("失败", Point(800, 90))],
                ])

            def ocr_all(self, roi):
                return next(self.responses)

        flow = ExpeditionMixin()
        flow.maa = Maa()
        _rewards, _status, result = flow._read_settlement_rewards({})
        self.assertEqual(result, "失败")


class DispatchVerifyTests(unittest.TestCase):
    """派遣选图验证（2026-09-07 加固）：时代卡/卡位是盲点坐标，
    选完必须全屏 OCR 看到目标图名，否则怕派错队伍不许往下走。"""

    class Maa:
        def __init__(self, map_visible=True):
            self.map_visible = map_visible
            self.ocr_clicks = []
            self.ocr_modes = []

        def screenshot(self, force=False):
            pass

        def click(self, pt):
            self.ocr_clicks.append((pt.x, pt.y))

        def ocr(self, expected, roi, match_mode="contains"):
            self.ocr_modes.append((expected, match_mode))
            return Point(1, 1)

        def ocr_all(self, roi):
            if self.map_visible:
                return [("鸟羽伏见之战", Point(300, 200))]
            return [("全然无关的字", Point(300, 200))]

        def template_match(self, template, roi=None, threshold=0.7):
            return None

        def exists(self, template):
            return False

    CONFIG = {
        "expedition": {
            "eras": {"1": [100, 100]},
            "map_ocr_roi": [0, 100, 1280, 400],
            "team_ui_ocr": {"expected": "部队选择", "roi": [506, 1, 774, 77]},
            "injury_stamp": {"template": "重伤.png"},
            "start_button": {"template": "远征开始.png"},
            "start_ocr": {"expected": "远征开始", "roi": [1000, 600, 1279, 719]},
            "confirm_ocr": {"expected": "确认", "roi": [600, 400, 900, 600]},
            "running_ocr": {"expected": "远征中", "roi": [0, 100, 1280, 400]},
        },
        "team_select": {"teams": {"2": [274, 91]}},
    }

    def _flow(self, maa):
        config = dict(self.CONFIG)

        class Flow(ExpeditionMixin):
            def __init__(self):
                self.maa = maa
                self.config = config
                self.current_location = "远征"
                self.point_clicks = []

            def navigate_to_stream(self, dest):
                return iter(())

            def _click_point(self, pt):
                self.point_clicks.append(tuple(pt))

            def _find_deploy_button(self, cfg):
                return None

        return Flow()

    def test_wrong_map_selection_is_blocked(self):
        # 选完图全屏对不上名字：重选一次仍对不上 → 停，绝不点远征开始
        maa = self.Maa(map_visible=False)
        flow = self._flow(maa)
        with patch("touken.flows.expedition.time.sleep"):
            logs = list(flow.expedition_stream(era=1, map_name="鸟羽", team_no=2))

        fails = [m for m in logs if "怕派错队伍" in m]
        self.assertTrue(fails)
        self.assertTrue(_is_fail(fails[-1]))
        self.assertEqual(maa.ocr_clicks, [(1, 1), (1, 1)])  # 只有两次点小图名

    def test_confirmed_map_dispatches_normally(self):
        # 图名对得上 → 一路走到"已出发"
        maa = self.Maa(map_visible=True)
        flow = self._flow(maa)
        with patch("touken.flows.expedition.time.sleep"):
            logs = list(flow.expedition_stream(era=1, map_name="鸟羽", team_no=2))

        self.assertTrue(any("已出发" in m for m in logs))
        self.assertIn(("远征开始", "exact"), maa.ocr_modes)


# ==================== 结算观察哨（收菜/扫地/导航共用） ====================

SETTLE_CFG = {
    "expedition": {
        "result_title_ocr": {"expected": "远征结果", "roi": [480, 0, 800, 80]},
        "settlement_team_roi": [20, 25, 220, 105],
        "settlement_map_roi": [850, 0, 1270, 60],
        "settlement_rewards": {
            "resource_rois": {
                "木炭": [875, 470, 920, 525],
                "玉钢": [875, 520, 920, 575],
                "冷却材": [875, 570, 920, 625],
                "砥石": [875, 620, 920, 680],
            },
            "result": {"roi": [680, 35, 940, 150]},
            "special_items": {"column_roi": [940, 455, 1095, 705],
                              "empty_ink_max": 0.16, "templates": {}},
        },
    }
}


def _roi_key(roi):
    return tuple(roi.to_tuple()) if hasattr(roi, "to_tuple") else tuple(roi)


def _K(x1, y1, x2, y2):
    return roi_4to4(x1, y1, x2, y2).to_tuple()


def _settle_texts(result_tokens=(), unreadable_rows=()):
    """一张标准结算屏的 OCR 剧本：第2部队从 1-1 鸟羽·伏见回来。"""
    texts = {
        _K(480, 0, 800, 80): [("远征结果", Point(640, 40))],
        _K(20, 25, 220, 105): [("第2部队", Point(100, 60))],
        _K(850, 0, 1270, 60): [("一-一 鸟羽·伏见之战", Point(1000, 30))],
        _K(680, 35, 940, 150): list(result_tokens),
        _K(875, 470, 920, 525): [("15", Point(900, 495))],
        _K(875, 520, 920, 575): [("22", Point(900, 545))],
        _K(875, 570, 920, 625): [("0", Point(900, 595))],
        _K(875, 620, 920, 680): [("0", Point(900, 645))],
    }
    for key in unreadable_rows:
        texts[_K(*key)] = []
    return texts


def _bright_frame():
    """明亮空栏帧：道具栏墨水占比 0 → special_status 应为 none。"""
    return np.full((720, 1280, 3), 200, dtype=np.uint8)


class _SettleMaa:
    def __init__(self, frame, texts, tpl_points=None, save_ok=True):
        self.frame = frame
        self.texts = texts
        self.tpl_points = tpl_points or {}
        self.save_ok = save_ok
        self.saved = []

    def screenshot(self, force=False):
        return self.frame

    def ocr(self, expected, roi, match_mode="contains"):
        for text, pt in self.texts.get(_roi_key(roi), []):
            if expected in str(text):
                return pt
        return None

    def ocr_all(self, roi, image=None):
        return list(self.texts.get(_roi_key(roi), []))

    def template_match(self, template, roi=None, threshold=0.7):
        return self.tpl_points.get(template)

    def save_screenshot(self, path, force=True):
        self.saved.append((path, force))
        if not self.save_ok:
            raise RuntimeError("disk full")
        return True

    def click(self, pt):
        return True


class _SettleFlow(ExpeditionMixin):
    def __init__(self, maa):
        self.maa = maa
        self.config = SETTLE_CFG
        self.events = []

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        return len(self.events)


def _observe(flow, seen, sequence=1):
    with patch("touken.flows.expedition.time.sleep"), \
            patch("touken.flows.expedition._load_exp_record", return_value={}), \
            patch("touken.flows.expedition._save_exp_record"):
        return flow.observe_expedition_settlement(via="collect", seen=seen,
                                                  sequence=sequence)


class SettlementObserverTests(unittest.TestCase):
    def test_settlement_event_is_honest_and_complete(self):
        maa = _SettleMaa(_bright_frame(),
                         _settle_texts(result_tokens=[("大成功", Point(700, 60))]))
        flow = _SettleFlow(maa)
        obs = _observe(flow, seen=[])

        self.assertTrue(obs["new"])
        self.assertEqual(obs["team_no"], 2)
        self.assertEqual((obs["era"], obs["slot"]), (1, 1))
        self.assertEqual(obs["result"], "大成功")
        self.assertEqual(obs["rewards"], {"木炭": 15, "玉钢": 22})
        self.assertEqual(obs["special_status"], "none")
        settled = [p for t, p in flow.events if t == "expedition.settled"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["via"], "collect")
        self.assertEqual(settled[0]["sequence"], 1)
        changes = [p for t, p in flow.events if t == "resource.change"]
        self.assertEqual({c["resource"] for c in changes}, {"木炭", "玉钢"})
        self.assertTrue(all(c["via"] == "collect" for c in changes))

    def test_unknown_result_and_row_never_default_to_success_or_zero(self):
        maa = _SettleMaa(_bright_frame(), _settle_texts(
            result_tokens=[], unreadable_rows=[(875, 570, 920, 625)]))
        flow = _SettleFlow(maa)
        obs = _observe(flow, seen=[])

        self.assertEqual(obs["result"], "unknown")
        self.assertEqual(obs["rewards_status"]["冷却材"], "unknown")
        changes = [p for t, p in flow.events if t == "resource.change"]
        self.assertNotIn("冷却材", {c["resource"] for c in changes})
        settled = [p for t, p in flow.events if t == "expedition.settled"][0]
        self.assertEqual(settled["result"], "unknown")

    def test_same_screen_is_never_recorded_twice(self):
        """转场慢半拍同一屏赖着不走：指纹相同，只记一次账。"""
        maa = _SettleMaa(_bright_frame(),
                         _settle_texts(result_tokens=[("成功", Point(700, 60))]))
        flow = _SettleFlow(maa)
        seen = []
        first = _observe(flow, seen)
        second = _observe(flow, seen, sequence=2)

        self.assertTrue(first["new"])
        self.assertEqual(second, {"new": False})
        self.assertEqual(
            len([1 for t, _p in flow.events if t == "expedition.settled"]), 1)

    def test_new_team_screen_is_a_new_record_even_if_ocr_blind(self):
        """换了队伍的结算屏：「第X部队」字样像素必然不同，OCR 全灭也分得开。"""
        frame2 = _bright_frame()
        frame2[30:60, 30:100] = 50  # 队号标签变了
        maa = _SettleMaa(_bright_frame(),
                         _settle_texts(result_tokens=[("成功", Point(700, 60))]))
        flow = _SettleFlow(maa)
        seen = []
        _observe(flow, seen)
        maa.frame = frame2
        second = _observe(flow, seen, sequence=2)

        self.assertTrue(second["new"])
        self.assertEqual(
            len([1 for t, _p in flow.events if t == "expedition.settled"]), 2)

    def test_special_column_with_content_is_unknown_not_empty(self):
        """道具栏有东西但模板没校准：记 unknown + 墨水量，绝不默认成没给。"""
        frame = _bright_frame()
        frame[480:680, 950:1080] = 40  # 栏里躺着道具图标 + 数量文字
        maa = _SettleMaa(frame, _settle_texts())
        flow = _SettleFlow(maa)
        obs = _observe(flow, seen=[])

        self.assertEqual(obs["special_status"], "unknown")
        self.assertGreater(obs["special_ink"], 0.16)
        settled = [p for t, p in flow.events if t == "expedition.settled"][0]
        self.assertIn("special_ink", settled)
        self.assertFalse(
            any(t == "resource.change" and p["resource"] == "小判"
                for t, p in flow.events))

    def test_special_template_hit_records_item_income(self):
        """模板校准后：图标命中 + 同行数量 → 道具收益进统一资源流水。"""
        cfg = json.loads(json.dumps(SETTLE_CFG))
        cfg["expedition"]["settlement_rewards"]["special_items"]["templates"] = \
            {"小判": "远征/道具_小判.png"}
        texts = _settle_texts()
        texts[_K(940, 455, 1095, 705)] = [("×200", Point(1020, 500))]
        maa = _SettleMaa(_bright_frame(), texts,
                         tpl_points={"远征/道具_小判.png": Point(975, 500)})
        flow = _SettleFlow(maa)
        flow.config = cfg
        obs = _observe(flow, seen=[])

        self.assertEqual(obs["special_status"], "ok")
        self.assertEqual(obs["special_rewards"],
                         [{"name": "小判", "amount": 200}])
        koban = [p for t, p in flow.events
                 if t == "resource.change" and p["resource"] == "小判"]
        self.assertEqual(koban[0]["delta"], 200)
        self.assertEqual(koban[0]["evidence"], "settlement_special_ocr")

    def test_not_a_settlement_screen_returns_none(self):
        maa = _SettleMaa(_bright_frame(), {})
        flow = _SettleFlow(maa)
        self.assertIsNone(_observe(flow, seen=[]))
        self.assertEqual(flow.events, [])


class SpecialUnknownSampleTests(unittest.TestCase):
    """道具栏认不出 → 留同源运行帧供校准；判空/认出/重复屏/存盘失败都守规矩。"""

    def _unknown_frame(self):
        frame = _bright_frame()
        frame[480:680, 950:1080] = 40  # 道具栏有内容
        return frame

    def test_unknown_saves_one_sample(self):
        maa = _SettleMaa(self._unknown_frame(), _settle_texts())
        flow = _SettleFlow(maa)
        obs = _observe(flow, seen=[])
        self.assertEqual(obs["special_status"], "unknown")
        self.assertTrue(obs["special_sample_saved"])
        self.assertEqual(len(maa.saved), 1)
        path, force = maa.saved[0]
        self.assertIn("expedition", path)
        self.assertFalse(force)  # 用缓存稳定帧，不另截图
        # 事件里带着留样事实
        settled = [p for t, p in flow.events if t == "expedition.settled"][0]
        self.assertTrue(settled["special_sample_saved"])

    def test_empty_column_saves_nothing(self):
        maa = _SettleMaa(_bright_frame(), _settle_texts())
        flow = _SettleFlow(maa)
        obs = _observe(flow, seen=[])
        self.assertEqual(obs["special_status"], "none")
        self.assertFalse(obs["special_sample_saved"])
        self.assertEqual(maa.saved, [])

    def test_sticky_screen_does_not_save_twice(self):
        maa = _SettleMaa(self._unknown_frame(), _settle_texts())
        flow = _SettleFlow(maa)
        seen = []
        _observe(flow, seen)
        second = _observe(flow, seen, sequence=2)  # 同一屏没翻动
        self.assertEqual(second, {"new": False})
        self.assertEqual(len(maa.saved), 1)

    def test_same_second_same_via_different_screens_never_collide(self):
        """冻结在同一秒 + via 相同 + sequence=None：两张不同的新结算屏
        必须拿到不同保存路径，谁也不覆盖谁。"""
        frame2 = self._unknown_frame()
        frame2[30:60, 30:100] = 50  # 换了一队的另一张结算屏
        maa = _SettleMaa(self._unknown_frame(), _settle_texts())
        flow = _SettleFlow(maa)
        seen = []
        with patch("touken.flows.expedition.time.strftime",
                   return_value="20260912-120000"), patch(
                       "touken.flows.expedition.time.time", return_value=1234567890.0):
            _observe(flow, seen, sequence=None)
            maa.frame = frame2
            _observe(flow, seen, sequence=None)
        paths = [p for p, _f in maa.saved]
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(set(paths)), 2)

    def test_save_failure_does_not_break_observation(self):
        maa = _SettleMaa(self._unknown_frame(), _settle_texts(),
                         save_ok=False)
        flow = _SettleFlow(maa)
        obs = _observe(flow, seen=[])  # 存盘抛异常也不许炸流程
        self.assertEqual(obs["special_status"], "unknown")
        self.assertFalse(obs["special_sample_saved"])
        self.assertEqual(
            len([1 for t, _p in flow.events if t == "expedition.settled"]), 1)


class SettlementJudgeWordingTests(unittest.TestCase):
    def test_game_failure_wording_is_not_marked_as_script_failure(self):
        msg = ("[收菜] 远征结果结算（第1份）：部队2 从 一-一 鸟羽·伏见之战 回来"
               "（游戏判定「失败」，不是翻车，照实记账）")
        self.assertFalse(_is_fail(msg))

    def test_unknown_wording_stays_green_but_real_failures_still_red(self):
        self.assertFalse(_is_fail(
            "[收菜] 远征结果结算（第1份）：部队2 从 一-一 回来"
            "（结果字样没看清，记 unknown；砥石的数字没看清，记 unknown）"))
        self.assertTrue(_is_fail("[远征] 没找到小图「鸟羽」，停"))


class _SweepSettleMaa(_SettleMaa):
    """扫地现场：结算屏挡住本丸，点跳过点 settle_taps 次后才见目录。"""

    def __init__(self, frame, texts, settle_taps=1):
        super().__init__(frame, texts)
        self.stage = "settle"
        self.settle_taps = settle_taps

    def exists(self, template, roi=None, threshold=0.7):
        return template == "目录.png" and self.stage == "home"

    def ocr(self, expected, roi, match_mode="exact"):
        if self.stage != "settle":
            return None
        return super().ocr(expected, roi)

    def ocr_all(self, roi, image=None):
        if self.stage != "settle":
            return []
        return super().ocr_all(roi)

    def click(self, pt):
        if self.stage == "settle" and (pt.x, pt.y) == (993, 690):
            self.settle_taps -= 1
            if self.settle_taps <= 0:
                self.stage = "home"
        return True


class _SweepSettleFlow(DailyMixin, ExpeditionMixin):
    def __init__(self, maa):
        self.maa = maa
        self.config = SETTLE_CFG
        self.events = []

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        return len(self.events)

    # 生产上由 LoginMixin / Naihanka 提供；本测试现场没有这些弹窗
    def _network_resume_visible(self):
        return False

    def _collect_report_gains(self):
        return []

    def _probe_nav_ready(self):
        return self.maa.stage == "home"


class SweepSettlementTests(unittest.TestCase):
    """通用进本丸路径：扫地撞见远征结算屏，必须先记账再点过。"""

    def _run_sweep(self, settle_taps):
        maa = _SweepSettleMaa(
            _bright_frame(),
            _settle_texts(result_tokens=[("大成功", Point(700, 60))]),
            settle_taps=settle_taps)
        flow = _SweepSettleFlow(maa)
        with patch("touken.flows.daily.time.sleep"), \
                patch("touken.flows.expedition.time.sleep"), \
                patch("touken.flows.expedition._load_exp_record",
                      return_value={}), \
                patch("touken.flows.expedition._save_exp_record"):
            arrived = flow._popup_sweep(max_rounds=10)
        return flow, arrived

    def test_sweep_records_settlement_before_tapping_through(self):
        flow, arrived = self._run_sweep(settle_taps=1)
        self.assertTrue(arrived)
        settled = [p for t, p in flow.events if t == "expedition.settled"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["via"], "popup_sweep")
        self.assertEqual(settled[0]["team_no"], 2)
        self.assertEqual(settled[0]["result"], "大成功")

    def test_sweep_sticky_screen_is_not_double_recorded(self):
        """点一下没翻动（转场慢）：第二轮指纹相同，不重复记账。"""
        flow, arrived = self._run_sweep(settle_taps=2)
        self.assertTrue(arrived)
        self.assertEqual(
            len([1 for t, _p in flow.events if t == "expedition.settled"]), 1)


class _OpenMenuMaa(_SettleMaa):
    """开目录现场：结算屏盖住目录按钮，点跳过后目录按钮回来。"""

    def __init__(self, frame, texts):
        super().__init__(frame, texts)
        self.stage = "settle"

    def exists(self, template, roi=None, threshold=0.7):
        if template == "目录.png":
            return self.stage in ("home", "menu")
        if template == "menu/ui目录.png":
            return self.stage == "menu"
        return False

    def ocr(self, expected, roi, match_mode="contains"):
        if self.stage != "settle":
            return None
        return super().ocr(expected, roi)

    def ocr_all(self, roi, image=None):
        if self.stage != "settle":
            return []
        return super().ocr_all(roi)

    def looks_like_loading(self):
        return False

    def click(self, pt):
        if self.stage == "settle" and (pt.x, pt.y) == (993, 690):
            self.stage = "home"
        return True


class _OpenMenuFlow(NavigationMixin, ExpeditionMixin):
    def __init__(self, maa):
        self.maa = maa
        self.config = {**SETTLE_CFG,
                       "navigation": {"通用入口": {"target": [775, 695]}}}
        self.current_location = None
        self.events = []

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        return len(self.events)

    def _click_point(self, target):
        # 目录按钮点完菜单展开
        if tuple(target) == (775, 695):
            self.maa.stage = "menu"
        return True


class OpenMenuSettlementTests(unittest.TestCase):
    def test_open_menu_records_settlement_before_skipping(self):
        maa = _OpenMenuMaa(
            _bright_frame(),
            _settle_texts(result_tokens=[("成功", Point(700, 60))]))
        flow = _OpenMenuFlow(maa)
        with patch("touken.navigator.time.sleep"), \
                patch("touken.flows.expedition.time.sleep"), \
                patch("touken.flows.expedition._load_exp_record",
                      return_value={}), \
                patch("touken.flows.expedition._save_exp_record"):
            opened = flow._open_menu()

        self.assertTrue(opened)
        settled = [p for t, p in flow.events if t == "expedition.settled"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["via"], "open_menu")


if __name__ == "__main__":
    unittest.main()
