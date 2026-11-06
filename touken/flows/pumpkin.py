# -*- coding: utf-8 -*-
"""
上层业务：南瓜大作战（刮刮乐/九宫格活动）

两种模式：
  死板版（默认）：不认剪影是谁，翻到谁是谁，全翻完才更新
  智能版（给 watch_names）：每场回来后认剪影——
    认出是目标刀 → 继续刷完这块板子；
    认出不是目标 → 点剪影更新提前换板子（烧 1 枚更新令牌）；
    认不准 → 不管，接着打，下一场回来再认

拆分（docs/dual-server-pipeline.md 阶段 3）：公共循环 pumpkin_loop.py
（令牌预算/看门狗/主循环/二次复核，与服务器无关），国服画面适配与
数据通道 pumpkin_cn.py，日服宿主 touken/jp_pumpkin.py 复用同一循环。
本类组合公共循环与国服视图两层，对外方法面不变。
"""
from .pumpkin_loop import PumpkinLoopMixin
from .pumpkin_cn import CnPumpkinViewMixin


class PumpkinMixin(CnPumpkinViewMixin, PumpkinLoopMixin):
    """南瓜大作战流程。依赖宿主类的 navigate_to_stream、_click_point、battle_loop_stream。"""
