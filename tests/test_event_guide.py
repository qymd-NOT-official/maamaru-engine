# -*- coding: utf-8 -*-
"""活动参谋攻略库（touken/data/event_guide.json）的 schema 测试。

攻略库是正式资源：条目 id 唯一、枚举合法、必填字段齐、计数对、
每条 sources 非空、confidence 合法——研究员草稿怎么约定的，
落库就怎么守住，不许悄悄变形。
"""

import unittest

from touken.event_briefing import load_guide

_DAMAGE = {"virtual", "real", None}
_BATTLE = {"day", "night", "indoor", "mixed", None}
_CONFIDENCE = {"high", "medium", "low"}
_KIND = {"event", "map"}

_REQUIRED = [
    "id", "kind", "name", "aliases", "damage", "battle", "map_points",
    "recommended_level", "bonus_swords", "bonus_rule", "enemy_fast_spear",
    "tips", "confidence", "sources",
]


def _entries():
    return load_guide()["entries"]


class EventGuideSchemaTests(unittest.TestCase):
    def test_loads_as_dict_with_entries(self):
        guide = load_guide()
        self.assertIsInstance(guide, dict)
        self.assertIsInstance(guide["entries"], list)

    def test_counts_8_events_32_maps(self):
        entries = _entries()
        events = [e for e in entries if e["kind"] == "event"]
        maps = [e for e in entries if e["kind"] == "map"]
        self.assertEqual(len(events), 8)
        self.assertEqual(len(maps), 32)
        self.assertEqual(len(entries), 40)

    def test_ids_unique(self):
        ids = [e["id"] for e in _entries()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_required_fields_present(self):
        for entry in _entries():
            with self.subTest(entry=entry.get("id")):
                for key in _REQUIRED:
                    self.assertIn(key, entry)

    def test_enums_legal(self):
        for entry in _entries():
            with self.subTest(entry=entry["id"]):
                self.assertIn(entry["kind"], _KIND)
                self.assertIn(entry["damage"], _DAMAGE)
                self.assertIn(entry["battle"], _BATTLE)
                self.assertIn(entry["confidence"], _CONFIDENCE)

    def test_sources_nonempty_strings(self):
        for entry in _entries():
            with self.subTest(entry=entry["id"]):
                self.assertIsInstance(entry["sources"], list)
                self.assertTrue(entry["sources"])
                for source in entry["sources"]:
                    self.assertIsInstance(source, str)
                    self.assertTrue(source.strip())

    def test_field_shapes(self):
        for entry in _entries():
            with self.subTest(entry=entry["id"]):
                self.assertIsInstance(entry["name"], str)
                self.assertTrue(entry["name"].strip())
                self.assertIsInstance(entry["aliases"], list)
                self.assertIsInstance(entry["tips"], list)
                self.assertIsInstance(entry["bonus_swords"], list)
                self.assertIsInstance(entry["enemy_fast_spear"], bool)
                for key in ("map_points", "recommended_level"):
                    value = entry[key]
                    self.assertTrue(value is None or type(value) is int,
                                    f"{key} 必须是整数或 null")
                self.assertTrue(entry["bonus_rule"] is None
                                or isinstance(entry["bonus_rule"], str))

    def test_map_entries_have_self_consistent_chapter_map_no(self):
        maps = [e for e in _entries() if e["kind"] == "map"]
        self.assertEqual({e["chapter"] for e in maps}, set(range(1, 9)))
        for entry in maps:
            with self.subTest(entry=entry["id"]):
                self.assertEqual(entry["id"],
                                 f"map-{entry['chapter']}-{entry['map_no']}")
                self.assertIn(entry["map_no"], (1, 2, 3, 4))

    def test_nulls_preserved_where_research_says_unknown(self):
        """null 是「没查到可靠来源」的透明性设计，落库必须保留。"""
        by_id = {e["id"]: e for e in _entries()}
        self.assertIsNone(by_id["tokumei"]["battle"])      # 特命调查无统一昼夜口径
        self.assertIsNone(by_id["yosari"]["bonus_rule"])   # 异去没有加成刀机制
        self.assertIsNone(by_id["raid"]["map_points"])     # 联队战连战无地图点数

    def test_load_guide_rejects_missing_file(self):
        from pathlib import Path
        with self.assertRaises(ValueError) as ctx:
            load_guide(Path("不存在的攻略库.json"))
        self.assertIn("攻略库", str(ctx.exception))

    def test_load_guide_rejects_broken_shape(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            bad = Path(tmp) / "guide.json"
            bad.write_text(json.dumps({"oops": []}), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_guide(bad)
            self.assertIn("entries", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
