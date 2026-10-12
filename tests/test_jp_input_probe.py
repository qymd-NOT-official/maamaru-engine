import pytest

from touken import jp_input_probe as probe
from touken import jp_listener


class FakePageSocket:
    """自建计数页：mouseReleased 模拟一次真实点击，evaluate 读计数。"""

    def __init__(self, url, state):
        self.state = state
        self.releases = 0

    def call(self, method, params=None, timeout=5.0):
        params = params or {}
        if method == 'Page.getLayoutMetrics':
            return {'cssLayoutViewport': {'clientWidth': 800, 'clientHeight': 600}}
        if method == 'Input.dispatchMouseEvent' and params.get('type') == 'mouseReleased':
            self.releases += 1
        if method == 'Runtime.evaluate':
            return {'result': {'value': self.state.get('clicks_seen', self.releases)}}
        return {}

    def close(self):
        pass


class FakeBrowserSocket:
    def __init__(self, url, state):
        self.state = state

    def call(self, method, params=None, timeout=5.0):
        params = params or {}
        if method == 'Target.createTarget':
            return {'targetId': 'tab1'}
        if method == 'Target.closeTarget':
            self.state['tab_closed'] = params.get('targetId')
        return {}

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch, tmp_path):
    state = {'tab_closed': None}
    monkeypatch.setattr(probe.frames, 'JP_DATA_DIR', tmp_path)

    def http_json(path, timeout=2.0):
        if path == '/json/version':
            return {'webSocketDebuggerUrl': 'ws://browser'}
        if path == '/json/list':
            return [{'id': 'tab1', 'type': 'page', 'webSocketDebuggerUrl': 'ws://tab'}]
        return None

    def make_socket(url):
        return FakeBrowserSocket(url, state) if url == 'ws://browser' else FakePageSocket(url, state)

    class Cancel:
        def __init__(self):
            self.calls = []

        def wait(self, seconds):
            self.calls.append(seconds)
            return False

        def is_set(self):
            return False

    cancel = Cancel()
    monkeypatch.setattr(jp_listener, '_http_json', http_json)
    monkeypatch.setattr(jp_listener, '_CdpSocket', make_socket)
    monkeypatch.setattr(probe, '_cancel', cancel)
    monkeypatch.setattr(probe, '_state', {'state': 'running', 'clicks': [], 'steps': []})
    return state, cancel


def test_all_clicks_delivered_reports_success(fake, tmp_path):
    state, cancel = fake
    probe._run(3, 15.0, None)
    result = probe.status()
    assert result['state'] == 'done'
    assert result['delivered'] == 3
    assert [row['ok'] for row in result['clicks']] == [True, True, True]
    assert '全部生效' in result['detail']
    assert cancel.calls == [15.0, 15.0, 15.0]
    assert state['tab_closed'] == 'tab1'
    report = (tmp_path / 'debug' / 'input_probe.json').read_text(encoding='utf-8')
    assert '"delivered": 3' in report
    assert 'ws://' not in report


def test_missed_click_marks_channel_unreliable(fake):
    state, _ = fake
    state['clicks_seen'] = 0  # 计数页始终读不到点击
    probe._run(3, 15.0, None)
    result = probe.status()
    assert result['delivered'] == 0
    assert '不可靠' in result['detail']


def test_screenoff_turns_display_off_before_clicking(fake, monkeypatch):
    _, cancel = fake
    off = []
    monkeypatch.setattr(probe, 'turn_display_off', lambda: off.append(True))
    probe._run(2, 15.0, 'screenoff')
    assert off == [True]
    assert cancel.calls[0] == 3
    assert probe.status()['delivered'] == 2


@pytest.mark.parametrize('kwargs', [
    {'mode': 'weird'}, {'count': 0}, {'count': 61}, {'interval': 1}, {'interval': 601}])
def test_invalid_params_never_start(kwargs):
    with pytest.raises(ValueError):
        probe.start(**kwargs)
    assert probe.status()['state'] == 'idle'


def test_duplicate_start_rejected(monkeypatch):
    monkeypatch.setattr(probe.threading.Thread, 'start', lambda self: None)
    probe.start(count=2)
    with pytest.raises(ValueError):
        probe.start(count=2)
