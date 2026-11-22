# -*- coding: utf-8 -*-
"""活动开幕参谋 API：POST /api/event-briefing 的契约测试。

刀帐数据源 patch 掉（touken.sword_archive.get_sword_archive），
不依赖真实用户数据目录的盘点产物。
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import server
from panel.event_briefing_api import archive_to_roster


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
    _entry(name_zh="拔丸", sword_type="太刀", level=22, fatigue=60),
    _entry(name_zh="药研藤四郎", sword_type="短刀", level=99,
           form_status="kiwame", fatigue=40),
    # 等级没读出来：参谋侧应丢掉，不进编队候选
    _entry(name_zh="笑面青江", sword_type="胁差", level=None, fatigue=None),
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
