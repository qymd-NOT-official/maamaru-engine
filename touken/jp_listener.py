# -*- coding: utf-8 -*-
"""日服实时听包：连上带调试端口的 Chrome（CDP），只读订阅 Network 事件，
把游戏接口的响应实时落进日服账房。

红线（与手动 netlog 导入同源同质）：

- 纯订阅 Network 事件，不注入脚本、不改请求、不向服务器多发任何东西；
  听来的事务与手动抓包文件里的一模一样。
- Chrome 必须带 --remote-debugging-port 启动；新版 Chrome 只允许非默认
  user-data-dir 开调试口，所以用日服专用配置档
  （用户数据目录/jp/browser-profile，首次使用需在里面登一次 DMM）。
- 战斗结算（battle/battle）是密文，与手动导入一样跳过不读内容。
"""

from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from touken import jp_import, jp_ledger
from touken.netlog import Transaction
from touken.runtime_paths import JP_DATA_DIR
from touken.telemetry import get_jp_telemetry_store

CDP_PORT = 9333
GAME_URL = "https://pc-play.games.dmm.com/play/tohken/"
GAME_HOST = "touken-ranbu.jp"  # 与手动导入的 host_filter 同源，钉住游戏域名
LISTENER_SCRIPT = "jp_listener"

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_chrome() -> str | None:
    """找本机 chrome.exe：注册表 App Paths 优先，常见安装路径兜底。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
            value, _ = winreg.QueryValueEx(key, "")
            if value and Path(value).is_file():
                return str(value)
    except (OSError, ValueError, TypeError):
        pass
    for candidate in _CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def browser_profile_dir() -> Path:
    return JP_DATA_DIR / "browser-profile"


def launch_browser(url: str = GAME_URL) -> str:
    """用日服专用配置档启动 Chrome（带调试口），返回启动的浏览器路径。

    专用配置档是独立书签/登录环境，第一次用要在里面登一次 DMM；
    它走系统代理，梯子不用单独配。"""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("没找到 Chrome，请先安装")
    profile = browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ])
    return chrome


def _http_json(path: str, timeout: float = 2.0) -> dict | list | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def debug_port_alive() -> bool:
    return _http_json("/json/version") is not None


def _interesting(url: str, card: dict) -> str | None:
    """URL 命中游戏域名 + 数据卡端点则返回端点路径，否则 None。"""
    if not url or GAME_HOST not in url:
        return None
    if not jp_import.classify(url, card):
        return None
    return jp_import.endpoint_path(url)


class _CdpSocket:
    """一条 CDP websocket 的最小封装：发命令、按 id 等应答、收事件。

    单线程使用：_wait 期间收到的事件先进 backlog，由主循环补处理。"""

    def __init__(self, url: str):
        import websocket
        # Chrome 拒绝带 Origin 的 ws 握手；suppress_origin 不发 Origin 即放行，
        # 比给 Chrome 加 --remote-allow-origins=* 干净（不放松浏览器安全面）。
        self._ws = websocket.create_connection(url, timeout=5,
                                               suppress_origin=True)
        self._next_id = 0
        self.backlog: list[dict] = []

    def send(self, method: str, params: dict | None = None,
             session_id: str | None = None) -> int:
        self._next_id += 1
        msg = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._ws.send(json.dumps(msg))
        return self._next_id

    def recv(self) -> dict | None:
        """收一条消息；超时返回 None，连接断开抛异常。"""
        import websocket
        try:
            raw = self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        if not raw:
            raise ConnectionError("CDP 连接已断开")
        return json.loads(raw)

    def call(self, method: str, params: dict | None = None,
             session_id: str | None = None, timeout: float = 5.0) -> dict:
        """发命令并等应答；期间挤进来的事件压进 backlog。"""
        cmd_id = self.send(method, params, session_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.recv()
            if msg is None:
                continue
            if msg.get("id") == cmd_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})
            self.backlog.append(msg)
        raise TimeoutError(f"CDP {method} 应答超时")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class JpListener:
    """听包线程：等浏览器调试口 -> 连 CDP -> 订阅游戏流量 -> 落账。

    断线自动重连（浏览器关掉再开、页面刷新都能续上），stop() 收尾。"""

    def __init__(self, launch: bool = False):
        self._launch = launch
        # 手动点「打开浏览器」时再放行一次拉起（自动循环绝不自己重开浏览器，
        # 老大亲手关掉的窗口不能自己又弹回来）
        self._relaunch_requested = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status = {
            "state": "off",          # off/waiting_browser/listening/error
            "detail": "",
            "started_at": None,
            "last_capture_at": None,
            "transactions": 0,
            "events_written": 0,
            "sessions": 0,
        }
        self._ledger: jp_ledger.JpLedgerSession | None = None
        self._sock: _CdpSocket | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._set(state="starting", detail="")
        self._thread = threading.Thread(
            target=self._run, name="jp-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock:
            sock.close()
        thread = self._thread
        if thread:
            thread.join(timeout=5)
        self._set(state="off", detail="")

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, **fields) -> None:
        with self._lock:
            self._status.update(fields)

    # ---- 主循环 ----

    def _run(self) -> None:
        self._ledger = jp_ledger.JpLedgerSession(
            get_jp_telemetry_store(), script=LISTENER_SCRIPT)
        self._set(started_at=time.time())
        launched = False
        while not self._stop.is_set():
            if not debug_port_alive():
                if self._launch and (not launched
                                     or self._relaunch_requested):
                    try:
                        launch_browser()
                        launched = True
                        self._relaunch_requested = False
                    except RuntimeError as exc:
                        self._set(state="error", detail=str(exc))
                        return
                self._set(state="waiting_browser",
                          detail="等日服浏览器上线（调试口还没开）")
                self._stop.wait(2.0)
                continue
            try:
                self._listen()
            except Exception as exc:  # 断线/浏览器关闭：喘口气再连
                if self._stop.is_set():
                    break
                self._set(state="waiting_browser",
                          detail=f"连接断了，等浏览器回来（{exc}）")
                self._stop.wait(2.0)

    def _listen(self) -> None:
        version = _http_json("/json/version")
        ws_url = (version or {}).get("webSocketDebuggerUrl")
        if not ws_url:
            raise ConnectionError("调试口没有 webSocketDebuggerUrl")
        sock = _CdpSocket(ws_url)
        self._sock = sock
        known: dict[str, str] = {}  # targetId -> sessionId，防重复挂靠
        try:
            # 扁平模式自动挂靠将来的目标（含跨域 iframe 拆出来的 OOPIF），
            # 不然游戏本体 iframe 的流量看不见。自动通知实测会漏「后出生的
            # iframe」，所以 _event_loop 里另有每 10 秒一轮的主动扫清单兜底。
            sock.call("Target.setAutoAttach", {
                "autoAttach": True, "waitForDebuggerOnStart": False,
                "flatten": True})
            for info in sock.call("Target.getTargets").get("targetInfos", []):
                self._attach(sock, known, info.get("targetId"),
                             info.get("type"))
            self._set(state="listening", detail="正在听包，玩就行",
                      sessions=len(known))
            self._event_loop(sock, known)
        finally:
            self._sock = None
            sock.close()

    def _attach(self, sock: _CdpSocket, known: dict, target_id: str | None,
                target_type: str | None,
                session_id: str | None = None) -> None:
        """挂上一个目标并开 Network 订阅；已挂过或类型不对则跳过。

        session_id 已知的（自动挂靠通知带来的）直接用，不再 attachToTarget
        ——同一目标挂两次会收到双份事件，锻刀消耗这类不去重的账会记双份。"""
        if not target_id or target_id in known:
            return
        if target_type not in ("page", "iframe"):
            return
        try:
            if session_id is None:
                result = sock.call("Target.attachToTarget",
                                   {"targetId": target_id, "flatten": True})
                session_id = result.get("sessionId")
            sock.call("Network.enable", session_id=session_id)
            known[target_id] = session_id
            self._set(sessions=len(known))
        except (RuntimeError, TimeoutError):
            pass  # 单个目标挂不上不掀桌子，下轮扫描再试

    def _event_loop(self, sock: _CdpSocket, known: dict) -> None:
        card = jp_import.load_card()
        # requestId -> (url, method, path, request_body)；只记命中数据卡的
        pending: dict[str, tuple] = {}
        last_sweep = time.monotonic()
        while not self._stop.is_set():
            msg = sock.backlog.pop(0) if sock.backlog else sock.recv()
            if msg is None:
                # 收信空闲时主动扫一遍目标清单：自动挂靠通知漏掉的目标
                # （实测漏过后出生的游戏 iframe）在这里补上。
                if time.monotonic() - last_sweep > 10:
                    last_sweep = time.monotonic()
                    targets = sock.call("Target.getTargets").get(
                        "targetInfos", [])
                    for info in targets:
                        self._attach(sock, known, info.get("targetId"),
                                     info.get("type"))
                continue
            method = msg.get("method", "")
            params = msg.get("params", {})
            session_id = msg.get("sessionId")
            if method == "Target.attachedToTarget":
                info = params.get("targetInfo", {})
                self._attach(sock, known, info.get("targetId"),
                             info.get("type"),
                             session_id=params.get("sessionId"))
                continue
            if method == "Target.detachedFromTarget":
                gone = params.get("sessionId")
                for tid, sid in list(known.items()):
                    if sid == gone:
                        del known[tid]
                self._set(sessions=len(known))
                continue
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                url = request.get("url", "")
                path = _interesting(url, card)
                if path:
                    body = request.get("postData")
                    pending[params["requestId"]] = (
                        url, request.get("method", "GET"), path,
                        body.encode("utf-8") if body else None)
                continue
            if method == "Network.loadingFinished":
                entry = pending.pop(params.get("requestId"), None)
                if not entry or not session_id:
                    continue
                url, http_method, path, req_body = entry
                try:
                    result = sock.call("Network.getResponseBody",
                                       {"requestId": params["requestId"]},
                                       session_id=session_id)
                except (RuntimeError, TimeoutError):
                    continue  # 响应体被浏览器回收了，这条跳过
                raw = result.get("body", "")
                body = (base64.b64decode(raw)
                        if result.get("base64Encoded")
                        else raw.encode("utf-8"))
                self._ingest(Transaction(
                    url=url, method=http_method, path=path,
                    request_line=None, response_body=body,
                    ts=time.time(), request_body=req_body))

    # 只数真正落账的事件；去重/跳过计数不算入账
    _WRITTEN_KEYS = ("inventory.captured", "training.captured",
                     "forge.started", "forge.collected", "resource.change")

    def _ingest(self, tx: Transaction) -> None:
        assert self._ledger is not None
        before = sum(self._ledger.stats[k] for k in self._WRITTEN_KEYS)
        try:
            self._ledger.feed(tx)
        except Exception:
            return  # 一条坏报文不掀桌子，下条继续
        after = sum(self._ledger.stats[k] for k in self._WRITTEN_KEYS)
        with self._lock:
            self._status["transactions"] += 1
            self._status["last_capture_at"] = time.time()
            if after > before:
                self._status["events_written"] += after - before


_listener: JpListener | None = None
_listener_lock = threading.Lock()


def start_listener(launch_browser_if_needed: bool = True) -> dict:
    """启动（或复用）全局听包线程，返回当前状态。"""
    global _listener
    with _listener_lock:
        if _listener is None:
            _listener = JpListener(launch=launch_browser_if_needed)
        if launch_browser_if_needed:
            _listener._launch = True
            _listener._relaunch_requested = True
        _listener.start()
        return _listener.status()


def stop_listener() -> dict:
    global _listener
    with _listener_lock:
        if _listener:
            _listener.stop()
            _listener = None
    return {"state": "off"}


def listener_status() -> dict:
    global _listener
    with _listener_lock:
        if _listener is None:
            return {"state": "off", "transactions": 0, "events_written": 0,
                    "sessions": 0,
                    "started_at": None, "last_capture_at": None,
                    "detail": "", "browser_alive": debug_port_alive()}
        status = _listener.status()
    status["browser_alive"] = debug_port_alive()
    return status
