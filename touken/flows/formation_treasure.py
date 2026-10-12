# -*- coding: utf-8 -*-
"""部队预设的宝物换装。坐标来自 2026-09-24 的 1280×720 MuMu 运行帧。

按真机换装列表逐页核对名称、等级和爱用度。相同指纹的多件宝物无法
区分实例，遇到时停止，不能用第一条可见卡片冒充玩家指定的实例。
"""

import re
import time

from ..maa_adapter import Point, roi_4to4
from ..roi_overrides import get_roi


_TITLE = get_roi("formation_treasure.title", (530, 0, 755, 58))
_PANEL_TITLE = get_roi("formation_treasure.panel_title", (845, 90, 1020, 130))
_COUNT = get_roi("formation_treasure.count", (850, 130, 1185, 170))
_CARD = get_roi("formation_treasure.card", (850, 174, 1250, 380))
_LIST = get_roi("formation_treasure.list", (850, 174, 1250, 688))
_SLOT = get_roi("formation_treasure.slot", (605, 330, 828, 399))
_TRANSFER = get_roi("formation_treasure.transfer", (440, 295, 845, 365))

_COUNT_RE = re.compile(r"未装备\s*/\s*总所持\s*(\d+)\s*个\s*/\s*(\d+)\s*个")
_OWNER_RE = re.compile(r"确认卸下(.+?)的装备吗")


def _tokens(maa, roi):
    maa.screenshot(force=True)
    return maa.ocr_all(roi_4to4(*roi)) or []


def _has_text(maa, expected, roi):
    maa.screenshot(force=True)
    return bool(maa.ocr(expected, roi_4to4(*roi)))


def parse_treasure_count(tokens):
    for value, _ in tokens:
        match = _COUNT_RE.search(str(value).replace(" ", ""))
        if match:
            free, total = map(int, match.groups())
            if 0 <= free <= total:
                return free, total
    return None


def match_single_treasure(tokens, target):
    """单候选真帧：名字、等级、爱用度都读到才认为目标可选择。"""
    name = target.get("name")
    level = target.get("level")
    affection = target.get("affection")
    if not isinstance(name, str) or not name.strip() or not isinstance(level, int):
        return None
    names = [(text, point) for text, point in tokens if str(text).strip() == name]
    if len(names) != 1:
        return None
    name_y = names[0][1].y
    level_found = any(re.fullmatch(rf"\s*{level}\s*级\s*", str(text))
                      and 55 <= point.y - name_y <= 95
                      for text, point in tokens)
    affection_found = any(re.search(rf"爱用度\s*{affection}(?!\d)", str(text))
                          and -45 <= point.y - name_y <= -10
                          for text, point in tokens)
    if not level_found or not affection_found:
        return None
    return names[0][1]


def visible_treasures(tokens):
    """读取完整卡片；滚动边缘露出的半张卡片不参与选择。"""
    cards = []
    for text, affection_point in tokens:
        match = re.fullmatch(r"\s*爱用度\s*(\d+)\s*", str(text))
        if not match or not 175 <= affection_point.y <= 575:
            continue
        names = [(str(value).strip(), point) for value, point in tokens
                 if 940 <= point.x <= 1140
                 and 20 <= point.y - affection_point.y <= 45
                 and not re.search(r"\d|爱用度|生存|打击|防御|机动|冲力|侦察|隐蔽|必杀", str(value))]
        if len(names) != 1:
            continue
        name, point = names[0]
        levels = [int(m.group(1)) for value, level_point in tokens
                  if (m := re.fullmatch(r"\s*(\d+)\s*级\s*", str(value)))
                  and 55 <= level_point.y - point.y <= 95]
        if len(levels) != 1:
            continue
        occupied = any("装备中" in str(value) and abs(badge_point.y - affection_point.y) <= 25
                       for value, badge_point in tokens)
        cards.append((name, levels[0], int(match.group(1)), occupied, point))
    return cards


def find_treasure_in_list(maa, target, total):
    """从列表顶端扫描；只在恰好读全 total 件且目标指纹唯一时返回位置。"""
    seen = {}
    target_page = None
    for page in range(12):
        cards = visible_treasures(_tokens(maa, _LIST))
        for name, level, affection, occupied, point in cards:
            key = (name, level, affection)
            if key in seen and seen[key][0] != page - 1:
                return None
            seen[key] = (page, occupied)
            if key == (target["name"], target["level"], target["affection"]):
                target_page = page
        if len(seen) == total:
            break
        if len(seen) > total or page == 11:
            return None
        maa.swipe(1100, 595, 1100, 245, 650)
        time.sleep(0.4)
    else:
        return None
    if target_page is None:
        return None
    for _ in range(page - target_page):
        maa.swipe(1100, 245, 1100, 595, 650)
        time.sleep(0.4)
    matches = [(occupied, point) for name, level, affection, occupied, point
               in visible_treasures(_tokens(maa, _LIST))
               if (name, level, affection) ==
               (target["name"], target["level"], target["affection"])]
    return matches[0] if len(matches) == 1 else None


def transfer_owner(tokens):
    for value, _ in tokens:
        match = _OWNER_RE.search(str(value).replace(" ", ""))
        if match:
            return match.group(1)
    return None


def equip_preset_treasure_stream(agent, slot_no, target, team_no=None):
    """在已确认的编队页给一个槽位换宝物；失败时绝不点未知卡片。"""
    from ..game_state import available_events, equipped_asset
    maa = agent.maa
    from .team_roster import _ROW_CY

    if not isinstance(slot_no, int) or not 1 <= slot_no <= 6:
        yield "[宝物] 位置不认识，停"
        return False
    if not _has_text(maa, "部队编成", (480, 0, 800, 60)):
        yield "[宝物] 不在部队编成页，停"
        return False

    maa.click(Point(964, _ROW_CY[slot_no - 1]))
    time.sleep(0.7)
    if not _has_text(maa, "更换装备", _TITLE):
        yield "[宝物] 装备页没打开，停"
        return False

    try:
        before = available_events(maa) if team_no else []
        current = equipped_asset(before, team_no, slot_no, "treasure") if team_no else None
        if current is not None and all(current.get(field) == target.get(field) for field in ("name", "level", "affection")):
            yield f"[宝物] {slot_no}号位已经装着指定的「{target['name']}」"
            return True
        maa.click(Point(634, 365))
        time.sleep(0.4)
        if not _has_text(maa, "宝物", _PANEL_TITLE):
            yield "[宝物] 宝物列表没打开，停"
            return False
        count = parse_treasure_count(_tokens(maa, _COUNT))
        if not count or count[1] < 1:
            yield "[宝物] 所持数量没有读清，停"
            return False
        if count[1] == 1:
            tokens = _tokens(maa, _CARD)
            point = match_single_treasure(tokens, target)
            equipped_elsewhere = any("装备中" in str(value) for value, _ in tokens)
        else:
            found = find_treasure_in_list(maa, target, count[1])
            point = found[1] if found else None
            equipped_elsewhere = found[0] if found else False
        if point is None:
            yield "[宝物] 没有读全列表，或目标的名称、等级、爱用度不唯一，停"
            return False
        if count[1] == 1 and equipped_elsewhere != (count[0] == 0):
            yield "[宝物] 所持计数与装备状态矛盾，停"
            return False

        maa.click(point)
        time.sleep(0.35)
        maa.screenshot(force=True)
        confirm = maa.ocr("确定", roi_4to4(960, point.y + 35, 1140, min(point.y + 90, 705)))
        if not confirm:
            yield "[宝物] 候选卡片没有出现确认按钮，停"
            return False
        maa.click(confirm)
        time.sleep(0.5)
        owner = transfer_owner(_tokens(maa, _TRANSFER))
        if equipped_elsewhere:
            if not owner:
                yield "[宝物] 装备中宝物的原持有者没有读清，停止确认"
                return False
            maa.click(Point(497, 468))
            time.sleep(0.55)
            yield f"[宝物] 已确认从「{owner}」转交宝物"
        elif owner:
            yield "[宝物] 未装备宝物却出现转交弹窗，停止确认"
            return False
        actual = equipped_asset(available_events(maa), team_no, slot_no, "treasure", previous=before) if team_no else None
        details_wrong = actual is not None and (actual.get("empty") or any(actual.get(field) != target.get(field) for field in ("level", "affection"))
                         or actual.get("name") is not None and actual["name"] != target["name"])
        # Unknown names retain the screen confirmation after selecting the full target fingerprint.
        named = actual is not None and actual.get("name") == target["name"]
        if details_wrong or not named and not any(str(value).strip() == target["name"]
                   for value, _ in _tokens(maa, _SLOT)):
            yield "[宝物] 换装后槽位未读到目标宝物，停"
            return False
        yield f"[宝物] ✓ {slot_no}号位已装备「{target['name']}」"
        return True
    finally:
        # 装备中途失明或原持有者不明时，可能还开着转交弹窗：先点否。
        if _has_text(maa, "否", (680, 430, 900, 510)):
            maa.click(Point(785, 468))
            time.sleep(0.3)
        if _has_text(maa, "更换装备", _TITLE):
            maa.click(Point(152, 26))
            time.sleep(0.5)
