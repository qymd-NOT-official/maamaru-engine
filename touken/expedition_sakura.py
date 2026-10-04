"""Exact-member expedition training with a recoverable team-record transaction."""
import json
import copy
import time

from . import sword_db, youzu_log
from .runtime_paths import STATE_DIR
from .selection_identity import client_events, successful_body

PENDING = STATE_DIR / 'expedition_sakura_pending.json'
SHELL = {'team_ui_ocr': {'expected': '部队编成', 'roi': [480, 0, 800, 60]}}


def latest_party(events, after):
    for event in reversed(events):
        body = successful_body(event)
        if (body and event.get('endpoint') == '/party/list'
                and (youzu_log._event_epoch(event) or 0) >= after):
            return body
    return None


def team_snapshot(body, team):
    """Preserve order and equipment kinds; troop instances can be consumed."""
    party = (body.get('party') or {}).get(str(team), {})
    if str(party.get('status')) != '1':
        raise ValueError('未确认部队待命')
    result = {}
    for slot in range(1, 7):
        serial = ((party.get('slot') or {}).get(str(slot)) or {}).get('serial_id')
        row = (body.get('sword') or {}).get(str(serial)) if serial else None
        if serial and not isinstance(row, dict):
            raise ValueError('队员资料不完整')
        gear = {}
        if row:
            for field, source in [('equip_id1', 'equip_serial_id1'),
                                  ('equip_id2', 'equip_serial_id2'),
                                  ('equip_id3', 'equip_serial_id3'),
                                  ('horse_id', 'horse_serial_id')]:
                sid = row.get(source)
                equip = (body.get('equip') or {}).get(str(sid)) if sid else None
                if sid and not isinstance(equip, dict):
                    raise ValueError('装备资料不完整')
                gear[field] = str(equip['equip_id']) if equip else None
            for field in ('item_id', 'artifact_serial_id1', 'artifact_serial_id2'):
                gear[field] = str(row[field]) if row.get(field) else None
        result[str(slot)] = {'serial_id': str(serial) if serial else None, **gear}
    return result


def record_matches(body, snapshot):
    record = ((body.get('preset') or {}).get('1') or {}).get('party') or {}
    # Every non-empty attachment must be represented by the game's record.
    for slot, expected in snapshot.items():
        actual = record.get(slot)
        if not isinstance(actual, dict):
            return False
        for key, value in expected.items():
            if (str(actual[key]) if actual.get(key) else None) != value:
                return False
    return True


def candidates(body, snapshot):
    found = []
    for member in snapshot.values():
        serial = member['serial_id']
        row = (body.get('sword') or {}).get(serial) if serial else None
        if not row or str(row.get('protect')) != '1':
            continue
        level = youzu_log._int(row.get('level'), None)
        fatigue = youzu_log._int(row.get('fatigue'), None)
        sword = sword_db.find_game_sword(youzu_log._int(row.get('sword_id')))
        if level is None or level <= 1 or fatigue is None or not 0 <= fatigue <= 49 or not sword:
            continue
        found.append({'observation_id': f'youzu:{serial}',
                      'sword_catalog_id': sword[0], 'form': sword[2],
                      'name': sword[1].get('name_zh') or sword[1]['name'],
                      'level': level, 'serial_id': serial, 'fatigue': fatigue})
    return sorted(found, key=lambda item: item['fatigue'])


def fresh_body_stream(agent, team, after=None):
    """Use an on-page response first; enter formation directly when needed."""
    from .maa_adapter import roi_4to4
    agent.maa.screenshot(force=True)
    in_place = bool(agent.maa.ocr('部队编成', roi_4to4(480, 0, 800, 60)))
    if in_place:
        agent.current_location = '编队'
        body = latest_party(client_events(agent.maa), after if after is not None else time.time() - 1)
        if body:
            return body
    since = time.time() - 1
    yield from agent.navigate_to_stream('编队')
    if agent.current_location != '编队' or not (yield from agent._select_team_confirmed(team)):
        return None
    return latest_party(client_events(agent.maa), since)


def record_body(agent, body, after):
    """Saving a record returns preset directly; no page round trip needed."""
    result = copy.deepcopy(body)
    for event in client_events(agent.maa):
        payload = successful_body(event)
        if (payload and (youzu_log._event_epoch(event) or 0) >= after
                and event.get('endpoint') in ('/party/set_preset', '/party/list')
                and isinstance(payload.get('preset'), dict)):
            result['preset'] = payload['preset']
    return result


def restore_stream(agent, pending):
    team, snapshot = pending['team_no'], pending['snapshot']
    # Training already saved and checked the backup on this page. Recovery
    # after interruption enters formation once, never detours via home.
    backup = pending.get('record_body')
    body = record_body(agent, backup, pending['started_at']) if backup else (yield from fresh_body_stream(agent, team, after=time.time() - 1))
    try:
        already_restored = not backup and bool(body) and team_snapshot(body, team) == snapshot
    except ValueError:
        already_restored = False
    if not already_restored and (not body or not record_matches(body, snapshot)):
        yield '[远征补花] ✗ 原队伍记录无法核对，请恢复队伍后再续派'
        return False
    restored_since = int(time.time())
    if not already_restored and not agent._load_team_record_confirmed(SHELL, 1):
        yield '[远征补花] ✗ 原队伍未恢复，暂不续派'
        return False
    if not already_restored:
        body = yield from fresh_body_stream(agent, team, after=restored_since)
    try:
        restored = bool(body) and team_snapshot(body, team) == snapshot
    except ValueError:
        restored = False
    if not restored:
        yield '[远征补花] ✗ 队员或装备未恢复齐，暂不续派'
        return False
    # Keep a recovery receipt, rather than deleting player state.
    pending['status'] = 'restored'
    pending.pop('record_body', None)
    PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding='utf-8')
    yield '[远征补花] ✓ 原队伍和装备已核对恢复'
    return True


def pending_restore():
    if not PENDING.exists():
        return None
    try:
        pending = json.loads(PENDING.read_text(encoding='utf-8'))
        return pending if pending.get('status') != 'restored' else None
    except (OSError, ValueError, AttributeError):
        return {'status': 'unreadable'}


def recover_stream(agent):
    pending = pending_restore()
    if not pending:
        return True
    if (pending.get('version') != 1 or pending.get('team_no') not in range(1, 6)
            or not isinstance(pending.get('snapshot'), dict)
            or set(pending['snapshot']) != {str(n) for n in range(1, 7)}
            or any(not isinstance(row, dict) or 'serial_id' not in row
                   for row in pending['snapshot'].values())):
        yield '[远征补花] ✗ 上次恢复记录无法读取，暂不派遣'
        return False
    agent._expedition_sakura_active = True
    try:
        return (yield from restore_stream(agent, pending))
    finally:
        agent._expedition_sakura_active = False


def train_stream(agent, team, repair_threshold='light'):
    if team not in range(1, 6) or repair_threshold not in ('light', 'medium', 'heavy'):
        yield '[远征补花] ✗ 部队或伤势条件无效，未清队'
        return False
    if pending_restore():
        yield '[远征补花] ✗ 上次补花的队伍尚未确认恢复，请先处理，暂不续派'
        return False
    body = yield from fresh_body_stream(agent, team)
    try:
        snapshot = team_snapshot(body, team) if body else None
    except ValueError:
        snapshot = None
    if snapshot is None:
        yield '[远征补花] ✗ 没读到完整的待命队伍，暂不续派'
        return False
    targets = candidates(body, snapshot)
    if not targets:
        yield '[远征补花] 这队没有需要补花的刀，直接派遣'
        return True
    saved_since = int(time.time())
    if not agent._save_team_record(SHELL, 1):
        yield '[远征补花] ✗ 原队伍未保存，未清队'
        return False
    saved = record_body(agent, body, saved_since)
    if not saved or not record_matches(saved, snapshot):
        yield '[远征补花] ✗ 部队记录没保存完整队员和装备，未清队'
        return False
    pending = {'version': 1, 'status': 'training', 'team_no': team,
               'snapshot': snapshot, 'started_at': time.time()}
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding='utf-8')
    agent._expedition_sakura_active = True
    try:
        # In-memory only: do not persist the full client inventory.
        pending['record_body'] = {'preset': saved['preset']}
        ready = yield from agent._prepare_sakura_team(team)
        if ready:
            for target in targets:
                result = yield from agent.ensure_team_member_stream(team, 1, target)
                if result.get('status') in ('ambiguous', 'not_found', 'unavailable'):
                    yield f"[远征补花] {target['name']}没能明确选中，本次跳过"
                    # Return from the selection list before the next target.
                    yield from fresh_body_stream(agent, team)
                    continue
                if result.get('status') not in ('changed', 'already_correct'):
                    ready = False
                    break
                # This exact member has not fought during team preparation;
                # selecting/equipping does not change the client's fatigue.
                fatigue = target['fatigue']
                if fatigue is None or not 0 <= fatigue <= 100:
                    yield '[远征补花] ✗ 选入后未读到疲劳，先恢复队伍，本次不续派'
                    ready = False
                    break
                if fatigue >= 100:
                    continue
                if not (yield from agent._auto_equip(1)):
                    ready = False
                    break
                for _ in range(40):
                    round_started = int(time.time())
                    done = False
                    for message in agent.sortie_stream(
                            chapter=1, map_no=1, team_no=team, auto_march=True,
                            max_loops=1, formation_mode='auto', formation='鱼鳞阵',
                            auto_equip=False, repair_threshold=repair_threshold,
                            injury_action='stop'):
                        yield message
                        done |= message == f'[出阵] ✓ 全部 1 圈跑完，部队{team}辛苦啦，收工！'
                    if not done:
                        ready = False
                        break
                    current = yield from fresh_body_stream(agent, team, after=round_started)
                    captain = (((current or {}).get('party') or {}).get(str(team), {}).get('slot') or {}).get('1') or {}
                    row = ((current or {}).get('sword') or {}).get(target['serial_id']) or {}
                    fatigue = (youzu_log._int(row.get('fatigue'), None)
                               if str(captain.get('serial_id')) == target['serial_id'] else None)
                    if fatigue is None or not 0 <= fatigue <= 100:
                        ready = False
                        break
                    if fatigue >= 100:
                        yield f"[远征补花] {target['name']}已刷到100"
                        # Shared full unload; the original attachments are in the record.
                        ready = yield from agent._sakura_unequip()
                        break
                else:
                    ready = False
                if not ready:
                    break
        restored = yield from restore_stream(agent, pending)
        if not ready:
            yield '[远征补花] ✗ 补花未完成，本次暂不续派'
        return ready and restored
    finally:
        # Generator cancellation never clicks in an unknown/battle screen.
        # The persisted transaction blocks later dispatches until recovery.
        agent._expedition_sakura_active = False
