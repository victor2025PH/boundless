"""LINE 媒体收发门禁（2026-07-31 补齐 LINE 号「只能收发文字」的缺口）。

覆盖三组不变量：

1. **纯函数**（contentType 归一 / 配置默认 / 贴纸 URL / 出站大类映射）——离线可跑，
   不依赖 okline。含两条**对钉**门禁：本模块镜像的 contentType 常量必须与
   ``okline.enums.ContentType`` 一致；入站大类必须落在 ``media_enrich`` 认得的
   kind 集合里（否则「下载了却不识别」＝白下载，AI 仍看不见）。
2. **出站两步编排**：占位消息 → OBS 上传；**上传失败必须撤回占位**（否则客户会看到
   一条永久点不开的破图，比什么都不发更糟）。
3. **``owns_media`` 契约**：``send_media`` 只在 ``platform_login.line.media.outbound``
   打开时才存在于 worker 上。编排器判据就是 ``hasattr(worker,"send_media")``，
   写成普通方法＝把自拍/相册/克隆语音/命理 K 线在 LINE 上一次性全放开。

全程 fake client + tmp 目录：无网络、无 Node、不写仓库 static 目录。
"""

import pytest

from src.integrations import line_media as LM
from src.integrations.account_orchestrator import LineProtocolWorker


# ─────────────────────────── 夹具 ───────────────────────────

@pytest.fixture(autouse=True)
def _static_to_tmp(tmp_path, monkeypatch):
    """把协议媒体落盘根目录重定向到 tmp。

    ``media_paths`` 真实写的是 ``src/web/static/protocol_media/``——那是被 git 跟踪的
    代码目录，测试往里写文件＝把仓库改脏（与「测试绝不能写仓库 config/」同一类事故）。
    """
    import src.integrations.protocol_bridge as PB
    monkeypatch.setattr(PB, "protocol_media_root", lambda: tmp_path / "static")


@pytest.fixture(autouse=True)
def _reset_stats():
    """进程级单例 → 每例前后清零，避免跨用例串味。"""
    from src.integrations.line_media_stats import get_line_media_stats
    get_line_media_stats().reset()
    yield
    get_line_media_stats().reset()


def _stats():
    from src.integrations.line_media_stats import get_line_media_stats
    return get_line_media_stats().dump()


def _msg(content_type, *, mid="m1", meta=None, text=""):
    return {
        "id": mid, "contentType": content_type, "text": text,
        "contentMetadata": meta if meta is not None else {},
    }


class _FakeObs:
    def __init__(self, data=b"BYTES", raise_on_download=False,
                 raise_on_upload=False):
        self._data = data
        self._raise_dl = raise_on_download
        self._raise_up = raise_on_upload
        self.downloads = []
        self.uploads = []

    def download_object(self, service, sid, oid, **kw):
        self.downloads.append((service, sid, oid))
        if self._raise_dl:
            raise RuntimeError("obs down")
        return self._data

    def upload_message_object(self, oid, data, **kw):
        self.uploads.append({"oid": oid, "size": len(data), **kw})
        if self._raise_up:
            raise RuntimeError("obs upload failed")
        return {"ok": True}


class _FakeApi:
    """鸭子类型的 OkLine：只实现本模块用到的那几个方法。"""

    def __init__(self, *, obs=None, send_id="sent-1", raise_on_unsend=False,
                 raise_on_text=False):
        self.obs = obs or _FakeObs()
        self._send_id = send_id
        self._raise_unsend = raise_on_unsend
        self._raise_text = raise_on_text
        self.sent_messages = []
        self.unsent = []
        self.texts = []
        self.checked = []

    def send_message(self, message):
        self.sent_messages.append(message)
        return {"id": self._send_id} if self._send_id else {}

    def get_encrypted_access_token(self, feature):
        return "enc-token"

    def unsend_message(self, msg_id):
        self.unsent.append(msg_id)
        if self._raise_unsend:
            raise RuntimeError("unsend failed")
        return {"ok": True}

    def send_text(self, to, text):
        self.texts.append((to, text))
        if self._raise_text:
            raise RuntimeError("text failed")
        return {"id": "t1"}

    def send_chat_checked(self, chat_mid, last_message_id):
        self.checked.append((chat_mid, last_message_id))
        return {"ok": True}


def _cfg(**media):
    return {"platform_login": {"line": {"media": media}}} if media else {}


# ─────────────────── 1. 纯函数 + 对钉 ───────────────────

def test_content_type_constants_match_okline():
    """本模块镜像的 contentType 必须与 okline 真枚举一致（升级漂移立刻点名）。

    刻意镜像而非直接 import：纯函数要能在没装 okline 的机器上跑门禁。
    """
    okline_enums = pytest.importorskip("okline.enums")
    CT = okline_enums.ContentType
    assert LM.CT_NONE == int(CT.NONE)
    assert LM.CT_IMAGE == int(CT.IMAGE)
    assert LM.CT_VIDEO == int(CT.VIDEO)
    assert LM.CT_AUDIO == int(CT.AUDIO)
    assert LM.CT_STICKER == int(CT.STICKER)
    assert LM.CT_FILE == int(CT.FILE)


def test_inbound_kinds_are_recognizable_downstream():
    """入站大类必须是 ``media_enrich`` 认得的 kind，否则下载白做（AI 仍看不见）。"""
    from src.inbox.media_enrich import (
        _IMAGE_KINDS, _VIDEO_KINDS, _VOICE_KINDS,
    )
    known = set(_IMAGE_KINDS) | set(_VOICE_KINDS) | set(_VIDEO_KINDS)
    for ct, (kind, ext) in LM.LINE_INBOUND_KINDS.items():
        assert ext.startswith("."), f"ct={ct} 扩展名要带点：{ext}"
        if kind == "document":
            continue  # 文件类不做内容识别，属预期
        assert kind in known, f"ct={ct} 的大类 {kind} 不被 media_enrich 识别"


@pytest.mark.parametrize("ct,expect", [
    (LM.CT_IMAGE, ("image", ".jpg")),
    (LM.CT_VIDEO, ("video", ".mp4")),
    (LM.CT_AUDIO, ("voice", ".m4a")),
    (LM.CT_STICKER, ("sticker", ".png")),
])
def test_media_meta_basic_kinds(ct, expect):
    assert LM.line_media_meta(_msg(ct)) == expect


def test_media_meta_text_and_unknown_are_none():
    assert LM.line_media_meta(_msg(LM.CT_NONE, text="你好")) is None
    assert LM.line_media_meta(_msg(999)) is None
    assert LM.line_media_meta(None) is None
    assert LM.line_media_meta({"contentType": "not-an-int"}) is None


def test_media_meta_file_keeps_real_extension():
    """文件沿用原名扩展名——落地还是 .pdf，坐席点开即认得。"""
    m = _msg(LM.CT_FILE, meta={"FILE_NAME": "报价单.pdf", "FILE_SIZE": "1024"})
    assert LM.line_media_meta(m) == ("document", ".pdf")
    # 无扩展名 / 畸形超长扩展名 → 回落 .bin
    assert LM.line_media_meta(_msg(LM.CT_FILE, meta={"FILE_NAME": "noext"})) == \
        ("document", ".bin")
    weird = _msg(LM.CT_FILE, meta={"FILE_NAME": "x." + "a" * 30})
    assert LM.line_media_meta(weird) == ("document", ".bin")


def test_declared_size():
    assert LM.line_declared_size(_msg(LM.CT_FILE, meta={"FILE_SIZE": "2048"})) == 2048
    assert LM.line_declared_size(_msg(LM.CT_IMAGE)) == 0          # 图不自报
    assert LM.line_declared_size(_msg(LM.CT_FILE, meta={"FILE_SIZE": "x"})) == 0
    assert LM.line_declared_size(None) == 0


def test_sticker_url_and_text_hint():
    m = _msg(LM.CT_STICKER, meta={"STKID": "51626494", "STKTXT": "開心"})
    assert LM.sticker_image_url(m).endswith("/51626494/android/sticker.png")
    assert LM.sticker_text_hint(m) == "開心"
    # 非贴纸 / 缺 STKID / 非数字 id → 空串（调用方回落占位）
    assert LM.sticker_image_url(_msg(LM.CT_IMAGE)) == ""
    assert LM.sticker_image_url(_msg(LM.CT_STICKER)) == ""
    assert LM.sticker_image_url(_msg(LM.CT_STICKER, meta={"STKID": "../evil"})) == ""
    assert LM.sticker_text_hint(_msg(LM.CT_STICKER)) == ""


def test_outbound_kind_mapping_and_ext_fallback():
    assert LM.outbound_kind("image")[0] == LM.CT_IMAGE
    assert LM.outbound_kind("voice")[0] == LM.CT_AUDIO
    assert LM.outbound_kind("video")[0] == LM.CT_VIDEO
    assert LM.outbound_kind("document")[0] == LM.CT_FILE
    # 图走 OBS 的 cat=original（LINE 客户端按它取原图）
    assert LM.outbound_kind("image")[2] == "original"
    # 大类缺失 → 按扩展名兜底（与 protocol_bridge 同一套判定，避免两处口径分裂）
    assert LM.outbound_kind("", ".ogg")[0] == LM.CT_AUDIO
    assert LM.outbound_kind("", ".png")[0] == LM.CT_IMAGE
    assert LM.outbound_kind("", ".xlsx")[0] == LM.CT_FILE
    assert LM.outbound_kind("") is None


def test_cfg_defaults_are_asymmetric_on_purpose():
    """收默认开（修缺陷、只读）、发默认关（新能力、级联媒体链、有账号风险）。"""
    c = LM.resolve_line_media_cfg(None)
    assert c["inbound"] is True
    assert c["outbound"] is False
    assert c["groups"] is False
    assert c["stickers"] is True
    assert c["inbound_max_bytes"] == LM.DEFAULT_INBOUND_MAX_BYTES


def test_cfg_overrides_and_dirty_values():
    c = LM.resolve_line_media_cfg(_cfg(
        inbound=False, outbound=True, groups=True, inbound_max_bytes=123))
    assert (c["inbound"], c["outbound"], c["groups"]) == (False, True, True)
    assert c["inbound_max_bytes"] == 123
    # 脏值/空值 → 回落默认，绝不抛
    dirty = LM.resolve_line_media_cfg(_cfg(
        inbound=None, inbound_max_bytes="oops", outbound_max_bytes=0))
    assert dirty["inbound"] is True
    assert dirty["inbound_max_bytes"] == LM.DEFAULT_INBOUND_MAX_BYTES
    assert dirty["outbound_max_bytes"] == LM.DEFAULT_OUTBOUND_MAX_BYTES
    assert LM.resolve_line_media_cfg({"platform_login": {"line": "nope"}})["inbound"] is True


# ─────────────────── 2. 入站下载 ───────────────────

def test_download_writes_static_and_returns_url(tmp_path):
    api = _FakeApi(obs=_FakeObs(data=b"JPEGDATA"))
    kind, url = LM.download_line_media(api, _msg(LM.CT_IMAGE, mid="M42"), "acct1")
    assert kind == "image"
    assert url.startswith("/static/protocol_media/line/")
    assert api.obs.downloads == [("talk", "m", "M42")]
    written = (tmp_path / "static" / "line").glob("*.jpg")
    assert [p.read_bytes() for p in written] == [b"JPEGDATA"]


def test_download_soft_fails_keep_kind_for_placeholder():
    """取不到字节 → 返回 ``(kind,'')``：保留类型让上游落「[图片]」占位＝改动前行为。"""
    api = _FakeApi(obs=_FakeObs(raise_on_download=True))
    assert LM.download_line_media(api, _msg(LM.CT_IMAGE), "a") == ("image", "")
    # 空字节同理
    api2 = _FakeApi(obs=_FakeObs(data=b""))
    assert LM.download_line_media(api2, _msg(LM.CT_IMAGE), "a") == ("image", "")
    # 无 msg id → 无从下载
    api3 = _FakeApi()
    assert LM.download_line_media(api3, _msg(LM.CT_IMAGE, mid=""), "a") == ("image", "")
    assert api3.obs.downloads == []


def test_download_respects_switches_and_caps():
    api = _FakeApi(obs=_FakeObs(data=b"X" * 100))
    off = LM.resolve_line_media_cfg(_cfg(inbound=False))
    assert LM.download_line_media(api, _msg(LM.CT_IMAGE), "a", cfg=off) == ("image", "")
    assert api.obs.downloads == [], "开关关时不应发生任何网络调用"
    # 自报体积超限 → 不下载
    small = LM.resolve_line_media_cfg(_cfg(inbound_max_bytes=10))
    big = _msg(LM.CT_FILE, meta={"FILE_NAME": "a.bin", "FILE_SIZE": "999"})
    assert LM.download_line_media(api, big, "a", cfg=small) == ("document", "")
    assert api.obs.downloads == []
    # 自报缺失但实际超限 → 下载后丢弃（护磁盘）
    assert LM.download_line_media(
        api, _msg(LM.CT_IMAGE), "a", cfg=small) == ("image", "")
    assert api.obs.downloads, "自报缺失时应当尝试下载"


def test_sticker_download_uses_cdn_not_obs(monkeypatch, tmp_path):
    calls = []

    def _fake_get(url, timeout=10.0):
        calls.append(url)
        return b"PNGDATA"

    monkeypatch.setattr(LM, "_download_sticker", _fake_get)
    api = _FakeApi()
    m = _msg(LM.CT_STICKER, meta={"STKID": "123"})
    kind, url = LM.download_line_media(api, m, "a")
    assert kind == "sticker" and url.endswith(".png")
    assert api.obs.downloads == [], "贴纸图在公开 CDN，不在 OBS 消息对象里"
    assert calls and "123" in calls[0]
    # 贴纸开关可单独关（CDN 路径万一失效时的止血阀）
    off = LM.resolve_line_media_cfg(_cfg(stickers=False))
    assert LM.download_line_media(api, m, "a", cfg=off) == ("sticker", "")


def test_download_non_media_is_noop():
    api = _FakeApi()
    assert LM.download_line_media(api, _msg(LM.CT_NONE, text="hi"), "a") == ("", "")
    assert api.obs.downloads == []


# ─────────────────── 3. 出站两步 + 撤回 ───────────────────

@pytest.fixture
def img(tmp_path):
    p = tmp_path / "selfie.jpg"
    p.write_bytes(b"\xff\xd8IMG")
    return str(p)


def test_send_media_two_step_success(img):
    pytest.importorskip("okline")
    api = _FakeApi()
    res = LM.send_line_media(api, "Uabc", media_path=img, media_type="image")
    assert res == {"delivered": True, "message_id": "sent-1"}
    assert len(api.sent_messages) == 1
    assert api.sent_messages[0]["contentType"] == LM.CT_IMAGE
    assert api.obs.uploads[0]["oid"] == "sent-1"
    assert api.obs.uploads[0]["obs_type"] == "image"
    assert api.obs.uploads[0]["enc_token"] == "enc-token"
    assert api.unsent == []


def test_upload_failure_recalls_placeholder(img):
    """核心不变量：字节传不上去就撤回占位——宁可不发，也不给客户留一条破图。"""
    pytest.importorskip("okline")
    api = _FakeApi(obs=_FakeObs(raise_on_upload=True))
    res = LM.send_line_media(api, "Uabc", media_path=img, media_type="image")
    assert res["delivered"] is False
    assert res["error"] == "obs_upload_failed"
    assert api.unsent == ["sent-1"], "上传失败必须撤回那条空壳占位消息"


def test_recall_failure_still_reports_not_delivered(img):
    """撤回本身也失败时不许抛、也不许谎报成功（会话里可能残留破图，但状态是诚实的）。"""
    pytest.importorskip("okline")
    api = _FakeApi(obs=_FakeObs(raise_on_upload=True), raise_on_unsend=True)
    res = LM.send_line_media(api, "Uabc", media_path=img, media_type="image")
    assert res["delivered"] is False


def test_no_message_id_skips_upload(img):
    pytest.importorskip("okline")
    api = _FakeApi(send_id="")
    res = LM.send_line_media(api, "Uabc", media_path=img, media_type="image")
    assert res == {"delivered": False, "error": "no_message_id"}
    assert api.obs.uploads == [], "拿不到 msg id 就没有上传目标，不该瞎传"


def test_caption_sent_as_separate_text_after_media(img):
    pytest.importorskip("okline")
    api = _FakeApi()
    LM.send_line_media(api, "Uabc", media_path=img, media_type="image",
                       caption="刚拍的～")
    assert api.texts == [("Uabc", "刚拍的～")]


def test_caption_failure_does_not_undo_delivered(img):
    """配文是锦上添花：图已送达就算成功，别因为一句配文把整次投递判失败。"""
    pytest.importorskip("okline")
    api = _FakeApi(raise_on_text=True)
    res = LM.send_line_media(api, "Uabc", media_path=img, media_type="image",
                             caption="嘿")
    assert res["delivered"] is True


def test_voice_placeholder_carries_duration(monkeypatch, tmp_path):
    """语音条要带 DURATION，否则 LINE 上显示 0:00＝像坏消息。"""
    pytest.importorskip("okline")
    monkeypatch.setattr(LM, "_probe_duration_ms", lambda p: 3500)
    p = tmp_path / "v.m4a"
    p.write_bytes(b"AUDIO")
    api = _FakeApi()
    LM.send_line_media(api, "Uabc", media_path=str(p), media_type="voice")
    assert api.sent_messages[0]["contentMetadata"]["DURATION"] == "3500"


def test_unknown_media_type_falls_back_to_extension(img):
    """陌生 ``media_type`` 不该否决一张完好的 JPEG——扩展名才是「这是什么字节」的准信号。"""
    pytest.importorskip("okline")
    api = _FakeApi()
    res = LM.send_line_media(api, "U", media_path=img, media_type="hologram")
    assert res["delivered"] is True
    assert api.sent_messages[0]["contentType"] == LM.CT_IMAGE


def test_send_media_guards(tmp_path, img):
    pytest.importorskip("okline")
    api = _FakeApi()
    assert LM.send_line_media(api, "U", media_path="")["error"] == "media_missing"
    assert LM.send_line_media(
        api, "U", media_path=str(tmp_path / "nope.jpg"))["error"] == "media_missing"
    # 既认不出大类、又没有扩展名 → 才判不支持（有扩展名一律能当文件发）
    noext = tmp_path / "blob"
    noext.write_bytes(b"??")
    assert LM.send_line_media(
        api, "U", media_path=str(noext), media_type="hologram")["error"] == \
        "unsupported_media_type"
    tiny = LM.resolve_line_media_cfg(_cfg(outbound_max_bytes=1))
    assert LM.send_line_media(
        api, "U", media_path=img, media_type="image", cfg=tiny)["error"] == \
        "media_too_large"
    assert api.sent_messages == [], "任一护栏拦下时都不该已经发出占位消息"


def test_placeholder_rejection_does_not_propagate(img):
    """Letter Sealing 会话会在「发占位」这步被服务端拒——必须变成可回落的失败，
    而不是异常穿透到 autosend 调用链（那会让整次投递崩掉而非退回文字）。"""
    pytest.importorskip("okline")

    class _Sealed(_FakeApi):
        def send_message(self, message):
            raise RuntimeError("LineApiError code=82 can not send using plain mode")

    api = _Sealed()
    res = LM.send_line_media(api, "U", media_path=img, media_type="image")
    assert res["delivered"] is False
    assert res["error"] == "placeholder_rejected"
    assert "82" in res.get("detail", ""), "错误原文要留痕（判断要不要做 E2EE 预检的依据）"
    assert api.obs.uploads == [] and api.unsent == []


# ─────────────────── 3b. 观测埋点 ───────────────────

def test_stats_record_inbound_paths(tmp_path):
    api = _FakeApi(obs=_FakeObs(data=b"IMG"))
    LM.download_line_media(api, _msg(LM.CT_IMAGE), "a")
    LM.download_line_media(api, _msg(LM.CT_IMAGE, mid=""), "a")   # no_message_id
    off = LM.resolve_line_media_cfg(_cfg(inbound=False))
    LM.download_line_media(api, _msg(LM.CT_VIDEO), "a", cfg=off)  # disabled
    d = _stats()["inbound"]
    assert d["total"] == 3 and d["ok"] == 1
    assert d["by_kind"] == {"image": 1}
    assert d["skipped"] == {"no_message_id": 1, "disabled": 1}
    # 非媒体消息不该进计数（否则 total 会被纯文本淹没）
    LM.download_line_media(api, _msg(LM.CT_NONE, text="hi"), "a")
    assert _stats()["inbound"]["total"] == 3


def test_stats_record_outbound_and_recall(img):
    pytest.importorskip("okline")
    LM.send_line_media(_FakeApi(), "U", media_path=img, media_type="image")
    LM.send_line_media(_FakeApi(obs=_FakeObs(raise_on_upload=True)), "U",
                       media_path=img, media_type="image")
    LM.send_line_media(_FakeApi(obs=_FakeObs(raise_on_upload=True),
                                raise_on_unsend=True),
                       "U", media_path=img, media_type="image")
    o = _stats()["outbound"]
    assert o["total"] == 3 and o["ok"] == 1
    assert o["by_kind"] == {"image": 1}
    assert o["failed"] == {"obs_upload_failed": 2}
    # 这两个数就是「两步链稳不稳」的直接读数
    assert o["orphan_recalled"] == 1
    assert o["recall_failed"] == 1


def test_stats_dump_and_prom_shape(img):
    pytest.importorskip("okline")
    assert _stats()["active"] is False, "零流量时 active=false（Prom 侧据此不输出零序列）"
    LM.send_line_media(_FakeApi(), "U", media_path=img, media_type="image")
    from src.integrations.line_media_stats import get_line_media_stats
    prom = get_line_media_stats().dump_prom()
    assert _stats()["active"] is True
    assert "line_media_outbound_ok_total 1" in prom
    assert "line_media_orphan_recalled_total 0" in prom


# ─────────────────── 4. worker 接线 / owns_media 契约 ───────────────────

def _worker(**media):
    return LineProtocolWorker({"account_id": "U1", "meta": {}}, _cfg(**media))


def test_send_media_attribute_gated_by_switch():
    """``owns_media()`` 的判据就是 ``hasattr(worker,"send_media")``。

    开关关 → 属性必须**不存在**（编排器判定 LINE 不支持媒体＝旧语义）；
    开 → 存在。谁把 ``_send_media_impl`` 改成普通 ``send_media`` 方法，这条就红——
    那等于把自拍/相册/克隆语音/命理 K 线在 LINE 上一次性全部放开。
    """
    assert not hasattr(_worker(), "send_media")
    assert not hasattr(_worker(outbound=False), "send_media")
    assert hasattr(_worker(outbound=True), "send_media")


def test_owns_media_reflects_switch():
    """端到端过编排器的真判定，而不是只测 hasattr（防判据将来改了这里还绿）。"""
    from src.integrations.account_orchestrator import (
        AccountOrchestrator, account_key,
    )
    for outbound, expect in ((False, False), (True, True)):
        orch = AccountOrchestrator(config=_cfg(outbound=outbound))
        w = _worker(outbound=outbound)
        managed = type("M", (), {"state": "running", "worker": w})()
        orch._managed[account_key("line", "U1")] = managed
        assert orch.owns_media("line", "U1") is expect


async def test_api_calls_are_serialized_per_account():
    """okline 的 ``next_req_seq()`` 是无锁 ``self._reqseq += 1``。

    两个线程并发取到同一个 reqSeq → LINE 侧按重复请求处理 = **静默丢消息**。
    发文本/发媒体/已读都从线程池打同一个 client，必须串行。
    """
    import asyncio
    import time

    w = _worker()
    order = []

    class _Slow:
        def send_text(self, to, text):
            order.append(f"in:{text}")
            time.sleep(0.05)
            order.append(f"out:{text}")
            return {"id": text}

    w.client = _Slow()
    await asyncio.gather(w.send("U", "a"), w.send("U", "b"))
    assert order in (
        ["in:a", "out:a", "in:b", "out:b"],
        ["in:b", "out:b", "in:a", "out:a"],
    ), f"两次发送交错了（reqSeq 会撞号）：{order}"


async def test_reply_to_uses_native_quote_and_falls_back():
    """引用回复：有 ref 就走 ``reply_text``；引用失败必须回落普通发送。

    被引用消息可能太旧/已撤回/不在本会话——引用只是气泡装饰，不该让整条消息发不出去。
    """
    w = _worker()

    class _Api(_FakeApi):
        def __init__(self, quote_ok=True):
            super().__init__()
            self.quote_ok = quote_ok
            self.quotes = []

        def reply_text(self, to, text, related_message_id):
            self.quotes.append((to, text, related_message_id))
            if not self.quote_ok:
                raise RuntimeError("too old to quote")
            return {"id": "q1"}

    w.client = _Api()
    assert (await w.send("U", "hi", reply_to={"id": "M7"}))["message_id"] == "q1"
    assert w.client.quotes == [("U", "hi", "M7")]
    assert w.client.texts == [], "有引用时不该再走普通发送"

    w.client = _Api(quote_ok=False)
    res = await w.send("U", "hi", reply_to={"id": "M7"})
    assert res["delivered"] is True, "引用失败也必须把消息发出去"
    assert w.client.texts == [("U", "hi")]

    w.client = _Api()
    await w.send("U", "plain")
    assert w.client.quotes == [] and w.client.texts == [("U", "plain")]


async def test_mark_read_needs_a_known_last_message():
    """LINE 已读要带「读到哪条」；没记到就返回 False，不猜。"""
    w = _worker()
    w.client = _FakeApi()
    assert await w.mark_read("Upeer") is False
    assert w.client.checked == []
    w._last_in_msg_id["Upeer"] = "M9"
    assert await w.mark_read("Upeer") is True
    assert w.client.checked == [("Upeer", "M9")]


async def test_mark_read_soft_fails():
    w = _worker()
    w.client = None
    assert await w.mark_read("U") is False

    class _Boom(_FakeApi):
        def send_chat_checked(self, chat_mid, last_message_id):
            raise RuntimeError("nope")

    w.client = _Boom()
    w._last_in_msg_id["U"] = "M1"
    assert await w.mark_read("U") is False


def test_inbound_media_skips_groups_by_default():
    """群与私聊共用同一条接收线程 → 群媒体默认不下载，别让群把私聊回复拖慢。"""
    w = _worker()
    w.client = _FakeApi(obs=_FakeObs(data=b"IMG"))
    ctx = type("C", (), {"message": _msg(LM.CT_IMAGE)})()
    assert w._inbound_media(ctx, is_group=True) == ("", "")
    assert w.client.obs.downloads == []
    kind, url = w._inbound_media(ctx, is_group=False)
    assert kind == "image" and url
    w2 = _worker(groups=True)
    w2.client = _FakeApi(obs=_FakeObs(data=b"IMG"))
    assert w2._inbound_media(ctx, is_group=True)[0] == "image"


# ─────────────────── 5. 探针工具护栏（tools/probe_line_media.py）───────────────

def _probe_mod():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "tools" / "probe_line_media.py"
    spec = importlib.util.spec_from_file_location("_probe_line_media", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_probe_resolves_relative_tokens_against_data_root(tmp_path):
    """注册表存的是 CWD 相对路径（``sessions\\line\\x.json``）。

    服务进程 CWD == 实例数据根故生产正确；从引擎根跑的 CLI 会解析到
    ``<引擎根>/sessions/...`` 找不到文件——本仓 AGENTS.md「CWD 相对路径＝迁移后的
    静默失真」那一节的活样本，实施当天就真的踩到了。
    """
    mod = _probe_mod()
    root = tmp_path / "data"
    (root / "sessions" / "line").mkdir(parents=True)
    (root / "sessions" / "line" / "acct.json").write_text("{}", encoding="utf-8")
    acct = {"account_id": "acct", "tokens_path": "sessions\\line\\acct.json"}
    got = mod._resolve_tokens_path({}, acct, root)
    assert got and got.endswith("acct.json")
    # 文件不存在 → 空串（调用方据此如实报错，不去连一个不存在的会话）
    assert mod._resolve_tokens_path(
        {}, {"account_id": "x", "tokens_path": "sessions/line/nope.json"}, root) == ""


def test_probe_refuses_confirm_without_target(monkeypatch, capsys):
    """``--confirm`` 不配目标一律拒跑——绝不替运维猜「发给谁」。"""
    mod = _probe_mod()
    monkeypatch.setattr("sys.argv", ["probe_line_media.py", "--confirm"])
    assert mod.main() == 2


def test_probe_output_is_ascii_only():
    """输出不得含 ✓/✗ 之类字符：PS5.1 控制台是 GBK，编不出就整条 traceback
    （本仓 watchdog_emotion_tts.ps1 与本工具首跑都踩过）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "tools" / "probe_line_media.py"
           ).read_text(encoding="utf-8")
    for bad in ("\u2713", "\u2717", "\u2714", "\u2718"):
        assert bad not in src, "输出符号 %r 在 GBK 控制台会炸" % bad


def test_inbound_media_never_raises():
    """入站落库主流程不能被一次媒体处理异常带走。"""
    w = _worker()
    w.client = None  # 触发 download 内部 AttributeError
    ctx = type("C", (), {"message": _msg(LM.CT_IMAGE)})()
    assert w._inbound_media(ctx, is_group=False) == ("image", "")
    broken = type("C", (), {})()  # 连 .message 都没有
    assert w._inbound_media(broken, is_group=False) == ("", "")
