"""老板「用手机亲自回复」→ 实时同步进客服工作台（Telegram 轮询兜底出站镜像）。

背景事故：``_poll_inbound_once`` 原先见 ``outgoing`` 即 ``continue``，老板/运营在手机
App 上回过的客户，工作台仍停在客户那条「未回复」上 → 坐席再回一次、L2 autosend 也可能
再发一次，同一客户收到两三条重复回复。

本文件把该同步的**安全不变量**钉死：

- flag 关（默认）→ outgoing 仍被跳过，严格向后兼容；
- flag 开 → 落库 ``direction="out"``、``unread`` 不增加；
- **红线**：出站镜像绝不触发 auto-draft / 自动回复 / 智能分析 / 记忆累积
  （``_new_inbound_cbs`` 零调用 + ``maybe_auto_reply`` 零调用 + ``_process_message`` 零调用）；
- 去重：同 ``message.id`` 镜像两次（发送侧已镜像 + 轮询又看到）只有一条消息；
- 时间闸门：过老的 outgoing 消息不镜像（防重启回灌远古历史）；
- 入站行为完全不变（回归保护）。

媒体归档段（2026-08-04，修「手机发图坐席只见『[图片]』占位」）：

- ``mirror_outgoing_media`` 关（默认）→ 严格旧行为：占位文字、零下载 RPC、无媒体字段；
- 开 → ``media_type`` 恒结构化落库；策略放行的类型真下载得 ``media_ref``（坐席可见图）；
- 下载失败/超限/类型未放行 → 退化为 ref 空的媒体行，**消息不丢**；
- caption 与媒体并存时两者都保留（修旧路径「有配文的图整条丢媒体语义」的潜伏缺口）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.client.message_dedup import MessageDedup
from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb

ACCOUNT = "accTG"
PEER = 5433982810


# ── 假 client 装配（沿用 tests/test_message_dedup.py 的最小 TelegramClient 构造法）──

def _mk_tc(*, mirror_outgoing: bool, mirror_inbox: bool = True, media_cfg=None):
    from src.client.telegram_client import TelegramClient
    tc = TelegramClient.__new__(TelegramClient)
    _pf = {"mirror_outgoing": mirror_outgoing}
    if media_cfg is not None:
        _pf["mirror_outgoing_media"] = media_cfg
    tc.config = SimpleNamespace(get_telegram_config=lambda: {
        "process_private": True,
        "poll_fallback": _pf,
    })
    tc._rate_limiter = SimpleNamespace(enabled=False)
    tc._msg_dedup = MessageDedup()
    # 水位闸属性（_poll_inbound_once 入站路径必经；与 test_message_dedup 的
    # 最小构造同款——缺了会 AttributeError 被 per-dialog try 吞掉，表现为
    # 入站管道静默不触发）
    from src.client.message_dedup import PollWatermark as _PW
    tc._poll_watermark = _PW()
    tc._boot_timestamp = time.time() - 3600
    tc.user_info = SimpleNamespace(id=999)
    tc.account_id = ACCOUNT
    tc._mirror_inbox = mirror_inbox
    tc._process_message = AsyncMock()
    return tc   # tc.logger 是惰性 property，无需注入


def _mk_dialog(*, mid: int, outgoing: bool, text: str = "好的，马上处理",
               age_sec: float = 5.0, chat_id: int = PEER, **msg_kw):
    ts = time.time() - age_sec
    msg = SimpleNamespace(
        id=mid, message_id=mid, text=text, caption=None,
        voice=None, audio=None, photo=None, document=None, video=None,
        video_note=None, animation=None, sticker=None,
        outgoing=outgoing,
        # 出站消息的 from_user 就是本账号（与真实 pyrogram 一致）
        from_user=SimpleNamespace(id=999 if outgoing else 123, is_bot=False),
        date=SimpleNamespace(timestamp=lambda _t=ts: _t),
        chat=SimpleNamespace(id=chat_id),
    )
    for k, v in msg_kw.items():
        setattr(msg, k, v)
    chat = SimpleNamespace(
        id=chat_id, type=SimpleNamespace(name="PRIVATE"),
        first_name="张", last_name="三", username="zhangsan", phone_number="",
        title=None,
    )
    return SimpleNamespace(chat=chat, top_message=msg)


def _wire_dialogs(tc, dialogs):
    async def get_dialogs(limit=30):
        for d in dialogs[:limit]:
            yield d
    tc.client = SimpleNamespace(get_dialogs=get_dialogs)


@pytest.fixture()
def wired_store(tmp_path):
    """真 InboxStore + 真 sink（emit_incoming → ingest_incoming），带 auto-draft 探针。

    ``cbs`` 收集 ``_new_inbound_cbs`` 调用——出站镜像必须一次都不触发它
    （它就是 auto-draft / System Z 的入口）。
    """
    store = InboxStore(tmp_path / "inbox.db")
    cbs: list = []
    store.register_new_inbound_cb(lambda conv, text: cbs.append((conv, text)))
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    try:
        yield store, cbs
    finally:
        pb.register_inbox_sink(None)


def _conv_id() -> str:
    return f"telegram:{ACCOUNT}:{PEER}"


# ── flag 关：严格向后兼容 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_off_outgoing_still_skipped(wired_store):
    store, cbs = wired_store
    tc = _mk_tc(mirror_outgoing=False)
    _wire_dialogs(tc, [_mk_dialog(mid=638, outgoing=True)])

    await tc._poll_inbound_once(30, catchup=600)

    assert store.get_conversation(_conv_id()) is None   # 一条都没落库
    assert tc._process_message.await_count == 0
    assert cbs == []


# ── flag 开：镜像成 direction=out、不加未读 ───────────────────────────────────

@pytest.mark.asyncio
async def test_flag_on_mirrors_outgoing_as_out_without_unread(wired_store):
    store, cbs = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=638, outgoing=True, text="好的，马上处理")])

    await tc._poll_inbound_once(30, catchup=600)

    cid = _conv_id()
    rows = store.list_messages(cid)
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert rows[0]["text"] == "好的，马上处理"
    assert rows[0]["platform_msg_id"] == "638"      # 真实 MTProto message.id 作去重键
    conv = store.get_conversation(cid)
    assert conv["unread"] == 0                       # 自己发的不该点未读
    assert conv["display_name"] == "张 三"           # peer 身份取自 dialog.chat


@pytest.mark.asyncio
async def test_mirrored_ts_is_message_date_not_discovery_time(wired_store):
    """会话 last_ts 用消息真实时间，否则与客户入站消息错序（catchup 可达 600s）。"""
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=700, outgoing=True, age_sec=300)])

    await tc._poll_inbound_once(30, catchup=600)

    rows = store.list_messages(_conv_id())
    assert time.time() - rows[0]["ts"] > 200         # 是 message.date，不是「此刻」


@pytest.mark.asyncio
async def test_media_without_caption_mirrors_placeholder(wired_store):
    """媒体归档关（默认）：无正文的出站媒体 → 落「[图片]」占位、零下载、无媒体字段。

    这是 ``mirror_outgoing_media`` 缺省时的严格向后兼容行为。
    """
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=641, outgoing=True, text=None,
                                  photo=object())])

    await tc._poll_inbound_once(30, catchup=600)

    rows = store.list_messages(_conv_id())
    assert len(rows) == 1
    assert rows[0]["text"] == "[图片]"
    assert rows[0]["direction"] == "out"
    assert rows[0]["media_ref"] == ""                # 没为镜像多打一次下载 RPC
    assert rows[0]["media_type"] == ""               # 也不落结构化媒体字段（旧口径）


# ── 红线：出站镜像不触发任何自动化 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_outgoing_mirror_never_triggers_automation(wired_store, monkeypatch):
    """最重要的一条：镜像自己发的消息绝不能引出自动回复/自动草稿。

    三重探针：``_new_inbound_cbs``（auto-draft/System Z 入口）、``maybe_auto_reply``
    （protocol 自动回复主管道）、``_process_message``（A 线 AI 全链）。
    """
    store, cbs = wired_store

    async def _boom(payload):   # pragma: no cover - 触发即测试失败
        raise AssertionError("出站镜像绝不能调用 maybe_auto_reply")

    monkeypatch.setattr(pb, "maybe_auto_reply", _boom)

    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=642, outgoing=True)])
    await tc._poll_inbound_once(30, catchup=600)

    assert store.list_messages(_conv_id())           # 确实镜像成功了（探针有意义）
    assert cbs == []                                 # auto-draft 零调用
    assert tc._process_message.await_count == 0      # AI 全链零调用


@pytest.mark.asyncio
async def test_inbound_still_triggers_pipeline_while_flag_on(wired_store):
    """对照组：同一开关下入站消息照常进 AI 管道（证明红线断言不是因为整条路都死了）。"""
    _store, _cbs = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=643, outgoing=False, text="在吗")])

    await tc._poll_inbound_once(30, catchup=600)

    assert tc._process_message.await_count == 1


# ── 去重：发送侧已镜像过 + 轮询又看到 → 只有一条气泡 ──────────────────────────

@pytest.mark.asyncio
async def test_dedup_with_send_side_mirror_same_msg_id(wired_store):
    """模拟系统自己发的消息：编排器/A 线已用真实 message.id 镜像过一次，
    轮询随后又看到同一条 outgoing → 主键相同，第二次 INSERT OR IGNORE 无行插入。"""
    store, _ = wired_store
    # 发送侧镜像（account_orchestrator.send / sender._postsend_mirror_and_record 同口径）
    pb.emit_incoming(pb.make_message(
        platform="telegram", account_id=ACCOUNT, chat_key=str(PEER),
        text="好的，马上处理", direction="out", msg_id="644",
        ts=time.time() - 5,
    ))
    assert len(store.list_messages(_conv_id())) == 1

    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=644, outgoing=True, text="好的，马上处理")])
    await tc._poll_inbound_once(30, catchup=600)

    assert len(store.list_messages(_conv_id())) == 1   # 仍然只有一条，无重复气泡


@pytest.mark.asyncio
async def test_dedup_probe_is_not_vacuous(wired_store):
    """探测器自证：换成**另一条** message.id 就应该真的多出一条气泡。

    否则上面那条去重测试可能只是「镜像根本没跑」的假绿。
    """
    store, _ = wired_store
    pb.emit_incoming(pb.make_message(
        platform="telegram", account_id=ACCOUNT, chat_key=str(PEER),
        text="好的，马上处理", direction="out", msg_id="644",
        ts=time.time() - 5,
    ))
    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=999644, outgoing=True, text="另一句话")])
    await tc._poll_inbound_once(30, catchup=600)

    assert len(store.list_messages(_conv_id())) == 2


@pytest.mark.asyncio
async def test_dedup_across_poll_cycles(wired_store):
    """top_message 在下一条消息到来前一直是这条 outgoing → 每轮都不得重复镜像。"""
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=645, outgoing=True)])

    await tc._poll_inbound_once(30, catchup=600)
    await tc._poll_inbound_once(30, catchup=600)
    await tc._poll_inbound_once(30, catchup=600)

    assert len(store.list_messages(_conv_id())) == 1


@pytest.mark.asyncio
async def test_no_msg_id_is_not_mirrored(wired_store):
    """无 message.id ⇒ 去重不成立 → 宁可不镜像，也不制造重复气泡。"""
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    d = _mk_dialog(mid=0, outgoing=True)
    d.top_message.id = 0
    d.top_message.message_id = 0
    _wire_dialogs(tc, [d])

    await tc._poll_inbound_once(30, catchup=600)

    assert store.get_conversation(_conv_id()) is None


# ── 时间闸门：远古历史不回灌 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_time_gate_blocks_ancient_outgoing(wired_store):
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    # boot 前 1 小时 + 远超 catchup 窗（30 天前）
    _wire_dialogs(tc, [_mk_dialog(mid=646, outgoing=True, age_sec=30 * 86400)])

    await tc._poll_inbound_once(30, catchup=600)

    assert store.get_conversation(_conv_id()) is None


@pytest.mark.asyncio
async def test_time_gate_allows_within_catchup(wired_store):
    """宕机窗口内（boot 前但 catchup 内）老板手机回的那条仍要补进来。"""
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    tc._boot_timestamp = time.time() - 60          # 刚重启
    _wire_dialogs(tc, [_mk_dialog(mid=647, outgoing=True, age_sec=300)])

    await tc._poll_inbound_once(30, catchup=600)

    assert len(store.list_messages(_conv_id())) == 1


# ── 群聊：本轮刻意不覆盖（PRIVATE 过滤在前）─────────────────────────────────

@pytest.mark.asyncio
async def test_group_outgoing_not_mirrored(wired_store):
    """群里自己发的话不镜像：群噪音大、坐席作业在私聊线，且 PRIVATE 过滤本就在前。"""
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True)
    d = _mk_dialog(mid=648, outgoing=True, chat_id=-1001234567890)
    d.chat.type = SimpleNamespace(name="SUPERGROUP")
    _wire_dialogs(tc, [d])

    await tc._poll_inbound_once(30, catchup=600)

    assert store.get_conversation(f"telegram:{ACCOUNT}:-1001234567890") is None


# ── mirror_inbox 总闸：standalone 部署零影响 ─────────────────────────────────

@pytest.mark.asyncio
async def test_respects_mirror_inbox_master_switch(wired_store):
    """A 线未开收件箱镜像（standalone main.py）→ 不该凭空只造出站会话。"""
    store, _ = wired_store
    tc = _mk_tc(mirror_outgoing=True, mirror_inbox=False)
    _wire_dialogs(tc, [_mk_dialog(mid=649, outgoing=True)])

    await tc._poll_inbound_once(30, catchup=600)

    assert store.get_conversation(_conv_id()) is None


# ═════════ 媒体归档（mirror_outgoing_media，2026-08-04）═════════════════════
#
# 修「手机发图坐席只见『[图片]』占位」：开关开启后镜像行结构化落 media_type，
# 策略放行的类型经 download_tg_media 归档得 media_ref → 前端直接渲染 <img>。

from src.client.telegram_client import parse_mirror_outgoing_media_cfg  # noqa: E402

_URL = "/static/protocol_media/telegram/out_accTG_ab12cd.jpg"


# ── 配置解析纯函数 ───────────────────────────────────────────────────────────

def test_media_cfg_absent_or_false_is_disabled():
    assert parse_mirror_outgoing_media_cfg({}) == (False, 0, frozenset())
    assert parse_mirror_outgoing_media_cfg(
        {"mirror_outgoing_media": False}) == (False, 0, frozenset())
    assert parse_mirror_outgoing_media_cfg(None) == (False, 0, frozenset())
    assert parse_mirror_outgoing_media_cfg(
        {"mirror_outgoing_media": "yes"}) == (False, 0, frozenset())  # 非法类型=关


def test_media_cfg_bool_true_uses_builtin_defaults():
    on, max_bytes, kinds = parse_mirror_outgoing_media_cfg(
        {"mirror_outgoing_media": True})
    assert on is True
    assert max_bytes == 5 * 1024 * 1024
    assert kinds == frozenset({"image", "sticker", "voice"})


def test_media_cfg_dict_implies_enabled_and_overrides():
    on, max_bytes, kinds = parse_mirror_outgoing_media_cfg({
        "mirror_outgoing_media": {"max_mb": 10, "kinds": ["Image", " VOICE ", ""]},
    })
    assert on is True
    assert max_bytes == 10 * 1024 * 1024
    assert kinds == frozenset({"image", "voice"})    # 归一化小写、剔空串


def test_media_cfg_dict_explicit_disable_and_edge_values():
    assert parse_mirror_outgoing_media_cfg(
        {"mirror_outgoing_media": {"enabled": False, "max_mb": 10}},
    ) == (False, 0, frozenset())
    # max_mb: 0 = 不限体积（对齐 download_tg_media(max_bytes=0) 语义）
    on, max_bytes, _ = parse_mirror_outgoing_media_cfg(
        {"mirror_outgoing_media": {"max_mb": 0}})
    assert on is True and max_bytes == 0
    # max_mb 非法 → 回落默认；kinds 非法类型 → 回落默认；kinds: [] = 只结构化不下载
    on, max_bytes, kinds = parse_mirror_outgoing_media_cfg(
        {"mirror_outgoing_media": {"max_mb": "abc", "kinds": "image"}})
    assert on is True and max_bytes == 5 * 1024 * 1024
    assert kinds == frozenset({"image", "sticker", "voice"})
    on, _, kinds = parse_mirror_outgoing_media_cfg(
        {"mirror_outgoing_media": {"kinds": []}})
    assert on is True and kinds == frozenset()


# ── 行为：下载成功 → 坐席可见图 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_media_on_photo_gets_media_ref(wired_store, monkeypatch):
    """开关开 + 图片下载成功 → 行带 media_type/media_ref（前端据此渲染 <img>），
    正文保持空（媒体行新口径），会话预览自动补「[图片]」标记。"""
    store, cbs = wired_store
    dl = AsyncMock(return_value=("image", _URL))
    monkeypatch.setattr(pb, "download_tg_media", dl)

    tc = _mk_tc(mirror_outgoing=True, media_cfg=True)
    _wire_dialogs(tc, [_mk_dialog(mid=801, outgoing=True, text=None,
                                  photo=object())])
    await tc._poll_inbound_once(30, catchup=600)

    rows = store.list_messages(_conv_id())
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert rows[0]["media_type"] == "image"
    assert rows[0]["media_ref"] == _URL
    assert rows[0]["text"] == ""                     # 形态语义由 media_type 承载
    conv = store.get_conversation(_conv_id())
    assert conv["last_text"] == "[图片]"             # 预览列仍可读（media_preview_text）
    assert conv["unread"] == 0
    assert cbs == []                                 # 红线：媒体行同样零自动化
    # 下载参数：账号 + 体积上限透传
    assert dl.await_args.kwargs.get("max_bytes") == 5 * 1024 * 1024


@pytest.mark.asyncio
async def test_media_on_caption_keeps_both_text_and_media(wired_store, monkeypatch):
    """带配文的图：caption 与媒体字段都保留——修旧路径「有配文整条丢媒体语义」缺口。"""
    store, _ = wired_store
    monkeypatch.setattr(pb, "download_tg_media",
                        AsyncMock(return_value=("image", _URL)))

    tc = _mk_tc(mirror_outgoing=True, media_cfg=True)
    _wire_dialogs(tc, [_mk_dialog(mid=802, outgoing=True, text=None,
                                  caption="给你看下这个", photo=object())])
    await tc._poll_inbound_once(30, catchup=600)

    rows = store.list_messages(_conv_id())
    assert rows[0]["text"] == "给你看下这个"
    assert rows[0]["media_type"] == "image"
    assert rows[0]["media_ref"] == _URL


# ── 行为：下载失败/超限/未放行 → 退化为无归档媒体行，消息不丢 ────────────────

@pytest.mark.asyncio
async def test_media_on_download_failure_still_mirrors_structured_row(
        wired_store, monkeypatch):
    store, _ = wired_store
    monkeypatch.setattr(pb, "download_tg_media",
                        AsyncMock(return_value=("", "")))   # 硬失败

    tc = _mk_tc(mirror_outgoing=True, media_cfg=True)
    _wire_dialogs(tc, [_mk_dialog(mid=803, outgoing=True, text=None,
                                  photo=object())])
    await tc._poll_inbound_once(30, catchup=600)

    rows = store.list_messages(_conv_id())
    assert len(rows) == 1                            # 消息不丢
    assert rows[0]["media_type"] == "image"          # 类型来自 tg_media_meta，不依赖下载
    assert rows[0]["media_ref"] == ""


@pytest.mark.asyncio
async def test_media_on_oversize_keeps_type_without_ref(wired_store, monkeypatch):
    """超限：download_tg_media 返回 (kind, '') → 保类型、无归档（与入站口径一致）。"""
    store, _ = wired_store
    monkeypatch.setattr(pb, "download_tg_media",
                        AsyncMock(return_value=("image", "")))

    tc = _mk_tc(mirror_outgoing=True, media_cfg={"max_mb": 1})
    _wire_dialogs(tc, [_mk_dialog(mid=804, outgoing=True, text=None,
                                  photo=object())])
    await tc._poll_inbound_once(30, catchup=600)

    rows = store.list_messages(_conv_id())
    assert rows[0]["media_type"] == "image"
    assert rows[0]["media_ref"] == ""


@pytest.mark.asyncio
async def test_media_on_kind_not_allowed_skips_download(wired_store, monkeypatch):
    """kinds 未放行（视频）→ 不打下载 RPC，但仍结构化落 media_type（形态卡可见）。"""
    store, _ = wired_store
    dl = AsyncMock(return_value=("video", _URL))
    monkeypatch.setattr(pb, "download_tg_media", dl)

    tc = _mk_tc(mirror_outgoing=True, media_cfg=True)   # 默认 kinds 不含 video
    _wire_dialogs(tc, [_mk_dialog(mid=805, outgoing=True, text=None,
                                  video=object())])
    await tc._poll_inbound_once(30, catchup=600)

    rows = store.list_messages(_conv_id())
    assert rows[0]["media_type"] == "video"
    assert rows[0]["media_ref"] == ""
    assert dl.await_count == 0                       # 一次下载 RPC 都没打


@pytest.mark.asyncio
async def test_media_off_never_calls_download(wired_store, monkeypatch):
    """对照组：开关缺省 → 下载函数零调用（「默认零下载 RPC」不是靠运气）。"""
    _store, _ = wired_store
    dl = AsyncMock(return_value=("image", _URL))
    monkeypatch.setattr(pb, "download_tg_media", dl)

    tc = _mk_tc(mirror_outgoing=True)
    _wire_dialogs(tc, [_mk_dialog(mid=806, outgoing=True, text=None,
                                  photo=object())])
    await tc._poll_inbound_once(30, catchup=600)

    assert dl.await_count == 0


@pytest.mark.asyncio
async def test_media_publish_stats_recorded(wired_store, monkeypatch):
    """成败进 outbound_mirror_stats（ops「📤 出站媒体归档」卡的数据源）。"""
    from src.integrations.outbound_mirror_stats import get_outbound_mirror_stats
    stats = get_outbound_mirror_stats()
    stats.reset()
    store, _ = wired_store
    monkeypatch.setattr(pb, "download_tg_media",
                        AsyncMock(side_effect=[("image", _URL), ("", "")]))

    tc = _mk_tc(mirror_outgoing=True, media_cfg=True)
    _wire_dialogs(tc, [_mk_dialog(mid=807, outgoing=True, text=None, photo=object()),
                       _mk_dialog(mid=808, outgoing=True, text=None, photo=object(),
                                  chat_id=PEER + 1)])
    await tc._poll_inbound_once(30, catchup=600)

    d = stats.dump()
    assert d["total"] == 2 and d["fail"] == 1
    assert d["by_platform"]["telegram"]["total"] == 2
    stats.reset()                                    # 不给其他用例留脏计数
