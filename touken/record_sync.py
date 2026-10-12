# -*- coding: utf-8 -*-
"""游戏数据同步的持久化消费方。

共用 game_state 读取一次，向账房、刀账与本丸近况分发同一批事实。
临时来源的清理由共用读取入口负责；界面层可注册额外消费方。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .runtime_paths import STATUS_DIR
from .telemetry import get_telemetry_store


def collect_game_records(
    adb_path, adb_address,
    extra_consumers: Iterable[Callable[[list], None]] = (),
) -> dict:
    """同步同一批游戏事实；各消费方按数据时间和记录编号去重。"""
    from .game_state import read_events
    return consume_game_records(read_events(adb_path, adb_address), extra_consumers)


def consume_game_records(events, extra_consumers=()):
    """Persist an already read batch; callers may still use facts if storage fails."""
    from . import youzu_log
    from .game_state import resource_facts, expedition_settlements
    from .sword_receipts import sync_receipts

    ledger = youzu_log.build_ledger(events)
    store = get_telemetry_store()
    result = youzu_log.write_ledger(store, ledger)
    result["receipts"] = sync_receipts(events, store=store)
    result["training"] = youzu_log.save_training_snapshot(store, events)
    result["home_situation"] = youzu_log.save_home_situation(
        events, STATUS_DIR / "youzu_home_situation.json")
    result["resource_facts"] = resource_facts(events)
    result["resources"] = {name: fact["count"] for name, fact in result["resource_facts"].items()}
    result["expedition_settlements"] = expedition_settlements(events, 0)
    for consumer in extra_consumers or ():
        consumer(events)
    return result
