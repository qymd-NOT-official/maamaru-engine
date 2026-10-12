# -*- coding: utf-8 -*-
"""预设编队的马与御守换装；只在已确认的编队页执行。"""

import re
import time

from ..maa_adapter import Point, roi_4to4
from ..roi_overrides import get_roi


_EQUIP_TITLE = get_roi("formation_treasure.title", (530, 0, 755, 58))
_HORSE_TITLE = get_roi("formation_accessory.horse_title", (845, 90, 1020, 130))
_CHARM_TITLE = get_roi("formation_accessory.charm_title", (845, 90, 1020, 130))
_HORSE_LIST = get_roi("formation_accessory.horse_list", (850, 174, 1250, 690))
_CHARM_LIST = get_roi("formation_accessory.charm_list", (850, 126, 1250, 330))
_TRANSFER = get_roi("formation_treasure.transfer", (440, 295, 845, 365))
_HORSE_SLOT = (605, 125, 830, 210)
_CHARM_SLOT = (605, 225, 830, 310)


def _tokens(maa, roi):
    maa.screenshot(force=True)
    return maa.ocr_all(roi_4to4(*roi)) or []


def _has_text(maa, expected, roi):
    maa.screenshot(force=True)
    return bool(maa.ocr(expected, roi_4to4(*roi)))


def _horse_name(text):
    return re.sub(r"[x×]\s*\d+$", "", str(text).replace(" ", "").strip())


def _horse_matches(tokens, name):
    matches = []
    for value, point in tokens:
        if not 940 <= point.x <= 1090 or not 175 <= point.y <= 660:
            continue
        # 运行帧上有时把序号前的边框认作「1」：108望月仍指 08望月。
        if not _horse_name(value).endswith(name):
            continue
        occupied = any("装备中" in str(badge)
                       and abs(badge_point.y - point.y) <= 25
                       for badge, badge_point in tokens)
        matches.append((point, occupied))
    return matches


def find_horse_in_list(maa, name):
    """同名马优先选未装备的一匹；已装备的只接受有序号的唯一马。"""
    previous = None
    uniquely_named = bool(re.match(r"^\d{2}", name)
                          or re.match(r"^祝[一二三四五六七八九]号$", name))
    for _ in range(12):
        tokens = _tokens(maa, _HORSE_LIST)
        matches = _horse_matches(tokens, name)
        free = [point for point, occupied in matches if not occupied]
        if free:
            return min(free, key=lambda point: point.y), False
        occupied = [point for point, taken in matches if taken]
        if uniquely_named and len(occupied) == 1:
            return occupied[0], True
        if uniquely_named and len(occupied) > 1:
            return None
        signature = tuple((str(value).strip(), point.y // 5)
                          for value, point in tokens if 940 <= point.x <= 1240)
        if not signature or signature == previous:
            return None
        previous = signature
        maa.swipe(1100, 600, 1100, 210, 650)
        time.sleep(0.4)
    return None


def find_charm_in_list(maa, name):
    tokens = _tokens(maa, _CHARM_LIST)
    matches = [(point, any("装备中" in str(badge)
                           and abs(badge_point.y - point.y) <= 25
                           for badge, badge_point in tokens))
               for value, point in tokens if str(value).strip() == name]
    free = [match for match in matches if not match[1]]
    return min(free, key=lambda match: match[0].y) if free else None


def _transfer_owner(maa):
    from .formation_treasure import transfer_owner
    return transfer_owner(_tokens(maa, _TRANSFER))


def equip_preset_accessory_stream(agent, slot_no, kind, name, team_no=None):
    """kind 为 horse/charm；失败时不继续预设。"""
    from .team_roster import _ROW_CY

    from ..game_state import available_events, equipped_asset
    maa = agent.maa
    if (not isinstance(slot_no, int) or not 1 <= slot_no <= 6
            or kind not in ("horse", "charm")
            or not isinstance(name, str) or not name.strip()):
        yield "[装备] 马或御守的指定不认识，停"
        return False
    label = "马" if kind == "horse" else "御守"
    slot = _HORSE_SLOT if kind == "horse" else _CHARM_SLOT
    entry_y = 166 if kind == "horse" else 266
    title_roi = _HORSE_TITLE if kind == "horse" else _CHARM_TITLE
    if not _has_text(maa, "部队编成", (480, 0, 800, 60)):
        yield f"[{label}] 不在部队编成页，停"
        return False
    maa.click(Point(964, _ROW_CY[slot_no - 1]))
    time.sleep(0.6)
    if not _has_text(maa, "更换装备", _EQUIP_TITLE):
        yield f"[{label}] 装备页没打开，停"
        return False
    try:
        before = available_events(maa) if team_no else []
        asset = equipped_asset(before, team_no, slot_no, kind) if team_no else None
        current = _tokens(maa, slot) if asset is None else []
        if asset is not None and asset.get("name") == name or asset is None and any(((_horse_name(value).endswith(name) if kind == "horse"
                 else str(value).strip() == name)) for value, _ in current):
            yield f"[{label}] {slot_no}号位已经是「{name}」"
            return True
        maa.click(Point(635, entry_y))
        time.sleep(0.4)
        if not _has_text(maa, label, title_roi):
            yield f"[{label}] 列表没打开，停"
            return False
        found = (find_horse_in_list(maa, name) if kind == "horse"
                 else find_charm_in_list(maa, name))
        if not found:
            yield f"[{label}] 没找到可确认的「{name}」，停"
            return False
        point, occupied = found
        maa.click(point)
        time.sleep(0.3)
        maa.screenshot(force=True)
        confirm = maa.ocr("确定", roi_4to4(980, point.y + 8,
                                         1130, min(point.y + 55, 700)))
        if not confirm:
            yield f"[{label}] 没读到换装确认，停"
            return False
        maa.click(confirm)
        time.sleep(0.5)
        owner = _transfer_owner(maa)
        if occupied:
            if not owner:
                yield f"[{label}] 原持有者没有读清，停止转交"
                return False
            maa.click(Point(497, 468))
            time.sleep(0.5)
            yield f"[{label}] 已确认从「{owner}」转交"
        elif owner:
            yield f"[{label}] 未装备的候选却出现转交弹窗，停"
            return False
        actual = equipped_asset(available_events(maa), team_no, slot_no, kind, previous=before) if team_no else None
        if (actual is not None and actual.get("name") != name) or (actual is None and not any(((_horse_name(value).endswith(name) if kind == "horse"
                     else str(value).strip() == name))
                   for value, _ in _tokens(maa, slot))):
            yield f"[{label}] 换装后没有读到「{name}」，停"
            return False
        yield f"[{label}] ✓ {slot_no}号位已装备「{name}」"
        return True
    finally:
        if _has_text(maa, "否", (680, 430, 900, 510)):
            maa.click(Point(785, 468))
            time.sleep(0.3)
        if _has_text(maa, "更换装备", _EQUIP_TITLE):
            maa.click(Point(152, 26))
            time.sleep(0.4)
