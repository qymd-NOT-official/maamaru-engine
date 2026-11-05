# -*- coding: utf-8 -*-
"""翻车断点存取：落盘/读取/驳回、篡改拒绝、坏 JSON 容忍、运行态字段剔除。"""

import tempfile
import unittest
from pathlib import Path

from panel import workflow_breakpoint as wb


def _resume(next_index=1, report_len=1, reason="failure"):
    return {"nodes": [{"type": "signin", "params": {}},
                      {"type": "naihanka", "params": {}}],
            "next_index": next_index,
            "report": [["签到", "✓"]] * report_len,
            "reason": reason, "game_closed": False, "forge_ran": False}


class BreakpointStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="workflow_bp_test_")
        self.path = Path(self._tmp.name) / "workflow_breakpoint.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_load_roundtrip_and_strips_runtime_keys(self):
        self.assertTrue(wb.save(
            "run1", "wf-1", "cfg.json",
            {"workflow_id": "wf-1", "daily_bootstrap_version": 1,
             "workflow_resume": {"reason": "failure"}, "workflow_wait_root": "root"},
            _resume(), "万屋领免费礼包", path=self.path))
        bp = wb.load(path=self.path)
        self.assertEqual(bp["run_id"], "run1")
        self.assertEqual(bp["workflow_id"], "wf-1")
        self.assertEqual(bp["failed_label"], "万屋领免费礼包")
        self.assertEqual(bp["resume"]["next_index"], 1)
        # 上一轮运行态字段必须剔除，续跑时才不会触发等待状态检查或嵌套续跑
        self.assertNotIn("workflow_resume", bp["params"])
        self.assertNotIn("workflow_wait_root", bp["params"])
        self.assertEqual(bp["params"]["workflow_id"], "wf-1")
        self.assertEqual(bp["params"]["daily_bootstrap_version"], 1)

    def test_load_missing_or_bad_json_returns_none(self):
        self.assertIsNone(wb.load(path=self.path))
        self.path.write_text("{这不是 JSON", encoding="utf-8")
        self.assertIsNone(wb.load(path=self.path))

    def test_tampered_resume_rejected(self):
        # next_index 越界
        self.assertFalse(wb.save("r", "w", "c", {}, _resume(next_index=5), "x",
                                 path=self.path))
        # report 长度与 next_index 不符
        self.assertFalse(wb.save("r", "w", "c", {}, _resume(report_len=0), "x",
                                 path=self.path))
        # 缺 reason=failure
        self.assertFalse(wb.save("r", "w", "c", {}, _resume(reason="expedition"), "x",
                                 path=self.path))
        # nodes 为空
        self.assertFalse(wb.save("r", "w", "c", {},
                                 {**_resume(), "nodes": []}, "x", path=self.path))
        self.assertIsNone(wb.load(path=self.path))

    def test_tampered_file_on_disk_is_rejected(self):
        wb.save("r", "w", "c", {}, _resume(), "x", path=self.path)
        import json
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["breakpoint"]["resume"]["next_index"] = 99
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.assertIsNone(wb.load(path=self.path))

    def test_clear_matches_workflow_id(self):
        wb.save("r", "wf-1", "c", {}, _resume(), "x", path=self.path)
        self.assertFalse(wb.clear("wf-2", path=self.path))  # 别家的断点不动
        self.assertIsNotNone(wb.load(path=self.path))
        self.assertTrue(wb.clear("wf-1", path=self.path))
        self.assertIsNone(wb.load(path=self.path))

    def test_clear_all_and_idempotent(self):
        wb.save("r", "wf-1", "c", {}, _resume(), "x", path=self.path)
        self.assertTrue(wb.clear(path=self.path))
        self.assertFalse(wb.clear(path=self.path))


if __name__ == "__main__":
    unittest.main()
