"""活动开/结束提醒：开始当天、结束前 3 天窗口、结束当天，各只喊一次。"""
import json
from datetime import datetime

import pytest

from panel import event_reminder
from touken import event_timeline as events

TZ = events._TZ


def at(y, m, d, hh=12):
    return datetime(y, m, d, hh, tzinfo=TZ).timestamp()


def event(name="南瓜大作战", start=(2026, 11, 14), end=(2026, 11, 20)):
    return {"name": name,
            "start": datetime(*start, 12, tzinfo=TZ),
            "end": datetime(*end, 12, tzinfo=TZ) if end else None}


@pytest.fixture
def patch_events(monkeypatch):
    def use(items):
        monkeypatch.setattr(event_reminder, "collect_events", lambda: items)
    return use


def test_start_day_fires_once(patch_events, tmp_path):
    patch_events([event()])
    state = tmp_path / "s.json"
    fired = event_reminder.check_and_send(at(2026, 11, 14), send=False, path=state)
    assert fired == ["「南瓜大作战」今天开始"]
    assert event_reminder.check_and_send(at(2026, 11, 14, 18), send=False, path=state) == []


def test_ending_window_fires_once_then_final_day(patch_events, tmp_path):
    patch_events([event()])
    state = tmp_path / "s.json"
    # 还剩 3 天：窗口开启，喊一次
    fired = event_reminder.check_and_send(at(2026, 11, 17), send=False, path=state)
    assert fired == ["「南瓜大作战」还剩 3 天结束（11 月 20 日）"]
    # 还剩 2 天、1 天：同一扇窗，不重复
    assert event_reminder.check_and_send(at(2026, 11, 18), send=False, path=state) == []
    assert event_reminder.check_and_send(at(2026, 11, 19), send=False, path=state) == []
    # 结束当天：最后一嗓子
    fired = event_reminder.check_and_send(at(2026, 11, 20, 9), send=False, path=state)
    assert fired == ["「南瓜大作战」今天结束"]
    assert event_reminder.check_and_send(at(2026, 11, 20, 10), send=False, path=state) == []


def test_quiet_days_and_past_events(patch_events, tmp_path):
    patch_events([event()])
    state = tmp_path / "s.json"
    assert event_reminder.check_and_send(at(2026, 11, 15), send=False, path=state) == []
    # 活动整个过去了：什么都不会喊，旧钥匙随活动翻篇
    assert event_reminder.check_and_send(at(2026, 11, 25), send=False, path=state) == []
    assert json.loads(state.read_text(encoding="utf-8"))["sent"] == []


def test_open_ended_event_only_reminds_start(patch_events, tmp_path):
    patch_events([event(end=None)])
    state = tmp_path / "s.json"
    fired = event_reminder.check_and_send(at(2026, 11, 14), send=False, path=state)
    assert fired == ["「南瓜大作战」今天开始"]
    assert event_reminder.check_and_send(at(2026, 11, 18), send=False, path=state) == []


def test_messages_read_naturally(patch_events):
    patch_events([event()])
    items = event_reminder.due_reminders(at(2026, 11, 14), set())
    qq, ntfy = event_reminder._messages(items[0])
    assert "今天开始啦" in qq and "南瓜大作战" in ntfy
    items = event_reminder.due_reminders(at(2026, 11, 20), set())
    qq, ntfy = event_reminder._messages(items[0])
    assert "最后一天" in qq and "今天结束" in ntfy
