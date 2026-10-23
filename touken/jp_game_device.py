# -*- coding: utf-8 -*-
"""日服侧的 GameDevice 实现：JpBrowser 组合件 + 基准分辨率像素空间。

组合（不是继承）JpBrowser：连接/视口熔断/取帧/输入全在 JpBrowser，
本层只做协议换算——对外一律基准分辨率像素（jp_vision.base_size()
锁存的标定空间），对内把基准像素换算成视口归一化坐标喂 browser。

帧语义：screenshot 返回宽度归一化后的 PIL RGB 帧（基准宽 × 内容高），
与模板同源；template_match/ocr 操作最近一次 screenshot 的缓存帧，
与 DeviceAdapter 的缓存语义一致。
"""
from __future__ import annotations

from typing import Optional

from . import cn_ocr, jp_ocr, jp_vision
from .device_adapter import Point, Region, _safe_print
from .jp_browser import JpBrowser


class JpGameDevice:
    """日服游戏页的 GameDevice；坐标一律基准分辨率像素。"""

    def __init__(self, browser: JpBrowser):
        self._browser = browser
        self._last_frame = None  # 缓存最近一次归一化帧

    # ---- 生命周期（透传 browser） ----

    def connect(self) -> "JpGameDevice":
        self._browser.connect()
        return self

    def close(self) -> None:
        self._browser.close()

    def __enter__(self) -> "JpGameDevice":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # 出阵状态机用到的浏览器级操作，原样透传。
    def reload(self, settle_timeout: float = 20.0) -> None:
        self._browser.reload(settle_timeout=settle_timeout)

    def relaxed_view(self):
        return self._browser.relaxed_view()

    @property
    def base_size(self) -> tuple[int, int]:
        """基准分辨率 = jp_capture 标定时锁存的值。"""
        return jp_vision.base_size()

    # ---- 画面 ----

    def screenshot(self, force: bool = False):
        """取一帧宽度归一化后的 PIL RGB 帧；force=False 有缓存用缓存。"""
        if not force and self._last_frame is not None:
            return self._last_frame
        frame = jp_vision.normalize_frame(self._browser.screenshot())
        self._last_frame = frame
        return frame

    # ---- 输入 ----

    def click(self, point: Point) -> bool:
        """点基准空间像素；落在视口外打日志返回 False，连接死向上抛。"""
        base_w, _ = self.base_size
        try:
            self._browser.click_base(point.x, point.y, base_w)
        except ValueError as exc:
            _safe_print(f'[JpDevice] 点击 ({point.x}, {point.y}) 落点无效，已跳过: {exc}')
            return False
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 400) -> bool:
        """基准空间像素滑动；x 除基准宽，y 除当前归一化帧高。"""
        base_w, _ = self.base_size
        frame = self.screenshot()
        if frame is None:
            _safe_print('[JpDevice] 滑动失败：没有可用画面')
            return False
        frame_h = frame.size[1]
        try:
            self._browser.swipe(x1 / base_w, y1 / frame_h,
                                x2 / base_w, y2 / frame_h,
                                duration_ms=duration_ms)
        except ValueError as exc:
            _safe_print(f'[JpDevice] 滑动 ({x1},{y1}) → ({x2},{y2}) 落点无效，已跳过: {exc}')
            return False
        return True

    # ---- 识别（操作缓存帧） ----

    def template_match(self, template: str, roi: Optional[Region] = None,
                       threshold: float = 0.7) -> Optional[Point]:
        """模板匹配：命中返回基准空间中心点，模板缺失打日志返回 None。"""
        name = template[:-4] if template.endswith('.png') else template
        frame = self.screenshot()
        if frame is None:
            return None
        fw, fh = frame.size
        nroi = None
        if roi is not None:
            nroi = (roi.x / fw, roi.y / fh, roi.w / fw, roi.h / fh)
        try:
            hit = jp_vision.find(frame, name, roi=nroi, threshold=threshold)
        except jp_vision.TemplateNotFound:
            _safe_print(f'[Jp模板] 模板缺失: {name}')
            return None
        if hit is None:
            _safe_print(f'[Jp模板] 模板未命中: {name}, threshold={threshold}')
            return None
        _safe_print(f'[Jp模板] 模板命中: {name} at '
                    f'({hit.nx * fw:.0f}, {hit.ny * fh:.0f}), score={hit.score:.3f}')
        return Point(round(hit.nx * fw), round(hit.ny * fh))

    def ocr(self, expected: str, roi: Optional[Region] = None,
            match_mode: str = 'contains') -> Optional[Point]:
        """OCR 找目标文字：命中返回基准空间中心点，失败返回 None。"""
        frame = self.screenshot()
        if frame is None:
            return None
        offset_x = offset_y = 0
        if roi is not None:
            fw, fh = frame.size
            offset_x, offset_y = max(0, int(roi.x)), max(0, int(roi.y))
            x2, y2 = min(fw, offset_x + int(roi.w)), min(fh, offset_y + int(roi.h))
            if x2 <= offset_x or y2 <= offset_y:
                return None
            frame = frame.crop((offset_x, offset_y, x2, y2))
        tokens = jp_ocr.read_tokens(frame)
        for text, score, cx, cy in tokens:
            if cn_ocr.text_matches(text, expected, match_mode):
                px, py = round(cx + offset_x), round(cy + offset_y)
                _safe_print(f'[JpOCR] OCR 命中: "{text}" (目标: "{expected}") '
                            f'at ({px}, {py}), score={score:.3f}')
                return Point(px, py)
        _safe_print(f'[JpOCR] OCR 识别到文字但未匹配: '
                    f'{[t for t, *_ in tokens]}, expected="{expected}"')
        return None

    def exists(self, template: str, roi: Optional[Region] = None,
               threshold: float = 0.7) -> bool:
        """判断模板是否存在"""
        return self.template_match(template, roi, threshold) is not None
