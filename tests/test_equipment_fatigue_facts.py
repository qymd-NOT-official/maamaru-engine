"""Equipment and fatigue facts retain identity and full preset conditions."""
from unittest.mock import Mock
import pytest

from touken import game_state as gs
from touken.device_adapter import Point


def event(endpoint,body,at=1000):
    return {"endpoint":endpoint,"direction":"S->C","status":200,
            "ts":"2026-10-13 12:00:00","payload":{"now_time":at,"status":0,**body}}


def snapshot(fatigue=40):
    return event("/party/list",{"party":{"1":{"slot":{"1":{"serial_id":1}}}},
        "sword":{"1":{"serial_id":1,"fatigue":fatigue,"horse_serial_id":2,"item_id":1,"artifact_serial_id1":3}},
        "equip":{"2":{"serial_id":2,"equip_id":10008}},
        "artifact":{"3":{"serial_id":3,"artifact_id":99,"level":2,"usage_score":10}}})


def drain(stream):
    while True:
        try:next(stream)
        except StopIteration as stop:return stop.value


def test_equipment_translation_and_new_field_requirement():
    e=snapshot()
    assert gs.equipped_asset([e],1,1,"horse",now=1001)=={"name":"08望月"}
    assert gs.equipped_asset([e],1,1,"charm",now=1001)=={"name":"御守"}
    assert gs.equipped_asset([e],1,1,"treasure",now=1001)=={"name":None,"level":2,"affection":10}
    assert gs.equipped_asset([e],1,1,"horse",now=1001,previous=[e]) is None
    assert gs.equipped_asset([e],1,1,"horse",now=1700) is None


def test_old_same_second_fatigue_cannot_override_later_battle():
    old=snapshot()
    battle=event("/battle/battle",{})
    assert gs.fatigue_value([old,battle],1,1,now=1001) is None
    updated=event("/party/list",snapshot(100)["payload"])
    assert gs.fatigue_value([old,battle,updated],1,1,now=1001)==100


@pytest.mark.parametrize("value",[-1,101,True,49.5])
def test_invalid_fatigue_remains_unknown(value):
    assert gs.fatigue_value([snapshot(value)],1,1,now=1001) is None


def test_current_fatigue_reads_without_navigation_or_ocr(monkeypatch):
    from touken.flows.sakura import SakuraMixin
    host=SakuraMixin()
    host.device=Mock()
    host.navigate_to_stream=Mock(side_effect=AssertionError("known fatigue needs no navigation"))
    host.device.ocr_all.side_effect=AssertionError("known fatigue needs no OCR")
    monkeypatch.setattr(gs,"available_events",lambda _:[snapshot(0)])
    monkeypatch.setattr(gs.time,"time",lambda:1001)
    assert drain(host._check_fatigue(1,1))==0


def test_duplicate_free_horses_and_charms_are_equivalent():
    from test_formation_accessory import _ListMaa,_card
    from touken.flows import formation_accessory as module
    device=_ListMaa([_card("白毛x1",188)+_card("白毛x1",300)])
    assert module.find_horse_in_list(device,"白毛")== (Point(990,188),False)
    device=_ListMaa([_card("御守",145)+_card("御守",244)])
    assert module.find_charm_in_list(device,"御守")== (Point(990,145),False)


def test_same_name_treasure_does_not_skip_full_target_selection(monkeypatch):
    from test_formation_treasure import FakeMaa,NAME,TARGET
    from touken.flows import formation_treasure as module
    device=FakeMaa()
    device.slot=NAME  # Same name says nothing about the current level/affection.
    host=type("Host",(),{"device":device})()
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    assert drain(module.equip_preset_treasure_stream(host,1,TARGET)) is True
    assert (1045,218) in device.clicks and (1051,277) in device.clicks


@pytest.mark.parametrize("actual",[{"name":None,"level":2,"affection":0},{"name":None,"level":1,"affection":2}])
def test_treasure_wrong_details_cannot_be_overridden_by_name(monkeypatch,actual):
    from test_formation_treasure import FakeMaa,TARGET
    from touken.flows import formation_treasure as module
    host=type("Host",(),{"device":FakeMaa()})()
    monkeypatch.setattr(gs,"equipped_asset",Mock(side_effect=[None,actual]))
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    assert drain(module.equip_preset_treasure_stream(host,1,TARGET,team_no=1)) is False


def test_full_treasure_data_accepts_zero_click_without_name_ocr(monkeypatch):
    from test_formation_treasure import FakeMaa,TARGET
    from touken.flows import formation_treasure as module
    device=FakeMaa()
    host=type("Host",(),{"device":device})()
    monkeypatch.setattr(gs,"equipped_asset",lambda *args,**kwargs:dict(TARGET))
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    assert drain(module.equip_preset_treasure_stream(host,1,TARGET,team_no=1)) is True
    assert (1045,218) not in device.clicks



def test_unloading_navigates_to_selected_team_after_data_only_fatigue_read(monkeypatch):
    from touken.flows import sakura as module
    host=module.SakuraMixin()
    host.device=Mock()
    host._sakura_active_team=2
    host.current_location="本丸"
    def navigate(destination):
        assert destination=="编队"
        host.current_location=destination
        yield "navigated"
    host.navigate_to_stream=Mock(side_effect=navigate)
    host._sakura_equipment_page=Mock(return_value=False)
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    assert drain(host._sakura_unequip()) is False
    host.navigate_to_stream.assert_called_once_with("编队")
    host.device.click.assert_called_once_with(Point(*module._TEAM_TAB[2]))


@pytest.mark.parametrize("actual,expected",[({"name":"08望月"},True),({"name":"01王庭"},False)])
def test_accessory_game_result_overrides_missing_or_positive_name(monkeypatch,actual,expected):
    from touken.flows import formation_accessory as module
    host=type("Host",(),{"device":Mock(adb_address=None)})()
    host.device.ocr.return_value=Point(1051,220)
    monkeypatch.setattr(module,"_has_text",lambda device,text,roi:text!="否")
    monkeypatch.setattr(module,"_tokens",lambda device,roi: [] if expected or roi==module._TRANSFER else [("08望月",Point(700,160))])
    monkeypatch.setattr(module,"find_horse_in_list",lambda *args:(Point(990,188),False))
    monkeypatch.setattr(gs,"equipped_asset",Mock(side_effect=[{"empty":True},actual]))
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    assert drain(module.equip_preset_accessory_stream(host,1,"horse","08望月",team_no=1)) is expected
