import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from launcher import light_update as light, updater


def program(root, text=b"old"):
    root.mkdir(parents=True)
    (root / light.EXE).write_bytes(text)
    (root / "manifest.json").write_text('{}')
    runtime = root / "_internal/python312.dll"
    runtime.parent.mkdir()
    runtime.write_bytes(b"stable runtime")
    static = root / "_internal/panel/static/index.html"
    static.parent.mkdir(parents=True)
    static.write_bytes(text)
    return root


def pair(tmp_path):
    old = program(tmp_path / "program")
    light.build_package(old, tmp_path / "assets", "1.3.1")
    new = program(tmp_path / "next", b"new")
    package = light.build_package(new, tmp_path / "assets", "1.4.0")
    return old, new, package


def test_light_update_contains_no_runtime_and_preserves_user_data(tmp_path):
    old, new, package = pair(tmp_path)
    data = tmp_path / "user-data/config.json"
    data.parent.mkdir()
    data.write_bytes(b"precious records")
    # Obsolete managed files disappear from the new installation, remain in backup.
    obsolete = old / "_internal/panel/static/obsolete.js"
    obsolete.write_bytes(b"obsolete")
    with zipfile.ZipFile(package) as archive:
        assert "_internal/python312.dll" not in archive.namelist()
    backup = tmp_path / "updates/backups/old"
    with patch.object(light, "probe") as probe:
        light.apply_package(package, old, backup, "1.4.0")
    assert probe.call_count == 2
    assert (old / light.EXE).read_bytes() == b"new"
    assert (backup / light.EXE).read_bytes() == b"old"
    assert not (old / "_internal/panel/static/obsolete.js").exists()
    assert (backup / "_internal/panel/static/obsolete.js").exists()
    assert data.read_bytes() == b"precious records"
    light.verify_files(old, light.installed_manifest(new))


def test_post_swap_probe_failure_restores_exact_old_directory(tmp_path):
    old, _, package = pair(tmp_path)
    before = {p.relative_to(old): p.read_bytes() for p in old.rglob('*') if p.is_file()}
    with patch.object(light, "probe", side_effect=[None, ValueError("bad startup")]):
        with pytest.raises(ValueError, match="bad startup"):
            light.apply_package(package, old, tmp_path / "backup", "1.4.0")
    assert before == {p.relative_to(old): p.read_bytes() for p in old.rglob('*') if p.is_file()}


def test_corrupt_payload_never_replaces_old_program(tmp_path):
    old, _, package = pair(tmp_path)
    with zipfile.ZipFile(package) as source:
        entries = {n: source.read(n) for n in source.namelist()}
    entries[light.EXE] = b"bad"
    with zipfile.ZipFile(package, 'w') as output:
        for name, data in entries.items():
            output.writestr(name, data)
    with pytest.raises(ValueError):
        light.apply_package(package, old, tmp_path / "backup", "1.4.0")
    assert (old / light.EXE).read_bytes() == b"old"


@pytest.mark.parametrize('name', ['../outside', '/outside', 'C:/outside',
                                 '_internal/../config.json', 'nul.txt', 'a\\b',
                                 'a:stream', 'a./b', 'a//b', 'a?/b'])
def test_windows_archive_path_escape_rejected(name):
    with pytest.raises(ValueError):
        light.safe_path(name)


def test_extra_zip_entry_rejected_without_touching_external_files(tmp_path):
    old, _, package = pair(tmp_path)
    with zipfile.ZipFile(package, 'a') as archive:
        archive.writestr('../user-data/config.json', b'damage')
    with pytest.raises(ValueError):
        light.stage(package, old, tmp_path / "candidate", "1.4.0")
    assert (old / light.EXE).read_bytes() == b"old"


def test_runtime_mismatch_requires_full_update(tmp_path):
    old, new, _ = pair(tmp_path)
    (new / '_internal/python312.dll').write_bytes(b'new runtime')
    package = light.build_package(new, tmp_path / 'assets', '1.4.0')
    with pytest.raises(ValueError, match='不兼容'):
        light.stage(package, old, tmp_path / "candidate", "1.4.0")


def test_release_selection_light_and_legacy_fallback(tmp_path):
    old, _, package = pair(tmp_path)
    def asset(path):
        return {'name': path.name, 'size': path.stat().st_size,
                'digest': 'sha256:' + light.digest(path),
                'browser_download_url': f'https://github.com/{updater.REPOSITORY}/releases/download/v1.4.0/{path.name}'}
    installer = tmp_path / 'maamaru-setup-v1.4.0.exe'
    installer.write_bytes(b'installer')
    release = {'tag_name': 'v1.4.0', 'assets': [asset(package), asset(installer)]}
    assert updater.select_update(release, old)['kind'] == 'light'
    assert updater.select_update(release, tmp_path / 'legacy')['name'] == installer.name
    (old / '_internal/python312.dll').write_bytes(b'broken')
    assert updater.select_update(release, old)['name'] == installer.name


def test_download_light_package_verifies_digest(tmp_path):
    from tests.test_updater import _Response
    old, _, package = pair(tmp_path)
    payload = package.read_bytes()
    asset = {'tag': 'v1.4.0', 'version': '1.4.0', 'name': package.name, 'kind': 'light',
             'digest': 'sha256:' + hashlib.sha256(payload).hexdigest(),
             'url': 'https://github.com/' + updater.REPOSITORY + '/releases/download/v1.4.0/' + package.name,
             'size': len(payload)}
    with patch.object(updater.urllib.request, 'urlopen', return_value=_Response(payload)):
        result = updater.download_installer(asset, tmp_path / 'updates')
    assert Path(result['path']).read_bytes() == payload


def test_release_wrong_source_or_tag_never_selected(tmp_path):
    installer = {'name': 'maamaru-setup-v1.4.0.exe', 'size': 10,
                 'digest': 'sha256:' + 'a' * 64,
                 'browser_download_url': 'https://github.com/other/project/releases/download/v1.4.0/maamaru-setup-v1.4.0.exe'}
    with pytest.raises(updater.UpdateError):
        updater.select_installer({'tag_name': 'v1.4.0', 'assets': [installer]})
    installer['browser_download_url'] = f'https://github.com/{updater.REPOSITORY}/releases/download/v1.3.0/maamaru-setup-v1.4.0.exe'
    with pytest.raises(updater.UpdateError):
        updater.select_installer({'tag_name': 'v1.4.0', 'assets': [installer]})


def test_user_configuration_cannot_enter_update_package(tmp_path):
    root = program(tmp_path / 'program')
    (root / '_internal/panel/panel_config.json').write_bytes(b'private')
    with pytest.raises(ValueError, match='用户数据'):
        light.build_package(root, tmp_path / 'assets', '1.4.0')


def test_runtime_fingerprint_ignores_base_library_member_order(tmp_path):
    roots = [program(tmp_path / 'a'), program(tmp_path / 'b')]
    for root, names in zip(roots, [('a.pyc', 'b.pyc'), ('b.pyc', 'a.pyc')]):
        with zipfile.ZipFile(root / '_internal/base_library.zip', 'w') as archive:
            for name in names:
                archive.writestr(name, name.encode())
        light.build_package(root, tmp_path / 'assets', '1.4.0')
    assert light.installed_manifest(roots[0])['runtime'] == light.installed_manifest(roots[1])['runtime']


def test_active_task_blocks_update_before_helper_starts(tmp_path):
    import io
    from launcher import app
    api = app.Api()
    api._pending_update = {'version': '1.4.0', 'name': 'maamaru-setup-v1.4.0.exe'}
    with patch.object(app, '_port_alive', return_value=True), \
            patch.object(app.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"running":true}')), \
            patch.object(app, 'prepare_apply') as prepare:
        result = api.apply_update()
    assert not result['ok']
    prepare.assert_not_called()


def test_failed_light_update_next_check_uses_full_installer():
    import io
    from launcher import app
    api = app.Api()
    with patch.object(app, 'ensure_runtime_data'), \
            patch.object(app, 'auto_configure_emulator'), \
            patch.object(app, 'run_checks', return_value=[]), \
            patch.object(app, 'pending_relocation_cleanup', return_value=None), \
            patch.object(app, 'consume_result', return_value={'ok': False, 'kind': 'light'}):
        api.check()
    release = b'{"tag_name":"v99.0.0","assets":[]}'
    with patch.object(app.urllib.request, 'urlopen', return_value=io.BytesIO(release)), \
            patch.object(updater, 'select_installer', return_value={'size': 100}) as full, \
            patch.object(app, 'select_update') as light_select:
        assert api.check_update()['download_ready']
    full.assert_called_once()
    light_select.assert_not_called()
