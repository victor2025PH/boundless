# -*- coding: utf-8 -*-
"""获客剧本库契约（2026-08-16 P0）：防幻觉模板 + 防风控事故。

背景：config/task_chains.yaml 的内置剧本会被新手无脑批量下发。若某步 task_type
拼错、或 params 键与执行器消费面对不上，不会报错——只会静默失效或按默认值跑；
若频控参数出厂就激进，等于把封号风险打包进"官方推荐"。本套件把四条纪律焊死：

  1) 动词真实性：每步 task_type ∈ 执行器真实认识的动词
     （src/host/executor.py 的具体 == 判断 ∪ tiktok_/facebook_ 前缀分发）
  2) 参数键对账：params 键 ∈ 执行器全局参数消费面（防拼写错静默失效）
  3) 元数据完整性：每条内置剧本 scene/funnel_stage/risk_level/... 齐全且取值合法
  4) 风控保守性：频控类参数出厂不得超过安全上限

动词/参数白名单由执行器源码扫描动态构建，不写死——执行器加新动词，门禁自动认。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_YAML = _ROOT / "config" / "task_chains.yaml"
_EXECUTOR = _ROOT / "src" / "host" / "executor.py"

_EXEC_SRC = _EXECUTOR.read_text(encoding="utf-8")
_CHAINS = (yaml.safe_load(_YAML.read_text(encoding="utf-8")) or {}).get("chains") or {}

# ── 执行器真实动词集 ────────────────────────────────────────────────
# 权威来源 = 执行器源码里两种精确分发形态的并集：
#   a) task_type == "xxx"
#   b) task_type in ("xxx", "yyy", ...)   ← tiktok_check_and_chat_followbacks 走这里
# 刻意不接受裸前缀（tiktok_/facebook_）放行：两个前缀分发器末尾都有显式
# "不支持的任务类型" else 兜底（executor.py:1771 facebook / :6290 tiktok），
# 即拼错的 tiktok_xxx 会运行时失败——若门禁放行裸前缀就抓不到这类幻觉动词
# （2026-08-16 负向标定实锤：tiktok_teleport_leads 曾被裸前缀漏过）。
_CONCRETE_VERBS = set(re.findall(r'task_type\s*==\s*["\']([a-z_0-9]+)["\']', _EXEC_SRC))
for _grp in re.findall(r'task_type\s+in\s*\(([^)]*)\)', _EXEC_SRC):
    _CONCRETE_VERBS.update(re.findall(r'["\']([a-z_0-9]+)["\']', _grp))


def _verb_is_real(verb: str) -> bool:
    return verb in _CONCRETE_VERBS


# ── 执行器全局参数消费面 ────────────────────────────────────────────
# params.get("k") / params["k"] / p.get("k") / task_params[...] 等常见形态。
_PARAM_KEYS = set(
    re.findall(r'(?:params|p|task_params|tp)\s*(?:\.get\(\s*|\[)\s*["\']([a-zA-Z_0-9]+)["\']', _EXEC_SRC)
)
# steps/on_fail 是链编排关键字（被 task_chain 消费，不是执行器 params），豁免。
_CHAIN_KEYWORDS = {"steps", "on_fail"}

# ── 元数据 schema ───────────────────────────────────────────────────
_REQUIRED_META = {"scene", "funnel_stage", "risk_level",
                  "prerequisites", "expected_output", "params_exposed"}
_FUNNEL_STAGES = {"warmup", "discover", "first_touch", "followup", "harvest", "full"}
_RISK_LEVELS = {"low", "medium", "high"}

# ── 频控安全上限（出厂保守，激进档留给用户自己在 UI 调）────────────────
# 键 → 单次运行的安全上限。剧本 params 里若出现该键，值不得超过上限。
_FREQ_CAPS = {
    "max_follows": 30,
    "max_friends_per_run": 8,
    "max_leads": 25,
    "max_targets": 25,
    "max_conversations": 30,
    "max_requests": 20,
    "max_replies": 20,
    "max_videos": 20,
    "max_tweets": 30,
    "max_accept": 20,
    "comments_per_video": 3,
}


def _all_steps():
    for chain_id, cfg in _CHAINS.items():
        for i, step in enumerate(cfg.get("steps") or []):
            yield chain_id, i, step


def test_chains_loaded():
    """至少 15 条内置剧本（P0 扩充目标），且 7 平台全覆盖。"""
    assert len(_CHAINS) >= 15, f"内置剧本仅 {len(_CHAINS)} 条，P0 目标 ≥15"
    platforms = {cfg.get("platform") for cfg in _CHAINS.values()}
    expected = {"tiktok", "facebook", "telegram", "whatsapp",
                "instagram", "linkedin", "twitter"}
    missing = expected - platforms
    assert not missing, f"以下平台无任何剧本覆盖: {sorted(missing)}"


def test_step_verbs_are_real():
    """纪律1：每步 task_type 必须是执行器真实认识的动词（防幻觉）。"""
    bad = []
    for chain_id, i, step in _all_steps():
        verb = step.get("task_type", "")
        if not _verb_is_real(verb):
            bad.append(f"{chain_id}[{i}] task_type={verb!r}")
    assert not bad, "以下步骤动词执行器不认识（会静默失效）:\n  " + "\n  ".join(bad)


def test_step_param_keys_are_consumed():
    """纪律2：params 键必须在执行器消费面内（防拼写错静默按默认值跑）。"""
    bad = []
    for chain_id, i, step in _all_steps():
        for key in (step.get("params") or {}):
            if key in _CHAIN_KEYWORDS:
                continue
            # facebook_campaign_run/group_member_greet 的 steps 是子动作列表，
            # 由平台执行器内部消费，键名可能不在顶层 params.get 面——这类
            # 复合动词的 'steps' 已在 _CHAIN_KEYWORDS 豁免；其余键须对账。
            if key not in _PARAM_KEYS:
                bad.append(f"{chain_id}[{i}] {step.get('task_type')} 的 params.{key}")
    assert not bad, "以下参数键执行器从不消费（拼写错？）:\n  " + "\n  ".join(bad)


def test_metadata_complete_and_valid():
    """纪律3：内置剧本元数据字段齐全且取值合法（货架展示/筛选依赖）。"""
    problems = []
    for chain_id, cfg in _CHAINS.items():
        missing = _REQUIRED_META - set(cfg.keys())
        if missing:
            problems.append(f"{chain_id} 缺元数据字段: {sorted(missing)}")
            continue
        if cfg["funnel_stage"] not in _FUNNEL_STAGES:
            problems.append(f"{chain_id} funnel_stage={cfg['funnel_stage']!r} 非法")
        if cfg["risk_level"] not in _RISK_LEVELS:
            problems.append(f"{chain_id} risk_level={cfg['risk_level']!r} 非法")
        if not isinstance(cfg.get("prerequisites"), list) or not cfg["prerequisites"]:
            problems.append(f"{chain_id} prerequisites 必须是非空列表")
        if not isinstance(cfg.get("params_exposed"), list):
            problems.append(f"{chain_id} params_exposed 必须是列表")
    assert not problems, "元数据问题:\n  " + "\n  ".join(problems)


def test_params_exposed_are_real_keys():
    """params_exposed 里声明的可调键，必须真的出现在该链某步的 params 里，
    否则 UI 会渲染一个改了没用的输入框。"""
    problems = []
    for chain_id, cfg in _CHAINS.items():
        used = set()
        for step in cfg.get("steps") or []:
            used.update((step.get("params") or {}).keys())
        for key in cfg.get("params_exposed") or []:
            if key not in used:
                problems.append(f"{chain_id} 暴露了 {key!r} 但没有任何步骤用到它")
    assert not problems, "params_exposed 悬空:\n  " + "\n  ".join(problems)


def test_frequency_caps_conservative():
    """纪律4：频控类参数出厂必须保守，不得超过安全上限（防风控事故）。"""
    violations = []
    for chain_id, i, step in _all_steps():
        for key, val in (step.get("params") or {}).items():
            cap = _FREQ_CAPS.get(key)
            if cap is not None and isinstance(val, (int, float)) and val > cap:
                violations.append(
                    f"{chain_id}[{i}] {step.get('task_type')} {key}={val} 超过安全上限 {cap}")
    assert not violations, "出厂频控参数过激（封号风险）:\n  " + "\n  ".join(violations)
