# -*- coding: utf-8 -*-
"""日服抓包落账：把抓来的事务（netlog 文件解析 / CDP 实时听包）写进
TelemetryStore，事件格式与国服账房一致（inventory.captured /
training.captured / forge.collected / resource.change），下游日报、
账房零改动直接可读。

落账原则：

- 时间戳一律取报文里的服务器时间 ``now``（日服为 JST，UTC+9），
  不用导入时刻——补导昨天的抓包，账也记在昨天。
- 只记实锤：资源快照、刀帐快照、锻刀开炉消耗（来自开炉请求体配方）、
  锻刀领取结果。推断不出来的收支不合成、不猜账。
- 连续重复的快照去重（同一份资源/刀帐被多个接口反复携带是常态）。
- 战斗结算（battle/battle）是密文，跳过不记。
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from touken import jp_import, sword_db
from touken.netlog import Transaction

JST = timezone(timedelta(hours=9))
# 账房展示口径沿用上海时区（UTC+8，无夏令时），与国服事件一致
DISPLAY_TZ = timezone(timedelta(hours=8))

FORGE_RECIPE_KEYS = ("charcoal", "steel", "coolant", "file")


def _parse_now(text: str | None) -> float | None:
    """'2026-10-07 03:48:16'（JST）→ epoch 秒；解析失败返回 None。"""
    if not text:
        return None
    try:
        return datetime.strptime(
            text.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).timestamp()
    except ValueError:
        return None


def _display_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _resource_snapshot(payload: dict, card: dict) -> dict | None:
    """响应里的 resource 子表 → 中文键资源快照；没有返回 None。"""
    raw = payload.get("resource")
    if not isinstance(raw, dict):
        return None
    snap = {
        card["resource_keys"].get(k, k): v
        for k, v in raw.items()
        if isinstance(v, (int, float)) and k in card["resource_keys"]
    }
    return snap or None


def _roster_fingerprint(roster: list[dict]) -> tuple:
    return tuple(
        tuple(sorted(r.items()))
        for r in roster
    )


def _record(store, ts: float, event_type: str, payload: dict,
            script: str) -> None:
    cursor = store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) "
        "VALUES (?, NULL, ?, ?, ?)",
        (ts, script, event_type, json.dumps(payload, ensure_ascii=False)))
    store._conn().commit()


class JpLedgerSession:
    """可持续喂的事务入账会话。

    去重状态（上次资源快照键 / 上次刀帐指纹）挂在会话上跨批次保持：
    手动整包导入（netlog 文件）一次喂完，CDP 实时听包来一条喂一条，
    两种喂法共用，重复快照都不会刷账。
    """

    def __init__(self, store, script: str = "jp_netlog"):
        self.store = store
        self.script = script
        self.card = jp_import.load_card()
        self.stats = {"inventory.captured": 0, "training.captured": 0,
                      "forge.started": 0, "forge.collected": 0,
                      "resource.change": 0,
                      "skipped_encrypted": 0, "skipped_no_now": 0,
                      "dedup_snapshots": 0}
        self._last_resource: tuple | None = None
        self._last_roster_fp: tuple | None = None

    def feed_all(self, transactions: list[Transaction]) -> dict:
        for tx in sorted(transactions, key=lambda t: t.ts):
            self.feed(tx)
        return self.stats

    def feed(self, tx: Transaction) -> None:
        stats = self.stats
        card = self.card
        info = jp_import.classify(tx.path or tx.url, card)
        if info is None or info.get("encrypted"):
            if info and info.get("encrypted"):
                stats["skipped_encrypted"] += 1
            return
        payload = tx.response_json()
        if not isinstance(payload, dict):
            return
        ts = _parse_now(payload.get("now"))
        if ts is None:
            # 无服务器时间的报文不参与落账（时间错了比没账更糟）
            if payload.get("resource") or payload.get("sword"):
                stats["skipped_no_now"] += 1
            return

        snap = _resource_snapshot(payload, card)
        if snap is not None:
            key = tuple(sorted(snap.items()))
            if key != self._last_resource:
                _record(self.store, ts, "inventory.captured", {
                    "captured_at": _display_time(ts),
                    "source": self.script, "resources": snap}, self.script)
                stats["inventory.captured"] += 1
                self._last_resource = key
            else:
                stats["dedup_snapshots"] += 1

        path = jp_import.endpoint_path(tx.path or tx.url)
        roster = jp_import.sword_roster(payload)
        # 已核对的所持列表决定成员；其他画面的读数只更新单振状态。
        roster_scope = "full" if path == "/party/list" else "partial"
        if roster or (roster_scope == "full" and payload.get("sword") == {}):
            fp = (roster_scope, _roster_fingerprint(roster))
            if fp != self._last_roster_fp:
                _record(self.store, ts, "training.captured", {
                    "captured_at": _display_time(ts),
                    "source": self.script, "swords": roster,
                    "roster_scope": roster_scope, "endpoint": path}, self.script)
                stats["training.captured"] += 1
                self._last_roster_fp = fp
            else:
                stats["dedup_snapshots"] += 1

        path = jp_import.endpoint_path(tx.path or tx.url)
        if path == "/forge/complete" and payload.get("sword_id"):
            name = None
            found = sword_db.find_by_id(payload["sword_id"])
            if found:
                name = found[1].get("name_zh") or found[1]["name"]
            _record(self.store, ts, "forge.collected", {
                "swords": [{
                    "name": name,
                    "sword_id": payload["sword_id"],
                    "serial_id": payload.get("serial_id"),
                    "is_first_get_sword": bool(
                        payload.get("is_first_get_sword")),
                }],
                "count": 1,
                "source": f"forge.{self.script}",
                "acquired_at": _display_time(ts)}, self.script)
            stats["forge.collected"] += 1

        if path == "/forge" and tx.request_body:
            form = urllib.parse.parse_qs(
                tx.request_body.decode("utf-8", errors="replace"))
            recipe = {key: _first_int(form.get(key))
                      for key in FORGE_RECIPE_KEYS}
            if any(recipe.values()):
                _record(self.store, ts, "forge.started", {
                    "slot_no": (_first_int(form.get("slot_no"))
                                or _first_int(form.get("slot"))),
                    **recipe,
                    "source": f"forge.{self.script}"}, self.script)
                stats["forge.started"] += 1
            for res_key in FORGE_RECIPE_KEYS:
                amount = recipe[res_key]
                if not amount:
                    continue
                cn_name = card["resource_keys"][res_key]
                _record(self.store, ts, "resource.change", {
                    "resource": cn_name, "delta": -amount,
                    "before": None, "after": None,
                    "source": f"forge.{self.script}./forge",
                    "note": f"锻刀 {cn_name} -{amount}",
                    "attribution": "confirmed"}, self.script)
                stats["resource.change"] += 1


def import_transactions(store, transactions: list[Transaction],
                        script: str = "jp_netlog") -> dict:
    """把一次抓包的事务落进账房，返回各类事件的写入统计。"""
    return JpLedgerSession(store, script).feed_all(transactions)


def _first_int(values) -> int | None:
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None
