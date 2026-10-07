"""用户主动启动的浏览器画面诊断；不发送游戏输入，不保存截图。"""
import base64
import hashlib
import io
import json
import threading
import time
from urllib.parse import urlsplit

from . import jp_listener
from .runtime_paths import JP_DATA_DIR

_lock = threading.Lock()
_state = {'state': 'idle'}


def frame_summary(encoded):
    from PIL import Image
    raw = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(raw)) as picture:
        picture.load()
        extrema = picture.convert('RGB').getextrema()
        return {'width': picture.width, 'height': picture.height,
                'near_black': all(high <= 8 for low, high in extrema),
                'digest': hashlib.sha256(picture.convert('RGB').tobytes()).hexdigest()}


def sample():
    targets = jp_listener._http_json('/json/list') or []
    target = next((row for row in targets if row.get('type') == 'page'
                   and urlsplit(row.get('url', '')).hostname in (
                       'pc-play.games.dmm.com', 'play.games.dmm.com')
                   and urlsplit(row.get('url', '')).path.rstrip('/') == '/play/tohken'), None)
    if not target:
        raise RuntimeError('game_missing')
    socket = jp_listener._CdpSocket(target['webSocketDebuggerUrl'])
    try:
        return frame_summary(socket.call('Page.captureScreenshot',
            {'format': 'png', 'fromSurface': True}, timeout=8)['data'])
    finally:
        socket.close()


def status():
    with _lock:
        return {**_state, 'samples': [dict(row) for row in _state.get('samples', [])]}


def _run():
    global _state
    previous = None
    started = time.monotonic()
    for index in range(43):
        if index:
            time.sleep(max(0, started + index * 10 - time.monotonic()))
        try:
            frame = sample()
            with _lock:
                _state['successful_frames'] += 1
                _state['changed_frames'] += int(previous is not None and previous != frame['digest'])
                _state['near_black_frames'] += int(frame['near_black'])
                _state['last_frame_at'] = time.time()
                _state['last_error'] = ''
                _state['samples'].append({'at': time.time(), 'ok': True,
                    'changed': previous is not None and previous != frame['digest'],
                    'near_black': frame['near_black']})
            previous = frame['digest']
        except Exception:
            with _lock:
                _state['failed_frames'] += 1
                _state['last_error'] = '游戏页面未找到，或画面读取失败'
                _state['samples'].append({'at': time.time(), 'ok': False})
    with _lock:
        _state['state'] = 'done'
        _state['finished_at'] = time.time()
        report = dict(_state)
    try:
        path = JP_DATA_DIR / 'debug' / 'browser_probe.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        with _lock:
            _state['last_error'] = '诊断结束，但报告未能保存'


def start():
    global _state
    with _lock:
        if _state.get('state') == 'running':
            return dict(_state)
        _state = {'state': 'running', 'started_at': time.time(),
                  'successful_frames': 0, 'changed_frames': 0, 'near_black_frames': 0,
                  'failed_frames': 0, 'last_frame_at': None, 'last_error': '',
                  'duration_seconds': 420, 'screen_off_verified': False,
                  'input_verified': False, 'samples': []}
        threading.Thread(target=_run, daemon=True, name='jp-browser-probe').start()
        return dict(_state)
