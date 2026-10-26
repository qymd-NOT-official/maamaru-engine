# -*- coding: utf-8 -*-
"""刷花与出阵自动换队长的纯逻辑测试（假设备，不碰真机）。"""

import unittest
from unittest.mock import patch

from touken.flows.sakura import SakuraMixin, _parse_fatigue_text
from touken.device_adapter import Point


class ParseFatigueTests(unittest.TestCase):
    def test_denominator_100_wins_over_survival_line(self):
        # roi 蹭到上面"生存 xx/xx"那行时，分母 100 的才是疲劳
        self.assertEqual(_parse_fatigue_text("生存 55/60 疲劳 88/100"), 88)

    def test_plain_fatigue(self):
        self.assertEqual(_parse_fatigue_text("疲劳 100/100"), 100)

    def test_no_100_denominator_falls_back_to_first_pair(self):
        self.assertEqual(_parse_fatigue_text("12/34"), 12)

    def test_garbage_is_none(self):
        self.assertIsNone(_parse_fatigue_text("啥也没有"))


# 部队选择页实测的疲劳行 y（2026-09-21 模板工坊帧校准，行距 ~94.5px）
_FY = {1: 199, 2: 293, 3: 388, 4: 483, 5: 577, 6: 672}


class Maa:
    """整列 OCR 假实现：疲劳列一把全给（带 y），名字列按 fy-32 归位；
    swipe 后切换成 after_rows/after_names 模拟拖动结果。"""

    def __init__(self, rows, after_rows=None, names=None, after_names=None):
        self.rows = rows
        self.after_rows = after_rows
        self.names = names or {}
        self.after_names = after_names
        self.swipes = []
        self.clicks = []

    def screenshot(self, force=False):
        pass

    def click(self, point):
        self.clicks.append(point)

    def swipe(self, x1, y1, x2, y2, duration_ms=400):
        self.swipes.append((x1, y1, x2, y2, duration_ms))
        if self.after_rows is not None:
            self.rows = self.after_rows
        if self.after_names is not None:
            self.names = self.after_names

    def ocr_all(self, roi):
        if roi.x == 285:  # 疲劳整列：一把全给
            return [(f"疲劳 {v}/100", Point(300, _FY[s]))
                    for s, v in sorted(self.rows.items())]
        if roi.x == 100:  # 名字列：roi.y = 疲劳行y - 32
            for slot, fy in _FY.items():
                if abs(roi.y - (fy - 32)) <= 2:
                    name = self.names.get(slot)
                    return [(name, Point(120, roi.y))] if name else []
            return []


def _flow(device):
    flow = SakuraMixin()
    flow.device = device
    return flow


FULL = {1: 80, 2: 60, 3: 90, 4: 30, 5: 70, 6: 85}


def _rotate(device, margin=10):
    from unittest.mock import patch
    with patch("touken.flows.sakura.time.sleep"):
        return list(_flow(device)._rotate_captain_here(margin=margin))


class RotateCaptainTests(unittest.TestCase):
    def test_lowest_fatigue_is_dragged_to_captain(self):
        device = Maa(dict(FULL), after_rows={**FULL, 1: 30}, names={4: "小狐丸"})
        messages = _rotate(device)

        # 从 4 号位（fy=483 → 行心 447）拖到队长位（fy=199 → 行心 163）
        self.assertEqual(device.swipes, [(200, 447, 200, 163, 1000)])
        self.assertTrue(any("小狐丸" in m and "上任队长" in m for m in messages))
        self.assertTrue(any("全队疲劳" in m for m in messages))

    def test_unrecognized_name_uses_slot_in_logs(self):
        device = Maa(dict(FULL), after_rows={**FULL, 1: 30}, names={4: "3KK"})
        messages = _rotate(device)
        self.assertTrue(any("4号位 上任队长" in m for m in messages))
        self.assertFalse(any("3KK" in m for m in messages))

    def test_same_garbage_name_cannot_confirm_swallowed_drag(self):
        device = Maa(dict(FULL), after_rows=dict(FULL),
                  names={4: "3KK"}, after_names={1: "3KK", 4: "3KK"})
        messages = _rotate(device)
        self.assertFalse(any("上任队长" in m for m in messages))
        self.assertTrue(any("拖动可能没生效" in m for m in messages))

    def test_captain_already_lowest_does_nothing(self):
        device = Maa({**FULL, 1: 20})
        messages = _rotate(device)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("位置没毛病" in m for m in messages))

    def test_gap_below_margin_is_not_worth_it(self):
        device = Maa({**FULL, 1: 40, 4: 35})
        messages = _rotate(device)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("不值得折腾" in m for m in messages))

    def test_missing_first_row_is_rejected(self):
        # 首行漏读会让全队错号：顶 anchor 直接拒读，宁可不换也不拖错人
        rows = dict(FULL)
        del rows[1]
        device = Maa(rows)
        messages = _rotate(device)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("读不齐" in m for m in messages))

    def test_missing_middle_row_is_rejected(self):
        # 中间漏行会留下倍距空洞，同样拒读
        rows = dict(FULL)
        del rows[3]
        device = Maa(rows)
        messages = _rotate(device)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("读不齐" in m for m in messages))

    def test_all_unreadable_stops(self):
        messages = _rotate(Maa({}))

        self.assertTrue(any("读不齐" in m for m in messages))

    def test_swallowed_drag_is_retried_then_reported(self):
        # 拖完队长位还是原值 → 手势被吞，按现位置再拖一次，仍不行就如实汇报
        device = Maa(dict(FULL), after_rows=dict(FULL))
        messages = _rotate(device)

        self.assertEqual(device.swipes, [(200, 447, 200, 163, 1000),
                                      (200, 447, 200, 163, 1500)])
        self.assertTrue(any("再拖一次" in m for m in messages))
        self.assertTrue(any("拖动可能没生效" in m for m in messages))
        self.assertFalse(any("上任队长" in m for m in messages))

    def test_misread_fatigue_is_verified_by_captain_name(self):
        # 疲劳复读 OCR 错字（30 认成 36）但队长位名字对得上 → 算换好了
        device = Maa(dict(FULL), after_rows={**FULL, 1: 36},
                  names={4: "小狐丸"}, after_names={1: "小狐丸"})
        messages = _rotate(device)

        self.assertEqual(len(device.swipes), 1)
        self.assertTrue(any("换好了" in m and "小狐丸" in m for m in messages))
        self.assertFalse(any("拖动可能没生效" in m for m in messages))

class SortieRotateHookTests(unittest.TestCase):
    """出阵流程的换队长钩子：开了才换，且每圈在部队选择步之后触发。"""

    class Maa:
        def screenshot(self, force=False):
            pass

        def click(self, point):
            pass

        def ocr(self, expected, roi, match_mode="contains"):
            return Point(1, 1) if expected in ("部队", "选择") else None

        def template_match(self, template, roi=None, threshold=0.7):
            # 只放行"小图页"地标，其它一律不命中，把流程快进到部队选择
            return Point(1, 1) if template == "area.png" else None

    def _host(self):
        from touken.flows.sortie import SortieMixin
        from touken.flows.battle import BattleMixin

        class Host(SortieMixin, BattleMixin):
            pass

        host = Host()
        host._disable_auto_march = lambda: True
        host.device = self.Maa()
        host.config = {
            "sortie": {"decide_button": {"template": "decide.png"},
                       "area_select_ui": {"template": "area.png"},
                       "depart_button": {"template": "depart.png"}},
            "map_select": {"合战场": {"chapters": {"1": [1, 2]},
                                      "maps": {"1": [3, 4]}}},
            "team_select": {"teams": {"3": [5, 6]}},
        }
        host.current_location = "本丸"

        def fake_nav(location):
            host.current_location = location  # 导航必然成功
            return iter(())

        host.navigate_to_stream = fake_nav
        host._click_point = lambda point: None
        host._wait_for_team_select = lambda cfg, attempts=12, open_after=2: True
        host._pick_team = lambda team_no: True
        host._expedition_takeover_requested = lambda now=None: False
        host._team_injury_status = lambda cfg: None
        host._combined_injury_status = lambda cfg, team_no, log_cache: (
            host._team_injury_status(cfg), None)
        host.saved_records = []
        host._save_team_record = lambda cfg, record_no=1: (
            host.saved_records.append(record_no) or True)
        host._click_depart = lambda cfg: False  # 换完队长就收场，别真出阵
        host.rotations = []
        host._rotate_captain_here = \
            lambda margin=10, team_no=None: iter(
                [host.rotations.append((margin, team_no)) or "rot"])
        return host

    def test_rotate_runs_each_loop_after_team_pick(self):
        host = self._host()
        from unittest.mock import patch
        with patch("touken.flows.sortie.time.sleep"):
            messages = list(host.sortie_stream(chapter=1, map_no=1, team_no=3,
                                               auto_equip=False, auto_march=False,
                                               rotate_captain=True,
                                               rotate_captain_margin=20))

        self.assertEqual(host.rotations, [(20, 3)])
        self.assertIn("rot", messages)
        self.assertIn("[出阵] 找不到即刻出阵按钮", messages)

    def test_rotate_off_by_default(self):
        host = self._host()
        from unittest.mock import patch
        with patch("touken.flows.sortie.time.sleep"):
            list(host.sortie_stream(chapter=1, map_no=1, team_no=3, auto_equip=False, auto_march=False))

        self.assertEqual(host.rotations, [])

    def test_auto_equip_stays_active_when_injury_action_will_stop(self):
        host = self._host()
        from unittest.mock import patch
        with patch("touken.flows.sortie.time.sleep"):
            list(host.sortie_stream(
                chapter=1, map_no=1, team_no=3,
                auto_equip=True, auto_march=False,
                injury_action="repair_stop"))

        self.assertEqual(host.saved_records, [1])


class SortieBuilderTests(unittest.TestCase):
    """面板 builder 把自动换队长开关和阈值透传给出阵/异去。"""

    class AgentStub:
        def sortie_stream(self, **kw):
            self.kw = kw
            return iter(())

        def yosari_stream(self, **kw):
            self.kw = kw
            return iter(())

    def test_build_sortie_passes_rotate_captain(self):
        from panel.server import _build_sortie, _build_yosari

        agent = self.AgentStub()
        list(_build_sortie(agent, "cfg", {
            "rotate_captain": True,
            "rotate_captain_margin": "20",
        }))
        self.assertIs(agent.kw["rotate_captain"], True)
        self.assertEqual(agent.kw["rotate_captain_margin"], 20)

        agent = self.AgentStub()
        list(_build_yosari(agent, "cfg", {
            "rotate_captain": True,
            "rotate_captain_margin": "5",
        }))
        self.assertIs(agent.kw["rotate_captain"], True)
        self.assertEqual(agent.kw["rotate_captain_margin"], 5)

        agent = self.AgentStub()
        list(_build_sortie(agent, "cfg", {}))
        self.assertIs(agent.kw["rotate_captain"], False)
        self.assertEqual(agent.kw["rotate_captain_margin"], 10)

    def test_battle_forms_only_show_margin_when_rotation_is_enabled(self):
        from panel.script_runner import _SCRIPTS
        import panel.server  # noqa: F401  # import 即注册脚本

        for script in ("sortie", "yosari"):
            fields = {field["key"]: field for field in _SCRIPTS[script]["params"]}
            self.assertIn("rotate_captain", fields)
            self.assertEqual(
                fields["rotate_captain_margin"]["visibleWhen"],
                {"key": "rotate_captain", "is": True})

    def test_auto_equip_is_independent_from_injury_action_in_battle_forms(self):
        from panel.script_runner import _SCRIPTS
        import panel.server  # noqa: F401  # import 即注册脚本

        for script in ("sortie", "yosari"):
            fields = {field["key"]: field for field in _SCRIPTS[script]["params"]}
            self.assertNotIn("visibleWhen", fields["auto_equip"])


# ==================== JSON 数据优先（与 _check_fatigue 同口径） ====================

def _facts_event(team=2, members=(), now=1000):
    """members: {slot: (serial, fatigue, sword_id, level, protect)} → /party/list 事件"""
    party = {str(team): {"slot": {str(s): {"serial_id": ser}
                                  for s, (ser, *_rest) in members.items()}}}
    swords = {str(ser): {"serial_id": ser, "fatigue": f, "sword_id": sid,
                         "level": lv, "protect": p}
              for ser, f, sid, lv, p in members.values()}
    return {"endpoint": "/party/list", "direction": "S->C", "status": 200,
            "ts": "2026-10-13 12:00:00",
            "payload": {"now_time": now, "status": 0,
                        "party": party, "sword": swords}}


def _pool_event(pool=(), parties=None, now=1000):
    """pool: (serial, fatigue, sword_id, level, protect)——不在任何部队里的刀。
    五支部队编成要全在册（空队也行）：半残编成不许下"全员飘花"的结论。"""
    swords = {str(ser): {"serial_id": ser, "fatigue": f, "sword_id": sid,
                         "level": lv, "protect": p}
              for ser, f, sid, lv, p in pool}
    return {"endpoint": "/party/list", "direction": "S->C", "status": 200,
            "ts": "2026-10-13 12:00:00",
            "payload": {"now_time": now, "status": 0,
                        "party": parties or {str(n): {"slot": {}} for n in range(1, 6)},
                        "sword": swords}}


class RotateCaptainFactsTests(unittest.TestCase):
    """换队长的数据优先路径：决策吃全队疲劳（游戏数据），OCR 只管行坐标。"""

    def test_changed_captain_number_succeeds_despite_47_read_as_49(self):
        members = {1: (101, 64, 3, 50, 1), 2: (102, 55, 5, 40, 1),
                   3: (103, 47, 118, 45, 1), 4: (104, 54, 5, 35, 1),
                   5: (105, 94, 11, 20, 1), 6: (106, 96, 7, 30, 1)}
        device = Maa({s: row[1] for s, row in members.items()},
                  after_rows={1: 49, 2: 55, 3: 64, 4: 54, 5: 94, 6: 96})
        messages = self._run(device, members)
        self.assertEqual(len(device.swipes), 1)
        self.assertTrue(any("编号核对一致" in message for message in messages))
        self.assertFalse(any("再拖一次" in message for message in messages))

    def test_unconfirmed_number_never_repeats_drag_even_with_matching_ocr(self):
        members = {1: (101, 80, 3, 50, 1), 2: (102, 60, 5, 40, 1),
                   3: (103, 90, 7, 30, 1), 4: (104, 30, 5, 35, 1),
                   5: (105, 70, 11, 20, 1), 6: (106, 85, 118, 45, 1)}
        device = Maa(dict(FULL), after_rows={**FULL, 1: 30})
        messages = self._run(device, members, confirm=False)
        self.assertEqual(len(device.swipes), 1)
        self.assertTrue(any("停止后续出阵" in message for message in messages))
        self.assertFalse(any("上任队长" in message for message in messages))

    def _run(self, device, members, team=2, now=1000, age=5, margin=10, confirm=True):
        from touken import game_state as gs
        flow = _flow(device)
        def events(adapter):
            current = dict(members)
            if device.swipes and confirm:
                low = min(current, key=lambda slot: current[slot][1])
                current[1], current[low] = current[low], current[1]
            return [_facts_event(team, current, now=now)]

        with patch.object(gs, "available_events", side_effect=events), \
                patch.object(gs.time, "time", return_value=now + age), \
                patch("touken.flows.sakura.time.sleep"):
            return list(flow._rotate_captain_here(margin=margin, team_no=team))

    def test_facts_decide_even_when_ocr_hides_red_face(self):
        # 2026-09-21 事故回归：OCR 把 4 号位 30 读成 100（红脸对轮换隐形），
        # 数据在册照样拖 4 号位——决策不再吃 OCR 读数。
        members = {1: (101, 80, 3, 50, 1), 2: (102, 60, 5, 40, 1),
                   3: (103, 90, 7, 30, 1), 4: (104, 30, 5, 35, 1),
                   5: (105, 70, 11, 20, 1), 6: (106, 85, 118, 45, 1)}
        device = Maa({**FULL, 4: 100}, after_rows={**FULL, 1: 30}, names={4: "3KK"})
        messages = self._run(device, members)

        self.assertEqual(device.swipes, [(200, 447, 200, 163, 1000)])
        self.assertTrue(any("小狐丸" in m and "上任队长" in m for m in messages))
        self.assertTrue(any("全队疲劳" in m for m in messages))
        # 汇报名来自名册数据：OCR 名字（3KK）既不可信也不该出现
        self.assertFalse(any("3KK" in m for m in messages))

    def test_stale_facts_skip_instead_of_guessing_from_ocr(self):
        # 数据过期时跳过可选轮换，不用 OCR 读数猜哪个实例最累。
        members = {1: (101, 80, 3, 50, 1), 2: (102, 60, 5, 40, 1),
                   3: (103, 90, 7, 30, 1), 4: (104, 40, 5, 35, 1),
                   5: (105, 70, 11, 20, 1), 6: (106, 20, 118, 45, 1)}
        device = Maa({**FULL, 1: 20})
        messages = self._run(device, members, age=601)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("数据不完整或已过期" in m for m in messages))

    def test_partial_facts_never_gate_decision(self):
        # 6 号位疲劳缺失：不拿部分数据或 OCR 猜测全队最低。
        members = {1: (101, 80, 3, 50, 1), 2: (102, 60, 5, 40, 1),
                   3: (103, 90, 7, 30, 1)}
        device = Maa({**FULL, 1: 20})
        messages = self._run(device, members)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("数据不完整或已过期" in m for m in messages))

    def test_facts_without_row_coordinates_never_drag(self):
        # 数据在册但画面一行都定位不到：没有坐标乱拖就是拖错人，宁可不换
        members = {1: (101, 80, 3, 50, 1), 2: (102, 60, 5, 40, 1),
                   3: (103, 90, 7, 30, 1), 4: (104, 30, 5, 35, 1),
                   5: (105, 70, 11, 20, 1), 6: (106, 85, 118, 45, 1)}
        device = Maa({})
        messages = self._run(device, members)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("定位不到" in m for m in messages))

    def test_facts_missing_low_row_coordinates_skip(self):
        # 数据说 6 号位最低，但画面 6 号行没定位到：拖点不全，本轮不换
        members = {1: (101, 80, 3, 50, 1), 2: (102, 60, 5, 40, 1),
                   3: (103, 90, 7, 30, 1), 4: (104, 40, 5, 35, 1),
                   5: (105, 70, 11, 20, 1), 6: (106, 20, 118, 45, 1)}
        device = Maa({s: FULL[s] for s in range(1, 6)})
        messages = self._run(device, members)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("没定位全" in m for m in messages))

    def test_facts_gap_below_margin_skips(self):
        members = {1: (101, 80, 3, 50, 1), 2: (102, 78, 5, 40, 1),
                   3: (103, 90, 7, 30, 1), 4: (104, 75, 5, 35, 1),
                   5: (105, 80, 11, 20, 1), 6: (106, 85, 118, 45, 1)}
        device = Maa(dict(FULL))
        messages = self._run(device, members)

        self.assertEqual(device.swipes, [])
        self.assertTrue(any("不值得折腾" in m for m in messages))


class SakuraListMaa:
    """刀剑男士选择列表假实现：疲劳列按 fy 给行，名字列按行 fy 归位。"""

    def __init__(self, rows, names=None):
        self.rows = rows          # [(fy, value)]
        self.names = names or {}  # {fy: raw_name}
        self.clicks = []
        self.swipes = []

    def screenshot(self, force=False):
        return None

    def ocr(self, text, roi, match_mode="contains"):
        return Point(640, 30)

    def ocr_all(self, roi):
        if roi.x == 460:
            return [(f"疲劳{v}/100", Point(540, fy)) for fy, v in self.rows]
        if roi.x == 100:
            hit = [raw for fy, raw in self.names.items() if roi.y == fy - 18]
            return [(hit[0], Point(150, roi.y + 18))] if hit else []
        return []

    def click(self, point):
        self.clicks.append((point.x, point.y))

    def template_match(self, *args, **kwargs):
        return None

    def swipe(self, *args):
        self.swipes.append(args)


class SwapTiredInFactsTests(unittest.TestCase):
    """刷花换人的数据优先路径：全刀池过滤候选，OCR 只定位行坐标。"""

    def _run(self, device, event, threshold=50, eligible=True):
        from touken import game_state as gs
        flow = _flow(device)
        calls = []

        def fake_legacy(self_, threshold_):
            calls.append(threshold_)
            yield "[刷花·换人] 画面逐行兜底"
            return "legacy"

        with patch.object(gs, "available_events",
                          return_value=[event] if event else []), \
                patch.object(gs.time, "time", return_value=1005), \
                patch("touken.flows.sakura.time.sleep"), \
                patch.object(flow, "_sakura_sort_list", return_value=True), \
                patch.object(flow, "_sakura_candidate_eligible",
                             return_value=eligible) as gate, \
                patch.object(flow, "_wait_list_closed", return_value=True,
                             create=True), \
                patch.object(SakuraMixin, "_swap_tired_in_by_ocr", fake_legacy):
            messages = []
            stream = flow._swap_tired_in(1, threshold)
            while True:
                try:
                    messages.append(next(stream))
                except StopIteration as stop:
                    return stop.value, calls, messages, gate

    def test_facts_pick_lowest_eligible_and_locate_by_name(self):
        pool = [(201, 40, 5, 39, 1), (202, 45, 3, 50, 1),
                (203, 10, 9, 1, 1),    # 1 级 → 排除
                (204, 20, 11, 20, 0),  # 没锁 → 排除
                (205, 35, 7, 30, 1)]   # 在部队3 → 排除
        parties = {str(n): {"slot": {}} for n in range(1, 6)}
        parties["3"] = {"slot": {"1": {"serial_id": 205}}}
        device = SakuraListMaa([(200, 40), (300, 45)],
                            names={200: "小狐丸", 300: "三日月"})
        result, calls, messages, gate = self._run(device, _pool_event(pool, parties))

        self.assertIs(result, True)
        # 换入按钮点的是小狐丸行（200-40）；数据在册不走老路
        self.assertEqual(device.clicks, [(1033, 160), (1197, 160)])
        self.assertEqual(calls, [])
        self.assertTrue(any("小狐丸" in m and "换！" in m for m in messages))
        # 点击前仍经画面核实锁/等级（同刀异振的坑不踩）
        self.assertEqual(gate.call_count, 1)

    def test_facts_confirm_full_blossom_without_scanning(self):
        pool = [(201, 80, 5, 39, 1), (202, 100, 3, 50, 1)]
        device = SakuraListMaa([], names={})
        result, calls, messages, _ = self._run(device, _pool_event(pool))

        self.assertIs(result, False)
        self.assertTrue(any("全员飘花" in m for m in messages))
        self.assertEqual(device.swipes, [])      # 数据定论：名单一页都不用扫
        # 无候选收尾：点左上返回箭头退回编队后才下收工结论（13d93a75197d）
        self.assertEqual(device.clicks, [(1033, 160), (135, 25)])
        self.assertEqual(calls, [])

    def test_missing_facts_fall_back_to_ocr_scan(self):
        device = SakuraListMaa([(200, 40)], names={200: "小狐丸"})
        result, calls, _, _ = self._run(device, None)

        self.assertEqual(result, "legacy")
        self.assertEqual(calls, [50])
        self.assertEqual(device.clicks, [(1033, 160)])

    def test_unlocated_candidate_never_clicked_and_falls_back(self):
        # 疲劳对上了但名字 OCR 空：定位没核实，不许盲点，交回视觉兜底
        pool = [(201, 40, 5, 39, 1)]
        device = SakuraListMaa([(200, 40)], names={})
        result, calls, messages, gate = self._run(device, _pool_event(pool))

        self.assertEqual(device.clicks, [(1033, 160)])
        self.assertEqual(gate.call_count, 0)
        self.assertEqual(result, "legacy")
        self.assertEqual(calls, [50])
        self.assertTrue(any("对不上" in m for m in messages))

    def test_same_fatigue_rows_disambiguated_by_name(self):
        # 两行同疲劳 40：三日月那振没锁不在候选里，只能点小狐丸那行
        pool = [(201, 40, 5, 39, 1), (202, 40, 3, 50, 0)]
        device = SakuraListMaa([(200, 40), (300, 40)],
                            names={200: "三日月", 300: "小狐丸"})
        result, calls, _, _ = self._run(device, _pool_event(pool))

        self.assertIs(result, True)
        self.assertEqual(device.clicks, [(1033, 160), (1197, 260)])
        self.assertEqual(calls, [])

    def test_gated_row_is_skipped_without_click(self):
        # 锁/等级画面核实不过：这行跳过不点，扫完回退视觉兜底
        pool = [(201, 40, 5, 39, 1)]
        device = SakuraListMaa([(200, 40)], names={200: "小狐丸"})
        result, calls, messages, gate = self._run(device, _pool_event(pool),
                                                  eligible=False)

        self.assertEqual(gate.call_count, 1)
        self.assertEqual(device.clicks, [(1033, 160)])
        self.assertEqual(result, "legacy")
        self.assertEqual(calls, [50])
        self.assertTrue(any("锁/等级没核实" in m for m in messages))

    def test_recovered_fatigue_still_matches(self):
        # 白山吉光事件：数据是上次同步的 46，画面已自然恢复到 49，
        # 容差内仍视作同一振，不许回退老路
        pool = [(201, 46, 165, 35, 1)]
        device = SakuraListMaa([(200, 49)], names={200: "白山吉光"})
        result, calls, messages, gate = self._run(device, _pool_event(pool))

        self.assertIs(result, True)
        self.assertEqual(device.clicks, [(1033, 160), (1197, 160)])
        self.assertEqual(calls, [])
        self.assertEqual(gate.call_count, 1)
        self.assertTrue(any("白山吉光" in m and "换！" in m for m in messages))

    def test_beyond_recovery_slack_falls_back(self):
        # 画面疲劳比数据高出容差以上：不能当同一振，交回视觉兜底
        pool = [(201, 46, 165, 35, 1)]
        device = SakuraListMaa([(200, 60)], names={200: "白山吉光"})
        result, calls, messages, gate = self._run(device, _pool_event(pool))

        self.assertEqual(gate.call_count, 0)
        self.assertEqual(device.clicks, [(1033, 160)])
        self.assertEqual(result, "legacy")
        self.assertEqual(calls, [50])


# ==================== 无候选收尾：先退回编队再下结论 ====================

class SakuraLeaveMaa:
    """收尾回归假实现：两个标题的 OCR 各自可开关（模拟在不在列表页、
    返回后回没回到编队），名单疲劳行按 fy 喂给 OCR 兜底路径。"""

    def __init__(self, rows=(), on_list=True, back_to_formation=True):
        self.rows = list(rows)                 # [(fy, 疲劳)]
        self.on_list = on_list                 # OCR "刀剑男士选择" 是否命中
        self.back_to_formation = back_to_formation  # 返回后 OCR "部队编成" 是否命中
        self.clicks = []
        self.swipes = []

    def screenshot(self, force=False):
        return None

    def ocr(self, text, roi, match_mode="contains"):
        if text == "刀剑男士选择":
            return Point(640, 30) if self.on_list else None
        if text == "部队编成":
            return Point(640, 30) if self.back_to_formation else None
        return None

    def ocr_all(self, roi):
        if roi.x == 460:
            return [(f"疲劳{v}/100", Point(540, fy)) for fy, v in self.rows]
        return []

    def click(self, point):
        self.clicks.append((point.x, point.y))

    def template_match(self, *args, **kwargs):
        return None

    def swipe(self, *args):
        self.swipes.append(args)


def _run_swap(flow, device, event, threshold=50):
    """走完整 _swap_tired_in：有事件=数据路径，None=OCR 兜底路径。"""
    from touken import game_state as gs
    with patch.object(gs, "available_events",
                      return_value=[event] if event else []), \
            patch.object(gs.time, "time", return_value=1005), \
            patch("touken.flows.sakura.time.sleep"), \
            patch.object(flow, "_sakura_sort_list", return_value=True), \
            patch.object(flow, "_sakura_candidate_eligible", return_value=True), \
            patch.object(flow, "_wait_list_closed", return_value=True, create=True):
        messages = []
        stream = flow._swap_tired_in(1, threshold)
        while True:
            try:
                messages.append(next(stream))
            except StopIteration as stop:
                return stop.value, messages


class NoCandidateBackOutTests(unittest.TestCase):
    """无候选两条路径的收尾纪律：先退回编队再 return False；
    退回没确认必须返回 None 并留明确日志，不许假装干净收工。"""

    def test_ocr_no_candidate_backs_out_before_finished(self):
        # OCR 兜底：本页疲劳全部 ≥ threshold = 没人累。先点返回箭头退回
        # 编队，再返回 False 下"没有候选"的结论
        device = SakuraLeaveMaa(rows=[(200, 80), (300, 90)])
        flow = _flow(device)
        result, messages = _run_swap(flow, device, None)

        self.assertIs(result, False)
        # 点击顺序：替换(1033,160) → 返回箭头(135,25)；名单一页都没翻
        self.assertEqual(device.clicks, [(1033, 160), (135, 25)])
        self.assertEqual(device.swipes, [])

    def test_no_candidate_back_out_failure_returns_none(self):
        # 数据路径：全刀池无候选，但返回后编队标题没确认——不能当干净
        # 收工，必须返回 None 并留明确日志
        pool = [(201, 80, 5, 39, 1)]
        device = SakuraLeaveMaa(back_to_formation=False)
        flow = _flow(device)
        result, messages = _run_swap(flow, device, _pool_event(pool))

        self.assertIsNone(result)
        self.assertTrue(any("不假装干净收工" in m for m in messages))
        self.assertEqual(device.clicks, [(1033, 160), (135, 25)])

    def test_ocr_no_candidate_back_out_failure_returns_none(self):
        device = SakuraLeaveMaa(rows=[(200, 80)], back_to_formation=False)
        flow = _flow(device)
        result, messages = _run_swap(flow, device, None)

        self.assertIsNone(result)
        self.assertTrue(any("不假装干净收工" in m for m in messages))
        self.assertEqual(device.clicks, [(1033, 160), (135, 25)])

    def test_already_left_selection_never_clicks_back(self):
        # 不在选人列表页：视为已离开，一个返回键都不许盲点
        device = SakuraLeaveMaa(on_list=False)
        flow = _flow(device)
        messages = list(flow._sakura_leave_selection_for_formation())

        self.assertTrue(any("不点返回" in m for m in messages))
        self.assertEqual(device.clicks, [])

    def test_found_candidate_never_triggers_back_out(self):
        # 正常换人的路径不得增加返回动作（守住既有候选过滤与点击顺序）
        pool = [(201, 40, 5, 39, 1)]
        device = SakuraListMaa([(200, 40)], names={200: "小狐丸"})
        flow = _flow(device)
        result, messages = _run_swap(flow, device, _pool_event(pool))

        self.assertIs(result, True)
        self.assertEqual(device.clicks, [(1033, 160), (1197, 160)])


if __name__ == "__main__":
    unittest.main()
