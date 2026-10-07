# -*- coding: utf-8 -*-
"""日报：把一天的 telemetry 事件翻成婶婶看得懂的一页小结。

五个小节：今日收支 / 今日掉落 / 今日练度 / 目标进度 / 今日出勤。
数据源只有 TelemetryStore（事件自身时间戳，绝不拿生成时间冒充数据时间）。
按 Asia/Shanghai 的 0 点切日——跨天边界按事件发生在哪天归哪天。

小节各自容错：某一节查询或解析翻车只放空那一节（degraded 里留名），
绝不让整份日报炸掉——telemetry 丢了不许拖垮玩家看账。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows 无 tzdata 时兜底：上海 1991 年后无夏令时，固定 +8 够用
    _TZ = timezone(timedelta(hours=8))

from .runtime_paths import STATUS_DIR
from .telemetry import _resource_reading

DAILY_REPORT_SCHEMA_VERSION = 1

# 归因条目列表最多铺这么多条（按金额绝对值取大头），总额不受限
_MAX_ATTRIBUTION_ENTRIES = 20
# 经验增长榜行数
_EXP_TOP_LIMIT = 5

_LEDGER_RESOURCE_ORDER = ("木炭", "玉钢", "冷却材", "砥石", "小判", "甲州金",
                          "委托符", "加速符")

_DROP_EVENT_TYPES = ("sword.obtained", "forge.collected")


def _today() -> date:
    return datetime.now(_TZ).date()


def _day_window(day: date) -> tuple[float, float]:
    """[start, end)：上海时区自然日，end 排他。"""
    start = datetime.combine(day, time.min, tzinfo=_TZ)
    return start.timestamp(), start.timestamp() + 86400


def _num(value):
    """事件 payload 里的数字清洗：bool/怪值一律 None，整数尽量保整数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not float(value).is_integer():
        return value
    return int(value)


def _day_events(store, event_types: tuple[str, ...], start_ts: float,
                end_ts: float) -> list[dict]:
    """直查事件表按窗口过滤，不吃 recent_events 的 1001 条上限。"""
    marks = ",".join("?" * len(event_types))
    rows = store._conn().execute(
        "SELECT id, ts, run_id, script, event_type, payload FROM events "
        f"WHERE ts >= ? AND ts < ? AND event_type IN ({marks}) "
        "ORDER BY ts, id", (start_ts, end_ts, *event_types)).fetchall()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        events.append({"id": row["id"], "ts": float(row["ts"]),
                       "run_id": row["run_id"], "script": row["script"],
                       "event_type": row["event_type"], "payload": payload})
    return events


def _last_event_before(store, event_type: str, before_ts: float) -> dict | None:
    row = store._conn().execute(
        "SELECT id, ts, payload FROM events WHERE event_type = ? AND ts < ? "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (event_type, before_ts)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, ValueError):
        payload = {}
    return {"id": row["id"], "ts": float(row["ts"]),
            "payload": payload if isinstance(payload, dict) else {}}


# ── 今日收支 ──

# note 为空的 resource.change（工作流自己记的账）按 source 给人话标签；
# 不在表里的来源不硬猜，留空让前端显示「来源未确认」。
_ENTRY_SOURCE_LABELS = {
    "forge.started": "锻刀",
    "forge.tenren": "十连锻刀",
    "dismantle.completed": "刀解",
    "task_rewards.reward_popup": "任务奖励",
    "repair.confirm_screen": "手入",
}


def _entry_label(payload: dict, resource: str, delta) -> str:
    """收支条目的展示标签：优先 note（剥掉尾巴上重复的「资源 +N」），
    没有 note 按 source 翻译，都不认识就留空（= 来源未确认）。"""
    note = str(payload.get("note") or "").strip()
    if note:
        # note 的尾巴是写入时的原文（youzu_log: 「远征完成·三队·B2 木炭 +1200」）；
        # 资源名用 payload 原值（可能带「·极」）；金额按整数百般匹配，非整浮点不硬凑
        original = str(payload.get("resource") or resource)
        amounts = {delta}
        if isinstance(delta, float) and delta.is_integer():
            amounts.add(int(delta))
        candidates = [f" {original} {amount:+d}" for amount in amounts
                      if isinstance(amount, int)
                      or (isinstance(amount, float) and amount.is_integer())]
        for suffix in candidates:
            if note.endswith(suffix):
                return note[: -len(suffix)].strip()
        return note
    return _ENTRY_SOURCE_LABELS.get(str(payload.get("source") or ""), "")


def _build_resources(store, start_ts: float, end_ts: float) -> dict | None:
    changes = _day_events(store, ("resource.change",), start_ts, end_ts)
    captures = _day_events(store, ("inventory.captured",), start_ts, end_ts)
    if not changes and not captures:
        return None
    net: dict[str, float] = {}
    entries = []
    for event in changes:
        payload = event["payload"]
        resource = str(payload.get("resource") or "")
        if resource == "加速符·极":
            resource = "加速符"
        delta = _num(payload.get("delta"))
        if not resource or delta is None or not delta:
            continue
        net[resource] = net.get(resource, 0) + delta
        entries.append({
            "ts": event["ts"], "resource": resource, "delta": delta,
            "label": _entry_label(payload, resource, delta),
            "note": str(payload.get("note") or "").strip(),
            "source": str(payload.get("source") or ""),
            "attribution": str(payload.get("attribution") or ""),
        })
    entries.sort(key=lambda item: -abs(item["delta"]))
    groups = {}
    for entry in entries:
        key = (entry["label"], entry["source"], entry["attribution"])
        group = groups.setdefault(key, {"label": entry["label"], "source": entry["source"],
            "attribution": entry["attribution"], "count": 0, "net": {}})
        group["count"] += 1
        resource = entry["resource"]
        group["net"][resource] = group["net"].get(resource, 0) + entry["delta"]
    truncated = max(0, len(entries) - _MAX_ATTRIBUTION_ENTRIES)
    entries = entries[:_MAX_ATTRIBUTION_ENTRIES]

    def _balance(event: dict | None) -> dict | None:
        if event is None:
            return None
        reading = _resource_reading(event["payload"].get("resources"))
        values = {name: _num(value) for name, value in reading.items()}
        return {"ts": event["ts"],
                "captured_at": str(event["payload"].get("captured_at") or ""),
                "resources": {name: value for name, value in values.items()
                              if value is not None}}

    return {
        "net": {name: net[name] for name in _LEDGER_RESOURCE_ORDER if name in net},
        "groups": list(groups.values()),
        "opening": _balance(captures[0] if captures else None),
        "closing": _balance(captures[-1] if captures else None),
        "entries": entries,
        "entry_total": len(entries) + truncated,
        "truncated": truncated,
    }


# ── 今日掉落 ──


def _drop_group_label(source: str, chapter, map_no) -> str:
    if source == "raid.drop":
        return "联队战"
    if source == "osaka.drop":
        return "大阪城"
    if source in ("sortie.drop", "battle.drop") and chapter and map_no:
        return f"{chapter}-{map_no}"
    if source == "forge" or source.startswith("forge."):
        return "锻刀"
    if source == "pumpkin.sword_obtained" or source == "pumpkin":
        return "季节活动"
    if source == "inbox.claim" or source.startswith("inbox."):
        return "收件箱"
    return "其他"


def _build_drops(store, start_ts: float, end_ts: float) -> dict | None:
    events = _day_events(store, _DROP_EVENT_TYPES, start_ts, end_ts)
    items = []
    for event in events:
        payload = event["payload"]
        source = str(payload.get("source") or event["event_type"])
        if event["event_type"] == "forge.collected":
            swords = payload.get("swords")
            if not isinstance(swords, list) or not swords:
                continue
            for row in swords:
                if not isinstance(row, dict):
                    continue
                items.append({
                    "name": str(row.get("name") or "").strip(),
                    "sword_id": _num(row.get("sword_id")),
                    "source": source, "chapter": None, "map_no": None,
                    "is_first_get_sword": bool(row.get("is_first_get_sword")),
                    "ts": event["ts"],
                })
            continue
        name = str(payload.get("name") or "").strip()
        if not name:
            continue
        chapter = _num(payload.get("chapter"))
        map_no = _num(payload.get("map_no"))
        items.append({
            "name": name,
            "sword_id": _num(payload.get("sword_id")),
            "source": source, "chapter": chapter, "map_no": map_no,
            "is_first_get_sword": bool(payload.get("is_first_get_sword")),
            "ts": event["ts"],
        })
    if not items:
        return None
    groups: dict[str, dict] = {}
    for item in items:
        label = _drop_group_label(item["source"], item["chapter"], item["map_no"])
        group = groups.setdefault(label, {"label": label, "swords": []})
        group["swords"].append({
            "name": item["name"],
            "is_first_get_sword": item["is_first_get_sword"],
            "ts": item["ts"],
        })
    for group in groups.values():
        group["swords"].sort(key=lambda row: row["ts"])
        group["count"] = len(group["swords"])
        group["first_get_count"] = sum(
            1 for row in group["swords"] if row["is_first_get_sword"])
    ordered = sorted(groups.values(),
                     key=lambda row: (-row["count"], row["label"]))
    return {
        "groups": ordered,
        "total": len(items),
        "first_get_total": sum(1 for item in items if item["is_first_get_sword"]),
    }


# ── 今日练度 ──


def _sword_name_resolver():
    """sword_id → 刀名；名册查不到就老实写「刀帐N」，不瞎编。"""
    from . import sword_db, youzu_log

    def resolve(sword_id):
        sid = _num(sword_id)
        if sid is None:
            return "刀帐?"
        return youzu_log._sword_name(sid, sword_db)

    return resolve


def _training_rows(payload: dict) -> dict[int, dict]:
    rows = {}
    swords = payload.get("swords")
    if not isinstance(swords, list):
        return rows
    for raw in swords:
        if not isinstance(raw, dict):
            continue
        serial = _num(raw.get("serial_id"))
        if serial is None:
            continue
        rows[serial] = {
            "sword_id": _num(raw.get("sword_id")),
            "level": _num(raw.get("level")),
            "exp": _num(raw.get("exp")),
            "ranbu_level": _num(raw.get("ranbu_level")),
            "ranbu_exp": _num(raw.get("ranbu_exp")),
        }
    return rows


def _build_training(store, start_ts: float, end_ts: float) -> dict | None:
    snapshots = _day_events(store, ("training.captured",), start_ts, end_ts)
    if not snapshots:
        return None
    current_event = snapshots[-1]
    current = _training_rows(current_event["payload"])
    previous_event = _last_event_before(store, "training.captured",
                                        current_event["ts"])
    if current_event["payload"].get("source") in ("jp_listener", "jp_netlog"):
        from .training_view import _training_events, current_training_roster
        chain = [event for event in _training_events(store)
                 if event["ts"] <= current_event["ts"]]
        current_event = current_training_roster(chain)
        current = _training_rows(current_event["payload"])
        previous_event = current_training_roster(chain[:-1])
    base = {
        "snapshot_ts": current_event["ts"],
        "snapshot_captured_at": str(
            current_event["payload"].get("captured_at") or ""),
        "previous_ts": None, "previous_captured_at": None,
        "has_previous": False,
        "sword_count": len(current),
        "level_ups": [], "ranbu_ups": [], "exp_top": [],
    }
    if previous_event is None:
        return base
    previous = _training_rows(previous_event["payload"])
    base["previous_ts"] = previous_event["ts"]
    base["previous_captured_at"] = str(
        previous_event["payload"].get("captured_at") or "")
    base["has_previous"] = True
    resolver = _sword_name_resolver()
    level_ups, ranbu_ups, exp_rows = [], [], []
    for serial, cur in current.items():
        name = resolver(cur.get("sword_id"))
        prev = previous.get(serial)
        level_from = prev.get("level") if prev else None
        ranbu_from = prev.get("ranbu_level") if prev else None
        exp_from = prev.get("exp") if prev else None
        if (level_from is not None and cur["level"] is not None
                and cur["level"] > level_from):
            level_ups.append({"name": name, "serial_id": serial,
                              "from": level_from, "to": cur["level"]})
        if (ranbu_from is not None and cur["ranbu_level"] is not None
                and cur["ranbu_level"] > ranbu_from):
            ranbu_ups.append({"name": name, "serial_id": serial,
                              "from": ranbu_from, "to": cur["ranbu_level"]})
        exp_gain = (cur["exp"] or 0) - (exp_from or 0)
        exp_rows.append({"name": name, "serial_id": serial,
                         "exp_gain": exp_gain, "exp": cur["exp"]})
    base["level_ups"] = sorted(level_ups, key=lambda row: (-(row["to"] - row["from"]), row["name"]))
    base["ranbu_ups"] = sorted(ranbu_ups, key=lambda row: (-(row["to"] - row["from"]), row["name"]))
    base["exp_top"] = sorted(exp_rows, key=lambda row: (-row["exp_gain"], row["name"]))[:_EXP_TOP_LIMIT]
    return base


# ── 目标进度 ──


def _build_goals(store, day: date) -> list[dict] | None:
    """只展示当前目标；未保存历史目标快照时不倒推过去的进度。"""
    if day != _today():
        return None
    from . import advisor
    now = datetime.combine(day, time(hour=12), tzinfo=_TZ)
    planning = advisor.get_planning(
        store, STATUS_DIR / advisor.GOALS_FILENAME, now=now)
    goals = []
    for goal in planning.get("goals") or []:
        if not isinstance(goal, dict):
            continue
        deadline = goal.get("deadline")
        if deadline and date.fromisoformat(str(deadline)[:10]) < day:
            continue
        goals.append({
            "id": goal.get("id"), "kind": goal.get("kind") or "resource",
            "resource": goal.get("resource"),
            "fragment": goal.get("fragment"),
            "event": goal.get("event"),
            "target": _num(goal.get("target")),
            "deadline": goal.get("deadline"),
            "status": str(goal.get("status") or "unknown"),
            "message": str(goal.get("message") or ""),
            "note": str(goal.get("note") or ""),
        })
    return goals


# ── 今日出勤 ──


def _build_attendance(store, start_ts: float, end_ts: float) -> list[dict] | None:
    runs = store.runs_between(start_ts, end_ts)
    # 旧运行缺收尾时间不代表它持续到今天；不改历史记录，只过滤日报。
    runs = [run for run in runs if (run.get("started_at") or 0) >= start_ts
            or run.get("ended_at") is not None]
    if not runs:
        return None
    return [{
        "run_id": run.get("run_id"), "script": run.get("script"),
        "label": run.get("label"), "status": run.get("status"),
        "started_at": run.get("started_at"), "ended_at": run.get("ended_at"),
    } for run in sorted(runs, key=lambda row: row.get("started_at") or 0)]


# ── 汇总入口 ──


def _coerce_day(value) -> date:
    """date 参数入口：None=今天，str='YYYY-MM-DD'，其余当 date 用。"""
    if value is None:
        return _today()
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    return value


def build_daily_report(store, date=None) -> dict:
    """聚合指定日期（默认今天，Asia/Shanghai）的日报。

    date 接受 date 或 'YYYY-MM-DD' 字符串；返回 dict 保证 JSON 可序列化。
    每个小节独立容错：单节失败只留空该节，并在 degraded 里留名。
    """
    day = _coerce_day(date)
    start_ts, end_ts = _day_window(day)
    report: dict = {
        "schema_version": DAILY_REPORT_SCHEMA_VERSION,
        "date": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "window": {"start": start_ts, "end": end_ts},
        "resources": None, "drops": None, "training": None,
        "goals": None, "attendance": None,
        "degraded": [],
    }
    builders = (
        ("resources", lambda: _build_resources(store, start_ts, end_ts)),
        ("drops", lambda: _build_drops(store, start_ts, end_ts)),
        ("training", lambda: _build_training(store, start_ts, end_ts)),
        ("goals", lambda: _build_goals(store, day)),
        ("attendance", lambda: _build_attendance(store, start_ts, end_ts)),
    )
    for section, build in builders:
        try:
            report[section] = build()
        except Exception:
            report[section] = None
            report["degraded"].append(section)
    return report
