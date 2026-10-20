# -*- coding: utf-8 -*-
"""国服画面文字读取（OCR）：numpy BGR 帧 + 像素 ROI 形态。

与 jp_ocr 共用同一个懒加载引擎（仓内 PP-OCRv6 权重，引擎实例全局
只加载一次）；区别只在输入语义：国服链路是 maa_adapter 的 numpy BGR
帧 + 像素 ROI，返回坐标必须是全帧坐标（调用方拿着去点击）。

本模块刻意不 import maa_adapter（它会被 maa_adapter 的引擎路由引用，
反向依赖就成环）：ROI 只要是有 x/y/w/h 属性的对象即可，坐标以裸
整数元组进出，包成 Point 是调用方的事。
"""
from __future__ import annotations

import numpy as np

from .jp_ocr import _get_engine

# det 需要四周留白：贴边裁剪的文字行检出全灭（白山吉光事件——名字、
# 疲劳行、绿底按钮、阴刻标题全是贴边 ROI）。REPLICATE 复制边缘色，
# 不引入新边界。短边仍过小再放大兜底（文字物理像素高度是检出关键）。
_DET_PAD = 16
_MIN_SHORT_SIDE = 40
_MAX_SCALE = 3.0


def _upscale_small(rgb):
    """短边过小就放大（文字物理像素高度是检出关键），返回 (图, 倍率)。"""
    import cv2
    h, w = rgb.shape[:2]
    short = min(h, w)
    if short >= _MIN_SHORT_SIDE:
        return rgb, 1.0
    scale = min(_MAX_SCALE, _MIN_SHORT_SIDE / short)
    big = cv2.resize(rgb, (round(w * scale), round(h * scale)),
                     interpolation=cv2.INTER_CUBIC)
    return big, scale


def _prepare_for_det(crop):
    """pad 留白 + 小图放大，返回 (图, 坐标还原用的偏移, 放大倍率)。"""
    import cv2
    padded = cv2.copyMakeBorder(crop, _DET_PAD, _DET_PAD, _DET_PAD,
                                _DET_PAD, cv2.BORDER_REPLICATE)
    big, scale = _upscale_small(padded)
    return big, _DET_PAD, scale


def _read_tokens(rgb, min_score):
    """在准备好的图上跑引擎，返回 [(text, cx, cy, score)]（本图局部坐标）。"""
    result, _elapse = _get_engine()(rgb)
    out = []
    for box, text, score in (result or []):
        if score < min_score:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((str(text), (min(xs) + max(xs)) / 2,
                    (min(ys) + max(ys)) / 2, float(score)))
    return out


def read_region(image, roi, min_score: float = 0.5) -> list:
    """读帧内一块像素区域的文字。

    Args:
        image: numpy BGR 帧（maa_adapter.screenshot 的返回形态）。
        roi: 有 x/y/w/h 属性的像素区域（如 maa_adapter.Region）。
        min_score: 置信度下限，低于的 token 丢弃。

    Returns:
        [(text, cx, cy, score), ...]，(cx, cy) 是全帧坐标；
        图像缺失或区域无效返回 []。
    """
    if image is None:
        return []
    x, y = max(0, int(roi.x)), max(0, int(roi.y))
    h_img, w_img = image.shape[:2]
    x2, y2 = min(w_img, x + int(roi.w)), min(h_img, y + int(roi.h))
    if x2 <= x or y2 <= y:
        return []
    crop = image[y:y2, x:x2]
    # BGR → RGB；::-1 产生负 stride，onnx 不吃，拷一份连续内存
    rgb = np.ascontiguousarray(crop[:, :, ::-1])
    prepared, pad, scale = _prepare_for_det(rgb)
    out = _read_tokens(prepared, min_score)
    if not out:
        # 售罄事件：深色按钮 + REPLICATE 留白会把按钮边缘拉成无边暗带，
        # det 反而整片漏检（同一张图不 pad 反而 0.999 检出）。主路检空时
        # 退路用原图（必要时放大）再试一次；只有检空才付这次双跑成本。
        raw, raw_scale = _upscale_small(rgb)
        out = _read_tokens(raw, min_score)
        pad, scale = 0, raw_scale
    return [(text, x + int(cx / scale - pad), y + int(cy / scale - pad), score)
            for text, cx, cy, score in out]


# 游戏 UI 繁简混用（按钮素材沿用日服原文）：rec 倾向输出简体，
# 匹配前把两边都归一到简体。遇到的繁体字按需扩充。
_T2S = str.maketrans({'櫻': '樱'})


def text_matches(text, expected, match_mode: str = "contains") -> bool:
    """与 maa_adapter._ocr_text_matches 同语义；空串永远不算命中。
    比对前两边都过一遍繁简归一（画面印繁体、引擎可能认成简体）。"""
    text = str(text or "").strip().translate(_T2S)
    expected = str(expected or "").strip().translate(_T2S)
    if not text or not expected:
        return False
    if match_mode == "exact":
        return text == expected
    if match_mode == "contains":
        return expected in text or text in expected
    return False


def find_text(image, roi, expected, match_mode: str = "contains",
              min_score: float = 0.5):
    """读 ROI 并找目标文字，命中返回全帧坐标 (x, y)，否则 None。

    注意 contains 语义对 token 拆分敏感：目标词被引擎拆成两个 token
    时单个 token 都含不住它（这和 MAA OCR 行为一致，调用方应像
    find_deploy_button 那样拆词分别命中）。
    """
    for text, cx, cy, _score in read_region(image, roi, min_score):
        if text_matches(text, expected, match_mode):
            return (cx, cy)
    return None
