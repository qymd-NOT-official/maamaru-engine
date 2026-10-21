"""Activity accounting cannot block play, invent results, or replay a purchase."""
from unittest.mock import Mock
from pathlib import Path

import pytest

from touken import game_state as gs
from touken.flows.edocastle import EdocastleMixin


def event(endpoint, body, at=1000, direction="S->C"):
    return {"endpoint":endpoint,"direction":direction,"status":200,
            "ts":"2026-10-13 12:00:00","payload":{"now_time":at,**body}}


def purchase(result=1, num=1):
    return [event("/sally/recovercost",{"event_id":123,"num":num},direction="C->S"),
            event("/sally/recovercost",{"status":0,"result":result,"currency":{"money":700}},1001)]


def boundary():
    return {"after":1000,"requests":set(),"resources":{"小判":1000}}


def test_ticket_receipt_requires_this_request_and_reports_actual_cost():
    events=purchase()
    assert gs.ticket_receipt(events,boundary())=={"count":1,"spent":300}
    old=boundary();old["requests"].add(gs._request_key(events[0]))
    assert gs.ticket_receipt(events,old) is None
    assert gs.ticket_receipt([events[1]],boundary()) is None
    assert gs.ticket_receipt(purchase(result=0),boundary()) is False
    assert gs.ticket_receipt(purchase(num=3),boundary()) is False


def test_success_without_prior_balance_does_not_invent_cost():
    before=boundary();before["resources"]={}
    assert gs.ticket_receipt(purchase(),before)=={"count":1,"spent":None}


def binding(mechanic="allout", event_id=123, at=1000):
    return [event("/sally/eventsally",{"event_id":event_id},at,"C->S"),
            event("/sally/eventsally",{"status":0,mechanic:{"active":1}},at+1)]


def test_points_use_bound_activity_not_first_or_largest_counter():
    es=[*binding(),event("/sally",{"status":0,"point":{"999":99999,"123":0}},1002)]
    assert gs.activity_points(es,"raid",now=1003)==0
    assert gs.activity_points(es,"raid",now=1003,after=1003) is None
    assert gs.activity_points(es,"hanafuda",now=1003) is None
    assert gs.activity_points(es,"raid",now=1700) is None
    es.extend(binding("koikoi",456,1004))
    es.append(event("/sally",{"status":0,"point":{"123":100,"456":200}},1006))
    assert gs.activity_points(es,"hanafuda",now=1007)==200


def test_unpaired_or_ambiguous_activity_never_claims_a_counter():
    es=binding()
    es[1]["payload"]["koikoi"]={"active":1}
    es.append(event("/sally",{"status":0,"point":{"123":500}},1002))
    assert gs.activity_points(es,"raid",now=1003) is None
    assert gs.activity_points(es[1:],"raid",now=1003) is None


@pytest.mark.parametrize("mechanic",["raid","hanafuda"])
def test_activity_reader_prefers_game_points_even_when_no_ocr_config(monkeypatch,mechanic):
    from touken.flows.raid import RaidMixin
    from touken.flows.hanafuda import HanafudaMixin
    host=RaidMixin() if mechanic=="raid" else HanafudaMixin()
    host.device=Mock()
    es=[*binding("allout" if mechanic=="raid" else "koikoi"),
        event("/sally",{"status":0,"point":{"123":600}},1002)]
    monkeypatch.setattr(gs,"available_events",lambda _:es)
    monkeypatch.setattr(gs.time,"time",lambda:1003)
    host.device.ocr_all.side_effect=AssertionError("known points require no OCR")
    reader=host._read_shells_total if mechanic=="raid" else host._read_tama_total
    assert reader({})==600


@pytest.mark.parametrize("counts,expected",[((None,None),None),((10,None),None),((None,40),None),((10,40),30)])
def test_edo_missing_key_readings_never_block_departure_or_publish_estimates(monkeypatch,counts,expected):
    from touken.flows import edocastle as module
    host=EdocastleMixin()
    host.config={"edocastle":{"map_archive":str(Path("resource/base/maps/edocastle-4.json").resolve()),
        "ui_title":{"template":"title"},"activity_entry":{"template":"entry"}},
        "team_select":{"teams":{"3":[1,1]}}}
    host.device=Mock()
    host.current_location="出阵"
    host.navigate_to_stream=lambda _:iter(())
    host.set_progress=Mock()
    host._expedition_takeover_requested=lambda:False
    host._read_key_total=Mock(side_effect=counts)
    def enter(*args,**kwargs):
        yield "departed"
        return True,True
    def run(*args,**kwargs):
        yield "map complete"
        return 50,True
    host._enter_map_stream=Mock(side_effect=enter)
    host._map_run_stream=run
    host._wait_entry_screen=lambda *args,**kwargs:True
    host.record_event=Mock()
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    messages=list(host.edocastle_stream(max_runs=1))
    host._enter_map_stream.assert_called_once()
    payload=host.record_event.call_args.kwargs
    assert payload.get("keys")==expected
    assert ("keys" in payload) is (expected is not None)
    assert any("不记入成绩" in m for m in messages) is (expected is None)
    from touken.flows.report_judge import _is_fail
    assert not any(_is_fail(m) for m in messages)


@pytest.mark.parametrize("receipt",[False,{"count":1,"spent":900},{"count":1,"spent":None}])
def test_refill_game_result_overrides_sticky_button_and_config_price(monkeypatch,receipt):
    from test_battle_components import Flow,FakeMaa
    from touken.device_adapter import Point
    from touken.flows import battle as module
    host=Flow(FakeMaa(templates={"refill":Point(100,100),"recover":Point(200,200),"confirm":Point(300,300)}))
    host.record_event=Mock(return_value=1)
    cfg={"ticket_price":300,"ticket_recover":{"popup_button":{"template":"refill"},
        "recover_button":{"template":"recover"},"confirm_button":{"template":"confirm"}}}
    monkeypatch.setattr(gs,"confirm_ticket",lambda *_:receipt)
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    list(host._recover_ticket_stream(cfg))
    assert host._recover_ok is (receipt is not False)
    if host._recover_ok:
        host._record_ticket_refill(cfg,"[RAID]")
        calls=host.record_event.call_args_list
        changes=[c.kwargs for c in calls if c.args[0]=="resource.change"]
        assert changes==([] if receipt["spent"] is None else [{"resource":"小判","delta":-900,
            "source":"ticket.refilled","attribution":"confirmed","evidence":"game_refill_result",
            "source_event_id":1,"note":"RAID补手形"}])
    else:
        host.record_event.assert_not_called()



def test_single_popup_game_rejection_cannot_be_overridden_by_disappearance(monkeypatch):
    from test_battle_components import _RefillMaa,_SafeDepartHost,_REFILL_CFG,_drain_chain
    from touken.flows import battle as module
    device=_RefillMaa()
    host=_SafeDepartHost(device=device)
    monkeypatch.setattr(gs,"confirm_ticket",lambda *_:False)
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    messages,result=_drain_chain(host,_REFILL_CFG,auto_refill=True)
    assert result==(False,False)
    assert device.clicked.count(_RefillMaa.CONFIRM)==1
    assert any("未受理" in m for m in messages)
