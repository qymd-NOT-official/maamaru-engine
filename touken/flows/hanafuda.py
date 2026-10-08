# -*- coding: utf-8 -*-
"""
上层业务：秘宝之里（花牌收集）活动

玩法（2026-09-11 实机科研，难度·超难 + 委托全程验证）：
  本丸→出阵→活动入口→秘宝之里标题确认→点难度块→部队选择
  →委托自动行军（必须挂上，v1 不做手动行军，挂了委托图内游戏全包：
    选花牌点、阵形、战斗、结算页「剩余 N 秒」自动继续行军）
  →通用安全出阵链→活动专用令牌确认弹窗点确定
  →行动次数耗尽或首领打完 → 直接回活动主界面，无阻塞结算弹窗，令牌 -1

安全规矩：
  - 出阵走 BattleMixin 通用安全出阵链（_safe_depart_stream）+
    _confirm_departure 二次确认。确认弹窗是活动专用（「出阵会消耗通行令牌，
    是否确认？」），标准 team/ui出阵确认.png 打不上它（离线实测 0.445），
    用花札自己的模板 花札/ui出阵确认.png（取自 MAAAdapter 运行帧）
  - 虚拟伤害活动不碎刀，默认只拦重伤（repair_threshold=heavy），中伤照跑
  - 图内脚本只盯异常，绝不碰选牌/行军：
      * 狐之助教学/提示气泡是唯一会卡死委托的东西（实测挂 25 秒+不动），
        地图 HUD 在且对话条有字 = 气泡，点掉继续；
      * 重伤行军警告永远点否，然后停手让人看；
      * 断网走 recover_network_stream，恢复落本丸则本圈作废收工
  - 票尽不数令牌格：游戏自己弹补票窗，安全链按 auto_refill 处理
    （花札补充页是滑条式：补充→数量页确认→小判消耗确认→结果确认，
    默认补 1 个，OCR 复核数量不是 1 就停手；不补则点右上角 X 收工）
"""

import re
import time
from pathlib import Path

from ..maa_adapter import roi_4to4

_DIFFICULTY_LABELS = {1: "易", 2: "普", 3: "难", 4: "超难"}


class HanafudaMixin:
    """秘宝之里（花牌收集）流程。依赖宿主类的 navigate_to_stream、
    _click_point、skip_safe、wait_landmark_skipping、_wait_for_team_select、
    _safe_depart_stream、_confirm_departure、_enable_auto_march、
    _deny_heavy_injury_warning、recover_network_stream。"""

    def hanafuda_stream(
        self,
        team_no: int = None,
        difficulty: int = None,
        max_runs: int = None,
        auto_refill: bool = None,
        rotate_captain: bool = None,
        rotate_captain_margin: int = None,
        debug_dir: str = None,
        **kwargs,
    ):
        """
        流式刷秘宝之里（花牌收集）

        Args:
            team_no: 部队编号，默认读配置 hanafuda.team_no
            difficulty: 难度 1易/2普/3难/4超难，默认读配置 hanafuda.difficulty
            max_runs: 本次最多跑几圈，必须为正数
            auto_refill: 票尽时是否用小判补票（走游戏自己的补票弹窗），
                默认读配置 hanafuda.use_koban_refill
            rotate_captain: 每圈出阵前是否把疲劳最低的队员换到队长位
            rotate_captain_margin: 最低疲劳与现队长相差多少才换
        """
        cfg = self.config.get("hanafuda", {})
        if not cfg:
            yield "[花札] 未配置秘宝之里活动"
            return

        team_no = team_no if team_no is not None else cfg.get("team_no", 3)
        if difficulty is None:
            difficulty = int(cfg.get("difficulty", 4))
        difficulty = int(difficulty)
        if auto_refill is None:
            auto_refill = bool(cfg.get("use_koban_refill", False))
        if max_runs is None:
            max_runs = int(cfg.get("max_runs", cfg.get("refill_run_limit", 6)))
        if max_runs <= 0:
            max_runs = 6
        if rotate_captain is None:
            rotate_captain = bool(cfg.get("rotate_captain", False))
        if rotate_captain_margin is None:
            rotate_captain_margin = int(cfg.get("rotate_captain_margin", 10))
        repair_threshold = str(cfg.get("repair_threshold", "heavy"))
        auto_equip = bool(cfg.get("auto_equip", False))

        card = cfg.get("difficulty_cards", {}).get(str(difficulty))
        if not card:
            yield f"[花札] 配置里没有难度{difficulty}的坐标"
            return
        label = _DIFFICULTY_LABELS.get(difficulty, str(difficulty))

        teams = self.config.get("team_select", {}).get("teams", {})
        if str(team_no) not in teams:
            yield f"[花札] 配置里没有部队{team_no}的坐标"
            return

        # ========== 1. 导航到出阵 ==========
        yield "[花札] 正在导航到出阵…"
        for nav_msg in self.navigate_to_stream("出阵"):
            yield nav_msg
        if self.current_location != "出阵":
            yield "[花札] 到达出阵失败"
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
            yield "[花札] 进不去活动界面（秘宝之里过季了？）"
            return
        yield "[花札] 到达秘宝之里入口"
        self.set_progress("hanafuda")

        # ========== 3. 主循环：一圈一圈跑 ==========
        runs_done = 0
        total_tama = 0
        team_record_saved = False
        while True:
            if max_runs > 0 and runs_done >= max_runs:
                yield f"[花札] 已达最大圈数 {max_runs}，收工"
                break
            if self._expedition_takeover_requested():
                self._expedition_takeover_remaining = max_runs - runs_done if max_runs > runs_done else None
                yield "[花札] 🚩 远征排班请求接管：不开新圈，安全收工"
                break

            tama_before = self._read_tama_total(cfg)
            yield f"[花札] 🎴 第 {runs_done + 1} 圈开场（难度·{label}）"

            entered, team_record_saved = yield from self._enter_hanafuda_map_stream(
                cfg, team_no, card, repair_threshold, auto_equip,
                team_record_saved, auto_refill=auto_refill,
                rotate_captain=rotate_captain,
                rotate_captain_margin=rotate_captain_margin)
            if not entered:
                yield "[花札] 没能进图，安全收工"
                break

            ok = yield from self._watch_round_stream(cfg, debug_dir)
            if not ok:
                yield "[花札] 本圈异常，已停手；游戏画面你去看一眼"
                return

            runs_done += 1
            tama_after = self._read_tama_total(cfg)
            if tama_before is not None and tama_after is not None:
                delta = tama_after - tama_before
                total_tama += delta
                if hasattr(self, "record_event"):
                    self.record_event("hanafuda.run_completed",
                                      run_no=runs_done, team_no=team_no,
                                      difficulty=difficulty, tama=delta,
                                      tama_total=tama_after)
                yield (f"[花札] ✓ 第 {runs_done} 圈收工，本圈玉 {delta:+d} "
                       f"（活动累计 {tama_after} 个）")
            else:
                if hasattr(self, "record_event"):
                    payload = {"run_no": runs_done, "team_no": team_no,
                               "difficulty": difficulty}
                    if tama_after is not None:
                        payload["tama_total"] = tama_after
                    self.record_event("hanafuda.run_completed", **payload)
                if tama_after is not None:
                    yield (f"[花札] ✓ 第 {runs_done} 圈收工，本圈增量没读出来 "
                           f"（活动累计 {tama_after} 个）")
                else:
                    yield f"[花札] ✓ 第 {runs_done} 圈收工（玉数没读出来，不碍事）"

        yield f"[花札] 收工，跑了 {runs_done} 圈，合计带回 {total_tama} 个玉"

    # ---------- 内部：入场 → 地图 ----------

    def _enter_hanafuda_map_stream(self, cfg: dict, team_no: int, card_point: list,
                          repair_threshold: str, auto_equip: bool,
                          team_record_saved: bool,
                          auto_refill: bool = False,
                          rotate_captain: bool = False,
                          rotate_captain_margin: int = 10):
        """点难度块→部队选择→挂委托→通用安全出阵链→令牌确认，直到进图。

        选队、伤势检查、刀装处理、重伤拦截全部走 BattleMixin 的
        _safe_depart_stream，花札只负责入口导航。委托必须在出阵前挂上：
        挂不上就宁可不出阵（v1 不做手动行军）。
        返回 (entered, team_record_saved)。
        """
        tag = "[花札]"
        self._click_point(card_point)
        time.sleep(1.2)

        # 同江户城：先看标题再点入口，绝不拿上一帧坐标点当前帧
        if not self._wait_for_team_select(cfg, attempts=10, open_after=1):
            yield f"{tag} 部队选择界面没打开"
            self.skip_safe(2, point=cfg.get("skip_tap"))
            return False, team_record_saved

        if not self._enable_auto_march():
            yield (f"{tag} ⚠️ 自动行军（委托）没挂上；花札只支持委托跑图，"
                   "本次不出阵，你手动看一眼部队选择页")
            return False, team_record_saved

        departure_cfg = dict(cfg)
        # 花札自己的补充页是滑条式（补充→确认），优先用 hanafuda 段的
        # ticket_recover；老配置还没有就退回联队战样式（恢复一个→确定）。
        departure_cfg["ticket_recover"] = (
            cfg.get("ticket_recover")
            or self.config.get("raid", {}).get("ticket_recover", {}))
        ok, team_record_saved = yield from self._safe_depart_stream(
            departure_cfg, team_no, tag,
            repair_threshold=repair_threshold,
            auto_equip=auto_equip,
            team_record_saved=team_record_saved,
            auto_refill=auto_refill,
            rotate_captain=rotate_captain,
            rotate_captain_margin=rotate_captain_margin)
        if not ok:
            return False, team_record_saved

        # 活动专用令牌确认弹窗：模板是「出阵会消耗通行令牌」那行
        if not self._confirm_departure(cfg):
            yield f"{tag} 没看到令牌确认弹窗，停止点击"
            return False, team_record_saved
        return True, team_record_saved

    # ---------- 内部：图内监控（委托全托管，只盯异常） ----------

    def _watch_round_stream(self, cfg: dict, debug_dir: str = None,
                            timeout_s: float = 1500.0):
        """委托跑图监控：等一圈跑完回活动主界面，中途只处理异常。

        正常一圈（超难 7 场左右）约 6～8 分钟，超时上限给 25 分钟。
        返回 True=一圈正常跑完（回到可操作的活动主界面）。

        圈结束判定必须先「上膛」：进图过场会重播活动横幅（2026-09-11 真机
        实测 ui_title 在过场帧 1.000 假命中，脚本差点在出发点直接宣布收工），
        所以先见到地图 HUD（剩余行动次数）才承认 ui_title 算圈结束；
        且要求部队选择按钮也在，防结算过场的横幅假命中。
        """
        hud = cfg.get("map_hud_ocr", {})
        hud_roi = roi_4to4(*hud["roi"]) if hud.get("roi") else None
        bubble_cfg = cfg.get("bubble_ocr", {})
        bubble_roi = roi_4to4(*bubble_cfg["roi"]) if bubble_cfg.get("roi") else None
        bubble_tap = cfg.get("bubble_tap", [870, 394])

        deadline = time.monotonic() + max(1.0, float(timeout_s))
        entry_deadline = time.monotonic() + 90.0
        armed = False  # 见过地图 HUD 才承认圈结束
        last_heartbeat = time.monotonic()
        bubbles = 0
        while time.monotonic() < deadline:
            self.maa.screenshot(force=True)

            # 重伤行军警告：永远点否。活动不碎刀，但警告一出说明局面
            # 超出脚本该管的范围，停手让人看
            if self._deny_heavy_injury_warning(cfg):
                self._save_hanafuda_shot(debug_dir, "heavy_injury_warning")
                yield ("[花札] 🛑 出现重伤行军警告，已点【否】；"
                       "先停下了，你去看一眼队伍")
                return False

            # 断网：恢复成功回原画面继续巡逻；落本丸则本圈作废
            net = yield from self.recover_network_stream()
            if net is None:
                yield "[花札] 断网没救回来，停"
                return False
            if net == "home":
                yield "[花札] 断网恢复后落在本丸，本圈状态作废，安全收工"
                return False

            if not armed:
                if hud_roi and self.maa.ocr(expected=hud["expected"], roi=hud_roi):
                    armed = True
                    yield "[花札] 进图了，委托跑图我盯着"
                elif time.monotonic() > entry_deadline:
                    self._save_hanafuda_shot(debug_dir, "entry_missing")
                    yield ("[花札] ⚠️ 确认出阵后 90 秒还没见到地图，"
                           "疑似没进图，停手留证（截图已存）")
                    return False
                time.sleep(0.9)
                continue

            # 圈结束：行动次数耗尽/首领打完回活动主界面（横幅+部队选择双保险）
            if self.maa.template_match(cfg["ui_title"]["template"]) \
                    and self._find_deploy_button(cfg):
                return True

            # 狐之助气泡：唯一会卡死委托的东西。已上膛=地图 HUD 刚出现过；
            # 战斗/结算页 HUD 不在，自然不会误判
            if hud_roi and bubble_roi and self.maa.ocr(
                    expected=hud["expected"], roi=hud_roi):
                if self.maa.ocr_all(bubble_roi):
                    bubbles += 1
                    if bubbles > 40:
                        self._save_hanafuda_shot(debug_dir, "bubble_stuck")
                        yield ("[花札] ⚠️ 气泡点了 40 下还没完，不像教学气泡，"
                               "停手留证（截图已存）")
                        return False
                    self._click_point(bubble_tap)
                    time.sleep(0.8)
                    continue
                bubbles = 0

            if time.monotonic() - last_heartbeat > 60:
                last_heartbeat = time.monotonic()
                tama = self._read_hud_tama(cfg)
                detail = f"，本圈已捡 {tama} 个玉" if tama is not None else ""
                yield f"[花札] 委托跑图中{detail}…"
            time.sleep(0.9)

        self._save_hanafuda_shot(debug_dir, "round_watchdog")
        yield (f"[花札] ⚠️ 一圈跑了 {int(timeout_s // 60)} 分钟还没回活动界面，"
               "疑似卡住，停手留证（截图已存）")
        return False

    # ---------- 内部：识别 ----------

    def _read_tama_total(self, cfg: dict) -> int | None:
        """读活动主界面的玉累计数（记账用，读不出不挡路）。"""
        ocr_cfg = cfg.get("tama_total_ocr", {})
        roi_raw = ocr_cfg.get("roi")
        if not roi_raw:
            return None
        return self._ocr_int(roi_4to4(*roi_raw))

    def _read_hud_tama(self, cfg: dict) -> int | None:
        """读地图 HUD 的本圈玉数（心跳用；小数字，放大识别）。"""
        ocr_cfg = cfg.get("hud_tama_ocr", {})
        roi_raw = ocr_cfg.get("roi")
        if not roi_raw:
            return None
        return self._ocr_int(roi_4to4(*roi_raw),
                             upscale=int(ocr_cfg.get("upscale", 3)))

    def _ocr_int(self, roi, upscale: int = 0) -> int | None:
        """OCR 区域内文字并提取最长连续数字串；upscale>1 先裁块放大再认。"""
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
            digits = "".join(
                re.sub(r"\D", "", str(text)) for text, _ in tokens or []
            )
            return int(digits) if digits else None
        except Exception:
            return None

    def _save_hanafuda_shot(self, debug_dir: str | None, name: str):
        """异常现场存证：优先 debug_dir，否则用户数据目录 debug/。"""
        try:
            out = Path(debug_dir) if debug_dir else Path(self._root) / "debug"
            out.mkdir(parents=True, exist_ok=True)
            self.maa.save_screenshot(
                str(out / f"hanafuda_{name}_{time.strftime('%H%M%S')}.png"))
        except Exception:
            pass
