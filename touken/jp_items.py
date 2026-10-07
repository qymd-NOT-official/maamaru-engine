"""日服道具对应与库存更新；未知编号保留，不套用国服扩展编号。"""
from .jp_import import _count

NAMES = {
    '1': '御守', '2': '御守·极', '3': '仙人团子', '4': '御札·富士',
    '5': '御札·松', '6': '御札·竹', '7': '御札·梅', '8': '加速符',
    '9': '修行召回鸽', '17': '幕内便当', '18': '一套纸笔',
    '19': '修行衣装', '20': '修行道具', '21': '远征召回鸽',
    '24': '苏言机', '25': '笛', '26': '琴', '27': '三味线',
    '28': '太鼓', '29': '铃', '37': '一口团子', '68': '堆肥',
    '1001': '根兵糖·並', '1002': '根兵糖·上', '6001': '异去探索道具',
}


def item_reading(payload):
    raw = payload.get('item')
    if not isinstance(raw, dict):
        return None  # 奖励列表不是当前库存。
    result = {}
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        cid = _count(entry.get('consumable_id'))
        count = _count(entry.get('num'))
        if cid is None or cid <= 0 or count is None:
            continue
        key = str(cid)
        row = {'count': count, 'item_id': key}
        # 只保留道具期限字段，不保存账号或响应中的其他内容。
        for field in ('expired_at', 'expire_at', 'expiration_date', 'limit_at'):
            value = entry.get(field)
            if isinstance(value, str) and len(value) <= 40:
                row['expires_at'] = value
                break
        result[key] = row
    return result or None


def inventory(store, to_ts=None):
    import json
    import time
    items = {}
    seen = set()
    for row in store._conn().execute(
        "SELECT ts,payload FROM events WHERE event_type='items.observed' "
        "AND script IN ('jp_listener','jp_netlog') AND ts <= ? ORDER BY ts DESC,id DESC",
        (time.time() if to_ts is None else float(to_ts),)):
        for cid, reading in json.loads(row['payload']).get('items', {}).items():
            if cid in seen:
                continue
            seen.add(cid)
            name = NAMES.get(cid, f'名称待确认（编号 {cid}）')
            items[name] = {**reading, 'observed_at': row['ts']}
    return items
