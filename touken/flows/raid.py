# -*- coding: utf-8 -*-
"""
上层业务：联队战（限时活动，专用流程，别抄去别的地方）

流程（教材 + 旧 raid.json 思路）：
  本丸 → 目录 → 出阵 → 活动 → 联队战界面 → 选难度（固定坐标）
  → 部队选择 → 选部队（固定坐标点两下）→（可选）自动换队长
  →（可选）自动行军委托：点自动行军 → 委托 → ✕ 关对话框
  → 即刻出阵
  → 确认弹窗（陆联旧弹窗 / 海联普通或特别合战场；可选三倍鱼笼）
  → 确定
  → 战斗循环：委托全自动（靠冷静期+横幅判圈结束）或手动 OCR"战斗"连点
  → 回到联队战界面 = 一圈结束，差分夜光贝记账

安全说明：
  - 联队战每场血量重置，无碎刀风险
  - 刀装未满警告（继续出阵/整备刀装弹窗）按教材规矩：停下上报，不擅自动
  - 海联补手形复用秘宝之里的数量滑条与确认链；陆联保留旧补票界面
  - 三倍鱼笼是甲州金道具：持有数 OCR 读出来 >0 才勾，读不出/为 0 绝不点
  - 自动行军委托挂不上（对话框没开或没验到勾选）就回退手动打法，
    绝不在原地卡死
"""

import re
import time
from datetime import datetime

from ..maa_adapter import roi_4to4
from ..runtime_paths import DEBUG_DIR


def _ocr_int(maa, roi_raw) -> int | None:
    """OCR 区域里只接受一个完整数值；多个数字混在一起时不猜。"""
    try:
        tokens = maa.ocr_all(roi_4to4(*roi_raw))
        numbers = [match.replace(",", "") for text, _ in tokens or []
                   for match in re.findall(r"\d[\d,]*", str(text))]
        return int(numbers[0]) if len(numbers) == 1 else None
    except Exception:
        return None


def _departure_config(cfg: dict, variant: str, config: dict) -> dict:
    departure = dict(cfg)
    if variant == "hailian":
        departure["confirm_ui"] = cfg.get("confirm_ui_hailian", {})
        departure["confirm_button"] = cfg.get("confirm_button_hailian", {})
        # 海联数量滑条与秘宝之里共用，不能再找陆联的“恢复一个”。
        departure["ticket_recover"] = config.get("hanafuda", {}).get("ticket_recover", {})
    return departure


class RaidMixin:
    """联队战流程。依赖宿主类的 navigate_to_stream、_click_point。"""

    def _save_raid_failure_frame(self, reason: str):
        """停在未知界面时留下运行帧，供事后定位。"""
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / f"raid_{reason}_{datetime.now():%Y%m%d_%H%M%S}.png"
            if self.maa.save_screenshot(str(path), force=True):
                return path
        except Exception:
            pass
        return None

    def raid_stream(self, max_rounds: int = 1, team_no: int = None,
                    use_triple: bool = True, max_buys: int = None,
                    difficulty_no: int = 4, auto_buy_ticket: bool = None,
                    auto_march: bool = None, rotate_captain: bool = None,
                    rotate_captain_margin: int = None):
        """
        流式跑联队战

        Args:
            max_rounds: 跑几圈（一圈 = 选难度到回到联队战界面）
            team_no: 部队编号，默认读配置 raid.team_no
            use_triple: 是否在确认弹窗勾三倍枡/三倍鱼笼（已勾选会跳过，
                        游戏有记忆）
            max_buys: 本次最多小判买几次手形（加班模式用），
                      不给就读配置 raid.max_buys_per_run
            auto_march: 每圈出阵前挂自动行军委托（海联新功能），挂不上就
                        回退手动打法；默认开
            rotate_captain: 每圈出阵前是否把疲劳最低的队员换到队长位
            rotate_captain_margin: 最低疲劳与现队长相差多少才换

        Yields:
            str: 执行状态消息
        """
        cfg = self.config.get("raid", {})
        if not cfg:
            yield "[RAID] 未配置联队战"
            return
        if max_buys is not None:
            cfg = dict(cfg)
            cfg["max_buys_per_run"] = max_buys
        if auto_buy_ticket is not None:
            cfg = dict(cfg)
            cfg["auto_buy_ticket"] = bool(auto_buy_ticket)

        team_no = team_no or cfg.get("team_no", 3)
        teams = self.config.get("team_select", {}).get("teams", {})
        if str(team_no) not in teams:
            yield f"[RAID] 配置里没有部队{team_no}的坐标"
            return

        if auto_march is None:
            auto_march = True
        if rotate_captain is None:
            rotate_captain = bool(cfg.get("rotate_captain", False))
        if rotate_captain_margin is None:
            rotate_captain_margin = int(cfg.get("rotate_captain_margin", 10))
        # 委托配置缺块（老安装没补到键）就当没开，流程照旧手动打
        march_cfg = cfg.get("auto_march") if auto_march else None

        self._raid_takeover_remaining = None
        self._ticket_buys = 0  # 本次运行的小判买票计数

        # ========== 1. 导航到出阵 ==========
        yield "[RAID] 正在导航到出阵..."
        for nav_msg in self.navigate_to_stream("出阵"):
            yield nav_msg
        if self.current_location != "出阵":
            yield "[RAID] 到达出阵失败"
            return

        # ========== 2. 进入联队战界面 ==========
        # 点"活动"按钮，等联队战标题出现（可能已经在联队战界面，那就直接过）。
        # 陆联/海联横幅不一样，两个模板都认；认到哪个就上报哪个变体。
        ui_title_variants = [("lulian", cfg["ui_title"]["template"])]
        hailian_title = cfg.get("ui_title_hailian")
        if hailian_title:
            ui_title_variants.append(("hailian", hailian_title["template"]))
        entered = None
        for _ in range(6):
            self.maa.screenshot(force=True)
            for variant, template in ui_title_variants:
                if self.maa.template_match(template):
                    entered = variant
                    break
            if entered:
                break
            entry = self.maa.template_match(cfg["activity_entry"]["template"])
            if entry:
                self.maa.click(entry)
                time.sleep(2.0)
            else:
                time.sleep(1.0)
        if not entered:
            yield "[RAID] 进不去联队战界面（活动结束了？）"
            return
        yield "[RAID] 到达联队战界面"
        self.set_progress(f"raid:{entered}")
        if entered != "hailian":
            march_cfg = None

        # ========== 3. 逐圈跑 ==========
        for round_no in range(1, max_rounds + 1):
            if self._expedition_takeover_requested():
                # 只在已确认上一圈结束的边界交接；当前圈尚未出发。
                self._raid_takeover_remaining = max_rounds - round_no + 1
                yield "[RAID] 🚩 远征排班请求接管：不开新圈，安全收工"
                return
            yield f"[RAID] ===== 第 {round_no}/{max_rounds} 圈 ====="

            # 3.1 选难度。旧配置只有图4坐标；其他图未标定时安全停止。
            #     夜光贝在本圈出发前先读一次家底（回到主界面再读一次算差分）。
            shells_before = self._read_shells_total(cfg)
            targets = cfg.get("difficulty_targets", {})
            target = targets.get(str(difficulty_no))
            if target is None and difficulty_no == 4:
                target = cfg.get("difficulty_target")
            if target is None:
                yield f"[RAID] 联队战{difficulty_no}图坐标尚未配置，本次不出阵"
                return
            self._click_point(target)
            time.sleep(0.8)

            # 3.2 点"部队选择"，OCR 验证标题
            deploy = self._find_deploy_button(cfg)
            if not deploy:
                self._save_raid_failure_frame("deploy_missing")
                yield "[RAID] 找不到部队选择按钮，已留图并停止"
                return
            self.maa.click(deploy)
            time.sleep(1.5)

            if not self._wait_for_team_select(cfg, attempts=10):
                self._save_raid_failure_frame("team_select_missing")
                yield "[RAID] 部队选择界面没打开，已留图并停止"
                return

            # 3.3 统一出阵链：选队、伤势、刀装、补票和重伤拦截都在这里。
            # 海联使用自己的确认标题和按钮；标题认错时安全链不会放行。
            departure_cfg = _departure_config(cfg, entered, self.config)
            if entered == "hailian":
                if (not cfg.get("confirm_ui_hailian", {}).get("ocr")
                        or not cfg.get("confirm_button_hailian", {}).get("template")):
                    yield "[RAID] 海联确认弹窗配置不全，本次不出阵"
                    return
                if not departure_cfg["ticket_recover"]:
                    yield "[RAID] 海联补票配置缺失，本次不出阵，请重启程序补齐配置"
                    return
            delegated = {"enabled": False}

            def prepare_team():
                if march_cfg:
                    delegated["enabled"] = bool(
                        (yield from self._raid_auto_march_stream(march_cfg)))
                    if not delegated["enabled"]:
                        yield "[RAID] 本圈回退手动打法"

            refill_state = {"used": False}
            max_buys_allowed = int(cfg.get("max_buys_per_run", 10))
            allow_refill = (bool(cfg.get("auto_buy_ticket", False))
                            and self._ticket_buys < max_buys_allowed)
            ok, _ = yield from self._safe_depart_stream(
                departure_cfg, team_no, "[RAID]", repair_threshold="heavy",
                auto_refill=allow_refill,
                rotate_captain=rotate_captain,
                rotate_captain_margin=rotate_captain_margin,
                prepare_team_stream=prepare_team if march_cfg else None,
                refill_state=refill_state)
            if refill_state["used"]:
                self._ticket_buys += 1
            if not ok:
                yield "[RAID] 出阵安全检查没通过，本次停止"
                return

            # 3.6 活动专用道具确认；有道具才勾，没有就直接确认出阵。
            popup_variant = entered
            if use_triple:
                fish3 = cfg.get("fish_basket3")
                if fish3 and popup_variant == "hailian":
                    for tri_msg in self._use_fish_basket3_stream(fish3):
                        yield tri_msg
                elif popup_variant == "lulian":
                    tri = cfg["triple"]
                    check_roi = roi_4to4(*tri["check_roi"])
                    self.maa.screenshot(force=True)
                    if self.maa.template_match(tri["check_template"], check_roi):
                        yield "[RAID] 三倍枡已勾选，跳过"
                    else:
                        self._click_point(tri["click"])
                        time.sleep(0.3)
                        yield "[RAID] 已勾三倍枡"

            if not self._confirm_departure(departure_cfg):
                self._save_raid_failure_frame("confirm_missing")
                yield "[RAID] 找不到出阵确认，已留图并停止"
                return
            yield "[RAID] 出发，进入战斗循环"

            # 3.7 战斗循环：委托全自动（靠冷静期+横幅判圈结束）或手动
            #     OCR"战斗"连点，回联队战界面 = 一圈完
            if delegated["enabled"]:
                for battle_msg in self.battle_loop_stream(
                        need_battle=False, max_iter=1200):
                    yield battle_msg
            else:
                for battle_msg in self.battle_loop_stream():
                    yield battle_msg
            round_done, battles = self._battle_loop_result

            if round_done:
                if hasattr(self, "record_event"):
                    payload = {"difficulty": difficulty_no,
                               "sequence": round_no,
                               "battle_taps": battles,
                               "triple": bool(use_triple)}
                    shells_after = self._read_shells_total(cfg)
                    if shells_after is not None:
                        payload["shells_total"] = shells_after
                        if shells_before is not None:
                            gained = shells_after - shells_before
                            if 0 <= gained <= 100_000:
                                payload["shells"] = gained
                    self.record_event("raid.round_completed", **payload)
                yield f"[RAID] 第 {round_no} 圈结束"
            else:
                yield f"[RAID] ⚠️ 战斗循环超过安全上限，强制停（打了 {battles} 场），你去看看卡哪了"
                return

        yield "[RAID] 全部圈数跑完，收工"
        return

    # ---------- 海联确认弹窗 / 鱼笼 / 委托 / 夜光贝 ----------

    def _use_fish_basket3_stream(self, fish3: dict):
        """海联弹窗勾三倍鱼笼。甲州金道具，规矩：
        持有数 OCR 读出来 >0 才勾；读不出或为 0 绝不点；已勾跳过；
        点完验不上也不再补点（再点一下反而会取消勾选）。"""
        count_roi = fish3.get("count_ocr", {}).get("roi")
        held = _ocr_int(self.maa, count_roi) if count_roi else None
        if held is None:
            yield "[RAID] 三倍鱼笼持有数没读出来，不碰甲州金道具"
            return
        if held <= 0:
            yield "[RAID] 三倍鱼笼持有 0，跳过勾选（甲州金道具不白买）"
            return
        check_roi = roi_4to4(*fish3["check_roi"])
        self.maa.screenshot(force=True)
        if self.maa.template_match(fish3["check_template"], check_roi):
            yield "[RAID] 三倍鱼笼已勾选，跳过"
            return
        self._click_point(fish3["click"])
        time.sleep(0.5)
        self.maa.screenshot(force=True)
        if self.maa.template_match(fish3["check_template"], check_roi):
            yield f"[RAID] 已勾三倍鱼笼（持有 {held}）"
        else:
            yield ("[RAID] ⚠️ 三倍鱼笼点完没验上，不再补点"
                   "（防反而取消勾选），你手动瞅一眼")

    def _raid_auto_march_stream(self, march_cfg: dict):
        """海联自动行军（委托）：点自动行军按钮 → 等对话框 → 点委托 →
        回读勾选 → ✕ 关闭。任何一步认不出来都安全回退手动打法，
        绝不卡死。委托状态跨圈持续，每圈重开只核对勾选。"""
        dlg = march_cfg.get("dialog_ocr", {})
        dlg_roi_raw = dlg.get("roi")
        dep = march_cfg.get("delegate_ocr", {})
        dep_roi_raw = dep.get("roi")
        selected_template = march_cfg.get("selected_template")
        selected_roi_raw = march_cfg.get("selected_roi")
        close = march_cfg.get("close")
        if not (march_cfg.get("button") and dlg_roi_raw
                and dep_roi_raw and selected_template and selected_roi_raw
                and close):
            yield "[RAID] 自动行军配置不全，本圈手动打"
            return False
        self.maa.screenshot(force=True)
        self._click_point(march_cfg["button"])
        opened = False
        for _ in range(10):
            time.sleep(0.6)
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected=dlg["expected"],
                            roi=roi_4to4(*dlg_roi_raw)):
                opened = True
                break
        if not opened:
            yield "[RAID] 自动行军对话框没开，本圈手动打"
            return False
        selected_roi = roi_4to4(*selected_roi_raw)
        if self.maa.template_match(selected_template, selected_roi):
            self._click_point(close)
            time.sleep(0.8)
            yield "[RAID] 自动行军委托已勾选，本圈继续使用"
            return True
        delegate = None
        for _ in range(6):
            hit = self.maa.ocr(expected=dep["expected"],
                               roi=roi_4to4(*dep_roi_raw))
            if hit:
                delegate = hit
                break
            time.sleep(0.4)
            self.maa.screenshot(force=True)
        if not delegate:
            self._click_point(close)
            time.sleep(0.8)
            yield "[RAID] 委托按钮认不出，已关对话框，本圈手动打"
            return False
        self.maa.click(delegate)
        time.sleep(0.8)
        self.maa.screenshot(force=True)
        selected = self.maa.template_match(selected_template, selected_roi)
        self._click_point(close)
        time.sleep(1.0)
        if not selected:
            yield "[RAID] 委托后没有读到勾选，本圈手动打"
            return False
        # 复查：对话框说明文字应已消失；没消失再补一下 ✕
        self.maa.screenshot(force=True)
        if self.maa.ocr(expected=dlg["expected"],
                        roi=roi_4to4(*dlg_roi_raw)):
            self._click_point(close)
            time.sleep(0.8)
        yield "[RAID] ✓ 已挂自动行军委托，本圈全自动"
        return True

    def _read_shells_total(self, cfg) -> int | None:
        """读联队战主界面的夜光贝累计数（记账用，读不出不挡路）。"""
        ocr_cfg = cfg.get("shells_total_ocr", {})
        roi_raw = ocr_cfg.get("roi")
        if not roi_raw:
            return None
        return _ocr_int(self.maa, roi_raw)

    # ---------- 战斗循环（独立公开，中途断线也能单独恢复）----------

    _battle_loop_result: tuple = (False, 0)

    def battle_loop_stream(self, cfg_key: str = "raid", tag: str = "[RAID]",
                           need_battle: bool = True, debug_dir: str = None,
                           fought: int = None, max_iter: int = 300):
        """
        战斗循环：OCR"战斗"连点下一场，安全区跳动画，回到活动界面算一圈完。
        结果放在 self._battle_loop_result = (是否完成, 打了几场)。

        出发后有短暂的过场，活动标题还留在屏幕上，
        此时一圈结束判定会误命中——冷静期内不判结束。

        cfg_key: 读哪段配置（raid / pumpkin 共用本循环）
        tag: 日志前缀
        need_battle: True=至少打过1场才算结束（联队战，防过场误命中）；
                     False=全自动战斗的活动（南瓜），靠冷静期就够
        debug_dir: 调试截图目录，给了就把每帧存下来（抓获得动画用）
        fought: 心跳里显示的场数。内部 battles 数的是"点过几次战斗按钮"，
                全自动战斗（南瓜）永远点不到按钮、恒为 0，会吓人，
                所以全自动模式由外层把已出阵次数传进来显示。
        max_iter: 安全上限迭代数，防死循环。委托全自动的圈比手动慢
                （一场场自动打），上限要给大些。
        """
        cfg = self.config.get(cfg_key, {})
        battles = 0
        round_done = False
        # 一圈结束 = 活动标题回到屏幕。联队战陆联/海联横幅不一样，
        # round_end 之外的变体（round_end_hailian）一并认。
        end_checks = []
        for key in ("round_end", "round_end_hailian"):
            end_cfg = cfg.get(key)
            if not end_cfg:
                continue
            # roi 可留空：有的模板（RGBA 的 ui南瓜）在 MAA 的 roi 匹配下会神秘不中，
            # 全屏 + 高阈值一样稳，标题本身够独特
            end_roi = roi_4to4(*end_cfg["roi"]) if end_cfg.get("roi") else None
            end_checks.append((end_cfg["template"], end_roi, end_cfg["threshold"]))
        if not end_checks:
            raise KeyError(f"{cfg_key} 配置缺 round_end")
        battle_cfg = cfg["battle_ocr"]
        battle_roi = roi_4to4(*battle_cfg["roi"])

        if debug_dir:
            from pathlib import Path as _P
            _P(debug_dir).mkdir(parents=True, exist_ok=True)
        debug_idx = 0

        battle_loop_start = time.time()
        END_CHECK_GRACE_SEC = 20

        for _i in range(max_iter):  # 安全上限，防死循环
            # 心跳日志：每 5 次报一次进度，卡死时能看到日志停在哪
            # 场数显示：全自动战斗（南瓜）内部 battles 恒 0，用外层传的 fought
            if _i % 5 == 0:
                shown = fought if fought is not None else battles
                yield f"{tag} 战斗循环心跳 {_i}/{max_iter}（已打 {shown} 场）"
                self.quick_peek(tag=cfg_key)  # 顺路拍顶栏家底，零导航（60s 节流）
            self.maa.screenshot(force=True)

            if debug_dir:
                debug_idx += 1
                self.maa.save_screenshot(f"{debug_dir}/f{debug_idx:03d}.png", force=False)

            # 网络超时弹窗（MuMu 断网）：点确定没用就重启模拟器续打
            net = yield from self.recover_network_stream()
            if net is None:
                yield f"{tag} ⚠️ 断网恢复失败，战斗循环安全停止（已打 {battles} 场）"
                break
            if net == "home":
                yield f"{tag} ⚠️ 断网恢复后落在本丸，这一圈打到哪不明，安全停止（已打 {battles} 场）"
                break
            if net == "resumed":
                battle_loop_start = time.time()  # 续打回来重新给冷静期
                continue

            # 继续下一场？（右下角"战斗"按钮）
            battle_btn = self.maa.ocr(expected=battle_cfg["expected"], roi=battle_roi)
            if battle_btn:
                battles += 1
                yield f"{tag} 第 {battles} 场"
                self.maa.click(battle_btn)
                time.sleep(3.0)
                continue

            # 一圈结束？（活动标题回到屏幕上，高阈值防战斗中误认）
            if (battles >= 1 or not need_battle) and time.time() - battle_loop_start > END_CHECK_GRACE_SEC:
                for end_template, end_roi, end_threshold in end_checks:
                    if self.maa.template_match(end_template, end_roi, end_threshold):
                        round_done = True
                        break
                if round_done:
                    break

            # 都不是 → 点安全区跳对话/动画
            self._click_point(cfg["skip_tap"])
            time.sleep(0.8)

        self._battle_loop_result = (round_done, battles)
