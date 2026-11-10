# -*- coding: utf-8 -*-
"""日服江户城潜入调查（委託行軍版）：画面适配 + 宿主。

路线 A（2026-11-10 老大亲定）：委托游戏自动行军，脚本只管
进活动/选难度/选队/委托/出阵/盯完一圈，不认格子、不走最短路线。
格子识别留给路线 B（待数据同步报文侦察后再定）。

安全底线（同 jp_sally / jp_pumpkin）：

- 只点认得出的画面；模板没标定就列清楚缺什么然后停，绝不盲点。
  日服模板只准从 JpBrowser 实跑帧裁剪（截图同源纪律）。
- 行军途中任何确认弹窗一律停；虚拟伤害活动（刀剣破壊なし）也不替
  玩家点弹窗。
- 出发前经队伍数据通道确认状态：重伤拦截、空队/不在待命/数据不新鲜
  一律停（复用 Sally 的检查）。
- 不碰小判：通行手形烧完后的补充弹窗不认识，按弹窗停车收工。
- 记录只写日服数据目录（JP_DATA_DIR 的独立账房库）。

真机标定（2026-11-10，JpBrowser 实跑帧 1508x997，基准 1508x1088）：
edo/banner（本丸轮播活动横幅）、edo/screen（活动页地标标题）、
edo/diff-1..4（难度卡片标题行）、edo/checked（难度选中绿勾）、
edo/deploy（部隊選択按钮）、edo/team-screen（部队屏标题锚点）、
edo/march-auto（自動行軍按钮）、edo/march-active（依頼中标记）、
edo/go（いざ出陣!!按钮）、error/screen + error/reload（通信错误页）。
"""
from __future__ import annotations

import time

from . import jp_browser, jp_game_device, jp_party, jp_sally, jp_vision
from .device_adapter import Point

SCRIPT_NAME = 'jp-edocastle'

_EDO_SCREEN = ('edo', ('edo/screen',))
_ERROR_SCREEN = ('error', ('error/screen',))
# 整页版错误页（米黄底狐狸哭脸，没有再読み込み按钮，只能整页刷新）
_ERROR_FULL_SCREEN = ('error_full', ('error/screen-full',))
# 消费手形的出阵确认窗（标题+确认文双模板强信号）：预期内流程，不是危险弹窗
_EDO_GO_SCREEN = ('edo_go', ('edo/confirm-go', 'edo/confirm-text'))
# 一圈走完的分歧点：部隊を回復 / 本丸へ帰城 / 調査を継続（认得帰城按钮即可）
_EDO_BRANCH_SCREEN = ('edo_branch', ('edo/return-home',))

# 画面分类：覆盖层（前 6 条）之后插入活动页/错误页（弹窗版+整页版）/
# 出阵确认窗/分歧点/远征屏（拿队伍数据用）地标；
# 部队屏锚点换成江户城自己的标题（普通出阵的 team/depart 这里没有）。
SCREENS = (jp_sally.SCREENS[:6]
           + (_EDO_GO_SCREEN, _EDO_SCREEN, _ERROR_SCREEN, _ERROR_FULL_SCREEN,
              _EDO_BRANCH_SCREEN,
              ('expedition', ('expedition/screen',)))
           + tuple(('team', ('edo/team-screen',)) if s[0] == 'team' else s
                   for s in jp_sally.SCREENS[6:]))

# 导航路线：活动横幅在本丸左侧轮播里，直接点。
# 走错到合战场各屏就点左上返回箭头回本丸重来。
EDO_ROUTE = {
    'home': ('hit', 'edo/banner', '活动入口横幅'),
    'directory': ('hit', 'directory/close', '目录大页关闭按钮'),
    'formation': ('near', 'formation/label', jp_sally.BACK_ARROW_OFFSET,
                  '编成页返回箭头'),
    'era': ('hit', 'common/back', '合战场时代屏返回箭头'),
    'map': ('hit', 'common/back', '合战场选图屏返回箭头'),
    'team': ('hit', 'common/back', '合战场部队屏返回箭头'),
}

# 难度卡片中心，相对 edo/screen 锚点中心（基准空间像素，真机标定）
DIFF_OFFSETS = {
    1: (-425, 244),   # 難易度・易
    2: (-100, 244),   # 難易度・普
    3: (225, 244),    # 難易度・難
    4: (550, 244),    # 難易度・超難
}
# 选中绿勾的期望中心 x（基准空间像素）：卡片右下
DIFF_CHECKED_X = {1: 439, 2: 764, 3: 1089, 4: 1414}
CHECKED_TOLERANCE = 40

# 部队页签，相对 edo/team-screen 标题锚点（真机标定）
TEAM_TAB_OFFSETS = {
    1: (-520, 78), 2: (-385, 78), 3: (-249, 78), 4: (-117, 78), 5: (4, 78),
}

# 体检要验的模板（复用 jp_sally 已标定的不在列）
_REQUIRED_TEMPLATES = (
    'edo/banner', 'edo/screen', 'edo/deploy',
    'edo/diff-1', 'edo/diff-2', 'edo/diff-3', 'edo/diff-4', 'edo/checked',
    'edo/team-screen', 'edo/march-auto', 'edo/go',
    'edo/confirm-go', 'edo/confirm-text', 'edo/decide', 'edo/return-home',
    'error/screen', 'error/reload', 'error/screen-full',
)

RUN_TIMEOUT_S = 1500.0   # 一圈行军总上限（委托行军含全部战斗）
NUMERAL = '一二三四五'


class JpEdocastle(jp_sally.Sally):
    """日服江户城宿主：Sally 的导航/选队/队伍检查底座 + 活动流程。"""

    SCREENS = SCREENS

    def __init__(self, device, params: dict, log=print,
                 poll: float = 2.0,
                 stuck_seconds: float = jp_sally.STUCK_SECONDS,
                 save_debug=None):
        super().__init__(device, log=log, poll=poll,
                         stuck_seconds=stuck_seconds, save_debug=save_debug)
        self._params = dict(params or {})

    # ==================== 宿主设施 ====================

    def record_event(self, event_type: str, **payload):
        """日服账房：只写 JP_DATA_DIR 的独立账房库，与国服互不可见。"""
        try:
            from .telemetry import get_jp_telemetry_store
            return get_jp_telemetry_store().record_event(event_type, payload)
        except Exception:
            return None

    def _preflight(self) -> list[str]:
        """模板体检：缺的逐条列出（绝不静默、绝不给假值）。"""
        missing = []
        for name in _REQUIRED_TEMPLATES:
            try:
                jp_vision.load_template(name)
            except jp_vision.TemplateNotFound as exc:
                missing.append(str(exc))
        return missing

    # ==================== 流程部件 ====================

    def _goto_event(self) -> None:
        self._navigate(EDO_ROUTE, frozenset({'edo'}), '前往江户城活动')

    def _select_difficulty(self, difficulty: int) -> None:
        """点难度卡片，校验绿勾落到目标卡片上；不在活动页/校验失败即停。"""
        image = self.device.screenshot(force=True)
        self._click_near('edo/screen', DIFF_OFFSETS[difficulty],
                         f'难度{NUMERAL[difficulty - 1]}卡片', image=image)
        time.sleep(1.5)
        image = self.device.screenshot(force=True)
        hit = jp_vision.find(image, 'edo/checked')
        if not hit:
            self._save_debug(image)
            raise jp_sally.SallyStop('难度选中标记没认出来，已停止；请人工查看游戏')
        w, _ = jp_vision.base_size()
        x = hit.nx * w
        if abs(x - DIFF_CHECKED_X[difficulty]) > CHECKED_TOLERANCE:
            raise jp_sally.SallyStop(
                f'难度切换没生效（选中标记落在 x={int(x)}，'
                f'期望 {DIFF_CHECKED_X[difficulty]} 附近），已停止')

    def _open_team(self) -> None:
        self._click_hit('edo/deploy', '部隊選択按钮')
        # edo 也可放行：点完部隊選択后的第一帧必然还是活动页
        self._wait(frozenset({'team'}), jp_sally.NAV_TIMEOUT,
                   frozenset({'loading', 'edo'}), '打开部队选择')

    def _select_team(self, team_no: int) -> None:
        self._click_near('edo/team-screen', TEAM_TAB_OFFSETS[team_no],
                         f'部队{NUMERAL[team_no - 1]}页签')
        time.sleep(1.0)

    def _enable_automarch(self) -> None:
        """委托自动行军；已是依頼中则跳过（委托状态在服务端，刷新不掉）。"""
        image = self.device.screenshot(force=True)
        if jp_vision.find(image, 'edo/march-active'):
            self.log('自动行军委托已开着')
            return
        self._click_hit('edo/march-auto', '自动行军按钮', image=image)
        try:
            self._wait(frozenset({'automarch_dialog'}), jp_sally.NAV_TIMEOUT,
                       frozenset({'loading', 'team'}), '打开自动行军')
        except jp_sally.SallyStop:
            raise jp_sally.SallyStop(
                '自动行军设置没打开；如果是第一次用，请手动点一次「自動行軍」看完说明')
        self._click_near('dialog/automarch-request',
                         jp_sally.AUTOMARCH_CONFIRM_OFFSET, '自动行军依頼する')
        time.sleep(1.0)
        self._click_near('dialog/automarch-request',
                         jp_sally.DIALOG_CLOSE_OFFSET, '关闭自动行军弹窗')
        self._wait(frozenset({'team'}), jp_sally.NAV_TIMEOUT,
                   frozenset({'loading', 'automarch_dialog'}), '返回部队选择')

    def _depart(self) -> None:
        """点 いざ出陣!! → 消费手形确认窗 → 決定，验证真离开了部队屏。

        确认窗里的調査道具（五倍/二倍）一概不用。手形烧完时确认窗
        不会正常出现（变成购买引导）——等不到确认窗就如实收工，
        不碰小判。"""
        self._click_hit('edo/go', '出阵按钮')
        try:
            self._wait(frozenset({'edo_go'}), jp_sally.NAV_TIMEOUT,
                       frozenset({'loading', 'team'}), '打开出阵确认')
        except jp_sally.SallyStop:
            self._save_debug(self.device.screenshot(force=True))
            raise jp_sally.SallyStop(
                '出阵确认窗没出来——通行手形可能烧完了，本轮收工；'
                '若不是，请人工查看游戏（截图已存 debug）')
        self._click_hit('edo/decide', '出阵确认决定')
        for _ in range(6):
            time.sleep(1.5)
            image = self.device.screenshot(force=True)
            screen = self.classify(image)
            if screen in ('loading', 'battle', 'field', 'mapview', 'unknown'):
                self.log('出发，进入行军')
                return
            if screen in ('dialog', 'confirm', 'automarch_dialog'):
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '出阵时出现了确认弹窗，已停止；'
                    '请人工查看游戏（截图已存 debug）')
            if screen in ('error', 'error_full'):
                raise _EdoCommError()
        self._save_debug(self.device.screenshot(force=True))
        raise jp_sally.SallyStop('出阵没有生效，已停止；'
                                 '请人工查看游戏（截图已存 debug）')

    def _watch_run(self) -> None:
        """盯完这一圈：走到分歧点 = 一圈走完，点「本丸へ帰城」回城收圈。
        弹窗/卡死/超时立即停。

        首次进江户城会连弹多组狐狸教学气泡并暂停流程（每号一次，
        地图屏/结算屏都可能弹）：放行集内画面静止 12 秒点一次气泡
        翻页区（委托行军中点这些位置无害），最多 8 次，还不走才熔断。"""
        self.log('行军中（委托自动行军）…')
        deadline = time.monotonic() + RUN_TIMEOUT_S
        last = (None, None)
        last_change = time.monotonic()
        last_beat = 0.0
        bubble_poked = 0
        passable = jp_sally.MARCH_PASSABLE | {'edo_branch'}
        while time.monotonic() < deadline:
            image = self.device.screenshot(force=True)
            screen = self.classify(image)
            if screen == 'edo_branch':
                self._settle_branch()
                return
            if screen == 'edo':
                return  # 回城转场被放行帧跳过了也算收圈
            if screen in ('error', 'error_full'):
                raise _EdoCommError()
            if screen in ('dialog', 'confirm', 'automarch_dialog'):
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '行军途中出现了确认弹窗，已停止；请人工查看游戏')
            if screen not in passable:
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '行军途中遇到了不认识的画面，已停止（截图已存到 debug）')
            digest = jp_sally._digest(image)
            if (screen, digest) != last:
                last = (screen, digest)
                last_change = time.monotonic()
            elif bubble_poked < 8 and time.monotonic() - last_change > 12:
                base_w, base_h = jp_vision.base_size()
                self.device.click(Point(int(base_w * 0.755),
                                        int(base_h * 0.488)))
                self.log('画面疑似盖着教学气泡，点一下翻页区继续盯…')
                bubble_poked += 1
                last_change = time.monotonic()
            elif screen != 'battle' and \
                    time.monotonic() - last_change > self.stuck_seconds:
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '画面长时间没有变化，可能行军停住了，已停止；请人工查看游戏')
            if time.monotonic() - last_beat > 30:
                self.log('仍在行军…')
                last_beat = time.monotonic()
            time.sleep(self.poll)
        raise jp_sally.SallyStop('行军超过 25 分钟仍未走完，已停止；请人工查看游戏')

    # ---- 分歧点收圈 / 通信错误页自愈 ----

    def _settle_branch(self) -> None:
        """分歧点收圈：本丸へ帰城 → 确认はい → 回活动页。

        回城确认窗（帰城しますか はい/いいえ）是我们自己点帰城触发的，
        上下文收敛，确认回城。"""
        self._click_hit('edo/return-home', '回本丸按钮')
        self._wait(frozenset({'dialog'}), jp_sally.NAV_TIMEOUT,
                   frozenset({'loading', 'edo_branch', 'unknown'}),
                   '打开回城确认')
        self._click_hit('dialog/yes', '回城确认はい')
        time.sleep(2.0)  # 让确认窗消失：_wait 见 dialog 会立即停
        self._wait(frozenset({'edo'}), jp_sally.NAV_TIMEOUT * 2,
                   frozenset({'loading', 'unknown'}), '回活动页')

    def _recover_error(self) -> bool:
        """通信错误自愈；返回 True 表示这一圈已在恢复过程中收完。

        报错的瞬间游戏可能已自己恢复（瞬断），按当前画面分派：
        还在弹窗版错误页才点再読み込み（本轮要重跑）；整页版错误页没有
        按钮、整页刷新重来；已回分歧点就直接收圈；其他画面如实停车。"""
        image = self.device.screenshot(force=True)
        screen = self.classify(image)
        if screen == 'edo_branch':
            self.log('通信错误已自己恢复，直接收圈…')
            self._settle_branch()
            return True
        if screen == 'error_full':
            # 整页版错误页没有再読み込み按钮，只能整页刷新重来
            self.log('整页版通信错误页，整页刷新重来…')
            self.device.reload()
            time.sleep(5.0)
            self._boot_to_home()
            return False
        if screen != 'error':
            self._save_debug(image)
            raise jp_sally.SallyStop(
                '通信错误后画面落点不认识，已停止；请人工查看游戏（截图已存 debug）')
        self.log('游戏报了通信错误，重新加载页面…')
        self._click_hit('error/reload', '错误页重载按钮', image=image)
        time.sleep(5.0)
        self._boot_to_home()
        return False

    # ---- 队伍数据 ----

    def _fetch_party_fresh(self, watch):
        """走远征画面逼游戏拉 /conquest 拿新鲜队伍数据（页面内导航，
        不刷新——梯子卡的时候刷新页面是白页死亡螺旋，2026-11-10 实测）。

        调用时应在活动页；拿完回回本丸，由调用方重新导航进活动。"""
        from . import jp_expedition
        self.log('去远征画面拿一次队伍数据…')
        self._click_hit('common/back', '活动页返回箭头')   # edo → home
        time.sleep(2.0)
        self._navigate(jp_expedition.EXPEDITION_ROUTE,
                       frozenset({'expedition'}), '去拿队伍数据')
        latest = self._await_party_data(watch)     # 等 /conquest 落袋
        self._click_hit('common/back', '远征页返回箭头')   # expedition → home
        time.sleep(2.0)
        return latest

    # ==================== 主流程 ====================

    def run(self, team_no: int, difficulty: int, rounds: int,
            party_watch=None) -> None:
        """跑 rounds 圈江户城；日志经 self.log 输出。"""
        if team_no not in TEAM_TAB_OFFSETS:
            raise ValueError('部队番号应在 1-5 之间')
        if difficulty not in DIFF_OFFSETS:
            raise ValueError('难度应在 1-4 之间')
        if not 1 <= rounds <= 50:
            raise ValueError('圈数应在 1-50 之间')
        missing = self._preflight()
        if missing:
            self.log('日服画面素材没标定，先干不了活。缺这些模板：')
            for item in missing:
                self.log(f'  ✗ {item}')
            raise jp_sally.SallyStop('模板没标定全，已停止')
        self.log('连接日服游戏页面…')
        done = 0
        for round_no in range(1, rounds + 1):
            try:
                self._run_one(round_no, rounds, team_no, difficulty,
                              party_watch)
            except _EdoCommError:
                # 通信错误页：自愈后本轮重试一次，再犯如实放弃；
                # 恢复途中圈已收完（瞬断回分歧点）就不再重跑，不多烧手形
                if not self._recover_error():
                    self._run_one(round_no, rounds, team_no, difficulty,
                                  party_watch)
            done += 1
            self.log(f'第 {round_no}/{rounds} 圈完成')
            self.record_event('edocastle.run_completed',
                              server='jp', difficulty=difficulty,
                              run=round_no, source='江户城')
        self.log(f'江户城收工，共跑 {done} 圈')

    def _run_one(self, round_no: int, rounds: int, team_no: int,
                 difficulty: int, party_watch) -> None:
        self.log(f'第 {round_no}/{rounds} 圈：前往活动页')
        self._goto_event()
        self._select_difficulty(difficulty)
        self._open_team()
        if party_watch is not None:
            latest = self._await_party_data(party_watch)
            if latest is None:
                # 起跑就在活动页时游戏不重拉 /sally（§66），页面内走
                # 远征画面拿 /conquest；拿不到才刷新页面兜底
                self._fetch_party_fresh(party_watch)
                self._goto_event()
                self._select_difficulty(difficulty)
                self._open_team()
                latest = self._await_party_data(party_watch)
            if latest is None:
                self.log('页面内拿不到队伍数据，刷新页面重拿一次…')
                self.device.reload()
                self._boot_to_home()
                self._goto_event()
                self._select_difficulty(difficulty)
                self._open_team()
                latest = self._await_party_data(party_watch)
            self._check_party(party_watch, team_no, latest)
        self._select_team(team_no)
        self._enable_automarch()
        self.log(f'部队{NUMERAL[team_no - 1]}出发（难度{NUMERAL[difficulty - 1]}）')
        self._depart()
        self._watch_run()


class _EdoCommError(Exception):
    """游戏弹了通信错误页（エラー 300018 等），可刷新自愈。"""


def run_script(config_path=None, params: dict | None = None):
    """面板脚本入口：generator[str]，复用 worker 子进程骨架。

    日志走 print（worker 捕获 stdout）；SallyStop 转成 FlowAborted，
    让面板按「安全停止」（退出码 42）收尾，而不是报「脚本崩了」。"""
    from .flow_control import FlowAborted
    del config_path  # 日服参数全走 params，不读国服配置
    params = params or {}
    try:
        normalized = {
            'team_no': int(params.get('team_no', 1)),
            'difficulty': int(params.get('difficulty', 1)),
            'rounds': int(params.get('rounds', 6)),
        }
    except (TypeError, ValueError):
        raise ValueError('参数格式不正确')
    try:
        with jp_game_device.JpGameDevice(jp_browser.JpBrowser()) as device:
            with jp_party.PartyWatch() as watch:
                JpEdocastle(device, normalized).run(
                    team_no=normalized['team_no'],
                    difficulty=normalized['difficulty'],
                    rounds=normalized['rounds'],
                    party_watch=watch)
    except jp_party.PartyWatchError as exc:
        raise FlowAborted(str(exc)) from exc
    except jp_vision.TemplateNotFound as exc:
        raise FlowAborted(str(exc)) from exc
    except jp_sally.SallyStop as exc:
        raise FlowAborted(str(exc)) from exc
    yield from ()
