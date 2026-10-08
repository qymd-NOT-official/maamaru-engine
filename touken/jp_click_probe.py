"""用户标定位置的单次定时输入测试；画面变化即停止，不重试。"""
import threading
import time
import json
import sys

from . import jp_browser_probe as frames, jp_listener

_lock = threading.Lock()
_cancel = threading.Event()
_state = {'state': 'idle', 'detail': '尚未开始'}
_reference = None


def step(label, operation):
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
            _state.setdefault('steps', []).append({'stage': label, 'seconds': round(time.monotonic() - began, 3)})


def turn_display_off():
    """仅一次关闭显示器，不改变电源设置，不阻止睡眠。"""
    if sys.platform != 'win32':
        raise ValueError('此测试仅支持 Windows')
    import ctypes
    from ctypes import wintypes
    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    if not send(0xffff, 0x0112, 0xf170, 2, 2, 1000, ctypes.byref(result)):
        raise RuntimeError('display_request_failed')


def capture(target_id=None):
    rows = step('查找游戏页面', lambda: jp_listener._http_json('/json/list')) or []
    pages = [row for row in rows if frames.is_game_page(row)
             and (target_id is None or row.get('id') == target_id)]
    if len(pages) != 1:
        raise ValueError('请只保留一个日服游戏页面，并进入本丸')
    page = pages[0]
    socket = step('连接游戏页面', lambda: jp_listener._CdpSocket(page['webSocketDebuggerUrl']))
    try:
        encoded = step('读取游戏画面', lambda: socket.call('Page.captureScreenshot', {'format': 'png', 'fromSurface': True}, timeout=12))['data']
        view = step('读取画面尺寸', lambda: socket.call('Page.getLayoutMetrics'))['cssLayoutViewport']
        return page, encoded, frames.frame_summary(encoded), view
    finally:
        socket.close()


def prepare():
    global _reference
    with _lock:
        if _state['state'] == 'running':
            raise ValueError('测试进行中，请先取消')
    page, encoded, summary, view = capture()
    if view.get('clientWidth', 0) <= 0 or view.get('clientHeight', 0) <= 0:
        raise ValueError('游戏画面尺寸无效，请恢复游戏窗口后重试')
    if summary['near_black']:
        raise ValueError('当前画面接近黑屏，请进入本丸后重试')
    with _lock:
        if _state['state'] == 'running':
            raise ValueError('测试进行中，请先取消')
        _reference = {'target_id': page['id'], 'summary': summary, 'view': view,
                      'prepared_at': time.monotonic()}
    return {'image': 'data:image/png;base64,' + encoded}


def status():
    with _lock:
        return {**_state, 'steps': [dict(row) for row in _state.get('steps', [])]}


def guarded_click(reference, x, y):
    page, _, summary, view = capture(reference['target_id'])
    if summary != reference['summary'] or view != reference['view']:
        raise ValueError('画面或窗口尺寸已变化，已跳过点击；请重新标定')
    if _cancel.is_set():
        raise ValueError('测试已取消')
    socket = step('连接输入通道', lambda: jp_listener._CdpSocket(page['webSocketDebuggerUrl']))
    try:
        point = {'x': x * view['clientWidth'], 'y': y * view['clientHeight'],
                 'button': 'left', 'clickCount': 1}
        step('定位点击位置', lambda: socket.call('Input.dispatchMouseEvent', {**point, 'type': 'mouseMoved', 'button': 'none', 'buttons': 0}))
        if _cancel.wait(0.1):
            raise ValueError('测试已取消')
        try:
            with _lock:
                _state['input_attempted'] = True
            step('按下按钮', lambda: socket.call('Input.dispatchMouseEvent', {**point, 'type': 'mousePressed', 'buttons': 1}))
            _cancel.wait(0.1)
        finally:
            step('松开按钮', lambda: socket.call('Input.dispatchMouseEvent', {**point, 'type': 'mouseReleased', 'buttons': 0}))
    finally:
        socket.close()


def _run(reference, x, y, immediate=False, mode=None):
    try:
        if mode == 'screenoff':
            if _cancel.wait(3):
                return
            step('请求关闭屏幕', turn_display_off)
        if _cancel.wait(10 if mode in ('background', 'screenoff') else 0 if immediate else 360):
            return
        guarded_click(reference, x, y)
        with _lock:
            _state.update(click_sent=True, clicked_at=time.time(), detail='已发送一次点击，等待画面核对')
        if _cancel.wait(10):
            return
        _, _, after, _ = capture(reference['target_id'])
        with _lock:
            changed = after['digest'] != reference['summary']['digest']
            _state.update(picture_changed=changed,
                detail='画面已变化，请确认是否进入结成' if changed else '未观察到画面变化，不能确认点击生效')
        if not immediate and mode is None:
            _cancel.wait(50)
    except ValueError as error:
        with _lock:
            _state['detail'] = str(error)
    except Exception:
        with _lock:
            _state['detail'] = f"{_state.get('failed_stage', '浏览器操作')}失败（{_state.get('error_type', '未知错误')}）；不重试，请手动核对游戏"
    finally:
        with _lock:
            _state.update(state='cancelled' if _cancel.is_set() else 'done', finished_at=time.time())
        try:
            path = frames.JP_DATA_DIR / 'debug' / 'click_probe.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(status(), ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        except OSError:
            pass


def start(x, y, immediate=False, mode=None):
    global _state
    if mode not in (None, 'background', 'screenoff'):
        raise ValueError('无效测试模式')
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or not (0 < x < 1 and 0 < y < 1):
        raise ValueError('请在截图内选择结成按钮')
    with _lock:
        if _state['state'] == 'running':
            raise ValueError('已有定时测试正在运行')
        if _reference is None or time.monotonic() - _reference['prepared_at'] > 120:
            raise ValueError('请先获取当前本丸截图，再选择结成按钮')
        reference = dict(_reference)
        _cancel.clear()
        _state = {'state': 'running', 'started_at': time.time(), 'click_sent': False,
                  'picture_changed': False, 'formation_verified': False, 'screen_off_verified': False,
                  'mode': mode or ('immediate' if immediate else 'timed'), 'steps': [], 'input_attempted': False,
                  'detail': '正在进行亮屏单次点击测试，约 10 秒后查看结果' if immediate else '等待第 6 分钟；不要操作游戏或调整窗口'}
        if mode:
            _state['detail'] = '3 秒后关闭屏幕，再等 10 秒点击一次' if mode == 'screenoff' else '10 秒后点击一次，请切到麻麻露，不要调整游戏窗口'
        threading.Thread(target=_run, args=(reference, x, y, immediate, mode), daemon=True, name='jp-click-probe').start()
        return dict(_state)


def cancel():
    _cancel.set()
    return status()
