"""收件箱体检流：只读扫描的识别与退出，绝不碰领取按钮。"""
from unittest.mock import Mock

import pytest

from touken import game_state
from touken.device_adapter import Point
from touken.flows.inbox_scan import InboxScanMixin


class ScanFlow(InboxScanMixin):
    def __init__(self, location="收件箱"):
        self.current_location = location
        self.device = Mock()

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
def flow(monkeypatch):
    monkeypatch.setattr("touken.flows.inbox_scan.time.sleep", lambda _: None)
    monkeypatch.setattr(game_state, "begin_operation", lambda _: {})
    return ScanFlow()


def fake_list(monkeypatch, body, ok=True):
    def confirm(device, boundary, operation, **kwargs):
        if operation == "inbox_list":
            boundary.update(result_filter="", result_body=body)
            return ok
        return None
    monkeypatch.setattr(game_state, "confirm_operation", confirm)


def mail(**overrides):
    base = {"serial_id": "100", "item_type": "2", "item_id": "228",
            "item_num": "1", "created_at": "2026-11-01 10:00:00"}
    base.update(overrides)
    return base


def clicked_points(flow):
    return [call.args[0] for call in flow.device.click.call_args_list]


def test_scan_returns_parsed_mails_without_touching_claim(monkeypatch, flow):
    fake_list(monkeypatch, {"receive": {"100": mail(), "101": mail(
        serial_id="101", item_type="1", item_id="51", item_num="5000",
        name="小判", reason="任务奖励")}})
    result, messages = collect(flow.inbox_scan_stream())
    assert [m["mailbox_id"] for m in result] == ["100", "101"]
    assert result[0]["inbox_at"] == "2026-11-01 10:00:00"
    assert result[1]["name"] == "小判"
    assert any("2 封" in m for m in messages)
    # 只读铁律：不做模板匹配（找领取按钮才需要），全程没碰领取
    flow.device.template_match.assert_not_called()
    flow.device.ocr.assert_not_called()
    # 筛选复位三件套 + 最后收箱子
    points = clicked_points(flow)
    assert points[:3] == [Point(990, 126), Point(803, 157), Point(640, 625)]
    assert points[-1] == Point(1248, 32)


def test_scan_accepts_list_shaped_receive_and_skips_bad_rows(monkeypatch, flow):
    fake_list(monkeypatch, {"receive": [mail(), "垃圾", {"no_serial": 1}]})
    result, _ = collect(flow.inbox_scan_stream())
    assert [m["mailbox_id"] for m in result] == ["100"]


def test_unconfirmed_list_returns_none_and_closes_inbox(monkeypatch, flow):
    fake_list(monkeypatch, {}, ok=False)
    result, messages = collect(flow.inbox_scan_stream())
    assert result is None
    assert any("没能确认邮件清单" in m for m in messages)
    flow.device.template_match.assert_not_called()
    assert clicked_points(flow)[-1] == Point(1248, 32)


def test_navigation_failure_touches_nothing(monkeypatch):
    monkeypatch.setattr("touken.flows.inbox_scan.time.sleep", lambda _: None)
    flow = ScanFlow(location="本丸")
    result, messages = collect(flow.inbox_scan_stream())
    assert result is None
    assert any("到达收件箱失败" in m for m in messages)
    flow.device.click.assert_not_called()
