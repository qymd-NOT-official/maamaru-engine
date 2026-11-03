# -*- coding: utf-8 -*-
"""国服演练的画面适配与数据通道。

演练公共循环（practice_loop.py）只调用本层的钩子；国服配置合并、
页面导航、对手列表/情报界面的 OCR 与像素坐标、刀剑字典查询和国服
数据通道（游戏事实/操作确认）都收在这里。公共循环不直接访问
device、config 或像素坐标。

画面读取的血泪校准：
  - 数值条每格 55.7px 宽，机动是第 4 格 x[754,806]——窗口偏 1px 都会
    把隔壁防御的尾数读进来，别改
  - 图标中心行距 94px，第一行 cy=152
"""
from __future__ import annotations

import re
import time

from ..device_adapter import roi_4to4, Point
from .. import sword_db
from ..game_state import available_events, begin_operation, practice_list, practice_rank, practice_receipt, practice_members

# 情报界面 6 个成员行的图标中心 y
_ROW_CY = [152, 246, 340, 434, 528, 622]
# 对手列表 5 行的队长卡点击点 y（真机校准：行距 112，不是 135！）
_LIST_CY = [179, 290, 402, 514, 626]

# 刀种模板（内芯裁剪版，resource/base/image/刀种2/）
_ICON_TEMPLATES = ["短_银", "打_银", "太_金"]

_CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}


class CnPracticeViewMixin:
    """国服演练的画面钩子与数据通道。"""

    # ==================== 公共循环钩子 ====================

    def _practice_view_config(self, team_no=None, formation_mode=None, formation=None):
        """合并后的演练配置：运行时覆盖 > 用户配置 practice.*。"""
        cfg = self.config.get("practice", {})
        if team_no is not None:
            cfg = dict(cfg)
            cfg["team_no"] = team_no
        if any(v is not None for v in (formation_mode, formation)):
            cfg = dict(cfg)
            if formation_mode is not None:
                cfg["formation_mode"] = formation_mode
            if formation is not None:
                cfg["formation"] = formation
        return cfg

    def _practice_goto(self):
        """导航到演练对手列表并刷新截图；到达返回 True。"""
        for nav_msg in self.navigate_to_stream("演练"):
            yield nav_msg
        if self.current_location != "演练":
            return False
        time.sleep(1.0)
        self.device.screenshot(force=True)
        return True

    def _practice_fetch_rows(self):
        """本轮五名对手的数据通道行（无数据时返回 None，走画面回退）。"""
        return practice_list(available_events(self.device))

    # ========== 列表层 ==========

    def _scan_existing_results(self):
        """读取五行现存结算章，返回 ``{行号: win/lose}``。

        这里只认明确的“胜利/败北”文字；OCR 不清时宁可不计，不能把一个
        仍可挑战的对手误当成已经获胜。
        """
        rows = getattr(self, "_practice_rows", None)
        results = {}
        for i, cy in enumerate(_LIST_CY):
            if rows is not None:
                rank = rows[i]["rank"]
                if str(rank) == "0":
                    continue
                known = practice_rank(rank)
                if known is not None:
                    results[i] = known
                    continue
            tokens = self.device.ocr_all(roi_4to4(1050, cy - 40, 1280, cy + 40))
            text = "".join(t for t, _ in tokens)
            if "胜利" in text:
                results[i] = "win"
            elif "败北" in text:
                results[i] = "lose"
        return results

    def _scan_list_order(self):
        """返回扫描顺序（行号列表）：没打过的行才扫，队长没樱花的优先"""
        rows = getattr(self, "_practice_rows", None)
        if rows is not None:
            return [i for i in range(5) if str(rows[i]["rank"]) == "0"]
        no_sakura, has_sakura = [], []
        for i, cy in enumerate(_LIST_CY):
            # 打过的行没有挑战按钮（盖着结算章），直接跳过
            if not self.device.ocr("挑战", roi_4to4(1110, cy - 30, 1275, cy + 30)):
                continue
            y1, y2 = cy - 60, cy + 55
            pt = self.device.template_match("极化樱花.png", roi=roi_4to4(0, y1, 500, y2),
                                         threshold=0.9)
            (has_sakura if pt else no_sakura).append(i)
        return no_sakura + has_sakura

    def _on_list(self) -> bool:
        self.device.screenshot(force=True)
        return bool(self.device.ocr("演练对手选择", roi_4to4(450, 60, 830, 110)))

    def _ensure_on_list(self) -> bool:
        for _ in range(3):
            if self._on_list():
                return True
            time.sleep(1.5)
        return False

    def _back_to_list(self):
        """从情报界面返回对手列表"""
        for _ in range(3):
            self.device.click(Point(150, 38))
            time.sleep(2.0)
            if self._on_list():
                return

    # ========== 情报界面层 ==========

    def _inspect_opponent(self, row_idx: int):
        """
        点进第 row_idx 个对手的情报界面并认人

        Returns:
            {"title": 对手名, "members": [...]} 或 None（没进去）
        """
        cy = _LIST_CY[row_idx]
        boundary = begin_operation(self.device)
        self.device.click(Point(190, cy))  # 队长卡
        time.sleep(2.5)
        self.device.screenshot(force=True)
        if not self.device.ocr("演练对手情报", roi_4to4(450, 10, 830, 60)):
            return None

        rows = getattr(self, "_practice_rows", None)
        enemy = rows[row_idx]["enemy_id"] if rows else None
        body = practice_receipt(available_events(self.device), boundary, "/practice/offer", enemy, "enemy_id")
        if body is False:
            return None
        if isinstance(body, dict):
            members = practice_members(body)
            if members is not None:
                self._practice_enemy_id = boundary.get("practice_enemy_id", enemy)
                return {"title": rows[row_idx]["title"] if rows else "对手", "members": members,
                        "source": "data"}
        self._practice_enemy_id = boundary.get("practice_enemy_id", enemy)
        # 对手名（审神者 xxx级 名字）
        title_tokens = self.device.ocr_all(roi_4to4(400, 90, 750, 120))
        title = " ".join(t for t, _ in title_tokens)

        members = []
        for cy in _ROW_CY:
            member = self._scan_member(cy)
            if member:
                members.append(member)
        return {"title": title, "members": members, "source": "screen"}

    def _scan_member(self, cy: int):
        """
        扫一行成员：OCR 名字 → 名册校验；短刀加读机动

        Returns:
            {"name", "type", "mobility"或None, "id"} 或 None（空位/认不出）
        """
        name_tokens = self.device.ocr_all(roi_4to4(95, cy + 38, 250, cy + 65))
        if not name_tokens:
            return {"name": "未识别成员", "type": "未知", "mobility": None, "id": None}
        # 取最长 token 当名字（防碎字）
        raw = max((t for t, _ in name_tokens), key=len)
        if len(raw) < 2:
            return {"name": "未识别成员", "type": "未知", "mobility": None, "id": None}

        found = sword_db.find_by_name(raw)
        if not found:
            return {"name": raw, "type": "未知", "mobility": None, "id": None}
        sid, info = found

        member = {
            "name": info.get("name_zh") or info["name"],
            "type": info.get("type", "未知"),
            "level": self._read_level(cy),
            "mobility": None,
            "id": sid,
        }
        # 短刀必须读机动判断极化（普短机动 <=65，极短 110+）
        if member["type"] == "短刀":
            member["mobility"] = self._read_mobility(cy)
        return member

    def _read_level(self, cy: int):
        """读取成员头像右侧的“刀剑 xx 级”数字。"""
        for _ in range(2):
            tokens = self.device.ocr_all(roi_4to4(455, cy - 28, 570, cy + 5))
            for text, _ in tokens:
                match = re.search(r"(\d{1,3})", text)
                if match:
                    return int(match.group(1))
            self.device.screenshot(force=True)
        return None

    def _read_mobility(self, cy: int):
        """读机动格数字（第 4 格，x[754,806]——别动窗口）"""
        for _ in range(2):  # OCR 偶尔翻车，给两次机会
            tokens = self.device.ocr_all(roi_4to4(754, cy + 26, 806, cy + 48))
            for t, _ in tokens:
                t = t.strip()
                if t.isdigit():
                    return int(t)
            self.device.screenshot(force=True)
        return None

    # ========== 战斗 ==========

    # 部队标签 x 中心（部队选择界面顶部）
    _TEAM_TAB_X = {1: 155, 2: 272, 3: 390, 4: 510, 5: 630}

    def _fight_one(self, cfg, row_idx: int):
        """
        真打一场：进入演练 → 部队选择 → 阵形选择 → 自动战斗 → 回列表读结算章

        路径要点（真机校准）：
          - 阵形卡第1点是选中，确定按钮出现在卡内左下 (卡心x-115, 卡心y+13)，第2点才确认
          - 索敌失败时不出现阵形选择，直接开打——轮询时两种结局都要等
          - 战斗 x2 速度自动播完，自动回对手列表，结算章（胜利/败北+评级）盖在打过的那行

        Returns:
            生成器，最后 return "win" / "lose" / "unknown"
        """
        team_no = cfg.get("team_no", 2)
        formation = self._formation_name(cfg.get("formation", "逆行阵"))
        formation_mode = cfg.get("formation_mode", "manual")
        yield f"[演练] 动手！（部队{_CN_NUM.get(team_no, team_no)}，{formation}）"

        boundary = begin_operation(self.device)
        # ---- 部队选择 ----
        self.device.click(Point(1200, 630))  # 进入演练
        time.sleep(2.5)
        self.device.screenshot(force=True)
        if not self.device.ocr("部队选择", roi_4to4(500, 10, 780, 60)):
            yield "[演练] 没进到部队选择界面，放弃这场"
            return "unknown"

        self.device.click(Point(self._TEAM_TAB_X.get(team_no, 272), 93))
        time.sleep(1.5)
        self.device.click(Point(1200, 630))  # 演练开始
        time.sleep(2.0)

        # ---- 阵形选择（索敌失败则跳过此界面直接打）----
        got_formation = False
        for _ in range(10):  # 最多等 20s
            self.device.screenshot(force=True)
            if self._formation_mode_state(
                    allow_auto_without_title=formation_mode != "auto") is not None:
                got_formation = True
                break
            if self._on_list():
                break
            time.sleep(2.0)

        if got_formation:
            result = self.choose_formation(
                formation_name=formation,
                enable_auto=formation_mode == "auto",
            )
            if result == "advantage":
                yield "[演练] 选择有利阵形"
            elif result == "fixed":
                yield f"[演练] 选择固定/兜底阵形：{formation}"
            else:
                yield "[演练] 阵形选择失败，停止这场，避免在选择页误点"
                return "unknown"

            formation_left = False
            for _ in range(8):
                time.sleep(0.5)
                self.device.screenshot(force=True)
                if self._formation_mode_state() is None:
                    formation_left = True
                    break
            if not formation_left:
                yield "[演练] 阵形仍未确认，停止这场，避免在选择页误点"
                return "unknown"
            yield "[演练] 阵形已确认，战斗中..."

        # ---- 等战斗播完回列表（途中每3秒点一下安全区，翻结算页/跳动画）----
        # 别点屏幕中央：万一列表检测慢半拍，中心点会戳到对手行误开下一场。
        for _ in range(60):  # 最多 180s
            if self._on_list():
                break
            self._click_point(self._skip_point(cfg.get("skip_tap")))
            time.sleep(3.0)
        else:
            yield "[演练] 等战斗结束超时"
            body = practice_receipt(available_events(self.device), boundary, "/battle/practicebattle",
                                    getattr(self, "_practice_enemy_id", None), "enemy_user_id")
            if isinstance(body, dict):
                return practice_rank((body.get("result") or {}).get("rank")) or "unknown"
            return "unknown"

        # ---- 读结算章 ----
        time.sleep(1.0)
        body = practice_receipt(available_events(self.device), boundary, "/battle/practicebattle",
                                getattr(self, "_practice_enemy_id", None), "enemy_user_id")
        if body is False:
            return "unknown"
        if isinstance(body, dict):
            result = practice_rank((body.get("result") or {}).get("rank"))
            if result is not None:
                return result
        return self._read_result(row_idx)

    def _read_result(self, row_idx: int):
        """读打过的那行的结算章：胜利/败北 + 评级（章比列表晚弹出，多试几次）"""
        cy = _LIST_CY[row_idx]
        text = ""
        for _ in range(4):
            time.sleep(1.5)
            self.device.screenshot(force=True)
            tokens = self.device.ocr_all(roi_4to4(1050, cy - 40, 1280, cy + 40))
            text = "".join(t for t, _ in tokens)
            if "胜利" in text or "败北" in text:
                break
        if "胜利" in text:
            rating = text.replace("完全", "").replace("胜利", "")
            return "win" if not rating else f"win({rating})"
        if "败北" in text:
            return "lose"
        return "unknown"
