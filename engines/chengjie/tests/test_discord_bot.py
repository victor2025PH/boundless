"""Discord Bot 接入门禁（2026-08-02）。

**不装 discord.py 也必须全绿**：本文件只测纯函数与「用假对象喂进去」的路径，
worker 的 ``__init__`` 与全部纯函数都不 import discord —— 这不是为了测试方便，
而是产品要求（未装依赖时整条 Discord 链要优雅缺席，不能把主进程启动带崩）。

重点守三类东西：

1. **平台事实翻译得对不对**：2000 字上限、附件 24h 过期、不能主动私聊陌生人。
   这三条错一条就是线上事故（半句话、死图、403 风暴）。
2. **入站过滤的三条不变量**：自己发的不收、其他 Bot 不收、频道按档收。
   漏第一条＝自问自答死循环，漏第二条＝两个 Bot 无限对轰。
3. **接线不掉**：编排器注册、能力矩阵有行、readiness 会说人话、主动触达排除。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from src.integrations import discord_bot_worker as W
from src.integrations import discord_bot_login as L


# ─────────────────── 长文本切分（2000 字硬上限） ───────────────────

def test_split_short_text_is_untouched():
    assert W.split_discord_text("你好") == ["你好"]
    assert W.split_discord_text("x" * W.MAX_MESSAGE_LEN) == ["x" * W.MAX_MESSAGE_LEN]


def test_split_empty_returns_nothing():
    """空文本返回 ``[]`` 而不是 ``[""]`` —— 后者会真的发一条空消息（API 还会 400）。"""
    for empty in ("", None, "   ", "\n\n"):
        assert W.split_discord_text(empty) == []


def test_split_respects_limit_and_loses_nothing():
    text = ("这是一段很长的话。" * 400)          # 远超 2000
    parts = W.split_discord_text(text)
    assert len(parts) > 1
    assert all(len(p) <= W.MAX_MESSAGE_LEN for p in parts)
    # 内容不丢：拼回去（切点吃掉的是空白）后与原文去空白一致
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_split_prefers_sentence_boundary_over_hard_cut():
    """切点必须落在句末，而不是把词拦腰砍断——两条断句比分两条本身更像机器人。"""
    head = "甲" * 1900
    parts = W.split_discord_text(head + "。" + "乙" * 500)
    assert parts[0].endswith("。")


def test_split_falls_back_to_hard_cut_when_no_separator():
    """一整块无分隔符的长串（如 base64）仍必须切开，不能因为找不到切点就超限。"""
    parts = W.split_discord_text("a" * 5000)
    assert len(parts) == 3 and all(len(p) <= W.MAX_MESSAGE_LEN for p in parts)


def test_split_ignores_too_early_separator():
    """靠太前的切点宁可不用：否则一条长消息会被切成一堆碎片。"""
    parts = W.split_discord_text("短句。" + "丙" * 3000)
    assert len(parts[0]) > W.MAX_MESSAGE_LEN * 0.4


# ─────────────────── chat_key ───────────────────

@pytest.mark.parametrize("raw,expect", [
    ("dm:123", ("dm", 123)),
    ("ch:456", ("ch", 456)),
    ("789", ("raw", 789)),          # 裸 id：不知是人是频道，调用方两头试
    ("", ("", 0)),
    ("dm:abc", ("", 0)),
    ("dm:", ("", 0)),
    (None, ("", 0)),
])
def test_parse_chat_key(raw, expect):
    assert W.parse_chat_key(raw) == expect


def test_chat_key_builders_roundtrip():
    assert W.parse_chat_key(W.dm_chat_key(42)) == ("dm", 42)
    assert W.parse_chat_key(W.channel_chat_key(42)) == ("ch", 42)


# ─────────────────── 附件分类 ───────────────────

@pytest.mark.parametrize("name,ct,kind", [
    ("a.jpg", "", "image"),
    ("a.PNG", "", "image"),                       # 大小写不敏感
    ("a.mp4", "", "video"),
    ("voice-message.ogg", "", "voice"),
    ("x.pdf", "", "file"),
    ("noext", "image/webp", "image"),             # 无扩展名时看 content_type
    ("noext", "audio/mpeg", "voice"),
    ("", "", "file"),
])
def test_attachment_kind(name, ct, kind):
    got, ext = W.attachment_kind(name, ct)
    assert got == kind
    assert ext.startswith("."), "扩展名要带点，直接用于拼落盘文件名"


# ─────────────────── 入站过滤三条不变量 ───────────────────

def test_never_ingest_own_messages():
    """自己发的永不收：send() 已镜像过一次，再收＝重复 + 自问自答死循环。"""
    assert not W.should_ingest(is_dm=True, is_self=True, author_is_bot=True,
                               mentioned=True, guild_mode="all")


def test_other_bots_ignored_by_default():
    """两个 Bot 互相触发自动回复＝无限对轰，本仓最贵的事故形态。"""
    assert not W.should_ingest(is_dm=True, is_self=False, author_is_bot=True,
                               mentioned=False)
    # 显式允许时才收（有客户真的用 Bot 做中转）
    assert W.should_ingest(is_dm=True, is_self=False, author_is_bot=True,
                           mentioned=False, ignore_bots=False)


def test_dm_always_ingested_regardless_of_guild_mode():
    for gm in ("mention", "all", "off"):
        assert W.should_ingest(is_dm=True, is_self=False, author_is_bot=False,
                               mentioned=False, guild_mode=gm), gm


@pytest.mark.parametrize("gm,mentioned,expect", [
    ("mention", True, True),
    ("mention", False, False),     # 默认档：没 @ 我就不进收件箱
    ("all", False, True),
    ("off", True, False),          # 完全不做服务器频道，@ 了也不收
    ("", False, False),            # 空值回落 mention 档
    ("MENTION", True, True),       # 大小写不敏感
])
def test_guild_channel_modes(gm, mentioned, expect):
    assert W.should_ingest(is_dm=False, is_self=False, author_is_bot=False,
                           mentioned=mentioned, guild_mode=gm) is expect


# ─────────────────── payload 归一 ───────────────────

def _author(uid=1001, name="alice", display="Alice", bot=False):
    return types.SimpleNamespace(
        id=uid, name=name, display_name=display, global_name=display, bot=bot,
        display_avatar=types.SimpleNamespace(url="https://cdn/x.png"))


def _msg(*, guild=None, channel=None, content="hi", mid=555):
    return types.SimpleNamespace(
        id=mid, content=content, author=_author(), guild=guild,
        channel=channel or types.SimpleNamespace(id=777, name="general"),
        created_at=None, attachments=[], mentions=[])


def test_dm_payload_keys_on_the_user_not_the_channel():
    """私聊会话键必须是**用户** id。

    DM 频道 id 对 Bot 不是稳定入口（没缓存时要先 create_dm 才拿得到），拿它当会话键
    会导致重启后回不去同一个会话；用户 id 则永远能 ``fetch_user`` → ``create_dm``。
    """
    p = W.discord_message_payload(_msg(), "botacct")
    assert p["chat_key"] == "dm:1001"
    assert p["platform"] == "discord" and p["direction"] == "in"
    assert p["source"]["chat_type"] == "private"
    assert p["mentioned"] is True          # 私聊天然算点名
    assert p["avatar_url"] == "https://cdn/x.png"


def test_guild_payload_keys_on_the_channel_and_splits_sender():
    guild = types.SimpleNamespace(name="我的服务器")
    p = W.discord_message_payload(_msg(guild=guild), "botacct", mentioned=True)
    assert p["chat_key"] == "ch:777"
    assert p["source"]["chat_type"] == "group"
    assert p["name"] == "我的服务器 #general"
    # 「谁说的」与「哪个会话」在群里是两件事
    assert p["sender_id"] == "1001" and p["sender_name"] == "Alice"
    assert p["mentioned"] is True


def test_guild_payload_not_mentioned():
    p = W.discord_message_payload(
        _msg(guild=types.SimpleNamespace(name="S")), "a", mentioned=False)
    assert p["mentioned"] is False


def test_payload_carries_media_fields():
    """媒体字段必须原样带下去——不带就等于客户发的图对 AI 不存在。"""
    p = W.discord_message_payload(_msg(), "a", media_type="image",
                                  media_ref="/static/x.jpg")
    assert (p["media_type"], p["media_ref"]) == ("image", "/static/x.jpg")


def test_group_dm_keys_on_the_channel_not_the_speaker():
    """群组私信没有 guild，但**不是** 1:1。

    按发言人建 `dm:` 键会把一个群聊裂成 N 个独立会话，而且回复会私发给某个人
    而不是发回群里——对方看到的是「我在群里问，它私聊回我」。
    """
    ch = types.SimpleNamespace(id=888, name="小群",
                               recipients=[object(), object()])
    p = W.discord_message_payload(_msg(channel=ch), "a")
    assert p["chat_key"] == "ch:888"
    assert p["source"]["chat_type"] == "group"
    assert p["sender_name"] == "Alice", "群里的发言人不能被会话名顶掉"
    assert p["mentioned"] is False, "被拉进群聊 ≠ 每句话都在跟 Bot 说"


def test_one_on_one_dm_still_keys_on_the_user():
    """1:1 的 DMChannel 是单数 recipient，不能被群组私信的判据误伤。"""
    ch = types.SimpleNamespace(id=888, name=None, recipient=object())
    p = W.discord_message_payload(_msg(channel=ch), "a")
    assert p["chat_key"] == "dm:1001"
    assert p["source"]["chat_type"] == "private"


def test_forbidden_detail_separates_dm_block_from_channel_permission():
    """两类 403 的处置动作完全不同，文案混用会把运营带去错误方向。

    50007＝去引导对方加服务器/开 DM；50013 等＝去改频道权限或重邀 Bot。
    """
    dm = W.forbidden_result("x", chat_key="dm:1", code=W.DM_FORBIDDEN_CODE)
    chan = W.forbidden_result("x", chat_key="ch:1", code=50013)
    assert dm["detail"] == W.FORBIDDEN_DETAIL
    assert chan["detail"] == W.FORBIDDEN_CHANNEL_DETAIL
    # 拿不到业务码时退到会话类型判断
    assert W.forbidden_result(chat_key="ch:1")["detail"] == W.FORBIDDEN_CHANNEL_DETAIL
    assert W.forbidden_result(chat_key="dm:1")["detail"] == W.FORBIDDEN_DETAIL


def test_payload_survives_the_real_inbox_sink(tmp_path):
    """端到端钉死「payload 的每个键都是 sink 认识的形参」。

    生产 sink 是 ``lambda m: ingest_incoming(store, **m)``，而 ``emit_incoming`` 把
    异常整个吞掉（debug 日志）——payload 多一个 sink 不认识的键 = **每条 Discord 消息
    静默消失**，线上表现是「Bot 在线、日志无红、就是收不到消息」，极难归因。
    故这里跑真 store + 真 ingest，不 mock。
    """
    from src.inbox.store import InboxStore
    from src.integrations.protocol_bridge import ingest_incoming

    store = InboxStore(tmp_path / "inbox.db")
    for m in (W.discord_message_payload(_msg(content="你好"), "botacct"),
              W.discord_message_payload(
                  _msg(guild=types.SimpleNamespace(name="S"), content="在群里"),
                  "botacct", mentioned=True)):
        assert ingest_incoming(store, **m), "Discord 消息没能落库"

    convs = store.list_conversations(platform="discord")
    got = {c["chat_key"]: c for c in convs}
    assert set(got) == {"dm:1001", "ch:777"}
    # 私聊/频道必须分流成不同 chat_type：群聊被当私聊会误入 SLA 告警与主动触达候选
    assert got["dm:1001"]["chat_type"] == "private"
    assert got["ch:777"]["chat_type"] == "group"


def test_payload_returns_none_on_malformed_message():
    """残缺对象一律返回 None（宁可丢一条，不可让 Gateway 回调抛异常断流）。"""
    assert W.discord_message_payload(types.SimpleNamespace(), "a") is None
    assert W.discord_message_payload(
        types.SimpleNamespace(channel=None, author=_author()), "a") is None


def test_payload_falls_back_to_username_when_no_display_name():
    author = types.SimpleNamespace(id=9, name="bob", bot=False,
                                   display_avatar=None)
    p = W.discord_message_payload(
        types.SimpleNamespace(id=1, content="x", author=author, guild=None,
                              channel=types.SimpleNamespace(id=2),
                              created_at=None),
        "a")
    assert p["name"] == "bob" and p["avatar_url"] == ""


# ─────────────────── 出站：403 不是异常，是平台规则 ───────────────────

class Forbidden(Exception):
    """仿 ``discord.Forbidden``（真类带 ``status=403``，这里两个特征都给）。"""
    status = 403
    code = W.DM_FORBIDDEN_CODE


class _SubclassOfForbidden(Forbidden):
    """真实调用里拿到的常是子类/包装类——按类名精确匹配会在这里静默失手。"""


class _OnlyStatus403(Exception):
    """名字完全无关、只有 HTTP 状态：另一半判据要顶得住。"""
    status = 403


def test_is_forbidden_recognises_all_three_shapes():
    assert W.is_forbidden(Forbidden())
    assert W.is_forbidden(_SubclassOfForbidden())
    assert W.is_forbidden(_OnlyStatus403())
    assert not W.is_forbidden(RuntimeError("boom"))
    assert not W.is_forbidden(type("HTTPException", (Exception,),
                                   {"status": 500})())


_Forbidden = Forbidden          # 下面用例沿用旧名


class _FakeDest:
    def __init__(self, fail_at=None):
        self.id = 777
        self.sent = []
        self._fail_at = fail_at

    async def send(self, content=None, **kw):
        if self._fail_at is not None and len(self.sent) == self._fail_at:
            raise _Forbidden("Cannot send messages to this user")
        self.sent.append((content, kw))
        return types.SimpleNamespace(id=1000 + len(self.sent))


def _worker(dest):
    w = W.DiscordBotWorker({"account_id": "acct", "meta": {}}, {})
    async def _resolve(_ck):
        if isinstance(dest, Exception):
            raise dest
        return dest
    w._resolve_dest = _resolve          # type: ignore[assignment]
    return w


def test_send_forbidden_is_a_delivery_failure_not_an_exception():
    """403 是平台规则，不是故障。

    抛异常会让编排器把**健康**的账号标成 error 并触发重启风暴——那是把「这个人不让
    我私信」误报成「Bot 挂了」。
    """
    w = _worker(_Forbidden("50007"))
    res = asyncio.run(w.send("dm:1", "在吗"))
    assert res["delivered"] is False and res["error"] == "forbidden"
    # detail 恒为人话，平台原文（常常只是「50007」）另放 raw
    assert "私信" in res["detail"] and res["raw"] == "50007"


def test_send_splits_long_text_into_multiple_messages():
    dest = _FakeDest()
    res = asyncio.run(_worker(dest).send("dm:1", "长句。" * 900))
    assert res["delivered"] is True and res["parts"] > 1
    assert len(dest.sent) == res["parts"]
    assert all(len(c) <= W.MAX_MESSAGE_LEN for c, _ in dest.sent)


def test_send_empty_text_is_refused_before_touching_the_api():
    res = asyncio.run(_worker(_FakeDest()).send("dm:1", "   "))
    assert res == {"delivered": False, "error": "empty_text"}


def test_partial_send_counts_as_delivered():
    """多段中途被拒：已发的算数、剩下的丢弃。

    重发整条会让客户看到前半段两遍——「少半句」远好过「重复一屏」。
    """
    dest = _FakeDest(fail_at=1)
    res = asyncio.run(_worker(dest).send("dm:1", "长句。" * 900))
    assert res["delivered"] is True and res.get("partial") is True
    assert len(dest.sent) == 1


def test_send_media_missing_file_fails_loudly_not_silently():
    w = _worker(_FakeDest())
    res = asyncio.run(w.send_media("dm:1", media_path="/nope/x.jpg"))
    assert res["delivered"] is False and res["error"] == "media_missing"


def test_forbidden_result_shape():
    r = W.forbidden_result()
    assert r["delivered"] is False and r["error"] == "forbidden" and r["detail"]


# ─────────────────── 出站：文件过大同样是投递失败，不是故障 ───────────────────

class _TooLarge(Exception):
    """仿 ``discord.HTTPException`` 的 413（业务码 40005）。"""
    status = 413
    code = W.TOO_LARGE_CODE


def test_is_too_large_recognises_status_and_code():
    assert W.is_too_large(_TooLarge())
    assert W.is_too_large(type("X", (Exception,), {"status": 413})())
    assert W.is_too_large(type("X", (Exception,), {"code": W.TOO_LARGE_CODE})())
    assert not W.is_too_large(RuntimeError("boom"))
    assert not W.is_too_large(Forbidden())


def test_upload_limit_follows_the_server_boost_level_not_a_hardcoded_number():
    """加成过的服务器允许 50/100MiB。把上限写死会让客户「明明能发却发不了」。"""
    boosted = types.SimpleNamespace(guild=types.SimpleNamespace(
        filesize_limit=100 * 1024 * 1024))
    assert W.outbound_size_limit(boosted) == 100 * 1024 * 1024
    # 私聊没有 guild → 回落免费档
    assert W.outbound_size_limit(types.SimpleNamespace()) == W.DEFAULT_UPLOAD_LIMIT_BYTES


def test_oversize_upload_is_refused_locally_before_burning_bandwidth(tmp_path):
    """预检在**上传之前**——不然慢出口会把整条出站队列堵几分钟才换来一个 413。"""
    big = tmp_path / "big.mp4"
    big.write_bytes(b"x" * 2048)
    dest = _FakeDest()
    dest.guild = types.SimpleNamespace(filesize_limit=1024)
    res = asyncio.run(_worker(dest).send_media("ch:1", media_path=str(big)))
    assert res["delivered"] is False and res["error"] == "media_too_large"
    assert "上限" in res["detail"]
    assert dest.sent == [], "超限文件不该真的发出去"


@pytest.fixture
def stub_discord(monkeypatch):
    """本机不装 discord.py（门禁不能依赖它），但上传路径必须能跑到。"""
    mod = types.ModuleType("discord")
    mod.File = lambda path, filename=None: types.SimpleNamespace(  # type: ignore[attr-defined]
        path=path, filename=filename)
    monkeypatch.setitem(__import__("sys").modules, "discord", mod)
    return mod


def test_server_side_413_is_a_clean_failure_not_a_stack_trace(tmp_path, stub_discord):
    """预检漏算时以服务端为准：仍是一条投递失败，不能把栈抛给编排器。"""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)

    class _Rejecting(_FakeDest):
        async def send(self, content=None, **kw):
            raise _TooLarge("Request entity too large")

    res = asyncio.run(_worker(_Rejecting()).send_media("dm:1", media_path=str(f)))
    assert res["delivered"] is False and res["error"] == "media_too_large"


def test_normal_sized_media_still_goes_out(tmp_path, stub_discord):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    dest = _FakeDest()
    res = asyncio.run(_worker(dest).send_media("dm:1", media_path=str(f),
                                               caption="看这个"))
    assert res["delivered"] is True and len(dest.sent) == 1


# ─────────────────── 入站：谁在叫我们（mention 判据） ───────────────────

def _role(rid, default=False):
    return types.SimpleNamespace(id=rid, is_default=lambda: default)


def _bot_with_roles(*roles):
    return types.SimpleNamespace(roles=list(roles))


def test_direct_mention_is_a_summon():
    me = object()
    msg = types.SimpleNamespace(mentions=[me], role_mentions=[], guild=None)
    assert W.mention_signal(msg, me) is True


def test_role_ping_is_a_summon_too():
    """社群里召唤客服的常态是 ``@客服`` 而不是 ``@Bot``。

    漏掉它，运营开了 mention 档会发现「@客服没人理」，只能退回 all 把整个服务器的
    闲聊灌进收件箱——那才是真正的噪音来源。
    """
    me, support = object(), _role(42)
    guild = types.SimpleNamespace(me=_bot_with_roles(support))
    msg = types.SimpleNamespace(mentions=[], role_mentions=[support], guild=guild)
    assert W.mention_signal(msg, me) is True


def test_other_teams_role_ping_is_not_our_business():
    me = object()
    guild = types.SimpleNamespace(me=_bot_with_roles(_role(42)))
    msg = types.SimpleNamespace(mentions=[], role_mentions=[_role(99)], guild=guild)
    assert W.mention_signal(msg, me) is False


def test_everyone_ping_is_broadcast_not_summon():
    """@everyone 每个人都带默认角色。认了它 = 每次全员通知都进收件箱。"""
    me = object()
    everyone = _role(1, default=True)
    guild = types.SimpleNamespace(me=_bot_with_roles(everyone))
    msg = types.SimpleNamespace(mentions=[], role_mentions=[everyone],
                                guild=guild, mention_everyone=True)
    assert W.mention_signal(msg, me) is False


def test_at_everyone_plus_direct_ping_still_counts():
    """`@everyone @Bot 帮忙看下` —— 直接点名优先于广播过滤。"""
    me = object()
    everyone = _role(1, default=True)
    guild = types.SimpleNamespace(me=_bot_with_roles(everyone))
    msg = types.SimpleNamespace(mentions=[me], role_mentions=[everyone],
                                guild=guild, mention_everyone=True)
    assert W.mention_signal(msg, me) is True


def test_mention_signal_survives_missing_fields():
    """入站解析永远不能因为字段缺失而抛——抛了整条消息就丢了。"""
    assert W.mention_signal(types.SimpleNamespace(), object()) is False
    assert W.mention_signal(types.SimpleNamespace(mentions=[]), None) is False


# ─────────────────── 掉线要能被编排器看见（自愈的接缝） ───────────────────

def test_healthy_reports_false_for_every_shape_of_death():
    """编排器的退避重启已由 test_account_orchestrator 覆盖，它只认 ``healthy()``。

    所以 Discord 侧唯一要钉死的就是这个接缝：Gateway 死了必须**如实说 False**。
    这里若因为 ``latency`` 是 nan、client 半死等原因误报 True，Bot 会静默掉线到
    有人来问「怎么不回消息了」为止——自愈机器再好也救不了一个撒谎的探针。
    """
    w = W.DiscordBotWorker({"account_id": "a", "meta": {}}, {})
    assert asyncio.run(w.healthy()) is False, "还没连接就不该说健康"

    w.client = types.SimpleNamespace(is_closed=lambda: True, is_ready=lambda: True)
    assert asyncio.run(w.healthy()) is False, "连接已关闭"

    w.client = types.SimpleNamespace(is_closed=lambda: False, is_ready=lambda: False)
    assert asyncio.run(w.healthy()) is False, "握手没完成（token 失效常停在这）"

    def _boom():
        raise RuntimeError("client 内部炸了")
    w.client = types.SimpleNamespace(is_closed=_boom, is_ready=lambda: True)
    assert asyncio.run(w.healthy()) is False, "探针自身出错也算不健康，不能装死"

    w.client = types.SimpleNamespace(is_closed=lambda: False, is_ready=lambda: True)
    assert asyncio.run(w.healthy()) is True


# ─────────────────── worker 生命周期（无依赖也要可构造） ───────────────────

def test_worker_constructs_without_discord_installed():
    """能力矩阵靠**构造实例**内省能力（``_instantiate``）——构造不了整行就变 `?`。"""
    w = W.DiscordBotWorker({"account_id": "a", "meta": {}}, {})
    assert w.state == "stopped"
    assert hasattr(w, "send") and hasattr(w, "send_media")
    assert hasattr(w, "send_chat_action")
    assert not hasattr(w, "mark_read"), \
        "Discord 已读态是用户端私有，Bot 无 API 可写——不能装作支持"


def test_start_without_token_says_which_two_places_to_look():
    w = W.DiscordBotWorker({"account_id": "a", "meta": {}}, {})
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(w.start())
    assert "bot_token" in str(ei.value)


def test_token_prefers_account_meta_over_config():
    """一号一 token：注册表里存的是这个账号自己的 Bot，不能被全局配置盖掉。"""
    cfg = {"platform_login": {"discord": {"bot_token": "GLOBAL"}}}
    w = W.DiscordBotWorker({"account_id": "a", "meta": {"bot_token": "MINE"}}, cfg)
    assert w._token() == "MINE"
    w2 = W.DiscordBotWorker({"account_id": "a", "meta": {}}, cfg)
    assert w2._token() == "GLOBAL"


def test_oversize_attachment_keeps_the_type_so_the_message_is_not_blank(tmp_path):
    """超限只跳过**下载**，类型必须留着（与 ``download_tg_media`` 同口径）。

    丢掉类型 → 「只有一张大图、没配文字」的消息落成一条彻底空白的消息，
    坐席看到会话冒泡、点开什么都没有，比显示「[图片]（未下载）」糟得多。
    """
    cfg = {"platform_login": {"discord": {"max_media_bytes": 10}}}
    w = W.DiscordBotWorker({"account_id": "a", "meta": {}}, cfg)
    att = types.SimpleNamespace(filename="big.jpg", content_type="image/jpeg",
                                size=999_999)
    msg = types.SimpleNamespace(id=7, attachments=[att])
    kind, ref = asyncio.run(w._download_first_attachment(msg))
    assert (kind, ref) == ("image", ""), "类型丢了，消息会变成空白"


def test_status_is_safe_before_connect():
    st = W.DiscordBotWorker({"account_id": "a", "meta": {}}, {}).status()
    assert st["type"] == "discord_bot" and st["state"] == "stopped"
    assert st["guilds"] == 0 and st["latency_ms"] == 0.0


def test_explain_translates_the_two_first_time_traps():
    """首次接入必踩的两个坑要给「照着做」的一句话，不是异常类名。"""
    class PrivilegedIntentsRequired(Exception):
        pass

    class LoginFailure(Exception):
        pass

    assert "MESSAGE CONTENT INTENT" in W.DiscordBotWorker._explain(
        PrivilegedIntentsRequired())
    assert "Token" in W.DiscordBotWorker._explain(LoginFailure())


# ─────────────────── 登录 provider ───────────────────

def test_mask_token_never_leaks_the_whole_thing():
    tok = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GhIjKl.abcdefghijklmnop"
    masked = L.mask_token(tok)
    assert tok not in masked and "…" in masked
    assert len(masked) < 16
    assert L.mask_token("short") == "***"
    assert L.mask_token("") == ""


def test_guild_mode_defaults_and_rejects_garbage():
    assert L.guild_mode({}) == "mention"
    assert L.guild_mode({"platform_login": {"discord": {"guild_mode": "ALL"}}}) == "all"
    assert L.guild_mode(
        {"platform_login": {"discord": {"guild_mode": "wat"}}}) == "mention"


def test_bot_enabled_defaults_off_even_on_desktop():
    """Discord 必须运营自己去建 Bot、开 intent、填 token——桌面版默认开只会点亮一个
    必然失败的入口。"""
    assert L.bot_enabled({}) is False
    assert L.bot_enabled({"platform_login": {"discord": {"bot_enabled": True}}}) is True


def test_validate_token_classifies_failures_machine_readably(monkeypatch):
    """``reason`` 必须机器可读：只回一句人话，调用方无从判断该重试还是该换 token。"""
    async def fake(url, headers, timeout=15.0):
        assert headers["Authorization"].startswith("Bot ")
        return fake.code, fake.body
    monkeypatch.setattr(L, "_get_json", fake)

    fake.code, fake.body = 401, {}
    assert asyncio.run(L.validate_token("x"))["reason"] == "invalid_token"

    fake.code, fake.body = 429, {"message": "rate limited"}
    assert asyncio.run(L.validate_token("x"))["reason"] == "http_429"

    assert asyncio.run(L.validate_token(""))["reason"] == "empty_token"


def test_validate_token_network_error_is_retryable_not_invalid(monkeypatch):
    """网络挂了不能报成「token 无效」——运营会去重置一把好好的 token。"""
    async def boom(*_a, **_kw):
        raise OSError("dns")
    monkeypatch.setattr(L, "_get_json", boom)
    assert asyncio.run(L.validate_token("x"))["reason"] == "network"


def test_validate_token_success_shape(monkeypatch):
    async def ok(*_a, **_kw):
        return 200, {"id": "42", "username": "svcbot", "avatar": "abc"}
    monkeypatch.setattr(L, "_get_json", ok)
    res = asyncio.run(L.validate_token("x"))
    assert res["ok"] and res["account_id"] == "42" and res["name"] == "svcbot"
    assert res["avatar_url"].endswith("/42/abc.png")


def test_provider_refuses_without_token_and_says_why():
    prov = L.make_provider({})
    out = asyncio.run(prov(None, "discord", "protocol", "", {}))
    assert out["reason_code"] == "token_required"
    assert out.get("poll") is None, "没 token 就不该进轮询态（前端会一直转圈）"


def test_provider_fails_loudly_when_the_token_cannot_be_persisted(monkeypatch):
    """存不下 token 就不许报「已连接」。

    token 是这条链路的唯一产物；没落库 → 编排器起 worker 时取不到 → 线上表现是
    「显示已连接却一条消息都收不到」，而且没有任何线索指回登录环节。
    """
    async def ok(*_a, **_kw):
        return 200, {"id": "42", "username": "svcbot", "avatar": ""}
    monkeypatch.setattr(L, "_get_json", ok)
    monkeypatch.setattr(L, "is_discord_available", lambda: True)

    class _Reg:
        def upsert(self, *_a, **_kw):
            raise OSError("disk full")
    monkeypatch.setattr(L, "get_account_registry", lambda: _Reg())

    out = asyncio.run(L.make_provider({})(None, "discord", "protocol", "",
                                          {"bot_token": "T"}))
    assert out["reason_code"] == "registry_write_failed"
    assert out.get("poll") is None, "写库失败还给 poll = 前端会显示接入成功"


def test_provider_succeeds_even_if_persona_binding_fails(monkeypatch):
    """人设兜底是锦上添花，不能反过来拦住已经存好 token 的接入。"""
    async def ok(*_a, **_kw):
        return 200, {"id": "42", "username": "svcbot", "avatar": ""}
    monkeypatch.setattr(L, "_get_json", ok)
    monkeypatch.setattr(L, "is_discord_available", lambda: True)
    monkeypatch.setattr(L, "get_account_registry",
                        lambda: types.SimpleNamespace(upsert=lambda *a, **k: None))
    import src.ai.persona_voice as PV
    monkeypatch.setattr(PV, "ensure_account_default_persona",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))

    out = asyncio.run(L.make_provider({})(None, "discord", "protocol", "",
                                          {"bot_token": "T"}))
    assert callable(out.get("poll"))
    assert asyncio.run(out["poll"](None))["status"] == "authorized"


def test_maybe_register_is_gated_and_idempotent():
    L._reset_for_tests()
    try:
        assert L.maybe_register({}) is False          # 开关关 → 不注册
        cfg = {"platform_login": {"discord": {"bot_enabled": True}}}
        assert L.maybe_register(cfg) is True
        assert L.maybe_register(cfg) is True          # 幂等
        from src.integrations.platform_login import get_login_provider
        assert get_login_provider("discord", "protocol") is not None
    finally:
        L._reset_for_tests()


# ─────────────────── 接线（漏一处 = 平台悄悄不存在） ───────────────────

def test_platform_login_tables_know_discord():
    from src.integrations import platform_login as PL
    assert "discord" in PL.SUPPORTED_PLATFORMS
    assert PL.DEFAULT_PLATFORM_MODES["discord"]["default"] == "protocol"
    assert PL.PLATFORM_INSTRUCTIONS.get("discord")


def test_orchestrator_registers_discord_worker_when_switched_on():
    from src.integrations import account_orchestrator as AO
    if not L.is_discord_available():
        pytest.skip("本机未装 discord.py")
    AO.ensure_builtin_workers({"platform_login": {"discord": {"bot_enabled": True}}})
    assert "discord:protocol" in AO._WORKER_FACTORIES


def test_capability_matrix_has_a_discord_row_with_inbound_wired():
    from src.integrations import platform_capabilities as PC
    row = PC.capability_matrix({})["discord:protocol"]
    assert row["available"], "worker 构造不出来 → 整行变 ?（必须能无依赖构造）"
    assert row["caps"]["send_text"] and row["caps"]["send_media"]
    assert row["caps"]["mark_read"] is False        # 平台不给写
    assert row["recv_media"] is True, "入站没把 media_type 带下去 = 客户的图对 AI 不存在"


def test_proactive_outreach_excludes_discord():
    """主动触达在**规划阶段**就跳过 discord。

    不跳的唯一结果是每 tick 撞一轮 403，而连败还会喂给 ``_mark_bad_peer`` 把本来能
    被动接待的会话误拉黑——比不发更糟。
    """
    from src.integrations.platform_capabilities import proactive_outreach_allowed
    assert not proactive_outreach_allowed("discord")
    src = (__import__("pathlib").Path(
        W.__file__).resolve().parents[1] / "companion" / "proactive_topic.py"
    ).read_text(encoding="utf-8")
    assert "proactive_outreach_allowed" in src, \
        "闸门没接进规划器 —— 常量再对也拦不住"


def test_deferred_outbox_refuses_to_enqueue_proactive_discord():
    """deferred 队列是 care / reactivation / reaction_followup / voicecall 的共同出口。

    候选集各自来源不同（DB 行 / identity 优先级 / 配置白名单），逐条去堵必漏；
    堵在入队处一处覆盖全部。进来的下场是每次到期 drain 撞一次 403 → mark_failed
    → 下次再来，白烧配额还把失败率喂给告警。
    """
    from src.integrations.shared.deferred_outbox import DeferredOutboxStore
    st = DeferredOutboxStore(db_path=":memory:")
    kw = dict(account_id="a", chat_key="dm:1", reply_text="在吗", defer_until=0.0)
    assert st.enqueue(platform="discord", **kw) == 0
    assert st.enqueue(platform="telegram", **kw) > 0, "别误伤正常平台"


def test_care_dispatcher_closes_the_row_instead_of_retrying_discord():
    """光靠队列「不入队」不够：care schedule 行会留着每 tick 重来一次。

    必须在派发层 mark_skipped 真正销账，原因还要写进台账可查。
    """
    import inspect
    from src.contacts import care_dispatcher as CD
    src = inspect.getsource(CD.CareDispatcher._dispatch_one)
    assert "_proactive_allowed(platform)" in src
    assert "platform_no_proactive" in src
    assert not CD._proactive_allowed("discord")
    assert CD._proactive_allowed("telegram")


def test_batch_outreach_does_not_segment_discord():
    """批量触达不走 deferred 队列，队列那道闸拦不到它。"""
    from src.inbox.outreach_planner import OutreachPlanner, OutreachFilters

    class _Store:
        def list_conversations(self, **_kw):
            return [{"conversation_id": "discord:default:dm:1", "platform": "discord",
                     "account_id": "default", "chat_key": "dm:1", "last_ts": 0},
                    {"conversation_id": "telegram:default:9", "platform": "telegram",
                     "account_id": "default", "chat_key": "9", "last_ts": 0}]

        def get_conversation_meta(self, _cid):
            return {}

    plats = [t.platform for t in
             OutreachPlanner(_Store()).select_segment(OutreachFilters())]
    assert plats == ["telegram"]


def test_readiness_reports_missing_token_as_creds_not_disabled():
    """开了开关没填 token，要说「缺凭据」而不是「未启用」。

    报「未启用」会让运营回去反复检查那个**已经打开**的开关——本仓 protocol_doctor
    最忌的那类误导。
    """
    from src.integrations import platform_readiness as PR
    codes = [b["code"] for b in PR._discord_bot_blockers(
        {"platform_login": {"discord": {"bot_enabled": True}}})]
    assert PR.BLOCK_NOT_ENABLED not in codes
    assert PR.BLOCK_CREDS_MISSING in codes


def test_readiness_reports_switch_off_first():
    from src.integrations import platform_readiness as PR
    codes = [b["code"] for b in PR._discord_bot_blockers({})]
    assert codes[0] == PR.BLOCK_NOT_ENABLED
