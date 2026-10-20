# -*- coding: utf-8 -*-
"""成绩单翻车词表判定：误伤白名单的回归测试。"""
from touken.flows.report_judge import _is_fail


def test_light_injury_fallback_notice_is_not_fail():
    # 10-20 假红冤案：刷花轻伤停时游戏没有对应设置项，脚本改用行军监控
    # 执行——这是正常功能说明，被词表的「停止」误伤导致刷花假翻车。
    assert not _is_fail('[出阵] 游戏没有轻伤停止设定，改用脚本行军执行轻伤停止')


def test_real_stop_still_fails():
    assert _is_fail('[刷花] 换入后未确认疲劳≤49，停止')
