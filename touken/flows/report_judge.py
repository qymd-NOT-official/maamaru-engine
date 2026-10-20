# -*- coding: utf-8 -*-
"""
成绩单判分 —— 日课和各玩法流式消息的红绿判定（翻车词表）

血泪红线：「没跑成必须 ✗，不许假绿」。判红宁可严一点，误伤由白名单捞回。
这里从 daily.py 抽出共用：日课、自定义工作流的节点判分都复用同一份词表。
"""

import re

# 白名单：带着"失败/停/跳过"等字样、但其实没翻车的消息，先放行再判红。
_PASS_RE = re.compile(
    r"已达到停止条件|"                      # 挖地/出阵按约定主动收工
    r"今天签过了|今天已经刀解过了|"          # 幂等跳过
    r"没有远征回来|没有启用常用安排|没有可领奖励|"
    r"无需重复挑战|"                        # 演练胜场已够
    r"可能派遣失败也可能已回本丸|"           # 远征结果不确定，日志里另有 ⚠️ 详述
    r"游戏判定「失败」|"                    # 远征结算的游戏结果，照实记账不是翻车
    r"这局当死板版刷|"                      # 南瓜剪影库没加载，降级刷不是翻车
    r"收场再补|"                            # 挖地开工小判没读到，收工时补拍
    r"小判金额未识别|"                      # 异去提灯补充已成功，只是金额没读出来
    r"游戏自动行军已经停止|"                # 游戏自身的保护性停军，脚本会安全收尾
    r"没有轻伤停止设定，改用脚本行军执行|"  # 刷花等轻伤停的常规替代路径（10-20 假红冤案）
    r"停止跳过并处理阵形|"                  # 江户城入场遇敌的正常操作播报（9-04 假红冤案）
    r"不影响"                               # 明确标注不影响主线的旁路失败（快照/推送等）
)

# 判定步骤翻车的消息特征：没 x 到/找不到/未配置/停止/，停 收场等都是各流程
# 自己约定的"中止"话术，一律判红；个别误伤用上面的白名单捞回来。
_FAIL_RE = re.compile(
    r"失败|翻车|没能|没等到|没打开|没找到|没出现|没看到|没读到|没读全|没识别|"
    r"找不到|未配置|尚未配置|配置里没有|未开始|未找到|未识别|未确认|未检测|"
    r"无法|不可用|停止|卡死|卡在|强制停|没生效|没回来|"
    r"，停(?=[，。]|$)|超时|放弃|刀装未满警告|"
    r"可能成了也可能没成"
)


def _is_fail(msg: str) -> bool:
    if _PASS_RE.search(msg):
        return False
    return bool(_FAIL_RE.search(msg))


def _is_success_status(status: str) -> bool:
    """Detailed successes such as ``✓ 本次领取成功`` are still green."""
    return str(status).lstrip().startswith("✓")


def _practice_report_status(msg: str, current=None):
    """演练专项判分：打了却一场没赢（认不出人/阵形卡死/全输）不算绿。"""
    if "无需重复挑战，收工" in msg:
        return "✓ 已有胜场够数"
    if "收工：本次新赢 0 场" in msg:
        return "✗ 一场没赢"
    return current


def _equip_warning_status(msg: str, current=None):
    if "刀装未满警告" not in msg:
        return current
    if "没能安全取消" in msg:
        return "✗ 刀装未满，取消整备失败，出阵停止"
    return "⚠ 刀装未满，已取消出阵并跳过"


def _shop_report_status(msg: str, current=None):
    if "今日暖心礼包已售罄" in msg:
        return "✓ 此前已领取（售罄）"
    if "领取成功" in msg:
        return "✓ 本次领取成功"
    if "未找到暖心礼包" in msg:
        return "✗ 未找到暖心礼包"
    if "未识别到领取按钮" in msg:
        return "✗ 未识别到领取按钮，未点击"
    if "未检测到0价格弹窗" in msg:
        return "✗ 未确认0价，已取消"
    if "本次未购买" in msg:
        return "✗ 购买未确认，已取消"
    return current


def _snapshot_report_status(msg: str, current=None):
    if "盘点不完整" in msg:
        return "⚠ 小判未读到"
    if "没能确认本轮盘点" in msg:
        return "✗ 未完成本轮盘点"
    return current


def _sugar_report_status(msg: str, current=None):
    """炼糖专项判分：邮箱真清空了才算绿；撞上限/刀位满收工必须如实标出
    （9-05 冤案：邮箱 999+ 封、刀位钉死满员，一趟只消化几把，
    收尾话术一个词不沾翻车词表，成绩单装绿）。"""
    if "持续没有进展" in msg:
        return "⚠ 炼糖停滞：未确认清完，请检查游戏画面"
    if "邮件里没刀了，收工" in msg:
        return "✓ 邮箱已清空"
    if "领到的刀都没重刀可喂，收工" in msg:
        return "✓ 没有可喂的重刀"
    if "到安全上限" in msg:
        return "⚠ 邮箱没清完：刀位太满，先刀解腾位置再多跑几趟"
    if "所持满了领不动、也没重刀可喂" in msg:
        return "⚠ 刀位满了领不动，先去刀解腾位"
    return current
