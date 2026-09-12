# -*- coding: utf-8 -*-
"""入站图 / 视频的「观察事实」（Q-36 #313 2JK95C · #318 VV7BRY，2026-09-12）。

两起实录同一根：**画面所见被当成客户事实、再被放大**。

- 2JK95C：客户「刚做了几个菜」+ 图，AI 出站「你一个人做**一桌菜**」——量词被 LLM 夸大；
- VV7BRY：客户只发过一张「水边房子」的照片，AI 主动开场却问「how the **Marina**'s treating you」——
  识图描述里的地物 / 地名被当成客户住处 / 专名引用。

本模块把识图 caption（``[图片内容] …`` / ``[视频内容] …``，A/B 线同字面）收成一类事实：

1. :func:`note_inbound`：入站带 caption → FactGate ``kind=observation``（caption 逐字在同一条入站里）
   → 写 InboxStore KV ``image_observation:<cid>``（**含原 caption**，TTL 24h，最多留 3 条）——与 Q-29
   ``peer_time`` 的 hint KV 同模式（``get_app_setting / set_app_setting``，store 缺席软失败）。
2. :func:`observation_captions`：出站守卫取 caption（KV 优先 → 无 KV 时从近史用户文本解析）。
3. :func:`soften_quantifiers`：出站里与 caption 原词冲突的**夸大量词**（一桌 / 满桌 / 好多 / 一堆 /
   a whole table of / tons of…）**软改**回 caption 原词（「一桌菜」→「几个菜」），只在 caption 有该名词的
   **小数量**原词时改；改不了的不硬拦（B 段是软改写 + 事实锚，不是硬拦）。
4. :func:`observation_line` / :func:`format_inbound_media_line`：proactive / goal / 接力摘要注入的固定措辞
   「TA 发过一张…的照片」+ 「画面所见 ≠ TA 自述、地物不当住处 / 专名」规则句。

纯函数 + 两个软失败 I/O（KV 读写）；任何异常 → 不改文本 / 不写 / 返回空。门禁 ``tests/test_media_intent_q36.py``。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

KV_PREFIX = "image_observation:"
MAX_KEEP = 3
DESC_MARKERS: Tuple[Tuple[str, str], ...] = (("[图片内容]", "image"), ("[视频内容]", "video"))
_CAPTION_MAX = 240


def _ttl() -> float:
    try:
        from src.companion.fact_gate import OBSERVATION_TTL_SEC
        return float(OBSERVATION_TTL_SEC)
    except Exception:
        return 24 * 3600.0


TTL_SEC = _ttl()

#: 注入 / 约束共用的规则句（ai_client 媒体块、proactive 上下文、接力摘要同源）
CAPTION_RULE = (
    "只描述 / 引用画面里**看到的具体物件与数量**（以上面的识别内容为准），"
    "不加「一桌 / 满桌 / 好多 / 一堆 / 满满」这类量词或程度词去夸大；"
    "画面里的地物 / 地名 / 房子（码头、海边、别墅…）只是画面所见，"
    "**不要当作对方的住处、所在地或专名**去引用；不确定就不说。"
)
OBSERVATION_RULE = (
    "画面描述是识图所见，不是 TA 说的话：物件和数量只按描述说，不加「一桌 / 满桌 / 好多」类量词；"
    "画面里的地物 / 地名 / 房子不当作 TA 的住处、所在地或专名去引用；不确定就不提。"
)


# ── caption 解析 ────────────────────────────────────────────────────────────

def split_caption(text: Any) -> Tuple[str, str, str]:
    """``配文\\n[图片内容] 描述`` → ``(配文, 描述, kind)``；无标记 → ``(text, "", "")``。"""
    t = str(text or "")
    idx, mk_len, kind = -1, 0, ""
    for mk, k in DESC_MARKERS:
        i = t.find(mk)
        if i >= 0 and (idx < 0 or i < idx):
            idx, mk_len, kind = i, len(mk), k
    if idx < 0:
        return t.strip(), "", ""
    return t[:idx].strip(), " ".join(t[idx + mk_len:].split()).strip(), kind


def caption_from_inbound(text: Any, media_desc: Any = "") -> Tuple[str, str]:
    """入站文本（或显式 ``media_desc``）→ ``(描述, kind)``；无描述 → ``("", "")``。"""
    desc = " ".join(str(media_desc or "").split()).strip()
    _cap, parsed, kind = split_caption(text)
    if not desc:
        desc = parsed
    if not desc:
        return "", ""
    return desc[:_CAPTION_MAX], (kind or "image")


def gate_caption(desc: Any, inbound_text: Any) -> Tuple[bool, str]:
    """FactGate ``kind=observation``：caption 必须逐字在这条入站里（识图描述本就写在行内）。"""
    d = " ".join(str(desc or "").split()).strip()
    if not d:
        return False, "no_evidence"
    try:
        from src.companion import fact_gate
        return fact_gate.check(d, slot_or_kind=fact_gate.KIND_OBSERVATION, evidence=d,
                               inbound_texts=[{"direction": "in", "text": str(inbound_text or "")}])
    except Exception:
        return False, "gate_error"


# ── KV（TTL 24h，含原 caption）────────────────────────────────────────────────

def kv_key(conversation_id: str) -> str:
    return f"{KV_PREFIX}{str(conversation_id or '').strip()}"


def _store(inbox_store: Any) -> Any:
    if inbox_store is not None:
        return inbox_store
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        return None


def read_observations(conversation_id: str, *, inbox_store: Any = None,
                      now: Optional[float] = None) -> List[Dict[str, Any]]:
    """KV 里未过期的观察（新→旧）；无 / 坏值 → []。绝不抛。"""
    st = _store(inbox_store)
    cid = str(conversation_id or "").strip()
    if st is None or not cid or not hasattr(st, "get_app_setting"):
        return []
    try:
        raw = st.get_app_setting(kv_key(cid), "") or ""
        if not raw:
            return []
        data = json.loads(raw)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        n = float(now if now is not None else time.time())
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict) or not str(it.get("caption") or "").strip():
                continue
            ts = float(it.get("ts") or 0)
            if ts <= 0 or n - ts > float(it.get("ttl_sec") or TTL_SEC):
                continue
            out.append(it)
        out.sort(key=lambda x: -float(x.get("ts") or 0))
        return out
    except Exception:
        return []


def _write(inbox_store: Any, conversation_id: str, items: List[Dict[str, Any]]) -> bool:
    st = _store(inbox_store)
    if st is None or not hasattr(st, "set_app_setting"):
        return False
    try:
        payload = json.dumps({"items": items[:MAX_KEEP]}, ensure_ascii=False)
        try:
            st.set_app_setting(kv_key(conversation_id), payload, updated_by="image_observation")
        except TypeError:
            st.set_app_setting(kv_key(conversation_id), payload)
        return True
    except Exception:
        logger.debug("[observation] write failed", exc_info=True)
        return False


def note_inbound(text: Any, conversation_id: str, *, media_desc: Any = "", inbox_store: Any = None,
                 ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """客户一条带识图描述的入站 → FactGate ``kind=observation`` → 写 KV（TTL 24h，含原 caption）。
    返回写入条目；无描述 / 门不过 / 无会话 → None。绝不抛。"""
    cid = str(conversation_id or "").strip()
    try:
        desc, kind = caption_from_inbound(text, media_desc)
        if not desc:
            return None
        # 显式 media_desc 可能与行文本分开到达（B 线 autodraft 先拿到 desc 再回写行）——
        # 作为 evidence 语料把两者拼在一起，门只核「caption 逐字在这条入站的语料里」。
        corpus = str(text or "")
        if desc not in corpus:
            corpus = f"{corpus}\n[图片内容] {desc}" if corpus else f"[图片内容] {desc}"
        ok, why = gate_caption(desc, corpus)
        if not ok:
            logger.info("[observation] drop conv=%s caption=%r reason=%s", cid or "-", desc[:60], why)
            return None
        t = float(ts if ts is not None else time.time())
        item: Dict[str, Any] = {"caption": desc, "kind": kind, "ts": t, "ttl_sec": TTL_SEC}
        if cid:
            old = [it for it in read_observations(cid, inbox_store=inbox_store, now=t)
                   if str(it.get("caption") or "") != desc]
            _write(inbox_store, cid, [item] + old)
        logger.info("[observation] note conv=%s kind=%s ttl=24h caption=%r", cid or "-", kind, desc[:80])
        return item
    except Exception:
        logger.debug("[observation] note_inbound failed", exc_info=True)
        return None


def captions_from_texts(texts: Optional[Sequence[Any]]) -> List[str]:
    """近史用户文本里的识图描述（新→旧；无 TTL 信息，只作 KV 缺席时的回落）。"""
    out: List[str] = []
    for t in reversed(list(texts or [])):
        if isinstance(t, dict):
            t = t.get("content") or t.get("text") or ""
        _cap, desc, _kind = split_caption(t)
        if desc and desc not in out:
            out.append(desc[:_CAPTION_MAX])
        if len(out) >= MAX_KEEP:
            break
    return out


def observation_captions(conversation_id: str = "", *, user_texts: Optional[Sequence[Any]] = None,
                         inbox_store: Any = None, now: Optional[float] = None) -> List[str]:
    """出站守卫用：KV（24h 内）优先；KV 空 → 从 ``user_texts`` 解析。绝不抛。"""
    try:
        got = [str(it.get("caption") or "") for it in read_observations(conversation_id, inbox_store=inbox_store, now=now)]
        got = [g for g in got if g]
        if got:
            return got[:MAX_KEEP]
        return captions_from_texts(user_texts)
    except Exception:
        return []


# ── 注入措辞（proactive / goal / 接力摘要）──────────────────────────────────

def observation_line(desc: Any, *, kind: str = "image", who: str = "TA", max_chars: int = 80) -> str:
    """固定措辞：「TA 发过一张…的照片（识图所见：…）」。"""
    d = " ".join(str(desc or "").split()).strip()
    if not d:
        return ""
    if len(d) > max_chars:
        d = d[: max_chars - 1] + "…"
    if str(kind or "image") == "video":
        return f"{who}发过一段视频（缩略图所见：{d}）"
    return f"{who}发过一张照片（识图所见：{d}）"


def format_inbound_media_line(text: Any, *, who: str = "TA", max_chars: int = 80) -> str:
    """入站行文本 → 若含识图描述，改成「配文 + TA 发过一张照片（识图所见：…）」；否则原样。"""
    t = str(text or "")
    cap, desc, kind = split_caption(t)
    if not desc:
        return t
    line = observation_line(desc, kind=kind or "image", who=who, max_chars=max_chars)
    if cap and not (cap.startswith("[") and cap.endswith("]")):
        return f"{cap}（{line}）"
    return line


def observation_note(captions: Sequence[Any], *, who: str = "TA") -> str:
    """多条观察 → 一段注入文（含规则句）；空 → ''。"""
    lines = [observation_line(c, who=who) for c in (captions or []) if str(c or "").strip()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    return "【对方发过的图（识图所见）】" + "；".join(lines[:MAX_KEEP]) + "。" + OBSERVATION_RULE


# ── 出站量词软改（「一桌菜」→ caption 原词「几个菜」）──────────────────────────

_SMALL_NUM_ZH = "一两二三四五几幾数數"
_CL_ZH = (r"(?:个|個|盘|盤|碗|道|份|只|隻|条|條|张|張|本|杯|瓶|块|塊|件|双|雙|朵|棵|辆|輛|台|部|间|間|颗|顆|片|串|根|支|把|套|"
          r"盒|袋|包|箱|桶|罐|锅|鍋|碟|叠|疊|排|位|名|口|种|種|样|樣)")
# 夸大量词（出站侧）：量词 + 可选修饰 + 名词 1-2 字
_INFLATE_ZH = re.compile(
    r"(?P<q>(?:满满(?:的|一大|一)?|滿滿(?:的|一大|一)?|好多|好几|好幾|一大堆|一堆|一大桌子?|一整桌子?|一桌子|一桌|"
    r"满桌子|满桌|滿桌子|滿桌|一屋子|一大片|一整片|这么多|這麼多|那么多|那麼多|超多|好大一桌|整整一桌|一大锅|一大鍋|"
    r"满满一锅|一大盘|一大盤|一大碗)(?:的)?)(?P<adj>[好大美香丰盛豐]{0,2})(?P<n>[\u4e00-\u9fff]{1,2})"
)
_INFLATE_EN = re.compile(
    r"\b(?P<q>a\s+whole\s+table\s+of|a\s+table\s+full\s+of|a\s+(?:huge|big|massive|whole)\s+(?:spread|feast)\s+of|"
    r"tons\s+of|a\s+ton\s+of|loads\s+of|so\s+many|a\s+bunch\s+of|plenty\s+of|lots\s+of|a\s+lot\s+of|piles\s+of|"
    r"a\s+pile\s+of|a\s+mountain\s+of|heaps\s+of|dozens\s+of|countless)\s+(?P<adj>(?:delicious|yummy|tasty|amazing|nice|beautiful|cute|little)\s+)?(?P<n>[a-z]+)",
    re.IGNORECASE,
)
_SMALL_NUM_EN = r"(?:a\s+couple\s+of|a\s+few|two|three|four|five|[1-5])"


def _small_count_phrase_zh(caption: str, noun: str) -> str:
    """caption 里「几个菜 / 两盘饺子」这类**小数量**原词（末尾是该名词）；无 → ''。"""
    if not noun:
        return ""
    rx = re.compile(r"(?:[" + _SMALL_NUM_ZH + r"]|[1-5])\s*" + _CL_ZH + r"\s*(?:小|大)?" + re.escape(noun))
    m = rx.search(caption)
    return m.group(0) if m else ""


def _small_count_phrase_en(caption: str, noun: str) -> str:
    stem = noun.lower().rstrip("s") if len(noun) > 3 else noun.lower()
    if not stem:
        return ""
    rx = re.compile(r"\b" + _SMALL_NUM_EN + r"\s+(?:\w+\s+)?" + re.escape(stem) + r"(?:s|es)?\b", re.IGNORECASE)
    m = rx.search(caption)
    return m.group(0) if m else ""


def soften_quantifiers(text: Any, captions: Sequence[Any]) -> Tuple[str, List[Dict[str, str]]]:
    """出站里与 caption 冲突的夸大量词 → 改回 caption 原词。返回 ``(text, hits)``，
    ``hits=[{from, to, caption}]``；caption 无该名词的小数量原词 → 不动（软改写，不硬拦）。绝不抛。"""
    src = str(text or "")
    caps = [" ".join(str(c or "").split()) for c in (captions or []) if str(c or "").strip()]
    if not src.strip() or not caps:
        return src, []
    hits: List[Dict[str, str]] = []
    out = src
    try:
        # zh
        def _zh(m: "re.Match[str]") -> str:
            q, adj, n = m.group("q"), m.group("adj") or "", m.group("n")
            for cap in caps:
                for noun in (n, n[:1]):
                    ph = _small_count_phrase_zh(cap, noun)
                    if ph:
                        rest = n[len(noun):]
                        frm = q + adj + noun
                        to = ph[: len(ph) - len(noun)] + adj + noun     # 保留形容词：满桌子好菜 → 几个好菜
                        if frm != to:
                            hits.append({"from": frm, "to": to, "caption": cap[:60]})
                        return to + rest
            return m.group(0)
        out = _INFLATE_ZH.sub(_zh, out)

        # en
        def _en(m: "re.Match[str]") -> str:
            q, adj, n = m.group("q"), m.group("adj") or "", m.group("n")
            for cap in caps:
                ph = _small_count_phrase_en(cap, n)
                if ph:
                    frm = m.group(0)
                    if frm.lower() != ph.lower():
                        hits.append({"from": frm, "to": ph, "caption": cap[:60]})
                    return ph
            return m.group(0)
        out = _INFLATE_EN.sub(_en, out)
    except Exception:
        return src, []
    if not out.strip():
        return src, []
    return out, hits


__all__ = [
    "KV_PREFIX", "MAX_KEEP", "TTL_SEC", "CAPTION_RULE", "OBSERVATION_RULE",
    "split_caption", "caption_from_inbound", "gate_caption",
    "note_inbound", "read_observations", "captions_from_texts", "observation_captions",
    "observation_line", "format_inbound_media_line", "observation_note",
    "soften_quantifiers",
]
