"""Recovery policy and progress contracts, with no game interaction."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from touken.flows.battle import BattleMixin
from touken.injury_recovery import RecoveryOutcome, RecoveryProgress, recover_injury_stream
from touken.flow_control import FlowAborted


def drain(stream):
    messages = []
    while True:
        try:
            messages.append(next(stream))
        except StopIteration as done:
            return messages, done.value


def host(repaired=0, speedups=0):
    agent = SimpleNamespace(device=SimpleNamespace(), repairs=[],
        _injury_reaches_threshold=BattleMixin._injury_reaches_threshold)

    def repair(**kwargs):
        agent.repairs.append(kwargs)
        agent.last_repair_stats = {"repaired": repaired, "speedups": speedups}
        yield "手入扫描结束"

    agent.repair_stream = repair
    return agent


def run(agent, observations, **kwargs):
    params = dict(team_no=4, injury="中伤", threshold="medium", action="continue",
                  progress=RecoveryProgress(10, 3), tag="[测试]")
    params.update(kwargs)
    with patch("touken.injury_recovery.current_team_injury", side_effect=observations):
        return drain(recover_injury_stream(agent, **params))


@pytest.mark.parametrize("action", ["stop", "repair_stop", "continue"])
def test_player_already_repaired_skips_old_injury_and_keeps_remaining(action):
    agent = host()
    messages, result = run(agent, [(True, None)], action=action)
    assert result.resume and result.remaining == 7
    assert not result.attempted and agent.repairs == []
    assert any("旧伤势记录已翻篇" in message for message in messages)


@pytest.mark.parametrize("repaired", [0, 1])
def test_current_health_not_repair_count_controls_resume(repaired):
    agent = host(repaired, 1)
    _, result = run(agent, [(True, "中伤"), (True, None)])
    assert result.resume and result.remaining == 7
    assert agent.repairs == [{"dry_run": False, "use_speedup": None, "speedup_teams": [4]}]
    assert result.repaired == repaired and result.speedups == 1


@pytest.mark.parametrize("after,reason", [((True, "重伤"), "still_injured"),
                                        ((False, None), "unknown")])
def test_zero_repaired_or_unknown_never_repeats_the_repair_cycle(after, reason):
    agent = host()
    _, result = run(agent, [(True, "中伤"), after])
    assert not result.resume and result.reason == reason
    assert len(agent.repairs) == 1 and result.remaining == 7


def test_even_positive_repair_count_does_not_prove_target_team_is_ready():
    agent = host(6)
    _, result = run(agent, [(True, "重伤"), (True, "重伤")], injury="重伤")
    assert not result.resume and result.repaired == 6


@pytest.mark.parametrize("action,heavy_stop,injury,repairs,speedup", [
    ("stop", False, "重伤", 0, None),  # Osaka keeps its original stop behavior.
    ("stop", True, "重伤", 1, False),  # Ordinary sortie slow-repairs then stops.
    ("stop", True, "中伤", 0, None),
    ("repair_stop", False, "中伤", 1, False),
    ("false", False, "中伤", 0, None),
])
def test_policy_preserves_existing_gameplay_meaning(action, heavy_stop, injury, repairs, speedup):
    agent = host(1)
    _, result = run(agent, [(True, injury)], action=action, injury=injury,
                    repair_heavy_on_stop=heavy_stop)
    assert not result.resume
    assert len(agent.repairs) == repairs
    if repairs:
        assert agent.repairs[0]["use_speedup"] is speedup
        assert agent.repairs[0]["speedup_teams"] is None


def test_finished_goal_never_arranges_extra_repair_or_departure():
    agent = host()
    _, result = run(agent, [], progress=RecoveryProgress(10, 10))
    assert result.reason == "complete" and result.remaining == 0
    assert agent.repairs == []


def test_invalid_progress_cannot_turn_into_extra_runs():
    with pytest.raises(ValueError):
        RecoveryProgress(10, 11)


@pytest.mark.parametrize("reason", ["still_injured", "unknown", "invalid_policy"])
@pytest.mark.parametrize("tag,unit", [("[出阵]", "圈"), ("[挖地]", "层")])
def test_failed_recovery_reports_remaining_work_as_aborted(reason, tag, unit):
    outcome = RecoveryOutcome(False, RecoveryProgress(3, 2).remaining, reason)
    with pytest.raises(FlowAborted, match=f"剩余 1 {unit}未完成，已安全停止"):
        outcome.raise_if_failed(tag, unit)


@pytest.mark.parametrize("reason", ["policy_stop", "repair_stop", "complete", "ready"])
def test_deliberate_policy_stop_keeps_original_result(reason):
    RecoveryOutcome(False, 1, reason).raise_if_failed("[出阵]")


def test_policy_stop_and_unconfirmed_recovery_have_distinct_report_results():
    from touken.flows.report_judge import _is_fail
    stopped, _ = run(host(), [(True, "中伤")], action="stop")
    assert not any(_is_fail(message) for message in stopped)
    unknown, _ = run(host(), [(True, "中伤"), (False, None)])
    assert any(_is_fail(message) for message in unknown)


def test_early_repair_exit_does_not_reuse_previous_session_statistics():
    agent = host()
    agent.last_repair_stats = {"repaired": 10, "speedups": 10}
    agent.repair_stream = lambda **kwargs: iter(())
    _, result = run(agent, [(True, "中伤"), (False, None)])
    assert not result.resume and result.repaired == result.speedups == 0


@pytest.mark.parametrize("point,completed", [("before", 0), ("floor", 1), ("march", 0)])
@pytest.mark.parametrize("still_injured", [False, True])
def test_osaka_all_injury_stops_share_recovery_and_preserve_progress(point, completed, still_injured):
    from test_osaka_flow import OsakaFlow
    flow = OsakaFlow([point == "floor"])
    flow._injury_reaches_threshold = BattleMixin._injury_reaches_threshold
    flow._team_injury_status = lambda cfg: "中伤"
    flow._return_home_from_march = lambda cfg: True
    if point == "march":
        flow.find_march_seq = [object()]
    if point == "before":
        def blocked(cfg, team_no, tag, **kwargs):
            kwargs["departure_state"]["blocked_injury"] = "中伤"
            yield from ()
            return False, False
        flow._safe_depart_stream = blocked
    resumed = []
    repairs = []

    def repair(**kwargs):
        repairs.append(kwargs)
        flow.last_repair_stats = {"repaired": 1, "speedups": 1}
        yield "手入完成"

    def resume(**kwargs):
        resumed.append(kwargs)
        yield "接续出阵"

    flow.repair_stream, flow.osaka_stream = repair, resume
    with patch("touken.flows.osaka.time.sleep"), patch(
            "touken.injury_recovery.current_team_injury",
            side_effect=[(True, "中伤"), (True, "重伤" if still_injured else None)]):
        if still_injured:
            with pytest.raises(FlowAborted, match=f"剩余 {3 - completed} 层未完成"):
                list(flow._osaka_stream_impl(max_floors=3))
        else:
            list(flow._osaka_stream_impl(max_floors=3))
    assert len([e for e in flow.events if e["event_type"] == "osaka.floor_completed"]) == completed
    if still_injured:
        assert len(repairs) == 1 and resumed == []
        return
    assert len(repairs) == len(resumed) == 1
    assert resumed[0]["max_floors"] == 3 - completed
    assert resumed[0]["_completed_floors"] == completed
    assert resumed[0]["_target_floors"] == 3
    assert resumed[0]["_repair_count"] == 1
    assert resumed[0]["_speedups_used"] == 1
