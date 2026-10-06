# -*- coding: utf-8 -*-
"""入手履历：单振刀的已确认事实时间线（入手/修行进出/首次观测满级）。

诚实口径（没实测校准的判定一律不编，拿不准的给 null 并在这里写明）：

- 入手事实有两个来源，分开记账、合并展示：刀帐档案（youzu_sword_archive）
  的 created_at 是游戏给的「首次获得日」；sword.obtained 是收据（来源/
  地图/是否初入手/acquired_at）。档案在、收据不在（收据白名单漏抓）→
  一条 obtained，收据字段 null；反之亦然。哪段都没有就没有哪段，不编。
- acquired_at（收据原文，数字或 "YYYY-MM-%d %H:%M:%S" 字符串）解析不出
  来时，排序 ts 退化为事件 ts（观测时刻），acquired_at 原样保留。
- 档案 created_at 同理：解析出来用它的 Unix 秒当 ts，解析不出 ts 给
  None（排最后），原文照常展示。
- 修行：kiwame.departed/kiwame.returned 都是客户端收据事实；ts 一律取
  事件 ts（观测时刻）。returned 的 finished_at 是游戏给的完成时间原文，
  字段形态未逐项实测，原样带出不解析。
- 满级：复用 training_view 的快照链口径——first_max_level_observed
  是「链上首次观测到 level=99」的快照时刻，不是「首次达成」（两次快照
  之间什么时候到的 99 无从考证）。乱舞/等级里程碑不在这里推，快照链
  history API 已有。
- 修行送修的请求字段名未实测（白名单采集），departed 可能很少或没有；
  缺段不补。timeline 一条都凑不出返回 None（路由 404）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from .game_sword_archive import read_archive
from .training_view import build_training_history

_EVENT_TYPES = ("sword.obtained", "kiwame.departed", "kiwame.returned")
_SH = timezone(timedelta(hours=8))


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not float(value).is_integer():
        return None
    return int(value)


def _parse_time(value) -> float | None:
    """游戏时间原文（数字 Unix 秒或 +08:00 的 "YYYY-MM-DD HH:MM:SS"）
    → Unix 秒；认不出 None，调用方决定退化口径。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_SH).timestamp()
    except ValueError:
        return None


def _fetch_life_events(store, serial: int) -> list[dict]:
    rows = store._conn().execute(
        "SELECT ts, event_type, payload FROM events WHERE event_type IN (?,?,?) "
        "ORDER BY ts, id", _EVENT_TYPES).fetchall()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            continue
        if _num(payload.get("serial_id")) != serial:
            continue
        events.append({"ts": float(row["ts"]), "event_type": row["event_type"],
                       "payload": payload})
    return events


def build_sword_journal(store, serial_id) -> dict | None:
    """单振刀履历时间线（ts 升序，ts 解析不出的排最后）；一条事实都没
    有返回 None。"""
    from .telemetry import TELEMETRY_SCHEMA_VERSION
    serial = _num(serial_id)
    if serial is None:
        return None
    timeline: list[dict] = []

    # ── 入手：刀帐档案 created_at + sword.obtained 收据 ──
    archive_row = None
    archive = read_archive(store)
    if archive:
        row = (archive.get("swords") or {}).get(str(serial))
        if isinstance(row, dict):
            archive_row = row
    life_events = _fetch_life_events(store, serial)
    receipts = [e for e in life_events if e["event_type"] == "sword.obtained"]
    archive_created_at = (str(archive_row.get("created_at")).strip()
                          if archive_row and archive_row.get("created_at")
                          else None)

    def _receipt_detail(payload: dict) -> dict:
        return {
            "name": (str(payload["name"]) if payload.get("name") is not None
                     else None),
            "sword_id": _num(payload.get("sword_id")),
            "source": (str(payload["source"]) if payload.get("source")
                       is not None else None),
            "chapter": _num(payload.get("chapter")),
            "map_no": _num(payload.get("map_no")),
            "is_first_get_sword": (payload.get("is_first_get_sword")
                                   if isinstance(payload.get("is_first_get_sword"), bool)
                                   else None),
            "acquired_at": payload.get("acquired_at"),
            "archive_created_at": archive_created_at,
        }

    for event in receipts:
        payload = event["payload"]
        ts = _parse_time(payload.get("acquired_at"))
        timeline.append({"ts": ts if ts is not None else event["ts"],
                         "kind": "obtained", "detail": _receipt_detail(payload)})
    if not receipts and archive_created_at:
        ts = _parse_time(archive_created_at)
        timeline.append({"ts": ts, "kind": "obtained", "detail": {
            "name": None, "sword_id": _num(archive_row.get("sword_id")),
            "source": None, "chapter": None, "map_no": None,
            "is_first_get_sword": None, "acquired_at": None,
            "archive_created_at": archive_created_at,
        }})

    # ── 修行：departed / returned（ts = 观测时刻）──
    for event in life_events:
        if event["event_type"] == "kiwame.departed":
            timeline.append({"ts": event["ts"], "kind": "departed",
                             "detail": {}})
        elif event["event_type"] == "kiwame.returned":
            timeline.append({"ts": event["ts"], "kind": "returned",
                             "detail": {"finished_at":
                                        event["payload"].get("finished_at")}})

    # ── 首次观测满级：练度快照链口径（training_view 复用，不重复推）──
    training = build_training_history(store, serial)
    if training:
        first_max = next((row for row in training["timeline"]
                          if row.get("level") == 99), None)
        if first_max:
            timeline.append({"ts": first_max["ts"],
                             "kind": "max_level_observed",
                             "detail": {"captured_at": first_max["captured_at"],
                                        "level": 99}})

    if not timeline:
        return None
    timeline.sort(key=lambda row: (row["ts"] is None, row["ts"] or 0))
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "generated_at": time.time(),
        "serial_id": serial,
        "timeline": timeline,
    }
