from unittest.mock import patch

from touken.selection_identity import identity_target, selected_serial, team_cleared, identity_unique
from touken.flows.formation_editor import decide_match


def event(body, endpoint='/party/list', epoch=101):
    return {'payload': {'status': 0, **body}, 'endpoint': endpoint,
            'status': 200, 'direction': 'S->C', 'epoch_test': epoch}


def test_clear_confirmation_requires_fresh_complete_target_team_response():
    slots = {str(n): {'serial_id': None} for n in range(1, 7)}
    cleared = event({'party': {'2': {'slot': slots}}}, '/party/dissolution')
    with patch('touken.selection_identity.youzu_log._event_epoch',
               side_effect=lambda e: e['epoch_test']):
        assert team_cleared([cleared], 2, 100)
        assert not team_cleared([cleared], 3, 100)
        assert not team_cleared([cleared], 2, 102)
        newer = event({'2': {'slot': {**slots, '1': {'serial_id': '123'}}}},
                      '/party/setsword', 102)
        assert not team_cleared([cleared, newer], 2, 100)
        slots.pop('6')
        assert not team_cleared([cleared], 2, 100)
        slots['6'] = {}
        assert not team_cleared([cleared], 2, 100)
        slots['6'] = {'serial_id': None}
        cleared['payload']['status'] = 1
        assert not team_cleared([cleared], 2, 100)


def test_equipped_base_scout_is_never_compared_to_displayed_bonus():
    sword = {'sword_id': '3', 'level': '35', 'ranbu_level': '9',
             'hp_max': '50', 'scout': '35', 'horse_serial_id': '123'}
    with patch('touken.selection_identity.youzu_log._event_epoch',
               side_effect=lambda e: e['epoch_test']):
        target = identity_target([event({'sword': {'1': sword}})], 1, 100)
        assert target['recon'] is None
        assert target['survival_max'] == 50
        sword['horse_serial_id'] = None
        assert identity_target([event({'sword': {'1': sword}})], 1, 100)['recon'] == 35
        sword['artifact_serial_id1'] = '4'
        assert identity_target([event({'sword': {'1': sword}})], 1, 100)['survival_max'] is None
        assert identity_target([event({'sword': {'1': sword}}, epoch=99)], 1, 100) is None


def test_post_selection_reads_requested_slot_and_rejects_old_or_failed_response():
    body = {'2': {'slot': {'3': {'serial_id': '456'}, '1': {'serial_id': '999'}}}}
    with patch('touken.selection_identity.youzu_log._event_epoch',
               side_effect=lambda e: e['epoch_test']):
        assert selected_serial([event(body, '/party/setsword')], 2, 3, 100) == 456
        assert selected_serial([event(body, '/party/setsword', 99)], 2, 3, 100) is None
        failed = event(body, '/party/setsword')
        failed['payload']['status'] = 1
        assert selected_serial([failed], 2, 3, 100) is None


def test_ranbu_disambiguates_but_identical_copies_remain_ambiguous():
    target = {'name': '狮子王', 'sword_catalog_id': 'lion', 'level': 1,
              'tou_level': 2, 'survival_max': 45, 'recon': 25}
    fields = ('name', 'level', 'tou_level', 'survival_max', 'recon')
    rows = [{**target, 'y': 200}, {**target, 'tou_level': 1, 'y': 300},
            {**target, 'tou_level': 1, 'y': 400}]
    assert decide_match([rows], target, fields)['status'] == 'unique'
    target['tou_level'] = 1
    assert decide_match([rows], target, fields)['status'] == 'ambiguous'
    target['tou_level'] = 2
    rows[1]['tou_level'] = None
    rows[1]['fatigue'] = rows[0]['fatigue'] = 49
    assert decide_match([rows], target, fields)['status'] == 'ambiguous'


def test_existing_client_member_skips_replacement_without_new_set_response():
    from test_formation_editor import _std_setup, _target, _run, HASEBE
    import time
    maa, host = _std_setup()
    now = time.time()
    existing = event({'party': {'2': {'slot': {'3': {'serial_id': '456'}}}}}, epoch=now-20)
    evidence = {'sword_catalog_id': HASEBE, 'form': 'normal', 'level': 35, 'tou_level': 9}
    with patch('touken.selection_identity.client_events', return_value=[existing]), \
         patch('touken.selection_identity.youzu_log._event_epoch', side_effect=lambda e: e['epoch_test']), \
         patch('touken.selection_identity.identity_target', return_value=evidence), \
         patch.object(host, '_apply_list_filter', side_effect=AssertionError('already selected')):
        result = _run(host, target=_target(observation_id='youzu:456'))
    assert result['result'] == 'already_correct'
    cleared = event({'party': {'2': {'slot': {'3': {'serial_id': None}}}}}, '/party/dissolution', now-10)
    with patch('touken.selection_identity.youzu_log._event_epoch', side_effect=lambda e: e['epoch_test']):
        assert selected_serial([existing, cleared], 2, 3, now-300) is None
        assert selected_serial([existing], 2, 3, now-1) is None


def test_number_verification_failure_prevents_changed_result():
    from test_formation_editor import _std_setup, _target, _row, _run, _DECOY_PAGE, HASEBE
    maa, host = _std_setup(pages=[[_row('压切长谷部', 200, level=35, fatigue=49)], _DECOY_PAGE])
    read = host._read_list_page

    def enriched():
        rows, bad = read()
        for row in rows:
            row.update(tou_level=9, survival_max=50, recon=45)
        return rows, bad

    evidence = {'sword_catalog_id': HASEBE, 'form': 'normal', 'level': 35,
                'tou_level': 9, 'survival_max': 50, 'recon': 45}
    for actual, expected in ((456, 'changed'), (999, 'screen_unrecognized'),
                             (None, 'screen_unrecognized')):
        maa, host = _std_setup(pages=[[_row('压切长谷部', 200, level=35, fatigue=49)], _DECOY_PAGE])
        read = host._read_list_page
        with patch.object(host, '_read_list_page', side_effect=enriched), \
                patch('touken.selection_identity.client_events', return_value=[]), \
                patch('touken.selection_identity.identity_target', return_value=evidence), \
                patch('touken.selection_identity.identity_unique', return_value=True), \
                patch.object(host, '_scan_selection_list', side_effect=AssertionError('no full scan')), \
                patch.object(host, '_goto_page', side_effect=AssertionError('no return trip')), \
                patch('touken.selection_identity.selected_serial', side_effect=[0, actual]):
            result = _run(host, target=_target(observation_id='youzu:456'))
        assert result['result'] == expected
        assert not maa.swipes


def test_client_roster_uniqueness_uses_all_same_form_copies():
    sword = {'sword_id': '3', 'level': '35', 'ranbu_level': '9', 'hp_max': '50', 'scout': '35'}
    swords = {'456': sword, '999': {**sword, 'level': '1'}}
    events = [event({'sword': swords})]
    with patch('touken.selection_identity.youzu_log._event_epoch', side_effect=lambda e: e['epoch_test']):
        assert identity_unique(events, 456, 100)
        swords['999'] = dict(sword)
        assert not identity_unique(events, 456, 100)
        swords['999']['ranbu_level'] = '2'
        assert identity_unique(events, 456, 100)
        assert not identity_unique(events, 456, 102)


def test_client_search_only_moves_forward_until_target_and_stops_on_single_page():
    from test_formation_editor import _std_setup, _target, HASEBE, KOGI
    maa, host = _std_setup()
    target = _target()
    row = {'sword_catalog_id': HASEBE, 'name': '压切长谷部', 'name_raw': '压切长谷部',
           'level': 35, 'fatigue': 49, 'y': 200}
    decoy = [{**row, 'sword_catalog_id': KOGI, 'name': '小狐丸', 'name_raw': '小狐丸'}]
    def finish(stream):
        while True:
            try: next(stream)
            except StopIteration as result: return result.value
    with patch.object(host, '_read_list_page', side_effect=[(decoy, 0), ([row], 0)]), \
            patch.object(host, '_scrollbar_bottom', return_value=500), \
            patch('touken.flows.formation_editor.time.sleep'):
        selected, page = finish(host._find_client_target_stream(target, ('name', 'level'), 60))
        assert selected is row and page == 1
        assert len(maa.swipes) == 1
        assert maa.swipes[0][3] < maa.swipes[0][1]
    maa.swipes.clear()
    with patch.object(host, '_read_list_page', return_value=(decoy, 0)), \
            patch.object(host, '_scrollbar_bottom', return_value=None):
        assert finish(host._find_client_target_stream(target, ('name', 'level'), 60))[0] is None
        assert not maa.swipes


def test_bottom_row_reads_identity_numbers_when_rois_are_fully_visible():
    import numpy as np
    from test_formation_editor import _std_setup, _P
    from touken.flows.formation_editor import FormationEditorMixin
    maa, host = _std_setup()
    catalog = 'touken_103_urashima_kotetsu'
    row = {'sword_catalog_id': catalog, 'name': '浦岛虎彻', 'level': 37, 'y': 666}
    host._selection_identity_target = {'sword_catalog_id': catalog, 'survival_max': 57, 'recon': 91}
    with patch.object(maa, 'screenshot', return_value=np.zeros((720, 1280, 3), dtype=np.uint8)), \
         patch.object(host, '_parse_selection_rows', return_value=([row], 0)), \
         patch.object(maa, 'ocr_all', side_effect=[[], [('乱舞8级', _P(530, 628))],
                                                 [('57', _P(620, 660))], [('91', _P(900, 660))]]):
        rows, _ = FormationEditorMixin._read_list_page(host)
    assert rows[0]['tou_level'] == 8
    assert rows[0]['survival_max'] == 57
    assert rows[0]['recon'] == 91
    row['y'] = 698
    with patch.object(maa, 'screenshot', return_value=np.zeros((720, 1280, 3), dtype=np.uint8)), \
         patch.object(host, '_parse_selection_rows', return_value=([row], 0)), \
         patch.object(maa, 'ocr_all', return_value=[]) as ocr:
        FormationEditorMixin._read_list_page(host)
    assert ocr.call_count == 1  # 数值 ROI 真正越过名单底缘时才跳过。


def test_missing_name_is_recovered_from_same_frame_white_strip():
    import numpy as np
    from test_formation_editor import _std_setup, _P
    from touken.flows.formation_editor import FormationEditorMixin, parse_selection_rows
    maa, host = _std_setup()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    catalog = 'touken_035_gotou_toushirou'
    host._selection_identity_target = {'sword_catalog_id': catalog, 'survival_max': 43, 'recon': None}
    full = [('乱码', _P(160, 536)), ('刀剑98级', _P(530, 473)),
            ('疲劳91/100', _P(530, 536))]
    def recognize(roi, image=None):
        if roi.x == 60:
            return full
        if roi.x == 100:
            assert image is frame
            return [('后藤藤四郎', _P(161, 538))]
        if roi.x == 470:
            return [('乱舞9级', _P(530, 498))]
        return [('43', _P(623, 530))]
    with patch.object(maa, 'screenshot', return_value=frame), \
         patch.object(maa, 'ocr_all', side_effect=recognize), \
         patch.object(host, '_parse_selection_rows', side_effect=parse_selection_rows):
        rows, _ = FormationEditorMixin._read_list_page(host)
    target = next(row for row in rows if row['sword_catalog_id'] == catalog)
    assert target['level'] == 98
    assert target['tou_level'] == 9
    assert target['survival_max'] == 43
