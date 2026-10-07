"""日服余额字段校正：先完整备份，再事务更新，重复执行不重复转换。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path


def repair_legacy_resources(store) -> dict:
    conn = store._conn()
    updates = []
    for row in conn.execute(
        "SELECT id, payload FROM events WHERE event_type='inventory.captured' "
        "AND script IN ('jp_netlog','jp_listener')"):
        payload = json.loads(row["payload"])
        if payload.get("resource_schema", 1) >= 2:
            continue
        resources = payload.get("resources") or {}
        if "小判" not in resources:
            continue
        if "委托符" in resources:
            raise ValueError("旧日服余额同时含两种标记，请先核对，未修改记录")
        resources["委托符"] = resources.pop("小判")
        payload["resource_schema"] = 2
        payload["resource_correction"] = "legacy_bill_label"
        updates.append((json.dumps(payload, ensure_ascii=False), row["id"]))
    if not updates:
        return {"updated": 0, "backup": None}
    backup_path = Path(str(store.db_path) + f".pre-resource-v2-{uuid.uuid4().hex}.bak")
    with sqlite3.connect(backup_path) as backup:
        conn.backup(backup)
    # SQLite 的备份包含完整历史及标注；失败时事务回滚，来源备份保留。
    with conn:
        conn.executemany("UPDATE events SET payload=? WHERE id=?", updates)
    return {"updated": len(updates), "backup": str(backup_path)}
