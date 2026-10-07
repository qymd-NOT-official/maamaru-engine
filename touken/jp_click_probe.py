"""用户标定位置的单次定时输入测试；画面变化即停止，不重试。"""
import threading
import time

from . import jp_browser_probe as frames, jp_listener

_lock = threading.Lock()
_cancel = threading.Event()
_state = {'state': 'idle', 'detail': '尚未开始'}
_reference = None


def capture(target_id=None):
    rows = jp_listener._http_json('/json/list') or []
    pages = [row for row in rows if frames.is_game_page(row)
             and (target_id is None or row.get('id') == target_id)]
    if len(pages) != 1:
        raise ValueError('请只保留一个日服游戏页面，并进入本丸')
    page = pages[0]
    socket = jp_listener._CdpSocket(page['webSocketDebuggerUrl'])
    try:
        encoded = socket.call('Page.captureScreenshot', {'format': 'png', 'fromSurface': True}, timeout=12)['data']
        view = socket.call('Page.getLayoutMetrics')['cssLayoutViewport']
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
        return dict(_state)


def guarded_click(reference, x, y):
    page, _, summary, view = capture(reference['target_id'])
    if summary != reference['summary'] or view != reference['view']:
        raise ValueError('画面或窗口尺寸已变化，已跳过点击；请重新标定')
    if _cancel.is_set():
        raise ValueError('测试已取消')
    socket = jp_listener._CdpSocket(page['webSocketDebuggerUrl'])
    try:
        point = {'x': x * view['clientWidth'], 'y': y * view['clientHeight'],
                 'button': 'left', 'clickCount': 1}
        try:
            socket.call('Input.dispatchMouseEvent', {**point, 'type': 'mousePressed'})
        finally:
            socket.call('Input.dispatchMouseEvent', {**point, 'type': 'mouseReleased'})
    finally:
        socket.close()


def _run(reference, x, y):
    try:
        if _cancel.wait(360):
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
        _cancel.wait(50)
    except ValueError as error:
        with _lock:
            _state['detail'] = str(error)
    except Exception:
        with _lock:
            _state['detail'] = '浏览器操作或画面读取失败；不重试，请手动核对游戏'
    finally:
        with _lock:
            _state.update(state='cancelled' if _cancel.is_set() else 'done', finished_at=time.time())


def start(x, y):
    global _state
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
                  'detail': '等待第 6 分钟；不要操作游戏或调整窗口'}
        threading.Thread(target=_run, args=(reference, x, y), daemon=True, name='jp-click-probe').start()
        return dict(_state)


def cancel():
    _cancel.set()
    return status()
