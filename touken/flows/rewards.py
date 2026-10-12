# -*- coding: utf-8 -*-
"""
上层业务：领取类——万屋暖心礼包、任务奖励（日常/月常/活动）
（从 touken_agent_engine_v2.py 原样搬家，逻辑未改）
"""

import re
import time

from ..maa_adapter import roi_4to4
from ..game_state import begin_operation, confirm_operation

# 「任务达成报酬一览」弹窗格子几何（1280×720 实测，证据 tmp/task_probe_2.png）：
# 标题顶部正中；物品格横排，首格 x=311、格距 110、格宽 104；
# 图标行 y≈150~256，数量黑框行 y≈260~296（数量带千分位逗号，要洗掉）。
# 物品种类和数量随标签页变，不能写死格子数，按格扫到空格为止。
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


class RewardsMixin:
    """领取类流程。依赖宿主类的 navigate_to_stream、_click_point、_click_template_config。"""

    # ==================== 万屋领取 ====================

    def claim_free_gift(self) -> bool:
        """同步版本：领取暖心礼包，返回是否成功"""
        for msg in self.claim_free_gift_stream():
            print(msg)
        return "领取成功" in msg if 'msg' in dir() else False

    def claim_free_gift_stream(self):
        """
        流式版本：领取暖心礼包，每步 yield 状态消息

        Yields:
            str: 执行状态消息，前端可实时显示
        """
        shop_config = self.config.get("shop", {}).get("free_gift")
        if not shop_config:
            yield "[SHOP] 未配置暖心礼包"
            return

        # 1. 导航到万屋
        yield "[SHOP] 正在导航到万屋..."
        for nav_msg in self.navigate_to_stream("万屋"):
            yield nav_msg
        if self.current_location != "万屋":
            yield "[SHOP] 到达万屋失败"
            return

        # 2. 识别"暖心"文字
        yield "[SHOP] 寻找暖心礼包..."
        find_config = shop_config["find_text"]
        raw_rois = find_config.get("rois")
        if not raw_rois:
            # 兼容旧配置，同时把右上角活动礼包位置纳入搜索；左侧 ROI
            # 不能删，常驻礼包仍可能出现在左上或左下。
            raw_rois = [
                find_config.get("roi", [0, 100, 700, 650]),
                [700, 100, 1280, 370],
            ]
        rois = [roi_4to4(*raw_roi) for raw_roi in raw_rois]

        # 强制刷新截图
        self.maa.screenshot(force=True)

        # OCR 依次扫描左侧和右上角；找到哪个位置，后面的领取 ROI 就跟着哪个卡片走。
        result = None
        for roi in rois:
            result = self.maa.ocr(
                expected=find_config["expected"],
                roi=roi,
                match_mode=find_config.get("match_mode", "contains")
            )
            if result:
                break

        if not result:
            yield "[SHOP] 未找到暖心礼包，可能已经领过了或界面不对"
            return

        yield f"[SHOP] 找到暖心礼包 at ({result.x}, {result.y})"

        # 3. 点击领取按钮
        claim_config = shop_config["claim_button"]
        # 万屋商品会随限时礼包增减而重新排版，不能用旧的固定纵坐标找“领取”。
        # 以刚识别到的“暖心”标题为锚，只框住它所在的商品卡；范围在
        # 下一行开始前截止，绝不能误点其它商品。左右两列都按标题位置计算。
        card_left = max(0, result.x - 180)
        card_top = max(90, result.y - 55)
        card_right = min(1280, card_left + 680)
        card_bottom = min(720, card_top + 270)
        # “领取/售罄”只可能出现在商品卡右下方的按钮条。
        claim_roi = roi_4to4(
            card_left + 350, card_top + 175, card_right, card_bottom)

        # 售罄是“今天已经领过”的明确成功状态，不要再去点旧兜底坐标。
        # 旧“售罄.png”是很小的灰底白字图，0.5 阈值在商品图片和边框上也会
        # 误命中。售罄属于文字状态，只接受按钮条内 OCR 的明确结果。
        if self.maa.ocr("售罄", claim_roi, match_mode="contains"):
            yield "[SHOP] 今日暖心礼包已售罄，说明此前已经领取，跳过"
            return

        claim_result = self.maa.template_match(
            template=claim_config["template"],
            roi=claim_roi,
            threshold=0.75
        )

        if claim_result:
            yield f"[SHOP] 点击领取按钮 at ({claim_result.x}, {claim_result.y})"
            self.maa.click(claim_result)
        else:
            # 模板更新不及时还可以用文字兜底；两者都失败就不盲点。
            claim_result = self.maa.ocr("领取", claim_roi)
            if not claim_result:
                yield "[SHOP] 暖心礼包未售罄，但未识别到领取按钮，本次未点击"
                return
            yield f"[SHOP] 通过文字找到领取按钮 at ({claim_result.x}, {claim_result.y})"
            self.maa.click(claim_result)

        yield "[SHOP] 等待弹窗出现..."
        time.sleep(1.5)

        # 4. 验证弹窗：确认价格0
        yield "[SHOP] 验证弹窗价格..."
        verify_config = shop_config["popup_verify"]
        verify_roi_raw = verify_config["roi"]
        verify_roi = roi_4to4(verify_roi_raw[0], verify_roi_raw[1], verify_roi_raw[2], verify_roi_raw[3])

        # 循环等待弹窗出现
        for _ in range(10):
            self.maa.screenshot(force=True)
            verify_result = self.maa.ocr(
                expected=verify_config["expected"],
                roi=verify_roi,
                match_mode=verify_config.get("match_mode", "contains")
            )
            if verify_result:
                yield "[SHOP] 确认价格0，安全"
                break
            time.sleep(0.5)
        else:
            yield "[SHOP] 未检测到0价格弹窗，可能不是免费礼包，取消"
            # 尝试关闭弹窗
            close_config = shop_config.get("popup_close")
            if close_config:
                self._click_template_config(close_config)
            return

        # 5. 点击弹窗购买按钮（状态驱动：点完必须看到0价标签消失才算买成）
        yield "[SHOP] 点击购买按钮..."
        buy_config = shop_config["popup_buy"]
        buy_roi_raw = buy_config.get("roi", [0, 0, 1280, 720])
        buy_roi = roi_4to4(buy_roi_raw[0], buy_roi_raw[1], buy_roi_raw[2], buy_roi_raw[3])

        boundary = begin_operation(self.maa)
        bought = False
        for _ in range(3):
            buy_result = self.maa.template_match(
                template=buy_config["template"],
                roi=buy_roi,
                threshold=0.5
            )
            if not buy_result:
                # 模板认不出用文字兜底；都认不出绝不盲点固定坐标
                # （2026-09-07 起禁用盲点：点位飘了会买错东西，花的是真小判）
                buy_result = self.maa.ocr("购买", buy_roi)
                if buy_result:
                    yield (f"[SHOP] 通过文字找到购买按钮 at "
                           f"({buy_result.x}, {buy_result.y})")
            if not buy_result:
                break
            self.maa.click(buy_result)
            yield f"[SHOP] 点击购买 at ({buy_result.x}, {buy_result.y})"
            time.sleep(0.5)
            outcome = confirm_operation(self.maa, boundary, "free_gift")
            if outcome is not None:
                bought = outcome
                break
            # 0价标签连续两帧消失 = 购买被受理（单帧 OCR 抖一下不算数）
            for _ in range(8):
                time.sleep(0.5)
                self.maa.screenshot(force=True)
                if self.maa.ocr(expected=verify_config["expected"],
                                roi=verify_roi,
                                match_mode=verify_config.get("match_mode",
                                                             "contains")):
                    continue
                self.maa.screenshot(force=True)
                if not self.maa.ocr(
                        expected=verify_config["expected"], roi=verify_roi,
                        match_mode=verify_config.get("match_mode",
                                                     "contains")):
                    bought = True
                    break
            if bought:
                break
            yield "[SHOP] 购买弹窗还在，补点一次"

        if not bought:
            yield "[SHOP] 购买按钮没识别到或点了没反应，本次未购买"
            # 弹窗别留给下一个流程
            close_config = shop_config.get("popup_close")
            if close_config:
                self._click_template_config(close_config)
            return

        # 6. 关闭可能的后续弹窗
        close_config = shop_config.get("popup_close")
        if close_config:
            for _ in range(3):
                self.maa.screenshot(force=True)
                if self.maa.exists(close_config.get("template")):
                    self._click_template_config(close_config)
                    time.sleep(0.5)
                else:
                    break

        yield "[SHOP] 领取成功！"
        return

    # ==================== 任务奖励领取 ====================

    def claim_task_rewards_stream(self):
        """
        流式领取任务奖励（日常、月常，以及当前存在时的活动）
        主线不领（长线成就，不会过期）

        Yields:
            str: 执行状态消息
        """
        task_config = self.config.get("task_reward", {})
        if not task_config:
            yield "[TASK] 未配置任务领取"
            return

        # 1. 导航到任务
        yield "[TASK] 正在导航到任务..."
        for nav_msg in self.navigate_to_stream("任务"):
            yield nav_msg
        if self.current_location != "任务":
            yield "[TASK] 到达任务失败"
            return

        # 2. 领取各标签页奖励
        tabs = task_config.get("tabs", {})
        for tab_name, tab_coord in tabs.items():
            # 活动任务标签会随运营开放/撤下，不能永久删配置。点击前只在旧活动
            # 按钮自己的局部区域确认纵排“活动”；不能全屏搜，弹窗背后的出阵
            # 菜单也有“活动”。标签不存在时旧坐标会落到主线，必须禁止点击。
            if tab_name == "活动":
                self.maa.screenshot(force=True)
                tab_x, tab_y = map(int, tab_coord)
                activity_roi = roi_4to4(
                    max(0, tab_x - 28), max(0, tab_y - 70),
                    min(1280, tab_x + 28), min(720, tab_y + 70))
                if not self.maa.ocr("活动", activity_roi,
                                    match_mode="contains"):
                    yield "[TASK] 当前没有活动任务标签，跳过（不领取主线）"
                    continue
            yield f"[TASK] 切换到 {tab_name}..."

            # 点击标签
            self._click_point(tab_coord)
            time.sleep(0.8)

            # 检查一键领取按钮
            claim_config = task_config["claim_button"]
            self.maa.screenshot(force=True)

            claim_result = self.maa.template_match(
                template=claim_config["template"],
                roi=None,
                threshold=_CLAIM_BUTTON_THRESHOLD
            )
            inactive_template = claim_config.get(
                "inactive_template", "一键领取_灰.png")
            inactive_result = None
            if not claim_result:
                inactive_result = self.maa.template_match(
                    template=inactive_template, roi=None,
                    threshold=_CLAIM_BUTTON_THRESHOLD)

            if claim_result:
                yield f"[TASK] {tab_name} 有奖励可领，点击一键领取..."
                boundary = begin_operation(self.maa)
                self.maa.click(claim_result)

                # 等待领取动画/弹窗
                time.sleep(1.5)

                # 弹窗还在的窗口期读报酬明细；读失败不阻断领取流程
                outcome = confirm_operation(self.maa, boundary, "task_rewards")
                popup, notes = (None, []) if outcome is not None else self._read_popup_guarded(tab_name)
                for note in notes:
                    yield note

                close_config = task_config.get("popup_close")
                if close_config:
                    _, close_msgs = self._close_reward_popup(close_config)
                    for msg in close_msgs:
                        yield msg

                # 处理"任务信息已失效"弹窗。它会吞掉那次领取点击（手动实测），
                # 确认后必须补点一次一键领取，否则三次领取全丢
                # （2026-08-20 日课血泪：日常/月常/活动全部 unconfirmed）。
                confirm_point = self.maa.template_match("通用_确定.png", None)
                if confirm_point and outcome is not True:
                    self.maa.click(confirm_point)
                    yield "[TASK] 任务信息已失效，刷新"
                    # 点确定后任务列表会自行刷新；实机并不保证 0.5 秒内就把
                    # 一键领取按钮画回来。这里必须等页面就绪，不能只截一帧，
                    # 否则随后按钮明明恢复了，状态机却已经放弃补点。
                    retry = None
                    for _ in range(8):
                        time.sleep(0.5)
                        self.maa.screenshot(force=True)
                        retry = self.maa.template_match(
                            template=claim_config["template"], roi=None,
                            threshold=_CLAIM_BUTTON_THRESHOLD)
                        if retry:
                            break
                    if retry:
                        yield f"[TASK] {tab_name} 补点一键领取..."
                        boundary = begin_operation(self.maa)
                        self.maa.click(retry)
                        time.sleep(1.5)
                        # 补点后重新走一遍报酬弹窗读取（首点被吞时没弹过）
                        outcome = confirm_operation(self.maa, boundary, "task_rewards")
                        if popup is None and outcome is None:
                            popup, notes = self._read_popup_guarded(tab_name)
                            for note in notes:
                                yield note
                        if close_config:
                            _, close_msgs = self._close_reward_popup(close_config)
                            for msg in close_msgs:
                                yield msg
                    else:
                        yield f"[TASK] {tab_name} 失效弹窗后没找到一键领取按钮，无法补点"

                # 优先等按钮刷新为灰态；报酬一览弹窗本身也是领取成功的直接证据。
                # 两者都没有才算未确认，避免“刚关奖励弹窗却说没领到”的假阴性。
                # 弹窗可能来得慢（实测日常 tab 点了 5 秒才弹），等待窗口内也要盯弹窗，
                # 否则弹窗晚于首次读取 = 领到了却被记成未确认（2026-08-22 安装版实测）。
                button_confirmed = False
                late_popup = False
                for _ in range(6) if outcome is None else ():
                    self.maa.screenshot(force=True)
                    still_active = self.maa.template_match(
                        template=claim_config["template"], roi=None,
                        threshold=_CLAIM_BUTTON_THRESHOLD)
                    now_inactive = self.maa.template_match(
                        template=inactive_template, roi=None,
                        threshold=_CLAIM_BUTTON_THRESHOLD)
                    if now_inactive and not still_active:
                        button_confirmed = True
                        break
                    if popup is None and self.maa.exists("ui完成任务.png"):
                        late_popup = True
                        break
                    time.sleep(0.5)
                if late_popup:
                    popup, notes = self._read_popup_guarded(tab_name)
                    for note in notes:
                        yield note
                    if close_config:
                        _, close_msgs = self._close_reward_popup(close_config)
                        for msg in close_msgs:
                            yield msg
                popup_confirmed = popup is not None or late_popup
                if outcome is None:
                    outcome = confirm_operation(self.maa, boundary, "task_rewards")
                if outcome is True or outcome is None and (button_confirmed or popup_confirmed):
                    if hasattr(self, "record_event"):
                        claimed_id = self.record_event("task_rewards.claimed",
                                                       tab=tab_name)
                        if outcome is True:
                            from ..youzu_log import _payload_resource_rewards
                            items = list(_payload_resource_rewards(boundary.get("result_body")).items())
                            self._emit_reward_popup_changes(items, claimed_id, evidence="client_reward_list")
                        elif popup and popup[0]:
                            self._emit_reward_popup_changes(popup[0], claimed_id)
                    evidence = "游戏结果" if outcome is True else "按钮变灰" if button_confirmed else "报酬弹窗"
                    yield f"[TASK] {tab_name} 领取成功，已确认{evidence}"
                else:
                    if hasattr(self, "record_event"):
                        self.record_event(
                            "task_rewards.unconfirmed", tab=tab_name,
                            stage="after_click")
                    yield (f"[TASK] {tab_name} 游戏拒绝本次领取，本次不计成绩" if outcome is False
                           else f"[TASK] {tab_name} 点击后未确认领取成功，本次不计成绩")
            elif inactive_result:
                if hasattr(self, "record_event"):
                    self.record_event("task_rewards.none", tab=tab_name)
                yield f"[TASK] {tab_name} 已确认没有可领奖励，跳过"
            else:
                if hasattr(self, "record_event"):
                    self.record_event(
                        "task_rewards.unconfirmed", tab=tab_name,
                        stage="before_click")
                yield f"[TASK] {tab_name} 未确认一键领取按钮状态，本次不点击也不计成绩"

        # 3. 关掉任务列表窗口（✕ 右上），别把弹窗留给下一个流程。
        #    中间步骤不点它是怕误关，收尾时必须关——日课就卡在这过。
        close_config = task_config.get("popup_close")
        if close_config:
            for _ in range(3):
                self.maa.screenshot(force=True)
                if self.maa.exists(close_config.get("template")):
                    self._click_template_config(close_config)
                    yield "[TASK] 已关闭任务列表"
                    time.sleep(0.8)
                else:
                    break

        yield "[TASK] 所有任务奖励领取完毕"
        return

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
            self.maa.screenshot(force=True)
            has_close = self.maa.exists(close_config.get("template"))
            has_complete = self.maa.exists("ui完成任务.png")

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
        self.maa.screenshot(force=True)
        if not self.maa.ocr("报酬一览", roi_4to4(*_POPUP_TITLE_ROI),
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
            save_screenshot = getattr(self.maa, "save_screenshot", None)
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
                if self.maa.template_match(template, roi=icon_roi, threshold=0.8):
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
            tokens = self.maa.ocr_all(roi)
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
