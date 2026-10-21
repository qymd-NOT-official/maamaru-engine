import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from touken.flows.sortie import SortieMixin


class _ScreenshotRecorder:
    def __init__(self):
        self.paths = []

    def save_screenshot(self, path, force=False):
        self.paths.append((Path(path), force))
        return True


class MapFeedbackTests(unittest.TestCase):
    def test_only_map_misses_are_saved_and_capped(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("touken.runtime_paths.STATUS_DIR", Path(tmp)):
            flow = SortieMixin()
            flow.device = _ScreenshotRecorder()
            flow._map_miss_count = 0

            for loop_no in range(1, 8):
                flow._save_map_miss(7, 4, loop_no)

            self.assertEqual(len(flow.device.paths), 5)
            self.assertTrue(all(path.parent.name == "map_miss" for path, _ in flow.device.paths))
            self.assertTrue(all(path.name.startswith("miss_7-4_") for path, _ in flow.device.paths))
            self.assertTrue(all(force is False for _, force in flow.device.paths))

    def test_sortie_has_no_automatic_success_or_retreat_frame_capture(self):
        source = Path(__file__).resolve().parents[1] / "touken" / "flows" / "sortie.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn('kind="ok"', text)
        self.assertNotIn('kind="retreat"', text)
        self.assertNotIn("_save_map_frame", text)


if __name__ == "__main__":
    unittest.main()
