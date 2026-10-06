# -*- coding: utf-8 -*-
"""练度档案：training.captured 快照的展示视图（总览 + 单振时间线 + 内番养成）。

数据只有 TelemetryStore 事件表里的 training.captured（收账管线落库，
payload.swords 每振 serial_id/sword_id/level/exp/ranbu_level/ranbu_exp）。
刀名一律不入库，展示层才拿 sword_id 走 sword_db + youzu_log._sword_name
解析；乱舞下一级换算走 data/ranbu_rules.json（髭切/膝丸特例 + 未实测
标注都在数据卡 _meta 里），need_swords_est 是公告口径估算不是实测值。

内番已喂数值（hp_up/scout_up 等 *_up）同样只在快照原文里出现才收；其中
生存(hp_up)/侦察(scout_up)是内番专属，打击/统率/机动/冲力/隐蔽可炼结喂，
所以内番视图只出这两项。
"""
from __future__ import annotations

import json

from . import ranbu_rules, sword_db
from .youzu_log import _sword_name

_EVENT_TYPE = "training.captured"

# 内番平台期判定窗口：该 *_up 值在快照链上「最近连续 K 条不增长」即报
# plateau=true。这是平台期启发式——内番上限表未校准，true 表示「连续多
# 次收账没再涨」，可能是喂满也可能是没喂，绝不等于「到官方上限」。
PLATEAU_K = 3


def _num(value):
    """事件 payload 里的数字清洗：bool/怪值一律 None，整数尽量保整数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not float(value).is_integer():
        return value
    return int(value)


def _training_events(store) -> list[dict]:
    """全部 training.captured 按时间升序；直查事件表，不吃
    recent_events 的 1001 条上限（快照链可能攒得比它长）。"""
    rows = store._conn().execute(
        "SELECT ts, payload FROM events WHERE event_type = ? ORDER BY ts, id",
        (_EVENT_TYPE,)).fetchall()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        events.append({"ts": float(row["ts"]), "payload": payload})
    return events


def _resolve_rarity(sword_id):
    """sword_db 基础稀有度（极化番号已映射回基础目录，天然符合 wiki
    「极按初的稀有度计」口径）；髭切/膝丸走数据卡特例。查不到 None。"""
    sid = _num(sword_id)
    if not sid:
        return None
    found = sword_db.find_game_sword(sid)
    if not found:
        return None
    catalog, info, _form = found
    return ranbu_rules.effective_rarity(catalog, info.get("rarity"))


def _sword_nums(raw: dict) -> dict:
    """单振快照字段清洗；读不出是 None，不编 0。"""
    return {key: _num(raw.get(key)) for key in
            ("serial_id", "sword_id", "level", "exp", "ranbu_level", "ranbu_exp",
             "hp_up", "scout_up")}


def _plateau(values: list[int], k: int = PLATEAU_K) -> bool | None:
    """平台期启发式：最近连续 k 条不增长（后者 <= 前者）→ True；
    有效读数不足 k 条 → None（判定不出，不猜）。值为 None 的快照帧
    不进该指标的比较链（缺读数不是 0）。"""
    if len(values) < k:
        return None
    tail = values[-k:]
    return all(later <= earlier for earlier, later in zip(tail, tail[1:]))


def build_training_overview(store) -> dict | None:
    """最新一条 training.captured 的全刀练度总览；没有快照返回 None。"""
    events = _training_events(store)
    if not events:
        return None
    latest = events[-1]
    captured_at = str(latest["payload"].get("captured_at") or "")
    swords = []
    for raw in latest["payload"].get("swords") or []:
        if not isinstance(raw, dict):
            continue
        nums = _sword_nums(raw)
        if nums["serial_id"] is None:
            continue
        swords.append({
            "serial_id": nums["serial_id"],
            "sword_id": nums["sword_id"],
            "name": _sword_name(nums["sword_id"], sword_db) if nums["sword_id"] else None,
            "level": nums["level"],
            "exp": nums["exp"],
            "ranbu_level": nums["ranbu_level"],
            "ranbu_exp": nums["ranbu_exp"],
            "ranbu_next": ranbu_rules.next_level_requirement(
                nums["ranbu_level"], nums["ranbu_exp"], _resolve_rarity(nums["sword_id"])),
            "captured_at": captured_at,
        })
    swords.sort(key=lambda row: row["serial_id"])
    return {"ts": latest["ts"], "captured_at": captured_at,
            "sword_count": len(swords), "swords": swords}


def build_training_history(store, serial_id) -> dict | None:
    """单振刀的 training.captured 快照链时间线（时间升序）；该编号从没
    进过快照返回 None。first_max_level_observed_at 是链上第一次观测到
    level=99 的 captured_at——「首次观测到」，不是「首次达成」
    （快照之间的实际达成时刻无从考证）。"""
    serial = _num(serial_id)
    if serial is None:
        return None
    timeline = []
    for event in _training_events(store):
        captured_at = str(event["payload"].get("captured_at") or "")
        for raw in event["payload"].get("swords") or []:
            if not isinstance(raw, dict):
                continue
            nums = _sword_nums(raw)
            if nums["serial_id"] != serial:
                continue
            timeline.append({"captured_at": captured_at, "ts": event["ts"],
                             "level": nums["level"], "exp": nums["exp"],
                             "ranbu_level": nums["ranbu_level"],
                             "ranbu_exp": nums["ranbu_exp"]})
            break  # 同一快照内 serial 唯一
    if not timeline:
        return None
    first_max = next((row["captured_at"] for row in timeline
                      if row["level"] == 99), None)
    return {"serial_id": serial, "timeline": timeline,
            "first_max_level_observed_at": first_max}


def build_internal_affairs(store) -> dict | None:
    """内番养成视图：最新快照在册的每振刀，列生存/侦察内番已喂数值与
    平台期启发式结论；没有快照返回 None。

    口径（诚实标注，见模块 docstring 与 docs/telemetry-data.md）：
    - 名册取最新一条 training.captured（已离开刀池的刀不出现）。
    - hp_up/scout_up 只在内番能喂；其他 *_up 炼结也能喂，不出。
    - hp_plateau/scout_plateau：该振自己的快照链（含它的帧，按时间序）
      上最近连续 PLATEAU_K 条不增长 → True；链上有效读数不足 K 条 →
      None（判定不出）。true 是「连续多次收账没再涨」，可能喂满也可能
      没喂——内番上限表未校准，绝不等于「到官方上限」。
    - 快照帧里该字段读不出（None）时不进比较链，不当 0 凑数。
    """
    events = _training_events(store)
    if not events:
        return None
    latest = events[-1]
    captured_at = str(latest["payload"].get("captured_at") or "")

    # 每振自己的 hp/scout 读数链（时间升序，None 帧跳过）
    chains: dict[int, dict[str, list[int]]] = {}
    for event in events:
        for raw in event["payload"].get("swords") or []:
            if not isinstance(raw, dict):
                continue
            nums = _sword_nums(raw)
            serial = nums["serial_id"]
            if serial is None:
                continue
            chain = chains.setdefault(serial, {"hp_up": [], "scout_up": []})
            for field in ("hp_up", "scout_up"):
                if nums[field] is not None:
                    chain[field].append(nums[field])

    swords = []
    for raw in latest["payload"].get("swords") or []:
        if not isinstance(raw, dict):
            continue
        nums = _sword_nums(raw)
        if nums["serial_id"] is None:
            continue
        chain = chains.get(nums["serial_id"], {"hp_up": [], "scout_up": []})
        swords.append({
            "serial_id": nums["serial_id"],
            "sword_id": nums["sword_id"],
            "name": (_sword_name(nums["sword_id"], sword_db)
                     if nums["sword_id"] else None),
            "hp_up": nums["hp_up"],
            "scout_up": nums["scout_up"],
            "hp_plateau": _plateau(chain["hp_up"]),
            "scout_plateau": _plateau(chain["scout_up"]),
            "captured_at": captured_at,
        })
    swords.sort(key=lambda row: row["serial_id"])
    return {"ts": latest["ts"], "captured_at": captured_at,
            "sword_count": len(swords), "plateau_k": PLATEAU_K,
            "swords": swords}
