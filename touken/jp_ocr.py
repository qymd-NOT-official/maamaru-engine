# -*- coding: utf-8 -*-
"""日服画面文字读取（OCR）：懒加载引擎 + 限定 ROI。

引擎首次使用才加载（约 1-2 秒）；没装文字识别组件的环境里 import
本模块不受影响，真正读字时才报错。只认 ROI 内的小字：全帧 OCR 又慢
又容易把标题大字搅进来，调用方必须尽量把区域限定到目标文字附近。

ROI 用归一化坐标 (nx, ny, nw, nh)，相对传入帧的宽高：DMM 页面是固定
宽度内容，窗口变矮只是视口裁剪，内容相对位置不变，归一化 ROI 天然免疫。
"""
from __future__ import annotations

import numpy as np

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                '文字识别组件没装上（缺 rapidocr_onnxruntime），'
                '请重新安装まあ丸或联系作者') from exc
        _engine = RapidOCR()
    return _engine


def crop_roi(image, roi: tuple[float, float, float, float]):
    """按归一化 (nx, ny, nw, nh) 裁剪帧区域。"""
    nx, ny, nw, nh = roi
    w, h = image.size
    return image.crop((int(nx * w), int(ny * h),
                       int((nx + nw) * w), int((ny + nh) * h)))


def read(image, roi: tuple[float, float, float, float] | None = None,
         min_score: float = 0.5) -> list[tuple[str, float]]:
    """读一块区域里的文字，返回 [(文本, 置信度), ...]（已按置信度过滤）。

    roi 为 None 读整帧（慢，尽量避免）。识别结果为空返回空列表。"""
    if roi is not None:
        image = crop_roi(image, roi)
    result, _elapse = _get_engine()(np.array(image.convert('RGB')))
    return [(text, float(score)) for _box, text, score in (result or [])
            if score >= min_score]


def read_text(image, roi: tuple[float, float, float, float] | None = None,
              min_score: float = 0.5, sep: str = ' ') -> str:
    """读一块区域并拼成一行文本；什么都没读到返回空串。"""
    return sep.join(text for text, _score in read(image, roi, min_score))
