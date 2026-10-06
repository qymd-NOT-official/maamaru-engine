from unittest.mock import Mock, patch

import pytest

from panel import server
from touken.flow_control import FlowAborted


@pytest.mark.parametrize("failed_step", [None, "boot_emulator", "login"])
def test_scheduled_dispatch_prepares_game_before_dispatch(failed_step):
    calls = []
    agent = Mock()
    agent.navigate_to_stream.return_value = iter(())

    def run_node(node, current_agent, params, config_path):
        calls.append(node["type"])
        if node["type"] == "boot_emulator":
            assert current_agent is None
        else:
            assert current_agent is agent
        yield "startup"
        return node["type"] != failed_step, "result"

    def make_agent(config_path):
        calls.append("connect")
        return agent

    def dispatch(*args):
        calls.append("dispatch")
        yield "dispatched"

    with patch.object(server._workflow, "_run_node", side_effect=run_node), \
         patch.object(server, "_make_agent", side_effect=make_agent), \
         patch.object(server, "_write_dispatch_result") as result:
        run = server._wrap_inventory("派遣", dispatch, scheduled_startup=True)
        if failed_step:
            with pytest.raises(FlowAborted):
                list(run("config", {"scheduled": True, "slot_key": "slot"}))
            assert "dispatch" not in calls
            result.assert_called_once()
            assert result.call_args.args[:2] == ("slot", "failed")
        else:
            list(run("config", {"scheduled": True, "slot_key": "slot"}))
            assert calls == ["boot_emulator", "connect", "login", "dispatch"]
            result.assert_not_called()


def test_manual_dispatch_keeps_existing_behavior():
    agent = Mock()
    agent.navigate_to_stream.return_value = iter(())
    dispatch = Mock(return_value=iter(()))
    with patch.object(server._workflow, "_run_node") as startup, \
         patch.object(server, "_make_agent", return_value=agent):
        list(server._wrap_inventory("派遣", dispatch, scheduled_startup=True)("config", {}))
        startup.assert_not_called()
        dispatch.assert_called_once_with(agent, "config", {})
