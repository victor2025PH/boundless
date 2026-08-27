"""智能养号执行引擎（工具箱「智能养号」P1，2026-08-21）。

镜像 proactive_care 的成熟模式：纯调度核心（nurture_scheduler）+ dry_run→go_live→pause
状态机 + 常备接线 + 配置热闸（nurture_engine）。默认全关；go_live 只对金丝雀白名单账号
产生真实平台动作，且严格尊重 kill-switch / 生命周期风险态。
"""
