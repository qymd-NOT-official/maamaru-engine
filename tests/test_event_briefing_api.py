# -*- coding: utf-8 -*-
"""活动开幕参谋 API：POST /api/event-briefing 的契约测试。

刀帐数据源 patch 掉（touken.sword_archive.get_sword_archive），
不依赖真实用户数据目录的盘点产物。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import server
from panel.event_briefing_api import archive_to_roster, briefing_preset_name
from touken import custom_formations as cf


def _archive(entries, done=True):
    return {
        "done": done,
        "reason": None if done else "还没有完整盘点",
        "observed_at": 1700000000,
        "snapshot_id": "snap-1",
        "summary": {"total": len(entries), "human_confirmed": 0,
                    "keepers": 0, "attention_count": 0},
        "entries": entries,
        "attention": [],
    }


def _entry(**over):
    base = {
        "observation_id": "9:1",
        "serial_id": 41,
        "data_source": "ocr",
        "sword_catalog_id": "touken_141_kesa",
        "name_zh": "后家兼光",
        "sword_type": "打刀",
        "level": 75,
        "form_status": "normal",
        "fatigue": 88,
        "stats": {"atk": 100},
    }
    base.update(over)
    return base


_ROSTER_ENTRIES = [
    _entry(),
    _entry(observation_id="9:2", name_zh="拔丸", sword_type="太刀", level=22, fatigue=60),
    _entry(observation_id="9:3", name_zh="药研藤四郎", sword_type="短刀", level=99,
           form_status="kiwame", fatigue=40,
           sword_catalog_id="touken_049_yagen"),
    # 等级没读出来：参谋侧应丢掉，不进编队候选
    _entry(observation_id="9:4", name_zh="笑面青江", sword_type="胁差", level=None, fatigue=None),
]

_EVENT = {
    "name": "战力扩充计划",
    "damage": "real",
    "battle": "night",
    "map_points": 5,
    "recommended_level": 60,
    "bonus_swords": ["后家兼光", "拔丸"],
}


class EventBriefingApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def _post(self, body, entries=None, done=True):
        archive = _archive(_ROSTER_ENTRIES if entries is None else entries, done=done)
        with patch("touken.sword_archive.get_sword_archive", return_value=archive):
            return self.client.post("/api/event-briefing", json=body)

    def test_happy_path_contract(self):
        resp = self._post({**_EVENT, "enemy_fast_spear": False})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for key in ("event", "summary", "bonus", "formation",
                    "troops", "flower", "warnings"):
            self.assertIn(key, data)
        self.assertIn("战力扩充计划", data["summary"])
        bonus = {b["name"]: b for b in data["bonus"]}
        self.assertEqual(bonus["后家兼光"]["verdict"], "carry")
        self.assertEqual(bonus["拔丸"]["verdict"], "burden")
        self.assertIn("reason", bonus["后家兼光"])
        # 夜战候选只出短刀/胁差/打刀；等级 None 的笑面青江不在
        candidates = data["formation"]["candidates"]
        self.assertLessEqual(len(candidates), 6)
        names = [c["name"] for c in candidates]
        self.assertIn("药研藤四郎", names)
        self.assertNotIn("笑面青江", names)
        candidate = candidates[0]
        for key in ("name", "type", "level", "form", "fatigue", "injury", "slots"):
            self.assertIn(key, candidate)
        self.assertIsNone(candidate["injury"])  # 刀帐没有伤势字段，如实为空

    def test_archive_not_ready_reports_guide(self):
        resp = self._post(_EVENT, done=False)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("盘点", resp.json()["detail"])

    def test_archive_empty_reports_guide(self):
        resp = self._post(_EVENT, entries=[])
        self.assertEqual(resp.status_code, 503)
        self.assertIn("盘点", resp.json()["detail"])

    def test_validation_error_is_400_player_facing(self):
        resp = self._post({**_EVENT, "damage": "mental"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("virtual", resp.json()["detail"])

    def test_garbage_body_rejected(self):
        archive = _archive(_ROSTER_ENTRIES)
        with patch("touken.sword_archive.get_sword_archive", return_value=archive):
            resp = self.client.post("/api/event-briefing", json=[1, 2, 3])
        self.assertEqual(resp.status_code, 400)

    def test_enemy_fast_spear_reaches_engine(self):
        archive = _archive(_ROSTER_ENTRIES)
        with patch("touken.sword_archive.get_sword_archive", return_value=archive), \
                patch("touken.event_briefing.build_briefing") as engine:
            engine.return_value = {"ok": True}
            resp = self.client.post("/api/event-briefing",
                                    json={**_EVENT, "enemy_fast_spear": True})
        self.assertEqual(resp.status_code, 200)
        _, kwargs = engine.call_args
        # 敌方快枪独立传递，活动字段原样透传（enemy_fast_spear 不算活动字段）
        self.assertIs(kwargs["enemy_fast_spear"], True)
        event_arg = engine.call_args.args[0]
        self.assertEqual(event_arg["damage"], "real")
        self.assertNotIn("enemy_fast_spear", event_arg)


class EventGuideApiTests(unittest.TestCase):
    """GET /api/event-briefing/guide：攻略库全量下发，坏了 500 喊出来。"""

    def setUp(self):
        self.client = TestClient(server.app)

    def test_happy_path_returns_all_entries(self):
        resp = self.client.get("/api/event-briefing/guide")
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 40)
        kinds = {e["kind"] for e in entries}
        self.assertEqual(kinds, {"event", "map"})
        first = entries[0]
        for key in ("id", "name", "damage", "battle", "confidence",
                    "sources", "tips"):
            self.assertIn(key, first)

    def test_broken_guide_file_is_500_honest(self):
        with patch("touken.event_briefing.load_guide",
                   side_effect=ValueError("活动参谋攻略库读不出来：boom")):
            resp = self.client.get("/api/event-briefing/guide")
        self.assertEqual(resp.status_code, 500)
        self.assertIn("攻略库", resp.json()["detail"])


class BriefingFormationApiTests(unittest.TestCase):
    """POST /api/event-briefing/formation：候选回刀帐认人、存预设、重名覆盖。"""

    def setUp(self):
        # 存储隔离照 tests/test_custom_formations_panel.py 的口径：
        # 逐用例把 cf.STATE_DIR 指到临时目录，绝不碰真实用户数据。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        state = Path(self._tmp.name) / "state"
        state.mkdir(parents=True)
        patcher = patch.object(cf, "STATE_DIR", state)
        patcher.start()
        self.addCleanup(patcher.stop)
        cf.save_formations([])
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def _post(self, body, entries=None, done=True):
        archive = _archive(_ROSTER_ENTRIES if entries is None else entries, done=done)
        with patch("touken.sword_archive.get_sword_archive", return_value=archive):
            return self.client.post("/api/event-briefing/formation", json=body)

    def _body(self, **over):
        body = {
            "event_name": "战力扩充计划",
            "target_team": 2,
            "candidates": [
                {"name": "药研藤四郎", "form": "kiwame", "level": 99},
                {"name": "后家兼光", "form": "normal", "level": 75},
            ],
        }
        body.update(over)
        return body

    def test_save_resolves_full_identity_and_persists(self):
        resp = self._post(self._body())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["overwritten"])
        record = data["formation"]
        self.assertEqual(record["name"], "活动·战力扩充计划")
        self.assertEqual(record["target_team"], 2)
        self.assertEqual(sorted(record["slots"]), ["1", "2"])
        slot = record["slots"]["1"]
        # 槽位快照照编队页选人那套身份字段落盘
        self.assertEqual(slot["name_zh"], "药研藤四郎")
        self.assertEqual(slot["form_status"], "kiwame")
        self.assertEqual(slot["level"], 99)
        self.assertEqual(slot["observation_id"], "9:3")
        self.assertEqual(slot["sword_catalog_id"], "touken_049_yagen")
        self.assertEqual(slot["stats"], {"atk": 100})
        # 真落盘了
        self.assertEqual(cf.load_formations(), [record])

    def test_same_name_overwrites_in_place_keeping_id(self):
        first = self._post(self._body()).json()["formation"]
        resp = self._post(self._body(target_team=4))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["overwritten"])
        record = data["formation"]
        self.assertEqual(record["id"], first["id"])
        self.assertEqual(record["target_team"], 4)
        self.assertEqual(record["created_at"], first["created_at"])
        self.assertEqual(len(cf.load_formations()), 1)

    def test_full_five_presets_reports_honestly(self):
        for index in range(5):
            self._post(self._body(event_name=f"活动{index}"))
        resp = self._post(self._body(event_name="第六个活动"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("最多 5 套", resp.json()["reason"])

    def test_archive_not_ready_reports_guide(self):
        resp = self._post(self._body(), done=False)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("盘点", resp.json()["detail"])

    def test_unknown_candidate_rejected_without_saving(self):
        resp = self._post(self._body(candidates=[
            {"name": "不存在的刀", "form": "normal", "level": 1}]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("对不上号", resp.json()["reason"])
        self.assertEqual(cf.load_formations(), [])

    def test_level_drift_accepted_when_name_form_unique(self):
        # 同名同形态只有一振：出阵升了级也认得回来（等级是参考）
        resp = self._post(self._body(candidates=[
            {"name": "药研藤四郎", "form": "kiwame", "level": 98}]))
        self.assertEqual(resp.status_code, 200)
        slot = resp.json()["formation"]["slots"]["1"]
        self.assertEqual(slot["level"], 99)  # 存刀帐里的当前等级

    def test_duplicate_name_form_with_wrong_level_is_ambiguous(self):
        entries = [
            _entry(name_zh="薬研", sword_type="短刀", level=90,
                   form_status="kiwame", observation_id="9:1"),
            _entry(name_zh="薬研", sword_type="短刀", level=95,
                   form_status="kiwame", observation_id="9:2",
                          sword_catalog_id="touken_049_yagen_dup"),
        ]
        resp = self._post(self._body(candidates=[
            {"name": "薬研", "form": "kiwame", "level": 99}],), entries=entries)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("分不清", resp.json()["reason"])
        self.assertEqual(cf.load_formations(), [])

    def test_exact_level_picks_the_right_duplicate(self):
        entries = [
            _entry(name_zh="薬研", sword_type="短刀", level=90,
                   form_status="kiwame", observation_id="9:1"),
            _entry(name_zh="薬研", sword_type="短刀", level=95,
                   form_status="kiwame", observation_id="9:2",
                          sword_catalog_id="touken_049_yagen_dup"),
        ]
        resp = self._post(self._body(candidates=[
            {"name": "薬研", "form": "kiwame", "level": 95}],), entries=entries)
        self.assertEqual(resp.status_code, 200)
        slot = resp.json()["formation"]["slots"]["1"]
        self.assertEqual(slot["observation_id"], "9:2")

    def test_same_sword_twice_rejected(self):
        resp = self._post(self._body(candidates=[
            {"name": "药研藤四郎", "form": "kiwame", "level": 99},
            {"name": "药研藤四郎", "form": "kiwame", "level": 99},
        ]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("同一振", resp.json()["reason"])

    def test_bad_target_team_rejected(self):
        resp = self._post(self._body(target_team="第一队"))
        self.assertEqual(resp.status_code, 400)
        resp = self._post(self._body(target_team=9))
        self.assertEqual(resp.status_code, 400)

    def test_empty_candidates_rejected(self):
        resp = self._post(self._body(candidates=[]))
        self.assertEqual(resp.status_code, 400)
        resp = self._post(self._body(candidates=[{}] * 7))
        self.assertEqual(resp.status_code, 400)

    def test_missing_identity_fields_rejected(self):
        resp = self._post(self._body(candidates=[{"name": "", "form": "x"}]))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("身份信息不全", resp.json()["reason"])


class PresetNameTests(unittest.TestCase):
    def test_prefix_and_trim(self):
        self.assertEqual(briefing_preset_name(" 战力扩充计划 "), "活动·战力扩充计划")
        self.assertEqual(briefing_preset_name(""), "活动编队")

    def test_truncated_to_contract_limit(self):
        name = briefing_preset_name("超长活动名" * 10)
        self.assertEqual(len(name), 20)
        self.assertTrue(name.startswith("活动·"))


class ArchiveToRosterTests(unittest.TestCase):
    def test_maps_archive_fields_to_roster_contract(self):
        roster = archive_to_roster(_archive([_entry()]))
        self.assertEqual(roster, [{
            "name": "后家兼光",
            "type": "打刀",
            "level": 75,
            "form": "normal",
            "fatigue": 88,
        }])

    def test_missing_fields_pass_through_as_none(self):
        roster = archive_to_roster(_archive([_entry(name_zh=None, sword_type=None,
                                                    level=None, form_status=None,
                                                    fatigue=None)]))
        self.assertEqual(roster[0]["name"], None)
        self.assertEqual(roster[0]["level"], None)
        self.assertEqual(roster[0]["fatigue"], None)

    def test_non_dict_entries_skipped(self):
        roster = archive_to_roster({"entries": ["junk", _entry()], "done": True})
        self.assertEqual(len(roster), 1)


if __name__ == "__main__":
    unittest.main()
