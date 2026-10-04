import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import maamaru_app
from launcher import app
from panel import server


class LauncherMaintenanceTests(unittest.TestCase):
    def test_version_comparison_handles_tags_and_prerelease_suffixes(self):
        self.assertEqual(app._version_tuple("v0.1.4"), (0, 1, 4))
        self.assertGreater(app._version_tuple("0.2.0"), app._version_tuple("0.1.9"))

    def test_repair_backs_up_invalid_json_before_restoring_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "touken_config.json"
            source = root / "default.json"
            target.write_text("{broken", encoding="utf-8")
            source.write_text('{"ok": true}', encoding="utf-8")

            fake_paths = ((target, source, "本丸配置"),)
            with patch.object(app, "_runtime_defaults", return_value=fake_paths), \
                    patch.object(app, "ensure_runtime_data"):
                repaired = app._repair_runtime_files()

            self.assertEqual(target.read_text(encoding="utf-8"), '{"ok": true}')
            self.assertTrue(list(root.glob("touken_config.json.broken-*")))
            self.assertIn("已备份并重建损坏的本丸配置", repaired)

    def test_launcher_uses_panel_sized_window_without_maximizing(self):
        with patch.object(app, "ensure_runtime_data"), \
                patch.object(app.webview, "create_window") as create_window, \
                patch.object(app.webview, "start") as start:
            app.main()

        kwargs = create_window.call_args.kwargs
        self.assertEqual((kwargs["width"], kwargs["height"]), (1080, 720))
        self.assertEqual(kwargs["min_size"], (860, 660))
        self.assertIn(f"v{app.CURRENT_VERSION}", kwargs["html"])
        self.assertIn("data:image/png;base64,", kwargs["html"])
        self.assertEqual(
            Path(start.call_args.kwargs["icon"]).name,
            "maamaru-launcher.ico",
        )
        self.assertTrue(app._launcher_icon_path().is_file())

    def test_launcher_exports_bundle_and_reveals_it_in_explorer(self):
        bundle = Path(r"C:\Temp\maamaru-feedback-test.zip")
        with patch.object(app, "create_diagnostic_bundle", return_value=bundle), \
                patch.object(app.subprocess, "Popen") as popen:
            result = app.Api().export_diagnostics()

        self.assertTrue(result["ok"])
        self.assertIn(bundle.name, result["message"])
        self.assertEqual(popen.call_args.args[0], ["explorer.exe", "/select,", str(bundle)])

    def test_launcher_exposes_verified_data_relocation_controls(self):
        self.assertIn("迁移数据", app.HTML)
        self.assertIn("清理旧副本", app.HTML)
        self.assertIn("永久删除旧数据副本", app.HTML)

    def test_launcher_offers_manual_emulator_selection(self):
        self.assertIn("选择模拟器", app.HTML)
        self.assertTrue(callable(getattr(app.Api(), "choose_emulator", None)))

    def test_launcher_offers_separate_ledger_entry(self):
        self.assertNotIn("只打开账房", app.HTML)
        self.assertNotIn("startApp('ledger')", app.HTML)
        self.assertFalse(app.Api().start("ledger")["ok"])
        self.assertNotEqual(maamaru_app.LEDGER_PORT, maamaru_app.AUTOMATION_PORT)

    def test_panel_can_return_to_the_same_launcher_window(self):
        window = Mock()
        thread = Mock()
        with patch.object(app.webview, "windows", [window]), \
                patch.object(app.threading, "Thread", return_value=thread) as thread_factory, \
                patch.object(app.time, "sleep"):
            result = app.Api().return_to_launcher()
            thread_factory.call_args.kwargs["target"]()

        self.assertTrue(result["ok"])
        thread.start.assert_called_once_with()
        window.resize.assert_called_once_with(1080, 720)
        loaded_html = window.load_html.call_args.args[0]
        self.assertIn("まあ丸", loaded_html)
        self.assertNotIn("__ICON_URI__", loaded_html)
        panel_source = (app._project_root() / "panel" / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
        self.assertIn("返回启动器", panel_source)
        self.assertIn("return_to_launcher", panel_source)

    def test_ledger_port_falls_back_when_preferred_is_occupied(self):
        with patch.object(app.socket, "socket", wraps=app.socket.socket):
            occupied = app.socket.socket(app.socket.AF_INET, app.socket.SOCK_STREAM)
            occupied.bind(("127.0.0.1", 0))
            preferred = occupied.getsockname()[1]
            try:
                selected = app._available_port(preferred)
            finally:
                occupied.close()
        self.assertNotEqual(selected, preferred)

    def test_server_start_pins_each_mode_without_mutating_process_environment(self):
        with patch.dict(os.environ, {"MAAMARU_LEDGER_MODE": "1"}), \
                patch("uvicorn.run") as uvicorn_run, \
                patch.object(server, "configure_app_mode") as configure_mode:
            maamaru_app._run_server(port=18082, ledger_mode=True)
            maamaru_app._run_server(port=8080, ledger_mode=False)
            self.assertEqual(os.environ["MAAMARU_LEDGER_MODE"], "1")

        self.assertEqual(
            [call.args[0] for call in configure_mode.call_args_list],
            [True, False],
        )
        self.assertEqual(uvicorn_run.call_count, 2)

    def test_non_maamaru_service_is_not_accepted_as_panel(self):
        with patch.object(app.urllib.request, "urlopen", side_effect=OSError):
            self.assertIsNone(app._panel_mode(8082))

    def test_timeout_without_exception_does_not_claim_a_log_exists(self):
        api = app.Api()
        with patch.object(app, "_panel_mode", return_value=None), \
                patch.object(app, "_port_alive", return_value=False), \
                patch.object(app.time, "time", side_effect=[0, 16, 16]), \
                patch.object(app.threading, "Thread"):
            result = api.start("automation")

        self.assertFalse(result["ok"])
        self.assertIn("启动等待超时", result["message"])
        self.assertNotIn("launcher.log", result["message"])


if __name__ == "__main__":
    unittest.main()
