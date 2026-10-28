# -*- coding: utf-8 -*-
"""日服远征手动派遣（阶段2试点）：与国服共用同一份官方流程单
（resource/base/flows/expedition.json，navigate → 选图积木 → 安全派遣积木）
和同一份素材包机制；日服差异全部收在 jp 素材包和本宿主里。

安全底线（复刻 jp_sally / 国服远征的保命语义）：

- 只点认得出的画面：时代卡/小图卡/部队页签坐标、部隊選択/出撃/确认按钮
  一律经素材包解析，缺标定就明确列出缺什么然后停，绝不伪造、绝不盲点。
- 验伤走队伍数据通道（jp_party）：重伤/状态不明/数据不新鲜一律停——
  日服没有国服重伤章兜底，「不知道」就是停。
- 派遣结果要游戏亲口确认（确认点之后的数据说部队「远征中」，或画面 OCR
  认出「遠征中」），确认不了就不记账、如实报失败，绝不假绿。
- 记录只写日服数据目录（JP_DATA_DIR），不碰国服 state/账房。

真机标定（2026-10-28 全部从 JpBrowser 实跑帧制作/标定完毕）：
模板 expedition/screen、expedition/team-screen、expedition/team-select、
expedition/depart、directory/expedition、menu/expedition 已入素材包；
点 expedition/era-1..5、expedition/map-1..4、expedition/team-1..5、
expedition/skip 已注册并逐点实测（未解锁的时代/图卡点击无反应属账号
进度预期）。实测确认：日服远征点「遠征開始」直接出发，无二次确认弹窗，
expedition/confirm、expedition/confirm-accept 仅作可选支持，未标定。
"""
from __future__ import annotations

import copy
import json
import re
import time

from . import jp_browser, jp_game_device, jp_ocr, jp_party, jp_sally
from .device_adapter import Point
from .runtime_paths import JP_DATA_DIR

DEFAULT_MAP_NAME = '鳥羽・伏見'      # 一-一，run_script 缺省图名（日服必填）
SCRIPT_NAME = 'jp-expedition'
PROOF_TIMEOUT_S = 15.0               # 等游戏亲口确认派遣成功的上限
SCROLL_WAIT_S = 4.0                  # 「出发远征」卷轴过场先让它跑完

NUMERAL = '一二三四五'

# 远征接续路线：与出阵同款目录大页进（不透明，景趣影响不到），
# 指挥菜单只作已打开时的接续。点击目标全部是待标定的远征素材。
# 出阵的三个选择屏（时代/小图/部队，右上都有目録按钮）经目録回目录大页
# 再转远征；行军/战斗/结算屏不接续，交给引擎兜底回本丸。
EXPEDITION_ROUTE = {
    'home': ('hit', 'home/catalog', '目录按钮'),
    'directory': ('hit', 'directory/expedition', '目录远征入口'),
    'formation': ('near', 'formation/label', jp_sally.BACK_ARROW_OFFSET,
                  '编成页返回箭头'),
    'shiki_menu': ('hit', 'menu/expedition', '远征入口'),
    'era': ('hit', 'home/catalog', '出阵时代屏目录按钮'),
    'map': ('hit', 'home/catalog', '出阵选图屏目录按钮'),
    'team': ('hit', 'home/catalog', '出阵部队屏目录按钮'),
}

# 弹窗扫地收工画面：认得出这些 = 画面干净；unknown 一律如实报扫不干净。
_CLEAN_SCREENS = frozenset({
    'home', 'directory', 'shiki_menu', 'formation', 'era', 'map', 'team',
    'mapview', 'result', 'field', 'battle', 'expedition', 'expedition-team',
})


def _resolve_refs(pack, refs: dict):
    """素材引用经包解析；缺的逐条列出（绝不静默、绝不给假值）。

    返回 ({key: 解析值}, [缺什么])。pack 为 None（流程外直调）也算缺。"""
    assets, missing = {}, []
    for key, (kind, ref) in refs.items():
        if pack is None:
            missing.append(f'{ref}（本次执行没加载素材包）')
            continue
        try:
            assets[key] = (pack.resolve_point(ref) if kind == 'point'
                           else pack.resolve_template(ref))
        except ValueError as exc:
            missing.append(f'{ref}（{exc}）')
    return assets, missing


def _pick_map_refs(era: int, map_slot: int) -> dict:
    return {'era': ('point', f'@expedition/era-{era}'),
            'map': ('point', f'@expedition/map-{map_slot}'),
            'team_select': ('template', '@expedition/team-select'),
            'team_screen': ('template', '@expedition/team-screen')}


def _dispatch_refs(team_no: int) -> dict:
    # 确认弹窗素材不在必需清单：日服远征实测点「遠征開始」直接出发，
    # 无二次确认；confirm/accept 仅作可选支持（出现才点，见派遣第 4 步）。
    return {'team': ('point', f'@expedition/team-{team_no}'),
            'depart': ('template', '@expedition/depart'),
            'team_screen': ('template', '@expedition/team-screen')}


def _optional_confirm_refs(pack) -> dict:
    """可选的确认弹窗素材：缺了不拦（日服远征实测无此弹窗）。"""
    if pack is None:
        return {}
    assets = {}
    for key, ref in (('confirm', '@expedition/confirm'),
                     ('accept', '@expedition/confirm-accept')):
        try:
            assets[key] = pack.resolve_template(ref)
        except ValueError:
            pass
    return assets


class JpExpedition(jp_sally.JpNavigator):
    """日服远征宿主：既是 flow_engine 的 agent（navigate_to_stream /
    两个远征积木方法 / record_event / team_state_reader），也是走
    官方流程单的状态机。device 用 JpGameDevice，watch 用 jp_party.PartyWatch。"""

    # 远征画面/远征部队页插在覆盖层（前 6 条）之后、出阵各屏之前：
    # 弹窗/公告永远优先认，避免盖在页面上时被当成底层画面。
    SCREENS = (jp_sally.SCREENS[:6]
               + (('expedition-team', ('expedition/team-screen',)),
                  ('expedition', ('expedition/screen',)))
               + jp_sally.SCREENS[6:])

    def __init__(self, device, watch=None, log=print, poll: float = 2.0,
                 stuck_seconds: float = jp_sally.STUCK_SECONDS,
                 save_debug=None):
        super().__init__(device, log=log, poll=poll,
                         stuck_seconds=stuck_seconds, save_debug=save_debug)
        self.watch = watch
        self.config = {}
        self.current_location = None
        self._last_pack = None
        # 流程引擎 team_state_reader 注入缝（team_state_reader.py）的日服实现
        self.team_state_reader = self._team_state_reader

    # ==================== 流程引擎宿主接口 ====================

    def navigate_to_stream(self, target: str, max_retries: int = 3):
        """引擎 navigate 步的宿主实现：只支持日服认识的界面名。

        「远征」走目录大页接续（与出阵同款的已实证导航）；「本丸」用
        页面刷新 + 启动序列——刷新后只点认得出的启动按钮，是日服已实证的
        安全回本丸姿势（不依赖任何未标定的返回按钮）。"""
        del max_retries  # 日服导航自带重试/熔断语义
        if target == '远征':
            yield '[远征] 正在导航到远征…'
            logs, error = self._run_logged(self._navigate_to_expedition)
            yield from logs
            if error is not None:
                yield f'[远征] ✗ {error}'
                return
            yield '[远征] ✓ 已到达 远征'
            return
        if target == '本丸':
            if self.current_location == '本丸':
                yield '[导航] 已在本丸'
                return
            yield '[导航] 刷新页面回本丸…'

            def _go_home():
                self.device.reload()
                self._boot_to_home()

            logs, error = self._run_logged(_go_home)
            yield from logs
            if error is not None:
                yield f'[导航] ✗ 没能回本丸：{error}'
                return
            self.current_location = '本丸'
            yield '[导航] ✓ 已到达 本丸'
            return
        yield f'✗ 日服导航不认识目标 {target!r}（只支持 远征 / 本丸）'

    def _run_logged(self, fn):
        """跑一段会经 self.log 输出的导航；SallyStop 不外抛，交给调用方播报。

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

    def record_event(self, event_type: str, **payload):
        """日服账房：只写 JP_DATA_DIR 的独立账房库，与国服互不可见。"""
        try:
            from .telemetry import get_jp_telemetry_store
            return get_jp_telemetry_store().record_event(event_type, payload)
        except Exception:
            return None

    def _popup_sweep(self, max_rounds: int = 10) -> bool:
        """日服弹窗扫地：只点认得出的公告栏/确认窗/启动提示；认不出如实报 False。"""
        for _ in range(max(1, int(max_rounds))):
            image = self.device.screenshot(force=True)
            screen = self.classify(image)
            if screen == 'notice':
                self.log('公告栏开着，点掉')
                self._click_hit('notice/close', '公告栏关闭', image=image)
            elif screen == 'confirm':
                self.log('确认窗默认选否')
                self._click_hit('dialog/decline', '确认窗いいえ', image=image)
            elif screen == 'automarch_dialog':
                self._click_near('dialog/automarch-request',
                                 jp_sally.DIALOG_CLOSE_OFFSET,
                                 '关闭弹窗', image=image)
            elif screen == 'boot_arrow':
                self._click_hit('boot/volume-arrow', '音量提示箭头', image=image)
            elif screen == 'boot_honmaru':
                self._click_hit('boot/honmaru-button', '标题页本丸按钮',
                                image=image)
            elif screen == 'loading':
                time.sleep(self.poll)
                continue
            elif screen in _CLEAN_SCREENS:
                return True
            else:
                self._save_debug(image)
                return False
            time.sleep(0.8)
        return False

    def _open_menu(self, max_attempts: int = 10, settle_seen=None) -> bool:
        """日服的「目录」= 目録大页：从本丸点目录按钮进去，已在目录页直接算开。"""
        del settle_seen  # 目录页不透明，没有国服结算屏压目录的救援分支
        if self.current_location == '通用入口':
            return True
        for _ in range(max(1, int(max_attempts))):
            image = self.device.screenshot(force=True)
            screen = self.classify(image)
            if screen == 'directory':
                self.current_location = '通用入口'
                return True
            if screen == 'loading':
                time.sleep(self.poll)
                continue
            if screen == 'notice':
                self._click_hit('notice/close', '公告栏关闭', image=image)
            elif screen == 'confirm':
                self._click_hit('dialog/decline', '确认窗いいえ', image=image)
            elif screen == 'home':
                self._click_hit('home/catalog', '目录按钮', image=image)
            else:
                return False
            time.sleep(0.8)
        return False

    def _skip_point(self, point=None):
        """日服安全点：只认素材包标定（@expedition/skip）或显式入参。

        宁可报缺也不漏出国服默认安全点坐标（(775,695) 是国服 1280×720
        空间的值，落在日服基准空间上是错的）。"""
        if point is not None:
            return (point[0], point[1])
        pack = self._last_pack
        if pack is not None:
            try:
                return pack.resolve_point('@expedition/skip')
            except ValueError:
                pass
        raise jp_sally.SallyStop('日服安全点未标定（素材包缺 @expedition/skip），已停止')

    def skip_safe(self, times: int = 3, interval: float = 0.8, point=None):
        """点安全区跳动画；安全点没标定就停（见 _skip_point）。"""
        pt = self._skip_point(point)
        for _ in range(max(1, int(times))):
            self.device.click(Point(pt[0], pt[1]))
            time.sleep(interval)

    # ==================== 远征导航 ====================

    def _navigate_to_expedition(self) -> str:
        """走到远征画面。支持本丸/目录/指挥菜单/编成等现场接续。"""
        screen = self._navigate(EXPEDITION_ROUTE, frozenset({'expedition'}),
                                '前往远征')
        self.current_location = '远征'
        return screen

    # ==================== 官方流程单的两个内置积木 ====================

    def _expedition_pick_map_stream(self, era, map_slot, map_name,
                                    pack=None):
        """选时代卡 → 选小图卡（OCR 对图名）→ 点「部隊選択」进部队选择页。

        全部坐标/模板经素材包解析；缺标定明确列出并停（绝不盲点），
        选图没确认成功绝不放行—— safe_dispatch 不会再被调用。"""
        tag = '[远征]'
        self._last_pack = pack
        try:
            era = int(era)
        except (TypeError, ValueError):
            yield f'{tag} ✗ 时代编号不是数字，停'
            return False
        if not 1 <= era <= 5:
            yield f'{tag} ✗ 时代编号要在 1-5 之间，收到 {era}，停'
            return False
        if map_slot is None:
            yield f'{tag} ✗ 日服选图必须给小图卡位（1-4），停'
            return False
        try:
            map_slot = int(map_slot)
        except (TypeError, ValueError):
            yield f'{tag} ✗ 小图卡位不是数字，停'
            return False
        if not 1 <= map_slot <= 4:
            yield f'{tag} ✗ 小图卡位要在 1-4 之间，收到 {map_slot}，停'
            return False
        map_name = str(map_name).strip() if map_name else ''
        if not map_name:
            yield f'{tag} ✗ 日服派遣必须告诉我要去哪个图（map_name 必填），停'
            return False

        assets, missing = _resolve_refs(pack, _pick_map_refs(era, map_slot))
        if missing:
            yield f'{tag} ✗ 日服远征素材还没标定齐，缺：' + '；'.join(missing)
            yield f'{tag} ✗ 缺素材不派遣（绝不盲点），请用真机帧标定后再跑'
            return False

        # 0. 必须在远征画面：流程单的 navigate 步负责带到，直调时自己走
        if self.current_location != '远征':
            yield f'{tag} 先走到远征画面…'
            logs, error = self._run_logged(self._navigate_to_expedition)
            yield from logs
            if error is not None:
                yield f'{tag} ✗ {error}'
                return False
            if self.current_location != '远征':
                yield f'{tag} ✗ 没能到达远征画面，停'
                return False

        # 1./2. 选时代卡 → 选小图卡（点完必须 OCR 对上图名才算数，
        #        点歪了会把队伍派去错误的图，而第 5 步的证明只认「远征中」）
        self.device.screenshot(force=True)
        selected = False
        for attempt in range(2):
            if attempt:
                yield f'{tag} 没确认选中目标图，重选一次'
            yield f'{tag} 🗺️ 翻去时代{era}…'
            self.device.click(Point(*assets['era']))
            time.sleep(1.0)
            yield f'{tag} 🗺️ 点小图卡位{map_slot}…'
            self.device.click(Point(*assets['map']))
            time.sleep(1.0)
            if self._confirm_map_name(map_name):
                selected = True
                break
        if not selected:
            yield f'{tag} ✗ 两次都没确认选中「{map_name}」，怕派错队伍，停'
            return False
        yield f'{tag} ✓ 已选中「{map_name}」'

        # 3. 点「部隊選択」→ 等部队选择界面（条件不满足按钮可能是灰的）
        team_ui_ok = False
        for attempt in range(12):
            self.device.screenshot(force=True)
            if self.device.exists(assets['team_screen']):
                team_ui_ok = True
                break
            if attempt <= 3:
                deploy = self.device.template_match(assets['team_select'])
                if deploy:
                    self.device.click(deploy)
                    time.sleep(1.5)
            time.sleep(0.5)
        if not team_ui_ok:
            yield f'{tag} ✗ 部队选择界面没打开（条件不满足按钮可能是灰的？），停'
            return False
        return True

    def _expedition_safe_dispatch_stream(self, team_no, era, map_slot,
                                         map_name, pack=None):
        """选部队 → 【保命】数据验伤 → 出撃 → 认得的确认 → 游戏结果证明 → 记账。

        与国服远征同款语义：重伤绝不派遣；日服没有重伤章兜底，验伤数据
        说不明白就直接停。证明有两条路（数据说「远征中」/画面认出「遠征中」），
        都说不清就报失败、绝不记账、绝不假绿。"""
        tag = '[远征]'
        self._last_pack = pack
        try:
            team_no = int(team_no)
        except (TypeError, ValueError):
            yield f'{tag} ✗ 部队编号不是数字，停'
            return False
        if not 1 <= team_no <= 5:
            yield f'{tag} ✗ 部队编号要在 1-5 之间，收到 {team_no}，停'
            return False
        map_name = str(map_name).strip() if map_name else ''

        assets, missing = _resolve_refs(pack, _dispatch_refs(team_no))
        if missing:
            yield f'{tag} ✗ 日服远征素材还没标定齐，缺：' + '；'.join(missing)
            yield f'{tag} ✗ 缺素材不派遣（绝不盲点），请用真机帧标定后再跑'
            return False

        # 0. 必须在部队选择页：画面认不出就停
        self.device.screenshot(force=True)
        if not self.device.exists(assets['team_screen']):
            yield f'{tag} ✗ 不在部队选择页（画面认不出），停'
            return False

        # 1. 选部队
        yield f'{tag} 点部队{NUMERAL[team_no - 1]}页签…'
        self.device.click(Point(*assets['team']))
        time.sleep(0.3)
        self.device.click(Point(*assets['team']))
        time.sleep(0.5)

        # 2. 【保命】数据验伤：数据缺失/不新鲜 → 刷新让游戏重报一次；
        #    重报仍不明白就停（宁停不放，日服没有重伤章兜底）
        snapshot, why = self._await_team_snapshot(team_no)
        if snapshot is None:
            yield f'{tag} {why}，刷新页面让游戏重报一次…'
            logs, error = self._run_logged(self._refresh_to_home)
            yield from logs
            if error is not None:
                yield f'{tag} ✗ 刷新重走失败：{error}'
                return False
            ok = yield from self._expedition_pick_map_stream(
                era, map_slot, map_name, pack=pack)
            if not ok:
                return False
            snapshot, why = self._await_team_snapshot(team_no)
        if snapshot is None:
            yield f'{tag} ✗ 重报后仍{why}，宁可停也不带盲区派遣'
            return False
        hurt = [m for m in snapshot.members if m.injury]
        heavy = [m for m in hurt if m.injury == '重伤']
        if heavy:
            names = '、'.join(m.name or f'#{m.serial_id}' for m in heavy)
            yield f'{tag} 🛑 {names}现在是重伤！按规矩绝不派遣，停。去修刀吧'
            return False
        if hurt:
            yield f'{tag} 带伤出发：' + '、'.join(
                f'{m.name or m.serial_id}（{m.injury}）' for m in hurt)
        else:
            yield f'{tag} 📋 验伤：全员满血'

        # 3. 点「出撃」（部队可能不满足该图条件，或仍在远征：认不出就停）
        self.device.screenshot(force=True)
        start = self.device.template_match(assets['depart'])
        if not start:
            yield f'{tag} ✗ 找不到出撃按钮；这支部队可能不满足该图条件，' \
                  f'或仍在远征，停止派遣'
            return False
        self.device.click(start)
        confirmed_at = time.time()
        time.sleep(1.5)

        # 4. 二次确认弹窗是可选的：日服远征实测点「遠征開始」直接出发
        #    （卷轴过场 + 状态变「遠征中」），无确认弹窗；若个别情形弹出，
        #    只点认得的「はい」（道具复选框不碰，默认不用）。
        confirm_assets = _optional_confirm_refs(pack)
        confirm_tpl = confirm_assets.get('confirm')
        accept_tpl = confirm_assets.get('accept')
        if confirm_tpl is not None and accept_tpl is not None:
            confirm = None
            for _ in range(4):
                self.device.screenshot(force=True)
                confirm = self.device.template_match(confirm_tpl)
                if confirm:
                    break
                time.sleep(0.5)
            if confirm:
                accept = self.device.template_match(accept_tpl)
                if not accept:
                    yield f'{tag} ✗ 认不出确认按钮，停（绝不盲点）'
                    return False
                self.device.click(accept)
                confirmed_at = time.time()
                yield f'{tag} 🐎 确认完毕，出发远征！过场动画跑着…'
            else:
                yield f'{tag} 🐎 无确认弹窗，直接出发！过场动画跑着…'
        else:
            yield f'{tag} 🐎 出发远征！过场动画跑着…'

        # 5. 结果证明：游戏亲口确认才算数；说不清就报失败、不记账
        proof = self._dispatch_proof(team_no, confirmed_at)
        if proof is False:
            yield f'{tag} ✗ 游戏没确认这次派遣成功（数据与画面都说不清），' \
                  f'停，你去看一眼'
            return False
        yield f'{tag} ✅ 部队{team_no}已出发：时代{era}「{map_name}」' \
              f'（{proof}确认），收工'
        if not self._record_dispatch(team_no, era, map_slot, map_name, proof):
            yield f'{tag} ⚠️ 派遣记录写失败（不影响已出发的事实），请同步近况'
        return True

    def _refresh_to_home(self) -> None:
        """刷新页面重走启动序列回原点（游戏重进本丸后会自己重报队伍数据）。"""
        self.device.reload()
        self._boot_to_home()
        self.current_location = None

    # ==================== 队伍数据（验伤/证明/注入缝） ====================

    def _team_snapshot(self, team_no: int):
        """新鲜且成员状态全齐的队伍快照；说不清返回 (None, 原因)。

        日服规矩：不知道就是停——没有国服重伤章模板兜底这条路。"""
        if self.watch is None:
            return None, '没有队伍数据通道'
        latest = self.watch.latest()
        if latest is None:
            return None, '还没收到队伍数据'
        captured_at, payload = latest
        if not 0 <= time.time() - captured_at <= jp_sally.PARTY_DATA_MAX_AGE:
            return None, '队伍数据不新鲜'
        snapshot = jp_party.build_team_snapshot(payload, team_no,
                                                now=captured_at)
        if snapshot is None or not snapshot.injury_known:
            return None, '队伍成员状态认不全'
        return snapshot, None

    def _await_team_snapshot(self, team_no: int):
        """轮询版 _team_snapshot：全量包过慢链路要一两拍，别瞬查空气。

        跟 jp_sally._await_party_data 同一宽限（PARTY_DATA_WAIT）；
        只在「还没收到」上等，其他原因（不新鲜/认不全）立刻返回。"""
        deadline = time.monotonic() + jp_sally.PARTY_DATA_WAIT
        while True:
            snapshot, why = self._team_snapshot(team_no)
            if snapshot is not None or why != '还没收到队伍数据':
                return snapshot, why
            if time.monotonic() >= deadline:
                return None, why
            time.sleep(0.5)

    def _team_state_reader(self, team_no, max_age=600, now=None):
        """team_state_reader 注入缝的日服实现：数据通道的新鲜队伍快照。"""
        if self.watch is None:
            return None
        latest = self.watch.latest()
        if latest is None:
            return None
        captured_at, payload = latest
        now = time.time() if now is None else now
        if not 0 <= now - captured_at <= max_age:
            return None
        return jp_party.build_team_snapshot(payload, team_no, now=captured_at)

    def _dispatch_proof(self, team_no: int, after: float):
        """游戏确认派遣成功返回 'party-data' / 'visual'；说不清返回 False。

        数据路：确认点之后来的队伍数据说该队「远征中」（status=2）。
        画面路：卷轴过场后全屏 OCR 认出「遠征中」。两条都没有就绝不猜。"""
        deadline = time.monotonic() + PROOF_TIMEOUT_S
        waited = False
        while time.monotonic() < deadline:
            if self.watch is not None:
                latest = self.watch.latest()
                if (latest and latest[0] >= after - 1.0
                        and jp_party.party_status(latest[1], team_no) == 2):
                    return 'party-data'
            if not waited:
                time.sleep(SCROLL_WAIT_S)   # 卷轴过场先让它跑完
                waited = True
                continue
            frame = self.device.screenshot(force=True)
            if frame is not None:
                try:
                    tokens = self._read_tokens(frame)
                except Exception:
                    tokens = []
                for text, _score, _cx, _cy in tokens:
                    if '遠征中' in str(text):
                        return 'visual'
            time.sleep(1.0)
        return False

    @staticmethod
    def _read_tokens(frame):
        return jp_ocr.read_tokens(frame)

    # ==================== 识别小件 ====================

    def _confirm_map_name(self, map_name: str) -> bool:
        """全屏 OCR 确认目标图名在画面上（选中后详情面板和卡列表都有名字）。

        只比汉字、双向包含，防 OCR 吃掉「・」或详略名差异（照抄国服
        _confirm_map_selected 的口径）。"""
        target = re.sub(r'[^一-鿿]', '', map_name or '')
        if len(target) < 2:
            return True  # 名字太短没法对，别挡路
        frame = self.device.screenshot(force=True)
        if frame is None:
            return False
        try:
            tokens = self._read_tokens(frame)
        except Exception:
            return False
        for text, _score, _cx, _cy in tokens:
            t = re.sub(r'[^一-鿿]', '', str(text))
            if len(t) >= 2 and (t in target or target in t):
                return True
        return False

    # ==================== 记录（只写日服数据目录） ====================

    def _record_dispatch(self, team_no, era, slot, map_name, proof) -> bool:
        """派遣记录写 JP_DATA_DIR/state/expeditions.json；写失败如实上报
        （不 undo 出发），账房事件照写日服库。绝不碰国服 state/账房。"""
        ok = True
        record = {'map_name': map_name, 'era': era, 'slot': slot,
                  'dispatched_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                  'proof': proof}
        try:
            path = JP_DATA_DIR / 'state' / 'expeditions.json'
            data = {}
            if path.exists():
                loaded = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    data = loaded
            data[str(team_no)] = record
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        except Exception as exc:
            ok = False
            self.log(f'[远征] ⚠️ 派遣记录写失败（不影响已出发的事实）: {exc}')
        self.record_event('expedition.dispatched', team_no=team_no, era=era,
                          slot=slot, map_name=map_name, proof=proof)
        return ok


def _load_official_expedition_flow() -> dict:
    from .flow_engine import find_flow
    flow = find_flow('expedition')
    if not flow:
        raise ValueError('官方流程「expedition」不存在（resource/base/flows/expedition.json）')
    return flow


def run_script(config_path=None, params: dict | None = None):
    """面板脚本入口 jp-expedition：generator[str]（日志走 print，worker 捕获）。

    深拷贝官方流程单填入实际参数后交给 run_flow(server='jp')——和国服
    跑的是同一份 JSON、同一套素材包机制。固定重伤即停；不做补花/收菜/
    排班/再派。安全停止（SallyStop/通道断连）转成 FlowAborted 按退出码 42
    收尾；缺素材/验伤不过等玩法内停止由流程成绩单的 ✗ 如实呈现。"""
    from .flow_control import FlowAborted
    params = params or {}
    try:
        team_no = int(params.get('team_no', 1))
        era = int(params.get('era', 1))
        map_slot = int(params.get('map_slot', 1))
    except (TypeError, ValueError):
        raise ValueError('参数格式不正确')
    if not 1 <= team_no <= 5:
        raise ValueError('部队番号应在 1-5 之间')
    if not 1 <= era <= 5:
        raise ValueError('时代编号应在 1-5 之间')
    if not 1 <= map_slot <= 4:
        raise ValueError('小图卡位应在 1-4 之间')
    map_name = str(params.get('map_name') or DEFAULT_MAP_NAME).strip()

    flow = copy.deepcopy(_load_official_expedition_flow())
    for step in flow.get('steps', []):
        step_params = step.get('params') or {}
        if step_params.get('name') == 'expedition_pick_map':
            step_params.update(era=era, map_slot=map_slot, map_name=map_name)
        elif step_params.get('name') == 'expedition_safe_dispatch':
            step_params.update(team_no=team_no, era=era, map_slot=map_slot,
                               map_name=map_name)

    try:
        with jp_game_device.JpGameDevice(jp_browser.JpBrowser()) as device:
            try:
                with jp_party.PartyWatch() as watch:
                    host = JpExpedition(device, watch=watch)
                    for message in _run_flow(host, flow, server='jp'):
                        print(message)
            except jp_party.PartyWatchError as exc:
                raise FlowAborted(str(exc)) from exc
    except jp_sally.SallyStop as exc:
        raise FlowAborted(str(exc)) from exc
    yield from ()


def _run_flow(host, flow, server):
    """独立小包装只为 run_script 里好 monkeypatch（与 flow_engine.run_flow 同物）。"""
    from .flow_engine import run_flow
    return run_flow(host, flow, server=server)
