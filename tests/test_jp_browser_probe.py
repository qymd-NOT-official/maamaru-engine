import base64
import io

from PIL import Image
import pytest
from touken import jp_browser_probe as probe


def encoded(color):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 6), color).save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()


def test_frame_summary_distinguishes_black_and_changes():
    black = probe.frame_summary(encoded('black'))
    white = probe.frame_summary(encoded('white'))
    assert black['near_black'] and not white['near_black']
    assert black['digest'] != white['digest']
    assert white['width'] == 8 and white['height'] == 6
    assert probe.frame_summary(encoded('white')) == white


def test_missing_game_does_not_connect_or_launch(monkeypatch):
    monkeypatch.setattr(probe.jp_listener, '_http_json', lambda path: [
        {'type': 'page', 'url': 'https://example.com/play/tohken'}])
    with pytest.raises(RuntimeError):
        probe.sample()


@pytest.mark.parametrize('host,path', [
    ('pc-play.games.dmm.com', '/play/tohken'),
    ('play.games.dmm.com', '/game/tohken'),
    ('play.games.dmm.com', '/game/tohken/'),
])
def test_game_page_redirect_is_accepted(host, path):
    assert probe.is_game_page({'type': 'page', 'url': f'https://{host}{path}?test=1'})


@pytest.mark.parametrize('url', [
    'https://example.com/game/tohken', 'https://play.games.dmm.com/game/other',
    'http://play.games.dmm.com/game/tohken', 'https://play.games.dmm.com.evil.test/game/tohken',
])
def test_other_pages_are_rejected(url):
    assert not probe.is_game_page({'type': 'page', 'url': url})


def test_sample_uses_redirected_game_and_closes_connection(monkeypatch):
    monkeypatch.setattr(probe.jp_listener, '_http_json', lambda path: [{
        'type': 'page', 'url': 'https://play.games.dmm.com/game/tohken',
        'webSocketDebuggerUrl': 'local-test'}])
    calls = []
    class Socket:
        def __init__(self, url):
            assert url == 'local-test'
        def call(self, method, params, timeout):
            calls.append(method)
            return {'data': encoded('white')}
        def close(self):
            calls.append('closed')
    monkeypatch.setattr(probe.jp_listener, '_CdpSocket', Socket)
    assert not probe.sample()['near_black']
    assert calls == ['Page.captureScreenshot', 'closed']


def test_report_has_no_image_or_automatic_success_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, '_state', {'state': 'idle'})
    monkeypatch.setattr(probe, 'JP_DATA_DIR', tmp_path)
    monkeypatch.setattr(probe.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(probe, 'sample', lambda: {'digest': 'same', 'near_black': False})
    monkeypatch.setattr(probe.threading.Thread, 'start', lambda self: None)
    probe.start()
    probe._run()
    result = probe.status()
    assert result['state'] == 'done' and result['successful_frames'] == 43
    assert result['changed_frames'] == 0 and not result['input_verified']
    assert not result['screen_off_verified']
    report = (tmp_path / 'debug/browser_probe.json').read_text(encoding='utf-8')
    assert 'digest' not in report and 'data:image' not in report
