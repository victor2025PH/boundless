"""P2：care LLM 影子扫描器门禁——安全不变量（只记不动）+ 预算 + 对照计数。"""
from __future__ import annotations

import json

import pytest

from src.contacts.care_shadow_scan import (
    CareShadowScanner, CareShadowStats, shadow_active,
)

# 夹具锚 now（防时间炸弹纪律 + B68 远期 sanity：2099 这类「永远的未来」正是
# 捕获侧要拦的日期解析错误形态，不能再当合法夹具用）：取 now+30 天。
from datetime import datetime as _dt_anchor, timedelta as _td_anchor
_FUTURE_DAY = (_dt_anchor.now() + _td_anchor(days=30)).date()
FUTURE_DATE_STR = _FUTURE_DAY.strftime("%Y-%m-%d")
FUTURE_JSON = ('{"found":true,"topic":"面试","date":"' + FUTURE_DATE_STR
               + '","greeting":false,"confidence":0.9}')
NOTFOUND_JSON = '{"found":false}'


class _FakeAI:
    """按调用序返回预置回复；记录 prompt 供断言。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def chat(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            return NOTFOUND_JSON
        return self.replies.pop(0)


def _cfg(enabled=True, shadow=True, budget=150):
    return {"enabled": enabled, "min_confidence": 0.6,
            "llm_extract": {"shadow": shadow, "daily_budget": budget}}


def _scanner(tmp_path, ai, cfg):
    return CareShadowScanner(
        ai_client=ai, cfg_provider=lambda: cfg, log_dir=tmp_path / "shadow",
        interval_sec=300, stats=CareShadowStats())


def _conv(cid="telegram:a:1", platform="telegram"):
    return {"conversation_id": cid, "platform": platform,
            "account_id": "a", "chat_key": "1"}


# ── shadow_active 判据 ───────────────────────────────────────────────────
def test_shadow_active_requires_engine_and_flag():
    assert shadow_active(_cfg(True, True))
    assert not shadow_active(_cfg(False, True))     # 引擎总闸关 → 影子一并停
    assert not shadow_active(_cfg(True, False))
    assert not shadow_active({})


# ── 入站回调：配置闸 + 廉价门 + 入队 ────────────────────────────────────
def test_inbound_cb_gates(tmp_path):
    ai = _FakeAI([])
    sc = _scanner(tmp_path, ai, _cfg())
    sc.inbound_cb(_conv(), "明天下午终面")        # 过门入队
    sc.inbound_cb(_conv(), "哈哈哈")              # 廉价门拒
    sc.inbound_cb(_conv(), "")                    # 空文本忽略
    assert sc.stats.enqueued == 1
    assert sc.stats.gate_rejected == 1
    assert sc.snapshot()["queue"] == 1


def test_inbound_cb_silent_when_disabled(tmp_path):
    sc = _scanner(tmp_path, _FakeAI([]), _cfg(enabled=False))
    sc.inbound_cb(_conv(), "明天下午终面")
    assert sc.stats.enqueued == 0 and sc.snapshot()["queue"] == 0


def test_inbound_cb_excludes_group_chats(tmp_path):
    """B68（实施67 P2-i）：群聊消息不进扫描队列——报障群消息被 LLM 捕成
    「关怀约定」实锤两条（值守现场 cancel）；关怀约定是私聊语义。"""
    sc = _scanner(tmp_path, _FakeAI([]), _cfg())
    conv_g = _conv(cid="telegram:a:g1")
    conv_g["chat_type"] = "group"
    sc.inbound_cb(conv_g, "明天下午终面记得复测")
    assert sc.stats.enqueued == 0
    # 显式 private / 缺省（向后兼容）都照常入队
    conv_p = _conv(cid="telegram:a:p1")
    conv_p["chat_type"] = "private"
    sc.inbound_cb(conv_p, "明天下午终面")
    sc.inbound_cb(_conv(cid="telegram:a:p2"), "明天下午终面")  # 无 chat_type
    assert sc.stats.enqueued == 2


def test_inbound_cb_scan_gate_all_admits_no_time_signal(tmp_path):
    """scan_gate=all（2026-08-18 评估加速）：无时间信号的入站也进队——
    窄门 7 天只攒 1 条 llm_only（切主判据要 ≥20），预算内买证据积累速度。"""
    cfg = _cfg()
    cfg["llm_extract"]["scan_gate"] = "all"
    sc = _scanner(tmp_path, _FakeAI([]), cfg)
    sc.inbound_cb(_conv(), "哈哈哈")            # 旧门必拒的闲聊
    sc.inbound_cb(_conv(cid="c2"), "明天面试")  # 有时间信号照常
    sc.inbound_cb(_conv(cid="c3"), "")          # 空文本仍忽略
    assert sc.stats.enqueued == 2
    assert sc.stats.gate_rejected == 0
    # 缺省/未知值回落窄门（旧行为逐位不变）
    cfg2 = _cfg()
    cfg2["llm_extract"]["scan_gate"] = "bogus"
    sc2 = _scanner(tmp_path, _FakeAI([]), cfg2)
    sc2.inbound_cb(_conv(), "哈哈哈")
    assert sc2.stats.gate_rejected == 1


def test_queue_drop_oldest(tmp_path):
    sc = CareShadowScanner(
        ai_client=_FakeAI([]), cfg_provider=lambda: _cfg(),
        log_dir=tmp_path, queue_max=10, stats=CareShadowStats())
    for i in range(13):
        sc.inbound_cb(_conv(cid=f"c{i}"), f"明天第{i}件事")
    assert sc.snapshot()["queue"] == 10
    assert sc.stats.dropped == 3


# ── drain：对照计数 + JSONL 落盘 + 绝不写库 ─────────────────────────────
async def test_run_once_llm_only_vs_both(tmp_path):
    # 泰语（正则必漏）→ LLM 命中 = llm_only；中文（正则命中）+ LLM 命中 = both
    ai = _FakeAI([FUTURE_JSON, FUTURE_JSON])
    sc = _scanner(tmp_path, ai, _cfg())
    sc.inbound_cb(_conv(cid="th"), "พรุ่งนี้ไปสัมภาษณ์งาน")
    sc.inbound_cb(_conv(cid="zh"), "明天下午去面试")
    n = await sc.run_once()
    assert n == 2
    s = sc.stats
    assert s.llm_calls == 2 and s.llm_ok == 2 and s.llm_err == 0
    assert s.llm_found == 2
    assert s.llm_only == 1          # 泰语条
    assert s.both_found == 1        # 中文条
    files = list((tmp_path / "shadow").glob("shadow-*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    th = next(r for r in lines if r["cid"] == "th")
    assert th["regex"] == [] and th["llm"]["found"] is True and th["agree"] is False


async def test_run_once_regex_only_and_agree_notfound(tmp_path):
    # LLM 判 found=false 而正则命中 → regex_only；双方都没命中 → agree
    ai = _FakeAI([NOTFOUND_JSON, NOTFOUND_JSON])
    sc = _scanner(tmp_path, ai, _cfg())
    sc.inbound_cb(_conv(cid="zh"), "明天去复查")
    sc.inbound_cb(_conv(cid="noise"), "weekend vibes hahaha")   # 过门但双方都不算约定
    await sc.run_once()
    s = sc.stats
    assert s.regex_only == 1
    assert s.llm_found == 0
    files = list((tmp_path / "shadow").glob("*.jsonl"))
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    noise = next(r for r in lines if r["cid"] == "noise")
    assert noise["agree"] is True


async def test_run_once_budget_exhaustion(tmp_path):
    ai = _FakeAI([FUTURE_JSON] * 3)
    sc = _scanner(tmp_path, ai, _cfg(budget=1))
    for i in range(3):
        sc.inbound_cb(_conv(cid=f"c{i}"), "明天面试")
    n = await sc.run_once()
    assert n == 3                       # 三条都被 drain
    assert sc.stats.llm_calls == 1      # 只烧了 1 次预算
    assert sc.stats.budget_skipped == 2


async def test_run_once_idle_when_shadow_off(tmp_path):
    ai = _FakeAI([FUTURE_JSON])
    cfg = _cfg()
    sc = _scanner(tmp_path, ai, cfg)
    sc.inbound_cb(_conv(), "明天面试")
    cfg["llm_extract"]["shadow"] = False       # 热闸：drain 前被关掉
    assert await sc.run_once() == 0
    assert sc.stats.llm_calls == 0
    assert sc.snapshot()["queue"] == 1          # 队列保留，重新开闸可续跑


async def test_llm_error_counted_not_raised(tmp_path):
    class _BoomAI:
        async def chat(self, prompt):
            raise RuntimeError("boom")

    sc = _scanner(tmp_path, _BoomAI(), _cfg())
    sc.inbound_cb(_conv(), "明天面试")
    n = await sc.run_once()
    assert n == 1
    assert sc.stats.llm_err == 1
    files = list((tmp_path / "shadow").glob("*.jsonl"))
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["llm"] == {"error": True}


def test_snapshot_shape(tmp_path):
    sc = _scanner(tmp_path, _FakeAI([]), _cfg())
    snap = sc.snapshot()
    for key in ("enqueued", "gate_rejected", "dropped", "drained", "llm_calls",
                "llm_only", "regex_only", "both_found", "budget_used_today",
                "queue", "running", "last_tick_ts",
                "captured", "capture_skipped_pending", "capture_invalid"):
        assert key in snap
    assert snap["running"] is False


# ── P4：真实捕获模式（llm_extract.enabled） ──────────────────────────────
from datetime import datetime as _dt  # noqa: E402

from src.contacts.care_commitment import CareCommitment  # noqa: E402
from src.contacts.care_schedule import CareScheduleStore  # noqa: E402


def _cfg_capture(budget=150):
    return {"enabled": True, "min_confidence": 0.6,
            "llm_extract": {"shadow": True, "enabled": True,
                            "daily_budget": budget},
            "dedup_window_days": 3}


def _scanner_cap(tmp_path, ai, cfg, care):
    return CareShadowScanner(
        ai_client=ai, cfg_provider=lambda: cfg, log_dir=tmp_path / "shadow",
        interval_sec=300, stats=CareShadowStats(), care_store=care)


async def test_capture_llm_only_writes_store(tmp_path):
    care = CareScheduleStore(":memory:")
    sc = _scanner_cap(tmp_path, _FakeAI([FUTURE_JSON]), _cfg_capture(), care)
    sc.inbound_cb(_conv(cid="th:1"), "พรุ่งนี้ไปสัมภาษณ์งาน")
    await sc.run_once()
    assert sc.stats.captured == 1
    rows = care.list_pending()
    assert len(rows) == 1
    r = rows[0]
    assert r["contact_key"] == "th:1"
    assert r["platform"] == "telegram" and r["account_id"] == "a"
    assert r["chat_key"] == "1"
    assert r["topic"] == "面试"
    due = _dt.fromtimestamp(r["due_at"])
    assert ((due.year, due.month, due.day, due.hour)
            == (_FUTURE_DAY.year, _FUTURE_DAY.month, _FUTURE_DAY.day, 20))
    # JSONL 记 captured=true
    import json as _json
    files = list((tmp_path / "shadow").glob("*.jsonl"))
    rec = _json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["captured"] is True


async def test_capture_off_in_pure_shadow(tmp_path):
    care = CareScheduleStore(":memory:")
    cfg = _cfg_capture()
    cfg["llm_extract"]["enabled"] = False       # 纯影子
    sc = _scanner_cap(tmp_path, _FakeAI([FUTURE_JSON]), cfg, care)
    sc.inbound_cb(_conv(cid="th:1"), "พรุ่งนี้ไปสัมภาษณ์งาน")
    await sc.run_once()
    assert sc.stats.llm_only == 1
    assert sc.stats.captured == 0
    assert care.count() == 0


async def test_capture_skipped_when_regex_also_found(tmp_path):
    # 正则命中的消息 ingest 时已由 care_capture 入库——扫描器绝不重复写（防双写①）
    care = CareScheduleStore(":memory:")
    sc = _scanner_cap(tmp_path, _FakeAI([FUTURE_JSON]), _cfg_capture(), care)
    sc.inbound_cb(_conv(cid="zh:1"), "明天下午去面试")
    await sc.run_once()
    assert sc.stats.both_found == 1
    assert sc.stats.captured == 0
    assert care.count() == 0


async def test_capture_same_day_pending_guard(tmp_path):
    # 同联系人同事件日已有 pending（正则昨天按「面试」入过）→ LLM 的「终面」不再入（防双写②）
    care = CareScheduleStore(":memory:")
    ev = _dt(_FUTURE_DAY.year, _FUTURE_DAY.month, _FUTURE_DAY.day, 0, 0, 0).timestamp()
    care.add_commitment(
        CareCommitment(due_at=ev + 20 * 3600, event_at=ev, topic="面试",
                       sentiment="neutral", anchor_text="regex",
                       source_text="x", confidence=0.9),
        contact_key="th:1", platform="telegram", account_id="a", chat_key="1",
        min_confidence=0.0, dedup_window_days=0.0)
    interview_variant = ('{"found":true,"topic":"终面","date":"' + FUTURE_DATE_STR
                         + '","greeting":false,"confidence":0.9}')
    sc = _scanner_cap(tmp_path, _FakeAI([interview_variant]), _cfg_capture(), care)
    sc.inbound_cb(_conv(cid="th:1"), "พรุ่งนี้ไปสัมภาษณ์งาน")
    await sc.run_once()
    assert sc.stats.captured == 0
    assert sc.stats.capture_skipped_pending == 1
    assert care.count(status="pending") == 1     # 还是那一条


async def test_capture_invalid_past_date(tmp_path):
    care = CareScheduleStore(":memory:")
    past = '{"found":true,"topic":"面试","date":"2001-01-01","confidence":0.9}'
    sc = _scanner_cap(tmp_path, _FakeAI([past]), _cfg_capture(), care)
    sc.inbound_cb(_conv(cid="th:1"), "พรุ่งนี้ไปสัมภาษณ์งาน")
    await sc.run_once()
    assert sc.stats.capture_invalid == 1
    assert care.count() == 0


async def test_capture_without_store_is_noop(tmp_path):
    # enabled=true 但没注入 care_store（异常部署形态）→ 只对照不写、不炸
    sc = _scanner(tmp_path, _FakeAI([FUTURE_JSON]), _cfg_capture())
    sc.inbound_cb(_conv(cid="th:1"), "พรุ่งนี้ไปสัมภาษณ์งาน")
    await sc.run_once()
    assert sc.stats.llm_only == 1 and sc.stats.captured == 0
