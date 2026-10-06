# -*- coding: utf-8 -*-
"""touken/netlog.py：合成 net-export 日志的解析测试。

fixture 全部手工构造（假域名/假 uid/假数据），不使用任何真实抓包。
"""

import base64
import gzip
import json

import pytest

from touken import netlog

# 事件类型 id 由 constants.logEventTypes 决定，测试里自定义一套编号
T_START = 901
T_READ = 902
T_SENT = 903


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _request_event(ts: int, method: str, path: str, host: str) -> dict:
    head = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
    return {"time": str(ts), "type": T_SENT,
            "source": {"id": 500 + ts}, "params": {"bytes": _b64(head)}}


def _make_log(tmp_path, transactions) -> str:
    """transactions: [(sid, ts, url, method, path, body_bytes, gzip_it)]"""
    events = []
    for sid, ts, url, method, path, body, gz in transactions:
        host = url.split("://", 1)[1].split("/", 1)[0]
        events.append({"time": str(ts), "type": T_START,
                       "source": {"id": sid}, "params": {"url": url}})
        if method:
            events.append(_request_event(ts - 1, method, path, host))
        blob = gzip.compress(body) if gz else body
        events.append({"time": str(ts + 1), "type": T_READ,
                       "source": {"id": sid},
                       "params": {"bytes": _b64(blob)}})
    doc = {"constants": {"logEventTypes": {
        "URL_REQUEST_START_JOB": T_START,
        "URL_REQUEST_JOB_FILTERED_BYTES_READ": T_READ,
        "SSL_SOCKET_BYTES_SENT": T_SENT,
    }}, "events": events}
    path = tmp_path / "netlog.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_parse_pairs_request_and_gunzips_response(tmp_path):
    body = json.dumps({"ok": 1}).encode()
    log = _make_log(tmp_path, [
        (10, 100, "https://w007.example.jp/home/index?uid=42",
         "POST", "/home/index?uid=42", body, True),
    ])
    txs = netlog.parse_transactions(log)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.method == "POST"
    assert tx.path == "/home/index?uid=42"
    assert tx.request_line.startswith("POST /home/index?uid=42")
    assert tx.response_json() == {"ok": 1}


def test_response_without_matching_request_still_yielded(tmp_path):
    log = _make_log(tmp_path, [
        (11, 100, "https://w007.example.jp/party/list?uid=42",
         None, None, b"{}", False),
    ])
    txs = netlog.parse_transactions(log)
    assert len(txs) == 1
    assert txs[0].request_line is None
    assert txs[0].path == "/party/list?uid=42"


def test_host_filter_and_non_json(tmp_path):
    log = _make_log(tmp_path, [
        (12, 100, "https://w007.example.jp/forge?uid=42",
         "POST", "/forge?uid=42", b"{}", False),
        (13, 200, "https://static.example.jp/webgl/asset.bin",
         "GET", "/webgl/asset.bin", b"\x00\x01binary", False),
    ])
    all_txs = netlog.parse_transactions(log)
    assert len(all_txs) == 2
    bin_tx = [t for t in all_txs if "asset.bin" in t.url][0]
    assert bin_tx.response_json() is None

    filtered = netlog.parse_transactions(log, host_filter="w007.example.jp")
    assert len(filtered) == 1
    assert "forge" in filtered[0].url

    json_only = list(netlog.iter_json_transactions(log))
    assert [t.url for t, _ in json_only] == [
        t.url for t in all_txs if t.response_json() is not None]


def test_fifo_pairing_repeated_endpoint(tmp_path):
    log = _make_log(tmp_path, [
        (14, 100, "https://w007.example.jp/forge?uid=42",
         "POST", "/forge?uid=42", b'{"n":1}', False),
        (15, 200, "https://w007.example.jp/forge?uid=42",
         "POST", "/forge?uid=42", b'{"n":2}', False),
    ])
    txs = netlog.parse_transactions(log)
    assert [t.response_json()["n"] for t in txs] == [1, 2]
    assert all(t.request_line for t in txs)
