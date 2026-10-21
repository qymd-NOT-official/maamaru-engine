import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from touken.flows.daily import (
    DailyMixin,
    _is_success_status,
    _snapshot_report_status,
)
from touken.flows.snapshot import SnapshotMixin, _parse_furnace_remain
from touken.device_adapter import Point


class _SnapshotMaa:
    def __init__(self, tokens=None, ocr_error=None):
        self.tokens = tokens or []
        self.ocr_error = ocr_error
        self.clicks = []

    def screenshot(self, force=False):
        return object()

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "menu/目录_所持道具.png":
            return Point(1020, 618)
        return None

    def click(self, point):
        self.clicks.append(point)

    def ocr_all(self, roi):
        if self.ocr_error:
            raise self.ocr_error
        return self.tokens


class _SnapshotFlow(SnapshotMixin):
    def __init__(self, device):
        self.device = device
        self.current_location = "锻刀"

    def _open_menu(self):
        self.current_location = "通用入口"
        return True


class SnapshotCleanupTests(unittest.TestCase):
    def test_reads_koban_without_requiring_page_template_and_always_exits(self):
        device = _SnapshotMaa([("519,967 枚", Point(1080, 50))])
        flow = _SnapshotFlow(device)

        with patch("touken.flows.snapshot.time.sleep"):
            koban = flow._read_koban()

        self.assertEqual(koban, 519967)
        self.assertEqual(device.clicks, [Point(1020, 618), Point(1248, 35)])
        self.assertIsNone(flow.current_location)

    def test_missing_koban_still_exits_and_invalidates_navigation_state(self):
        device = _SnapshotMaa([])
        flow = _SnapshotFlow(device)

        with patch("touken.flows.snapshot.time.sleep"):
            self.assertIsNone(flow._read_koban())

        self.assertEqual(device.clicks[-1], Point(1248, 35))
        self.assertIsNone(flow.current_location)

    def test_ocr_exception_still_exits_and_does_not_escape(self):
        device = _SnapshotMaa(ocr_error=RuntimeError("OCR offline"))
        flow = _SnapshotFlow(device)

        with patch("touken.flows.snapshot.time.sleep"):
            self.assertIsNone(flow._read_koban())

        self.assertEqual(device.clicks[-1], Point(1248, 35))
        self.assertIsNone(flow.current_location)

    def test_parse_furnace_remain(self):
        self.assertEqual(_parse_furnace_remain("1:29:57"), "1:29:57")
        self.assertEqual(_parse_furnace_remain("01：29：57"), "1:29:57")
        # OCR 吃掉冒号 → 连续数字按 H MM SS 切
        self.assertEqual(_parse_furnace_remain("012957"), "1:29:57")
        self.assertEqual(_parse_furnace_remain("12957"), "1:29:57")
        # 分秒超界视为脏识别
        self.assertIsNone(_parse_furnace_remain("19:99:00"))
        self.assertIsNone(_parse_furnace_remain("abc"))
        self.assertIsNone(_parse_furnace_remain(""))

    def test_incomplete_forge_snapshot_is_a_snapshot_warning(self):
        flow = DailyMixin()
        flow._last_full_snapshot_complete = False
        messages = list(flow._closing_snapshot_stream(forge_ran=True))
        status = _snapshot_report_status(messages[-1])

        self.assertEqual(status, "⚠ 小判未读到")
        self.assertFalse(_is_success_status(status))

    def test_report_records_current_run_id(self):
        flow = DailyMixin()
        with tempfile.TemporaryDirectory() as temporary, \
                patch("touken.flows.daily.STATUS_DIR", Path(temporary)), \
                patch.dict("os.environ", {"MAAMARU_RUN_ID": "run-current"}):
            payload = flow._flush_report([("登录", "✓")], finished=True)
            saved = json.loads(
                (Path(temporary) / "latest_report.json").read_text("utf-8"))

        self.assertEqual(payload["run_id"], "run-current")
        self.assertEqual(saved["run_id"], "run-current")


if __name__ == "__main__":
    unittest.main()
