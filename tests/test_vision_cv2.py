# -*- coding: utf-8 -*-
import numpy as np
import pytest
from PIL import Image

from touken import vision_cv2
from touken.maa_adapter import MAAAdapter, Point, Region


def make_frame_and_template(tmp_path, box=(100, 60, 40, 30)):
    """灰噪声底 + 一块独特图案的模板；返回 (帧BGR, 模板路径, 模板中心)。"""
    rng = np.random.default_rng(7)
    frame = rng.integers(40, 80, size=(720, 1280), dtype=np.uint8)
    x, y, w, h = box
    patch = rng.integers(120, 255, size=(h, w), dtype=np.uint8)
    frame[y:y + h, x:x + w] = patch
    frame_bgr = np.stack([frame] * 3, axis=-1)
    tpl_path = tmp_path / 'tpl.png'
    Image.fromarray(patch).save(tpl_path)
    vision_cv2._cache.clear()
    return frame_bgr, tpl_path, (x + w // 2, y + h // 2)


def test_match_hits_center(tmp_path):
    frame, tpl, (cx, cy) = make_frame_and_template(tmp_path)
    hit = vision_cv2.match(frame, tpl)
    assert hit is not None
    assert abs(hit[0] - cx) <= 1 and abs(hit[1] - cy) <= 1
    assert hit[2] > 0.99


def test_match_miss_returns_none(tmp_path):
    frame, tpl, _ = make_frame_and_template(tmp_path)
    vision_cv2._cache.clear()
    other = np.zeros_like(frame)  # 纯黑帧里找不到花模板
    assert vision_cv2.match(other, tpl) is None


def test_match_roi_offsets_result(tmp_path):
    frame, tpl, (cx, cy) = make_frame_and_template(tmp_path)
    # ROI 框住模板：结果坐标仍是全帧坐标
    hit = vision_cv2.match(frame, tpl, roi=(50, 30, 200, 120))
    assert hit is not None and abs(hit[0] - cx) <= 1
    # ROI 不含模板：找不到
    assert vision_cv2.match(frame, tpl, roi=(600, 400, 300, 200)) is None


def test_match_score_threshold_gate(tmp_path):
    frame, tpl, _ = make_frame_and_template(tmp_path)
    assert vision_cv2.match_score(frame, tpl) > 0.99
    assert vision_cv2.match_score(frame, tpl, threshold=1.01) == 0.0


def test_missing_template_raises(tmp_path):
    frame, _, _ = make_frame_and_template(tmp_path)
    with pytest.raises(FileNotFoundError):
        vision_cv2.match(frame, tmp_path / '不存在.png')


# ---- adapter 路由 ----

def make_adapter(tmp_path, monkeypatch, engine):
    monkeypatch.setenv('MAAMARU_TEMPLATE_ENGINE', engine)
    adapter = MAAAdapter(adb_path='x', adb_address='x',
                         resource_dir=str(tmp_path))
    return adapter


def test_adapter_cv2_engine(tmp_path, monkeypatch):
    (tmp_path / 'image').mkdir()
    frame, tpl, (cx, cy) = make_frame_and_template(tmp_path / 'image')
    (tmp_path / 'image' / 'tpl.png').write_bytes(tpl.read_bytes())
    vision_cv2._cache.clear()
    adapter = make_adapter(tmp_path, monkeypatch, 'cv2')
    monkeypatch.setattr(adapter, 'screenshot', lambda: frame)
    point = adapter.template_match('tpl.png')
    assert isinstance(point, Point)
    assert abs(point.x - cx) <= 1 and abs(point.y - cy) <= 1
    assert adapter.template_match_score('tpl.png') > 0.99
    assert adapter.exists('tpl.png')


def test_adapter_default_engine_is_maa(tmp_path, monkeypatch):
    monkeypatch.delenv('MAAMARU_TEMPLATE_ENGINE', raising=False)
    adapter = MAAAdapter(adb_path='x', adb_address='x',
                         resource_dir=str(tmp_path))
    called = []
    monkeypatch.setattr(adapter, '_template_match_maa',
                        lambda *a, **k: called.append(1) or Point(1, 2))
    assert adapter.template_match('任意.png') == Point(1, 2)
    assert called  # 默认走 MAA，不碰 cv2


def test_adapter_ab_mode_returns_maa_result(tmp_path, monkeypatch, capsys):
    (tmp_path / 'image').mkdir()
    frame, tpl, _ = make_frame_and_template(tmp_path / 'image')
    (tmp_path / 'image' / 'tpl.png').write_bytes(tpl.read_bytes())
    vision_cv2._cache.clear()
    adapter = make_adapter(tmp_path, monkeypatch, 'ab')
    monkeypatch.setattr(adapter, 'screenshot', lambda: frame)
    monkeypatch.setattr(adapter, '_template_match_maa',
                        lambda *a, **k: Point(9, 9))
    point = adapter.template_match('tpl.png')
    assert point == Point(9, 9)   # AB 模式以 MAA 为准
    assert '[AB模板] tpl.png' in capsys.readouterr().out
