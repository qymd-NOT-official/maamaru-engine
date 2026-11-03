"""同一收取循环可由独立画面适配驱动：无 device/config 的宿主跑公共循环。

验证拆分边界（对标 tests/test_sortie_split.py）：循环的编排（认屏顺序、
连击熔断、收据优先、再派编排）都在公共层，画面与数据触点全是钩子。
"""
from unittest.mock import patch

from touken.flows.expedition_collect import ExpeditionCollectMixin


def _obs(team_no=2, era=1, slot=1, map_name="鸟羽·伏见之战"):
    return {"new": True, "via": "collect", "result": "成功",
            "rewards": {}, "rewards_status": {},
            "special_rewards": [], "special_status": "none",
            "special_sample_saved": False,
            "team_no": team_no, "era": era, "slot": slot,
            "map_name": map_name, "header": None}


def _receipt(team_no, key):
    return {"ts": 100.0, "payload": {"receipt_key": key, "team_no": team_no,
                                     "era": 1, "slot": 1, "map_code": "1-1",
                                     "rewards": {}}}


class CollectHost(ExpeditionCollectMixin):
    """没有 device/config/像素坐标的宿主，验证循环不会暗中借用国服画面。"""

    def __init__(self, frames, obs_script=(), facts_baseline=(),
                 facts_after=(), sync_failed=False, gate_error=None,
                 reach_home=True, goal_pick=("2-1", 2, 1)):
        self.frames = list(frames)      # 每次点击后依次呈现的画面
        self.obs_script = list(obs_script)
        self.facts_baseline = list(facts_baseline)
        self.facts_after = list(facts_after)
        self.sync_failed = sync_failed
        self.gate_error = gate_error
        self.reach_home = reach_home
        self.goal_pick = goal_pick
        self.current = None
        self.taps = 0
        self.visual_records = []
        self.forgotten = []
        self.dispatches = []
        self.facts_calls = []

    # ---- 画面钩子（无设备实现）----
    def _collect_gate(self):
        return self.gate_error

    def _collect_trigger_return_animation(self):
        yield "[收菜] 🧺 先绕去远征界面再回本丸，勾一下归来动画..."
        if not self.reach_home:
            yield "[收菜] 没能回到本丸，停"
            return False
        return True

    def _collect_refresh_frame(self):
        self.current = self.frames[0] if self.frames else "unknown"

    def _collect_at_settlement(self):
        return self.current == "settlement"

    def _collect_at_banner(self):
        return self.current == "banner"

    def _collect_at_home(self):
        return self.current == "home"

    def _collect_tap(self):
        self.taps += 1
        if len(self.frames) > 1:
            self.frames.pop(0)

    # ---- 数据与记账钩子 ----
    def observe_expedition_settlement(self, via, seen=None, sequence=None,
                                      record=True):
        return self.obs_script.pop(0)

    def _collection_facts(self, after, exclude=()):
        self.facts_calls.append((after, set(exclude)))
        if after > 0:
            self._collection_sync_failed = self.sync_failed
            return list(self.facts_after)
        return list(self.facts_baseline)

    def _forget_collected_records(self, receipts):
        self.forgotten.extend(receipts)

    def _record_visual_settlement(self, obs, sequence):
        self.visual_records.append((obs, sequence))

    def _pick_map_for_goal(self, resource):
        return self.goal_pick

    def expedition_stream(self, era, map_name=None, team_no=1, map_slot=None):
        self.dispatches.append({"era": era, "map_name": map_name,
                                "team_no": team_no, "map_slot": map_slot})
        yield f"[派遣] 部队{team_no} 已出发"


def run(host, redispatch=None):
    with patch("time.sleep"):
        return list(host.collect_expedition_stream(redispatch=redispatch))


def test_gate_error_stops_before_any_action():
    host = CollectHost(frames=["home"], gate_error="[收菜] 未配置远征")
    messages = run(host)
    assert messages == ["[收菜] 未配置远征"]
    assert host.facts_calls == [] and host.taps == 0


def test_not_reaching_home_stops_early():
    host = CollectHost(frames=["home"], reach_home=False)
    messages = run(host)
    assert messages[-1] == "[收菜] 没能回到本丸，停"
    assert host.taps == 0
    assert len(host.facts_calls) == 1  # 只有出发前的基线核对


def test_banner_then_settlement_then_home():
    host = CollectHost(frames=["banner", "settlement", "home", "home"],
                       obs_script=[_obs()])
    messages = run(host)
    assert "[收菜] 远征归来横幅，点过" in messages
    assert "[收菜] 已翻过第1份远征结算" in messages
    assert "[收菜] ✅ 收了 1 份远征奖励" in messages
    assert host.taps == 2
    assert len(host.visual_records) == 1
    assert host.visual_records[0][1] == 1  # 顺序号从 1 开始


def test_same_screen_not_flipped_taps_without_double_count():
    host = CollectHost(
        frames=["settlement", "settlement", "home", "home"],
        obs_script=[_obs(), {"new": False}])
    messages = run(host)
    assert messages.count("[收菜] 已翻过第1份远征结算") == 1
    assert "已翻过第2份" not in "".join(messages)
    assert host.taps == 2
    assert len(host.visual_records) == 1


def test_no_expedition_home_and_calm():
    host = CollectHost(frames=["home", "home"])
    messages = run(host, redispatch="same")
    assert "[收菜] ✓ 没有远征回来，本丸风平浪静～" in messages
    assert host.dispatches == [] and host.taps == 0


def test_unresolved_screen_keeps_records_and_skips_redispatch():
    host = CollectHost(frames=["unknown"] * 6)
    messages = run(host, redispatch="same")
    assert "[收菜] ⚠️ 当前页面未确认回到本丸，结算记录已保留，暂不再派" in messages
    assert host.taps == 5  # 连认不出 5 次后第 6 次熔断，不再盲点
    assert host.dispatches == []


def test_receipts_take_priority_over_visual_records():
    receipts = [_receipt(2, "k1"), _receipt(3, "k2")]
    host = CollectHost(frames=["home", "home"], facts_after=receipts,
                       sync_failed=True)
    messages = run(host)
    assert "[收菜] 游戏已确认 2 份结算的部队、地图和奖励" in messages
    assert "[收菜] 账房同步未完成，可稍后同步近况；本次结算已确认" in messages
    assert "[收菜] ✅ 收了 2 份远征奖励" in messages
    assert host.forgotten == receipts
    assert host.visual_records == []  # 有收据不走视觉兜底


def test_redispatch_same_map_per_returned_team():
    host = CollectHost(frames=["settlement", "home", "home"],
                       obs_script=[_obs(team_no=3, era=4, slot=2, map_name="X图")])
    messages = run(host, redispatch="same")
    assert "[收菜] 顺手把部队3派回原图（时代4卡位2）..." in messages
    assert host.dispatches == [{"era": 4, "map_name": "X图",
                                "team_no": 3, "map_slot": 2}]
    assert messages[-1] == "[收菜] 再派完毕，共处理 1 支队"


def test_redispatch_same_skips_team_with_incomplete_info():
    obs = _obs(team_no=2, era=None, slot=None, map_name=None)
    host = CollectHost(frames=["settlement", "home", "home"], obs_script=[obs])
    messages = run(host, redispatch="same")
    assert "[收菜] 部队2的归来信息没读全，这队跳过" in messages
    assert host.dispatches == []


def test_redispatch_by_resource_goal():
    host = CollectHost(frames=["settlement", "home", "home"],
                       obs_script=[_obs(team_no=2)], goal_pick=("3-4", 3, 4))
    messages = run(host, redispatch="小判")
    assert "[收菜] 目标「小判」时薪最高的图：3-4" in messages
    assert host.dispatches == [{"era": 3, "map_name": None,
                                "team_no": 2, "map_slot": 4}]


def test_redispatch_goal_without_map_skips():
    host = CollectHost(frames=["settlement", "home", "home"],
                       obs_script=[_obs()], goal_pick=None)
    messages = run(host, redispatch="小判")
    assert "[收菜] 收益表里没有产「小判」的图，再派跳过" in messages
    assert host.dispatches == []


def test_redispatch_only_once_per_team():
    obs1, obs2 = _obs(team_no=2), _obs(team_no=2)
    host = CollectHost(
        frames=["settlement", "settlement", "home", "home"],
        obs_script=[obs1, obs2])
    messages = run(host, redispatch="same")
    assert len(host.dispatches) == 1  # 同支队多份结算只派回一次
    assert messages[-1] == "[收菜] 再派完毕，共处理 1 支队"
