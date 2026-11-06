# -*- coding: utf-8 -*-
"""日服数据同步：把连接记录转换为独立账房事件，写进
TelemetryStore，事件格式与国服账房一致（inventory.captured /
training.captured / forge.collected / resource.change），下游日报、
账房零改动直接可读。

落账原则：

- 时间戳一律取报文里的服务器时间 ``now``（日服为 JST，UTC+9），
  不用导入时刻——同步昨天的记录，账也记在昨天。
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
    return jp_import.resource_reading(payload, card)


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
    文件更新和实时连接共用，重复快照都不会刷账。
    """

    def __init__(self, store, script: str = "jp_netlog"):
        self.store = store
        self.script = script
        self.card = jp_import.load_card()
        self.stats = {"inventory.captured": 0, "training.captured": 0,
                      "forge.started": 0, "forge.collected": 0,
                      "resource.change": 0,
                      "pumpkin.sheet_observed": 0, "training.newcomers": 0,
                      "skipped_encrypted": 0, "skipped_no_now": 0,
                      "dedup_snapshots": 0}
        self._last_resource: tuple | None = None
        self._last_roster_fp: tuple | None = None
        self._last_items = None
        self._last_sheet = None
        # 新刀差分的已知 serial 集合；None = 还没建立基线
        self._known_serials: set | None = None

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
        from .jp_items import item_reading
        items = item_reading(payload)
        if items:
            fingerprint = json.dumps(items, sort_keys=True)
            if fingerprint != self._last_items:
                _record(self.store, ts, 'items.observed', {'items': items}, self.script)
                self._last_items = fingerprint
        if snap is not None:
            key = tuple(sorted(snap.items()))
            if key != self._last_resource:
                _record(self.store, ts, "inventory.captured", {
                    "captured_at": _display_time(ts),
                    "source": self.script, "resources": snap,
                    "resource_schema": 2}, self.script)
                stats["inventory.captured"] += 1
                self._last_resource = key
            else:
                stats["dedup_snapshots"] += 1

        from .jp_home import home_observation
        if jp_import.endpoint_path(tx.path or tx.url) in ("/login/start", "/home/index"):
            observation = home_observation(payload)
            if observation:
                _record(self.store, ts, "home.observed", observation, self.script)

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

        self._feed_strike_sheet(payload, ts)
        self._feed_newcomers(roster, ts)

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


    def _feed_strike_sheet(self, payload: dict, ts: float) -> None:
        """九宫格活动（かぼちゃ大作戦）的板子状态观测。

        /sally 等响应的 event.strike.sheet 是明文：每张 sheet 9 格
        is_clear、小槌存量、奖励刀 id 全在里面——翻板判定以它为准，
        不碰加密的 battle/battle。连续重复的板子状态去重。"""
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        strike = event.get("strike")
        if not isinstance(strike, dict):
            return
        sheets = strike.get("sheet")
        if not isinstance(sheets, dict) or not sheets:
            return
        summary = {}
        for key, sheet in sheets.items():
            if not isinstance(sheet, dict):
                continue
            panels = sheet.get("panel")
            if not isinstance(panels, list):
                continue
            cleared = sum(1 for p in panels
                          if isinstance(p, dict) and p.get("is_clear"))
            summary[str(key)] = {
                "cleared": cleared,
                "total": len(panels),
                "is_full": bool(panels) and cleared == len(panels),
                "count": sheet.get("count"),
                "rest_item": sheet.get("rest_item"),
                "max_rest_item": sheet.get("max_rest_item"),
                "reward_id": sheet.get("reward_id"),
                "silhouette": sheet.get("silhouette"),
                "sheet_id": sheet.get("sheet_id"),
            }
        if not summary:
            return
        fingerprint = json.dumps(summary, sort_keys=True)
        if fingerprint == self._last_sheet:
            self.stats["dedup_snapshots"] += 1
            return
        self._last_sheet = fingerprint
        _record(self.store, ts, "pumpkin.sheet_observed", {
            "observed_at": _display_time(ts),
            "source": self.script,
            "sheets": summary,
            "clear_panel": strike.get("clear_panel"),
            "sheet_reward": strike.get("sheet_reward"),
            "point": event.get("point"),
        }, self.script)
        self.stats["pumpkin.sheet_observed"] += 1

    def _seed_known_serials(self) -> set | None:
        """新刀差分的跨会话基线：账房里最近一份全量刀帐快照的 serial 集合。

        没有基线（全新账房）返回 None——调用方把第一眼看到的刀帐当基线，
        不补录历史。"""
        try:
            rows = self.store._conn().execute(
                "SELECT payload FROM events WHERE event_type='training.captured' "
                "ORDER BY ts DESC LIMIT 20").fetchall()
        except Exception:
            return None
        for (raw,) in rows:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if data.get("roster_scope") != "full":
                continue
            serials = {int(s["serial_id"]) for s in data.get("swords") or []
                       if s.get("serial_id")}
            if serials:
                return serials
        return None

    def _feed_newcomers(self, roster: list[dict], ts: float) -> None:
        """刀帐差分录新刀：任何响应携带的刀帐里出现没见过的 serial = 新入手。

        掉落照单全收（一把不许漏）：战斗结算是密文读不了掉落 id，
        但新刀一定出现在随后的刀帐快照里（/sally、/party/list 等都带）。
        基线 = 账房最近全量快照 ∪ 本会话见过的所有 serial；
        基线陈旧（快照后玩家手动收过刀）可能把老刀报成新刀——只影响
        日志归属，不漏记，宁滥勿缺。"""
        serials = {r["serial_id"] for r in roster if r.get("serial_id")}
        if not serials:
            return
        if self._known_serials is None:
            self._known_serials = self._seed_known_serials()
            if self._known_serials is None:
                self._known_serials = set(serials)
                return
        new = sorted(serials - self._known_serials)
        self._known_serials |= serials
        if not new:
            return
        by_serial = {r["serial_id"]: r for r in roster}
        swords = [{"serial_id": s,
                   "sword_id": by_serial[s].get("sword_id"),
                   "name": by_serial[s].get("name")}
                  for s in new]
        _record(self.store, ts, "training.newcomers", {
            "swords": swords,
            "count": len(swords),
            "observed_at": _display_time(ts),
            "source": self.script}, self.script)
        self.stats["training.newcomers"] += 1


def import_transactions(store, transactions: list[Transaction],
                        script: str = "jp_netlog") -> dict:
    """把一次数据更新落进账房，返回各类事件的写入统计。"""
    return JpLedgerSession(store, script).feed_all(transactions)


def _first_int(values) -> int | None:
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None
