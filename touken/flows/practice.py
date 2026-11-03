# -*- coding: utf-8 -*-
"""
上层业务：演练（演习场）——认人避战版

规矩（用户亲授）：
  1. 对手列表显示的队长的极化状态不代表全队，必须点进去看配置
  2. 硬性躲避：丙子椒林剑和识别不清的成员
  3. 先看完五队再排序：人数少优先；都有极短时比较机动、等级；最后看平均等级
  4. 每天只需赢 3 场，对手 3 点/15 点刷新，一次 5 队，挑软的捏
  5. 出战用部队二（极短队），阵形选逆行阵（抢机动先手）

技术要点（血泪校准）：
  - 游戏编号优先确定刀种与极化；以下机动判定仅用于画面回退：
    名册刀种是短刀 + 机动 >= 100
    （普刀全数值封顶两位数；极短机动 110+，隔了一个数量级）
  - 极化樱花在情报界面会被白底肖像骗（一文字则宗的花瓣背景 0.84 误报），
    所以在情报界面不靠樱花，只信名册 + 机动
  - 数值条每格 55.7px 宽，机动是第 4 格 x[754,806]——窗口偏 1px 都会
    把隔壁防御的尾数读进来，别改
  - 图标中心行距 94px，第一行 cy=152

拆分（docs/dual-server-pipeline.md 阶段 3）：公共循环 practice_loop.py
（决策与编排，与服务器无关），国服画面适配与数据通道 practice_cn.py，
本类组合两层，对外方法面不变。
"""
from .practice_loop import PracticeLoopMixin
from .practice_cn import CnPracticeViewMixin


class PracticeMixin(CnPracticeViewMixin, PracticeLoopMixin):
    """演练。依赖宿主类的 navigate_to_stream、_click_point、_skip_point。"""
