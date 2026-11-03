"""同一任务奖励循环可由独立画面适配驱动：无 device/config 的宿主跑公共循环。

验证拆分边界（对标 tests/test_repair_split.py）：数据预检拦截、活动标签
守卫、按钮三态、领取结果三路仲裁（游戏结果/按钮变灰/报酬弹窗）、
失效弹窗补点、未确认不计成绩都在公共层，画面与数据触点全是钩子。
"""
from unittest.mock import patch

from touken.flows.task_rewards_loop import TaskRewardsLoopMixin


class TaskRewardsHost(TaskRewardsLoopMixin):
    """没有 device/config/模板像素的宿主，验证循环不会暗中借用国服画面。"""

    def __init__(self, cfg=None, goto_ok=True, claimable=None,
                 tab_present=True, button_state=(None, None),
                 confirms=(), popups=(), stale=False, retry_point=None,
                 wait_confirmation=(False, False)):
        self.cfg = {"tabs": {"日常": [48, 204]},
                    "claim_button": {"template": "一键领取.png"},
                    "popup_close": {"template": "通用_关闭.png"}} \
            if cfg is None else cfg
        self.goto_ok = goto_ok
        self.claimable = claimable
        self.tab_present = tab_present
        self.button_state = button_state
        self.confirms = list(confirms)
        self.popups = list(popups)
        self.stale = stale
        self.retry_point = retry_point
        self.wait_confirmation = wait_confirmation
        self.tab_clicks = []
        self.claim_clicks = []
        self.claimed_records = []
        self.events = []

    # ---- 钩子 ----

    def _task_reward_config(self):
        return self.cfg

    def _task_reward_goto(self):
        yield "[TASK] 导航中"
        return self.goto_ok

    def _task_reward_claimable(self):
        return self.claimable

    def _task_reward_tab_present(self, tab_name, tab_coord):
        return self.tab_present

    def _task_reward_click_tab(self, tab_coord):
        self.tab_clicks.append(tab_coord)

    def _task_reward_claim_button_state(self, task_config):
        return self.button_state

    def _task_reward_click_claim(self, task_config, point):
        self.claim_clicks.append(point)
        return {"boundary": len(self.claim_clicks)}

    def _task_reward_confirm(self, boundary):
        return self.confirms.pop(0) if self.confirms else None

    def _read_popup_guarded(self, tab_name):
        if self.popups:
            popup = self.popups.pop(0)
            if popup is not None:
                return popup
        return None, []

    def _close_reward_popup(self, close_config):
        return False, []

    def _task_reward_dismiss_stale(self):
        return self.stale

    def _task_reward_wait_claim_button(self, task_config):
        return self.retry_point

    def _task_reward_wait_confirmation(self, task_config, popup_seen):
        return self.wait_confirmation

    def _task_reward_record_claimed(self, tab_name, outcome, popup, boundary):
        self.claimed_records.append(
            {"tab": tab_name, "outcome": outcome, "popup": popup,
             "boundary": boundary})

    def _task_reward_close_list(self, task_config):
        return ["[TASK] 已关闭任务列表"]

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        return len(self.events)


def run(host, **kwargs):
    with patch("time.sleep"):
        return list(host.claim_task_rewards_stream(**kwargs))


def test_unconfigured_stops():
    messages = run(TaskRewardsHost(cfg={}))
    assert messages == ["[TASK] 未配置任务领取"]


def test_goto_failure_stops():
    messages = run(TaskRewardsHost(goto_ok=False))
    assert messages[-1] == "[TASK] 到达任务失败"


def test_precheck_false_skips_before_any_click():
    host = TaskRewardsHost(claimable=False,
                           button_state=("active", "point"))
    messages = run(host)
    assert any("数据确认没有可领奖励，跳过" in m for m in messages)
    assert ("task_rewards.none", {"tab": "日常"}) in host.events
    assert host.tab_clicks == [] and host.claim_clicks == []


def test_missing_activity_tab_is_not_clicked():
    host = TaskRewardsHost(tab_present=False)
    host.cfg["tabs"] = {"活动": [52, 615]}
    messages = run(host)
    assert any("当前没有活动任务标签，跳过（不领取主线）" in m for m in messages)
    assert host.tab_clicks == [] and host.events == []


def test_inactive_button_records_none():
    host = TaskRewardsHost(button_state=("inactive", None))
    messages = run(host)
    assert any("已确认没有可领奖励，跳过" in m for m in messages)
    assert ("task_rewards.none", {"tab": "日常"}) in host.events
    assert host.claim_clicks == []


def test_unknown_button_state_neither_clicked_nor_counted():
    host = TaskRewardsHost(button_state=(None, None))
    messages = run(host)
    assert any("未确认一键领取按钮状态，本次不点击也不计成绩" in m for m in messages)
    assert ("task_rewards.unconfirmed",
            {"tab": "日常", "stage": "before_click"}) in host.events
    assert host.claim_clicks == []


def test_game_result_true_claims_with_client_evidence():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[True])
    messages = run(host)
    assert host.claim_clicks == ["point"]
    assert host.claimed_records[0]["outcome"] is True
    assert any("领取成功，已确认游戏结果" in m for m in messages)
    # 游戏结果已确认：不读弹窗、不等按钮变灰
    assert any("所有任务奖励领取完毕" in m for m in messages)


def test_gray_button_confirms_claim():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[None, None],
                           wait_confirmation=(True, False))
    messages = run(host)
    assert len(host.claimed_records) == 1
    assert any("领取成功，已确认按钮变灰" in m for m in messages)


def test_reward_popup_confirms_claim():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[None, None],
                           popups=[([("木炭", 1050)], ["note"])])
    messages = run(host)
    assert "note" in messages
    assert len(host.claimed_records) == 1
    assert any("领取成功，已确认报酬弹窗" in m for m in messages)


def test_late_popup_confirms_claim():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[None, None],
                           popups=[None, ([("玉钢", 800)], [])],
                           wait_confirmation=(False, True))
    messages = run(host)
    assert len(host.claimed_records) == 1
    assert any("领取成功，已确认报酬弹窗" in m for m in messages)


def test_game_rejected_is_not_counted():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[False])
    messages = run(host)
    assert host.claimed_records == []
    assert ("task_rewards.unconfirmed",
            {"tab": "日常", "stage": "after_click"}) in host.events
    assert any("游戏拒绝本次领取，本次不计成绩" in m for m in messages)


def test_no_evidence_is_unconfirmed():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[None, None],
                           wait_confirmation=(False, False))
    messages = run(host)
    assert host.claimed_records == []
    assert any("点击后未确认领取成功，本次不计成绩" in m for m in messages)


def test_stale_popup_swallow_triggers_retry():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[None, True],
                           stale=True, retry_point="retry-point")
    messages = run(host)
    assert host.claim_clicks == ["point", "retry-point"]
    assert any("任务信息已失效，刷新" in m for m in messages)
    assert any("补点一键领取" in m for m in messages)
    assert len(host.claimed_records) == 1


def test_stale_popup_without_recovered_button_gives_up():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[None, None],
                           stale=True, retry_point=None)
    messages = run(host)
    assert host.claim_clicks == ["point"]
    assert any("失效弹窗后没找到一键领取按钮，无法补点" in m for m in messages)


def test_game_result_true_skips_stale_dismiss():
    host = TaskRewardsHost(button_state=("active", "point"),
                           confirms=[True], stale=True,
                           retry_point="retry-point")
    run(host)
    assert host.claim_clicks == ["point"]  # 已确认领成，不补点


def test_close_list_and_finish_message():
    messages = run(TaskRewardsHost(button_state=("inactive", None)))
    assert "[TASK] 已关闭任务列表" in messages
    assert messages[-1] == "[TASK] 所有任务奖励领取完毕"
