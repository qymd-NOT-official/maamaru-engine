# -*- coding: utf-8 -*-
"""马与御守列表的选择规则；不碰真机。"""

import unittest
from unittest.mock import patch

from touken.device_adapter import Point
from touken.flows import formation_accessory as accessory


class _ListMaa:
    def __init__(self, pages):
        self.pages = pages
        self.index = 0

    def screenshot(self, force=False):
        return None

    def ocr_all(self, _roi):
        return self.pages[self.index]

    def swipe(self, *_args):
        self.index = min(self.index + 1, len(self.pages) - 1)


def _card(name, y, occupied=False):
    result = [(name, Point(990, y))]
    if occupied:
        result.append(("装备中", Point(1190, y)))
    return result


class HorseListTests(unittest.TestCase):
    def test_find_free_named_horse_on_next_page(self):
        device = _ListMaa([_card("01王庭x1", 188), _card("08望月x1", 188)])
        with patch.object(accessory.time, "sleep"):
            found = accessory.find_horse_in_list(device, "08望月")
        self.assertEqual(found, (Point(990, 188), False))

    def test_numbered_occupied_horse_requires_transfer(self):
        device = _ListMaa([_card("108望月x1", 188, occupied=True)])
        self.assertEqual(accessory.find_horse_in_list(device, "08望月"),
                         (Point(990, 188), True))

    def test_ninth_anniversary_horse_requires_transfer(self):
        device = _ListMaa([_card("祝九号x1", 188, occupied=True)])
        self.assertEqual(accessory.find_horse_in_list(device, "祝九号"),
                         (Point(990, 188), True))

    def test_generic_occupied_horse_is_not_chosen(self):
        device = _ListMaa([_card("白毛x1", 188, occupied=True)])
        with patch.object(accessory.time, "sleep"):
            self.assertIsNone(accessory.find_horse_in_list(device, "白毛"))

    def test_generic_free_horse_is_chosen(self):
        device = _ListMaa([_card("白毛x5", 188)])
        self.assertEqual(accessory.find_horse_in_list(device, "白毛"),
                         (Point(990, 188), False))


class CharmListTests(unittest.TestCase):
    def test_charm_requires_one_free_exact_name(self):
        device = _ListMaa([_card("御守", 145), _card("御守·极", 244)])
        self.assertEqual(accessory.find_charm_in_list(device, "御守"),
                         (Point(990, 145), False))
        self.assertIsNone(accessory.find_charm_in_list(device, "御守·桃"))
        device = _ListMaa([_card("御守", 145, occupied=True)])
        self.assertIsNone(accessory.find_charm_in_list(device, "御守"))


if __name__ == "__main__":
    unittest.main()
