# -*- coding: utf-8 -*-
"""活动开/结束提醒：纯日历比对，不开游戏。

事件证据与时间表/算盘同源：活动卡片（advisor）+ 公告候选排期。
里程碑：开始当天喊一次、结束前 3 天内喊一次、结束当天再喊一次；
同一期同一档只喊一次，状态落 status/event_reminders.json。
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from touken import event_timeline as events
from touken.runtime_paths import STATUS_DIR

STATE_PATH = STATUS_DIR / "event_reminders.json"
_ENDING_SOON_DAYS = 3


def collect_events() -> list[dict]:
    """当前已知的活动窗口 [{name, start, end}]（tz-aware datetime，可缺 end）。"""
    from touken.advisor import load_event_cards
    found: dict[tuple, dict] = {}

    def add(name, start, end):
        if not name or start is None:
            return
        key = (str(name), start.date().isoformat())
        old = found.get(key)
        if old is None or (old["end"] is None and end is not None):
            found[key] = {"name": str(name), "start": start, "end": end}

    try:
        cards = load_event_cards(STATUS_DIR)
    except Exception:
        cards = {}
    for name, card in (cards or {}).items():
        try:
            start, end, _ = events._card_window(card)
        except Exception:
            continue
        add(name, start, end)

    try:
        from .server import _load_events_calendar
        calendar, _ = _load_events_calendar()
    except Exception:
        calendar = {}
    for announcement in (calendar or {}).get("announcements", []):
        for cand in announcement.get("schedule_candidates") or []:
            add(cand.get("name"),
                events._parse_dt(cand.get("start_at")),
                events._parse_dt(cand.get("end_at")))
    return list(found.values())


def due_reminders(now: float, sent: set[str]) -> list[dict]:
    """算出本次该喊的里程碑 [{key, milestone, name, days, end_date}]。"""
    today = datetime.fromtimestamp(now, events._TZ).date()
    due = []
    for event in collect_events():
        start, end = event["start"], event["end"]
        base = f"{event['name']}|{start.date().isoformat()}"
        if start.date() == today:
            due.append({"key": f"{base}|start", "milestone": "start",
                        "name": event["name"], "days": 0, "end": end})
        if end is None:
            continue
        left = (end.date() - today).days
        if left == 0:
            due.append({"key": f"{base}|end0", "milestone": "end0",
                        "name": event["name"], "days": 0, "end": end})
        elif 1 <= left <= _ENDING_SOON_DAYS:
            # 整个「还剩 1~3 天」窗口共用一把钥匙：只在进入窗口时喊一次
            due.append({"key": f"{base}|ending", "milestone": "ending",
                        "name": event["name"], "days": left, "end": end})
    return [item for item in due if item["key"] not in sent]


def _messages(item: dict) -> tuple[str, str]:
    name = item["name"]
    if item["milestone"] == "start":
        return (f"🎉 「{name}」今天开始啦！主君要不要安排出阵呀？",
                f"「{name}」今天开始")
    end_text = f"{item['end'].month} 月 {item['end'].day} 日"
    if item["milestone"] == "end0":
        return (f"‼️ 「{name}」今天最后一天！还没收摊的赶紧了！",
                f"「{name}」今天结束")
    return (f"⏳ 「{name}」{end_text} 就收摊了（还剩 {item['days']} 天），主君抓紧～",
            f"「{name}」还剩 {item['days']} 天结束（{end_text}）")


def check_and_send(now: float | None = None, send: bool = True,
                   path=STATE_PATH) -> list[str]:
    """比对一次，把到期的里程碑喊出去；返回本次喊了什么的文字记录。"""
    now = time.time() if now is None else now
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    sent = set(state.get("sent") or [])
    fired = []
    for item in due_reminders(now, sent):
        qq, ntfy = _messages(item)
        fired.append(ntfy)
        if send:
            try:
                from .broadcaster import get_broadcaster
                bc = get_broadcaster()
                if bc:
                    bc.announce(qq, ntfy_text=ntfy, title="Maamaru Event",
                                tags="calendar",
                                priority="high" if item["milestone"] == "end0" else "default")
            except Exception as exc:
                print(f"[活动提醒] 发送失败: {exc}", flush=True)
        sent.add(item["key"])
    # 只保留仍能对上当前活动的记录，过期的钥匙随活动一起翻篇
    live_keys = {item["key"] for item in due_reminders(now, set())}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"sent": sorted(sent & live_keys)}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as exc:
        print(f"[活动提醒] 状态写入失败（不影响提醒）: {exc}", flush=True)
    return fired
