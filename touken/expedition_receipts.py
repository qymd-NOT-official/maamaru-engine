"""Return posts use the party and sword identities in the collection response."""
import hashlib
import json

from . import sword_db, youzu_log


def _mapping(value):
    return value if isinstance(value, dict) else {}


def build_expedition_receipts(events):
    receipts = []
    for event in events:
        body = event.get('payload')
        if (event.get('endpoint') != '/conquest/complete'
                or event.get('direction') != 'S->C' or event.get('status') != 200
                or not isinstance(body, dict) or str(body.get('status')) != '0'):
            continue
        ts = youzu_log._event_epoch(event)
        team, field = youzu_log._int(body.get('party_no')), youzu_log._int(body.get('field_id'))
        if not ts or team not in range(1, 6) or field not in range(1, 21):
            continue
        party = _mapping(_mapping(body.get('party')).get(str(team)))
        serial = youzu_log._int(_mapping(_mapping(party.get('slot')).get('1')).get('serial_id'))
        swords = _mapping(body.get('sword'))
        sword = _mapping(swords.get(str(serial))) or next((s for s in swords.values()
            if isinstance(s, dict) and youzu_log._int(s.get('serial_id')) == serial), {})
        sid = youzu_log._int(sword.get('sword_id'))
        captain = None
        name = youzu_log._sword_name(sid, sword_db)
        if serial > 0 and sid > 0 and not name.startswith('刀帐'):
            captain = {'serial_id': serial, 'sword_id': sid,
                       'name': name}
        rewards = youzu_log._payload_resource_rewards(body)
        seasonal = youzu_log._payload_resource_rewards({'reward': body.get('season_reward')})
        for name, amount in seasonal.items():
            rewards[name] = rewards.get(name, 0) + amount
        identity = json.dumps([ts, team, field], separators=(',', ':'))
        receipts.append({'ts': ts, 'event_type': 'expedition.settled', 'payload': {
            'team_no': team, 'era': (field - 1) // 4 + 1, 'slot': (field - 1) % 4 + 1,
            'map_code': f"{'ABCDE'[(field - 1) // 4]}{(field - 1) % 4 + 1}",
            'captain': captain, 'rewards': rewards, 'rewards_status': 'confirmed',
            'evidence_source': 'youzu_log', 'endpoint': '/conquest/complete',
            'receipt_key': hashlib.sha256(identity.encode()).hexdigest(),
        }})
    return receipts


def link_execution(receipts, store):
    """Attach current collection facts to their run without inserting a second receipt."""
    run_id, script = store.runtime_context()
    if not run_id or not receipts:
        return
    keys = {receipt['payload']['receipt_key'] for receipt in receipts}
    conn = store._conn()
    with conn:
        for row in conn.execute(
                "SELECT id, run_id, payload FROM events WHERE event_type='expedition.settled' "
                "AND script='youzu_log' AND ts BETWEEN ? AND ?",
                (min(r['ts'] for r in receipts), max(r['ts'] for r in receipts))).fetchall():
            payload = json.loads(row['payload'])
            if payload.get('receipt_key') not in keys or row['run_id'] not in (None, run_id):
                continue
            payload['execution_script'] = script or 'expedition'
            conn.execute("UPDATE events SET run_id=?, payload=? WHERE id=?",
                         (run_id, json.dumps(payload, ensure_ascii=False), row['id']))
