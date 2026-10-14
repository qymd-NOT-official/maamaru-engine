"""安排的取消入口：按归属撤销授权，调度器只执行仍有效的安排。"""

import copy
import time
from pathlib import Path

from . import day_conductor as dc, expedition_choices as ec, scheduler
from . import workflow_waits


def plan_is_authorized(plan_id: str | None, state: dict | None = None) -> bool:
    """无归属的是独立任务；有归属的任务必须仍由当前未取消的安排授权。"""
    if not plan_id:
        return True
    state = dc.load_state(dc.STATE_PATH) if state is None else state
    return bool(state and state.get('plan_id') == plan_id and not state.get('cancelled_at'))


def authorize_expeditions(records: dict, now: float | None = None) -> None:
    """新授权已落盘后，明确重建同键的取消记录；巡检本身不能复活它。"""
    now = time.time() if now is None else now
    with scheduler.CONTROL_LOCK:
        cfg = scheduler.load_config()
        slots = cfg['automation'].get('slot_states', {})
        changed = False
        for key, record in records.items():
            if slots.get(key, {}).get('state') == scheduler.SLOT_CANCELLED:
                slots[key] = scheduler._new_slot({**record, 'key': key}, cfg, now)
                changed = True
        if changed:
            scheduler.save_config(cfg)


def cancel_expeditions(runner, *, keys: set[str] | None = None,
                       plan_id: str | None = None, disable_schedule: bool = False,
                       choices_path: Path | None = None, now: float | None = None,
                       stop_worker: bool = True, intentions: dict | None = None) -> set[str]:
    """keys/plan_id 只取消指定班；均未指定时关闭全部远征安排，不动玩法任务。"""
    now = time.time() if now is None else now
    choices_path = ec.CHOICES_PATH if choices_path is None else choices_path
    with scheduler.CONTROL_LOCK, ec._WRITE_LOCK:
        cfg = scheduler.load_config()
        auto = cfg['automation']
        slots = auto.setdefault('slot_states', {})
        skipped, forced = ec.load_choice_sets(choices_path)
        selected = set(keys or ())
        if keys is None and plan_id is None:
            selected.update(slots)
            selected.update(forced)
        if plan_id:
            selected.update(key for key, record in forced.items() if record.get('plan_id') == plan_id)
            selected.update(key for key, slot in slots.items() if slot.get('plan_id') == plan_id
                            and (key not in forced or forced[key].get('plan_id') == plan_id))
        if not selected and not disable_schedule:
            return set()
        if disable_schedule:
            auto['enabled'] = False
            scheduler.save_config(cfg)
        for key in selected:
            slot = slots.get(key)
            # 已确认出发的远征不召回，完成记录也不能改写成取消。
            if (slot or {}).get('state') == scheduler.SLOT_DISPATCHED or auto.get('last_runs', {}).get(key):
                continue
            record = forced.pop(key, None)
            if record:
                skipped[key] = record
            elif slot or (intentions or {}).get(key):
                source = slot or intentions[key]
                skipped[key] = {field: source[field] for field in ('team_no', 'map_code', 'planned_at') if field in source}
            if not slot and (record or (intentions or {}).get(key)):
                source = record or intentions[key]
                slot = slots[key] = scheduler._new_slot({**source, 'key': key}, cfg, now)
            if slot and slot.get('state') not in scheduler.TERMINAL_STATES:
                slot.update(state=scheduler.SLOT_CANCELLED, cancelled_at=now,
                            blocked_reason='玩家已取消这班远征')
        scheduler.save_config(cfg)
        try:
            ec._write_sets(skipped, forced, choices_path)
        finally:
            scheduler.sync_takeover_flag(cfg, now)
            context = getattr(runner, 'current_execution', {})
            if (stop_worker and runner.is_running and isinstance(context, dict) and context.get('scheduled')
                    and context.get('slot_key') in selected):
                runner.stop(cancel_waits=False)
        return selected


def stop_today(runner, *, state_path: Path = dc.STATE_PATH,
               choices_path: Path = ec.CHOICES_PATH, now: float | None = None) -> dict:
    """只取消当前今日安排及其子任务；独立派遣、手动任务不受影响。"""
    now = time.time() if now is None else now
    with dc._LOCK, scheduler.CONTROL_LOCK:
        state = dc.load_state(state_path)
        if not state:
            return {'ok': True, 'message': '没有需要停止的今日安排。'}
        plan_id = state.get('plan_id')
        roots = {block['run_id'] for block in state['blocks'] if block.get('run_id')}
        # 旧版若留下明确的子班列表就沿用；不猜测无归属的手动远征。
        keys = (set(state.get('expedition_keys', state.get('initial_expedition_keys', [])))
                if not plan_id else set())
        if keys:
            # 旧版子班键不能覆盖后来已经明确记录的新归属。
            current = ec.load_choice_sets(choices_path)[1]
            keys = {key for key in keys if not current.get(key, {}).get('plan_id')}
        before = copy.deepcopy(state['blocks'])
        state['enabled'] = False
        state['cancelled_at'] = now
        state['today_execution'] = False
        for block in state['blocks']:
            if block['status'] not in {'ended', 'missed', 'cancelled'}:
                block.update(status='cancelled', reason='玩家已停止这份今日安排', finished_at=now)
        if before != state['blocks']:
            state.setdefault('cancelled_arrangements', []).append(
                {'stopped_at': now, 'day_start': state['day_start'], 'blocks': before})
        selected = keys
        try:
            dc._save(state, state_path)
            waits = workflow_waits.load()
            for root, record in waits.items():
                if (root in roots or record.get('run_id') in roots
                        or (plan_id and record.get('params', {}).get('plan_id') == plan_id)):
                    roots.add(root)
                    if record.get('run_id'):
                        roots.add(record['run_id'])
            # remember 防止已停止的进程迟到写入等待点，再把自己复活。
            for root in roots:
                workflow_waits.cancel(root, remember=True)
            selected = cancel_expeditions(runner, keys=keys, plan_id=plan_id,
                                          choices_path=choices_path, now=now, stop_worker=False)
        finally:
            context = getattr(runner, 'current_execution', {})
            if runner.is_running and (runner.current_run_id in roots or (
                    plan_id and isinstance(context, dict) and context.get('plan_id') == plan_id) or (
                    isinstance(context, dict) and context.get('scheduled') and context.get('slot_key') in selected)):
                runner.stop(cancel_waits=False)
    return {'ok': True, 'message': '已停止这份今日安排及它带出的远征。独立安排不受影响，可重新开工。'}
