import unittest

from touken import event_briefing


def _event(**overrides):
    base = {
        "name": "战力扩充计划",
        "damage": "real",
        "battle": "night",
        "map_points": 5,
        "recommended_level": 60,
        "bonus_swords": ["后家兼光", "拔丸"],
    }
    base.update(overrides)
    return base


def _roster():
    return [
        {"name": "后家兼光", "type": "打刀", "level": 75, "fatigue": 88},
        {"name": "拔丸", "type": "太刀", "level": 22, "fatigue": 60},
        {"name": "药研藤四郎", "type": "短刀", "level": 99, "form": "kiwame", "fatigue": 40},
        {"name": "笑面青江", "type": "胁差", "level": 70, "fatigue": 95},
        {"name": "烛台切光忠", "type": "太刀", "level": 66, "injury": "中伤", "fatigue": 80},
        {"name": "小夜左文字", "type": "短刀", "level": 45, "fatigue": 12},
    ]


class EventValidationTests(unittest.TestCase):
    def test_rejects_bad_damage_type(self):
        with self.assertRaises(ValueError):
            event_briefing.build_briefing(_event(damage="mental"), [])

    def test_rejects_bad_battle_type(self):
        with self.assertRaises(ValueError):
            event_briefing.build_briefing(_event(battle="space"), [])

    def test_optional_fields_may_be_omitted(self):
        briefing = event_briefing.build_briefing(
            _event(map_points=None, recommended_level=None, bonus_swords=[]), [])
        self.assertIn("没填加成刀", briefing["summary"])


class BonusJudgementTests(unittest.TestCase):
    def test_carry_when_level_meets_recommendation(self):
        briefing = event_briefing.build_briefing(_event(), _roster())
        bonus = {b["name"]: b for b in briefing["bonus"]}
        self.assertEqual(bonus["后家兼光"]["verdict"], "carry")

    def test_burden_when_far_below_recommendation(self):
        briefing = event_briefing.build_briefing(_event(), _roster())
        bonus = {b["name"]: b for b in briefing["bonus"]}
        self.assertEqual(bonus["拔丸"]["verdict"], "burden")

    def test_virtual_damage_lets_low_level_ride_free(self):
        briefing = event_briefing.build_briefing(_event(damage="virtual"), _roster())
        bonus = {b["name"]: b for b in briefing["bonus"]}
        self.assertEqual(bonus["拔丸"]["verdict"], "free")

    def test_missing_sword_reported(self):
        briefing = event_briefing.build_briefing(_event(bonus_swords=["大典太光世"]), _roster())
        self.assertEqual(briefing["bonus"][0]["verdict"], "missing")

    def test_marginal_band(self):
        roster = [{"name": "拔丸", "type": "太刀", "level": 50}]
        briefing = event_briefing.build_briefing(_event(bonus_swords=["拔丸"]), roster)
        self.assertEqual(briefing["bonus"][0]["verdict"], "marginal")


class FormationTests(unittest.TestCase):
    def test_night_battle_picks_short_types(self):
        briefing = event_briefing.build_briefing(_event(), _roster())
        types = {c["type"] for c in briefing["formation"]["candidates"]}
        self.assertTrue(types <= {"短刀", "胁差", "打刀"})

    def test_day_battle_picks_long_types(self):
        briefing = event_briefing.build_briefing(_event(battle="day"), _roster())
        types = {c["type"] for c in briefing["formation"]["candidates"]}
        self.assertTrue(types <= {"太刀", "大太刀"})

    def test_injured_medium_and_above_excluded(self):
        briefing = event_briefing.build_briefing(_event(battle="day"), _roster())
        names = {c["name"] for c in briefing["formation"]["candidates"]}
        self.assertNotIn("烛台切光忠", names)
        self.assertTrue(any("烛台切光忠" in w for w in briefing["warnings"]))

    def test_kiwame_tanto_train_note_shown(self):
        briefing = event_briefing.build_briefing(_event(), _roster())
        self.assertIn("五带一", briefing["formation"]["train_note"])


class TroopTests(unittest.TestCase):
    def test_flower_needing_sword_gets_archers(self):
        # 药研疲劳 40（需要控花）→ 弓兵；疲劳满 88 的打刀 → 投石
        briefing = event_briefing.build_briefing(_event(), _roster())
        troops = {t["name"]: t["troop"] for t in briefing["troops"]}
        self.assertEqual(troops.get("药研藤四郎"), "弓兵")
        self.assertEqual(troops.get("后家兼光"), "投石")

    def test_fast_spear_switches_tanto_to_guns(self):
        briefing = event_briefing.build_briefing(_event(), _roster(), enemy_fast_spear=True)
        troops = {t["name"]: t["troop"] for t in briefing["troops"]}
        # 药研 need_flower 规则命中更早，仍拿弓；满花的极短才会走到铳兵规则
        self.assertEqual(troops.get("药研藤四郎"), "弓兵")


class FlowerTests(unittest.TestCase):
    def test_low_point_map_gives_up_flower_control(self):
        briefing = event_briefing.build_briefing(_event(map_points=3), _roster())
        self.assertTrue(any("控不了花" in tip for tip in briefing["flower"]))

    def test_normal_map_includes_archer_rule(self):
        briefing = event_briefing.build_briefing(_event(), _roster())
        self.assertTrue(any("弓" in tip and "誉" in tip for tip in briefing["flower"]))

    def test_unknown_points_says_so(self):
        briefing = event_briefing.build_briefing(_event(map_points=None), _roster())
        self.assertTrue(any("没填" in tip for tip in briefing["flower"]))


class WarningTests(unittest.TestCase):
    def test_tired_sword_warned(self):
        briefing = event_briefing.build_briefing(_event(), _roster())
        self.assertTrue(any("小夜左文字" in w for w in briefing["warnings"]))


class RulesFileTests(unittest.TestCase):
    def test_rules_file_loads(self):
        rules = event_briefing.load_rules()
        self.assertIn("troops", rules)
        self.assertIn("sources", rules)

    def test_bad_rules_file_raises(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                event_briefing.load_rules(path)


if __name__ == "__main__":
    unittest.main()
