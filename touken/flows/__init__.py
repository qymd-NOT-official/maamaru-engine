# -*- coding: utf-8 -*-
"""
上层业务入口：各玩法流程
每个文件管一件事，名字写清楚就是专用——要加新玩法就在这里新建文件。
"""

from .login import LoginMixin
from .battle import BattleMixin
from .rewards import RewardsMixin
from .raid import RaidMixin
from .pumpkin import PumpkinMixin
from .naihanka import NaihankaMixin
from .sortie import SortieMixin
from .expedition import ExpeditionMixin
from .repair import RepairMixin
from .practice import PracticeMixin
from .signin import SigninMixin
from .daily import DailyMixin
from .smith import SmithMixin
from .synthesize import SynthesizeMixin
from .sugar import SugarMixin
from .inbox_scan import InboxScanMixin
from .sakura import SakuraMixin
from .logout import LogoutMixin
from .snapshot import SnapshotMixin
from .osaka import OsakaMixin
from .edocastle import EdocastleMixin
from .hanafuda import HanafudaMixin
from .sword_inventory import SwordInventoryMixin
from .team_roster import TeamRosterMixin
from .formation_editor import FormationEditorMixin

__all__ = ["LoginMixin", "BattleMixin", "RewardsMixin", "RaidMixin", "PumpkinMixin", "NaihankaMixin", "SortieMixin", "ExpeditionMixin", "RepairMixin", "PracticeMixin", "SigninMixin", "DailyMixin", "SmithMixin", "SynthesizeMixin", "SugarMixin", "InboxScanMixin", "SakuraMixin", "LogoutMixin", "SnapshotMixin", "OsakaMixin", "EdocastleMixin", "HanafudaMixin", "SwordInventoryMixin", "TeamRosterMixin", "FormationEditorMixin"]
