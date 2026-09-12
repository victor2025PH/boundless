# -*- coding: utf-8 -*-
"""P1 语音链路补全门禁（2026-08-02）。

四条主线：
1. **会话列表预览形态标记**：媒体行正文已是干净转写/配文（[语音] 语义在 media_type），
   纯文本的会话列表须由 ``media_preview_text`` 补形态标记——空文本占位（旧行为）、
   已带 [xxx] 标记不叠加、干净正文加前缀；
2. **A 线入站语音留档**：归档必须在**转录之前**（转录失败时坐席更需要原音证据）、
   引用经队列透传进镜像媒体行、镜像正文去 [语音转录] 前缀（与 B 线回填同口径）；
3. **「谁的音色」徽标**：``persona_display_name`` best-effort 纯函数 + 三条语音链
   （A 线 voice_reply / B 线 autosend / 坐席手动）sender_name 接线；
4. **端到端**：ingest 落库后 messages.sender_name / conversations.last_text 如实。
"""

from pathlib import Path

from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ─────────────────── 1) 会话列表预览形态标记 ───────────────────

def test_media_preview_text_semantics():
    f = pb.media_preview_text
    assert f("", "voice") == "[语音]"                      # 空文本 → 占位（旧行为）
    assert f("行行行，这就来。", "voice") == "[语音] 行行行，这就来。"
    assert f("看我新裙子", "image") == "[图片] 看我新裙子"
    # 已带 [xxx] 标记（识别产物/存量旧行）不叠加，防「[图片] [图片内容] 描述」
    assert f("[图片内容] 一只猫", "image") == "[图片内容] 一只猫"
    assert f("[语音] 旧行念稿", "voice") == "[语音] 旧行念稿"
    assert f("  ", "voice") == "[语音]"                    # 纯空白按空处理


def test_ingest_voice_row_preview_and_clean_body(tmp_path):
    """入站语音媒体行：正文=干净转写（喂 AI/检索），预览=带 [语音] 标记（rail 表意）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(
        store, platform="telegram", account_id="a", chat_key="77",
        text="是啊是啊，你那里怎么样", media_type="voice",
        media_ref="/static/protocol_media/telegram/v1.ogg", direction="in")
    rows = store.list_messages(cid)
    assert rows[-1]["text"] == "是啊是啊，你那里怎么样"
    assert rows[-1]["media_type"] == "voice"
    convs = store.list_conversations(limit=10, platform="telegram")
    conv = next(c for c in convs if c["conversation_id"] == cid)
    assert conv["last_text"] == "[语音] 是啊是啊，你那里怎么样"


def test_ingest_media_only_placeholder_preserved(tmp_path):
    """空文本媒体行 → 预览仍是纯占位（与改动前逐字一致，防回归）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(
        store, platform="telegram", account_id="a", chat_key="78",
        text="", media_type="voice", direction="in")
    convs = store.list_conversations(limit=10, platform="telegram")
    conv = next(c for c in convs if c["conversation_id"] == cid)
    assert conv["last_text"] == "[语音]"


# ─────────────────── 2) A 线入站语音留档接线 ───────────────────

def _tg_src() -> str:
    return _src("src/client/telegram_client.py")


def test_inbound_voice_publishes_before_transcribe():
    """归档必须排在转录之前：转录失败/异常路径也要留下原音证据。"""
    src = _tg_src()
    dl = src.index("voice_file = await self._download_voice_file(message)")
    tr = src.index("transcribe_voice_message(", dl)
    assert "publish_outbound_media(" in src[dl:tr], (
        "入站语音归档不在「下载 → 转录」窗口内：排到转录之后会丢失异常路径的原音")


def test_inbound_voice_ref_threads_into_mirror():
    src = _tg_src()
    # 队列透传（同步段 → 异步段）
    assert "'voice_media_ref': voice_media_ref" in src
    # 异步段媒体分支：voice 行带归档引用
    i = src.index('elif getattr(message, "voice", None) or '
                  'getattr(message, "audio", None):')
    seg = src[i:i + 420]
    assert '_media_type = "voice"' in seg
    assert 'message_data.get("voice_media_ref")' in seg


def test_inbound_voice_mirror_text_strips_prefix():
    """镜像正文去 [语音转录] 前缀（与 B 线转录回填同口径）；AI 链内部 text 不动。

    钉锚更新（2026-08-23）：剥前缀逻辑已被 B29 emoji 镜像批收编进单源助手
    ``_inbound_mirror_text``（行为不变、纯函数化）——钉行为改钉助手本体 +
    调用点 + emit 消费，保护等价。
    """
    src = _tg_src()
    i = src.index("def _inbound_mirror_text(")
    seg = src[i:i + 900]
    assert "[语音转录] " in seg and 'media_type == "voice"' in seg, (
        "语音镜像剥前缀逻辑离开了 _inbound_mirror_text 单源助手")
    k = src.index("_mirror_text = _inbound_mirror_text(")
    j = src.index("self._emit_inbox(", k)
    assert "text=_mirror_text" in src[j:j + 220], "emit 必须用去前缀的 _mirror_text"
    # 行为级双保险（静态钉之外直接调用纯函数）
    mod = __import__("src.client.telegram_client",
                     fromlist=["_inbound_mirror_text"])
    fn = mod._inbound_mirror_text
    assert fn("voice", "[语音转录] 给我唱首歌吧", "") == "给我唱首歌吧"
    assert fn("", "普通文本", "") == "普通文本"


# ─────────────────── 3) 「谁的音色」显示名 ───────────────────

def test_persona_display_name_best_effort(monkeypatch):
    import src.utils.persona_manager as pm
    from src.ai.persona_voice import persona_display_name

    class _PM:
        def get_persona_by_id(self, pid):
            return {"name": "林小雨"} if pid == "p1" else None

    monkeypatch.setattr(pm.PersonaManager, "get_instance",
                        staticmethod(lambda: _PM()))
    assert persona_display_name("p1") == "林小雨"
    assert persona_display_name("nope") == ""
    assert persona_display_name("") == ""
    assert persona_display_name(None) == ""


def test_persona_display_name_never_raises(monkeypatch):
    import src.utils.persona_manager as pm

    def _boom():
        raise RuntimeError("pm down")

    monkeypatch.setattr(pm.PersonaManager, "get_instance", staticmethod(_boom))
    from src.ai.persona_voice import persona_display_name
    assert persona_display_name("p1") == ""


def test_voice_send_paths_carry_sender_name():
    """三条语音链 + 编排器出口都接了 sender_name（防「函数加了没人调」）。"""
    seg = _src("src/integrations/account_orchestrator.py")
    i = seg.index("async def send_media(")
    body = seg[i:seg.index("async def ", i + 10)]   # 到下一个方法为止＝整个函数体
    assert 'sender_name: str = ""' in body
    assert '_msrc["sender_name"] = str(sender_name)' in body   # 四期：与 sent_by 同一 source dict
    for rel, needle in [
        ("src/web/routes/unified_inbox_send_routes.py", "sender_name=_voice_sender_name"),
        ("src/inbox/autosend_helpers.py", "sender_name=_v_sender"),
        ("src/client/sender.py", "sender_name=_v_sender"),   # 分条
        ("src/client/sender.py", "sender_name=_vwho"),       # 单条两分支
    ]:
        assert needle in _src(rel), f"{rel} 缺 {needle}"


# ─────────────────── 4) 端到端：sender_name 落库 ───────────────────

def test_ingest_outbound_voice_row_carries_sender_name(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = pb.ingest_incoming(
        store, platform="telegram", account_id="a", chat_key="88",
        text="呸，还这么了是吧", media_type="voice",
        media_ref="/static/protocol_media/telegram/v2.ogg",
        direction="out", msg_id="901",
        source={"sender_name": "林小雨"})
    rows = store.list_messages(cid)
    assert rows[-1]["direction"] == "out"
    assert rows[-1]["sender_name"] == "林小雨"
    assert rows[-1]["media_ref"].endswith("v2.ogg")
