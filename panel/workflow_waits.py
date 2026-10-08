"""任务流中途等待的续跑记录；等待不占游戏执行位置。"""

import json
import math
import shutil
import threading
import time
from pathlib import Path

from touken.runtime_paths import STATE_DIR

PATH = STATE_DIR / "workflow_waits.json"
LOCK = threading.RLock()


def load(path=None):
    try:
        raw = json.loads(Path(path or PATH).read_text(encoding="utf-8"))
        if raw.get("version") != 1 or not isinstance(raw.get("runs"), dict):
            return {}
        result = {}
        for key, record in raw["runs"].items():
            if not isinstance(record, dict) or record.get("id") != key:
                continue
            if record.get("status") in {"waiting", "launching", "running"} and (
                    type(record.get("wake_at")) not in (int, float) or not math.isfinite(record["wake_at"])
                    or not isinstance(record.get("params"), dict)
                    or not isinstance(record.get("config_path"), str)):
                continue
            result[key] = record
        return result
    except (OSError, ValueError, AttributeError):
        return {}


def _save(runs, path=None):
    path = Path(path or PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 1, "runs": runs}, ensure_ascii=False), encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    temporary.replace(path)


def park(run_id, config_path, params, deadline, resume, name, path=None, script="workflow"):
    with LOCK:
        runs = load(path)
        root = params.get("workflow_wait_root") or run_id
        # 续跑过程被取消后，不允许下一段等待把取消状态复活。
        if runs.get(root, {}).get("status") == "cancelled":
            return False
        runs[root] = {"id": root, "run_id": run_id, "name": name,
                      "script": script,
                      "status": "waiting", "wake_at": deadline, "config_path": config_path,
                      "params": {**params, "workflow_wait_root": root, "workflow_resume": resume}}
        if resume.get("reason") == "expedition":
            runs[root]["reason"] = '等远征派遣后继续未完成的任务'
            from . import scheduler
            now = time.time()
            slots = scheduler.load_config().get('automation', {}).get('slot_states', {})
            runs[root]['expedition_keys'] = [key for key, slot in slots.items()
                if slot.get('state') in {scheduler.SLOT_READY, scheduler.SLOT_WAITING_BUSY, scheduler.SLOT_WAITING_UNKNOWN}
                and now <= float(slot.get('expires_at', 0))]
        _save(runs, path)
        return True


def segment_started(root, run_id, path=None):
    with LOCK:
        runs = load(path)
        if root in runs and runs[root]["status"] == "launching":
            runs[root].update(status="running", run_id=run_id)
            _save(runs, path)


def finish(run_id, status, path=None):
    with LOCK:
        runs = load(path)
        for record in runs.values():
            if record.get("run_id") == run_id and record.get("status") == "running":
                record["status"] = status
                _save(runs, path)
                break


def cancel(root=None, path=None, remember=False):
    with LOCK:
        runs = load(path)
        changed = False
        if remember and root is not None and root not in runs:
            runs[root] = {"id": root, "status": "cancelled"}
            changed = True
        for key, record in runs.items():
            if (root is None or root == key) and record.get("status") in {"waiting", "launching", "running", "interrupted"}:
                record["status"] = "cancelled"
                changed = True
        if changed:
            _save(runs, path)
        return changed


def recover(path=None):
    """重启只恢复已安全停在等待点的流程，执行中的断点不能猜着重跑。"""
    with LOCK:
        runs = load(path)
        changed = False
        for record in runs.values():
            if record.get("status") in {"running", "launching"}:
                record.update(status="interrupted", reason="面板重启时正在执行，请核对后重新安排")
                changed = True
        if changed:
            _save(runs, path)


def resume_due(now, runner, path=None):
    selected = None
    with LOCK:
        if runner.is_running:
            return
        runs = load(path)
        for record in sorted(runs.values(), key=lambda r: r.get("wake_at", 0)):
            if record.get("status") != "waiting" or now < record["wake_at"]:
                continue
            if record["params"].get("workflow_resume", {}).get("reason") == "expedition":
                deadline = record["params"].get("scheduled_deadline")
                if deadline is not None and now >= deadline:
                    record.update(status="interrupted", reason="本日安排或活动已结束，剩余圈数未补跑")
                    _save(runs, path)
                    continue
                from .scheduler import takeover_flag_path, load_config, SLOT_READY, SLOT_WAITING_BUSY, SLOT_FAILED, SLOT_EXPIRED
                slots = load_config().get("automation", {}).get("slot_states", {})
                if any(slots.get(key, {}).get('state') in {SLOT_FAILED, SLOT_EXPIRED} for key in record.get('expedition_keys', [])):
                    record.update(status='interrupted', reason='远征派遣未完成，后续任务与结束行为停止')
                    _save(runs, path)
                    continue
                if any(slot.get("state") in {SLOT_READY, SLOT_WAITING_BUSY}
                       and now <= float(slot.get("expires_at", now)) for slot in slots.values()):
                    continue  # 包括接管预告窗口，防止联队战与派遣互相抢位置。
                try:
                    flag = json.loads(takeover_flag_path().read_text(encoding="utf-8"))
                    requested = float(flag.get("requested_at", 0))
                except (OSError, ValueError, TypeError):
                    requested = 0
                if requested > 0 and 0 <= now - requested <= 2 * 3600:
                    continue  # 排班仍在排队，不能抢回执行位置。
            # 启动前写领取状态，崩溃也不会把已开始的积木再跑一次。
            record["status"] = "launching"
            _save(runs, path)
            selected = record
            break
    if selected:
        run_id = runner.start(selected.get("script", "workflow"), selected["config_path"], selected["params"])
        if not run_id:
            with LOCK:
                runs = load(path)
                if runs[selected["id"]]["status"] == "launching":
                    runs[selected["id"]]["status"] = "waiting"
                    _save(runs, path)


def projection(preset, start_ts):
    """展示同一份步骤；未知耗时只给执行顺序，不伪造结束时间。"""
    from . import workflow
    nodes = workflow.normalize_nodes(preset.get("nodes"))
    result = []
    known_at = start_ts
    for node in nodes:
        definition = workflow.NODE_REGISTRY[node["type"]]
        step = {"label": definition["label"], "type": node["type"], "at": known_at, "wait_time": None}
        if node["type"] == "expedition":
            from .scheduler import load_config, managed_teams, TEAM_NAMES
            cfg = load_config()
            owned = managed_teams(cfg)
            plan = [p for p in cfg.get("common_plan", []) if p.get("enabled") and p.get("map_code")
                    and int(p["team_no"]) not in owned]
            step["label"] = "收远征奖励" + ("，再派 " + "、".join(
                f"{TEAM_NAMES.get(int(p['team_no']), p['team_no'])}→{p['map_code']}" for p in plan) if plan else "")
        if node["type"] == "wait_until":
            step["wait_time"] = node["params"].get("time")
            if known_at is not None and workflow._parse_hhmm(step["wait_time"]) is not None:
                seconds, _ = workflow._wait_seconds(workflow._parse_hhmm(step["wait_time"]), known_at)
                known_at += seconds
            else:
                known_at = None
        else:
            known_at = None
        result.append(step)
    return result


def public_records(path=None):
    result = []
    for key, record in load(path).items():
        if record.get("status") not in {"waiting", "launching", "running", "interrupted"}:
            continue
        resume = record.get("params", {}).get("workflow_resume", {})
        try:
            nodes = resume.get("nodes", [])[resume.get("next_index", 0):]
            steps = projection({"nodes": nodes}, record.get("wake_at")) if nodes else []
        except (ValueError, TypeError, KeyError):
            steps = []
        result.append({"id": key, "name": record.get("name", "任务流"),
                       "wake_at": record.get("wake_at"), "status": record["status"],
                       "reason": record.get("reason"), "steps": steps})
    return result
