# -*- coding: utf-8 -*-
"""凌晨日课自动预排：04:00 换日后，把「一键日课」按设置时间自动排进今日安排。

每分钟醒一次，只做一件事：确保今天的时段表里有一块 builtin-daily。
到点开工是大总管（day_conductor）的既有职责，这里不重复造。

开关与钟点都在日课设置里（builtin-daily 预设的 daily_ui）：
  autoSchedule: true 且 startTime 合法（HH:MM）才生效，默认关。
默认只预排日课块本身；开了 autoExecutePlan（默认关）才在预排时点
直接复用 today_execution.execute_today 做整份收编（远征建议采纳+
活动窗口+日课排入），与按「一键执行今日安排」按钮等价。
autoSchedule 关着时 autoExecutePlan 不生效（收编依赖预排时点）。

玩家优先（她自己上手管了，今天就别插手）：
  - 她手动停了今日安排（conductor cancelled_at）→ 当天罢手；
  - 今天 runs 里有日课痕迹（script=="daily"，或 workflow 形态的
    builtin-daily——runs 表没有 params，按开工时写入的 label 认），
    running/completed/stopped/failed 任一状态都罢手，绝不背靠背双跑；
  - 我预排/收编过但块已不在 plan → 她手动删了，记 dismissed 当天罢手；
  - 状态文件损坏 → 备份后按「conductor state → run 记录 → plan 现状」
    重建罢手证据，有疑义当天不补。

失败分级：
  - review_plan/收编校验类（块重叠、超窗口、建议不可用等排不进去）
    → 记 give_up 当天不再反复 arm/写盘，每次只重审不轰炸；她改了安排
    或联队战进度同步后，重审通过自动恢复；
  - arm/runner 竞争类（runner 忙/块 running）→ 下分钟再试，同一错误只报一次。
"""
from __future__ import annotations

import json
import math
import shutil
import threading
import time
from pathlib import Path

from touken.runtime_paths import STATUS_DIR

from . import day_conductor as dc, day_plan as dp, day_timeline as dt, scheduler, workflow
from . import today_execution as te

STATE_PATH = STATUS_DIR / "daily_autoschedule.json"
_TICK_SECONDS = 60
_ROLLOVER_MIN = 4 * 60  # 04:00 换日
_DEFAULT_DAILY_NAME = "一键日课"


def _execute_today(runner, timeline_fn, raid_settings_fn, *, daily_preset, **paths):
    """收编入口：与「一键执行今日安排」按钮同一条 today_execution 逻辑。"""
    return te.execute_today(runner, timeline_fn, raid_settings_fn,
                            daily_preset=daily_preset, **paths)


def _load_state(path: Path = STATE_PATH) -> tuple[dict, bool]:
    """读状态文件；损坏时备份后重置为空（默认未 dismissed），返回 (状态, 是否损坏)。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        try:
            bad = path.with_name(path.name + ".bad-"
                                 + time.strftime("%Y%m%d-%H%M%S"))
            path.replace(bad)
        except OSError:
            pass
        return {}, True
    if not isinstance(raw, dict):
        return {}, True
    return raw, False


def _save_state(state: dict, path: Path = STATE_PATH) -> None:
    """tmp + replace 原子写（仿 day_conductor._save），失败时旧文件仍在。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    temp.replace(path)


def _daily_touched(day_start: float, day_end: float, preset_name: str) -> bool:
    """今天 runs 里有日课痕迹：她自己上手管了，今天就别插手。

    script=="daily" 直接算；workflow 形态的 builtin-daily（手动从工作流页
    跑、统一今日安排、大总管开工都是这条通道）按开工时写进 runs.label 的
    预设名认。状态不论是 running/completed/stopped/failed——还在跑更不
    能再排一个，跑了半截也说明今天她已经自己管过了。
    """
    try:
        from touken.telemetry import get_telemetry_store
        rows = get_telemetry_store().runs_between(day_start, day_end)
    except Exception:
        return False
    names = {preset_name or _DEFAULT_DAILY_NAME, _DEFAULT_DAILY_NAME}
    for row in rows or []:
        try:
            if float(row.get("started_at") or 0) < day_start:
                continue
        except (TypeError, ValueError):
            continue
        if row.get("script") == "daily":
            return True
        if row.get("script") == "workflow" and (row.get("label") or "") in names:
            return True
    return False


def _target_start_min(clock) -> int | None:
    """HH:MM → 窗口分钟。窗口以 00:00 为原点、28 小时长（04:00 换日，
    与 compose_daily_plan / 时间表前端同口径）：04:00 起按当天分钟数，
    00:00–03:59 的凌晨块排在窗口尾部。"""
    minute = workflow._parse_hhmm(clock)
    if minute is None:
        return None
    return minute if minute >= _ROLLOVER_MIN else minute + 1440


def _adopt_today_plan(state: dict, state_path: Path, emit, runner, execute_fn,
                      timeline_fn, raid_settings_fn, preset: dict,
                      conductor_path: Path, plan_path: Path,
                      choices_path: Path | None) -> str:
    """autoExecutePlan 开关打开时：代按「一键执行今日安排」整份收编。

    收编本体是 today_execution.execute_today（远征建议采纳+日课排入+武装
    大总管），这里只负责收口：竞争类（runner 忙）下分钟再试，校验类
    （安排排不进、建议不可用）记 give_up 当天只重审不轰炸。
    调用方已持 dc._LOCK + CONTROL_LOCK，execute_today 内部的同锁重入安全。
    """
    if runner is None:
        from .script_runner import get_runner
        runner = get_runner()
    # 竞争类：runner 被别的任务占着，本次跳过，下分钟再试
    if runner.is_running:
        return "busy"
    paths = {"state_path": conductor_path, "plan_path": plan_path}
    if choices_path is not None:
        paths["choices_path"] = choices_path
    try:
        result = execute_fn(runner, timeline_fn, raid_settings_fn,
                            daily_preset=preset, **paths)
    except ValueError as exc:
        # 校验类：这版安排/建议本身接不进去，她改了安排或进度同步后
        # 重审通过自动恢复；同一原因只报一次。
        message = str(exc)
        if state.get("give_up") != message:
            state["give_up"] = message
            _save_state(state, state_path)
            emit("autoschedule",
                 f"[日课预排] 今天自动收编没接成：{message}（情况变化会自己再试）")
        return "gave_up"
    state.update(armed=True, give_up=None, last_error=None)
    _save_state(state, state_path)
    detail = (result or {}).get("message")
    emit("autoschedule",
         f"[日课预排] 已自动收编今日安排：{detail}" if detail else
         "[日课预排] 已自动收编今日安排，到点大总管自己开工")
    return "armed"


def _wall_clock(start_min: int) -> str:
    wall = start_min % 1440
    return f"{wall // 60:02d}:{wall % 60:02d}"


def _is_daily_block(block: dict) -> bool:
    return (block.get("kind") == "daily"
            or (block.get("kind") == "workflow"
                and block.get("workflow_id") == workflow.DAILY_PRESET_ID))


def maybe_schedule(now=None, *, emit=None, timeline_fn=None, raid_settings_fn=None,
                   preset_fn=None, touched_fn=None, runner=None, execute_fn=None,
                   choices_path: Path | None = None,
                   state_path: Path = STATE_PATH, plan_path: Path = dp.PLAN_PATH,
                   conductor_path: Path = dc.STATE_PATH) -> str:
    """醒一次；返回本次结论（测试与日志用）。失败只记日志，绝不炸线程。"""
    emit = emit or (lambda script, message: None)
    preset_fn = preset_fn or workflow.find_preset
    touched_fn = touched_fn or _daily_touched
    execute_fn = execute_fn or _execute_today
    now = time.time() if now is None else now
    day_start, day_end = dt._day_window(now)

    preset = preset_fn(workflow.DAILY_PRESET_ID) or {}
    ui = preset.get("daily_ui") or {}
    start_min = (_target_start_min(ui.get("startTime"))
                 if ui.get("autoSchedule") is True else None)
    # 自动收编：autoSchedule 关着（含没填开始时间）时一律不生效
    auto_execute = start_min is not None and ui.get("autoExecutePlan") is True
    if start_min is None:
        return "off"

    state, corrupted = _load_state(state_path)
    if state.get("day_start") != day_start:
        state = {"day_start": day_start, "armed": False, "dismissed": False}

    # 读 plan/conductor → 追加 → save_plan → arm 全程持同一把锁：
    # api_save_day_schedule 也在 dc._LOCK 里跑完全程，两边不会互相踩掉
    # save 与 arm 之间的窗口（state 签名回落、全天停摆事故）。
    with dc._LOCK, scheduler.CONTROL_LOCK:
        conductor = dc.load_state(conductor_path)
        same_day = bool(conductor and conductor.get("day_start") == day_start)
        # 闸门 a：她手动停了今日安排，当天罢手
        if same_day and conductor.get("cancelled_at"):
            return "cancelled"
        # 状态文件损坏重建罢手证据：conductor 同日已有非 pending 的日课块
        # （跑过/跑翻车/被取消过都算痕迹），有疑义当天不补。
        if corrupted and same_day and any(
                _is_daily_block(b) and b.get("status") != "pending"
                for b in conductor["blocks"]):
            state.update(armed=True, dismissed=True)
            _save_state(state, state_path)
            emit("autoschedule",
                 "[日课预排] 预排记录损坏，今天已有日课执行痕迹，不再补排")
            return "recovered"
        # 闸门 b：今天 runs 里有日课痕迹（含还在跑的），罢手
        if touched_fn(day_start, day_end, preset.get("name") or ""):
            return "touched"

        plan = dp.load_plan(plan_path)
        same_plan = bool(plan and plan.get("day_start") == day_start)
        blocks = [dict(b) for b in plan["blocks"]] if same_plan else []
        has_daily = any(_is_daily_block(b) for b in blocks)
        # 闸门 c：我预排过，但块已经不在——她手动删了，当天罢手
        if state.get("armed") and not has_daily:
            if not state.get("dismissed"):
                state["dismissed"] = True
                _save_state(state, state_path)
                emit("autoschedule", "[日课预排] 预排的日课块被删掉了，今天不再补排")
            return "dismissed"
        if state.get("dismissed"):
            return "dismissed"
        # 已排了日课块：尊重她的时间，不动
        if has_daily:
            return "present"
        # 闸门 d：有块正在跑，本次跳过，下分钟再试
        if same_day and any(b.get("status") == "running" for b in conductor["blocks"]):
            return "busy"

        now_min = max(0, math.ceil((now - day_start) / 60))
        # 03:50–04:10 是游戏换日特殊窗口：此刻补排等于让日课骑着换日点跑，
        # 今天不再补排（每天只提醒一次）。
        if now_min >= dt.DAILY_RESET_WINDOW[0]:
            if not state.get("reset_noted"):
                state["reset_noted"] = True
                _save_state(state, state_path)
                emit("autoschedule",
                     "[日课预排] 已到日课刷新窗口（03:50 之后），今天不再补排")
            return "reset_window"

        if auto_execute:
            return _adopt_today_plan(state, state_path, emit, runner, execute_fn,
                                     timeline_fn, raid_settings_fn, preset,
                                     conductor_path, plan_path, choices_path)

        block = {"start_min": max(start_min, now_min), "kind": "workflow",
                 "workflow_id": workflow.DAILY_PRESET_ID,
                 **({"after_raids": True}
                    if any(b.get("kind") == "raid" for b in blocks) else {})}
        blocks.append(block)
        blocks.sort(key=lambda b: b["start_min"])
        new_plan = {"day_start": day_start,
                    "event_end_at": (plan.get("event_end_at") if same_plan else None),
                    "blocks": blocks}
        timeline = timeline_fn()
        raid_settings = raid_settings_fn() if raid_settings_fn else {}
        # 校验类失败（块重叠、超窗口等）是这版安排本身排不进去：当天不再
        # 反复 arm/写盘；每次 tick 只重审，她改了安排或联队战进度同步后
        # 重审通过会自动恢复。
        issues = dp.review_plan(new_plan, timeline)
        if issues:
            message = "；".join(issues)
            if state.get("give_up") != message:
                state["give_up"] = message
                _save_state(state, state_path)
                emit("autoschedule",
                     f"[日课预排] 今天这版安排排不进日课：{message}"
                     "（安排有变化会自己再试）")
            return "gave_up"
        try:
            dc.arm(new_plan, timeline, dc.BUILTIN_ID, raid_settings,
                   conductor_path, persist=False)
            saved = dp.save_plan(new_plan["day_start"], new_plan["event_end_at"],
                                 new_plan["blocks"], plan_path)
            dc.arm(saved, timeline, dc.BUILTIN_ID, raid_settings, conductor_path)
        except (ValueError, workflow.WorkflowError) as exc:
            # 竞争类失败（runner 忙/有块在跑）：下分钟再试，同一错误只报一次
            message = str(exc)
            if state.get("last_error") != message:
                state["last_error"] = message
                _save_state(state, state_path)
                emit("autoschedule",
                     f"[日课预排] 暂时没能把日课排进今天：{message}（下分钟再试）")
            return "error"
    state.update(armed=True, give_up=None, last_error=None)
    _save_state(state, state_path)
    emit("autoschedule",
         f"[日课预排] 已把「一键日课」自动排进今天 {_wall_clock(block['start_min'])}，"
         "到点大总管自己开工")
    return "armed"


def start_autoschedule(timeline_fn, raid_settings_fn, emit_fn):
    """每分钟 tick 一次的守护线程；到点执行是大总管的活，不归这里管。"""
    def _loop():
        time.sleep(20)  # 等面板和大总管站稳再开口
        while True:
            try:
                maybe_schedule(time.time(), emit=emit_fn,
                               timeline_fn=timeline_fn,
                               raid_settings_fn=raid_settings_fn)
            except Exception as exc:
                print(f"[日课预排] 巡检异常: {exc}", flush=True)
            time.sleep(_TICK_SECONDS)

    thread = threading.Thread(target=_loop, daemon=True, name="daily-autoschedule")
    thread.start()
    return thread
