"""仪表盘「今天的时间表」数据组装（纯只读）。

把远征排班投影和 telemetry 的运行记录压到同一条 24 小时横轴上，
全部依赖可注入（cfg / store / script_labels / active），方便单测。
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone

from . import scheduler
from .expedition_choices import is_adhoc_record, load_choice_sets
from . import expedition_advisor

DAY_MINUTES = 28 * 60  # 白天及次日凌晨，04:00 换日
SHANGHAI_TZ = timezone(timedelta(hours=8))

# 建议层的避让口径
DAILY_RESET_WINDOW = (27 * 60 + 50, 28 * 60 + 10)  # 03:50–04:10 领旧日课+重登
ACTION_WINDOW_BEFORE_MIN = 2   # 班次计划时刻前 2 分钟起占画面（派遣/收菜动作）
ACTION_WINDOW_AFTER_MIN = 5
MIN_SUGGESTION_MIN = 30        # 一圈约 7 分钟，不足 30 分钟的碎片不建议
MAX_SUGGESTION_BLOCKS = 2      # 一块装不下才切第二块

_RUN_TONES = {
    "waiting": "waiting",
    "completed": "ok",
    "failed": "failed",
    "stopped": "stopped",
    "watchdog": "stopped",
    "running": "running",
}


def _day_window(now: float) -> tuple[float, float]:
    local = datetime.fromtimestamp(now)
    anchor = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if local.hour < 4:
        anchor -= timedelta(days=1)
    day_start = anchor.timestamp()
    return day_start, day_start + DAY_MINUTES * 60


# 建议引擎的家底报告（resource_watch / koban_watch）：30 秒轮询的时间表
# 不值得每次都重算一遍账本，成功结果缓存两分钟；失败不缓存，下次再试。
_PLANNING_TTL_SEC = 120.0
_planning_cache: dict[float, dict] = {}


def _load_planning_snapshot(store):
    if store is None:
        return None
    now = time.time()
    for cached_at, data in list(_planning_cache.items()):
        if now - cached_at < _PLANNING_TTL_SEC and data is not None:
            return data
    try:
        from touken import advisor
        from touken.runtime_paths import CONFIG_PATH, STATE_DIR
        recipe = None
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            recipe = (config.get("forge") or {}).get("recipe")
        except Exception:
            pass
        data = advisor.get_planning(store, STATE_DIR / advisor.GOALS_FILENAME,
                                    forge_recipe=recipe)
    except Exception:
        return None
    _planning_cache.clear()
    _planning_cache[now] = data if isinstance(data, dict) else None
    return data


def _minute_of(time_text: str) -> int:
    try:
        hh, mm = str(time_text).split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, TypeError):
        return 0


def _planned_ts_of(record) -> float | None:
    """forced 记录的 planned_at 兼容 ISO 字符串和时间戳。"""
    if not isinstance(record, dict):
        return None
    raw = record.get("planned_at")
    try:
        if isinstance(raw, str):
            return datetime.fromisoformat(raw).timestamp()
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _forced_expedition_items(cfg: dict, now: float, day_start: float,
                             forced: dict, durations: dict) -> list[dict]:
    """forced 班（preset/custom 抬上来的 + 建议引擎采纳的自描述班）。

    v2 起时间轴不再渲染 preset 投影：没点名的班不上轴。
    """
    auto = cfg.get("automation", {}) if isinstance(cfg, dict) else {}
    slots = auto.get("slot_states", {})
    last_runs = auto.get("last_runs", {})
    items = []
    for key, record in (forced or {}).items():
        if not isinstance(record, dict):
            continue
        try:
            team_no = int(record.get("team_no"))
        except (TypeError, ValueError):
            continue
        map_code = str(record.get("map_code") or "")
        planned_ts = _planned_ts_of(record)
        if planned_ts is None or map_code == "":
            continue
        if not day_start <= planned_ts < day_start + DAY_MINUTES * 60:
            continue
        slot = slots.get(key)
        if slot:
            state = slot.get("state", scheduler.SLOT_WAITING_UNKNOWN)
            blocked_reason = slot.get("blocked_reason") or ""
            ended = slot.get("dispatched_at")
            try:
                done_ts = time.mktime(time.strptime(ended, "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                done_ts = now
            late_min = max(0, int((done_ts - planned_ts) / 60))
        elif last_runs.get(key):
            state, blocked_reason = scheduler.SLOT_DISPATCHED, ""
            late_min = max(0, int((now - planned_ts) / 60))
        else:
            state = "pending" if planned_ts > now else "missed"
            blocked_reason = ""
            late_min = max(0, int((now - planned_ts) / 60))
        # 自描述班时长以记录为准；排班引用班查收益表
        duration = (int(record["duration_min"])
                    if is_adhoc_record(record)
                    else durations.get(map_code, 0))
        will_run = state not in (scheduler.SLOT_EXPIRED, scheduler.SLOT_FAILED)
        items.append({
            "key": key, "kind": "forced", "planned_at": planned_ts,
            **{field: record[field] for field in ("formation_id", "formation_name") if record.get(field)},
            "time_min": int((planned_ts - day_start) // 60),
            "duration_min": duration,
            "team_no": team_no,
            "map_code": map_code,
            "state": state,
            "blocked_reason": blocked_reason,
            "late_min": late_min,
            "enabled": will_run,
            "base_enabled": False,
            "entry_enabled": True,
            "skipped_today": False,
            "forced_today": True,
            "will_run": will_run,
            # 还没进状态机终态、又没临近开班的班可以点掉（取消 forced）
            "toggleable": (state not in scheduler.TERMINAL_STATES
                           and planned_ts > now + 60),
        })
    return items


def _running_expedition_items(records: dict, now: float, day_start: float,
                              durations: dict) -> list[dict]:
    """远征中/待收：来自 expeditions.json 派遣记录（收菜后销账）。"""
    items = []
    day_end = day_start + DAY_MINUTES * 60
    for team_str, record in (records or {}).items():
        if not isinstance(record, dict):
            continue
        try:
            team_no = int(team_str)
            started = time.mktime(time.strptime(
                str(record["dispatched_at"]), "%Y-%m-%d %H:%M:%S"))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        map_code = str(record.get("map_code") or "")
        duration = int(record.get("duration_min")
                       or durations.get(map_code, 0) or 0)
        end_ts = started + duration * 60
        if started >= day_end or end_ts < day_start - 6 * 3600:
            continue  # 明天的班/太久远的残账都不上今天的轴
        time_min = int((started - day_start) // 60)
        if time_min < 0:  # 跨午夜：截到今天 0 点起画
            duration += time_min
            time_min = 0
        state = "running" if now < end_ts else "awaiting_collect"
        items.append({
            "key": f"running:{team_no}:{map_code}",
            "kind": "running", "planned_at": started,
            "time_min": time_min,
            "duration_min": max(0, duration),
            "team_no": team_no,
            "map_code": map_code,
            "state": state,
            "blocked_reason": "",
            "late_min": 0,
            "enabled": True,
            "base_enabled": True,
            "entry_enabled": True,
            "skipped_today": False,
            "forced_today": False,
            "will_run": True,
            "toggleable": False,
        })
    return items


def _expedition_items(cfg: dict, now: float, day_start: float,
                      forced: dict | None = None,
                      records: dict | None = None) -> list[dict]:
    """v2 泳道内容 = 已 forced 的班 + 远征中/待收；preset 死班表不上轴。"""
    durations = {}
    try:
        durations = {m["code"]: int(m.get("duration_min", 0))
                     for m in scheduler.map_options()}
    except Exception:
        pass
    running = _running_expedition_items(records, now, day_start, durations)
    forced_items = _forced_expedition_items(cfg, now, day_start,
                                            forced or {}, durations)
    # 已确认派出且队伍在外的 forced 班由「远征中」块代言，不画两遍
    running_teams = {item["team_no"] for item in running}
    forced_items = [item for item in forced_items
                    if not (item["team_no"] in running_teams
                            and item["state"] == scheduler.SLOT_DISPATCHED)]
    items = running + forced_items
    items.sort(key=lambda x: (x["time_min"], x.get("team_no") or 0))
    return items


def _expedition_counts(cfg, forced, records, store, day_start, now):
    """当天出发事实 + 尚未执行的安排；收菜销掉倒计时也不销掉次数。"""
    begin, end = day_start + 4 * 3600, day_start + DAY_MINUTES * 60
    facts = []

    def remember(team, code, stamp):
        try:
            team = int(team)
            at = (datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
                  if isinstance(stamp, str) else float(stamp))
        except (TypeError, ValueError, OverflowError):
            return
        if team not in range(1, 6) or not begin <= at < end or at > now:
            return
        if not any(t == team and c == code and abs(s - at) < 5 for t, c, s in facts):
            facts.append((team, code, at))

    auto = cfg.get("automation", {})
    for key, stamp in auto.get("last_runs", {}).items():
        source = forced.get(key) or auto.get("slot_states", {}).get(key) or {}
        remember(source.get("team_no"), source.get("map_code"), stamp)
    for source in auto.get("slot_states", {}).values():
        if isinstance(source, dict) and source.get("state") == scheduler.SLOT_DISPATCHED:
            remember(source.get("team_no"), source.get("map_code"), source.get("dispatched_at"))
    for team, record in (records or {}).items():
        if isinstance(record, dict):
            remember(team, record.get("map_code"), record.get("dispatched_at"))
    if store:
        try:
            cursor = None
            while True:
                rows = store.recent_events(limit=1000, event_type="expedition.dispatched",
                                           from_ts=begin, to_ts=min(end, now + 0.001), before_id=cursor)
                for row in rows:
                    payload = row.get("payload") or {}
                    remember(payload.get("team_no"), payload.get("map_code"), row.get("ts"))
                if len(rows) < 1000:
                    break
                cursor = rows[-1]["id"]
        except Exception:
            pass
    counts = {}
    for team, _, _ in facts:
        counts[team] = counts.get(team, 0) + 1
    for item in _forced_expedition_items(cfg, now, day_start, forced, {}):
        if item["will_run"] and item["state"] not in scheduler.TERMINAL_STATES:
            team = item["team_no"]
            counts[team] = counts.get(team, 0) + 1
        elif item["state"] == scheduler.SLOT_DISPATCHED:
            # 兼容只有班次终态、没有出发时间的旧记录。
            source = auto.get("slot_states", {}).get(item["key"], {})
            if not source.get("dispatched_at") and not auto.get("last_runs", {}).get(item["key"]):
                team = item["team_no"]
                counts[team] = counts.get(team, 0) + 1
    return counts


def _run_items(store, active: dict | None, day_start: float, day_end: float,
               script_labels: dict | None) -> list[dict]:
    labels = script_labels or {}
    try:
        rows = store.runs_between(day_start, day_end) if store else []
    except Exception:
        rows = []
    active_script = (active or {}).get("script")
    active_started = float((active or {}).get("started") or 0)
    items = []
    for row in rows:
        script = row.get("script") or ""
        started_at = float(row.get("started_at") or 0)
        ended_at = row.get("ended_at")
        ended_at = float(ended_at) if ended_at else None
        status = row.get("status") or ""
        tone = _RUN_TONES.get(status, "stopped")
        is_active = bool(
            active_script and script == active_script
            and abs(started_at - active_started) < 2)
        # runs_between 会把所有 ended_at=NULL 的旧记录当作跨日记录返回。
        # 它们只是往日面板异常退出留下的尸体，不属于“今天”，也不能全挤在 0 点。
        if started_at < day_start and ended_at is None and not is_active:
            continue
        if is_active:
            tone = "running"
            ended_at = None
        elif status == "running":
            # 脚本只活在面板进程里：runner 没在跑它，这条就是上次面板挂掉
            # 留下的「尸体」记录，别在轴上画成还在跑
            tone = "stopped"
            ended_at = None
        items.append({
            "script": script,
            # 工作流在开工时已经把当时的名字写进 runs.label；优先使用这份
            # 快照，既不退回笼统的“自定义工作流”，也不被日后改名篡改历史。
            "label": row.get("label") or labels.get(script) or script,
            "started_at": started_at,
            "ended_at": ended_at,
            "status": status,
            "tone": tone,
        })
    return items


def _hanafuda_active_plan(now: float, store) -> dict | None:
    """秘宝之里进行中且样本够算时返回 hanafuda_plan 的输出；否则 None。"""
    if store is None:
        return None
    try:
        from touken import advisor
        from touken.runtime_paths import STATE_DIR
        cards = advisor.load_event_cards(STATE_DIR)
        now_dt = datetime.fromtimestamp(now, SHANGHAI_TZ)
        for card in (cards or {}).values():
            if not isinstance(card, dict) or card.get("mechanics") != "hanafuda":
                continue
            plan = advisor.hanafuda_plan(store, card, now_dt=now_dt)
            if (plan.get("estimated_seconds") and plan.get("seconds_to_end")
                    and plan["seconds_to_end"] > 0 and plan.get("tama_remaining")):
                return plan
    except Exception:
        pass
    return None


def _raid_active_plan(now: float, store) -> dict | None:
    """只使用已确认的本期联队战卡与实测圈数／圈速。"""
    if store is None:
        return None
    try:
        from touken import advisor
        from touken.runtime_paths import STATE_DIR
        cards = advisor.load_event_cards(STATE_DIR)
        now_dt = datetime.fromtimestamp(now, SHANGHAI_TZ)
        for card in (cards or {}).values():
            if not isinstance(card, dict) or card.get("mechanics") != "raid":
                continue
            plan = advisor.currency_plan(store, card, now_dt=now_dt,
                                         mechanics="raid")
            if (plan.get("runs_needed") and plan.get("seconds_per_loop")
                    and plan.get("seconds_to_end") and plan["seconds_to_end"] > 0
                    and plan.get("tama_remaining")):
                game_day = now_dt.replace(hour=4, minute=0, second=0,
                                          microsecond=0)
                if now_dt < game_day:
                    game_day -= timedelta(days=1)
                period_events = advisor._currency_period_events(
                    store, card, mechanics="raid", limit=1001)
                observed_at = plan.get("tama_observed_at") or now
                plan["completed_today"] = sum(
                    1 for ts, _ in period_events
                    if game_day.timestamp() <= ts <= min(now, observed_at))
                plan["game_day_started_at"] = game_day.timestamp()
                plan["now"] = now
                return plan
    except Exception:
        pass
    return None


def _raid_daily_runs(plan: dict) -> int:
    """活动卡的日均圈数扣掉本丸换日后已完成的圈，不让目标边跑边重置。"""
    runs = int(plan["runs_needed"])
    completed = int(plan.get("completed_today") or 0)
    elapsed = max(0, plan.get("now", 0) - plan.get("game_day_started_at", 0))
    days_left = (plan["seconds_to_end"] + elapsed) / 86400
    original_target = min(runs + completed,
                          max(1, math.ceil((runs + completed) / days_left)))
    return min(runs, max(0, original_target - completed))


def _hanafuda_hint(plan: dict | None) -> str | None:
    if not plan:
        return None
    est = plan["estimated_seconds"]
    hours = est / 3600.0
    duration = (f"约 {hours:.1f} 小时" if hours >= 1
                else f"约 {int(est // 60)} 分钟")
    return f"秘宝之里：按现在的节奏，拿完剩下的玉还要{duration}。"


def _daily_quota_seconds(plan: dict, remaining_today: float) -> int | None:
    """今天该挂多少秒。

    口径和活动卡前端一致（EventTimeline.vue tamaTimeText）：
    estimated_seconds 按剩余天数（seconds_to_end / 86400）平摊，
    再被 estimated_seconds 本身、今天剩余时间、收摊时间三头卡住。
    """
    est = plan.get("estimated_seconds")
    seconds_to_end = plan.get("seconds_to_end")
    if not (est and seconds_to_end and seconds_to_end > 0):
        return None
    days_left = seconds_to_end / 86400
    daily = est / days_left if days_left > 0 else est
    quota = min(daily, est, remaining_today, seconds_to_end)
    return int(quota) if quota > 0 else None


def suggest_windows(now_min: float, occupied: list[dict],
                    needed_seconds: int) -> tuple[list[dict], int]:
    """从 now_min 向次日 04:00 贪心填空闲段，返回 (建议块, 排不下的秒数)。

    occupied: [{start_min, end_min, label}]，会被裁剪合并。
    规则：最多 2 块；不足 30 分钟的碎片不出块（除非这一块正好填满需求）；
    块尾的 note 记录它避开的占用段。
    """
    if needed_seconds <= 0:
        return [], 0
    cursor = max(0, min(int(now_min), DAY_MINUTES))
    merged = []
    for seg in sorted(occupied, key=lambda s: (s["start_min"], s["end_min"])):
        s = max(0, int(seg["start_min"]))
        e = min(DAY_MINUTES, int(seg["end_min"]))
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e, merged[-1][2])
        else:
            merged.append((s, e, seg.get("label") or ""))

    blocks = []
    remaining = needed_seconds
    min_block = MIN_SUGGESTION_MIN * 60

    def _try_fill(free_start: int, free_end: int, label: str) -> None:
        nonlocal remaining
        cap = (free_end - free_start) * 60
        if cap <= 0:
            return
        piece = min(remaining, cap)
        if piece < min_block and piece < remaining:
            return  # 碎片太短，留给 shortfall 诚实上报
        blocks.append({
            "start_min": free_start,
            "duration_min": piece // 60,
            "note": f"避开{label}" if label and piece == cap else "",
        })
        remaining -= piece

    for s, e, label in merged:
        if remaining <= 0 or len(blocks) >= MAX_SUGGESTION_BLOCKS:
            break
        if e <= cursor:
            continue
        if s > cursor:
            _try_fill(cursor, s, label)
        cursor = max(cursor, e)
    if (remaining > 0 and len(blocks) < MAX_SUGGESTION_BLOCKS
            and cursor < DAY_MINUTES):
        _try_fill(cursor, DAY_MINUTES, "")
    return blocks, remaining


def suggest_round_windows(now_min: float, occupied: list[dict],
                          needed_runs: int, seconds_per_loop: int) -> tuple[list[dict], int]:
    """联队战建议只放完整圈；每段最多 99 圈，避免推荐任务表单填不下的数量。"""
    if needed_runs <= 0 or seconds_per_loop <= 0:
        return [], max(0, needed_runs)
    cursor = max(0, min(math.ceil(now_min), DAY_MINUTES))
    merged = []
    for seg in sorted(occupied, key=lambda s: (s["start_min"], s["end_min"])):
        start = max(0, int(seg["start_min"]))
        end = min(DAY_MINUTES, int(seg["end_min"]))
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end, merged[-1][2])
        else:
            merged.append((start, end, seg.get("label") or ""))

    blocks = []
    remaining = needed_runs

    def fill(start: int, end: int, label: str) -> None:
        nonlocal remaining
        while remaining > 0 and len(blocks) < MAX_SUGGESTION_BLOCKS:
            capacity = (end - start) * 60 // seconds_per_loop
            runs = min(remaining, capacity, 99)
            if runs <= 0:
                return
            duration = math.ceil(runs * seconds_per_loop / 60)
            blocks.append({
                "start_min": start,
                "duration_min": duration,
                "runs": runs,
                "note": f"避开{label}" if label else "",
            })
            remaining -= runs
            start += duration

    for start, end, label in merged:
        if remaining <= 0 or len(blocks) >= MAX_SUGGESTION_BLOCKS:
            break
        if end <= cursor:
            continue
        if start > cursor:
            fill(cursor, start, label)
        cursor = max(cursor, end)
    if remaining > 0 and len(blocks) < MAX_SUGGESTION_BLOCKS and cursor < DAY_MINUTES:
        fill(cursor, DAY_MINUTES, "")
    return blocks, remaining


def _occupied_segments(expedition_items: list[dict], cfg: dict,
                       activity_team_no: int | None) -> list[dict]:
    """避开刷新、派遣动作和活动队外出；队伍未知时保守避开全部远征。

    v2：上轴的只有 forced 班和远征中/待收——待收的队已到家门口不占窗。
    """
    occupied = [{
        "start_min": DAILY_RESET_WINDOW[0],
        "end_min": DAILY_RESET_WINDOW[1],
        "label": "日课刷新",
    }]
    active_items = [e for e in expedition_items
                    if e.get("will_run") and e.get("state") != "awaiting_collect"]
    managed = {int(e.get("team_no") or 0) for e in active_items}
    away_whole_shift = bool(activity_team_no and activity_team_no in managed)
    for e in active_items:
        team = scheduler.TEAM_NAMES.get(int(e.get("team_no") or 0),
                                        f"部队{e.get('team_no')}")
        start = e["time_min"]
        when = f"{start // 60:02d}:{start % 60:02d}"
        if e.get("state") == "running":
            # 队在外面：活动队是这支（或不知道活动队）时整段避让
            if activity_team_no is None \
                    or int(e.get("team_no") or 0) == activity_team_no:
                occupied.append({
                    "start_min": start,
                    "end_min": start + int(e.get("duration_min") or 0)
                    if e.get("duration_min") else DAY_MINUTES,
                    "label": f"{when} {team}远征",
                })
            continue
        if e.get("state") in (scheduler.SLOT_EXPIRED, scheduler.SLOT_FAILED):
            continue
        occupied.append({
            "start_min": start - ACTION_WINDOW_BEFORE_MIN,
            "end_min": start + ACTION_WINDOW_AFTER_MIN,
            "label": f"{when} {team}派遣",
        })
        # 活动队今天有班要跑时，整段远征队伍都不在家，出不了阵
        if activity_team_no is None or (away_whole_shift
                                        and int(e.get("team_no") or 0) == activity_team_no):
            occupied.append({
                "start_min": start,
                "end_min": start + int(e.get("duration_min") or 0)
                if e.get("duration_min") else DAY_MINUTES,
                "label": f"{when} {team}远征",
            })
    return occupied


def build_day_timeline(now: float | None = None, *, cfg: dict | None = None,
                       store=None, script_labels: dict | None = None,
                       active: dict | None = None,
                       player_tasks: list | None = None,
                       hanafuda_team_no: int | None = None,
                       raid_team_no: int | None = None,
                       expedition_choices: dict | None = None,
                       expedition_forced: dict | None = None,
                       expedition_help: dict | None = None,
                       expedition_records: dict | None = None,
                       planning: dict | None = None,
                       situation_path=None) -> dict:
    """组装 24 小时只读时间轴：远征块 + 任务运行条 + 参考线 + 挂机建议。

    远征泳道 v2：只有已 forced 的班和远征中/待收上轴（preset 死班表
    不再投影）；建议淡影由引擎按缺口现算，点了才跑。
    expedition_help / planning / expedition_records 都可注入（测试）；
    缺省分别从偏好文件、账本报告和 expeditions.json 取。
    """
    now = time.time() if now is None else now
    if cfg is None:
        cfg = scheduler.load_config()
    if store is None:
        try:
            from touken.telemetry import get_telemetry_store
            store = get_telemetry_store()
        except Exception:
            store = None
    day_start, day_end = _day_window(now)
    if expedition_choices is None or expedition_forced is None:
        loaded_choices, loaded_forced = load_choice_sets()
        if expedition_choices is None:
            expedition_choices = loaded_choices
        if expedition_forced is None:
            expedition_forced = loaded_forced
    raw_records = expedition_records
    if expedition_records is None:
        try:
            expedition_records = scheduler.expedition_records()
            raw_records = expedition_records
            from .expedition_observation import load_observations, visible_records
            expedition_records = visible_records(expedition_records, load_observations())
        except Exception:
            expedition_records = {}
    expeditions = _expedition_items(cfg, now, day_start,
                                    forced=expedition_forced,
                                    records=expedition_records)
    if expedition_help is None:
        expedition_help = expedition_advisor.load_prefs()
    if planning is None:
        planning = _load_planning_snapshot(store)
    now_min = (now - day_start) / 60
    if player_tasks is None and active and active.get('script'):
        from .task_reservations import task_windows
        player_tasks = task_windows(now, day_start, None, None, active, store, lambda _: None)
    # 每队当天已出发（含已完成）及待执行的班都计数，按每队 N 班补足；
    # team_busy_until 记每队最后一班几点收工，补的班往那之后排
    committed_counts = _expedition_counts(cfg, expedition_forced, raw_records, store, day_start, now)
    team_busy_until: dict[int, int] = {}
    for item in expeditions:
        if item["kind"] == "running" \
                or (item["will_run"]
                    and item["state"] not in scheduler.TERMINAL_STATES):
            team_no = int(item.get("team_no") or 0)
            if team_no:
                end_min = int(item.get("time_min") or 0) + int(
                    item.get("duration_min") or 0)
                team_busy_until[team_no] = max(
                    team_busy_until.get(team_no, 0), end_min)
    # 一张图同时只能一队在跑：未完结班（running/待收/已点且时段没过完）
    # 占住的图不再给新建议；expired/failed 的班 will_run=False，不占图
    occupied_maps = set()
    occupied_windows = []
    for item in expeditions:
        code = str(item.get("map_code") or "")
        if not code:
            continue
        if item["kind"] == "running":  # 远征中/待收都算没完结
            if item["state"] == "awaiting_collect":
                occupied_maps.add(code)  # 未确认收菜，不能让别队抢同图
            else:
                occupied_windows.append((code, item["time_min"], item["time_min"] + item["duration_min"]))
        elif item.get("will_run") and now_min < item["time_min"] + int(
                item.get("duration_min") or 0):
            occupied_windows.append((code, item["time_min"], item["time_min"] + item["duration_min"]))
    # 今天同图同队 failed 过的组合拉黑到今天结束（防失败循环；
    # 图本身不拉黑，换队仍可荐）
    failed_combos = {(str(item.get("map_code") or ""),
                      int(item.get("team_no") or 0))
                     for item in expeditions
                     if item["kind"] == "forced"
                     and item.get("state") == scheduler.SLOT_FAILED
                     and not ((expedition_help or {}).get("team_formations", {}).get(str(item.get("team_no")))
                              and expedition_help["team_formations"][str(item["team_no"])] != item.get("formation_id"))
                     and item.get("map_code")}
    advice = expedition_advisor.build_expedition_suggestions(
        expedition_help, planning=planning, situation_path=situation_path,
        now_min=now_min, committed_counts=committed_counts, occupied_windows=occupied_windows,
        team_busy_until=team_busy_until,
        occupied_maps=occupied_maps, failed_combos=failed_combos,
        task_windows=player_tasks or [])
    hanafuda_plan = _hanafuda_active_plan(now, store)
    raid_plan = _raid_active_plan(now, store)
    suggestions = None
    shortfall_seconds = None
    activity = None
    hint = None
    if hanafuda_plan and raid_plan:
        hint = "秘宝之里和联队战都在进行，今天先不替你选活动；时间表仍显示远征班次。"
    elif raid_plan:
        daily_runs = _raid_daily_runs(raid_plan)
        pace = int(raid_plan["seconds_per_loop"])
        completed = int(raid_plan.get("completed_today") or 0)
        occupied = _occupied_segments(expeditions, cfg, raid_team_no) + list(player_tasks or [])
        # 活动收摊前留五分钟收尾，不把一圈安排到收摊之后。
        finish_min = math.floor(now_min + raid_plan["seconds_to_end"] / 60 - 5)
        if finish_min < DAY_MINUTES:
            occupied.append({"start_min": finish_min,
                             "end_min": DAY_MINUTES, "label": "活动收摊"})
        suggestions, remaining_runs = suggest_round_windows(
            now_min, occupied, daily_runs, pace)
        shortfall_seconds = remaining_runs * pace
        activity = {"name": "联队战", "target_runs": daily_runs,
                    "planned_runs": daily_runs - remaining_runs,
                    "completed_today": completed,
                    "seconds_per_loop": pace,
                    "remaining_runs": int(raid_plan["runs_needed"]),
                    "event_end_at": now + raid_plan["seconds_to_end"],
                    "occupied": occupied}
        pace_label = (f"{pace // 60} 分 {pace % 60} 秒" if pace >= 60
                      else f"{pace} 秒")
        hint = (f"联队战：今天已记 {completed} 圈，接下来按进度建议"
                f" {daily_runs} 圈；按本期实测每圈约 {pace_label}"
                "找远征空窗。只是建议，不会自动开工。")
        if active and active.get('script') == 'raid':
            suggestions = None
            activity = None
            hint = '联队战正在运行；收工后按最新圈数更新建议。'
    else:
        quota = (_daily_quota_seconds(hanafuda_plan, day_end - now)
                 if hanafuda_plan else None)
        if quota:
            occupied = _occupied_segments(expeditions, cfg, hanafuda_team_no) + list(player_tasks or [])
            suggestions, shortfall_seconds = suggest_windows(now_min, occupied, quota)
        hint = _hanafuda_hint(hanafuda_plan)
    return {
        "now": now,
        "day_start": day_start,
        "markers": [{"time_min": DAY_MINUTES, "label": "日课刷新", "kind": "daily_reset"}],
        "expeditions": expeditions,
        "expedition_schedule_enabled": bool(cfg.get("automation", {}).get("enabled")),
        "expedition_help": {
            "rounds_per_team": int(expedition_help.get("rounds_per_team")
                                   or 0),
            "available_teams": list(expedition_help.get("available_teams") or []),
            "resource_focus": expedition_help.get("resource_focus") or "",
            "team_formations": dict(expedition_help.get("team_formations") or {}),
            "suggested_resource": expedition_advisor.shortage_order(planning, 1)[0][0] if expedition_advisor._planning_has_data(planning) else "",
        },
        "expedition_suggestions": advice["suggestions"],
        "expedition_advice_note": advice["note"],
        "runs": _run_items(store, active, day_start, day_end, script_labels),
        "hint": hint,
        "activity": activity,
        "suggestions": suggestions,
        "shortfall_seconds": shortfall_seconds,
    }
