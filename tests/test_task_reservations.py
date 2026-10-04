from types import SimpleNamespace
import pytest
from panel.task_reservations import task_windows, next_dispatch


def test_new_player_daily_reserves_half_hour_and_dispatch_waits():
    plan = {'day_start': 0, 'blocks': [{'kind': 'daily', 'start_min': 100}]}
    windows = task_windows(90 * 60, 0, plan, None, None, None, lambda _: None)
    assert windows == [{'start_min': 100, 'end_min': 130, 'label': '一键日课'}]
    assert next_dispatch(98, windows) == 130
    assert next_dispatch(90, windows) == 90  # expedition can continue during tasks


def test_named_workflow_uses_successful_history_only_and_queues_after_active():
    rows = [{'script': 'workflow', 'label': '我的日课', 'status': status,
             'started_at': 60, 'ended_at': 60 + minutes * 60}
            for status, minutes in [('completed', 10), ('completed', 14), ('failed', 100)]]
    store = SimpleNamespace(runs_between=lambda *args: rows)
    plan = {'day_start': 0, 'blocks': [{'kind': 'workflow', 'workflow_id': 'mine', 'start_min': 90}]}
    windows = task_windows(100 * 60, 0, plan, None,
        {'script': 'daily', 'started': 95 * 60}, store, lambda _: '我的日课')
    assert windows[0]['end_min'] == 125
    assert windows[1] == {'start_min': 125, 'end_min': 137, 'label': '我的日课'}


@pytest.mark.parametrize('status', ['ended', 'interrupted', 'missed', 'completed'])
def test_completed_booking_does_not_reserve_again(status):
    block = {'kind': 'daily', 'start_min': 100}
    assert not task_windows(140 * 60, 0, {'day_start': 0, 'blocks': [block]},
        {'day_start': 0, 'blocks': [{**block, 'status': status}]}, None, None, lambda _: None)
