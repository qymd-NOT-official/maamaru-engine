import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from touken.runtime_paths import DATA_SCHEMA_VERSION, ensure_runtime_data


class RuntimePathsTests(unittest.TestCase):
    @staticmethod
    def _snapshot(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file()
        }

    def _bundle(self, root: Path) -> Path:
        bundle = root / "program"
        (bundle / "panel").mkdir(parents=True)
        (bundle / "profiles").mkdir()
        (bundle / "profiles" / "official_head.png").write_bytes(b"official")
        (bundle / "touken_config.example.json").write_text('{"default": true}', encoding="utf-8")
        (bundle / "panel" / "panel_config.example.json").write_text('{"panel": true}', encoding="utf-8")
        (bundle / "panel" / "expedition_schedule.json").write_text('{"entries": []}', encoding="utf-8")
        return bundle

    def test_first_start_creates_separated_layout_from_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self._bundle(root)
            data = root / "user-data"

            result = ensure_runtime_data(data, bundle, legacy_roots=[])

            self.assertEqual(result["data_schema"], DATA_SCHEMA_VERSION)
            self.assertEqual(json.loads((data / "config" / "touken.json").read_text()), {"default": True})
            self.assertTrue((data / "config" / "panel.json").is_file())
            self.assertTrue((data / "config" / "expedition.json").is_file())
            for directory in ("state", "logs", "debug", "backups", "updates", "profiles/overrides"):
                self.assertTrue((data / directory).is_dir(), directory)
            self.assertFalse((data / "profiles" / "overrides" / "official_head.png").exists())

    def test_legacy_migration_copies_backs_up_and_never_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self._bundle(root)
            data = root / "user-data"
            legacy = root / "legacy"
            (legacy / "status").mkdir(parents=True)
            (legacy / "profiles").mkdir()
            (legacy / "touken_config.json").write_text('{"mine": true}', encoding="utf-8")
            (legacy / "status" / "daily_flags.json").write_text('{"done": true}', encoding="utf-8")
            (legacy / "status" / "maamaru_logs.db").write_bytes(b"sqlite")
            (legacy / "profiles" / "custom_head.png").write_bytes(b"png")

            result = ensure_runtime_data(data, bundle, legacy_roots=[legacy])

            self.assertGreaterEqual(result["copied"], 4)
            # 迁移来的老配置会被补上新版本新增的字段，已有键保持不动
            self.assertEqual(json.loads((data / "config" / "touken.json").read_text()),
                             {"mine": True, "default": True})
            self.assertTrue((data / "state" / "daily_flags.json").is_file())
            self.assertFalse((data / "state" / "maamaru_logs.db").exists())
            self.assertEqual((data / "logs" / "maamaru_logs.db").read_bytes(), b"sqlite")
            self.assertTrue((data / "profiles" / "overrides" / "custom_head.png").is_file())
            self.assertTrue(Path(result["backup"]).is_dir())
            self.assertTrue((legacy / "touken_config.json").is_file())

            second = ensure_runtime_data(data, bundle, legacy_roots=[legacy])
            self.assertEqual(second, result)

    def test_existing_new_config_wins_over_legacy_and_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self._bundle(root)
            data = root / "user-data"
            legacy = root / "legacy"
            (data / "config").mkdir(parents=True)
            (legacy).mkdir()
            target = data / "config" / "touken.json"
            # default=false 模拟用户改过的值：补键时绝不能被模板覆盖
            target.write_text('{"new": true, "default": false}', encoding="utf-8")
            (legacy / "touken_config.json").write_text('{"old": true}', encoding="utf-8")

            ensure_runtime_data(data, bundle, legacy_roots=[legacy])

            self.assertEqual(json.loads(target.read_text()), {"new": True, "default": False})

    def test_old_config_gets_new_template_keys_filled_with_backup(self):
        """复现 2026-08-22 安装版事故：老配置没有异去坐标，升级后出阵哑跑报绿。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "program"
            (bundle / "panel").mkdir(parents=True)
            (bundle / "profiles").mkdir()
            (bundle / "touken_config.example.json").write_text(json.dumps({
                "map_select": {"合战场": {"chapters": {"1": [1, 2]}},
                               "异去": {"chapters": {"1": [3, 4]}}},
                "yosari": {"auto_refill": False},
            }, ensure_ascii=False), encoding="utf-8")
            (bundle / "panel" / "panel_config.example.json").write_text('{}', encoding="utf-8")
            (bundle / "panel" / "expedition_schedule.json").write_text('{}', encoding="utf-8")
            data = root / "user-data"
            (data / "config").mkdir(parents=True)
            target = data / "config" / "touken.json"
            target.write_text(json.dumps({
                "map_select": {"合战场": {"chapters": {"1": [9, 9]}}},
            }, ensure_ascii=False), encoding="utf-8")

            ensure_runtime_data(data, bundle, legacy_roots=[])

            merged = json.loads(target.read_text(encoding="utf-8"))
            # 新增字段补齐
            self.assertEqual(merged["map_select"]["异去"], {"chapters": {"1": [3, 4]}})
            self.assertEqual(merged["yosari"], {"auto_refill": False})
            # 用户已有的值（哪怕和模板不一样）原样保留
            self.assertEqual(merged["map_select"]["合战场"]["chapters"]["1"], [9, 9])
            # 写入前有备份
            backups = list((data / "backups").glob("touken-pre-keyfill-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertNotIn("异去", backups[0].read_text(encoding="utf-8"))
            # 再跑一次：没有新键可补，文件和备份数都不变
            digest_before = hashlib.sha256(target.read_bytes()).hexdigest()
            ensure_runtime_data(data, bundle, legacy_roots=[])
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), digest_before)
            self.assertEqual(len(list((data / "backups").glob("touken-pre-keyfill-*.json"))), 1)

    def test_old_config_gets_edocastle_keys_filled(self):
        """江户城新增配置段必须能补进老安装，防止 8-22 式哑跑事故。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "program"
            (bundle / "panel").mkdir(parents=True)
            (bundle / "profiles").mkdir()
            example_cfg = {
                "edocastle": {
                    "team_no": 3,
                    "difficulty": 4,
                    "map_archive": "resource/base/maps/edocastle-4.json",
                    "use_koban_refill": False,
                    "max_runs": 6,
                }
            }
            (bundle / "touken_config.example.json").write_text(
                json.dumps(example_cfg, ensure_ascii=False), encoding="utf-8")
            (bundle / "panel" / "panel_config.example.json").write_text('{}', encoding="utf-8")
            (bundle / "panel" / "expedition_schedule.json").write_text('{}', encoding="utf-8")
            data = root / "user-data"
            (data / "config").mkdir(parents=True)
            target = data / "config" / "touken.json"
            target.write_text(json.dumps({"old": True}, ensure_ascii=False), encoding="utf-8")

            ensure_runtime_data(data, bundle, legacy_roots=[])

            merged = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("edocastle", merged)
            self.assertEqual(merged["edocastle"]["team_no"], 3)
            self.assertEqual(merged["edocastle"]["map_archive"],
                             "resource/base/maps/edocastle-4.json")
            # 用户旧值保留
            self.assertTrue(merged["old"])
            # 有备份
            self.assertEqual(
                len(list((data / "backups").glob("touken-pre-keyfill-*.json"))), 1)

    def test_old_edocastle_section_gets_safety_keys_filled(self):
        """老安装已有 edocastle 段（v1 手搓版）：递归补键必须把通用安全出阵链
        依赖的伤势/重伤拦截键补齐，且不动用户已有值。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "program"
            (bundle / "panel").mkdir(parents=True)
            (bundle / "profiles").mkdir()
            # 直接拿真实 example 当模板，保证测试跟着配置演进
            real_example = Path(__file__).resolve().parent.parent / (
                "touken_config.example.json")
            example_cfg = json.loads(real_example.read_text(encoding="utf-8-sig"))
            (bundle / "touken_config.example.json").write_text(
                json.dumps(example_cfg, ensure_ascii=False), encoding="utf-8")
            (bundle / "panel" / "panel_config.example.json").write_text('{}', encoding="utf-8")
            (bundle / "panel" / "expedition_schedule.json").write_text('{}', encoding="utf-8")
            data = root / "user-data"
            (data / "config").mkdir(parents=True)
            target = data / "config" / "touken.json"
            # 模拟 2026-08-27 中午那版老 edocastle 段：有基础键，没安全键
            target.write_text(json.dumps({
                "edocastle": {
                    "team_no": 4,
                    "difficulty": 4,
                    "map_archive": "resource/base/maps/edocastle-4.json",
                    "max_runs": 2,
                    "confirm_button": {"template": "通用_确定.png",
                                       "roi": [536, 565, 742, 637]},
                },
            }, ensure_ascii=False), encoding="utf-8")

            ensure_runtime_data(data, bundle, legacy_roots=[])

            merged = json.loads(target.read_text(encoding="utf-8"))["edocastle"]
            # 安全出阵链和地图可操作门闩依赖的键全部补齐
            for key in ("repair_threshold", "auto_equip", "injury_deny_button",
                        "injury_stamps", "injury_stamp_roi", "injury_status_roi",
                        "map_ready", "ticket_price"):
                self.assertIn(key, merged, f"老安装没补到 edocastle.{key}")
            self.assertEqual(
                merged["map_ready"]["expected"], "地图点选择")
            self.assertEqual(merged["ticket_price"], 300)
            # 用户已有的值原样保留（递归补键不覆盖）
            self.assertEqual(merged["team_no"], 4)
            self.assertEqual(merged["max_runs"], 2)
            self.assertEqual(merged["confirm_button"]["roi"], [536, 565, 742, 637])

    def test_old_expedition_section_gets_special_items_filled(self):
        """老安装的远征结算段没有 special_items（获得道具栏观察）：
        递归补键必须补上，且不动用户已有的资源行 ROI。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "program"
            (bundle / "panel").mkdir(parents=True)
            (bundle / "profiles").mkdir()
            # 直接拿真实 example 当模板，保证测试跟着配置演进
            real_example = Path(__file__).resolve().parent.parent / (
                "touken_config.example.json")
            example_cfg = json.loads(real_example.read_text(encoding="utf-8-sig"))
            (bundle / "touken_config.example.json").write_text(
                json.dumps(example_cfg, ensure_ascii=False), encoding="utf-8")
            (bundle / "panel" / "panel_config.example.json").write_text('{}', encoding="utf-8")
            (bundle / "panel" / "expedition_schedule.json").write_text('{}', encoding="utf-8")
            data = root / "user-data"
            (data / "config").mkdir(parents=True)
            target = data / "config" / "touken.json"
            # 模拟本次改动前的老 expedition 段：有四资源行 ROI，没道具栏观察
            target.write_text(json.dumps({
                "expedition": {
                    "settlement_rewards": {
                        "resource_rois": {"木炭": [870, 470, 920, 525]},
                        "result": {"roi": [680, 35, 940, 150]},
                    },
                },
            }, ensure_ascii=False), encoding="utf-8")

            ensure_runtime_data(data, bundle, legacy_roots=[])

            merged = json.loads(target.read_text(encoding="utf-8"))["expedition"]
            special = merged["settlement_rewards"].get("special_items")
            self.assertIsNotNone(special, "老安装没补到 special_items")
            self.assertIn("column_roi", special)
            self.assertIn("empty_ink_max", special)
            # 用户已有的资源行 ROI 原样保留（递归补键不覆盖）
            self.assertEqual(
                merged["settlement_rewards"]["resource_rois"]["木炭"],
                [870, 470, 920, 525])

    def test_v014_flat_user_directory_migrates_completely_and_is_repeatable(self):
        """Model the writable files produced beside data in the v0.1.4 release."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self._bundle(root)
            old = root / "v0.1.4-user-data"
            old.mkdir()

            fixtures = {
                "touken_config.json": b'{"version":"1.0","adb_path":"D:/MuMu/adb.exe"}',
                "panel_config.json": b'{"ai":{"api_key":"private-test-key"}}',
                "expedition_schedule.json": b'{"entries":[{"team":3}]}',
                "launcher.log": "旧启动日志".encode(),
                "status/daily_flags.json": b'{"forge":"2026-08-11"}',
                "status/expeditions.json": b'{"3":{"map":"5-4"}}',
                "status/inventory.json": '{"木炭":12345}'.encode(),
                "status/session_state.json": b'{"messages":[]}',
                "status/maamaru_logs.db": b"SQLite format 3\x00test-db",
                "debug/device.log": b"debug-log",
                "debug/last-screen.png": b"fake-png",
                "profiles/custom_head.png": b"custom-profile",
            }
            for relative, content in fixtures.items():
                path = old / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            original = self._snapshot(old)
            first = ensure_runtime_data(old, bundle, legacy_roots=[old])

            expected = {
                "touken_config.json": "config/touken.json",
                "panel_config.json": "config/panel.json",
                "expedition_schedule.json": "config/expedition.json",
                "launcher.log": "logs/launcher.log",
                "status/daily_flags.json": "state/daily_flags.json",
                "status/expeditions.json": "state/expeditions.json",
                "status/inventory.json": "state/inventory.json",
                "status/session_state.json": "state/session_state.json",
                "status/maamaru_logs.db": "logs/maamaru_logs.db",
                "debug/device.log": "debug/device.log",
                "debug/last-screen.png": "debug/last-screen.png",
                "profiles/custom_head.png": "profiles/overrides/custom_head.png",
            }
            for source, target in expected.items():
                if source == "touken_config.json":
                    # 迁移落地后还会被补上新版本新增的模板字段
                    self.assertEqual(
                        json.loads((old / target).read_text(encoding="utf-8")),
                        {**json.loads(fixtures[source]), "default": True}, target)
                    continue
                self.assertEqual((old / target).read_bytes(), fixtures[source], target)
            for source, digest in original.items():
                self.assertEqual(hashlib.sha256((old / source).read_bytes()).hexdigest(), digest)

            backup = Path(first["backup"])
            self.assertTrue(backup.is_dir())
            for source, target in expected.items():
                if source.startswith("debug/"):
                    continue  # v0.1.4 and v1 use the same debug/ location
                backup_target = source if source.startswith("profiles/") else target
                self.assertEqual((backup / backup_target).read_bytes(), fixtures[source], backup_target)

            after_first = self._snapshot(old)
            second = ensure_runtime_data(old, bundle, legacy_roots=[old])
            after_second = self._snapshot(old)
            self.assertEqual(second, first)
            self.assertEqual(after_second, after_first)


if __name__ == "__main__":
    unittest.main()
