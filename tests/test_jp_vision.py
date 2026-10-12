import pytest

from touken import jp_vision

pytest.importorskip('cv2')


@pytest.fixture
def scene():
    """1280x720 密集噪声底 + 一块有内部结构的醒目矩形作为「按钮」。

    模板必须带内部纹理：纯色块在归一化相关匹配里是退化的（方差为零，
    到处都能"匹配"），真实按钮都有边框和文字，这里用红底白条纹模拟。"""
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 40, size=(720, 1280, 3), dtype=np.uint8)
    # 红底白条纹的「按钮」：x 500-620, y 300-360
    noise[300:361, 500:621] = (200, 30, 30)
    noise[315:346, 520:601] = (240, 240, 240)
    noise[315:346, 540:561] = (200, 30, 30)
    image = Image.fromarray(noise)
    template = image.crop((500, 300, 621, 361))
    return image, template


def test_match_finds_template_center_normalized(scene):
    frame, template = scene
    hit = jp_vision.match(frame, template, name='button')
    assert hit is not None and hit.score > 0.95
    assert hit.nx == pytest.approx(560.5 / 1280, abs=0.01)
    assert hit.ny == pytest.approx(330.5 / 720, abs=0.01)


def test_match_miss_returns_none(scene):
    frame, template = scene
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(99)
    alien = Image.fromarray(rng.integers(0, 40, size=(61, 121, 3), dtype=np.uint8))
    assert jp_vision.match(frame, alien) is None


def test_roi_limits_search_area(scene):
    frame, template = scene
    assert jp_vision.match(frame, template, roi=(0.0, 0.0, 0.3, 0.3)) is None
    hit = jp_vision.match(frame, template, roi=(0.3, 0.3, 0.4, 0.4))
    assert hit is not None


def test_normalize_frame_scales_to_base(scene):
    frame, template = scene
    small = frame.resize((640, 360))
    normalized = jp_vision.normalize_frame(small)
    assert normalized.size == (1280, 720)
    assert jp_vision.match(normalized, template) is not None


def test_find_reports_missing_template(monkeypatch, tmp_path, scene):
    monkeypatch.setattr(jp_vision, 'TEMPLATE_DIR', tmp_path)
    frame, template = scene
    template.save(tmp_path / 'button.png')
    assert jp_vision.find(frame, 'button') is not None
    with pytest.raises(jp_vision.TemplateNotFound, match='画面标定'):
        jp_vision.find(frame, 'absent')


def test_matches_collects_hits(monkeypatch, tmp_path, scene):
    monkeypatch.setattr(jp_vision, 'TEMPLATE_DIR', tmp_path)
    frame, template = scene
    template.save(tmp_path / 'button.png')
    template.transpose(__import__('PIL.Image', fromlist=['Image']).FLIP_LEFT_RIGHT).save(
        tmp_path / 'flipped.png')
    found = jp_vision.matches(frame, ['button', 'flipped'])
    assert set(found) == {'button'}


def test_near_black_and_base_size_guard():
    from PIL import Image
    assert jp_vision.near_black(Image.new('RGB', (64, 64), (2, 2, 2)))
    assert not jp_vision.near_black(Image.new('RGB', (64, 64), (90, 90, 90)))
    assert jp_vision.base_size_ok(Image.new('RGB', (1920, 1080)))
    assert not jp_vision.base_size_ok(Image.new('RGB', (1000, 1000)))
