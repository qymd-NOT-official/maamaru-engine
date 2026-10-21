# -*- coding: utf-8 -*-
"""编队页只读点名（team_roster）的单元测试。

假设备 是**点击驱动的页面状态机**：只有 click(部队标签) 才会切页，
页面内容（含位置标签）只取决于当前页——切队确认逻辑读错队会直接暴露，
不存在"按截图次数自动翻页"的假宿主。可模拟点击被吞（swallow）和
慢一拍才切成功（delayed）两类真实故障。

页面剧本按队提供：字段 OCR 文本（可给显式 (text, y) token 序列）、
位置标签、白樱花、伤势各类别真实分数、卡面涂装（占位纹理/空位纯白）。
花数通道用仓库自带的刀种花数模板合成帧做真实 cv2 校准。

伤势配置形状与 touken_config.example.json 完全一致（中文键），事件断言
用英文稳定枚举。花数证据受 _PROVEN_FLOWER_COMBOS 白名单约束。

JSON 事实优先用例（GameFactsFirstTests）：注入合成游戏数据事件流
（形状与 tests/test_game_state.py 相同），验证身份/等级/生存/疲劳/
伤势以 sword_facts.parties 等事实为准、缺失/过期逐字段回退视觉。

真机运行帧校准用例（RealFrameCalibrationTests）：帧取自本机数据目录
debug/research（不提交仓库），缺帧时自动跳过。
"""

import os
import time
import unittest
from pathlib import Path

import numpy as np

from touken.flows.team_roster import (
    TeamRosterMixin, _ROW_CY, _TEAM_TAB, _BADGE_THRESHOLD,
    _PROVEN_FLOWER_COMBOS, ROW_CELL_ROIS, _slot_name_from_combined,
    _roster_facts_from_events, link_visible_slot)

_REPO = Path(__file__).resolve().parents[1]
_DEV_FRAMES = Path(os.environ.get("LOCALAPPDATA", "")) / "Maamaru-Dev" / "debug" / "research"


def _load_png(path):
    import cv2
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8),
                        cv2.IMREAD_COLOR)


class _P:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _Page:
    """一支部队的编队页剧本。"""

    def __init__(self, frame, rows=None, labels=None, sakura=(),
                 stamp_scores=None):
        self.frame = frame
        self.rows = rows or {}              # {cy: {field: 文本 或 [(text,y),...]}}
        self.labels = labels or {}          # {cy: "四之一"}
        self.sakura = set(sakura)
        self.stamp_scores = stamp_scores or {}  # {cy: {伤势键: 真实分}}


class _RosterMaa:
    """点击驱动的编队页状态机假设备。

    页面只由 click(部队标签) 切换：
      - swallow 集合里的队号模拟"点击被吞"，永远停在当前页；
      - delayed 集合里的队号模拟"延迟一拍"，第 2 次 force 截图才切过去。
    切队确认读的是当前页的位置标签，读错队会直接暴露。
    """

    def __init__(self, pages, current=1):
        self.pages = pages                  # {team_no: _Page}
        self.current = current
        self.clicks = []
        self.swallow = set()
        self.delayed = set()
        self._pending = None                # [team, 剩余拍数]
        self.resource_dir = str(_REPO / "resource" / "base")

    def click(self, point):
        self.clicks.append((point.x, point.y))
        for team, (tx, ty) in _TEAM_TAB.items():
            if (point.x, point.y) == (tx, ty):
                if team in self.swallow or team == self.current:
                    return
                if team in self.delayed:
                    if self._pending is None:
                        self._pending = [team, 2]  # 延迟切页；连点不重置倒计时
                else:
                    self.current = team
                return

    def screenshot(self, force=False):
        if force and self._pending is not None:
            team, ticks = self._pending
            ticks -= 1
            if ticks <= 0:
                self.current = team
                self._pending = None
            else:
                self._pending = [team, ticks]
        return self.pages[self.current].frame

    def ocr_all(self, roi, image=None):
        page = self.pages[self.current]
        x, y = roi.x, roi.y
        for cy, label in page.labels.items():
            if abs(x - 20) < 5 and abs(y - (cy - 48)) < 6:
                return [(label, _P(40, cy - 30))]
        for cy, fields in page.rows.items():
            slot = _ROW_CY.index(cy) + 1
            combined = ROW_CELL_ROIS[slot]["name"]
            if x == combined[0] and y == combined[1]:
                return self._tokens(fields.get("combined_name"), cy + 20)
            if abs(x - 68) < 5 and abs(y - (cy + 5)) < 5:
                return self._tokens(fields.get("name"), cy + 20)
            if abs(x - 348) < 5 and abs(y - (cy - 42)) < 5:
                return self._tokens(fields.get("level"), cy - 30)
            if abs(x - 289) < 5 and abs(y - (cy + 6)) < 5:
                return self._tokens(fields.get("survival"), cy + 17)
            if abs(x - 290) < 5 and abs(y - (cy + 28)) < 5:
                return self._tokens(fields.get("fatigue"), cy + 38)
            if abs(x - 62) < 5 and abs(y - (cy - 50)) < 5:
                return self._tokens(fields.get("badge_char"), cy - 40)
            if abs(x - 165) < 5 and abs(y - (cy - 50)) < 5:
                return self._tokens(fields.get("stamp_ocr"), cy - 30)
        return []

    @staticmethod
    def _tokens(value, default_y):
        if value is None:
            return []
        if isinstance(value, list):     # 显式 (text, y) token 序列（测乱序）
            return [(text, _P(300, yy)) for text, yy in value]
        return [(value, _P(300, default_y))]

    def template_match(self, template, roi=None, threshold=0.7):
        y = roi.y if roi else 0
        if template.endswith("樱花.png"):
            for cy in self.pages[self.current].sakura:
                if abs(y - (cy - 50)) < 45:
                    return _P(260, cy - 40)
            return None
        hit = self._stamp_score(template, y, threshold)
        return _P(200, y + 10) if hit else None

    def template_match_score(self, template, roi=None, threshold=0.5):
        y = roi.y if roi else 0
        return self._stamp_score(template, y, threshold)

    def _stamp_score(self, template, y, threshold):
        for cy, scores in self.pages[self.current].stamp_scores.items():
            for key, score in scores.items():
                if key in template and abs(y - (cy - 50)) < 45:
                    return score if score >= threshold else 0.0
        return 0.0

class _RosterHost(TeamRosterMixin):
    def __init__(self, device):
        self.device = device
        # 与 touken_config.example.json/sortie 相同的真实形状：中文键
        self.config = {"sortie": {"injury_stamps": {
            "重伤": {"template": "battle/ui重伤.png", "threshold": 0.88},
            "中伤": {"template": "battle/中伤.png", "threshold": 0.88},
            "轻伤": {"template": "battle/轻伤.png", "threshold": 0.88},
        }}}
        self.current_location = "编队"
        self.events = []

    def record_event(self, event_type, **payload):
        self.events.append({"event_type": event_type,
                            "payload": payload})

    def navigate_to_stream(self, dest):
        self.current_location = dest
        yield f"nav→{dest}"


_CN = "一二三四五六"


def _paint_card(frame, cy, card):
    """按校准形态涂卡面立绘区：blank=纯白（空位）/ occupied=纹理（占位）。"""
    x0, y0, x1, y1 = 150, cy - 45, 290, cy + 30
    if card == "blank":
        frame[y0:y1, x0:x1] = 255
    else:  # occupied
        rng = np.random.default_rng(cy)
        frame[y0:y1, x0:x1] = rng.integers(60, 220, (y1 - y0, x1 - x0, 3),
                                           dtype=np.uint8)


def _composite_frame(cy, badge_template=None, badge_scale=1.0, card="occupied"):
    """合成单槽帧：暗底（模拟证据不足/失明页）+ 卡面涂装 + 可选真实徽章贴图。"""
    import cv2
    frame = np.full((720, 1280, 3), 50, np.uint8)
    _paint_card(frame, cy, card)
    if badge_template:
        tpl = _load_png(_REPO / "resource" / "base" / "image" / "刀种"
                        / f"{badge_template}.png")
        if badge_scale != 1.0:
            tpl = cv2.resize(tpl, None, fx=badge_scale, fy=badge_scale)
        x0 = 62 + max(0, (78 - tpl.shape[1]) // 2)
        y0 = cy - 50 + max(0, (70 - tpl.shape[0]) // 2)
        if y0 + tpl.shape[0] <= 720 and x0 + tpl.shape[1] <= 1280:
            frame[y0:y0 + tpl.shape[0], x0:x0 + tpl.shape[1]] = tpl
    return frame


def _page_frame(occupied_cys=(), badges=None, badge_scale=1.0):
    """整页帧：白底（贴近真机空卡），占位行涂立绘纹理，徽章贴真实模板。"""
    import cv2
    frame = np.full((720, 1280, 3), 255, np.uint8)
    for cy in _ROW_CY:
        _paint_card(frame, cy, "occupied" if cy in occupied_cys else "blank")
    for cy, tpl_name in (badges or {}).items():
        tpl = _load_png(_REPO / "resource" / "base" / "image" / "刀种"
                        / f"{tpl_name}.png")
        if badge_scale != 1.0:
            tpl = cv2.resize(tpl, None, fx=badge_scale, fy=badge_scale)
        x0 = 62 + max(0, (78 - tpl.shape[1]) // 2)
        y0 = cy - 50 + max(0, (70 - tpl.shape[0]) // 2)
        frame[y0:y0 + tpl.shape[0], x0:x0 + tpl.shape[1]] = tpl
    return frame


def _labels_for(team_no):
    """一支部队六行的位置标签 {"四之一": ...}。"""
    team_cn = _CN[team_no - 1]
    return {cy: f"{team_cn}之{_CN[slot - 1]}"
            for slot, cy in enumerate(_ROW_CY, start=1)}


def _make_device(pages_spec, current=1, swallow=(), delayed=(), cards=None,
              badge_scale=1.0):
    """pages_spec: {team_no: (rows, badges, sakura, stamp_scores)}。

    cards: {cy: "blank"/"flat"} 覆盖指定行的卡面涂装（默认占位纹理）。
    badge_scale: 徽章贴图缩放（测低分降级用）。
    """
    pages = {}
    for team_no, (rows, badges, sakura, stamp_scores) in pages_spec.items():
        occ = tuple(rows)
        frame = _page_frame(occ, badges, badge_scale)
        for cy, card in (cards or {}).items():
            _paint_card(frame, cy, card)
        pages[team_no] = _Page(frame, rows=rows,
                               labels=_labels_for(team_no), sakura=sakura,
                               stamp_scores=stamp_scores)
    device = _RosterMaa(pages, current=current)
    device.swallow = set(swallow)
    device.delayed = set(delayed)
    return device


def _read(cy=160, name=None, level=None, fatigue=None, survival=None,
          badge_char=None, badge_template=None, badge_scale=1.0,
          sakura=False, stamp_scores=None, card="occupied",
          combined_name=None, stamp_ocr=None):
    """读一个槽位：字段/徽章/伤势分数/章面文字/白樱花/卡面形态按剧本组合。

    stamp_scores: {伤势中文键: 真实分}（模拟脉动帧的各类别实测分）。
    stamp_ocr: 章面 OCR 读到的文字（伤势章 OCR 优先通道）。
    """
    fields = {"level": level, "fatigue": fatigue, "survival": survival,
              "badge_char": badge_char, "combined_name": combined_name,
              "stamp_ocr": stamp_ocr}
    if name is not None:
        fields["name"] = name
    scores = {cy: stamp_scores} if stamp_scores else {}
    cards = {cy: card} if card != "occupied" else None
    device = _make_device({1: ({cy: fields},
                         {cy: badge_template} if badge_template else None,
                         {cy} if sakura else set(), scores)},
                    cards=cards, badge_scale=badge_scale)
    host = _RosterHost(device)
    return host._read_roster_slot(_ROW_CY.index(cy) + 1, cy,
                                  host._roster_injury_stamps())


class MroResolutionTests(unittest.TestCase):
    """MRO 防截胡：agent 上的点名方法必须解析到 TeamRosterMixin 版本。"""

    def test_roster_methods_not_shadowed(self):
        from touken.agent import ToukenAgent
        for method in ("team_roster_stream", "_read_roster_slot",
                       "_read_badge_char", "_match_slot_flowers",
                       "_kiwame_conclusion", "_card_blank",
                       "_roster_ocr_text", "_read_slot_injury",
                       "_read_row_label", "_roster_ocr_tokens"):
            self.assertEqual(getattr(ToukenAgent, method),
                             getattr(TeamRosterMixin, method),
                             f"{method} 被其他 mixin 截胡")


class TabConfirmTests(unittest.TestCase):
    """切队正面确认：标签核对、点击被吞、延迟一拍、逐队确认。"""

    @staticmethod
    def _spec(team_no, name, badge_char):
        rows = {160: {"name": name, "level": "99 级", "fatigue": "88/100",
                      "survival": "45/45", "badge_char": badge_char}}
        return (rows, None, set(), {})

    def test_five_teams_confirmed_sequentially(self):
        pages = {1: self._spec(1, "へし切長谷部", "打"),
                 2: self._spec(2, "小夜左文字", "短"),
                 3: self._spec(3, "物吉貞宗", "脇"),
                 4: self._spec(4, "莺丸", "太"),
                 5: self._spec(5, "小豆长光", "太")}
        host = _RosterHost(_make_device(pages))
        list(host.team_roster_stream(teams=[1, 2, 3, 4, 5]))
        self.assertEqual(len(host.events), 5)
        for event, team in zip(host.events, (1, 2, 3, 4, 5)):
            self.assertEqual(event["payload"]["team_no"], team)
            self.assertEqual(event["payload"]["observation_status"], "partial")
            self.assertEqual(event["payload"]["slots"][0]["slot_status"],
                             "occupied")

    def test_swallowed_click_never_misattributes(self):
        # 点部队四被吞，页面停在一队：必须 failed、slots=[]，绝不读一队冒充四队
        pages = {1: self._spec(1, "へし切長谷部", "打"),
                 4: self._spec(4, "莺丸", "太")}
        host = _RosterHost(_make_device(pages, swallow={4}))
        list(host.team_roster_stream(teams=[4]))
        self.assertEqual(len(host.events), 1)
        payload = host.events[0]["payload"]
        self.assertEqual(payload["observation_status"], "failed")
        self.assertEqual(payload["slots"], [])
        self.assertIn("切队未确认", payload["fail_reason"])
        self.assertNotIn("莺丸", repr(payload))

    def test_delayed_switch_confirmed_on_retry(self):
        # 慢一拍才切成功：第一次标签核对失败，重试后确认，正常落账
        pages = {1: self._spec(1, "へし切長谷部", "打"),
                 4: self._spec(4, "莺丸", "太")}
        host = _RosterHost(_make_device(pages, delayed={4}))
        list(host.team_roster_stream(teams=[4]))
        self.assertEqual(len(host.events), 1)
        payload = host.events[0]["payload"]
        self.assertEqual(payload["observation_status"], "partial")
        self.assertEqual(payload["slots"][0]["name"], "莺丸")

    def test_swallowed_click_between_teams_not_misattributed(self):
        # 连跑两队、第二队点击被吞：第一队数据不得被记到第二队名下
        pages = {1: self._spec(1, "へし切長谷部", "打"),
                 2: self._spec(2, "小夜左文字", "短")}
        host = _RosterHost(_make_device(pages, swallow={2}))
        list(host.team_roster_stream(teams=[1, 2]))
        self.assertEqual(len(host.events), 2)
        self.assertEqual(host.events[0]["payload"]["team_no"], 1)
        self.assertEqual(host.events[0]["payload"]["slots"][0]["name"],
                         "压切长谷部")
        second = host.events[1]["payload"]
        self.assertEqual(second["observation_status"], "failed")
        self.assertNotIn("压切长谷部", repr(second))


class TeamRosterFlowTests(unittest.TestCase):
    """事件契约 + 只读约束。"""

    def _run_two_team_flow(self, swallow=()):
        spec = {
            1: ({160: {"name": "へし切長谷部", "level": "99 级",
                       "fatigue": "88/100", "survival": "45/45",
                       "badge_char": "打"}}, None, set(), {}),
            2: ({160: {"name": "三日月宗近", "level": "95 级",
                       "fatigue": "66/100", "survival": "38/42",
                       "badge_char": "太"}}, None, set(), {}),
        }
        host = _RosterHost(_make_device(spec, swallow=swallow))
        list(host.team_roster_stream(teams=[1, 2]))
        return host

    def test_event_contract(self):
        host = self._run_two_team_flow()
        self.assertEqual(len(host.events), 2)
        event = host.events[0]
        self.assertEqual(event["event_type"], "team_roster.observed")
        self.assertEqual(event["payload"]["team_no"], 1)
        self.assertEqual(event["payload"]["source"], "formation_page")
        self.assertIn(event["payload"]["observation_status"],
                      ("complete", "partial", "failed"))
        self.assertNotIn("observed_at", event["payload"])
        self.assertEqual(len(event["payload"]["slots"]), 6)

    def test_slot_contract_fields(self):
        host = self._run_two_team_flow()
        slot = host.events[0]["payload"]["slots"][0]
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertEqual(slot["name"], "压切长谷部")
        self.assertEqual(slot["sword_catalog_id"], "touken_118_heshikiri_hasebe")
        self.assertEqual(slot["sword_type"], "打刀")
        self.assertEqual(slot["rarity_base"], 2)
        self.assertEqual(slot["level"], 99)
        self.assertEqual(slot["fatigue"], 88)
        self.assertEqual(slot["survival"], 45)
        self.assertEqual(slot["survival_max"], 45)
        self.assertEqual(slot["injury"], "none")
        self.assertIsInstance(slot["unknown_fields"], list)

    def test_empty_slots_have_positive_blank_evidence(self):
        host = self._run_two_team_flow()
        for slot in host.events[0]["payload"]["slots"][1:]:
            self.assertEqual(slot["slot_status"], "empty")
            self.assertEqual(slot["unknown_fields"], [])

    def test_only_tab_clicks(self):
        # 只读约束：全程点击只落在部队标签坐标
        host = self._run_two_team_flow()
        self.assertTrue(set(host.device.clicks) <= set(_TEAM_TAB.values()))

    def test_nav_failure_records_failed_event(self):
        device = _make_device({1: ({}, None, set(), {})})
        host = _RosterHost(device)
        host.current_location = "本丸"  # 导航失败：到达地不是编队

        def broken_nav(dest):
            return
            yield  # pragma: no cover

        host.navigate_to_stream = broken_nav
        list(host.team_roster_stream(teams=[1]))
        self.assertEqual(len(host.events), 1)
        payload = host.events[0]["payload"]
        self.assertEqual(payload["observation_status"], "failed")
        self.assertEqual(payload["slots"], [])
        self.assertTrue(payload["fail_reason"])


class SlotFieldTests(unittest.TestCase):
    """单槽读取：字段降级 / 疲劳按坐标 / 空位正面证据 / unknown。"""

    def test_occupied_with_all_fields(self):
        slot = _read(name="压切长谷部", level="99 级", fatigue="85/100",
                     survival="45/45", badge_char="打", stamp_scores=None)
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertEqual(slot["name"], "压切长谷部")
        self.assertEqual(slot["sword_type"], "打刀")
        self.assertEqual(slot["level"], 99)
        self.assertEqual(slot["fatigue"], 85)
        self.assertEqual(slot["survival"], 45)
        self.assertEqual(slot["survival_max"], 45)
        self.assertEqual(slot["injury"], "none")  # 满血正面证明的无伤

    def test_fatigue_picked_by_y_not_token_order(self):
        # 生存 100/100 蹭进 ROI：不管 OCR 返回顺序如何，疲劳都取 y 更靠下的 88
        rev = [("疲劳 88/100", 198), ("生存 100/100", 174)]  # 返回顺序颠倒
        slot = _read(name="压切长谷部", level="99 级", fatigue=rev,
                     survival="45/45", badge_char="打")
        self.assertEqual(slot["fatigue"], 88)
        fwd = [("生存 100/100", 174), ("疲劳 88/100", 198)]
        slot = _read(name="压切长谷部", level="99 级", fatigue=fwd,
                     survival="45/45", badge_char="打")
        self.assertEqual(slot["fatigue"], 88)

    def test_survival_100_can_never_masquerade_as_fatigue(self):
        # 生存 100/100 无论 token 顺序、哪怕分母恰为 100，
        # 都在疲劳行带之外 → 疲劳 unknown，绝不安 100
        for rev in (False, True):
            toks = [("生存 100/100", 174), ("生存 100/100", 184)]
            if rev:
                toks = toks[::-1]
            slot = _read(name="压切长谷部", level="99 级", fatigue=toks,
                         survival="45/45", badge_char="打")
            self.assertIsNone(slot["fatigue"], rev)

    def test_injury_none_requires_positive_survival(self):
        # 章未命中且生存读不出 → None(unknown)，绝不假装无伤
        slot = _read(name="压切长谷部", level="99 级", fatigue="85/100",
                     survival=None, badge_char="打")
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertIsNone(slot["injury"])
        self.assertIn("injury", slot["unknown_fields"])

    def test_injury_below_max_without_stamp_is_unknown(self):
        slot = _read(name="压切长谷部", level="99 级", fatigue="85/100",
                     survival="30/45", badge_char="打")
        self.assertIsNone(slot["injury"])

    def test_injury_best_class_wins_with_margin(self):
        # 中伤真实分最高、与重伤差距足够 → medium
        slot = _read(name="莺丸", badge_char="太",
                     stamp_scores={"重伤": 0.62, "中伤": 0.89, "轻伤": 0.40})
        self.assertEqual(slot["injury"], "medium")

    def test_injury_close_candidates_degrade_to_unknown(self):
        # 重伤弱命中蹭到 0.85：与中伤只差 0.04，区分度不足 → unknown
        slot = _read(name="莺丸", badge_char="太",
                     stamp_scores={"重伤": 0.85, "中伤": 0.89})
        self.assertIsNone(slot["injury"])
        self.assertIn("injury", slot["unknown_fields"])

    def test_injury_unproven_class_stays_unknown(self):
        # 编队页真帧只有中伤样本：最高分是重伤也不硬判（同源规矩）
        slot = _read(name="莺丸", badge_char="太",
                     stamp_scores={"重伤": 0.91, "中伤": 0.55})
        self.assertIsNone(slot["injury"])

    def test_injury_below_threshold_unknown(self):
        slot = _read(name="莺丸", badge_char="太",
                     stamp_scores={"中伤": 0.60})
        self.assertIsNone(slot["injury"])

    def test_injury_ocr_word_beats_template_channel(self):
        # 章面文字直接读出：不依赖模板分，模板通道整段不问
        slot = _read(name="莺丸", badge_char="太", stamp_ocr="中伤",
                     stamp_scores={"重伤": 0.91})
        self.assertEqual(slot["injury"], "medium")

    def test_injury_ocr_heavy_accepted_on_text_evidence(self):
        # 模板通道对编队页未证明类别一律 unknown；OCR 读到「重伤」
        # 两字本身就是证据，不受同源清单限制
        slot = _read(name="莺丸", badge_char="太", stamp_ocr="重伤")
        self.assertEqual(slot["injury"], "heavy")

    def test_injury_ocr_fragment_stays_unknown(self):
        # 单个「伤」残片不推断等级，模板也没分 → unknown
        slot = _read(name="莺丸", badge_char="太", stamp_ocr="伤")
        self.assertIsNone(slot["injury"])
        self.assertIn("injury", slot["unknown_fields"])

    def test_tactical_role_with_real_config_shape(self):
        # 真实配置形状回归：中伤 + 极化 + 太刀 → 碰瓷角色（英文枚举）
        slot = _read(name="莺丸", badge_char="太",
                     badge_template="五花太刀",
                     stamp_scores={"中伤": 0.89})
        self.assertEqual(slot["injury"], "medium")
        self.assertEqual(slot["kiwame_status"], "kiwame")
        self.assertEqual(slot["tactical_roles"],
                         ["medium_injury_kiwame_tachi"])

    def test_no_tactical_role_unless_all_three_confirmed(self):
        slot = _read(name="压切长谷部", badge_char="打",
                     stamp_scores={"中伤": 0.89})
        self.assertEqual(slot["tactical_roles"], [])
        slot = _read(name="明石国行", badge_char="太",
                     badge_template="三花太刀", stamp_scores={"中伤": 0.89})
        self.assertEqual(slot["tactical_roles"], [])

    def test_name_failure_degrades_only_name(self):
        slot = _read(name="乱码丼丼", level="99 级", fatigue="85/100",
                     survival="45/45", badge_char="打")
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertIsNone(slot["name"])
        self.assertEqual(slot["name_status"], "unrecognized")
        self.assertEqual(slot["level"], 99)
        self.assertEqual(slot["fatigue"], 85)
        self.assertIn("name", slot["unknown_fields"])

    def test_level_failure_degrades_only_level(self):
        slot = _read(name="压切长谷部", level=None, fatigue="85/100",
                     survival="45/45", badge_char="打")
        self.assertIsNone(slot["level"])
        self.assertEqual(slot["fatigue"], 85)
        self.assertEqual(slot["slot_status"], "occupied")

    def test_all_silent_blank_card_is_empty(self):
        slot = _read(card="blank")
        self.assertEqual(slot["slot_status"], "empty")
        self.assertIsNone(slot["kiwame_status"])
        self.assertEqual(slot["unknown_fields"], [])

    def test_blind_page_is_unknown_not_empty(self):
        # 整页失明（暗且平的帧，OCR 全哑）：不得判空位 → unknown
        slot = _read(card="flat")
        self.assertEqual(slot["slot_status"], "unknown")
        self.assertIn("name", slot["unknown_fields"])
        self.assertIn("kiwame_status", slot["unknown_fields"])

    def test_badge_char_present_blank_card_is_unknown(self):
        slot = _read(badge_char="太", card="blank")
        self.assertEqual(slot["slot_status"], "unknown")

    def test_injury_stamp_present_blank_card_is_unknown(self):
        slot = _read(stamp_scores={"中伤": 0.89}, card="blank")
        self.assertEqual(slot["slot_status"], "unknown")

    def test_garbled_content_is_unknown(self):
        slot = _read(name="丼丼", badge_char="丼")
        self.assertEqual(slot["slot_status"], "unknown")
        self.assertEqual(slot["kiwame_status"], "unknown")


class CombinedSlotEvidenceTests(unittest.TestCase):
    def test_position_prefix_and_empty_candidate(self):
        self.assertEqual(_slot_name_from_combined("四鹤丸国永", 4),
                         ("鹤丸国永", False))
        self.assertEqual(_slot_name_from_combined("四", 4), (None, False))
        self.assertEqual(_slot_name_from_combined("三鹤丸国永", 4),
                         (None, True))

    def test_label_only_requires_blank_card(self):
        self.assertEqual(_read(cy=455, card="blank", combined_name="四")
                         ["slot_status"], "empty")
        self.assertEqual(_read(cy=455, card="occupied", combined_name="四")
                         ["slot_status"], "unknown")

    def test_visible_fingerprint_links_only_unique_complete_instance(self):
        keys = ("生存", "打击", "防御", "机动", "冲力", "侦察", "隐蔽", "必杀")
        stats = {key: index + 50 for index, key in enumerate(keys)}
        slot = {"slot_status": "occupied", "sword_catalog_id": "same-sword",
                "level": 91, "tou_level": 9, "survival_max": 73,
                "stats": stats}
        entries = [dict(slot, observation_id="snapshot:1"),
                   dict(slot, observation_id="snapshot:2",
                        stats={**stats, "侦察": 99})]
        self.assertEqual(link_visible_slot(slot, entries),
                         {"status": "linked", "observation_id": "snapshot:1"})
        self.assertEqual(link_visible_slot({**slot, "level": 92}, entries),
                         {"status": "linked", "observation_id": "snapshot:1"})
        self.assertEqual(link_visible_slot(slot, entries + [dict(entries[0])]),
                         {"status": "ambiguous", "observation_id": None})
        self.assertEqual(link_visible_slot({**slot, "level": 92},
                                           entries + [dict(entries[0],
                                                           observation_id="snapshot:3")]),
                         {"status": "ambiguous", "observation_id": None})
        self.assertEqual(link_visible_slot({**slot, "stats": {"生存": 73}}, entries),
                         {"status": "insufficient", "observation_id": None})
        self.assertEqual(link_visible_slot({**slot, "level": 90}, entries),
                         {"status": "stale", "observation_id": None})

    def test_other_stats_do_not_identify_non_cultivation_swords(self):
        stats = {"生存": 55, "侦察": 39, "打击": 80, "防御": 80,
                 "机动": 80, "冲力": 80, "隐蔽": 80, "必杀": 80}
        slot = {"slot_status": "occupied", "sword_catalog_id": "same-sword",
                "level": 91, "tou_level": 9, "survival_max": 55,
                "stats": {**stats, "打击": 81}}
        entries = [dict(slot, observation_id="snapshot:1", stats=stats)]
        self.assertEqual(link_visible_slot(slot, entries),
                         {"status": "linked", "observation_id": "snapshot:1"})
        entries.append(dict(entries[0], observation_id="snapshot:2",
                            stats={**stats, "打击": 90}))
        self.assertEqual(link_visible_slot(slot, entries),
                         {"status": "ambiguous", "observation_id": None})

    def test_cultivation_growth_requires_table_pair_and_six_other_stats(self):
        stats = {"生存": 55, "侦察": 39, "打击": 80, "防御": 80,
                 "机动": 80, "冲力": 80, "隐蔽": 80, "必杀": 80}
        old = {"slot_status": "occupied", "sword_catalog_id": "same-sword",
               "name_zh": "测试刀", "level": 90, "tou_level": 9,
               "survival_max": 55, "stats": stats,
               "observation_id": "snapshot:1"}
        slot = dict(old, level=91, survival_max=56,
                    stats={**stats, "生存": 56, "侦察": 40})
        self.assertEqual(link_visible_slot(slot, [old])["status"], "stale")
        table = {"测试刀": {"生存": 56, "侦察": 40}}
        self.assertEqual(link_visible_slot(slot, [old], table)["status"], "linked")
        larger = {**slot, "survival_max": 57,
                  "stats": {**slot["stats"], "生存": 57}}
        self.assertEqual(link_visible_slot(
            larger, [old], {"测试刀": {"生存": 57, "侦察": 40}})
            ["status"], "stale")
        self.assertEqual(link_visible_slot(
            larger, [old], {"oid:snapshot:1": {"生存": 57, "侦察": 40}})
            ["status"], "linked")
        self.assertEqual(link_visible_slot(
            {**slot, "stats": {**slot["stats"], "打击": 81}}, [old], table)
            ["status"], "stale")
        duplicate = dict(old, observation_id="snapshot:2")
        self.assertEqual(link_visible_slot(slot, [old, duplicate], table)
                         ["status"], "ambiguous")


class NameMatchTests(unittest.TestCase):
    """名字专用匹配：精确/唯一包含才认，多候选与乱码一律拒认。"""

    def test_head_trimmed_name_recognized(self):
        slot = _read(name="夜左文字", level="70 级", fatigue="80/100",
                     survival="40/40", badge_char="短")
        self.assertEqual(slot["name_status"], "recognized")
        self.assertEqual(slot["name"], "小夜左文字")

    def test_generic_short_name_rejected(self):
        slot = _read(name="藤四郎", level="70 级", fatigue="80/100",
                     survival="40/40", badge_char="短")
        self.assertEqual(slot["name_status"], "unrecognized")
        self.assertIsNone(slot["sword_catalog_id"])

    def test_garbled_rejected(self):
        slot = _read(name="丼丼丼", level="70 级", fatigue="80/100",
                     survival="40/40", badge_char="短")
        self.assertEqual(slot["name_status"], "unrecognized")

    def test_single_char_rejected(self):
        slot = _read(name="刀", level="70 级", fatigue="80/100",
                     survival="40/40", badge_char="短")
        self.assertEqual(slot["name_status"], "unrecognized")


class KiwameConclusionTests(unittest.TestCase):
    """极化分层单向证明：花数徽章（白名单内）/ 白樱花 / 例外表 / 刀种一致。"""

    def test_flowers_exceed_base_is_kiwame(self):
        # 莺丸：名册太刀四花；徽章五花（白名单内组合）→ 极化
        slot = _read(name="莺丸", badge_char="太",
                     badge_template="五花太刀", stamp_scores={"中伤": 0.89})
        self.assertEqual(slot["kiwame_status"], "kiwame")
        types = [e["type"] for e in slot["kiwame_evidence"]]
        self.assertIn("badge_flowers_vs_base", types)

    def test_flowers_equal_base_is_normal(self):
        # 压切长谷部：打刀二花（白名单内组合）= 名册基线 → normal
        slot = _read(name="压切长谷部", badge_char="打",
                     badge_template="二花打刀")
        self.assertEqual(slot["kiwame_status"], "normal")
        self.assertEqual(slot["badge"]["conclusion"], "recognized")

    def test_unproven_combo_yields_no_flower_evidence(self):
        # 太刀六花组合没有编队页真帧达标记录：不出证据 → unknown
        slot = _read(name="道誉一文字", badge_char="太",
                     badge_template="六花太刀")
        self.assertEqual(slot["badge"]["conclusion"], "unproven_combo")
        self.assertIsNone(slot["badge"]["flowers"])
        self.assertEqual(slot["kiwame_status"], "unknown")

    def test_dynamic_flower_exception_is_unknown(self):
        # 髭切：名册太刀二花、真帧四花白名单组合，花数超基线但不区分极化
        slot = _read(name="髭切", badge_char="太",
                     badge_template="四花太刀")
        self.assertEqual(slot["kiwame_status"], "unknown")
        self.assertEqual(slot["kiwame_evidence"][0]["conclusion"],
                         "ambiguous_dynamic_flowers")

    def test_low_score_badge_is_unknown(self):
        slot = _read(name="莺丸", badge_char="太",
                     badge_template="五花太刀", badge_scale=0.5)
        self.assertEqual(slot["kiwame_status"], "unknown")
        self.assertEqual(slot["kiwame_evidence"], [])

    def test_sakura_hit_proves_kiwame(self):
        slot = _read(name="莺丸", sakura=True)
        self.assertEqual(slot["kiwame_status"], "kiwame")
        self.assertEqual(slot["kiwame_evidence"][-1]["type"],
                         "sakura_template")

    def test_sakura_and_badge_conflict_is_unknown(self):
        # 白樱花说极化、白名单内花数证据说普通：冲突 → unknown，两条证据都留
        slot = _read(name="压切长谷部", badge_char="打",
                     badge_template="二花打刀", sakura=True)
        self.assertEqual(slot["kiwame_status"], "unknown")
        types = [e["type"] for e in slot["kiwame_evidence"]]
        self.assertIn("sakura_template", types)
        self.assertIn("badge_flowers_vs_base", types)

    def test_char_roster_type_conflict_kills_flowers(self):
        slot = _read(name="莺丸", badge_char="打",
                     badge_template="四花打刀")
        self.assertEqual(slot["kiwame_status"], "unknown")
        types = [e["type"] for e in slot["kiwame_evidence"]]
        self.assertIn("flower_type_conflict", types)
        self.assertNotIn("badge_flowers_vs_base", types)

    def test_top_template_other_type_is_conflict(self):
        slot = _read(name="莺丸", badge_template="五花枪")
        self.assertEqual(slot["kiwame_status"], "unknown")
        self.assertEqual(slot["badge"]["conclusion"], "type_conflict")
        self.assertEqual(slot["badge"]["top_type"], "枪")

    def test_no_confirmed_type_no_flower_evidence(self):
        slot = _read(name="丼丼丼丼", fatigue="80/100",
                     badge_template="五花太刀")
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertEqual(slot["badge"]["conclusion"], "no_confirmed_type")
        self.assertIsNone(slot["badge"]["flowers"])
        self.assertEqual(slot["kiwame_status"], "unknown")

    def test_wakizashi_type_normalized(self):
        # 物吉贞宗：名册脇差 → 事件枚举胁差；三花胁差白名单内、超基线 → 极化
        slot = _read(name="物吉貞宗", badge_template="三花胁差",
                     badge_char="脇")
        self.assertEqual(slot["sword_type"], "胁差")
        self.assertEqual(slot["badge"]["conclusion"], "recognized")
        self.assertEqual(slot["kiwame_status"], "kiwame")


# ---------------------------------------------------------------- JSON 事实优先

def _json_event(body, endpoint="/party/list", at=None):
    """合成游戏数据事件（形状与 tests/test_game_state.py 的 event 相同）。"""
    return {"direction": "S->C", "status": 200, "endpoint": endpoint,
            "payload": {"status": 0,
                        "now_time": time.time() if at is None else at,
                        **body}}


def _party_events(now, *, team_no=1, serial="111", sword_id=118, level="99",
                  hp="45", hp_max="45", fatigue="88",
                  slots=("111", None, None, None, None, None)):
    """一份 /party/list 全量快照：默认 1 号槽 serial 111，其余空槽。"""
    sword = {serial: {"serial_id": serial, "sword_id": sword_id,
                      "level": level, "hp": hp, "hp_max": hp_max,
                      "fatigue": fatigue, "protect": "1"}}
    slot = {str(i + 1): {"serial_id": s} for i, s in enumerate(slots)}
    return [_json_event(
        {"sword": sword, "party": {str(team_no): {"slot": slot}}}, at=now)]


class _FactsHost(_RosterHost):
    """注入固定 JSON 事件流的点名宿主（覆写事件源注入缝）。"""

    def __init__(self, device, events):
        super().__init__(device)
        self._facts_events = events

    def _roster_game_events(self):
        return self._facts_events


def _facts_host(now, *, events=None, rows=None, pages_team=1):
    """JSON 事件（默认一份新鲜全量快照）+ 指定页面剧本的点名宿主。"""
    pages = {pages_team: (rows or {}, None, set(), {})}
    device = _make_device(pages, current=pages_team)
    return _FactsHost(device,
                      _party_events(now) if events is None else events)


class RosterFactsPureTests(unittest.TestCase):
    """_roster_facts_from_events：新鲜度/战后时效/空槽/身份映射纯函数判定。"""

    def test_unknown_team_returns_none(self):
        now = time.time()
        self.assertIsNone(
            _roster_facts_from_events(_party_events(now), 9, now=now))

    def test_occupied_and_empty_slots(self):
        now = time.time()
        events = _party_events(now, slots=("111", "0", None))
        facts = _roster_facts_from_events(events, 1, now=now)
        self.assertTrue(facts[1]["occupied"])
        self.assertEqual(facts[1]["sword_catalog_id"],
                         "touken_118_heshikiri_hasebe")
        self.assertEqual(facts[1]["level"], 99)
        self.assertEqual(facts[1]["fatigue"], 88)
        self.assertEqual(facts[1]["survival"], 45)
        self.assertEqual(facts[1]["survival_max"], 45)
        self.assertEqual(facts[1]["injury"], "none")
        self.assertFalse(facts[2]["occupied"])   # serial "0" 也是空槽
        self.assertFalse(facts[3]["occupied"])   # serial None 也是空槽

    def test_stale_membership_nulls_whole_slot(self):
        now = time.time()
        facts = _roster_facts_from_events(_party_events(now - 700), 1,
                                          now=now)
        self.assertIsNone(facts[1])

    def test_unmappable_sword_id_keeps_other_fields(self):
        # sword_id 映射不进目录：身份缺省，其余字段不受影响
        now = time.time()
        facts = _roster_facts_from_events(
            _party_events(now, sword_id=999999), 1, now=now)
        self.assertNotIn("sword_catalog_id", facts[1])
        self.assertEqual(facts[1]["level"], 99)
        self.assertEqual(facts[1]["injury"], "none")

    def test_pre_combat_hp_is_rejected(self):
        now = time.time()
        events = (_party_events(now - 60, hp="23", hp_max="79")
                  + [_json_event({}, "/battle/pve", at=now - 30)])
        facts = _roster_facts_from_events(events, 1, now=now)
        self.assertNotIn("survival", facts[1])
        self.assertNotIn("injury", facts[1])
        self.assertNotIn("fatigue", facts[1])


class GameFactsFirstTests(unittest.TestCase):
    """JSON 数据优先、视觉兜底：身份/等级/生存/疲劳/伤势以 sword_facts 为准。"""

    def test_json_identity_beats_garbled_ocr(self):
        # 页面名字/数值全是乱码：JSON 身份与字段照样落，且不列 unknown
        now = time.time()
        rows = {160: {"name": "丼丼丼", "level": "11 级",
                      "fatigue": "11/100", "survival": "11/11"}}
        slots = _facts_host(now, rows=rows)._read_team_page()
        s0 = slots[0]
        self.assertEqual(s0["slot_status"], "occupied")
        self.assertEqual(s0["name"], "压切长谷部")
        self.assertEqual(s0["name_status"], "recognized")
        self.assertEqual(s0["sword_catalog_id"],
                         "touken_118_heshikiri_hasebe")
        self.assertEqual(s0["sword_type"], "打刀")
        self.assertEqual(s0["rarity_base"], 2)
        self.assertEqual(s0["level"], 99)
        self.assertEqual(s0["fatigue"], 88)
        self.assertEqual(s0["survival"], 45)
        self.assertEqual(s0["survival_max"], 45)
        self.assertEqual(s0["injury"], "none")
        for field in ("name", "level", "fatigue", "survival", "injury"):
            self.assertNotIn(field, s0["unknown_fields"])
        for s in slots[1:]:
            self.assertEqual(s["slot_status"], "empty")

    def test_json_injury_tiers_without_stamp_channel(self):
        # 伤势由 hp 换算直接给（含满血 none），伤势章通道整段不用跑
        now = time.time()
        cases = (("45", "45", "none"), ("53", "79", "light"),
                 ("27", "79", "medium"), ("26", "79", "medium"),
                 ("25", "77", "medium"), ("23", "77", "heavy"))
        for hp, hp_max, expected in cases:
            with self.subTest(hp=hp, hp_max=hp_max):
                rows = {160: {"name": "x"}}
                host = _facts_host(now, rows=rows,
                                   events=_party_events(now, hp=hp,
                                                        hp_max=hp_max))
                slot = host._read_team_page()[0]
                self.assertEqual(slot["injury"], expected)
                self.assertEqual(slot["survival"], int(hp))
                self.assertEqual(slot["survival_max"], int(hp_max))

    def test_stale_json_falls_back_to_vision(self):
        # 全量快照过期（>600s）：整槽回退视觉 OCR，满血正面证明仍成立
        now = time.time()
        rows = {160: {"name": "压切长谷部", "level": "99 级",
                      "fatigue": "85/100", "survival": "45/45",
                      "badge_char": "打"}}
        slot = _facts_host(now - 700, rows=rows)._read_team_page()[0]
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertEqual(slot["name"], "压切长谷部")
        self.assertEqual(slot["level"], 99)
        self.assertEqual(slot["fatigue"], 85)
        self.assertEqual(slot["injury"], "none")

    def test_partial_json_degrades_per_field(self):
        # 成员关系+疲劳新鲜（setsword 后又有稀疏疲劳更新），hp/等级/身份
        # 停在 700 秒前的全量快照：疲劳吃 JSON，其余本字段回退视觉
        now = time.time()
        slot_update = {"1": {"slot": {str(i + 1): {"serial_id": s}
                                      for i, s in enumerate(
                                          ("111", None, None, None, None,
                                           None))}}}
        events = (_party_events(now - 700, fatigue="10")
                  + [_json_event({"sword": {"111": {"fatigue": 66}}},
                                 "/repair/complete", at=now - 1),
                     _json_event(slot_update, "/party/setsword", at=now)])
        rows = {160: {"name": "压切长谷部", "level": "99 级",
                      "fatigue": "85/100", "survival": "30/45",
                      "badge_char": "打"}}
        slot = _facts_host(now, rows=rows, events=events)._read_team_page()[0]
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertEqual(slot["name"], "压切长谷部")   # sword_id 过期 → OCR 认人
        self.assertEqual(slot["level"], 99)            # level 过期 → OCR
        self.assertEqual(slot["fatigue"], 66)          # JSON 新鲜，盖过 OCR 85
        self.assertEqual((slot["survival"], slot["survival_max"]), (30, 45))
        self.assertIsNone(slot["injury"])
        self.assertIn("injury", slot["unknown_fields"])

    def test_json_empty_slot_overrides_visual_content(self):
        # 二槽 JSON 说空：卡面涂着占位纹理、OCR 有乱码内容也判 empty
        now = time.time()
        rows = {160: {"name": "压切长谷部", "level": "99 级",
                      "fatigue": "85/100", "survival": "45/45",
                      "badge_char": "打"},
                258: {"name": "幽灵丼", "level": "1 级",
                      "fatigue": "5/100", "survival": "1/1"}}
        slots = _facts_host(now, rows=rows)._read_team_page()
        self.assertEqual(slots[0]["slot_status"], "occupied")
        self.assertEqual(slots[1]["slot_status"], "empty")

    def test_facts_anchor_to_visual_team_label(self):
        # 页面标签=部队二 + 事件里二队数据：JSON 生效；页面标签=部队一
        # + 事件只有二队：绝不用二队数据充数，全槽回退视觉
        now = time.time()
        rows = {160: {"name": "丼丼", "level": "1 级",
                      "fatigue": "1/100", "survival": "1/1"}}
        events = _party_events(now, team_no=2)
        host = _facts_host(now, rows=rows, pages_team=2, events=events)
        slot = host._read_team_page()[0]
        self.assertEqual(slot["name"], "压切长谷部")
        self.assertEqual(slot["level"], 99)
        host = _facts_host(now, rows=rows, events=events)
        slot = host._read_team_page()[0]
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertIsNone(slot["name"])
        self.assertEqual(slot["name_status"], "unrecognized")
        self.assertEqual(slot["level"], 1)
        self.assertEqual(slot["fatigue"], 1)

    def test_no_events_is_pure_visual(self):
        now = time.time()
        rows = {160: {"name": "压切长谷部", "level": "99 级",
                      "fatigue": "85/100", "survival": "45/45",
                      "badge_char": "打"}}
        slot = _facts_host(now, rows=rows, events=[])._read_team_page()[0]
        self.assertEqual(slot["name"], "压切长谷部")
        self.assertEqual(slot["fatigue"], 85)

    def test_pre_combat_json_hp_falls_back_to_stamp_channel(self):
        # hp/疲劳快照都在战斗之前：JSON 伤势与疲劳作废，章通道+OCR 兜底
        # （JSON 本会说 heavy，章命中 medium 证明吃到的真是视觉链）
        now = time.time()
        events = (_party_events(now - 60, hp="26", hp_max="79")
                  + [_json_event({}, "/battle/pve", at=now - 30)])
        rows = {160: {"name": "压切长谷部", "level": "99 级",
                      "fatigue": "40/100", "survival": "23/79",
                      "badge_char": "打"}}
        pages = {1: (rows, None, set(), {160: {"中伤": 0.89}})}
        host = _FactsHost(_make_device(pages), events)
        slot = host._read_team_page()[0]
        self.assertEqual(slot["injury"], "medium")
        self.assertEqual((slot["survival"], slot["survival_max"]), (23, 79))
        self.assertEqual(slot["fatigue"], 40)
        self.assertEqual(slot["level"], 99)

    def test_unreadable_label_disables_json(self):
        # 行首标签读不出 → 部队锚不定 → JSON 整页不接，回退视觉
        now = time.time()
        rows = {160: {"name": "压切长谷部", "level": "99 级",
                      "fatigue": "85/100", "survival": "45/45",
                      "badge_char": "打"}}
        page = _Page(_page_frame((160,)), rows=rows, labels={},
                     sakura=set(), stamp_scores={})
        host = _FactsHost(_RosterMaa({1: page}, current=1),
                          _party_events(now))
        slot = host._read_team_page()[0]
        self.assertEqual(slot["slot_status"], "occupied")
        self.assertEqual(slot["fatigue"], 85)


class RealFrameCalibrationTests(unittest.TestCase):
    """真实运行帧校准证据（本机 debug/research 五帧；缺帧自动跳过）。

    校准事实（2026-09-13 真机 30 槽）：空位卡面 std≤9.6/暗像素=0/亮度≥252，
    占位卡 std≥51。花数白名单 _PROVEN_FLOWER_COMBOS 必须与真帧实测同步：
    真帧上达标的组合 ⊆ 白名单（证据不出黑名单）。
    """

    TEAM_FRAMES = ((1, "nav_编队.png"), (2, "tap_274_91.png"),
                   (3, "tap_394_91.png"), (4, "tap_516_91.png"),
                   (5, "tap_638_91.png"))

    # 各队各槽确认刀种（2026-09-13 真机验收实测，空槽为 None）
    SLOT_TYPES = {
        1: ["打刀", None, None, None, None, None],
        2: ["短刀"] * 6,
        3: ["胁差", "太刀", "短刀", "打刀", "太刀", "短刀"],
        4: ["打刀", "太刀", "太刀", "太刀", None, "太刀"],
        5: ["太刀", "枪", "打刀", "打刀", "枪", None],
    }

    @classmethod
    def setUpClass(cls):
        import cv2
        if not _DEV_FRAMES.exists():
            raise unittest.SkipTest("本机无真机运行帧，跳过校准用例")
        cls.frames = {}
        for team, fname in cls.TEAM_FRAMES:
            path = _DEV_FRAMES / fname
            if not path.exists():
                raise unittest.SkipTest(f"缺真机帧 {fname}")
            cls.frames[team] = cv2.imdecode(
                np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)

    def _host(self, team):
        page = _Page(self.frames[team])
        device = _RosterMaa({team: page}, current=team)
        return _RosterHost(device)

    def test_blank_classification_matches_real_occupancy(self):
        host = self._host(1)
        self.assertFalse(host._card_blank(160))
        for cy in _ROW_CY[1:]:
            self.assertTrue(host._card_blank(cy), cy)
        host4 = self._host(4)
        self.assertTrue(host4._card_blank(553))
        self.assertFalse(host4._card_blank(652))
        host5 = self._host(5)
        self.assertTrue(host5._card_blank(652))
        self.assertFalse(host5._card_blank(553))

    def test_flower_evidence_on_real_frames_stays_inside_whitelist(self):
        # 真帧上"确认刀种下达标"的组合必须 ⊆ 白名单；白名单组合真帧真能读出
        hit_on_frames = set()
        for team, types in self.SLOT_TYPES.items():
            host = self._host(team)
            for slot, sword_type in enumerate(types, start=1):
                if sword_type is None:
                    continue
                cy = _ROW_CY[slot - 1]
                badge = host._match_slot_flowers(
                    cy, host._read_badge_char(cy), sword_type)
                if badge["conclusion"] == "recognized":
                    combo = (sword_type, badge["flowers"])
                    self.assertIn(combo, _PROVEN_FLOWER_COMBOS,
                                  f"真帧达标组合 {combo} 不在白名单")
                    hit_on_frames.add(combo)
        self.assertTrue({"打刀", "太刀"} <= {t for t, _ in hit_on_frames})

    def test_key_combos_read_on_real_frames(self):
        host4 = self._host(4)
        badge = host4._match_slot_flowers(357, host4._read_badge_char(357), "太刀")
        self.assertEqual(badge["flowers"], 5)      # 莺丸（极太五花）
        badge = host4._match_slot_flowers(258, host4._read_badge_char(258), "太刀")
        self.assertEqual(badge["flowers"], 4)      # 烛台切光忠（极太）
        host1 = self._host(1)
        badge = host1._match_slot_flowers(160, host1._read_badge_char(160), "打刀")
        self.assertEqual(badge["flowers"], 2)      # 压切长谷部（普打二花）

    def test_low_score_swords_stay_unknown_on_real_frames(self):
        host4 = self._host(4)
        for cy in (455, 652):   # 三日月宗近 / 明石国行：真机低分
            badge = host4._match_slot_flowers(
                cy, host4._read_badge_char(cy), "太刀")
            self.assertEqual(badge["flowers"], None, cy)
            self.assertLess(badge["score"] or 0, _BADGE_THRESHOLD, cy)


if __name__ == "__main__":
    unittest.main()
