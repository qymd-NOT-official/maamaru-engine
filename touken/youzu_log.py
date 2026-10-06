# -*- coding: utf-8 -*-
"""国服客户端 HttpRequestCollect 日志读取 · 只读事实层（原型）

背景（2026-09-28 实测）：国服 APK（com.youzu.djlw，游族）把每一次
HTTP 请求的完整收发明文写进
  /data/data/com.youzu.djlw/files/userdata/HttpRequestCollect
响应体是不加密的 JSON（128 位的 t 字段只是签名）。MuMu 上 `adb root`
之后 adb pull 即可拿到，对游戏进程完全无感（无注入/无改包/无网络接触）。

特性（都已实测）：
  - 每次启动游戏时日志被清空重写——想留记录必须在下次启动前 pull；
  - 启动进本丸会自动拉全量（login/start、party/list、sally），
    开局即快照，不用翻任何界面；
  - 每 30 秒左右一条 keepalive。

本模块只做两件事：pull 日志、解析出本丸状态快照。不读写游戏、
不重放请求（拿 session 直接调 API 是另一条有风险的路，没走）。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

PACKAGE = "com.youzu.djlw"
REMOTE_LOG = f"/data/data/{PACKAGE}/files/userdata/HttpRequestCollect"

DEFAULT_ADB = r"D:\MUMU\MuMuPlayer\nx_device\12.0\shell\adb.exe"
DEFAULT_ADDRESS = "127.0.0.1:16384"

# 行格式（实测）：
#   【2026-09-28 11:34:37】【C->S】[POST] <url> Data:[k=v&k=v]
#   【2026-09-28 11:34:37】【S->C】<url> readyState:4 status:200 data:{...}
_LINE_RE = re.compile(
    r"^【(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})】"
    r"【(?P<direction>C->S|S->C)】(?P<rest>.*)$")

# party.status 的含义是按日服 API 惯例推的（1=待命 2=远征中 3=出阵中），
# 国服还没逐项实测校准，显示时保留原始值，别拿这个标签做自动化判断。
_PARTY_STATUS_LABEL = {"0": "未知", "1": "待命", "2": "远征中?", "3": "出阵中?"}


# ---------------------------------------------------------------- pull

def _adb_run(adb_path: str, address: str, args: list, timeout: int = 30):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([adb_path, "-s", address] + args,
                          capture_output=True, timeout=timeout,
                          creationflags=flags)


def pull_log(adb_path: str = DEFAULT_ADB, address: str = DEFAULT_ADDRESS,
             dest_dir: Path | str = Path(".tmp") / "youzu") -> Path:
    """把设备上的 HttpRequestCollect 拉到本地，返回本地路径。

    adb root 是幂等的（已是 root 时秒回）；adbd 重启后 root 会掉，
    所以每次 pull 前都补一刀。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    _adb_run(adb_path, address, ["root"], timeout=15)
    time.sleep(1.5)  # root 重启 adbd，给它一口气的工夫
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"HttpRequestCollect-{stamp}.log"
    r = _adb_run(adb_path, address, ["pull", REMOTE_LOG, str(dest)],
                 timeout=120)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError(f"adb pull 失败: {r.stderr or r.stdout}")
    return dest


# ---------------------------------------------------------------- parse

def parse_events(path: Path | str) -> list[dict]:
    """把日志解析成事件列表 [{ts, direction, method, url, endpoint, payload}]。

    S->C 的 payload 是响应 JSON（解析失败时 payload=None 并记 raw 长度）；
    C->S 的 payload 是请求参数 dict。解析失败的行不静默吞——记进
    返回列表的 bad_lines 统计里（挂事件末尾的元信息条）。
    """
    events: list[dict] = []
    bad = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = _LINE_RE.match(line)
            if not m:
                if line.strip():
                    bad += 1
                continue
            rest = m.group("rest")
            ev = {"ts": m.group("ts"), "direction": m.group("direction"),
                  "method": None, "url": None, "endpoint": None,
                  "payload": None}
            if ev["direction"] == "C->S":
                mm = re.match(r"\[(?P<method>\w+)\]\s+(?P<url>\S+)"
                              r"(?:\s+Data:\[(?P<data>.*)\])?\s*$", rest)
                if mm:
                    ev["method"] = mm.group("method")
                    ev["url"] = mm.group("url")
                    data = mm.group("data")
                    if data:
                        ev["payload"] = dict(parse_qsl(data, keep_blank_values=True))
                else:
                    bad += 1
                    continue
            else:
                mm = re.match(r"(?P<url>\S+)\s+readyState:(?P<rs>\d+)"
                              r"\s+status:(?P<sc>\d+)\s+data:(?P<data>.*)$", rest)
                if not mm:
                    bad += 1
                    continue
                ev["url"] = mm.group("url")
                ev["status"] = int(mm.group("sc"))
                try:
                    ev["payload"] = json.loads(mm.group("data"))
                except ValueError:
                    bad += 1
            if ev["url"]:
                path_part = re.sub(r"^https?://[^/]+", "", ev["url"])
                ev["endpoint"] = path_part.split("?", 1)[0]
            events.append(ev)
    events.append({"ts": None, "direction": "META",
                   "endpoint": None, "url": None, "method": None,
                   "payload": {"bad_lines": bad, "event_count": len(events)}})
    return events


def _latest(events: list[dict], endpoint: str) -> dict | None:
    """某个端点最后一次响应的 payload（没有则 None）。"""
    for ev in reversed(events):
        if ev["direction"] == "S->C" and ev["endpoint"] == endpoint \
                and isinstance(ev["payload"], dict):
            return ev["payload"]
    return None


def _latest_merged(events: list[dict], endpoint: str) -> dict:
    """某个端点所有响应的逐键合并（旧的先铺，新的覆盖同名字段）。

    同一个端点的响应不一定每次都带全字段（比如 /home 后续心跳式
    调用可能只回变化的键），只取最后一条会丢字段。
    """
    merged: dict = {}
    for ev in events:
        if ev["direction"] == "S->C" and ev["endpoint"] == endpoint \
                and isinstance(ev["payload"], dict):
            merged.update(ev["payload"])
    return merged


# ---------------------------------------------------------------- snapshot

def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _sword_name(sword_id, db) -> str:
    """sword_id → 名字。极化刀帐号 = 通常番号 + 1（如 3 三日月→4 三月极），
    名册只收了通常形态；查不到就试前一个号并补「极」标。
    （国服的 evol_num 字段不是极化标记，实测极化刀它也是 0，别踩。）"""
    sid = _int(sword_id)
    try:
        if hasattr(db, "find_game_sword"):
            found_game = db.find_game_sword(sid)
            if found_game:
                _, info, form = found_game
                base = info.get("name_zh") or info.get("name") or f"刀帐{sid}"
                return base + ("·极" if form == "kiwame" else "")
            return f"刀帐{sid}"
        found = db.find_by_id(sid)
        if not found and sid > 1:
            prev = db.find_by_id(sid - 1)
            if prev:
                info = prev[1]
                base = info.get("name_zh") or info.get("name") or f"刀帐{sid - 1}"
                return base + "·极"
            return f"刀帐{sid}"
    except Exception:
        return f"刀帐{sid}"
    info = found[1]
    return info.get("name_zh") or info.get("name") or f"刀帐{sid}"


def _party_source(events):
    """只认含槽位的完整部队块；home/situation 的状态片段不能覆盖名单。"""
    endpoints = {"/party/list", "/login/start", "/sally", "/conquest", "/conquest/start", "/conquest/complete"}
    for ev in reversed(events):
        body = ev.get("payload")
        if (ev.get("direction") != "S->C" or ev.get("endpoint") not in endpoints
                or ev.get("status") != 200 or not isinstance(body, dict)
                or str(body.get("status")) != "0"):
            continue
        parties = body.get("party")
        if isinstance(parties, dict) and (
                (not parties and ev["endpoint"] == "/party/list")
                or (parties and all(isinstance(p, dict) and isinstance(p.get("slot"), dict)
                                    for p in parties.values()))):
            return ev
    return None


def build_snapshot(events: list[dict], with_swords: bool = True) -> dict:
    """把事件流汇总成本丸状态快照（latest-wins）。

    返回 dict 可直接 json.dump；swords 全量默认带上（它是事实层，
    展示层自己决定切多少）。
    """
    from . import sword_db  # 延迟 import，解析逻辑单测可以不碰名册

    login = _latest(events, "/login/start") or {}
    home = _latest_merged(events, "/home")
    situation = _latest(events, "/home/situation") or {}
    party_list = _latest(events, "/party/list") or {}
    sally = _latest(events, "/sally") or {}
    forge = _latest(events, "/forge") or {}
    conquest = _latest(events, "/conquest") or {}
    mission_index = _latest(events, "/mission/index") or {}
    leave = _latest_merged(events, "/home/leave")
    activity = _latest(events, "/home/get_all_activity") or {}

    swords = {}
    # 编队页/出阵准备页提供全量刀数据；远征页与结算响应补充最新成员状态。
    for ev in events:
        body = ev.get("payload")
        endpoint = ev.get("endpoint")
        if (ev.get("direction") != "S->C" or ev.get("status") != 200
                or not isinstance(body, dict) or str(body.get("status")) != "0"):
            continue
        if endpoint == "/party/list" and isinstance(body.get("sword"), dict):
            swords = dict(body["sword"])
        elif endpoint == "/sally" and isinstance(body.get("sword_all"), dict):
            swords = dict(body["sword_all"])
        elif endpoint in ("/conquest", "/conquest/complete") and isinstance(body.get("sword"), dict):
            swords.update(body["sword"])
    party_source = _party_source(events)
    parties = party_source["payload"]["party"] if party_source else {}

    # 资源：home/forge/conquest 里都带 resource，谁新用谁（此处按端点
    # 优先级取第一个非空，同一局内差异不大；要精确到时刻就查事件流）
    resource = home.get("resource") or forge.get("resource") \
        or conquest.get("resource") or {}
    currency = home.get("currency") or login.get("currency") or {}

    def _sword_brief(s):
        return {
            "serial_id": _int(s.get("serial_id")),
            "sword_id": _int(s.get("sword_id")),
            "name": _sword_name(s.get("sword_id"), sword_db),
            "level": _int(s.get("level")),
            "rarity": _int(s.get("rarity")),
            "hp": _int(s.get("hp")), "hp_max": _int(s.get("hp_max")),
            "fatigue": _int(s.get("fatigue")),
            "evol_num": _int(s.get("evol_num")),  # >0 一般是极化
            "protected": bool(_int(s.get("protect"))),
        }

    party_status = situation.get("party") or {}
    party_rows = []
    for no in sorted(parties, key=lambda x: _int(x)):
        p = parties[no]
        members = []
        for slot_no in sorted((p.get("slot") or {}), key=lambda x: _int(x)):
            sid = (p["slot"][slot_no] or {}).get("serial_id")
            if sid and str(sid) in swords:
                members.append(_sword_brief(swords[str(sid)]))
        st = str((party_status.get(str(no)) or {}).get("status")
                 if party_status else p.get("status") or "")
        party_rows.append({
            "party_no": _int(no),
            "party_name": p.get("party_name") or "",
            "status": _int(st, -1),
            "status_label": _PARTY_STATUS_LABEL.get(st, f"未知({st})"),
            "finished_at": p.get("finished_at"),
            "members": members,
        })

    forge_rows = []
    for slot_no, slot in sorted((situation.get("forge")
                                 or forge.get("forge") or {}).items(),
                                key=lambda kv: _int(kv[0])):
        forge_rows.append({"slot_no": _int(slot_no),
                           "finished_at": slot.get("finished_at")})

    missions = [
        {"mission_id": _int(m.get("mission_id")),
         "value": _int(m.get("value")),
         "status": _int(m.get("status"), -1)}
        for m in (mission_index.get("mission") or {}).values()
        if isinstance(m, dict)
    ]

    kiwame_return = []
    swords_by_serial = {str(s.get("serial_id")): s
                        for s in swords.values() if isinstance(s, dict)}
    for entry in ((leave.get("evolution") or {}).get("back") or {}).values():
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("serial_id"))
        known = swords_by_serial.get(sid) or {}
        kiwame_return.append({
            "serial_id": _int(entry.get("serial_id")),
            "name": (_sword_name(known.get("sword_id"), sword_db)
                     if known else ""),
            "finished_at": entry.get("finished_at"),
        })

    events_calendar = [
        {"event_id": e.get("event_id"), "type": _int(e.get("type"), -1),
         "start_at": e.get("start_at"), "end_at": e.get("end_at")}
        for e in (activity.get("event") or {}).values()
        if isinstance(e, dict)
    ]

    snap = {
        "schema": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "profile": {
            # 不落盘：名字 / 服务器 / user_id / user_code —— 「名字+那串
            # 1w-xxx」是流传已久的盗号两件套，状态面板用不着，一概不留。
            "level": _int(login.get("level")),
            "exp": _int(login.get("exp")),
            "created_at": login.get("created_at") or "",
            "secretary": _sword_name(login.get("secretary"), sword_db)
            if login.get("secretary") else "",
        },
        "resources": {
            "charcoal": _int(resource.get("charcoal")),   # 木炭
            "steel": _int(resource.get("steel")),         # 玉钢
            "coolant": _int(resource.get("coolant")),     # 冷却材
            "whetstone": _int(resource.get("file")),      # 砥石
            "bill": _int(resource.get("bill")),           # 手伝い札
            "koban": _int(currency.get("money")),         # 小判
        },
        "sword_count": len(swords),
        "sword_capacity": _int(login.get("sword_max_slot")
                               or login.get("max_sword")),
        "parties": party_rows,
        "forge_slots": forge_rows,
        "repair": situation.get("repair") or [],
        "duty": home.get("duty") or {},
        "conquest_summary": conquest.get("summary") or [],
        "event_points": sally.get("point") or {},
        "season": {"season_id": home.get("season_id"),
                   "end_at": home.get("season_end_at")},
        "missions": missions,
        "kiwame_return": kiwame_return,
        "events_calendar": events_calendar,
        "server_time": home.get("now") or login.get("now"),
    }
    if with_swords:
        snap["swords"] = [_sword_brief(s) for s in swords.values()]
    return snap


# ---------------------------------------------------------------- injury

# 伤势分档阈值（2026-09-28 CU 编队页实拍校准，样本：队3/队4）：
#   34%/38%/45%/60%/61% 全部挂「中伤」章 → 中伤线 > 61%，取 2/3；
#   94%/96% 在编队页不挂章（轻伤章编队页不显示，轻伤=掉血但 >2/3）；
#   重伤线无样本（全 roster 最伤 34% 仍是中伤），取 1/3 保守线——
#   若真线是 1/4，我们只是在 26~33% 提前停，安全方向。
def injury_tier(hp, hp_max) -> str | None:
    """hp 比例 → 轻伤/中伤/重伤；满血或数据非法返回 None。"""
    hp, hp_max = _int(hp, -1), _int(hp_max, 0)
    if hp_max <= 0 or hp < 0 or hp >= hp_max:
        return None
    if hp * 3 <= hp_max:
        return "重伤"
    if hp * 3 <= hp_max * 2:
        return "中伤"
    return "轻伤"


# 携带全量刀数据的端点（2026-09-28 实测：/sally 进出阵菜单必刷新，
# /party/list 进编队页刷新；/conquest/complete 等只带部分块，不算）。
_SWORD_FULL_ENDPOINTS = ("/party/list", "/sally")


def _latest_sword_pool(events: list[dict]):
    """最新一次全量刀数据 (swords_dict, event)。没有返回 (None, None)。"""
    for ev in reversed(events):
        if ev.get("direction") != "S->C" or ev.get("endpoint") not in \
                _SWORD_FULL_ENDPOINTS or not isinstance(ev.get("payload"), dict):
            continue
        pool = ev["payload"].get("sword") or ev["payload"].get("sword_all")
        if isinstance(pool, dict) and pool:
            return pool, ev
    return None, None


def party_injury_report(events: list[dict], party_no: int) -> dict | None:
    """某部队的逐振验伤报告（serial 锚定，同名复制人精确区分）。

    成员名单来自最新 /party/list，hp 来自最新全量刀数据响应；
    两边各自带 observed_at（数据时间，不许拿 pull 时间冒充）。
    任一成员在刀池里查不到 → 数据不全，整体返回 None（调用方回退
    视觉链，绝不拿半残数据放行）。
    """
    party_ev = None
    for ev in reversed(events):
        if ev.get("direction") == "S->C" and ev.get("endpoint") == "/party/list" \
                and isinstance(ev.get("payload"), dict) \
                and isinstance(ev["payload"].get("party"), dict):
            party_ev = ev
            break
    pool, pool_ev = _latest_sword_pool(events)
    if party_ev is None or pool is None:
        return None
    party = party_ev["payload"]["party"].get(str(party_no))
    if not isinstance(party, dict):
        return None
    from . import sword_db  # 与 build_snapshot 同款延迟 import
    roster = [{"serial_id": _int(sid), "name": _sword_name(s.get("sword_id"),
                                                            sword_db),
               "level": _int(s.get("level"))}
              for sid, s in pool.items() if isinstance(s, dict)]
    labels = dup_labels(roster)
    members = []
    for slot_no in sorted((party.get("slot") or {}), key=lambda x: _int(x)):
        serial = str((party["slot"][slot_no] or {}).get("serial_id") or "")
        if not serial or serial == "0":
            continue
        s = pool.get(serial)
        if not isinstance(s, dict):
            return None  # 有成员查不到 = 数据不全，不猜
        members.append({
            "serial_id": _int(serial),
            "sword_id": _int(s.get("sword_id")),
            "name": _sword_name(s.get("sword_id"), sword_db),
            "label": labels.get(_int(serial), ""),
            "level": _int(s.get("level")),
            "hp": _int(s.get("hp")), "hp_max": _int(s.get("hp_max")),
            "fatigue": _int(s.get("fatigue")),
            "protected": bool(_int(s.get("protect"))),
            "omamori": _int(s.get("item_id"), 0) or None,  # 装备的御守道具 id
            "injury": injury_tier(s.get("hp"), s.get("hp_max")),
        })
    if not members:
        return None
    rank = {None: 0, "轻伤": 1, "中伤": 2, "重伤": 3}
    worst = max(members, key=lambda m: rank[m["injury"]])
    return {
        "party_no": party_no,
        "members": members,
        "max_injury": worst["injury"],
        "observed_at": pool_ev.get("ts"),
        "party_observed_at": party_ev.get("ts"),
    }


def dup_labels(swords) -> dict:
    """同名刀消歧标签 {serial_id: 标签}。独占名字的给原名；撞名的
    等级能区分用「·Lv99」，等级也撞按 serial 升序给「·2号机」
    （serial 近似获得顺序，编号稳定不漂移）。"""
    rows = []
    for s in swords:
        if isinstance(s, dict) and s.get("serial_id") is not None:
            rows.append(s)
    groups: dict[str, list] = {}
    for s in rows:
        groups.setdefault(str(s.get("name") or ""), []).append(s)
    labels = {}
    for name, members in groups.items():
        members.sort(key=lambda m: _int(m.get("serial_id")))
        if len(members) == 1:
            labels[_int(members[0]["serial_id"])] = name
            continue
        lv_seen = {}
        for m in members:
            lv_seen.setdefault(_int(m.get("level")), []).append(m)
        for idx, m in enumerate(members, 1):
            lv = _int(m.get("level"))
            if len(lv_seen[lv]) == 1:
                labels[_int(m["serial_id"])] = f"{name}·Lv{lv}"
            else:
                labels[_int(m["serial_id"])] = f"{name}·{idx}号机"
    return labels


def build_home_situation(events: list[dict]) -> dict | None:
    """Only the allowlisted game facts needed by the personal homepage.

    Keep the source time of each section. A recent pull can contain an old
    login/party response, so pull time must never masquerade as observation time.
    """
    endpoints = ("/login/start", "/home", "/party/list",
                 "/home/leave", "/home/situation", "/sally",
                 "/conquest", "/conquest/start", "/conquest/complete")
    found = {}
    accepted = []
    for ev in events:
        if (ev.get("direction") == "S->C" and ev.get("endpoint") in endpoints
                and ev.get("status") == 200 and isinstance(ev.get("payload"), dict)
                and str(ev["payload"].get("status", 0)) == "0"):
            found[ev["endpoint"]] = ev
            accepted.append(ev)
    if not found:
        return None
    snap = build_snapshot(accepted, with_swords=True)
    party_source = _party_source(accepted)
    # 同名刀消歧（label/serial_tail 是新增字段，name/level 原样不动，
    # 旧前端无感；渲染侧认领后展示 label 即可）
    labels = dup_labels(snap.get("swords") or [])

    def _member(m):
        serial = m.get("serial_id")
        return {"name": m["name"], "level": m["level"],
                "label": labels.get(serial, m["name"]),
                "serial_tail": str(serial)[-4:] if serial else "",
                "hp": m.get("hp"), "hp_max": m.get("hp_max"),
                "fatigue": m.get("fatigue"),
                "injury": injury_tier(m.get("hp"), m.get("hp_max"))}

    def observed(endpoint):
        return (found.get(endpoint) or {}).get("ts")
    # 资源读数只认 /home 里真实出现的 resource/currency 块；日志没进过
    # 本丸（只有登录响应）时宁可缺省，也不拿 login 残块或全 0 冒充读数。
    home = _latest_merged(accepted, "/home")
    resources = (snap["resources"] if isinstance(home.get("resource"), dict)
                 or isinstance(home.get("currency"), dict) else None)
    # 手入槽：名字尽量用消歧标签，结构没实拍过，只透传认识的键
    swords_by_serial = {s.get("serial_id"): s for s in (snap.get("swords") or [])}
    repair = []
    for slot in snap.get("repair") or []:
        if not isinstance(slot, dict):
            continue
        entry = {"slot_no": _int(slot.get("slot_no")),
                 "finished_at": slot.get("finished_at")}
        sword = swords_by_serial.get(_int(slot.get("serial_id"), -1))
        if sword:
            entry["name"] = labels.get(sword["serial_id"], sword["name"])
        repair.append(entry)
    # 内番：同样没实拍样本，只有 finished_at 才值得上主页
    duty_raw = home.get("duty") or {}
    duty = ({"finished_at": duty_raw.get("finished_at")}
            if isinstance(duty_raw, dict) and duty_raw.get("finished_at")
            else None)
    # 活动点数：/sally 的 point 块（{活动id: 点数}），id→活动名未校准，
    # 主页只展示数字，名字交给面板自己的活动时间轴
    event_points = [{"event_id": str(k), "points": _int(v)}
                    for k, v in (snap.get("event_points") or {}).items()
                    if _int(v) > 0]
    return {
        "schema": 1,
        "secretary": {"name": snap["profile"]["secretary"],
                      "observed_at": observed("/login/start")},
        "parties": [{"party_no": p["party_no"], "party_name": p["party_name"],
                     "members": [_member(m) for m in p["members"]],
                     "finished_at": p["finished_at"]}
                    for p in snap["parties"]] if party_source else [],
        "parties_observed_at": party_source["ts"] if party_source else None,
        "kiwame_return": [{"name": k["name"], "finished_at": k["finished_at"]}
                          for k in snap["kiwame_return"] if k["finished_at"]],
        "kiwame_observed_at": observed("/home/leave"),
        "forge_slots": [{"slot_no": f["slot_no"], "finished_at": f["finished_at"]}
                        for f in snap["forge_slots"] if f["finished_at"]],
        "forge_observed_at": observed("/home/situation"),
        "resources": resources,
        "resources_observed_at": observed("/home") if resources else None,
        "repair": repair,
        "repair_observed_at": observed("/home/situation"),
        "duty": duty,
        "duty_observed_at": observed("/home") if duty else None,
        "event_points": event_points,
        "event_points_observed_at": observed("/sally"),
    }


def save_home_situation(events: list[dict], path: Path | str) -> dict | None:
    """Atomically replace the small, credential-free homepage snapshot."""
    situation = build_home_situation(events)
    if situation is None:
        return None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not situation.get("parties_observed_at"):
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            situation["parties"] = previous.get("parties", [])
            situation["parties_observed_at"] = previous.get("parties_observed_at")
        except (OSError, ValueError, AttributeError):
            pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(situation, ensure_ascii=False), encoding="utf-8")
    if path.exists():
        import shutil
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    temporary.replace(path)
    return situation


# ---------------------------------------------------------------- ledger

# 账房八资源 ← 日志字段映射（resource 块 + currency 块）。
# bill=委托符已于 2026-09-28 校准：日志链条 787+1(签到)+3(任务)=791，
# 与游戏界面委托符数字分毫不差。
LEDGER_RESOURCE_MAP = {
    "charcoal": "木炭", "steel": "玉钢", "coolant": "冷却材",
    "file": "砥石", "bill": "委托符",
}
LEDGER_CURRENCY_MAP = {"money": "小判"}
# 甲州金 = currency.point + point_free（付费+免费合并，和游戏界面显示一致）

# consumable_id → 道具名（2026-09-28 CU 逐页干净截图 + 日志库存数量
# 双向对上锤死；长截图有拼接重影，全部以 CU 单帧为准）。
# 注：日志里值为 6 的恰有三槽，UI 里值为 6 的也恰有三件
# （菊碎片/经验符·中/内番符），排除法对上；其中 #10009=经验符·中
# 是排除法推断（置信略低于数量唯一锤死的），将来有出入先查它。
# 全部锤死，无平局组。锤法（2026-09-28 晚前后日志 diff 实验）：
#   #6003=碎片结合剂：老大买一个，1→2；#6109=双鹤图碎片：排除法；
#   #6113=狮子碎片：打异去爆一个，2→3；
#   #6105=南蛮胴具足碎片：打异去又爆一个，2→3；#6103=狮子螺钿鞍：排除法。
ITEM_NAMES = {
    "3155": "御守·桃",  # 2026-10-03 玩家确认；普/极压切各装备一个
    "1": "御守", "2": "御守·极",
    "3": "仙人团子", "4": "御札·富士", "5": "御札·松",
    "6": "御札·竹", "7": "御札·梅",
    "8": "加速符", "9": "修行召回鸽",
    "13": "小判箱·小", "14": "小判箱·中", "15": "小判箱·大",
    "17": "幕内便当", "18": "一套纸笔", "19": "修行衣装",
    "20": "修行道具", "21": "远征召回鸽", "22": "兵粮丸",
    "24": "苏言机", "25": "笛", "26": "琴", "27": "三味线",
    "28": "太鼓", "29": "铃", "37": "一口团子",
    "60": "福豆", "68": "堆肥", "112": "制衣券", "117": "御祝重便当",
    "1001": "根兵糖·中", "1002": "根兵糖·上",
    "4205": "栗", "4206": "天竺牡丹",
    "6005": "归城提灯五", "6107": "锷·月下梅树透图碎片",
    "6001": "异去探索道具", "6003": "碎片结合剂", "6109": "锷·双鹤图碎片",
    "6111": "三所物·菊碎片", "6113": "三所物·狮子碎片",
    "6103": "狮子螺钿鞍碎片", "6105": "南蛮胴具足碎片",
    "10001": "仙人团子·小", "10002": "幕内便当·小",
    "10005": "内番符", "10009": "经验符·中", "10019": "远征筹备手册",
    "20075": "暖心福袋",
}

_ENDPOINT_LABEL = {
    "/battle/alloutbattle": "联队战奖励",
    "/conquest/complete": "远征完成", "/conquest/start": "远征派遣",
    "/forge/startmultiple": "锻刀开炉", "/forge/complete": "锻刀完成",
    "/forge/completemultiple": "锻刀完成", "/forge/fastmultiple": "锻刀加速",
    "/mission/rewards": "任务奖励", "/receive/get": "收信箱",
    "/composition/compose": "合成", "/composition/union": "习合",
    "/duty/complete": "内番完成", "/home/back": "修行归来",
    "/monthcard/salary": "月卡俸禄", "/sign/info": "签到", "/sign": "签到",
    "/sally/recovercost": "补充活动手形",
    "/sally/forward": "出阵资源奖励",
    "/shop/buy": "万屋购买", "/sword/dismantle_many": "刀解",
    "/sally/parallelpastsally": "异去出阵",
    "/sally/parallelpastrecovercost": "异去恢复探索次数",
    "/artifact/buybindingagent": "购买碎片结合剂",
}

_LEDGER_READ_ENDPOINTS = {
    "/home", "/home/info", "/home/situation", "/home/get_all_activity",
    "/home/number_info", "/home/notice", "/home/test", "/party/list",
    "/mission/index", "/sally", "/push", "/keepalive", "/rolling/index",
    "/enter", "/platformmobile/login", "/login/start", "/treasurebox/check_open",
    "/party/setsword", "/party/set_preset", "/composition/list",
    "/forge", "/repair", "/conquest", "/practice", "/duty",
    "/receive/list", "/shop/list", "/notice/index", "/user/profile",
    "/user/param", "/album/list", "/sign/info",
}


def ledger_change_source(endpoint: str | None, requests: list[dict],
                         detail: str | None = None) -> dict:
    """余额准确不代表来源明确；多个动作间的净差不能归给第一个请求。"""
    actions = list(dict.fromkeys(r["endpoint"] for r in requests
                                if r.get("endpoint") not in _LEDGER_READ_ENDPOINTS))
    if endpoint in _ENDPOINT_LABEL and endpoint not in actions:
        actions.append(endpoint)
    if len(actions) == 1 and actions[0] in _ENDPOINT_LABEL:
        action = actions[0]
        return {"source_endpoint": action, "attribution": "confirmed",
                "via": [detail if action == endpoint and detail else _ENDPOINT_LABEL[action]]}
    return {"source_endpoint": None, "attribution": "inferred",
            "via": [actions[0].lstrip("/")] if len(actions) == 1 else ["来源待确认"],
            "candidate_endpoints": actions}


_LEDGER_ENDPOINT_CATEGORIES = {
    "conquest": "expedition", "forge": "forge", "repair": "repair",
    "mission/rewards": "task_rewards", "receive/get": "inbox",
    "battle/alloutbattle": "raid", "sally/parallelpastsally": "yosari", "sally/parallelpastrecovercost": "yosari",
    "artifact/buybindingagent": "artifact", "monthcard/salary": "salary",
    "sign/info": "signin", "sign": "signin",
    "sally/recovercost": "ticket", "sally/forward": "sortie", "shop/buy": "shop",
    "sword/dismantle_many": "dismantle", "composition/compose": "composition",
    "composition/union": "union", "duty/complete": "duty", "home/back": "training",
}


def translate_ledger_source(source: str, note: str) -> tuple[str, str]:
    """翻译游戏记录；旧记录按已保存的动作细节修正分类，不改原始数据库。"""
    endpoint = source.removeprefix("youzu_log.")
    # Older writers kept a single explicit endpoint in the note while marking
    # the source unknown. Recover that evidence without guessing mixed changes.
    if endpoint == "unknown":
        raw_action = note.split(" ", 1)[0]
        if "/" + raw_action in _ENDPOINT_LABEL:
            endpoint = raw_action
    if note.startswith("签到 ") and re.search(r" -\d+$", note):
        return "unknown.youzu_log", "来源待确认"
    if note.startswith("来源待确认"):
        return "unknown.youzu_log", "来源待确认"
    # 老写入器误取了第一条翻页请求，但动作标签仍然保留在 note 中。
    labels = {label: path.lstrip("/") for path, label in _ENDPOINT_LABEL.items()}
    if note.startswith("远征完成·"):
        endpoint = "conquest/complete"
    else:
        candidates = {path for label, path in labels.items() if note.startswith(label + " ")}
        if len(candidates) == 1:
            candidate = candidates.pop()
            original_category = (_LEDGER_ENDPOINT_CATEGORIES.get(endpoint)
                                 or _LEDGER_ENDPOINT_CATEGORIES.get(endpoint.split("/")[0]))
            candidate_category = (_LEDGER_ENDPOINT_CATEGORIES.get(candidate)
                                  or _LEDGER_ENDPOINT_CATEGORIES.get(candidate.split("/")[0]))
            if original_category and candidate_category != original_category:
                return "unknown.youzu_log", "来源待确认"
            endpoint = candidate
        elif "、" in note.split(" ")[0]:
            return "unknown.youzu_log", "来源待确认"
    category = _LEDGER_ENDPOINT_CATEGORIES.get(endpoint)
    if not category:
        category = _LEDGER_ENDPOINT_CATEGORIES.get(endpoint.split("/")[0])
    label = _ENDPOINT_LABEL.get("/" + endpoint)
    if label and note.startswith(endpoint + " "):
        note = label + note[len(endpoint):]
    if not category:
        return "unknown.youzu_log", "来源待确认" if not note.startswith("来源待确认") else note
    return f"{category}.youzu_log.{endpoint}", note

_PARTY_NO_CN = {1: "一队", 2: "二队", 3: "三队", 4: "四队", 5: "五队"}


def _expedition_map_label(field_id, scheme: str = "sequential") -> str:
    """field_id → 地图编号+名字（如 "B1 公武合体运动"）。

    国服 field_id 口径（2026-09-28 拿真实报文锤死）：
      - sequential（默认，唯一实测在用的）：/conquest 的 summary、
        /conquest/start 请求、/conquest/complete 响应顶层全是它——
        小图按章连排（A1-A4=1-4、B1=5……E4=20）。
      - era_slot（十位=era 个位=slot）：响应里另有一个 conquest
        子对象 field_id 恒为 "21"——B1 和 A1 的结算里它都是 21，
        与远征目的地无关（疑似界面停留页之类的粘性状态），
        不能拿来当地图。留这个 scheme 仅为解读该字段。
    对不上就老实显示 field_id 原值。
    """
    fid = _int(field_id, -1)
    if scheme == "era_slot":
        era, slot = fid // 10, fid % 10
    else:
        era, slot = (fid - 1) // 4 + 1, (fid - 1) % 4 + 1
    if not (1 <= era <= 5 and 1 <= slot <= 4):
        return f"field#{field_id}"
    code = f"{'ABCDE'[era - 1]}{slot}"
    try:
        from .expedition_planner import load_maps
        name = (load_maps().get(code) or {}).get("name") or ""
    except Exception:
        name = ""
    return f"{code} {name}".strip()


def _conquest_detail_label(endpoint: str, payload) -> str | None:
    """远征相关报文 → 带部队和地图的细分标签（从报文原文取，不是猜）。

    complete 响应顶层、start 请求的 field_id 都是 sequential 连排口径
    （2026-09-28 实测：A1 start 请求 field_id=1，B1 complete 顶层
    field_id=5）。
    """
    if not isinstance(payload, dict):
        return None
    party_no = _int(payload.get("party_no"), 0)
    field_id = payload.get("field_id")
    if not party_no or field_id in (None, ""):
        return None
    who = _PARTY_NO_CN.get(party_no, f"{party_no}队")
    where = _expedition_map_label(field_id)
    if endpoint == "/conquest/complete":
        return f"远征完成·{who}·{where}"
    if endpoint == "/conquest/start":
        return f"远征派遣·{who}·{where}"
    return None


def _event_epoch(ev: dict) -> float | None:
    """事件时间戳：优先响应体里的服务器 now_time（时区安全），
    退而求其次用日志行时间（设备本地时间，按 +08:00 解释）。"""
    payload = ev.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("now_time"),
                                                (int, float)):
        return float(payload["now_time"])
    ts = ev.get("ts")
    if ts:
        try:
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=8))).timestamp()
        except ValueError:
            return None
    return None


def _reading_from_payload(payload) -> dict | None:
    """从响应体提取余额读数 {资源名: 数量}；没带任何读数返回 None。

    除八资源外还收两类（2026-09-28 实测）：
      - 活动点数：顶层 point 块 {活动id: 点数}（/sally 每次都带）；
      - 道具库存：item 为 **dict** 时是 consumable 全量库存
        （/home/leave、/conquest/complete 都带）；item 为 list 时是
        奖励清单（mission/rewards），那是增量不是读数，跳过。
        道具名走 ITEM_NAMES（2026-09-28 CU 逐页校准）；没对上号的
        平局组保持「道具#N」原样，不硬猜。
    """
    if not isinstance(payload, dict):
        return None
    reading = {}
    for field, name in LEDGER_RESOURCE_MAP.items():
        value = (payload.get("resource") or {}).get(field)
        if isinstance(value, (int, float)):
            reading[name] = int(value)
    currency = payload.get("currency") or {}
    for field, name in LEDGER_CURRENCY_MAP.items():
        value = currency.get(field)
        if isinstance(value, (int, float)) or (isinstance(value, str)
                                               and value.isdigit()):
            reading[name] = int(value)
    point = _int(currency.get("point"), None)
    point_free = _int(currency.get("point_free"), None)
    if point is not None or point_free is not None:
        reading["甲州金"] = (point or 0) + (point_free or 0)
    event_points = payload.get("point")
    if isinstance(event_points, dict):
        for event_id, pts in event_points.items():
            value = _int(pts, None)
            if value is not None:
                reading[f"活动点数·{event_id}"] = value
    items = payload.get("item")
    if isinstance(items, dict):
        for entry in items.values():
            if isinstance(entry, dict) and "consumable_id" in entry:
                num = _int(entry.get("num"), None)
                if num is not None:
                    cid = str(entry["consumable_id"])
                    reading[ITEM_NAMES.get(cid, f"道具#{cid}")] = num
    # Real ten-forge acceleration traces identify item 8: 390 -> 380 -> 370.
    if str(payload.get("assist_item_id")) == "8":
        value = _int(payload.get("assist_item_num"), None)
        if value is not None:
            reading["加速符"] = value
    return reading or None


def _payload_resource_rewards(payload) -> dict[str, int]:
    """Only decode calibrated resource IDs from explicit reward lists."""
    names = {"1": "委托符", "2": "木炭", "3": "玉钢", "4": "冷却材", "5": "砥石"}
    rewards = {}
    if not isinstance(payload, dict):
        return rewards
    for key in ("reward", "item"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("item_type"))
            if kind == "4":
                name = "小判"
            elif kind == "5":
                name = names.get(str(entry.get("item_id")))
            elif kind == "1" and str(entry.get("item_id")) == "8":
                name = "加速符"
            else:
                name = None
            amount = _int(entry.get("item_num"))
            if name and amount > 0:
                rewards[name] = rewards.get(name, 0) + amount
    return rewards


def build_ledger(events: list[dict]) -> dict:
    """从事件流提取账本：余额观察链 + 逐笔归因收支。

    原理：每个带资源块的响应是一次精确读数；相邻读数间同一资源的差值，
    归因给夹在中间的那些 C->S 请求的玩法。全部来自服务器响应原文，
    奖励清单优先作为明确收据；余额差只核对剩余变化，混合净差保留待确认。

    注意响应是稀疏的（比如 /sally 的 currency 只带 money）：差值只在
    「这次读到了、以前也读到过」的资源上计算，缺键不等于归零。
    """
    observations: list[dict] = []
    changes: list[dict] = []
    last_known: dict[str, int] = {}
    pending_requests: list[dict] = []  # 当前响应前的请求
    resource_requests: dict[str, list[dict]] = {}
    inbox_entries: dict[str, dict] = {}
    assets = []

    for ev in events:
        if ev["direction"] == "C->S":
            if ev["endpoint"] and ev["endpoint"] not in ("/keepalive",):
                pending_requests.append(ev)
                for requests in resource_requests.values():
                    requests.append(ev)
            continue
        if ev["direction"] != "S->C":
            continue
        if (ev.get("status", 200) != 200 or not isinstance(ev.get("payload"), dict)
                or str(ev["payload"].get("status", 0)) != "0"):
            for requests in [pending_requests, *resource_requests.values()]:
                requests[:] = [r for r in requests if r["endpoint"] != ev["endpoint"]]
            continue
        payload = ev["payload"]
        if ev["endpoint"] == "/receive/list" and isinstance(payload.get("receive"), dict):
            for entry in payload["receive"].values():
                if isinstance(entry, dict) and entry.get("serial_id") is not None:
                    inbox_entries[str(entry["serial_id"])] = entry
        if ev["endpoint"] in ("/party/list", "/sally"):
            fields = {"equip": ("serial_id", "equip_id", "soldier"),
                      "artifact": ("serial_id", "artifact_id", "level", "usage_score", "equip_sword_serial_id"),
                      "sword": ("serial_id", "sword_id", "equip_serial_id1", "equip_serial_id2", "equip_serial_id3", "horse_serial_id", "item_id", "artifact_serial_id1", "artifact_serial_id2")}
            observed = {name: [{k: row[k] for k in keys if k in row} for row in payload[name].values() if isinstance(row, dict)]
                        for name, keys in fields.items() if isinstance(payload.get(name), dict)}
            if observed:
                assets.append({"ts": _event_epoch(ev), "source": "youzu_log", **observed})
        reading = _reading_from_payload(ev.get("payload"))
        reading = reading or {}
        # These responses contain the complete held consumable inventory;
        # absence there means zero, unlike missing fields in sparse responses.
        if ev["endpoint"] in ("/login/start", "/sally", "/shop/list") and isinstance(payload.get("item"), dict):
            for name in ITEM_NAMES.values():
                reading.setdefault(name, 0)
        ts = _event_epoch(ev)

        # 远征细分标签：complete 响应原文自带 party_no+field_id；start 的
        # 响应没有，退而取它自己的 C->S 请求原文（两处都是连排口径）。
        # 哪支队哪张图直接细分出来（视觉识别最难啃的点，这里白拿）
        detail = _conquest_detail_label(ev["endpoint"], ev.get("payload"))
        if detail is None and ev["endpoint"] == "/conquest/start":
            req = next((r for r in reversed(pending_requests)
                        if r["endpoint"] == "/conquest/start"), None)
            if req:
                detail = _conquest_detail_label(ev["endpoint"],
                                                req.get("payload"))

        # Explicit receipts own their amounts; balance differences only explain
        # the remainder. Do not manufacture a balance for a receipt without a read.
        rewards = _payload_resource_rewards(payload)
        receipt_evidence = "client_reward_list"
        if ev["endpoint"] == "/receive/get":
            # Only items the successful response explicitly confirms as received.
            ids = payload.get("serial_ids")
            if isinstance(ids, str):
                ids = ids.split(",")
            if isinstance(ids, list):
                claimed = [inbox_entries.pop(str(i)) for i in ids if str(i) in inbox_entries]
                if not rewards:
                    rewards = _payload_resource_rewards({"item": claimed})
                    receipt_evidence = "client_inbox_receipt"
        if ev["endpoint"] == "/forge/startmultiple":
            requests = [r for r in pending_requests if r["endpoint"] == ev["endpoint"]]
            accepted = payload.get("multiple")
            if len(requests) == 1 and isinstance(accepted, list) and accepted:
                recipe = requests[0].get("payload") or {}
                for field, name in LEDGER_RESOURCE_MAP.items():
                    if field == "bill":
                        continue  # Discounts are not inferable from recipe quantities.
                    amount = _int(recipe.get(field), None)
                    if amount is not None and amount > 0:
                        rewards[name] = -amount * len(accepted)
                receipt_evidence = "client_forge_recipe"

        for name, amount in rewards.items():
            old = last_known.get(name)
            new = reading.get(name)
            changes.append({"ts": ts, "delta": {name: amount},
                "source_endpoint": ev["endpoint"], "attribution": "confirmed",
                "before": {name: old if new is not None and old is not None and new - old == amount else None},
                "after": {name: new if new is not None and old is not None and new - old == amount else None},
                "via": [detail or _ENDPOINT_LABEL.get(ev["endpoint"], (ev["endpoint"] or "?").lstrip("/"))],
                "via_endpoints": [], "evidence": receipt_evidence})
            if new is None and old is not None:
                last_known[name] = old + amount

        delta, before, after = {}, {}, {}
        for name, value in reading.items():
            if name in last_known:
                residual = value - last_known[name] - rewards.get(name, 0)
                if residual:
                    delta[name] = residual
                    # A mixed residual is not itself a direct balance pair.
                    if name not in rewards:
                        before[name] = last_known[name]
                        after[name] = value
        for name, value in reading.items():
            last_known[name] = value

        # Emit only fields actually returned now, never a carried or computed balance.
        if reading:
            observations.append({"ts": ts, "endpoint": ev["endpoint"],
                                 "reading": dict(reading)})
        grouped = {}
        for name, amount in delta.items():
            meta = ledger_change_source(ev["endpoint"], resource_requests.get(name, []), detail)
            # Daily Yosari tickets can be observed after several battle requests.
            # Those steps are one gameplay flow, not competing resource sources.
            actions = set(meta.get("candidate_endpoints", []))
            yosari_steps = {"/sally/parallelpastsally", "/sally/parallelpaststartup",
                            "/sally/parallelpastforward", "/battle/battle"}
            if (name == "归城提灯五" and amount < 0
                    and "/sally/parallelpastsally" in actions
                    and actions <= yosari_steps):
                meta = {"source_endpoint": "/sally/parallelpastsally",
                        "attribution": "inferred", "via": ["异去门票消耗"],
                        "evidence": "client_ticket_balance_with_yosari_flow"}

            raid_steps = {"/sally/eventsally", "/battle/alloutbattle", "/sally/recovercost", "/notice/index"}
            if (name == "活动点数·10031" and amount > 0
                    and "/battle/alloutbattle" in actions and actions <= raid_steps):
                meta = {"source_endpoint": "/battle/alloutbattle", "attribution": "inferred",
                        "via": ["联队战奖励"], "evidence": "client_event_points_with_raid_flow"}
            if name in rewards:
                meta = {"source_endpoint": None, "attribution": "inferred",
                        "via": ["来源待确认"], "candidate_endpoints": meta.get("candidate_endpoints", [])}
            key = (meta["source_endpoint"], tuple(meta["via"]), meta["attribution"])
            ch = grouped.setdefault(key, {"ts": ts, "delta": {}, "before": {},
                "after": {}, "via_endpoints": [r["endpoint"] for r in resource_requests.get(name, [])], **meta})
            ch["delta"][name] = amount
            ch["before"][name] = before.get(name)
            ch["after"][name] = after.get(name)
        changes.extend(grouped.values())
        for name in reading:
            resource_requests[name] = []

        # 远征经验账：result 块有审神者经验（exp 是发奖后的总值，
        # before = exp - user_exp 反推）；sword 块每刀带 get_exp，
        # 合计记一笔（逐刀太碎，要看刀的去快照）
        if ev["endpoint"] == "/conquest/complete" \
                and isinstance(ev.get("payload"), dict):
            result = ev["payload"].get("result") or {}
            user_exp = _int(result.get("user_exp"))
            if user_exp:
                exp_after = _int(result.get("exp"), None)
                changes.append({
                    "ts": ts,
                    "delta": {"审神者经验": user_exp},
                    "source_endpoint": ev["endpoint"], "attribution": "confirmed",
                    "before": {"审神者经验": (exp_after - user_exp)
                               if exp_after is not None else None},
                    "after": {"审神者经验": exp_after},
                    "via": [detail or "远征完成"],
                    "via_endpoints": [r["endpoint"]
                                      for r in pending_requests],
                })
            sword_exp = sum(_int(s.get("get_exp"))
                            for s in (ev["payload"].get("sword")
                                      or {}).values()
                            if isinstance(s, dict))
            if sword_exp:
                changes.append({
                    "ts": ts,
                    "delta": {"刀剑经验": sword_exp},
                    "source_endpoint": ev["endpoint"], "attribution": "confirmed",
                    "before": {}, "after": {},
                    "via": [detail or "远征完成"],
                    "via_endpoints": [r["endpoint"]
                                      for r in pending_requests],
                })
        pending_requests = []

    return {"observations": observations, "changes": changes, "assets": assets}


def format_ledger(ledger: dict) -> str:
    lines = [f"账本预览：{len(ledger['observations'])} 次读数，"
             f"{len(ledger['changes'])} 笔收支"]
    for ch in ledger["changes"]:
        when = (datetime.fromtimestamp(ch["ts"]).strftime("%m-%d %H:%M:%S")
                if ch["ts"] else "?")
        parts = " ".join(f"{k}{v:+d}" for k, v in ch["delta"].items())
        lines.append(f"  [{when}] {parts}  ← {'、'.join(ch['via'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- ledger write

_LEDGER_SCRIPT = "youzu_log"


def _ledger_state_path() -> Path:
    from .runtime_paths import STATE_DIR
    return STATE_DIR / "youzu_ledger_state.json"


def _repair_receipt_sources(store, ledger: dict, last_ts: float) -> int:
    """Correct uniquely matching old receipts; preserve amounts and event identities."""
    conn = store._conn()
    updates = {}
    for change in ledger.get("changes", []):
        ts = change.get("ts")
        endpoint = change.get("source_endpoint")
        if not ts or ts > last_ts or not endpoint:
            continue
        for name, delta in change.get("delta", {}).items():
            matches = []
            for row in conn.execute("SELECT id, payload FROM events WHERE ts = ? AND script = ? AND event_type = 'resource.change'", (ts, _LEDGER_SCRIPT)):
                old = json.loads(row["payload"])
                old_name = old.get("resource")
                if old_name == "加速符·极":
                    old_name = "加速符"
                if old_name == name and old.get("delta") == delta:
                    matches.append((row["id"], old))
            if len(matches) != 1:
                continue
            event_id, old = matches[0]
            if old.get("source") != "youzu_log.unknown":
                continue
            old.update(source="youzu_log." + endpoint.lstrip("/"),
                       note="、".join(change["via"]) + f" {name} {delta:+d}",
                       attribution=change.get("attribution", "inferred"),
                       evidence=change.get("evidence", "client_reward_list"))
            updates[event_id] = json.dumps(old, ensure_ascii=False)
    if updates:
        # Back up before correcting user-local history; no records are deleted.
        import sqlite3
        db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{db_path.name}.receipt-sources-{time.time_ns()}.bak"
        with sqlite3.connect(backup_path) as backup:
            conn.backup(backup)
        with conn:
            conn.executemany("UPDATE events SET payload = ? WHERE id = ?",
                             [(payload, event_id) for event_id, payload in updates.items()])
    return len(updates)


def write_ledger(store, ledger: dict,
                 state_path: Path | str | None = None) -> dict:
    """把账本写进 TelemetryStore（账房页面直接可见）。

    - 余额观察 → inventory.captured（source=youzu_log，账房观察链，
      不会混进手动家底列表——那边只认 manual_entry/manual_import）
    - 收支 → 每资源一条 resource.change（before/after/delta/source/note
      数量来自服务器原文；动作明确才确认来源）
    - 幂等：状态文件记 last_ts，已写过的部分重拉不重记
      （日志每局重写，跨局的时间戳天然递增，不会撞车）
    """
    state_path = Path(state_path) if state_path else _ledger_state_path()
    last_ts = 0.0
    try:
        last_ts = float(json.loads(state_path.read_text(
            encoding="utf-8")).get("last_ts") or 0)
    except (OSError, ValueError):
        pass

    conn = store._conn()
    repaired_sources = _repair_receipt_sources(store, ledger, last_ts)
    written_obs = written_changes = 0
    max_ts = last_ts
    for obs in ledger["observations"]:
        ts = obs.get("ts")
        if not ts or ts <= last_ts:
            continue
        payload = {
            "captured_at": datetime.fromtimestamp(ts).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "source": "youzu_log",
            "resources": obs["reading"],
        }
        conn.execute(
            "INSERT INTO events(ts, run_id, script, event_type, payload) "
            "VALUES (?, NULL, ?, 'inventory.captured', ?)",
            (ts, _LEDGER_SCRIPT, json.dumps(payload, ensure_ascii=False)))
        written_obs += 1
        max_ts = max(max_ts, ts)
    for ch in ledger["changes"]:
        ts = ch.get("ts")
        if not ts or ts <= last_ts:
            continue
        via = "、".join(ch["via"])
        for name, delta in ch["delta"].items():
            payload = {
                "resource": name, "delta": delta,
                "before": ch["before"].get(name), "after": ch["after"].get(name),
                "source": f"youzu_log.{(ch.get('source_endpoint') or 'unknown').lstrip('/')}",
                "candidate_endpoints": ch.get("candidate_endpoints", []),
                "note": f"{via} {name} {delta:+d}",
                "attribution": ch.get("attribution", "inferred"),
                "evidence": ch.get("evidence", "client_balance_difference"),
            }
            conn.execute(
                "INSERT INTO events(ts, run_id, script, event_type, payload) "
                "VALUES (?, NULL, ?, 'resource.change', ?)",
                (ts, _LEDGER_SCRIPT, json.dumps(payload, ensure_ascii=False)))
            written_changes += 1
        max_ts = max(max_ts, ts)
    for snapshot in ledger.get("assets", []):
        ts = snapshot.get("ts")
        if ts:
            encoded = json.dumps(snapshot, ensure_ascii=False)
            conn.execute("INSERT INTO events(ts, run_id, script, event_type, payload) SELECT ?, NULL, ?, 'game_assets.captured', ? WHERE NOT EXISTS (SELECT 1 FROM events WHERE ts = ? AND script = ? AND event_type = 'game_assets.captured' AND payload = ?)",
                         (ts, _LEDGER_SCRIPT, encoded, ts, _LEDGER_SCRIPT, encoded))
            max_ts = max(max_ts, ts)
    conn.commit()

    if max_ts > last_ts:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"last_ts": max_ts}),
                              encoding="utf-8")
    return {"sources_repaired": repaired_sources, "observations_written": written_obs,
            "changes_written": written_changes, "last_ts": max_ts}


# ---------------------------------------------------------------- training snapshot

# 练度快照只收日志原文里已有的数字：等级/累积经验/乱舞/内番已喂数值。
# 刀名一律不入库（展示层拿 sword_id 走 sword_db 解析）。
_TRAINING_UP_FIELDS = ("hp_up", "atk_up", "def_up", "mobile_up",
                       "back_up", "scout_up", "hide_up")


def build_training_snapshot(events: list[dict]) -> dict | None:
    """最新全量刀池 → 练度快照 {"ts", "payload"}；没有刀池返回 None。

    ts 取刀池事件自身的 now_time/日志行时间——数据时间绝不拿 pull 时间冒充。
    """
    pool, pool_ev = _latest_sword_pool(events)
    if pool is None:
        return None
    ts = _event_epoch(pool_ev)
    if ts is None:
        return None
    swords = []
    for raw in pool.values():
        if not isinstance(raw, dict):
            continue
        serial = _int(raw.get("serial_id"))
        if serial <= 0:
            continue
        entry = {"serial_id": serial, "sword_id": _int(raw.get("sword_id")),
                 "level": _int(raw.get("level")), "exp": _int(raw.get("exp")),
                 "ranbu_level": _int(raw.get("ranbu_level")),
                 "ranbu_exp": _int(raw.get("ranbu_exp"))}
        for field in _TRAINING_UP_FIELDS:
            entry[field] = _int(raw.get(field), None)
        swords.append(entry)
    swords.sort(key=lambda row: row["serial_id"])
    return {"ts": ts, "payload": {
        "captured_at": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "youzu_log", "swords": swords}}


def save_training_snapshot(store, events: list[dict]) -> dict:
    """练度快照入库为 training.captured；同 ts 同 payload 不重写。

    仿 game_assets.captured 的 INSERT ... WHERE NOT EXISTS 幂等写法：
    重复 pull 同一局日志不会堆重复快照。
    """
    snap = build_training_snapshot(events)
    if snap is None:
        return {"written": 0}
    encoded = json.dumps(snap["payload"], ensure_ascii=False)
    cursor = store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) "
        "SELECT ?, NULL, ?, 'training.captured', ? WHERE NOT EXISTS "
        "(SELECT 1 FROM events WHERE ts = ? AND script = ? "
        "AND event_type = 'training.captured' AND payload = ?)",
        (snap["ts"], _LEDGER_SCRIPT, encoded, snap["ts"], _LEDGER_SCRIPT, encoded))
    store._conn().commit()
    return {"written": cursor.rowcount}


# ---------------------------------------------------------------- display

def format_summary(snap: dict) -> str:
    p = snap["profile"]
    r = snap["resources"]
    lines = [
        f"本丸快照（数据时间 {snap.get('server_time') or '?'}，"
        f"生成于 {snap['generated_at']}）",
        f"  审神者 Lv.{p['level']}  近侍：{p['secretary'] or '?'}",
        f"  资源：木炭 {r['charcoal']} / 玉钢 {r['steel']} / "
        f"冷却材 {r['coolant']} / 砥石 {r['whetstone']}",
        f"        {LEDGER_RESOURCE_MAP['bill']} {r['bill']} / 小判 {r['koban']}",
        f"  刀剑：{snap['sword_count']} / {snap['sword_capacity'] or '?'} 振",
    ]
    for party in snap["parties"]:
        mem = "、".join(f"{m['name']}Lv{m['level']}"
                        + (f"(伤{m['hp']}/{m['hp_max']})"
                           if m['hp'] < m['hp_max'] else "")
                        for m in party["members"]) or "（空）"
        suffix = ""
        if party["status"] == 2 and party.get("finished_at"):
            done = (snap.get("server_time") or "") >= party["finished_at"]
            suffix = ("，已到家待收" if done
                      else f"，归队 {party['finished_at'][5:16]}")
        lines.append(f"  第{party['party_no']}部队 [{party['status_label']}] "
                     f"{party['party_name']}{suffix}：{mem}")
    busy = [f"槽{f['slot_no']}→{f['finished_at']}" for f in snap["forge_slots"]
            if f.get("finished_at")]
    if busy:
        lines.append("  锻刀槽：" + "，".join(busy))
    if snap.get("event_points"):
        pts = "，".join(f"活动{k}:{v}" for k, v in snap["event_points"].items())
        lines.append(f"  活动点数：{pts}")
    if snap.get("season", {}).get("end_at"):
        lines.append(f"  赛季 {snap['season']['season_id']}  "
                     f"截止 {snap['season']['end_at']}")
    for k in snap.get("kiwame_return") or []:
        if k.get("finished_at"):
            lines.append(f"  修行中：{k.get('name') or '刀#' + str(k['serial_id'])}"
                         f"  归来 {k['finished_at'][5:16]}")
    for e in snap.get("events_calendar") or []:
        if e.get("end_at"):
            lines.append(f"  活动 {e['event_id']}：{e.get('start_at') or '?'} "
                         f"~ {e['end_at']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- cli

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="国服 HttpRequestCollect 日志 → 本丸快照")
    ap.add_argument("--file", help="解析本地日志文件（不给则从模拟器 pull）")
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--address", default=DEFAULT_ADDRESS)
    ap.add_argument("--out", help="快照 JSON 输出路径（默认 .tmp/youzu/snapshot.json）")
    ap.add_argument("--no-swords", action="store_true", help="快照不带全刀帐明细")
    ap.add_argument("--ledger", action="store_true",
                    help="打印账本预览（余额观察链 + 逐笔归因收支）")
    ap.add_argument("--write-ledger", action="store_true",
                    help="把账本写进账房数据库（幂等，重复拉取不重记；"
                         "注意开发环境写的是 Maamaru-Dev 数据目录）")
    ap.add_argument("--keep-log", action="store_true",
                    help="保留 pull 下来的原始日志（默认解析完即焚："
                         "原档里有名字/user_code/session 凭证，不落盘为安）")
    args = ap.parse_args(argv)

    pulled = args.file is None
    src = Path(args.file) if args.file else pull_log(args.adb, args.address)
    print(f"[日志] {src} ({src.stat().st_size:,} 字节)")
    events = parse_events(src)
    meta = events[-1]["payload"]
    print(f"[解析] 事件 {meta['event_count']} 条，坏行 {meta['bad_lines']} 条")
    snap = build_snapshot(events, with_swords=not args.no_swords)
    out = Path(args.out) if args.out else Path(".tmp") / "youzu" / "snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[快照] {out}")
    if pulled and not args.keep_log:
        src.unlink()  # 阅后即焚（2026-09-28 老大亲批）；--file 传的别人的文件不碰
        print(f"[焚毁] {src.name} 已删，原始日志不留本地")
    print()
    print(format_summary(snap))
    if args.ledger:
        print()
        print(format_ledger(build_ledger(events)))
    if args.write_ledger:
        from .telemetry import TelemetryStore
        result = write_ledger(TelemetryStore(), build_ledger(events))
        from .sword_receipts import sync_receipts
        swords = sync_receipts(events)
        print(f"\n[账房] 入库：观察 {result['observations_written']} 条，"
              f"收支 {result['changes_written']} 条，"
              f"刀剑领取 {swords['written']} 条、补全 {swords['reconciled']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
