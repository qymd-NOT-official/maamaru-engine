# -*- coding: utf-8 -*-
"""
上层业务：手入（修复）——黑名单版

规矩（用户亲授）：
  1. 游戏数据确认实例、血量与部队；画面文字用于定位列表行。
     游戏数据缺失时保留原来的画面读取。
  2. 黑名单 = 碰瓷队的极化太刀——他们带伤上班是故意的
     （中伤挨打触发真剑必杀），看到名字就跳过，绝对不修
  3. speedup_teams 里的部队（默认部队三=挖地队）：
     行首编号是"三之x"的，修的时候勾加速符秒修，修完继续打；
     其他队的就慢慢修，加速符省着
  4. 加速符决策权归游戏数据（JSON 事实）：数据确认轻伤及以上才烧符，
     画面伤势章只作日志旁证——章与数据不一致记日志，不改变决策。
     数据分档最细只有轻伤，hp≥90% 的"轻伤"按擦伤处理不烧符
     （老规矩"没章不烧符"的数据版）；数据缺失/过期时回到老规矩：
     没章不烧符，擦伤刀慢慢修
     （2026-08-20 实战血泪：擦伤刀白吃了三张加速符）
"""

import re
import subprocess
import time

from ..maa_adapter import roi_4to4, Point
from .. import sword_db

_TEAM_NUMERAL = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}

# 选人界面左面板「所需资源」四行（顺序固定），对应配置 repair.cost_rois
_REPAIR_RES = ("木炭", "玉钢", "冷却材", "砥石")

# 伤势章文字（混在名字列 OCR 里）；加速符只给带章的刀
_INJURY_STAMPS = ("轻伤", "中伤", "重伤")
# 章和名字的 y 配对容差（章压在头像上，比名字文字带略高）
_INJURY_PAIR_DY = 45


class RepairMixin:
    """手入。依赖宿主类的 navigate_to_stream、_click_point。"""

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
        cfg = self.config.get("repair", {})
        if not cfg:
            yield "[手入] 未配置手入"
            return

        # 黑名单：运行时覆盖 > 配置
        if blacklist is None:
            blacklist = cfg.get("blacklist", [])
        bl_ids = set()
        for zh in blacklist:
            r = sword_db.find_by_name(zh)
            if r:
                bl_ids.add(r[0])
        if use_speedup is False:
            speedup_teams = []
        elif speedup_teams is None:
            speedup_teams = cfg.get("speedup_teams", [])
        speedup_marks = [_TEAM_NUMERAL[t] + "之" for t in speedup_teams
                         if t in _TEAM_NUMERAL]

        yield f"[手入] 黑名单 {len(blacklist)} 人（解析出 {len(bl_ids)} 个ID），" \
              f"加速部队标记 {speedup_marks or '无'}"

        # ========== 1. 导航到修复 ==========
        yield "[手入] 正在导航到修复..."
        for nav_msg in self.navigate_to_stream("修复"):
            yield nav_msg
        if self.current_location != "修复":
            yield "[手入] 到达修复失败"
            return
        time.sleep(1.0)

        # 修复落地页是「修复状况」（手入槽列表），要点空闲槽才进选人界面
        if not self._open_select_screen(cfg):
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
            rows, junk = self._scan_page(cfg)
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
                hit = sword_db.find_by_name(name)
                sid = hit[0] if hit else None
                if fact:
                    sid = fact["catalog_id"]
                std_name = hit[1]["name_zh"] if hit else name
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
                ok = yield from self._repair_one(cfg, name_y, need_speed)
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
                        yield f"[手入] {std_name} 已加速修好，重新进入手入刷新编队状态..."
                        for nav_msg in self.navigate_to_stream("本丸"):
                            yield nav_msg
                        if self.current_location != "本丸":
                            yield "[手入] 加速修复后没能返回本丸，无法确认伤势已刷新，停"
                            return
                        for nav_msg in self.navigate_to_stream("修复"):
                            yield nav_msg
                        if self.current_location != "修复":
                            yield "[手入] 加速修复后没能重新进入手入，无法接回成员，停"
                            return
                        yield f"[手入] ✓ 已重新进入手入，{std_name} 的伤势状态应已刷新"
                    # 修完会退回修复状况页，重新点槽进选人界面；
                    # 列表也会重排，本页剩下的行作废重新扫
                    if not self._open_select_screen(cfg):
                        yield "[手入] 修完后没能重新打开选人界面，停"
                        return
                    break
                else:
                    if ok is None:
                        return
                    yield f"[手入] {std_name} 没能修成，跳过它继续"
            else:
                # 本页 5 行都处理完没被打断 → 翻页
                pass

            if page_new == 0:
                no_new_pages += 1
                if no_new_pages >= 2:
                    break
            else:
                no_new_pages = 0

            if dry_run:
                # 演习模式翻页继续认人
                self._scroll_list(cfg)
                time.sleep(1.0)
                continue
            # 真修模式：列表变了，直接重扫当前页，不翻页
            if page_new == 0:
                self._scroll_list(cfg)
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

    # ==================== 内部 ====================

    def _repair_game_facts(self):
        if not getattr(self.maa, "adb_address", None):
            return None
        from ..game_state import read_events, sword_facts
        try:
            return sword_facts(read_events(self.maa.adb_path, self.maa.adb_address))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _repair_row_fact(row, state):
        """A screen row locates an instance; JSON supplies HP and team membership."""
        if not state:
            return None
        from ..game_state import FACT_MAX_AGE, sword_injury
        from ..youzu_log import _int
        hit = sword_db.find_by_name(row["name"])
        if not hit:
            return None
        now = time.time()
        candidates = []
        for serial, sword in state["swords"].items():
            at, index = state["hp_times"].get(serial, (0, -1))
            found = sword_db.find_game_sword(_int(sword.get("sword_id")))
            hp, maximum = _int(sword.get("hp"), -1), _int(sword.get("hp_max"), 0)
            if (not found or found[0] != hit[0] or not 0 <= now - at <= FACT_MAX_AGE
                    or index < state["combat_index"] or at < state["combat_at"]
                    or maximum <= 0 or not 0 <= hp <= maximum):
                continue
            memberships = [(int(team), slot) for team, slots in state["parties"].items()
                           for slot, member in slots.items() if str(member) == serial
                           and 0 <= now - state["slot_times"][team][slot] <= FACT_MAX_AGE]
            candidates.append({"serial_id": int(serial), "catalog_id": found[0],
                               "hp": hp, "hp_max": maximum, "injury": sword_injury(state, serial, now=now)[0],
                               "team_no": memberships[0][0] if len(memberships) == 1 else None,
                               "memberships": memberships})
        if not candidates:
            return None
        prefix = row.get("prefix", "")
        match = re.fullmatch(r"([一二三四五])之([一二三四五六])", prefix)
        if match:
            numerals = "一二三四五六"
            position = (numerals.index(match[1]) + 1, str(numerals.index(match[2]) + 1))
            candidates = [fact for fact in candidates if position in fact["memberships"]]
        else:
            candidates = [fact for fact in candidates if fact["hp"] < fact["hp_max"]]
        return candidates[0] if len(candidates) == 1 else False

    def _open_select_screen(self, cfg) -> bool:
        """在修复状况页点空闲槽，等刀剑男士选择界面打开"""
        free_ocr = cfg["free_slot_ocr"]
        free_roi = roi_4to4(*free_ocr["roi"])
        ui_ocr = cfg["select_ui_ocr"]
        ui_roi = roi_4to4(*ui_ocr["roi"])

        free = None
        for _ in range(6):
            self.maa.screenshot(force=True)
            free = self.maa.ocr(expected=free_ocr["expected"], roi=free_roi)
            if free:
                break
            time.sleep(0.5)
        if not free:
            return False
        self.maa.click(free)
        time.sleep(1.0)

        for _ in range(10):
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected=ui_ocr["expected"], roi=ui_roi):
                time.sleep(0.5)
                return True
            time.sleep(0.5)
        return False

    def _scan_page(self, cfg) -> tuple:
        """
        整列 OCR 当前可见的行：名字列 + 编号列，按 y 坐标配对。
        翻页后行位置不对齐固定网格，所以不用固定行位，
        OCR 结果自带的中心点 y 就是行的位置。

        列里会混进伤势章文字（轻伤/中伤/重伤）和乱码，
        名字必须过 sword_db 字典验证才算数，编号必须
        符合"三之x"格式才保留——认不出的宁可跳过不乱修。
        伤势章单独收集，按 y 跟行配对进 row["injury"]；
        章只作日志旁证，加速符决策权在游戏数据
        （数据缺失/过期时才回退到章，没章不烧符）。

        Returns:
            (rows, junk) — rows: [{"prefix","name","y","injury"}]；
            junk: 被过滤的文本（伤势章不再算 junk）
        """
        # 翻页后画面变了，必须强制截图，不然 OCR 读的是旧缓存图
        self.maa.screenshot(force=True)
        names = self.maa.ocr_all(roi_4to4(*cfg["name_col_roi"]))
        prefixes = self.maa.ocr_all(roi_4to4(*cfg["prefix_col_roi"]))

        # 编号列先过滤：只留"三之x"格式的
        valid_prefixes = [
            (p.strip(), pt) for p, pt in prefixes
            if re.match(r"^[一二三四五]之[一二三四五六]$", p.strip())
        ]

        # 先收章再配行：OCR token 顺序不保证，章可能排在它所属名字的后面
        stamps, sword_names = [], []
        for name, pt in names:
            name = name.strip()
            if name in _INJURY_STAMPS:
                stamps.append((name, pt.y))
            else:
                sword_names.append((name, pt))

        rows, junk = [], []
        for name, pt in sword_names:
            # 名字至少 2 个汉字，且必须在刀剑字典里查得到
            if len(name) < 2 or not sword_db.find_by_name(name):
                junk.append(name)
                continue
            best_prefix, best_dy = "", 40
            for pfx, ppt in valid_prefixes:
                dy = abs(ppt.y - pt.y)
                if dy < best_dy:
                    best_prefix, best_dy = pfx, dy
            injury, injury_dy = None, _INJURY_PAIR_DY
            for stamp, sy in stamps:
                dy = abs(sy - pt.y)
                if dy < injury_dy:
                    injury, injury_dy = stamp, dy
            rows.append({"prefix": best_prefix, "name": name, "y": pt.y,
                         "injury": injury})
        return rows, junk

    def _scroll_list(self, cfg):
        x1, y1 = cfg["scroll_from"]
        x2, y2 = cfg["scroll_to"]
        # 滑动要快于 400ms 会被游戏吞掉，实测 800ms 才稳
        self.maa.swipe(x1, y1, x2, y2, 800)

    def _repair_one(self, cfg, name_y: int, need_speed: bool):
        """点选择 → （可选）勾加速符 → 修复开始 → 确认"""
        from ..game_state import begin_operation, confirm_operation
        sx = cfg["select_btn_x"]
        self.maa.click(Point(sx, name_y + cfg["select_offset_y"]))
        time.sleep(1.2)

        if need_speed:
            # 优先 OCR 找"使用"二字，复选框在它左边；找不到再用固定坐标
            use_ocr = cfg.get("speedup_use_ocr")
            pt = None
            if use_ocr:
                self.maa.screenshot(force=True)
                pt = self.maa.ocr(expected=use_ocr["expected"],
                                  roi=roi_4to4(*use_ocr["roi"]))
            if pt:
                self.maa.click(Point(pt.x - 33, pt.y))
            else:
                cx, cy = cfg["speedup_checkbox"]
                self.maa.click(Point(cx, cy))
            time.sleep(0.5)

        self.maa.screenshot(force=True)
        # 修复开始之前，先把左面板「所需资源」的真实成本读下来（记账用）
        costs = self._read_repair_costs(cfg)
        start = self.maa.template_match(cfg["start_button"]["template"])
        if not start:
            yield "[手入] 找不到修复开始按钮"
            return False
        boundary = begin_operation(self.maa)
        self.maa.click(start)
        time.sleep(1.2)

        # 可能有二次确认弹窗（确定/确认都试试）
        self.maa.screenshot(force=True)
        for tpl in ("通用_确定.png", "team/是.png"):
            confirm = self.maa.template_match(tpl)
            if confirm:
                self.maa.click(confirm)
                time.sleep(1.0)
                break

        confirmed = confirm_operation(self.maa, boundary, "repair",
                                      getattr(self, "_repair_target_serial", None))
        if confirmed is not True:
            yield "[手入] 未确认手入结果，本次不再重复点击" if confirmed is None else "[手入] 手入失败：游戏拒绝本次操作，未开工"
            return confirmed
        self._emit_repair_costs(costs, need_speed)
        yield "[手入] 已开工"
        return True

    def _read_repair_costs(self, cfg):
        """OCR 选人界面左面板「所需资源」四个成本数字（黑框，顺序固定）。

        返回 [木炭, 玉钢, 冷却材, 砥石]，读不到的行是 None；
        没配 cost_rois 返回 None（由 _emit_repair_costs 记 unknown 兜底）。
        """
        rois = cfg.get("cost_rois")
        if not rois:
            return None
        costs = []
        for roi in rois[:4]:
            value = None
            try:
                tokens = self.maa.ocr_all(roi_4to4(*roi))
                m = re.search(r"[\d,]+", "".join(t for t, _ in tokens))
                if m:
                    value = int(m.group(0).replace(",", ""))
            except Exception:
                value = None
            costs.append(value)
        return costs

    def _emit_repair_costs(self, costs, need_speed: bool):
        """手入开工的资源记账：四资源按确认界面 OCR 负扣（confirmed）。

        读不到的资源记 attribution=unknown、delta=null，保留「修过了」的事实。
        cost_rois 没配 / 整面读崩（costs is None）也按四条 unknown 兜底——
        静默漏账违反契约（2026-08-20 日课血泪：queued 4 条 change 0 条，
        运行中面板还没加载新配置，cost_rois 不存在）。
        need_speed 时加速符 -1 是确定事实（复选框是我们亲手勾的）。
        """
        if not hasattr(self, "record_event"):
            return
        if costs is None:
            costs = [None] * len(_REPAIR_RES)
        for name, cost in zip(_REPAIR_RES, costs):
            if cost is None:
                self.record_event(
                    "resource.change", resource=name, delta=None,
                    source="repair.confirm_screen", script="repair",
                    attribution="unknown", evidence="repair_confirm_ocr",
                    note="发生了修理消耗但数值读取失败")
            else:
                self.record_event(
                    "resource.change", resource=name, delta=-cost,
                    source="repair.confirm_screen", script="repair",
                    attribution="confirmed", evidence="repair_confirm_ocr")
        if need_speed:
            self.record_event(
                "resource.change", resource="加速符", delta=-1,
                source="repair.confirm_screen", script="repair",
                attribution="confirmed", evidence="known_recipe")
