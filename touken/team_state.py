"""Client-independent, already validated team observations. Unknown stays unknown."""
from dataclasses import dataclass, replace
import math


INJURY_RANK = {None: 0, "轻伤": 1, "中伤": 2, "重伤": 3}


def injury_reaches_threshold(injury, threshold):
    rank = INJURY_RANK.get(injury, 3) if injury is not None else 3
    return rank >= {"light": 1, "medium": 2, "heavy": 3}.get(str(threshold or "light"), 1)


def captain_rotation_slot(fatigue, margin):
    if not fatigue or 1 not in fatigue:
        return None
    lowest = min(fatigue, key=fatigue.get)
    return lowest if lowest != 1 and fatigue[1] - fatigue[lowest] >= margin else None


@dataclass(frozen=True)
class TeamMemberSnapshot:
    slot: int
    serial_id: str
    name: str | None = None
    hp: int | None = None
    hp_max: int | None = None
    injury: str | None = None
    injury_source: str | None = None
    hp_observed_at: float | None = None
    fatigue: int | None = None
    fatigue_observed_at: float | None = None
    equipment: tuple[bool, bool, bool] | None = None
    equipment_observed_at: float | None = None

    def __post_init__(self):
        if (type(self.slot) is not int or self.slot not in range(1, 7)
                or not isinstance(self.serial_id, str) or not self.serial_id.strip() or self.serial_id == "0"):
            raise ValueError("部队成员身份无效")
        if (self.hp, self.hp_max) != (None, None):
            if (type(self.hp) is not int or type(self.hp_max) is not int
                    or not 0 <= self.hp <= self.hp_max or self.hp_max <= 0):
                raise ValueError("部队生存数据无效")
        elif self.injury is not None:
            raise ValueError("伤势缺少有效生存数据")
        if self.injury not in INJURY_RANK:
            raise ValueError("部队伤势无效")
        if self.fatigue is not None and (type(self.fatigue) is not int or not 0 <= self.fatigue <= 100):
            raise ValueError("部队疲劳数据无效")
        if self.equipment is not None and (not isinstance(self.equipment, tuple)
                or len(self.equipment) != 3 or any(type(value) is not bool for value in self.equipment)):
            raise ValueError("部队刀装数据无效")
        for value, at in ((self.hp, self.hp_observed_at), (self.fatigue, self.fatigue_observed_at),
                          (self.equipment, self.equipment_observed_at)):
            if value is not None and (type(at) not in (int, float) or not math.isfinite(at)):
                raise ValueError("部队数据缺少有效观察时间")


@dataclass(frozen=True)
class TeamSnapshot:
    team_no: int
    members: tuple[TeamMemberSnapshot, ...]
    observed_at: float  # Time of the verified membership, never the read time.

    def __post_init__(self):
        if (type(self.team_no) is not int or self.team_no not in range(1, 6)
                or not isinstance(self.members, tuple) or not self.members
                or any(not isinstance(m, TeamMemberSnapshot) for m in self.members)
                or len({m.slot for m in self.members}) != len(self.members)
                or len({m.serial_id for m in self.members}) != len(self.members)
                or type(self.observed_at) not in (int, float) or not math.isfinite(self.observed_at)):
            raise ValueError("部队名单无效")

    def current(self, *, now, max_age):
        """Fresh membership and individual fields; reading never renews their age."""
        if not 0 <= now - self.observed_at <= max_age:
            return None
        members = []
        for member in self.members:
            changes = {}
            if member.hp is not None and not 0 <= now - member.hp_observed_at <= max_age:
                changes.update(hp=None, hp_max=None, injury=None, injury_source=None, hp_observed_at=None)
            if member.fatigue is not None and not 0 <= now - member.fatigue_observed_at <= max_age:
                changes.update(fatigue=None, fatigue_observed_at=None)
            if member.equipment is not None and not 0 <= now - member.equipment_observed_at <= max_age:
                changes.update(equipment=None, equipment_observed_at=None)
            members.append(replace(member, **changes) if changes else member)
        return replace(self, members=tuple(members))

    @property
    def injury_known(self):
        return all(member.hp is not None for member in self.members)

    @property
    def max_injury(self):
        if not self.injury_known:
            return None
        return max((member.injury for member in self.members), key=INJURY_RANK.get)

    def injury_report(self):
        if not self.injury_known:
            return None
        return {"party_no": self.team_no,
                "members": [{"serial_id": int(m.serial_id) if m.serial_id.isdigit() else m.serial_id,
                             "name": m.name, "label": m.name, "hp": m.hp, "hp_max": m.hp_max,
                             "injury": m.injury, "injury_source": m.injury_source} for m in self.members],
                "max_injury": self.max_injury,
                "observed_epoch": min(m.hp_observed_at for m in self.members),
                "party_observed_epoch": self.observed_at}

    def fatigue_readings(self, *, full_team=False):
        if (full_team and {m.slot for m in self.members} != set(range(1, 7))
                or any(m.fatigue is None for m in self.members)):
            return None
        return {m.slot: {"serial_id": m.serial_id, "name": m.name, "fatigue": m.fatigue}
                for m in self.members}

    def maintenance_facts(self):
        if not self.injury_known:
            return None
        fatigue = self.fatigue_readings()
        return {"fatigue": {slot: row["fatigue"] for slot, row in fatigue.items()} if fatigue else None,
                "equipment": ({m.serial_id: m.equipment for m in self.members}
                              if all(m.equipment is not None for m in self.members) else None)}


def maintenance_reasons(facts, *, rotate_captain, margin, auto_equip, equipment_baseline):
    """Only verified fatigue gaps and destroyed equipped slots need a trip home."""
    reasons = []
    if facts is None:
        return reasons
    fatigue = facts["fatigue"]
    if rotate_captain and captain_rotation_slot(fatigue, max(1, int(margin))) is not None:
        reasons.append("换队长")
    equipment = facts["equipment"]
    if auto_equip and equipment_baseline and equipment and equipment.keys() == equipment_baseline.keys():
        if any(needed and not current for serial in equipment
               for needed, current in zip(equipment_baseline[serial], equipment[serial])):
            reasons.append("补刀装")
    return reasons

