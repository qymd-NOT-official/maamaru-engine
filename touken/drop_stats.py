# -*- coding: utf-8 -*-
"""掉落统计：周回分母（手动 battle.completed + 面板逐圈记录）× 掉落分子
（sword.obtained 掉落收据）按地图/玩法分组的聚合视图。

诚实口径（没实测校准的判定一律不编，拿不准的给 null 并在这里写明）：

- 手动侧「哪格是王点」未校准（battle.completed 的 square_id 终点没有
  锤死），所以手动 battle.completed 只进 battles 分母，不产出任何
  boss_reached 判定；纯手动来源的地图组 boss_reached 一律 null。
- 面板侧 sortie 圈的 boss_reached 用 outcome 统计：completed = 到达王点
  （游戏机制：正常打完王点才算 completed），retreated_before_boss /
  interrupted / unknown 都不算。该计数不受 drop_observation 影响——
  委托自动行军跳过获得动画（not_observed）不代表没到王点。
- drop_observation=="not_observed" 的圈既不是「掉了」也不是「没掉」
  （touken/flows/sortie.py 的口径），整圈剔除出分母，也不进 boss_reached
  之外的任何统计。confirmed_none / recognized / 老数据没有该字段的圈
  照常进分母。
- battles 的口径混合两个来源，单位是「场」：
  · 手动 battle.completed 一条 = 1 场（客户端收据逐场记账）。
  · sortie 圈优先用 battle_count（结算页沿计数，实测口径）；读不出时
    completed 圈记 1（王点战必有结算页，这是游戏机制不是猜）；中断/
    结局未知的圈记 0（不编）。
  · 联队战回合用 battle_taps（战斗按钮点击数，一戳一场）；读不出记 0。
  · 大阪城一层按 1 场计（挖地一层一次击破战）；层内若有不止一场的
    情形未实测，这里不细分。
- 分子只认 sword.obtained 里 source ∈ {sortie.drop, battle.drop,
  raid.drop, osaka.drop} 的掉落收据；锻刀/收件箱/远征等来源不算掉落。
  圈记录里的 drops_recognized 只是数量，不当分子用（明细以
  sword.obtained 为准，同一圈两条口径不重复计）。
- 已知口径不对称（如实呈现，不悄悄修）：
  · 联队战/大阪城的分子来自客户端日志收据（手动打 + 面板代打都算），
    分母只来自面板跑的回合/层数——纯手动的联队战/大阪城只进分子。
  · 面板代打后又同步客户端日志时，同一场战斗会同时以「面板圈」和
    「battle.completed 收据」各进一次分母；大阪城代打的场次经日志回同步
    还会落到地图键（客户端日志分不清大阪城图和普图）而非「大阪城」组。
    两种重叠都没有可靠的判别字段，不做启发式剔除。
- 缺 chapter/map_no 进不了任何分组的事件（老收据丢路线、认人降级等）
  不静默丢弃，统一记进 unattributed，保证账能对上。
"""
from __future__ import annotations

import json
import time

from .telemetry import pair_loop_records

# 出阵圈配对所需的出发/结束事件 + 联队战回合 + 大阪城层
_LOOP_EVENT_TYPES = (
    "sortie.loop_started", "sortie.completed", "sortie.retreated_before_boss",
    "sortie.interrupted", "raid.round_completed", "osaka.floor_completed",
)
_MANUAL_BATTLE_TYPE = "battle.completed"
_OBTAINED_TYPE = "sword.obtained"
_DROP_SOURCES = frozenset(("sortie.drop", "battle.drop", "raid.drop", "osaka.drop"))

GROUP_KIND_MAP = "map"
GROUP_KIND_RAID = "raid"
GROUP_KIND_OSAKA = "osaka"
RAID_GROUP_KEY = "联队战"
OSAKA_GROUP_KEY = "大阪城"


def _int_or_none(value):
    """分组键用的数字清洗：bool/怪值/非数字串一律 None，不硬解析。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _map_key(chapter, map_no):
    chapter, map_no = _int_or_none(chapter), _int_or_none(map_no)
    if chapter is None or map_no is None:
        return None
    return f"{chapter}-{map_no}"


def _loop_battle_count(record: dict) -> int:
    """一圈对 battles 的贡献，口径见模块 docstring（不猜，读不出给 0/1 的
    依据都写在注释里）。"""
    count = record.get("battle_count")
    if isinstance(count, bool) or not isinstance(count, int):
        count = None
    if count is not None and count > 0:
        return count
    if count == 0:
        return 0  # 中断/撤退圈 0 场是合法真值
    # battle_count 读不出（completed 却数到 0 场 = 锚点失明，写库时置 null）
    if record.get("outcome") == "completed":
        return 1  # 王点战必有结算页（游戏机制），completed 圈至少这 1 场
    return 0


def _fetch_events(store, event_types, from_ts) -> list[dict]:
    clauses = "event_type IN (%s)" % ",".join("?" * len(event_types))
    args = list(event_types)
    if from_ts is not None:
        clauses += " AND ts >= ?"
        args.append(float(from_ts))
    rows = store._conn().execute(
        "SELECT id, ts, run_id, event_type, payload FROM events WHERE " + clauses +
        " ORDER BY id", args).fetchall()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        events.append({"id": row["id"], "ts": float(row["ts"]),
                       "run_id": row["run_id"],
                       "event_type": row["event_type"], "payload": payload})
    return events


def _new_group(kind: str, key: str) -> dict:
    # boss_reached 初值 0；只有「组里一条面板圈都没有」（纯手动组）输出时才
    # 给 null——圈被观测过就有资格报 0，纯手动组没资格判定才报 null。
    return {"key": key, "kind": kind, "battles": 0, "boss_reached": 0,
            "_drops": {}, "drop_total": 0, "_panel_loops": 0}


def _drop_rows(group: dict) -> list[dict]:
    rows = [{"name": name, "count": stats["count"],
             "first_get_count": stats["first_get_count"]}
            for name, stats in group["_drops"].items()]
    rows.sort(key=lambda row: (-row["count"], row["name"] or ""))
    return rows


def build_drop_stats(store, days: int = 30, now: float | None = None) -> dict:
    """按地图/玩法聚合窗口内的周回分母与掉落分子。days<=0 表示全部。"""
    from .telemetry import TELEMETRY_SCHEMA_VERSION
    now = float(now if now is not None else time.time())
    days = int(days) if isinstance(days, (int, float)) and not isinstance(days, bool) else 0
    from_ts = None if days <= 0 else now - days * 86400.0

    loop_events = _fetch_events(store, _LOOP_EVENT_TYPES, from_ts)
    manual_battles = _fetch_events(store, (_MANUAL_BATTLE_TYPE,), from_ts)
    obtained = [e for e in _fetch_events(store, (_OBTAINED_TYPE,), from_ts)
                if e["payload"].get("source") in _DROP_SOURCES]

    groups: dict[tuple[str, str], dict] = {}
    unattributed = {"battles": 0, "drop_total": 0}

    def _group(kind: str, key: str) -> dict:
        return groups.setdefault((kind, key), _new_group(kind, key))

    # ── 分母来源一：手动 battle.completed（逐场收据）──
    for event in manual_battles:
        key = _map_key(event["payload"].get("chapter"), event["payload"].get("map_no"))
        if key is None:
            unattributed["battles"] += 1
            continue
        _group(GROUP_KIND_MAP, key)["battles"] += 1

    # ── 分母来源二：面板逐圈记录（按 run 配对，剔除 not_observed 圈）──
    by_run: dict[str | None, list[dict]] = {}
    for event in loop_events:
        by_run.setdefault(event["run_id"], []).append(event)
    for run_events in by_run.values():
        # sortie 圈：出发 × 结束配对（与 run_summary 的 loop_records 同口径）
        for record in pair_loop_records(run_events):
            if record.get("drop_observation") == "not_observed":
                continue  # 观察不成立的圈不进掉率分母（sortie.py 口径）
            key = _map_key(record.get("chapter"), record.get("map_no"))
            if key is None:
                # 圈缺地图信息进不了组：按它贡献的战斗数记账，不静默丢
                unattributed["battles"] += _loop_battle_count(record)
                continue
            group = _group(GROUP_KIND_MAP, key)
            group["battles"] += _loop_battle_count(record)
            group["_panel_loops"] += 1
            if record.get("outcome") == "completed":
                group["boss_reached"] += 1
        # 联队战回合 / 大阪城层：无配对，一条事件一个单位
        for event in run_events:
            payload = event["payload"]
            if event["event_type"] == "raid.round_completed":
                group = _group(GROUP_KIND_RAID, RAID_GROUP_KEY)
                taps = payload.get("battle_taps")
                if isinstance(taps, bool) or not isinstance(taps, int) or taps < 0:
                    taps = 0  # 读不出不编，按 0 记账（事件仍在，明细可查）
                group["battles"] += taps
            elif event["event_type"] == "osaka.floor_completed":
                group = _group(GROUP_KIND_OSAKA, OSAKA_GROUP_KEY)
                group["battles"] += 1  # 一层按 1 场计（见模块 docstring）

    # ── 分子：sword.obtained 掉落收据 ──
    for event in obtained:
        payload = event["payload"]
        source = payload.get("source")
        if source in ("sortie.drop", "battle.drop"):
            key = _map_key(payload.get("chapter"), payload.get("map_no"))
            if key is None:
                unattributed["drop_total"] += 1
                continue
            group = _group(GROUP_KIND_MAP, key)
        elif source == "raid.drop":
            group = _group(GROUP_KIND_RAID, RAID_GROUP_KEY)
        else:  # osaka.drop
            group = _group(GROUP_KIND_OSAKA, OSAKA_GROUP_KEY)
        name = payload.get("name")
        name = str(name) if name is not None else None
        stats = group["_drops"].setdefault(name, {"count": 0, "first_get_count": 0})
        stats["count"] += 1
        if payload.get("is_first_get_sword"):
            stats["first_get_count"] += 1
        group["drop_total"] += 1

    # ── 收尾：boss_reached 定型 + 排序 + 去内部键 ──
    def _sort_key(item):
        (kind, key), _ = item
        if kind == GROUP_KIND_MAP:
            chapter, _, map_no = key.partition("-")
            return (0, int(chapter), int(map_no), "")
        return (1 if kind == GROUP_KIND_RAID else 2, 0, 0, key)

    result_groups = []
    for (_, _), group in sorted(groups.items(), key=_sort_key):
        row = {"key": group["key"], "kind": group["kind"],
               "battles": group["battles"],
               "boss_reached": (group["boss_reached"]
                                if group["_panel_loops"] else None),
               "drops": _drop_rows(group), "drop_total": group["drop_total"]}
        result_groups.append(row)

    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "generated_at": now,
        "window": {"days": days if days > 0 else 0,
                   "from_ts": from_ts, "to_ts": now},
        "groups": result_groups,
        "unattributed": unattributed,
    }
