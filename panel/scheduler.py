# -*- coding: utf-8 -*-
"""远征计划：常用安排、攻略预设与自定义时刻表。"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from touken.runtime_paths import SCHEDULE_PATH, STATE_DIR
from .expedition_choices import (adhoc_planned_date, is_adhoc_record,
                                 is_forced, is_skipped, load_choice_sets)

_SCHED_PATH = SCHEDULE_PATH
_MAPS_PATH = (Path(__file__).resolve().parent.parent
              / "touken" / "data" / "expedition_maps.json")

TEAM_NAMES = {1: "部队一", 2: "部队二", 3: "部队三", 4: "部队四", 5: "部队五"}


# —— 班次状态机常量 ——
SLOT_WAITING_BUSY = "waiting_busy"        # 到点但自家任务（runner）在跑
SLOT_WAITING_UNKNOWN = "waiting_unknown"  # runner 空闲但门卫没过/游戏没跑/上次没确认
SLOT_READY = "ready"                      # 即将接管（15 秒预告窗口）
SLOT_DISPATCHED = "dispatched"            # 已确认派出（终态）
SLOT_EXPIRED = "expired"                  # 超过允许延迟，明确跳过（终态）
SLOT_FAILED = "failed_unknown"            # 连续 3 次无法确认结果（终态）
TERMINAL_STATES = {SLOT_DISPATCHED, SLOT_EXPIRED, SLOT_FAILED}

TAKEOVER_PREVIEW_SEC = 15     # ready 预告窗口
RETRY_AFTER_SEC = 300         # 门卫拒绝/结果未确认后的冷却
MAX_DISPATCH_ATTEMPTS = 3     # 连续这么多次确认不了就 failed_unknown
TAKEOVER_FLAG_NAME = "expedition_takeover.json"
DISPATCH_RESULT_NAME = "dispatch_result.json"


def _max_delay_min(auto: dict) -> int:
    try:
        return max(1, int(auto.get("max_delay_min", 30)))
    except (TypeError, ValueError):
        return 30


def _grace_min(auto: dict) -> int:
    """到点窗口：普通 = max_delay_min；资本家 = 4 倍硬封顶。"""
    base = _max_delay_min(auto)
    return base * 4 if auto.get("capitalist", False) else base


PRESET_TOTALS = {
    "木炭": "木炭 3195 · 玉钢 1860 · 冷却材 2340 · 砥石 2640 · 小判 2500 · 委托符 13",
    "玉钢": "木炭 1710 · 玉钢 4995 · 冷却材 1125 · 砥石 1830 · 小判 1600 · 加速符 3/8",
    "冷却材": "木炭 2385 · 玉钢 1995 · 冷却材 6615 · 砥石 1620 · 小判 2000 · 委托符 13 · 加速符 8",
    "砥石": "木炭 825 · 玉钢 2025 · 冷却材 270 · 砥石 6375 · 小判 3900 · 加速符 3/1",
    "小判": "木炭 1425 · 玉钢 1860 · 冷却材 2100 · 砥石 3900 · 小判 6100",
    "加速符": "木炭 960 · 玉钢 3435 · 冷却材 1185 · 砥石 4395 · 小判 700 · 加速符 15 · 委托符 2",
    "委托符": "木炭 2745 · 玉钢 2580 · 冷却材 3240 · 砥石 1980 · 小判 1100 · 委托符 19 · 加速符 1",
}

# 每条 lane 是从开始时间起依次执行的地图。地图时长来自 expedition_maps.json。
PRESETS = {
    "木炭": [
        ["B3"] * 8 + ["D4"],
        ["C1"] * 4 + ["C4"],
        ["D4", "D4", "D1", "C3"],
    ],
    "玉钢": [
        ["D2", "D2", "D2", "A4", "C4"],
        ["B4"] * 5 + ["A4", "C3"],
        ["C1", "C1", "C1", "E1"],
    ],
    "冷却材": [
        ["B1"] * 8 + ["E1"],
        ["B3"] * 7 + ["C3"],
        ["D1"] * 6 + ["D3"],
    ],
    "砥石": [
        ["B2"] * 5 + ["A4", "D4"],
        ["D4", "D4", "A4", "B1", "B1", "D2"],
        ["C4", "C4", "C4"],
    ],
    "小判": [
        ["D4", "D4", "B2", "D4"],
        ["D2", "B2", "D2", "C3"],
        ["B1"] * 10 + ["D2"],
    ],
    "加速符": [
        ["B4"] * 6 + ["C4"],
        ["C2"] * 5 + ["A4", "D4"],
        ["C4", "B3", "B3", "E1"],
    ],
    "委托符": [
        ["B3"] * 8 + ["C4"],
        ["D1"] * 7 + ["C3"],
        ["C1"] * 4 + ["D4"],
    ],
}


def _defaults() -> dict:
    return {
        "version": 2,
        "common_plan": [
            {"team_no": n, "map_code": "E2", "enabled": False}
            for n in range(1, 6)
        ],
        "automation": {
            "enabled": False,
            "mode": "preset",
            "preset": "小判",
            "start_time": "08:00",
            "teams": [2, 3, 4],
            "capitalist": False,
            "sakura_before_dispatch": False,
            "paused_until": "",
            # 一班最多允许晚多少分钟（过期就明确跳过，不突然补跑）；
            # 资本家模式的补跑窗口硬封顶是它的 4 倍。
            "max_delay_min": 30,
            "last_runs": {},
            "lane_shifts": {},
            # 每班持久化状态机：key 同 last_runs；terminal 状态
            # （dispatched/expired/failed_unknown）重启后绝不重复派。
            "slot_states": {},
        },
        "entries": [],
    }


def load_config() -> dict:
    cfg = _defaults()
    try:
        if _SCHED_PATH.exists():
            raw = json.loads(_SCHED_PATH.read_text(encoding="utf-8"))
            # 兼容旧文件 {"entries": [...]}
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in cfg})
                if isinstance(raw.get("automation"), dict):
                    merged = _defaults()["automation"]
                    merged.update(raw["automation"])
                    cfg["automation"] = merged
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    _SCHED_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def load_entries() -> list:
    return list(load_config().get("entries", []))


def save_entries(entries: list):
    cfg = load_config()
    cfg["entries"] = entries
    save_config(cfg)


def map_options() -> list:
    try:
        d = json.loads(_MAPS_PATH.read_text(encoding="utf-8"))
        out = []
        for code, m in d.get("maps", {}).items():
            dur = int(m.get("duration_min", 0))
            out.append({
                "code": code, "era": m["era"], "slot": m["slot"],
                "name": m.get("name") or code, "duration_min": dur,
                "duration_text": f"{dur // 60}h{dur % 60:02d}分" if dur >= 60 else f"{dur}分",
            })
        out.sort(key=lambda x: (x["era"], x["slot"]))
        return out
    except Exception:
        return []


def find_map(code: str):
    return next((m for m in map_options() if m["code"] == code), None)


def preset_payload() -> dict:
    maps = {m["code"]: m for m in map_options()}
    result = {}
    for name, lanes in PRESETS.items():
        rendered = []
        for lane in lanes:
            minute = 0
            parts = []
            for code in lane:
                m = maps.get(code)
                if not m:
                    continue
                parts.append({"offset_min": minute, "map_code": code,
                              "duration_min": m["duration_min"]})
                minute += m["duration_min"]
            rendered.append(parts)
        result[name] = {"lanes": rendered, "totals": PRESET_TOTALS.get(name, "")}
    return result


def _minute_of_day(hhmm: str) -> int:
    try:
        h, m = map(int, hhmm.split(":"))
        return h * 60 + m
    except Exception:
        return 480


def _emulator_ready(config_path: str) -> bool:
    """只在游戏进程仍在时接管；退出游戏或关模拟器后不主动启动。"""
    try:
        from touken.emulator import _run
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        result = _run([cfg.get("adb_path", ""), "-s", cfg.get("adb_address", ""),
                       "shell", "pidof", cfg.get("daily", {}).get("logout", {}).get("package", "com.youzu.djlw")], timeout=5)
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def _preset_due(cfg: dict, now_min: int, today: str,
                choices: dict | None = None, forced: dict | None = None) -> list:
    auto = cfg["automation"]
    preset = preset_payload().get(auto.get("preset"), {})
    lanes = preset.get("lanes", [])
    teams = auto.get("teams", [2, 3, 4])
    start = _minute_of_day(auto.get("start_time", "08:00"))
    elapsed = (now_min - start) % 1440
    cycle_day = datetime.now().date()
    if now_min < start:
        cycle_day -= timedelta(days=1)
    cycle_label = cycle_day.isoformat()
    cycle_base = datetime.combine(cycle_day, datetime.min.time())
    grace = _grace_min(auto)
    choices = choices or {}
    out = []
    for idx, lane in enumerate(lanes[:3]):
        if idx >= len(teams):
            continue
        shift_key = f"{cycle_label}:lane:{idx}"
        shift = int(auto.get("lane_shifts", {}).get(shift_key, 0))
        current = None
        for part in lane:
            actual_offset = part["offset_min"] + shift
            if actual_offset <= elapsed < actual_offset + part["duration_min"]:
                current = part
                break
        if not current:
            continue
        late = elapsed - (current["offset_min"] + shift)
        if late > grace:
            continue
        key = f"{cycle_label}:preset:{idx}:{current['offset_min']}"
        if auto.get("last_runs", {}).get(key):
            continue
        planned = cycle_base + timedelta(minutes=start + current["offset_min"] + shift)
        skipped_today = is_skipped(
            choices, key=key, team_no=int(teams[idx]),
            map_code=current["map_code"], planned_at=planned.timestamp())
        forced_today = is_forced(
            forced or {}, key=key, team_no=int(teams[idx]),
            map_code=current["map_code"])
        if skipped_today and not forced_today:
            continue
        out.append({"key": key, "team_no": int(teams[idx]),
                    "map_code": current["map_code"], "late_min": late,
                    "shift_key": shift_key,
                    "planned_at": planned.isoformat(timespec="seconds"),
                    "apply_shift": late if auto.get("capitalist") and not shift and late > grace else 0})
    return out


def _custom_due(cfg: dict, now_min: int, today: str,
                choices: dict | None = None, forced: dict | None = None) -> list:
    auto = cfg["automation"]
    grace = _grace_min(auto)
    choices = choices or {}
    out = []
    latest = {}
    for idx, e in enumerate(cfg.get("entries", [])):
        if e.get("enabled", True) and _minute_of_day(e.get("time", "08:00")) <= now_min:
            team = int(e.get("team_no", 2))
            if team not in latest or e.get("time", "08:00") >= latest[team][1].get("time", "08:00"):
                latest[team] = (idx, e)
    for idx, e in latest.values():
        if not e.get("enabled", True):
            continue
        due = _minute_of_day(e.get("time", "08:00"))
        if now_min < due:
            continue
        late = (now_min - due) % 1440
        if late > grace:
            continue
        key = f"{today}:custom:{idx}:{e.get('time')}"
        if auto.get("last_runs", {}).get(key):
            continue
        planned_at = (datetime.combine(datetime.fromisoformat(today).date(),
                                       datetime.min.time())
                      + timedelta(minutes=due)).timestamp()
        team_no = int(e.get("team_no", 2))
        if is_skipped(choices, key=key, team_no=team_no,
                      map_code=e.get("map_code", ""), planned_at=planned_at) \
                and not is_forced(forced or {}, key=key, team_no=team_no,
                                  map_code=e.get("map_code", "")):
            continue
        out.append({"key": key, "team_no": int(e.get("team_no", 2)),
                    "map_code": e.get("map_code", ""), "late_min": late,
                    "planned_at": f"{today}T{e.get('time', '08:00')}:00"})
    return out


def forced_only(due: list, forced: dict) -> list:
    """排班总开关关着时，只有今日强制启用的班还照常到期。"""
    return [job for job in due
            if is_forced(forced or {}, key=job["key"],
                         team_no=int(job["team_no"]), map_code=job["map_code"])]


def adhoc_due(cfg: dict, forced: dict, now: float, today: str) -> list:
    """自描述 forced 班（建议引擎采纳的班）的到期产出。

    不引用任何 preset/custom 条目：队伍/图/计划时刻全在 forced 记录里。
    键里编了日期，昨天的残留自动不参与；宽限口径与排班一致
    （max_delay_min，资本家 4 倍）。
    """
    auto = cfg["automation"]
    grace = _grace_min(auto)
    last_runs = auto.get("last_runs", {})
    out = []
    for key, record in (forced or {}).items():
        if not is_adhoc_record(record):
            continue
        if adhoc_planned_date(record) != today:
            continue
        raw_planned = record.get("planned_at")
        try:
            planned_ts = (datetime.fromisoformat(raw_planned).timestamp()
                          if isinstance(raw_planned, str)
                          else float(raw_planned))
            team_no = int(record.get("team_no"))
            map_code = str(record.get("map_code") or "")
        except (TypeError, ValueError):
            continue
        if not map_code:
            continue
        late = (now - planned_ts) / 60
        if late < 0 or late > grace:
            continue
        if last_runs.get(key):
            continue
        out.append({"key": key, "team_no": team_no, "map_code": map_code,
                    "late_min": int(late),
                    "planned_at": str(record["planned_at"]),
                    **{key: record[key] for key in ("formation_id", "formation_name", "formation_signature") if record.get(key)}})
    return out


def managed_teams(cfg=None):
    cfg = load_config() if cfg is None else cfg
    auto = cfg.get("automation", {})
    if not auto.get("enabled"):
        return set()
    if auto.get("mode") == "preset":
        return {int(t) for t in auto.get("teams", [2, 3, 4])}
    return {int(e.get("team_no", 2)) for e in cfg.get("entries", [])
            if e.get("enabled", True)}


def expedition_records():
    try:
        return json.loads((STATE_DIR / "expeditions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def team_available(team, records, now):
    record = records.get(str(team), {})
    try:
        end = time.mktime(time.strptime(record["dispatched_at"], "%Y-%m-%d %H:%M:%S"))
        end += int(record["duration_min"]) * 60
        return now >= end
    except (KeyError, TypeError, ValueError):
        return True


class DeferredDispatches:
    """Keep one outstanding departure per team while the shared runner is busy."""
    def __init__(self):
        self.jobs = {}
        self.signature = None

    def update(self, cfg, due, now):
        auto = cfg["automation"]
        signature = json.dumps([auto.get(k) for k in
            ("enabled", "mode", "preset", "start_time", "teams", "paused_until")]
            + [cfg.get("entries", [])], sort_keys=True)
        if signature != self.signature:
            self.jobs.clear()
            self.signature = signature
        for job in due:
            team = job["team_no"]
            if team not in self.jobs:
                self.jobs[team] = {**job, "observed_at": now}
            elif auto.get("mode") == "custom" and job["key"] != self.jobs[team]["key"]:
                # Custom times describe the latest intention, not a backlog to replay.
                self.jobs[team] = {**job, "observed_at": now}
        return list(self.jobs.values())


def record_completed_dispatch(cfg, job, before, records, now):
    record = records.get(str(job["team_no"]), {})
    if (record.get("map_code") != job["map_code"] or not record.get("dispatched_at")
            or record.get("dispatched_at") == before):
        return False
    auto = cfg["automation"]
    auto.setdefault("last_runs", {})[job["key"]] = record["dispatched_at"]
    if job.get("shift_key"):
        delay = job["late_min"] + int((now - job["observed_at"]) / 60)
        shifts = auto.setdefault("lane_shifts", {})
        shifts[job["shift_key"]] = shifts.get(job["shift_key"], 0) + delay
    return True


# ==================== 班次状态机 ====================
# 每班持久化在 automation.slot_states（key 同 last_runs），每 tick 由 tick()
# 纯函数推进；dispatched/expired/failed_unknown 是持久终态，重启绝不重复派。
# 播报话术红线：正常播报逐条过 report_judge 翻车词表不判红（测试钉死）。

def _planned_ts(slot: dict) -> float:
    try:
        return datetime.fromisoformat(str(slot.get("planned_at", ""))).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _new_slot(job: dict, cfg: dict, now: float) -> dict:
    planned_at = str(job.get("planned_at")
                     or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)))
    try:
        planned_ts = datetime.fromisoformat(planned_at).timestamp()
    except ValueError:
        planned_ts = now
    return {
        "key": job["key"], "team_no": job["team_no"], "map_code": job["map_code"],
        "planned_at": planned_at, "state": SLOT_WAITING_UNKNOWN,
        "blocked_reason": "", "next_retry_at": 0.0,
        "expires_at": planned_ts + _grace_min(cfg["automation"]) * 60,
        "attempts": 0, "dispatched_at": "",
        # 资本家顺延（record_completed_dispatch）需要的班次上下文
        "late_min": job.get("late_min", 0), "shift_key": job.get("shift_key"),
        "observed_at": now,
        **{key: job[key] for key in ("formation_id", "formation_name", "formation_signature") if job.get(key)},
    }


def _slot_label(slot: dict) -> str:
    try:
        team = TEAM_NAMES.get(int(slot.get("team_no", 0)), f"部队{slot.get('team_no')}")
    except (TypeError, ValueError):
        team = "部队?"
    return f"{team} → {slot.get('map_code', '?')}"


def _slot_confirmed_at(slot: dict, records: dict):
    """expeditions.json 里出现了这班计划时间之后的同图派遣 → 返回 dispatched_at。"""
    record = records.get(str(slot.get("team_no", "")), {})
    if record.get("map_code") != slot.get("map_code"):
        return None
    try:
        ts = time.mktime(time.strptime(record["dispatched_at"], "%Y-%m-%d %H:%M:%S"))
    except (KeyError, TypeError, ValueError):
        return None
    planned = _planned_ts(slot)
    if planned and ts < planned - 60:
        return None
    return record["dispatched_at"]


def _finalize_dispatched(cfg: dict, slot: dict, records: dict, now: float,
                         before=None) -> bool:
    """确认派出：终态 + last_runs +（资本家）顺延后续班次。"""
    job = {"key": slot["key"], "team_no": slot["team_no"],
           "map_code": slot["map_code"], "late_min": slot.get("late_min", 0),
           "shift_key": slot.get("shift_key"),
           "observed_at": slot.get("observed_at", now)}
    if not record_completed_dispatch(cfg, job, before, records, now):
        return False
    slot["state"] = SLOT_DISPATCHED
    slot["dispatched_at"] = cfg["automation"]["last_runs"][slot["key"]]
    slot["blocked_reason"] = ""
    return True


def _msg_dispatched(slot: dict, now: float) -> str:
    planned = _planned_ts(slot)
    late = max(0, int((now - planned) / 60)) if planned else 0
    suffix = f"（比计划晚了 {late} 分钟）" if late >= 1 else ""
    return f"[排班] ✓ {_slot_label(slot)} 已确认派出{suffix}"


def resolve_inflight(cfg: dict, slot_key: str, before, now: float,
                     records: dict, result: dict | None):
    """派遣子进程跑完后的结算。result = dispatch_result.json 内容或 None。
    返回 (events, changed)；只动 cfg，不落盘不播报。"""
    slot = cfg["automation"].get("slot_states", {}).get(slot_key)
    if not slot or slot.get("state") in TERMINAL_STATES:
        return [], False
    if (isinstance(result, dict) and result.get("key") == slot_key
            and result.get("outcome") == "failed"):
        detail = str(result.get("detail") or "派遣流程提前停止，原因没有读清")
        slot["state"] = SLOT_FAILED
        slot["blocked_reason"] = detail
        slot["attempts"] = int(slot.get("attempts", 0)) + 1
        slot["next_retry_at"] = 0
        return [(slot_key, f"[排班] ✗ {_slot_label(slot)} 派遣已停止：{detail}。"
                           "这班不再自动重试，请检查队伍条件")], True
    if (isinstance(result, dict) and result.get("key") == slot_key
            and result.get("outcome") == "refused"):
        # 接管门卫拒了：画面多半主人在手动玩。冷却后重试，不猛试；
        # 门卫没动手点屏，不算一次派遣尝试（attempts 不加）。
        slot["state"] = SLOT_WAITING_UNKNOWN
        slot["blocked_reason"] = str(
            result.get("detail") or "画面不在本丸，像主人在手动玩")
        slot["next_retry_at"] = now + RETRY_AFTER_SEC
        return [(slot_key, f"[排班] {_slot_label(slot)} 这班先交回排班："
                           f"{slot['blocked_reason']}，5 分钟后再来看")], True
    if _finalize_dispatched(cfg, slot, records, now, before=before):
        return [(slot_key, _msg_dispatched(slot, now))], True
    slot["attempts"] = int(slot.get("attempts", 0)) + 1
    if slot["attempts"] >= MAX_DISPATCH_ATTEMPTS:
        slot["state"] = SLOT_FAILED
        slot["blocked_reason"] = "连续 3 次无法确认派遣结果"
        return [(slot_key, f"[排班] ✗ {_slot_label(slot)} 试了 "
                           f"{MAX_DISPATCH_ATTEMPTS} 次都无法确认远征派出没有，"
                           "这班标记为「未确认」，请去看看游戏画面")], True
    slot["state"] = SLOT_WAITING_UNKNOWN
    slot["blocked_reason"] = "派遣结果还没确认到"
    slot["next_retry_at"] = now + RETRY_AFTER_SEC
    return [(slot_key, f"[排班] {_slot_label(slot)} 的派遣结果还没确认到，"
                       "5 分钟后再试一次")], True


def tick(cfg: dict, due: list, now: float, *, runner_busy: bool,
         emulator_ok: bool, records: dict, inflight_key: str | None = None):
    """推进全部班次状态机一格（纯函数风格：只改 cfg，返回要播报/要起的事）。

    返回 {"events": [(key, msg)], "start": slot|None, "changed": bool}。
    转移顺序：记录确认 > 过期 > runner 忙 > 游戏没跑 > 部队没回 > 冷却中 > ready。
    """
    auto = cfg["automation"]
    slots = auto.setdefault("slot_states", {})
    events = []
    changed = False
    to_start = None

    # 新到点的班建档；已有记录（含终态）绝不重建——重启不重复派靠这句。
    due_keys = set()
    for job in due:
        due_keys.add(job["key"])
        if job["key"] not in slots:
            slots[job["key"]] = _new_slot(job, cfg, now)
            changed = True

    for key in list(slots.keys()):
        slot = slots[key]
        if slot.get("state") in TERMINAL_STATES or key == inflight_key:
            continue
        if _slot_confirmed_at(slot, records):
            if _finalize_dispatched(cfg, slot, records, now):
                events.append((key, _msg_dispatched(slot, now)))
                changed = True
            continue
        if now > float(slot.get("expires_at", 0) or 0):
            slot["state"] = SLOT_EXPIRED
            slot["blocked_reason"] = ""
            events.append((key, f"[排班] {_slot_label(slot)} 晚过了最大延迟"
                                f"（{_grace_min(auto)} 分钟），这班跳过，不补跑"))
            changed = True
            continue
        if key not in due_keys:
            continue  # 配置被改了/窗口挪走了：留着等过期，不动它
        prev = slot.get("state")
        retry_at = float(slot.get("next_retry_at", 0) or 0)
        if runner_busy:
            new_state, reason = SLOT_WAITING_BUSY, "等自家任务收工"
        elif not emulator_ok:
            new_state, reason = SLOT_WAITING_UNKNOWN, "游戏没在跑"
        elif not team_available(slot["team_no"], records, now):
            new_state, reason = SLOT_WAITING_UNKNOWN, "部队还在外面远征"
        elif prev == SLOT_READY and now >= retry_at:
            if to_start is None:
                to_start = slot
            new_state, reason = SLOT_READY, slot.get("blocked_reason", "")
        elif now < retry_at:
            new_state = prev or SLOT_WAITING_UNKNOWN  # 冷却/预告窗口内保持原状态
            reason = slot.get("blocked_reason", "")
        else:
            new_state, reason = SLOT_READY, ""
        slot["state"] = new_state
        slot["blocked_reason"] = reason
        if new_state != prev:
            changed = True
            if new_state == SLOT_READY:
                slot["next_retry_at"] = now + TAKEOVER_PREVIEW_SEC
                events.append((key, f"[排班] ⏳ 15 秒后接管游戏：{_slot_label(slot)}"
                                    "（可在远征配置里暂停）"))
            elif new_state == SLOT_WAITING_BUSY:
                events.append((key, f"[排班] {_slot_label(slot)} 到点了，"
                                    "等手头任务收工就派（已留小旗请它顺路收工）"))
            elif new_state == SLOT_WAITING_UNKNOWN:
                events.append((key, f"[排班] {_slot_label(slot)} 先等等：{reason}"))
    return {"events": events, "start": to_start, "changed": changed}


# ==================== 安全插队旗标 / 派遣结果文件 ====================

def takeover_flag_path():
    return STATE_DIR / TAKEOVER_FLAG_NAME


def clear_takeover_flag():
    """排班暂停/停用时必须清旗：不然玩法循环会为一个不会来的排班白收工。"""
    try:
        takeover_flag_path().unlink(missing_ok=True)
    except OSError:
        pass


def sync_takeover_flag(cfg: dict, now: float | None = None) -> bool:
    """有 waiting_busy 班次就确保旗标落盘，没有就清掉。返回当前是否有旗。"""
    slots = cfg.get("automation", {}).get("slot_states", {})
    waiting = [s for s in slots.values() if s.get("state") == SLOT_WAITING_BUSY]
    path = takeover_flag_path()
    try:
        if not waiting:
            path.unlink(missing_ok=True)
            return False
        if path.exists():
            return True
        first = sorted(waiting, key=lambda s: float(s.get("expires_at", 0) or 0))[0]
        path.write_text(json.dumps({
            "key": first["key"], "team_no": first["team_no"],
            "map_code": first["map_code"],
            "requested_at": now if now is not None else time.time(),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return True


def read_dispatch_result():
    try:
        return json.loads((STATE_DIR / DISPATCH_RESULT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ==================== 时间表实况投影 ====================

def today_projection(cfg: dict | None = None, now: float | None = None,
                     choices: dict | None = None, forced: dict | None = None) -> dict:
    """今天各班的状态投影：preset lanes 全量 + custom entries 全量。

    state 除状态机六态外还有：pending（还没到点）、missed（计划时间已过但
    面板从没见过这班，多半是那会儿没在线）、dispatched（老版本 last_runs
    里确认过、没有 slot 记录的班）。
    """
    cfg = load_config() if cfg is None else cfg
    now = time.time() if now is None else now
    auto = cfg.get("automation", {})
    slots = auto.get("slot_states", {})
    last_runs = auto.get("last_runs", {})
    choices = choices or {}
    lt = time.localtime(now)
    now_min = lt.tm_hour * 60 + lt.tm_min
    today = time.strftime("%Y-%m-%d", lt)

    def _status(key, planned_ts, team_no, map_code, extra):
        slot = slots.get(key)
        item = {"time": time.strftime("%H:%M", time.localtime(planned_ts)),
                "team_no": team_no, "map_code": map_code,
                "key": key, "planned_at": planned_ts, **extra}
        if slot:
            item["state"] = slot.get("state", SLOT_WAITING_UNKNOWN)
            item["blocked_reason"] = slot.get("blocked_reason") or ""
            retry = float(slot.get("next_retry_at") or 0)
            item["next_retry_in_min"] = (int((retry - now + 59) // 60)
                                         if retry > now else None)
            base = _planned_ts(slot) or planned_ts
            ended = slot.get("dispatched_at")
            try:
                done_ts = time.mktime(time.strptime(ended, "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                done_ts = now
            item["late_min"] = max(0, int((done_ts - base) / 60))
        elif last_runs.get(key):
            item.update(state=SLOT_DISPATCHED, blocked_reason="",
                        next_retry_in_min=None,
                        late_min=max(0, int((now - planned_ts) / 60)))
        else:
            item.update(state=("pending" if planned_ts > now else "missed"),
                        blocked_reason="", next_retry_in_min=None,
                        late_min=max(0, int((now - planned_ts) / 60)))
        item["skipped_today"] = is_skipped(
            choices, key=key, team_no=team_no,
            map_code=map_code, planned_at=planned_ts) and not is_forced(
            forced or {}, key=key, team_no=team_no, map_code=map_code)
        if item["skipped_today"] and item["state"] not in TERMINAL_STATES:
            item["state"] = "skipped"
        return item

    preset_items = []
    preset = preset_payload().get(auto.get("preset"), {})
    teams = auto.get("teams", [2, 3, 4])
    start = _minute_of_day(auto.get("start_time", "08:00"))
    cycle_day = datetime.fromtimestamp(now).date()
    if now_min < start:
        cycle_day -= timedelta(days=1)
    cycle_label = cycle_day.isoformat()
    cycle_base = datetime.combine(cycle_day, datetime.min.time())
    for idx, lane in enumerate(preset.get("lanes", [])[:3]):
        if idx >= len(teams):
            continue
        shift = int(auto.get("lane_shifts", {}).get(f"{cycle_label}:lane:{idx}", 0))
        for part in lane:
            key = f"{cycle_label}:preset:{idx}:{part['offset_min']}"
            planned_ts = (cycle_base + timedelta(
                minutes=start + part["offset_min"] + shift)).timestamp()
            preset_items.append(_status(key, planned_ts, int(teams[idx]),
                                        part["map_code"],
                                        {"lane": idx, "offset_min": part["offset_min"]}))

    custom_items = []
    day_base = datetime.combine(datetime.fromtimestamp(now).date(),
                                datetime.min.time())
    for idx, e in enumerate(cfg.get("entries", [])):
        key = f"{today}:custom:{idx}:{e.get('time')}"
        planned_ts = (day_base + timedelta(
            minutes=_minute_of_day(e.get("time", "08:00")))).timestamp()
        custom_items.append(_status(key, planned_ts, int(e.get("team_no", 2)),
                                    e.get("map_code", ""), {"index": idx}))
    return {"preset": preset_items, "custom": custom_items}


def start_scheduler(config_path: str, emit_fn):
    """5 秒巡检：推进每班持久化状态机（slot_states），到点预告 15 秒接管。"""
    def _loop():
        from .script_runner import get_runner
        runner = get_runner()
        inflight = None  # (slot_key, 派遣前该队的 dispatched_at)
        while True:
            try:
                cfg = load_config()
                auto = cfg.get("automation", {})
                now = time.time()
                records = expedition_records()
                changed = False
                if inflight and not runner.is_running:
                    slot_key, before = inflight
                    inflight = None
                    events, resolved = resolve_inflight(
                        cfg, slot_key, before, now, records, read_dispatch_result())
                    changed = changed or resolved
                    for _, msg in events:
                        emit_fn("scheduler", msg)
                paused = auto.get("paused_until", "")
                pause_active = bool(paused and paused > time.strftime("%Y-%m-%d %H:%M:%S"))
                now_min = int(time.strftime("%H")) * 60 + int(time.strftime("%M"))
                today = time.strftime("%Y-%m-%d")
                choices, forced = load_choice_sets()
                due = (_preset_due(cfg, now_min, today, choices, forced)
                       if auto.get("mode") == "preset"
                       else _custom_due(cfg, now_min, today, choices, forced))
                # 自描述 forced 班（建议引擎采纳的班）不走 preset/custom 投影，
                # 两种模式下都照常到点进状态机
                due += adhoc_due(cfg, forced, now, today)
                if not auto.get("enabled"):
                    # 总开关关着：只有今日强制启用的班照常走状态机，其余照旧不跑
                    due = forced_only(due, forced)
                if pause_active or (not auto.get("enabled") and not due):
                    # 暂停/停用且无强制班 = 不接管：接管旗标必须清掉，不然玩法循环会
                    # 为一个不会来的排班白白提前收工
                    clear_takeover_flag()
                    if changed:
                        save_config(cfg)
                    time.sleep(5)
                    continue
                # 自家派遣子进程在跑不算「忙」：别的班照常评估，只是起不来
                busy = runner.is_running and runner.current_script != "dispatch"
                outcome = tick(cfg, due, now,
                               runner_busy=busy,
                               emulator_ok=True if busy else _emulator_ready(config_path),
                               records=records,
                               inflight_key=inflight[0] if inflight else None)
                changed = changed or outcome["changed"]
                for _, msg in outcome["events"]:
                    emit_fn("scheduler", msg)
                slot = outcome["start"]
                if slot is not None:
                    run_id = runner.start("dispatch", config_path, {
                        "team_no": str(slot["team_no"]), "map_code": slot["map_code"],
                        "scheduled": True, "slot_key": slot["key"],
                        **{key: slot[key] for key in ("formation_id", "formation_signature") if slot.get(key)}})
                    if run_id:
                        inflight = (slot["key"], records.get(
                            str(slot["team_no"]), {}).get("dispatched_at"))
                        emit_fn("scheduler",
                                f"[排班] 🕐 开始派遣 {_slot_label(slot)}")
                sync_takeover_flag(cfg, now)
                if changed:
                    save_config(cfg)
            except Exception as exc:
                print(f"[时刻表] 异常: {exc}", flush=True)
            time.sleep(5)

    t = threading.Thread(target=_loop, daemon=True, name="exp-scheduler")
    t.start()
    return t
