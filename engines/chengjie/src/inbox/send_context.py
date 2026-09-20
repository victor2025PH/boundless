"""出站「人工 / 自动」上下文标记（M-2 C，#233，2026-09-06）。

手动发送与自动投递要分开配额、手动优先（UE7VM3 ⑧ / K9CY6R ②）：边车 ``send_backoff``
窗内对 ``manual=true`` 的发送放行一次探测性投递（成功即解锁）。发送链层层转手
（路由 → ``send_via_adapters`` → 编排器 worker / 平台适配器 → 边车 HTTP），给每层
签名加 ``origin`` 形参会碰十几个调用点（含测试假件）；这里用 ``contextvars``：
路由入口按 ``origin="manual"`` 置位，边车 HTTP 载荷组装处读取——同一 await 链内天然
传播，跨任务不泄漏。缺省 False＝自动链，行为与此前完全一致。
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

_MANUAL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "inbox_manual_send", default=False)


def is_manual_send() -> bool:
    """当前 await 链是否人工发送（坐席手发 / 人工通过草稿投递）。"""
    try:
        return bool(_MANUAL.get())
    except Exception:
        return False


@contextmanager
def manual_send_scope(enabled: bool = True) -> Iterator[None]:
    """在 ``with`` 块内标记为人工发送；退出恢复上一层的值（嵌套安全）。"""
    token = _MANUAL.set(bool(enabled))
    try:
        yield
    finally:
        try:
            _MANUAL.reset(token)
        except Exception:
            pass


__all__ = ["is_manual_send", "manual_send_scope"]
