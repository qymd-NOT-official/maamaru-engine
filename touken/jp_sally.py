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
BACK_ARROW = (0.138, 0.115)         # 编成等子页左上「↩」回本丸
TEAM_TABS = {1: (0.142, 0.168), 2: (0.229, 0.168), 3: (0.316, 0.168),
             4: (0.403, 0.168), 5: (0.490, 0.168)}
AUTOMARCH_BUTTON = (0.907, 0.490)   # 部队选择页「自動行軍」
AUTOMARCH_CONFIRM = (0.600, 0.687)  # 自动行军弹窗「依頼する」
DIALOG_CLOSE = (0.788, 0.116)       # 弹窗右上 ✕

NAV_TIMEOUT = 25.0                  # 每步导航认画面的上限
MARCH_TIMEOUT = 1500.0              # 一轮行军总上限
STUCK_SECONDS = 120.0               # 画面纹丝不动的熔断线
RELOAD_AFTER = 60.0                 # 导航时黑屏加载超过这么久就刷新一次页面自愈
BOOT_TIMEOUT = 300.0                # 刷新后重进本丸的上限（数据加载可能很慢）
GREET_STABLE = 4.0                  # 登录问候画面稳定这么久才点中央跳过

# 画面分类：按优先级从上到下
# 注意 shiki_menu 必须排在 home 前面：指挥菜单是盖在本丸上的浮层，
# 本丸绿标依然可见，先认 home 会永远看不见菜单。
SCREENS = (
    ('loading', ('loading/sakura',)),
    ('automarch_dialog', ('dialog/automarch-request',)),
    ('dialog', ('dialog/yes', 'dialog/no')),      # 两个按钮都中才算弹窗
    ('boot_arrow', ('boot/volume-arrow',)),       # 启动「音量注意」
    ('boot_honmaru', ('boot/honmaru-button',)),   # 标题页「本丸」按钮
    ('formation', ('formation/label',)),          # 编成页（停留现场，点 ↩ 回本丸）
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
        if jp_vision.near_black(image) or jp_vision.mostly_dark(image):
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

    def _click_hit(self, template: str, what: str, image=None) -> None:
        """在已确认的画面上找模板并点其中心。

        默认重新取一帧；调用方若刚分类过某帧（如启动序列）应把它传进来——
        音量箭头是动的，重新取帧可能抓到另一个动画相位导致匹配失败。"""
        if image is None:
            image = self.b.screenshot()
        hit = jp_vision.find(image, template)
        if not hit:
            raise SallyStop(f'{what}的按钮没认出来，已停止；请人工查看游戏')
        self.b.click(hit.nx, hit.ny)

    # ---- 出阵流程 ----

    def _boot_to_home(self) -> None:
        """刷新后的启动序列：音量注意 → 标题页「本丸」按钮 → 进本丸。

        启动途中会路过白屏/黑屏/读条等认不出的画面，一律只等不点；
        只点认得的两个按钮（音量提示的绿色箭头、标题页的本丸）。
        导航期间视口读数会随滚动条显隐抖动，这里放宽尺寸熔断，
        回本丸后自动重锁基线。"""
        deadline = time.monotonic() + BOOT_TIMEOUT
        last = (None, None)
        last_change = time.monotonic()
        entered = False                   # 已点过标题页本丸按钮
        greeted = False                   # 登录问候已点过
        unknown_since: float | None = None  # 进入未知画面的时刻
        departed = False                  # 已看到旧画面消失（防刷新前的残留帧被误当「到本丸了」）
        with self.b.relaxed_view():
            while time.monotonic() < deadline:
                image = self.b.screenshot()
                screen = self.classify(image)
                if screen != 'home':
                    departed = True
                elif not departed:
                    time.sleep(self.poll)
                    continue
                else:
                    self.log('游戏已重新进入本丸')
                    return
                if screen == 'dialog':
                    raise SallyStop('游戏重新启动时出现了确认弹窗，已停止；请人工查看游戏')
                if screen == 'boot_arrow':
                    self._click_hit('boot/volume-arrow', '音量提示箭头', image=image)
                elif screen == 'boot_honmaru':
                    self._click_hit('boot/honmaru-button', '标题页本丸按钮', image=image)
                    entered = True
                elif (entered and not greeted and screen == 'unknown'
                        and unknown_since is not None
                        and time.monotonic() - unknown_since > GREET_STABLE):
                    # 点过本丸按钮后的合理落点只有登录问候（近侍无 UI 全身像）。
                    # 它没有可匹配的固定元素，认图无解——此处上下文收敛，
                    # 等画面稳定 4 秒后点一次中央跳过问候。只点这一次；若猜错
                    # （如是公告页），后续画面不动会触发卡死熔断，安全停车。
                    self.log('跳过登录问候…')
                    self.b.click(0.5, 0.5)
                    greeted = True
                unknown_since = (unknown_since if screen == 'unknown' else None)
                if unknown_since is None and screen == 'unknown':
                    unknown_since = time.monotonic()
                digest = _digest(image)
                if (screen, digest) != last:
                    last = (screen, digest)
                    last_change = time.monotonic()
                elif time.monotonic() - last_change > self.stuck_seconds:
                    self._save_debug(image)
                    raise SallyStop('游戏重新启动时画面卡住不动了，已停止；请人工查看游戏')
                time.sleep(self.poll)
        self._save_debug(self.b.screenshot())
        raise SallyStop('游戏重新启动超时，已停止；请人工查看游戏')

    def _navigate_to_team(self) -> None:
        """走到部队选择页。支持从本丸/指挥菜单/时代/地域/编成任意一层接续。

        黑屏加载（转场/挂起后页面重载）是正常路过的画面；但持续黑屏超过
        RELOAD_AFTER 说明游戏多半挂在加载上（闲置掉线、渲染上下文丢失），
        自动刷新一次页面让它重进本丸。刷新后仍认不出画面则安全停止。"""
        deadline = time.monotonic() + NAV_TIMEOUT * 5
        last = (None, None)
        last_change = time.monotonic()
        loading_since: float | None = None
        reloaded = False
        while time.monotonic() < deadline:
            image = self.b.screenshot()
            screen = self.classify(image)
            if screen == 'team':
                return
            if screen == 'dialog':
                raise SallyStop('前往出阵时出现了确认弹窗，已停止；请人工查看游戏')
            if screen == 'loading':
                if loading_since is None:
                    loading_since = time.monotonic()
                elif not reloaded and time.monotonic() - loading_since > RELOAD_AFTER:
                    self.log('游戏黑屏加载太久，自动刷新一次页面…')
                    self.b.reload()
                    reloaded = True
                    self._boot_to_home()
                    deadline = time.monotonic() + NAV_TIMEOUT * 5
                    loading_since = None
                    continue
                time.sleep(self.poll)
                continue
            loading_since = None
            if screen == 'home':
                self.b.click(*SHIKI_TAB)
            elif screen == 'formation':
                self.b.click(*BACK_ARROW)
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
        raise SallyStop('前往出阵超时，已停止；若游戏一直黑屏请手动刷新页面后重试')

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
