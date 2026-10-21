# -*- coding: utf-8 -*-
"""江户城潜入调查：巡游决策与地图档案测试。"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from touken.edo_route import (
    EDOCASTLE_TOUR,
    bfs_distance,
    bfs_path,
    build_graph,
    decide_next,
    load_archive,
)
from touken.flows.battle import BattleMixin
from touken.flows.edocastle import EdocastleMixin


ARCHIVE_PATH = Path(__file__).resolve().parent.parent / "resource" / "base" / "maps" / "edocastle-4.json"


class EdocastleRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = load_archive(ARCHIVE_PATH)
        cls.graph = build_graph(cls.archive)

    # ── 老大的实例场景，必须逐字复现 ──

    def test_16_with_4_steps_goes_11(self):
        """16 剩 4 步 → 11（tour），9/8 已被踩过。"""
        visited = set(EDOCASTLE_TOUR[:EDOCASTLE_TOUR.index(16)]) | {9, 8, 16}
        nxt, mode = decide_next(self.archive, EDOCASTLE_TOUR, 16, visited, 4)
        self.assertEqual(nxt, 11)
        self.assertEqual(mode, "tour")

    def test_11_after_step_refill_goes_8(self):
        """11 步数回后 → 8（tour），12 已被踩过所以 target 是 1，BFS 走 8。"""
        visited = set(EDOCASTLE_TOUR[:EDOCASTLE_TOUR.index(11)]) | {12, 11}
        nxt, mode = decide_next(self.archive, EDOCASTLE_TOUR, 11, visited, 5)
        self.assertEqual(nxt, 8)
        self.assertEqual(mode, "tour")

    def test_19_desperate_rushes_via_17(self):
        """19 烂透 → 沿 BFS 奔王点，第一步是 17。"""
        visited = {21, 20, 19}
        nxt, mode = decide_next(self.archive, EDOCASTLE_TOUR, 19, visited, 2)
        self.assertEqual(nxt, 17)
        self.assertEqual(mode, "rush")

    def test_opening_20_with_5_steps_goes_19(self):
        """开局 20 剩 5 步 → 19。"""
        visited = {21, 20}
        nxt, mode = decide_next(self.archive, EDOCASTLE_TOUR, 20, visited, 5)
        self.assertEqual(nxt, 19)
        self.assertEqual(mode, "tour")

    # ── 边界 ──

    def test_exact_steps_to_boss_rushes(self):
        """步数正好等于到王点距离时，继续巡游的不变量会失败，应 rush。"""
        # cur=7 距王点 1 步；target=1（若 1 未访问）dist(1,2)=2，steps-1=0 < 2
        visited = {21, 20, 19, 17, 18, 6, 4, 10, 14, 13, 15, 16, 9, 8, 11, 12, 7}
        nxt, mode = decide_next(self.archive, EDOCASTLE_TOUR, 7, visited, 1)
        self.assertEqual(nxt, 2)
        self.assertEqual(mode, "rush")

    def test_cur_not_in_tour_rushes(self):
        """cur 不在巡游序上（节点 3）直接 BFS 奔王点。"""
        nxt, mode = decide_next(self.archive, EDOCASTLE_TOUR, 3, {3}, 5)
        self.assertEqual(mode, "rush")
        # 3 的邻边是 4/7，到王点最短第一步是 7
        self.assertEqual(nxt, 7)

    def test_visited_skips_already_visited(self):
        """巡游序上已访问的节点会被跳过，从 20 出发只认 21/20 已访问时 target 是 19。"""
        nxt, mode = decide_next(self.archive, EDOCASTLE_TOUR, 20, {21, 20}, 5)
        self.assertEqual(nxt, 19)

    def test_all_visited_rushes_to_boss(self):
        """巡游序全访问完直奔王点。"""
        visited = set(EDOCASTLE_TOUR)  # 含王点 2
        nxt, mode = decide_next(self.archive, EDOCASTLE_TOUR, 16, visited, 10)
        self.assertEqual(mode, "rush")
        self.assertEqual(nxt, 11)  # 16→11→12→7→2 第一步


class EdocastleArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = load_archive(ARCHIVE_PATH)
        cls.graph = build_graph(cls.archive)

    def test_archive_verified_flag(self):
        self.assertTrue(self.archive.get("verified", False))

    def test_all_tour_edges_exist(self):
        """巡游序中相邻节点必须能在档案里直接走到（或经 BFS 一步）。"""
        for i in range(len(EDOCASTLE_TOUR) - 1):
            a, b = EDOCASTLE_TOUR[i], EDOCASTLE_TOUR[i + 1]
            if b not in self.graph.get(a, set()):
                self.fail(f"巡游边 {a}→{b} 在档案里不是直接相邻边")

    def test_entry_reaches_boss(self):
        entry = self.archive.get("entry", 21)
        boss = self.archive.get("boss", 2)
        dist = bfs_distance(self.graph, entry, boss)
        self.assertIsNotNone(dist)
        self.assertEqual(dist, 5)  # 踩点记录：入口→王点最短 5 步

    def test_graph_is_connected(self):
        """全图连通，任意节点可达王点。"""
        boss = self.archive.get("boss", 2)
        for node in self.archive["nodes"]:
            dist = bfs_distance(self.graph, node["id"], boss)
            self.assertIsNotNone(
                dist, f"节点 {node['id']} 不可达王点"
            )

    def test_boss_is_node_2(self):
        self.assertEqual(self.archive.get("boss"), 2)

    def test_bfs_path_to_boss(self):
        path = bfs_path(self.graph, 20, 2)
        self.assertIsNotNone(path)
        self.assertEqual(path[0], 20)
        self.assertEqual(path[-1], 2)
        self.assertEqual(len(path) - 1, bfs_distance(self.graph, 20, 2))

    def test_no_node_id_zero_or_five_in_archive(self):
        """终审删除了 0/5 两个美术假点。"""
        ids = {node["id"] for node in self.archive["nodes"]}
        self.assertNotIn(0, ids)
        self.assertNotIn(5, ids)


class EdocastleConfigTests(unittest.TestCase):
    def test_example_config_has_edocastle_section(self):
        example_path = Path(__file__).resolve().parent.parent / "touken_config.example.json"
        cfg = json.loads(example_path.read_text(encoding="utf-8-sig"))
        self.assertIn("edocastle", cfg)
        edo = cfg["edocastle"]
        self.assertEqual(edo["difficulty"], 4)
        self.assertEqual(edo["map_archive"], "resource/base/maps/edocastle-4.json")
        self.assertEqual(edo["ticket_price"], 300)
        self.assertEqual(edo["tour"], EDOCASTLE_TOUR)
        self.assertIn("team_ui_ocr", edo)
        self.assertEqual(edo["map_ready"]["expected"], "地图点选择")
        self.assertIn("hud_step_ocr", edo)
        self.assertNotIn("ticket_refill", edo)  # 江户城不是单层确定/取消补票 UI
        recover = cfg["raid"]["ticket_recover"]
        self.assertEqual(recover["popup_button"]["template"], "team/补充.png")
        self.assertEqual(recover["recover_button"]["template"], "team/恢复一个.png")
        self.assertEqual(recover["confirm_button"]["template"], "通用_确定.png")
        self.assertNotIn("tokens", edo)  # 数令牌格已拆除，票尽走游戏补票弹窗
        # 通用安全出阵链依赖的键必须在配置契约里
        self.assertEqual(edo["repair_threshold"], "heavy")  # 虚拟伤害，中伤照跑
        self.assertIn("auto_equip", edo)
        for key in ("injury_deny_button", "injury_stamps",
                    "injury_stamp_roi", "injury_status_roi"):
            self.assertIn(key, edo, f"example 配置缺 edocastle.{key}")


class _MapRunHost(EdocastleMixin):
    """_map_run_stream 的最小宿主：节点全是空点，OCR 读数按剧本给。"""

    def __init__(self, step_reads):
        import tempfile
        from types import SimpleNamespace
        self.device = SimpleNamespace(
            screenshot=lambda force=False: None,
            ocr=lambda expected, roi=None: None,
            ocr_all=lambda roi, image=None: [],
            template_match=lambda *a, **k: None,
            save_screenshot=lambda path: True,
            click=lambda p: True,
        )
        self._root = tempfile.mkdtemp()
        self._step_reads = iter(step_reads)
        self.clicked = []

    def _click_point(self, point):
        self.clicked.append(list(point) if not isinstance(point, list) else point)

    def _read_hud_steps(self, cfg):
        return next(self._step_reads, None)

    def _wait_node_outcome(self, cfg, skip_point, formation_mode,
                           timeout_s=20.0):
        return "map"  # 全是空点，没有战斗

    def _wait_map_landmark(self, cfg, timeout_s=15.0):
        return True

    def _wait_round_end(self, cfg, skip_point, timeout_s=30.0):
        return True

    def skip_safe(self, times, interval=0.8, point=None):
        pass


def _run_map(flow, cfg=None):
    """驱动 _map_run_stream 生成器，返回 (消息列表, 返回值)。"""
    from touken.edo_route import load_archive
    archive = load_archive(ARCHIVE_PATH)
    gen = flow._map_run_stream(
        cfg or {}, archive, EDOCASTLE_TOUR, archive.get("boss", 2), [775, 695],
        "manual", "鱼鳞阵",
    )
    msgs = []
    while True:
        try:
            msgs.append(next(gen))
        except StopIteration as stop:
            return msgs, stop.value


class EdocastleOcrFallbackTests(unittest.TestCase):
    def test_ocr_miss_falls_back_to_pessimistic_estimate(self):
        """读不出步数时按"没回步"悲观估算继续走，最终打到王点收工。"""
        # 开局 6；19 后瞎一次(估 5)；17 后读出 3；13 后瞎(估 2)；
        # 7 后读出 1；点王点后瞎一次(估 0)——王点判定在读数之后，正常收工
        flow = _MapRunHost([6, None, 3, None, 1, None])
        msgs, result = _run_map(flow)
        self.assertEqual(result, (0, True))
        est_msgs = [m for m in msgs if "悲观估算" in m]
        self.assertEqual(len(est_msgs), 3)
        self.assertFalse(any("太瞎了" in m for m in msgs))

    def test_three_consecutive_ocr_misses_stops(self):
        """连续 3 次读不出步数：太瞎了，停。"""
        flow = _MapRunHost([6, None, None, None])
        msgs, result = _run_map(flow)
        self.assertEqual(result, (0, False))
        self.assertTrue(any("太瞎了" in m for m in msgs))


class _BattleGateMaa:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.frame = set()

    def screenshot(self, force=False):
        self.frame = set(next(self.frames, self.frame))

    def template_match(self, template, roi=None, threshold=0.7):
        return template if template in self.frame else None

    def ocr(self, expected, roi=None):
        return expected if expected in self.frame else None


class _BattleGateHost(EdocastleMixin, BattleMixin):
    def __init__(self, frames):
        self.device = _BattleGateMaa(frames)
        self.clicked = []
        self.config = {}

    def _click_point(self, point):
        self.clicked.append(point)


class EdocastleBattleGateTests(unittest.TestCase):
    @patch("touken.flows.edocastle.time.sleep")
    def test_does_not_tap_until_battle_result_then_stops_on_map(self, _sleep):
        flow = _BattleGateHost([
            set(), set(), {"battle/ui战斗结果.png"},
            {"江户城/获得钥匙.png"}, {"江户城/地图难度标签.png"},
        ])
        ok = flow._wait_after_battle(
            "江户城/地图难度标签.png", [775, 695], timeout_s=10)
        self.assertTrue(ok)
        self.assertEqual(flow.clicked, [[775, 695], [775, 695]])

    @patch("touken.flows.edocastle.time.sleep")
    def test_auto_formation_hands_battle_to_result_gate_without_timeout(self, _sleep):
        flow = _BattleGateHost([])
        flow._wait_formation_page = lambda *args, **kwargs: True
        flow.choose_formation = lambda **kwargs: "auto"
        flow._formation_mode_state = lambda *args, **kwargs: self.fail(
            "自动阵形接管后不该再等待阵形标题消失"
        )

        self.assertTrue(flow._fight_one_battle(
            {}, "auto", "鱼鳞阵", [775, 695]
        ))

    @patch("touken.flows.edocastle.time.sleep")
    def test_auto_toggle_current_manual_page_is_confirmed_before_result_gate(self, _sleep):
        """公共阵形层把手动→自动的当前场点完后，江户城仍要确认页面离开。"""
        flow = _BattleGateHost([])
        flow._wait_formation_page = lambda *args, **kwargs: True
        flow.choose_formation = lambda **kwargs: "fixed"
        states = iter(["manual", None])
        flow._formation_mode_state = lambda *args, **kwargs: next(states, None)

        self.assertTrue(flow._fight_one_battle(
            {}, "auto", "鱼鳞阵", [775, 695]
        ))


class _EntryGateHost(_BattleGateHost):
    def __init__(self, frames, auto_marker=False):
        super().__init__(frames)
        self.config = {
            "formation": {
                "verify": {
                    "template": "battle/ui阵形选择.png",
                    "roi": [571, 5, 707, 44],
                }
            }
        }
        self.auto_marker = auto_marker

    def _formation_auto_marker_visible(self):
        return self.auto_marker


class EdocastleEntryGateTests(unittest.TestCase):
    CFG = {
        "map_ready": {
            "expected": "地图点选择",
            "roi": [45, 605, 245, 715],
        }
    }

    @patch("touken.flows.edocastle.time.sleep")
    def test_common_formation_title_stops_manual_mode_tapping(self, _sleep):
        flow = _EntryGateHost([
            set(), {"battle/ui阵形选择.png"},
        ])
        state = flow._wait_entry_map_or_formation(
            self.CFG, [775, 695], "manual", timeout_s=10)
        self.assertEqual(state, "formation")
        self.assertEqual(flow.clicked, [[775, 695]])

    @patch("touken.flows.edocastle.time.sleep")
    def test_auto_marker_stops_before_common_title(self, _sleep):
        flow = _EntryGateHost([set()], auto_marker=True)
        state = flow._wait_entry_map_or_formation(
            self.CFG, [775, 695], "auto", timeout_s=10)
        self.assertEqual(state, "formation")
        self.assertEqual(flow.clicked, [])

    @patch("touken.flows.edocastle.time.sleep")
    def test_round_end_banner_is_returned_without_extra_tap(self, _sleep):
        flow = _BattleGateHost([
            set(), {"battle/ui战斗结果.png"}, {"江户城/调查完了.png"},
        ])
        ok = flow._wait_after_battle(
            "江户城/调查完了.png", [775, 695], timeout_s=10)
        self.assertTrue(ok)
        self.assertEqual(flow.clicked, [[775, 695]])

    @patch("touken.flows.edocastle.time.sleep")
    def test_map_text_ocr_stops_post_battle_tapping(self, _sleep):
        flow = _BattleGateHost([
            set(), {"battle/ui战斗结果.png"}, {"江户城/获得钥匙.png"},
            {"地图点选择"},
        ])
        ok = flow._wait_after_battle(
            None, [775, 695], timeout_s=10,
            target_roi=[45, 605, 245, 715],
            target_ocr_expected="地图点选择")
        self.assertTrue(ok)
        self.assertEqual(flow.clicked, [[775, 695], [775, 695]])


class EdocastleNodeOutcomeTests(unittest.TestCase):
    """点完节点后的统一观察窗：迟到的战斗也必须被认出来。

    2026-09-09 翻车原型：战斗迟了 7 秒才开场，掉进「5 秒阵型窗 +
    12 秒地图窗」之间的盲区，被当成空点一路盲点。
    """

    CFG = {
        "map_ready": {"expected": "地图点选择", "roi": [45, 605, 245, 715]},
    }

    @patch("touken.flows.edocastle.time.sleep")
    def test_late_auto_battle_is_caught_beyond_old_5s_window(self, _sleep):
        """第 8 拍才出现自动标志（战斗迟到），也必须认成战斗。"""
        frames = [set()] * 7 + [{"battle/阵形选择自动.png"}]
        flow = _BattleGateHost(frames)
        outcome = flow._wait_node_outcome(self.CFG, [775, 695], "auto",
                                          timeout_s=20)
        self.assertEqual(outcome, "battle")
        self.assertEqual(flow.clicked, [[775, 695]] * 7)

    @patch("touken.flows.edocastle.time.sleep")
    def test_battle_result_page_counts_as_battle(self, _sleep):
        """战果页已出现 = 战斗已被接管，绝不能当空点继续点穿。"""
        frames = [set(), set(), {"battle/ui战斗结果.png"}]
        flow = _BattleGateHost(frames)
        outcome = flow._wait_node_outcome(self.CFG, [775, 695], "auto",
                                          timeout_s=20)
        self.assertEqual(outcome, "battle_result")
        self.assertEqual(flow.clicked, [[775, 695], [775, 695]])

    @patch("touken.flows.edocastle.time.sleep")
    def test_empty_node_returns_map(self, _sleep):
        frames = [set(), {"地图点选择"}]
        flow = _BattleGateHost(frames)
        outcome = flow._wait_node_outcome(self.CFG, [775, 695], "manual",
                                          timeout_s=20)
        self.assertEqual(outcome, "map")
        self.assertEqual(flow.clicked, [[775, 695]])

    @patch("touken.flows.edocastle.time.sleep")
    def test_all_blind_returns_none(self, _sleep):
        frames = [set()] * 3
        flow = _BattleGateHost(frames)
        outcome = flow._wait_node_outcome(self.CFG, [775, 695], "manual",
                                          timeout_s=0.5)
        self.assertIsNone(outcome)


class _OutcomeMapRunHost(_MapRunHost):
    """节点结果按剧本给：None=啥也没发生，配合 device.ocr 走点空重试/停手。"""

    def __init__(self, step_reads, outcomes):
        super().__init__(step_reads)
        self._outcomes = iter(outcomes)

    def _wait_node_outcome(self, cfg, skip_point, formation_mode,
                           timeout_s=20.0):
        return next(self._outcomes, "map")


MAP_CFG = {"map_ready": {"expected": "地图点选择", "roi": [45, 605, 245, 715]}}


class EdocastleMistapRecoveryTests(unittest.TestCase):
    def test_missed_node_click_retries_without_retreat(self):
        """点空（点击被动画吞掉）：地图还在 → 原地重新决策，不撤退不记账。"""
        # 首次点 19 什么都没发生（None），重试后一路空点到王点收工
        flow = _OutcomeMapRunHost(
            [6, 5, 4, 2, 1, None],
            [None, "map", "map", "map", "map", "map"],
        )
        flow.device.ocr = lambda expected, roi=None: expected  # 地图活着
        msgs, result = _run_map(flow, MAP_CFG)
        self.assertEqual(result, (0, True))
        self.assertTrue(any("点空" in m for m in msgs))
        self.assertFalse(any("撤退" in m for m in msgs))
        # 同一个节点被点了两次
        self.assertEqual(flow.clicked.count(flow.clicked[0]), 2)

    def test_unknown_outcome_stops_without_blind_retreat(self):
        """既没开打也没回地图：停手留证返回失败，不点任何撤退坐标。"""
        flow = _OutcomeMapRunHost([6], [None])
        # device.ocr 默认返回 None：地图也认不出
        msgs, result = _run_map(flow, MAP_CFG)
        self.assertEqual(result, (0, False))
        self.assertTrue(any("停手留证" in m for m in msgs))


class EdocastleInMapTests(unittest.TestCase):
    CFG = {
        "map_landmark": {"template": "江户城/地图难度标签.png"},
        "map_ready": {"expected": "地图点选择", "roi": [45, 605, 245, 715]},
    }

    def test_banner_alone_is_not_map(self):
        """难度旗在战斗结算页背景里也有，单看旗子会误判（2026-09-09 翻车）。"""
        flow = _BattleGateHost([{"江户城/地图难度标签.png"}])
        self.assertFalse(flow._in_map(self.CFG))

    def test_banner_plus_map_text_is_map(self):
        flow = _BattleGateHost([{"江户城/地图难度标签.png", "地图点选择"}])
        self.assertTrue(flow._in_map(self.CFG))


class _BailHost(_BattleGateHost):
    def __init__(self, frames):
        super().__init__(frames)
        import tempfile
        self._root = tempfile.mkdtemp()
        self.skipped = 0

    def skip_safe(self, times, interval=0.8, point=None):
        self.skipped += times


def _run_bail(flow, cfg):
    gen = flow._bail_out_stream(cfg)
    msgs = []
    while True:
        try:
            msgs.append(next(gen))
        except StopIteration as stop:
            return msgs, stop.value


class EdocastleBailOutTests(unittest.TestCase):
    CFG = {
        "map_landmark": {"template": "江户城/地图难度标签.png"},
        "map_ready": {"expected": "地图点选择", "roi": [45, 605, 245, 715]},
        "retreat": {"tab_point": [1240, 595], "home_button": [1050, 429],
                    "confirm_yes": [497, 470]},
        "skip_tap": [775, 695],
    }

    @patch("touken.flows.edocastle.time.sleep")
    def test_menu_not_opening_aborts_before_blind_clicks(self, _sleep):
        """行动选择菜单没打开：只点 tab，不碰返回本丸/确认，绝不谎报回城。"""
        flow = _BailHost([set()])
        msgs, ok = _run_bail(flow, self.CFG)
        self.assertFalse(ok)
        self.assertEqual(flow.clicked, [[1240, 595]])
        self.assertTrue(any("撤退中止" in m for m in msgs))
        self.assertFalse(any("已主动回城" in m for m in msgs))

    @patch("touken.flows.edocastle.time.sleep")
    def test_still_in_map_after_confirm_is_reported_not_lied(self, _sleep):
        """点了确认但人还在地图：如实汇报撤退没生效。"""
        flow = _BailHost([
            {"返回本丸"},  # 菜单打开
            {"江户城/地图难度标签.png", "地图点选择"},  # 撤完仍在地图
        ])
        msgs, ok = _run_bail(flow, self.CFG)
        self.assertFalse(ok)
        self.assertEqual(flow.clicked, [[1240, 595], [1050, 429], [497, 470]])
        self.assertTrue(any("撤退没生效" in m for m in msgs))
        self.assertFalse(any("已主动回城" in m for m in msgs))
        self.assertEqual(flow.skipped, 0)

    @patch("touken.flows.edocastle.time.sleep")
    def test_verified_retreat_reports_honestly(self, _sleep):
        """菜单验证 + 撤完确认离开地图，才报「已主动回城」。"""
        flow = _BailHost([
            {"返回本丸"},
            set(),  # 撤完既没旗也没地图文字 → 确实离开了
        ])
        msgs, ok = _run_bail(flow, self.CFG)
        self.assertTrue(ok)
        self.assertEqual(flow.clicked, [[1240, 595], [1050, 429], [497, 470]])
        self.assertTrue(any("已主动回城" in m for m in msgs))
        self.assertEqual(flow.skipped, 4)


if __name__ == "__main__":
    unittest.main()
