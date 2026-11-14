# -*- coding: utf-8 -*-
"""收件箱临期提醒：邮件 30 天保存期到点前喊人，每封邮件每档只喊一次。

数据来自收件箱体检流（touken/flows/inbox_scan.py 的只读扫描）；
提醒出口走事件播报器（QQ 狐之助版 + ntfy 事实版），没配出口就只做记录。
状态落在用户数据目录 status/inbox_watch.json，重启不丢、不重复喊。
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from touken.runtime_paths import STATUS_DIR

INBOX_TTL_DAYS = 30          # 官方规则：邮件发送至收件箱 30 天后过期消失
THRESHOLDS = (3, 1)          # 剩 3 天、剩 1 天各喊一次
_LIST_CAP = 8                # 一条消息最多点名的邮件数，再多折叠成「等 N 封」

STATE_PATH = STATUS_DIR / "inbox_watch.json"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def load_state(path=STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict, path=STATE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:
        print(f"[收件箱体检] 状态写入失败（不影响提醒）: {exc}", flush=True)


def _parse_inbox_at(text: str) -> float | None:
    try:
        return datetime.strptime(text, _TIME_FORMAT).timestamp()
    except (ValueError, TypeError):
        return None


def mail_label(mail: dict) -> str:
    """邮件的称呼：刀剑认刀名，杂物用邮件自带名字，都没有就给个编号。"""
    name = ""
    if str(mail.get("item_type")) == "2":
        from touken.inbox_receipts import sword_catalog
        name = sword_catalog(mail.get("item_id"))
        if name.startswith("unknown:"):
            name = ""
    name = name or str(mail.get("name") or "").strip()
    if not name:
        name = f"邮件#{mail.get('mailbox_id', '?')}"
    try:
        count = int(mail.get("item_num") or 1)
    except (TypeError, ValueError):
        count = 1
    return f"{name}×{count}" if count > 1 else name


def days_left(mail: dict, now: float) -> float | None:
    """距过期还剩几天（不足一天按小数算）；到件时间认不出返回 None。"""
    inbox_at = _parse_inbox_at(str(mail.get("inbox_at") or ""))
    if inbox_at is None:
        return None
    expire_at = inbox_at + INBOX_TTL_DAYS * 86400
    return (expire_at - now) / 86400


def due_reminders(mails: list[dict], now: float,
                  reminded: dict) -> dict[int, list[dict]]:
    """按档位归集本次该喊的邮件；只读 reminded，不写。

    一封邮件同时跨过两档（第一次查就只剩 1 天）只进最急的那档，
    较宽的档在调用方一并标记，避免第二天补喊一次废话。
    """
    due: dict[int, list[dict]] = {}
    for mail in mails:
        left = days_left(mail, now)
        if left is None:
            continue
        hit = next((t for t in sorted(THRESHOLDS) if left <= t), None)
        if hit is None:
            continue
        seen = reminded.get(str(mail.get("mailbox_id")), [])
        # 同档或更急的档喊过才跳过；只喊过 3 天档的，剩 1 天时必须再喊
        if any(t <= hit and t in seen for t in THRESHOLDS):
            continue
        due.setdefault(hit, []).append(mail)
    return due


def _name_list(mails: list[dict]) -> str:
    names = [mail_label(m) for m in mails[:_LIST_CAP]]
    extra = len(mails) - len(names)
    text = "、".join(names)
    return f"{text} 等 {len(mails)} 封" if extra > 0 else text


def _messages(threshold: int, mails: list[dict]) -> tuple[str, str]:
    """(QQ 狐之助版, ntfy 事实版)"""
    listing = _name_list(mails)
    if threshold <= 1:
        qq = (f"🚨 急急急！收件箱有 {len(mails)} 封邮件明天前就会消失："
              f"{listing}！主君现在就去领，过期就没了！")
        ntfy = f"收件箱 {len(mails)} 封邮件明天前到期：{listing}"
    else:
        qq = (f"📮 主君主君——收件箱有 {len(mails)} 封邮件 {threshold} 天内到期："
              f"{listing}。记得上线领一下～")
        ntfy = f"收件箱 {len(mails)} 封邮件 {threshold} 天内到期：{listing}"
    return qq, ntfy


def scanned_today(state: dict, now: float | None = None) -> bool:
    """今天（本地日）是否已经体检过——日课顺风车也算数。"""
    ts = state.get("scanned_at")
    if not isinstance(ts, (int, float)):
        return False
    now = time.time() if now is None else now
    return datetime.fromtimestamp(ts).date() == datetime.fromtimestamp(now).date()


def process_scan(mails: list[dict], now: float | None = None,
                 path=STATE_PATH, send: bool = True):
    """体检结果入账 + 临期提醒。产出的每一行是给运行日志看的播报。"""
    now = time.time() if now is None else now
    state = load_state(path)
    reminded = state.get("reminded") if isinstance(state.get("reminded"), dict) else {}
    # 邮箱里已经不在的邮件（领走或过期）清掉档案，同编号新邮件当新邮件算
    live_ids = {str(m.get("mailbox_id")) for m in mails}
    reminded = {k: v for k, v in reminded.items() if k in live_ids}

    due = due_reminders(mails, now, reminded)
    for threshold in sorted(due):
        group = due[threshold]
        qq, ntfy = _messages(threshold, group)
        yield f"[收件箱体检] {ntfy}，已提醒主君"
        if send:
            try:
                from .broadcaster import get_broadcaster
                bc = get_broadcaster()
                if bc:
                    bc.announce(qq, ntfy_text=ntfy, title="Maamaru Inbox",
                                tags="warning", priority="high" if threshold <= 1 else "default")
            except Exception as exc:
                print(f"[收件箱体检] 提醒发送失败: {exc}", flush=True)
        for mail in group:
            seen = reminded.setdefault(str(mail["mailbox_id"]), [])
            for t in THRESHOLDS:
                if t >= threshold and t not in seen:
                    seen.append(t)
    if not due:
        yield "[收件箱体检] 没有临期邮件，收件箱很健康"

    state = {"scanned_at": now, "pending_count": len(mails),
             "mails": [{**m, "days_left": (round(left, 1) if (left := days_left(m, now)) is not None else None)}
                       for m in mails],
             "reminded": reminded}
    _save_state(state, path)
