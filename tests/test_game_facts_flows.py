"""Game facts drive collection, redispatch and repair; screens only locate controls."""
import time
from unittest.mock import Mock

from touken import game_state
from touken.flows.expedition import ExpeditionMixin
from touken.flows.repair import RepairMixin


def event(body, endpoint="/party/list", at=None):
    return {"direction": "S->C", "endpoint": endpoint, "status": 200,
            "payload": {"status": 0, "now_time": time.time() if at is None else at, **body}}


def repair_state(hp=20, *, twin=False):
    catalog = 47  # Game sword_id, distinct from the local catalog key.
    swords = {"111": {"sword_id": catalog, "hp": hp, "hp_max": 45}}
    if twin:
        swords["222"] = {"sword_id": catalog, "hp": 10, "hp_max": 45}
    return game_state.sword_facts([event({"sword": swords, "party": {
        "3": {"slot": {"1": {"serial_id": "111"}}}}})])


def test_repair_row_uses_exact_member_and_json_hp():
    fact = RepairMixin._repair_row_fact({"name": "五虎退", "prefix": "三之一"}, repair_state(twin=True))
    assert fact["serial_id"] == 111
    assert fact["team_no"] == 3
    assert fact["injury"] == "中伤"
    assert RepairMixin._repair_row_fact({"name": "五虎退", "prefix": ""}, repair_state(twin=True)) is False


def test_repair_json_decision_ignores_missing_stamp(monkeypatch):
    """JSON 确认有伤就按 JSON 决策：画面没读到章不影响中伤/真轻伤烧符；
    hp≥90% 的擦伤（数据分档最细也是轻伤）不烧符，满血整行跳过"""
    from test_flow_resource_changes import _FlowHost
    monkeypatch.setattr("touken.flows.repair.time.sleep", lambda _: None)
    for hp, speed, injury in [(20, True, "中伤"), (40, True, "轻伤"),
                              (44, False, None), (45, None, None)]:
        host = _FlowHost([("五虎退", 200)], [("三之一", 200)])
        host._repair_game_facts = Mock(return_value=repair_state(hp))
        messages = list(host.repair_stream())
        assert host.speed_calls == ([] if speed is None else [speed])
        if speed is not None:
            queued = next(p for kind, p in host.events if kind == "repair.queued")
            assert queued["serial_id"] == 111
            assert queued["injury"] == injury
        if speed is False:
            assert any("省一张加速符" in m for m in messages)
        if injury is not None:
            assert any("不一致" in m and "按游戏数据决策" in m for m in messages)
        else:
            assert not any("不一致" in m for m in messages)


def test_repair_json_overrides_conflicting_stamp(monkeypatch):
    """章与 JSON 冲突时按 JSON 决策（章只是旁证，记日志不翻案）"""
    from test_flow_resource_changes import _FlowHost
    monkeypatch.setattr("touken.flows.repair.time.sleep", lambda _: None)
    # 画面读到中伤章，但 JSON 血量只是轻伤：按轻伤决策记档
    host = _FlowHost([("五虎退", 200), ("中伤", 195)], [("三之一", 200)])
    host._repair_game_facts = Mock(return_value=repair_state(40))
    messages = list(host.repair_stream())
    assert host.speed_calls == [True]
    queued = next(p for kind, p in host.events if kind == "repair.queued")
    assert queued["injury"] == "轻伤"
    assert any("不一致" in m and "按游戏数据决策" in m for m in messages)


def test_repair_matching_stamp_stays_quiet(monkeypatch):
    """章与 JSON 一致时不唠叨旁证日志"""
    from test_flow_resource_changes import _FlowHost
    monkeypatch.setattr("touken.flows.repair.time.sleep", lambda _: None)
    host = _FlowHost([("五虎退", 200), ("轻伤", 198)], [("三之一", 200)])
    host._repair_game_facts = Mock(return_value=repair_state(40))
    messages = list(host.repair_stream())
    assert host.speed_calls == [True]
    assert not any("不一致" in m for m in messages)


def test_json_does_not_bypass_repair_blacklist(monkeypatch):
    from test_flow_resource_changes import _FlowHost
    monkeypatch.setattr("touken.flows.repair.time.sleep", lambda _: None)
    host = _FlowHost([("五虎退", 200)], [("三之一", 200)])
    host._repair_game_facts = Mock(return_value=repair_state())
    list(host.repair_stream(blacklist=["五虎退"]))
    assert host.speed_calls == []


def test_ambiguous_repair_row_is_reported_without_claiming_complete(monkeypatch):
    from test_flow_resource_changes import _FlowHost
    monkeypatch.setattr("touken.flows.repair.time.sleep", lambda _: None)
    host = _FlowHost([("五虎退", 200)], [])
    host._repair_game_facts = Mock(return_value=repair_state(twin=True))
    messages = list(host.repair_stream())
    assert host.speed_calls == []
    assert host.last_repair_stats["unresolved"] == 1
    assert "未能确认身份" in messages[-1]
    assert "✅" not in messages[-1]


def settlement(at, team=2, field=1):
    return game_state.expedition_settlements([
        event({"party_no": team, "field_id": field, "reward": []}, "/conquest/complete", at)], 0)[0]


class CollectionHost(ExpeditionMixin):
    def __init__(self, home=True):
        self.config = {"expedition": {
            "return_banner_ocr": {"expected": "banner", "roi": [0, 0, 100, 100]},
            "result_title_ocr": {"expected": "result", "roi": [0, 0, 100, 100]},
            "collect_tap": [100, 100], "home_ui": {"template": "home"}}}
        self.device = Mock()
        self.device.ocr.return_value = None
        self.device.template_match.return_value = home
        self._click_point = Mock()
        self._record_visual_settlement = Mock()
        self._forget_collected_records = Mock()
        self.departures = []

    def navigate_to_stream(self, destination):
        self.current_location = destination
        yield destination

    def expedition_stream(self, **params):
        self.departures.append(params)
        yield "dispatched"


def test_collection_and_same_map_redispatch_need_no_result_text(monkeypatch):
    monkeypatch.setattr("touken.flows.expedition.time.sleep", lambda _: None)
    host = CollectionHost()
    old, current = settlement(1000), settlement(time.time())
    host._collection_facts = Mock(side_effect=[[old], [current]])
    messages = list(host.collect_expedition_stream(redispatch="same"))
    assert any("收了 1 份" in message for message in messages)
    assert host.departures == [{"era": 1, "map_name": None, "team_no": 2, "map_slot": 1}]
    host._record_visual_settlement.assert_not_called()
    assert host._collection_facts.call_args.args[1] == {old["payload"]["receipt_key"]}


def test_historic_and_repeated_receipts_do_not_create_new_collection():
    old, fresh = settlement(1000), settlement(1001)
    assert game_state.filter_settlements([old, fresh, fresh], 1000, [old["payload"]["receipt_key"]]) == [fresh]


def test_uncertain_page_retains_json_receipt_and_does_not_redispatch(monkeypatch):
    monkeypatch.setattr("touken.flows.expedition.time.sleep", lambda _: None)
    host = CollectionHost(home=False)
    host._collection_facts = Mock(side_effect=[[], [settlement(time.time())]])
    messages = list(host.collect_expedition_stream(redispatch="same"))
    assert any("游戏已确认" in message for message in messages)
    assert host.departures == []
    assert any("暂不再派" in message for message in messages)


def test_partial_hp_updates_do_not_refresh_other_members_or_fields():
    events = [event({"sword": {"111": {"sword_id": 118, "hp": 20, "hp_max": 45},
                              "222": {"sword_id": 118, "hp": 20, "hp_max": 45}},
                     "party": {"1": {"slot": {"1": {"serial_id": "111"},
                                                "2": {"serial_id": "222"}}}}}, at=1000),
              event({"sword": {"serial_id": "111", "hp": 45}}, "/repair/complete", at=1001)]
    report = game_state.injury_report(events, 1, now=1002)
    assert [member["hp"] for member in report["members"]] == [45, 20]
    state = game_state.sword_facts(events)
    assert state["hp_times"]["222"][0] == 1000
    assert state["swords"]["111"]["hp_max"] == 45


def test_combat_without_all_member_hp_cannot_reuse_prebattle_health():
    events = [event({"sword": {"111": {"hp": 45, "hp_max": 45}},
                     "party": {"1": {"slot": {"1": {"serial_id": "111"}}}}}, at=1000),
              event({}, "/battle/battle", at=1001)]
    assert game_state.injury_report(events, 1, now=1002) is None
    events.append(event({"sword": {"111": {"hp": 5}}}, "/battle/battle", at=1002))
    assert game_state.injury_report(events, 1, now=1003)["max_injury"] == "重伤"


def test_membership_update_selects_new_instance_without_throwing_away_pool():
    base = event({"sword": {"111": {"hp": 45, "hp_max": 45},
                            "222": {"hp": 5, "hp_max": 45}},
                  "party": {"1": {"slot": {"1": {"serial_id": "111"}}}}}, at=1000)
    update = event({"1": {"slot": {"1": {"serial_id": "222"}}}}, "/party/setsword", at=1001)
    report = game_state.injury_report([base, update], 1, now=1002)
    assert report["members"][0]["serial_id"] == 222
    assert report["max_injury"] == "重伤"
    cleared = event({"party": {"1": {"slot": {"1": {"serial_id": None}}}}},
                    "/party/dissolution", at=1002)
    assert game_state.injury_report([base, update, cleared], 1, now=1003) is None


def test_non_hp_update_cannot_refresh_old_health_time():
    base = event({"sword": {"111": {"hp": 45, "hp_max": 45}},
                  "party": {"1": {"slot": {"1": {"serial_id": "111"}}}}}, at=1000)
    update = event({"sword": {"111": {"level": 99}}}, "/conquest/complete", at=1601)
    assert game_state.injury_report([base, update], 1, now=1602) is None
    empty = event({"sword": {}, "party": {}}, at=1001)
    assert game_state.sword_facts([base, empty])["swords"] == {}


def test_storage_failure_does_not_overrule_confirmed_collection(monkeypatch):
    host = CollectionHost()
    host.device.adb_path, host.device.adb_address = "adb", "address"
    proof = event({"party_no": 2, "field_id": 1}, "/conquest/complete", at=1001)
    monkeypatch.setattr(game_state, "read_events", Mock(return_value=[proof]))
    monkeypatch.setattr("touken.record_sync.consume_game_records", Mock(side_effect=OSError("storage")))
    assert len(host._collection_facts(1000)) == 1
    assert host._collection_sync_failed


def test_deferred_visual_observation_does_not_write_competing_receipt(monkeypatch):
    from test_expedition import _SettleFlow, _SettleMaa, _bright_frame, _settle_texts
    monkeypatch.setattr("touken.flows.expedition.time.sleep", lambda _: None)
    flow = _SettleFlow(_SettleMaa(_bright_frame(), _settle_texts()))
    observation = flow.observe_expedition_settlement("collect", seen=[], record=False)
    assert observation["new"]
    assert flow.events == []


def test_bad_countdown_cache_cannot_override_collection_fact(monkeypatch):
    from touken.flows import expedition
    save = Mock()
    monkeypatch.setattr(expedition, "_save_exp_record", save)
    monkeypatch.setattr(expedition, "_load_exp_record", lambda: [])
    ExpeditionMixin._forget_collected_records([settlement(1001)])
    save.assert_not_called()


def test_late_older_response_cannot_replace_newer_health_or_hide_battle():
    base = event({"sword": {"111": {"hp": 45, "hp_max": 45}},
                  "party": {"1": {"slot": {"1": {"serial_id": "111"}}}}}, at=1000)
    hurt = event({"sword": {"111": {"hp": 5}}}, "/battle/battle", at=1002)
    delayed = event({"sword": {"111": {"hp": 45}}}, "/repair/complete", at=1001)
    assert game_state.injury_report([base, hurt, delayed], 1, now=1003)["max_injury"] == "重伤"
    battle = event({}, "/battle/battle", at=1002)
    assert game_state.injury_report([base, battle, delayed], 1, now=1003) is None
