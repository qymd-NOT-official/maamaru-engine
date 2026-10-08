"""今天的时段表大总管：在任务流外守时，到点启动已授权的时段块。

块 kind：activity（单玩法 N 次）/ raid（旧联队战 N 圈）/ workflow（自定义任务流）/ daily（一键日课）。
到点时 runner 忙就保持 pending 排队等；只有换日才把没排上的块标 missed，
错过不补跑。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import threading
import time
from pathlib import Path

from touken.runtime_paths import STATE_DIR

from . import day_timeline, scheduler, workflow
from .day_plan import load_plan, review_plan


STATE_PATH = STATE_DIR / "day_conductor.json"
BUILTIN_ID = "builtin-scheduled-raid"
_BLOCK_STATUSES = {"pending", "running", "ended",
                   "interrupted", "missed", "blocked"}
_BLOCK_KINDS = {"raid", "activity", "workflow", "daily"}
_LOCK = threading.RLock()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")).hexdigest()


def _workflow_block_signature(preset: dict) -> str:
    """workflow 块的授权签名：节点规整结果 + 收尾 + 日课模式。"""
    return _digest({"nodes": workflow.normalize_nodes(preset["nodes"]),
                    "after": preset.get("after", "none"),
                    "daily_mode": bool(preset.get("daily_mode", False))})


def _migrate_v1_block(block: dict, signature) -> dict:
    migrated = {"start_min": block["start_min"], "kind": "raid",
                "runs": block["runs"],
                "label": f"联队战 {block['runs']} 圈",
                "status": block["status"]}
    if signature:
        migrated["workflow_signature"] = signature
    for key in ("run_id", "started_at", "finished_at", "reason"):
        if block.get(key) is not None:
            migrated[key] = block[key]
    return migrated


def _valid_v2_block(block) -> bool:
    if (not isinstance(block, dict)
            or type(block.get("start_min")) is not int
            or block.get("kind") not in _BLOCK_KINDS
            or block.get("status") not in _BLOCK_STATUSES):
        return False
    if block["kind"] == "raid" and type(block.get("runs")) is not int:
        return False
    if block["kind"] == "activity" and (
            type(block.get("runs")) is not int
            or not isinstance(block.get("script"), str)
            or not isinstance(block.get("event_key"), str)):
        return False
    if block["kind"] == "workflow" and not isinstance(block.get("workflow_id"), str):
        return False
    return True


def load_state(path: Path = STATE_PATH) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), list):
        return None
    version = raw.get("version")
    if version == 1:
        # v1 state 的块全是联队战；顶层签名搬进每个块，内存统一成 v2，
        # 下次 _save 落盘即 v2。
        if any(not isinstance(block, dict)
               or type(block.get("start_min")) is not int
               or type(block.get("runs")) is not int
               or block.get("status") not in _BLOCK_STATUSES
               for block in raw["blocks"]):
            return None
        raw = {**raw, "version": 2,
               "blocks": [_migrate_v1_block(block, raw.get("workflow_signature"))
                          for block in raw["blocks"]]}
    elif version != 2:
        return None
    if any(not _valid_v2_block(block) for block in raw["blocks"]):
        return None
    return raw


def _save(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    temp.replace(path)


def _plan_signature(plan: dict) -> str:
    return _digest({key: plan.get(key) for key in
                    ("day_start", "event_end_at", "blocks")})


def _preset(workflow_id: str) -> dict:
    if workflow_id == BUILTIN_ID:
        return {"id": BUILTIN_ID, "name": "按联队战设置开工",
                "nodes": [{"type": "raid", "params": {}, "on_error": "stop"}],
                "after": "none", "daily_mode": False}
    preset = workflow.find_preset(workflow_id)
    if not preset:
        raise ValueError("这份任务流已经找不到了")
    return preset


def _eligible(preset: dict) -> bool:
    try:
        nodes = workflow.normalize_nodes(preset.get("nodes"))
    except workflow.WorkflowError:
        return False
    raids = [node for node in nodes if node["type"] == "raid"]
    return len(raids) == 1 and raids[0]["on_error"] == "stop"


def options() -> list[dict]:
    presets = [_preset(BUILTIN_ID), *workflow.list_presets()]
    return [{"id": p["id"], "name": p["name"]} for p in presets if _eligible(p)]


def workflow_spec(workflow_id: str, raid_settings: dict | None = None) -> dict:
    preset = _preset(workflow_id)
    if not _eligible(preset):
        raise ValueError("请选择包含一个联队战步骤、且联队战翻车即停的任务流")
    source_nodes = workflow.normalize_nodes(preset["nodes"])
    raid_index = next(i for i, node in enumerate(source_nodes) if node["type"] == "raid")
    source_node = source_nodes[raid_index]
    saved = raid_settings if isinstance(raid_settings, dict) else {}
    effective = {**saved, **source_node["params"]}
    # 一键安排按目标圈数执行；单跑的节省手形设置不影响今日安排。
    if workflow_id == BUILTIN_ID:
        effective["auto_refill"] = True
    node = {**source_node, "params": effective}
    raw_team = effective.get("team_no", "3")
    if isinstance(raw_team, str) and raw_team.startswith("preset:"):
        from touken.custom_formations import load_formations
        formation = next((item for item in load_formations()
                          if item.get("id") == raw_team[7:]), None)
        raw_team = formation.get("target_team") if formation else None
    try:
        team_no = int(raw_team)
    except (TypeError, ValueError):
        team_no = 0
    if team_no not in (1, 2, 3, 4, 5):
        raise ValueError("这份任务流的出阵部队还没认清，请先检查联队战设置")
    signature_payload = {"raid_settings": saved, "team_no": team_no}
    after = workflow.normalize_after(preset.get("after", "none"))
    daily_mode = bool(preset.get("daily_mode", False))
    # 保留旧单步骤签名；整套任务流则同时核对其余步骤及下班安排。
    if len(source_nodes) != 1 or after != "none" or daily_mode:
        signature_payload.update(after=after, daily_mode=daily_mode)
    nodes = [dict(item) for item in source_nodes]
    nodes[raid_index] = node
    source_signature = _digest({**signature_payload, "nodes": source_nodes})
    effective_signature = _digest({**signature_payload, "nodes": nodes})
    compatible = {source_signature, effective_signature}
    # 内置今日安排固定补充手形，兼容旧安排的同一策略签名。
    if workflow_id == BUILTIN_ID:
        legacy_nodes = [{**source_node, "params": {"auto_refill": True}}]
        compatible.add(_digest({**signature_payload, "nodes": legacy_nodes}))
    return {"name": preset["name"], "team_no": team_no, "nodes": nodes,
            "raid_index": raid_index, "after": after, "daily_mode": daily_mode,
            "signature": source_signature,
            "compatible_signatures": compatible}


def _for_team(timeline: dict, team_no: int) -> dict:
    result = {**timeline}
    activity = timeline.get("activity")
    if activity:
        result["activity"] = {**activity, "occupied": day_timeline._occupied_segments(
            timeline.get("expeditions", []), scheduler.load_config(), team_no)}
    return result


def _block_issues(block: dict, timeline: dict) -> list[str]:
    activity = timeline.get("activity") or {}
    if activity.get("name") != "联队战":
        return ["当前没有可核对的联队战进度"]
    pace = int(activity.get("seconds_per_loop") or 0)
    if pace <= 0:
        return ["还没有可靠的本期圈速"]
    start, runs = block["start_min"], block["runs"]
    end = start + math.ceil(runs * pace / 60)
    deadline = min(day_timeline.DAY_MINUTES, math.floor(
        (activity["event_end_at"] - timeline["day_start"]) / 60) - 5)
    issues = []
    if end > deadline:
        issues.append("预计赶不上今天的收摊时间")
    if runs > int(activity.get("remaining_runs") or 0):
        issues.append("安排圈数超过本期剩余圈数")
    for occupied in activity.get("occupied", []):
        if start < occupied["end_min"] and end > occupied["start_min"]:
            issues.append(f"会撞上{occupied['label']}")
            break
    return issues


def arm(plan: dict, timeline: dict, workflow_id: str, raid_settings: dict,
        path: Path = STATE_PATH, *, persist: bool = True) -> dict:
    with _LOCK:
        old = load_state(path)
        if old and any(b.get("status") == "running" for b in old["blocks"]):
            raise ValueError("已有一段正在执行，等它收工后再改大总管")
        raid_spec = None
        if any(block.get("kind") == "raid" for block in plan["blocks"]):
            raid_spec = workflow_spec(workflow_id, raid_settings)
        checked = (_for_team(timeline, raid_spec["team_no"])
                   if raid_spec else timeline)
        issues = review_plan(plan, checked)
        if issues:
            raise ValueError("；".join(issues))
        blocks = []
        for block in plan["blocks"]:
            kind = block["kind"]
            if kind == "raid":
                blocks.append({
                    "start_min": block["start_min"], "kind": "raid",
                    "runs": block["runs"],
                    "label": f"联队战 {block['runs']} 圈",
                    "status": "pending",
                    "workflow_signature": raid_spec["signature"],
                })
            elif kind == "activity":
                from .scheduled_gameplay import spec
                current = spec(block["script"])
                blocks.append({**block, "status": "pending",
                               "label": f"{current['label']} {block['runs']} 次",
                               "gameplay_signature": current["signature"]})
            elif kind == "workflow":
                preset = workflow.find_preset(block["workflow_id"])
                if not preset:
                    raise ValueError("这份任务流已经找不到了")
                try:
                    signature = _workflow_block_signature(preset)
                except workflow.WorkflowError as exc:
                    raise ValueError(
                        f"任务流「{preset.get('name') or '未命名'}」拼不起来：{exc}"
                    ) from exc
                blocks.append({
                    "start_min": block["start_min"], "kind": "workflow",
                    "workflow_id": preset["id"], "label": preset["name"],
                    "status": "pending", "workflow_signature": signature,
                })
            else:
                blocks.append({"start_min": block["start_min"], "kind": "daily",
                               "label": "一键日课", "status": "pending",
                               **({"after_raids": True} if block.get("after_raids") is True else {})})
        # 编辑安排不能把已执行的同一段重新排队。身份由时间与内容决定，
        # 与整份计划的签名、数组下标和当前玩法设置无关。
        if old and old.get("day_start") == plan["day_start"]:
            previous = list(old["blocks"])
            for block in blocks:
                identity = ("kind", "start_min", "runs", "workflow_id", "script", "event_key")
                matched = next((item for item in previous
                                if all(item.get(key) == block.get(key)
                                       for key in identity)), None)
                if matched:
                    previous.remove(matched)
                    if matched.get("status") in {"ended", "interrupted", "missed"}:
                        block.update(matched)
        state = {"version": 2, "enabled": True,
                 "day_start": plan["day_start"],
                 "plan_signature": _plan_signature(plan),
                 "blocks": blocks}
        if raid_spec:
            state["workflow_id"] = workflow_id
            state["workflow_name"] = raid_spec["name"]
        if persist:
            _save(state, path)
        return state


def disarm(path: Path = STATE_PATH) -> dict | None:
    with _LOCK:
        state = load_state(path)
        if state and state.get("enabled"):
            state["enabled"] = False
            _save(state, path)
        return state


def raid_recovery(block: dict) -> dict | None:
    """Only confirmed round-boundary logs count; an unfinished round stays uncertain."""
    if block.get("kind") != "raid" or block.get("status") != "interrupted":
        return None
    if block.get("reason") != "执行状态不明，后续已停用":
        return None
    from .workflow_waits import load as load_waits
    if block.get("run_id") in load_waits():
        return None
    from .log_store import get_store
    logs = get_store().raid_progress_messages(block.get("run_id"))
    completed = 0
    started = 0
    for message in logs:
        if "已手动停止" in message or "看门狗已处决" in message:
            return None
        match = re.fullmatch(r"\[RAID\] 第 (\d+) 圈结束", message)
        if match:
            completed = max(completed, int(match[1]))
        match = re.fullmatch(r"\[RAID\] ===== 第 (\d+)/(\d+) 圈 =====", message)
        if match:
            started = max(started, int(match[1]))
    base = int(block.get("completed_base", 0))
    done = min(block["runs"], base + completed)
    return {"completed": done, "remaining": block["runs"] - done,
            "uncertain_round": started > completed}


def resume_raid(run_id: str, finished_round: bool, runner, timeline_fn,
                raid_settings_fn, config_path: str, emit_fn,
                path: Path = STATE_PATH, plan_path: Path | None = None) -> None:
    with _LOCK:
        state = load_state(path)
        plan = load_plan(plan_path) if plan_path else load_plan()
        now = time.time()
        if runner.is_running:
            raise ValueError("还有任务正在执行，等它收工再继续")
        if not state or not plan or _plan_signature(plan) != state.get("plan_signature"):
            raise ValueError("今天的安排已经变化，请重新安排")
        if state.get("workflow_id") != BUILTIN_ID:
            raise ValueError("这份自定义任务流暂不支持中断续跑")
        block = next((b for b in state["blocks"] if b.get("run_id") == run_id), None)
        recovery = raid_recovery(block) if block else None
        if not recovery:
            raise ValueError("这段联队战没有可续跑的记录")
        if not state["day_start"] <= now < state["day_start"] + day_timeline.DAY_MINUTES * 60:
            raise ValueError("今天的安排已结束，请重新安排")
        spec = workflow_spec(BUILTIN_ID, raid_settings_fn())
        if block.get("workflow_signature") not in spec["compatible_signatures"]:
            raise ValueError("联队战设置已变化，请重新安排")
        completed = recovery["completed"] + int(finished_round and recovery["uncertain_round"])
        remaining = max(0, block["runs"] - completed)
        if not remaining:
            block.update(status="ended", finished_at=now, completed_base=completed)
            _save(state, path)
            return
        candidate = {**block, "runs": remaining,
                     "start_min": int((now - state["day_start"]) // 60)}
        if not _start_block(candidate, state, runner, timeline_fn, raid_settings_fn,
                            plan, config_path, now, emit_fn):
            raise ValueError(candidate.get("reason") or "暂时不能继续，请稍后重试")
        block.update(status="running", run_id=candidate["run_id"], started_at=now,
                     completed_base=completed)
        block.pop("reason", None)
        block.pop("finished_at", None)
        # Resume this block only; do not silently re-enable other stopped work.
        _save(state, path)


def projection(plan: dict | None, timeline: dict, raid_settings: dict,
               path: Path = STATE_PATH) -> dict:
    state = load_state(path)
    result = {"enabled": False, "workflow_id": BUILTIN_ID,
              "workflow_name": "按联队战设置开工", "blocks": [],
              "issues": [], "options": options()}
    if not state or state.get("day_start") != timeline.get("day_start"):
        return result
    for key in ("enabled", "workflow_id", "workflow_name", "blocks"):
        if state.get(key) is not None:
            result[key] = state[key]
    if state.get("workflow_id") == BUILTIN_ID:
        result["blocks"] = [dict(block) for block in result["blocks"]]
        for block in result["blocks"]:
            recovery = raid_recovery(block)
            if recovery:
                block["recovery"] = recovery
    if not state.get("enabled"):
        return result
    if not plan or _plan_signature(plan) != state.get("plan_signature"):
        result["issues"].append("今天的安排改过了，请重新开启大总管")
        return result
    raid_blocks = [b for b in state["blocks"] if b.get("kind") == "raid"]
    if raid_blocks:
        try:
            spec = workflow_spec(state.get("workflow_id") or BUILTIN_ID,
                                 raid_settings)
        except ValueError as exc:
            result["issues"].append(str(exc))
            return result
        if any(b.get("workflow_signature") != spec["signature"]
               for b in raid_blocks):
            result["issues"].append("联队战设置或任务流改过了，请重新开启大总管")
            return result
        if timeline.get("activity") or not any(
                block.get("status") == "running" for block in state["blocks"]):
            adjusted = _for_team(timeline, spec["team_no"])
            for block in raid_blocks:
                if block.get("status") == "pending":
                    result["issues"].extend(_block_issues(block, adjusted))
    from .scheduled_gameplay import issues as gameplay_issues
    for block in state["blocks"]:
        if block.get("kind") == "activity" and block.get("status") == "pending":
            result["issues"].extend(gameplay_issues(block, timeline, check_settings=True))
    return result


def _start_block(block: dict, state: dict, runner, timeline_fn,
                 raid_settings_fn, plan: dict, config_path: str, now: float,
                 emit_fn) -> bool:
    """ runner 空闲时对一块做开工前预检并启动；返回是否真的点着了火。"""
    kind = block.get("kind")
    if kind == "raid":
        try:
            spec = workflow_spec(state.get("workflow_id") or BUILTIN_ID,
                                 raid_settings_fn())
        except ValueError as exc:
            block["status"] = "blocked"
            block["reason"] = str(exc)
            emit_fn("conductor", f"[大总管] 本段没有开工：{block['reason']}")
            return False
        timeline = timeline_fn()
        adjusted = _for_team(timeline, spec["team_no"])
        issues = _block_issues(block, adjusted)
        event_end_at = plan.get("event_end_at")
        if (not adjusted.get("activity") or event_end_at is None
                or abs(float(event_end_at)
                       - float(adjusted["activity"]["event_end_at"])) > 60):
            issues.append("活动时间已变化")
        if issues:
            block["status"] = "blocked"
            block["reason"] = "；".join(issues)
            emit_fn("conductor", f"[大总管] 本段没有开工：{block['reason']}")
            return False
        run_id = runner.start("workflow", config_path, {
            "workflow_id": state.get("workflow_id") or BUILTIN_ID,
            "scheduled_raid_runs": block["runs"],
            "scheduled_workflow_signature": block.get("workflow_signature"),
            "scheduled_deadline": min(state["day_start"] + day_timeline.DAY_MINUTES * 60,
                                      float(event_end_at)),
        })
        if not run_id:
            return False
        block.update(status="running", run_id=run_id, started_at=now)
        emit_fn("conductor", f"[大总管] 联队战开工，安排 {block['runs']} 圈")
        return True
    if kind == "activity":
        from .scheduled_gameplay import issues as gameplay_issues
        problems = gameplay_issues(block, timeline_fn(), check_settings=True)
        if problems:
            block.update(status="blocked", reason="；".join(problems))
            emit_fn("conductor", f"[大总管] 本段没有开工：{block['reason']}")
            return False
        run_id = runner.start("scheduled_gameplay", config_path, {
            "script": block["script"], "runs": block["runs"],
            "event_key": block["event_key"], "gameplay_signature": block["gameplay_signature"],
            "scheduled_deadline": min(state["day_start"] + day_timeline.DAY_MINUTES * 60,
                next((option.get("end_at") or float("inf") for option in timeline_fn().get("gameplay_options", [])
                      if option["script"] == block["script"]), float("inf"))),
        })
        if not run_id:
            return False
        block.update(status="running", run_id=run_id, started_at=now)
        emit_fn("conductor", f"[大总管] {block['label']} 开工")
        return True
    if kind == "workflow":
        preset = workflow.find_preset(block.get("workflow_id") or "")
        signature = None
        if preset:
            try:
                signature = _workflow_block_signature(preset)
            except workflow.WorkflowError:
                signature = None
        if not preset:
            block["status"] = "blocked"
            block["reason"] = "这份任务流已经找不到了"
        elif signature != block.get("workflow_signature"):
            block["status"] = "blocked"
            block["reason"] = "任务流改过了，请重新开启大总管"
        else:
            run_id = runner.start("workflow", config_path,
                                  {"workflow_id": block["workflow_id"]})
            if not run_id:
                return False
            block.update(status="running", run_id=run_id, started_at=now)
            emit_fn("conductor",
                    f"[大总管] 「{block.get('label') or '排好的任务流'}」开工")
            return True
        emit_fn("conductor", f"[大总管] 本段没有开工：{block['reason']}")
        return False
    # daily：日出而作，直接过。
    run_id = runner.start("daily", config_path, {})
    if not run_id:
        return False
    block.update(status="running", run_id=run_id, started_at=now)
    emit_fn("conductor", "[大总管] 一键日课开工，收今天的奖励")
    return True


def tick(now: float, runner, timeline_fn, raid_settings_fn, config_path: str,
         emit_fn, path: Path = STATE_PATH, plan_path: Path | None = None) -> None:
    """巡检一次；到点的块排队等 runner 空位，只有换日才结算 missed。"""
    from . import workflow_waits
    workflow_waits.resume_due(now, runner)
    waiting_runs = workflow_waits.load()
    with _LOCK:
        state = load_state(path)
        if not state:
            return
        changed = False
        for block in state["blocks"]:
            if block.get("status") != "running":
                continue
            continuation = waiting_runs.get(block.get("run_id"), {})
            if continuation.get("status") in {"waiting", "launching", "running"}:
                continue
            if runner.is_running and runner.current_run_id == block.get("run_id"):
                continue
            last_run_id, last_status = runner.last_run_result
            block["status"] = ("ended" if continuation.get("status") == "completed" or (last_run_id == block.get("run_id")
                               and last_status == "completed") else "interrupted")
            block["finished_at"] = now
            if block["status"] == "interrupted":
                state["enabled"] = False
                block["reason"] = ("任务流失败，后续已停用" if last_run_id == block.get("run_id")
                                   and last_status in {"failed", "stopped", "watchdog"}
                                   else "执行状态不明，后续已停用")
            changed = True
            if block["status"] == "ended":
                emit_fn("conductor", "[大总管] 联队战时段已结束，请到成绩单看实际圈数"
                        if block.get("kind") == "raid" else
                        f"[大总管] {block.get('label') or '这段活'}收工了，成绩单已记账")
            else:
                emit_fn("conductor", f"[大总管] {block['reason']}")
        if not state.get("enabled"):
            if changed:
                _save(state, path)
            return
        plan = load_plan(plan_path) if plan_path else load_plan()
        if not plan or _plan_signature(plan) != state.get("plan_signature"):
            state["enabled"] = False
            changed = True
            emit_fn("conductor", "[大总管] 今日安排变了或已经换日，自动开工已停用")
        elif not state["day_start"] <= now < state["day_start"] + day_timeline.DAY_MINUTES * 60:
            # 换日：仍排队的块统一标 missed，错过不补跑。
            for block in state["blocks"]:
                if block.get("status") == "pending":
                    block["status"] = "missed"
                    changed = True
            state["enabled"] = False
            changed = True
            emit_fn("conductor", "[大总管] 今天翻篇了，没排上的时段标为错过，明天请重新安排")
        if not state.get("enabled"):
            _save(state, path)
            return
        if any(b.get("status") == "running" and waiting_runs.get(b.get("run_id"), {}).get("status") != "waiting"
               for b in state["blocks"]):
            if changed:
                _save(state, path)
            return
        for block in state["blocks"]:
            if block.get("status") != "pending":
                continue
            due = float(state["day_start"]) + block["start_min"] * 60
            after_raids = block.get("kind") == "daily" and block.get("after_raids") is True
            if after_raids and any(b.get("kind") == "raid" and b.get("status") != "ended"
                                   for b in state["blocks"]):
                continue
            if now < due and not after_raids:
                break
            if runner.is_running:
                # 到点的块保持 pending 排队等，远征或其他任务优先，不抢位置
                break
            started = _start_block(block, state, runner, timeline_fn,
                                   raid_settings_fn, plan, config_path, now,
                                   emit_fn)
            if started:
                changed = True
                break  # 一块 running 时不启新块
            if block.get("status") == "blocked":
                changed = True
                continue  # 这块开不了不挡后面的块
            break  # runner 临时拒了开工（抢位置的竞争），下次巡检再试
        if changed:
            _save(state, path)


def start_conductor(config_path: str, runner, timeline_fn, raid_settings_fn,
                    emit_fn):
    from .workflow_waits import recover
    recover()
    def _loop():
        while True:
            try:
                tick(time.time(), runner, timeline_fn, raid_settings_fn,
                     config_path, emit_fn)
            except Exception as exc:
                print(f"[大总管] 巡检异常: {exc}", flush=True)
            time.sleep(5)

    thread = threading.Thread(target=_loop, daemon=True, name="day-conductor")
    thread.start()
    return thread
