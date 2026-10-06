"""所持刀剑的游戏观察：完整名单定成员，局部响应只更新已知实例。"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .youzu_log import _event_epoch

SCHEMA = 1
FIELDS = {
    "sword_id", "serial_id", "level", "exp", "ranbu_level", "ranbu_exp",
    "hp", "hp_max", "fatigue",
    "protect", "created_at", "atk", "def", "mobile", "back", "hide", "scout", "loyalties",
    "hp_up", "atk_up", "def_up", "mobile_up", "back_up", "scout_up", "hide_up",
    "horse_serial_id", "equip_serial_id1", "equip_serial_id2", "equip_serial_id3",
    "artifact_serial_id1", "artifact_serial_id2",
}


def archive_path(store):
    from .runtime_paths import LOG_DIR, STATE_DIR
    path = Path(store.db_path)
    return (STATE_DIR / "youzu_sword_archive.json" if path.parent.resolve() == LOG_DIR.resolve()
            else path.with_suffix(".swords.json"))


def read_archive(store):
    try:
        data = json.loads(archive_path(store).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and data.get("schema") == SCHEMA and data.get("complete_at")
                and isinstance(data.get("complete_at"), (int, float))
                and _valid_rows(data.get("swords")) is not None
                and all(str(row.get("serial_id")) == str(serial)
                        and isinstance(row.get("observed_at"), (int, float))
                        for serial, row in data["swords"].items())):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _integer(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _valid_rows(rows):
    if not isinstance(rows, dict):
        return None
    result = {}
    for value in rows.values():
        if not isinstance(value, dict):
            return None
        serial, sword_id = _integer(value.get("serial_id")), _integer(value.get("sword_id"))
        if not serial or serial < 0 or not sword_id or sword_id < 0 or str(serial) in result:
            return None
        result[str(serial)] = {key: value[key] for key in FIELDS if key in value}
    return result


def update_archive(events, previous=None):
    state = copy.deepcopy(previous) if previous else {"schema": SCHEMA, "complete_at": None, "swords": {}}
    pending = {}
    operations = {
        "/composition/compose": ("链结", "base_id", "material_id"),
        "/composition/union": ("习合", "base_serial_id", "material_serial_id"),
        "/sword/dismantle_many": ("刀解", None, "serial_ids"),
    }
    for event in events:
        p = event.get("payload")
        endpoint = event.get("endpoint")
        if endpoint in operations and event.get("direction") == "C->S":
            # 同接口存在多个未完成请求时，无法可靠配对，宁可等完整名单。
            pending[endpoint] = None if endpoint in pending else event
            continue
        request = pending.pop(endpoint, None) if endpoint in operations else None
        if (event.get("direction") != "S->C" or event.get("status") != 200
                or not isinstance(p, dict) or str(p.get("status", 0)) != "0"):
            continue
        ts = _event_epoch(event)
        if not ts:
            continue
        if request and state.get("complete_at"):
            if str(p.get("status")) != "0":
                continue
            request_ts = _event_epoch(request)
            # 没有配对请求、失败响应、编号缺失或本体不一致都不能移除材料。
            label, base_key, material_key = operations[endpoint]
            params = request.get("payload") or {}
            raw = params.get(material_key) if isinstance(params, dict) else None
            parts = str(raw).split(",") if raw is not None else []
            serials = [_integer(part) for part in parts]
            base = _integer(params.get(base_key)) if base_key else None
            target = _valid_rows({"target": p.get("sword")}) if base_key else None
            if (request_ts and 0 <= ts - request_ts <= 60 and serials
                    and all(serial and serial > 0 for serial in serials)
                    and len(set(serials)) == len(serials)
                    and (not base_key or (base and base not in serials and target
                                         and set(target) == {str(base)}))):
                departures = state.setdefault("departures", {})
                for serial in serials:
                    key = str(serial)
                    old = state["swords"].get(key)
                    gone = departures.get(key)
                    if (gone and gone.get("observed_at", 0) >= ts):
                        continue
                    if old and old.get("observed_at", 0) > request_ts:
                        continue
                    departures[key] = {"reason": label, "observed_at": ts,
                                       "base_serial_id": base,
                                       "sword": old or (gone or {}).get("sword")}
                    state["swords"].pop(key, None)
                for key, update in (target or {}).items():
                    old = state["swords"].get(key)
                    if old is not None and ts > old.get("observed_at", 0):
                        old.update(update, observed_at=ts)
                state["updated_at"] = max(ts, state.get("updated_at") or state["complete_at"])
        if endpoint == "/party/list" and "sword" in p:
            rows = _valid_rows(p["sword"])
            if rows is None or ts < (state.get("complete_at") or 0):
                continue
            # 旧日志中的完整名单不能冲掉已经观察到的较新局部状态。
            for serial, row in rows.items():
                row["observed_at"] = ts
                old = state["swords"].get(serial)
                if old and old.get("observed_at", 0) > ts:
                    row.update(old)
            # 旧完整名单不能复活已经确认消耗的编号。
            for serial, gone in (state.get("departures") or {}).items():
                if gone.get("observed_at", 0) >= ts:
                    rows.pop(serial, None)
            state.update(complete_at=ts, swords=rows)
        elif state.get("complete_at"):
            rows = None
            if endpoint == "/sally":
                rows = _valid_rows(p.get("sword_all"))
            elif endpoint in ("/battle/battle", "/battle/alloutbattle"):
                result = p.get("result") or {}
                if isinstance(result, dict):
                    player = result.get("player")
                    party = player.get("party") if isinstance(player, dict) else None
                    rows = _valid_rows(party.get("slot")) if isinstance(party, dict) else None
            for serial, update in (rows or {}).items():
                old = state["swords"].get(serial)
                if old is not None and ts > old.get("observed_at", 0):
                    old.update(update, observed_at=ts)
    return state if state.get("complete_at") else previous


def sync_archive(events, store, receipts=None):
    # 和进账共用数据库锁；原始响应及账号凭证绝不落进档案。
    with store._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = read_archive(store)
        state = update_archive(events, previous)
        if state is not None:
            origins = state.setdefault("origins", {})
            for receipt in receipts or []:
                p = receipt["payload"]
                for sword in p.get("swords", [p]):
                    serial = _integer(sword.get("serial_id"))
                    if not serial or serial <= 0:
                        continue
                    source = p.get("source")
                    label = p.get("origin_label") if source == "inbox.claim" else {
                        "forge": "锻刀", "sortie.drop": "出阵掉落", "battle.drop": "战斗掉落",
                        "raid.drop": "联队战掉落"}.get(source)
                    if not label:
                        continue
                    origin = origins.setdefault(str(serial), {})
                    if not origin.get("label") or origin.get("source") == "inbox.claim":
                        origin.update(source=source, label=label)
                        if p.get("chapter") and p.get("map_no"):
                            origin["location"] = f'{p["chapter"]}-{p["map_no"]}'
                    if source == "inbox.claim":
                        for key in ("mailbox_id", "origin_message", "inbox_at", "received_at"):
                            origin[key] = p.get(key)
        if state is None or state == previous:
            return False
        path = archive_path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_bytes(path.read_bytes())
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            json.dump(state, handle, ensure_ascii=False)
            temporary = handle.name
        os.replace(temporary, path)
    return True


def candidate_pool(store):
    from . import sword_db
    state = read_archive(store)
    if state is None:
        return None
    entries = []
    for serial, row in state["swords"].items():
        sid = _integer(row.get("sword_id"))
        found = sword_db.find_game_sword(sid) if sid else None
        catalog, info, form = found if found else (None, {}, "unknown")
        try:
            day = datetime.strptime(row.get("created_at") or "", "%Y-%m-%d %H:%M:%S")
            manifest = f"{day.year}-{day.month}-{day.day}"
        except (ValueError, TypeError):
            manifest = None
        entry = {
            "observation_id": f"youzu:{serial}", "serial_id": int(serial),
            "row_no": None, "page_no": None, "source_snapshot_id": None,
            "sword_catalog_id": catalog, "same_team_exclusion_key": catalog,
            "name_zh": info.get("name_zh") or info.get("name") or f"刀帐{sid}",
            "level": _integer(row.get("level")), "tou_level": _integer(row.get("ranbu_level")),
            "exp": _integer(row.get("exp")), "ranbu_exp": _integer(row.get("ranbu_exp")),
            "hp_up": _integer(row.get("hp_up")), "atk_up": _integer(row.get("atk_up")),
            "def_up": _integer(row.get("def_up")), "mobile_up": _integer(row.get("mobile_up")),
            "back_up": _integer(row.get("back_up")), "scout_up": _integer(row.get("scout_up")),
            "hide_up": _integer(row.get("hide_up")),
            "survival": _integer(row.get("hp")), "survival_max": _integer(row.get("hp_max")),
            "fatigue": _integer(row.get("fatigue")), "fatigue_max": None,
            "locked": bool(_integer(row["protect"])) if _integer(row.get("protect")) in (0, 1) else None,
            "kiwame_date": manifest, "form_status": form, "machine_form_status": None,
            "form_overridden": False,
            "form_evidence": [f"游戏所持刀剑编号 {sid}"] if form != "unknown" else [],
            "stats": {label: _integer(row[key]) for key, label in
                      (("atk", "打击"), ("def", "统率"), ("mobile", "机动"),
                       ("back", "冲力"), ("hide", "隐蔽"), ("scout", "侦察"),
                       ("loyalties", "必杀")) if key in row and _integer(row[key]) is not None},
            "equipment_serials": {key: _integer(row[key]) for key in FIELDS
                                  if "serial_id" in key and key != "serial_id" and key in row},
            "observed_at": row.get("observed_at"), "data_source": "youzu_log",
            "acquisition": (state.get("origins") or {}).get(serial),
        }
        entry["unknown_fields"] = [key for key in ("level", "tou_level", "survival", "survival_max",
                                                    "fatigue", "fatigue_max", "kiwame_date", "locked")
                                   if entry[key] is None]
        if not catalog:
            entry["unknown_fields"].append("identity")
        entries.append(entry)
    return {"done": True, "completeness": "complete", "source": {"kind": "youzu_log", "snapshot_id": None},
            "observed_at": max(state["complete_at"], state.get("updated_at") or 0), "owned": len(entries),
            "entry_count": len(entries), "entries": entries, "skipped_newer_snapshots": []}
