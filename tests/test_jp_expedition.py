# -*- coding: utf-8 -*-
"""日服远征手动派遣：假帧/假数据通道/假素材包的合成测试。

覆盖：正常派遣、重伤拦截、状态不明（刷新重试一次仍停）、证明失败、
画面路证明、数据隔离（记录只进日服数据目录）、同一份官方流程单经
run_flow 的调用、run_script 参数门面、缺素材如实报缺、安全点不漏国服坐标。

真机素材未标定：本测试的素材包是夹具（tiny PNG + 假坐标），真 jp 包
缺哪些引用由 test_jp_pack_expedition_assets_honestly_missing 钉住。
"""
import json
import time

import pytest
from PIL import Image

from touken import flow_engine, jp_expedition, jp_party, jp_sally, jp_vision
from touken.device_adapter import Point
from touken.runtime_paths import JP_DATA_DIR, RESOURCE_DIR, STATE_DIR
from touken.server_pack import ServerPack, UnknownReferenceError, load_server_pack

pytestmark = pytest.mark.skipif(
    not jp_vision.TEMPLATE_DIR.is_dir(), reason='日服模板目录不存在')

FLOW_PATH = RESOURCE_DIR / 'flows' / 'expedition.json'

ERA_PT = (200, 220)
MAP_PT = (300, 600)
TEAM_PT = (400, 160)
SKIP_PT = (1400, 1010)

TEAM_SELECT = 'jp/expedition/team-select.png'
TEAM_SCREEN = 'jp/expedition/team-screen.png'
DEPART = 'jp/expedition/depart.png'
CONFIRM = 'jp/expedition/confirm.png'
ACCEPT = 'jp/expedition/confirm-accept.png'


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(jp_vision, '_baseline_path', lambda: tmp_path / 'vision.json')
    monkeypatch.setattr(jp_vision, 'base_size', lambda: (1508, 1088))
    monkeypatch.setattr(jp_expedition.time, 'sleep', lambda s: None)
    monkeypatch.setattr(jp_expedition, 'PROOF_TIMEOUT_S', 0.05)
    monkeypatch.setattr(jp_expedition, 'SCROLL_WAIT_S', 0.0)
    # 会话级隔离目录跨测试共享：派遣记录文件每个测试清场，互不留账
    record = JP_DATA_DIR / 'state' / 'expeditions.json'
    if record.exists():
        record.unlink()
    yield
    if record.exists():
        record.unlink()


class FakeDevice:
    """JpGameDevice 形状：screenshot/click/template_match/exists/ocr +
    reload/relaxed_view/base_size。识别结果全部由剧本驱动。"""

    def __init__(self, tokens=()):
        self.clicks = []
        self.tokens = list(tokens)
        self.template_hits = {}
        self.frames = []
        self.last = object()          # 无剧本帧时也保证「有画面」
        self.reloads = 0
        self.base_size = (1508, 1088)

    def screenshot(self, force=False):
        if self.frames:
            self.last = self.frames.pop(0)
        return self.last

    def click(self, point):
        self.clicks.append((point.x, point.y))
        return True

    def template_match(self, template, roi=None, threshold=0.7):
        return self.template_hits.get(template)

    def exists(self, template, roi=None, threshold=0.7):
        # 必须经 template_match：序列设备靠这里改变画面状态
        return self.template_match(template, roi, threshold) is not None

    def ocr(self, expected, roi=None, match_mode='contains'):
        return None

    def reload(self):
        self.reloads += 1

    def relaxed_view(self):
        import contextlib
        return contextlib.nullcontext()


class FakeWatch:
    """假队伍数据通道。refetched=True 模拟派遣后游戏已重报（时间戳取当前）。"""

    def __init__(self, payload=None, age=0.0):
        self._payload = payload
        self._captured = None if payload is None else time.time() - age
        self.refetched = False

    def latest(self):
        if self._payload is None:
            return None
        captured = time.time() if self.refetched else self._captured
        return captured, self._payload


def make_payload(members, status=1, team_no=1):
    """合成 /sally 响应：members = [(slot, serial_id, sword_id, hp, hp_max)]"""
    slot = {str(i): {'serial_id': None} for i in range(1, 7)}
    sword = {}
    for slot_no, serial, sword_id, hp, hp_max in members:
        slot[str(slot_no)] = {'serial_id': serial}
        sword[str(serial)] = {'serial_id': serial, 'sword_id': sword_id,
                              'hp': hp, 'hp_max': hp_max, 'fatigue': 80}
    return {'party': {str(team_no): {'party_no': team_no, 'status': status,
                                     'slot': slot}},
            'sword': sword}


HEALTHY_TEAM = [(1, 101, 3, 56, 56), (2, 102, 5, 33, 33)]
HEAVY_TEAM = HEALTHY_TEAM + [(3, 103, 7, 10, 56)]    # 10/56 = 重伤
MAP_TOKENS = [('鳥羽・伏見', 0.9, 100, 100)]


def make_pack(tmp_path, points=None):
    """夹具素材包：tiny 真 PNG（过包的路径/存在性校验）+ 假坐标。"""
    root = tmp_path / 'resource'
    table = {}
    for logical in ('expedition/team-select', 'expedition/team-screen',
                    'expedition/depart', 'expedition/confirm',
                    'expedition/confirm-accept'):
        rel = f'jp/{logical}.png'
        path = root / 'image' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (8, 8), (200, 30, 30)).save(path)
        table[logical] = rel
    pts = {'expedition/era-1': list(ERA_PT), 'expedition/map-1': list(MAP_PT),
           'expedition/team-1': list(TEAM_PT), 'expedition/skip': list(SKIP_PT)}
    pts.update(points or {})
    return ServerPack(server='jp-test', base_size=(1508, 1088),
                      resource_dir=root, templates=table, ocr={}, rois={},
                      points=pts, navigation={'expedition/nav': '远征'})


def happy_device(tokens=MAP_TOKENS, cls=None):
    device = (cls or FakeDevice)(tokens)
    device.template_hits = {
        TEAM_SELECT: Point(900, 700),
        TEAM_SCREEN: Point(700, 200),
        DEPART: Point(1300, 760),
        CONFIRM: Point(754, 300),
        ACCEPT: Point(820, 420),
    }
    return device


class SequenceDevice(FakeDevice):
    """点了「部隊選択」之后才出现部队选择页（走真实的入口点击路径）。"""

    def __init__(self, tokens=MAP_TOKENS):
        super().__init__(tokens)
        self.team_select_clicked = False

    def template_match(self, template, roi=None, threshold=0.7):
        if template == TEAM_SCREEN and not self.team_select_clicked:
            return None
        return self.template_hits.get(template)

    def click(self, point):
        if (point.x, point.y) == (900, 700):
            self.team_select_clicked = True
        return super().click(point)


def make_host(device=None, watch=None):
    host = jp_expedition.JpExpedition(device or FakeDevice(), watch=watch,
                                      log=lambda s: None, poll=0.01,
                                      save_debug=lambda img: None)
    device = device or host.device
    host._read_tokens = lambda frame: list(device.tokens)
    return host


def run(gen):
    """跑完生成器，返回 (msgs, return_value)。"""
    msgs = []
    while True:
        try:
            msgs.append(next(gen))
        except StopIteration as stop:
            return msgs, stop.value


def dispatch_once(host, pack, team_no=1, status_payload=None):
    return run(host._expedition_safe_dispatch_stream(
        team_no, 1, 1, '鳥羽・伏見', pack=pack))


# ---- 正常派遣 ----

def test_pick_map_then_dispatch_happy_path(tmp_path):
    pack = make_pack(tmp_path)
    device = happy_device(cls=SequenceDevice)
    watch = FakeWatch(make_payload(HEALTHY_TEAM, status=2), )
    watch.refetched = True            # 派遣后游戏重报：status=2 → 数据路证明
    host = make_host(device, watch)
    host.current_location = '远征'
    events = []
    host.record_event = lambda et, **kw: events.append((et, kw)) or 1

    msgs, ok = run(host._expedition_pick_map_stream(1, 1, '鳥羽・伏見', pack=pack))
    assert ok is True
    assert ERA_PT in device.clicks            # 时代卡（素材包坐标）
    assert MAP_PT in device.clicks            # 小图卡
    assert (900, 700) in device.clicks        # 部隊選択
    assert any('已选中「鳥羽・伏見」' in m for m in msgs)

    msgs, ok = dispatch_once(host, pack)
    assert ok is True
    assert device.clicks.count(TEAM_PT) == 2  # 部队页签（国服同款双击）
    assert (1300, 760) in device.clicks       # 出撃
    assert (820, 420) in device.clicks        # 确认 はい
    assert any('已出发' in m and 'party-data' in m for m in msgs)
    # 记账：事件 + 记录文件（内容见数据隔离测试）
    assert [et for et, _ in events] == ['expedition.dispatched']


def test_dispatch_without_confirm_dialog(tmp_path):
    """日服远征实测：点「遠征開始」直接出发，无确认弹窗。
    弹窗素材在包里但画面不出现 → 不点はい，照样走证明/记账。"""
    pack = make_pack(tmp_path)
    device = happy_device(cls=SequenceDevice)
    device.team_select_clicked = True     # 直调派遣：视同已在部队选择页
    del device.template_hits[CONFIRM]     # 画面不出确认弹窗
    del device.template_hits[ACCEPT]
    watch = FakeWatch(make_payload(HEALTHY_TEAM, status=2))
    watch.refetched = True
    host = make_host(device, watch)
    host.current_location = '远征'

    msgs, ok = dispatch_once(host, pack)
    assert ok is True
    assert (1300, 760) in device.clicks       # 出撃
    assert (820, 420) not in device.clicks    # 没有はい可点
    assert any('无确认弹窗' in m for m in msgs)
    assert any('已出发' in m and 'party-data' in m for m in msgs)


def test_pick_map_retries_once_when_name_not_confirmed(tmp_path):
    """第一帧 OCR 没对上名 → 原坐标重选一次；对上就算过。"""
    pack = make_pack(tmp_path)
    device = happy_device(tokens=[])
    host = make_host(device, None)
    calls = {'n': 0}

    def toks(frame):
        calls['n'] += 1
        return [] if calls['n'] == 1 else MAP_TOKENS

    host._read_tokens = toks
    host.current_location = '远征'
    msgs, ok = run(host._expedition_pick_map_stream(1, 1, '鳥羽・伏見', pack=pack))
    assert ok is True
    assert device.clicks.count(ERA_PT) == 2   # 两组时代/小图各点过两次
    assert device.clicks.count(MAP_PT) == 2
    assert any('重选一次' in m for m in msgs)


def test_pick_map_gives_up_when_name_never_confirmed(tmp_path):
    """两次都对不上图名：怕派错队伍，停（绝不带病放行）。"""
    pack = make_pack(tmp_path)
    device = happy_device(tokens=[])
    host = make_host(device, None)
    host.current_location = '远征'
    msgs, ok = run(host._expedition_pick_map_stream(1, 1, '鳥羽・伏見', pack=pack))
    assert ok is False
    assert device.clicks.count(ERA_PT) == 2   # 重试过一次后收手
    assert not any(c == (900, 700) for c in device.clicks)  # 没进部队选择
    assert any('怕派错队伍' in m for m in msgs)


# ---- 重伤拦截 ----

def test_heavy_injury_blocks_departure(tmp_path):
    pack = make_pack(tmp_path)
    device = happy_device()
    host = make_host(device, FakeWatch(make_payload(HEAVY_TEAM)))
    host.current_location = '远征'
    msgs, ok = dispatch_once(host, pack)
    assert ok is False
    assert any('重伤' in m and '绝不派遣' in m for m in msgs)
    assert (1300, 760) not in device.clicks   # 出撃没点
    assert (820, 420) not in device.clicks
    assert not (JP_DATA_DIR / 'state' / 'expeditions.json').exists()


def test_medium_injury_logs_and_departs(tmp_path):
    pack = make_pack(tmp_path)
    # 数据路没戏（仍在待命）→ 走画面路证明：认出「遠征中」
    device = happy_device(tokens=MAP_TOKENS + [('遠征中', 0.9, 500, 500)])
    payload = make_payload(HEALTHY_TEAM + [(3, 103, 7, 20, 56)])  # 中伤
    host = make_host(device, FakeWatch(payload))
    host.current_location = '远征'
    msgs, ok = dispatch_once(host, pack)
    assert ok is True
    assert any('带伤出发' in m and '中伤' in m for m in msgs)


# ---- 状态不明：刷新重试一次，仍不明白就停 ----

def test_unknown_party_data_refreshes_once_then_stops(tmp_path, monkeypatch):
    pack = make_pack(tmp_path)
    device = happy_device()
    host = make_host(device, FakeWatch(None))   # 始终没数据
    host.current_location = '远征'
    monkeypatch.setattr(host, '_boot_to_home', lambda: None)
    monkeypatch.setattr(host, '_navigate_to_expedition',
                        lambda: setattr(host, 'current_location', '远征'))
    msgs, ok = dispatch_once(host, pack)
    assert ok is False
    assert device.reloads == 1                  # 刷新重拿过一次，不是直接放弃
    assert device.clicks.count(ERA_PT) == 1     # 刷新后重走了一遍选图
    assert any('宁可停' in m for m in msgs)
    assert (1300, 760) not in device.clicks     # 出撃始终没点
    assert not (JP_DATA_DIR / 'state' / 'expeditions.json').exists()


def test_stale_party_data_blocks_departure(tmp_path, monkeypatch):
    pack = make_pack(tmp_path)
    device = happy_device()
    host = make_host(device, FakeWatch(make_payload(HEALTHY_TEAM), age=9999))
    host.current_location = '远征'
    monkeypatch.setattr(host, '_boot_to_home', lambda: None)
    monkeypatch.setattr(host, '_navigate_to_expedition',
                        lambda: setattr(host, 'current_location', '远征'))
    msgs, ok = dispatch_once(host, pack)
    assert ok is False
    assert any('不新鲜' in m for m in msgs)
    assert (1300, 760) not in device.clicks


# ---- 结果证明：说不清就绝不假绿 ----

def test_proof_failure_never_records(tmp_path):
    pack = make_pack(tmp_path)
    device = happy_device(tokens=[('何か別の文字', 0.9, 10, 10)])
    watch = FakeWatch(make_payload(HEALTHY_TEAM, status=1))  # 始终待命
    watch.refetched = True
    host = make_host(device, watch)
    host.current_location = '远征'
    events = []
    host.record_event = lambda et, **kw: events.append((et, kw)) or 1
    msgs, ok = dispatch_once(host, pack)
    assert ok is False
    # 派遣动作本身照点（出撃+确认点了），但证明失败如实报、绝不记账
    assert (1300, 760) in device.clicks
    assert (820, 420) in device.clicks
    assert any('游戏没确认' in m for m in msgs)
    assert events == []
    assert not (JP_DATA_DIR / 'state' / 'expeditions.json').exists()


def test_visual_proof_via_running_text(tmp_path):
    """数据路没戏（仍在待命）时，画面认出「遠征中」也算游戏亲口确认。"""
    pack = make_pack(tmp_path)
    device = happy_device(tokens=MAP_TOKENS + [('遠征中', 0.9, 500, 500)])
    watch = FakeWatch(make_payload(HEALTHY_TEAM, status=1))
    watch.refetched = True
    host = make_host(device, watch)
    host.current_location = '远征'
    msgs, ok = dispatch_once(host, pack)
    assert ok is True
    assert any('visual确认' in m for m in msgs)


# ---- 数据隔离：记录只进日服数据目录 ----

def test_records_stay_in_jp_data_dir(tmp_path):
    pack = make_pack(tmp_path)
    device = happy_device(cls=SequenceDevice)
    watch = FakeWatch(make_payload(HEALTHY_TEAM, status=2))
    watch.refetched = True
    host = make_host(device, watch)
    host.current_location = '远征'
    events = []
    host.record_event = lambda et, **kw: events.append((et, kw)) or 1
    # 国服的倒计时记录文件绝不被日服写入（隔离目录里可能有别的测试
    # 留下的旧档，所以比「派遣前后字节不变」而不是「不存在」）
    cn_record = STATE_DIR / 'expeditions.json'
    before = cn_record.read_bytes() if cn_record.exists() else None

    msgs, ok = run(host._expedition_pick_map_stream(1, 1, '鳥羽・伏見', pack=pack))
    assert ok is True
    msgs, ok = dispatch_once(host, pack)
    assert ok is True

    record_path = JP_DATA_DIR / 'state' / 'expeditions.json'
    assert record_path.exists()
    data = json.loads(record_path.read_text(encoding='utf-8'))
    assert data['1']['map_name'] == '鳥羽・伏見'
    assert data['1']['era'] == 1 and data['1']['slot'] == 1
    after = cn_record.read_bytes() if cn_record.exists() else None
    assert after == before
    # 账房事件走日服实现（record_event 是本宿主覆写，调的是 get_jp_telemetry_store）
    assert events[0][0] == 'expedition.dispatched'
    assert events[0][1]['team_no'] == 1


# ---- 导航宿主接口 ----

def test_navigate_to_expedition_marks_location(tmp_path, monkeypatch):
    host = make_host(FakeDevice())
    monkeypatch.setattr(host, '_navigate_to_expedition',
                        lambda: setattr(host, 'current_location', '远征'))
    msgs = list(host.navigate_to_stream('远征'))
    assert host.current_location == '远征'
    assert any('已到达 远征' in m for m in msgs)


def test_navigate_home_reloads_and_boots(tmp_path, monkeypatch):
    device = FakeDevice()
    host = make_host(device)
    monkeypatch.setattr(host, '_boot_to_home', lambda: None)
    msgs = list(host.navigate_to_stream('本丸'))
    assert device.reloads == 1
    assert host.current_location == '本丸'
    assert any('已到达 本丸' in m for m in msgs)


def test_navigate_unknown_target_fails(tmp_path):
    host = make_host(FakeDevice())
    msgs = list(host.navigate_to_stream('鍛刀'))
    assert any('✗' in m for m in msgs)


def test_skip_point_never_leaks_cn_default(tmp_path):
    """安全点没标定就停，绝不漏出国服 (775,695)。"""
    host = make_host(FakeDevice())
    with pytest.raises(jp_sally.SallyStop, match='未标定'):
        host._skip_point()
    assert (775, 695) not in host.device.clicks
    assert host._skip_point((9, 9)) == (9, 9)          # 显式入参照走
    host._last_pack = make_pack(tmp_path)
    assert host._skip_point() == SKIP_PT               # 素材包给了就用


# ---- 弹窗扫地（只点认得的） ----

KNOWN_CENTERS = {
    'notice/close': (0.833, 0.164),
    'notice/hide-forever': (0.847, 0.658),
    'home/badge': (0.062, 0.129),
}


def fake_screen(*names):
    image = Image.new('RGB', (1508, 1088), (60, 60, 70))
    for name in names:
        template = jp_vision.load_template(name)
        nx, ny = KNOWN_CENTERS[name]
        image.paste(template, (int(nx * 1508 - template.width / 2),
                               int(ny * 1088 - template.height / 2)))
    return image


def test_popup_sweep_closes_notice_and_reports_clean(tmp_path):
    device = FakeDevice()
    device.frames = [fake_screen('notice/close', 'notice/hide-forever'),
                     fake_screen('home/badge')]
    host = make_host(device)
    assert host._popup_sweep(max_rounds=3) is True
    nx, ny = KNOWN_CENTERS['notice/close']
    assert any(abs(x - nx * 1508) < 12 and abs(y - ny * 1088) < 12
               for x, y in device.clicks)


def test_popup_sweep_unknown_screen_never_blind_clicks(tmp_path):
    device = FakeDevice()
    device.frames = [Image.new('RGB', (1508, 1088), (10, 200, 10))]
    host = make_host(device)
    assert host._popup_sweep(max_rounds=2) is False
    assert device.clicks == []


# ---- 缺素材：明确翻车，绝不伪造 ----

def test_missing_assets_listed_and_no_clicks(tmp_path):
    """包缺远征素材：逐步骤列出缺什么，一个坐标都不点。"""
    import dataclasses
    host = make_host(FakeDevice())
    host.current_location = '远征'
    real_pack = load_server_pack('jp')
    broken = dataclasses.replace(
        real_pack, points={},
        templates={k: v for k, v in real_pack.templates.items()
                   if not k.startswith('expedition/')})
    msgs, ok = run(host._expedition_pick_map_stream(1, 1, '鳥羽・伏見',
                                                    pack=broken))
    assert ok is False
    joined = '\n'.join(msgs)
    assert '没标定齐' in joined
    for ref in ('@expedition/era-1', '@expedition/map-1',
                '@expedition/team-select', '@expedition/team-screen'):
        assert ref in joined
    assert host.device.clicks == []
    assert any('绝不盲点' in m for m in msgs)


def test_bad_params_rejected_before_any_click(tmp_path):
    pack = make_pack(tmp_path)
    device = happy_device()
    host = make_host(device, FakeWatch(make_payload(HEALTHY_TEAM)))
    host.current_location = '远征'
    msgs, ok = run(host._expedition_pick_map_stream(9, 1, '鳥羽・伏見', pack=pack))
    assert ok is False and device.clicks == []
    msgs, ok = run(host._expedition_pick_map_stream(1, 9, '鳥羽・伏見', pack=pack))
    assert ok is False and device.clicks == []
    msgs, ok = run(host._expedition_pick_map_stream(1, 1, None, pack=pack))
    assert ok is False and any('map_name 必填' in m for m in msgs)
    msgs, ok = dispatch_once(host, pack, team_no=6)
    assert ok is False and (1300, 760) not in device.clicks


# ---- 官方流程单：形状 + 同 JSON 经 run_flow 调用 ----

def test_official_flow_shape_matches_contract():
    """三方共同契约钉死：id/步骤/参数/不设 on_fail，改动必须三方同步。"""
    flow = json.loads(FLOW_PATH.read_text(encoding='utf-8'))
    assert flow['id'] == 'expedition'
    assert flow['official'] is True
    steps = flow['steps']
    assert [s['type'] for s in steps] == ['navigate', 'builtin', 'builtin']
    assert steps[0]['params']['target'] == '@expedition/nav'
    assert steps[1]['params']['name'] == 'expedition_pick_map'
    assert steps[2]['params']['name'] == 'expedition_safe_dispatch'
    for step in steps:
        assert 'on_fail' not in step, '官方流程不设 on_fail（引擎默认收拾）'
        if step['type'] != 'builtin':
            continue
        params = step['params']
        assert params['era'] == 1 and params['map_slot'] == 1
        assert params['map_name'] is None
        assert params['team_no'] == 1   # pick_map 可忽略，但契约要求参数在场


def _flow_with_map_name(map_name='鳥羽・伏見'):
    """官方 JSON 的 map_name 契约默认 null（模板）；日服必填，按 run_script 同款姿势填实参。"""
    flow = json.loads(FLOW_PATH.read_text(encoding='utf-8'))
    for step in flow['steps']:
        params = step.get('params') or {}
        if params.get('name') in ('expedition_pick_map', 'expedition_safe_dispatch'):
            params['map_name'] = map_name
    return flow


@pytest.mark.skipif(
    not all(name in flow_engine.BUILTIN_REGISTRY
            for name in ('expedition_pick_map', 'expedition_safe_dispatch')),
    reason='flow_engine 侧的内置积木尚未注册（兄弟任务范围）')
def test_same_json_flow_runs_end_to_end(tmp_path, monkeypatch):
    """同一份官方 JSON 经 run_flow(server='jp') 打到日服宿主：全绿且点击齐全。"""
    pack = make_pack(tmp_path)
    monkeypatch.setattr(flow_engine, 'load_server_pack', lambda server='cn': pack)
    device = happy_device(cls=SequenceDevice)
    watch = FakeWatch(make_payload(HEALTHY_TEAM, status=2))
    watch.refetched = True
    host = make_host(device, watch)

    def fake_nav(target, max_retries=3):
        host.current_location = target
        yield f'nav→{target}'

    monkeypatch.setattr(host, 'navigate_to_stream', fake_nav)
    host.record_event = lambda et, **kw: 1     # 账房库在数据隔离测试里专门验
    msgs = list(flow_engine.run_flow(host, _flow_with_map_name(), server='jp'))
    assert any('全部跑完，全绿' in m for m in msgs), '\n'.join(msgs)
    assert (900, 700) in device.clicks        # 部隊選択真的被点过
    assert ERA_PT in device.clicks
    assert TEAM_PT in device.clicks
    assert (1300, 760) in device.clicks
    assert (JP_DATA_DIR / 'state' / 'expeditions.json').exists()


def test_pick_map_failure_stops_before_dispatch(tmp_path, monkeypatch):
    """契约：选图失败绝不继续派遣——失败步是不可自动恢复积木，剩余步不跑。"""
    pack = make_pack(tmp_path)
    device = happy_device(tokens=[])          # 图名永远对不上 → 选图必败
    host = make_host(device, None)
    called = []

    def spy_dispatch(team_no, era, map_slot, map_name, pack=None):
        called.append(team_no)
        yield from host._expedition_safe_dispatch_stream(
            team_no, era, map_slot, map_name, pack=pack)

    monkeypatch.setattr(host, '_expedition_safe_dispatch_stream', spy_dispatch)
    flow = _flow_with_map_name()
    if not all(name in flow_engine.BUILTIN_REGISTRY
               for name in ('expedition_pick_map', 'expedition_safe_dispatch')):
        pytest.skip('flow_engine 侧的内置积木尚未注册（兄弟任务范围）')
    monkeypatch.setattr(flow_engine, 'load_server_pack', lambda server='cn': pack)

    def fake_nav(target, max_retries=3):
        host.current_location = target
        yield f'nav→{target}'

    monkeypatch.setattr(host, 'navigate_to_stream', fake_nav)
    msgs = list(flow_engine.run_flow(host, flow, server='jp'))
    assert called == []                       # 派遣积木压根没被调用
    assert any('不可自动收拾' in m or '不跑' in m for m in msgs)


# ---- run_script 门面 ----

def test_run_script_params_validated():
    with pytest.raises(ValueError):
        list(jp_expedition.run_script(params={'team_no': 6}))
    with pytest.raises(ValueError):
        list(jp_expedition.run_script(params={'team_no': '二'}))
    with pytest.raises(ValueError):
        list(jp_expedition.run_script(params={'era': 0}))
    with pytest.raises(ValueError):
        list(jp_expedition.run_script(params={'era': 6}))
    with pytest.raises(ValueError):
        list(jp_expedition.run_script(params={'map_slot': 5}))
    with pytest.raises(ValueError):
        list(jp_expedition.run_script(params={'map_slot': '一'}))


def test_run_script_flows_with_defaults_and_same_json(tmp_path, monkeypatch,
                                                      capsys):
    """run_script 默认参数（鳥羽・伏見）+ 同一 JSON 全绿走通（设备/通道全假）。"""
    if not all(name in flow_engine.BUILTIN_REGISTRY
               for name in ('expedition_pick_map', 'expedition_safe_dispatch')):
        pytest.skip('flow_engine 侧的内置积木尚未注册（兄弟任务范围）')
    pack = make_pack(tmp_path)
    device = happy_device(cls=SequenceDevice)
    watch = FakeWatch(make_payload(HEALTHY_TEAM, status=2))
    watch.refetched = True

    monkeypatch.setattr(flow_engine, 'load_server_pack', lambda server='cn': pack)
    monkeypatch.setattr(jp_expedition.jp_browser, 'JpBrowser', lambda: object())

    class FakeGameDevice:
        def __enter__(self):
            return device

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(jp_expedition.jp_game_device, 'JpGameDevice',
                        lambda browser: FakeGameDevice())

    class FakePartyWatch:
        def __enter__(self):
            return watch

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(jp_expedition.jp_party, 'PartyWatch', lambda: FakePartyWatch())
    monkeypatch.setattr(jp_expedition.JpExpedition, '_navigate_to_expedition',
                        lambda self: setattr(self, 'current_location', '远征'))
    monkeypatch.setattr(jp_expedition.JpExpedition, 'record_event',
                        lambda self, et, **kw: 1)
    monkeypatch.setattr(jp_expedition.JpExpedition, '_read_tokens',
                        lambda self, frame: list(device.tokens))

    lines = list(jp_expedition.run_script(params={'team_no': 1}))
    assert lines == []
    out = capsys.readouterr().out
    assert '全部跑完，全绿' in out
    data = json.loads((JP_DATA_DIR / 'state' / 'expeditions.json')
                      .read_text(encoding='utf-8'))
    assert data['1']['map_name'] == '鳥羽・伏見'   # 默认图名落账


def test_run_script_aborts_when_watch_unavailable(tmp_path, monkeypatch):
    from touken.flow_control import FlowAborted

    if not all(name in flow_engine.BUILTIN_REGISTRY
               for name in ('expedition_pick_map', 'expedition_safe_dispatch')):
        pytest.skip('flow_engine 侧的内置积木尚未注册（兄弟任务范围）')
    monkeypatch.setattr(jp_expedition.jp_browser, 'JpBrowser', lambda: object())

    class FakeGameDevice:
        def __enter__(self):
            return FakeDevice()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(jp_expedition.jp_game_device, 'JpGameDevice',
                        lambda browser: FakeGameDevice())

    def broken_watch():
        raise jp_party.PartyWatchError('没能连上日服浏览器的数据通道')

    monkeypatch.setattr(jp_expedition.jp_party, 'PartyWatch', broken_watch)
    with pytest.raises(FlowAborted, match='数据通道'):
        list(jp_expedition.run_script(params={'team_no': 1}))


# ---- 真 jp 素材包断言（本任务不改 test_server_pack.py，放这里） ----

def test_jp_pack_resolves_expedition_navigation():
    pack = load_server_pack('jp')
    assert pack.base_size == (1508, 1088)
    assert pack.resolve_navigation('@expedition/nav') == '远征'


def test_jp_pack_expedition_assets_calibrated():
    """真机标定已完成（2026-10-28 实跑帧）：远征引用全部可解析。
    confirm/confirm-accept 是可选素材（日服远征实测无二次确认弹窗），
    不在此列。"""
    pack = load_server_pack('jp')
    for ref in [f'@expedition/era-{i}' for i in range(1, 6)]:
        assert len(pack.resolve_point(ref)) == 2
    for ref in [f'@expedition/map-{i}' for i in range(1, 5)]:
        assert len(pack.resolve_point(ref)) == 2
    for ref in [f'@expedition/team-{i}' for i in range(1, 6)]:
        assert len(pack.resolve_point(ref)) == 2
    assert len(pack.resolve_point('@expedition/skip')) == 2
    for ref in ('@expedition/screen', '@expedition/team-select',
                '@expedition/team-screen', '@expedition/depart',
                '@directory/expedition', '@menu/expedition'):
        assert pack.resolve_template(ref).endswith('.png')
