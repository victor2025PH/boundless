# -*- coding: utf-8 -*-
"""风险放行影子台账（#160 I-1-C，2026-09-04）。

老板拍板 v2：风险扣稿一条不留、全部放行，**触发只写台账**；攒满一个月真实
流量再定新拦截规则。本模块就是那份台账。

两层口径，别混：
  · **JSONL 落盘**＝持久口径、一个月数据的权威来源。落点
    ``logs/autosend_shadow/shadow_YYYYMMDD.jsonl``（相对 CWD——生产进程 CWD 是实例
    数据根，相对路径在这里是**正确**的，见 tools/audit_relative_paths 的 C 类；
    测试/CLI 用 ``AITR_AUTOSEND_SHADOW_DIR`` 覆写落点）。与 care.llm_extract 影子档
    同款：进程计数器重启就清零，**JSONL 才是持久口径**。
  · **进程内计数器**＝ops 看板/metrics 的实时读数（按 hold_reason 分桶、
    stop_contact / self_harm 单列——那两个是要人看的）。

⛔ **不记出站/入站原文**：``conv_key`` + ``draft_id`` + ``ts`` 已足够回溯到
protocol_media 里的原始会话做抽样复核；落原文只是白担隐私风险。文本只留
``text_fp``（sha1 前 8 位）与 ``text_len``。

``risk_hits``（命中词）**必须有**——没有它一个月后分不清「真该拦」还是「正则误伤」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DIR = "logs/autosend_shadow"
ENV_DIR = "AITR_AUTOSEND_SHADOW_DIR"
FILE_PREFIX = "shadow_"

# 要人看的两个 reason：命中即时推值守群（不拦，只报）
ALERT_REASONS = ("stop_contact", "self_harm")

# 台账字段契约（CLI 与门禁按它读；新增字段只许追加，不许改名/删除——
# 一个月后补不回来的字段现在不加就永久缺失）
RECORD_FIELDS = (
    "ts", "platform", "account_id", "conv_key", "draft_id",
    "would_hold_level", "hold_reason", "peer_risk", "peer_reasons",
    "reply_risk", "reply_reasons", "risk_hits", "text_fp", "text_len",
    # 追加字段（v1 即有，供分析分桶）
    "stage",            # peer=入站定级时 / reply=AI 稿收尾时 / analysis=LLM 分析 overlay
    "automation_mode",  # 会话档位（应恒为 auto_ai——review 不进台账）
    "policy_mode",      # shadow（enforce 档不写本台账）
    # v1.1（2026-09-04 二批）：一个月后补不回来的维度
    "kind",             # hold（本会被扣）| outcome（放行后的去向，见 OUTCOME_FIELDS）
    "lang",             # 会话/入站语言（中英正则误伤率分开看）
    "intent",           # quick_analyze 意图（白给）
    "emotion",          # quick_analyze 情绪（白给）
    "persona_id",       # 生效人设（「人设 A 的客户更常叫停」这类结论靠它）
    "peer_text_fp",     # 入站原话 sha1 前 8 位（比 conv_key+ts 更准的回溯键；不落原文）
    "peer_msg_id",      # 触发拟稿的入站消息 id（messages.message_id，与转录回写同口径；可空）
    "peer_msg_match",   # peer_msg_id 怎么来的：exact（正文逐字相同）/ newest（取最新入站）/ 空
)
# 契约冻结：一个月台账靠字段名分析，改名/删除/重排前缀都会让旧行读不出来。
# 门禁 test_record_fields_contract_is_append_only 钉住——新字段只许追加到末尾。
RECORD_FIELDS_FROZEN_V1 = (
    "ts", "platform", "account_id", "conv_key", "draft_id",
    "would_hold_level", "hold_reason", "peer_risk", "peer_reasons",
    "reply_risk", "reply_reasons", "risk_hits", "text_fp", "text_len",
    "stage", "automation_mode", "policy_mode",
)

# 放行后的去向（kind=outcome 行；与 hold 行按 draft_id 关联）。台账只记「放行」不记
# 「送达」就答不了「放出去的稿到底发出去了没」——worker 的 send_block / mode_downgraded /
# channel_mutex / superseded 都可能在放行之后取消它。
OUTCOME_FIELDS = (
    "kind", "ts", "draft_id", "outcome", "reason", "hold_reason", "hold_ts",
    "latency_sec", "platform", "account_id", "conv_key",
)
# outcome 取值：sent（sent_at>0，真送达）/ cancelled（worker/陈旧作废，reason=decided_by）
# / rejected（人工拒）/ delivery_failed（approved 后 worker 记了 autosend_failed 审计，
# reason=「类别:原文」，类别复用 send_health.classify_fail_reason：gate=闸门节流 /
# permanent=无发言权等 / platform=平台故障 / other）/ approved_unsent（approved 但超宽限仍无
# sent_at 且无失败审计＝延迟窗内客户插话放弃、人工通过未接投递等）/ missing（草稿行已被清理）
OUTCOMES = ("sent", "cancelled", "rejected", "delivery_failed", "approved_unsent", "missing")
# approved 后多久没 sent_at 才算终局（拟人延迟窗 + 分条间隔最长约 2–3 分钟，留足余量）
DEFAULT_OUTCOME_GRACE_SEC = 30 * 60
# hold 行多久还没终局就放弃追踪（草稿被 cleanup_old_drafts 清了也归 missing）
DEFAULT_OUTCOME_MAX_AGE_SEC = 7 * 86400
# 在途稿复查 autosend_failed 审计的最小间隔（reconcile 每 tick 跑，别每 tick 都翻审计）
AUDIT_RECHECK_SEC = 120.0
_PENDING_CAP = 5000


_WRITE_LOCK = threading.Lock()


def _day_key(ts: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts))


class ShadowStats:
    """进程级计数器（重启清零；持久口径看 JSONL）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.by_reason: Dict[str, int] = {}
            self.by_level: Dict[str, int] = {}
            self.by_stage: Dict[str, int] = {}
            self.by_hit: Dict[str, int] = {}
            self.outcomes: Dict[str, int] = {}
            self.alerts = 0
            self.write_errors = 0
            self.last_ts = 0.0
            self.last_reason = ""
            self._day = _day_key(time.time())
            self.today = 0
            self.warmed_from = ""     # 非空＝计数器从磁盘台账回填过（跨重启口径）

    def bump(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            now = float(rec.get("ts") or time.time())
            day = _day_key(now)
            if day != self._day:
                self._day, self.today = day, 0
            self.total += 1
            self.today += 1
            r = str(rec.get("hold_reason") or "unknown")
            self.by_reason[r] = self.by_reason.get(r, 0) + 1
            lv = str(rec.get("would_hold_level") or "?")
            self.by_level[lv] = self.by_level.get(lv, 0) + 1
            st = str(rec.get("stage") or "?")
            self.by_stage[st] = self.by_stage.get(st, 0) + 1
            for h in (rec.get("risk_hits") or [])[:8]:
                hk = str(h)[:40]
                if len(self.by_hit) < 200 or hk in self.by_hit:
                    self.by_hit[hk] = self.by_hit.get(hk, 0) + 1
            self.last_ts = max(self.last_ts, now)
            self.last_reason = r

    def bump_outcome(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            o = str(rec.get("outcome") or "unknown")
            self.outcomes[o] = self.outcomes.get(o, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            top_hits = sorted(self.by_hit.items(), key=lambda kv: -kv[1])[:10]
            sent = self.outcomes.get("sent", 0)
            settled = sum(self.outcomes.values())
            return {
                "total": self.total,
                "today": self.today if self._day == _day_key(time.time()) else 0,
                "by_reason": dict(sorted(self.by_reason.items(), key=lambda kv: -kv[1])),
                "by_level": dict(self.by_level),
                "by_stage": dict(self.by_stage),
                "top_hits": [{"hit": k, "n": v} for k, v in top_hits],
                "stop_contact": self.by_reason.get("stop_contact", 0),
                "self_harm": self.by_reason.get("self_harm", 0),
                # 放行后的去向（已终局的那部分；pending_outcomes＝还在等 worker/人处置）
                "outcomes": dict(sorted(self.outcomes.items(), key=lambda kv: -kv[1])),
                "settled": settled,
                "sent": sent,
                "sent_rate": (round(sent / settled, 3) if settled else None),
                "pending_outcomes": len(_PENDING),
                "alerts": self.alerts,
                "write_errors": self.write_errors,
                "last_ts": self.last_ts,
                "last_reason": self.last_reason,
                "warmed_from": self.warmed_from,
                "dir": str(shadow_dir()),
            }


_STATS = ShadowStats()

# 待终局登记：draft_id → hold 行摘要。record() 登记；reconcile_outcomes() 查草稿行终态
# 后写 outcome 行并注销。重启后由 warm_from_disk() 从近几天 JSONL 回填（hold 减 outcome）。
_PENDING: Dict[str, Dict[str, Any]] = {}
# 登记表被 web 线程（record）与 worker 线程池（reconcile）同时碰——迭代时被插入会抛，
# 首次回填也可能被两个线程同时触发，统一走这把锁。
_PENDING_LOCK = threading.RLock()
_WARMED = False


def _pending_put(rec: Dict[str, Any]) -> None:
    did = str(rec.get("draft_id") or "")
    if not did:
        return
    with _PENDING_LOCK:
        if len(_PENDING) >= _PENDING_CAP and did not in _PENDING:
            # 满了淘汰最老的一条（极端情况下宁丢旧账不撑爆内存）
            oldest = min(_PENDING.items(), key=lambda kv: float(kv[1].get("ts") or 0))[0]
            _PENDING.pop(oldest, None)
        _PENDING[did] = {
            "ts": float(rec.get("ts") or time.time()),
            "hold_reason": str(rec.get("hold_reason") or ""),
            "platform": str(rec.get("platform") or ""),
            "account_id": str(rec.get("account_id") or ""),
            "conv_key": str(rec.get("conv_key") or ""),
        }


def warm_from_disk(days: int = 3, *, force: bool = False) -> int:
    """重启后回填：进程计数器 + 待终局登记 ← 近 N 天 JSONL。

    进程计数器重启就清零是既有教训（care 影子档）；本机每天重启数次，不回填的话
    ops 卡每天都从 0 起跳。返回回填的 hold 行数。幂等（只做一次，``force`` 重做），
    且必须先于本进程第一次 bump（三个入口 record / reconcile / stats_snapshot 都先调它）。
    """
    global _WARMED
    with _PENDING_LOCK:
        if _WARMED and not force:
            return 0
        _WARMED = True
        n = 0
        settled: set = set()
        holds: List[Dict[str, Any]] = []
        for rec in iter_records(days):
            if str(rec.get("kind") or "hold") == "outcome":
                settled.add(str(rec.get("draft_id") or ""))
                _STATS.bump_outcome(rec)
            else:
                holds.append(rec)
        # iter_records 按文件名（日期）升序产出 → bump 的按日翻转逻辑成立，today 回填正确。
        # 回填后计数器语义＝「近 N 天 + 本进程」（卡上标 warmed_from），跨月累计看 CLI。
        for rec in holds:
            _STATS.bump(rec)
            n += 1
            did = str(rec.get("draft_id") or "")
            if did and did not in settled:
                _pending_put(rec)
        _STATS.warmed_from = f"{days}d"
        return n


def get_stats() -> ShadowStats:
    return _STATS


def stats_snapshot() -> Dict[str, Any]:
    """metrics / ops 卡消费口。零命中时 total=0（卡片按此隐藏）。

    首次调用先从磁盘回填（幂等）：重启后 ops 一读就是近几天口径，不是 0。
    """
    try:
        warm_from_disk()
    except Exception:
        logger.debug("autosend_shadow warm_from_disk 失败（忽略）", exc_info=True)
    return _STATS.snapshot()


def shadow_dir() -> Path:
    return Path(os.environ.get(ENV_DIR) or DEFAULT_DIR)


def shadow_file_for(ts: float) -> Path:
    return shadow_dir() / f"{FILE_PREFIX}{_day_key(ts)}.jsonl"


def text_fingerprint(text: str) -> str:
    """出站文本 sha1 前 8 位——只用于「同一稿」去重/回溯，不可逆。"""
    return hashlib.sha1(str(text or "").encode("utf-8", "ignore")).hexdigest()[:8]


def build_record(
    *,
    platform: str,
    account_id: str,
    conv_key: str,
    draft_id: str,
    would_hold_level: str,
    hold_reason: str,
    peer_risk: str,
    peer_reasons: Iterable[str],
    reply_risk: str,
    reply_reasons: Iterable[str],
    risk_hits: Iterable[str],
    text: str = "",
    stage: str = "peer",
    automation_mode: str = "auto_ai",
    policy_mode: str = "shadow",
    ts: Optional[float] = None,
    lang: str = "",
    intent: str = "",
    emotion: str = "",
    persona_id: str = "",
    peer_text: str = "",
    peer_msg_id: str = "",
    peer_msg_match: str = "",
) -> Dict[str, Any]:
    """按 RECORD_FIELDS 契约组装一行 hold 记录（不落盘）。

    ``text``（出站稿）与 ``peer_text``（入站原话）都只取指纹与长度，绝不入账。
    """
    return {
        "ts": float(ts if ts is not None else time.time()),
        "platform": str(platform or ""),
        "account_id": str(account_id or ""),
        "conv_key": str(conv_key or ""),
        "draft_id": str(draft_id or ""),
        "would_hold_level": str(would_hold_level or ""),
        "hold_reason": str(hold_reason or ""),
        "peer_risk": str(peer_risk or "low"),
        "peer_reasons": [str(r) for r in (peer_reasons or [])],
        "reply_risk": str(reply_risk or "low"),
        "reply_reasons": [str(r) for r in (reply_reasons or [])],
        # 命中词＝正则匹配到的**短语片段**（截 40 字），不是整条消息
        "risk_hits": [str(h)[:40] for h in (risk_hits or [])][:16],
        "text_fp": text_fingerprint(text) if text else "",
        "text_len": len(str(text or "")),
        "stage": str(stage or "peer"),
        "automation_mode": str(automation_mode or ""),
        "policy_mode": str(policy_mode or "shadow"),
        "kind": "hold",
        "lang": str(lang or "")[:16],
        "intent": str(intent or "")[:32],
        "emotion": str(emotion or "")[:32],
        "persona_id": str(persona_id or "")[:64],
        "peer_text_fp": text_fingerprint(peer_text) if peer_text else "",
        "peer_msg_id": str(peer_msg_id or "")[:64],
        "peer_msg_match": str(peer_msg_match or "")[:8],
    }


def _append_line(rec: Dict[str, Any]) -> bool:
    try:
        p = shadow_file_for(float(rec.get("ts") or time.time()))
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with _WRITE_LOCK:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except Exception:
        _STATS.write_errors += 1
        logger.warning("autosend_shadow 台账落盘失败（已忽略）", exc_info=True)
        return False


def record(rec: Dict[str, Any]) -> bool:
    """落一行 hold 记录（JSONL）+ 计数 + 登记待终局。任何异常只记日志不抛——台账绝不阻塞发送。

    先回填再落行：磁盘回填必须发生在本进程**第一次** bump 之前，否则本进程刚写的行
    会被回填再数一遍（record / reconcile / stats_snapshot 三个入口都先 warm，幂等）。
    """
    try:
        warm_from_disk()
    except Exception:
        logger.debug("autosend_shadow warm_from_disk 失败（忽略）", exc_info=True)
    ok = _append_line(rec)
    _STATS.bump(rec)
    _pending_put(rec)
    return ok


def classify_outcome(row: Optional[Dict[str, Any]], hold_ts: float, *,
                     now: Optional[float] = None,
                     grace_sec: float = DEFAULT_OUTCOME_GRACE_SEC,
                     max_age_sec: float = DEFAULT_OUTCOME_MAX_AGE_SEC,
                     failed_reason: Optional[str] = None) -> Tuple[str, str]:
    """草稿行终态 → (outcome, reason)；("", "") 表示尚未终局（继续等）。纯函数，门禁钉口径。

    - ``sent_at>0`` → sent（无论谁批的：autosend 与人工通过后的真投递都会 mark_draft_sent）
    - status cancelled → cancelled，reason=decided_by（channel_mutex / mode_downgraded /
      send_blocked / pilot_native / superseded_by_inbound / work_schedule_regen / stale_peer…）
    - status rejected → rejected（人工拒）
    - status approved 且 sent_at==0：
        · ``failed_reason`` 非 None（调用方从 draft_audit_log 读到 autosend_failed）→
          **立即** delivery_failed，不等宽限（投递失败宁丢不重发，行不会再变）；
        · 否则宽限内视为在途（拟人延迟窗/分条间隔）；超宽限 → approved_unsent
          （延迟窗内客户插话放弃 / 人工通过未接投递 / 进程中途重启）
    - 行不存在（被 cleanup_old_drafts 清理）→ missing；pending/enriching 超 max_age → missing
    """
    now_f = float(now if now is not None else time.time())
    if row is None:
        return ("missing", "row_gone") if now_f - hold_ts > 60 else ("", "")
    if float(row.get("sent_at") or 0) > 0:
        return "sent", str(row.get("decided_by") or "")
    status = str(row.get("status") or "")
    if status == "cancelled":
        return "cancelled", str(row.get("decided_by") or "")
    if status == "rejected":
        return "rejected", str(row.get("decided_by") or "")
    if status == "approved":
        if failed_reason is not None:
            return "delivery_failed", str(failed_reason or "")
        decided_at = float(row.get("decided_at") or row.get("updated_at") or hold_ts)
        if now_f - decided_at >= grace_sec:
            return "approved_unsent", str(row.get("error") or row.get("decided_by") or "")
        return "", ""
    if now_f - hold_ts >= max_age_sec:
        return "missing", f"stale_{status or 'unknown'}"
    return "", ""


def failed_reason_from_audit(store: Any, draft_id: str) -> Optional[str]:
    """从 draft_audit_log 取该稿的 autosend_failed 原因（无则 None）。

    worker 的投递失败**不改草稿行**（宁丢不重发，只写审计 ``record_autosend_failure``），
    所以行上看不出失败；这里读审计补齐。返回「类别:原文」——类别复用 send_health 的
    归因（gate=闸门节流 / permanent=无发言权等 / platform=平台故障 / other），一个月后
    一眼分清「被反封号闸门拦了」和「平台真故障」。任何异常 → None（按无失败审计处理）。
    """
    fn = getattr(store, "list_draft_audit", None)
    if not callable(fn):
        return None
    try:
        rows = fn(draft_id=draft_id, limit=10) or []
    except Exception:
        return None
    for r in rows:
        if str((r or {}).get("action") or "") == "autosend_failed":
            reason = str(r.get("reason") or "")
            try:
                from src.inbox.send_health import classify_fail_reason
                cat = classify_fail_reason(reason)
            except Exception:
                cat = "other"
            return f"{cat}:{reason[:48]}" if reason else cat
    return None


def reconcile_outcomes(store: Any, *, now: Optional[float] = None, max_items: int = 200,
                       grace_sec: float = DEFAULT_OUTCOME_GRACE_SEC,
                       max_age_sec: float = DEFAULT_OUTCOME_MAX_AGE_SEC) -> int:
    """把待终局的 hold 记录对照草稿行终态，写 outcome 行。返回本轮写出的行数。

    单一收口点：worker 里 6 处取消路径 + 投递成功/失败点都不用各埋一个钩子——它们
    最终都体现在 reply_drafts 行的 status/decided_by/sent_at 上，这里按结果读。
    只读 ``store.get_draft``（主键查询，≤max_items 次），任何异常只记日志不抛。
    """
    if store is None or not hasattr(store, "get_draft"):
        return 0
    warm_from_disk()
    now_f = float(now if now is not None else time.time())
    written = 0
    # 老的先查（先到先终局），单轮封顶防大积压时拖慢 worker tick；先拷贝快照再查库，
    # 查库期间不持锁（record 不被阻塞）
    with _PENDING_LOCK:
        batch = sorted(((k, dict(v)) for k, v in _PENDING.items()),
                       key=lambda kv: float(kv[1].get("ts") or 0))[:max_items]
    for did, meta in batch:
        try:
            row = store.get_draft(did)
        except Exception:
            logger.debug("reconcile: get_draft(%s) 异常（跳过）", did, exc_info=True)
            continue
        hold_ts = float(meta.get("ts") or 0)
        # approved 且未送达的稿才去翻审计（其它终态行上就看得出）；同一稿在途期间最多
        # 每 AUDIT_RECHECK_SEC 查一次审计——worker tick 30-60s 一轮，宽限 30min 内不
        # 节流会对同一条稿打几十次审计查询
        failed: Optional[str] = None
        if (row is not None and str(row.get("status") or "") == "approved"
                and float(row.get("sent_at") or 0) <= 0):
            last_chk = float(meta.get("audit_checked_ts") or 0)
            if now_f - last_chk >= AUDIT_RECHECK_SEC:
                failed = failed_reason_from_audit(store, did)
                with _PENDING_LOCK:
                    if did in _PENDING:
                        _PENDING[did]["audit_checked_ts"] = now_f
        outcome, reason = classify_outcome(
            row, hold_ts, now=now_f, grace_sec=grace_sec, max_age_sec=max_age_sec,
            failed_reason=failed)
        if not outcome:
            continue
        rec = {
            "kind": "outcome",
            "ts": now_f,
            "draft_id": did,
            "outcome": outcome,
            "reason": str(reason or "")[:64],
            "hold_reason": str(meta.get("hold_reason") or ""),
            "hold_ts": hold_ts,
            "latency_sec": round(max(0.0, now_f - hold_ts), 1),
            "platform": str(meta.get("platform") or ""),
            "account_id": str(meta.get("account_id") or ""),
            "conv_key": str(meta.get("conv_key") or ""),
        }
        if _append_line(rec):
            written += 1
        _STATS.bump_outcome(rec)
        with _PENDING_LOCK:
            _PENDING.pop(did, None)
    return written


def pending_count() -> int:
    with _PENDING_LOCK:
        return len(_PENDING)


def _reset_pending_for_tests() -> None:
    global _WARMED
    with _PENDING_LOCK:
        _PENDING.clear()
        _WARMED = False


def maybe_alert(rec: Dict[str, Any]) -> bool:
    """``stop_contact`` / ``self_harm`` 命中 → 即时 EventBus 事件（不拦，只报）。

    事件 ``autosend_shadow_alert``，webhook 订阅别名 ``autosend_shadow``；
    ``rate_key`` 按 ``account:conv`` 防同会话刷屏。文案带会话 id + 命中词 +
    「已放行，如需止损请人工介入」。返回是否已发布。
    """
    reason = str(rec.get("hold_reason") or "")
    if reason not in ALERT_REASONS:
        return False
    try:
        from src.integrations.shared.event_bus import get_event_bus
        acct = str(rec.get("account_id") or "")
        conv = str(rec.get("conv_key") or "")
        get_event_bus().publish("autosend_shadow_alert", {
            "reason": reason,
            "platform": str(rec.get("platform") or ""),
            "account_id": acct,
            "conv_key": conv,
            "draft_id": str(rec.get("draft_id") or ""),
            "would_hold_level": str(rec.get("would_hold_level") or ""),
            "risk_hits": list(rec.get("risk_hits") or [])[:6],
            "stage": str(rec.get("stage") or ""),
            "rate_key": f"{acct}:{conv}",
        })
        _STATS.alerts += 1
        return True
    except Exception:
        logger.debug("autosend_shadow_alert 发布失败（已忽略）", exc_info=True)
        return False


def iter_records(days: int = 30, *, base: Optional[Path] = None,
                 now: Optional[float] = None) -> Iterable[Dict[str, Any]]:
    """只读遍历近 N 天 JSONL（CLI 用）。坏行跳过不抛。"""
    base = Path(base) if base is not None else shadow_dir()
    if not base.exists():
        return
    now = float(now if now is not None else time.time())
    keep = {_day_key(now - 86400 * i) for i in range(max(1, int(days)))}
    for p in sorted(base.glob(f"{FILE_PREFIX}*.jsonl")):
        day = p.stem[len(FILE_PREFIX):]
        if day not in keep:
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        yield obj
        except Exception:
            logger.debug("读取 %s 失败（跳过）", p, exc_info=True)


__all__ = [
    "ALERT_REASONS", "RECORD_FIELDS", "OUTCOME_FIELDS", "OUTCOMES", "DEFAULT_DIR", "ENV_DIR",
    "DEFAULT_OUTCOME_GRACE_SEC", "DEFAULT_OUTCOME_MAX_AGE_SEC",
    "build_record", "record", "maybe_alert", "iter_records",
    "classify_outcome", "failed_reason_from_audit", "reconcile_outcomes", "warm_from_disk",
    "pending_count",
    "stats_snapshot", "get_stats", "shadow_dir", "shadow_file_for", "text_fingerprint",
]
