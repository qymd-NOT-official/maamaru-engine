# -*- coding: utf-8 -*-
"""玩法流程的受控退出信号。"""


from dataclasses import dataclass


class FlowAborted(RuntimeError):
    """玩法已安全收尾，但因游戏状态异常不能算作正常完成。"""


@dataclass(frozen=True)
class BattleProgress:
    target: int
    completed: int

    def __post_init__(self):
        if not 0 <= self.completed <= self.target:
            raise ValueError("出阵进度无效，不能猜测剩余次数")

    @property
    def remaining(self):
        return self.target - self.completed
