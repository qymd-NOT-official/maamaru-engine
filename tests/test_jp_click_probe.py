import pytest
from touken import jp_click_probe as probe


@pytest.fixture(autouse=True)
def isolated_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.frames, 'JP_DATA_DIR', tmp_path)


@pytest.fixture
def reference(monkeypatch):
    ref = {'target_id': 'game', 'summary': {'digest': 'same', 'near_black': False},
           'view': {'clientWidth': 1000, 'clientHeight': 600}, 'prepared_at': probe.time.monotonic()}
    monkeypatch.setattr(probe, '_reference', ref)
    monkeypatch.setattr(probe, '_state', {'state': 'idle'})
    monkeypatch.setattr(probe, '_cancel', probe.threading.Event())
    return ref


@pytest.mark.parametrize('mode,expected', [('background', [10, 10]), ('screenoff', [3, 10, 10])])
def test_short_modes_single_click_and_no_long_wait(reference, monkeypatch, tmp_path, mode, expected):
    waits, clicks, off = [], [], []
    class Cancel:
        def wait(self, seconds):
            waits.append(seconds)
            return False
        def is_set(self):
            return False
    monkeypatch.setattr(probe, '_cancel', Cancel())
    monkeypatch.setattr(probe.frames, 'JP_DATA_DIR', tmp_path)
    monkeypatch.setattr(probe, 'turn_display_off', lambda: off.append(True))
    monkeypatch.setattr(probe, 'guarded_click', lambda *args: clicks.append(True))
    monkeypatch.setattr(probe, 'capture', lambda target: ({}, '', {'digest': 'changed'}, {}))
    probe._run(reference, .5, .5, mode=mode)
    assert waits == expected and clicks == [True]
    assert len(off) == int(mode == 'screenoff')
    assert (tmp_path / 'debug' / 'click_probe.json').exists()


def test_step_error_is_redacted_and_has_duration(reference):
    def fail():
        raise TimeoutError('private url and credentials')
    with pytest.raises(TimeoutError):
        probe.step('读取画面尺寸', fail)
    result = probe.status()
    assert result['failed_stage'] == '读取画面尺寸'
    assert result['error_type'] == 'TimeoutError'
    assert result['steps'][0]['seconds'] >= 0
    assert 'private' not in str(result)


@pytest.mark.parametrize('x,y', [(0, .5), (1, .5), (-1, .5), (True, .5), (.5, float('nan'))])
def test_invalid_point_never_starts(reference, x, y):
    with pytest.raises(ValueError):
        probe.start(x, y)
    assert probe.status()['state'] == 'idle'


def test_changed_screen_never_connects_input(reference, monkeypatch):
    monkeypatch.setattr(probe, 'capture', lambda target: ({}, '', {'digest': 'changed'}, reference['view']))
    with pytest.raises(ValueError, match='已跳过'):
        probe.guarded_click(reference, .5, .5)


def test_single_click_maps_image_point_to_css_viewport(reference, monkeypatch):
    monkeypatch.setattr(probe, 'capture', lambda target: (
        {'webSocketDebuggerUrl': 'test'}, '', reference['summary'], reference['view']))
    calls = []
    class Socket:
        def __init__(self, url):
            assert url == 'test'
        def call(self, method, params):
            calls.append((method, params))
        def close(self):
            pass
    monkeypatch.setattr(probe.jp_listener, '_CdpSocket', Socket)
    probe.guarded_click(reference, .8, .4)
    assert [params['type'] for _, params in calls] == ['mouseMoved', 'mousePressed', 'mouseReleased']
    assert [params['buttons'] for _, params in calls] == [0, 1, 0]
    assert all(params['x'] == 800 and params['y'] == 240 for _, params in calls)
    assert all(method == 'Input.dispatchMouseEvent' for method, _ in calls)


def test_cancel_before_click_has_no_input(reference, monkeypatch):
    probe._cancel.set()
    monkeypatch.setattr(probe, 'guarded_click', lambda *args: pytest.fail('cancelled input'))
    probe._run(reference, .5, .5)
    assert probe.status()['state'] == 'cancelled'


def test_press_failure_still_releases_button(reference, monkeypatch):
    monkeypatch.setattr(probe, 'capture', lambda target: (
        {'webSocketDebuggerUrl': 'test'}, '', reference['summary'], reference['view']))
    calls = []
    class Socket:
        def __init__(self, url):
            pass
        def call(self, method, params):
            calls.append(params['type'])
            if params['type'] == 'mousePressed':
                raise TimeoutError()
        def close(self):
            pass
    monkeypatch.setattr(probe.jp_listener, '_CdpSocket', Socket)
    with pytest.raises(TimeoutError):
        probe.guarded_click(reference, .5, .5)
    assert calls == ['mouseMoved', 'mousePressed', 'mouseReleased']


def test_duplicate_start_rejected_and_no_success_claim(reference, monkeypatch):
    monkeypatch.setattr(probe.threading.Thread, 'start', lambda self: None)
    result = probe.start(.5, .5)
    assert not result['click_sent'] and not result['formation_verified']
    assert not result['screen_off_verified']
    with pytest.raises(ValueError):
        probe.start(.5, .5)


def test_prepare_timeout_is_player_readable(monkeypatch):
    from fastapi.testclient import TestClient
    from panel.server import app
    def timeout():
        raise TimeoutError('private diagnostic details')
    monkeypatch.setattr(probe, 'prepare', timeout)
    result = TestClient(app).post('/api/jp-click-probe/prepare')
    assert result.status_code == 400
    assert '读取超时' in result.json()['detail']
    assert 'private' not in result.text


def test_immediate_mode_does_not_wait_six_minutes(reference, monkeypatch):
    waits = []
    class Cancel:
        def wait(self, seconds):
            waits.append(seconds)
            return False
        def is_set(self):
            return False
    monkeypatch.setattr(probe, '_cancel', Cancel())
    clicks = []
    monkeypatch.setattr(probe, 'guarded_click', lambda *args: clicks.append(args))
    monkeypatch.setattr(probe, 'capture', lambda target: ({}, '', {'digest': 'changed'}, {}))
    probe._run(reference, .5, .5, immediate=True)
    assert waits == [0, 10] and len(clicks) == 1
    assert probe.status()['click_sent'] and probe.status()['picture_changed']
    assert '请确认' in probe.status()['detail']


def test_immediate_endpoint_selects_immediate_mode(monkeypatch):
    from fastapi.testclient import TestClient
    from panel.server import app
    calls = []
    monkeypatch.setattr(probe, 'start', lambda x, y, immediate=False: calls.append(immediate) or {'state': 'running'})
    assert TestClient(app).post('/api/jp-click-probe/immediate', json={'x': .5, 'y': .5}).status_code == 200
    assert calls == [True]
