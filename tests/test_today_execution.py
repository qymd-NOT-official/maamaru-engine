import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panel import server  # register workflow nodes
from panel import day_conductor as dc, day_plan as dp, expedition_choices as ec
from panel.today_execution import compose_plan, execute_today
from tests.test_day_conductor import DAY, FakeRunner, timeline


class TodayExecutionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.paths = dict(state_path=root / 'conductor.json', plan_path=root / 'plan.json',
                          choices_path=root / 'choices.json')
        self.runner = FakeRunner()
        self.day = {**timeline(now=DAY + 600 * 60), 'expeditions': [], 'runs': [],
                    'expedition_suggestions': [{'team_no': 4, 'map_code': 'B2',
                        'start_min': 600, 'duration_min': 180,
                        'formation_id': 'preset', 'formation_name': '我的远征队',
                        'formation_signature': 'unchanged'}]}
        self.day['activity']['target_runs'] = 8
        self.cfg = patch('panel.scheduler.load_config', return_value={'automation': {}, 'state': {}})
        self.cfg.start()
        self.addCleanup(self.cfg.stop)

    def execute(self):
        return execute_today(self.runner, lambda: self.day, lambda: {}, **self.paths)

    def test_adopts_expeditions_raid_and_daily_once_and_survives_reload(self):
        self.execute()
        plan = dp.load_plan(self.paths['plan_path'])
        state = dc.load_state(self.paths['state_path'])
        self.assertEqual([b['kind'] for b in plan['blocks']], ['raid', 'daily'])
        self.assertEqual(plan['blocks'][0]['runs'], 8)
        self.assertGreater(plan['blocks'][0]['start_min'], 600)
        self.assertTrue(plan['blocks'][1]['after_raids'])
        self.assertTrue(state['blocks'][1]['after_raids'])
        forced = ec.load_choice_sets(self.paths['choices_path'])[1]
        self.assertEqual(len(forced), 1)
        self.assertEqual(next(iter(forced.values()))['formation_signature'], 'unchanged')
        original = {p: p.read_bytes() for p in self.paths.values()}
        self.day['now'] += 1800
        self.day['expedition_suggestions'][0]['start_min'] += 30
        self.execute()
        self.assertEqual({p: p.read_bytes() for p in self.paths.values()}, original)
        self.assertEqual(self.runner.calls, [])

    def test_daily_only_without_activity(self):
        self.day['activity'] = None
        self.day['expedition_suggestions'] = []
        self.execute()
        self.assertEqual(dp.load_plan(self.paths['plan_path'])['blocks'],
                         [{'start_min': 600, 'kind': 'daily'}])

    def test_already_completed_daily_is_not_repeated(self):
        self.day['runs'] = [{'script': 'daily', 'status': 'completed', 'started_at': DAY + 3600}]
        self.execute()
        self.assertEqual([b['kind'] for b in dp.load_plan(self.paths['plan_path'])['blocks']], ['raid'])

    def test_busy_and_interrupted_jobs_do_not_enable_anything(self):
        self.runner.is_running = True
        with self.assertRaisesRegex(ValueError, '正在执行'):
            self.execute()
        self.assertFalse(any(p.exists() for p in self.paths.values()))
        self.runner.is_running = False
        dc._save({'version': 2, 'day_start': DAY, 'enabled': False,
                  'blocks': [{'kind': 'raid', 'start_min': 600, 'runs': 26,
                              'status': 'interrupted', 'run_id': 'old'}]}, self.paths['state_path'])
        with self.assertRaisesRegex(ValueError, '中断'):
            self.execute()
        self.assertFalse(self.paths['choices_path'].exists())

    def test_invalid_settings_do_not_write_expeditions(self):
        with patch.object(dc, 'workflow_spec', side_effect=ValueError('设置不完整')):
            with self.assertRaisesRegex(ValueError, '设置不完整'):
                self.execute()
        self.assertFalse(any(p.exists() for p in self.paths.values()))

    def test_failed_save_restores_original_files_and_keeps_backup(self):
        for path in self.paths.values():
            path.write_text('null\n', encoding='utf-8')
        original = {p: p.read_bytes() for p in self.paths.values()}
        with patch.object(ec, '_write_sets', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(ValueError, '原来的安排已保留'):
                self.execute()
        self.assertEqual({p: p.read_bytes() for p in self.paths.values()}, original)
        self.assertEqual(self.paths['plan_path'].with_suffix('.json.bak').read_bytes(), original[self.paths['plan_path']])

    def test_saved_arrangements_are_kept_and_daily_follows_them(self):
        self.day['expedition_suggestions'] = []
        old = dp.save_plan(DAY, DAY + 24 * 3600,
                           [{'start_min': 620, 'kind': 'raid', 'runs': 4}], self.paths['plan_path'])
        plan = compose_plan(self.day, old, {})
        self.assertEqual(plan['blocks'][0], old['blocks'][0])
        self.assertEqual(plan['blocks'][1], {'start_min': 648, 'kind': 'daily', 'after_raids': True})

    def test_daily_waits_for_all_raid_blocks_even_while_parked(self):
        self.execute()
        state = dc.load_state(self.paths['state_path'])
        state['blocks'][0].update(status='running', run_id='parked')
        dc._save(state, self.paths['state_path'])
        with patch('panel.workflow_waits.resume_due'), patch('panel.workflow_waits.load', return_value={
                'parked': {'status': 'waiting'}}):
            dc.tick(DAY + 900 * 60, self.runner, lambda: self.day, lambda: {}, 'config.json',
                    lambda *args: None, self.paths['state_path'], self.paths['plan_path'])
        self.assertEqual(self.runner.calls, [])
        state['blocks'][0]['status'] = 'ended'
        dc._save(state, self.paths['state_path'])
        with patch('panel.workflow_waits.resume_due'), patch('panel.workflow_waits.load', return_value={}):
            # Actual completion beats the estimated clock: daily starts immediately.
            dc.tick(DAY + 615 * 60, self.runner, lambda: self.day, lambda: {}, 'config.json',
                    lambda *args: None, self.paths['state_path'], self.paths['plan_path'])
        self.assertEqual(self.runner.calls[0][0], 'daily')

    def test_ledger_endpoint_is_blocked(self):
        from fastapi.testclient import TestClient
        with patch.object(server, '_ledger_mode', return_value=True):
            self.assertEqual(TestClient(server.app).post('/api/today/execute').status_code, 403)


    def test_existing_daily_is_moved_after_new_raid_suggestions(self):
        self.day['expedition_suggestions'] = []
        old = dp.save_plan(DAY, None,
                           [{'start_min': 600, 'kind': 'daily'}], self.paths['plan_path'])
        plan = compose_plan(self.day, old, {})
        self.assertEqual([b['kind'] for b in plan['blocks']], ['raid', 'daily'])
        self.assertEqual(plan['blocks'][1]['start_min'], 656)
        self.assertTrue(plan['blocks'][1]['after_raids'])
        self.assertEqual(plan['event_end_at'], self.day['activity']['event_end_at'])

    def test_completed_old_raid_is_not_replayed_from_saved_plan(self):
        self.day['expedition_suggestions'] = []
        self.day['activity']['target_runs'] = 0
        old = dp.save_plan(DAY, DAY + 24 * 3600,
                           [{'start_min': 500, 'kind': 'raid', 'runs': 8}], self.paths['plan_path'])
        state = {'blocks': [{'kind': 'raid', 'start_min': 500, 'status': 'ended'}]}
        plan = compose_plan(self.day, old, {}, state)
        self.assertEqual(plan['blocks'], [{'kind': 'daily', 'start_min': 600}])

    def test_paused_expeditions_and_new_raid_collision_do_not_save(self):
        with patch('panel.scheduler.load_config', return_value={
                'automation': {'paused_until': '2999-01-01 00:00:00'}}):
            with self.assertRaisesRegex(ValueError, '暂停'):
                self.execute()
        self.assertFalse(any(p.exists() for p in self.paths.values()))
        dp.save_plan(DAY, DAY + 24 * 3600,
                     [{'start_min': 600, 'kind': 'raid', 'runs': 8}], self.paths['plan_path'])
        with self.assertRaisesRegex(ValueError, '会撞上'):
            self.execute()
        self.assertFalse(self.paths['choices_path'].exists())


    def test_next_game_day_gets_a_new_receipt_and_old_expeditions_are_kept(self):
        self.execute()
        original_forced = ec.load_choice_sets(self.paths['choices_path'])[1]
        self.day['day_start'] += 86400
        self.day['now'] += 86400
        self.day['activity']['event_end_at'] += 86400
        self.execute()
        state = dc.load_state(self.paths['state_path'])
        self.assertEqual(state['day_start'], DAY + 86400)
        forced = ec.load_choice_sets(self.paths['choices_path'])[1]
        self.assertEqual(len(forced), 2)
        self.assertTrue(set(original_forced).issubset(forced))
