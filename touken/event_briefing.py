"""活动开幕参谋：喂活动参数和刀帐，输出编队/刀装/控花简报。

纯函数模块：不碰游戏画面、不碰网络，和 advisor.py 一个模子。
规则全部在 data/event_briefing_rules.json——改规矩改表，不动代码；
规则出处（攻略链接、老大口述）都记在表内 sources。

活动参数靠人喂（每期三分钟）：加成刀、伤害类型、战场类型、地图点数、
建议等级。自动读公告不在本模块范围内。
"""
from __future__ import annotations

import json
from pathlib import Path

_RULES_PATH = Path(__file__).resolve().parent / "data" / "event_briefing_rules.json"

DAMAGE_TYPES = ("virtual", "real")
BATTLE_TYPES = ("day", "night", "indoor", "mixed")
INJURY_ORDER = {None: 0, "轻伤": 1, "中伤": 2, "重伤": 3}

# 加成刀评级线：达到建议等级即「带得动」；差 20 级以内算「勉强」
_BONUS_MARGINAL_GAP = 20


def load_rules(path: Path | None = None) -> dict:
    """读规则表；文件缺失或损坏就 ValueError——规则表是正式资源，坏了要喊出来。"""
    path = Path(path) if path else _RULES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"活动参谋规则表读不出来：{exc}") from exc
    if not isinstance(data, dict) or "troops" not in data or "fatigue" not in data:
        raise ValueError("活动参谋规则表格式不对（缺 troops/fatigue）")
    return data


def _validate_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError("活动参数得是个字典")
    name = str(event.get("name") or "").strip()
    if not name:
        raise ValueError("活动得有个名字")
    damage = event.get("damage")
    if damage not in DAMAGE_TYPES:
        raise ValueError("伤害类型得填 virtual（虚拟伤害）或 real（真伤）")
    battle = event.get("battle")
    if battle not in BATTLE_TYPES:
        raise ValueError("战场类型得填 day/night/indoor/mixed 之一")
    points = event.get("map_points")
    if points is not None:
        if not isinstance(points, int) or isinstance(points, bool) or not 1 <= points <= 99:
            raise ValueError("地图点数得是 1~99 的整数（不知道就别填）")
    level = event.get("recommended_level")
    if level is not None:
        if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 99:
            raise ValueError("建议等级得是 1~99 的整数（不知道就别填）")
    bonus = event.get("bonus_swords") or []
    if not isinstance(bonus, list) or not all(isinstance(b, str) and b.strip() for b in bonus):
        raise ValueError("加成刀得是一串名字")
    return {
        "name": name,
        "damage": damage,
        "battle": battle,
        "map_points": points,
        "recommended_level": level,
        "bonus_swords": [b.strip() for b in bonus],
        "notes": str(event.get("notes") or "").strip()[:200],
    }


def _normalize_roster(roster: list[dict]) -> list[dict]:
    """刀帐条目清洗：缺等级/刀种的条目丢掉（不知道就是不知道），返回干净列表。"""
    clean = []
    for item in roster or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        stype = str(item.get("type") or "").strip()
        level = item.get("level")
        if not name or not stype or not isinstance(level, int) or level < 1:
            continue
        form = item.get("form")
        fatigue = item.get("fatigue")
        injury = item.get("injury")
        clean.append({
            "name": name,
            "type": stype,
            "level": level,
            "form": "kiwame" if form == "kiwame" else "normal",
            "fatigue": fatigue if isinstance(fatigue, (int, float)) else None,
            "injury": injury if injury in INJURY_ORDER else None,
            "slots": item.get("slots") if isinstance(item.get("slots"), int) else None,
        })
    return clean


# ── 加成刀评估 ──


def _judge_bonus_sword(name: str, owned: dict | None, event: dict) -> dict:
    verdict = {"name": name, "owned": owned is not None,
               "level": None, "form": None, "verdict": "missing", "reason": "刀帐里没有这把"}
    if owned is None:
        return verdict
    verdict.update({"level": owned["level"], "form": owned["form"]})
    recommended = event["recommended_level"]
    if event["damage"] == "virtual":
        verdict["verdict"] = "free"
        verdict["reason"] = "虚拟伤害活动，多少级都能带，白嫖加成不受伤"
        return verdict
    if recommended is None:
        verdict["verdict"] = "unknown"
        verdict["reason"] = "没填建议等级，自己掂量；真伤活动别硬送"
        return verdict
    if owned["level"] >= recommended:
        verdict["verdict"] = "carry"
        verdict["reason"] = f"{owned['level']} 级 ≥ 建议 {recommended} 级，带得动"
    elif owned["level"] >= recommended - _BONUS_MARGINAL_GAP:
        verdict["verdict"] = "marginal"
        verdict["reason"] = (f"{owned['level']} 级，差建议等级 "
                             f"{recommended - owned['level']} 级，能带但放后排吃经验")
    else:
        verdict["verdict"] = "burden"
        verdict["reason"] = (f"{owned['level']} 级，差建议等级太多；"
                             "真伤活动带它是白带，还会拖累队伍")
    return verdict


# ── 编队建议 ──


def _suggest_formation(event: dict, roster: list[dict], rules: dict) -> dict:
    templates = rules["formation_templates"]
    battle = event["battle"]
    if battle == "day":
        template = templates["day"]
    elif battle == "mixed":
        template = templates["mixed"]
    else:  # night / indoor
        template = templates["night_indoor"]

    wanted = template["types"]
    # 无伤或轻伤才进编队候选；中伤以上直接不建议上阵
    candidates = [r for r in roster
                  if r["type"] in wanted and INJURY_ORDER.get(r["injury"], 0) <= 1]
    candidates.sort(key=lambda r: (-r["level"], r["name"]))

    kiwame_tanto = [r for r in roster if r["type"] == "短刀" and r["form"] == "kiwame"]
    line = {
        "template_note": template["note"],
        "wanted_types": wanted,
        "candidates": candidates[:6],
        "train_note": None,
    }
    if kiwame_tanto and rules["formation_templates"]["train"]["kiwame_tanto_universal"]:
        line["train_note"] = rules["formation_templates"]["train"]["note"]
    return line


# ── 刀装建议 ──


def _suggest_troops(roster: list[dict], rules: dict, *,
                    enemy_fast_spear: bool = False) -> list[dict]:
    table = rules["troops"]
    advice = []
    for sword in roster:
        tree = table.get(sword["type"])
        if not tree:
            continue
        need_flower = (sword["fatigue"] is not None and sword["fatigue"] < 50)
        for rule in tree:
            cond = rule["condition"]
            hit = (
                cond == "always"
                or (cond == "need_flower" and need_flower)
                or (cond == "enemy_fast_spear" and enemy_fast_spear)
            )
            if hit:
                advice.append({"name": sword["name"], "type": sword["type"],
                               "troop": rule["troop"], "why": rule["why"]})
                break
    return advice


# ── 控花建议 ──


def _suggest_flower(event: dict, rules: dict) -> list[str]:
    ctrl = rules["flower_control"]
    fatigue = rules["fatigue"]
    tips = []
    points = event["map_points"]
    if points is None:
        tips.append("地图点数没填：点数 ≥5 的图才有控花操作空间，<5 的图直接摆烂薙刀一带五。")
        return tips
    if points < ctrl["min_map_points"]:
        tips.append(f"这图只有 {points} 个点，控不了花——薙刀一带五摆烂，或者别管花了。")
        return tips
    for rule in ctrl["rules"]:
        if rule["when"] != "地图点数<5":
            tips.append(f"{rule['when']}：{rule['do']}")
    tips.append(ctrl["archer_rule"])
    reflower = fatigue["reflower"]
    tips.append(f"花掉了去 {reflower['solo_map']} 刷：A 胜 +{reflower['a_win_gain']} / "
                f"S 胜 +{reflower['s_win_gain']}；活动期 {'/'.join(reflower['event_spots'])} 可整队刷。")
    return tips


# ── 状态警告 ──


def _warnings(roster: list[dict]) -> list[str]:
    warnings = []
    for sword in roster:
        if INJURY_ORDER.get(sword["injury"], 0) >= 2:
            warnings.append(f"{sword['name']} {sword['injury']}：先进手入，别进编队。")
        elif sword["fatigue"] is not None and sword["fatigue"] < 20:
            warnings.append(f"{sword['name']} 黄脸/红脸（疲劳 {int(sword['fatigue'])}）："
                            "先刷花或歇着，别硬拉上阵。")
    return warnings


# ── 主入口 ──


def build_briefing(event: dict, roster: list[dict], *,
                   rules: dict | None = None,
                   enemy_fast_spear: bool = False) -> dict:
    """生成开幕简报。event 见 _validate_event；roster 见 _normalize_roster。"""
    rules = rules or load_rules()
    event = _validate_event(event)
    roster = _normalize_roster(roster)

    by_name = {}
    for sword in roster:
        by_name.setdefault(sword["name"], sword)

    bonus = [_judge_bonus_sword(name, by_name.get(name), event)
             for name in event["bonus_swords"]]
    formation = _suggest_formation(event, roster, rules)
    troops = _suggest_troops(formation["candidates"] or roster, rules,
                             enemy_fast_spear=enemy_fast_spear)
    flower = _suggest_flower(event, rules)
    warnings = _warnings(roster)

    carry = sum(1 for b in bonus if b["verdict"] in ("carry", "free"))
    missing = sum(1 for b in bonus if b["verdict"] == "missing")
    if not event["bonus_swords"]:
        head = f"「{event['name']}」本期没填加成刀，编队按战场类型走。"
    elif missing == len(bonus):
        head = f"「{event['name']}」的加成刀你一把都没有，安心灵活编队。"
    else:
        head = f"「{event['name']}」加成刀 {carry}/{len(bonus)} 把能上场，详细看下面。"

    return {
        "event": event,
        "summary": head,
        "bonus": bonus,
        "formation": formation,
        "troops": troops,
        "flower": flower,
        "warnings": warnings,
    }
