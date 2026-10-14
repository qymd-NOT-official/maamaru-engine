"""取消的范围、续跑防复活与旧数据兼容，均使用隔离目录。"""
import asyncio
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from panel import day_conductor as dc, expedition_choices as ec, scheduler as s, server
from panel import workflow_waits as waits
from panel.plan_control import cancel_expeditions, stop_today
from panel.today_execution import execute_today
from tests.test_day_conductor import DAY, FakeRunner, timeline
from tests.test_today_stop import isolated  # noqa: F401


def state(path, *, plan_id='today', status='pending', run_id='root', **extra):
    receipt = dict(version=2, enabled=True, day_start=DAY, plan_id=plan_id,
                   blocks=[dict(kind='daily', start_min=600, status=status, run_id=run_id)], **extra)
    if plan_id is None:
        receipt.pop('plan_id')
    dc._save(receipt, path)
    return receipt


def departures(path):
    now = time.time()
    records = {
        'owned': dict(team_no=4, map_code='B2', planned_at=now, plan_id='today'),
        'manual': dict(team_no=5, map_code='B1', planned_at=now),
        'other-plan': dict(team_no=3, map_code='C1', planned_at=now, plan_id='other'),
    }
    ec._write_sets({}, records, path)
    cfg = s._defaults()
    cfg['automation'].update(enabled=True, slot_states={key: dict(key=key, **record,
        state=s.SLOT_WAITING_BUSY, expires_at=now+900) for key, record in records.items()})
    s.save_config(cfg)
    return records


def test_close_expeditions_leaves_daily_and_unrelated_waits_running(isolated):
    before = state(isolated['state_path'], status='running')
    records = departures(isolated['choices_path'])
    waits.park('manual-wait', 'qa', {}, time.time()+900, {}, '独立任务')
    runner = Mock(is_running=True, current_run_id='root', current_execution={'plan_id': 'today'})
    cancel_expeditions(runner, disable_schedule=True, choices_path=isolated['choices_path'])
    assert dc.load_state(isolated['state_path']) == before
    runner.stop.assert_not_called()
    assert waits.load()['manual-wait']['status'] == 'waiting'
    assert not s.load_config()['automation']['enabled']
    skipped, forced = ec.load_choice_sets(isolated['choices_path'])
    assert set(skipped) == set(records) and not forced
    assert all(slot['state'] == s.SLOT_CANCELLED for slot in s.load_config()['automation']['slot_states'].values())
    assert not s.takeover_flag_path().exists()


def test_stop_today_cancels_only_its_parked_children_and_preserves_manual_worker(isolated):
    state(isolated['state_path'], status='running')
    departures(isolated['choices_path'])
    waits.park('root', 'qa', {'plan_id': 'today'}, time.time()-1, {}, '日课')
    pending = waits.load()
    pending['root']['status'] = 'launching'
    waits._save(pending)
    waits.segment_started('root', 'child')
    waits.park('foreign', 'qa', {}, time.time()-1, {}, '独立任务')
    runner = Mock(is_running=True, current_run_id='manual-running', current_execution={})
    stop_today(runner, **isolated)
    runner.stop.assert_not_called()
    assert waits.load()['root']['status'] == 'cancelled'
    assert waits.load()['foreign']['status'] == 'waiting'
    assert not waits.park('late-child', 'qa', {'plan_id': 'today', 'workflow_wait_root': 'root'},
                          time.time(), {}, '迟到的等待点')
    assert set(ec.load_choice_sets(isolated['choices_path'])[1]) == {'manual', 'other-plan'}
    assert s.load_config()['automation']['enabled']
    # 残留的另一班仍可保留小旗；停止日课不能把别人的等待一并清空。
    flag = json.loads(s.takeover_flag_path().read_text())
    assert flag['key'] in {'manual', 'other-plan'}


def test_tagged_suggestions_follow_plan_through_worker_and_park(isolated, tmp_path):
    runner = FakeRunner()
    day = {**timeline(now=DAY+600*60, activity=False), 'runs': [],
           'expedition_suggestions': [dict(team_no=4, map_code='B2', start_min=700, duration_min=180)]}
    preset = dict(id='builtin-daily', name='日课', nodes=[dict(type='signin', params={})])
    with patch.object(dc.workflow, 'find_preset', return_value=preset):
        execute_today(runner, lambda: day, lambda: {}, daily_preset=preset,
                      plan_path=tmp_path/'plan.json', **isolated)
        receipt = dc.load_state(isolated['state_path'])
        forced = ec.load_choice_sets(isolated['choices_path'])[1]
        record = next(iter(forced.values()))
        assert record['plan_id'] == receipt['plan_id']
        when = datetime.fromisoformat(record['planned_at']).timestamp()
        job = s.adhoc_due(s.load_config(), forced, when, datetime.fromtimestamp(when).date().isoformat())[0]
        assert s._new_slot(job, s.load_config(), when)['plan_id'] == receipt['plan_id']
        dc.tick(day['now'], runner, lambda: day, lambda: {}, 'qa', Mock(),
                isolated['state_path'], tmp_path/'plan.json')
    params = runner.calls[0][2]
    assert params['plan_id'] == receipt['plan_id']
    waits.park(runner.current_run_id, 'qa', params, time.time()-1, {}, '日课')
    runner.is_running = False
    runner.stop = Mock()
    stop_today(runner, **isolated)
    before = list(runner.calls)
    waits.resume_due(time.time(), runner)
    assert runner.calls == before


def test_moving_owned_departure_keeps_owner_and_stop_finds_new_key(isolated):
    state(isolated['state_path'])
    departures(isolated['choices_path'])
    ec.set_forced_adhoc(key='moved', replace_key='owned', team_no=4, map_code='B2',
                        start_min=800, duration_min=180, planned_at=datetime.now().isoformat(),
                        path=isolated['choices_path'])
    assert ec.load_choice_sets(isolated['choices_path'])[1]['moved']['plan_id'] == 'today'
    stop_today(Mock(is_running=False), **isolated)
    assert set(ec.load_choice_sets(isolated['choices_path'])[1]) == {'manual', 'other-plan'}


@pytest.mark.parametrize('known_children', [False, True])
def test_old_receipt_uses_only_explicit_child_evidence(isolated, known_children):
    state(isolated['state_path'], plan_id=None,
          **({'initial_expedition_keys': ['owned']} if known_children else {}))
    records = departures(isolated['choices_path'])
    for record in records.values():
        record.pop('plan_id', None)
    ec._write_sets({}, records, isolated['choices_path'])
    stop_today(Mock(is_running=False), **isolated)
    forced = ec.load_choice_sets(isolated['choices_path'])[1]
    assert set(forced) == ({'manual', 'other-plan'} if known_children else set(records))
    assert dc.load_state(isolated['state_path'])['blocks'][0]['status'] == 'cancelled'


def test_cancelled_authorization_wins_over_leftover_choices_after_write_failure(isolated):
    now = time.time()
    day, _ = dc.day_timeline._day_window(now)
    minute = int((now-day)/60)
    state(isolated['state_path'], status='running')
    record = dict(team_no=4, map_code='B2', start_min=minute, duration_min=180,
                  planned_at=now, plan_id='today')
    key = ec.adhoc_key(datetime.now().date().isoformat(), 4, minute)
    ec._write_sets({}, {key: record}, isolated['choices_path'])
    assert s.due_jobs(s.load_config(), now)
    runner = Mock(is_running=True, current_run_id='root', current_execution={'plan_id': 'today'})
    with patch.object(ec, '_write_sets', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            stop_today(runner, **isolated)
    assert key in ec.load_choice_sets(isolated['choices_path'])[1]
    # 模拟重启：只读落盘状态，不靠内存旗标，也不能恢复已取消的子班。
    assert s.due_jobs(s.load_config(), now) == []
    runner.stop.assert_called_once_with(cancel_waits=False)
    from panel.script_runner import ScriptRunner
    worker = ScriptRunner()
    with patch.object(worker, '_spawn') as spawn:
        assert worker.start('workflow', 'qa', {'plan_id': 'today'}) is None
    spawn.assert_not_called()
    assert not waits.park('late-new-root', 'qa', {'plan_id': 'today'}, now, {}, '迟到的等待点')


def test_manual_replacement_is_not_claimed_by_old_plan_slot(isolated):
    state(isolated['state_path'])
    records = departures(isolated['choices_path'])
    records['owned'].pop('plan_id')  # 玩家已取消原班，再单独启用同一时刻。
    ec._write_sets({}, records, isolated['choices_path'])
    stop_today(Mock(is_running=False), **isolated)
    assert 'owned' in ec.load_choice_sets(isolated['choices_path'])[1]


def test_legacy_child_keys_do_not_override_a_new_explicit_owner(isolated):
    state(isolated['state_path'], plan_id=None, initial_expedition_keys=['owned'])
    departures(isolated['choices_path'])
    stop_today(Mock(is_running=False), **isolated)
    assert 'owned' in ec.load_choice_sets(isolated['choices_path'])[1]


def test_single_cancel_stops_only_matching_dispatch_and_never_recalls_completed_team(isolated):
    records = departures(isolated['choices_path'])
    runner = Mock(is_running=True, current_execution={'scheduled': True, 'slot_key': 'owned'})
    slot = dict(key='owned', **records['owned'], toggleable=True)
    with patch.object(server, 'get_runner', return_value=runner), patch.object(
            server, '_day_timeline_payload', return_value={'expeditions': [slot]}):
        request = Mock(json=AsyncMock(return_value={'key': 'owned', 'will_run': False}))
        assert asyncio.run(server.api_set_day_expedition_slot(request))['ok']
    runner.stop.assert_called_once_with(cancel_waits=False)
    assert set(ec.load_choice_sets(isolated['choices_path'])[1]) == {'manual', 'other-plan'}
    cfg = s.load_config()
    cfg['automation']['slot_states']['manual']['state'] = s.SLOT_DISPATCHED
    s.save_config(cfg)
    cancel_expeditions(Mock(is_running=False), keys={'manual'}, choices_path=isolated['choices_path'])
    assert s.load_config()['automation']['slot_states']['manual']['state'] == s.SLOT_DISPATCHED


def test_stopping_and_rearming_gets_new_owner_without_replaying_old_work(isolated, tmp_path):
    runner = FakeRunner()
    preset = dict(id='builtin-daily', name='日课', nodes=[dict(type='signin', params={})])
    day = {**timeline(now=DAY+600*60, activity=False), 'runs': [],
           'expedition_suggestions': [dict(team_no=4, map_code='B2', start_min=700, duration_min=180)]}
    with patch.object(dc.workflow, 'find_preset', return_value=preset):
        execute_today(runner, lambda: day, lambda: {}, daily_preset=preset,
                      plan_path=tmp_path/'plan.json', **isolated)
        old = dc.load_state(isolated['state_path'])['plan_id']
        stop_today(runner, **isolated)
        execute_today(runner, lambda: day, lambda: {}, daily_preset=preset,
                      plan_path=tmp_path/'plan.json', **isolated)
    new = dc.load_state(isolated['state_path'])['plan_id']
    assert new != old
    assert {record['plan_id'] for record in ec.load_choice_sets(isolated['choices_path'])[1].values()} == {new}
    assert not runner.calls


def test_close_failure_cannot_resurrect_independent_departure_on_next_poll(isolated):
    now = time.time()
    record = dict(team_no=4, map_code='B2', start_min=600, duration_min=180, planned_at=now)
    key = ec.adhoc_key(datetime.now().date().isoformat(), 4, 600)
    ec._write_sets({}, {key: record}, isolated['choices_path'])
    runner = Mock(is_running=True, current_execution={'scheduled': True, 'slot_key': key})
    with patch.object(ec, '_write_sets', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            cancel_expeditions(runner, disable_schedule=True, choices_path=isolated['choices_path'])
    runner.stop.assert_called_once_with(cancel_waits=False)
    assert key in ec.load_choice_sets(isolated['choices_path'])[1]
    cfg = s.load_config()
    assert cfg['automation']['slot_states'][key]['state'] == s.SLOT_CANCELLED
    outcome = s.tick(cfg, s.due_jobs(cfg, now), now+30, runner_busy=False, emulator_ok=True, records={})
    assert outcome['start'] is None
    assert cfg['automation']['slot_states'][key]['state'] == s.SLOT_CANCELLED
    assert s.resolve_inflight(cfg, key, None, now+30, {}, {'key': key, 'outcome': 'failed'}) == ([], False)


def test_old_v1_plan_is_backed_up_exactly_before_cancellation(isolated):
    raw = json.dumps(dict(version=1, enabled=True, day_start=DAY,
                          blocks=[dict(start_min=600, runs=3, status='pending')])).encode()
    isolated['state_path'].write_bytes(raw)
    stop_today(Mock(is_running=False), **isolated)
    assert isolated['state_path'].with_suffix('.json.bak').read_bytes() == raw
    migrated = dc.load_state(isolated['state_path'])
    assert migrated['version'] == 2 and migrated['blocks'][0]['status'] == 'cancelled'
    assert 'plan_id' not in migrated  # 不编造旧数据的归属。


def test_failed_schedule_replacement_preserves_original_and_backup(isolated):
    cfg = s.load_config()
    cfg['automation']['enabled'] = True
    s.save_config(cfg)
    original = s._SCHED_PATH.read_bytes()
    cfg['automation']['enabled'] = False
    with patch('pathlib.Path.replace', side_effect=OSError('busy')):
        with pytest.raises(OSError):
            s.save_config(cfg)
    assert s._SCHED_PATH.read_bytes() == original
    assert s._SCHED_PATH.with_suffix('.json.bak').read_bytes() == original
