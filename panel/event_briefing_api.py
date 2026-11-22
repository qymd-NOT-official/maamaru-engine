"""活动开幕参谋 API：把 event_briefing 纯函数接到国服刀帐上。

POST /api/event-briefing：入参是活动各字段 + enemy_fast_spear（平铺 JSON），
后端读刀帐档案（机器盘点 + 人工标注合并视图）转成 roster 契约，
交给 touken.event_briefing.build_briefing 出简报。刀帐没数据就如实报错，
绝不编一份假的糊弄玩家。
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

# 刀帐没盘点时的引导文案：说清去哪补数据，不甩锅不编造。
_ARCHIVE_EMPTY_GUIDE = (
    "刀帐还没有可用数据：先去「刀剑」页跑一次「刀剑盘点」，"
    "参谋要对着你的真实家底说话，绝不硬编。"
)

# 预设名上限沿用 custom_formations.validate_formation 的 20 字契约。
_PRESET_NAME_MAX = 20


def briefing_preset_name(event_name) -> str:
    """预设名契约：「活动·<活动名>」，总长按 20 字截断（活动名太长砍尾巴）。"""
    base = str(event_name or "").strip()
    name = f"活动·{base}" if base else "活动编队"
    return name[:_PRESET_NAME_MAX]


def _archive_ready(archive) -> bool:
    return (isinstance(archive, dict) and archive.get("done")
            and bool(archive.get("entries")))


def _slot_entry(entry: dict) -> dict:
    """刀帐档案条目 → 预设槽位快照；字段口径同编队页 assignPresetCandidate。"""
    slot = {}
    for key in ("observation_id", "sword_catalog_id", "name_zh", "level",
                "tou_level", "survival_max", "form_status", "kiwame_date",
                "observed_at"):
        value = entry.get(key)
        if value is not None:
            slot[key] = value
    stats = entry.get("stats")
    if isinstance(stats, dict) and stats:
        slot["stats"] = dict(stats)
    return slot


def _form_label(form) -> str:
    return {"kiwame": "极", "normal": "普通"}.get(form, str(form or "未知"))


def _resolve_candidate(candidate: dict, entries: list[dict]):
    """一条候选 → 唯一刀帐条目；对不上/分不清都返回玩家看得懂的原因。

    身份语义照编队页选人那套：刀名+形态是本体，等级是参考——同名同形态
    只剩一振时，等级涨过（出阵回来升级了）也认；同名同形态有多振时
    必须等级精确吻合，否则如实说分不清，绝不乱点鸳鸯谱。
    """
    name = str(candidate.get("name") or "").strip()
    form = candidate.get("form")
    level = candidate.get("level")
    label = f"「{name}（{_form_label(form)}）」" if name else "一位没报上名的候选"
    if not name or form not in ("kiwame", "normal"):
        return None, f"{label}的身份信息不全（刀名和普通／极形态都得有），存不了"
    pool = [entry for entry in entries
            if entry.get("name_zh") == name
            and entry.get("form_status") == form]
    if not pool:
        return None, (f"{label}在刀帐里对不上号——刀帐可能不是最新的，"
                      "先去「刀剑」页跑一次「刀剑盘点」再来")
    exact = [entry for entry in pool
             if type(level) is int and entry.get("level") == level]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, (f"{label}在刀帐里有 {len(exact)} 振同名同形态同等级，"
                      "分不清是哪一振；请到「部队预设」页亲手挑")
    if len(pool) == 1:
        return pool[0], None
    return None, (f"{label}在刀帐里有 {len(pool)} 振同名同形态，等级又都对不上，"
                  "分不清是哪一振；先到「刀剑」页跑一次「刀剑盘点」，"
                  "或到「部队预设」页亲手挑")


def save_briefing_formation(body: dict, archive: dict):
    """参谋编队候选 → 部队预设记录并落盘。返回 (record, overwritten, error)。

    重名（同名「活动·X」）覆盖旧预设，id 保留；新名字占新坑，满 5 套如实报。
    """
    import time

    from touken import custom_formations as cf

    if not isinstance(body, dict):
        return None, False, "参数得是一组编队安排"
    try:
        target_team = int(body.get("target_team"))
    except (TypeError, ValueError):
        return None, False, "得选一个目标部队（部队一到部队五）"
    candidates = body.get("candidates")
    if (not isinstance(candidates, list) or not candidates
            or len(candidates) > 6
            or any(not isinstance(item, dict) for item in candidates)):
        return None, False, "编队候选得是 1~6 振刀的名单"
    entries = archive.get("entries") or []

    slots = {}
    seen_identity = {}
    for index, candidate in enumerate(candidates, 1):
        entry, err = _resolve_candidate(candidate, entries)
        if err:
            return None, False, err
        identity = entry.get("observation_id") or entry.get("sword_catalog_id")
        if identity and identity in seen_identity:
            return None, False, (f"{index}号位和{seen_identity[identity]}号位"
                                 "挑中了同一振刀，一队里一位刀只能站一个位置")
        if identity:
            seen_identity[identity] = index
        slots[str(index)] = _slot_entry(entry)

    name = briefing_preset_name(body.get("event_name"))
    formations = cf.load_formations()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    existing = next((item for item in formations
                     if isinstance(item, dict)
                     and str(item.get("name") or "") == name), None)
    if existing is not None:
        record = {"id": existing.get("id"), "name": name,
                  "target_team": target_team, "slots": slots,
                  "created_at": existing.get("created_at", now),
                  "updated_at": now}
        err = cf.validate_formation(
            record, [f for f in formations if f.get("id") != record["id"]])
        if err:
            return None, False, err
        formations[formations.index(existing)] = record
        cf.save_formations(formations)
        return record, True, None
    try:
        fid = cf.new_formation_id(formations)
    except ValueError as exc:
        return None, False, str(exc)
    record = {"id": fid, "name": name, "target_team": target_team,
              "slots": slots, "created_at": now, "updated_at": now}
    err = cf.validate_formation(record, formations)
    if err:
        return None, False, err
    formations.append(record)
    cf.save_formations(formations)
    return record, False, None


def archive_to_roster(archive: dict) -> list[dict]:
    """刀帐档案条目 → event_briefing roster 契约。

    契约映射：name_zh→name、sword_type→type（档案层已把脇差/槍/剣
    归一成胁差/枪/剑）、level/form_status/fatigue 同名透传。
    档案没有伤势和刀装位字段——不知道就是不知道，不出字段，
    由 event_briefing._normalize_roster 落 None。缺 name/type/level
    的条目也原样透传，清洗归参谋模块管。
    """
    roster = []
    for entry in archive.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        roster.append({
            "name": entry.get("name_zh"),
            "type": entry.get("sword_type"),
            "level": entry.get("level"),
            "form": entry.get("form_status"),
            "fatigue": entry.get("fatigue"),
        })
    return roster


def create_event_briefing_router():
    router = APIRouter(prefix="/api/event-briefing")

    @router.post("")
    async def build_event_briefing(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "参数没读明白，请重试。")
        if not isinstance(body, dict):
            raise HTTPException(400, "参数得是一组活动设置。")
        # 平铺契约：event 各字段 + enemy_fast_spear，enemy_fast_spear 不算活动字段
        event = {key: value for key, value in body.items() if key != "enemy_fast_spear"}
        enemy_fast_spear = bool(body.get("enemy_fast_spear"))

        # 延迟导入：测试可 patch touken.sword_archive.get_sword_archive，
        # 也避免面板启动就拉起整条刀帐管线。
        from touken.sword_archive import get_sword_archive
        archive = get_sword_archive()
        if not isinstance(archive, dict) or not archive.get("done") \
                or not archive.get("entries"):
            raise HTTPException(503, _ARCHIVE_EMPTY_GUIDE)
        roster = archive_to_roster(archive)

        from touken.event_briefing import build_briefing
        try:
            return build_briefing(event, roster, enemy_fast_spear=enemy_fast_spear)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.post("/formation")
    async def save_formation_preset(request: Request):
        """编队候选一键存成部队预设：候选回刀帐认人，认不齐就如实拒绝。"""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "参数没读明白，请重试。")
        from touken.sword_archive import get_sword_archive
        archive = get_sword_archive()
        if not _archive_ready(archive):
            raise HTTPException(503, _ARCHIVE_EMPTY_GUIDE)
        record, overwritten, err = save_briefing_formation(body, archive)
        if err:
            return JSONResponse({"ok": False, "reason": err}, status_code=400)
        return {"ok": True, "formation": record, "overwritten": overwritten}

    return router
