# -*- coding: utf-8 -*-
"""锻刀串链：forge.started ⋈ forge.collected 按炉位 + 时间序配对的履历视图。

诚实口径（没实测校准的判定一律不编，拿不准的给 null 并在这里写明）：

- 配对只看「同一炉位的时间序」：一条 started 之后、同炉位下一条 started
  之前的 collected 归这炉。started 没有等到 collected → 照常出一炉，
  collected_at/swords 给 null（炉子还烧着或领取没记上，不编结果）；
  collected 前面没有同炉位的 started（开炉在采集起点之前、跨窗口边界）
  → 进 orphan_collected 老实列出，不硬撮合。
- 炉位号两种写法都收：youzu_log 收据写 slot_no（带配方），面板锻刀流
  写 slot（不带配方）；配方字段缺失一律 null，cost_est 整个给 null，
  绝不拿默认配方填。
- 近侍 = 开炉时刻之前最近一条 secretary.observed（登录时刻的观测）。
  局内换人不会产观测——登录之后在游戏里换的近侍，开炉时实际站的是谁
  分辨不出，只能按最近一次登录观测给，这是口径不是 bug。
- cost_est 只估四种资源的配方消耗：count>1（十连一把收）按配方×count，
  普通按配方×1。委托符/加速符消耗不走这里（resource.change 里另记），
  读数读不出的资源字段保持 null，不补 0。
- days 窗口按事件自身 ts 过滤 started 与 collected（缺省 30，0=全部）；
  窗口只保证窗口内的事件互相配对，started 在窗外的炉其 collected 落进
  窗口会进 orphan_collected。
- forges/orphan_collected 一律时间升序（新的在后）。
"""
from __future__ import annotations

import json
import time
from bisect import bisect_right
from datetime import timedelta, timezone

from . import sword_db
from .youzu_log import _sword_name

_FORGE_STARTED = "forge.started"
_FORGE_COLLECTED = "forge.collected"
_SECRETARY = "secretary.observed"
_EVENT_TYPES = (_FORGE_STARTED, _FORGE_COLLECTED, _SECRETARY)
_RECIPE_FIELDS = ("charcoal", "steel", "coolant", "file")
_SH = timezone(timedelta(hours=8))


def _num(value):
    """数字清洗：bool/怪值一律 None；浮点整数尽量保整数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not float(value).is_integer():
        return None
    return int(value)


def _slot_no(payload: dict) -> int | None:
    """炉位号：started 收据写 slot_no、面板锻刀流写 slot，都收。"""
    for key in ("slot_no", "slot"):
        slot = _num(payload.get(key))
        if slot is not None:
            return slot
    return None


def _fetch_events(store, from_ts) -> list[dict]:
    clauses = "event_type IN (?,?,?)"
    args: list = list(_EVENT_TYPES)
    if from_ts is not None:
        clauses += " AND ts >= ?"
        args.append(float(from_ts))
    rows = store._conn().execute(
        "SELECT id, ts, event_type, payload FROM events WHERE " + clauses +
        " ORDER BY ts, id", args).fetchall()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        events.append({"id": row["id"], "ts": float(row["ts"]),
                       "event_type": row["event_type"], "payload": payload})
    return events


def _recipe(payload: dict) -> dict:
    return {field: _num(payload.get(field)) for field in _RECIPE_FIELDS}


def _swords_from_collected(payload: dict) -> list[dict]:
    """领取结果的刀列表：收据是 swords 列表；面板锻刀流是内联的单刀字段
    （sword_id/name，认不出时连这个都没有）。"""
    raw = payload.get("swords")
    if isinstance(raw, list):
        swords = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            swords.append({
                "name": str(name) if name is not None else None,
                "sword_id": _num(row.get("sword_id")),
                "serial_id": _num(row.get("serial_id")),
                "is_first_get_sword": (bool(row.get("is_first_get_sword"))
                                       if row.get("is_first_get_sword") is not None
                                       else None),
            })
        return swords
    if payload.get("sword_id") is not None or payload.get("name") is not None:
        name = payload.get("name")
        return [{"name": str(name) if name is not None else None,
                 "sword_id": _num(payload.get("sword_id")),
                 "serial_id": _num(payload.get("serial_id")),
                 "is_first_get_sword": None}]
    return []


def _count_from_collected(payload: dict, swords: list[dict]) -> int:
    """领取把数：收据 count 优先（正整数），否则按数出的刀数。"""
    count = _num(payload.get("count"))
    if count is not None and count > 0:
        return count
    return len(swords)


def _cost_est(recipe: dict, count: int) -> dict | None:
    """配方消耗估计：count>1 按配方×count，普通×1；配方全缺给 null。"""
    if all(value is None for value in recipe.values()):
        return None
    mult = count if count > 1 else 1
    return {field: (value * mult if value is not None else None)
            for field, value in recipe.items()}


def build_forge_history(store, days: int = 30, now: float | None = None) -> dict:
    """锻刀串链视图；days<=0 表示全部。返回 forges（有 started 的炉，
    时间升序）与 orphan_collected（配不上的领取，时间升序）。"""
    from .telemetry import TELEMETRY_SCHEMA_VERSION
    now = float(now if now is not None else time.time())
    days = (int(days) if isinstance(days, (int, float)) and not isinstance(days, bool)
            else 0)
    from_ts = None if days <= 0 else now - days * 86400.0
    events = _fetch_events(store, from_ts)

    # 近侍观测全量取（登录才有，量小）：开炉时刻之前最近一条。
    secretary_events = [e for e in events if e["event_type"] == _SECRETARY]
    secretary_ts = [e["ts"] for e in secretary_events]

    def _secretary_at(ts: float) -> dict | None:
        index = bisect_right(secretary_ts, ts) - 1
        if index < 0:
            return None
        observed = secretary_events[index]
        sword_id = _num(observed["payload"].get("sword_id"))
        return {"sword_id": sword_id,
                "name": (_sword_name(sword_id, sword_db) if sword_id else None),
                "observed_at": observed["ts"]}

    # 每炉位把 started/collected 混一起按 (ts, id) 走：当前 started 收
    # 直到同炉位下一条 started 为止的全部 collected。
    streams: dict[int, list[dict]] = {}
    for event in events:
        if event["event_type"] == _SECRETARY:
            continue
        slot = _slot_no(event["payload"])
        if slot is None:
            continue  # 炉位号都没有，连归属都谈不上，不编
        streams.setdefault(slot, []).append(event)

    forges: list[dict] = []
    orphan_collected: list[dict] = []
    for slot in sorted(streams):
        open_forge: dict | None = None
        for event in streams[slot]:
            payload = event["payload"]
            if event["event_type"] == _FORGE_STARTED:
                if open_forge is not None:  # 上一条 started 没收到领取就翻新炉
                    forges.append(open_forge)
                open_forge = {
                    "slot_no": slot,
                    "started_at": event["ts"],
                    "recipe": _recipe(payload),
                    "secretary": _secretary_at(event["ts"]),
                    "_swords": [],
                    "_count": 0,
                    "_collected_at": None,
                }
                continue
            swords = _swords_from_collected(payload)
            if open_forge is None:
                orphan_collected.append({
                    "slot_no": slot,
                    "collected_at": event["ts"],
                    "count": _count_from_collected(payload, swords),
                    "swords": swords or None,
                })
                continue
            open_forge["_swords"].extend(swords)
            open_forge["_count"] += _count_from_collected(payload, swords)
            open_forge["_collected_at"] = event["ts"]
        if open_forge is not None:
            forges.append(open_forge)

    result_forges = []
    for forge in forges:
        swords = forge["_swords"]
        result_forges.append({
            "slot_no": forge["slot_no"],
            "started_at": forge["started_at"],
            "recipe": forge["recipe"],
            "secretary": forge["secretary"],
            "collected_at": forge["_collected_at"],
            "swords": swords if swords else None,
            "cost_est": _cost_est(forge["recipe"], forge["_count"]),
        })
    result_forges.sort(key=lambda row: row["started_at"])
    orphan_collected.sort(key=lambda row: row["collected_at"])
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "generated_at": now,
        "window": {"days": days if days > 0 else 0,
                   "from_ts": from_ts, "to_ts": now},
        "forges": result_forges,
        "orphan_collected": orphan_collected,
    }
