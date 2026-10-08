# -*- coding: utf-8 -*-
"""自定义工作流（乐高排班）后端测试。

覆盖：预设 CRUD（含坏 JSON 容错、非法节点/策略/超限拒绝）、
runner 编排（mock agent：全绿顺序、翻车即停、跳过继续、异常计失败、
每节点落 latest_report.json 且 schema 与日课一致）、
假红红线（步骤没跑成必须 ✗）、白名单不误伤、
logout_stream 参数覆盖。
"""

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# server 模块导入时注入出阵类工作流节点；在临时数据目录下做，不碰真实用户数据。
_module_tmp = tempfile.TemporaryDirectory(prefix="workflow_module_test_")
with patch.dict("os.environ", {"MAAMARU_DATA_DIR": _module_tmp.name}):
    import panel.server  # noqa: F401  (import 即完成节点注入与脚本注册)
    from panel import workflow
    from panel.script_runner import _SCRIPTS
    from touken.flows import daily, report_judge
    from touken.flows.logout import LogoutMixin


def _node(type_, on_error=None, params=None):
    n = {"type": type_, "params": params or {}}
    if on_error:
        n["on_error"] = on_error
    return n


class _FakeAgent:
    """按剧本回应各 stream 调用的假 Agent。"""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []
        self.current_location = "本丸"

    def _gen(self, name, spec):
        for item in spec:
            if isinstance(item, Exception):
                raise item
            yield item

    def __getattr__(self, name):
        # 只兜底 *_stream 方法；其余属性（maa/config 等）不存在
        if not name.endswith("_stream"):
            raise AttributeError(name)
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self._gen(name, self.script.get(name, ["[fake] ✓ 顺利完成"]))
        return method

    # 工作流 runner 直接调的冷启动/收尾方法
    def _ensure_game_started(self):
        yield from self._gen("_ensure_game_started",
                             self.script.get("_ensure_game_started", ["[fake] 游戏已启动"]))
        return True

    def login(self):
        self.calls.append(("login", (), {}))
        return True

    def _popup_sweep(self, max_rounds=30):
        self.calls.append(("_popup_sweep", (), {"max_rounds": max_rounds}))
        return True

    def _daily_update_gate(self):
        yield from self._gen("_daily_update_gate", self.script.get("_daily_update_gate", []))
        return True

    def navigate_to_stream(self, target):
        self.current_location = target
        yield from self._gen("navigate_to_stream",
                             self.script.get("navigate_to_stream", []))

    def quick_peek(self, **kwargs):
        self.calls.append(("quick_peek", (), kwargs))

    def set_progress(self, step):
        self.calls.append(("set_progress", (), {"step": step}))


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="workflow_store_test_")
        self._patch = patch.object(workflow, "STATUS_DIR", Path(self._tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_create_and_roundtrip(self):
        preset = workflow.create_preset({
            "name": "早课",
            "nodes": [_node("signin"), _node("logout", on_error="continue",
                                             params={"mode": "shutdown"})],
        })
        self.assertTrue(preset["id"])
        loaded = workflow.load_presets()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "早课")
        self.assertEqual(loaded[0]["nodes"][1]["on_error"], "continue")
        self.assertEqual(loaded[0]["nodes"][1]["params"], {"mode": "shutdown"})
        # 缺省 on_error 归一为 stop
        self.assertEqual(loaded[0]["nodes"][0]["on_error"], "stop")

    def test_update_and_delete(self):
        preset = workflow.create_preset({"name": "A", "nodes": [_node("signin")]})
        updated = workflow.update_preset(preset["id"], {
            "name": "B", "nodes": [_node("signin"), _node("snapshot")]})
        self.assertEqual(updated["name"], "B")
        self.assertEqual(len(updated["nodes"]), 2)
        # id 以路径为准，body 里的 id 不顶用
        updated2 = workflow.update_preset(preset["id"], {"id": "hacked",
                                                           "name": "C",
                                                           "nodes": [_node("signin")]})
        self.assertEqual(updated2["id"], preset["id"])
        self.assertTrue(workflow.delete_preset(preset["id"]))
        self.assertEqual(workflow.load_presets(), [])
        self.assertFalse(workflow.delete_preset(preset["id"]))
        self.assertIsNone(workflow.update_preset("no-such", {"name": "X",
                                                             "nodes": [_node("signin")]}))

    def test_bad_json_is_backed_up_and_reset(self):
        path = Path(self._tmp.name) / "workflows.json"
        path.write_text("{这不是 JSON", encoding="utf-8")
        self.assertEqual(workflow.load_presets(), [])
        backups = list(Path(self._tmp.name).glob("workflows.json.bad-*"))
        self.assertEqual(len(backups), 1)
        # 重置后能正常存取
        workflow.create_preset({"name": "A", "nodes": [_node("signin")]})
        self.assertEqual(len(workflow.load_presets()), 1)

    def test_unknown_node_type_rejected(self):
        with self.assertRaises(workflow.WorkflowError):
            workflow.create_preset({"name": "X",
                                    "nodes": [_node("signin"), _node("hack_master")]})

    def test_invalid_on_error_rejected(self):
        with self.assertRaises(workflow.WorkflowError):
            workflow.create_preset({"name": "X",
                                    "nodes": [_node("signin", on_error="ignore")]})

    def test_over_30_nodes_rejected(self):
        with self.assertRaises(workflow.WorkflowError):
            workflow.create_preset({"name": "X",
                                    "nodes": [_node("signin")] * 31})

    def test_empty_name_rejected(self):
        with self.assertRaises(workflow.WorkflowError):
            workflow.create_preset({"name": "  ", "nodes": [_node("signin")]})

    def test_battle_node_type_accepted_after_server_injection(self):
        preset = workflow.create_preset({"name": "出阵流",
                                         "nodes": [_node("login"), _node("raid"),
                                                   _node("logout")]})
        self.assertEqual([n["type"] for n in preset["nodes"]],
                         ["login", "raid", "logout"])


class NodeCatalogTests(unittest.TestCase):
    def test_catalog_shape_and_categories(self):
        catalog = {n["type"]: n for n in workflow.node_catalog()}
        for type_ in ("boot_emulator", "login", "signin", "expedition",
                      "sortie", "logout"):
            self.assertIn(type_, catalog)
        self.assertEqual(catalog["boot_emulator"]["category"], "cold")
        self.assertEqual(catalog["login"]["category"], "cold")
        self.assertEqual(catalog["signin"]["category"], "chore")
        self.assertEqual(catalog["expedition"]["category"], "chore")
        self.assertEqual(catalog["sortie"]["category"], "battle")
        self.assertEqual(catalog["logout"]["category"], "finish")
        for node in catalog.values():
            self.assertTrue(node["label"])
            self.assertIsInstance(node["params"], list)

    def test_battle_nodes_reuse_script_param_schemas(self):
        for script in ("practice", "raid", "edocastle", "sortie",
                       "yosari", "osaka", "pumpkin", "sakura", "forge", "repair"):
            self.assertEqual(workflow.NODE_REGISTRY[script]["params"],
                             _SCRIPTS[script]["params"], script)


class WorkflowApiTests(unittest.TestCase):
    """HTTP 契约：前端按这个形状开发。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        self._tmp = tempfile.TemporaryDirectory(prefix="workflow_api_test_")
        # 路由里真正读的是 workflow 模块的 STATUS_DIR（load_presets 等直接用），
        # 必须 patch 这一处；只 patch server.STATUS_DIR 会写进真实用户数据目录
        # （全量跑时 runtime_paths 早已被别的测试 import，模块头的环境变量
        # 补丁不再生效，9-04 实测把测试预设写进了老大的 workflows.json）。
        self._patch = patch.object(workflow, "STATUS_DIR", Path(self._tmp.name))
        self._patch.start()
        self.client = TestClient(panel.server.app)

    def tearDown(self):
        self.client.close()
        self._patch.stop()
        self._tmp.cleanup()

    def test_crud_roundtrip_over_http(self):
        r = self.client.post("/api/workflows", json={
            "name": "短流程", "nodes": [_node("signin"), _node("snapshot")]})
        self.assertEqual(r.status_code, 200, r.text)
        preset = r.json()["preset"]
        r = self.client.get("/api/workflows")
        self.assertEqual([p["id"] for p in r.json()["presets"]], [workflow.DAILY_PRESET_ID, preset["id"]])

        r = self.client.put(f"/api/workflows/{preset['id']}", json={
            "name": "改名", "nodes": [_node("signin")]})
        self.assertEqual(r.status_code, 200, r.text)

        r = self.client.get("/api/workflows/nodes")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.json()["nodes"]), 10)

        r = self.client.delete(f"/api/workflows/{preset['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([p["id"] for p in self.client.get("/api/workflows").json()["presets"]], [workflow.DAILY_PRESET_ID])

    def test_invalid_payload_is_400_and_not_persisted(self):
        r = self.client.post("/api/workflows", json={
            "name": "坏", "nodes": [_node("nope")]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/workflows", json={
            "name": "坏", "nodes": [_node("signin", on_error="garbage")]})
        self.assertEqual(r.status_code, 400)
        r = self.client.put("/api/workflows/no-such", json={
            "name": "X", "nodes": [_node("signin")]})
        self.assertEqual(r.status_code, 404)
        r = self.client.delete("/api/workflows/no-such")
        self.assertEqual(r.status_code, 404)
        self.assertEqual([p["id"] for p in self.client.get("/api/workflows").json()["presets"]], [workflow.DAILY_PRESET_ID])


class WorkflowRunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="workflow_runner_test_")
        self._patch = patch.object(workflow, "STATUS_DIR", Path(self._tmp.name))
        self._patch.start()
        self._notify = patch("touken.notify.notify_destination", return_value=None)
        self._notify.start()
        self.agent_calls = []

    def tearDown(self):
        self._notify.stop()
        self._patch.stop()
        self._tmp.cleanup()

    def _run(self, nodes, agent=None, config_path="fake-config.json"):
        agent = agent or _FakeAgent()
        made = []

        def make_agent(path):
            made.append(path)
            return agent

        messages = list(workflow.run_workflow(config_path, nodes, make_agent))
        return messages, agent, made

    def _latest_report(self):
        return json.loads(
            (Path(self._tmp.name) / "latest_report.json").read_text("utf-8"))

    def test_all_green_runs_in_order(self):
        agent = _FakeAgent()
        messages, agent, made = self._run(
            [_node("signin"), _node("snapshot"), _node("task_rewards")], agent)
        order = [c[0] for c in agent.calls if c[0].endswith("_stream")]
        self.assertEqual(order, ["signin_stream", "status_snapshot_stream",
                                 "claim_task_rewards_stream"])
        self.assertEqual(made, ["fake-config.json"])
        self.assertTrue(any("全绿" in m for m in messages))
        report = self._latest_report()
        self.assertTrue(report["finished"])
        self.assertTrue(report["all_green"])
        self.assertEqual(report["steps"], [
            {"name": "签到", "status": "✓"},
            {"name": "库存快照", "status": "✓"},
            {"name": "领任务奖励", "status": "✓"},
        ])
        # 成绩单 schema 与日课一致
        self.assertEqual(set(report),
                         {"run_id", "finished_at", "finished", "all_green", "steps"})

    def test_failure_stops_and_marks_rest_skipped(self):
        agent = _FakeAgent({
            "signin_stream": ["[签到] ✓ 签到了"],
            "claim_free_gift_stream": ["[万屋] 未找到暖心礼包，停"],
            "naihanka_stream": ["[内番] 不该跑到这"],
        })
        messages, agent, made = self._run(
            [_node("signin"), _node("free_gift"), _node("naihanka")], agent)
        ran = [c[0] for c in agent.calls if c[0].endswith("_stream")]
        self.assertEqual(ran, ["signin_stream", "claim_free_gift_stream"])
        self.assertTrue(any("翻车即停" in m for m in messages))
        report = self._latest_report()
        self.assertEqual(report["steps"], [
            {"name": "签到", "status": "✓"},
            {"name": "万屋领免费礼包", "status": "✗ 未找到暖心礼包"},
            {"name": "内番", "status": "⏭ 跳过（翻车即停）"},
        ])
        self.assertFalse(report["all_green"])
        # ⏭ 不算翻车项
        self.assertFalse(any("内番" in m and "翻车项" in m for m in messages))

    def test_on_error_continue_marks_fail_and_keeps_going(self):
        agent = _FakeAgent({
            "claim_free_gift_stream": ["[万屋] 未识别到领取按钮，停"],
            "naihanka_stream": ["[内番] ✓ 已安排上工"],
        })
        self._run([_node("free_gift", on_error="continue"), _node("naihanka")], agent)
        report = self._latest_report()
        self.assertEqual(report["steps"], [
            {"name": "万屋领免费礼包", "status": "✗ 未识别到领取按钮，未点击"},
            {"name": "内番", "status": "✓"},
        ])
        self.assertFalse(report["all_green"])

    def test_exception_in_node_counts_as_failure(self):
        agent = _FakeAgent({
            "signin_stream": [RuntimeError("MAA 炸了")],
            "status_snapshot_stream": ["[快照] ✓ 拍完了"],
        })
        messages, agent, _ = self._run(
            [_node("signin", on_error="continue"), _node("snapshot")], agent)
        self.assertTrue(any("节点执行翻车" in m for m in messages))
        report = self._latest_report()
        self.assertTrue(report["steps"][0]["status"].startswith("✗"))
        self.assertEqual(report["steps"][1]["status"], "✓")

    def test_fake_green_is_rejected(self):
        """假红红线：步骤根本没跑成的话术必须判 ✗。"""
        agent = _FakeAgent({
            "signin_stream": ["[签到] 没找到领取奖励按钮，停"],
        })
        self._run([_node("signin")], agent)
        self.assertTrue(self._latest_report()["steps"][0]["status"].startswith("✗"))

    def test_whitelist_wording_stays_green(self):
        agent = _FakeAgent({
            "signin_stream": ["[签到] 没有领取奖励按钮（今天签过了？），跳过"],
        })
        self._run([_node("signin")], agent)
        self.assertEqual(self._latest_report()["steps"][0]["status"], "✓")

    def test_edocastle_operational_wording_stays_green(self):
        """9-04 假红冤案：江户城「入场先遇敌，停止跳过并处理阵形」是正常
        操作播报，撞上词表里的「停止」被误判 ✗——6 圈 119 钥匙白跑。
        出阵类长循环的话术必须能被白名单捞回。"""
        self.assertFalse(report_judge._is_fail("[江户城] 入场先遇敌，停止跳过并处理阵形"))
        # 真翻车话术仍然判红，白名单不许误捞
        self.assertTrue(report_judge._is_fail("[江户城] 没找到活动入口，停"))

    def test_detail_status_from_shop_special_judge(self):
        agent = _FakeAgent({
            "claim_free_gift_stream": ["[SHOP] 今日暖心礼包已售罄，跳过"],
        })
        self._run([_node("free_gift")], agent)
        self.assertEqual(self._latest_report()["steps"][0]["status"],
                         "✓ 此前已领取（售罄）")

    def test_login_failure_stops_workflow(self):
        class NoStartAgent(_FakeAgent):
            def _ensure_game_started(self):
                yield "[fake] 点了游戏图标"
                return False

        messages, agent, made = self._run(
            [_node("login"), _node("signin")], NoStartAgent())
        # agent 会建（进节点后才确认游戏没启动），但登录块内部拦下、签到没跑
        self.assertEqual(made, ["fake-config.json"])
        report = self._latest_report()
        self.assertTrue(report["steps"][0]["status"].startswith("✗"))
        self.assertEqual(report["steps"][1]["status"], "⏭ 跳过（翻车即停）")
        # 签到没跑
        self.assertFalse(any(c[0] == "signin_stream" for c in agent.calls))

    def test_boot_emulator_runs_before_agent_creation(self):
        import touken.emulator as emulator

        emitted = []

        def fake_ensure(adb_path, address, manager_path=None, instance=0,
                        emit=print, max_wait_s=360):
            emit("[模拟器] ADB 连不上，正在启动模拟器...")
            emit("[模拟器] 开机完毕，ADB 已连接 ✓")
            return True

        config_path = str(Path(self._tmp.name) / "touken.json")
        (Path(self._tmp.name) / "touken.json").write_text(
            json.dumps({"adb_path": "adb.exe", "adb_address": "127.0.0.1:1"}),
            encoding="utf-8")

        agent = _FakeAgent()
        made = []
        with patch.object(emulator, "ensure_emulator", fake_ensure):
            messages = list(workflow.run_workflow(
                config_path,
                [_node("boot_emulator"), _node("signin")],
                lambda path: (made.append(path) or agent)))

        self.assertEqual(made, [config_path])  # 先开模拟器再建 agent
        self.assertTrue(any("模拟器已就绪" in m for m in messages))
        report = self._latest_report()
        self.assertEqual(report["steps"][0], {"name": "开模拟器", "status": "✓"})
        self.assertTrue(report["all_green"])

    def test_boot_emulator_failure_skips_everything(self):
        import touken.emulator as emulator

        agent = _FakeAgent()
        made = []
        with patch.object(emulator, "ensure_emulator",
                          lambda *a, **k: False):
            messages = list(workflow.run_workflow(
                "cfg.json", [_node("boot_emulator"), _node("signin")],
                lambda path: (made.append(path) or agent)))
        self.assertEqual(made, [])  # 模拟器没起来，agent 都不建
        report = self._latest_report()
        self.assertTrue(report["steps"][0]["status"].startswith("✗"))
        self.assertEqual(report["steps"][1]["status"], "⏭ 跳过（翻车即停）")
        self.assertFalse(any(c[0] == "signin_stream" for c in agent.calls))

    def test_scheduled_raid_cold_start_and_failures(self):
        from panel import server, day_conductor
        from touken.flow_control import FlowAborted
        import touken.emulator as emulator

        config_path = Path(self._tmp.name) / "scheduled.json"
        config_path.write_text('{}', encoding="utf-8")
        signature = day_conductor.workflow_spec(day_conductor.BUILTIN_ID, {})["signature"]
        params = {"workflow_id": day_conductor.BUILTIN_ID,
                  "scheduled_raid_runs": 8, "scheduled_workflow_signature": signature}

        for failed in (None, "boot", "game", "login", "sweep", "update"):
            with self.subTest(failed=failed):
                calls = []

                class Agent(_FakeAgent):
                    def _ensure_game_started(self):
                        calls.append("game")
                        yield "[fake] 检查游戏启动"
                        return failed != "game"

                    def login(self):
                        calls.append("login")
                        return failed != "login"

                    def _popup_sweep(self, **kwargs):
                        calls.append("sweep")
                        return failed != "sweep"

                    def _daily_update_gate(self):
                        calls.append("update")
                        yield "[fake] 检查更新"
                        return None if failed == "update" else False

                def boot(*args, **kwargs):
                    calls.append("boot")
                    return failed != "boot"

                def make_agent(path):
                    calls.append("agent")
                    return Agent()

                def raid(agent, raid_params, path):
                    calls.append("raid")
                    self.assertEqual(raid_params["rounds"], 8)
                    yield "[fake] ✓ 联队战完成"

                raid_node = {**workflow.NODE_REGISTRY["raid"], "run": raid}
                with patch.object(server, "_load_panel_settings", return_value={"params": {}}), \
                     patch.object(server, "_make_agent", make_agent), \
                     patch.object(emulator, "ensure_emulator", boot), \
                     patch.dict(workflow.NODE_REGISTRY, {"raid": raid_node}):
                    if failed:
                        with self.assertRaises(FlowAborted):
                            list(server._build_workflow(str(config_path), params))
                        self.assertNotIn("raid", calls)
                        self.assertFalse(self._latest_report()["all_green"])
                        if failed == "boot":
                            self.assertNotIn("agent", calls)
                    else:
                        list(server._build_workflow(str(config_path), params))
                        self.assertEqual(calls[:7],
                                         ["boot", "agent", "game", "login", "sweep", "update", "raid"])

    def test_logout_node_skips_closing_navigation(self):
        """下班积木跑过后游戏已关，收尾导航只会撞死在离线设备上——
        日课 9-04 凌晨翻车冤案同款，必须跳过。"""
        agent = _FakeAgent()
        messages, agent, _ = self._run(
            [_node("signin"), _node("logout")], agent)
        self.assertTrue(any(c[0] == "logout_stream" for c in agent.calls))
        self.assertTrue(any("跳过收尾回本丸" in m for m in messages))
        self.assertFalse(any(c[0] == "quick_peek" for c in agent.calls))
        self.assertTrue(any("全绿" in m for m in messages))

    def test_closing_navigation_runs_without_logout_node(self):
        """对照组：没有下班积木时，收尾照常回本丸 + 强制 peek。"""
        agent = _FakeAgent()
        messages, agent, _ = self._run([_node("signin")], agent)
        self.assertTrue(any("收尾：导航回本丸" in m for m in messages))
        self.assertTrue(any(c[0] == "quick_peek" for c in agent.calls))

    def test_closing_sweeps_return_screens_before_peek(self):
        """收尾回本丸后先点穿归来结算屏再拍照（9-04 冤案二号：
        远征归来结算屏亮着没人收，看着像卡死，peek 也拍歪）。"""
        agent = _FakeAgent()
        messages, agent, _ = self._run([_node("signin")], agent)
        self.assertTrue(any("扫一遍归来结算屏" in m for m in messages))
        sweep = [c for c in agent.calls if c[0] == "_popup_sweep"]
        peek = [c for c in agent.calls if c[0] == "quick_peek"]
        self.assertTrue(sweep and peek)
        # 扫地必须在拍照之前
        order = [c[0] for c in agent.calls]
        self.assertLess(max(i for i, n in enumerate(order) if n == "_popup_sweep"),
                        min(i for i, n in enumerate(order) if n == "quick_peek"))


class WorkflowScriptEntryTests(unittest.TestCase):
    def test_missing_preset_yields_error_message(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(workflow, "STATUS_DIR", Path(td)):
            messages = list(panel.server._build_workflow("cfg.json",
                                                         {"workflow_id": "nope"}))
        self.assertTrue(any("找不到预设" in m for m in messages))

    def test_script_registered_hidden(self):
        self.assertIn("workflow", _SCRIPTS)
        self.assertTrue(_SCRIPTS["workflow"]["hidden"])
        # hidden 不出现在普通任务列表
        from panel.script_runner import list_scripts
        self.assertNotIn("workflow", list_scripts())


class JudgeExtractionTests(unittest.TestCase):
    """判分抽取后 daily 的行为不变（同一份词表对象）。"""

    def test_daily_reuses_report_judge(self):
        self.assertIs(daily._is_fail, report_judge._is_fail)
        self.assertIs(daily._FAIL_RE, report_judge._FAIL_RE)
        self.assertIs(daily._shop_report_status, report_judge._shop_report_status)


class LogoutOverrideTests(unittest.TestCase):
    class _Maa:
        adb_path = "adb.exe"
        adb_address = "127.0.0.1:16384"

    def _flow(self, cfg_logout):
        flow = LogoutMixin()
        flow.maa = self._Maa()
        flow.config = {"daily": {"logout": cfg_logout}}
        return flow

    def test_defaults_fall_back_to_config(self):
        flow = self._flow({"kill_game": True, "close_emulator": False,
                           "sleep_pc": False})
        with patch("touken.flows.logout.subprocess.run") as run:
            run.return_value = type("R", (), {"stdout": "", "stderr": "",
                                              "returncode": 0})()
            messages = list(flow.logout_stream())
        # 只杀游戏，不关模拟器不休眠
        self.assertEqual(len(run.call_args_list), 1)
        self.assertIn("force-stop", run.call_args_list[0].args[0])
        self.assertTrue(any("游戏进程已杀" in m for m in messages))

    def test_override_enables_shutdown_and_sleep(self):
        flow = self._flow({"kill_game": True, "close_emulator": False,
                           "sleep_pc": False})
        with patch("touken.flows.logout.subprocess.run") as run, \
                patch("touken.flows.logout.time.sleep"):
            run.return_value = type("R", (), {"stdout": "", "stderr": "",
                                              "returncode": 0})()
            messages = list(flow.logout_stream(close_emulator=True,
                                               sleep_pc=True))
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertTrue(any("force-stop" in cmd for cmd in cmds))
        self.assertTrue(any("taskkill" in cmd for cmd in cmds))
        self.assertTrue(any("powrprof" in arg for cmd in cmds for arg in cmd))
        self.assertTrue(any("模拟器进程已关" in m for m in messages))
        self.assertTrue(any("休眠" in m for m in messages))

    def test_override_can_disable_kill_game(self):
        flow = self._flow({"kill_game": True, "close_emulator": True,
                           "sleep_pc": False})
        with patch("touken.flows.logout.subprocess.run") as run, \
                patch("touken.flows.logout.time.sleep"):
            run.return_value = type("R", (), {"stdout": "", "stderr": "",
                                              "returncode": 0})()
            messages = list(flow.logout_stream(kill_game=False))
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertFalse(any("force-stop" in cmd for cmd in cmds))
        self.assertTrue(any("taskkill" in cmd for cmd in cmds))
        self.assertFalse(any("游戏进程已杀" in m for m in messages))


class WaitUntilNodeTests(unittest.TestCase):
    """定时唤醒积木：时刻解析、下一刻计算（跨零点/刚错过宽限）、
    心跳间隔压看门狗红线、话术不撞翻车词表。"""

    def _ts(self, hhmm, day="2026-09-20"):
        return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").timestamp()

    def test_parse_hhmm(self):
        self.assertEqual(workflow._parse_hhmm("04:05"), 245)
        self.assertEqual(workflow._parse_hhmm("4:05"), 245)
        self.assertEqual(workflow._parse_hhmm(" 04:05 "), 245)
        self.assertEqual(workflow._parse_hhmm("00:00"), 0)
        self.assertEqual(workflow._parse_hhmm("23:59"), 1439)
        for bad in ("", None, "abc", "25:00", "12:60", "4:5", "04:05:00",
                    "04:05x"):
            self.assertIsNone(workflow._parse_hhmm(bad), bad)

    def test_wait_seconds_today(self):
        seconds, mode = workflow._wait_seconds(245, now=self._ts("02:34"))
        self.assertEqual(mode, "today")
        self.assertEqual(seconds, 91 * 60)

    def test_wait_seconds_tomorrow(self):
        # 04:05 已过 15 分钟宽限 → 睡明天同刻
        seconds, mode = workflow._wait_seconds(245, now=self._ts("04:21"))
        self.assertEqual(mode, "tomorrow")
        self.assertEqual(seconds, (24 * 60 - 16) * 60)

    def test_wait_seconds_just_passed_grace(self):
        for hhmm in ("04:05", "04:10", "04:19"):
            seconds, mode = workflow._wait_seconds(245, now=self._ts(hhmm))
            self.assertEqual(mode, "just_passed", hhmm)
            self.assertEqual(seconds, 0)

    def test_wait_seconds_grace_boundary(self):
        # 恰好过 15 分钟 = 宽限内；多 1 秒就睡到明天
        _, mode = workflow._wait_seconds(245, now=self._ts("04:05") + 900)
        self.assertEqual(mode, "just_passed")
        _, mode = workflow._wait_seconds(245, now=self._ts("04:05") + 901)
        self.assertEqual(mode, "tomorrow")

    def test_node_registered_with_time_category(self):
        self.assertIn("wait_until", workflow.NODE_REGISTRY)
        self.assertEqual(workflow.NODE_REGISTRY["wait_until"]["category"], "time")
        entry = next(n for n in workflow.node_catalog()
                     if n["type"] == "wait_until")
        self.assertEqual(entry["category"], "time")
        self.assertEqual(entry["params"][0]["key"], "time")
        node = workflow.normalize_nodes(
            [{"type": "wait_until", "params": {"time": "04:05"}}])[0]
        self.assertEqual(node["type"], "wait_until")

    def test_invalid_time_fails_loud(self):
        msgs = list(workflow._run_wait_until(None, {"time": "25:00"}, "cfg"))
        self.assertEqual(len(msgs), 1)
        # ✗ 开头即失败（workflow._run_node 的双保险），不许假绿
        self.assertTrue(msgs[0].lstrip().startswith("✗"))

    def test_just_passed_skips_sleep(self):
        clock = _FakeClock(self._ts("04:10"))
        with patch.object(workflow, "time", clock):
            msgs = list(workflow._run_wait_until(None, {"time": "04:05"}, "cfg"))
        self.assertEqual(len(msgs), 1)
        self.assertIn("不睡了", msgs[0])
        self.assertEqual(clock.now, self._ts("04:10"))  # 一秒没睡

    def test_sleeps_with_heartbeats_under_watchdog(self):
        clock = _FakeClock(self._ts("02:30"))
        with patch.object(workflow, "time", clock):
            gen = workflow._run_wait_until(None, {"time": "04:05"}, "cfg")
            stamps = []
            for msg in gen:
                stamps.append((clock.now, msg))
        self.assertIn("闹钟定好了", stamps[0][1])
        self.assertIn("04:05", stamps[0][1])
        self.assertIn("到点", stamps[-1][1])
        self.assertTrue(any("还在睡" in m for _, m in stamps))
        # 相邻两条消息的墙钟间隔必须压在 300s 沉默看门狗红线内
        gaps = [b[0] - a[0] for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(gaps)
        self.assertTrue(all(0 < g <= 120.0 for g in gaps), gaps)
        # 一路睡到 04:05（5 秒一片，最后一片可能不满）
        self.assertGreaterEqual(clock.now, self._ts("04:05"))
        self.assertLess(clock.now, self._ts("04:05") + 5.0)

    def test_tomorrow_mode_announced(self):
        clock = _FakeClock(self._ts("23:50"))
        with patch.object(workflow, "time", clock):
            msgs = list(workflow._run_wait_until(None, {"time": "04:05"}, "cfg"))
        self.assertIn("明天 04:05", msgs[0])

    def test_wording_stays_off_fail_list(self):
        """闹钟的正常播报一个词都不许撞翻车词表（假红红线）。"""
        for msg in (
            "[闹钟] ⏰ 闹钟定好了：今天 04:05 起床（还要睡 1 小时 26 分钟），zzZ…",
            "[闹钟] ⏰ 闹钟定好了：明天 04:05 起床（还要睡 4 小时 15 分钟），zzZ…",
            "[闹钟] ⏰ 还在睡，04:05 起床（还有 40 分钟）…",
            "[闹钟] ⏰ 04:05 到点，起床接着干活",
            "[闹钟] ⏰ 04:05 刚过没几分钟，不睡了，直接接着干活",
        ):
            self.assertFalse(report_judge._is_fail(msg), msg)

    def test_wait_node_in_workflow_run(self):
        """拼进完整工作流：闹钟块全绿、后续积木照跑、成绩单落盘。"""
        tmp = tempfile.TemporaryDirectory(prefix="workflow_wait_test_")
        self.addCleanup(tmp.cleanup)
        clock = _FakeClock(self._ts("03:58"))
        agent = _FakeAgent()
        with patch.object(workflow, "STATUS_DIR", Path(tmp.name)), \
                patch.object(workflow, "time", clock), \
                patch("touken.notify.notify_destination", return_value=None):
            messages = list(workflow.run_workflow(
                "fake-config.json",
                [{"type": "wait_until", "params": {"time": "04:05"}},
                 {"type": "signin", "params": {}, "on_error": "stop"}],
                make_agent=lambda path: agent))
        report = json.loads(
            (Path(tmp.name) / "latest_report.json").read_text("utf-8"))
        self.assertEqual(report["steps"], [
            {"name": "定时开启", "status": "✓"},
            {"name": "签到", "status": "✓"},
        ])
        self.assertTrue(report["all_green"])
        self.assertIn(("signin_stream", (), {}), agent.calls)


class _FakeClock:
    """钉死墙钟的假 time 模块：sleep 直接拨快指针。"""

    def __init__(self, start):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def strftime(self, *args, **kwargs):
        return time.strftime(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
