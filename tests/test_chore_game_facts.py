"""Chore outcomes follow current game facts, including failures and missing data."""
import json
from unittest.mock import Mock

import pytest

from touken import game_state as gs
from touken.device_adapter import Point


def event(endpoint, body, at=1000, direction="S->C"):
    return {"endpoint": endpoint, "direction": direction, "status": 200,
            "payload": {"now_time": at, **body}, "ts": "2026-10-12 12:00:00"}


def boundary():
    return {"after": 1000, "requests": set()}


@pytest.mark.parametrize("kind,endpoint,params,body", [
    ("compose", "/composition/compose", {"base_id": "1", "material_id": "2"},
     {"sword": {"serial_id": 1}}),
    ("task_rewards", "/mission/rewards", {"category": "2", "count": "5"},
     {"item": [{"item_type": 4, "item_num": 250}]}),
    ("free_gift", "/shop/buy", {"shop_id": "1", "num": "1"}, {}),
    ("duty", "/duty/start", {}, {}),
])
def test_receipt_excludes_history_and_preserves_rejections(kind, endpoint, params, body):
    request = event(endpoint, params, direction="C->S")
    response = event(endpoint, {"status": 0, **body}, 1001)
    old = boundary()
    old["requests"].add(gs._request_key(request))
    assert gs.operation_result([request, response], old, kind) is None
    assert gs.operation_result([response], boundary(), kind) is None
    assert gs.operation_result([request, response], boundary(), kind) is True
    response["payload"]["status"] = 1
    assert gs.operation_result([request, response], boundary(), kind) is False


def test_compose_requires_correct_base_and_distinct_materials():
    request = event("/composition/compose", {"base_id": "1", "material_id": "2"}, direction="C->S")
    response = event("/composition/compose", {"status": 0, "sword": {"serial_id": 3}}, 1001)
    assert gs.operation_result([request, response], boundary(), "compose") is None
    response["payload"]["sword"]["serial_id"] = 1
    request["payload"]["material_id"] = "1"
    assert gs.operation_result([request, response], boundary(), "compose") is None


def test_shop_insufficient_resources_is_not_a_success():
    request = event("/shop/buy", {"shop_id": "1"}, direction="C->S")
    response = event("/shop/buy", {"status": 0, "is_lack": 1}, 1001)
    assert gs.operation_result([request, response], boundary(), "free_gift") is False


def test_duty_clock_uses_deadline_and_completion_invalidates_old_home():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 10, 12, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
    home = event("/home", {"status": 0, "duty": {"finished_at": "2026-10-13 10:00:00", "item_use_status": "0"}}, now)
    state = gs.duty_state([home], now=now)
    assert state == {"running": True, "finished_at": "2026-10-13 10:00:00", "started_at": "2026-10-12 10:00:00"}
    assert gs.duty_state([home], now=now + 601) is None
    assert gs.duty_state([home, event("/duty/complete", {"status": 0}, now + 1)], now=now + 2) is None


def troop_snapshot(equip=4):
    return event("/party/list", {"status": 0,
        "party": {"1": {"slot": {"1": {"serial_id": 1}}}},
        "sword": {"1": {"serial_id": 1, "equip_serial_id1": equip}},
        "equip": {"4": {"serial_id": 4, "equip_id": 109}}})


def test_troop_requires_new_slot_update_but_reuses_known_instance_name():
    before = troop_snapshot(0)
    update = event("/party/setequip", {"status": 0, "sword": {
        "serial_id": 1, "equip_serial_id1": 4}})  # same second, different response
    assert gs.troop_matches([before, update], 1, 1, 1, "轻步兵·新春", previous=[before], now=1001) is True
    assert gs.troop_matches([before], 1, 1, 1, "轻步兵·新春", previous=[before], now=1001) is None
    assert gs.troop_matches([before, update], 1, 1, 1, "盾兵·特上", previous=[before], now=1001) is False
    assert gs.troop_matches([before, update], 1, 1, 1, "轻步兵·新春", now=1700) is None


@pytest.mark.parametrize("outcome", [True, False, None])
def test_compose_return_page_alone_never_overrides_game_result(monkeypatch, outcome):
    from touken.flows import synthesize as module
    host = module.SynthesizeMixin()
    host.device = Mock(adb_address=None)
    host.device.template_match.return_value = Point(1200, 166)
    host.device.ocr.return_value = Point(500, 130)
    host.device.ocr_all.return_value = [("乱藤四郎", Point(440, 180))]
    host._protect_filter_on = lambda: False
    host.current_location = "强化"
    host.navigate_to_stream = lambda _: iter(())
    monkeypatch.setattr(module.sword_db, "find_by_name", lambda _: (1, {"name": "乱藤四郎"}))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(gs, "confirm_operation", lambda *_: outcome)
    messages = list(host.synthesize_stream())
    assert any("合成完成" in m for m in messages) is (outcome is True)


@pytest.mark.parametrize("outcome", [True, False])
def test_reward_game_result_overrides_visual_and_supplies_amount(monkeypatch, outcome):
    from test_flow_resource_changes import _RewardsHost, _ClaimFakeMaa
    from touken.flows import rewards as module
    device = _ClaimFakeMaa()
    device.stale_dismissed = True
    host = _RewardsHost(device)
    host._read_popup_guarded = Mock(side_effect=AssertionError("known game result needs no OCR"))
    def confirm(_maa, result, _kind):
        result["result_body"] = {"item": [{"item_type": 4, "item_num": 250}]}
        return outcome
    monkeypatch.setattr(module, "confirm_operation", confirm)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    messages = list(host.claim_task_rewards_stream())
    assert any(t == "task_rewards.claimed" for t, _ in host.events) is outcome
    changes = [p for t, p in host.events if t == "resource.change"]
    assert changes == ([{"resource": "小判", "delta": 250,
        "source": "task_rewards.reward_popup", "script": "task", "attribution": "confirmed",
        "evidence": "client_reward_list", "source_event_id": 1}] if outcome else [])


def test_duplicate_troop_rows_are_interchangeable():
    from touken.flows import formation_troops as module
    device = Mock()
    device.ocr_all.return_value = [("轻步兵·新春", Point(1000, 250)), ("轻步兵·新春", Point(1000, 150))]
    assert module.find_troop_in_list(device, "轻步兵·新春") == Point(1000, 150)


@pytest.mark.parametrize("outcome", [True, False])
def test_troop_game_result_overrides_missing_or_positive_ocr(monkeypatch, outcome):
    from test_formation_troops import FakeMaa
    from touken.flows import formation_troops as module
    device = FakeMaa()
    agent = type("Agent", (), {"device": device})()
    monkeypatch.setattr(gs, "troop_matches", Mock(side_effect=[False, outcome]))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    if outcome:
        original = device.ocr_all
        device.ocr_all = lambda roi: [] if roi.x == 315 else original(roi)
    stream = module.equip_preset_troop_stream(agent, 1, 1, "轻步兵·新春", team_no=1)
    while True:
        try:
            next(stream)
        except StopIteration as stop:
            assert stop.value is outcome
            break


def test_standalone_duty_dialogue_preserves_existing_clock(monkeypatch, tmp_path):
    from touken.flows import naihanka as module
    path = tmp_path / "naihanka.json"
    existing = {"started_at": "2026-10-12 10:00:00", "stats": {"机动": 1}}
    path.write_text(json.dumps(existing), encoding="utf-8")
    host = module.NaihankaMixin()
    host.config = {"naihanka": {"running_marker": {"template": "running"},
        "ui_title": {"template": "title"}, "cancel_button": {"template": "cancel"}, "skip_tap": (0, 0)}}
    host.device = Mock(adb_address=None)
    host.device.template_match.side_effect = lambda template, *_: None if template == "cancel" else Point(1, 1)
    host._click_point = Mock()
    monkeypatch.setattr(module, "STATUS_DIR", tmp_path)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    clock = iter(range(0, 100))
    monkeypatch.setattr(module.time, "time", lambda: next(clock))
    list(host.skip_dialogue_stream())
    assert json.loads(path.read_text(encoding="utf-8")) == existing


def test_equipment_member_check_uses_instance_not_ocr_name():
    state = troop_snapshot()
    assert gs.team_member_matches([state], 1, 1, {"observation_id": "youzu:1"}, now=1001) is True
    assert gs.team_member_matches([state], 1, 1, {"observation_id": "youzu:2"}, now=1001) is False
    assert gs.team_member_matches([state], 1, 1, {"observation_id": "youzu:1"}, now=1700) is None


@pytest.mark.parametrize("outcome", [True, False])
def test_free_gift_game_result_prevents_extra_purchase(monkeypatch, outcome):
    from touken.flows import rewards as module
    host = module.RewardsMixin()
    host.current_location = "万屋"
    host.navigate_to_stream = lambda _: iter(())
    host.config = {"shop": {"free_gift": {
        "find_text": {"expected": "暖心", "roi": [0, 100, 700, 650]},
        "claim_button": {"template": "claim"}, "popup_verify": {"expected": "0", "roi": [0, 0, 1280, 720]},
        "popup_buy": {"template": "buy"}}}}
    host.device = Mock(adb_address=None)
    host.device.ocr.side_effect = lambda expected, *args, **kwargs: Point(145, 145) if expected in ("暖心", "0") else None
    host.device.template_match.side_effect = lambda template, **kwargs: Point(500, 600) if template == "buy" else Point(500, 300)
    monkeypatch.setattr(module, "confirm_operation", lambda *_: outcome)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    messages = list(host.claim_free_gift_stream())
    buys = [call for call in host.device.click.call_args_list if call.args[0] == Point(500, 600)]
    assert len(buys) == 1
    assert any("领取成功" in m for m in messages) is outcome


def test_duty_running_data_skips_start_even_without_marker(monkeypatch, tmp_path):
    from touken.flows import naihanka as module
    host = module.NaihankaMixin()
    host.current_location = "内番"
    host.config = {"naihanka": {"running_marker": {"template": "running"}}}
    host.device = Mock(adb_address=None)
    host.device.template_match.return_value = None
    host.navigate_to_stream = lambda _: iter(())
    host.naihanka_observe_stream = lambda: iter(())
    host.naihanka_snapshot_stream = lambda: iter(())
    monkeypatch.setattr(module, "STATUS_DIR", tmp_path)
    monkeypatch.setattr(module, "duty_state", lambda _: {"running": True,
        "started_at": "2026-10-12 10:00:00", "finished_at": "2026-10-13 10:00:00"})
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert any("已经在内番中" in m for m in host.naihanka_stream())
    host.device.click.assert_not_called()
    assert json.loads((tmp_path / "naihanka.json").read_text(encoding="utf-8"))["started_at"] == "2026-10-12 10:00:00"


def test_expired_reward_list_is_rejected_despite_transport_success():
    request = event("/mission/rewards", {"category": "2", "count": "5"}, direction="C->S")
    response = event("/mission/rewards", {"status": 0, "reward_status": 2, "item": [], "finish_mission_ids": []}, 1001)
    assert gs.operation_result([request, response], boundary(), "task_rewards") is False


def test_duty_without_normal_duration_evidence_never_invents_start():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 10, 12, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
    home = event("/home", {"status": 0, "duty": {"finished_at": "2026-10-13 10:00:00"}}, now)
    assert gs.duty_state([home], now=now)["started_at"] is None


@pytest.mark.parametrize("outcome", [True, False])
def test_duty_receipt_avoids_missing_marker_wait_and_preserves_rejection(monkeypatch, tmp_path, outcome):
    from touken.flows import naihanka as module
    host = module.NaihankaMixin()
    host.config = {"naihanka": {"running_marker": {"template": "running"},
        "ui_title": {"template": "title"}, "cancel_button": {"template": "cancel"}, "skip_tap": (0, 0)}}
    host.device = Mock(adb_address=None)
    host.device.template_match.side_effect = lambda template, *_: Point(1, 1) if template == "title" else None
    host._click_point = Mock()
    host._duty_boundary = {"result_at": 1000}
    monkeypatch.setattr(module, "STATUS_DIR", tmp_path)
    monkeypatch.setattr(module, "confirm_operation", lambda *_: outcome)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    clock = iter(range(100))
    monkeypatch.setattr(module.time, "time", lambda: next(clock))
    messages = list(host.skip_dialogue_stream())
    assert any("内番开始成功" in m for m in messages) is outcome
    assert host._click_point.call_count < 15
    assert (tmp_path / "naihanka.json").exists() is outcome
