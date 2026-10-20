import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from touken import maa_adapter
from touken.maa_adapter import MAAAdapter, Region


def _make_adapter(connect_ok=True, bind_ok=True):
    adapter = MAAAdapter.__new__(MAAAdapter)
    adapter.adb_path = "adb"
    adapter.adb_address = "127.0.0.1:16384"
    adapter.resource = object()
    adapter.controller = SimpleNamespace(name="旧控制器")
    adapter.tasker = SimpleNamespace(name="旧Tasker")
    adapter._last_image = object()
    adapter._maa_timeouts = 0
    adapter._revive_attempts = 0
    adapter._template_engine = "maa"
    adapter._ocr_engine = "maa"
    adapter._initialized = True

    adb_calls = []
    adapter._adb_run = lambda args, timeout=15.0, binary=False: (
        adb_calls.append(args) or b"")

    job = SimpleNamespace(done=True, succeeded=connect_ok)

    class FakeController:
        def __init__(self, **kwargs):
            pass

        def post_connection(self):
            return job

    class FakeTasker:
        inited = bind_ok

        def bind(self, resource, controller):
            return bind_ok

    return adapter, adb_calls, FakeController, FakeTasker


class MaaReviveTests(unittest.TestCase):
    def test_revive_restarts_adb_rebinds_and_resets_counter(self):
        adapter, adb_calls, FakeController, FakeTasker = _make_adapter()
        with patch.dict(os.environ, {"MAAMARU_WORKER": "1"}), \
                patch.object(maa_adapter, "AdbController", FakeController), \
                patch.object(maa_adapter, "Tasker", FakeTasker), \
                patch.object(maa_adapter.os, "_exit") as mock_exit:
            for _ in range(MAAAdapter.MAX_CONSECUTIVE_TIMEOUTS):
                adapter._note_recognize_timeout()

        mock_exit.assert_not_called()
        self.assertEqual(adb_calls, [
            ["kill-server"],
            ["start-server"],
            ["connect", "127.0.0.1:16384"],
        ])
        # 超时计数清零、缓存截图作废、Tasker 换成新的
        self.assertEqual(adapter._maa_timeouts, 0)
        self.assertIsNone(adapter._last_image)
        self.assertIsInstance(adapter.tasker, FakeTasker)
        self.assertEqual(adapter._revive_attempts, 1)

    def test_failed_revive_still_exits_43(self):
        adapter, _, FakeController, FakeTasker = _make_adapter(connect_ok=False)
        with patch.dict(os.environ, {"MAAMARU_WORKER": "1"}), \
                patch.object(maa_adapter, "AdbController", FakeController), \
                patch.object(maa_adapter, "Tasker", FakeTasker), \
                patch.object(maa_adapter.os, "_exit") as mock_exit:
            for _ in range(MAAAdapter.MAX_CONSECUTIVE_TIMEOUTS):
                adapter._note_recognize_timeout()

        mock_exit.assert_called_once_with(43)

    def test_revive_attempts_are_capped(self):
        """抢救上限用完后，再超时直接了断，不无限抢救。"""
        adapter, _, FakeController, FakeTasker = _make_adapter(bind_ok=False)
        with patch.dict(os.environ, {"MAAMARU_WORKER": "1"}), \
                patch.object(maa_adapter, "AdbController", FakeController), \
                patch.object(maa_adapter, "Tasker", FakeTasker), \
                patch.object(maa_adapter.os, "_exit") as mock_exit:
            for _ in range(MAAAdapter.MAX_CONSECUTIVE_TIMEOUTS * 3):
                adapter._note_recognize_timeout()

        self.assertEqual(adapter._revive_attempts, MAAAdapter.MAX_REVIVE_ATTEMPTS)
        mock_exit.assert_called_with(43)

    def test_non_worker_only_counts(self):
        """非工人环境（手动调试/测试）不抢救也不自杀。"""
        adapter, adb_calls, FakeController, FakeTasker = _make_adapter()
        env = {k: v for k, v in os.environ.items() if k != "MAAMARU_WORKER"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(maa_adapter, "AdbController", FakeController), \
                patch.object(maa_adapter, "Tasker", FakeTasker), \
                patch.object(maa_adapter.os, "_exit") as mock_exit:
            for _ in range(MAAAdapter.MAX_CONSECUTIVE_TIMEOUTS + 1):
                adapter._note_recognize_timeout()

        mock_exit.assert_not_called()
        self.assertEqual(adb_calls, [])
        self.assertEqual(adapter._maa_timeouts, MAAAdapter.MAX_CONSECUTIVE_TIMEOUTS + 1)

    def test_dead_tasker_short_circuits_wait_and_triggers_revive(self):
        """Tasker 暴毙（inited=False）时秒杀，不傻等 30s，直接进抢救。"""
        import time as _time

        adapter, adb_calls, FakeController, FakeTasker = _make_adapter()
        # 暴毙现场：旧 Tasker inited=False，post 出去的 job 永远不 done
        adapter.tasker = SimpleNamespace(
            inited=False,
            post_recognition=lambda *a: SimpleNamespace(done=False))
        adapter.screenshot = lambda force=False: object()
        adapter._record_ocr = lambda *a, **k: None

        with patch.dict(os.environ, {"MAAMARU_WORKER": "1"}), \
                patch.object(maa_adapter, "AdbController", FakeController), \
                patch.object(maa_adapter, "Tasker", FakeTasker):
            start = _t = _time.time()
            result = adapter.ocr("本丸", Region(0, 0, 10, 10))
            elapsed = _time.time() - _t

        self.assertIsNone(result)
        self.assertLess(elapsed, 5)
        self.assertTrue(adb_calls)  # 直接进了抢救，没等超时
        self.assertIsInstance(adapter.tasker, FakeTasker)


class AbSnapshotTests(unittest.TestCase):
    def test_divergence_snapshot_saved_under_debug_dir(self):
        import pathlib
        import tempfile

        import numpy as np

        from touken import runtime_paths
        img = np.zeros((100, 200, 3), dtype=np.uint8)
        roi = Region(10, 20, 50, 30)
        with tempfile.TemporaryDirectory() as td, \
                patch.object(runtime_paths, "DEBUG_DIR", pathlib.Path(td)):
            maa_adapter.MAAAdapter._ab_snapshot(img, roi, "编队")
            files = list((pathlib.Path(td) / "ocr_ab").glob("*.png"))
        self.assertEqual(len(files), 1)
        self.assertIn("编队", files[0].name)


if __name__ == "__main__":
    unittest.main()
