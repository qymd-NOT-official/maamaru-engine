import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import update_apply


class UpdateApplyTests(unittest.TestCase):
    def _plan(self, root: Path):
        updates = root / "updates"
        installer = updates / "0.1.6" / "maamaru-setup-v0.1.6.exe"
        installer.parent.mkdir(parents=True)
        installer.write_bytes(b"installer")
        program = root / "Programs" / "Maamaru"
        program.mkdir(parents=True)
        (program / "まあ丸启动器.exe").write_bytes(b"old")
        return {
            "version": "0.1.6", "installer": str(installer),
            "sha256": hashlib.sha256(b"installer").hexdigest(),
            "program_dir": str(program), "backup_dir": str(updates / "backups" / "before-0.1.6"),
            "previous_executable": str(program / "まあ丸启动器.exe"), "parent_pid": 1,
            "data_root": str(root / "data"),
        }, updates

    def test_failed_installer_restores_exact_program_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, updates = self._plan(Path(tmp))
            plan_path = updates / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            class Failed:
                returncode = 7
            with patch.object(update_apply, "UPDATES_DIR", updates), \
                    patch.object(update_apply, "DATA_ROOT", Path(plan["data_root"])), \
                    patch.object(update_apply, "RESULT_PATH", updates / "result.json"), \
                    patch.object(update_apply, "_program_dir", return_value=Path(plan["program_dir"])), \
                    patch.object(update_apply, "_wait_for_process"), \
                    patch.object(update_apply.subprocess, "run", return_value=Failed()), \
                    patch.object(update_apply, "_restart"):
                self.assertEqual(update_apply.run_plan(plan_path), 7)
            program = Path(plan["program_dir"])
            self.assertEqual((program / "まあ丸启动器.exe").read_bytes(), b"old")
            result = json.loads((updates / "result.json").read_text(encoding="utf-8"))
            self.assertTrue(result["rolled_back"])

    def test_directory_bundle_helper_keeps_native_runtime_outside_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, updates = self._plan(Path(tmp))
            program = Path(plan['program_dir'])
            (program / '_internal').mkdir()
            (program / '_internal/python312.dll').write_bytes(b'native runtime')
            with patch.object(update_apply, 'UPDATES_DIR', updates), \
                    patch.object(update_apply, 'DATA_ROOT', Path(plan['data_root'])), \
                    patch.object(update_apply, '_program_dir', return_value=program), \
                    patch.object(update_apply.sys, 'frozen', True, create=True), \
                    patch.object(update_apply.sys, 'executable', plan['previous_executable']), \
                    patch.object(update_apply.subprocess, 'Popen') as launch:
                update_apply.prepare_apply(Path(plan['installer']), plan['sha256'], '0.1.6')
            helper = Path(launch.call_args.args[0][0])
            self.assertTrue(helper.is_relative_to(updates))
            self.assertEqual((helper.parent / '_internal/python312.dll').read_bytes(), b'native runtime')
            self.assertFalse((helper.parent / 'manifest.json').exists())

    def test_plan_cannot_put_program_inside_user_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, updates = self._plan(Path(tmp))
            plan["program_dir"] = str(Path(plan["data_root"]) / "program")
            with patch.object(update_apply, "UPDATES_DIR", updates), \
                    patch.object(update_apply, "DATA_ROOT", Path(plan["data_root"])), \
                    patch.object(update_apply, "_program_dir", return_value=Path(plan["program_dir"])):
                with self.assertRaises(update_apply.ApplyError):
                    update_apply._validate_plan(plan)

    def test_plan_cannot_put_user_data_inside_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, updates = self._plan(Path(tmp))
            plan["data_root"] = str(Path(plan["program_dir"]) / "records")
            with patch.object(update_apply, "UPDATES_DIR", updates), \
                    patch.object(update_apply, "DATA_ROOT", Path(plan["data_root"])), \
                    patch.object(update_apply, "_program_dir", return_value=Path(plan["program_dir"])):
                with self.assertRaises(update_apply.ApplyError):
                    update_apply._validate_plan(plan)

    def test_update_helper_accepts_custom_install_dir_with_manifest(self):
        """助手副本看不到真实安装目录时，manifest.json 可作替代凭证。"""
        with tempfile.TemporaryDirectory() as tmp:
            plan, updates = self._plan(Path(tmp))
            (Path(plan["program_dir"]) / "manifest.json").write_text("{}", encoding="utf-8")
            elsewhere = Path(tmp) / "elsewhere"
            with patch.object(update_apply, "UPDATES_DIR", updates), \
                    patch.object(update_apply, "DATA_ROOT", Path(plan["data_root"])), \
                    patch.object(update_apply, "_program_dir", return_value=elsewhere), \
                    patch.object(update_apply, "_has_local_manifest", return_value=False):
                update_apply._validate_plan(plan)

    def test_mismatched_program_dir_without_manifest_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, updates = self._plan(Path(tmp))
            elsewhere = Path(tmp) / "elsewhere"
            with patch.object(update_apply, "UPDATES_DIR", updates), \
                    patch.object(update_apply, "DATA_ROOT", Path(plan["data_root"])), \
                    patch.object(update_apply, "_program_dir", return_value=elsewhere), \
                    patch.object(update_apply, "_has_local_manifest", return_value=False):
                with self.assertRaises(update_apply.ApplyError):
                    update_apply._validate_plan(plan)

    def test_invalid_plan_records_failure_and_restarts_previous(self):
        """计划校验失败要留结果、拉回旧启动器，而不是裸崩。"""
        with tempfile.TemporaryDirectory() as tmp:
            plan, updates = self._plan(Path(tmp))
            plan["program_dir"] = str(Path(plan["data_root"]) / "program")
            plan_path = updates / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with patch.object(update_apply, "UPDATES_DIR", updates), \
                    patch.object(update_apply, "DATA_ROOT", Path(plan["data_root"])), \
                    patch.object(update_apply, "RESULT_PATH", updates / "result.json"), \
                    patch.object(update_apply, "_program_dir", return_value=Path(plan["program_dir"])), \
                    patch.object(update_apply, "_restart") as restart:
                self.assertEqual(update_apply.run_plan(plan_path), 3)
            restart.assert_called_once_with(Path(plan["previous_executable"]))
            result = json.loads((updates / "result.json").read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertIn("更新计划校验失败", result["message"])
            self.assertFalse(result["rolled_back"])


if __name__ == "__main__":
    unittest.main()
