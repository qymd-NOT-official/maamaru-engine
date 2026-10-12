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
    hp_times, slot_times, field_times = {}, {}, {}
    combat_index, combat_at = -1, 0
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
                swords, hp_times, field_times = {}, {}, {}
            for key, row in rows.items():
                if not isinstance(row, dict):
                    continue
                serial = str(row.get("serial_id") or key)
                if not serial.isdigit() or int(serial) <= 0:
                    continue
                current = swords.setdefault(serial, {})
                times = field_times.setdefault(serial, {})
                for field, value in row.items():
                    if at >= times.get(field, 0):
                        current[field], times[field] = value, at
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
    return {"swords": swords, "parties": parties, "hp_times": hp_times,
            "slot_times": slot_times, "combat_index": combat_index, "combat_at": combat_at}


def injury_report(events, team_no, *, max_age=FACT_MAX_AGE, now=None):
    """Merged member HP is authoritative; missing post-battle HP remains unknown."""
    from . import sword_db
    state = sword_facts(events)
    now = time.time() if now is None else now
    slots = state["parties"].get(str(team_no))
    if not slots:
        return None
    members, hp_ages, party_ages = [], [], []
    for slot, serial in slots.items():
        if serial in (None, "", 0, "0"):
            continue
        serial = str(serial)
        sword = state["swords"].get(serial, {})
        hp_at, hp_index = state["hp_times"].get(serial, (None, -1))
        party_at = state["slot_times"][str(team_no)].get(slot)
        if (hp_at is None or party_at is None or hp_index < state["combat_index"]
                or hp_at < state["combat_at"]
                or any(not 0 <= now - at <= max_age for at in (hp_at, party_at))):
            return None
        hp, maximum = youzu_log._int(sword.get("hp"), -1), youzu_log._int(sword.get("hp_max"), 0)
        if not 0 <= hp <= maximum or maximum <= 0:
            return None
        name = youzu_log._sword_name(sword.get("sword_id"), sword_db)
        members.append({"serial_id": int(serial), "name": name, "label": name,
                        "hp": hp, "hp_max": maximum,
                        "injury": youzu_log.injury_tier(hp, maximum)})
        hp_ages.append(hp_at)
        party_ages.append(party_at)
    if not members:
        return None
    rank = {None: 0, "轻伤": 1, "中伤": 2, "重伤": 3}
    return {"party_no": team_no, "members": members,
            "max_injury": max((m["injury"] for m in members), key=rank.get),
            "observed_epoch": min(hp_ages), "party_observed_epoch": min(party_ages)}


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
    if getattr(maa, "adb_address", None):
        try:
            events = read_events(maa.adb_path, maa.adb_address)
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
            "requests": {_request_key(e) for e in events if e.get("direction") == "C->S"},
            "repairs": repairs,
            "repair_known": repair_at is not None and 0 <= now - repair_at <= FACT_MAX_AGE,
            "swords": swords}


def operation_result(events, boundary, operation, serial=None):
    """True=confirmed, False=rejected, None=unknown; a click alone is never success."""
    endpoint = {"union": "/composition/union", "dismantle": "/sword/dismantle_many",
                "forge": "/forge/startmultiple", "forge_ten": "/forge/fastmultiple"}.get(operation)
    pending = []
    outcome = None
    for event in events:
        at = youzu_log._event_epoch(event)
        if at is None or at < boundary["after"]:
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
        raw = params.get("material_serial_id" if operation == "union" else "serial_ids")
        ids = str(raw).split(",") if raw is not None else []
        if (not ids or len(set(ids)) != len(ids)
                or not all(value.isdigit() and int(value) > 0 for value in ids)):
            continue
        if operation == "union" and (youzu_log._int(params.get("base_serial_id")) <= 0
                                     or str(params.get("base_serial_id")) in ids):
            continue
        body = successful_body(event)
        if body is not None:
            sword = body.get("sword")
            if (operation == "union" and isinstance(sword, dict) and "serial_id" in sword
                    and str(sword["serial_id"]) != str(params["base_serial_id"])):
                outcome = None
                continue
            boundary["serial_ids"] = [int(value) for value in ids]
            outcome = True
        elif (event.get("status", 0) >= 400
              or isinstance(event.get("payload"), dict) and "status" in event["payload"]
              and str(event["payload"]["status"]) != "0"):
            outcome = False
    return outcome


def confirm_operation(maa, boundary, operation, serial=None):
    if not getattr(maa, "adb_address", None):
        return None
    try:
        return operation_result(read_events(maa.adb_path, maa.adb_address), boundary, operation, serial)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None


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
