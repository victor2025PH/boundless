"""聊天记录导入（跨平台档案 P1，2026-08-18）：解析 → LLM 摘要/候选事实 → 接地标记。

管线三段，除 LLM 段全部纯函数可单测：
1. **解析**：WhatsApp / 微信（第三方导出）txt、Telegram Desktop JSON、通用 CSV →
   统一 ``{ts, sender, text}``；编码回退（utf-8-sig → utf-16 → gb18030）兜微信/QQ
   常见 GBK 导出；体量守卫（≤5MB / ≤5000 条）。
2. **摘要**：LLM 出严格 JSON（话题域 + 一句话背景 + 候选事实带原话出处），
   坏 JSON 宽松解析，失败软降级（返回 error，不阻断导入——运营可手填）。
3. **接地**：每条候选事实过 ``memory_grounding`` 词汇重叠 + quote 原文核对，
   不接地 → ``grounded=False``（UI 默认不勾选、标「存疑」）——防 LLM 编造事实
   混进人工确认清单（Phase8 教训的导入版）。

原文件解析后即弃：调用方只留 sha256 进批次台账（隐私 + 防重复导入）。
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Dict, List, Optional

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_MESSAGES = 5000
MAX_FACTS = 8
MAX_TOPICS = 8
_SAMPLE_HEAD = 60
_SAMPLE_TAIL = 60
_SAMPLE_MAX_CHARS = 6000

# 「我方」惯用名（猜客户 sender 时排除；大小写不敏感）
_SELF_HINTS = {"我", "me", "you", "自己", "本人"}

# ── 解码 / 格式探测 ──────────────────────────────────────────────────────────

def decode_bytes(data: bytes) -> str:
    """聊天导出常见编码回退链：BOM UTF-16 → utf-8-sig → gb18030 → latin-1 兜底。"""
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except Exception:
            pass
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("latin-1", errors="replace")


def detect_format(filename: str, text: str) -> str:
    """按文件名 + 内容特征探测格式：telegram_json / csv / whatsapp_txt / wechat_txt / generic_txt。

    JSON 嗅探刻意收窄到 ``{`` / ``[{``——WhatsApp 方括号导出（``[2024/3/5, …]``）
    同样以 ``[`` 开头，宽嗅探会把它整份误判成 JSON（首版实锤）。
    """
    name = str(filename or "").lower()
    head = (text or "").lstrip()[:2000]
    compact = re.sub(r"\s+", "", head)[:8]
    if name.endswith(".json") or compact.startswith("{") or compact.startswith("[{"):
        return "telegram_json"
    if name.endswith(".csv"):
        return "csv"
    lines = [ln for ln in (text or "").splitlines() if ln.strip()][:80]
    wa_hits = sum(1 for ln in lines if _WA_BRACKET_RE.match(ln) or _WA_DASH_RE.match(ln))
    wx_hits = sum(1 for ln in lines if _WX_HEADER_RE.match(ln) or _WX_HEADER2_RE.match(ln))
    if wa_hits >= 3 and wa_hits >= wx_hits:
        return "whatsapp_txt"
    if wx_hits >= 3:
        return "wechat_txt"
    return "generic_txt"


# ── 各格式解析器（统一产出 {ts, sender, text}）──────────────────────────────

# WhatsApp：`[2024/3/5, 14:23:45] Bob: hi` / `[05.03.24, 14:23] Bob: hi`
_WA_BRACKET_RE = re.compile(
    r"^\[(?P<date>[\d./-]{6,10}),?\s*(?P<time>[\d:]{4,8}(?:\s?[APap][Mm])?)\]\s*"
    r"(?P<sender>[^:]{1,64}?):\s?(?P<text>.*)$")
# WhatsApp：`3/5/24, 14:23 - Bob: hi`
_WA_DASH_RE = re.compile(
    r"^(?P<date>[\d./-]{6,10}),?\s+(?P<time>[\d:]{4,8}(?:\s?[APap][Mm])?)\s+-\s+"
    r"(?P<sender>[^:]{1,64}?):\s?(?P<text>.*)$")
# 微信第三方导出：`2024-03-05 14:23:45 昵称`（正文在后续行）；P3 扩容：
# 日期斜杠变体 `2024/3/5 14:23 昵称`、QQ 导出 `2024-03-05 14:23:45 昵称(10086)` /
# `昵称<mail@x.com>`（sender 尾缀账号标识在 _clean_sender 剥掉）。
_WX_HEADER_RE = re.compile(
    r"^(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<sender>\S.{0,63})$")
# 变体：`昵称 2024-03-05 14:23:45`
_WX_HEADER2_RE = re.compile(
    r"^(?P<sender>\S.{0,63}?)\s+(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)$")
# QQ/微信 sender 尾缀账号标识：`昵称(10086)` / `昵称<mail@x.com>` → 剥掉取净名
_SENDER_SUFFIX_RE = re.compile(r"[(（<][^)）>]{1,64}[)）>]\s*$")


def _clean_sender(raw: str) -> str:
    s = str(raw or "").strip()
    cleaned = _SENDER_SUFFIX_RE.sub("", s).strip()
    return cleaned or s
# 通用：`Bob: hi` / `Bob：hi`
_GENERIC_RE = re.compile(r"^(?P<sender>[^:：]{1,32}?)[:：]\s?(?P<text>\S.*)$")

_WA_SYSTEM_HINTS = ("end-to-end encrypted", "消息和通话都进行端到端加密", "创建了群组", "changed the subject")


def _norm_date(raw: str) -> str:
    """各式日期 → YYYY-MM-DD（尽力而为；歧义 M/D 按首段 ≤12 判月）。失败 → 空。"""
    s = str(raw or "").strip().replace(".", "/").replace("-", "/")
    parts = [p for p in s.split("/") if p]
    if len(parts) != 3:
        return ""
    try:
        a, b, c = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return ""
    if a > 1900:                      # 2024/3/5
        y, m, d = a, b, c
    elif c > 1900:                    # 3/5/2024 或 05/03/2024
        y = c
        m, d = (a, b) if a <= 12 else (b, a)
    else:                             # 3/5/24
        y = 2000 + c
        m, d = (a, b) if a <= 12 else (b, a)
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def _parse_whatsapp(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for raw in (text or "").splitlines():
        ln = raw.strip("\u200e\u200f ").rstrip()
        if not ln:
            continue
        m = _WA_BRACKET_RE.match(ln) or _WA_DASH_RE.match(ln)
        if m:
            body = m.group("text").strip()
            if any(h in body for h in _WA_SYSTEM_HINTS):
                continue
            out.append({
                "ts": _norm_date(m.group("date")),
                "sender": m.group("sender").strip(),
                "text": body,
            })
        elif out:
            out[-1]["text"] = (out[-1]["text"] + "\n" + ln).strip()
    return out


def _parse_wechat(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for raw in (text or "").splitlines():
        ln = raw.rstrip()
        if not ln.strip():
            continue
        m = _WX_HEADER_RE.match(ln.strip()) or _WX_HEADER2_RE.match(ln.strip())
        if m:
            out.append({
                "ts": _norm_date(m.group("date")),
                "sender": _clean_sender(m.group("sender")),
                "text": "",
            })
        elif out:
            out[-1]["text"] = (out[-1]["text"] + "\n" + ln.strip()).strip()
    return [m for m in out if m["text"]]


def _tg_text(node: Any) -> str:
    """Telegram 导出 text 字段：str 或 [str|{text}] 混合列表。"""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        parts: List[str] = []
        for seg in node:
            if isinstance(seg, str):
                parts.append(seg)
            elif isinstance(seg, dict):
                parts.append(str(seg.get("text") or ""))
        return "".join(parts)
    return ""


def _parse_telegram_json(text: str) -> List[Dict[str, str]]:
    try:
        obj = json.loads(text)
    except Exception:
        return []
    msgs = None
    if isinstance(obj, dict):
        msgs = obj.get("messages")
        if msgs is None and isinstance(obj.get("chats"), dict):
            chats = obj["chats"].get("list") or []
            msgs = chats[0].get("messages") if chats else None
    elif isinstance(obj, list):
        msgs = obj
    out: List[Dict[str, str]] = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        if m.get("type") not in (None, "message"):
            continue
        body = _tg_text(m.get("text")).strip()
        if not body:
            continue
        out.append({
            "ts": str(m.get("date") or "")[:10],
            "sender": str(m.get("from") or m.get("from_id") or "").strip(),
            "text": body,
        })
    return out


_CSV_TS_KEYS = ("ts", "time", "date", "datetime", "时间", "日期")
_CSV_SENDER_KEYS = ("sender", "from", "name", "author", "发送人", "昵称", "发送者")
_CSV_TEXT_KEYS = ("text", "content", "message", "msg", "内容", "消息", "正文")


def _parse_csv(text: str) -> List[Dict[str, str]]:
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except Exception:
        return []
    if not rows or len(rows) < 2:
        return []
    header = [str(h or "").strip().lower() for h in rows[0]]

    def _col(keys) -> int:
        for i, h in enumerate(header):
            if h in keys:
                return i
        return -1

    ci_ts, ci_sender, ci_text = _col(_CSV_TS_KEYS), _col(_CSV_SENDER_KEYS), _col(_CSV_TEXT_KEYS)
    if ci_sender < 0 or ci_text < 0:
        return []
    out: List[Dict[str, str]] = []
    for r in rows[1:]:
        if len(r) <= max(ci_sender, ci_text):
            continue
        body = str(r[ci_text] or "").strip()
        if not body:
            continue
        out.append({
            "ts": _norm_date(str(r[ci_ts]).split(" ")[0]) if ci_ts >= 0 and len(r) > ci_ts else "",
            "sender": str(r[ci_sender] or "").strip(),
            "text": body,
        })
    return out


def _parse_generic(text: str) -> List[Dict[str, str]]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    out: List[Dict[str, str]] = []
    hit = 0
    for ln in lines:
        m = _GENERIC_RE.match(ln)
        if m:
            hit += 1
            out.append({"ts": "", "sender": m.group("sender").strip(),
                        "text": m.group("text").strip()})
        elif out:
            out[-1]["text"] = (out[-1]["text"] + "\n" + ln).strip()
    # 命中率太低 = 不是对话格式，如实报不认识（宁可报错也不瞎拆）
    if not lines or hit < max(3, len(lines) * 0.3):
        return []
    return out


def parse_chat_export(filename: str, data: bytes) -> Dict[str, Any]:
    """入口：字节流 → ``{ok, format, messages, senders, msg_count, date_from, date_to}``。

    失败 → ``{ok: False, error: file_too_large | empty_file | unrecognized_format}``。
    超过 ``MAX_MESSAGES`` 截断保留最近的（近期记忆价值更高）并置 ``truncated=True``。
    """
    if data and len(data) > MAX_FILE_BYTES:
        return {"ok": False, "error": "file_too_large"}
    text = decode_bytes(data or b"")
    if not text.strip():
        return {"ok": False, "error": "empty_file"}
    fmt = detect_format(filename, text)
    parser = {
        "whatsapp_txt": _parse_whatsapp,
        "wechat_txt": _parse_wechat,
        "telegram_json": _parse_telegram_json,
        "csv": _parse_csv,
        "generic_txt": _parse_generic,
    }[fmt]
    messages = parser(text)
    if not messages and fmt != "generic_txt":
        messages = _parse_generic(text)   # 探测误判时给通用格式最后一次机会
        fmt = "generic_txt" if messages else fmt
    if not messages:
        return {"ok": False, "error": "unrecognized_format", "format": fmt}
    truncated = len(messages) > MAX_MESSAGES
    if truncated:
        messages = messages[-MAX_MESSAGES:]
    counts: Dict[str, int] = {}
    for m in messages:
        s = m.get("sender") or "?"
        counts[s] = counts.get(s, 0) + 1
    senders = [{"name": k, "count": v}
               for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    dates = sorted(d for d in (m.get("ts") or "" for m in messages) if d)
    return {
        "ok": True, "format": fmt, "messages": messages,
        "msg_count": len(messages), "senders": senders,
        "date_from": dates[0] if dates else "",
        "date_to": dates[-1] if dates else "",
        "truncated": truncated,
    }


def guess_customer_sender(senders: List[Dict[str, Any]], *, self_names: Optional[List[str]] = None) -> str:
    """猜哪个 sender 是客户：排除我方惯用名/显式自名后取消息最多者。"""
    exclude = {str(n).strip().lower() for n in (self_names or []) if str(n).strip()}
    exclude |= _SELF_HINTS
    for s in senders or []:
        name = str(s.get("name") or "").strip()
        if name and name.lower() not in exclude:
            return name
    return str((senders or [{}])[0].get("name") or "") if senders else ""


# ── LLM 摘要 + 接地 ──────────────────────────────────────────────────────────

def transcript_sample(
    messages: List[Dict[str, str]], customer_sender: str,
    *, max_chars: int = _SAMPLE_MAX_CHARS,
) -> str:
    """打样：头尾各 N 条（近期优先），角色归一为「客户/我方」，硬预算截断。"""
    msgs = messages or []
    if len(msgs) > (_SAMPLE_HEAD + _SAMPLE_TAIL):
        msgs = msgs[:_SAMPLE_HEAD] + msgs[-_SAMPLE_TAIL:]
    cust = str(customer_sender or "").strip()
    lines: List[str] = []
    for m in msgs:
        role = "客户" if (m.get("sender") or "") == cust else "我方"
        body = str(m.get("text") or "").replace("\n", " ")[:200]
        if body:
            lines.append(f"{role}: {body}")
    out = "\n".join(lines)
    return out[-max_chars:] if len(out) > max_chars else out


def build_import_summary_prompt(
    sample: str, *, source_label: str = "", msg_count: int = 0, date_range: str = "",
) -> str:
    src = source_label or "其他平台"
    meta = f"共 {msg_count} 条" + (f"，{date_range}" if date_range else "")
    return (
        f"你是客服团队的资料整理助手。下面是我方与一位客户在{src}的历史聊天记录节选（{meta}）。\n"
        "请只输出严格 JSON（无 markdown 代码块、无解释）：\n"
        '{"topics": ["话题域，2-6字/个"], "note": "一句话概括客户背景与关系（≤80字，事实向）",'
        ' "facts": [{"text": "关于客户本人的稳定事实（≤60字，第三人称）", "quote": "客户原话出处片段（≤60字）"}]}\n'
        f"要求：topics 最多 {MAX_TOPICS} 个；facts 最多 {MAX_FACTS} 条，"
        "只收客户亲口说过、对以后聊天有用的稳定事实（家庭/工作/兴趣/偏好/重要计划）；"
        "绝不把我方说的话、猜测、临时状态（如「今天很忙」）当事实；quote 必须是客户消息原文片段。\n"
        "--- 记录开始 ---\n" + sample + "\n--- 记录结束 ---"
    )


def parse_summary_json(raw: str) -> Optional[Dict[str, Any]]:
    """宽松解析 LLM JSON（剥代码块围栏 / 提取首个 {...}）。失败 → None。"""
    s = str(raw or "").strip()
    if not s:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.M).strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None
        s = s[i:j + 1]
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    topics = [str(t).strip()[:30] for t in (obj.get("topics") or [])
              if str(t).strip()][:MAX_TOPICS]
    note = str(obj.get("note") or "").strip()[:120]
    facts: List[Dict[str, str]] = []
    for f in (obj.get("facts") or [])[:MAX_FACTS]:
        if isinstance(f, dict):
            txt = str(f.get("text") or "").strip()[:200]
            quote = str(f.get("quote") or "").strip()[:120]
        else:
            txt, quote = str(f or "").strip()[:200], ""
        if len(txt) >= 2:
            facts.append({"text": txt, "quote": quote})
    return {"topics": topics, "note": note, "facts": facts}


def _norm_for_quote(s: str) -> str:
    return re.sub(r"[\s，。！？,.!?~～\u200b]+", "", str(s or "").lower())


def ground_facts(
    facts: List[Dict[str, str]], messages: List[Dict[str, str]], customer_sender: str,
) -> List[Dict[str, Any]]:
    """候选事实接地：quote 原文核对 或 memory_grounding 词汇重叠，二者其一即 grounded。

    不接地的**不丢**（导入场景由人工最终把关），标 ``grounded=False`` 供 UI
    默认不勾选 + 「存疑」标注——与在线抽取的 fail-closed 不同，这里人是终审。
    """
    cust = str(customer_sender or "").strip()
    cust_text = "\n".join(
        str(m.get("text") or "") for m in (messages or [])
        if (m.get("sender") or "") == cust)
    cust_norm = _norm_for_quote(cust_text)
    try:
        from src.ai.memory_grounding import fact_grounded_in_user_msg
    except Exception:  # pragma: no cover - 防御
        fact_grounded_in_user_msg = None
    out: List[Dict[str, Any]] = []
    for f in facts or []:
        txt = str(f.get("text") or "").strip()
        quote = str(f.get("quote") or "").strip()
        grounded = False
        if quote and _norm_for_quote(quote) and _norm_for_quote(quote) in cust_norm:
            grounded = True
        elif fact_grounded_in_user_msg is not None:
            try:
                grounded = bool(fact_grounded_in_user_msg(txt, cust_text))
            except Exception:
                grounded = False
        out.append({"text": txt, "quote": quote, "grounded": grounded})
    return out


async def summarize_import(
    ai_client: Any,
    messages: List[Dict[str, str]],
    customer_sender: str,
    *,
    source_label: str = "",
    date_range: str = "",
) -> Dict[str, Any]:
    """LLM 摘要 + 接地标记；LLM 缺席/失败 → ``{error: llm_unavailable|llm_bad_json}``
    软降级（解析结果仍可用，运营手填话题/事实）。"""
    if ai_client is None or not hasattr(ai_client, "chat"):
        return {"topics": [], "note": "", "facts": [], "error": "llm_unavailable"}
    sample = transcript_sample(messages, customer_sender)
    prompt = build_import_summary_prompt(
        sample, source_label=source_label,
        msg_count=len(messages or []), date_range=date_range)
    raw = ""
    try:
        raw = await ai_client.chat(
            [{"role": "user", "content": prompt}],
            strategy_overrides={"_internal_purpose": "origin_import_summary"},
        )
    except TypeError:
        try:
            raw = await ai_client.chat([{"role": "user", "content": prompt}])
        except Exception:
            return {"topics": [], "note": "", "facts": [], "error": "llm_unavailable"}
    except Exception:
        return {"topics": [], "note": "", "facts": [], "error": "llm_unavailable"}
    parsed = parse_summary_json(raw)
    if parsed is None:
        return {"topics": [], "note": "", "facts": [], "error": "llm_bad_json"}
    parsed["facts"] = ground_facts(parsed.get("facts") or [], messages, customer_sender)
    parsed["error"] = ""
    return parsed


__all__ = [
    "MAX_FILE_BYTES", "MAX_MESSAGES", "MAX_FACTS", "MAX_TOPICS",
    "decode_bytes", "detect_format", "parse_chat_export",
    "guess_customer_sender", "transcript_sample",
    "build_import_summary_prompt", "parse_summary_json",
    "ground_facts", "summarize_import",
]
