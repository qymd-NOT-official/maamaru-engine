# -*- coding: utf-8 -*-
"""
minitouch 客户端：内核级触摸注入（自养版）。

为什么需要它：刀剑乱舞部分页面（刀帐竖向网格、一览横向翻页）不认
`input swipe` 注入的触摸事件，只认直接写 /dev/input 的内核级事件；
而 `input tap` 哪里都好使。minitouch（openstf/minitouch, Apache-2.0）
就是那个写设备节点的小二进制，本模块负责把它推进模拟器、拉起来、
通过 adb forward 的 socket 发指令。

协议（纯文本，换行结尾，每条指令后 `c` 提交）：
    d <contact> <x> <y> <pressure>   按下
    m <contact> <x> <y> <pressure>   移动
    u <contact>                      抬起
    r                                重置全部触点
坐标系是触摸设备的原始坐标，用启动横幅 `^ <触点上限> <max_x> <max_y> <压力上限>`
里的 max_x/max_y 按屏幕分辨率等比缩放。
"""

import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional


def _safe_print(message: object) -> None:
    """Best-effort console output（Windows 工人进程可能是 GBK 控制台）。"""
    try:
        print(str(message), flush=True)
    except UnicodeEncodeError:
        try:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(str(message).encode(encoding, errors="backslashreplace")
                  .decode(encoding), flush=True)
        except Exception:
            pass
    except Exception:
        pass

_DEVICE_PATH = "/data/local/tmp/maamaru-minitouch"
_SOCKET_NAME = "maamaru_touch"   # localabstract 名，跟 MAA 的 minitouch 错开
_PRESSURE = 50
_MOVE_INTERVAL = 0.016           # 滑动静差帧间隔（约 60fps）
_ABI_MAP = {
    "x86_64": "x86_64",
    "x86": "x86_64",             # x86 模拟器跑 x86_64 二进制（32 位内核没见过）
    "arm64-v8a": "arm64-v8a",
    "armeabi-v7a": "arm64-v8a",
    "armeabi": "arm64-v8a",
}


class MinitouchError(RuntimeError):
    pass


class MinitouchClient:
    """按需拉起、断线重连的 minitouch 客户端。线程安全（内部一把锁）。"""

    def __init__(self, adb_path: str, adb_address: str, bin_root: str):
        """
        Args:
            adb_path: ADB 可执行文件路径
            adb_address: 设备地址，如 127.0.0.1:16384
            bin_root: 仓内 minitouch 目录（resource/base/bin/minitouch）
        """
        self._adb = adb_path
        self._addr = adb_address
        self._bin_root = Path(bin_root)
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._max_x = 0
        self._max_y = 0
        self._scr_w = 0
        self._scr_h = 0
        self._rotation = 0           # 0/90/180/270，横屏模拟器通常是 90

    # ---------- 生命周期 ----------

    def _adb_run(self, args: list, timeout: float = 15.0) -> Optional[bytes]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run([self._adb, "-s", self._addr] + args,
                                  capture_output=True, timeout=timeout,
                                  check=False, creationflags=flags)
            if proc.returncode != 0:
                _safe_print(f"[minitouch] adb 失败 ({proc.returncode}): "
                            f"{' '.join(args[:3])}")
                return None
            return proc.stdout or b""
        except Exception as exc:
            _safe_print(f"[minitouch] adb 异常: {exc}")
            return None

    def _ensure_binary(self) -> bool:
        """把与设备 ABI 匹配的二进制推进去；已存在且大小一致就跳过。"""
        abi_raw = self._adb_run(["shell", "getprop", "ro.product.cpu.abi"])
        if not abi_raw:
            return False
        abi = _ABI_MAP.get(abi_raw.decode().strip())
        if abi is None:
            _safe_print(f"[minitouch] 不支持的设备 ABI: {abi_raw!r}")
            return False
        local = self._bin_root / abi / "minitouch"
        if not local.exists():
            _safe_print(f"[minitouch] 仓内缺少二进制: {local}")
            return False
        remote = self._adb_run(["shell", f"stat -c %s {_DEVICE_PATH} 2>/dev/null; true"])
        if remote and remote.decode().strip() == str(local.stat().st_size):
            return True
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                [self._adb, "-s", self._addr, "push", str(local), _DEVICE_PATH],
                capture_output=True, timeout=30.0, check=False,
                creationflags=flags)
            if proc.returncode != 0:
                _safe_print(f"[minitouch] push 失败: "
                            f"{proc.stderr.decode('utf-8', 'ignore')[:120]}")
                return False
        except Exception as exc:
            _safe_print(f"[minitouch] push 异常: {exc}")
            return False
        return self._adb_run(["shell", f"chmod 755 {_DEVICE_PATH}"]) is not None

    def _read_banner(self, quiet: bool = False) -> bool:
        """从 socket 读启动横幅，解析 ^ 行的 max_x/max_y。"""
        buf = b""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                chunk = self._sock.recv(4096)
            except (OSError, socket.timeout):
                break
            if not chunk:
                break
            buf += chunk
            if buf.count(b"\n") >= 2:
                break
        for line in buf.decode("utf-8", "ignore").splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and parts[0] == "^":
                try:
                    self._max_x = int(parts[2])
                    self._max_y = int(parts[3])
                except ValueError:
                    continue
                return self._max_x > 0 and self._max_y > 0
        if not quiet:
            _safe_print(f"[minitouch] 启动横幅解析失败: {buf[:120]!r}")
        return False

    def _read_screen_size(self) -> bool:
        out = self._adb_run(["shell", "wm", "size"])
        if not out:
            return False
        # "Physical size: 1280x720"（Override size 在前时取最后一行）
        for line in reversed(out.decode("utf-8", "ignore").splitlines()):
            if "size:" in line and "x" in line:
                try:
                    wh = line.rsplit(":", 1)[1].strip().split("x")
                    self._scr_w, self._scr_h = int(wh[0]), int(wh[1])
                    break
                except (ValueError, IndexError):
                    continue
        if self._scr_w <= 0 or self._scr_h <= 0:
            return False
        # 触摸坐标系永远是「自然竖屏」方向，横屏（90/270）要旋转换算
        rot_out = self._adb_run(["shell", "dumpsys window"])
        self._rotation = 0
        if rot_out:
            m = re.search(r"mRotation=ROTATION_(\d+)",
                          rot_out.decode("utf-8", "ignore"))
            if m:
                self._rotation = int(m.group(1))  # 名字里就是度数：0/90/180/270
        return True

    def _connect_locked(self) -> bool:
        """（持锁）拉起设备端进程并建立 socket。已连接则直接返回。"""
        if self._sock is not None:
            return True
        self.close_locked()
        if not self._ensure_binary() or not self._read_screen_size():
            return False
        # 旧实例清场（重名 socket 会被旧进程占着。[h] 防 pkill 匹配到
        # 自己这条 shell 命令行把自家 shell 一起带走——实测会返回 143）
        self._adb_run(["shell", f"pkill -f 'maamaru-minitouc[h]'; true"])
        fwd = self._adb_run(["forward", "tcp:0",
                             f"localabstract:{_SOCKET_NAME}"])
        if not fwd:
            return False
        try:
            port = int(fwd.decode().strip())
        except ValueError:
            _safe_print(f"[minitouch] forward 返回异常: {fwd!r}")
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [self._adb, "-s", self._addr, "shell",
                 f"{_DEVICE_PATH} -n {_SOCKET_NAME}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags)
        except Exception as exc:
            _safe_print(f"[minitouch] 进程拉起失败: {exc}")
            return False
        # minitouch 建 socket 要一瞬间，连接带重试；
        # forward 在设备端还没监听时会接受连接又立刻掐掉（空横幅），
        # 前几次失败是正常竞态，不刷日志，最后一次再吱声
        for attempt in range(10):
            try:
                sock = socket.create_connection(("127.0.0.1", port),
                                                timeout=5.0)
                sock.settimeout(10.0)
                self._sock = sock
                if self._read_banner(quiet=attempt < 9):
                    return True
            except (OSError, socket.timeout):
                pass
            self._sock = None
            time.sleep(0.3)
        _safe_print("[minitouch] 连接/横幅握手失败")
        self.close_locked()
        return False

    def close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    def close(self) -> None:
        with self._lock:
            self.close_locked()

    # ---------- 触摸操作 ----------

    def _scale(self, x: float, y: float) -> tuple[int, int]:
        """屏幕像素坐标（当前朝向）→ 触摸设备原始坐标（自然竖屏系）。

        rot 90（MuMu 横屏常态）：屏幕横过来时，触摸系的原点在屏幕
        右上角方向——natural_x = 屏高 - y，natural_y = x。
        """
        w, h = self._scr_w, self._scr_h   # 物理（自然方向）分辨率
        if self._rotation == 90:
            sw, sh = h, w                 # 横屏后的屏幕尺寸
            nx, ny = sh - y, x
        elif self._rotation == 180:
            nx, ny = w - x, h - y
        elif self._rotation == 270:
            sw, sh = h, w
            nx, ny = y, sw - x
        else:
            nx, ny = x, y
        return (round(nx * self._max_x / w),
                round(ny * self._max_y / h))

    def _send_locked(self, *lines: str) -> bool:
        try:
            payload = ("\n".join(lines) + "\n").encode("ascii")
            self._sock.sendall(payload)
            return True
        except (OSError, AttributeError) as exc:
            _safe_print(f"[minitouch] 指令发送失败: {exc}")
            self.close_locked()
            return False

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 400) -> bool:
        """内核注入滑动（起点终点都是屏幕像素坐标）。断线自动重连一次。"""
        with self._lock:
            for attempt in (1, 2):
                if not self._connect_locked():
                    continue
                if self._swipe_locked(x1, y1, x2, y2, duration_ms):
                    return True
            _safe_print(f"[minitouch] 滑动 ({x1},{y1}) → ({x2},{y2}) 失败")
            return False

    def _swipe_locked(self, x1, y1, x2, y2, duration_ms) -> bool:
        if not self._send_locked("r", "c"):
            return False
        sx, sy = self._scale(x1, y1)
        if not self._send_locked(f"d 0 {sx} {sy} {_PRESSURE}", "c"):
            return False
        steps = max(2, round(duration_ms / (_MOVE_INTERVAL * 1000)))
        for i in range(1, steps + 1):
            t = i / steps
            mx, my = self._scale(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            if not self._send_locked(f"m 0 {mx} {my} {_PRESSURE}", "c"):
                return False
            time.sleep(_MOVE_INTERVAL)
        if not self._send_locked("u 0", "c"):
            return False
        _safe_print(f"[minitouch] 滑动 ({x1},{y1}) → ({x2},{y2})")
        return True
