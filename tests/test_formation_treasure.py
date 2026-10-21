"""宝物预设的唯一性、占用确认与失败恢复。只模拟点击，不连真机。"""

import unittest
from unittest.mock import patch

from touken.device_adapter import Point
from touken.flows import formation_treasure as ft


NAME = "锷·月下梅树透图"
TARGET = {"name": NAME, "level": 1, "affection": 0}


def card(owner=False):
    tokens = [(NAME, Point(1045, 218)), ("1级", Point(907, 295)),
              ("爱用度0", Point(999, 189))]
    if owner:
        tokens.append(("装备中", Point(1203, 190)))
    return tokens


class ParserTests(unittest.TestCase):
    def test_count_and_unique_fingerprint(self):
        self.assertEqual(ft.parse_treasure_count(
            [("未装备/总所持 0个/ 1个", Point(1014, 149))]), (0, 1))
        self.assertEqual(ft.match_single_treasure(card(), TARGET), Point(1045, 218))
        self.assertIsNone(ft.match_single_treasure(card() + [(NAME, Point(1045, 420))], TARGET))
        self.assertIsNone(ft.match_single_treasure(card(), {**TARGET, "level": 2}))
        self.assertIsNone(ft.match_single_treasure(card(), {**TARGET, "affection": 1}))

    def test_owner_only_from_transfer_question(self):
        self.assertEqual(ft.transfer_owner(
            [("确认卸下小豆长光的装备吗?", Point(638, 331))]), "小豆长光")
        self.assertIsNone(ft.transfer_owner([("装备中", Point(1203, 190))]))

    def test_visible_cards_require_complete_fingerprint(self):
        tokens = [("爱用度0", Point(995, 189)), ("曜变天目", Point(995, 218)),
                  ("1级", Point(906, 294)), ("装备中", Point(1200, 190)),
                  ("爱用度0", Point(995, 403)), (NAME, Point(1045, 432)),
                  ("1级", Point(907, 508)),
                  ("爱用度0", Point(995, 617)), ("三所物·菊", Point(1010, 646))]
        self.assertEqual([(name, level, affection, occupied)
                          for name, level, affection, occupied, _ in ft.visible_treasures(tokens)],
                         [("曜变天目", 1, 0, True), (NAME, 1, 0, False)])

    @patch.object(ft.time, "sleep")
    def test_four_card_scroll_finds_last_then_first(self, _sleep):
        class ListMaa:
            page = 0

            def screenshot(self, force=False):
                return object()

            def swipe(self, _x1, y1, _x2, _y2, _duration):
                self.page = 1 if y1 > 400 else 0

            def ocr_all(self, _roi):
                rows = ([('曜变天目', 218), (NAME, 432)] if self.page == 0
                        else [('三所物·菊', 304), ('三所物·狮子', 518)])
                tokens = []
                for name, y in rows:
                    tokens.extend([('爱用度0', Point(999, y - 29)),
                                   (name, Point(1010, y)),
                                   ('1级', Point(907, y + 76))])
                return tokens

        device = ListMaa()
        last = ft.find_treasure_in_list(
            device, {'name': '三所物·狮子', 'level': 1, 'affection': 0}, 4)
        self.assertEqual(last, (False, Point(1010, 518)))
        device.page = 0
        first = ft.find_treasure_in_list(
            device, {'name': '曜变天目', 'level': 1, 'affection': 0}, 4)
        self.assertEqual(first, (False, Point(1010, 218)))
        self.assertEqual(device.page, 0)


class FakeMaa:
    def __init__(self, *, total=1, equipped=False, owner_known=True,
                 count_free=None):
        self.state = "formation"
        self.total = total
        self.equipped = equipped
        self.owner_known = owner_known
        self.count_free = count_free
        self.slot = None
        self.clicks = []
        self.confirm_y = 277

    def swipe(self, *_args):
        pass

    def screenshot(self, force=False):
        return object()

    def click(self, point):
        xy = (point.x, point.y)
        self.clicks.append(xy)
        if self.state == "formation" and xy == (964, 160):
            self.state = "equip"
        elif self.state == "equip" and xy == (634, 365):
            self.state = "list"
        elif self.state == "list" and xy == (1045, 218):
            self.state = "preview"
            self.confirm_y = 277
        elif self.state == "preview" and xy == (1051, 277):
            if self.equipped:
                self.state = "modal"
            else:
                self.slot = NAME
                self.state = "equip"
        elif self.state == "modal" and xy == (497, 468):
            self.slot = NAME
            self.state = "equip"
        elif self.state == "modal" and xy == (785, 468):
            self.state = "list"
        elif xy == (152, 26):
            self.state = "formation"

    def ocr(self, expected, roi):
        found = ((expected == "部队编成" and self.state == "formation")
                 or (expected == "更换装备" and self.state in ("equip", "list", "preview"))
                 or (expected == "宝物" and self.state == "list")
                 or (expected == "确定" and self.state == "preview")
                 or (expected == "否" and self.state == "modal"))
        if found and expected == "确定":
            return Point(1051, self.confirm_y)
        return Point(640, 25) if found else None

    def ocr_all(self, roi):
        rect = (roi.x, roi.y, roi.x2, roi.y2)
        if rect == ft._COUNT:
            free = self.count_free if self.count_free is not None else (0 if self.equipped else 1)
            return [(f"未装备/总所持 {free}个/{self.total}个",
                     Point(1014, 149))]
        if rect == ft._CARD:
            return card(self.equipped)
        if rect == ft._SLOT:
            return [(self.slot or "空", Point(735, 370))]
        if rect == ft._TRANSFER and self.state == "modal" and self.owner_known:
            return [("确认卸下小豆长光的装备吗？", Point(640, 330))]
        return []


class EquipTests(unittest.TestCase):
    @patch.object(ft.time, "sleep")
    def test_occupancy_contradiction_stops_before_card_click(self, _sleep):
        device = FakeMaa(equipped=True, count_free=1)
        agent = type("Agent", (), {"device": device})()
        gen = ft.equip_preset_treasure_stream(agent, 1, TARGET)
        while True:
            try:
                next(gen)
            except StopIteration as stop:
                self.assertFalse(stop.value)
                break
        self.assertNotIn((1045, 218), device.clicks)
        self.assertEqual(device.state, "formation")

    @patch.object(ft.time, "sleep")
    def test_multi_item_inventory_stops_before_selection(self, _sleep):
        device = FakeMaa(total=2)
        agent = type("Agent", (), {"device": device})()
        gen = ft.equip_preset_treasure_stream(agent, 1, TARGET)
        while True:
            try:
                next(gen)
            except StopIteration as stop:
                self.assertFalse(stop.value)
                break
        self.assertEqual(device.clicks, [(964, 160), (634, 365), (152, 26)])

    @patch.object(ft.time, "sleep")
    def test_unreadable_owner_cancels_transfer(self, _sleep):
        device = FakeMaa(equipped=True, owner_known=False)
        agent = type("Agent", (), {"device": device})()
        gen = ft.equip_preset_treasure_stream(agent, 1, TARGET)
        while True:
            try:
                next(gen)
            except StopIteration as stop:
                self.assertFalse(stop.value)
                break
        self.assertIn((785, 468), device.clicks)
        self.assertNotIn((497, 468), device.clicks)
        self.assertEqual(device.state, "formation")

    @patch.object(ft.time, "sleep")
    def test_transfer_requires_owner_then_verifies_slot(self, _sleep):
        device = FakeMaa(equipped=True)
        agent = type("Agent", (), {"device": device})()
        gen = ft.equip_preset_treasure_stream(agent, 1, TARGET)
        while True:
            try:
                next(gen)
            except StopIteration as stop:
                self.assertTrue(stop.value)
                break
        self.assertIn((497, 468), device.clicks)
        self.assertEqual(device.slot, NAME)
        self.assertEqual(device.state, "formation")


if __name__ == "__main__":
    unittest.main()
