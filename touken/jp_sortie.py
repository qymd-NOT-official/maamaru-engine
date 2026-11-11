# -*- coding: utf-8 -*-
"""日服普通出阵刷图（jp-sortie）：指定时代/小图/部队刷圈的宿主。

与 jp-sally 的差别：sally 用游戏记忆的图，本宿主每圈自己选时代卡 +
小图卡。导航/选队/委托自动行军/队伍数据检查全部复用 Sally 底座，
不手搓零件；流程编排照 jp_edocastle 的风格（圈循环 + 通信错误自愈）。

安全底线（同 jp_sally / jp_edocastle）：

- 只点认得出的画面；模板没标定就列清楚缺什么然后停，绝不盲点。
  日服模板只准从 JpBrowser 实跑帧裁剪（截图同源纪律）。
- 行军途中任何确认弹窗一律停——重伤警告就是弹窗，绝不点「继续行军」。
- 出发前经队伍数据通道确认状态：重伤拦截、空队/不在待命/数据不新鲜
  一律停（复用 Sally._check_party）。
- 疲劳停止走 jp_party 数据通道读气力值，阈值是参数；触线收工不硬刷。
- 记录只写日服数据目录（JP_DATA_DIR 的独立账房库）。

【待实机标定清单】（骨架未在真机跑过，以下全部要实机验证/标定后才准实跑）：

1. ERA_OFFSETS：时代屏 8 张时代卡的点击偏移（相对 era/decide 锚点），
   当前是占位值。时代卡多了可能要翻页/滚动，真机确认 8 卡是否同屏。
2. MAP_OFFSETS：小图屏 4 张小图卡的点击偏移（相对 map/team-select
   锚点），当前是占位值。
3. 点已选中的时代卡/小图卡是否等于确认并跳级（国服机制如此，
   sortie.py:189-243 有实锤；日服待验证）——_pick_map 按状态驱动写，
   三种落点（era/map/team）都接得住，但跳级行为要真机过一遍。
4. 日服普通合战场点 team/depart 后有无二次确认窗（国服普通合战场没有；
   江户城有消费手形确认窗）。_depart 见到任何弹窗一律停，若真机发现
   普通出阵有预期内确认窗，再为它加专用双模板屏（照 edo/confirm-go）。
5. 打完一圈是否自动回本丸（国服普通合战场是）。_watch_march 按
   「见到 home = 收圈」写；若真机落在别的画面（如结算屏等戳），
   需要补收圈动作。
6. 行军途中的实际画面构成（battle/field/mapview/result 是否都会出现）
   按 Sally 的 MARCH_PASSABLE 放行，真机第一圈要盯着日志过一遍。
"""
from __future__ import annotations

import time

from . import jp_browser, jp_game_device, jp_party, jp_sally, jp_vision

SCRIPT_NAME = 'jp-sortie'

_ERROR_SCREEN = ('error', ('error/screen',))
# 整页版错误页（米黄底狐狸哭脸，没有再読み込み按钮，只能整页刷新）
_ERROR_FULL_SCREEN = ('error_full', ('error/screen-full',))

# 画面分类：覆盖层（前 6 条）之后插入错误页（弹窗版+整页版），
# 其余沿用 Sally（普通出阵的时代/小图/部队屏锚点都是 Sally 自带的）。
SCREENS = (jp_sally.SCREENS[:6]
           + (_ERROR_SCREEN, _ERROR_FULL_SCREEN)
           + jp_sally.SCREENS[6:])

# 进图导航：与 Sally 同一张表，但到时代屏就停（时代/小图由 _pick_map
# 状态驱动地选，不让 route 拿游戏记忆的选择自动决定）。
SORTIE_ROUTE = {k: v for k, v in jp_sally.SALLY_ROUTE.items()
                if k not in ('era', 'map')}

# 回本丸导航：时代/小图/部队屏右上都有目録按钮（jp_expedition 已实机
# 实证），经目録大页点「本丸」瓦回本丸——目录的关闭按钮是回到打开它
# 的那一屏，回本丸必须走本丸瓦（2026-11-11 排雷实机验证，别用
# common/back 瞎点）。shiki_menu 是指挥菜单浮层，不在这条路线里：
# 起手撞见属于怪现场，交给 _navigate 安全停车。
HOME_ROUTE = {
    'directory': ('hit', 'directory/home', '目录本丸瓦'),
    'formation': ('near', 'formation/label', jp_sally.BACK_ARROW_OFFSET,
                  '编成页返回箭头'),
    'era': ('hit', 'home/catalog', '时代屏目录按钮'),
    'map': ('hit', 'home/catalog', '选图屏目录按钮'),
    'team': ('hit', 'home/catalog', '部队屏目录按钮'),
}

# 【待实机标定】时代卡中心，相对 era/decide 锚点中心（基准空间像素）。
# 占位值！8 张时代卡的实际布局（同屏还是翻页）要真机确认后重填。
ERA_OFFSETS = {
    1: (-450, -250), 2: (-150, -250), 3: (150, -250), 4: (450, -250),
    5: (-450, -50), 6: (-150, -50), 7: (150, -50), 8: (450, -50),
}
# 【待实机标定】小图卡中心，相对 map/team-select 锚点中心
# （基准空间像素）。占位值！
MAP_OFFSETS = {
    1: (-450, -250), 2: (-150, -250), 3: (150, -250), 4: (450, -250),
}

# 体检要验的模板（Sally 底座已验过的不在列；era/decide、map/team-select、
# team/depart 是早期本机标定 + 2026-11-11 重裁，照列防回归）
_REQUIRED_TEMPLATES = (
    'directory/sally', 'directory/home',
    'era/decide', 'map/team-select', 'team/depart',
    'error/screen', 'error/reload', 'error/screen-full',
)

PICK_BEATS = 25          # 选图状态机的心跳上限（每拍约 1 秒）
NUMERAL = '一二三四五'
ERAS = tuple(range(1, 9))       # 时代一～八
MAP_NOS = tuple(range(1, 5))    # 小图一～四


class JpSortie(jp_sally.Sally):
    """日服普通出阵宿主：Sally 的导航/选队/队伍检查底座 + 选图刷圈流程。"""

    SCREENS = SCREENS

    def __init__(self, device, params: dict | None = None, log=print,
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

    # ==================== 导航 ====================

    def _goto_sortie(self) -> str:
        """走到出阵的时代/小图/部队屏之一（哪层先撞上算哪层，选图接手）。"""
        return self._navigate(SORTIE_ROUTE, frozenset({'era', 'map', 'team'}),
                              '前往出阵')

    def _go_home(self) -> None:
        """回本丸：目録大页走「本丸」瓦（目录的关闭按钮回的是打开它的屏）。"""
        self._navigate(HOME_ROUTE, frozenset({'home'}), '回本丸')

    # ==================== 选图（状态驱动，不认死流程） ====================

    def _pick_map(self, chapter: int, map_no: int) -> None:
        """选时代卡 → 选小图卡 → 进部队屏。

        游戏会记住上次选的时代/小图：点到已选中的项可能等于确认直接
        跳级（国服实测机制，日服待实机验证，见模块头清单第 3 条）。
        所以每拍先认自己在哪页再动手，三种落点都接得住：
          部队屏 = 已被跳级送到，直接收工；
          小图屏 = 点小图卡，再到小图屏才点「部队选择」入口；
          时代屏 = 点时代卡，仍在时代屏才补点「决定」。
        """
        map_picked = False
        for _ in range(PICK_BEATS):
            image = self.device.screenshot(force=True)
            screen = self.classify(image)
            if screen == 'team':
                return
            if screen == 'map':
                if not map_picked:
                    self._click_near('map/team-select', MAP_OFFSETS[map_no],
                                     f'小图{NUMERAL[map_no - 1]}卡片',
                                     image=image)
                    map_picked = True
                else:
                    self._click_hit('map/team-select', '部队选择入口',
                                    image=image)
                time.sleep(1.0)
                continue
            if screen == 'era':
                self._click_near('era/decide', ERA_OFFSETS[chapter],
                                 f'时代{NUMERAL[chapter - 1]}卡片',
                                 image=image)
                time.sleep(0.8)
                # 点已选中的时代卡可能已跳级：仍在时代屏才补点决定
                image = self.device.screenshot(force=True)
                if self.classify(image) == 'era':
                    self._click_hit('era/decide', '时代决定', image=image)
                time.sleep(1.0)
                continue
            if screen == 'loading':
                time.sleep(self.poll)
                continue
            if screen in ('dialog', 'confirm', 'automarch_dialog'):
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '选图时出现了确认弹窗，已停止；请人工查看游戏')
            if screen in ('error', 'error_full'):
                raise _CommError()
            self._save_debug(image)
            raise jp_sally.SallyStop(
                '选图时遇到了不认识的画面，已停止（截图已存到 debug）')
        self._save_debug(self.device.screenshot(force=True))
        raise jp_sally.SallyStop('选图超时，已停止；请人工查看游戏')

    # ==================== 出发 / 盯屏 ====================

    def _depart(self) -> None:
        """点 いざ出陣!! 并验证真离开了部队屏。

        日服普通合战场有无二次确认窗待实机验证（见模块头清单第 4 条）；
        见到任何弹窗一律停——重伤警告就是弹窗。"""
        self._click_hit('team/depart', '出阵按钮')
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
                raise _CommError()
            # team 残留帧：继续等
        self._save_debug(self.device.screenshot(force=True))
        raise jp_sally.SallyStop('出阵没有生效，已停止；'
                                 '请人工查看游戏（截图已存 debug）')

    def _watch_march(self) -> None:
        """盯行军：回本丸即收圈；弹窗/卡死/超时立即停；通信错误页抛自愈。

        与 Sally._watch_march 同一状态机，多认两张通信错误页。"""
        self.log('行军中（自动行军）…')
        deadline = time.monotonic() + jp_sally.MARCH_TIMEOUT
        last = (None, None)
        last_change = time.monotonic()
        last_beat = 0.0
        while time.monotonic() < deadline:
            image = self.device.screenshot(force=True)
            screen = self.classify(image)
            if screen == 'home':
                return
            if screen in ('error', 'error_full'):
                raise _CommError()
            if screen in ('dialog', 'confirm', 'automarch_dialog'):
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '行军途中出现了确认弹窗（可能是重伤警告），已停止；'
                    '请人工查看游戏')
            if screen not in jp_sally.MARCH_PASSABLE:
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '行军途中遇到了不认识的画面，已停止（截图已存到 debug）')
            digest = jp_sally._digest(image)
            if (screen, digest) != last:
                last = (screen, digest)
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
        raise jp_sally.SallyStop('行军超过 25 分钟仍未回本丸，已停止；请人工查看游戏')

    def _recover_error(self) -> bool:
        """通信错误自愈；返回 True 表示这一圈已在恢复过程中收完。

        报错的瞬间游戏可能已自己恢复（瞬断），按当前画面分派：
        已回本丸 = 圈已收完；整页版错误页只能整页刷新；弹窗版点
        再読み込み后重走启动序列（本轮要重跑）；其他画面如实停车。"""
        image = self.device.screenshot(force=True)
        screen = self.classify(image)
        if screen == 'home':
            self.log('通信错误已自己恢复，这一圈其实已经跑完了')
            return True
        if screen == 'error_full':
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

    # ==================== 队伍数据 ====================

    def _fetch_party_fresh(self, watch):
        """走远征画面逼游戏拉 /conquest 拿新鲜队伍数据（页面内导航，
        不刷新——梯子卡的时候刷新页面是白页死亡螺旋，2026-11-10 实测）。

        调用时应在时代/小图/部队屏；先经目録本丸瓦回本丸，拿完数据
        留在本丸，由调用方重新导航进图。"""
        from . import jp_expedition
        self.log('去远征画面拿一次队伍数据…')
        self._go_home()
        self._navigate(jp_expedition.EXPEDITION_ROUTE,
                       frozenset({'expedition'}), '去拿队伍数据')
        latest = self._await_party_data(watch)     # 等 /conquest 落袋
        self._click_hit('common/back', '远征页返回箭头')   # expedition → home
        time.sleep(2.0)
        return latest

    def _check_fatigue(self, latest, team_no: int, threshold: int) -> None:
        """疲劳停止：任一成员气力 ≤ 阈值就收工（不硬刷）。

        气力字段缺失的成员不拦（_check_party 已保证伤势数据齐全；
        气力读不到只记日志，不当停车理由）。"""
        captured_at, payload = latest
        snapshot = jp_party.build_team_snapshot(payload, team_no,
                                                now=captured_at)
        if snapshot is None:
            return  # 编制认不出：_check_party 已经停过了，到不了这里
        tired = [m for m in snapshot.members
                 if m.fatigue is not None and m.fatigue <= threshold]
        if tired:
            names = '、'.join(m.name or f'#{m.serial_id}' for m in tired)
            raise jp_sally.SallyStop(
                f'{names}的气力已经掉到 {threshold} 或以下，'
                f'按疲劳停止收工；去喂喂团子或者换队吧')

    # ==================== 主流程 ====================

    def run(self, chapter: int, map_no: int, team_no: int, rounds: int,
            party_watch=None, stop_on_fatigue: bool = True,
            fatigue_threshold: int = 30) -> None:
        """跑 rounds 圈普通合战场；日志经 self.log 输出。"""
        if chapter not in ERAS:
            raise ValueError('章节（时代）应在 1-8 之间')
        if map_no not in MAP_NOS:
            raise ValueError('小图编号应在 1-4 之间')
        if team_no not in jp_sally.TEAM_TAB_OFFSETS:
            raise ValueError('部队番号应在 1-5 之间')
        if not 1 <= rounds <= 200:
            raise ValueError('圈数应在 1-200 之间')
        if not 1 <= fatigue_threshold <= 99:
            raise ValueError('疲劳阈值应在 1-99 之间')
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
                self._run_one(round_no, rounds, chapter, map_no, team_no,
                              party_watch, stop_on_fatigue, fatigue_threshold)
            except _CommError:
                # 通信错误页：自愈后本轮重试一次；恢复途中圈已收完
                # （瞬断回本丸）就不再重跑
                if not self._recover_error():
                    try:
                        self._run_one(round_no, rounds, chapter, map_no,
                                      team_no, party_watch, stop_on_fatigue,
                                      fatigue_threshold)
                    except _CommError:
                        raise jp_sally.SallyStop(
                            '通信错误反复出现，这一轮放弃；'
                            '请检查网络后重新开跑')
            done += 1
            self.log(f'第 {round_no}/{rounds} 圈完成')
            self.record_event('sortie.run_completed',
                              server='jp', chapter=chapter, map_no=map_no,
                              run=round_no, source='普通出阵')
        self.log(f'普通出阵收工，共跑 {done} 圈')

    def _run_one(self, round_no: int, rounds: int, chapter: int, map_no: int,
                 team_no: int, party_watch, stop_on_fatigue: bool,
                 fatigue_threshold: int) -> None:
        self.log(f'第 {round_no}/{rounds} 圈：前往出阵（时代{NUMERAL[chapter - 1]}'
                 f'小图{NUMERAL[map_no - 1]}）')
        self._goto_sortie()
        self._pick_map(chapter, map_no)
        if party_watch is not None:
            latest = self._await_party_data(party_watch)
            if latest is None:
                # 起手就在出阵各屏时游戏不重拉 /sally（§66），页面内走
                # 远征画面拿 /conquest；拿不到才刷新页面兜底
                self._fetch_party_fresh(party_watch)
                self._goto_sortie()
                self._pick_map(chapter, map_no)
                latest = self._await_party_data(party_watch)
            if latest is None:
                self.log('页面内拿不到队伍数据，刷新页面重拿一次…')
                self.device.reload()
                self._boot_to_home()
                self._goto_sortie()
                self._pick_map(chapter, map_no)
                latest = self._await_party_data(party_watch)
            self._check_party(party_watch, team_no, latest)
            if stop_on_fatigue:
                self._check_fatigue(latest, team_no, fatigue_threshold)
        self._select_team(team_no)
        self._enable_automarch()
        self.log(f'部队{NUMERAL[team_no - 1]}出发（时代{NUMERAL[chapter - 1]}'
                 f'小图{NUMERAL[map_no - 1]}）')
        self._depart()
        self._watch_march()


class _CommError(Exception):
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
            'chapter': int(params.get('chapter', 1)),
            'map_no': int(params.get('map_no', 1)),
            'team_no': int(params.get('team_no', 1)),
            'rounds': int(params.get('rounds', 1)),
            'stop_on_fatigue': bool(params.get('stop_on_fatigue', True)),
            'fatigue_threshold': int(params.get('fatigue_threshold', 30)),
        }
    except (TypeError, ValueError):
        raise ValueError('参数格式不正确')
    try:
        with jp_game_device.JpGameDevice(jp_browser.JpBrowser()) as device:
            with jp_party.PartyWatch() as watch:
                JpSortie(device, normalized).run(
                    chapter=normalized['chapter'],
                    map_no=normalized['map_no'],
                    team_no=normalized['team_no'],
                    rounds=normalized['rounds'],
                    party_watch=watch,
                    stop_on_fatigue=normalized['stop_on_fatigue'],
                    fatigue_threshold=normalized['fatigue_threshold'])
    except jp_party.PartyWatchError as exc:
        raise FlowAborted(str(exc)) from exc
    except jp_vision.TemplateNotFound as exc:
        raise FlowAborted(str(exc)) from exc
    except jp_sally.SallyStop as exc:
        raise FlowAborted(str(exc)) from exc
    yield from ()
