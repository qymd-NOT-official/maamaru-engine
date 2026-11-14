"""每日自动体检脉冲：到点、一天一次、忙了让路、可开关。"""
from datetime import datetime

import pytest

from panel import inbox_pulse


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm).timestamp()


class Runner:
    def __init__(self, run_id="r1"):
        self.calls = []
        self.run_id = run_id

    def start(self, *args):
        self.calls.append(args)
        return self.run_id


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(inbox_pulse, "_cfg",
                        lambda: {"enabled": True, "hour": 12, "minute": 10})
    monkeypatch.setattr(inbox_pulse.inbox_watch, "load_state",
                        lambda *a, **k: {})


def test_starts_at_the_bell_and_only_once_a_day(cfg, monkeypatch):
    runner = Runner()
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 12, 10)) == "started"
    assert runner.calls[0][0] == "inbox_scan"
    # 当天已查过（日课顺风车也算）：不再自己开游戏
    monkeypatch.setattr(inbox_pulse.inbox_watch, "load_state",
                        lambda *a, **k: {"scanned_at": at(2026, 11, 14, 9, 0)})
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 12, 11)) == "already"
    assert len(runner.calls) == 1


def test_too_early_and_too_late(cfg):
    runner = Runner()
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 9, 0)) == "early"
    # 宽限 3 小时，过了就等明天
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 15, 10)) == "started"
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 15, 11)) == "missed"


def test_busy_runner_waits_for_next_tick(cfg):
    runner = Runner(run_id=None)
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 12, 10)) == "busy"


def test_disabled_and_custom_clock(cfg, monkeypatch):
    runner = Runner()
    monkeypatch.setattr(inbox_pulse, "_cfg", lambda: {"enabled": False})
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 12, 10)) == "disabled"
    monkeypatch.setattr(inbox_pulse, "_cfg",
                        lambda: {"enabled": True, "hour": 7, "minute": 45})
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 7, 44)) == "early"
    assert inbox_pulse.maybe_autoscan("cfg", runner, at(2026, 11, 14, 7, 45)) == "started"
