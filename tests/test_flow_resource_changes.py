import pytest
import json
import tempfile
import unittest
from pathlib import Path

from touken.agent import ToukenAgent
from touken.flows.repair import RepairMixin
from touken.flows.rewards import RewardsMixin
from touken.flows.smith import SmithMixin
from touken.maa_adapter import Point, roi_4to4
from touken.telemetry import TelemetryStore

_REPAIR_CFG = {
    "select_btn_x": 1065,
    "select_offset_y": -28,
    "speedup_checkbox": [1162, 251],
    "start_button": {"template": "修复开始.png"},
    "cost_rois": [
        [288, 404, 378, 436],
        [288, 475, 378, 507],
        [288, 546, 378, 578],
        [288, 617, 378, 649],
    ],
}


class _Host(SmithMixin, RepairMixin, RewardsMixin):
    """最小宿主：config + record_event 收集（可选写入真 TelemetryStore）"""

    def __init__(self, config, store=None):
        self.config = config
        self.store = store
        self.events = []
        self.maa = None

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        if self.store is not None:
            return self.store.record_event(event_type, payload)
        return len(self.events)

    def record_resource_change(self, resource, delta, *, source,
                               attribution="confirmed", **evidence):
        return self.record_event(
            "resource.change", resource=resource, delta=delta, source=source,
            attribution=attribution, **evidence)


class ResourceChangeContractTests(unittest.TestCase):
    def test_agent_emits_one_normalized_resource_change(self):
        agent = ToukenAgent.__new__(ToukenAgent)
        events = []
        agent.record_event = lambda event_type, **payload: (
            events.append((event_type, payload)) or 7)

        event_id = agent.record_resource_change(
            "小判", -300, source="ticket.refilled",
            source_event_id=6, evidence="confirmed_refill_flow")

        self.assertEqual(event_id, 7)
        self.assertEqual(events, [("resource.change", {
            "resource": "小判", "delta": -300,
            "source": "ticket.refilled", "attribution": "confirmed",
            "source_event_id": 6, "evidence": "confirmed_refill_flow",
        })])

    def test_agent_rejects_unknown_zero_and_fractional_changes(self):
        agent = ToukenAgent.__new__(ToukenAgent)
        events = []
        agent.record_event = lambda event_type, **payload: events.append(
            (event_type, payload))

        self.assertIsNone(agent.record_resource_change(
            "不存在", -1, source="test"))
        self.assertIsNone(agent.record_resource_change(
            "小判", 0, source="test"))
        self.assertIsNone(agent.record_resource_change(
            "小判", 1.5, source="test"))
        self.assertEqual(events, [])


class _RepairFakeMaa:
    """_repair_one 用：成本 OCR 按 ROI 出数，修复开始模板给点，确认弹窗没有"""

    def __init__(self, cost_tokens):
        self.cost_tokens = cost_tokens  # {tuple(roi): [(text, point), ...]}
        self.clicked = []

    def click(self, pt):
        self.clicked.append((pt.x, pt.y))
        return True

    def screenshot(self, force=False):
        return None

    def ocr_all(self, roi):
        return self.cost_tokens.get(tuple(roi.to_list()), [])

    def template_match(self, template, roi=None, threshold=0.8):
        if template == "修复开始.png":
            return Point(1150, 600)
        return None


class _RewardFakeMaa:
    """报酬弹窗用：标题命中；格子按 cells 配置出图标匹配和数量文本"""

    def __init__(self, cells, title_found=True):
        self.cells = cells  # {格号: (匹配模板 or None, 数量文本 or None)}
        self.title_found = title_found
        self.saved = []

    def screenshot(self, force=False):
        return None

    def ocr(self, expected, roi, match_mode="exact"):
        return Point(640, 70) if self.title_found else None

    def template_match(self, template, roi=None, threshold=0.8):
        # 从 icon_roi 的 x 反推格号：x = 311 + i*110
        if roi is None:
            return None
        x1 = roi.to_list()[0]
        i = round((x1 - 311) / 110)
        cell = self.cells.get(i)
        if cell and cell[0] == template:
            return Point(x1 + 50, 200)
        return None

    def ocr_all(self, roi):
        x1, y1 = roi.to_list()[:2]
        i = round((x1 - 311) / 110)
        cell = self.cells.get(i)
        if cell and cell[1] is not None and y1 == 260:
            return [(cell[1], Point(x1 + 50, 278))]
        return []

    def save_screenshot(self, path, force=True):
        self.saved.append((path, force))
        return True


class ForgeResourceChangeTests(unittest.TestCase):
    def test_recipe_from_config_and_fallback(self):
        host = _Host({"forge": {"recipe": [950, 950, 950, 950]}})
        self.assertEqual(host._forge_recipe(), [950, 950, 950, 950])
        self.assertEqual(_Host({})._forge_recipe(), [700, 700, 700, 700])
        self.assertEqual(_Host({"forge": {"recipe": [1, 2]}})._forge_recipe(),
                         [700, 700, 700, 700])

    def test_forge_emits_five_confirmed_changes(self):
        host = _Host({"forge": {"recipe": [700, 700, 700, 700]}})
        host._emit_forge_costs(started_event_id=42)

        changes = [p for t, p in host.events if t == "resource.change"]
        self.assertEqual(len(changes), 5)
        by_res = {p["resource"]: p for p in changes}
        for name in ("木炭", "玉钢", "冷却材", "砥石"):
            self.assertEqual(by_res[name]["delta"], -700)
        self.assertEqual(by_res["委托符"]["delta"], -1)
        for p in changes:
            self.assertEqual(p["source"], "forge.started")
            self.assertEqual(p["attribution"], "confirmed")
            self.assertEqual(p["evidence"], "known_recipe")
            self.assertEqual(p["source_event_id"], 42)

    def test_forge_recipe_change_follows_config(self):
        host = _Host({"forge": {"recipe": [350, 350, 350, 350]}})
        host._emit_forge_costs(started_event_id=None)
        by_res = {p["resource"]: p for t, p in host.events
                  if t == "resource.change"}
        self.assertEqual(by_res["木炭"]["delta"], -350)
        self.assertNotIn("source_event_id", by_res["木炭"])


class _DismantlePreviewFakeMaa:
    def __init__(self, values):
        self.values = values

    def screenshot(self, force=False):
        return None

    def ocr_all(self, roi):
        key = tuple(roi.to_list())
        value = self.values.get(key)
        return [] if value is None else [(value, Point(key[0] + 10, key[1] + 10))]


class DismantleResourceChangeTests(unittest.TestCase):
    ROIS = (
        (590, 196, 635, 235),
        (730, 196, 775, 235),
        (870, 196, 915, 235),
        (1010, 196, 1055, 235),
    )

    def test_preview_reads_four_resources_and_repairs_narrow_one(self):
        values = {tuple(roi_4to4(*roi).to_list()): value
                  for roi, value in zip(self.ROIS, ("4", "4", "i", "4"))}
        host = _Host({})
        host.maa = _DismantlePreviewFakeMaa(values)

        preview = host._read_dismantle_resources()

        self.assertEqual(preview, {
            "木炭": 4, "玉钢": 4, "冷却材": 1, "砥石": 4})

    def test_success_emits_confirmed_values_and_unknown_for_failed_roi(self):
        host = _Host({})
        host._emit_dismantle_resources(
            {"木炭": 4, "玉钢": 4, "冷却材": None, "砥石": 4}, 88)

        changes = [payload for event, payload in host.events
                   if event == "resource.change"]
        self.assertEqual(len(changes), 4)
        by_resource = {payload["resource"]: payload for payload in changes}
        for resource in ("木炭", "玉钢", "砥石"):
            self.assertEqual(by_resource[resource]["delta"], 4)
            self.assertEqual(by_resource[resource]["attribution"], "confirmed")
            self.assertEqual(by_resource[resource]["source_event_id"], 88)
        self.assertIsNone(by_resource["冷却材"]["delta"])
        self.assertEqual(by_resource["冷却材"]["attribution"], "unknown")
        self.assertIn("读取失败", by_resource["冷却材"]["note"])


class RepairResourceChangeTests(unittest.TestCase):
    def _run_repair_one(self, cost_tokens, need_speed=False):
        host = _Host({"repair": _REPAIR_CFG})
        host.maa = _RepairFakeMaa(cost_tokens)
        messages = list(host._repair_one(_REPAIR_CFG, name_y=200,
                                         need_speed=need_speed))
        return host, messages

    def test_ocr_success_emits_confirmed_costs(self):
        tokens = {tuple(roi_4to4(*roi).to_list()): [(text, Point(330, 420))]
                  for roi, text in zip(_REPAIR_CFG["cost_rois"],
                                       ("264", "429", "429", "198"))}
        host, messages = self._run_repair_one(tokens, need_speed=True)

        self.assertIn("[手入] 已开工", messages)
        changes = [p for t, p in host.events if t == "resource.change"]
        self.assertEqual(len(changes), 5)
        by_res = {p["resource"]: p for p in changes}
        self.assertEqual(by_res["木炭"]["delta"], -264)
        self.assertEqual(by_res["玉钢"]["delta"], -429)
        self.assertEqual(by_res["砥石"]["delta"], -198)
        self.assertEqual(by_res["加速符"]["delta"], -1)
        for name in ("木炭", "玉钢", "冷却材", "砥石"):
            self.assertEqual(by_res[name]["attribution"], "confirmed")
            self.assertEqual(by_res[name]["evidence"], "repair_confirm_ocr")
            self.assertEqual(by_res[name]["source"], "repair.confirm_screen")
        self.assertEqual(by_res["加速符"]["attribution"], "confirmed")

    def test_ocr_partial_failure_marks_unknown(self):
        tokens = {tuple(roi_4to4(*_REPAIR_CFG["cost_rois"][0]).to_list()):
                  [("264", Point(330, 420))],
                  tuple(roi_4to4(*_REPAIR_CFG["cost_rois"][1]).to_list()):
                  [("429", Point(330, 484))],
                  # 冷却材 OCR 出乱码，砥石读空
                  tuple(roi_4to4(*_REPAIR_CFG["cost_rois"][2]).to_list()):
                  [("---", Point(330, 548))]}
        host, messages = self._run_repair_one(tokens)

        self.assertIn("[手入] 已开工", messages)
        by_res = {p["resource"]: p for t, p in host.events
                  if t == "resource.change"}
        self.assertEqual(by_res["木炭"]["delta"], -264)
        for name in ("冷却材", "砥石"):
            self.assertIsNone(by_res[name]["delta"])
            self.assertEqual(by_res[name]["attribution"], "unknown")
            self.assertIn("读取失败", by_res[name]["note"])
        self.assertNotIn("加速符", by_res)

    def test_no_cost_rois_falls_back_to_unknown(self):
        # cost_rois 缺失（运行中配置没加载新键）也按四条 unknown/null 兜底，
        # 不静默漏账；need_speed 的加速符 -1 照发（确定事实）
        cfg = {k: v for k, v in _REPAIR_CFG.items() if k != "cost_rois"}
        host = _Host({"repair": cfg})
        host.maa = _RepairFakeMaa({})
        messages = list(host._repair_one(cfg, name_y=200, need_speed=True))
        self.assertIn("[手入] 已开工", messages)
        changes = [p for t, p in host.events if t == "resource.change"]
        self.assertEqual(len(changes), 5)
        by_res = {p["resource"]: p for p in changes}
        for name in ("木炭", "玉钢", "冷却材", "砥石"):
            self.assertIsNone(by_res[name]["delta"])
            self.assertEqual(by_res[name]["attribution"], "unknown")
            self.assertIn("读取失败", by_res[name]["note"])
        self.assertEqual(by_res["加速符"]["delta"], -1)
        self.assertEqual(by_res["加速符"]["attribution"], "confirmed")

    def test_start_button_missing_no_bookkeeping(self):
        host = _Host({"repair": _REPAIR_CFG})
        host.maa = _RepairFakeMaa({})
        host.maa.template_match = lambda *a, **k: None
        messages = list(host._repair_one(_REPAIR_CFG, name_y=200,
                                         need_speed=False))
        self.assertNotIn("[手入] 已开工", messages)
        self.assertEqual(host.events, [])


class RewardPopupTests(unittest.TestCase):
    def test_popup_items_and_unknown_icon_skip(self):
        cells = {
            0: ("资源/icon木炭.png", "1,050"),
            1: ("资源/icon小判.png", "650"),
            2: (None, "300"),  # 图标不认识但数量在 → 跳过并记 note
            # 第 3 格起什么都没有 → 扫描停止
        }
        host = _Host({})
        host.maa = _RewardFakeMaa(cells)

        items, notes = host._read_reward_popup()

        self.assertEqual(items, [("木炭", 1050), ("小判", 650)])
        self.assertEqual(len(notes), 1)
        self.assertIn("图标不认识", notes[0])
        self.assertIn("已留取同源运行截图", notes[0])
        self.assertEqual(len(host.maa.saved), 1)
        self.assertFalse(host.maa.saved[0][1])

    def test_duplicate_resource_match_is_ambiguous_and_captures_frame(self):
        cells = {
            0: ("资源/icon委托符.png", "4"),
            1: ("资源/icon委托符.png", "3"),
        }
        host = _Host({})
        host.maa = _RewardFakeMaa(cells)

        items, notes = host._read_reward_popup()

        self.assertEqual(items, [])
        self.assertTrue(any("模板撞车" in note for note in notes), notes)
        self.assertEqual(len(host.maa.saved), 1)

    def test_unknown_icon_with_nonnumeric_ocr_captures_frame(self):
        cells = {
            0: ("资源/icon木炭.png", "500"),
            1: (None, "a"),  # 实机：加速符格的数量 1 被 OCR 成字母 a
        }
        host = _Host({})
        host.maa = _RewardFakeMaa(cells)

        items, notes = host._read_reward_popup()

        self.assertEqual(items, [("木炭", 500)])
        self.assertTrue(any("OCR 未读成数字" in note for note in notes), notes)
        self.assertEqual(len(host.maa.saved), 1)
        self.assertFalse(host.maa.saved[0][1])

    def test_popup_absent_returns_none(self):
        host = _Host({})
        host.maa = _RewardFakeMaa({}, title_found=False)
        self.assertIsNone(host._read_reward_popup())

    def test_emit_changes_link_claimed_event(self):
        host = _Host({})
        host._emit_reward_popup_changes([("木炭", 1050), ("委托符", 2)],
                                        claimed_event_id=77)
        changes = [p for t, p in host.events if t == "resource.change"]
        self.assertEqual(len(changes), 2)
        for p in changes:
            self.assertEqual(p["source"], "task_rewards.reward_popup")
            self.assertEqual(p["attribution"], "confirmed")
            self.assertEqual(p["evidence"], "reward_popup_ocr")
            self.assertEqual(p["source_event_id"], 77)
        self.assertEqual(changes[0]["delta"], 1050)
        self.assertEqual(changes[1]["resource"], "委托符")


class LedgerAggregationTests(unittest.TestCase):
    """三流程的 resource.change 能被 TelemetryStore.resource_ledger 正确聚合"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TelemetryStore(Path(self.temp.name) / "telemetry.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_flow_changes_aggregated_into_attributions(self):
        host = _Host({"forge": {"recipe": [700, 700, 700, 700]}},
                     store=self.store)

        started_id = self.store.record_event("forge.started", {"slot": 1})
        host._emit_forge_costs(started_event_id=started_id)

        host.events.clear()
        tokens = {tuple(roi_4to4(*roi).to_list()): [(text, Point(330, 420))]
                  for roi, text in zip(_REPAIR_CFG["cost_rois"],
                                       ("264", "429", "429", "198"))}
        host.config = {"repair": _REPAIR_CFG}
        host.maa = _RepairFakeMaa(tokens)
        list(host._repair_one(_REPAIR_CFG, name_y=200, need_speed=True))

        claimed_id = self.store.record_event("task_rewards.claimed",
                                             {"tab": "日常"})
        host._emit_reward_popup_changes([("木炭", 1050), ("小判", 650)],
                                        claimed_event_id=claimed_id)

        ledger = self.store.resource_ledger(0, 9999999999)
        attrs = ledger["attributions"]

        charcoal = [a for a in attrs if a["resource"] == "木炭"]
        # 锻刀 -700、手入 -264、任务 +1050
        self.assertEqual(sorted(a["delta"] for a in charcoal),
                         [-700, -264, 1050])
        self.assertTrue(all(a["confidence"] == "confirmed" for a in attrs))
        sources = {a["source"] for a in attrs}
        self.assertEqual(sources, {"forge.started", "repair.confirm_screen",
                                   "task_rewards.reward_popup"})
        ticket = next(a for a in attrs if a["resource"] == "委托符")
        self.assertEqual(ticket["delta"], -1)
        koban = next(a for a in attrs if a["resource"] == "小判")
        self.assertEqual(koban["delta"], 650)
        # 每资源 attributed_delta 汇总正确
        per = {r["resource"]: r for r in ledger["per_resource"]}
        self.assertEqual(per["木炭"]["attributed_delta"], -700 - 264 + 1050)
        self.assertEqual(per["加速符"]["attributed_delta"], -1)

    def test_unknown_null_delta_stays_out_of_attributions(self):
        # delta=null 的 unknown 记账保留事实但不进聚合明细
        host = _Host({}, store=self.store)
        host.record_event("resource.change", resource="砥石", delta=None,
                          source="repair.confirm_screen", script="repair",
                          attribution="unknown", evidence="repair_confirm_ocr",
                          note="发生了修理消耗但数值读取失败")

        ledger = self.store.resource_ledger(0, 9999999999)

        self.assertEqual(ledger["attributions"], [])
        # 事件本身仍在 events 表里可查
        rows = self.store.recent_events(event_type="resource.change")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["payload"]["delta"])
        self.assertEqual(rows[0]["payload"]["attribution"], "unknown")


class SpeedupDedupTests(unittest.TestCase):
    """加速符去重：同 run 有逐笔记账时 session_completed 的汇总让位，老数据照常"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TelemetryStore(Path(self.temp.name) / "telemetry.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _event(self, ts, event_type, payload, run_id=None, script="repair"):
        cursor = self.store._conn().execute(
            "INSERT INTO events(ts, run_id, script, event_type, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, run_id, script, event_type,
             json.dumps(payload, ensure_ascii=False)))
        self.store._conn().commit()
        return cursor.lastrowid

    def _per_repair(self, ts, run_id=None):
        return self._event(ts, "resource.change",
                           {"resource": "加速符", "delta": -1,
                            "source": "repair.confirm_screen", "script": "repair",
                            "attribution": "confirmed", "evidence": "known_recipe"},
                           run_id=run_id)

    def _speedup_attrs(self, ledger):
        return [a for a in ledger["attributions"] if a["resource"] == "加速符"]

    def test_same_run_session_completed_yields_to_per_repair(self):
        t0 = 1786600000.0
        for i in range(3):
            self._per_repair(t0 + i, run_id="run-a")
        self._event(t0 + 10, "repair.session_completed",
                    {"repaired": 3, "speedups": 3, "source": "osaka"},
                    run_id="run-a", script="osaka")

        attrs = self._speedup_attrs(self.store.resource_ledger(0, 9999999999))

        # 只算逐笔的 3 条，session_completed 的 -3 不再计入
        self.assertEqual(len(attrs), 3)
        self.assertTrue(all(a["delta"] == -1 for a in attrs))
        self.assertTrue(all(a["source"] == "repair.confirm_screen" for a in attrs))
        per = {r["resource"]: r for r in
               self.store.resource_ledger(0, 9999999999)["per_resource"]}
        self.assertEqual(per["加速符"]["attributed_delta"], -3)

    def test_legacy_session_completed_still_counted(self):
        # 没有逐笔记账的老数据：照常靠 session_completed 归因
        t0 = 1786600000.0
        self._event(t0, "repair.session_completed",
                    {"repaired": 2, "speedups": 3, "source": "osaka"},
                    run_id="run-old", script="osaka")

        attrs = self._speedup_attrs(self.store.resource_ledger(0, 9999999999))

        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0]["delta"], -3)
        self.assertEqual(attrs[0]["source"], "repair.session_completed")

    def test_cross_run_does_not_shadow(self):
        # run-a 有逐笔，run-b 只有 session_completed：各算各的
        t0 = 1786600000.0
        self._per_repair(t0, run_id="run-a")
        self._event(t0 + 10, "repair.session_completed",
                    {"repaired": 1, "speedups": 2, "source": "osaka"},
                    run_id="run-b", script="osaka")

        attrs = self._speedup_attrs(self.store.resource_ledger(0, 9999999999))

        self.assertEqual(sorted(a["delta"] for a in attrs), [-2, -1])

    def test_null_run_id_falls_back_to_window_presence(self):
        # run_id 缺失的兜底：窗内有逐笔记录就跳过 session_completed
        t0 = 1786600000.0
        self._per_repair(t0, run_id=None)
        self._event(t0 + 10, "repair.session_completed",
                    {"repaired": 1, "speedups": 1, "source": "osaka"},
                    run_id=None, script="osaka")

        attrs = self._speedup_attrs(self.store.resource_ledger(0, 9999999999))

        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0]["source"], "repair.confirm_screen")


_FLOW_REPAIR_CFG = {
    "blacklist": [],
    "speedup_teams": [3],
    "name_col_roi": [490, 100, 720, 660],
    "prefix_col_roi": [445, 100, 505, 660],
    "scroll_from": [640, 480],
    "scroll_to": [640, 240],
}


class _ScanFakeMaa:
    """repair_stream 全流用：按列 ROI 出名字/编号 token，滑动空转"""

    def __init__(self, names, prefixes):
        self.names = names      # [(text, y)]，可混伤势章和乱码
        self.prefixes = prefixes

    def screenshot(self, force=False):
        return None

    def ocr_all(self, roi):
        lst = roi.to_list()
        if lst == [490, 100, 230, 560]:
            return [(t, Point(580, y)) for t, y in self.names]
        if lst == [445, 100, 60, 560]:
            return [(t, Point(475, y)) for t, y in self.prefixes]
        return []

    def swipe(self, *a, **k):
        return True


class _FlowHost(RepairMixin):
    """repair_stream 最小宿主：导航瞬移、选人界面必开、_repair_one 记决策"""

    def __init__(self, names, prefixes):
        self.config = {"repair": dict(_FLOW_REPAIR_CFG)}
        self.maa = _ScanFakeMaa(names, prefixes)
        self.current_location = "修复"
        self.speed_calls = []
        self.events = []

    def navigate_to_stream(self, dest):
        self.current_location = dest
        yield f"nav {dest}"

    def _open_select_screen(self, cfg):
        return True

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        return len(self.events)

    def _repair_one(self, cfg, name_y, need_speed):
        self.speed_calls.append(need_speed)
        yield "[手入] 已开工"
        return True


class SpeedupDecisionTests(unittest.TestCase):
    """加速符只给带伤势章（轻伤/中伤/重伤）的刀"""

    def _run(self, names, prefixes):
        host = _FlowHost(names, prefixes)
        messages = list(host.repair_stream())
        return host, messages

    def test_stamped_speed_team_uses_speedup(self):
        host, _ = self._run([("五虎退", 200), ("中伤", 195)],
                            [("三之一", 200)])
        self.assertEqual(host.speed_calls, [True])
        queued = next(p for t, p in host.events if t == "repair.queued")
        self.assertEqual(queued["injury"], "中伤")
        self.assertTrue(queued["speedup"])

    def test_unstamped_speed_team_saves_speedup(self):
        host, messages = self._run([("小夜左文字", 200)], [("三之二", 200)])
        self.assertEqual(host.speed_calls, [False])
        self.assertTrue(any("省一张加速符" in m for m in messages))
        queued = next(p for t, p in host.events if t == "repair.queued")
        self.assertIsNone(queued["injury"])
        self.assertFalse(queued["speedup"])

    def test_stamped_non_speed_team_no_speedup(self):
        host, _ = self._run([("今剑", 200), ("轻伤", 198)],
                            [("一之一", 200)])
        self.assertEqual(host.speed_calls, [False])

    def test_stamp_paired_to_row_by_y(self):
        # 重伤章配给 y 最近的五虎退；远处的轻伤章谁也不配
        host, _ = self._run(
            [("五虎退", 200), ("小夜左文字", 300),
             ("重伤", 205), ("轻伤", 600)],
            [("三之一", 200), ("三之二", 300)])
        self.assertEqual(host.speed_calls, [True, False])
        injuries = [p["injury"] for t, p in host.events
                    if t == "repair.queued"]
        self.assertEqual(injuries, ["重伤", None])

    def test_stamp_no_longer_junk(self):
        host = _FlowHost([("五虎退", 200), ("中伤", 195), ("乱码@#", 250)],
                         [("三之一", 200)])
        rows, junk = host._scan_page(_FLOW_REPAIR_CFG)
        self.assertNotIn("中伤", junk)
        self.assertIn("乱码@#", junk)
        self.assertEqual(rows[0]["injury"], "中伤")


class _ClaimFakeMaa:
    """一键领取全流程假 maa：首点被「任务信息已失效」吞掉，补点后才真领到"""

    def __init__(self):
        self.claim_clicks = 0
        self.claimed = False          # 领取真正生效（按钮变灰）
        self.stale_dismissed = False  # 失效弹窗已确认
        self.popup_shown = False      # 报酬弹窗已弹（补点后）
        self.popup_closed = False

    def screenshot(self, force=False):
        return None

    def click(self, pt):
        if (pt.x, pt.y) == (1130, 480):      # 一键领取按钮
            self.claim_clicks += 1
            if self.claim_clicks > 1:        # 首点被失效弹窗吞掉
                self.claimed = True
                self.popup_shown = True
        elif (pt.x, pt.y) == (640, 580):     # 失效弹窗的确定
            self.stale_dismissed = True
        return True

    def template_match(self, template, roi=None, threshold=0.8):
        if template == "一键领取.png":
            return None if self.claimed else Point(1130, 480)
        if template == "一键领取_灰.png":
            return Point(1130, 480) if self.claimed else None
        if template == "通用_确定.png":
            if self.claim_clicks == 1 and not self.stale_dismissed:
                return Point(640, 580)
            return None
        if (template == "资源/icon木炭.png"
                and self.popup_shown and not self.popup_closed):
            return Point(360, 200)
        return None

    def exists(self, template, roi=None, threshold=0.8):
        if template in ("通用_关闭.png", "ui完成任务.png"):
            return self.popup_shown and not self.popup_closed
        return False

    def ocr(self, expected, roi, match_mode="exact"):
        if (expected == "报酬一览"
                and self.popup_shown and not self.popup_closed):
            return Point(640, 70)
        return None

    def ocr_all(self, roi):
        x1, y1 = roi.to_list()[:2]
        if (self.popup_shown and not self.popup_closed
                and y1 == 260 and x1 == 311):
            return [("1,050", Point(360, 278))]
        return []


class _DelayedRefreshClaimFakeMaa(_ClaimFakeMaa):
    """失效弹窗确认后，任务页需要数帧才重新画出一键领取。"""

    def __init__(self):
        super().__init__()
        self.refresh_frames = 0

    def screenshot(self, force=False):
        if self.stale_dismissed and self.claim_clicks == 1:
            self.refresh_frames += 1
        return None

    def template_match(self, template, roi=None, threshold=0.8):
        if (template == "一键领取.png" and self.stale_dismissed
                and self.claim_clicks == 1 and self.refresh_frames < 3):
            return None
        return super().template_match(template, roi, threshold)


class _GrayClaimButtonFakeMaa(_ClaimFakeMaa):
    """灰按钮会以 0.846 串中蓝模板，但灰模板本身是 1.000。"""

    def __init__(self, activity_available=False):
        super().__init__()
        self.activity_available = activity_available

    def template_match(self, template, roi=None, threshold=0.8):
        if template == "一键领取.png":
            return Point(1130, 480) if threshold <= 0.846 else None
        if template == "一键领取_灰.png":
            return Point(1130, 480) if threshold <= 1.0 else None
        return None

    def exists(self, template, roi=None, threshold=0.8):
        return False

    def ocr(self, expected, roi, match_mode="exact"):
        if expected == "活动":
            return Point(52, 615) if self.activity_available else None
        return super().ocr(expected, roi, match_mode)


class _RewardsHost(RewardsMixin):
    """claim_task_rewards_stream 最小宿主"""

    def __init__(self, maa):
        self.config = {"task_reward": {
            "tabs": {"日常": [48, 204]},
            "claim_button": {"template": "一键领取.png"},
            "popup_close": {"template": "通用_关闭.png"}}}
        self.maa = maa
        self.current_location = "任务"
        self.events = []

    def navigate_to_stream(self, dest):
        self.current_location = dest
        yield f"nav {dest}"

    def _click_point(self, coord):
        pass

    def _click_template_config(self, cfg):
        self.maa.popup_closed = True

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        return len(self.events)


class StalePopupRetryTests(unittest.TestCase):
    """「任务信息已失效」吞掉领取点击：确认后必须补点，补点后的弹窗照记账"""

    def test_stale_popup_then_retry_claim(self):
        maa = _ClaimFakeMaa()
        host = _RewardsHost(maa)
        messages = list(host.claim_task_rewards_stream())

        self.assertEqual(maa.claim_clicks, 2)  # 首点被吞 + 补点
        self.assertTrue(any("任务信息已失效" in m for m in messages))
        self.assertTrue(any("补点一键领取" in m for m in messages))
        claimed = [p for t, p in host.events if t == "task_rewards.claimed"]
        self.assertEqual(len(claimed), 1)
        self.assertFalse(any(t == "task_rewards.unconfirmed"
                             for t, _ in host.events))
        changes = [p for t, p in host.events if t == "resource.change"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["resource"], "木炭")
        self.assertEqual(changes[0]["delta"], 1050)
        self.assertEqual(changes[0]["source"], "task_rewards.reward_popup")
        # claimed 是宿主记录的第 1 条事件，source_event_id 应指向它
        self.assertEqual(changes[0]["source_event_id"], 1)

    def test_stale_popup_waits_for_claim_button_after_refresh(self):
        maa = _DelayedRefreshClaimFakeMaa()
        host = _RewardsHost(maa)

        messages = list(host.claim_task_rewards_stream())

        self.assertGreaterEqual(maa.refresh_frames, 3)
        self.assertEqual(maa.claim_clicks, 2)
        self.assertTrue(any("补点一键领取" in message for message in messages))
        self.assertTrue(any(event == "task_rewards.claimed"
                            for event, _ in host.events))

    def test_gray_claim_button_wins_over_blue_template_crosstalk(self):
        maa = _GrayClaimButtonFakeMaa()
        host = _RewardsHost(maa)

        messages = list(host.claim_task_rewards_stream())

        self.assertEqual(maa.claim_clicks, 0)
        self.assertTrue(any("已确认没有可领奖励" in message
                            for message in messages))
        self.assertTrue(any(event == "task_rewards.none"
                            for event, _ in host.events))
        self.assertFalse(any(event == "task_rewards.unconfirmed"
                             for event, _ in host.events))

    def test_missing_activity_tab_is_skipped_instead_of_clicking_main_tab(self):
        maa = _GrayClaimButtonFakeMaa()
        host = _RewardsHost(maa)
        host.config["task_reward"]["tabs"] = {"活动": [52, 615]}

        messages = list(host.claim_task_rewards_stream())

        self.assertTrue(any("当前没有活动任务标签" in message
                            for message in messages))
        self.assertTrue(any("不领取主线" in message for message in messages))
        self.assertEqual(maa.claim_clicks, 0)
        self.assertEqual(host.events, [])

    def test_activity_tab_is_still_processed_when_it_returns(self):
        maa = _GrayClaimButtonFakeMaa(activity_available=True)
        host = _RewardsHost(maa)
        host.config["task_reward"]["tabs"] = {"活动": [52, 615]}

        messages = list(host.claim_task_rewards_stream())

        self.assertTrue(any("切换到 活动" in message for message in messages))
        self.assertTrue(any("已确认没有可领奖励" in message
                            for message in messages))
        self.assertTrue(any(event == "task_rewards.none"
                            for event, _ in host.events))


if __name__ == "__main__":
    unittest.main()


@pytest.fixture(autouse=True)
def confirmed_operations_for_accounting_tests(monkeypatch):
    """Accounting tests assume a confirmed operation; outcome gates are tested separately."""
    monkeypatch.setattr("touken.game_state.begin_operation", lambda maa: {})
    monkeypatch.setattr("touken.game_state.confirm_operation", lambda *args: True)
