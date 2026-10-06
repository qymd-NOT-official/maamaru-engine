# -*- coding: utf-8 -*-
"""统一采集管线 touken.record_sync：步骤编排、阅后即焚、启动前钩子容错、
training.captured 幂等入库（全部合成数据）。"""

import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from panel import game_inventory
from touken import record_sync, youzu_log
from touken.flows.daily import DailyMixin
from touken.telemetry import TelemetryStore

NOW = 1780000000  # 固定 now_time，便于断言数据时间

SWORD_FULL = {
    "111": {"serial_id": "111", "sword_id": "118", "level": "99",
            "exp": "123456", "pre_exp": "123000", "ranbu_level": "2",
            "ranbu_exp": "50", "hp": "45", "hp_max": "45", "fatigue": "100",
            "protect": "1", "item_id": "0", "created_at": "2020-01-01 00:00:00",
            "hp_up": "3", "atk_up": "1", "def_up": "0", "mobile_up": "2",
            "back_up": "0", "scout_up": "1", "hide_up": "0"},
    "222": {"serial_id": "222", "sword_id": "5", "level": "35", "exp": "8000",
            "hp_up": "0"},
}


def _s2c(ts, url, payload):
    return (f"【{ts}】【S->C】{url} readyState:4 status:200 "
            f"data:{json.dumps(payload, ensure_ascii=False)}")


def make_log(path, *, pool_endpoint="/party/list", pool_key="sword"):
    """一局合成日志：一次 /home 资源读数 + 一份全量刀池。"""
    lines = [
        _s2c("2026-05-27 12:00:00", "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"resource": {"charcoal": 100, "steel": 200, "coolant": 300,
                           "file": 400, "bill": 5},
              "currency": {"money": "999"}, "status": 0, "now_time": NOW}),
        _s2c("2026-05-27 12:00:01",
             f"https://s39-ios-djlw.youzu.com{pool_endpoint}?uid=1",
             {pool_key: dict(SWORD_FULL),
              "party": {"1": {"party_no": "1", "status": "1",
                              "party_name": "主力",
                              "slot": {"1": {"serial_id": "111"}},
                              "finished_at": None}},
              "status": 0, "now_time": NOW}),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """真实 parse/build/write 全链路，store/状态文件/状态目录全部落到 tmp。"""
    store = TelemetryStore(tmp_path / "telemetry.db")
    monkeypatch.setattr(record_sync, "get_telemetry_store", lambda: store)
    monkeypatch.setattr(record_sync, "STATUS_DIR", tmp_path)
    monkeypatch.setattr(game_inventory, "STATUS_DIR", tmp_path)
    monkeypatch.setattr(youzu_log, "_ledger_state_path",
                        lambda: tmp_path / "youzu_ledger_state.json")
    return store, tmp_path


def test_collect_full_pipeline_burns_and_counts(monkeypatch, harness):
    store, tmp_path = harness
    make_log(tmp_path / "source.log")
    monkeypatch.setattr(youzu_log, "pull_log",
                        Mock(side_effect=lambda *a, **k: make_log(tmp_path / "pulled.log")))
    consumer = Mock()
    result = record_sync.collect_game_records("adb", "addr",
                                              extra_consumers=(consumer,))
    assert result["observations_written"] == 1
    assert result["changes_written"] == 0
    assert result["receipts"] == {"written": 0, "reconciled": 0}
    assert result["training"] == {"written": 1}
    assert result["resources"]["木炭"] == 100
    assert result["resources"]["小判"] == 999
    consumer.assert_called_once()
    assert consumer.call_args.args[0][-1]["direction"] == "META"
    assert not (tmp_path / "pulled.log").exists()  # 阅后即焚
    assert (tmp_path / "youzu_home_situation.json").exists()
    # 账房观察链与练度快照都进了库
    assert len(store.recent_events(event_type="inventory.captured",
                                   script="youzu_log")) == 1
    rows = store.recent_events(event_type="training.captured")
    assert len(rows) == 1
    assert rows[0]["ts"] == NOW  # 数据时间来自日志事件，不是 pull 时间


def test_collect_is_idempotent_across_repulls(monkeypatch, harness):
    store, tmp_path = harness
    make_log(tmp_path / "source.log")
    monkeypatch.setattr(youzu_log, "pull_log",
                        Mock(side_effect=lambda *a, **k: make_log(tmp_path / "pulled.log")))
    first = record_sync.collect_game_records("adb", "addr")
    second = record_sync.collect_game_records("adb", "addr")
    assert first["training"] == {"written": 1}
    assert second["training"] == {"written": 0}
    assert second["observations_written"] == 0  # last_ts 去重
    assert len(store.recent_events(event_type="training.captured")) == 1
    assert len(store.recent_events(event_type="inventory.captured",
                                   script="youzu_log")) == 1


def test_collect_burns_log_even_when_parse_fails(monkeypatch, harness):
    store, tmp_path = harness
    pulled = tmp_path / "pulled.log"
    pulled.write_text("这不是日志", encoding="utf-8")
    monkeypatch.setattr(youzu_log, "pull_log", Mock(return_value=pulled))
    monkeypatch.setattr(youzu_log, "parse_events",
                        Mock(side_effect=ValueError("bad log")))
    with pytest.raises(ValueError):
        record_sync.collect_game_records("adb", "addr")
    assert not pulled.exists()


def test_collect_without_sword_pool_skips_training(monkeypatch, harness):
    store, tmp_path = harness
    pulled = tmp_path / "pulled.log"
    pulled.write_text(
        _s2c("2026-05-27 12:00:00",
             "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"resource": {"charcoal": 1}, "status": 0, "now_time": NOW}),
        encoding="utf-8")
    monkeypatch.setattr(youzu_log, "pull_log", Mock(return_value=pulled))
    result = record_sync.collect_game_records("adb", "addr")
    assert result["training"] == {"written": 0}
    assert store.recent_events(event_type="training.captured") == []


# ---------------------------------------------------------------- training 快照

def _events_with_pool(tmp_path, **kwargs):
    return youzu_log.parse_events(make_log(tmp_path / "log.txt", **kwargs))


@pytest.mark.parametrize("endpoint,key", [("/party/list", "sword"),
                                           ("/sally", "sword_all")])
def test_training_snapshot_payload_from_event_time(tmp_path, endpoint, key):
    store = TelemetryStore(tmp_path / "t.db")
    events = _events_with_pool(tmp_path, pool_endpoint=endpoint, pool_key=key)
    assert youzu_log.save_training_snapshot(store, events) == {"written": 1}
    assert youzu_log.save_training_snapshot(store, events) == {"written": 0}
    rows = store.recent_events(event_type="training.captured")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["captured_at"] == datetime.fromtimestamp(NOW).strftime(
        "%Y-%m-%d %H:%M:%S")
    assert payload["source"] == "youzu_log"
    assert "name" not in json.dumps(payload, ensure_ascii=False)  # 刀名不入库
    swords = {s["serial_id"]: s for s in payload["swords"]}
    assert swords[111] == {"serial_id": 111, "sword_id": 118, "level": 99,
                           "exp": 123456, "ranbu_level": 2, "ranbu_exp": 50,
                           "hp_up": 3, "atk_up": 1, "def_up": 0, "mobile_up": 2,
                           "back_up": 0, "scout_up": 1, "hide_up": 0}
    # 内番没喂过的字段缺省保持 None，不猜 0
    assert swords[222] == {"serial_id": 222, "sword_id": 5, "level": 35,
                           "exp": 8000, "ranbu_level": 0, "ranbu_exp": 0,
                           "hp_up": 0, "atk_up": None, "def_up": None,
                           "mobile_up": None, "back_up": None, "scout_up": None,
                           "hide_up": None}


def test_training_snapshot_absent_pool(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    log = tmp_path / "log.txt"
    log.write_text(
        _s2c("2026-05-27 12:00:00",
             "https://s39-ios-djlw.youzu.com/home?uid=1",
             {"resource": {"charcoal": 1}, "status": 0, "now_time": NOW}),
        encoding="utf-8")
    events = youzu_log.parse_events(log)
    assert youzu_log.build_training_snapshot(events) is None
    assert youzu_log.save_training_snapshot(store, events) == {"written": 0}


# ---------------------------------------------------------------- panel 两处接线

def test_game_inventory_wrapper_runs_pipeline_with_consumer(monkeypatch, harness):
    store, tmp_path = harness
    make_log(tmp_path / "source.log")
    monkeypatch.setattr(youzu_log, "pull_log",
                        Mock(side_effect=lambda *a, **k: make_log(tmp_path / "pulled.log")))
    result = game_inventory.sync_game_records("adb", "addr")
    assert result["training"] == {"written": 1}
    assert result["resources"]["小判"] == 999
    assert not (tmp_path / "pulled.log").exists()
    # 远征观察消费方确实挂上了（party/list 待命证据写盘）
    assert (tmp_path / "youzu_expedition_observations.json").exists()


def test_daily_ledger_sync_wires_pipeline_and_consumer(monkeypatch, harness):
    store, tmp_path = harness
    import touken.runtime_paths
    monkeypatch.setattr(touken.runtime_paths, "STATUS_DIR", tmp_path)
    make_log(tmp_path / "source.log")
    monkeypatch.setattr(youzu_log, "pull_log",
                        Mock(side_effect=lambda *a, **k: make_log(tmp_path / "pulled.log")))
    from panel import server
    run = server._workflow.NODE_REGISTRY["ledger_sync"]["run"]
    agent = Mock()
    agent.maa.adb_path = "adb"
    agent.maa.adb_address = "127.0.0.1:16384"
    messages = list(run(agent, {}, None))
    assert any("账本已同步：观察 1 条，收支 0 条" in m for m in messages)
    assert not (tmp_path / "pulled.log").exists()
    assert (tmp_path / "youzu_expedition_observations.json").exists()
    assert len(store.recent_events(event_type="training.captured")) == 1


# ---------------------------------------------------------------- 启动前收账钩子

def _boot_flow():
    """带假 maa 的日课流：resolve/am start 都走脚本化的 _adb_run。"""
    flow = DailyMixin.__new__(DailyMixin)
    flow.config = {"daily": {"logout": {"package": "com.youzu.djlw"}}}
    flow.recorded = []
    flow.dir_checks = 0
    flow.adb_calls = []

    class Maa:
        adb_path = "adb"
        adb_address = "127.0.0.1:16384"

        def _adb_run(self, args, timeout=None):
            flow.adb_calls.append(args)
            if args[:2] == ["shell", "cmd"]:
                return b"com.youzu.djlw/.MainActivity\n"
            return b"Starting: Intent{}\n"

        def screenshot(self, force=False):
            pass

        def exists(self, template, roi=None, threshold=0.7):
            if template == "目录.png":
                flow.dir_checks += 1
                return flow.dir_checks >= 3  # 前两次是启动前的本丸/登录检查
            return False

        def template_match(self, template, roi=None, threshold=0.7):
            return None

        def ocr(self, expected, roi, match_mode="exact"):
            return None

    flow.maa = Maa()
    flow.record_event = lambda event_type, **payload: flow.recorded.append(
        (event_type, payload))
    return flow


def test_launch_collects_records_before_am_start(monkeypatch):
    flow = _boot_flow()
    result = {"observations_written": 2, "changes_written": 7,
              "receipts": {"written": 1, "reconciled": 0},
              "training": {"written": 1}}
    collect = Mock(return_value=result)
    monkeypatch.setattr("touken.flows.daily.collect_game_records", collect)
    assert flow._launch_game_via_adb() is True
    collect.assert_called_once_with("adb", "127.0.0.1:16384")
    assert flow._records_precollected == result
    # resolve → 收账 → am start：启动命令确实在收账之后发出
    assert flow.adb_calls[-1][:3] == ["shell", "am", "start"]
    assert flow.recorded == [("youzu_log.precollected", {
        "observations_written": 2, "changes_written": 7,
        "receipts_written": 1, "training_written": 1})]


def test_launch_collect_failure_still_starts_silently(monkeypatch):
    flow = _boot_flow()
    collect = Mock(side_effect=RuntimeError("adb 不通"))
    monkeypatch.setattr("touken.flows.daily.collect_game_records", collect)
    assert flow._launch_game_via_adb() is True
    assert flow.adb_calls[-1][:3] == ["shell", "am", "start"]
    assert flow._records_precollected is None
    assert flow.recorded == []  # 失败无痕，不炸不播


def test_launch_skips_collect_when_component_unresolved(monkeypatch):
    flow = _boot_flow()

    class BrokenMaa(flow.maa.__class__):
        def _adb_run(self, args, timeout=None):
            flow.adb_calls.append(args)
            return b"Error: unable to resolve activity\n"

    flow.maa = BrokenMaa()
    collect = Mock()
    monkeypatch.setattr("touken.flows.daily.collect_game_records", collect)
    assert flow._launch_game_via_adb() is False
    collect.assert_not_called()


def test_ensure_game_started_broadcasts_precollection(monkeypatch):
    flow = _boot_flow()
    collect = Mock(return_value={"observations_written": 2, "changes_written": 7,
                                 "receipts": {"written": 0, "reconciled": 0},
                                 "training": {"written": 0}})
    monkeypatch.setattr("touken.flows.daily.collect_game_records", collect)
    with patch("touken.flows.daily.time.sleep"):
        messages = list(flow._ensure_game_started())
    assert any("启动前已收走上一局游戏记录：观察 2 条，收支 7 条" in m
               for m in messages)


def test_ensure_game_started_silent_when_precollection_fails(monkeypatch):
    flow = _boot_flow()
    collect = Mock(side_effect=RuntimeError("adb 不通"))
    monkeypatch.setattr("touken.flows.daily.collect_game_records", collect)
    with patch("touken.flows.daily.time.sleep"):
        messages = list(flow._ensure_game_started())
    assert any("已绕过 MuMu 桌面遮挡" in m for m in messages)
    assert not any("收走上一局" in m for m in messages)
