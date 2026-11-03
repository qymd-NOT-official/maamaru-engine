# -*- coding: utf-8 -*-
"""远征收取的公共循环（与服务器无关）。

循环只负责编排：触发归来动画 → 循环认结算屏/归来横幅/本丸并翻页 →
数据通道确认收据 → 顺手再派。画面识别、点击、配置素材和数据通道全是
宿主画面适配层的钩子（国服实现见 expedition_collect_cn.py），本层不碰
device、config、模板、OCR 词和像素坐标；钩子给不出答案就显式停止，
不借任何服务器默认值放行。
"""
from __future__ import annotations

import time


class ExpeditionCollectMixin:
    """远征收取公共循环；UI 与数据触点全部是钩子。"""

    def collect_expedition_stream(self, redispatch=None):
        """
        流式收菜：领取远征完成的奖励

        原理（用户亲授）：远征完成后，【重新进入本丸场景】会自动播放
        「远征归来」横幅 → 点一下进「远征结果」结算 → 再点翻页，
        多支队伍同时回来就多张结算连翻，翻完回到本丸。

        所以流程：远征界面 → 回本丸（强制换场景触发动画）→ 循环
        识别横幅/结算并点过，直到确认站在本丸且无事发生。
        识别与点击走 _collect_* 钩子，由各服画面适配实现。

        Args:
            redispatch: 收完顺手再派的模式
                None   = 只收不派（默认）
                "same" = 哪支队回来就把哪支队派回原来那张图
                资源名  = "小判"/"加速符"/"委托符"/"木炭"/"玉钢"/"冷却材"/"砥石"
                          回来的队全部派去该资源时薪最高的图
                          （收益表由 _pick_map_for_goal 钩子所在服提供）

        Yields:
            str: 执行状态消息
        """
        gate_error = self._collect_gate()
        if gate_error:
            yield gate_error
            return

        baseline = self._collection_facts(0) or []
        excluded = {receipt["payload"]["receipt_key"] for receipt in baseline}
        started_at = int(time.time())
        screen_unresolved = False

        # ========== 1. 换场景回本丸，触发归来动画 ==========
        reached = yield from self._collect_trigger_return_animation()
        if not reached:
            return

        # ========== 2. 收菜循环 ==========
        collected = 0        # 点掉的结算界面的数量
        settlements = []     # 每份结算的归来信息（第几部队、哪张图）
        home_streak = 0      # 连续看到本丸的次数
        unknown_streak = 0   # 连续认不出画面的次数
        settle_seen = []     # 本轮已记账结算屏的像素指纹（同一屏没翻动不重复记）
        time.sleep(2.0)      # 等归来动画冒出来

        for _ in range(30):  # 安全上限
            self._collect_refresh_frame()

            # 结算界面 → 观察哨读账记账（认不出记 unknown），再点一下翻页
            obs = None
            if self._collect_at_settlement():
                obs = self.observe_expedition_settlement(
                    via="collect", seen=settle_seen, sequence=collected + 1, record=False)
            if obs is not None:
                if not obs["new"]:
                    # 同一屏没翻动（转场慢半拍），再点一下，不重复记账
                    self._collect_tap()
                    time.sleep(1.2)
                    continue
                collected += 1
                settlements.append(obs)
                yield f"[收菜] 已翻过第{collected}份远征结算"
                self._collect_tap()
                home_streak = 0
                unknown_streak = 0
                time.sleep(1.2)
                continue

            # 归来横幅 → 点一下进结算
            if self._collect_at_banner():
                yield "[收菜] 远征归来横幅，点过"
                self._collect_tap()
                home_streak = 0
                unknown_streak = 0
                time.sleep(1.2)
                continue

            # 确认站在本丸：连续两次看到且无横幅/结算，收工
            if self._collect_at_home():
                home_streak += 1
                if home_streak >= 2:
                    break
                time.sleep(1.0)
                continue

            # 认不出的画面（过场动画途中等）→ 点一下安全区帮忙快进
            home_streak = 0
            unknown_streak += 1
            if unknown_streak > 5:
                screen_unresolved = True
                break
            self._collect_tap()
            time.sleep(1.0)
        else:
            screen_unresolved = True

        receipts = self._collection_facts(started_at, excluded)
        if receipts:
            settlements = [{**receipt["payload"], "map_name": None} for receipt in receipts]
            collected = len(settlements)
            self._forget_collected_records(receipts)
            yield f"[收菜] 游戏已确认 {collected} 份结算的部队、地图和奖励"
            if getattr(self, "_collection_sync_failed", False):
                yield "[收菜] 账房同步未完成，可稍后同步近况；本次结算已确认"
        else:
            for sequence, obs in enumerate(settlements, 1):
                self._record_visual_settlement(obs, sequence)
        if screen_unresolved:
            yield "[收菜] ⚠️ 当前页面未确认回到本丸，结算记录已保留，暂不再派"
            return

        if collected > 0:
            yield f"[收菜] ✅ 收了 {collected} 份远征奖励"
        else:
            yield "[收菜] ✓ 没有远征回来，本丸风平浪静～"
            return

        # ========== 3. 顺手再派 ==========
        if not redispatch or not settlements:
            return

        if redispatch != "same":
            pick = self._pick_map_for_goal(redispatch)
            if not pick:
                yield f"[收菜] 收益表里没有产「{redispatch}」的图，再派跳过"
                return
            goal_code, goal_era, goal_slot = pick
            yield f"[收菜] 目标「{redispatch}」时薪最高的图：{goal_code}"

        dispatched = set()
        for info in settlements:
            team = info["team_no"]
            if not team or team in dispatched:
                continue
            dispatched.add(team)

            if redispatch == "same":
                era, slot, name = info["era"], info["slot"], info["map_name"]
                if not era or (not slot and not name):
                    yield f"[收菜] 部队{team}的归来信息没读全，这队跳过"
                    continue
                yield f"[收菜] 顺手把部队{team}派回原图（时代{era}卡位{slot}）..."
            else:
                era, slot, name = goal_era, goal_slot, None
                yield f"[收菜] 把部队{team}派去 {goal_code}（时代{era}卡位{slot}）..."

            for dis_msg in self.expedition_stream(era=era, map_name=name,
                                                  team_no=team, map_slot=slot):
                yield dis_msg

        yield f"[收菜] 再派完毕，共处理 {len(dispatched)} 支队"
