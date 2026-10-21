import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from touken.flows import naihanka_report as nr
from touken.flows.daily import DailyMixin
from touken.flows.naihanka import NaihankaMixin
from touken.device_adapter import Point

FIXTURE = Path(__file__).parent / "fixtures" / "naihanka_report.png"


def _pt(x, y):
    return SimpleNamespace(x=x, y=y)


class BadgeDetectionTests(unittest.TestCase):
    """真机报告屏（安宅切机动+1、长谷部侦察+1、博多侦察+1；小龙喂满金框无徽章）"""

    def test_fixture_finds_exactly_three_badges(self):
        img = cv2.imread(str(FIXTURE))
        self.assertEqual(nr.find_plus1_badges(img),
                         {(0, 0, 1), (1, 0, 1), (1, 1, 1)})

    def test_blank_images_have_no_badge(self):
        self.assertEqual(nr.find_plus1_badges(
            np.zeros((720, 1280, 3), np.uint8)), set())
        self.assertEqual(nr.find_plus1_badges(
            np.full((720, 1280, 3), 255, np.uint8)), set())
        self.assertEqual(nr.find_plus1_badges(None), set())

    def test_read_report_gains_maps_name_and_stat(self):
        img = cv2.imread(str(FIXTURE))
        tokens_by_region = {
            0: [("安宅切", _pt(100, 485))],
            1: [("压切长谷部", _pt(500, 480)), ("博多藤四郎", _pt(500, 570))],
            2: [("小龙景光", _pt(950, 480))],
        }
        calls = []

        def fake_ocr(roi):
            calls.append(roi)
            return tokens_by_region.get(len(calls) - 1, [])

        gains = nr.read_report_gains(img, fake_ocr)
        by_name = {g["name"]: g["stat"] for g in gains}
        self.assertEqual(by_name, {
            "安宅切": "机动", "压切长谷部": "侦察", "博多藤四郎": "侦察"})

    def test_read_report_gains_falls_back_to_slot_label(self):
        img = cv2.imread(str(FIXTURE))
        gains = nr.read_report_gains(img, lambda roi: [])
        slots = {g["slot"] for g in gains}
        self.assertEqual(slots, {"饲马上位", "耕作上位", "耕作下位"})
        # 名字认不到也要说实话，slot 标签顶替
        self.assertTrue(all(g["name"] == g["slot"] for g in gains))

    def test_no_badge_no_ocr_calls(self):
        img = np.zeros((720, 1280, 3), np.uint8)
        gains = nr.read_report_gains(img, lambda roi: 1 / 0)
        self.assertEqual(gains, [])


class ParseColumnTokensTests(unittest.TestCase):
    def test_digits_and_names_assigned_by_position(self):
        tokens = [
            ("安宅切", _pt(100, 420)),       # 上槽名字
            ("49", _pt(375, 400)),           # 上槽左（x<mid）
            ("48", _pt(415, 400)),           # 上槽右
            ("巴形薙刀", _pt(100, 505)),     # 下槽名字
            ("94", _pt(375, 495)), ("61", _pt(415, 495)),
            ("获取物", _pt(100, 600)),       # 垃圾词元，名册里没有
            ("4B8", _pt(375, 999)),          # 非纯数字，丢
        ]
        slots = nr.parse_column_tokens(tokens, mid_x=400,
                                       values_split_y=450, names_split_y=478)
        self.assertEqual(slots[0]["name"], "安宅切")
        self.assertEqual(slots[0]["values"], [49, 48])
        self.assertEqual(slots[1]["name"], "巴形薙刀")
        self.assertEqual(slots[1]["values"], [94, 61])

    def test_empty_tokens(self):
        slots = nr.parse_column_tokens([], 400, 450, 478)
        self.assertEqual([s["name"] for s in slots], [None, None])
        self.assertEqual([s["values"] for s in slots],
                         [[None, None], [None, None]])


class DiffSnapshotTests(unittest.TestCase):
    def test_pure_gain_reported(self):
        old = {"安宅切": {"防御": 49, "机动": 47}}
        new = {"安宅切": {"防御": 49, "机动": 48}}
        self.assertEqual(nr.diff_snapshots(old, new),
                         [{"name": "安宅切", "stat": "机动", "old": 47, "new": 48}])

    def test_new_face_and_drop_ignored(self):
        old = {"小龙景光": {"打击": 127}}
        new = {"小豆长光": {"打击": 80}, "小龙景光": {"打击": 100}}
        self.assertEqual(nr.diff_snapshots(old, new), [])

    def test_empty_old_reports_nothing(self):
        self.assertEqual(nr.diff_snapshots({}, {"安宅切": {"机动": 48}}), [])
        self.assertEqual(nr.diff_snapshots(None, {"安宅切": {"机动": 48}}), [])


class TableDuplicateNameTests(unittest.TestCase):
    def test_same_name_in_two_slots_is_not_overwritten_as_one_person(self):
        from touken.device_adapter import roi_4to4
        farm_roi = roi_4to4(*nr.TABLE_OCR_REGIONS[1]).to_tuple()

        def ocr(roi):
            if roi.to_tuple() != farm_roi:
                return []
            return [("压切长谷部", _pt(500, 420)),
                    ("55", _pt(760, 400)), ("39", _pt(820, 400)),
                    ("压切长谷部", _pt(500, 510)),
                    ("56", _pt(760, 490)), ("40", _pt(820, 490))]

        self.assertNotIn("压切长谷部", nr.read_table_stats(ocr))


class _FakeMaa:
    """观察窗剧本：先报告屏后内番表。"""

    def __init__(self, img, report_frames=1):
        self.img = img
        self.report_frames = report_frames
        self.ocr_calls = 0

    def screenshot(self, force=False):
        return self.img

    def ocr(self, expected, roi, **kw):
        if expected == "内番报告" and self.ocr_calls < self.report_frames:
            self.ocr_calls += 1
            return _pt(640, 340)
        return None

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "ui内番.png" and self.ocr_calls >= self.report_frames:
            return _pt(40, 60)
        return None

    def ocr_all(self, roi):
        return []

    def click(self, target):
        return True


class _Flow(NaihankaMixin):
    def __init__(self, device):
        self.device = device
        self.config = {"naihanka": {
            "running_marker": {"template": "内番中.png"},
            "ui_title": {"template": "ui内番.png"},
            "cancel_button": {"template": "通用_取消.png"},
            "skip_tap": [993, 690],
        }}
        self.clicks = []
        self.events = []

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))

    def _click_point(self, target):
        self.clicks.append(list(target) if isinstance(target, tuple) else target)
        return True


class ObserveStreamTests(unittest.TestCase):
    def test_report_read_then_tapped_through(self):
        img = cv2.imread(str(FIXTURE))
        device = _FakeMaa(img, report_frames=1)
        flow = _Flow(device)
        with tempfile.TemporaryDirectory() as td, \
                patch("touken.flows.naihanka.STATUS_DIR", Path(td)), \
                patch("touken.flows.naihanka.time.sleep"):
            messages = list(flow.naihanka_observe_stream())
        # 报告屏读到三枚徽章（名字 OCR 空 → 槽位兜底）
        self.assertEqual(sum("🎉" in m for m in messages), 3)
        self.assertTrue(any("饲马上位 机动+1" in m for m in messages))
        self.assertIn([993, 690], flow.clicks)  # 点穿动画回表屏

    def test_plain_table_exits_immediately_without_clicks(self):
        device = _FakeMaa(None, report_frames=0)
        flow = _Flow(device)
        with patch("touken.flows.naihanka.time.sleep"):
            messages = list(flow.naihanka_observe_stream())
        self.assertEqual(messages, [])
        self.assertEqual(flow.clicks, [])


class _LoginPopupMaa:
    """登录扫地剧本：报告屏必须点掉后才会露出本丸目录。"""

    def __init__(self):
        self.stage = "report"
        self.clicks = []

    def screenshot(self, force=False):
        return None

    def template_match(self, template, roi=None, threshold=0.7):
        return None

    def ocr(self, expected, roi, **kw):
        if expected == "内番报告" and self.stage == "report":
            return _pt(640, 340)
        return None

    def exists(self, template, roi=None, threshold=0.7):
        return template == "目录.png" and self.stage == "home"

    def click(self, target):
        self.clicks.append(target)
        if target == Point(993, 690):
            self.stage = "home"
        return True


class _LoginPopupFlow(DailyMixin):
    def __init__(self):
        self.device = _LoginPopupMaa()
        self.report_reads = 0

    # 生产上由 LoginMixin 提供；扫地测试默认没有续打弹窗
    def _network_resume_visible(self):
        return False

    def _collect_report_gains(self):
        self.report_reads += 1
        return ["[内番] 测试刀剑男士 机动+1"]

    def _probe_nav_ready(self):
        return self.device.stage == "home"


class LoginPopupSweepTests(unittest.TestCase):
    def test_report_is_read_then_closed_before_home_is_accepted(self):
        flow = _LoginPopupFlow()

        with patch("touken.flows.daily.time.sleep"):
            arrived = flow._popup_sweep(max_rounds=5)

        self.assertTrue(arrived)
        self.assertEqual(flow.report_reads, 1)
        self.assertEqual(flow.device.clicks, [Point(993, 690)])

    def test_late_login_button_is_clicked_before_waiting_for_home(self):
        class Maa(_LoginPopupMaa):
            def __init__(self):
                super().__init__()
                self.stage = "login"
                self.login_point = Point(640, 600)

            def template_match(self, template, roi=None, threshold=0.7):
                if template == "登录.png" and self.stage == "login":
                    return self.login_point
                return None

            def click(self, target):
                self.clicks.append(target)
                if target == self.login_point:
                    self.stage = "home"
                return True

        flow = _LoginPopupFlow()
        flow.device = Maa()

        with patch("touken.flows.daily.time.sleep"):
            arrived = flow._popup_sweep(max_rounds=5)

        self.assertTrue(arrived)
        self.assertEqual(flow.device.clicks, [flow.device.login_point])

    def test_stale_resume_popup_is_dismissed_with_no(self):
        # 隔夜中断的续打弹窗：脚本对断点没记忆，点【否】清掉回本丸，
        # 不能点【是】闭眼进战斗（2026-09-08 老大手动中断后任务卡死）
        class Maa(_LoginPopupMaa):
            def __init__(self):
                super().__init__()
                self.stage = "resume"
                self.no_point = Point(800, 467)

            def ocr(self, expected, roi, **kw):
                if expected == "否" and self.stage == "resume":
                    return self.no_point
                return super().ocr(expected, roi, **kw)

            def click(self, target):
                self.clicks.append(target)
                if target == self.no_point:
                    self.stage = "home"
                return True

        class Flow(_LoginPopupFlow):
            def _network_resume_visible(self):
                return self.device.stage == "resume"

        flow = Flow()
        flow.device = Maa()

        with patch("touken.flows.daily.time.sleep"):
            arrived = flow._popup_sweep(max_rounds=5)

        self.assertTrue(arrived)
        self.assertEqual(flow.device.clicks, [flow.device.no_point])


class DailyLoginGateTests(unittest.TestCase):
    class Flow(DailyMixin):
        def __init__(self, popup_result=False, login_error=None):
            self.config = {"daily": {}}
            self.popup_result = popup_result
            self.login_error = login_error
            self.flushed = []

        def _daily_update_gate(self):
            if False:
                yield None
            return True

        def _ensure_game_started(self):
            if False:
                yield None
            return True

        def login(self):
            if self.login_error:
                raise self.login_error

        def _popup_sweep(self, max_rounds=30):
            return self.popup_result

        def _flush_report(self, report, finished):
            self.flushed.append((list(report), finished))

    def test_daily_stops_when_home_was_not_confirmed(self):
        flow = self.Flow(popup_result=False)

        with patch("touken.flows.daily.time.sleep"):
            messages = list(flow.daily_stream())

        self.assertIn("[日课] 登录后仍没到本丸，本次日课停止", messages)
        self.assertFalse(any("②" in message for message in messages))
        self.assertEqual(flow.flushed, [([("登录", "✗ 没到本丸")], True)])

    def test_daily_stops_when_login_raises(self):
        flow = self.Flow(login_error=RuntimeError("登录按钮没点到"))

        with patch("touken.flows.daily.time.sleep"):
            messages = list(flow.daily_stream())

        self.assertTrue(any("未确认进入本丸，本次日课停止" in message
                            for message in messages))
        self.assertEqual(flow.flushed,
                         [([("登录", "✗ 登录按钮没点到")], True)])


class CollectReportGainsTests(unittest.TestCase):
    def test_reads_names_writes_last_report_and_is_idempotent(self):
        img = cv2.imread(str(FIXTURE))

        class Maa(_FakeMaa):
            def ocr_all(self, roi):
                t = roi.to_tuple()
                if t == (55, 360, 395, 250):   # 报告屏栏 0
                    return [("安宅切", _pt(100, 485))]
                if t == (440, 360, 395, 250):  # 栏 1
                    return [("压切长谷部", _pt(500, 480)),
                            ("博多藤四郎", _pt(500, 570))]
                return []

        flow = _Flow(Maa(img, report_frames=0))
        with tempfile.TemporaryDirectory() as td, \
                patch("touken.flows.naihanka.STATUS_DIR", Path(td)), \
                patch("touken.flows.naihanka.time.sleep"):
            messages = flow._collect_report_gains()
            self.assertEqual(sum("🎉" in m for m in messages), 3)
            self.assertTrue(any("安宅切 机动+1" in m for m in messages))
            self.assertTrue(any("压切长谷部 侦察+1" in m for m in messages))
            state = json.loads((Path(td) / "naihanka.json").read_text("utf-8"))
            self.assertEqual(len(state["last_report"]["gains"]), 3)
            # 幂等：本次会话再叫一遍不重读
            self.assertEqual(flow._collect_report_gains(), [])
            # 成绩单「全部记录」留痕
            self.assertEqual(len(flow.events), 1)
            etype, payload = flow.events[0]
            self.assertEqual(etype, "naihanka.gains")
            self.assertEqual(payload["source"], "report")
            self.assertEqual(len(payload["gains"]), 3)


class SnapshotStreamTests(unittest.TestCase):
    def _maa_with_table(self):
        from touken.device_adapter import roi_4to4
        col0 = roi_4to4(*nr.TABLE_OCR_REGIONS[0]).to_tuple()

        class Maa(_FakeMaa):
            def ocr_all(self, roi):
                # 栏 0：安宅切 49/48
                if roi.to_tuple() == col0:
                    return [("安宅切", _pt(100, 420)),
                            ("49", _pt(375, 400)), ("48", _pt(415, 400))]
                return []
        return Maa(None, report_frames=0)

    def test_diff_gain_reported_and_snapshot_saved(self):
        flow = _Flow(self._maa_with_table())
        flow._naihanka_gains = []
        with tempfile.TemporaryDirectory() as td, \
                patch("touken.flows.naihanka.STATUS_DIR", Path(td)):
            (Path(td) / "naihanka.json").write_text(json.dumps({
                "started_at": "2026-08-24 08:00:00",
                "stats": {"安宅切": {"防御": 49, "机动": 47}},
            }), encoding="utf-8")
            messages = list(flow.naihanka_snapshot_stream())
            self.assertTrue(any("安宅切 机动 47→48" in m for m in messages))
            state = json.loads((Path(td) / "naihanka.json").read_text("utf-8"))
            # 新快照落盘，且 started_at 没被冲掉
            self.assertEqual(state["stats"]["安宅切"], {"防御": 49, "机动": 48})
            self.assertEqual(state["started_at"], "2026-08-24 08:00:00")
            # diff 独占的 +1 也留痕，来源如实标 diff
            self.assertEqual(flow.events, [("naihanka.gains", {
                "source": "diff",
                "gains": [{"name": "安宅切", "stat": "机动", "old": 47, "new": 48}],
            })])

    def test_badge_gain_not_double_reported(self):
        flow = _Flow(self._maa_with_table())
        flow._naihanka_gains = [{"name": "安宅切", "stat": "机动", "slot": "饲马上位"}]
        with tempfile.TemporaryDirectory() as td, \
                patch("touken.flows.naihanka.STATUS_DIR", Path(td)):
            (Path(td) / "naihanka.json").write_text(json.dumps({
                "stats": {"安宅切": {"防御": 49, "机动": 47}},
            }), encoding="utf-8")
            messages = list(flow.naihanka_snapshot_stream())
            self.assertEqual(messages, [])  # 徽章播报过的不重复
            self.assertEqual(flow.events, [])  # 也不重复留痕
            # 快照照样落盘更新
            state = json.loads((Path(td) / "naihanka.json").read_text("utf-8"))
            self.assertEqual(state["stats"]["安宅切"]["机动"], 48)

    def test_first_run_silently_saves(self):
        flow = _Flow(self._maa_with_table())
        flow._naihanka_gains = []
        with tempfile.TemporaryDirectory() as td, \
                patch("touken.flows.naihanka.STATUS_DIR", Path(td)):
            messages = list(flow.naihanka_snapshot_stream())
            self.assertEqual(messages, [])  # 没旧快照不瞎报
            self.assertTrue((Path(td) / "naihanka.json").exists())

    def test_cultivation_gain_keeps_unique_instance_evidence(self):
        from touken.device_adapter import roi_4to4
        farm_roi = roi_4to4(*nr.TABLE_OCR_REGIONS[1]).to_tuple()

        class Maa(_FakeMaa):
            def ocr_all(self, roi):
                if roi.to_tuple() == farm_roi:
                    return [("压切长谷部", _pt(500, 420)),
                            ("56", _pt(760, 400)), ("39", _pt(820, 400))]
                return []

        pool = {"done": True, "source": {"snapshot_id": 24},
                "entries": [{"observation_id": "24:1",
                             "name_zh": "压切长谷部",
                             "stats": {"生存": 55, "侦察": 39}}]}
        flow = _Flow(Maa(None, report_frames=0))
        flow._naihanka_gains = []
        with tempfile.TemporaryDirectory() as td, \
                patch("touken.flows.naihanka.STATUS_DIR", Path(td)), \
                patch("touken.honmaru_profile.get_honmaru_profile",
                      return_value={"candidate_pool": pool}):
            (Path(td) / "naihanka.json").write_text(json.dumps({
                "stats": {"压切长谷部": {"生存": 55, "侦察": 39}},
            }), encoding="utf-8")
            list(flow.naihanka_snapshot_stream())
            state = json.loads((Path(td) / "naihanka.json").read_text("utf-8"))
            self.assertEqual(state["cultivation_evidence"]["24:1"]["生存"], 56)


if __name__ == "__main__":
    unittest.main()
