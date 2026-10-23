import pytest
from PIL import Image

from touken import jp_sally, jp_vision

# 各模板在 1508x1088 基准帧上的实测中心（标定帧模板匹配结果）
KNOWN = {
    'home/badge': (0.062, 0.129),
    'home/catalog': (0.926, 0.110),
    'directory/sally': (0.191, 0.247),
    'menu/shiki-tab': (0.939, 0.726),
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
    'boot/volume-arrow': (0.496, 0.681),
    'boot/honmaru-button': (0.492, 0.731),
    'formation/label': (0.060, 0.134),
    'notice/close': (0.833, 0.164),          # 公告栏右上 ✕（采自 944 高窗口帧）
    'notice/hide-forever': (0.847, 0.658),   # 公告栏右侧「今後表示しない」绿条
    'dialog/decline': (0.385, 0.819),        # 确认窗 いいえ（采自 944 高窗口帧）
    'dialog/accept': (0.597, 0.831),         # 确认窗 はい
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


class FakeDevice:
    """JpGameDevice 形状：screenshot(force) + click(Point) + reload/relaxed_view。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.clicks = []          # 基准空间像素 (x, y)
        self.reloads = 0

    def screenshot(self, force=False):
        if self.frames:
            self.last = self.frames.pop(0)
        return self.last

    def click(self, point):
        self.clicks.append((point.x, point.y))
        return True

    def reload(self):
        self.reloads += 1

    def relaxed_view(self):
        import contextlib
        return contextlib.nullcontext()


def make_sally(device, **kwargs):
    logs = []
    sally = jp_sally.Sally(device, log=logs.append, poll=0.01,
                           save_debug=lambda img: None, **kwargs)
    return sally, logs


def happy_frames():
    """一轮完整出阵的帧序列。导航点击复用分类帧不额外取帧；部队页签/
    自动行军/弹窗按钮/出发按钮各重新取一帧。"""
    home = fake_screen('home/badge', 'home/catalog')
    team = fake_screen('team/depart')
    dialog = fake_screen('dialog/automarch-request')
    return [
        home,                            # classify → 本丸，点目录按钮
        fake_screen('directory/sally'),  # 目录大页，点出阵入口
        fake_screen('era/decide'),
        fake_screen('map/team-select'),
        team,                            # classify → 部队选择页
        team,                            # 选部队页签取帧
        team,                            # 点自动行军取帧
        dialog,                          # _wait 命中弹窗
        dialog,                          # 依頼する 取帧
        dialog,                          # 关弹窗 ✕ 取帧
        team,                            # _wait 回部队页
        team,                            # 出阵按钮取帧
        fake_screen('loading/sakura'),
        fake_screen('battle/start'),
        fake_screen('battle/result'),
        fake_screen('march/advance'),
        fake_screen('home/badge'),       # 回城
    ]


def click_near(clicks, x, y, tol=12):
    return any(abs(c[0] - x) < tol and abs(c[1] - y) < tol for c in clicks)


# 锚点 + 偏移算出的期望点击位置（基准空间像素 = 归一化 × (1508, 1088) + 偏移）
def _px(name):
    return (KNOWN[name][0] * 1508, KNOWN[name][1] * 1088)


CATALOG_CLICK = _px('home/catalog')
DIR_SALLY_CLICK = _px('directory/sally')
MENU_SALLY_CLICK = _px('menu/sally')
TAB1_CLICK = (0.905 * 1508 + jp_sally.TEAM_TAB_OFFSETS[1][0],
              0.714 * 1088 + jp_sally.TEAM_TAB_OFFSETS[1][1])
AUTOMARCH_CLICK = (0.905 * 1508 + jp_sally.AUTOMARCH_OFFSET[0],
                   0.714 * 1088 + jp_sally.AUTOMARCH_OFFSET[1])
CONFIRM_CLICK = (0.500 * 1508 + jp_sally.AUTOMARCH_CONFIRM_OFFSET[0],
                 0.154 * 1088 + jp_sally.AUTOMARCH_CONFIRM_OFFSET[1])
CLOSE_CLICK = (0.500 * 1508 + jp_sally.DIALOG_CLOSE_OFFSET[0],
               0.154 * 1088 + jp_sally.DIALOG_CLOSE_OFFSET[1])
BACK_CLICK = (0.060 * 1508 + jp_sally.BACK_ARROW_OFFSET[0],
              0.134 * 1088 + jp_sally.BACK_ARROW_OFFSET[1])


def test_happy_path_single_round():
    browser = FakeDevice(happy_frames())
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1)
    assert any('出阵完成' in line for line in logs)
    # 关键点击：目录按钮、目录出阵入口、部队一页签、自动行军、依頼、关窗、出发
    assert click_near(browser.clicks, *CATALOG_CLICK)
    assert click_near(browser.clicks, *DIR_SALLY_CLICK)
    assert click_near(browser.clicks, *TAB1_CLICK)
    assert click_near(browser.clicks, *AUTOMARCH_CLICK)
    assert click_near(browser.clicks, *CONFIRM_CLICK)
    assert click_near(browser.clicks, *CLOSE_CLICK)
    depart = [c for c in browser.clicks
              if abs(c[0] - 0.905 * 1508) < 12 and abs(c[1] - 0.714 * 1088) < 12]
    assert depart, '出阵按钮没被点'


def test_menu_already_open_used_directly():
    """指挥菜单已被手动开着（上次中断留下的现场）：直接用，不走目录。"""
    frames = [fake_screen('menu/sally')  # 菜单已开，直接点出阵入口
              ] + happy_frames()[2:]
    browser = FakeDevice(frames)
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1)
    assert not click_near(browser.clicks, *CATALOG_CLICK)  # 没绕道目录
    assert click_near(browser.clicks, *MENU_SALLY_CLICK)
    assert any('出阵完成' in line for line in logs)


def test_navigation_continues_from_formation():
    """停留在编成页：点左上 ↩ 回本丸再接着走。"""
    frames = [fake_screen('formation/label'),   # 编成页
              ] + happy_frames()
    browser = FakeDevice(frames)
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1)
    assert click_near(browser.clicks[:1], *BACK_CLICK)
    assert any('出阵完成' in line for line in logs)


def test_dialog_during_march_stops_everything():
    frames = happy_frames()[:12] + [fake_screen('dialog/yes', 'dialog/no')]
    browser = FakeDevice(frames)
    sally, logs = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='弹窗'):
        sally.run(team_no=2, rounds=1)
    # 弹窗之后绝不再有点击（出发是行军前的最后一下）
    assert len(browser.clicks) == 9


def test_unknown_screen_mid_navigation_stops(monkeypatch):
    """起手认得、点了东西之后才撞见未知画面：立即安全停车，不刷新。"""
    monkeypatch.setattr(jp_sally, 'UNKNOWN_STABLE', -1.0)
    weird = Image.new('RGB', (1508, 1088), (10, 200, 10))
    frames = [fake_screen('home/badge', 'home/catalog'), weird]
    browser = FakeDevice(frames)
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='不认识'):
        sally.run(team_no=1, rounds=1)
    assert browser.reloads == 0


def test_unknown_at_start_reloads_once(monkeypatch):
    """起手就认不出（如登录问候残留）：等一拍后刷新一次，还不认识就停车。"""
    monkeypatch.setattr(jp_sally, 'UNKNOWN_STABLE', -1.0)
    monkeypatch.setattr(jp_sally, 'BOOT_TIMEOUT', 0.05)
    weird = Image.new('RGB', (1508, 1088), (10, 200, 10))
    browser = FakeDevice([weird])
    sally, logs = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop):
        sally.run(team_no=1, rounds=1)
    assert browser.reloads == 1
    assert any('刷新一次页面重来' in line for line in logs)
    assert browser.clicks == []  # 直到停车都没盲点过


def test_stuck_march_blows_fuse():
    weird = Image.new('RGB', (1508, 1088), (10, 200, 10))
    browser = FakeDevice([weird])
    sally, _ = make_sally(browser, stuck_seconds=0.05)
    with pytest.raises(jp_sally.SallyStop, match='没有变化'):
        sally._watch_march()


def test_near_black_counts_as_loading_then_timeout():
    black = Image.new('RGB', (1508, 1088), (0, 0, 0))
    browser = FakeDevice([black])
    sally, _ = make_sally(browser)
    sally.stuck_seconds = 999
    with pytest.raises(jp_sally.SallyStop, match='超时'):
        sally._wait(frozenset({'home'}), timeout=0.1,
                    passable=frozenset({'loading'}), what='测试')


def greeting_frame():
    """登录问候：近侍全身像无 UI——没有可匹配元素，用纯色不暗帧充当。"""
    return Image.new('RGB', (1508, 1088), (60, 60, 70))


def notice_frame():
    """公告栏：右上 ✕ + 右侧绿条都中才算。"""
    return fake_screen('notice/close', 'notice/hide-forever')


NOTICE_CLOSE_CLICK = _px('notice/close')


def test_notice_board_closed_during_navigation():
    """公告栏挡在导航路上：认出后点 ✕ 关掉，照常出阵。"""
    browser = FakeDevice([notice_frame()] + happy_frames())
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1)
    assert click_near(browser.clicks, *NOTICE_CLOSE_CLICK)
    assert any('公告栏' in line for line in logs)
    assert any('出阵完成' in line for line in logs)


def test_notice_board_during_boot_gets_closed(monkeypatch):
    """启动序列撞上公告栏：点 ✕ 关掉，不影响回本丸。"""
    monkeypatch.setattr(jp_sally, 'GREET_STABLE', -1.0)
    frames = [
        fake_screen('boot/volume-arrow'),
        fake_screen('boot/honmaru-button'),
        greeting_frame(), greeting_frame(),    # 登录问候（unknown 稳定后点中央）
        notice_frame(),                        # 公告栏
        fake_screen('home/badge'),
    ]
    browser = FakeDevice(frames)
    sally, logs = make_sally(browser)
    sally._boot_to_home()
    assert click_near(browser.clicks, *NOTICE_CLOSE_CLICK)
    assert any('重新进入本丸' in line for line in logs)


def test_notice_during_march_still_stops():
    """行军途中弹出公告栏：不在白名单里，安全停车，绝不盲点。"""
    frames = happy_frames()[:12] + [notice_frame()]
    browser = FakeDevice(frames)
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='不认识'):
        sally.run(team_no=1, rounds=1)
    assert not click_near(browser.clicks, *NOTICE_CLOSE_CLICK)


def confirm_frame():
    """内番续班等确认窗：いいえ + はい 都中才算。"""
    return fake_screen('dialog/decline', 'dialog/accept')


DECLINE_CLICK = _px('dialog/decline')
ACCEPT_CLICK = _px('dialog/accept')


def test_confirm_dialog_declined_during_navigation():
    """导航路上撞见确认窗：默认点 いいえ 拒绝，照常出阵；绝不碰 はい。"""
    browser = FakeDevice([confirm_frame()] + happy_frames())
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1)
    assert click_near(browser.clicks, *DECLINE_CLICK)
    assert not click_near(browser.clicks, *ACCEPT_CLICK)
    assert any('确认窗默认选否' in line for line in logs)
    assert any('出阵完成' in line for line in logs)


def test_confirm_dialog_during_boot_gets_declined(monkeypatch):
    """启动序列撞上确认窗：点 いいえ 拒掉，不影响回本丸。"""
    monkeypatch.setattr(jp_sally, 'GREET_STABLE', -1.0)
    frames = [
        fake_screen('boot/volume-arrow'),
        fake_screen('boot/honmaru-button'),
        greeting_frame(), greeting_frame(),    # 登录问候（unknown 稳定后点中央）
        confirm_frame(),                       # 确认窗
        fake_screen('home/badge'),
    ]
    browser = FakeDevice(frames)
    sally, logs = make_sally(browser)
    sally._boot_to_home()
    assert click_near(browser.clicks, *DECLINE_CLICK)
    assert not click_near(browser.clicks, *ACCEPT_CLICK)
    assert any('重新进入本丸' in line for line in logs)


def test_confirm_during_march_still_stops():
    """行军途中弹出确认窗：不在白名单里，安全停车，两个按钮都不点。"""
    frames = happy_frames()[:12] + [confirm_frame()]
    browser = FakeDevice(frames)
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='不认识'):
        sally.run(team_no=1, rounds=1)
    assert not click_near(browser.clicks, *DECLINE_CLICK)
    assert not click_near(browser.clicks, *ACCEPT_CLICK)


# ---- 出发前队伍状态门闩（jp_party.PartyWatch） ----

import time as _time

from touken import jp_party


def make_payload(members, status=1, team_no=1):
    """合成 /sally 响应：members = [(slot, serial_id, sword_id, hp, hp_max)]"""
    slot = {str(i): {'serial_id': None} for i in range(1, 7)}
    sword = {}
    for slot_no, serial, sword_id, hp, hp_max in members:
        slot[str(slot_no)] = {'serial_id': serial}
        sword[str(serial)] = {'serial_id': serial, 'sword_id': sword_id,
                              'hp': hp, 'hp_max': hp_max, 'fatigue': 80}
    return {'party': {str(team_no): {'party_no': team_no, 'status': status,
                                     'slot': slot}},
            'sword': sword}


HEALTHY_TEAM = [(1, 101, 3, 56, 56), (2, 102, 5, 33, 33)]
DEPART_CLICK = _px('team/depart')


class FakeWatch:
    def __init__(self, payload=None, age=0.0):
        self._latest = (None if payload is None
                        else (_time.time() - age, payload))

    def latest(self):
        return self._latest


def test_heavy_injury_blocks_departure():
    """队伍里有重伤：出发前拦下，出发按钮绝不点。"""
    payload = make_payload(HEALTHY_TEAM + [(3, 103, 7, 10, 56)])  # 重伤
    browser = FakeDevice(happy_frames())
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='重伤'):
        sally.run(team_no=1, rounds=1, party_watch=FakeWatch(payload))
    assert not click_near(browser.clicks, *DEPART_CLICK)


def test_medium_injury_logs_and_departs():
    """中伤不拦：日志点名带伤出发，流程照常走完。"""
    payload = make_payload(HEALTHY_TEAM + [(3, 103, 7, 20, 56)])  # 中伤
    browser = FakeDevice(happy_frames())
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1, party_watch=FakeWatch(payload))
    assert any('带伤出发' in line and '中伤' in line for line in logs)
    assert any('出阵完成' in line for line in logs)
    assert click_near(browser.clicks, *DEPART_CLICK)


def test_stale_or_missing_party_data_blocks(monkeypatch):
    """没收到队伍数据/数据过期：刷新重拿一次仍没有，宁可停也不带盲区放行。"""
    monkeypatch.setattr(jp_sally, 'PARTY_DATA_WAIT', 0.05)
    monkeypatch.setattr(jp_sally.Sally, '_boot_to_home', lambda self: None)

    browser = FakeDevice(happy_frames())
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='没能确认部队状态'):
        sally.run(team_no=1, rounds=1, party_watch=FakeWatch(None))
    assert browser.reloads == 1            # 试过刷新重拿，不是直接放弃

    payload = make_payload(HEALTHY_TEAM)
    browser = FakeDevice(happy_frames())
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='没能确认部队状态'):
        sally.run(team_no=1, rounds=1,
                  party_watch=FakeWatch(payload, age=9999))
    assert browser.reloads == 1


def test_team_away_blocks_departure():
    """队伍在远征/出阵中（status != 1）：拦下，不盲点出发。"""
    payload = make_payload(HEALTHY_TEAM, status=2)
    browser = FakeDevice(happy_frames())
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop, match='待命'):
        sally.run(team_no=1, rounds=1, party_watch=FakeWatch(payload))
    assert not click_near(browser.clicks, *DEPART_CLICK)


def test_black_loading_too_long_reloads_and_boots(monkeypatch):
    """黑屏加载卡死 → 自动刷新一次 → 启动序列（音量/标题/问候）→ 回本丸出阵。"""
    monkeypatch.setattr(jp_sally, 'RELOAD_AFTER', -1.0)   # 第二帧黑屏即触发
    monkeypatch.setattr(jp_sally, 'GREET_STABLE', -1.0)   # 问候稳定判定立即满足
    black = Image.new('RGB', (1508, 1088), (0, 0, 0))
    frames = [
        black, black,                          # 持续黑屏 → 刷新
        fake_screen('boot/volume-arrow'),      # 启动：音量注意
        fake_screen('boot/honmaru-button'),    # 启动：标题页
        greeting_frame(), greeting_frame(),    # 登录问候（unknown 稳定后点中央）
        fake_screen('home/badge'),             # 回到本丸
    ] + happy_frames()[1:]                     # 之后照常走到出阵
    browser = FakeDevice(frames)
    sally, logs = make_sally(browser)
    sally.run(team_no=1, rounds=1)
    assert browser.reloads == 1
    assert any('自动刷新' in line for line in logs)
    assert any('跳过登录问候' in line for line in logs)
    assert (754, 544) in browser.clicks      # 问候屏基准空间中央
    assert any('出阵完成' in line for line in logs)


def test_reload_only_once_per_navigation(monkeypatch):
    """刷新一次后仍持续黑屏 → 不再刷第二次，超时安全停车。"""
    monkeypatch.setattr(jp_sally, 'RELOAD_AFTER', -1.0)
    monkeypatch.setattr(jp_sally, 'BOOT_TIMEOUT', 0.05)
    black = Image.new('RGB', (1508, 1088), (0, 0, 0))
    browser = FakeDevice([black])             # 永远黑屏
    sally, _ = make_sally(browser)
    with pytest.raises(jp_sally.SallyStop):
        sally.run(team_no=1, rounds=1)
    assert browser.reloads == 1
    assert browser.clicks == []


def test_boot_ignores_stale_home_frame(monkeypatch):
    """刷新后的前几帧可能还是旧本丸画面——必须等它消失再认「到家」。"""
    monkeypatch.setattr(jp_sally, 'GREET_STABLE', -1.0)
    frames = [
        fake_screen('home/badge'),             # 刷新前的残留帧
        fake_screen('boot/volume-arrow'),
        fake_screen('boot/honmaru-button'),
        fake_screen('home/badge'),             # 真正回到本丸
    ]
    browser = FakeDevice(frames)
    sally, logs = make_sally(browser)
    sally._boot_to_home()
    # 残留帧没骗过它：箭头与本丸按钮都被点过
    assert len(browser.clicks) == 2
    assert any('重新进入本丸' in line for line in logs)


def test_run_script_success_path(monkeypatch):
    """run_script 成功跑完一轮：不抛错、产出空日志迭代器（日志走 print）。"""
    fake = FakeDevice(happy_frames())

    class FakeGameDevice:
        def __enter__(self):
            return fake

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(jp_sally.jp_browser, 'JpBrowser', lambda: object())
    monkeypatch.setattr(jp_sally.jp_game_device, 'JpGameDevice',
                        lambda browser: FakeGameDevice())

    class FakePartyWatch:
        def __enter__(self):
            return FakeWatch(make_payload(HEALTHY_TEAM))

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(jp_sally.jp_party, 'PartyWatch', lambda: FakePartyWatch())
    lines = list(jp_sally.run_script(params={'team_no': 1, 'rounds': 1}))
    assert lines == []
    assert any(abs(c[0] - 0.905 * 1508) < 12 for c in fake.clicks)  # 出发键按过


@pytest.mark.parametrize('team_no', [0, 6, '二'])
def test_bad_team_rejected(team_no):
    sally, _ = make_sally(FakeDevice([]))
    with pytest.raises((ValueError, KeyError, TypeError)):
        sally.run(team_no=team_no, rounds=1)


def test_rounds_must_be_sane():
    sally, _ = make_sally(FakeDevice([]))
    with pytest.raises(ValueError):
        sally.run(team_no=1, rounds=0)
    with pytest.raises(ValueError):
        sally.run(team_no=1, rounds=201)
