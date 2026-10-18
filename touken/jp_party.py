# -*- coding: utf-8 -*-
"""日服出阵前的队伍状态确认：随出阵导航被动读取游戏返回的队伍数据。

与账房同一手法、同一红线：浏览器级 CDP 连接只读订阅 Network 事件，
不注入脚本、不改请求、不向服务器多发任何东西。队伍数据来自游戏
进出阵菜单时自己拉取的响应（/sally 必带全量刀帐与编制），
解析成共用的 TeamSnapshot 契约，供出发前确认伤势与队伍状态。
"""
from __future__ import annotations

import base64
import json
import threading
import time

from . import jp_import, jp_listener, sword_db, youzu_log
from .team_state import TeamMemberSnapshot, TeamSnapshot

GAME_HOST_MARK = 'touken-ranbu.jp'
SALLY_PATH = '/sally'               # 进出阵菜单必刷新（全量刀帐 + 编制）
RESWEEP_SECONDS = 10.0              # 兜底重扫目标清单的间隔


class PartyWatchError(RuntimeError):
    """队伍数据通道连不上；文案面向玩家。"""


def _sword_name(sword_id) -> str | None:
    if type(sword_id) is not int:
        return None
    found = sword_db.find_by_id(sword_id)
    if not found:
        return None
    _key, entry = found
    return entry.get('name_zh') or entry.get('name')


def _sword_map(payload: dict) -> dict:
    sword = payload.get('sword')
    if not isinstance(sword, dict):
        return {}
    inner = sword.get('sword') if isinstance(sword.get('sword'), dict) else sword
    out = {}
    for key, entry in inner.items():
        if isinstance(entry, dict) and 'sword_id' in entry:
            out[str(entry.get('serial_id') or key)] = entry
    return out


def party_status(payload: dict, team_no: int) -> int | None:
    """队伍状态码（1=待命 2=远征中 3=出阵中）；没有这条队伍返回 None。"""
    party = payload.get('party')
    if not isinstance(party, dict):
        return None
    entry = party.get(str(team_no))
    if not isinstance(entry, dict):
        return None
    status = entry.get('status')
    return status if type(status) is int else None


def build_team_snapshot(payload: dict, team_no: int, *, now: float | None = None
                        ) -> TeamSnapshot | None:
    """把 /sally 响应转成队伍快照；编制缺员或成员数据不全返回 None。

    hp/hp_max 缺失的队员不算「认得」——验伤宁可不承认这支队，
    也不带盲区放行。"""
    now = time.time() if now is None else now
    party = payload.get('party')
    if not isinstance(party, dict):
        return None
    entry = party.get(str(team_no))
    if not isinstance(entry, dict):
        return None
    slots = entry.get('slot')
    if not isinstance(slots, dict):
        return None
    swords = _sword_map(payload)
    members = []
    for slot_key in sorted(slots, key=lambda k: int(k) if str(k).isdigit() else 99):
        slot = slots[slot_key]
        serial_id = slot.get('serial_id') if isinstance(slot, dict) else None
        if serial_id is None:
            continue
        record = swords.get(str(serial_id))
        if not record:
            return None
        hp, hp_max = record.get('hp'), record.get('hp_max')
        if type(hp) is not int or type(hp_max) is not int or hp_max <= 0:
            return None
        fatigue = record.get('fatigue')
        if type(fatigue) is not int:
            fatigue = None
        members.append(TeamMemberSnapshot(
            slot=int(slot_key),
            serial_id=str(serial_id),
            name=_sword_name(record.get('sword_id')),
            hp=hp, hp_max=hp_max,
            injury=youzu_log.injury_tier(hp, hp_max), injury_source='hp',
            hp_observed_at=now,
            fatigue=fatigue,
            fatigue_observed_at=now if fatigue is not None else None,
            equipment=tuple(record.get(f'equip_serial_id{i}') is not None
                            for i in (1, 2, 3)),
            equipment_observed_at=now))
    if not members:
        return None
    return TeamSnapshot(team_no=team_no, members=tuple(members),
                        observed_at=now)


class PartyWatch:
    """出阵期间随队的队伍数据通道（后台线程被动收包）。

    用法：with PartyWatch() as watch: ... watch.latest()
    latest() 返回 (捕获时间, /sally 响应 dict)；还没收到返回 None。"""

    def __init__(self):
        self._sock: jp_listener._CdpSocket | None = None
        self._session_id: str | None = None     # 游戏 iframe 的挂靠会话
        self._pending: dict[str, str] = {}      # requestId -> path
        self._latest: tuple[float, dict] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> 'PartyWatch':
        version = jp_listener._http_json('/json/version')
        ws_url = (version or {}).get('webSocketDebuggerUrl')
        if not ws_url:
            raise PartyWatchError('没能连上日服浏览器的数据通道，请从まあ丸打开游戏后重试')
        self._sock = jp_listener._CdpSocket(ws_url)
        try:
            self._sock.call('Target.setAutoAttach', {
                'autoAttach': True, 'waitForDebuggerOnStart': False,
                'flatten': True})
            self._attach_existing()
        except Exception:
            self._sock.close()
            self._sock = None
            raise PartyWatchError('没能连上日服游戏的数据通道，请刷新游戏页面后重试')
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name='jp-party-watch', daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock:
            sock.close()
        thread = self._thread
        if thread:
            thread.join(timeout=5)

    def __enter__(self) -> 'PartyWatch':
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def latest(self) -> tuple[float, dict] | None:
        with self._lock:
            return self._latest

    # ---- 后台 ----

    def _attach_existing(self) -> None:
        assert self._sock is not None
        targets = self._sock.call('Target.getTargets').get('targetInfos', [])
        for info in targets:
            url = info.get('url') or ''
            if info.get('type') == 'iframe' and GAME_HOST_MARK in url:
                self._attach(info['targetId'])
                return
        raise PartyWatchError('没找到日服游戏画面，请确认游戏已经打开')

    def _attach(self, target_id: str,
                session_id: str | None = None) -> None:
        assert self._sock is not None
        if session_id is None:
            session_id = self._sock.call('Target.attachToTarget', {
                'targetId': target_id, 'flatten': True})['sessionId']
        self._sock.call('Network.enable', session_id=session_id)
        self._session_id = session_id

    def _loop(self) -> None:
        sock = self._sock
        last_sweep = time.monotonic()
        while not self._stop.is_set():
            try:
                msg = sock.backlog.pop(0) if sock.backlog else sock.recv()
            except (ConnectionError, OSError):
                return
            if msg is None:
                if time.monotonic() - last_sweep > RESWEEP_SECONDS:
                    last_sweep = time.monotonic()
                    self._resweep()
                continue
            try:
                self._handle(msg)
            except Exception:
                continue  # 一条坏消息不掀桌子

    def _resweep(self) -> None:
        """自动挂靠通知实测会漏后出生的 iframe，定期补扫。"""
        if self._session_id or self._sock is None:
            return
        try:
            targets = self._sock.call('Target.getTargets').get('targetInfos', [])
            for info in targets:
                url = info.get('url') or ''
                if info.get('type') == 'iframe' and GAME_HOST_MARK in url:
                    self._attach(info['targetId'])
                    return
        except (RuntimeError, TimeoutError, ConnectionError, PartyWatchError):
            pass

    def _handle(self, msg: dict) -> None:
        method = msg.get('method', '')
        params = msg.get('params', {})
        if method == 'Target.attachedToTarget':
            info = params.get('targetInfo', {})
            url = info.get('url') or ''
            if info.get('type') == 'iframe' and GAME_HOST_MARK in url \
                    and self._session_id is None:
                self._attach(info['targetId'],
                             session_id=params.get('sessionId'))
            return
        if method == 'Target.detachedFromTarget':
            if params.get('sessionId') == self._session_id:
                self._session_id = None
                self._pending.clear()
            return
        if msg.get('sessionId') != self._session_id:
            return
        if method == 'Network.requestWillBeSent':
            url = params.get('request', {}).get('url', '')
            if GAME_HOST_MARK in url \
                    and jp_import.endpoint_path(url) == SALLY_PATH:
                self._pending[params['requestId']] = url
            return
        if method == 'Network.loadingFinished':
            if self._pending.pop(params.get('requestId'), None) is None:
                return
            result = self._sock.call('Network.getResponseBody',
                                     {'requestId': params['requestId']},
                                     session_id=self._session_id)
            raw = result.get('body', '')
            body = (base64.b64decode(raw) if result.get('base64Encoded')
                    else raw.encode('utf-8'))
            payload = json.loads(body)
            if isinstance(payload, dict) and isinstance(payload.get('party'), dict):
                with self._lock:
                    self._latest = (time.time(), payload)
