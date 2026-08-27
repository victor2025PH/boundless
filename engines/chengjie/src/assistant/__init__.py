# -*- coding: utf-8 -*-
"""assistant — 全局 AI 助手悬浮球（「小智」）后端核心。

P0 范围：产品问答（独立帮助库 RAG + ai_client 容灾链）、一键报障
（复用 bug_tickets 生命周期）、问答台账（自答率/miss 清单）、限频。

设计规格：见 2026-08-19 assistant-ball 方案（意向板 assistant-ball-p0-impl）。
隔离红线：帮助语料独立建库（assistant_help.db），绝不灌进客户话术 KB；
上下文构建按角色过滤，agent 拿不到管理侧信息。
"""
