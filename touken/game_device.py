# -*- coding: utf-8 -*-
"""双服设备窄接口（GameDevice 协议）。

玩法流程只认这层接口，不认服务器。坐标一律用**基准分辨率像素空间**
（int 像素，Point/Region 语义不变）：

- 国服基准 = 实际帧 1280×720，基准空间即像素空间（恒等）。
- 日服基准 = jp_vision.base_size() 锁存的标定分辨率；DMM 页面是固定
  宽度内容，窗口变矮只是视口裁剪，内容在「宽度归一化到基准宽」的空间
  里逐像素不变。不用 0~1 归一化坐标：视口高度变化时归一化 y 会漂
  （2026-10-12 事故），基准像素则锚死在内容上。

帧类型 device-native，协议不统一帧格式：国服 screenshot 返回 numpy
BGR 帧，日服返回宽度归一化后的 PIL RGB 帧。识别方法
（template_match/ocr）操作设备最近一次 screenshot 的缓存帧——与
DeviceAdapter 现有语义一致（screenshot(force=False) 用缓存，
template_match 内部 screenshot() 不 force）；要看清当下画面就
screenshot(force=True)。
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from .device_adapter import Point, Region


@runtime_checkable
class GameDevice(Protocol):
    """玩法层可见的设备能力全集；两服各自实现，坐标语义同上。"""

    @property
    def base_size(self) -> tuple[int, int]:
        """基准分辨率 (宽, 高)：本协议所有像素坐标都以此为空间。"""
        ...

    def screenshot(self, force: bool = False):
        """取帧（设备原生类型）；force=False 时有缓存用缓存。"""
        ...

    def click(self, point: Point) -> bool:
        """点基准空间像素坐标；落点无效返回 False，不盲点。"""
        ...

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 400) -> bool:
        """基准空间像素坐标滑动。"""
        ...

    def template_match(self, template: str, roi: Optional[Region] = None,
                       threshold: float = 0.7) -> Optional[Point]:
        """在最近缓存帧上模板匹配；命中返回基准空间中心点。"""
        ...

    def ocr(self, expected: str, roi: Optional[Region] = None,
            match_mode: str = 'contains') -> Optional[Point]:
        """在最近缓存帧上读字找目标；命中返回基准空间中心点。"""
        ...

    def exists(self, template: str, roi: Optional[Region] = None,
               threshold: float = 0.7) -> bool:
        """模板是否存在于最近缓存帧。"""
        ...
