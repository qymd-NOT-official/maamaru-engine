# -*- coding: utf-8 -*-
"""内番「谁+1」识别：报告屏徽章 + 表屏数值快照，双保险（老大钦定）

侦察结论（2026-08-25 真机实验，用了一张内番符）：
  内番收结果时播放：内番结束横幅 → 结束对话 → 「内番报告」屏 → 今日内番表
  - 报告屏三栏六槽固定布局，涨属性的数值框底角冒蓝底白字「+1」徽章
  - 徽章按组蹦出来有先后（切磋组慢），看到标题后要等 ~2 秒再读
  - 属性喂满（金框）的那格不涨也不冒徽章 → 徽章会漏，所以配表屏数值快照做二次确认
  - 快照比对的盲区：打击/防御/冲力/机动也能被合成喂上去，diff 独占的 +1
    播报时如实标注来源，不装是内番加的

认错不如认不到：刀名走名册严格匹配，数值 OCR 读不出就空着跳过。
"""

import re

import numpy as np

from .. import sword_db
from ..device_adapter import roi_4to4

# ===== 「内番报告」屏布局（1280x720，真机 nk_06 量定）=====
REPORT_TITLE_ROI = (525, 318, 758, 362)  # 标题「内番报告」

# (组名, 左属性, 右属性, 数值框左缘, 数值框右缘, 中缝x)
REPORT_COLS = (
    ("饲马", "防御", "机动", 295, 420, 357),
    ("耕作", "生存", "侦察", 700, 830, 765),
    ("切磋", "打击", "冲力", 1065, 1205, 1135),
)
REPORT_ROWS = (("上", 485), ("下", 578))  # (行名, 数值框底边y)

# 报告屏一栏的 OCR 区域（罩住头像+名字+数值框）
REPORT_OCR_REGIONS = (
    (55, 360, 450, 610),
    (440, 360, 835, 610),
    (825, 360, 1220, 610),
)
REPORT_MIDS = (357, 765, 1135)      # 一栏数值框中缝 x，分左右属性用
REPORT_VALUES_SPLIT_Y = 505         # 上/下槽数值分界
REPORT_NAMES_SPLIT_Y = 530          # 上/下槽名字分界

# ===== 「今日内番表」屏布局（真机 nk_07 量定）=====
TABLE_OCR_REGIONS = (
    (55, 340, 460, 545),
    (430, 340, 880, 545),
    (885, 340, 1275, 545),
)
TABLE_MIDS = (400, 792, 1207)
TABLE_VALUES_SPLIT_Y = 450
TABLE_NAMES_SPLIT_Y = 478

# 表屏栏位属性（顺序同 REPORT_COLS）
TABLE_STATS = (("防御", "机动"), ("生存", "侦察"), ("打击", "冲力"))

_BADGE_MIN_BLUE = 150  # 锚点内徽章蓝像素下限（真机三中锚点 223~241，非徽章 <30）


def _badge_roi(col, bottom, side):
    """徽章锚点：钉在涨的那格数值框底角。右属性钉框右底角，左属性钉中缝底角
    （左侧锚点没有真机样本，按右锚镜像推的——漏报不误报，认了）"""
    ax = (col[5] - 45) if side == 0 else (col[4] - 57)
    return (ax, bottom - 13, ax + 50, bottom + 16)


def find_plus1_badges(img) -> set:
    """报告屏找 +1 徽章，返回 {(栏, 行, 左右)}。img 为 BGR ndarray。

    徽章蓝 B>200 且明显偏蓝（真机实测 BGR 约 (235,175,135)）；
    防御表头蓝 B-R 差偏小，且锚点位置压不到表头——但保险起见，
    本函数只应在确认标题后的报告屏上调用。
    """
    if img is None:
        return set()
    a = img.astype(np.int32)
    b, g, r = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    blue = (b > 200) & (b - r > 60) & (g > 120) & (g < 215)
    hits = set()
    for ci, col in enumerate(REPORT_COLS):
        for ri, (_rn, bot) in enumerate(REPORT_ROWS):
            for side in (0, 1):
                x1, y1, x2, y2 = _badge_roi(col, bot, side)
                if int(blue[y1:y2, x1:x2].sum()) > _BADGE_MIN_BLUE:
                    hits.add((ci, ri, side))
    return hits


def parse_column_tokens(tokens, mid_x, values_split_y, names_split_y):
    """一栏的 OCR 词元 → 上下两槽 [{name, sword_id, values:[左,右]}]。

    纯数字词元按位置归槽（y 分行、x 相对中缝分左右）；
    非数字词元走名册严格匹配认刀名。认不到就空着，不猜。
    """
    slots = [dict(name=None, sword_id=None, values=[None, None]),
             dict(name=None, sword_id=None, values=[None, None])]
    for text, pt in tokens:
        t = (text or "").strip()
        if not t:
            continue
        if re.fullmatch(r"\d{1,3}", t):
            row = 0 if pt.y < values_split_y else 1
            side = 0 if pt.x < mid_x else 1
            slots[row]["values"][side] = int(t)
            continue
        found = sword_db.find_by_name(t, fuzzy=False)
        if found:
            sid, info = found
            row = 0 if pt.y < names_split_y else 1
            slots[row]["name"] = info.get("name_zh") or info["name"]
            slots[row]["sword_id"] = sid
    return slots


def read_report_gains(img, ocr_func) -> list:
    """读报告屏：徽章定位谁+1，OCR 认名字补上「是谁」。

    ocr_func(Region) -> [(文本, Point)]，一般传 device.ocr_all。
    返回 [{"name", "stat", "slot"}]，名字认不到用「饲马上位」式兜底标签。
    """
    badges = find_plus1_badges(img)
    if not badges:
        return []
    names = {}
    for ci, region in enumerate(REPORT_OCR_REGIONS):
        try:
            tokens = ocr_func(roi_4to4(*region))
        except Exception:
            continue
        for ri, slot in enumerate(parse_column_tokens(
                tokens, REPORT_MIDS[ci],
                REPORT_VALUES_SPLIT_Y, REPORT_NAMES_SPLIT_Y)):
            if slot["name"]:
                names[(ci, ri)] = slot["name"]
    gains = []
    for ci, ri, side in sorted(badges):
        col = REPORT_COLS[ci]
        slot_label = f"{col[0]}{REPORT_ROWS[ri][0]}位"
        gains.append({
            "name": names.get((ci, ri)) or slot_label,
            "stat": col[1] if side == 0 else col[2],
            "slot": slot_label,
        })
    return gains


def read_table_stats(ocr_func) -> dict:
    """读今日内番表：{刀名: {属性: 值}}。读不全的槽整个跳过（不编数）。"""
    table = {}
    seen_names = set()
    duplicate_names = set()
    for ci, region in enumerate(TABLE_OCR_REGIONS):
        try:
            tokens = ocr_func(roi_4to4(*region))
        except Exception:
            continue
        for slot in parse_column_tokens(
                tokens, TABLE_MIDS[ci],
                TABLE_VALUES_SPLIT_Y, TABLE_NAMES_SPLIT_Y):
            if not slot["name"]:
                continue
            if slot["name"] in seen_names:
                duplicate_names.add(slot["name"])
            seen_names.add(slot["name"])
            stats = {}
            for side, label in enumerate(TABLE_STATS[ci]):
                v = slot["values"][side]
                if v is not None:
                    stats[label] = v
            if stats:
                if slot["name"] not in duplicate_names:
                    table[slot["name"]] = stats
    # 表屏只认刀名，没有实例编号；同名两振不能互相覆盖。
    for name in duplicate_names:
        table.pop(name, None)
    return table


def diff_snapshots(old: dict, new: dict) -> list:
    """新旧快照比对：谁在哪个属性涨了就报谁。
    只报纯涨（new>old）；换班/新面孔没有旧值，不报。
    注意打击/防御/冲力/机动也能被合成喂上去——调用方如实标注来源。"""
    gains = []
    for name, stats in (new or {}).items():
        prev = (old or {}).get(name) or {}
        for stat, val in stats.items():
            ov = prev.get(stat)
            if isinstance(ov, int) and isinstance(val, int) and val > ov:
                gains.append({"name": name, "stat": stat, "old": ov, "new": val})
    return gains
