# -*- coding: utf-8 -*-
"""`/api/assistant/query` 的 SSE 心跳契约（2026-08-28 事故沉淀）。

事故复盘（当日生产实测，非推断）：
  · 日志 17:55:19 / 17:55:42 各一条 `[assistant] query 被取消`，与坐席截图的
    两次「网络异常，请重试」一一对应；
  · `assistant.db::qa_log` 最后一条记录停在 08-23 —— 当天的两次提问**从未走到
    任何 record 调用**，即请求是在 LLM 出话阶段断的；
  · 云端 DeepSeek 实测 0.28s 可达、日志无「直连流式失败」、前端 fetch 也没有
    任何超时 abort —— 三条都排除了「网络异常」这个字面解释。
根因：本端点先推 meta，随后在等 LLM 首 token 期间**完全零字节**（qa_log 里存
过 latency_ms=37638 的真实案例），任何中间层的空闲超时都会把这段静默当死连接
掐断；那时 `except CancelledError` 里再 yield err 已经没有接收者，前端只剩
meta → 落到兜底文案。同一个 app 里 admin.py 的另一个 SSE 一直有 keepalive，
这个端点漏了。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.web.body_replay import make_replay_receive
from src.web.routes.assistant_routes import (
    _KA_INTERVAL_SEC,
    _SSE_KEEPALIVE,
    stream_with_keepalive,
)

_ROUTE_SRC = (Path(__file__).resolve().parents[1]
              / "src" / "web" / "routes" / "assistant_routes.py")
_BALL_SRC = (Path(__file__).resolve().parents[1]
             / "shared" / "assistant" / "assistant-ball.js")


async def _gen(items, delay=0.0):
    for it in items:
        if delay:
            await asyncio.sleep(delay)
        yield it


async def _boom(items, delay=0.0):
    async for it in _gen(items, delay):
        yield it
    raise RuntimeError("upstream exploded")


def test_keepalive_frame_is_an_sse_comment():
    """心跳必须是 SSE 注释行：前端读取器据此天然忽略（零前端改动的前提）。"""
    assert _SSE_KEEPALIVE.startswith(":")
    assert _SSE_KEEPALIVE.endswith("\n\n")
    assert "data:" not in _SSE_KEEPALIVE


async def test_silence_emits_keepalive():
    kinds = []
    async for kind, _p in stream_with_keepalive(_gen(["a"], delay=0.30),
                                                interval=0.05):
        kinds.append(kind)
    assert "ka" in kinds, "上游静默超过 interval 却没产心跳＝事故会复发"
    assert kinds[-1] == "piece"


async def test_no_piece_lost_across_keepalives():
    """心跳不得吞 token。

    这是 `wait_for(q.get())` 竞态的回归钉：那种写法在超时取消时可能「已经取到
    值却把它丢掉」，症状是回答里随机少字，比整体报错更难查。
    """
    items = [str(i) for i in range(15)]
    got = []
    async for kind, payload in stream_with_keepalive(_gen(items, delay=0.02),
                                                     interval=0.01):
        if kind == "piece":
            got.append(payload)
    assert got == items


async def test_fast_upstream_emits_no_keepalive():
    """上游够快就不该有心跳（不给正常路径加噪声）。"""
    kinds = [k async for k, _ in stream_with_keepalive(_gen(list("abc")),
                                                       interval=5.0)]
    assert kinds == ["piece"] * 3


async def test_upstream_exception_propagates():
    """上游异常必须原样抛出——路由靠它决定「回落主链」还是「已吐半截则如实报错」。"""
    with pytest.raises(RuntimeError, match="upstream exploded"):
        async for _k, _p in stream_with_keepalive(_boom(["a"]), interval=5.0):
            pass


async def test_break_does_not_hang():
    """调用方 break（NO_BASIS 哨兵那条路径）必须能干净退出，不挂起。"""
    async for kind, _p in stream_with_keepalive(_gen(list("abcdef"),
                                                     delay=0.01),
                                                interval=5.0):
        if kind == "piece":
            break
    await asyncio.sleep(0.05)  # 让 finally 里 cancel 掉的 pump 真正收尾


def test_route_is_wired_and_headers_forbid_transform():
    """静态接线断言：防「心跳函数还在、但路由改回裸 async for」的静默回退。"""
    src = _ROUTE_SRC.read_text(encoding="utf-8")
    assert "stream_with_keepalive(" in src, "路由没有接心跳"
    assert src.count("yield _SSE_KEEPALIVE") >= 2, (
        "直连流式与非流式回落**两条**出话路径都必须有心跳"
        "（回落路径静默窗更长，云端降级时正是它在顶班）"
    )
    assert "no-transform" in src, "SSE 响应头缺 no-transform"
    assert "ka=%d" in src, (
        "「被取消」日志必须带心跳计数：ka>0＝连接一直有字节仍被掐断（不是空闲"
        "超时，得查别的策略），ka=0＝客户端自己走了。缺了它两种成因分不开"
    )


def test_frontend_ignores_unknown_sse_lines():
    """前端读取器必须容忍非 data: 行，否则心跳反而会打断流读。"""
    js = _BALL_SRC.read_text(encoding="utf-8")
    idx = js.find("function handleLine")
    assert idx > 0, "handleLine 不见了（SSE 读取器改名？）"
    body = js[idx:idx + 700]
    assert "JSON.parse" in body
    assert "catch" in body, "JSON.parse 没有 catch —— 心跳行会抛错中断流读"


def test_interval_is_sane():
    """心跳间隔要明显短于常见空闲超时（nginx 默认 60s / 多数隧道 30s+）。"""
    assert 1.0 <= _KA_INTERVAL_SEC <= 20.0


# ───────────────────────────── 三键同源（2026-08-28 401 错配事故）
def test_llm_keys_support_inherit_and_stay_same_source():
    """`assistant.query.llm` 的 base_url/model/api_key 必须都支持 inherit。

    事故：只有 api_key 写了 inherit，base_url/model 是硬编码的
    api.deepseek.com + deepseek-v4-flash。主链端点后来迁到 siliconflow，于是
    inherit 取到的是**新家的 key**，拿它打旧家端点 → 每次提问 401。而 401 落在
    `except Exception` 里只记一条 WARNING、然后静默回落，坐席端看到的是
    「网络异常，请重试」—— 因果链断了五天没人对得上。
    三个键同源（都 inherit）是唯一不会再分叉的配法。
    """
    src = _ROUTE_SRC.read_text(encoding="utf-8")
    idx = src.find("async def _direct_llm_stream")
    assert idx > 0
    body = src[idx:idx + 2600]
    for key in ("base_url", "model"):
        assert f'llm_cfg.get("{key}")' in body, f"{key} 解析没了？"
    # 三个键各自都要有 inherit 回落分支
    assert body.count('in ("", "inherit")') >= 3, (
        "base_url/model/api_key 三者都必须支持 inherit —— 少一个就还能分叉"
    )
    assert '_ai.get("base_url")' in body and '_ai.get("model")' in body, (
        "inherit 必须回落到 ai.* 主链同一份配置"
    )


def test_auth_failure_is_called_out_not_buried():
    """401/403 必须在日志里被点名成「配置分叉」，而不是混在普通失败里。

    认证被拒不是网络抖动，是**配置错了**；日志得直接说去哪儿看，否则下一个人
    还要把我今天走的整条排查路重走一遍。
    """
    src = _ROUTE_SRC.read_text(encoding="utf-8")
    assert "_authish" in src, "认证类失败没有单独分型"
    assert "401" in src and "Authentication" in src
    assert "同源" in src, "文案必须指出三键同源这个具体要求"


def test_body_limit_replay_must_not_fake_disconnect():
    """`body_size_limit_middleware` 的 replay_receive 不得伪造 http.disconnect。

    这是「网络异常」的**真正根因**（2026-08-28 实锤）：该中间件为了防伪
    Content-Length 会把 body 读完再重放给下游，而重放耗尽后原实现直接返回
    `{"type": "http.disconnect"}`。对普通响应无害；但 StreamingResponse 会并发
    跑 listen_for_disconnect（「客户端一走就停止出话」），它拿到这个伪造信号后
    task group 的 cancel scope 立刻掐掉正在出话的生成器 → 小智问答 100% 在 meta
    之后 0.2s 断流，坐席看到「网络异常，请重试」。

    正确行为＝回落真实 receive，让下游去等**真的**断开。

    ⚠ 本条曾**只做静态断言**（断言源码里存在 `await request._receive()`），而那行
    字符串恰好就是第二次事故的 bug 本身：赋值 `request._receive = replay_receive`
    已经发生，于是它是自调用 → 967 层 `RecursionError`（boot err.log 实证）→
    listen_for_disconnect 所在 task group 被 cancel → **症状与伪造 disconnect 完全
    一样**。教训：静态断言只能证明「作者写下了他打算写的那行」，证不了那行是对的。
    现在逻辑已提取为 `body_replay.make_replay_receive`（有 import 面），改为行为
    断言；换回任一错误写法本测试都会失败。
    """
    calls = {"n": 0}

    async def _original():
        calls["n"] += 1
        return {"type": "http.disconnect"}

    rcv = make_replay_receive(_original, b'{"q":"hi"}')

    async def _drive():
        one = await rcv()
        assert one == {"type": "http.request", "body": b'{"q":"hi"}',
                       "more_body": False}, "首次必须交出重放的 body"
        assert calls["n"] == 0, "首次不得触碰 original_receive"
        # 关键：第二次必须回落 original，而不是自调用（自调用 = RecursionError）
        two = await rcv()
        assert two == {"type": "http.disconnect"}, "重放耗尽后必须回落 original"
        assert calls["n"] == 1
        # 多次调用仍持续回落（listen_for_disconnect 是 while True 循环）
        await rcv()
        assert calls["n"] == 2

    asyncio.run(_drive())

    # 静态 ratchet：中间件不得再把自己当回落目标（自引用必然递归）
    admin_src = (Path(__file__).resolve().parents[1]
                 / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    assert "make_replay_receive(request._receive, full_body)" in admin_src, (
        "body_size_limit_middleware 必须经工厂替换 receive 并把**替换前**的 "
        "receive 作为回落目标"
    )
    assert "async def replay_receive" not in admin_src, (
        "回落逻辑已收口到 src/web/body_replay.py；就地重写闭包会让自引用与"
        "伪造 disconnect 两个坑重新变得可写"
    )


def test_self_referential_fallback_would_recurse():
    """自证：上一条的驱动方式（连调两次）确实抓得住「自引用回落」。

    2026-08-28 第二次事故的写法在此内联复刻。若这种驱动方式抓不到它，上一条的
    绿灯就毫无意义——而它此前正是被一句静态字符串断言放过去的。
    """
    state = {"replayed": False}

    async def bad_receive():
        if state["replayed"]:
            return await bad_receive()  # 事故写法：回落目标是自己
        state["replayed"] = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _drive():
        await bad_receive()  # 首次正常交出 body
        with pytest.raises(RecursionError):
            await bad_receive()  # 第二次必然递归

    asyncio.run(_drive())


def test_cancel_log_carries_stage():
    """取消日志必须带 stage —— 否则只知道「它死了」，不知道死在哪一步。

    CancelledError 是 BaseException，`except Exception` 抓不到，而 yield 是暂停
    点，取消可以落在任意两个 yield 之间。2026-08-28 排查这条故障时，正是因为
    只有一句「被取消」，才不得不靠逐层复刻（TestClient → uvicorn → +压缩 →
    +BaseHTTPMiddleware）去反推现场。
    """
    src = _ROUTE_SRC.read_text(encoding="utf-8")
    assert "stage=%s" in src, "取消日志缺 stage"
    for st in ("kb_searched", "meta_sent", "ctx_built", "llm_direct_open",
               "fallback_main"):
        assert f'stage = "{st}"' in src, f"阶段游标缺 {st}"
