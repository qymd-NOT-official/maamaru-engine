# -*- coding: utf-8 -*-
"""乱舞换算数据卡 touken/data/ranbu_rules.json + touken/ranbu_rules.py。

阈值逐稀有度抽点核对（wiki 2025-09 版，本次抓取已对过简易版/累计行）；
髭切/膝丸特例、满级 null、未实测标注的诚实性也在此盯死。
"""

from touken import ranbu_rules
from touken import sword_db


def test_meta_honesty_marks():
    meta = ranbu_rules.load_rules()["_meta"]
    assert meta["exp_per_sword"] == 100
    assert meta["exp_per_sword_calibrated"] is False
    assert meta["cn_max_level"] is None
    assert meta["max_table_level"] == 10
    assert "touken.youzu.com" in meta["sources"]["exp_per_sword"]
    assert "wikiwiki.jp/toulove" in meta["sources"]["thresholds"]


def test_cumulative_thresholds_spot_check():
    rules = ranbu_rules.load_rules()["rarities"]
    # 累计到 Lv10 的习合值 = 任务给定总振数 × 100
    assert rules["1"]["cumulative_exp"]["10"] == 11200
    assert rules["2"]["cumulative_exp"]["10"] == 10400
    assert rules["3"]["cumulative_exp"]["10"] == 7600
    assert rules["4"]["cumulative_exp"]["10"] == 5200
    assert rules["5"]["cumulative_exp"]["10"] == 3900
    # 每级所需振数抽点
    assert rules["4"]["swords_per_level"] == {
        "2": 2, "3": 3, "4": 3, "5": 3, "6": 7, "7": 7, "8": 7,
        "9": 10, "10": 10}
    assert rules["5"]["swords_per_level"]["10"] == 8
    assert rules["1"]["swords_per_level"]["6"] == 16
    # 累计中间值抽点（稀有 3：2,7,12,17,28,39,50,63,76）
    assert rules["3"]["cumulative_exp"] == {
        "2": 200, "3": 700, "4": 1200, "5": 1700, "6": 2800,
        "7": 3900, "8": 5000, "9": 6300, "10": 7600}


def test_next_level_requirement_per_rarity():
    # 稀有 5（三日月）：Lv1 起步差整 200
    assert ranbu_rules.next_level_requirement(1, 0, 5) == {
        "need_exp": 200, "need_swords_est": 2}
    # 稀有 3：Lv2→Lv3 阈值 700，已有 650 → 差 50 → 估 1 振
    assert ranbu_rules.next_level_requirement(2, 650, 3) == {
        "need_exp": 50, "need_swords_est": 1}
    # 稀有 1：Lv9→Lv10 阈值 11200
    assert ranbu_rules.next_level_requirement(9, 11000, 1) == {
        "need_exp": 200, "need_swords_est": 2}


def test_next_level_requirement_max_level_is_null():
    # 换算表 Lv10 封顶：已满级，再往上没有阈值 → null（诚实说算不出）
    assert ranbu_rules.next_level_requirement(10, 5200, 4) is None
    assert ranbu_rules.next_level_requirement(10, 99999, 5) is None
    # 表外等级同样 null
    assert ranbu_rules.next_level_requirement(11, 0, 5) is None


def test_next_level_requirement_unknown_inputs_are_null():
    assert ranbu_rules.next_level_requirement(None, 0, 5) is None
    assert ranbu_rules.next_level_requirement(1, 0, None) is None
    assert ranbu_rules.next_level_requirement(1, 0, 9) is None  # 表外稀有度


def test_next_level_requirement_snap_lag_clamps_to_zero():
    # 快照滞后：习合值已到阈值但等级还没刷新 → need 钳 0，不返回负数
    assert ranbu_rules.next_level_requirement(1, 300, 5) == {
        "need_exp": 0, "need_swords_est": 0}


def test_genji_brothers_follow_rarity_four_exception():
    rules = ranbu_rules.load_rules()
    for catalog in ("touken_107_higekiri", "touken_112_hizamaru"):
        assert catalog in rules["exceptions"]
        assert rules["exceptions"][catalog]["treat_as_rarity"] == 4
    # swords.json 记稀有 2，乱舞换算按稀有 4
    assert ranbu_rules.effective_rarity("touken_107_higekiri", 2) == 4
    assert ranbu_rules.effective_rarity("touken_112_hizamaru", 2) == 4
    # 髭切 Lv1 → 稀有 4 阈值 200；若误按稀有 2 会是 200 同值，用 Lv5 区分：
    # 稀有 4 到 Lv6 累计 1800，稀有 2 到 Lv6 累计 3800
    assert ranbu_rules.next_level_requirement(5, 1700, 4) == {
        "need_exp": 100, "need_swords_est": 1}
    # 普通刀不受特例影响
    assert ranbu_rules.effective_rarity("touken_003_mikazuki_munechika", 5) == 5
    assert ranbu_rules.effective_rarity("whatever", 3) == 3
    assert ranbu_rules.effective_rarity("whatever", None) is None


def test_sword_db_rarity_for_test_swords():
    # 测试用刀的基础稀有度（防数据卡与名册悄悄漂移）
    assert sword_db.find_game_sword(3)[1]["rarity"] == 5    # 三日月宗近
    assert sword_db.find_game_sword(65)[1]["rarity"] == 3   # 蜻蛉切
    assert sword_db.find_game_sword(107)[1]["rarity"] == 2  # 髭切（特例按 4）
    # 极化番号映射回基础目录：同一份稀有度
    assert sword_db.find_game_sword(4)[0] == "touken_003_mikazuki_munechika"
    assert sword_db.find_game_sword(111)[0] == "touken_107_higekiri"
