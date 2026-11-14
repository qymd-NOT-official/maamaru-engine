# -*- coding: utf-8 -*-
"""每日脉冲：收件箱自动体检 + 活动开/结束提醒。

每分钟醒一次：
  - 活动提醒纯看日历，任何时候都比对一次（内部去重，不刷屏）；
  - 收件箱体检到点（默认 12:10）且今天还没查过就自动起一个
    「收件箱体检」run（自己会开游戏进本丸，翻完一封不领）。
    runner 忙着就下分钟再试，宽限 3 小时，过了就等明天。

开关与钟点写在 panel_config.json（都可省，默认开启）：
  "inbox_watch": {"enabled": true, "hour": 12, "minute": 10}
日课里跑过体检也算数（同一份状态文件），不会一天翻两次收件箱。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

from touken.runtime_paths import PANEL_CONFIG_PATH

from . import event_reminder, inbox_watch

_DEFAULT_HOUR = 12
_DEFAULT_MINUTE = 10
_RETRY_WINDOW_MINUTES = 180
_TICK_SECONDS = 60


def _cfg() -> dict:
    try:
        return json.loads(PANEL_CONFIG_PATH.read_text(encoding="utf-8")).get("inbox_watch", {})
    except Exception:
        return {}


def maybe_autoscan(config_path: str, runner, now: float | None = None) -> str:
    """到点且今天没查过就自动体检；返回本次结论（测试与日志用）。"""
    cfg = _cfg()
    if cfg.get("enabled", True) is False:
        return "disabled"
    now = time.time() if now is None else now
    moment = datetime.fromtimestamp(now)
    try:
        hour = int(cfg.get("hour", _DEFAULT_HOUR))
        minute = int(cfg.get("minute", _DEFAULT_MINUTE))
    except (TypeError, ValueError):
        hour, minute = _DEFAULT_HOUR, _DEFAULT_MINUTE
    target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment < target:
        return "early"
    if moment > target + timedelta(minutes=_RETRY_WINDOW_MINUTES):
        return "missed"
    if inbox_watch.scanned_today(inbox_watch.load_state(), now):
        return "already"
    run_id = runner.start("inbox_scan", config_path, {})
    if not run_id:
        return "busy"
    print(f"[收件箱体检] 到点自动开查（run {run_id}）", flush=True)
    return "started"


def start_pulse(config_path: str, runner):
    def _loop():
        time.sleep(30)  # 等面板和播报器站稳再开口
        while True:
            try:
                fired = event_reminder.check_and_send()
                for text in fired:
                    print(f"[活动提醒] {text}", flush=True)
            except Exception as exc:
                print(f"[活动提醒] 比对异常: {exc}", flush=True)
            try:
                maybe_autoscan(config_path, runner)
            except Exception as exc:
                print(f"[收件箱体检] 自动检查异常: {exc}", flush=True)
            time.sleep(_TICK_SECONDS)

    thread = threading.Thread(target=_loop, daemon=True, name="inbox-pulse")
    thread.start()
    return thread
