# -*- coding: utf-8 -*-
"""测试全局隔离：把整个用户数据目录指到隔离处。

ROI 覆盖层会读用户数据目录里模板实验室的调试产物
（debug/template_lab/code-rois.json）。不隔离的话，真机调试留下的覆盖
会漏进 mock 测试——2026-10-19 本地 40 个编队测试被
formation_editor.list 覆盖污染成 screen_unrecognized，CI 上全绿。

必须用环境变量且必须早于任何 touken import：touken/__init__ 会链式
拉起全部 flows，模块级的 get_roi(...)/路径常量在 import 时就固化，
等 fixture 阶段再 patch 已来不及。runtime_paths 原生支持
MAAMARU_DATA_DIR 环境变量，conftest 顶部设置即可全链路隔离
（ROI 覆盖、状态、日志都不碰真实用户数据）。
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault(
    "MAAMARU_DATA_DIR",
    str(Path(tempfile.gettempdir()) / "maamaru-tests-isolation"))
