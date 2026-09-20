"""O-1 D（D-O4，#252 #254）「拟人程度」三档 + composing 可观测 + 首回延迟 / 连发合并 / 夜间静默。

FW78ZP 事故：起草→发出恒 12–13s 与长度无关、全天秒回、240 分钟零 composing 日志。
本门禁钉住：
  - 三档参数表与设置页 PACING_PROFILE_CHOICES 一致；
  - estimate_human_pacing 纯函数：读分量随入站词数单调、打字分量随出站长度单调、CJK 走 cpm、
    夹 cap、stop 含在 type 段；
  - resolve_pacing profile 分支：已耗时照扣、块显式值覆盖档位缺省、typing_lead = 打字分量；
    legacy 块（无 profile）输出零变化（profile=""，既有 test_humanize* 全绿即是）；
  - 验收回放：20 条不同长度入站 → 发出间隔与字数相关系数 >0.6、无两条间隔到秒相同；
  - [pacing] 日志行格式与 composing 六态（nocb / off / sent / unsupported / skipped）；
  - 首回延迟：首次接触 / 沉寂 ≥6h → 60–300s 确定性 hold；短沉寂不延；legacy 块不开；危机不延；
  - 连发合并：配置路径 window_max_sec 缺省 15、开窗随机落在 [8, 15]；
  - 夜间：种子班表 08:20–01:00 → 03:00 扣留、10:00 放行；
  - 出厂基线：A 类 profile=natural / inbound_merge / work_schedule 三件 + 种子同值；
  - 三预设各带 profile；模板 / i18n / feat 探测钉住。
"""

from __future__ import annotations

import logging
import math
import random
import re
from pathlib import Path

import pytest
import yaml

from src.inbox import humanize as hz
from src.inbox.humanize import (
    PACING_PROFILE_NAMES,
    PACING_PROFILES,
    estimate_human_pacing,
    first_reply_hold_sec,
    resolve_first_reply_cfg,
    resolve_min_gap_sec,
    resolve_pacing,
    resolve_profile,
    silence_before_inbound,
)
from src.inbox.reply_pacing_settings import FIELDS, PACING_PROFILE_CHOICES, PRESETS

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "src" / "web" / "templates" / "reply_settings.html"
SEED = ROOT / "config" / "config.desktop.min.yaml"


def _mid(lo, hi):
    """确定性 rng：恒取区间中点（抖动 ×1.0）。"""
    return (lo + hi) / 2.0


# ── 三档参数表 ────────────────────────────────────────────────────────────────

def test_profile_table_matches_settings_choices():
    assert set(PACING_PROFILE_NAMES) == set(PACING_PROFILE_CHOICES) - {"custom"}
    assert set(FIELDS["inbox.l2_autosend.deliver_delay.profile"]["choices"]) == set(PACING_PROFILE_CHOICES)
    need = {"read_min", "read_max", "read_per_10_words", "read_cap", "think_min", "think_max",
            "wpm_min", "wpm_max", "cpm_min", "cpm_max", "type_cap", "jitter",
            "stop_before_send", "min_gap_sec", "min_residual_sec"}
    for name, p in PACING_PROFILES.items():
        assert need <= set(p), name
        assert p["read_min"] <= p["read_max"] and p["think_min"] <= p["think_max"]
        assert p["wpm_min"] <= p["wpm_max"] and p["cpm_min"] <= p["cpm_max"]
    # D-O4 原话：读 3–6s + 每 10 词 +1s 上限 20；想 3–8s；35–45 wpm / 60–90 cpm；上限 90；停 1s
    n = PACING_PROFILES["natural"]
    assert (n["read_min"], n["read_max"], n["read_per_10_words"], n["read_cap"]) == (3.0, 6.0, 1.0, 20.0)
    assert (n["think_min"], n["think_max"]) == (3.0, 8.0)
    assert (n["wpm_min"], n["wpm_max"], n["cpm_min"], n["cpm_max"], n["type_cap"]) == (35.0, 45.0, 60.0, 90.0, 90.0)
    assert n["stop_before_send"] == 1.0
    # 偏快仍是人的手速（不是秒回）；偏慢比自然慢
    assert PACING_PROFILES["fast"]["read_min"] >= 1.0
    assert PACING_PROFILES["slow"]["think_min"] > n["think_min"]


def test_resolve_profile_semantics():
    assert resolve_profile({"profile": "natural"}) == "natural"
    assert resolve_profile({"profile": " Fast "}) == "fast"
    assert resolve_profile({"profile": "custom"}) == ""
    assert resolve_profile({"profile": "nope"}) == ""
    assert resolve_profile({}) == "" and resolve_profile(None) == ""


# ── estimate_human_pacing 纯函数 ──────────────────────────────────────────────

def test_read_component_monotone_in_inbound_words():
    reply = "ok sure"
    shorts = estimate_human_pacing(reply, inbound_text="hi", rng=_mid)
    mids = estimate_human_pacing(reply, inbound_text=" ".join(["word"] * 30), rng=_mid)
    longs = estimate_human_pacing(reply, inbound_text=" ".join(["word"] * 120), rng=_mid)
    assert shorts.read < mids.read < longs.read
    assert shorts.words == 1 and mids.words == 30 and longs.words == 120
    # 夹 read_cap（120 词 → 4.5 + 12 = 16.5 < 20；400 词才撞 cap）
    huge = estimate_human_pacing(reply, inbound_text=" ".join(["w"] * 400), rng=_mid)
    assert huge.read == PACING_PROFILES["natural"]["read_cap"]


def test_type_component_monotone_in_reply_length_and_capped():
    a = estimate_human_pacing("ok", inbound_text="x", rng=_mid)
    b = estimate_human_pacing(" ".join(["word"] * 20), inbound_text="x", rng=_mid)
    c = estimate_human_pacing(" ".join(["word"] * 60), inbound_text="x", rng=_mid)
    assert a.type_ < b.type_ < c.type_
    # 40 wpm：20 词 = 30s、60 词 = 90s（撞 cap）
    assert abs(b.type_ - 30.0) < 0.01
    assert c.type_ == PACING_PROFILES["natural"]["type_cap"]
    d = estimate_human_pacing(" ".join(["word"] * 300), inbound_text="x", rng=_mid)
    assert d.type_ == PACING_PROFILES["natural"]["type_cap"]
    # 想分量与文本无关（中点 5.5s）
    assert abs(a.think - 5.5) < 0.01 and abs(c.think - 5.5) < 0.01


def test_cjk_reply_uses_cpm():
    zh = estimate_human_pacing("今天天气真好啊我们出去走走吧" , inbound_text="嗨", rng=_mid)
    assert zh.cjk is True and zh.reply_units == 14
    # 75 字/分 → 14 字 = 11.2s
    assert abs(zh.type_ - 14 / 75.0 * 60.0) < 0.01
    en = estimate_human_pacing(" ".join(["w"] * 14), inbound_text="hi", rng=_mid)
    assert en.cjk is False and en.reply_units == 14
    # 同 14 单位：中文按字/分（75）比英文按词/分（40）打得快
    assert zh.type_ < en.type_


def test_stop_contained_in_type_segment_and_total():
    hp = estimate_human_pacing("k", inbound_text="hi", rng=_mid)
    assert hp.stop == 1.0
    assert hp.type_ >= hp.stop + 0.6           # 打字段至少容下「停 composing」
    assert abs(hp.total - (hp.read + hp.think + hp.type_)) < 1e-9   # stop 不另加


def test_jitter_bounds_with_real_rng():
    rng = random.Random(7).uniform
    for _ in range(200):
        hp = estimate_human_pacing(" ".join(["w"] * 20), inbound_text=" ".join(["w"] * 20), rng=rng)
        assert 3.0 * 0.7 <= hp.read <= min(20.0, (6.0 + 2.0) * 1.3) + 1e-9
        assert 3.0 * 0.7 <= hp.think <= 8.0 * 1.3 + 1e-9
        assert 1.6 <= hp.type_ <= 90.0


# ── resolve_pacing profile 分支 ───────────────────────────────────────────────

def test_resolve_pacing_profile_branch_fields():
    block = {"profile": "natural", "min_sec": 8, "max_sec": 20, "adaptive": True}
    r = resolve_pacing(block, text=" ".join(["w"] * 20), inbound_text=" ".join(["w"] * 10), rng=_mid)
    assert r.profile == "natural" and r.enabled and r.adaptive
    assert r.read_sec > 0 and r.think_sec > 0 and r.type_sec > 0 and r.stop_sec == 1.0
    assert abs(r.delay - (r.read_sec + r.think_sec + r.type_sec)) < 1e-9
    assert r.typing_lead == r.type_sec           # 可见打字 = 打字分量
    # min/max 被忽略：30s 打字 + 读想 > 20 max
    assert r.delay > 20.0


def test_resolve_pacing_profile_deducts_elapsed_but_keeps_typing():
    block = {"profile": "natural"}
    full = resolve_pacing(block, text=" ".join(["w"] * 20), inbound_text="hi", rng=_mid)
    part = resolve_pacing(block, text=" ".join(["w"] * 20), inbound_text="hi", rng=_mid, elapsed_sec=5.0)
    assert abs(part.delay - (full.delay - 5.0)) < 1e-9 and part.elapsed == 5.0
    # 生成耗时吃光读想 → 至少还留打字段（不能秒回）
    eaten = resolve_pacing(block, text=" ".join(["w"] * 20), inbound_text="hi", rng=_mid, elapsed_sec=500.0)
    assert abs(eaten.delay - eaten.type_sec) < 1e-9 and eaten.floored
    assert eaten.typing_lead == eaten.type_sec


def test_resolve_pacing_block_explicit_param_overrides_profile_default():
    base = resolve_pacing({"profile": "natural"}, text="a b c", inbound_text="hi", rng=_mid)
    over = resolve_pacing({"profile": "natural", "think_min": 20, "think_max": 30},
                          text="a b c", inbound_text="hi", rng=_mid)
    assert abs(over.think_sec - 25.0) < 1e-9 and abs(base.think_sec - 5.5) < 1e-9
    # 布尔不算数值覆写（adaptive: True 不会污染 params）
    r = resolve_pacing({"profile": "natural", "adaptive": True, "jitter": 0}, text="a b c", rng=_mid)
    assert r.profile == "natural"


def test_legacy_block_untouched():
    block = {"min_sec": 3, "max_sec": 12, "adaptive": True}
    r = resolve_pacing(block, text="hello there", rng=_mid)
    assert r.profile == "" and r.read_sec == 0 and r.think_sec == 0 and r.type_sec == 0
    assert r.stop_sec == 0 and r.typing_lead is None
    r2 = resolve_pacing({"profile": "custom", "min_sec": 3, "max_sec": 12}, text="x", rng=_mid)
    assert r2.profile == "" and r2.delay == 7.5


def test_min_gap_profile_default_and_explicit_override():
    assert resolve_min_gap_sec({"profile": "natural"}) == 8.0
    assert resolve_min_gap_sec({"profile": "slow"}) == 12.0
    assert resolve_min_gap_sec({"profile": "natural", "min_gap_sec": 3}) == 3.0
    assert resolve_min_gap_sec({"profile": "natural", "min_gap_sec": 0}) == 0.0   # 显式 0 = 关
    assert resolve_min_gap_sec({}) == 0.0


# ── 验收回放：20 条不同长度 ───────────────────────────────────────────────────

def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (vx * vy) if vx and vy else 0.0


def test_replay_20_inbounds_delay_tracks_length_and_no_two_equal_seconds():
    rng = random.Random(20260908).uniform
    block = {"profile": "natural", "min_sec": 8, "max_sec": 20, "adaptive": True}
    lengths, delays = [], []
    for i in range(20):
        n_in = 2 + i * 4                      # 2 … 78 词
        n_out = 3 + i * 2                     # 3 … 41 词
        r = resolve_pacing(block, text=" ".join(["w"] * n_out),
                           inbound_text=" ".join(["w"] * n_in), rng=rng)
        lengths.append(n_in + n_out)
        delays.append(r.delay)
    assert _pearson(lengths, delays) > 0.6
    assert len({int(round(d)) for d in delays}) == 20, delays
    # 事故形态「恒 12–13s」不再：跨度至少几十秒
    assert max(delays) - min(delays) > 20.0


# ── [pacing] 日志 + composing 六态 ────────────────────────────────────────────

class _Svc:
    def list_drafts(self, status="pending", limit=200):
        return []

    def resolve_with_audit(self, *a, **k):
        return {"ok": True}


def _worker(**kw):
    from src.inbox.autosend_worker import AutosendWorker

    async def _sleep(_d):
        return None

    return AutosendWorker(draft_service=_Svc(), sleep=_sleep, **kw)


def _pacing_line(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[pacing] conv=")]
    assert len(lines) == 1, lines
    return lines[0]


@pytest.mark.asyncio
async def test_pacing_log_line_format_and_composing_sent(caplog):
    calls = []

    async def _typing(platform, account_id, chat_key, action):
        calls.append(action)
        return True

    w = _worker(config={"deliver_delay": {"profile": "natural"}, "typing_indicator": True},
                typing_callback=_typing)
    with caplog.at_level(logging.INFO, logger="src.inbox.autosend_worker"):
        await w._run_humanize("telegram", "a1", "c1", text=" ".join(["w"] * 20),
                              conversation_id="conv1", inbound_text="hello how are you")
    line = _pacing_line(caplog)
    for k in ("conv=conv1", "platform=telegram", "profile=natural", "read=", "think=", "type=",
              "stop=1.0", "elapsed=", "total=", "typing_lead=", "composing=sent", "words=20"):
        assert k in line, line
    assert re.search(r"typing_calls=\d+", line) and calls


@pytest.mark.asyncio
async def test_composing_unsupported_when_callback_returns_false(caplog):
    async def _typing(*a):
        return False        # LINE / Messenger：编排器 send_chat_action 无能力位 → False

    w = _worker(config={"deliver_delay": {"profile": "natural"}}, typing_callback=_typing)
    with caplog.at_level(logging.INFO, logger="src.inbox.autosend_worker"):
        await w._run_humanize("line", "a1", "c1", text=" ".join(["w"] * 20), conversation_id="c")
    assert "composing=unsupported" in _pacing_line(caplog)


@pytest.mark.asyncio
async def test_composing_nocb_and_off(caplog):
    w = _worker(config={"deliver_delay": {"profile": "natural"}})
    with caplog.at_level(logging.INFO, logger="src.inbox.autosend_worker"):
        await w._run_humanize("telegram", "a1", "c1", text="hi there", conversation_id="c")
    assert "composing=nocb" in _pacing_line(caplog)
    caplog.clear()

    async def _typing(*a):
        return True

    w2 = _worker(config={"deliver_delay": {"profile": "natural"}, "typing_indicator": False},
                 typing_callback=_typing)
    with caplog.at_level(logging.INFO, logger="src.inbox.autosend_worker"):
        await w2._run_humanize("telegram", "a1", "c1", text="hi there", conversation_id="c")
    assert "composing=off" in _pacing_line(caplog)


@pytest.mark.asyncio
async def test_composing_skipped_when_no_delay(caplog):
    async def _typing(*a):
        return True

    w = _worker(config={}, typing_callback=_typing)     # legacy 无延迟 → 0s → 不挂气泡
    with caplog.at_level(logging.INFO, logger="src.inbox.autosend_worker"):
        await w._run_humanize("telegram", "a1", "c1", text="hi", conversation_id="c")
    line = _pacing_line(caplog)
    assert "composing=skipped" in line and "profile=legacy" in line


@pytest.mark.asyncio
async def test_stop_before_send_leaves_silent_tail():
    """打字段末尾 stop 秒不再续挂 composing：最后一次 typing 调用之后还有 ≥1s 的 sleep。"""
    events = []

    async def _sleep(d):
        events.append(("sleep", d))

    async def _typing(*a):
        events.append(("typing", 0))
        return True

    from src.inbox.autosend_worker import AutosendWorker
    w = AutosendWorker(draft_service=_Svc(), sleep=_sleep, typing_callback=_typing,
                       config={"deliver_delay": {"profile": "natural"}})
    await w._run_humanize("telegram", "a1", "c1", text=" ".join(["w"] * 20), conversation_id="c")
    last_typing = max(i for i, e in enumerate(events) if e[0] == "typing")
    tail = [e[1] for e in events[last_typing + 1:] if e[0] == "sleep"]
    assert tail and abs(tail[-1] - 1.0) < 1e-6


# ── 首回延迟（首次接触 / 沉寂 >6h）────────────────────────────────────────────

def test_first_reply_cfg_defaults_follow_profile():
    assert resolve_first_reply_cfg({"profile": "natural"})["enabled"] is True
    assert resolve_first_reply_cfg({"min_sec": 3, "max_sec": 12})["enabled"] is False
    assert resolve_first_reply_cfg({"profile": "natural", "first_reply": {"enabled": False}})["enabled"] is False
    c = resolve_first_reply_cfg({"first_reply": {"enabled": True, "silence_hours": 2, "min_sec": 10, "max_sec": 5}})
    assert c["enabled"] and c["silence_hours"] == 2.0 and c["min_sec"] == 10.0 and c["max_sec"] == 10.0


def test_first_reply_wechat_pc_defaults_to_fast_hold_unless_configured():
    """微信 PC 副驾（platform=wechat）全自动档：首回 hold 出厂 5–20s（其它渠道仍 60–300s）；
    显式 first_reply / platform_overrides.wechat.first_reply 仍优先。"""
    blk = {"profile": "natural"}
    wx = resolve_first_reply_cfg(blk, platform="wechat")
    assert wx["enabled"] and (wx["min_sec"], wx["max_sec"], wx["silence_hours"]) == (5.0, 20.0, 6.0)
    tg = resolve_first_reply_cfg(blk, platform="telegram")
    assert (tg["min_sec"], tg["max_sec"]) == (60.0, 300.0)
    assert resolve_first_reply_cfg(blk)["min_sec"] == 60.0
    for i in range(20):
        assert 5.0 <= first_reply_hold_sec(wx, silence_sec=None, key=f"wx#{i}") <= 20.0
    # 主人显式配了就听主人的（全局 first_reply / 平台覆写层）
    c = resolve_first_reply_cfg({"profile": "natural", "first_reply": {"min_sec": 30, "max_sec": 90}}, platform="wechat")
    assert (c["min_sec"], c["max_sec"]) == (30.0, 90.0)
    c2 = resolve_first_reply_cfg(
        {"profile": "natural", "platform_overrides": {"wechat": {"first_reply": {"enabled": False}}}}, platform="wechat")
    assert c2["enabled"] is False


def test_silence_before_inbound_cases():
    t0 = 1_700_000_000.0
    # 首次接触：只有入站
    s, anchor = silence_before_inbound([{"direction": "in", "ts": t0, "text": "hi"}], draft_ts=t0 + 5)
    assert s is None and anchor == t0
    # 客户连发三条（5s 内）之前 8h 有我方出站 → 沉寂 = 8h，锚点 = 本轮首条
    rows = [{"direction": "out", "ts": t0 - 8 * 3600},
            {"direction": "in", "ts": t0}, {"direction": "in", "ts": t0 + 2}, {"direction": "in", "ts": t0 + 5}]
    s, anchor = silence_before_inbound(rows, draft_ts=t0 + 6)
    assert abs(s - 8 * 3600) < 1e-6 and anchor == t0
    # 刚聊过（1 分钟前出站）→ 沉寂 60s
    s, _ = silence_before_inbound([{"direction": "out", "ts": t0 - 60}, {"direction": "in", "ts": t0}], draft_ts=t0 + 3)
    assert abs(s - 60) < 1e-6
    # 最后一条是出站（本稿不是在回入站）/ 空行 → 不延
    assert silence_before_inbound([{"direction": "out", "ts": t0}], draft_ts=t0 + 3) == (0.0, t0 + 3)
    assert silence_before_inbound([], draft_ts=t0) == (0.0, t0)
    assert silence_before_inbound(None, draft_ts=t0) == (0.0, t0)
    # 草稿之后才到的行不算本轮
    rows = [{"direction": "out", "ts": t0 - 3600 * 7}, {"direction": "in", "ts": t0}, {"direction": "in", "ts": t0 + 900}]
    s, anchor = silence_before_inbound(rows, draft_ts=t0 + 3)
    assert abs(s - 7 * 3600) < 1e-6 and anchor == t0


def test_first_reply_hold_deterministic_and_bounded():
    cfg = resolve_first_reply_cfg({"profile": "natural"})
    h1 = first_reply_hold_sec(cfg, silence_sec=None, key="conv#1")
    assert 60.0 <= h1 <= 300.0
    assert first_reply_hold_sec(cfg, silence_sec=None, key="conv#1") == h1        # 确定性
    assert first_reply_hold_sec(cfg, silence_sec=7 * 3600, key="conv#2") >= 60.0  # 沉寂 ≥6h
    assert first_reply_hold_sec(cfg, silence_sec=5 * 3600, key="conv#2") == 0.0   # 沉寂不够
    assert first_reply_hold_sec(cfg, silence_sec=30, key="conv#2") == 0.0
    off = resolve_first_reply_cfg({"min_sec": 3, "max_sec": 12})
    assert first_reply_hold_sec(off, silence_sec=None, key="k") == 0.0
    # 不同 key 落点分散（20 个里至少 10 个不同秒）
    vals = {int(first_reply_hold_sec(cfg, silence_sec=None, key=f"c#{i}")) for i in range(20)}
    assert len(vals) >= 10


class _StoreSvc(_Svc):
    def __init__(self, rows):
        self._store = _Store(rows)


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, cid, limit=50):
        return list(self.rows)


def test_worker_first_reply_hold_keeps_pending_then_releases(monkeypatch, caplog):
    import time as _t
    from src.inbox.autosend_worker import AutosendWorker

    t0 = 1_700_000_000.0
    svc = _StoreSvc([{"direction": "in", "ts": t0, "text": "hello"}])

    async def _sleep(_d):
        return None

    async def _send(*a):
        return True

    w = AutosendWorker(draft_service=svc, sleep=_sleep, send_callback=_send,
                       config={"deliver_delay": {"profile": "natural"}})
    d = {"draft_id": "d1", "platform": "telegram", "account_id": "a", "chat_key": "c",
         "conversation_id": "x", "created_ts": t0 + 4, "peer_text": "hello"}
    monkeypatch.setattr(_t, "time", lambda: t0 + 10)
    with caplog.at_level(logging.INFO, logger="src.inbox.autosend_worker"):
        remain = w._first_reply_remaining(d, "x")
    assert 50.0 <= remain <= 290.0
    assert w.total_first_reply_held == 1 and w.status_snapshot()["total_first_reply_held"] == 1
    held = [r.getMessage() for r in caplog.records if "[pacing] first_reply hold" in r.getMessage()]
    assert len(held) == 1 and "silence=first_contact" in held[0] and "conv=x" in held[0]
    # 同稿再判：不重复计数不重复落日志
    w._first_reply_remaining(d, "x")
    assert w.total_first_reply_held == 1
    # 到点放行
    monkeypatch.setattr(_t, "time", lambda: t0 + 400)
    assert w._first_reply_remaining(d, "x") == 0.0
    # 危机消息不延
    monkeypatch.setattr(_t, "time", lambda: t0 + 10)
    d2 = dict(d, draft_id="d2", peer_text="I want to kill myself tonight")
    assert w._first_reply_remaining(d2, "x") == 0.0
    # legacy 块不开
    w2 = AutosendWorker(draft_service=svc, sleep=_sleep, send_callback=_send,
                        config={"deliver_delay": {"min_sec": 3, "max_sec": 12}})
    assert w2._first_reply_remaining(d, "x") == 0.0


def test_worker_process_batch_hook_is_wired():
    src = (ROOT / "src" / "inbox" / "autosend_worker.py").read_text(encoding="utf-8")
    assert "_fr_remain = self._first_reply_remaining(d, _conv)" in src
    # 在班表闸之后、fresh_guard 之前
    assert src.index("self.total_skipped_off_hours += 1") < src.index("_fr_remain = self._first_reply_remaining") \
        < src.index("guard=fresh 新入站过期跳过")


# ── 连发合并 8–15s（复用 inbound_merge，不做第二套）──────────────────────────

def test_inbound_merge_window_randomised_8_to_15():
    from src.inbox.inbound_debounce import DEFAULT_WINDOW_MAX_SEC, InboundMerger, resolve_merge_cfg
    assert DEFAULT_WINDOW_MAX_SEC == 15.0
    cfg = resolve_merge_cfg({"inbound_merge": {"enabled": True}})
    assert cfg["window_sec"] == 8.0 and cfg["window_max_sec"] == 15.0
    delays = []

    def _timer(delay, fn):
        delays.append(delay)
        return type("T", (), {"cancel": lambda self: None})()

    rng = random.Random(3).uniform
    m = InboundMerger(lambda c, t: None, window_sec=8, window_max_sec=15, timer_factory=_timer, rng=rng)
    for i in range(30):
        m.push({"conversation_id": f"c{i}"}, "hi")
    assert all(8.0 <= d <= 15.0 for d in delays) and len({round(d, 1) for d in delays}) > 10
    # 不传 window_max_sec → 恒定窗（既有调用零变化）
    delays.clear()
    m2 = InboundMerger(lambda c, t: None, window_sec=8, timer_factory=_timer)
    m2.push({"conversation_id": "z"}, "hi")
    assert delays == [8.0]
    helpers = (ROOT / "src" / "inbox" / "autodraft_helpers.py").read_text(encoding="utf-8")
    assert 'window_max_sec=_im_cfg.get("window_max_sec")' in helpers


# ── 夜间：种子班表 01–08 不回、08 后补 ───────────────────────────────────────

def test_seed_work_schedule_holds_at_night_and_releases_in_the_morning():
    """班表**开启后**的夜间扣留 / 早晨放行语义（D-O4 节奏本体不变）。

    2026-09-09 D-Q1（Q-4 #267）：种子出厂**关**——1.0.78 把 08:20–01:00 按本机时区补进
    每台机器把美英客户的下午关掉了。这里显式开启 + 显式选时区再验节奏；种子本身
    enabled 必须为 false（红线②），建议班次仍预填 08:20/01:00。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.inbox.work_hours_gate import should_hold_auto_reply, work_schedule_cfg
    seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    ws = dict(work_schedule_cfg(seed))
    assert ws.get("enabled") is False, "D-Q1：班表出厂关（红线②）"
    assert ws["default"]["start"] == "08:20" and ws["default"]["end"] == "01:00"
    ws["enabled"] = True
    ws["timezone"] = "Asia/Shanghai"
    tz = ZoneInfo("Asia/Shanghai")
    night = datetime(2026, 9, 9, 3, 0, tzinfo=tz).timestamp()
    morning = datetime(2026, 9, 9, 10, 0, tzinfo=tz).timestamp()
    evening = datetime(2026, 9, 9, 22, 0, tzinfo=tz).timestamp()
    assert should_hold_auto_reply(ws, "telegram", "a1", peer_text="hi", now_ts=night) == "off_hours"
    assert should_hold_auto_reply(ws, "telegram", "a1", peer_text="hi", now_ts=morning) == ""
    assert should_hold_auto_reply(ws, "telegram", "a1", peer_text="hi", now_ts=evening) == ""
    # 08 后 0–40 min 补发窗：08:20 ± 20 min 抖动 → 08:00 前一定还在休、08:41 一定已开
    before = datetime(2026, 9, 9, 7, 59, tzinfo=tz).timestamp()
    after = datetime(2026, 9, 9, 8, 41, tzinfo=tz).timestamp()
    assert should_hold_auto_reply(ws, "telegram", "a1", peer_text="hi", now_ts=before) == "off_hours"
    assert should_hold_auto_reply(ws, "telegram", "a1", peer_text="hi", now_ts=after) == ""
    # 危机穿透
    assert should_hold_auto_reply(ws, "telegram", "a1", peer_text="I want to kill myself", now_ts=night) == ""


# ── 出厂基线 + 三预设 + 模板 / i18n ────────────────────────────────────────────

def test_factory_baseline_registry_and_seed():
    from src.utils.feature_registry import baseline_patch, by_key
    p = baseline_patch({})
    assert p["inbox.l2_autosend.deliver_delay.profile"] == "natural"
    assert p["inbox.auto_draft.inbound_merge.enabled"] is True
    # 2026-09-09 D-Q1（Q-4 #267）：班表撤出基线（A→B），不再补齐
    assert "inbox.work_schedule.enabled" not in p
    assert by_key("inbox.work_schedule.enabled").cls == "B"
    for k in ("inbox.l2_autosend.deliver_delay.profile", "inbox.auto_draft.inbound_merge.enabled"):
        assert by_key(k).cls == "A" and by_key(k).show is False
    # 存量机器：块里已显式写 profile（含 custom）一字不动
    part = baseline_patch({"inbox": {"l2_autosend": {"deliver_delay": {"profile": "custom"}}}})
    assert "inbox.l2_autosend.deliver_delay.profile" not in part
    seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    dd = seed["inbox"]["l2_autosend"]["deliver_delay"]
    assert dd["profile"] == "natural"
    assert seed["inbox"]["auto_draft"]["inbound_merge"]["enabled"] is True


def test_presets_each_carry_profile():
    assert PRESETS["cautious"]["inbox.l2_autosend.deliver_delay.profile"] == "slow"
    assert PRESETS["natural"]["inbox.l2_autosend.deliver_delay.profile"] == "natural"
    assert PRESETS["rapid"]["inbox.l2_autosend.deliver_delay.profile"] == "fast"
    assert FIELDS["inbox.l2_autosend.deliver_delay.profile"]["default"] == "custom"
    assert FIELDS["inbox.l2_autosend.deliver_delay.profile"]["hot"] == "worker"


def test_template_profile_row_and_i18n_keys():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="rps-profile-row"' in html and 'id="rps-profile"' in html
    assert 'feat: "profile"' in html and "rpsProfileEnabled" in html
    assert '"inbox.l2_autosend.deliver_delay.profile"' in html
    # 门禁 ①：<script> 里不许有 Jinja 注释（test_template_inline_js_syntax 只占位 {% %}/{{ }}）
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, flags=re.S):
        assert "{#" not in m.group(1), "reply_settings.html <script> 内出现 {# #} Jinja 注释"
    from src.web.i18n_packs.reply_settings_page import EN, ZH
    for k in ("rps_profile_label", "rps_profile_natural", "rps_profile_fast",
              "rps_profile_slow", "rps_profile_custom", "rps_profile_hint"):
        assert k in ZH and k in EN, k
        assert not re.search(r"[\u4e00-\u9fff]", EN[k]), (k, EN[k])
