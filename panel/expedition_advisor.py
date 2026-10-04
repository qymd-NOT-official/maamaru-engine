"""远征建议引擎 v2：家底缺什么 → 今天丢哪几队、去哪些图。

玩家决定每队派几次、哪些队可以丢，以及是否先套用指定的部队预设。
v2 起建议不再依赖 preset 排班投影——引擎按缺口排序直接算班
（图/队伍/时刻自描述），采纳 = 写一条自描述 forced 记录
（expedition_day_choices.json v2），排班总开关关着也单独走状态机，
点了才跑，绝不自动执行。

缺口排序：resource_watch.limiting（锻刀短板，先卡炉的在前）→ 小判
（有目标缺口时）→ 剩下的按锻刀余量从少到多补齐（最终兜底小判）。
拿不到 resource_watch（没盘点过）时给空建议 + 原因，不瞎猜。
"""

from __future__ import annotations

import json
import hashlib
import shutil
import threading
from pathlib import Path

from touken.runtime_paths import STATE_DIR

PREFS_PATH = STATE_DIR / "expedition_help_prefs.json"
PREFS_VERSION = 2
DEFAULT_ROUNDS_PER_TEAM = 1
DEFAULT_AVAILABLE_TEAMS = [1, 4, 5]
MAX_ROUNDS_PER_TEAM = 5
VALID_TEAMS = (1, 2, 3, 4, 5)

TEAM_NAMES = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}
FORGE_RESOURCES = ("木炭", "玉钢", "冷却材", "砥石")
KOBAN = "小判"
FOCUS_RESOURCES = (*FORGE_RESOURCES, KOBAN, "委托符", "加速符")

_MAPS_PATH = (Path(__file__).resolve().parent.parent
              / "touken" / "data" / "expedition_maps.json")
SITUATION_FILENAME = "youzu_home_situation.json"

_WRITE_LOCK = threading.Lock()


# ── 长期偏好：每队各派几次 + 哪些队可以丢 ──


def _normalize_prefs(value) -> dict:
    """宽容归一：缺键用默认、坏值回退，永远吐得出能用的偏好。

    v1 → v2 迁移：旧字段 teams_out（总共丢几队）读作 rounds_per_team
    （每队各派几次）——老大拍板的新语义，队伍是载体、次数按队算。
    """
    if not isinstance(value, dict):
        value = {}
    raw_rounds = value.get("rounds_per_team",
                           value.get("teams_out", DEFAULT_ROUNDS_PER_TEAM))
    try:
        rounds = int(raw_rounds)
    except (TypeError, ValueError):
        rounds = DEFAULT_ROUNDS_PER_TEAM
    rounds = max(0, min(MAX_ROUNDS_PER_TEAM, rounds))
    raw_teams = value.get("available_teams", DEFAULT_AVAILABLE_TEAMS)
    teams: list[int] = []
    if isinstance(raw_teams, list):
        for item in raw_teams:
            try:
                team = int(item)
            except (TypeError, ValueError):
                continue
            if team in VALID_TEAMS and team not in teams:
                teams.append(team)
    if not teams:
        teams = list(DEFAULT_AVAILABLE_TEAMS)
    raw_formations = value.get("team_formations", {})
    formations = {str(team): fid for team, fid in raw_formations.items()
                  if str(team) in {str(t) for t in VALID_TEAMS} and isinstance(fid, str)} if isinstance(raw_formations, dict) else {}
    return {"version": PREFS_VERSION, "rounds_per_team": rounds,
            "available_teams": sorted(teams), "team_formations": formations,
            "resource_focus": value.get("resource_focus") if value.get("resource_focus") in FOCUS_RESOURCES else ""}


def load_prefs(path: Path = PREFS_PATH) -> dict:
    """坏文件当没有；不存在的键用默认。只读不写（v1 老文件读出即迁移，
    下次保存落 v2）。"""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _normalize_prefs(None)
    if not isinstance(value, dict):
        return _normalize_prefs(None)
    version = value.get("version")
    if version not in (1, PREFS_VERSION):  # v1 走迁移，再老的当没有
        return _normalize_prefs(None)
    return _normalize_prefs(value)


def _as_count(value, label: str) -> int:
    """JSON 数字 → 整数；布尔/字符串/小数一律拒。"""
    if isinstance(value, bool):
        raise ValueError(f"{label}得是个数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"{label}得是个数")


def save_prefs(*, rounds_per_team=None, available_teams=None, resource_focus=None, team_formations=None,
               path: Path = PREFS_PATH) -> dict:
    """校验落盘（原子替换 + 备份上一版）；参数不合法抛 ValueError。"""
    previous = load_prefs(path)
    if rounds_per_team is None:
        rounds_per_team = previous["rounds_per_team"]
    if available_teams is None:
        available_teams = previous["available_teams"]
    if resource_focus is not None and resource_focus not in (*FOCUS_RESOURCES, ""):
        raise ValueError("关注项不支持远征补给")
    if team_formations is not None:
        if not isinstance(team_formations, dict):
            raise ValueError("编队选择得按部队保存")
        from touken.custom_formations import load_formations
        records = {record["id"]: record for record in load_formations()}
        for team, fid in team_formations.items():
            if str(team) not in {str(t) for t in VALID_TEAMS} or not isinstance(fid, str):
                raise ValueError("请选择部队一到五和已保存的预设")
            if fid and (fid not in records or records[fid].get("target_team") != int(team)):
                raise ValueError(f"部队{TEAM_NAMES[int(team)]}的预设已删除或覆盖的是另一支部队，请重新选择")
    rounds = _as_count(rounds_per_team, "每队派几次")
    if not 0 <= rounds <= MAX_ROUNDS_PER_TEAM:
        raise ValueError(f"每队派几次要在 0 到 {MAX_ROUNDS_PER_TEAM} 之间")
    if not isinstance(available_teams, list):
        raise ValueError("可丢的队伍得是一串队号")
    teams: list[int] = []
    for item in available_teams:
        team = _as_count(item, "队号")
        if team not in VALID_TEAMS:
            raise ValueError(f"队伍只有一到五，{team} 不存在")
        if team not in teams:
            teams.append(team)
    prefs = _normalize_prefs({"version": PREFS_VERSION,
                              "rounds_per_team": rounds,
                              "available_teams": sorted(teams)})
    path = Path(path)
    with _WRITE_LOCK:
        prefs["team_formations"] = {**load_prefs(path)["team_formations"],
                                    **(team_formations or {})}
        prefs["resource_focus"] = (load_prefs(path)["resource_focus"]
                                   if resource_focus is None else resource_focus)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(prefs, ensure_ascii=False, indent=2)
                             + "\n", encoding="utf-8")
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        temporary.replace(path)
    return prefs


# ── 数据装载 ──


def load_maps(path: Path = _MAPS_PATH) -> dict:
    """远征图数据：code → meta（含各资源收益/时长/等级条件）。坏文件当空。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    maps = data.get("maps")
    return maps if isinstance(maps, dict) else {}


def party_levels_from_situation(path: Path) -> dict | None:
    """本丸近况 → {队号: {sum, max, count, names}}；读不到（没同步过）返回 None。

    有了它才能核远征图的 total_level/level_req 和刀种要求；没有就只做
    收益匹配，建议里注明等级没核。names 是成员名册名（简体中文，极化带
    「·极」后缀），刀种资格门用。
    """
    try:
        situation = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    parties = situation.get("parties")
    if not isinstance(parties, list):
        return None
    levels: dict[int, dict] = {}
    for party in parties:
        if not isinstance(party, dict):
            continue
        try:
            team = int(party.get("party_no"))
        except (TypeError, ValueError):
            continue
        rows = [m for m in party.get("members") or [] if isinstance(m, dict)]
        members = [int(m["level"]) for m in rows
                   if isinstance(m.get("level"), (int, float))]
        if not members:
            continue
        levels[team] = {"sum": sum(members), "max": max(members),
                        "count": len(members),
                        "names": [str(m.get("name") or "") for m in rows]}
    return levels or None


# 名册（swords.json）刀种是日文旧字体（脇差/槍/剣），远征规则和面板用简体；
# 映射与 touken/flows/team_roster._TYPE_NORMALIZE 保持一致
_TYPE_NORMALIZE = {"脇差": "胁差", "槍": "枪", "剣": "剑"}
_SWORD_TYPE_TABLE: dict | None = None


def _sword_type_table() -> dict:
    """刀名（日/中）→ 规范刀种（简体）。精确匹配不模糊——资格门认错比
    认不到更糟（错拦只是少条建议，错放就是 failed_unknown）。"""
    global _SWORD_TYPE_TABLE
    if _SWORD_TYPE_TABLE is None:
        from touken import sword_db
        table = {}
        for info in sword_db.all_swords().values():
            raw = info.get("type") or ""
            sword_type = _TYPE_NORMALIZE.get(raw, raw)
            if not sword_type:
                continue
            for name in (info.get("name"), info.get("name_zh")):
                if name:
                    table.setdefault(name, sword_type)
        _SWORD_TYPE_TABLE = table
    return _SWORD_TYPE_TABLE


def party_sword_types(party: dict) -> list:
    """近况队伍 → 每人规范刀种（认不出为 None）。极化刀种不变，去「·极」后缀。"""
    table = _sword_type_table()
    types = []
    for name in party.get("names") or []:
        base = str(name).removesuffix("·极").strip()
        types.append(table.get(base))
    return types


def _type_shortfall(meta, party) -> dict | None:
    """刀种门槛（「含有」语义：至少一把该刀种在队）。

    返回 None = 合格/无从核（图没要求、没近况、名册全认不出都不拦，
    和等级门一个口径）；否则 {"missing": [缺的刀种], "distinct": (现有种数,
    要求种数) | None}。
    """
    if not party:
        return None
    rules = meta.get("rules") if isinstance(meta, dict) else None
    if not isinstance(rules, dict):
        return None
    required = rules.get("required_types")
    distinct_need = rules.get("min_distinct_types")
    has_distinct_rule = isinstance(distinct_need, int) \
        and not isinstance(distinct_need, bool) and distinct_need > 0
    if required is None and not has_distinct_rule:
        return None
    known = [t for t in party_sword_types(party) if t]
    if not known:
        return None
    missing = []
    if isinstance(required, dict):
        for type_name, need in sorted(required.items()):
            if not isinstance(need, int) or isinstance(need, bool) \
                    or need <= 0:
                continue
            if known.count(type_name) < need:
                missing.append(type_name)
    distinct = None
    if has_distinct_rule:
        have = len(set(known))
        if have < distinct_need:
            distinct = (have, distinct_need)
    if missing or distinct:
        return {"missing": missing, "distinct": distinct}
    return None


def _type_block_detail(team_no: int, party: dict, shortfall: dict) -> str:
    """人话描述哪队卡在哪：「部队四全是太刀，没有打刀」。"""
    team = f"部队{TEAM_NAMES.get(team_no, team_no)}"
    known = sorted({t for t in party_sword_types(party) if t})
    if len(known) == 1:
        comp = f"全是{known[0]}"
    elif known:
        comp = f"只有{'、'.join(known)}"
    else:
        comp = "刀种没核到"
    parts = []
    if shortfall["missing"]:
        parts.append("没有" + "和".join(shortfall["missing"]))
    if shortfall["distinct"]:
        have, need = shortfall["distinct"]
        parts.append(f"只凑出{have}种刀，要{need}种")
    return f"{team}{comp}，{'，'.join(parts)}"


def _type_req_text(meta) -> str:
    """图的刀种要求人话：「队里有打刀和太刀」「凑4种刀」。"""
    rules = meta.get("rules") if isinstance(meta, dict) else None
    if not isinstance(rules, dict):
        return ""
    parts = []
    required = rules.get("required_types")
    if isinstance(required, dict) and required:
        parts.append("队里有" + "和".join(sorted(required)))
    distinct_need = rules.get("min_distinct_types")
    if isinstance(distinct_need, int) and not isinstance(distinct_need, bool) \
            and distinct_need > 0:
        parts.append(f"凑{distinct_need}种刀")
    return "，".join(parts)


def _per_hour(meta, resource: str) -> float:
    if not isinstance(meta, dict):
        return 0.0
    amount = meta.get(resource)
    duration = meta.get("duration_min")
    if (not isinstance(amount, (int, float)) or amount <= 0
            or not isinstance(duration, (int, float)) or duration <= 0):
        return 0.0
    return round(amount / duration * 60, 2)


def _resource_rank(maps: dict, resource: str, map_code: str) -> int | None:
    """某图在某资源的时薪榜排第几（1 起）；不入榜（不产/没这图）返回 None。"""
    entries = []
    for code, meta in maps.items():
        rate = _per_hour(meta, resource)
        if rate > 0:
            entries.append((code, rate))
    if not entries:
        return None
    entries.sort(key=lambda item: -item[1])
    for index, (code, _) in enumerate(entries, start=1):
        if code == map_code:
            return index
    return None


def _forge_capacity(planning: dict, resource: str):
    watch = (planning or {}).get("resource_watch") or {}
    for row in watch.get("resources") or []:
        if isinstance(row, dict) and row.get("resource") == resource:
            return row.get("forge_capacity")
    return None


def _koban_short(planning: dict) -> bool:
    watch = (planning or {}).get("koban_watch") or {}
    try:
        if watch.get("available") is not None \
                and float(watch["available"]) <= 0:
            return True
    except (TypeError, ValueError):
        pass
    for goal in (planning or {}).get("goals") or []:
        if (isinstance(goal, dict) and goal.get("resource") == KOBAN
                and goal.get("status") != "done"):
            return True
    for event in (planning or {}).get("events") or []:
        if isinstance(event, dict) and event.get("shortfall"):
            return True
    return False


def shortage_order(planning: dict, n: int) -> list[tuple[str, str]]:
    """缺口榜：[(资源, 来头)]，来头 ∈ limiting / koban / fill，长度 ≥ n。

    limiting 全列（四资源齐平）视为「都不算缺」，后面用 fill 口吻兜底。
    """
    watch = (planning or {}).get("resource_watch") or {}
    limiting = [name for name in (watch.get("limiting") or [])
                if isinstance(name, str)]
    order: list[tuple[str, str]] = []
    seen: set[str] = set()
    if limiting and len(limiting) < len(FORGE_RESOURCES):
        for name in limiting:
            if name not in seen:
                order.append((name, "limiting"))
                seen.add(name)
    if _koban_short(planning) and KOBAN not in seen:
        order.append((KOBAN, "koban"))
        seen.add(KOBAN)
    capacities = []
    for row in watch.get("resources") or []:
        if (isinstance(row, dict)
                and row.get("resource") in FORGE_RESOURCES
                and isinstance(row.get("forge_capacity"), (int, float))
                and row["resource"] not in seen):
            capacities.append((row["forge_capacity"], row["resource"]))
    capacities.sort()
    for _, name in capacities:
        order.append((name, "fill"))
        seen.add(name)
    if KOBAN not in seen:
        order.append((KOBAN, "fill"))
        seen.add(KOBAN)
    if not order:
        order.append((KOBAN, "fill"))
    while len(order) < n:
        order.append((KOBAN, "fill"))
    return order[:n] if n > 0 else []


def _planning_has_data(planning) -> bool:
    if not isinstance(planning, dict):
        return False
    watch = planning.get("resource_watch")
    if not isinstance(watch, dict):
        return False
    return (watch.get("forge_capacity") is not None
            or bool(watch.get("limiting")))


def _level_shortfall(meta, party) -> str | None:
    """明确的合计等级规则优先；level_req 是旧表兼容字段，不能再当单振要求。"""
    if not party:
        return None
    rules = meta.get("rules") if isinstance(meta, dict) else None
    total = rules.get("total_level") if isinstance(rules, dict) else None
    if isinstance(total, (int, float)) and total > 0:
        return f"等级合计 {party['sum']}，要求 {int(total)}（差 {int(total - party['sum'])}）" if party["sum"] < total else None
    req = meta.get("level_req") if isinstance(meta, dict) else None
    if isinstance(req, (int, float)) and req > 0 and party["max"] < req:
        return f"最高等级 {party['max']}，要求 {int(req)}（差 {int(req - party['max'])}）"
    return None


def _level_ok(meta, party) -> bool:
    """队伍等级门槛：有近况就核（total_level 看队伍等级和、level_req 看
    最高等级）；没近况不拦，由 reason 注明。"""
    return _level_shortfall(meta, party) is None


# ── 建议生成 v2：引擎现算班，不依赖排班投影 ──

SUGGEST_LEAD_MIN = 5          # 建议从 now+5 分钟起排
DAY_END_MIN = 28 * 60 - 1    # 次日 03:59 前出发；归来允许跨日
COLLECT_BUFFER_MIN = 10       # 同队连班之间的收菜缓冲


def _reason(resource, tag, team_no, map_code, map_name, rank,
            capacity, party, type_note="") -> str:
    """v2 口吻：缺什么 → 派哪队去哪张图，为什么这队够格。"""
    team = f"部队{TEAM_NAMES.get(team_no, team_no)}"
    if tag == "limiting":
        head = f"{resource}最缺"
        if isinstance(capacity, (int, float)):
            head += f"（就剩{int(capacity)}炉）"
    elif tag == "manual":
        head = f"你选了优先攒{resource}"
    elif tag == "koban":
        head = f"{resource}有目标缺口"
    else:
        head = f"家底不算缺，顺手攒{resource}"
    where = f"{map_code}「{map_name}」" if map_name and map_name != map_code \
        else map_code
    if rank == 1:
        tail = f"→ 派{team}去{where}，{resource}时薪正是第一"
    elif rank:
        tail = f"→ 派{team}去{where}，{resource}时薪第{rank}"
    else:
        tail = f"→ 派{team}去{where}"
    reason = f"{head}{tail}"
    suffix = "等级合计够格" if party else "队伍等级没核到"
    if type_note:
        suffix += f"，{type_note}"
    reason += f"（{suffix}）"
    return reason


def formation_signature(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _known_lock(entry: dict) -> bool | None:
    # 本丸候选池统一使用 locked；兼容旧的识别状态，但未知值绝不当上锁。
    if type(entry.get("locked")) is bool:
        return entry["locked"]
    return {"locked": True, "unlocked": False}.get(entry.get("lock_status"))


def expedition_formation_options(team: int, parties: dict | None, rejections: dict | None = None) -> list[dict]:
    """只用已保存且能核清身份、等级、刀种的预设，不借其他队的刀。"""
    from touken.custom_formations import load_formations, resolve_formation_slots, _current_candidate_pool
    if rejections is None:
        rejections = {}
    if not parties or team not in parties:
        rejections["*"] = "还没读到这支队伍的现有成员，先同步本丸近况"
        return []
    other_names = {name.removesuffix("·极").strip()
                   for number, party in parties.items() if number != team
                   for name in party.get("names", []) if name}
    formations = load_formations()
    # 已采纳的其他队换队班也保留人选，避免两班同时争用同一振。
    from .expedition_choices import load_choice_sets, adhoc_planned_date
    from datetime import date
    from touken import sword_db
    _, forced = load_choice_sets()
    by_id = {record.get("id"): record for record in formations}
    for booking in forced.values():
        if not isinstance(booking, dict) or booking.get("team_no") == team or (adhoc_planned_date(booking) or "") < date.today().isoformat():
            continue
        reserved = by_id.get(booking.get("formation_id"), {})
        for entry in reserved.get("slots", {}).values():
            info = sword_db.all_swords().get(entry.get("sword_catalog_id"), {})
            name = entry.get("name_zh") or info.get("name_zh") or info.get("name") or ""
            if name:
                other_names.add(name.removesuffix("·极").strip())
    options = []
    for record in formations:
        if record.get("target_team") != team:
            continue
        applied_record = record
        if any(entry.get("selection_policy") == "locked_highest_level" for entry in record.get("slots", {}).values()):
            pool = _current_candidate_pool()
            if not pool.get("done"):
                rejections[record["id"]] = "需要完整刀帐才能选出上锁且等级最高的刀，请先更新刀帐"
                continue
            pinned = {}
            for key, saved in record["slots"].items():
                if saved.get("selection_policy") != "locked_highest_level":
                    pinned[key] = saved
                    continue
                matches = [entry for entry in pool.get("entries", [])
                           if entry.get("sword_catalog_id") == saved.get("sword_catalog_id")
                           and entry.get("form_status") == saved.get("form_status")]
                if not matches or any(type(entry.get("level")) is not int
                                      or _known_lock(entry) is None for entry in matches):
                    break
                locked = [entry for entry in matches if _known_lock(entry) is True]
                if not locked:
                    break
                highest = max(entry["level"] for entry in locked)
                top = [entry for entry in locked if entry["level"] == highest]
                if len(top) != 1:
                    break
                pinned[key] = {**top[0], **{field: saved[field] for field in ("troops", "horse", "charm", "treasure") if saved.get(field)}}
            if len(pinned) != len(record["slots"]):
                rejections[record["id"]] = "未能唯一确定上锁且等级最高的刀，请核对刀帐"
                continue
            applied_record = {**record, "slots": pinned}
        prepared = resolve_formation_slots(applied_record)
        if not prepared.get("ok"):
            rejections[record["id"]] = prepared.get("reason") or "预设人选尚未核清"
            continue
        entries = list(prepared["slots"].values())
        if any(entry.get("selection_policy") == "locked_highest_level"
               or type(entry.get("level")) is not int or entry["level"] <= 0
               for entry in entries):
            rejections[record["id"]] = "预设人选的等级尚未核清，请更新刀帐"
            continue
        party = {"sum": sum(entry["level"] for entry in entries),
                 "max": max(entry["level"] for entry in entries), "count": len(entries),
                 "names": [str(entry.get("name_zh") or entry.get("name") or "") for entry in entries]}
        if not all(party_sword_types(party)):
            rejections[record["id"]] = "预设人选的刀种尚未核清，请更新刀帐"
            continue
        if any(name.removesuffix("·极").strip() in other_names for name in party["names"]):
            rejections[record["id"]] = "预设人选已在其他部队或已被另一班远征预留，不能借来换队"
            continue
        options.append({"formation_id": record["id"], "formation_name": record["name"],
                        "formation_signature": formation_signature(record), "party": party, "record": applied_record})
    return options


def build_expedition_suggestions(prefs: dict, *,
                                 planning=None, maps: dict | None = None,
                                 situation_path: Path | None = None,
                                 now_min: float = 0.0,
                                 committed_counts=None,
                                 team_busy_until=None,
                                 occupied_maps=(),
                                 occupied_windows=(),
                                 task_windows=(),
                                 failed_combos=()) -> dict:
    """玩家驱动的建议：每个可丢队伍今天各派 N 班（rounds_per_team）。

    - 班次轮转：各队第 1 班依次取缺口榜第 1、2… 种资源的对口图，
      第 2 班接着往后排；榜轮完从头再轮（越缺的资源出现越勤）。
    - 图：该资源时薪从高到低试，次日 03:59 前能出发即可，归来可跨日；
      occupied_maps（待收菜占图）不入选，其余班按时间段核对撞图
      （游戏机制一张图同时只能一队在跑）。
    - 队：滤图的等级条件（total_level/level_req）和刀种条件
      （required_types「含有」语义 / min_distinct_types，有近况才核）。
    - 同队多班串行：下一班从上一班 start+duration+10 分钟收菜缓冲后
      起排（一队同时只能跑一班）；不同队伍同刻出发是游戏常态。
    - committed_counts：{队号: 今天已排班数}（forced/在跑/待收都算），
      引擎只补足到 N 班；team_busy_until {队号: 已排班到几点（分钟)}，
      补的班从该时刻+10 分钟收菜缓冲后起排。
    - 黑名单：failed_combos 里「今天同图同队没派成」的组合不再荐
      （failed 会释放图，但同组合拉黑到今天结束），note 如实说明。
    返回 {"suggestions": [...], "note": 玩家可看的原因/None}。
    """
    prefs = _normalize_prefs(prefs)
    rounds = prefs["rounds_per_team"]
    empty = {"suggestions": [], "note": None}
    if rounds <= 0:
        empty["note"] = "今天不丢队出门；想丢就在上面把每队次数挑起来。"
        return empty
    teams = sorted(prefs["available_teams"])
    if not _planning_has_data(planning):
        empty["note"] = ("还不知道你家底缺什么——先去跑一次盘点/同步，"
                         "回来再点建议。")
        return empty
    if maps is None:
        maps = load_maps()
    if situation_path is None:
        situation_path = STATE_DIR / SITUATION_FILENAME
    party_levels = party_levels_from_situation(situation_path)
    start_min = max(0, int(now_min) + SUGGEST_LEAD_MIN)

    counts: dict[int, int] = {}
    for team, count in (committed_counts or {}).items():
        try:
            team_no, c = int(team), int(count)
        except (TypeError, ValueError):
            continue
        if c > 0:
            counts[team_no] = c
    remaining = {t: max(0, rounds - counts.get(t, 0)) for t in teams}
    wanted = sum(remaining.values())
    if wanted <= 0:
        empty["note"] = (f"能丢的队伍今天的班都排上了（每队 {rounds} 班），"
                         "不用再点。")
        return empty

    occupied = {str(code) for code in occupied_maps or () if code}
    windows = list(occupied_windows or ())

    failed = set()
    for combo in failed_combos or ():
        try:
            code, team = combo
            failed.add((str(code), int(team)))
        except (TypeError, ValueError):
            continue

    base_order = shortage_order(planning, len(FORGE_RESOURCES) + 1)
    focus = prefs.get("resource_focus")
    if focus in FOCUS_RESOURCES:
        base_order = [(focus, "manual")] + [item for item in base_order if item[0] != focus]
    suggestions = []
    preset_options = {}
    selection_notes = []
    blocked_teams = set()
    for team in teams:
        fid = prefs["team_formations"].get(str(team), "")
        if not fid:
            continue
        rejections = {}
        options = expedition_formation_options(team, party_levels, rejections)
        preset_options[team] = [option for option in options if option["formation_id"] == fid]
        if not preset_options[team]:
            blocked_teams.add(team)
            reason = rejections.get(fid) or rejections.get("*") or "预设已删除或覆盖的是另一支部队，请重新选择"
            selection_notes.append(f"部队{TEAM_NAMES[team]}指定预设未能安排：{reason}；不会改用现有编队。")
    misses = []
    reserved_names = {}
    placed_retry_notes = []  # 班排上了但撞过拉黑组合，如实知会一声
    busy: dict[int, int] = {}
    for team, until in (team_busy_until or {}).items():
        try:
            team_no, minute = int(team), int(until)
        except (TypeError, ValueError):
            continue
        busy[team_no] = max(busy.get(team_no, 0), minute)
    next_start = {}
    for t in teams:
        start = start_min
        if t in busy:  # 已排的班占着时间，补班排在收工+缓冲后
            start = max(start, busy[t] + COLLECT_BUFFER_MIN)
        next_start[t] = start
    for r in range(rounds):
        for team in teams:
            if remaining[team] <= r or team in blocked_teams:
                continue
            from .task_reservations import next_dispatch
            start = next_dispatch(next_start[team], task_windows)
            shift_no = counts.get(team, 0) + r + 1  # 今天第几班（含已排的）
            # 固定到「队伍 × 当天班次」，采纳首班后刷新不能把次班资源重置。
            priority_slot = 0 if focus in FOCUS_RESOURCES else (shift_no - 1) * len(teams) + teams.index(team)
            assigned = base_order[priority_slot % len(base_order)]
            placed = False
            restrictions = []
            formation_change = False
            miss = {"team": team, "shift": r + 1, "resource": "",
                    "fit": False, "level": False, "occupied": False,
                    "type": False, "retry": False, "detail": "",
                    "retry_resource": ""}
            for i in range(len(base_order)):
                # 本轮资源排不出（没产图/图被占/队不够格）就顺延下一种
                resource, tag = base_order[(priority_slot + i) % len(base_order)]
                ranked = sorted(
                    (item for item in maps.items()
                     if _per_hour(item[1], resource) > 0
                     and int(item[1].get("duration_min") or 0) > 0),
                    key=lambda item: -_per_hour(item[1], resource))
                if not ranked:
                    if not miss["resource"]:
                        miss["resource"] = resource
                    continue
                if not miss["resource"]:
                    miss["resource"] = resource
                candidates = ([(code, meta, preset) for preset in preset_options[team] for code, meta in ranked]
                              if team in preset_options else [(code, meta, None) for code, meta in ranked])
                for map_code, meta, preset in candidates:
                    if preset and any(name.removesuffix("·极").strip() in reserved_names and reserved_names[name.removesuffix("·极").strip()] != team for name in preset["party"]["names"]):
                        miss["detail"] = "指定预设的人选已被另一支远征队预留"
                        continue
                    def blocked(detail):
                        if resource == assigned[0]:
                            restrictions.append(f"{map_code}：{detail}")

                    duration = int(meta.get("duration_min") or 0)
                    if map_code in occupied or any(code == map_code and start < end and start + duration > begin
                                                   for code, begin, end in windows):
                        miss["occupied"] = True
                        owner = next((s["team_no"] for s in suggestions if s["map_code"] == map_code), None)
                        blocked(f"已安排部队{TEAM_NAMES.get(owner, owner)}" if owner else "已有远征安排")
                        continue
                    duration = int(meta.get("duration_min") or 0)
                    if start > DAY_END_MIN:
                        miss["fit"] = True
                        blocked("今天剩余时间排不下")
                        continue
                    party = preset["party"] if preset else (party_levels or {}).get(team)
                    level_detail = _level_shortfall(meta, party)
                    shortfall = _type_shortfall(meta, party)
                    if level_detail:
                        miss["level"] = True
                        if resource == assigned[0]:
                            formation_change = True
                        blocked(level_detail + (f"；{_type_block_detail(team, party, shortfall)}" if shortfall else ""))
                        continue
                    if shortfall is not None:
                        miss["type"] = True
                        if resource == assigned[0]:
                            formation_change = True
                        blocked(_type_block_detail(team, party, shortfall))
                        if not miss["detail"]:
                            miss["detail"] = (
                                f"{map_code}要{_type_req_text(meta)}，"
                                + _type_block_detail(team, party, shortfall))
                        continue
                    # 同图同队今天 failed 过的组合拉黑到今天结束（图不拉黑）
                    if (map_code, team) in failed:
                        miss["retry"] = True
                        blocked("本队今天派遣失败，暂不重试")
                        if not miss["retry_resource"]:
                            miss["retry_resource"] = resource
                        continue
                    per_hour = _per_hour(meta, resource)
                    rank = _resource_rank(maps, resource, map_code)
                    capacity = _forge_capacity(planning, resource)
                    req_text = _type_req_text(meta)
                    type_note = ""
                    if req_text:
                        known = [t for t in party_sword_types(party) if t] \
                            if party else []
                        type_note = f"刀种也够格（{req_text}）" if known \
                            else "刀种没核到"
                    reason = _reason(resource, tag, team, map_code,
                                     str(meta.get("name") or map_code),
                                     rank if per_hour > 0 else None,
                                     capacity, party, type_note)
                    shift_cn = TEAM_NAMES.get(shift_no, shift_no)
                    reason += f"；这队今天第{shift_cn}班"
                    if i:
                        reason += (f"；本轮的{assigned[0]}排不出，"
                                   f"顺延补{resource}")
                    if preset:
                        reserved_names.update({name.removesuffix("·极").strip(): team for name in preset["party"]["names"]})
                        reason += f"；先换成「{preset['formation_name']}」再派出"
                    suggestions.append({
                        **({key: preset[key] for key in ("formation_id", "formation_name", "formation_signature")} if preset else {}),
                        "kind": "expedition",
                        "key": f"suggest:{team}:{map_code}:{start}",
                        "team_no": team,
                        "map_code": map_code,
                        "map_name": str(meta.get("name") or map_code),
                        "resource": resource,
                        "duration_min": duration,
                        "start_min": start,
                        "shift_no": shift_no,
                        "reason": reason,
                        "blocked_resource": assigned[0] if i else None,
                        "restrictions": restrictions if i else [],
                        "formation_change": formation_change if i else False,
                    })
                    next_start[team] = start + duration + COLLECT_BUFFER_MIN
                    windows.append((map_code, start, start + duration))
                    placed = True
                    break
                if placed:
                    break
            if placed:
                if miss["retry"]:
                    placed_retry_notes.append(
                        (team, shift_no, miss["retry_resource"],
                         suggestions[-1]["resource"]))
            else:
                misses.append(miss)

    notes = list(selection_notes)
    for team, shift_no, blocked_resource, placed_resource \
            in placed_retry_notes:
        team_cn = TEAM_NAMES.get(team, team)
        shift_cn = TEAM_NAMES.get(shift_no, shift_no)
        notes.append(f"部队{team_cn}第{shift_cn}班：{blocked_resource}这班"
                     f"今天没派成，今天暂不重试同队同图；改排了{placed_resource}。")
    for miss in misses:
        team_cn = TEAM_NAMES.get(miss["team"], miss["team"])
        shift_cn = TEAM_NAMES.get(miss["shift"], miss["shift"])
        head = f"部队{team_cn}第{shift_cn}班（{miss['resource']}）"
        if miss["team"] in preset_options:
            head += f" · 指定「{preset_options[miss['team']][0]['formation_name']}」"
        frags = []
        if miss["detail"] and not miss["type"]:
            frags.append(miss["detail"])
        if miss["occupied"]:
            frags.append("对口图今天都有班在跑或已点上，明天再丢")
        if miss["retry"]:
            frags.append("这班今天没派成，今天暂不重试同队同图，换队/换图试试")
        if miss["type"]:
            frags.append(f"刀种门槛卡住：{miss['detail']}")
        if miss["fit"]:
            frags.append("对口图次日 03:59 前无法出发，今天塞不进这班了")
        if miss["level"]:
            frags.append("这队等级都不够对口图")
        if frags:
            notes.append(f"{head}：" + "；".join(frags) + "。")
        else:
            notes.append(f"{head}排不出班（图太晚或队不够格）。")
    if suggestions and len(suggestions) < wanted:
        notes.append(f"想给能丢的队各排 {rounds} 班，"
                     f"只排得出 {len(suggestions)} 班。")
    return {"suggestions": suggestions,
            "note": "；".join(notes) if notes else None}
