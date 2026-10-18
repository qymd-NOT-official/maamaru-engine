"""Shared game facts: one reader, explicit observation times and operation results."""
from __future__ import annotations

import math
import hashlib
import json
import subprocess
import threading
import time

from . import youzu_log
from .runtime_paths import DEBUG_DIR

_READ_LOCK = threading.Lock()
FACT_MAX_AGE = 600
RESOURCE_NAMES = frozenset(("木炭", "玉钢", "冷却材", "砥石", "甲州金", "委托符", "加速符", "小判"))


def read_events(adb_path, adb_address):
    """Synchronize once per caller; always dispose of the temporary source."""
    with _READ_LOCK:
        path = youzu_log.pull_log(adb_path, adb_address, dest_dir=DEBUG_DIR)
        try:
            return youzu_log.parse_events(path)
        finally:
            path.unlink(missing_ok=True)


def successful_body(event):
    body = event.get("payload")
    return (body if event.get("direction") == "S->C"
            and event.get("status") == 200 and isinstance(body, dict)
            and str(body.get("status")) == "0" else None)


def resource_facts(events):
    """Sparse responses update only fields actually supplied, using data time."""
    facts = {}
    for event in events:
        body = successful_body(event)
        at = youzu_log._event_epoch(event)
        if body is None or at is None or not math.isfinite(at):
            continue
        for name, value in (youzu_log._reading_from_payload(body) or {}).items():
            if (name in RESOURCE_NAMES and value >= 0
                    and at >= facts.get(name, {}).get("observed_at", 0)):
                facts[name] = {"count": value, "observed_at": at}
    return facts


def fresh_resources(facts, *, now=None, max_age=FACT_MAX_AGE):
    now = time.time() if now is None else now
    return {name: fact["count"] for name, fact in facts.items()
            if 0 <= now - fact["observed_at"] <= max_age}


def expedition_started(events, team_no, field_id, after):
    """Return matching success, explicit failure, or None when evidence is absent."""
    matching_request = False
    result = None
    for event in events:
        if event.get("endpoint") != "/conquest/start":
            continue
        at = youzu_log._event_epoch(event)
        if at is None or at < after:
            continue
        payload = event.get("payload")
        if event.get("direction") == "C->S":
            matching_request = (isinstance(payload, dict)
                                and str(payload.get("party_no")) == str(team_no)
                                and str(payload.get("field_id")) == str(field_id))
            continue
        body = successful_body(event)
        if body is None:
            if matching_request and (event.get("status", 0) >= 400
                    or isinstance(payload, dict) and "status" in payload
                    and str(payload["status"]) != "0"):
                result = False
            matching_request = False
            continue
        summary = body.get("summary")
        if isinstance(summary, dict) and summary:
            result = next((event for row in summary.values()
                           if isinstance(row, dict)
                           and str(row.get("party_no")) == str(team_no)
                           and str(row.get("field_id")) == str(field_id)), False)
        elif matching_request:
            result = event
        matching_request = False
    return result


def sword_facts(events):
    """Reduce full snapshots and sparse updates by instance and field, in event order."""
    swords, parties = {}, {}
    hp_times, slot_times, field_times, field_indices = {}, {}, {}, {}
    battle_injuries = {}
    combat_index, combat_at = -1, 0
    battle_index = -1
    for index, event in enumerate(events):
        body = successful_body(event)
        at = youzu_log._event_epoch(event)
        if body is None or at is None:
            continue
        endpoint = event.get("endpoint") or ""
        full = endpoint in youzu_log._SWORD_FULL_ENDPOINTS
        rows = body.get("sword") if "sword" in body else body.get("sword_all")
        # Some updates return a single sword object rather than a keyed pool.
        if isinstance(rows, dict) and "serial_id" in rows:
            rows = {str(rows["serial_id"]): rows}
        if full and at < max((ts for fields in field_times.values() for ts in fields.values()), default=0):
            rows = None
        if isinstance(rows, dict):
            if full:
                swords, hp_times, field_times, field_indices = {}, {}, {}, {}
            for key, row in rows.items():
                if not isinstance(row, dict):
                    continue
                serial = str(row.get("serial_id") or key)
                if not serial.isdigit() or int(serial) <= 0:
                    continue
                evidence = battle_injuries.get(serial)
                if evidence and at >= evidence["at"] and any(
                        field in row and youzu_log._int(row[field], -1) != evidence[field]
                        for field in ("hp", "hp_max")):
                    battle_injuries.pop(serial, None)
                current = swords.setdefault(serial, {})
                times = field_times.setdefault(serial, {})
                for field, value in row.items():
                    if at >= times.get(field, 0):
                        current[field], times[field] = value, at
                        field_indices.setdefault(serial, {})[field] = index
                        if field == "hp":
                            hp_times[serial] = (at, index)
        teams = body if endpoint == "/party/setsword" else body.get("party")
        if full and at < max((ts for slots in slot_times.values() for ts in slots.values()), default=0):
            teams = None
        if isinstance(teams, dict):
            if full:
                parties, slot_times = {}, {}
            for team, row in teams.items():
                if not isinstance(row, dict) or not isinstance(row.get("slot"), dict):
                    continue
                team = str(team)
                if team not in {"1", "2", "3", "4", "5"}:
                    continue
                if full:
                    parties[team], slot_times[team] = {}, {}
                elif team not in parties:
                    continue  # Partial membership cannot invent the rest of a team.
                for slot, member in row["slot"].items():
                    if isinstance(member, dict) and "serial_id" in member:
                        if at >= slot_times[team].get(str(slot), 0):
                            parties[team][str(slot)] = member["serial_id"]
                            slot_times[team][str(slot)] = at
        if endpoint.startswith(("/battle", "/sally/")):
            combat_index = index
            combat_at = max(combat_at, at)
        if endpoint.startswith("/battle"):
            battle_index = index
        # Only our own completed battle results carry injury status. The same
        # field on roster rows has a different meaning and must never be used.
        result = body.get("result") if endpoint.startswith("/battle/") else None
        player = result.get("player") if isinstance(result, dict) else None
        party = player.get("party") if isinstance(player, dict) else None
        slots = party.get("slot") if isinstance(party, dict) else None
        for row in (slots.values() if isinstance(slots, dict) else ()):
            if not isinstance(row, dict):
                continue
            serial = str(row.get("serial_id") or "")
            hp = youzu_log._int(row.get("hp"), -1)
            maximum = youzu_log._int(row.get("hp_max"), 0)
            if not serial.isdigit() or int(serial) <= 0 or not 0 <= hp <= maximum or maximum <= 0:
                continue
            current = swords.setdefault(serial, {})
            times = field_times.setdefault(serial, {})
            if at < times.get("hp", 0):
                continue
            for field in ("hp", "hp_max", "sword_id", "fatigue",
                          "equip_serial_id1", "equip_serial_id2", "equip_serial_id3"):
                if field in row and at >= times.get(field, 0):
                    current[field], times[field] = row[field], at
                    field_indices.setdefault(serial, {})[field] = index
            hp_times[serial] = (at, index)
            battle_injuries[serial] = {"status": row.get("status"), "hp": hp,
                                      "hp_max": maximum, "at": at, "index": index}
        # A complete current battle roster can renew an existing team binding.
        # Never infer a team from a partial result or from sword names.
        if isinstance(slots, dict) and slots:
            roster = {str(slot): str(row.get("serial_id", "")) for slot, row in slots.items()
                      if isinstance(row, dict)}
            matching = [team for team, members in parties.items()
                        if roster == {slot: str(serial) for slot, serial in members.items()
                                      if serial not in (None, "", 0, "0")}]
            if len(matching) == 1:
                for slot in roster:
                    slot_times[matching[0]][slot] = max(at, slot_times[matching[0]].get(slot, 0))
    return {"swords": swords, "parties": parties, "hp_times": hp_times,
            "battle_injuries": battle_injuries,
            "battle_index": battle_index,
            "slot_times": slot_times, "field_times": field_times, "field_indices": field_indices,
            "combat_index": combat_index, "combat_at": combat_at}


def sword_injury(state, serial, *, now=None, max_age=FACT_MAX_AGE):
    """Use observed battle injury codes only for the same instance and HP.

    Codes 0 (healthy), 2 (medium injury), and 3 (heavy injury) have runtime samples.
    Other codes use HP until calibrated; conflicting heavy HP never passes.
    """
    serial = str(serial)
    sword = state["swords"].get(serial, {})
    hp = youzu_log._int(sword.get("hp"), -1)
    maximum = youzu_log._int(sword.get("hp_max"), 0)
    fallback = youzu_log.injury_tier(hp, maximum)
    evidence = (state.get("battle_injuries") or {}).get(serial)
    now = time.time() if now is None else now
    if (not evidence or not 0 <= now - evidence["at"] <= max_age
            or evidence["index"] < state.get("battle_index", state["combat_index"])
            or (hp, maximum) != (evidence["hp"], evidence["hp_max"])):
        return fallback, "hp"
    code = str(evidence["status"])
    if code == "0" and hp == maximum and maximum > 0:
        return None, "battle_status"
    if code == "3":
        return "重伤", "battle_status"
    if code == "2" and 0 < hp < maximum and fallback != "重伤":
        return "中伤", "battle_status"
    return fallback, "hp"


def team_snapshot(events, team_no, *, max_age=FACT_MAX_AGE, now=None):
    """Adapt current client facts to the shared team contract, once per read."""
    from . import sword_db
    from .team_state import TeamMemberSnapshot, TeamSnapshot
    state = sword_facts(events)
    now = time.time() if now is None else now
    slots = state["parties"].get(str(team_no))
    if not slots:
        return None
    members, party_ages = [], []
    for slot, serial in slots.items():
        if serial in (None, "", 0, "0"):
            continue
        if not str(slot).isdigit() or int(slot) not in range(1, 7):
            return None
        serial = str(serial)
        party_at = state["slot_times"][str(team_no)].get(slot)
        if party_at is None or not 0 <= now - party_at <= max_age:
            return None
        row = state["swords"].get(serial, {})

        def current(field):
            at = state["field_times"].get(serial, {}).get(field, 0)
            index = state["field_indices"].get(serial, {}).get(field, -1)
            return (field in row and 0 <= now - at <= max_age
                    and at >= state["combat_at"] and index >= state["combat_index"])

        hp, maximum = youzu_log._int(row.get("hp"), -1), youzu_log._int(row.get("hp_max"), 0)
        hp_at, hp_index = state["hp_times"].get(serial, (None, -1))
        hp_known = (hp_at is not None and hp_index >= state["combat_index"]
                    and hp_at >= state["combat_at"] and 0 <= now - hp_at <= max_age
                    and 0 <= hp <= maximum and maximum > 0)
        injury, source = (sword_injury(state, serial, now=now, max_age=max_age)
                          if hp_known else (None, None))
        raw = row.get("fatigue")
        fatigue = youzu_log._int(raw, -1)
        if (not current("fatigue") or isinstance(raw, bool)
                or isinstance(raw, float) and not raw.is_integer() or not 0 <= fatigue <= 100):
            fatigue = None
        equipped = []
        for number in (1, 2, 3):
            field = f"equip_serial_id{number}"
            value = row.get(field)
            if not current(field) or (value is not None and not str(value).isdigit()):
                equipped = None
                break
            equipped.append(value is not None and youzu_log._int(value, 0) > 0)
        members.append(TeamMemberSnapshot(
            slot=int(slot), serial_id=serial,
            name=youzu_log._sword_name(row.get("sword_id"), sword_db),
            hp=hp if hp_known else None, hp_max=maximum if hp_known else None,
            injury=injury, injury_source=source, hp_observed_at=hp_at if hp_known else None,
            fatigue=fatigue,
            fatigue_observed_at=state["field_times"][serial]["fatigue"] if fatigue is not None else None,
            equipment=tuple(equipped) if equipped is not None else None,
            equipment_observed_at=(min(state["field_times"][serial][f"equip_serial_id{n}"] for n in (1, 2, 3))
                                   if equipped is not None else None)))
        party_ages.append(party_at)
    if not members or len({member.serial_id for member in members}) != len(members):
        return None
    return TeamSnapshot(int(team_no), tuple(members), min(party_ages))


def injury_report(events, team_no, *, max_age=FACT_MAX_AGE, now=None):
    """Compatibility view of the shared, current team observation."""
    snapshot = team_snapshot(events, team_no, max_age=max_age, now=now)
    return snapshot.injury_report() if snapshot is not None else None


def team_maintenance_facts(events, team_no, *, now=None):
    snapshot = team_snapshot(events, team_no, now=now)
    return snapshot.maintenance_facts() if snapshot is not None else None


def expedition_settlements(events, after, exclude=()):
    from .expedition_receipts import build_expedition_receipts
    return filter_settlements(build_expedition_receipts(events), after, exclude)


def filter_settlements(receipts, after, exclude=()):
    seen = set(exclude)
    result = []
    for receipt in receipts:
        key = receipt["payload"]["receipt_key"]
        if receipt["ts"] < after or key in seen:
            continue
        seen.add(key)
        result.append(receipt)
    return result


def _request_key(event):
    value = [event.get("endpoint"), event.get("ts"), event.get("payload")]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _repair_entries(body):
    rows = body.get("repair")
    rows = rows.values() if isinstance(rows, dict) else rows if isinstance(rows, list) else ()
    return {(str(row["serial_id"]), str(row["finished_at"])) for row in rows
            if isinstance(row, dict) and row.get("serial_id")
            and row.get("finished_at")}


def begin_operation(maa):
    """Capture the pre-click boundary; never change the game to obtain evidence."""
    events = []
    request_snapshot = False
    if getattr(maa, "adb_address", None):
        try:
            events = read_events(maa.adb_path, maa.adb_address)
            request_snapshot = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
    repairs, repair_at = set(), None
    for event in events:
        body = successful_body(event)
        if body is not None and "repair" in body:
            repairs = _repair_entries(body)
            repair_at = youzu_log._event_epoch(event)
    now = time.time()
    state = sword_facts(events)
    swords = {serial: sword for serial, sword in state["swords"].items()
              if serial in state["hp_times"]
              and 0 <= now - state["hp_times"][serial][0] <= FACT_MAX_AGE
              and state["hp_times"][serial][0] >= state["combat_at"]
              and state["hp_times"][serial][1] >= state["combat_index"]}
    return {"after": int(time.time()),
            "request_snapshot": request_snapshot,
            "requests": {_request_key(e) for e in events if e.get("direction") == "C->S"},
            "repairs": repairs,
            "repair_known": repair_at is not None and 0 <= now - repair_at <= FACT_MAX_AGE,
            "swords": swords, "resources": fresh_resources(resource_facts(events))}


def operation_result(events, boundary, operation, serial=None):
    """Confirm new paired requests against the pre-click snapshot, independent of clocks.

    Without a complete request snapshot, retain the conservative time boundary.
    True=confirmed, False=rejected, None=unknown; a click alone is never success.
    """
    endpoint = {"union": "/composition/union", "dismantle": "/sword/dismantle_many",
                "forge": "/forge/startmultiple", "forge_ten": "/forge/fastmultiple",
                "compose": "/composition/compose", "task_rewards": "/mission/rewards",
                "free_gift": "/shop/buy", "duty": "/duty/start",
                "inbox_list": "/receive/list", "inbox_claim": "/receive/get"}.get(operation)
    pending = []
    outcome = None
    snapshot_boundary = operation != "repair" and boundary.get("request_snapshot") is True
    for event in events:
        at = youzu_log._event_epoch(event)
        if not snapshot_boundary and (at is None or at < boundary["after"]):
            continue
        if operation == "repair":
            body = successful_body(event)
            if body is None:
                continue
            added = _repair_entries(body) - boundary["repairs"]
            if boundary.get("repair_known", False) and any(serial is None or member == str(serial) for member, _ in added):
                return True
            if serial is not None:
                rows = body.get("sword") if "sword" in body else body.get("sword_all")
                if isinstance(rows, dict) and "serial_id" in rows:
                    rows = {str(rows["serial_id"]): rows}
                row = rows.get(str(serial)) if isinstance(rows, dict) else None
                old = boundary["swords"].get(str(serial), {})
                if isinstance(row, dict) and "hp" in row:
                    maximum = youzu_log._int(row.get("hp_max", old.get("hp_max")), 0)
                    before = youzu_log._int(old.get("hp"), -1)
                    if maximum > 0 and 0 <= before < maximum and youzu_log._int(row["hp"], -1) == maximum:
                        return True
            continue
        if event.get("endpoint") != endpoint:
            continue
        if event.get("direction") == "C->S":
            if _request_key(event) not in boundary["requests"]:
                pending.append(event)
            continue
        if event.get("direction") != "S->C" or not pending:
            continue
        if len(pending) != 1:
            pending.clear()
            outcome = None
            continue
        request = pending.pop()
        params = request.get("payload") or {}
        if operation in ("inbox_list", "inbox_claim"):
            body = successful_body(event)
            if body is not None:
                boundary["result_body"] = body
                boundary["result_filter"] = str(params.get("item_type", ""))
                if operation == "inbox_list":
                    outcome = isinstance(body.get("receive"), (dict, list))
                else:
                    ids = body.get("serial_ids")
                    outcome = bool(ids) if isinstance(ids, (list, str)) else None
            elif (event.get("status", 0) >= 400 or isinstance(event.get("payload"), dict)
                  and "status" in event["payload"] and str(event["payload"]["status"]) != "0"):
                outcome = False
            continue
        if operation in ("task_rewards", "free_gift", "duty"):
            body = successful_body(event)
            if operation == "task_rewards" and not params.get("category"):
                continue
            if operation == "free_gift" and not params.get("shop_id"):
                continue
            if body is not None:
                boundary["result_body"] = body
                boundary["result_at"] = at
                outcome = not (operation == "free_gift" and youzu_log._int(body.get("is_lack")) != 0)
                # Status=0 can still carry an expired mission list (reward_status=2).
                if operation == "task_rewards" and youzu_log._int(body.get("reward_status")) == 2:
                    outcome = False
            elif (event.get("status", 0) >= 400 or isinstance(event.get("payload"), dict)
                  and "status" in event["payload"] and str(event["payload"]["status"]) != "0"):
                boundary.pop("result_body", None)
                outcome = False
            continue
        if operation in ("forge", "forge_ten"):
            body = successful_body(event)
            if body is None:
                if (event.get("status", 0) >= 400 or isinstance(event.get("payload"), dict)
                        and str(event["payload"].get("status", "0")) != "0"):
                    outcome = False
                continue
            if operation == "forge":
                slot = youzu_log._int(params.get("slot_no"))
                recipe = [youzu_log._int(params.get(field), -1)
                          for field in ("charcoal", "steel", "coolant", "file")]
                outcome = ((slot == boundary.get("forge_slot")
                            and recipe == boundary.get("forge_recipe"))
                           if slot > 0 and all(value >= 0 for value in recipe) else None)
            else:
                count = youzu_log._int(body.get("finish_count"), -1)
                outcome = count == 10 if count >= 0 else None
            continue
        material_key = {"union": "material_serial_id", "compose": "material_id"}.get(operation, "serial_ids")
        base_key = "base_id" if operation == "compose" else "base_serial_id"
        raw = params.get(material_key)
        ids = str(raw).split(",") if raw is not None else []
        if (not ids or len(set(ids)) != len(ids)
                or not all(value.isdigit() and int(value) > 0 for value in ids)):
            continue
        if operation in ("union", "compose") and (youzu_log._int(params.get(base_key)) <= 0
                                                  or str(params.get(base_key)) in ids):
            continue
        body = successful_body(event)
        if body is not None:
            sword = body.get("sword")
            if operation == "compose" and (not isinstance(sword, dict)
                    or str(sword.get("serial_id")) != str(params[base_key])):
                outcome = None
                continue
            if (operation in ("union", "compose") and isinstance(sword, dict) and "serial_id" in sword
                    and str(sword["serial_id"]) != str(params[base_key])):
                outcome = None
                continue
            boundary["serial_ids"] = [int(value) for value in ids]
            outcome = True
        elif (event.get("status", 0) >= 400
              or isinstance(event.get("payload"), dict) and "status" in event["payload"]
              and str(event["payload"]["status"]) != "0"):
            outcome = False
    return outcome


def confirm_operation(maa, boundary, operation, serial=None, *, timeout=0.0):
    if not getattr(maa, "adb_address", None):
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            outcome = operation_result(read_events(maa.adb_path, maa.adb_address), boundary, operation, serial)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            outcome = None
        if outcome is not None:
            return outcome
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.5, remaining))


def forge_capacity(events, *, now=None):
    """Current forge roster/limit, with confirmed arrivals and consumed instances."""
    now = time.time() if now is None else now
    snapshots = [(youzu_log._event_epoch(e), i, successful_body(e))
                 for i, e in enumerate(events) if e.get("endpoint") == "/forge"
                 and successful_body(e) is not None]
    snapshots = [(at, i, body) for at, i, body in snapshots if at is not None]
    if not snapshots:
        return None
    observed, index, body = max(snapshots, key=lambda row: (row[0], row[1]))
    raw_maximum = body.get("sword_max")
    if isinstance(raw_maximum, bool) or isinstance(raw_maximum, float) and not raw_maximum.is_integer():
        return None
    maximum = youzu_log._int(raw_maximum, -1)

    def members(rows):
        if not isinstance(rows, dict):
            return None
        ids = {str(row.get("serial_id")) for row in rows.values() if isinstance(row, dict)}
        return ids if len(ids) == len(rows) and all(s.isdigit() and int(s) > 0 for s in ids) else None

    ids = members(body.get("sword"))
    if ids is None or maximum <= 0:
        return None
    pending = {}
    consume = {"/sword/dismantle_many": "serial_ids", "/composition/union": "material_serial_id",
               "/composition/compose": "material_id"}
    arrivals = {"/forge/complete", "/forge/completemultiple", "/forge/fastmultiple"}
    for event in events[index + 1:]:
        endpoint = event.get("endpoint")
        if endpoint in consume and event.get("direction") == "C->S":
            pending[endpoint] = None if endpoint in pending else event.get("payload")
            continue
        request = pending.pop(endpoint, None) if endpoint in consume and event.get("direction") == "S->C" else None
        body = successful_body(event)
        at = youzu_log._event_epoch(event)
        if body is None or at is None or at < observed:
            continue
        if endpoint in consume:
            raw = request.get(consume[endpoint]) if isinstance(request, dict) else None
            removed = str(raw).split(",") if raw is not None else []
            if not removed or not all(s.isdigit() and int(s) > 0 for s in removed):
                return None
            ids.difference_update(removed)
            observed = at
        elif endpoint in arrivals:
            rows = body.get("sword")
            rows = [rows] if isinstance(rows, dict) else rows
            if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) and r.get("serial_id") for r in rows):
                return None
            added = [str(row["serial_id"]) for row in rows]
            if (len(set(added)) != len(added) or not all(s.isdigit() and int(s) > 0 for s in added)
                    or youzu_log._int(body.get("finish_count"), len(added)) != len(added)):
                return None
            ids.update(added)
            observed = at
        elif endpoint in ("/party/list", "/sally"):
            rows = body.get("sword") if "sword" in body else body.get("sword_all")
            if rows is not None:
                current = members(rows)
                if current is None:
                    return None
                ids, observed = current, at
        elif endpoint == "/shop/buy" or endpoint and endpoint.startswith(("/receive/", "/battle/")):
            return None
    return (len(ids), maximum) if 0 <= now - observed <= FACT_MAX_AGE else None


def available_events(maa):
    """Missing connection evidence uses the existing screen path."""
    if not getattr(maa, "adb_address", None):
        return []
    try:
        return read_events(maa.adb_path, maa.adb_address)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return []


def duty_state(events, *, now=None):
    """Fresh duty deadline from the game; no local observation becomes a new start."""
    from datetime import datetime, timedelta, timezone
    now = time.time() if now is None else now
    for event in reversed(events):
        body = successful_body(event)
        at = youzu_log._event_epoch(event)
        if body is None or event.get("endpoint") not in ("/home", "/duty", "/duty/start", "/duty/complete"):
            continue
        if event.get("endpoint") == "/duty/complete":
            return None
        if not isinstance(body.get("duty"), dict):
            continue
        if at is None or not 0 <= now - at <= FACT_MAX_AGE:
            return None
        finish = body["duty"].get("finished_at")
        if not finish:
            return None
        try:
            end = datetime.strptime(str(finish), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=8))).timestamp()
        except ValueError:
            return None
        # Normal duty takes 24 hours; the deadline, rather than read time, owns the clock.
        return {"running": end > now, "finished_at": str(finish),
                "started_at": (datetime.fromtimestamp(end - 86400, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                               if str(body["duty"].get("item_use_status")) == "0" else None)}
    return None


def party_home(events, team_no, *, now=None):
    """最新一条新鲜 /party/list 成功响应里该队的 status：'1'=待命在家。

    True=游戏明说该队在家；False=游戏给了明确状态且不是 '1'（还在外）；
    None=没有新鲜成功响应、队伍行缺/坏——「不知道」绝不冒充「在家」。
    status 语义按日服 API 惯例推的（1=待命 2=远征中 3=出阵中），国服未
    逐项实测校准（见 youzu_log._PARTY_STATUS_LABEL），所以只有游戏
    明说 '1' 才翻篇，其余一律按不在家处理，宁保守不放行。
    """
    now = time.time() if now is None else now
    for event in reversed(events):
        body = successful_body(event)
        if body is None or event.get("endpoint") != "/party/list":
            continue
        at = youzu_log._event_epoch(event)
        if at is None or not 0 <= now - at <= FACT_MAX_AGE:
            return None
        rows = body.get("party")
        if not isinstance(rows, dict):
            return None
        row = rows.get(str(team_no))
        if not isinstance(row, dict):
            return None
        status = row.get("status")
        return str(status) == "1" if status is not None else None
    return None


def troop_matches(events, team, slot, position, name, *, previous=None, now=None):
    """Match a current member's equipped instance to its calibrated troop name."""
    from .client_equipment import EQUIPMENT_NAMES
    now = time.time() if now is None else now
    state = sword_facts(events)
    serial = str(state["parties"].get(str(team), {}).get(str(slot), ""))
    member_at = state["slot_times"].get(str(team), {}).get(str(slot), 0)
    field = f"equip_serial_id{position}"
    field_at = state["field_times"].get(serial, {}).get(field, 0)
    if (not serial or not 0 <= now - field_at <= FACT_MAX_AGE
            or not 0 <= now - member_at <= FACT_MAX_AGE):
        return None
    if previous is not None:
        previous_serial = str(sword_facts(previous)["parties"].get(str(team), {}).get(str(slot), ""))
        if previous_serial != serial:
            return False
        old = {_request_key(e) for e in previous if e.get("direction") == "S->C"}
        updates = sword_facts([e for e in events if e.get("direction") == "S->C"
                               and _request_key(e) not in old])
        if field not in updates["swords"].get(serial, {}):
            return None
    equip_serial = state["swords"].get(serial, {}).get(field)
    if equip_serial in (None, 0, "0", ""):
        return False
    for event in reversed(events):
        body = successful_body(event)
        at = youzu_log._event_epoch(event)
        rows = body.get("equip") if body else None
        if not isinstance(rows, dict) or at is None or not 0 <= now - at <= FACT_MAX_AGE:
            continue
        row = next((r for r in rows.values() if isinstance(r, dict)
                    and str(r.get("serial_id")) == str(equip_serial)), None)
        if row is not None:
            calibrated = EQUIPMENT_NAMES.get(youzu_log._int(row.get("equip_id")))
            return calibrated[1] == name if calibrated and calibrated[0] == "troop" else None
    return None


def team_member_matches(events, team, slot, expected, *, now=None):
    """Equipment belongs to the current, identified member of the requested slot."""
    from . import sword_db
    state = sword_facts(events)
    now = time.time() if now is None else now
    serial = str(state["parties"].get(str(team), {}).get(str(slot), ""))
    at = state["slot_times"].get(str(team), {}).get(str(slot), 0)
    if not serial or not 0 <= now - at <= FACT_MAX_AGE:
        return None
    observation = str(expected.get("observation_id") or "")
    if observation.startswith("youzu:") and observation[6:].isdigit():
        return serial == observation[6:]
    sword = state["swords"].get(serial, {})
    at = state["field_times"].get(serial, {}).get("sword_id", 0)
    if not expected.get("sword_catalog_id") or not 0 <= now - at <= FACT_MAX_AGE:
        return None
    found = sword_db.find_game_sword(youzu_log._int(sword.get("sword_id")))
    return found[0] == expected["sword_catalog_id"] if found else None


def practice_rank(rank):
    """Only calibrated CN ranks; other grades keep their screen fallback."""
    # CN rank 2 matched to the same battle's recorded victory stamp (2026-10-07).
    return "win" if str(rank) == "2" else None


def practice_list(events, *, now=None):
    now = time.time() if now is None else now
    def period(at):
        return int((at + 5 * 3600) // (12 * 3600))  # CN refresh at 03:00 / 15:00 (+08).
    for event in reversed(events):
        body = successful_body(event)
        at = youzu_log._event_epoch(event)
        if body is None or event.get("endpoint") != "/practice":
            continue
        if at is None or not 0 <= now - at <= FACT_MAX_AGE or period(at) != period(now):
            return None
        rows = body.get("summary")
        if not isinstance(rows, dict) or set(rows) != set("12345"):
            return None
        if not all(isinstance(r, dict) and youzu_log._int(r.get("enemy_id")) > 0 and "rank" in r for r in rows.values()):
            return None
        result = {int(k)-1: {"enemy_id": str(r["enemy_id"]), "title": str(r.get("name") or ""),
                            "rank": r["rank"]} for k,r in rows.items()}
        boundary = {"after": at, "requests": set()}
        subsequent = events[events.index(event)+1:]
        for row in result.values():
            receipt = practice_receipt(subsequent, boundary, "/battle/practicebattle", row["enemy_id"], "enemy_user_id", ignore_other=True)
            if isinstance(receipt, dict) and isinstance(receipt.get("result"), dict) and "rank" in receipt["result"]:
                row["rank"] = receipt["result"]["rank"]
        return result
    return None


def practice_receipt(events, boundary, endpoint, enemy_id, parameter, *, ignore_other=False):
    pending = []
    result = None
    for event in events:
        at = youzu_log._event_epoch(event)
        # A successful pre-click snapshot identifies old requests directly.
        # Device request times, server response times and the PC clock differ;
        # comparing those clocks can discard the request for this very click.
        if event.get("endpoint") != endpoint:
            continue
        if not boundary.get("request_snapshot") and (at is None or at < boundary["after"]):
            continue
        if event.get("direction") == "C->S":
            if _request_key(event) not in boundary["requests"]:
                pending.append(event)
            continue
        if event.get("direction") != "S->C" or not pending:
            continue
        if len(pending) != 1:
            pending.clear()
            result = None
            continue
        request = pending.pop()
        if enemy_id is not None and str((request.get("payload") or {}).get(parameter)) != str(enemy_id):
            if not ignore_other:
                result = False
            continue
        body = successful_body(event)
        if body is not None:
            boundary["practice_enemy_id"] = (request.get("payload") or {}).get(parameter)
            result = body
        elif event.get("status", 0) >= 400 or isinstance(event.get("payload"), dict) and "status" in event["payload"] and str(event["payload"]["status"]) != "0":
            result = False
    return result


def practice_members(body):
    from . import sword_db
    rows = body.get("enemy_sword")
    parties = body.get("enemy_party")
    party = parties.get("1") if isinstance(parties, dict) else None
    slots = party.get("slot") if isinstance(party, dict) else None
    if not isinstance(rows, dict) or not isinstance(slots, dict) or set(slots) != set("123456"):
        return None
    members = []
    seen = set()
    for slot in map(str, range(1,7)):
        entry = slots[slot]
        if not isinstance(entry, dict) or "serial_id" not in entry:
            return None
        serial = entry["serial_id"]
        if serial in (None, 0, "0", ""):
            continue
        if str(serial) in seen:
            return None
        seen.add(str(serial))
        row = rows.get(str(serial))
        if not isinstance(row, dict) or "serial_id" in row and str(row["serial_id"]) != str(serial):
            return None
        found = sword_db.find_game_sword(youzu_log._int(row.get("sword_id")))
        if not found:
            members.append({"name": "未识别成员", "type": "未知", "id": None, "mobility": None, "level": None})
            continue
        sid, info, form = found
        mobile = youzu_log._int(row.get("mobile"), None)
        members.append({"id": sid, "name": info.get("name_zh") or info["name"],
                        "type": info.get("type", "未知"), "level": youzu_log._int(row.get("level"), None),
                        "mobility": mobile, "extreme": form == "kiwame"})
    return members


def furnace_facts(events, *, now=None):
    from datetime import datetime, timedelta, timezone
    now = time.time() if now is None else now
    for event in reversed(events):
        body = successful_body(event)
        at = youzu_log._event_epoch(event)
        if body is None:
            continue
        endpoint = event.get("endpoint") or ""
        if endpoint.startswith("/forge/") and "forge" not in body:
            return None
        if (endpoint not in ("/home/situation", "/forge") and not endpoint.startswith("/forge/")) or "forge" not in body:
            continue
        if at is None or not 0 <= now - at <= FACT_MAX_AGE or not isinstance(body["forge"], dict):
            return None
        result = []
        for slot, row in body["forge"].items():
            if not isinstance(row, dict) or "finished_at" not in row:
                return None
            finish = row["finished_at"]
            state, remain = "空闲中", None
            if finish:
                try:
                    end = datetime.strptime(str(finish), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
                except ValueError:
                    return None
                left = max(0, int(end-now))
                state = "锻造中" if left else "完成"
                if left:
                    remain = f"{left//3600}:{left%3600//60:02d}:{left%60:02d}"
            number = youzu_log._int(row.get("slot_no", slot))
            if number <= 0 or number in [r["slot"] for r in result]:
                return None
            result.append({"slot": number, "state": state, "remain": remain})
        return sorted(result, key=lambda r:r["slot"]) or None
    return None


def ticket_receipt(events, boundary):
    """A fresh paid recovery request and its exact result, including actual balance change."""
    body = practice_receipt(events, boundary, "/sally/recovercost", "1", "num")
    if body is False or body is None:
        return body
    # The CN receipt returns the recovered quantity as result (one for num=1).
    count = youzu_log._int(body.get("result"), -1)
    if count == 0:
        return False
    if count != 1:
        return None
    before = boundary.get("resources", {}).get("小判")
    after = (youzu_log._reading_from_payload(body) or {}).get("小判")
    spent = before - after if before is not None and after is not None and after <= before else None
    return {"count": count, "spent": spent}


def confirm_ticket(maa, boundary):
    return ticket_receipt(available_events(maa), boundary)


def activity_points(events, mechanic, *, now=None, after=None):
    """Bind an activity ID to its active game block, then read that ID's current points."""
    block = {"raid": "allout", "hanafuda": "koikoi"}.get(mechanic)
    if block is None:
        return None
    now = time.time() if now is None else now
    pending, binding, points = [], None, {}
    for event in events:
        endpoint = event.get("endpoint")
        at = youzu_log._event_epoch(event)
        if endpoint == "/sally/eventsally" and event.get("direction") == "C->S":
            pending.append(event)
            continue
        body = successful_body(event)
        if endpoint == "/sally/eventsally" and event.get("direction") == "S->C":
            request = pending.pop() if len(pending) == 1 else None
            pending.clear()
            if request and body is not None and at is not None:
                active = [key for key in ("allout", "koikoi", "edo", "goldenfall", "tsukimi")
                          if isinstance(body.get(key), dict) and body[key]]
                event_id = (request.get("payload") or {}).get("event_id")
                if active == [block] and youzu_log._int(event_id) > 0:
                    binding = (str(event_id), at)
        if body is not None and at is not None and isinstance(body.get("point"), dict):
            for event_id, value in body["point"].items():
                count = youzu_log._int(value, -1)
                if count >= 0 and at >= points.get(str(event_id), (-1, 0))[1]:
                    points[str(event_id)] = (count, at)
    if binding is None or not 0 <= now - binding[1] <= FACT_MAX_AGE:
        return None
    count, at = points.get(binding[0], (-1, 0))
    return count if count >= 0 and 0 <= now - at <= FACT_MAX_AGE and (after is None or at >= after) else None


def member_field(events, team, slot, field, *, now=None, previous=None):
    """An identified current member and a freshly observed field."""
    now = time.time() if now is None else now
    state = sword_facts(events)
    serial = str(state["parties"].get(str(team), {}).get(str(slot), ""))
    member_at = state["slot_times"].get(str(team), {}).get(str(slot), 0)
    if not serial.isdigit() or int(serial) <= 0:
        return None
    at = state["field_times"].get(serial, {}).get(field, 0)
    if not all(0 <= now - t <= FACT_MAX_AGE for t in (member_at, at)):
        return None
    if previous is not None:
        before = sword_facts(previous)
        if str(before["parties"].get(str(team), {}).get(str(slot), "")) != serial:
            return None
        old = {_request_key(e) for e in previous if e.get("direction") == "S->C"}
        new = sword_facts([e for e in events if e.get("direction") == "S->C" and _request_key(e) not in old])
        if field not in new["swords"].get(serial, {}):
            return None
    return serial, state["swords"].get(serial, {}).get(field)


def fatigue_value(events, team, slot, *, now=None):
    member = member_field(events, team, slot, "fatigue", now=now)
    if member is None:
        return None
    serial, raw = member
    state = sword_facts(events)
    if (state["field_indices"].get(serial, {}).get("fatigue", -1) < state["combat_index"]
            or state["field_times"][serial]["fatigue"] < state["combat_at"]):
        return None
    if isinstance(raw, bool) or isinstance(raw, float) and not raw.is_integer():
        return None
    value = youzu_log._int(raw, -1)
    return value if 0 <= value <= 100 else None


def equipped_asset(events, team, slot, kind, *, now=None, previous=None):
    from .client_equipment import EQUIPMENT_NAMES, CHARM_NAMES, TREASURE_NAMES
    field = {"horse": "horse_serial_id", "charm": "item_id", "treasure": "artifact_serial_id1"}[kind]
    member = member_field(events, team, slot, field, now=now, previous=previous)
    if member is None:
        return None
    serial, asset_id = member
    if asset_id in (None, "", 0, "0"):
        return {"empty": True}
    if kind == "charm":
        name = CHARM_NAMES.get(youzu_log._int(asset_id))
        return {"name": name} if name else None
    now = time.time() if now is None else now
    for event in reversed(events):
        body = successful_body(event)
        at = youzu_log._event_epoch(event)
        pool = body.get("artifact" if kind == "treasure" else "equip") if body else None
        if not isinstance(pool, dict) or at is None or not 0 <= now-at <= FACT_MAX_AGE:
            continue
        row = next((r for r in pool.values() if isinstance(r, dict) and str(r.get("serial_id")) == str(asset_id)), None)
        if row is None:
            continue
        if kind == "horse":
            known = EQUIPMENT_NAMES.get(youzu_log._int(row.get("equip_id")))
            return {"name": known[1]} if known and known[0] == "horse" else None
        level = youzu_log._int(row.get("level"), -1)
        affection = youzu_log._int(row.get("usage_score"), -1)
        if level < 1 or affection < 0:
            return None
        return {"name": TREASURE_NAMES.get(youzu_log._int(row.get("artifact_id"))),
                "level": level, "affection": affection}
    return None
