# -*- coding: utf-8 -*-
"""统一的游戏记录采集管线：pull → parse → 记账 → 焚毁。

国服客户端把全部 HTTP 通信明文写进 HttpRequestCollect，游戏每次启动时
清空重写——上一局的记录必须在下次启动前拉走，否则永远丢失。原始日志里
有名字/user_code/session 凭证，阅后即焚是铁律：无论解析成败，finally 里
必须删掉本地副本。

失败允许向上抛：各调用方自己负责播报与容错（telemetry 丢了不许炸主流程）。
面板层有额外消费方（如远征观察）时通过 extra_consumers 挂进来——touken
层不许 import panel。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .runtime_paths import DEBUG_DIR, STATUS_DIR
from .telemetry import get_telemetry_store


def collect_game_records(
    adb_path, adb_address,
    extra_consumers: Iterable[Callable[[list], None]] = (),
) -> dict:
    """拉取游戏记录并落库，返回各步骤计数。

    步骤：pull_log → parse_events → build_ledger → write_ledger →
    sync_receipts → save_training_snapshot → save_home_situation →
    extra_consumers → 焚毁原档。重复 pull 不重复记账（write_ledger 按
    last_ts 去重，sync_receipts 按 receipt_key 去重，training.captured
    按 payload 去重）。
    """
    from . import youzu_log
    from .sword_receipts import sync_receipts

    path = youzu_log.pull_log(adb_path, adb_address, dest_dir=DEBUG_DIR)
    try:
        events = youzu_log.parse_events(path)
        ledger = youzu_log.build_ledger(events)
        store = get_telemetry_store()
        result = youzu_log.write_ledger(store, ledger)
        result["receipts"] = sync_receipts(events, store=store)
        result["training"] = youzu_log.save_training_snapshot(store, events)
        result["home_situation"] = youzu_log.save_home_situation(
            events, STATUS_DIR / "youzu_home_situation.json")
        result["resources"] = next((obs["reading"] for obs in reversed(ledger["observations"])
                                    if obs.get("reading")), {})
        for consumer in extra_consumers or ():
            consumer(events)
        return result
    finally:
        path.unlink(missing_ok=True)  # 阅后即焚，原始日志不留本地
