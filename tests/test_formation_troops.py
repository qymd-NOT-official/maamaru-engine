"""预设刀装按格选择的查找与失败保护。"""

import unittest
from unittest.mock import patch

from touken.device_adapter import Point
from touken.flows import formation_troops as ft


NAME = "轻步兵·新春"


class FakeMaa:
    def __init__(self, *, item=True, slot=True):
        self.state = "formation"
        self.item = item
        self.slot_available = slot
        self.slot_name = None
        self.clicks = []

    def screenshot(self, force=False):
        return object()

    def ocr(self, expected, _roi):
        found = ((expected == "部队编成" and self.state == "formation")
                 or (expected == "更换装备" and self.state in
                     ("equip", "list", "preview"))
                 or (expected == "刀装" and self.state == "list")
                 or (expected == "确定" and self.state == "preview"))
        return Point(1051, 177) if found and expected == "确定" else (
            Point(640, 27) if found else None)

    def ocr_all(self, roi):
        if roi.x == 315:
            return [(self.slot_name or "空", Point(455, 178))]
        if roi.x == 850 and self.state in ("list", "preview"):
            return [(NAME if self.item else "盾兵·特上", Point(1023, 144))]
        return []

    def click(self, point):
        xy = (point.x, point.y)
        self.clicks.append(xy)
        if self.state == "formation" and xy == (964, 160):
            self.state = "equip"
        elif self.state == "equip" and xy == (350, 166) and self.slot_available:
            self.state = "list"
        elif self.state == "list" and xy == (1023, 144):
            self.state = "preview"
        elif self.state == "preview" and xy == (1051, 177):
            self.slot_name = NAME
            self.state = "equip"
        elif xy == (152, 26):
            self.state = "formation"

    def swipe(self, *_args):
        pass


def run(device):
    agent = type("Agent", (), {"device": device})()
    gen = ft.equip_preset_troop_stream(agent, 1, 1, NAME)
    messages = []
    while True:
        try:
            messages.append(next(gen))
        except StopIteration as stop:
            return stop.value, messages


class TroopTests(unittest.TestCase):
    @patch.object(ft.time, "sleep")
    def test_exact_name_equips_and_verifies_slot(self, _sleep):
        device = FakeMaa()
        ok, _messages = run(device)
        self.assertTrue(ok)
        self.assertEqual(device.slot_name, NAME)
        self.assertEqual(device.state, "formation")

    @patch.object(ft.time, "sleep")
    def test_unknown_name_never_confirms(self, _sleep):
        device = FakeMaa(item=False)
        ok, _messages = run(device)
        self.assertFalse(ok)
        self.assertNotIn((1051, 177), device.clicks)
        self.assertEqual(device.state, "formation")

    @patch.object(ft.time, "sleep")
    def test_missing_slot_stops(self, _sleep):
        device = FakeMaa(slot=False)
        ok, _messages = run(device)
        self.assertFalse(ok)
        self.assertNotIn((1023, 144), device.clicks)
        self.assertEqual(device.state, "formation")


if __name__ == "__main__":
    unittest.main()
