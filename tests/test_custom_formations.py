# -*- coding: utf-8 -*-
"""预设编队档案（custom_formations）与批量应用（apply_preset_formation_stream）。

假 MAA / 假 ensure，不碰真机；STATE_DIR 一律指到临时目录，不碰真实用户数据。
话术纪律钉死：成功的消息不许命中翻车词表，停下的话术必须命中
（touken/flows/report_judge.py 的 _FAIL_RE）。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from touken import custom_formations as cf
from touken.flows import formation_editor as fe
from touken.flows.formation_editor import (
    ALREADY_CORRECT, AMBIGUOUS, CHANGED, FormationEditorMixin, _TEAM_TAB)
from touken.flows.report_judge import _is_fail


def _record(**kw):
    base = {"id": "pf1", "name": "预设编队一", "target_team": 3,
            "slots": {"1": {"sword_catalog_id": "touken_003_mikazuki_munechika",
                            "name_zh": "三日月宗近", "level": 99}}}
    base.update(kw)
    return base


# ==================== 存取 ====================

class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(cf, "STATE_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip(self):
        records = [_record(), _record(id="pf2", name="二队", target_team=5)]
        cf.save_formations(records)
        self.assertEqual(cf.load_formations(), records)
        raw = json.loads((Path(self._tmp.name) / "custom_formations.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], cf.SCHEMA_VERSION)

    def test_legacy_file_without_version_loads_losslessly(self):
        path = Path(self._tmp.name) / "custom_formations.json"
        records = [_record()]
        path.write_text(json.dumps({"formations": records}, ensure_ascii=False),
                        encoding="utf-8")
        self.assertEqual(cf.load_formations(), records)
        cf.save_formations(cf.load_formations())
        upgraded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["schema_version"], cf.SCHEMA_VERSION)
        self.assertEqual(upgraded["formations"], records)

    def test_ranked_policy_roundtrip_keeps_old_records(self):
        ranked = _record(id="pf2", slots={"2": {
            "selection_policy": "locked_highest_level",
            "sword_catalog_id": "touken_003_mikazuki_munechika", "name_zh": "三日月宗近",
            "form_status": "normal"}})
        records = [_record(), ranked]
        cf.save_formations(records)
        self.assertEqual(cf.load_formations(), records)

    def test_missing_file_is_empty(self):
        self.assertEqual(cf.load_formations(), [])

    def test_corrupt_json_is_backed_up_and_empty(self):
        bad = Path(self._tmp.name) / "custom_formations.json"
        bad.write_text("{不是 json", encoding="utf-8")
        self.assertEqual(cf.load_formations(), [])
        self.assertFalse(bad.exists())
        backups = list(Path(self._tmp.name).glob("custom_formations.json.bad-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("{不是 json", backups[0].read_text(encoding="utf-8"))
        # 备份之后还能正常当空库用（写入/重读）
        cf.save_formations([_record()])
        self.assertEqual(len(cf.load_formations()), 1)

    def test_wrong_shape_is_empty(self):
        bad = Path(self._tmp.name) / "custom_formations.json"
        bad.write_text(json.dumps(["不是 dict"], ensure_ascii=False),
                       encoding="utf-8")
        self.assertEqual(cf.load_formations(), [])


# ==================== id 分配 / 查找 ====================

class IdTests(unittest.TestCase):
    def test_first_gap_is_picked(self):
        self.assertEqual(cf.new_formation_id([]), "pf1")
        existing = [_record(id="pf1"), _record(id="pf2"), _record(id="pf4")]
        self.assertEqual(cf.new_formation_id(existing), "pf3")

    def test_full_raises(self):
        existing = [_record(id=f"pf{i}") for i in range(1, 6)]
        with self.assertRaises(ValueError) as ctx:
            cf.new_formation_id(existing)
        self.assertIn("预设编队最多 5 套", str(ctx.exception))


class FindTests(unittest.TestCase):
    def test_find(self):
        a, b = _record(id="pf1"), _record(id="pf2")
        self.assertIs(cf.find_formation([a, b], "pf2"), b)
        self.assertIsNone(cf.find_formation([a, b], "pf9"))
        self.assertIsNone(cf.find_formation([], "pf1"))
        self.assertIsNone(cf.find_formation(None, "pf1"))


# ==================== 校验 ====================

class ValidateTests(unittest.TestCase):
    def test_valid_record(self):
        self.assertIsNone(cf.validate_formation(
            _record(), existing=[_record(id="pf2")]))

    def test_valid_empty_slots(self):
        # 还没指定任何槽位也合法（应用时会拦）
        self.assertIsNone(cf.validate_formation(_record(slots={})))

    def test_valid_without_id(self):
        rec = _record()
        del rec["id"]
        self.assertIsNone(cf.validate_formation(rec))

    def test_name_required(self):
        for bad in (None, "", "   ", 123):
            self.assertIsNotNone(cf.validate_formation(_record(name=bad)),
                                 f"name={bad!r} 应被拒")

    def test_name_max_20_chars(self):
        self.assertIsNone(cf.validate_formation(_record(name="刀" * 20)))
        self.assertIsNotNone(cf.validate_formation(_record(name="刀" * 21)))

    def test_target_team_range(self):
        for bad in (0, 6, "3", 3.0, None, True):
            self.assertIsNotNone(
                cf.validate_formation(_record(target_team=bad)),
                f"target_team={bad!r} 应被拒")
        for good in (1, 5):
            self.assertIsNone(cf.validate_formation(_record(target_team=good)))

    def test_slots_must_be_dict(self):
        for bad in (None, [], "1"):
            self.assertIsNotNone(cf.validate_formation(_record(slots=bad)),
                                 f"slots={bad!r} 应被拒")

    def test_slots_max_six_keys(self):
        slots = {str(i): {"name_zh": "三日月宗近"} for i in range(1, 7)}
        self.assertIsNone(cf.validate_formation(_record(slots=slots)))
        slots["7"] = {"name_zh": "小狐丸"}
        self.assertIsNotNone(cf.validate_formation(_record(slots=slots)))

    def test_slot_key_must_be_1_to_6(self):
        for key in ("0", "7", "七", 1, ""):
            self.assertIsNotNone(
                cf.validate_formation(_record(slots={key: {"name_zh": "x"}})),
                f"槽位键 {key!r} 应被拒")

    def test_slot_entry_needs_identity(self):
        for entry in ({}, {"sword_catalog_id": ""}, {"name_zh": "  "},
                      {"sword_catalog_id": None, "name_zh": None},
                      "三日月宗近", None):
            self.assertIsNotNone(
                cf.validate_formation(_record(slots={"1": entry})),
                f"entry={entry!r} 应被拒")
        self.assertIsNone(cf.validate_formation(
            _record(slots={"1": {"sword_catalog_id": "touken_003"}})))
        self.assertIsNone(cf.validate_formation(
            _record(slots={"1": {"name_zh": "三日月宗近"}})))

    def test_ranked_policy_requires_catalog_and_form(self):
        base = {"selection_policy": "locked_highest_level",
                "sword_catalog_id": "touken_003_mikazuki_munechika", "form_status": "normal"}
        self.assertIsNone(cf.validate_formation(_record(slots={"1": base})))
        for bad in ({**base, "form_status": "unknown"},
                    {**base, "sword_catalog_id": ""},
                    {**base, "observation_id": "9:1"},
                    {**base, "selection_policy": "anything"}):
            self.assertIsNotNone(cf.validate_formation(_record(slots={"1": bad})))

    def test_treasure_requires_visible_fingerprint_and_single_assignment(self):
        sword = {"selection_policy": "locked_highest_level",
                 "sword_catalog_id": "touken_003_mikazuki_munechika",
                 "form_status": "normal"}
        treasure = {"name": "锷·月下梅树透图", "level": 1, "affection": 0}
        self.assertIsNone(cf.validate_formation(
            _record(slots={"1": {**sword, "treasure": treasure}})))
        self.assertIsNotNone(cf.validate_formation(
            _record(slots={"1": {**sword, "treasure": {**treasure, "name": ""}}})))
        self.assertIsNotNone(cf.validate_formation(
            _record(slots={"1": {**sword, "treasure": treasure},
                           "2": {**sword, "treasure": treasure}})))
        self.assertIsNone(cf.validate_formation(
            _record(slots={"1": {**sword, "treasure": treasure},
                           "2": {"name_zh": "今剑", "treasure":
                                 {**treasure, "name": "三所物·菊"}}})))

    def test_troop_positions_require_full_names(self):
        sword = {"selection_policy": "locked_highest_level",
                 "sword_catalog_id": "touken_003_mikazuki_munechika",
                 "form_status": "normal"}
        self.assertIsNone(cf.validate_formation(_record(
            slots={"1": {**sword, "troops": {"1": "轻步兵·特上",
                                             "3": "盾兵·特上"}}})))
        for bad in ({"4": "盾兵·特上"}, {"1": ""}, {"2": 123}):
            self.assertIsNotNone(cf.validate_formation(_record(
                slots={"1": {**sword, "troops": bad}})))

    def test_horse_and_charm_names_are_optional_but_nonempty(self):
        sword = {"sword_catalog_id": "touken_003_mikazuki_munechika",
                 "name_zh": "三日月宗近"}
        self.assertIsNone(cf.validate_formation(_record(
            slots={"1": {**sword, "horse": "08望月", "charm": "御守·极"}})))
        for key in ("horse", "charm"):
            for bad in ("", "  ", 1):
                self.assertIsNotNone(cf.validate_formation(_record(
                    slots={"1": {**sword, key: bad}})))
        self.assertIn("同名马", cf.validate_formation(_record(slots={
            "1": {**sword, "horse": "08望月"},
            "2": {**sword, "horse": "08望月"}})))
        self.assertIsNone(cf.validate_formation(_record(slots={
            "1": {**sword, "horse": "白毛"},
            "2": {**sword, "horse": "白毛"}})))

    def test_id_format(self):
        for bad in ("PF1", "pf-1", "pf 1", "pf_1", "p.f1", "", 123):
            self.assertIsNotNone(cf.validate_formation(_record(id=bad)),
                                 f"id={bad!r} 应被拒")

    def test_id_unique_against_existing(self):
        other = _record(id="pf2", name="别的")
        self.assertIsNotNone(
            cf.validate_formation(_record(id="pf2"), existing=[other]))
        self.assertIsNone(
            cf.validate_formation(_record(id="pf1"), existing=[other]))


def _pool_entry(oid, catalog, name, level=99, **extra):
    entry = {"observation_id": oid, "sword_catalog_id": catalog,
             "same_team_exclusion_key": catalog, "name_zh": name,
             "level": level, "form_status": "normal", "stats": {}}
    entry.update(extra)
    return entry


class ResolvePresetTests(unittest.TestCase):
    def test_ranked_resolves_without_archive(self):
        ranked = {"selection_policy": "locked_highest_level",
                  "sword_catalog_id": "touken_003_mikazuki_munechika", "name_zh": "三日月宗近",
                  "form_status": "normal"}
        result = cf.resolve_formation_slots(_record(slots={"1": ranked}),
                                            {"done": False, "reason": "没有刀账"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["slots"]["1"], ranked)

    def test_ranked_duplicate_catalog_stops_before_game(self):
        ranked = {"selection_policy": "locked_highest_level",
                  "sword_catalog_id": "touken_003_mikazuki_munechika", "form_status": "normal"}
        result = cf.resolve_formation_slots(_record(slots={"1": ranked,
                                                        "2": {**ranked, "form_status": "kiwame"}}),
                                            {"done": False})
        self.assertFalse(result["ok"])

    def test_ranked_and_old_name_only_slot_still_conflict(self):
        ranked = {"selection_policy": "locked_highest_level",
                  "sword_catalog_id": "touken_003_mikazuki_munechika",
                  "form_status": "normal"}
        old = {"name_zh": "三日月宗近", "level": 99}
        entry = _pool_entry("9:1", "touken_003_mikazuki_munechika",
                            "三日月宗近")
        result = cf.resolve_formation_slots(_record(slots={"1": ranked, "2": old}),
                                            {"done": True, "entries": [entry]})
        self.assertFalse(result["ok"])
        self.assertIn("不能重复", result["reason"])

    def test_client_instance_survives_growth_and_never_replaces_missing_sword(self):
        entry = _pool_entry("youzu:1732", "touken_003", "三日月宗近")
        entry["level"] = 100
        record = _record(slots={"1": {"observation_id": "youzu:1732",
                          "sword_catalog_id": "touken_003", "name_zh": "三日月宗近", "level": 50}})
        result = cf.resolve_formation_slots(record, {"done": True, "entries": [entry]})
        self.assertTrue(result["ok"])
        self.assertIs(result["slots"]["1"], entry)
        other = {**entry, "observation_id": "youzu:9999", "level": 50}
        result = cf.resolve_formation_slots(record, {"done": True, "entries": [other]})
        self.assertFalse(result["ok"])

    def test_observation_id_links_directly(self):
        entry = _pool_entry("9:1", "touken_003", "三日月宗近")
        record = _record(slots={"1": {"observation_id": "9:1",
                                            "sword_catalog_id": "touken_003",
                                            "name_zh": "三日月宗近", "level": 99}})
        result = cf.resolve_formation_slots(
            record, {"done": True, "entries": [entry]})
        self.assertTrue(result["ok"])
        self.assertIs(result["slots"]["1"], entry)

    def test_legacy_client_relink_ignores_equipment_but_requires_unique_date(self):
        saved = _pool_entry("24:4074", "touken_035", "后藤藤四郎", level=93,
                            tou_level=9, survival_max=43,
                            stats={"生存": 43, "侦察": 96, "打击": 94, "机动": 152})
        saved.update(form_status="kiwame", kiwame_date="2022-4-9",
                     troops={"1": "轻步兵·特上"})
        current = {**saved, "observation_id": "youzu:18156485", "level": 98,
                   "stats": {"打击": 88, "机动": 147, "侦察": 96}}
        current.pop("troops")
        record = _record(slots={"1": saved})
        result = cf.resolve_formation_slots(record, {"done": True, "entries": [current]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["slots"]["1"]["observation_id"], "youzu:18156485")
        self.assertEqual(result["slots"]["1"]["troops"], saved["troops"])
        duplicate = {**current, "observation_id": "youzu:2"}
        self.assertFalse(cf.resolve_formation_slots(record,
            {"done": True, "entries": [current, duplicate]})["ok"])
        for changes in ({"kiwame_date": "2022-4-10"}, {"level": 1}, {"form_status": "normal"}):
            self.assertFalse(cf.resolve_formation_slots(record,
                {"done": True, "entries": [{**current, **changes}]})["ok"])

    def test_client_upgrade_backed_up_and_does_not_overwrite_edits(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom_formations.json"
            with patch.object(cf, "_formations_path", return_value=path):
                record = _record(slots={"1": {"observation_id": "24:1", "name_zh": "后藤藤四郎"}})
                cf.save_formations([record])
                original = path.read_bytes()
                entry = {"observation_id": "youzu:123", "name_zh": "后藤藤四郎"}
                self.assertTrue(cf.persist_client_slots(record, {"1": entry}))
                self.assertEqual(path.with_name(path.name + ".before-client-ids.bak").read_bytes(), original)
                self.assertEqual(cf.load_formations()[0]["slots"]["1"], entry)
                self.assertFalse(cf.persist_client_slots(record, {"1": entry}))

    def test_exact_slot_keeps_equipment_after_archive_relink(self):
        entry = _pool_entry("9:1", "touken_003", "三日月宗近")
        treasure = {"name": "锷·月下梅树透图", "level": 1, "affection": 0}
        troops = {"1": "轻步兵·特上", "3": "盾兵·特上"}
        record = _record(slots={"1": {"observation_id": "9:1",
                                            "sword_catalog_id": "touken_003",
                                            "name_zh": "三日月宗近", "level": 99,
                                            "treasure": treasure,
                                            "troops": troops}})
        result = cf.resolve_formation_slots(record,
                                             {"done": True, "entries": [entry]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["slots"]["1"]["treasure"], treasure)
        self.assertEqual(result["slots"]["1"]["troops"], troops)
        self.assertNotIn("treasure", entry)
        self.assertNotIn("troops", entry)

    def test_new_snapshot_relinks_by_saved_fingerprint(self):
        entry = _pool_entry("10:7", "touken_003", "三日月宗近",
                            tou_level=4, survival_max=60,
                            stats={"recon": 42})
        saved = {**entry, "observation_id": "9:1"}
        result = cf.resolve_formation_slots(
            _record(slots={"1": saved}),
            {"done": True, "entries": [entry]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["slots"]["1"]["observation_id"], "10:7")

    def test_level_up_relinks_with_stable_unique_fingerprint(self):
        entry = _pool_entry("10:7", "touken_003", "三日月宗近", level=91,
                            tou_level=4, survival_max=60,
                            stats={"打击": 68})
        saved = {**entry, "observation_id": "9:1", "level": 90}
        result = cf.resolve_formation_slots(
            _record(slots={"1": saved}),
            {"done": True, "entries": [entry]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["slots"]["1"]["level"], 91)

    def test_level_up_without_stable_evidence_stays_unresolved(self):
        entry = _pool_entry("10:7", "touken_003", "三日月宗近", level=91)
        saved = {"observation_id": "9:1", "sword_catalog_id": "touken_003",
                 "name_zh": "三日月宗近", "level": 90}
        result = cf.resolve_formation_slots(
            _record(slots={"1": saved}),
            {"done": True, "entries": [entry]})
        self.assertFalse(result["ok"])

    def test_level_up_with_same_fingerprint_on_two_swords_is_ambiguous(self):
        entries = [_pool_entry(oid, "touken_118", "压切长谷部", level=level,
                               tou_level=4, survival_max=60,
                               stats={"打击": 68})
                   for oid, level in (("10:7", 91), ("10:8", 92))]
        saved = {**entries[0], "observation_id": "9:1", "level": 90}
        result = cf.resolve_formation_slots(
            _record(slots={"1": saved}),
            {"done": True, "entries": entries})
        self.assertFalse(result["ok"])
        self.assertIn("2 振分不清", result["reason"])

    def test_cultivation_pair_change_relinks_only_with_matching_table(self):
        stats = {"生存": 55, "侦察": 39, "打击": 80, "防御": 80,
                 "机动": 80, "冲力": 80, "隐蔽": 80, "必杀": 80}
        old = _pool_entry("9:1", "touken_003", "三日月宗近", level=90,
                          tou_level=4, survival_max=55, stats=stats)
        new = _pool_entry("10:7", "touken_003", "三日月宗近", level=91,
                          tou_level=4, survival_max=56,
                          stats={**stats, "生存": 56, "侦察": 40})
        pool = {"done": True, "observed_at": 1789992100, "entries": [new]}
        state = {"stats_at": "2026-09-22 21:33:18",
                 "stats": {"三日月宗近": {"生存": 56, "侦察": 40}}}
        with patch("touken.flows.naihanka._load_naihanka_state",
                   return_value=state):
            self.assertTrue(cf.resolve_formation_slots(
                _record(slots={"1": old}), pool)["ok"])
        with patch("touken.flows.naihanka._load_naihanka_state",
                   return_value={}):
            self.assertFalse(cf.resolve_formation_slots(
                _record(slots={"1": old}), pool)["ok"])

    def test_non_cultivation_pair_stays_distinct_even_if_other_stats_change(self):
        stats = {"生存": 55, "侦察": 39, "打击": 80, "防御": 80,
                 "机动": 80, "冲力": 80, "隐蔽": 80, "必杀": 80}
        old = _pool_entry("9:1", "touken_003", "三日月宗近", level=90,
                          tou_level=4, survival_max=55, stats=stats)
        same = _pool_entry("10:7", "touken_003", "三日月宗近", level=91,
                           tou_level=4, survival_max=55,
                           stats={**stats, "打击": 81})
        different = _pool_entry("10:8", "touken_003", "三日月宗近", level=91,
                                tou_level=4, survival_max=55,
                                stats={**stats, "侦察": 40})
        with patch("touken.flows.naihanka._load_naihanka_state",
                   return_value={}):
            result = cf.resolve_formation_slots(
                _record(slots={"1": old}),
                {"done": True, "entries": [same, different]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["slots"]["1"]["observation_id"], "10:7")

    def test_legacy_ambiguous_same_name_level_is_rejected_before_clicks(self):
        entries = [_pool_entry("9:1", "touken_118", "压切长谷部"),
                   _pool_entry("9:2", "touken_118", "压切长谷部")]
        result = cf.resolve_formation_slots(
            _record(slots={"1": {"sword_catalog_id": "touken_118",
                                        "name_zh": "压切长谷部", "level": 99}}),
            {"done": True, "entries": entries})
        self.assertFalse(result["ok"])
        self.assertIn("2 振分不清", result["reason"])

    def test_growth_stats_can_distinguish_same_name_level(self):
        entries = [_pool_entry("9:1", "touken_118", "压切长谷部",
                               survival_max=55, stats={"recon": 44}),
                   _pool_entry("9:2", "touken_118", "压切长谷部",
                               survival_max=57, stats={"recon": 46})]
        result = cf.resolve_formation_slots(
            _record(slots={"1": {"sword_catalog_id": "touken_118",
                                        "name_zh": "压切长谷部", "level": 99,
                                        "survival_max": 57,
                                        "stats": {"recon": 46}}}),
            {"done": True, "entries": entries})
        self.assertTrue(result["ok"])
        self.assertEqual(result["slots"]["1"]["observation_id"], "9:2")

    def test_same_instance_in_two_slots_is_rejected(self):
        entry = _pool_entry("9:1", "touken_003", "三日月宗近")
        saved = {"observation_id": "9:1", "sword_catalog_id": "touken_003",
                 "name_zh": "三日月宗近", "level": 99}
        result = cf.resolve_formation_slots(
            _record(slots={"1": saved, "2": dict(saved)}),
            {"done": True, "entries": [entry]})
        self.assertFalse(result["ok"])
        self.assertIn("不能重复", result["reason"])

    def test_incomplete_pool_is_rejected(self):
        result = cf.resolve_formation_slots(
            _record(), {"done": False, "reason": "只有残缺盘点"})
        self.assertFalse(result["ok"])
        self.assertIn("残缺", result["reason"])


# ==================== 批量应用流 ====================

class _ClickMaa:
    def __init__(self):
        self.clicks = []

    def click(self, point):
        self.clicks.append((point.x, point.y))


class _PresetHost(FormationEditorMixin):
    """假宿主：ensure 按槽位剧本返结果；导航指哪打哪（current_location
    即目的地）。host 上直接替换方法，与下游 panel 的用法一致。"""

    def __init__(self, ensure_results):
        self.maa = _ClickMaa()
        self.config = {}
        self.current_location = None
        self.nav_calls = []
        self.ensure_calls = []
        self._ensure_results = ensure_results

    def navigate_to_stream(self, dest):
        self.nav_calls.append(dest)
        self.current_location = dest
        yield f"nav→{dest}"

    def ensure_team_member_stream(self, team_no, slot_no, target,
                                  entry_context="auto", **kw):
        self.ensure_calls.append({"team_no": team_no, "slot_no": slot_no,
                                  "target": target,
                                  "entry_context": entry_context,
                                  "match_fields": kw.get("match_fields")})
        yield f"ensure@{slot_no}"
        return self._ensure_results.get(slot_no,
                                        {"result": CHANGED, "reason": ""})

    def _formation_read_team(self):
        return [{"slot_status": "occupied",
                 "sword_catalog_id": "touken_003_mikazuki_munechika"}]


SLOTS = {
    "1": {"sword_catalog_id": "touken_003_mikazuki_munechika",
          "name_zh": "三日月宗近"},
    "2": {"name_zh": "小狐丸"},
    "3": {"sword_catalog_id": "touken_118_heshikiri_hasebe"},
    "4": {"name_zh": "前田藤四郎"},
}


def _apply(host, team_no=3, slots=None, name="演练预设"):
    with patch("touken.flows.formation_editor.time.sleep", lambda *_: None):
        gen = host.apply_preset_formation_stream(
            team_no, SLOTS if slots is None else slots, name)
        msgs = []
        while True:
            try:
                msgs.append(next(gen))
            except StopIteration as stop:
                return stop.value, msgs


class ApplyStreamTests(unittest.TestCase):
    def test_accessory_failure_stops_preset_before_departure(self):
        for kind, name in (("horse", "08望月"), ("charm", "御守")):
            with self.subTest(kind=kind):
                host = _PresetHost({})
                slots = {"1": {**SLOTS["1"], kind: name}}
                with patch("touken.flows.formation_accessory.equip_preset_accessory_stream",
                           return_value=iter([f"[{kind}] 没找到，停"])) as equip:
                    ok, msgs = _apply(host, slots=slots)
                self.assertFalse(ok)
                equip.assert_called_once_with(host, 1, kind, name)
                self.assertIn("绝不继续出发", msgs[-1])

    def test_troop_failure_stops_preset_before_departure(self):
        host = _PresetHost({})
        slots = {"1": {**SLOTS["1"], "troops": {"1": "轻步兵·特上"}}}
        with patch("touken.flows.formation_troops.equip_preset_troop_stream",
                   return_value=iter(["[刀装] 名称没找到，停"])) as equip:
            ok, msgs = _apply(host, slots=slots)
        self.assertFalse(ok)
        self.assertTrue(equip.called)
        self.assertIn("绝不继续出发", msgs[-1])

    def test_treasure_failure_stops_preset_before_departure(self):
        host = _PresetHost({})
        treasure = {"name": "锷·月下梅树透图", "level": 1, "affection": 0}
        slots = {"1": {**SLOTS["1"], "treasure": treasure}}
        with patch("touken.flows.formation_treasure.equip_preset_treasure_stream",
                   return_value=iter(["[宝物] 仓库数量不明，停"])) as equip:
            ok, msgs = _apply(host, slots=slots)
        self.assertFalse(ok)
        self.assertTrue(equip.called)
        self.assertIn("绝不继续出发", msgs[-1])

    def test_all_ok_reports_summary_and_true(self):
        host = _PresetHost({2: {"result": ALREADY_CORRECT, "reason": ""}})
        ok, msgs = _apply(host)

        self.assertTrue(ok)
        summary = msgs[-1]
        self.assertIn("『演练预设』已覆盖部队3", summary)
        self.assertIn("换好 3 位", summary)
        self.assertIn("1 位本来就在", summary)
        self.assertFalse(_is_fail(summary))     # 成功话术不许命中翻车词
        # 逐槽升序、条目原样透传、formation 外壳
        self.assertEqual([c["slot_no"] for c in host.ensure_calls],
                         [1, 2, 3, 4])
        for c in host.ensure_calls:
            self.assertEqual(c["team_no"], 3)
            self.assertEqual(c["entry_context"], "formation")
            self.assertIs(c["target"], SLOTS[str(c["slot_no"])])
            # 选择列表无形态直读通道：预设应用必须收窄 match_fields，
            # 否则槽位里的 form_status 会让每行都背证据缺口而必 ambiguous
            self.assertEqual(c["match_fields"], ("name", "level"))
        # 切队标签点过
        self.assertIn(_TEAM_TAB[3], host.maa.clicks)
        self.assertEqual(host.nav_calls, ["编队"])

    def test_slots_applied_in_ascending_order_even_if_dict_unsorted(self):
        host = _PresetHost({})
        ok, _ = _apply(host, slots={"3": SLOTS["3"], "1": SLOTS["1"]})
        self.assertTrue(ok)
        self.assertEqual([c["slot_no"] for c in host.ensure_calls], [1, 3])

    def test_ambiguous_slot_stops_and_later_slots_never_run(self):
        host = _PresetHost({3: {"result": AMBIGUOUS,
                                "reason": "同名候选缺身份证据（form）"}})
        ok, msgs = _apply(host)

        self.assertFalse(ok)
        self.assertIn("卡在3号位", msgs[-1])
        self.assertIn("同名候选缺身份证据", msgs[-1])
        self.assertIn("队伍现在是半套", msgs[-1])
        self.assertTrue(_is_fail(msgs[-1]))     # 停下必须命中翻车词
        self.assertEqual([c["slot_no"] for c in host.ensure_calls],
                         [1, 2, 3])             # 4 号位没执行

    def test_empty_slots_refused_before_anything(self):
        host = _PresetHost({})
        ok, msgs = _apply(host, slots={})

        self.assertFalse(ok)
        self.assertIn("一个位置都没指定", msgs[-1])
        self.assertTrue(_is_fail(msgs[-1]))
        self.assertEqual(host.nav_calls, [])
        self.assertEqual(host.ensure_calls, [])
        self.assertEqual(host.maa.clicks, [])

    def test_bad_team_no_refused_before_anything(self):
        host = _PresetHost({})
        ok, msgs = _apply(host, team_no=9)

        self.assertFalse(ok)
        self.assertTrue(_is_fail(msgs[-1]))
        self.assertEqual(host.nav_calls, [])

    def test_nav_failure_stops_before_any_swap(self):
        class _LostHost(_PresetHost):
            def navigate_to_stream(self, dest):
                self.nav_calls.append(dest)
                yield "nav→迷路"          # current_location 不变

        host = _LostHost({})
        ok, msgs = _apply(host)

        self.assertFalse(ok)
        self.assertTrue(_is_fail(msgs[-1]))
        self.assertEqual(host.nav_calls, ["编队"])
        self.assertEqual(host.ensure_calls, [])
        self.assertEqual(host.maa.clicks, [])


# ==================== 远征占用预检 ====================

def _write_expeditions(tmp, payload):
    Path(tmp, "expeditions.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class ExpeditionGuardTests(unittest.TestCase):
    def test_dispatched_team_blocks_before_nav(self):
        dispatched = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with tempfile.TemporaryDirectory() as tmp:
            _write_expeditions(tmp, {
                "3": {"map_code": "E2", "duration_min": 99999,
                      "dispatched_at": dispatched}})
            with patch.object(fe, "STATE_DIR", Path(tmp)):
                host = _PresetHost({})
                ok, msgs = _apply(host)

        self.assertFalse(ok)
        self.assertIn("部队3还在远征", msgs[-1])
        self.assertTrue(_is_fail(msgs[-1]))
        self.assertEqual(host.nav_calls, [])        # 没进编队页
        self.assertEqual(host.ensure_calls, [])
        self.assertEqual(host.maa.clicks, [])

    def test_expired_record_does_not_block(self):
        dispatched = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - 7200))
        with tempfile.TemporaryDirectory() as tmp:
            _write_expeditions(tmp,
                               {"3": {"duration_min": 60,
                                      "dispatched_at": dispatched}})
            with patch.object(fe, "STATE_DIR", Path(tmp)):
                host = _PresetHost({})
                ok, _ = _apply(host)

        self.assertTrue(ok)
        self.assertEqual(len(host.ensure_calls), 4)

    def test_corrupt_expeditions_file_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "expeditions.json").write_text("{坏", encoding="utf-8")
            with patch.object(fe, "STATE_DIR", Path(tmp)):
                host = _PresetHost({})
                ok, _ = _apply(host)

        self.assertTrue(ok)

    def test_missing_expeditions_file_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(fe, "STATE_DIR", Path(tmp)):
                host = _PresetHost({})
                ok, _ = _apply(host)

        self.assertTrue(ok)

    def test_malformed_record_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_expeditions(tmp, {"3": {"dispatched_at": "不是时间"}})
            with patch.object(fe, "STATE_DIR", Path(tmp)):
                host = _PresetHost({})
                ok, _ = _apply(host)

        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
