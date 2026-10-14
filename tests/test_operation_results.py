"""Synthetic operation receipts, rejected actions, missing evidence and stale state."""
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from touken import game_state


def event(endpoint, direction, body, at=1001, http=200):
    return {"endpoint": endpoint, "direction": direction, "status": http,
            "ts": "2026-10-12 12:00:00", "payload": {"now_time": at, **body}}


def boundary():
    return {"after": 1000, "requests": set(), "repairs": set(), "repair_known": True,
            "swords": {"111": {"hp": 10, "hp_max": 45}}}


@pytest.mark.parametrize("kind,endpoint,params", [
    ("union", "/composition/union", {"base_serial_id": "111", "material_serial_id": "222"}),
    ("dismantle", "/sword/dismantle_many", {"serial_ids": "222"}),
])
def test_only_paired_current_success_confirms_action(kind, endpoint, params):
    request = event(endpoint, "C->S", params)
    response = event(endpoint, "S->C", {"status": 0})
    assert game_state.operation_result([request, response], boundary(), kind) is True
    assert game_state.operation_result([response], boundary(), kind) is None
    old = boundary()
    old["requests"].add(game_state._request_key(request))
    assert game_state.operation_result([request, response], old, kind) is None
    response["payload"]["status"] = 1
    assert game_state.operation_result([request, response], boundary(), kind) is False
    response["payload"]["status"] = 0
    response["status"] = 500
    assert game_state.operation_result([request, response], boundary(), kind) is False


def test_overlapping_requests_are_not_guessed():
    request = event("/composition/union", "C->S", {"base_serial_id": "111", "material_serial_id": "222"})
    response = event("/composition/union", "S->C", {"status": 0})
    assert game_state.operation_result([request, request, response], boundary(), "union") is None


def test_returned_union_instance_must_match_request():
    request = event("/composition/union", "C->S", {"base_serial_id": "111", "material_serial_id": "222"})
    response = event("/composition/union", "S->C", {"status": 0, "sword": {"serial_id": "333"}})
    assert game_state.operation_result([request, response], boundary(), "union") is None


def test_actual_dismantled_instances_are_preserved():
    request = event("/sword/dismantle_many", "C->S", {"serial_ids": "111,222"})
    response = event("/sword/dismantle_many", "S->C", {"status": 0})
    token = boundary()
    assert game_state.operation_result([request, response], token, "dismantle") is True
    assert token["serial_ids"] == [111, 222]


def test_repair_menu_and_unchanged_hp_are_not_completion():
    menu = event("/repair", "S->C", {"status": 0, "repair": [], "sword": {"111": {"hp": 10}}})
    assert game_state.operation_result([menu], boundary(), "repair", 111) is None
    fixed = event("/repair", "S->C", {"status": 0, "sword": {"111": {"hp": 45}}})
    assert game_state.operation_result([fixed], boundary(), "repair", 111) is True
    assert game_state.operation_result([fixed], boundary(), "repair", 222) is None
    fixed["payload"]["now_time"] = 999
    assert game_state.operation_result([fixed], boundary(), "repair", 111) is None


def test_repair_queue_requires_new_target_entry():
    queued = event("/home/situation", "S->C", {"status": 0,
        "repair": [{"serial_id": "111", "finished_at": "2026-10-12 13:00:00"}]})
    assert game_state.operation_result([queued], boundary(), "repair", 111) is True
    assert game_state.operation_result([queued], boundary(), "repair", 222) is None
    old = boundary()
    old["repairs"] = {("111", "2026-10-12 13:00:00")}
    assert game_state.operation_result([queued], old, "repair", 111) is None
    old["repairs"] = set()
    old["repair_known"] = False
    assert game_state.operation_result([queued], old, "repair", 111) is None


@pytest.mark.parametrize("outcome", [None, False])
def test_repair_does_not_claim_start_or_record_cost_on_unknown_result(monkeypatch, outcome):
    from test_flow_resource_changes import _Host, _RepairFakeMaa, _REPAIR_CFG
    monkeypatch.setattr("touken.flows.repair.time.sleep", lambda _: None)
    monkeypatch.setattr(game_state, "begin_operation", Mock(return_value=boundary()))
    monkeypatch.setattr(game_state, "confirm_operation", Mock(return_value=outcome))
    host = _Host({"repair": _REPAIR_CFG})
    host.maa = _RepairFakeMaa({})
    messages = list(host._repair_one(_REPAIR_CFG, 200, False))
    assert not any("已开工" in message for message in messages)
    assert host.events == []


@pytest.mark.parametrize("outcome", [None, False])
def test_union_returning_to_materials_does_not_claim_success_without_receipt(monkeypatch, outcome):
    from test_sugar_flow import Flow
    monkeypatch.setattr("touken.flows.sugar.time.sleep", lambda _: None)
    monkeypatch.setattr(game_state, "begin_operation", Mock(return_value=boundary()))
    monkeypatch.setattr(game_state, "confirm_operation", Mock(return_value=outcome))
    flow = Flow()
    messages = list(flow._shugo_loop_stream(False))
    assert flow.maa.feeds == 1
    assert not any("完成第" in message for message in messages)
    assert "炼了 0 轮" in messages[-1]


@pytest.mark.parametrize("outcome", [None, False, True])
def test_dismantle_marks_daily_completion_only_with_receipt(monkeypatch, outcome):
    from touken.flows.smith import SmithMixin
    from touken.maa_adapter import Point
    monkeypatch.setattr("touken.flows.smith.time.sleep", lambda _: None)
    monkeypatch.setattr(game_state, "begin_operation", Mock(return_value=boundary()))
    monkeypatch.setattr(game_state, "confirm_operation", Mock(return_value=outcome))
    mark = Mock()
    monkeypatch.setattr("touken.flows.smith._mark_dismantled_today", mark)
    host = SmithMixin()
    host.config = {}
    host.maa = Mock()
    host.maa.ocr.return_value = Point(1, 1)
    host._scan_whitelist_row = Mock(return_value=("五虎退", 200))
    host._read_dismantle_resources = Mock(return_value={})
    host.record_event = Mock()
    host._emit_dismantle_resources = Mock()
    messages = list(host.dismantle_stream(max_dismantle=1, _from_forge=True, whitelist=["五虎退"]))
    assert mark.call_count == (1 if outcome is True else 0)
    assert any("分解完成" in message for message in messages) is (outcome is True)
    assert host.record_event.call_count == (1 if outcome is True else 0)


def _dismantle_event(day_offset=0, direction='S->C', ok=True):
    import time as _time
    ts = _time.time() + day_offset * 86400
    return {"direction": direction, "endpoint": "/sword/dismantle_many",
            "status": 200, "ts": ts,
            "payload": {"status": 0 if ok else 1, "now_time": ts}}


def test_dismantled_today_in_log_covers_manual_player_action(monkeypatch):
    from touken.flows import smith
    from touken import game_state
    today = _dismantle_event()
    yesterday = _dismantle_event(day_offset=-1)
    monkeypatch.setattr(game_state, "read_events",
                        lambda *a: [yesterday, today])
    maa = SimpleNamespace(adb_path="adb", adb_address="addr")
    assert smith.dismantled_today_in_log(maa) is True
    monkeypatch.setattr(game_state, "read_events", lambda *a: [yesterday])
    assert smith.dismantled_today_in_log(maa) is False
    monkeypatch.setattr(game_state, "read_events", lambda *a: [])
    assert smith.dismantled_today_in_log(maa) is None
    # 只有请求没有成功响应不算数
    req = _dismantle_event(direction='C->S')
    monkeypatch.setattr(game_state, "read_events", lambda *a: [req])
    assert smith.dismantled_today_in_log(maa) is False


def test_daily_dismantle_step_skips_when_log_shows_player_dismantled(monkeypatch):
    from touken.flows import daily, smith
    monkeypatch.setattr(smith, "dismantled_today", lambda: False)
    host = SimpleNamespace(maa=SimpleNamespace())
    calls = []

    def dismantle_stream(**kw):
        calls.append(kw)
        yield "dismantled"

    host.dismantle_stream = dismantle_stream
    monkeypatch.setattr(smith, "dismantled_today_in_log", lambda maa: True)
    messages = list(daily.DailyMixin._dismantle_step(host))
    assert calls == []
    assert any("游戏记录显示今天已经刀解过了" in m for m in messages)
    monkeypatch.setattr(smith, "dismantled_today_in_log", lambda maa: None)
    messages = list(daily.DailyMixin._dismantle_step(host))
    assert calls == [{"max_dismantle": 1}]
