# -*- coding: utf-8 -*-
"""日服南瓜宿主（jp_pumpkin）：配置、板子判定、素材体检。

画面导航/点击不走真设备，素材体检用 monkeypatch 模拟模板缺失
（模板已于 2026-11-06 标定入仓）——宿主必须把缺什么列清楚然后停，绝不盲点。
"""
import json

import pytest

from touken import jp_pumpkin, jp_vision
from touken.jp_ledger import JpLedgerSession
from touken.netlog import Transaction
from touken.telemetry import TelemetryStore


class FakeDevice:
    base_size = (1508, 1088)

    def __init__(self):
        self.clicks = []

    def screenshot(self, force=False):
        return object()

    def click(self, point):
        self.clicks.append(point)
        return True


def make_host(params=None):
    logs = []
    host = jp_pumpkin.JpPumpkin(FakeDevice(), params or {}, log=logs.append)
    return host, logs


def test_activity_config_defaults_and_validation():
    host, _ = make_host()
    assert host._pumpkin_activity_config() == {
        'team_no': 1, 'max_skips': 4, 'difficulty': 1}
    host, _ = make_host({'team_no': '3', 'max_skips': '2', 'difficulty': '2'})
    assert host._pumpkin_activity_config() == {
        'team_no': 3, 'max_skips': 2, 'difficulty': 2}
    host, _ = make_host({'team_no': 'abc'})
    assert host._pumpkin_activity_config() == {}
    host, _ = make_host({'team_no': 9})
    assert host._pumpkin_team_error({'team_no': 9}, 9)
    assert host._pumpkin_team_error({'team_no': 2}, 2) is None


def test_identify_and_takeover_disabled():
    host, _ = make_host()
    assert host._pumpkin_identify_supported() is False
    assert host._pumpkin_takeover_requested(3) is False
    assert host._pumpkin_read_obtained({}) is None


def _feed_sheet(store, minute, cleared):
    payload = {
        "now": f"2026-11-05 12:{minute:02d}:00",
        "event": {"strike": {"sheet": {"1": {
            "count": 1, "rest_item": 8, "max_rest_item": 8,
            "panel": [{"panel_id": i, "is_clear": i < cleared}
                      for i in range(9)],
        }}}},
    }
    session = JpLedgerSession(store)
    session.feed(Transaction(url='/sally', path='/sally', method='POST',
                             request_line=None,
                             response_body=json.dumps(payload).encode()))


def test_board_full_reads_sheet_state(tmp_path, monkeypatch):
    store = TelemetryStore(tmp_path / 'jp.db')
    monkeypatch.setattr('touken.telemetry.get_jp_telemetry_store',
                        lambda: store)
    host, _ = make_host()

    # 没数据不算满（看门狗兜底，不假装知道）
    assert host._pumpkin_board_full({'difficulty': 1}) is False

    _feed_sheet(store, 0, 8)
    assert host._pumpkin_board_full({'difficulty': 1}) is False

    _feed_sheet(store, 1, 9)
    assert host._pumpkin_board_full({'difficulty': 1}) is True
    # 别的难度页签没有数据
    assert host._pumpkin_board_full({'difficulty': 2}) is False
    store.close()


@pytest.fixture
def templates_missing(monkeypatch):
    # 2026-11-06 起模板已标定入仓，缺素材的场景改为模拟
    def _boom(name):
        raise jp_vision.TemplateNotFound(f'{name}（模拟缺失）')
    monkeypatch.setattr(jp_vision, 'load_template', _boom)


def test_goto_sally_reports_missing_templates_and_stops(templates_missing):
    host, _ = make_host()
    gen = host._pumpkin_goto_sally()
    messages = []
    try:
        while True:
            messages.append(next(gen))
    except StopIteration as stop:
        assert stop.value is False
    text = "\n".join(messages)
    assert '没标定' in text
    for name in jp_pumpkin._REQUIRED_TEMPLATES:
        assert name in text


def test_preflight_lists_every_missing_template(templates_missing):
    host, _ = make_host()
    missing = host._preflight()
    assert len(missing) == len(jp_pumpkin._REQUIRED_TEMPLATES)
    assert all('pumpkin/' in item for item in missing)
