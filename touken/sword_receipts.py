"""客户端确认的事实收据：刀剑领取、合战场掉落、周回分母、开炉配方、
近侍观测、活动日历、修行进出；不依赖 OCR 刀帐。"""
from __future__ import annotations

import hashlib
import json

from .youzu_log import _event_epoch, _int, _sword_name

# 锻刀配方的资源字段（2026-09~10 实测：/forge/startmultiple 请求 payload）。
_FORGE_RECIPE_FIELDS = ("charcoal", "steel", "coolant", "file")

# /home/leave 请求（送刀去修行）的 payload 字段名未实测（2026-10 校准轮
# 没抓到样本）：只白名单尝试这几个候选键，取不到就不产 kiwame.departed，
# 绝不猜别的键名。
_KIWAME_DEPART_FIELDS = ("serial_id", "sword_serial_id")


def _receipt_identity(event_type, payload):
    """去重键里的身份段：每种事件只取自己的事实字段，取不到就是 None。"""
    if event_type == "battle.completed":
        return [payload.get("chapter"), payload.get("map_no"),
                payload.get("team_no"), payload.get("square_id")]
    if event_type == "forge.started":
        return [payload.get("slot_no")]
    if event_type == "secretary.observed":
        return [payload.get("sword_id")]
    if event_type in ("kiwame.returned", "kiwame.departed"):
        return [payload.get("serial_id")]
    if event_type == "activity.calendar":
        return [payload.get("events")]
    swords = payload.get("swords")
    return ([(s["sword_id"], s["serial_id"]) for s in swords]
            if isinstance(swords, list) else payload.get("sword_id"))


def _receipt_key(ts, endpoint, payload, event_type=None):
    raw = json.dumps([ts, endpoint, _receipt_identity(event_type, payload)],
                     sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def build_receipts(events):
    from . import sword_db
    pending = {}
    route = {}
    square = None
    receipts = []
    for event in events:
        endpoint = event.get("endpoint") or ""
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("direction") == "C->S":
            pending[endpoint] = payload
            # 新出阵开始就清空，失败的请求也不能沿用上一张地图。
            if endpoint.startswith("/sally/") and endpoint not in (
                    "/sally/forward", "/sally/sally"):
                route, square = {}, None
            if endpoint == "/sally/sally":
                route, square = {}, None
            continue
        if (event.get("direction") != "S->C"
                or event.get("status") != 200
                or str(payload.get("status", 0)) != "0"):
            continue
        request = pending.pop(endpoint, {})
        ts = _event_epoch(event)
        if not ts:
            continue
        if endpoint == "/sally/sally":
            route = {"chapter": _int(request.get("episode_id")),
                     "map_no": _int(request.get("field_id")),
                     "team_no": _int(request.get("party_no"))}
        if endpoint == "/sally/forward":
            square = payload.get("square_id")
        produced = []
        if endpoint in ("/forge/completemultiple", "/forge/complete", "/forge/fastmultiple"):
            swords = payload.get("sword")
            if isinstance(swords, dict):
                swords = [swords]
            if not isinstance(swords, list):
                swords = []
            swords = [{"name": _sword_name(row["sword_id"], sword_db),
                           "sword_id": _int(row["sword_id"]),
                           "serial_id": _int(row.get("serial_id")),
                           "is_first_get_sword": bool(row.get("is_first_get_sword"))}
                          for row in swords if isinstance(row, dict)
                          and _int(row.get("sword_id")) > 0]
            if swords:
                produced.append(("forge.collected",
                                 {"source": "forge", "slot": _int(request.get("slot_no")),
                                  "count": len(swords), "swords": swords}))
        elif endpoint in ("/battle/battle", "/battle/alloutbattle"):
            normal = endpoint == "/battle/battle"
            # 周回分母：不管掉不掉刀，成功打了一仗就记一条（路线取不到就 null）。
            produced.append(("battle.completed", {
                "chapter": (route.get("chapter") or None) if normal else None,
                "map_no": (route.get("map_no") or None) if normal else None,
                "team_no": (route.get("team_no") or None) if normal else None,
                "square_id": square if normal else None}))
            result = payload.get("result") or {}
            if isinstance(result, dict):
                sid = _int(result.get("get_sword_id"))
                if sid > 0:
                    source = ("sortie.drop" if route.get("chapter") and route.get("map_no")
                              else "battle.drop") if normal else "raid.drop"
                    produced.append(("sword.obtained",
                                     {"name": _sword_name(sid, sword_db), "sword_id": sid,
                                      "acquired_at": payload.get("now") or event["ts"],
                                      "source": source, **(route if normal else {}),
                                      "square_id": square if normal else None,
                                      "is_first_get_sword": bool(result.get("is_first_get_sword"))}))
        elif endpoint == "/forge/startmultiple":
            produced.extend(("forge.started", d) for d in _forge_started(request, payload))
        elif endpoint == "/login/start":
            secretary = _int(payload.get("secretary"), None)
            if secretary:
                produced.append(("secretary.observed", {"sword_id": secretary}))
        elif endpoint == "/home/get_all_activity":
            calendar = _activity_calendar(payload)
            if calendar is not None:
                produced.append(("activity.calendar", calendar))
        elif endpoint == "/home/leave":
            produced.extend(("kiwame.returned", d) for d in _kiwame_returned(payload))
            departed = _kiwame_departed(request)
            if departed is not None:
                produced.append(("kiwame.departed", departed))
        for kind, detail in produced:
            key = _receipt_key(ts, endpoint, detail, kind)
            detail.update(evidence_source="youzu_log", endpoint=endpoint,
                          receipt_key=key)
            receipts.append({"ts": ts, "event_type": kind, "payload": detail})
    from .inbox_receipts import build_inbox_receipts, link_receipt_serials, observed_swords
    swords = observed_swords(events)
    link_receipt_serials(receipts, swords)
    from .expedition_receipts import build_expedition_receipts
    return receipts + build_inbox_receipts(events, swords) + build_expedition_receipts(events)


def _forge_started(request, payload):
    """/forge/startmultiple 成功响应 + 它的请求配方，逐开炉槽位一条
    （配方+开炉时刻是锻刀串链的分母）。multiple 元素形态未逐项实测：
    标量按槽位号解释，dict 白名单取 slot_no；槽位号取不到的那一槽不产
    事件，配方取不到的字段记 null，都不猜。"""
    multiple = payload.get("multiple")
    if not isinstance(multiple, list) or not multiple:
        return []
    recipe = {field: _int(request.get(field), None)
              for field in _FORGE_RECIPE_FIELDS}
    rows = []
    for entry in multiple:
        slot = _int(entry.get("slot_no"), None) if isinstance(entry, dict) \
            else _int(entry, None)
        if not slot:
            continue
        rows.append({"slot_no": slot, **recipe})
    return rows


def _activity_calendar(payload):
    """/home/get_all_activity 的 event 日历（解析口径同 build_snapshot，
    youzu_log.py 的 events_calendar；日历内容为整体事实，同内容不重写）。"""
    raw = payload.get("event") or {}
    if not isinstance(raw, dict) or not raw:
        return None
    calendar = [{"event_id": e.get("event_id"),
                 "type": _int(e.get("type"), -1),
                 "start_at": e.get("start_at"), "end_at": e.get("end_at")}
                for e in raw.values() if isinstance(e, dict)]
    return {"events": calendar} if calendar else None


def _kiwame_returned(payload):
    """/home/leave 响应 evolution.back：每振修行归来一条（履历事实）。"""
    evolution = payload.get("evolution")
    back = evolution.get("back") if isinstance(evolution, dict) else None
    if not isinstance(back, dict):
        return []
    rows = []
    for entry in back.values():
        if not isinstance(entry, dict):
            continue
        serial = _int(entry.get("serial_id"), None)
        if not serial:
            continue
        rows.append({"serial_id": serial,
                     "finished_at": entry.get("finished_at")})
    return rows


def _kiwame_departed(request):
    """送刀去修行的 C->S /home/leave 请求 → {serial_id}。请求字段名未实测，
    只白名单尝试 _KIWAME_DEPART_FIELDS 里的候选键；都取不到返回 None。"""
    for field in _KIWAME_DEPART_FIELDS:
        serial = _int(request.get(field), None)
        if serial:
            return {"serial_id": serial}
    return None


def write_receipts(store, receipts):
    """独立于资源余额水位去重；唯一对应时补全原 OCR 记录并保留证据。"""
    if not receipts:
        return {"written": 0, "reconciled": 0}
    conn = store._conn()
    written = reconciled = 0
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, ts, run_id, script, event_type, payload FROM events "
            "WHERE ts BETWEEN ? AND ? AND event_type IN "
            "('forge.collected', 'sword.obtained', 'sword.drop_unrecognized', 'sword.inbox_received', 'expedition.settled', "
            "'battle.completed', 'forge.started', 'secretary.observed', 'kiwame.returned', 'kiwame.departed', 'activity.calendar')",
            (min(r["ts"] for r in receipts) - 90,
             max(r["ts"] for r in receipts) + 90)).fetchall()
        existing = [dict(zip(("id", "ts", "run_id", "script", "event_type", "payload"), row))
                    for row in rows]
        for row in existing:
            row["payload"] = json.loads(row["payload"])
        keys = {r["payload"].get("receipt_key") for r in existing}
        keys.update(_receipt_key(r["ts"], r["payload"].get("endpoint"), r["payload"],
                                 r["event_type"])
                    for r in existing if r["payload"].get("receipt_key"))

        def matches(receipt, row):
            p, old = receipt["payload"], row["payload"]
            if (row["script"] in ("manual", "youzu_log") or old.get("receipt_key")
                    or abs(receipt["ts"] - row["ts"]) > 90):
                return False
            if receipt["event_type"] == "forge.collected":
                return (row["event_type"] == "forge.collected"
                        and p.get("slot") == old.get("slot")
                        and (not old.get("name") or old["name"] in
                             [s["name"] for s in p["swords"]]))
            if receipt["event_type"] == "expedition.settled":
                return (row['event_type'] == 'expedition.settled'
                        and p['team_no'] == old.get('team_no')
                        and p['era'] == old.get('era') and p['slot'] == old.get('slot'))
            return (receipt["event_type"] == "sword.obtained" and p.get("source") == "sortie.drop"
                    and row["event_type"] in ("sword.obtained", "sword.drop_unrecognized")
                    and old.get("source") == "sortie.drop"
                    and (not old.get("name") or old["name"] == p["name"])
                    and all(not old.get(k) or str(old[k]) == str(p.get(k))
                            for k in ("chapter", "map_no")))

        for receipt in receipts:
            p = dict(receipt["payload"])
            if receipt["event_type"] == "activity.calendar":
                # 日历是整体事实：同 ts 同 payload 不重写（仿 training.captured
                # 的 INSERT ... WHERE NOT EXISTS），receipt_key 另挡同批重复。
                encoded = json.dumps(p, ensure_ascii=False)
                cursor = conn.execute(
                    "INSERT INTO events(ts, run_id, script, event_type, payload) "
                    "SELECT ?, NULL, 'youzu_log', 'activity.calendar', ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM events WHERE ts = ? AND script = 'youzu_log' "
                    "AND event_type = 'activity.calendar' AND payload = ?)",
                    (receipt["ts"], encoded, receipt["ts"], encoded))
                written += cursor.rowcount
                keys.add(p["receipt_key"])
                continue
            if p["receipt_key"] in keys:
                continue
            candidates = [row for row in existing if matches(receipt, row)]
            if (len(candidates) == 1 and sum(matches(other, candidates[0])
                    for other in receipts) == 1):
                row = candidates[0]
                p.update(ocr_evidence=row["payload"], execution_script=row["script"])
                conn.execute("UPDATE events SET ts=?, script=?, event_type=?, payload=? WHERE id=?",
                             (receipt["ts"], "youzu_log", receipt["event_type"],
                              json.dumps(p, ensure_ascii=False), row["id"]))
                row["payload"] = p
                reconciled += 1
            else:
                conn.execute("INSERT INTO events(ts, run_id, script, event_type, payload) "
                             "VALUES (?, NULL, 'youzu_log', ?, ?)",
                             (receipt["ts"], receipt["event_type"],
                              json.dumps(p, ensure_ascii=False)))
                written += 1
            keys.add(p["receipt_key"])
    return {"written": written, "reconciled": reconciled}


def sync_receipts(events, store=None):
    if store is None:
        from .telemetry import TelemetryStore
        store = TelemetryStore()
    receipts = build_receipts(events)
    result = write_receipts(store, receipts)
    from .game_sword_archive import sync_archive
    sync_archive(events, store, receipts=receipts)
    return result
