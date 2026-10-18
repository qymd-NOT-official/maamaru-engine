# -*- coding: utf-8 -*-
"""日服点击出阵（sally）：画面识别 + 点击的状态机，无 OCR。

安全底线（复刻国服 _safe_depart_stream 的安全语义）：

- 只在认得出的画面上点击；认不出就停，绝不盲点。
- 行军中画面长时间不变且没回本丸 → 熔断（卡住比点错安全）。
- 行军途中出现任何弹窗一律停——重伤警告就是弹窗，绝不点「继续行军」。
- 出发前经队伍数据通道确认状态：重伤拦截、空队/不在待命/数据不新鲜一律停。
- 只打已攻略的图 + 自动行军：不开自动行军的出阵直接拒绝。
"""
from __future__ import annotations

import hashlib
import time

from . import jp_browser, jp_browser_probe as frames, jp_party, jp_vision

# 点击目标一律「锚点模板 + 基准分辨率像素偏移」：固定归一化坐标在窗口
# 高度变化后会漂（2026-10-12 事故：窗口 1088→944，指揮标签点歪到编成），
# 锚点找得到，偏移就跟着对。偏移量都是基准空间（1508x1088）像素。
TEAM_TAB_OFFSETS = {                  # 部队页签，相对 team/depart 按钮
    1: (-1151, -593), 2: (-1018, -593), 3: (-884, -593),
    4: (-750, -593), 5: (-616, -593),
}
AUTOMARCH_OFFSET = (1, -239)          # 部队选择页「自動行軍」，相对 team/depart
AUTOMARCH_CONFIRM_OFFSET = (151, 582) # 弹窗「依頼する」，相对弹窗标题
DIALOG_CLOSE_OFFSET = (434, -40)      # 弹窗右上 ✕，相对弹窗标题
BACK_ARROW_OFFSET = (118, -36)        # 编成页左上「↩」，相对 formation/label

NAV_TIMEOUT = 25.0                  # 每步导航认画面的上限
MARCH_TIMEOUT = 1500.0              # 一轮行军总上限
STUCK_SECONDS = 120.0               # 画面纹丝不动的熔断线
RELOAD_AFTER = 60.0                 # 导航时黑屏加载超过这么久就刷新一次页面自愈
BOOT_TIMEOUT = 300.0                # 刷新后重进本丸的上限（数据加载可能很慢）
GREET_STABLE = 4.0                  # 登录问候画面稳定这么久才点中央跳过
UNKNOWN_STABLE = 8.0                # 起手未知画面稳定这么久就刷新页面重来
PARTY_DATA_WAIT = 10.0              # 等队伍数据跟上的宽限（进出阵菜单后接口回填要一两拍）
PARTY_DATA_MAX_AGE = 180.0          # 队伍数据超过这么久没更新就不算数

# 画面分类：按优先级从上到下
# 注意 shiki_menu 必须排在 home 前面：指挥菜单是盖在本丸上的浮层，
# 本丸绿标依然可见，先认 home 会永远看不见菜单。
SCREENS = (
    ('loading', ('loading/sakura',)),
    ('automarch_dialog', ('dialog/automarch-request',)),
    ('dialog', ('dialog/yes', 'dialog/no')),      # 两个按钮都中才算弹窗
    ('confirm', ('dialog/decline', 'dialog/accept')),  # 内番续班等 はい/いいえ 确认窗
    ('notice', ('notice/close', 'notice/hide-forever')),  # 公告栏：✕ + 绿条都中才算
    ('boot_arrow', ('boot/volume-arrow',)),       # 启动「音量注意」
    ('boot_honmaru', ('boot/honmaru-button',)),   # 标题页「本丸」按钮
    ('formation', ('formation/label',)),          # 编成页（停留现场，点 ↩ 回本丸）
    ('directory', ('directory/sally',)),          # 目录大页（不透明，景趣影响不到）
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

    def _click_near(self, anchor: str, offset: tuple[float, float],
                    what: str, image=None) -> None:
        """点锚点模板中心 + 基准分辨率像素偏移处。

        用于没有独立模板价值的固定按钮（页签、弹窗按钮、返回箭头）：
        窗口高度变化只是视口裁剪，内容相对位置不变，锚点命中偏移就对。"""
        if image is None:
            image = self.b.screenshot()
        normalized = jp_vision.normalize_frame(image)
        hit = jp_vision.match(normalized, jp_vision.load_template(anchor),
                              name=anchor)
        if not hit:
            raise SallyStop(f'{what}没认出来，已停止；请人工查看游戏')
        w, h = normalized.size
        self.b.click(hit.nx + offset[0] / w, hit.ny + offset[1] / h)

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
                elif screen == 'confirm':
                    self.log('确认窗默认选否，继续')
                    self._click_hit('dialog/decline', '确认窗いいえ', image=image)
                elif screen == 'notice':
                    self.log('公告栏开着，点掉继续')
                    self._click_hit('notice/close', '公告栏关闭', image=image)
                elif (entered and not greeted and screen == 'unknown'
                        and unknown_since is not None
                        and time.monotonic() - unknown_since > GREET_STABLE):
                    # 点过本丸按钮后的合理落点只有登录问候（近侍无 UI 全身像）。
                    # 它没有可匹配的固定元素，认图无解——此处上下文收敛，
                    # 等画面稳定 4 秒后点一次中央跳过问候。只点这一次；若猜错
                    # 画面，公告栏会被认出点掉，其余陌生画面不动会触发卡死
                    # 熔断，安全停车。
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
        """走到部队选择页。支持从本丸/目录/指挥菜单/时代/地域/编成任意一层接续。

        本丸一律走「目録」大页进出阵：目录页不透明，景趣怎么换都影响不到；
        指挥菜单是半透明浮层，模板会被景趣透出来的背景干扰（2026-10-12 西式
        景趣事故），只作为菜单已被手动打开时的接续路径。

        黑屏加载（转场/挂起后页面重载）是正常路过的画面；但持续黑屏超过
        RELOAD_AFTER 说明游戏多半挂在加载上（闲置掉线、渲染上下文丢失），
        自动刷新一次页面让它重进本丸。起手就认不出的画面（登录问候、滞留
        陌生页）同理等 UNKNOWN_STABLE 后刷新一次。刷新后仍认不出则安全停止。"""
        deadline = time.monotonic() + NAV_TIMEOUT * 5
        last = (None, None)
        last_change = time.monotonic()
        loading_since: float | None = None
        unknown_since: float | None = None
        progressed = False              # 已经点过东西（导航有进展）
        reloaded = False
        while time.monotonic() < deadline:
            image = self.b.screenshot()
            screen = self.classify(image)
            if screen == 'team':
                return
            if screen == 'dialog':
                raise SallyStop('前往出阵时出现了确认弹窗，已停止；请人工查看游戏')
            if screen == 'loading':
                unknown_since = None
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
            if screen == 'unknown' and not progressed:
                # 起手就认不出（登录问候/滞留陌生页）：原地等一小会儿，
                # 还不认识就刷新一次重走启动序列；导航有进展后再撞见
                # 未知画面仍按安全停车处理。
                loading_since = None
                if unknown_since is None:
                    unknown_since = time.monotonic()
                    time.sleep(self.poll)
                    continue
                if time.monotonic() - unknown_since <= UNKNOWN_STABLE:
                    time.sleep(self.poll)
                    continue
                if not reloaded:
                    self.log('当前画面认不出，刷新一次页面重来…')
                    self.b.reload()
                    reloaded = True
                    self._boot_to_home()
                    deadline = time.monotonic() + NAV_TIMEOUT * 5
                    unknown_since = None
                    time.sleep(self.poll)
                    continue
                # 刷新后依旧认不出 → 落到下面安全停车
            else:
                loading_since = None
                unknown_since = None
            if screen == 'notice':
                self.log('公告栏开着，点掉继续')
                self._click_hit('notice/close', '公告栏关闭', image=image)
            elif screen == 'confirm':
                self.log('确认窗默认选否，继续')
                self._click_hit('dialog/decline', '确认窗いいえ', image=image)
            elif screen == 'home':
                self._click_hit('home/catalog', '目录按钮', image=image)
            elif screen == 'directory':
                self._click_hit('directory/sally', '目录出阵入口', image=image)
            elif screen == 'formation':
                self._click_near('formation/label', BACK_ARROW_OFFSET,
                                 '编成页返回箭头', image=image)
            elif screen == 'shiki_menu':
                self._click_hit('menu/sally', '出阵入口', image=image)
            elif screen == 'era':
                self._click_hit('era/decide', '时代决定', image=image)
            elif screen == 'map':
                self._click_hit('map/team-select', '部队选择入口', image=image)
            else:
                self._save_debug(image)
                raise SallyStop('前往出阵时遇到了不认识的画面，已停止（截图已存到 debug）')
            progressed = True
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
        self._click_near('team/depart', TEAM_TAB_OFFSETS[team_no],
                         f'部队{"一二三四五"[team_no - 1]}页签')
        time.sleep(1.0)

    def _enable_automarch(self) -> None:
        self._click_near('team/depart', AUTOMARCH_OFFSET, '自动行军按钮')
        try:
            self._wait(frozenset({'automarch_dialog'}), NAV_TIMEOUT,
                       frozenset({'loading', 'team'}), '打开自动行军')
        except SallyStop:
            raise SallyStop('自动行军设置没打开；如果是第一次用，请手动点一次「自動行軍」看完说明')
        self._click_near('dialog/automarch-request', AUTOMARCH_CONFIRM_OFFSET,
                         '自动行军依頼する')
        time.sleep(1.0)
        self._click_near('dialog/automarch-request', DIALOG_CLOSE_OFFSET,
                         '关闭自动行军弹窗')
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

    def _await_party_data(self, watch):
        """等一份新鲜的队伍数据；宽限内没等到返回 None。

        队伍数据随「进出阵菜单」由游戏自己拉取：正常导航到部队页时它
        早已落袋，宽限只是等后台线程把响应收完；起手就停在部队页则
        根本没有新数据，等也白等，由调用方刷新页面重拿。"""
        deadline = time.monotonic() + PARTY_DATA_WAIT
        while True:
            latest = watch.latest()
            if latest and 0 <= time.time() - latest[0] <= PARTY_DATA_MAX_AGE:
                return latest
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.poll)

    def _check_party(self, watch, team_no: int, latest) -> None:
        """出发前确认队伍状态：重伤拦截，空队/不在待命/数据不新鲜一律停。"""
        numeral = '一二三四五'[team_no - 1]
        if latest is None:
            raise SallyStop('没能确认部队状态，已停止；请刷新游戏页面后重试')
        captured_at, payload = latest
        status = jp_party.party_status(payload, team_no)
        if status is None:
            raise SallyStop(f'没能认出部队{numeral}的编制，已停止；请人工查看游戏')
        if status != 1:
            raise SallyStop(f'部队{numeral}现在不在本丸待命（可能在远征或出阵中），已停止')
        snapshot = jp_party.build_team_snapshot(payload, team_no, now=captured_at)
        if snapshot is None or not snapshot.injury_known:
            raise SallyStop(f'没能确认部队{numeral}的成员状态，已停止；请人工查看游戏')
        heavy = [m for m in snapshot.members if m.injury == '重伤']
        if heavy:
            names = '、'.join(m.name or f'#{m.serial_id}' for m in heavy)
            raise SallyStop(f'{names}现在是重伤，继续出阵可能就毁了，已停止；'
                            '请先手入或换人')
        hurt = [m for m in snapshot.members if m.injury]
        if hurt:
            self.log('带伤出发：' + '、'.join(
                f'{m.name or m.serial_id}（{m.injury}）' for m in hurt))

    def run(self, team_no: int = 1, rounds: int = 1, party_watch=None) -> None:
        """跑 rounds 轮出阵；日志经 self.log 回调输出，返回值无意义。

        party_watch 给到队伍数据通道（jp_party.PartyWatch）：带上时出发前
        确认队伍状态（重伤拦截）；不带则跳过确认（仅测试这么跑）。"""
        if team_no not in TEAM_TAB_OFFSETS:
            raise ValueError('部队番号应在 1-5 之间')
        if not 1 <= rounds <= 200:
            raise ValueError('连刷轮数应在 1-200 之间')
        self.log('连接日服游戏页面…')
        for round_no in range(1, rounds + 1):
            self.log(f'第 {round_no}/{rounds} 轮：前往出阵')
            self._navigate_to_team()
            if party_watch is not None:
                latest = self._await_party_data(party_watch)
                if latest is None:
                    # 起手已停在部队页等场景：游戏没拉新数据，刷新页面
                    # 重走一遍导航逼它拉取，再拿不到才停。
                    self.log('没拿到队伍数据，刷新页面重新拿一次…')
                    self.b.reload()
                    self._boot_to_home()
                    self._navigate_to_team()
                    latest = self._await_party_data(party_watch)
                self._check_party(party_watch, team_no, latest)
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

    Sally 的日志走 print（worker 捕获 stdout），run() 本身不产出日志行，
    所以这里 yield from () 只为保持 generator 形态。
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
            with jp_party.PartyWatch() as watch:
                Sally(browser).run(team_no=team_no, rounds=rounds,
                                   party_watch=watch)
    except jp_party.PartyWatchError as exc:
        raise FlowAborted(str(exc)) from exc
    except SallyStop as exc:
        raise FlowAborted(str(exc)) from exc
    yield from ()
