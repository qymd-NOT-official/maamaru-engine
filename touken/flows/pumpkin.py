# -*- coding: utf-8 -*-
"""
上层业务：南瓜大作战（刮刮乐/九宫格活动）

玩法（用户口述 + 实屏确认）：
  活动界面点"部队选择" → 选部队 → 即刻出阵 → 确认弹窗确定
  → 战斗循环（OCR"战斗"连点 + 安全区跳动画）→ 回到活动界面
  → 重复，直到九宫格 9 格全翻完 → 获得刀剑男士（动画要点掉）
  → 点"剪影更新"（二次弹窗确定）开新一局 → 继续刷

两种模式：
  死板版（默认）：不认剪影是谁，翻到谁是谁，全翻完才更新
  智能版（给 watch_names）：每场回来后认剪影——
    认出是目标刀 → 继续刷完这块板子；
    认出不是目标 → 点剪影更新提前换板子（烧 1 枚更新令牌）；
    认不准 → 不管，接着打，下一场回来再认
  "九宫格满了"的判定：左侧面板「已解锁所有面板」OCR，不靠猜——
  猜错的代价是误点剪影更新白烧一枚更新令牌（血泪教训）
  活动图不存在刀剑破坏；刀装警告仍按老规矩停下上报
"""

import time
from pathlib import Path

from ..maa_adapter import roi_4to4
from ..silhouette import load_library, extract_observed, identify, is_confident, MIN_BLACK_PIXELS

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


class PumpkinMixin:
    """南瓜大作战流程。依赖宿主类的 navigate_to_stream、_click_point、battle_loop_stream。"""

    _battle_started: bool = False
    _refresh_ok: bool = False
    _abort: str = ""  # 非空 = 必须立刻停的原因
    _profile_lib = None      # 剪影素材库缓存；False = 加载失败别再试
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
        cfg = self.config.get("pumpkin", {})
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

        teams = self.config.get("team_select", {}).get("teams", {})
        if str(team_no) not in teams:
            yield f"[南瓜] 配置里没有部队{team_no}的坐标"
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
        for nav_msg in self.navigate_to_stream("出阵"):
            yield nav_msg
        if self.current_location != "出阵":
            yield "[南瓜] 到达出阵失败"
            return

        # ========== 2. 进入活动界面 ==========
        entered = False
        for _ in range(6):
            self.maa.screenshot(force=True)
            if self.maa.template_match(cfg["ui_title"]["template"]):
                entered = True
                break
            entry = self.maa.template_match(cfg["activity_entry"]["template"])
            if entry:
                self.maa.click(entry)
                time.sleep(2.0)
            else:
                time.sleep(1.0)
        if not entered:
            yield "[南瓜] 进不去活动界面（南瓜过季了？）"
            return
        yield "[南瓜] 到达南瓜大作战界面"
        self.set_progress("pumpkin")

        # 选难度（None=保持游戏当前 tab）
        if difficulty:
            tab = cfg.get("difficulty_tabs", {}).get(str(difficulty))
            if tab:
                self._click_point(tab)
                time.sleep(0.8)
                yield f"[南瓜] 已切难度 tab（{difficulty}）"

        # ========== 3. 主循环：打 → 翻格 → 满了更新 → 下一局 ==========
        # 板子满没满看左侧面板的「已解锁所有面板」官方提示，不靠猜——
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

            # 无进展看门狗：180 秒没有任何进展 → 疑似 MAA 卡死，强制停
            if time.time() - last_progress > 180:
                yield f"[南瓜] ⚠️ 已 {int(time.time() - last_progress)} 秒无进展（出阵/换板/拿刀都没有），疑似卡死，强制停。你去看下模拟器画面"
                return

            if self._expedition_takeover_requested():
                self._expedition_takeover_remaining = max_skips - skips
                yield "[南瓜] 🚩 远征排班请求接管：不开新圈，安全收工"
                break

            # 3.1 确保在活动界面（获得动画/弹窗可能盖着，点安全区扒拉掉）
            if not self._ensure_on_board(cfg):
                yield "[南瓜] 回不到活动界面，卡在未知画面，停"
                return

            # 3.2 板子真满了？→ 这块拿到了刀，记下是谁；预算够就更新开新局
            full_cfg = cfg["board_full_ocr"]
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected=full_cfg["expected"], roi=roi_4to4(*full_cfg["roi"])):
                yield "[南瓜] 面板全解锁，这块拿到了刀"
                _bump_progress()
                # 获得动画里 OCR 认一下是谁（记日志，不截图占内存）
                got = self._read_obtained_sword(cfg)
                if got:
                    got_names.append(got)
                    if hasattr(self, "record_event"):
                        self.record_event("pumpkin.sword_obtained", name=got,
                                          obtained_count=len(got_names),
                                          boards_completed=boards_done + 1)
                    yield f"[南瓜] 🎉 获得【{got}】（累计 {len(got_names)} 把）"
                else:
                    yield "[南瓜] 获得动画没 OCR 出名字（继续）"
                # 点掉获得动画
                self.skip_safe(3, point=cfg["skip_tap"])
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
            for msg in self._start_battle_stream(cfg, teams, team_no):
                yield msg
            if self._abort:
                yield self._abort
                return

            if self._battle_started:
                misses = 0
                # 南瓜是全自动战斗（没有"战斗"按钮可点），靠冷静期+标题回归判结束
                battle_debug = None
                if debug_dir:
                    battle_debug = f"{debug_dir}/b{battles_total + 1:02d}"
                for battle_msg in self.battle_loop_stream(cfg_key="pumpkin", tag="[南瓜]",
                                                          need_battle=False,
                                                          debug_dir=battle_debug,
                                                          fought=battles_total):
                    yield battle_msg
                round_done, _ = self._battle_loop_result
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
                self.skip_safe(3, point=cfg["skip_tap"])
                if debug_dir:
                    self._ensure_on_board(cfg)
                    self.maa.save_screenshot(f"{debug_dir}/board_after_b{battles_total:02d}.png")
                continue

            # 3.4 板子没满但开不了战斗 → 狐狸奖励弹窗之类的挡路，驱散再试
            misses += 1
            if misses >= 3:
                yield "[南瓜] 板子没满但战斗开不起来，弹窗驱散了也没用，你去看看卡哪了，停"
                return
            yield "[南瓜] 战斗没开起来，点点安全区驱散弹窗再试..."
            self.skip_safe(3, point=cfg["skip_tap"])

        got = f"，拿到 {len(got_names)} 把刀：{'、'.join(got_names)}" if got_names else ""
        yield f"[南瓜] 收工，刷了 {boards_done} 块板子，共出阵 {battles_total} 次，烧令牌 {skips}/{max_skips} 枚{got}"

    # ---------- 内部：获得动画 OCR ----------

    def _read_obtained_sword(self, cfg: dict) -> str:
        """
        打完九宫格的获得动画里 OCR 刀名（记日志用，不截图）。
        区域可配：cfg["obtain_ocr"] = {"roi": [...], "exclude": ["刀剑乱舞", "獲得"]}
        认不到返回 ""。
        """
        try:
            ocr_cfg = cfg.get("obtain_ocr")
            if not ocr_cfg:
                return ""
            roi = roi_4to4(*ocr_cfg["roi"])
            exclude = ocr_cfg.get("exclude", [])
            texts = self.maa.ocr_all(roi)
            for text, _pt in texts:
                t = text.strip()
                if not t or any(x in t for x in exclude):
                    continue
                return t
        except Exception:
            pass
        return ""

    # ---------- 内部：智能认人 ----------

    def _get_profile_lib(self, cfg: dict):
        """加载剪影素材库（只加载一次；失败置 False 不再重试）"""
        if self._profile_lib is not None and self._profile_lib is not False:
            return self._profile_lib
        if self._profile_lib is False:
            return None
        from ..runtime_paths import BUNDLED_PROFILES_DIR, USER_PROFILES_DIR

        configured = cfg.get("profiles", "profiles")
        if configured and configured != "profiles":
            custom = Path(configured)
            if not custom.is_absolute():
                custom = self._root / custom
            lib_dirs = (BUNDLED_PROFILES_DIR, custom, USER_PROFILES_DIR)
        else:
            lib_dirs = (BUNDLED_PROFILES_DIR, USER_PROFILES_DIR)
        try:
            lib = load_library(lib_dirs)
            if not lib:
                raise ValueError("库里一个模板都没有")
            self._profile_lib = lib
            return lib
        except Exception as exc:
            joined = "、".join(str(path) for path in lib_dirs)
            print(f"[南瓜] 剪影素材库加载失败: {exc}（{joined}）")
            self._profile_lib = False
            return None

    def _identify_board_stream(self, cfg: dict, watch_names: list):
        """
        认当前板子的剪影。结果放在 self._identify_decision + self._identify_name：
          None  = 认不准/素材太少，继续打下一场回来再认
          "keep" = 是目标刀，刷完它
          "skip" = 不是目标刀，烧令牌换板子
        """
        self._identify_decision = None
        self._identify_name = None

        lib = self._get_profile_lib(cfg)
        if lib is None:
            yield "[南瓜] ⚠️ 剪影素材库加载失败，这局当死板版刷"
            self._identify_decision = "keep"  # 别误烧令牌，也别再认了
            return

        img = self.maa.screenshot(force=True)
        if img is None:
            return
        obs = extract_observed(img)
        black = int(obs.sum())
        if black < MIN_BLACK_PIXELS:
            yield f"[南瓜] 剪影才翻出 {black} 个黑像素，认不出，继续打"
            return

        t0 = time.time()
        results = identify(obs, lib)
        if not results:
            return
        top_str = " / ".join(f"{name}({view}) {score:.2f}" for score, name, view in results[:3])
        yield f"[南瓜] 剪影识别（{time.time() - t0:.0f}s）：{top_str}"

        if not is_confident(results):
            yield "[南瓜] 分差不够，认不准，再打一场回来看"
            return

        name = results[0][1]
        self._identify_name = name
        if name in watch_names:
            yield f"[南瓜] 认出来了，是【{name}】——目标刀！刷完这块板子"
            self._identify_decision = "keep"
        else:
            yield f"[南瓜] 认出来了，是【{name}】，不是目标，烧令牌换板子"
            self._identify_decision = "skip"

    # ---------- 内部：确保在活动界面 ----------

    def _ensure_on_board(self, cfg: dict) -> bool:
        """在活动界面返回 True；被动画/弹窗盖着就点安全区扒拉，扒拉不回来返回 False。"""
        return self.wait_landmark_skipping(
            template=cfg["ui_title"]["template"],
            skip_point=cfg["skip_tap"],
            timeout_s=8, stable_hits=1, interval=1.0)

    # ---------- 内部：开一场战斗 ----------

    def _start_battle_stream(self, cfg: dict, teams: dict, team_no: int):
        """
        点部队选择 → 选部队 → 即刻出阵 → 确定。
        结果放在 self._battle_started；九宫格满了开不起来时置 False（不算错误）。
        遇上必须人来看的情况置 self._abort。
        """
        self._battle_started = False

        deploy = self._find_deploy_button(cfg)
        if not deploy:
            yield "[南瓜] 没找到部队选择按钮"
            return
        self.maa.click(deploy)
        time.sleep(1.5)

        ocr_cfg = cfg["team_ui_ocr"]
        roi = roi_4to4(*ocr_cfg["roi"])
        if not self._wait_for_team_select(cfg, attempts=8):
            # 部队界面没开：可能是弹窗挡路，不代表板子满了（满没满看「已解锁」OCR）
            yield "[南瓜] 部队选择界面没打开"
            # 把拦路弹窗点掉再退出去，免得挡后面的剪影更新
            self.maa.screenshot(force=True)
            ok = self.maa.template_match(cfg["confirm_button"]["template"])
            if ok:
                self.maa.click(ok)
                time.sleep(1.0)
            else:
                self._click_point(cfg["skip_tap"])
                time.sleep(0.8)
            return

        # 选部队（固定坐标，点两下确认）
        self._pick_team(team_no)

        # 即刻出阵
        if not self._click_depart(cfg):
            yield "[南瓜] 找不到即刻出阵按钮"
            return

        self.maa.screenshot(force=True)

        # 刀装未满警告 → 安全取消整备，给后续日课让路
        equip_cancelled = self._cancel_equip_warning(cfg)
        if equip_cancelled is not None:
            if equip_cancelled:
                self._abort = "[南瓜] ⚠️ 刀装未满警告；已取消出阵并返回部队选择，本次跳过"
            else:
                self._abort = "[南瓜] ⚠️ 刀装未满警告；没能安全取消整备，本次出阵停止"
            return

        # 确认弹窗（可能有也可能直接进图）
        if self.maa.template_match(cfg["confirm_ui"]["template"]):
            confirm_cfg = cfg["confirm_button"]
            confirm_roi = roi_4to4(*confirm_cfg["roi"]) if "roi" in confirm_cfg else None
            confirm = self.maa.template_match(confirm_cfg["template"], confirm_roi)
            if confirm:
                self.maa.click(confirm)
                time.sleep(2.0)

        # 验证真出去了：格子有编队任务，条件不满足会被游戏拒回部队选择
        # （拒的时候弹个提示窗，先点掉再看标题）
        time.sleep(2.0)
        self.maa.screenshot(force=True)
        reject_ok = self.maa.template_match(cfg["confirm_button"]["template"])
        if reject_ok:
            self.maa.click(reject_ok)
            time.sleep(1.0)
            self.maa.screenshot(force=True)
        if self.maa.ocr(expected=ocr_cfg["expected"], roi=roi):
            self._abort = ("[南瓜] ⚠️ 即刻出阵被游戏拒了，还停在部队选择界面"
                           "——编队不满足这个格子的任务条件（左侧任务栏写着要啥），你去改下编队再来")
            return

        self._battle_started = True
        yield "[南瓜] 出发，进入战斗循环"

    # ---------- 内部：剪影更新 ----------

    def _refresh_board_stream(self, cfg: dict, auto_refill: bool = False):
        """
        剪影更新 → 二次弹窗确定。结果放在 self._refresh_ok。
        更新完会校验：能不能重新点开部队选择界面（能 = 新板子出来了）。

        注意：南瓜 Pt 里程碑的狐狸对话（"已解锁所有面板…"那个看板娘弹窗）
        是模态的，会挡住剪影更新的确认弹窗。它没有关闭按钮，
        靠点 skip_tap 翻页（一页一句，一般 2 页），翻完自己消失。
        所以确认弹窗没出来就先驱散几轮再重试，别急着收工。
        """
        self._refresh_ok = False

        confirm = None
        for attempt in range(3):
            recovered = False
            if attempt:
                yield f"[南瓜] 剪影更新第 {attempt + 1} 次尝试..."
            # 先驱散可能挡路的狐狸对话（每点一下翻一页，没弹窗时点的是安全区，无害）
            self.skip_safe(2, point=cfg["skip_tap"])

            self.maa.screenshot(force=True)
            refresh = self.maa.template_match(cfg["refresh_button"]["template"])
            if not refresh:
                yield "[南瓜] 找不到剪影更新按钮"
                return
            self.maa.click(refresh)
            time.sleep(1.5)

            # 二次确认弹窗（可能被狐狸对话挡着，多等几拍）
            for _ in range(4):
                self.maa.screenshot(force=True)
                confirm = self.maa.template_match(cfg["refresh_confirm"]["template"])
                if confirm:
                    break
                recover_cfg = self.config.get("raid", {}).get("ticket_recover", {})
                popup_template = recover_cfg.get("popup_button", {}).get("template")
                if popup_template and self.maa.template_match(popup_template):
                    if not auto_refill:
                        close_template = recover_cfg.get("close_button", {}).get("template")
                        close = self.maa.template_match(close_template) if close_template else None
                        if close:
                            self.maa.click(close)
                        yield "[南瓜] 手形不足；自动补充已关闭，不消耗小判，收工"
                        return
                    yield "[南瓜] 手形不足，按设置使用小判自动补充..."
                    recovery = dict(cfg)
                    recovery["ticket_recover"] = recover_cfg
                    for recover_msg in self._recover_ticket_stream(recovery, tag="[南瓜]"):
                        yield recover_msg
                    if not self._recover_ok:
                        yield "[南瓜] 手形补充失败，收工"
                        return
                    # 旧活动没有已验证票价；先落补票事实，不猜小判金额。
                    self._record_ticket_refill(recovery, "[南瓜]")
                    recovered = True
                    break
                time.sleep(0.8)
            if not confirm and recovered:
                continue
            if confirm:
                break
            # 确认没出来 → 狐狸对话之类挡路，翻页驱散后重试
            yield "[南瓜] 确认弹窗没出来，可能有狐狸里程碑对话挡路，翻页驱散后重试..."
            self.skip_safe(3, point=cfg["skip_tap"])
        else:
            yield "[南瓜] 剪影更新的确认弹窗一直没出来（令牌烧完了，或者弹窗没驱散掉）"
            return

        self.maa.click(confirm)
        time.sleep(2.0)

        # 可能有"新剪影出现了"之类的动画，点掉
        self.skip_safe(3, point=cfg["skip_tap"])

        # 校验：回活动界面 + 部队选择按钮回来了 = 更新成功
        if not self._ensure_on_board(cfg):
            yield "[南瓜] 更新完回不到活动界面"
            return
        self.maa.screenshot(force=True)
        if self._find_deploy_button(cfg):
            self._refresh_ok = True
            yield "[南瓜] 剪影更新完成，新板子就位"
        else:
            yield "[南瓜] 更新完部队选择按钮没回来"
