"""今天单班远征的跳过/强制选择；与循环排班配置分开保存。

skipped：今天这班别跑（排班开着也跳过）。
forced：今天这班一定要跑（排班总开关关着也单独走状态机）。
两者互斥，后写的赢；forced 不 lifted 自定义排班里被关掉的条目。

version 2 起 forced 记录可以是「自描述班」：带 start_min/duration_min，
不引用任何 preset/custom 排班条目（建议引擎采纳的班就是这种）。
迁移：version 1 老文件照常读（记录缺自描述键 = 排班引用班），
首次写回自动升级成 version 2 并留 .bak 备份。
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

from touken.runtime_paths import STATE_DIR

CHOICES_PATH = STATE_DIR / "expedition_day_choices.json"
CHOICES_VERSION = 2
ADHOC_KEY_MARK = ":adhoc:"
_WRITE_LOCK = threading.Lock()


def load_choice_sets(path: Path = CHOICES_PATH) -> tuple[dict, dict]:
    """返回 (skipped, forced) 两个字典；文件坏了都按空处理。

    version 1 老文件按空自描述信息自动升级（读取即兼容，写回时落 v2）。
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(value, dict) \
            or value.get("version") not in (1, CHOICES_VERSION):
        return {}, {}
    skipped = value.get("skipped")
    forced = value.get("forced")
    return (skipped if isinstance(skipped, dict) else {},
            forced if isinstance(forced, dict) else {})


def load_choices(path: Path = CHOICES_PATH) -> dict:
    """兼容旧调用：只取 skipped 字典。"""
    return load_choice_sets(path)[0]


def _record_matches(record, *, team_no: int, map_code: str) -> bool:
    return (isinstance(record, dict)
            and record.get("team_no") == team_no
            and record.get("map_code") == map_code)


def is_skipped(choices: dict, *, key: str, team_no: int,
               map_code: str, planned_at: float) -> bool:
    """班次键外再核对队伍和地图；前班迟到顺延时仍跳过同一班。"""
    return _record_matches(choices.get(key), team_no=team_no, map_code=map_code)


def is_forced(forced: dict, *, key: str, team_no: int, map_code: str) -> bool:
    """今日单班强制启用；核对口径同 is_skipped。"""
    return _record_matches(forced.get(key), team_no=team_no, map_code=map_code)


def is_adhoc_record(record) -> bool:
    """自描述班：forced 记录自带 start_min/duration_min，不引用排班条目。"""
    return (isinstance(record, dict)
            and isinstance(record.get("start_min"), (int, float))
            and not isinstance(record.get("start_min"), bool)
            and isinstance(record.get("duration_min"), (int, float))
            and not isinstance(record.get("duration_min"), bool))


def adhoc_key(date: str, team_no: int, start_min: int) -> str:
    """自描述班的键：日期编进键里，天然只管今天，换日自动失效。"""
    return f"{date}{ADHOC_KEY_MARK}{int(team_no)}:{int(start_min)}"


def adhoc_planned_date(record) -> str | None:
    """自描述班的计划日期（planned_at 支持 ISO 字符串或时间戳）。"""
    if not isinstance(record, dict):
        return None
    raw = record.get("planned_at")
    try:
        if isinstance(raw, str):
            return datetime.fromisoformat(raw).date().isoformat()
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return datetime.fromtimestamp(float(raw)).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _write_sets(skipped: dict, forced: dict, path: Path) -> None:
    payload = {"version": CHOICES_VERSION, "skipped": skipped, "forced": forced}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    temporary.replace(path)


def set_skipped(*, key: str, team_no: int, map_code: str,
                planned_at: float, skipped: bool,
                path: Path = CHOICES_PATH) -> dict:
    """原子写入并保留上一版，失败时原文件仍在。记跳过的同时清掉强制。"""
    with _WRITE_LOCK:
        skipped_map, forced_map = load_choice_sets(path)
        if skipped:
            skipped_map[key] = {"team_no": team_no, "map_code": map_code,
                                "planned_at": planned_at}
            forced_map.pop(key, None)
        else:
            skipped_map.pop(key, None)
        _write_sets(skipped_map, forced_map, path)
        return skipped_map


def set_forced(*, key: str, team_no: int, map_code: str,
               planned_at: float, forced: bool,
               path: Path = CHOICES_PATH) -> dict:
    """记强制的同时清掉跳过；排班开关关着就靠它单独跑这一班。"""
    with _WRITE_LOCK:
        skipped_map, forced_map = load_choice_sets(path)
        if forced:
            forced_map[key] = {"team_no": team_no, "map_code": map_code,
                               "planned_at": planned_at}
            skipped_map.pop(key, None)
        else:
            forced_map.pop(key, None)
        _write_sets(skipped_map, forced_map, path)
        return forced_map


def set_slot_intention(*, key: str, team_no: int, map_code: str,
                       planned_at: float, will_run: bool, base_enabled: bool,
                       path: Path = CHOICES_PATH) -> dict:
    """按目标状态一键定下今天这班：will_run=True 时排班开着只清选择、
    关着记 forced；will_run=False 记 skipped。skipped/forced 互斥。"""
    with _WRITE_LOCK:
        skipped_map, forced_map = load_choice_sets(path)
        if will_run:
            skipped_map.pop(key, None)
            if base_enabled:
                forced_map.pop(key, None)
            else:
                forced_map[key] = {"team_no": team_no, "map_code": map_code,
                                   "planned_at": planned_at}
        else:
            forced_map.pop(key, None)
            skipped_map[key] = {"team_no": team_no, "map_code": map_code,
                                "planned_at": planned_at}
        _write_sets(skipped_map, forced_map, path)
        return {"skipped": skipped_map, "forced": forced_map}


def set_forced_adhoc(*, key: str, team_no: int, map_code: str,
                     start_min: int, duration_min: int, planned_at: str,
                     forced: bool = True, formation_id: str = "",
                     formation_name: str = "", formation_signature: str = "",
                     sakura_before_dispatch: bool = False, repair_threshold: str = "light",
                     replace_key: str = "", path: Path = CHOICES_PATH) -> dict:
    """记下/取消一班自描述 forced 远征（建议引擎采纳的班）。

    记录自带 {team_no, map_code, start_min, duration_min, planned_at}，
    不引用任何 preset/custom 条目；scheduled 派遣链只读这些字段。
    写的同时顺手清掉昨天及更早残留的自描述记录（键里带日期，已失效）。
    """
    today = datetime.now().date().isoformat()
    with _WRITE_LOCK:
        skipped_map, forced_map = load_choice_sets(path)
        stale = [k for k, rec in forced_map.items()
                 if ADHOC_KEY_MARK in str(k)
                 and (adhoc_planned_date(rec) or "") < today]
        for old_key in stale:
            forced_map.pop(old_key, None)
        if forced:
            if replace_key:
                forced_map.pop(replace_key, None)
            forced_map[key] = {"team_no": team_no, "map_code": map_code,
                               "planned_at": planned_at,
                               "start_min": int(start_min),
                               "duration_min": int(duration_min)}
            forced_map[key].update(sakura_before_dispatch=bool(sakura_before_dispatch),
                                   repair_threshold=repair_threshold)
            if formation_id:
                forced_map[key].update(formation_id=formation_id, formation_name=formation_name,
                                       formation_signature=formation_signature)
            skipped_map.pop(key, None)
        else:
            forced_map.pop(key, None)
        _write_sets(skipped_map, forced_map, path)
        return forced_map
