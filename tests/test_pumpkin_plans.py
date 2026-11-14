import unittest
from unittest.mock import patch

from panel.server import (_build_daily, _build_edocastle, _build_osaka,
                          _build_pumpkin, _build_raid, _wrap_inventory,
                          list_scripts)

# 面板实际注册的是包装器（开工/收工盘点）；FakeAgent 没有 status_snapshot_stream，
# 正好验证“无快照能力自动跳过盘点”的兜底路径
wrap = lambda builder: _wrap_inventory("T", builder)


class FakeAgent:
    def __init__(self):
        self.daily_args = None
        self.edocastle_args = None
        self.pumpkin_args = None
        self.raid_args = None
        self.yosari_args = None
        self.osaka_args = None

    def daily_stream(self, **kwargs):
        self.daily_args = kwargs
        yield "daily"

    def pumpkin_stream(self, **kwargs):
        self.pumpkin_args = kwargs
        yield "pumpkin"

    def edocastle_stream(self, **kwargs):
        self.edocastle_args = kwargs
        yield "edocastle"

    def raid_stream(self, **kwargs):
        self.raid_args = kwargs
        yield "raid"

    def yosari_stream(self, **kwargs):
        self.yosari_args = kwargs
        yield "yosari"

    def osaka_stream(self, **kwargs):
        self.osaka_args = kwargs
        yield "osaka"


class PumpkinPlanTests(unittest.TestCase):
    def test_edocastle_exposes_the_formation_choices_it_actually_uses(self):
        fields = list_scripts()["edocastle"]["params"]
        by_key = {field["key"]: field for field in fields}
        self.assertEqual(by_key["runs"]["label"], "出阵次数")
        self.assertEqual(by_key["runs"]["min"], 1)
        self.assertNotIn("help", by_key["runs"])
        self.assertEqual(by_key["use_koban_refill"]["label"], "是否补充手形")
        self.assertEqual(by_key["formation_mode"]["options"],
                         [["manual", "手动阵形"], ["auto", "自动阵形"]])
        # 阵形策略选项已拆：手动=固定点所选阵形，自动=游戏选、抓瞎时
        # 脚本先认有利标记再落兜底
        self.assertNotIn("formation_strategy", by_key)
        self.assertNotIn("visibleWhen", by_key["formation"])
        self.assertEqual(by_key["formation"]["options"][-1], ["逆行阵", "逆行阵"])

    def test_edocastle_always_uses_a_positive_run_limit(self):
        agent = FakeAgent()
        list(_build_edocastle(agent, None, {
            "team_no": "3", "runs": "9", "use_koban_refill": False,
        }))
        self.assertEqual(agent.edocastle_args["max_runs"], 9)

        list(_build_edocastle(agent, None, {
            "team_no": "3", "max_runs": "0", "use_koban_refill": True,
        }))
        self.assertEqual(agent.edocastle_args["max_runs"], 6)

        list(_build_edocastle(agent, None, {
            "team_no": "3", "max_runs": "7", "use_koban_refill": True,
        }))
        self.assertEqual(agent.edocastle_args["max_runs"], 7)

    def test_osaka_formation_mode_matches_the_sortie_panel_semantics(self):
        fields = list_scripts()["osaka"]["params"]
        by_key = {field["key"]: field for field in fields}
        self.assertEqual(by_key["formation_mode"]["options"],
                         [["manual", "手动阵形"], ["auto", "自动阵形"]])
        self.assertNotIn("formation_strategy", by_key)
        self.assertNotIn("visibleWhen", by_key["formation"])
        self.assertEqual(by_key["repair_threshold"]["options"],
                         [["light", "轻伤时停止"],
                          ["medium", "中伤时停止"],
                          ["heavy", "重伤时停止"]])
        self.assertEqual(by_key["repair_on_injury"]["options"][-1],
                         ["stop", "返回本丸，不进行手入"])
        self.assertTrue(by_key["auto_equip"]["default"])
        self.assertNotIn("visibleWhen", by_key["auto_equip"])

        agent = FakeAgent()
        with patch("panel.server._make_agent", return_value=agent):
            list(wrap(_build_osaka)("config.json", {
                "team_no": "3", "runs": "2", "formation_mode": "auto",
                "formation": "逆行阵",
                "repair_threshold": "medium", "repair_on_injury": "repair_stop",
            }))
        self.assertEqual(agent.osaka_args["formation_mode"], "auto")
        self.assertEqual(agent.osaka_args["repair_threshold"], "medium")
        self.assertEqual(agent.osaka_args["injury_action"], "repair_stop")
        self.assertTrue(agent.osaka_args["auto_equip"])

    def test_four_sortie_forms_share_one_quantity_key(self):
        scripts = list_scripts()
        for name in ("raid", "pumpkin", "sortie", "yosari"):
            keys = [field.get("key") for field in scripts[name]["params"]]
            self.assertIn("runs", keys, name)
        for name in ("raid", "pumpkin"):
            keys = [field.get("key") for field in scripts[name]["params"]]
            self.assertNotIn("repair_threshold", keys, name)
        raid_keys = [field.get("key") for field in scripts["raid"]["params"]]
        pumpkin_keys = [field.get("key") for field in scripts["pumpkin"]["params"]]
        self.assertIn("auto_march", raid_keys)
        self.assertNotIn("auto_march", pumpkin_keys)
        for name in ("sortie", "yosari"):
            keys = [field.get("key") for field in scripts[name]["params"]]
            self.assertIn("auto_march", keys, name)
            self.assertIn("repair_threshold", keys, name)
            self.assertIn("auto_equip", keys, name)

    def test_yosari_uses_chapter_and_map_fields_like_sortie(self):
        fields = list_scripts()["yosari"]["params"]
        chapter = next(field for field in fields if field.get("key") == "chapter")
        map_no = next(field for field in fields if field.get("key") == "map_no")
        self.assertEqual(chapter["options"], [["1", "1章"]])
        self.assertEqual(map_no["options"],
                         [["1", "1图"], ["2", "2图"], ["3", "3图"], ["4", "4图"]])

    def test_sortie_forms_put_map_before_team_and_runs(self):
        scripts = list_scripts()
        expected = {
            "raid": ["map_no", "team_no", "runs", "auto_refill"],
            "pumpkin": ["difficulty", "team_no", "runs", "auto_refill"],
            "sortie": ["chapter", "map_no", "team_no", "runs"],
            "yosari": ["chapter", "map_no", "team_no", "runs"],
        }
        for name, prefix in expected.items():
            keys = [field.get("key") for field in scripts[name]["params"]]
            self.assertEqual(keys[:len(prefix)], prefix, name)
        self.assertNotIn("watch", [field.get("key") for field in scripts["pumpkin"]["params"]])

    def test_raid_selected_map_reaches_flow(self):
        agent = FakeAgent()
        with patch("panel.server._make_agent", return_value=agent):
            list(wrap(_build_raid)("config.json", {"map_no": "2", "runs": "5", "team_no": "4"}))
        self.assertEqual(agent.raid_args["difficulty_no"], 2)
        self.assertEqual(agent.raid_args["max_rounds"], 5)
        self.assertEqual(agent.raid_args["team_no"], 4)
        self.assertFalse(agent.raid_args["auto_buy_ticket"])

    def test_standalone_form_uses_shared_run_count_field(self):
        fields = list_scripts()["pumpkin"]["params"]
        difficulty = next(field for field in fields if field.get("key") == "difficulty")
        budget = next(field for field in fields if field.get("key") == "runs")
        self.assertEqual(difficulty["default"], "1")
        self.assertEqual(budget["label"], "出阵次数")
        refill = next(field for field in fields if field.get("key") == "auto_refill")
        self.assertFalse(refill["default"])
        self.assertFalse(any(field.get("key") == "run_mode" for field in fields))

    def test_daily_pumpkin_plan_is_independent(self):
        agent = FakeAgent()
        params = {"sortie_mode": "pumpkin", "team_no": "2",
                  "pumpkin_difficulty": "2", "pumpkin_runs": "6",
                  "pumpkin_watch": ["三日月宗近"]}
        with patch("panel.server._make_agent", return_value=agent), patch(
            "panel.server._load_panel_settings", return_value={}
        ):
            list(wrap(_build_daily)("config.json", params))

        plan = agent.daily_args["sortie_override"]
        self.assertEqual(plan, {"mode": "pumpkin", "difficulty": 2,
                                "team_no": 2, "watch_names": ["三日月宗近"],
                                "max_skips": 6})
        daily_modes = next(field for field in list_scripts()["daily"]["params"]
                           if field.get("key") == "sortie_mode")
        self.assertIn("pumpkin", [value for value, _ in daily_modes["options"]])
        self.assertEqual(daily_modes["default"], "none")

    def test_daily_forge_times_field_reaches_daily_stream(self):
        agent = FakeAgent()
        with patch("panel.server._make_agent", return_value=agent), patch(
            "panel.server._load_panel_settings", return_value={}
        ):
            list(wrap(_build_daily)("config.json", {"forge_times": "5"}))
        self.assertEqual(agent.daily_args["forge_times"], 5)
        # 不填时不覆盖配置里的 daily.forge_times
        with patch("panel.server._make_agent", return_value=agent), patch(
            "panel.server._load_panel_settings", return_value={}
        ):
            list(wrap(_build_daily)("config.json", {}))
        self.assertIsNone(agent.daily_args["forge_times"])
        field = next(field for field in list_scripts()["daily"]["params"]
                     if field.get("key") == "forge_times")
        self.assertEqual(field["default"], 3)
        # 上限与锻刀积木（times）一致，日课也能一次排满 200 炉
        self.assertEqual(field["max"], 200)

    def test_daily_forge_recipe_reaches_daily_stream(self):
        agent = FakeAgent()
        params = {
            "recipe_charcoal": "333", "recipe_steel": "444",
            "recipe_coolant": "555", "recipe_whetstone": "666",
        }
        with patch("panel.server._make_agent", return_value=agent), patch(
            "panel.server._load_panel_settings", return_value={}
        ):
            list(wrap(_build_daily)("config.json", params))
        self.assertEqual(agent.daily_args["forge_recipe"],
                         [333, 444, 555, 666])

    def test_daily_can_schedule_yosari(self):
        agent = FakeAgent()
        params = {"sortie_mode": "yosari", "team_no": "4",
                  "yosari_map_no": "3", "yosari_runs": "8",
                  "yosari_auto_refill": True,
                  "auto_march": False, "repair_threshold": "medium",
                  "rotate_captain": True, "rotate_captain_margin": "5"}
        with patch("panel.server._make_agent", return_value=agent), patch(
            "panel.server._load_panel_settings", return_value={}
        ):
            list(wrap(_build_daily)("config.json", params))
        plan = agent.daily_args["sortie_override"]
        self.assertEqual(plan["mode"], "yosari")
        self.assertEqual(plan["map_no"], 3)
        self.assertEqual(plan["team_no"], 4)
        self.assertEqual(plan["loops"], 8)
        self.assertTrue(plan["auto_refill"])
        self.assertFalse(plan["auto_march"])
        self.assertEqual(plan["repair_threshold"], "medium")
        self.assertTrue(plan["rotate_captain"])
        self.assertEqual(plan["rotate_captain_margin"], 5)

    def test_daily_battle_settings_ignore_config_page(self):
        # issue#7 切割：配置页存的值不再漏进日课，缺键回落硬默认
        agent = FakeAgent()
        params = {"sortie_mode": "yosari"}
        saved = {"params": {"yosari": {"auto_march": False,
                                          "repair_threshold": "medium",
                                          "rotate_captain": True}}}
        with patch("panel.server._make_agent", return_value=agent), patch(
            "panel.server._load_panel_settings", return_value=saved
        ):
            list(wrap(_build_daily)("config.json", params))
        plan = agent.daily_args["sortie_override"]
        self.assertTrue(plan["auto_march"])            # 硬默认，不是配置页的 False
        self.assertEqual(plan["repair_threshold"], "light")
        self.assertFalse(plan["rotate_captain"])

    def test_daily_sortie_can_retreat_before_boss(self):
        agent = FakeAgent()
        params = {"sortie_mode": "sortie", "chapter": "5", "map_no": "4",
                  "loops": "3", "team_no": "2", "retreat_before_boss": True,
                  "auto_march": False, "rotate_captain": True,
                  "rotate_captain_margin": "20"}
        with patch("panel.server._make_agent", return_value=agent), patch(
            "panel.server._load_panel_settings", return_value={}
        ):
            list(wrap(_build_daily)("config.json", params))

        plan = agent.daily_args["sortie_override"]
        self.assertTrue(plan["retreat_before_boss"])
        self.assertFalse(plan["auto_march"])
        self.assertTrue(plan["rotate_captain"])
        self.assertEqual(plan["rotate_captain_margin"], 20)
        field = next(field for field in list_scripts()["daily"]["params"]
                     if field.get("key") == "retreat_before_boss")
        self.assertEqual(field["visibleWhen"], {"key": "sortie_mode", "is": "sortie"})

    def test_daily_can_schedule_osaka_with_own_battle_strategy(self):
        agent = FakeAgent()
        params = {"sortie_mode": "osaka", "team_no": "4",
                  "osaka_runs": "15", "osaka_select_floor": True,
                  "osaka_target_floor": "88",
                  "formation_mode": "auto", "repair_threshold": "medium",
                  "repair_on_injury": "repair_stop", "auto_equip": False}
        with patch("panel.server._make_agent", return_value=agent), patch(
            "panel.server._load_panel_settings", return_value={}
        ):
            list(wrap(_build_daily)("config.json", params))
        plan = agent.daily_args["sortie_override"]
        self.assertEqual(plan["mode"], "osaka")
        self.assertEqual(plan["team_no"], 4)
        self.assertEqual(plan["loops"], 15)
        self.assertTrue(plan["select_floor"])
        self.assertEqual(plan["target_floor"], 88)
        self.assertEqual(plan["formation_mode"], "auto")
        self.assertEqual(plan["repair_threshold"], "medium")
        self.assertEqual(plan["repair_on_injury"], "repair_stop")
        self.assertFalse(plan["auto_equip"])
        daily_modes = next(field for field in list_scripts()["daily"]["params"]
                           if field.get("key") == "sortie_mode")
        self.assertIn("osaka", [value for value, _ in daily_modes["options"]])

    def test_daily_uses_own_expedition_plan(self):
        agent = FakeAgent()
        common = {"common_plan": [
            {"team_no": 2, "map_code": "B2", "enabled": True},
            {"team_no": 3, "map_code": "C3", "enabled": False},
        ]}
        with patch("panel.server._make_agent", return_value=agent), \
                patch("panel.server._load_panel_settings", return_value={}), \
                patch("panel.scheduler.load_config", return_value=common), \
                patch("panel.scheduler.find_map", return_value={
                    "code": "B2", "name": "享保の大飢饉", "era": 2, "slot": 2,
                }):
            list(wrap(_build_daily)("config.json", {"steps": ["远征"], "expedition": {"enabled_2": True, "map_2": "B2"}}))

        self.assertEqual(agent.daily_args["expedition_override"], [{
            "enabled": True, "team_no": 2, "map_code": "B2", "era": 2, "map_slot": 2,
            "sakura_before_dispatch": False, "repair_threshold": "light",
            "map_name": "享保の大飢饉",
        }])

    def test_standalone_plan_uses_targets_and_selected_handshape_budget(self):
        agent = FakeAgent()
        with patch("panel.server._make_agent", return_value=agent):
            list(wrap(_build_pumpkin)("config.json", {
                "watch": "三日月宗近， 小狐丸", "team_no": "3", "runs": "12"
            }))

        self.assertEqual(agent.pumpkin_args["watch_names"], ["三日月宗近", "小狐丸"])
        self.assertEqual(agent.pumpkin_args["max_skips"], 12)
        self.assertFalse(agent.pumpkin_args["auto_refill"])

    def test_ticket_refill_choice_reaches_raid_but_pumpkin_stays_safe(self):
        agent = FakeAgent()
        with patch("panel.server._make_agent", return_value=agent):
            list(wrap(_build_raid)("config.json", {"runs": "99", "auto_refill": True}))
            list(wrap(_build_pumpkin)("config.json", {"runs": "99", "auto_refill": True}))
        self.assertTrue(agent.raid_args["auto_buy_ticket"])
        self.assertFalse(agent.pumpkin_args["auto_refill"])

    def test_old_token_budget_is_still_read(self):
        agent = FakeAgent()
        with patch("panel.server._make_agent", return_value=agent):
            list(wrap(_build_pumpkin)("config.json", {"max_skips": "7"}))
        self.assertEqual(agent.pumpkin_args["max_skips"], 7)


if __name__ == "__main__":
    unittest.main()
