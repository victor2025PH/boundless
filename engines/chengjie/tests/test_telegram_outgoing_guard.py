"""Telegram 私聊**实时** handler 的出站 / 系统会话守卫（2026-07-26 P0 潜伏事故）。

事故机制：pyrogram 的 dispatcher 对 ``UpdateNewMessage`` 零方向过滤
（``Message._parse`` 把 MTProto ``message.out`` 原样落进 ``outgoing``），``filters.private``
也只看 ``chat.type``。于是老板用手机 App 回客户时，那条 outgoing 消息经多端同步推给本
session、照样派发进 ``handle_private_message``。而私聊侧此前**一道守卫都没有**（群 handler
有双重守卫），后果两个且互相耦合：

1. **安全**：我方自己的话被当客户消息走完整 AI 管道 → 镜像成 ``direction="in"`` 并生成一条
   回复发给客户；
2. **功能死锁**：``_mirror_outgoing_message`` 内部要 ``claim`` 同一个 mid，实时 handler 已
   抢先 claim → 轮询侧那次镜像调用必然失败，「镜像手机已发消息」上线即死。

本文件把守卫的不变量钉死。与 ``test_telegram_outgoing_mirror.py`` 分工：那份测**轮询**侧
的镜像语义（落库字段/去重/时间闸门），这份测**实时** handler 的闸门位置与放行边界。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.client.message_dedup import MessageDedup
from src.client.telegram_client import TELEGRAM_SERVICE_CHAT_ID
from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb

ACCOUNT = "accTG"
PEER = 5433982810
SELF_ID = 999


# ── 探针替身 ────────────────────────────────────────────────────────────────

class _SpyLimiter:
    """限流器替身：记录是否被调用。

    出站消息若走到这里，我方自己的 uid 就会白耗令牌桶，连发几条甚至被
    ``check_auto_ban`` 把自己账号封了——所以「零调用」本身就是不变量。
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.allow_calls: list = []
        self.ban_checks: list = []
        self.banned_checks: list = []

    def allow(self, uid, chat_id):
        self.allow_calls.append((uid, chat_id))
        return True, ""

    def check_auto_ban(self, uid):
        self.ban_checks.append(uid)
        return False

    def is_banned(self, uid):
        self.banned_checks.append(uid)
        return False


class _SpyDedup(MessageDedup):
    """去重表替身：把 claim 调用按顺序记进共享 trace，用于钉死「先 claim 后 mirror」这个 bug。"""

    def __init__(self, trace: list):
        super().__init__()
        self._trace = trace

    def claim(self, chat_id, message_id):
        self._trace.append(("claim", chat_id, message_id))
        return super().claim(chat_id, message_id)


# ── 假 client 装配（沿用 test_telegram_outgoing_mirror.py 的最小构造法）──────

def _mk_tc(*, mirror_outgoing: bool = True, process_private: bool = True,
           trace: list | None = None, mirror_inbox: bool = True):
    from src.client.telegram_client import TelegramClient
    tc = TelegramClient.__new__(TelegramClient)
    tc.config = SimpleNamespace(
        get_telegram_config=lambda: {
            "process_private": process_private,
            "poll_fallback": {"mirror_outgoing": mirror_outgoing},
        },
        get=lambda _k, _d=None: _d if _d is not None else {},
    )
    tc._rate_limiter = _SpyLimiter()
    tc._msg_dedup = _SpyDedup(trace if trace is not None else [])
    tc._boot_timestamp = time.time() - 3600
    tc.user_info = SimpleNamespace(id=SELF_ID)
    tc.account_id = ACCOUNT
    tc._mirror_inbox = mirror_inbox
    tc._process_message = AsyncMock()
    return tc   # tc.logger 是惰性 property，无需注入


def _private_handler(tc):
    """跑真 ``_setup_handlers``，把装饰器闭包 ``handle_private_message`` 抓出来。

    刻意不 patch 掉注册过程——这样「守卫放在 handler 里的哪个位置」是被真代码路径
    验证的，而不是只测了抽出来的纯函数。
    """
    caught: dict = {}

    def _deco(_flt=None):
        def wrap(fn):
            caught[fn.__name__] = fn
            return fn
        return wrap

    tc.client = SimpleNamespace(
        on_message=_deco, on_edited_message=_deco,
        add_handler=lambda *a, **k: None,
    )
    tc._setup_handlers()
    return caught["handle_private_message"]


def _mk_msg(*, mid: int, outgoing: bool, chat_id: int = PEER,
            text: str | None = "好的，马上处理", from_id: int | None = None,
            age_sec: float = 5.0):
    ts = time.time() - age_sec
    return SimpleNamespace(
        id=mid, message_id=mid, text=text, caption=None,
        voice=None, audio=None, photo=None, document=None, video=None,
        video_note=None, animation=None, sticker=None,
        outgoing=outgoing,
        from_user=SimpleNamespace(
            id=SELF_ID if from_id is None and outgoing else (from_id or 123),
            is_bot=False),
        date=SimpleNamespace(timestamp=lambda _t=ts: _t),
        chat=SimpleNamespace(
            id=chat_id, type=SimpleNamespace(name="PRIVATE"),
            first_name="张", last_name="三", username="zhangsan",
            phone_number="", title=None),
    )


@pytest.fixture()
def wired_store(tmp_path):
    """真 InboxStore + 真 sink，带 auto-draft 探针（``_new_inbound_cbs`` 必须零调用）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cbs: list = []
    store.register_new_inbound_cb(lambda conv, text: cbs.append((conv, text)))
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    try:
        yield store, cbs
    finally:
        pb.register_inbox_sink(None)


# ── 1. 安全红线：outgoing 绝不进 AI 管道 ─────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("mirror_outgoing", [True, False])
async def test_outgoing_never_reaches_process_message(mirror_outgoing):
    """最重要的一条：无论镜像开关如何，我方自己发的消息都不得走 AI 全链。"""
    tc = _mk_tc(mirror_outgoing=mirror_outgoing)
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=638, outgoing=True))

    assert tc._process_message.await_count == 0


# ── 2/3. 镜像开关语义：开则镜像，关则不镜像——但都必须 return ────────────────

@pytest.mark.asyncio
async def test_flag_on_mirrors_outgoing():
    tc = _mk_tc(mirror_outgoing=True)
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    msg = _mk_msg(mid=639, outgoing=True)
    await handler(tc.client, msg)

    assert tc._mirror_outgoing_message.call_count == 1
    chat_arg, msg_arg, catchup_arg = tc._mirror_outgoing_message.call_args[0]
    assert chat_arg is msg.chat and msg_arg is msg
    # catchup 传 0：实时消息 mts >= _boot_timestamp 恒成立，after_boot 分支必过
    assert catchup_arg == 0


@pytest.mark.asyncio
async def test_flag_off_skips_without_mirroring():
    """关掉镜像只是不落工作台，**不是**放行进 AI 管道（这里曾是唯一的守卫缺口）。"""
    tc = _mk_tc(mirror_outgoing=False)
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=640, outgoing=True))

    assert tc._mirror_outgoing_message.call_count == 0
    assert tc._process_message.await_count == 0


@pytest.mark.asyncio
async def test_mirror_flag_is_read_live_per_message():
    """现读配置：热重载后下一条消息即换行为，无需重启（对齐轮询侧写法）。"""
    flag = {"on": False}
    tc = _mk_tc()
    tc.config = SimpleNamespace(
        get_telegram_config=lambda: {
            "process_private": True,
            "poll_fallback": {"mirror_outgoing": flag["on"]},
        },
        get=lambda _k, _d=None: _d if _d is not None else {},
    )
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=641, outgoing=True))
    assert tc._mirror_outgoing_message.call_count == 0

    flag["on"] = True
    await handler(tc.client, _mk_msg(mid=642, outgoing=True))
    assert tc._mirror_outgoing_message.call_count == 1


# ── 4. 守卫在限流器之前：出站消息不耗令牌、不触发自动封禁 ────────────────────

@pytest.mark.asyncio
async def test_outgoing_does_not_consume_rate_limit_or_autoban():
    tc = _mk_tc(mirror_outgoing=True)
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    for mid in (643, 644, 645):
        await handler(tc.client, _mk_msg(mid=mid, outgoing=True))

    assert tc._rate_limiter.allow_calls == []      # 令牌桶零消耗
    assert tc._rate_limiter.ban_checks == []       # 绝不会把自己封了
    assert tc._rate_limiter.banned_checks == []


# ── 5. 守卫在 claim 之前：钉死「镜像上线即死」的机制 ─────────────────────────

@pytest.mark.asyncio
async def test_outbound_branch_does_not_claim_before_mirroring():
    """handler 不得抢先 claim——``_mirror_outgoing_message`` 内部要用同一把 claim。

    这正是修复前「2452 条轮询兜底日志、镜像 0 条」的根因，直接钉住。
    """
    trace: list = []
    tc = _mk_tc(mirror_outgoing=True, trace=trace)
    tc._mirror_outgoing_message = MagicMock(
        side_effect=lambda *_a: trace.append(("mirror",)) or True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=646, outgoing=True))

    assert trace == [("mirror",)]                  # mirror 之前一次 claim 都没有


@pytest.mark.asyncio
async def test_realtime_mirror_actually_lands_in_store(wired_store):
    """端到端：走真 ``_mirror_outgoing_message``，工作台真的多出一条 ``direction="out"``。

    修复前这条必红（handler 先 claim → 镜像内部 claim 失败 → 静默返回 False）。
    """
    store, cbs = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=647, outgoing=True, text="好的，马上处理"))

    rows = store.list_messages(f"telegram:{ACCOUNT}:{PEER}")
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert rows[0]["platform_msg_id"] == "647"
    assert cbs == []                               # auto-draft 零调用（红线）
    assert tc._process_message.await_count == 0


@pytest.mark.asyncio
async def test_realtime_mirror_covers_every_message_not_just_the_last(wired_store):
    """覆盖度升级：老板手机连发 3 条 → 3 条都进工作台。

    轮询侧每轮只看 ``top_message``，最多镜像每会话最后一条；实时路径每条都过。
    """
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    handler = _private_handler(tc)

    for mid, text in ((651, "在的"), (652, "稍等"), (653, "已经安排了")):
        await handler(tc.client, _mk_msg(mid=mid, outgoing=True, text=text))

    rows = store.list_messages(f"telegram:{ACCOUNT}:{PEER}")
    assert [r["text"] for r in rows] == ["在的", "稍等", "已经安排了"]
    assert {r["direction"] for r in rows} == {"out"}


# ── 6/7. 系统会话：Saved Messages / Telegram 服务号既不处理也不镜像 ──────────

@pytest.mark.asyncio
async def test_saved_messages_neither_processed_nor_mirrored():
    """收藏夹（chat.id == 本账号 id）：pyrogram 明确「发给自己的不算 outgoing」，
    没这道闸就会变成「AI 在老板的收藏夹里回复他」。笔记不是客户会话，也不该镜像。"""
    tc = _mk_tc(mirror_outgoing=True)
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=648, outgoing=False, chat_id=SELF_ID,
                                     from_id=SELF_ID, text="记一下：明天寄样品"))

    assert tc._process_message.await_count == 0
    assert tc._mirror_outgoing_message.call_count == 0
    assert tc._rate_limiter.allow_calls == []


@pytest.mark.asyncio
async def test_telegram_service_chat_neither_processed_nor_mirrored():
    """777000 = Telegram 官方服务号（登录验证码）。让 AI 对它回话既荒唐又白烧 token。"""
    tc = _mk_tc(mirror_outgoing=True)
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=649, outgoing=False,
                                     chat_id=TELEGRAM_SERVICE_CHAT_ID,
                                     from_id=TELEGRAM_SERVICE_CHAT_ID,
                                     text="登录验证码: 12345"))

    assert tc._process_message.await_count == 0
    assert tc._mirror_outgoing_message.call_count == 0


@pytest.mark.asyncio
async def test_saved_messages_not_mirrored_even_if_marked_outgoing():
    """防隐私外泄：万一某天 Saved Messages 真带上 ``outgoing=True``（服务端行为随 layer
    变动），也绝不能把老板的私人笔记镜像进客户工作台——故系统会话判在出站分支之前。"""
    tc = _mk_tc(mirror_outgoing=True)
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=650, outgoing=True, chat_id=SELF_ID,
                                     from_id=SELF_ID, text="私人备忘"))

    assert tc._mirror_outgoing_message.call_count == 0
    assert tc._process_message.await_count == 0


def test_is_system_chat_survives_missing_user_info():
    """登录未完成（user_info=None）时只判服务号，绝不因身份未就绪而崩。"""
    tc = _mk_tc()
    tc.user_info = None

    assert tc._is_system_chat(TELEGRAM_SERVICE_CHAT_ID) is True
    assert tc._is_system_chat(PEER) is False
    assert tc._is_system_chat(None) is False
    assert tc._is_system_chat("not-an-int") is False


# ── 8. 回归保护：正常入站客户消息行为完全不变 ────────────────────────────────

@pytest.mark.asyncio
async def test_normal_inbound_still_flows_into_pipeline():
    """对照组：普通客户消息照旧限流 → claim → 进 AI 管道（证明上面的断言不是整条路都死了）。"""
    trace: list = []
    tc = _mk_tc(mirror_outgoing=True, trace=trace)
    tc._mirror_outgoing_message = MagicMock(return_value=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=660, outgoing=False, text="在吗"))

    assert tc._process_message.await_count == 1
    assert tc._rate_limiter.allow_calls == [("123", PEER)]   # 限流照常生效
    assert trace == [("claim", PEER, 660)]                   # 去重照常登记
    assert tc._mirror_outgoing_message.call_count == 0


@pytest.mark.asyncio
async def test_inbound_dedup_still_blocks_replay():
    """入站去重未被守卫改动：同一条消息重投只处理一次。"""
    tc = _mk_tc(mirror_outgoing=True)
    handler = _private_handler(tc)

    await handler(tc.client, _mk_msg(mid=661, outgoing=False, text="在吗"))
    await handler(tc.client, _mk_msg(mid=661, outgoing=False, text="在吗"))

    assert tc._process_message.await_count == 1


# ── 纯方法契约（守卫抽出来后可独立断言，位置约束由上面的 handler 级测试保证）──

@pytest.mark.parametrize(
    "outgoing,chat_id,mirror_flag,expected",
    [
        (False, PEER, True, (False, False)),                       # 普通入站 → 放行
        (True, PEER, True, (True, True)),                          # 出站 + 开 → 跳过并镜像
        (True, PEER, False, (True, False)),                        # 出站 + 关 → 只跳过
        (False, SELF_ID, True, (True, False)),                     # 收藏夹 → 跳过不镜像
        (True, SELF_ID, True, (True, False)),                      # 收藏夹（即使标 outgoing）
        (False, TELEGRAM_SERVICE_CHAT_ID, True, (True, False)),    # 官方服务号
    ],
)
def test_should_skip_as_outbound_matrix(outgoing, chat_id, mirror_flag, expected):
    tc = _mk_tc(mirror_outgoing=mirror_flag)
    assert tc._should_skip_as_outbound(
        _mk_msg(mid=670, outgoing=outgoing, chat_id=chat_id)) == expected


def test_config_read_failure_still_skips_outgoing():
    """读配置炸了 → 不镜像，但仍然跳过：AI 红线优先于镜像功能。"""
    tc = _mk_tc()

    def _boom():
        raise RuntimeError("config unavailable")

    tc.config = SimpleNamespace(get_telegram_config=_boom,
                                get=lambda _k, _d=None: _d)

    assert tc._should_skip_as_outbound(
        _mk_msg(mid=671, outgoing=True)) == (True, False)
