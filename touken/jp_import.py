# -*- coding: utf-8 -*-
"""日服 netlog 导入适配：把 netlog.parse_transactions 的产出按
data/jp_endpoints.json 数据卡分类，汇总成账房可消费的结构。

只读导入：本模块不构造、不重放任何游戏请求。战斗结算
（battle/battle）为密文，仅计数不报内容。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from touken import sword_db
from touken.netlog import Transaction

_CARD_PATH = Path(__file__).parent / "data" / "jp_endpoints.json"


def load_card() -> dict:
    with open(_CARD_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def endpoint_path(url_or_path: str) -> str:
    """从完整 URL 或请求路径提取端点路径（去 query、去 host）。"""
    text = url_or_path.split("?", 1)[0]
    if "://" in text:
        text = text.split("://", 1)[1]
        slash = text.find("/")
        text = text[slash:] if slash >= 0 else "/"
    return text


def classify(path: str, card: dict | None = None) -> dict | None:
    """端点路径 -> 数据卡条目；未知端点返回 None。"""
    card = card or load_card()
    return card["endpoints"].get(endpoint_path(path))


def _sword_name(sword_id: int) -> str | None:
    found = sword_db.find_by_id(sword_id)
    if not found:
        return None
    _key, entry = found
    return entry.get("name_zh") or entry.get("name")


def _count(value) -> int | None:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 and float(value).is_integer() else None


def resource_reading(payload: dict, card: dict | None = None) -> dict | None:
    """独立的日服余额对应；稀疏响应缺项不补零，奖励列表不是库存。"""
    card = card or load_card()
    reading = {}
    raw = payload.get("resource")
    if isinstance(raw, dict):
        for key, name in card["resource_keys"].items():
            value = _count(raw.get(key))
            if value is not None:
                reading[name] = value
    currency = payload.get("currency")
    if isinstance(currency, dict):
        money = _count(currency.get("money"))
        if money is not None:
            reading["小判"] = money
        paid, free = _count(currency.get("point")), _count(currency.get("point_free"))
        # 两部分都读到才确认合计；不能把缺失的部分当零。
        if paid is not None and free is not None:
            reading["甲州金"] = paid + free
    items = payload.get("item")
    if isinstance(items, dict):
        for entry in items.values():
            if isinstance(entry, dict) and str(entry.get("consumable_id")) == "8":
                value = _count(entry.get("num"))
                if value is not None:
                    reading["加速符"] = value
    if str(payload.get("assist_item_id")) == "8":
        value = _count(payload.get("assist_item_num"))
        if value is not None:
            reading["加速符"] = value
    return reading or None


def summarize(transactions: list[Transaction]) -> dict:
    """把一次数据更新汇总成结构化摘要。

    返回键：
    - kinds: 各数据类别出现次数（按数据卡 kind 聚合）
    - unknown_paths: 数据卡之外的端点（新端点雷达，见之即补卡）
    - encrypted_battles: 密文战斗结算次数（内容不读）
    - resource: 最后一次本丸快照的资源（键已翻成中文）
    - swords: 最近一次全刀帐的振数与前若干振名录
    """
    card = load_card()
    kinds: dict[str, int] = {}
    unknown: dict[str, int] = {}
    encrypted_battles = 0
    resource: dict | None = None
    swords_count = 0
    swords_sample: list[str] = []

    for tx in transactions:
        info = classify(tx.path or tx.url, card)
        if info is None:
            unknown[endpoint_path(tx.path or tx.url)] = \
                unknown.get(endpoint_path(tx.path or tx.url), 0) + 1
            continue
        kind = info["kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
        if info.get("encrypted"):
            encrypted_battles += 1
            continue
        payload = tx.response_json()
        if not isinstance(payload, dict):
            continue
        reading = resource_reading(payload, card)
        if reading:
            resource = {**(resource or {}), **reading}
        if kind == "swords" and isinstance(payload.get("sword"), dict):
            roster = payload["sword"]
            inner = roster.get("sword") if isinstance(
                roster.get("sword"), dict) else roster
            ids = []
            for entry in inner.values():
                if isinstance(entry, dict) and "sword_id" in entry:
                    ids.append(entry["sword_id"])
            if ids:
                swords_count = len(ids)
                swords_sample = [
                    _sword_name(sid) or f"#{sid}" for sid in ids[:5]
                ]

    return {
        "kinds": kinds,
        "unknown_paths": unknown,
        "encrypted_battles": encrypted_battles,
        "resource": resource,
        "swords": {"count": swords_count, "sample": swords_sample},
    }


def sword_roster(payload: dict) -> list[dict]:
    """从 party/list、organization/index 等响应提取全刀帐名录。

    字段对齐账房 training.captured 快照格式：serial_id/sword_id/
    level/exp/ranbu_level/ranbu_exp/七项内番加成，另附中文名和疲劳。
    """
    roster = payload.get("sword")
    if not isinstance(roster, dict):
        return []
    inner = roster.get("sword") if isinstance(roster.get("sword"), dict) \
        else roster
    out = []
    for key, entry in inner.items():
        if not isinstance(entry, dict) or "sword_id" not in entry:
            continue
        record = {
            "serial_id": entry.get("serial_id") or _int_or_none(key),
            "sword_id": entry["sword_id"],
            "name": _sword_name(entry["sword_id"]),
            "level": entry.get("level"),
            "exp": entry.get("exp"),
            "ranbu_level": entry.get("ranbu_level"),
            "ranbu_exp": entry.get("ranbu_exp"),
            "fatigue": entry.get("fatigue"),
        }
        for stat in ("hp", "atk", "def", "mobile", "back", "scout", "hide"):
            record[f"{stat}_up"] = entry.get(f"{stat}_up")
        out.append(record)
    out.sort(key=lambda r: (r["sword_id"], r["serial_id"] or 0))
    return out


def _int_or_none(text) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None
