# -*- coding: utf-8 -*-
"""Messenger 召回体检报告 —— 判词 + 文本渲染纯函数层（P3 2026-08-15）。

输入 = ``fb_store.messenger_recall_summary`` 的 dict, 输出 = 结论判词 + 文本
报告。**零 DB 依赖**, 便于离线夹具测试。

存在理由: P0/P1/P2 全是「离线推断的启发式」(未读关键词/通知解析/预览指纹/
搜索校验), 是否真的有效只能靠生产运行数据自证。本报告把散落的计数聚合成
**可直接读的结论**(像不像有效、误点高不高、身份错乱严不严重), 而不是让人对着
一堆数字自己猜——决定 P3 后续(身份层/自适应限流)做不做、怎么做的数据依据。
"""
from __future__ import annotations

from typing import Any, Dict, List

# 判词阈值 (依据: 保守起步, 上线后按实际分布再校准)
MIN_RUNS = 20          # 样本不足则只观察不下结论
MIN_PROCESSED = 20     # 处理量太少, 率不可信
RESCUE_HIGH = 0.30     # 救回占比 >30% = 未读判定漏读严重
SEARCH_MISMATCH_HIGH = 0.20   # 搜索误点 >20% = 该收紧
TITLE_MISMATCH_HIGH = 0.10    # 身份错乱 >10% = P3 身份层该做
EMPTY_HIGH = 0.25      # 空读 >25% = 提取启发式/页面状态有问题


def recall_verdicts(summary: Dict[str, Any]) -> List[Dict[str, str]]:
    """把聚合 summary 转成结论判词列表 [{level, text}]。

    level ∈ {ok, info, warn}: warn=需要行动, info=值得关注, ok=健康/占位。
    样本不足时只给一条观察判词 (不对噪声下结论)。
    """
    runs = int(summary.get("runs", 0) or 0)
    totals = summary.get("totals", {}) or {}
    rates = summary.get("rates", {}) or {}
    processed = int(totals.get("unread_processed", 0) or 0)

    if runs < MIN_RUNS:
        return [{
            "level": "info",
            "text": f"样本不足（{runs}/{MIN_RUNS} 次运行），继续观察，暂不下结论。",
        }]

    out: List[Dict[str, str]] = []

    # ① 未读判定召回是否够 (救回占比)
    rescue = rates.get("rescue_rate", 0.0)
    rescued = int(summary.get("rescued", 0) or 0)
    if processed >= MIN_PROCESSED and rescue >= RESCUE_HIGH:
        out.append({
            "level": "warn",
            "text": (f"未读判定召回不足：{rescue:.0%} 的处理（{rescued}/{processed}）"
                     f"靠通知/预览/搜索救回。说明单靠未读判定会大量漏读——"
                     f"P1/P2 正在兜住，但应排查未读判定为何漏（多语关键词/badge 结构）。"),
        })
    elif processed >= MIN_PROCESSED:
        out.append({
            "level": "ok",
            "text": (f"未读判定召回健康：仅 {rescue:.0%} 靠兜底信号救回，"
                     f"主门（未读判定）覆盖良好。"),
        })

    # ② 搜索误点率
    so = int(totals.get("search_opened", 0) or 0)
    sm = int(totals.get("search_mismatch", 0) or 0)
    smr = rates.get("search_mismatch_rate", 0.0)
    if (so + sm) >= 10 and smr >= SEARCH_MISMATCH_HIGH:
        out.append({
            "level": "warn",
            "text": (f"搜索误点率偏高：{smr:.0%}（{sm}/{so + sm}）搜索进入的是错误"
                     f"的人（已被校验拦下、未误发）。建议调低 max_search_opens 或"
                     f"在高误点 phase 配 0。"),
        })
    elif (so + sm) >= 10:
        out.append({
            "level": "ok",
            "text": f"搜索打开质量良好：误点率 {smr:.0%}，屏外补捞 {so} 次有效。",
        })

    # ③ 身份错乱频度 (标题校验触发)
    tm = int(totals.get("title_mismatch", 0) or 0)
    tmr = rates.get("title_mismatch_rate", 0.0)
    if processed >= MIN_PROCESSED and tmr >= TITLE_MISMATCH_HIGH:
        out.append({
            "level": "warn",
            "text": (f"身份错乱疑高：{tmr:.0%}（{tm}/{processed}）会话进入后标题与"
                     f"列表名不符（已运行时纠正）。频度足以支撑做持久身份层"
                     f"（thread 令牌同名消歧）。"),
        })

    # ④ 空读率
    er = rates.get("empty_rate", 0.0)
    ee = int(totals.get("empty_extract", 0) or 0)
    if processed >= MIN_PROCESSED and er >= EMPTY_HIGH:
        out.append({
            "level": "warn",
            "text": (f"空读率偏高：{er:.0%}（{ee}/{processed}）打开了却没提取到文本。"
                     f"排查消息提取启发式或页面加载时序。"),
        })

    # ⑤ 屏外漏读候选 (还没被搜索打开的量)
    off = int(totals.get("notif_offscreen", 0) or 0)
    if off > 0:
        out.append({
            "level": "info",
            "text": (f"屏外漏读候选累计 {off} 人次（通知有活动但不在列表）；"
                     f"其中 {so} 次已由搜索补捞。"),
        })

    if not out:
        out.append({"level": "ok", "text": "召回链路各项指标健康。"})
    return out


_LEVEL_MARK = {"ok": "[OK]", "info": "[i]", "warn": "[!]"}


def format_text_report(summary: Dict[str, Any]) -> str:
    """渲染人类可读文本报告。"""
    totals = summary.get("totals", {}) or {}
    rates = summary.get("rates", {}) or {}
    runs = int(summary.get("runs", 0) or 0)
    days = summary.get("days", 0)
    dev = summary.get("device_id") or "(全部设备)"

    lines: List[str] = []
    lines.append("=" * 60)
    lines.append(f"Messenger 召回体检 — 近 {days} 天 / 设备 {dev}")
    lines.append("=" * 60)
    lines.append(f"运行次数: {runs}")
    lines.append("")
    lines.append("[ 处理量 ]")
    lines.append(f"  列出会话   : {totals.get('conversations_listed', 0)}")
    lines.append(f"  判为未读   : {totals.get('unread_detected', 0)}")
    lines.append(f"  实际处理   : {totals.get('unread_processed', 0)}")
    lines.append("")
    lines.append("[ 召回来源 (未读判定漏判、被兜底救回) ]")
    lines.append(f"  通知强制   : {totals.get('notif_forced', 0)}")
    lines.append(f"  预览变化   : {totals.get('preview_forced', 0)}")
    lines.append(f"  屏外搜索   : {totals.get('search_opened', 0)}")
    lines.append(f"  救回合计   : {summary.get('rescued', 0)}"
                 f"  (救回占比 {rates.get('rescue_rate', 0.0):.0%})")
    lines.append("")
    lines.append("[ 质量风险 ]")
    lines.append(f"  标题不符   : {totals.get('title_mismatch', 0)}"
                 f"  (身份错乱率 {rates.get('title_mismatch_rate', 0.0):.0%})")
    lines.append(f"  搜索误点   : {totals.get('search_mismatch', 0)}"
                 f"  (误点率 {rates.get('search_mismatch_rate', 0.0):.0%})")
    lines.append(f"  搜索失败   : {totals.get('search_failed', 0)}")
    lines.append(f"  空读       : {totals.get('empty_extract', 0)}"
                 f"  (空读率 {rates.get('empty_rate', 0.0):.0%})")
    lines.append(f"  去重拦截   : {totals.get('dedup_skipped', 0)}")
    lines.append(f"  屏外候选   : {totals.get('notif_offscreen', 0)}")
    lines.append(f"  运行内错误 : {totals.get('errors', 0)}")
    lines.append("")
    lines.append("[ 结论 ]")
    for v in recall_verdicts(summary):
        mark = _LEVEL_MARK.get(v.get("level", "info"), "[i]")
        lines.append(f"  {mark} {v.get('text', '')}")
    lines.append("=" * 60)
    return "\n".join(lines)
