from touken.game_sword_archive import archive_path, candidate_pool, read_archive, sync_archive, update_archive
from touken.honmaru_profile import build_candidate_pool
from touken.sword_archive import build_sword_archive
from touken.telemetry import TelemetryStore
import pytest


def event(endpoint, payload, minute=0, status=0):
    return {"direction": "S->C", "endpoint": endpoint, "status": 200,
            "ts": f"2026-10-01 12:{minute:02d}:00", "payload": {**payload, "status": status}}


def sword(serial, sid=3, level=10):
    return {"serial_id": serial, "sword_id": sid, "level": level, "hp": 30, "hp_max": 60,
            "fatigue": 55, "protect": 1, "created_at": "2023-01-27 17:19:10", "ranbu_level": 2,
            "atk": 40, "equip_serial_id1": 500, "session": "never-store-this"}


def full(*rows, minute=0):
    return event("/party/list", {"sword": {str(r["serial_id"]): r for r in rows}}, minute)


def test_partial_only_never_creates_inventory_or_removes_members():
    partial = event("/sally", {"sword_all": {"1": sword(1, level=99)}}, 1)
    assert update_archive([partial]) is None
    state = update_archive([full(sword(1), sword(2)), partial])
    assert len(state["swords"]) == 2
    assert state["swords"]["1"]["level"] == 99
    assert state["swords"]["2"]["level"] == 10
    unknown = event("/sally", {"sword_all": {"9": sword(9)}}, 2)
    assert update_archive([unknown], state) == state


def test_battle_updates_exact_serial_and_old_log_cannot_revert():
    battle = event("/battle/battle", {"result": {"player": {"party": {"slot": {
        "1": {"serial_id": 2, "sword_id": 3, "hp": 20, "hp_max": 60, "fatigue": 40, "level": 11}}}}}}, 2)
    state = update_archive([full(sword(1), sword(2)), battle])
    assert state["swords"]["2"]["hp"] == 20
    assert state["swords"]["1"]["hp"] == 30
    assert state["swords"]["2"]["created_at"] == "2023-01-27 17:19:10"
    assert update_archive([full(sword(1), sword(2))], state) == state
    # 新完整名单确认少了一振，后续旧的局部记录不能把它复活。
    newer = update_archive([full(sword(1), minute=3), battle], state)
    assert set(newer["swords"]) == {"1"}


def test_bad_or_failed_full_response_preserves_good_snapshot():
    state = update_archive([full(sword(1))])
    invalid = full(sword(1), minute=2)
    invalid["payload"]["sword"]["broken"] = {"serial_id": 1, "sword_id": 3}
    assert update_archive([invalid, event("/party/list", {"sword": {}}, 3, status=1)], state) == state


def test_persistence_backup_idempotence_and_credentials_are_excluded(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    assert sync_archive([full(sword(1))], store)
    old = archive_path(store).read_bytes()
    assert b"never-store-this" not in old
    assert not sync_archive([full(sword(1))], store)
    assert sync_archive([full(sword(1, level=20), minute=2)], store)
    assert archive_path(store).with_suffix(".json.bak").read_bytes() == old
    # 回滚只读旧快照，原 OCR 数据与标注均无迁移/覆盖。
    archive_path(store).write_bytes(old)
    assert candidate_pool(store)["entries"][0]["level"] == 10


def test_json_and_ocr_switch_only_complete_current_pool_without_overwriting_sources(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    sync_archive([full(sword(1))], store)
    original = archive_path(store).read_bytes()
    ts = candidate_pool(store)["observed_at"]
    row = {"sword_id": "touken_003_mikazuki_munechika", "name_zh": "三日月宗近", "level": 20,
           "kiwame_date": "2023-1-27"}
    # 更新但不完整、或者只是图鉴扫描，不能顶掉游戏所持名单。
    store.save_sword_snapshot([row], owned=2, missing=1, captured_at=ts + 60, source="owned_inventory")
    store.save_sword_snapshot([row], owned=1, missing=0, captured_at=ts + 61, source="album")
    assert build_candidate_pool(store)["source"]["kind"] == "youzu_log"
    snapshot = store.save_sword_snapshot([row], owned=1, missing=0, captured_at=ts + 120, source="owned_inventory")
    assert build_candidate_pool(store)["source"]["snapshot_id"] == snapshot
    assert archive_path(store).read_bytes() == original
    # JSON 重读旧记录不会把更新的完整截图名单冲回去。
    sync_archive([full(sword(1))], store)
    assert build_candidate_pool(store)["source"]["snapshot_id"] == snapshot
    sync_archive([full(sword(1, level=30), minute=3)], store)
    assert build_candidate_pool(store)["source"]["kind"] == "youzu_log"
    assert build_candidate_pool(store)["entries"][0]["level"] == 30
    assert store.sword_snapshot_detail(snapshot)["swords"][0]["level"] == 20


def test_serial_annotation_stays_pending_after_newer_ocr_not_falsely_historical(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    catalog = "touken_003_mikazuki_munechika"
    row = {"sword_id": catalog, "name_zh": "三日月宗近", "level": 10, "kiwame_date": "2023-1-27"}
    store.save_sword_snapshot([row], owned=1, missing=0, captured_at=1, source="owned_inventory")
    sync_archive([full(sword(1))], store)
    annotation = store.save_sword_annotation(catalog, "2023-1-27", serial_id=1, watch=True)
    ts = candidate_pool(store)["observed_at"]
    store.save_sword_snapshot([row], owned=1, missing=0, captured_at=ts + 120, source="owned_inventory")
    archive = build_sword_archive(store)
    assert not archive["historical_annotations"]
    assert any(item.get("annotation_id") == annotation["id"] for item in archive["attention"])
    assert archive["entries"][0]["human"] is None  # 没有独立编号，不凭同名套标记。
    assert store.sword_annotations()[0]["watch"]
    sync_archive([full(sword(1), minute=3)], store)
    assert build_sword_archive(store)["entries"][0]["human"]["watch"]


def test_migration_fallback_and_manual_annotations_survive(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    catalog = "touken_003_mikazuki_munechika"
    snapshot = store.save_sword_snapshot([
        {"sword_id": catalog, "name_zh": "三日月宗近", "level": 10, "page_no": 1,
         "kiwame_date": "2023-1-27"}], owned=1, capacity=300, missing=0,
        captured_at=1, source="owned_inventory")
    store.save_sword_annotation(catalog, "2023-1-27", favorite=True, note="保留这振")
    assert build_candidate_pool(store)["source"]["snapshot_id"] == snapshot
    sync_archive([full(sword(1, sid=4, level=95))], store)
    archive = build_sword_archive(store)
    entry = archive["entries"][0]
    assert archive["data_source"] == "youzu_log"
    assert entry["form_status"] == "kiwame"
    assert entry["human"]["favorite"] and entry["human"]["note"] == "保留这振"
    assert entry["level"] == 95 and entry["observation_id"] == "youzu:1"
    assert entry["kiwame_date"] == "2023-1-27"
    assert store.sword_snapshot_detail(snapshot)["swords"][0]["level"] == 10
    archive_path(store).write_text('{"schema":999}', encoding="utf-8")
    assert build_candidate_pool(store)["source"]["snapshot_id"] == snapshot


def test_same_name_same_day_instances_remain_distinct_and_annotations_ambiguous(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    sync_archive([full(sword(1), sword(2))], store)
    store.save_sword_annotation("touken_003_mikazuki_munechika", "2023-1-27", keeper=True)
    archive = build_sword_archive(store)
    assert len({e["observation_id"] for e in archive["entries"]}) == 2
    assert all(e["human"]["stale"] for e in archive["entries"])
    assert candidate_pool(store)["entries"][0]["equipment_serials"]["equip_serial_id1"] == 500


def test_genji_special_stages_are_not_kiwame(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    sync_archive([full(*(sword(sid, sid=sid) for sid in (108, 109, 110, 111, 113, 114, 115)))], store)
    entries = {e["serial_id"]: e for e in candidate_pool(store)["entries"]}
    assert entries[111]["name_zh"] == "髭切"
    assert entries[115]["name_zh"] == "膝丸"
    assert all(entries[sid]["form_status"] == "normal" for sid in (108, 109, 110, 113, 114))
    assert all(entries[sid]["form_status"] == "kiwame" for sid in (111, 115))


def test_serial_annotations_distinguish_same_day_and_never_follow_replacement(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    catalog = "touken_003_mikazuki_munechika"
    sync_archive([full(sword(1), sword(2))], store)
    first = store.save_sword_annotation(catalog, "2023-1-27", serial_id=1, keeper=True)
    second = store.save_sword_annotation(catalog, "2023-1-27", serial_id=2, watch=True)
    assert first["id"] != second["id"]
    entries = {e["serial_id"]: e for e in build_sword_archive(store)["entries"]}
    assert entries[1]["human"]["keeper"] and not entries[1]["human"]["watch"]
    assert entries[2]["human"]["watch"] and not entries[2]["human"]["keeper"]
    assert not entries[1]["human"]["stale"]
    sync_archive([full(sword(2), sword(3), minute=3)], store)
    archive = build_sword_archive(store)
    assert [a["annotation_id"] for a in archive["historical_annotations"]] == [first["id"]]
    assert next(e for e in archive["entries"] if e["serial_id"] == 3)["human"] is None


def test_unique_legacy_annotation_binds_once_and_survives_form_and_date_change(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    catalog = "touken_003_mikazuki_munechika"
    old = store.save_sword_annotation(catalog, "2023-1-27", favorite=True)
    sync_archive([full(sword(1))], store)
    build_sword_archive(store)
    assert store.sword_annotations()[0]["serial_id"] == 1
    changed = sword(1, sid=4)
    changed["created_at"] = "2023-01-28 00:00:00"
    sync_archive([full(changed, minute=3)], store)
    entry = build_sword_archive(store)["entries"][0]
    assert entry["human"]["id"] == old["id"] and entry["human"]["favorite"]


def test_game_sword_types_match_player_filters(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    sync_archive([full(sword(1, sid=99), sword(2, sid=65))], store)
    assert {e["sword_type"] for e in build_sword_archive(store)["entries"]} == {"胁差", "枪"}


def consumption(endpoint, materials, base=1, minute=2, status=0):
    fields = {
        "/composition/compose": {"base_id": str(base), "material_id": materials},
        "/composition/union": {"base_serial_id": str(base), "material_serial_id": materials},
        "/sword/dismantle_many": {"serial_ids": materials},
    }
    response = event(endpoint, {} if endpoint.startswith('/sword/') else
                     {"sword": {**sword(base, level=22), "ranbu_level": 5, "atk": 70}}, minute, status)
    request = {**response, "direction": "C->S", "payload": {**fields[endpoint], "t": "never-store-this"}}
    return [request, response]


@pytest.mark.parametrize('endpoint,label', [('/composition/compose', '链结'),
                         ('/composition/union', '习合'), ('/sword/dismantle_many', '刀解')])
def test_successful_consumption_removes_exact_instances_and_is_idempotent(tmp_path, endpoint, label):
    store = TelemetryStore(tmp_path / 'telemetry.db')
    sync_archive([full(sword(1), sword(2), sword(3), sword(4))], store)
    store.save_sword_annotation('touken_003_mikazuki_munechika', '2023-1-27', serial_id=2, keeper=True)
    events = consumption(endpoint, '2,3')
    assert sync_archive(events, store)
    state = read_archive(store)
    assert set(state['swords']) == {'1', '4'}
    assert state['departures']['2']['reason'] == label
    assert 'never-store-this' not in archive_path(store).read_text(encoding='utf-8')
    if endpoint != '/sword/dismantle_many':
        assert state['swords']['1']['ranbu_level'] == 5
        assert state['swords']['1']['atk'] == 70
    assert not sync_archive(events, store)
    assert build_sword_archive(store)['historical_annotations'][0]['departure_reason'] == label
    # 同一旧完整名单不能复活材料；备份能恢复同步前的档案。
    assert set(update_archive([full(sword(1), sword(2), sword(3), sword(4))], state)['swords']) == {'1', '4'}
    archive_path(store).write_bytes(archive_path(store).with_suffix('.json.bak').read_bytes())
    assert set(read_archive(store)['swords']) == {'1', '2', '3', '4'}


@pytest.mark.parametrize('mutation', ['failed', 'missing_request', 'wrong_base', 'bad_ids', 'base_as_material', 'missing_status'])
def test_unconfirmed_consumption_never_removes_swords(mutation):
    state = update_archive([full(sword(1), sword(2))])
    events = consumption('/composition/compose', '2')
    if mutation == 'failed': events[1]['payload']['status'] = 1
    if mutation == 'missing_request': events = events[1:]
    if mutation == 'wrong_base': events[1]['payload']['sword']['serial_id'] = 9
    if mutation == 'bad_ids': events[0]['payload']['material_id'] = '2,invalid'
    if mutation == 'base_as_material': events[0]['payload']['material_id'] = '1'
    if mutation == 'missing_status': events[1]['payload'].pop('status')
    assert update_archive(events, state) == state


def test_old_consumption_does_not_remove_sword_seen_in_newer_full_inventory():
    state = update_archive([full(sword(1), sword(2), minute=5)])
    updated = update_archive(consumption('/composition/union', '2', minute=2), state)
    assert set(updated['swords']) == {'1', '2'}
    assert updated['swords']['1']['ranbu_level'] == 2


# ---------------------------------------------------------------- training 字段扩容


def test_training_fields_are_archived_and_passed_through(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    row = sword(1)
    row.update({"exp": 123456, "ranbu_exp": 2300, "hp_up": 1, "atk_up": 2,
                "def_up": 3, "mobile_up": 4, "back_up": 5, "scout_up": 6,
                "hide_up": 7, "session": "never-store-this"})
    sync_archive([full(row)], store)
    archived = read_archive(store)["swords"]["1"]
    assert archived["exp"] == 123456 and archived["ranbu_exp"] == 2300
    assert archived["hide_up"] == 7
    assert "session" not in archived
    entry = candidate_pool(store)["entries"][0]
    for key in ("exp", "ranbu_exp", "hp_up", "atk_up", "def_up",
                "mobile_up", "back_up", "scout_up", "hide_up"):
        assert entry[key] == archived[key]
    merged = build_sword_archive(store)["entries"][0]
    for key in ("exp", "ranbu_exp", "hp_up", "atk_up", "def_up",
                "mobile_up", "back_up", "scout_up", "hide_up"):
        assert merged[key] == archived[key]


def test_newer_partial_update_wins_on_training_fields(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    sync_archive([full(sword(1))], store)
    newer = event("/sally", {"sword_all": {"1": {**sword(1), "exp": 999999,
                                                 "ranbu_exp": 100}}}, 1)
    sync_archive([newer], store)
    entry = candidate_pool(store)["entries"][0]
    assert entry["exp"] == 999999 and entry["ranbu_exp"] == 100


def test_legacy_rows_without_training_fields_pass_null_not_zero(tmp_path):
    # 旧档案行（白名单扩容前落盘）没有这些字段：透传 null，不编 0。
    store = TelemetryStore(tmp_path / "telemetry.db")
    sync_archive([full(sword(1))], store)
    archived = read_archive(store)["swords"]["1"]
    assert "exp" not in archived and "hp_up" not in archived
    entry = candidate_pool(store)["entries"][0]
    merged = build_sword_archive(store)["entries"][0]
    for target in (entry, merged):
        for key in ("exp", "ranbu_exp", "hp_up", "atk_up", "def_up",
                    "mobile_up", "back_up", "scout_up", "hide_up"):
            assert target[key] is None

