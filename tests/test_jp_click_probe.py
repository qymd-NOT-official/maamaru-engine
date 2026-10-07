import pytest
from touken import jp_click_probe as probe


@pytest.fixture
def reference(monkeypatch):
    ref = {'target_id': 'game', 'summary': {'digest': 'same', 'near_black': False},
           'view': {'clientWidth': 1000, 'clientHeight': 600}, 'prepared_at': probe.time.monotonic()}
    monkeypatch.setattr(probe, '_reference', ref)
    monkeypatch.setattr(probe, '_state', {'state': 'idle'})
    monkeypatch.setattr(probe, '_cancel', probe.threading.Event())
    return ref


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
    assert [params['type'] for _, params in calls] == ['mousePressed', 'mouseReleased']
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
    assert calls == ['mousePressed', 'mouseReleased']


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
