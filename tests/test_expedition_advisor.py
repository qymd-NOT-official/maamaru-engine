import pytest
# -*- coding: utf-8 -*-
"""远征建议引擎（panel/expedition_advisor.py）测试。

v3 语义：每个可丢队伍各派 N 班（rounds_per_team），班次按缺口榜轮转、
同队串行（收工+10 分钟缓冲）；点建议 = 采纳记 forced，绝不自动执行。
全部依赖注入：maps / planning / 偏好 / 近况文件都用临时件，不碰真实数据。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import day_timeline, scheduler
from panel import expedition_advisor as ea
from panel import expedition_choices as ec
from panel import server


def test_completed_departures_survive_collection_and_restart(tmp_path):
    from datetime import datetime
    from touken.telemetry import TelemetryStore
    begin = datetime(2026, 10, 2).timestamp()
    cfg = {"automation": {"last_runs": {"a": "2026-10-02 08:00:00", "b": "2026-10-02 12:00:00"},
           "slot_states": {"a": {"state": "dispatched", "team_no": 4, "map_code": "C1", "dispatched_at": "2026-10-02 08:00:00"},
                           "b": {"state": "dispatched", "team_no": 4, "map_code": "C2", "dispatched_at": "2026-10-02 12:00:00"}}}}
    store = TelemetryStore(tmp_path / "t.db")
    with patch("touken.telemetry.time.time", return_value=begin + 8 * 3600 + 0.5):
        store.record_event("expedition.dispatched", {"team_no": 4, "map_code": "C1"})
    # 倒计时/forced 已清空；三个来源的同一班不能算三次。
    assert day_timeline._expedition_counts(cfg, {}, {}, store, begin, begin + 13 * 3600) == {4: 2}
    reopened = TelemetryStore(tmp_path / "t.db")
    assert day_timeline._expedition_counts(cfg, {}, {}, reopened, begin, begin + 27 * 3600) == {4: 2}
    # 04:00 换日，昨日完成班不占今日名额。
    assert day_timeline._expedition_counts(cfg, {}, {}, reopened, begin + 86400, begin + 28 * 3600) == {}
    advice = ea.build_expedition_suggestions(_prefs(rounds=2, available_teams=(4,)),
        planning=_planning(), committed_counts={4: 2}, maps={}, situation_path=tmp_path / "missing")
    assert advice["suggestions"] == []


def test_dispatch_suggestion_gives_player_daily_priority(tmp_path):
    suggestions = ea.build_expedition_suggestions(_prefs(rounds=1, available_teams=(4,)),
        planning=_planning(), maps={'C1': {'duration_min': 60, '砥石': 100}},
        now_min=600, task_windows=[{'start_min': 602, 'end_min': 632, 'label': '一键日课'}],
        situation_path=tmp_path / 'missing')['suggestions']
    assert suggestions[0]['start_min'] == 632


def test_two_shifts_reuse_same_map_after_return(tmp_path):
    prefs = {**_prefs(rounds=2, available_teams=(4,)), "resource_focus": "砥石"}
    maps = {"C1": {"duration_min": 60, "砥石": 100}}
    suggestions = ea.build_expedition_suggestions(prefs, planning=_planning(), maps=maps,
        now_min=600, situation_path=tmp_path / "missing")["suggestions"]
    assert [(s["map_code"], s["start_min"], s["shift_no"]) for s in suggestions] == [
        ("C1", 605, 1), ("C1", 675, 2)]
    # 第一班采纳后刷新，只补第二班，时间不跑到第一班前面。
    remainder = ea.build_expedition_suggestions(prefs, planning=_planning(), maps=maps,
        now_min=600, committed_counts={4: 1}, team_busy_until={4: 665},
        occupied_windows=[("C1", 605, 665)], situation_path=tmp_path / "missing")["suggestions"]
    assert len(remainder) == 1 and remainder[0]["start_min"] == 675


def test_explicit_total_level_does_not_require_one_sword_to_reach_total():
    meta = {"level_req": 350, "rules": {"total_level": 350}}
    assert ea._level_ok(meta, {"sum": 396, "max": 99})
    assert ea._level_shortfall(meta, {"sum": 315, "max": 99}) == "等级合计 315，要求 350（差 35）"
    assert ea._level_shortfall({"level_req": 50}, {"sum": 200, "max": 40}) == "最高等级 40，要求 50（差 10）"


def test_fallback_explains_map_occupation_missing_types_and_actual_level_gap(tmp_path):
    situation = tmp_path / "situation.json"
    situation.write_text(json.dumps({"parties": [{"party_no": 5, "members": [
        {"name": "小豆长光·极", "level": 39}, {"name": "大千鸟十文字枪", "level": 99}]}]}), encoding="utf-8")
    maps = {
        "B1": {"duration_min": 90, "冷却材": 135, "rules": {"total_level": 50}},
        "A2": {"duration_min": 20, "冷却材": 45, "rules": {"required_types": {"短刀": 1}}},
        "A3": {"duration_min": 20, "冷却材": 30, "rules": {"required_types": {"胁差": 1}}},
        "C3": {"duration_min": 60, "冷却材": 750, "level_req": 350, "rules": {"total_level": 350}},
        "A1": {"duration_min": 10, "玉钢": 10, "rules": {}},
    }
    result = ea.build_expedition_suggestions({"rounds_per_team": 1, "available_teams": [5]},
        planning=_planning(limiting=("冷却材",)), maps=maps, situation_path=situation,
        occupied_maps=["B1"], now_min=500)
    suggestion = result["suggestions"][0]
    assert suggestion["resource"] == "玉钢"
    assert suggestion["blocked_resource"] == "冷却材"
    reasons = "；".join(suggestion["restrictions"])
    assert "B1：已有远征安排" in reasons
    assert "没有短刀" in reasons and "没有胁差" in reasons
    assert "等级合计 138，要求 350（差 212）" in reasons


# 假图数据：时薪都是 120/小时，靠 total_level / 资源种类区分
_MAPS = {
    "A1": {"era": 1, "slot": 1, "name": "练习场", "duration_min": 30,
           "木炭": 60, "玉钢": 0, "冷却材": 0, "砥石": 0, "小判": 0,
           "level_req": 5, "rules": {"total_level": 10}},
    "A2": {"era": 1, "slot": 2, "name": "矿山", "duration_min": 60,
           "木炭": 0, "玉钢": 120, "冷却材": 0, "砥石": 0, "小判": 0,
           "level_req": 50, "rules": {"total_level": 200}},
    "B1": {"era": 2, "slot": 1, "name": "湖底", "duration_min": 90,
           "木炭": 0, "玉钢": 0, "冷却材": 0, "砥石": 180, "小判": 90,
           "level_req": 10, "rules": {"total_level": 20}},
    "D4": {"era": 4, "slot": 4, "name": "大坂", "duration_min": 240,
           "木炭": 0, "玉钢": 0, "冷却材": 0, "砥石": 0, "小判": 400,
           "level_req": 30, "rules": {"total_level": 100}},
}


def _planning(*, limiting=("砥石",), capacity=3, koban_available=1000,
              goals=(), events=()):
    return {
        "resource_watch": {
            "resources": [
                {"resource": "木炭", "forge_capacity": capacity + 5},
                {"resource": "玉钢", "forge_capacity": capacity + 2},
                {"resource": "冷却材", "forge_capacity": capacity + 8},
                {"resource": "砥石", "forge_capacity": capacity},
                {"resource": "委托符", "forge_capacity": 12},
            ],
            "forge_capacity": capacity,
            "limiting": list(limiting),
        },
        "koban_watch": {"available": koban_available},
        "goals": list(goals),
        "events": list(events),
    }


def _prefs(rounds=1, available_teams=(1, 4)):
    return {"version": 2, "rounds_per_team": rounds,
            "available_teams": list(available_teams)}


def _write_situation(folder, parties):
    path = Path(folder) / "situation.json"
    path.write_text(json.dumps({"schema": 1, "parties": parties},
                               ensure_ascii=False), encoding="utf-8")
    return path


class ShortageOrderTests(unittest.TestCase):
    def test_limiting_drives_order_then_capacity_fill(self):
        order = ea.shortage_order(_planning(limiting=("砥石", "玉钢")), 3)
        self.assertEqual(order[0], ("砥石", "limiting"))
        self.assertEqual(order[1], ("玉钢", "limiting"))
        # 第三名按锻刀余量从少到多补：capacity+2 的玉钢已上榜，下一个是木炭
        self.assertEqual(order[2], ("木炭", "fill"))

    def test_koban_appended_when_short(self):
        planning = _planning(limiting=("砥石",), koban_available=-50)
        order = ea.shortage_order(planning, 2)
        self.assertEqual(order[1], ("小判", "koban"))

    def test_koban_goal_counts_as_short(self):
        planning = _planning(limiting=("砥石",),
                             goals=[{"resource": "小判", "status": "active"}])
        self.assertEqual(ea.shortage_order(planning, 2)[1], ("小判", "koban"))

    def test_all_four_tied_is_not_a_crisis(self):
        """四资源齐平不算「最缺」，走 fill 口吻按余量兜底。"""
        planning = _planning(limiting=("木炭", "玉钢", "冷却材", "砥石"),
                             capacity=400)
        order = ea.shortage_order(planning, 2)
        self.assertTrue(all(tag == "fill" for _, tag in order))
        self.assertEqual(order[0], ("砥石", "fill"))  # capacity 400 最少

    def test_pad_to_n_with_koban(self):
        order = ea.shortage_order(_planning(limiting=("砥石",)), 6)
        self.assertEqual(len(order), 6)
        self.assertEqual(order[-1], ("小判", "fill"))


class SuggestionBuildTests(unittest.TestCase):
    """v3：每个可丢队伍各派 N 班（rounds_per_team）；班次按缺口榜轮转，
    同队多班串行（上一班收工+10 分钟收菜缓冲后起排下一班）。"""

    def _build(self, prefs, **kwargs):
        kwargs.setdefault("planning", _planning())
        kwargs.setdefault("maps", _MAPS)
        kwargs.setdefault("situation_path", Path("/nonexistent"))
        kwargs.setdefault("now_min", 600)
        return ea.build_expedition_suggestions(prefs, **kwargs)

    def test_each_team_one_shift_by_shortage_order(self):
        """可丢 [1,4]、每队 1 班 → 部队一缺口第一（砥石）、部队四缺口第二。"""
        out = self._build(_prefs(rounds=1, available_teams=(1, 4)),
                          planning=_planning(limiting=("砥石", "玉钢")))
        self.assertEqual(len(out["suggestions"]), 2)
        first, second = out["suggestions"]
        self.assertEqual((first["team_no"], first["resource"],
                          first["map_code"]), (1, "砥石", "B1"))
        self.assertEqual((second["team_no"], second["resource"],
                          second["map_code"]), (4, "玉钢", "A2"))
        # now+5 分钟起排；不同队伍同时出发是游戏常态
        self.assertEqual(first["start_min"], 605)
        self.assertEqual(second["start_min"], 605)
        self.assertEqual(first["shift_no"], 1)
        self.assertEqual(first["key"], "suggest:1:B1:605")
        self.assertIsNone(out["note"])

    def test_rounds_two_serial_shifts_per_team(self):
        """每队 2 班：同队第二班从第一班收工+10 分钟收菜缓冲后起排。"""
        out = self._build(_prefs(rounds=2, available_teams=(1,)),
                          planning=_planning(limiting=("砥石", "玉钢")))
        self.assertEqual(len(out["suggestions"]), 2)
        first, second = out["suggestions"]
        self.assertEqual((first["resource"], first["map_code"],
                          first["start_min"], first["shift_no"]),
                         ("砥石", "B1", 605, 1))
        # 605 + 90 分钟 + 10 分钟缓冲 = 705 起第二班，接缺口榜下一种资源
        self.assertEqual((second["resource"], second["map_code"],
                          second["start_min"], second["shift_no"]),
                         ("玉钢", "A2", 705, 2))
        self.assertIn("这队今天第二班", second["reason"])

    def test_rotation_walks_shortage_list_across_rounds(self):
        """轮转：各队第 1 班依次取缺口第 1、2 种，第 2 班接着往后排；
        轮到的资源没产图就顺延下一种（冷却材没图 → 顺延小判）。"""
        out = self._build(_prefs(rounds=2, available_teams=(1, 4)),
                          planning=_planning(limiting=("砥石", "玉钢")))
        got = [(s["team_no"], s["resource"], s["map_code"], s["start_min"])
               for s in out["suggestions"]]
        # 榜：[砥石, 玉钢, 木炭, 冷却材, 小判]；部队四第二班轮到冷却材，
        # 没产图顺延小判 D4（起排 675 = 605+60+10 缓冲）
        self.assertEqual(got, [(1, "砥石", "B1", 605), (4, "玉钢", "A2", 605),
                               (1, "木炭", "A1", 705), (4, "小判", "D4", 675)])
        self.assertIn("顺延补小判", out["suggestions"][3]["reason"])
        self.assertIsNone(out["note"])

    def test_committed_counts_top_up_to_n(self):
        """committed 按队计数：部队四已有一班 → 只补一班，且算今天第二班。"""
        out = self._build(_prefs(rounds=2, available_teams=(1, 4)),
                          planning=_planning(limiting=("砥石", "玉钢")),
                          committed_counts={4: 1})
        got = [(s["team_no"], s["resource"], s["shift_no"])
               for s in out["suggestions"]]
        self.assertEqual(got, [(1, "砥石", 1), (4, "小判", 2), (1, "木炭", 2)])

    def test_committed_shift_pushes_next_start_after_it(self):
        """已排的班占着时间：补的班从已排班收工+缓冲后起排。"""
        out = self._build(_prefs(rounds=2, available_teams=(4,)),
                          planning=_planning(limiting=("砥石",)),
                          committed_counts={4: 1},
                          team_busy_until={4: 840})  # 已有一班到 14:00
        self.assertEqual(len(out["suggestions"]), 1)
        suggestion = out["suggestions"][0]
        self.assertEqual(suggestion["shift_no"], 2)
        self.assertEqual(suggestion["start_min"], 850)  # 840 + 10 分钟缓冲

    def test_all_teams_full_gives_honest_note(self):
        out = self._build(_prefs(rounds=1, available_teams=(4,)),
                          committed_counts={4: 1})
        self.assertEqual(out["suggestions"], [])
        self.assertIn("班都排上了", out["note"])

    def test_second_shift_that_cannot_fit_is_dropped_with_note(self):
        """第一班能在刷新前出发，但第二班出发已过04:00。"""
        out = self._build(_prefs(rounds=2, available_teams=(1,)),
                          now_min=1600)  # 次日 02:45 出发，归来后已过 04:00，不能再排第二班
        self.assertEqual(len(out["suggestions"]), 1)
        self.assertEqual(out["suggestions"][0]["map_code"], "B1")
        self.assertIn("部队一第二班", out["note"])
        self.assertIn("次日 03:59 前无法出发", out["note"])

    def test_reason_names_shortage_map_and_rank(self):
        out = self._build(_prefs(rounds=1, available_teams=(4,)))
        reason = out["suggestions"][0]["reason"]
        self.assertIn("砥石最缺（就剩3炉）", reason)
        self.assertIn("B1「湖底」", reason)
        self.assertIn("时薪正是第一", reason)
        self.assertIn("等级没核", reason)  # 没近况文件，如实标注

    def test_best_map_can_return_after_midnight(self):
        """晚段最优图可以跨夜归来，不必降为短图。"""
        planning = _planning(limiting=(), koban_available=-50)
        out = self._build(_prefs(rounds=1, available_teams=(1,)),
                          planning=planning, now_min=1200)  # 20:05 起排
        suggestion = out["suggestions"][0]
        self.assertEqual(suggestion["map_code"], "D4")
        self.assertIn("时薪正是第一", suggestion["reason"])

    def test_late_evening_keeps_best_map_across_midnight(self):
        """晚段仍选对口图，允许跨夜归来。"""
        out = self._build(_prefs(rounds=1, available_teams=(1,)),
                          now_min=23 * 60)  # 23:05 起排，B1/A2 装不下，A1 行
        suggestion = out["suggestions"][0]
        self.assertEqual(suggestion["map_code"], "B1")
        self.assertGreater(suggestion["start_min"] + suggestion["duration_min"], 1440)

    def test_0359_departure_can_return_after_reset(self):
        out = self._build(_prefs(rounds=1, available_teams=(1,)), now_min=1674)
        suggestion = out["suggestions"][0]
        self.assertEqual(suggestion["start_min"], 1679)
        self.assertGreater(suggestion["start_min"] + suggestion["duration_min"], 1680)

    def test_past_day_end_gives_no_suggestion_with_honest_note(self):
        out = self._build(_prefs(rounds=1, available_teams=(1,)),
                          now_min=1675)  # 五分钟准备后已到次日 04:00
        self.assertEqual(out["suggestions"], [])
        self.assertIn("次日 03:59 前无法出发", out["note"])

    def test_level_gate_falls_back_to_lower_map(self):
        """D4 要等级合计 100，队伍只有 50 → 回退到门槛 20 的 B1。"""
        with tempfile.TemporaryDirectory() as folder:
            path = _write_situation(folder, [
                {"party_no": 1, "members": [{"level": 50}]},
            ])
            planning = _planning(limiting=(), koban_available=-50)
            out = self._build(_prefs(rounds=1, available_teams=(1,)),
                              planning=planning, situation_path=path)
            suggestion = out["suggestions"][0]
            self.assertEqual(suggestion["map_code"], "B1")
            self.assertIn("等级合计够格", suggestion["reason"])

    def test_all_teams_level_blocked_gives_no_suggestion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = _write_situation(folder, [
                {"party_no": 4, "members": [{"level": 1}]},
            ])
            out = self._build(_prefs(rounds=1, available_teams=(4,)),
                              situation_path=path)
            self.assertEqual(out["suggestions"], [])
            self.assertIn("等级都不够", out["note"])

    def test_team_missing_from_situation_passes_gate_but_is_marked(self):
        """近况里没这队 = 等级核不了，不拦但注明。"""
        with tempfile.TemporaryDirectory() as folder:
            path = _write_situation(folder, [
                {"party_no": 2, "members": [{"level": 99}]},
            ])
            out = self._build(_prefs(rounds=1, available_teams=(4,)),
                              situation_path=path)
            self.assertEqual(len(out["suggestions"]), 1)
            self.assertIn("等级没核", out["suggestions"][0]["reason"])

    def test_full_team_gets_no_new_suggestion(self):
        out = self._build(_prefs(rounds=1, available_teams=(1, 4)),
                          committed_counts={1: 1})
        self.assertEqual([s["team_no"] for s in out["suggestions"]], [4])

    def test_partial_fill_gets_summary_note(self):
        """部队四所有对口图都被占 → 只排出部队一一班，小结 note 说实话。"""
        out = self._build(_prefs(rounds=1, available_teams=(1, 4)),
                          planning=_planning(limiting=("砥石", "玉钢")),
                          occupied_maps={"A2", "B1", "D4"})
        # 部队一顺延到木炭 A1；部队四全榜撞占用，排不出
        self.assertEqual([s["team_no"] for s in out["suggestions"]], [1])
        self.assertIn("只排得出 1 班", out["note"])
        self.assertIn("部队四第一班", out["note"])

    def test_occupied_map_falls_back_to_second_best(self):
        """小判榜首 D4 今天已有班在跑 → 建议顺移次优的 B1。"""
        planning = _planning(limiting=(), koban_available=-50)
        out = self._build(_prefs(rounds=1, available_teams=(1,)),
                          planning=planning, occupied_maps={"D4"})
        suggestion = out["suggestions"][0]
        self.assertEqual(suggestion["map_code"], "B1")
        self.assertEqual(suggestion["resource"], "小判")

    def test_all_producing_maps_occupied_gives_honest_note(self):
        """全榜对口图都被占 → 不硬塞，note 说明白。"""
        planning = _planning(limiting=(), koban_available=-50)
        out = self._build(_prefs(rounds=1, available_teams=(1,)),
                          planning=planning,
                          occupied_maps={"A1", "A2", "B1", "D4"})
        self.assertEqual(out["suggestions"], [])
        self.assertIn("都有班在跑或已点上", out["note"])

    def test_two_suggestions_never_share_one_map(self):
        """两资源时薪榜首同图时，第二条建议回退次优图（一图一班）。"""
        maps = {
            "M1": {"era": 1, "slot": 1, "name": "双产", "duration_min": 60,
                   "木炭": 120, "玉钢": 120, "冷却材": 0, "砥石": 0, "小判": 0,
                   "rules": {}},
            "M2": {"era": 1, "slot": 2, "name": "单产", "duration_min": 60,
                   "木炭": 0, "玉钢": 60, "冷却材": 0, "砥石": 0, "小判": 0,
                   "rules": {}},
        }
        planning = _planning(limiting=("木炭", "玉钢"))
        out = self._build(_prefs(rounds=1, available_teams=(1, 4)),
                          planning=planning, maps=maps)
        self.assertEqual(len(out["suggestions"]), 2)
        first, second = out["suggestions"]
        self.assertEqual((first["resource"], first["map_code"],
                          first["team_no"]), ("木炭", "M1", 1))
        # 玉钢榜首也是 M1，但已被本批建议占住 → 回退 M2
        self.assertEqual((second["resource"], second["map_code"],
                          second["team_no"]), ("玉钢", "M2", 4))

    def test_resource_without_producing_map_falls_through(self):
        """轮到的资源没产图 → 顺延下一种，reason 写清楚顺延。"""
        out = self._build(_prefs(rounds=1, available_teams=(1,)),
                          planning=_planning(limiting=("冷却材",)))
        suggestion = out["suggestions"][0]
        self.assertEqual(suggestion["resource"], "砥石")  # 榜上下一种
        self.assertIn("冷却材排不出，顺延补砥石", suggestion["reason"])

    def test_fill_tone_when_nothing_is_urgent(self):
        """四资源齐平不算「最缺」，reason 走顺手攒的 fill 口吻。"""
        planning = _planning(limiting=("木炭", "玉钢", "冷却材", "砥石"),
                             capacity=400)
        out = self._build(_prefs(rounds=1, available_teams=(4,)),
                          planning=planning)
        self.assertEqual(out["suggestions"][0]["resource"], "砥石")
        self.assertIn("顺手攒砥石", out["suggestions"][0]["reason"])

    def test_no_planning_gives_empty_advice_with_note(self):
        out = self._build(_prefs(rounds=1), planning=None)
        self.assertEqual(out["suggestions"], [])
        self.assertIn("盘点", out["note"])

    def test_empty_resource_watch_is_no_data(self):
        planning = {"resource_watch": {"forge_capacity": None,
                                       "limiting": []}}
        out = self._build(_prefs(rounds=1), planning=planning)
        self.assertEqual(out["suggestions"], [])
        self.assertIn("盘点", out["note"])

    def test_rounds_zero_means_no_advice(self):
        out = self._build(_prefs(rounds=0))
        self.assertEqual(out["suggestions"], [])
        self.assertIn("不丢队", out["note"])


# 刀种门槛测试用图：B2 要队里有打刀（老大部队四翻车现场），B1 无要求兜底，
# E4 要凑 4 种刀。收益只为排队次服务
_TYPE_MAPS = {
    "B2": {"era": 2, "slot": 2, "name": "加役方人足寄场", "duration_min": 180,
           "木炭": 0, "玉钢": 0, "冷却材": 0, "砥石": 0, "小判": 300,
           "rules": {"total_level": 60, "required_types": {"打刀": 1}}},
    "B1": {"era": 2, "slot": 1, "name": "湖底", "duration_min": 90,
           "木炭": 0, "玉钢": 0, "冷却材": 0, "砥石": 180, "小判": 90,
           "rules": {"total_level": 20, "required_types": {}}},
}
_E4_MAPS = {
    "E4": {"era": 5, "slot": 4, "name": "天下布武", "duration_min": 360,
           "木炭": 0, "玉钢": 0, "冷却材": 0, "砥石": 0, "小判": 600,
           "rules": {"total_level": 300, "required_types": {},
                     "min_distinct_types": 4}},
}


def _members(*names, level=99):
    return [{"level": level, "name": name} for name in names]


class TypeGateTests(unittest.TestCase):
    """刀种资格门：「含有」语义——至少一把该刀种在队；极化刀种不变。
    成员名单走真实名册（swords.json）解析。"""

    def _build(self, prefs, parties, maps=_TYPE_MAPS):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = _write_situation(folder.name, parties)
        planning = _planning(limiting=(), koban_available=-50)
        return ea.build_expedition_suggestions(
            prefs, planning=planning, maps=maps, situation_path=path,
            now_min=600)

    def test_all_tachi_team_skips_b2_to_second_map(self):
        """全太刀队（老大部队四翻车同款）不给 B2，顺移无门槛的 B1。"""
        out = self._build(
            _prefs(rounds=1, available_teams=(4,)),
            [{"party_no": 4, "members": _members(
                "三日月宗近", "小狐丸", "一期一振")}])
        suggestion = out["suggestions"][0]
        self.assertEqual(suggestion["map_code"], "B1")
        self.assertEqual(suggestion["team_no"], 4)

    def test_qualified_team_gets_b2_over_unqualified(self):
        """两队可丢时，B2 顺移给队里有打刀的部队一，不给全太刀的部队四。"""
        out = self._build(
            _prefs(rounds=1, available_teams=(1, 4)),
            [{"party_no": 4, "members": _members(
                "三日月宗近", "小狐丸", "一期一振")},
             {"party_no": 1, "members": _members(
                 "加州清光", "山姥切国广", "三日月宗近")}])
        suggestion = out["suggestions"][0]
        self.assertEqual((suggestion["map_code"], suggestion["team_no"]),
                         ("B2", 1))
        self.assertIn("刀种也够格", suggestion["reason"])

    def test_contains_semantics_one_uchigatana_is_enough(self):
        """「含有」不是「全是」：五把太刀里掺一把打刀就合格。"""
        out = self._build(
            _prefs(rounds=1, available_teams=(4,)),
            [{"party_no": 4, "members": _members(
                "三日月宗近", "小狐丸", "一期一振", "江雪左文字",
                "加州清光")}])
        suggestion = out["suggestions"][0]
        self.assertEqual(suggestion["map_code"], "B2")

    def test_kiwame_suffix_keeps_base_type(self):
        """极化刀种不变：「加州清光·极」仍算打刀。"""
        out = self._build(
            _prefs(rounds=1, available_teams=(4,)),
            [{"party_no": 4, "members": _members(
                "三日月宗近", "小狐丸", "一期一振", "加州清光·极")}])
        self.assertEqual(out["suggestions"][0]["map_code"], "B2")

    def test_all_teams_type_blocked_gives_human_note(self):
        """唯一可丢队全是太刀且次优图也占 → note 写人话说明卡在哪。"""
        out = self._build(
            _prefs(rounds=1, available_teams=(4,)),
            [{"party_no": 4, "members": _members(
                "三日月宗近", "小狐丸", "一期一振")}],
            maps={"B2": _TYPE_MAPS["B2"]})
        self.assertEqual(out["suggestions"], [])
        self.assertIn("刀种门槛", out["note"])
        self.assertIn("部队四全是太刀，没有打刀", out["note"])

    def test_e4_min_distinct_types_gate(self):
        """E4 要凑 4 种刀：3 种不够，补一把大太刀凑够 4 种放行。"""
        three_kinds = [{"party_no": 4, "members": _members(
            "三日月宗近", "小狐丸", "加州清光", "山姥切国广",
            "今剑", "今剑")}]  # 太刀/打刀/短刀 = 3 种
        out = self._build(_prefs(rounds=1, available_teams=(4,)),
                          three_kinds, maps=_E4_MAPS)
        self.assertEqual(out["suggestions"], [])
        self.assertIn("凑4种刀", out["note"])
        self.assertIn("只凑出3种刀", out["note"])

        four_kinds = [{"party_no": 4, "members": _members(
            "三日月宗近", "小狐丸", "加州清光", "今剑", "石切丸",
            "石切丸")}]  # 太刀/打刀/短刀/大太刀 = 4 种
        out = self._build(_prefs(rounds=1, available_teams=(4,)),
                          four_kinds, maps=_E4_MAPS)
        self.assertEqual(out["suggestions"][0]["map_code"], "E4")
        self.assertIn("凑4种刀", out["suggestions"][0]["reason"])

    def test_failed_combo_blacklisted_but_map_open_to_other_team(self):
        """同图同队今天 failed 过 → 拉黑该组合，同图换队仍可荐。"""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = _write_situation(folder.name, [
            {"party_no": 1, "members": _members("加州清光", "今剑")},
            {"party_no": 4, "members": _members("加州清光", "今剑")},
        ])
        out = ea.build_expedition_suggestions(
            _prefs(rounds=1, available_teams=(1, 4)),
            planning=_planning(limiting=(), koban_available=-50),
            maps=_TYPE_MAPS, situation_path=path, now_min=600,
            failed_combos={("B2", 4)})
        # (B2,部队四) 拉黑，B2 改荐部队一
        suggestion = out["suggestions"][0]
        self.assertEqual((suggestion["map_code"], suggestion["team_no"]),
                         ("B2", 1))

    def test_failed_combo_without_fallback_gives_note(self):
        """同组合 failed 且无队无图可换 → note 写「这班今天没派成」。"""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = _write_situation(folder.name, [
            {"party_no": 4, "members": _members("加州清光", "今剑")},
        ])
        out = ea.build_expedition_suggestions(
            _prefs(rounds=1, available_teams=(4,)),
            planning=_planning(limiting=(), koban_available=-50),
            maps={"B2": _TYPE_MAPS["B2"]}, situation_path=path, now_min=600,
            failed_combos={("B2", 4)})
        self.assertEqual(out["suggestions"], [])
        self.assertIn("这班今天没派成", out["note"])
        self.assertIn("换队/换图试试", out["note"])

    def test_failed_combo_still_explains_missing_sword_type(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = _write_situation(folder.name, [
            {"party_no": 4, "members": _members("三日月宗近", "小狐丸", "一期一振")},
        ])
        out = ea.build_expedition_suggestions(
            _prefs(rounds=1, available_teams=(4,)),
            planning=_planning(limiting=(), koban_available=-50),
            maps={"B2": _TYPE_MAPS["B2"]}, situation_path=path, now_min=600,
            failed_combos={("B2", 4)})
        self.assertEqual(out["suggestions"], [])
        self.assertIn("没有打刀", out["note"])


class PrefsStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "prefs.json"

    def test_missing_file_gives_defaults(self):
        prefs = ea.load_prefs(self.path)
        self.assertEqual(prefs["rounds_per_team"], 1)
        self.assertEqual(prefs["available_teams"], [1, 4, 5])

    def test_broken_file_gives_defaults(self):
        self.path.write_text("{not json", encoding="utf-8")
        prefs = ea.load_prefs(self.path)
        self.assertEqual(prefs["available_teams"], [1, 4, 5])

    def test_v1_file_migrates_teams_out_to_rounds(self):
        """v1 老偏好（teams_out=总共丢几队）读出即迁移成每队次数。"""
        self.path.write_text(json.dumps(
            {"version": 1, "teams_out": 3, "available_teams": [4]}),
            encoding="utf-8")
        prefs = ea.load_prefs(self.path)
        self.assertEqual(prefs["version"], 2)
        self.assertEqual(prefs["rounds_per_team"], 3)
        self.assertEqual(prefs["available_teams"], [4])
        # 只读迁移不落盘，下次保存才写 v2
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["version"], 1)

    def test_unknown_version_gives_defaults(self):
        self.path.write_text(json.dumps({"version": 3, "rounds_per_team": 3}),
                             encoding="utf-8")
        self.assertEqual(ea.load_prefs(self.path)["rounds_per_team"], 1)

    def test_partial_keys_fall_back(self):
        self.path.write_text(json.dumps({"version": 2, "rounds_per_team": 4}),
                             encoding="utf-8")
        prefs = ea.load_prefs(self.path)
        self.assertEqual(prefs["rounds_per_team"], 4)
        self.assertEqual(prefs["available_teams"], [1, 4, 5])

    def test_bad_values_normalized_not_fatal(self):
        self.path.write_text(json.dumps(
            {"version": 2, "rounds_per_team": 99,
             "available_teams": [1, 9, "x"]}),
            encoding="utf-8")
        prefs = ea.load_prefs(self.path)
        self.assertEqual(prefs["rounds_per_team"], 5)
        self.assertEqual(prefs["available_teams"], [1])

    def test_save_roundtrip_and_backup(self):
        prefs = ea.save_prefs(rounds_per_team=2, available_teams=[4, 1, 4],
                              path=self.path)
        self.assertEqual(prefs["rounds_per_team"], 2)
        self.assertEqual(prefs["available_teams"], [1, 4])
        self.assertEqual(ea.load_prefs(self.path)["available_teams"], [1, 4])
        again = ea.save_prefs(rounds_per_team=3, available_teams=[5],
                              path=self.path)
        self.assertEqual(again["available_teams"], [5])
        backup = json.loads(self.path.with_suffix(".json.bak")
                            .read_text(encoding="utf-8"))
        self.assertEqual(backup["rounds_per_team"], 2)
        # 落盘就是 v2 新字段
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["version"], 2)
        self.assertIn("rounds_per_team", on_disk)

    def test_save_validation(self):
        for bad in (6, -1, "3", True, 1.5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ea.save_prefs(rounds_per_team=bad, available_teams=[1],
                              path=self.path)
        for bad in ([0], [6], ["1"], [True], "14"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ea.save_prefs(rounds_per_team=1, available_teams=bad,
                              path=self.path)
        self.assertFalse(self.path.exists())


class TimelineIntegrationTests(unittest.TestCase):
    """build_day_timeline 注入偏好/账本/近况，吐出建议淡影字段（v2 现算班）。"""

    def setUp(self):
        day_timeline._planning_cache.clear()  # 别家的测试可能预填了真账本缓存
        self.addCleanup(day_timeline._planning_cache.clear)
        patcher = patch.object(scheduler, "map_options", lambda: [
            {"code": "A1", "duration_min": 30},
            {"code": "B1", "duration_min": 90},
        ])
        patcher.start()
        self.addCleanup(patcher.stop)
        # build_day_timeline 不传 maps，建议引擎走自己的 load_maps
        maps_patcher = patch.object(ea, "load_maps", lambda: _MAPS)
        maps_patcher.start()
        self.addCleanup(maps_patcher.stop)

    def _today_at(self, hour, minute=0):
        return time.mktime(time.strptime(
            f"{time.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}:00",
            "%Y-%m-%d %H:%M:%S"))

    def _build(self, now, cfg, **kwargs):
        kwargs.setdefault("store", object())
        kwargs.setdefault("script_labels", {})
        kwargs.setdefault("expedition_forced", {})
        kwargs.setdefault("expedition_records", {})
        kwargs.setdefault("situation_path", Path("/nonexistent"))
        return day_timeline.build_day_timeline(now, cfg=cfg, **kwargs)

    def test_timeline_carries_help_and_suggestions(self):
        now = self._today_at(6, 0)
        cfg = {"entries": [{"time": "10:00", "team_no": 4, "map_code": "B1",
                            "enabled": True}],
               "automation": {"enabled": False, "mode": "custom",
                              "preset": "小判", "teams": [2, 3, 4],
                              "start_time": "08:00"}}
        out = self._build(now, cfg,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4,)),
                          planning=_planning())
        self.assertEqual(out["expedition_help"]["rounds_per_team"], 1)
        self.assertEqual(out["expedition_help"]["available_teams"], [4])
        # v2：排班条目不再上轴，也不影响建议——引擎按缺口现算
        self.assertEqual(out["expeditions"], [])
        self.assertEqual(len(out["expedition_suggestions"]), 1)
        suggestion = out["expedition_suggestions"][0]
        self.assertEqual(suggestion["kind"], "expedition")
        self.assertEqual(suggestion["team_no"], 4)
        self.assertEqual(suggestion["map_code"], "B1")
        self.assertEqual(suggestion["start_min"], 365)  # 06:00 + 5 分钟
        self.assertEqual(suggestion["duration_min"], 90)
        self.assertEqual(suggestion["resource"], "砥石")
        self.assertEqual(suggestion["shift_no"], 1)
        self.assertTrue(suggestion["reason"])

    def test_forced_teams_get_no_new_suggestion(self):
        """已点上班的队（forced 落账）班次计满，不再给新建议。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [], "automation": {"enabled": False,
                                             "mode": "custom"}}
        forced = {"k": {"team_no": 4, "map_code": "B1",
                        "planned_at": self._today_at(10, 0)}}
        out = self._build(now, cfg, expedition_forced=forced,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4,)),
                          planning=_planning())
        self.assertEqual(len(out["expeditions"]), 1)
        self.assertEqual(out["expedition_suggestions"], [])
        self.assertIn("班都排上了", out["expedition_advice_note"])

    def test_running_shift_counts_toward_rounds(self):
        """每队 2 班 + 在跑 1 班 → 只补 1 班，排在在跑班收工+缓冲后。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [], "automation": {"enabled": False,
                                             "mode": "custom"}}
        records = {"4": {"map_code": "B1", "duration_min": 90,
                         "dispatched_at": time.strftime(
                             "%Y-%m-%d %H:%M:%S",
                             time.localtime(self._today_at(5, 0)))}}
        out = self._build(now, cfg, expedition_records=records,
                          expedition_help=_prefs(rounds=2,
                                                 available_teams=(4,)),
                          planning=_planning(limiting=(),
                                             koban_available=-50))
        suggestions = out["expedition_suggestions"]
        self.assertEqual(len(suggestions), 1)
        suggestion = suggestions[0]
        self.assertEqual(suggestion["shift_no"], 2)
        # 在跑班 05:00+90min=06:30(390) 收工 +10 分钟缓冲 → 400 起排
        self.assertEqual(suggestion["start_min"], 400)
        self.assertEqual(suggestion["map_code"], "B1")  # 第二班延续资源轮转，已归来的图可复用

    def test_running_team_gets_no_new_suggestion(self):
        """队伍还在外面远征（expeditions.json）时不再给新建议。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [], "automation": {"enabled": False,
                                             "mode": "custom"}}
        records = {"4": {"map_code": "B1", "duration_min": 90,
                         "dispatched_at": time.strftime(
                             "%Y-%m-%d %H:%M:%S",
                             time.localtime(self._today_at(5, 0)))}}
        out = self._build(now, cfg, expedition_records=records,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4,)),
                          planning=_planning())
        self.assertEqual(out["expeditions"][0]["kind"], "running")
        self.assertEqual(out["expedition_suggestions"], [])

    def test_forced_shift_occupies_its_map(self):
        """已点的班（时段没过完）占住图：D4 已点 → 小判建议回退 B1。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [], "automation": {"enabled": False,
                                             "mode": "custom"}}
        forced = {"k": {"team_no": 4, "map_code": "D4",
                        "planned_at": self._today_at(10, 0),
                        "start_min": 600, "duration_min": 240}}
        out = self._build(now, cfg, expedition_forced=forced,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4, 5)),
                          planning=_planning(limiting=(), koban_available=-50))
        suggestions = out["expedition_suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["map_code"], "B1")
        self.assertEqual(suggestions[0]["team_no"], 5)

    def test_running_expedition_occupies_its_map(self):
        """还在跑的班占住图：D4 在跑 → 小判建议回退 B1。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [], "automation": {"enabled": False,
                                             "mode": "custom"}}
        records = {"5": {"map_code": "D4", "duration_min": 240,
                         "dispatched_at": time.strftime(
                             "%Y-%m-%d %H:%M:%S",
                             time.localtime(self._today_at(5, 0)))}}
        out = self._build(now, cfg, expedition_records=records,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4, 5)),
                          planning=_planning(limiting=(), koban_available=-50))
        self.assertEqual(out["expeditions"][0]["state"], "running")
        suggestions = out["expedition_suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["map_code"], "B1")
        self.assertEqual(suggestions[0]["team_no"], 4)

    def test_awaiting_collect_expedition_occupies_its_map(self):
        """跑完待收也算没完结：图仍被占，建议不往 D4 塞。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [], "automation": {"enabled": False,
                                             "mode": "custom"}}
        records = {"5": {"map_code": "D4", "duration_min": 90,
                         "dispatched_at": time.strftime(
                             "%Y-%m-%d %H:%M:%S",
                             time.localtime(self._today_at(4, 0)))}}
        out = self._build(now, cfg, expedition_records=records,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4, 5)),
                          planning=_planning(limiting=(), koban_available=-50))
        self.assertEqual(out["expeditions"][0]["state"], "awaiting_collect")
        suggestions = out["expedition_suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["map_code"], "B1")

    def test_expired_shift_frees_its_map(self):
        """过点作废的班不占图不计班：D4 的班 expired → 建议照常给 D4。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [],
               "automation": {"enabled": False, "mode": "custom",
                              "slot_states": {"k": {"state": "expired",
                                                    "blocked_reason": ""}}}}
        forced = {"k": {"team_no": 4, "map_code": "D4",
                        "planned_at": self._today_at(10, 0),
                        "start_min": 600, "duration_min": 240}}
        out = self._build(now, cfg, expedition_forced=forced,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4, 5)),
                          planning=_planning(limiting=(), koban_available=-50))
        suggestions = out["expedition_suggestions"]
        # 两队都还有班次额度：部队四拿小判榜首 D4，部队五砥石 B1
        self.assertEqual(len(suggestions), 2)
        self.assertEqual((suggestions[0]["map_code"],
                          suggestions[0]["team_no"]), ("D4", 4))

    def test_failed_combo_blacklisted_map_stays_open(self):
        """确认失败的班不占图，但同图同队拉黑：部队四的小判班改落 B1，
        拉黑的事 note 如实知会。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [],
               "automation": {"enabled": False, "mode": "custom",
                              "slot_states": {"k": {"state": "failed_unknown",
                                                    "blocked_reason": ""}}}}
        forced = {"k": {"team_no": 4, "map_code": "D4",
                        "planned_at": self._today_at(10, 0),
                        "start_min": 600, "duration_min": 240}}
        out = self._build(now, cfg, expedition_forced=forced,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4, 5)),
                          planning=_planning(limiting=(), koban_available=-50))
        suggestions = out["expedition_suggestions"]
        # (D4,部队四) 拉黑 → 部队四小判改 B1；部队五顺延玉钢 A2
        self.assertEqual([(s["map_code"], s["team_no"]) for s in suggestions],
                         [("B1", 4), ("A2", 5)])
        self.assertIn("没派成", out["expedition_advice_note"])
        self.assertIn("改排了小判", out["expedition_advice_note"])

    def test_failed_combo_without_other_team_gives_honest_note(self):
        """没派成的组合拉黑后顺延别的资源补班，note 照样说没派成的事。"""
        now = self._today_at(6, 0)
        cfg = {"entries": [],
               "automation": {"enabled": False, "mode": "custom",
                              "slot_states": {
                                  "k": {"state": "failed_unknown",
                                        "blocked_reason": ""},
                                  "k2": {"state": "failed_unknown",
                                         "blocked_reason": ""}}}}
        forced = {"k": {"team_no": 4, "map_code": "D4",
                        "planned_at": self._today_at(10, 0),
                        "start_min": 600, "duration_min": 240},
                  "k2": {"team_no": 4, "map_code": "B1",
                         "planned_at": self._today_at(15, 0),
                         "start_min": 900, "duration_min": 90}}
        out = self._build(now, cfg, expedition_forced=forced,
                          expedition_help=_prefs(rounds=1,
                                                 available_teams=(4,)),
                          planning=_planning(limiting=(), koban_available=-50))
        # 小判（D4/B1）和砥石（B1）的组合都拉黑 → 顺延玉钢 A2 补上这班
        suggestions = out["expedition_suggestions"]
        self.assertEqual([(s["map_code"], s["team_no"]) for s in suggestions],
                         [("A2", 4)])
        self.assertIn("没派成", out["expedition_advice_note"])
        self.assertIn("今天暂不重试同队同图", out["expedition_advice_note"])

    def test_selecting_different_preset_rechecks_failed_combination(self):
        now = self._today_at(6, 0)
        cfg = {"entries": [], "automation": {"enabled": False,
               "slot_states": {"k": {"state": "failed_unknown"}}}}
        forced = {"k": {"team_no": 4, "map_code": "B2",
                        "planned_at": self._today_at(5, 0),
                        "start_min": 300, "duration_min": 180}}
        prefs = {**_prefs(rounds=1, available_teams=(4,)), "team_formations": {"4": "new-preset"}}
        with patch.object(ea, "build_expedition_suggestions", return_value={"suggestions": [], "note": None}) as build:
            self._build(now, cfg, expedition_forced=forced, expedition_help=prefs, planning=_planning())
        self.assertEqual(build.call_args.kwargs["failed_combos"], set())

    def test_timeline_note_when_no_planning(self):
        now = self._today_at(6, 0)
        cfg = {"entries": [], "automation": {"enabled": False,
                                             "mode": "custom"}}
        out = self._build(now, cfg, expedition_help=_prefs(rounds=1),
                          planning=None)
        self.assertEqual(out["expedition_suggestions"], [])
        self.assertIn("盘点", out["expedition_advice_note"])


class AdoptEndpointTests(unittest.TestCase):
    """PUT /api/day-timeline/expedition-adopt：采纳 = 自描述 forced 落账。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "choices.json"
        self.day_start = time.mktime(time.strptime(
            f"{time.strftime('%Y-%m-%d')} 00:00:00", "%Y-%m-%d %H:%M:%S"))
        self.suggestion = {"kind": "expedition", "key": "suggest:4:B1",
                           "team_no": 4, "map_code": "B1", "map_name": "湖底",
                           "resource": "砥石", "duration_min": 90,
                           "start_min": 600, "reason": "砥石最缺"}
        self.timeline = {"day_start": self.day_start,
                         "expeditions": [],
                         "expedition_suggestions": [self.suggestion],
                         "expedition_help": {"rounds_per_team": 1,
                                             "available_teams": [4]},
                         "expedition_advice_note": None}

    def _put(self, payload):
        real_load = ec.load_choice_sets
        real_set = ec.set_forced_adhoc

        def load_temp(*_args, **_kwargs):
            return real_load(self.path)

        def set_temp(**kwargs):
            kwargs["path"] = self.path
            return real_set(**kwargs)

        with patch.object(server, "_day_timeline_payload",
                          return_value=self.timeline), \
             patch.object(ec, "load_choice_sets", side_effect=load_temp), \
             patch.object(ec, "set_forced_adhoc", side_effect=set_temp):
            return TestClient(server.app).put(
                "/api/day-timeline/expedition-adopt", json=payload)

    def _today(self):
        return time.strftime("%Y-%m-%d", time.localtime(self.day_start))

    def test_adopt_writes_self_describing_forced(self):
        response = self._put({"team_no": 4, "map_code": "B1",
                              "start_min": 600})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        skipped, forced = ec.load_choice_sets(self.path)
        self.assertEqual(skipped, {})
        key = ec.adhoc_key(self._today(), 4, 600)
        self.assertIn(key, forced)
        record = forced[key]
        # 自描述：队伍/图/时刻/时长全在记录里，不引用排班条目
        self.assertEqual(record["team_no"], 4)
        self.assertEqual(record["map_code"], "B1")
        self.assertEqual(record["start_min"], 600)
        self.assertEqual(record["duration_min"], 90)
        self.assertEqual(record["planned_at"], time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(self.day_start + 600 * 60)))
        self.assertTrue(ec.is_adhoc_record(record))
        # 响应带新鲜的泳道/建议，前端不用再多拉一次
        self.assertIn("expeditions", response.json())
        self.assertIn("expedition_suggestions", response.json())

    def test_midnight_and_next_morning_adoption_have_actual_departure_date(self):
        for minute in (0, 1440, 1679):
            with self.subTest(minute=minute):
                self.suggestion["start_min"] = minute
                response = self._put({"team_no": 4, "map_code": "B1", "start_min": minute})
                self.assertEqual(response.status_code, 200)
                _, forced = ec.load_choice_sets(self.path)
                due = self.day_start + minute * 60
                date = time.strftime("%Y-%m-%d", time.localtime(due))
                record = forced[ec.adhoc_key(date, 4, minute)]
                self.assertEqual(record["planned_at"], time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(due)))
                jobs = scheduler.adhoc_due(scheduler.load_config(), forced, due, date)
                self.assertTrue(any(job["key"] == ec.adhoc_key(date, 4, minute) for job in jobs))

    def test_selected_preset_is_bound_to_adopted_shift_and_stale_choice_is_rejected(self):
        self.suggestion.update(formation_id="f4", formation_name="远征四", formation_signature="sig")
        request = {"team_no": 4, "map_code": "B1", "start_min": 600,
                   "formation_id": "f4", "formation_signature": "sig"}
        response = self._put(request)
        self.assertEqual(response.status_code, 200)
        _, forced = ec.load_choice_sets(self.path)
        record = next(iter(forced.values()))
        self.assertEqual(record["formation_id"], "f4")
        self.assertEqual(record["formation_signature"], "sig")
        self.suggestion["start_min"] = 610
        self.suggestion["formation_signature"] = "edited"
        response = self._put({**request, "start_min": 610})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(ec.load_choice_sets(self.path)[1]), 1)

    def test_duplicate_adopt_rejected(self):
        first = self._put({"team_no": 4, "map_code": "B1", "start_min": 600})
        self.assertEqual(first.status_code, 200)
        again = self._put({"team_no": 4, "map_code": "B1", "start_min": 600})
        self.assertEqual(again.status_code, 409)
        self.assertIn("已经点上", again.json()["detail"])

    def test_stale_suggestion_rejected(self):
        response = self._put({"team_no": 4, "map_code": "B1",
                              "start_min": 601})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.path.exists())

    def test_configured_suggestion_saves_changed_time(self):
        with patch.object(server.time, "time", return_value=self.day_start + 500 * 60), \
             patch.object(ea, "party_levels_from_situation", return_value=None):
            response = self._put({"team_no": 4, "map_code": "B1", "source_start_min": 600, "start_min": 720})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(ec.load_choice_sets(self.path)[1].values())[0]["start_min"], 720)

    def test_configured_suggestion_rejects_team_time_conflict(self):
        self.timeline["expeditions"] = [{"key": "other", "team_no": 4, "map_code": "C4", "time_min": 650, "duration_min": 180, "will_run": True}]
        with patch.object(server.time, "time", return_value=self.day_start + 500 * 60):
            response = self._put({"team_no": 4, "map_code": "B1", "source_start_min": 600, "start_min": 720})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.path.exists())

    def test_configured_suggestion_rejects_changed_preset(self):
        with patch.object(server.time, "time", return_value=self.day_start + 500 * 60), \
             patch.object(ea, "expedition_formation_options", return_value=[]):
            response = self._put({"team_no": 4, "map_code": "B1", "source_start_min": 600, "start_min": 720, "formation_id": "gone", "formation_signature": "old"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.path.exists())

    def test_configured_suggestion_persists_chosen_preset(self):
        preset = {"formation_id": "p4", "formation_name": "打刀远征队", "formation_signature": "verified",
                  "party": {"sum": 100, "max": 100, "count": 1, "names": ["加州清光"]}}
        with patch.object(server.time, "time", return_value=self.day_start + 500 * 60), \
             patch.object(ea, "expedition_formation_options", return_value=[preset]), \
             patch.object(ea, "party_levels_from_situation", return_value=None):
            response = self._put({"team_no": 4, "map_code": "B1", "source_start_min": 600, "start_min": 720,
                                  "formation_id": "p4", "formation_signature": "verified"})
        self.assertEqual(response.status_code, 200)
        record = list(ec.load_choice_sets(self.path)[1].values())[0]
        self.assertEqual(record["formation_name"], "打刀远征队")
        self.assertEqual(record["formation_signature"], "verified")

    def test_configured_suggestion_rejects_missing_sword_type(self):
        self.suggestion["map_code"] = "B2"
        party = {"sum": 100, "max": 100, "count": 1, "names": ["三日月宗近"]}
        with patch.object(server.time, "time", return_value=self.day_start + 500 * 60), \
             patch.object(ea, "party_levels_from_situation", return_value={4: party}):
            response = self._put({"team_no": 4, "map_code": "B2", "source_start_min": 600, "start_min": 720})
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有打刀", response.json()["detail"])
        self.assertFalse(self.path.exists())

    def test_edit_saved_expedition_replaces_old_booking(self):
        old = ec.adhoc_key(self._today(), 4, 600)
        self._put({"team_no": 4, "map_code": "B1", "start_min": 600})
        self.timeline["expeditions"] = [{"key": old, "team_no": 4, "map_code": "B1", "time_min": 600, "duration_min": 90, "will_run": True, "toggleable": True}]
        with patch.object(server.time, "time", return_value=self.day_start + 500 * 60), \
             patch.object(ea, "party_levels_from_situation", return_value=None):
            response = self._put({"team_no": 4, "map_code": "B1", "source_key": old, "start_min": 720})
        self.assertEqual(response.status_code, 200)
        records = ec.load_choice_sets(self.path)[1]
        self.assertNotIn(old, records)
        self.assertEqual(len(records), 1)

    def test_unknown_team_rejected(self):
        response = self._put({"team_no": 9, "map_code": "B1",
                              "start_min": 600})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.path.exists())


class HelpPrefsEndpointTests(unittest.TestCase):
    """PUT /api/expedition-help-prefs：校验落盘，GET 并进时间轴。"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "prefs.json"

    def _put(self, payload):
        real = ea.save_prefs

        def to_temp(**kwargs):
            kwargs["path"] = self.path
            return real(**kwargs)

        with patch.object(ea, "save_prefs", side_effect=to_temp):
            return TestClient(server.app).put("/api/expedition-help-prefs",
                                              json=payload)

    def test_save_and_read_back(self):
        response = self._put({"rounds_per_team": 2, "available_teams": [1, 4]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["expedition_help"]
                         ["rounds_per_team"], 2)
        self.assertEqual(ea.load_prefs(self.path)["available_teams"], [1, 4])

    def test_save_explicit_formation_and_reject_wrong_team(self):
        from touken import custom_formations as cf
        with patch.object(cf, "load_formations", return_value=[{"id": "f4", "target_team": 4}]):
            response = self._put({"team_formations": {"4": "f4"}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(ea.load_prefs(self.path)["team_formations"], {"4": "f4"})
            response = self._put({"team_formations": {"5": "f4"}})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(ea.load_prefs(self.path)["team_formations"], {"4": "f4"})

    def test_legacy_teams_out_field_still_accepted(self):
        """旧前端/旧脚本只认 teams_out：兜底读作每队次数。"""
        response = self._put({"teams_out": 3, "available_teams": [4]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["expedition_help"]
                         ["rounds_per_team"], 3)

    def test_validation_error_400(self):
        response = self._put({"rounds_per_team": 9, "available_teams": [1]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.path.exists())

    def test_bad_shape_400(self):
        response = self._put({"rounds_per_team": 1, "available_teams": "14"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.path.exists())


class ExpeditionMapsDataTests(unittest.TestCase):
    """touken/data/expedition_maps.json 刀种要求校验。

    刀种数据来源：4399 远征攻略（2026-09-30 转录），B2~B4 与游戏内截图
    核对一致；等级合计沿用 TapTap 白月魔女统计表（2026-07-26 转录）。
    """

    KNOWN_LEVELS = {"A1": 5, "A2": 10, "A3": 20, "A4": 30,
                    "B1": 50, "B2": 60, "B3": 80, "B4": 100,
                    "C1": 110, "C2": 120, "C3": 130, "C4": 140,
                    "D1": 150, "D2": 180, "D3": 200, "D4": 220,
                    "E1": 240, "E2": 260, "E3": 280, "E4": 300}
    KNOWN_TYPES = {"A2": {"短刀": 1}, "A3": {"胁差": 1},
                   "A4": {"短刀": 1, "胁差": 1},
                   "B2": {"打刀": 1}, "B3": {"太刀": 1},
                   "B4": {"打刀": 1, "太刀": 1},
                   "C2": {"大太刀": 1}, "E2": {"枪": 1}, "E3": {"薙刀": 1}}
    FREE_MAPS = {"A1", "B1", "C1", "C3", "C4",
                 "D1", "D2", "D3", "D4", "E1", "E4"}

    def setUp(self):
        self.maps = ea.load_maps()

    def test_all_20_maps_have_known_type_rules(self):
        """每张图 required_types 必须查实（含「确认自由」={}），不许 null。"""
        self.assertEqual(len(self.maps), 20)
        for code, meta in self.maps.items():
            rules = meta.get("rules") or {}
            with self.subTest(map=code):
                self.assertIsInstance(rules.get("required_types"), dict)
                self.assertNotIn("required_types",
                                 rules.get("unknown_aspects") or [])
                self.assertIn("min_distinct_types", rules)

    def test_levels_match_known_table(self):
        for code, total in self.KNOWN_LEVELS.items():
            with self.subTest(map=code):
                self.assertEqual(self.maps[code]["rules"]["total_level"],
                                 total)
                self.assertEqual(self.maps[code]["level_req"], total)

    def test_type_requirements_match_known_table(self):
        for code in self.maps:
            with self.subTest(map=code):
                self.assertEqual(self.maps[code]["rules"]["required_types"],
                                 self.KNOWN_TYPES.get(code, {}))
                expected_distinct = 4 if code == "E4" else None
                self.assertEqual(
                    self.maps[code]["rules"]["min_distinct_types"],
                    expected_distinct)
        self.assertEqual(self.FREE_MAPS,
                         {c for c in self.maps if c not in self.KNOWN_TYPES})


if __name__ == "__main__":
    unittest.main()


class SharedResourceFocusTests(unittest.TestCase):
    def test_preference_preserves_focus_and_backups_then_restores_auto(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefs.json"
            path.write_text(json.dumps(_prefs()), encoding="utf-8")
            self.assertEqual(ea.load_prefs(path)["resource_focus"], "")
            ea.save_prefs(resource_focus="小判", path=path)
            self.assertEqual(json.loads(path.with_suffix(".json.bak").read_text())["available_teams"], [1, 4])
            ea.save_prefs(rounds_per_team=2, available_teams=[4], path=path)
            self.assertEqual(ea.load_prefs(path)["resource_focus"], "小判")
            ea.save_prefs(resource_focus="", path=path)
            self.assertEqual(ea.load_prefs(path)["rounds_per_team"], 2)
            self.assertEqual(ea.load_prefs(path)["resource_focus"], "")
            with self.assertRaises(ValueError):
                ea.save_prefs(resource_focus="甲州金", path=path)

    def test_manual_choice_changes_actual_recommended_map(self):
        with tempfile.TemporaryDirectory() as folder:
            situation = _write_situation(folder, {})
            args = dict(planning=_planning(), maps=_MAPS, situation_path=situation, now_min=60)
            auto = ea.build_expedition_suggestions(_prefs(available_teams=(4,)), **args)
            manual = ea.build_expedition_suggestions({**_prefs(available_teams=(4,)), "resource_focus": "小判"}, **args)
            self.assertEqual(auto["suggestions"][0]["resource"], "砥石")
            self.assertEqual(manual["suggestions"][0]["resource"], "小判")
            self.assertEqual(manual["suggestions"][0]["map_code"], "D4")


def test_recommendation_uses_explicit_preset_even_when_current_team_can_go(tmp_path):
    situation = tmp_path / "situation.json"
    situation.write_text(json.dumps({"parties": [{"party_no": 5, "members": [
        {"name": "小豆长光", "level": 39}]}]}), encoding="utf-8")
    maps = {"A2": {"duration_min": 20, "冷却材": 45,
                   "rules": {"total_level": 100, "required_types": {"短刀": 1}}}}
    preset = {"formation_id": "f5", "formation_name": "远征五", "formation_signature": "sig",
              "party": {"sum": 120, "max": 60, "names": ["药研藤四郎", "小豆长光"]}}
    args = dict(planning=_planning(limiting=("冷却材",)), maps=maps,
                situation_path=situation, now_min=60)
    with patch.object(ea, "expedition_formation_options", return_value=[preset]):
        out = ea.build_expedition_suggestions({**_prefs(available_teams=(5,)), "team_formations": {"5": "f5"}}, **args)
    assert out["suggestions"][0]["formation_id"] == "f5"
    assert "先换成「远征五」" in out["suggestions"][0]["reason"]
    maps["A2"]["rules"] = {}
    with patch.object(ea, "expedition_formation_options", return_value=[preset]):
        out = ea.build_expedition_suggestions({**_prefs(available_teams=(5,)), "team_formations": {"5": "f5"}}, **args)
    assert out["suggestions"][0]["formation_id"] == "f5"


def test_preset_rejects_borrowing_other_teams_and_unknown_levels():
    from touken import custom_formations as cf
    record = {"id": "f5", "name": "远征五", "target_team": 5,
              "slots": {"1": {"name_zh": "药研藤四郎", "level": 99}}}
    parties = {5: {"names": ["小豆长光"]}, 1: {"names": ["药研藤四郎"]}}
    with patch.object(cf, "load_formations", return_value=[record]), patch.object(
            cf, "resolve_formation_slots", return_value={"ok": True, "slots": record["slots"]}):
        assert ea.expedition_formation_options(5, parties) == []
        parties[1]["names"] = []
        assert ea.expedition_formation_options(5, parties)[0]["party"]["sum"] == 99
        record["slots"]["1"].pop("level")
        assert ea.expedition_formation_options(5, parties) == []


def test_preset_choice_survives_persistence_and_scheduler_slot(tmp_path):
    from panel import scheduler
    from datetime import datetime
    path = tmp_path / "choices.json"
    legacy = {"version": 2, "skipped": {}, "forced": {"old": {
        "team_no": 4, "map_code": "B1", "planned_at": 1}}}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    today = datetime.now().date().isoformat()
    planned = today + "T08:00:00"
    ec.set_forced_adhoc(key=ec.adhoc_key(today, 5, 480), team_no=5, map_code="A2",
        start_min=480, duration_min=20, planned_at=planned, path=path,
        formation_id="f5", formation_name="远征五", formation_signature="sig")
    _, forced = ec.load_choice_sets(path)
    now = datetime.fromisoformat(planned).timestamp()
    cfg = scheduler.load_config()
    jobs = scheduler.adhoc_due(cfg, forced, now, today)
    assert jobs[0]["formation_id"] == "f5"
    slot = scheduler._new_slot(jobs[0], cfg, now)
    assert slot["formation_signature"] == "sig"
    assert forced["old"] == legacy["forced"]["old"]
    assert json.loads(path.with_suffix(".json.bak").read_text(encoding="utf-8")) == legacy


def test_ranked_saved_preset_pins_unique_locked_highest_for_expedition():
    from touken import custom_formations as cf
    record = {"id": "f5", "name": "远征五", "target_team": 5, "slots": {"1": {
        "sword_catalog_id": "x", "form_status": "normal", "selection_policy": "locked_highest_level"}}}
    entry = {"sword_catalog_id": "x", "form_status": "normal", "name_zh": "药研藤四郎", "level": 99, "lock_status": "locked"}
    with patch.object(cf, "load_formations", return_value=[record]), patch.object(
            cf, "_current_candidate_pool", return_value={"done": True, "entries": [entry]}), patch.object(
            cf, "resolve_formation_slots", side_effect=lambda value: {"ok": True, "slots": value["slots"]}):
        out = ea.expedition_formation_options(5, {5: {"names": []}})
        assert out[0]["record"]["slots"]["1"]["level"] == 99
        assert "selection_policy" not in out[0]["record"]["slots"]["1"]
        assert record["slots"]["1"]["selection_policy"] == "locked_highest_level"
        entry["lock_status"] = "unknown"
        assert ea.expedition_formation_options(5, {5: {"names": []}}) == []


@pytest.mark.parametrize("selection", ["", "missing"])
def test_keep_current_and_invalid_selection_never_choose_another_preset(tmp_path, selection):
    situation = _write_situation(tmp_path, [{"party_no": 5, "members": [{"name": "小豆长光", "level": 39}]}])
    maps = {"A2": {"duration_min": 20, "冷却材": 45, "rules": {"total_level": 100}}}
    preset = {"formation_id": "f5", "formation_name": "远征五", "formation_signature": "sig",
              "party": {"sum": 120, "max": 60, "names": ["药研藤四郎", "小豆长光"]}}
    with patch.object(ea, "expedition_formation_options", return_value=[preset]):
        out = ea.build_expedition_suggestions({**_prefs(available_teams=(5,)), "team_formations": {"5": selection}},
            planning=_planning(limiting=("冷却材",)), maps=maps, situation_path=situation)
    assert out["suggestions"] == []
    assert out["note"]


def test_explicit_ineligible_preset_never_falls_back_to_eligible_current_team(tmp_path):
    situation = _write_situation(tmp_path, [{"party_no": 5, "members": [{"name": "小豆长光", "level": 999}]}])
    preset = {"formation_id": "f5", "formation_name": "远征五", "formation_signature": "sig",
              "party": {"sum": 1, "max": 1, "names": ["药研藤四郎"]}}
    with patch.object(ea, "expedition_formation_options", return_value=[preset]):
        out = ea.build_expedition_suggestions({**_prefs(available_teams=(5,)), "team_formations": {"5": "f5"}},
            planning=_planning(), maps={"B1": {"duration_min": 90, "砥石": 180, "rules": {"total_level": 100}}},
            situation_path=situation)
    assert not out["suggestions"]
    assert "指定「远征五」" in out["note"] and "等级" in out["note"]


def test_same_team_can_reuse_selected_preset_after_return_but_other_team_cannot(tmp_path):
    preset = {"formation_id": "f5", "formation_name": "远征五", "formation_signature": "sig",
              "party": {"sum": 120, "max": 60, "names": ["药研藤四郎"]}}
    maps = {"A1": {"duration_min": 20, "砥石": 30, "rules": {}},
            "A2": {"duration_min": 20, "玉钢": 30, "rules": {}}}
    with patch.object(ea, "expedition_formation_options", return_value=[preset]):
        out = ea.build_expedition_suggestions({**_prefs(rounds=2, available_teams=(5,)), "team_formations": {"5": "f5"}},
            planning=_planning(), maps=maps, situation_path=tmp_path / "missing.json")
    assert len(out["suggestions"]) == 2
    assert all(item["formation_id"] == "f5" for item in out["suggestions"])
    assert out["suggestions"][1]["start_min"] >= out["suggestions"][0]["start_min"] + 30
    with patch.object(ea, "expedition_formation_options", return_value=[preset]):
        out = ea.build_expedition_suggestions({**_prefs(available_teams=(4, 5)), "team_formations": {"4": "f5", "5": "f5"}},
            planning=_planning(), maps=maps, situation_path=tmp_path / "missing.json")
    assert len(out["suggestions"]) == 1
    assert "预留" in out["note"]


def test_team_preferences_migrate_preserve_other_fields_backup_and_rollback(tmp_path):
    from touken import custom_formations as cf
    path = tmp_path / "prefs.json"
    old = {"version": 1, "teams_out": 2, "available_teams": [4, 5], "resource_focus": "小判"}
    original = json.dumps(old).encode()
    path.write_bytes(original)
    assert ea.load_prefs(path)["team_formations"] == {}
    assert path.read_bytes() == original
    with patch.object(cf, "load_formations", return_value=[{"id": "f4", "target_team": 4}, {"id": "f5", "target_team": 5}]):
        ea.save_prefs(team_formations={"4": "f4"}, path=path)
        assert path.with_suffix(".json.bak").read_bytes() == original
        ea.save_prefs(team_formations={"5": "f5"}, path=path)
        ea.save_prefs(rounds_per_team=3, resource_focus="玉钢", path=path)
        saved = ea.load_prefs(path)
        assert saved["team_formations"] == {"4": "f4", "5": "f5"}
        assert saved["available_teams"] == [4, 5]
        assert saved["resource_focus"] == "玉钢"
        assert saved["rounds_per_team"] == 3
        ea.save_prefs(team_formations={"4": ""}, path=path)
        assert ea.load_prefs(path)["team_formations"] == {"4": "", "5": "f5"}
        assert ea.load_prefs(path.with_suffix(".json.bak"))["team_formations"]["4"] == "f4"
        before = path.read_bytes()
        for invalid in ({"1": "f5"}, {"9": ""}, {"5": False}, {"5": "deleted"}, []):
            with pytest.raises(ValueError):
                ea.save_prefs(team_formations=invalid, path=path)
            assert path.read_bytes() == before
    path.write_bytes(original)
    assert ea.load_prefs(path)["rounds_per_team"] == 2
    assert ea.load_prefs(path)["team_formations"] == {}


def test_ranked_preset_accepts_real_json_locked_boolean_and_rejects_unknown(tmp_path):
    from touken import custom_formations as cf
    from touken.game_sword_archive import sync_archive, candidate_pool
    from touken.telemetry import TelemetryStore
    store = TelemetryStore(tmp_path / "telemetry.db")
    sync_archive([{"direction": "S->C", "endpoint": "/party/list", "status": 200,
        "ts": "2026-10-02 21:00:00", "payload": {"status": 0, "sword": {"1": {
            "serial_id": 1, "sword_id": 3, "level": 99, "protect": 1,
            "created_at": "2023-01-27 17:19:10"}}}}], store)
    pool = candidate_pool(store)
    record = {"id": "f4", "name": "遠征四", "target_team": 4, "slots": {"1": {
        "sword_catalog_id": "touken_003_mikazuki_munechika", "form_status": "normal",
        "selection_policy": "locked_highest_level"}}}
    with patch.object(cf, "load_formations", return_value=[record]), patch.object(
            cf, "_current_candidate_pool", return_value=pool), patch.object(
            cf, "resolve_formation_slots", side_effect=lambda value: {"ok": True, "slots": value["slots"]}):
        assert ea.expedition_formation_options(4, {4: {"names": []}})[0]["party"]["sum"] == 99
        pool["entries"][0]["locked"] = None
        assert ea.expedition_formation_options(4, {4: {"names": []}}) == []
