# -*- coding: utf-8 -*-
"""日服远征收取的画面适配与结算流水账。

收取公共循环（expedition_collect.py）只调用本层的 _collect_* 钩子；
日服画面素材（日语 OCR 词/ROI/点击点/本丸模板）、结算屏指纹去重和
日服本地流水账都收在这里。公共循环不直接访问 device、模板或像素坐标。

素材标定状态（诚实声明）：
- 本丸模板 home/catalog：已标定（resource/base/image/jp/，来自运行帧）。
- ROI 与点击点：两服游戏画面同构，从国服配置（1280×720）按比例迁移。
- 结算标题词「遠征結果」、归来横幅词「帰」：按国服同款语义与单字策略
  （书法字 OCR 会拆单字，国服用「归」）迁移，**待日服真机远征归来时
  标定**。认不出的失败方向是「画面未确认→停在本丸、不再派」，安全。
- 日服收据通道（JSON 收据仲裁）未建：_collection_facts 恒返回 []，
  结算一律走视觉流水账（JP_DATA_DIR/collect_journal.jsonl，认不出
  的字段诚实记 unknown，不臆造），顺手再派暂不开放。
"""
from __future__ import annotations

import hashlib
import json
import time

from .. import jp_browser, jp_expedition, jp_game_device, jp_sally
from ..device_adapter import Point, Region
from ..runtime_paths import JP_DATA_DIR
from .expedition_collect import ExpeditionCollectMixin

# 结算标题（顶部居中）与归来横幅（中部书法字）的 ROI：国服
# touken_config.example.json 的 [x1,y1,x2,y2] 换算成 (x,y,w,h) 比例，
# 运行时乘日服基准尺寸（jp_vision.base_size() 锁存的标定空间）。
_SETTLEMENT_TITLE_ROI = (0.375, 0.0, 0.25, 0.1111)   # 国服 [480,0,800,80]
_BANNER_ROI = (0.2969, 0.25, 0.4062, 0.4167)         # 国服 [380,180,900,480]
_TAP_POINT = (0.5, 0.8333)                           # 国服 [640,600]

# 日语 OCR 词（标定状态见模块 docstring）
_SETTLEMENT_TITLE_WORD = "遠征結果"
# 书法字横幅整句会被 OCR 拆成单字（国服因此用「归」做包含匹配），
# 日服同理用「帰」单字，宁宽勿漏——误中横幅的代价只是多点一下。
_BANNER_WORD = "帰"

_HOME_TEMPLATE = "home/catalog"

# 结算屏指纹采样区（中部主体，避开四角动态元素），用于同屏去重
_FINGERPRINT_CROP = (0.25, 0.15, 0.75, 0.85)


class JpExpeditionCollectViewMixin:
    """日服远征收取的画面钩子与结算流水账。宿主需提供 device。"""

    # ==================== 公共循环钩子 ====================

    def _collect_gate(self):
        """设备门：日服收取只依赖浏览器帧通道，没有就趁早炸。"""
        if getattr(self, "device", None) is None:
            return "[收菜] 日服设备通道未连接，请从まあ丸打开游戏后重试"
        return None

    def _collect_trigger_return_animation(self):
        """远征界面 → 回本丸，强制换场景勾出归来动画；到位返回 True。"""
        yield "[收菜] 🧺 先绕去远征界面再回本丸，勾一下归来动画..."
        for nav_msg in self.navigate_to_stream("远征"):
            yield nav_msg
        if self.current_location != "远征":
            yield "[收菜] 没能到达远征界面，停"
            return False
        for nav_msg in self.navigate_to_stream("本丸"):
            yield nav_msg
        if self.current_location != "本丸":
            yield "[收菜] 没能回到本丸，停"
            return False
        return True

    def _collect_refresh_frame(self):
        self.device.screenshot(force=True)

    def _collect_at_settlement(self) -> bool:
        return bool(self.device.ocr(expected=_SETTLEMENT_TITLE_WORD,
                                    roi=self._roi(_SETTLEMENT_TITLE_ROI)))

    def _collect_at_banner(self) -> bool:
        return bool(self.device.ocr(expected=_BANNER_WORD,
                                    roi=self._roi(_BANNER_ROI)))

    def _collect_at_home(self) -> bool:
        return bool(self.device.template_match(_HOME_TEMPLATE))

    def _collect_tap(self):
        width, height = self.device.base_size()
        self.device.click(Point(round(width * _TAP_POINT[0]),
                                round(height * _TAP_POINT[1])))

    def _collection_facts(self, after, exclude=()):
        """日服收据通道未建（/conquest 归来报文未样本化）：恒返回 []，
        公共循环自动落到视觉流水账。不借默认值装出有收据。"""
        return []

    def _forget_collected_records(self, receipts):
        """日服无收据通道，公共循环拿到空收据就不会走到这；防御性保留。"""

    # ==================== 结算观察与流水账 ====================

    def observe_expedition_settlement(self, via: str, seen: list = None,
                                      sequence: int = None, record: bool = True) -> dict | None:
        """
        日服远征结算屏观察哨。当前画面是结算屏时把能确认的事实记下来，
        返回观察记录；不是结算屏返回 None。

        诚实契约与国服一致：只观察、不点击；认不出的一律记 unknown——
        部队号读不出不会默认成某队，结果读不出不会默认成「成功」。
        seen 是调用方持有的像素指纹列表：同一屏没翻动时凭指纹只报一次新。
        日服 v1 不读队伍/地图/奖励字段（日语版面 OCR 词表未标定），
        只确认「这是一张结算屏」并留指纹。
        """
        if not self._collect_at_settlement():
            return None
        # 转场半途保护（与国服同款）：标题连看两帧都在才认，防读进翻页动画
        time.sleep(0.4)
        self._collect_refresh_frame()
        if not self._collect_at_settlement():
            return None

        fingerprint = self._settlement_fingerprint(self.device.screenshot())
        if fingerprint and seen is not None:
            if fingerprint in seen:
                return {"new": False}
            seen.append(fingerprint)

        return {"new": True, "via": via, "sequence": sequence,
                "team_no": None, "era": None, "slot": None, "map_name": None,
                "result": "unknown", "rewards": "unknown",
                "fingerprint": fingerprint, "observed_at": int(time.time())}

    def _record_visual_settlement(self, obs: dict, sequence: int):
        """视觉结算记账：写日服本地流水账 JSONL（诚实 unknown，不臆造）。"""
        record = {"ts": int(time.time()), "server": "jp", "via": obs.get("via"),
                  "sequence": sequence, "team_no": obs.get("team_no"),
                  "era": obs.get("era"), "slot": obs.get("slot"),
                  "map_name": obs.get("map_name"),
                  "result": obs.get("result", "unknown"),
                  "rewards": obs.get("rewards", "unknown"),
                  "fingerprint": obs.get("fingerprint")}
        JP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(JP_DATA_DIR / "collect_journal.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ==================== 内部 ====================

    def _roi(self, rel) -> Region:
        """比例 ROI → 基准像素 Region。"""
        width, height = self.device.base_size()
        return Region(round(width * rel[0]), round(height * rel[1]),
                      round(width * rel[2]), round(height * rel[3]))

    @staticmethod
    def _settlement_fingerprint(frame):
        """结算屏像素指纹：中部主体降采样哈希。不同队伍/地图的结算屏
        文字不同则指纹不同；同一张屏在 CDP 取帧下像素确定，指纹稳定。"""
        if frame is None:
            return None
        w, h = frame.size
        body = frame.crop((int(w * _FINGERPRINT_CROP[0]), int(h * _FINGERPRINT_CROP[1]),
                           int(w * _FINGERPRINT_CROP[2]), int(h * _FINGERPRINT_CROP[3])))
        body = body.convert("L").resize((64, 48))
        return hashlib.md5(body.tobytes()).hexdigest()


class JpExpeditionCollect(JpExpeditionCollectViewMixin,
                          jp_expedition.JpExpedition,
                          ExpeditionCollectMixin):
    """日服远征收取宿主：JpExpedition 的导航/设备/屏幕识别 +
    日服画面钩子 + 公共循环。v1 只收不派（结算字段阅读未标定）。"""


def run_script(config_path=None, params: dict | None = None):
    """面板脚本入口（预备 jp-expedition-collect）：generator[str]
    （日志走 print，worker 捕获，与 jp_sally/jp_expedition 同款）。

    v1 只收不派：传了 redispatch 如实提示并忽略，不假装支持。"""
    from ..flow_control import FlowAborted
    params = params or {}
    redispatch = params.get("redispatch")
    if redispatch:
        print("[收菜] 日服顺手再派尚未实装（结算字段阅读未标定），本次只收不派")
        redispatch = None
    try:
        with jp_game_device.JpGameDevice(jp_browser.JpBrowser()) as device:
            host = JpExpeditionCollect(device)
            for message in host.collect_expedition_stream(redispatch=redispatch):
                print(message)
    except jp_sally.SallyStop as exc:
        raise FlowAborted(str(exc)) from exc
    yield from ()
