# -*- coding: utf-8 -*-
"""大阪城地下活动：从活动入口出阵，并逐层手动行军。"""

import re
import time

from ..maa_adapter import roi_4to4


class OsakaMixin:
    """大阪城挖地流程。依赖导航、共用部队选择和阵形组件。"""

    def osaka_stream(self, max_floors: int = 1, team_no: int = 3,
                     select_floor: bool = False, target_floor: int = 81,
                     formation_mode: str = "manual",
                     formation: str = "鱼鳞阵",
                     repair_threshold: str = "light",
                     injury_action: str = "continue",
                     auto_equip: bool = True,
                     _target_floors: int = None,
                     _completed_floors: int = 0,
                     _repair_count: int = 0,
                     _speedups_used: int = 0,
                     _team_record_saved: bool = False,
                     _auto_equip_active: bool = None,
                     koban_science: bool = True):
        """挖地外套：小判掉落率实验记账（开工/收场各读一次小判，差值≈掉落）。

        递归续跑会再次经过这里，用 _osaka_koban_open 保证只有最外层记账。
        finally 里只用 print 不用 yield（用户中途停止时生成器被 close，
        finally 里 yield 会炸 RuntimeError）；print 走 stdout 一样进日志。
        koban_science=False 时完全不读不写（面板「小判掉落率实验」开关）。
        """
        outermost = not getattr(self, "_osaka_koban_open", False)
        if outermost:
            # 不管测不测都先插旗：不然递归续跑会误以为自己是外层，重复记账
            self._osaka_koban_open = True
        measure = outermost and koban_science
        if measure:
            self._osaka_floors_total = 0
            self._osaka_koban_before = self._read_koban()
            if self._osaka_koban_before is not None:
                yield f"[挖地] 🧪 掉落率实验开测：开工小判 {self._osaka_koban_before}"
            else:
                yield "[挖地] 🧪 开工小判没读到（不在本丸？），收场再补"
        try:
            yield from self._osaka_stream_impl(
                max_floors=max_floors, team_no=team_no,
                select_floor=select_floor, target_floor=target_floor,
                formation_mode=formation_mode,
                formation=formation,
                repair_threshold=repair_threshold, injury_action=injury_action,
                auto_equip=auto_equip,
                _target_floors=_target_floors, _completed_floors=_completed_floors,
                _repair_count=_repair_count, _speedups_used=_speedups_used,
                _team_record_saved=_team_record_saved,
                _auto_equip_active=_auto_equip_active)
        finally:
            if outermost:
                self._osaka_koban_open = False
            if measure:
                before = getattr(self, "_osaka_koban_before", None)
                after = self._read_koban()
                floors_done = getattr(self, "_osaka_floors_total", 0)
                if before is not None and after is not None:
                    delta = after - before
                    per = f"，平均每层 {delta / floors_done:.1f}" if floors_done else ""
                    print(f"[挖地] 🧪 收场小判 {after}，这趟变化 {delta:+d}"
                          f"（挖了 {floors_done} 层{per}）")
                    if hasattr(self, "record_event"):
                        self.record_event("osaka.koban_session",
                                          before=before, after=after,
                                          delta=delta, floors=floors_done,
                                          target_floor=target_floor)
                else:
                    print(f"[挖地] 🧪 小判记账不全（前{before}/后{after}），"
                          "这趟掉落率没记上")

    def _osaka_stream_impl(self, max_floors: int = 1, team_no: int = 3,
                     select_floor: bool = False, target_floor: int = 81,
                     formation_mode: str = "manual",
                     formation: str = "鱼鳞阵",
                     repair_threshold: str = "light",
                     injury_action: str = "continue",
                     auto_equip: bool = True,
                     _target_floors: int = None,
                     _completed_floors: int = 0,
                     _repair_count: int = 0,
                     _speedups_used: int = 0,
                     _team_record_saved: bool = False,
                     _auto_equip_active: bool = None):
        if _target_floors is None:
            _target_floors = max_floors
        if _auto_equip_active is None:
            # 刀装恢复与伤势处理互不绑定；伤势仍在出阵前优先按
            # injury_action 处理，刀装警告出现时则只看 auto_equip。
            _auto_equip_active = bool(auto_equip)
        cfg = self.config.get("osaka", {})
        if not cfg:
            yield "[挖地] 未配置大阪城"
            return
        teams = self.config.get("team_select", {}).get("teams", {})
        if str(team_no) not in teams:
            yield f"[挖地] 配置里没有部队{team_no}的坐标"
            return

        # 任务启动时也可能正好停在更新框（例如上一次任务已被更新中断）。
        self.maa.screenshot(force=True)
        startup_update = yield from self.recover_game_update_stream()
        if startup_update is None:
            return
        if startup_update:
            yield "[挖地] 更新恢复完成，从活动入口重新开始"

        yield "[挖地] 正在从目录进入出阵 → 活动 → 大阪城..."
        for msg in self.navigate_to_stream("出阵"):
            yield msg
        if self.current_location != "出阵":
            yield "[挖地] 到达出阵失败"
            return

        if not self._open_osaka(cfg):
            yield "[挖地] 没找到大阪城活动入口"
            return
        if select_floor:
            target_floor = max(1, min(99, int(target_floor)))
            current_floor = self._read_osaka_floor(cfg)
            if current_floor is None:
                yield "[挖地] 没读到活动页左上角的当前层数，未点击层数箭头"
                return
            yield f"[挖地] 当前选择第 {current_floor} 层，准备切换到第 {target_floor} 层"
            selected_floor = self._select_osaka_floor(cfg, target_floor)
            if selected_floor != target_floor:
                shown = "无法识别" if selected_floor is None else f"第 {selected_floor} 层"
                yield f"[挖地] 层数没有成功切到目标（现在是{shown}），已停止出阵"
                return
            yield f"[挖地] 已确认指定第 {target_floor} 层"
        if not self._wait_for_team_select(cfg, attempts=15, open_after=5):
            yield "[挖地] 部队选择界面没打开"
            return

        ok, _team_record_saved = yield from self._safe_depart_stream(
            cfg, team_no, "[挖地]",
            repair_threshold=repair_threshold,
            auto_equip=_auto_equip_active,
            team_record_saved=_team_record_saved)
        if not ok:
            return

        # 大阪城没有手形，也没有自动行军；即刻出阵后只有普通二次确认。
        if not self._confirm_departure(cfg):
            yield "[挖地] 没看到出阵二次确认，停止点击"
            return
        yield f"[挖地] 部队{team_no}出发，狐之助的废话交给安全区慢慢跳过"

        floors = 0
        idle_checks = 0
        drop_credit = None  # 掉落认人去重：获得画面等戳、连帧都在，刚出现才记一次
        floor_credited = False  # 层末结算页去重：同一画面只记一圈，离场后才放行
        floor_end_stuck = 0     # 结算页赖着不走的计数，超时才算真卡死
        while idle_checks < 300:
            self.maa.screenshot(force=True)

            # 伤势章/OCR 理论上会在点击行军前拦住重伤；这里再兜游戏自己
            # 弹出的“重伤仍要行军吗”。永远点否，然后返回本丸。
            if self._deny_heavy_injury_warning(cfg):
                yield "[挖地] 🛑 出现重伤行军警告，已点【否】，不再前进"
                self.maa.screenshot(force=True)
                if self._return_home_from_march(cfg):
                    self.current_location = "本丸"
                    yield "[挖地] ✓ 已从重伤警告处安全返回本丸"
                else:
                    yield "[挖地] 点否后没能确认返回本丸，已停止点击"
                return

            # 强制更新会把游戏从战斗中踢回登录页。恢复后旧战斗状态已经作废，
            # 必须按已结算层数从活动入口重开，不能接着在原坐标上盲点。
            update_recovered = yield from self.recover_game_update_stream()
            if update_recovered is None:
                return
            if update_recovered:
                total_completed = _completed_floors + floors
                remaining = _target_floors - total_completed
                yield (f"[挖地] 更新完成，原战斗已被重置；保留进度 "
                       f"{total_completed}/{_target_floors}，重新进场继续剩余 {remaining} 层")
                yield from self.osaka_stream(
                    max_floors=remaining,
                    team_no=team_no,
                    select_floor=select_floor,
                    target_floor=target_floor,
                    formation_mode=formation_mode,
                    formation=formation,
                    repair_threshold=repair_threshold,
                    injury_action=injury_action,
                    auto_equip=auto_equip,
                    _target_floors=_target_floors,
                    _completed_floors=total_completed,
                    _repair_count=_repair_count,
                    _speedups_used=_speedups_used,
                    _team_record_saved=_team_record_saved,
                    _auto_equip_active=_auto_equip_active,
                )
                return

            # 网络超时弹窗（MuMu 断网）。续打成功画面会回到断点（比如阵形选择），
            # 下面的巡逻判定会自然接上；落在本丸则跟更新恢复一样从入口重开。
            net_recovered = yield from self.recover_network_stream()
            if net_recovered is None:
                return
            if net_recovered == "home":
                total_completed = _completed_floors + floors
                remaining = _target_floors - total_completed
                yield (f"[挖地] 断网恢复后落在本丸，原战斗已被重置；保留进度 "
                       f"{total_completed}/{_target_floors}，重新进场继续剩余 {remaining} 层")
                yield from self.osaka_stream(
                    max_floors=remaining,
                    team_no=team_no,
                    select_floor=select_floor,
                    target_floor=target_floor,
                    formation_mode=formation_mode,
                    formation=formation,
                    repair_threshold=repair_threshold,
                    injury_action=injury_action,
                    auto_equip=auto_equip,
                    _target_floors=_target_floors,
                    _completed_floors=total_completed,
                    _repair_count=_repair_count,
                    _speedups_used=_speedups_used,
                    _team_record_saved=_team_record_saved,
                    _auto_equip_active=_auto_equip_active,
                )
                return
            if net_recovered == "resumed":
                idle_checks = 0
                yield "[挖地] 断网续打成功，接着巡逻"
                continue

            # 道中和层末都有“部队恢复 / 返回本丸 / 行军”，三者全部是常驻操作，
            # 不能参与层末判断。只认结算页专有的“当前层数 + 传送凭证”。
            floor_done_now = self._osaka_floor_done(cfg)
            if not floor_done_now:
                floor_credited = False
            if floor_done_now and floor_credited:
                # 同一结算页还没走（转场慢/上一击没生效）：不重复记圈，
                # 只补点行军等它离场；赖着 ~30 秒不动才算真卡死。
                # ——2026-08-26 实测：转场慢半拍时同一页被记成两圈，还把整单吓停
                floor_end_stuck += 1
                if floor_end_stuck >= 30:
                    yield ("[挖地] ⚠️ 层末结算页 30 秒没动静，"
                           "停止点击，请查看卡在哪个画面")
                    return
                march = self._find_osaka_march(cfg)
                if march:
                    self.maa.click(march)
                    time.sleep(1.2)
                else:
                    time.sleep(1.0)
                continue
            if floor_done_now:
                floor_credited = True
                floor_end_stuck = 0
                idle_checks = 0
                floors += 1
                total_completed = _completed_floors + floors
                self._osaka_floors_total = total_completed  # 喂给外套的小判实验
                if hasattr(self, "record_event"):
                    self.record_event(
                        "osaka.floor_completed",
                        completed=total_completed,
                        target=_target_floors,
                        selected_floor=target_floor if select_floor else None,
                    )
                yield f"[挖地] ✓ 已完成 {total_completed}/{_target_floors} 层"
                injury = self._team_injury_status(cfg)
                goal_reached = total_completed >= _target_floors
                injury_reached = bool(
                    injury and self._injury_reaches_threshold(
                        injury, repair_threshold))
                # 远征排班在等画面：层末是挖地唯一的安全收工点（行军决策点），
                # 绝不挖到一半响应；和「目标达成」走同一条回本丸收尾的路。
                takeover = self._expedition_takeover_requested()
                if injury and not injury_reached:
                    yield f"[挖地] 部队出现{injury}，尚未达到停止条件，继续向下挖"
                if goal_reached or injury_reached or takeover:
                    reasons = []
                    if goal_reached:
                        reasons.append("目标层数已完成")
                    if injury_reached:
                        reasons.append(f"部队出现{injury}")
                    if takeover:
                        reasons.append("远征排班请求接管")
                    yield f"[挖地] {'，'.join(reasons)}，准备返回本丸收尾"
                    returned = self._return_home_from_march(cfg)
                    if returned:
                        self.current_location = "本丸"
                        yield "[挖地] ✓ 已安全返回本丸"
                    else:
                        yield "[挖地] 没能确认返回本丸；已停止点击，请手动查看"
                        return
                    if goal_reached:
                        yield (f"[挖地] 目标层数完成，收工；期间手入 {_repair_count} 次，"
                               f"累计使用加速符 {_speedups_used} 个")
                        return
                    if takeover:
                        if not injury_reached:
                            self._expedition_takeover_remaining = _target_floors - total_completed
                        yield "[挖地] 🚩 远征排班请求接管：不开新层，安全收工"
                        return

                    action = str(injury_action or "continue")
                    if action == "stop":
                        yield "[挖地] 按设置只返回本丸，不进行手入，收工"
                        return
                    repair_and_stop = action == "repair_stop"
                    yield ("[挖地] 开始手入，完成后收工" if repair_and_stop
                           else "[挖地] 开始手入并加速当前部队，之后继续剩余层数")
                    for repair_msg in self.repair_stream(
                            dry_run=False,
                            use_speedup=False if repair_and_stop else None,
                            speedup_teams=None if repair_and_stop else [team_no]):
                        yield repair_msg
                    stats = getattr(self, "last_repair_stats", {})
                    repaired = int(stats.get("repaired", 0))
                    speedups = int(stats.get("speedups", 0))
                    _repair_count += 1
                    _speedups_used += speedups
                    if hasattr(self, "record_event"):
                        self.record_event("repair.session_completed", source="osaka",
                                          repaired=repaired, speedups=speedups,
                                          session_count=_repair_count)
                    yield (f"[挖地] 🩹 第 {_repair_count} 次手入：修复 {repaired} 把，"
                           f"使用加速符 {speedups} 个；累计使用 {_speedups_used} 个")
                    if repair_and_stop:
                        yield "[挖地] 手入已安排，收工"
                        return
                    remaining = _target_floors - total_completed
                    yield (f"[挖地] 手入结束，当前总进度 {total_completed}/{_target_floors}，"
                           f"继续剩余 {remaining} 层")
                    yield from self.osaka_stream(
                        max_floors=remaining,
                        team_no=team_no,
                        select_floor=select_floor,
                        target_floor=target_floor,
                        formation_mode=formation_mode,
                            formation=formation,
                        repair_threshold=repair_threshold,
                        injury_action=injury_action,
                        auto_equip=auto_equip,
                        _target_floors=_target_floors,
                        _completed_floors=total_completed,
                        _repair_count=_repair_count,
                        _speedups_used=_speedups_used,
                        _team_record_saved=_team_record_saved,
                        _auto_equip_active=_auto_equip_active,
                    )
                    return
                march = self._wait_for_osaka_march(cfg)
                if not march:
                    # 多半是画面已抢先进下一场（选阵形/过场），回巡逻位让主循环
                    # 接手；真卡死有 idle_checks 和结算页卡死计数兜底，不再像
                    # 2026-08-26 那样整单收工、把游戏晾在选阵形画面一整夜。
                    yield ("[挖地] 层末画面先跑了（多半进了下一场），"
                           "回巡逻位继续观察（不中止本次任务）")
                    continue
                self.maa.click(march)
                time.sleep(1.2)
                continue

            if self._formation_mode_state(
                    allow_auto_without_title=formation_mode != "auto") is not None:
                idle_checks = 0
                result = self.choose_formation(
                    formation_name=formation,
                    enable_auto=formation_mode == "auto",
                )
                if result == "auto":
                    yield "[挖地] 已开启游戏自动阵形"
                elif result == "failed":
                    yield "[挖地] ⚠️ 阵形没选成，下轮巡逻再试"
                else:
                    chosen = "有利阵形" if result == "advantage" else formation
                    yield f"[挖地] 已选择「{chosen}」"
                # 阵形确认后的转场略慢；等页面真正消失，避免下一轮重复选阵
                # （出阵循环已实测过这个坑；挖地 8-23 整夜每场战斗选两遍阵）。
                for _ in range(8):
                    time.sleep(0.4)
                    self.maa.screenshot(force=True)
                    if self._formation_mode_state(
                            allow_auto_without_title=formation_mode != "auto") is None:
                        break
                continue

            march = self._find_osaka_march(cfg)
            if march:
                idle_checks = 0
                # 大阪城没有自动行军：每次战斗结果页出现“行军”时都先查伤势。
                # 达到停止条件就绝不点击行军，避免拖到整层结束才发现伤员。
                field_injury = self._team_injury_status(cfg)
                if field_injury and self._injury_reaches_threshold(
                        field_injury, repair_threshold):
                    yield f"[挖地] 道中检测到{field_injury}，不再继续行军"
                    if not self._return_home_from_march(cfg):
                        yield "[挖地] 没能确认返回本丸；已停止点击，请手动查看"
                        return
                    self.current_location = "本丸"
                    yield "[挖地] ✓ 已安全返回本丸"
                    action = str(injury_action or "continue")
                    if action == "stop":
                        yield "[挖地] 按设置不进行手入，收工"
                        return
                    repair_and_stop = action == "repair_stop"
                    yield ("[挖地] 开始手入，完成后收工" if repair_and_stop
                           else "[挖地] 开始手入并加速当前部队，之后继续剩余层数")
                    for repair_msg in self.repair_stream(
                            dry_run=False,
                            use_speedup=False if repair_and_stop else None,
                            speedup_teams=None if repair_and_stop else [team_no]):
                        yield repair_msg
                    stats = getattr(self, "last_repair_stats", {})
                    repaired = int(stats.get("repaired", 0))
                    speedups = int(stats.get("speedups", 0))
                    _repair_count += 1
                    _speedups_used += speedups
                    if hasattr(self, "record_event"):
                        self.record_event("repair.session_completed", source="osaka",
                                          repaired=repaired, speedups=speedups,
                                          session_count=_repair_count)
                    yield (f"[挖地] 🩹 第 {_repair_count} 次手入：修复 {repaired} 把，"
                           f"使用加速符 {speedups} 个；累计使用 {_speedups_used} 个")
                    if repair_and_stop:
                        yield "[挖地] 手入已安排，收工"
                        return
                    total_completed = _completed_floors + floors
                    remaining = _target_floors - total_completed
                    yield (f"[挖地] 手入结束，当前总进度 {total_completed}/{_target_floors}，"
                           f"继续剩余 {remaining} 层")
                    yield from self.osaka_stream(
                        max_floors=remaining,
                        team_no=team_no,
                        select_floor=select_floor,
                        target_floor=target_floor,
                        formation_mode=formation_mode,
                            formation=formation,
                        repair_threshold=repair_threshold,
                        injury_action=injury_action,
                        auto_equip=auto_equip,
                        _target_floors=_target_floors,
                        _completed_floors=total_completed,
                        _repair_count=_repair_count,
                        _speedups_used=_speedups_used,
                        _team_record_saved=_team_record_saved,
                        _auto_equip_active=_auto_equip_active,
                    )
                    return
                yield "[挖地] 行军，继续向下挖"
                self.maa.click(march)
                time.sleep(1.0)
                continue

            # 掉落获得画面：左下对话框名牌认人（挖地没有自动行军，
            # 获得画面一定会等戳，跟合战场共用 _read_drop_sword）
            drop_result = self._read_drop_sword()
            if drop_result["status"] == "recognized":
                dropped = drop_result["sword"]
                if drop_credit != dropped["sword_id"]:
                    drop_credit = dropped["sword_id"]
                    yield f"[挖地] 🎉 刀剑男士【{dropped['name']}】来本丸了！"
                    if hasattr(self, "record_event"):
                        # select_floor/target_floor 可能是布尔（开关语义），
                        # 只有真数字层号才值得记账（bool 是 int 子类，得先排掉）
                        floor = next((f for f in (select_floor, target_floor)
                                      if isinstance(f, int) and not isinstance(f, bool)),
                                     None)
                        self.record_event(
                            "sword.obtained", **dropped, source="osaka.drop",
                            floor=floor)
            elif drop_result["status"] == "unrecognized":
                # 明确看见掉刀证据但名字没认出：留一条可查询的结构化事实，
                # 绝不静默消失、更不许算成"没掉"（挖地没有圈事件可承载
                # drop_observation，只能靠这条事件本身）
                if drop_credit != "unrecognized":
                    drop_credit = "unrecognized"
                    if hasattr(self, "record_event"):
                        floor = next((f for f in (select_floor, target_floor)
                                      if isinstance(f, int) and not isinstance(f, bool)),
                                     None)
                        self.record_event("sword.drop_unrecognized",
                                          source="osaka.drop", floor=floor)
            else:
                drop_credit = None

            # 狐之助对话和战斗过场都用右下安全区驱散；没有目标时绝不盲点按钮区。
            self._click_point(cfg.get("skip_tap", [775, 695]))
            self.quick_peek(tag="osaka")  # 顺路拍顶栏家底，零导航（60s 节流）
            idle_checks += 1
            time.sleep(0.8)

        yield (f"[挖地] 连续 {idle_checks} 次没有识别到阵形、行军或层末"
               f"（总进度 {_completed_floors + floors}/{_target_floors}），"
               f"停止点击，请查看卡在哪个画面")

    def _open_osaka(self, cfg: dict) -> bool:
        activity = cfg.get("activity_entry", {})
        entry = cfg.get("event_entry", {})
        # 活动页有加载动画，模板可能早拍了半拍（2026-08-24 验证跑遇到一次
        # 点开活动页瞬间模板未命中直接放弃），每步给几拍重试
        for step in (activity, entry):
            template = step.get("template")
            if not template:
                return False
            target = None
            for _ in range(4):
                target = self.maa.template_match(template)
                if target:
                    break
                time.sleep(1.0)
                self.maa.screenshot(force=True)
            if not target:
                return False
            self.maa.click(target)
            time.sleep(1.5)
            self.maa.screenshot(force=True)
        return True

    def _read_osaka_floor(self, cfg: dict):
        """读取活动页标题中的层数；不用滚轮数字，避免两个 OCR 结果错序。"""
        title = cfg.get("floor_title_ocr", {})
        roi = roi_4to4(*title.get("roi", [75, 155, 555, 225]))
        tokens = self.maa.ocr_all(roi)
        text = "".join(str(token[0]) for token in tokens)
        text = re.sub(r"\s+", "", text)
        match = re.search(r"大阪城地下(\d{1,2})层", text)
        if not match:
            # OCR 偶尔会漏掉固定标题，但数字两侧仍有“地下/层”可作护栏。
            match = re.search(r"地下(\d{1,2})层", text)
        if not match:
            return None
        floor = int(match.group(1))
        return floor if 1 <= floor <= 99 else None

    def _select_osaka_floor(self, cfg: dict, target_floor: int):
        """逐位调节层数；每次点击后复读标题，灰色箭头不会被连续盲点。"""
        arrows = cfg.get("floor_arrows", {})
        points = {
            "tens_up": arrows.get("tens_up", [1014, 284]),
            "ones_up": arrows.get("ones_up", [1084, 284]),
            "tens_down": arrows.get("tens_down", [1014, 420]),
            "ones_down": arrows.get("ones_down", [1084, 420]),
        }
        current = self._read_osaka_floor(cfg)
        for _ in range(20):
            if current is None or current == target_floor:
                return current
            current_tens, current_ones = divmod(current, 10)
            target_tens, target_ones = divmod(target_floor, 10)
            if current_tens != target_tens:
                key = "tens_up" if target_tens > current_tens else "tens_down"
            else:
                key = "ones_up" if target_ones > current_ones else "ones_down"
            self._click_point(points[key])
            time.sleep(0.45)
            self.maa.screenshot(force=True)
            updated = self._read_osaka_floor(cfg)
            if updated is None or updated == current:
                # 灰色（不可用）箭头的表现就是标题层数没有变化。
                return updated if updated is not None else current
            current = updated
        return current

    def _osaka_floor_done(self, cfg: dict) -> bool:
        """只用层末专有文字判断；故意不查看三个道中常驻按钮。"""
        marker = cfg.get("floor_end_ocr", {})
        roi = roi_4to4(*marker.get("roi", [825, 270, 1280, 355]))
        if not self.maa.ocr(marker.get("expected", "当前层数"), roi):
            return False
        witness = cfg.get("floor_end_witness_ocr", {})
        witness_roi = roi_4to4(*witness.get("roi", [825, 85, 1280, 285]))
        return bool(self.maa.ocr(
            witness.get("expected", "传送凭证"), witness_roi))

    def _find_osaka_march(self, cfg: dict):
        march = cfg.get("march_button", {})
        roi = roi_4to4(*march.get("roi", [1030, 500, 1280, 720]))
        return self.maa.template_match(
            march.get("template", "battle/行军.png"), roi)

    def _wait_for_osaka_march(self, cfg: dict, attempts: int = 8):
        """层末文字通常先于按钮出现；等按钮动画落稳，不因单帧抢跑停机。"""
        for _ in range(attempts):
            self.maa.screenshot(force=True)
            march = self._find_osaka_march(cfg)
            if march:
                return march
            time.sleep(0.5)
        return None
