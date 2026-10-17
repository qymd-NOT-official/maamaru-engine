"""运行中的流程身份与状态接口；启动、线程、遥测全部隔离。"""
import tempfile
import unittest
from unittest.mock import Mock, patch

_data = tempfile.TemporaryDirectory(prefix="workflow_status_")
with patch.dict("os.environ", {"MAAMARU_DATA_DIR": _data.name}):
    from panel import server, workflow
    from panel.script_runner import ScriptRunner


class WorkflowRunStatusTests(unittest.TestCase):
    def test_failed_recovery_reaches_panel_as_incomplete_run(self):
        import io
        from panel import worker, script_runner
        from touken.injury_recovery import RecoveryOutcome, RecoveryProgress

        def failed_sortie(config_path, params):
            yield "第 2/3 圈完成"
            RecoveryOutcome(False, RecoveryProgress(3, 2).remaining,
                            "still_injured").raise_if_failed("[出阵]")

        output = io.StringIO()
        with patch.object(worker, "_rescue_stdout"), \
             patch("sys.argv", ["worker", "test_recovery", "fake.json"]), \
             patch.dict(script_runner._SCRIPTS, {"test_recovery": {"label": "测试", "fn": failed_sortie}}), \
             patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as stopped:
                worker.main()
        self.assertEqual(stopped.exception.code, script_runner.EXIT_FLOW_ABORTED)
        self.assertIn("剩余 1 圈未完成", output.getvalue())

        self.proc.stdout = io.StringIO(output.getvalue())
        self.proc.wait.return_value = stopped.exception.code
        store = Mock()
        with patch.object(script_runner, "get_store", return_value=store), \
             patch.object(self.runner, "_emit"), \
             patch("panel.workflow_waits.finish") as finish, \
             patch("sys.stdout", io.StringIO()):
            self.runner._pump(self.proc, "test-recovery", "sortie")
        finish.assert_called_once_with("test-recovery", "failed")
        self.assertIn("已安全停止且未计作完成", store.append.call_args.args[2])

    def setUp(self):
        self.runner = ScriptRunner()
        self.proc = Mock()
        self.proc.poll.return_value = None
        self.preset = {"id": "evening", "name": "晚间日课", "nodes": [], "private": "不下发"}
        patches = [
            patch.object(self.runner, "_spawn", return_value=self.proc),
            patch("panel.script_runner.threading.Thread"),
            patch("touken.telemetry.get_telemetry_store"),
            patch.object(workflow, "find_preset", return_value=self.preset),
            patch.object(server, "get_runner", return_value=self.runner),
            patch.object(server, "_ledger_mode", return_value=False),
            patch.object(server, "_event_hidden_scripts", return_value=[]),
            patch("subprocess.Popen", side_effect=AssertionError("测试禁止启动真实进程")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def start(self):
        return self.runner.start("workflow", "fake.json", {"workflow_id": "evening"})

    def test_identity_is_a_launch_snapshot_and_contains_no_params(self):
        self.assertIsNotNone(self.start())
        self.preset["name"] = "后来改名了"
        identity = self.runner.current_workflow
        self.assertEqual(identity, {"id": "evening", "name": "晚间日课"})
        identity["name"] = "调用者修改"
        self.assertEqual(self.runner.current_workflow["name"], "晚间日课")

    def test_scheduled_raid_has_a_readable_workflow_name(self):
        from panel.day_conductor import BUILTIN_ID
        with patch.object(workflow, "find_preset", return_value=None):
            self.assertIsNotNone(self.runner.start("workflow", "fake.json", {
                "workflow_id": BUILTIN_ID, "scheduled_raid_runs": 18}))
        self.assertEqual(self.runner.current_workflow,
                         {"id": BUILTIN_ID, "name": "今日联队战 · 自动开工"})
        from touken.telemetry import get_telemetry_store
        get_telemetry_store().start_run.assert_called_once()
        self.assertEqual(get_telemetry_store().start_run.call_args.kwargs["label"],
                         "今日联队战 · 自动开工")

    def test_finished_and_next_regular_task_do_not_keep_workflow_identity(self):
        self.start()
        self.proc.poll.return_value = 0
        self.assertIsNone(self.runner.current_workflow)
        def spawn(*args):
            self.proc.poll.return_value = None
            return self.proc
        with patch.object(self.runner, "_spawn", side_effect=spawn):
            self.assertIsNotNone(self.runner.start("practice", "fake.json"))
        self.assertTrue(self.runner.is_running)
        self.assertIsNone(self.runner.current_workflow)

    def test_rejected_start_keeps_the_running_workflow(self):
        self.start()
        self.assertIsNone(self.runner.start("practice", "fake.json"))
        self.assertEqual(self.runner.current_workflow["id"], "evening")

    def test_failed_spawn_does_not_publish_running_identity(self):
        with patch.object(self.runner, "_spawn", side_effect=OSError("模拟启动失败")), \
             patch.object(self.runner, "_emit"):
            self.assertIsNone(self.start())
        self.assertFalse(self.runner.is_running)
        self.assertIsNone(self.runner.current_workflow)

    def test_status_survives_new_http_client_and_clears_when_finished(self):
        from fastapi.testclient import TestClient
        first = TestClient(server.app)
        self.addCleanup(first.close)
        started = first.post("/api/scripts/run", json={"script": "workflow", "params": {"workflow_id": "evening"}})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["workflow"], {"id": "evening", "name": "晚间日课"})
        second = TestClient(server.app)
        self.addCleanup(second.close)
        state = second.get("/api/scripts").json()
        self.assertTrue(state["running"])
        self.assertEqual(state["workflow"], started.json()["workflow"])
        self.proc.poll.return_value = 0
        state = second.get("/api/scripts").json()
        self.assertFalse(state["running"])
        self.assertIsNone(state["workflow"])


if __name__ == "__main__":
    unittest.main()
