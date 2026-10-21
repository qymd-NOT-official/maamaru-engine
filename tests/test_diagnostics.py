import sqlite3
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import server
from touken.diagnostics import _expedition_schedule_summary, build_diagnostic_bundle, create_diagnostic_bundle


class ExpeditionScheduleSummaryTests(unittest.TestCase):
    def test_summary_covers_switches_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "config").mkdir(parents=True)
            (data / "state").mkdir()
            (data / "config" / "expedition.json").write_text(
                '{"automation": {"enabled": true, "mode": "preset", "preset": "小判",'
                ' "start_time": "08:00", "teams": [2, 3], "paused_until": "2099-01-01 00:00:00"},'
                ' "common_plan": [{"team_no": 5, "map_code": "E2", "enabled": true}],'
                ' "entries": []}',
                encoding="utf-8",
            )
            (data / "state" / "expeditions.json").write_text(
                '{"2": {"map_code": "D4", "dispatched_at": "2026-09-11 08:00:00", "duration_min": 120},'
                ' "3": {"map_code": null, "dispatched_at": null, "duration_min": null}}',
                encoding="utf-8",
            )
            now = time.mktime(time.strptime("2026-09-11 09:00:00", "%Y-%m-%d %H:%M:%S"))
            text = _expedition_schedule_summary(data, now=now)
            self.assertIn("自动排班总开关: 开", text)
            self.assertIn("暂停生效中", text)
            self.assertIn("攻略预设「小判」", text)
            self.assertIn("部队5→E2（启用）", text)
            self.assertIn("部队2: D4", text)
            self.assertIn("还剩约 60 分钟", text)
            self.assertIn("部队3: 记录不完整", text)

    def test_summary_without_config_says_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = _expedition_schedule_summary(Path(tmp))
            self.assertIn("未找到排班配置文件", text)
            self.assertIn("自动排班总开关: 关", text)
            self.assertIn("无派遣记录", text)

    def test_bundle_includes_schedule_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, logs, debug, program = DiagnosticBundleTests()._fixture(Path(tmp))
            bundle = build_diagnostic_bundle(
                data_root=data, log_dir=logs, debug_dir=debug,
                bundle_root=program, version="9.9.9", frozen=True,
            )
            with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
                digest = archive.read("expedition-schedule.txt").decode("utf-8")
            self.assertIn("远征排班状态摘要", digest)


class DiagnosticBundleTests(unittest.TestCase):
    def _fixture(self, root: Path):
        data = root / "Users" / "Alice" / "AppData" / "Local" / "Maamaru"
        logs = data / "logs"
        debug = data / "debug"
        program = root / "Program Files" / "Maamaru"
        logs.mkdir(parents=True)
        debug.mkdir(parents=True)
        program.mkdir(parents=True)
        (logs / "launcher.log").write_text(
            r"failed at C:\Users\Alice\AppData\Local\Maamaru token=launcher-secret",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(logs / "maamaru_logs.db")) as connection:
            connection.execute(
                "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts REAL, run_id TEXT, script TEXT, message TEXT)"
            )
            connection.execute(
                "CREATE TABLE chat_history (id INTEGER PRIMARY KEY, content TEXT)"
            )
            connection.execute(
                "INSERT INTO logs VALUES (1, 1000, 'run-1', 'daily', ?)",
                (r"trace C:\Users\Alice\game password=hunter2",),
            )
            connection.execute("INSERT INTO chat_history VALUES (1, 'private chat must stay out')")
            connection.commit()
        (debug / "asst.log").write_text(
            "device api_key=debug-secret Authorization: Bearer bearer-secret sk-1234567890abcdef",
            encoding="utf-8",
        )
        (data / "config").mkdir()
        (data / "config" / "touken.json").write_text('{"private": true}', encoding="utf-8")
        return data, logs, debug, program

    def test_bundle_contains_only_sanitized_text_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, logs, debug, program = self._fixture(Path(tmp))
            bundle = build_diagnostic_bundle(
                data_root=data,
                log_dir=logs,
                debug_dir=debug,
                bundle_root=program,
                version="9.9.9",
                frozen=True,
            )

            with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
                names = archive.namelist()
                combined = "\n".join(archive.read(name).decode("utf-8") for name in names)

            self.assertIn("diagnostic-summary.txt", names)
            self.assertIn("launcher.log", names)
            self.assertIn("recent-panel-logs.txt", names)
            self.assertTrue(any(name.startswith("debug/") for name in names))
            self.assertIn("Maamaru version: 9.9.9", combined)
            self.assertIn("<USER_HOME>", combined)
            self.assertIn("<REDACTED>", combined)
            self.assertNotIn("Alice", combined)
            self.assertNotIn("launcher-secret", combined)
            self.assertNotIn("hunter2", combined)
            self.assertNotIn("debug-secret", combined)
            self.assertNotIn("bearer-secret", combined)
            self.assertNotIn("sk-1234567890abcdef", combined)
            self.assertNotIn("private chat must stay out", combined)
            self.assertNotIn("touken.json", names)
            self.assertFalse(any(name.endswith(".db") for name in names))

    def test_create_bundle_writes_zip_to_requested_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, logs, debug, program = self._fixture(root)
            target = create_diagnostic_bundle(
                root / "exports",
                data_root=data,
                log_dir=logs,
                debug_dir=debug,
                bundle_root=program,
            )
            self.assertTrue(target.is_file())
            self.assertEqual(target.parent, root / "exports")
            self.assertTrue(target.name.startswith("maamaru-feedback-"))
            self.assertTrue(zipfile.is_zipfile(target))


class DiagnosticExportEndpointTests(unittest.TestCase):
    def test_export_local_saves_bundle_and_reports_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "maamaru-feedback-test.zip"
            fake.write_bytes(b"zip")
            with patch.object(server, "create_diagnostic_bundle", return_value=fake), \
                    patch.object(server, "reveal_file_in_explorer", return_value=True) as reveal:
                resp = TestClient(server.app).post("/api/diagnostics/export-local")
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["filename"], fake.name)
            self.assertTrue(payload["revealed"])
            reveal.assert_called_once_with(fake)

    def test_export_local_reports_failure_as_json(self):
        with patch.object(server, "create_diagnostic_bundle", side_effect=OSError("boom")):
            resp = TestClient(server.app).post("/api/diagnostics/export-local")
        self.assertEqual(resp.status_code, 500)
        payload = resp.json()
        self.assertFalse(payload["ok"])
        self.assertIn("boom", payload["message"])


class DiagnosticUiContractTests(unittest.TestCase):
    def test_feedback_entry_and_failure_easter_eggs_stay_wired(self):
        root = Path(__file__).resolve().parent.parent
        launcher = (root / "launcher" / "app.py").read_text(encoding="utf-8")
        log_panel = (root / "panel" / "frontend" / "src" / "components" / "LogPanel.vue").read_text(
            encoding="utf-8"
        )
        combined = launcher + log_panel

        self.assertNotIn(">过程</button>", log_panel)
        self.assertIn("反馈错误", launcher)
        self.assertIn("反馈错误", log_panel)
        # 面板在 pywebview 里浏览器下载会被吞，必须走落盘接口
        self.assertIn("/api/diagnostics/export-local", log_panel)
        self.assertIn("/api/diagnostics/export-local",
                      (root / "panel" / "server.py").read_text(encoding="utf-8"))
        self.assertIn("https://github.com/qymd-NOT-official/maamaru-engine/issues/new", combined)
        for line in (
            "导出失败？问问上天",
            "还失败？去issue骂作者",
            "干嘛不去？",
            "你是不是想骂连错误处理系统都做不好？",
            "噫吁嚱，惶恐滩头说惶恐，零丁洋里叹零丁。",
            "面包店里卖面包，蛋糕店里卖蛋糕。",
            "你还点",
            "我没有日志，你也不去issue，你到底想让我怎样",
            "狐之助已下班",
        ):
            self.assertIn(line, launcher)
            self.assertIn(line, log_panel)
        self.assertIn("}, 3000)", log_panel)
        self.assertIn("},3000)", launcher)


if __name__ == "__main__":
    unittest.main()
