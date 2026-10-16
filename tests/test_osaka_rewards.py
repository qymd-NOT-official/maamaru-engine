import json
from types import SimpleNamespace
from unittest.mock import Mock

from touken import youzu_log
from touken.flows.osaka import OsakaMixin, koban_reward_summary
from touken.game_state import _request_key
from touken.telemetry import TelemetryStore


def event(direction, payload, stamp="2026-10-16 12:52:19", endpoint="/battle/battle", status=200):
    return {"ts": stamp, "endpoint": endpoint, "direction": direction,
            "status": status, "payload": payload}


def battle(stamp="2026-10-16 12:52:19"):
    return [event("C->S", {"square_id": "5"}, stamp),
            event("S->C", {"status": 0, "koban": {"is_boss_defeat": True}, "result": {"reward": [
                {"item_type": "1", "item_id": "45", "item_num": "1", "bonus": 0},
                {"item_type": "4", "item_id": "0", "item_num": "525", "bonus": 525,
                 "bonus_monthcard_salary": 0}]}}, stamp)]


def test_boss_koban_includes_special_sword_bonus():
    assert youzu_log._payload_resource_rewards(battle()[1]["payload"]) == {"小判": 1050}
    ledger = youzu_log.build_ledger(battle())
    assert len(ledger["changes"]) == 1
    assert ledger["changes"][0]["delta"] == {"小判": 1050}
    assert ledger["changes"][0]["source_endpoint"] == "/battle/battle"
    assert ledger["changes"][0]["attribution"] == "confirmed"


def test_session_excludes_old_and_unrelated_rewards_and_repeated_responses():
    old = battle("2026-10-16 12:00:00")
    current = battle()
    mission = event("S->C", {"status": 0, "item": [
        {"item_type": 4, "item_num": 600}]}, endpoint="/mission/rewards")
    summary = koban_reward_summary(old + current + [current[1], mission], {_request_key(old[0])})
    assert summary == {"delta": 1050, "receipts": 1, "bosses": 1}


def test_failed_or_unpaired_response_does_not_become_income():
    request, response = battle()
    assert koban_reward_summary([response], set()) is None
    response["status"] = 500
    assert koban_reward_summary([request, response], set()) is None


class RewardFlow(OsakaMixin):
    def __init__(self):
        self.maa = SimpleNamespace()
        self.record_event = Mock()
        self._read_koban = Mock(side_effect=AssertionError("Must not open an inventory page"))

    def _osaka_stream_impl(self, **kwargs):
        self._osaka_floors_total = 1
        yield "floor completed"


def test_osaka_automatically_summarizes_json_without_inventory(monkeypatch):
    baseline = [event("S->C", {"status": 0}, endpoint="/home")]
    reader = Mock(side_effect=[baseline, baseline + battle()])
    monkeypatch.setattr("touken.flows.osaka.available_events", reader)
    monkeypatch.setattr("touken.telemetry.get_telemetry_store", Mock())
    writer = Mock()
    monkeypatch.setattr(youzu_log, "write_ledger", writer)
    flow = RewardFlow()
    assert list(flow.osaka_stream(target_floor=2)) == ["floor completed"]
    flow._read_koban.assert_not_called()
    flow.record_event.assert_called_once_with(
        "osaka.koban_session", delta=1050, floors=1, target_floor=2,
        source="game_rewards", summary_only=True, receipts=1)
    writer.assert_called_once()


def test_missing_json_does_not_stop_gameplay_or_invent_zero(monkeypatch):
    monkeypatch.setattr("touken.flows.osaka.available_events", lambda _: [])
    flow = RewardFlow()
    assert list(flow.osaka_stream()) == ["floor completed"]
    flow.record_event.assert_not_called()
    flow._read_koban.assert_not_called()


def test_average_uses_confirmed_boss_count_instead_of_screen_counter(monkeypatch):
    baseline = [event("S->C", {"status": 0}, endpoint="/home")]
    events = baseline + battle() + battle("2026-10-16 12:57:00")
    monkeypatch.setattr("touken.flows.osaka.available_events", Mock(side_effect=[baseline, events]))
    monkeypatch.setattr("touken.telemetry.get_telemetry_store", Mock())
    monkeypatch.setattr(youzu_log, "write_ledger", Mock())
    flow = RewardFlow()
    list(flow.osaka_stream())
    payload = flow.record_event.call_args.kwargs
    assert payload["delta"] == 2100
    assert payload["floors"] == 2


def test_missing_boss_result_does_not_claim_complete_session_income(monkeypatch):
    baseline = [event("S->C", {"status": 0}, endpoint="/home")]
    route = [event("C->S", {}, endpoint="/sally/eventforward"),
             event("S->C", {"status": 0, "reward": []}, endpoint="/sally/eventforward")]
    monkeypatch.setattr("touken.flows.osaka.available_events", Mock(side_effect=[baseline, baseline + route]))
    flow = RewardFlow()
    assert list(flow.osaka_stream()) == ["floor completed"]
    flow.record_event.assert_not_called()


def test_recursive_repair_continuation_records_one_session(monkeypatch):
    baseline = [event("S->C", {"status": 0}, endpoint="/home")]
    reader = Mock(side_effect=[baseline, baseline + battle()])
    monkeypatch.setattr("touken.flows.osaka.available_events", reader)
    monkeypatch.setattr("touken.telemetry.get_telemetry_store", Mock())
    monkeypatch.setattr(youzu_log, "write_ledger", Mock())
    class RecursiveFlow(RewardFlow):
        def _osaka_stream_impl(self, **kwargs):
            if not kwargs["_completed_floors"]:
                yield from self.osaka_stream(_completed_floors=1)
            else:
                self._osaka_floors_total = 1
                yield "floor completed"
    flow = RecursiveFlow()
    assert list(flow.osaka_stream()) == ["floor completed"]
    assert reader.call_count == 2
    flow.record_event.assert_called_once()


def test_json_session_summary_does_not_double_count_receipt_and_supports_run_summary(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / "telemetry.db")
    monkeypatch.setenv("MAAMARU_RUN_ID", "osaka-1")
    monkeypatch.setenv("MAAMARU_SCRIPT", "osaka")
    store.start_run("osaka-1", "osaka", started_at=100)
    samples = [
        (110, "osaka.floor_completed", {"completed": 1}),
        (120, "osaka.koban_session", {
            "delta": 1050, "floors": 1, "source": "game_rewards", "summary_only": True}),
        (115, "resource.change", {
            "resource": "小判", "delta": 1050, "source": "youzu_log.battle/battle",
            "attribution": "confirmed", "before": 1000, "after": 2050}),
    ]
    store._conn().executemany(
        "INSERT INTO events(ts, run_id, script, event_type, payload) VALUES (?, 'osaka-1', 'osaka', ?, ?)",
        [(stamp, kind, json.dumps(payload)) for stamp, kind, payload in samples])
    store._conn().commit()
    store.finish_run("osaka-1", "completed", ended_at=130)
    assert store.run_summary("osaka-1")["resource_delta"]["小判"] == 1050
    changes = store.resource_ledger(100, 130)["attributions"]
    assert sum(item["delta"] for item in changes if item["resource"] == "小判") == 1050
    store.close()


def test_panel_removes_experiment_and_ignores_old_saved_switch(monkeypatch):
    from panel import server
    from panel.script_runner import _SCRIPTS

    assert all(field["key"] != "compare_resources" for field in _SCRIPTS["osaka"]["params"])
    def team(*args):
        yield from ()
        return 3
    monkeypatch.setattr(server, "_team_with_preset_stream", team)
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        yield from ()
    list(server._build_osaka(SimpleNamespace(osaka_stream=run), "unused", {"compare_resources": False}))
    assert "koban_science" not in calls[0]
