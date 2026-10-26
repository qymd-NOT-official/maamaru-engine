# -*- coding: utf-8 -*-
"""素材包（ServerPack）：逻辑名 → 具体素材的绑定表（docs/dual-server-pipeline.md 3.2）。

一个服务器一个包，包 JSON 在 resource/base/server_packs/{cn,jp}.json：
流程单里只准写逻辑名（@ 开头），具体模板路径 / OCR 词 / ROI / 坐标 /
导航路径全部收在包里，两个服共用同一份流程单。

解析约定（与 flow_engine 改造同契）：
- 原始值（不带 @）按旧语义返回：模板是相对 image/ 的相对路径，
  OCR 就是目标文字本身，ROI 是 [x1,y1,x2,y2]，点是 [x,y] 或 {"x","y"}，
  导航是界面名或路径列表。原始值同样做类型 / 路径安全 / 边界校验，
  校验翻车一样抛 ValueError 子类。
- @name 从本服包解析；缺引用、坏路径、越界一律抛 ValueError 子类
  （ServerPackError 系），绝不静默返回 None。

边界语义：ROI 是半开区间 [x1,x2)×[y1,y2)，须 x1<x2、y1<y2（零面积不认），
允许 x2==基准宽、y2==基准高；点是有效像素，合法范围 0..基准宽-1 / 0..基准高-1。
模板路径白名单与旧流程一致：仅 .png，拒绝空段/'.'/'..'，反斜杠按路径分隔符处理。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

from .runtime_paths import RESOURCE_DIR

PACK_DIR_NAME = "server_packs"
LOGICAL_PREFIX = "@"


class ServerPackError(ValueError):
    """素材包一切翻车的基类（是 ValueError 子类，旧调用方按值错误兜得住）。"""


class UnknownServerError(ServerPackError):
    """未知服务器、包文件缺失或包 JSON 根本没法读。"""


class UnknownReferenceError(ServerPackError):
    """@逻辑名 在包里查无此人。"""


class PackPathError(ServerPackError):
    """模板路径不安全（绝对路径 / 越级）或指向不存在的文件。"""


class PackValueError(ServerPackError):
    """素材值类型不对或坐标越界。"""


def _is_coord(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_relative_path(value: Any, what: str) -> str:
    """模板路径必须是相对 image/ 的安全相对路径（旧语义即如此拼接）。

    白名单与旧流程一致：仅 .png；反斜杠一律按路径分隔符处理；
    拒绝绝对路径、盘符、空段、'.'、'..'。校验通过返回以 '/' 分隔的归一形式。
    """
    if not isinstance(value, str) or not value.strip():
        raise PackValueError(f"{what} 必须是字符串，收到 {type(value).__name__}")
    text = value.replace("\\", "/")
    if text.startswith("/") or Path(text).is_absolute() or Path(text).drive \
            or (len(text) >= 2 and text[1] == ":" and text[0].isalpha()):
        raise PackPathError(f"{what} 不安全（拒绝绝对路径和越级）: {value!r}")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PackPathError(f"{what} 不安全（空段/点段/越级）: {value!r}")
    if not parts[-1].endswith(".png"):
        raise PackPathError(f"{what} 只认 .png 模板: {value!r}")
    return text


@dataclass(frozen=True)
class ServerPack:
    """单个服务器的素材绑定表；字段即包 JSON 里的五个字典。"""

    server: str
    base_size: tuple[int, int]
    resource_dir: Path
    templates: dict[str, str] = field(default_factory=dict)
    ocr: dict[str, str] = field(default_factory=dict)
    rois: dict[str, list[int]] = field(default_factory=dict)
    points: dict[str, list[int]] = field(default_factory=dict)
    navigation: dict[str, Any] = field(default_factory=dict)

    # ── 解析入口：原始值按旧语义返回，@name 从包解析 ──

    def resolve_template(self, value: Union[str, Any]) -> str:
        """模板：返回相对 resource_dir/image/ 的相对路径（旧语义原样喂 template_match）。"""
        if not isinstance(value, str):
            raise PackValueError(f"模板引用必须是字符串，收到 {type(value).__name__}")
        if not value.startswith(LOGICAL_PREFIX):
            return _check_relative_path(value, "模板路径")
        name = value[len(LOGICAL_PREFIX):]
        rel = self._lookup(self.templates, name, "模板")
        rel = _check_relative_path(rel, f"模板 {name!r} 的路径")
        path = self.resource_dir / "image" / rel
        if not path.is_file():
            raise PackPathError(f"模板 {name!r} 的文件不存在: {rel}")
        return rel

    def resolve_ocr(self, value: Any) -> str:
        """OCR：返回要在画面上找的目标文字。"""
        if not isinstance(value, str):
            raise PackValueError(f"OCR 引用必须是字符串，收到 {type(value).__name__}")
        if not value.startswith(LOGICAL_PREFIX):
            if not value.strip():
                raise PackValueError("OCR 目标文字不能为空")
            return value
        word = self._lookup(self.ocr, value[len(LOGICAL_PREFIX):], "OCR 词")
        if not isinstance(word, str) or not word.strip():
            raise PackValueError(f"OCR 词 {value[len(LOGICAL_PREFIX):]!r} 必须是字符串")
        return word

    def resolve_roi(self, value: Any) -> list[int]:
        """ROI：返回 [x1,y1,x2,y2] 整数列表；越界、倒序、零面积、类型不对都翻车。"""
        if isinstance(value, str) and value.startswith(LOGICAL_PREFIX):
            value = self._lookup(self.rois, value[len(LOGICAL_PREFIX):], "ROI")
        return self._validate_roi(value)

    def resolve_point(self, value: Any) -> tuple[int, int]:
        """点：返回 (x, y) 整数二元组；接受 [x,y] / (x,y) / {"x","y"}。"""
        if isinstance(value, str) and value.startswith(LOGICAL_PREFIX):
            value = self._lookup(self.points, value[len(LOGICAL_PREFIX):], "点")
        x, y = self._coerce_point(value)
        width, height = self.base_size
        if not (0 <= x < width and 0 <= y < height):
            raise PackValueError(
                f"点 ({x},{y}) 越界：合法范围 0..{width - 1} × 0..{height - 1}")
        return (x, y)

    def resolve_navigation(self, value: Any) -> Any:
        """导航：界面名（字符串）或路径列表；原样返回，不解释内容。"""
        if isinstance(value, str) and value.startswith(LOGICAL_PREFIX):
            target = self._lookup(
                self.navigation, value[len(LOGICAL_PREFIX):], "导航")
        else:
            target = value
        if not isinstance(target, (str, list)):
            raise PackValueError(
                f"导航目标必须是界面名或路径列表，收到 {type(target).__name__}")
        return target

    # ── 内部校验 ──

    def _lookup(self, table: dict, name: str, what: str) -> Any:
        if not name:
            raise UnknownReferenceError(f"{what}逻辑名不能为空（单独的 @）")
        if name not in table:
            raise UnknownReferenceError(
                f"{self.server} 包里没有 {what} {name!r}")
        return table[name]

    def _validate_roi(self, value: Any) -> list[int]:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise PackValueError(
                f"ROI 必须是 4 元列表 [x1,y1,x2,y2]，收到 {value!r}")
        if not all(_is_coord(v) for v in value):
            raise PackValueError(f"ROI 坐标必须是整数，收到 {value!r}")
        x1, y1, x2, y2 = (int(v) for v in value)
        width, height = self.base_size
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise PackValueError(
                f"ROI {[x1, y1, x2, y2]} 越界/倒序/零面积：须满足 "
                f"0≤x1<x2≤{width}、0≤y1<y2≤{height}（半开区间，允许贴边）")
        return [x1, y1, x2, y2]

    def _coerce_point(self, value: Any) -> tuple[int, int]:
        if isinstance(value, dict):
            if set(value) != {"x", "y"}:
                raise PackValueError(f"点字典必须是 {{'x','y'}}，收到 {value!r}")
            pair = (value["x"], value["y"])
        elif isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise PackValueError(f"点必须是二元坐标，收到 {value!r}")
            pair = tuple(value)
        else:
            raise PackValueError(f"点必须是 [x,y] 或 {'{x,y}'}，收到 {type(value).__name__}")
        if not all(_is_coord(v) for v in pair):
            raise PackValueError(f"点坐标必须是整数，收到 {value!r}")
        return int(pair[0]), int(pair[1])


def _safe_server_name(server: Any) -> str:
    if not isinstance(server, str) or not server.strip():
        raise UnknownServerError(f"未知服务器: {server!r}")
    name = server.strip()
    if Path(name).name != name or name in (".", ".."):
        raise UnknownServerError(f"未知服务器: {server!r}")
    return name


def load_server_pack(server: str = "cn", resource_dir: Union[str, Path, None] = None) -> ServerPack:
    """读一个服务器的素材包并全量校验（坏包在加载时就翻车，不留到运行时）。

    resource_dir 默认仓内 resource/base；测试可指向临时目录。
    """
    name = _safe_server_name(server)
    root = Path(resource_dir) if resource_dir is not None else RESOURCE_DIR
    path = root / PACK_DIR_NAME / f"{name}.json"
    if not path.is_file():
        raise UnknownServerError(f"服务器 {name!r} 的素材包不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UnknownServerError(f"服务器 {name!r} 的素材包读不了: {exc}") from exc
    if not isinstance(data, dict):
        raise UnknownServerError(f"素材包 {path} 顶层必须是字典")

    base = data.get("base_size")
    if (not isinstance(base, (list, tuple)) or len(base) != 2
            or not all(_is_coord(v) for v in base) or base[0] <= 0 or base[1] <= 0):
        raise PackValueError(f"素材包 {path} 的 base_size 必须是正的 [宽, 高]，收到 {base!r}")

    tables: dict[str, dict] = {}
    for key in ("templates", "ocr", "rois", "points", "navigation"):
        table = data.get(key, {})
        if not isinstance(table, dict):
            raise PackValueError(f"素材包 {path} 的 {key} 必须是字典")
        tables[key] = table

    pack = ServerPack(
        server=name,
        base_size=(int(base[0]), int(base[1])),
        resource_dir=Path(root),
        templates=tables["templates"],
        ocr=tables["ocr"],
        rois=tables["rois"],
        points=tables["points"],
        navigation=tables["navigation"],
    )
    _validate_pack(pack, path)
    return pack


def _validate_pack(pack: ServerPack, path: Path) -> None:
    """加载期全量校验：包自带的每个条目都必须能经解析入口原样通过。"""
    for logical, _ in pack.templates.items():
        pack.resolve_template(LOGICAL_PREFIX + logical)
    for logical, _ in pack.ocr.items():
        pack.resolve_ocr(LOGICAL_PREFIX + logical)
    for logical, _ in pack.rois.items():
        pack.resolve_roi(LOGICAL_PREFIX + logical)
    for logical, _ in pack.points.items():
        pack.resolve_point(LOGICAL_PREFIX + logical)
    for logical, _ in pack.navigation.items():
        pack.resolve_navigation(LOGICAL_PREFIX + logical)
