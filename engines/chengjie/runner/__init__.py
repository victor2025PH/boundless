# -*- coding: utf-8 -*-
"""小智「Windows 操控 runner」独立子系统（实施91 手②）。

`pc_runner.py` 是受控机上常驻的服务本体；**独立可打包**（仅 stdlib + 可选
uiautomation），可整目录拷到目标机 `python -m runner.pc_runner` 或
`python pc_runner.py` 直接跑，不依赖 src/。小智后端经 src/assistant/runner_client.py
调用它。
"""
