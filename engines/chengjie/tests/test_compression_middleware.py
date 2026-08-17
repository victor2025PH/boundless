# -*- coding: utf-8 -*-
"""CompressionMiddleware 门禁（brotli 优先 / gzip 兜底，2026-08-07）。

替代裸 GZipMiddleware 的自带实现（src/web/compression.py）。钉住：
- 协商：br 优先于 gzip；只认 gzip 的老客户端拿 gzip；不声明的拿原文；
- 语义护栏：小响应不压、上游已 Content-Encoding 不二次压、**SSE 不压**
  （压缩器缓冲会拖住事件推送——GZipMiddleware 的历史盲区，本实现显式修掉）；
- 流式大响应逐块压缩后可完整还原；
- Vary: Accept-Encoding 正确追加（缓存代理正确性）。
"""

import gzip

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from src.web.compression import CompressionMiddleware

BIG = ("chengjie 无界智聊 compression probe -- " * 400)   # ~15KB，远超 minimum_size
SMALL = "tiny"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/big")
    async def big():
        return PlainTextResponse(BIG)

    @app.get("/small")
    async def small():
        return PlainTextResponse(SMALL)

    @app.get("/sse")
    async def sse():
        async def gen():
            for i in range(3):
                yield f"data: evt-{i}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/stream")
    async def stream():
        async def gen():
            for _ in range(40):
                yield BIG[:1000]
        return StreamingResponse(gen(), media_type="text/plain")

    @app.get("/preencoded")
    async def preencoded():
        return Response(gzip.compress(BIG.encode()), media_type="text/plain",
                        headers={"Content-Encoding": "gzip"})

    app.add_middleware(CompressionMiddleware, minimum_size=1024)
    return app


@pytest.fixture()
def client():
    # httpx 会按 Accept-Encoding 自动解码，断言编码看响应头、断言正确性看解码后正文
    return TestClient(_app())


def test_brotli_preferred_when_client_accepts_both(client):
    r = client.get("/big", headers={"Accept-Encoding": "gzip, deflate, br"})
    assert r.headers.get("content-encoding") == "br"
    assert r.text == BIG, "br 往返必须无损"
    assert "accept-encoding" in r.headers.get("vary", "").lower()


def test_gzip_fallback_for_gzip_only_client(client):
    r = client.get("/big", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert r.text == BIG


def test_identity_when_client_accepts_nothing(client):
    r = client.get("/big", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers
    assert r.text == BIG


def test_small_response_not_compressed(client):
    r = client.get("/small", headers={"Accept-Encoding": "br, gzip"})
    assert "content-encoding" not in r.headers
    assert r.text == SMALL


def test_sse_never_compressed(client):
    """SSE 压缩=事件被压缩器缓冲延迟——坐席端心跳/告警都走 SSE，必须直通。"""
    r = client.get("/sse", headers={"Accept-Encoding": "br, gzip"})
    assert "content-encoding" not in r.headers
    assert "evt-2" in r.text


def test_preencoded_response_untouched(client):
    r = client.get("/preencoded", headers={"Accept-Encoding": "br, gzip"})
    assert r.headers.get("content-encoding") == "gzip", "已编码响应绝不能套第二层"
    # httpx 按 content-encoding 自动解压——若中间件套了第二层（br over gzip），
    # 这里解出来的就不是原文；r.text==原文 即证明只有原来那一层 gzip。
    assert r.text == BIG


def test_streaming_large_response_roundtrip_br(client):
    r = client.get("/stream", headers={"Accept-Encoding": "br"})
    assert r.headers.get("content-encoding") == "br"
    assert r.text == BIG[:1000] * 40, "流式逐块压缩必须可完整还原"
    assert "content-length" not in r.headers, "流式压缩长度未知，须走 chunked"


def test_streaming_large_response_roundtrip_gzip(client):
    r = client.get("/stream", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert r.text == BIG[:1000] * 40
