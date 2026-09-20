"""裸流上传的共用落盘片段（Q-40 A，2026-09-15，#316 追加）。

抽自 ``unified_inbox_send_routes._send_media_streamed``（M-3 A #227）的三个纯机械动作，
让相册上传口 ``persona_media_routes`` 与聊天 send-media **同一份**代码读流 / 写盘 / 吞流——
不另起一套、不各自演化。send-media 侧只改 import，行为零变化
（``tests/test_media_send_stream_m3.py`` 零改动全绿是本模块的验收线）。

三个动作：

- ``drain_stream``：超限 / 拒绝时把剩余请求体读掉再回 413 / 4xx。服务端一响应就关连接的话，
  浏览器还在推 body，只会看到连接被重置（onerror，无状态码），坐席看到的就又是「结果未知」。
  ≤ ``DRAIN_MAX`` 顺手吞完（本机回环秒级），更大的直接响应。
- ``read_head``：先读 ≥64KB 头部——内容守卫看魔数（前 16 字节），多读一点让空文件 /
  截断件也能判；**空头＝空文件**由调用方决定怎么拒。
- ``stream_to_file``：头部 + 剩余块攒到 ``WRITE_BUF`` 再经线程池写盘（磁盘慢 / 杀软扫描都不占
  事件循环）；边收边比上限，超限即停（峰值内存恒定），返回 ``(size, overflow)``。
  I/O 异常包成 ``StreamRecvError``（带已收字节数）交调用方清理 + 记日志。

调用方仍负责：上限来源、拒绝文案、幂等键、临时文件清理、每条退出路径的日志。
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Tuple

#: 超限时最多再吞多少字节让浏览器**读得到** 413。
DRAIN_MAX = 256 * 1024 * 1024
#: 写缓冲：收到的块攒到这么多再写一次盘。
WRITE_BUF = 1024 * 1024
#: 头部至少读多少再做内容判定。
HEAD_BYTES = 64 * 1024


class StreamRecvError(Exception):
    """流式落盘途中的 I/O / 读流异常；``received`` 是出错时已收到的字节数。"""

    def __init__(self, cause: BaseException, received: int):
        super().__init__(str(cause))
        self.cause = cause
        self.received = int(received)


async def drain_stream(chunks: Any, max_bytes: int) -> int:
    """读掉并丢弃剩余请求体（上限 max_bytes），返回吞掉的字节数。任何异常吞掉即停。"""
    n = 0
    try:
        async for c in chunks:
            n += len(c)
            if n >= max_bytes:
                break
    except Exception:  # noqa: BLE001
        pass
    return n


async def read_head(it: AsyncIterator[bytes], min_bytes: int = HEAD_BYTES) -> bytes:
    """从**迭代器**（已 ``__aiter__``）读到至少 ``min_bytes`` 或流尽，返回头部字节（可能为空）。

    调用方随后把同一个 ``it`` 交给 ``stream_to_file``——头部不会被重复消费。
    """
    head = b""
    while len(head) < min_bytes:
        try:
            c = await it.__anext__()
        except StopAsyncIteration:
            break
        head += c
    return head


async def stream_to_file(it: AsyncIterator[bytes], path: str, *, head: bytes,
                         cap_bytes: int, write_buf: int = WRITE_BUF) -> Tuple[int, bool]:
    """``head`` + ``it`` 余下的块流式写到 ``path``；超过 ``cap_bytes`` 即停。

    返回 ``(size, overflow)``：``overflow=True`` 时文件不完整（调用方删）、``size`` 是停下时
    已收的字节数（不是总大小——流式闸只知道「超了」）。I/O / 读流异常 → ``StreamRecvError``。
    """
    from starlette.concurrency import run_in_threadpool

    size = 0
    overflow = False
    try:
        fh = await run_in_threadpool(open, path, "wb")
        try:
            buf = bytearray(head)
            size = len(head)
            if size > cap_bytes:
                overflow = True
            while not overflow:
                if len(buf) >= write_buf:
                    await run_in_threadpool(fh.write, bytes(buf))
                    buf = bytearray()
                try:
                    c = await it.__anext__()
                except StopAsyncIteration:
                    break
                size += len(c)
                if size > cap_bytes:
                    overflow = True
                    break
                buf += c
            if buf and not overflow:
                await run_in_threadpool(fh.write, bytes(buf))
        finally:
            await run_in_threadpool(fh.close)
    except Exception as ex:  # noqa: BLE001
        raise StreamRecvError(ex, size) from ex
    return size, overflow


async def upload_file_chunks(upload: Any, chunk: int = WRITE_BUF):
    """把 Starlette ``UploadFile`` 变成与裸流同形状的异步块流（multipart 旧路复用同一落盘代码）。"""
    while True:
        c = await upload.read(chunk)
        if not c:
            return
        yield c


def declared_length(request: Any) -> int:
    """``Content-Length`` 请求头 → int；缺 / 坏 → 0（＝未知，调用方走流式闸）。"""
    try:
        return int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0
