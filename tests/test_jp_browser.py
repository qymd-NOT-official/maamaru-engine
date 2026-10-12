import base64
import io

import pytest

from touken import jp_browser, jp_listener

GAME = {'id': 'game', 'type': 'page',
        'url': 'https://pc-play.games.dmm.com/play/tohken',
        'webSocketDebuggerUrl': 'ws://game'}
OTHER = {**GAME, 'id': 'game2'}


def _png_b64():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), (10, 20, 30)).save(buf, 'PNG')
    return base64.b64encode(buf.getvalue()).decode()


class FakeSocket:
    def __init__(self, url, state):
        self.url = url
        self.state = state
        self.calls = []
        self.closed = False
        self.broken = False
        self.fail_on = None

    def call(self, method, params=None, timeout=5.0):
        if self.broken:
            raise ConnectionError('CDP 连接已断开')
        self.calls.append((method, params or {}))
        if method == 'Page.getLayoutMetrics':
            return {'cssLayoutViewport': dict(self.state['view'])}
        if method == 'Page.captureScreenshot':
            return {'data': _png_b64()}
        return {}

    def send(self, method, params=None):
        if self.broken:
            raise ConnectionError('CDP 连接已断开')
        self.calls.append((method, params or {}))
        if self.fail_on and (params or {}).get('type') == self.fail_on:
            raise ConnectionError('CDP 连接已断开')

    def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    state = {'view': {'clientWidth': 1000, 'clientHeight': 600},
             'pages': [GAME], 'sockets': []}

    def make(url):
        socket = FakeSocket(url, state)
        state['sockets'].append(socket)
        return socket

    monkeypatch.setattr(jp_listener, '_http_json',
                        lambda path, timeout=2.0: state['pages'] if path == '/json/list' else None)
    monkeypatch.setattr(jp_listener, '_CdpSocket', make)
    return state


def test_connect_requires_game_page(fake):
    fake['pages'] = []
    with pytest.raises(jp_browser.BrowserError, match='未找到'):
        jp_browser.JpBrowser().connect()


def test_connect_rejects_multiple_game_pages(fake):
    fake['pages'] = [GAME, OTHER]
    with pytest.raises(jp_browser.BrowserError, match='只保留一个'):
        jp_browser.JpBrowser().connect()


def test_screenshot_returns_rgb_image(fake):
    with jp_browser.JpBrowser() as browser:
        image = browser.screenshot()
        assert image.mode == 'RGB' and image.size == (4, 4)
    assert fake['sockets'][0].closed


def test_view_change_trips_fuse_on_screenshot_and_click(fake):
    browser = jp_browser.JpBrowser().connect()
    fake['view'] = {'clientWidth': 1200, 'clientHeight': 600}
    with pytest.raises(jp_browser.BrowserError, match='尺寸已变化'):
        browser.screenshot()
    with pytest.raises(jp_browser.BrowserError, match='尺寸已变化'):
        browser.click(.5, .5)
    socket = fake['sockets'][0]
    assert not any(method == 'Input.dispatchMouseEvent' for method, _ in socket.calls)


def test_click_maps_normalized_point_full_sequence(fake):
    browser = jp_browser.JpBrowser().connect()
    browser.click(.8, .4)
    socket = fake['sockets'][0]
    events = [params for method, params in socket.calls
              if method == 'Input.dispatchMouseEvent']
    assert [params['type'] for params in events] == ['mouseMoved', 'mousePressed', 'mouseReleased']
    assert [params['buttons'] for params in events] == [0, 1, 0]
    assert all(params['x'] == 800 and params['y'] == 240 for params in events)


@pytest.mark.parametrize('x,y', [(0, .5), (1, .5), (-.1, .5), (True, .5), (.5, float('nan'))])
def test_click_rejects_point_outside_frame(fake, x, y):
    browser = jp_browser.JpBrowser().connect()
    with pytest.raises(ValueError):
        browser.click(x, y)


def test_press_failure_surfaces_original_error(fake):
    browser = jp_browser.JpBrowser().connect()
    socket = fake['sockets'][0]
    socket.fail_on = 'mousePressed'
    with pytest.raises(jp_browser.BrowserError, match='连接已断开'):
        browser.click(.5, .5)
    events = [params['type'] for method, params in socket.calls
              if method == 'Input.dispatchMouseEvent']
    # 连接已死，松开只能尽力而为；关键是不盖掉按下的原始错误
    assert events == ['mouseMoved', 'mousePressed']
    assert socket.closed


def test_reload_rebaselines_view(fake, monkeypatch):
    monkeypatch.setattr(jp_browser.time, 'sleep', lambda s: None)
    browser = jp_browser.JpBrowser().connect()
    fake['view'] = {'clientWidth': 1206, 'clientHeight': 755}  # 滚动条显隐级抖动
    browser.reload()
    socket = fake['sockets'][0]
    assert any(method == 'Page.reload' for method, _ in socket.calls)
    assert browser.base_view['clientWidth'] == 1206
    browser.screenshot()  # 不炸


def test_reload_never_stable_view_raises(fake, monkeypatch):
    monkeypatch.setattr(jp_browser.time, 'sleep', lambda s: None)
    browser = jp_browser.JpBrowser().connect()
    import itertools
    views = itertools.cycle([{'clientWidth': 1191, 'clientHeight': 740},
                             {'clientWidth': 1206, 'clientHeight': 755}])
    monkeypatch.setattr(browser, '_read_view', lambda: dict(next(views)))
    with pytest.raises(jp_browser.BrowserError, match='不稳定'):
        browser.reload(settle_timeout=0.2)


def test_relaxed_view_tolerates_flap_and_relocks(fake):
    browser = jp_browser.JpBrowser().connect()
    with browser.relaxed_view():
        fake['view'] = {'clientWidth': 1206, 'clientHeight': 755}
        browser.screenshot()  # 抖动不熔断
        browser.click(.5, .5)
    assert browser.base_view['clientWidth'] == 1206  # 退出时重锁基线
    fake['view'] = {'clientWidth': 800, 'clientHeight': 755}
    with pytest.raises(jp_browser.BrowserError, match='尺寸已变化'):
        browser.screenshot()  # 熔断恢复生效


def test_connection_drop_becomes_readable_error(fake):
    browser = jp_browser.JpBrowser().connect()
    fake['sockets'][0].broken = True
    with pytest.raises(jp_browser.BrowserError, match='连接已断开'):
        browser.screenshot()
    with pytest.raises(jp_browser.BrowserError, match='尚未连接'):
        browser.screenshot()


def test_error_messages_carry_no_connection_detail(fake):
    fake['sockets']  # silence
    browser = jp_browser.JpBrowser().connect()
    fake['sockets'][0].broken = True
    with pytest.raises(jp_browser.BrowserError) as caught:
        browser.screenshot()
    assert 'ws://' not in str(caught.value)
