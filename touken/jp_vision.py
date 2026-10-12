# -*- coding: utf-8 -*-
"""日服游戏画面的图形识别：帧归一化 + 模板匹配。

v1 不上 OCR：文字类判断一律回避，全靠图形模板。模板按同源铁律从实跑帧
裁剪（resource/base/image/jp/，由 jp_capture 采集后制作）；运行帧先
resize 到标定基准分辨率再匹配，吸收浏览器窗口缩放带来的尺寸漂移。
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from .runtime_paths import RESOURCE_DIR

TEMPLATE_DIR = RESOURCE_DIR / 'image' / 'jp'
FALLBACK_BASE = (1280, 720)
DEFAULT_THRESHOLD = 0.85


def _baseline_path():
    from .runtime_paths import JP_DATA_DIR
    return JP_DATA_DIR / 'vision.json'


def base_size() -> tuple[int, int]:
    """标定基准分辨率（宽, 高）：优先读 jp_capture 标定时锁存的值。"""
    import json
    try:
        data = json.loads(_baseline_path().read_text(encoding='utf-8'))
        w, h = data['base']
        if w > 0 and h > 0:
            return (w, h)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return FALLBACK_BASE


def lock_base_size(width: int, height: int) -> None:
    """标定采集时锁存基准分辨率；之后窗口尺寸变了会被出阵熔断拦住。"""
    import json
    path = _baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'base': [width, height]}, ensure_ascii=False),
                    encoding='utf-8')


class TemplateNotFound(FileNotFoundError):
    """模板缺失：说明标定素材还没做全，出阵状态机应把它当「认不出画面」处理。"""


class Match(NamedTuple):
    """一次模板命中；坐标均为画面归一化值（0-1），直接可喂 JpBrowser.click。"""
    name: str
    score: float
    nx: float
    ny: float
    nw: float
    nh: float


def load_template(name: str):
    """按名取模板（PIL RGB）；name 不含扩展名，如 'sally/confirm'。"""
    from PIL import Image
    path = TEMPLATE_DIR / f'{name}.png'
    if not path.is_file():
        raise TemplateNotFound(f'缺少日服模板 {name}，请先完成画面标定')
    with Image.open(path) as picture:
        picture.load()
        return picture.convert('RGB')


def normalize_frame(image, base: tuple[int, int] | None = None):
    base = base or base_size()
    """把运行帧缩到基准分辨率；已是基准尺寸则原样返回。"""
    if image.size == tuple(base):
        return image
    return image.resize(tuple(base))


def near_black(image, ceiling: int = 8) -> bool:
    return all(high <= ceiling for low, high in image.getextrema())


def _to_gray(image):
    import cv2
    import numpy as np
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)


def match(frame, template, name: str = '', roi: tuple[float, float, float, float] | None = None,
          threshold: float = DEFAULT_THRESHOLD) -> Match | None:
    """在（已归一化的）帧里找模板；roi 为归一化 (x, y, w, h)，限定搜索区域。

    命中返回 Match（中心点归一化坐标），未命中返回 None。"""
    import cv2
    gray = _to_gray(frame)
    cut = _to_gray(template)
    fh, fw = gray.shape
    if roi is not None:
        rx, ry, rw, rh = roi
        x0, y0 = int(rx * fw), int(ry * fh)
        x1, y1 = int((rx + rw) * fw), int((ry + rh) * fh)
        origin = (x0, y0)
        gray = gray[y0:y1, x0:x1]
    else:
        origin = (0, 0)
    th, tw = cut.shape
    if gray.shape[0] < th or gray.shape[1] < tw:
        return None
    scores = cv2.matchTemplate(gray, cut, cv2.TM_CCOEFF_NORMED)
    _, best, _, (px, py) = cv2.minMaxLoc(scores)
    if best < threshold:
        return None
    cx, cy = px + origin[0] + tw / 2, py + origin[1] + th / 2
    return Match(name=name, score=float(best),
                 nx=cx / fw, ny=cy / fh, nw=tw / fw, nh=th / fh)


def find(frame, name: str, roi: tuple[float, float, float, float] | None = None,
         threshold: float = DEFAULT_THRESHOLD) -> Match | None:
    """归一化帧 + 按名取模板 + 匹配，一步完成。"""
    return match(normalize_frame(frame), load_template(name),
                 name=name, roi=roi, threshold=threshold)


def matches(frame, names: list[str],
            threshold: float = DEFAULT_THRESHOLD) -> dict[str, Match]:
    """一次归一化帧，找一组模板；返回命中的 {name: Match}。"""
    normalized = normalize_frame(frame)
    found = {}
    for name in names:
        hit = match(normalized, load_template(name), name=name, threshold=threshold)
        if hit:
            found[name] = hit
    return found


def base_size_ok(image, base: tuple[int, int] | None = None, tolerance: float = 0.02) -> bool:
    base = base or base_size()
    """画面宽高比与基准一致才允许跑（窗口拖变形了模板会全线漂移）。"""
    w, h = image.size
    if w <= 0 or h <= 0:
        return False
    bw, bh = base
    return abs(w / h - bw / bh) <= tolerance
