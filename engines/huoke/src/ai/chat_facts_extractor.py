# -*- coding: utf-8 -*-
"""P10b: L3 结构化记忆的 LLM 抽取写侧 (B 独占)。

和 P10 MVP 读侧的配对。P10 MVP 已经把 ``fb_contact_events.meta_json`` 的
``extracted_facts`` reserved key 作为 L3 记忆源头, 任何写入都能被 B 的
``chat_memory.get_peer_extracted_facts`` 自动消费。本模块是**最常见的**
写入方式——跑 LLM 从对方 incoming 消息里自动抽取事实。

设计原则 (深入思考后的重构):

  * **默认关闭, config 激活**: 没跑过真机前,抽取参数(sampling 频率 /
    budget cap / prompt 措辞) 都是盲调。默认 ``enabled=False``,
    真机跑一段时间后通过 config 打开观察。零激活时完全 zero cost。
  * **严格 budget 控制**: 每设备每日 cap N 次 (默认 10); 每 peer 每
    20 小时最多 1 次。避免 LLM 成本失控。
  * **增量抽取**: Prompt 里带已知 facts, 让 LLM 只返回**新信息或更新**,
    不重复抽已知字段。
  * **Graceful 全链路**: LLM 不可用 / JSON 解析失败 / record_contact_event
    不存在 (A Phase 5 未 merge) — 任一环节失败都 silently skip,不影响
    主流程 _ai_reply_and_send。

集成点: ``_ai_reply_and_send`` 发送成功后调 ``run_facts_extraction()``,
和 wa_referral_sent contact_event 同位置 (已在最后一步)。

extracted_facts schema 见 ``chat_memory.get_peer_extracted_facts`` docstring。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExtractConfig:
    enabled: bool = False                # 主开关, 默认关
    daily_cap_per_device: int = 10       # 每设备每日抽取次数上限
    per_peer_min_hours: float = 20.0     # 同 peer 两次抽取最小间隔
    min_incomings_for_extraction: int = 3  # 对话至少 N 条 incoming 才抽
    max_incomings_per_call: int = 10     # 一次送给 LLM 最多 N 条
    max_tokens: int = 400                # LLM max_tokens
    temperature: float = 0.1             # 抽取要稳,低温


DEFAULT_CONFIG = ExtractConfig()


# ─────────────────────────────────────────────────────────────────────────────
# Sampling gate
# ─────────────────────────────────────────────────────────────────────────────

def _count_extractions_today(device_id: str) -> int:
    """查今日该设备已抽取次数 (从 fb_contact_events 读)。
    Phase 5 未 merge 时返 0。"""
    try:
        from src.host.database import _connect
    except Exception:
        return 0
    today = _dt.datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM fb_contact_events"
                " WHERE device_id=?"
                " AND event_type='facts_extracted'"
                " AND detected_at >= ?",
                (device_id, today),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _last_extraction_at(device_id: str, peer_name: str) -> Optional[_dt.datetime]:
    """查该 peer 上次抽取时间。未抽过或 Phase 5 未 merge 返 None。"""
    try:
        from src.host.database import _connect
    except Exception:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT MAX(detected_at) FROM fb_contact_events"
                " WHERE device_id=? AND peer_name=?"
                " AND event_type='facts_extracted'",
                (device_id, peer_name),
            ).fetchone()
        if not row or not row[0]:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                return _dt.datetime.strptime(row[0], fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _recent_incomings(device_id: str, peer_name: str,
                      limit: int) -> List[Dict[str, Any]]:
    """最近 N 条 incoming 消息文本 (按 id 升序)。"""
    try:
        from src.host.database import _connect
    except Exception:
        return []
    try:
        with _connect() as conn:
            sql = (
                "SELECT message_text, seen_at FROM ("
                " SELECT id, message_text, seen_at FROM facebook_inbox_messages"
                " WHERE device_id=? AND peer_name=? AND direction='incoming'"
                " AND COALESCE(message_text,'')<>''"
                " ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC"
            )
            rows = conn.execute(sql, (device_id, peer_name, int(limit))).fetchall()
        return [{"text": r[0], "seen_at": r[1]} for r in rows]
    except Exception as e:
        log.debug("[facts_extractor] 查询 incoming 失败: %s", e)
        return []


@dataclass
class ExtractDecision:
    should_run: bool
    reason: str
    incoming_count: int = 0
    device_extractions_today: int = 0


def should_run_extraction(device_id: str, peer_name: str, *,
                          config: Optional[ExtractConfig] = None,
                          now: Optional[_dt.datetime] = None
                          ) -> ExtractDecision:
    """Sampling gate — 是否应该对该 peer 跑抽取。

    依次检查 (失败即返 False + 原因):
      1. config.enabled (主开关)
      2. device_id + peer_name 非空
      3. 今日设备抽取次数 < daily_cap
      4. 该 peer 距上次抽取 >= per_peer_min_hours
      5. peer 的 incoming 条数 >= min_incomings_for_extraction
    """
    cfg = config or DEFAULT_CONFIG
    if not cfg.enabled:
        return ExtractDecision(False, "extraction_disabled")
    if not device_id or not peer_name:
        return ExtractDecision(False, "empty_device_or_peer")

    today_n = _count_extractions_today(device_id)
    if today_n >= int(cfg.daily_cap_per_device):
        return ExtractDecision(False, f"daily_cap_reached({today_n})",
                                device_extractions_today=today_n)

    now = now or _dt.datetime.utcnow()
    last = _last_extraction_at(device_id, peer_name)
    if last is not None:
        delta_h = (now - last).total_seconds() / 3600.0
        if delta_h < cfg.per_peer_min_hours:
            return ExtractDecision(
                False,
                f"peer_cooldown({delta_h:.1f}h<{cfg.per_peer_min_hours}h)",
                device_extractions_today=today_n)

    incomings = _recent_incomings(device_id, peer_name,
                                   cfg.max_incomings_per_call)
    if len(incomings) < int(cfg.min_incomings_for_extraction):
        return ExtractDecision(
            False,
            f"too_few_incomings({len(incomings)}<{cfg.min_incomings_for_extraction})",
            incoming_count=len(incomings),
            device_extractions_today=today_n)

    return ExtractDecision(True, "gate_pass",
                            incoming_count=len(incomings),
                            device_extractions_today=today_n)


# ─────────────────────────────────────────────────────────────────────────────
# LLM 抽取
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = """You extract structured facts about a user from
their Messenger messages. Output ONLY a JSON object - no prose.

Known facts (already extracted in prior calls):
{known_facts_json}

Extract or update these fields ONLY when new info is clearly present in
messages. SKIP fields without new info (don't guess).

Schema:
- birthday: "YYYY-MM-DD" or "YYYY-MM" (only if directly stated)
- occupation: short string, e.g. "designer" / "teacher" / "retired"
- interests: list of 1-5 tags, e.g. ["photography", "travel"]
- location: city / region, e.g. "Tokyo" / "Rome"
- family: dict, e.g. {{"status": "married", "kids": 2}}
- pain_points: list, e.g. ["insomnia", "neck pain"]
- budget_signal: "high" | "mid" | "low"
- timezone_hint: e.g. "UTC+9"

Output:
{{"updated_facts": {{...only changed or new fields...}},
  "reason": "one short sentence in user's language"}}

If nothing new, return {{"updated_facts": {{}}, "reason": "no new info"}}."""


def _call_llm_for_extraction(incoming_messages: List[Dict[str, Any]],
                             known_facts: Dict[str, Any],
                             config: ExtractConfig) -> Optional[Dict[str, Any]]:
    """调 LLM 抽取。失败返 None。永不抛。"""
    try:
        from src.ai.llm_client import LLMClient
        client = LLMClient()
    except Exception as e:
        log.debug("[facts_extractor] LLMClient 不可用: %s", e)
        return None

    # 压缩 known_facts 为 JSON 字符串
    try:
        known_str = json.dumps(known_facts or {}, ensure_ascii=False)[:2000]
    except Exception:
        known_str = "{}"

    sys_prompt = _EXTRACT_SYSTEM_PROMPT.replace("{known_facts_json}", known_str)

    # 用户消息 = 最近 N 条 incoming 列表
    lines: List[str] = []
    for i, m in enumerate(incoming_messages, 1):
        ts = (m.get("seen_at") or "").replace("T", " ").replace("Z", "")[:16]
        text = (m.get("text") or "").replace("\n", " ").strip()[:300]
        if not text:
            continue
        lines.append(f"{i}. [{ts}] {text}")
    user_msg = "User's recent incoming messages:\n" + "\n".join(lines)

    try:
        resp = client.chat_with_system(
            system=sys_prompt,
            user=user_msg,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    except Exception as e:
        log.debug("[facts_extractor] LLM 调用失败: %s", e)
        return None
    if not resp:
        return None

    # 容错解析
    s = resp.strip()
    if s.startswith("```"):
        s = "\n".join(ln for ln in s.splitlines()
                      if not ln.strip().startswith("```"))
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(s[i:j + 1])
    except Exception as e:
        log.debug("[facts_extractor] JSON 解析失败: %s resp=%r", e, s[:200])
        return None
    if not isinstance(data, dict):
        return None
    updated = data.get("updated_facts")
    if not isinstance(updated, dict):
        return None
    return {
        "updated_facts": updated,
        "reason": str(data.get("reason", ""))[:200],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 持久化
# ─────────────────────────────────────────────────────────────────────────────

def _persist_extraction(device_id: str, peer_name: str,
                        updated_facts: Dict[str, Any],
                        reason: str, preset_key: str = "") -> bool:
    """写入 fb_contact_events{event_type='facts_extracted',
    meta={extracted_facts: updated, reason: ...}}。

    返回 True=写入成功, False=Phase 5 未 merge / record_contact_event 缺失。
    """
    try:
        from src.host.fb_store import record_contact_event
    except ImportError:
        return False
    try:
        record_contact_event(
            device_id, peer_name, "facts_extracted",
            preset_key=preset_key,
            meta={
                "extracted_facts": updated_facts,
                "reason": reason,
            },
        )
        return True
    except Exception as e:
        log.debug("[facts_extractor] 写 contact_event 失败: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 主入口 (供 _ai_reply_and_send 调用)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExtractionRun:
    ran: bool
    decision_reason: str
    updated_facts: Dict[str, Any] = field(default_factory=dict)
    persisted: bool = False
    llm_reason: str = ""


def run_facts_extraction(device_id: str, peer_name: str, *,
                         preset_key: str = "",
                         config: Optional[ExtractConfig] = None,
                         now: Optional[_dt.datetime] = None
                         ) -> ExtractionRun:
    """主入口 — 一站式抽取 + 持久化, graceful 永不抛。

    流程:
      1. should_run_extraction 做 sampling gate (默认关, 需 config.enabled)
      2. 若通过 → 拉最近 incomings + 已有 facts (chat_memory) → 调 LLM
      3. LLM 返 {updated_facts, reason} → record_contact_event 写入
      4. 返回 ExtractionRun 结构体 (ran/decision_reason/updated_facts/persisted)

    任何一环失败都 graceful, 主流程 (_ai_reply_and_send) 不受影响。
    """
    cfg = config or DEFAULT_CONFIG
    decision = should_run_extraction(device_id, peer_name,
                                      config=cfg, now=now)
    if not decision.should_run:
        return ExtractionRun(ran=False,
                              decision_reason=decision.reason)

    # 拉已知 facts (L3 读侧, P10 MVP)
    known: Dict[str, Any] = {}
    try:
        from src.ai.chat_memory import get_peer_extracted_facts
        known = get_peer_extracted_facts(device_id, peer_name) or {}
    except Exception:
        pass

    incomings = _recent_incomings(device_id, peer_name,
                                   cfg.max_incomings_per_call)

    llm_result = _call_llm_for_extraction(incomings, known, cfg)
    if llm_result is None:
        return ExtractionRun(ran=False,
                              decision_reason="llm_call_failed")

    updated = llm_result.get("updated_facts") or {}
    llm_reason = llm_result.get("reason", "")

    # 空更新也不写 event (节省空行), 直接返
    if not updated:
        return ExtractionRun(ran=True,
                              decision_reason=decision.reason,
                              llm_reason=llm_reason,
                              persisted=False)

    persisted = _persist_extraction(device_id, peer_name,
                                     updated, llm_reason, preset_key)
    return ExtractionRun(ran=True,
                          decision_reason=decision.reason,
                          updated_facts=updated,
                          llm_reason=llm_reason,
                          persisted=persisted)
