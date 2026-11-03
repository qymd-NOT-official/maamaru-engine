"""同一手入循环可由独立画面适配驱动：无 device/config 的宿主跑公共循环。

验证拆分边界（对标 tests/test_sortie_split.py）：黑名单、擦伤守卫、
章与数据仲裁、加速符判定、演习模式、失败处理都在公共层，
画面与数据触点全是钩子。
"""
from unittest.mock import patch

from touken.flows.repair_loop import RepairLoopMixin

_NUMERAL = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}


def _row(name, prefix="一之一", y=200, injury=None):
    return {"prefix": prefix, "name": name, "y": y, "injury": injury}


def _fact(hp=20, hp_max=45, injury="中伤", team_no=1, serial=101, catalog=10):
    return {"serial_id": serial, "catalog_id": catalog, "hp": hp,
            "hp_max": hp_max, "injury": injury, "team_no": team_no,
            "memberships": [(team_no, "1")]}


class RepairHost(RepairLoopMixin):
    """没有 device/config/像素坐标的宿主，验证循环不会暗中借用国服画面。"""

    def __init__(self, pages, config=None, facts=(), row_facts=None,
                 lookup=None, repair_results=(), refresh_ok=True):
        self.pages = list(pages)          # 每次扫描依次给出的页面 [(rows, junk)]
        self.rcfg = config
        self.facts = facts                # () 表示无数据通道
        self.row_facts = row_facts or {}  # (prefix, name) -> fact dict / False / None
        self.lookup = lookup or {}
        self.repair_results = list(repair_results)
        self.refresh_ok = refresh_ok
        self.repair_calls = []
        self.refreshed = []
        self.scrolls = 0
        self.open_calls = 0
        self.events = []

    def _repair_config(self):
        return self.rcfg

    def _repair_sword_lookup(self, name):
        return self.lookup.get(name)

    def _repair_speedup_mark(self, team_no):
        numeral = _NUMERAL.get(team_no)
        return f"{numeral}之" if numeral else None

    def _repair_goto_room(self):
        yield "[手入] 正在导航到修复..."
        return True

    def _repair_refresh_membership(self, std_name):
        self.refreshed.append(std_name)
        yield "[手入] 刷新编队状态"
        return self.refresh_ok

    def _repair_open_select_screen(self):
        self.open_calls += 1
        return True

    def _repair_scan_page(self):
        if self.pages:
            return self.pages.pop(0)
        return [], []

    def _repair_scroll_list(self):
        self.scrolls += 1

    def _repair_game_facts(self):
        return self.facts or None

    def _repair_row_fact(self, row, facts):
        if facts is None:
            return None
        return self.row_facts.get((row["prefix"], row["name"]))

    def _repair_one(self, name_y, need_speed):
        self.repair_calls.append({"name_y": name_y, "need_speed": need_speed})
        result = self.repair_results.pop(0) if self.repair_results else True
        if result is True:
            yield "[手入] 已开工"
        return result

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))


def run(host, **kwargs):
    with patch("time.sleep"):
        return list(host.repair_stream(**kwargs))


CFG = {"blacklist": [], "speedup_teams": []}


def test_gate_blocks_unconfigured():
    host = RepairHost(pages=[], config=None)
    assert run(host) == ["[手入] 未配置手入"]
    assert host.last_repair_stats == {"repaired": 0, "speedups": 0,
                                      "skipped": 0, "unresolved": 0}


def test_blacklist_skips_without_repairing():
    host = RepairHost(
        pages=[([_row("碰瓷刀")], [])], config={"blacklist": ["碰瓷刀"],
                                               "speedup_teams": []},
        facts=["facts"],
        row_facts={("一之一", "碰瓷刀"): _fact(catalog=99)},
        lookup={"碰瓷刀": (99, "碰瓷刀")})
    messages = run(host)
    assert any("黑名单（碰瓷队带伤上班），跳过" in m for m in messages)
    assert host.repair_calls == []
    assert ("repair.skipped", {"name": "碰瓷刀", "sword_id": 99,
                              "team_mark": "一之一",
                              "reason": "blacklist"}) in host.events


def test_full_hp_row_passes_silently():
    host = RepairHost(
        pages=[([_row("五虎退")], [])], config=CFG, facts=["facts"],
        row_facts={("一之一", "五虎退"): _fact(hp=45, hp_max=45)},
        lookup={"五虎退": (10, "五虎退")})
    messages = run(host)
    assert host.repair_calls == []
    assert "完事：修了 0 把" in messages[-1]


def test_plain_repair_without_speedup():
    host = RepairHost(
        pages=[([_row("五虎退")], [])], config=CFG, facts=["facts"],
        row_facts={("一之一", "五虎退"): _fact(injury="中伤", team_no=1)},
        lookup={"五虎退": (10, "五虎退")})
    messages = run(host)
    assert any("一之一 五虎退 → 修（不用加速符）" in m for m in messages)
    assert host.repair_calls == [{"name_y": 200, "need_speed": False}]
    assert host.last_repair_stats["repaired"] == 1
    assert "✅ 完事：修了 1 把" in messages[-1]


def test_scratch_guard_saves_speedup_and_logs_discrepancy():
    # 数据轻伤但 hp≥90% 按擦伤：不烧符，章与数据不一致留旁证日志
    host = RepairHost(
        pages=[([_row("五虎退", prefix="三之一", injury="轻伤")], [])],
        config={"blacklist": [], "speedup_teams": [3]}, facts=["facts"],
        row_facts={("三之一", "五虎退"): _fact(hp=41, hp_max=45,
                                              injury="轻伤", team_no=3)},
        lookup={"五虎退": (10, "五虎退")})
    messages = run(host)
    assert any("按游戏数据决策" in m for m in messages)
    assert any("三之一 五虎退 → 修（擦伤没到轻伤，省一张加速符）" in m for m in messages)
    assert host.repair_calls == [{"name_y": 200, "need_speed": False}]


def test_speed_team_injury_uses_speedup_and_refreshes():
    host = RepairHost(
        pages=[([_row("五虎退", prefix="三之一")], [])],
        config={"blacklist": [], "speedup_teams": [3]}, facts=["facts"],
        row_facts={("三之一", "五虎退"): _fact(injury="中伤", team_no=3)},
        lookup={"五虎退": (10, "五虎退")})
    messages = run(host)
    assert any("三之一 五虎退 → 修+加速符（中伤）" in m for m in messages)
    assert host.repair_calls == [{"name_y": 200, "need_speed": True}]
    assert host.refreshed == ["五虎退"]  # 加速修好要重新进手入刷新编队
    assert host.last_repair_stats["speedups"] == 1
    queued = next(p for t, p in host.events if t == "repair.queued")
    assert queued["speedup"] is True and queued["injury"] == "中伤"


def test_no_facts_falls_back_to_stamp():
    # 数据通道缺席：画面章兜底，加速部队带章才烧符
    host = RepairHost(
        pages=[([_row("五虎退", prefix="三之一", injury="中伤")], [])],
        config={"blacklist": [], "speedup_teams": [3]}, facts=(),
        lookup={"五虎退": (10, "五虎退")})
    run(host)
    assert host.repair_calls == [{"name_y": 200, "need_speed": True}]


def test_unresolved_row_skipped_and_reported():
    host = RepairHost(
        pages=[([_row("五虎退")], [])], config=CFG, facts=["facts"],
        row_facts={("一之一", "五虎退"): False},
        lookup={"五虎退": (10, "五虎退")})
    messages = run(host)
    assert any("一之一 五虎退 的实例无法对应，跳过这一行" in m for m in messages)
    assert host.repair_calls == []
    assert "✗ 修了 0 把，另有 1 行未能确认身份，已跳过" in messages[-1]


def test_failed_repair_continues_with_next():
    host = RepairHost(
        pages=[([_row("五虎退")], [])], config=CFG, facts=["facts"],
        row_facts={("一之一", "五虎退"): _fact()},
        lookup={"五虎退": (10, "五虎退")}, repair_results=[False])
    messages = run(host)
    assert any("五虎退 没能修成，跳过它继续" in m for m in messages)
    assert host.last_repair_stats["repaired"] == 0


def test_unknown_repair_result_stops_immediately():
    host = RepairHost(
        pages=[([_row("五虎退")], [])], config=CFG, facts=["facts"],
        row_facts={("一之一", "五虎退"): _fact()},
        lookup={"五虎退": (10, "五虎退")}, repair_results=[None])
    messages = run(host)
    assert "完事" not in messages[-1]  # 未确认结果直接收工，不重复点击
    assert host.last_repair_stats["repaired"] == 0


def test_dry_run_scans_without_clicking():
    host = RepairHost(
        pages=[([_row("五虎退")], [])], config=CFG, facts=["facts"],
        row_facts={("一之一", "五虎退"): _fact()},
        lookup={"五虎退": (10, "五虎退")})
    messages = run(host, dry_run=True)
    assert any("一之一 五虎退 → 修（不用加速符） [演习]" in m for m in messages)
    assert host.repair_calls == []       # 演习一个按钮都不点
    assert host.scrolls >= 1             # 演习模式翻页继续认人
    assert "演习结束：共认出 1 把待修刀，黑名单跳过 0 把" in messages[-1]
