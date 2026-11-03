# -*- coding: utf-8 -*-
"""国服任务奖励领取的画面适配与数据通道。

任务奖励公共循环（task_rewards_loop.py）只调用本层的钩子；国服配置
（config.task_reward 下的标签坐标/按钮模板）、一键领取按钮模板识别、
「任务达成报酬一览」弹窗的格子几何/OCR、页面导航和国服数据通道
（/mission/index 预检、操作确认）都收在这里。公共循环不直接访问
device、config 或像素坐标。

弹窗读取的血泪校准：
  - 「任务达成报酬一览」格子几何（1280×720 实测，证据 tmp/task_probe_2.png）：
    标题顶部正中；物品格横排，首格 x=311、格距 110、格宽 104；
    图标行 y≈150~256，数量黑框行 y≈260~296（数量带千分位逗号，要洗掉）。
    物品种类和数量随标签页变，不能写死格子数，按格扫到空格为止。
  - 同源 MuMu 运行帧实测：真按钮为 1.000；蓝/灰模板互相串台约
    0.838~0.846。任务页必须越过串台分数，不能用通用的宽松门槛。
"""
from __future__ import annotations

import re
import time

from ..device_adapter import roi_4to4
from ..youzu_log import _event_epoch, _int
from ..game_state import (FACT_MAX_AGE, available_events, begin_operation,
                          confirm_operation, successful_body)

# 「任务达成报酬一览」弹窗格子几何
_POPUP_TITLE_ROI = (500, 55, 780, 85)
_POPUP_CELL_X = 311
_POPUP_CELL_PITCH = 110
_POPUP_CELL_W = 104
_POPUP_ICON_Y = (150, 256)
_POPUP_QTY_Y = (260, 296)
_POPUP_MAX_CELLS = 8
# 同源 MuMu 运行帧实测：真按钮为 1.000；蓝/灰模板互相串台约
# 0.838~0.846。任务页必须越过串台分数，不能用通用的宽松门槛。
_CLAIM_BUTTON_THRESHOLD = 0.95
_POPUP_ICONS = (("资源/icon木炭.png", "木炭"), ("资源/icon玉钢.png", "玉钢"),
                ("资源/icon冷却材.png", "冷却材"), ("资源/icon砥石.png", "砥石"),
                ("资源/icon委托符.png", "委托符"), ("资源/icon小判.png", "小判"))


def _mission_reward_claimable(events, *, now=None):
    """/mission/index 预检：还有任务停在「达成待领」就 True，没有就 False。

    任务行只有 status 字段（1=进行中 2=达成待领 3=已领取），不带 tab
    归属，所以只能回答「整个任务界面有没有可领」这个全局问题。报文缺失、
    最新一份过期或坏包返回 None，调用方照旧走按钮模板兜底。
    口径来源：2026-09-28 真实报文——领取成功的 finish_mission_ids 与领取前
    status=2 的清单完全一致，领完齐刷刷变 3。
    """
    now = time.time() if now is None else now
    for event in reversed(events):
        body = successful_body(event)
        if body is None or event.get("endpoint") != "/mission/index":
            continue
        at = _event_epoch(event)
        if at is None or not 0 <= now - at <= FACT_MAX_AGE:
            return None
        rows = body.get("mission")
        if not isinstance(rows, dict):
            return None
        return any(_int(row.get("status"), -1) == 2
                   for row in rows.values() if isinstance(row, dict))
    return None


class CnTaskRewardsViewMixin:
    """国服任务奖励的画面钩子与数据通道。"""

    # ==================== 公共循环钩子 ====================

    def _task_reward_config(self):
        """任务领取用户配置（标签坐标/按钮模板/弹窗关闭）；未配置返回空。"""
        return self.config.get("task_reward", {})

    def _task_reward_goto(self):
        """导航到任务界面；到达返回 True。"""
        for nav_msg in self.navigate_to_stream("任务"):
            yield nav_msg
        return self.current_location == "任务"

    def _task_reward_claimable(self):
        """数据通道预检：有「达成待领」True / 没有 False / 数据缺席 None。"""
        return _mission_reward_claimable(available_events(self.device))

    def _task_reward_tab_present(self, tab_name, tab_coord):
        """标签页当前是否存在。活动任务标签会随运营开放/撤下，不能永久删
        配置。点击前只在旧活动按钮自己的局部区域确认纵排“活动”；不能全屏
        搜，弹窗背后的出阵菜单也有“活动”。标签不存在时旧坐标会落到主线，
        必须禁止点击。"""
        if tab_name != "活动":
            return True
        self.device.screenshot(force=True)
        tab_x, tab_y = map(int, tab_coord)
        activity_roi = roi_4to4(
            max(0, tab_x - 28), max(0, tab_y - 70),
            min(1280, tab_x + 28), min(720, tab_y + 70))
        return bool(self.device.ocr("活动", activity_roi,
                                match_mode="contains"))

    def _task_reward_click_tab(self, tab_coord):
        self._click_point(tab_coord)
        time.sleep(0.8)

    def _task_reward_claim_button_state(self, task_config):
        """一键领取按钮状态：("active", 点) / ("inactive", None) / (None, None)。

        蓝/灰模板会互相串台，两个都用越串台的高门槛；蓝的不在才看灰的。
        """
        claim_config = task_config["claim_button"]
        self.device.screenshot(force=True)
        claim_result = self.device.template_match(
            template=claim_config["template"],
            roi=None,
            threshold=_CLAIM_BUTTON_THRESHOLD
        )
        if claim_result:
            return "active", claim_result
        inactive_template = claim_config.get(
            "inactive_template", "一键领取_灰.png")
        if self.device.template_match(
                template=inactive_template, roi=None,
                threshold=_CLAIM_BUTTON_THRESHOLD):
            return "inactive", None
        return None, None

    def _task_reward_click_claim(self, task_config, point):
        """点击一键领取并等动画；返回本次操作的数据通道边界。"""
        boundary = begin_operation(self.device)
        self.device.click(point)
        # 等待领取动画/弹窗
        time.sleep(1.5)
        return boundary

    def _task_reward_confirm(self, boundary):
        """数据通道确认本次领取：True 领成 / False 被拒 / None 无数据。"""
        return confirm_operation(self.device, boundary, "task_rewards")

    def _task_reward_dismiss_stale(self):
        """「任务信息已失效」弹窗：确认会吞掉那次领取点击。有就点掉。"""
        confirm_point = self.device.template_match("通用_确定.png", None)
        if confirm_point:
            self.device.click(confirm_point)
            return True
        return False

    def _task_reward_wait_claim_button(self, task_config):
        """失效弹窗确认后等一键领取按钮画回来；返回按钮点或 None。"""
        claim_config = task_config["claim_button"]
        for _ in range(8):
            time.sleep(0.5)
            self.device.screenshot(force=True)
            retry = self.device.template_match(
                template=claim_config["template"], roi=None,
                threshold=_CLAIM_BUTTON_THRESHOLD)
            if retry:
                return retry
        return None

    def _task_reward_wait_confirmation(self, task_config, popup_seen):
        """领取后的确认窗口：等按钮变灰或迟来的报酬弹窗。

        Returns:
            (button_confirmed, late_popup)
        """
        claim_config = task_config["claim_button"]
        inactive_template = claim_config.get(
            "inactive_template", "一键领取_灰.png")
        for _ in range(6):
            self.device.screenshot(force=True)
            still_active = self.device.template_match(
                template=claim_config["template"], roi=None,
                threshold=_CLAIM_BUTTON_THRESHOLD)
            now_inactive = self.device.template_match(
                template=inactive_template, roi=None,
                threshold=_CLAIM_BUTTON_THRESHOLD)
            if now_inactive and not still_active:
                return True, False
            if not popup_seen and self.device.exists("ui完成任务.png"):
                return False, True
            time.sleep(0.5)
        return False, False

    def _task_reward_record_claimed(self, tab_name, outcome, popup, boundary):
        """领取成功记账：先记 claimed 事件，再把奖励明细挂到账本。"""
        if not hasattr(self, "record_event"):
            return
        claimed_id = self.record_event("task_rewards.claimed", tab=tab_name)
        if outcome is True:
            from ..youzu_log import _payload_resource_rewards
            items = list(_payload_resource_rewards(boundary.get("result_body")).items())
            self._emit_reward_popup_changes(items, claimed_id, evidence="client_reward_list")
        elif popup and popup[0]:
            self._emit_reward_popup_changes(popup[0], claimed_id)

    def _task_reward_close_list(self, task_config):
        """收尾关掉任务列表窗口（✕ 右上）；返回日志消息列表。"""
        close_config = task_config.get("popup_close")
        if not close_config:
            return []
        messages = []
        for _ in range(3):
            self.device.screenshot(force=True)
            if self.device.exists(close_config.get("template")):
                self._click_template_config(close_config)
                messages.append("[TASK] 已关闭任务列表")
                time.sleep(0.8)
            else:
                break
        return messages

    # ==================== 报酬弹窗读取 ====================

    def _read_popup_guarded(self, tab_name):
        """读报酬弹窗（带护法）：读崩/没弹都不阻断领取流程。

        Returns:
            (popup, [日志消息]) — popup 为 _read_reward_popup 的返回值或 None。
        """
        try:
            popup = self._read_reward_popup()
        except Exception:
            popup = None
        notes = [f"[TASK] {tab_name} 报酬弹窗：{note}"
                 for note in (popup[1] if popup else [])]
        return popup, notes

    def _close_reward_popup(self, close_config):
        """关闭奖励弹窗。判定：必须同时有 通用_关闭.png 和 ui完成任务.png，
        防止误关任务界面本身。

        Returns:
            (是否关过弹窗, [日志消息])
        """
        messages = []
        closed = False
        for _ in range(5):
            self.device.screenshot(force=True)
            has_close = self.device.exists(close_config.get("template"))
            has_complete = self.device.exists("ui完成任务.png")

            if has_close and has_complete:
                # 是奖励弹窗，关闭
                self._click_template_config(close_config)
                messages.append("[TASK] 关闭奖励弹窗")
                closed = True
                time.sleep(0.5)
            elif has_close and not has_complete:
                # 是任务界面本身的关闭按钮，不点
                messages.append("[TASK] 检测到任务界面关闭按钮，跳过")
                break
            else:
                break
        return closed, messages

    def _read_reward_popup(self):
        """读「任务达成报酬一览」弹窗：图标模板匹配认资源，数量黑框 OCR 认数量。

        Returns:
            (items, notes) — items: [(资源名, 数量)]；notes: 被跳过格子的说明。
            标题识别不到 = 弹窗没弹（没领到），返回 None。
        """
        self.device.screenshot(force=True)
        if not self.device.ocr("报酬一览", roi_4to4(*_POPUP_TITLE_ROI),
                            match_mode="contains"):
            return None
        items, notes = [], []
        seen_resources = set()
        ambiguous_resources = set()
        unknown_frame_saved = False

        def _save_unknown_frame() -> bool:
            nonlocal unknown_frame_saved
            if unknown_frame_saved:
                return True
            save_screenshot = getattr(self.device, "save_screenshot", None)
            if not callable(save_screenshot):
                return False
            try:
                from ..runtime_paths import DEBUG_DIR
                sample = DEBUG_DIR / (
                    f"reward-unknown-{time.strftime('%Y%m%d-%H%M%S')}.png")
                unknown_frame_saved = bool(
                    save_screenshot(str(sample), force=False))
            except Exception:
                pass
            return unknown_frame_saved

        for i in range(_POPUP_MAX_CELLS):
            x = _POPUP_CELL_X + i * _POPUP_CELL_PITCH
            icon_roi = roi_4to4(x, _POPUP_ICON_Y[0],
                                x + _POPUP_CELL_W, _POPUP_ICON_Y[1])
            resource = None
            for template, name in _POPUP_ICONS:
                if self.device.template_match(template, roi=icon_roi, threshold=0.8):
                    resource = name
                    break
            qty, qty_observed = self._read_popup_qty(roi_4to4(
                x, _POPUP_QTY_Y[0], x + _POPUP_CELL_W, _POPUP_QTY_Y[1]),
                with_presence=True)
            if resource is None and qty is None and not qty_observed:
                break  # 空格 = 这一页奖励到此为止
            if resource is None:
                sample_note = ("，已留取同源运行截图"
                               if _save_unknown_frame() else "")
                qty_label = qty if qty is not None else "OCR 未读成数字"
                notes.append(
                    f"第{i + 1}格图标不认识（数量 {qty_label}）{sample_note}，跳过")
                continue
            if qty is None:
                notes.append(f"第{i + 1}格 {resource} 数量读取失败，跳过")
                continue
            if resource in ambiguous_resources:
                notes.append(f"第{i + 1}格又像{resource}，继续按不确定跳过")
                continue
            if resource in seen_resources:
                # 同一奖励弹窗通常会合并同种资源。两格都命中一个模板说明
                # 模板发生近邻类别碰撞（实测加速符会撞委托符），哪格是真货
                # 无法确认：撤掉先前那格，两个都不入账并保存同源帧。
                items = [(name, amount) for name, amount in items
                         if name != resource]
                ambiguous_resources.add(resource)
                sample_note = ("，已留取同源运行截图"
                               if _save_unknown_frame() else "")
                notes.append(
                    f"第{i + 1}格与前一格都像{resource}，疑似图标模板撞车"
                    f"{sample_note}；两格均不入账")
                continue
            seen_resources.add(resource)
            items.append((resource, qty))
        return items, notes

    def _read_popup_qty(self, roi, with_presence=False):
        """数量黑框 OCR；可同时返回框内是否出现过任何 OCR 内容。"""
        observed = False
        value = None
        try:
            tokens = self.device.ocr_all(roi)
            observed = any(str(text).strip() for text, _ in tokens)
            m = re.search(r"[\d,]+", "".join(t for t, _ in tokens))
            if m:
                value = int(m.group(0).replace(",", ""))
        except Exception:
            pass
        return (value, observed) if with_presence else value

    def _emit_reward_popup_changes(self, items, claimed_event_id, evidence="reward_popup_ocr"):
        """游戏奖励清单优先，缺数据时使用报酬弹窗；沿用既有账本来源分类。"""
        if not hasattr(self, "record_event"):
            return
        for resource, qty in items:
            payload = {"source": "task_rewards.reward_popup", "script": "task",
                       "attribution": "confirmed", "evidence": evidence}
            if isinstance(claimed_event_id, int):
                payload["source_event_id"] = claimed_event_id
            if hasattr(self, "record_resource_change"):
                self.record_resource_change(resource, qty, **payload)
            else:
                self.record_event(
                    "resource.change", resource=resource, delta=qty, **payload)
