# -*- coding: utf-8 -*-
"""乱舞（习合）换算规则：data/ranbu_rules.json 的加载与查询。

数据卡自带诚实标注（_meta）：
- 每喂 1 振同名刀 = 100 习合值是国服官网公告口径，calibrated=false，
  未实测校准——换算出来的「还需几振」一律是估算（est）；
- 各等级阈值抄日服 wiki（2025-09 版，乱舞 Lv10 上限），本次抓取已核对；
- 国服当前开放上限未实测（cn_max_level=null），换算只覆盖 wiki 表有的
  Lv10，超出/算不出返回 None，不瞎编数字。
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).parent / "data" / "ranbu_rules.json"


@lru_cache(maxsize=1)
def load_rules() -> dict:
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def _as_int(value):
    """bool/怪值一律 None；整数保整数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not float(value).is_integer():
        return None
    return int(value)


def effective_rarity(catalog_id, base_rarity):
    """乱舞换算用稀有度：髭切/膝丸按 wiki 特例换算（swords.json 记稀有 2，
    乱舞表按稀有 4 计）；无特例原样返回基础稀有度。基础稀有度查不到
    （None）仍返回 None，调用方据此放弃换算。"""
    base = _as_int(base_rarity)
    if base is None:
        return None
    exc = (load_rules().get("exceptions") or {}).get(str(catalog_id or ""))
    if exc:
        override = _as_int((exc or {}).get("treat_as_rarity"))
        if override is not None:
            return override
    return base


def next_level_requirement(ranbu_level, ranbu_exp, rarity):
    """到下一级乱舞还差多少：{"need_exp", "need_swords_est"} 或 None。

    need_exp     到下一级还差的累计习合值（快照滞后导致已超阈值时钳到 0）
    need_swords_est  按数据卡 exp_per_sword（100/振，未实测）估算的还需振数

    返回 None 的诚实情形：乱舞等级读不出、稀有度查不到/表里没有、
    已满是换算表上限（ranbu_level+1 无阈值）、超出国服已知上限
    （cn_max_level 钉死后生效）。
    """
    level = _as_int(ranbu_level)
    if level is None or level < 1:
        return None
    rarity_rules = (load_rules().get("rarities") or {}).get(str(rarity))
    if not rarity_rules:
        return None
    meta = load_rules().get("_meta") or {}
    cn_max = _as_int(meta.get("cn_max_level"))
    if cn_max is not None and level >= cn_max:
        return None
    need_total = (rarity_rules.get("cumulative_exp") or {}).get(str(level + 1))
    if need_total is None:
        return None  # 满级（换算表只到 Lv10）或表外等级
    current = _as_int(ranbu_exp) or 0
    need_exp = max(0, need_total - current)
    per_sword = _as_int(meta.get("exp_per_sword")) or 100
    return {"need_exp": need_exp,
            "need_swords_est": math.ceil(need_exp / per_sword)}
