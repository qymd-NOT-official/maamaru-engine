# -*- coding: utf-8 -*-
"""连续输入测试：在专用浏览器里新开一个自建计数页，定时连点多次并逐次核对。

回答的问题只有一个：人走开后（后台/熄屏），输入通道能不能持续生效——
现有单次点击探针只验证过「点一次」。测试页是我们自建的空白页，计多少次
点击都无所谓；全程不触碰游戏页面。

手动跑法（亮屏 3 次）：

    python -m touken.jp_input_probe --count 3 --interval 5
    python -m touken.jp_input_probe --count 5 --interval 15 --mode screenoff
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from urllib.parse import quote

from . import jp_browser_probe as frames
from . import jp_listener
from .jp_click_probe import turn_display_off

_lock = threading.Lock()
_cancel = threading.Event()
_state: dict = {'state': 'idle', 'detail': '尚未开始'}

_COUNTER_HTML = (
    '<!doctype html><meta charset="utf-8"><title>input-probe</title>'
    '<body style="margin:0;width:100vw;height:100vh;background:#183048">'
    '<script>window.__clicks=0;'
    "document.addEventListener('click',function(){window.__clicks+=1;"
    "document.body.style.background='#2a5a3a'})</script>")
COUNTER_URL = 'data:text/html;charset=utf-8,' + quote(_COUNTER_HTML)


def status() -> dict:
    with _lock:
        return {**_state, 'clicks': [dict(row) for row in _state.get('clicks', [])]}


def _step(label: str, operation):
    began = time.monotonic()
    with _lock:
        _state['stage'] = label
    try:
        return operation()
    except Exception as error:
        with _lock:
            _state.setdefault('failed_stage', label)
            _state.setdefault('error_type', type(error).__name__)
        raise
    finally:
        with _lock:
            _state.setdefault('steps', []).append(
                {'stage': label, 'seconds': round(time.monotonic() - began, 3)})


def _browser_socket() -> jp_listener._CdpSocket:
    version = jp_listener._http_json('/json/version') or {}
    url = version.get('webSocketDebuggerUrl')
    if not url:
        raise ValueError('未找到日服浏览器，请从まあ丸打开游戏')
    return jp_listener._CdpSocket(url)


def _open_counter_tab():
    """新建自建计数页 -> (浏览器连接, 页面连接, target_id)。"""
    browser = _step('连接浏览器', _browser_socket)
    try:
        target = _step('新建测试页', lambda: browser.call(
            'Target.createTarget', {'url': COUNTER_URL, 'background': False}))['targetId']
        time.sleep(1.0)  # 等页面加载出清单
        rows = _step('查找测试页', lambda: jp_listener._http_json('/json/list')) or []
        page = next((row for row in rows
                     if row.get('id') == target and row.get('type') == 'page'), None)
        if not page:
            raise ValueError('测试页未能打开')
        socket = _step('连接测试页', lambda: jp_listener._CdpSocket(page['webSocketDebuggerUrl']))
        return browser, socket, target
    except Exception:
        browser.close()
        raise


def _click_and_count(socket, view, expected: int) -> dict:
    """点一次页面中心，再读计数；返回本次结果。"""
    point = {'x': view['clientWidth'] / 2, 'y': view['clientHeight'] / 2,
             'button': 'left', 'clickCount': 1}
    socket.call('Input.dispatchMouseEvent',
                {**point, 'type': 'mouseMoved', 'button': 'none', 'buttons': 0})
    time.sleep(0.1)
    try:
        socket.call('Input.dispatchMouseEvent', {**point, 'type': 'mousePressed', 'buttons': 1})
        time.sleep(0.1)
    finally:
        socket.call('Input.dispatchMouseEvent', {**point, 'type': 'mouseReleased', 'buttons': 0})
    time.sleep(0.5)
    result = socket.call('Runtime.evaluate',
                         {'expression': 'window.__clicks||0', 'returnByValue': True})
    actual = result.get('result', {}).get('value')
    return {'at': time.time(), 'expected': expected, 'actual': actual, 'ok': actual == expected}


def _run(count: int, interval: float, mode: str | None) -> None:
    browser = socket = None
    try:
        browser, socket, target = _open_counter_tab()
        view = _step('读取页面尺寸', lambda: socket.call('Page.getLayoutMetrics'))['cssLayoutViewport']
        if view.get('clientWidth', 0) <= 0 or view.get('clientHeight', 0) <= 0:
            raise ValueError('测试页尺寸无效，请恢复浏览器窗口后重试')
        if mode == 'screenoff':
            if _cancel.wait(3):
                return
            _step('请求关闭屏幕', turn_display_off)
        delivered = 0
        for index in range(1, count + 1):
            if _cancel.wait(interval):
                return
            row = _step(f'第 {index} 次点击并核对', lambda i=index: _click_and_count(socket, view, i))
            delivered += int(row['ok'])
            with _lock:
                _state['clicks'].append(row)
                _state['delivered'] = delivered
                _state['detail'] = f'已点击 {index}/{count} 次，生效 {delivered} 次'
        with _lock:
            _state['detail'] = (f'{count} 次点击全部生效，输入通道在人走开后可持续工作'
                                if delivered == count else
                                f'{count} 次点击仅 {delivered} 次生效，人走开后输入不可靠，'
                                '自动化只应在亮屏前台使用')
    except ValueError as error:
        with _lock:
            _state['detail'] = str(error)
    except Exception:
        with _lock:
            _state['detail'] = (f"{_state.get('failed_stage', '测试')}失败"
                                f"（{_state.get('error_type', '未知错误')}）；不重试")
    finally:
        if browser:
            try:
                browser.call('Target.closeTarget', {'targetId': target})
            except Exception:
                pass
            browser.close()
        if socket:
            socket.close()
        with _lock:
            _state.update(state='cancelled' if _cancel.is_set() else 'done',
                          finished_at=time.time())
        try:
            path = frames.JP_DATA_DIR / 'debug' / 'input_probe.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(status(), ensure_ascii=False, indent=2),
                                 encoding='utf-8')
            temporary.replace(path)
        except OSError:
            pass


def start(count: int = 5, interval: float = 15.0, mode: str | None = None) -> dict:
    global _state
    if mode not in (None, 'background', 'screenoff'):
        raise ValueError('无效测试模式')
    if not (1 <= count <= 60):
        raise ValueError('点击次数应在 1-60 之间')
    if not (2.0 <= interval <= 600.0):
        raise ValueError('点击间隔应在 2-600 秒之间')
    with _lock:
        if _state.get('state') == 'running':
            raise ValueError('已有连续输入测试正在运行')
        _cancel.clear()
        hint = {'background': '请切到别的窗口，不要调整浏览器窗口',
                'screenoff': '3 秒后关闭屏幕，然后连续点击'}.get(mode, '保持亮屏，不要操作浏览器')
        _state = {'state': 'running', 'started_at': time.time(), 'mode': mode or 'front',
                  'count': count, 'interval': interval, 'delivered': 0,
                  'clicks': [], 'steps': [], 'detail': f'{hint}；共 {count} 次，间隔 {interval:g} 秒'}
        threading.Thread(target=_run, args=(count, interval, mode),
                         daemon=True, name='jp-input-probe').start()
        return dict(_state)


def cancel() -> dict:
    _cancel.set()
    return status()


def main() -> None:
    parser = argparse.ArgumentParser(description='日服连续输入测试')
    parser.add_argument('--count', type=int, default=5)
    parser.add_argument('--interval', type=float, default=15.0)
    parser.add_argument('--mode', choices=['background', 'screenoff'], default=None)
    args = parser.parse_args()
    start(count=args.count, interval=args.interval, mode=args.mode)
    while True:
        current = status()
        print(current['detail'], flush=True)
        if current['state'] != 'running':
            break
        time.sleep(2)


if __name__ == '__main__':
    main()
