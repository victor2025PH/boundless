# -*- coding: utf-8 -*-
"""
话术模板权重自动优化器 v2。

数据来源（双轨并行）：
  Track A — A/B 实验系统（ab_testing.py）
    · 从 experiment_events 表读取 dm_template_style 实验的各变体事件
    · 统计 sent / reply_received / converted 比例
    · 找出最优变体

  Track B — LeadsStore 交互记录（leads.db）
    · 从 interactions.metadata.ab_variant 读取每次 DM 使用的变体
    · 关联 lead 最终状态（converted/qualified/contacted）
    · 计算各变体的转化漏斗

综合两轨结果，更新 chat_messages.yaml 中 message_variants 的权重。
每天凌晨由 job_scheduler 自动调用。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from src.host.device_registry import config_file, data_file

logger = logging.getLogger(__name__)

_CHAT_MESSAGES_PATH = config_file("chat_messages.yaml")
_AB_WINNER_PATH = data_file("ab_winner.json")

# 最低样本量：变体被使用次数低于此值时，跳过权重调整
_MIN_SAMPLE_SIZE = 10
# 权重边界
_WEIGHT_MIN, _WEIGHT_MAX = 15, 85


def optimize_template_weights(dry_run: bool = False) -> dict:
    """
    综合 A/B 实验数据 + LeadsStore 交互记录，优化话术模板权重。

    Returns:
        {
            "updated": int,           # 更新的变体数
            "best_variant": str,      # 当前最优 A/B 变体
            "ab_analysis": dict,      # A/B 实验完整统计
            "weight_changes": list,   # 各变体权重变化详情
            "dry_run": bool,
        }
    """
    result: dict = {
        "updated": 0, "dry_run": dry_run,
        "best_variant": "", "ab_analysis": {}, "weight_changes": [],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # ── Track A：A/B 实验统计 ──
    ab_stats = _get_ab_experiment_stats()
    result["ab_analysis"] = ab_stats

    if ab_stats.get("best_variant"):
        result["best_variant"] = ab_stats["best_variant"]
        logger.info("[TemplateOptimizer] A/B 最优变体: %s (reply_rate=%.3f)",
                    ab_stats["best_variant"],
                    ab_stats["variants"].get(ab_stats["best_variant"], {}).get("reply_rate", 0))

    # ── Track B：LeadsStore 交互记录统计 ──
    leads_stats = _get_leads_interaction_stats(lookback_days=30)

    # ── 合并两轨数据，计算各 message_variant 的综合得分 ──
    try:
        import yaml
    except ImportError:
        logger.warning("[TemplateOptimizer] PyYAML 未安装")
        return {**result, "error": "pyyaml not installed"}

    if not _CHAT_MESSAGES_PATH.exists():
        return {**result, "error": "chat_messages.yaml not found"}

    with open(_CHAT_MESSAGES_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    variants: List[dict] = config.get("message_variants", [])
    if not variants:
        return {**result, "reason": "no message_variants in config"}

    changes = _calculate_and_apply_weights(
        variants, ab_stats, leads_stats, dry_run=dry_run
    )
    result["weight_changes"] = changes
    result["updated"] = len(changes)

    if changes and not dry_run:
        with open(_CHAT_MESSAGES_PATH, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)
        logger.info("[TemplateOptimizer] 已更新 %d 个 variant 权重", len(changes))

    # ── 写入胜者索引文件 — 供 _generate_chat_message() 70/30 exploitation 使用 ──
    if ab_stats.get("best_variant") and not dry_run:
        _write_ab_winner(ab_stats["best_variant"], variants)

    # ── 记录本次优化事件到 A/B 系统（便于追踪优化历史） ──
    try:
        from src.host.ab_testing import get_ab_store
        ab = get_ab_store()
        ab.record("dm_template_style", result.get("best_variant", "unknown"),
                  "weight_optimized",
                  metadata={"changes": len(changes), "dry_run": dry_run})
    except Exception:
        pass

    return result


def _write_ab_winner(best_variant: str, variants: List[dict]) -> None:
    """
    将 A/B 实验胜者的模板索引写入 data/ab_winner.json。

    _generate_chat_message() 读取此文件实现 70% exploitation + 30% exploration。
    映射规则与 _calculate_and_apply_weights 一致：hash(variant) % n。
    """
    import json
    n = len(variants)
    if n == 0:
        return

    if best_variant == "control":
        winner_idx = None  # control = 均匀，不偏向某模板
    else:
        winner_idx = hash(best_variant) % n

    winner_data = {
        "best_variant": best_variant,
        "winner_idx": winner_idx,
        "n_variants": n,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    try:
        _AB_WINNER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_AB_WINNER_PATH, "w", encoding="utf-8") as f:
            json.dump(winner_data, f, ensure_ascii=False, indent=2)
        logger.info("[TemplateOptimizer] 胜者写入 ab_winner.json: variant=%s idx=%s",
                    best_variant, winner_idx)
    except Exception as e:
        logger.warning("[TemplateOptimizer] 写入 ab_winner.json 失败: %s", e)


# ── Track A: A/B 实验统计 ──

def _get_ab_experiment_stats() -> dict:
    """
    从 A/B 实验系统读取 dm_template_style 实验的统计数据。
    同时聚合集群中所有 Worker 节点的数据（通过 /experiments/{name}/analyze 端点）。
    """
    try:
        from src.host.ab_testing import get_ab_store
        ab = get_ab_store()
        analysis = ab.analyze("dm_template_style") or {}

        # ── 聚合集群 Worker 节点 A/B 数据 ──
        analysis = _merge_worker_ab_stats("dm_template_style", analysis)

        if not analysis:
            return {"variants": {}, "best_variant": ""}

        # 找最优变体（降低 min_samples 到 3，因群控环境数据量有限）
        best_variant = ab.best_variant("dm_template_style",
                                       metric="reply_received", min_samples=3)
        # 若 reply_received 样本不足，回退到全量数据中的最高 reply_rate
        if best_variant == "control":
            best_by_rate = max(
                analysis.items(),
                key=lambda kv: kv[1].get("reply_rate", 0) if kv[1].get("sent", 0) >= 3 else -1,
                default=("control", {}),
            )
            if best_by_rate[1].get("reply_rate", 0) > 0:
                best_variant = best_by_rate[0]

        # Z-test 置信度：样本足够时检验统计显著性
        confidence_map = _ztest_winner_confidence(analysis)
        for vname, conf in confidence_map.items():
            if vname in analysis:
                analysis[vname]["winner_confidence"] = conf

        # 若胜者置信度 < 60%（样本太少）则保持 control 以防过拟合
        winner_conf = confidence_map.get(best_variant, 0.0)
        if winner_conf < 0.60 and best_variant != "control":
            logger.info("[TemplateOptimizer] 胜者 %s 置信度 %.1f%% < 60%%，保持 control",
                        best_variant, winner_conf * 100)
            best_variant = "control"

        return {
            "variants": analysis,
            "best_variant": best_variant or "control",
            "winner_confidence": winner_conf,
            "experiment": "dm_template_style",
        }
    except Exception as e:
        logger.debug("[TemplateOptimizer] A/B 统计获取失败: %s", e)
        return {"variants": {}, "best_variant": ""}


def _ztest_winner_confidence(variants: dict) -> Dict[str, float]:
    """
    对各 A/B 变体进行两比例 Z 检验，返回各变体相对 control 的置信度 (0~1)。

    公式：Z = (p1 - p2) / sqrt(p_pooled*(1-p_pooled)*(1/n1+1/n2))
    置信度 = Φ(|Z|)（正态累积分布函数近似）

    当样本量 < 10 时返回 0（不可信）。
    """
    import math

    def _phi(z: float) -> float:
        """Φ(z) 近似 — Abramowitz & Stegun 7.1.26，精度 <1.5e-7。"""
        sign = 1 if z >= 0 else -1
        z = abs(z)
        t = 1.0 / (1.0 + 0.2316419 * z)
        poly = t * (0.319381530 + t * (-0.356563782 + t * (
            1.781477937 + t * (-1.821255978 + t * 1.330274429))))
        return 0.5 + sign * (0.5 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-z * z / 2) * poly)

    control = variants.get("control", {})
    n_ctrl = control.get("sent", 0)
    r_ctrl = control.get("reply_received", 0)
    p_ctrl = r_ctrl / n_ctrl if n_ctrl > 0 else 0.0

    confidence: Dict[str, float] = {}
    for vname, vdata in variants.items():
        n = vdata.get("sent", 0)
        r = vdata.get("reply_received", 0)
        if n < 10 or n_ctrl < 10:
            confidence[vname] = 0.0
            continue
        p = r / n
        p_pooled = (r + r_ctrl) / (n + n_ctrl)
        denom = math.sqrt(p_pooled * (1 - p_pooled) * (1 / n + 1 / n_ctrl))
        if denom == 0:
            confidence[vname] = 0.0
            continue
        z = (p - p_ctrl) / denom
        conf = 2 * _phi(abs(z)) - 1  # 双尾转单边置信度
        confidence[vname] = round(max(0.0, min(1.0, conf)), 4)

    return confidence


def _merge_worker_ab_stats(experiment: str, local_stats: dict) -> dict:
    """
    从集群 Worker 节点拉取 A/B 实验数据并与本地数据合并。
    使用 /experiments/{name}/analyze 端点（无需鉴权，仅内网访问）。
    """
    import json as _json
    import urllib.request as _ur

    merged = {v: dict(d) for v, d in local_stats.items()}

    worker_urls = _discover_worker_urls()
    for url in worker_urls:
        try:
            endpoint = f"{url}/experiments/{experiment}/analyze"
            resp = _ur.urlopen(_ur.Request(endpoint), timeout=5)
            data = _json.loads(resp.read().decode())
            remote_variants = data.get("variants", {})
            for variant, stats in remote_variants.items():
                if variant not in merged:
                    merged[variant] = {}
                # 合并：对数值型字段求和，reply_rate 重新计算
                for key in ("sent", "reply_received", "converted"):
                    merged[variant][key] = (
                        merged[variant].get(key, 0) + stats.get(key, 0)
                    )
            logger.debug("[TemplateOptimizer] 已聚合 Worker %s 的 A/B 数据", url)
        except Exception as e:
            logger.debug("[TemplateOptimizer] Worker %s A/B 聚合失败: %s", url, e)

    # 重新计算 reply_rate
    for stats in merged.values():
        sent = stats.get("sent", 0)
        if sent > 0:
            stats["reply_rate"] = round(stats.get("reply_received", 0) / sent, 4)

    return merged


def _discover_worker_urls() -> List[str]:
    """获取集群中除本机外的所有在线 Worker URL。"""
    urls = []
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        for h in coord._hosts.values():
            if getattr(h, "online", False) and getattr(h, "host_ip", ""):
                port = getattr(h, "port", 8000)
                urls.append(f"http://{h.host_ip}:{port}")
    except Exception:
        # 回退：固定 W03 地址（实际部署常量）
        urls = ["http://192.168.0.103:8000"]
    return urls


# ── Track B: LeadsStore 交互记录统计 ──

def _get_leads_interaction_stats(lookback_days: int = 30) -> Dict[str, dict]:
    """
    从 leads.db 读取各 ab_variant 的转化情况。

    interactions.metadata.ab_variant 由 follow_tracker.record_dm() 写入。
    统计各变体：contacted_count / qualified_count / converted_count。
    """
    stats: Dict[str, dict] = {}
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()

        leads = store.list_leads(limit=5000)
        for lead in leads:
            lid = lead["id"]
            lead_status = lead.get("status", "new")

            # 读取该 lead 的最新 DM 交互（找 ab_variant）
            interactions = store.get_interactions(lid, platform="tiktok", limit=10)
            for ix in interactions:
                if ix.get("action") != "send_dm" or ix.get("direction") != "outbound":
                    continue
                meta = ix.get("metadata") or {}
                variant = meta.get("ab_variant", "")
                if not variant:
                    continue

                if variant not in stats:
                    stats[variant] = {"contacted": 0, "qualified": 0, "converted": 0}

                stats[variant]["contacted"] += 1
                if lead_status in ("qualified", "responded"):
                    stats[variant]["qualified"] += 1
                elif lead_status == "converted":
                    stats[variant]["converted"] += 1
                break  # 每个 lead 只计一次

    except Exception as e:
        logger.debug("[TemplateOptimizer] LeadsStore 统计失败: %s", e)

    # 计算转化率
    for v, s in stats.items():
        c = s["contacted"]
        if c > 0:
            s["lead_conversion_rate"] = round(
                (s["converted"] * 1.0 + s["qualified"] * 0.4) / c, 4
            )
        else:
            s["lead_conversion_rate"] = 0.0

    return stats


# ── 权重计算与应用 ──

def _calculate_and_apply_weights(
    variants: List[dict],
    ab_stats: dict,
    leads_stats: Dict[str, dict],
    dry_run: bool = False,
) -> List[dict]:
    """
    综合两轨数据，计算新权重并更新 variants 列表（原地修改）。

    A/B 实验变体（control/variant_a/variant_b）通过 hash(variant) % N 映射到
    message_variants 索引，与 _generate_chat_message() 中的映射逻辑一致。

    策略：
      - 优先使用 LeadsStore 转化率（最终结果更准确）
      - 若样本量不足，用 A/B reply_received 数量补充
      - softmax 平滑（temperature=2），映射到 [15, 85]
      - 变化幅度 < 5 时不更新（避免抖动）
    """
    import math

    n = len(variants)
    if n == 0:
        return []

    # ── 将 A/B 变体得分映射到 message_variants 索引 ──
    # 映射规则与 tiktok.py _generate_chat_message() 完全一致
    # control → 所有模板均匀使用（基准，不单独偏移某索引）
    # variant_x → hash(variant_x) % n 确定映射到哪个模板
    ab_variants_data = ab_stats.get("variants", {})

    # 每个 message_variant 的得分（索引 → 得分）
    index_scores: Dict[int, float] = {i: 0.05 for i in range(n)}  # 基础分

    for ab_v, ab_data in ab_variants_data.items():
        ab_sent = ab_data.get("sent", 0)
        ab_replies = ab_data.get("reply_received", 0)
        ab_converted = ab_data.get("converted", 0)
        ab_score = (ab_replies * 0.6 + ab_converted * 1.5) / max(ab_sent, 1)

        if ab_v == "control":
            # control 变体均匀贡献到所有模板
            for i in range(n):
                index_scores[i] += ab_score / n
        else:
            # 非 control 变体：用相同 hash 映射
            idx = hash(ab_v) % n
            if ab_sent >= 3:  # 最低 3 条记录才纳入计算
                index_scores[idx] += ab_score

    # ── 叠加 LeadsStore 转化率（按 ab_variant → 模板索引 映射） ──
    for ab_v, lead_data in leads_stats.items():
        lead_sample = lead_data.get("contacted", 0)
        lead_rate = lead_data.get("lead_conversion_rate", 0.0)
        if lead_sample < _MIN_SAMPLE_SIZE:
            continue
        if ab_v == "control":
            for i in range(n):
                index_scores[i] += lead_rate * 0.7 / n
        else:
            idx = hash(ab_v) % n
            index_scores[idx] += lead_rate * 0.7

    # ── softmax 平滑 (temperature=2) ──
    temperature = 2.0
    exp_s = {i: math.exp(s / temperature) for i, s in index_scores.items()}
    total_exp = sum(exp_s.values()) or 1.0

    changes = []
    for i, variant_cfg in enumerate(variants):
        vid = variant_cfg.get("id", "")
        if not vid:
            continue

        old_weight = variant_cfg.get("weight", 50)
        softmax_val = exp_s[i] / total_exp
        # 映射到 [_WEIGHT_MIN, _WEIGHT_MAX]
        new_weight = int(_WEIGHT_MIN + softmax_val * n * (_WEIGHT_MAX - _WEIGHT_MIN))
        new_weight = max(_WEIGHT_MIN, min(_WEIGHT_MAX, new_weight))

        if abs(new_weight - old_weight) < 5:
            continue

        changes.append({
            "id": vid,
            "old_weight": old_weight,
            "new_weight": new_weight,
            "score": round(index_scores[i], 4),
            "template_index": i,
        })

        if not dry_run:
            variant_cfg["weight"] = new_weight
            logger.info("[TemplateOptimizer] %s (idx=%d): weight %d → %d (score=%.4f)",
                        vid, i, old_weight, new_weight, index_scores[i])

    return changes
