"""KB 注入前守门（2026-07-22 质量治理 Sprint A1）。

背景（真机测试实锤，2026-07-21 夜）：加权 BM25 的**原始分**与 0.30 的"强命中"
阈值口径完全不匹配（实测原始分 14~292），任何一个常用字重叠都被当强命中，
导致大量不相关 KB 注入提示词：

- 「怎麼不恢復我了呢」→ 命中「怎么付款：USDT 结算」(14.8)——仅"怎么"两字重叠，
  陪聊人设（护士）随后向客户推销付款客服号，被客户当场抓包"两个没关系的东西怎么混一起"；
- 「[图片内容] 怪味胡豆…」→ 命中「客户端安装指引」(181)——识图描述文本不是用户提问；
- 正确命中「帮我介绍一下智聊的价格」→「七产品线总览」(292) 有"智聊/价格/介绍"多实词重叠。

单一分数阈值无法区分 181(误) 与 292(对) → 本模块用**词汇重叠质量**守门：
查询与命中条目需有足够的实词重叠（非停用词 ≥2 个，或单个强词 ≥4 字）才放行注入。

全部纯函数、零 IO，便于单测与复用（skill_manager 注入点 / 未来 RAG 链路同口径）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

# 中文高频虚词/口语词 + 通用客套（单独出现不构成"在问该条目的事"的证据）。
# 保守收录：宁少勿多——漏拦一个停用词只是多一次向量验证，误收一个实词会杀掉正确命中。
_STOPWORDS = frozenset({
    "怎么", "怎麼", "什么", "什麼", "可以", "不可", "为什么", "為什麼",
    "知道", "现在", "而家", "今天", "明天", "一下", "一个", "一個",
    "你们", "你哋", "我们", "我哋", "他们", "自己", "这个", "這個",
    "那个", "那個", "这样", "那样", "咁样", "点解", "系咪", "唔系",
    "没有", "冇有", "不是", "就是", "还是", "還是", "但是", "不过",
    "如果", "因为", "因為", "所以", "然后", "然後", "已经", "已經",
    "真的", "真系", "好像", "觉得", "覺得", "感觉", "感覺", "需要",
    "帮我", "幫我", "给我", "畀我", "发给", "發給", "看下", "睇下",
    "谢谢", "多谢", "你好", "请问", "請問",
})

# 与 kb_store._tokenize 同风格的轻量分词：中文二元组 + 连续英数词。
# 不 import kb_store（避免循环依赖），口径一致性由单测锚定。
_WORD_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def _tokenize_query(text: str) -> List[str]:
    """查询侧分词：英数整词 + 中文相邻二元组（与 BM25 索引口径对齐）。"""
    t = str(text or "")
    raw = _WORD_RE.findall(t)
    tokens: List[str] = []
    han_buf: List[str] = []

    def _flush_han():
        if len(han_buf) == 1:
            tokens.append(han_buf[0])
        else:
            for i in range(len(han_buf) - 1):
                tokens.append(han_buf[i] + han_buf[i + 1])
        han_buf.clear()

    for w in raw:
        if re.fullmatch(r"[\u4e00-\u9fff]", w):
            han_buf.append(w)
        else:
            _flush_han()
            if len(w) >= 2:
                tokens.append(w.lower())
    _flush_han()
    return tokens


def content_tokens(text: str) -> List[str]:
    """提取"实词"token：剔除停用词与单字。"""
    return [
        t for t in _tokenize_query(text)
        if len(t) >= 2 and t not in _STOPWORDS
    ]


def entry_blob(entry: Dict[str, Any]) -> str:
    """KB 条目的匹配面文本（与 BM25 加权字段一致：triggers/title/scenario）。"""
    if not isinstance(entry, dict):
        return ""
    parts = []
    for k in ("triggers", "title", "scenario", "category"):
        v = entry.get(k)
        if isinstance(v, (list, tuple)):
            parts.append(" ".join(str(x) for x in v))
        elif v:
            parts.append(str(v))
    return " ".join(parts)


def lexical_overlap_ok(
    query: str,
    entry: Dict[str, Any],
    *,
    min_content_overlap: int = 2,
    strong_token_len: int = 4,
) -> Tuple[bool, str]:
    """查询与 KB 条目的实词重叠是否足以支撑注入。

    放行条件（任一）：
      1. 实词重叠 ≥ ``min_content_overlap`` 个；
      2. 存在单个"强词"重叠（长度 ≥ ``strong_token_len``，如英文品牌词/长专名）。

    返回 ``(ok, reason)``；reason 供日志观测（overlap=N / strong:<token> / no_overlap）。
    """
    q_tokens = set(content_tokens(query))
    if not q_tokens:
        # 查询全是虚词（如"怎么办呢"）→ 无证据支撑任何条目
        return False, "query_no_content_tokens"
    blob = entry_blob(entry)
    blob_low = blob.lower()
    overlap = [t for t in q_tokens if t in blob_low]
    for t in overlap:
        if len(t) >= strong_token_len:
            return True, f"strong:{t}"
    if len(overlap) >= min_content_overlap:
        return True, f"overlap={len(overlap)}"
    return False, (f"weak_overlap={len(overlap)}" if overlap else "no_overlap")


def is_media_desc_text(text: str) -> bool:
    """入站文本是否为媒体识别描述（识图/识视频产物，非用户提问）。

    这类文本进 KB 检索只会撞出环境词噪声（实测「怪味胡豆图」命中「客户端安装指引」），
    应整体跳过 KB。语音转写**不算**——那是用户说的话。
    """
    t = str(text or "").lstrip()
    return t.startswith("[图片内容]") or t.startswith("[视频内容]")


def persona_kb_suppressed(
    persona: Any,
    tier: str,
) -> bool:
    """显式绑定的陪聊人设会话是否应抑制 KB 注入（单一身份源的知识面延伸）。

    ``chat_binding`` / ``account_profile`` 两档 tier 表示会话被显式绑定了人设
    （陪聊/角色扮演场景）——业务 KB（产品卖点/付款通道/安装指引）与该身份天然冲突
    （护士人设推销 USDT 事故）。默认抑制；人设可显式声明 ``kb_access: true`` 恢复
    （如"客服人设"确需业务知识）。未绑定（domain/default tier，如网页顾问顾嘉）不受影响。
    """
    if str(tier or "") not in ("chat_binding", "account_profile"):
        return False
    if isinstance(persona, dict) and bool(persona.get("kb_access", False)):
        return False
    return True


__all__ = [
    "content_tokens",
    "entry_blob",
    "is_media_desc_text",
    "lexical_overlap_ok",
    "persona_kb_suppressed",
]
