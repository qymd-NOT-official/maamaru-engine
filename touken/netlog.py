# -*- coding: utf-8 -*-
"""Chrome net-export 日志解析：从 chrome://net-export 导出的 JSON 里
提取 HTTP 事务（请求行 + 响应体）。

要求导出时勾选「Include raw bytes」。原理：

- ``URL_REQUEST_START_JOB`` 事件给出 source id -> URL 的映射；
- ``SSL_SOCKET_BYTES_SENT`` 事件携带 TLS 解密前的应用层明文，
  其中以 ``GET ``/``POST `` 开头的片段即请求报文；
- ``URL_REQUEST_JOB_FILTERED_BYTES_READ`` 事件携带解码后的响应体，
  按 source id 聚合后 gzip 解压即得原文。

已知限制：仅覆盖 TCP/TLS 会话；经 QUIC（HTTP/3）的会话不产出上述
事件，抓不到时请在浏览器 flags 里停用 QUIC 或让流量经过 HTTP 代理
（CONNECT 隧道下浏览器自动回退 TCP）。大日志（数百 MB）一次性读入
内存，需要流式处理时再改造。
"""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Union

PathLike = Union[str, Path]

_METHODS = (b"GET ", b"POST ", b"PUT ", b"DELETE ", b"HEAD ", b"PATCH ")


@dataclass
class Transaction:
    """一次 HTTP 往返。request_line 形如 'POST /path?x=1 HTTP/1.1'，
    可能为 None（响应在而请求帧未捕获，例如连接复用边界）。"""

    url: str
    method: str
    path: str
    request_line: str | None
    response_body: bytes
    ts: float = 0.0

    def response_json(self) -> dict | list | None:
        """响应体按 JSON 解析；非 JSON（图片/wasm 等）返回 None。"""
        try:
            return json.loads(self.response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


def _type_ids(constants: dict) -> dict[str, int]:
    """logEventTypes 的名字 -> 数字 id（各 Chrome 版本编号不同）。"""
    table = constants.get("logEventTypes", {})
    return {name: int(tid) for name, tid in table.items()}


def _gunzip(blob: bytes) -> bytes:
    if blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob)
        except OSError:
            pass
    return blob


@dataclass
class _Pending:
    method: str
    path: str
    line: str
    ts: float


def parse_transactions(path: PathLike,
                       host_filter: str | None = None) -> list[Transaction]:
    """解析整份 net-export，返回按时间排序的事务列表。

    host_filter 给定时只保留 URL 中含该子串的事务（如域名）。
    请求与响应按 (method, path) 先进先出配对；会话被掐断时可能出现
    只有响应没有请求的事务，此时 method/path 从 URL 推断。
    """
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    events = doc.get("events", [])
    ids = _type_ids(doc.get("constants", {}))
    t_start = ids.get("URL_REQUEST_START_JOB")
    t_read = ids.get("URL_REQUEST_JOB_FILTERED_BYTES_READ")
    t_sent = ids.get("SSL_SOCKET_BYTES_SENT")

    url_by_source: dict[int, str] = {}
    for ev in events:
        if ev.get("type") != t_start:
            continue
        params = ev.get("params", {})
        url = params.get("url")
        sid = ev.get("source", {}).get("id")
        if url and sid is not None:
            url_by_source[sid] = url

    pending: dict[tuple[str, str], list[_Pending]] = {}
    for ev in events:
        if ev.get("type") != t_sent:
            continue
        raw64 = ev.get("params", {}).get("bytes")
        if not raw64:
            continue
        try:
            raw = base64.b64decode(raw64)
        except ValueError:
            continue
        if not raw.startswith(_METHODS):
            continue
        head = raw.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        parts = head.split(" ")
        if len(parts) < 2:
            continue
        host = ""
        for line in raw.split(b"\r\n")[1:12]:
            if line.lower().startswith(b"host:"):
                host = line.split(b":", 1)[1].strip().decode(
                    "utf-8", errors="replace")
                break
        method, req_path = parts[0], parts[1]
        key = (method, f"{host}{req_path}")
        pending.setdefault(key, []).append(
            _Pending(method, req_path, head, float(ev.get("time", 0))))

    body_parts: dict[int, list[bytes]] = {}
    order: list[int] = []
    for ev in events:
        if ev.get("type") != t_read:
            continue
        sid = ev.get("source", {}).get("id")
        raw64 = ev.get("params", {}).get("bytes")
        if sid is None or not raw64:
            continue
        try:
            chunk = base64.b64decode(raw64)
        except ValueError:
            continue
        if sid not in body_parts:
            body_parts[sid] = []
            order.append(sid)
        body_parts[sid].append(chunk)

    out: list[Transaction] = []
    for sid in order:
        url = url_by_source.get(sid, "")
        if not url:
            continue
        if host_filter and host_filter not in url:
            continue
        blob = _gunzip(b"".join(body_parts[sid]))
        # 从 URL 反推 method/path，用于和请求帧配对
        without_scheme = url.split("://", 1)[-1]
        slash = without_scheme.find("/")
        host_path = without_scheme if slash >= 0 else without_scheme + "/"
        path_only = host_path[host_path.find("/"):]
        req = None
        for candidate in (("POST", host_path), ("GET", host_path)):
            queue = pending.get(candidate)
            if queue:
                req = queue.pop(0)
                break
        method = req.method if req else "GET"
        out.append(Transaction(
            url=url,
            method=method,
            path=req.path if req else path_only,
            request_line=req.line if req else None,
            response_body=blob,
            ts=req.ts if req else 0.0,
        ))
    out.sort(key=lambda tx: tx.ts)
    return out


def iter_json_transactions(path: PathLike,
                           host_filter: str | None = None,
                           ) -> Iterator[tuple[Transaction, dict | list]]:
    """只产出响应为合法 JSON 的事务，省去逐个判空。"""
    for tx in parse_transactions(path, host_filter=host_filter):
        payload = tx.response_json()
        if payload is not None:
            yield tx, payload
