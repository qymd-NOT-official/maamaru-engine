"""任务专用参数：只在迁移时读取单独玩法，执行时使用快照。"""
import copy


def field_defaults(fields):
    return {f["key"]: copy.deepcopy(f.get("default", ""))
            for f in fields if f.get("type") != "note"}


def expedition_fields(map_field):
    fields = []
    for team in range(2, 6):
        fields.extend([
            {"key": f"enabled_{team}", "type": "toggle", "label": f"派遣部队{team}", "default": False},
            {**copy.deepcopy(map_field), "key": f"map_{team}", "label": f"部队{team}远征图",
             "visibleWhen": {"key": f"enabled_{team}", "is": True}},
            {"key": f"preset_{team}", "type": "select", "label": f"部队{team}编队", "default": "",
             "options": [["", "保持当前部队"]],
             "visibleWhen": {"key": f"enabled_{team}", "is": True}},
        ])
    fields.extend([
        {"key": "sakura_before_dispatch", "type": "toggle", "label": "派遣前刷花", "default": False},
        {"key": "repair_threshold", "type": "select", "label": "刷花伤势停止条件",
         "options": [["light", "轻伤"], ["medium", "中伤"], ["heavy", "重伤"]], "default": "light"},
    ])
    return fields


def expedition_snapshot(schedule, sakura):
    values = {"sakura_before_dispatch": schedule.get("automation", {}).get("sakura_before_dispatch", False),
              "repair_threshold": sakura.get("repair_threshold", "light")}
    for row in schedule.get("common_plan", []):
        team = row["team_no"]
        values.update({f"enabled_{team}": row.get("enabled", False),
                       f"map_{team}": row.get("map_code", ""),
                       f"preset_{team}": row.get("formation_id", "")})
    return values


def expedition_routes(values, find_map, owned):
    routes = []
    for team in range(2, 6):
        enabled = values.get(f"enabled_{team}", False)
        if enabled not in (True, "true") or team in owned:
            continue
        code = values.get(f"map_{team}")
        found = find_map(code)
        if not found:
            raise ValueError(f"部队{team}的远征图无效，停止派遣")
        route = {"enabled": True, "team_no": team, "map_code": code, "era": found["era"],
                 "map_slot": found["slot"], "map_name": found["name"],
                 "sakura_before_dispatch": values.get("sakura_before_dispatch", False) in (True, "true"),
                 "repair_threshold": values.get("repair_threshold", "light")}
        if values.get(f"preset_{team}"):
            route["formation_id"] = str(values[f"preset_{team}"])
        routes.append(route)
    return routes


def isolate_presets(presets, registry, saved, practice_fallback, expedition):
    result = copy.deepcopy(presets)
    for preset in result:
        if preset.get("parameter_version") == 1:
            continue
        for node in preset.get("nodes", []):
            kind = node["type"]
            definition = registry.get(kind, {})
            inherited = {}
            if definition.get("snapshot_saved"):
                inherited = saved.get(kind, {}) or (practice_fallback if kind == "practice" and preset.get("daily_mode") else {})
            if kind == "expedition":
                inherited = expedition
            node["params"] = {**field_defaults(definition.get("params", [])),
                              **copy.deepcopy(inherited), **node.get("params", {})}
        preset["parameter_version"] = 1
    return result
