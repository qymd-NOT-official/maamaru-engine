# -*- coding: utf-8 -*-
"""日服远征收取画面适配：钩子行为与诚实记账（无浏览器，假设备驱动）。

对标 tests/test_expedition_collect.py 的循环层测试，本文件验证日服
视图层自己的职责：OCR 词与比例 ROI 的基准像素换算、本丸模板名、
点击点换算、结算指纹去重、视觉流水账（unknown 诚实字段）、
空收据回退到视觉记账。
"""
import json
from unittest.mock import patch

from PIL import Image

import touken.flows.expedition_collect_jp as jp_collect
from touken.flows.expedition_collect import ExpeditionCollectMixin


def _frame(color):
    return Image.new("RGB", (1280, 720), color)


class FakeDevice:
    """无浏览器的日服设备：按当前画面脚本应答 OCR/模板/点击。"""

    def __init__(self, screens, base=(1280, 720)):
        self.screens = list(screens)  # 每次点击后依次呈现的画面
        self._base = base
        self.frames = {"banner": _frame((200, 50, 50)),
                       "settlement": _frame((50, 200, 50)),
                       "home": _frame((50, 50, 200))}
        self.ocr_calls = []
        self.template_calls = []
        self.clicks = []

    @property
    def current(self):
        return self.screens[0] if self.screens else "home"

    def base_size(self):
        return self._base

    def screenshot(self, force=False):
        return self.frames.get(self.current, _frame((0, 0, 0)))

    def ocr(self, expected, roi=None, match_mode="contains"):
        self.ocr_calls.append((expected, roi))
        if expected == jp_collect._SETTLEMENT_TITLE_WORD and self.current == "settlement":
            return True
        if expected == jp_collect._BANNER_WORD and self.current == "banner":
            return True
        return None

    def template_match(self, template, roi=None, threshold=0.7):
        self.template_calls.append(template)
        return self.current == "home"

    def click(self, point):
        self.clicks.append(point)
        if len(self.screens) > 1:
            self.screens.pop(0)


class JpHost(jp_collect.JpExpeditionCollectViewMixin, ExpeditionCollectMixin):
    """假设备宿主：组合日服视图与公共循环，不碰浏览器。"""

    def __init__(self, device, reach_home=True):
        self.device = device
        self.reach_home = reach_home
        self.current_location = "本丸"

    def navigate_to_stream(self, target):
        if target == "本丸" and not self.reach_home:
            yield "[导航] ✗ 没能回本丸"
            return
        self.current_location = target
        yield f"[导航] ✓ 已到达 {target}"


def run(host, redispatch=None):
    with patch("time.sleep"):
        return list(host.collect_expedition_stream(redispatch=redispatch))


# ==================== 钩子行为 ====================

def test_gate_requires_device():
    host = JpHost(FakeDevice(["home"]))
    host.device = None
    assert host._collect_gate() == "[收菜] 日服设备通道未连接，请从まあ丸打开游戏后重试"
    assert JpHost(FakeDevice(["home"]))._collect_gate() is None


def test_ocr_words_and_roi_scaling():
    device = FakeDevice(["settlement"])
    host = JpHost(device)
    assert host._collect_at_settlement() is True
    expected, roi = device.ocr_calls[-1]
    assert expected == "遠征結果"
    assert (roi.x, roi.y, roi.w, roi.h) == (480, 0, 320, 80)

    device2 = FakeDevice(["banner"])
    host2 = JpHost(device2)
    assert host2._collect_at_banner() is True
    expected2, roi2 = device2.ocr_calls[-1]
    assert expected2 == "帰"
    assert (roi2.x, roi2.y, roi2.w, roi2.h) == (380, 180, 520, 300)


def test_roi_follows_base_size():
    device = FakeDevice(["settlement"], base=(1600, 900))
    JpHost(device)._collect_at_settlement()
    _, roi = device.ocr_calls[-1]
    assert (roi.x, roi.y, roi.w, roi.h) == (600, 0, 400, 100)


def test_at_home_uses_calibrated_template():
    device = FakeDevice(["home"])
    assert JpHost(device)._collect_at_home() is True
    assert device.template_calls == ["home/catalog"]


def test_tap_point_follows_base_size():
    device = FakeDevice(["home"])
    JpHost(device)._collect_tap()
    assert (device.clicks[-1].x, device.clicks[-1].y) == (640, 600)

    device2 = FakeDevice(["home"], base=(1600, 900))
    JpHost(device2)._collect_tap()
    assert (device2.clicks[-1].x, device2.clicks[-1].y) == (800, 750)


# ==================== 结算观察哨 ====================

def test_observer_honest_unknown_and_fingerprint_dedupe():
    device = FakeDevice(["settlement"])
    host = JpHost(device)
    seen = []
    with patch("time.sleep"):
        obs = host.observe_expedition_settlement(via="collect", seen=seen,
                                                 sequence=1, record=False)
    assert obs["new"] is True
    assert obs["team_no"] is None and obs["result"] == "unknown"
    assert obs["rewards"] == "unknown"
    assert len(seen) == 1

    # 同一屏没翻动：指纹命中，不重复报新
    with patch("time.sleep"):
        again = host.observe_expedition_settlement(via="collect", seen=seen,
                                                   sequence=1, record=False)
    assert again == {"new": False}


def test_observer_returns_none_off_settlement():
    host = JpHost(FakeDevice(["home"]))
    with patch("time.sleep"):
        assert host.observe_expedition_settlement(via="collect", seen=[]) is None


def test_facts_empty_means_visual_fallback():
    host = JpHost(FakeDevice(["home"]))
    assert host._collection_facts(0) == []
    assert host._collection_facts(999, {"x"}) == []


def test_journal_records_honest_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(jp_collect, "JP_DATA_DIR", tmp_path)
    host = JpHost(FakeDevice(["settlement"]))
    obs = {"via": "collect", "team_no": None, "era": None, "slot": None,
           "map_name": None, "result": "unknown", "rewards": "unknown",
           "fingerprint": "abc123"}
    host._record_visual_settlement(obs, 1)
    lines = (tmp_path / "collect_journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["server"] == "jp" and record["sequence"] == 1
    assert record["result"] == "unknown" and record["rewards"] == "unknown"
    assert record["team_no"] is None


# ==================== 全流程（假设备） ====================

def test_full_collect_loop_visual_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(jp_collect, "JP_DATA_DIR", tmp_path)
    device = FakeDevice(["banner", "settlement", "home", "home"])
    host = JpHost(device)
    messages = run(host)
    assert "[收菜] 远征归来横幅，点过" in messages
    assert "[收菜] 已翻过第1份远征结算" in messages
    assert "[收菜] ✅ 收了 1 份远征奖励" in messages
    assert len(device.clicks) == 2
    # 收据为空 → 视觉流水账顶账
    lines = (tmp_path / "collect_journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["sequence"] == 1


def test_same_settlement_not_double_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(jp_collect, "JP_DATA_DIR", tmp_path)
    device = FakeDevice(["settlement", "settlement", "home", "home"])
    host = JpHost(device)
    messages = run(host)
    assert messages.count("[收菜] 已翻过第1份远征结算") == 1
    assert "已翻过第2份" not in "".join(messages)
    lines = (tmp_path / "collect_journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_not_reaching_home_stops_early():
    device = FakeDevice(["home"])
    host = JpHost(device, reach_home=False)
    messages = run(host)
    assert messages[-1] == "[收菜] 没能回到本丸，停"
    assert device.clicks == []
