import unittest
from unittest.mock import patch

import numpy as np

from touken.flow_control import FlowAborted
from touken.flows.sugar import SugarMixin
from touken.device_adapter import Point


class Game:
    """Two same-name bodies: first needs two feeds, second needs one."""
    def __init__(self, timeout=False, popup=None):
        self.state = "list"
        self.remaining = [2, 1]
        self.body = 0
        self.picks = 0
        self.feeds = 0
        self.timeout = timeout
        # popup: None 不捣乱 / "late" 第一记习合开始被吞、补点才弹确认 /
        #        "once" 蹦一次怪弹窗、有关闭X / "stuck" 关不掉
        self.popup = popup
        self.popup_seen = False
        self.filter_checked = True
        self.filter_clicks = 0
        self.filter_click_works = True
        self.list_visible = True

    def screenshot(self, **kwargs):
        image = np.full((720, 1280, 3), 255, dtype=np.uint8)
        if self.filter_checked:
            image[96:100, 138:143] = (20, 20, 190)
        image[131:201, 85:94] = (0, 175, 210)
        return image

    def swipe(self, *args):
        return True

    def ocr_all(self, roi, image=None):
        if roi.x == 1180 and self.state == "list" and self.body < 2:
            return [("选择", Point(1220, 166))]
        return []

    def template_match(self, name, *args, **kwargs):
        if name == "习合.png":
            return "tab"
        if name == "通用_关闭.png":
            if self.state == "weird" and self.popup == "once":
                return "closeX"
            return None
        if name == "选择png.png" and self.state == "list" and self.body < 2:
            return "body"
        if name == "一键选择.png" and self.state == "materials" and self.remaining[self.body]:
            return "select"
        if name == "习合开始.png" and self.state == "selected":
            return "go"
        return None

    def ocr(self, text, *args):
        if text == "隐藏无法习合的刀剑":
            return self.state == "list" and self.list_visible
        if text in ("选择", "乱舞"):
            return None
        if text == "是否确认":
            return self.state == "confirm"
        return self.state == "materials"

    def click(self, target):
        if getattr(target, "to_tuple", lambda: None)() == (142, 102):
            self.filter_clicks += 1
            if self.filter_click_works:
                self.filter_checked = not self.filter_checked
            return
        if target == Point(1220, 166):
            target = "body"
        if target == "tab":
            return
        if target == "closeX":
            if self.popup == "once":
                self.state = "materials"  # X 关掉弹窗，露出底下的素材界面
            return
        if target == "body":
            self.picks += 1
            self.state = "materials"
        elif target == "select":
            self.state = "selected"
        elif target == "go":
            if self.popup and not self.popup_seen:
                self.popup_seen = True
                if self.popup == "late":
                    pass  # 第一记被吞：还停在素材界面，弹窗没来
                else:
                    self.state = "weird"
            else:
                self.state = "confirm"
        elif self.state == "confirm":
            self.remaining[self.body] -= 1
            self.feeds += 1
            self.state = "animation" if self.timeout else "materials"
        elif self.state == "materials":
            self.body += 1
            self.state = "list"


class Flow(SugarMixin):
    def __init__(self, timeout=False, popup=None):
        self.device = Game(timeout, popup)
        self.current_location = "强化"

    def navigate_to_stream(self, target):
        yield "navigate"


class SevenGame(Game):
    def __init__(self, rows):
        super().__init__()
        self.levels = [level for level, locked, remaining in rows]
        self.locks = [locked for level, locked, remaining in rows]
        self.remaining = [remaining for level, locked, remaining in rows]
        self.fed_bodies = []
        self.page = 0

    def visible(self):
        rows = [index for index, remaining in enumerate(self.remaining) if remaining]
        return rows[self.page * 5:(self.page + 1) * 5]

    def screenshot(self, **kwargs):
        image = super().screenshot(**kwargs)
        image[120:690, 85:94] = (170, 170, 170)
        image[125:690, 1258:1278] = 20 * (self.page + 1)
        for row_no, index in enumerate(self.visible()):
            center_y = 166 + 101 * row_no
            if self.locks[index]:
                image[center_y - 35:center_y + 35, 85:94] = (0, 175, 210)
        return image

    def swipe(self, x1, y1, x2, y2, duration):
        if y2 > y1:
            self.page = max(0, self.page - 1)
        else:
            count = sum(remaining > 0 for remaining in self.remaining)
            self.page = min(max(0, (count - 1) // 5), self.page + 1)
        return True

    def ocr_all(self, roi, image=None):
        if self.state != "list":
            return []
        if roi.x == 1180:
            return [("选择", Point(1220, 166 + 101 * row_no)) for row_no, index in enumerate(self.visible())]
        for row_no, index in enumerate(self.visible()):
            center_y = 166 + 101 * row_no
            if roi.y == center_y - 46:
                level = self.levels[index]
                return [] if level is None else [(f"刀剑1级乱舞{level}级", Point(600, center_y - 20))]
        return []

    def template_match(self, name, *args, **kwargs):
        if name == "选择png.png":
            return None
        return super().template_match(name, *args, **kwargs)

    def click(self, target):
        if isinstance(target, Point) and target.x == 1220:
            self.body = self.visible()[round((target.y - 166) / 101)]
            return super().click("body")
        if self.state == "materials" and target == Point(141, 25):
            self.state = "list"
            return
        confirming = self.state == "confirm"
        super().click(target)
        if confirming:
            self.levels[self.body] += 1
            self.fed_bodies.append(self.body)


class SugarFlowTests(unittest.TestCase):
    def setUp(self):
        # These tests cover UI transitions after an independently confirmed operation.
        begin = patch("touken.game_state.begin_operation", return_value={})
        confirm = patch("touken.game_state.confirm_operation", return_value=True)
        begin.start()
        confirm.start()
        self.addCleanup(begin.stop)
        self.addCleanup(confirm.stop)

    @patch("touken.flows.sugar.time.sleep")
    def test_feed_current_body_then_pick_next_without_name_filter(self, sleep):
        flow = Flow()
        messages = list(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.feeds, 3)
        self.assertEqual(flow.device.picks, 2)
        self.assertIn("炼了 3 轮", messages[-1])

    @patch("touken.flows.sugar.time.sleep")
    def test_confirmed_operation_survives_animation_timeout(self, sleep):
        flow = Flow(timeout=True)
        messages = []
        with self.assertRaisesRegex(FlowAborted, "回不到素材界面"):
            messages.extend(flow._shugo_loop_stream(False))
        self.assertTrue(any("完成第" in message for message in messages))
        self.assertEqual(flow.device.feeds, 1)

    @patch("touken.flows.sugar.time.sleep")
    def test_dry_run_does_not_feed(self, sleep):
        flow = Flow()
        list(flow._shugo_loop_stream(True))
        self.assertEqual(flow.device.feeds, 0)

    @patch("touken.flows.sugar.time.sleep")
    def test_progress_runs_past_sixty_feeds(self, sleep):
        flow = Flow()
        flow.device.remaining = [80, 1]
        list(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.feeds, 81)

    @patch("touken.flows.sugar.time.sleep")
    def test_repeated_unfeedable_body_stops(self, sleep):
        flow = Flow()
        flow.device.remaining = [0, 0]
        original_click = flow.device.click
        def click(target):
            original_click(target)
            if flow.device.state == "list":
                flow.device.body = 0
        flow.device.click = click
        messages = []
        with self.assertRaisesRegex(FlowAborted, "连续 3 次重选"):
            messages.extend(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.picks, 3)
        self.assertFalse(any("完成第" in message for message in messages))

    @patch("touken.flows.sugar.time.sleep")
    def test_swallowed_tap_recovers_by_reclicking(self, sleep):
        flow = Flow(popup="late")
        messages = list(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.feeds, 3)
        self.assertNotIn("没等到确认弹窗", "".join(messages))
        self.assertNotIn("没反应", "".join(messages))
        self.assertIn("收工：炼了 3 轮", messages[-1])

    @patch("touken.flows.sugar.time.sleep")
    def test_unexpected_popup_is_closed_and_run_continues(self, sleep):
        flow = Flow(popup="once")
        messages = list(flow._shugo_loop_stream(False))
        self.assertIn("没等到确认弹窗", "".join(messages))
        self.assertEqual(flow.device.feeds, 1)
        self.assertIn("收工：炼了 1 轮", messages[-1])

    @patch("touken.flows.sugar.time.sleep")
    def test_stuck_popup_ends_loop_without_crash(self, sleep):
        flow = Flow(popup="stuck")
        messages = []
        with self.assertRaises(FlowAborted):
            messages.extend(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.feeds, 0)
        self.assertIn("没等到确认弹窗", "".join(messages))

    @patch("touken.flows.sugar.time.sleep")
    def test_disabled_filter_is_enabled_before_selecting(self, sleep):
        flow = Flow()
        flow.device.filter_checked = False
        list(flow._shugo_loop_stream(False))
        self.assertTrue(flow.device.filter_checked)
        self.assertEqual(flow.device.filter_clicks, 1)
        self.assertEqual(flow.device.feeds, 3)

    @patch("touken.flows.sugar.time.sleep")
    def test_enabled_filter_is_not_toggled_off(self, sleep):
        flow = Flow()
        list(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.filter_clicks, 0)

    @patch("touken.flows.sugar.time.sleep")
    def test_failed_filter_toggle_stops_before_selecting(self, sleep):
        flow = Flow()
        flow.device.filter_checked = False
        flow.device.filter_click_works = False
        with self.assertRaisesRegex(FlowAborted, "未能开启"):
            list(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.filter_clicks, 1)
        self.assertEqual(flow.device.picks, 0)

    @patch("touken.flows.sugar.time.sleep")
    def test_wrong_page_does_not_toggle_or_select(self, sleep):
        flow = Flow()
        flow.device.list_visible = False
        with self.assertRaisesRegex(FlowAborted, "未确认习合本体列表"):
            list(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.filter_clicks, 0)
        self.assertEqual(flow.device.picks, 0)

    @patch("touken.flows.sugar.time.sleep")
    def test_unknown_checkbox_state_does_not_toggle_or_select(self, sleep):
        for image in (None, np.zeros((720, 1280, 3), dtype=np.uint8),
                      np.zeros((360, 640, 3), dtype=np.uint8)):
            with self.subTest(shape=getattr(image, "shape", None)):
                flow = Flow()
                flow.device.screenshot = lambda **kwargs: image
                with self.assertRaisesRegex(FlowAborted, "未识别.*勾选状态"):
                    list(flow._shugo_loop_stream(False))
                self.assertEqual(flow.device.filter_clicks, 0)
                self.assertEqual(flow.device.picks, 0)

    @patch("touken.flows.sugar.time.sleep")
    def test_selection_text_recovers_missed_button_template(self, sleep):
        flow = Flow()
        original_match = flow.device.template_match
        original_ocr = flow.device.ocr
        flow.device.template_match = lambda name, *args, **kwargs: (
            None if name == "选择png.png" else original_match(name, *args, **kwargs))
        def ocr(text, *args):
            if text == "选择":
                return "body" if flow.device.state == "list" and flow.device.body < 2 else None
            return original_ocr(text, *args)
        flow.device.ocr = ocr
        list(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.feeds, 3)

    @patch("touken.flows.sugar.time.sleep")
    def test_visible_swords_without_button_are_not_treated_as_empty(self, sleep):
        flow = Flow()
        original_match = flow.device.template_match
        original_ocr = flow.device.ocr
        flow.device.ocr_all = lambda *args, **kwargs: []
        flow.device.template_match = lambda name, *args, **kwargs: (
            None if name == "选择png.png" else original_match(name, *args, **kwargs))
        flow.device.ocr = lambda text, *args: text == "乱舞" or original_ocr(text, *args)
        with self.assertRaisesRegex(FlowAborted, "列表仍有刀剑"):
            list(flow._shugo_loop_stream(False))

    @patch("touken.flows.sugar.time.sleep")
    def test_animation_timeout_does_not_start_another_inbox_claim(self, sleep):
        flow = Flow(timeout=True)
        claims = []
        def claim(dry_run):
            claims.append(dry_run)
            yield from ()
            return "claimed"
        flow._inbox_claim_stream = claim
        with self.assertRaises(FlowAborted):
            list(flow.sugar_stream())
        self.assertEqual(len(claims), 1)

    @patch("touken.flows.sugar.time.sleep")
    def test_skips_unlocked_seven_and_feeds_later_bodies(self, sleep):
        flow = Flow()
        flow.device = SevenGame([(7, False, 2), (5, False, 1), (4, False, 1)])
        messages = list(flow._shugo_loop_stream(False, skip_unlocked_seven=True))
        self.assertEqual(flow.device.fed_bodies, [1, 2])
        self.assertEqual(flow.device.remaining[0], 2)
        self.assertTrue(any("乱舞 7 级" in message for message in messages))

    @patch("touken.flows.sugar.time.sleep")
    def test_unlocked_body_reaching_seven_moves_to_next_body(self, sleep):
        flow = Flow()
        flow.device = SevenGame([(6, False, 3), (4, False, 1)])
        list(flow._shugo_loop_stream(False, skip_unlocked_seven=True))
        self.assertEqual(flow.device.fed_bodies, [0, 1])
        self.assertEqual(flow.device.levels[0], 7)
        self.assertEqual(flow.device.remaining[0], 2)

    @patch("touken.flows.sugar.time.sleep")
    def test_yellow_locked_body_continues_past_seven(self, sleep):
        flow = Flow()
        flow.device = SevenGame([(7, True, 2), (8, False, 2)])
        list(flow._shugo_loop_stream(False, skip_unlocked_seven=True))
        self.assertEqual(flow.device.fed_bodies, [0, 0])
        self.assertEqual(flow.device.levels[0], 9)

    @patch("touken.flows.sugar.time.sleep")
    def test_all_skipped_page_does_not_hide_later_candidates(self, sleep):
        flow = Flow()
        flow.device = SevenGame([(7, False, 1)] * 5 + [(4, False, 1)])
        list(flow._shugo_loop_stream(False, skip_unlocked_seven=True))
        self.assertEqual(flow.device.fed_bodies, [5])

    @patch("touken.flows.sugar.time.sleep")
    def test_all_seven_returns_without_feeding(self, sleep):
        flow = Flow()
        flow.device = SevenGame([(7, False, 1), (9, False, 1)])
        list(flow._shugo_loop_stream(False, skip_unlocked_seven=True))
        self.assertEqual(flow.device.feeds, 0)

    @patch("touken.flows.sugar.time.sleep")
    def test_unknown_unlocked_level_does_not_get_fed(self, sleep):
        flow = Flow()
        flow.device = SevenGame([(None, False, 1)])
        with self.assertRaisesRegex(FlowAborted, "未读清"):
            list(flow._shugo_loop_stream(False, skip_unlocked_seven=True))
        self.assertEqual(flow.device.feeds, 0)

    @patch("touken.flows.sugar.time.sleep")
    def test_non_sugar_caller_keeps_feeding_unlocked_seven(self, sleep):
        flow = Flow()
        flow.device = SevenGame([(7, False, 2)])
        list(flow._shugo_loop_stream(False))
        self.assertEqual(flow.device.fed_bodies, [0, 0])
