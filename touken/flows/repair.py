# -*- coding: utf-8 -*-
"""
上层业务：手入（修复）——黑名单版

规矩（用户亲授）：
  1. 游戏数据确认实例、血量与部队；画面文字用于定位列表行。
     游戏数据缺失时保留原来的画面读取。
  2. 黑名单 = 碰瓷队的极化太刀——他们带伤上班是故意的
     （中伤挨打触发真剑必杀），看到名字就跳过，绝对不修
  3. speedup_teams 里的部队（默认部队三=挖地队）：
     行首编号是"三之x"的，修的时候勾加速符秒修，修完继续打；
     其他队的就慢慢修，加速符省着
  4. 加速符决策权归游戏数据（JSON 事实）：数据确认轻伤及以上才烧符，
     画面伤势章只作日志旁证——章与数据不一致记日志，不改变决策。
     数据分档最细只有轻伤，hp≥90% 的"轻伤"按擦伤处理不烧符
     （老规矩"没章不烧符"的数据版）；数据缺失/过期时回到老规矩：
     没章不烧符，擦伤刀慢慢修
     （2026-08-20 实战血泪：擦伤刀白吃了三张加速符）

拆分（docs/dual-server-pipeline.md 阶段 3）：公共循环 repair_loop.py
（决策与编排，与服务器无关），国服画面适配与数据通道 repair_cn.py，
本类组合两层，对外方法面不变。
"""
from .repair_loop import RepairLoopMixin
from .repair_cn import CnRepairViewMixin


class RepairMixin(CnRepairViewMixin, RepairLoopMixin):
    """手入。依赖宿主类的 navigate_to_stream、_click_point。"""
