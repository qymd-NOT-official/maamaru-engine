# -*- coding: utf-8 -*-
"""日服点击出阵（sally）：画面识别 + 点击的状态机，无 OCR。

安全底线（复刻国服 _safe_depart_stream 的安全语义）：

- 只在认得出的画面上点击；认不出就停，绝不盲点。
- 行军中画面长时间不变且没回本丸 → 熔断（卡住比点错安全）。
- 行军途中出现任何弹窗一律停——重伤警告就是弹窗，绝不点「继续行军」。
- 只打已攻略的图 + 自动行军：不开自动行军的出阵直接拒绝。
"""
from __future__ import annotations

import hashlib
import time

from . import jp_browser, jp_browser_probe as frames, jp_vision

SHIKI_TAB = (0.940, 0.728)          # 本丸右侧「指揮」拉页
TEAM_TABS = {1: (0.142, 0.168), 2: (0.229, 0.168), 3: (0.316, 0.168),
             4: (0.403, 0.168), 5: (0.490, 0.168)}
AUTOMARCH_BUTTON = (0.907, 0.490)   # 部队选择页「自動行軍」
AUTOMARCH_CONFIRM = (0.600, 0.687)  # 自动行军弹窗「依頼する」
DIALOG_CLOSE = (0.788, 0.116)       # 弹窗右上 ✕

NAV_TIMEOUT = 25.0                  # 每步导航认画面的上限
MARCH_TIMEOUT = 1500.0              # 一轮行军总上限
STUCK_SECONDS = 120.0               # 画面纹丝不动的熔断线

# 画面分类：按优先级从上到下
# 注意 shiki_menu 必须排在 home 前面：指挥菜单是盖在本丸上的浮层，
# 本丸绿标依然可见，先认 home 会永远看不见菜单。
SCREENS = (
    ('loading', ('loading/sakura',)),
    ('automarch_dialog', ('dialog/automarch-request',)),
    ('dialog', ('dialog/yes', 'dialog/no')),      # 两个按钮都中才算弹窗
    ('shiki_menu', ('menu/sally',)),
    ('era', ('era/decide',)),
    ('map', ('map/team-select',)),
    ('team', ('team/depart',)),
    ('result', ('battle/result',)),
    ('field', ('march/advance',)),
    ('battle', ('battle/start',)),
    ('mapview', ('map/label',)),
    ('home', ('home/badge',)),
)
# 行军途中允许路过的画面；弹窗和回家单独处理。team 是出发按钮点完后的残留帧。
MARCH_PASSABLE = frozenset(
    {'loading', 'battle', 'result', 'field', 'mapview', 'team', 'unknown'})


class SallyStop(RuntimeError):
    """出阵中止；文案面向玩家。"""


def _digest(image) -> str:
    return hashlib.sha256(image.tobytes()).hexdigest()


class Sally:
    """一轮/多轮出阵状态机。browser 只需 screenshot() 与 click(nx, ny)。"""

    def __init__(self, browser, log=print, poll: float = 2.0,
                 stuck_seconds: float = STUCK_SECONDS,
                 save_debug=None):
        self.b = browser
        self.log = log
        self.poll = poll
        self.stuck_seconds = stuck_seconds
        self._save_debug = save_debug or self._default_save_debug

    # ---- 画面识别 ----

    def classify(self, image) -> str:
        """认当前画面；认不出返回 'unknown'。"""
        if jp_vision.near_black(image):
            return 'loading'
        normalized = jp_vision.normalize_frame(image)
        for name, templates in SCREENS:
            try:
                hits = [jp_vision.match(normalized, jp_vision.load_template(t))
                        for t in templates]
            except jp_vision.TemplateNotFound:
                continue
            if hits and all(hits):
                return name
        return 'unknown'

    def _wait(self, targets: frozenset[str], timeout: float,
              passable: frozenset[str], what: str) -> str:
        """轮询画面直到命中 targets；弹窗立即中止，卡死超时熔断。"""
        deadline = time.monotonic() + timeout
        last = (None, None)          # (screen, digest)
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            image = self.b.screenshot()
            screen = self.classify(image)
            if screen in targets:
                return screen
            if screen == 'dialog':
                raise SallyStop(f'{what}时出现了确认弹窗，已停止；请人工查看游戏')
            if screen not in passable:
                self._save_debug(image)
                raise SallyStop(f'{what}时遇到了不认识的画面，已停止（截图已存到 debug）')
            digest = _digest(image)
            if (screen, digest) != last:
                last = (screen, digest)
                last_change = time.monotonic()
            elif time.monotonic() - last_change > self.stuck_seconds:
                self._save_debug(image)
                raise SallyStop(f'{what}时画面超过 {int(self.stuck_seconds)} 秒没有变化，已停止')
            time.sleep(self.poll)
        raise SallyStop(f'{what}超时，已停止；请人工查看游戏')

    def _click_hit(self, template: str, what: str) -> None:
        """在当前已确认的画面上找模板并点其中心。"""
        image = self.b.screenshot()
        hit = jp_vision.find(image, template)
        if not hit:
            raise SallyStop(f'{what}的按钮没认出来，已停止；请人工查看游戏')
        self.b.click(hit.nx, hit.ny)

    # ---- 出阵流程 ----

    def _navigate_to_team(self) -> None:
        """走到部队选择页。支持从本丸/指挥菜单/时代/地域任意一层接续。"""
        deadline = time.monotonic() + NAV_TIMEOUT * 5
        last = (None, None)
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            image = self.b.screenshot()
            screen = self.classify(image)
            if screen == 'team':
                return
            if screen == 'dialog':
                raise SallyStop('前往出阵时出现了确认弹窗，已停止；请人工查看游戏')
            if screen == 'loading':
                time.sleep(self.poll)
                continue
            if screen == 'home':
                self.b.click(*SHIKI_TAB)
            elif screen == 'shiki_menu':
                self._click_hit('menu/sally', '出阵入口')
            elif screen == 'era':
                self._click_hit('era/decide', '时代决定')
            elif screen == 'map':
                self._click_hit('map/team-select', '部队选择入口')
            else:
                self._save_debug(image)
                raise SallyStop('前往出阵时遇到了不认识的画面，已停止（截图已存到 debug）')
            digest = _digest(image)
            if (screen, digest) != last:
                last = (screen, digest)
                last_change = time.monotonic()
            elif time.monotonic() - last_change > self.stuck_seconds:
                self._save_debug(image)
                raise SallyStop('导航时画面卡住不动了，已停止；请人工查看游戏')
            time.sleep(self.poll)
        raise SallyStop('前往出阵超时，已停止；请人工查看游戏')

    def _select_team(self, team_no: int) -> None:
        self.b.click(*TEAM_TABS[team_no])
        time.sleep(1.0)

    def _enable_automarch(self) -> None:
        self.b.click(*AUTOMARCH_BUTTON)
        try:
            self._wait(frozenset({'automarch_dialog'}), NAV_TIMEOUT,
                       frozenset({'loading', 'team'}), '打开自动行军')
        except SallyStop:
            raise SallyStop('自动行军设置没打开；如果是第一次用，请手动点一次「自動行軍」看完说明')
        self.b.click(*AUTOMARCH_CONFIRM)
        time.sleep(1.0)
        self.b.click(*DIALOG_CLOSE)
        self._wait(frozenset({'team'}), NAV_TIMEOUT,
                   frozenset({'loading', 'automarch_dialog'}), '返回部队选择')

    def _watch_march(self) -> None:
        """盯行军：回家即完成；弹窗/卡死/超时立即停。"""
        self.log('行军中（自动行军）…')
        deadline = time.monotonic() + MARCH_TIMEOUT
        last = (None, None)
        last_change = time.monotonic()
        last_beat = 0.0
        while time.monotonic() < deadline:
            image = self.b.screenshot()
            screen = self.classify(image)
            if screen == 'home':
                return
            if screen == 'dialog':
                raise SallyStop('行军途中出现了确认弹窗（可能是重伤警告），已停止；请人工查看游戏')
            if screen not in MARCH_PASSABLE:
                self._save_debug(image)
                raise SallyStop('行军途中遇到了不认识的画面，已停止（截图已存到 debug）')
            digest = _digest(image)
            if (screen, digest) != last:
                last = (screen, digest)
                last_change = time.monotonic()
            elif screen != 'battle' and time.monotonic() - last_change > self.stuck_seconds:
                self._save_debug(image)
                raise SallyStop('画面长时间没有变化，可能行军停住了，已停止；请人工查看游戏')
            if time.monotonic() - last_beat > 30:
                self.log('仍在行军…')
                last_beat = time.monotonic()
            time.sleep(self.poll)
        raise SallyStop('行军超过 25 分钟仍未回本丸，已停止；请人工查看游戏')

    def run(self, team_no: int = 1, rounds: int = 1):
        """generator：逐步产出日志行。team_no 1-5，rounds ≥1。"""
        if team_no not in TEAM_TABS:
            raise ValueError('部队番号应在 1-5 之间')
        if not 1 <= rounds <= 200:
            raise ValueError('连刷轮数应在 1-200 之间')
        self.log('连接日服游戏页面…')
        for round_no in range(1, rounds + 1):
            self.log(f'第 {round_no}/{rounds} 轮：前往出阵')
            self._navigate_to_team()
            self._select_team(team_no)
            self._enable_automarch()
            self.log(f'部队{ "一二三四五"[team_no - 1] }出发')
            self._click_hit('team/depart', '出阵按钮')
            self._watch_march()
            self.log(f'第 {round_no}/{rounds} 轮回本丸')
        self.log(f'出阵完成，共 {rounds} 轮')

    # ---- 调试帧 ----

    @staticmethod
    def _default_save_debug(image) -> None:
        try:
            path = frames.JP_DATA_DIR / 'debug' / 'frames' / f"sally-stop-{time.strftime('%Y%m%d-%H%M%S')}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path)
        except OSError:
            pass


def run_script(config_path=None, params: dict | None = None):
    """面板脚本入口：generator[str]，复用 worker 子进程骨架。

    SallyStop 转成 FlowAborted，让面板按「安全停止」（退出码 42）收尾，
    而不是报「脚本崩了」。"""
    from .flow_control import FlowAborted
    params = params or {}
    try:
        team_no = int(params.get('team_no', 1))
        rounds = int(params.get('rounds', 1))
    except (TypeError, ValueError):
        raise ValueError('参数格式不正确')
    try:
        with jp_browser.JpBrowser() as browser:
            sally = Sally(browser)
            yield from sally.run(team_no=team_no, rounds=rounds)
    except SallyStop as exc:
        raise FlowAborted(str(exc)) from exc
