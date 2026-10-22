"""通用时间表：不实际启动模拟器，验证保存、换期和设置授权边界。"""
import json
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from panel import day_conductor as dc, day_plan as dp, scheduled_gameplay as gp, server
from touken.flow_control import FlowAborted

DAY = 2_000_000_000


def timeline(script="hanafuda", key="this-event"):
    return {"day_start": DAY, "now": DAY + 500 * 60, "activity": None,
            "gameplay_options": [{"script": script, "label": script, "available": True,
                                   "event_key": key, "end_at": DAY + 86400}]}


def block(script="hanafuda", key="this-event"):
    return {"kind": "activity", "start_min": 600, "script": script,
            "event_key": key, "runs": 7}


@pytest.mark.parametrize("script", gp.SCRIPTS)
def test_worker_uses_saved_settings_overrides_only_count(script):
    saved = {"params": {script: {"team_no": "2", "runs": 15,
                                 "auto_refill": False, "use_koban_refill": False,
                                 "rotate_captain": True}}}
    captured = []

    def run(_config, nodes, **kwargs):
        captured.extend(nodes)
        yield "安全流程"
        return True

    with patch.object(server, "_load_panel_settings", return_value=saved), \
            patch.object(gp, "catalog", return_value=timeline(script)["gameplay_options"]), \
            patch.object(server._workflow, "run_workflow", side_effect=run):
        current = gp.spec(script)
        params = {**block(script), "gameplay_signature": current["signature"]}
        assert list(server._build_scheduled_gameplay("config", params)) == ["安全流程"]
    assert [node["type"] for node in captured] == ["boot_emulator", "login", script]
    effective = captured[-1]["params"]
    assert effective["runs"] == 7
    assert effective["team_no"] == "2"
    assert effective["rotate_captain"] is True
    assert effective["auto_refill"] is False
    assert effective["use_koban_refill"] is False
    assert all("on_error" not in node for node in captured)
    assert saved["params"][script]["runs"] == 15


def test_count_change_does_not_change_authorization_but_spending_does():
    saved = {"params": {"raid": {"runs": 9, "auto_refill": False}}}
    with patch.object(server, "_load_panel_settings", return_value=saved):
        signature = gp.spec("raid")["signature"]
        saved["params"]["raid"]["runs"] = 1
        assert gp.spec("raid")["signature"] == signature
        saved["params"]["raid"]["auto_refill"] = True
        assert gp.spec("raid")["signature"] != signature


def test_worker_refuses_changed_event_before_any_node():
    with patch.object(server, "_load_panel_settings", return_value={}), \
            patch.object(gp, "catalog", return_value=timeline(key="next-event")["gameplay_options"]), \
            patch.object(server._workflow, "run_workflow") as run:
        params = {**block(), "gameplay_signature": gp.spec("hanafuda")["signature"]}
        with pytest.raises(FlowAborted, match="换期"):
            list(server._build_scheduled_gameplay("config", params))
        run.assert_not_called()


def test_gameplay_failure_is_not_completed():
    def failed(*args, **kwargs):
        yield "步骤没有完成"
        return False

    with patch.object(server, "_load_panel_settings", return_value={}), \
            patch.object(gp, "catalog", return_value=timeline()["gameplay_options"]), \
            patch.object(server._workflow, "run_workflow", side_effect=failed):
        params = {**block(), "gameplay_signature": gp.spec("hanafuda")["signature"]}
        with pytest.raises(FlowAborted, match="未完成"):
            list(server._build_scheduled_gameplay("config", params))


def test_plan_and_conductor_backup_preserve_old_data(tmp_path):
    plan_path, state_path = tmp_path / "plan.json", tmp_path / "state.json"
    old = dp.save_plan(DAY, None, [{"kind": "daily", "start_min": 500}], plan_path)
    dc.arm(old, timeline(), dc.BUILTIN_ID, {}, state_path)
    old_state = state_path.read_bytes()
    plan = dp.save_plan(DAY, None, [block()], plan_path)
    with patch.object(server, "_load_panel_settings", return_value={}):
        state = dc.arm(plan, timeline(), dc.BUILTIN_ID, {}, state_path)
        assert state_path.with_suffix(".json.bak").read_bytes() == old_state
        assert dp.load_plan(plan_path)["blocks"] == [block()]
        assert dc.load_state(state_path)["blocks"][0]["kind"] == "activity"
        state["blocks"][0]["status"] = "ended"
        dc._save(state, state_path)
        saved = dc.arm(plan, timeline(), dc.BUILTIN_ID, {}, state_path)
        assert saved["blocks"][0]["status"] == "ended"
    assert dp.load_plan(plan_path.with_suffix(".json.bak")) == old
    assert json.loads(old_state)["blocks"][0]["kind"] == "daily"
    plan_path.write_bytes(plan_path.with_suffix(".json.bak").read_bytes())
    state_path.write_bytes(old_state)
    assert dp.load_plan(plan_path) == old
    assert dc.load_state(state_path)["blocks"][0]["kind"] == "daily"


@pytest.mark.parametrize("change", ["unavailable", "event", "settings"])
def test_due_gameplay_blocks_on_drift(tmp_path, change):
    class Runner:
        def start(self, *args):
            raise AssertionError("不能启动任务")

    saved = {"params": {"hanafuda": {"use_koban_refill": False}}}
    with patch.object(server, "_load_panel_settings", return_value=saved):
        state = dc.arm({"day_start": DAY, "blocks": [block()]}, timeline(), "", {}, tmp_path / "s.json")
        current = timeline()
        if change == "unavailable":
            current["gameplay_options"][0]["available"] = False
        elif change == "event":
            current["gameplay_options"][0]["event_key"] = "next-event"
        else:
            saved["params"]["hanafuda"]["use_koban_refill"] = True
        result = dc._start_block(state["blocks"][0], state, Runner(), lambda: current,
                                 lambda: {}, {}, "config", DAY + 600 * 60, lambda *args: None)
        assert result is False
        assert state["blocks"][0]["status"] == "blocked"


def test_scoped_settings_save_preserves_other_gameplay_and_count(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "pixel", "params": {
        "daily": {"custom": "留着"}, "raid": {"runs": 12, "auto_refill": False}}}), encoding="utf-8")
    with patch.object(server, "_SETTINGS_FILE", path):
        response = TestClient(server.app).put("/api/gameplay-settings/raid", json={
            "params": {"runs": 99, "auto_refill": True, "foreign": "不应保存"}})
    assert response.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["theme"] == "pixel"
    assert saved["params"]["daily"] == {"custom": "留着"}
    assert saved["params"]["raid"] == {"runs": 12, "auto_refill": True}


def test_event_windows_advance_and_permanent_gameplay_stays_available():
    now = datetime(2026, 10, 2, tzinfo=gp.events._TZ).timestamp()
    cards = {"大阪城": {"start_date": "2026-10-01", "end_date": "2026-10-03"}}
    first = gp.event_window("osaka", cards, [], now)
    assert first and first["end_at"] > now
    assert gp.event_window("hanafuda", cards, [], now) is None
    assert gp.event_window("sortie", {}, [], now)["event_key"] == "permanent"
    assert gp.event_window("osaka", cards, [], now + 86400 * 3) is None
    cards["大阪城"]["start_date"] = "2026-10-02"
    assert gp.event_window("osaka", cards, [], now)["event_key"] != first["event_key"]


def test_due_gameplay_starts_with_its_own_settings_signature(tmp_path):
    calls = []

    class Runner:
        def start(self, *args):
            calls.append(args)
            return "run-1"

    with patch.object(server, "_load_panel_settings", return_value={}):
        state = dc.arm({"day_start": DAY, "blocks": [block()]}, timeline(), "", {}, tmp_path / "s.json")
        assert dc._start_block(state["blocks"][0], state, Runner(), timeline,
                               lambda: {}, {}, "config", DAY + 600 * 60, lambda *args: None)
    assert calls[0][0] == "scheduled_gameplay"
    assert calls[0][2]["script"] == "hanafuda"
    assert calls[0][2]["runs"] == 7
    assert state["blocks"][0]["status"] == "running"
    assert state["blocks"][0]["run_id"] == "run-1"


def test_invalid_schedule_does_not_replace_existing_plan_or_state():
    with patch.object(server, "_day_timeline_payload", return_value=timeline()), \
            patch.object(server, "_ledger_mode", return_value=False), \
            patch.object(dp, "save_plan") as save, \
            patch.object(dc, "_save") as save_state, \
            patch.object(dc, "load_state", return_value=None), \
            patch.object(dc.workflow, "find_preset", return_value=None):
        response = TestClient(server.app).put("/api/day-timeline/schedule", json={
            "blocks": [{"kind": "workflow", "start_min": 600, "workflow_id": "missing"}]})
    assert response.status_code == 409
    save.assert_not_called()
    save_state.assert_not_called()


def test_changed_formation_requires_new_authorization():
    from touken import custom_formations
    saved = {"params": {"raid": {"team_no": "preset:one"}}}
    formation = {"id": "one", "target_team": 3, "slots": ["A"]}
    with patch.object(server, "_load_panel_settings", return_value=saved), \
            patch.object(custom_formations, "load_formations", return_value=[formation]):
        signature = gp.spec("raid")["signature"]
        formation["slots"] = ["B"]
        assert gp.spec("raid")["signature"] != signature


def test_old_expedition_estimate_does_not_block_selected_gameplay_team():
    current = timeline()
    current["expeditions"] = [{"team_no": 2, "time_min": 590, "duration_min": 90,
                               "state": "running", "will_run": True}]
    saved = {"params": {"hanafuda": {"team_no": "2"}}}
    with patch.object(server, "_load_panel_settings", return_value=saved):
        assert gp.issues(block(), current) == []
        saved["params"]["hanafuda"]["team_no"] = "3"
        assert gp.issues(block(), current) == []


def test_planned_dispatch_still_reserves_its_actual_action_window():
    current = timeline()
    current["expeditions"] = [{"team_no": 2, "time_min": 600, "duration_min": 90,
                               "state": "pending", "will_run": True}]
    with patch.object(server, "_load_panel_settings", return_value={"params": {"hanafuda": {"team_no": "2"}}}):
        assert any("派遣" in problem for problem in gp.issues(block(), current))
