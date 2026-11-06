# -*- coding: utf-8 -*-
"""日服南瓜大作战（戦術強化訓練 かぼちゃ大作戦）：画面适配 + 宿主。

复用 flows/pumpkin_loop.py 的公共循环（令牌预算/看门狗/主循环），
日服差异全收在本层。2026-11-05 老大亲定的日服策略（交接文档 §64）：

- 不做活动条件表解析：固定满编攻略队出门，不做条件驱动的动态编队；
- 判定全部以数据同步结果为准：板子满没满看同步数据
  （/sally 明文响应 event.strike.sheet 的 9 格 is_clear，jp_ledger 落账为
  pumpkin.sheet_observed），掉落看刀帐差分（training.newcomers，
  照单全收一把不漏）；不读获得动画、不做剪影认人（死板版）；
- browser-lab 的 hunt 系列只是侦察工具，本文件按 jp_sally/jp_expedition
  的产品化姿势另写，野生脚本不进仓。

安全底线（同 jp_sally / jp_expedition）：

- 只点认得出的画面；模板没标定就把缺什么列清楚然后停，绝不盲点。
  日服模板只准从 JpBrowser 实跑帧裁剪（截图同源纪律）。
- 战斗后的确认弹窗一律停：日服弹窗语义未经真机校准前不替玩家做主。
- 记录只写日服数据目录（JP_DATA_DIR 的独立账房库），不碰国服 state/账房。
- 数据同步（listener）不在跑就拿不到板子状态——等不到新鲜数据即停，
  不假绿。

真机标定清单（JpBrowser 实跑帧，标定前本脚本会在体检步报缺并停）：
模板 pumpkin/screen（活动页地标）、pumpkin/banner（出撃页活动入口）、
pumpkin/deploy（部隊選択）、pumpkin/update（シート更新）、
pumpkin/update-confirm（更新确认）、pumpkin/tab-1..3（初/中/上級页签）；
复用已标定的 home/catalog、directory/sally、formation/label、team/depart。
"""
from __future__ import annotations

import json
import time

from . import jp_browser, jp_game_device, jp_sally, jp_vision
from .flows.pumpkin_loop import PumpkinLoopMixin

SCRIPT_NAME = 'jp-pumpkin'

# 画面分类扩展：活动页地标插在覆盖层（前 6 条）之后、出阵各屏之前。
_PUMPKIN_SCREEN = ('pumpkin', ('pumpkin/screen',))

# 导航路线：与出阵同款走目录大页进（不透明，景趣影响不到），
# 到出撃页点活动横幅进活动页。走错到合战场各屏就回目录重来。
PUMPKIN_ROUTE = {
    'home': ('hit', 'home/catalog', '目录按钮'),
    'directory': ('hit', 'directory/sally', '目录出阵入口'),
    'formation': ('near', 'formation/label', jp_sally.BACK_ARROW_OFFSET,
                  '编成页返回箭头'),
    'shiki_menu': ('hit', 'pumpkin/banner', '活动入口横幅'),
    'era': ('hit', 'home/catalog', '合战场时代屏目录按钮'),
    'map': ('hit', 'home/catalog', '合战场选图屏目录按钮'),
    'team': ('hit', 'home/catalog', '合战场部队屏目录按钮'),
}

# 安全点：相对 pumpkin/screen 锚点的偏移（基准空间像素），待真机标定
PUMPKIN_SKIP_OFFSET = (0, 420)

# 体检要验的模板（复用 jp_sally 已标定的不在列）
_REQUIRED_TEMPLATES = (
    'pumpkin/screen', 'pumpkin/banner', 'pumpkin/deploy',
    'pumpkin/update', 'pumpkin/update-confirm',
    'pumpkin/tab-1', 'pumpkin/tab-2', 'pumpkin/tab-3',
)

SHEET_WAIT_S = 45.0        # 进活动页后等数据同步喂来板子状态的上限
BATTLE_TIMEOUT_S = 600.0   # 一场活动战斗的上限（自动战斗，正常两分钟以内）
NUMERAL = '一二三四五'


def latest_sheet_state(store) -> dict | None:
    """账房里最新一份板子状态观测（pumpkin.sheet_observed），没有返回 None。"""
    row = store._conn().execute(
        "SELECT payload, ts FROM events "
        "WHERE event_type='pumpkin.sheet_observed' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('sheets'), dict):
        return None
    data['_ts'] = row[1]
    return data


class JpPumpkin(jp_sally.JpNavigator, PumpkinLoopMixin):
    """日服南瓜宿主：JpNavigator 的认屏/点击底座 + 南瓜公共循环。"""

    SCREENS = (jp_sally.SCREENS[:6] + (_PUMPKIN_SCREEN,)
               + jp_sally.SCREENS[6:])

    def __init__(self, device, params: dict, log=print,
                 poll: float = 2.0,
                 stuck_seconds: float = jp_sally.STUCK_SECONDS,
                 save_debug=None):
        super().__init__(device, log=log, poll=poll,
                         stuck_seconds=stuck_seconds, save_debug=save_debug)
        self._params = dict(params or {})
        self.config = {}
        self.current_location = None
        self._run_started_at = time.time()

    # ==================== 宿主设施 ====================

    def record_event(self, event_type: str, **payload):
        """日服账房：只写 JP_DATA_DIR 的独立账房库，与国服互不可见。"""
        try:
            from .telemetry import get_jp_telemetry_store
            return get_jp_telemetry_store().record_event(event_type, payload)
        except Exception:
            return None

    def _run_logged(self, fn):
        """跑一段会经 self.log 输出的操作；SallyStop 不外抛。

        返回 (logs, error)：logs 是 fn 期间产生的日志，error 是 SallyStop 或 None。"""
        logs = []
        prev = self.log
        self.log = logs.append
        error = None
        try:
            fn()
        except jp_sally.SallyStop as exc:
            error = exc
        finally:
            self.log = prev
        return logs, error

    def _preflight(self) -> list[str]:
        """模板体检：缺的逐条列出（绝不静默、绝不给假值）。"""
        missing = []
        for name in _REQUIRED_TEMPLATES:
            try:
                jp_vision.load_template(name)
            except jp_vision.TemplateNotFound as exc:
                missing.append(str(exc))
        return missing

    def _store(self):
        from .telemetry import get_jp_telemetry_store
        return get_jp_telemetry_store()

    def _wait_sheet_fresh(self) -> bool:
        """等数据同步喂来本次运行后的板子状态；超时返回 False。"""
        deadline = time.monotonic() + SHEET_WAIT_S
        while time.monotonic() < deadline:
            state = latest_sheet_state(self._store())
            if state and state.get('_ts', 0) >= self._run_started_at - 5:
                return True
            time.sleep(self.poll)
        return False

    # ==================== 公共循环钩子 ====================

    def _pumpkin_activity_config(self) -> dict:
        try:
            return {
                'team_no': int(self._params.get('team_no', 1)),
                'max_skips': int(self._params.get('max_skips', 4)),
                'difficulty': int(self._params.get('difficulty', 1)),
            }
        except (TypeError, ValueError):
            return {}

    def _pumpkin_team_error(self, cfg: dict, team_no: int):
        if not 1 <= int(team_no) <= 5:
            return f"[南瓜] 部队编号应是 1-5（收到 {team_no}）"
        return None

    def _pumpkin_identify_supported(self) -> bool:
        return False  # 日服死板版：不啃剪影库，判定全走数据同步

    def _pumpkin_takeover_requested(self, remaining: int) -> bool:
        return False  # 远征排班接管是国服设施，日服没有

    def _pumpkin_goto_sally(self):
        missing = self._preflight()
        if missing:
            yield "[南瓜] 日服画面素材没标定，先干不了活。缺这些模板："
            for item in missing:
                yield f"[南瓜]   ✗ {item}"
            yield "[南瓜] 模板要从日服实际运行画面裁剪标定后再来"
            return False

        def _go():
            self._navigate(PUMPKIN_ROUTE, frozenset({'pumpkin'}), '前往活动界面')

        logs, error = self._run_logged(_go)
        for line in logs:
            yield f"[南瓜] {line}"
        if error is not None:
            yield f"[南瓜] ✗ {error}"
            return False
        return True

    def _pumpkin_enter_event(self, cfg: dict, difficulty: int):
        if difficulty:
            tab = f'pumpkin/tab-{int(difficulty)}'
            logs, error = self._run_logged(
                lambda: self._click_hit(tab, f'难度页签{difficulty}'))
            for line in logs:
                yield f"[南瓜] {line}"
            if error is not None:
                yield f"[南瓜] ✗ 切难度失败：{error}"
                return False
        # 进活动页会触发游戏拉 /sally，板子状态经数据同步落账；
        # 等不到新鲜数据 = 数据同步没在跑，判定无源，停
        if not self._wait_sheet_fresh():
            yield "[南瓜] ✗ 等不到数据同步的板子状态——数据同步没在跑？先开数据同步再来"
            return False
        return True

    def _ensure_on_board(self, cfg: dict) -> bool:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            image = self.device.screenshot(force=True)
            if self.classify(image) == 'pumpkin':
                return True
            self._skip_tap()
            time.sleep(1.0)
        return False

    def _skip_tap(self) -> None:
        image = self.device.screenshot(force=True)
        self._click_near('pumpkin/screen', PUMPKIN_SKIP_OFFSET,
                         '安全区', image=image)

    def _pumpkin_board_full(self, cfg: dict) -> bool:
        state = latest_sheet_state(self._store())
        if not state:
            return False  # 没数据不算满；看门狗会兜底
        sheet = state['sheets'].get(str(cfg.get('difficulty') or 1))
        return bool(sheet and sheet.get('is_full'))

    def _pumpkin_read_obtained(self, cfg: dict):
        return None  # 日服不读获得动画；掉落由数据同步的刀帐差分照单全收

    def _pumpkin_skip_safe(self, cfg: dict, times: int):
        for _ in range(max(1, int(times))):
            self._skip_tap()
            time.sleep(0.8)

    def _pumpkin_after_battle_debug(self, cfg: dict, debug_dir: str,
                                    battles_total: int):
        return None  # 剪影数据采集是国服设施

    def _identify_board_stream(self, cfg: dict, watch_names: list):
        # 不会被调到（_pumpkin_identify_supported 恒 False），保底占位
        self._identify_decision = None
        self._identify_name = None
        yield "[南瓜] 日服不支持剪影认人"
        return

    def _start_battle_stream(self, cfg: dict, team_no: int):
        """部隊選択 → 选部队 → 出撃。结果放在 self._battle_started。

        日服活动战斗不受伤（2026-11-05 实测），但出发后出现的任何确认
        弹窗一律停——弹窗语义没校准前不替玩家点。"""
        self._battle_started = False

        logs, error = self._run_logged(
            lambda: self._click_hit('pumpkin/deploy', '部隊選択按钮'))
        for line in logs:
            yield f"[南瓜] {line}"
        if error is not None:
            yield f"[南瓜] 没找到部队选择按钮（{error}）"
            return
        try:
            self._wait(frozenset({'team'}), jp_sally.NAV_TIMEOUT,
                       frozenset({'loading'}), '打开部队选择')
        except jp_sally.SallyStop:
            yield "[南瓜] 部队选择界面没打开"
            return

        numeral = NUMERAL[team_no - 1]
        logs, error = self._run_logged(lambda: (
            self._click_near('team/depart', jp_sally.TEAM_TAB_OFFSETS[team_no],
                             f'部队{numeral}页签'),
            time.sleep(1.0),
            self._click_hit('team/depart', '出撃按钮')))
        for line in logs:
            yield f"[南瓜] {line}"
        if error is not None:
            yield f"[南瓜] ✗ {error}"
            return

        # 验证真出去了：离开部队屏 = 出发；弹窗一律停；
        # 还在部队屏 = 被游戏拒了（编队不满足格子条件）
        for _ in range(6):
            time.sleep(1.5)
            image = self.device.screenshot(force=True)
            screen = self.classify(image)
            if screen in ('loading', 'battle', 'field', 'result', 'unknown'):
                self._battle_started = True
                yield "[南瓜] 出发，进入战斗循环"
                return
            if screen in ('dialog', 'confirm', 'automarch_dialog'):
                self._save_debug(image)
                self._abort = ("[南瓜] ⚠️ 出发后出现确认弹窗，日服弹窗语义还没校准，"
                               "已停止——你去看下游戏画面（截图已存 debug）")
                return
        self._save_debug(self.device.screenshot(force=True))
        yield "[南瓜] 出撃被游戏拒了，还停在部队选择界面——编队可能不满足格子条件"

    def _pumpkin_battle_loop(self, cfg: dict, battle_debug, fought: int):
        """盯完这场自动战斗：回到活动页 = 收圈。弹窗/卡死立即停。"""
        del battle_debug, fought  # 数据采集是国服设施
        deadline = time.monotonic() + BATTLE_TIMEOUT_S
        last = (None, None)
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            image = self.device.screenshot(force=True)
            screen = self.classify(image)
            if screen == 'pumpkin':
                return True
            if screen in ('dialog', 'confirm', 'automarch_dialog'):
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '战斗途中出现了确认弹窗，已停止；请人工查看游戏')
            digest = jp_sally._digest(image)
            if (screen, digest) != last:
                last = (screen, digest)
                last_change = time.monotonic()
            elif screen != 'battle' and \
                    time.monotonic() - last_change > self.stuck_seconds:
                self._save_debug(image)
                raise jp_sally.SallyStop(
                    '画面长时间没有变化，已停止；请人工查看游戏')
            time.sleep(self.poll)
        return False

    def _refresh_board_stream(self, cfg: dict, auto_refill: bool = False):
        """シート更新 → 确认。结果放在 self._refresh_ok。

        更新手形不足时的购买弹窗不认识（也没打算认识）：确认按钮模板
        没出现就如实收工，不碰小判。"""
        del auto_refill  # 日服不碰小判自动补充
        self._refresh_ok = False

        logs, error = self._run_logged(
            lambda: self._click_hit('pumpkin/update', 'シート更新按钮'))
        for line in logs:
            yield f"[南瓜] {line}"
        if error is not None:
            yield "[南瓜] 找不到剪影更新按钮"
            return

        confirm = None
        for _ in range(4):
            image = self.device.screenshot(force=True)
            confirm = jp_vision.find(image, 'pumpkin/update-confirm')
            if confirm:
                break
            time.sleep(1.0)
        if not confirm:
            yield "[南瓜] 剪影更新的确认弹窗一直没出来（更新手形烧完了，或者有弹窗挡路）"
            return
        self.device.click(confirm)
        time.sleep(2.0)

        # 校验：回活动页 + 部隊選択按钮回来了 = 更新成功
        if not self._ensure_on_board(cfg):
            yield "[南瓜] 更新完回不到活动界面"
            return
        image = self.device.screenshot(force=True)
        if jp_vision.find(image, 'pumpkin/deploy'):
            self._refresh_ok = True
            yield "[南瓜] 剪影更新完成，新板子就位"
        else:
            yield "[南瓜] 更新完部队选择按钮没回来"


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
            'max_skips': int(params.get('max_skips', 4)),
            'difficulty': int(params.get('difficulty', 1)),
        }
    except (TypeError, ValueError):
        raise ValueError('参数格式不正确')
    try:
        with jp_game_device.JpGameDevice(jp_browser.JpBrowser()) as device:
            host = JpPumpkin(device, normalized)
            for msg in host.pumpkin_stream():
                print(msg, flush=True)
    except jp_vision.TemplateNotFound as exc:
        raise FlowAborted(str(exc)) from exc
    except jp_sally.SallyStop as exc:
        raise FlowAborted(str(exc)) from exc
    yield from ()
