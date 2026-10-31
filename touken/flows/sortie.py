# -*- coding: utf-8 -*-
"""出阵共用循环：选图、完整安全出发、行军、回本丸与同圈重试。

本模块不读截图、不解释模板/OCR/坐标；画面观察和操作由服务器适配提供。
宿主继续提供导航、BattleMixin 安全链、伤势恢复及事件记录。
SortieMixin 保留现有国服调用入口；SortieLoopMixin 可与其他服的适配组合。
"""
import time

from ..injury_recovery import recover_injury_stream
from ..flow_control import BattleProgress
from .sortie_cn import CnSortieViewMixin, _parse_fragment_counts, _parse_popup_map


class SortieLoopMixin:
    """共享编排，不提供任何服务器的画面兜底。"""

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
        cfg, map_cfg, teams = self._sortie_config(cfg_key, map_type)
        if not cfg:
            yield f"[出阵] 未配置{map_type}"
            return

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
                    if not self._sortie_wait_decide(cfg, timeout_s=90):
                        # 弹窗可能在等待中途才弹出来，收一次再试
                        if self._dismiss_yosari_milestone(cfg):
                            yield "[异去] 🎁 累计圈数里程碑奖励（火车切）已领，继续"
                            if not self._sortie_wait_decide(cfg, timeout_s=30):
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
                    self._sortie_capture_frame()
                    if self._on_team_select_page(cfg):
                        jumped = True
                        break
                    if self._sortie_find_deploy(cfg):
                        area_ok = True
                        break
                    now = time.monotonic()
                    if now - last_nav_tap >= 2.0:
                        decide = self._sortie_decide_button(cfg)
                        if decide:
                            if nav_tapped:
                                yield "[出阵] 章节页没点动，再补一次章节+决定"
                            self._sortie_tap(map_cfg["chapters"][str(chapter)])
                            time.sleep(0.5)
                            self._sortie_capture_frame()
                            decide = self._sortie_decide_button(cfg)
                            if decide:
                                self._sortie_click(decide)
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
                self._sortie_tap(map_cfg["maps"][str(map_no)])
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

            # 公共安全链负责选队、验伤、记录与刀装；这里仅提供合战场的
            # 行军准备。恢复刀装可能重置委托，必须重新确认再出发。
            delegated_march = False

            def prepare_march():
                nonlocal delegated_march
                delegated_march = False
                if auto_march:
                    if not self._configure_auto_march_settings(policy):
                        yield "[出阵] ⚠️ 自动行军详细设定没能确认，停止出阵"
                        return False
                    delegated_march = self._enable_auto_march()
                    time.sleep(0.5)
                    if not delegated_march:
                        yield "[出阵] ⚠️ 游戏自动行军没有挂成功，降级为脚本手动行军"
                if not delegated_march and not self._disable_auto_march():
                    yield "[出阵] ⚠️ 没能确认游戏自动行军已关闭，停止出阵"
                    return False
                return True

            departure_state = {}

            def depart():
                return self._safe_depart_stream(
                    cfg, team_no, "[出阵]", repair_threshold=repair_threshold,
                    auto_equip=auto_equip_active, team_record_saved=record_saved,
                    rotate_captain=rotate_captain,
                    rotate_captain_margin=rotate_captain_margin,
                    prepare_team_stream=prepare_march,
                    after_equipment_stream=prepare_march,
                    departure_state=departure_state)

            ok, record_saved = yield from depart()
            injury = departure_state.get("blocked_injury")
            if not ok and not injury:
                return
            if injury:
                recovery = yield from recover_injury_stream(
                    self, team_no=team_no, injury=injury,
                    threshold=repair_threshold, action=injury_action,
                    progress=BattleProgress(max_loops, loop_no - 1),
                    tag="[出阵]", repair_heavy_on_stop=True)
                if not recovery.resume:
                    recovery.raise_if_failed("[出阵]")
                    return
                continue

            # 普通合战场点击即进入行军，没有二次确认窗；异去的归城提灯
            # 确认仍保留独立规则，其确认点击复用公共 _confirm_departure。
            if cfg_key == "yosari":
                confirmed = yield from self._confirm_yosari_departure(
                    cfg, auto_refill=auto_refill)
                if confirmed == "refilled":
                    yield "[异去] 已补充归城提灯，重新点击即刻出阵"
                    # 补充后回到部队页，重新走完整安全链；不直接点出阵。
                    ok, record_saved = yield from depart()
                    if not ok:
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
                self._sortie_capture_frame()
                if watch_dir is not None:
                    watch_seq += 1
                    self._dump_watch_frame(watch_dir, loop_no, watch_seq)

                # 战斗计数：每张新帧统一过 _watch_battle_frame，
                # 必须放在任何可能点掉结算页的动作之前。
                self._watch_battle_frame()

                if self._deny_heavy_injury_warning(cfg):
                    yield "[出阵] 🛑 出现重伤行军警告，已点【否】，准备返回本丸"
                    self._sortie_capture_frame()
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
                if self._sortie_home_visible(cfg):
                    march_done = True
                    break

                # 游戏自动行军确认挂上后，路线与阵形全交给游戏，不介入。
                # 行军停止必须认“自动行军停止”横幅；不能拿“返回本丸”按钮判定，
                # 因为那个按钮在行军区常驻，误认后点击就会变成脚本主动撤退。
                if delegated_march:
                    if self._sortie_march_stopped(cfg):
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
                    self._sortie_tap(cfg["skip_tap"])
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
                        self._sortie_capture_frame()
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
                        if not self._sortie_can_read_boss():
                            yield "[出阵] 🗺️ 王点前撤退需要 opencv，当前环境没装，本圈按普通行军跑"
                            retreat_before_boss = False
                        else:
                            boss_dist = self._sortie_boss_distance()
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
                    self._sortie_click(march_button)
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
                self._sortie_tap(cfg["skip_tap"])
                time.sleep(0.8)

            if march_done:
                if cfg_key == "yosari":
                    yield f"[异去] ✓ 第 {loop_no} 圈结束，已回到异去小图页"
                    self.current_location = "出阵"
                else:
                    yield f"[出阵] ✓ 第 {loop_no} 圈凯旋！已回本丸"
                    self.current_location = "本丸"
                end_loop("sortie.completed", "completed")
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
                loop_no += 1
            else:
                end_loop("sortie.interrupted", "unknown",
                         reason="monitor_timeout")
                yield "[出阵] ⚠️ 行军监控超过安全上限，强制停，你去看看卡哪了"
                return

        yield f"[出阵] ✓ 全部 {max_loops} 圈跑完，部队{team_no}辛苦啦，收工！"
        return

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


class SortieMixin(CnSortieViewMixin, SortieLoopMixin):
    """现有国服入口：国服画面适配 + 共用出阵循环，保持调用契约。"""
