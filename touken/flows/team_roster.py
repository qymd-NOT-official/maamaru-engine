# -*- coding: utf-8 -*-
"""编队页只读点名：走进部队编成页，逐部队逐槽读当前队伍事实。

issue #7「智能编队」第 1 步工单（牛老师终版口径 + 返工单）：
  - 只读：导航 + 切部队标签；禁止成员卡、替换、装备等危险点击，禁止 swipe/drag；
  - JSON 数据优先：身份/等级/生存/疲劳/伤势先取 sword_facts.parties
    等游戏事实（FACT_MAX_AGE=600s，逐字段校验新鲜度与战后时效，
    半残数据不放行），数据缺失/过期才降级本字段视觉链（OCR/伤势章）；
    行定位（切队标签锚定、卡面空位正面证据、极化徽章/白樱花）保留视觉；
  - 切队正面确认：点标签后读行首"N之M"位置标签核对队号，确认不了该队
    observation_status=failed、slots=[]，绝不张冠李戴；
  - 伤势分类比分数选最佳（最佳+区分度+同源已证明类别），不吃配置顺序抢答；
  - 六槽固定输出，slot_status=occupied/empty/unknown；任一字段失败只降级本字段；
  - 事件 team_roster.observed（ts 由 telemetry 补，payload 不带 observed_at），
    顶层 observation_status=complete/partial/failed；
  - 空位必须有正面证据：卡面近纯白（真机 30 槽校准）才算空槽形态，
    整页失明或证据不足 → unknown，绝不静默判空；
  - 伤势：章命中 → heavy/medium/light（配置中文键统一映射成英文枚举）；
    章未命中必须用生存 x/x 正面证明满血才判 none，否则 unknown——
    "章没匹配到"绝不是"无伤"；
  - 名字：本链路专用匹配（精确命中或唯一包含候选），禁用模糊兜底；
  - 极化=分层单向证明：白樱花同源模板可靠命中 → kiwame；花数证据只在
    确认刀种（名册刀种，其次徽章字符）下匹配且分数达标才成立，
    全局最高分模板是别的刀种 → 刀种冲突，花数证据作废记 unknown；
    髭切/膝丸动态涨花例外 → ambiguous；低分 → unknown；"没匹配到"绝不反推 normal；
  - 碰瓷语义：太刀+极化+中伤三项都确认 → tactical_roles 记碰瓷候选；
  - kiwame_date 实际是显现日期（获得日期），本流程禁止用于极化判断；
  - 属性上限 wiki 数据本版不抓，stat_exceeds_normal_cap 留给后续补强票。

页面坐标（1280x720，与 sakura.py 编队页同布局，真机校准）：
  部队标签一~五 (154/274/394/516/638, 91)；
  六行行心 y [160, 258, 357, 455, 553, 652]；
  每槽卡面：花形徽章（左上，刀种字符+花瓣数）、名字（徽章下方）、
  刀剑/乱舞/生存/疲劳 标签值堆（x≈288-430；相对行心 -36/-5/+17/+37）、
  伤势章（卡面右上）、白樱花（极化标记，卡面右上角、会被伤势章遮挡）。

模板来源说明：
  - 编队极化樱花.png：编队页运行帧（Maamaru-Dev/debug/research/tap_638_91.png
    部队五帧 (255,113)）同源裁剪，52×52；
  - 刀种/ 花数模板：仓库既有版本资源（e739867 随版本发布入库），
    非本链路同源帧重裁；可用性经真机帧校准（见 tests 真帧校准用例）。
"""

import re
import time
from pathlib import Path

from .. import sword_db
from ..formation_identity import GROWTH_STATS, growth_stats_match
from ..maa_adapter import roi_4to4, Point
from ..roi_overrides import get_roi
from ..roi_registry import FORMATION_ROW_DEFAULTS

_TEAM_TAB = {1: (154, 91), 2: (274, 91), 3: (394, 91), 4: (516, 91), 5: (638, 91)}
_ROW_CY = [160, 258, 357, 455, 553, 652]
_STAT_NAMES = ("生存", "打击", "防御", "机动", "冲力", "侦察", "隐蔽", "必杀")
ROW_CELL_ROIS = {
    slot: {field: get_roi(f"team_roster.row{slot}.{field}", rect)
           for field, rect in cells.items()}
    for slot, cells in FORMATION_ROW_DEFAULTS.items()
}

# 每槽读取 ROI（相对行心 cy 的偏移，1280x720）
_NAME_ROI = (68, 5, 270, 38)        # 名字（标签右侧空隙 64~76，首字不掐头）
_LEVEL_ROI = (348, -42, 412, -18)   # "刀剑 n 级" 数值（避开标签与右侧生存面板）
_SURVIVAL_ROI = (288, 6, 420, 28)   # "生存 x/x"（真机帧行带 +11..+24）
_FATIGUE_ROI = (290, 28, 425, 52)   # "疲劳 n/100"
_BADGE_ROI = (62, -50, 140, 20)     # 左上花形徽章（避开槽位标签；刀种字符+花瓣）
_INJURY_ROI = (165, -50, 315, 15)   # 卡面右上伤势章
_LABEL_ROI = (20, -48, 72, 46)      # 行首位置标签（兼容一队金色高亮与灰色标签）
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
_TEAM_NAME = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}
_SAKURA_ROI = (240, -50, 330, 15)   # 卡面右上白樱花（极化标记）

# 卡面占用判据区域（立绘位置）：真机 30 槽校准（2026-09-13，Maamaru-Dev
# debug/research 五帧）——占位 std≥51.1 / dark≥0.075，空位 std≤9.6 / dark=0
# 且亮度≥252；阈值取中间并留余量
_CARD_REGION = (150, -45, 290, 30)
_CARD_BLANK_MEAN = 230.0
_CARD_BLANK_STD = 25.0
_CARD_BLANK_DARK = 0.02

_FATIGUE_PAIR = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")

# 伤势配置中文键 → 事件英文稳定枚举（配置键以 touken_config 为准）
_INJURY_ENUM = {"重伤": "heavy", "中伤": "medium", "轻伤": "light"}

# 刀种规范化：名册/游戏 UI 用日文汉字，事件统一简体枚举
_TYPE_NORMALIZE = {"槍": "枪", "剣": "剑", "脇差": "胁差"}

# 徽章字符 → 刀种枚举（游戏 UI 章为日文汉字，兼容简体）
_BADGE_TYPE_OF = {"短": "短刀", "打": "打刀", "太": "太刀", "大": "大太刀",
                  "枪": "枪", "槍": "枪", "薙": "薙刀", "剑": "剑", "剣": "剑",
                  "胁": "胁差", "脇": "胁差"}

# 徽章读取置信阈值：真帧校准（占位槽 0.72~0.98 / 空位 ≤0.54 / 噪声 0.66）
_BADGE_THRESHOLD = 0.7

# 编队页伤势章阈值：章面在此页持续脉动，真章单帧匹配分实测 0.67~0.92
# （Maamaru-Dev/debug/research 探针 2026-09-13），横跨出阵页的 0.88 阈值；
# 伪章（重伤模板蹭中伤卡等）实测 ≤0.65、battle 页记录 0.704，0.75 取中间。
_ROSTER_STAMP_THRESHOLD = 0.75
# 最佳类别与次高分的最小区分度：动画章可能互相蹭分，拉不开就 unknown
_ROSTER_STAMP_MARGIN = 0.08
# 编队页同源已证明的伤势类别（事件枚举空间）：五帧 30 槽实测只有中伤样本；
# 重伤/轻伤等采到编队页运行帧后再解禁，出阵页模板不得降阈值硬判
_FORMATION_PROVEN_INJURIES = frozenset({"medium"})
_SAKURA_THRESHOLD = 0.9
_SAKURA_TEMPLATE = "编队极化樱花.png"

_FLOWER_OF = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}

# 花数证据白名单：只有编队页真机运行帧里实测达标（确认刀种下过滤匹配
# ≥阈值）的刀种×花数组合才可产出 flowers 证据；其余组合一律 unknown，
# 采到对应运行帧再逐步解禁——旧模板非编队页同源，不硬凑 21 张全开。
# 依据 2026-09-13 五帧 30 槽实测（tests 真帧校准用例守着这份清单）。
_PROVEN_FLOWER_COMBOS = frozenset({
    ("短刀", 2),   # T2 小夜左文字（极化短）
    ("打刀", 2),   # T1 压切长谷部（普打）
    ("打刀", 3),   # T3 加州清光、T5 丰前江/山姥切长义（极化打）
    ("胁差", 3),   # T3 物吉贞宗（极化胁）
    ("太刀", 4),   # T4 烛台切光忠（极太）
    ("太刀", 5),   # T4 莺丸（极太五花）、T3 道誉一文字（普太五花）
    ("枪", 3),     # T5 人间无骨（普枪）
})

# 普通形态会动态涨花的例外刀：花数>名册基线不能证明极化
# （髭切/膝丸随特阶段涨花，名册静态 rarity 都是 2）
_DYNAMIC_FLOWER_EXCEPTIONS = frozenset({
    "touken_107_higekiri", "touken_112_hizamaru"})


def _norm_type(type_name: str):
    """刀种枚举规范化：槍→枪、剣→剑、脇差→胁差，其余原样。"""
    if not type_name:
        return None
    return _TYPE_NORMALIZE.get(type_name, type_name)


# 公开别名：一览盘点（sword_inventory）等同包模块复用
norm_sword_type = _norm_type

_FLOWER_TEMPLATE_CACHE = {}


def load_flower_templates(resource_dir):
    """花数徽章模板 [(路径, 花数, 刀种枚举)]，按资源目录进程内缓存。

    模板来自仓库既有版本资源（文件名自带刀种，如 五花太刀.png），
    刀种统一规范化后供"确认刀种过滤"用。
    """
    key = str(resource_dir)
    cached = _FLOWER_TEMPLATE_CACHE.get(key)
    if cached is not None:
        return cached
    folder = Path(resource_dir) / "image" / "刀种"
    templates = []
    for path in sorted(folder.glob("*.png")):
        m = re.match(r"([一二三四五六])花(.+)", path.stem)
        if m:
            templates.append((path, _FLOWER_OF[m.group(1)],
                              _norm_type(m.group(2))))
    _FLOWER_TEMPLATE_CACHE[key] = templates
    return templates


def match_badge_flowers(region, templates, confirmed_type, proven_combos):
    """徽章花数匹配核心（纯 cv2，编队页/一览共用同一套判定）。

    region: 已裁好的徽章区域 BGR 图（None/空图 → low_score）；
    confirmed_type: 确认刀种（名册优先，其次徽章字符），None 表示没有
    确认刀种 → 不产生花数证据（no_confirmed_type），只留全局诊断分数；
    proven_combos: 调用方所在页面的同源真帧校准白名单 (刀种, 花数)，
    页面各自校准，不得跨页挪用。
    返回 badge dict：flowers/score/threshold/top_type/top_score/conclusion。
    """
    import cv2
    import numpy as np
    badge = {"flowers": None, "score": None, "threshold": _BADGE_THRESHOLD,
             "top_type": None, "top_score": None, "conclusion": "low_score"}
    if region is None or region.size == 0 or not templates:
        return badge
    best = {"flowers": None, "score": 0.0, "type": None}
    top = {"flowers": None, "score": 0.0, "type": None}
    for path, flowers, tpl_type in templates:
        raw = np.fromfile(str(path), dtype=np.uint8)
        tpl = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if tpl is None:
            continue
        for scale in (0.9, 1.0, 1.1, 1.2, 1.3, 1.4):
            t = cv2.resize(tpl, None, fx=scale, fy=scale)
            if t.shape[0] > region.shape[0] or t.shape[1] > region.shape[1]:
                continue
            res = cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED)
            _mn, mx, _mnl, _mxl = cv2.minMaxLoc(res)
            if mx > top["score"]:
                top.update(flowers=flowers, score=mx, type=tpl_type)
            if (confirmed_type is None or tpl_type == confirmed_type) \
                    and mx > best["score"]:
                best.update(flowers=flowers, score=mx, type=tpl_type)
    badge["top_type"] = top["type"]
    badge["top_score"] = round(top["score"], 3)
    # 原始观测（确认刀种过滤后的最佳花数/分数）不受阈值与白名单门禁影响，
    # 始终落盘供审计与跨页校准；只有 flowers/score 是出过门禁的证据
    badge["observed_flowers"] = best["flowers"] if best["type"] else None
    badge["observed_score"] = round(best["score"], 3) if best["type"] else None
    if confirmed_type is None:
        badge["conclusion"] = "no_confirmed_type"
        return badge
    if top["type"] is not None and top["type"] != confirmed_type \
            and top["score"] >= _BADGE_THRESHOLD \
            and top["score"] > best["score"]:
        # 全局最高分模板是另一个刀种：刀种冲突，花数证据作废
        badge.update(flowers=None, score=round(top["score"], 3),
                     conclusion="type_conflict")
        return badge
    if best["type"] == confirmed_type and best["score"] >= _BADGE_THRESHOLD:
        if (confirmed_type, best["flowers"]) not in proven_combos:
            # 该刀种×花数组合在本页面没有同源真帧达标记录：不出证据
            badge["score"] = round(best["score"], 3)
            badge["conclusion"] = "unproven_combo"
            return badge
        badge.update(flowers=best["flowers"], score=round(best["score"], 3),
                     conclusion="recognized")
    else:
        badge["score"] = round(best["score"], 3)
        badge["conclusion"] = "low_score"
    return badge


def conclude_kiwame(sid, rarity_base, badge, sakura_hit=False):
    """极化结论：证据数组 + 单向结论，冲突 → unknown（编队页/一览共用）。

    Returns:
        (kiwame_status ∈ kiwame/normal/unknown, kiwame_evidence 数组)
        数组每项 {type, raw_value, score, threshold, conclusion}，
        保留每条证据的实测原值供以后审计。
    """
    evidence = []
    if badge["conclusion"] == "type_conflict":
        # 刀种冲突：花数证据不可参与极化结论
        evidence.append({"type": "flower_type_conflict",
                         "raw_value": f"冲突刀种={badge['top_type']}",
                         "score": badge["score"],
                         "threshold": _BADGE_THRESHOLD,
                         "conclusion": "unknown"})
    elif rarity_base is not None and badge["flowers"] is not None:
        # 徽章证据：确认刀种下的花数 vs 名册基线
        if sid in _DYNAMIC_FLOWER_EXCEPTIONS:
            # 髭切/膝丸普通形态随特阶段涨花：徽章花数不区分极化
            conclusion = "ambiguous_dynamic_flowers"
        elif badge["flowers"] > rarity_base:
            conclusion = "kiwame"
        elif badge["flowers"] == rarity_base:
            # normal 需正面、无遮挡且达标的徽章证据，不接受"没匹配到"
            conclusion = "normal"
        else:
            conclusion = "flowers_below_base"
        evidence.append({"type": "badge_flowers_vs_base",
                         "raw_value": f"花数{badge['flowers']}/基线{rarity_base}",
                         "score": badge["score"],
                         "threshold": badge["threshold"],
                         "conclusion": conclusion})
    if sakura_hit:
        # 白樱花（极化标记）可靠命中：直接证明 kiwame；
        # 未命中不产生证据（可能被章遮挡或可见性波动，不反推 normal）
        evidence.append({"type": "sakura_template",
                         "raw_value": "白樱花",
                         "score": None,
                         "threshold": _SAKURA_THRESHOLD,
                         "conclusion": "kiwame"})

    # 单向结论：白樱花命中优先；徽章花数差其次；冲突/不足 → unknown
    conclusions = {e["conclusion"] for e in evidence}
    if "kiwame" in conclusions and "normal" in conclusions:
        return "unknown", evidence  # 白樱花与徽章花数冲突
    if "kiwame" in conclusions:
        return "kiwame", evidence
    if "normal" in conclusions:
        return "normal", evidence
    return "unknown", evidence


def _parse_fatigue_tokens(tokens, y_min=None, y_max=None):
    """疲劳 = 疲劳行带（按 token 实际 y）内分母恰为 100 的配对。

    双保险：只认落在行带 [y_min, y_max] 里的 token（生存行的
    100/100 哪怕分母恰为 100 也进不了疲劳行带），行带内再按 y 取
    最靠下的——完全不依赖 OCR 返回顺序。
    """
    best = None
    for text, pt in tokens or []:
        y = getattr(pt, "y", 0) or 0
        if y_min is not None and y < y_min:
            continue
        if y_max is not None and y > y_max:
            continue
        for cur, mx in _FATIGUE_PAIR.findall(text or ""):
            if int(mx) == 100 and (best is None or y >= best[0]):
                best = (y, int(cur))
    return best[1] if best else None


def _parse_survival_tokens(tokens):
    """生存 = y 位置最靠下的 x/x 配对 → (survival, survival_max)。

    生存行在疲劳行上方一行，ROI 收窄后互不越界；按坐标取数不吃
    OCR 返回顺序。当前值>上限视为读坏（OCR 粘连），不进事实。
    """
    best = None
    for text, pt in tokens or []:
        y = getattr(pt, "y", 0) or 0
        for cur, mx in _FATIGUE_PAIR.findall(text or ""):
            cur, mx = int(cur), int(mx)
            if cur <= mx and (best is None or y >= best[0]):
                best = (y, cur, mx)
    return (best[1], best[2]) if best else (None, None)


def _parse_level(text: str):
    m = re.search(r"(\d{1,3})", text or "")
    return int(m.group()) if m else None


def _match_name(raw: str):
    """编队点名专用认人：精确命中，或唯一包含候选；多候选一律拒认。

    刻意不用 find_by_name(fuzzy=True)——它的包含匹配按字典序取第一个，
    "藤四郎"这类泛称会错认成第一个同名后缀刀；点名认错比认不出更糟。
    Returns: sword 目录 id 或 None（unrecognized）
    """
    target = sword_db._normalize(raw or "")
    if len(target) < 2:
        return None
    chars = sword_db.all_swords()
    # 1. 精确（日文或中文名全等）
    for sid, info in chars.items():
        if sword_db._normalize(info["name"]) == target \
                or sword_db._normalize(info.get("name_zh", "")) == target:
            return sid
    # 2. 唯一包含候选（OCR 掐头/多读字）；两个及以上候选 → 拒认
    hit = None
    for sid, info in chars.items():
        name = sword_db._normalize(info["name"])
        if len(name) >= 2 and (target in name or name in target):
            if hit is not None:
                return None
            hit = sid
    return hit


def _slot_name_from_combined(raw: str, slot: int):
    """从「位置号＋刀名」格剥离本槽号；号错了不能拿剩余字认刀。"""
    text = re.sub(r"\s+", "", raw or "")
    if not text:
        return None, False
    numerals = "一二三四五六"
    if text[0] in numerals:
        if text[0] != numerals[slot - 1]:
            return None, True
        return text[1:] or None, False
    return text, False


def link_visible_slot(slot: dict, entries: list[dict], cultivation=None) -> dict:
    """当前槽与完整刀账按可见指纹链接；缺字段、重复或过期都不猜实例。"""
    if slot.get("slot_status") != "occupied" or not slot.get("sword_catalog_id"):
        return {"status": "insufficient", "observation_id": None}
    if any(slot.get(key) is None for key in
           ("level", "tou_level", "survival_max")):
        return {"status": "insufficient", "observation_id": None}
    if not isinstance(slot["level"], int) or isinstance(slot["level"], bool):
        return {"status": "insufficient", "observation_id": None}
    stats = slot.get("stats") or {}
    if any(stats.get(key) is None for key in GROWTH_STATS):
        return {"status": "insufficient", "observation_id": None}
    cultivation = cultivation or {}
    matches = []
    for entry in entries:
        if entry.get("sword_catalog_id") != slot["sword_catalog_id"]:
            continue
        # 等级会自然上涨，不能当实例身份证；只拒绝等级倒退与其他可见
        # 指纹冲突。完整刀账若有多振同指纹，下面仍会判 ambiguous。
        archived_level = entry.get("level")
        if not isinstance(archived_level, int) or slot["level"] < archived_level:
            continue
        if entry.get("tou_level") != slot["tou_level"]:
            continue
        archived = entry.get("stats") or {}
        if growth_stats_match(
                archived, stats, entry.get("name_zh") or "", cultivation,
                old_survival_max=entry.get("survival_max"),
                new_survival_max=slot["survival_max"],
                observation_id=entry.get("observation_id")):
            matches.append(entry)
    if len(matches) == 1 and matches[0].get("observation_id"):
        return {"status": "linked",
                "observation_id": matches[0]["observation_id"]}
    return {"status": "ambiguous" if matches else "stale",
            "observation_id": None}


def _shift(roi, cy):
    x0, y0, x1, y1 = roi
    return (x0, y0 + cy, x1, y1 + cy)


def _badge_cell(cy):
    try:
        return ROW_CELL_ROIS[_ROW_CY.index(cy) + 1]["badge"]
    except ValueError:
        return _shift(_BADGE_ROI, cy)


def _tactical_roles(sword_type, kiwame_status, injury):
    """碰瓷语义：太刀+极化+中伤三项都确认 → 碰瓷候选。

    供以后智能编队消费；heavy 永远安全硬拦截、medium 不被通用
    "受伤排除"提前吞掉是消费方的安全约束，本流程只落事实不编队。
    """
    if sword_type == "太刀" and kiwame_status == "kiwame" and injury == "medium":
        return ["medium_injury_kiwame_tachi"]
    return []


def _roster_facts_from_events(events, team_no, *, now=None):
    """JSON 事实 → {槽号: 槽事实}；整队不可用返回 None（调用方全量回退视觉）。

    每槽事实 {"occupied": bool, "sword_catalog_id", "level", "fatigue",
    "survival", "survival_max", "injury"}：
      - occupied 由成员关系（parties + slot_times）新鲜度正面判定，
        serial 空/0 即空槽（客户端数据本身就是空位正面证据）；
      - 其余字段各自独立校验 FACT_MAX_AGE 与战后时效（hp/疲劳战斗中
        会变，战斗前的读数一律作废），读不出就不出现该键——半残数据
        不放行，由 _read_roster_slot 逐字段回退视觉；
      - 当前页是哪支部队由调用方用行首位置标签视觉锚定后传入 team_no。
    """
    from .. import youzu_log
    from ..game_state import FACT_MAX_AGE, sword_facts
    now = time.time() if now is None else now
    state = sword_facts(events)
    party = state["parties"].get(str(team_no)) or {}
    slot_times = state["slot_times"].get(str(team_no)) or {}
    if not party:
        return None
    combat_index, combat_at = state["combat_index"], state["combat_at"]

    def fresh(at):
        return at is not None and 0 <= now - at <= FACT_MAX_AGE

    facts = {}
    for slot in range(1, 7):
        member_at = slot_times.get(str(slot))
        if not fresh(member_at):
            facts[slot] = None   # 成员关系缺失/过期：本槽整槽回退视觉
            continue
        serial = party.get(str(slot))
        if serial in (None, "", 0, "0"):
            facts[slot] = {"occupied": False}
            continue
        serial = str(serial)
        sword = state["swords"].get(serial) or {}
        times = state["field_times"].get(serial) or {}
        fact = {"occupied": True}
        if fresh(times.get("sword_id")):
            found = sword_db.find_game_sword(
                youzu_log._int(sword.get("sword_id"), -1))
            if found:
                fact["sword_catalog_id"] = found[0]
        if fresh(times.get("level")):
            member_level = youzu_log._int(sword.get("level"), -1)
            if member_level > 0:
                fact["level"] = member_level
        hp_at, hp_index = state["hp_times"].get(serial, (None, -1))
        if (fresh(hp_at) and hp_index >= combat_index and hp_at >= combat_at
                and fresh(times.get("hp_max"))):
            hp = youzu_log._int(sword.get("hp"), -1)
            maximum = youzu_log._int(sword.get("hp_max"), 0)
            if maximum > 0 and 0 <= hp <= maximum:
                fact["survival"], fact["survival_max"] = hp, maximum
                tier = youzu_log.injury_tier(hp, maximum)
                fact["injury"] = "none" if tier is None else _INJURY_ENUM[tier]
        fatigue_at = times.get("fatigue")
        fatigue_index = (state["field_indices"].get(serial) or {}
                         ).get("fatigue", -1)
        if (fresh(fatigue_at) and fatigue_index >= combat_index
                and fatigue_at >= combat_at):
            value = youzu_log._int(sword.get("fatigue"), -1)
            if 0 <= value <= 100:
                fact["fatigue"] = value
        facts[slot] = fact
    return facts


class TeamRosterMixin:
    """编队页只读点名。依赖宿主类的 navigate_to_stream、maa、config、record_event。"""

    def team_roster_stream(self, teams=None):
        """流式只读点名：逐部队切标签读六槽，落 team_roster.observed 事实。

        Args:
            teams: 要点名的部队号列表，默认 [1, 2, 3, 4, 5]。

        Yields:
            str: 执行状态消息
        """
        team_list = list(teams) if teams else [1, 2, 3, 4, 5]
        yield "[点名] 正在进入编队页…"
        for nav_msg in self.navigate_to_stream("编队"):
            yield nav_msg
        if self.current_location != "编队":
            if hasattr(self, "record_event"):
                self.record_event("team_roster.observed", team_no=None,
                                  slots=[], observation_status="failed",
                                  fail_reason="未到达编队页",
                                  source="formation_page")
            yield "[点名] 到达编队失败"
            return

        for team_no in team_list:
            if team_no not in _TEAM_TAB:
                yield f"[点名] 部队{team_no}不认识（只支持 1~5），跳过"
                continue
            # 切队正面确认：位置标签（"N之一"金匾）必须显示请求的队号，
            # 首尾两行都核对；点击被吞/延迟一拍都不算数——确认不了宁可
            # failed，绝不把上一队的成员记到这一队。
            label_seen = None
            confirmed = False
            for attempt in (1, 2, 3):
                yield f"[点名] 部队{team_no}：切标签（第 {attempt}/3 次）…"
                self.maa.click(Point(*_TEAM_TAB[team_no]))
                time.sleep(1.2)
                self.maa.screenshot(force=True)
                label_seen = (self._read_row_label(_ROW_CY[0]),
                              self._read_row_label(_ROW_CY[5]))
                if label_seen == (team_no, team_no):
                    confirmed = True
                    break
            if not confirmed:
                seen = "、".join(sorted(
                    {_TEAM_NAME.get(n, "未知") for n in label_seen}))
                if hasattr(self, "record_event"):
                    self.record_event("team_roster.observed", team_no=team_no,
                                      slots=[], observation_status="failed",
                                      fail_reason=f"队伍标签显示{seen}，切队未确认",
                                      source="formation_page")
                yield (f"[点名] 部队{team_no}：标签停在{seen}队，"
                       "切队未确认，该队不落事实")
                continue
            yield f"[点名] 部队{team_no}：标签确认，读六槽…"
            slots = self._read_team_page()
            if hasattr(self, "record_event"):
                self.record_event("team_roster.observed",
                                  team_no=team_no, slots=slots,
                                  observation_status=self._observation_status(slots),
                                  source="formation_page")
            occupied = sum(1 for s in slots if s["slot_status"] == "occupied")
            unknown = sum(1 for s in slots if s["slot_status"] == "unknown")
            kiwame = sum(1 for s in slots if s.get("kiwame_status") == "kiwame")
            yield (f"[点名] 部队{team_no}：{occupied} 振在队"
                   f"（极化 {kiwame}），{unknown} 槽存疑，事实已落账")
        yield "[点名] 点名完成，全程只读未动队伍"

    @staticmethod
    def _observation_status(slots):
        """顶层观测质量：任一槽 unknown 或有未读字段 → partial，否则 complete。"""
        for s in slots:
            if s["slot_status"] == "unknown" or s["unknown_fields"]:
                return "partial"
        return "complete"

    # ---------- JSON 事实（数据优先通道，视觉兜底） ----------

    def _roster_game_events(self):
        """JSON 事实事件源（注入缝：无 ADB 环境/测试可覆写）。"""
        from ..game_state import available_events
        return available_events(self.maa)

    def _roster_facts_by_slot(self):
        """当前页六槽的 JSON 事实 {槽号: 事实}；不可用返回 None。

        当前部队号由行首位置标签视觉锚定（行定位保留视觉）；
        锚不定就不用 JSON——绝不张冠李戴。
        """
        team_no = self._read_row_label(_ROW_CY[0])
        if team_no is None:
            return None
        try:
            events = self._roster_game_events()
        except (OSError, ValueError, RuntimeError):
            events = []
        if not events:
            return None
        return _roster_facts_from_events(events, team_no)

    # ---------- 单部队 ----------

    def _read_team_page(self):
        """当前帧读六槽。Returns: [slot dict × 6]（六槽固定输出）。

        身份/等级/生存/疲劳/伤势以 JSON 事实（sword_facts.parties 等）
        为准，数据缺失/过期才逐字段降级本链路的 OCR/伤势章；行定位
        （切队标签锚定、卡面空位正面证据、极化徽章/白樱花）保留视觉。

        伤势章在编队页持续脉动（真机探针：真章单帧匹配分 0.67~0.92
        随机摆动）——JSON 伤势不可用、走伤势章兜底的占用槽，对章没命中
        且未满血的最多补采两帧、间隔 0.6s，任一帧命中即取枚举；只重试
        伤势通道，其他字段仍以首帧为准。
        """
        stamps = self._roster_injury_stamps()
        facts = self._roster_facts_by_slot()
        slots = [self._read_roster_slot(slot, cy, stamps,
                                        None if facts is None
                                        else facts.get(slot))
                 for slot, cy in enumerate(_ROW_CY, start=1)]
        if stamps and any(s["slot_status"] == "occupied" and s["injury"] is None
                          for s in slots):
            for _attempt in range(2):
                time.sleep(0.6)
                self.maa.screenshot(force=True)
                for s in slots:
                    if s["slot_status"] == "occupied" and s["injury"] is None:
                        cy = _ROW_CY[s["slot"] - 1]
                        hit = self._read_slot_injury(
                            cy, stamps, s["survival"], s["survival_max"])
                        if hit is not None:
                            s["injury"] = hit
                if all(s["injury"] is not None
                       for s in slots if s["slot_status"] == "occupied"):
                    break
            for s in slots:
                if s["injury"] is not None:
                    s["unknown_fields"] = [f for f in s["unknown_fields"]
                                           if f != "injury"]
                    # 碰瓷角色依赖伤势枚举，补中后要重算
                    s["tactical_roles"] = _tactical_roles(
                        s["sword_type"], s["kiwame_status"], s["injury"])
        return slots

    def _read_roster_slot(self, slot, cy, stamps, fact=None):
        """读单个槽位：JSON 事实优先，视觉逐字段兜底。

        fact: _roster_facts_from_events 给出的本槽事实（occupied/
        sword_catalog_id/level/fatigue/survival/survival_max/injury），
        None 表示 JSON 不可用，全字段走视觉链。槽位形态：
        空位（正面证据）/ 占用（逐字段降级）/ 未知。
        """
        cells = ROW_CELL_ROIS[slot]
        combined = self._roster_ocr_text(cells["name"])
        name_raw, label_conflict = _slot_name_from_combined(combined, slot)
        if combined is None:
            # 老测试桩及旧识别路径兜底；新格有内容但位置号冲突时不能回退猜名。
            name_raw = self._roster_ocr_text(_shift(_NAME_ROI, cy))

        levels_tokens = self._roster_ocr_tokens(cells["levels"])
        if levels_tokens:
            # 延迟导入：sword_inventory 本身会导入 team_roster 的徽章判定。
            from .sword_inventory import parse_levels_cell
            levels = parse_levels_cell(levels_tokens)
        else:
            levels = {}
        level = levels.get("level")
        tou_level = levels.get("tou_level")
        survival = levels.get("survival")
        survival_max = levels.get("survival_max")
        fatigue = levels.get("fatigue")
        if level is None:
            level = _parse_level(self._roster_ocr_text(_shift(_LEVEL_ROI, cy)))
        if fatigue is None:
            fatigue = _parse_fatigue_tokens(
                self._roster_ocr_tokens(_shift(_FATIGUE_ROI, cy)),
                y_min=cy + 28, y_max=cy + 55)
        if survival is None:
            survival, survival_max = _parse_survival_tokens(
                self._roster_ocr_tokens(_shift(_SURVIVAL_ROI, cy)))
        badge = self._read_badge_char(cy)

        # ---- JSON 事实逐字段覆盖：数据缺失/过期保持上面的视觉值，
        # 由本字段各自的兜底链（OCR/伤势章）顶上 ----
        fact = fact or {}
        json_occupied = fact.get("occupied")   # True=占用 / False=空槽 / None=未知
        if fact.get("level") is not None:
            level = fact["level"]
        if fact.get("fatigue") is not None:
            fatigue = fact["fatigue"]
        if fact.get("survival") is not None:
            survival = fact["survival"]
        if fact.get("survival_max") is not None:
            survival_max = fact["survival_max"]
        if fact.get("injury") is not None:
            injury = fact["injury"]
        else:
            injury = self._read_slot_injury(cy, stamps, survival, survival_max)
        blank = self._card_blank(cy)

        # 内容证据任一：JSON 确认占用、名字/数值/徽章字符/伤势章
        # （可靠花数在占用分支里才算）
        has_content = label_conflict or json_occupied is True or any(
            v is not None for v in (name_raw, level, fatigue, survival,
                                    badge["type_raw"], injury))

        # 空位：JSON 成员关系给空槽正面证据直接成立；否则维持视觉
        # 正面证据（卡面近纯白 + 无内容证据）。纯白卡面上花数模板
        # 不可能可靠命中，花数证据只在占用分支产生
        if json_occupied is False or (blank and not has_content):
            return {"slot": slot, "slot_status": "empty", "name_raw": None,
                    "name": None, "name_status": None,
                    "sword_catalog_id": None, "sword_type": None,
                    "rarity_base": None, "level": None, "tou_level": None,
                    "fatigue": None,
                    "survival": None, "survival_max": None, "injury": None,
                    "badge": badge, "kiwame_status": None,
                    "kiwame_evidence": [], "tactical_roles": [],
                    "unknown_fields": []}

        # 身份：JSON sword_id 映射目录优先；失败降级 OCR 名字匹配
        sid = fact.get("sword_catalog_id") if json_occupied else None
        if sid is None and name_raw:
            sid = _match_name(name_raw)
        if sid:
            info = sword_db.all_swords()[sid]
            name = info.get("name_zh") or info["name"]
            name_status = "recognized"
        else:
            info, name = {}, None
            name_status = "unrecognized" if name_raw else None

        # 卡面有形但没有任何读得动的内容（整页失明/证据不足），
        # 或卡面纯白却有内容证据（矛盾）：都 → unknown，绝不硬判空位
        if label_conflict or (blank and has_content) or (not blank and not has_content):
            return {"slot": slot, "slot_status": "unknown", "name_raw": name_raw,
                    "name": name, "name_status": name_status,
                    "sword_catalog_id": sid, "sword_type": None,
                    "rarity_base": None, "level": level, "tou_level": tou_level,
                    "fatigue": fatigue,
                    "survival": survival, "survival_max": survival_max,
                    "injury": injury, "badge": badge, "kiwame_status": "unknown",
                    "kiwame_evidence": [], "tactical_roles": [],
                    "unknown_fields": _unknown_fields(
                        name_status, sid, None, level, fatigue, survival,
                        injury, badge, True)}

        # 有内容但身份/数值全读不出：unknown（JSON 已确认占用的除外——
        # 成员关系本身就是正面占用证据，字段各自降级即可）
        if json_occupied is not True and name_status != "recognized" \
                and level is None and fatigue is None and survival is None:
            return {"slot": slot, "slot_status": "unknown", "name_raw": name_raw,
                    "name": name, "name_status": name_status,
                    "sword_catalog_id": sid, "sword_type": None,
                    "rarity_base": None, "level": None, "tou_level": tou_level,
                    "fatigue": None,
                    "survival": None, "survival_max": None,
                    "injury": injury, "badge": badge, "kiwame_status": "unknown",
                    "kiwame_evidence": [], "tactical_roles": [],
                    "unknown_fields": _unknown_fields(
                        name_status, sid, None, None, None, None,
                        injury, badge, True)}

        # 占用：名册身份 + 数值逐字段降级
        rarity_base = info.get("rarity") if sid else None
        sword_type = _norm_type(info.get("type")) if sid else None
        badge = self._match_slot_flowers(cy, badge, sword_type)
        kiwame_status, kiwame_evidence = self._kiwame_conclusion(
            sid, rarity_base, badge, injury, sakura_hit=self._sakura_hit(cy))
        return {"slot": slot, "slot_status": "occupied", "name_raw": name_raw,
                "name": name, "name_status": name_status,
                "sword_catalog_id": sid, "sword_type": sword_type,
                "rarity_base": rarity_base, "level": level,
                "tou_level": tou_level, "fatigue": fatigue,
                "survival": survival, "survival_max": survival_max,
                "injury": injury, "badge": badge,
                "kiwame_status": kiwame_status,
                "kiwame_evidence": kiwame_evidence,
                "tactical_roles": _tactical_roles(sword_type, kiwame_status, injury),
                "unknown_fields": _unknown_fields(
                    name_status, sid, sword_type, level, fatigue, survival,
                    injury, badge, kiwame_status == "unknown")}

    def _read_slot_stats(self, slot):
        """属性带逐格读；整带 OCR 会把相邻数字粘成一个数。缺格留 None。"""
        from .sword_inventory import split_stats_roi
        cells = split_stats_roi(ROW_CELL_ROIS[slot]["stats"])
        out = {}
        for name, rect in zip(_STAT_NAMES, cells):
            numbers = [int(text.strip()) for text, _pt in
                       self._roster_ocr_tokens(rect)
                       if re.fullmatch(r"\d{1,3}", text.strip())]
            out[name] = numbers[0] if len(numbers) == 1 else None
        return out

    # ---------- 伤势 / 白樱花 ----------

    def _roster_injury_stamps(self):
        """伤势章模板（沿用出阵配置的同一组章；键为中文 重伤/中伤/轻伤）。"""
        return self.config.get("sortie", {}).get("injury_stamps", {})

    def _read_slot_injury(self, cy, stamps, survival, survival_max):
        """伤势视觉兜底：三类章各取真实分，比分数选最佳，不吃配置顺序抢答。

        只在 JSON 伤势（hp/hp_max 新鲜换算）缺失/过期时由
        _read_roster_slot 调用；JSON 在席时章匹配整通道跳过。

        - 最高分且 ≥ 编队页校准阈值才入围；
        - 与次高分差距不足（脉动动画章会互相蹭分）→ unknown；
        - 编队页真帧未证明的类别（当前只有中伤有样本）→ unknown，
          绝不出阵页阈值硬判；
        - 全部未命中：生存==上限且两个数字可靠读出 → none（正面无伤），
          否则 unknown——"章没匹配到"绝不直接当无伤。
        """
        img = self.maa.screenshot()
        if img is not None and stamps:
            scores = {}
            for key, stamp in stamps.items():
                tpl = stamp.get("template") if isinstance(stamp, dict) else None
                if not tpl:
                    continue
                enum = _INJURY_ENUM.get(key, key)
                scores[enum] = self.maa.template_match_score(
                    tpl, roi=roi_4to4(*_shift(_INJURY_ROI, cy)))
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            if ranked and ranked[0][1] >= _ROSTER_STAMP_THRESHOLD:
                top_enum, top_score = ranked[0]
                second = ranked[1][1] if len(ranked) > 1 else 0.0
                if (top_enum in _FORMATION_PROVEN_INJURIES
                        and top_score - second >= _ROSTER_STAMP_MARGIN):
                    return top_enum
        # 章未命中：生存==上限且两个数字都可靠读出 → 正面无伤
        if (survival is not None and survival_max is not None
                and survival == survival_max):
            return "none"
        return None

    def _sakura_hit(self, cy):
        """白樱花（极化标记）编队页同源模板命中检测。"""
        return bool(self.maa.template_match(
            _SAKURA_TEMPLATE, roi=roi_4to4(*_shift(_SAKURA_ROI, cy)),
            threshold=_SAKURA_THRESHOLD))

    # ---------- 卡面占用（空位正面证据） ----------

    def _card_blank(self, cy):
        """卡面立绘区是否近纯白（真机校准的空槽形态）。

        占位卡有立绘纹理（真机 30 槽 std≥51.1），空位卡是纯白纸面
        （std≤9.6、亮度≥252）；截图失明/黑帧不满足"亮且平"，
        返回 False → 走 unknown，绝不判空。
        """
        import cv2
        img = self.maa.screenshot()
        if img is None:
            return False
        x0, y0, x1, y1 = _shift(_CARD_REGION, cy)
        card = img[y0:y1, x0:x1]
        if card.size == 0:
            return False
        gray = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
        return (gray.mean() >= _CARD_BLANK_MEAN
                and gray.std() < _CARD_BLANK_STD
                and (gray < 150).mean() < _CARD_BLANK_DARK)

    # ---------- 徽章双通道 ----------

    def _read_badge_char(self, cy):
        """徽章字符通道：OCR 认刀种字符 → 规范化刀种枚举。

        Returns:
            {"type_raw": 原始字符或 None, "type": 刀种枚举或 None,
             "flowers": None, "score": None, "threshold": 阈值,
             "top_type": None, "top_score": None, "conclusion": "char_only"}
            花数通道由 _match_slot_flowers 在确认刀种后补齐。
        """
        img = self.maa.screenshot()
        type_raw = None
        if img is not None:
            for text, _pt in self.maa.ocr_all(
                    roi_4to4(*_badge_cell(cy)), img) or []:
                for ch in _BADGE_TYPE_OF:
                    if ch in text:
                        type_raw = ch
                        break
                if type_raw:
                    break
        return {"type_raw": type_raw, "type": _BADGE_TYPE_OF.get(type_raw),
                "flowers": None, "score": None, "threshold": _BADGE_THRESHOLD,
                "top_type": None, "top_score": None, "conclusion": "char_only"}

    def _flower_templates(self):
        """花数徽章模板 [(路径, 花数, 刀种枚举)]，进程内缓存。

        模板来自仓库既有版本资源（文件名自带刀种，如 五花太刀.png），
        刀种统一规范化后供"确认刀种过滤"用。加载本体在模块级
        load_flower_templates（一览盘点复用同一套模板库）。
        """
        return load_flower_templates(
            getattr(self.maa, "resource_dir", "resource/base"))

    def _match_slot_flowers(self, cy, badge, sword_type):
        """花瓣数量通道：在确认刀种的模板子集里匹配当前花数。

        刀种一致性（返工单第五条）：
          - 徽章字符刀种与名册刀种不一致 → 花数证据作废（type_conflict）；
          - 有确认刀种（名册优先，其次徽章字符）→ 只在该刀种的模板里
            匹配；全局最高分模板若是别的刀种且分数达标 → 刀种冲突作废；
          - 没有确认刀种（名字没认出+字符没读到）→ 不产生花数证据
            （no_confirmed_type），只留全局诊断分数。
        匹配核心在模块级 match_badge_flowers（一览盘点复用同一套）。
        """
        char_type = badge["type"]
        if sword_type and char_type and sword_type != char_type:
            # 徽章字符与名册刀种不一致：花数证据不可参与极化结论
            badge["conclusion"] = "type_conflict"
            badge["top_type"] = char_type
            return badge
        confirmed_type = sword_type or char_type
        img = self.maa.screenshot()
        region = None
        if img is not None:
            x0, y0, x1, y1 = _badge_cell(cy)
            region = img[y0:y1, x0:x1]
        badge.update(match_badge_flowers(
            region, self._flower_templates(), confirmed_type,
            _PROVEN_FLOWER_COMBOS))
        return badge

    # ---------- 极化结论（分层单向证明） ----------

    def _kiwame_conclusion(self, sid, rarity_base, badge, injury, sakura_hit):
        """极化结论：证据数组 + 单向结论，冲突 → unknown。

        Returns:
            (kiwame_status ∈ kiwame/normal/unknown, kiwame_evidence 数组)
            数组每项 {type, raw_value, score, threshold, conclusion}，
            保留每条证据的实测原值供以后审计。
        injury 仅保留在签名里占位（伤势不参与形态结论）。
        规则本体在模块级 conclude_kiwame（一览盘点复用同一套）。
        """
        return conclude_kiwame(sid, rarity_base, badge, sakura_hit)

    # ---------- 位置标签（切队正面确认） ----------

    def _read_row_label(self, cy):
        """读行首位置标签（竖排"N之M"金匾），返回队伍号 1~5 或 None。"""
        tokens = self._roster_ocr_tokens(_shift(_LABEL_ROI, cy))
        chars = "".join(t for t, _pt in sorted(
            tokens, key=lambda tp: getattr(tp[1], "y", 0) or 0))
        m = re.search(r"[一二三四五]", chars)
        return _CN_NUM.get(m.group()) if m else None

    # ---------- 小工具 ----------

    def _roster_ocr_text(self, roi):
        """本链路专用 OCR 入口：按 f9f13b2 先例改名，防 login 的 _ocr_text 截胡。"""
        text = "".join(t for t, _pt in self._roster_ocr_tokens(roi)).strip()
        return text or None

    def _roster_ocr_tokens(self, roi):
        """本链路专用 OCR token 入口（保留坐标，供按 y 位置取数）。"""
        img = self.maa.screenshot()
        if img is None:
            return []
        return self.maa.ocr_all(roi_4to4(*roi), img) or []


def _unknown_fields(name_status, sid, sword_type, level, fatigue, survival,
                    injury, badge, kiwame_unknown):
    """列出该槽没读出来的字段；unknown 槽位（全静默）则列全部。"""
    if kiwame_unknown and name_status is None and sid is None \
            and sword_type is None and level is None and fatigue is None \
            and survival is None and injury is None \
            and badge["type"] is None and badge["flowers"] is None:
        return ["name", "sword_catalog_id", "sword_type", "rarity_base",
                "level", "fatigue", "survival", "injury", "badge.type",
                "badge.flowers", "kiwame_status"]
    fields = []
    if name_status != "recognized":
        fields.extend(["name", "sword_catalog_id", "sword_type", "rarity_base"])
    if level is None:
        fields.append("level")
    if fatigue is None:
        fields.append("fatigue")
    if survival is None:
        fields.append("survival")
    if injury is None:
        fields.append("injury")
    if badge["type"] is None:
        fields.append("badge.type")
    if badge["flowers"] is None:
        fields.append("badge.flowers")
    if kiwame_unknown:
        fields.append("kiwame_status")
    return fields
