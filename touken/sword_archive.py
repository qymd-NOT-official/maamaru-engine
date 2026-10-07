# -*- coding: utf-8 -*-
"""刀帐档案：机器盘点 + 人工标注的合并视图（build_sword_archive）。

与「当前本丸共用档案」同一份最新完整盘点做地基；人工标注
（sword_annotations）优先按游戏独立编号 serial_id 挂到具体一振。
旧指纹只有唯一对应时才持久绑定编号；未能绑定的 OCR 标注保留原匹配规则。
形态合并、要练标记、待人工清单与历史标注在这里合成。

铁律（与 honmaru_profile 同一套）：
  - 一振一行，同名多振保留；指纹撞车（一标注多行/一行多标注）不自动
    裁决，标 stale/duplicate_fingerprint 交回给人点；
  - 人工 form 与机器结论不同才算改判（form_overridden=True，机器原值
    留在 machine_form_status，机器证据保留）；一致只追加确认证据；
    改判后 form_status 是确定值，attention 的 form_unknown/form_ambiguous
    自然不再触发；
  - 人工等级只补空缺，永不覆盖机器读数（等级会随练级涨，人填的会过期）；
  - 绑定编号已从完整游戏名单消失，或旧完整盘点证明曾持有的条目，
    进入历史；其余未匹配旧标注仍进 attention，不猜具体处理方式。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from .honmaru_profile import (_entry_annotation_key, _annotation_index, _human_annotations,
                              build_honmaru_profile)

ARCHIVE_SCHEMA_VERSION = 1

# attention 排序：按刀帐番号升序（对齐游戏「刀帐顺序」，方便对照游戏
# 翻页核对，2026-09-21 老大点名）；番号认不出的排最后。
# 条内 reasons 的先后仍按添加顺序：形态 > 等级 > 打架 > 撞车 > 对不上号。
_CATALOG_ID_RE = re.compile(r"touken_(\d+)_")
_DAY_RE = re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})")


def _catalog_no(sword_catalog_id):
    """刀帐番号（touken_118_... → 118）；认不出返回 None。"""
    match = _CATALOG_ID_RE.match(str(sword_catalog_id or ""))
    return int(match.group(1)) if match else None


def _attention_sort_key(item: dict) -> tuple:
    """等你拿主意排序：刀帐番号升序（认不出的殿后），同番号按显现
    日期（老的在前），再按 observation_id 稳定收尾。"""
    no = _catalog_no(item.get("sword_catalog_id"))
    day = _parse_manifest_day(item.get("kiwame_date")) or date.max
    return (no is None, no or 0, day, item.get("observation_id") or "")


def _catalog_info(sword_catalog_id):
    """按目录 id（如 touken_011_...）反查名册 (id, info)；认不出 None。"""
    from . import sword_db
    match = _CATALOG_ID_RE.match(str(sword_catalog_id or ""))
    if not match:
        return None
    try:
        return sword_db.find_by_id(int(match.group(1)))
    except Exception:
        return None


def _catalog_display_name(sword_catalog_id) -> str:
    found = _catalog_info(sword_catalog_id)
    if found:
        return found[1].get("name_zh") or found[1].get("name") \
            or str(sword_catalog_id)
    return str(sword_catalog_id)


def _catalog_type(sword_catalog_id):
    found = _catalog_info(sword_catalog_id)
    value = found[1].get("type") if found else None
    return {"脇差": "胁差", "槍": "枪", "剣": "剑"}.get(value, value)


def _parse_manifest_day(value) -> date | None:
    """显现日期稳健解析（2024/5/1、2024-1-5 都行）；认不出返回 None。"""
    match = _DAY_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    try:
        return date(*[int(part) for part in match.groups()])
    except ValueError:
        return None


def _build_hints(entries: list) -> dict:
    """同名多振组的提示：等级最高（并列都给）/ 显现最早（并列都给）。

    返回 {observation_id: [hint, ...]}；单振组不出提示。日期解析不了就只
    跳过那条日期提示——不出就是不出，不猜。
    """
    groups = {}
    for entry in entries:
        groups.setdefault(entry.get("sword_catalog_id"), []).append(entry)
    hints = {}
    for catalog_id, rows in groups.items():
        if not catalog_id or len(rows) < 2:
            continue
        count = len(rows)
        levels = [r["level"] for r in rows
                  if isinstance(r.get("level"), int)
                  and not isinstance(r.get("level"), bool)]
        # 组内有等级没读出来的行时不出等级提示——缺一振的"最高"会误导人
        if len(levels) == len(rows):
            top = max(levels)
            for row in rows:
                if row.get("level") == top:
                    hints.setdefault(row["observation_id"], []).append(
                        f"同名 {count} 振中等级最高")
        parsed = [(row, _parse_manifest_day(row.get("kiwame_date")))
                  for row in rows]
        parsed = [(row, day) for row, day in parsed if day is not None]
        if parsed:
            earliest = min(day for _, day in parsed)
            for row, day in parsed:
                if day == earliest:
                    hints.setdefault(row["observation_id"], []).append(
                        "同名中显现最早")
    return hints


def build_sword_archive(store, profile=None) -> dict:
    """生成刀帐档案；候选池会把能唯一对应的旧标注绑定到游戏编号。

    机器形态结论直接复用 honmaru_profile 的完整管线（盘点落盘事实 +
    编队页直读 + 图鉴极标），人工合并在候选池输出前已完成（unknown ←
    人工确认；与机器不同的人工值改判机器结论，见 honmaru_profile
    _apply_human_confirmations）；本层负责挂 human 字段、stale/duplicate
    判定、attention 清单与同名提示。
    """
    profile = build_honmaru_profile(store) if profile is None else profile
    pool = profile.get("candidate_pool") or {}
    if not pool.get("done"):
        return {"done": False, "reason": pool.get("reason"),
                "observed_at": pool.get("observed_at"), "snapshot_id": None,
                "summary": {"total": 0, "human_confirmed": 0,
                            "keepers": 0, "attention_count": 0},
                "entries": [], "attention": []}

    entries = pool.get("entries") or []
    annotations = _human_annotations(store)
    index = _annotation_index(annotations)
    row_counts = Counter(
        _entry_annotation_key(entry, index)
        for entry in entries)
    for entry in entries:
        legacy = (entry.get("sword_catalog_id"), entry.get("kiwame_date"))
        if _entry_annotation_key(entry, index) != legacy:
            row_counts[legacy] += 1
    matched_ids = set()
    hints = _build_hints(entries)
    out_entries = []
    attention = []
    for entry in entries:
        key = _entry_annotation_key(entry, index)
        anns = index.get(key) or []
        matched_ids.update(ann.get("id") for ann in anns)
        row_hints = hints.get(entry.get("observation_id"), [])
        reasons = []
        form_status = entry.get("form_status") or "unknown"
        if form_status == "unknown":
            reasons.append("form_unknown")
        # 合并后等级仍是空缺（机器没读出、人工也没补）→ 等人来填
        if entry.get("level") is None:
            reasons.append("level_unknown")
        if form_status == "ambiguous":
            reasons.append("form_ambiguous")
        human = None
        if anns:
            collision = len(anns) > 1 or row_counts.get(key, 0) > 1
            human = {"id": anns[0].get("id"),
                     "form": anns[0].get("form_confirmed"),
                     "level": anns[0].get("level_confirmed"),
                     "keeper": bool(anns[0].get("keeper")),
                     # favorite/watch 与 keeper 一样是玩家偏好契约，本层
                     # 只透传，编队消费留待后续批次
                     "favorite": bool(anns[0].get("favorite")),
                     "watch": bool(anns[0].get("watch")),
                     "note": anns[0].get("note"),
                     "confirmed_at": anns[0].get("updated_at"),
                     "stale": collision}
            if collision:
                reasons.append("duplicate_fingerprint")
        out_entries.append({
            "observation_id": entry.get("observation_id"),
            "serial_id": entry.get("serial_id"),
            "data_source": entry.get("data_source") or "ocr",
            "observed_at": entry.get("observed_at"),
            "survival": entry.get("survival"), "survival_max": entry.get("survival_max"),
            "fatigue": entry.get("fatigue"), "locked": entry.get("locked"),
            "stats": entry.get("stats") or {},
            "equipment_serials": entry.get("equipment_serials") or {},
            "acquisition": entry.get("acquisition"),
            "sword_catalog_id": entry.get("sword_catalog_id"),
            "name_zh": entry.get("name_zh"),
            "sword_type": _catalog_type(entry.get("sword_catalog_id")),
            "level": entry.get("level"),
            "tou_level": entry.get("tou_level"),
            "exp": entry.get("exp"), "ranbu_exp": entry.get("ranbu_exp"),
            "hp_up": entry.get("hp_up"), "atk_up": entry.get("atk_up"),
            "def_up": entry.get("def_up"), "mobile_up": entry.get("mobile_up"),
            "back_up": entry.get("back_up"), "scout_up": entry.get("scout_up"),
            "hide_up": entry.get("hide_up"),
            "kiwame_date": entry.get("kiwame_date"),
            "form_status": form_status,
            "machine_form_status": entry.get("machine_form_status"),
            "form_overridden": bool(entry.get("form_overridden")),
            "form_evidence": entry.get("form_evidence") or [],
            "unknown_fields": entry.get("unknown_fields") or [],
            "human": human,
            "hints": row_hints,
        })
        if reasons:
            attention.append({
                "observation_id": entry.get("observation_id"),
                "serial_id": entry.get("serial_id"),
                "sword_catalog_id": entry.get("sword_catalog_id"),
                "name_zh": entry.get("name_zh"),
                "level": entry.get("level"),
                "kiwame_date": entry.get("kiwame_date"),
                "reasons": reasons,
                "hints": row_hints,
            })
    historical_annotations = []
    from .game_sword_archive import read_archive
    game_state = read_archive(store) if hasattr(store, "db_path") else None
    departures = (game_state or {}).get("departures") or {}
    from . import sword_db
    from .youzu_log import _sword_name
    sword_departures = [
        {"serial_id": int(serial), "ts": item.get("observed_at"),
         "reason": item.get("reason"),
         "name": _sword_name((item.get("sword") or {}).get("sword_id"), sword_db)
                 if (item.get("sword") or {}).get("sword_id") else None}
        for serial, item in departures.items()
    ]
    # 旧完整名单能证明曾持有、当前完整名单已经没有：保留标注，退出待核对。
    # 没有旧名单佐证的错误指纹仍待核对；不能猜它被用于乱舞、链结或刀解。
    current_fingerprints = {(entry.get("sword_catalog_id"), entry.get("kiwame_date"))
                            for entry in entries}
    for ann in annotations:
        if ann.get("id") in matched_ids:
            continue
        item = {
            "observation_id": None,
            "serial_id": ann.get("serial_id"),
            "departure_reason": (departures.get(str(ann.get("serial_id"))) or {}).get("reason"),
            "annotation_id": ann.get("id"),
            "sword_catalog_id": ann.get("sword_catalog_id"),
            "name_zh": _catalog_display_name(ann.get("sword_catalog_id")),
            "level": None,
            "kiwame_date": ann.get("kiwame_date"),
            "reasons": ["stale_annotation"],
            "hints": [],
        }
        serial = ann.get("serial_id")
        if serial is not None:
            # OCR 没有游戏独立编号，未匹配不代表这振已经离开本丸。
            absent = ((pool.get("source") or {}).get("kind") == "youzu_log"
                      or ((pool.get("source") or {}).get("kind") == "jp" and pool.get("roster_complete"))
                      or str(serial) in departures)
        else:
            absent = (ann.get("sword_catalog_id") and ann.get("kiwame_date")
                      and (ann["sword_catalog_id"], ann["kiwame_date"]) not in current_fingerprints
                      and hasattr(store, "previously_owned_sword")
                      and store.previously_owned_sword(
                          ann["sword_catalog_id"], ann["kiwame_date"],
                          pool.get("observed_at") or 0))
        if absent:
            historical_annotations.append(item)
        else:
            attention.append(item)
    attention.sort(key=_attention_sort_key)
    summary = {
        "total": len(out_entries),
        # 撞车（stale）的标注没生效，不算确认下来
        "human_confirmed": sum(1 for e in out_entries
                               if e["human"] and e["human"]["form"]
                               and not e["human"]["stale"]),
        "keepers": sum(1 for e in out_entries
                       if e["human"] and e["human"]["keeper"]
                       and not e["human"]["stale"]),
        "attention_count": len(attention),
    }
    return {"done": True, "reason": None,
            "observed_at": pool.get("observed_at"),
            "snapshot_id": (pool.get("source") or {}).get("snapshot_id"),
            "data_source": (pool.get("source") or {}).get("kind") or "ocr",
            "summary": summary, "entries": out_entries, "attention": attention,
            "historical_annotations": historical_annotations,
            "sword_departures": sword_departures}


def build_jp_sword_archive(store) -> dict:
    """日服独立名单转为共用刀帐契约，标注仍按独立编号合并。"""
    from .training_view import _training_events, current_training_roster
    from .honmaru_profile import _apply_human_confirmations
    from . import sword_db
    from .youzu_log import _sword_name
    current = current_training_roster(_training_events(store))
    entries = []
    payload = current["payload"] if current else {}
    for raw in payload.get("swords") or []:
        serial = raw.get("serial_id")
        if not serial:
            continue
        found = sword_db.find_game_sword(raw.get("sword_id") or 0)
        catalog, info, form = found if found else (None, {}, "unknown")
        entries.append({
            **raw, "observation_id": f"jp:{serial}", "data_source": "jp",
            "sword_catalog_id": catalog or f"jp_{raw.get('sword_id') or serial}",
            "name_zh": _sword_name(raw.get("sword_id"), sword_db),
            "tou_level": raw.get("ranbu_level"), "kiwame_date": None,
            "form_status": form, "machine_form_status": form,
            "form_evidence": ["游戏刀帐番号"] if found else [],
            "unknown_fields": ["level"] if raw.get("level") is None else [],
            "observed_at": current["ts"],
        })
    _apply_human_confirmations(entries, _human_annotations(store))
    result = build_sword_archive(store, {"candidate_pool": {
        "done": current is not None, "reason": "还没有日服所持名单",
        "entries": entries, "observed_at": current["ts"] if current else None,
        "source": {"kind": "jp"}, "roster_complete": payload.get("roster_complete", False),
    }})
    result["roster_complete"] = payload.get("roster_complete", False)
    return result


def get_sword_archive(store=None) -> dict:
    """后端消费者稳定入口：默认取全局 telemetry 库。

    档案生成失败不拖垮调用方——返回 done=False 的骨架并注明错误。"""
    if store is None:
        from .telemetry import get_telemetry_store
        store = get_telemetry_store()
    try:
        return build_sword_archive(store)
    except Exception as exc:
        return {"done": False, "reason": f"刀帐档案生成失败：{exc}",
                "observed_at": None, "snapshot_id": None,
                "summary": {"total": 0, "human_confirmed": 0,
                            "keepers": 0, "attention_count": 0},
                "entries": [], "attention": []}
