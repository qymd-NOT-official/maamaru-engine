import unittest
from types import SimpleNamespace
from unittest.mock import patch

from touken.maa_adapter import MAAAdapter, Region


class MaaAdapterLoggingTests(unittest.TestCase):
    def test_ocr_all_keeps_results_when_console_cannot_encode_text(self):
        result = SimpleNamespace(text="HYPERGRYPH ©", score=0.99, box=[10, 20, 100, 30])
        recognition = SimpleNamespace(hit=True, all_results=[result], best_result=result)
        detail = SimpleNamespace(nodes=[SimpleNamespace(recognition=recognition)])
        job = SimpleNamespace(done=True, get=lambda: detail)

        adapter = MAAAdapter.__new__(MAAAdapter)
        adapter._ocr_engine = "maa"  # 这条测的是 MAA 老路的日志容错
        adapter.tasker = SimpleNamespace(post_recognition=lambda *_args: job)
        adapter.screenshot = lambda force=False: object()
        adapter._record_ocr = lambda *_args, **_kwargs: None
        adapter._maa_timeouts = 0
        adapter._initialized = True

        encoding_error = UnicodeEncodeError("gbk", "©", 0, 1, "illegal multibyte sequence")
        with patch("builtins.print", side_effect=encoding_error):
            output = adapter.ocr_all(Region(0, 0, 1280, 720))

        self.assertEqual(output[0][0], "HYPERGRYPH ©")
        self.assertEqual(output[0][1].to_tuple(), (60, 35))


if __name__ == "__main__":
    unittest.main()
