"""模板归档兼容：老配置、设备两种匹配入口、双服隔离与坏映射拒绝。"""
import json
import os
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from touken.device_adapter import DeviceAdapter, Point, Region
from touken.runtime_paths import RESOURCE_DIR
from touken.server_pack import (
    PackPathError, PackValueError, UnknownReferenceError,
    load_server_pack, resolve_template_path,
)


MOVED = {
    '决定.png': ('@sortie/decide', 'sortie/decide.png'),
    'ui本丸.png': ('@home/screen', 'home/screen.png'),
    '通用_确定.png': ('@dialog/confirm', 'common/confirm.png'),
    '否.png': ('@dialog/no', 'common/no.png'),
}


def write_pack(root, aliases=None, templates=None):
    folder = root / 'server_packs'
    folder.mkdir(exist_ok=True)
    path = folder / 'cn.json'
    path.write_text(json.dumps({
        'base_size': [1280, 720],
        'templates': templates or {'dialog/confirm': 'common/confirm.png'},
        'template_aliases': aliases if aliases is not None else {'old.png': '@dialog/confirm'},
    }), encoding='utf-8')
    return path


@pytest.fixture
def resource(tmp_path):
    image = tmp_path / 'image' / 'common'
    image.mkdir(parents=True)
    (image / 'confirm.png').write_bytes(b'original-template')
    write_pack(tmp_path)
    return tmp_path


@pytest.mark.parametrize('legacy', MOVED)
def test_existing_config_and_logical_name_find_same_archived_file(legacy, tmp_path):
    logical, relative = MOVED[legacy]
    # 老配置原样复制，不做补键或覆盖用户自定义值，也能继续找到模板。
    config = tmp_path / 'old-config.json'
    before = json.dumps({'button': {'template': legacy}, 'custom': 17}).encode()
    config.write_bytes(before)
    saved = json.loads(config.read_bytes())
    old = resolve_template_path(saved['button']['template'], RESOURCE_DIR)
    new = resolve_template_path(logical, RESOURCE_DIR)
    assert old == new == RESOURCE_DIR.resolve() / 'image' / relative
    assert old.is_file()
    assert not (RESOURCE_DIR / 'image' / legacy).exists()
    assert config.read_bytes() == before


@pytest.mark.parametrize('reference', ['old.png', '@dialog/confirm', 'common/confirm.png'])
def test_device_match_score_and_exists_use_same_resolver(resource, reference):
    adapter = DeviceAdapter('', '', str(resource))
    frame = np.zeros((20, 30, 3), dtype=np.uint8)
    roi = Region(1, 2, 3, 4)
    with patch.object(adapter, 'screenshot', return_value=frame), \
            patch('touken.vision_cv2.best', return_value=(5, 6, .9)) as best, \
            patch('touken.vision_cv2.match_score', return_value=.85) as score:
        assert adapter.template_match(reference, roi, threshold=.8) == Point(5, 6)
        assert adapter.template_match_score(reference, roi, threshold=.7) == .85
        assert adapter.exists(reference, roi, threshold=.8)
    target = resource / 'image' / 'common' / 'confirm.png'
    assert best.call_args.args[1:] == (target, roi.to_tuple())
    assert score.call_args.args[1:] == (target, roi.to_tuple(), .7)


@pytest.mark.parametrize('legacy', MOVED)
def test_archived_png_matches_through_real_device_entry(legacy):
    logical, _ = MOVED[legacy]
    with Image.open(resolve_template_path(logical, RESOURCE_DIR)) as image:
        tile = np.asarray(image.convert('RGB'))[:, :, ::-1]
    height, width = tile.shape[:2]
    frame = np.random.default_rng(7).integers(0, 256, (height + 40, width + 40, 3), dtype=np.uint8)
    frame[11:11 + height, 17:17 + width] = tile
    adapter = DeviceAdapter('', '', str(RESOURCE_DIR))
    with patch.object(adapter, 'screenshot', return_value=frame):
        expected = Point(17 + width // 2, 11 + height // 2)
        assert adapter.template_match(legacy) == expected
        assert adapter.template_match(logical) == expected
        assert adapter.template_match_score(legacy) == pytest.approx(1, abs=1e-5)
        assert adapter.template_match_score(logical) == pytest.approx(1, abs=1e-5)


@pytest.mark.parametrize('aliases,error', [
    ({'old.png': 'common/confirm.png'}, PackValueError),
    ({'old.png': '@missing'}, UnknownReferenceError),
    ({'../old.png': '@dialog/confirm'}, PackPathError),
    ({'@legacy.png': '@dialog/confirm'}, PackValueError),
])
def test_invalid_aliases_fail_when_pack_loads(resource, aliases, error):
    write_pack(resource, aliases=aliases)
    with pytest.raises(error):
        load_server_pack('cn', resource)


def test_missing_target_never_falls_back_to_old_file(resource):
    (resource / 'image' / 'old.png').write_bytes(b'stale')
    write_pack(resource, templates={'dialog/confirm': 'missing.png'})
    with pytest.raises(PackPathError):
        resolve_template_path('old.png', resource)


def test_aliases_are_isolated_by_server(resource):
    (resource / 'server_packs' / 'jp.json').write_text(
        json.dumps({'base_size': [1508, 1088]}), encoding='utf-8')
    assert resolve_template_path('old.png', resource, 'jp') == resource / 'image' / 'old.png'
    with pytest.raises(UnknownReferenceError):
        resolve_template_path('@dialog/confirm', resource, 'jp')


def test_resource_update_refreshes_cached_mapping(resource):
    manifest = resource / 'server_packs' / 'cn.json'
    first_stamp = manifest.stat().st_mtime_ns
    assert resolve_template_path('old.png', resource).name == 'confirm.png'
    (resource / 'image' / 'common' / 'new.png').write_bytes(b'updated')
    write_pack(resource, templates={'dialog/confirm': 'common/new.png'})
    os.utime(manifest, ns=(first_stamp + 1_000_000, first_stamp + 1_000_000))
    assert resolve_template_path('old.png', resource).name == 'new.png'


def test_plain_custom_templates_without_pack_still_work(tmp_path):
    assert resolve_template_path('custom/button.png', tmp_path) == tmp_path / 'image/custom/button.png'
    with pytest.raises(PackPathError):
        resolve_template_path('../outside.png', tmp_path)
