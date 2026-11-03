# -*- coding: utf-8 -*-
"""任务奖励领取（日常/月常/活动）的公共循环（与服务器无关）。

循环只负责编排与决策：逐标签页预检 → 切页 → 认一键领取按钮状态 →
点击 → 领取结果三路仲裁（游戏结果/按钮变灰/报酬弹窗）→ 「任务信息已
失效」吞点击后的补点。领没领到必须拿到明确证据才计成绩、没证据不
计成绩这些规矩是游戏机制，两服共用。

画面识别、点击、导航、配置、弹窗读取和数据通道全是宿主画面适配层的
钩子（国服实现见 task_rewards_cn.py）；本层不碰 device、config、
OCR 词、模板名和像素坐标。
"""
from __future__ import annotations


class TaskRewardsLoopMixin:
    """任务奖励公共循环；UI 与数据触点全部是钩子。"""

    def claim_task_rewards_stream(self):
        """
        流式领取任务奖励（日常、月常，以及当前存在时的活动）
        主线不领（长线成就，不会过期）

        Yields:
            str: 执行状态消息
        """
        task_config = self._task_reward_config()
        if not task_config:
            yield "[TASK] 未配置任务领取"
            return

        # 1. 导航到任务
        yield "[TASK] 正在导航到任务..."
        reached = yield from self._task_reward_goto()
        if not reached:
            yield "[TASK] 到达任务失败"
            return

        # 2. 领取各标签页奖励
        close_config = task_config.get("popup_close")
        for tab_name, tab_coord in task_config.get("tabs", {}).items():
            # JSON 预检先行：/mission/index 确认没有「达成待领」任务时，这
            # 一页的一键领取点了也是白点（空 tab 瞎点就是这么来的）。任务行
            # 不带 tab 归属，预检给的是全局答案，只用来全拦，不替按钮模板做
            # 决定；数据缺失/过期返回 None，照旧落到下面的模板兜底。
            claimable = self._task_reward_claimable()
            if claimable is False:
                if hasattr(self, "record_event"):
                    self.record_event("task_rewards.none", tab=tab_name)
                yield f"[TASK] {tab_name} 数据确认没有可领奖励，跳过"
                continue
            # 活动标签会随运营开放/撤下，不存在时禁止点击（会落到主线）
            if not self._task_reward_tab_present(tab_name, tab_coord):
                yield "[TASK] 当前没有活动任务标签，跳过（不领取主线）"
                continue
            yield f"[TASK] 切换到 {tab_name}..."

            self._task_reward_click_tab(tab_coord)

            state, claim_point = self._task_reward_claim_button_state(task_config)

            if state == "active":
                yield f"[TASK] {tab_name} 有奖励可领，点击一键领取..."
                boundary = self._task_reward_click_claim(task_config, claim_point)

                # 弹窗还在的窗口期读报酬明细；读失败不阻断领取流程
                outcome = self._task_reward_confirm(boundary)
                popup, notes = (None, []) if outcome is not None \
                    else self._read_popup_guarded(tab_name)
                for note in notes:
                    yield note

                if close_config:
                    _, close_msgs = self._close_reward_popup(close_config)
                    for msg in close_msgs:
                        yield msg

                # 处理"任务信息已失效"弹窗。它会吞掉那次领取点击（手动实测），
                # 确认后必须补点一次一键领取，否则三次领取全丢
                # （2026-08-20 日课血泪：日常/月常/活动全部 unconfirmed）。
                if outcome is not True and self._task_reward_dismiss_stale():
                    yield "[TASK] 任务信息已失效，刷新"
                    # 点确定后任务列表会自行刷新；实机并不保证 0.5 秒内就把
                    # 一键领取按钮画回来。这里必须等页面就绪，不能只截一帧，
                    # 否则随后按钮明明恢复了，状态机却已经放弃补点。
                    retry = self._task_reward_wait_claim_button(task_config)
                    if retry:
                        yield f"[TASK] {tab_name} 补点一键领取..."
                        boundary = self._task_reward_click_claim(task_config, retry)
                        # 补点后重新走一遍报酬弹窗读取（首点被吞时没弹过）
                        outcome = self._task_reward_confirm(boundary)
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
                if outcome is None:
                    button_confirmed, late_popup = \
                        self._task_reward_wait_confirmation(
                            task_config, popup_seen=popup is not None)
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
                    outcome = self._task_reward_confirm(boundary)
                if outcome is True or outcome is None and (button_confirmed or popup_confirmed):
                    self._task_reward_record_claimed(tab_name, outcome,
                                                     popup, boundary)
                    evidence = "游戏结果" if outcome is True else "按钮变灰" if button_confirmed else "报酬弹窗"
                    yield f"[TASK] {tab_name} 领取成功，已确认{evidence}"
                else:
                    if hasattr(self, "record_event"):
                        self.record_event(
                            "task_rewards.unconfirmed", tab=tab_name,
                            stage="after_click")
                    yield (f"[TASK] {tab_name} 游戏拒绝本次领取，本次不计成绩" if outcome is False
                           else f"[TASK] {tab_name} 点击后未确认领取成功，本次不计成绩")
            elif state == "inactive":
                if hasattr(self, "record_event"):
                    self.record_event("task_rewards.none", tab=tab_name)
                yield f"[TASK] {tab_name} 已确认没有可领奖励，跳过"
            else:
                if hasattr(self, "record_event"):
                    self.record_event(
                        "task_rewards.unconfirmed", tab=tab_name,
                        stage="before_click")
                yield f"[TASK] {tab_name} 未确认一键领取按钮状态，本次不点击也不计成绩"

        # 3. 关掉任务列表窗口，别把弹窗留给下一个流程。
        #    中间步骤不点它是怕误关，收尾时必须关——日课就卡在这过。
        for msg in self._task_reward_close_list(task_config):
            yield msg

        yield "[TASK] 所有任务奖励领取完毕"
        return
