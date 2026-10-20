# -*- coding: utf-8 -*-
import numpy as np
import pytest

from touken import cn_ocr


class FakeEngine:
    def __init__(self):
        self.seen = []

    def __call__(self, arr):
        self.seen.append(arr)
        return [
            ([[10, 4], [40, 4], [40, 20], [10, 20]], '部队记录', 0.99),
            ([[0, 30], [8, 30], [8, 38], [0, 38]], '糊', 0.30),
        ], 0.1


class Roi:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h


@pytest.fixture
def fake_engine(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(cn_ocr, '_get_engine', lambda: engine)
    return engine


def frame():
    rng = np.random.default_rng(3)
    return rng.integers(0, 255, size=(720, 1280, 3), dtype=np.uint8)


def test_read_region_returns_full_frame_coords(fake_engine):
    out = cn_ocr.read_region(frame(), Roi(100, 200, 300, 100))
    assert len(out) == 1                      # 0.30 低分被过滤
    text, cx, cy, score = out[0]
    assert text == '部队记录'
    assert (cx, cy) == (125, 212)             # 局部中心 (25,12) + ROI 偏移
    assert score == pytest.approx(0.99)


def test_read_region_feeds_bgr_as_rgb(fake_engine):
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[10:20, 10:20] = (255, 0, 0)           # BGR 纯蓝块
    cn_ocr.read_region(img, Roi(0, 0, 100, 100))
    fed = fake_engine.seen[0]
    assert fed.shape == (100, 100, 3)
    assert fed[15, 15].tolist() == [0, 0, 255]  # 引擎看到的应是 RGB 红块
    assert fed.flags['C_CONTIGUOUS']


def test_read_region_crops_before_engine(fake_engine):
    cn_ocr.read_region(frame(), Roi(100, 200, 300, 100))
    assert fake_engine.seen[0].shape == (100, 300, 3)   # 只送裁后区域


def test_read_region_clamps_out_of_bounds_roi(fake_engine):
    cn_ocr.read_region(frame(), Roi(-50, 700, 300, 100))
    assert fake_engine.seen[0].shape == (20, 300, 3)   # 贴边裁剪不越界


def test_read_region_empty_inputs(fake_engine):
    assert cn_ocr.read_region(None, Roi(0, 0, 10, 10)) == []
    assert cn_ocr.read_region(frame(), Roi(500, 500, 0, 30)) == []


def test_text_matches_semantics():
    assert cn_ocr.text_matches('部队记录', '部队记录', 'exact')
    assert not cn_ocr.text_matches('部队记录一', '部队记录', 'exact')
    assert cn_ocr.text_matches('使用部队记录', '部队记录')
    assert cn_ocr.text_matches('部队', '部队记录')        # contains 双向
    assert not cn_ocr.text_matches('', '部队记录')        # 空串不命中
    assert not cn_ocr.text_matches('部队记录', '')
    # contains 是双向的：残片「伤」也命中「重伤」——安全敏感处不许用
    # contains，扫原始 token 比对（injury_from_ocr_tokens 守着这条）
    assert cn_ocr.text_matches('伤', '重伤')


def test_find_text_hit_and_miss(fake_engine):
    assert cn_ocr.find_text(frame(), Roi(0, 0, 500, 500),
                            '部队记录') == (25, 12)
    assert cn_ocr.find_text(frame(), Roi(0, 0, 500, 500),
                            '不存在') is None
    assert cn_ocr.find_text(frame(), Roi(0, 0, 500, 500),
                            '部队记录', match_mode='exact') == (25, 12)


# ---- maa_adapter 引擎路由 ----

from touken.maa_adapter import MAAAdapter, Point, Region


def _adapter(monkeypatch, engine, native_tokens):
    if engine is None:
        monkeypatch.delenv('MAAMARU_OCR_ENGINE', raising=False)
    else:
        monkeypatch.setenv('MAAMARU_OCR_ENGINE', engine)
    adapter = MAAAdapter(adb_path='x', adb_address='x', resource_dir='x')
    monkeypatch.setattr(adapter, 'screenshot', lambda: frame())
    monkeypatch.setattr(cn_ocr, 'read_region',
                        lambda image, roi, min_score=0.5: native_tokens)
    return adapter


def test_ocr_engine_defaults_to_maa(monkeypatch):
    adapter = _adapter(monkeypatch, None, [('目录', 1224, 38, 0.99)])
    called = []
    monkeypatch.setattr(adapter, '_ocr_maa',
                        lambda *a, **k: called.append(1) or Point(1, 2))
    assert adapter.ocr('目录', Region(1100, 0, 1280, 60)) == Point(1, 2)
    assert called  # 默认仍走 MAA，不碰自研


def test_ocr_native_engine_hit_and_miss(monkeypatch):
    adapter = _adapter(monkeypatch, 'native', [('部队记录', 1210, 150, 0.99)])
    assert adapter.ocr('部队记录', Region(1100, 100, 1280, 200)) == Point(1210, 150)
    assert adapter.ocr('不存在', Region(1100, 100, 1280, 200)) is None


def test_ocr_all_native_returns_points(monkeypatch):
    adapter = _adapter(monkeypatch, 'native',
                       [('中伤', 290, 153, 0.97), ('疲劳 100/100', 500, 200, 0.99)])
    out = adapter.ocr_all(Region(0, 90, 570, 690))
    assert out == [('中伤', Point(290, 153)), ('疲劳 100/100', Point(500, 200))]


def test_ocr_ab_mode_returns_maa_and_logs(monkeypatch, capsys):
    adapter = _adapter(monkeypatch, 'ab', [('目录', 1224, 38, 0.99)])
    monkeypatch.setattr(adapter, '_ocr_maa', lambda *a, **k: Point(1220, 40))
    monkeypatch.setattr(adapter, '_ocr_all_maa',
                        lambda *a, **k: [('目录', Point(1220, 40))])
    assert adapter.ocr('目录', Region(1100, 0, 1280, 60)) == Point(1220, 40)
    assert adapter.ocr_all(Region(1100, 0, 1280, 60)) == [('目录', Point(1220, 40))]
    out = capsys.readouterr().out
    assert '[AB文字] "目录": maa=命中(1220,40) native=命中(1224,38)' in out
    assert '[AB文字] 全量: maa=1词 native=1词' in out
    assert '分歧' not in out


def test_ocr_ab_mode_flags_divergence(monkeypatch, capsys):
    adapter = _adapter(monkeypatch, 'ab', [])  # native 读不到
    monkeypatch.setattr(adapter, '_ocr_maa', lambda *a, **k: Point(5, 6))
    assert adapter.ocr('目录', Region(0, 0, 100, 60)) == Point(5, 6)  # MAA 为准
    assert '⚠分歧' in capsys.readouterr().out
