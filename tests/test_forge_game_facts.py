"""Forge decisions use current game facts, while controls and policies stay unchanged."""
from unittest.mock import Mock

from touken import game_state
from touken.flows.smith import SmithMixin
from touken.maa_adapter import Point


def event(endpoint, body, at=1000, direction="S->C"):
    return {"endpoint": endpoint, "direction": direction, "status": 200,
            "payload": {"now_time": at, **body}, "ts": "2026-10-12 12:00:00"}


def roster(count=2):
    return event("/forge", {"status": 0, "sword_max": 200,
        "sword": {str(i): {"serial_id": str(i), "sword_id": 118} for i in range(1, count + 1)}})


def boundary():
    return {"after": 1000, "requests": set(), "forge_slot": 1, "forge_recipe": [700] * 4}


def test_normal_forge_checks_this_request_slot_recipe_and_response():
    request = event("/forge/startmultiple", {"slot_no": "1", "charcoal": "700", "steel": "700",
                    "coolant": "700", "file": "700"}, direction="C->S")
    response = event("/forge/startmultiple", {"status": 0}, 1001)
    assert game_state.operation_result([request, response], boundary(), "forge") is True
    response["payload"]["status"] = 1
    assert game_state.operation_result([request, response], boundary(), "forge") is False
    response["payload"]["status"] = 0
    request["payload"]["steel"] = "50"
    assert game_state.operation_result([request, response], boundary(), "forge") is False
    del request["payload"]["steel"]
    assert game_state.operation_result([request, response], boundary(), "forge") is None
    assert game_state.operation_result([response], boundary(), "forge") is None


def test_ten_forge_uses_actual_completion_count_and_excludes_old_requests():
    request = event("/forge/fastmultiple", {"slot_no": "1", "item": "10"}, direction="C->S")
    response = event("/forge/fastmultiple", {"status": 0, "finish_count": 10}, 1001)
    assert game_state.operation_result([request, response], boundary(), "forge_ten") is True
    old = boundary()
    old["requests"].add(game_state._request_key(request))
    assert game_state.operation_result([request, response], old, "forge_ten") is None
    response["payload"]["finish_count"] = 2
    assert game_state.operation_result([request, response], boundary(), "forge_ten") is False


def test_capacity_updates_exact_instances_after_collection_and_dismantle():
    collected = event("/forge/fastmultiple", {"status": 0, "finish_count": 2,
        "sword": [{"serial_id": 3}, {"serial_id": 4}]}, 1001)
    request = event("/sword/dismantle_many", {"serial_ids": "1"}, 1002, "C->S")
    completed = event("/sword/dismantle_many", {"status": 0}, 1003)
    assert game_state.forge_capacity([roster(), collected, request, completed], now=1004) == (3, 200)
    completed["payload"]["status"] = 1
    assert game_state.forge_capacity([roster(), collected, request, completed], now=1004) == (4, 200)


def test_missing_stale_or_ambiguous_capacity_never_invents_empty_slots():
    assert game_state.forge_capacity([roster()], now=1700) is None
    missing = roster()
    del missing["payload"]["sword"]
    assert game_state.forge_capacity([missing], now=1001) is None
    changed = event("/sword/dismantle_many", {"status": 0}, 1001)
    assert game_state.forge_capacity([roster(), changed], now=1002) is None
    expanded = event("/shop/buy", {"status": 0}, 1001)
    assert game_state.forge_capacity([roster(), expanded], now=1002) is None


def test_capacity_json_is_used_without_ocr(monkeypatch):
    monkeypatch.setattr(game_state.time, "time", lambda: 1001)
    host = SmithMixin()
    host._forge_events = Mock(return_value=[roster()])
    host.maa = Mock()
    host.maa.ocr_all.side_effect = AssertionError("JSON already supplies capacity")
    assert host._read_capacity() == (2, 200)


def test_capacity_missing_json_falls_back_to_actual_screen():
    host = SmithMixin()
    host._forge_events = Mock(return_value=[])
    host.maa = Mock()
    host.maa.ocr_all.return_value = [("196 / 200", Point(1100, 40))]
    assert host._read_capacity() == (196, 200)


def test_current_ticket_json_is_not_overwritten_by_screen(monkeypatch):
    monkeypatch.setattr(game_state.time, "time", lambda: 1001)
    host = SmithMixin()
    host._forge_events = Mock(return_value=[event("/forge", {"status": 0, "resource": {"bill": 90},
        "assist_item_id": "8", "assist_item_num": 100})])
    host.maa = Mock()
    host.maa.ocr_all.side_effect = AssertionError("JSON already supplies ticket counts")
    assert host._read_sidebar_counts() == {"委托符": 90, "加速符": 100}


def test_forge_game_success_does_not_require_return_title(monkeypatch):
    from test_smith_ocr import _LimitedForgeMaa
    monkeypatch.setattr("touken.flows.smith.time.sleep", lambda _: None)
    monkeypatch.setattr(game_state, "confirm_operation", Mock(return_value=True))
    host = SmithMixin()
    host.maa = _LimitedForgeMaa(popup=False)
    original = host.maa.ocr
    host.maa.ocr = lambda text, *args, **kwargs: None if text == "锻刀状况" else original(text, *args, **kwargs)
    assert host._start_forge(205)


def test_game_rejection_cannot_be_overridden_by_running_screen(monkeypatch):
    from test_smith_ocr import _LimitedForgeMaa
    monkeypatch.setattr("touken.flows.smith.time.sleep", lambda _: None)
    monkeypatch.setattr(game_state, "confirm_operation", Mock(return_value=False))
    host = SmithMixin()
    host.maa = _LimitedForgeMaa(popup=False)
    assert not host._start_forge(205)


def test_ten_game_rejection_overrides_visual_board_and_capacity(monkeypatch):
    from test_smith_ocr import _TenrenHost, _TenrenMaa, _BOARD1
    monkeypatch.setattr("touken.flows.smith.time.sleep", lambda _: None)
    monkeypatch.setattr(game_state, "confirm_operation", Mock(return_value=False))
    host = _TenrenHost(_TenrenMaa(boards=[_BOARD1]))
    messages = list(host.limited_forge_stream(total=10))
    assert not any(kind == "forge.tenren" for kind, _ in host.events)
    assert any("无法确认" in message for message in messages)


def test_failed_consumption_does_not_poison_next_capacity_update():
    request = event("/sword/dismantle_many", {"serial_ids": "1"}, 1001, "C->S")
    failed = event("/sword/dismantle_many", {"status": 1}, 1002)
    retry = event("/sword/dismantle_many", {"serial_ids": "1"}, 1003, "C->S")
    completed = event("/sword/dismantle_many", {"status": 0}, 1004)
    assert game_state.forge_capacity([roster(), request, failed, retry, completed], now=1005) == (1, 200)


def test_returning_to_forge_page_without_countdown_is_not_success(monkeypatch):
    from test_smith_ocr import _LimitedForgeMaa
    monkeypatch.setattr("touken.flows.smith.time.sleep", lambda _: None)
    host = SmithMixin()
    host.maa = _LimitedForgeMaa(popup=False)
    host._read_countdown = Mock(return_value=None)
    assert not host._start_forge(205)
