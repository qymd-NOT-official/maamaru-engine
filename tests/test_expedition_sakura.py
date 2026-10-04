import copy
import json
import pytest
from types import SimpleNamespace
from unittest.mock import patch

from touken import expedition_sakura as training
from touken.flows.battle import BattleMixin


@pytest.fixture(autouse=True)
def no_client_device():
    with patch.object(training, 'client_events', return_value=[]):
        yield


def finish(stream):
    messages = []
    while True:
        try:
            messages.append(next(stream))
        except StopIteration as result:
            return result.value, messages


def body():
    return {'party': {'3': {'status': '1', 'slot': {
        '1': {'serial_id': '101'}, '2': {'serial_id': '102'}}}},
        'sword': {'101': {'sword_id': '3', 'protect': '1', 'level': '20',
                         'fatigue': '49', 'equip_serial_id1': '501'},
                  '102': {'sword_id': '3', 'protect': '1', 'level': '1', 'fatigue': '49'}},
        'equip': {'501': {'equip_id': '9'}}}


def stream_result(value):
    if False:
        yield
    return value


class Agent:
    def __init__(self, data, result='changed', round_done=True):
        self.data = data
        self.result = result
        self.round_done = round_done
        self.calls = []
        self._expedition_sakura_active = False
        self.maa = SimpleNamespace()
    def _save_team_record(self, cfg, number):
        self.calls.append('save')
        self.data['preset'] = {'1': {'party': training.team_snapshot(self.data, 3)}}
        return True
    def _prepare_sakura_team(self, team):
        self.calls.append('clear')
        self.data['party']['3']['slot'].pop('2', None)
        return stream_result(True)
    def ensure_team_member_stream(self, team, slot, target):
        self.calls.append(('select', target['observation_id']))
        return stream_result({'status': self.result})
    def _check_fatigue(self, *args, **kwargs):
        return stream_result(49 if kwargs.get('in_place') else 100)
    def _auto_equip(self, slot):
        return stream_result(True)
    def sortie_stream(self, **kwargs):
        self.calls.append(('sortie', kwargs))
        if self.round_done:
            self.data['sword']['101']['fatigue'] = '100'
            yield '[出阵] ✓ 全部 1 圈跑完，部队3辛苦啦，收工！'
        else:
            yield '[出阵] 重伤，绝不出阵'
    def _sakura_unequip(self):
        self.calls.append('unload')
        return stream_result(True)
    def _load_team_record_confirmed(self, cfg, number):
        self.calls.append('restore')
        self.data['party']['3']['slot']['2'] = {'serial_id': '102'}
        return True


def test_exact_members_training_keeps_backup_and_injury_setting(tmp_path):
    data = body()
    agent = Agent(data)
    with patch.object(training, 'PENDING', tmp_path / 'pending.json'), \
            patch.object(training, 'fresh_body_stream', side_effect=lambda *a, **kw: stream_result(data)), \
            patch.object(training.sword_db, 'find_game_sword', return_value=(3, {'name': '刀'}, 'normal')):
        success, _ = finish(training.train_stream(agent, 3, 'heavy'))
        assert success
        assert agent.calls[0:3] == ['save', 'clear', ('select', 'youzu:101')]
        sortie = next(c[1] for c in agent.calls if isinstance(c, tuple) and c[0] == 'sortie')
        assert sortie['auto_equip'] is False  # do not overwrite original record
        assert sortie['repair_threshold'] == 'heavy'
        assert sortie['injury_action'] == 'stop'
        assert agent.calls[-2:] == ['unload', 'restore']
        assert training.pending_restore() is None
        assert not agent._expedition_sakura_active


def test_record_missing_attachment_never_clears_team(tmp_path):
    data = body()
    data['sword']['101']['artifact_serial_id1'] = '801'
    agent = Agent(data)
    original_save = agent._save_team_record
    def save(*args):
        original_save(*args)
        del data['preset']['1']['party']['1']['artifact_serial_id1']
        return True
    agent._save_team_record = save
    with patch.object(training, 'PENDING', tmp_path / 'pending.json'), \
            patch.object(training, 'fresh_body_stream', side_effect=lambda *a, **kw: stream_result(data)), \
            patch.object(training.sword_db, 'find_game_sword', return_value=(3, {'name': '刀'}, 'normal')):
        success, _ = finish(training.train_stream(agent, 3))
    assert not success
    assert agent.calls == ['save']


def test_failed_sortie_restores_but_refuses_dispatch(tmp_path):
    data = body()
    agent = Agent(data, round_done=False)
    with patch.object(training, 'PENDING', tmp_path / 'pending.json'), \
            patch.object(training, 'fresh_body_stream', side_effect=lambda *a, **kw: stream_result(data)), \
            patch.object(training.sword_db, 'find_game_sword', return_value=(3, {'name': '刀'}, 'normal')):
        success, _ = finish(training.train_stream(agent, 3))
        assert not success
        assert agent.calls[-1] == 'restore'
        assert training.pending_restore() is None


def test_cancellation_leaves_recovery_record_and_protects_game_backup(tmp_path):
    data = body()
    agent = Agent(data)
    def prepare(team):
        data['party']['3']['slot'].pop('2', None)
        yield 'cleared'
        return True
    agent._prepare_sakura_team = prepare
    with patch.object(training, 'PENDING', tmp_path / 'pending.json'), \
            patch.object(training, 'fresh_body_stream', side_effect=lambda *a, **kw: stream_result(data)), \
            patch.object(training.sword_db, 'find_game_sword', return_value=(3, {'name': '刀'}, 'normal')):
        stream = training.train_stream(agent, 3)
        assert next(stream) == 'cleared'
        stream.close()
        pending = copy.deepcopy(training.pending_restore())
        assert pending['snapshot']['1']['serial_id'] == '101'
        assert not agent._expedition_sakura_active
        host = BattleMixin()
        assert host._save_team_record({}, 1) is False
        assert finish(training.restore_stream(agent, pending))[0]
        assert training.pending_restore() is None


def test_wrong_restore_keeps_pending_and_wrong_record_is_never_loaded(tmp_path):
    data = body()
    snapshot = training.team_snapshot(data, 3)
    pending = {'version': 1, 'team_no': 3, 'snapshot': snapshot, 'status': 'training'}
    agent = Agent(data)
    agent._save_team_record({}, 1)
    data['preset']['1']['party']['1']['serial_id'] = '999'
    data['party']['3']['slot'].pop('2')
    with patch.object(training, 'PENDING', tmp_path / 'pending.json'), \
            patch.object(training, 'fresh_body_stream', side_effect=lambda *a, **kw: stream_result(data)):
        assert not finish(training.restore_stream(agent, pending))[0]
        assert 'restore' not in agent.calls
        data['preset']['1']['party']['1']['serial_id'] = '101'
        data['party']['3']['slot']['1']['serial_id'] = '102'
        assert not finish(training.restore_stream(agent, pending))[0]
        assert 'restore' in agent.calls


def test_old_schedule_defaults_training_off_and_saves_new_flag(tmp_path):
    from panel import scheduler
    path = tmp_path / 'schedule.json'
    path.write_text(json.dumps({'automation': {'enabled': True, 'capitalist': True}}), encoding='utf-8')
    with patch.object(scheduler, '_SCHED_PATH', path):
        cfg = scheduler.load_config()
        assert cfg['automation']['sakura_before_dispatch'] is False
        assert cfg['automation']['capitalist'] is True
        cfg['automation']['sakura_before_dispatch'] = True
        scheduler.save_config(cfg)
        assert scheduler.load_config()['automation']['sakura_before_dispatch'] is True


def test_takeover_is_suppressed_only_during_owned_training(tmp_path):
    (tmp_path / 'expedition_takeover.json').write_text(json.dumps({'requested_at': 100}), encoding='utf-8')
    host = BattleMixin()
    with patch('touken.flows.battle.STATE_DIR', tmp_path):
        assert host._expedition_takeover_requested(now=101)
        host._expedition_sakura_active = True
        assert not host._expedition_takeover_requested(now=101)
        host._expedition_sakura_active = False
        assert host._expedition_takeover_requested(now=101)


def test_pending_recovery_blocks_unrelated_departure_without_game_click(tmp_path):
    path = tmp_path / 'pending.json'
    path.write_text('{"status":"training"}', encoding='utf-8')
    host = BattleMixin()
    with patch.object(training, 'PENDING', path):
        result, messages = finish(host._safe_depart_stream({}, 3, '[出阵]'))
        assert result == (False, False)
        assert '绝不出阵' in messages[-1]


def test_fresh_body_stays_on_formation_and_navigation_never_detours_home():
    data = body()
    host = SimpleNamespace(current_location='本丸')
    class Maa:
        def screenshot(self, force=False): pass
        def ocr(self, *args): return True
    host.maa = Maa()
    with patch.object(training, 'latest_party', return_value=data):
        assert finish(training.fresh_body_stream(host, 3))[0] is data
        assert host.current_location == '编队'  # no navigation interface needed
    calls = []
    host.maa.ocr = lambda *args: False
    def navigate(destination):
        calls.append(destination)
        host.current_location = destination
        return stream_result(None)
    host.navigate_to_stream = navigate
    host._select_team_confirmed = lambda team: stream_result(True)
    with patch.object(training, 'latest_party', return_value=data):
        assert finish(training.fresh_body_stream(host, 3))[0] is data
    assert calls == ['编队']
