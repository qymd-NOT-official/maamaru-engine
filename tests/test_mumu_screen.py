"""MuMu 显存截图通道的单元测试。dll 用 mock，真机验证走手动。"""
import ctypes
import unittest
from pathlib import Path
from unittest import mock

from touken import mumu_screen
from touken.mumu_screen import MumuScreen


def _fake_lib(frame_rgba: bytes | None = None, width: int = 4, height: int = 2,
              query_rc: int = 0, capture_rc: int = 0, handle: int = 7):
    """造一个假的 external_renderer_ipc。frame_rgba 是 w*h*4 的原始帧。"""
    lib = mock.MagicMock()
    lib.nemu_connect.return_value = handle

    def capture(h, display_id, buf_size, w_ptr, h_ptr, pixels):
        w = ctypes.cast(w_ptr, ctypes.POINTER(ctypes.c_int))
        h = ctypes.cast(h_ptr, ctypes.POINTER(ctypes.c_int))
        if buf_size == 0:
            if query_rc != 0:
                return query_rc
            w.contents.value = width
            h.contents.value = height
            return 0
        if capture_rc != 0:
            return capture_rc
        data = frame_rgba if frame_rgba is not None else bytes(buf_size)
        for i, b in enumerate(data):
            pixels[i] = b
        return 0

    lib.nemu_capture_display.side_effect = capture
    return lib


def _frozen_lib(state, w=2, h=2):
    """帧内容随时可换的假 dll：state['frame'] 是当前吐的帧。"""
    lib = mock.MagicMock()
    lib.nemu_connect.return_value = 7

    def capture(handle, display_id, buf_size, w_ptr, h_ptr, pixels):
        wp = ctypes.cast(w_ptr, ctypes.POINTER(ctypes.c_int))
        hp = ctypes.cast(h_ptr, ctypes.POINTER(ctypes.c_int))
        if buf_size == 0:
            wp.contents.value = w
            hp.contents.value = h
            return 0
        data = state["frame"]
        for i, b in enumerate(data):
            pixels[i] = b
        return 0

    lib.nemu_capture_display.side_effect = capture
    return lib


class FromAdbTests(unittest.TestCase):
    def test_standard_mumu_layout(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            shell = Path(tmp) / "MuMuPlayer" / "nx_device" / "12.0" / "shell"
            (shell / "sdk").mkdir(parents=True)
            (shell / "sdk" / "external_renderer_ipc.dll").write_bytes(b"x")
            (shell / "adb.exe").write_bytes(b"x")
            ms = MumuScreen.from_adb(str(shell / "adb.exe"), "127.0.0.1:16384")
            self.assertIsNotNone(ms)
            self.assertEqual(ms.instance_index, 0)
            self.assertEqual(ms.install_root,
                             (Path(tmp) / "MuMuPlayer").resolve())
            ms2 = MumuScreen.from_adb(str(shell / "adb.exe"), "127.0.0.1:16480")
            self.assertEqual(ms2.instance_index, 3)  # 16384 + 32*3

    def test_non_mumu_layout_returns_none(self):
        with mock.patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(MumuScreen.from_adb("C:/adb/adb.exe",
                                                  "127.0.0.1:5555"))


class CaptureTests(unittest.TestCase):
    def _channel(self):
        return MumuScreen(Path("C:/MuMu"), Path("C:/MuMu/sdk/x.dll"))

    def test_capture_converts_rgba_to_bgr_and_flips(self):
        # 2x2 帧：上行[红, 绿]，下行[蓝, 白]（RGBA）
        frame = bytes([
            255, 0, 0, 255,  0, 255, 0, 255,      # 上行
            0, 0, 255, 255,  255, 255, 255, 255,  # 下行
        ])
        channel = self._channel()
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_fake_lib(frame, 2, 2)):
            img = channel.capture()
        self.assertIsNotNone(img)
        self.assertEqual(img.shape, (2, 2, 3))
        # 垂直翻转：原下行变第一行；RGBA→BGR：蓝(0,0,255) 仍是蓝
        self.assertEqual(list(img[0, 0]), [255, 0, 0])   # 蓝 BGR
        self.assertEqual(list(img[0, 1]), [255, 255, 255])
        self.assertEqual(list(img[1, 0]), [0, 0, 255])   # 红 BGR
        self.assertEqual(list(img[1, 1]), [0, 255, 0])   # 绿

    def test_query_failure_returns_none_and_counts(self):
        channel = self._channel()
        lib = _fake_lib(query_rc=-1)
        with mock.patch.object(mumu_screen.ctypes, "CDLL", return_value=lib):
            for _ in range(mumu_screen._MAX_FAILURES):
                self.assertIsNone(channel.capture())
        self.assertTrue(channel._dead)
        # 判死后不再碰 dll
        lib.nemu_connect.reset_mock()
        self.assertIsNone(channel.capture())
        lib.nemu_connect.assert_not_called()

    def test_connect_failure_returns_none(self):
        channel = self._channel()
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_fake_lib(handle=0)):
            self.assertIsNone(channel.capture())

    def test_dll_missing_marks_dead(self):
        channel = self._channel()
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               side_effect=OSError("没有这个 dll")):
            self.assertIsNone(channel.capture())
        self.assertTrue(channel._dead)

    def test_recovery_after_transient_failure(self):
        # 一次抽风后下一次能重连恢复，不永久判死
        channel = self._channel()
        state = {"rc": -1}
        lib = _fake_lib(bytes(4 * 2 * 4), 2, 4)
        orig = lib.nemu_capture_display.side_effect

        def flaky(h, display_id, buf_size, w_ptr, h_ptr, pixels):
            if buf_size != 0 and state["rc"] != 0:
                return state["rc"]
            return orig(h, display_id, buf_size, w_ptr, h_ptr, pixels)

        lib.nemu_capture_display.side_effect = flaky
        with mock.patch.object(mumu_screen.ctypes, "CDLL", return_value=lib):
            self.assertIsNone(channel.capture())
            self.assertFalse(channel._dead)
            state["rc"] = 0  # 链路恢复了
            img = channel.capture()
        self.assertIsNotNone(img)
        self.assertEqual(img.shape, (4, 2, 3))

    def test_frozen_channel_reconnects_then_recovers(self):
        # 冻帧撞线 → 当次返回 None（调用方回退 ADB）；MuMu 复活后自动恢复
        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel()
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_frozen_lib(state)):
            for _ in range(mumu_screen._STALE_LIMIT):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())  # 撞线，断开重连
            self.assertFalse(channel._dead)
            state["frame"] = bytes([1]) * (4 * 2 * 2)  # MuMu 复活，画面变了
            self.assertIsNotNone(channel.capture())

    def test_frozen_channel_dies_after_revive_fails(self):
        # 重连后还是同一帧 → 本场判死，不再碰 dll
        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel()
        lib = _frozen_lib(state)
        with mock.patch.object(mumu_screen.ctypes, "CDLL", return_value=lib):
            for _ in range(mumu_screen._STALE_LIMIT):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())  # 第一次撞线：重连
            self.assertFalse(channel._dead)
            for _ in range(mumu_screen._STALE_LIMIT - 1):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())  # 第二次撞线：判死
            self.assertTrue(channel._dead)
            lib.nemu_connect.reset_mock()
            self.assertIsNone(channel.capture())
            lib.nemu_connect.assert_not_called()

    def test_live_frames_never_trigger_stale_detection(self):
        # 帧 alternating 变化，撞不到冻帧线
        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel()
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_frozen_lib(state)):
            for i in range(mumu_screen._STALE_LIMIT * 3):
                state["frame"] = bytes([i % 256]) * (4 * 2 * 2)
                self.assertIsNotNone(channel.capture())
            self.assertFalse(channel._dead)


class StaticVerifierTests(unittest.TestCase):
    """冻帧撞线时的静止核验：真静止放行，False/None 走原安全回退。"""

    def _channel(self, verifier):
        return MumuScreen(Path("C:/MuMu"), Path("C:/MuMu/sdk/x.dll"),
                          static_verifier=verifier)

    def test_verifier_true_passes_frame_and_never_disconnects(self):
        # 选人页式真静止：核验说 ADB 也是同一张图 → 帧照返回、
        # 计数重置、不断开、永不判死
        calls = []

        def verifier(frame):
            calls.append(frame.copy())
            return True

        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel(verifier)
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_frozen_lib(state)):
            # 首帧只立基线，从第 2 帧起才算连撞
            for _ in range(mumu_screen._STALE_LIMIT * 3 + 1):
                img = channel.capture()
                self.assertIsNotNone(img)
                self.assertEqual(img.shape, (2, 2, 3))
            self.assertFalse(channel._dead)
            self.assertIsNotNone(channel._handle)  # 一直没断开
            # 每撞线一次才核验一次（重置后重新计数）
            self.assertEqual(len(calls), 3)

    def test_verifier_true_clears_revive_count_after_reconnect(self):
        # 回归：真冻帧重连过一次（revives=1）→ 经过健康静止页（True 核验）
        # → revive 计数必须清零；否则下次真冻帧会沿旧计数直接判死。
        flag = {"verdict": False}

        def verifier(frame):
            return flag["verdict"]

        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel(verifier)
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_frozen_lib(state)):
            # 第一轮：False → 撞线断开重连，revives=1
            for _ in range(mumu_screen._STALE_LIMIT):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())
            self.assertEqual(channel._stale_revives, 1)
            # 第二轮：健康静止页，True → 帧照返回且 revive 计数清零
            # （撞线后每轮是 9 帧普通 + 第 10 帧撞线，别再多数一帧）
            flag["verdict"] = True
            for _ in range(mumu_screen._STALE_LIMIT):
                self.assertIsNotNone(channel.capture())
            self.assertEqual(channel._stale_revives, 0)
            self.assertFalse(channel._dead)
            self.assertIsNotNone(channel._handle)
            # 第三轮：又真冻（False）→ 视同首次，只重连不判死
            flag["verdict"] = False
            for _ in range(mumu_screen._STALE_LIMIT - 1):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())  # 撞线：revives 0→1，重连
            self.assertFalse(channel._dead)
            self.assertEqual(channel._stale_revives, 1)
            # 第四轮：再一次冻帧才轮到判死（revives 1→2）
            for _ in range(mumu_screen._STALE_LIMIT - 1):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())
            self.assertTrue(channel._dead)

    def test_verifier_false_still_reconnects(self):
        def verifier(frame):
            return False

        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel(verifier)
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_frozen_lib(state)):
            for _ in range(mumu_screen._STALE_LIMIT):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())  # 撞线：断开重连
            self.assertFalse(channel._dead)
            self.assertIsNone(channel._handle)
            state["frame"] = bytes([1]) * (4 * 2 * 2)  # MuMu 复活
            self.assertIsNotNone(channel.capture())

    def test_verifier_unavailable_falls_back_to_safe_path(self):
        # 核验返回 None（ADB 拿不到）→ 跟没有核验一样：断开重连，再冻判死
        def verifier(frame):
            return None

        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel(verifier)
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_frozen_lib(state)):
            for _ in range(mumu_screen._STALE_LIMIT):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())  # 撞线：核验不可用，安全回退
            self.assertFalse(channel._dead)
            for _ in range(mumu_screen._STALE_LIMIT - 1):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())  # 第二次撞线：本场判死
            self.assertTrue(channel._dead)

    def test_verifier_exception_falls_back_to_safe_path(self):
        # 核验自己炸了不许砸截图：按「不可用」安全回退
        def verifier(frame):
            raise RuntimeError("核验通道炸了")

        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel(verifier)
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_frozen_lib(state)):
            for _ in range(mumu_screen._STALE_LIMIT):
                self.assertIsNotNone(channel.capture())
            self.assertIsNone(channel.capture())
            self.assertFalse(channel._dead)
            self.assertIsNone(channel._handle)

    def test_live_frames_never_consult_verifier(self):
        def verifier(frame):
            raise AssertionError("活帧不该调用静止核验")

        state = {"frame": bytes(4 * 2 * 2)}
        channel = self._channel(verifier)
        with mock.patch.object(mumu_screen.ctypes, "CDLL",
                               return_value=_frozen_lib(state)):
            for i in range(mumu_screen._STALE_LIMIT * 3):
                state["frame"] = bytes([i % 256]) * (4 * 2 * 2)
                self.assertIsNotNone(channel.capture())
            self.assertFalse(channel._dead)


class DeviceAdapterVerifierTests(unittest.TestCase):
    """DeviceAdapter 侧的 ADB 核验：只走裸 ADB helper，绝不递归 screenshot。"""

    def _adapter(self):
        from touken.device_adapter import DeviceAdapter
        return DeviceAdapter(adb_path="C:/MuMu/adb.exe",
                             adb_address="127.0.0.1:16384",
                             resource_dir="resource/base")

    def test_verify_true_when_adb_frame_pixel_identical(self):
        import numpy as np
        adapter = self._adapter()
        frame = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
        with mock.patch.object(adapter, "_adb_capture_bgr",
                               return_value=frame.copy()) as capture:
            self.assertTrue(adapter._verify_static_frame(frame))
            capture.assert_called_once_with()

    def test_verify_false_when_adb_frame_differs(self):
        import numpy as np
        adapter = self._adapter()
        frame = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
        other = np.zeros((2, 2, 3), dtype=np.uint8)
        with mock.patch.object(adapter, "_adb_capture_bgr", return_value=other):
            self.assertFalse(adapter._verify_static_frame(frame))

    def test_verify_none_when_adb_capture_fails(self):
        import numpy as np
        adapter = self._adapter()
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        with mock.patch.object(adapter, "_adb_capture_bgr", return_value=None):
            self.assertIsNone(adapter._verify_static_frame(frame))

    def test_verify_never_touches_screenshot_cache(self):
        # 核验在 capture() 回调里被调，递归 screenshot 会死锁/串缓存：
        # 必须只走 _adb_capture_bgr，且不读不写 _last_image
        import numpy as np
        adapter = self._adapter()
        sentinel = object()
        adapter._last_image = sentinel
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        with mock.patch.object(adapter, "screenshot",
                               side_effect=AssertionError("不许递归 screenshot")):
            with mock.patch.object(adapter, "_adb_capture_bgr",
                                   return_value=frame.copy()):
                self.assertTrue(adapter._verify_static_frame(frame))
        self.assertIs(adapter._last_image, sentinel)

    def test_screenshot_injects_verifier_into_new_mumu_screen(self):
        # 新建的 MumuScreen 必须带上 ADB 静止核验
        import numpy as np
        adapter = self._adapter()
        adapter._initialized = True
        frame = np.zeros((4, 2, 3), dtype=np.uint8)
        fake_screen = mock.MagicMock()
        fake_screen.capture.return_value = frame
        fake_screen._dead = False
        with mock.patch("touken.mumu_screen.MumuScreen.from_adb",
                        return_value=fake_screen) as from_adb:
            out = adapter.screenshot(force=True)
        self.assertIs(out, frame)
        _, kwargs = from_adb.call_args
        # bound method 每次访问是新对象，比底层函数与宿主
        verifier = kwargs["static_verifier"]
        self.assertEqual(verifier.__func__,
                         adapter._verify_static_frame.__func__)
        self.assertIs(verifier.__self__, adapter)


if __name__ == "__main__":
    unittest.main()
