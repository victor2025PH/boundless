"""Telegram 原生语音通话（native calls）子系统。

纯函数核心在 ``core``（配置解析 / 接听决策 / 状态机 / 帧数学 / 拟人填充调度），
IO 适配器（pytgcalls 传输、实时语音大脑桥）后续阶段落位，默认整体关闭
（``telegram_calls.enabled``，遵「新子系统默认 false」约定）。
"""
