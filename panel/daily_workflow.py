"""默认日课模板：保留日课专属编排，复用现有执行方法，不另写玩法。"""
import copy

RECIPE_KEYS = ("recipe_charcoal", "recipe_steel", "recipe_coolant", "recipe_whetstone")

DAILY_SORTIE_KEYS = {
    "sortie_mode", "team_no", "raid_rounds", "raid_auto_refill",
    "pumpkin_difficulty", "pumpkin_runs", "yosari_map_no", "yosari_runs",
    "yosari_auto_refill", "osaka_runs", "osaka_select_floor",
    "osaka_target_floor", "chapter", "map_no", "loops",
    "retreat_before_boss", "auto_march", "formation_mode", "formation",
    "repair_threshold", "repair_on_injury", "auto_equip", "rotate_captain",
    "rotate_captain_margin",
}


def recipe_fields():
    """锻刀配方四个数字字段（配置页锻刀脚本和一键日课共用）"""
    names = zip(RECIPE_KEYS, ("木炭", "玉钢", "冷却材", "砥石"))
    return [{"key": key, "type": "number", "label": f"配方·{zh}",
             "default": 700, "min": 10, "max": 999,
             **({"help": "点火前自动把配比设成这四个数。游戏会记住上次配方，"
                         "一致时跳过不重设。"} if i == 0 else {})}
            for i, (key, zh) in enumerate(names)]


def recipe_from_params(params):
    """从面板参数读配方；缺键/越界（10~999）返回 None（= 用配置文件里的配方）"""
    out = []
    for key in RECIPE_KEYS:
        try:
            v = int(params.get(key))
        except (TypeError, ValueError):
            return None
        if not 10 <= v <= 999:
            return None
        out.append(v)
    return out


def make_template(settings, config, daily_steps):
    daily = (settings.get("params", {}).get("daily") or {})
    wanted = daily.get("steps") or daily.get("only") or daily_steps
    mapping = {"登录": "login", "签到": "signin", "万屋": "free_gift",
               "演练": "practice", "远征": "expedition", "内番": "naihanka",
               "锻刀": "forge", "刀解": "dismantle", "合成": "synthesize",
               "出阵": "daily_sortie", "任务奖励": "task_rewards", "库存快照": "snapshot"}
    nodes = []
    for step in daily_steps:
        if step not in wanted:
            continue
        params = {}
        if step == "锻刀":
            params = {
                "times": daily.get(
                    "forge_times", config.get("daily", {}).get("forge_times", 3)),
            }
            recipe = config.get("forge", {}).get("recipe") or []
            for index, key in enumerate(RECIPE_KEYS):
                if key in daily:
                    params[key] = copy.deepcopy(daily[key])
                elif len(recipe) == 4:
                    params[key] = recipe[index]
        elif step == "演练":
            params = copy.deepcopy(daily.get("practice") or {})
        elif step == "远征":
            params = copy.deepcopy(daily.get("expedition") or {})
        elif step == "出阵":
            params = {k: copy.deepcopy(v) for k, v in daily.items()
                      if k in DAILY_SORTIE_KEYS}
            params.setdefault("sortie_mode", "none")
        nodes.append({"type": mapping[step], "params": params,
                      "on_error": "stop" if step == "登录" else "continue"})
    # 账本同步永远殿后：把国服客户端的 HttpRequestCollect 流量日志
    # 拉下来解析成真账房流水（只读、阅后即焚）。daily.ledger_sync=false 可关。
    if daily.get("ledger_sync", True):
        nodes.append({"type": "ledger_sync", "params": {},
                      "on_error": "continue"})
    return {"id": "builtin-daily", "name": "一键日课", "nodes": nodes,
            "after": daily.get("after") or "none", "daily_mode": True}


def install_daily_template(workflow, scripts, *, _load_settings, config, daily_steps, plan_inputs):
    workflow.daily_template_provider = lambda: make_template(_load_settings(), config, daily_steps)

    def daily_login(agent, params, config_path):
        if not (yield from agent._ensure_game_started()):
            yield "[日课] ✗ 游戏没有启动，日课停止"
            return
        agent.login()
        if not agent._popup_sweep():
            yield "[日课] ✗ 登录后没到本丸，日课停止"
        else:
            yield "[日课] ✓ 已登录本丸"

    def daily_practice(agent, params, config_path):
        # 复用普通演练积木的部队解析：普通队号与 preset:<id> 都走同一条
        # 开工前检查/套队逻辑，避免一键日课另造半套实现。
        yield from workflow.NODE_REGISTRY["practice"]["run"](
            agent, params, config_path)

    def daily_expedition(agent, params, config_path):
        routes = plan_inputs({"expedition": params})[4]
        yield from agent._daily_expedition_step(routes)

    def daily_snapshot(agent, params, config_path):
        yield from agent._closing_snapshot_stream(getattr(agent, "_workflow_forge_ran", False))

    def daily_dismantle(agent, params, config_path):
        yield from agent._dismantle_step()

    def daily_forge(agent, params, config_path):
        # 十连限锻（烧加速符）和盯时长都走锻刀积木自己的 run，参数那边全认
        if params.get("watch") or str(params.get("forge_limited")) == "true":
            yield from workflow.NODE_REGISTRY["forge"]["run"](agent, params, config_path)
        else:
            yield from agent.forge_stream(times=int(params.get("times", 3)),
                                          recipe=recipe_from_params(params))

    def daily_sortie(agent, params, config_path):
        plan = plan_inputs(params)[2]
        if plan.get("mode") == "none":
            yield "[日课] ⏭ 按安排不出阵"
            return
        report = []
        yield from agent._sortie_step({"sortie": plan}, report)
        for name, status in report:
            yield f"[日课] {name}: {status}"

    def sortie_status(message, previous):
        return "⏭ 按安排不出阵" if message == "[日课] ⏭ 按安排不出阵" else previous

    def daily_ledger_sync(agent, params, config_path):
        """日课收尾记账：拉国服流量日志 → 解析 → 入库账房 → 焚毁原档。

        失败只许播报不许炸——账本丢了不影响日课本体（telemetry 铁律同款）。
        """
        try:
            from touken.record_sync import collect_game_records
            from touken.runtime_paths import STATUS_DIR
            from .expedition_observation import FILENAME, save_observations
            result = collect_game_records(
                agent.maa.adb_path, agent.maa.adb_address,
                extra_consumers=(
                    lambda events: save_observations(events, STATUS_DIR / FILENAME),))
            yield (f"[日课] ✓ 账本已同步：观察 {result['observations_written']} 条，"
                   f"收支 {result['changes_written']} 条")
        except Exception as exc:
            yield f"[日课] ⚠ 账本同步失败（不影响日课本体）：{exc}"

    workflow.register_node({
        "type": "ledger_sync", "label": "账本同步",
        "desc": "拉取日志，去敏记账，原始日志阅后即焚。",
        "category": "finish", "params": [], "run": daily_ledger_sync,
        "template_only": True})

    for name, callback in (("login", daily_login), ("practice", daily_practice),
                           ("expedition", daily_expedition), ("snapshot", daily_snapshot),
                           ("dismantle", daily_dismantle), ("forge", daily_forge)):
        workflow.NODE_REGISTRY[name]["daily_run"] = callback
    # 「日课出阵」只收出阵字段。锻刀次数/配方属于前面的「锻刀」积木，
    # 复制整张日课表单会让无效字段混进出阵配置，既误导又无法执行。
    fields = [copy.deepcopy(f) for f in scripts["daily"]["params"]
              if f["key"] in DAILY_SORTIE_KEYS]
    fields.append({"key": "pumpkin_watch", "type": "text", "label": "南瓜目标刀剑",
                   "swords": True, "default": "", "placeholder": "多个名字用逗号分隔",
                   "visibleWhen": {"key": "sortie_mode", "is": "pumpkin"}})
    workflow.register_node({"type": "daily_sortie", "label": "日课出阵",
        "desc": "按日课的地图和次数出阵；行军、阵形和伤势处理由本节点自己的设置决定，与「配置」页无关。也可以选择不出阵。",
        "category": "battle", "params": fields, "run": daily_sortie,
        "detail": [*workflow.NODE_REGISTRY["sortie"].get("detail", []), sortie_status], "template_only": True})
