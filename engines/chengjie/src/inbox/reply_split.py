"""文本短句分条（bubble）— 陪伴域出站「像真人连发几条」的纯函数核心。

设计对齐语音 ``pack_voice_parts`` / Stephanie2 节奏模型，但刻意**先尊重 LLM
换行合同**（``ai_client._build_context_prompt`` 陪伴域已要求「每行 1 句、会被
拆成独立消息」）——算法切句只是兜底。

接线口径（调用方负责）：
  - 翻译之后、``_send_via`` 之前拆（对**译文**拆，避免中文分条后整段译碎）。
  - **仅编排器拥有的账号**拆条：LINE/WA/Messenger RPA runner 已有
    ``human_pacing.split_message``，再拆会双重切碎。
  - 群聊 / 桌面桥 / 结构化长单（含不可切断的 URL 独占超长行）保守不切或整段保留。
"""
from __future__ import annotations

import random
import re
import threading
from typing import Any, Dict, List, Optional

# 不可切断的「整块」：URL / 纯长数字单（订单号/手机号），硬切会毁掉可点性或可复制性
_URL_RE = re.compile(r"(?i)https?://\S+|www\.\S+")
_LONG_DIGIT_RE = re.compile(r"\d{8,}")


def parse_bubbles_cfg(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读 ``inbox.reply_style.bubbles``，返回归一化配置（全有默认值，永不抛）。"""
    cfg = config if isinstance(config, dict) else {}
    bubbles = (((cfg.get("inbox") or {}).get("reply_style") or {}).get("bubbles")
               if isinstance(cfg.get("inbox"), dict) else None)
    if not isinstance(bubbles, dict):
        bubbles = {}

    def _f(key: str, default: float) -> float:
        try:
            return float(bubbles.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _i(key: str, default: int, lo: int = 0, hi: int = 10_000) -> int:
        try:
            v = int(bubbles.get(key, default))
        except (TypeError, ValueError):
            v = int(default)
        return max(lo, min(hi, v))

    gap_lo = max(0.0, _f("gap_sec_lo", 0.8))
    gap_hi = max(gap_lo, _f("gap_sec_hi", 2.5))
    # P3 随机保留组：0..0.5——本可分条的消息按此概率强制整段（干净因果对照，
    # 与 bubbles 组比 3 天回复率）；0=不留对照（默认）。
    holdout = min(0.5, max(0.0, _f("holdout_pct", 0.0)))
    return {
        "enabled": bool(bubbles.get("enabled", False)),
        "holdout_pct": holdout,
        "max_parts": _i("max_parts", 3, lo=1, hi=5),
        "max_chars": _i("max_chars", 60, lo=20, hi=300),
        "min_tail_chars": _i("min_tail_chars", 4, lo=0, hi=40),
        "min_total_chars": _i("min_total_chars", 24, lo=0, hi=500),
        "gap_sec_lo": gap_lo,
        "gap_sec_hi": gap_hi,
        "per_char_sec": max(0.0, _f("per_char_sec", 0.03)),
        "skip_groups": bool(bubbles.get("skip_groups", True)),
        "orch_only": bool(bubbles.get("orch_only", True)),
    }


def looks_like_group_chat(platform: str, chat_key: str) -> bool:
    """廉价群聊启发式（零 DB）：Telegram 负数 peer id / WhatsApp ``@g.us`` jid = 群。

    WhatsApp(baileys) 是编排器接管路径，群 jid 若不识别会被当私聊拆条刷屏，
    必须在纯函数层兜住。其它平台无可靠 chat_key 约定时返回 False
    （交由调用方用 store.chat_type 补判）。
    """
    plat = str(platform or "").lower()
    key = str(chat_key or "").strip()
    if not key:
        return False
    if plat == "telegram":
        # -100… 超群 / -… 普通群；正数=私聊用户
        if key.startswith("-") and key[1:].isdigit():
            return True
    if plat == "whatsapp" and key.lower().endswith("@g.us"):
        return True
    return False


# ── 分条观测（进程级计数，autosend-status 汇总消费）────────────────────────────
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {"sends": {}, "parts_sent": 0, "partial": 0}


def record_bubble_send(source: str, parts: int, *, partial: bool = False) -> None:
    """记一次分条发送：``source`` ∈ autosend|manual|aline（未知归 other）。"""
    src = str(source or "other").strip() or "other"
    with _STATS_LOCK:
        _STATS["sends"][src] = int(_STATS["sends"].get(src, 0)) + 1
        _STATS["parts_sent"] = int(_STATS["parts_sent"]) + max(0, int(parts))
        if partial:
            _STATS["partial"] = int(_STATS["partial"]) + 1


def bubbles_metrics_snapshot() -> Dict[str, Any]:
    """导出分条发送计数（autosend-status 的 ``bubbles`` 段）。"""
    with _STATS_LOCK:
        sends = {k: int(v) for k, v in _STATS["sends"].items()}
        return {
            "sends": sum(sends.values()),
            "sends_by_source": sends,
            "parts_sent": int(_STATS["parts_sent"]),
            "partial": int(_STATS["partial"]),
        }


def _is_atomic_chunk(seg: str) -> bool:
    """含 URL 或长数字串的片段：禁止二次硬切（保持可点/可复制）。"""
    s = str(seg or "")
    return bool(_URL_RE.search(s) or _LONG_DIGIT_RE.search(s))


def _merge_short_tail(parts: List[str], min_tail_chars: int) -> List[str]:
    chunks = [p.strip() for p in parts if p and str(p).strip()]
    if (len(chunks) >= 2 and min_tail_chars > 0
            and len(chunks[-1]) < int(min_tail_chars)):
        tail = chunks.pop()
        sep = "\n" if ("\n" in chunks[-1] or "\n" in tail) else " "
        chunks[-1] = (chunks[-1] + sep + tail).strip()
    return chunks


def _cap_part_count(parts: List[str], max_parts: int) -> List[str]:
    chunks = [p.strip() for p in parts if p and str(p).strip()]
    keep = max(1, int(max_parts))
    if len(chunks) <= keep:
        return chunks
    head = chunks[: keep - 1]
    rest = chunks[keep - 1:]
    sep = "\n" if any(len(r) > 40 for r in rest) else " "
    return head + [sep.join(rest)]


def _from_newlines(text: str, max_parts: int, max_chars: int,
                   min_tail_chars: int) -> Optional[List[str]]:
    """LLM 换行合同路径：≥2 非空行 → 按行成条；超长行再句级打包。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    expanded: List[str] = []
    for ln in lines:
        if len(ln) <= max_chars or _is_atomic_chunk(ln):
            expanded.append(ln)
            continue
        # 超长且可切：复用语音侧句级打包（单一切句事实源）
        try:
            from src.ai.voice_clone_client import pack_voice_parts
            sub = pack_voice_parts(
                ln, part_max_chars=max_chars, max_parts=max_parts,
                min_tail_chars=min_tail_chars,
            )
            expanded.extend(sub or [ln])
        except Exception:
            expanded.append(ln)
    expanded = _merge_short_tail(expanded, min_tail_chars)
    expanded = _cap_part_count(expanded, max_parts)
    return expanded if len(expanded) >= 2 else None


def split_reply_parts(
    text: str,
    *,
    max_parts: int = 3,
    max_chars: int = 60,
    min_tail_chars: int = 4,
    min_total_chars: int = 24,
) -> List[str]:
    """把回复拆成 1..max_parts 条短消息文本。

    优先级：
      1. 空 → ``[]``
      2. 总长 < min_total_chars → 单条（短回复不装样子拆）
      3. LLM 换行合同（≥2 行）→ 按行
      4. 否则 ``pack_voice_parts`` 句级打包兜底
      5. 异常 / 切不出第二条 → ``[原文]``
    纯函数、防御式。
    """
    t = str(text or "").strip()
    if not t:
        return []
    max_parts = max(1, min(5, int(max_parts)))
    max_chars = max(20, int(max_chars))
    min_tail_chars = max(0, int(min_tail_chars))
    min_total_chars = max(0, int(min_total_chars))
    if min_total_chars and len(t) < min_total_chars:
        return [t]
    if max_parts <= 1:
        return [t]

    lined = _from_newlines(t, max_parts, max_chars, min_tail_chars)
    if lined and len(lined) >= 2:
        return lined

    # 整段是「原子块」（单 URL / 长单号）→ 不切
    if _is_atomic_chunk(t) and len(t.split()) <= 3:
        return [t]

    try:
        from src.ai.voice_clone_client import pack_voice_parts
        parts = pack_voice_parts(
            t, part_max_chars=max_chars, max_parts=max_parts,
            min_tail_chars=min_tail_chars,
        )
    except Exception:
        return [t]
    parts = [p.strip() for p in (parts or []) if p and str(p).strip()]
    if len(parts) < 2:
        return [t]
    return parts


def inter_part_delay_sec(
    next_text: str,
    *,
    gap_sec_lo: float = 0.8,
    gap_sec_hi: float = 2.5,
    per_char_sec: float = 0.03,
    rng: Optional[random.Random] = None,
) -> float:
    """条间延迟 = 基础思考抖动 + 下一条长度×打字速率（Stephanie2 简化版）。

    夹在 [gap_lo, gap_hi + 1.5]；``rng`` 可注入以便测试确定性。
    """
    lo = max(0.0, float(gap_sec_lo))
    hi = max(lo, float(gap_sec_hi))
    r = rng or random
    think = r.uniform(lo, hi)
    typing = max(0.0, len(str(next_text or ""))) * max(0.0, float(per_char_sec))
    # 思考占主导、打字微调；总时长封顶防拖垮投递 SLA
    return min(6.0, max(lo, think * 0.55 + typing))


def should_split_for_delivery(
    *,
    cfg: Dict[str, Any],
    platform: str,
    chat_key: str,
    orch_owns: bool,
    chat_type: str = "",
) -> bool:
    """投递侧总闸：配置开 +（非 orch_only 或编排器拥有）+ 非群。"""
    if not cfg or not cfg.get("enabled"):
        return False
    if cfg.get("orch_only", True) and not orch_owns:
        return False
    if cfg.get("skip_groups", True):
        ct = str(chat_type or "").strip().lower()
        if ct and ct not in ("private", ""):
            return False
        if looks_like_group_chat(platform, chat_key):
            return False
    return True
