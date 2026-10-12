# -*- coding: utf-8 -*-
"""日服游戏页的常驻画面读取与输入通道。

与 jp_click_probe（标定后点一次的诊断）不同，这里供自动化任务长期持有：
页面级连接常驻，每次取帧和点击前都核对窗口尺寸——尺寸变了立即熔断，
绝不盲点。重连策略由调用方（出阵状态机）决定，本层保持简单可测。
"""
from __future__ import annotations

import base64
import io
import time

from . import jp_browser_probe as frames
from . import jp_listener


class BrowserError(RuntimeError):
    """游戏页连接/画面异常；文案面向玩家，不含连接细节。"""


class JpBrowser:
    """一个日服游戏页的操作通道；恰好一个游戏页才允许连接。"""

    def __init__(self):
        self._socket: jp_listener._CdpSocket | None = None
        self._page_id: str | None = None
        self._view: dict | None = None

    # ---- 生命周期 ----

    def connect(self) -> "JpBrowser":
        self.close()
        rows = jp_listener._http_json('/json/list') or []
        pages = [row for row in rows if frames.is_game_page(row)]
        if not pages:
            raise BrowserError('未找到日服游戏页面，请从まあ丸打开游戏并进入本丸')
        if len(pages) != 1:
            raise BrowserError('请只保留一个日服游戏页面')
        page = pages[0]
        socket = jp_listener._CdpSocket(page['webSocketDebuggerUrl'])
        self._socket, self._page_id = socket, page['id']
        try:
            self._view = self._read_view()
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket:
            socket.close()

    def __enter__(self) -> "JpBrowser":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def page_id(self) -> str | None:
        return self._page_id

    @property
    def base_view(self) -> dict | None:
        return dict(self._view) if self._view else None

    # ---- 底层调用 ----

    def _call(self, method: str, params: dict | None = None,
              timeout: float = 5.0) -> dict:
        if self._socket is None:
            raise BrowserError('尚未连接游戏页面')
        try:
            return self._socket.call(method, params, timeout=timeout)
        except ConnectionError:
            self.close()
            raise BrowserError('与游戏页面的连接已断开，请确认浏览器仍在运行')

    def _read_view(self) -> dict:
        view = self._call('Page.getLayoutMetrics')['cssLayoutViewport']
        if view.get('clientWidth', 0) <= 0 or view.get('clientHeight', 0) <= 0:
            raise BrowserError('游戏画面尺寸无效，请恢复游戏窗口后重试')
        return view

    def check_view(self) -> dict:
        """核对窗口尺寸与连接时一致；变了立即熔断，绝不盲点。"""
        current = self._read_view()
        base = self._view or current
        if (current['clientWidth'], current['clientHeight']) != (
                base['clientWidth'], base['clientHeight']):
            raise BrowserError('游戏窗口尺寸已变化，已停止操作；请恢复窗口后重新开始')
        return current

    def _send(self, method: str, params: dict | None = None) -> None:
        """只发不等应答（ack 进 backlog，后续 _call 会自动收掉）。

        输入通道专用：游戏嵌在跨源 iframe 里，熄屏/后台时 dispatch 的应答
        会被浏览器压到 5 秒后才回，但事件本身即刻送达——等 ack 纯属自残。
        """
        if self._socket is None:
            raise BrowserError('尚未连接游戏页面')
        try:
            self._socket.send(method, params or {})
        except ConnectionError:
            self.close()
            raise BrowserError('与游戏页面的连接已断开，请确认浏览器仍在运行')

    # ---- 画面与输入 ----

    def screenshot(self):
        """取一帧游戏画面（PIL RGB 图）；取帧前先核对窗口尺寸。"""
        self.check_view()
        encoded = self._call('Page.captureScreenshot',
                             {'format': 'png', 'fromSurface': True}, timeout=12)['data']
        from PIL import Image
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as picture:
            picture.load()
            return picture.convert('RGB')

    def click(self, nx: float, ny: float, settle: float = 0.1) -> None:
        """按画面归一化坐标点击一次（移动-按下-松开完整序列，只发不等应答）。"""
        if (isinstance(nx, bool) or isinstance(ny, bool)
                or not isinstance(nx, (int, float)) or not isinstance(ny, (int, float))
                or not (0 < nx < 1 and 0 < ny < 1)):
            raise ValueError('点击位置必须在游戏画面内')
        view = self.check_view()
        point = {'x': nx * view['clientWidth'], 'y': ny * view['clientHeight'],
                 'button': 'left', 'clickCount': 1}
        self._send('Input.dispatchMouseEvent',
                   {**point, 'type': 'mouseMoved', 'button': 'none', 'buttons': 0})
        time.sleep(settle)
        error = None
        try:
            self._send('Input.dispatchMouseEvent',
                       {**point, 'type': 'mousePressed', 'buttons': 1})
            time.sleep(settle)
        except BrowserError as exc:
            error = exc
        finally:
            try:
                self._send('Input.dispatchMouseEvent',
                           {**point, 'type': 'mouseReleased', 'buttons': 0})
            except BrowserError:
                pass  # 松开是善后，连接已死时不盖掉按下的原始错误
        if error:
            raise error
