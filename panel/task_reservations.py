"""Reserve player tasks before suggesting optional work. Durations are estimates."""
import math
import statistics


def task_windows(now, day_start, plan, state, active, store, workflow_name):
    try:
        history = store.runs_between(now - 30 * 86400, now) if store else []
    except Exception:
        history = []
    def duration(script, label=None):
        rows = [r for r in history if r.get('script') == script
                and (not label or r.get('label') == label)
                and r.get('status') in ('success', 'completed')
                and r.get('ended_at') and r.get('started_at')
                and r['ended_at'] > r['started_at']]
        samples = [(r['ended_at'] - r['started_at']) / 60 for r in rows[-10:]]
        return max(1, math.ceil(statistics.median(samples))) if samples else 30
    minute = (now - day_start) / 60
    windows = []
    if active and active.get('script'):
        start = (float(active.get('started') or now) - day_start) / 60
        windows.append({'start_min': minute,
                        'end_min': max(minute + 5, start + duration(active['script'], active.get('label'))),
                        'label': active.get('label') or '当前任务'})
    if not plan or plan.get('day_start') != day_start:
        return windows
    states = (state or {}).get('blocks', []) if (state or {}).get('day_start') == day_start else []
    cursor = max([minute] + [w['end_min'] for w in windows])
    for block in sorted(plan.get('blocks', []), key=lambda b: b['start_min']):
        if block.get('kind') not in ('daily', 'workflow'):
            continue
        receipt = next((r for r in states if r.get('kind') == block['kind']
                        and r.get('start_min') == block['start_min']
                        and r.get('workflow_id') == block.get('workflow_id')), {})
        if receipt.get('status') in ('completed', 'success', 'ended', 'interrupted', 'missed',
                                     'failed', 'cancelled', 'blocked', 'running'):
            continue
        script = 'daily' if block['kind'] == 'daily' else 'workflow'
        label = '一键日课' if script == 'daily' else workflow_name(block['workflow_id'])
        start = max(cursor, block['start_min'])
        end = start + duration(script, label if script == 'workflow' else None)
        windows.append({'start_min': start, 'end_min': end, 'label': label or '任务流'})
        cursor = end
    return windows


def next_dispatch(minute, windows):
    # Only dispatch occupies the game; the expedition itself can run in parallel.
    for window in sorted(windows, key=lambda w: w['start_min']):
        if minute < window['end_min'] and minute + 5 > window['start_min']:
            minute = math.ceil(window['end_min'])
    return minute
