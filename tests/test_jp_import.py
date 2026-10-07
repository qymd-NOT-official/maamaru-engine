# -*- coding: utf-8 -*-
"""touken/jp_import.py：端点分类、摘要汇总、刀帐名录提取。

payload 全部手工构造（假数据），刀名预期值现场从 sword_db 查，
不硬编码、不使用真实抓包。
"""

import json

from touken import jp_import, netlog, sword_db


def _tx(path: str, payload) -> netlog.Transaction:
    body = payload if isinstance(payload, bytes) else json.dumps(
        payload).encode("utf-8")
    return netlog.Transaction(
        url=f"https://w007.example.jp{path}?uid=42",
        method="POST", path=f"{path}?uid=42",
        request_line=f"POST {path}?uid=42 HTTP/1.1",
        response_body=body)


def test_endpoint_path_strips_query_and_host():
    assert jp_import.endpoint_path(
        "https://w007.example.jp/home/index?uid=42") == "/home/index"
    assert jp_import.endpoint_path("/duty/start?uid=42") == "/duty/start"


def test_classify_known_and_unknown():
    assert jp_import.classify("/home/index")["kind"] == "home"
    assert jp_import.classify("/battle/battle")["encrypted"] is True
    assert jp_import.classify("/totally/new") is None


def test_summarize_counts_and_translates_resources():
    txs = [
        _tx("/home/index", {"resource": {
            "charcoal": 100, "steel": 50, "coolant": 30,
            "file": 20, "bill": 5, "recovered_at": "2026-01-01 00:00:00"}}),
        _tx("/battle/battle", b'{"data":"aa55"}'),
        _tx("/battle/battle", b'{"data":"bb66"}'),
        _tx("/brand/new_endpoint", {"x": 1}),
    ]
    summary = jp_import.summarize(txs)
    assert summary["kinds"]["home"] == 1
    assert summary["kinds"]["battle"] == 2
    assert summary["encrypted_battles"] == 2
    assert summary["unknown_paths"] == {"/brand/new_endpoint": 1}
    res = summary["resource"]
    assert res["木炭"] == 100 and res["玉钢"] == 50
    assert res["冷却材"] == 30 and res["砥石"] == 20 and res["委托符"] == 5
    assert "recovered_at" not in res  # 非数值字段不进资源快照


def test_summarize_tracks_swords_roster():
    txs = [_tx("/party/list", {"sword": {
        "900001": {"sword_id": 3, "level": 90, "fatigue": 46},
        "900002": {"sword_id": 5, "level": 1, "fatigue": 100},
    }, "party": {}})]
    summary = jp_import.summarize(txs)
    assert summary["swords"]["count"] == 2
    assert len(summary["swords"]["sample"]) == 2


def test_sword_roster_maps_names_via_sword_db():
    payload = {"sword": {
        "900001": {"sword_id": 3, "level": 90, "exp": 1275576,
                   "ranbu_level": 2, "ranbu_exp": 200, "fatigue": 46},
    }}
    roster = jp_import.sword_roster(payload)
    assert len(roster) == 1
    entry = roster[0]
    expected = sword_db.find_by_id(3)[1]
    assert entry["name"] == (expected.get("name_zh") or expected["name"])
    assert entry["ranbu_level"] == 2 and entry["ranbu_exp"] == 200
    assert entry["fatigue"] == 46


def test_sword_roster_handles_nested_organization_shape():
    # organization/index 的刀帐多包一层 {"sword": {"sword": {...}}}
    inner = {"900003": {"sword_id": 7, "level": 12}}
    roster = jp_import.sword_roster({"sword": {"sword": inner}})
    assert len(roster) == 1
    assert roster[0]["sword_id"] == 7


def test_sword_roster_empty_when_no_swords():
    assert jp_import.sword_roster({"party": {}}) == []
