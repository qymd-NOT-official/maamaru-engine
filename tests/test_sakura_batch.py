from unittest.mock import patch

import numpy as np

from touken.flows.sakura import SakuraMixin
from touken.maa_adapter import Point


def test_sakura_requires_confirmed_lock_and_sword_level_above_one():
    host = SakuraMixin()
    class Maa:
        text = '刀剑39级'
        def ocr_all(self, roi):
            return [(self.text, Point(530, 135))]
    host.maa = Maa()
    for lock, text, expected in (
            ('locked', '刀剑39级', True), ('locked', '刀剑1级', False),
            ('unlocked', '刀剑39级', False), ('unknown', '刀剑39级', False),
            ('locked', '乱舞9级', False), ('locked', '', False),
            ('locked', '刀剑39级刀剑40级', False)):
        host.maa.text = text
        with patch('touken.flows.formation_editor.recognize_selection_lock', return_value=lock):
            assert host._sakura_candidate_eligible(None, 200) is expected


def test_prepare_team_uses_client_clear_confirmation_not_empty_slot_ocr():
    class Maa:
        def screenshot(self, force=False): pass
        def template_match(self, *args, **kwargs): return Point(1200, 234)
        def ocr(self, *args, **kwargs): return Point(640, 29)
        def click(self, point): pass
    host = SakuraMixin()
    host.maa = Maa()
    host.current_location = '编队'
    def done(value):
        if False: yield
        return value
    def run():
        stream = host._prepare_sakura_team(2)
        while True:
            try: next(stream)
            except StopIteration as result: return result.value
    with patch.object(host, 'navigate_to_stream', return_value=iter(()), create=True), \
            patch.object(host, '_select_team_confirmed', side_effect=lambda n: done(True), create=True), \
            patch.object(host, '_read_team_page', side_effect=AssertionError('empty-slot OCR must not gate'), create=True), \
            patch('touken.flows.sakura.time.sleep'), \
            patch('touken.selection_identity.client_events', return_value=[]), \
            patch('touken.selection_identity.team_cleared', return_value=True) as confirmed:
        assert run() is True
        assert confirmed.call_args.args[1] == 2
        confirmed.return_value = False
        assert run() is False


def test_sakura_skips_ineligible_row_before_deciding():
    class Maa:
        clicks = []
        def screenshot(self, force=False):
            return None
        def ocr(self, text, roi):
            return Point(640, 30)
        def ocr_all(self, roi):
            if roi.x == 460:
                return [('疲劳49/100', Point(540, 200)), ('疲劳49/100', Point(540, 300))]
            return [('次郎太刀', Point(150, 294))]
        def click(self, point):
            self.clicks.append((point.x, point.y))
        def template_match(self, *args, **kwargs):
            return None
    host = SakuraMixin()
    host.maa = Maa()
    with patch.object(host, '_sakura_sort_list', return_value=True), \
            patch.object(host, '_sakura_candidate_eligible', side_effect=[False, True]), \
            patch.object(host, '_wait_list_closed', return_value=True, create=True), \
            patch('touken.flows.sakura.time.sleep'):
        list(host._swap_tired_in(1, 50))
    assert (1197, 160) not in host.maa.clicks
    assert (1197, 260) in host.maa.clicks


def test_selected_captain_fatigue_is_read_without_navigation_or_team_clicks():
    class Maa:
        def screenshot(self, force=False):
            pass

        def ocr(self, text, roi):
            return Point(640, 30)

        def ocr_all(self, roi):
            return [('疲劳49/100', Point(350, 196))]

    host = SakuraMixin()
    host.maa = Maa()
    # 没有导航和点击接口：选人后应只观察当前编队页。
    stream = host._check_fatigue(1, 1, in_place=True)
    try:
        next(stream)
    except StopIteration as result:
        assert result.value == 49
    else:
        raise AssertionError('unexpected navigation')


class Batch(SakuraMixin):
    def __init__(self, selections=(True, True), fatigue=(49, 80, 100, 49, 100), serials=(1, 2)):
        self.selections = iter(selections)
        self.fatigue = iter(fatigue)
        self.serials = iter(serials)
        self.calls = []
        self.equip_ok = self.unequip_ok = self.single = self.round_ok = True

    def _prepare_sakura_team(self, team):
        self.calls.append('clear')
        yield 'clear'
        return True

    def _swap_tired_in(self, slot, threshold):
        self.calls.append(('select', slot, threshold))
        yield 'select'
        return next(self.selections)

    def _check_fatigue(self, team, slot, *, in_place=False):
        yield 'read'
        return next(self.fatigue)

    def _read_team_page(self):
        return [{'slot_status': 'occupied'}] + [{'slot_status': 'empty' if self.single else 'occupied'}] * 5

    def _sakura_selected_serial(self, team):
        return next(self.serials)

    def _auto_equip(self, slot):
        self.calls.append('equip')
        yield 'equip'
        return self.equip_ok

    def _sakura_unequip(self):
        self.calls.append('unequip')
        yield 'unequip'
        return self.unequip_ok

    def _expedition_takeover_requested(self):
        return False

    def sortie_stream(self, **kw):
        assert kw['formation_mode'] == 'auto'
        assert kw['formation'] == '鱼鳞阵'
        assert kw['injury_action'] == 'stop'
        self.last_threshold = kw['repair_threshold']
        self.last_auto_equip = kw['auto_equip']
        self.calls.append('battle')
        yield (f"[出阵] ✓ 全部 1 圈跑完，部队{kw['team_no']}辛苦啦，收工！"
               if self.round_ok else '绝不出阵')


def test_batch_unloads_before_next_selection_and_respects_count():
    host = Batch()
    messages = list(host.sakura_stream(sword_count=2))
    assert host.calls == ['clear', ('select', 1, 50), 'equip', 'battle', 'battle',
                          'unequip', ('select', 1, 50), 'equip', 'battle', 'unequip']
    assert '已完成 2 振' in messages[-1]


def test_no_candidate_is_not_counted_as_finished():
    host = Batch(selections=(False,))
    assert '已完成 0 振' in list(host.sakura_stream(sword_count=2))[-1]
    assert 'battle' not in host.calls


def test_selection_unknown_is_not_reported_as_no_candidate():
    host = Batch(selections=(None,))
    messages = list(host.sakura_stream())
    assert not any('没有可选' in m for m in messages)
    assert 'battle' not in host.calls


def test_fatigue_50_and_multiple_members_do_not_depart():
    for host in (Batch(fatigue=(50,)), Batch()):
        host.single = False
        list(host.sakura_stream())
        assert 'battle' not in host.calls


def test_failed_equipment_or_battle_never_counts_completion():
    for reason in ('equip_ok', 'round_ok'):
        host = Batch()
        setattr(host, reason, False)
        messages = list(host.sakura_stream())
        assert 'unequip' not in host.calls
        assert not any('第 1 振刷到100' in m for m in messages)


def test_failed_unload_stops_before_replacing_next_sword():
    host = Batch(fatigue=(49, 100))
    host.unequip_ok = False
    list(host.sakura_stream(sword_count=2))
    assert host.calls.count(('select', 1, 50)) == 1


def test_round_limit_does_not_replace_unfinished_sword():
    host = Batch(fatigue=(49, 80))
    messages = list(host.sakura_stream(sword_count=2, max_rounds=1))
    assert '安全上限' in messages[-1]
    assert 'unequip' not in host.calls


def test_repeated_client_serial_does_not_run_again():
    host = Batch(fatigue=(49, 100, 49), serials=(1, 1))
    messages = list(host.sakura_stream(sword_count=2))
    assert host.calls.count('battle') == 1
    assert '未重复计数' in messages[-1]


def test_equipment_filters_only_click_checked_boxes():
    class Maa:
        def __init__(self):
            self.checked = {296: True, 430: False}
            self.clicks = []

        def screenshot(self, force=False):
            return np.full((720, 1280, 3), 255, dtype=np.uint8)

        def template_match(self, template, roi, threshold):
            return Point(roi.x, 96) if self.checked[roi.x] else None

        def click(self, point):
            self.clicks.append(point.x)
            self.checked[point.x] = False

    host = SakuraMixin()
    host.maa = Maa()
    with patch('touken.flows.sakura.time.sleep'):
        list(host._sakura_clear_equipment_filters())
    assert host.maa.clicks == [296]
    assert not host._sakura_blank_checkbox(None, (296, 81, 327, 112))


def test_builder_uses_captain_and_batch_size_even_with_legacy_slot():
    from panel.server import _build_sakura

    class Agent:
        def sakura_stream(self, **kwargs):
            self.kwargs = kwargs
            return iter(())

    agent = Agent()
    list(_build_sakura(agent, 'unused', {'team_no': '4', 'slot': '6', 'sword_count': '3'}))
    assert agent.kwargs == {'team_no': 4, 'slot': 1, 'sword_count': 3, 'repair_threshold': 'light'}
    list(_build_sakura(agent, 'unused', {'team_no': '4', 'repair_threshold': 'heavy'}))
    assert agent.kwargs['repair_threshold'] == 'heavy'


def test_sakura_passes_heavy_threshold_to_shared_departure_and_rejects_invalid():
    host = Batch(fatigue=(49, 100))
    list(host.sakura_stream(repair_threshold='heavy'))
    assert host.last_threshold == 'heavy'
    assert host.last_auto_equip is False  # ordinary sakura never saves record one
    host = Batch()
    list(host.sakura_stream(repair_threshold='invalid'))
    assert not host.calls


def test_legacy_preset_selection_does_not_silently_clear_team_one():
    from panel.server import _build_sakura
    messages = list(_build_sakura(object(), 'unused', {'team_no': 'preset:old'}))
    assert '未清队' in messages[-1]


def test_fresh_setsword_response_supplies_serial_but_old_roster_does_not(tmp_path):
    from touken import youzu_log

    host = SakuraMixin()
    host.maa = type('Maa', (), {'adb_path': 'unused', 'adb_address': 'unused'})()
    host._sakura_selection_at = 100
    event = {'direction': 'S->C', 'status': 200, 'endpoint': '/party/setsword',
             'payload': {'status': 0, '1': {'slot': {'1': {'serial_id': '456'}}}}}
    for timestamp, expected in ((101, 456), (90, None)):
        path = tmp_path / f'{timestamp}.log'
        path.write_text('fixture', encoding='utf-8')
        with patch.object(youzu_log, 'pull_log', return_value=path), \
                patch.object(youzu_log, 'parse_events', return_value=[event]), \
                patch.object(youzu_log, '_event_epoch', return_value=timestamp):
            assert host._sakura_selected_serial(1) == expected
