# -*- coding: utf-8 -*-
"""GameDevice 协议一致性 + JpGameDevice 换算逻辑。

日服无法真机验证，这里的换算是唯一验证手段，断言按像素级写：
基准空间（1508×1088 标定值）→ 视口归一化的每一步都要对得上。
"""
import pytest
from PIL import Image

from touken import jp_game_device, jp_ocr, jp_vision
from touken.device_adapter import DeviceAdapter, Point, Region
from touken.game_device import GameDevice
from touken.jp_browser import BrowserError

BASE = (1508, 1088)


class FakeBrowser:
    """JpBrowser 形状：记录换算前的入参，视口换算不归它管。"""

    def __init__(self, frame_size=BASE):
        self.frame = Image.new('RGB', frame_size, (60, 60, 70))
        self.shots = 0
        self.base_clicks = []      # (bx, by, base_width)
        self.swipes = []           # (nx1, ny1, nx2, ny2, duration_ms)
        self.click_error = None    # 设成异常实例后 click_base 抛它
        self.connected = 0
        self.closed = 0

    def connect(self):
        self.connected += 1
        return self

    def close(self):
        self.closed += 1

    def screenshot(self):
        self.shots += 1
        return self.frame

    def click_base(self, bx, by, base_width, settle=0.1):
        if self.click_error:
            raise self.click_error
        self.base_clicks.append((bx, by, base_width))

    def swipe(self, nx1, ny1, nx2, ny2, duration_ms=400):
        self.swipes.append((nx1, ny1, nx2, ny2, duration_ms))


@pytest.fixture
def device(monkeypatch, tmp_path):
    monkeypatch.setattr(jp_vision, '_baseline_path',
                        lambda: tmp_path / 'vision.json')
    monkeypatch.setattr(jp_vision, 'base_size', lambda: BASE)
    return jp_game_device.JpGameDevice(FakeBrowser())


# ---- 协议一致性 ----

def test_device_adapter_satisfies_protocol():
    adapter = DeviceAdapter('adb', '127.0.0.1:16384', 'resource/base')
    assert isinstance(adapter, GameDevice)
    assert adapter.base_size == (1280, 720)


def test_jp_game_device_satisfies_protocol(device):
    assert isinstance(device, GameDevice)
    assert device.base_size == BASE


# ---- 生命周期透传 ----

def test_lifecycle_passthrough():
    browser = FakeBrowser()
    with jp_game_device.JpGameDevice(browser) as device:
        assert device.base_size
    assert browser.connected == 1 and browser.closed == 1


# ---- 截图缓存 ----

def test_screenshot_caches_unless_forced(device):
    frame = device.screenshot()
    assert frame.size == BASE
    assert device.screenshot() is frame          # force=False 用缓存
    assert device._browser.shots == 1
    device.screenshot(force=True)
    assert device._browser.shots == 2


def test_screenshot_normalizes_width(monkeypatch, device):
    """运行帧宽度不等于基准宽时按宽度比等比缩放（高度随视口裁剪）。"""
    device._browser.frame = Image.new('RGB', (754, 544), (60, 60, 70))
    frame = device.screenshot(force=True)
    assert frame.size == (1508, 1088)


# ---- 点击换算 ----

def test_click_passes_base_pixels_and_width(device):
    assert device.click(Point(754, 544)) is True
    assert device._browser.base_clicks == [(754, 544, 1508)]


def test_click_out_of_viewport_returns_false(device):
    device._browser.click_error = ValueError('点击位置在当前视口之外')
    assert device.click(Point(754, 2000)) is False


def test_click_browser_error_propagates(device):
    device._browser.click_error = BrowserError('与游戏页面的连接已断开')
    with pytest.raises(BrowserError):
        device.click(Point(754, 544))


# ---- 滑动换算 ----

def test_swipe_normalizes_by_base_width_and_frame_height(device):
    assert device.swipe(754, 1088 - 1, 754, 108, duration_ms=400) is True
    nx1, ny1, nx2, ny2, ms = device._browser.swipes[0]
    assert nx1 == nx2 == pytest.approx(754 / 1508)
    assert ny1 == pytest.approx(1087 / 1088)
    assert ny2 == pytest.approx(108 / 1088)
    assert ms == 400


# ---- 模板匹配 ----

def test_template_match_hit_maps_to_base_pixels(device, monkeypatch):
    seen = {}

    def fake_find(frame, name, roi=None, threshold=0.85):
        seen.update(name=name, roi=roi, size=frame.size)
        return jp_vision.Match(name=name, score=0.99, nx=0.5, ny=0.25,
                               nw=0.1, nh=0.05)

    monkeypatch.setattr(jp_vision, 'find', fake_find)
    hit = device.template_match('menu/sally', Region(150, 108, 300, 200))
    assert hit == Point(754, 272)                # (0.5×1508, 0.25×1088)
    assert seen['name'] == 'menu/sally'
    assert seen['roi'] == pytest.approx(
        (150 / 1508, 108 / 1088, 300 / 1508, 200 / 1088))


def test_template_match_tolerates_png_suffix(device, monkeypatch):
    seen = {}

    def fake_find(frame, name, **kw):
        seen['name'] = name
        return None

    monkeypatch.setattr(jp_vision, 'find', fake_find)
    assert device.template_match('menu/sally.png') is None
    assert seen['name'] == 'menu/sally'


def test_template_match_accepts_pack_relative_path(device, monkeypatch):
    """素材包解析出的 image/ 相对路径 'jp/menu/sally(.png)'：剥 .png 和
    开头 'jp/' 段，jp_vision 收到的必须是 'menu/sally'（坐标换算不变）。"""
    seen = {}

    def fake_find(frame, name, roi=None, threshold=0.85):
        seen.update(name=name, roi=roi, size=frame.size)
        return jp_vision.Match(name=name, score=0.99, nx=0.5, ny=0.25,
                               nw=0.1, nh=0.05)

    monkeypatch.setattr(jp_vision, 'find', fake_find)
    hit = device.template_match('jp/menu/sally.png', Region(150, 108, 300, 200))
    assert seen['name'] == 'menu/sally'
    assert seen['roi'] == pytest.approx(
        (150 / 1508, 108 / 1088, 300 / 1508, 200 / 1088))
    assert hit == Point(754, 272)
    device._last_frame = None
    assert device.template_match('jp/menu/sally') == Point(754, 272)
    assert seen['name'] == 'menu/sally'   # 不带 .png 的包路径同样剥前缀


def test_template_match_pack_path_end_to_end_name(device, monkeypatch):
    """真 jp_vision 链路（真实模板文件）：jp/ 前缀剥掉后按 menu/sally 取文件。"""
    seen = {}

    real_load = jp_vision.load_template

    def spy_load(name):
        seen['name'] = name
        return real_load(name)

    monkeypatch.setattr(jp_vision, 'load_template', spy_load)
    monkeypatch.setattr(jp_vision, 'find', jp_vision.find)
    # FakeBrowser 纯色帧配不上真模板 → None；关键是加载器收到的名字
    assert device.template_match('jp/menu/sally.png') is None
    assert seen['name'] == 'menu/sally'


def test_template_match_legacy_name_untouched(device, monkeypatch):
    """旧写法 'menu/sally(.png)' 原样到达 jp_vision，不被 jp/ 规则误伤。"""
    seen = {}

    def fake_find(frame, name, **kw):
        seen['name'] = name
        return None

    monkeypatch.setattr(jp_vision, 'find', fake_find)
    assert device.template_match('menu/sally') is None
    assert seen['name'] == 'menu/sally'


def test_template_match_missing_template_returns_none(device, monkeypatch):
    def missing(name):
        raise jp_vision.TemplateNotFound(name)

    monkeypatch.setattr(jp_vision, 'load_template', missing)
    monkeypatch.setattr(jp_vision, 'find', jp_vision.find)  # 走真 find→load_template
    assert device.template_match('menu/sally') is None


def test_exists_matches_template_match(device, monkeypatch):
    monkeypatch.setattr(jp_vision, 'find',
                        lambda frame, name, **kw: jp_vision.Match(
                            name=name, score=0.9, nx=0.5, ny=0.5, nw=0.1, nh=0.1))
    assert device.exists('menu/sally') is True
    monkeypatch.setattr(jp_vision, 'find', lambda frame, name, **kw: None)
    device._last_frame = None
    assert device.exists('menu/sally') is False


# ---- OCR ----

def _fake_tokens(tokens):
    return lambda image, roi=None, min_score=0.5: tokens


def test_ocr_contains_hit_returns_base_point(device, monkeypatch):
    monkeypatch.setattr(jp_ocr, 'read_tokens',
                        _fake_tokens([('本丸です', 0.99, 100.0, 50.0)]))
    hit = device.ocr('本丸')
    assert hit == Point(100, 50)


def test_ocr_exact_requires_full_match(device, monkeypatch):
    monkeypatch.setattr(jp_ocr, 'read_tokens',
                        _fake_tokens([('本丸です', 0.99, 100.0, 50.0)]))
    assert device.ocr('本丸です', match_mode='exact') == Point(100, 50)
    device._last_frame = None
    assert device.ocr('本丸', match_mode='exact') is None


def test_ocr_roi_crops_and_offsets_point(device, monkeypatch):
    seen = {}

    def fake_read_tokens(image, roi=None, min_score=0.5):
        seen['size'] = image.size
        return [('出撃', 0.99, 50.0, 40.0)]

    monkeypatch.setattr(jp_ocr, 'read_tokens', fake_read_tokens)
    hit = device.ocr('出撃', Region(100, 100, 400, 200))
    assert seen['size'] == (400, 200)            # 像素框裁剪
    assert hit == Point(150, 140)                # 加回裁剪偏移


def test_ocr_no_match_returns_none(device, monkeypatch):
    monkeypatch.setattr(jp_ocr, 'read_tokens',
                        _fake_tokens([('远征', 0.99, 10.0, 10.0)]))
    assert device.ocr('本丸') is None
