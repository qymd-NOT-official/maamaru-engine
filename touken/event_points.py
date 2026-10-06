# -*- coding: utf-8 -*-
"""活动点数历史：activity.calendar 的活动清单 + inventory.captured 里
「活动点数·{event_id}」读数按活动期切片的时间线。

诚实口径（没实测校准的判定一律不编，拿不准的给 null 并在这里写明）：

- 活动清单只认最新一条 activity.calendar（日历是整体事实，新事实落新
  事件）；原样带 event_id/type/start_at/end_at，本地没有 event_id →
  活动名的映射，名字留给前端/活动时间轴（panel 已有活动知识）。
- 时间线切片窗口 = [start_at, end_at + 1 天]：读数在活动结束后 1 天内
  仍算收尾读数（收账往往落在活动刚结束的窗口）；start_at 之前没有
   grace，活动还没开始的读数不进线。
- start_at/end_at 是游戏给的服务器时间原文（国服 +08:00），同时兼容
  "2026-10-01 00:00:00" 和 ISO "2026-10-01T00:00:00" 两种写法；两个都
  解析不出来就抛 ValueError——切片窗口都不成立，硬切是不诚实。
- 点数读数只收非负整数；读不出的帧跳过，不补 0 不插值，前端画断点。
- 活动日历里没有这个 event_id → 返回 None（路由给 404），不猜时间窗。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

_CALENDAR = "activity.calendar"
_INVENTORY = "inventory.captured"
_POINT_KEY = "活动点数·{event_id}"
# 收尾读数宽限：活动结束后 1 天内的读数仍算该活动的收尾读数
END_GRACE_SECONDS = 86400.0
_SH = timezone(timedelta(hours=8))


def _parse_server_time(value) -> float | None:
    """游戏服务器时间原文 → Unix 秒；两种写法都认，认不出 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_SH)
            return parsed.timestamp()
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_SH).timestamp()
    except ValueError:
        return None


def _latest_calendar(store) -> dict | None:
    row = store._conn().execute(
        "SELECT ts, payload FROM events WHERE event_type = ? "
        "ORDER BY ts DESC, id DESC LIMIT 1", (_CALENDAR,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, ValueError):
        payload = {}
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return None
    return {"observed_at": float(row["ts"]), "events": events}


def build_event_points_list(store) -> dict:
    """最新一条活动日历的 events 原样列出（event_id → 活动名本地没有，
    原样给 id）；从没同步过日历返回空清单。"""
    from .telemetry import TELEMETRY_SCHEMA_VERSION
    calendar = _latest_calendar(store)
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "generated_at": time.time(),
        "observed_at": calendar["observed_at"] if calendar else None,
        "events": calendar["events"] if calendar else [],
    }


def build_event_points_timeline(store, event_id) -> dict | None:
    """单活动的点数时间线（时间升序）；日历里没有该活动返回 None。"""
    from .telemetry import TELEMETRY_SCHEMA_VERSION
    event_id = str(event_id or "").strip()
    calendar = _latest_calendar(store)
    if not calendar:
        return None
    activity = next((e for e in calendar["events"]
                     if isinstance(e, dict)
                     and str(e.get("event_id")) == event_id), None)
    if activity is None:
        return None
    start_ts = _parse_server_time(activity.get("start_at"))
    end_ts = _parse_server_time(activity.get("end_at"))
    if start_ts is None or end_ts is None:
        raise ValueError(
            f"活动 {event_id} 的起止时间解析不出来（start_at="
            f"{activity.get('start_at')!r}, end_at={activity.get('end_at')!r}），"
            "切不出时间窗。")
    window_to = end_ts + END_GRACE_SECONDS

    rows = store._conn().execute(
        "SELECT ts, payload FROM events WHERE event_type = ? "
        "AND ts >= ? AND ts <= ? ORDER BY ts, id",
        (_INVENTORY, start_ts, window_to)).fetchall()
    timeline = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            continue
        resources = payload.get("resources") if isinstance(payload, dict) else None
        if not isinstance(resources, dict):
            continue
        value = resources.get(_POINT_KEY.format(event_id=event_id))
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value < 0):
            continue  # 读数读不出/是怪值：跳过，不补 0 不插值
        timeline.append({
            "ts": float(row["ts"]),
            "captured_at": str(payload.get("captured_at") or "") or None,
            "points": int(value),
        })
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "generated_at": time.time(),
        "event_id": event_id,
        "type": activity.get("type"),
        "start_at": activity.get("start_at"),
        "end_at": activity.get("end_at"),
        "window": {"from_ts": start_ts, "to_ts": window_to,
                   "grace_seconds": END_GRACE_SECONDS},
        "timeline": timeline,
    }
