"""Issue #10: cancellation follows ownership across dispatches, workers and parked continuations."""
import asyncio
import json
import time
import threading
from unittest.mock import AsyncMock, Mock, patch

import pytest

from panel import day_conductor as dc, expedition_choices as ec, scheduler as s, server
from panel.today_execution import execute_today
from panel.plan_control import stop_today
from panel import workflow_waits as waits
from tests.test_day_conductor import FakeRunner, timeline


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(s, '_SCHED_PATH', tmp_path / 'schedule.json')
    monkeypatch.setattr(s, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(ec, 'CHOICES_PATH', tmp_path / 'choices.json')
    monkeypatch.setattr(waits, 'PATH', tmp_path / 'waits.json')
    monkeypatch.setattr(dc, 'STATE_PATH', tmp_path / 'conductor.json')
    monkeypatch.setattr(s, 'load_choice_sets', lambda: ec.load_choice_sets(ec.CHOICES_PATH))
    monkeypatch.setattr(server, 'get_runner', lambda: Mock(is_running=False, current_execution={}))
    s.save_config(s._defaults())
    return dict(state_path=tmp_path / 'conductor.json', choices_path=ec.CHOICES_PATH)


@pytest.mark.parametrize('status', ['pending', 'running', 'interrupted', 'blocked'])
def test_stop_cancels_departures_and_old_block_without_replaying(status, isolated, tmp_path):
    now = time.time()
    day_start, _ = dc.day_timeline._day_window(now)
    minute = int((now - day_start) / 60)
    old_block = dict(kind='workflow', workflow_id='builtin-daily', start_min=minute,
                     status=status, run_id='old-run')
    dc._save(dict(version=2, enabled=status in {'running', 'pending'},
                  day_start=day_start, today_execution=True, unified_today=True, plan_id='plan-a',
                  blocks=[old_block]), isolated['state_path'])
    key = ec.adhoc_key(time.strftime('%Y-%m-%d'), 4, minute)
    ec._write_sets({}, {key: dict(team_no=4, map_code='B2', start_min=minute,
                                duration_min=180, planned_at=now, plan_id='plan-a'),
        'independent': dict(team_no=5, map_code='B1', planned_at=now+3600)}, isolated['choices_path'])
    cfg = s.load_config()
    cfg['automation']['enabled'] = True
    cfg['automation']['slot_states'] = {key: dict(key=key, team_no=4, map_code='B2',
        state=s.SLOT_WAITING_BUSY, expires_at=now + 900, plan_id='plan-a')}
    s.save_config(cfg)
    s.takeover_flag_path().write_text('{}', encoding='utf-8')
    runner = FakeRunner()
    runner.is_running = status == 'running'
    runner.current_run_id = 'old-run'
    runner.stop = Mock(side_effect=lambda **kw: setattr(runner, 'is_running', False))
    with patch.object(s, 'expedition_records', return_value={}):
        stop_today(runner, now=now, **isolated)
    assert runner.stop.call_count == int(status == 'running')
    assert runner.calls == []
    assert not s.takeover_flag_path().exists()
    assert not s.has_pending_dispatch(s.load_config(), now)
    assert s.load_config()['automation']['enabled']
    skipped, forced = ec.load_choice_sets(isolated['choices_path'])
    assert key in skipped and set(forced) == {'independent'}
    assert s.load_config()['automation']['slot_states'][key]['state'] == s.SLOT_CANCELLED
    stopped = dc.load_state(isolated['state_path'])
    assert not stopped['enabled'] and stopped['blocks'][0]['status'] == 'cancelled'
    assert stopped['cancelled_arrangements'][0]['blocks'] == [old_block]
    # The previous interrupted state is still recoverable from backup.
    assert json.loads(isolated['state_path'].with_suffix('.json.bak').read_text())['blocks'] == [old_block]
    preset = dict(id='builtin-daily', name='日课', nodes=[dict(type='signin', params={})],
                  daily_ui={'plannedExpeditions': False})
    day = dict(timeline(now=now, activity=False), day_start=day_start, runs=[])
    with patch.object(dc.workflow, 'find_preset', return_value=preset):
        execute_today(runner, lambda: day, lambda: {}, daily_preset=preset,
                      plan_path=tmp_path / 'plan.json', **isolated)
    armed = dc.load_state(isolated['state_path'])
    assert armed['enabled'] and armed['blocks'][0]['status'] == 'pending'
    assert armed['plan_id'] != 'plan-a'
    assert armed['cancelled_arrangements'] == stopped['cancelled_arrangements']
    assert runner.calls == []  # New work starts only through the normal conductor.


def test_disable_cancels_existing_forced_but_allows_new_explicit_choice(isolated):
    cfg = s._defaults()
    cfg['automation']['enabled'] = True
    s.save_config(cfg)
    old = dict(team_no=4, map_code='B2', planned_at=time.time())
    ec._write_sets({}, {'old': old}, ec.CHOICES_PATH)
    request = Mock(json=AsyncMock(return_value={'automation': {'enabled': False}}))
    asyncio.run(server.api_save_schedule(request))
    assert ec.load_choice_sets(ec.CHOICES_PATH) == ({'old': old}, {})
    ec.set_forced(key='new', **old, forced=True, path=ec.CHOICES_PATH)
    assert 'new' in ec.load_choice_sets(ec.CHOICES_PATH)[1]


@pytest.mark.parametrize('change', ['disabled', 'moved', 'cancelled'])
def test_stale_ready_slot_does_not_block_daily_before_next_poll(change, isolated):
    now = time.time()
    cfg = s._defaults()
    cfg['automation'].update(enabled=True, mode='custom')
    cfg['entries'] = [dict(time=time.strftime('%H:%M', time.localtime(now)),
                           team_no=4, map_code='B2', enabled=True)]
    job = s.due_jobs(cfg, now)[0]
    s.tick(cfg, [job], now, runner_busy=False, emulator_ok=True, records={})
    assert s.has_pending_dispatch(cfg, now)
    if change == 'disabled':
        cfg['automation']['enabled'] = False
    elif change == 'moved':
        cfg['entries'][0]['time'] = time.strftime('%H:%M', time.localtime(now + 3600))
    else:
        ec.set_skipped(key=job['key'], team_no=4, map_code='B2', planned_at=now,
                       skipped=True, path=ec.CHOICES_PATH)
    # Use the real choice reader, with its original default redirected explicitly.
    with patch.object(s, 'load_choice_sets', side_effect=lambda: ec.load_choice_sets(ec.CHOICES_PATH)):
        assert not s.has_pending_dispatch(cfg, now)


def test_stop_endpoint_uses_complete_stop_service():
    from fastapi.testclient import TestClient
    runner = Mock()
    with patch.object(server, 'get_runner', return_value=runner), patch(
            'panel.plan_control.stop_today', return_value={'ok': True, 'message': '已停止'}) as stop:
        assert TestClient(server.app).post('/api/today/stop').json()['ok']
    stop.assert_called_once_with(runner)


def test_stop_still_stops_owned_worker_if_choice_save_fails(isolated):
    dc._save(dict(version=2, enabled=True, day_start=0, plan_id='plan-a',
                  blocks=[dict(kind='daily', start_min=0, status='running', run_id='owned')]), isolated['state_path'])
    old = {'key': dict(team_no=4, map_code='B2', planned_at=time.time(), plan_id='plan-a')}
    ec._write_sets({}, old, ec.CHOICES_PATH)
    runner = Mock(is_running=True, current_run_id='owned', current_execution={'plan_id': 'plan-a'})
    with patch.object(ec, '_write_sets', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            stop_today(runner, **isolated)
    runner.stop.assert_called_once_with(cancel_waits=False)
    assert ec.load_choice_sets(ec.CHOICES_PATH)[1] == old
    assert not dc.load_state(isolated['state_path'])['enabled']


def test_stop_without_daily_plan_preserves_independent_settings(isolated):
    cfg = s._defaults()
    cfg['automation'].update(enabled=True, teams=[2, 4, 5])
    s.save_config(cfg)
    ec._write_sets({}, {'old': dict(team_no=4, map_code='B2')}, ec.CHOICES_PATH)
    with patch.object(s, 'expedition_records', return_value={}):
        stop_today(Mock(), **isolated)
    assert s.load_config()['automation']['enabled']
    assert s.load_config()['automation']['teams'] == [2, 4, 5]
    assert 'old' in ec.load_choice_sets(ec.CHOICES_PATH)[1]


def test_poll_cannot_overwrite_a_player_stop_with_its_old_snapshot(isolated):
    now = time.time()
    cfg = s._defaults()
    cfg['automation'].update(enabled=True, mode='custom')
    cfg['entries'] = [dict(time=time.strftime('%H:%M', time.localtime(now)),
                           team_no=4, map_code='B2', enabled=True)]
    s.save_config(cfg)
    real_load = s.load_config
    read_started, allow_write, stop_started, stop_done = (threading.Event() for _ in range(4))
    errors = []

    class EndPoll(BaseException):
        pass

    def load():
        result = real_load()
        if threading.current_thread().name == 'issue10-poll':
            read_started.set()
            assert allow_write.wait(3)
        return result

    def poll():
        try:
            target()
        except EndPoll:
            pass
        except BaseException as exc:
            errors.append(exc)

    def stop():
        stop_started.set()
        try:
            request = Mock(json=AsyncMock(return_value={'automation': {'enabled': False}}))
            asyncio.run(server.api_save_schedule(request))
        except BaseException as exc:
            errors.append(exc)
        finally:
            stop_done.set()

    with patch.object(s.threading, 'Thread') as constructor:
        s.start_scheduler('unused', Mock())
        target = constructor.call_args.kwargs['target']
    with patch.object(s, 'load_config', side_effect=load), patch.object(
            s, 'expedition_records', return_value={}), patch.object(
            s, 'load_choice_sets', return_value=({}, {})), patch.object(
            s.time, 'sleep', side_effect=EndPoll), patch(
            'panel.script_runner.get_runner', return_value=Mock(is_running=False)):
        worker = threading.Thread(target=poll, name='issue10-poll')
        player = threading.Thread(target=stop)
        worker.start()
        try:
            assert read_started.wait(3)
            player.start()
            assert stop_started.wait(3)
            assert not stop_done.wait(.05)
        finally:
            allow_write.set()
            worker.join(3)
            if player.ident:
                player.join(3)
        assert not worker.is_alive() and not player.is_alive()
    assert not errors
    assert not real_load()['automation']['enabled']
    assert not s.takeover_flag_path().exists()
