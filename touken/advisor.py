"""规划建议 MVP：吃现有账本数据，回答「按现在的速度，到目标日能攒多少」。

纯函数模块：不碰 FastAPI、不碰游戏画面。输入是 telemetry 的
resource_ledger(daily_series) 和 osaka.koban_session 事件，输出结构化
建议 + 一句狐狸人话。目标清单存在数据目录的 planning_goals.json。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 老 Python 兜底
    _TZ = timezone(timedelta(hours=8))

from .telemetry import LEDGER_RESOURCES
from . import acquisition

PLANNING_SCHEMA_VERSION = 1
GOALS_SCHEMA_VERSION = 2  # planning_goals.json 文件格式：{schema_version, goals[]}
RATE_WINDOW_DAYS = 14
EVENT_RATE_WINDOW_DAYS = 60
GOALS_FILENAME = "planning_goals.json"
_MAX_TARGET = 100_000_000

# 这些天进账结构完全不同（活动图/门票/钥匙），和平常不能一锅端
_EVENT_SOURCE_HEADS = ("osaka", "edocastle", "raid", "pumpkin")

# 代币档线型活动（§25.3）：秘宝之里「玉」、联队战「夜光贝」共用同一套
# 规划口径（默认冲满档、本期可自定目标、telemetry 实测场均/圈速、白票
# 回票）。只有事件名、payload 字段和单圈可信上限按机理区分。
CURRENCY_MECHANICS = {
    "hanafuda": {"event_type": "hanafuda.run_completed",
                 "delta_field": "tama",
                 "total_field": "tama_total",
                 "loop_cap": 10_000},
    "raid": {"event_type": "raid.round_completed",
             "delta_field": "shells",
             "total_field": "shells_total",
             "loop_cap": 100_000},
}
# 两种货币满档都是 300,000
DEFAULT_CURRENCY_TARGET = 300_000


def _next_game_day_reset(now_ts: float) -> float:
    """国服按上海时间凌晨 4 点换日；返回 now 之后最近的一次换日时刻。"""
    local = datetime.fromtimestamp(now_ts, _TZ)
    reset = local.replace(hour=4, minute=0, second=0, microsecond=0)
    if local >= reset:
        reset += timedelta(days=1)
    return reset.timestamp()


def game_days_remaining(now_ts: float, end_ts: float) -> int:
    """now 到 end 之间还剩几个「完整游戏日」（凌晨 4 点换日口径）。

    当前游戏日算 1 个，之后每多一个完整 04:00→04:00 日 +1；收摊前
    不足一天的零头（比如凌晨 5 点收摊只剩 04:00–05:00）并入最后一天，
    不单独占一天——免得把最后一圈摊到收摊前的凌晨，默认玩家四五点
    起床补圈。
    """
    if end_ts <= now_ts:
        return 1
    reset = _next_game_day_reset(now_ts)
    if end_ts <= reset:
        return 1
    return 1 + int((end_ts - reset) // 86400)


def _today() -> date:
    return datetime.now(_TZ).date()


def _resolve_now(today: date | None = None,
                 now: datetime | None = None) -> datetime:
    """生产入口用真实上海时刻；测试显式传 today 时保持中午的确定性。"""
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=_TZ)
        return now.astimezone(_TZ)
    if today is not None:
        return datetime.combine(today, datetime.min.time(), tzinfo=_TZ) \
            + timedelta(hours=12)
    return datetime.now(_TZ)


def _fmt(value: float | int) -> str:
    return f"{int(round(value)):,}"


def _local_date(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), _TZ).date().isoformat()


# ── 目标清单存取 ──


def load_goals(path: Path) -> list[dict]:
    """读目标清单；文件缺失或损坏都当空清单（不炸面板）。
    兼容两代格式：v1 纯数组、v2 {schema_version, goals[]}。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        entries = data.get("goals")
    else:
        entries = data  # v1：顶层就是数组
    if not isinstance(entries, list):
        return []
    goals = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == "fragment":
            # 碎片目标：resource 就是碎片名，不走账本八资源校验；名字在立目标
            # 时已对数据卡校验，读取时容忍（数据卡更新不该误杀老目标）
            target = entry.get("target")
            if (isinstance(entry.get("fragment"), str) and entry["fragment"]
                    and isinstance(target, (int, float)) and 0 < target <= 999):
                goals.append(entry)
            continue
        if entry.get("resource") not in LEDGER_RESOURCES:
            continue
        mode = entry.get("goal_mode") or "combined"
        has_target = isinstance(entry.get("target"), (int, float))
        has_deadline = isinstance(entry.get("deadline"), str) and bool(entry.get("deadline"))
        if ((mode == "amount_target" and has_target)
                or (mode == "deadline_target" and has_deadline)
                or (mode not in ("amount_target", "deadline_target")
                    and has_target and has_deadline)):
            goals.append(entry)
    for goal in goals:
        goal.setdefault("kind", "resource")  # v1 老目标一律是攒钱目标
    return goals


def _save_goals(path: Path, goals: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # v1 → v2 迁移留后路：先备份旧文件，再临时文件 + 原子替换
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            old = None
        if isinstance(old, list):
            shutil.copy2(path, path.with_name(path.name + ".v1.bak"))
    payload = {"schema_version": GOALS_SCHEMA_VERSION, "goals": goals}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def add_goal(path: Path, *, resource: str, target=None, deadline: str = "",
             goal_mode: str = "combined", note: str = "") -> dict:
    """加一条攒钱目标。参数不像话就 ValueError（人话，直接给前端显示）。"""
    if resource not in LEDGER_RESOURCES:
        raise ValueError(f"不认识「{resource}」，目标资源得是账本里的八种之一")
    if goal_mode not in ("combined", "amount_target", "deadline_target"):
        raise ValueError("目标方式不认识，请重新选择")
    parsed_target = None
    if goal_mode != "deadline_target":
        try:
            parsed_target = int(target)
        except (TypeError, ValueError):
            raise ValueError("目标数量得是个整数")
        if not 0 < parsed_target <= _MAX_TARGET:
            raise ValueError("目标数量不对劲（得大于 0，也别超过一亿啦）")
    deadline_date = None
    if goal_mode != "amount_target":
        try:
            deadline_date = date.fromisoformat(str(deadline).strip())
        except ValueError:
            raise ValueError("截止日期得是 年-月-日 这样的日期")
        if deadline_date < _today():
            raise ValueError("截止日期已经过去啦，往今天以后挑")
    goals = load_goals(path)
    goal = {
        "id": max([int(g.get("id") or 0) for g in goals], default=0) + 1,
        "kind": "resource",
        "goal_mode": goal_mode,
        "resource": resource,
        "note": str(note or "").strip()[:50],
        "created_at": time.time(),
    }
    if parsed_target is not None:
        goal["target"] = parsed_target
    if deadline_date is not None:
        goal["deadline"] = deadline_date.isoformat()
    goals.append(goal)
    _save_goals(path, goals)
    return goal


def add_fragment_goal(path: Path, *, fragment: str, target, note: str = "") -> dict:
    """立一条异去碎片目标。一种碎片至多一条，重复立原位更新。"""
    fragment = str(fragment or "").strip()
    catalog = acquisition.fragment_catalog()
    if fragment not in catalog:
        raise ValueError(f"不认识碎片「{fragment}」，异去数据卡里没有收录")
    try:
        target = int(target)
    except (TypeError, ValueError):
        raise ValueError("目标数量得是个整数")
    if not 0 < target <= 999:
        raise ValueError("目标数量不对劲（得大于 0，999 封顶啦）")
    goals = load_goals(path)
    existing = next((g for g in goals
                     if g.get("kind") == "fragment"
                     and g.get("fragment") == fragment), None)
    if existing is not None:
        existing.update({"target": target, "note": str(note or "").strip()[:50],
                         "updated_at": time.time()})
        _save_goals(path, goals)
        return existing
    goal = {
        "id": max([int(g.get("id") or 0) for g in goals], default=0) + 1,
        "kind": "fragment",
        "goal_mode": "amount_target",
        "resource": fragment,  # 前端进度条/标题都吃 resource，碎片名直接顶上
        "fragment": fragment,
        "target": target,
        "note": str(note or "").strip()[:50],
        "created_at": time.time(),
    }
    goals.append(goal)
    _save_goals(path, goals)
    return goal


def delete_goal(path: Path, goal_id: int) -> bool:
    goals = load_goals(path)
    kept = [g for g in goals if int(g.get("id") or 0) != int(goal_id)]
    if len(kept) == len(goals):
        return False
    _save_goals(path, kept)
    return True


# ── 速率与单产估计 ──


def estimate_daily_rates(daily_series: list[dict], *,
                         window_days: int = RATE_WINDOW_DAYS,
                         today: date | None = None,
                         exclude_dates: set[str] | None = None) -> dict:
    """每种资源的日均净收支：窗口内有首末读数的日子取 total_delta 求平均。

    没有完整读数的日子跳过（不知道就是不知道，不按 0 算）。
    exclude_dates 里的日子（活动期）不算进平常速度——活动期间和平常
    的进账是两码事，混在一起预测必歪。
    """
    today = today or _today()
    cutoff = (today - timedelta(days=window_days - 1)).isoformat()
    today_iso = today.isoformat()
    excluded = exclude_dates or set()
    rates = {}
    for name in LEDGER_RESOURCES:
        deltas = [row["total_delta"] for row in daily_series
                  if row.get("resource") == name
                  and row.get("total_delta") is not None
                  and cutoff <= str(row.get("date", "")) <= today_iso
                  and str(row.get("date", "")) not in excluded]
        rates[name] = {
            "daily": (sum(deltas) / len(deltas)) if deltas else None,
            "days_observed": len(deltas),
        }
    return rates


def event_day_dates(attributions: list[dict], *,
                    window_days: int = EVENT_RATE_WINDOW_DAYS,
                    today: date | None = None) -> set[str]:
    """哪些日子是「活动期间」：归因里出现活动来源（大阪城/江户城/联队战/南瓜）的日子。"""
    today = today or _today()
    cutoff = (today - timedelta(days=window_days - 1)).isoformat()
    dates: set[str] = set()
    for item in attributions:
        head = str(item.get("source") or "").split(".", 1)[0].split("/", 1)[0]
        if head not in _EVENT_SOURCE_HEADS:
            continue
        ts = item.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        day = _local_date(ts)
        if cutoff <= day <= today.isoformat():
            dates.add(day)
    return dates


def estimate_event_rates(daily_series: list[dict], event_dates: set[str], *,
                         today: date | None = None) -> dict:
    """活动期间的日均净收支：只取活动日的 total_delta 求平均。

    活动样本本来就少，不设观察天数下限，但 days_observed 会如实带出去，
    让前端/建议文案知道这是几场活动的手气。
    """
    today = today or _today()
    today_iso = today.isoformat()
    rates = {}
    for name in LEDGER_RESOURCES:
        deltas = [row["total_delta"] for row in daily_series
                  if row.get("resource") == name
                  and row.get("total_delta") is not None
                  and str(row.get("date", "")) in event_dates
                  and str(row.get("date", "")) <= today_iso]
        rates[name] = {
            "daily": (sum(deltas) / len(deltas)) if deltas else None,
            "days_observed": len(deltas),
        }
    return rates


def koban_floor_yield(events: list[dict], *,
                      max_sessions: int = 30,
                      window_days: int = RATE_WINDOW_DAYS,
                      now: float | None = None) -> dict | None:
    """从 osaka.koban_session 事件估平均每层小判（按层数加权）。

    events 用 recent_events 的倒序结果即可。只认 window_days 天内的样本——
    大阪城关了之后还拿旧手气换算「每天多挖 N 层」就是空头支票；
    没有新鲜样本就返回 None，建议里不提层数。
    """
    cutoff = (now if now is not None else time.time()) - window_days * 86400
    floors_total = 0.0
    delta_total = 0.0
    sessions = 0
    for event in events[:max_sessions]:
        if not isinstance(event.get("ts"), (int, float)) or event["ts"] < cutoff:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        floors = payload.get("floors")
        delta = payload.get("delta")
        if not isinstance(floors, (int, float)) or floors <= 0:
            continue
        if not isinstance(delta, (int, float)):
            continue
        floors_total += floors
        delta_total += delta
        sessions += 1
    if not sessions or floors_total <= 0:
        return None
    return {"per_floor": delta_total / floors_total, "sessions": sessions}


def latest_osaka_floor_speed(store, *, now: float | None = None,
                             window_days: int = RATE_WINDOW_DAYS) -> dict | None:
    """取最近一轮有效的大阪城实测层速。

    规划要回答的是「剩余层数还要挂多久」，所以这里沿用成绩单已经算好的
    average_loop_seconds。失败或手动停止的轮次只要确实跑出了层数，层速仍然
    是有效样本；零层、脏数字和过期记录不参与。
    """
    getter = getattr(store, "recent_run_summaries", None)
    if not callable(getter):
        return None
    now_ts = now if now is not None else time.time()
    try:
        runs = getter(limit=10, script="osaka",
                      from_ts=now_ts - window_days * 86400, to_ts=now_ts + 1)
    except (TypeError, ValueError):
        return None
    for run in runs:
        seconds = run.get("average_loop_seconds")
        floors = run.get("loops")
        if (not isinstance(seconds, (int, float)) or seconds <= 0
                or not isinstance(floors, (int, float)) or floors <= 0):
            continue
        return {
            "seconds_per_floor": round(float(seconds), 1),
            "floors": int(floors),
            "run_started_at": run.get("started_at"),
        }
    return None


# ── 目标评估 ──


def _split_goal_days(today: date, deadline: date,
                     event_windows: list[dict] | None) -> tuple[int, int, list[str]]:
    """今天到截止日之间，平常几天、活动期几天（按活动知识卡的开打/收摊日）。

    返回 (平常天数, 活动期天数, 沾边的活动名)。活动期重叠的日子只算一次。
    """
    if isinstance(today, str):
        today = date.fromisoformat(today)
    if isinstance(deadline, str):
        deadline = date.fromisoformat(deadline)
    event_dates: set[date] = set()
    names: list[str] = []
    for window in event_windows or []:
        try:
            start = date.fromisoformat(str(window.get("start_date") or ""))
            end = date.fromisoformat(str(window.get("end_date") or ""))
        except ValueError:
            continue
        # 攒钱从明天算起（days_left 语义），截止日当天也算一天
        start = max(start, today + timedelta(days=1))
        end = min(end, deadline)
        if start > end:
            continue
        if window.get("name"):
            names.append(str(window["name"]))
        day = start
        while day <= end:
            event_dates.add(day)
            day += timedelta(days=1)
    total = max((deadline - today).days, 0)
    return total - len(event_dates), len(event_dates), names


def _window_coverage(today: date, deadline: date,
                     window: dict) -> tuple[int, int]:
    """活动窗口落在目标期内的天数 + 窗口总天数（用来按比例折算窗口影响）。"""
    try:
        start = date.fromisoformat(str(window.get("start_date") or ""))
        end = date.fromisoformat(str(window.get("end_date") or ""))
    except ValueError:
        return 0, 1
    total = max((end - start).days + 1, 1)
    covered_start = max(start, today + timedelta(days=1))
    covered_end = min(end, deadline)
    covered = max((covered_end - covered_start).days + 1, 0)
    return covered, total


def _evaluate_fragment_goal(goal: dict, *, inventory: dict | None,
                            guides: dict | None) -> dict:
    """碎片目标算命。进度来自 yosari.fragments 库存快照；指引来自掉率途径卡。

    没有日期预测——异去打不打、每天打几圈全看安排，不按速率瞎估。
    """
    fragment = goal.get("fragment") or goal.get("resource")
    target = int(goal["target"])
    current = None
    if isinstance(inventory, dict):
        value = inventory.get(fragment)
        current = int(value) if isinstance(value, (int, float)) else None
    advice = {
        "id": goal.get("id"), "kind": "fragment", "event": None,
        "goal_mode": "amount_target", "resource": fragment, "fragment": fragment,
        "target": target, "deadline": None, "estimated_deadline": None,
        "deadline_at": None, "note": goal.get("note") or "",
        "days_left": None, "current": current, "rate": None,
        "event_days": 0, "event_rate": None, "projected": None,
        "shortfall": None, "extra_daily": None, "extra_floors": None,
        "expected_runs": None, "best_map_label": None, "fragment_rate": None,
        "status": "unknown", "message": "",
    }
    guide = (guides or {}).get(fragment) or {}
    best = guide.get("best_map") or {}
    best_rate = best.get("rate") if isinstance(best.get("rate"), (int, float)) else None
    if best_rate:
        advice["best_map_label"] = best.get("label")
        advice["fragment_rate"] = best_rate
    if current is None:
        advice["message"] = (f"还没读到「{fragment}」的库存，去打一圈异去，"
                             "狐之助每圈结束会开碎片弹窗对账。")
        return advice
    if current >= target:
        advice["status"] = "done"
        advice["message"] = (f"「{fragment}」已经有 {current} 个，"
                             f"目标 {target} 个达成啦🎉")
        return advice
    shortfall = int(target - current)
    advice["shortfall"] = shortfall
    advice["status"] = "active"
    if best_rate and best_rate > 0:
        import math
        runs = max(1, math.ceil(shortfall / best_rate))
        advice["expected_runs"] = runs
        advice["message"] = (
            f"「{fragment}」现在有 {current} 个，还差 {shortfall} 个。"
            f"按日服实测掉率（{best.get('label')} 每圈约 {best_rate * 100:.0f}%），"
            f"大约还要 {runs} 圈异去。")
    else:
        advice["message"] = (f"「{fragment}」现在有 {current} 个，还差 {shortfall} 个；"
                             "数据卡还没收录它的掉率，先按自己喜欢的图刷。")
    return advice


def evaluate_goal(goal: dict, *, current: float | None, rate_info: dict,
                  floor_yield: dict | None, today: date | None = None,
                  event_windows: list[dict] | None = None,
                  window_impacts: dict | None = None,
                  floor_speed: dict | None = None,
                  now: datetime | None = None,
                  fragment_inventory: dict | None = None,
                  fragment_guides: dict | None = None) -> dict:
    """给一条目标算命。status: done / on_track / behind / expired / unknown。

    rate_info 里 daily 是平常速度、event_daily 是活动期间速度；截止日内
    落在活动期的天数按活动速度估，其余按平常速度。
    """
    if (goal.get("kind") or "resource") == "fragment":
        return _evaluate_fragment_goal(goal, inventory=fragment_inventory,
                                       guides=fragment_guides)
    now_dt = _resolve_now(today, now)
    today = today or now_dt.date()
    resource = goal["resource"]
    goal_mode = goal.get("goal_mode") or "combined"
    target = int(goal["target"]) if goal.get("target") is not None else None
    deadline = (date.fromisoformat(goal["deadline"])
                if goal.get("deadline") else None)
    days_left = (deadline - today).days if deadline else None
    rate = (rate_info or {}).get("daily")
    advice = {
        "id": goal.get("id"),
        "kind": goal.get("kind") or "resource",
        "event": goal.get("event"),
        "goal_mode": goal.get("goal_mode"),
        "resource": resource,
        "target": target,
        "deadline": goal.get("deadline"),
        "estimated_deadline": None,
        "deadline_at": goal.get("deadline_at"),
        "note": goal.get("note") or "",
        "days_left": max(days_left, 0) if days_left is not None else None,
        "current": current,
        "rate": rate,
        "event_days": 0,
        "event_rate": None,
        "projected": None,
        "shortfall": None,
        "extra_daily": None,
        "extra_floors": None,
        "floors_needed": None,
        "floors_per_day": None,
        "seconds_per_floor": None,
        "speed_sample_floors": None,
        "estimated_seconds": None,
        "remaining_seconds": None,
        "time_margin_seconds": None,
        "can_finish": None,
        "status": "unknown",
        "message": "",
    }

    if current is None:
        advice["message"] = (f"还没观察到{resource}的库存，跑一趟任务"
                             "让狐之助看一眼家底再算。")
        return advice
    if goal_mode == "amount_target":
        if current >= target:
            advice["status"] = "done"
            advice["message"] = (f"{resource}已经有 {_fmt(current)}，"
                                 f"数量目标 {_fmt(target)} 已经达成。")
            return advice
        if rate is None or rate <= 0:
            advice["message"] = (f"最近 {RATE_WINDOW_DAYS} 天还没有稳定的{resource}净进账，"
                                 "暂时算不出哪天能攒到。")
            return advice
        import math
        days_needed = max(1, math.ceil((target - current) / rate))
        estimated = today + timedelta(days=days_needed)
        advice["status"] = "active"
        advice["days_left"] = days_needed
        advice["estimated_deadline"] = estimated.isoformat()
        advice["shortfall"] = int(target - current)
        advice["message"] = (f"现在有 {_fmt(current)} {resource}，按最近每天 "
                             f"{int(round(rate)):+,} 的净进账，大约 {estimated.isoformat()}"
                             f"能攒到 {_fmt(target)}。")
        return advice
    if target is not None and current >= target:
        advice["status"] = "done"
        advice["message"] = (f"{resource}已经有 {_fmt(current)}，"
                             f"目标 {_fmt(target)} 稳稳拿下啦🎉")
        return advice
    if goal.get("goal_mode") == "stock_target":
        deadline_at = goal.get("deadline_at")
        try:
            end_dt = (datetime.fromisoformat(str(deadline_at))
                      if deadline_at else datetime.combine(
                          deadline, datetime.max.time(), tzinfo=_TZ))
        except ValueError:
            end_dt = datetime.combine(deadline, datetime.max.time(), tzinfo=_TZ)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=_TZ)
        advice["remaining_seconds"] = max(
            0, int(round((end_dt - now_dt).total_seconds())))
        if now_dt >= end_dt:
            advice["status"] = "expired"
            advice["shortfall"] = int(target - current)
            advice["message"] = (f"{goal.get('event') or '活动'}已经收摊，"
                                 f"{resource}离目标还差 {_fmt(target - current)}。")
            return advice
        advice["shortfall"] = int(target - current)
        per_floor = (floor_yield or {}).get("per_floor")
        if not per_floor or per_floor <= 0:
            advice["message"] = (
                f"现在有 {_fmt(current)} {resource}，离目标还差 "
                f"{_fmt(target - current)}。狐之助还没攒够大阪城单层收益样本，"
                "再挖几层就能换算需要多少层。")
            return advice
        import math
        floors_needed = max(1, math.ceil((target - current) / per_floor))
        # 剩余天数按凌晨 4 点换日数完整游戏日：收摊前的零头小时并入
        # 今天，不单独占一天（与联队战/花札的每日建议同一口径）。
        remaining_days = game_days_remaining(now_dt.timestamp(),
                                             end_dt.timestamp())
        advice["status"] = "active"
        advice["floors_needed"] = floors_needed
        advice["floors_per_day"] = max(1, math.ceil(floors_needed / remaining_days))
        advice["extra_floors"] = advice["floors_per_day"]
        seconds_per_floor = (floor_speed or {}).get("seconds_per_floor")
        if isinstance(seconds_per_floor, (int, float)) and seconds_per_floor > 0:
            estimated_seconds = int(math.ceil(floors_needed * seconds_per_floor))
            margin_seconds = advice["remaining_seconds"] - estimated_seconds
            advice["seconds_per_floor"] = round(float(seconds_per_floor), 1)
            advice["speed_sample_floors"] = int(
                (floor_speed or {}).get("floors") or 0) or None
            advice["estimated_seconds"] = estimated_seconds
            advice["time_margin_seconds"] = margin_seconds
            advice["can_finish"] = margin_seconds >= 0
        advice["message"] = (
            f"现在有 {_fmt(current)} 小判，收摊前还差 "
            f"{_fmt(target - current)}。按最近 {floor_yield.get('sessions', 0)} 次"
            f"挖地实测（每层约 {per_floor:.0f} 小判），还要挖约 "
            f"{floors_needed} 层。所需工时按最近一轮有效层速另算。")
        return advice
    if days_left is not None and days_left <= 0 and goal_mode != "deadline_target":
        advice["status"] = "expired"
        advice["shortfall"] = target - current
        advice["message"] = (f"截止日到了，{resource}还差 "
                             f"{_fmt(target - current)}。把目标往后挪挪，"
                             "或者这页就翻篇~")
        return advice
    if rate is None:
        advice["message"] = (f"最近 {RATE_WINDOW_DAYS} 天没有{resource}的平常"
                             "收支记录，还算不出速度——再挂几天机就有了。")
        return advice

    normal_days, event_days, event_names = _split_goal_days(today, deadline, event_windows)
    # 有知识卡机理模型的活动窗口：按模型直接算净影响（江户城门票是负数），
    # 不再拿「速率 × 天数」猜（§25）；没模型的窗口维持速率兜底。
    modeled_days = 0
    modeled_delta = 0.0
    modeled_notes = []
    for window in event_windows or []:
        impact = (window_impacts or {}).get(str(window.get("name") or ""))
        if not impact or impact.get("resource") != resource:
            continue
        covered, total_days = _window_coverage(today, deadline, window)
        if not covered:
            continue
        scaled = impact["delta"] * covered / total_days
        modeled_days += covered  # 活动重叠很罕见，真重叠时多算几天认了
        modeled_delta += scaled
        modeled_notes.append(
            f"{window['name']}按知识卡估 {int(round(scaled)):+,}")
    unmodeled_days = max(event_days - modeled_days, 0)
    event_rate = (rate_info or {}).get("event_daily")
    if unmodeled_days and event_rate is None:
        # 活动收益还没实测过，先按平常速度兜底，文案里说清楚
        event_rate = rate
        event_guessed = True
    else:
        event_guessed = False
    advice["event_days"] = event_days
    advice["event_rate"] = event_rate if unmodeled_days else None
    advice["event_modeled"] = modeled_notes
    projected = (current + rate * normal_days
                 + (event_rate or 0) * unmodeled_days + modeled_delta)
    advice["projected"] = int(round(projected))
    if event_days:
        if unmodeled_days:
            pace = f"平常每天 {int(round(rate)):+,}、活动期每天 {int(round(event_rate)):+,}"
        else:
            pace = f"平常每天 {int(round(rate)):+,}"
        event_note = f"其中 {event_days} 天撞上{'、'.join(event_names) or '活动'}，"
        if modeled_notes:
            event_note += "；".join(modeled_notes) + "。"
        if unmodeled_days:
            event_note += ("活动期的收益还没实测过，先按平常速度估。"
                           if event_guessed else "按活动期间的速度另算。")
    else:
        pace = f"平常每天 {int(round(rate)):+,}"
        event_note = ""
    if goal_mode == "deadline_target":
        advice["status"] = "expired" if days_left <= 0 else "active"
        advice["message"] = (f"按{pace}的速度，到 {goal['deadline']}"
                             f"预计有 {_fmt(projected)} {resource}。{event_note}")
        return advice
    advice["shortfall"] = max(0, int(round(target - projected)))
    if projected >= target:
        advice["status"] = "on_track"
        advice["message"] = (f"按{pace}的速度，到 {goal['deadline']}"
                             f"（还有 {days_left} 天）能攒到 {_fmt(projected)}，"
                             f"目标 {_fmt(target)} 稳的。{event_note}")
        return advice

    advice["status"] = "behind"
    extra_daily = (target - projected) / days_left
    advice["extra_daily"] = int(round(extra_daily))
    message = (f"按{pace}的速度，到 {goal['deadline']}"
               f"（还有 {days_left} 天）大概攒到 {_fmt(projected)}，"
               f"离目标还差 {_fmt(target - projected)}——"
               f"剩下 {days_left} 天每天得多攒 {_fmt(extra_daily)}。{event_note}")
    if (resource == "小判" and floor_yield
            and floor_yield.get("per_floor", 0) > 0):
        floors = extra_daily / floor_yield["per_floor"]
        advice["extra_floors"] = int(round(floors)) or 1
        message += (f"按你最近挖地的手气（每层约 "
                    f"{floor_yield['per_floor']:.0f} 小判），"
                    f"≈ 每天多挖 {advice['extra_floors']} 层大阪城。")
    advice["message"] = message
    return advice


def _current_balances(store, now: float) -> dict:
    """账本里每种资源的最新余额（closing 优先，opening 兜底）。"""
    ledger = store.resource_ledger(now - EVENT_RATE_WINDOW_DAYS * 86400, now)
    current = {}
    for row in ledger.get("per_resource", []):
        value = row.get("closing")
        current[row.get("resource")] = value if value is not None else row.get("opening")
    return current


def _upsert_event_goal(path: Path, *, event: str, target: int,
                       deadline: str, note: str,
                       goal_mode: str = "budget",
                       deadline_at: str | None = None) -> dict:
    """每个活动至多一条目标；数字变化时原位更新，不重复追加。"""
    if not 0 < int(target) <= _MAX_TARGET:
        raise ValueError("活动预算不对劲，暂时不能立目标")
    goals = load_goals(path)
    goal = next((item for item in goals
                 if item.get("kind") == "event"
                 and item.get("event") == event), None)
    if goal is None:
        goal = {
            "id": max([int(item.get("id") or 0) for item in goals], default=0) + 1,
            "kind": "event",
            "event": event,
            "goal_mode": goal_mode,
            "resource": "小判",
            "target": int(target),
            "deadline": deadline,
            "note": note[:50],
            "created_at": time.time(),
        }
        goals.append(goal)
    else:
        goal.update({"goal_mode": goal_mode, "resource": "小判",
                     "target": int(target),
                     "deadline": deadline, "note": note[:50],
                     "updated_at": time.time()})
    if deadline_at:
        goal["deadline_at"] = deadline_at
    else:
        goal.pop("deadline_at", None)
    _save_goals(path, goals)
    return dict(goal)


def add_event_goal(store, goals_path: Path, event: str, *,
                   target=None,
                   today: date | None = None,
                   now: datetime | None = None) -> dict:
    """把活动准备立成目标。

    大阪城是「收摊时小判家底达到多少」；其余预算型活动的目标是预算本身，
    家底会拿来抵；
    不是旧前端那套「现有余额 + 预算」（那等于让人家多攒一倍）。
    家底已够就不立目标，直接报充足。预算数字只能这里算，前端不许自己拼。
    """
    now_dt = _resolve_now(today, now)
    today = today or now_dt.date()
    cards = load_event_cards(Path(goals_path).parent)
    if event not in cards:
        raise ValueError(f"不认识活动「{event}」")
    card = cards[event]
    current = _current_balances(store, now_dt.timestamp()).get("小判")
    if card.get("mechanics") == "osaka":
        if isinstance(target, bool):
            raise ValueError("先填一个想攒到的小判数")
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise ValueError("先填一个想攒到的小判数") from None
        if not 0 < target <= _MAX_TARGET:
            raise ValueError("小判目标不对劲，暂时不能立目标")
        deadline = card.get("end_date")
        if not deadline:
            raise ValueError("这张活动卡还没结束日期，立不了目标")
        _, end_dt, _ = _card_window(card)
        if end_dt is not None and now_dt >= end_dt:
            raise ValueError("这场活动已经结束，不能再立目标")
        result = {
            "goal_mode": "stock_target", "sufficient": None, "goal": None,
            "target": target, "koban_cost": None, "available_now": current,
            "shortfall": None,
        }
        if current is not None:
            result["shortfall"] = max(0, int(target - current))
            result["sufficient"] = result["shortfall"] == 0
        existing = next((item for item in load_goals(goals_path)
                         if item.get("kind") == "event"
                         and item.get("event") == event), None)
        if result["sufficient"] and existing is None:
            return result
        result["goal"] = _upsert_event_goal(
            goals_path, event=event, target=target, deadline=deadline,
            deadline_at=card.get("end_at"), goal_mode="stock_target",
            note=f"{event}收摊目标")
        return result
    from . import event_history
    periods = event_history.load_history(Path(goals_path).parent)
    abacus = event_abacus(event, card,
                          measured=resolve_keys_per_run(store, event, card,
                                                        periods),
                          today=today, now=now_dt)
    cost = abacus.get("koban_cost")
    if cost is None:
        raise ValueError(abacus.get("message") or "这场活动的预算还算不出来")
    deadline = card.get("start_date") or card.get("end_date")
    if not deadline:
        raise ValueError("这张活动卡还没日期，立不了目标")
    start_dt, _, _ = _card_window(card)
    if ((start_dt is not None and now_dt >= start_dt)
            or date.fromisoformat(deadline) < today) and card.get("end_date"):
        deadline = card["end_date"]  # 已开打：钱在收摊前备齐就行
    if date.fromisoformat(deadline) < today:
        raise ValueError("这场活动已经结束，不能再立预算目标")
    result = {"sufficient": None, "goal": None, "koban_cost": cost,
              "available_now": current, "shortfall": None}
    if cost <= 0:
        result["shortfall"] = 0
        result["sufficient"] = True
    elif current is not None:
        result["shortfall"] = max(0, int(cost - current))
        result["sufficient"] = result["shortfall"] == 0
    existing = next((item for item in load_goals(goals_path)
                     if item.get("kind") == "event"
                     and item.get("event") == event), None)
    if result["sufficient"] and existing is None:
        return result
    result["goal"] = _upsert_event_goal(
        goals_path, event=event, target=cost, deadline=deadline,
        note=f"{event}门票预算", goal_mode="budget")
    return result


def add_gameplay_budget_goal(store, goals_path: Path, values: dict, *,
                             now: datetime | None = None) -> dict:
    """把玩法试算结果保存成活动预算目标。

    预算数字、截止时间和当前家底都由服务端决定；前端只提交玩家选择，
    不自行拼出目标金额。当前只有异去使用这条轻量玩法试算链。
    """
    from .gameplay_planning import estimate

    now_dt = now or datetime.now(_TZ)
    result = estimate(store, values, now=now_dt)
    cost = result.get("cost")
    if cost is None:
        raise ValueError("还缺这张图的圈速，暂时不能立活动预算")
    if cost <= 0:
        raise ValueError("这套方案不需要额外小判，不用另立预算")
    campaign = result.get("campaign") or {}
    try:
        end_dt = datetime.fromisoformat(str(result.get("deadline") or ""))
    except ValueError:
        raise ValueError("这套方案还没有可用的截止时间") from None
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=_TZ)
    if end_dt <= now_dt:
        raise ValueError("这期活动已经结束，不能再立预算")

    event = str(campaign.get("name") or "异去").split(" · ", 1)[0]
    goal = _upsert_event_goal(
        goals_path, event=event, target=int(cost),
        deadline=end_dt.date().isoformat(), deadline_at=end_dt.isoformat(),
        note=f"{event}活动预算", goal_mode="budget")
    current = _current_balances(store, now_dt.timestamp()).get("小判")
    shortfall = None if current is None else max(0, int(cost - current))
    return {"goal": goal, "estimate": result, "available_now": current,
            "shortfall": shortfall,
            "sufficient": None if shortfall is None else shortfall == 0}


def get_planning(store, goals_path: Path, *,
                 forge_recipe: list | None = None,
                 today: date | None = None,
                 now: datetime | None = None) -> dict:
    """汇总入口：store 是 telemetry 仓库（resource_ledger / recent_events）。"""
    now_dt = _resolve_now(today, now)
    today = today or now_dt.date()
    now_ts = now_dt.timestamp()
    # 平常速度看最近 14 天；活动速度要回望更久，老活动样本也是样本
    ledger = store.resource_ledger(now_ts - EVENT_RATE_WINDOW_DAYS * 86400,
                                   now_ts)
    current = {}
    for row in ledger.get("per_resource", []):
        value = row.get("closing")
        current[row.get("resource")] = value if value is not None else row.get("opening")
    event_dates = event_day_dates(ledger.get("attributions", []), today=today)
    normal_rates = estimate_daily_rates(ledger.get("daily_series", []),
                                        today=today, exclude_dates=event_dates)
    event_rates = estimate_event_rates(ledger.get("daily_series", []),
                                       event_dates, today=today)
    rates = {}
    for name in LEDGER_RESOURCES:
        rates[name] = {**normal_rates[name],
                       "event_daily": event_rates[name]["daily"],
                       "event_days_observed": event_rates[name]["days_observed"]}
    floor_yield = koban_floor_yield(
        store.recent_events(limit=50, event_type="osaka.koban_session"),
        now=now_ts)
    floor_speed = latest_osaka_floor_speed(store, now=now_ts)
    # 碎片库存：取最近一次 yosari.fragments 快照（每圈结束差分记账时读的全量）
    fragment_inventory = None
    for event in store.recent_events(limit=10, event_type="yosari.fragments"):
        payload = event.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("counts"), dict):
            fragment_inventory = payload["counts"]
            break
    client_items = (store.client_item_inventory(now_ts)
                    if hasattr(store, "client_item_inventory") else {"items": {}})
    fragment_inventory = dict(fragment_inventory or {})
    ocr_fragment_ts = float(event.get("ts") or 0) if fragment_inventory else 0
    for name, item in client_items["items"].items():
        fragment = name.removesuffix("碎片")
        if "碎片" in name and (fragment not in fragment_inventory or item["observed_at"] >= ocr_fragment_ts):
            fragment_inventory[fragment] = item["count"]
    fragment_guides = acquisition.fragment_catalog()
    cards = load_event_cards(Path(goals_path).parent)
    event_windows = [{"name": name, "start_date": card.get("start_date"),
                      "end_date": card.get("end_date")}
                     for name, card in cards.items()
                     if card.get("start_date") and card.get("end_date")]
    # 活动周期档案：收摊的期次懒归档（幂等）；取值链 本期实测 > 上期经验 > 手填
    from . import event_history
    status_dir = Path(goals_path).parent
    periods = event_history.load_history(status_dir)
    resolutions = {}
    for name, card in cards.items():
        event_history.archive_if_finished(store, name, card, status_dir,
                                          now=now_dt)
        resolutions[name] = resolve_keys_per_run(store, name, card, periods)
    # §25：有机理模型的活动按知识卡直接算窗口净影响，喂给目标评估
    window_impacts = {}
    for name, card in cards.items():
        impact = window_impact(name, card, measured_keys=resolutions.get(name),
                               floor_yield=floor_yield, today=today,
                               now=now_dt)
        if impact:
            window_impacts[name] = impact
    saved_goals = load_goals(goals_path)
    goals = [evaluate_goal(goal, current=current.get(goal["resource"]),
                           rate_info=rates.get(goal["resource"]),
                           floor_yield=floor_yield, today=today,
                           event_windows=event_windows,
                           window_impacts=window_impacts,
                           floor_speed=floor_speed, now=now_dt,
                           fragment_inventory=fragment_inventory,
                           fragment_guides=fragment_guides)
             for goal in saved_goals]
    # 已确认的活动预算是未来会花掉的钱，不属于“怎么攒”。把它对小判
    # 数量目标的影响单独算清楚，避免前端拿家底、预算和日均自行拼数。
    live_budgets = []
    for goal in goals:
        if (goal.get("kind") != "event"
                or goal.get("goal_mode") != "budget"
                or (goal.get("target") or 0) <= 0):
            continue
        try:
            if (goal.get("deadline")
                    and date.fromisoformat(goal["deadline"]) < today):
                continue
        except ValueError:
            continue
        live_budgets.append(goal)
    koban_rate = (rates.get("小判") or {}).get("daily")
    amount_goals = [goal for goal in goals
                    if goal.get("resource") == "小判"
                    and goal.get("goal_mode") == "amount_target"
                    and goal.get("status") != "done"]
    if koban_rate and koban_rate > 0 and amount_goals and live_budgets:
        import math
        total_spending = sum(int(goal["target"]) for goal in live_budgets)
        for goal in amount_goals:
            base_days = int(goal.get("days_left") or 0)
            adjusted_days = max(1, math.ceil(
                ((goal.get("target") or 0) - (goal.get("current") or 0)
                 + total_spending) / koban_rate))
            goal["planned_spending"] = total_spending
            goal["impact_days"] = max(0, adjusted_days - base_days)
            goal["days_left"] = adjusted_days
            goal["estimated_deadline"] = (
                today + timedelta(days=adjusted_days)).isoformat()
            goal["message"] += (
                f" 已确认的活动预算共 {total_spending:,} 小判；若按计划花掉，"
                f"预计再推迟约 {goal['impact_days']} 天。")
        for budget in live_budgets:
            budget["impact_days"] = max(
                1, math.ceil(int(budget["target"]) / koban_rate))
            budget["conflicting_goal"] = (
                amount_goals[0].get("note")
                or f"{amount_goals[0].get('resource')}目标")
    forge_resources = ["木炭", "玉钢", "冷却材", "砥石"]
    if (not isinstance(forge_recipe, list) or len(forge_recipe) != 4
            or any(not isinstance(value, (int, float)) or value <= 0
                   for value in forge_recipe)):
        forge_recipe = [700, 700, 700, 700]
    def forge_watch(costs):
        rows = []
        for name, cost in costs:
            balance = current.get(name)
            rows.append({
                "resource": name, "current": balance, "per_forge": int(cost),
                "forge_capacity": (None if balance is None
                                   else max(0, int(balance // cost))),
            })
        capacities = [row["forge_capacity"] for row in rows]
        capacity = min(capacities) if all(value is not None for value in capacities) else None
        return {
            "resources": rows,
            "forge_capacity": capacity,
            "limiting": ([row["resource"] for row in rows
                          if row["forge_capacity"] == capacity]
                         if capacity is not None else []),
        }

    normal_forge = forge_watch([*zip(forge_resources, forge_recipe),
                                ("委托符", 1)])
    ten_forge = forge_watch([*[(name, cost * 10)
                               for name, cost in zip(forge_resources, forge_recipe)],
                             ("委托符", 9), ("加速符", 10)])
    confirmed_spending = sum(
        -int(round(item.get("delta") or 0))
        for item in ledger.get("attributions", [])
        if item.get("resource") == "小判"
        and (item.get("delta") or 0) < 0
        and (item.get("ts") or 0) >= now_ts - RATE_WINDOW_DAYS * 86400)
    koban_current = current.get("小判")
    abacuses = [event_abacus(name, card, measured=resolutions.get(name),
                             today=today, now=now_dt)
                for name, card in cards.items()]
    # 预算 vs 家底的判定也在服务端做：前端只展示，不许自己拼目标数字
    koban_now = current.get("小判")
    for abacus in abacuses:
        card = cards.get(abacus.get("event")) or {}
        abacus["mechanics"] = card.get("mechanics")
        if abacus["mechanics"] in CURRENCY_MECHANICS:
            abacus.update(currency_plan(store, card, now_dt=now_dt,
                                        mechanics=abacus["mechanics"]))
        if abacus.get("goal_mode") == "stock_target":
            abacus["yield_per_floor"] = (floor_yield or {}).get("per_floor")
            abacus["yield_sessions"] = (floor_yield or {}).get("sessions")
            abacus["koban_earned"] = osaka_period_income(store, card, now=now_dt)
        cost = abacus.get("koban_cost")
        abacus["available_now"] = koban_now
        if cost is None or koban_now is None:
            abacus["sufficient"] = None
            abacus["shortfall"] = None
        else:
            abacus["shortfall"] = max(0, int(cost - koban_now))
            abacus["sufficient"] = abacus["shortfall"] == 0
    budgets = koban_budget_rows(live_budgets, abacuses, cards, now_dt=now_dt)
    reserved = sum(row["amount"] or 0 for row in budgets)
    return {
        "schema_version": PLANNING_SCHEMA_VERSION,
        "generated_at": now_ts,
        "today": today.isoformat(),
        "rate_window_days": RATE_WINDOW_DAYS,
        "event_rate_window_days": EVENT_RATE_WINDOW_DAYS,
        "rates": rates,
        "current": current,
        "koban_per_floor": floor_yield,
        "osaka_floor_speed": floor_speed,
        "resource_watch": {**normal_forge, "ten_forge": ten_forge},
        "client_inventory": client_items,
        "koban_watch": {
            "current": koban_current,
            "boxed_reserve": client_items.get("koban_reserve"),
            "total_with_boxes": (int(koban_current) + client_items["koban_reserve"] if koban_current is not None and client_items.get("koban_reserve") is not None else None),
            "reserved": reserved,
            "budgets": budgets,
            "available": (None if koban_current is None
                          else int(koban_current - reserved)),
            "confirmed_spending": confirmed_spending,
            "spending_days": RATE_WINDOW_DAYS,
        },
        "goals": goals,
        "events": abacuses,
        # 每种资源一张「在哪弄」途径卡：远征时薪动态算，日课/活动文案在数据文件
        "acquisition": {name: acquisition.resource_guide(name)
                        for name in LEDGER_RESOURCES},
        # 异去碎片途径卡 + 公共备注（掉率出处/里程碑/加倍活动），表单下拉也用这份
        "fragments": fragment_guides,
        "fragment_notes": acquisition.fragment_notes(
            now=now_dt, store=store,
            baseline_path=status_dir / "yosari_milestone.json"),
    }


# ── 活动算盘（江户城这类门票/钥匙活动的预算） ──

EVENTS_META_LOCAL = "events_meta.local.json"
_DATA_DIR = Path(__file__).resolve().parent / "data"


def load_event_cards(status_dir: Path) -> dict:
    """活动知识卡：仓库默认卡 + 数据目录本地覆盖（老大的场均预估存本地）。"""
    cards = {}
    try:
        cards = json.loads((_DATA_DIR / "events_meta.json")
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        local = json.loads((Path(status_dir) / EVENTS_META_LOCAL)
                           .read_text(encoding="utf-8"))
        for name, patch in local.items():
            if isinstance(patch, dict) and name in cards:
                cards[name].update(patch)
    except (OSError, ValueError):
        pass
    return cards


def save_key_estimate(status_dir: Path, event: str, keys_per_run) -> dict:
    """保存老大手填的场均钥匙预估（实测数据来了会被实测覆盖显示）。"""
    cards = load_event_cards(status_dir)
    if event not in cards:
        raise ValueError(f"不认识活动「{event}」")
    try:
        estimate = float(keys_per_run)
    except (TypeError, ValueError):
        raise ValueError("场均钥匙得是个数")
    if not 0 < estimate <= 200:
        raise ValueError("场均钥匙不对劲（大于 0，别超过 200）")
    path = Path(status_dir) / EVENTS_META_LOCAL
    try:
        local = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        local = {}
    local.setdefault(event, {})["est_keys_per_run"] = estimate
    # 估计按期次归档：复刻换新期时老估计不自动带过来（取值链会校验）
    from .event_history import period_key
    period = period_key(event, cards[event])
    if period:
        local[event]["est_period"] = period
    path.write_text(json.dumps(local, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return cards[event]


def save_currency_target(status_dir: Path, event: str, target, *,
                         mechanics: str = None) -> dict:
    """保存本期活动货币目标（玉/夜光贝同口径）；复刻换期后自动回默认满档。"""
    cards = load_event_cards(status_dir)
    card = cards.get(event)
    card_mechanics = (card or {}).get("mechanics")
    if (not card or card_mechanics not in CURRENCY_MECHANICS
            or (mechanics is not None and card_mechanics != mechanics)):
        raise ValueError(f"「{event}」不是可设置货币目标的活动")
    currency = card.get("currency") or "玉"
    if isinstance(target, bool):
        raise ValueError(f"目标{currency}数得是整数")
    try:
        currency_target = int(target)
    except (TypeError, ValueError):
        raise ValueError(f"目标{currency}数得是整数")
    if currency_target != target or not 1 <= currency_target <= 10_000_000:
        raise ValueError(f"目标{currency}数不对劲（1 到 10,000,000 之间的整数）")
    path = Path(status_dir) / EVENTS_META_LOCAL
    try:
        local = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        local = {}
    # 本地覆盖键两种机理共用（老档案里就叫 tama_target，不改名）
    local.setdefault(event, {})["tama_target"] = currency_target
    from .event_history import period_key
    period = period_key(event, card)
    if period:
        local[event]["tama_target_period"] = period
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(local, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return {**card, "tama_target": currency_target,
            "tama_target_period": period}


def save_hanafuda_tama_target(status_dir: Path, event: str, target) -> dict:
    """保存本期秘宝之里的玉目标；复刻换期后自动回到最高档默认值。"""
    return save_currency_target(status_dir, event, target,
                                mechanics="hanafuda")


# 一圈钥匙的合理上限：实测 9~37 把，OCR 读岔会产生 10157 这种垃圾值
MAX_PLAUSIBLE_KEYS_PER_RUN = 100


def _legacy_period_in_window(marker, name: str, start_dt, end_dt) -> bool:
    """老版本（2026-09-07 前）把期次标签打成「活动名@跑圈当天日期」，
    同名且日期落在本期窗口内的老标签仍算本期实测，别让数据白跑。"""
    prefix = f"{name}@"
    if not (isinstance(marker, str) and marker.startswith(prefix)):
        return False
    try:
        day = date.fromisoformat(marker[len(prefix):])
    except ValueError:
        return False
    if start_dt is None:
        return False
    upper = end_dt.date() if end_dt else date.max
    return start_dt.date() <= day <= upper


def measured_keys_per_run(store, *, name: str | None = None,
                          card: dict | None = None,
                          limit: int = 50) -> dict | None:
    """实测场均钥匙：edocastle.run_completed 事件（江户城流程产出）的平均。

    给了 name+card 就只算本期（复刻不串期）：payload 带 period 的按期次
    归属；老数据没 period 的，按事件时间落在卡的窗口内归属。
    """
    from .event_history import period_key
    events = store.recent_events(limit=limit, event_type="edocastle.run_completed")
    this_period = period_key(name, card) if name and card else None
    start_dt, end_dt, _ = _card_window(card) if card else (None, None, False)
    keys = []
    for event in events:
        payload = event.get("payload")
        if not (isinstance(payload, dict)
                and isinstance(payload.get("keys"), (int, float))
                and 0 < payload["keys"] <= MAX_PLAUSIBLE_KEYS_PER_RUN):
            continue
        if this_period:
            marker = payload.get("period")
            if marker:
                if marker != this_period and not _legacy_period_in_window(
                        marker, name, start_dt, end_dt):
                    continue  # 别期的实测
            elif start_dt is not None:
                ts = event.get("ts")
                upper = end_dt.timestamp() if end_dt else float("inf")
                if not isinstance(ts, (int, float)) \
                        or not (start_dt.timestamp() <= ts <= upper):
                    continue  # 老数据落在窗口外，不是本期
        keys.append(payload["keys"])
    if not keys:
        return None
    return {"per_run": sum(keys) / len(keys), "runs": len(keys),
            "keys_total": int(sum(keys))}


def resolve_keys_per_run(store, name: str, card: dict, periods: list[dict],
                         *, limit: int = 50) -> dict | None:
    """场均钥匙取值链：本期实测 > 规则相同的上期经验 > 玩家估计 > 没数。

    返回 {"per_run", "source", "basis", ...}，啥都没有返回 None。
    basis 是人话依据，前端原样展示，不许自己编理由。
    source: measured（本期实测）/ history（上期经验）/ estimate（手填估计）。
    """
    from .event_history import (find_matching_period, period_key,
                                rules_fingerprint)
    this_period = period_key(name, card)
    measured = measured_keys_per_run(store, name=name, card=card, limit=limit)
    if measured:
        return {**measured, "source": "measured", "period": this_period,
                "basis": f"本期实测 {measured['runs']} 圈平均出来的"}
    previous = find_matching_period(
        periods, name, rules_fingerprint(card),
        str(card.get("start_date") or "")[:10] or None)
    if previous and isinstance(previous.get("keys_per_run"), (int, float)):
        return {"per_run": float(previous["keys_per_run"]),
                "runs": previous.get("runs"),
                "source": "history",
                "period": f"{name}@{previous['start_date']}",
                "basis": (f"上期（{previous['start_date']}）打了 "
                          f"{previous.get('runs')} 圈，平均每圈 "
                          f"{previous['keys_per_run']:.1f} 把，规则相同，"
                          "先按这个估")}
    estimate = card.get("est_keys_per_run")
    if estimate:
        est_period = card.get("est_period")
        # 别的期次填的估计不带过来；没记期次的老估计默认是当期填的
        if not (est_period and this_period and est_period != this_period):
            return {"per_run": float(estimate), "source": "estimate",
                    "period": this_period, "basis": "你手填的临时估计"}
    return None


def _ticket_cap(card: dict) -> int:
    return (int(card.get("ticket_cap") or 0)
            or int(card.get("daily_free_tickets") or 0) // 2)


def _card_window(card: dict):
    """活动窗口：(start_dt, end_dt, precise)。优先精确时刻，退回日期按整天。"""
    try:
        if card.get("start_at") and card.get("end_at"):
            return (datetime.fromisoformat(str(card["start_at"])),
                    datetime.fromisoformat(str(card["end_at"])), True)
        if card.get("start_date") and card.get("end_date"):
            start = date.fromisoformat(str(card["start_date"]))
            end = date.fromisoformat(str(card["end_date"]))
            return (datetime.combine(start, datetime.min.time(), tzinfo=_TZ),
                    datetime.combine(end, datetime.max.time(), tzinfo=_TZ), False)
    except ValueError:
        pass
    return None, None, False


def osaka_period_income(store, card: dict, *, now: datetime) -> int | None:
    start, end, _ = _card_window(card)
    if start is None or end is None or not callable(getattr(store, "_conn", None)):
        return None
    lower, upper = start.timestamp(), min(end.timestamp(), now.timestamp())
    rows = store._conn().execute(
        "SELECT e.ts, e.event_type, e.payload, r.started_at FROM events e "
        "LEFT JOIN runs r ON r.run_id = e.run_id "
        "WHERE e.ts >= ? AND e.ts < ? AND e.event_type IN ('resource.change', 'osaka.koban_session')",
        (lower, upper)).fetchall()
    receipts = []
    sessions = []
    for row in rows:
        payload = json.loads(row["payload"])
        if row["event_type"] == "resource.change":
            if (payload.get("activity") == "osaka" and payload.get("resource") == "小判"
                    and payload.get("attribution") == "confirmed" and (payload.get("delta") or 0) > 0):
                receipts.append((row["ts"], payload["delta"]))
        elif payload.get("source") == "game_rewards":
            amount = payload.get("delta")
            if amount is not None and amount >= 0 and row["started_at"] is not None and row["started_at"] >= lower:
                sessions.append((row["started_at"], row["ts"], amount))
    total = sum(amount for _, amount in receipts)
    for started, ended, amount in sessions:
        covered = sum(value for ts, value in receipts if started <= ts <= ended)
        total += max(0, amount - covered)
    return int(total) if receipts or sessions else None


def latest_currency_total(store, card: dict, *, mechanics: str) -> dict | None:
    """取本期最近一次读到的活动货币累计数，绝不串到复刻活动。

    按事件时间戳取最新，不依赖 store 返回顺序。
    """
    total_field = CURRENCY_MECHANICS[mechanics]["total_field"]
    latest = None
    for ts, payload in _currency_period_events(store, card,
                                               mechanics=mechanics):
        total = payload.get(total_field)
        if isinstance(total, (int, float)) and total >= 0:
            latest = {"current": int(total), "observed_at": ts}
    return latest


def latest_hanafuda_tama(store, card: dict) -> dict | None:
    """本期秘宝之里最近一次读到的活动总玉数（花札专用口）。"""
    return latest_currency_total(store, card, mechanics="hanafuda")


def _currency_window_ts(card: dict) -> tuple[float | None, float | None,
                                             datetime | None, datetime | None]:
    """活动货币卡的活动窗口 → (start_ts, end_ts, start_dt, end_dt)。"""
    start_dt, end_dt, _ = _card_window(card)
    return ((start_dt.timestamp() if start_dt else None),
            (end_dt.timestamp() if end_dt else None),
            start_dt, end_dt)


def _currency_period_events(store, card: dict, *, mechanics: str,
                            limit: int = 100) -> list[tuple[float, dict]]:
    """本期窗口内的活动货币记录，按时间升序。只此一条数据源。"""
    start_ts, end_ts, _, _ = _currency_window_ts(card)
    event_type = CURRENCY_MECHANICS[mechanics]["event_type"]
    events = []
    for event in store.recent_events(limit=limit, event_type=event_type):
        ts = event.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if start_ts is not None and ts < start_ts:
            continue
        if end_ts is not None and ts > end_ts:
            continue
        payload = event.get("payload")
        events.append((float(ts), payload if isinstance(payload, dict) else {}))
    events.sort(key=lambda item: item[0])
    return events


def measured_currency_per_run(store, *, card: dict,
                              mechanics: str) -> dict | None:
    """实测场均货币：本期窗口内事件的增量字段平均。

    与 currency_plan 同一数据源、同一样本过滤（OCR 读岔的 0 和巨大值
    不进均值）；归属只看卡窗口，不串复刻期。归档要算全期总量，
    拉满 store 单查询上限，不吃默认 100 条的截断。没样本返回 None。
    """
    spec = CURRENCY_MECHANICS[mechanics]
    delta_field = spec["delta_field"]
    deltas = [float(payload[delta_field]) for _, payload
              in _currency_period_events(store, card, mechanics=mechanics,
                                         limit=1001)
              if isinstance(payload.get(delta_field), (int, float))
              and 0 < payload[delta_field] <= spec["loop_cap"]]
    if not deltas:
        return None
    return {"per_run": sum(deltas) / len(deltas), "runs": len(deltas),
            "currency_total": int(sum(deltas))}


def measured_tama_per_run(store, *, card: dict) -> dict | None:
    """实测场均玉（花札专用口）：与 hanafuda_plan 同源同样本过滤。"""
    measured = measured_currency_per_run(store, card=card,
                                         mechanics="hanafuda")
    if measured:
        measured["tama_total"] = measured.pop("currency_total")
    return measured


# 秘宝之里默认冲最高档 300,000 玉；玩家可以为当期改成自己的目标。
HANAFUDA_TAMA_TARGET = DEFAULT_CURRENCY_TARGET
# 一圈玉增量的可信范围：超难实测一圈约 300~1,000 玉；OCR 读岔会出 0 或
# 巨大值，超出 10 倍余量的样本不进均值
HANAFUDA_TAMA_LOOP_CAP = CURRENCY_MECHANICS["hanafuda"]["loop_cap"]
# 联队战一圈 10 场（可能带十倍鱼笼），夜光贝单圈上限放宽到 100,000：
# OCR 读岔的垃圾值照样被这个盖子挡在均值外
RAID_SHELLS_LOOP_CAP = CURRENCY_MECHANICS["raid"]["loop_cap"]
# 一圈时长的可信上限：图内监控 25 分钟就超时收圈，间隔再长多半是隔天再跑
HANAFUDA_LOOP_SECONDS_CAP = 1800
# 样本下限：达不到就老实说“再完成几圈后可估算”，绝不拿一两个样本外推
HANAFUDA_TAMA_MIN_SAMPLES = 3
def koban_budget_rows(live_budgets: list, abacuses: list, cards: dict,
                      *, now_dt: datetime) -> list[dict]:
    """补票预估与手动预算合并，同一活动取较高值，不重复预留。"""
    rows = []
    by_event = {}
    for goal in live_budgets:
        event = goal.get("event")
        if event and event in by_event:
            by_event[event]["amount"] += int(goal["target"])
            continue
        row = {"id": f"goal-{goal['id']}",
               "event": event or goal.get("note") or "活动预算",
               "amount": int(goal["target"]), "source": "manual"}
        rows.append(row)
        if event:
            by_event[event] = row
    for plan in abacuses:
        event = plan.get("event")
        card = cards.get(event) or {}
        if card.get("mechanics") != "raid":
            continue
        _, _, start, end = _currency_window_ts(card)
        if start is None or end is None or not start <= now_dt < end:
            continue
        cost = plan.get("koban_cost")
        if event in by_event:
            row = by_event[event]
            if cost is not None and cost > row["amount"]:
                row.update(amount=int(cost), source="estimated")
            continue
        if cost == 0:
            continue
        rows.append({"id": f"tickets-{event}", "event": event,
                     "amount": int(cost) if cost is not None else None,
                     "source": "estimated"})
    return rows


HANAFUDA_PACE_MIN_SAMPLES = 2


def _currency_target(card: dict) -> int:
    """取本期自定货币目标；跨期的本地覆盖不沿用。"""
    target = card.get("tama_target")
    if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
        return DEFAULT_CURRENCY_TARGET
    target_period = card.get("tama_target_period")
    if target_period:
        start = card.get("start_date") or str(card.get("start_at") or "")[:10]
        if not start or not str(target_period).endswith("@" + start):
            return DEFAULT_CURRENCY_TARGET
    return target


def _hanafuda_tama_target(card: dict) -> int:
    """花札口径的兼容口：老调用点/test 都在用。"""
    return _currency_target(card)


def currency_plan(store, card: dict, *, now_dt: datetime,
                  mechanics: str) -> dict:
    """代币档线型活动行动规划：默认冲最高档，也支持本期自定货币目标。

    数据源只有一条事件流，三件事同源、互不重复计数：total 取最近一次
    读到的活动累计（现状）；增量字段算场均（速率）；相邻记录的间隔算
    圈速（速率）。剩余圈数 = 剩余货币 ÷ 场均，剩余时间 = 圈数 × 圈速，
    补票 = 圈数 − 未来白票。输出键沿用 tama_* 命名（前端契约），
    另带 currency 供文案参数化。
    """
    spec = CURRENCY_MECHANICS[mechanics]
    delta_field = spec["delta_field"]
    _, _, start_dt, end_dt = _currency_window_ts(card)
    events = _currency_period_events(store, card, mechanics=mechanics)
    deltas = [float(payload[delta_field]) for _, payload in events
              if isinstance(payload.get(delta_field), (int, float))
              and 0 < payload[delta_field] <= spec["loop_cap"]]
    intervals = [later - earlier
                 for (earlier, _), (later, _) in zip(events, events[1:])
                 if 0 < later - earlier <= HANAFUDA_LOOP_SECONDS_CAP]
    total = latest_currency_total(store, card, mechanics=mechanics)
    target = _currency_target(card)
    plan = {
        "currency": card.get("currency") or "玉",
        "tama_target": target,
        "tama_target_custom": target != DEFAULT_CURRENCY_TARGET,
        "tama_current": total["current"] if total else None,
        "tama_observed_at": total["observed_at"] if total else None,
        "tama_remaining": (max(0, target - total["current"])
                           if total else None),
        "tama_per_loop": None,
        "tama_samples": len(deltas),
        "runs_needed": None,
        "seconds_per_loop": None,
        "estimated_seconds": None,
        "seconds_to_end": None,
        "can_finish": None,
        "free_tickets_remaining": None,
        "paid_tickets": None,
        "koban_cost": None,
    }
    if end_dt is not None:
        plan["seconds_to_end"] = max(
            0, int(end_dt.timestamp() - now_dt.timestamp()))
    if len(deltas) >= HANAFUDA_TAMA_MIN_SAMPLES:
        plan["tama_per_loop"] = round(sum(deltas) / len(deltas), 1)
    if len(intervals) >= HANAFUDA_PACE_MIN_SAMPLES:
        plan["seconds_per_loop"] = round(sum(intervals) / len(intervals))
    if end_dt is not None:
        plan["free_tickets_remaining"] = _remaining_free_tickets(
            card, start_dt, end_dt, now_dt)
    if plan["tama_per_loop"] is None or plan["tama_current"] is None:
        return plan  # 样本不够，或当前累计还没读到——不硬算
    import math
    remaining = max(0, target - plan["tama_current"])
    plan["runs_needed"] = math.ceil(
        remaining / plan["tama_per_loop"]) if remaining else 0
    if plan["runs_needed"] and plan["seconds_per_loop"]:
        plan["estimated_seconds"] = (plan["runs_needed"]
                                     * plan["seconds_per_loop"])
        if plan["seconds_to_end"] is not None:
            plan["can_finish"] = (plan["estimated_seconds"]
                                  <= plan["seconds_to_end"])
    free = plan["free_tickets_remaining"]
    if free is not None:
        plan["paid_tickets"] = max(0, plan["runs_needed"] - free)
        # 卡上没写票价（未核实）就不给小判估算，前端也不展示预算块
        price = card.get("ticket_price")
        if price:
            plan["koban_cost"] = plan["paid_tickets"] * int(price)
    return plan


def hanafuda_plan(store, card: dict, *, now_dt: datetime) -> dict:
    """秘宝之里行动规划 = currency_plan 的花札口（输出字段名不变）。"""
    return currency_plan(store, card, now_dt=now_dt, mechanics="hanafuda")


def _remaining_free_tickets(card: dict, start_dt: datetime | None,
                            end_dt: datetime, now_dt: datetime) -> int | None:
    """从现在到收摊还能免费领多少张通行手形（回票点 × 每次回票数）。

    回满制：每个回票点把手形回到可出阵的水平，按“每张都及时用掉”估算。
    手上现存的票脚本读不到，不计入；活动还没开场时只算开场所持的一手。
    卡里没写回票规则（refill_amount）就返回 None，让前端别展示。
    """
    refill_amount = card.get("refill_amount")
    if refill_amount is None:
        return None
    refill_amount = int(refill_amount)
    hours = card.get("refill_hours") or [5, 17]
    count = 0
    day = (start_dt or now_dt).date() - timedelta(days=1)
    while day <= end_dt.date():
        for hour in hours:
            refill = (datetime.combine(day, datetime.min.time(), tzinfo=_TZ)
                      + timedelta(hours=hour))
            if start_dt is not None and refill <= start_dt:
                continue
            if refill <= now_dt or refill >= end_dt:
                continue
            count += 1
        day += timedelta(days=1)
    free = count * refill_amount
    if start_dt is not None and now_dt < start_dt:
        free += refill_amount  # 开场所持的一手
    return free


def _count_free_tickets(card: dict, start_dt: datetime, end_dt: datetime) -> int:
    """按活动窗口计算可免费获取的令牌数。

    新模型（卡里有 refill_amount）：开场满额 cap 枚，之后每次回票点
    严格落在活动窗口 (start_dt, end_dt) 内时加 refill_amount 枚；
    收摊时刻及之后的回票不计，开场前回票不计。

    老模型（无 refill_amount）：维持原 12 小时回满窗口交集逻辑。
    """
    cap = _ticket_cap(card)
    if not cap:
        return 0
    hours = card.get("refill_hours") or [5, 17]

    refill_amount = card.get("refill_amount")
    if refill_amount is not None:
        # 新模型：cap + 严格在窗口内的回票点 × refill_amount
        refill_amount = int(refill_amount)
        count = 0
        day = start_dt.date() - timedelta(days=1)
        while day <= end_dt.date():
            for hour in hours:
                refill = (datetime.combine(day, datetime.min.time(), tzinfo=_TZ)
                          + timedelta(hours=hour))
                if start_dt < refill < end_dt:
                    count += 1
            day += timedelta(days=1)
        return cap + count * refill_amount

    # 老模型（无 refill_amount）：维持原 12 小时回满窗口交集逻辑
    count = 0
    day = start_dt.date() - timedelta(days=1)
    while day <= end_dt.date():
        for hour in hours:
            refill = (datetime.combine(day, datetime.min.time(), tzinfo=_TZ)
                      + timedelta(hours=hour))
            if min(refill + timedelta(hours=12), end_dt) > max(refill, start_dt):
                count += 1
        day += timedelta(days=1)
    return count * cap


def event_abacus(name: str, card: dict, *, measured: dict | None,
                 today: date | None = None, now: datetime | None = None) -> dict:
    """江户城式算盘：钥匙总量 ÷ 场均 = 圈数 → 扣白票 → 补票钱。

    实测场均优先，其次老大手填预估，都没有就老实说还没数。
    """
    now_dt = _resolve_now(today, now)
    today = today or now_dt.date()
    keys_total = int(card.get("keys_total") or 0)
    ticket_price = int(card.get("ticket_price") or 0)
    daily_free = int(card.get("daily_free_tickets") or 0)
    abacus = {
        "event": name,
        "goal_mode": ("stock_target"
                      if card.get("mechanics") == "osaka" else "budget"),
        "goal_resource": "小判",
        "start_date": card.get("start_date"),
        "end_date": card.get("end_date"),
        "keys_total": keys_total,
        "boxes": card.get("boxes"),
        "ticket_price": ticket_price,
        "daily_free_tickets": daily_free,
        "note": card.get("note") or "",
        "keys_per_run": None,
        "keys_source": None,
        "keys_obtained": None,
        "keys_remaining": None,
        "runs_total": None,
        "runs_needed": None,
        "free_runs": None,
        "paid_tickets": None,
        "koban_cost": None,
        "days_left": None,
        "message": "",
    }

    if card.get("mechanics") == "osaka":
        abacus["message"] = (
            "大阪城会持续产小判。给收摊时的家底定个数，狐之助会按最近"
            "实测换算还要挖多少层。")
        return abacus

    if card.get("mechanics") not in (None, "edocastle"):
        abacus["message"] = (card.get("planning_message")
                              or "这张活动卡还缺数据，回头补。")
        return abacus

    if measured:
        keys_per_run = measured["per_run"]
        source = measured.get("source") or "measured"
    elif card.get("est_keys_per_run"):
        keys_per_run, source = float(card["est_keys_per_run"]), "estimate"
    else:
        keys_per_run, source = None, None
    abacus["keys_per_run"] = round(keys_per_run, 1) if keys_per_run else None
    abacus["keys_source"] = source
    abacus["keys_basis"] = (measured or {}).get("basis")  # 取值依据（人话）

    if not keys_total or not ticket_price:
        abacus["message"] = "这张活动卡还缺数据，回头补。"
        return abacus
    if keys_per_run is None:
        abacus["message"] = (
            f"四座宝库全开要 {keys_total:,} 把钥匙（{card.get('boxes')} 箱 × "
            f"{card.get('keys_per_box')} 把）。场均几把还没数——你可以先填个估计，"
            "或者等开战狐之助实测几圈就会算了。")
        return abacus

    import math
    # 本期实测带已拿钥匙总数：圈数/补票按「还差多少」算，不按满额虚高
    # （2026-09-08 老大气笑了现场：打了 46 圈拿 899 把，卡上还按 1500 把算）
    obtained = None
    if source == "measured":
        raw_obtained = (measured or {}).get("keys_total")
        if isinstance(raw_obtained, (int, float)) and raw_obtained > 0:
            obtained = int(raw_obtained)
    abacus["keys_obtained"] = obtained
    remaining = max(0, keys_total - obtained) if obtained else keys_total
    abacus["keys_remaining"] = remaining
    runs_total = math.ceil(keys_total / keys_per_run)
    abacus["runs_total"] = runs_total
    runs = math.ceil(remaining / keys_per_run)
    abacus["runs_needed"] = runs
    label = {"measured": "实测", "history": "上期经验",
             "estimate": "估计"}.get(source, "估计")
    if obtained:
        progress = f"已拿 {obtained:,} 把，还差 {remaining:,} 把——"
    else:
        progress = ""

    start_dt, end_dt, precise = _card_window(card)
    if end_dt is None:
        # 不知道结束日：算不出白票能顶多少，给全自费上限
        abacus["koban_cost"] = runs * ticket_price
        abacus["message"] = (
            f"{progress}按{label}场均 {keys_per_run:.1f} 把钥匙，"
            f"全开四座宝库还要打 {runs} 圈。门票 {ticket_price} 小判/张，"
            f"全自费最坏 {runs * ticket_price:,} 小判；每天白送 {daily_free} 张票，"
            "排进日常就能省一大截。等结束日定了狐之助再算细账。")
        return abacus

    effective_start = max(start_dt, now_dt) if start_dt else now_dt
    days_left = max((end_dt.date() - effective_start.date()).days + 1, 0)
    abacus["days_left"] = days_left
    if precise:
        free_runs = _count_free_tickets(card, effective_start, end_dt)
        refill_amount = card.get("refill_amount")
        if refill_amount is not None:
            # 获得制：开场满额 + 每个回票点加 refill_amount 枚（不是回满）
            refill_times = (free_runs - _ticket_cap(card)) // int(refill_amount)
            free_desc = (f"开场满 {_ticket_cap(card)} 枚 + "
                         f"回票 {refill_times} 次 × {int(refill_amount)} 枚")
        else:
            free_desc = f"回满 {free_runs // _ticket_cap(card)} 次 × {_ticket_cap(card)} 枚"
    else:
        free_runs = daily_free * days_left
        free_desc = f"每天 {daily_free} 张 × {days_left} 天"
    abacus["free_runs"] = free_runs
    paid = max(0, runs - free_runs)
    abacus["paid_tickets"] = paid
    abacus["koban_cost"] = paid * ticket_price
    end_label = card.get("end_at") or card["end_date"]
    if precise and isinstance(end_label, str) and "T" in end_label:
        end_label = end_label.replace("T", " ")[:16]
    if remaining == 0:
        abacus["message"] = (
            f"四座宝库全开啦🎉 已拿 {obtained:,} 把，剩下的票留着也没处花，"
            "想刷宝库奖励可以继续，不想刷就收工。")
    elif paid == 0:
        abacus["message"] = (
            f"{progress}按{label}场均 {keys_per_run:.1f} 把，再打 {runs} 圈全开——"
            f"到 {end_label} 的白票（{free_desc}）就够用了，"
            "一个小判都不用花🎉")
    else:
        abacus["message"] = (
            f"{progress}按{label}场均 {keys_per_run:.1f} 把，再打 {runs} 圈全开；"
            f"到 {end_label}（还剩 {days_left} 天）白票能顶 "
            f"{free_runs} 圈，还得补 {paid} 张票 ≈ {paid * ticket_price:,} 小判。")
    return abacus


def window_impact(name: str, card: dict, *, measured_keys: dict | None = None,
                  floor_yield: dict | None = None,
                  today: date | None = None,
                  now: datetime | None = None) -> dict | None:
    """按知识卡机理算活动窗口的资源净影响（§25 活动感知规划）。

    返回 {"resource", "delta", "detail"} 或 None（没模型/缺数据/没日期）。
    delta 正数 = 活动净赚，负数 = 活动净花（门票钱）。没数据时返回 None，
    让目标评估退回速率兜底——宁可少说，不许瞎编。
    """
    now_dt = _resolve_now(today, now)
    today = today or now_dt.date()
    mechanics = card.get("mechanics")
    if mechanics == "edocastle":
        abacus = event_abacus(name, card, measured=measured_keys, today=today,
                              now=now_dt)
        cost = abacus.get("koban_cost")
        if cost is None:
            return None
        return {"resource": "小判", "delta": -int(cost),
                "detail": f"{name}门票钱"}
    if mechanics == "osaka":
        per_floor = (floor_yield or {}).get("per_floor")
        if not per_floor:
            return None
        try:
            start = date.fromisoformat(str(card.get("start_date")))
            end = date.fromisoformat(str(card.get("end_date")))
        except ValueError:
            return None
        days = max((end - max(today, start)).days + 1, 0)
        if not days:
            return None
        hours = float(card.get("hours_per_night") or 6)
        lap_min = float(card.get("lap_minutes") or 5)
        if lap_min <= 0:
            return None
        floors = hours * 60 / lap_min * days
        return {"resource": "小判", "delta": int(round(floors * per_floor)),
                "detail": f"{name}挂机 {days} 晚"}
    return None
