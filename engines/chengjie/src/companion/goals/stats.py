"""营销目标观测（进程级单例，风格对齐 bazi_stats / avatar_voice_stats）。

漏斗五段：建目标 → 每日拍规划（含 hold 分桶）→ 注入生成链（draft/reply/proactive）
→ 主动桥真发 → 生命周期终态（done/failed/expired/cancelled）。
经 ``/api/workspace/metrics.goals`` + Prometheus ``goals_*`` 导出，
ops-overview「🎯 营销目标」卡消费。方法绝不抛。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class GoalStats:
    __slots__ = (
        "_lock", "_since", "created", "done", "failed", "expired", "cancelled",
        "paused", "beats_planned", "hold_emotion", "hold_silent",
        "injected_draft", "injected_reply", "injected_proactive",
        "injected_opener",
        "beats_sent_proactive", "settle_runs", "milestones_advanced",
        "feedback_adopt", "feedback_reject", "feedback_undo", "agenda_reads",
        "profile_captured", "catalog_injected",
        "catalog_cta_order", "catalog_cta_cs", "catalog_cta_roi",
        "auto_created", "profile_captured_llm",
        "orders_received", "orders_matched", "orders_late_settled",
        "link_stripped", "retention_created", "winback_created",
        "reconvert_created", "churn_captured", "churn_steered",
        "offer_cited", "offer_by_id", "offer_claims_stripped",
        "offer_strip_samples", "offer_strip_by_source",
        "offer_strip_by_persona", "claim_stripped",
        "deadline_shorten", "deadline_extend",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._since = time.time()
        self.created = 0
        self.done = 0
        self.failed = 0
        self.expired = 0
        self.cancelled = 0
        self.paused = 0
        self.beats_planned = 0
        self.hold_emotion = 0
        self.hold_silent = 0
        self.injected_draft = 0
        self.injected_reply = 0
        self.injected_proactive = 0
        self.injected_opener = 0
        self.beats_sent_proactive = 0
        self.settle_runs = 0
        self.milestones_advanced = 0
        self.feedback_adopt = 0
        self.feedback_reject = 0
        self.feedback_undo = 0
        self.agenda_reads = 0
        self.profile_captured = 0
        self.catalog_injected = 0
        self.catalog_cta_order = 0
        self.catalog_cta_cs = 0
        self.catalog_cta_roi = 0
        self.auto_created = 0
        self.profile_captured_llm = 0
        self.orders_received = 0
        self.orders_matched = 0
        self.orders_late_settled = 0
        self.link_stripped = 0
        self.retention_created = 0
        self.winback_created = 0
        self.reconvert_created = 0
        self.churn_captured = 0
        self.churn_steered = 0
        self.offer_cited = 0
        self.offer_by_id: Dict[str, int] = {}
        self.offer_claims_stripped = 0
        self.offer_strip_samples: Dict[str, int] = {}
        self.offer_strip_by_source: Dict[str, int] = {}
        self.offer_strip_by_persona: Dict[str, int] = {}
        self.claim_stripped = 0
        self.deadline_shorten = 0
        self.deadline_extend = 0

    # ── 记录（绝不抛）────────────────────────────────────────────────────────
    def record_created(self) -> None:
        with self._lock:
            self.created += 1

    def record_terminal(self, status: str) -> None:
        with self._lock:
            s = str(status or "")
            if s == "done":
                self.done += 1
            elif s == "failed":
                self.failed += 1
            elif s == "expired":
                self.expired += 1
            elif s == "cancelled":
                self.cancelled += 1

    def record_paused(self) -> None:
        with self._lock:
            self.paused += 1

    def record_beat_planned(self) -> None:
        with self._lock:
            self.beats_planned += 1

    def record_hold(self, reason: str) -> None:
        with self._lock:
            if str(reason) == "emotion":
                self.hold_emotion += 1
            else:
                self.hold_silent += 1

    def record_injected(self, chain: str) -> None:
        with self._lock:
            c = str(chain or "")
            if c == "draft":
                self.injected_draft += 1
            elif c == "proactive":
                self.injected_proactive += 1
            elif c == "opener":
                self.injected_opener += 1
            else:
                self.injected_reply += 1

    def record_beat_sent_proactive(self) -> None:
        with self._lock:
            self.beats_sent_proactive += 1

    def record_settle(self, *, milestones_advanced: int = 0) -> None:
        with self._lock:
            self.settle_runs += 1
            if milestones_advanced > 0:
                self.milestones_advanced += int(milestones_advanced)

    def record_feedback(self, verdict: str) -> None:
        with self._lock:
            if str(verdict) == "adopt":
                self.feedback_adopt += 1
            elif str(verdict) == "reject":
                self.feedback_reject += 1

    def record_feedback_undo(self) -> None:
        """坐席撤销一次采纳/驳回（P17）。**刻意不倒扣** adopt/reject——
        台账要的是「人审动过几次」，倒扣会把「反馈率」洗白成没发生过。"""
        with self._lock:
            self.feedback_undo += 1

    def record_agenda_read(self) -> None:
        """今日工作清单被读一次（P17）。**刻意不进 ``active`` 判据**——
        看板卡的显隐要看真业务流量，不能被「有人开过这页」点亮。"""
        with self._lock:
            self.agenda_reads += 1

    def record_profile_captured(self, slots: int = 1) -> None:
        with self._lock:
            self.profile_captured += max(1, int(slots or 1))

    def record_catalog_injected(self, cta: str = "") -> None:
        """目录块注入 +1；``cta`` 为 pick_cta 分级（order/cs/roi/空=纯种草）。"""
        with self._lock:
            self.catalog_injected += 1
            c = str(cta or "")
            if c == "order":
                self.catalog_cta_order += 1
            elif c == "cs":
                self.catalog_cta_cs += 1
            elif c == "roi":
                self.catalog_cta_roi += 1

    def record_auto_created(self) -> None:
        with self._lock:
            self.auto_created += 1

    def record_profile_captured_llm(self, slots: int = 1) -> None:
        with self._lock:
            self.profile_captured_llm += max(1, int(slots or 1))

    def record_order(self, *, matched: bool, late: bool = False) -> None:
        """``late=True``＝迟到结算（P6：订单到时目标已到期/失败，按硬事实复活
        为 done）——单独计数供观测「站外成交漏标」有多系统性。"""
        with self._lock:
            self.orders_received += 1
            if matched:
                self.orders_matched += 1
            if late:
                self.orders_late_settled += 1

    def record_link_stripped(self, n: int = 1) -> None:
        """链接纪律守卫剥离越纪律官网链 +n（P4 出站守卫命中面观测）。"""
        with self._lock:
            self.link_stripped += max(1, int(n or 1))

    def record_retention_created(self) -> None:
        """成交后自动起留存/续费目标 +1（P5 LTV 环开工面观测）。"""
        with self._lock:
            self.retention_created += 1

    def record_winback_created(self) -> None:
        """流失冷却后自动起挽回目标 +1（P6 流失挽回环观测）。"""
        with self._lock:
            self.winback_created += 1

    def record_reconvert_created(self) -> None:
        """挽回成功（回话）后自动起再转化目标 +1（P7 回流环观测）。"""
        with self._lock:
            self.reconvert_created += 1

    def record_churn_captured(self) -> None:
        """流失原因首次落画像 +1（P8 lifecycle 采集命中面；同客户重复命中不计）。"""
        with self._lock:
            self.churn_captured += 1

    def record_churn_steered(self) -> None:
        """流失原因驱动选品/CTA 转向 +1（P11：入门档深链或 cs/roi 偏置命中）。"""
        with self._lock:
            self.churn_steered += 1

    def record_offer_cited(self, offer_id: str = "") -> None:
        """运营授权活动被引用一次（P13）。按 id 分桶（上限 20 防目录刷爆）。"""
        with self._lock:
            self.offer_cited += 1
            oid = str(offer_id or "-").strip() or "-"
            if oid in self.offer_by_id or len(self.offer_by_id) < 20:
                self.offer_by_id[oid] = self.offer_by_id.get(oid, 0) + 1

    def record_claim_stripped(self, n: int = 1) -> None:
        """出站事实声明守卫处置数（P15：报价/试用时长与目录不符、gated 线泄漏、
        内部指令泄漏）。>0 = LLM 在说对不上登记事实的话，要查 prompt/目录。"""
        with self._lock:
            self.claim_stripped += max(0, int(n or 0))

    def record_deadline_edit(self, direction: str) -> None:
        """坐席改目标期限一次（P25 续）：``shorten``=加急 / ``extend``=延期。

        进程级脉搏（重启清零）——耐久口径在 goal_events 的
        ``deadline_days:X->Y`` 明细里，周审/P2 校准挖那份。加急占比高
        ＝坐席在跟模板默认节奏对着干＝default_days 该调的信号。"""
        with self._lock:
            d = str(direction or "")
            if d == "shorten":
                self.deadline_shorten += 1
            elif d == "extend":
                self.deadline_extend += 1

    def record_offer_claim_stripped(
        self, n: int = 1, samples: Any = (), source: str = "",
        persona: str = "",
    ) -> None:
        """出站守卫剥掉的未授权优惠承诺处数（P14；>0 = LLM 在编折扣，要查 prompt）。

        ``samples``＝命中片段（P15 观测：只记「打8折」这种片段不记整句——
        运营要的是「在编什么」，不是聊天原文；distinct 上限 24 防撑爆）。
        ``source``＝链路归属（chat=回复/拟稿、proactive=主动开场）。
        ``persona``＝人设归属（P16：哪个人设在编——收紧 prompt 有的放矢）。
        """
        with self._lock:
            self.offer_claims_stripped += max(0, int(n or 0))
            src = str(source or "").strip()
            if src and n:
                self.offer_strip_by_source[src] = (
                    self.offer_strip_by_source.get(src, 0) + max(0, int(n)))
            pid = str(persona or "").strip()[:48]
            if pid and n and (pid in self.offer_strip_by_persona
                              or len(self.offer_strip_by_persona) < 16):
                self.offer_strip_by_persona[pid] = (
                    self.offer_strip_by_persona.get(pid, 0) + max(0, int(n)))
            try:
                for frag in list(samples or [])[:8]:
                    key = str(frag or "").strip()[:40]
                    if not key:
                        continue
                    if (key in self.offer_strip_samples
                            or len(self.offer_strip_samples) < 24):
                        self.offer_strip_samples[key] = (
                            self.offer_strip_samples.get(key, 0) + 1)
            except Exception:
                pass

    # ── 导出 ────────────────────────────────────────────────────────────────
    def dump(self) -> Dict[str, Any]:
        with self._lock:
            injected = (self.injected_draft + self.injected_reply
                        + self.injected_proactive + self.injected_opener)
            out: Dict[str, Any] = {
                "since": self._since,
                "created": self.created,
                "terminal": {"done": self.done, "failed": self.failed,
                             "expired": self.expired,
                             "cancelled": self.cancelled},
                "paused": self.paused,
                "beats": {"planned": self.beats_planned,
                          "hold_emotion": self.hold_emotion,
                          "hold_silent": self.hold_silent,
                          "sent_proactive": self.beats_sent_proactive},
                "injected": {"draft": self.injected_draft,
                             "reply": self.injected_reply,
                             "proactive": self.injected_proactive,
                             "opener": self.injected_opener,
                             "total": injected},
                "feedback": {"adopt": self.feedback_adopt,
                             "reject": self.feedback_reject},
                # undo 单列不进 feedback 桶：那个桶是「adopt/reject 两态」的
                # 固定形状（看板/门禁按此断言），撤销是第三种动作不是第三种态。
                "feedback_undo": self.feedback_undo,
                "agenda_reads": self.agenda_reads,
                "settle_runs": self.settle_runs,
                "milestones_advanced": self.milestones_advanced,
                "profile_captured": self.profile_captured,
                "profile_captured_llm": self.profile_captured_llm,
                "catalog_injected": self.catalog_injected,
                "catalog_cta": {"order": self.catalog_cta_order,
                                "cs": self.catalog_cta_cs,
                                "roi": self.catalog_cta_roi},
                "auto_created": self.auto_created,
                "orders": {"received": self.orders_received,
                           "matched": self.orders_matched,
                           "late_settled": self.orders_late_settled},
                "link_stripped": self.link_stripped,
                "retention_created": self.retention_created,
                "winback_created": self.winback_created,
                "reconvert_created": self.reconvert_created,
                "churn_captured": self.churn_captured,
                "churn_steered": self.churn_steered,
                "offer_cited": self.offer_cited,
                "offer_by_id": dict(self.offer_by_id),
                "offer_claims_stripped": self.offer_claims_stripped,
                "offer_strip_samples": dict(self.offer_strip_samples),
                "offer_strip_by_source": dict(self.offer_strip_by_source),
                "offer_strip_by_persona": dict(self.offer_strip_by_persona),
                "claim_stripped": self.claim_stripped,
                "deadline_edits": {"shorten": self.deadline_shorten,
                                   "extend": self.deadline_extend},
            }
            out["active"] = bool(
                self.created or injected or self.beats_planned
                or self.done or self.failed or self.expired)
            return out

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP goals_created_total Marketing goals created",
                "# TYPE goals_created_total counter",
                f"goals_created_total {self.created}",
                "# HELP goals_terminal_total Goal terminal outcomes",
                "# TYPE goals_terminal_total counter",
                f'goals_terminal_total{{status="done"}} {self.done}',
                f'goals_terminal_total{{status="failed"}} {self.failed}',
                f'goals_terminal_total{{status="expired"}} {self.expired}',
                f'goals_terminal_total{{status="cancelled"}} {self.cancelled}',
                "# HELP goals_beats_total Daily beats planned/held/sent",
                "# TYPE goals_beats_total counter",
                f'goals_beats_total{{kind="planned"}} {self.beats_planned}',
                f'goals_beats_total{{kind="hold_emotion"}} {self.hold_emotion}',
                f'goals_beats_total{{kind="hold_silent"}} {self.hold_silent}',
                f'goals_beats_total{{kind="sent_proactive"}} {self.beats_sent_proactive}',
                "# HELP goals_injected_total Goal blocks injected into generation",
                "# TYPE goals_injected_total counter",
                f'goals_injected_total{{chain="draft"}} {self.injected_draft}',
                f'goals_injected_total{{chain="reply"}} {self.injected_reply}',
                f'goals_injected_total{{chain="proactive"}} {self.injected_proactive}',
                f'goals_injected_total{{chain="opener"}} {self.injected_opener}',
                "# HELP goals_milestones_advanced_total Milestone advances",
                "# TYPE goals_milestones_advanced_total counter",
                f"goals_milestones_advanced_total {self.milestones_advanced}",
                "# HELP goals_beat_feedback_total Agent feedback on daily beats",
                "# TYPE goals_beat_feedback_total counter",
                f'goals_beat_feedback_total{{verdict="adopt"}} {self.feedback_adopt}',
                f'goals_beat_feedback_total{{verdict="reject"}} {self.feedback_reject}',
                f'goals_beat_feedback_total{{verdict="undo"}} {self.feedback_undo}',
                "# HELP goals_agenda_reads_total Today-agenda list reads",
                "# TYPE goals_agenda_reads_total counter",
                f"goals_agenda_reads_total {self.agenda_reads}",
                "# HELP goals_profile_captured_total Profile slots auto-captured",
                "# TYPE goals_profile_captured_total counter",
                f"goals_profile_captured_total {self.profile_captured}",
                "# HELP goals_catalog_injected_total Catalog blocks injected",
                "# TYPE goals_catalog_injected_total counter",
                f"goals_catalog_injected_total {self.catalog_injected}",
                "# HELP goals_catalog_cta_total Catalog CTA tier picked at injection",
                "# TYPE goals_catalog_cta_total counter",
                f'goals_catalog_cta_total{{tier="order"}} {self.catalog_cta_order}',
                f'goals_catalog_cta_total{{tier="cs"}} {self.catalog_cta_cs}',
                f'goals_catalog_cta_total{{tier="roi"}} {self.catalog_cta_roi}',
                "# HELP goals_auto_created_total Goals auto-created on new conversations",
                "# TYPE goals_auto_created_total counter",
                f"goals_auto_created_total {self.auto_created}",
                "# HELP goals_profile_captured_llm_total Profile slots captured via LLM track",
                "# TYPE goals_profile_captured_llm_total counter",
                f"goals_profile_captured_llm_total {self.profile_captured_llm}",
                "# HELP goals_orders_total Order webhooks received/matched to goals",
                "# TYPE goals_orders_total counter",
                f'goals_orders_total{{kind="received"}} {self.orders_received}',
                f'goals_orders_total{{kind="matched"}} {self.orders_matched}',
                "# HELP goals_link_stripped_total Off-discipline site links stripped",
                "# TYPE goals_link_stripped_total counter",
                f"goals_link_stripped_total {self.link_stripped}",
                "# HELP goals_retention_created_total Retention goals auto-created on won deals",
                "# TYPE goals_retention_created_total counter",
                f"goals_retention_created_total {self.retention_created}",
                "# HELP goals_winback_created_total Winback goals auto-created after churn cooldown",
                "# TYPE goals_winback_created_total counter",
                f"goals_winback_created_total {self.winback_created}",
                "# HELP goals_reconvert_created_total Re-conversion goals auto-created after successful winback",
                "# TYPE goals_reconvert_created_total counter",
                f"goals_reconvert_created_total {self.reconvert_created}",
                "# HELP goals_churn_captured_total Churn reasons captured into customer profiles",
                "# TYPE goals_churn_captured_total counter",
                f"goals_churn_captured_total {self.churn_captured}",
                "# HELP goals_churn_steered_total Catalog/CTA steered by churn reason",
                "# TYPE goals_churn_steered_total counter",
                f"goals_churn_steered_total {self.churn_steered}",
                "# HELP goals_offer_cited_total Operator-authorized offers cited in chat",
                "# TYPE goals_offer_cited_total counter",
                f"goals_offer_cited_total {self.offer_cited}",
                "# HELP goals_offer_claims_stripped_total Unauthorized discount claims stripped from outbound",
                "# TYPE goals_offer_claims_stripped_total counter",
                f"goals_offer_claims_stripped_total {self.offer_claims_stripped}",
                "# HELP goals_offer_strip_by_source_total Offer-guard strips by outbound chain",
                "# TYPE goals_offer_strip_by_source_total counter",
                f'goals_offer_strip_by_source_total{{source="chat"}} '
                f"{self.offer_strip_by_source.get('chat', 0)}",
                f'goals_offer_strip_by_source_total{{source="proactive"}} '
                f"{self.offer_strip_by_source.get('proactive', 0)}",
                "# HELP goals_claim_stripped_total Outbound factual claims contradicting the catalog, stripped",
                "# TYPE goals_claim_stripped_total counter",
                f"goals_claim_stripped_total {self.claim_stripped}",
                "# HELP goals_deadline_edits_total Agent deadline adjustments (pace changes)",
                "# TYPE goals_deadline_edits_total counter",
                f'goals_deadline_edits_total{{direction="shorten"}} {self.deadline_shorten}',
                f'goals_deadline_edits_total{{direction="extend"}} {self.deadline_extend}',
            ]
            return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._since = time.time()
            self.created = 0
            self.done = 0
            self.failed = 0
            self.expired = 0
            self.cancelled = 0
            self.paused = 0
            self.beats_planned = 0
            self.hold_emotion = 0
            self.hold_silent = 0
            self.injected_draft = 0
            self.injected_reply = 0
            self.injected_proactive = 0
            self.injected_opener = 0
            self.beats_sent_proactive = 0
            self.settle_runs = 0
            self.milestones_advanced = 0
            self.feedback_adopt = 0
            self.feedback_reject = 0
            self.feedback_undo = 0
            self.agenda_reads = 0
            self.profile_captured = 0
            self.catalog_injected = 0
            self.catalog_cta_order = 0
            self.catalog_cta_cs = 0
            self.catalog_cta_roi = 0
            self.auto_created = 0
            self.profile_captured_llm = 0
            self.orders_received = 0
            self.orders_matched = 0
            self.link_stripped = 0
            self.retention_created = 0
            self.winback_created = 0
            self.reconvert_created = 0
            self.churn_captured = 0
            self.churn_steered = 0
            self.offer_cited = 0
            self.offer_by_id = {}
            self.offer_claims_stripped = 0
            self.offer_strip_samples = {}
            self.offer_strip_by_source = {}
            self.offer_strip_by_persona = {}
            self.claim_stripped = 0
            self.deadline_shorten = 0
            self.deadline_extend = 0


_SINGLETON: Optional[GoalStats] = None
_LOCK = threading.Lock()


def get_goal_stats() -> GoalStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = GoalStats()
    return _SINGLETON


__all__ = ["GoalStats", "get_goal_stats"]
