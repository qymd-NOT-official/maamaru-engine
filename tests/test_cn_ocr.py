# -*- coding: utf-8 -*-
import numpy as np
import pytest

from touken import cn_ocr


class FakeEngine:
    def __init__(self):
        self.seen = []

    def __call__(self, arr):
        self.seen.append(arr)
        # box 坐标是引擎收到的 pad 图上的（真实引擎在 pad 图上跑，
        # 文字坐标带 16px 留白偏移），read_region 负责还原
        return [
            ([[26, 20], [56, 20], [56, 36], [26, 36]], '部队记录', 0.99),
            ([[16, 46], [24, 46], [24, 54], [16, 54]], '糊', 0.30),
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
    assert fed.shape == (132, 132, 3)              # pad 16 留白
    assert fed[15 + 16, 15 + 16].tolist() == [0, 0, 255]  # RGB 红块
    assert fed.flags['C_CONTIGUOUS']


def test_read_region_crops_before_engine(fake_engine):
    cn_ocr.read_region(frame(), Roi(100, 200, 300, 100))
    assert fake_engine.seen[0].shape == (132, 332, 3)  # 裁后区域 + pad


def test_read_region_clamps_out_of_bounds_roi(fake_engine):
    cn_ocr.read_region(frame(), Roi(-50, 700, 300, 100))
    # 贴边裁剪不越界；裁出 20x300，pad 后 52x332
    assert fake_engine.seen[0].shape == (52, 332, 3)


def test_read_region_pads_thin_roi(fake_engine):
    # 白山吉光事件：贴边裁剪的矮条 ROI det 检出全灭，留白是命门。
    # pad 后坐标必须还原到原始尺度再补 ROI 偏移。
    out = cn_ocr.read_region(frame(), Roi(100, 200, 300, 24))
    fed = fake_engine.seen[0]
    assert fed.shape[0] == 56                            # 24 + 16×2 留白
    text, cx, cy, _ = out[0]
    assert (cx, cy) == (125, 212)                        # pad 已还原
    assert text == '部队记录'


def test_read_region_empty_inputs(fake_engine):
    assert cn_ocr.read_region(None, Roi(0, 0, 10, 10)) == []
    assert cn_ocr.read_region(frame(), Roi(500, 500, 0, 30)) == []


class EmptyThenHitEngine:
    """第一次（pad 图）检空、第二次（原图退路）命中的引擎。"""

    def __init__(self):
        self.shapes = []

    def __call__(self, arr):
        self.shapes.append(arr.shape)
        if len(self.shapes) == 1:
            return [], 0.1
        return [([[10, 10], [30, 10], [30, 26], [10, 26]], '售罄', 0.99)], 0.1


def test_read_region_retries_without_pad_when_padded_pass_empty(monkeypatch):
    # 售罄事件：深色按钮 + REPLICATE 留白把边缘拉成无边暗带，det 整片
    # 漏检（原图反而 0.999 检出）。主路检空必须退路原图再试，且坐标
    # 按原图还原（无 pad 偏移）。
    engine = EmptyThenHitEngine()
    monkeypatch.setattr(cn_ocr, '_get_engine', lambda: engine)
    out = cn_ocr.read_region(frame(), Roi(100, 200, 330, 95))
    assert engine.shapes == [(127, 362, 3), (95, 330, 3)]  # pad 图 → 原图
    text, cx, cy, _ = out[0]
    assert text == '售罄'
    assert (cx, cy) == (120, 218)  # 局部 (20,18) + ROI 偏移，无 pad


def test_read_region_no_retry_when_padded_pass_hits(fake_engine):
    # 主路有 token 就不许双跑（成本红线）
    cn_ocr.read_region(frame(), Roi(100, 200, 300, 100))
    assert len(fake_engine.seen) == 1


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


def test_text_matches_traditional_simplified_normalization():
    # 游戏 UI 繁简混用：画面印「櫻吹雪」，引擎可能认成简体「樱吹雪」
    assert cn_ocr.text_matches('樱吹雪', '櫻吹雪')
    assert cn_ocr.text_matches('櫻吹雪', '樱吹雪')
    assert cn_ocr.text_matches('櫻吹雪', '櫻吹雪', 'exact')


def test_find_text_hit_and_miss(fake_engine):
    assert cn_ocr.find_text(frame(), Roi(0, 0, 500, 500),
                            '部队记录') == (25, 12)
    assert cn_ocr.find_text(frame(), Roi(0, 0, 500, 500),
                            '不存在') is None
    assert cn_ocr.find_text(frame(), Roi(0, 0, 500, 500),
                            '部队记录', match_mode='exact') == (25, 12)


# ---- device_adapter OCR 通道 ----

from touken.device_adapter import DeviceAdapter, Point, Region


def _adapter(monkeypatch, native_tokens):
    adapter = DeviceAdapter(adb_path='x', adb_address='x', resource_dir='x')
    monkeypatch.setattr(adapter, 'screenshot', lambda: frame())
    monkeypatch.setattr(cn_ocr, 'read_region',
                        lambda image, roi, min_score=0.5: native_tokens)
    return adapter


def test_ocr_hit_and_miss(monkeypatch):
    # 2026-10-20 转正：AB 双跑 3000+ 次定点查找，修完留白/检空退路后
    # 1085 次零分歧，自研 cn_ocr 毕业，OCR 只有这一条路
    adapter = _adapter(monkeypatch, [('目录', 1224, 38, 0.99)])
    assert adapter.ocr('目录', Region(1100, 0, 1280, 60)) == Point(1224, 38)
    assert adapter.ocr('不存在', Region(1100, 0, 1280, 60)) is None


def test_ocr_all_returns_points(monkeypatch):
    adapter = _adapter(monkeypatch,
                       [('中伤', 290, 153, 0.97), ('疲劳 100/100', 500, 200, 0.99)])
    out = adapter.ocr_all(Region(0, 90, 570, 690))
    assert out == [('中伤', Point(290, 153)), ('疲劳 100/100', Point(500, 200))]

