"""Exercise real frozen startup, program update and rollback in isolated copies."""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launcher.light_update import EXE, MANIFEST, apply_package, build_package, digest, probe
from launcher import light_update


def main(package: Path):
    with tempfile.TemporaryDirectory(prefix="maamaru-update-smoke-") as scratch:
        root = Path(scratch)
        old = root / "program"
        new = root / "next"
        shutil.copytree(package, old)
        shutil.copytree(package, new)
        data = root / "user-data/config.json"
        data.parent.mkdir()
        data.write_bytes(b"update-smoke-preserve-records")
        marker = new / "_internal/panel/static/update-smoke.txt"
        marker.write_bytes(b"new program content")
        asset = build_package(new, root / "assets", "99.0.1")
        probe(old)
        apply_package(asset, old, root / "backups/before-success", "99.0.1")
        assert (old / "_internal/panel/static/update-smoke.txt").read_bytes() == b"new program content"
        before = {p.relative_to(old): digest(p) for p in old.rglob('*') if p.is_file()}
        # A damaged executable passes package-integrity checks but must fail startup.
        (new / EXE).write_bytes(b"invalid Windows executable")
        broken = build_package(new, root / "assets", "99.0.2")
        try:
            apply_package(broken, old, root / "backups/before-failure", "99.0.2")
        except (OSError, ValueError):
            pass
        else:
            raise AssertionError("Broken executable unexpectedly accepted")
        assert before == {p.relative_to(old): digest(p) for p in old.rglob('*') if p.is_file()}
        # Also force an actual startup failure after the directory switch.
        shutil.copy2(old / EXE, new / EXE)
        valid = build_package(new, root / 'assets', '99.0.3')
        calls = 0
        def fail_after_switch(candidate):
            nonlocal calls
            calls += 1
            if calls == 2:
                (candidate / EXE).write_bytes(b'invalid executable after switch')
            probe(candidate)
        with patch.object(light_update, 'probe', side_effect=fail_after_switch):
            try:
                apply_package(valid, old, root / 'backups/before-post-swap-failure', '99.0.3')
            except (OSError, ValueError):
                pass
            else:
                raise AssertionError('Post-switch startup failure was accepted')
        assert before == {p.relative_to(old): digest(p) for p in old.rglob('*') if p.is_file()}
        assert data.read_bytes() == b"update-smoke-preserve-records"
        probe(old)
        full_size = sum(p.stat().st_size for p in package.rglob('*') if p.is_file())
        print(json.dumps({"startup": True, "upgrade": True, "failed_update_preserved_old": True,
                          "post_switch_rollback": True,
                          "user_data_preserved": True, "program_bytes": full_size,
                          "update_bytes": asset.stat().st_size}))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
