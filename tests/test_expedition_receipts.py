import json

from touken.expedition_receipts import build_expedition_receipts
from touken.sword_receipts import write_receipts
from touken.telemetry import TelemetryStore


def test_link_current_execution_keeps_one_authoritative_receipt(tmp_path, monkeypatch):
    from touken.expedition_receipts import link_execution
    store = TelemetryStore(tmp_path / 'events.db')
    receipt, = build_expedition_receipts([response()])
    write_receipts(store, [receipt])
    monkeypatch.setenv('MAAMARU_RUN_ID', 'collection-now')
    monkeypatch.setenv('MAAMARU_SCRIPT', 'expedition_collect')
    link_execution([receipt], store)
    link_execution([receipt], store)
    rows = store._conn().execute("SELECT run_id, script, payload FROM events WHERE event_type='expedition.settled'").fetchall()
    assert len(rows) == 1
    assert rows[0]['run_id'] == 'collection-now'
    assert rows[0]['script'] == 'youzu_log'
    assert json.loads(rows[0]['payload'])['execution_script'] == 'expedition_collect'
    monkeypatch.setenv('MAAMARU_RUN_ID', 'another-run')
    link_execution([receipt], store)
    assert store._conn().execute("SELECT run_id FROM events WHERE event_type='expedition.settled'").fetchone()[0] == 'collection-now'
    store.close()


def response():
    return {'endpoint': '/conquest/complete', 'direction': 'S->C', 'status': 200,
            'ts': '2026-10-03 17:10:09', 'payload': {
                'status': 0, 'party_no': '5', 'field_id': '12',
                'party': {'5': {'slot': {'1': {'serial_id': '20222034'}}}},
                'sword': {'20222034': {'serial_id': '20222034', 'sword_id': '149'}},
                'reward': [{'item_type': '5', 'item_id': '3', 'item_num': 300},
                           {'item_type': '5', 'item_id': '5', 'item_num': 750}],
            }}


def test_captain_and_rewards_are_from_collection_response():
    receipt, = build_expedition_receipts([response()])
    p = receipt['payload']
    assert p['captain'] == {'serial_id': 20222034, 'sword_id': 149, 'name': '小豆长光·极'}
    assert p['map_code'] == 'C4'
    assert p['rewards'] == {'玉钢': 300, '砥石': 750}


def test_missing_identity_does_not_use_other_team_or_old_roster():
    event = response()
    event['payload']['party'] = {'4': event['payload']['party']['5']}
    assert build_expedition_receipts([event])[0]['payload']['captain'] is None
    event['payload']['party'] = []
    event['payload']['sword'] = []
    assert build_expedition_receipts([event])[0]['payload']['captain'] is None
    event['payload']['status'] = 1
    assert build_expedition_receipts([event]) == []


def test_duplicate_logs_and_ocr_reconciliation(tmp_path):
    receipt, = build_expedition_receipts([response()])
    store = TelemetryStore(tmp_path / 'events.db')
    old = {'team_no': 5, 'era': 3, 'slot': 4, 'rewards': {'砥石': 700}}
    conn = store._conn()
    conn.execute("INSERT INTO events(ts,run_id,script,event_type,payload) VALUES (?,NULL,'expedition','expedition.settled',?)",
                 (receipt['ts'] + 2, json.dumps(old)))
    conn.commit()
    assert write_receipts(store, [receipt]) == {'written': 0, 'reconciled': 1}
    assert write_receipts(store, [receipt, receipt]) == {'written': 0, 'reconciled': 0}
    rows = store.recent_events()
    assert len(rows) == 1
    assert rows[0]['payload']['ocr_evidence'] == old
    assert rows[0]['payload']['captain']['serial_id'] == 20222034
