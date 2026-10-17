"""Shared injury recovery decisions; callers own navigation and safe departure."""
from dataclasses import dataclass

from .game_state import available_events, injury_report


@dataclass(frozen=True)
class RecoveryProgress:
    target: int
    completed: int

    def __post_init__(self):
        if not 0 <= self.completed <= self.target:
            raise ValueError("出阵进度无效，不能猜测剩余次数")

    @property
    def remaining(self):
        return self.target - self.completed


@dataclass(frozen=True)
class RecoveryOutcome:
    resume: bool
    remaining: int
    reason: str
    attempted: bool = False
    repaired: int = 0
    speedups: int = 0


def current_team_injury(agent, team_no):
    """Unknown is distinct from a freshly confirmed healthy team."""
    report = injury_report(available_events(agent.maa), team_no)
    return (False, None) if report is None else (True, report["max_injury"])


def recover_injury_stream(agent, *, team_no, injury, threshold, action,
                          progress, tag, repair_heavy_on_stop=False):
    """Handle an injury only after the caller has reached a safe stopping point.

    Resume means re-enter the shared safety checks, never click departure here.
    Normal sorties retain their existing heavy-injury slow-repair-on-stop rule;
    Osaka's stop option continues to return home without arranging repairs.
    """
    remaining = progress.remaining
    if remaining == 0:
        return RecoveryOutcome(False, remaining, "complete")
    known, fresh = current_team_injury(agent, team_no)
    if known:
        if not fresh or not agent._injury_reaches_threshold(fresh, threshold):
            yield f"{tag} 当前游戏数据确认部队已满足出阵条件，旧伤势记录已翻篇，继续剩余 {remaining} 次"
            return RecoveryOutcome(True, remaining, "ready")
        injury = fresh
    action = str(action or "continue")
    action = {"true": "continue", "1": "continue", "false": "stop", "0": "stop"}.get(action, action)
    if action not in {"stop", "repair_stop", "continue"}:
        yield f"{tag} ✗ 伤势处理设置无效，停止出阵"
        return RecoveryOutcome(False, remaining, "invalid_policy")
    if action == "stop" and not (repair_heavy_on_stop and injury == "重伤"):
        yield f"{tag} 已达到停止条件，按设置不进行手入，收工"
        return RecoveryOutcome(False, remaining, "policy_stop")
    repair_and_stop = action != "continue"
    yield (f"{tag} 开始手入，不用加速符，完成后收工" if repair_and_stop
           else f"{tag} 开始手入并加速当前部队，之后复查剩余 {remaining} 次")
    # An early exit must not reuse statistics from a previous repair session.
    agent.last_repair_stats = {"repaired": 0, "speedups": 0, "skipped": 0, "unresolved": 0}
    yield from agent.repair_stream(dry_run=False,
        use_speedup=False if repair_and_stop else None,
        speedup_teams=None if repair_and_stop else [team_no])
    stats = agent.last_repair_stats
    repaired, speedups = int(stats.get("repaired", 0)), int(stats.get("speedups", 0))

    def outcome(resume, reason):
        return RecoveryOutcome(resume, remaining, reason, True, repaired, speedups)

    if repair_and_stop and repaired > 0:
        yield f"{tag} 手入已安排，按设置收工"
        return outcome(False, "repair_stop")
    known, fresh = current_team_injury(agent, team_no)
    if known and (not fresh or not agent._injury_reaches_threshold(fresh, threshold)):
        if repair_and_stop:
            yield f"{tag} 当前部队已满足出阵条件，按设置收工"
            return outcome(False, "repair_stop")
        yield f"{tag} 游戏数据确认可以继续，剩余 {remaining} 次仍须重新过安全出阵检查"
        return outcome(True, "ready")
    if known:
        yield f"{tag} ✗ 手入后仍检测到{fresh}，可能有保护名单或未加速成员，停止；不再往返重试"
        return outcome(False, "still_injured")
    yield f"{tag} ✗ 未能确认手入后当前部队的伤势，停止；不按修复数量猜测可以出阵"
    return outcome(False, "unknown")
