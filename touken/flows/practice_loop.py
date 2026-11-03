# -*- coding: utf-8 -*-
"""演练（演习场）的公共循环（与服务器无关）。

循环只负责编排与决策：导航 → 已有胜场统计 → 普刀队长优先的扫描顺序 →
逐个认对手 → _judge 判定 → 软弱评分排序 → 挑软的捏。丙子椒林剑硬躲、
短刀机动读不出当极短、不满编是好心人这些规矩是游戏机制，两服共用；
刀种名「短刀」等汉字与游戏内部 ID 两服通用，留在本层。

画面识别、点击、导航、配置和数据通道全是宿主画面适配层的钩子（国服
实现见 practice_cn.py）；本层不碰 device、config、OCR 词和像素坐标。
"""
from __future__ import annotations

_HEISHI_ID = "touken_208_heishi_shourinken"  # 丙子椒林剑


class PracticeLoopMixin:
    """演练公共循环；UI 与数据触点全部是钩子。"""

    def practice_stream(self, dry_run: bool = True, max_wins: int = None,
                        team_no: int = None, formation_mode: str = None,
                        formation: str = None):
        """
        流式演练：扫描 5 个对手，逐个读取阵容，挑软柿子打

        Args:
            dry_run: True=只认人报决策，不动手（认人考试模式）
            max_wins: 赢够几场收工，默认读配置 practice.max_wins（3）
            team_no: 用部队几打，默认读配置 practice.team_no（2）

        Yields:
            str: 执行状态消息
        """
        vcfg = self._practice_view_config(team_no, formation_mode, formation)
        if max_wins is None:
            max_wins = vcfg.get("max_wins", 3)

        yield f"[演练] {'认人演习' if dry_run else '真打'} 模式，目标赢 {max_wins} 场"

        # ========== 1. 导航到演练 ==========
        yield "[演练] 正在导航到演练..."
        reached = yield from self._practice_goto()
        if not reached:
            yield "[演练] 到达演练失败"
            return

        # 游戏会把本轮已经打过的结果章一直留在五名对手的行末。它比本地
        # 时间/进程记录更可靠：3 点、15 点换一批对手时，章也会自然清空。
        # 先把已有胜场算进去，避免同一刷新周期内二次触发又从 0 开始打。
        self._practice_rows = self._practice_fetch_rows()
        existing_results = self._scan_existing_results()
        existing_wins = sum(result.startswith("win")
                            for result in existing_results.values())
        if existing_results:
            yield (f"[演练] 当前刷新场已打 {len(existing_results)} 场，"
                   f"其中胜利 {existing_wins} 场")
        if not dry_run and existing_wins >= max_wins:
            yield f"[演练] 已有胜场 {existing_wins}/{max_wins}，无需重复挑战，收工"
            return

        # ========== 2. 列表扫描：樱花标记的队长靠后看 ==========
        order = self._scan_list_order()
        yield f"[演练] 扫描顺序（普刀队长优先）: {[i + 1 for i in order]}"

        # ========== 3. 先看完所有对手，再从弱到强挑选 ==========
        candidates = []
        for row_idx in order:
            opponent = self._inspect_opponent(row_idx)
            if opponent is None:
                yield f"[演练] 对手{row_idx + 1}: 进情报界面失败，跳过"
                continue

            members = opponent["members"]
            verdict, reasons = self._judge(members)
            roster = "、".join(
                f"{m['name']}({m['type']} Lv.{m.get('level') or '?'}"
                f"{' 机动' + str(m['mobility']) if m.get('mobility') else ''})"
                for m in members
            ) or "（空）"
            yield f"[演练] 对手{row_idx + 1} [{opponent['title']}]: {verdict} —— {'；'.join(reasons)}"
            if opponent.get("source") == "screen":
                yield f"[演练]   画面读取（未取得本次完整阵容，空位尚未确认）: {roster}"
            else:
                yield f"[演练]   阵容({len(members)}/6): {roster}"

            if verdict == "打":
                score = self._strength_key(members)
                candidates.append((score, row_idx, opponent))
                yield f"[演练]   软弱评分：{self._score_text(score)}"
            self._back_to_list()

        candidates.sort(key=lambda item: item[0])
        yield "[演练] 推荐挑战顺序：" + (", ".join(
            f"对手{row + 1}({self._score_text(score)})"
            for score, row, _ in candidates
        ) or "没有可安全识别的对手")

        if dry_run:
            yield f"[演练] 收工：候选 {len(candidates)} 队（演习模式，未真打）"
            return

        wins = existing_wins
        new_wins = 0
        for _, row_idx, _ in candidates:
            if wins >= max_wins:
                break
            # 扫描阶段已经退回列表；开战前重新进入该对手情报页。
            current = self._inspect_opponent(row_idx)
            if current is None:
                yield f"[演练] 对手{row_idx + 1}: 再次进入失败，跳过"
                continue
            if self._judge(current["members"])[0] != "打":
                yield f"[演练] 对手{row_idx + 1}: 当前阵容不适合挑战，跳过"
                self._back_to_list()
                continue
            result = yield from self._fight_one(vcfg, row_idx)
            won = result.startswith("win")
            if hasattr(self, "record_event"):
                self.record_event("practice.result", result=result,
                                  won=won, wins=wins + int(won),
                                  target=max_wins, dry_run=dry_run)
            if won:
                wins += 1
                new_wins += 1
                yield f"[演练] 胜场 {wins}/{max_wins}（{result}）"
            else:
                yield f"[演练] 这场结果: {result}"
            # 打完回到列表（结算后一般自动回，保险起见导航校验）
            if not self._ensure_on_list():
                yield "[演练] 打完没回到对手列表，重新导航"
                yield from self._practice_goto()

        yield (f"[演练] 收工：本次新赢 {new_wins} 场，当前刷新场累计 {wins} 场"
               + ("（演习模式，未真打）" if dry_run else ""))

    # ========== 决策 ==========

    def _judge(self, members: list):
        """
        返回 (verdict, reasons)，verdict ∈ {"打", "躲"}
        """
        if not members:
            return "躲", ["一个人都没认出来，怂"]

        dodge = []
        for m in members:
            if m["id"] == _HEISHI_ID:
                dodge.append(f"有丙子椒林剑（第1出手无效，无解）")
            if m["type"] == "短刀":
                mob = m.get("mobility")
                if mob is None and "extreme" not in m:
                    dodge.append(f"{m['name']}是短刀但机动读不出，当极短处理")
            if m["type"] == "未知":
                dodge.append(f"{m['name']}身份未确认")

        if dodge:
            return "躲", list(dict.fromkeys(dodge))

        reasons = []
        if len(members) < 6:
            reasons.append(f"只配了{len(members)}人，好心人")
        else:
            extreme_count = sum(
                1 for m in members
                if m.get("type") == "短刀" and m.get("extreme", (m.get("mobility") or 0) >= 100)
            )
            reasons.append(
                f"满编队，检测到{extreme_count}把极短，加入候选后统一比强度"
                if extreme_count else "满编但无极短无丙子，可以打"
            )
        return "打", reasons

    def _strength_key(self, members: list):
        """数值越小越值得打：人数 → 极短威胁 → 全队平均等级。"""
        levels = [m["level"] for m in members if m.get("level") is not None]
        average_level = (sum(levels) / len(levels)) if levels else 999.0
        extreme = [m for m in members
                   if m.get("type") == "短刀" and m.get("extreme", (m.get("mobility") or 0) >= 100)]
        strongest_mobility = max((m.get("mobility") or 999 for m in extreme), default=0)
        strongest_level = max((m.get("level") or 999 for m in extreme), default=0)
        return (len(members), len(extreme), strongest_mobility,
                strongest_level, round(average_level, 1))

    @staticmethod
    def _score_text(score) -> str:
        count, extreme_count, mobility, level, average = score
        extreme = (f"极短{extreme_count}把/最高机动{mobility}/最高Lv.{level}"
                   if extreme_count else "无极短")
        average_text = "等级读取不足" if average >= 999 else f"平均Lv.{average:g}"
        return f"{count}人，{extreme}，{average_text}"
