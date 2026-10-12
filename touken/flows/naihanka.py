# -*- coding: utf-8 -*-
"""
上层业务：内番（日课，安全无消耗）

流程（教材原文）：
  目录 → 内番 → 内番开始 → 二次确认点确定
  → 内番开始动画/对话 → 跳过 → 可能弹"内番符"氪金加速（不准用，点取消）
  → 回到内番界面，右下角出现"内番中" = 完工

教材补充：
  - 完工标志也可以是"目录界面的内番没有角标"
  - 如果内番已经在跑（没有内番开始按钮，直接看到内番中），跳过不重复开始
"""

import json
import time

from ..maa_adapter import roi_4to4
from ..runtime_paths import STATUS_DIR
from ..game_state import available_events, duty_state, begin_operation, confirm_operation
from . import naihanka_report


def _load_naihanka_state() -> dict:
    """读 status/naihanka.json，没有或坏了就空字典（别为个记录文件炸流程）"""
    try:
        p = STATUS_DIR / "naihanka.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_naihanka_state(data: dict) -> None:
    try:
        STATUS_DIR.mkdir(exist_ok=True)
        (STATUS_DIR / "naihanka.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class NaihankaMixin:
    """内番流程。依赖宿主类的 navigate_to_stream、_click_point。"""

    def naihanka_stream(self):
        """
        流式开始内番

        Yields:
            str: 执行状态消息
        """
        cfg = self.config.get("naihanka", {})
        if not cfg:
            yield "[内番] 未配置内番"
            return

        # ========== 1. 导航到内番 ==========
        yield "[内番] 正在导航到内番..."
        for nav_msg in self.navigate_to_stream("内番"):
            yield nav_msg
        if self.current_location != "内番":
            yield "[内番] 到达内番失败"
            return

        # ========== 1.5 收结果观察窗 ==========
        # 内番跑完后再进来会自动播：结束横幅 → 对话 → 内番报告（谁+1）
        for obs_msg in self.naihanka_observe_stream():
            yield obs_msg

        # ========== 1.6 数值快照比对（报告屏徽章的二次确认）==========
        for snap_msg in self.naihanka_snapshot_stream():
            yield snap_msg

        # ========== 2. 判断状态：已在跑 or 可以开始 ==========
        time.sleep(1.0)
        self.maa.screenshot(force=True)

        marker_cfg = cfg["running_marker"]
        marker_roi = roi_4to4(*marker_cfg["roi"]) if "roi" in marker_cfg else None
        duty = duty_state(available_events(self.maa))
        if duty and duty["running"]:
            self._remember_duty(duty)
            yield "[内番] 游戏数据确认已经在内番中，收工"
            return
        if duty is None and self.maa.template_match(marker_cfg["template"], marker_roi):
            yield "[内番] 已经在内番中了，今天不用开始，收工"
            return

        start = self.maa.template_match(cfg["start_button"]["template"])
        if not start:
            yield "[内番] 既没看到内番开始按钮也没看到内番中标记，画面不对劲，停"
            return

        # ========== 3. 点开始 + 二次确认 ==========
        self.maa.click(start)
        time.sleep(1.0)

        confirmed = False
        for _ in range(10):
            self.maa.screenshot(force=True)
            confirm = self.maa.template_match(cfg["confirm_button"]["template"])
            if confirm:
                self._duty_boundary = begin_operation(self.maa)
                self.maa.click(confirm)
                confirmed = True
                break
            time.sleep(0.5)
        if not confirmed:
            yield "[内番] 二次确认弹窗没出现，停"
            return
        yield "[内番] 已确认开始，进入跳动画阶段"
        time.sleep(1.0)

        # ========== 4. 跳动画/对话，防内番符氪金弹窗 ==========
        for skip_msg in self.skip_dialogue_stream():
            yield skip_msg
        return

    @staticmethod
    def _remember_duty(duty):
        if not duty.get("started_at"):
            return
        state = _load_naihanka_state()
        state["started_at"] = duty["started_at"]
        _save_naihanka_state(state)

    # ---------- 对话跳过（独立公开，卡在对话里能单独救场）----------

    def skip_dialogue_stream(self):
        """
        跳过内番对话/动画，直到真正回到内番界面（标题 + 内番中标记）。

        防踩坑：点完确定后、对话开始前，内番界面会短暂闪现（内番中标记也在），
        此时判完工会被对话卡住——所以必须"见过画面离开内番界面"（对话发生过）
        或确认超过 10 秒，才接受完工判定。
        """
        cfg = self.config.get("naihanka", {})
        marker_cfg = cfg["running_marker"]
        marker_roi = roi_4to4(*marker_cfg["roi"]) if "roi" in marker_cfg else None
        title_template = cfg["ui_title"]["template"]

        boundary = getattr(self, "_duty_boundary", None)
        self._duty_boundary = None
        outcome = confirm_operation(self.maa, boundary, "duty") if boundary else None
        if outcome is False:
            yield "[内番] 游戏拒绝本次内番开始，本次停止"
            return
        duty = duty_state(available_events(self.maa))
        if outcome is True:
            state = _load_naihanka_state()
            state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boundary["result_at"]))
            _save_naihanka_state(state)
        elif duty and duty["running"]:
            self._remember_duty(duty)
        confirm_time = time.time()
        dialogue_seen = False

        for _ in range(60):  # 安全上限
            self.maa.screenshot(force=True)

            # 内番符氪金弹窗 → 教材规矩：不准用，点取消
            cancel = self.maa.template_match(cfg["cancel_button"]["template"])
            if cancel:
                self.maa.click(cancel)
                yield "[内番] 检测到内番符弹窗，已点取消（咱不氪这个）"
                time.sleep(1.0)
                continue

            # 画面离开了内番界面 = 对话/动画正在演
            title_visible = self.maa.template_match(title_template)
            if not title_visible:
                dialogue_seen = True

            # 完工标志：回到内番界面 + 右下角内番中
            running = outcome is True or bool(duty and duty["running"])
            if title_visible and (running or self.maa.template_match(marker_cfg["template"], marker_roi)):
                elapsed = time.time() - confirm_time
                if dialogue_seen or elapsed > 10:
                    yield "[内番] 内番开始成功，收工 🎉"
                    return

            self._click_point(cfg["skip_tap"])
            time.sleep(0.8)

        if outcome is True or duty and duty["running"]:
            yield "[内番] 内番已开始；对话页面暂未回到内番页，请检查画面"
        else:
            yield "[内番] ⚠️ 跳动画超过安全上限还没看到内番中，你去看看卡哪了"
        return

    # ---------- 收结果观察窗 & 数值快照（谁+1 识别，纯观察不碰开工逻辑）----------

    def _collect_report_gains(self) -> list:
        """报告屏读「谁+1」：等徽章落定 → 读屏 → 落盘 last_report。

        本次会话只读一回（读过直接返回空）；观察窗和登录扫地共用。
        Returns: 待播报的消息列表（调用方决定 yield 还是 print）。
        """
        if getattr(self, "_naihanka_report_read", False):
            return []
        self._naihanka_report_read = True
        time.sleep(2.0)  # 徽章按组蹦，切磋组慢，等落定再读
        img = self.maa.screenshot(force=True)
        try:
            gains = naihanka_report.read_report_gains(img, self.maa.ocr_all)
        except Exception:
            gains = []
        self._naihanka_gains = gains
        # 成绩单「全部记录」留痕：收工了谁+1（没人+1也记，方便查谁不干活）
        self.record_event("naihanka.gains", source="report", gains=[
            {"name": g["name"], "stat": g["stat"]} for g in gains])
        if gains:
            state = _load_naihanka_state()
            state["last_report"] = {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "gains": gains,
            }
            _save_naihanka_state(state)
            return [f"[内番] 🎉 {gn['name']} {gn['stat']}+1" for gn in gains]
        return ["[内番] 报告屏没人+1（或者都喂满金框了）"]

    def naihanka_observe_stream(self):
        """进内番后的观察窗：内番跑完会自动播收结果动画（横幅→对话→内番报告）。

        看到报告屏就读「谁+1」；顺手把动画点穿回今日内番表。
        画面本来就是内番表（内番中/待开工）则一轮就退出，一下不多点。
        读到的东西存 self._naihanka_gains 供快照比对去重。
        """
        cfg = self.config.get("naihanka", {})
        title_roi = roi_4to4(*naihanka_report.REPORT_TITLE_ROI)
        title_template = cfg["ui_title"]["template"]
        self._naihanka_gains = []
        self._naihanka_report_read = False

        for _ in range(10):  # 安全上限
            self.maa.screenshot(force=True)

            # 内番报告屏：谁+1 在这儿
            if self.maa.ocr("内番报告", title_roi):
                for msg in self._collect_report_gains():
                    yield msg
                self._click_point(cfg["skip_tap"])
                time.sleep(1.2)
                continue

            # 回到今日内番表 = 收结果播完了（或本来就没得收）
            if self.maa.template_match(title_template):
                break

            # 残留的内番符弹窗 → 教材规矩：不准用，点取消
            cancel = self.maa.template_match(cfg["cancel_button"]["template"])
            if cancel:
                self.maa.click(cancel)
                yield "[内番] 检测到内番符弹窗，已点取消（咱不氪这个）"
            else:
                self._click_point(cfg["skip_tap"])  # 横幅/对话：点穿
            time.sleep(1.2)
        return

    def naihanka_snapshot_stream(self):
        """读今日内番表六把刀数值存快照，和上次比对——报告屏徽章的二次确认。

        快照只记数不操作；比对出的纯涨播报时标注「数值比对」，
        因为打击/防御/冲力/机动也可能是合成喂的，不抢报告屏的功。
        """
        try:
            table = naihanka_report.read_table_stats(self.maa.ocr_all)
        except Exception:
            return
        if not table:
            return

        state = _load_naihanka_state()
        diff_gains = naihanka_report.diff_snapshots(state.get("stats"), table)
        badge_hits = {(g["name"], g["stat"])
                      for g in getattr(self, "_naihanka_gains", [])}
        extra = [gd for gd in diff_gains if (gd["name"], gd["stat"]) not in badge_hits]
        for gd in extra:
            yield (f"[内番] 📊 数值比对：{gd['name']} {gd['stat']} "
                   f"{gd['old']}→{gd['new']}（报告屏没抓到，喂满金框或合成？）")
        if extra:
            # 报告屏漏掉的 +1（金框喂满等）：如实标来源，不抢报告屏的功
            self.record_event("naihanka.gains", source="diff", gains=[
                {"name": gd["name"], "stat": gd["stat"],
                 "old": gd["old"], "new": gd["new"]} for gd in extra])

        # 耕作的生存/侦察涨值若能由旧值唯一落到完整刀账的一振，留给编队
        # 识别使用；轮换下一批内番后仍能认出这振。无法唯一认人则不记。
        try:
            from ..formation_identity import record_cultivation_growth
            from ..honmaru_profile import get_honmaru_profile
            state = record_cultivation_growth(
                state, table, get_honmaru_profile().get("candidate_pool") or {})
        except Exception:
            pass  # 内番记账是附加证据，不影响本次快照保存
        state["stats"] = table
        state["stats_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_naihanka_state(state)
        return
