# -*- coding: utf-8 -*-
"""国服手入的画面适配与数据通道。

手入公共循环（repair_loop.py）只调用本层的钩子；国服配置素材
（config.repair 下的 OCR 词/ROI/坐标/模板）、刀剑字典查询、页面导航和
国服数据通道（游戏事实/操作确认）都收在这里。公共循环不直接访问
device、config 或像素坐标。
"""
from __future__ import annotations

import re
import subprocess
import time

from ..device_adapter import roi_4to4, Point
from .. import sword_db

_TEAM_NUMERAL = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}

# 选人界面左面板「所需资源」四行（顺序固定），对应配置 repair.cost_rois
_REPAIR_RES = ("木炭", "玉钢", "冷却材", "砥石")

# 伤势章文字（混在名字列 OCR 里）；加速符只给带章的刀
_INJURY_STAMPS = ("轻伤", "中伤", "重伤")
# 章和名字的 y 配对容差（章压在头像上，比名字文字带略高）
_INJURY_PAIR_DY = 45


class CnRepairViewMixin:
    """国服手入的画面钩子与数据通道。"""

    # ==================== 公共循环钩子 ====================

    def _repair_config(self):
        """手入用户配置（黑名单/加速部队）；未配置返回 None。"""
        cfg = self.config.get("repair", {})
        if not cfg:
            return None
        return {"blacklist": cfg.get("blacklist", []),
                "speedup_teams": cfg.get("speedup_teams", [])}

    @staticmethod
    def _repair_sword_lookup(name: str):
        """刀剑字典查询 → (图鉴 id, 标准名) 或 None。"""
        hit = sword_db.find_by_name(name)
        if not hit:
            return None
        return hit[0], hit[1]["name_zh"]

    @staticmethod
    def _repair_speedup_mark(team_no: int):
        """行首编号里的部队标记（如「三之」），认不出的部队号返回 None。"""
        numeral = _TEAM_NUMERAL.get(team_no)
        return f"{numeral}之" if numeral else None

    def _repair_goto_room(self):
        """导航到修复；到位返回 True。"""
        yield "[手入] 正在导航到修复..."
        for nav_msg in self.navigate_to_stream("修复"):
            yield nav_msg
        if self.current_location != "修复":
            yield "[手入] 到达修复失败"
            return False
        time.sleep(1.0)
        return True

    def _repair_refresh_membership(self, std_name: str):
        """加速修好后离开手入再重新进入，刷新编队伤势状态。"""
        yield f"[手入] {std_name} 已加速修好，重新进入手入刷新编队状态..."
        for nav_msg in self.navigate_to_stream("本丸"):
            yield nav_msg
        if self.current_location != "本丸":
            yield "[手入] 加速修复后没能返回本丸，无法确认伤势已刷新，停"
            return False
        for nav_msg in self.navigate_to_stream("修复"):
            yield nav_msg
        if self.current_location != "修复":
            yield "[手入] 加速修复后没能重新进入手入，无法接回成员，停"
            return False
        yield f"[手入] ✓ 已重新进入手入，{std_name} 的伤势状态应已刷新"
        return True

    def _repair_open_select_screen(self) -> bool:
        """在修复状况页点空闲槽，等刀剑男士选择界面打开"""
        cfg = self.config["repair"]
        free_ocr = cfg["free_slot_ocr"]
        free_roi = roi_4to4(*free_ocr["roi"])
        ui_ocr = cfg["select_ui_ocr"]
        ui_roi = roi_4to4(*ui_ocr["roi"])

        free = None
        for _ in range(6):
            self.device.screenshot(force=True)
            free = self.device.ocr(expected=free_ocr["expected"], roi=free_roi)
            if free:
                break
            time.sleep(0.5)
        if not free:
            return False
        self.device.click(free)
        time.sleep(1.0)

        for _ in range(10):
            self.device.screenshot(force=True)
            if self.device.ocr(expected=ui_ocr["expected"], roi=ui_roi):
                time.sleep(0.5)
                return True
            time.sleep(0.5)
        return False

    def _repair_scan_page(self) -> tuple:
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
        cfg = self.config["repair"]
        # 翻页后画面变了，必须强制截图，不然 OCR 读的是旧缓存图
        self.device.screenshot(force=True)
        names = self.device.ocr_all(roi_4to4(*cfg["name_col_roi"]))
        prefixes = self.device.ocr_all(roi_4to4(*cfg["prefix_col_roi"]))

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

    def _repair_scroll_list(self):
        cfg = self.config["repair"]
        x1, y1 = cfg["scroll_from"]
        x2, y2 = cfg["scroll_to"]
        # 滑动要快于 400ms 会被游戏吞掉，实测 800ms 才稳
        self.device.swipe(x1, y1, x2, y2, 800)

    def _repair_one(self, name_y: int, need_speed: bool):
        """点选择 → （可选）勾加速符 → 修复开始 → 确认"""
        from ..game_state import begin_operation, confirm_operation
        cfg = self.config["repair"]
        sx = cfg["select_btn_x"]
        self.device.click(Point(sx, name_y + cfg["select_offset_y"]))
        time.sleep(1.2)

        if need_speed:
            # 优先 OCR 找"使用"二字，复选框在它左边；找不到再用固定坐标
            use_ocr = cfg.get("speedup_use_ocr")
            pt = None
            if use_ocr:
                self.device.screenshot(force=True)
                pt = self.device.ocr(expected=use_ocr["expected"],
                                  roi=roi_4to4(*use_ocr["roi"]))
            if pt:
                self.device.click(Point(pt.x - 33, pt.y))
            else:
                cx, cy = cfg["speedup_checkbox"]
                self.device.click(Point(cx, cy))
            time.sleep(0.5)

        self.device.screenshot(force=True)
        # 修复开始之前，先把左面板「所需资源」的真实成本读下来（记账用）
        costs = self._read_repair_costs()
        start = self.device.template_match(cfg["start_button"]["template"])
        if not start:
            yield "[手入] 找不到修复开始按钮"
            return False
        boundary = begin_operation(self.device)
        self.device.click(start)
        time.sleep(1.2)

        # 可能有二次确认弹窗（确定/确认都试试）
        self.device.screenshot(force=True)
        for tpl in ("通用_确定.png", "team/是.png"):
            confirm = self.device.template_match(tpl)
            if confirm:
                self.device.click(confirm)
                time.sleep(1.0)
                break

        confirmed = confirm_operation(self.device, boundary, "repair",
                                      getattr(self, "_repair_target_serial", None))
        if confirmed is not True:
            yield "[手入] 未确认手入结果，本次不再重复点击" if confirmed is None else "[手入] 手入失败：游戏拒绝本次操作，未开工"
            return confirmed
        self._emit_repair_costs(costs, need_speed)
        yield "[手入] 已开工"
        return True

    def _read_repair_costs(self):
        """OCR 选人界面左面板「所需资源」四个成本数字（黑框，顺序固定）。

        返回 [木炭, 玉钢, 冷却材, 砥石]，读不到的行是 None；
        没配 cost_rois 返回 None（由 _emit_repair_costs 记 unknown 兜底）。
        """
        rois = self.config["repair"].get("cost_rois")
        if not rois:
            return None
        costs = []
        for roi in rois[:4]:
            value = None
            try:
                tokens = self.device.ocr_all(roi_4to4(*roi))
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

    # ==================== 数据通道 ====================

    def _repair_game_facts(self):
        if not getattr(self.device, "adb_address", None):
            return None
        from ..game_state import read_events, sword_facts
        try:
            return sword_facts(read_events(self.device.adb_path, self.device.adb_address))
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
