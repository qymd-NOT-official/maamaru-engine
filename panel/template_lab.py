# -*- coding: utf-8 -*-
"""
模板工坊 —— 截图采样 → 裁草稿 → 验证匹配 → 采纳进正式资源目录
         → ROI 存档 → 离线 OCR 试读

开发版调试工具：给做模板的人一条不用手搓 ADB 截图的活路。
会话帧/草稿只写用户数据目录 DEBUG_DIR/template_lab/，采纳时才落 RESOURCE_DIR/image/。

- 面板进程刻意不常驻游戏连接：adapter 在 capture 时才借 panel.server 的
  _make_device + _CONFIG_PATH 现造（lazy import，避开 server 挂 router 的循环依赖）。
- 帧与草稿内存里一律 BGR numpy、存盘一律 RGB PNG。cv2.imwrite 不认中文路径，
  存盘走 PIL（save_screenshot 同款），读盘走 np.fromfile + cv2.imdecode
  （match_badge_flowers 同款）。
- verify 刻意只做 1.0 尺度：模板和帧同源同分辨率，多尺度只会引入误报
  （match_badge_flowers 的 0.9~1.4 多尺度是给跨页面缩放的刀种徽章用的，这里不抄）。

依赖方向：本模块**不许在函数体以外 import panel.server**
（server 接线路由时会反过来 import 本模块）。
"""

import asyncio
import json
import re
import shutil
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from touken import roi_overrides
from touken.roi_registry import ROI_REGISTRY
from touken.runtime_paths import DEBUG_DIR, RESOURCE_DIR

_SESSION_ID_RE = re.compile(r"\d{8}-\d{6}")
_NAME_RE = re.compile(r"[A-Za-z0-9一-鿿._-]+")  # 一-鿿 即 \u4e00-\u9fff（基本汉字）
_COUNT_MIN, _COUNT_MAX = 1, 50
_INTERVAL_MIN, _INTERVAL_MAX = 100, 5000
_FRAME_W, _FRAME_H = 1280, 720  # 游戏画面固定尺寸（MuMu 1280×720），ROI 不许越界
_OCR_MAX_FRAMES = 100  # 一次 OCR 试读最多扫的帧总数，防手滑点成全库跑飞


def _sessions_dir() -> Path:
    return DEBUG_DIR / "template_lab" / "sessions"


def _drafts_dir() -> Path:
    return DEBUG_DIR / "template_lab" / "drafts"


def _rois_file() -> Path:
    return DEBUG_DIR / "template_lab" / "rois.json"


def _adopt_backup_dir() -> Path:
    return DEBUG_DIR / "template_lab" / "adopted-backup"


def _is_ledger() -> bool:
    """账房模式判定：lazy import，server 挂 router 时会反向 import 本模块。"""
    from .server import _ledger_mode
    return _ledger_mode()


def _is_dev() -> bool:
    return not getattr(sys, "frozen", False)


def _create_adapter():
    """adapter 工厂钩子：默认借面板同款构造；测试 patch 本函数注入假截图源。"""
    from .server import _CONFIG_PATH, _make_device
    return _make_device(_CONFIG_PATH)


def _validate_session_id(session) -> str:
    if not isinstance(session, str) or not _SESSION_ID_RE.fullmatch(session):
        raise HTTPException(400, "会话编号格式不正确。")
    return session


def _validate_name(name, label: str = "名称") -> str:
    """中英文、数字、-_ .，禁路径分隔符；允许顺手带 .png 后缀（会剥掉）。"""
    if not isinstance(name, str):
        raise HTTPException(400, f"{label}必须是字符串。")
    stem = name[:-4] if name.lower().endswith(".png") else name
    if not stem or stem in (".", "..") or not _NAME_RE.fullmatch(stem):
        raise HTTPException(400, f"{label}只允许中英文、数字和 - _ .，不能用路径分隔符。")
    if len(stem) > 80:
        raise HTTPException(400, f"{label}太长了（最多 80 个字符）。")
    return stem


def _validate_target(target) -> str:
    """采用目标名：允许「子目录/名称」形式（如 刀种/一花短刀），
    子目录必须是 image/ 下已存在的目录，防路径穿越。"""
    if not isinstance(target, str):
        raise HTTPException(400, "目标名必须是字符串。")
    parts = target.replace("\\", "/").split("/")
    if len(parts) > 2 or any(not p for p in parts):
        raise HTTPException(400, "目标名格式不正确，最多带一级子目录（如 刀种/一花短刀）。")
    stem = _validate_name(parts[-1], "目标名")
    if len(parts) == 1:
        return stem
    sub = parts[0]
    image_dir = (RESOURCE_DIR / "image").resolve()
    sub_dir = (image_dir / sub).resolve()
    if not _NAME_RE.fullmatch(sub) or not sub_dir.is_dir() \
            or not sub_dir.is_relative_to(image_dir):
        raise HTTPException(400, f"模板目录下没有「{sub}」这个子目录。")
    return f"{sub}/{stem}"


def _frame_name(idx: int) -> str:
    return f"frame_{idx:03d}.png"


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON。")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象。")
    return body


def _clamp_int(value, default: int, lo: int, hi: int, label: str) -> int:
    """JSON 里的整数参数：缺省给默认，越界收进范围，不是数字就 400。"""
    if value is None:
        return default
    if isinstance(value, bool):
        raise HTTPException(400, f"{label}必须是数字。")
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{label}必须是数字。")
    return max(lo, min(hi, num))


def _rect_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(400, f"裁剪参数 {label} 必须是数字。")
    if isinstance(value, float) and not value.is_integer():
        raise HTTPException(400, f"裁剪参数 {label} 必须是整数。")
    return int(value)


def _threshold(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(400, "threshold 必须是 0~1 的数字。")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise HTTPException(400, "threshold 必须在 0~1 之间。")
    return value


def _load_rois() -> dict:
    """ROI 存档：{name: {x, y, w, h, updated}}。缺文件/坏 JSON/字段烂都当空的，
    下次保存会整体重写，顺带修掉写坏的条目。"""
    try:
        data = json.loads(_rois_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    rois = {}
    for name, rect in data.items():
        if not isinstance(name, str) or not isinstance(rect, dict):
            continue
        try:
            rois[name] = {"x": int(rect["x"]), "y": int(rect["y"]),
                          "w": int(rect["w"]), "h": int(rect["h"]),
                          "updated": float(rect.get("updated") or 0.0)}
        except (KeyError, TypeError, ValueError):
            continue
    return rois


def _save_rois(rois: dict) -> None:
    path = _rois_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rois, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _validate_roi_rect(body: dict) -> tuple[int, int, int, int]:
    """ROI 矩形：整数坐标、宽高至少 2、不许超出帧画面（1280×720）。"""
    x = _rect_int(body.get("x"), "x")
    y = _rect_int(body.get("y"), "y")
    w = _rect_int(body.get("w"), "w")
    h = _rect_int(body.get("h"), "h")
    if x < 0 or y < 0:
        raise HTTPException(400, "ROI 起点不能是负数。")
    if w < 2 or h < 2:
        raise HTTPException(400, "ROI 宽高至少为 2。")
    if x + w > _FRAME_W or y + h > _FRAME_H:
        raise HTTPException(400, f"ROI 超出画面（画面 {_FRAME_W}×{_FRAME_H}）。")
    return x, y, w, h


def _int_rect_values(value, shape: str) -> list[int]:
    """矩形列表的四条整数化：与 _rect_int 同款语义（浮点要整数值、bool 不算）。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise HTTPException(400, f"rect 必须是 {shape} 四个数。")
    rect = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(400, f"rect 必须是 {shape} 四个数。")
        if isinstance(v, float) and not v.is_integer():
            raise HTTPException(400, "rect 必须是整数坐标。")
        rect.append(int(v))
    return rect


def _validate_xyxy(value) -> list[int]:
    """代码 ROI 的 xyxy 矩形：0≤x1<x2≤1280、0≤y1<y2≤720。"""
    x1, y1, x2, y2 = _int_rect_values(value, "[x1, y1, x2, y2]")
    if not (0 <= x1 < x2 <= _FRAME_W and 0 <= y1 < y2 <= _FRAME_H):
        raise HTTPException(400, "rect 不合法：要满足 "
                                 f"0≤x1<x2≤{_FRAME_W}、0≤y1<y2≤{_FRAME_H}。")
    return [x1, y1, x2, y2]


def _validate_xywh(value) -> list[int]:
    """ocr-test 直传模式的 xywh 矩形：与存档 ROI 同款边界（宽高至少 2）。"""
    x, y, w, h = _int_rect_values(value, "[x, y, w, h]")
    if x < 0 or y < 0 or w < 2 or h < 2:
        raise HTTPException(400, "rect 宽高至少为 2，起点不能是负数。")
    if x + w > _FRAME_W or y + h > _FRAME_H:
        raise HTTPException(400, f"rect 超出画面（画面 {_FRAME_W}×{_FRAME_H}）。")
    return [x, y, w, h]


def _registry_ids() -> set:
    """注册表 id 集合：code-rois 端点拿来判 404。"""
    return {entry["id"] for entry in ROI_REGISTRY}


def _save_bgr_png(image, path: Path) -> None:
    """BGR numpy → RGB PNG。cv2.imwrite 不认中文路径，走 PIL（save_screenshot 同款）。"""
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(image[:, :, ::-1]).save(path)


def _read_png(path: Path, *, color: bool = True):
    """PNG → numpy。color=True 强制 3 通道 BGR（验证匹配用），
    False 保留原通道（裁剪剥 alpha 用）。读盘走 fromfile+imdecode 认中文路径。"""
    import cv2
    import numpy as np
    raw = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(raw, cv2.IMREAD_COLOR if color else cv2.IMREAD_UNCHANGED)


def _strip_alpha(image):
    """强制 3 通道 BGR：剥 alpha、灰度转彩，草稿统一成无透明通道的 RGB PNG。"""
    import cv2
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def _png_size(path: Path) -> tuple[int, int]:
    """读 PNG IHDR 拿宽高，不解码像素（会话列表动辄上百张，全解码太慢）。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return (0, 0)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return (0, 0)
    return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def _scan_session(session_dir: Path) -> list[dict]:
    frames = []
    for path in session_dir.glob("frame_*.png"):
        match = re.fullmatch(r"frame_(\d+)\.png", path.name)
        if not match:
            continue
        width, height = _png_size(path)
        frames.append({"idx": int(match.group(1)), "name": path.name,
                       "width": width, "height": height,
                       "mtime": path.stat().st_mtime})
    frames.sort(key=lambda item: item["idx"])
    return frames


def _session_frame_path(session, idx) -> Path:
    if isinstance(idx, bool) or not isinstance(idx, int):
        raise HTTPException(400, "帧序号必须是整数。")
    _validate_session_id(session)
    path = (_sessions_dir() / session / _frame_name(idx)).resolve()
    if not path.is_relative_to(_sessions_dir().resolve()):
        raise HTTPException(400, "会话路径不合法。")
    return path


def _unique_draft_path(folder: Path, stem: str) -> Path:
    """重名自动 -2、-3 后缀，不覆盖已有草稿/备份。"""
    candidate = folder / f"{stem}.png"
    serial = 2
    while candidate.exists():
        candidate = folder / f"{stem}-{serial}.png"
        serial += 1
    return candidate


def _sleep(seconds: float) -> None:
    """抽出来只为测试能 patch 掉等待。"""
    time.sleep(seconds)


def _ensure_capture_available() -> None:
    if _is_ledger():
        raise HTTPException(503, "账房模式没有游戏连接，模板工坊不可用。")
    if not _is_dev():
        raise HTTPException(503, "模板工坊仅开发版可用。")


def _sanitize_memo(memo) -> str:
    """会话备注：去首尾空白、去掉换行和控制字符，最多 50 字。"""
    if not isinstance(memo, str):
        return ""
    cleaned = "".join(ch for ch in memo if ch.isprintable()).strip()
    return cleaned[:50]


def _session_meta_path(session_dir: Path) -> Path:
    return session_dir / "meta.json"


def _read_session_memo(session_dir: Path):
    try:
        meta = json.loads(_session_meta_path(session_dir).read_text(encoding="utf-8"))
        return meta.get("memo") or None
    except Exception:
        return None


def _write_session_memo(session_dir: Path, memo: str) -> None:
    _session_meta_path(session_dir).write_text(
        json.dumps({"memo": memo}, ensure_ascii=False), encoding="utf-8")


def _capture_sync(count: int, interval_ms: int, memo: str = "") -> dict:
    try:
        adapter = _create_adapter()
        if not adapter.init():
            raise RuntimeError("设备初始化失败，检查 ADB 连接 / 模拟器是不是关了。")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"截图通道起不来：{exc}") from exc

    session_id = time.strftime("%Y%m%d-%H%M%S")
    session_dir = _sessions_dir() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    frames, errors = [], []
    for idx in range(count):
        try:
            image = adapter.screenshot(force=True)
            if image is None:
                errors.append(f"第 {idx + 1} 帧截图失败（返回空）。")
            else:
                name = _frame_name(idx)
                _save_bgr_png(image, session_dir / name)
                frames.append({"idx": idx, "name": name,
                               "width": int(image.shape[1]),
                               "height": int(image.shape[0])})
        except Exception as exc:
            errors.append(f"第 {idx + 1} 帧存盘失败：{exc}")
        if idx < count - 1:
            _sleep(interval_ms / 1000)
    if memo:
        _write_session_memo(session_dir, memo)
    return {"session": session_id, "frames": frames, "errors": errors}


def _match(image, tpl) -> tuple[float, dict]:
    """单尺度模板匹配：刻意只跑 1.0 尺度——模板与帧同源同分辨率，
    多尺度只会引入误报（match_badge_flowers 的 0.9~1.4 是给跨页面缩放的徽章用的，别抄）。"""
    import cv2
    if tpl.shape[0] > image.shape[0] or tpl.shape[1] > image.shape[1]:
        return 0.0, {"x": 0, "y": 0}
    result = cv2.matchTemplate(image, tpl, cv2.TM_CCOEFF_NORMED)
    _min, max_score, _min_loc, max_loc = cv2.minMaxLoc(result)
    return float(max_score), {"x": int(max_loc[0]), "y": int(max_loc[1])}


def _verify_sync(draft_stem: str, sessions: list, threshold: float) -> dict:
    draft_path = _drafts_dir() / f"{draft_stem}.png"
    if not draft_path.is_file():
        raise HTTPException(404, "这张草稿不存在。")
    tpl = _read_png(draft_path, color=True)
    if tpl is None:
        raise HTTPException(400, "这张草稿读不出来，可能不是有效图片。")

    # 其他草稿提前加载，撞车检查逐帧复用
    others = []
    for path in sorted(_drafts_dir().glob("*.png")):
        if path.stem == draft_stem:
            continue
        other = _read_png(path, color=True)
        if other is not None:
            others.append((path.stem, other))

    results, confusion = [], []
    for session in sessions:
        _validate_session_id(session)
        session_dir = _sessions_dir() / session
        if not session_dir.is_dir():
            raise HTTPException(404, f"会话 {session} 不存在。")
        for frame in _scan_session(session_dir):
            image = _read_png(session_dir / frame["name"], color=True)
            if image is None:
                continue
            score, loc = _match(image, tpl)
            results.append({
                "session": session, "frame": frame["idx"],
                "score": round(score, 3), "loc": loc,
                "hit": score >= threshold,
            })
            for other_stem, other in others:
                other_score, _loc = _match(image, other)
                # 撞车：同帧上别的草稿匹配得更像（且过阈值），这帧就不能算这张草稿的证据
                if other_score >= threshold and other_score > score:
                    confusion.append({
                        "session": session, "frame": frame["idx"],
                        "other_draft": other_stem,
                        "other_score": round(other_score, 3),
                        "margin": round(other_score - score, 3),
                    })
    return {"draft": draft_stem, "threshold": threshold,
            "results": results, "confusion": confusion}


def _crop_sync(session, idx, x: int, y: int, w: int, h: int, stem: str) -> dict:
    frame_path = _session_frame_path(session, idx)
    if not frame_path.is_file():
        raise HTTPException(404, "这张会话帧不存在。")
    image = _read_png(frame_path, color=False)
    if image is None or image.size == 0:
        raise HTTPException(400, "这张会话帧读不出来，可能不是有效图片。")
    image = _strip_alpha(image)
    frame_h, frame_w = image.shape[:2]
    if x + w > frame_w or y + h > frame_h:
        raise HTTPException(400, f"裁剪区域超出画面（画面 {frame_w}×{frame_h}）。")
    piece = image[y:y + h, x:x + w]
    draft_path = _unique_draft_path(_drafts_dir(), stem)
    _save_bgr_png(piece, draft_path)
    return {"draft": {"name": draft_path.stem,
                      "width": int(piece.shape[1]), "height": int(piece.shape[0])}}


def _adopt_sync(draft_stem: str, target: str) -> dict:
    draft_path = _drafts_dir() / f"{draft_stem}.png"
    if not draft_path.is_file():
        raise HTTPException(404, "这张草稿不存在。")
    image_dir = RESOURCE_DIR / "image"
    image_dir.mkdir(parents=True, exist_ok=True)
    from touken.server_pack import resolve_template_path, ServerPackError
    try:
        dest = resolve_template_path(f"{target}.png", RESOURCE_DIR)
    except ServerPackError as exc:
        raise HTTPException(400, str(exc))
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if dest.exists():
        # 覆盖正式资源前先把旧的留底，回头不满意还能找回来
        backup_dir = _adopt_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = _unique_draft_path(backup_dir, f"{stamp}_{target.replace('/', '_')}")
        shutil.copy2(dest, backup)
    shutil.copy2(draft_path, dest)
    return {"ok": True, "path": str(dest),
            "backup": str(backup) if backup is not None else None}


def _adb_ready() -> bool:
    """只查 adb 可执行文件在不在，不真连（面板进程不常驻游戏连接）。"""
    try:
        from .server import _CONFIG_PATH, _DEFAULT_ADB_PATH
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            adb_path = str(cfg.get("adb_path") or _DEFAULT_ADB_PATH)
        except (OSError, ValueError):
            adb_path = str(_DEFAULT_ADB_PATH)
        return Path(adb_path).exists()
    except Exception:
        return False


def _synthetic_ocr_probe_image():
    """白底黑字合成图，专门喂给离线绑定的试读：OCR 通道坏了不抛异常、
    只会安静回空表，所以必须真读出字才算通道可用。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (320, 80), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in ("msyh.ttc", "arial.ttf"):  # 微软雅黑/Arial，Windows 面板必有
        try:
            font = ImageFont.truetype(candidate, 40)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    draw.text((12, 18), "48/48", fill=(0, 0, 0), font=font)
    return np.array(image)[:, :, ::-1].copy()


def _try_bind_offline_ocr(adapter) -> bool:
    """离线 OCR 通道自检：离线图是文件读入的 numpy BGR 帧，cn_ocr 本地
    推理直接读，不需要设备连接。OCR 通道坏了不抛异常、只会安静回空表，
    所以必须真从合成探针图上读出字才算通道可用。"""
    try:
        from touken import cn_ocr
        from touken.device_adapter import Region
        probe = _synthetic_ocr_probe_image()
        found = cn_ocr.read_region(probe,
                                   Region(0, 0, probe.shape[1], probe.shape[0]))
        if not found:
            return False
        # 试读过了才挂牌 initialized（init() 开头拿它当「已就绪」的短路标志），
        # 没过的 adapter 原样还给 init() 走完整重连
        adapter._initialized = True
        return True
    except Exception:
        return False


def _create_ocr_adapter():
    """离线 OCR 通道：优先不连 ADB 直接自检 cn_ocr；不行再回落完整 init。
    测试 patch 本函数注入假 OCR adapter。"""
    adapter = _create_adapter()
    if _try_bind_offline_ocr(adapter):
        return adapter
    if adapter.init():
        return adapter
    raise RuntimeError("离线绑定和完整 init 都没能起 OCR 通道。")


def _ocr_test_sync(roi: dict, sessions: list) -> dict:
    """对已解析好的 ROI（{"name","x","y","w","h"}）逐帧 OCR 试读。"""
    # 会话先全验一遍再起 OCR 通道，免得白加载一遍模型
    plans = []
    for session in sessions:
        _validate_session_id(session)
        session_dir = _sessions_dir() / session
        if not session_dir.is_dir():
            raise HTTPException(404, f"会话 {session} 不存在。")
        plans.append((session, session_dir, _scan_session(session_dir)))

    try:
        adapter = _create_ocr_adapter()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"OCR 通道起不来：{exc}") from exc

    from touken.device_adapter import Region
    region = Region(roi["x"], roi["y"], roi["w"], roi["h"])
    results = []
    scanned = 0
    for session, session_dir, frames in plans:
        # 帧总数钳到上限，超出的整段不扫（会话按传入顺序，帧按编号）
        for frame in frames[:max(0, _OCR_MAX_FRAMES - scanned)]:
            scanned += 1
            image = _read_png(session_dir / frame["name"], color=True)
            if image is None:
                continue
            found = adapter.ocr_all(region, image=image)
            results.append({"session": session, "frame": frame["idx"],
                            "texts": [text for text, _point in found]})
    return {"roi": roi, "results": results}


def _ocr_test_sync_stored(name: str, sessions: list) -> dict:
    rois = _load_rois()
    if name not in rois:
        raise HTTPException(404, f"ROI「{name}」不存在。")
    return _ocr_test_sync({"name": name, **rois[name]}, sessions)


def create_template_lab_router() -> APIRouter:
    router = APIRouter(prefix="/api/template-lab")

    @router.get("/status")
    async def status():
        ledger = _is_ledger()
        return {"enabled": _is_dev() and not ledger,
                "ledger_mode": ledger, "adb_ready": _adb_ready()}

    @router.post("/capture")
    async def capture(request: Request):
        _ensure_capture_available()
        body = await _json_body(request)
        count = _clamp_int(body.get("count"), 10, _COUNT_MIN, _COUNT_MAX, "连拍张数")
        interval_ms = _clamp_int(body.get("interval_ms"), 500,
                                 _INTERVAL_MIN, _INTERVAL_MAX, "间隔毫秒")
        memo = _sanitize_memo(body.get("memo", ""))
        try:
            return await asyncio.to_thread(_capture_sync, count, interval_ms, memo)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, f"连拍失败：{exc}") from exc

    @router.get("/sessions")
    async def list_sessions():
        root = _sessions_dir()
        sessions = []
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir() and _SESSION_ID_RE.fullmatch(child.name):
                    sessions.append({"id": child.name,
                                     "memo": _read_session_memo(child),
                                     "frames": _scan_session(child)})
        sessions.sort(key=lambda item: item["id"], reverse=True)
        return {"sessions": sessions}

    @router.post("/session-memo")
    async def set_session_memo(request: Request):
        body = await _json_body(request)
        session = _validate_session_id(body.get("session"))
        session_dir = _sessions_dir() / session
        if not session_dir.is_dir():
            raise HTTPException(404, f"会话 {session} 不存在。")
        memo = _sanitize_memo(body.get("memo", ""))
        _write_session_memo(session_dir, memo)
        return {"ok": True, "session": session, "memo": memo or None}

    @router.get("/frame")
    async def get_frame(session: str, idx: int):
        path = _session_frame_path(session, idx)
        if not path.is_file():
            raise HTTPException(404, "这张会话帧不存在。")
        return FileResponse(str(path), media_type="image/png")

    @router.post("/crop")
    async def crop(request: Request):
        body = await _json_body(request)
        session = body.get("session")
        idx = body.get("frame")
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise HTTPException(400, "frame 必须是整数。")
        x = _rect_int(body.get("x"), "x")
        y = _rect_int(body.get("y"), "y")
        w = _rect_int(body.get("w"), "w")
        h = _rect_int(body.get("h"), "h")
        stem = _validate_name(body.get("name"), "草稿名")
        if x < 0 or y < 0 or w < 1 or h < 1:
            raise HTTPException(400, "裁剪宽高至少为 1，起点不能是负数。")
        return await asyncio.to_thread(_crop_sync, session, idx, x, y, w, h, stem)

    @router.get("/drafts")
    async def list_drafts():
        folder = _drafts_dir()
        drafts = []
        if folder.is_dir():
            for path in folder.glob("*.png"):
                width, height = _png_size(path)
                drafts.append({"name": path.stem, "width": width,
                               "height": height, "mtime": path.stat().st_mtime})
        drafts.sort(key=lambda item: item["mtime"], reverse=True)
        return {"drafts": drafts}

    @router.get("/draft")
    async def get_draft(name: str):
        stem = _validate_name(name, "草稿名")
        path = _drafts_dir() / f"{stem}.png"
        if not path.is_file():
            raise HTTPException(404, "这张草稿不存在。")
        return FileResponse(str(path), media_type="image/png")

    @router.post("/verify")
    async def verify(request: Request):
        body = await _json_body(request)
        stem = _validate_name(body.get("draft"), "草稿名")
        sessions = body.get("sessions")
        if not isinstance(sessions, list) or not all(isinstance(s, str) for s in sessions):
            raise HTTPException(400, "sessions 必须是会话编号列表。")
        threshold = _threshold(body.get("threshold", 0.7))
        return await asyncio.to_thread(_verify_sync, stem, sessions, threshold)

    @router.post("/adopt")
    async def adopt(request: Request):
        body = await _json_body(request)
        stem = _validate_name(body.get("draft"), "草稿名")
        target = _validate_target(body.get("target"))
        return await asyncio.to_thread(_adopt_sync, stem, target)

    @router.get("/rois")
    async def list_rois():
        rois = [{"name": name, **rect} for name, rect in _load_rois().items()]
        rois.sort(key=lambda item: item["name"])
        return {"rois": rois}

    @router.post("/rois")
    async def save_roi(request: Request):
        body = await _json_body(request)
        name = _validate_name(body.get("name"), "ROI 名")
        x, y, w, h = _validate_roi_rect(body)
        rois = _load_rois()
        roi = {"x": x, "y": y, "w": w, "h": h, "updated": time.time()}
        rois[name] = roi
        _save_rois(rois)
        return {"ok": True, "roi": {"name": name, **roi}}

    @router.delete("/rois/{name}")
    async def delete_roi(name: str):
        stem = _validate_name(name, "ROI 名")
        rois = _load_rois()
        if stem not in rois:
            raise HTTPException(404, f"ROI「{stem}」不存在。")
        del rois[stem]
        _save_rois(rois)
        return {"ok": True}

    @router.post("/ocr-test")
    async def ocr_test(request: Request):
        body = await _json_body(request)
        sessions = body.get("sessions")
        if not isinstance(sessions, list) \
                or not all(isinstance(s, str) for s in sessions):
            raise HTTPException(400, "sessions 必须是会话编号列表。")
        name, rect = body.get("name"), body.get("rect")
        if name is not None and rect is not None:
            raise HTTPException(400, "name 和 rect 只能给一个。")
        if rect is not None:
            # 直传模式：xywh 不查存储，roi 原样回 {name: null, x, y, w, h}
            x, y, w, h = _validate_xywh(rect)
            roi = {"name": None, "x": x, "y": y, "w": w, "h": h}
            return await asyncio.to_thread(_ocr_test_sync, roi, sessions)
        if name is None:
            raise HTTPException(400, "必须给 name 或 rect 之一。")
        stem = _validate_name(name, "ROI 名")
        return await asyncio.to_thread(_ocr_test_sync_stored, stem, sessions)

    @router.get("/code-rois")
    async def list_code_rois():
        return {"rois": roi_overrides.effective_rois()}

    @router.post("/code-rois")
    async def save_code_roi(request: Request):
        body = await _json_body(request)
        roi_id = body.get("id")
        if not isinstance(roi_id, str) or roi_id not in _registry_ids():
            raise HTTPException(404, f"代码 ROI「{roi_id}」不在注册表里。")
        rect = _validate_xyxy(body.get("rect"))
        try:
            roi_overrides.save_override(roi_id, rect)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        roi = next(item for item in roi_overrides.effective_rois()
                   if item["id"] == roi_id)
        return {"ok": True, "roi": roi}

    @router.delete("/code-rois/{roi_id}")
    async def delete_code_roi(roi_id: str):
        if roi_id not in _registry_ids():
            raise HTTPException(404, f"代码 ROI「{roi_id}」不在注册表里。")
        # 没覆盖也算成功：幂等恢复默认
        roi_overrides.delete_override(roi_id)
        return {"ok": True}

    return router
