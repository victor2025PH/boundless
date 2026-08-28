# -*- coding: utf-8 -*-
"""body 重放 receive 工厂（`body_size_limit_middleware` 的可测部件）。

该中间件为了防伪 Content-Length 会把请求 body 先读完再重放给下游，因此必须替换
`request._receive`。这个替换有一条**必须由结构保证**的不变量：重放耗尽后回落的
是**替换前**那个 receive。2026-08-28 两次事故坐实了写错的两种后果：

· 返回伪造的 ``{"type": "http.disconnect"}``——对普通响应无害（body 读完就没人
  再碰 receive），但 StreamingResponse 会并发跑 listen_for_disconnect 来实现
  「客户端一走就停止出话」，它把伪信号当真 → task group 的 cancel scope 立刻
  掐掉正在出话的生成器 → 小智问答 100% 在 meta 之后 0.2s 断流。
· 写成 ``await request._receive()``——赋值早已发生，这就是**自调用**：967 层后
  ``RecursionError``（boot err.log 实证），listen_for_disconnect 所在 task group
  同样被 cancel，于是**症状与上一种完全一样**，app.log 里都只剩一句
  ``Cancelled by cancel scope``，与「用户真的关掉了页面」无从区分。

故做成工厂：``original_receive`` 是形参、由闭包捕获，自引用在结构上写不出来；
且它有了 import 面，门禁能真正驱动它跑——此前只能静态断言源码里存在某行字符串，
而那行字符串恰好就是第二次事故的 bug 本身。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

Receive = Callable[[], Awaitable[Dict[str, Any]]]


def make_replay_receive(original_receive: Receive, full_body: bytes) -> Receive:
    """返回一个 receive：首次交出重放的 body，此后一律回落 ``original_receive``。

    回落（而不是伪造断开信号）让下游——尤其 StreamingResponse——去等**真的**
    http.disconnect，这是 POST + SSE 端点不被误掐的前提。
    """
    state = {"replayed": False}

    async def replay_receive() -> Dict[str, Any]:
        if state["replayed"]:
            return await original_receive()
        state["replayed"] = True
        return {"type": "http.request", "body": full_body, "more_body": False}

    return replay_receive
