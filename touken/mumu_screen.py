"""MuMu 显存截图通道（external_renderer_ipc.dll 共享内存）。

不走 ADB：直接读模拟器渲染好的帧，毫秒级一张，天然免疫裸 ADB
screencap 流传输抽风吐半张图（image file is truncated → 旧截图链路掉线
判死，2026-08-25/26 两次实测炸点）。

dll 随 MuMuPlayer 附带，路径从 adb 路径推导：
  <install>/nx_device/12.0/shell/adb.exe
    → <install>/nx_device/12.0/shell/sdk/external_renderer_ipc.dll
函数签名参考 MAA 本家 MuMu 截图增强及公开实现（盯帧器）：
  nemu_connect(install_path: wchar*, instance_index) -> handle(>0 成功)
  nemu_capture_display(handle, display_id, buffer_size, *w, *h, *pixels)
    —— buffer_size=0 且 pixels=NULL 时只查询宽高
  nemu_disconnect(handle)

任何一步失败都返回 None，调用方（device_adapter）回退裸 ADB 老路。
连续失败 3 次后本场运行停用该通道（省得每帧白试），ADB 兜底照旧。
注意：工人进程用 os._exit(43) 自杀时不会走 disconnect，IPC 连接随
进程陪葬，MuMu 侧自己回收——这是刻意的，不为此加钩子。
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Optional

_MAX_FAILURES = 3
_DEFAULT_ADB_PORT = 16384  # MuMu 12：实例 N 的 ADB 端口 = 16384 + 32*N
_PORT_STEP = 32
# 连续这么多帧字节级一致 = 通道冻了。真静态画面（黑屏过场）误判了也无害：
# 回退 ADB 截到的还是同一张静态图，只是慢一点。2026-09-17 现场：通道冻在
# 公告弹窗帧上（dll 照样返回 rc=0），真实画面已在本丸，until_gone 死循环。
_STALE_LIMIT = 10



def _safe_print(message: object) -> None:
    """日志不许砸流程：GBK 终端吃不下的字符转义后再打。"""
    text = str(message)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        import sys
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, errors="backslashreplace").decode(encoding),
              flush=True)


class MumuScreen:
    """MuMu 共享内存截图。capture() 返回 BGR numpy 数组或 None。"""

    def __init__(self, install_root: Path, dll_path: Path,
                 instance_index: int = 0, display_id: int = 0):
        self.install_root = Path(install_root)
        self.dll_path = Path(dll_path)
        self.instance_index = instance_index
        self.display_id = display_id
        self._lib = None
        self._handle: Optional[int] = None
        self._buf = None
        self._buf_size = 0
        self._failures = 0
        self._dead = False
        self._prev_frame = None
        self._same_frames = 0
        self._stale_revives = 0

    @classmethod
    def from_adb(cls, adb_path: str, adb_address: str) -> Optional["MumuScreen"]:
        """从 ADB 路径/地址推导 MuMu 实例。不是 MuMu 布局就返回 None。"""
        try:
            shell_dir = Path(adb_path).resolve().parent  # .../12.0/shell
            dll = shell_dir / "sdk" / "external_renderer_ipc.dll"
            if not dll.is_file():
                return None
            install_root = shell_dir.parents[2]  # shell → 12.0 → nx_device → install
            index = 0
            try:
                port = int(str(adb_address).rsplit(":", 1)[-1])
                index = max(0, (port - _DEFAULT_ADB_PORT) // _PORT_STEP)
            except (ValueError, IndexError):
                pass
            return cls(install_root, dll, index)
        except (OSError, IndexError):
            return None

    def _load(self) -> bool:
        if self._lib is not None:
            return True
        try:
            lib = ctypes.CDLL(str(self.dll_path))
            lib.nemu_connect.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
            lib.nemu_connect.restype = ctypes.c_int
            lib.nemu_disconnect.argtypes = [ctypes.c_int]
            lib.nemu_disconnect.restype = None
            lib.nemu_capture_display.argtypes = [
                ctypes.c_int, ctypes.c_uint, ctypes.c_int,
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ubyte)]
            lib.nemu_capture_display.restype = ctypes.c_int
            self._lib = lib
            return True
        except OSError as exc:
            _safe_print(f"[MuMu截图] dll 加载失败: {exc}")
            self._dead = True
            return False

    def _connect(self) -> bool:
        if not self._load():
            return False
        handle = self._lib.nemu_connect(str(self.install_root),
                                        self.instance_index)
        if not handle or handle <= 0:
            _safe_print(f"[MuMu截图] 连接失败（实例 {self.instance_index}，"
                  f"返回 {handle}）")
            return False
        self._handle = handle
        _safe_print(f"[MuMu截图] ✓ 显存通道已连接（实例 {self.instance_index}）")
        return True

    def _drop(self) -> None:
        """本次连接作废：断开、计数，连续失败到顶就永久停用。"""
        if self._handle is not None:
            try:
                self._lib.nemu_disconnect(self._handle)
            except Exception:
                pass
            self._handle = None
        self._failures += 1
        if self._failures >= _MAX_FAILURES and not self._dead:
            self._dead = True
            _safe_print(f"[MuMu截图] 连续 {_MAX_FAILURES} 次失败，"
                  "本场停用显存通道，退回 ADB 截图")

    def capture(self):
        """抓一帧。返回 BGR numpy 数组；任何失败返回 None（调用方回退 ADB）。"""
        if self._dead:
            return None
        if self._handle is None and not self._connect():
            self._drop()
            return None
        try:
            width = ctypes.c_int(0)
            height = ctypes.c_int(0)
            rc = self._lib.nemu_capture_display(
                self._handle, self.display_id, 0,
                ctypes.byref(width), ctypes.byref(height), None)
            if rc != 0 or width.value <= 0 or height.value <= 0:
                _safe_print(f"[MuMu截图] 查询尺寸失败（rc={rc}）")
                self._drop()
                return None
            size = 4 * width.value * height.value
            if self._buf_size != size:
                self._buf = (ctypes.c_ubyte * size)()
                self._buf_size = size
            rc = self._lib.nemu_capture_display(
                self._handle, self.display_id, size,
                ctypes.byref(width), ctypes.byref(height), self._buf)
            if rc != 0:
                _safe_print(f"[MuMu截图] 抓帧失败（rc={rc}）")
                self._drop()
                return None
            import numpy as np
            frame = np.frombuffer(self._buf, dtype=np.uint8).reshape(
                (height.value, width.value, 4))
            # RGBA → BGR（识别引擎要 BGR），并垂直翻转（显存帧是倒的）
            bgr = np.ascontiguousarray(frame[::-1, :, [2, 1, 0]])
            self._failures = 0
            # 冻帧检测：dll 缓冲区冻结时照样返回 rc=0 + 旧帧，表面一切正常。
            # 字节级连撞 _STALE_LIMIT 次就断开重连；救过还冻就本场判死，
            # 返回 None 让调用方回退 ADB 截图。
            if self._prev_frame is not None and np.array_equal(bgr, self._prev_frame):
                self._same_frames += 1
                if self._same_frames >= _STALE_LIMIT:
                    self._same_frames = 0
                    self._stale_revives += 1
                    if self._handle is not None:
                        try:
                            self._lib.nemu_disconnect(self._handle)
                        except Exception:
                            pass
                        self._handle = None
                    if self._stale_revives >= 2:
                        _safe_print("[MuMu截图] 重连后仍是冻帧，本场停用显存通道，"
                                    "退回 ADB 截图")
                        self._dead = True
                    else:
                        _safe_print(f"[MuMu截图] 连续 {_STALE_LIMIT} 帧完全一致，"
                                    "通道疑似冻帧，断开重连")
                    return None
            else:
                self._same_frames = 0
                self._stale_revives = 0
            self._prev_frame = bgr
            return bgr
        except Exception as exc:
            _safe_print(f"[MuMu截图] 抓帧异常: {exc}")
            self._drop()
            return None
