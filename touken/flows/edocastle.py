# -*- coding: utf-8 -*-
"""
上层业务：江户城潜入调查（edocastle）

玩法（老大口述 + 踩点素材）：
  本丸→出阵→活动入口→江户城标题确认→难度四卡片→部队选择→即刻出阵
  →确认弹窗→地图屏→按巡游策略点节点→战斗/钥匙/空点分支→王点战胜
  →「调查完了！」横幅→结算→回入场屏→下一圈（令牌够的话）

安全规矩：
  - v1 只做难度四，不主动用道具，不主动返回本丸
  - 出阵走 BattleMixin 的通用安全出阵链（_safe_depart_stream）：选队验证、
    出阵前伤势检查、刀装未满处理、重伤弹窗拦截一个不少；二次确认用通用
    _confirm_departure。虚拟伤害活动不碎刀，默认只拦重伤（repair_threshold
    =heavy），中伤照跑
  - 步数/钥匙靠 HUD OCR；当前位置靠流程自己记账
  - 票尽不数令牌格：游戏自己会弹补票窗，安全链按 auto_refill 处理
    （确定补一张再出阵 / 取消收工，异去同款交互）
  - 败北/意外回入场屏按前后钥匙差记账并停
"""

import re
import time
from pathlib import Path

from ..edo_route import EDOCASTLE_TOUR, decide_next, load_archive
from ..flow_control import FlowAborted
from ..maa_adapter import roi_4to4


class EdocastleMixin:
    """江户城潜入调查流程。依赖宿主类的 navigate_to_stream、_click_point、
    choose_formation、_safe_depart_stream、_confirm_departure。"""

    def edocastle_stream(
        self,
        team_no: int = None,
        use_koban_refill: bool = None,
        max_runs: int = None,
        formation_mode: str = "manual",
        formation: str = "鱼鳞阵",
        repair_threshold: str = None,
        auto_equip: bool = None,
        debug_dir: str = None,
        **kwargs,
    ):
        """
        流式刷江户城潜入调查（难度四·超难）

        Args:
            team_no: 部队编号，默认读配置 edocastle.team_no
            use_koban_refill: 票尽时是否用小判补票（走游戏自己的补票弹窗：
                出阵→确定补一张→再出阵），默认读配置 edocastle.use_koban_refill
            max_runs: 本次最多跑几圈，必须为正数
            formation_mode: "manual"/"auto"，复用合战场阵型选择
            formation: 固定阵型名
            repair_threshold: 出阵前伤势停止线（light/medium/heavy），
                默认读配置 edocastle.repair_threshold（"heavy"：虚拟伤害活动
                不碎刀，中伤照跑，只拦重伤）
            auto_equip: 刀装有空缺时是否用记录一自动补齐，
                默认读配置 edocastle.auto_equip（v1 保守默认 False=安全取消）
        """
        cfg = self.config.get("edocastle", {})
        if not cfg:
            yield "[江户城] 未配置江户城潜入调查"
            return

        team_no = team_no if team_no is not None else cfg.get("team_no", 3)
        if use_koban_refill is None:
            use_koban_refill = bool(cfg.get("use_koban_refill", False))
        if max_runs is None:
            max_runs = int(cfg.get("max_runs", cfg.get("refill_run_limit", 6)))
        if max_runs <= 0:
            max_runs = 6
        if repair_threshold is None:
            repair_threshold = str(cfg.get("repair_threshold", "heavy"))
        if auto_equip is None:
            auto_equip = bool(cfg.get("auto_equip", False))

        teams = self.config.get("team_select", {}).get("teams", {})
        if str(team_no) not in teams:
            yield f"[江户城] 配置里没有部队{team_no}的坐标"
            return

        archive_path = cfg.get("map_archive")
        if not archive_path:
            yield "[江户城] 未配置地图档案路径"
            return
        archive_path = Path(archive_path)
        if not archive_path.is_absolute():
            # 地图档案是程序资源（跟模板图一样捆在 bundle 里），
            # 不能拿用户数据目录当根（实测翻车：_root 是数据目录）
            from ..runtime_paths import BUNDLE_ROOT
            archive_path = BUNDLE_ROOT / archive_path
        try:
            archive = load_archive(archive_path)
        except Exception as exc:
            yield f"[江户城] 加载地图档案失败：{exc}"
            return

        tour = cfg.get("tour", EDOCASTLE_TOUR)
        boss = archive.get("boss", 2)
        skip_point = cfg.get("skip_tap", [775, 695])

        # 记账期次：必须是「活动名@开幕日」（算盘的期次口径），
        # 不能拿跑圈当天日期——否则实测全被当成别届数据丢弃
        # （2026-09-07 前就是这么写错的，老数据由读取端兼容）
        from ..advisor import load_event_cards
        from ..event_history import period_key
        from ..runtime_paths import STATE_DIR
        period = period_key(
            "江户城潜入调查",
            load_event_cards(STATE_DIR).get("江户城潜入调查") or {},
        ) or f"江户城潜入调查@{time.strftime('%Y-%m-%d')}"

        # ========== 1. 导航到出阵 ==========
        yield "[江户城] 正在导航到出阵…"
        for nav_msg in self.navigate_to_stream("出阵"):
            yield nav_msg
        if self.current_location != "出阵":
            yield "[江户城] 到达出阵失败"
            return

        # ========== 2. 进入活动界面 ==========
        entered = False
        for _ in range(8):
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
            yield "[江户城] 进不去活动界面，停"
            return
        yield "[江户城] 到达江户城潜入调查入口"
        self.set_progress("edocastle")

        # ========== 3. 主循环：一圈一圈跑 ==========
        runs_done = 0
        total_keys = 0
        team_record_saved = False
        while True:
            if max_runs > 0 and runs_done >= max_runs:
                yield f"[江户城] 已达最大圈数 {max_runs}，收工"
                break
            if self._expedition_takeover_requested():
                self._expedition_takeover_remaining = max_runs - runs_done if max_runs > runs_done else None
                yield "[江户城] 🚩 远征排班请求接管：不开新圈，安全收工"
                break

            # 读入场屏钥匙总数（本圈 before）
            keys_before = self._read_key_total(cfg)
            if keys_before is None:
                yield "[江户城] 入场屏钥匙总数读不出，停"
                return
            yield f"[江户城] 本丸钥匙家底：{keys_before} 把"

            yield f"[江户城] ⚔️ 第 {runs_done + 1} 圈开场"

            # 每圈重新点难度四卡片→部队选择→通用安全出阵链→二次确认。
            # 票尽时游戏会自己弹补票窗，由安全链按 auto_refill 处理
            # （确定补一张再出阵 / 取消收工），不用提前数令牌。
            entered, team_record_saved = yield from self._enter_map_stream(
                cfg, team_no, skip_point,
                repair_threshold, auto_equip, team_record_saved,
                auto_refill=use_koban_refill,
                formation_mode=formation_mode,
                formation=formation)
            if not entered:
                yield "[江户城] 没能进地图，安全收工"
                break

            # ========== 4. 地图巡游 ==========
            run_keys, ok = yield from self._map_run_stream(
                cfg, archive, tour, boss, skip_point,
                formation_mode, formation,
                debug_dir,
            )

            # 无论正常结算还是败北/意外回城，都先尝试回到入场屏记账
            if not ok:
                yield "[江户城] 本圈异常，看看是不是还在地图里…"
                recovered = False
                if self._in_map(cfg):
                    yield "[江户城] 还站在地图里，走「返回本丸」撤退"
                    recovered = yield from self._bail_out_stream(cfg)
                elif self._formation_mode_state() is not None:
                    # 卡在阵型选择页：战斗里没有撤退按钮，只能打完再走撤退。
                    # 这会页面早过了进场动画，再试一次选阵型通常能成。
                    yield "[江户城] 卡在阵型选择页，再试一次把这场打完"
                    if self._fight_one_battle(
                            cfg, formation_mode,
                            formation, skip_point):
                        if self._wait_map_landmark(cfg, timeout_s=30):
                            recovered = yield from self._bail_out_stream(cfg)
                        else:
                            yield "[江户城] 战斗打完没回到地图，停"
                    else:
                        yield "[江户城] 阵型还是选不动，停在战斗里了，需要手动看一眼"
                else:
                    self._save_debug_shot(debug_dir, "aborted_unknown")
                    yield ("[江户城] 认不出现场（不在地图也不在阵型页），"
                           "停手留证（截图已存），不盲撤")
                if not recovered:
                    # 没能把局面收回已知状态：原地停手等手动处理，不再拿
                    # 安全区盲点（2026-09-09：盲点把撤退点击全打进了战斗
                    # 结算页，还谎报「已回城」）。
                    yield "[江户城] 本圈收不回已知状态，已停手，请手动看一眼游戏"
                    return

            if not self._wait_entry_screen(cfg, skip_point, timeout_s=30):
                self._save_debug_shot(debug_dir, "entry_screen_missing")
                yield "[江户城] 结算后没回到入场屏，停"
                return

            keys_after = self._read_key_total(cfg)
            if keys_after is None:
                # 用流程内部估算兜底
                keys_after = keys_before + max(0, run_keys)
                yield f"[江户城] 结算后钥匙总数没读到，按 HUD 估算 {keys_after} 把"
            delta = keys_after - keys_before
            total_keys += delta
            if ok:
                if hasattr(self, "record_event"):
                    from ..advisor import MAX_PLAUSIBLE_KEYS_PER_RUN
                    payload = dict(period=period, difficulty=4,
                                   run_no=runs_done + 1, team_no=team_no)
                    if 0 < delta <= MAX_PLAUSIBLE_KEYS_PER_RUN:
                        payload["keys"] = delta
                    else:
                        yield (f"[江户城] 本圈钥匙差 {delta:+d} 离谱，"
                               "疑似 OCR 读岔，这趟不记场均")
                    self.record_event("edocastle.run_completed", **payload)
                yield (
                    f"[江户城] ✓ 第 {runs_done + 1} 圈收工，本圈钥匙 {delta:+d} "
                    f"（累计 {total_keys} 把）"
                )
            else:
                if hasattr(self, "record_event"):
                    self.record_event(
                        "edocastle.run_aborted",
                        keys_retained=delta,
                        period=period,
                        difficulty=4,
                        attempted_run_no=runs_done + 1,
                        team_no=team_no,
                    )
                yield (
                    f"[江户城] ⚠️ 第 {runs_done + 1} 圈意外结束，本圈钥匙 {delta:+d}，"
                    "先停下了"
                )
                raise FlowAborted(
                    f"江户城第 {runs_done + 1} 圈战斗状态异常，已主动回城"
                )
            runs_done += 1

        yield (
            f"[江户城] 收工，跑了 {runs_done} 圈，合计带回 {total_keys} 把钥匙"
        )

    # ---------- 内部：入场 → 地图 ----------

    def _enter_map_stream(self, cfg: dict, team_no: int,
                          skip_point: list, repair_threshold: str,
                          auto_equip: bool, team_record_saved: bool,
                          auto_refill: bool = False,
                          formation_mode: str = "manual",
                          formation: str = "鱼鳞阵"):
        """在入场屏点击难度卡片→部队选择→通用安全出阵链→二次确认，直到地图屏。

        选队、伤势检查、刀装处理、重伤拦截全部走 BattleMixin 的
        _safe_depart_stream，江户城只负责入口导航和入图验证。
        返回 (entered, team_record_saved)。
        """        # 点难度四卡片
        self._click_point(cfg["difficulty_card"]["target"])
        time.sleep(1.5)

        # 先看顶部标题：难度卡切页较快时可能已经在部队选择，绝不能再拿
        # 上一帧的“部队选择”坐标去点当前帧同位置的“即刻出阵”。只有确认
        # 尚未进选队页时，才在强制新截图上找并点击入口。
        if not self._wait_for_team_select(cfg, attempts=10, open_after=1):
            yield "[江户城] 部队选择界面没打开"
            # 驱散可能弹窗
            self.skip_safe(2, point=skip_point)
            return False, team_record_saved

        departure_cfg = dict(cfg)
        departure_cfg["ticket_recover"] = self.config.get(
            "raid", {}).get("ticket_recover", {})
        # 当前江户城每次恢复一个手形固定消耗 300 小判。老配置还没有这个
        # 字段时也要记得上账；example 的补键迁移会为后续安装补齐显式配置。
        departure_cfg["ticket_price"] = int(cfg.get("ticket_price", 300))
        ok, team_record_saved = yield from self._safe_depart_stream(
            departure_cfg, team_no, "[江户城]",
            repair_threshold=repair_threshold,
            auto_equip=auto_equip,
            team_record_saved=team_record_saved,
            auto_refill=auto_refill)
        if not ok:
            return False, team_record_saved

        # 江户城没有手形和自动行军；通用二次确认（模板优先，未配兜底坐标）
        if not self._confirm_departure(cfg):
            yield "[江户城] 没看到出阵二次确认，停止点击"
            return False, team_record_saved

        # 江户城入场有时会先打一场，再落到地图。此处必须同时等地图和
        # 阵形选择共同标题；只等地图会把阵形页误当过场一直点安全区。
        entry_state = self._wait_entry_map_or_formation(
            cfg, skip_point, formation_mode, timeout_s=15)
        if entry_state == "formation":
            yield "[江户城] 入场先遇敌，停止跳过并处理阵形"
            if not self._fight_one_battle(
                    cfg, formation_mode, formation, skip_point):
                yield "[江户城] 入场战斗处理失败，停"
                return False, team_record_saved
            if not self._wait_battle_return_to_map(
                    cfg, skip_point, timeout_s=30):
                yield "[江户城] 入场战斗后没回到地图，停"
                return False, team_record_saved
        elif entry_state != "map":
            yield "[江户城] 没进地图屏，停"
            return False, team_record_saved
        return True, team_record_saved

    # ---------- 内部：地图巡游核心 ----------

    def _map_run_stream(
        self,
        cfg: dict,
        archive: dict,
        tour: list,
        boss: int,
        skip_point: list,
        formation_mode: str,
        formation: str,
        debug_dir: str = None,
    ):
        """
        在地图屏走一圈，直到王点战胜。

        Yields 状态消息；返回 (run_keys_estimate, ok)。
        run_keys_estimate 是 HUD 读到的钥匙增量估算（结算差值优先，这里只是兜底）。
        """
        # 进场后当前点已经是 20；21 是视觉起点
        current = 20
        visited = {21, current}
        steps = self._read_hud_steps(cfg)
        if steps is None:
            yield "[江户城] 刚进地图就读不出剩余步数，停"
            return 0, False
        yield f"[江户城] 地图加载完成，当前在 {current}，剩余 {steps} 步"

        keys_start = self._read_hud_keys(cfg) or 0
        last_progress = time.monotonic()
        safe_steps = 0
        ocr_misses = 0
        mistaps = 0

        while True:
            # 无进展看门狗：120 秒没走到下一步/没打完，停
            if time.monotonic() - last_progress > 120:
                yield (
                    f"[江户城] ⚠️ 已 {int(time.monotonic() - last_progress)} 秒没进展，"
                    "疑似卡住，停"
                )
                return 0, False

            visited.add(current)
            if current == boss:
                # 王点战斗已结束，等横幅
                yield "[江户城] 到王点了，等结算横幅…"
                if not self._wait_round_end(cfg, skip_point, timeout_s=30):
                    yield "[江户城] 没等到『调查完了！』横幅，停"
                    return 0, False
                keys_end = self._read_hud_keys(cfg)
                delta = (keys_end - keys_start) if keys_end is not None else 0
                return delta, True

            nxt, mode = decide_next(archive, tour, current, visited, steps)
            coord = self._node_coordinate(archive, nxt)
            if coord is None:
                yield f"[江户城] 档案里找不到节点 {nxt} 的坐标，停"
                return 0, False

            yield f"[江户城] 在 {current}（剩 {steps} 步），{mode} 去 {nxt} {coord}"
            self._click_point(coord)
            time.sleep(1.5)

            # 战斗可能迟到（慢加载时阵型页/自动标志几秒内都不出现，
            # 自动阵型又不需要人点阵型，战斗在后台自己就开打了——
            # 2026-09-09 实测翻车），所以统一观察窗全程同时盯四个信号
            outcome = self._wait_node_outcome(
                cfg, skip_point, formation_mode, timeout_s=20)

            if outcome in ("battle", "battle_result"):
                mistaps = 0
                if outcome == "battle":
                    yield "[江户城] 紫点战斗，选阵型开打"
                    if not self._fight_one_battle(
                        cfg, formation_mode, formation, skip_point
                    ):
                        yield "[江户城] 战斗处理失败，停"
                        return 0, False
                else:
                    # 战果页已出现：阵型页早过去了，回头找阵型只会盲点
                    # 战果页，直接等战斗流程走完回地图
                    yield "[江户城] 战斗开场慢了半拍，已自行开打，等战果"

                if nxt == boss:
                    # 王点战结束等横幅
                    if not self._wait_round_end(cfg, skip_point, timeout_s=30):
                        yield "[江户城] 王点战后没等到『调查完了！』，停"
                        return 0, False
                    keys_end = self._read_hud_keys(cfg)
                    delta = (keys_end - keys_start) if keys_end is not None else 0
                    return delta, True

                # 非王点战斗后等回地图
                if not self._wait_battle_return_to_map(
                        cfg, skip_point, timeout_s=30):
                    yield "[江户城] 战斗后没回到地图，停"
                    return 0, False
            elif outcome == "map":
                mistaps = 0
                node_kind = "黄点钥匙" if self._looks_like_key_node(cfg) else "空点"
                yield f"[江户城] {node_kind}，继续逛"
            else:
                # 20 秒既没开打也没回地图。最后不点击地验一次地图：
                # 地图活着 = 刚才那下被动画吞了点空了，原地重新决策；
                # 地图也不在 = 认不出现场，停手留证，绝不盲撤。
                self.maa.screenshot(force=True)
                ready = cfg["map_ready"]
                ready_roi = roi_4to4(*ready["roi"]) if ready.get("roi") else None
                if self.maa.ocr(expected=ready["expected"], roi=ready_roi):
                    mistaps += 1
                    if mistaps >= 3:
                        yield "[江户城] 连续点空 3 次，节点坐标可能漂了，停"
                        return 0, False
                    yield "[江户城] 刚才那下像点空了，地图还在，重新决策"
                    continue
                self._save_debug_shot(debug_dir, "node_outcome_unknown")
                yield ("[江户城] 点完节点后既没开打也没回地图，认不出现场，"
                       "停手留证（截图已存），不盲动")
                return 0, False

            current = nxt
            new_steps = self._read_hud_steps(cfg)
            if new_steps is None:
                # 悲观兜底：按"这一步没回步数"估算继续走。移动必扣 1、
                # 回步只加不减，所以 估算<=真实，低估只会提前收头奔王点，
                # 绝不会走死。连续 3 次都读不出说明真瞎了，停。
                ocr_misses += 1
                self._save_debug_shot(debug_dir, f"steps_ocr_miss{ocr_misses}")
                if ocr_misses >= 3:
                    yield "[江户城] 连续读不出剩余步数，太瞎了，停"
                    return 0, False
                new_steps = max(0, steps - 1)
                yield (
                    f"[江户城] 步数读不出，按悲观估算 {new_steps} 步继续"
                    "（现场截图已存）"
                )
            else:
                ocr_misses = 0
            steps = new_steps
            last_progress = time.monotonic()
            safe_steps += 1
            if safe_steps > 60:
                yield "[江户城] 单圈步数超过安全上限，停"
                return 0, False

    # ---------- 内部：点完节点后的统一观察窗 ----------

    def _wait_node_outcome(self, cfg: dict, skip_point: list,
                           formation_mode: str, timeout_s: float = 20.0):
        """点完节点后等局面落地。

        返回 "battle"（阵型页/自动标志出现，需要走选阵型流程）、
        "battle_result"（战果页已出现，战斗已被自动阵型接管甚至打完）、
        "map"（左下角『地图点选择』回来，空点/钥匙点）、None（超时）。

        旧实现是「5 秒等阵型页 + 12 秒等地图」两个互不衔接的观察窗：
        战斗只要晚于 5 秒开场就掉进盲区，被当成空点一路盲点，点穿战果
        页还把撤退三连点打进了结算（2026-09-09 第 7 圈实测翻车）。
        自动阵型不需要人点阵型，战斗迟到完全正常，所以整个等待期间
        必须同时盯四个信号；战果页一旦出现立即停手，绝不点穿江户城
        专属的钥匙/步数横幅。
        """
        ready = cfg["map_ready"]
        ready_roi = roi_4to4(*ready["roi"]) if ready.get("roi") else None
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        while time.monotonic() < deadline:
            self.maa.screenshot(force=True)
            if self._formation_mode_state(
                allow_auto_without_title=formation_mode != "auto"
            ) is not None:
                return "battle"
            if formation_mode == "auto" and self._formation_auto_marker_visible():
                return "battle"
            if self.maa.template_match("battle/ui战斗结果.png"):
                return "battle_result"
            if self.maa.ocr(expected=ready["expected"], roi=ready_roi):
                return "map"
            if skip_point:
                self._click_point(skip_point)
            time.sleep(0.9)
        return None

    # ---------- 内部：单场战斗 ----------

    def _wait_formation_page(self, cfg: dict, timeout_s: float = 5.0,
                             skip_point: list = None,
                             formation_mode: str = "manual") -> bool:
        """等阵形选择页出现；等不到返回 False。
        formation_mode 必须传运行时值：配置文件 edocastle 段没有这个键，
        读 cfg 永远拿到默认 manual（曾导致自动模式下也放行无标题判定）。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.maa.screenshot(force=True)
            if self._formation_mode_state(
                allow_auto_without_title=formation_mode != "auto"
            ) is not None:
                return True
            # 自动阵形按钮比顶部标题先出现，而且开打后仍会短暂常驻。
            # 在“刚点过江户城节点”这个窄上下文里，它足以证明战斗已接管；
            # 从这一帧开始绝不能再拿安全点催动画，否则会一路点穿战斗结果
            # 和江户城专属的钥匙/步数横幅。
            if formation_mode == "auto" and self._formation_auto_marker_visible():
                return True
            if skip_point:
                self._click_point(skip_point)
            time.sleep(0.6)
        return False

    def _fight_one_battle(self, cfg: dict, formation_mode: str,
                          formation: str,
                          skip_point: list) -> bool:
        """处理一场合战场式战斗：索敌→选阵型→等战斗结束。"""
        # 等阵形页稳一点
        if not self._wait_formation_page(cfg, timeout_s=10, skip_point=skip_point,
                                         formation_mode=formation_mode):
            return False

        result = self.choose_formation(
            formation_name=formation,
            enable_auto=(formation_mode == "auto"),
        )
        if result == "failed":
            # 自动阵形可能在顶部标题完全落地前已经自行开打。此时右上角
            # 自动标志仍在，算“战斗已接管”，后续只等战果，不能倒回去盲点。
            if formation_mode == "auto" and self._formation_auto_marker_visible():
                return True
            return False

        # 自动阵形已经把这场交给游戏。这里不能再要求阵形页在固定 5 秒内
        # 消失：索敌/演出稍慢时标题和“自动”标志会继续留在画面上，后面的
        # 战果门闩本来就会安静等到战斗结果出现，再开始点击跳过。
        if result == "auto":
            return True

        # 等阵形页消失
        for _ in range(10):
            time.sleep(0.5)
            self.maa.screenshot(force=True)
            if self._formation_mode_state() is None:
                return True
        return False

    def _formation_auto_marker_visible(self) -> bool:
        """只探测右上角自动阵形标志，不把它当成可点击的阵形页。"""
        formation = self.config.get("formation", {})
        mode = formation.get("auto_mode", {})
        roi = roi_4to4(*mode.get("roi", [840, 0, 980, 68]))
        return bool(self.maa.template_match(
            mode.get("auto_template", "battle/阵形选择自动.png"),
            roi,
            threshold=float(mode.get("threshold", 0.9)),
        ))

    # ---------- 内部：识别与 OCR ----------

    def _read_hud_steps(self, cfg: dict) -> int | None:
        """读取地图屏左上角剩余行动回数。HUD 数字只有十几像素高，
        原生分辨率下检测器处于临界：同值同屏时好时坏（实测剩 2 步连读
        4 次全空）；ROI 裁块放大 3 倍后 218 帧录像回放 100% 读出。
        仍加重试兜底。"""
        ocr_cfg = cfg.get("hud_step_ocr", {})
        roi = roi_4to4(*ocr_cfg.get("roi", [25, 8, 200, 60]))
        upscale = int(ocr_cfg.get("upscale", 3))
        for _ in range(4):
            val = self._ocr_digits(roi, upscale=upscale)
            if val is not None:
                return val
            time.sleep(0.8)
        return None

    def _read_hud_keys(self, cfg: dict) -> int | None:
        """读取地图屏左上角当前持有钥匙数（HUD，同样的小数字，放大识别）。"""
        ocr_cfg = cfg.get("hud_key_ocr", {})
        roi = roi_4to4(*ocr_cfg.get("roi", [230, 8, 360, 60]))
        upscale = int(ocr_cfg.get("upscale", 3))
        for _ in range(3):
            val = self._ocr_digits(roi, upscale=upscale)
            if val is not None:
                return val
            time.sleep(0.6)
        return None

    def _read_key_total(self, cfg: dict) -> int | None:
        """读取入场屏钥匙总数。"""
        ocr_cfg = cfg.get("key_total_ocr", {})
        roi = roi_4to4(*ocr_cfg.get("roi", [920, 185, 1085, 225]))
        return self._ocr_digits(roi)

    def _ocr_digits(self, roi, upscale: int = 0) -> int | None:
        """OCR 区域内所有文字并提取最长连续数字串。

        upscale > 1：先把 ROI 从新截图裁出来放大再识别（治小数字临界）。
        """
        try:
            if upscale > 1:
                import cv2
                img = self.maa.screenshot(force=True)
                if img is None:
                    return None
                crop = img[roi.y:roi.y + roi.h, roi.x:roi.x + roi.w]
                if crop.size == 0:
                    return None
                up = cv2.resize(crop, None, fx=upscale, fy=upscale,
                                interpolation=cv2.INTER_CUBIC)
                tokens = self.maa.ocr_all(
                    roi_4to4(0, 0, up.shape[1], up.shape[0]), image=up)
            else:
                tokens = self.maa.ocr_all(roi)
            ordered = sorted(tokens or [], key=lambda row: getattr(row[1], "x", 0))
            digits = "".join(
                re.sub(r"\D", "", str(text)) for text, _ in ordered
            )
            return int(digits) if digits else None
        except Exception:
            return None

    def _looks_like_key_node(self, cfg: dict) -> bool:
        """粗略判断刚踩的节点是不是黄点钥匙：HUD 钥匙数 +1 且步数 +1。

        因为非战斗点有两种，这里只给日志用，不影响决策。
        """
        # v1 不细究，返回 False 让日志显示"空点/钥匙"
        return False

    def _save_debug_shot(self, debug_dir: str | None, name: str):
        """异常现场存证：优先 debug_dir，否则用户数据目录 debug/。"""
        try:
            out = Path(debug_dir) if debug_dir else Path(self._root) / "debug"
            out.mkdir(parents=True, exist_ok=True)
            self.maa.save_screenshot(
                str(out / f"edo_{name}_{time.strftime('%H%M%S')}.png"))
        except Exception:
            pass

    def _node_coordinate(self, archive: dict, node_id: int) -> list | None:
        for node in archive.get("nodes", []):
            if node["id"] == node_id:
                return [node["x"], node["y"]]
        return None

    def _wait_map_landmark(self, cfg: dict, timeout_s: float = 15.0) -> bool:
        """等左下角『地图点选择』出现，确认地图已经恢复可操作。

        右侧难度牌在钥匙对话弹窗期间仍然可见，只能证明“人还在地图”，
        不能作为下一节点可点击的依据。
        """
        ready = cfg["map_ready"]
        roi_raw = ready.get("roi")
        roi = roi_4to4(*roi_raw) if roi_raw else None
        return self.wait_landmark_skipping(
            ocr_expected=ready["expected"],
            roi=roi,
            skip_point=cfg.get("skip_tap"),
            timeout_s=timeout_s,
            stable_hits=1,
            interval=0.9,
        )

    def _wait_entry_map_or_formation(
            self, cfg: dict, skip_point: list, formation_mode: str,
            timeout_s: float = 15.0) -> str | None:
        """出阵确认后等地图或入场战斗，识别到任一状态立即停止跳过。"""
        ready = cfg["map_ready"]
        ready_roi = roi_4to4(*ready["roi"]) if ready.get("roi") else None
        verify = self.config.get("formation", {}).get("verify", {})
        formation_roi = roi_4to4(
            *verify.get("roi", [571, 5, 707, 44]))
        formation_template = verify.get(
            "template", "battle/ui阵形选择.png")
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        while time.monotonic() < deadline:
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected=ready["expected"], roi=ready_roi):
                return "map"
            if self.maa.template_match(
                    formation_template, roi=formation_roi):
                return "formation"
            # 自动标志比共同标题略早出现；这里只在刚确认出阵的窄上下文
            # 使用它提前刹车。手动模式仍依赖自动/手动共有的红色标题。
            if (formation_mode == "auto"
                    and self._formation_auto_marker_visible()):
                return "formation"
            self._click_point(skip_point)
            time.sleep(0.9)
        return None

    def _wait_round_end(self, cfg: dict, skip_point: list,
                        timeout_s: float = 30.0) -> bool:
        """王点战果出现前不点击；战果出现后逐页等『调查完了！』。"""
        return self._wait_after_battle(
            cfg["round_end"]["template"], skip_point, timeout_s)

    def _wait_battle_return_to_map(self, cfg: dict, skip_point: list,
                                   timeout_s: float = 30.0) -> bool:
        """普通战斗：等战果出现后才逐页点回江户城地图。"""
        ready = cfg["map_ready"]
        return self._wait_after_battle(
            None, skip_point, timeout_s,
            target_roi=ready.get("roi"),
            target_ocr_expected=ready["expected"])

    def _wait_after_battle(self, target_template: str | None, skip_point: list,
                           timeout_s: float, interval: float = 0.9,
                           target_roi: list | None = None,
                           target_ocr_expected: str | None = None) -> bool:
        """战斗结束的两阶段门闩。

        战斗结果页出现前只观察，绝不点击；出现后每次先看目标地标，没到才
        点一下安全区。这样既能跳过战果和活动横幅，也不会在地图已经回来后
        多补一枪。0.9 秒位于老大指定的 0.8～1.0 秒区间内。
        """
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        result_seen = False
        roi = roi_4to4(*target_roi) if target_roi else None
        while time.monotonic() < deadline:
            self.maa.screenshot(force=True)
            target_found = bool(
                target_template
                and self.maa.template_match(target_template, roi=roi)
            )
            if not target_found and target_ocr_expected:
                target_found = bool(self.maa.ocr(
                    expected=target_ocr_expected, roi=roi))
            if target_found:
                return True
            if not result_seen:
                result_seen = bool(self.maa.template_match("battle/ui战斗结果.png"))
                if not result_seen:
                    time.sleep(0.5)
                    continue
            self._click_point(skip_point)
            time.sleep(interval)
        return False

    def _wait_entry_screen(self, cfg: dict, skip_point: list,
                           timeout_s: float = 30.0) -> bool:
        """等回到入场屏标题。"""
        return self.wait_landmark_skipping(
            template=cfg["ui_title"]["template"],
            skip_point=skip_point,
            timeout_s=timeout_s,
            stable_hits=1,
            interval=0.9,
        )

    # ---------- 内部：地图内撤退 ----------

    def _in_map(self, cfg: dict) -> bool:
        """当前是否真站在可操作的地图屏（撤退前必须先过这关）。

        光认右侧难度旗不够：战斗结算页的背景里也带着这面旗
        （2026-09-09 实测误判，把撤退三连点全点进了结算页）。
        必须同时看到左下角『地图点选择』才算地图真的可操作。
        """
        self.maa.screenshot(force=True)
        if not self.maa.template_match(cfg["map_landmark"]["template"]):
            return False
        ready = cfg["map_ready"]
        roi = roi_4to4(*ready["roi"]) if ready.get("roi") else None
        return bool(self.maa.ocr(expected=ready["expected"], roi=roi))

    def _bail_out_stream(self, cfg: dict):
        """地图内主动撤退：行动选择 tab → 返回本丸 → 是 → 点掉回城结算。

        坐标已实测（2026-08-27）：tab (1240,595)、返回本丸 (1050,429)、
        确认「是」(497,470)。主动回城只带回了了了几把钥匙（按规则打折），
        但比卡死在地图里强。

        每步都验证、逐步推进，撤不动就如实汇报并停手留证，绝不谎报
        「已回城」（2026-09-09：旧版盲点三坐标记假账，其实全点在了
        战斗结算页上）。返回 True 表示确认已离开地图。
        """
        retreat = cfg.get("retreat", {})
        # 调用方已确认地图可操作。点开 tab 后必须真的看到「返回本丸」
        # 菜单项才继续，否则界面不是预想的样子，停手。
        self._click_point(retreat.get("tab_point", [1240, 595]))
        menu_open = False
        for _ in range(5):
            time.sleep(0.8)
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected="返回本丸"):
                menu_open = True
                break
        if not menu_open:
            self._save_debug_shot(None, "bail_menu_fail")
            yield "[江户城] 行动选择菜单没打开，撤退中止，停手留证（截图已存）"
            return False
        self._click_point(retreat.get("home_button", [1050, 429]))
        time.sleep(1.5)
        self._click_point(retreat.get("confirm_yes", [497, 470]))
        time.sleep(3.0)
        # 确认撤退真的生效：还站在可操作的地图上就是没撤成
        if self._in_map(cfg):
            self._save_debug_shot(None, "bail_still_in_map")
            yield "[江户城] 点了撤退但人还在地图，撤退没生效，停手留证（截图已存）"
            return False
        # 「主动回城」结算屏点掉
        self.skip_safe(4, point=cfg.get("skip_tap", [775, 695]))
        yield "[江户城] 已主动回城（钥匙按规则打折，认栽）"
        return True
