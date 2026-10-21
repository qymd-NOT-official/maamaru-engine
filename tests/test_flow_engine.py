# -*- coding: utf-8 -*-
"""流程引擎（touken/flow_engine）测试。

覆盖：normalize 校验（坏类型/越界坐标/坏 ROI/未知 builtin/模板路径穿越/
跳转目标缺失/坏 on_fail/重 id/空流程）、线性执行顺序、on_fail 三策略、
retry、jump_if 分支、wait_landmark 超时与命中、失败消息进翻车词表、
执行步数上限（死循环保险）、内置积木（安全出阵链/弹窗扫地/回本丸）、
存储备份式写入与坏文件恢复。

FakeAgent 只实现引擎会碰的接口（device / config / navigate_to_stream /
skip_safe / _popup_sweep / _safe_depart_stream / _confirm_departure），
与 tests/test_workflow.py 的 _FakeAgent 同款思路。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from touken import flow_engine
from touken.flow_engine import FlowError
from touken.flows.report_judge import _is_fail
from touken.device_adapter import Point


class FakeMaa:
    """按剧本回应识别的假 DeviceAdapter。"""

    def __init__(self, templates=None, ocrs=None):
        self.templates = templates or {}  # template -> Point | None
        self.ocrs = ocrs or {}            # expected -> Point | None
        self.calls = []                   # ("click", Point) / ("swipe", ...) / ...

    def screenshot(self, force=False):
        self.calls.append(("screenshot", force))
        return None

    def click(self, point):
        self.calls.append(("click", point))
        return True

    def swipe(self, x1, y1, x2, y2, duration_ms=400):
        self.calls.append(("swipe", x1, y1, x2, y2, duration_ms))
        return True

    def template_match(self, template, roi=None, threshold=0.7):
        self.calls.append(("template_match", template, roi, threshold))
        return self.templates.get(template)

    def ocr(self, expected, roi=None, match_mode="contains"):
        self.calls.append(("ocr", expected, roi, match_mode))
        return self.ocrs.get(expected)


class FakeAgent:
    def __init__(self, device=None, config=None):
        self.device = device or FakeMaa()
        self.config = config or {}
        self.current_location = None
        self.calls = []
        self.depart_ok = True
        self.confirm_ok = True
        self.sweep_ok = True

    def _skip_point(self, point=None):
        return (775, 695)

    def skip_safe(self, times=3, interval=0.8, point=None):
        self.calls.append(("skip_safe", times, interval))
        for _ in range(times):
            self.device.click(Point(775, 695))

    def navigate_to_stream(self, target):
        self.calls.append(("navigate_to_stream", target))
        self.current_location = target
        yield f"[NAV] 成功到达: {target}"

    def _popup_sweep(self, max_rounds=30):
        self.calls.append(("_popup_sweep", max_rounds))
        return self.sweep_ok

    def _safe_depart_stream(self, cfg, team_no, tag, **kwargs):
        self.calls.append(("_safe_depart_stream", cfg, team_no, tag, kwargs))
        yield f"{tag} ✓ 安全出阵链走完了"
        return self.depart_ok, False

    def _confirm_departure(self, cfg):
        self.calls.append(("_confirm_departure", cfg))
        return self.confirm_ok


def _step(id_, type_, params=None, on_fail=None, label=None, **extra):
    step = {"id": id_, "type": type_, "params": params or {}}
    if on_fail:
        step["on_fail"] = on_fail
    if label:
        step["label"] = label
    step.update(extra)
    return step


def _flow(name, steps):
    return {"name": name, "steps": steps}


class NormalizeValidationTests(unittest.TestCase):
    def _norm(self, steps, name="测试流程"):
        return flow_engine.normalize_flow(_flow(name, steps))

    def test_minimal_flow_passes(self):
        plan = self._norm([_step("s1", "click_point", {"x": 100, "y": 200})])
        self.assertEqual(plan["name"], "测试流程")
        self.assertEqual(plan["steps"][0]["type"], "click_point")
        self.assertEqual(plan["steps"][0]["on_fail"], "stop")

    def test_bad_step_type_rejected(self):
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "fly_to_moon")])

    def test_out_of_bounds_point_rejected(self):
        for bad in ({"x": 1281, "y": 10}, {"x": 10, "y": 721},
                    {"x": -1, "y": 10}, {"x": "a", "y": 10}):
            with self.assertRaises(FlowError, msg=repr(bad)):
                self._norm([_step("s1", "click_point", bad)])

    def test_bad_roi_rejected(self):
        for bad in ([100, 100, 50, 200], [-1, 0, 10, 10],
                    [0, 0, 1281, 10], [0, 0, 10], [0, 0, 10, 720.5], "xyxy"):
            with self.assertRaises(FlowError, msg=repr(bad)):
                self._norm([_step("s1", "check",
                                  {"template": "a.png", "roi": bad})])

    def test_template_path_traversal_rejected(self):
        for bad in ("../evil.png", "a/../../evil.png", "/abs/evil.png",
                    "D:/evil.png", "a\\..\\evil.png", "no_suffix.jpg",
                    "", "..", "a//b.png"):
            with self.assertRaises(FlowError, msg=repr(bad)):
                self._norm([_step("s1", "check", {"template": bad})])

    def test_unknown_builtin_rejected(self):
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "builtin", {"name": "hack_depart"})])

    def test_builtin_safe_depart_requires_activity(self):
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "builtin",
                              {"name": "safe_depart", "team_no": 1})])

    def test_check_needs_template_or_text(self):
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "check", {})])

    def test_jump_target_must_exist(self):
        steps = [_step("s1", "check", {"template": "a.png"}),
                 _step("s2", "jump_if", {"when": "hit", "target": "ghost"})]
        with self.assertRaises(FlowError):
            self._norm(steps)

    def test_bad_on_fail_and_retry_params(self):
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "click_point", {"x": 1, "y": 1},
                              on_fail="ignore")])
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "click_point", {"x": 1, "y": 1},
                              on_fail="retry", retry_times="好多")])

    def test_duplicate_step_id_rejected(self):
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "click_point", {"x": 1, "y": 1}),
                        _step("s1", "sleep", {"seconds": 1})])

    def test_empty_and_oversized_flow_rejected(self):
        with self.assertRaises(FlowError):
            self._norm([])
        with self.assertRaises(FlowError):
            self._norm([_step(f"s{i}", "sleep", {"seconds": 1})
                        for i in range(flow_engine.MAX_STEPS + 1)])

    def test_bad_name_rejected(self):
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "sleep", {"seconds": 1})], name="  ")
        with self.assertRaises(FlowError):
            self._norm([_step("s1", "sleep", {"seconds": 1})],
                       name="超" * 31)

    def test_normalize_test_step_is_lenient(self):
        step = flow_engine.normalize_test_step(
            {"type": "click_point", "params": {"x": 1, "y": 2}})
        self.assertEqual(step["params"], {"x": 1, "y": 2})
        with self.assertRaises(FlowError):
            flow_engine.normalize_test_step({"type": "nope"})


class LinearExecutionTests(unittest.TestCase):
    def setUp(self):
        self._sleep = patch.object(flow_engine, "_sleep")
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def _run(self, flow, agent=None):
        agent = agent or FakeAgent()
        return list(flow_engine.run_flow(agent, flow)), agent

    def test_steps_run_in_order_and_all_green(self):
        flow = _flow("签到流", [
            _step("s1", "click_point", {"x": 100, "y": 200}, label="点公告"),
            _step("s2", "sleep", {"seconds": 0.1}),
            _step("s3", "skip_safe", {"times": 2, "interval_s": 0.1}),
        ])
        messages, agent = self._run(flow)
        clicks = [c for c in agent.device.calls if c[0] == "click"]
        self.assertEqual(clicks[0][1].to_tuple(), (100, 200))
        # 顺序：点公告 → 睡 → 安全区 2 下
        self.assertEqual(clicks[1][1].to_tuple(), (775, 695))
        self.assertEqual(clicks[2][1].to_tuple(), (775, 695))
        self.assertEqual(len(clicks), 3)
        self.assertTrue(any("全部跑完，全绿" in m for m in messages))
        self.assertTrue(any("========== 流程成绩单 ==========" in m for m in messages))

    def test_messages_carry_flow_name_prefix(self):
        flow = _flow("我的流程", [_step("s1", "sleep", {"seconds": 0.1})])
        messages, _ = self._run(flow)
        banners = [m for m in messages if m.startswith("[我的流程]")]
        self.assertTrue(banners)

    def test_stop_policy_halts_and_skips_rest(self):
        flow = _flow("翻车即停", [
            _step("s1", "click_template", {"template": "a/没了.png"}),
            _step("s2", "click_point", {"x": 1, "y": 1}),
        ])
        messages, agent = self._run(flow)
        ran = [c for c in agent.device.calls if c[0] == "click"]
        self.assertEqual(ran, [])  # 模板没认到，什么都没点
        self.assertTrue(any("剩余 1 步不跑" in m for m in messages))
        self.assertTrue(any("点坐标" in m and "⏭" in m for m in messages))
        self.assertTrue(any("有翻车项" in m for m in messages))
        self.assertFalse(any("全部跑完，全绿" in m for m in messages))

    def test_continue_policy_keeps_going(self):
        flow = _flow("跳过继续", [
            _step("s1", "click_template", {"template": "a/没了.png"},
                  on_fail="continue"),
            _step("s2", "click_point", {"x": 5, "y": 6}),
        ])
        messages, agent = self._run(flow)
        clicks = [c for c in agent.device.calls if c[0] == "click"]
        self.assertEqual(clicks[0][1].to_tuple(), (5, 6))
        self.assertTrue(any("按设定跳过继续" in m for m in messages))
        self.assertTrue(any("有翻车项" in m for m in messages))

    def test_retry_policy_repeats_then_stops(self):
        flow = _flow("重试两次", [
            _step("s1", "click_template", {"template": "a/没了.png"},
                  on_fail="retry", retry_times=2, retry_interval_s=0),
            _step("s2", "click_point", {"x": 5, "y": 6}),
        ])
        messages, agent = self._run(flow)
        matches = [c for c in agent.device.calls if c[0] == "template_match"]
        self.assertEqual(len(matches), 3)  # 1 初跑 + 2 重试
        self.assertTrue(any("第 1/2 次" in m for m in messages))
        self.assertTrue(any("第 2/2 次" in m for m in messages))
        # retry 用尽 = 翻车即停，后面不跑
        self.assertFalse(any(c[0] == "click" for c in agent.device.calls))

    def test_retry_recovers_on_second_attempt(self):
        attempts = {"n": 0}

        class FlakyMaa(FakeMaa):
            def template_match(self, template, roi=None, threshold=0.7):
                attempts["n"] += 1
                return None if attempts["n"] == 1 else Point(50, 60)

        flow = _flow("重试成功", [
            _step("s1", "click_template", {"template": "a/按钮.png"},
                  on_fail="retry", retry_times=2, retry_interval_s=0),
        ])
        messages, agent = self._run(flow, FakeAgent(FlakyMaa()))
        clicks = [c for c in agent.device.calls if c[0] == "click"]
        self.assertEqual(clicks[0][1].to_tuple(), (50, 60))
        self.assertTrue(any("全部跑完，全绿" in m for m in messages))


class JumpIfTests(unittest.TestCase):
    def setUp(self):
        self._sleep = patch.object(flow_engine, "_sleep")
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def _run(self, flow, agent=None):
        agent = agent or FakeAgent()
        return list(flow_engine.run_flow(agent, flow)), agent

    def test_jump_forward_when_hit(self):
        flow = _flow("分支", [
            _step("s1", "check", {"template": "a/入口.png"}),
            _step("s2", "jump_if", {"when": "hit", "target": "s4"}),
            _step("s3", "click_point", {"x": 1, "y": 1}, label="被跳过的步"),
            _step("s4", "click_point", {"x": 9, "y": 9}, label="落地步"),
        ])
        device = FakeMaa(templates={"a/入口.png": Point(30, 40)})
        messages, agent = self._run(flow, FakeAgent(device))
        clicks = [c for c in agent.device.calls if c[0] == "click"]
        self.assertEqual([c[1].to_tuple() for c in clicks], [(9, 9)])
        self.assertTrue(any("条件成立" in m for m in messages))

    def test_no_jump_when_miss(self):
        flow = _flow("分支", [
            _step("s1", "check", {"template": "a/入口.png"}),
            _step("s2", "jump_if", {"when": "hit", "target": "s4"}),
            _step("s3", "click_point", {"x": 1, "y": 1}, label="正常往下"),
            _step("s4", "click_point", {"x": 9, "y": 9}),
        ])
        messages, agent = self._run(flow, FakeAgent(FakeMaa()))
        clicks = [c for c in agent.device.calls if c[0] == "click"]
        self.assertEqual([c[1].to_tuple() for c in clicks], [(1, 1), (9, 9)])
        self.assertTrue(any("条件不成立" in m for m in messages))

    def test_jump_on_miss_condition(self):
        flow = _flow("分支", [
            _step("s1", "check", {"template": "a/入口.png"}),
            _step("s2", "jump_if", {"when": "miss", "target": "s4"}),
            _step("s3", "click_point", {"x": 1, "y": 1}, label="被跳过的步"),
            _step("s4", "click_point", {"x": 9, "y": 9}),
        ])
        messages, agent = self._run(flow, FakeAgent(FakeMaa()))
        clicks = [c for c in agent.device.calls if c[0] == "click"]
        self.assertEqual([c[1].to_tuple() for c in clicks], [(9, 9)])

    def test_jump_without_check_fails(self):
        flow = _flow("瞎跳", [
            _step("s1", "jump_if", {"when": "hit", "target": "s2"}),
            _step("s2", "click_point", {"x": 1, "y": 1}),
        ])
        messages, agent = self._run(flow)
        self.assertFalse(any(c[0] == "click" for c in agent.device.calls))
        self.assertTrue(any("有翻车项" in m for m in messages))
        fail_lines = [m for m in messages if m.lstrip().startswith("✗")]
        self.assertTrue(fail_lines)
        for line in fail_lines:
            self.assertTrue(_is_fail(line), line)

    def test_loop_guard_stops_endless_jumps(self):
        # check（永远认不到）→ miss 跳回 check，没有保险就是死循环
        flow = _flow("死循环", [
            _step("s1", "check", {"template": "a/没有.png"}),
            _step("s2", "jump_if", {"when": "miss", "target": "s1"}),
        ])
        messages, agent = self._run(flow)
        self.assertTrue(any("执行步数超过上限" in m for m in messages))
        self.assertTrue(any("有翻车项" in m for m in messages))


class WaitLandmarkTests(unittest.TestCase):
    def setUp(self):
        self._sleep = patch.object(flow_engine, "_sleep")
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def test_timeout_yields_fail_message_in_wordlist(self):
        flow = _flow("等地标", [
            _step("s1", "wait_landmark",
                  {"template": "a/不来.png", "timeout_s": 1, "stable_hits": 1}),
        ])
        messages = list(flow_engine.run_flow(FakeAgent(FakeMaa()), flow))
        fail_lines = [m for m in messages if m.lstrip().startswith("✗")]
        self.assertTrue(fail_lines)
        for line in fail_lines:
            self.assertTrue(_is_fail(line), line)
        self.assertTrue(any("没等到地标" in m for m in messages))

    def test_stable_hits_required(self):
        # 只命中一次不够（stable_hits=2），第二次命中才算就绪
        hits = {"n": 0}

        class FlakyMaa(FakeMaa):
            def template_match(self, template, roi=None, threshold=0.7):
                hits["n"] += 1
                return Point(10, 10) if hits["n"] >= 2 else None

        flow = _flow("等地标", [
            _step("s1", "wait_landmark",
                  {"template": "a/来了.png", "timeout_s": 60, "stable_hits": 2}),
        ])
        messages = list(flow_engine.run_flow(FakeAgent(FlakyMaa()), flow))
        self.assertTrue(any("地标已就绪" in m for m in messages))
        self.assertTrue(any("全部跑完，全绿" in m for m in messages))

    def test_ocr_landmark(self):
        flow = _flow("等字", [
            _step("s1", "wait_landmark",
                  {"ocr_expected": "决定", "timeout_s": 30}),
        ])
        device = FakeMaa(ocrs={"决定": Point(640, 360)})
        messages = list(flow_engine.run_flow(FakeAgent(device), flow))
        self.assertTrue(any("地标已就绪" in m for m in messages))


class BuiltinTests(unittest.TestCase):
    def setUp(self):
        self._sleep = patch.object(flow_engine, "_sleep")
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def _run(self, flow, agent):
        return list(flow_engine.run_flow(agent, flow)), agent

    def test_safe_depart_runs_full_chain(self):
        flow = _flow("出阵", [
            _step("s1", "builtin",
                  {"name": "safe_depart", "activity": "raid", "team_no": 3}),
        ])
        agent = FakeAgent(config={"raid": {"depart_button": {}}})
        messages, agent = self._run(flow, agent)
        kinds = [c[0] for c in agent.calls]
        self.assertIn("_safe_depart_stream", kinds)
        self.assertIn("_confirm_departure", kinds)
        # 整链传的参数：cfg、部队号、tag、伤势阈值
        depart = next(c for c in agent.calls if c[0] == "_safe_depart_stream")
        self.assertEqual(depart[2], 3)
        self.assertEqual(depart[3], "[出阵]")
        self.assertEqual(depart[4].get("repair_threshold"), "light")
        self.assertTrue(any("全部跑完，全绿" in m for m in messages))

    def test_safe_depart_missing_config_fails(self):
        flow = _flow("出阵", [
            _step("s1", "builtin",
                  {"name": "safe_depart", "activity": "ghost"}),
        ])
        messages, agent = self._run(flow, FakeAgent(config={}))
        self.assertFalse(any(c[0] == "_safe_depart_stream" for c in agent.calls))
        self.assertTrue(any("有翻车项" in m for m in messages))

    def test_safe_depart_chain_abort_is_failure(self):
        flow = _flow("出阵", [
            _step("s1", "builtin",
                  {"name": "safe_depart", "activity": "raid"}),
        ])
        agent = FakeAgent(config={"raid": {}})
        agent.depart_ok = False
        messages, agent = self._run(flow, agent)
        self.assertFalse(any(c[0] == "_confirm_departure" for c in agent.calls))
        self.assertTrue(any("有翻车项" in m for m in messages))

    def test_popup_sweep_builtin(self):
        flow = _flow("扫地", [
            _step("s1", "builtin", {"name": "popup_sweep", "max_rounds": 6}),
        ])
        messages, agent = self._run(flow, FakeAgent())
        self.assertIn(("_popup_sweep", 6), agent.calls)
        self.assertTrue(any("全部跑完，全绿" in m for m in messages))

    def test_home_builtin_navigates_and_sweeps(self):
        flow = _flow("回家", [
            _step("s1", "builtin", {"name": "home"}),
        ])
        messages, agent = self._run(flow, FakeAgent())
        self.assertIn(("navigate_to_stream", "本丸"), agent.calls)
        self.assertIn(("_popup_sweep", 6), agent.calls)
        self.assertTrue(any("已回本丸" in m for m in messages))

    def test_builtin_failure_message_in_wordlist(self):
        flow = _flow("回家", [
            _step("s1", "builtin", {"name": "home"}),
        ])
        agent = FakeAgent()
        agent.current_location = "卡住的地方"

        class StuckAgent(FakeAgent):
            def navigate_to_stream(self, target):
                self.calls.append(("navigate_to_stream", target))
                yield f"[NAV] 进入 {target} 失败"

        messages, _ = self._run(flow, StuckAgent())
        # 失败消息：含 ✗ 的运行时消息（成绩单 "  名字: ✗" 缩进行除外），
        # 每一条都必须能被翻车词表认出（builtin 是 tag 前缀格式，同真实玩法流）
        fail_lines = [m for m in messages if "✗" in m and not m.startswith("  ")]
        self.assertTrue(fail_lines)
        for line in fail_lines:
            self.assertTrue(_is_fail(line), line)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flow_engine_store_")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = patch.object(flow_engine, "STATUS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_saves_backup_style_and_reload(self):
        flow = flow_engine.create_flow(_flow("存取", [
            _step("s1", "click_point", {"x": 1, "y": 2})]))
        path = flow_engine._flows_path()
        self.assertTrue(path.is_file())
        # 备份式写入的痕迹：save 前 copy .bak
        flow_engine.create_flow(_flow("第二条", [
            _step("s1", "sleep", {"seconds": 1})]))
        self.assertTrue(path.with_suffix(".json.bak").is_file())
        loaded = flow_engine.load_flows()
        self.assertEqual([f["name"] for f in loaded], ["存取", "第二条"])
        self.assertEqual(loaded[0]["id"], flow["id"])

    def test_bad_file_is_backed_up_and_reset(self):
        path = flow_engine._flows_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(flow_engine.load_flows(), [])
        backups = list(self.dir.glob("custom_flows.json.bad-*"))
        self.assertEqual(len(backups), 1)
        # 坏结构（不是 {"flows": [...]}）也当空，不崩
        path.write_text(json.dumps({"oops": []}), encoding="utf-8")
        self.assertEqual(flow_engine.load_flows(), [])

    def test_update_delete_duplicate(self):
        flow = flow_engine.create_flow(_flow("原始", [
            _step("s1", "click_point", {"x": 1, "y": 2})]))
        updated = flow_engine.update_flow(flow["id"], {"name": "改名"})
        self.assertEqual(updated["name"], "改名")
        self.assertEqual(len(updated["steps"]), 1)
        self.assertIsNone(flow_engine.update_flow("ghost", {"name": "x"}))
        cloned = flow_engine.duplicate_flow(flow["id"])
        self.assertIsNotNone(cloned)
        self.assertNotEqual(cloned["id"], flow["id"])
        self.assertIn("副本", cloned["name"])
        self.assertIsNone(flow_engine.duplicate_flow("ghost"))
        self.assertTrue(flow_engine.delete_flow(flow["id"]))
        self.assertFalse(flow_engine.delete_flow(flow["id"]))
        self.assertEqual([f["id"] for f in flow_engine.load_flows()], [cloned["id"]])

    def test_update_revalidates_steps(self):
        flow = flow_engine.create_flow(_flow("原始", [
            _step("s1", "click_point", {"x": 1, "y": 2})]))
        with self.assertRaises(FlowError):
            flow_engine.update_flow(flow["id"], {
                "steps": [_step("s1", "builtin", {"name": "hack"})]})


class OfficialFlowTests(unittest.TestCase):
    """官方流程加载：resource/base/flows/*.json 只读收编，坏文件跳过不崩。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flow_official_test_")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = patch.object(flow_engine, "STATUS_DIR", self.dir / "status")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(flow_engine, "RESOURCE_DIR", self.dir / "resource")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.flows_dir = self.dir / "resource" / "flows"
        self.flows_dir.mkdir(parents=True)

    def _write(self, name, data):
        data = dict(data)
        data.setdefault("id", name.replace(".json", ""))
        (self.flows_dir / name).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_bad_files_skipped_and_official_flag_set(self):
        (self.flows_dir / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
        self._write("invalid.json", _flow("校验不过", [_step("s1", "nope_type")]))
        self._write("good.json", _flow("官方好流程", [
            _step("s1", "sleep", {"seconds": 1}),
            _step("s2", "jump_if", {"when": "hit", "target": "s1"})]))
        flows = flow_engine.load_official_flows()
        self.assertEqual([f["id"] for f in flows], [flows[0]["id"]])  # 只剩一个
        self.assertEqual(flows[0]["name"], "官方好流程")
        self.assertIs(flows[0]["official"], True)

    def test_missing_dir_is_empty_and_customs_unaffected(self):
        self.flows_dir.rmdir()
        self.assertEqual(flow_engine.load_official_flows(), [])
        self.assertEqual(flow_engine.list_flows(), [])
        self.assertTrue(flow_engine.is_official_flow("builtin-signin") is False)

    def test_copy_flow_turns_official_into_custom(self):
        self._write("t-good.json", _flow("官方好流程", [
            _step("s1", "click_point", {"x": 1, "y": 2})]))
        official = flow_engine.load_official_flows()[0]
        self.assertTrue(flow_engine.is_official_flow("t-good"))
        copied = flow_engine.copy_flow("t-good")
        self.assertIsNotNone(copied)
        self.assertNotEqual(copied["id"], "t-good")
        self.assertNotIn("official", copied)
        self.assertIn("副本", copied["name"])
        self.assertEqual(copied["steps"], official["steps"])
        # 官方还在原地，副本在私货存储里
        self.assertTrue(flow_engine.is_official_flow("t-good"))
        self.assertIn(copied["id"], [f["id"] for f in flow_engine.load_flows()])
        # find_flow 官方优先：同 id 时拿回的是官方件
        flow_engine.save_flows([{"id": "t-good", "name": "私货撞号",
                                 "steps": official["steps"]}])
        self.assertIs(flow_engine.find_flow("t-good")["official"], True)
        self.assertTrue(any("撞了编号" in w for w in flow_engine.flow_conflicts()))
        # 列表合并：官方在前，撞号私货让位
        listed = [f["id"] for f in flow_engine.list_flows()]
        self.assertEqual(listed[0], "t-good")
        self.assertEqual(len([i for i in listed if i == "t-good"]), 1)


if __name__ == "__main__":
    unittest.main()
