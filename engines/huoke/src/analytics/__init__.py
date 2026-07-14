# -*- coding: utf-8 -*-
"""B 机分析模块 (Messenger 聊天机器人的漏斗/意图/gate 观测扩展)。

独立于 src/host/ 的 fb_store 基础漏斗, 不改共享区 schema, 提供:
  * chat_funnel.reply_rate_by_intent — 各意图的回复率
  * chat_funnel.gate_block_distribution — gate 拒绝原因分布
  * chat_funnel.stranger_conversion_rate — 陌生人转化率
  * chat_funnel.intent_source_coverage — rule/llm 命中率 (P9 预留)
  * chat_funnel.get_funnel_metrics_extended — 一站式扩展漏斗
"""
