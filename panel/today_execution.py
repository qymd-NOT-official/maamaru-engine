"""Adopt the current suggestions once, using the existing two schedulers."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import day_conductor as dc, day_plan as dp, day_timeline as dt
from . import expedition_choices as ec, scheduler


def suggestion_records(timeline: dict) -> dict:
    records = {}
    for item in timeline.get("expedition_suggestions") or []:
        start = int(item["start_min"])
        when = datetime.fromtimestamp(timeline["day_start"] + start * 60,
                                      timezone(timedelta(hours=8)))
        key = ec.adhoc_key(when.date().isoformat(), item["team_no"], start)
        records[key] = {key: item[key] for key in
                        ("team_no", "map_code", "start_min", "duration_min")}
        records[key].update(planned_at=when.isoformat(),
                            sakura_before_dispatch=bool(item.get("sakura_before_dispatch", False)),
                            repair_threshold=item.get("repair_threshold", "light"))
        if item.get("formation_id"):
            records[key].update({key: item[key] for key in
                                ("formation_id", "formation_name", "formation_signature")})
    return records


def compose_plan(timeline: dict, old_plan: dict | None, raid_settings: dict, old_state: dict | None = None) -> dict:
    """Keep saved arrangements; otherwise calculate raid windows with the adopted expeditions."""
    day_start = timeline["day_start"]
    activity = timeline.get("activity") or {}
    now_min = max(0, math.ceil((timeline["now"] - day_start) / 60))
    if now_min >= dt.DAY_MINUTES:
        raise ValueError("今天已经收摊了，刷新后再安排")
    daily_done = any(run.get("script") == "daily" and run.get("status") == "completed"
                     and float(run.get("started_at") or 0) >= day_start
                     for run in timeline.get("runs") or [])
    same_day = old_plan and old_plan.get("day_start") == day_start
    blocks = [dict(b) for b in old_plan["blocks"]] if same_day else []
    completed = {(b["kind"], b["start_min"]) for b in (old_state or {}).get("blocks", [])
                 if b.get("status") == "ended"}
    blocks = [b for b in blocks if (b["kind"], b["start_min"]) not in completed]
    if daily_done:
        blocks = [b for b in blocks if b["kind"] != "daily"]
    uses_saved_raid = any(b["kind"] == "raid" for b in blocks)
    if not uses_saved_raid and activity.get("name") == "联队战":
        spec = dc.workflow_spec(dc.BUILTIN_ID, raid_settings)
        new_expeditions = [{**item, "will_run": True, "state": "pending",
                            "time_min": item["start_min"]}
                           for item in timeline.get("expedition_suggestions") or []]
        occupied = [*[window for window in activity.get("occupied", [])
                       if window.get("label") != "一键日课"],
                    *dt._occupied_segments(new_expeditions, scheduler.load_config(), spec["team_no"])]
        if int(activity.get("seconds_per_loop") or 0) <= 0:
            raise ValueError("还没有可靠的本期圈速，先同步联队战进度再安排")
        windows, _ = dt.suggest_round_windows(now_min, occupied,
                                              int(activity.get("target_runs") or 0),
                                              int(activity.get("seconds_per_loop") or 0))
        blocks.extend({"start_min": b["start_min"], "kind": "raid", "runs": b["runs"]}
                      for b in windows)
        blocks.sort(key=lambda b: b["start_min"])
    raid_blocks = [b for b in blocks if b["kind"] == "raid"]
    if raid_blocks and not daily_done:
        for block in blocks:
            previous = next((b for b in (old_state or {}).get("blocks", [])
                             if b["kind"] == "daily" and b["start_min"] == block["start_min"]), None)
            if block["kind"] == "daily" and (not previous or previous["status"] == "pending"):
                block["start_min"] = max(now_min, max(b["start_min"] + math.ceil(
                    b["runs"] * int(activity.get("seconds_per_loop") or 0) / 60) for b in raid_blocks))
                block["after_raids"] = True
        blocks.sort(key=lambda b: b["start_min"])
    if not daily_done and not any(b["kind"] == "daily" for b in blocks):
        pace = int(activity.get("seconds_per_loop") or 0)
        end = max([now_min, *[b["start_min"] + (
            math.ceil(b["runs"] * pace / 60) if b["kind"] == "raid"
            else dp.GENERIC_BLOCK_MINUTES) for b in blocks]])
        if end >= dt.DAY_MINUTES:
            raise ValueError("今天已经排满了，日课暂时接不进去")
        blocks.append({"start_min": end, "kind": "daily",
                       **({"after_raids": True} if any(b["kind"] == "raid" for b in blocks) else {})})
    return {"version": 2, "day_start": day_start,
            "event_end_at": (old_plan.get("event_end_at") if same_day and uses_saved_raid else activity.get("event_end_at")),
            "blocks": blocks, "daily_done": daily_done, "uses_saved_raid": uses_saved_raid}


def compose_daily_plan(timeline: dict, preset: dict) -> dict:
    """统一今日安排只执行规划页的日课，不再额外生成一份活动流程。"""
    now_min = max(0, math.ceil((timeline['now'] - timeline['day_start']) / 60))
    start = now_min
    clock = (preset.get('daily_ui') or {}).get('startTime') or ''
    if clock:
        try:
            hour, minute = map(int, clock.split(':'))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError('开始时间格式不正确') from None
        start = hour * 60 + minute
        if start < now_min:
            start += 1440
    if start >= dt.DAY_MINUTES:
        raise ValueError('这个开始时间超过本次日课刷新，请选择更早的时间')
    return {'version': 2, 'day_start': timeline['day_start'], 'event_end_at': None,
            'blocks': [{'kind': 'workflow', 'workflow_id': 'builtin-daily', 'start_min': start}],
            'daily_done': False, 'uses_saved_raid': False}


def execute_today(runner, timeline_fn, raid_settings_fn, *,
                  state_path: Path = dc.STATE_PATH, plan_path: Path = dp.PLAN_PATH,
                  choices_path: Path = ec.CHOICES_PATH, daily_preset: dict | None = None) -> dict:
    with dc._LOCK, ec._WRITE_LOCK:
        timeline = timeline_fn()
        old = dc.load_state(state_path)
        same_day = old and old.get("day_start") == timeline["day_start"]
        if same_day and any(b["status"] in {"interrupted", "blocked"} for b in old["blocks"]):
            raise ValueError("还有中断的安排，请先到时间表确认后继续")
        if same_day and old.get("today_execution") and (daily_preset is None or old.get("unified_today")) and (
                old.get("enabled") or all(b["status"] == "ended" for b in old["blocks"])):
            return {"ok": True, "message": "今日安排已经完成了。" if all(b["status"] == "ended" for b in old["blocks"]) else "今日安排已经接班了。"}
        if runner.is_running:
            raise ValueError("还有任务正在执行，等它收工后再接今日安排")
        if same_day and any(b["status"] == "running" for b in old["blocks"]):
            raise ValueError("上次的任务还在核对，请稍后刷新时间表")
        settings = raid_settings_fn()
        plan = (compose_daily_plan(timeline, daily_preset) if daily_preset is not None else
                compose_plan(timeline, dp.load_plan(plan_path), settings, old if same_day else None))
        records = {} if same_day and old.get("today_execution") else suggestion_records(timeline)
        if daily_preset is not None and (daily_preset.get('daily_ui') or {}).get('plannedExpeditions') is False:
            records = {}
        cfg = scheduler.load_config()
        paused = cfg.get("automation", {}).get("paused_until", "")
        if records and paused and paused > datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
            raise ValueError("远征排班正在暂停，先恢复远征再接今日安排")
        workflow_id = old.get("workflow_id", dc.BUILTIN_ID) if same_day and plan["uses_saved_raid"] else dc.BUILTIN_ID
        # Validate every gameplay step before enabling any new expedition.
        validation_timeline = {**timeline, "expeditions": [*timeline.get("expeditions", []),
            *[{**record, "time_min": record["start_min"], "will_run": True, "state": "pending"}
              for record in records.values()]]}
        candidate = dc.arm(plan, validation_timeline, workflow_id, settings, state_path, persist=False) if plan["blocks"] else {
            "version": 2, "day_start": plan["day_start"], "blocks": [], "enabled": False,
            "plan_signature": dc._plan_signature(plan)}
        if plan["daily_done"]:
            for block in candidate["blocks"]:
                if block["kind"] == "daily" and block["status"] == "pending":
                    block.update(status="ended", reason="今日已完成")
        candidate["today_execution"] = True
        if daily_preset is not None:
            cutoff = plan['blocks'][0]['start_min']
            existing_skipped, existing_forced = ec.load_choice_sets(choices_path)
            keys = {key for key, item in {**existing_forced, **records}.items()
                    if key not in existing_skipped
                    and cfg.get('automation', {}).get('slot_states', {}).get(key, {}).get('state') not in scheduler.TERMINAL_STATES
                    and item.get('start_min', dt.DAY_MINUTES) <= cutoff
                    and str(item.get('planned_at', '')).startswith(datetime.fromtimestamp(
                        timeline['day_start'] + cutoff * 60, timezone(timedelta(hours=8))).date().isoformat())}
            keys.update(item['key'] for item in timeline.get('expeditions', [])
                        if item.get('will_run') and item.get('kind') != 'running' and item.get('time_min', dt.DAY_MINUTES) <= cutoff
                        and item.get('state') not in scheduler.TERMINAL_STATES)
            candidate['initial_expedition_keys'] = sorted(keys)
            candidate['unified_today'] = True
            if keys and paused and paused > datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
                raise ValueError("远征排班正在暂停，先恢复远征再接今日安排")
        skipped, forced = ec.load_choice_sets(choices_path)
        for key, record in records.items():
            if key in forced and forced[key] != record:
                raise ValueError("这班远征已有安排，请刷新后再试")
        snapshots = {p: p.read_bytes() if p.exists() else None
                     for p in (plan_path, state_path, choices_path)}
        try:
            dp.save_plan(plan["day_start"], plan["event_end_at"], plan["blocks"], plan_path)
            dc._save(candidate, state_path)
            if records:
                ec._write_sets(skipped, {**forced, **records}, choices_path)
        except OSError:
            # Keep the original schedules, including absent-file semantics; never delete user data.
            for path, content in snapshots.items():
                temporary = path.with_suffix(path.suffix + ".rollback")
                temporary.write_bytes(content if content is not None else b"null\n")
                temporary.replace(path)
            raise ValueError("今日安排暂时没保存成功，原来的安排已保留，请重试") from None
        raid_runs = sum(b.get("runs", 0) for b in candidate["blocks"]
                        if b["kind"] == "raid" and b["status"] == "pending")
        daily = any(b["kind"] == "daily" and b["status"] == "pending" for b in candidate["blocks"])
        pieces = []
        if daily_preset is not None:
            return {'ok': True, 'message': ('今日安排已接班：先处理到点远征，再执行一键日课，最后按设置收工。'
                    if not (daily_preset.get('daily_ui') or {}).get('startTime') else
                    '今日安排已接班，将按设置的开始时间执行。')}
        if records:
            pieces.append(f"远征 {len(records)} 班")
        if raid_runs:
            pieces.append(f"联队战 {raid_runs} 圈")
        if daily:
            pieces.append("联队战收工后做日课" if raid_runs else "一键日课")
        return {"ok": True, "message": "已接下今日安排：" + "，".join(pieces) + "。" if pieces else "今天的活已经做完了。"}
