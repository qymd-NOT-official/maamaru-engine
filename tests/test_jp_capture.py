import pytest

from touken import jp_browser, jp_capture


class FakeBrowser:
    def __init__(self):
        self.base_view = {'clientWidth': 1280, 'clientHeight': 720}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def screenshot(self):
        from PIL import Image
        return Image.new('RGB', (1280, 720), (70, 80, 90))


def test_capture_saves_labeled_frame(monkeypatch, tmp_path):
    monkeypatch.setattr(jp_capture.jp_browser, 'JpBrowser', FakeBrowser)
    monkeypatch.setattr(jp_capture.frames, 'JP_DATA_DIR', tmp_path)
    result = jp_capture.capture('home')
    assert result['width'] == 1280 and not result['near_black']
    saved = list((tmp_path / 'debug' / 'frames').glob('home-*.png'))
    assert len(saved) == 1 and result['path'] == str(saved[0])


def test_capture_rejects_pathy_label(monkeypatch):
    monkeypatch.setattr(jp_capture.jp_browser, 'JpBrowser', FakeBrowser)
    with pytest.raises(ValueError):
        jp_capture.capture('../escape')


def test_capture_surfaces_browser_error(monkeypatch, tmp_path):
    class Dead(FakeBrowser):
        def __enter__(self):
            raise jp_browser.BrowserError('未找到日服游戏页面，请从まあ丸打开游戏并进入本丸')

    monkeypatch.setattr(jp_capture.jp_browser, 'JpBrowser', Dead)
    monkeypatch.setattr(jp_capture.frames, 'JP_DATA_DIR', tmp_path)
    with pytest.raises(jp_browser.BrowserError, match='未找到'):
        jp_capture.capture('home')
    assert not list(tmp_path.rglob('*.png'))
