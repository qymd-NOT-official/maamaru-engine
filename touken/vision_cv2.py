# -*- coding: utf-8 -*-
"""国服侧自研模板匹配（cv2），拆 MAA 的替换件。

语义对齐 MAAAdapter 现有约定：帧是 numpy BGR 数组，ROI 是像素
(x, y, w, h)（(0,0,0,0) 或 None = 全屏），返回命中中心的像素坐标与分数。
灰度 + TM_CCOEFF_NORMED，与 MaaFW 模板匹配的底层算法同族；
分差用 A/B 模式实测校准（MAAMARU_TEMPLATE_ENGINE=ab）。

中文文件名注意：cv2.imread 在 Windows 下不吃非 ASCII 路径，
模板一律走 PIL 加载。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

_cache: dict[str, np.ndarray] = {}


def load_template(path: Path) -> np.ndarray:
    """按路径载模板（灰度），带缓存。文件不存在抛 FileNotFoundError。"""
    key = str(path)
    if key not in _cache:
        with Image.open(path) as img:
            arr = np.asarray(img.convert('RGB'))
        _cache[key] = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    return _cache[key]


def match(image_bgr: np.ndarray, template_path: Path,
          roi: tuple[int, int, int, int] | None = None,
          threshold: float = 0.7) -> tuple[int, int, float] | None:
    """在帧里找模板；命中返回 (中心x, 中心y, 分数)，未命中返回 None。"""
    cut = load_template(template_path)
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    origin = (0, 0)
    if roi:
        x, y, w, h = roi
        if (x, y, w, h) != (0, 0, 0, 0):
            gray = gray[y:y + h, x:x + w]
            origin = (x, y)
    th, tw = cut.shape
    if gray.shape[0] < th or gray.shape[1] < tw:
        return None
    scores = cv2.matchTemplate(gray, cut, cv2.TM_CCOEFF_NORMED)
    _, best, _, (px, py) = cv2.minMaxLoc(scores)
    if best < threshold:
        return None
    return (px + origin[0] + tw // 2, py + origin[1] + th // 2, float(best))


def match_score(image_bgr: np.ndarray, template_path: Path,
                roi: tuple[int, int, int, int] | None = None,
                threshold: float = 0.5) -> float:
    """只取分数；低于阈值或未命中返回 0.0（对齐 MAA 的过滤语义）。"""
    hit = match(image_bgr, template_path, roi, threshold)
    return hit[2] if hit else 0.0
