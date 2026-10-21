# -*- coding: utf-8 -*-
"""
上层业务：签到（公告里领每日奖励）

路径（真机校准）：
  目录 → 公告（菜单右栏）→ 一般直接落在签到页 → OCR 领取奖励 → 道具弹窗关掉 → 关公告
  落不在签到页就点顶部"签到"标签
  没有领取奖励按钮 = 今天签过了，跳过——幂等，每天随便跑。

实现已收编为官方流程 resource/base/flows/signin.json（流程工坊里可见的展品，
步骤怎么拼的一目了然），这里只剩薄壳：找官方流程 → 交给流程引擎跑。
日课、工作流积木等所有调用方自动继承，行为对等测试在 tests/test_signin_flow.py。
"""

from ..flow_engine import FlowError, find_flow, normalize_flow, run_flow

SIGNIN_FLOW_ID = "builtin-signin"


class SigninMixin:
    """签到。依赖宿主类的 _open_menu、device、current_location（经流程引擎调用）。"""

    def signin_stream(self):
        """
        流式签到

        Yields:
            str: 执行状态消息
        """
        flow = find_flow(SIGNIN_FLOW_ID)
        if flow is None:
            yield ("[签到] ✗ 找不到官方签到流程"
                   "（resource/base/flows/signin.json 缺失），跳过")
            return
        try:
            plan = normalize_flow(flow)
        except FlowError as exc:
            yield f"[签到] ✗ 官方签到流程校验翻车: {exc}"
            return
        yield from run_flow(self, plan)
