# -*- coding: utf-8 -*-
"""
流程工坊 —— 自定义流程（线性卡片流）的后端：流程 CRUD、步骤目录、
模板/ROI 挑选、单步试跑。

老大拼流程的调试闭环：模板先在模板工坊验分采纳 → 这里下拉挑模板、
引用模板工坊框好的 ROI → 单步试跑只认不点 → 整流交给 runner 排队跑。

开发版调试工具（_is_dev 门禁，账房模式禁用试跑），流程只存用户数据目录
STATUS_DIR/custom_flows.json，不进程序目录/发布包。

依赖方向：本模块**不许在函数体以外 import panel.server**
（server 接线路由时会反过来 import 本模块）。
"""

import asyncio
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from touken import flow_engine
from touken.flow_engine import FlowError
from touken.device_adapter import Region
from touken.runtime_paths import RESOURCE_DIR

from . import template_lab as _template_lab


def _is_dev() -> bool:
    return not getattr(sys, "frozen", False)


def _is_ledger() -> bool:
    """账房模式判定：lazy import，server 挂 router 时会反向 import 本模块。"""
    from .server import _ledger_mode
    return _ledger_mode()


def _ensure_available() -> None:
    if not _is_dev():
        raise HTTPException(503, "流程工坊仅开发版可用。")


def _ensure_capture_available() -> None:
    """单步试跑要真连游戏抓帧；账房模式没有游戏连接。"""
    if _is_ledger():
        raise HTTPException(503, "账房模式没有游戏连接，流程工坊不可用。")
    if not _is_dev():
        raise HTTPException(503, "流程工坊仅开发版可用。")


def _create_adapter():
    """adapter 工厂钩子：默认借面板同款构造；测试 patch 本函数注入假截图源。"""
    from .server import _CONFIG_PATH, _make_device
    return _make_device(_CONFIG_PATH)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON。")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象。")
    return body


def _image_dir() -> Path:
    return RESOURCE_DIR / "image"


def _run_probe(adapter, step: dict, defn: dict) -> dict:
    """单步试跑：「认」类真识别返回命中与分数；「点」类只预览将要点的位置，不真点。"""
    params = step["params"]
    roi = flow_engine.params_to_region(params)
    adapter.screenshot(force=True)

    if step["type"] in ("check", "wait_landmark"):
        result = {"kind": "recognize", "hit": False, "score": None,
                  "point": None, "texts": None}
        template = params.get("template")
        if template:
            score = adapter.template_match_score(template, roi=roi)
            point = adapter.template_match(
                template, roi=roi, threshold=params.get("threshold", 0.7))
            result["score"] = round(float(score), 3)
            result["hit"] = point is not None
            if point:
                result["point"] = point.to_list()
        ocr_expected = params.get("ocr_expected")
        if ocr_expected:
            texts = [text for text, _pt in adapter.ocr_all(
                roi or Region(0, 0, flow_engine.FRAME_W, flow_engine.FRAME_H))]
            point = adapter.ocr(expected=ocr_expected, roi=roi,
                                match_mode=params.get("match_mode", "contains"))
            result["texts"] = texts
            if point is not None:
                result["hit"] = True
                result["point"] = point.to_list()
        return result

    if step["type"] == "click_point":
        return {"kind": "preview", "action": "click",
                "point": [params["x"], params["y"]]}
    if step["type"] in ("click_template", "click_ocr"):
        if step["type"] == "click_template":
            point = adapter.template_match(
                params["template"], roi=roi,
                threshold=params.get("threshold", 0.7))
        else:
            point = adapter.ocr(expected=params["ocr_expected"], roi=roi,
                                match_mode=params.get("match_mode", "contains"))
        return {"kind": "preview", "action": "click",
                "hit": point is not None,
                "point": point.to_list() if point else None}
    if step["type"] == "swipe":
        return {"kind": "preview", "action": "swipe",
                "from": [params["x1"], params["y1"]],
                "to": [params["x2"], params["y2"]],
                "duration_ms": params.get("duration_ms", 400)}
    if step["type"] == "skip_safe":
        return {"kind": "preview", "action": "click",
                "point": list(flow_engine.DEFAULT_SKIP_POINT),
                "note": "安全区跳过点（游戏配置 skip_tap 优先，此处为全局默认）"}
    if step["type"] == "sleep":
        return {"kind": "preview", "action": "sleep",
                "seconds": params.get("seconds", 1.0)}
    raise HTTPException(400, f"步骤类型 {step['type']!r} 不支持单步试跑。")


def _test_step_sync(step) -> dict:
    if not isinstance(step, dict):
        raise HTTPException(400, "step 必须是对象。")
    normalized = flow_engine.normalize_test_step(step)
    defn = flow_engine.STEP_REGISTRY[normalized["type"]]
    if defn["category"] == "结构":
        raise HTTPException(400, "结构类步骤（导航/跳转/内置积木）不支持单步试跑。")
    try:
        adapter = _create_adapter()
        if not adapter.init():
            raise RuntimeError("设备初始化失败，检查 ADB 连接 / 模拟器是不是关了。")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"截图通道起不来：{exc}") from exc
    return _run_probe(adapter, normalized, defn)


def create_flow_lab_router() -> APIRouter:
    router = APIRouter(prefix="/api/flow-lab")

    @router.get("/flows")
    async def list_flows():
        _ensure_available()
        return {"flows": flow_engine.list_flows(),
                "warnings": flow_engine.flow_conflicts()}

    @router.post("/flows")
    async def create_flow(request: Request):
        _ensure_available()
        body = await _json_body(request)
        try:
            flow = flow_engine.create_flow(body)
        except FlowError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "flow": flow}

    @router.put("/flows/{flow_id}")
    async def update_flow(flow_id: str, request: Request):
        _ensure_available()
        if flow_engine.is_official_flow(flow_id):
            raise HTTPException(403, "官方流程是随包发布的样板，只读。复制一条成自己的流程就能改。")
        body = await _json_body(request)
        try:
            flow = flow_engine.update_flow(flow_id, body)
        except FlowError as exc:
            raise HTTPException(400, str(exc))
        if flow is None:
            raise HTTPException(404, "没有这个流程。")
        return {"ok": True, "flow": flow}

    @router.delete("/flows/{flow_id}")
    async def delete_flow(flow_id: str):
        _ensure_available()
        if flow_engine.is_official_flow(flow_id):
            raise HTTPException(403, "官方流程是随包发布的样板，只读。复制一条成自己的流程就能改。")
        if not flow_engine.delete_flow(flow_id):
            raise HTTPException(404, "没有这个流程。")
        return {"ok": True}

    @router.post("/flows/{flow_id}/copy")
    async def copy_flow(flow_id: str):
        """官方 → 复制成私货（流程工坊里改官方样板的唯一姿势）。"""
        _ensure_available()
        cloned = flow_engine.copy_flow(flow_id)
        if cloned is None:
            raise HTTPException(404, "没有这个流程。")
        return {"ok": True, "flow": cloned}

    @router.post("/flows/{flow_id}/duplicate")
    async def duplicate_flow(flow_id: str):
        _ensure_available()
        cloned = flow_engine.duplicate_flow(flow_id)
        if cloned is None:
            raise HTTPException(404, "没有这个流程。")
        return {"ok": True, "flow": cloned}

    @router.get("/steps")
    async def list_steps():
        """步骤目录 + 参数 schema（前端渲染步骤选择器和参数表单用）。"""
        _ensure_available()
        return {"steps": flow_engine.step_catalog(),
                "builtins": flow_engine.builtin_catalog()}

    @router.get("/templates")
    async def list_templates():
        """resource/base/image/ 下可用模板（相对 image/ 的路径，供下拉挑选）。"""
        _ensure_available()
        image_dir = _image_dir()
        templates = []
        if image_dir.is_dir():
            templates = sorted(
                path.relative_to(image_dir).as_posix()
                for path in image_dir.rglob("*.png"))
        return {"templates": templates}

    @router.get("/template-image")
    async def template_image(path: str):
        """模板预览图：只发 image/ 白名单内的 .png（挑模板时给老大看图）。"""
        _ensure_available()
        try:
            rel = flow_engine.clean_template_path(path)
        except FlowError as exc:
            raise HTTPException(400, str(exc))
        image_dir = _image_dir().resolve()
        full = (image_dir / rel).resolve()
        if not full.is_relative_to(image_dir) or not full.is_file():
            raise HTTPException(404, "没有这个模板。")
        return FileResponse(str(full), media_type="image/png")

    @router.get("/rois")
    async def list_rois():
        """模板工坊框好的 ROI 存档，流程里可直接引用。"""
        _ensure_available()
        rois = [{"name": name, **rect}
                for name, rect in _template_lab._load_rois().items()]
        rois.sort(key=lambda item: item["name"])
        return {"rois": rois}

    @router.post("/test-step")
    async def test_step(request: Request):
        _ensure_capture_available()
        body = await _json_body(request)
        try:
            return await asyncio.to_thread(_test_step_sync, body.get("step"))
        except FlowError as exc:
            raise HTTPException(400, str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, f"试跑失败：{exc}") from exc

    return router
