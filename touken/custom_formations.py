# -*- coding: utf-8 -*-
"""预设编队档案：我们这边存的阵容预设（覆盖游戏内某部队 1-5）。

玩法选中预设时，先把 slots 逐槽应用成游戏内队伍再跑玩法
（formation_editor.apply_preset_formation_stream）。存储 = 用户数据目录
STATE_DIR/custom_formations.json；坏文件改名备份后当空的处理，绝不崩。
"""

import json
import re
import time

from .runtime_paths import STATE_DIR

MAX_FORMATIONS = 5
SCHEMA_VERSION = 2

_ID_RE = re.compile(r"^[a-z0-9]+$")
_SLOT_KEYS = frozenset("123456")


def _formations_path():
    return STATE_DIR / "custom_formations.json"


def load_formations() -> list[dict]:
    """读预设编队；坏 JSON 备份成 .bad-时间戳 后返回空，绝不让调用方崩。"""
    path = _formations_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            bad = path.with_name(path.name + ".bad-"
                                 + time.strftime("%Y%m%d-%H%M%S"))
            path.replace(bad)
        except OSError:
            pass
        return []
    if not isinstance(data, dict) or not isinstance(data.get("formations"), list):
        return []
    return [f for f in data["formations"] if isinstance(f, dict)]


def save_formations(formations: list[dict]):
    """原子写：先落 .tmp 再 replace（同 data_relocation._write_json 风格）。"""
    path = _formations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION,
                    "formations": formations}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    temporary.replace(path)


def validate_formation(record: dict, existing: list[dict] | None = None) -> str | None:
    """校验一条预设；返回错误消息或 None。

    槽位值至少含 sword_catalog_id/name_zh 其一（身份铁律同
    formation_editor：sword_catalog_id 是刀种同位键，name_zh 可过名册
    校正）；slots 允许空 dict（=还没指定人，应用时会如实拦下）。
    """
    if not isinstance(record, dict):
        return "预设记录必须是 dict"
    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        return "预设名字不能为空"
    if len(name.strip()) > 20:
        return "预设名字最多 20 字"
    team = record.get("target_team")
    if not isinstance(team, int) or isinstance(team, bool) or not 1 <= team <= 5:
        return "目标部队必须是 1~5 的整数"
    slots = record.get("slots")
    if not isinstance(slots, dict):
        return "slots 必须是 dict（槽位号 → 目标条目）"
    if len(slots) > 6:
        return "slots 最多 6 个键"
    for key, entry in slots.items():
        if not isinstance(key, str) or key not in _SLOT_KEYS:
            return f"槽位键必须是 '1'~'6'（收到 {key!r}）"
        if not isinstance(entry, dict):
            return f"槽位 {key} 的目标必须是 dict"
        sid = entry.get("sword_catalog_id")
        nm = entry.get("name_zh")
        has_id = isinstance(sid, str) and bool(sid.strip())
        has_name = isinstance(nm, str) and bool(nm.strip())
        if not has_id and not has_name:
            return f"槽位 {key} 缺身份：sword_catalog_id/name_zh 至少给一样"
        policy = entry.get("selection_policy")
        if policy not in (None, "locked_highest_level"):
            return f"槽位 {key} 的选人方式不认识"
        if policy == "locked_highest_level":
            if not has_id or entry.get("form_status") not in ("normal", "kiwame"):
                return f"槽位 {key} 需明确刀剑图鉴号和普通/极形态"
            if entry.get("observation_id"):
                return f"槽位 {key} 不能同时指定一振和按等级选人"
            from . import sword_db
            if sid not in sword_db.all_swords():
                return f"槽位 {key} 的刀剑图鉴号不在名册里"
        treasure = entry.get("treasure")
        if treasure is not None:
            if (not isinstance(treasure, dict)
                    or not isinstance(treasure.get("name"), str)
                    or not treasure["name"].strip()
                    or not isinstance(treasure.get("level"), int)
                    or isinstance(treasure["level"], bool)
                    or treasure["level"] < 1
                    or not isinstance(treasure.get("affection"), int)
                    or isinstance(treasure["affection"], bool)
                    or treasure["affection"] < 0):
                return f"槽位 {key} 的宝物需填写名称、等级和爱用度"
        troops = entry.get("troops")
        if troops is not None:
            if (not isinstance(troops, dict)
                    or any(slot not in ("1", "2", "3")
                           or not isinstance(name, str) or not name.strip()
                           for slot, name in troops.items())):
                return f"槽位 {key} 的刀装需按第 1／2／3 格填写游戏中的完整名称"
        for equipment_key, label in (("horse", "马匹"), ("charm", "御守")):
            value = entry.get(equipment_key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                return f"槽位 {key} 的{label}名称不正确"
    seen_horses = {}
    for key, entry in slots.items():
        horse = entry.get("horse")
        if not horse or horse in ("白毛", "鹿毛", "青毛"):
            continue
        horse = horse.strip()
        if horse in seen_horses:
            return f"{seen_horses[horse]}号位和{key}号位不能重复指定同名马"
        seen_horses[horse] = key
    seen_treasures = {}
    for key, entry in slots.items():
        treasure = entry.get("treasure")
        if not treasure:
            continue
        fingerprint = (treasure["name"].strip(), treasure["level"],
                       treasure["affection"])
        if fingerprint in seen_treasures:
            return (f"{seen_treasures[fingerprint]}号位和{key}号位不能重复"
                    "指定同一件宝物；同款多实例尚未辨认")
        seen_treasures[fingerprint] = key
    fid = record.get("id")
    if fid is not None:
        if not isinstance(fid, str) or not _ID_RE.match(fid):
            return "id 只能是小写字母和数字（[a-z0-9]+）"
        for other in existing or []:
            if isinstance(other, dict) and other.get("id") == fid:
                return f"id {fid} 已被别的预设占用"
    return None


def new_formation_id(existing) -> str:
    """分配 pf1..pf5 里第一个空位；满了抛 ValueError。"""
    used = {f.get("id") for f in existing or [] if isinstance(f, dict)}
    for i in range(1, MAX_FORMATIONS + 1):
        fid = f"pf{i}"
        if fid not in used:
            return fid
    raise ValueError("预设编队最多 5 套")


def find_formation(formations, fid) -> dict | None:
    for f in formations or []:
        if isinstance(f, dict) and f.get("id") == fid:
            return f
    return None


_FINGERPRINT_FIELDS = (
    "sword_catalog_id", "name_zh", "form_status", "tou_level",
    "kiwame_date",
)


def _fingerprint_matches(saved: dict, current: dict,
                         cultivation=None) -> bool:
    """旧预设可重连：等级只准增长；其余已保存的可见指纹必须吻合。"""
    for field in _FINGERPRINT_FIELDS:
        expected = saved.get(field)
        if expected in (None, "", "unknown", "ambiguous"):
            continue
        if current.get(field) != expected:
            return False
    expected_stats = saved.get("stats")
    saved_level = saved.get("level")
    if saved_level is not None:
        current_level = current.get("level")
        if (not isinstance(saved_level, int) or isinstance(saved_level, bool)
                or not isinstance(current_level, int)
                or isinstance(current_level, bool)
                or current_level < saved_level):
            return False
        if current_level > saved_level and not (
                any(saved.get(key) is not None for key in
                    ("tou_level", "survival_max", "kiwame_date"))
                or (isinstance(expected_stats, dict) and
                    any(value is not None for value in expected_stats.values()))):
            # 旧版只存「刀名＋等级」时，升级后无法排除是另一振同名刀。
            return False
    current_stats = current.get("stats") or {}
    from .formation_identity import GROWTH_STATS, growth_stats_match
    has_pair = (isinstance(expected_stats, dict)
                and all(expected_stats.get(key) is not None and
                        current_stats.get(key) is not None
                        for key in GROWTH_STATS))
    if has_pair:
        if not growth_stats_match(
                expected_stats, current_stats, saved.get("name_zh") or "",
                cultivation or {}, old_survival_max=saved.get("survival_max"),
                new_survival_max=current.get("survival_max"),
                observation_id=current.get("observation_id")):
            return False
    else:
        # 旧预设没存齐生存/侦察，沿用所有已保存字段的严格比对。
        if (saved.get("survival_max") is not None and
                current.get("survival_max") != saved["survival_max"]):
            return False
        if isinstance(expected_stats, dict):
            for key, value in expected_stats.items():
                if value is not None and current_stats.get(key) != value:
                    return False
    return True


def _current_candidate_pool() -> dict:
    from .honmaru_profile import get_honmaru_profile
    return get_honmaru_profile().get("candidate_pool") or {}


def _legacy_client_matches(saved: dict, current: dict) -> bool:
    """旧 OCR 预设接到客户端：日期和刀种/形态作证，不混比装备加成。"""
    if not str(current.get("observation_id") or "").startswith("youzu:"):
        return False
    for field in ("sword_catalog_id", "form_status", "kiwame_date"):
        if not saved.get(field) or saved[field] != current.get(field):
            return False
    if saved.get("survival_max") is None or saved["survival_max"] != current.get("survival_max"):
        return False
    for field in ("level", "tou_level"):
        old, new = saved.get(field), current.get(field)
        if (type(old) is not int or type(new) is not int or new < old):
            return False
    return True


def resolve_formation_slots(record: dict, candidate_pool: dict | None = None) -> dict:
    """开工前一次性把整套预设链接到最新完整刀账。

    observation_id 只在原快照内有效：同一快照优先直连；刀账更新后按已保存
    的可见指纹重新链接。任何槽位不唯一、档案不可用或同位刀冲突，都在
    点游戏第一下之前整体拒绝。
    """
    err = validate_formation(record)
    if err:
        return {"ok": False, "reason": err}
    slots = record.get("slots") or {}
    if not slots:
        return {"ok": False, "reason": "这套预设一个位置都没指定"}
    exact_slots = {key: saved for key, saved in slots.items()
                   if saved.get("selection_policy") != "locked_highest_level"}
    if exact_slots:
        if candidate_pool is None:
            candidate_pool = _current_candidate_pool()
        if not candidate_pool.get("done"):
            return {"ok": False,
                    "reason": candidate_pool.get("reason")
                    or "指定具体一振的槽位需要可信的完整刀账"}
    else:
        if candidate_pool is None:
            candidate_pool = _current_candidate_pool()

    # 同一图鉴号的普通/极刀不能同队。整队开工前检查，避免套到一半。
    seen_catalog = {}
    for key in sorted(slots, key=int):
        sid = slots[key].get("sword_catalog_id")
        if sid and sid in seen_catalog:
            return {"ok": False, "reason":
                    f"{seen_catalog[sid]}号位和{key}号位不能重复指定同一位刀"}
        if sid:
            seen_catalog[sid] = key

    entries = candidate_pool.get("entries") or []
    from .formation_identity import cultivation_pairs
    from .flows.naihanka import _load_naihanka_state
    cultivation = cultivation_pairs(_load_naihanka_state(),
                                    candidate_pool.get("observed_at"),
                                    (candidate_pool.get("source") or {}).get("snapshot_id")) if exact_slots else {}
    by_oid = {entry.get("observation_id"): entry for entry in entries
              if entry.get("observation_id")}
    resolved = {}
    for key in sorted(slots, key=int):
        saved = slots[key]
        if saved.get("selection_policy") == "locked_highest_level":
            client_entries = [entry for entry in entries
                              if candidate_pool.get("done")
                              and str(entry.get("observation_id") or "").startswith("youzu:")]
            if client_entries:
                eligible = [entry for entry in client_entries
                            if entry.get("sword_catalog_id") == saved.get("sword_catalog_id")
                            and entry.get("form_status") == saved.get("form_status")
                            and entry.get("locked") is True
                            and type(entry.get("level")) is int]
                if not eligible:
                    return {"ok": False, "reason": f"{key}号位在刀账里没有符合条件的带锁刀剑"}
                # 同等级按稳定实例编号选一振；策略仍留在预设里，下次重新按最新等级选。
                chosen = max(eligible, key=lambda entry: (entry["level"],
                             -int(entry["observation_id"].split(":")[1])))
                resolved[key] = {**chosen, **{field: saved[field] for field in
                                 ("treasure", "troops", "horse", "charm") if saved.get(field)}}
                continue
            resolved[key] = saved
            continue
        direct = by_oid.get(saved.get("observation_id"))
        stable_id = str(saved.get("observation_id") or "").startswith("youzu:")
        if stable_id:
            # Client instance IDs remain stable across levels and roster refreshes.
            # A missing instance must never fall back to another sword's fingerprint.
            matches = [direct] if direct is not None else []
        elif direct is not None and _fingerprint_matches(saved, direct,
                                                      cultivation):
            matches = [direct]
        else:
            matches = [entry for entry in entries if
                       (_legacy_client_matches(saved, entry)
                        if str(entry.get("observation_id") or "").startswith("youzu:")
                        and saved.get("kiwame_date")
                        else _fingerprint_matches(saved, entry, cultivation))]
        label = saved.get("name_zh") or saved.get("sword_catalog_id") or "未识别刀剑"
        if not matches:
            return {"ok": False,
                    "reason": f"{key}号位「{label}」已对不上最新刀账，重新选一次"}
        if len(matches) > 1:
            return {"ok": False,
                    "reason": f"{key}号位「{label}」在最新刀账里仍有 {len(matches)} 振分不清"}
        resolved[key] = ({**matches[0],
                          **({"treasure": saved["treasure"]} if saved.get("treasure") else {}),
                          **({"troops": saved["troops"]} if saved.get("troops") else {}),
                          **({"horse": saved["horse"]} if saved.get("horse") else {}),
                          **({"charm": saved["charm"]} if saved.get("charm") else {})}
                         if any(saved.get(key) for key in ("treasure", "troops", "horse", "charm"))
                         else matches[0])

    resolved_catalog = {}
    for key in sorted(resolved, key=int):
        sid = resolved[key].get("sword_catalog_id")
        if any(resolved[key].get(field) for field in
               ("treasure", "troops", "horse", "charm")) and not sid:
            return {"ok": False, "reason":
                    f"{key}号位带装备时，需先在名册里确认刀剑身份"}
        if sid and sid in resolved_catalog:
            return {"ok": False, "reason":
                    f"{resolved_catalog[sid]}号位和{key}号位不能重复指定同一位刀"}
        if sid:
            resolved_catalog[sid] = key

    from .honmaru_profile import formation_conflicts
    conflicts = formation_conflicts([resolved[key] for key in exact_slots])
    if conflicts:
        slots_text = []
        for conflict in conflicts:
            ids = set(conflict.get("observation_ids") or [])
            numbers = [key for key, entry in resolved.items()
                       if entry.get("observation_id") in ids]
            slots_text.append("、".join(f"{key}号位" for key in numbers))
        return {"ok": False,
                "reason": "同一位刀不能重复编入一队：" + "；".join(slots_text)}
    return {"ok": True, "slots": resolved,
            "candidate_observed_at": candidate_pool.get("observed_at")}


def apply_formation_preset_stream(agent, record: dict,
                                  candidate_pool: dict | None = None):
    """所有玩法/远征/任务流共用的套预设入口。"""
    prepared = resolve_formation_slots(record, candidate_pool=candidate_pool)
    if not prepared.get("ok"):
        yield f"[部队预设] ✗ 开工前检查没通过：{prepared.get('reason')}，没有动游戏"
        return False
    persist_client_slots(record, prepared["slots"])
    return (yield from agent.apply_preset_formation_stream(
        int(record["target_team"]), prepared["slots"],
        str(record.get("name") or "部队预设")))


def persist_client_slots(record: dict, resolved: dict) -> bool:
    """有唯一证据才升级旧编号；先备份，且不覆盖用户刚编辑的预设。"""
    import shutil
    upgrades = {key: entry for key, entry in resolved.items()
                if str(entry.get("observation_id") or "").startswith("youzu:")
                and (record.get("slots") or {}).get(key, {}).get("selection_policy") != "locked_highest_level"
                and not str((record.get("slots") or {}).get(key, {}).get("observation_id") or "").startswith("youzu:")}
    if not upgrades:
        return False
    formations = load_formations()
    current = find_formation(formations, record.get("id"))
    if current != record:
        return False
    path = _formations_path()
    backup = path.with_name(path.name + ".before-client-ids.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    current["slots"] = {**current["slots"], **upgrades}
    save_formations(formations)
    return True


def apply_formation_preset_by_id_stream(agent, preset_id: str,
                                        expected_team: int | None = None):
    """按保存 id 套预设；远征和任务流共用，删除/错队都在点击前拒绝。"""
    record = find_formation(load_formations(), str(preset_id or ""))
    if record is None:
        yield "[部队预设] ✗ 找不到这套预设（可能已删除），没有动游戏"
        return False
    if expected_team is not None and record.get("target_team") != expected_team:
        yield (f"[部队预设] ✗ 「{record.get('name')}」覆盖的是部队"
               f"{record.get('target_team')}，不能拿来改部队{expected_team}，没有动游戏")
        return False
    return (yield from apply_formation_preset_stream(agent, record))
