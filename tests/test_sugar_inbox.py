from unittest.mock import Mock

import pytest

from touken import game_state
from touken.flow_control import FlowAborted
from touken.flows.sugar import SugarMixin
from touken.device_adapter import Point


class InboxFlow(SugarMixin):
    def __init__(self):
        self.current_location = "收件箱"
        self.device = Mock()
        self.device.template_match.return_value = Point(1170, 622)
        self.device.ocr.return_value = False
        self._apply_inbox_filter = Mock()

    def navigate_to_stream(self, target):
        yield from ()


def collect(stream):
    messages = []
    while True:
        try:
            messages.append(next(stream))
        except StopIteration as result:
            return result.value, messages


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr("touken.flows.sugar.time.sleep", lambda _: None)
    monkeypatch.setattr(game_state, "begin_operation", lambda _: {})

    def configure(entries, *, selected="2", claimed=None):
        def confirm(device, boundary, operation, **kwargs):
            if operation == "inbox_list":
                boundary.update(result_filter=selected, result_body={"receive": entries, "receive_total": 46})
                return True
            return claimed
        monkeypatch.setattr(game_state, "confirm_operation", confirm)
        return InboxFlow()

    return configure


@pytest.mark.parametrize("entries", [[], {}])
def test_filtered_empty_does_not_click_false_positive_claim_button(setup, entries):
    flow = setup(entries)
    result, _ = collect(flow._inbox_claim_stream(False))
    assert result == "empty"
    flow.device.template_match.assert_not_called()
    assert [call.args[0] for call in flow.device.click.call_args_list] == [Point(1248, 32)]


def test_empty_mailbox_and_no_fodder_finish_after_one_cycle(setup):
    flow = setup([])
    def feed(*args, **kwargs):
        yield from ()
        return 0
    flow._shugo_loop_stream = feed
    messages = list(flow.sugar_stream())
    assert messages[-1] == "[炼糖] 邮件里没刀了，收工"
    assert sum("===== 第" in message for message in messages) == 1


def test_click_without_receipt_is_not_claimed_and_not_repeated(setup):
    flow = setup({"mail": {"item_type": "2"}})
    with pytest.raises(FlowAborted, match="未确认领到邮件"):
        list(flow._inbox_claim_stream(False))
    assert flow.device.click.call_count == 1


def test_nonempty_mail_with_no_claim_button_is_blocked_not_empty(setup):
    flow = setup({"mail": {"item_type": "2"}})
    flow.device.template_match.return_value = None
    assert collect(flow._inbox_claim_stream(False))[0] == "blocked"


def test_receipt_or_reward_window_confirms_claim(setup):
    flow = setup({"mail": {}}, claimed=True)
    assert collect(flow._inbox_claim_stream(False))[0] == "claimed"
    flow = setup({"mail": {}})
    flow.device.ocr.side_effect = lambda text, *args: text == "奖励确认"
    assert collect(flow._inbox_claim_stream(False))[0] == "claimed"


def test_wrong_filter_cannot_prove_sword_mailbox_empty(setup):
    flow = setup([], selected="4")
    flow.device.template_match.return_value = None
    with pytest.raises(FlowAborted, match="未确认邮件为空"):
        list(flow._inbox_claim_stream(False))


def test_inbox_receipts_use_fresh_requests_and_actual_received_ids():
    def event(direction, endpoint, payload, stamp):
        return {"direction": direction, "endpoint": endpoint, "payload": payload,
                "status": 200, "ts": stamp}
    endpoint = "/receive/list"
    old = event("C->S", endpoint, {"item_type": "2"}, "2026-10-16 15:49:00")
    current = event("C->S", endpoint, {"item_type": "2"}, "2026-10-16 15:50:00")
    response = event("S->C", endpoint, {"status": 0, "receive": [], "receive_total": 46}, "2026-10-16 15:50:03")
    boundary = {"after": 9999999999, "request_snapshot": True, "requests": {game_state._request_key(old)}}
    assert game_state.operation_result([old, response], boundary, "inbox_list") is None
    assert game_state.operation_result([current, response], boundary, "inbox_list") is True
    assert boundary["result_body"]["receive"] == []
    assert boundary["result_filter"] == "2"
    request = event("C->S", "/receive/get", {}, current["ts"])
    for ids, expected in [([], False), (["mail"], True), (None, None)]:
        receipt = event("S->C", "/receive/get", {"status": 0, "serial_ids": ids}, response["ts"])
        assert game_state.operation_result([request, receipt], boundary, "inbox_claim") is expected
