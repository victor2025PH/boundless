# -*- coding: utf-8 -*-
"""TG 协议 worker loop 亲和守卫门禁（2026-08-17 贴纸真发实锤）。

pyrogram Client/Session 在**创建时**绑定 ``asyncio.get_event_loop()``；监督循环
在自己的 loop 上重建 worker 后，web 路由（另一 loop）对该账号的媒体发送在
``upload.SaveFilePart`` 首块就炸 ``attached to a different loop``（2026-08-16
voice_sender 同族先例）。守卫语义（``TelegramProtocolWorker._on_client_loop``）：

- 跨 loop → ``run_coroutine_threadsafe`` 封送回 client 自己的 loop；
- 同 loop → 直跑（零开销旧行为）；
- client 的 loop 未在运行（半死）→ 直跑，让真实异常如实暴露而非静默挂起。

本门禁用真双 loop（后台线程 run_forever）钉住上述路径——单 loop 假编排器
测不出这个病（test_sticker_routes 全绿它照炸）。
"""
import asyncio
import threading

import pytest

from src.integrations.account_orchestrator import TelegramProtocolWorker


class _FakeMsg:
    id = 42


class _FakeClient:
    """记录协程真正运行所在 loop 与调用形态的假 pyrogram client。"""

    def __init__(self, loop):
        self.loop = loop
        self.ran_on = []
        self.calls = []

    def _rec(self, kind):
        self.ran_on.append(asyncio.get_running_loop())
        self.calls.append(kind)

    async def send_message(self, target, text, **kw):
        self._rec("message")
        return _FakeMsg()

    async def send_sticker(self, target, path):
        self._rec("sticker")
        return _FakeMsg()

    async def send_photo(self, target, path, caption=""):
        self._rec("photo")
        return _FakeMsg()

    async def send_animation(self, target, path, caption=""):
        self._rec("animation")
        return _FakeMsg()

    async def send_document(self, target, path, caption=""):
        self._rec("document")
        return _FakeMsg()

    async def delete_messages(self, target, ids, revoke=True):
        self._rec("delete")
        return len(ids)


@pytest.fixture()
def bg_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def _worker(client) -> TelegramProtocolWorker:
    w = TelegramProtocolWorker({"account_id": "t1", "meta": {}}, {})
    w.client = client
    return w


async def test_same_loop_runs_direct():
    cur = asyncio.get_running_loop()
    c = _FakeClient(cur)
    res = await _worker(c).send_media(
        "me", media_path="x.webp", media_type="sticker")
    assert res["delivered"] and res["message_id"] == "42"
    assert c.ran_on == [cur]


async def test_cross_loop_media_marshalled_to_client_loop(bg_loop):
    c = _FakeClient(bg_loop)
    res = await _worker(c).send_media(
        "me", media_path="x.webp", media_type="sticker")
    assert res["delivered"] and res["message_id"] == "42"
    assert c.ran_on == [bg_loop], "上传协程必须跑在 client 自己的 loop 上"


async def test_cross_loop_text_marshalled_to_client_loop(bg_loop):
    c = _FakeClient(bg_loop)
    res = await _worker(c).send("me", "hi")
    assert res["delivered"] and c.ran_on == [bg_loop]


async def test_cross_loop_image_marshalled(bg_loop):
    c = _FakeClient(bg_loop)
    res = await _worker(c).send_media(
        "me", media_path="x.jpg", media_type="image", caption="c")
    assert res["delivered"] and c.ran_on == [bg_loop]


async def test_client_loop_not_running_falls_back_direct():
    dead = asyncio.new_event_loop()  # 从不 run —— 半死 worker 形态
    try:
        cur = asyncio.get_running_loop()
        c = _FakeClient(dead)
        res = await _worker(c).send_media(
            "me", media_path="x.webp", media_type="sticker")
        assert res["delivered"] and c.ran_on == [cur]
    finally:
        dead.close()


async def test_exception_propagates_across_loops(bg_loop):
    class _Boom(_FakeClient):
        async def send_sticker(self, target, path):
            raise RuntimeError("boom")

    c = _Boom(bg_loop)
    with pytest.raises(RuntimeError, match="boom"):
        await _worker(c).send_media(
            "me", media_path="x.webp", media_type="sticker")


# ── client_bound_loop：封送目标解析（session.loop 优先，Client.loop 会撒谎）──


class _Sess:
    def __init__(self, loop):
        self.loop = loop


async def test_resolver_prefers_session_loop(bg_loop):
    from src.integrations.telegram_companion_worker import client_bound_loop
    cur = asyncio.get_running_loop()
    c = _FakeClient(cur)          # Client.loop 撒谎说「就是当前 loop」
    c.session = _Sess(bg_loop)    # 会话真身绑在别的 loop
    assert client_bound_loop(c) is bg_loop


async def test_resolver_falls_back_to_client_loop(bg_loop):
    from src.integrations.telegram_companion_worker import client_bound_loop
    c = _FakeClient(bg_loop)      # 无 session（未 connect）
    assert client_bound_loop(c) is bg_loop
    c2 = _FakeClient(bg_loop)
    c2.session = object()         # session 无 loop 属性 → 回落
    assert client_bound_loop(c2) is bg_loop


async def test_lying_client_loop_still_marshalled_to_session_loop(bg_loop):
    """2026-08-18 悬案机制回归钉：Client.loop == 当前 loop（撒谎）而
    session.loop 在别处 → 守卫必须按 session.loop 封送，绝不能判同 loop 直跑。"""
    cur = asyncio.get_running_loop()
    c = _FakeClient(cur)
    c.session = _Sess(bg_loop)
    res = await _worker(c).send_media(
        "me", media_path="x.webp", media_type="sticker")
    assert res["delivered"] and c.ran_on == [bg_loop]


async def test_companion_worker_guard_uses_resolver(bg_loop):
    """companion worker 同一守卫路径（_on_client_loop → client_bound_loop）。"""
    from src.integrations.telegram_companion_worker import TelegramCompanionWorker
    cur = asyncio.get_running_loop()
    inner = _FakeClient(cur)          # 撒谎的 Client.loop
    inner.session = _Sess(bg_loop)    # 真身在 bg loop

    class _Wrap:  # A 线 TelegramClient 包装形态：pyrogram 在 .client
        def __init__(self, c):
            self.client = c

    w = TelegramCompanionWorker({"account_id": "c1", "meta": {}}, {})
    w.client = _Wrap(inner)
    out = await w._send_media_impl(
        "me", media_path="x.webp", media_type="sticker")
    assert out["delivered"] and inner.ran_on == [bg_loop]


# ── companion worker（本机生产实际在用的 TG worker 类）────────────────────────
# companion_runtime 开时全部 TG 协议号走 TelegramCompanionWorker（A 线运行时），
# 2026-08-17 实锤两颗雷都在它身上：① client 活在 A 线 loop、web 路由直 await 跨
# loop 崩；② _send_media_impl 缺 sticker/animation 分支 → 贴纸落 else 被当
# document 附件发出。以下用例把两颗雷都钉死。

from src.integrations.telegram_companion_worker import (  # noqa: E402
    TelegramCompanionWorker,
)


class _FakeALine:
    """假 A 线 TelegramClient 包装：.client=内层 pyrogram。"""

    def __init__(self, inner):
        self.client = inner

    async def send_message(self, target, text):
        self.client._rec("aline_text")
        return True


def _companion(inner) -> TelegramCompanionWorker:
    w = TelegramCompanionWorker(
        {"account_id": "t1", "meta": {}},
        {"platform_login": {"telegram": {"companion_media": True}}})
    w.client = _FakeALine(inner)
    return w


async def test_companion_sticker_native_branch_same_loop():
    """贴纸必须走 send_sticker 原生分支，不许落 document（第二颗雷回归钉）。"""
    c = _FakeClient(asyncio.get_running_loop())
    res = await _companion(c).send_media(
        "me", media_path="x.webp", media_type="sticker")
    assert res["delivered"] and c.calls == ["sticker"]


async def test_companion_animation_branch_same_loop():
    c = _FakeClient(asyncio.get_running_loop())
    res = await _companion(c).send_media(
        "me", media_path="x.gif", media_type="animation", caption="")
    assert res["delivered"] and c.calls == ["animation"]


async def test_companion_cross_loop_media_marshalled(bg_loop):
    c = _FakeClient(bg_loop)
    res = await _companion(c).send_media(
        "me", media_path="x.webp", media_type="sticker")
    assert res["delivered"] and res["message_id"] == "42"
    assert c.ran_on == [bg_loop] and c.calls == ["sticker"]


async def test_companion_cross_loop_text_marshalled(bg_loop):
    c = _FakeClient(bg_loop)
    res = await _companion(c).send("me", "hi")
    assert res["delivered"] and c.ran_on == [bg_loop]
    assert c.calls == ["aline_text"]


async def test_companion_cross_loop_delete_messages(bg_loop):
    c = _FakeClient(bg_loop)
    res = await _companion(c).delete_messages("123", ["7"], revoke=True)
    assert res["ok"] and res["deleted"] == 1
    assert c.ran_on == [bg_loop] and c.calls == ["delete"]


async def test_companion_media_flag_off_no_send_media():
    """companion_media 关 → 不绑 send_media（owns_media 判据，行为守恒）。"""
    w = TelegramCompanionWorker({"account_id": "t1", "meta": {}}, {})
    assert not hasattr(w, "send_media")


# ── B14 回归钉（2026-08-21 打包版全媒体上传跨环实锤）─────────────────────────

def test_client_bound_loop_realigns_lying_client_loop():
    """session.loop 才是真身——解析时必须把构造期撒谎的 Client.loop 扶正，
    否则 pyrogram save_file 内部 self.loop.create_task 起的上传 worker 与
    队列各在一环（打包版语音/图片上传同报 Queue is bound to a different
    event loop，skuio 红框截图实锤）。"""
    from src.integrations.telegram_companion_worker import client_bound_loop

    class _Obj:
        pass

    real, liar = object(), object()
    c = _Obj()
    c.loop = liar
    c.session = _Obj()
    c.session.loop = real
    assert client_bound_loop(c) is real
    assert c.loop is real, "Client.loop 必须被扶正（save_file 用它起 worker）"


def test_client_bound_loop_without_session_keeps_client_loop():
    from src.integrations.telegram_companion_worker import client_bound_loop

    class _Obj:
        pass

    lp = object()
    c = _Obj()
    c.loop = lp
    assert client_bound_loop(c) is lp
    assert c.loop is lp


async def test_voice_sender_marshals_by_session_loop_not_lying_client_loop(bg_loop):
    """voice_sender 此前读裸 client.loop——Client.loop 撒谎说「同环」时守卫
    判直跑，session 真身在别环照崩。必须经 client_bound_loop 解析。"""
    from src.client import voice_sender as vs

    class _Obj:
        pass

    cur = asyncio.get_running_loop()
    c = _Obj()
    c.loop = cur              # 撒谎：构造期 loop 恰好是当前环
    c.session = _Obj()
    c.session.loop = bg_loop  # 真身在后台环
    ran_on = []

    async def _send_voice(**kw):
        ran_on.append(asyncio.get_running_loop())
        return _FakeMsg()

    c.send_voice = _send_voice
    msg = await vs._invoke_on_client_loop(c, {"chat_id": 1, "voice": "x.ogg"})
    assert getattr(msg, "id", None) == 42
    assert ran_on == [bg_loop], "上传必须封送到 session 真身环"
