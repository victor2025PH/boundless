# -*- coding: utf-8 -*-
"""
conversation.py — Phase 9 对话式数字人 编排骨架（纯代码 / mock 可跑 / 零下载）

把「听(STT) → 想(LLM) → 说(TTS)」抽象成可插拔流式管线：
  - STT / LLM 后端以适配器接口提供，默认 mock，真实后端(faster-whisper / OpenAI 兼容
    LLM 如 Ollama·vLLM·llama.cpp)只需实现接口并 register，无需改编排与端点。
  - 编排器 ConversationOrchestrator 以**异步事件流**驱动：STT→LLM 流式 token→句级聚合
    →逐句 TTS（TTS 由 avatar_hub 注入回调，复用现有引擎/水印）。
  - 句级聚合 = 首句一就绪就开口，边想边说，压低端到端首音延迟(TTFA)。
  - 支持 barge-in（用户插话）：传入 cancel_event，编排在 token/句边界处中断当前发言。
  - 全程记录延迟预算(stt/llm_first_token/first_sentence/first_audio/total)。

本模块**不依赖 avatar_hub**，可独立导入与单测。TTS 经注入回调解耦。
"""
from __future__ import annotations

import os
import re
import json
import time
import asyncio
import sqlite3
import threading
from abc import ABC, abstractmethod
from collections import deque, OrderedDict
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional, Awaitable

# 句末标点（句级聚合的 flush 触发点）
_SENT_END = "。！？!?…\n"
_SENT_SOFT = "，,；;：、"         # 软切点：首句过长时用于尽快出第一块（含顿号）
_MAX_SENT_CHARS = 60            # 句子超过此长度强制切，避免 LLM 长输出憋住 TTS
# 首句更短（压低首音 TTFA）：首块在更小字数处软切，且无软标点时也封顶切
# 2 字即可软切：让"嗯，""好的，"等极短缓存开场词能被单独切出→0ms 命中缓存
_FIRST_MIN_CHARS = int(os.environ.get("CONV_FIRST_MIN_CHARS", "2"))   # 首块软切最小字数
# Fish 合成 ~200-300ms/字：首块越短首音越快。10 字首块 ≈ 2.5s→借垫话掩盖；
# 配合软切(逗号处更早切)，多数首块 3~8 字，真实首音可压到 ~1.5s。
_FIRST_MAX_CHARS = int(os.environ.get("CONV_FIRST_MAX_CHARS", "10"))  # 首块无软标点的强制上限
# 引导 LLM 先回极短句（与首句软切互补，二者取其先到）
_SHORT_OPENER = os.environ.get("CONV_SHORT_OPENER", "1") == "1"
# 本轮合成无声（引擎全不可达/被快跳，tts_fn 返回空且未抛异常）时，发一次中性 tts_unavailable
# 候选事件——是否真正提示由 avatar_hub 转发层据健康/角色门控（在线则视为内容跳过、静默）。默认开。
_TTS_UNAVAIL_EVENT = os.environ.get("CONV_TTS_UNAVAIL_EVENT", "1") != "0"
# 让回复以一个简短口头语 + 逗号 起头：该口头语已被预合成缓存 → 首块 0ms 命中，
# 真实首音≈LLM首token(~0.3s)，口头语本身也像真人开口的自然停顿。
# 关键：由【服务端每轮随机指定】具体用哪个词（LLM 自由选会固定用某一个→单调），
# 这样既保证 100% 命中又自然轮换。这些词须与 avatar_hub._OPENER_PHRASES 一致(含全角逗号)。
import random as _random
_OPENER_CHOICES = [s for s in os.environ.get(
    "CONV_OPENER_CHOICES",
    "嗯，|这个嘛，|让我想想，|我觉得，|其实，|说起来，|怎么说呢，|嗯…").split("|") if s.strip()]
_last_opener_choice = {"v": ""}

def _short_opener_hint() -> str:
    pool = [o for o in _OPENER_CHOICES if o != _last_opener_choice["v"]] or _OPENER_CHOICES
    op = _random.choice(pool)
    _last_opener_choice["v"] = op
    return (f"\n（回答请务必以「{op}」开头（一字不差，包括标点），紧接正文；"
            f"正文简洁自然。）")


# ── 可插拔后端接口 ───────────────────────────────────────────────────
class STTBackend(ABC):
    name: str = "stt"

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, *, language: str = "") -> str:
        """音频字节 → 文本。"""
        ...


class LLMBackend(ABC):
    name: str = "llm"

    @abstractmethod
    async def stream(self, messages: list, **opts) -> AsyncIterator[str]:
        """messages=[{role,content}...] → 异步逐 token(增量文本) 产出。"""
        ...


# ── Mock 后端（零依赖，确定性，供离线开发与单测）─────────────────────
class MockSTT(STTBackend):
    name = "mock_stt"

    def __init__(self, canned: str = ""):
        self.canned = canned

    async def transcribe(self, audio_bytes: bytes, *, language: str = "") -> str:
        if self.canned:
            return self.canned
        # 无预设时给个确定性占位（按字节长度），便于测试断言
        return f"[mock语音{len(audio_bytes)}字节]"


class MockLLM(LLMBackend):
    name = "mock_llm"

    def __init__(self, chunk: int = 3, delay_ms: int = 0):
        self.chunk = max(1, chunk)
        self.delay_ms = delay_ms

    def _reply_for(self, messages: list) -> str:
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content", ""); break
        return f"我收到了你的消息：{user}。这是一条用于联调的占位回复。"

    async def stream(self, messages: list, **opts) -> AsyncIterator[str]:
        reply = self._reply_for(messages)
        for i in range(0, len(reply), self.chunk):
            if self.delay_ms:
                await asyncio.sleep(self.delay_ms / 1000.0)
            yield reply[i:i + self.chunk]


# ── 真实 STT 后端：调用 Whisper 微服务（stt_server.py / 7854）──────────────
class HTTPSTT(STTBackend):
    """把音频字节 POST 给 STT 微服务转写。手机麦克风 → 文本走这里。
    与 Whisper(7854) / Nemotron(7857) 同一 /transcribe 契约，故同一适配器按 base_url+name 复用。"""
    name = "whisper_stt"

    def __init__(self, base_url: str = "http://127.0.0.1:7854", timeout: float = 60.0,
                 name: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if name:
            self.name = name      # 实例级覆盖（如 nemotron_stt），不影响其它实例

    async def transcribe(self, audio_bytes: bytes, *, language: str = "") -> str:
        import httpx
        files = {"audio": ("audio.wav", audio_bytes, "application/octet-stream")}
        # 空语种 → "auto" 让 Whisper 自行识别语种（多语种语音输入的关键）。绝不回退到
        # 固定 "zh"：那会把英/日/韩语音强行按中文转写→乱码。显式语种则原样透传。
        data = {"language": language or "auto"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}/transcribe", files=files, data=data)
            r.raise_for_status()
            return (r.json().get("text") or "").strip()


# ── 真实后端示例（不默认启用；按需实现/注册）──────────────────────────
class OpenAICompatLLM(LLMBackend):
    """对接任意 OpenAI 兼容 /v1/chat/completions 流式端点（Ollama / vLLM / llama.cpp）。
    仅作可插拔示例：构造时给 base_url+model，register 后即可用，无需改编排。"""
    name = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 60.0,
                 keep_alive: str = "", kind: str = "local", label: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.keep_alive = keep_alive  # Ollama 常驻(如 "30m"/-1)；非空才透传
        self.kind = kind              # local(本机) / lan(局域网) / cloud(云端) —— 供 UI 分组/切换
        self.label = label or model   # UI 友好显示名

    async def stream(self, messages: list, **opts) -> AsyncIterator[str]:
        import json as _json
        import httpx
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "messages": messages, "stream": True,
                   "temperature": opts.get("temperature", 0.7)}
        # 可选采样参数（仅在请求显式给出时透传，避免覆盖后端默认）
        if opts.get("max_tokens"):
            payload["max_tokens"] = int(opts["max_tokens"])
        if opts.get("top_p") is not None:
            payload["top_p"] = float(opts["top_p"])
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive  # Ollama 扩展字段，其它后端忽略
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            async with cli.stream("POST", f"{self.base_url}/v1/chat/completions",
                                  headers=headers, json=payload) as resp:
                # 关键：非 2xx（401 密钥失效 / 429 限流 / 5xx）必须抛出，否则错误体不是
                # SSE「data:」行 → 被逐行跳过 → 本生成器「零 token 正常结束」，FailoverLLM
                # 误判为成功而不切兜底，最终静默吐空回复。抛出后由 Failover 切换或上抛报错。
                if resp.status_code >= 400:
                    try:
                        body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    except Exception:
                        body = ""
                    raise RuntimeError(f"{self.name} LLM HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = _json.loads(data)["choices"][0]["delta"].get("content", "")
                    except Exception:
                        delta = ""
                    if delta:
                        yield delta


class FailoverLLM(LLMBackend):
    """容灾包装：主引擎(通常云端)优先；首 token 超时或连接异常时，
    自动切到兜底引擎链(通常本地)，保证对话不断线。
    已产出 token 后再失败则不重试（避免重复内容），仅结束本轮。
    last_used 记录本轮实际服务的后端名，供观测。"""

    def __init__(self, primary: "LLMBackend", fallbacks: list,
                 name: str = "", first_token_timeout: float = 12.0,
                 fallback_timeout: float = 45.0):
        self.primary = primary
        self.fallbacks = [b for b in fallbacks if b is not None and b is not primary]
        self.first_token_timeout = first_token_timeout   # 云端首响快速失败阈值
        self.fallback_timeout = fallback_timeout         # 本地兜底给冷启动留足时间
        self.name = name or getattr(primary, "name", "failover")
        # 对外暴露主引擎的元信息，保证 UI/预热/introspection 不变
        self.kind = getattr(primary, "kind", "cloud")
        self.label = getattr(primary, "label", self.name)
        self.model = getattr(primary, "model", "")
        self.base_url = getattr(primary, "base_url", "")
        self.api_key = getattr(primary, "api_key", "")
        self.keep_alive = getattr(primary, "keep_alive", "")
        self.last_used = self.primary.name

    async def stream(self, messages: list, **opts) -> AsyncIterator[str]:
        chain = [self.primary] + self.fallbacks
        last_err = None
        n = len(chain)
        for idx, be in enumerate(chain):
            produced = False
            self.last_used = getattr(be, "name", f"backend{idx}")
            is_last = (idx == n - 1)
            # 云端(主)快速失败→切本地；本地兜底给冷启动留时间；最后一个不限时(必须等)
            if idx == 0:
                _to = self.first_token_timeout
            elif is_last:
                _to = None
            else:
                _to = self.fallback_timeout
            try:
                agen = be.stream(messages, **opts).__aiter__()
                while True:
                    try:
                        if produced or _to is None:
                            tok = await agen.__anext__()
                        else:
                            # 仅对首 token 设超时；超时即触发切换到下一引擎
                            tok = await asyncio.wait_for(
                                agen.__anext__(), timeout=_to)
                    except StopAsyncIteration:
                        break
                    produced = True
                    yield tok
                # 兜底防线：主引擎「零 token 正常结束」（如 200 空流/解析全失败）也视为软失败，
                # 仍切下一引擎，绝不静默吐空（已产出 token 则正常收尾）。
                if not produced and not is_last:
                    print(f"[Failover] {self.last_used} 零 token 结束，切换→ {chain[idx + 1].name}")
                    continue
                if idx > 0:
                    print(f"[Failover] 已由兜底引擎 {self.last_used} 完成本轮回复")
                return  # 正常完成
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                if produced:
                    # 已流出部分内容，不再重试（避免与前文重复）
                    print(f"[Failover] {self.last_used} 中途中断，已结束本轮: {e!r}")
                    return
                nxt = chain[idx + 1].name if idx + 1 < len(chain) else "（无）"
                print(f"[Failover] {self.last_used} 首响失败/超时({e!r})，切换→ {nxt}")
                continue
        if last_err:
            raise last_err


# ── 安全双闸门（9-5：LLM 前查输入、TTS 前查输出）──────────────────────
@dataclass
class GuardResult:
    ok: bool                # 输入: False=拦截整轮；输出: False=该句不发声
    reason: str = ""
    text: str = ""          # 可能脱敏后的文本（输出闸门用）


class SafetyGuard(ABC):
    name: str = "guard"

    @abstractmethod
    def inspect(self, text: str, *, stage: str) -> GuardResult:
        """stage: 'input'(LLM前) / 'output'(TTS前)。"""
        ...


class KeywordGuard(SafetyGuard):
    """规则化关键词闸门：输入命中→拦截整轮；输出命中→脱敏(默认)或拒发。
    默认 blocklist 为空 → 零行为变化；运营方经 /api/converse/guard 配置。"""
    name = "keyword_guard"

    def __init__(self, blocklist=None, redact: bool = True):
        self.blocklist = [w.lower() for w in (blocklist or []) if w]
        self.redact = redact

    def _hits(self, text: str) -> list:
        low = text.lower()
        return [w for w in self.blocklist if w in low]

    def inspect(self, text: str, *, stage: str) -> GuardResult:
        hits = self._hits(text)
        if not hits:
            return GuardResult(True, "", text)
        if stage == "input":
            return GuardResult(False, f"输入命中敏感词: {hits[0]}", text)
        # output
        if not self.redact:
            return GuardResult(False, f"输出命中敏感词: {hits[0]}", text)
        out = text
        for w in hits:
            out = re.sub(re.escape(w), "*" * len(w), out, flags=re.IGNORECASE)
        return GuardResult(True, f"输出已脱敏: {hits[0]}", out)


# ── 知识库 RAG（9-4：纯词法检索 BM25，零依赖/零下载，CJK 友好）────────
def _tokenize(text: str) -> list:
    """轻量分词：latin 词 + CJK 单字 + CJK 二元组（无需 jieba/嵌入模型）。"""
    low = text.lower()
    toks = re.findall(r"[a-z0-9]+", low)
    toks += re.findall(r"[\u4e00-\u9fff]", low)            # CJK 单字
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", low):     # CJK 二元组（提升短语匹配）
        toks += [run[i:i + 2] for i in range(len(run) - 1)]
    return toks


@dataclass
class Doc:
    id: str
    text: str
    meta: dict = field(default_factory=dict)
    _toks: list = field(default_factory=list, repr=False)
    emb: Optional[list] = field(default=None, repr=False)   # 语义向量（启用嵌入后端时填充）


class KnowledgeBase:
    """文档库：内存维护文档 + 缓存分词（检索零重复分词）。
    可选 SQLite 持久化（db_path 非空时）：增/清同步落盘，重启自动加载。
    默认 db_path="" → 纯内存，行为与既有一致（零变化）。"""
    def __init__(self, db_path: str = ""):
        self.docs: list = []
        self.db_path = db_path
        self._counter = 0
        self._lock = threading.Lock()
        self._conn = None
        self.embedder = None         # 9-5: 语义嵌入后端（None=纯 BM25，行为与既有一致）
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kb_docs(id TEXT PRIMARY KEY, text TEXT, meta TEXT)")
            # 9-5: 平滑迁移——老库补 emb 列（已存在则忽略）
            try:
                self._conn.execute("ALTER TABLE kb_docs ADD COLUMN emb TEXT")
            except Exception:
                pass
            self._conn.commit()
            self._load()

    def _load(self):
        try:
            rows = self._conn.execute("SELECT id, text, meta, emb FROM kb_docs")
            has_emb = True
        except Exception:
            rows = self._conn.execute("SELECT id, text, meta FROM kb_docs")
            has_emb = False
        for row in rows:
            did, text, meta = row[0], row[1], row[2]
            emb = None
            if has_emb and len(row) > 3 and row[3]:
                try:
                    emb = json.loads(row[3])
                except Exception:
                    emb = None
            self.docs.append(Doc(did, text, json.loads(meta or "{}"), _tokenize(text), emb))
            if did.startswith("d") and did[1:].isdigit():
                self._counter = max(self._counter, int(did[1:]))

    def set_embedder(self, embedder):
        """挂载/更换语义嵌入后端。挂载后新增文档自动嵌入；存量可调 ensure_embeddings() 回填。"""
        self.embedder = embedder

    def _persist(self, did, text, meta, emb):
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO kb_docs(id, text, meta, emb) VALUES(?,?,?,?)",
            (did, text, json.dumps(meta or {}, ensure_ascii=False),
             json.dumps(emb) if emb is not None else None))
        self._conn.commit()

    def add(self, text: str, meta: dict = None, id: str = "", emb: list = None) -> str:
        # 嵌入后端就绪且未传入向量 → 即时嵌入（失败则降级存无向量文档，语义检索自动跳过）
        if emb is None and self.embedder is not None:
            try:
                emb = self.embedder.embed([text])[0]
            except Exception:
                emb = None
        with self._lock:
            self._counter += 1
            did = id or f"d{self._counter}"
            self.docs.append(Doc(did, text, meta or {}, _tokenize(text), emb))
            self._persist(did, text, meta, emb)
            return did

    def add_many(self, texts: list) -> list:
        texts = [t for t in texts if t and t.strip()]
        # 有嵌入后端 → 批量一次性嵌入（远快于逐条 HTTP）；失败回退逐条（add 内再降级）
        embs = None
        if texts and self.embedder is not None:
            try:
                embs = self.embedder.embed(texts)
            except Exception:
                embs = None
        out = []
        for i, t in enumerate(texts):
            e = embs[i] if (embs is not None and i < len(embs)) else None
            out.append(self.add(t, emb=e))
        return out

    def ensure_embeddings(self, batch: int = 64) -> int:
        """为缺向量的存量文档批量回填嵌入（挂载后端后调用）。返回回填数。"""
        if self.embedder is None:
            return 0
        todo = [d for d in self.docs if not d.emb]
        n = 0
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            try:
                vecs = self.embedder.embed([d.text for d in chunk])
            except Exception:
                break
            for d, v in zip(chunk, vecs):
                d.emb = v
                self._persist(d.id, d.text, d.meta, v)
                n += 1
        return n

    def clear(self):
        with self._lock:
            self.docs.clear()
            if self._conn is not None:
                self._conn.execute("DELETE FROM kb_docs")
                self._conn.commit()

    def count(self) -> int:
        return len(self.docs)


class Retriever(ABC):
    name: str = "retriever"

    @abstractmethod
    def search(self, query: str, kb: KnowledgeBase, top_k: int = 3) -> list:
        """返回 [(Doc, score)] 降序。"""
        ...


class LexicalRetriever(Retriever):
    """BM25 词法检索：无需嵌入模型，离线可用。未来可换嵌入/向量检索（实现 Retriever 即可）。"""
    name = "bm25"

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

    def search(self, query: str, kb: KnowledgeBase, top_k: int = 3) -> list:
        import math
        from collections import Counter
        docs = kb.docs
        if not docs:
            return []
        N = len(docs)
        avgdl = sum(len(d._toks) for d in docs) / N or 1.0
        df = Counter()
        for d in docs:
            for w in set(d._toks):
                df[w] += 1
        q = _tokenize(query)
        out = []
        for d in docs:
            tf = Counter(d._toks)
            dl = len(d._toks) or 1
            s = 0.0
            for w in q:
                if w not in tf:
                    continue
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * (tf[w] * (self.k1 + 1)) / (
                    tf[w] + self.k1 * (1 - self.b + self.b * dl / avgdl))
            if s > 0:
                out.append((d, s))
        out.sort(key=lambda x: -x[1])
        return out[:top_k]


# ── 语义嵌入检索（9-5：向量召回，BM25+语义混合 RRF；嵌入后端缺失则自动降级 BM25）────
#   语义置信度阈值（cosine / 归一化BM25 同处 [0,1] 标度）；低于此判低置信→提示防编造。
_SEM_MIN = float(os.environ.get("CONV_RAG_SEM_MIN", "0.30"))


class EmbeddingBackend(ABC):
    name: str = "embedding"

    @abstractmethod
    def embed(self, texts: list) -> list:
        """[str] → [[float]]（顺序一一对应）。失败应抛异常，调用方据此降级 BM25。"""
        ...


_EMBED_CACHE_MAX = int(os.environ.get("CONV_EMBED_CACHE", "2048"))


class OpenAICompatEmbedding(EmbeddingBackend):
    """对接 OpenAI 兼容 /v1/embeddings（Ollama 近版 / vLLM / LM Studio / OpenAI）。
    Ollama 用法：`ollama pull bge-m3`（或 nomic-embed-text），base_url=http://127.0.0.1:11434。
    同步实现（KB.add 在同步路径调用）；失败抛异常 → 知识库/检索自动降级 BM25。
    9-7：内置线程安全 LRU 查询缓存——同一文本（高频是重复 query）免重复 HTTP，压低检索延迟。"""
    name = "openai_embed"

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = 30.0, name: str = "", cache_size: int = -1):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        if name:
            self.name = name
        self._cache_max = _EMBED_CACHE_MAX if cache_size < 0 else cache_size
        self._cache: "OrderedDict[str, list]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0

    def _cache_get(self, key: str):
        if self._cache_max <= 0:
            return None
        with self._cache_lock:
            v = self._cache.get(key)
            if v is not None:
                self._cache.move_to_end(key)   # LRU 触达
            return v

    def _cache_put(self, key: str, vec: list):
        if self._cache_max <= 0:
            return
        with self._cache_lock:
            self._cache[key] = vec
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)

    def _ck(self, text: str) -> str:
        return f"{self.model}\x1f{text}"

    def _http_embed(self, texts: list) -> list:
        import httpx
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.Client(timeout=self.timeout) as cli:
            r = cli.post(f"{self.base_url}/v1/embeddings", headers=headers,
                         json={"model": self.model, "input": list(texts)})
            r.raise_for_status()
            data = r.json().get("data", [])
        data = sorted(data, key=lambda d: d.get("index", 0))   # 按 index 严格对齐输入顺序
        return [d["embedding"] for d in data]

    def embed(self, texts: list) -> list:
        if not texts:
            return []
        texts = list(texts)
        out: list = [None] * len(texts)
        miss_idx: list = []
        miss_txt: list = []
        for i, t in enumerate(texts):
            hit = self._cache_get(self._ck(t))
            if hit is not None:
                out[i] = hit
                self.cache_hits += 1
            else:
                miss_idx.append(i)
                miss_txt.append(t)
                self.cache_misses += 1
        if miss_txt:
            vecs = self._http_embed(miss_txt)   # 仅请求未命中部分
            for j, v in zip(miss_idx, vecs):
                out[j] = v
                self._cache_put(self._ck(texts[j]), v)
        return out


def _cosine(a: list, b: list) -> float:
    s = sa = sb = 0.0
    for x, y in zip(a, b):
        s += x * y; sa += x * x; sb += y * y
    if sa <= 0.0 or sb <= 0.0:
        return 0.0
    return s / ((sa ** 0.5) * (sb ** 0.5))


class SemanticRetriever(Retriever):
    """纯语义向量检索（cosine）。返回分数为 [0,1] cosine 相似度。"""
    name = "semantic"
    min_score = _SEM_MIN

    def __init__(self, embedder: EmbeddingBackend):
        self.embedder = embedder

    def search(self, query: str, kb: KnowledgeBase, top_k: int = 3) -> list:
        docs = [d for d in kb.docs if d.emb]
        if not docs:
            return []
        try:
            qe = self.embedder.embed([query])[0]
        except Exception:
            return []
        out = [(d, _cosine(qe, d.emb)) for d in docs]
        out.sort(key=lambda x: -x[1])
        return out[:top_k]


class Reranker(ABC):
    name: str = "reranker"

    @abstractmethod
    def rerank(self, query: str, hits: list, *, top_k: int,
               query_emb: list = None) -> list:
        """对候选 hits=[(Doc, conf)] 重排，返回 top_k 个 [(Doc, conf)]。"""
        ...


class MMRReranker(Reranker):
    """最大边际相关（MMR）精排：在「与查询相关」与「彼此不冗余」间权衡，
    避免把 3 段近义重复塞进上下文（挤占 token、降低信息密度）。
    score = λ·rel(q,d) − (1−λ)·max_sim(d, 已选)。λ 越大越偏相关、越小越偏多样。
    纯向量运算、零新依赖，复用文档/查询既有嵌入（候选池很小，开销可忽略）。
    缺查询/文档向量时优雅退回原融合顺序。报告分数沿用融合 conf（保持低置信门标度一致）。"""
    name = "mmr"

    def __init__(self, lam: float = 0.7):
        self.lam = lam

    def rerank(self, query: str, hits: list, *, top_k: int,
               query_emb: list = None) -> list:
        if not hits or query_emb is None:
            return hits[:top_k]
        cand = [(d, c) for d, c in hits if getattr(d, "emb", None)]
        rest = [(d, c) for d, c in hits if not getattr(d, "emb", None)]
        if not cand:
            return hits[:top_k]
        rel = {d.id: _cosine(query_emb, d.emb) for d, _ in cand}
        selected: list = []
        pool = cand[:]
        while pool and len(selected) < top_k:
            best = None
            best_score = -1e18
            for d, c in pool:
                div = max((_cosine(d.emb, s.emb) for s, _ in selected), default=0.0)
                mmr = self.lam * rel[d.id] - (1.0 - self.lam) * div
                if mmr > best_score:
                    best_score = mmr
                    best = (d, c)
            selected.append(best)
            pool.remove(best)
        out = list(selected)
        for pair in rest:                 # 候选不足 top_k 时补无向量文档（保持原 conf）
            if len(out) >= top_k:
                break
            out.append(pair)
        return out[:top_k]


class HybridRetriever(Retriever):
    """BM25 + 语义嵌入，RRF（倒数排名融合）重排——兼顾关键词精确匹配与语义/同义召回。
    报告分数统一为 [0,1] 置信度（语义 cosine 与归一化 BM25 取大），与 _SEM_MIN 同标度。
    嵌入不可用（后端缺/调用失败/文档无向量）→ 自动降级为纯 BM25 召回（分数仍归一化）。
    RRF 对不同标度的排序列表稳健，无需调权重，是混合检索的事实标准做法。
    9-7：融合后再过 MMR 精排（去近义冗余、相关性优先），候选用 3×top_k 宽召回。"""
    name = "hybrid"
    min_score = _SEM_MIN

    def __init__(self, embedder: EmbeddingBackend, k_rrf: int = 60,
                 k1: float = 1.5, b: float = 0.75,
                 rerank: bool = None, mmr_lambda: float = -1.0,
                 wide_mult: int = 3):
        self.embedder = embedder
        self.bm25 = LexicalRetriever(k1, b)
        self.sem = SemanticRetriever(embedder)
        self.k_rrf = k_rrf
        self.wide_mult = max(1, wide_mult)
        if rerank is None:
            rerank = os.environ.get("CONV_RAG_RERANK", "1") != "0"
        if mmr_lambda < 0:
            mmr_lambda = float(os.environ.get("CONV_RAG_MMR_LAMBDA", "0.7"))
        self.reranker: Optional[Reranker] = MMRReranker(mmr_lambda) if rerank else None

    @staticmethod
    def _norm_bm25(s: float) -> float:
        # 单调压缩到 [0,1) 与 cosine 同标度：1.2→0.375, 2→0.5, 4→0.667
        return s / (s + 2.0)

    def search(self, query: str, kb: KnowledgeBase, top_k: int = 3) -> list:
        wide = max(top_k * self.wide_mult, top_k)
        bm = self.bm25.search(query, kb, wide)
        sem = self.sem.search(query, kb, wide)
        rrf: dict = {}
        conf: dict = {}
        docmap: dict = {}
        for r, (d, s) in enumerate(bm):
            rrf[d.id] = rrf.get(d.id, 0.0) + 1.0 / (self.k_rrf + r + 1)
            conf[d.id] = max(conf.get(d.id, 0.0), self._norm_bm25(s))
            docmap[d.id] = d
        for r, (d, s) in enumerate(sem):
            rrf[d.id] = rrf.get(d.id, 0.0) + 1.0 / (self.k_rrf + r + 1)
            conf[d.id] = max(conf.get(d.id, 0.0), float(s))
            docmap[d.id] = d
        order = sorted(rrf.keys(), key=lambda i: -rrf[i])
        fused = [(docmap[i], conf[i]) for i in order]
        # MMR 精排：仅在有 reranker 且候选含向量时生效；查询向量经缓存（同 query 零开销）
        if self.reranker is not None and len(fused) > 1:
            qe = None
            try:
                qe = self.embedder.embed([query])[0]
            except Exception:
                qe = None
            if qe is not None:
                return self.reranker.rerank(query, fused[:wide], top_k=top_k, query_emb=qe)
        return fused[:top_k]


# ── 后端注册表 ───────────────────────────────────────────────────────
class ConvBackendRegistry:
    def __init__(self):
        self._stt: dict[str, STTBackend] = {}
        self._llm: dict[str, LLMBackend] = {}
        self.default_stt = "mock_stt"
        self.default_llm = "mock_llm"
        self.guard: SafetyGuard = KeywordGuard([])   # 默认空 blocklist=不拦截
        self.kb: KnowledgeBase = KnowledgeBase()     # 9-4: 全局知识库（默认空=不启用 RAG）
        self.retriever: Retriever = LexicalRetriever()
        self.embedder: Optional[EmbeddingBackend] = None  # 9-5: 语义嵌入后端（None=纯 BM25）

    def set_embedder(self, backend, *, swap_retriever: bool = True, backfill: bool = True):
        """挂载语义嵌入后端：KB 后续文档自动嵌入，并默认切换为 BM25+语义 混合检索。
        backend=None → 撤回，退回纯 BM25。任何异常都不影响既有 BM25 路径（优雅降级）。"""
        self.embedder = backend
        self.kb.set_embedder(backend)
        if backend is None:
            self.retriever = LexicalRetriever()
            return
        if swap_retriever:
            self.retriever = HybridRetriever(backend)
        if backfill:
            try:
                self.kb.ensure_embeddings()
            except Exception:
                pass

    def set_guard(self, guard: SafetyGuard):
        self.guard = guard

    def register_stt(self, backend: STTBackend, *, default: bool = False):
        self._stt[backend.name] = backend
        if default:
            self.default_stt = backend.name

    def register_llm(self, backend: LLMBackend, *, default: bool = False):
        self._llm[backend.name] = backend
        if default:
            self.default_llm = backend.name

    def set_default_llm(self, name: str) -> bool:
        """切换默认（活跃）对话引擎；name 必须已注册。"""
        if name in self._llm:
            self.default_llm = name
            return True
        return False

    def remove_llm(self, name: str) -> bool:
        """移除一个对话后端；若移除的是默认，则回退到任一剩余后端（否则 mock_llm）。"""
        if name not in self._llm or name == "mock_llm":
            return False
        self._llm.pop(name, None)
        if self.default_llm == name:
            self.default_llm = next((n for n in self._llm if n != "mock_llm"),
                                    "mock_llm")
        return True

    def get_stt(self, name: str = "") -> Optional[STTBackend]:
        return self._stt.get(name or self.default_stt)

    def get_llm(self, name: str = "") -> Optional[LLMBackend]:
        return self._llm.get(name or self.default_llm)

    def list(self) -> dict:
        def _meta(b):
            return {"name": b.name, "mock": b.name.startswith("mock_"),
                    "class": type(b).__name__}
        def _llm_meta(b):
            m = _meta(b)
            # 暴露 UI 切换/分组所需信息（cloud 不回传 api_key）
            m.update({
                "kind":       getattr(b, "kind", "local"),
                "label":      getattr(b, "label", b.name),
                "model":      getattr(b, "model", ""),
                "base_url":   getattr(b, "base_url", ""),
                "keep_alive": getattr(b, "keep_alive", ""),
                "has_key":    bool(getattr(b, "api_key", "")),
                "is_default": (b.name == self.default_llm),
            })
            return m
        return {
            "stt": [_meta(b) for b in self._stt.values()],
            "llm": [_llm_meta(b) for b in self._llm.values()],
            "defaults": {"stt": self.default_stt, "llm": self.default_llm},
            "guard": {"name": self.guard.name,
                      "blocklist_size": len(getattr(self.guard, "blocklist", [])),
                      "redact": getattr(self.guard, "redact", None)},
            "kb": {"retriever": self.retriever.name, "docs": self.kb.count(),
                   "embedder": (getattr(self.embedder, "name", None) and
                                getattr(self.embedder, "model", self.embedder.name)),
                   "embedded_docs": sum(1 for d in self.kb.docs if d.emb)},
        }


registry = ConvBackendRegistry()
registry.register_stt(MockSTT(), default=True)
registry.register_llm(MockLLM(), default=True)


# ── 会话状态 ─────────────────────────────────────────────────────────
_SUMMARY_MAX_CHARS = int(os.environ.get("CONV_SUMMARY_MAX_CHARS", "720"))
_SUMMARY_KEEP_PAIRS = int(os.environ.get("CONV_SUMMARY_KEEP_PAIRS", "8"))
_RAG_MIN_SCORE = float(os.environ.get("CONV_RAG_MIN_SCORE", "1.2"))


@dataclass
class ConversationSession:
    session_id: str
    system_prompt: str = "你是一个友好的虚拟主播，用简短自然的中文回答。"
    max_turns: int = 20                       # 保留的最近对话轮数（user+assistant 成对）
    history: deque = field(default_factory=lambda: deque(maxlen=40))
    created: float = field(default_factory=time.time)
    short_opener: bool = True                 # 多语种回复时由 Hub 置 False，避免中文垫词污染
    summary: str = ""                         # 滚动摘要：长聊时压缩早期轮次，保持人设连贯
    long_term_memory: str = ""                # 9-8: 跨会话长期记忆（每轮由 Hub 据 user 召回注入）

    def messages(self, user_text: str, context: str = "",
                 emotion_hint: str = "") -> list:
        sys_content = self.system_prompt + (
            _short_opener_hint() if (_SHORT_OPENER and self.short_opener) else "")
        msgs = [{"role": "system", "content": sys_content}]
        if emotion_hint:        # 9-9: 共情语气引导（据用户当前情绪，仅影响本轮口吻）
            msgs.append({"role": "system", "content": emotion_hint})
        if self.long_term_memory:
            msgs.append({"role": "system",
                         "content": "【关于该用户的长期记忆】自然融入对话、体现「记得对方」，"
                                    "但不要生硬复述清单：\n" + self.long_term_memory})
        if self.summary:
            msgs.append({"role": "system",
                         "content": "【此前对话摘要】请保持人设与话题连贯：\n" + self.summary})
        if context:
            msgs.append({"role": "system",
                         "content": "回答时优先依据以下参考资料；资料未涵盖则如实说明。\n" + context})
        msgs.extend(self.history)
        msgs.append({"role": "user", "content": user_text})
        return msgs

    def maybe_compress(self, *, keep_pairs: int | None = None) -> bool:
        """长聊滚动压缩：将最早 2 轮压入 summary，保留最近 N 轮明细（零 LLM 延迟）。"""
        keep = int(keep_pairs or _SUMMARY_KEEP_PAIRS)
        keep_msgs = max(4, keep * 2)
        batch = 4
        if len(self.history) <= keep_msgs + batch:
            return False
        removed: list = []
        for _ in range(batch):
            if len(self.history) <= keep_msgs:
                break
            removed.append(self.history.popleft())
        if not removed:
            return False
        lines: list[str] = []
        i = 0
        while i < len(removed):
            item = removed[i]
            if item.get("role") == "user":
                u = (item.get("content") or "").replace("\n", " ")[:72]
                a = ""
                if i + 1 < len(removed) and removed[i + 1].get("role") == "assistant":
                    a = (removed[i + 1].get("content") or "").replace("\n", " ")[:72]
                    i += 2
                else:
                    i += 1
                if u:
                    lines.append(f"·用户:{u}" + (f"→答:{a}" if a else ""))
            else:
                i += 1
        if not lines:
            return False
        addition = " ".join(lines)
        self.summary = f"{self.summary} {addition}".strip() if self.summary else addition
        if len(self.summary) > _SUMMARY_MAX_CHARS:
            self.summary = self.summary[-_SUMMARY_MAX_CHARS:]
        return True

    def commit(self, user_text: str, assistant_text: str):
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": assistant_text})
        self.maybe_compress()


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, ConversationSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, sid: str, **kw) -> ConversationSession:
        async with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                s = ConversationSession(session_id=sid, **kw)
                self._sessions[sid] = s
            return s

    async def reset(self, sid: str) -> bool:
        async with self._lock:
            return self._sessions.pop(sid, None) is not None

    def count(self) -> int:
        return len(self._sessions)


sessions = SessionStore()


# ── 跨会话长期记忆（9-8：持久化「关于用户的事实」，按需召回注入后续对话）──────────
_LTM_MAX_PER_USER = int(os.environ.get("CONV_LTM_MAX", "64"))
_LTM_DEDUP_SIM = float(os.environ.get("CONV_LTM_DEDUP_SIM", "0.94"))
# 9-10 时效与冲突：
_LTM_HALFLIFE_DAYS = float(os.environ.get("CONV_LTM_HALFLIFE_DAYS", "45"))  # 召回时效半衰期(天)，<=0 关
_LTM_CONFLICT = os.environ.get("CONV_LTM_CONFLICT", "1") != "0"             # 冲突消解(新覆盖旧)
# 冲突带：相似度落在 [冲突阈, 去重阈) 视作「同主题的更新」→ 旧条被新条取代（如改喜好）
_LTM_CONFLICT_SIM = float(os.environ.get("CONV_LTM_CONFLICT_SIM", "0.86"))
# 9-11 按记忆类型分设半衰期：身份(姓名/职业/所在地)几乎不变→永不衰；偏好易变→衰减更快。
_LTM_HALFLIFE_PREF_DAYS = float(os.environ.get("CONV_LTM_HALFLIFE_PREF_DAYS", "20"))
_LTM_HALFLIFE_BY_KIND = {
    "identity": 0.0,                      # 0=不衰减（姓名、称呼、职业、所在地…）
    "preference": _LTM_HALFLIFE_PREF_DAYS,
    "fact": _LTM_HALFLIFE_DAYS,
}
# 类型启发式标记（仅当调用方传通用 kind="fact" 时细化；显式 identity/preference 不覆盖）
_LTM_IDENTITY_MARKERS = ("我叫", "名字", "称呼我", "我是", "我姓", "我住", "我家在",
                         "老家", "职业", "我的工作", "我在", "岁", "年龄", "生日")
_LTM_PREF_MARKERS = ("喜欢", "喜爱", "偏好", "最爱", "讨厌", "不喜欢", "爱吃", "爱喝",
                     "口味", "习惯", "兴趣", "爱好")


def _classify_kind(text: str) -> str:
    """据文本粗分记忆类型：身份/偏好/一般事实（用于按类型分设时效衰减）。"""
    t = text or ""
    if any(m in t for m in _LTM_IDENTITY_MARKERS):
        return "identity"
    if any(m in t for m in _LTM_PREF_MARKERS):
        return "preference"
    return "fact"


def _ltm_decay(ts: float, kind: str = "fact") -> float:
    """时效衰减权重 ∈ (0,1]：半衰期按记忆类型取（_LTM_HALFLIFE_BY_KIND）。
    半衰期<=0 或 ts 缺失 → 1.0（不衰减）。"""
    hl = _LTM_HALFLIFE_BY_KIND.get(kind, _LTM_HALFLIFE_DAYS)
    if hl <= 0 or not ts:
        return 1.0
    age_days = max(0.0, (time.time() - ts) / 86400.0)
    return 0.5 ** (age_days / hl)


class LongTermMemory:
    """跨会话长期记忆：把对话中抽取的「关于用户的持久事实」（称呼/偏好/背景/约定）落 SQLite，
    后续对话按需召回并注入 → 数字人「记得你」（重启不丢）。
    **复用** `KnowledgeBase`(嵌入/持久化) + 既有检索器(语义/词法)，按 user_key 隔离；
    去重(精确 + 语义近似) 防膨胀，按上限淘汰最旧。嵌入缺失自动退回词法/最近召回（优雅降级）。
    9-10：召回叠加时效衰减（旧偏好降权）；新增时做冲突消解（同主题高相似→新条取代旧条）。"""

    def __init__(self, db_path: str = "", embedder=None,
                 max_per_user: int = -1):
        self.kb = KnowledgeBase(db_path=db_path)
        if embedder is not None:
            self.kb.set_embedder(embedder)
        self.max_per_user = _LTM_MAX_PER_USER if max_per_user < 0 else max_per_user
        self._lock = threading.Lock()

    def set_embedder(self, embedder):
        self.kb.set_embedder(embedder)

    def _user_docs(self, user_key: str) -> list:
        return [d for d in self.kb.docs if d.meta.get("user_key") == user_key]

    def add(self, user_key: str, text: str, kind: str = "fact") -> Optional[str]:
        text = (text or "").strip()
        if not text or not user_key:
            return None
        if kind == "fact":                       # 9-11 仅细化通用类型，显式类型不覆盖
            kind = _classify_kind(text)
        with self._lock:
            existing = self._user_docs(user_key)
            low = text.lower()
            for d in existing:                       # 精确去重
                if (d.text or "").lower() == low:
                    return None
            qe = None
            superseded = None                        # 9-10 冲突消解：将被新条取代的旧条
            if self.kb.embedder is not None:
                try:
                    qe = self.kb.embedder.embed([text])[0]
                    best_d, best_s = None, 0.0
                    for d in existing:
                        if not d.emb:
                            continue
                        s = _cosine(qe, d.emb)
                        if s > best_s:
                            best_s, best_d = s, d
                    if best_d is not None:
                        if best_s >= _LTM_DEDUP_SIM:     # 近乎重复 → 跳过
                            return None
                        if _LTM_CONFLICT and best_s >= _LTM_CONFLICT_SIM:
                            superseded = best_d          # 同主题更新 → 新覆盖旧
                except Exception:
                    qe = None
            if superseded is not None:
                self._delete(superseded.id)
            # 复用已算向量，避免 kb.add 再次嵌入（嵌入失败时 qe=None，kb.add 自行处理）
            did = self.kb.add(text, meta={"user_key": user_key, "kind": kind,
                                          "ts": time.time()}, emb=qe)
            self._evict(user_key)
            return did

    def _evict(self, user_key: str):
        docs = self._user_docs(user_key)
        if len(docs) <= self.max_per_user:
            return
        docs.sort(key=lambda d: d.meta.get("ts", 0))      # 最旧先淘汰
        for d in docs[:len(docs) - self.max_per_user]:
            self._delete(d.id)

    def _delete(self, did: str):
        self.kb.docs = [d for d in self.kb.docs if d.id != did]
        if self.kb._conn is not None:
            try:
                self.kb._conn.execute("DELETE FROM kb_docs WHERE id=?", (did,))
                self.kb._conn.commit()
            except Exception:
                pass

    def recall(self, user_key: str, query: str = "", top_k: int = 4,
               retriever: Retriever = None) -> list:
        """召回该用户最相关的记忆 [(Doc, score)]。有 query+检索器→语义/词法；否则取最近。"""
        docs = self._user_docs(user_key)
        if not docs:
            return []
        if not query or retriever is None:
            docs = sorted(docs, key=lambda d: -d.meta.get("ts", 0))
            return [(d, 0.0) for d in docs[:top_k]]
        try:
            hits = retriever.search(query, self.kb, top_k=max(top_k * 4, top_k))
        except Exception:
            hits = []
        hits = [(d, s) for d, s in hits if d.meta.get("user_key") == user_key]
        if not hits:                                  # 检索无果 → 最近兜底
            docs = sorted(docs, key=lambda d: -d.meta.get("ts", 0))
            return [(d, 0.0) for d in docs[:top_k]]
        # 9-10/9-11 时效衰减：相关性 × 半衰期权重（按记忆类型：身份不衰、偏好衰快），
        # 旧记忆降权（不删除，仍可被强相关唤回）后重排
        hits = [(d, s * _ltm_decay(d.meta.get("ts", 0), d.meta.get("kind", "fact")))
                for d, s in hits]
        hits.sort(key=lambda x: -x[1])
        return hits[:top_k]

    def forget(self, user_key: str) -> int:
        n = 0
        for d in self._user_docs(user_key):
            self._delete(d.id); n += 1
        return n

    def list(self, user_key: str) -> list:
        docs = sorted(self._user_docs(user_key), key=lambda d: -d.meta.get("ts", 0))
        return [{"id": d.id, "text": d.text, "kind": d.meta.get("kind", "fact"),
                 "ts": d.meta.get("ts", 0)} for d in docs]

    def count(self, user_key: str = "") -> int:
        return len(self._user_docs(user_key)) if user_key else len(self.kb.docs)


long_term = LongTermMemory()


# ── 句级流式聚合 ─────────────────────────────────────────────────────
def _word_safe_cut(buf: str, cut: int, min_chars: int = 1) -> int:
    """长度封顶切句时，避免把拉丁单词切两半：若切点落在词中间且该段含空格，
    回退到最近的空格处。中文(无空格)与正好落在空格的情况原样返回。"""
    if cut <= 0 or cut >= len(buf):
        return cut
    seg = buf[:cut]
    if " " not in seg:
        return cut
    if buf[cut - 1].isspace() or buf[cut].isspace():
        return cut
    sp = seg.rfind(" ")
    if sp + 1 >= max(1, min_chars):
        return sp + 1
    return cut


class _SynthCancelled(Exception):
    """合成在途被 barge-in 取消（见 _synth_or_cancel）。"""


async def _synth_or_cancel(coro, cancel_event):
    """把一次合成协程与取消事件竞速：取消先到 → 中断在途合成(释放 GPU)并抛 _SynthCancelled；
    否则返回合成结果。cancel_event 为空(mock/无打断)时退化为普通 await。"""
    if cancel_event is None:
        return await coro
    task = asyncio.ensure_future(coro)
    cwait = asyncio.ensure_future(cancel_event.wait())
    try:
        await asyncio.wait({task, cwait}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        cwait.cancel()
    if cancel_event.is_set():
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise _SynthCancelled()
    return await task


async def aggregate_sentences(token_stream: AsyncIterator[str],
                              *, first_soft_split: bool = True,
                              first_chunk_stems: list | None = None
                              ) -> AsyncIterator[str]:
    """把 LLM 的增量 token 流聚合成「整句」流：遇句末标点 / 超长则 flush。
    first_soft_split: 首句允许在软标点处提前切，尽快产出第一块以降低 TTFA。
    first_chunk_stems: 已缓存开场词词干(按长度降序)。首块若以某词干开头，
        即在词干后切出(含紧随逗号)，使"有词无逗号"也能 0ms 命中缓存。"""
    buf = ""
    emitted_first = False
    async for tok in token_stream:
        buf += tok
        while True:
            cut = -1
            # 首块开场词词干优先：buf 以缓存口头语开头 → 切出该词(及紧随逗号)即 0ms 命中
            if (not emitted_first and first_chunk_stems):
                _lead = buf.lstrip()
                _pad = len(buf) - len(_lead)
                for stem in first_chunk_stems:
                    if _lead.startswith(stem):
                        nxt = _lead[len(stem):len(stem) + 1]
                        if nxt in _SENT_SOFT:          # 词后带逗号 → 含逗号一起切
                            cut = _pad + len(stem) + 1
                        elif nxt and nxt not in _SENT_END:  # 词后是正文 → 只切词
                            cut = _pad + len(stem)
                        # 词后为空(待更多字符)或句末标点 → 不在此切，走常规逻辑
                        if cut > 0:
                            break
            # 硬句末优先
            if cut < 0:
                for i, ch in enumerate(buf):
                    if ch in _SENT_END:
                        cut = i + 1; break
                    # 英文句号：字母后且后接空格/引号 才切（避免 3.14 / U.S. / e.g.）；
                    # 句末若是最后一个字符则等待更多 token，防止把小数点/缩写误判为句末。
                    if ch == "." and i > 0 and buf[i - 1].isalpha() and i + 1 < len(buf):
                        if buf[i + 1] in " \t\n\"')）":
                            cut = i + 1; break
            # 首句软切（仅一次）：软标点处尽早切；无软标点也在首块上限封顶，封住 TTFA
            if cut < 0 and first_soft_split and not emitted_first:
                for i, ch in enumerate(buf):
                    if ch in _SENT_SOFT and i + 1 >= _FIRST_MIN_CHARS:
                        cut = i + 1; break
                if cut < 0 and len(buf) >= _FIRST_MAX_CHARS:
                    cut = _word_safe_cut(buf, _FIRST_MAX_CHARS, _FIRST_MIN_CHARS)
            # 超长强制切
            if cut < 0 and len(buf) >= _MAX_SENT_CHARS:
                cut = _word_safe_cut(buf, _MAX_SENT_CHARS, 8)
            if cut < 0:
                break
            sent = buf[:cut].strip()
            buf = buf[cut:]
            if sent:
                emitted_first = True
                yield sent
    tail = buf.strip()
    if tail:
        yield tail


# ── 编排器 ───────────────────────────────────────────────────────────
# TTS 回调签名：async (text, *, index) -> audio_base64(str)  ；返回 "" 表示无音频
TtsFn = Callable[..., Awaitable[str]]
# 口型回调签名：async (audio_b64, *, index) -> video_base64(str)  ；返回 "" 表示无视频
LipSyncFn = Callable[..., Awaitable[str]]


class ConversationOrchestrator:
    def __init__(self, backend_registry: ConvBackendRegistry = registry):
        self.reg = backend_registry

    async def run_turn(self, session: ConversationSession, *,
                       audio_bytes: Optional[bytes] = None, text: str = "",
                       stt_engine: str = "", llm_engine: str = "",
                       tts_fn: Optional[TtsFn] = None,
                       tts_stream_fn=None,            # 流式 TTS：async gen，逐块产音频 b64（边出边喂口型→破单句固定延迟）
                       lipsync_fn: Optional[LipSyncFn] = None,
                       cancel_event: Optional[asyncio.Event] = None,
                       guard: Optional[SafetyGuard] = None,
                       kb: Optional[KnowledgeBase] = None,
                       retriever: Optional[Retriever] = None,
                       use_rag: bool = True, rag_top_k: int = 3,
                       rag_profile: str = "",
                       language: str = "", lipsync_serialize: bool = True,
                       llm_two_phase: bool = False, phase1_chars: int = 14,
                       phase2_wait_s: float = 8.0,
                       first_chunk_stems: list | None = None,
                       empathy_fn: Optional[Callable[[str], dict]] = None,
                       **llm_opts) -> AsyncIterator[dict]:
        """驱动一轮对话，异步产出事件字典。事件 phase:
        stt_start/stt_done/guard_block/rag/llm_start/sentence/guard_redact/
        tts_chunk/lipsync_chunk/done/cancelled/error
        双流：每句先产 tts_chunk(音频先到)，若提供 lipsync_fn 再产 lipsync_chunk(口型随后)。"""
        guard = guard or self.reg.guard
        kb = kb if kb is not None else self.reg.kb
        retriever = retriever or self.reg.retriever
        t0 = time.time()
        timings = {"stt_ms": 0, "llm_first_token_ms": 0,
                   "first_sentence_ms": 0, "first_audio_ms": 0,
                   "first_lipsync_ms": 0, "total_ms": 0}

        def _cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        # ── STT ──
        user_text = text or ""
        if not user_text and audio_bytes is not None:
            stt = self.reg.get_stt(stt_engine)
            if stt is None:
                yield {"phase": "error", "message": f"未知 STT 引擎: {stt_engine}"}; return
            yield {"phase": "stt_start", "engine": stt.name}
            try:
                t_stt = time.time()
                user_text = await stt.transcribe(audio_bytes, language=language)
                timings["stt_ms"] = int((time.time() - t_stt) * 1000)
            except Exception as e:
                yield {"phase": "error", "message": f"STT 失败: {e}"}; return
        yield {"phase": "stt_done", "text": user_text, "stt_ms": timings["stt_ms"]}
        if not user_text.strip():
            yield {"phase": "error", "message": "空输入"}; return
        if _cancelled():
            yield {"phase": "cancelled"}; return

        # ── 输入安全闸门（LLM 前）──
        if guard is not None:
            gi = guard.inspect(user_text, stage="input")
            if not gi.ok:
                yield {"phase": "guard_block", "stage": "input", "reason": gi.reason}
                return

        # ── 共情：据用户情绪生成本轮语气引导（9-9，可选；失败/未提供则跳过）──
        emotion_hint = ""
        if empathy_fn is not None:
            try:
                _emo = empathy_fn(user_text) or {}
                emotion_hint = _emo.get("tone_hint", "") or ""
                yield {"phase": "user_emotion",
                       "emotion": _emo.get("user_emotion", "neutral"),
                       "confidence": _emo.get("confidence", 0.0),
                       "tone": _emo.get("tone", ""),
                       # 9-11 轨迹可观测：持续负面升级 / 由负转正鼓励
                       "neg_streak": _emo.get("neg_streak", 0),
                       "escalated": bool(_emo.get("escalated", False)),
                       "encourage": bool(_emo.get("encourage", False))}
            except Exception:
                emotion_hint = ""

        # ── 长聊摘要压缩（LLM 前，零额外请求）──
        if session.maybe_compress():
            yield {"phase": "session_compress", "summary_chars": len(session.summary),
                   "history_msgs": len(session.history)}

        # ── RAG 知识库检索（LLM 前，可选）──
        rag_context = ""
        if use_rag and kb is not None and kb.count() and retriever is not None:
            try:
                hits = retriever.search(user_text, kb, top_k=rag_top_k)
            except Exception:
                hits = []
            if rag_profile:
                hits = [(d, s) for d, s in hits
                        if not d.meta.get("profile") or d.meta.get("profile") == rag_profile]
            if hits:
                top_sc = float(hits[0][1])
                # 阈值随检索器标度自适应：BM25 用 _RAG_MIN_SCORE(≈1.2)，
                # 语义/混合用 _SEM_MIN([0,1] cosine 标度)——经检索器 min_score 暴露。
                _floor = getattr(retriever, "min_score", None)
                low_conf = top_sc < (_floor if _floor is not None else _RAG_MIN_SCORE)
                if low_conf:
                    rag_context = (
                        "【注意】知识库检索置信度较低，请勿编造演讲稿或史实细节。"
                        "若无把握请坦诚说明「这部分我暂无准确资料」，"
                        "并可用一句简短追问澄清用户意图（如「您指的是哪方面？」）。\n"
                        + "\n".join(f"- {d.text}" for d, _ in hits[:max(1, rag_top_k - 1)])
                    )
                else:
                    rag_context = "\n".join(f"- {d.text}" for d, _ in hits)
                yield {"phase": "rag", "engine": retriever.name,
                       "low_confidence": low_conf, "top_score": round(top_sc, 3),
                       "hits": [{"id": d.id, "score": round(s, 3),
                                 "text": d.text[:240],
                                 "text_full": d.text,
                                 "source": (d.meta.get("source") or d.meta.get("title")
                                            or d.meta.get("profile") or ""),
                                 "profile": d.meta.get("profile", "")} for d, s in hits]}
            else:
                rag_context = (
                    "【注意】知识库未检索到直接相关段落。请勿编造演讲稿细节；"
                    "可礼貌追问用户具体想了解的话题或关键词。"
                )
                yield {"phase": "rag", "engine": retriever.name,
                       "low_confidence": True, "top_score": 0.0, "hits": [],
                       "no_hits": True}

        # ── LLM 流式 ──
        llm = self.reg.get_llm(llm_engine)
        if llm is None:
            yield {"phase": "error", "message": f"未知 LLM 引擎: {llm_engine}"}; return
        yield {"phase": "llm_start", "engine": llm.name}
        messages = session.messages(user_text, context=rag_context,
                                    emotion_hint=emotion_hint)

        t_llm = time.time()
        first_token_seen = False
        reply_parts: list[str] = []
        # 首块音频就绪信号：两段式据此放行 LLM 续写（首块 GPU 独占合成期间 LLM 暂停）
        _first_audio_ev = asyncio.Event()

        def _on_tok(tok: str):
            nonlocal first_token_seen
            if not first_token_seen:
                first_token_seen = True
                timings["llm_first_token_ms"] = int((time.time() - t_llm) * 1000)
            reply_parts.append(tok)

        async def _token_stream():
            async for tok in llm.stream(messages, **llm_opts):
                if _cancelled():
                    return
                _on_tok(tok)
                yield tok

        async def _token_stream_two_phase():
            """降低首音 GPU 争用：
            ① 先只生成够首块的极短开场，随即断开 LLM（本地模型停止生成→GPU 让给首块 TTS）；
            ② 首块 GPU 独占合成完(_first_audio_ev)后，用 assistant 预填把已说出的开场
               作为上文，让模型自然续写余下内容（无重复、连贯）。
            仅对「本地 GPU LLM + 需要合成」有收益；云端/mock 用单段式。"""
            opener = ""
            opts1 = dict(llm_opts)
            opts1.setdefault("max_tokens", max(8, phase1_chars))
            gen1 = llm.stream(messages, **opts1)
            try:
                async for tok in gen1:
                    if _cancelled():
                        return
                    _on_tok(tok)
                    opener += tok
                    yield tok
                    if len(opener.strip()) >= phase1_chars:
                        break
            finally:
                # 关闭首段连接 → 本地 LLM 立即停止生成，腾出 GPU 给首块合成
                try:
                    await gen1.aclose()
                except Exception:
                    pass
            if os.environ.get("CONV_TTFA_DEBUG") == "1":
                print(f"[2phase] 首段结束 +{int((time.time()-t0)*1000)}ms "
                      f"开场={len(opener)}字「{opener[:20]}」", flush=True)
            # 等首块合成完（GPU 独占）再续写；超时兜底，避免首块失败时卡死
            if tts_fn is not None and opener.strip():
                try:
                    await asyncio.wait_for(_first_audio_ev.wait(), timeout=phase2_wait_s)
                except asyncio.TimeoutError:
                    pass
            if _cancelled():
                return
            if os.environ.get("CONV_TTFA_DEBUG") == "1":
                print(f"[2phase] 放行续写 +{int((time.time()-t0)*1000)}ms "
                      f"(首音={timings['first_audio_ms']}ms)", flush=True)
            # 续写：开场作为 assistant 预填，模型从断点自然接续
            m2 = list(messages) + [{"role": "assistant", "content": opener}]
            async for tok in llm.stream(m2, **llm_opts):
                if _cancelled():
                    return
                _on_tok(tok)
                yield tok

        _tok_src = _token_stream_two_phase if llm_two_phase else _token_stream

        # ── 句级聚合 + 逐句 TTS ──
        # 口型(尤其高清扩散)耗时长 → 不在句循环里 await，改后台并发任务，完成即产出，
        # 让 LLM/TTS 继续流；服务端单线程串行化 GPU，避免显存争用。
        sent_idx = 0
        lip_tasks: list[dict] = []
        _tts_unavail_sent = False   # 本轮是否已发过 tts_unavailable 候选（至多一次）

        def _make_lip_event(it):
            try:
                vb = it["task"].result()
            except Exception as e:
                return {"phase": "lipsync_error", "index": it["index"], "message": str(e)}
            if vb:
                if timings["first_lipsync_ms"] == 0:
                    timings["first_lipsync_ms"] = int((time.time() - t0) * 1000)
                return {"phase": "lipsync_chunk", "index": it["index"], "video_base64": vb}
            return None

        try:
            async for sent in aggregate_sentences(_tok_src(),
                                                  first_chunk_stems=first_chunk_stems):
                if _cancelled():
                    for it in lip_tasks: it["task"].cancel()
                    # 多轮记忆：打断时也把「本轮问 + 已说出的部分回答」存入历史，
                    # 否则用户打断后追问会失忆（AI 不记得刚说过的话）。
                    _partial = "".join(reply_parts).strip()
                    if _partial:
                        session.commit(user_text, _partial + "（被用户打断）")
                    elif user_text.strip():
                        session.history.append({"role": "user", "content": user_text})
                    yield {"phase": "cancelled", "reply": _partial}; return
                # 进入本句 TTS 前，先等上一句口型跑完并产出。
                #   单卡部署下 CosyVoice TTS 与 MuseTalk 口型并发会严重争用 GPU
                #   (实测首句口型被拖 ~25×：680ms/帧 vs 热态 25ms/帧)。串行化后口型
                #   只与「LLM 出下一句 token」重叠(轻)，不与重型 TTS 重叠 → 各自满速，
                #   首帧与整轮总时长双降。多卡/轻量口型可传 lipsync_serialize=False 复用并发。
                if lipsync_serialize:
                    while lip_tasks:
                        it = lip_tasks.pop(0)
                        try:
                            # 可打断：等上一句口型期间(MuseTalk 可能数秒)发生 barge-in，
                            # 立即取消所有在途口型并结束本轮，不再傻等 → 停响不被口型拖长。
                            await _synth_or_cancel(it["task"], cancel_event)
                        except _SynthCancelled:
                            it["task"].cancel()
                            for x in lip_tasks: x["task"].cancel()
                            _partial = "".join(reply_parts).strip()
                            if _partial:
                                session.commit(user_text, _partial + "（被用户打断）")
                            elif user_text.strip():
                                session.history.append({"role": "user", "content": user_text})
                            yield {"phase": "cancelled", "reply": _partial}; return
                        except Exception: pass
                        ev = _make_lip_event(it)
                        if ev: yield ev
                sent_idx += 1
                if sent_idx == 1:
                    timings["first_sentence_ms"] = int((time.time() - t0) * 1000)
                # ── 输出安全闸门（TTS 前）──
                if guard is not None:
                    go = guard.inspect(sent, stage="output")
                    if not go.ok:
                        yield {"phase": "guard_block", "stage": "output",
                               "index": sent_idx, "reason": go.reason}
                        continue        # 该句不发声，跳过 TTS
                    if go.text != sent:
                        yield {"phase": "guard_redact", "index": sent_idx,
                               "reason": go.reason, "original": sent}
                        sent = go.text
                yield {"phase": "sentence", "index": sent_idx, "text": sent}
                # 混合路径：开场词(首句且启用短开场)走整句路径——它是预合成缓存词，0ms 命中 →
                # 保住极致 TTFA；若改走流式会绕过缓存、白白叠上 coalesce 首块延迟(实测 TTFA 637→1006ms)。
                # 正文句(idx≥2)才走流式：长句上边合成边出/喂口型，破“整句合成”固定延迟。短开场关闭
                # (多语种)时首句即正文，也走流式。
                _opener_cached = _SHORT_OPENER and getattr(session, "short_opener", True)
                # 仅当存在整句 tts_fn(能命中开场词缓存)时才把开场词转整句；否则开场词照样走流式
                # (绝不因转路而静音)。
                _opener_to_whole = (sent_idx == 1 and _opener_cached and tts_fn is not None)
                _use_stream = (tts_stream_fn is not None) and not _opener_to_whole
                if _use_stream:
                    # ── 流式 TTS：本句边合成边吐音频块，逐块即刻喂口型 → 破“整句合成”固定延迟 ──
                    # 与非流式分支互斥；首块到即放行两段式 + 起播，单句内 TTS/口型/播放重叠。
                    _chunk_no = 0
                    try:
                        async for audio_b64 in tts_stream_fn(sent, index=sent_idx):
                            if _cancelled():
                                for it in lip_tasks: it["task"].cancel()
                                _partial = "".join(reply_parts).strip()
                                if _partial:
                                    session.commit(user_text, _partial + "（被用户打断）")
                                elif user_text.strip():
                                    session.history.append({"role": "user", "content": user_text})
                                yield {"phase": "cancelled", "reply": _partial}; return
                            if not audio_b64:
                                continue
                            if timings["first_audio_ms"] == 0:
                                timings["first_audio_ms"] = int((time.time() - t0) * 1000)
                                _first_audio_ev.set()
                            yield {"phase": "tts_chunk", "index": sent_idx,
                                   "chunk": _chunk_no, "audio_base64": audio_b64}
                            if lipsync_fn is not None:
                                yield {"phase": "lipsync_start", "index": sent_idx, "chunk": _chunk_no}
                                lip_tasks.append({"index": sent_idx,
                                    "task": asyncio.ensure_future(lipsync_fn(audio_b64, index=sent_idx))})
                            _chunk_no += 1
                    except Exception as e:
                        yield {"phase": "tts_error", "index": sent_idx, "message": str(e)}
                    else:
                        # 无异常却一块未产出 → 本句静默无声，发一次中性候选（门控在转发层）
                        if _chunk_no == 0 and not _tts_unavail_sent and _TTS_UNAVAIL_EVENT:
                            _tts_unavail_sent = True
                            yield {"phase": "tts_unavailable", "index": sent_idx}
                elif tts_fn is not None:
                    _t_syn = time.time()
                    _synth_exc = False
                    try:
                        # 可打断合成：本句合成(对 fish/CosyVoice 的 HTTP 调用)可达 1-3s，期间发生
                        # barge-in 时把在途合成立即取消(中断该 HTTP→释放 GPU 给新一轮)，并丢弃本句
                        # 音频/口型，避免「打断后还冒出一句」+ 旧轮占用算力拖慢新轮。停响 ~2.7s→<0.2s。
                        audio_b64 = await _synth_or_cancel(
                            tts_fn(sent, index=sent_idx), cancel_event)
                        if sent_idx == 1 and os.environ.get("CONV_TTFA_DEBUG") == "1":
                            print(f"[2phase] 首块合成 {int((time.time()-_t_syn)*1000)}ms "
                                  f"({len(sent)}字「{sent[:16]}」) 起+{int((_t_syn-t0)*1000)}ms",
                                  flush=True)
                    except _SynthCancelled:
                        # barge-in 命中本句合成期：丢弃本句，已说部分入历史，干净结束本轮
                        for it in lip_tasks: it["task"].cancel()
                        _partial = "".join(reply_parts).strip()
                        if _partial:
                            session.commit(user_text, _partial + "（被用户打断）")
                        elif user_text.strip():
                            session.history.append({"role": "user", "content": user_text})
                        yield {"phase": "cancelled", "reply": _partial}; return
                    except Exception as e:
                        audio_b64 = ""
                        _synth_exc = True
                        yield {"phase": "tts_error", "index": sent_idx, "message": str(e)}
                    if audio_b64:
                        if timings["first_audio_ms"] == 0:
                            timings["first_audio_ms"] = int((time.time() - t0) * 1000)
                            _first_audio_ev.set()   # 放行两段式续写：首块已合成完
                        yield {"phase": "tts_chunk", "index": sent_idx,
                               "audio_base64": audio_b64}
                        # ── 双流：音频先到，口型后台并发（不阻塞后续句）──
                        if lipsync_fn is not None:
                            yield {"phase": "lipsync_start", "index": sent_idx}
                            lip_tasks.append({"index": sent_idx,
                                "task": asyncio.ensure_future(lipsync_fn(audio_b64, index=sent_idx))})
                    elif not _synth_exc and not _tts_unavail_sent and _TTS_UNAVAIL_EVENT:
                        # 合成无异常却返回空（引擎全不可达/被 B1 快跳）→ 静默无声，发一次中性候选
                        _tts_unavail_sent = True
                        yield {"phase": "tts_unavailable", "index": sent_idx}
                # 机会式回收已完成的口型任务（不阻塞）
                for it in [x for x in lip_tasks if x["task"].done()]:
                    lip_tasks.remove(it)
                    ev = _make_lip_event(it)
                    if ev: yield ev
            # 句流结束：等剩余口型任务跑完（按完成顺序产出）
            while lip_tasks:
                done_set, _ = await asyncio.wait([x["task"] for x in lip_tasks],
                                                 return_when=asyncio.FIRST_COMPLETED)
                for it in [x for x in lip_tasks if x["task"] in done_set]:
                    lip_tasks.remove(it)
                    ev = _make_lip_event(it)
                    if ev: yield ev
        except Exception as e:
            for it in lip_tasks: it["task"].cancel()
            yield {"phase": "error", "message": f"LLM/编排失败: {e}"}; return

        reply = "".join(reply_parts).strip()
        session.commit(user_text, reply)
        timings["total_ms"] = int((time.time() - t0) * 1000)
        # 容灾可观测：若 FailoverLLM 实际由兜底引擎服务，告知前端轻提示
        _served = getattr(llm, "last_used", "") or llm.name
        _fellback = bool(_served and _served != llm.name)
        yield {"phase": "done", "reply": reply, "recognized": user_text,
               "sentences": sent_idx, "timings": timings,
               "session_id": session.session_id,
               "served_by": _served, "fallback": _fellback,
               "summary_chars": len(session.summary),
               "history_msgs": len(session.history)}
