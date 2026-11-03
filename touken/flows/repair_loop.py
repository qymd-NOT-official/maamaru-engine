# -*- coding: utf-8 -*-
"""手入（修复）的公共循环（与服务器无关）。

循环只负责编排与决策：扫描行 → 事实对号入座 → 黑名单/加速符判定 →
修完重扫 → 翻页收尾。黑名单跳过、擦伤守卫（数据轻伤但 hp≥90% 按擦伤）、
章与数据不一致仲裁这些规矩是游戏机制，两服共用。

画面识别、点击、滑动、导航、配置素材、刀剑字典和数据通道全是宿主画面
适配层的钩子（国服实现见 repair_cn.py）；本层不碰 device、config、
OCR 词和像素坐标。钩子给不出答案就显式跳过/停止，不借默认值放行。
"""
from __future__ import annotations

import time


class RepairLoopMixin:
    """手入公共循环；UI 与数据触点全部是钩子。"""

    def repair_stream(self, dry_run: bool = False, blacklist: list = None,
                      use_speedup: bool = None, speedup_teams: list = None):
        """
        流式手入：扫描修复列表，黑名单跳过，其余修掉

        Args:
            dry_run: True=只扫描报决策，一个按钮都不点（认人考试模式）
            blacklist: 运行时覆盖黑名单，None 则读配置
            speedup_teams: 运行时覆盖使用加速符的部队，None 则读配置

        Yields:
            str: 执行状态消息
        """
        # 给连续出阵等上层流程读取；每次调用都重新开始统计。
        self.last_repair_stats = {"repaired": 0, "speedups": 0, "skipped": 0, "unresolved": 0}
        rcfg = self._repair_config()
        if rcfg is None:
            yield "[手入] 未配置手入"
            return

        # 黑名单：运行时覆盖 > 配置
        if blacklist is None:
            blacklist = rcfg["blacklist"]
        bl_ids = set()
        for zh in blacklist:
            hit = self._repair_sword_lookup(zh)
            if hit:
                bl_ids.add(hit[0])
        if use_speedup is False:
            speedup_teams = []
        elif speedup_teams is None:
            speedup_teams = rcfg["speedup_teams"]
        speedup_marks = [mark for mark in
                         (self._repair_speedup_mark(t) for t in speedup_teams)
                         if mark]

        yield f"[手入] 黑名单 {len(blacklist)} 人（解析出 {len(bl_ids)} 个ID），" \
              f"加速部队标记 {speedup_marks or '无'}"

        # ========== 1. 导航到修复 ==========
        reached = yield from self._repair_goto_room()
        if not reached:
            return

        # 修复落地页是「修复状况」（手入槽列表），要点空闲槽才进选人界面
        if not self._repair_open_select_screen():
            yield "[手入] 没有空闲修复槽（都在修？）或选人界面没打开，停"
            return

        # ========== 2. 逐页扫描 ==========
        seen = set()          # 扫过的行（编号+名字），翻页去重
        repaired = 0
        skipped = 0
        no_new_pages = 0

        for page in range(10):  # 安全上限
            page_new = 0
            facts = self._repair_game_facts()
            rows, junk = self._repair_scan_page()
            if junk:
                yield f"[手入] （本页过滤掉的乱码：{junk}）"
            for row in rows:
                prefix, name, name_y = row["prefix"], row["name"], row["y"]
                stamp = row.get("injury")  # OCR 伤势章：旁证，决策权在游戏数据
                fact = self._repair_row_fact(row, facts)
                if fact is False:
                    unresolved_key = f"unresolved:{prefix}|{name}|{name_y}"
                    if unresolved_key in seen:
                        continue
                    seen.add(unresolved_key)
                    page_new += 1
                    self.last_repair_stats["unresolved"] += 1
                    yield f"[手入] {prefix} {name} 的实例无法对应，跳过这一行"
                    continue
                serial = fact.get("serial_id") if fact else None
                if fact:
                    hp, hp_max = fact["hp"], fact["hp_max"]
                    if hp == hp_max:
                        continue
                    # 决策权全归 JSON 事实：章与数据不一致只记旁证日志，不改决策
                    injury = fact["injury"]
                    # 擦伤守卫：数据分档最细只有轻伤，44/45 这种蹭破皮过去
                    # 靠"画面没章"省下加速符；决策改吃数据后这条省钱规矩不变——
                    # hp≥90% 的轻伤按擦伤处理，不烧符
                    if injury == "轻伤" and hp * 10 >= hp_max * 9:
                        injury = None
                    if stamp != injury:
                        yield (f"[手入] ⚠ {prefix} {name}：画面伤势章"
                               f"（{stamp or '没读到'}）与游戏数据"
                               f"（{injury or '擦伤'}）不一致，"
                               f"按游戏数据决策")
                else:
                    # 游戏数据缺失/过期：保留画面兜底，没章不烧符的规矩不变
                    injury = stamp

                key = f"serial:{serial}" if serial else f"{prefix}|{name}"
                if key in seen:
                    continue
                seen.add(key)
                page_new += 1

                # ---- 决策 ----
                hit = self._repair_sword_lookup(name)
                sid = hit[0] if hit else None
                if fact:
                    sid = fact["catalog_id"]
                std_name = hit[1] if hit else name
                is_black = (sid in bl_ids) or any(b in name for b in blacklist)
                # 伤势以游戏数据为准（缺失时才是画面章兜底）；加速部队的
                # 擦伤刀也慢修，十几个资源几分钟的事，不配烧一张符（老大原话精神）
                in_speed_team = (fact["team_no"] in speedup_teams if fact
                                 else any(mark in prefix for mark in speedup_marks))
                need_speed = in_speed_team and bool(injury)

                if is_black:
                    skipped += 1
                    self.last_repair_stats["skipped"] = skipped
                    if hasattr(self, "record_event"):
                        self.record_event("repair.skipped", name=std_name,
                                          sword_id=sid, team_mark=prefix,
                                          reason="blacklist")
                    yield f"[手入] {prefix} {std_name} → 黑名单（碰瓷队带伤上班），跳过"
                    continue

                if need_speed:
                    tag = f"修+加速符（{injury}）"
                elif in_speed_team:
                    tag = "修（擦伤没到轻伤，省一张加速符）"
                else:
                    tag = "修（不用加速符）"
                if dry_run:
                    yield f"[手入] {prefix} {std_name} → {tag} [演习]"
                    continue

                # ---- 真修 ----
                yield f"[手入] {prefix} {std_name} → {tag}"
                self._repair_target_serial = serial
                ok = yield from self._repair_one(name_y, need_speed)
                if ok is True:
                    repaired += 1
                    self.last_repair_stats["repaired"] = repaired
                    if need_speed:
                        self.last_repair_stats["speedups"] += 1
                    if hasattr(self, "record_event"):
                        self.record_event("repair.queued", name=std_name,
                                          sword_id=sid, team_mark=prefix,
                                          serial_id=serial,
                                          speedup=need_speed, injury=injury)
                    if need_speed:
                        # 游戏的奇怪缓存：加速手入已经完成，但如果立刻去编队，
                        # 伤势章仍可能残留。必须离开手入再重新进入一次，等价于
                        # 把人从手入室“接出来”，编队状态才会刷新。
                        refreshed = yield from self._repair_refresh_membership(std_name)
                        if not refreshed:
                            return
                    # 修完会退回修复状况页，重新点槽进选人界面；
                    # 列表也会重排，本页剩下的行作废重新扫
                    if not self._repair_open_select_screen():
                        yield "[手入] 修完后没能重新打开选人界面，停"
                        return
                    break
                else:
                    if ok is None:
                        return
                    yield f"[手入] {std_name} 没能修成，跳过它继续"

            if page_new == 0:
                no_new_pages += 1
                if no_new_pages >= 2:
                    break
            else:
                no_new_pages = 0

            if dry_run:
                # 演习模式翻页继续认人
                self._repair_scroll_list()
                time.sleep(1.0)
                continue
            # 真修模式：列表变了，直接重扫当前页，不翻页
            if page_new == 0:
                self._repair_scroll_list()
                time.sleep(1.0)

        if dry_run:
            yield f"[手入] 演习结束：共认出 {len(seen)} 把待修刀，" \
                  f"黑名单跳过 {skipped} 把"
        elif self.last_repair_stats["unresolved"]:
            yield (f"[手入] ✗ 修了 {repaired} 把，另有 "
                   f"{self.last_repair_stats['unresolved']} 行未能确认身份，已跳过")
        else:
            yield f"[手入] ✅ 完事：修了 {repaired} 把，黑名单跳过 {skipped} 把"
        return
