# -*- coding: utf-8 -*-
"""南瓜大作战（刮刮乐/九宫格活动）的公共循环（与服务器无关）。

循环只负责编排与决策：令牌预算（令牌=一切，烧完就收工）、无进展看门狗、
打→翻格→满了更新→下一局的主循环、智能认人的二次复核（连续两次同名才
动手，防一次误读白烧令牌）、远征接管与事件记录。「板子满没满只认官方
凭据不靠猜」是血泪教训，两服共用。

画面识别、点击、配置和数据通道全是宿主画面适配层的钩子（国服实现见
pumpkin_cn.py，日服宿主见 touken/jp_pumpkin.py）；本层不碰 device、
config、OCR 词和像素坐标。

视图层钩子契约（各服实现，缺一不许跑）：

  _pumpkin_activity_config() -> dict
      玩法配置（空 dict = 未配置，循环直接收工）
  _pumpkin_team_error(cfg, team_no) -> str | None
      部队不可用时的报错文案；None = 可以打
  _pumpkin_identify_supported() -> bool
      本服是否支持剪影认人（智能版）；False 时按死板版全刷
  _pumpkin_takeover_requested(remaining) -> bool
      远征排班是否请求接管；实现方负责记下 remaining
  _pumpkin_goto_sally() -> 生成器，return bool
      导航到出阵入口
  _pumpkin_enter_event(cfg, difficulty) -> 生成器，return bool
      进入活动界面并切难度
  _ensure_on_board(cfg) -> bool
      确认/恢复在活动界面
  _pumpkin_board_full(cfg) -> bool
      板子 9 格全翻完的官方凭据（各服用各服的数据/画面通道，不靠猜）
  _pumpkin_read_obtained(cfg) -> str | None
      获得动画里认刀名；"" = 该认但认不出（循环记一条日志），
      None = 本服不读这个（循环保持安静）
  _pumpkin_skip_safe(cfg, times)
      点安全区驱散动画/弹窗
  _pumpkin_after_battle_debug(cfg, debug_dir, battles_total)
      数据采集钩子（debug_dir 为 None 时循环不会调）
  _identify_board_stream(cfg, watch_names) -> 生成器
      认剪影，结果写 _identify_decision / _identify_name
  _start_battle_stream(cfg, team_no) -> 生成器
      开一场战斗，结果写 _battle_started；必须停写 _abort
  _pumpkin_battle_loop(cfg, battle_debug, fought) -> 生成器，return bool
      盯完这场战斗，return 是否正常收圈
  _refresh_board_stream(cfg, auto_refill) -> 生成器
      剪影更新开新局，结果写 _refresh_ok

跨层结果属性（视图写、循环读）：
  _battle_started / _refresh_ok / _abort / _identify_decision / _identify_name
"""
from __future__ import annotations

import time

# ── 中日刀名别名映射 ──
# swings.json 有 name（日文）和 name_zh（中文），用这个表让用户写中文也能匹配剪影库的日文名
_NAME_ALIAS_CACHE = None


def _name_to_jp(name: str) -> str:
    """把中日文统一洗成日文标准名（用于剪影识别对比）"""
    global _NAME_ALIAS_CACHE
    if _NAME_ALIAS_CACHE is None:
        try:
            from ..sword_db import all_swords
            swords = all_swords()
            alias = {}
            for sid, info in swords.items():
                jp = info["name"]
                alias[jp] = jp
                zh = info.get("name_zh", "")
                if zh:
                    alias[zh] = jp
            _NAME_ALIAS_CACHE = alias
        except Exception:
            _NAME_ALIAS_CACHE = {}  # 加载失败就当没别名
    return _NAME_ALIAS_CACHE.get(name, name)  # 不在名册里的原样返回


class PumpkinLoopMixin:
    """南瓜公共循环；UI 与数据触点全部是钩子。"""

    _battle_started: bool = False
    _refresh_ok: bool = False
    _abort: str = ""  # 非空 = 必须立刻停的原因
    _identify_decision = None   # None=认不准 / "keep"=目标刀 / "skip"=烧令牌换板子
    _identify_name = None       # 本次认出的名字（仅 decision 非 None 时有值）

    def pumpkin_stream(self, max_skips: int = None, team_no: int = None,
                       difficulty: int = None, debug_dir: str = None,
                       watch_names: list = None, auto_refill: bool = False, **kwargs):
        """
        流式刷南瓜大作战

        Args:
            max_skips: 最多烧几枚更新令牌（令牌 = 一切，烧完就收工）。
                       默认读配置，再默认 4。
            team_no: 部队编号，默认读配置 pumpkin.team_no
            difficulty: 难度 1初级/2中级/3上级，None=不点（用游戏当前 tab）
            debug_dir: 剪影识别数据采集目录（给了就存每帧战斗画面+每次回板子截图）
            watch_names: 智能版目标刀名单（如 ["小竜景光"]），None/空=死板版全刷
        """
        cfg = self._pumpkin_activity_config()
        if not cfg:
            yield "[南瓜] 未配置南瓜大作战"
            return

        if auto_refill:
            yield ("[南瓜] 自动补充目前仅为占位：新版更新令牌购买后不会正确刷新并关闭弹窗；"
                   "本次按关闭处理，令牌不足时不消费小判")
            auto_refill = False

        team_no = team_no or cfg.get("team_no", 3)
        if max_skips is None:
            max_skips = cfg.get("max_skips", 4)
        min_battles = cfg.get("identify_min_battles", 4)  # 第几场起开始认（翻太少认了也不准）
        watch_names = [n.strip() for n in (watch_names or []) if n and n.strip()]
        smart = bool(watch_names)
        if smart and not self._pumpkin_identify_supported():
            yield "[南瓜] 本服暂不支持剪影认人，按死板版全刷"
            smart = False

        team_error = self._pumpkin_team_error(cfg, team_no)
        if team_error:
            yield team_error
            return

        self._abort = ""
        if smart:
            # 通过 swords.json 把中文名转成日文标准名再匹配（不然"大般若长光"对不上"大般若長光"）
            canon = []
            unknown = []
            for n in watch_names:
                jp = _name_to_jp(n)
                if jp == n and n not in _NAME_ALIAS_CACHE:  # 没命中任何别名
                    unknown.append(n)
                canon.append(jp)
            watch_names = canon
            if unknown:
                yield f"[南瓜] ⚠️ 以下名字名册没找到（会原样匹配）：{unknown}"
            yield f"[南瓜] 智能模式：只刷 {watch_names}，认出别的刀就烧令牌换板子（上限 {max_skips} 枚）"

        # ========== 1. 导航到出阵 ==========
        yield "[南瓜] 正在导航到出阵..."
        reached = yield from self._pumpkin_goto_sally()
        if not reached:
            yield "[南瓜] 到达出阵失败"
            return

        # ========== 2. 进入活动界面 ==========
        entered = yield from self._pumpkin_enter_event(cfg, difficulty)
        if not entered:
            yield "[南瓜] 进不去活动界面（南瓜过季了？）"
            return
        yield "[南瓜] 到达南瓜大作战界面"
        if hasattr(self, "set_progress"):
            self.set_progress("pumpkin")

        # ========== 3. 主循环：打 → 翻格 → 满了更新 → 下一局 ==========
        # 板子满没满看官方凭据（视图钩子），不靠猜——
        # 猜错的代价是误点剪影更新白烧一枚更新令牌（血泪教训）
        boards_done = 0
        battles_total = 0
        misses = 0  # 连续开不出战斗的次数，防死循环
        board_battles = 0       # 当前这块板子打了几场（认剪影用，换板子清零）
        keep_confirmed = False  # 当前板子已确认是目标刀，不用再认
        pending_name = None     # 上一次认出的名字，连续两次一致才动手（防一次误读烧令牌）
        skips = 0               # 烧掉的更新令牌数（令牌 = 一切，烧完就收工）
        got_names = []          # 打满九宫格拿到的刀，记个日志
        last_progress = time.time()  # 无进展超时看门狗：格子+1/令牌+1/拿刀 都会刷新

        def _budge():
            """令牌预算还剩几枚（只有剪影更新/刷新才消耗；打板子不耗令牌）"""
            return max_skips - skips

        def _bump_progress():
            """记录一次有效进展（出阵/换板/拿刀），喂给无进展看门狗"""
            nonlocal last_progress
            last_progress = time.time()

        while True:
            if self._abort:
                yield self._abort
                return

            # 无进展看门狗：180 秒没有任何进展 → 疑似识别卡死，强制停
            if time.time() - last_progress > 180:
                yield f"[南瓜] ⚠️ 已 {int(time.time() - last_progress)} 秒无进展（出阵/换板/拿刀都没有），疑似卡死，强制停。你去看下模拟器画面"
                return

            if self._pumpkin_takeover_requested(max_skips - skips):
                yield "[南瓜] 🚩 远征排班请求接管：不开新圈，安全收工"
                break

            # 3.1 确保在活动界面（获得动画/弹窗可能盖着，点安全区扒拉掉）
            if not self._ensure_on_board(cfg):
                yield "[南瓜] 回不到活动界面，卡在未知画面，停"
                return

            # 3.2 板子真满了？→ 这块拿到了刀，记下是谁；预算够就更新开新局
            if self._pumpkin_board_full(cfg):
                yield "[南瓜] 面板全解锁，这块拿到了刀"
                _bump_progress()
                # 获得动画里认一下是谁（记日志用）
                got = self._pumpkin_read_obtained(cfg)
                if got:
                    got_names.append(got)
                    if hasattr(self, "record_event"):
                        self.record_event("pumpkin.sword_obtained", name=got,
                                          obtained_count=len(got_names),
                                          boards_completed=boards_done + 1)
                    yield f"[南瓜] 🎉 获得【{got}】（累计 {len(got_names)} 把）"
                elif got is not None:
                    yield "[南瓜] 获得动画没 OCR 出名字（继续）"
                # 点掉获得动画
                self._pumpkin_skip_safe(cfg, 3)
                boards_done += 1
                if hasattr(self, "record_event"):
                    self.record_event("pumpkin.board_completed",
                                      sequence=boards_done,
                                      sorties=battles_total,
                                      tokens_used=skips)
                # 预算还够 → 剪影更新开新局；不够 → 打满收工（不再白烧）
                if _budge() <= 0:
                    yield f"[南瓜] 令牌预算用完了（{skips}/{max_skips}），最后这块已打完，收工"
                    break
                yield "[南瓜] 点剪影更新开新局..."
                for msg in self._refresh_board_stream(cfg, auto_refill=auto_refill):
                    yield msg
                if not self._refresh_ok:
                    yield "[南瓜] 剪影更新没生效（令牌烧完了，或者有弹窗没驱散掉），收工"
                    return
                skips += 1
                if hasattr(self, "record_event"):
                    self.record_event("pumpkin.token_used", reason="new_board",
                                      used=skips, boards_completed=boards_done)
                misses = 0
                board_battles = 0
                keep_confirmed = False
                pending_name = None
                yield f"[南瓜] ===== 第 {boards_done} 块板子刷完，新板子开刷（令牌 {skips}/{max_skips}）====="
                continue

            # 3.25 智能认人：打够场数后每场回来认一次剪影，
            # 连续两次认出同一个名字才动手——是目标就刷完，不是就烧令牌换板子
            if smart and not keep_confirmed and board_battles >= min_battles:
                for msg in self._identify_board_stream(cfg, watch_names):
                    yield msg
                decision = self._identify_decision
                if decision in ("keep", "skip"):
                    if self._identify_name != pending_name:
                        pending_name = self._identify_name
                        yield f"[南瓜] 先记住【{pending_name}】，再打一场复核，免得误烧令牌"
                    elif decision == "keep":
                        keep_confirmed = True
                        yield f"[南瓜] 复核一致，就是【{pending_name}】，安心刷完"
                    else:
                        # 烧令牌前先看预算，不够就直接收工（不白烧）
                        if _budge() <= 0:
                            yield f"[南瓜] 令牌预算用完了（{skips}/{max_skips}），收工"
                            return
                        yield f"[南瓜] 复核一致，是【{pending_name}】，确认不要，烧令牌"
                        for msg in self._refresh_board_stream(cfg, auto_refill=auto_refill):
                            yield msg
                        if not self._refresh_ok:
                            yield "[南瓜] 剪影更新没生效（令牌烧完了，或者有弹窗没驱散掉），收工"
                            return
                        skips += 1
                        if hasattr(self, "record_event"):
                            self.record_event("pumpkin.token_used", reason="skip_sword",
                                              used=skips, sword=pending_name,
                                              boards_completed=boards_done)
                        board_battles = 0
                        keep_confirmed = False
                        pending_name = None
                        misses = 0
                        _bump_progress()
                        yield f"[南瓜] ===== 换新板子（令牌 {skips}/{max_skips}），接着认 ====="
                        continue

            # 3.3 尝试开一场战斗
            for msg in self._start_battle_stream(cfg, team_no):
                yield msg
            if self._abort:
                yield self._abort
                return

            if self._battle_started:
                misses = 0
                battle_debug = None
                if debug_dir:
                    battle_debug = f"{debug_dir}/b{battles_total + 1:02d}"
                round_done = yield from self._pumpkin_battle_loop(
                    cfg, battle_debug, battles_total)
                battles_total += 1  # 南瓜一次出阵=一场，按出阵次数计
                board_battles += 1
                if not round_done:
                    yield f"[南瓜] ⚠️ 战斗循环超过安全上限，强制停（共出阵 {battles_total} 次），你去看看卡哪了"
                    return
                if hasattr(self, "record_event"):
                    self.record_event("pumpkin.sortie_completed",
                                      sequence=battles_total,
                                      board_sorties=board_battles,
                                      boards_completed=boards_done)
                yield f"[南瓜] 第 {battles_total} 次出阵回来，格子 +1"
                _bump_progress()
                # 回来后可能有"获得刀剑男士"动画，点几下安全区
                self._pumpkin_skip_safe(cfg, 3)
                if debug_dir:
                    self._pumpkin_after_battle_debug(cfg, debug_dir, battles_total)
                continue

            # 3.4 板子没满但开不了战斗 → 狐狸奖励弹窗之类的挡路，驱散再试
            misses += 1
            if misses >= 3:
                yield "[南瓜] 板子没满但战斗开不起来，弹窗驱散了也没用，你去看看卡哪了，停"
                return
            yield "[南瓜] 战斗没开起来，点点安全区驱散弹窗再试..."
            self._pumpkin_skip_safe(cfg, 3)

        got = f"，拿到 {len(got_names)} 把刀：{'、'.join(got_names)}" if got_names else ""
        yield f"[南瓜] 收工，刷了 {boards_done} 块板子，共出阵 {battles_total} 次，烧令牌 {skips}/{max_skips} 枚{got}"
