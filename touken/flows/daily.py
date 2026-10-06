# -*- coding: utf-8 -*-
"""
上层业务：一键日课——把每天的活儿按顺序串起来

流程（用户排的）：
  ① 登录（含登录弹窗扫地：特别登录礼物/公告 全关，不然导航全被挡）
  ② 签到（公告里的每日奖励，幂等保险）
  ③ 万屋免费鸡蛋（暖心礼包）
  ④ 演练（认人避战，赢够收工）
  ⑤ 远征（收菜 + 顺手再派）
  ⑥ 内番（安排上工，24小时的活儿早点派）
  ⑦ 锻刀（每日3炉，收完成的+点空闲的，刀位满了去刀解腾位置）
  ⑧ 刀解（白名单一把，任务奖励加速符）
  ⑨ 合成（白名单喂一把）
  ⑩ 炼糖（收件箱清狗粮 + 习合循环）
  ⑪ 出阵（配置驱动：活动=raid / 普通图=sortie / 不打=none）
  ⑫ 领任务奖励
  ⑬ 下线（还没做，可选）

设计原则：每一步独立 try + 消息关键字判成败，翻车不拖死后面，最后给真实成绩单。

冷启动血泪教训：
  login() 各步是在加载画面空点的（游戏还没到本丸），登录礼物/公告弹窗
  是之后才不紧不慢蹦出来的——所以登录后必须 _popup_sweep() 边等本丸边关弹窗。
  特别登录礼物的"今日不再弹出"和模板长得不一样，但 X 就是 通用_关闭.png。
"""

import os
import time

from ..runtime_paths import STATUS_DIR

from ..maa_adapter import Point, roi_4to4
from ..record_sync import collect_game_records
from . import naihanka_report
# 判分词表抽到 report_judge 共用（工作流节点判分复用同一份，行为不变）；
# 这里 re-export，老 import 路径（tests 等）不受影响。
from .report_judge import (  # noqa: F401
    _FAIL_RE,
    _PASS_RE,
    _equip_warning_status,
    _is_fail,
    _is_success_status,
    _practice_report_status,
    _shop_report_status,
    _snapshot_report_status,
)


class DailyMixin:
    """一键日课。依赖宿主类已注册的各流程 Mixin。"""

    def daily_stream(self, logout: bool = False, only=None, after: str = None,
                     sortie_override: dict = None, practice_override: dict = None,
                     expedition_override: list = None, forge_times: int = None,
                     forge_recipe: list = None):
        """
        流式一键日课

        Args:
            logout: 最后是否下线（老参数，等价于 after="logout"）
            only: 只跑指定步骤（名字列表，如 ["签到","演练","出阵"]），
                  None = 全跑。面板勾选功能用的就是这个。
            after: 跑完干啥（可选，默认啥也不干——被黑屏吓过，必须手动选）：
                  "none"     啥也不干
                  "logout"   退出游戏
                  "shutdown" 退出游戏 + 关模拟器
                  "sleep"    退出游戏 + 关模拟器 + 电脑休眠
            sortie_override: 覆盖出阵安排（面板传的），如
                  {"mode":"none"} / {"mode":"raid","rounds":3} /
                  {"mode":"sortie","chapter":1,"map_no":1,"loops":2,"team_no":3}
            forge_times: 覆盖日课锻刀次数（面板传的）；None 时读配置
                  daily.forge_times，再缺省为 3
            forge_recipe: 覆盖日课锻刀配方；None 时读配置 forge.recipe

        Yields:
            str: 执行状态消息
        """
        if after is None:
            after = "logout" if logout else "none"
        plan = self.config.get("daily", {})
        if sortie_override is not None:
            plan = dict(plan)
            plan["sortie"] = sortie_override
        if practice_override:
            plan = dict(plan)
            plan["practice"] = dict(practice_override)
        if forge_times is not None:
            plan = dict(plan)
            plan["forge_times"] = int(forge_times)
        report = []
        wanted = set(only) if only else None

        def _w(name):
            return wanted is None or name in wanted

        # 断点续跑时往往不会勾「登录」。因此不能把更新检查寄托在登录步骤里；
        # 先于任何日课导航验一次，之后每个步骤开始前再验，避免把弹窗背后
        # 露出来的「目录」误当成可操作的本丸。
        update_state = yield from self._daily_update_gate()
        if update_state is None:
            yield "[日课] 游戏更新没有恢复完成，本次日课停止"
            return

        # ========== ① 登录 + 弹窗扫地 ==========
        if _w("登录"):
            yield "========== ① 登录 =========="
            try:
                started = yield from self._ensure_game_started()
                if not started:
                    report.append(("登录", "✗ 游戏没有启动"))
                    yield "[日课] 没有确认游戏成功启动，本次日课停止"
                    self._flush_report(report, finished=True)
                    return
                self.login()
                if self._popup_sweep():
                    report.append(("登录", "✓"))
                else:
                    report.append(("登录", "✗ 没到本丸"))
                    yield "[日课] 登录后仍没到本丸，本次日课停止"
                    self._flush_report(report, finished=True)
                    return
            except Exception as exc:
                report.append(("登录", f"✗ {exc}"))
                yield f"[日课] 登录翻车: {exc}；未确认进入本丸，本次日课停止"
                self._flush_report(report, finished=True)
                return
            self._flush_report(report, finished=False)
            time.sleep(1.0)

        # ========== ②~⑧ 各步 ==========
        # （开工/收工的例行盘点已砍掉：完整快照融进锻刀收工顺手拍，
        #   顶栏五资源靠各循环的 quick_peek 顺路更新，不再专程跑腿）
        steps = [
            ("签到", lambda: self.signin_stream()),
            ("万屋", lambda: self.claim_free_gift_stream()),
            ("演练", lambda: self._daily_practice_step(
                plan.get("practice", {}))),
            ("远征", lambda: self._daily_expedition_step(
                expedition_override,
                fallback_redispatch=plan.get("expedition_redispatch", "same"))),
            ("内番", lambda: self.naihanka_stream()),
            ("锻刀", lambda: self.forge_stream(
                times=plan.get("forge_times", 3), recipe=forge_recipe)),
            ("刀解", lambda: self._dismantle_step()),
            ("合成", lambda: self.synthesize_stream()),
            ("任务奖励", lambda: self.claim_task_rewards_stream()),
            ("库存快照", lambda: self._closing_snapshot_stream(_w("锻刀"))),
        ]
        titles = {
            "签到": "② 签到",
            "万屋": "③ 万屋免费鸡蛋",
            "演练": "④ 演练",
            "远征": "⑤ 远征",
            "内番": "⑥ 内番",
            "锻刀": "⑦ 锻刀",
            "刀解": "⑧ 刀解",
            "合成": "⑨ 合成",
            "任务奖励": "⑪ 领任务奖励",
            "库存快照": "⑫ 库存快照（看板数据）",
        }

        # 出阵插到任务奖励前面（就算没勾任务奖励，单勾出阵也能跑）
        seq = []
        for name, fn in steps:
            if name == "任务奖励":
                seq.append(("出阵", None))
            seq.append((name, fn))

        for name, fn in seq:
            if not _w(name):
                continue
            update_state = yield from self._daily_update_gate()
            if update_state is None:
                report.append((name, "✗ 游戏更新未完成"))
                yield f"[日课] 更新恢复失败，未开始{name}；后续步骤停止"
                break
            if name == "出阵":
                yield "========== ⑩ 出阵 =========="
                self.set_progress("daily:出阵")
                for msg in self._sortie_step(plan, report):
                    yield msg
                time.sleep(1.0)
                continue

            yield f"========== {titles[name]} =========="
            self.set_progress("daily:" + name)
            ok = True
            detail_status = None
            try:
                for msg in fn():
                    yield msg
                    if _is_fail(msg):
                        ok = False
                    if name == "万屋":
                        detail_status = _shop_report_status(msg, detail_status)
                    if name == "演练":
                        detail_status = _practice_report_status(msg, detail_status)
                    if name == "库存快照":
                        detail_status = _snapshot_report_status(msg, detail_status)
            except Exception as exc:
                ok = False
                yield f"[日课] {name}翻车: {exc}"
            report.append((name, detail_status or ("✓" if ok else "✗")))
            self._flush_report(report, finished=False)
            time.sleep(1.0)

        # ========== ⑬ 下线 ==========
        if after in ("logout", "shutdown", "sleep"):
            yield "========== ⑬ 下线 =========="
            try:
                for msg in self.logout_stream(
                        kill_game=True, close_emulator=False, sleep_pc=False):
                    yield msg
            except Exception as exc:
                yield f"[日课] 下线翻车: {exc}"

        # ========== 成绩单 ==========
        yield "========== 日课成绩单 =========="
        for name, status in report:
            yield f"  {name}: {status}"
        fails = [n for n, s in report if not _is_success_status(s)]
        yield "[日课] 全部跑完" + (f"，但有翻车项: {'、'.join(fails)}" if fails else "，全绿")

        # ========== 落盘最终成绩单 + 手机推送 ==========
        payload = self._flush_report(report, finished=True)
        if payload is None:
            yield "[日课] 成绩单落盘失败（不影响跑）"
        try:
            from ..notify import notify_daily_report, notify_destination
            destination = notify_destination()
            if payload and notify_daily_report(payload):
                yield (f"[日课] 成绩单已发送到 ntfy 频道「{destination}」；"
                       "手机订阅该频道后才能收到")
            elif not destination:
                yield "[日课] 未配置 ntfy 频道，成绩单只保存在本机"
            else:
                yield "[日课] ntfy 频道发送失败（网络或服务问题），成绩单已保存在本机"
        except Exception as exc:
            yield f"[日课] 手机推送翻车（不影响跑）: {exc}"

        # ========== ⑭ 收尾（可选项，推完成绩单才干，休眠放最后）==========
        if after in ("shutdown", "sleep"):
            yield "[日课] 关模拟器..."
            try:
                from ..emulator import shutdown_emulator
                mgr = self.config.get("emulator_manager")
                inst = int(self.config.get("emulator_instance", 0))
                if mgr and shutdown_emulator(mgr, inst):
                    yield "[日课] ✓ 模拟器已关闭，辛苦了"
                else:
                    yield "[日课] ⚠️ 模拟器没关成（没配管家路径？），你手动关一下"
            except Exception as exc:
                yield f"[日课] 关模拟器翻车（不影响成绩单）: {exc}"
        if after == "sleep":
            yield "[日课] 😴 成绩单已推送，电脑 10 秒后休眠，晚安"
            time.sleep(10)
            try:
                from ..emulator import sleep_computer
                sleep_computer()
            except Exception as exc:
                yield f"[日课] 休眠翻车: {exc}"

    def _daily_update_gate(self):
        """日课导航前的更新门卫；兼容只勾中间步骤的断点续跑。"""
        self.maa.screenshot(force=True)
        recovered = yield from self.recover_game_update_stream()
        if recovered is None:
            return None
        if recovered:
            yield "[日课] 游戏更新完成，先清理登录弹窗再继续日课"
            if not self._popup_sweep():
                yield "[日课] 更新后没能确认本丸已可操作"
                return None
            yield "[日课] ✓ 本丸已恢复，可以继续当前步骤"
        return recovered

    def _flush_report(self, report, finished: bool):
        """
        成绩单落盘。每跑完一步就写一次（finished=False），防超时被杀丢数据；
        全部跑完再写终版（finished=True）。看板 Widget 吃的就是这个文件。
        """
        try:
            import json as _json
            from pathlib import Path
            status_dir = STATUS_DIR
            status_dir.mkdir(exist_ok=True)
            fails = [n for n, s in report if not _is_success_status(s)]
            payload = {
                "run_id": os.environ.get("MAAMARU_RUN_ID") or None,
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished": finished,
                "all_green": finished and not fails,
                "steps": [{"name": n, "status": s} for n, s in report],
            }
            (status_dir / "latest_report.json").write_text(
                _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return payload
        except Exception:
            return None

    # ========== 出阵步 ==========

    def _dismantle_step(self):
        """日课的刀解步：今天已经解过（比如锻刀收刀腾位置顺手解的）就跳过"""
        from .smith import dismantled_today
        if dismantled_today():
            yield "[日课] ✓ 今天已经刀解过了（锻刀收刀腾位置时顺手解的），这步跳过"
            return
        yield from self.dismantle_stream(max_dismantle=1)

    def _daily_expedition_step(self, routes, fallback_redispatch="same"):
        """收菜后按独立“远征”的常用安排补派；不读取自动排班。"""
        if routes is None:
            # 非面板调用保持旧配置兼容。
            yield from self.collect_expedition_stream(
                redispatch=fallback_redispatch)
            return

        yield from self.collect_expedition_stream(redispatch=None)
        from ..expedition_sakura import recover_stream
        if not (yield from recover_stream(self)):
            return
        if not routes:
            yield "[远征] 没有启用常用安排，本次只收取归来奖励"
            return

        from .expedition import _load_exp_record
        records = _load_exp_record()
        for route in routes:
            team = int(route["team_no"])
            record = records.get(str(team), {})
            try:
                started = time.mktime(time.strptime(
                    record["dispatched_at"], "%Y-%m-%d %H:%M:%S"))
                remain = max(0, int(
                    started + int(record["duration_min"]) * 60 - time.time()))
            except (KeyError, TypeError, ValueError):
                remain = 0
            if remain > 0:
                yield (f"[远征] 部队{team}仍在外面（约剩 {remain // 60} 分钟），"
                       "按常用安排跳过")
                continue
            if not route.get("era") or not route.get("map_slot"):
                yield f"[远征] 常用安排地图 {route.get('map_code')} 不存在，无法派遣"
                continue
            formation_id = str(route.get("formation_id") or "")
            if formation_id:
                from ..custom_formations import apply_formation_preset_by_id_stream
                applied = yield from apply_formation_preset_by_id_stream(
                    self, formation_id, expected_team=team)
                if not applied:
                    yield f"[远征] ✗ 部队{team}的预设没套好，本次不派这队"
                    continue
            yield (f"[远征] 按常用安排派部队{team}去 {route['map_code']}"
                   f"「{route.get('map_name') or ''}」")
            yield from self.expedition_stream(
                era=int(route["era"]), map_slot=int(route["map_slot"]),
                team_no=team,
                **({'sakura_before_dispatch': True,
                    'repair_threshold': route.get('repair_threshold', 'light')}
                   if route.get('sakura_before_dispatch') else {}))

    def _apply_daily_preset(self, plan, step_label):
        error = str(plan.get("formation_error") or "")
        if error:
            yield f"[日课] ✗ {step_label}选择的部队预设不可用：{error}；没有动游戏"
            return False
        formation_id = str(plan.get("formation_id") or "")
        if not formation_id:
            return True
        from ..custom_formations import apply_formation_preset_by_id_stream
        applied = yield from apply_formation_preset_by_id_stream(
            self, formation_id, expected_team=plan.get("team_no"))
        if not applied:
            yield f"[日课] ✗ {step_label}的部队预设没套好，本次{step_label}不开始"
            return False
        return True

    def _daily_practice_step(self, practice_plan):
        if not (yield from self._apply_daily_preset(practice_plan, "演练")):
            return
        yield from self.practice_stream(
            dry_run=False,
            team_no=practice_plan.get("team_no"),
            formation_mode=practice_plan.get("formation_mode"),
            formation=practice_plan.get("formation"))

    def _sortie_step(self, plan, report):
        sortie_plan = plan.get("sortie", {"mode": "none"})
        mode = sortie_plan.get("mode", "none")
        try:
            if mode != "none" and not (yield from self._apply_daily_preset(
                    sortie_plan, "出阵")):
                report.append(("出阵", "✗ 部队预设未套用"))
                return
            if mode == "raid":
                ok = True
                equip_status = None
                for msg in self.raid_stream(
                        max_rounds=sortie_plan.get("rounds", 1),
                        team_no=sortie_plan.get("team_no"),
                        auto_buy_ticket=sortie_plan.get("auto_buy_ticket", False),
                        max_buys=sortie_plan.get("max_buys")):
                    yield msg
                    if _is_fail(msg):
                        ok = False
                    equip_status = _equip_warning_status(msg, equip_status)
                status = equip_status or ("✓" if ok else "✗")
                report.append(("出阵(活动)", status))
            elif mode == "sortie":
                ok = True
                equip_status = None
                for msg in self.sortie_stream(
                        chapter=sortie_plan["chapter"],
                        map_no=sortie_plan["map_no"],
                        team_no=sortie_plan.get("team_no", 3),
                        max_loops=sortie_plan.get("loops", 1),
                        auto_march=sortie_plan.get("auto_march", True),
                        stop_on_fatigue=sortie_plan.get("stop_on_fatigue", True),
                        formation_mode=sortie_plan.get("formation_mode", "manual"),
                        formation=sortie_plan.get("formation", "鱼鳞阵"),
                        repair_threshold=sortie_plan.get("repair_threshold", "light"),
                        injury_action=sortie_plan.get("repair_on_injury", "continue"),
                        auto_equip=sortie_plan.get("auto_equip", True),
                        retreat_before_boss=sortie_plan.get(
                            "retreat_before_boss", False),
                        rotate_captain=sortie_plan.get("rotate_captain", False),
                        rotate_captain_margin=sortie_plan.get(
                            "rotate_captain_margin", 10)):
                    yield msg
                    if _is_fail(msg):
                        ok = False
                    equip_status = _equip_warning_status(msg, equip_status)
                status = equip_status or ("✓" if ok else "✗")
                report.append(("出阵(推图)", status))
            elif mode == "pumpkin":
                ok = True
                equip_status = None
                watch = sortie_plan.get("watch_names") or []
                for msg in self.pumpkin_stream(
                        team_no=sortie_plan.get("team_no", 3),
                        difficulty=sortie_plan.get("difficulty", 1),
                        watch_names=watch or None,
                        max_skips=sortie_plan.get("max_skips", 4),
                        auto_refill=False):
                    yield msg
                    if _is_fail(msg):
                        ok = False
                    equip_status = _equip_warning_status(msg, equip_status)
                status = equip_status or ("✓" if ok else "✗")
                report.append(("出阵(南瓜)", status))
            elif mode == "yosari":
                ok = True
                equip_status = None
                for msg in self.yosari_stream(
                        map_no=sortie_plan.get("map_no", 1),
                        team_no=sortie_plan.get("team_no", 3),
                        max_loops=sortie_plan.get("loops", 1),
                        auto_refill=sortie_plan.get("auto_refill", False),
                        auto_march=sortie_plan.get("auto_march", True),
                        stop_on_fatigue=sortie_plan.get("stop_on_fatigue", True),
                        formation_mode=sortie_plan.get("formation_mode", "manual"),
                        formation=sortie_plan.get("formation", "鱼鳞阵"),
                        repair_threshold=sortie_plan.get("repair_threshold", "light"),
                        injury_action=sortie_plan.get("repair_on_injury", "continue"),
                        auto_equip=sortie_plan.get("auto_equip", True),
                        rotate_captain=sortie_plan.get("rotate_captain", False),
                        rotate_captain_margin=sortie_plan.get(
                            "rotate_captain_margin", 10)):
                    yield msg
                    if _is_fail(msg):
                        ok = False
                    equip_status = _equip_warning_status(msg, equip_status)
                status = equip_status or ("✓" if ok else "✗")
                report.append(("出阵(异去)", status))
            elif mode == "osaka":
                ok = True
                equip_status = None
                for msg in self.osaka_stream(
                        max_floors=sortie_plan.get("loops", 1),
                        team_no=sortie_plan.get("team_no", 3),
                        select_floor=sortie_plan.get("select_floor", False),
                        target_floor=sortie_plan.get("target_floor", 81),
                        formation_mode=sortie_plan.get("formation_mode", "manual"),
                        formation=sortie_plan.get("formation", "鱼鳞阵"),
                        repair_threshold=sortie_plan.get("repair_threshold", "light"),
                        injury_action=sortie_plan.get("repair_on_injury", "continue"),
                        auto_equip=sortie_plan.get("auto_equip", True)):
                    yield msg
                    if _is_fail(msg):
                        ok = False
                    equip_status = _equip_warning_status(msg, equip_status)
                status = equip_status or ("✓" if ok else "✗")
                report.append(("出阵(大阪城)", status))
            else:
                yield "[日课] 配置为不打，跳过"
        except Exception as exc:
            report.append(("出阵", f"✗ {exc}"))
            yield f"[日课] 出阵翻车: {exc}"

    # ========== 收工盘点（锻刀拍过就不重复跑腿） ==========

    def _closing_snapshot_stream(self, forge_ran: bool):
        """日课收尾的家底盘点：锻刀步骤收工时已经顺手拍过完整快照（含小判），
        跑了锻刀就跳过；锻刀被跳过的话才专程导航拍一次。"""
        if forge_ran:
            complete = getattr(self, "_last_full_snapshot_complete", None)
            if complete is True:
                yield "[日课] 锻刀收工时已顺手盘点过家底（含小判），收工快照不再专程跑腿"
            elif complete is False:
                yield "[日课] 锻刀收工盘点不完整：小判没读到，其他家底已保存"
            else:
                yield "[日课] 没能确认本轮盘点是否完成"
            return
        for msg in self.status_snapshot_stream(phase="after"):
            yield msg
        if getattr(self, "_last_full_snapshot_complete", None) is False:
            yield "[日课] 收工盘点不完整：小判没读到，其他家底已保存"

    # ========== 冷启动：优先按包名直启，可信图标只作回退 ==========

    def _collect_pending_game_records(self):
        """am start 之前收走上一局的游戏记录（游戏一启动日志就被清空重写）。

        幂等：write_ledger 按 last_ts、sync_receipts 按 receipt_key、
        training.captured 按 payload 去重，重复收不重复记账。任何失败
        （adb 不通/日志不存在/解析翻车）都静默吞掉——收账是顺手福利，
        绝不能阻塞或炸掉游戏启动（日课主流程铁律）。成功时把计数留给
        调用处播报，并留一条机器事件。
        """
        self._records_precollected = None
        try:
            result = collect_game_records(self.maa.adb_path, self.maa.adb_address)
        except Exception:
            return
        self._records_precollected = result
        if hasattr(self, "record_event"):
            receipts = result.get("receipts") or {}
            training = result.get("training") or {}
            self.record_event("youzu_log.precollected",
                              observations_written=result.get("observations_written", 0),
                              changes_written=result.get("changes_written", 0),
                              receipts_written=receipts.get("written", 0),
                              training_written=training.get("written", 0))

    def _launch_game_via_adb(self) -> bool:
        """通过已配置的包名解析入口并启动游戏；不依赖可能被广告遮住的桌面。"""
        package = (self.config.get("daily", {}).get("logout", {})
                   .get("package", "com.youzu.djlw"))
        resolve = self.maa._adb_run([
            "shell", "cmd", "package", "resolve-activity", "--brief",
            "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER", package,
        ], timeout=20.0)
        if resolve is None:
            return False

        component = None
        for raw_line in reversed(resolve.decode("utf-8", "ignore").splitlines()):
            candidate = raw_line.strip()
            if candidate.startswith(package + "/") and " " not in candidate:
                component = candidate
                break
        if not component:
            return False

        # 启动前顺手收走上一局日志：游戏一旦 am start，旧日志立刻没了
        self._collect_pending_game_records()

        started = self.maa._adb_run(
            ["shell", "am", "start", "-W", "-n", component], timeout=30.0)
        return started is not None

    def _wait_for_game_entry(self):
        """等待登录页或本丸出现，返回是否确认游戏已进入可接管状态。"""
        for i in range(75):  # 最多等 150s
            time.sleep(2.0)
            self.maa.screenshot(force=True)
            if self.maa.exists("目录.png", threshold=0.7):
                yield f"[日课] 游戏已进入本丸（{i * 2 + 2}s）"
                return True
            if self.maa.exists("登录.png", threshold=0.7):
                yield f"[日课] 登录按钮出现（{i * 2 + 2}s）"
                return True
            # 版本更新框会挡在登录前（「检测到更新」→ 选线路），偶尔查一次
            if i % 6 == 3:
                upd = self.maa.ocr("线路一", roi_4to4(200, 300, 900, 600))
                if upd:
                    yield "[日课] 检测到游戏更新，选线路一更新..."
                    self.maa.click(upd)
            # 游戏其实已经在跑、只是停在签到/公告等中间界面时，
            # 死等本丸/登录页只会白等——认出来就交给登录流程接管
            if i % 4 == 2:
                hint = self._ingame_hint()
                if hint:
                    yield f"[日课] 游戏已在运行（{hint}，{i * 2 + 2}s），交给登录流程接管"
                    return True
        yield "[日课] 等待游戏登录页超时；没有继续盲点"
        return False

    def _ensure_game_started(self):
        """
        检查游戏开没开：在本丸就跳过；否则优先让 ADB 按包名直接启动。
        只有 ADB 明确失败，才点击经图标模板 + 文字 OCR 双重确认的桌面入口。
        """
        self.maa.screenshot(force=True)
        if self.maa.exists("目录.png", threshold=0.7):
            yield "[日课] 游戏已在本丸，直接开跑"
            return True
        if self.maa.exists("登录.png", threshold=0.7):
            yield "[日课] 游戏已在登录页，直接接管"
            return True
        # 游戏开着但停在签到页/公告等中间界面（issue#7 翻车现场）：
        # 不是没开，是不认识——认出来直接接管，别再去桌面找图标
        hint = self._ingame_hint()
        if hint:
            yield f"[日课] 游戏已在运行（{hint}），直接接管"
            return True

        if self._launch_game_via_adb():
            yield "[日课] 已绕过 MuMu 桌面遮挡，直接启动刀剑乱舞"
            pre = getattr(self, "_records_precollected", None)
            if pre:
                yield (f"[日课] 启动前已收走上一局游戏记录："
                       f"观察 {pre.get('observations_written', 0)} 条，"
                       f"收支 {pre.get('changes_written', 0)} 条")
            return (yield from self._wait_for_game_entry())

        yield "[日课] ADB 直启失败，尝试寻找经过文字确认的桌面图标"
        pt = self.maa.template_match("刀剑乱舞.png", threshold=0.8)
        if not pt:
            yield "[日课] 没找到可安全点击的刀剑乱舞入口；没有在当前画面盲点"
            return False

        # ⚠️ 广告担保层（7-29 实测翻车：模拟器广告里的像素跟图标模板撞脸，
        # 点下去直接触发下载了个别的游戏）。模板只是 55x55 图标图，没有文字，
        # 所以点击前必须 OCR 验明正身：真桌面图标正下方写着「刀剑乱舞」，广告没有。
        guard = roi_4to4(
            max(0, pt.x - 80), pt.y + 10,
            min(1280, pt.x + 80), min(720, pt.y + 110),
        )
        if not self.maa.ocr("刀剑乱舞", guard):
            yield "[日课] ⚠️ 找到疑似图标但底下没写「刀剑乱舞」——怕是广告，没敢点"
            return False

        yield "[日课] 游戏没开，点图标启动（OCR 验明正身 ✓）..."
        self.maa.click(pt)
        return (yield from self._wait_for_game_entry())

    def _ingame_hint(self):
        """游戏明显在跑、但既不是本丸也不是登录页时，认出停在哪个已知界面。
        返回界面描述（给人看的），认不出返回 None。调用前要有新截图。
        只加「点了安全」的识别：这里只负责认，接管后的动作由登录流程和扫地决定。"""
        # 启动后的签到日历：底部有「领取奖励」按钮（领过变灰字也在）
        if self.maa.ocr("领取奖励", roi_4to4(750, 550, 1100, 670)):
            return "签到页"
        # 公告/登录礼物弹窗：右上 X 或「今日不再弹出」
        for tpl in ("今日不再弹出.png", "通用_关闭.png"):
            if self.maa.template_match(tpl, threshold=0.7):
                return "公告/弹窗"
        return None

    # ========== 登录后弹窗扫地 ==========

    def _popup_sweep(self, max_rounds: int = 30) -> bool:
        """
        边等本丸边扫地：登录后本丸不安静——特别登录礼物/公告弹窗要关 X，
        远征归来/内番结束/修行结束的结算动画和对话要点点点往前推。
        连续 2 轮看到目录按钮且没弹窗可关、没动画在演，才算真正落地。

        Returns:
            是否到达本丸
        """
        clean = 0
        claimed_signin = False  # 启动签到奖励每轮扫地最多领一次（领完按钮变灰，字还在，别空点死循环）
        settle_seen = []      # 本轮已记账远征结算屏的像素指纹（同一屏只记一次）
        for _ in range(max_rounds):
            self.maa.screenshot(force=True)
            acted = False
            for tpl in ("今日不再弹出.png", "通用_关闭.png"):
                pt = self.maa.template_match(tpl, threshold=0.7)
                if pt:
                    self.maa.click(pt)
                    time.sleep(1.5)
                    acted = True
                    break
            if acted:
                clean = 0
                continue
            # 上次出阵被打断（手动停/断电/断网隔夜），重登会问要不要续打。
            # 脚本对旧断点毫无记忆，续打等于闭眼进战斗（重伤拦截/阵型都不在
            # 岗）——点【否】清掉中断数据回本丸，最多损失一圈没结算的进度。
            # （断网自愈流程里的【是】是另一码事：那是脚本自己刚断的线。）
            if self._network_resume_visible():
                print("[扫地] 检测到续打弹窗（上次出阵中断），点【否】清掉回本丸")
                no_pt = self.maa.ocr("否", roi_4to4(680, 420, 930, 510),
                                     match_mode="exact")
                if no_pt:
                    self.maa.click(no_pt)
                else:
                    self._click_point((800, 467))  # 【否】1280x720 固定坐标兜底
                time.sleep(3.0)
                clean = 0
                continue
            # 冷启动恰逢资源更新时，选完线路后登录页可能晚到或重新出现。
            # login() 只负责配置里的那一次点击；扫地阶段再看见登录按钮就补点，
            # 直到真正通过本丸目录探针，不能把“点过登录”当成“已经登录”。
            login_pt = self.maa.template_match("登录.png", threshold=0.7)
            if login_pt:
                self.maa.click(login_pt)
                clean = 0
                time.sleep(2.0)
                continue
            # 内番报告屏：谁+1 在这儿，先读再点穿（自然收工的横幅在本丸随机蹦）
            if self.maa.ocr("内番报告", roi_4to4(*naihanka_report.REPORT_TITLE_ROI)):
                for msg in self._collect_report_gains():
                    print(msg)
                # 报告已经读完才点穿，避免登录扫地每轮都停在同一张报告上。
                self.maa.click(Point(993, 690))
                time.sleep(1.0)
                continue
            # 道具详情窗（签到领奖后蹦的）：X 在 (945,105)，不是通用_关闭的样式
            if self.maa.ocr("道具详情", roi_4to4(450, 80, 830, 140)):
                print("[扫地] 关道具详情弹窗")
                self.maa.click(Point(945, 105))
                time.sleep(1.5)
                clean = 0
                continue
            # 启动签到日历：领每日奖励。领完按钮变灰但字还在，所以一轮只领一次；
            # 之后日历窗的 X 交给最上面的通用_关闭/今日不再弹出分支关
            if not claimed_signin:
                claim = self.maa.ocr("领取奖励", roi_4to4(750, 550, 1100, 670))
                if claim:
                    print("[扫地] 签到页，点领取奖励")
                    self.maa.click(claim)
                    claimed_signin = True
                    time.sleep(2.0)
                    clean = 0
                    continue
            # 刀剑男士申请修行：点穿对话后弹「修行启程」确认窗（是否消耗道具
            # 派遣 XX 修行）。点【取消】婉拒——弹窗自己写着随时能在强化/组织
            # 界面再派，可逆；点确认才要烧三件道具送走 96 小时，不替主人决定。
            # （2026-09-11 真机取帧：取消在左 (496,614)，确认在右，别记反。）
            if self.maa.ocr("修行启程", roi_4to4(450, 40, 830, 110)):
                cancel = self.maa.ocr("取消", roi_4to4(400, 560, 640, 670),
                                      match_mode="exact")
                self.maa.click(cancel if cancel else Point(496, 614))
                print("[扫地] 有刀剑男士申请修行，点【取消】婉拒"
                      "（想去的话到强化/组织界面手动派）")
                time.sleep(2.0)
                clean = 0
                continue
            # 修行中的来信：狐之助说有信、全屏信纸、读完后的提示分别推进。
            # 狐之助对话仍露着目录按钮，必须先认字再判定本丸已安静。
            letter_roi = roi_4to4(360, 445, 800, 550)
            if (self.maa.ocr("书信", letter_roi)
                    or (not self.maa.exists("目录.png", threshold=0.7)
                        and self.maa.ocr("致主人", roi_4to4(85, 85, 245, 170),
                                         match_mode="exact"))):
                print("[扫地] 修行来信，点过当前画面")
                self.maa.click(Point(993, 690))
                time.sleep(1.5)
                clean = 0
                continue
            # 远征结算屏：归来部队的收益先照实记账再点过（认不出记 unknown），
            # 不许盲点跳动画把账点没了——冤案二号之后扫地本来就负责收这块屏
            if hasattr(self, "observe_expedition_settlement"):
                obs = self.observe_expedition_settlement(via="popup_sweep",
                                                         seen=settle_seen)
                if obs is not None:
                    if obs.get("new"):
                        who = (f"部队{obs['team_no']}" if obs.get("team_no")
                               else "未知部队")
                        print(f"[扫地] 远征结算屏：{who} 从 "
                              f"{obs.get('header') or '未知地图'} 回来"
                              f"（结果{obs.get('result') or 'unknown'}），"
                              f"照实记账，点过")
                    self.maa.click(Point(993, 690))
                    clean = 0
                    time.sleep(1.5)
                    continue
            if self.maa.exists("目录.png", threshold=0.7):
                clean += 1
                if clean >= 2:
                    # 看着到本丸了还不算数——15:00 实测翻车教训：结算动画余波里
                    # 目录按钮看得见但点了没反应，连环导航失败。必须真开一次目录
                    # 验证界面稳了，顺手回本体本丸，才算落地。
                    if self._probe_nav_ready():
                        return True
                    clean = 0
            else:
                # 没弹窗也没目录按钮：在演结算动画/对话/加载，点跳过点往前推
                # (993,690) 是内番对话验证过的跳过点，结算动画也是点哪都前进
                self.maa.click(Point(993, 690))
                clean = 0
            time.sleep(2.0)
        return False

    def _probe_nav_ready(self) -> bool:
        """
        落地探针：本丸看着到了，再验一刀——目录能不能真打开。
        能打开就顺手点"本丸"回本体（也是后续步骤的安全出发位），返回 True。
        """
        pt = self.maa.template_match("目录.png", threshold=0.7)
        if not pt:
            return False
        self.maa.click(pt)
        for _ in range(4):
            time.sleep(0.7)
            self.maa.screenshot(force=True)
            if self.maa.exists("menu/ui目录.png"):
                self.current_location = "通用入口"
                return self.navigate_to("本丸")
        return False
