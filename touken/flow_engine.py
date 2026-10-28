# -*- coding: utf-8 -*-
"""
流程引擎 —— 流程工坊（Flow Lab）拼出来的自定义流程的解释器

老大在面板里把「认」（模板/ROI/OCR）+「点」（点击/滑动/等待）的线性卡片
流拼成流程，本模块负责校验（normalize）、存取（STATUS_DIR/custom_flows.json）
和逐步执行（run_flow）。只准复用已有封装（navigator 的识别语义、
battle 的 _safe_depart_stream 安全出阵链），不新造轮子。

- 纯解释器：不 import panel 任何东西，方便单测与复用。
- ROI 在 JSON 里统一 xyxy 四元数组，引擎内 roi_4to4 转 Region。
- 步骤翻车消息必须 ✗ 开头、且能被 report_judge._is_fail 认出，不许假绿。
- 出阵确认链是红线：builtin 积木写死注册表，流程里不准拿裸 click 手搓。
- 双服素材走 ServerPack（touken/server_pack.py，docs/dual-server-pipeline.md 3.2）：
  流程参数可以写 @逻辑名，识别/点击/ROI/导航执行前经 ctx["pack"] 解析成
  本服的具体模板/OCR 词/ROI/坐标/导航目标；官方流程只准写 @逻辑名，
  原始值（含盲点坐标这类逃生舱）只留给私货兼容。
- 翻车收拾是引擎的事（设计文档 3.4）：步骤没显式写 on_fail 时默认
  「回本丸收拾残局 → 跳过这步继续」；显式 stop/continue/retry 旧数据原样；
  安全出阵和开目录失败不可自动收拾（安全拦截不能带病继续）。
"""

import copy
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .flows.report_judge import _is_fail
from .device_adapter import Point, Region, roi_4to4
from .runtime_paths import RESOURCE_DIR, STATUS_DIR
from .server_pack import load_server_pack

MAX_STEPS = 50            # 单流程最多步数（保存时校验）
MAX_EXEC_STEPS = 500      # 单轮最多执行步数（jump_if 死循环保险，超限按翻车停）
FRAME_W, FRAME_H = 1280, 720  # 国服默认画面尺寸（MuMu 1280×720）
DEFAULT_BASE_SIZE = (FRAME_W, FRAME_H)  # 坐标/ROI 校验的默认基准（normalize 可换）
VALID_ON_FAIL = ("stop", "continue", "retry", "recover")
RETRY_TIMES_MIN, RETRY_TIMES_MAX = 1, 5
RETRY_INTERVAL_MIN, RETRY_INTERVAL_MAX = 0.0, 60.0

# ⏭ = 翻车即停、这步没轮到跑——不是翻车，成绩单不算失败
_SKIPPED_STATUS = "⏭ 跳过（翻车即停）"

# 点安全区跳动画的全局默认（navigator.NavigationMixin.DEFAULT_SKIP_POINT 同款）
DEFAULT_SKIP_POINT = (775, 695)


class FlowError(ValueError):
    """流程/步骤校验失败（API 层转成 400）。"""


def _sleep(seconds: float) -> None:
    """抽出来只为测试能 patch 掉等待。"""
    time.sleep(seconds)


# ── 参数校验小件 ──

def _int(value, label: str, default: int, lo: int, hi: int) -> int:
    """整数参数：缺省给默认，越界收进范围，不是数字就报错。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FlowError(f"{label} 必须是数字")
    if isinstance(value, float) and not value.is_integer():
        raise FlowError(f"{label} 必须是整数")
    return max(lo, min(hi, int(value)))


def _fnum(value, label: str, default: float, lo: float, hi: float) -> float:
    """浮点参数：语义与 _int 同款。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FlowError(f"{label} 必须是数字")
    return max(lo, min(hi, float(value)))


def _threshold(value, default: float = 0.7) -> float:
    return _fnum(value, "threshold", default, 0.0, 1.0)


def _is_logical_ref(value) -> bool:
    """是不是素材包逻辑引用（@名字）。"""
    return isinstance(value, str) and value.startswith("@")


def _check_ref_name(value, label: str) -> str:
    """@引用在 normalize 期只做格式校验；存在性/越界执行时经素材包解析把关。"""
    name = value[1:].strip()
    if not name:
        raise FlowError(f"{label}：@ 后面要接逻辑名，不能只写一个 @")
    return "@" + name


def _no_official_raw(official: bool, value, label: str):
    """官方流程只准写 @逻辑名；原始值（路径/文字/坐标）一律拒。"""
    if official and value not in (None, "") and not _is_logical_ref(value):
        raise FlowError(f"{label}：官方流程只准写 @逻辑名（素材包引用），"
                        f"不收服务器专属的原始值 {value!r}")


def _template_path(value, label: str = "模板路径") -> str:
    """模板必须是 image/ 下的相对路径：不许绝对、不许 ..、限 .png。"""
    if not isinstance(value, str):
        raise FlowError(f"{label}必须是字符串")
    path = value.strip().replace("\\", "/")
    if not path or path.startswith("/") or ":" in path.split("/")[0]:
        raise FlowError(f"{label}必须是相对路径（如 event/入口.png）")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise FlowError(f"{label}不许包含 .. 或空目录段")
    if not path.lower().endswith(".png"):
        raise FlowError(f"{label}必须指向 .png 模板")
    return path


def clean_template_path(value) -> str:
    """公开版模板路径白名单校验（panel 层挑图、发图也用同一套规矩）。"""
    return _template_path(value)


def _optional_template(params: dict, label: str = "模板路径",
                       official: bool = False) -> str | None:
    value = params.get("template")
    if value in (None, ""):
        return None
    if _is_logical_ref(value):
        return _check_ref_name(value, label)
    _no_official_raw(official, value, label)
    return _template_path(value, label)


def _ocr_text(params: dict, label: str = "要认的文字",
              official: bool = False) -> str | None:
    value = params.get("ocr_expected")
    if value in (None, ""):
        return None
    if _is_logical_ref(value):
        return _check_ref_name(value, label)
    _no_official_raw(official, value, label)
    if not isinstance(value, str):
        raise FlowError(f"{label}必须是字符串")
    text = value.strip()
    if not text:
        raise FlowError(f"{label}不能为空")
    return text


def _xy(value, label: str, base_size: tuple = DEFAULT_BASE_SIZE) -> list:
    """坐标点 [x, y]：数字、必须在基准画面内（含贴边，旧语义）。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise FlowError(f"{label}必须是 [x, y] 两个数")
    out = []
    for axis, v in zip(("x", "y"), value):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FlowError(f"{label}的{axis}必须是数字")
        if isinstance(v, float) and not v.is_integer():
            raise FlowError(f"{label}的{axis}必须是整数坐标")
        out.append(int(v))
    w, h = base_size
    if not (0 <= out[0] <= w and 0 <= out[1] <= h):
        raise FlowError(f"{label}越界：画面只有 {w}×{h}")
    return out


def _xyxy(value, label: str = "roi", base_size: tuple = DEFAULT_BASE_SIZE) -> list:
    """ROI xyxy 四元数组：0≤x1<x2≤基准宽、0≤y1<y2≤基准高。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise FlowError(f"{label}必须是 [x1, y1, x2, y2] 四个数")
    rect = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FlowError(f"{label}必须是数字坐标")
        if isinstance(v, float) and not v.is_integer():
            raise FlowError(f"{label}必须是整数坐标")
        rect.append(int(v))
    w, h = base_size
    if not (0 <= rect[0] < rect[2] <= w
            and 0 <= rect[1] < rect[3] <= h):
        raise FlowError(f"{label}不合法：要满足 0≤x1<x2≤{w}、"
                        f"0≤y1<y2≤{h}")
    return rect


def _optional_roi(params: dict, base_size: tuple = DEFAULT_BASE_SIZE,
                  official: bool = False, label: str = "roi") -> list | None:
    value = params.get("roi")
    if value in (None, ""):
        return None
    if _is_logical_ref(value):
        return _check_ref_name(value, label)
    _no_official_raw(official, value, label)
    return _xyxy(value, label, base_size=base_size)


def params_to_region(params: dict, pack=None) -> Region | None:
    """JSON 里的 xyxy ROI → Region；@逻辑名 经素材包解析；没有 ROI 返回 None（全屏）。"""
    roi = (params or {}).get("roi")
    if not roi:
        return None
    if _is_logical_ref(roi):
        if pack is None:
            raise FlowError(f"ROI 引用 {roi!r} 需要素材包，但本次执行没加载")
        roi = pack.resolve_roi(roi)
    return roi_4to4(roi[0], roi[1], roi[2], roi[3])


# ── 执行期 @引用解析 ──

def _resolve_ref(ctx: dict, value, method: str):
    """@逻辑名 经 ctx 里的素材包解析；缺引用/坏路径/越界抛 ValueError 子类。"""
    pack = (ctx or {}).get("pack")
    if pack is None:
        raise FlowError(f"步骤引用了 {value!r} 但没加载素材包（内部错误）")
    return getattr(pack, method)(value)


def _maybe_resolve(ctx: dict, value, method: str):
    """字符串以 @ 开头才走素材包解析，其余原样返回（旧语义，normalize 期已校验）。"""
    if _is_logical_ref(value):
        return _resolve_ref(ctx, value, method)
    return value


# ── 步骤注册表 ──
# 每步：{type, label, desc, category(认/点/结构), params(schema), run(agent, params, ctx)}
# run 是生成器：yield 日志消息，return (ok, info)。ctx 携带上一步 check 结果等运行时状态。

STEP_REGISTRY: dict[str, dict] = {}


def register_step(defn: dict):
    STEP_REGISTRY[defn["type"]] = defn


def _step(type_, label, desc, category, run, validate, params=None):
    register_step({
        "type": type_, "label": label, "desc": desc, "category": category,
        "params": params or [], "run": run, "validate": validate,
    })


def step_catalog() -> list[dict]:
    """步骤目录（前端渲染步骤选择器和参数表单用）。"""
    order = {"认": 0, "点": 1, "结构": 2}
    steps = [{
        "type": d["type"], "label": d["label"], "desc": d.get("desc", ""),
        "category": d["category"], "params": d.get("params") or [],
    } for d in STEP_REGISTRY.values()]
    steps.sort(key=lambda s: (order.get(s["category"], 9), s["type"]))
    return steps


# ── 「认」类 ──

def _validate_wait_landmark(params: dict, i: int, *,
                            base_size: tuple = DEFAULT_BASE_SIZE,
                            official: bool = False) -> dict:
    label = f"第 {i + 1} 步"
    template = _optional_template(params, f"{label}的模板路径", official)
    ocr_expected = _ocr_text(params, official=official)
    if not template and not ocr_expected:
        raise FlowError(f"{label}：等地标至少要给模板或要认的文字其一")
    out = {"template": template, "ocr_expected": ocr_expected,
           "roi": _optional_roi(params, base_size, official),
           "threshold": _threshold(params.get("threshold")),
           "timeout_s": _int(params.get("timeout_s"), "timeout_s", 30, 1, 600),
           "stable_hits": _int(params.get("stable_hits"), "stable_hits", 2, 1, 10)}
    return {k: v for k, v in out.items() if v is not None or k in ("threshold",)}


def _run_wait_landmark(agent, params, ctx):
    """边点安全区边等地标出现，地标要连续 stable_hits 次都在才算就绪。

    语义照抄 navigator.wait_landmark_skipping（渐入式剧情会先露脸再被盖住，
    看一眼就动手会点进动画里卡死），但模板阈值按流程参数走——固定阈值的
    老封装表达不了这个，所以循环落在引擎里，注释指向出处。
    模板/OCR/ROI 支持 @逻辑名（双服素材包），执行前经 ctx["pack"] 解析。
    """
    template = _maybe_resolve(ctx, params.get("template"), "resolve_template")
    ocr_expected = _maybe_resolve(ctx, params.get("ocr_expected"), "resolve_ocr")
    roi = params_to_region(params, pack=(ctx or {}).get("pack"))
    threshold = params.get("threshold", 0.7)
    timeout_s = params.get("timeout_s", 30)
    stable_hits = params.get("stable_hits", 2)
    skip_pt = DEFAULT_SKIP_POINT
    skip_fn = getattr(agent, "_skip_point", None)
    if callable(skip_fn):
        try:
            skip_pt = skip_fn() or DEFAULT_SKIP_POINT
        except Exception:
            skip_pt = DEFAULT_SKIP_POINT

    found_desc = template or ocr_expected
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    hits = 0
    need = max(1, int(stable_hits))
    while time.monotonic() < deadline:
        agent.device.screenshot(force=True)
        found = False
        if template and agent.device.template_match(template, roi=roi,
                                                 threshold=threshold):
            found = True
        if not found and ocr_expected and agent.device.ocr(
                expected=ocr_expected, roi=roi):
            found = True
        if found:
            hits += 1
            if hits >= need:
                yield f"✓ 地标已就绪：{found_desc}"
                return True, None
            _sleep(0.8)
            continue
        hits = 0
        agent.device.click(Point(skip_pt[0], skip_pt[1]))
        _sleep(0.8)
    yield f"✗ 没等到地标：{found_desc}（等了 {timeout_s} 秒）"
    return False, None


_step("wait_landmark", "等地标",
      "边点安全区边等模板/文字出现，连续命中几次才算就绪。过场动画、迟到的入口都用这个等。",
      "认", _run_wait_landmark, _validate_wait_landmark,
      params=[{"key": "template", "type": "template", "label": "模板",
               "help": "相对 image/ 的路径，如 event/入口.png；与「要认的文字」至少给一个。"},
              {"key": "ocr_expected", "type": "text", "label": "要认的文字",
               "help": "OCR 认这串字（包含匹配）；与「模板」至少给一个。"},
              {"key": "roi", "type": "roi", "label": "识别区域（xyxy）",
               "help": "限定搜索范围，留空全屏。"},
              {"key": "threshold", "type": "number", "label": "模板阈值",
               "default": 0.7, "min": 0, "max": 1},
              {"key": "timeout_s", "type": "number", "label": "最多等几秒",
               "default": 30, "min": 1, "max": 600},
              {"key": "stable_hits", "type": "number", "label": "连续命中几次算数",
               "default": 2, "min": 1, "max": 10,
               "help": "防渐入式剧情「先露脸再被盖住」：看一眼就动手会点进动画卡死。"}])


def _validate_check(params: dict, i: int, *,
                    base_size: tuple = DEFAULT_BASE_SIZE,
                    official: bool = False) -> dict:
    template = _optional_template(params, f"第 {i + 1} 步的模板路径", official)
    ocr_expected = _ocr_text(params, official=official)
    if not template and not ocr_expected:
        raise FlowError(f"第 {i + 1} 步：只认不点至少要给模板或要认的文字其一")
    out = {"template": template, "ocr_expected": ocr_expected,
           "roi": _optional_roi(params, base_size, official),
           "threshold": _threshold(params.get("threshold"))}
    return {k: v for k, v in out.items() if v is not None or k == "threshold"}


def _run_check(agent, params, ctx):
    """只认不点，结果存进 ctx 供后面的 jump_if 用。"""
    template = _maybe_resolve(ctx, params.get("template"), "resolve_template")
    ocr_expected = _maybe_resolve(ctx, params.get("ocr_expected"), "resolve_ocr")
    roi = params_to_region(params, pack=(ctx or {}).get("pack"))
    threshold = params.get("threshold", 0.7)
    agent.device.screenshot(force=True)
    hit = False
    detail = ""
    if template:
        pt = agent.device.template_match(template, roi=roi, threshold=threshold)
        hit = pt is not None
        detail = (f"模板 {template} 命中 @({pt.x}, {pt.y})" if pt
                  else f"模板 {template} 未命中")
    if not hit and ocr_expected:
        pt = agent.device.ocr(expected=ocr_expected, roi=roi)
        hit = pt is not None
        detail = (f"认到「{ocr_expected}」@({pt.x}, {pt.y})" if pt
                  else f"没认到「{ocr_expected}」")
    ctx["check"] = {"hit": hit, "point": pt.to_list() if pt else None}
    yield f"{'✓' if hit else '·'} 判定：{detail}"
    return True, None


_step("check", "认一下（不点）",
      "只认不点：模板/文字在不在，结果给后面的「条件跳转」用。自己不翻车，认不到也是正常结论。",
      "认", _run_check, _validate_check,
      params=[{"key": "template", "type": "template", "label": "模板",
               "help": "相对 image/ 的路径；与「要认的文字」至少给一个。"},
              {"key": "ocr_expected", "type": "text", "label": "要认的文字",
               "help": "OCR 认这串字（包含匹配）；与「模板」至少给一个。"},
              {"key": "roi", "type": "roi", "label": "识别区域（xyxy）",
               "help": "限定搜索范围，留空全屏。"},
              {"key": "threshold", "type": "number", "label": "模板阈值",
               "default": 0.7, "min": 0, "max": 1}])


def _run_click_hit(agent, params, ctx):
    """点击上一步「认一下」认到的位置——认+点分两步的玩法用它，省一次重复识别。"""
    last = ctx.get("check")
    point = (last or {}).get("point")
    if not (last and last.get("hit") and point):
        yield "✗ 上一步「认一下」没有认到可点的位置，没点"
        return False, None
    pt = Point(int(point[0]), int(point[1]))
    if agent.device.click(pt):
        yield f"✓ 已点击上一步认到的位置 ({pt.x}, {pt.y})"
        return True, None
    yield f"✗ 点击 ({pt.x}, {pt.y}) 失败，没点成"
    return False, None


_step("click_hit", "点刚认到的",
      "点击上一步「认一下」认到的位置。先认再分支、最后才点的玩法用它，不多认一次。",
      "点", _run_click_hit, lambda params, i, **_: {})


# ── 「点」类 ──

def _validate_click_point(params: dict, i: int, *,
                          base_size: tuple = DEFAULT_BASE_SIZE,
                          official: bool = False) -> dict:
    label = f"第 {i + 1} 步"
    point = params.get("point")
    if point not in (None, ""):
        # @逻辑名：坐标收在素材包里，执行前解析（官方流程的唯一合法姿势）
        if not _is_logical_ref(point):
            raise FlowError(f"{label}：point 只准写 @逻辑名（素材包逻辑点），收到 {point!r}")
        if "x" in params or "y" in params:
            raise FlowError(f"{label}：point 和 x/y 二选一，别同时给")
        return {"point": _check_ref_name(point, f"{label}的 point")}
    if official:
        raise FlowError(f"{label}：官方流程禁用裸坐标盲点，"
                        f"请给 point=\"@逻辑名\"（坐标收进素材包）")
    if "x" not in params or "y" not in params:
        raise FlowError(f"{label}：盲点坐标要给 x 和 y（或 point=\"@逻辑名\"）")
    return {"x": _xy([params["x"], params["y"]], "点击坐标",
                     base_size=base_size)[0],
            "y": _xy([params["x"], params["y"]], "点击坐标",
                     base_size=base_size)[1]}


def _run_click_point(agent, params, ctx):
    if params.get("point"):
        x, y = _resolve_ref(ctx, params["point"], "resolve_point")
        desc = f"{params['point']} ({x}, {y})"
    else:
        x, y = params["x"], params["y"]
        desc = f"({x}, {y})"
    if agent.device.click(Point(x, y)):
        yield f"✓ 已点击 {desc}"
        return True, None
    yield f"✗ 点击 {desc} 失败，没点成"
    return False, None


_step("click_point", "盲点坐标",
      "闭眼点一个固定坐标。只有位置永不变的东西才配用（比如已验证的兜底点）；官方流程只准用 point=\"@逻辑名\"。",
      "点", _run_click_point, _validate_click_point,
      params=[{"key": "point", "type": "text", "label": "逻辑点（@名）",
               "help": "素材包里的逻辑点名，如 @signin/tab；填了它就优先于 x/y。"},
              {"key": "x", "type": "number", "label": "x 坐标",
               "min": 0, "max": FRAME_W},
              {"key": "y", "type": "number", "label": "y 坐标",
               "min": 0, "max": FRAME_H}])


def _validate_click_template(params: dict, i: int, *,
                             base_size: tuple = DEFAULT_BASE_SIZE,
                             official: bool = False) -> dict:
    if not params.get("template"):
        raise FlowError(f"第 {i + 1} 步：认模板点击必须给模板")
    out = {"template": _optional_template(params, f"第 {i + 1} 步的模板路径",
                                          official),
           "roi": _optional_roi(params, base_size, official),
           "threshold": _threshold(params.get("threshold"))}
    return {k: v for k, v in out.items() if v is not None or k == "threshold"}


def _click_recognized(agent, params, ctx, mode: str):
    roi = params_to_region(params, pack=(ctx or {}).get("pack"))
    if mode == "template":
        template = _maybe_resolve(ctx, params.get("template"), "resolve_template")
        pt = agent.device.template_match(template, roi=roi,
                                      threshold=params.get("threshold", 0.7))
        desc = f"模板 {template}"
    else:
        ocr_expected = _maybe_resolve(ctx, params.get("ocr_expected"), "resolve_ocr")
        pt = agent.device.ocr(expected=ocr_expected, roi=roi,
                           match_mode=params.get("match_mode", "contains"))
        desc = f"「{ocr_expected}」"
    if not pt:
        yield f"✗ 没找到可点的{desc}，没点"
        return False, None
    if not agent.device.click(pt):
        yield f"✗ 认到{desc} @({pt.x}, {pt.y}) 但点击失败"
        return False, None
    yield f"✓ 认到并点击{desc} @({pt.x}, {pt.y})"
    return True, None


def _run_click_template(agent, params, ctx):
    return (yield from _click_recognized(agent, params, ctx, "template"))


_step("click_template", "认到就点（模板）",
      "先模板匹配，认到了点中心，认不到不点（翻车）。按钮、入口都用这个。",
      "点", _run_click_template, _validate_click_template,
      params=[{"key": "template", "type": "template", "label": "模板",
               "help": "相对 image/ 的路径，先在模板工坊验分采纳再来这选。"},
              {"key": "roi", "type": "roi", "label": "识别区域（xyxy）",
               "help": "限定搜索范围，留空全屏。"},
              {"key": "threshold", "type": "number", "label": "模板阈值",
               "default": 0.7, "min": 0, "max": 1}])


def _validate_click_ocr(params: dict, i: int, *, base_size: tuple = DEFAULT_BASE_SIZE,
                        official: bool = False) -> dict:
    text = _ocr_text(params, official=official)
    if not text:
        raise FlowError(f"第 {i + 1} 步：认字点击必须给要认的文字")
    match_mode = params.get("match_mode") or "contains"
    if match_mode not in ("contains", "exact"):
        raise FlowError(f"第 {i + 1} 步：match_mode 只准 contains/exact")
    out = {"ocr_expected": text, "match_mode": match_mode,
           "roi": _optional_roi(params, base_size, official)}
    return {k: v for k, v in out.items() if v is not None}


def _run_click_ocr(agent, params, ctx):
    return (yield from _click_recognized(agent, params, ctx, "ocr"))


_step("click_ocr", "认到就点（文字）",
      "OCR 认出这串字就点它，认不到不点（翻车）。字比模板稳的按钮用它。",
      "点", _run_click_ocr, _validate_click_ocr,
      params=[{"key": "ocr_expected", "type": "text", "label": "要认的文字"},
              {"key": "match_mode", "type": "select", "label": "匹配方式",
               "options": [["contains", "互相包含"], ["exact", "完全一致"]],
               "default": "contains"},
              {"key": "roi", "type": "roi", "label": "识别区域（xyxy）",
               "help": "限定搜索范围，留空全屏。"}])


def _validate_swipe(params: dict, i: int, *, base_size: tuple = DEFAULT_BASE_SIZE,
                    official: bool = False) -> dict:
    if official:
        raise FlowError(f"第 {i + 1} 步：官方流程禁用裸坐标滑动，"
                        f"滑动类操作请做成素材包逻辑点或内置积木")
    for key in ("x1", "y1", "x2", "y2"):
        if key not in params:
            raise FlowError(f"第 {i + 1} 步：滑动要给 {key}")
    return {"x1": _xy([params["x1"], params["y1"]], "滑动起点",
                      base_size=base_size)[0],
            "y1": _xy([params["x1"], params["y1"]], "滑动起点",
                      base_size=base_size)[1],
            "x2": _xy([params["x2"], params["y2"]], "滑动终点",
                      base_size=base_size)[0],
            "y2": _xy([params["x2"], params["y2"]], "滑动终点",
                      base_size=base_size)[1],
            "duration_ms": _int(params.get("duration_ms"), "duration_ms",
                                400, 50, 5000)}


def _run_swipe(agent, params, ctx):
    ok = agent.device.swipe(params["x1"], params["y1"], params["x2"], params["y2"],
                         duration_ms=params.get("duration_ms", 400))
    if ok:
        yield (f"✓ 已滑动 ({params['x1']}, {params['y1']}) → "
               f"({params['x2']}, {params['y2']})")
        return True, None
    yield "✗ 滑动失败"
    return False, None


_step("swipe", "滑动",
      "从起点滑到终点：翻页、拖动列表。",
      "点", _run_swipe, _validate_swipe,
      params=[{"key": "x1", "type": "number", "label": "起点 x", "min": 0, "max": FRAME_W},
              {"key": "y1", "type": "number", "label": "起点 y", "min": 0, "max": FRAME_H},
              {"key": "x2", "type": "number", "label": "终点 x", "min": 0, "max": FRAME_W},
              {"key": "y2", "type": "number", "label": "终点 y", "min": 0, "max": FRAME_H},
              {"key": "duration_ms", "type": "number", "label": "滑多久（毫秒）",
               "default": 400, "min": 50, "max": 5000}])


def _validate_skip_safe(params: dict, i: int, *,
                        base_size: tuple = DEFAULT_BASE_SIZE,
                        official: bool = False) -> dict:
    return {"times": _int(params.get("times"), "times", 3, 1, 30),
            "interval_s": _fnum(params.get("interval_s"), "interval_s",
                                0.8, 0.1, 10.0)}


def _run_skip_safe(agent, params, ctx):
    times = params.get("times", 3)
    agent.skip_safe(times=times, interval=params.get("interval_s", 0.8))
    yield f"✓ 已点安全区 {times} 下，把动画/对话往前推"
    return True, None


_step("skip_safe", "点安全区跳动画",
      "连点几下右下角安全区，跳对话和过场。复用导航层的安全点（配置 skip_tap 优先）。",
      "点", _run_skip_safe, _validate_skip_safe,
      params=[{"key": "times", "type": "number", "label": "点几下",
               "default": 3, "min": 1, "max": 30},
              {"key": "interval_s", "type": "number", "label": "间隔秒数",
               "default": 0.8, "min": 0.1, "max": 10}])


def _validate_sleep(params: dict, i: int, *,
                      base_size: tuple = DEFAULT_BASE_SIZE,
                      official: bool = False) -> dict:
    return {"seconds": _fnum(params.get("seconds"), "seconds", 1.0, 0.1, 120.0)}


def _run_sleep(agent, params, ctx):
    seconds = params.get("seconds", 1.0)
    _sleep(seconds)
    yield f"✓ 睡了 {seconds} 秒"
    return True, None


_step("sleep", "等一会儿",
      "固定睡几秒。能用等地标就不用它——死等不认画面，能少就少。",
      "点", _run_sleep, _validate_sleep,
      params=[{"key": "seconds", "type": "number", "label": "等几秒",
               "default": 1, "min": 0.1, "max": 120}])


def _validate_note(params: dict, i: int, *,
                   base_size: tuple = DEFAULT_BASE_SIZE,
                   official: bool = False) -> dict:
    text = params.get("text")
    if not isinstance(text, str) or not text.strip():
        raise FlowError(f"第 {i + 1} 步：插播一句话要给文案")
    text = text.strip()
    if len(text) > 120:
        raise FlowError(f"第 {i + 1} 步：插播文案最多 120 个字")
    return {"text": text}


def _run_note(agent, params, ctx):
    """只播报、不碰游戏——分支的人话说明靠它（播报表里也能留痕）。"""
    yield params["text"]
    return True, None


_step("note", "插播一句",
      "只播一句话，不碰游戏。走到哪一步了、这个分支是什么意思，写给人看的。",
      "结构", _run_note, _validate_note,
      params=[{"key": "text", "type": "text", "label": "要播报的话",
               "help": "进日志和成绩单，建议带 [流程名] 前缀，比如 [签到] 打开目录…"}])


# ── 「结构」类 ──

def _validate_navigate(params: dict, i: int, *, base_size: tuple = DEFAULT_BASE_SIZE,
                       official: bool = False) -> dict:
    target = params.get("target")
    if not isinstance(target, str) or not target.strip():
        raise FlowError(f"第 {i + 1} 步：导航要给目标界面名（如 本丸）")
    target = target.strip()
    if _is_logical_ref(target):
        target = _check_ref_name(target, f"第 {i + 1} 步的导航目标")
    else:
        _no_official_raw(official, target, f"第 {i + 1} 步的导航目标")
    return {"target": target}


def _run_navigate(agent, params, ctx):
    target = _maybe_resolve(ctx, params["target"], "resolve_navigation")
    if not isinstance(target, str) or not target.strip():
        yield f"✗ 导航目标 {params['target']!r} 解析出来不是界面名，没去"
        return False, None
    for msg in agent.navigate_to_stream(target):
        yield msg
    if getattr(agent, "current_location", None) == target:
        yield f"✓ 已到达 {target}"
        return True, None
    yield f"✗ 没能导航到 {target}"
    return False, None


_step("navigate", "去界面",
      "走既有导航表到指定界面（含加载等待/弹窗救援，白捡的）。目标名要导航表里有的。",
      "结构", _run_navigate, _validate_navigate,
      params=[{"key": "target", "type": "text", "label": "目标界面",
               "help": "如 本丸 / 出阵 / 远征，与配置 navigation 表一致。"}])


def _validate_jump_if(params: dict, i: int, *,
                      base_size: tuple = DEFAULT_BASE_SIZE,
                      official: bool = False) -> dict:
    when = params.get("when") or "hit"
    if when not in ("hit", "miss"):
        raise FlowError(f"第 {i + 1} 步：跳转条件只准 hit/miss，收到 {when!r}")
    target = params.get("target")
    if not isinstance(target, str) or not target.strip():
        raise FlowError(f"第 {i + 1} 步：条件跳转要给目标步骤 id")
    return {"when": when, "target": target.strip()}


def _run_jump_if(agent, params, ctx):
    """线性卡片流里唯一的分支：吃上一步 check 的结果，成立/不成立跳转。"""
    last = ctx.get("check")
    if last is None:
        yield "✗ 条件跳转前面没有「认一下」的结果可参考，停止"
        return False, None
    when = params["when"]
    hit = bool(last.get("hit"))
    take = (when == "hit" and hit) or (when == "miss" and not hit)
    if take:
        yield f"✓ 条件成立（认{('到' if hit else '不到')}，按 {when} 跳），跳到步骤 {params['target']}"
        return True, {"jump": params["target"]}
    yield f"✓ 条件不成立（认{('到' if hit else '不到')}，按 {when} 不跳），继续往下"
    return True, {"jump": None}


_step("jump_if", "条件跳转",
      "上一步「认一下」命中/没命中时跳到指定步骤，是线性流程里唯一的分支手段。跳转目标必须存在。",
      "结构", _run_jump_if, _validate_jump_if,
      params=[{"key": "when", "type": "select", "label": "什么时候跳",
               "options": [["hit", "认到了跳"], ["miss", "认不到跳"]],
               "default": "hit"},
              {"key": "target", "type": "step", "label": "跳到哪个步骤",
               "help": "填流程里某一步的 id，保存时会校验存在。"}])


# ── 内置积木（builtin 步骤的目录，写死、不可拆）──
# 安全红线：涉及出阵只允许 safe_depart 这块积木（复用 _safe_depart_stream +
# _confirm_departure 整链），流程里拿裸 click 手搓出阵确认链 = 违规。

BUILTIN_REGISTRY: dict[str, dict] = {}


def register_builtin(defn: dict):
    BUILTIN_REGISTRY[defn["name"]] = defn


def builtin_catalog() -> list[dict]:
    builtins = [{
        "name": d["name"], "label": d["label"], "desc": d.get("desc", ""),
        "params": d.get("params") or [],
    } for d in BUILTIN_REGISTRY.values()]
    builtins.sort(key=lambda b: b["name"])
    return builtins


def _builtin_safe_depart(agent, params, ctx):
    """安全出阵整链：选队验证→伤势检查→刀装处理→重伤拦截→二次确认。

    cfg 取游戏配置里指定活动节（depart_button/confirm_ui/confirm_button/
    injury_* 那套，与各玩法同款）——新活动先把这节配进 touken_config.json。
    前置：流程要先把游戏带到该玩法的部队选择页（点入口是流程自己的活）。
    """
    tag = ctx.get("flow_name", "[流程]")
    activity = params.get("activity") or ""
    team_no = int(params.get("team_no", 1))
    repair_threshold = params.get("repair_threshold", "light")
    cfg = (getattr(agent, "config", None) or {}).get(activity)
    if not isinstance(cfg, dict) or not cfg:
        yield f"{tag} ✗ 配置里没有「{activity}」这一节出阵配置，停止"
        return False
    ok, _saved = yield from agent._safe_depart_stream(
        cfg, team_no, tag, repair_threshold=repair_threshold)
    if not ok:
        return False
    if not agent._confirm_departure(cfg):
        yield f"{tag} ✗ 没看到出阵二次确认，停止点击"
        return False
    yield f"{tag} ✓ 出阵二次确认已点，队伍出发"
    return True


def _validate_safe_depart(params: dict, i: int, *,
                          base_size: tuple = DEFAULT_BASE_SIZE,
                          official: bool = False) -> dict:
    activity = params.get("activity")
    if not isinstance(activity, str) or not activity.strip():
        raise FlowError(f"第 {i + 1} 步：安全出阵要给出阵配置节名（游戏配置里的活动节）")
    threshold = params.get("repair_threshold") or "light"
    if threshold not in ("light", "medium", "heavy"):
        raise FlowError(f"第 {i + 1} 步：伤势停止条件只准 light/medium/heavy")
    return {"activity": activity.strip(),
            "team_no": _int(params.get("team_no"), "team_no", 1, 1, 5),
            "repair_threshold": threshold}


register_builtin({
    "name": "safe_depart", "label": "安全出阵",
    "desc": "选队→伤势检查→刀装处理→重伤拦截→二次确认，整链不可拆。前置：流程已把游戏带到该玩法的部队选择页。",
    "params": [{"key": "activity", "type": "text", "label": "出阵配置节名",
                "help": "游戏配置 touken_config.json 里该玩法的节（含 depart_button/confirm_ui/injury_* 那套），如 raid、hanafuda。"},
               {"key": "team_no", "type": "select", "label": "出阵部队",
                "options": [["1", "部队一"], ["2", "部队二"], ["3", "部队三"],
                            ["4", "部队四"], ["5", "部队五"]], "default": "1"},
               {"key": "repair_threshold", "type": "select", "label": "伤势停止条件",
                "options": [["light", "轻伤时停止"], ["medium", "中伤时停止"],
                            ["heavy", "重伤时停止"]], "default": "light"}],
    "validate": _validate_safe_depart,
    "run": _builtin_safe_depart,
})


def _builtin_popup_sweep(agent, params, ctx):
    tag = ctx.get("flow_name", "[流程]")
    if agent._popup_sweep(max_rounds=int(params.get("max_rounds", 10))):
        yield f"{tag} ✓ 弹窗已清扫，画面干净了"
        return True
    yield f"{tag} ✗ 弹窗扫地没落地，停止"
    return False


def _builtin_open_menu(agent, params, ctx):
    tag = ctx.get("flow_name", "[流程]")
    if agent._open_menu():
        yield f"{tag} ✓ 目录已展开"
        return True
    yield f"{tag} ✗ 目录没打开，停"
    return False


register_builtin({
    "name": "open_menu", "label": "开目录",
    "desc": "循环点目录按钮直到展开（含加载等待/弹窗救援），就是导航层的 _open_menu。进公告、编队这类菜单页面前用它。",
    "params": [],
    "validate": lambda params, i, **_: {},
    "run": _builtin_open_menu,
})


register_builtin({
    "name": "popup_sweep", "label": "弹窗扫地",
    "desc": "关公告/礼物弹窗、点穿归来结算屏，确认落地才收工。",
    "params": [{"key": "max_rounds", "type": "number", "label": "最多扫几轮",
                "default": 10, "min": 1, "max": 30}],
    "validate": lambda params, i, **_: {
        "max_rounds": _int(params.get("max_rounds"), "max_rounds", 10, 1, 30)},
    "run": _builtin_popup_sweep,
})


def _builtin_home(agent, params, ctx):
    tag = ctx.get("flow_name", "[流程]")
    for msg in agent.navigate_to_stream("本丸"):
        yield msg
    if getattr(agent, "current_location", None) != "本丸":
        yield f"{tag} ✗ 没能回本丸，停止"
        return False
    try:
        agent._popup_sweep(max_rounds=6)
    except Exception as exc:
        yield f"{tag} ⚠️ 归来结算屏清扫失败（不影响流程）: {exc}"
    yield f"{tag} ✓ 已回本丸，归来结算屏已点穿"
    return True


register_builtin({
    "name": "home", "label": "回本丸",
    "desc": "导航回本丸，顺手把远征/内番/修行的归来结算屏记账点穿，别让它们挡着下一步。",
    "params": [],
    "validate": lambda params, i, **_: {},
    "run": _builtin_home,
})


def _builtin_reset_location(agent, params, ctx):
    tag = ctx.get("flow_name", "[流程]")
    agent.current_location = None
    yield f"{tag} ✓ 已标记当前位置失效，接下来重新认路"
    return True


register_builtin({
    "name": "reset_location", "label": "位置作废",
    "desc": "标记「当前位置已失效」——比如关公告后其实已回本丸，不告诉导航层，它会以为目录还开着乱点。等价旧代码的 current_location = None。",
    "params": [],
    "validate": lambda params, i, **_: {},
    "run": _builtin_reset_location,
})


# ── 远征派遣积木（docs/dual-server-pipeline.md 阶段 2）──
# 流程单：navigate(@expedition/nav) → expedition_pick_map → expedition_safe_dispatch。
# 选图/派遣的具体活在宿主玩法层（agent._expedition_pick_map_stream /
# _expedition_safe_dispatch_stream），那里管配置门、地图元数据、重伤拦截、
# 游戏结果证明和记账；引擎只负责按契约传参（含 ctx["pack"]）和兜底语义。

def _validate_expedition_dispatch_params(params: dict, i: int, *,
                                         base_size: tuple = DEFAULT_BASE_SIZE,
                                         official: bool = False) -> dict:
    """远征两积木共用参数：时代 1-5、卡位 1-4、部队 1-5 整数收界；
    map_name 可空（留空按卡位走，名字由玩法层按地图元数据解析）。"""
    map_name = params.get("map_name")
    if map_name is not None:
        if not isinstance(map_name, str) or not map_name.strip():
            raise FlowError(f"第 {i + 1} 步：map_name 要给地图名字或留空")
        map_name = map_name.strip()
    return {"era": _int(params.get("era"), "era", 1, 1, 5),
            "map_slot": (None if params.get("map_slot") is None
                         else _int(params.get("map_slot"), "map_slot", 1, 1, 4)),
            "map_name": map_name,
            "team_no": _int(params.get("team_no"), "team_no", 1, 1, 5)}


def _builtin_expedition_pick_map(agent, params, ctx):
    """选时代卡 → 点小图（卡位或按名字 OCR）→ 全屏 OCR 对图名，对不上重选再停。"""
    tag = ctx.get("flow_name", "[流程]")
    ok = yield from agent._expedition_pick_map_stream(
        era=params.get("era", 1),
        map_slot=params.get("map_slot"),
        map_name=params.get("map_name"),
        pack=(ctx or {}).get("pack"))
    if not ok:
        yield f"{tag} ✗ 选远征图没成，绝不继续派遣，停"
        return False
    yield f"{tag} ✓ 远征图已选定"
    return True


def _builtin_expedition_safe_dispatch(agent, params, ctx):
    """部队选择 → 重伤拦截 → 远征开始 → 二次确认 → 游戏结果证明 → 记账。"""
    tag = ctx.get("flow_name", "[流程]")
    ok = yield from agent._expedition_safe_dispatch_stream(
        team_no=params.get("team_no", 1),
        era=params.get("era", 1),
        map_slot=params.get("map_slot"),
        map_name=params.get("map_name"),
        pack=(ctx or {}).get("pack"))
    if not ok:
        yield f"{tag} ✗ 安全派遣没成，停"
        return False
    return True


_EXPEDITION_BUILTIN_PARAMS = [
    {"key": "era", "type": "select", "label": "时代",
     "options": [["1", "时代一"], ["2", "时代二"], ["3", "时代三"],
                 ["4", "时代四"], ["5", "时代五"]], "default": "1"},
    {"key": "map_slot", "type": "select", "label": "小图卡位",
     "options": [["1", "卡位一"], ["2", "卡位二"], ["3", "卡位三"],
                 ["4", "卡位四"]], "default": "1",
     "help": "小图卡一行四张；选了卡位就按坐标点， map_name 留空即可。"},
    {"key": "map_name", "type": "text", "label": "小图名字",
     "help": "OCR 点名字选图（包含匹配，不用写全）；给了 map_slot 就留空。"},
    {"key": "team_no", "type": "select", "label": "派遣部队",
     "options": [["1", "部队一"], ["2", "部队二"], ["3", "部队三"],
                 ["4", "部队四"], ["5", "部队五"]], "default": "1"},
]

register_builtin({
    "name": "expedition_pick_map", "label": "远征选图",
    "desc": "选时代卡→点小图卡位或按名字 OCR 点图→全屏 OCR 对图名，对不上重选一次再停。"
            "前置：流程已把游戏带到远征页。具体识别经素材包 @expedition/* 解析。",
    "params": _EXPEDITION_BUILTIN_PARAMS,
    "validate": _validate_expedition_dispatch_params,
    "run": _builtin_expedition_pick_map,
})

register_builtin({
    "name": "expedition_safe_dispatch", "label": "远征安全派遣",
    "desc": "开部队选择→选部队→重伤拦截（数据优先、重伤章兜底）→点远征开始→二次确认→"
            "游戏结果证明→记账。涉及派遣只准用这块，不许裸点手搓。",
    "params": _EXPEDITION_BUILTIN_PARAMS,
    "validate": _validate_expedition_dispatch_params,
    "run": _builtin_expedition_safe_dispatch,
})


def _validate_builtin(params: dict, i: int, *, base_size: tuple = DEFAULT_BASE_SIZE,
                      official: bool = False) -> dict:
    name = params.get("name")
    if name not in BUILTIN_REGISTRY:
        raise FlowError(f"第 {i + 1} 步：未知内置积木 {name!r}，只准用目录里写死的")
    rest = {k: v for k, v in params.items() if k != "name"}
    return {"name": name,
            **BUILTIN_REGISTRY[name]["validate"](rest, i, base_size=base_size,
                                                 official=official)}


def _run_builtin(agent, params, ctx):
    ok = yield from BUILTIN_REGISTRY[params["name"]]["run"](agent, params, ctx)
    return bool(ok), None


_step("builtin", "内置积木",
      "调写死的内置积木（安全出阵/弹窗扫地/回本丸）。涉及出阵只准用这块，不许裸点手搓。",
      "结构", _run_builtin, _validate_builtin,
      params=[{"key": "name", "type": "select", "label": "积木",
               "options": [[b["name"], b["label"]] for b in builtin_catalog()],
               "default": "home"},
              *[{"key": f["key"], "type": f["type"], "label": f["label"],
                 **{k: v for k, v in f.items() if k not in ("key", "type", "label")},
                 "visibleWhen": {"key": "name", "is": b["name"]}}
                for b in builtin_catalog() for f in b["params"]]])


# ── 校验（normalize）──

def _normalize_step(step, i: int, require_id: bool = True,
                    base_size: tuple = DEFAULT_BASE_SIZE,
                    official: bool = False) -> dict:
    """校验并规范化一步；非法抛 FlowError。jump 目标存在性由 normalize_flow 统一查。

    on_fail 缺省是 "recover"（引擎级翻车收拾：回本丸→跳过这步继续）；
    旧数据里显式写的 stop/continue/retry 原样保留（兼容红线）。
    """
    if not isinstance(step, dict):
        raise FlowError(f"第 {i + 1} 步不是对象")
    type_ = step.get("type")
    if type_ not in STEP_REGISTRY:
        raise FlowError(f"第 {i + 1} 步：不支持的步骤类型 {type_!r}")
    on_fail = step.get("on_fail") or "recover"
    if on_fail not in VALID_ON_FAIL:
        raise FlowError(f"第 {i + 1} 步：翻车策略只准 stop/continue/retry"
                        f"（缺省 recover 回本丸收拾），收到 {on_fail!r}")
    params = step.get("params") or {}
    if not isinstance(params, dict):
        raise FlowError(f"第 {i + 1} 步：参数必须是对象")
    out = {
        "type": type_,
        "params": STEP_REGISTRY[type_]["validate"](params, i,
                                                   base_size=base_size,
                                                   official=official),
        "on_fail": on_fail,
        "retry_times": _int(step.get("retry_times"), "retry_times", 2,
                            RETRY_TIMES_MIN, RETRY_TIMES_MAX),
        "retry_interval_s": _fnum(step.get("retry_interval_s"), "retry_interval_s",
                                  1.0, RETRY_INTERVAL_MIN, RETRY_INTERVAL_MAX),
    }
    sid = step.get("id")
    if require_id:
        if not isinstance(sid, str) or not sid.strip():
            raise FlowError(f"第 {i + 1} 步：缺步骤 id")
        sid = sid.strip()
        if len(sid) > 20:
            raise FlowError(f"第 {i + 1} 步：步骤 id 最多 20 个字符")
        out["id"] = sid
    elif isinstance(sid, str) and sid.strip():
        out["id"] = sid.strip()
    label = step.get("label")
    if label is not None:
        if not isinstance(label, str) or len(label) > 30:
            raise FlowError(f"第 {i + 1} 步：名字最多 30 个字")
        out["label"] = label.strip()
    return out


def normalize_flow(flow, base_size: tuple | None = None) -> dict:
    """校验并规范化一条流程；非法抛 FlowError（写入存储前必须过这关）。

    base_size 是坐标/ROI 校验用的基准分辨率，缺省 1280×720（国服旧语义）；
    run_flow 会传 agent.device.base_size（日服基准随设备标定走）。
    官方流程（official=True）只准写 @逻辑名，原始模板/文字/ROI/坐标一律拒。
    """
    if not isinstance(flow, dict):
        raise FlowError("流程必须是对象")
    name = str(flow.get("name") or "").strip()
    if not name:
        raise FlowError("流程名字不能为空")
    if len(name) > 30:
        raise FlowError("流程名字最多 30 个字")
    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise FlowError("流程至少要有一步")
    if len(steps) > MAX_STEPS:
        raise FlowError(f"流程最多 {MAX_STEPS} 步")
    size = tuple(base_size) if base_size else DEFAULT_BASE_SIZE
    official = flow.get("official") is True
    out_steps = [_normalize_step(step, i, base_size=size, official=official)
                 for i, step in enumerate(steps)]
    ids = set()
    for i, step in enumerate(out_steps):
        if step["id"] in ids:
            raise FlowError(f"第 {i + 1} 步：步骤 id {step['id']!r} 重复了")
        ids.add(step["id"])
    for i, step in enumerate(out_steps):
        if step["type"] == "jump_if" and step["params"]["target"] not in ids:
            raise FlowError(f"第 {i + 1} 步：跳转目标 {step['params']['target']!r} "
                            "不是流程里的步骤 id")
    return {"id": flow.get("id"), "name": name, "steps": out_steps,
            "official": flow.get("official") is True}


def normalize_test_step(step) -> dict:
    """单步试跑用的轻量校验：只查类型和参数，不查 id/on_fail/跳转目标。"""
    if not isinstance(step, dict):
        raise FlowError("步骤必须是对象")
    return _normalize_step(step, 0, require_id=False)


# ── 流程存取（STATUS_DIR/custom_flows.json）──

def _flows_path() -> Path:
    return STATUS_DIR / "custom_flows.json"


def load_flows() -> list[dict]:
    """读流程；坏文件备份后重置为空，绝不让面板崩。"""
    path = _flows_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            bad = path.with_name(path.name + ".bad-"
                                 + time.strftime("%Y%m%d-%H%M%S"))
            path.replace(bad)
        except OSError:
            pass
        return []
    if not isinstance(data, dict) or not isinstance(data.get("flows"), list):
        return []
    return [f for f in data["flows"] if isinstance(f, dict)]


def save_flows(flows: list[dict]):
    path = _flows_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 保存前保留旧结构，替换失败时原文件仍在（与 workflow.save_presets 同款）
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps({"flows": flows}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    os.replace(temp, path)


def find_flow(flow_id) -> dict | None:
    """按 id 找流程：官方优先（官方 id 是收编时定死的，私货撞号也算官方的）。"""
    for flow in load_official_flows():
        if flow.get("id") == flow_id:
            return flow
    for flow in load_flows():
        if flow.get("id") == flow_id:
            return flow
    return None


def create_flow(body: dict) -> dict:
    if not isinstance(body, dict):
        raise FlowError("流程必须是对象")
    preset = normalize_flow({"name": body.get("name"), "steps": body.get("steps")})
    flow = {"id": uuid.uuid4().hex[:8], "name": preset["name"],
            "steps": preset["steps"]}
    flows = load_flows()
    flows.append(flow)
    save_flows(flows)
    return flow


def update_flow(flow_id: str, body: dict) -> dict | None:
    """改流程；id 以路径为准。返回更新后的流程，找不到返回 None。"""
    if not isinstance(body, dict):
        raise FlowError("流程必须是对象")
    flows = load_flows()
    for i, existing in enumerate(flows):
        if existing.get("id") != flow_id:
            continue
        preset = normalize_flow({
            "name": body.get("name") if body.get("name") is not None
            else existing.get("name"),
            "steps": body.get("steps") if body.get("steps") is not None
            else existing.get("steps"),
        })
        flows[i] = {"id": flow_id, "name": preset["name"], "steps": preset["steps"]}
        save_flows(flows)
        return flows[i]
    return None


def delete_flow(flow_id: str) -> bool:
    flows = load_flows()
    remaining = [f for f in flows if f.get("id") != flow_id]
    if len(remaining) == len(flows):
        return False
    save_flows(remaining)
    return True


def duplicate_flow(flow_id: str) -> dict | None:
    """复制流程——拼新活动的主要姿势是拿旧流程改改。"""
    flows = load_flows()
    for flow in flows:
        if flow.get("id") != flow_id:
            continue
        cloned = copy.deepcopy(flow)
        cloned["id"] = uuid.uuid4().hex[:8]
        cloned["name"] = ((cloned.get("name") or "流程")[:27] + " 副本")
        flows.append(cloned)
        save_flows(flows)
        return cloned
    return None


# ── 官方流程（resource/base/flows/*.json，随包发布，只读）──
# 老大在流程工坊里能直接看到现有玩法怎么拼的；官方流程是展品，
# 不许改，但一键复制成私货就能改（copy_flow 对两者都开放）。

def official_flows_dir() -> Path:
    return RESOURCE_DIR / "flows"


def load_official_flows() -> list[dict]:
    """读官方流程目录；单个文件坏了/校验不过就跳过，绝不影响面板。"""
    folder = official_flows_dir()
    if not folder.is_dir():
        return []
    flows = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            flow = normalize_flow(data)
        except (OSError, ValueError, FlowError):
            continue
        flow["official"] = True
        flows.append(flow)
    return flows


def list_flows() -> list[dict]:
    """官方 + 私货合并列表：官方在前；id 撞车时官方优先，私货让位。"""
    official = load_official_flows()
    official_ids = {flow["id"] for flow in official}
    customs = [flow for flow in load_flows()
               if flow.get("id") not in official_ids]
    return official + customs


def flow_conflicts() -> list[str]:
    """私货与官方撞 id 的告警文案（列表接口顺带带给前端，老大看得见）。"""
    official_ids = {flow["id"] for flow in load_official_flows()}
    return [f"你的流程「{flow.get('name') or flow.get('id')}」和官方流程撞了编号，"
            "列表里让位给官方了，复制一条换个编号吧"
            for flow in load_flows() if flow.get("id") in official_ids]


def copy_flow(flow_id: str) -> dict | None:
    """官方 → 复制成私货（名字加「副本」，官方标记剥掉）；对私货等价 duplicate。"""
    flow = find_flow(flow_id)
    if flow is None:
        return None
    cloned = copy.deepcopy(flow)
    cloned["id"] = uuid.uuid4().hex[:8]
    cloned["name"] = ((cloned.get("name") or "流程")[:27] + " 副本")
    cloned.pop("official", None)
    customs = load_flows()
    customs.append(cloned)
    save_flows(customs)
    return cloned


def is_official_flow(flow_id) -> bool:
    return any(flow.get("id") == flow_id for flow in load_official_flows())


# ── 执行 ──

# 翻车不可自动收拾的内置积木：
# - safe_depart：安全出阵链翻车 = 安全拦截，带病继续可能把重伤刀送出阵；
# - open_menu：目录都打不开，回本丸的收拾残局本身也不可信（导航依赖目录）；
# - expedition_pick_map / expedition_safe_dispatch：选图失败绝不能继续派遣，
#   派遣链翻车（重伤拦截/没确认出发）带病继续可能把队伍派错或假绿。
_UNRECOVERABLE_BUILTINS = ("safe_depart", "open_menu",
                           "expedition_pick_map", "expedition_safe_dispatch")


def _unrecoverable_step(step: dict) -> bool:
    """这步翻车后是否禁止引擎默认的「回本丸收拾残局」。"""
    return (step["type"] == "builtin"
            and step["params"].get("name") in _UNRECOVERABLE_BUILTINS)


def _run_step(defn: dict, agent, step: dict, ctx: dict):
    """执行一步：透传消息并判红，返回 (ok, info)。异常 = 翻车。"""
    failed_by_msg = False
    try:
        runner = defn["run"](agent, step["params"], ctx)
        while True:
            try:
                msg = next(runner)
            except StopIteration as stop:
                result = stop.value or (True, None)
                break
            yield msg
            text = str(msg)
            if _is_fail(text) or text.lstrip().startswith("✗"):
                failed_by_msg = True
        ok, info = result
    except Exception as exc:  # 步骤执行炸了就翻车，不许带病继续
        yield f"✗ 步骤执行异常: {exc}"
        return False, None
    return bool(ok) and not failed_by_msg, info


def run_flow(agent, flow, server: str = "cn"):
    """流式跑一条自定义流程。

    Args:
        agent: ToukenAgent（含 device/config/navigate_to_stream 那套）
        flow: {"name", "steps": [...]}，会先过 normalize_flow 校验
        server: 素材包名（cn/jp）：@逻辑名从这包解析，加载一次全程复用

    Yields:
        str: 执行状态消息（带 [流程名] 前缀；翻车消息 ✗ 开头且进翻车词表）
    """
    device = getattr(agent, "device", None)
    device_base = getattr(device, "base_size", None)
    plan = normalize_flow(flow, base_size=device_base)
    name = plan["name"]
    steps = plan["steps"]
    try:
        pack = load_server_pack(server)
    except ValueError as exc:
        yield f"[{name}] ✗ 素材包（{server}）加载翻车：{exc}，流程不跑"
        return
    if device_base is not None and tuple(device_base) != tuple(pack.base_size):
        yield (f"[{name}] ✗ 设备基准分辨率 {tuple(device_base)} 与 {server} "
               f"素材包基准 {tuple(pack.base_size)} 不一致，流程不跑")
        return
    index_of = {s["id"]: pos for pos, s in enumerate(steps)}
    ctx = {"check": None, "flow_name": f"[{name}]", "pack": pack,
           "server": server}
    report: list[tuple] = []
    skipped: list[dict] = []

    yield f"[{name}] ▶ 开跑「{name}」，共 {len(steps)} 步（素材包 {server}）"
    pc = 0
    executed = 0
    while pc < len(steps):
        step = steps[pc]
        defn = STEP_REGISTRY[step["type"]]
        label = step.get("label") or defn["label"]
        executed += 1
        if executed > MAX_EXEC_STEPS:
            yield (f"[{name}] ✗ 执行步数超过上限 {MAX_EXEC_STEPS}，"
                   "步骤间互相跳疑似死循环，停止")
            report.append((label, "✗ 执行步数超限"))
            skipped = steps[pc:]
            break

        max_tries = 1 + (step["retry_times"] if step["on_fail"] == "retry" else 0)
        tried = 0
        while True:
            yield f"[{name}] ▶ 第 {pc + 1}/{len(steps)} 步：{label}" \
                + (f"（翻车重试 {tried}/{step['retry_times']}）" if tried else "")
            ok, info = yield from _run_step(defn, agent, step, ctx)
            tried += 1
            if ok or tried >= max_tries:
                break
            yield (f"[{name}] 「{label}」翻车，{step['retry_interval_s']} 秒后再试"
                   f"（第 {tried}/{step['retry_times']} 次）")
            _sleep(step["retry_interval_s"])

        if ok:
            report.append((label, "✓"))
            if step["type"] == "jump_if" and info and info.get("jump"):
                pc = index_of[info["jump"]]
                continue
            pc += 1
            continue

        report.append((label, "✗"))
        on_fail = step["on_fail"]
        if on_fail == "continue":
            yield f"[{name}] 「{label}」翻车，按设定跳过继续"
            pc += 1
            continue
        if on_fail == "recover":
            if _unrecoverable_step(step):
                # 安全出阵/开目录/远征选图派遣翻车不可自动收拾：安全拦截不能带病继续
                skipped = steps[pc + 1:]
                yield (f"[{name}] ✗ 「{label}」翻车，这步是安全出阵/开目录/"
                       f"远征选图派遣，不可自动收拾，停止，剩余 {len(skipped)} 步不跑")
                break
            # 引擎级默认翻车收拾：回本丸 → 确认落地 → 跳过这步继续
            yield f"[{name}] 「{label}」翻车，按引擎默认回本丸收拾残局"
            try:
                for msg in agent.navigate_to_stream("本丸"):
                    yield msg
            except Exception as exc:
                # 收拾残局自己炸了也是翻车：记 ✗、剩余步骤标跳过，成绩单照出
                skipped = steps[pc + 1:]
                yield (f"[{name}] ✗ 「{label}」翻车，回本丸收拾残局时导航炸了"
                       f"（{exc}），停止，剩余 {len(skipped)} 步不跑")
                break
            if getattr(agent, "current_location", None) == "本丸":
                yield f"[{name}] ✓ 已回本丸，跳过「{label}」继续往下"
                pc += 1
                continue
            skipped = steps[pc + 1:]
            yield (f"[{name}] ✗ 「{label}」翻车，没能回本丸，"
                   f"停止，剩余 {len(skipped)} 步不跑")
            break
        # stop（显式）/ retry 用尽：翻车即停，剩下的不跑
        skipped = steps[pc + 1:]
        yield (f"[{name}] ✗ 「{label}」翻车，按设定停止，"
               f"剩余 {len(skipped)} 步不跑")
        break

    for step in skipped:
        skipped_label = step.get("label") or STEP_REGISTRY[step["type"]]["label"]
        report.append((skipped_label, _SKIPPED_STATUS))

    fails = [n for n, s in report if str(s).lstrip().startswith(("✗", "⚠"))]
    yield "========== 流程成绩单 =========="
    for step_name, status in report:
        yield f"  {step_name}: {status}"
    yield (f"[{name}] 全部跑完，全绿" if not fails
           else f"[{name}] 跑完了，但有翻车项：{'、'.join(fails)}")
