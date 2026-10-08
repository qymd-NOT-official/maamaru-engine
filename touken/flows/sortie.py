# -*- coding: utf-8 -*-
"""
上层业务：出阵（合战场）——自动行军版

流程（教材 + 旧脚本思路）：
  出阵 → 合战场选章节（固定坐标）→ 决定 → 地域选择 → 选小图（固定坐标）
  → 部队选择 → 选部队 → （可选）委托自动行军
  → 【重伤检查】有重伤绝不出阵
  → 即刻出阵 → 分支：刀装警告停 / 队员重伤点否停 / 进入行军
  → 自动行军打完自动回本丸；行军中断（中伤）→ 返回本丸

教材安全规矩（写死，不商量）：
  - 只要有重伤就绝对不能出阵（会碎刀）
  - 刀装未满：没要求自动补充就停脚本上报
  - 战斗内发现重伤（自动行军停止）：直接返回本丸
  - 队员重伤确认弹窗：永远点"否"

备注：章节/小图坐标是老配置里估的，首次用新章节前要实测校准。
"""

import os
import re
import time

from .. import sword_db
from ..maa_adapter import roi_4to4
from .battle import find_deploy_button
from ..map_read import CV2_AVAILABLE, boss_distance_from_image


def _parse_fragment_counts(tokens: list) -> dict:
    """「可以获得的宝物碎片」弹窗 OCR 词元 → {碎片名: 所持数}。

    tokens: [(文本, x, y)]。名字词元以“碎片”结尾且在弹窗上部导航区
    之外（y>150，排除标题“可以获得的宝物碎片”）；数量词元形如“3 个”，
    取名字正下方（纵向 80px、横向 80px 以内）最近的一个。配不对的名字
    不进结果——宁可缺数，不可瞎编。
    """
    names = [(text, x, y) for text, x, y in tokens
             if text.endswith("碎片") and y > 150]
    counts = []
    for text, x, y in tokens:
        m = re.fullmatch(r"(\d+)\s*个", text)
        if m:
            counts.append((int(m.group(1)), x, y))
    result = {}
    for name, nx, ny in names:
        below = [(c, cx, cy) for c, cx, cy in counts
                 if 0 < cy - ny <= 80 and abs(cx - nx) <= 80]
        if below:
            best = min(below, key=lambda item: (item[2] - ny) ** 2
                       + (item[1] - nx) ** 2)
            result[name] = best[0]
    return result


def _parse_popup_map(tokens: list) -> int | None:
    """弹窗顶部小图选择器（如“四 鸟羽”）→ 小图号；认不出返回 None。"""
    for text, _x, y in tokens:
        if y < 150 and text in ("一", "二", "三", "四"):
            return ("一", "二", "三", "四").index(text) + 1
    return None


class SortieMixin:
    """合战场出阵。依赖宿主类的 navigate_to_stream、_click_point、_enable_auto_march。"""

    def _on_team_select_page(self, cfg: dict) -> bool:
        """顶部标题 OCR 判断当前是否已经在部队选择页（复用 _last_image）。"""
        ocr_cfg = cfg.get("team_ui_ocr", {})
        roi_raw = ocr_cfg.get("roi")
        if not roi_raw:
            return False
        return bool(self.maa.ocr(expected=ocr_cfg.get("expected", "部队选择"),
                                 roi=roi_4to4(*roi_raw)))

    def sortie_stream(self, chapter: int, map_no: int, team_no: int = 3,
                      auto_march: bool = True, max_loops: int = 1,
                      formation_mode: str = "manual",
                      formation: str = "鱼鳞阵",
                      repair_threshold: str = "light",
                      injury_action: str = "continue",
                      auto_equip: bool = True,
                      retreat_before_boss: bool = False,
                      rotate_captain: bool = False,
                      rotate_captain_margin: int = 10,
                      stop_on_fatigue: bool = True):
        yield from self._map_sortie_stream(
            chapter=chapter, map_no=map_no, team_no=team_no,
            auto_march=auto_march, max_loops=max_loops,
            formation_mode=formation_mode,
            formation=formation,
            repair_threshold=repair_threshold, injury_action=injury_action,
            auto_equip=auto_equip,
            retreat_before_boss=retreat_before_boss,
            rotate_captain=rotate_captain,
            rotate_captain_margin=rotate_captain_margin,
            stop_on_fatigue=stop_on_fatigue,
        )

    def yosari_stream(self, map_no: int, team_no: int = 3,
                      auto_march: bool = True, max_loops: int = 1,
                      auto_refill: bool = False,
                      formation_mode: str = "manual",
                      formation: str = "鱼鳞阵",
                      repair_threshold: str = "light",
                      injury_action: str = "continue",
                      auto_equip: bool = True,
                      rotate_captain: bool = False,
                      rotate_captain_margin: int = 10,
                      stop_on_fatigue: bool = True):
        """流式跑常驻玩法“异去”；目前只有第一章。"""
        yield from self._map_sortie_stream(
            chapter=1, map_no=map_no, team_no=team_no,
            auto_march=auto_march, max_loops=max_loops,
            auto_refill=auto_refill,
            formation_mode=formation_mode,
            formation=formation,
            repair_threshold=repair_threshold, injury_action=injury_action,
            auto_equip=auto_equip,
            rotate_captain=rotate_captain,
            rotate_captain_margin=rotate_captain_margin,
            stop_on_fatigue=stop_on_fatigue,
            map_type="异去", cfg_key="yosari",
        )

    def _map_sortie_stream(self, chapter: int, map_no: int, team_no: int = 3,
                           auto_march: bool = True, max_loops: int = 1,
                           formation_mode: str = "manual",
                           formation: str = "鱼鳞阵",
                           repair_threshold: str = "light",
                           injury_action: str = "continue",
                           auto_equip: bool = True,
                           auto_refill: bool = False,
                           retreat_before_boss: bool = False,
                           rotate_captain: bool = False,
                           rotate_captain_margin: int = 10,
                           map_type: str = "合战场",
                           cfg_key: str = "sortie",
                           stop_on_fatigue: bool = True):
        """
        流式跑合战场

        Args:
            chapter: 章节编号（1-8，对应 map_select.合战场.chapters）
            map_no: 小图编号（1-4，对应 map_select.合战场.maps）
            team_no: 部队编号
            auto_march: 是否委托自动行军（True=全自动打完一圈回本丸）
            max_loops: 连续打几圈
            repair_threshold: 自动手入阈值（light / medium / heavy）
            rotate_captain: 每圈在部队选择页读全队疲劳，最低的拖去队长位（保花）
            rotate_captain_margin: 当前队长与全队最低疲劳相差多少才换

        Yields:
            str: 执行状态消息
        """
        cfg = dict(self.config.get("sortie", {}))
        cfg.update(self.config.get(cfg_key, {}))
        if not cfg:
            yield f"[出阵] 未配置{map_type}"
            return

        map_cfg = self.config.get("map_select", {}).get(map_type, {})
        teams = self.config.get("team_select", {}).get("teams", {})
        if str(chapter) not in map_cfg.get("chapters", {}):
            yield f"[出阵] 配置里没有章节{chapter}的坐标"
            return
        if str(map_no) not in map_cfg.get("maps", {}):
            yield f"[出阵] 配置里没有小图{map_no}的坐标"
            return
        if str(team_no) not in teams:
            yield f"[出阵] 配置里没有部队{team_no}的坐标"
            return

        # 硬保护：王点前撤退必须脚本亲手盯小地图走；委托挂上后路线全归游戏，
        # 撤退会静默失效直接进王点。面板只是隐藏开关不是清空，这里必须兜底。
        from ..auto_march_settings import march_policy
        policy = march_policy(repair_threshold, injury_action, auto_equip,
                              stop_on_fatigue, formation)
        if auto_march and policy is None:
            yield "[出阵] 游戏没有轻伤停止设定，改用脚本行军执行轻伤停止"
            auto_march = False
        if retreat_before_boss and auto_march and cfg_key == "sortie":
            yield ("[出阵] 🛡️ 王点前撤退和自动行军只能二选一"
                   "（撤退得脚本盯着小地图走），本轮改用脚本手动行军")
            auto_march = False

        # ========== 1. 导航到出阵（默认就在合战场） ==========
        yield "[出阵] 正在导航到出阵..."
        for nav_msg in self.navigate_to_stream("出阵"):
            yield nav_msg
        if self.current_location != "出阵":
            yield "[出阵] 到达出阵失败"
            return

        loop_no = 1
        repair_attempts = 0
        map_page_ready = False
        record_saved = False
        self._map_miss_count = 0
        loop_attempts = {}  # 圈序号 → 已出发次数（中断后重试同一圈会再 +1）
        # 蹲点模式（MAAMARU_YOSARI_WATCH=1）：异去行军每一帧存盘，
        # 事后翻碎片掉落画面长什么样。只服务异去，正常跑不开。
        watch_dir = self._yosari_watch_dir(cfg_key)
        watch_seq = 0
        # 碎片库存差分：每圈结束开弹窗读一次所持数，与上次读数对比得收益。
        frag_inv = None
        # 刀装恢复与伤势处理是两项独立决策：是否保存/使用记录一只看
        # auto_equip；若先检测到伤势，仍按 injury_action 处理并停止或续跑。
        auto_equip_active = bool(auto_equip)
        while loop_no <= max_loops:
            if self._expedition_takeover_requested():
                self._expedition_takeover_remaining = max_loops - loop_no + 1
                tag = "[异去]" if cfg_key == "yosari" else "[出阵]"
                yield f"{tag} 🚩 远征排班请求接管：不开新圈，安全收工"
                return
            if self.current_location != "出阵":
                for nav_msg in self.navigate_to_stream("出阵"):
                    yield nav_msg
                if self.current_location != "出阵":
                    yield "[出阵] 重新进入出阵失败，停止"
                    return
            yield f"[出阵] ⚔️ 第 {loop_no}/{max_loops} 圈：{chapter}章-{map_no}图，部队{team_no}准备上场"

            if not map_page_ready:
                if cfg_key == "yosari" and not self._enter_yosari(cfg):
                    yield "[异去] 没能从过去切换到异去，停止"
                    return

                # 每日首次进异去会渐入剧情演出：进场前右下角“决定”按钮还看得见，
                # 动画一盖上来就被挡住，脚本在动画里乱点会彻底打乱卡死。
                # 只点安全区把动画跳完，直到“决定”按钮完整出现，再走章节选择。
                if cfg_key == "yosari":
                    # 累计圈数里程碑弹窗（500/800/1100 圈送火车切）是
                    # 「确定」键，会挡在章节页前面把等决定的循环卡死
                    # （2026-09-10 第 62 圈实测翻车），进等待前先收一次
                    if self._dismiss_yosari_milestone(cfg):
                        yield "[异去] 🎁 累计圈数里程碑奖励（火车切）已领，继续"
                    # 火车切渐入演出会让"决定"按钮先露脸再被盖住、说完才回来，
                    # 必须连续命中才算真就绪；章节页点安全区验证过无害，边等边点加速推对话。
                    if not self.wait_landmark_skipping(
                            template=cfg["decide_button"]["template"],
                            skip_point=cfg.get("skip_tap"),
                            timeout_s=90, stable_hits=3,
                            tap_even_when_found=True):
                        # 弹窗可能在等待中途才弹出来，收一次再试
                        if self._dismiss_yosari_milestone(cfg):
                            yield "[异去] 🎁 累计圈数里程碑奖励（火车切）已领，继续"
                            if not self.wait_landmark_skipping(
                                    template=cfg["decide_button"]["template"],
                                    skip_point=cfg.get("skip_tap"),
                                    timeout_s=30, stable_hits=3,
                                    tap_even_when_found=True):
                                yield "[异去] 剧情演出跳不完，没看到“决定”按钮，停止"
                                return
                        else:
                            yield "[异去] 剧情演出跳不完，没看到“决定”按钮，停止"
                            return

                # ========== 2/3. 章节页 → 小图页（状态驱动，不认死流程） ==========
                # 游戏会记住上次选的章节/小图：点到已选中的项等于确认，直接跳进
                # 下一级界面（2026-09-07 老大实测机制，当天翻车实锤）。所以每拍
                # 先认自己在哪页再动手，三种落点都接得住：
                #   部队选择页（顶部标题）= 被跳级了，跳过点小图直接跟上；
                #   小图页（右下角"部队选择"）= 正常落点；
                #   章节页（"决定"还在）= 上一口点击被吞，补点章节+决定。
                area_ok = False
                jumped = False
                nav_tapped = False
                last_nav_tap = 0.0
                for _ in range(25):  # 约 20~25 秒预算
                    self.maa.screenshot(force=True)
                    if self._on_team_select_page(cfg):
                        jumped = True
                        break
                    if find_deploy_button(self.maa, cfg):
                        area_ok = True
                        break
                    now = time.monotonic()
                    if now - last_nav_tap >= 2.0:
                        decide = self.maa.template_match(
                            cfg["decide_button"]["template"])
                        if decide:
                            if nav_tapped:
                                yield "[出阵] 章节页没点动，再补一次章节+决定"
                            self._click_point(map_cfg["chapters"][str(chapter)])
                            time.sleep(0.5)
                            self.maa.screenshot(force=True)
                            decide = self.maa.template_match(
                                cfg["decide_button"]["template"])
                            if decide:
                                self.maa.click(decide)
                            nav_tapped = True
                            last_nav_tap = now
                    time.sleep(0.7)
                if jumped:
                    yield "[出阵] 游戏记着上次选的图，一点就跳进部队选择页了，跟上"
                elif not area_ok:
                    yield "[出阵] 没识别到小图页的部队选择按钮（章节坐标或页面状态不对），停止"
                    return
                if area_ok and cfg_key == "yosari" and frag_inv is None:
                    baseline = self._read_yosari_fragments(cfg)
                    if baseline == "stuck":
                        yield "[异去] 碎片弹窗关不掉，为防误点收工；你去瞅一眼"
                        return
                    if baseline:
                        frag_inv = baseline
                elif jumped and cfg_key == "yosari" and frag_inv is None:
                    # 跳级进场没路过小图页，碎片弹窗开不了；圈末读数会自动当上基线
                    yield "[异去] 跳级进场，本圈碎片基线跳过，下圈开始记账"
            else:
                yield "[异去] 已回到四张小图页，直接开始下一圈"
                jumped = False
            if not jumped:
                self._click_point(map_cfg["maps"][str(map_no)])
                time.sleep(1.0)
            map_page_ready = False

            # ========== 4. 部队选择 → 选部队 ==========
            # 合战场点完小图会【直接】进部队选择界面，没有中间按钮；
            # 但保险起见：没在部队选择界面时才去找"部队选择"按钮点
            if not self._wait_for_team_select(cfg, attempts=12, open_after=2):
                # 里程碑弹窗（确定键）也会挡在部队选择前面，收一次再试
                if cfg_key == "yosari" and self._dismiss_yosari_milestone(cfg):
                    yield "[异去] 🎁 累计圈数里程碑奖励（火车切）已领，继续"
                    if not self._wait_for_team_select(cfg, attempts=12,
                                                      open_after=2):
                        yield "[出阵] 部队选择界面没打开，本圈放弃"
                        continue
                else:
                    yield "[出阵] 部队选择界面没打开，本圈放弃"
                    continue

            self._pick_team(team_no)

            # ========== 4.5 每圈原地换队长（保花：部队选择页与编队页布局相同，
            # 用户实测能直接拖人换位，不用绕路编队页；差距不到阈值会自动跳过） ==========
            if rotate_captain:
                try:
                    for rot_msg in self._rotate_captain_here(rotate_captain_margin):
                        yield rot_msg
                except Exception as exc:
                    yield f"[出阵] 自动换队长翻车（不影响出阵）: {exc}"

            # ========== 5. 【保命】重伤检查（先于一切出阵准备） ==========
            self.maa.screenshot(force=True)
            log_cache: dict = {}
            injury, log_note = self._combined_injury_status(cfg, team_no,
                                                            log_cache)
            if log_note:
                yield f"[出阵] 日志验伤：{log_note}"
            if injury:
                threshold = str(repair_threshold or "light")
                severity_rank = {"轻伤": 1, "中伤": 2, "重伤": 3}
                threshold_rank = {"light": 1, "medium": 2, "heavy": 3}.get(threshold, 1)
                must_repair = injury == "重伤"
                if not must_repair and severity_rank.get(injury, 3) < threshold_rank:
                    yield f"[出阵] 部队{team_no}{injury}，尚未达到手入阈值，继续本圈"
                    injury = None
            if injury:
                must_repair = injury == "重伤"
                action = str(injury_action or "continue")
                if action in ("true", "1"):
                    action = "continue"
                elif action in ("false", "0"):
                    action = "stop"
                if not must_repair and action == "stop":
                    yield f"[出阵] 检测到部队{team_no}{injury}；自动手入已关闭，本次收工"
                    return
                # 重伤必须尝试手入；即便用户选择“不手入”，也改为普通手入后收工。
                repair_and_stop = action == "repair_stop" or (must_repair and action == "stop")
                suffix = "，不用加速符，手入后收工" if repair_and_stop else "，转去手入后重试本圈"
                yield f"[出阵] 检测到部队{team_no}{injury}{suffix}"
                for repair_msg in self.repair_stream(
                        dry_run=False,
                        use_speedup=False if repair_and_stop else None,
                        # 继续原定出阵时，本次出阵队必须即时修好；
                        # 手入列表里的其他队仍会送修，但不会使用加速符。
                        speedup_teams=None if repair_and_stop else [team_no]):
                    yield repair_msg
                if repair_and_stop:
                    yield "[出阵] 已安排手入（黑名单已跳过、未使用加速符），本次收工"
                    return
                repair_attempts += 1
                if repair_attempts >= 2:
                    yield "[出阵] 手入后仍检测到伤势（可能是黑名单或未加速成员），停止"
                    return
                continue

            # 只在整轮任务第一次出阵前保存。战斗后刀装可能已碎，绝不能在
            # 下一圈覆盖记录一，否则保存下来的就是缺刀装状态。
            if auto_equip_active and not record_saved:
                yield "[出阵] 自动补充刀装已开启，先把当前部队保存到记录一"
                if self._save_team_record(cfg, record_no=1):
                    record_saved = True
                    yield "[出阵] ✓ 当前部队已保存到记录一"
                else:
                    yield "[出阵] ⚠️ 没能安全保存记录一，已停止；请查看是否有确认弹窗未处理"
                    return

            # ========== 6. 同步游戏自动行军开关 ==========
            delegated_march = False
            if auto_march:
                if not self._configure_auto_march_settings(policy):
                    yield "[出阵] ⚠️ 自动行军详细设定没能确认，停止出阵"
                    return
                delegated_march = self._enable_auto_march()
                time.sleep(0.5)
                if not delegated_march:
                    yield "[出阵] ⚠️ 游戏自动行军没有挂成功，降级为脚本手动行军"
            if not delegated_march and not self._disable_auto_march():
                yield "[出阵] ⚠️ 没能确认游戏自动行军已关闭，停止出阵"
                return

            # ========== 7. 即刻出阵 → 刀装恢复分支 ==========
            equip_retries = 0
            while True:
                if not self._click_depart(cfg):
                    yield "[出阵] 找不到即刻出阵按钮（队长重伤会变灰？），停"
                    return

                self.maa.screenshot(force=True)
                if auto_equip_active:
                    equip_result = self._restore_equipment_from_warning(cfg, record_no=1)
                    if equip_result is None:
                        break
                    if not equip_result:
                        yield "[出阵] ⚠️ 刀装未满警告；从记录一恢复失败，已停止出阵"
                        return
                    equip_retries += 1
                    yield "[出阵] 🛡️ 刀装有空缺，已使用记录一自动补齐"
                    if equip_retries >= 2:
                        yield "[出阵] 恢复刀装后仍出现空缺警告，停止重试"
                        return
                    # 使用记录后重新做保命检查；异常时绝不再次点出阵。
                    self.maa.screenshot(force=True)
                    restored_injury, _ = self._combined_injury_status(
                        cfg, team_no, log_cache)
                    if restored_injury and self._injury_reaches_threshold(
                            restored_injury, repair_threshold):
                        yield f"[出阵] 恢复刀装后检测到{restored_injury}，不再出阵"
                        return
                    if auto_march:
                        delegated_march = self._enable_auto_march()
                    if not delegated_march and not self._disable_auto_march():
                        yield "[出阵] ⚠️ 恢复刀装后没能确认自动行军已关闭，停止出阵"
                        return
                    continue

                # 未开启自动补充时保持原安全行为：点整备刀装退出，不碰继续出阵。
                equip_cancelled = self._cancel_equip_warning(cfg)
                if equip_cancelled is None:
                    break
                if equip_cancelled:
                    yield "[出阵] ⚠️ 刀装未满警告；已进入整备，本次跳过"
                else:
                    yield "[出阵] ⚠️ 刀装未满警告；没能安全进入整备，本次出阵停止"
                return

            # 队员重伤确认弹窗 → 教材规矩：永远点"否"
            if self._deny_heavy_injury_warning(cfg):
                yield "[出阵] 🛑 队员重伤确认弹窗，已点【否】。有重伤绝不出阵，停"
                return

            if cfg_key == "yosari":
                confirmed = yield from self._confirm_yosari_departure(
                    cfg, auto_refill=auto_refill)
                if confirmed == "refilled":
                    yield "[异去] 已补充归城提灯，重新点击即刻出阵"
                    if not self._click_depart(cfg):
                        yield "[异去] 补充后找不到即刻出阵按钮，收工"
                        return
                    confirmed = yield from self._confirm_yosari_departure(
                        cfg, auto_refill=auto_refill, refill_attempted=True)
                if not confirmed:
                    return

            # ========== 7.5 一圈的开始边界：确认全部通过、部队真正出发 ==========
            # 从这里到回本的耗时是纯游戏流程耗时，第一圈也有精确起点。
            loop_started_at = time.time()
            # 战斗结算观察是实例级状态：掉落识别、返回本丸等内部取到的新帧
            # 也走同一个入口计数，不允许任何帧绕过（见 _watch_battle_frame）
            self._battle_watch = {"battles": 0, "visible": False}
            drops_this_loop = []
            loop_march_mode = "delegated" if delegated_march else "script"
            self._drop_watch_failed = False
            attempt = loop_attempts.get(loop_no, 0) + 1
            loop_attempts[loop_no] = attempt
            if hasattr(self, "record_event"):
                self.record_event(
                    "sortie.loop_started", mode=cfg_key, chapter=chapter,
                    map_no=map_no, team_no=team_no, sequence=loop_no,
                    attempt=attempt, march_mode=loop_march_mode)

            def end_loop(event_type, outcome, reason=None):
                self._record_sortie_loop_end(
                    event_type, outcome=outcome, reason=reason,
                    cfg_key=cfg_key, chapter=chapter, map_no=map_no,
                    team_no=team_no, loop_no=loop_no, attempt=attempt,
                    started_at=loop_started_at,
                    battles=self._battle_watch["battles"],
                    march_mode=loop_march_mode, drops=drops_this_loop)

            yield f"[出阵] 🐎 部队{team_no}出发！行军监控开着呢，我全程盯着"

            # ========== 8. 行军监控：打完自动回本丸 / 中断则返回本丸 ==========
            march_done = False
            interrupted = False
            retreated = False
            interrupt_reason = None
            # 王点航位推算状态：(上次认出的距王点步数, 此后盲走的步数)。
            # None = 还没有可信读数。每圈出阵重置。
            boss_track = None
            # 掉落认人去重：获得画面会等戳、连帧都在，只在「刚出现」时记一次
            drop_credit = None
            for _ in range(300):  # 安全上限
                self.maa.screenshot(force=True)
                if watch_dir is not None:
                    watch_seq += 1
                    self._dump_watch_frame(watch_dir, loop_no, watch_seq)

                # 战斗计数：每张新帧统一过 _watch_battle_frame，
                # 必须放在任何可能点掉结算页的动作之前。
                self._watch_battle_frame()

                if self._deny_heavy_injury_warning(cfg):
                    yield "[出阵] 🛑 出现重伤行军警告，已点【否】，准备返回本丸"
                    self.maa.screenshot(force=True)
                    if not self._return_home_from_march(cfg):
                        end_loop("sortie.interrupted", "unknown",
                                 reason="heavy_injury_denied_return_failed")
                        yield "[出阵] 点否后找不到返回本丸按钮，已停止点击"
                        return
                    interrupted = True
                    interrupt_reason = "heavy_injury_warning"
                    break

                # 异去一圈结束后回到四张小图页，不会回本丸。
                if cfg_key == "yosari" and self._yosari_round_done(cfg):
                    march_done = True
                    map_page_ready = True
                    break

                # 普通合战场打完一圈会自动回本丸。
                if self.maa.template_match(cfg["home_ui"]["template"]):
                    march_done = True
                    break

                # 游戏自动行军确认挂上后，路线与阵形全交给游戏，不介入。
                # 行军停止必须认“自动行军停止”横幅；不能拿“返回本丸”按钮判定，
                # 因为那个按钮在行军区常驻，误认后点击就会变成脚本主动撤退。
                if delegated_march:
                    stop_ocr = cfg["march_stop_ocr"]
                    stop_roi = roi_4to4(*stop_ocr["roi"])
                    if self.maa.ocr(expected=stop_ocr["expected"], roi=stop_roi):
                        field_injury = self._team_injury_status(cfg)
                        detail = f"（检测到{field_injury}）" if field_injury else ""
                        yield f"[出阵] ⚠️ 游戏自动行军已经停止{detail}，准备安全返回本丸"
                        if not self._return_home_from_march(cfg):
                            end_loop("sortie.interrupted", "unknown",
                                     reason="auto_march_stopped_return_failed")
                            yield "[出阵] 找不到返回本丸按钮，停止点击，等你手动处理"
                            return
                        if not field_injury or not self._injury_reaches_threshold(field_injury, repair_threshold):
                            end_loop("sortie.interrupted", "interrupted", reason="auto_march_non_injury_stop")
                            yield "[出阵] 游戏自动行军因其他条件停止，已回本丸收工"
                            return
                        interrupted = True
                        interrupt_reason = "auto_march_stopped"
                        break
                    self._click_point(cfg["skip_tap"])
                    time.sleep(0.8)
                    continue

                # 只有自动委托没挂上，或用户明确选择脚本手动行军，才处理阵形和岔路。
                # ——刷花实测卡死在这的教训
                if self._formation_mode_state(
                        allow_auto_without_title=formation_mode != "auto") is not None:
                    result = self.choose_formation(
                        formation_name=formation,
                        enable_auto=formation_mode == "auto",
                    )
                    chosen = {"advantage": "有利阵形", "auto": "游戏自动阵形"}.get(
                        result, formation)
                    yield f"[出阵] 🛡️ 已选择「{chosen}」继续"
                    # 阵形确认后的转场略慢；等页面真正消失，避免下一轮重复点阵。
                    for _ in range(8):
                        time.sleep(0.4)
                        self.maa.screenshot(force=True)
                        if self._formation_mode_state() is None:
                            break
                    continue

                # 手动行军决策屏（委托没挂上时每个节点都问）：点"行军"继续
                # ——刷花实测：_enable_auto_march 会静默失败，不能全指望委托
                march_button = self._find_march_continue(cfg)
                if march_button:
                    field_injury = self._team_injury_status(cfg)
                    if field_injury and self._injury_reaches_threshold(
                            field_injury, repair_threshold):
                        yield f"[出阵] 🩹 局内检测到{field_injury}，已达到手入阈值，不再继续行军"
                        if not self._return_home_from_march(cfg):
                            end_loop("sortie.interrupted", "unknown",
                                     reason="field_injury_return_failed")
                            yield "[出阵] 找不到返回本丸按钮，停止点击，等你手动处理"
                            return
                        interrupted = True
                        interrupt_reason = "field_injury_threshold"
                        break
                    # 王点前撤退（仅合战场 + 脚本手动行军）：决策屏右上小地图
                    # 永远干净完整，距王点 1 步 = 下一脚就是王点，撤。
                    # 认不出来（None）本身不是撤退信号——但允许一次航位推算：
                    # 上次明确读到 2 步、此后只盲走了 1 步，估算 == 1 才敢撤。
                    # 盲走更多不猜：岔路骰子可能把距离带涨，估错就是半路白回家。
                    if retreat_before_boss and cfg_key == "sortie":
                        if not CV2_AVAILABLE:
                            yield "[出阵] 🗺️ 王点前撤退需要 opencv，当前环境没装，本圈按普通行军跑"
                            retreat_before_boss = False
                        else:
                            boss_dist = boss_distance_from_image(
                                self.maa.screenshot())
                            if boss_dist == 1:
                                yield "[出阵] 🏳️ 小地图看明白了：下一脚就是王点，按约定撤退回本丸"
                                if not self._return_home_from_march(cfg):
                                    end_loop("sortie.interrupted", "unknown",
                                             reason="retreat_return_failed")
                                    yield "[出阵] 找不到返回本丸按钮，停止点击，等你手动处理"
                                    return
                                retreated = True
                                break
                            if boss_dist is None:
                                self._save_map_miss(chapter, map_no, loop_no)
                                if boss_track == (2, 1):
                                    yield ("[出阵] 🏳️ 这帧小地图没认明白，但上次明确读到距王点 2 步、"
                                           "此后只走了 1 步——航位推算下一脚就是王点，按约定撤退。"
                                           "要是估错了（岔路骰子搞事）算我的，map_miss 里有现场")
                                    if not self._return_home_from_march(cfg):
                                        end_loop("sortie.interrupted", "unknown",
                                                 reason="retreat_return_failed")
                                        yield "[出阵] 找不到返回本丸按钮，停止点击，等你手动处理"
                                        return
                                    retreated = True
                                    break
                                yield "[出阵] 🗺️ 小地图这帧没认明白，这步先照常走"
                            else:
                                boss_track = (boss_dist, 0)
                                yield f"[出阵] 🗺️ 距王点还有 {boss_dist} 步，继续行军"
                    yield "[出阵] 🚩 岔路口问我话呢，点「行军」继续"
                    self.maa.click(march_button)
                    if boss_track is not None:
                        boss_track = (boss_track[0], boss_track[1] + 1)
                    time.sleep(1.0)
                    continue

                # 掉落获得画面：左下对话框名牌认人（画面等戳不自动翻页；
                # 自动行军时游戏自己跳过获得动画，认不到正常）
                drop_result = self._read_drop_sword()
                if drop_result["status"] == "recognized":
                    dropped = drop_result["sword"]
                    if drop_credit != dropped["sword_id"]:
                        drop_credit = dropped["sword_id"]
                        drops_this_loop.append(dropped["sword_id"])
                        yield f"[出阵] 🎉 刀剑男士【{dropped['name']}】来本丸了！"
                        if hasattr(self, "record_event"):
                            self.record_event(
                                "sword.obtained", **dropped,
                                source="sortie.drop", chapter=chapter,
                                map_no=map_no, sequence=loop_no,
                                attempt=attempt)
                elif drop_result["status"] == "unrecognized":
                    # 掉刀证据在、名字没认出（认人器已置观察降级旗）：
                    # 本圈绝不落 confirmed_none；之前拍认出过名字的圈
                    # 不受影响（有掉落事实优先）。同大阪城一样留一条
                    # 结构化事实——圈总结是 recognized 也不能吞掉
                    # "另一振没认出来"，刀名绝不当唯一身份。
                    if drop_credit != "unrecognized":
                        drop_credit = "unrecognized"
                        if hasattr(self, "record_event"):
                            self.record_event(
                                "sword.drop_unrecognized",
                                source="sortie.drop", mode=cfg_key,
                                chapter=chapter, map_no=map_no,
                                sequence=loop_no, attempt=attempt)
                else:
                    drop_credit = None

                # 安全区跳动画
                self._click_point(cfg["skip_tap"])
                time.sleep(0.8)

            if march_done:
                if cfg_key == "yosari":
                    yield f"[异去] ✓ 第 {loop_no} 圈结束，已回到异去小图页"
                    self.current_location = "出阵"
                else:
                    yield f"[出阵] ✓ 第 {loop_no} 圈凯旋！已回本丸"
                    self.current_location = "本丸"
                end_loop("sortie.completed", "completed")
                repair_attempts = 0
                if cfg_key == "yosari":
                    inv = self._read_yosari_fragments(cfg)
                    if inv == "stuck":
                        yield "[异去] 碎片弹窗关不掉，为防误点收工；你去瞅一眼"
                        return
                    if inv:
                        gained = {}
                        if frag_inv and frag_inv.get("map_no") == inv["map_no"]:
                            for name, count in inv["counts"].items():
                                diff = count - frag_inv["counts"].get(name, 0)
                                if diff > 0:
                                    gained[name] = diff
                        if hasattr(self, "record_event"):
                            self.record_event(
                                "yosari.fragments", map_no=inv["map_no"],
                                sequence=loop_no, counts=inv["counts"],
                                gained=gained)
                        frag_inv = inv
                        if gained:
                            yield ("[异去] 🧩 碎片进账：" + "、".join(
                                f"{name}×{num}" for name, num in gained.items()))
                loop_no += 1
            elif interrupted:
                yield "[出阵] ⚠️ 行军因伤势中断，已返回本丸；重新检查轻/中/重伤"
                self.current_location = "本丸"
                end_loop("sortie.interrupted", "interrupted",
                         reason=interrupt_reason)
                continue
            elif retreated:
                # 王点前主动撤退算完成一圈（练级打法），回满状态接着进下一圈
                yield f"[出阵] ✓ 第 {loop_no} 圈王点前撤退完成，已回本丸"
                self.current_location = "本丸"
                end_loop("sortie.retreated_before_boss", "retreated_before_boss")
                repair_attempts = 0
                loop_no += 1
            else:
                end_loop("sortie.interrupted", "unknown",
                         reason="monitor_timeout")
                yield "[出阵] ⚠️ 行军监控超过安全上限，强制停，你去看看卡哪了"
                return

        yield f"[出阵] ✓ 全部 {max_loops} 圈跑完，部队{team_no}辛苦啦，收工！"
        return

    def _watch_battle_frame(self):
        """行军期间每张实际运行帧的统一战斗结算观察入口。

        锚点：结算页「戦闘結果」每场战斗必现且停留 ≥2 帧（2026-09-05 异去
        委托行军 148 帧运行实录，9 场全中、场间空窗 ≥6 帧）。出现沿计一场、
        消失后重新武装，同一画面重复帧不重数。掉落识别、返回本丸等流程
        内部取到的新帧也必须过这里，不允许任何帧绕过计数。
        未处于行军监控（self._battle_watch 为 None）时直接返回。
        """
        watch = getattr(self, "_battle_watch", None)
        if watch is None:
            return
        if self.maa.template_match("battle/ui战斗结果.png"):
            if not watch["visible"]:
                watch["battles"] += 1
            watch["visible"] = True
        else:
            watch["visible"] = False

    def _record_sortie_loop_end(self, event_type, *, outcome, reason=None,
                                cfg_key, chapter, map_no, team_no, loop_no,
                                attempt, started_at, battles, march_mode,
                                drops):
        """一圈出阵的结束事实。只写真实可证的状态，不可知的字段给 null/说明。

        掉落可观察状态（drop_observation）：
          recognized     —— 本圈真的认到掉落（drops 非空）
          not_observed   —— 观察不成立：委托自动行军游戏跳过获得动画 /
                            观察中断（结局未知）/ 认人流程内部异常
          confirmed_none —— 脚本手动行军且全程逐帧盯屏、认人流程无异常，
                            确实没有掉落出现（可进掉率分母）
        not_observed 既不是「掉了」也不是「没掉」，统计掉率时必须剔除。
        """
        if not hasattr(self, "record_event"):
            return
        duration = round(time.time() - started_at, 1) if started_at else None
        drop_reason = None
        if drops:
            drop_observation = "recognized"
        elif march_mode == "delegated":
            drop_observation = "not_observed"
            drop_reason = "auto_march_skips_obtain_animation"
        elif outcome == "unknown":
            drop_observation = "not_observed"
            drop_reason = "observation_lost"
        elif getattr(self, "_drop_watch_failed", False):
            drop_observation = "not_observed"
            drop_reason = "recognizer_error"
        else:
            drop_observation = "confirmed_none"
        battle_count = battles
        battle_note = None
        # 打完王点才算正常完成一圈，王点战必有结算页；正常完成却 0 场
        # 只可能是锚点失明——宁可 unknown 也不拿 0 污染时间/掉落矩阵
        if outcome == "completed" and battle_count == 0:
            battle_count = None
            battle_note = "completed without any battle result page; anchor presumed blind"
        payload = {
            "mode": cfg_key, "chapter": chapter, "map_no": map_no,
            "team_no": team_no, "sequence": loop_no, "attempt": attempt,
            "outcome": outcome,
            "duration_seconds": duration,
            "march_mode": march_mode,
            "battle_count": battle_count,
            "battle_count_basis": "battle_result_page_edges",
            "drop_observation": drop_observation,
            "drops_recognized": len(drops),
        }
        if reason:
            payload["interrupt_reason"] = reason
        if drop_reason:
            payload["drop_observation_reason"] = drop_reason
        if battle_note:
            payload["battle_count_note"] = battle_note
        self.record_event(event_type, **payload)
        self._battle_watch = None  # 本圈观察关闭，下一圈出发时重新武装

    # 掉落获得画面的识别点位（1280x720，真机截图校准）
    _OBTAIN_BADGE_ROI = (1105, 40, 1170, 160)   # 右侧立牌的红底「刀派」徽（稀有款）
    # 掉刀预告横幅「发现了新的刀剑男士！」：点掉结算页后 +0.4~1.6s 贴在结算页
    # 上（2026-08-24 真机连拍），是掉刀最早最稳的信号
    _OBTAIN_BANNER_ROI = (100, 280, 1180, 460)
    _OBTAIN_BANNER_TEXT = "新的刀剑男士"
    # 左下对话框名牌「打刀 大和守安定」。获得画面有两种布局：带刀派立牌的
    # 对话框偏下（名牌 y≈630-690），不带的偏上（y≈500-560，2026-08-24 真机
    # 连拍实测），宽 ROI 两种都罩住。
    _NAME_PLATE_ROI = (0, 480, 430, 710)
    # 名牌 = 刀种前缀 + 空格 + 名字；名字里不许夹标点（防 OCR 粘连对话框台词）
    _STRIP_RE = re.compile(
        r"^\s*(短刀|胁差|打刀|太刀|大太刀|枪|薙刀|剑)[\s_　]*([^\s，。,.、！!？?]+)")
    _SWORD_TYPES = ("短刀", "胁差", "打刀", "太刀", "大太刀", "枪", "薙刀", "剑")

    def _read_drop_sword(self):
        """掉落获得画面认人：横幅预告 + 名牌确认，两段式。

        时机（2026-08-24 真机连拍，从点掉结算页起算）：+0.4~1.6s 横幅
        「发现了新的刀剑男士！」→ +2~3s 樱花转场 → +3.5~4.8s 裸立绘
        （名牌区全空，啥都认不到）→ +5s 起立牌+对话框+名牌齐活，画面
        静止等戳。所以名牌区空时先看横幅：有横幅 = 掉刀预告，耐心等名牌
        到位（约 5 秒），绝不提前放弃——提前返回 None，下一拍安全区点击
        正好把刚到位的获得画面点掉（漏认的元凶）。没横幅按老节奏快速退场，
        不拖慢正常行军圈。
        名牌的「刀种+名字」组合只有获得画面有；结果页成员栏裸名没有刀种
        前缀，天然拒认（2026-08-24 假博多事故的根治）。认错比认不到糟，
        名字走名册严格匹配（关模糊兜底）。

        返回三态（调用方必须区分，绝不能把"没看到"和"看到但没认出"都
        当成无掉落）：
          {"status": "recognized",   "sword": {...}}  名字认出
          {"status": "unrecognized", "sword": None}   见过横幅/刀种/立牌等
                掉刀证据但名字没认出，或认人流程自身异常。返回该状态时
                本方法已置 _drop_watch_failed：本圈观察不可信，不许落
                confirmed_none；后续拍若认出名字，掉落事实照常优先
          {"status": "none",         "sword": None}   全程没有任何掉刀证据
        """

        def _recognized(found):
            sid, info = found
            return {"status": "recognized",
                    "sword": {"sword_id": sid,
                              "name": info.get("name_zh") or info["name"],
                              "name_jp": info["name"]}}

        try:
            def _grab():
                # 内部取的每一张新帧也是行军观察帧，必须过战斗计数入口
                self.maa.screenshot(force=True)
                self._watch_battle_frame()

            evidence = False  # 见过横幅/刀种前缀/立牌徽章 = 确知掉刀
            patient = False
            for attempt in range(10):  # 耐心模式覆盖横幅→名牌约 5 秒，余量翻倍
                tokens = self.maa.ocr_all(roi_4to4(*self._NAME_PLATE_ROI))
                saw_prefix = False
                for text, _pt in tokens:
                    m = self._STRIP_RE.match(text)
                    if m:
                        saw_prefix = True
                        found = sword_db.find_by_name(m.group(2), fuzzy=False)
                        if found:
                            return _recognized(found)
                    elif text.strip() in self._SWORD_TYPES:
                        saw_prefix = True
                if saw_prefix:
                    evidence = True
                    # 刀种和名字被 OCR 拆成两条：有刀种在场，裸名也认
                    # （结果页成员栏没有刀种前缀，进不来这个分支）
                    for text, _pt in tokens:
                        if self._STRIP_RE.match(text) or text.strip() in self._SWORD_TYPES:
                            continue
                        found = sword_db.find_by_name(text.strip(), fuzzy=False)
                        if found:
                            return _recognized(found)
                    time.sleep(0.8)  # 名牌在但名字没匹配上（OCR 花了），重读
                    _grab()
                    continue
                # 名牌区空空：横幅在 = 掉刀预告，转耐心模式等获得画面到位
                if not patient and self.maa.ocr(
                        self._OBTAIN_BANNER_TEXT,
                        roi_4to4(*self._OBTAIN_BANNER_ROI)):
                    evidence = True
                    patient = True
                if patient:
                    time.sleep(0.8)
                    _grab()
                    continue
                # 没横幅：有立牌=获得画面，等对话框滑入重读；
                # 没立牌先给一拍（对话框可能还在路上），第二拍还空就不是获得画面
                if self.maa.ocr("刀派", roi_4to4(*self._OBTAIN_BADGE_ROI)):
                    evidence = True
                    time.sleep(0.8)
                    _grab()
                    continue
                if attempt == 0:
                    time.sleep(0.6)
                    _grab()
                    continue
                return {"status": "none", "sword": None}
        except Exception:
            # 认人流程翻车 = 这段行军掉落观察不可信，本圈不许 confirmed_none
            self._drop_watch_failed = True
            return {"status": "unrecognized", "sword": None}
        # 耐心拍数耗尽：见过掉刀证据就得承认"有掉刀但没认出名字"，
        # 绝不许假装没看见（那会把真实掉落记成"确认无掉落"）
        if evidence:
            self._drop_watch_failed = True
            return {"status": "unrecognized", "sword": None}
        return {"status": "none", "sword": None}

    def _dismiss_yosari_milestone(self, cfg: dict) -> bool:
        """收掉异去累计圈数里程碑弹窗（500/800/1100 圈送火车切）。

        弹窗是「确定」键而不是章节页的「决定」键，只会等决定的循环
        被它卡死（2026-09-10 第 62 圈实测翻车：老大手动点确定后才
        看到火车切获得窗）。双条件防误点：确定按钮 + 画面里出现
        「奖励」才动手（老大回忆弹窗原句说的是"奖励"而非刀名，
        「火车切」留作兜底）；点完确定还有获得窗，用安全区收掉。
        返回 True = 本回合收过弹窗。
        """
        full = roi_4to4(0, 0, 1280, 720)
        handled = False
        for _ in range(3):
            self.maa.screenshot(force=True)
            confirm = self.maa.template_match("通用_确定.png")
            if not confirm:
                break
            if not (self.maa.ocr("奖励", full) or self.maa.ocr("火车切", full)):
                break
            self.maa.click(confirm)
            handled = True
            time.sleep(1.2)
        if handled:
            self.skip_safe(3, point=cfg.get("skip_tap"))
            if hasattr(self, "record_event"):
                self.record_event("yosari.milestone_claimed")
        return handled

    def _enter_yosari(self, cfg: dict) -> bool:
        """从默认的“过去”切换到右上角“异去”，并用文字复核。"""
        entry = cfg.get("entry", {})
        roi = roi_4to4(*entry.get("verify_roi", [285, 50, 850, 145]))
        expected = entry.get("expected", "归城提灯")
        self.maa.screenshot(force=True)
        if self.maa.ocr(expected, roi):
            return True
        self._click_point(entry.get("target", [978, 93]))
        # 异去是渐入进场的动画演出：进场途中归城提灯会被盖住/误认，
        # 先等 2 秒让页面稳定再认，别在动画里乱判
        time.sleep(2.0)
        for _ in range(6):
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected, roi):
                return True
            time.sleep(0.5)
        return False

    def _confirm_yosari_departure(self, cfg: dict, auto_refill: bool = False,
                                  refill_attempted: bool = False):
        """处理新版归城提灯确认；缺灯时按四段确认链补充后返回部队选择。"""
        prompt = cfg.get("departure_confirm", {})
        roi = roi_4to4(*prompt.get("roi", [310, 155, 970, 330]))
        expected = prompt.get("expected", "归城提灯进行出阵")
        for _ in range(8):
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected, roi):
                self._click_point(prompt.get("confirm_target", [640, 603]))
                time.sleep(1.5)
                break
            time.sleep(0.5)
        else:
            yield "[异去] 没看到归城提灯出阵确认框；停止"
            return False

        refill = cfg.get("ticket_refill", {})

        def _wait_text(step: dict, fallback_expected: str, fallback_roi: list) -> bool:
            text = step.get("expected", fallback_expected)
            text_roi = roi_4to4(*step.get("roi", fallback_roi))
            for _ in range(8):
                self.maa.screenshot(force=True)
                if self.maa.ocr(text, text_roi):
                    return True
                time.sleep(0.4)
            return False

        purchase_screen = refill.get("purchase_screen", {})
        if not _wait_text(purchase_screen, "补充所需", [270, 75, 1010, 555]):
            # 没进入购买页，说明现有提灯有效，第一次确认后已经真正出阵。
            yield "[异去] 已确认使用现有归城提灯（未勾选探索道具）"
            return True

        if refill_attempted:
            yield "[异去] 补充后仍进入购买页，停止，避免重复消费小判"
            return False
        if not auto_refill:
            self._click_point(purchase_screen.get("close_target", [1040, 48]))
            yield "[异去] 归城提灯不足；自动补充已关闭，不消耗小判，收工"
            return False

        purchase_ledger = self._read_yosari_koban_purchase(purchase_screen)
        yield "[异去] 归城提灯不足，开始四段确认中的补充步骤"
        self._click_point(purchase_screen.get("confirm_target", [638, 611]))
        time.sleep(1.2)

        spend_confirm = refill.get("spend_confirm", {})
        if not _wait_text(spend_confirm, "是否消耗", [300, 80, 1010, 560]):
            yield "[异去] 没看到消耗小判的确认页，停止点击"
            return False
        self._click_point(spend_confirm.get("confirm_target", [784, 604]))
        time.sleep(1.2)

        completed = refill.get("completed", {})
        if not _wait_text(completed, "补充了归城提灯", [330, 120, 950, 460]):
            yield "[异去] 没看到归城提灯补充完成页，停止点击"
            return False
        event_payload = {
            "item": "归城提灯一",
            "source": "yosari.ticket_refill",
            "evidence": "purchase_completed",
        }
        if purchase_ledger:
            event_payload.update(purchase_ledger)
            self.record_event(
                "resource.change",
                resource="小判",
                delta=purchase_ledger["delta"],
                before=purchase_ledger["before"],
                after=purchase_ledger["after"],
                source="yosari.ticket_refill",
                attribution="confirmed",
                evidence="purchase_preview_balances",
                note="异去归城提灯补充",
            )
        self.record_event("yosari.ticket_refilled", **event_payload)
        self._click_point(completed.get("confirm_target", [638, 511]))
        time.sleep(1.2)
        if purchase_ledger:
            yield (f"[异去] 归城提灯补充完成，小判 "
                   f"{purchase_ledger['delta']:+d}，已回到部队选择")
        else:
            yield "[异去] 归城提灯补充完成；小判金额未识别，已回到部队选择"
        return "refilled"

    def _read_yosari_koban_purchase(self, purchase_screen: dict):
        """读取购买页展示的购买前/后小判；读不全时不猜金额。"""
        ocr_all = getattr(self.maa, "ocr_all", None)
        if not callable(ocr_all):
            return None

        def _read_one(key: str, fallback: list):
            try:
                rows = ocr_all(roi_4to4(*purchase_screen.get(key, fallback)))
            except Exception:
                return None
            ordered = sorted(rows or [], key=lambda row: getattr(row[1], "x", 0))
            digits = "".join(re.sub(r"\D", "", str(text))
                             for text, _point in ordered)
            return int(digits) if digits else None

        before = _read_one("balance_before_roi", [465, 345, 625, 410])
        after = _read_one("balance_after_roi", [675, 345, 835, 410])
        if before is None or after is None or after >= before:
            return None
        return {"before": before, "after": after, "delta": after - before}

    def _yosari_round_done(self, cfg: dict) -> bool:
        """归城提灯标题与部队选择同时出现，才算确实回到异去小图页。"""
        marker = cfg.get("round_end_ocr", cfg.get("entry", {}))
        roi_raw = marker.get("roi", marker.get("verify_roi", [285, 50, 850, 145]))
        expected = marker.get("expected", "归城提灯")
        if not self.maa.ocr(expected, roi_4to4(*roi_raw)):
            return False
        return bool(find_deploy_button(self.maa, cfg))

    def _yosari_watch_dir(self, cfg_key: str):
        """蹲点目录：仅异去且显式开环境变量才启用，返回 None 表示不蹲。"""
        if cfg_key != "yosari" or os.environ.get("MAAMARU_YOSARI_WATCH") != "1":
            return None
        try:
            from ..runtime_paths import STATUS_DIR
            folder = STATUS_DIR / "debug" / "yosari_watch" / time.strftime("%Y%m%d_%H%M%S")
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except Exception:
            return None

    def _dump_watch_frame(self, folder, loop_no: int, seq: int):
        """把行军监控刚取的帧存成 JPEG；任何异常都不许打断行军。"""
        try:
            image = self.maa.screenshot(force=False)
            if image is None:
                return
            from PIL import Image as _PILImage
            _PILImage.fromarray(image[:, :, ::-1]).save(
                str(folder / f"loop{loop_no}_{seq:04d}.jpg"), quality=70)
        except Exception:
            pass

    def _read_yosari_fragments(self, cfg: dict):
        """小图页开「宝物碎片」弹窗，OCR 读各类碎片所持数，读完关窗。

        返回 {"map_no": int|None, "counts": {碎片名: 个数}}；
        弹窗没开起来或识别翻车都返回 None，绝不影响出阵；
        弹窗关不掉返回 "stuck"——调用方必须收工，弹窗会挡住选图点击。
        """
        popup = cfg.get("fragments_popup", {})
        button = cfg.get("fragments_button", [895, 632])
        title = popup.get("title_expected", "可以获得的宝物碎片")
        close = popup.get("close", [1255, 30])
        roi = roi_4to4(*popup.get("ocr_roi", [30, 30, 1230, 500]))

        def _popup_tokens() -> list:
            self.maa.screenshot(force=True)
            return [(text, pt.x, pt.y) for text, pt in self.maa.ocr_all(roi)]

        try:
            self._click_point(button)
            time.sleep(1.2)
            tokens = _popup_tokens()
            if not any(title in text for text, _, _ in tokens):
                return None
            result = {"map_no": _parse_popup_map(tokens),
                      "counts": _parse_fragment_counts(tokens)}
        except Exception:
            return None
        for _ in range(3):
            self._click_point(close)
            time.sleep(0.8)
            try:
                if not any(title in text for text, _, _ in _popup_tokens()):
                    return result
            except Exception:
                return None
        return "stuck"

    def _save_map_miss(self, chapter: int, map_no: int, loop_no: int):
        """只保存无法判读的小地图，供用户主动反馈；正常识别不落盘。"""
        if self._map_miss_count >= 5:
            return
        try:
            from ..runtime_paths import STATUS_DIR
            folder = STATUS_DIR / "map_miss"
            folder.mkdir(parents=True, exist_ok=True)
            name = (f"miss_{chapter}-{map_no}_loop{loop_no}_"
                    f"{time.strftime('%H%M%S')}.png")
            if self.maa.save_screenshot(str(folder / name), force=False):
                self._map_miss_count += 1
        except Exception:
            pass

    def _find_march_continue(self, cfg: dict):
        """只在右下角按钮区寻找完整“行军”按钮，避免命中自动行军横幅。"""
        march_cfg = cfg.get("march_continue_button", {})
        roi = roi_4to4(*march_cfg.get("roi", [990, 510, 1280, 720]))
        template = march_cfg.get("template", "battle/行军.png")
        return self.maa.template_match(template, roi)

    def _return_home_from_march(self, cfg) -> bool:
        """在行军选择/停止画面安全返回本丸，并等待本丸真正出现。

        长时间挂机后游戏回本丸偶尔会卡在加载过渡几十秒。这里必须等到本丸
        标志实际出现，不能只因点击了确认按钮就把后续盘点放出去。
        """
        stop_btn = None
        for _ in range(6):
            self.maa.screenshot(force=True)
            self._watch_battle_frame()  # 停止横幅下的结算页也算行军观察帧
            stop_btn = self.maa.template_match(cfg["march_stop_button"]["template"])
            if stop_btn:
                break
            time.sleep(0.5)
        if not stop_btn:
            return False
        self.maa.click(stop_btn)
        time.sleep(1.5)
        self.maa.screenshot(force=True)
        yes = self.maa.template_match(cfg["return_home_confirm"]["template"])
        if yes:
            self.maa.click(yes)
        time.sleep(2.0)
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            self.maa.screenshot(force=True)
            if self.maa.template_match(cfg["home_ui"]["template"]):
                return True
            time.sleep(0.8)
        return False
