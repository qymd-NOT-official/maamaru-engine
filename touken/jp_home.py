"""日服首页状态：只保留展示所需字段，不保存账号标识或完整响应。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import sword_db
from .youzu_log import _sword_name

JST = timezone(timedelta(hours=9))
DISPLAY_TZ = timezone(timedelta(hours=8))


def display_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def home_observation(payload):
    observed = display_time(payload.get("now"))
    if not observed:
        return None
    result = {}
    sid = payload.get("secretary")
    if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0:
        result["secretary"] = {"name": _sword_name(sid, sword_db) or f"刀帐{sid}", "observed_at": observed}
    raw = payload.get("situation")
    if isinstance(raw, dict):
        if isinstance(raw.get("conquest"), dict):
            result["parties"] = [{"party_no": int(key), "party_name": "", "members": [],
                "finished_at": display_time(value.get("finished_at"))}
                for key, value in raw["conquest"].items()
                if str(key).isdigit() and 1 <= int(key) <= 5 and isinstance(value, dict)]
            result["parties_observed_at"] = observed
        for source, target, stamp in [("forge", "forge_slots", "forge_observed_at"),
                                       ("repair", "repair", "repair_observed_at")]:
            if isinstance(raw.get(source), dict):
                result[target] = [{"slot_no": int(key), "finished_at": finish}
                    for key, value in raw[source].items()
                    if str(key).isdigit() and 1 <= int(key) <= 10 and isinstance(value, dict)
                    and (finish := display_time(value.get("finished_at")))]
                result[stamp] = observed
        if isinstance(raw.get("duty"), dict):
            result["duty"] = {"finished_at": display_time(raw["duty"].get("finished_at"))}
            result["duty_observed_at"] = observed
    return result or None


def current_situation(store):
    state = {"schema": 1, "secretary": {"name": "", "observed_at": None},
             "parties": [], "parties_observed_at": None,
             "kiwame_return": [], "kiwame_observed_at": None,
             "forge_slots": [], "forge_observed_at": None,
             "repair": [], "repair_observed_at": None,
             "duty": None, "duty_observed_at": None}
    rows = store._conn().execute(
        "SELECT payload FROM events WHERE event_type='home.observed' "
        "AND script IN ('jp_listener','jp_netlog') ORDER BY ts,id").fetchall()
    for row in rows:
        update = json.loads(row["payload"])
        state.update({key: value for key, value in update.items() if key in state and key != "schema"})
    return state if rows else None
