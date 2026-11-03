"""同一演练循环可由独立画面适配驱动：无 device/config 的宿主跑公共循环。

验证拆分边界（对标 tests/test_repair_split.py）：已有胜场统计、认人
判定（丙子硬躲/极短机动/未知身份）、软弱评分排序、dry_run、真打赢场
计数与再战前复检都在公共层，画面与数据触点全是钩子。
"""
from unittest.mock import patch

from touken.flows.practice_loop import PracticeLoopMixin, _HEISHI_ID


def _member(name="测试刀", type="打刀", level=50, mobility=None, id="known"):
    return {"name": name, "type": type, "level": level,
            "mobility": mobility, "id": id}


def _opponent(members, title="对手", source="data"):
    return {"title": title, "members": members, "source": source}


class PracticeHost(PracticeLoopMixin):
    """没有 device/config/像素坐标的宿主，验证循环不会暗中借用国服画面。"""

    def __init__(self, opponents=None, existing=None, order=None,
                 goto_ok=True, vcfg=None, fight_results=()):
        # row_idx -> opponent dict / None / 逐次结果列表（再战前复检用）
        self.opponents = opponents or {}
        self.existing = existing or {}
        self.order = order
        self.goto_ok = goto_ok
        self.vcfg = vcfg if vcfg is not None else {"max_wins": 3}
        self.fight_results = list(fight_results)
        self.fight_calls = []
        self.inspects = []
        self.events = []
        self._fetched = False

    def _practice_view_config(self, team_no=None, formation_mode=None, formation=None):
        cfg = dict(self.vcfg)
        if team_no is not None:
            cfg["team_no"] = team_no
        return cfg

    def _practice_goto(self):
        yield "[演练] 导航中"
        return self.goto_ok

    def _practice_fetch_rows(self):
        self._fetched = True
        return None

    def _scan_existing_results(self):
        return dict(self.existing)

    def _scan_list_order(self):
        if self.order is not None:
            return list(self.order)
        return sorted(self.opponents)

    def _inspect_opponent(self, row_idx):
        self.inspects.append(row_idx)
        value = self.opponents.get(row_idx)
        if isinstance(value, list):
            return value.pop(0) if value else None
        return value

    def _back_to_list(self):
        pass

    def _ensure_on_list(self):
        return True

    def _fight_one(self, cfg, row_idx):
        self.fight_calls.append((cfg, row_idx))
        result = self.fight_results.pop(0) if self.fight_results else "win"
        yield f"[演练] 打完 {result}"
        return result

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))


def run(host, **kwargs):
    with patch("time.sleep"):
        return list(host.practice_stream(**kwargs))


def test_goto_failure_stops():
    host = PracticeHost(goto_ok=False)
    messages = run(host)
    assert messages[-1] == "[演练] 到达演练失败"
    assert host._fetched is False


def test_dry_run_reports_without_fighting():
    host = PracticeHost(opponents={
        0: _opponent([_member()]),
        1: _opponent([_member(id=_HEISHI_ID)]),
    })
    messages = run(host, dry_run=True)
    assert "认人演习" in messages[0]
    assert any("对手1" in m and "打" in m for m in messages)
    assert any("对手2" in m and "躲" in m and "丙子椒林剑" in m for m in messages)
    assert any("推荐挑战顺序：对手1" in m for m in messages)
    assert "收工：候选 1 队（演习模式，未真打）" in messages[-1]
    assert host.fight_calls == []


def test_existing_wins_meet_target_and_skip():
    host = PracticeHost(existing={0: "win", 1: "win", 2: "win"},
                        opponents={3: _opponent([_member()])})
    messages = run(host, dry_run=False, max_wins=3)
    assert any("已有胜场 3/3，无需重复挑战，收工" in m for m in messages)
    assert host.fight_calls == []
    assert host.inspects == []


def test_existing_wins_count_toward_target():
    host = PracticeHost(existing={0: "win", 1: "lose"},
                        opponents={2: _opponent([_member()])})
    messages = run(host, dry_run=False, max_wins=2)
    assert any("已打 2 场，其中胜利 1 场" in m for m in messages)
    assert host.fight_calls == [({"max_wins": 3}, 2)]
    assert any("胜场 2/2" in m for m in messages)


def test_real_fight_counts_wins_and_records_event():
    host = PracticeHost(
        opponents={i: _opponent([_member()]) for i in range(3)},
        fight_results=["win", "lose", "win"])
    messages = run(host, dry_run=False, max_wins=2)
    assert [row for _, row in host.fight_calls] == [0, 1, 2]
    assert any("胜场 1/2" in m for m in messages)
    assert any("这场结果: lose" in m for m in messages)
    assert "收工：本次新赢 2 场，当前刷新场累计 2 场" in messages[-1]
    results = [p for t, p in host.events if t == "practice.result"]
    assert [p["won"] for p in results] == [True, False, True]
    assert results[-1]["wins"] == 2 and results[-1]["target"] == 2


def test_weakest_first_by_strength_key():
    weak = _opponent([_member()])  # 1 人
    full = _opponent([_member() for _ in range(6)])
    host = PracticeHost(opponents={0: full, 1: weak})
    run(host, dry_run=False, max_wins=1)
    assert host.fight_calls[0][1] == 1  # 人少的先打


def test_reinspect_verdict_change_skips_fight():
    good = _opponent([_member()])
    turned_bad = _opponent([_member(id=_HEISHI_ID)])
    host = PracticeHost(opponents={0: [good, turned_bad]})
    messages = run(host, dry_run=False, max_wins=1)
    assert any("当前阵容不适合挑战，跳过" in m for m in messages)
    assert host.fight_calls == []


def test_inspect_failure_skips_opponent():
    host = PracticeHost(opponents={0: None, 1: _opponent([_member()])})
    messages = run(host, dry_run=True)
    assert any("对手1: 进情报界面失败，跳过" in m for m in messages)
    assert "收工：候选 1 队（演习模式，未真打）" in messages[-1]


# ========== _judge 判定 ==========

def test_judge_empty_members_dodges():
    verdict, reasons = PracticeHost()._judge([])
    assert verdict == "躲" and "怂" in reasons[0]


def test_judge_heishi_dodges():
    verdict, reasons = PracticeHost()._judge([_member(id=_HEISHI_ID)])
    assert verdict == "躲" and any("丙子椒林剑" in r for r in reasons)


def test_judge_short_sword_without_mobility_treated_as_extreme():
    verdict, reasons = PracticeHost()._judge([_member(type="短刀", mobility=None)])
    assert verdict == "躲" and any("当极短处理" in r for r in reasons)


def test_judge_unknown_identity_dodges():
    verdict, reasons = PracticeHost()._judge([_member(type="未知")])
    assert verdict == "躲" and any("身份未确认" in r for r in reasons)


def test_judge_understaffed_is_kind():
    verdict, reasons = PracticeHost()._judge([_member() for _ in range(3)])
    assert verdict == "打" and any("好心人" in r for r in reasons)


def test_judge_full_team_counts_extreme_short_swords():
    members = [_member() for _ in range(5)]
    members.append(_member(type="短刀", mobility=120))
    verdict, reasons = PracticeHost()._judge(members)
    assert verdict == "打" and any("检测到1把极短" in r for r in reasons)


def test_judge_full_team_without_extreme():
    verdict, reasons = PracticeHost()._judge([_member() for _ in range(6)])
    assert verdict == "打" and any("可以打" in r for r in reasons)


# ========== 软弱评分 ==========

def test_strength_key_prefers_fewer_members_then_extreme_then_level():
    host = PracticeHost()
    solo = host._strength_key([_member(level=99)])
    full_plain = host._strength_key([_member(level=10) for _ in range(6)])
    full_extreme = host._strength_key(
        [_member() for _ in range(5)] + [_member(type="短刀", mobility=120, level=80)])
    assert solo < full_plain < full_extreme


def test_score_text_readable():
    host = PracticeHost()
    assert host._score_text((6, 0, 0, 0, 50.0)) == "6人，无极短，平均Lv.50"
    assert "极短2把" in host._score_text((6, 2, 120, 80, 50.0))
    assert "等级读取不足" in host._score_text((1, 0, 0, 0, 999.0))
