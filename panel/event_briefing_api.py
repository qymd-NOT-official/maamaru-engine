"""活动开幕参谋 API：把 event_briefing 纯函数接到国服刀帐上。

POST /api/event-briefing：入参是活动各字段 + enemy_fast_spear（平铺 JSON），
后端读刀帐档案（机器盘点 + 人工标注合并视图）转成 roster 契约，
交给 touken.event_briefing.build_briefing 出简报。刀帐没数据就如实报错，
绝不编一份假的糊弄玩家。
"""

from fastapi import APIRouter, HTTPException, Request

# 刀帐没盘点时的引导文案：说清去哪补数据，不甩锅不编造。
_ARCHIVE_EMPTY_GUIDE = (
    "刀帐还没有可用数据：先去「刀剑」页跑一次「刀剑盘点」，"
    "参谋要对着你的真实家底说话，绝不硬编。"
)


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

    return router
