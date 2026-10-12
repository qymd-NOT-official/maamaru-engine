# -*- coding: utf-8 -*-
"""
上层业务：演练（演习场）——认人避战版

规矩（用户亲授）：
  1. 对手列表显示的队长的极化状态不代表全队，必须点进去看配置
  2. 硬性躲避：丙子椒林剑和识别不清的成员
  3. 先看完五队再排序：人数少优先；都有极短时比较机动、等级；最后看平均等级
  4. 每天只需赢 3 场，对手 3 点/15 点刷新，一次 5 队，挑软的捏
  5. 出战用部队二（极短队），阵形选逆行阵（抢机动先手）

技术要点（血泪校准）：
  - 游戏编号优先确定刀种与极化；以下机动判定仅用于画面回退：
    名册刀种是短刀 + 机动 >= 100
    （普刀全数值封顶两位数；极短机动 110+，隔了一个数量级）
  - 极化樱花在情报界面会被白底肖像骗（一文字则宗的花瓣背景 0.84 误报），
    所以在情报界面不靠樱花，只信名册 + 机动
  - 数值条每格 55.7px 宽，机动是第 4 格 x[754,806]——窗口偏 1px 都会
    把隔壁防御的尾数读进来，别改
  - 图标中心行距 94px，第一行 cy=152
"""

import re
import time

from ..maa_adapter import roi_4to4, Point
from .. import sword_db
from ..game_state import available_events, begin_operation, practice_list, practice_rank, practice_receipt, practice_members

# 情报界面 6 个成员行的图标中心 y
_ROW_CY = [152, 246, 340, 434, 528, 622]
# 对手列表 5 行的队长卡点击点 y（真机校准：行距 112，不是 135！）
_LIST_CY = [179, 290, 402, 514, 626]

_HEISHI_ID = "touken_208_heishi_shourinken"  # 丙子椒林剑

# 刀种模板（内芯裁剪版，resource/base/image/刀种2/）
_ICON_TEMPLATES = ["短_银", "打_银", "太_金"]


class PracticeMixin:
    """演练。依赖宿主类的 navigate_to_stream、maa、config。"""

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
        if max_wins is None:
            max_wins = cfg.get("max_wins", 3)

        yield f"[演练] {'认人演习' if dry_run else '真打'} 模式，目标赢 {max_wins} 场"

        # ========== 1. 导航到演练 ==========
        yield "[演练] 正在导航到演练..."
        for nav_msg in self.navigate_to_stream("演练"):
            yield nav_msg
        if self.current_location != "演练":
            yield "[演练] 到达演练失败"
            return
        time.sleep(1.0)
        self.maa.screenshot(force=True)

        # 游戏会把本轮已经打过的结果章一直留在五名对手的行末。它比本地
        # 时间/进程记录更可靠：3 点、15 点换一批对手时，章也会自然清空。
        # 先把已有胜场算进去，避免同一刷新周期内二次触发又从 0 开始打。
        self._practice_rows = practice_list(available_events(self.maa))
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
            result = yield from self._fight_one(cfg, row_idx)
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
                for nav_msg in self.navigate_to_stream("演练"):
                    yield nav_msg
                time.sleep(1.0)

        yield (f"[演练] 收工：本次新赢 {new_wins} 场，当前刷新场累计 {wins} 场"
               + ("（演习模式，未真打）" if dry_run else ""))

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
            tokens = self.maa.ocr_all(roi_4to4(1050, cy - 40, 1280, cy + 40))
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
            if not self.maa.ocr("挑战", roi_4to4(1110, cy - 30, 1275, cy + 30)):
                continue
            y1, y2 = cy - 60, cy + 55
            pt = self.maa.template_match("极化樱花.png", roi=roi_4to4(0, y1, 500, y2),
                                         threshold=0.9)
            (has_sakura if pt else no_sakura).append(i)
        return no_sakura + has_sakura

    def _on_list(self) -> bool:
        self.maa.screenshot(force=True)
        return bool(self.maa.ocr("演练对手选择", roi_4to4(450, 60, 830, 110)))

    def _ensure_on_list(self) -> bool:
        for _ in range(3):
            if self._on_list():
                return True
            time.sleep(1.5)
        return False

    def _back_to_list(self):
        """从情报界面返回对手列表"""
        for _ in range(3):
            self.maa.click(Point(150, 38))
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
        boundary = begin_operation(self.maa)
        self.maa.click(Point(190, cy))  # 队长卡
        time.sleep(2.5)
        self.maa.screenshot(force=True)
        if not self.maa.ocr("演练对手情报", roi_4to4(450, 10, 830, 60)):
            return None

        rows = getattr(self, "_practice_rows", None)
        enemy = rows[row_idx]["enemy_id"] if rows else None
        body = practice_receipt(available_events(self.maa), boundary, "/practice/offer", enemy, "enemy_id")
        if body is False:
            return None
        if isinstance(body, dict):
            members = practice_members(body)
            if members is not None:
                self._practice_enemy_id = boundary.get("practice_enemy_id", enemy)
                return {"title": rows[row_idx]["title"] if rows else "对手", "members": members}
        self._practice_enemy_id = boundary.get("practice_enemy_id", enemy)
        # 对手名（审神者 xxx级 名字）
        title_tokens = self.maa.ocr_all(roi_4to4(400, 90, 750, 120))
        title = " ".join(t for t, _ in title_tokens)

        members = []
        for cy in _ROW_CY:
            member = self._scan_member(cy)
            if member:
                members.append(member)
        return {"title": title, "members": members}

    def _scan_member(self, cy: int):
        """
        扫一行成员：OCR 名字 → 名册校验；短刀加读机动

        Returns:
            {"name", "type", "mobility"或None, "id"} 或 None（空位/认不出）
        """
        name_tokens = self.maa.ocr_all(roi_4to4(95, cy + 38, 250, cy + 65))
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
            tokens = self.maa.ocr_all(roi_4to4(455, cy - 28, 570, cy + 5))
            for text, _ in tokens:
                match = re.search(r"(\d{1,3})", text)
                if match:
                    return int(match.group(1))
            self.maa.screenshot(force=True)
        return None

    def _read_mobility(self, cy: int):
        """读机动格数字（第 4 格，x[754,806]——别动窗口）"""
        for _ in range(2):  # OCR 偶尔翻车，给两次机会
            tokens = self.maa.ocr_all(roi_4to4(754, cy + 26, 806, cy + 48))
            for t, _ in tokens:
                t = t.strip()
                if t.isdigit():
                    return int(t)
            self.maa.screenshot(force=True)
        return None

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
                dodge.append(f"{m['name']} 名册查无此人，认不清不敢打")

        if dodge:
            return "躲", dodge

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

        boundary = begin_operation(self.maa)
        # ---- 部队选择 ----
        self.maa.click(Point(1200, 630))  # 进入演练
        time.sleep(2.5)
        self.maa.screenshot(force=True)
        if not self.maa.ocr("部队选择", roi_4to4(500, 10, 780, 60)):
            yield "[演练] 没进到部队选择界面，放弃这场"
            return "unknown"

        self.maa.click(Point(self._TEAM_TAB_X.get(team_no, 272), 93))
        time.sleep(1.5)
        self.maa.click(Point(1200, 630))  # 演练开始
        time.sleep(2.0)

        # ---- 阵形选择（索敌失败则跳过此界面直接打）----
        got_formation = False
        for _ in range(10):  # 最多等 20s
            self.maa.screenshot(force=True)
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
                self.maa.screenshot(force=True)
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
            body = practice_receipt(available_events(self.maa), boundary, "/battle/practicebattle",
                                    getattr(self, "_practice_enemy_id", None), "enemy_user_id")
            if isinstance(body, dict):
                return practice_rank((body.get("result") or {}).get("rank")) or "unknown"
            return "unknown"

        # ---- 读结算章 ----
        time.sleep(1.0)
        body = practice_receipt(available_events(self.maa), boundary, "/battle/practicebattle",
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
            self.maa.screenshot(force=True)
            tokens = self.maa.ocr_all(roi_4to4(1050, cy - 40, 1280, cy + 40))
            text = "".join(t for t, _ in tokens)
            if "胜利" in text or "败北" in text:
                break
        if "胜利" in text:
            rating = text.replace("完全", "").replace("胜利", "")
            return "win" if not rating else f"win({rating})"
        if "败北" in text:
            return "lose"
        return "unknown"


_CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}
