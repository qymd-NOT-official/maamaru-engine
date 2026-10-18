# -*- coding: utf-8 -*-
import sys

import pytest
from PIL import Image

from touken import jp_ocr


class FakeEngine:
    def __init__(self):
        self.seen_shapes = []

    def __call__(self, arr):
        self.seen_shapes.append(arr.shape)
        return [
            ([[0, 0], [1, 0], [1, 1], [0, 1]], '31/31', 0.99),
            ([[0, 0], [1, 0], [1, 1], [0, 1]], '糊掉的字', 0.30),
        ], 0.1


@pytest.fixture
def fake_engine(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(jp_ocr, '_engine', engine)
    return engine


def frame():
    return Image.new('RGB', (1508, 944), (60, 60, 70))


def test_crop_roi_normalized_math():
    cropped = jp_ocr.crop_roi(frame(), (0.25, 0.5, 0.5, 0.25))
    assert cropped.size == (754, 236)


def test_read_filters_low_score(fake_engine):
    lines = jp_ocr.read(frame())
    assert lines == [('31/31', pytest.approx(0.99))]
    assert fake_engine.seen_shapes[0][:2] == (944, 1508)  # 整帧


def test_read_with_roi_crops_first(fake_engine):
    jp_ocr.read(frame(), roi=(0.0, 0.0, 0.5, 0.5))
    assert fake_engine.seen_shapes[0][:2] == (472, 754)   # 只送裁后区域


def test_read_text_joins_and_filters(fake_engine):
    assert jp_ocr.read_text(frame()) == '31/31'


def test_missing_component_has_player_facing_message(monkeypatch):
    monkeypatch.setattr(jp_ocr, '_engine', None)
    monkeypatch.setitem(sys.modules, 'rapidocr_onnxruntime', None)
    with pytest.raises(RuntimeError, match='文字识别组件没装上'):
        jp_ocr.read(frame())
