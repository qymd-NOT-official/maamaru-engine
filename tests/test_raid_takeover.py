from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from touken.flows.raid import RaidMixin


@pytest.mark.parametrize("completed", [0, 2, 8])
def test_takeover_counts_only_confirmed_rounds(completed, monkeypatch):
    from touken.flows import raid
    monkeypatch.setattr(raid.time, "sleep", lambda _: None)
    agent = RaidMixin()
    agent.config = {"raid": {"ui_title": {"template": "title"},
        "difficulty_target": [1, 1]}, "team_select": {"teams": {"3": {}}}}
    agent.maa = Mock()
    agent.maa.template_match.return_value = (1, 1)
    agent.current_location = "出阵"
    agent.navigate_to_stream = lambda _: iter(())
    agent.set_progress = Mock()
    rounds = []
    agent._expedition_takeover_requested = lambda: len(rounds) >= completed
    agent._read_shells_total = lambda _: None
    agent._click_point = Mock()
    agent._find_deploy_button = lambda _: (1, 1)
    agent._wait_for_team_select = lambda *args, **kwargs: True
    def depart(*args, **kwargs):
        yield "safe departure"
        return True, None
    agent._safe_depart_stream = depart
    agent._confirm_departure = lambda _: True
    def battle():
        rounds.append(True)
        agent._battle_loop_result = (True, 10)
        yield "round complete"
    agent.battle_loop_stream = battle
    messages = list(agent.raid_stream(max_rounds=8, use_triple=False))
    assert len(rounds) == completed
    assert agent._raid_takeover_remaining == (8 - completed if completed < 8 else None)
    assert any("请求接管" in message for message in messages) == (completed < 8)
