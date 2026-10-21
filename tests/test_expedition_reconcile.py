# -*- coding: utf-8 -*-
"""远征派遣记录与游戏现状对账（玩家优先，2026-10-14）。

残留记录（收菜时数据通道不可用留下、玩家其实已手动收菜）+ 游戏数据
确认队伍已在家 → 翻篇删档、照常派遣；游戏说还在外 / 拿不到数据 →
保留并跳过（旧行为）；记录损坏 → 不炸。
"""
import json
import time

from touken import game_state
from touken.expedition_state import load_active_expeditions
from touken.flows import expedition as expedition_flow
from touken.flows.daily import DailyMixin

NOW = 1_000_000.0


def event(body, endpoint="/party/list", at=NOW):
    return {"direction": "S->C", "endpoint": endpoint, "status": 200,
            "payload": {"status": 0, "now_time": at, **body}}


def party_event(team_no, status, at):
    return event({"party": {str(team_no): {"party_no": str(team_no),
                                           "status": str(status)}}}, at=at)


def active_record(now, minutes=60):
    """一条还没到期的派遣记录（now-60s 派出，共 minutes 分钟）。"""
    return {"map_code": "B2", "map_name": "测试图", "era": 2, "slot": 2,
            "duration_min": minutes,
            "dispatched_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(now - 60))}


class _NoMaa:
    """没有数据通道的宿主（adb_address 都拿不到）。"""


class _RecordStore:
    """有状态的 _load/_save 替身：reconcile 删档后循环再读到新内容。"""

    def __init__(self, records):
        self.records = records

    def load(self):
        return self.records

    def save(self, records):
        self.records = records


# ---------------------------------------------------------------- party_home

class TestPartyHome:
    def test_status_1_means_home(self):
        assert game_state.party_home([party_event(2, 1, NOW)], 2,
                                     now=NOW) is True

    def test_other_status_means_away(self):
        assert game_state.party_home([party_event(2, 2, NOW)], 2,
                                     now=NOW) is False
        assert game_state.party_home([party_event(2, 3, NOW)], 2,
                                     now=NOW) is False

    def test_latest_response_wins(self):
        events = [party_event(2, 2, NOW - 30), party_event(2, 1, NOW)]
        assert game_state.party_home(events, 2, now=NOW) is True

    def test_no_fresh_success_response_is_unknown(self):
        assert game_state.party_home([], 2, now=NOW) is None
        stale = [party_event(2, 1, NOW - game_state.FACT_MAX_AGE - 1)]
        assert game_state.party_home(stale, 2, now=NOW) is None
        request_only = [{**party_event(2, 1, NOW), "direction": "C->S"}]
        assert game_state.party_home(request_only, 2, now=NOW) is None

    def test_missing_row_or_status_is_unknown(self):
        other_team = [event({"party": {"1": {"status": "1"}}}, at=NOW)]
        assert game_state.party_home(other_team, 2, now=NOW) is None
        no_status = [event({"party": {"2": {"party_no": "2"}}}, at=NOW)]
        assert game_state.party_home(no_status, 2, now=NOW) is None
        bad_body = [event({"sword": {}}, at=NOW)]
        assert game_state.party_home(bad_body, 2, now=NOW) is None


# ------------------------------------------------- reconcile_records_with_game

class TestReconcileRecords:
    def _patch_store(self, monkeypatch, records):
        store = _RecordStore(records)
        monkeypatch.setattr(expedition_flow, "_load_exp_record", store.load)
        monkeypatch.setattr(expedition_flow, "_save_exp_record", store.save)
        return store

    def test_residual_record_confirmed_home_is_cleared(self, monkeypatch):
        now = time.time()
        store = self._patch_store(monkeypatch, {"2": active_record(now)})
        device = type("Maa", (), {"adb_path": "adb", "adb_address": "addr"})()
        monkeypatch.setattr(game_state, "read_events",
                            lambda p, a: [party_event(2, 1, now)])

        assert expedition_flow.reconcile_records_with_game(device) == [2]
        assert store.records == {}

    def test_team_confirmed_away_is_kept(self, monkeypatch):
        now = time.time()
        record = active_record(now)
        store = self._patch_store(monkeypatch, {"2": record})
        device = type("Maa", (), {"adb_path": "adb", "adb_address": "addr"})()
        monkeypatch.setattr(game_state, "read_events",
                            lambda p, a: [party_event(2, 2, now)])

        assert expedition_flow.reconcile_records_with_game(device) == []
        assert store.records == {"2": record}

    def test_no_events_keeps_old_behavior(self, monkeypatch):
        record = active_record(time.time())
        store = self._patch_store(monkeypatch, {"2": record})

        assert expedition_flow.reconcile_records_with_game(_NoMaa()) == []
        assert store.records == {"2": record}

    def test_corrupt_records_do_not_crash(self, monkeypatch):
        store = self._patch_store(monkeypatch, ["not", "a", "dict"])
        device = type("Maa", (), {"adb_path": "adb", "adb_address": "addr"})()
        monkeypatch.setattr(game_state, "read_events",
                            lambda p, a: [party_event(2, 1, time.time())])

        assert expedition_flow.reconcile_records_with_game(device) == []
        assert store.records == ["not", "a", "dict"]

    def test_expired_record_is_not_reconciled(self, monkeypatch):
        # 已到期记录不归对账管（视图本来就不要它），文件保持原样
        now = time.time()
        expired = active_record(now, minutes=0)
        store = self._patch_store(monkeypatch, {"2": expired})
        device = type("Maa", (), {"adb_path": "adb", "adb_address": "addr"})()
        monkeypatch.setattr(game_state, "read_events",
                            lambda p, a: [party_event(2, 1, now)])

        assert expedition_flow.reconcile_records_with_game(device) == []
        assert store.records == {"2": expired}


# ------------------------------------------------- load_active_expeditions

class TestLoadActiveReconcile:
    def _write(self, tmp_path, records):
        path = tmp_path / "expeditions.json"
        path.write_text(json.dumps(records, ensure_ascii=False),
                        encoding="utf-8")
        return path

    def test_game_confirmed_home_is_reconciled_from_view(self, tmp_path):
        records = {"2": active_record(NOW)}
        path = self._write(tmp_path, records)
        parsed = load_active_expeditions(now=NOW, path=path,
                                         events=[party_event(2, 1, NOW)])
        assert 2 not in parsed["active"]
        assert parsed["reconciled"] == [2]
        # 翻篇是正面解决，不是降级理由
        assert parsed["warnings"] == []
        # 本模块只读：文件本身不动
        assert json.loads(path.read_text(encoding="utf-8")) == records

    def test_game_confirmed_away_stays_active(self, tmp_path):
        records = {"2": active_record(NOW)}
        path = self._write(tmp_path, records)
        parsed = load_active_expeditions(now=NOW, path=path,
                                         events=[party_event(2, 2, NOW)])
        assert 2 in parsed["active"]
        assert "reconciled" not in parsed

    def test_unknown_end_reconciled_when_game_confirms_home(self, tmp_path):
        bad_time = dict(active_record(NOW), dispatched_at="不是时间")
        path = self._write(tmp_path, {"2": bad_time})
        parsed = load_active_expeditions(now=NOW, path=path,
                                         events=[party_event(2, 1, NOW)])
        assert parsed["unknown_end"] == []
        assert parsed["reconciled"] == [2]


# ------------------------------------------------------ 日课远征步接入对账

def _daily_host():
    flow = DailyMixin()
    flow.collect_expedition_stream = lambda redispatch=None: iter(())
    dispatched = []
    flow.expedition_stream = lambda **kwargs: (
        dispatched.append(kwargs) or iter(()))
    return flow, dispatched


def _patch_records(monkeypatch, store):
    monkeypatch.setattr(expedition_flow, "_load_exp_record", store.load)
    monkeypatch.setattr(expedition_flow, "_save_exp_record", store.save)


ROUTES = [{"team_no": 2, "map_code": "B2", "map_name": "测试图",
           "era": 2, "map_slot": 2}]


def test_daily_step_reconciles_residual_and_dispatches(monkeypatch):
    now = time.time()
    store = _RecordStore({"2": active_record(now)})
    _patch_records(monkeypatch, store)
    flow, dispatched = _daily_host()
    flow.device = type("Maa", (), {"adb_path": "adb", "adb_address": "addr"})()
    monkeypatch.setattr(game_state, "read_events",
                        lambda p, a: [party_event(2, 1, now)])

    messages = list(flow._daily_expedition_step(list(ROUTES)))

    assert dispatched == [{"era": 2, "map_slot": 2, "team_no": 2}]
    assert store.records == {}
    assert any("以游戏为准继续安排" in m for m in messages)
    assert any("手动收菜" in m for m in messages)


def test_daily_step_keeps_skip_when_game_confirms_away(monkeypatch):
    now = time.time()
    record = active_record(now)
    store = _RecordStore({"2": record})
    _patch_records(monkeypatch, store)
    flow, dispatched = _daily_host()
    flow.device = type("Maa", (), {"adb_path": "adb", "adb_address": "addr"})()
    monkeypatch.setattr(game_state, "read_events",
                        lambda p, a: [party_event(2, 2, now)])

    messages = list(flow._daily_expedition_step(list(ROUTES)))

    assert dispatched == []
    assert any("仍在外面" in m for m in messages)
    assert store.records == {"2": record}


def test_daily_step_without_data_falls_back_to_old_skip(monkeypatch):
    record = active_record(time.time())
    store = _RecordStore({"2": record})
    _patch_records(monkeypatch, store)
    flow, dispatched = _daily_host()
    flow.device = _NoMaa()  # 数据通道不在：回退旧行为

    messages = list(flow._daily_expedition_step(list(ROUTES)))

    assert dispatched == []
    assert any("仍在外面" in m for m in messages)
    assert store.records == {"2": record}
