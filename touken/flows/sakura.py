# -*- coding: utf-8 -*-
"""
上层业务：刷花（刷疲劳到100）——1-1 单曲循环（按需工具，不是日课）

为什么要刷花（用户亲授）：
  疲劳掉到 49 就不飘花（樱吹雪），远征大成功概率跟着掉。
  游戏机制：队长位必定+疲劳，誉（MVP）也+疲劳——所以刷花是
  【队长一个人穿好刀装，单挑 1-1】，不能往队里塞别人（会抢誉）。

流程（全本丸轮刷，只选上锁且等级大于1的刀，不按标签筛选）：
  编队 → 解散指定部队 → 替换列表按樱吹雪升序
  → 选已上锁、等级>1、疲劳≤49的刀 → 取消期间限定/远战勾选 → 自动装备刀装
  → 队长单挑1-1，复用既有出阵保护 → 疲劳100 → 全部卸装
  → 换下一振，达到本次数量或没有候选时收工


坐标（真机校准，1280x720）：
  编队页部队标签：部队一(154,91) 二(274,91) 三(394,91) 四(516,91) 五(638,91)
  成员行心 y [160,258,357,455,553,652]（一之一~一之六）
  疲劳文本在每行 "疲劳 xx/100"：roi x[290,425] y[cy+28,cy+52]
  每行"装备"按钮 x≈963 y=cy；更换装备页"自动装备刀装"(621,96)；返回箭头(135,25)

疲劳值识别要点：roi 可能蹭到上面那行"生存 xx/xx"，
  所以配对的两个数里分母是 100 的才是疲劳（疲劳上限永远 100）。
"""

import re
import subprocess
import time

from .. import sword_db
from ..maa_adapter import roi_4to4, Point

_TEAM_TAB = {1: (154, 91), 2: (274, 91), 3: (394, 91), 4: (516, 91), 5: (638, 91)}
_ROW_CY = [160, 258, 357, 455, 553, 652]
_EQUIP_X = 963            # 每行"装备"按钮
_SWAP_X = 1033            # 每行"替换"按钮
_AUTO_EQUIP = (621, 96)   # 更换装备页"自动装备刀装"
_EQUIP_BACK = (135, 25)   # 更换装备页返回箭头

# 换队长拖拽：从成员卡片拖到队长位。起点 x 是真机校准点——
# 太快会被游戏吞（repair.py 实测 <400ms 失灵）；adb input swipe 匀速
# 插值，拖远了速度跟着变快，容易被当成列表滚动，所以时长按距离放大
_DRAG_X = 200
_DRAG_MS = 1000   # 起步时长，实际取 max(此值, 拖动距离×2.5)

# 换队长读疲劳：成员信息列整列 OCR（x 覆盖"疲劳 xx/100"文本）后按 y
# 归位。血泪（2026-09-21 六号位隐形事故）：编队页行距 ~98px、部队选择页
# ~94.5px，写死六行坐标会在底部行累积 60px+ 偏差，5/6 号位疲劳整个
# 读空——累的人对轮换隐形。两页不再共用行坐标假设。
_FATIGUE_COL = (285, 100, 435, 710)      # 疲劳列整列 ROI（xyxy）
_FIRST_ROW_Y = (140, 260)                # 首行疲劳 y 合理范围（两页均 ~196-199）
_FATIGUE_TO_ROW_CY = 36                  # 疲劳行 y → 行中心（拖拽/名字定位用）


def _parse_fatigue_text(text: str):
    """从一行 OCR 文本里挑疲劳值：配对的两个数里分母是 100 的才是疲劳
    （roi 可能蹭到上面那行"生存 xx/xx"，疲劳上限永远 100）。
    Returns: 疲劳值 int 或 None
    """
    pairs = re.findall(r"(\d{1,3})\D{0,2}/\D{0,2}(\d{1,3})", text)
    if not pairs:
        return None
    for cur, mx in pairs:
        if mx == "100":
            return int(cur)
    return int(pairs[0][0])

# 刀剑男士选择（替换列表）：行位置随滚动会飘，不写死行坐标，
# 整列 OCR 按 y 分行找"疲劳"行；决定按钮 x≈1197，中心≈疲劳行上方 40
_SEL_DECIDE_X = 1197
_SEL_MAX_PAGES = 80        # 翻页安全上限


class SakuraMixin:
    """刷花。依赖宿主类的 navigate_to_stream、sortie_stream、maa。"""

    def sakura_stream(self, team_no: int = 5, slot: int = 1,
                      target: int = 100, max_rounds: int = 40,
                      auto_swap: bool = True, swap_threshold: int = 50,
                      sword_count: int = 1, repair_threshold: str = "light"):
        """全本丸轮刷：单人队长，疲劳≤49入选，100卸装换人。"""
        if team_no not in _TEAM_TAB or slot != 1:
            yield "[刷花] 请指定部队1-5的队长位，单人刷花不支持其他位置"
            return
        if not 1 <= sword_count <= 1000 or max_rounds < 1:
            yield "[刷花] 本次刷花数量或圈数上限无效，停止"
            return
        if repair_threshold not in ('light', 'medium', 'heavy'):
            yield "[刷花] 伤势停止条件无效，未清队"
            return
        target, swap_threshold = 100, 50
        from ..expedition_sakura import pending_restore
        if pending_restore():
            yield '[刷花] ✗ 远征补花队伍尚未恢复，请先执行远征管理恢复队伍'
            return
        yield f"[刷花] 部队{team_no}，本次最多刷 {sword_count} 振，只选上锁、等级>1、疲劳≤49的刀"
        if not (yield from self._prepare_sakura_team(team_no)):
            return
        completed = 0
        completed_serials = set()
        for index in range(sword_count):
            if self._expedition_takeover_requested():
                yield "[刷花] 🚩 远征排班请求接管：不开新圈，安全收工"
                return
            selected = yield from self._swap_tired_in(1, 50)
            if selected is not True:
                if selected is False:
                    yield f"[刷花] 没有可选的疲劳≤49刀剑，已完成 {completed} 振，收工"
                return
            fatigue = yield from self._check_fatigue(team_no, 1, in_place=True)
            if fatigue is None or not 0 <= fatigue <= 49:
                yield "[刷花] 换入后未确认疲劳≤49，停止"
                return
            slots = self._read_team_page()
            if (len(slots) != 6 or slots[0]['slot_status'] != 'occupied'
                    or any(row['slot_status'] != 'empty' for row in slots[1:])):
                yield "[刷花] 未确认队伍只有队长一人，停止"
                return
            serial = self._sakura_selected_serial(team_no)
            if serial is not None and serial in completed_serials:
                yield "[刷花] 游戏记录显示这振本轮已完成，停止，未重复计数"
                return
            if not (yield from self._auto_equip(1)):
                return
            for rd in range(1, max_rounds + 1):
                if self._expedition_takeover_requested():
                    yield "[刷花] 🚩 远征排班请求接管：不开新圈，安全收工"
                    return
                yield f"[刷花] 第 {index + 1}/{sword_count} 振，第 {rd} 圈（疲劳 {fatigue}/100）"
                round_done = stop = False
                for msg in self.sortie_stream(chapter=1, map_no=1, team_no=team_no,
                                              auto_march=True, max_loops=1,
                                              formation_mode="auto", formation="鱼鳞阵",
                                              repair_threshold=repair_threshold,
                                              injury_action="stop"):
                    yield msg
                    round_done |= msg == f"[出阵] ✓ 全部 1 圈跑完，部队{team_no}辛苦啦，收工！"
                    stop |= any(word in msg for word in ("绝不出阵", "强制停", "行军中断"))
                if stop or not round_done:
                    yield "[刷花] 这圈未完成或被安全规矩拦下，停止刷花"
                    return
                fatigue = yield from self._check_fatigue(team_no, 1)
                if fatigue is None or not 0 <= fatigue <= 100:
                    yield "[刷花] 打完一圈读不到疲劳，停止"
                    return
                if fatigue >= 100:
                    if not (yield from self._sakura_unequip()):
                        return
                    if serial is not None:
                        completed_serials.add(serial)
                    completed += 1
                    yield f"[刷花] 第 {completed} 振刷到100，已卸装"
                    break
            else:
                yield f"[刷花] 这振刷了 {max_rounds} 圈还没到100，已完成 {completed} 振，安全上限收工"
                return
        yield f"[刷花] 已完成 {completed} 振，本次数量已达，收工"

    def _sakura_selected_serial(self, team_no):
        """只用本次换人之后的客户端部队响应辅助去重，旧名单不冒充现场。"""
        from .. import youzu_log
        from ..runtime_paths import DEBUG_DIR
        path = None
        try:
            path = youzu_log.pull_log(self.maa.adb_path, self.maa.adb_address, DEBUG_DIR)
            for event in reversed(youzu_log.parse_events(path)):
                body = event.get('payload')
                if (event.get('direction') != 'S->C' or event.get('status') != 200
                        or not isinstance(body, dict) or str(body.get('status')) != '0'
                        or (youzu_log._event_epoch(event) or 0) < getattr(self, '_sakura_selection_at', float('inf')) - 1):
                    continue
                parties = body.get('party')
                # /party/setsword 的响应直接以「1」到「5」为键，没有 party 外层。
                if event.get('endpoint') == '/party/setsword':
                    parties = body
                party = parties.get(str(team_no)) if isinstance(parties, dict) else None
                slots = party.get('slot') if isinstance(party, dict) else None
                captain = slots.get('1') if isinstance(slots, dict) else None
                if isinstance(captain, dict):
                    return youzu_log._int(captain.get('serial_id')) or None
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pass
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        return None

    def _prepare_sakura_team(self, team_no):
        yield from self.navigate_to_stream("编队")
        if self.current_location != "编队" or not (yield from self._select_team_confirmed(team_no)):
            yield "[刷花] 目标部队未确认，停止"
            return False
        self.maa.screenshot(force=True)
        pt = self.maa.template_match("team/部队解散.png", roi_4to4(1120, 205, 1280, 265), threshold=.85)
        if not pt:
            yield "[刷花] 找不到部队解散按钮，停止"
            return False
        self.maa.click(pt)
        time.sleep(1)
        self.maa.screenshot(force=True)
        yes = self.maa.ocr("是", roi_4to4(350, 250, 930, 650), match_mode="exact")
        if not yes:
            yield "[刷花] 未确认解散弹窗，停止"
            return False
        cleared_since = int(time.time())
        self.maa.click(yes)
        time.sleep(1)
        self.maa.screenshot(force=True)
        from ..selection_identity import client_events, team_cleared
        # 解散会清空六槽。空槽的数字/底线会被 OCR 当成内容，不能拿它
        # 推翻客户端本次响应；页面标题只确认仍在编队，人数由 JSON 确认。
        if (not self.maa.ocr('部队编成', roi_4to4(500, 0, 780, 60))
                or not team_cleared(client_events(self.maa), team_no, cleared_since)):
            yield "[刷花] 解散后未确认队员位置清空，停止"
            return False
        yield f"[刷花] 客户端已确认部队{team_no}六个位置清空，接下来选队长，单人轮刷"
        return True

    def _rotate_captain_here(self, margin: int = 10):
        """
        在当前页原地换队长：整列 OCR 读全队疲劳，最低的拖到队长位，拖完复查。

        调用前必须已停在目标部队的成员列表页（部队标签已切好）。
        编队页和出阵前的部队选择页都能拖人换位，但两页行距不同
        （编队 ~98px、部队选择 ~94.5px），所以读数走整列 OCR 按 y
        归位，不写死行坐标——写死会在底部行累积偏差，2026-09-21
        出过 5/6 号位疲劳读空、红脸队员对轮换隐形的事故。

        Yields:
            str: 执行状态消息
        """
        rows = self._read_rows_fatigue()
        if not rows:
            yield "[换队长] 全队疲劳读不齐（在远征？界面不对？），本轮不换"
            return

        values = {slot: value for slot, (fy, value) in rows.items()}
        captain = values.get(1)
        if captain is None:
            yield "[换队长] 队长位读不到疲劳（空位？），跳过"
            return
        low_slot = min(values, key=values.get)
        low = values[low_slot]
        overview = "、".join(f"{s}号位{v}" for s, v in sorted(values.items()))
        yield f"[换队长] 全队疲劳：{overview}"

        if low_slot == 1:
            yield f"[换队长] 队长自己就是全队最低（{captain}/100），位置没毛病，收工"
            return
        if captain - low < margin:
            yield (f"[换队长] 全队最低才 {low}/100，跟队长（{captain}）"
                   f"差不到 {margin}，不值得折腾，收工")
            return

        # 读个名字好汇报（名字在疲劳行上方，同 _swap_tired_in 的相对位置）。
        # OCR 老眼昏花会漏字（"夜左文字"），过名册校正成标准名再上日志
        low_fy = rows[low_slot][0]
        cy_low = low_fy - _FATIGUE_TO_ROW_CY
        cy_top = rows[1][0] - _FATIGUE_TO_ROW_CY
        name_tokens = self.maa.ocr_all(roi_4to4(100, low_fy - 32, 265, low_fy - 4))
        name_raw = max((t for t, _ in name_tokens), key=len, default=f"{low_slot}号位")
        found_name = sword_db.find_by_name(name_raw, fuzzy=False)
        recognized_name = (found_name[1].get("name_zh") or found_name[1]["name"]
                           if found_name else None)
        name = recognized_name or f"{low_slot}号位"
        yield f"[换队长] {name} 疲劳 {low} 全队最低，拖去队长位（原队长 {captain}/100）"

        # adb input swipe 匀速插值：拖远了速度跟着变快，容易被游戏当成
        # 列表滚动吞掉，时长按距离放大
        duration = max(_DRAG_MS, int((cy_low - cy_top) * 2.5))
        after_values = {}
        for attempt in (1, 2):
            self.maa.swipe(_DRAG_X, cy_low, _DRAG_X, cy_top,
                           duration if attempt == 1 else int(duration * 1.5))
            time.sleep(2.0)

            # 拖完复查：队长位的疲劳应该变成刚才那位最低值
            after = self._read_rows_fatigue()
            after_values = {s: v for s, (f, v) in after.items()}
            if after_values.get(1) == low:
                yield f"[换队长] ✓ 换好了，{name} 上任队长，去吃疲劳加成吧"
                return
            # 疲劳复读会 OCR 错字（88 认成 86）：用队长位的名字再核一遍
            if after:
                cap_fy = after[1][0]
                cap_tokens = self.maa.ocr_all(
                    roi_4to4(100, cap_fy - 32, 265, cap_fy - 4))
                cap_found = sword_db.find_by_name(
                    max((t for t, _ in cap_tokens), key=len, default=""), fuzzy=False)
                cap_name = (cap_found[1].get("name_zh") or cap_found[1]["name"]
                            if cap_found else None)
                if recognized_name and cap_name == recognized_name:
                    yield (f"[换队长] ✓ 换好了（疲劳复读 {after_values.get(1)} "
                           f"对不上 {low}，但队长位已是 {name}），去吃疲劳加成吧")
                    return
            if attempt == 1:
                # 手势若被当成滚动，行位置可能已经飘了：按现位置再拖一次
                if after:
                    cy_low = next((fy for fy, v in after.values() if v == low),
                                  after[1][0]) - _FATIGUE_TO_ROW_CY
                    cy_top = after[1][0] - _FATIGUE_TO_ROW_CY
                yield "[换队长] 复查对不上，按当前位置再拖一次..."
        got = after_values.get(1, "读不到")
        yield (f"[换队长] ⚠️ 拖完队长位疲劳是 {got}，不是预期的 {low}"
               "——拖动可能没生效（手势被吞？），你手动瞅一眼")

    def _read_rows_fatigue(self) -> dict:
        """当前页成员列表整列 OCR：抓所有"疲劳"行连同 y 坐标，按 y 排序
        对号 1~N 号位（适配 3~6 人队伍，编队/部队选择两页通吃）。

        Returns: {位置: (疲劳行y, 疲劳值)}；首行位置不对或相邻行距突变
        说明整列错位/中间漏行，对号会错——返回 {}，宁可不读也不拖错人。
        """
        self.maa.screenshot(force=True)
        tokens = self.maa.ocr_all(roi_4to4(*_FATIGUE_COL))
        lines = {}  # y 聚类 -> 该行 token（±12px 算同一行）
        for t, p in tokens:
            key = round(p.y / 12)
            lines.setdefault(key, []).append((p.x, t, p.y))
        rows = []  # (疲劳行y, 疲劳值)
        for group in lines.values():
            group.sort()
            text = "".join(t for _, t, _ in group)
            if "疲" not in text:  # "疲劳"偶发认成"疲务"，只看半边
                continue
            value = _parse_fatigue_text(text)
            if value is None:
                continue
            rows.append((group[0][2], value))
        rows.sort()
        if not rows:
            return {}
        # 漏读首行会让全队错号（顶 anchor）；中间漏行会留下倍距空洞
        if not (_FIRST_ROW_Y[0] <= rows[0][0] <= _FIRST_ROW_Y[1]):
            return {}
        if len(rows) >= 3:
            gaps = [rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1)]
            median = sorted(gaps)[len(gaps) // 2]
            if any(g > median * 1.6 for g in gaps):
                return {}
        return {slot: row for slot, row in enumerate(rows, start=1)}

    # ==================== 读疲劳 ====================

    def _check_fatigue(self, team_no: int, slot: int, *, in_place: bool = False):
        """
        导航到编队 → 切部队标签 → OCR 读疲劳。
        Returns: 疲劳值 int；读不到返回 None（yield from 接返回值）
        """
        if in_place:
            # 换人已回到所选部队，直接读，不重走目录或重选部队。
            self.maa.screenshot(force=True)
            if not self.maa.ocr("部队编成", roi_4to4(500, 0, 780, 60)):
                yield "[刷花] 换人后未确认编队页，停止"
                return None
        else:
            for nav_msg in self.navigate_to_stream("编队"):
                yield nav_msg
            if self.current_location != "编队":
                yield "[刷花] 到不了编队"
                return None
            self.maa.click(Point(*_TEAM_TAB[team_no]))
            time.sleep(1.5)
        self.maa.screenshot(force=True)

        cy = _ROW_CY[slot - 1]
        tokens = self.maa.ocr_all(roi_4to4(290, cy + 28, 425, cy + 52))
        text = "".join(t for t, _ in tokens)
        return _parse_fatigue_text(text)

    # ==================== 换人：找疲劳低的换进来 ====================

    def _swap_tired_in(self, slot: int, threshold: int):
        """
        编队页点那行的"替换"→ 刀剑男士选择列表逐行 OCR 疲劳，
        第一个低于 threshold 的点"决定"换进来（此时已经在编队页）。
        Returns: 是否换人成功（yield from 接返回值）
        """
        cy = _ROW_CY[slot - 1]
        self.maa.click(Point(_SWAP_X, cy))
        time.sleep(2.0)
        self.maa.screenshot(force=True)
        if not self.maa.ocr("刀剑男士选择", roi_4to4(500, 0, 780, 55)):
            yield "[刷花·换人] 选择列表没打开，放弃换人"
            return None
        if not self._sakura_sort_list():
            yield "[刷花·换人] 樱吹雪升序未确认，停止"
            return None

        seen_pages = set()
        for page in range(_SEL_MAX_PAGES):
            image = self.maa.screenshot(force=True)
            # 整列 OCR 按 y 分行找"疲劳"——列表滚动后行位置会飘，不写死行坐标
            tokens = self.maa.ocr_all(roi_4to4(460, 100, 620, 700))
            fingerprint = tuple((t, round(p.y)) for t, p in tokens)
            if fingerprint in seen_pages:
                yield "[刷花·换人] 名单到底或翻页未生效，停止；未确认没有候选"
                return None
            seen_pages.add(fingerprint)
            lines = {}  # y中心 -> 该行文本
            for t, p in tokens:
                key = round(p.y / 12)
                lines.setdefault(key, []).append((p.x, t, p.y))
            # OCR会把「疲劳」和「49/100」拆成两个token，先按行合并再判定。
            fatigue_lines = ["".join(t for _, t, _ in sorted(group)) for group in lines.values()]
            fatigue_lines = [text for text in fatigue_lines if "疲劳" in text]
            values = [_parse_fatigue_text(text) for text in fatigue_lines]
            if (values and all(v is not None and threshold <= v <= 100 for v in values)):
                return False
            for group in lines.values():
                group.sort()
                text = "".join(t for _, t, _ in group)
                if "疲劳" not in text:
                    continue
                fy = group[0][2]
                pairs = re.findall(r"(\d{1,3})\D{0,2}/\D{0,2}(\d{1,3})", text)
                value = None
                for cur, mx in pairs:
                    if mx == "100":
                        value = int(cur)
                        break
                if value is None or not 0 <= value < threshold:
                    continue
                if not self._sakura_candidate_eligible(image, fy):
                    continue
                # 找到累的了：读个名字好汇报（过名册校正错别字），点决定
                # （按钮中心≈疲劳行上方40）
                name_tokens = self.maa.ocr_all(roi_4to4(100, fy - 18, 325, fy + 13))
                raw_name = max((t for t, _ in name_tokens), key=len, default="")
                found = sword_db.find_by_name(raw_name, fuzzy=False)
                name = (found[1].get('name_zh') or found[1]['name']) if found else '这振刀'
                yield f"[刷花·换人] 第{page + 1}页找到 {name} 疲劳{value}，换！"
                self._sakura_selection_at = time.time()
                self.maa.click(Point(_SEL_DECIDE_X, fy - 40))
                time.sleep(1.5)
                # 可能有确认弹窗
                self.maa.screenshot(force=True)
                pt = self.maa.template_match("通用_确定.png", threshold=0.7)
                if pt:
                    self.maa.click(pt)
                    time.sleep(1.5)
                if self._wait_list_closed():
                    return True
                yield f"[刷花·换人] {name}目前无法选入，继续找下一振"
            # 本页没有累的，翻页
            self.maa.swipe(640, 550, 640, 200, 800)
            time.sleep(2.0)
        # 完整扫描上限不等于没有候选：不能把没扫完说成全军飘花。
        yield "[刷花·换人] 名单扫描达到上限，停止；未确认全本丸都已飘花"
        return None

    def _sakura_candidate_eligible(self, image, fatigue_y):
        """锁和刀剑等级都要正面读到；未知、未锁、1级均跳过。"""
        from .formation_editor import recognize_selection_lock
        # 名字行在疲劳行上方约6px；复用同源校准的选择列表锁识别。
        if recognize_selection_lock(image, fatigue_y - 6) != 'locked':
            return False
        tokens = self.maa.ocr_all(roi_4to4(470, fatigue_y - 84, 591, fatigue_y - 57)) or []
        text = ''.join(t for t, _ in tokens)
        levels = re.findall(r'刀剑\s*(\d{1,3})\s*级', text)
        return len(levels) == 1 and int(levels[0]) > 1

    def _sakura_sort_list(self):
        if not self._open_filter_panel():
            return False
        if not self._click_panel_button("取消筛选"):
            return False
        self.maa.screenshot(force=True)
        if not self.maa.ocr("筛选", roi_4to4(150, 50, 900, 150)):
            if not self._open_filter_panel():
                return False
        if not self._click_panel_button("全刀剑", require_selected=True):
            return False
        # 国服按钮实际写「櫻吹雪」，不让繁简差异变成找不到排序。
        if not self._click_panel_button("櫻吹雪", roi=(942, 430, 1082, 479), require_selected=True):
            return False
        if not self._click_panel_button("确定"):
            return False
        time.sleep(1)
        for _ in range(2):
            self.maa.screenshot(force=True)
            roi = roi_4to4(930, 70, 1030, 115)
            if self.maa.ocr("升序", roi):
                return True
            pt = self.maa.ocr("降序", roi)
            if not pt:
                return False
            self.maa.click(pt)
            time.sleep(1)
        return False

    # ==================== 自动装备刀装 ====================

    def _sakura_equipment_page(self, slot=1):
        self.maa.click(Point(_EQUIP_X, _ROW_CY[slot - 1]))
        time.sleep(2)
        self.maa.screenshot(force=True)
        return bool(self.maa.ocr("更换装备", roi_4to4(530, 0, 750, 60)))

    @staticmethod
    def _sakura_blank_checkbox(image, bounds):
        if getattr(image, 'shape', None) != (720, 1280, 3):
            return False
        x1, y1, x2, y2 = bounds
        interior = image[y1 + 7:y2 - 7, x1 + 7:x2 - 7]
        return bool(interior.size and ((interior >= 220).all(axis=2)).mean() > .97)

    def _sakura_clear_equipment_filters(self):
        # 运行帧校准：只搜各自复选框，避免拿别处的勾来判断。
        for label, bounds in (("期间限定", (296, 81, 327, 112)),
                              ("远战", (430, 81, 461, 112))):
            image = self.maa.screenshot(force=True)
            if image is None:
                return False
            pt = self.maa.template_match("team/ui道具勾选.png", roi_4to4(*bounds), threshold=.85)
            if pt:
                self.maa.click(pt)
                time.sleep(2)  # 等点击特效消失再读勾。
                image = self.maa.screenshot(force=True)
                if self.maa.template_match("team/ui道具勾选.png", roi_4to4(*bounds), threshold=.85):
                    yield f"[刷花] {label}仍勾选，停止"
                    return False
            if not self._sakura_blank_checkbox(image, bounds):
                yield f"[刷花] {label}未确认取消勾选，停止"
                return False
        return True

    def _sakura_equipment_return(self):
        self.maa.click(Point(*_EQUIP_BACK))
        time.sleep(1.5)
        self.maa.screenshot(force=True)
        return bool(self.maa.ocr("部队编成", roi_4to4(500, 0, 780, 60)))

    def _auto_equip(self, slot: int):
        if not self._sakura_equipment_page(slot):
            yield "[刷花] 装备页没打开，停止"
            return False
        if not (yield from self._sakura_clear_equipment_filters()):
            return False
        pt = self.maa.template_match("team/自动装备刀装.png", threshold=.85)
        if not pt:
            yield "[刷花] 自动装备刀装按钮未确认，停止"
            return False
        self.maa.click(pt)
        time.sleep(2)
        self.maa.screenshot(force=True)
        confirm = self.maa.template_match("通用_确定.png", threshold=.7)
        if confirm:
            self.maa.click(confirm)
            time.sleep(1)
        if not self._sakura_equipment_return():
            yield "[刷花] 装备后未回到编队，停止"
            return False
        yield "[刷花] 已取消期间限定和远战筛选，已点自动装备刀装"
        return True

    def _sakura_unequip(self):
        if not self._sakura_equipment_page():
            yield "[刷花] 卸装时装备页没打开，停止"
            return False
        pt = self.maa.template_match("team/全部卸装.png", threshold=.85)
        if not pt:
            yield "[刷花] 全部卸装按钮未确认，停止"
            return False
        self.maa.click(pt)
        time.sleep(2)
        self.maa.screenshot(force=True)
        confirm = self.maa.template_match("通用_确定.png", threshold=.7)
        if confirm:
            self.maa.click(confirm)
            time.sleep(1)
        if not self._sakura_equipment_return():
            yield "[刷花] 卸装后未回到编队，停止"
            return False
        return True
