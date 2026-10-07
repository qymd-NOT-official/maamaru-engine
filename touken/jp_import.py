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


def summarize(transactions: list[Transaction]) -> dict:
    """把一次抓包的事务列表汇总成结构化摘要。

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
        if kind in ("home", "account") and isinstance(
                payload.get("resource"), dict):
            resource = {
                card["resource_keys"].get(k, k): v
                for k, v in payload["resource"].items()
                if isinstance(v, (int, float))
            }
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
            record[f"{stat}_up"] = entry.get(f"{stat}_up", 0)
        out.append(record)
    out.sort(key=lambda r: (r["sword_id"], r["serial_id"] or 0))
    return out


def _int_or_none(text) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None
