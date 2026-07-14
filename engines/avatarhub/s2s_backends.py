# -*- coding: utf-8 -*-
"""S2S 同传后端插件层（P0-1 竞争力增强：INTERP_S2S_BACKEND 抽象）。

动机（2026-07 竞品分析）：字节 Seed LiveInterpret 2.0 代表的"端到端语音到语音同传"
在延迟（~2.2s vs 级联 3-5s）与译文质量上对本地级联(STT→NMT→TTS)形成代差。
本模块把云端 S2S 作为【可选后端】接入——默认关闭、密钥不配不连网，
本地级联永远是兜底（离线可用性是本产品的立身之本，绝不动摇）。

设计：
- 纯协议客户端，零项目内依赖（不 import live_interpreter，胶水层在调用方），
  依赖仅 stdlib + websockets（facefusion 环境已有）。numpy 不需要（音频进出都是 bytes）。
- 火山引擎"同声传译 2.0"(AST v2 / Seed LiveInterpret)：
  wss://openspeech.bytedance.com/api/v4/ast/v2/translate
  Protobuf over WebSocket。协议消息小而稳定（TranslateRequest/TranslateResponse），
  这里手写 wire-format 编解码（~120 行），不引入 protoc 代码生成/protobuf 运行时，
  离线可单测（--selftest 起本进程假服务器全链路回归）。
- 事件模型（服务端）：
  SessionStarted(150) / SessionFailed(153) / SessionFinished(152)
  SourceSubtitle Start/Response/End (650/651/652)   原文字幕（增量→定稿）
  TranslationSubtitle Start/Response/End (653/654/655) 译文字幕（增量→定稿）
  TTSSentenceStart/End (350/351) + TTSResponse(352)  克隆配音音频（PCM 分块）
  UsageResponse(154) 用量 / AudioMuted(250) 静音提示
- 音色复刻：StartSession 的 speaker_id 留空 = 云端直接复刻"当前说话人"的音色输出译文
  （与本产品克隆音同传的定位天然对齐）。

安全/合规：密钥从环境变量读取（secrets.bat 注入），不落盘、不出现在日志。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
import logging

logger = logging.getLogger("s2s")

# ── 事件 ID（与火山 events.proto 对齐）──────────────────────────────────
EV_START_CONNECTION = 1
EV_FINISH_CONNECTION = 2
EV_CONNECTION_STARTED = 50
EV_CONNECTION_FAILED = 51
EV_START_SESSION = 100
EV_CANCEL_SESSION = 101
EV_FINISH_SESSION = 102
EV_SESSION_STARTED = 150
EV_SESSION_CANCELED = 151
EV_SESSION_FINISHED = 152
EV_SESSION_FAILED = 153
EV_USAGE_RESPONSE = 154
EV_TASK_REQUEST = 200
EV_UPDATE_CONFIG = 201
EV_AUDIO_MUTED = 250
EV_TTS_SENTENCE_START = 350
EV_TTS_SENTENCE_END = 351
EV_TTS_RESPONSE = 352
EV_TTS_ENDED = 359
EV_SRC_SUB_START = 650
EV_SRC_SUB_RESPONSE = 651
EV_SRC_SUB_END = 652
EV_DST_SUB_START = 653
EV_DST_SUB_RESPONSE = 654
EV_DST_SUB_END = 655

EVENT_NAMES = {
    150: "SessionStarted", 151: "SessionCanceled", 152: "SessionFinished",
    153: "SessionFailed", 154: "UsageResponse", 250: "AudioMuted",
    350: "TTSSentenceStart", 351: "TTSSentenceEnd", 352: "TTSResponse", 359: "TTSEnded",
    650: "SourceSubtitleStart", 651: "SourceSubtitleResponse", 652: "SourceSubtitleEnd",
    653: "TranslationSubtitleStart", 654: "TranslationSubtitleResponse",
    655: "TranslationSubtitleEnd",
}

_OK_STATUS = (0, 20000000)          # 20000000 = 火山成功码（同 HTTP 200 语义）

DEFAULT_URL = "wss://openspeech.bytedance.com/api/v4/ast/v2/translate"
DEFAULT_RESOURCE_ID = "volc.service_type.10053"   # 同传 2.0 资源号（控制台可查）

# ══════════ 迷你 protobuf wire-format 编解码（proto3，仅本协议所需子集）══════════

def _vint(n: int) -> bytes:
    """无符号 varint。负数按 64 位补码（本协议未用负值，防御性保留）。"""
    n &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _vint((field << 3) | wire)


def _f_varint(field: int, n: int, keep_zero: bool = False) -> bytes:
    if not n and not keep_zero:
        return b""
    return _tag(field, 0) + _vint(int(n))


def _f_bytes(field: int, b: bytes) -> bytes:
    if not b:
        return b""
    return _tag(field, 2) + _vint(len(b)) + b


def _f_str(field: int, s: str) -> bytes:
    return _f_bytes(field, (s or "").encode("utf-8"))


def _f_msg(field: int, payload: bytes, keep_empty: bool = False) -> bytes:
    if not payload and not keep_empty:
        return b""
    return _tag(field, 2) + _vint(len(payload)) + payload


def _pb_walk(buf: bytes):
    """通用解码：yield (field, wire, value)。wire0→int, wire2→bytes, wire1/5 跳过。"""
    i, n = 0, len(buf)
    while i < n:
        key = 0
        shift = 0
        while True:
            b = buf[i]; i += 1
            key |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        field, wire = key >> 3, key & 7
        if wire == 0:
            val = 0
            shift = 0
            while True:
                b = buf[i]; i += 1
                val |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            yield field, wire, val
        elif wire == 2:
            ln = 0
            shift = 0
            while True:
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
            yield field, wire, buf[i:i + ln]
            i += ln
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:                                    # 未知 wire → 无法安全跳过
            return


def _pb_fields(buf: bytes) -> dict:
    """{field: [values...]}（保留 repeated）。"""
    out: dict = {}
    for f, _w, v in _pb_walk(buf):
        out.setdefault(f, []).append(v)
    return out


def _first(d: dict, field: int, default=None):
    v = d.get(field)
    return v[0] if v else default


def _as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def _as_int(v, default: int = 0) -> int:
    """varint 字段防御式取整：遇到类型不符(如把请求误喂进响应解码器)不炸。"""
    try:
        if v is None or isinstance(v, (bytes, bytearray)):
            return default
        return int(v)
    except Exception:
        return default


# ── 请求编码（TranslateRequest）───────────────────────────────────────

def _enc_request_meta(endpoint: str = "", app_key: str = "", app_id: str = "",
                      resource_id: str = "", connection_id: str = "",
                      session_id: str = "", sequence: int = 0) -> bytes:
    return (_f_str(1, endpoint) + _f_str(2, app_key) + _f_str(3, app_id) +
            _f_str(4, resource_id) + _f_str(5, connection_id) +
            _f_str(6, session_id) + _f_varint(7, sequence))


def _enc_audio(fmt: str = "", codec: str = "", rate: int = 0, bits: int = 0,
               channel: int = 0, binary_data: bytes = b"") -> bytes:
    return (_f_str(4, fmt) + _f_str(5, codec) + _f_varint(7, rate) +
            _f_varint(8, bits) + _f_varint(9, channel) + _f_bytes(14, binary_data))


def _enc_corpus(glossary: dict | None = None, hot_words: list | None = None) -> bytes:
    out = b""
    for w in (hot_words or [])[:100]:
        out += _f_str(9, str(w))
    for k, v in list((glossary or {}).items())[:200]:
        entry = _f_str(1, str(k)) + _f_str(2, str(v))
        out += _f_msg(10, entry)
    return out


def _enc_req_params(mode: str, src: str, dst: str, speaker: str = "",
                    glossary: dict | None = None, hot_words: list | None = None) -> bytes:
    body = (_f_str(1, mode) + _f_str(2, src) + _f_str(3, dst) + _f_str(4, speaker))
    corpus = _enc_corpus(glossary, hot_words)
    if corpus:
        body += _f_msg(100, corpus)
    return body


def encode_start_session(cfg: dict, session_id: str, connection_id: str,
                         sequence: int = 0) -> bytes:
    """StartSession：源音频 16k/16bit/mono PCM；s2s 模式请求 24k float32 PCM 回传。"""
    meta = _enc_request_meta(
        endpoint=cfg.get("resource_id", DEFAULT_RESOURCE_ID),
        app_key=cfg.get("app_key", ""),
        app_id=cfg.get("app_id", ""),
        resource_id=cfg.get("resource_id", DEFAULT_RESOURCE_ID),
        connection_id=connection_id, session_id=session_id, sequence=sequence)
    user = _f_str(1, "avatarhub") + _f_str(3, "windows")
    src_audio = _enc_audio(fmt="wav", codec="raw", rate=16000, bits=16, channel=1)
    mode = cfg.get("mode", "s2s")
    if mode == "s2s":
        tgt_audio = _enc_audio(fmt="pcm", rate=int(cfg.get("tts_rate", 24000)),
                               bits=32, channel=1)
    else:
        tgt_audio = b""
    params = _enc_req_params(mode, cfg.get("source_language", "zh"),
                             cfg.get("target_language", "en"),
                             speaker=cfg.get("speaker_id", ""),
                             glossary=cfg.get("glossary"),
                             hot_words=cfg.get("hot_words"))
    body = (_f_msg(1, meta) + _f_varint(2, EV_START_SESSION) + _f_msg(3, user) +
            _f_msg(4, src_audio) + _f_msg(5, tgt_audio) + _f_msg(6, params))
    if cfg.get("denoise"):
        body += _f_varint(7, 1)
    return body


def encode_audio_frame(session_id: str, connection_id: str, sequence: int,
                       pcm16: bytes) -> bytes:
    meta = _enc_request_meta(connection_id=connection_id, session_id=session_id,
                             sequence=sequence)
    audio = _f_bytes(14, pcm16)
    return _f_msg(1, meta) + _f_varint(2, EV_TASK_REQUEST) + _f_msg(4, audio)


def encode_finish_session(session_id: str, connection_id: str, sequence: int) -> bytes:
    meta = _enc_request_meta(connection_id=connection_id, session_id=session_id,
                             sequence=sequence)
    return _f_msg(1, meta) + _f_varint(2, EV_FINISH_SESSION)


def encode_update_config(session_id: str, connection_id: str, sequence: int,
                         mode: str, glossary: dict | None, hot_words: list | None) -> bytes:
    meta = _enc_request_meta(connection_id=connection_id, session_id=session_id,
                             sequence=sequence)
    params = _enc_req_params(mode, "", "", glossary=glossary, hot_words=hot_words)
    return _f_msg(1, meta) + _f_varint(2, EV_UPDATE_CONFIG) + _f_msg(6, params)


# ── 响应解码（TranslateResponse）──────────────────────────────────────

def decode_response(buf: bytes) -> dict:
    """→ {event, name, text, data, start_time, end_time, spk_chg, status, message, session}"""
    f = _pb_fields(buf)
    meta = _pb_fields(_first(f, 1, b"") or b"")
    ev = _as_int(_first(f, 2, 0))
    return {
        "event": ev,
        "name": EVENT_NAMES.get(ev, str(ev)),
        "data": bytes(_first(f, 3, b"") or b""),
        "text": _as_str(_first(f, 4, b"")),
        "start_time": _as_int(_first(f, 5, 0)),
        "end_time": _as_int(_first(f, 6, 0)),
        "spk_chg": bool(_as_int(_first(f, 7, 0))),
        "muted_ms": _as_int(_first(f, 8, 0)),
        "session": _as_str(_first(meta, 1, b"")),
        "status": _as_int(_first(meta, 3, 0)),
        "message": _as_str(_first(meta, 4, b"")),
    }


# 服务端响应编码（仅 --selftest 假服务器用）
def encode_response(event: int, text: str = "", data: bytes = b"",
                    session: str = "", status: int = 20000000, message: str = "",
                    spk_chg: bool = False) -> bytes:
    meta = (_f_str(1, session) + _f_varint(3, status) + _f_str(4, message))
    return (_f_msg(1, meta) + _f_varint(2, event) + _f_bytes(3, data) +
            _f_str(4, text) + (_f_varint(7, 1) if spk_chg else b""))


# ══════════ 配置校验 ══════════

def seed_config_from_env(env=None) -> dict:
    env = env if env is not None else os.environ
    return {
        "url": (env.get("SEED_S2S_URL") or DEFAULT_URL).strip(),
        "app_key": (env.get("SEED_S2S_APP_KEY") or "").strip(),
        "access_key": (env.get("SEED_S2S_ACCESS_KEY") or "").strip(),
        "api_key": (env.get("SEED_S2S_API_KEY") or "").strip(),
        "app_id": (env.get("SEED_S2S_APP_ID") or "").strip(),
        "resource_id": (env.get("SEED_S2S_RESOURCE_ID") or DEFAULT_RESOURCE_ID).strip(),
        "mode": (env.get("SEED_S2S_MODE") or "s2s").strip().lower(),
        "speaker_id": (env.get("SEED_S2S_SPEAKER") or "").strip(),
        "denoise": (env.get("SEED_S2S_DENOISE", "1") == "1"),
        "langs": set(x.strip().lower() for x in
                     (env.get("SEED_S2S_LANGS") or "zh,en").split(",") if x.strip()),
        "tts_rate": int(env.get("SEED_S2S_TTS_RATE", "24000")),
    }


def seed_config_ready(env=None) -> tuple:
    """(ok, why)。新控制台单 Key(SEED_S2S_API_KEY) 或旧控制台双 Key 任一即可。"""
    c = seed_config_from_env(env)
    if c["api_key"] or (c["app_key"] and c["access_key"]):
        return True, ""
    return False, ("未配置密钥：请在 secrets.bat 设 SEED_S2S_API_KEY（新控制台）"
                   "或 SEED_S2S_APP_KEY + SEED_S2S_ACCESS_KEY（旧控制台）")


def _auth_headers(cfg: dict) -> dict:
    h = {"X-Api-Resource-Id": cfg.get("resource_id", DEFAULT_RESOURCE_ID),
         "X-Api-Connect-Id": str(uuid.uuid4())}
    if cfg.get("api_key"):
        h["X-Api-Key"] = cfg["api_key"]
    if cfg.get("app_key"):
        h["X-Api-App-Key"] = cfg["app_key"]
    if cfg.get("access_key"):
        h["X-Api-Access-Key"] = cfg["access_key"]
    if cfg.get("app_id"):
        h["X-Api-App-ID"] = cfg["app_id"]
    return h


# ══════════ Seed AST v2 客户端 ══════════

class SeedAstClient:
    """线程内 asyncio 客户端：feed(pcm16_bytes) 喂 16k/16bit/mono 音频，回调吐结果。

    回调（均在客户端线程触发，调用方自行保证线程安全/轻量）：
      on_source(text, phase, spk_chg)        phase: start|update|final
      on_translation(text, phase)
      on_tts_chunk(raw_bytes, rate, bits)    s2s 模式的克隆配音 PCM 分块
      on_tts_sentence_end()
      on_state(name, detail)                 SessionStarted / Usage / AudioMuted ...
      on_fail(reason)                        致命错误（调用方应回退本地级联）
    生命周期：start() 起线程；stop() 发 FinishSession 并优雅收尾。
    无中途自动重连——同传丢上下文的重连没有意义，失败即回退本地（一次性故障转移）。
    """
    FRAME_BYTES = 2560          # 80ms @16k/16bit/mono（官方建议一包 80ms）
    CONNECT_TIMEOUT = 8.0
    SILENCE_GAP = 0.24          # 无上行超过此秒数补一帧静音（服务端 VAD 需要连续流）

    def __init__(self, cfg: dict, on_source=None, on_translation=None,
                 on_tts_chunk=None, on_tts_sentence_end=None,
                 on_state=None, on_fail=None):
        self.cfg = dict(cfg)
        self.on_source = on_source or (lambda *a: None)
        self.on_translation = on_translation or (lambda *a: None)
        self.on_tts_chunk = on_tts_chunk or (lambda *a: None)
        self.on_tts_sentence_end = on_tts_sentence_end or (lambda: None)
        self.on_state = on_state or (lambda *a: None)
        self.on_fail = on_fail or (lambda *a: None)
        self.session_id = str(uuid.uuid4())
        self.connection_id = str(uuid.uuid4())
        self._seq = 0
        self._q: "queue.Queue" = queue.Queue(maxsize=512)   # ≈40s 上行缓冲
        self._pending = b""
        self._stop = threading.Event()
        self._failed = False
        self._connected = False
        self.stats = {"tx_frames": 0, "rx_events": 0, "tts_bytes": 0,
                      "src_final": 0, "dst_final": 0, "usage": None}
        self._th = threading.Thread(target=self._run, daemon=True,
                                    name="s2s-seed")

    # ── 对外接口（任意线程）─────────────────────────────────────────
    def start(self):
        self._th.start()

    def feed(self, pcm16: bytes):
        if self._stop.is_set() or self._failed or not pcm16:
            return
        try:
            self._q.put_nowait(pcm16)
        except queue.Full:                       # 网络堵死时丢最旧，保实时
            try:
                self._q.get_nowait()
                self._q.put_nowait(pcm16)
            except Exception:
                pass

    def update_corpus(self, glossary: dict | None, hot_words: list | None):
        try:
            self._q.put_nowait(("__CORPUS__", glossary or {}, hot_words or []))
        except queue.Full:
            pass

    def stop(self, join_timeout: float = 3.0):
        self._stop.set()
        try:
            self._q.put_nowait(b"__EOS__")
        except Exception:
            pass
        if self._th.is_alive():
            self._th.join(timeout=join_timeout)

    @property
    def connected(self) -> bool:
        return self._connected and not self._failed

    # ── 内部：线程 + asyncio ────────────────────────────────────────
    def _run(self):
        import asyncio
        try:
            asyncio.run(self._main())
        except Exception as e:
            self._die(f"客户端线程异常: {e}")

    def _die(self, reason: str):
        if self._failed:
            return
        self._failed = True
        self._connected = False
        try:
            self.on_fail(reason)
        except Exception:
            logger.exception("on_fail 回调异常")

    async def _main(self):
        import asyncio
        import websockets
        headers = _auth_headers(self.cfg)
        url = self.cfg.get("url") or DEFAULT_URL
        try:
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(url, additional_headers=headers,
                                       max_size=None, ping_interval=15,
                                       ping_timeout=20),
                    timeout=self.CONNECT_TIMEOUT)
            except TypeError:                    # 旧版 websockets 形参名不同
                ws = await asyncio.wait_for(
                    websockets.connect(url, extra_headers=headers,
                                       max_size=None, ping_interval=15,
                                       ping_timeout=20),
                    timeout=self.CONNECT_TIMEOUT)
        except Exception as e:
            self._die(f"连接失败: {e}")
            return
        try:
            await ws.send(encode_start_session(self.cfg, self.session_id,
                                               self.connection_id, self._seq))
            self._seq += 1
            # 等 SessionStarted（期间服务端也可能直接推错误）
            try:
                started = await asyncio.wait_for(self._wait_started(ws),
                                                 timeout=self.CONNECT_TIMEOUT)
            except Exception:
                started = False
            if not started:
                if not self._failed:
                    self._die("会话建立超时/被拒")
                return
            self._connected = True
            self.on_state("SessionStarted", {})
            await self._pump(ws)
        finally:
            try:
                await ws.close()
            except Exception:
                pass
            self._connected = False

    async def _wait_started(self, ws) -> bool:
        while True:
            raw = await ws.recv()
            if not isinstance(raw, (bytes, bytearray)):
                continue
            r = decode_response(bytes(raw))
            self.stats["rx_events"] += 1
            if r["status"] not in _OK_STATUS:
                self._die(f"服务端拒绝({r['status']}): {r['message']}")
                return False
            if r["event"] == EV_SESSION_STARTED:
                return True
            if r["event"] in (EV_SESSION_FAILED, EV_CONNECTION_FAILED):
                self._die(f"会话失败: {r['message'] or r['name']}")
                return False

    async def _pump(self, ws):
        import asyncio
        sender = asyncio.ensure_future(self._sender(ws))
        recver = asyncio.ensure_future(self._receiver(ws))
        done, pending = await asyncio.wait({sender, recver},
                                           return_when=asyncio.FIRST_COMPLETED)
        if sender in done and recver in pending:
            # 发送侧正常收尾(EOS/FinishSession 已发)：给接收侧 2s 拿最后的定稿/用量
            try:
                await asyncio.wait_for(recver, timeout=2.0)
            except Exception:
                recver.cancel()
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not self._stop.is_set():
                self._die(f"链路中断: {exc}")

    async def _sender(self, ws):
        import asyncio
        loop = asyncio.get_running_loop()
        last_tx = time.time()
        finished = False
        while True:
            try:
                item = await loop.run_in_executor(None, self._q_get)
            except Exception:
                item = None
            if item is None:
                if self._stop.is_set():
                    break
                if time.time() - last_tx > self.SILENCE_GAP:   # 静音保活
                    await self._send_pcm(ws, b"\x00" * self.FRAME_BYTES)
                    last_tx = time.time()
                continue
            if isinstance(item, tuple) and item and item[0] == "__CORPUS__":
                await ws.send(encode_update_config(
                    self.session_id, self.connection_id, self._seq,
                    self.cfg.get("mode", "s2s"), item[1], item[2]))
                self._seq += 1
                continue
            if item == b"__EOS__":
                finished = True
                break
            self._pending += item
            while len(self._pending) >= self.FRAME_BYTES:
                frame, self._pending = (self._pending[:self.FRAME_BYTES],
                                        self._pending[self.FRAME_BYTES:])
                await self._send_pcm(ws, frame)
                last_tx = time.time()
        if self._pending:
            await self._send_pcm(ws, self._pending)
            self._pending = b""
        if finished:
            try:
                await ws.send(encode_finish_session(self.session_id,
                                                    self.connection_id, self._seq))
                self._seq += 1
            except Exception:
                pass
            # 收尾交给 _receiver（_pump 在发送侧完成后给接收侧 2s 排空定稿/用量）

    def _q_get(self):
        try:
            return self._q.get(timeout=0.08)
        except queue.Empty:
            return None

    async def _send_pcm(self, ws, frame: bytes):
        await ws.send(encode_audio_frame(self.session_id, self.connection_id,
                                         self._seq, frame))
        self._seq += 1
        self.stats["tx_frames"] += 1

    async def _receiver(self, ws):
        while True:
            raw = await ws.recv()
            if not isinstance(raw, (bytes, bytearray)):
                continue
            if self._handle(bytes(raw)):
                return

    def _handle(self, raw: bytes) -> bool:
        """处理一条服务端消息。返回 True=会话终结。回调异常不打断链路。"""
        r = decode_response(raw)
        self.stats["rx_events"] += 1
        ev = r["event"]
        if r["status"] not in _OK_STATUS:
            self._die(f"服务端错误({r['status']}): {r['message']}")
            return True
        try:
            if ev == EV_SRC_SUB_START:
                self.on_source(r["text"], "start", r["spk_chg"])
            elif ev == EV_SRC_SUB_RESPONSE:
                self.on_source(r["text"], "update", r["spk_chg"])
            elif ev == EV_SRC_SUB_END:
                self.stats["src_final"] += 1
                self.on_source(r["text"], "final", False)
            elif ev == EV_DST_SUB_START:
                self.on_translation(r["text"], "start")
            elif ev == EV_DST_SUB_RESPONSE:
                self.on_translation(r["text"], "update")
            elif ev == EV_DST_SUB_END:
                self.stats["dst_final"] += 1
                self.on_translation(r["text"], "final")
            elif ev == EV_TTS_RESPONSE:
                if r["data"]:
                    self.stats["tts_bytes"] += len(r["data"])
                    self.on_tts_chunk(r["data"], int(self.cfg.get("tts_rate", 24000)), 32)
            elif ev in (EV_TTS_SENTENCE_END, EV_TTS_ENDED):
                self.on_tts_sentence_end()
            elif ev == EV_AUDIO_MUTED:
                self.on_state("AudioMuted", {"muted_ms": r["muted_ms"]})
            elif ev == EV_USAGE_RESPONSE:
                self.stats["usage"] = r["message"] or "reported"
                self.on_state("Usage", {})
            elif ev in (EV_SESSION_FINISHED, EV_SESSION_CANCELED):
                self.on_state("SessionFinished", {})
                return True
            elif ev == EV_SESSION_FAILED:
                self._die(f"会话失败: {r['message'] or 'SessionFailed'}")
                return True
        except Exception:
            logger.exception("S2S 回调异常(已忽略,链路继续)")
        return False


# ══════════ 后端注册表 ══════════

BACKENDS = {"seed": SeedAstClient}


def create_backend(name: str, cfg: dict, **callbacks):
    cls = BACKENDS.get((name or "").strip().lower())
    if cls is None:
        raise ValueError(f"未知 S2S 后端: {name}（可选: {sorted(BACKENDS)}）")
    return cls(cfg, **callbacks)


# ══════════ 离线自测（--selftest：假服务器全链路回归，不出网）══════════

def _selftest() -> int:
    import asyncio
    import math
    import struct

    # 1) 编解码往返
    cfg = {"resource_id": "volc.service_type.10053", "app_key": "AK", "access_key": "SK",
           "mode": "s2s", "source_language": "zh", "target_language": "en",
           "glossary": {"火山引擎": "Volcano Engine"}, "hot_words": ["AvatarHub"],
           "denoise": True}
    blob = encode_start_session(cfg, "sess-1", "conn-1", 0)
    f = _pb_fields(blob)
    assert int(_first(f, 2)) == EV_START_SESSION, "StartSession 事件号错误"
    params = _pb_fields(_first(f, 6))
    assert _as_str(_first(params, 1)) == "s2s" and _as_str(_first(params, 3)) == "en"
    corpus = _pb_fields(_first(params, 100))
    assert _as_str(_first(corpus, 9)) == "AvatarHub", "热词编码失败"
    ge = _pb_fields(_first(corpus, 10))
    assert _as_str(_first(ge, 2)) == "Volcano Engine", "术语表编码失败"
    frame = encode_audio_frame("sess-1", "conn-1", 3, b"\x01\x02" * 100)
    ff = _pb_fields(frame)
    assert int(_first(ff, 2)) == EV_TASK_REQUEST
    assert len(_first(_pb_fields(_first(ff, 4)), 14)) == 200
    resp = encode_response(EV_DST_SUB_END, text="Hello world", session="sess-1")
    d = decode_response(resp)
    assert d["event"] == EV_DST_SUB_END and d["text"] == "Hello world" and d["status"] == 20000000
    print("[1/3] protobuf 编解码往返 ... OK")

    # 2) 假服务器全链路：StartSession→字幕→TTS→Finish
    got = {"src": [], "dst": [], "tts": 0, "ended": 0, "state": [], "fail": []}

    async def fake_server(ws):
        async for raw in ws:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            # 服务端视角：解请求（字段号与响应不同，直接按事件字段2解）
            req = _pb_fields(bytes(raw))
            ev = int(_first(req, 2, 0) or 0)
            sid = _as_str(_first(_pb_fields(_first(req, 1, b"") or b""), 6, b""))
            if ev == EV_START_SESSION:
                await ws.send(encode_response(EV_SESSION_STARTED, session=sid))
            elif ev == EV_TASK_REQUEST:
                fake_server.n = getattr(fake_server, "n", 0) + 1
                if fake_server.n == 5:
                    await ws.send(encode_response(EV_SRC_SUB_START, text="你好", session=sid))
                    await ws.send(encode_response(EV_SRC_SUB_END, text="你好世界", session=sid))
                    await ws.send(encode_response(EV_DST_SUB_START, text="Hello", session=sid))
                    await ws.send(encode_response(EV_DST_SUB_END, text="Hello world", session=sid))
                    pcm = struct.pack("<%df" % 240,
                                      *[0.1 * math.sin(i / 8.0) for i in range(240)])
                    await ws.send(encode_response(EV_TTS_RESPONSE, data=pcm, session=sid))
                    await ws.send(encode_response(EV_TTS_SENTENCE_END, session=sid))
            elif ev == EV_FINISH_SESSION:
                try:                              # 客户端 2s 排空窗口关闭后发送会失败,属测试收尾竞态
                    await ws.send(encode_response(EV_USAGE_RESPONSE, message="dur=1", session=sid))
                    await ws.send(encode_response(EV_SESSION_FINISHED, session=sid))
                except Exception:
                    pass
                return

    async def run_case():
        try:
            from websockets.asyncio.server import serve
        except ImportError:
            from websockets import serve
        async with serve(fake_server, "127.0.0.1", 0) as server:
            port = list(server.sockets)[0].getsockname()[1]
            c = SeedAstClient(
                {**cfg, "url": f"ws://127.0.0.1:{port}", "api_key": "K"},
                on_source=lambda t, p, s: got["src"].append((p, t)),
                on_translation=lambda t, p: got["dst"].append((p, t)),
                on_tts_chunk=lambda b, r, bits: got.__setitem__("tts", got["tts"] + len(b)),
                on_tts_sentence_end=lambda: got.__setitem__("ended", got["ended"] + 1),
                on_state=lambda n, d: got["state"].append(n),
                on_fail=lambda m: got["fail"].append(m))
            c.start()
            for _ in range(10):
                c.feed(b"\x00" * SeedAstClient.FRAME_BYTES)
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.6)
            c.stop()
            assert ("final", "你好世界") in got["src"], f"未收到原文定稿: {got['src']}"
            assert ("final", "Hello world") in got["dst"], f"未收到译文定稿: {got['dst']}"
            assert got["tts"] == 960, f"TTS 字节数不符: {got['tts']}"
            assert got["ended"] >= 1 and "SessionStarted" in got["state"]
            assert not got["fail"], f"不应失败: {got['fail']}"

    asyncio.run(run_case())
    print("[2/3] 假服务器全链路(字幕+TTS+用量) ... OK")

    # 3) 故障转移：服务器半途掐线 → on_fail 必须触发
    got2 = {"fail": []}

    async def dead_server(ws):
        async for raw in ws:
            req = _pb_fields(bytes(raw))
            if int(_first(req, 2, 0) or 0) == EV_START_SESSION:
                sid = _as_str(_first(_pb_fields(_first(req, 1, b"") or b""), 6, b""))
                await ws.send(encode_response(EV_SESSION_STARTED, session=sid))
            else:
                await ws.close(code=1011)        # 突然掐线
                return

    async def run_fail():
        try:
            from websockets.asyncio.server import serve
        except ImportError:
            from websockets import serve
        async with serve(dead_server, "127.0.0.1", 0) as server:
            port = list(server.sockets)[0].getsockname()[1]
            c = SeedAstClient({**cfg, "url": f"ws://127.0.0.1:{port}", "api_key": "K"},
                              on_fail=lambda m: got2["fail"].append(m))
            c.start()
            for _ in range(4):
                c.feed(b"\x00" * SeedAstClient.FRAME_BYTES)
                await asyncio.sleep(0.03)
            await asyncio.sleep(0.8)
            c.stop()
            assert got2["fail"], "掐线后未触发 on_fail(故障转移不会发生)"

    asyncio.run(run_fail())
    print("[3/3] 故障转移(掐线→on_fail) ... OK")
    print("S2S selftest 全部通过")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
