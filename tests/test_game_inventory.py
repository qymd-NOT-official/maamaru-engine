"""独立盘点的双路读取、失败和旧快照边界，全部使用合成数据。"""

import json
import time
from unittest.mock import Mock

import pytest

from panel import game_inventory
from touken.flow_control import FlowAborted


RESOURCES = {name: 100 for name in
             ("木炭", "玉钢", "冷却材", "砥石", "甲州金", "委托符", "加速符", "小判")}


def setup_reading(monkeypatch, tmp_path, *, records=None, ocr=None):
    monkeypatch.setattr(game_inventory, "STATUS_DIR", tmp_path)
    sync = Mock(return_value={"resource_facts": {
        name: {"count": value, "observed_at": time.time()}
        for name, value in (records or {}).items()}})
    monkeypatch.setattr(game_inventory, "sync_game_records", sync)
    recorded = Mock()
    monkeypatch.setattr(game_inventory, "record_event", recorded)
    config = tmp_path / "config.json"
    config.write_text('{"adb_address": "emulator-5554"}', encoding="utf-8")

    class Agent:
        def status_snapshot_stream(self):
            if ocr is not None:
                (tmp_path / "inventory.json").write_text(
                    json.dumps({"resources": ocr}), encoding="utf-8")
            yield "画面盘点"

        def navigate_to_stream(self, destination):
            assert destination == "本丸"
            yield "回本丸"

    return config, sync, recorded, lambda _: Agent()


def test_both_readers_run_and_return_confirmed_balances(monkeypatch, tmp_path):
    config, sync, recorded, make_agent = setup_reading(
        monkeypatch, tmp_path, records={"小判": 50}, ocr=RESOURCES)
    messages = list(game_inventory.refresh_game_inventory(str(config), {}, make_agent=make_agent))
    sync.assert_called_once()
    assert "读取完成" in messages[-1]
    assert recorded.call_args.args[1] == {
        "records": "ok", "ocr": "ok", "resources": {**RESOURCES, "小判": 50}}


def test_record_failure_can_complete_with_fresh_ocr(monkeypatch, tmp_path):
    config, sync, recorded, make_agent = setup_reading(monkeypatch, tmp_path, ocr=RESOURCES)
    sync.side_effect = RuntimeError("not connected")
    list(game_inventory.refresh_game_inventory(config, {}, make_agent=make_agent))
    result = recorded.call_args.args[1]
    assert result["records"] == "failed"
    assert result["ocr"] == "ok"
    assert result["resources"] == RESOURCES


def test_complete_json_needs_no_screen_reader(monkeypatch, tmp_path):
    config, _, recorded, _ = setup_reading(monkeypatch, tmp_path, records=RESOURCES)
    make_agent = Mock(side_effect=RuntimeError("no screen"))
    list(game_inventory.refresh_game_inventory(config, {}, make_agent=make_agent))
    make_agent.assert_not_called()
    assert recorded.call_args.args[1]["ocr"] == "skipped"


def test_stale_json_is_filled_by_current_screen_read(monkeypatch, tmp_path):
    config, sync, recorded, make_agent = setup_reading(monkeypatch, tmp_path, ocr=RESOURCES)
    sync.return_value = {"resource_facts": {
        name: {"count": 999, "observed_at": time.time() - 700} for name in RESOURCES}}
    list(game_inventory.refresh_game_inventory(config, {}, make_agent=make_agent))
    assert recorded.call_args.args[1]["resources"] == RESOURCES


def test_old_snapshot_cannot_fill_missing_json(monkeypatch, tmp_path):
    config, _, recorded, make_agent = setup_reading(monkeypatch, tmp_path, records={"小判": 50})
    (tmp_path / "inventory.json").write_text(json.dumps({"resources": RESOURCES}), encoding="utf-8")
    with pytest.raises(FlowAborted):
        list(game_inventory.refresh_game_inventory(config, {}, make_agent=make_agent))
    assert recorded.call_args.args[1]["resources"] == {"小判": 50}


def test_partial_ocr_preserves_known_records_and_reports_partial(monkeypatch, tmp_path):
    config, _, recorded, make_agent = setup_reading(
        monkeypatch, tmp_path, records={"小判": 50}, ocr={"木炭": 100, "小判": None})
    with pytest.raises(FlowAborted):
        list(game_inventory.refresh_game_inventory(config, {}, make_agent=make_agent))
    result = recorded.call_args.args[1]
    assert result["ocr"] == "partial"
    assert result["resources"] == {"小判": 50, "木炭": 100}


def test_agent_start_failure_still_keeps_records(monkeypatch, tmp_path):
    config, _, recorded, _ = setup_reading(monkeypatch, tmp_path, records={"小判": 50})
    with pytest.raises(FlowAborted):
        list(game_inventory.refresh_game_inventory(
            config, {}, make_agent=Mock(side_effect=RuntimeError("no emulator"))))
    assert recorded.call_args.args[1]["resources"] == {"小判": 50}


def test_raw_log_removed_even_when_parse_fails(monkeypatch, tmp_path):
    from touken import youzu_log
    path = tmp_path / "raw.txt"
    path.write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(youzu_log, "pull_log", Mock(return_value=path))
    monkeypatch.setattr(youzu_log, "parse_events", Mock(side_effect=ValueError("bad log")))
    with pytest.raises(ValueError):
        game_inventory.sync_game_records("adb", "address")
    assert not path.exists()
