# -*- coding: utf-8 -*-
"""可解释的远征纸面规划器 v1（纯函数，无 UI、不换人、不派遣）。

输入（全部显式传入，不另造事实库）：
  - profile：当前本丸共用档案（touken/honmaru_profile.build_honmaru_profile），
    含候选池与五队 roster 链接层；
  - member_facts：近侍/修行/手入事实。事实层目前没有这三项的观察入口，
    由调用方申报；三类各有完整度语义——attendant 必须是带
    sword_catalog_id 的 dict（近侍必定存在，没有「无近侍」申报），
    training/repair 必须显式给 list（[] = 确认无人受限；缺键/None =
    未知）。任何一类未知，整卷降级 needs_confirmation，绝不把缺字段
    当成明确为空；
  - weights：目标权重（有序列表或 {资源: 权重}），默认 加速符 > 小判 >
    当前最缺的基础资源（老大个人偏好，可覆盖，不是所有玩家的规则）；
  - allow_all_teams_away：默认保留至少一支确认非空队在家打日课；
    显式 True 才让全部可派队出门（无留守时带醒目警告）；

硬规则（牛老师工单 2026-09-14 口径）：
  - 每张图验：最低人数（若规则已知）、全队等级合计、指定刀种「至少包含」；
    规则不全的图只给 rule_incomplete，不输出伪安全方案；
  - 修行中/手入中的具体刀不能远征；受伤（含重伤）不影响远征资格，只如实展示；
  - 近侍只扣那一振具体刀：多重集容量（组内 n 振、占用 1）只证明
    「本丸另有一振可用」，不能证明固定队槽位里这振不是被占用者——
    无法对到具体实例时该队 needs_confirmation，提示确认/换另一振，
    绝不写成本队已用另一振；n=1 时容量为 0 硬拦；
  - 同一远征地点同时只能派一支队（wikiwiki 远征规则，老大查证
    2026-09-14）：最终 assignments 同一 map_code 至多一次，
    跨队穷举联合分配，先最大化整卷置信等级和、再最大化得分和；
  - same_team_exclusion_key 只管「同位刀不能同队」（复用
    honmaru_profile.formation_conflicts），与近侍约束是两套独立规则；
  - 樱吹雪阈值：fatigue >= 50（49 是自然恢复上限，不算花）；
    大成功是概率优化不是硬门槛，没有可靠概率数据，只输出
    「更有利/无法精确估算」，绝不编造精确数字；
  - 进行中远征（expeditions.json 只读解析，见 expedition_state）：
    未到期记录排除该队且占用其 map_code；已到期视为已归来；
    记录损坏/时间不可靠/地点未知一律降级，绝不静默当全员空闲；
  - 留守只认「已确认可出阵」的队（reserve_ready）：近侍队可以，
    仅不满足远征专属条件的可以；在外/修行/手入/重伤/同队冲突/
    槽位或成员状态未知/未观测/空队都不算；
  - 零派遣输出机器可读 outcome：assigned / intentionally_staying_home /
    waiting_for_active_expeditions / no_feasible_assignment（infeasible），
    状态未知一律 needs_confirmation，不把「全体留守」当万能成功。
    waiting 优先级：仅当某队原本满足硬条件的图被活跃占图全部占掉
    （occupied_blocked_teams），或已观测非空队全部在外面；家中队
    纯硬失败/无可用图不算等待。waiting 表示本卷下一步动作，各队
    具体原因仍留在 per-team infeasible。
"""

import time
from pathlib import Path

from . import sword_db
from .expedition_state import load_active_expeditions, parse_expedition_records
from .honmaru_profile import build_honmaru_profile, formation_conflicts

PLAN_SCHEMA_VERSION = 2

_MAPS_FILE = Path(__file__).parent / "data" / "expedition_maps.json"
BASE_RESOURCES = ("木炭", "玉钢", "冷却材", "砥石")
YIELD_RESOURCES = BASE_RESOURCES + ("委托符", "加速符", "小判")
SAKURA_FATIGUE_MIN = 50
# 档案/roster 观测超过这么久就提示陈旧（不硬拦截）
STALE_HOURS = 24

# 默认目标：老大个人偏好（加速符 > 小判 > 最缺的基础资源），可覆盖
_DEFAULT_GOALS = ("加速符", "小判", "most_lacking_base")

_CONFIDENCE_RANK = {"infeasible": 0, "rule_incomplete": 1,
                    "needs_confirmation": 2, "executable": 3}


def load_maps(path=None) -> dict:
    """读远征地图表（含 rules 子结构）。Returns: {map_code: map_info}。"""
    import json
    with open(path or _MAPS_FILE, encoding="utf-8") as f:
        return json.load(f)["maps"]


def latest_inventory_resources(store) -> dict | None:
    """最新一次 inventory.captured 的 resources（没有就 None）。"""
    events = store.recent_events(limit=1, event_type="inventory.captured")
    if not events:
        return None
    resources = (events[0].get("payload") or {}).get("resources")
    return resources if isinstance(resources, dict) else None


def most_lacking_base_resource(resources: dict | None):
    """四种基础资源里当前最少的一种；读不出/并列无法裁决时如实 None。"""
    if not resources:
        return None
    known = {r: resources.get(r) for r in BASE_RESOURCES
             if isinstance(resources.get(r), (int, float))}
    if not known:
        return None
    floor = min(known.values())
    leaders = sorted(r for r, v in known.items() if v == floor)
    if len(leaders) > 1:
        return None  # 并列最缺，不替玩家拍板
    return leaders[0]


def normalize_goals(weights, resources) -> tuple[list[dict], list[str]]:
    """目标权重归一成 [(resource, weight)]，按位次赋分（第 1 名权重最高）。

    weights=None → 默认偏好（加速符>小判>最缺基础资源）；
    有序 list → 按顺序；dict → 显式权重。返回 (goals, notes)。
    """
    notes = []
    if weights is None:
        lacking = most_lacking_base_resource(resources)
        ordered = []
        for g in _DEFAULT_GOALS:
            if g == "most_lacking_base":
                if lacking:
                    ordered.append(lacking)
                else:
                    notes.append("当前最缺基础资源算不出（库存快照缺失或并列），"
                                 "第三目标空缺")
            else:
                ordered.append(g)
        goals = [{"resource": r, "weight": float(len(ordered) - i)}
                 for i, r in enumerate(ordered)]
        notes.append("使用默认目标偏好：" + " > ".join(r["resource"] for r in goals))
        return goals, notes
    if isinstance(weights, dict):
        goals = [{"resource": r, "weight": float(w)}
                 for r, w in weights.items() if w]
        goals.sort(key=lambda g: -g["weight"])
        return goals, notes
    ordered = list(weights)
    return ([{"resource": r, "weight": float(len(ordered) - i)}
             for i, r in enumerate(ordered)], notes)


# ---------------------------------------------------------------- 成员视图

def _entry_form(entry: dict) -> str:
    """盘点行的形态：只信形态证据链的 form_status（一览徽章直读/编队页
    直读，规则见 team_roster）。历史版本曾拿 kiwame_date 推形态——那是
    每振刀都有的「显现日期」，不是极化证据（2026-09-15 P0 拔毒）；
    证据不足一律 unknown，宁可分组保守也不吃假身份。"""
    status = entry.get("form_status")
    return status if status in ("kiwame", "normal") else "unknown"


def _entry_group(entry: dict):
    """等价候选多重集组键：(catalog_id, 形态)。身份未知不分组。"""
    catalog_id = entry.get("sword_catalog_id")
    if not catalog_id:
        return None
    return (catalog_id, _entry_form(entry))


def _slot_form(slot: dict) -> str:
    status = slot.get("kiwame_status")
    return status if status in ("kiwame", "normal") else "unknown"


def _normalize_facts(member_facts) -> tuple[list[dict], list[str], bool]:
    """调用方申报的近侍/修行/手入事实 → (占用声明, 警告, 事实完整?)。

    每条声明 {"kind": attendant/training/repair, "sword_catalog_id":...,
    "form": kiwame/normal/None}。form=None 表示申报方也不知道形态，
    占用会落在该 catalog 的所有形态组上（不确定占用）。

    三类事实各有明确完整度语义，缺一即整卷降级 needs_confirmation：
      - attendant：近侍必定存在（本丸看板），必须是带 sword_catalog_id
        的 dict；缺键/None/缺 id 都是「未知」，不存在「没有近侍」；
      - training / repair：键存在且为 list 才算「已确认」——显式 []
        表示当前无人修行/手入；缺键或 None 是「未提供/未知」，
        绝不把缺字段当成明确为空。
    """
    claims, warnings = [], []
    if not member_facts:
        warnings.append("未提供近侍/修行/手入事实——这不等于无人受限："
                        "所有方案按 needs_confirmation 降级，派遣前请人工核对")
        return claims, warnings, False
    complete = True
    att = member_facts.get("attendant")
    if isinstance(att, dict) and att.get("sword_catalog_id"):
        claims.append({"kind": "attendant",
                       "sword_catalog_id": att["sword_catalog_id"],
                       "form": att.get("form")})
    else:
        complete = False
        warnings.append("近侍身份未申报/未知：各队「不含近侍本人」无法确认，"
                        "方案降级 needs_confirmation")
    for kind in ("training", "repair"):
        value = member_facts.get(kind)
        if not isinstance(value, list):
            complete = False
            warnings.append(f"{_KIND_LABEL_FACTS[kind]}事实未提供/未知"
                            "（显式传 [] 才表示确认无人受限）："
                            "相关占用无法排除，方案降级 needs_confirmation")
            continue
        for item in value:
            if isinstance(item, dict) and item.get("sword_catalog_id"):
                claims.append({"kind": kind,
                               "sword_catalog_id": item["sword_catalog_id"],
                               "form": item.get("form")})
    return claims, warnings, complete


_KIND_LABEL_FACTS = {"training": "修行中", "repair": "手入中"}


_KIND_LABEL = {"attendant": "近侍", "training": "修行中", "repair": "手入中"}


def _group_capacities(entries, claims):
    """每组等价候选的可用容量。

    Returns:
        sizes: {group: 观察振数}
        occupied_definite: {group: 确定占用数}（形态明确的声明）
        occupied_possible: {group: 不确定占用数}（形态未知的声明可能落在这组）
        occ_kinds: {group: {占用来历...}}（attendant/training/repair）
        unmatched_claims: 候选池里找不到对应组的声明（档案可能过期）
    """
    sizes, occ_def, occ_pos, occ_kinds = {}, {}, {}, {}

    def _occupy(group, claim, bucket):
        bucket[group] = bucket.get(group, 0) + 1
        occ_kinds.setdefault(group, set()).add(claim["kind"])

    for entry in entries:
        group = _entry_group(entry)
        if group:
            sizes[group] = sizes.get(group, 0) + 1
    unmatched = []
    for claim in claims:
        if claim.get("form") in ("kiwame", "normal"):
            group = (claim["sword_catalog_id"], claim["form"])
            if group in sizes:
                _occupy(group, claim, occ_def)
            else:
                unmatched.append(claim)
        else:
            groups = [g for g in sizes if g[0] == claim["sword_catalog_id"]]
            if not groups:
                unmatched.append(claim)
            for group in groups:
                _occupy(group, claim, occ_pos)
    return sizes, occ_def, occ_pos, occ_kinds, unmatched


def _member_view(slot: dict, entries_by_id: dict) -> dict:
    """一个编队槽 → 规划用成员视图（保留原始观察，不补默认值）。"""
    observed = slot.get("observed") or {}
    entry = entries_by_id.get(slot.get("observation_id"))
    candidates = [entries_by_id[oid] for oid in slot.get("candidate_ids") or []
                  if oid in entries_by_id]
    level = observed.get("level")
    level_basis = "roster"
    if level is None and entry and entry.get("level") is not None:
        level = entry["level"]
        level_basis = "candidate_pool"  # 盘点值，可能比编队页旧
    form = _slot_form(observed)
    if form == "unknown" and entry:
        form = _entry_form(entry)
    groups = set()
    if slot.get("link_status") == "linked" and entry:
        group = _entry_group(entry)
        if group:
            groups.add(group)
    else:
        for cand in candidates:
            group = _entry_group(cand)
            if group and (form == "unknown" or group[1] == form):
                groups.add(group)
    return {
        "slot": slot.get("slot"),
        "name": observed.get("name"),
        "sword_catalog_id": observed.get("sword_catalog_id"),
        "sword_type": observed.get("sword_type"),
        "level": level, "level_basis": level_basis,
        "fatigue": observed.get("fatigue"),
        "injury": observed.get("injury"),
        "kiwame_status": observed.get("kiwame_status"),
        "form": form,
        "link_status": slot.get("link_status"),
        "observation_id": slot.get("observation_id"),
        "candidate_ids": list(slot.get("candidate_ids") or []),
        "identity_groups": sorted(groups),
        "unknown_fields": list(observed.get("unknown_fields") or []),
    }


def _team_availability(members, sizes, occ_def, occ_pos, occ_kinds):
    """近侍/修行/手入占用检查（诚实语义：容量 ≠ 实例清白）。

    多重集容量（组内 n 振 - 占用 k = 剩 n-k）只能证明「本丸存在另一振
    可用」，不能证明当前固定队伍槽位里的这一振恰好不是被占用者——
    本版不从候选池重新选人，只评估已编好的队伍：
      - 槽位的等价组完全没有占用声明 → 不受影响（不出现在结果里）；
      - 确定抽取 d 振 > 组内最好情况可用 max_available
        → blocked（数学上必然带上了被占用的那振）；
      - 其余任何从有占用声明的组里抽人的情况 → uncertain：
        容量够也只说明「本丸另有一振可用」，需人工确认或换成另一振，
        绝不写成本队已经用了另一振（当前事实层没有把占用声明链接到
        具体槽位实例的证据，不造假接口）。

    Returns: (blocked 文案, blocked_kinds, uncertain 文案, uncertain_kinds,
              notes)。blocked/uncertain 的 kinds 都是结构化集合，不靠解析
              中文文案：blocked_kinds 里 attendant 拦截的队仍可出阵留守，
              training/repair 不行；uncertain_kinds 里 training/repair
              身份不明同样失去留守资格（当前槽位可能就是被占用者），
              attendant 身份不明不影响留守（近侍本人本来就能随队出阵）。
    """
    definite, possible = {}, {}
    for m in members:
        groups = m["identity_groups"]
        if m["link_status"] == "linked" and len(groups) == 1:
            definite[groups[0]] = definite.get(groups[0], 0) + 1
        else:
            for g in groups:
                possible[g] = possible.get(g, 0) + 1

    blocked, blocked_kinds, uncertain, uncertain_kinds, notes = \
        [], set(), [], set(), []
    for group in set(definite) | set(possible):
        size = sizes.get(group, 0)
        d = definite.get(group, 0)
        p = possible.get(group, 0)
        od = occ_def.get(group, 0)
        op = occ_pos.get(group, 0)
        if not od and not op:
            continue
        label = _group_label(group)
        kinds = "、".join(_KIND_LABEL[k] for k in
                          sorted(occ_kinds.get(group) or ()))
        max_avail = size - od
        if d > max_avail:
            blocked.append(f"{label}：名额已全被{kinds}占用"
                           f"（观察 {size} 振，占用 {od + op}）")
            blocked_kinds.update(occ_kinds.get(group) or ())
        else:
            free = size - od - op
            spare = (f"本丸另有 {free} 振可用" if free > 0
                     else "本丸没有可替换的同型余量")
            uncertain.append(
                f"{label}：组内 {size} 振、{kinds}占 {od + op} 名额，"
                f"无法确认本队这振是不是被占用的那一振——{spare}，"
                "需人工确认或换成另一振后再派")
            uncertain_kinds.update(occ_kinds.get(group) or ())
    return blocked, blocked_kinds, uncertain, uncertain_kinds, notes


def _group_label(group) -> str:
    catalog_id, form = group
    info = sword_db.all_swords().get(catalog_id) or {}
    name = info.get("name_zh") or info.get("name") or catalog_id
    suffix = {"kiwame": "（极化）", "normal": "（普通）"}.get(form, "")
    return f"{name}{suffix}"


# ---------------------------------------------------------------- 地图条件

def check_map_eligibility(map_code: str, map_info: dict,
                          members: list[dict]) -> dict:
    """单队单图硬条件检查（纯函数）。

    members：_member_view 列表（只含占用槽）。
    Returns: {eligible, confidence, passed, failures, unknowns}
      confidence: executable / needs_confirmation / rule_incomplete / infeasible
    """
    rules = map_info.get("rules") or {}
    passed, failures, unknowns = [], [], []

    # 最低人数：null = 不知道，不是「没有要求」
    # （slot_status=unknown 的槽不在 members 里，队伍构成的不确定性
    #   由调用方在队伍层降级，这里只数确认占用的）
    min_members = rules.get("min_members")
    n = len(members)
    if isinstance(min_members, int):
        if n >= min_members:
            passed.append(f"在队 {n} 振 ≥ 最低 {min_members} 振")
        else:
            failures.append(f"在队 {n} 振 < 最低 {min_members} 振")

    # 总等级
    req_level = rules.get("total_level")
    if isinstance(req_level, int):
        known_sum = sum(m["level"] for m in members
                        if isinstance(m["level"], (int, float)))
        n_no_level = sum(1 for m in members
                         if not isinstance(m["level"], (int, float)))
        if n_no_level == 0:
            if known_sum >= req_level:
                passed.append(f"全队等级合计 {known_sum} ≥ {req_level}")
            else:
                failures.append(f"全队等级合计 {known_sum} < {req_level}")
        else:
            if known_sum >= req_level:
                passed.append(f"已确认等级合计 {known_sum} ≥ {req_level}"
                              f"（{n_no_level} 振等级未知，按 0 计也够）")
            else:
                unknowns.append(f"{n_no_level} 振等级读不出，已确认合计 "
                                f"{known_sum} / 要求 {req_level}，无法确认")

    # 指定刀种「至少包含」
    required_types = rules.get("required_types") or {}
    for type_name, need in sorted(required_types.items()):
        have = sum(1 for m in members if m["sword_type"] == type_name)
        maybe = sum(1 for m in members if m["sword_type"] is None)
        if have >= need:
            passed.append(f"含{type_name} {have} 振 ≥ {need} 振")
        elif have + maybe >= need:
            unknowns.append(f"确认含{type_name} {have} 振 < {need} 振，"
                            f"{maybe} 振刀种未知，无法确认")
        else:
            failures.append(f"需要至少 {need} 振{type_name}，"
                            f"全队确认只有 {have} 振")

    confidence = "executable"
    if failures:
        confidence = "infeasible"
    elif unknowns:
        confidence = "needs_confirmation"
    if confidence != "infeasible" and rules.get("completeness") != "complete":
        unknown_aspects = rules.get("unknown_aspects") or ["未知面未标注"]
        confidence = "rule_incomplete"
        unknowns.append(f"该图规则不完整（{'、'.join(unknown_aspects)}无依据），"
                        "以上通过项不构成可执行保证")
    return {"map_code": map_code, "eligible": not failures,
            "confidence": confidence,
            "passed": passed, "failures": failures, "unknowns": unknowns}


# ---------------------------------------------------------------- 方案生成

def _per_hour_yields(map_info: dict) -> dict:
    duration = map_info.get("duration_min") or 0
    if duration <= 0:
        return {}
    hours = duration / 60.0
    return {r: round((map_info.get(r) or 0) / hours, 3) for r in YIELD_RESOURCES}


def _score(map_info: dict, goals: list[dict],
           max_per_hour: dict) -> float | None:
    """按时薪加权打分；时长或收益缺失 → None（不参与排名）。

    各资源时薪先按全图表内的最高时薪归一（该资源最好的图=1.0），
    再加权求和——不同资源量级差几个数量级（小判几百 vs 加速符个位），
    不归一的话名义上的「优先加速符」会被绝对数值架空。
    """
    per_hour = _per_hour_yields(map_info)
    if not per_hour:
        return None
    total = 0.0
    for g in goals:
        peak = max_per_hour.get(g["resource"]) or 0
        if peak > 0:
            total += g["weight"] * per_hour.get(g["resource"], 0) / peak
    return total


def _max_per_hour(maps: dict) -> dict:
    peak = {}
    for info in maps.values():
        for r, v in _per_hour_yields(info).items():
            peak[r] = max(peak.get(r, 0), v)
    return peak


def _great_success_note(members) -> dict:
    """樱吹雪只按 fatigue>=50；没有可靠概率数据，只给定性结论。"""
    occupied = members
    if not occupied:
        return {"status": "unknown", "note": "没有成员事实，无法评估"}
    bloomed = [m for m in occupied
               if isinstance(m["fatigue"], (int, float))
               and m["fatigue"] >= SAKURA_FATIGUE_MIN]
    unknown = [m for m in occupied
               if not isinstance(m["fatigue"], (int, float))]
    if unknown:
        return {"status": "unknown",
                "note": f"{len(unknown)} 振疲劳读不出，大成功条件无法评估；"
                        "飘花只影响大成功概率，不影响普通成功"}
    if len(bloomed) == len(occupied):
        return {"status": "favored",
                "note": "全员樱吹雪（疲劳≥50），大成功更有利"
                        "（概率无可靠数据，无法精确估算）"}
    return {"status": "not_favored",
            "note": f"{len(bloomed)}/{len(occupied)} 振飘花，"
                    "不影响普通成功；大成功概率无法精确估算"}


def _sacrifices(map_info: dict) -> list[str]:
    duration = map_info.get("duration_min") or 0
    out = []
    if duration:
        hours = duration / 60.0
        out.append(f"该队约 {hours:.1f} 小时内无法出阵/内番/演练")
    return out


def _team_options(team: dict, members: list[dict], maps: dict,
                  goals: list[dict], now: float, next_online,
                  max_per_hour: dict, team_uncertain: list[str],
                  obs_status: str):
    """一队的全部候选图（不做最终选择，选图归全局联合分配）。

    队伍层的不确定性（占用身份不明/观测 partial/槽位读不出）在这里
    统一压进每个候选的 confidence 和 uncertainties。

    Returns: (options, rejections)
      rejections = [{"map_code", "failures", "unknowns"}]——每张没选上的图
      为什么不行，可行性审计要用。
    """
    options, rejections = [], []
    for code, info in maps.items():
        check = check_map_eligibility(code, info, members)
        if not check["eligible"]:
            rejections.append({"map_code": code,
                               "map_name": info.get("name") or "",
                               "failures": check["failures"],
                               "unknowns": check["unknowns"]})
            continue
        score = _score(info, goals, max_per_hour)
        duration = info.get("duration_min") or 0
        return_at = now + duration * 60 if duration else None
        return_note = None
        if return_at is not None and next_online and return_at > next_online:
            return_note = "预计归来晚于下次上线时间"
        confidence = check["confidence"]
        uncertainties = list(check["unknowns"])
        if team_uncertain or obs_status == "partial":
            confidence = ("needs_confirmation"
                          if confidence == "executable" else confidence)
            uncertainties += list(team_uncertain)
            if obs_status == "partial":
                uncertainties.append("该队编队观测本身是 partial")
        yields = {r: info.get(r) or 0 for r in YIELD_RESOURCES}
        options.append({
            "team_no": team["team_no"], "map_code": code,
            "map_name": info.get("name") or "",
            "confidence": confidence,
            "why_eligible": check["passed"],
            "uncertainties": uncertainties,
            "score": score,
            "members": [_member_brief(m) for m in members],
            "duration_min": duration,
            "expected_return_at": return_at,
            "expected_return_text": (time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(return_at))
                                     if return_at else "未知"),
            "return_note": return_note,
            "yields": yields,
            "yields_per_hour": _per_hour_yields(info),
            "great_success": _great_success_note(members),
            "sacrifices": _sacrifices(info),
        })
    return options, rejections


def _option_rank(option: dict) -> tuple:
    """单选项排序键：置信等级优先，其次得分，最后地图编号（确定 Tie-break）。"""
    return (-_CONFIDENCE_RANK[option["confidence"]],
            -(option["score"] if option["score"] is not None else -1),
            option["map_code"])


def _global_assign(team_options: list[tuple[int, list[dict]]],
                   used_maps=frozenset()) -> dict:
    """跨队联合分配：同一远征地点同时只能派一支队（wikiwiki 远征规则
    「第一部隊～第五部隊は、それぞれ異なる遠征先に同時に派遣」「同一の
    遠征先には１部隊しか派遣できない」，老大查证 2026-09-14）。

    used_maps：进行中远征已占用的 map_code，与新 assignments 一起保证
    唯一性——活跃队在外，其地点本轮任何队都不得再去。

    穷举 DFS：队伍按 team_no 排序、候选按 map_code 排序，目标先最大化
    整卷置信等级和、再最大化整卷得分和；严格大于才替换首个最优解，
    结果与输入队伍顺序无关。5 队 × 20 图规模下穷举足够快，不引重依赖。

    Returns: {team_no: option}
    """
    teams = sorted(team_options, key=lambda t: t[0])
    best = {"key": None, "assign": None}

    def dfs(i, used, chosen, conf_sum, score_sum):
        if i == len(teams):
            key = (conf_sum, round(score_sum, 9))
            if best["key"] is None or key > best["key"]:
                best["key"] = key
                best["assign"] = dict(chosen)
            return
        team_no, options = teams[i]
        for opt in sorted(options, key=lambda o: o["map_code"]):
            if opt["map_code"] in used:
                continue
            used.add(opt["map_code"])
            chosen[team_no] = opt
            dfs(i + 1, used, chosen,
                conf_sum + _CONFIDENCE_RANK[opt["confidence"]],
                score_sum + (opt["score"] or 0.0))
            used.discard(opt["map_code"])
            del chosen[team_no]
        # 该队没有可去的图（都被占满）时也必须能走到叶子；
        # 置信和恒大于 0，能派时「不派」永远不会成为最优
        dfs(i + 1, used, chosen, conf_sum, score_sum)

    dfs(0, set(used_maps), {}, 0, 0.0)
    return best["assign"] or {}


def _objective(assign: dict) -> tuple:
    """整卷目标：(置信等级和, 得分和)，与 _global_assign 的口径一致。"""
    return (sum(_CONFIDENCE_RANK[o["confidence"]] for o in assign.values()),
            round(sum(o["score"] or 0.0 for o in assign.values()), 9))


def _reserve_readiness(rec: dict) -> tuple[bool, str | None]:
    """保守的留守出阵资格（reserve_ready）。

    只是规划层的保守资格，不承诺必能出阵——真实出阵仍必须走
    BattleMixin._safe_depart_stream + _confirm_departure。

    计入：非空、观测 complete、无读不出的槽位、成员伤势全部已知且
    无重伤、无修行/手入/同队冲突拦截。近侍所在队只是不能远征，
    可以作为留守出阵队；仅仅不满足远征专属条件（等级/人数/刀种）
    不妨碍留守资格；中伤/轻伤可作候选（状态由 injury_note 展示）。
    """
    if not rec["members"]:
        return False, "空队或全部槽位未占用"
    kinds = rec["blocked_kinds"] - {"attendant"}
    if kinds:
        label = {"training": "修行中", "repair": "手入中",
                 "conflict": "同队互斥冲突"}
        return False, ("含"
                       + "、".join(label.get(k, k) for k in sorted(kinds))
                       + "拦截")
    uncertain_kinds = rec.get("uncertain_kinds") or set()
    bad_uncertain = uncertain_kinds - {"attendant"}
    if bad_uncertain:
        label = {"training": "修行中", "repair": "手入中"}
        return False, (
            "、".join(label.get(k, k) for k in sorted(bad_uncertain))
            + "身份不明：当前槽位可能正是被占用的那一振，"
            "无法确认该队已安全")
    if rec["obs_status"] != "complete":
        return False, "编队观测不完整"
    if rec["unknown_slots"]:
        return False, "有槽位状态读不出"
    for m in rec["members"]:
        if m["injury"] is None:
            return False, "有成员伤势状态未知"
        if m["injury"] == "heavy":
            return False, "含重伤成员"
    return True, None


def _member_brief(m: dict) -> dict:
    return {"slot": m["slot"], "name": m["name"], "level": m["level"],
            "sword_type": m["sword_type"], "fatigue": m["fatigue"],
            "injury": m["injury"], "link_status": m["link_status"],
            "observation_id": m["observation_id"]}


def plan_expeditions(profile=None, *, store=None, member_facts=None,
                     weights=None, allow_all_teams_away=False,
                     now=None, next_online=None, maps=None,
                     inventory="__from_store__",
                     active_expeditions="__from_file__") -> dict:
    """远征纸面规划主入口（纯函数，不写库、不触碰游戏）。

    Args:
        profile: honmaru_profile 档案；None 时从 store/全局库现生成。
        store: TelemetryStore（profile/inventory 缺省时的数据来源）。
        member_facts: {"attendant": {"sword_catalog_id":..., "form":...},
                       "training": [...], "repair": [...]}；
                      三类都需显式申报（training/repair 给 [] 表示确认无人）；
                      None 或任一类缺失 = 未知（整卷降级 needs_confirmation）。
        weights: 目标权重（有序 list 或 dict）；None = 默认偏好。
        allow_all_teams_away: True 才允许所有确认非空队全出（此时若无
                      已确认可出阵的留守队会带醒目警告）；绝不能重派
                      正在外面的队。
        now/next_online: epoch 秒；next_online 只标注不拦截。
        maps: 地图表（默认 data/expedition_maps.json）。
        inventory: 资源 dict；"__from_store__" 时从 store 读最新库存。
        active_expeditions: 进行中远征。默认 "__from_file__" 只读解析
                      现有 expeditions.json（touken/expedition_state.py，
                      不另建状态）；显式传 expeditions.json 原始 dict
                      （{}=已确认当前没有进行中远征）供确定性测试。
                      记录损坏/时间不可靠不静默当全员空闲——见
                      expedition_state 的诚实契约。
    """
    now = now if now is not None else time.time()
    if store is None:
        from .telemetry import get_telemetry_store
        store = get_telemetry_store()
    if profile is None:
        profile = build_honmaru_profile(store)
    maps = maps or load_maps()
    max_per_hour = _max_per_hour(maps)
    if inventory == "__from_store__":
        inventory = latest_inventory_resources(store)

    warnings, explanation = [], []
    pool = profile.get("candidate_pool") or {}
    entries = pool.get("entries") or []
    entries_by_id = {e["observation_id"]: e for e in entries}
    if not pool.get("done"):
        warnings.append(f"候选池不可用：{pool.get('reason') or '原因未知'}——"
                        "编队成员无法对账，所有方案降级")
    goals, goal_notes = normalize_goals(weights, inventory)
    explanation.extend(goal_notes)
    if inventory is None:
        warnings.append("没有库存快照，「最缺基础资源」目标按空缺处理")

    # ---- 进行中远征（唯一事实来源 expeditions.json 的只读解析）----
    if active_expeditions == "__from_file__":
        active_info = load_active_expeditions(now)
    else:
        active_info = parse_expedition_records(active_expeditions, now)
    warnings.extend(active_info["warnings"])
    if active_info.get("reconciled"):
        # 调用方带游戏事件时 load_active_expeditions 已把游戏确认在家
        # 的残留记录剔出视图；这是翻篇说明不是降级理由（见
        # expedition_state.reconcile_view_with_game）。
        explanation.append(
            "部队" + "、".join(str(t) for t in active_info["reconciled"])
            + " 的派遣记录与游戏现状不符（已手动收菜？），"
              "以游戏为准视为已归来")
    active_problems = bool(active_info["warnings"])  # 损坏/未知一律降级
    away = active_info["active"]                     # {team_no: info}
    unknown_state_teams = set(active_info["unknown_end"])
    occupied_maps = {info["map_code"] for info in away.values()
                     if info["map_code"]}
    if active_info["status"] == "missing":
        explanation.append("没有远征派遣记录文件，按无进行中远征处理"
                           "（若有手动派遣请显式申报）")
    waiting_for = [{"team_no": t, **info} for t, info in sorted(away.items())]
    for w in waiting_for:
        explanation.append(
            f"部队{w['team_no']}正在远征「{w['map_name'] or w['map_code'] or '?'}」，"
            f"剩余约 {w['remain_min']} 分钟，预计 {w['return_text']} 归来；"
            "本轮不再分配，其地点其他队也不得再去")

    claims, fact_warnings, facts_complete = _normalize_facts(member_facts)
    warnings.extend(fact_warnings)
    sizes, occ_def, occ_pos, occ_kinds, unmatched = _group_capacities(
        entries, claims)
    for claim in unmatched:
        warnings.append(
            f"{_KIND_LABEL[claim['kind']]}申报的刀（{claim['sword_catalog_id']}）"
            "不在候选池里：档案可能过期，相关约束无法确认，方案降级")

    # 陈旧提示（不硬拦截）
    for label, ts in (("所持刀剑盘点", pool.get("observed_at")),):
        if ts and now - ts > STALE_HOURS * 3600:
            warnings.append(f"{label}观测于 {time.strftime('%m-%d %H:%M', time.localtime(ts))}"
                            f"，超过 {STALE_HOURS} 小时，可能陈旧")

    teams_out = []       # 每队一条评估记录 dict
    unobserved_teams = []
    for team in (profile.get("roster") or {}).get("teams") or []:
        team_no = team.get("team_no")
        # 正在远征的队：不再分配、不算留守，地点已被占用（解释已在上面给）
        if team_no in away or team_no in unknown_state_teams:
            continue
        obs_status = team.get("observation_status")
        if obs_status in (None, "unknown") and not team.get("slots"):
            explanation.append(f"部队{team_no}：无编队观测，不参与规划")
            unobserved_teams.append(team_no)
            continue
        if obs_status == "failed":
            warnings.append(f"部队{team_no}：编队观测失败，不参与规划")
            unobserved_teams.append(team_no)
            continue
        slots = [s for s in team.get("slots") or []
                 if s.get("slot_status") == "occupied"]
        unknown_slots = [s for s in team.get("slots") or []
                         if s.get("slot_status") == "unknown"]
        members = [_member_view(s, entries_by_id) for s in slots]
        blocked, blocked_kinds, uncertain, uncertain_kinds, avail_notes = \
            _team_availability(members, sizes, occ_def, occ_pos, occ_kinds)
        if unknown_slots:
            uncertain.append(f"{len(unknown_slots)} 个槽位状态读不出，"
                             "队伍构成无法完全确认")
        # 同队互斥（独立于近侍约束）：游戏本身不允许同位刀同队，
        # 观测到冲突=数据可疑，如实拦下。复用 formation_conflicts，
        # 键取槽位输出的 same_team_exclusion_key（身份未知=None 不判）。
        slot_entries = [{"observation_id": f"slot{s.get('slot')}",
                         "same_team_exclusion_key": s.get("same_team_exclusion_key")}
                        for s in slots]
        conflicts = formation_conflicts(slot_entries)
        if conflicts:
            blocked.append("队内存同位刀互斥冲突（观测数据可疑）："
                           + "；".join(f"键 {c['exclusion_key']} ×"
                                       f"{len(c['observation_ids'])}"
                                       for c in conflicts))
            blocked_kinds.add("conflict")
        rec = {"team": team, "members": members, "blocked": blocked,
               "blocked_kinds": blocked_kinds, "uncertain": uncertain,
               "uncertain_kinds": uncertain_kinds,
               "avail_notes": avail_notes, "obs_status": obs_status,
               "unknown_slots": unknown_slots}
        rec["reserve_ready"], rec["reserve_reason"] = _reserve_readiness(rec)
        teams_out.append(rec)

    # ---- 队伍评估：可远征的进候选，其余按留守资格分层 ----
    candidate_teams = []   # rec + options/rejections
    infeasible = []
    staying_nonempty = []  # 确认非空但不远征的队（空队/未观测/在外不算）
    for rec in teams_out:
        team = rec["team"]
        team_no = team["team_no"]
        if not rec["members"]:
            infeasible.append({"team_no": team_no,
                               "reasons": ["队伍为空或全部槽位未占用"]})
            continue
        if rec["blocked"]:
            infeasible.append({"team_no": team_no, "reasons": rec["blocked"]})
            staying_nonempty.append(team_no)
            continue
        options, rejections = _team_options(
            team, rec["members"], maps, goals, now, next_online,
            max_per_hour, rec["uncertain"], rec["obs_status"])
        if not options:
            reasons = ["所有远征图的硬条件都不满足或无法确认"]
            for r in rejections:
                for f in r["failures"]:
                    reasons.append(f"{r['map_code']} {r['map_name']}：{f}")
            infeasible.append({"team_no": team_no, "reasons": reasons,
                               "rejected_maps": rejections})
            staying_nonempty.append(team_no)
            continue
        rec = {**rec, "team_no": team_no,
               "options": options, "rejections": rejections}
        candidate_teams.append(rec)

    reserve_ready_of = {rec["team"]["team_no"]: rec["reserve_ready"]
                        for rec in teams_out}

    # ---- 跨队联合分配：同一远征地点同时只能派一支队（含活跃占用）----
    # 先扣除 active occupied_maps：可去图全被占满的队不是「可派队」，
    # 零派遣原因是占用而非留守策略，绝不能记成 intentionally_staying_home
    chosen_assign = {}
    holdout = None          # 默认保队时从可远征队里额外留下的那支
    holdout_reason = None
    free_candidates = []
    occupied_blocked_teams = []  # 原本有满足硬条件的图、只因活跃占图而
                                 # free_options 变空的队：零派遣与活跃
                                 # 占图直接相关（区别于纯硬失败）
    for cand in candidate_teams:
        free = [o for o in cand["options"]
                if o["map_code"] not in occupied_maps]
        if free:
            free_candidates.append({**cand, "free_options": free})
        else:
            occupied_blocked_teams.append(cand["team_no"])
            infeasible.append({
                "team_no": cand["team_no"],
                "reasons": ["可去的远征图都在被远征中的队伍占用"
                            "（同一远征地点同时只能派一支队）"],
                "rejected_maps": cand["rejections"]})
            staying_nonempty.append(cand["team_no"])
    reserve_staying_now = [n for n in staying_nonempty
                           if reserve_ready_of.get(n)]
    need_holdout = not reserve_staying_now
    if free_candidates:
        if allow_all_teams_away or not need_holdout:
            # 显式允许全出，或已有可出阵队天然留守：可派队全进联合分配
            chosen_assign = _global_assign(
                [(t["team_no"], t["free_options"]) for t in free_candidates],
                used_maps=occupied_maps)
        else:
            # 先确认「不执行留守策略时确实能派出去」，否则零派遣不是
            # 主动留守造成的
            full = _global_assign(
                [(t["team_no"], t["free_options"]) for t in free_candidates],
                used_maps=occupied_maps)
            if not full:
                pass  # 无图可派是占用/条件造成，与留守策略无关
            elif len(free_candidates) == 1:
                if free_candidates[0]["reserve_ready"]:
                    holdout = free_candidates[0]
                    holdout_reason = ("只有一支可安全出阵的非空队，默认保留在家"
                                      "打日课；如需派出请显式 "
                                      "allow_all_teams_away=True")
                else:
                    # 唯一的可远征队自身状态不安全，扣下来也当不了留守队
                    chosen_assign = full
            else:
                # 枚举「留哪支」：留守队必须自己能出阵（reserve_ready）
                # 优先，其次取留下后整卷最优（顺序无关）
                best = None
                for hold in free_candidates:
                    rest = [t for t in free_candidates if t is not hold]
                    assign = _global_assign(
                        [(t["team_no"], t["free_options"]) for t in rest],
                        used_maps=occupied_maps)
                    key = (1 if hold["reserve_ready"] else 0,
                           *_objective(assign))
                    if best is None or key > best[0]:
                        best = (key, hold, assign)
                _key, holdout, chosen_assign = best
                if holdout["reserve_ready"]:
                    holdout_reason = ("默认保留至少一支可出阵队伍在家（打日课）；"
                                      "如需全出请显式 allow_all_teams_away=True")
                else:
                    holdout_reason = ("没有任何状态安全的队可留守，只能留下状态"
                                      "未达出阵候选的一队；如需全出请显式 "
                                      "allow_all_teams_away=True")

    assignments = []
    by_no = {t["team_no"]: t for t in free_candidates}
    for team_no, opt in sorted(chosen_assign.items()):
        cand = by_no[team_no]
        a = dict(opt)
        others = [o for o in cand["free_options"]
                  if o["map_code"] != opt["map_code"]]
        a["alternatives"] = [{"map_code": o["map_code"],
                              "map_name": o["map_name"],
                              "confidence": o["confidence"],
                              "score": o["score"]}
                             for o in sorted(others, key=_option_rank)]
        a["rejected_maps"] = cand["rejections"]
        # 高分图因唯一地点约束落选时如实解释：被本轮别队占用，或被
        # 正在远征的队伍占用
        personal_best = sorted(cand["options"], key=_option_rank)[0]
        if personal_best["map_code"] != opt["map_code"]:
            pb_code = personal_best["map_code"]
            if pb_code in occupied_maps:
                holder_text = "正在远征的队伍"
            else:
                holder = next((no for no, o in chosen_assign.items()
                               if o["map_code"] == pb_code), None)
                holder_text = f"部队{holder}" if holder else "其他队伍"
            a["occupancy_note"] = (
                f"本队单看最优是 {pb_code} {personal_best['map_name']}，"
                f"但同一远征地点同时只能派一支队，该图已被{holder_text}占用，"
                f"整卷最优改派 {opt['map_code']} {opt['map_name']}")
        if cand["avail_notes"]:
            a["availability_notes"] = cand["avail_notes"]
        a["injury_note"] = _injury_note(cand["members"])
        assignments.append(a)

    # 联合分配后仍没分到图的可派队（可去的图被别队占满）：如实记原因，
    # 它非空留在家中，计入留守
    for cand in free_candidates:
        if cand["team_no"] in chosen_assign:
            continue
        if holdout is not None and cand["team_no"] == holdout["team_no"]:
            continue
        infeasible.append({
            "team_no": cand["team_no"],
            "reasons": ["可去的远征图都被其他队占满"
                        "（同一远征地点同时只能派一支队）"],
            "rejected_maps": cand["rejections"]})
        staying_nonempty.append(cand["team_no"])

    # ---- 留守与全出文案：只认「已确认可出阵」的留守队 ----
    staying = None
    staying_parts = []
    staying_teams = sorted(staying_nonempty)
    if staying_nonempty:
        reserve_nos = [n for n in staying_teams if reserve_ready_of.get(n)]
        not_ready_nos = [n for n in staying_teams
                         if not reserve_ready_of.get(n)]
        if reserve_nos:
            staying_parts.append(
                "可出阵队留守本丸："
                + "、".join(f"部队{n}" for n in reserve_nos))
        if not_ready_nos:
            staying_parts.append(
                "另有队伍在家但出阵状态不可确认："
                + "、".join(f"部队{n}" for n in not_ready_nos))
    if holdout is not None:
        staying_teams = sorted(staying_teams + [holdout["team_no"]])
        staying_parts.append(holdout_reason)
        reserve_ready_of[holdout["team_no"]] = holdout["reserve_ready"]
    if staying_teams:
        staying = {"teams": staying_teams,
                   "reason": "；".join(staying_parts)}

    reserve_staying = [n for n in staying_teams if reserve_ready_of.get(n)]
    all_away_warning = None
    if assignments:
        if reserve_staying:
            explanation.append(
                f"本轮新派出 {len(assignments)} 队（"
                + "、".join(f"部队{a['team_no']}" for a in assignments)
                + "），可出阵留守："
                + "、".join(f"部队{n}" for n in reserve_staying))
        else:
            parts = [f"本轮新派出 {len(assignments)} 队（"
                     + "、".join(f"部队{a['team_no']}" for a in assignments)
                     + "）"]
            if away:
                parts.append("原本已在远征途中："
                             + "、".join(f"部队{n}" for n in sorted(away)))
            if staying_teams:
                parts.append("在家的队均不可确认能出阵："
                             + "、".join(f"部队{n}" for n in staying_teams))
            all_away_warning = ("⚠️ 本丸将没有已确认可出阵的留守队（"
                                + "；".join(parts) + "）")
            if unobserved_teams:
                all_away_warning += (
                    "；另有未观测队伍（"
                    + "、".join(f"部队{n}" for n in unobserved_teams)
                    + "）状态未知，不能算作留守")

    # ---- 零派遣原因：机器可读 outcome，不一律当成功 ----
    # 优先级：主动留守 > 活跃占图导致可行选项归零（即使另有队硬失败）
    # > 非空队全在外面 > 纯硬失败/无可用图。waiting 只表示「这卷的下一步
    # 动作是等归来」，不代表每支队归来后都可行；各队原因留在 infeasible
    home_nonempty = [rec["team"]["team_no"] for rec in teams_out
                     if rec["members"]]
    if assignments:
        outcome = "assigned"
        plan_confidence = min((a["confidence"] for a in assignments),
                              key=lambda c: _CONFIDENCE_RANK[c])
    elif holdout is not None:
        outcome = "intentionally_staying_home"
        plan_confidence = "executable"   # 方案 = 主动留守，本身可执行
    elif occupied_blocked_teams:
        outcome = "waiting_for_active_expeditions"
        plan_confidence = "executable"   # 方案 = 等归来，本身可执行
        explanation.append(
            "没有新派遣："
            + "、".join(f"部队{n}" for n in occupied_blocked_teams)
            + " 原本满足硬条件的远征图都被正在远征的队伍占用；"
            + "、".join(f"部队{w['team_no']}预计 {w['return_text']} 归来"
                        for w in waiting_for)
            + "，归来后可再评估")
    elif away and not home_nonempty:
        outcome = "waiting_for_active_expeditions"
        plan_confidence = "executable"   # 方案 = 等归来，本身可执行
        explanation.append("没有新派遣是因为队伍正在远征："
                           + "、".join(f"部队{w['team_no']}预计 "
                                       f"{w['return_text']} 归来"
                                       for w in waiting_for))
    else:
        outcome = "no_feasible_assignment"
        plan_confidence = "infeasible"
    # 事实不完整/状态未知（没申报、申报对不上账、远征记录损坏或时间
    # 不可靠、活跃地点未知）：硬资格不确定，不得伪装成可直接执行
    if plan_confidence == "executable" and (
            not facts_complete or unmatched or active_problems
            or unknown_state_teams):
        plan_confidence = "needs_confirmation"

    return {"schema_version": PLAN_SCHEMA_VERSION,
            "generated_at": now,
            "inputs": {
                "profile_observed_at": pool.get("observed_at"),
                "pool_snapshot": pool.get("source"),
                "member_facts_status": ("provided" if member_facts
                                        else "missing"),
                "goals": goals,
                "allow_all_teams_away": bool(allow_all_teams_away),
                "active_expeditions": {
                    "status": active_info["status"],
                    "active_teams": sorted(away),
                    "occupied_maps": sorted(occupied_maps),
                    "unknown_state_teams": sorted(unknown_state_teams),
                },
                "now": now, "next_online": next_online,
            },
            "plan": {"confidence": plan_confidence,
                     "outcome": outcome,
                     "assignments": assignments,
                     "staying_home": staying,
                     "waiting_for": waiting_for,
                     "all_away_warning": all_away_warning},
            "infeasible": infeasible,
            "warnings": warnings,
            "explanation": explanation}


def _injury_note(members) -> str | None:
    """受伤不剔除，只如实展示。"""
    injured = [(m["name"], m["injury"]) for m in members
               if m["injury"] in ("light", "medium", "heavy")]
    if not injured:
        return None
    label = {"light": "轻伤", "medium": "中伤", "heavy": "重伤"}
    return ("带伤出征不影响远征资格："
            + "、".join(f"{n or '?'}({label[i]})" for n, i in injured))
