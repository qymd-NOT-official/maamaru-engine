# -*- coding: utf-8 -*-
"""预设编队按刀装格换装。坐标取自 1280×720 MuMu 运行帧。"""

import time

from ..device_adapter import Point, roi_4to4
from ..roi_overrides import get_roi


_EQUIP_TITLE = get_roi("formation_treasure.title", (530, 0, 755, 58))
_TROOP_TITLE = get_roi("formation_troops.title", (845, 90, 1020, 130))
_LIST = get_roi("formation_troops.list", (850, 126, 1250, 690))
_SLOT_CY = (166, 266, 365)


def _has_text(device, expected, roi):
    device.screenshot(force=True)
    return bool(device.ocr(expected, roi_4to4(*roi)))


def _slot_tokens(device, position):
    cy = _SLOT_CY[position - 1]
    device.screenshot(force=True)
    return device.ocr_all(roi_4to4(315, cy - 42, 548, cy + 43)) or []


def find_troop_in_list(device, name):
    """逐页找完整名称；同名同品级任选一件，列表停滞才停止。"""
    previous = None
    for _ in range(20):
        device.screenshot(force=True)
        tokens = device.ocr_all(roi_4to4(*_LIST)) or []
        matches = [point for value, point in tokens
                   if str(value).strip() == name and 130 <= point.y <= 650]
        if matches:
            return min(matches, key=lambda point: point.y)
        signature = tuple((str(value).strip(), point.y // 5)
                          for value, point in tokens if 940 <= point.x <= 1240)
        if not signature or signature == previous:
            return None
        previous = signature
        device.swipe(1100, 590, 1100, 250, 600)
        time.sleep(0.4)
    return None


def equip_preset_troop_stream(agent, slot_no, position, name, team_no=None):
    """只装备精确名称；每一步失明都停在当前编队，绝不继续出发。"""
    from .team_roster import _ROW_CY

    from ..game_state import available_events, troop_matches
    device = agent.device
    if (not isinstance(slot_no, int) or not 1 <= slot_no <= 6
            or not isinstance(position, int) or not 1 <= position <= 3
            or not isinstance(name, str) or not name.strip()):
        yield "[刀装] 位置或刀装名称不认识，停"
        return False
    if not _has_text(device, "部队编成", (480, 0, 800, 60)):
        yield "[刀装] 不在部队编成页，停"
        return False
    device.click(Point(964, _ROW_CY[slot_no - 1]))
    time.sleep(0.6)
    if not _has_text(device, "更换装备", _EQUIP_TITLE):
        yield "[刀装] 装备页没打开，停"
        return False

    try:
        before = available_events(device) if team_no else []
        current = troop_matches(before, team_no, slot_no, position, name.strip()) if team_no else None
        if current is True or current is None and any(str(value).strip() == name.strip()
               for value, _ in _slot_tokens(device, position)):
            yield f"[刀装] {slot_no}号位第{position}格已经是「{name}」"
            return True
        device.click(Point(350, _SLOT_CY[position - 1]))
        time.sleep(0.4)
        if not _has_text(device, "刀装", _TROOP_TITLE):
            yield f"[刀装] {slot_no}号位没有打开第{position}格刀装列表，停"
            return False
        point = find_troop_in_list(device, name.strip())
        if point is None:
            yield f"[刀装] 没找到「{name}」，或列表没读全，停"
            return False
        device.click(point)
        time.sleep(0.3)
        device.screenshot(force=True)
        confirm = device.ocr("确定", roi_4to4(980, point.y + 8,
                                         1130, min(point.y + 55, 700)))
        if not confirm:
            yield "[刀装] 选中后没有读到确定，停"
            return False
        device.click(confirm)
        time.sleep(0.5)
        actual = troop_matches(available_events(device), team_no, slot_no, position,
                               name.strip(), previous=before) if team_no else None
        if actual is False or actual is None and not any(str(value).strip() == name.strip()
                   for value, _ in _slot_tokens(device, position)):
            yield "[刀装] 换装后没有读到目标刀装，停"
            return False
        yield f"[刀装] ✓ {slot_no}号位第{position}格已装备「{name}」"
        return True
    finally:
        if _has_text(device, "更换装备", _EQUIP_TITLE):
            device.click(Point(152, 26))
            time.sleep(0.4)
