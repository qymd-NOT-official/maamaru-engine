"""Shared game facts: one reader, explicit observation times and operation results."""
from __future__ import annotations

import math
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


def injury_report(events, team_no, *, max_age=FACT_MAX_AGE, now=None):
    """Use complete, current member HP; later changes invalidate older snapshots."""
    report = youzu_log.party_injury_report(events, team_no)
    if report is None:
        return None
    now = time.time() if now is None else now
    hp_at, party_at = report["observed_epoch"], report["party_observed_epoch"]
    if any(at is None or not 0 <= now - at <= max_age for at in (hp_at, party_at)):
        return None
    party_index = max(i for i, event in enumerate(events)
                      if successful_body(event) is not None
                      and event.get("endpoint") in youzu_log._SWORD_FULL_ENDPOINTS
                      and isinstance(event["payload"].get("party"), dict))
    _, pool_event = youzu_log._latest_sword_pool(events)
    hp_index = next(i for i, event in enumerate(events) if event is pool_event)
    for i, event in enumerate(events):
        body = successful_body(event)
        if body is None:
            continue
        endpoint = event.get("endpoint") or ""
        if i > party_index and endpoint in ("/party/setsword", "/party/dissolution"):
            return None
        if i > hp_index and (isinstance(body.get("sword"), dict)
                           or isinstance(body.get("sword_all"), dict)
                           or endpoint.startswith(("/battle", "/sally/"))):
            return None
    return report
