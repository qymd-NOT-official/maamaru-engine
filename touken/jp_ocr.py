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

from .runtime_paths import RESOURCE_DIR

_engine = None

# 优先用仓里随包发行的 PP-OCRv6 三件套（与国服同源权重，日文也在
# 字表里，实测比引擎自带模型更快更准）；文件缺失时回退引擎自带模型。
_MODEL_DIR = RESOURCE_DIR / 'model' / 'ocr'

# 引擎参数（2026-10-21 真机Benchmark，笔记本上每次 OCR 2~4 秒的元凶）：
# - det_limit_type 'min'+736 是默认值，会把小 ROI 短边无脑放大到 736
#   （480×65 标题栏 → 736×5449 巨图），慢 10 倍且对检出毫无帮助；
#   'max'+2000 = 保持原生分辨率，只压超限大图（我们帧最大 1280，不触发）。
# - use_cls=False：游戏文字永远是正的，方向分类器白跑，关掉再省一刀。
# 精度验证：售罄证物 + 介绍页竖排书法/对话文本，与旧配置逐项对齐。
_ENGINE_KW = dict(use_cls=False, det_limit_type='max', det_limit_side_len=2000)

# 省电模式（设置-模拟器-性能档位）：限制 onnxruntime 推理线程数，
# CPU 占用平缓，笔记本散热差的场景画面不卡。实测小 ROI 和整屏
# 都不吃亏甚至更快（小图多线程同步开销大于算力收益），
# 2026-10-21 起 eco 就是默认档，fast 留作对照备胎。
_ECO_ENGINE_KW = dict(intra_op_num_threads=2, inter_op_num_threads=1)


def _performance_mode() -> str:
    """读主配置的 performance_mode；缺省/读不到都算 eco。"""
    try:
        import json
        from .runtime_paths import CONFIG_PATH
        cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        mode = str(cfg.get('performance_mode', '')).strip().lower()
        return mode if mode in ('fast', 'eco') else 'eco'
    except Exception:
        return 'eco'


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                '文字识别组件没装上（缺 rapidocr_onnxruntime），'
                '请重新安装まあ丸或联系作者') from exc
        kw = dict(_ENGINE_KW)
        if _performance_mode() == 'eco':
            kw.update(_ECO_ENGINE_KW)
        det, rec, keys = (_MODEL_DIR / 'det.onnx', _MODEL_DIR / 'rec.onnx',
                          _MODEL_DIR / 'keys.txt')
        if all(p.is_file() for p in (det, rec, keys)):
            _engine = RapidOCR(det_model_path=str(det),
                               rec_model_path=str(rec),
                               rec_keys_path=str(keys),
                               **kw)
        else:
            _engine = RapidOCR(**kw)
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
