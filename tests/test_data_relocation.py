import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from touken.data_relocation import (
    DataRelocationError,
    RELOCATION_MARKER,
    _safe_cleanup_source,
    cleanup_previous_data,
    pending_relocation_cleanup,
    relocate_user_data,
    suggested_data_root,
)


class DataRelocationTests(unittest.TestCase):
    def test_cleanup_never_accepts_a_parent_of_the_user_profile(self):
        source = Path.home().resolve().parent
        target = Path(tempfile.gettempdir()).resolve() / "maamaru-relocation-target"
        self.assertFalse(_safe_cleanup_source(source, target))

    def test_selected_parent_gets_maamaru_folder(self):
        self.assertEqual(
            suggested_data_root(Path("D:/Data")),
            Path("D:/Data/Maamaru").resolve(),
        )
        self.assertEqual(
            suggested_data_root(Path("D:/Maamaru")),
            Path("D:/Maamaru").resolve(),
        )

    def test_copy_is_verified_and_source_is_kept_until_explicit_cleanup(self):
        # conftest 的测试隔离会设 MAAMARU_DATA_DIR，而迁移守卫拒绝在
        # 环境变量重定向下工作——本用例测的恰是无重定向的注册表迁移路径
        with patch.dict(os.environ, {"MAAMARU_DATA_DIR": ""}), \
                tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "old" / "Maamaru"
            (source / "config").mkdir(parents=True)
            (source / "logs").mkdir()
            (source / "config" / "touken.json").write_text('{"mine": true}', encoding="utf-8")
            (source / "logs" / "telemetry.db").write_bytes(b"sqlite-data")
            selected = root / "new-disk"
            writes = []

            record = relocate_user_data(
                selected,
                source_root=source,
                location_writer=lambda target, previous: writes.append((target, previous)),
            )
            target = selected / "Maamaru"

            self.assertEqual(writes, [(target.resolve(), source.resolve())])
            self.assertEqual(record["files"], 2)
            self.assertEqual((target / "config" / "touken.json").read_text(encoding="utf-8"), '{"mine": true}')
            self.assertEqual((target / "logs" / "telemetry.db").read_bytes(), b"sqlite-data")
            self.assertTrue((source / "config" / "touken.json").is_file())
            self.assertTrue((source / RELOCATION_MARKER).is_file())
            self.assertEqual(pending_relocation_cleanup(data_root=target)["token"], record["token"])

            cleared = []
            cleanup_previous_data(
                record["token"],
                data_root=target,
                clear_previous=lambda: cleared.append(True),
            )
            self.assertFalse(source.exists())
            self.assertTrue((target / "config" / "touken.json").is_file())
            self.assertEqual(cleared, [True])
            self.assertIsNone(pending_relocation_cleanup(data_root=target))

    def test_relocation_refuses_nested_or_nonempty_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Maamaru"
            source.mkdir()
            (source / "data-version.json").write_text("{}", encoding="utf-8")

            with self.assertRaises(DataRelocationError):
                relocate_user_data(
                    source / "nested",
                    source_root=source,
                    location_writer=lambda *_: None,
                )

            selected = root / "destination"
            target = selected / "Maamaru"
            target.mkdir(parents=True)
            (target / "someone-elses-file.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(DataRelocationError):
                relocate_user_data(
                    selected,
                    source_root=source,
                    location_writer=lambda *_: None,
                )
            self.assertEqual((target / "someone-elses-file.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
