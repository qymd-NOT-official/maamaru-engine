"""Client identity evidence for the visible sword selection list."""
import subprocess

from . import youzu_log, sword_db
from .runtime_paths import DEBUG_DIR


def client_events(maa):
    """Pull observations only; never click or update the roster."""
    try:
        path = youzu_log.pull_log(maa.adb_path, maa.adb_address, DEBUG_DIR)
        return youzu_log.parse_events(path)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return []


def successful_body(event):
    body = event.get('payload')
    return (body if event.get('direction') == 'S->C'
            and event.get('status') == 200 and isinstance(body, dict)
            and str(body.get('status')) == '0' else None)


def team_cleared(events, team, after):
    """Latest fresh party response must explicitly report all six slots empty."""
    for event in reversed(events):
        body = successful_body(event)
        if not body or (youzu_log._event_epoch(event) or 0) < after:
            continue
        endpoint = event.get('endpoint')
        if endpoint not in ('/party/dissolution', '/party/list', '/party/setsword'):
            continue
        parties = body if endpoint == '/party/setsword' else body.get('party')
        party = parties.get(str(team)) if isinstance(parties, dict) else None
        if not isinstance(party, dict):
            continue
        slots = party.get('slot')
        if not isinstance(slots, dict):
            return False
        return all(isinstance(slots.get(str(slot)), dict)
                   and 'serial_id' in slots[str(slot)]
                   and slots[str(slot)]['serial_id'] is None
                   for slot in range(1, 7))
    return False


def identity_target(events, serial, after):
    for event in reversed(events):
        body = successful_body(event)
        if (not body or event.get('endpoint') != '/party/list'
                or (youzu_log._event_epoch(event) or 0) < after):
            continue
        row = (body.get('sword') or {}).get(str(serial))
        if not isinstance(row, dict):
            return None
        found = sword_db.find_game_sword(youzu_log._int(row.get('sword_id')))
        if not found:
            return None
        # The screen includes equipment bonuses. Unknown bonuses must never be
        # compared to raw base values. Levels remain usable on equipped swords.
        geared = any(row.get(key) for key in ('equip_serial_id1', 'equip_serial_id2',
                     'equip_serial_id3', 'horse_serial_id', 'artifact_serial_id1',
                     'artifact_serial_id2'))
        artifact = any(row.get(key) for key in ('artifact_serial_id1', 'artifact_serial_id2'))
        return {'sword_catalog_id': found[0], 'form': found[2],
                'level': youzu_log._int(row.get('level'), None),
                'tou_level': youzu_log._int(row.get('ranbu_level'), None),
                'survival_max': None if artifact else youzu_log._int(row.get('hp_max'), None),
                'recon': None if geared else youzu_log._int(row.get('scout'), None)}
    return None


def selected_serial(events, team, slot, after):
    for event in reversed(events):
        body = successful_body(event)
        if not body or (youzu_log._event_epoch(event) or 0) < after:
            continue
        if event.get('endpoint') not in ('/party/setsword', '/party/list'):
            continue
        parties = body if event.get('endpoint') == '/party/setsword' else body.get('party')
        party = parties.get(str(team)) if isinstance(parties, dict) else None
        member = ((party.get('slot') or {}).get(str(slot))
                  if isinstance(party, dict) else None)
        if isinstance(member, dict):
            return youzu_log._int(member.get('serial_id'), None)
    return None
