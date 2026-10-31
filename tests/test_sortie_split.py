"""同一出阵循环可由独立画面适配驱动，安全步骤与同圈重试仍在公共编排中。"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from touken.flows.sortie import SortieLoopMixin, SortieMixin


class AlternateViewHost(SortieLoopMixin):
    """没有 device/config/像素坐标的宿主，验证循环不会暗中借用国服画面。"""

    def __init__(self, block=None):
        self.current_location = None
        self.actions = []
        self.events = []
        self.block = block
        self.rounds = 0

    def _sortie_config(self, cfg_key, map_type):
        return ({'skip_tap': 'skip'},
                {'chapters': {'1': 'chapter'}, 'maps': {'1': 'map'}},
                {'3': 'team'})

    def navigate_to_stream(self, destination):
        self.current_location = destination
        self.actions.append(('navigate', destination))
        yield '到达目标'

    def _expedition_takeover_requested(self):
        return False

    def _yosari_watch_dir(self, cfg_key):
        return None

    def _sortie_capture_frame(self):
        self.actions.append(('frame',))

    def _on_team_select_page(self, cfg):
        return False

    def _sortie_find_deploy(self, cfg):
        return True

    def _sortie_tap(self, target):
        self.actions.append(('tap', target))

    def _wait_for_team_select(self, cfg, **kwargs):
        return True

    def _disable_auto_march(self):
        self.actions.append(('disable-auto',))
        return True

    def _safe_depart_stream(self, cfg, team_no, tag, **kwargs):
        self.actions.append(('safe-depart', team_no))
        if self.block:
            reason, self.block = self.block, None
            if reason == 'injury':
                kwargs['departure_state']['blocked_injury'] = '中伤'
            yield '安全检查拦截'
            return False, False
        assert (yield from kwargs['prepare_team_stream']())
        # 模拟安全链补刀装后再次准备委托：公共循环不得漏掉这个回调。
        assert (yield from kwargs['after_equipment_stream']())
        self.rounds += 1
        yield '完整安全链已通过'
        return True, True

    def _watch_battle_frame(self):
        self._battle_watch['battles'] = 1

    def _deny_heavy_injury_warning(self, cfg):
        return False

    def _sortie_home_visible(self, cfg):
        return True

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))


def run(host, rounds=1):
    with patch('touken.flows.sortie.time.sleep'):
        return list(host.sortie_stream(1, 1, team_no=3, auto_march=False,
                                      max_loops=rounds))


def test_two_rounds_use_shared_loop_without_cn_device_or_config():
    host = AlternateViewHost()
    run(host, rounds=2)
    assert host.rounds == 2
    assert host.actions.count(('safe-depart', 3)) == 2
    assert host.actions.count(('disable-auto',)) == 4
    assert host.actions.count(('tap', 'map')) == 2
    completed = [payload for event, payload in host.events if event == 'sortie.completed']
    assert [payload['sequence'] for payload in completed] == [1, 2]
    assert all(payload['battle_count'] == 1 for payload in completed)


def test_safety_failure_never_starts_or_records_a_completed_round():
    host = AlternateViewHost(block='safety')
    run(host)
    assert host.rounds == 0
    assert host.events == []
    assert host.actions[-1] == ('safe-depart', 3)


def test_injury_recovery_retries_same_round_through_full_safety_chain():
    host = AlternateViewHost(block='injury')
    recoveries = []

    def recover(agent, **kwargs):
        recoveries.append(kwargs)
        yield '手入完成'
        return SimpleNamespace(resume=True)

    with patch('touken.flows.sortie.recover_injury_stream', recover):
        run(host)
    assert host.actions.count(('safe-depart', 3)) == 2
    assert host.rounds == 1
    assert recoveries[0]['injury'] == '中伤'
    assert recoveries[0]['progress'].completed == 0
    assert [payload['sequence'] for event, payload in host.events
            if event == 'sortie.completed'] == [1]


def test_cn_binding_preserves_existing_user_coordinates_and_yosari_overlay():
    host = SortieMixin()
    host.config = {
        'sortie': {'skip_tap': [101, 202], 'home_ui': {'template': 'custom.png'}},
        'yosari': {'skip_tap': [303, 404]},
        'map_select': {'异去': {'chapters': {'1': [11, 22]}, 'maps': {'1': [33, 44]}}},
        'team_select': {'teams': {'3': [55, 66]}},
    }
    original = deepcopy(host.config)
    cfg, maps, teams = host._sortie_config('yosari', '异去')
    assert cfg['skip_tap'] == [303, 404]
    assert cfg['home_ui']['template'] == 'custom.png'
    assert maps['maps']['1'] == [33, 44]
    assert teams['3'] == [55, 66]
    assert host.config == original
