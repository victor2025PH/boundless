# -*- coding: utf-8 -*-
"""主动触达 P6 门禁（2026-07-29，分位阈值自适应）。

锁四条不变量：
  1. 配置解析：auto 默认关；分位/人群/间隔参数夹紧。
  2. 校准纯函数：人群不足→配置值；同质分布→配置值（分层=追噪声）；
     正常分布→分位数 + 间隔保底；只取 obs_n≥min_obs 的合格会话。
  3. 规划器接线：auto 开时按**校准后**阈值分层（固定阈值不会触发、
     分位阈值会触发的构造样本）；auto 关=旧行为。
  4. 周报分布段：账本 → P25/P50/P75 读数（阈值判据可见化）。
"""

from __future__ import annotations

import json
import time

from src.integrations.companion_proactive import plan_proactive_sends
from src.utils.proactive_pacing import (
    _percentile,
    calibrate_response_thresholds,
    parse_response_pacing_cfg,
)


def _noon_today() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0,
                        lt.tm_wday, lt.tm_yday, -1))


_AUTO = parse_response_pacing_cfg({"response_pacing": {
    "relax": 0.85, "auto_thresholds": {"enabled": True}}})


# ── 1. 配置解析 ─────────────────────────────────────────────────────────

def test_parse_auto_defaults_and_clamps():
    cfg = parse_response_pacing_cfg({})
    assert cfg["auto_thresholds"]["enabled"] is False
    assert cfg["auto_thresholds"]["min_population"] == 12
    over = parse_response_pacing_cfg({"response_pacing": {"auto_thresholds": {
        "enabled": True, "low_percentile": 1, "high_percentile": 99.9,
        "min_population": 1, "min_separation": 0.9,
    }}})
    a = over["auto_thresholds"]
    assert a["low_percentile"] == 5.0      # 下夹
    assert a["high_percentile"] == 95.0    # 上夹
    assert a["min_population"] == 4        # 人群保底
    assert a["min_separation"] == 0.5      # 间隔封顶


def test_percentile_small_impl():
    assert _percentile([], 50) == 0.0
    assert _percentile([0.4], 25) == 0.4
    vals = [0.0, 0.1, 0.2, 0.3, 0.4]
    assert abs(_percentile(vals, 50) - 0.2) < 1e-9
    assert abs(_percentile(vals, 25) - 0.1) < 1e-9
    assert abs(_percentile(vals, 100) - 0.4) < 1e-9


# ── 2. 校准纯函数 ───────────────────────────────────────────────────────

def _obs(rates, n_each=10):
    """把回复率列表变成 (obs_n, obs_replied) 对。"""
    return [(n_each, round(r * n_each)) for r in rates]


def test_calibrate_off_and_insufficient_population():
    off = parse_response_pacing_cfg({"response_pacing": {}})
    got = calibrate_response_thresholds(_obs([0.1] * 50), off)
    assert got["source"] == "config" and got["low_rate"] == 0.15
    # 开了但合格人群只有 5（<12）→ 配置值 + population 如实回报
    got = calibrate_response_thresholds(_obs([0.0, 0.2, 0.4, 0.6, 0.8]), _AUTO)
    assert got["source"] == "config" and got["population"] == 5


def test_calibrate_excludes_underobserved():
    # 30 条会话但只有 6 条 obs_n≥4 → 人群 6 < 12 → 配置值
    pairs = [(2, 1)] * 24 + [(10, 5)] * 6
    got = calibrate_response_thresholds(pairs, _AUTO)
    assert got["population"] == 6 and got["source"] == "config"


def test_calibrate_percentiles_and_homogeneous_fallback():
    # 均匀分布 0.0..0.9：P25=0.225 P75=0.675 → 激活
    rates = [i / 10 for i in range(10)] * 2  # n=20
    got = calibrate_response_thresholds(_obs(rates), _AUTO)
    assert got["source"] == "percentile" and got["population"] == 20
    assert 0.15 < got["low_rate"] < 0.3
    assert 0.6 < got["high_rate"] < 0.75
    assert got["high_rate"] - got["low_rate"] >= 0.15
    # 同质人群（全 0.3 上下微差）→ P75-P25 < 0.15 → 回落配置值
    homo = calibrate_response_thresholds(
        _obs([0.28, 0.3, 0.32] * 7), _AUTO)
    assert homo["source"] == "config"


# ── 3. 规划器接线 ───────────────────────────────────────────────────────

def _conv(cid, *, silent_h, last_in_offset_h, now):
    return {
        "conversation_id": cid, "platform": "telegram", "account_id": "a",
        "chat_key": "1", "last_ts": now - silent_h * 3600.0,
        "last_direction": "out", "archived": False, "memory_key": "1",
        "intimacy": 0.0, "last_in_ts": now - last_in_offset_h * 3600.0,
    }


def _op(**kw):
    return {"mode": "gentle_checkin", "directive": "hi", "fact": ""}


def test_planner_uses_calibrated_thresholds():
    now = _noon_today()
    # 账本人群：12 条高回复(0.8) + 12 条低回复(0.05) → P25≈0.05 P75≈0.8。
    # 目标会话回复率 0.05：固定阈值 low=0.15 会 stretch（0.05≤0.15）——
    # 构造反向证明改用 rate 0.3 的会话：固定 low=0.15 不 stretch、
    # 校准 low≈0.05 也不 stretch；固定 high=0.6 不 relax、校准 high≈0.8 也不。
    # 更判别的样本：rate=0.7 → 固定 high=0.6 会 relax(×0.85)、
    # 校准 high≈0.8 不 relax → 用「44h<48h 冷却」是否放行来区分两种阈值。
    cd = {}
    for i in range(12):
        cd[f"tg:a:h{i}"] = {"ts": 1000, "sent_ts": 1000, "streak": 0,
                            "last_text": "", "obs_n": 10, "obs_replied": 8}
    for i in range(12):
        cd[f"tg:a:l{i}"] = {"ts": 1000, "sent_ts": 1000, "streak": 0,
                            "last_text": "", "obs_n": 10, "obs_replied": 0}
    tgt = f"tg:a:tgt"
    cd[tgt] = {"ts": now - 44 * 3600, "sent_ts": now - 44 * 3600, "streak": 1,
               "last_text": "x", "obs_n": 10, "obs_replied": 7}  # rate 0.7
    conv = _conv(tgt, silent_h=44, last_in_offset_h=30, now=now)
    fixed = parse_response_pacing_cfg({"response_pacing": {"relax": 0.85}})
    args = dict(
        cooldown_map=cd, opener_fn=_op, now=now,
        min_silent_hours=24, cooldown_hours=48, max_per_tick=5,
        quiet_start_hour=23, quiet_end_hour=8, backoff_cfg=None)
    # 固定阈值：0.7 ≥ 0.6 → relax 48×0.85=40.8h < 44h → 放行
    plans = plan_proactive_sends([conv], response_pacing_cfg=fixed, **args)
    assert len(plans) == 1 and plans[0]["response_factor"] == 0.85
    # 分位阈值：high≈0.8 > 0.7 → 不 relax → 48h 冷却未过 → 不发
    plans2 = plan_proactive_sends([conv], response_pacing_cfg=_AUTO, **args)
    assert plans2 == []


def test_planner_auto_with_small_ledger_equals_fixed():
    now = _noon_today()
    # 账本只有目标一条（人群 1 < 12）→ auto 回落配置阈值，行为与 fixed 一致
    cd = {"tg:a:t": {"ts": now - 44 * 3600, "sent_ts": now - 44 * 3600,
                     "streak": 1, "last_text": "x",
                     "obs_n": 10, "obs_replied": 7}}
    conv = _conv("tg:a:t", silent_h=44, last_in_offset_h=30, now=now)
    args = dict(
        cooldown_map=cd, opener_fn=_op, now=now,
        min_silent_hours=24, cooldown_hours=48, max_per_tick=5,
        quiet_start_hour=23, quiet_end_hour=8, backoff_cfg=None)
    plans = plan_proactive_sends([conv], response_pacing_cfg=_AUTO, **args)
    assert len(plans) == 1 and plans[0]["response_factor"] == 0.85


# ── 4. 周报分布段 ───────────────────────────────────────────────────────

def test_review_resp_rates_distribution(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    ledger = {}
    for i, r in enumerate((0.0, 0.2, 0.4, 0.6, 0.8, 1.0)):
        ledger[f"c{i}"] = {"ts": 1, "sent_ts": 1, "streak": 0,
                           "last_text": "", "obs_n": 10,
                           "obs_replied": round(r * 10)}
    ledger["under"] = {"ts": 1, "sent_ts": 1, "streak": 0,
                       "last_text": "", "obs_n": 2, "obs_replied": 2}
    (cfg_dir / "companion_proactive_cooldown.json").write_text(
        json.dumps(ledger), "utf-8")
    from scripts.proactive_review import collect_review, render_review
    rep = collect_review(tmp_path, days=14.0)
    rr = rep["resp_rates"]
    assert rr["n"] == 6  # obs_n<4 的不计
    assert abs(rr["p50"] - 0.5) < 0.01
    assert "阈值自适应判据" in render_review(rep)
