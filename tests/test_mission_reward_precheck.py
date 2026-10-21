"""任务奖励 JSON 预检：/mission/index 有新鲜数据就不点空 tab，缺数据照旧走模板。"""
import time

import pytest

from touken import game_state as gs
from touken.flows import rewards as module
from touken.device_adapter import Point

NOW = 1_000_000.0


def mission_index_event(missions, *, at=None, direction="S->C", http=200):
    if at is None:
        at = time.time()
    return {"endpoint": "/mission/index", "direction": direction,
            "status": http,
            "payload": {"status": 0, "now_time": at, "mission": missions},
            "ts": "2026-10-12 12:00:00"}


class TestPrecheckFacts:
    def test_true_when_any_mission_awaiting_claim(self):
        events = [mission_index_event(
            {"1": {"status": "3"}, "487": {"status": "2"}}, at=NOW)]
        assert module._mission_reward_claimable(events, now=NOW) is True

    def test_int_status_accepted(self):
        # 真实报文里 status 的 int/str 混着来，两种都得认
        events = [mission_index_event({"2": {"status": 2}}, at=NOW)]
        assert module._mission_reward_claimable(events, now=NOW) is True

    def test_false_when_nothing_awaiting(self):
        events = [mission_index_event(
            {"1": {"status": "3"}, "2": {"status": "1"}}, at=NOW)]
        assert module._mission_reward_claimable(events, now=NOW) is False

    def test_none_when_index_missing_stale_or_request_only(self):
        assert module._mission_reward_claimable([], now=NOW) is None
        stale = [mission_index_event({"1": {"status": "2"}},
                                     at=NOW - gs.FACT_MAX_AGE - 1)]
        assert module._mission_reward_claimable(stale, now=NOW) is None
        request_only = [mission_index_event({}, at=NOW, direction="C->S")]
        assert module._mission_reward_claimable(request_only, now=NOW) is None

    def test_boundary_age_is_still_fresh(self):
        edge = [mission_index_event({"1": {"status": "2"}},
                                    at=NOW - gs.FACT_MAX_AGE)]
        assert module._mission_reward_claimable(edge, now=NOW) is True

    def test_latest_index_wins_and_bad_payload_is_none(self):
        older = mission_index_event({"1": {"status": "2"}}, at=NOW - 30)
        latest_claimed = mission_index_event({"1": {"status": "3"}}, at=NOW)
        assert module._mission_reward_claimable(
            [older, latest_claimed], now=NOW) is False
        bad = [mission_index_event([], at=NOW)]  # mission 不是 dict，坏包
        assert module._mission_reward_claimable(bad, now=NOW) is None


class _FakeMaa:
    """蓝/灰按钮永远命中的瞎点制造机：JSON 说没有可领时绝不允许出手。"""

    def __init__(self):
        self.template_calls = 0
        self.clicks = []

    def screenshot(self, force=False):
        return None

    def template_match(self, template, roi=None, threshold=0.8):
        self.template_calls += 1
        return Point(1130, 480)

    def exists(self, template, roi=None, threshold=0.8):
        return False

    def ocr(self, expected, roi, match_mode="exact"):
        return None

    def ocr_all(self, roi):
        return []

    def click(self, pt):
        self.clicks.append(pt)
        return True


class _Host(module.RewardsMixin):
    """claim_task_rewards_stream 最小宿主（仿 test_flow_resource_changes）"""

    def __init__(self, device, tabs=("日常",)):
        self.device = device
        self.current_location = "任务"
        self.config = {"task_reward": {
            "tabs": {name: [48, 204] for name in tabs},
            "claim_button": {"template": "一键领取.png"}}}
        self.events = []
        self.tab_clicks = []

    def navigate_to_stream(self, dest):
        self.current_location = dest
        yield f"nav {dest}"

    def _click_point(self, coord):
        self.tab_clicks.append(coord)

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        return len(self.events)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)


def test_facts_none_empty_tab_is_never_clicked(no_sleep, monkeypatch):
    fresh_none = [mission_index_event({"1": {"status": "3"}})]
    monkeypatch.setattr(module, "available_events", lambda device: fresh_none)
    device = _FakeMaa()
    host = _Host(device)

    messages = list(host.claim_task_rewards_stream())

    # 预检拦截在一切点击之前：tab 不切换、按钮模板不看、手不出
    assert device.clicks == [] and device.template_calls == 0
    assert host.tab_clicks == []
    assert host.events == [("task_rewards.none", {"tab": "日常"})]
    assert any("数据确认没有可领奖励" in m for m in messages)


@pytest.mark.parametrize("mode", ["missing", "stale"])
def test_facts_unusable_falls_back_to_template(no_sleep, monkeypatch, mode):
    if mode == "missing":
        monkeypatch.setattr(module, "available_events", lambda device: [])
    else:
        stale = [mission_index_event({"1": {"status": "2"}},
                                     at=time.time() - gs.FACT_MAX_AGE - 1)]
        monkeypatch.setattr(module, "available_events", lambda device: stale)
    device = _FakeMaa()
    host = _Host(device)

    messages = list(host.claim_task_rewards_stream())

    # 兜底 = 旧行为：模板说按钮在就点，游戏回执缺失就不计成绩，但不跳过
    assert device.clicks and device.template_calls > 0
    assert not any(t == "task_rewards.none" for t, _ in host.events)
    assert not any("数据确认没有可领奖励" in m for m in messages)


def test_facts_claimable_leaves_clicking_to_template_gate(no_sleep, monkeypatch):
    fresh = [mission_index_event({"1": {"status": "3"}, "487": {"status": "2"}})]
    monkeypatch.setattr(module, "available_events", lambda device: fresh)

    def confirm(device, boundary, kind):
        boundary["result_body"] = {"item": []}
        return True

    monkeypatch.setattr(module, "confirm_operation", confirm)
    device = _FakeMaa()
    host = _Host(device)

    messages = list(host.claim_task_rewards_stream())

    assert host.events[0][0] == "task_rewards.claimed"
    assert device.clicks  # 预检 True 不拦点击，按钮模板仍是每页的执行闸门
    assert any("游戏结果" in m for m in messages)


def test_precheck_refreshes_between_tabs(no_sleep, monkeypatch):
    reads = [
        [mission_index_event({"487": {"status": "2"}})],  # 日常：有可领
        [mission_index_event({"487": {"status": "3"}})],  # 月常：已领完
    ]
    monkeypatch.setattr(module, "available_events", lambda device: reads.pop(0))
    monkeypatch.setattr(module, "confirm_operation",
                        lambda device, boundary, kind: True)
    device = _FakeMaa()
    host = _Host(device, tabs=("日常", "月常"))

    list(host.claim_task_rewards_stream())

    assert [t for t, _ in host.events] == ["task_rewards.claimed",
                                           "task_rewards.none"]
    assert host.tab_clicks == [[48, 204]]  # 月常 tab 根本不用切换


def test_facts_none_skips_activity_tab_before_ocr_guard(no_sleep, monkeypatch):
    monkeypatch.setattr(module, "available_events",
                        lambda device: [mission_index_event({"1": {"status": "3"}})])
    device = _FakeMaa()
    host = _Host(device, tabs=("活动",))

    messages = list(host.claim_task_rewards_stream())

    # 无可领时活动 tab 连「标签是否存在」都不用问，更不会点旧坐标
    assert host.tab_clicks == [] and device.clicks == []
    assert host.events == [("task_rewards.none", {"tab": "活动"})]
    assert any("数据确认没有可领奖励" in m for m in messages)
    assert not any("当前没有活动任务标签" in m for m in messages)
