"""隔离验证中途等待、续跑、重启、取消以及时间表展示。"""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from panel import server, workflow, workflow_waits as waits, day_conductor as dc


@pytest.fixture(autouse=True)
def isolated_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(waits, "PATH", tmp_path / "waits.json")
    monkeypatch.setattr(workflow, "STATUS_DIR", tmp_path)
    monkeypatch.setattr(workflow, "_finale", lambda *args: iter([]))


def test_wait_releases_execution_and_resume_does_not_repeat_steps(tmp_path, monkeypatch):
    calls = []
    def run(agent, params, config):
        calls.append(params["name"])
        yield "✓ 完成"
    monkeypatch.setitem(workflow.NODE_REGISTRY, "qa_step", {"type": "qa_step", "label": "测试步骤", "run": run})
    now = datetime(2026, 10, 3, 10).timestamp()
    monkeypatch.setattr(workflow.time, "time", lambda: now)
    plan = workflow.normalize_nodes([
        {"type": "qa_step", "params": {"name": "first"}},
        {"type": "wait_until", "params": {"time": "18:00"}},
        {"type": "qa_step", "params": {"name": "second"}}])
    def agent(_):
        return SimpleNamespace(navigate_to_stream=lambda _: iter([]), current_location="本丸")
    with pytest.raises(workflow.WorkflowPaused) as paused:
        list(workflow.run_workflow("qa.json", plan, agent, defer_wait=True))
    assert calls == ["first"]
    assert paused.value.resume["next_index"] == 2
    assert json.loads((tmp_path / "latest_report.json").read_text(encoding="utf-8"))["finished"] is False
    # 经过真实序列化再续跑，前面的积木不再执行。
    resume = json.loads(json.dumps(paused.value.resume))
    list(workflow.run_workflow("qa.json", plan, agent, resume=resume, defer_wait=True))
    assert calls == ["first", "second"]
    report = json.loads((tmp_path / "latest_report.json").read_text(encoding="utf-8"))
    assert report["finished"] and report["all_green"]
    assert len(report["steps"]) == 3


def state():
    return {"nodes": workflow.normalize_nodes([
        {"type": "wait_until", "params": {"time": "18:00"}},
        {"type": "signin", "params": {}}]), "next_index": 1,
        "report": [["等待", "等待至18:00"]], "game_closed": False, "forge_ran": True}


def test_raid_takeover_resumes_remaining_only_and_preserves_later_steps(tmp_path, monkeypatch):
    calls = []
    attempts = iter([6, 3, None])

    def raid(agent, params, config):
        calls.append(("raid", params["runs"]))
        agent._raid_takeover_remaining = next(attempts)
        yield "远征排班请求接管" if agent._raid_takeover_remaining else "全部圈数跑完"

    def step(agent, params, config):
        calls.append((params["name"], None))
        yield "✓"

    monkeypatch.setitem(workflow.NODE_REGISTRY, "raid", {"type": "raid", "label": "联队战", "run": raid})
    monkeypatch.setitem(workflow.NODE_REGISTRY, "qa_step", {"type": "qa_step", "label": "测试", "run": step})
    for kind in ("boot_emulator", "login"):
        monkeypatch.setitem(workflow.NODE_REGISTRY, kind, {"type": kind, "label": kind,
            "run": lambda *args: iter(["✓"])})
    monkeypatch.setattr(workflow.time, "time", lambda: 100)
    plan = workflow.normalize_nodes([
        {"type": "qa_step", "params": {"name": "before"}},
        {"type": "raid", "params": {"runs": 8}},
        {"type": "qa_step", "params": {"name": "after"}}])
    make = lambda _: SimpleNamespace(navigate_to_stream=lambda _: iter([]), current_location="本丸")
    resume = None
    for remaining in (6, 3):
        with pytest.raises(workflow.WorkflowPaused) as paused:
            list(workflow.run_workflow("qa", plan, make, resume=resume, defer_expedition=True))
        resume = json.loads(json.dumps(paused.value.resume))
        assert resume["remaining_runs"] == remaining
        assert resume["next_index"] == 1
        report = json.loads((tmp_path / "latest_report.json").read_text(encoding="utf-8"))
        assert not report["finished"] and not report["all_green"]
        assert calls[-1][0] == "raid"
    list(workflow.run_workflow("qa", plan, make, resume=resume, defer_expedition=True))
    assert calls == [("before", None), ("raid", 8), ("raid", 6), ("raid", 3), ("after", None)]


@pytest.mark.parametrize("remaining", [0, 9, True, "3"])
def test_invalid_raid_checkpoint_never_connects(remaining):
    plan = workflow.normalize_nodes([{"type": "raid", "params": {"runs": 8}}])
    resume = {"nodes": plan, "next_index": 0, "report": [], "reason": "expedition", "remaining_runs": remaining}
    make = Mock()
    with pytest.raises(workflow.WorkflowError):
        list(workflow.run_workflow("qa", plan, make, resume=resume, defer_expedition=True))
    make.assert_not_called()


def test_raid_resume_waits_for_expedition_preview_and_stops_at_deadline(tmp_path, monkeypatch):
    from panel import scheduler
    monkeypatch.setattr(scheduler, "takeover_flag_path", lambda: tmp_path / "flag.json")
    slots = {"one": {"state": "ready", "expires_at": 200}}
    monkeypatch.setattr(scheduler, "load_config", lambda: {"automation": {"slot_states": slots}})
    resume = {**state(), "reason": "expedition", "remaining_runs": 3}
    params = {"scheduled_deadline": 150}
    waits.park("root", "qa", params, 100, resume, "联队战")
    runner = SimpleNamespace(is_running=False, start=Mock(return_value="new"))
    waits.resume_due(110, runner)
    runner.start.assert_not_called()
    slots.clear()
    (tmp_path / "flag.json").write_text(json.dumps({"requested_at": 100}))
    waits.resume_due(110, runner)
    runner.start.assert_not_called()
    # 派遣完成清旗后仅续跑一次。
    (tmp_path / "flag.json").write_text("{}")
    waits.resume_due(110, runner)
    runner.start.assert_called_once()
    waits.resume_due(115, runner)
    runner.start.assert_called_once()
    waits.park("expired", "qa", params, 100, resume, "联队战")
    waits.resume_due(150, runner)
    assert waits.load()["expired"]["status"] == "interrupted"
    runner.start.assert_called_once()


@pytest.mark.parametrize("script", ["workflow", "scheduled_gameplay"])
def test_worker_pause_message_is_saved_without_exposing_checkpoint(monkeypatch, script):
    from panel import script_runner
    from touken import telemetry
    store, telemetry_store = Mock(), Mock()
    telemetry_store.run_summary.return_value = None
    monkeypatch.setattr(script_runner, "get_store", lambda: store)
    monkeypatch.setattr(telemetry, "get_telemetry_store", lambda: telemetry_store)
    payload = {"config_path": "PRIVATE_PATH", "params": {}, "wake_at": 100, "resume": state()}
    process = SimpleNamespace(stdout=["@@MAAMARU_WORKFLOW_WAIT@@" + json.dumps(payload)], wait=lambda: 44)
    runner = script_runner.ScriptRunner()
    runner._proc = process
    runner._pump(process, "root", script, "晚班")
    assert runner.last_run_result == ("root", "waiting")
    assert waits.load()["root"]["status"] == "waiting"
    assert waits.load()["root"]["script"] == script
    assert runner._proc is None
    assert "PRIVATE_PATH" not in str(store.append.call_args_list)
    telemetry_store.finish_run.assert_called_once_with("root", "waiting")


def test_single_gameplay_checkpoint_resumes_same_script():
    waits.park("root", "qa", {}, 100, state(), "联队战", script="scheduled_gameplay")
    runner = SimpleNamespace(is_running=False, start=Mock(return_value="child"))
    waits.resume_due(110, runner)
    assert runner.start.call_args.args[0] == "scheduled_gameplay"


def test_missing_checkpoint_is_failed_instead_of_waiting(monkeypatch):
    from panel import script_runner
    from touken import telemetry
    monkeypatch.setattr(script_runner, "get_store", lambda: Mock())
    monkeypatch.setattr(telemetry, "get_telemetry_store", lambda: Mock())
    process = SimpleNamespace(stdout=[], wait=lambda: 44)
    runner = script_runner.ScriptRunner()
    runner._proc = process
    runner._pump(process, "root", "workflow")
    assert runner.last_run_result == ("root", "failed")


def test_wait_survives_restart_backs_up_and_resumes_once(monkeypatch):
    waits.park("root", "qa.json", {"workflow_id": "wf"}, 100, state(), "晚班")
    original = waits.PATH.read_bytes()
    waits.recover()
    assert waits.load()["root"]["status"] == "waiting"
    runner = SimpleNamespace(is_running=True, start=Mock())
    waits.resume_due(110, runner)
    runner.start.assert_not_called()
    runner.is_running = False
    waits.resume_due(99, runner)
    runner.start.assert_not_called()
    def start(script, config, params):
        waits.segment_started(params["workflow_wait_root"], "child")
        return "child"
    runner.start.side_effect = start
    waits.resume_due(110, runner)
    assert runner.start.call_count == 1
    assert waits.load()["root"]["run_id"] == "child"
    assert waits.PATH.with_suffix(".json.bak").exists()
    waits.resume_due(120, runner)
    assert runner.start.call_count == 1
    waits.finish("child", "completed")
    assert waits.load()["root"]["status"] == "completed"
    # 原始安全等待记录的备份可以独立恢复读取。
    restored = waits.PATH.parent / "restored.json"
    restored.write_bytes(original)
    assert waits.load(restored)["root"]["status"] == "waiting"


def test_restart_does_not_replay_in_progress_steps():
    waits.park("root", "qa.json", {}, 100, state(), "晚班")
    runs = waits.load()
    runs["root"]["status"] = "launching"
    waits._save(runs)
    waits.recover()
    runner = SimpleNamespace(is_running=False, start=Mock())
    waits.resume_due(200, runner)
    runner.start.assert_not_called()
    assert waits.load()["root"]["status"] == "interrupted"


def test_cancel_cannot_be_revived_by_late_pause_message():
    waits.cancel("root", remember=True)
    waits.park("root", "qa.json", {}, 100, state(), "晚班")
    assert waits.load()["root"]["status"] == "cancelled"
    runner = SimpleNamespace(is_running=False, start=Mock())
    waits.resume_due(200, runner)
    runner.start.assert_not_called()


def test_second_wait_keeps_same_logical_flow():
    waits.park("root", "qa.json", {}, 100, state(), "晚班")
    waits.park("child", "qa.json", {"workflow_wait_root": "root"}, 200, state(), "晚班")
    waits.finish("child", "waiting")
    assert len(waits.load()) == 1
    assert waits.load()["root"]["wake_at"] == 200


def test_projection_does_not_invent_times_and_public_data_hides_params():
    start = datetime(2026, 10, 3, 10).timestamp()
    steps = waits.projection({"nodes": [
        {"type": "signin", "params": {}},
        {"type": "wait_until", "params": {"time": "18:00"}},
        {"type": "repair", "params": {}}]}, start)
    assert steps[0]["at"] == start
    assert steps[1]["wait_time"] == "18:00"
    assert steps[2]["at"] is None
    waits.park("root", "PRIVATE_PATH", {"private": "SECRET"}, start, state(), "晚班")
    public = json.dumps(waits.public_records())
    assert "PRIVATE_PATH" not in public and "SECRET" not in public


def test_modified_preset_blocks_resume_before_touching_game(monkeypatch):
    preset = {"id": "wf", "nodes": [{"type": "signin", "params": {}}]}
    monkeypatch.setattr(workflow, "find_preset", lambda _: preset)
    agent = Mock(side_effect=AssertionError("不准碰游戏"))
    monkeypatch.setattr(server, "_make_agent", agent)
    from touken.flow_control import FlowAborted
    with pytest.raises(FlowAborted):
        list(server._build_workflow("qa.json", {"workflow_id": "wf", "workflow_resume": state(), "workflow_preset_signature": "old"}))
    agent.assert_not_called()


def test_deleted_preset_does_not_mark_continuation_complete(monkeypatch):
    monkeypatch.setattr(workflow, "find_preset", lambda _: None)
    from touken.flow_control import FlowAborted
    with pytest.raises(FlowAborted, match="已删除"):
        list(server._build_workflow("qa.json", {"workflow_id": "wf", "workflow_resume": state()}))


def test_conductor_does_not_treat_wait_as_finished_and_other_block_can_start(tmp_path, monkeypatch):
    from panel.day_plan import save_plan
    now = 2_000_000_000
    plan_path, conductor_path = tmp_path / "plan.json", tmp_path / "conductor.json"
    plan = save_plan(now, None, [{"kind": "workflow", "start_min": 1, "workflow_id": "wf"},
                               {"kind": "daily", "start_min": 10}], plan_path)
    first = {**plan["blocks"][0], "status": "running", "run_id": "root"}
    second = {**plan["blocks"][1], "status": "pending"}
    dc._save({"version": 2, "enabled": True, "day_start": now,
              "plan_signature": dc._plan_signature(plan), "blocks": [first, second]}, conductor_path)
    waits.park("root", "qa.json", {}, now + 3600, state(), "晚班")
    runner = SimpleNamespace(is_running=False, last_run_result=("root", "waiting"), start=Mock(return_value="daily-run"))
    dc.tick(now + 10 * 60, runner, lambda: {}, lambda: {}, "qa.json", lambda *a: None, conductor_path, plan_path)
    assert runner.start.call_args.args[0] == "daily"
    assert dc.load_state(conductor_path)["blocks"][0]["status"] == "running"
    assert dc.load_state(conductor_path)["enabled"]


def test_daily_pauses_between_steps_then_resumes_two_sorties_without_repeating_rewards(tmp_path, monkeypatch):
    calls = []
    takeover = {'active': False}
    attempts = {'yosari': 0, 'raid': 0}
    def before(agent, params, config):
        calls.append('signin')
        takeover['active'] = True
        yield '已签到'
    def battle(kind):
        def run(agent, params, config):
            calls.append((kind, params['runs']))
            attempts[kind] += 1
            if attempts[kind] == 1:
                agent._expedition_takeover_remaining = params['runs'] - 1
                yield '远征排班请求接管'
            else:
                yield '目标全部完成'
        return run
    def after(agent, params, config):
        calls.append('rewards')
        yield '已领取'
    for kind, run in [('qa_before', before), ('yosari', battle('yosari')), ('raid', battle('raid')), ('qa_after', after)]:
        monkeypatch.setitem(workflow.NODE_REGISTRY, kind, {'type': kind, 'label': kind, 'run': run})
    for kind in ['boot_emulator', 'login']:
        monkeypatch.setitem(workflow.NODE_REGISTRY, kind, {'type': kind, 'label': kind, 'run': lambda *args: iter(['✓'])})
    plan = workflow.normalize_nodes([{'type':'qa_before'}, {'type':'yosari','params':{'runs':3}},
                                    {'type':'raid','params':{'runs':20}}, {'type':'qa_after'}])
    make = lambda _: SimpleNamespace(current_location='本丸', navigate_to_stream=lambda _:iter(()),
        _expedition_takeover_requested=lambda: takeover['active'])
    resume = None
    for checkpoint in ['before_node', 'remaining', 'remaining']:
        with pytest.raises(workflow.WorkflowPaused) as paused:
            list(workflow.run_workflow('qa', plan, make, resume=resume, defer_expedition=True))
        resume = json.loads(json.dumps(paused.value.resume))
        assert resume['checkpoint'] == checkpoint
        takeover['active'] = False
        report = json.loads((tmp_path/'latest_report.json').read_text(encoding='utf-8'))
        assert not report['finished'] and not report['all_green']
        assert 'rewards' not in calls
    list(workflow.run_workflow('qa', plan, make, resume=resume, defer_expedition=True))
    assert calls == ['signin', ('yosari',3), ('yosari',2), ('raid',20), ('raid',19), 'rewards']


def test_untracked_takeover_cannot_turn_green_or_execute_later_actions(tmp_path, monkeypatch):
    def interrupted(agent, params, config):
        yield '远征排班请求接管'
    monkeypatch.setitem(workflow.NODE_REGISTRY, 'qa_interrupted', {'type':'qa_interrupted','label':'未完成','run':interrupted})
    later = Mock(return_value=iter(['✓']))
    monkeypatch.setitem(workflow.NODE_REGISTRY, 'qa_later', {'type':'qa_later','label':'后续','run':later})
    monkeypatch.setitem(workflow.NODE_REGISTRY, 'logout', {'type':'logout','label':'退出','run':later})
    make = lambda _: SimpleNamespace(current_location='本丸', navigate_to_stream=lambda _:iter(()))
    list(workflow.run_workflow('qa', [{'type':'qa_interrupted','on_error':'continue'}, {'type':'qa_later'}], make, after='logout'))
    later.assert_not_called()
    report = json.loads((tmp_path/'latest_report.json').read_text(encoding='utf-8'))
    assert not report['all_green']
    assert report['steps'][0]['status'].startswith('✗')


def test_failed_expedition_interrupts_waiting_daily_instead_of_resuming(tmp_path, monkeypatch):
    from panel import scheduler
    slots = {'one': {'state':'waiting_busy','expires_at':200}}
    monkeypatch.setattr(waits.time,'time',lambda:100)
    monkeypatch.setattr(scheduler,'load_config',lambda:{'automation':{'slot_states':slots}})
    waits.park('root','qa',{},100,{**state(),'reason':'expedition','remaining_runs':3},'日课')
    slots['one']['state']='failed_unknown'
    runner = SimpleNamespace(is_running=False,start=Mock())
    waits.resume_due(110,runner)
    runner.start.assert_not_called()
    assert waits.load()['root']['status']=='interrupted'


def test_pending_expedition_defers_ending_without_repeating_finished_steps(monkeypatch):
    flag = {'active':False}
    calls = []
    def step(agent, params, config):
        calls.append('step')
        flag['active'] = True
        yield '✓'
    def end(agent, params, config):
        calls.append('logout')
        yield '✓'
    for kind, run in [('qa_done',step),('logout',end),('boot_emulator',lambda *args:iter(['✓'])),('login',lambda *args:iter(['✓']))]:
        monkeypatch.setitem(workflow.NODE_REGISTRY,kind,{'type':kind,'label':kind,'run':run})
    make = lambda _:SimpleNamespace(current_location='本丸',navigate_to_stream=lambda _:iter(()),
        _expedition_takeover_requested=lambda:flag['active'])
    plan = workflow.normalize_nodes([{'type':'qa_done'}])
    with pytest.raises(workflow.WorkflowPaused) as paused:
        list(workflow.run_workflow('qa',plan,make,after='logout',defer_expedition=True))
    assert calls == ['step']
    assert paused.value.resume['checkpoint'] == 'after_steps'
    flag['active'] = False
    list(workflow.run_workflow('qa',plan,make,after='logout',resume=paused.value.resume,defer_expedition=True))
    assert calls == ['step','logout']


@pytest.mark.parametrize('kind,params,remaining,key', [
    ('yosari', {'runs':3}, 2, 'runs'),
    ('sortie', {'runs':3}, 2, 'runs'),
    ('edocastle', {'runs':3}, 2, 'runs'),
    ('hanafuda', {'runs':3}, 2, 'runs'),
    ('osaka', {'runs':3,'select_floor':True}, 2, 'runs'),
    ('pumpkin', {'runs':3}, 0, 'runs'),
    ('daily_sortie', {'sortie_mode':'yosari','yosari_runs':3}, 2, 'yosari_runs'),
])
def test_counted_gameplay_resume_uses_only_remaining_budget(kind, params, remaining, key, monkeypatch):
    calls = []
    def run(agent, actual, config):
        calls.append(actual.copy())
        if len(calls) == 1:
            agent._expedition_takeover_remaining = remaining
            yield '远征排班请求接管'
        else:
            yield '目标完成'
    monkeypatch.setitem(workflow.NODE_REGISTRY, kind, {'type':kind,'label':kind,'run':run})
    for entry in ['boot_emulator','login']:
        monkeypatch.setitem(workflow.NODE_REGISTRY,entry,{'type':entry,'label':entry,'run':lambda *args:iter(['✓'])})
    make=lambda _:SimpleNamespace(current_location='本丸',navigate_to_stream=lambda _:iter(()))
    plan=workflow.normalize_nodes([{'type':kind,'params':params}])
    with pytest.raises(workflow.WorkflowPaused) as paused:
        list(workflow.run_workflow('qa',plan,make,defer_expedition=True))
    list(workflow.run_workflow('qa',plan,make,resume=paused.value.resume,defer_expedition=True))
    assert calls[1][key] == remaining
    if kind == 'osaka': assert calls[1]['select_floor'] is False
