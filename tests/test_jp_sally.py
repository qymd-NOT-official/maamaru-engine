import pytest
from PIL import Image

from touken import jp_sally, jp_vision

# 各模板在 1508x1088 基准帧上的实测中心（标定帧模板匹配结果）
KNOWN = {
    'home/badge': (0.062, 0.129),
    'menu/sally': (0.845, 0.709),
    'era/decide': (0.909, 0.726),
    'map/team-select': (0.908, 0.728),
    'team/depart': (0.905, 0.714),
    'battle/result': (0.500, 0.108),
    'march/advance': (0.852, 0.679),
    'battle/start': (0.500, 0.452),
    'map/label': (0.906, 0.256),
    'loading/sakura': (0.057, 0.761),
    'dialog/yes': (0.600, 0.601),
    'dialog/no': (0.388, 0.601),
    'dialog/automarch-request': (0.500, 0.154),
}

pytestmark = pytest.mark.skipif(
    not jp_vision.TEMPLATE_DIR.is_dir(), reason='日服模板目录不存在')


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(jp_vision, '_baseline_path', lambda: tmp_path / 'vision.json')
    monkeypatch.setattr(jp_vision, 'base_size', lambda: (1508, 1088))
    monkeypatch.setattr(jp_sally.time, 'sleep', lambda s: None)


def fake_screen(*names):
    image = Image.new('RGB', (1508, 1088), (60, 60, 70))
    for name in names:
        template = jp_vision.load_template(name)
        nx, ny = KNOWN[name]
        image.paste(template, (int(nx * 1508 - template.width / 2),
                               int(ny * 1088 - template.height / 2)))
    return image


class FakeBrowser:
    def __init__(self, frames):
        self.frames = list(frames)
        self.clicks = []

    def screenshot(self):
        if self.frames:
            self.last = self.frames.pop(0)
        return self.last

    def click(self, nx, ny):
        self.clicks.append((round(nx, 3), round(ny, 3)))


def make_sally(browser, **kwargs):
    logs = []
    sally = jp_sally.Sally(browser, log=logs.append, poll=0.01,
                           save_debug=lambda img: None, **kwargs)
    return sally, logs


def happy_frames():
    return [
        fake_screen('home/badge'),                 # 回本丸
        fake_screen('menu/sally'),                 # 指挥菜单
        fake_screen('menu/sally'),                 # _click_hit 取帧
        fake_screen('era/decide'),
        fake_screen('era/decide'),
        fake_screen('map/team-select'),
        fake_screen('map/team-select'),
        fake_screen('team/depart'),
        fake_screen('dialog/automarch-request'),   # 自动行军弹窗
        fake_screen('team/depart'),                # 弹窗关闭回部队页
        fake_screen('team/depart'),                # 出阵按钮取帧
        fake_screen('loading/sakura'),
        fake_screen('battle/start'),
        fake_screen('battle/result'),
        fake_screen('march/advance'),
        fake_screen('home/badge'),                 # 回城
    ]


def test_happy_path_single_round():
    browser = FakeBrowser(happy_frames())
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1)
    assert any('出阵完成' in line for line in logs)
    # 关键点击：指挥标签、出阵入口、决定、部队选择、部队一、自动行军、依頼、关窗、出发
    assert (0.94, 0.728) in browser.clicks                    # 指揮标签
    assert jp_sally.TEAM_TABS[1] in browser.clicks
    assert jp_sally.AUTOMARCH_BUTTON in browser.clicks
    assert jp_sally.AUTOMARCH_CONFIRM in browser.clicks
    assert jp_sally.DIALOG_CLOSE in browser.clicks
    depart = [c for c in browser.clicks if abs(c[0] - 0.905) < 0.01 and abs(c[1] - 0.714) < 0.01]
    assert depart, '出阵按钮没被点'


def test_menu_already_open_skips_shiki_tab():
    frames = happy_frames()[1:]  # 指挥菜单已开着（上次中断留下的现场）
    browser = FakeBrowser(frames)
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1)
    assert (0.94, 0.728) not in browser.clicks  # 没再点指揮（会把菜单关上）
    assert any('出阵完成' in line for line in logs)


def test_dialog_during_march_stops_everything():
    frames = happy_frames()[:11] + [fake_screen('dialog/yes', 'dialog/no')]
    browser = FakeBrowser(frames)
    sally, logs = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='弹窗'):
        sally.run(team_no=2, rounds=1)
    # 弹窗之后绝不再有点击（出发是行军前的最后一下）
    assert len(browser.clicks) == 9


def test_unknown_screen_in_navigation_stops():
    browser = FakeBrowser([Image.new('RGB', (1508, 1088), (10, 200, 10))])
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='不认识'):
        sally.run(team_no=1, rounds=1)
    assert browser.clicks == []  # 什么都还没点


def test_stuck_march_blows_fuse():
    weird = Image.new('RGB', (1508, 1088), (10, 200, 10))
    browser = FakeBrowser([weird])
    sally, _ = make_sally(browser, stuck_seconds=0.05)
    with pytest.raises(jp_sally.SallyStop, match='没有变化'):
        sally._watch_march()


def test_near_black_counts_as_loading_then_timeout():
    black = Image.new('RGB', (1508, 1088), (0, 0, 0))
    browser = FakeBrowser([black])
    sally, _ = make_sally(browser)
    sally.stuck_seconds = 999
    with pytest.raises(jp_sally.SallyStop, match='超时'):
        sally._wait(frozenset({'home'}), timeout=0.1,
                    passable=frozenset({'loading'}), what='测试')


@pytest.mark.parametrize('team_no', [0, 6, '二'])
def test_bad_team_rejected(team_no):
    sally, _ = make_sally(FakeBrowser([]))
    with pytest.raises((ValueError, KeyError, TypeError)):
        sally.run(team_no=team_no, rounds=1)


def test_rounds_must_be_sane():
    sally, _ = make_sally(FakeBrowser([]))
    with pytest.raises(ValueError):
        sally.run(team_no=1, rounds=0)
    with pytest.raises(ValueError):
        sally.run(team_no=1, rounds=201)
