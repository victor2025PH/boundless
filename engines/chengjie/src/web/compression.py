# -*- coding: utf-8 -*-
"""HTTP 响应压缩中间件：brotli 优先、gzip 兜底（P2 传输减重，2026-08-07）。

为什么自带而不是 pip 装 ``brotli-asgi``：
- 编解码库 ``brotli`` 本机已在环境里（VLM/评测链早有），中间件本体 ~120 行——
  自带省掉一个第三方运行时依赖（共享生产机改环境影响所有并行线，能免则免）；
- 顺带修正 GZipMiddleware 的一个盲区：**SSE（text/event-stream）不该压缩**——
  压缩器缓冲会延迟事件推送（坐席端 SSE 心跳/告警都走它），本实现显式跳过；
- brotli 对文本比 gzip 再小 ~15-20%：工作台 1.9MB HTML gzip 552KB → br ~450KB，
  在 ~30KB/s 反向 SSH 隧道上每次导航再省 ~3s。

语义对齐 starlette GZipMiddleware：minimum_size 跳过小响应、已带
Content-Encoding 的响应不二次压缩、流式响应逐 chunk 压缩+flush、Vary 头追加。
``brotli`` 库缺失时自动退化为纯 gzip（永不因为可选依赖崩启动）。
"""

from __future__ import annotations

import gzip
import io
import zlib

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:
    import brotli  # 已在环境（1.2.0）；requirements 亦登记
    _HAS_BROTLI = True
except Exception:  # pragma: no cover - 环境缺库的退化档
    brotli = None
    _HAS_BROTLI = False


class CompressionMiddleware:
    """br 优先 / gzip 兜底的响应压缩（替代裸 GZipMiddleware）。"""

    def __init__(self, app: ASGIApp, minimum_size: int = 1024,
                 br_quality: int = 5, gzip_level: int = 6) -> None:
        # br_quality=5：动态压缩的甜点档（q11 是静态资源预压档，CPU 会拖垮首字节）
        self.app = app
        self.minimum_size = minimum_size
        self.br_quality = br_quality
        self.gzip_level = gzip_level

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        accept = Headers(scope=scope).get("Accept-Encoding", "")
        if _HAS_BROTLI and "br" in accept:
            algo = "br"
        elif "gzip" in accept:
            algo = "gzip"
        else:
            await self.app(scope, receive, send)
            return
        responder = _CompressResponder(
            self.app, algo,
            minimum_size=self.minimum_size,
            br_quality=self.br_quality,
            gzip_level=self.gzip_level,
        )
        await responder(scope, receive, send)


class _CompressResponder:
    def __init__(self, app: ASGIApp, algo: str, *, minimum_size: int,
                 br_quality: int, gzip_level: int) -> None:
        self.app = app
        self.algo = algo
        self.minimum_size = minimum_size
        self.br_quality = br_quality
        self.gzip_level = gzip_level
        self.send: Send = lambda *_: None  # type: ignore[assignment]
        self.initial_message: Message = {}
        self.started = False
        self.passthrough = False
        self.compressor = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        await self.app(scope, receive, self._send_wrapper)

    # ── 压缩器（br 与 gzip 统一成 process/flush/finish 三口）────────────────
    def _new_compressor(self):
        if self.algo == "br":
            return _BrStream(self.br_quality)
        return _GzStream(self.gzip_level)

    async def _send_wrapper(self, message: Message) -> None:
        mtype = message["type"]
        if mtype == "http.response.start":
            # 押到首个 body 块再定夺（要看长度/类型/是否已编码）
            self.initial_message = message
            return
        if mtype != "http.response.body" or self.passthrough:
            await self.send(message)
            return

        body = message.get("body", b"")
        more = message.get("more_body", False)

        if not self.started:
            self.started = True
            headers = Headers(raw=self.initial_message["headers"])
            ct = headers.get("Content-Type", "")
            skip = (
                "Content-Encoding" in headers            # 上游已压缩，绝不套两层
                or ct.startswith("text/event-stream")     # SSE：压缩缓冲会拖住事件推送
                or (not more and len(body) < self.minimum_size)
            )
            if skip:
                self.passthrough = True
                await self.send(self.initial_message)
                await self.send(message)
                return

            self.compressor = self._new_compressor()
            out_headers = MutableHeaders(raw=self.initial_message["headers"])
            out_headers["Content-Encoding"] = self.algo
            out_headers.add_vary_header("Accept-Encoding")
            if not more:
                data = self.compressor.compress_all(body)
                out_headers["Content-Length"] = str(len(data))
                await self.send(self.initial_message)
                await self.send({"type": "http.response.body", "body": data,
                                 "more_body": False})
                return
            # 流式：长度未知，交给 chunked 传输
            del out_headers["Content-Length"]
            await self.send(self.initial_message)
            await self.send({"type": "http.response.body",
                             "body": self.compressor.process_flush(body),
                             "more_body": True})
            return

        # 流式后续块
        if more:
            await self.send({"type": "http.response.body",
                             "body": self.compressor.process_flush(body),
                             "more_body": True})
        else:
            await self.send({"type": "http.response.body",
                             "body": self.compressor.finish(body),
                             "more_body": False})


class _BrStream:
    def __init__(self, quality: int) -> None:
        self._c = brotli.Compressor(quality=quality)

    def compress_all(self, data: bytes) -> bytes:
        return self._c.process(data) + self._c.finish()

    def process_flush(self, data: bytes) -> bytes:
        return self._c.process(data) + self._c.flush()

    def finish(self, data: bytes) -> bytes:
        return self._c.process(data) + self._c.finish()


class _GzStream:
    """gzip 流式压缩（zlib wbits=31 = gzip 封装），一次性/流式同一实现。"""

    def __init__(self, level: int) -> None:
        self._c = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)

    def compress_all(self, data: bytes) -> bytes:
        return self._c.compress(data) + self._c.flush(zlib.Z_FINISH)

    def process_flush(self, data: bytes) -> bytes:
        return self._c.compress(data) + self._c.flush(zlib.Z_SYNC_FLUSH)

    def finish(self, data: bytes) -> bytes:
        return self._c.compress(data) + self._c.flush(zlib.Z_FINISH)
