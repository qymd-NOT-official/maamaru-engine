# -*- coding: utf-8 -*-
"""刀帐盘点：图鉴解析、快照存储与老库迁移"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from touken.flows import sword_inventory as _inv_mod  # noqa: E402
from touken.flows.sword_inventory import (  # noqa: E402
    _STAT_NAMES, _parse_row, _row_key, _strip_row_scratch,
    match_name_text, parse_album_tokens,
    parse_collected, parse_date_cell, parse_levels_cell,
    parse_list_tokens, parse_owned, read_page_cells, read_row_form_fact,
    split_stats_roi, stats_from_cells)
from touken.flows.report_judge import _is_fail  # noqa: E402
from touken.flows.team_roster import load_flower_templates  # noqa: E402
from touken.telemetry import TelemetryStore  # noqa: E402

HASEBE = "touken_118_heshikiri_hasebe"   # 压切长谷部（打刀，名册基线 2）
HIGEKIRI = "touken_107_higekiri"         # 髭切（太刀，动态涨花例外）


def _tok(text, x, y):
    return (text, (x, y))


class _Pt:
    """仿 maa.ocr_all 结果里的点（.x/.y 属性）"""
    def __init__(self, x, y):
        self.x, self.y = x, y


# 一览第 1 页「安宅切」一行的真实坐标样本（2026-09-12 运行帧 OCR）。
# 等级(158) 和乱舞(175) 只差 17px，OCR 抖动就能翻序——标签锚定就是为此。
ROW_ATAGI = [
    _tok("胁", 201, 171), _tok("安宅切", 206, 215),
    _tok("刀剑", 423, 157), _tok("99 级", 495, 158),
    _tok("乱舞", 423, 174), _tok("1级", 495, 175),
    _tok("生存", 424, 197), _tok("40/40", 492, 198),
    _tok("疲劳", 425, 220), _tok("85/100", 488, 221),
    _tok("生存", 551, 167), _tok("45", 551, 206),
    _tok("打击", 603, 167), _tok("46", 604, 206),
    _tok("防御", 662, 167), _tok("54", 663, 206),
    _tok("机动", 716, 167), _tok("52", 718, 207),
    _tok("冲力", 770, 167), _tok("34", 771, 207),
    _tok("侦察", 827, 167), _tok("42", 828, 205),
    _tok("隐蔽", 883, 167), _tok("40", 883, 206),
    _tok("必杀", 936, 166), _tok("29", 937, 207),
    _tok("范围", 991, 166), _tok("狭", 989, 207),
    _tok("显现", 1062, 165), _tok("2024", 1062, 193), _tok("3/8", 1061, 216),
    _tok("裝备", 1134, 186), _tok("修行", 1207, 186),
]


class ParseRowTests(unittest.TestCase):
    def test_full_row_fields(self):
        row = _parse_row(ROW_ATAGI)
        self.assertIsNotNone(row)
        self.assertEqual(row["sword_id"], "touken_250_atagi_kiri")
        self.assertEqual(row["name_zh"], "安宅切")
        self.assertEqual(row["level"], 99)   # 刀剑等级，不是乱舞的 1
        self.assertEqual(row["tou_level"], 1)
        self.assertEqual((row["survival"], row["survival_max"]), (40, 40))
        self.assertEqual((row["fatigue"], row["fatigue_max"]), (85, 100))
        self.assertEqual(row["stats"]["生存"], 45)
        self.assertEqual(row["stats"]["机动"], 52)
        self.assertEqual(row["stats"]["必杀"], 29)
        self.assertEqual(len(row["stats"]), 8)  # 范围「狭」是汉字不占数字槽
        self.assertEqual(row["kiwame_date"], "2024-3-8")

    def test_label_anchor_beats_y_jitter(self):
        # 等级和乱舞的 token y 被抖动互换后（乱舞读到更小的 y），
        # 标签锚定仍应把 99 给刀剑等级、1 给乱舞
        tokens = [(_tok[0], _tok[1]) for _tok in ROW_ATAGI]
        swapped = []
        for t, (x, y) in tokens:
            if t == "99 级":
                swapped.append(_tok(t, 495, 176))
            elif t == "1级":
                swapped.append(_tok(t, 495, 157))
            else:
                swapped.append(_tok(t, x, y))
        row = _parse_row(swapped)
        self.assertEqual(row["level"], 99)
        self.assertEqual(row["tou_level"], 1)

    def test_multiple_copies_are_distinct_rows(self):
        # 锻刀会堆出同名刀：石切丸 34 级和石切丸 1 级是两行，指纹必须不同
        mature = _parse_row(ROW_ATAGI)
        fresh = dict(mature, level=1, tou_level=1, survival=32,
                     survival_max=32, kiwame_date="2026-8-29")
        self.assertNotEqual(_row_key(mature), _row_key(fresh))
        self.assertEqual(_row_key(fresh), _row_key(dict(fresh)))

    def test_unreadable_row_counts_as_failure(self):
        row = _parse_row([
            _tok("???", 206, 215), _tok("99 级", 495, 158), _tok("40/40", 492, 198),
        ])
        self.assertEqual(row.get("_failed"), 1)
        self.assertIsNone(row["sword_id"])

    def test_blank_line_is_neither_row_nor_failure(self):
        result = parse_list_tokens([_tok("組织图", 21, 204), _tok("8", 103, 201)])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["fail_rows"], 0)

    def test_parse_owned(self):
        self.assertEqual(parse_owned("所持刀剑 196/200"), (196, 200))
        self.assertEqual(parse_owned("197 /200"), (197, 200))
        self.assertEqual(parse_owned("没读出来"), (None, None))


# 2026-09-12 刀帐图鉴真实运行帧的 OCR 输出（序号 39~50 一屏）。
# 注意：六个「极」字标实测只读出序号 42 那个——金字小标漏读是已知限制，
# 测试样本保持真实读数，不臆造没读出的 token。
ALBUM_SCREEN = [
    _tok("短", 137, 166), _tok("短", 137, 443),
    _tok("序号.39", 201, 130), _tok("前田藤四郎", 263, 217),
    _tok("序号.40", 388, 130), _tok("前田藤四郎", 450, 217),
    _tok("序号.41", 576, 130), _tok("秋田藤四郎", 637, 218),
    _tok("序号.42", 763, 130), _tok("秋田藤四郎", 825, 221), _tok("极", 826, 302),
    _tok("序号.43", 950, 130), _tok("博多藤四郎", 1010, 216),
    _tok("序号.44", 1138, 130), _tok("博多藤四郎", 1200, 220),
    _tok("序号.45", 202, 407), _tok("乱藤四郎", 263, 482),
    _tok("序号.46", 389, 406), _tok("乱藤四郎", 450, 482),
    _tok("序号.47", 576, 407), _tok("五虎退", 638, 467),
    _tok("序号.48", 763, 407), _tok("五虎退", 823, 467),
    _tok("序号.49", 950, 407), _tok("药研藤四郎", 1012, 493),
    _tok("序号.50", 1138, 407), _tok("药研藤四郎", 1199, 494),
]


class ParseAlbumTests(unittest.TestCase):
    def test_full_screen_twelve_cells(self):
        cells = parse_album_tokens(ALBUM_SCREEN)
        self.assertEqual(len(cells), 12)
        nos = [c["no"] for c in cells]
        self.assertEqual(nos, list(range(39, 51)))

    def test_kiwame_flag_when_ocr_reads_it(self):
        cells = {c["no"]: c for c in parse_album_tokens(ALBUM_SCREEN)}
        # 同名刀的普通/极化是两个图鉴条目：39 非极、40 极
        self.assertFalse(cells[39]["kiwame"])
        self.assertEqual(cells[39]["name_zh"], "前田藤四郎")
        self.assertEqual(cells[40]["name_zh"], "前田藤四郎")
        # 已知限制：金色小「极」字 OCR 漏读率高（一屏 6 个极实测只读出 1 个），
        # 极化状态允许缺报，但对账按格数走不受影响
        self.assertTrue(cells[42]["kiwame"])

    def test_missing_name_cell_is_skipped(self):
        # 空栏（未收集）：只有序号没有名字 → 不收，宁缺勿错
        tokens = [_tok("序号.99", 201, 130), _tok("短", 137, 166)]
        self.assertEqual(parse_album_tokens(tokens), [])

    def test_parse_collected(self):
        self.assertEqual(parse_collected("收集 204/208"), (204, 208))
        self.assertEqual(parse_collected("205 /208"), (205, 208))
        self.assertEqual(parse_collected("没读出来"), (None, None))


class RowFormFactTests(unittest.TestCase):
    """一览行徽章形态事实（read_row_form_fact）：合成帧 + 仓库真模板验证
    白名单门禁与结论规则（规则本体与编队页同一套，team_roster）。

    合成帧把模板原图贴进行 1 徽章格（逐格 ROI (170,143,231,205) 内），
    匹配分必然接近 1.0，专测「结论怎么走」，不测「真机能不能读出」——
    后者靠一览同源真帧校准（_INV_PROVEN_FLOWER_COMBOS 注释里的流程）。"""

    BADGE_RECT = (170, 143, 231, 205)  # ROW_CELL_ROIS[1]["badge"]

    @classmethod
    def setUpClass(cls):
        cls.templates = load_flower_templates("resource/base")
        if not cls.templates:
            raise unittest.SkipTest("缺刀种花数模板资源")

    def _frame_with_badge(self, template_stem):
        import cv2
        import numpy as np
        img = np.full((720, 1280, 3), 235, dtype=np.uint8)
        for path, _flowers, _type in self.templates:
            if path.stem == template_stem:
                tpl = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8),
                                   cv2.IMREAD_COLOR)
                h, w = tpl.shape[:2]
                x0, y0 = self.BADGE_RECT[0] + 2, self.BADGE_RECT[1] + 2
                img[y0:y0 + h, x0:x0 + w] = tpl
                return img
        raise AssertionError(f"模板不存在 {template_stem}")

    def test_normal_when_flowers_equal_base(self):
        # 压切长谷部（打刀基线 2）+ 二花打刀徽章 → 普通
        img = self._frame_with_badge("二花打刀")
        fact = read_row_form_fact(img, self.BADGE_RECT, HASEBE, self.templates,
                                  {("打刀", 2)})
        self.assertEqual(fact["status"], "normal")
        self.assertEqual(fact["badge"]["flowers"], 2)
        self.assertEqual(fact["sword_type"], "打刀")
        self.assertEqual(fact["rarity_base"], 2)
        self.assertTrue(any("花数2/基线2" in e for e in fact["evidence"]))

    def test_kiwame_when_flowers_above_base(self):
        # 压切长谷部（基线 2）+ 三花打刀徽章 → 极化
        img = self._frame_with_badge("三花打刀")
        fact = read_row_form_fact(img, self.BADGE_RECT, HASEBE, self.templates,
                                  {("打刀", 3)})
        self.assertEqual(fact["status"], "kiwame")
        self.assertEqual(fact["badge"]["flowers"], 3)

    def test_unproven_combo_stays_unknown_but_keeps_raw_observation(self):
        # 白名单外的达标匹配：不出结论，但原始观测（花数/分数）保留落盘
        img = self._frame_with_badge("二花打刀")
        fact = read_row_form_fact(img, self.BADGE_RECT, HASEBE, self.templates,
                                  frozenset())
        self.assertEqual(fact["status"], "unknown")
        self.assertEqual(fact["evidence"], [])
        self.assertEqual(fact["badge"]["conclusion"], "unproven_combo")
        self.assertEqual(fact["badge"]["observed_flowers"], 2)
        self.assertGreaterEqual(fact["badge"]["observed_score"], 0.7)

    def test_dynamic_flower_exception_never_concludes(self):
        # 髭切/膝丸普通形态随特阶段涨花：花数>基线也不区分极化
        img = self._frame_with_badge("三花太刀")
        fact = read_row_form_fact(img, self.BADGE_RECT, HIGEKIRI, self.templates,
                                  {("太刀", 3)})
        self.assertEqual(fact["status"], "unknown")
        self.assertTrue(any("花数3/基线2" in e for e in fact["evidence"]))

    def test_no_frame_or_wrong_identity_gives_unknown(self):
        # 截图失明：不出证据
        fact = read_row_form_fact(None, self.BADGE_RECT, HASEBE, self.templates,
                                  {("打刀", 2)})
        self.assertEqual(fact["status"], "unknown")
        self.assertEqual(fact["badge"]["conclusion"], "low_score")
        # 名册没有的身份：没有确认刀种，不产生花数证据
        img = self._frame_with_badge("二花打刀")
        fact = read_row_form_fact(img, self.BADGE_RECT, "touken_999_nobody",
                                  self.templates, {("打刀", 2)})
        self.assertEqual(fact["status"], "unknown")
        self.assertEqual(fact["badge"]["conclusion"], "no_confirmed_type")


class CellParseTests(unittest.TestCase):
    """逐格精读纯函数：等级格 join 解析、数值带 9 等分、日期格、整页装配。"""

    def test_levels_cell_glued_and_split_forms_agree(self):
        glued = parse_levels_cell(
            [_tok("刀剑99级乱舞1级生存40/40疲劳85/100", 460, 180)])
        self.assertEqual((glued["level"], glued["tou_level"]), (99, 1))
        self.assertEqual((glued["survival"], glued["survival_max"]), (40, 40))
        self.assertEqual((glued["fatigue"], glued["fatigue_max"]), (85, 100))
        split = parse_levels_cell([
            _tok("刀剑", 423, 157), _tok("99 级", 495, 158),
            _tok("乱舞", 423, 174), _tok("1级", 495, 175),
            _tok("生存", 424, 197), _tok("40/40", 492, 198),
            _tok("疲劳", 425, 220), _tok("85/100", 488, 221)])
        self.assertEqual(split, glued)

    def test_levels_cell_scrambled_order_pairs_by_y(self):
        # 2026-09-20 真机实测：小格 OCR 返回顺序会乱（刀剑 乱舞 1级 99级），
        # join 正则会错配成 刀剑1级，y 锚定必须纠回来
        out = parse_levels_cell([
            _tok("生存 48/48", 460, 197), _tok("疲劳 58/100", 460, 220),
            _tok("刀剑", 423, 157), _tok("乱舞", 423, 174),
            _tok("1级", 495, 175), _tok("99 级", 495, 158)])
        self.assertEqual((out["level"], out["tou_level"]), (99, 1))
        self.assertEqual((out["survival"], out["survival_max"]), (48, 48))
        self.assertEqual((out["fatigue"], out["fatigue_max"]), (58, 100))

    def test_levels_cell_value_range_swap_insurance(self):
        # 乱舞最多十几级：刀剑1级+乱舞99级必是读串，互换
        out = parse_levels_cell([_tok("刀剑1级乱舞99级生存30/30", 460, 180)])
        self.assertEqual((out["level"], out["tou_level"]), (99, 1))

    def test_levels_cell_missing_fields_stay_none(self):
        out = parse_levels_cell([_tok("刀剑30级生存30/30", 460, 180)])
        self.assertEqual(out["level"], 30)
        self.assertIsNone(out["tou_level"])
        self.assertIsNone(out["fatigue"])
        self.assertIsNone(out["fatigue_max"])
        self.assertIsNone(parse_levels_cell([])["level"])

    def test_levels_cell_detached_grade_merges_back(self):
        # 2026-10-19 炼糖现场：滑动回弹期「2级」被拆成「2」「级」两个 token，
        # 同 y 带内数字唯一时拼回再解析
        out = parse_levels_cell([
            _tok("刀剑", 423, 157), _tok("24", 470, 158), _tok("级", 505, 158),
            _tok("乱舞", 423, 174), _tok("2", 470, 175), _tok("级", 500, 175),
            _tok("生存", 424, 197), _tok("40/40", 492, 198)])
        self.assertEqual(out["level"], 24)
        self.assertEqual(out["tou_level"], 2)

    def test_levels_cell_detached_grade_ambiguous_stays_none(self):
        # 同 y 带内有两个数字候选：不猜、不拼，「级」保持孤独
        from touken.flows.sword_inventory import (
            _merge_split_grade_tokens, _split_label_tokens)
        items = _split_label_tokens([
            _tok("刀剑", 423, 157), _tok("1", 470, 158), _tok("24", 490, 158),
            _tok("级", 520, 158)])
        texts = [t for _k, t, _x, _y in _merge_split_grade_tokens(items)]
        self.assertEqual(texts, ["刀剑", "1", "24", "级"])

    def test_split_stats_roi_nine_cells_with_inset(self):
        cells = split_stats_roi((521, 144, 1026, 231))
        self.assertEqual(len(cells), 9)
        for prev, cur in zip(cells, cells[1:]):  # 内缩后相邻格不接壤
            self.assertLess(prev[2], cur[0])
        self.assertGreaterEqual(cells[0][0], 521)
        self.assertLessEqual(cells[-1][2], 1026)
        self.assertTrue(all(c[1] == 144 and c[3] == 231 for c in cells))

    def test_stats_from_cells_eight_stats_ninth_ignored(self):
        texts = ["48", "71", "67", "38", "51", "41", "28", "37", "狭"]
        stats = stats_from_cells(texts)
        self.assertEqual([stats[k] for k in _STAT_NAMES],
                         [48, 71, 67, 38, 51, 41, 28, 37])
        self.assertEqual(len(stats), 8)  # 范围格不进 stats

    def test_stats_from_cells_tolerates_empty_cells(self):
        stats = stats_from_cells(["48", "", "糊了", "38", "51", "41", "28", "37", "广"])
        self.assertNotIn("打击", stats)
        self.assertNotIn("防御", stats)
        self.assertEqual(stats["机动"], 38)

    def test_date_cell(self):
        self.assertEqual(parse_date_cell([_tok("显现", 1062, 165),
                                          _tok("2026", 1062, 193),
                                          _tok("2/12", 1061, 216)]),
                         "2026-2-12")
        self.assertIsNone(parse_date_cell([_tok("2/12", 1061, 216)]))
        self.assertIsNone(parse_date_cell([_tok("2026", 1062, 193)]))
        self.assertIsNone(parse_date_cell([]))

    def test_date_cell_glued_tokens(self):
        # 快照 #22 的 7 把日期留空：窄格（宽≈80px）里两行小字被 OCR 粘成
        # 一个 token，分立整匹配抓不到，拼接串兜底必须接住
        self.assertEqual(parse_date_cell([_tok("20262/12", 1062, 200)]),
                         "2026-2-12")
        self.assertEqual(parse_date_cell([_tok("2026 2/12", 1062, 200)]),
                         "2026-2-12")
        self.assertEqual(parse_date_cell([_tok("显现2026", 1062, 180),
                                          _tok("2/12", 1061, 216)]),
                         "2026-2-12")
        self.assertEqual(parse_date_cell([_tok("202612/3", 1062, 200)]),
                         "2026-12-3")
        # 缺年/缺日照旧 None，不许硬编
        self.assertIsNone(parse_date_cell([_tok("2/12", 1061, 216),
                                           _tok("显现", 1062, 165)]))
        self.assertIsNone(parse_date_cell([_tok("2026", 1062, 193)]))

    def test_match_name_text(self):
        hit = match_name_text("安宅切")
        self.assertEqual(hit["sword_id"], "touken_250_atagi_kiri")
        self.assertIsNone(match_name_text(""))
        self.assertIsNone(match_name_text("裝备修行"))

    @staticmethod
    def _fake_ocr(mapping):
        return lambda roi: mapping.get(tuple(roi), [])

    def test_read_page_cells_full_row(self):
        rois = _inv_mod.ROW_CELL_ROIS[1]
        mapping = {
            rois["name"]: [_tok("安宅切", 280, 217)],
            rois["levels"]: [_tok("刀剑99级乱舞1级生存45/45疲劳100/100", 460, 180)],
            rois["date"]: [_tok("显现", 1062, 165), _tok("2026", 1062, 193),
                           _tok("7/29", 1061, 216)],
        }
        for cell, value in zip(split_stats_roi(rois["stats"]),
                               ["45", "46", "55", "52", "34", "42", "40", "29", "狭"]):
            mapping[cell] = [_tok(value, cell[0] + 5, cell[1] + 5)]
        parsed = read_page_cells(self._fake_ocr(mapping))
        self.assertEqual(parsed["fail_rows"], 0)
        self.assertEqual(len(parsed["rows"]), 1)  # 其余四行名字格空白=空行
        row = parsed["rows"][0]
        self.assertEqual(row["sword_id"], "touken_250_atagi_kiri")
        self.assertEqual((row["level"], row["tou_level"]), (99, 1))
        self.assertEqual((row["survival"], row["survival_max"]), (45, 45))
        self.assertEqual([row["stats"][k] for k in _STAT_NAMES],
                         [45, 46, 55, 52, 34, 42, 40, 29])
        self.assertEqual(row["kiwame_date"], "2026-7-29")
        self.assertEqual(row["_badge_rect"], rois["badge"])
        self.assertEqual(row["_date_roi"], rois["date"])  # 缺日期重读定位用
        self.assertEqual(row["_row_no"], 1)  # 头像核验行带定位用

    def test_read_page_cells_garbage_name_is_fail_row(self):
        rois = _inv_mod.ROW_CELL_ROIS[2]
        parsed = read_page_cells(self._fake_ocr({
            rois["name"]: [_tok("选择部队", 280, 420)],  # 部队选择页的文字
        }))
        self.assertEqual(parsed["fail_rows"], 1)
        self.assertIsNone(parsed["rows"][0]["sword_id"])

    def test_read_page_cells_blank_page_is_silent(self):
        parsed = read_page_cells(self._fake_ocr({}))
        self.assertEqual(parsed["rows"], [])
        self.assertEqual(parsed["fail_rows"], 0)

    def test_fallback_message_not_fail_worded(self):
        """兜底话术是正常播报（如实上报但流程没翻车），不许撞翻车词表"""
        self.assertFalse(_is_fail("第 3 页逐格精读一行名字都没认出，改用整列读法"))

    def test_missing_date_warning_not_fail_worded(self):
        """日期留空的收尾提示是 ⚠️ 级播报（人工可补），不许撞翻车词表"""
        self.assertFalse(_is_fail(
            "⚠️ 有 7 把刀的显现日期留空了（名字等级都在），"
            "刀帐页待核对里能人工补"))

    def test_strip_row_scratch(self):
        """落库前清掉流程暂存键：_base_y/_badge_rect/_date_roi/_row_no 不进快照"""
        rows = [{"sword_id": "x", "level": 99, "_base_y": 215,
                 "_badge_rect": (1, 2, 3, 4), "_date_roi": (5, 6, 7, 8),
                 "_row_no": 1},
                {"sword_id": "y"}]
        _strip_row_scratch(rows)
        self.assertEqual(rows, [{"sword_id": "x", "level": 99},
                                {"sword_id": "y"}])


class SwordSnapshotStoreTests(unittest.TestCase):
    def _store(self):
        return TelemetryStore(Path(tempfile.mkdtemp()) / "telemetry.db")

    def test_save_and_read_roundtrip(self):
        store = self._store()
        rows = [{
            "sword_id": "album_039", "name_zh": "前田藤四郎",
            "stats": {}, "page_no": 1,
        }, {
            "sword_id": "album_040", "name_zh": "前田藤四郎",
            "stats": {"极化": True}, "page_no": 1,
        }]
        snapshot_id = store.save_sword_snapshot(
            rows, owned=204, capacity=208, missing=202,
            captured_at=time.time())
        detail = store.sword_snapshot_detail(snapshot_id)
        self.assertEqual(detail["owned"], 204)
        self.assertEqual(detail["sword_count"], 2)
        self.assertEqual(detail["missing"], 202)
        self.assertEqual(detail["swords"][1]["stats"], {"极化": True})
        recent = store.recent_sword_snapshots()
        self.assertEqual(recent[0]["id"], snapshot_id)

    def test_legacy_db_without_sword_tables_gets_them(self):
        # 老库（只有 v8 时代的表）被新代码打开后，快照表自动补建且老数据不丢
        import sqlite3
        db = Path(tempfile.mkdtemp()) / "telemetry.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE runs (run_id TEXT PRIMARY KEY, script TEXT NOT NULL,
                started_at REAL NOT NULL, ended_at REAL,
                status TEXT NOT NULL DEFAULT 'running', label TEXT);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, run_id TEXT, script TEXT, event_type TEXT, payload TEXT);
            INSERT INTO runs(run_id, script, started_at, status)
                VALUES ('legacy_run', 'workflow', 1765500000, 'completed');
        """)
        conn.commit()
        conn.close()

        store = TelemetryStore(db)  # 打开即迁移
        snapshot_id = store.save_sword_snapshot(
            [{"sword_id": "album_003", "name_zh": "三日月宗近"}])
        self.assertEqual(store.sword_snapshot_detail(snapshot_id)["sword_count"], 1)
        # 老数据安然无恙
        check = sqlite3.connect(str(db))
        kept = check.execute(
            "SELECT script FROM runs WHERE run_id='legacy_run'").fetchone()
        check.close()
        self.assertEqual(kept[0], "workflow")

    def test_form_fact_roundtrip(self):
        # 形态事实随快照落盘：存进去什么读出来什么
        store = self._store()
        fact = {"status": "kiwame", "evidence": ["花数3/基线2"],
                "badge": {"flowers": 3, "score": 0.9,
                          "conclusion": "recognized"},
                "rarity_base": 2, "sword_type": "打刀"}
        snapshot_id = store.save_sword_snapshot(
            [{"sword_id": "touken_118_heshikiri_hasebe", "name_zh": "压切长谷部",
              "form_fact": fact}], owned=1, capacity=300, missing=0)
        row = store.sword_snapshot_detail(snapshot_id)["swords"][0]
        self.assertEqual(row["form_fact"], fact)
        # 没带形态事实的行落 NULL，读回 None
        snapshot_id = store.save_sword_snapshot(
            [{"sword_id": "album_003", "name_zh": "三日月宗近"}])
        row = store.sword_snapshot_detail(snapshot_id)["swords"][0]
        self.assertIsNone(row["form_fact"])

    def test_legacy_rows_table_gets_form_fact_column(self):
        # v10 时代的老库（行表无 form_fact）打开即补列；老行不丢、
        # 形态事实为 None（不回填不猜测：老快照没看过徽章就是没有）
        import sqlite3
        db = Path(tempfile.mkdtemp()) / "telemetry.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE sword_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at REAL NOT NULL, owned INTEGER, capacity INTEGER,
                sword_count INTEGER NOT NULL DEFAULT 0, missing INTEGER,
                source TEXT, completeness TEXT);
            CREATE TABLE sword_snapshot_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL, sword_id TEXT NOT NULL,
                name_zh TEXT NOT NULL, level INTEGER, tou_level INTEGER,
                survival INTEGER, survival_max INTEGER,
                fatigue INTEGER, fatigue_max INTEGER,
                stats TEXT NOT NULL DEFAULT '{}', kiwame_date TEXT,
                locked INTEGER, page_no INTEGER);
            INSERT INTO sword_snapshots(captured_at, owned, capacity,
                sword_count, missing, source, completeness)
                VALUES (100, 1, 300, 1, 0, 'owned_inventory', 'complete');
            INSERT INTO sword_snapshot_rows(snapshot_id, sword_id, name_zh,
                level, tou_level, survival, survival_max, fatigue, fatigue_max,
                stats, kiwame_date, locked, page_no)
                VALUES (1, 'touken_118_heshikiri_hasebe', '压切长谷部', 99, 1,
                        50, 50, 100, 100, '{}', '2024-01-01', 1, 1);
        """)
        conn.commit()
        conn.close()

        store = TelemetryStore(db)  # 打开即迁移
        detail = store.sword_snapshot_detail(1)
        self.assertEqual(detail["swords"][0]["name_zh"], "压切长谷部")
        self.assertIsNone(detail["swords"][0]["form_fact"])
        cols = {r["name"] for r in store._conn().execute(
            "PRAGMA table_info(sword_snapshot_rows)")}
        self.assertIn("form_fact", cols)


# 2026-09-18 真机帧（快照 #20 第 40 页）全量 OCR token：三日月 95 级曾被
# 旧 ±50 窗口分桶错记成下一行的 1 级——动态行锚的回归测试。
PAGE40_TOKENS = [
        _tok('天', 248, 175), _tok('刀剑', 423, 155), _tok('1级', 497, 156), _tok('显现', 1064, 163),
        _tok('生存', 553, 165), _tok('冲力', 770, 166), _tok('打击', 604, 166), _tok('防御', 661, 166),
        _tok('机动', 716, 166), _tok('侦察', 828, 166), _tok('必杀', 936, 166), _tok('范围', 990, 166),
        _tok('隐蔽', 884, 166), _tok('5 级', 495, 174), _tok('乱舞', 423, 176), _tok('2026', 1062, 193),
        _tok('生存', 424, 198), _tok('60/60', 494, 198), _tok('广', 990, 205), _tok('60', 551, 206),
        _tok('32', 936, 205), _tok('20', 881, 206), _tok('60', 605, 206), _tok('52', 661, 206),
        _tok('10', 716, 206), _tok('48', 770, 206), _tok('10', 829, 206), _tok('石切丸', 206, 216),
        _tok('8/29', 1062, 216), _tok('疲劳', 425, 220), _tok('49/100', 487, 220), _tok('恭', 282, 272),
        _tok('35 级', 493, 253), _tok('刀剑', 424, 253), _tok('显现', 1064, 264), _tok('生存', 553, 265),
        _tok('打击', 603, 266), _tok('防御', 661, 266), _tok('机动', 716, 266), _tok('冲力', 770, 266),
        _tok('侦察', 828, 266), _tok('必杀', 936, 266), _tok('范围', 990, 266), _tok('隐蔽', 883, 266),
        _tok('乱舞', 423, 275), _tok('6级', 495, 275), _tok('2018', 1062, 292), _tok('生存', 424, 298),
        _tok('86/86', 493, 299), _tok('72', 935, 306), _tok('狭', 990, 307), _tok('86', 551, 306),
        _tok('126', 606, 306), _tok('87', 768, 306), _tok('44', 828, 306), _tok('70', 714, 306),
        _tok('56', 881, 306), _tok('121', 659, 307), _tok('小狐丸', 206, 316), _tok('1/6', 1062, 317),
        _tok('疲劳', 425, 320), _tok('81/100', 486, 320), _tok('中伤', 368, 368), _tok('刀剑', 424, 353),
        _tok('95 级', 493, 354), _tok('天', 229, 371), _tok('四之四', 149, 385), _tok('显现', 1063, 364),
        _tok('生存', 553, 366), _tok('打击', 603, 366), _tok('防御', 661, 367), _tok('机动', 716, 367),
        _tok('冲力', 770, 367), _tok('侦察', 827, 367), _tok('必杀', 936, 367), _tok('范围', 990, 367),
        _tok('隐蔽', 883, 368), _tok('5 级', 495, 377), _tok('乱舞', 424, 376), _tok('2017', 1062, 394),
        _tok('30/79', 492, 399), _tok('生存', 424, 400), _tok('狭', 991, 407), _tok('79', 550, 406),
        _tok('160', 661, 407), _tok('89', 769, 407), _tok('36', 828, 407), _tok('69', 936, 407),
        _tok('129', 605, 407), _tok('72', 716, 407), _tok('69', 880, 407), _tok('7/14', 1063, 417),
        _tok('三日月宗近', 229, 417), _tok('66/100', 488, 421), _tok('疲劳', 426, 421), _tok('因', 242, 469),
        _tok('刀剑', 424, 454), _tok('1级', 497, 454), _tok('显现', 1063, 464), _tok('生存', 554, 466),
        _tok('打击', 602, 467), _tok('防御', 661, 467), _tok('机动', 716, 467), _tok('冲力', 770, 467),
        _tok('侦察', 827, 467), _tok('必杀', 936, 467), _tok('隐蔽', 883, 468), _tok('范围', 990, 467),
        _tok('1级', 496, 476), _tok('乱舞', 424, 478), _tok('2023', 1062, 495), _tok('生存', 424, 500),
        _tok('50/50', 493, 500), _tok('50', 551, 507), _tok('50', 605, 508), _tok('40', 771, 508),
        _tok('29', 827, 508), _tok('29', 881, 508), _tok('30', 936, 508), _tok('狭', 992, 508),
        _tok('52', 661, 508), _tok('31', 718, 508), _tok('三日月宗近', 230, 517), _tok('1/27', 1063, 518),
        _tok('疲劳', 432, 522), _tok('49/100', 486, 522),
]


class DynamicRowAnchorTests(unittest.TestCase):
    def test_page40_levels_belong_to_their_own_rows(self):
        result = parse_list_tokens(PAGE40_TOKENS)
        rows = result["rows"]
        self.assertEqual(result["fail_rows"], 0)
        self.assertEqual([r["name_zh"] for r in rows],
                         ["石切丸", "小狐丸", "三日月宗近", "三日月宗近"])
        got = [(r["level"], r["tou_level"], r["survival"], r["survival_max"],
                r["kiwame_date"]) for r in rows]
        self.assertEqual(got, [
            (1, 5, 60, 60, "2026-8-29"),     # 石切丸：上一版的 35 是下一行的
            (35, 6, 86, 86, "2018-1-6"),     # 小狐丸：上一版的 95 是三日月的
            (95, 5, 30, 79, "2017-7-14"),    # 三日月（中伤那振）
            (1, 1, 50, 50, "2023-1-27"),     # 三日月（新入手那振）
        ])

    def test_row_baselines_fall_back_to_hardcoded_without_anchors(self):
        from touken.flows.sword_inventory import _ROW_NAME_YS, _row_baselines
        self.assertEqual(_row_baselines([_tok("你好", 100, 100)]),
                         list(_ROW_NAME_YS))

    def test_missing_anchor_interpolated(self):
        from touken.flows.sword_inventory import _row_baselines
        # 中间一行「刀剑」标签 OCR 漏掉：按行距插值补回
        baselines = _row_baselines([
            _tok("刀剑", 424, 155), _tok("刀剑", 424, 256), _tok("刀剑", 424, 458),
        ])
        self.assertEqual(baselines, [155 + 62, 256 + 62, 357 + 62, 458 + 62])


class PageTurnConfirmTests(unittest.TestCase):
    """翻页确认：两页内容一模一样时（整页五振 Lv.1 狮子王连排，
    2026-09-21 第 15→16 页真机实锤）行指纹分不出翻没翻，靠页码条
    高亮块像素兜底；指纹用有序多重集，重复行数不同也算翻过。"""

    def _strip(self, fill):
        import numpy as np
        return np.full((45, 400, 3), fill, dtype=np.uint8)

    def test_identical_pages_but_strip_moved_is_turned(self):
        from touken.flows.sword_inventory import _page_turned
        row = {"sword_id": "touken_122_shishiou", "level": 1,
               "survival_max": 45, "survival": 45, "kiwame_date": None}
        fp = sorted([_row_key(row)] * 5)   # 两页各五振一模一样的狮子王
        old_strip = self._strip(0)
        new_strip = self._strip(0)
        new_strip[10:30, 100:160] = 255    # 高亮块挪了一格
        self.assertTrue(_page_turned(fp, fp, old_strip, new_strip, True))

    def test_identical_pages_and_strip_still_is_not_turned(self):
        from touken.flows.sword_inventory import _page_turned
        row = {"sword_id": "touken_122_shishiou", "level": 1,
               "survival_max": 45, "survival": 45, "kiwame_date": None}
        fp = sorted([_row_key(row)] * 5)
        strip = self._strip(0)
        self.assertFalse(_page_turned(fp, fp, strip, strip.copy(), True))

    def test_duplicate_row_count_differs_is_turned(self):
        from touken.flows.sword_inventory import _page_turned
        row = {"sword_id": "touken_122_shishiou", "level": 1,
               "survival_max": 45, "survival": 45, "kiwame_date": None}
        old_fp = sorted([_row_key(row)] * 5)
        new_fp = sorted([_row_key(row)] * 4)   # 末页少一行
        strip = self._strip(0)
        self.assertTrue(_page_turned(old_fp, new_fp,
                                     strip, strip.copy(), True))

    def test_strip_noise_below_threshold_is_not_turned(self):
        from touken.flows.sword_inventory import _page_turned
        old_strip = self._strip(0)
        new_strip = self._strip(0)
        new_strip[0, :49] = 60               # 零星抖动 < 50 像素阈值
        self.assertFalse(_page_turned([], [], old_strip, new_strip, False))


class ScanListPageGateTests(unittest.TestCase):
    """逐格全落空时的整列兜底必须经过标题门禁：部队选择页（2026-09-20
    离线验收 221316 实锤）也读得出刀名 token，无标题的整列结果是假行。

    注意常量取值要走 _inv_mod 运行时属性：全量跑时模块可能被前面的
    漂移测试 reload（覆盖层清空），模块级 import 绑定的是旧对象。"""

    class _FakeMaa:
        """cell/整列 OCR 走查表（Region 反解 xyxy），标题 OCR 走开关。"""
        def __init__(self, list_roi, cell_map, legacy_tokens, title_hit):
            self._list_roi = tuple(list_roi)
            self._cell_map = {tuple(k): v for k, v in cell_map.items()}
            self._legacy = [(t, _Pt(x, y)) for t, (x, y) in legacy_tokens]
            self._title_hit = title_hit
            self.list_ocr_calls = 0

        def ocr_all(self, roi, img=None):
            xyxy = (roi.x, roi.y, roi.x + roi.w, roi.y + roi.h)
            if xyxy == self._list_roi:
                self.list_ocr_calls += 1
                return self._legacy
            return [(t, _Pt(x, y))
                    for t, (x, y) in self._cell_map.get(xyxy, [])]

        def ocr(self, expected, roi, match_mode="contains"):
            return object() if self._title_hit else None

    def _scan(self, cell_map, legacy_tokens, title_hit):
        maa = self._FakeMaa(_inv_mod._LIST_ROI, cell_map, legacy_tokens,
                            title_hit)
        inst = _inv_mod.SwordInventoryMixin.__new__(_inv_mod.SwordInventoryMixin)
        inst.maa = maa
        return inst._scan_list_page(img=None), maa

    def test_named_cells_skip_legacy_entirely(self):
        # 逐格有名字命中：不碰整列 OCR，不兜底
        (parsed, fell_back), maa = self._scan(
            {_inv_mod.ROW_CELL_ROIS[1]["name"]: [_tok("安宅切", 280, 217)]},
            ROW_ATAGI, title_hit=True)
        self.assertFalse(fell_back)
        self.assertEqual(maa.list_ocr_calls, 0)
        self.assertEqual([r["sword_id"] for r in parsed["rows"]],
                         ["touken_250_atagi_kiri"])

    def test_blank_cells_with_title_fall_back_to_legacy(self):
        # 逐格全落空 + fail 行 + 标题在 → 退回整列老路径
        (parsed, fell_back), maa = self._scan(
            {_inv_mod.ROW_CELL_ROIS[2]["name"]: [_tok("选择部队", 280, 420)]},
            ROW_ATAGI, title_hit=True)
        self.assertTrue(fell_back)
        self.assertEqual(maa.list_ocr_calls, 1)
        self.assertEqual([r["sword_id"] for r in parsed["rows"]],
                         ["touken_250_atagi_kiri"])

    def test_blank_cells_without_title_stay_blank(self):
        # 部队选择页情形：标题不在，整列读出的刀名是假行，不得采纳
        (parsed, fell_back), maa = self._scan(
            {_inv_mod.ROW_CELL_ROIS[2]["name"]: [_tok("选择部队", 280, 420)]},
            ROW_ATAGI, title_hit=False)
        self.assertFalse(fell_back)
        self.assertEqual(maa.list_ocr_calls, 1)   # 整列读了但结果被门禁挡下
        self.assertEqual([r["sword_id"] for r in parsed["rows"]], [None])

    def test_double_blank_is_quiet_empty(self):
        # 逐格、整列都空（真空页/转场帧）：不算兜底，安静按空页返回
        (parsed, fell_back), _maa = self._scan({}, [], title_hit=True)
        self.assertFalse(fell_back)
        self.assertEqual(parsed["rows"], [])
        self.assertEqual(parsed["fail_rows"], 0)


class RetryMissingDatesTests(unittest.TestCase):
    """缺日期重读：强制刷帧重读该格，第二次四边放宽 6px，最多两次；
    只处理逐格路径的行（整列兜底行没有 _date_roi）。"""

    DATE_ROI = (1023, 141, 1103, 232)

    class _FakeMaa:
        def __init__(self, batches):
            self._batches = list(batches)
            self.rois = []
            self.force_shots = 0

        def screenshot(self, force=False):
            if force:
                self.force_shots += 1
            return None

        def ocr_all(self, roi, img=None):
            self.rois.append((roi.x, roi.y, roi.x + roi.w, roi.y + roi.h))
            batch = self._batches.pop(0) if self._batches else []
            return [(t, _Pt(x, y)) for t, (x, y) in batch]

    def _row(self, **kw):
        row = {"sword_id": "touken_250_atagi_kiri", "name_zh": "安宅切",
               "kiwame_date": None, "_date_roi": self.DATE_ROI}
        row.update(kw)
        return row

    def _run(self, rows, batches):
        maa = self._FakeMaa(batches)
        inst = _inv_mod.SwordInventoryMixin.__new__(_inv_mod.SwordInventoryMixin)
        inst.maa = maa
        with patch("touken.flows.sword_inventory.time.sleep"):
            inst._retry_missing_dates(rows)
        return maa

    def test_first_attempt_success_no_retry(self):
        row = self._row()
        maa = self._run([row], [[_tok("20262/12", 1062, 200)]])
        self.assertEqual(row["kiwame_date"], "2026-2-12")
        self.assertEqual(len(maa.rois), 1)
        self.assertEqual(maa.rois[0], self.DATE_ROI)

    def test_second_attempt_widens_roi(self):
        row = self._row()
        maa = self._run([row], [[],
                                [_tok("2026", 1062, 193),
                                 _tok("2/12", 1061, 216)]])
        self.assertEqual(row["kiwame_date"], "2026-2-12")
        self.assertEqual(maa.force_shots, 2)   # 每次都强制刷帧
        self.assertEqual(maa.rois[0], self.DATE_ROI)
        self.assertEqual(maa.rois[1], (1017, 135, 1109, 238))  # 四边放宽 6px

    def test_gives_up_after_two_attempts(self):
        row = self._row()
        maa = self._run([row], [[], []])
        self.assertIsNone(row["kiwame_date"])
        self.assertEqual(len(maa.rois), 2)

    def test_rows_without_date_roi_are_skipped(self):
        # 整列兜底行没有 _date_roi、已有日期的行、fail 行：都不重读
        legacy_row = {"sword_id": "x", "kiwame_date": None}
        dated_row = self._row(kiwame_date="2026-2-12")
        fail_row = {"sword_id": None, "kiwame_date": None,
                    "_date_roi": self.DATE_ROI}
        maa = self._run([legacy_row, dated_row, fail_row], [])
        self.assertEqual(maa.rois, [])


class AvatarVerifyTests(unittest.TestCase):
    """逐行头像核验三分支（互证/对不上/捞回）+ 形态证据保守合并。
    假图假模板：拼贴脸=高分命中，无关脸=低分。"""

    @staticmethod
    def _img(h, w, seed):
        import numpy as np
        rng = np.random.default_rng(seed)
        return rng.integers(0, 255, (h, w, 3), dtype=np.uint8)

    def _tpl(self, sword_id, name_zh, form, seed):
        return {"no": "0000", "form": form, "name": name_zh, "tag": None,
                "sword_id": sword_id, "name_zh": name_zh, "path": None,
                "img": self._img(56, 76, seed)}

    def _frame_with_face(self, row_no, face):
        img = self._img(720, 1280, seed=99)
        x0, y0, _x1, _y1 = _inv_mod._avatar_band(row_no)
        img[y0 + 10:y0 + 66, x0 + 20:x0 + 96] = face
        return img

    def _parsed(self, rows, fail_rows=0):
        return {"rows": rows, "fail_rows": fail_rows}

    def test_agree_records_score_without_threshold(self):
        face_tpl = self._tpl("sid_a", "甲", "普", seed=7)
        img = self._frame_with_face(1, face_tpl["img"])
        row = {"sword_id": "sid_a", "name_zh": "甲", "_row_no": 1}
        parsed = self._parsed([row])
        _inv_mod.verify_rows_by_avatar(img, parsed, [face_tpl])
        check = row["avatar_check"]
        self.assertEqual(check["result"], "agree")
        self.assertEqual(check["hit"]["form"], "普")
        self.assertGreaterEqual(check["hit"]["score"], 0.99)
        self.assertEqual(parsed.get("avatar_notes"), [])   # 互证不嚷嚷

    def test_disagree_keeps_ocr_and_notes(self):
        face_tpl = self._tpl("sid_a", "甲", "普", seed=7)
        img = self._frame_with_face(1, face_tpl["img"])
        row = {"sword_id": "sid_b", "name_zh": "乙", "_row_no": 1}
        parsed = self._parsed([row])
        _inv_mod.verify_rows_by_avatar(img, parsed, [face_tpl])
        self.assertEqual(row["sword_id"], "sid_b")   # OCR 为主，不二选一
        check = row["avatar_check"]
        self.assertEqual(check["result"], "disagree")
        self.assertEqual(check["hit"]["name_zh"], "甲")
        note = parsed["avatar_notes"][0]
        self.assertEqual(note["kind"], "disagree")
        self.assertEqual((note["ocr"], note["avatar"]), ("乙", "甲"))

    def test_rescued_fail_row_gets_identity_and_cells_backfilled(self):
        face_tpl = self._tpl("sid_a", "甲", "普", seed=7)
        img = self._frame_with_face(2, face_tpl["img"])
        row = {"sword_id": None, "name_zh": None, "_row_no": 2}
        parsed = self._parsed([row], fail_rows=1)
        rois = _inv_mod.ROW_CELL_ROIS[2]
        cell_map = {
            tuple(rois["levels"]): [_tok("刀剑99级乱舞1级生存45/45疲劳100/100",
                                         460, 280)],
            tuple(rois["date"]): [_tok("2026", 1062, 394),
                                  _tok("7/29", 1061, 417)],
        }
        for cell, value in zip(split_stats_roi(rois["stats"]),
                               ["45", "46", "55", "52", "34", "42", "40", "29",
                                "狭"]):
            cell_map[tuple(cell)] = [_tok(value, cell[0] + 5, cell[1] + 5)]
        cell_ocr = lambda roi: cell_map.get(tuple(roi), [])  # noqa: E731
        _inv_mod.verify_rows_by_avatar(img, parsed, [face_tpl], cell_ocr)
        self.assertEqual(row["sword_id"], "sid_a")
        self.assertEqual(row["name_zh"], "甲")
        self.assertEqual(row["avatar_check"]["result"], "rescued")
        self.assertEqual(parsed["fail_rows"], 0)     # 捞回不算 fail
        self.assertEqual((row["level"], row["tou_level"]), (99, 1))
        self.assertEqual(row["stats"]["生存"], 45)
        self.assertEqual(row["kiwame_date"], "2026-7-29")
        self.assertEqual(row["_badge_rect"], rois["badge"])
        note = parsed["avatar_notes"][0]
        self.assertEqual(note["kind"], "rescued")

    def test_low_score_mismatch_stays_observation_only(self):
        # 带里没有这张脸：分数够不上采纳线，不报警不捞回，只记观测
        face_tpl = self._tpl("sid_a", "甲", "普", seed=7)
        img = self._img(720, 1280, seed=99)   # 纯噪声，没有脸
        named = {"sword_id": "sid_b", "name_zh": "乙", "_row_no": 1}
        fail = {"sword_id": None, "name_zh": None, "_row_no": 2}
        parsed = self._parsed([named, fail], fail_rows=1)
        _inv_mod.verify_rows_by_avatar(img, parsed, [face_tpl])
        self.assertEqual(named["avatar_check"]["result"], "low_score")
        self.assertEqual(fail["avatar_check"]["result"], "low_score")
        self.assertIsNone(fail["sword_id"])          # 低分不许捞
        self.assertEqual(parsed["fail_rows"], 1)
        self.assertEqual(parsed["avatar_notes"], [])

    def test_merge_avatar_form_fills_only_badge_unknown(self):
        hit = {"sword_id": "sid", "name_zh": "甲", "form": "普", "tag": None,
               "score": 0.95, "margin": 0.3, "forms_in_library": ["普", "极"],
               "form_rival_score": 0.80}
        agree = {"result": "agree", "hit": hit}
        # badge unknown + 头像达标（总开关默认开）→ 头像补结论
        from touken import avatar_db
        fact = {"status": "unknown", "evidence": []}
        _inv_mod.merge_avatar_form(fact, agree)
        self.assertEqual(fact["status"], "normal")
        self.assertEqual(fact["avatar"]["form"], "普")
        self.assertTrue(any("头像通道" in e for e in fact["evidence"]))
        # badge 已确认 → 头像不许推翻（观测照记）
        fact = {"status": "kiwame", "evidence": ["花数3/基线2"]}
        _inv_mod.merge_avatar_form(fact, agree)
        self.assertEqual(fact["status"], "kiwame")
        self.assertEqual(fact["avatar"]["form"], "普")
        # 总开关拨回 False：观测照记，结论不下
        with patch.object(avatar_db, "AVATAR_FORM_ENABLED", False):
            fact = {"status": "unknown", "evidence": []}
            _inv_mod.merge_avatar_form(fact, agree)
            self.assertEqual(fact["status"], "unknown")
            self.assertEqual(fact["avatar"]["form"], "普")
        # disagree 的头像是另一把刀：形态观测不进本行
        fact = {"status": "unknown", "evidence": []}
        _inv_mod.merge_avatar_form(fact, {"result": "disagree", "hit": hit})
        self.assertEqual(fact["status"], "unknown")
        self.assertNotIn("avatar", fact)
        # 弱分 → 观测照记，结论不下（2026-09-21 放宽后仍保的底线）
        weak = dict(hit, forms_in_library=["普"], form_rival_score=None, score=0.80)
        fact = {"status": "unknown", "evidence": []}
        _inv_mod.merge_avatar_form(fact, {"result": "agree", "hit": weak})
        self.assertEqual(fact["status"], "unknown")
        self.assertEqual(fact["avatar"]["form"], "普")
        # 单形态库高分 → 放宽后直接下结论（跨形态分数离 0.88 线物理级隔离）
        single = dict(hit, forms_in_library=["普"], form_rival_score=None, score=0.95)
        fact = {"status": "unknown", "evidence": []}
        _inv_mod.merge_avatar_form(fact, {"result": "agree", "hit": single})
        self.assertEqual(fact["status"], "normal")
        self.assertEqual(fact["avatar"]["form"], "普")

    def test_avatar_messages_not_fail_worded(self):
        """对不上/捞回的话术是 ⚠️ 级播报，不许撞翻车词表"""
        self.assertFalse(_is_fail(
            "⚠️ 第 3 页第 2 行 OCR 和头像对不上：OCR=安宅切，头像=小狐丸，"
            "先按 OCR 记，已标注待核对"))
        self.assertFalse(_is_fail(
            "第 3 页第 2 行 OCR 没认出的行用头像捞回：面影"))


if __name__ == "__main__":
    unittest.main()
