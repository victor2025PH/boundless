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
    # #169：出站缺省与收件箱路由同源（内建 LINE 100MB），不再是独立硬编码的 20MB
    from src.inbox.media_limits import platform_media_cap_bytes
    assert dirty["outbound_max_bytes"] == platform_media_cap_bytes(None, "line")
    assert dirty["outbound_max_bytes"] > LM.DEFAULT_OUTBOUND_MAX_BYTES
    # 运营在 inbox.media.limits_mb.line 压上限 → worker 缺省跟着走（两端不分叉）
    tight = LM.resolve_line_media_cfg({
        "platform_login": {"line": {"media": {}}},
        "inbox": {"media": {"limits_mb": {"line": 20}}},
    })
    assert tight["outbound_max_bytes"] == 20 * 1024 * 1024
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


# ─────────────────── 3a. reqSeq 抗判重（HTTP 423 病理） ───────────────────
#
# 病理（2026-09-01 skuio 客户机实锤）：应用重启后新 client 的 _reqseq 从 0 起，
# 媒体占位内容彼此相同 → 撞历史 (reqSeq, 内容) 被服务端判重 → 返回**旧** message
# id → OBS 上传 423 Locked → 旧逻辑按「上传失败」撤回＝误删客户已收到的好消息。

class _Obs423(_FakeObs):
    """前 ``lock_times`` 次上传回 423（真实 okline 异常带 status 属性 + 消息文本）。"""

    def __init__(self, lock_times=1):
        super().__init__()
        self._lock_times = lock_times

    def upload_message_object(self, oid, data, **kw):
        self.uploads.append({"oid": oid, "size": len(data), **kw})
        if len(self.uploads) <= self._lock_times:
            e = RuntimeError("OBS upload failed: HTTP 423 path=https://obs/x")
            e.status = 423
            raise e
        return {"ok": True}


class _RestartedApi(_FakeApi):
    """模拟重启后的 client：低位 reqSeq；两次 send_message 回不同 id。"""

    def __init__(self, ids=("old-hit", "fresh-2"), **kw):
        super().__init__(**kw)
        self._reqseq = 3
        self._ids = list(ids)

    def send_message(self, message):
        self.sent_messages.append(message)
        return {"id": self._ids[min(len(self.sent_messages), len(self._ids)) - 1]}


def test_is_obs_locked_detects_status_attr_and_text():
    class _E(Exception):
        status = 423

    assert LM._is_obs_locked(_E("locked")) is True
    assert LM._is_obs_locked(RuntimeError("OBS upload failed: HTTP 423 path=x")) is True
    assert LM._is_obs_locked(RuntimeError("OBS upload failed: HTTP 500")) is False


def test_compute_reqseq_floor_monotonic_even_on_clock_rollback():
    f1 = LM.compute_reqseq_floor(0, now=LM._REQSEQ_EPOCH + 100)
    assert f1 == 100, "正常路径＝秒级时间基线"
    # 时钟回拨：时间基线倒退，也必须靠「已知值 + 兜底步长」严格前进
    f2 = LM.compute_reqseq_floor(f1, now=LM._REQSEQ_EPOCH + 50)
    assert f2 > f1


def test_bump_client_reqseq_advances_across_restarts(tmp_path):
    ff = str(tmp_path / "tokens.json.reqseq")
    api1 = _FakeApi()
    api1._reqseq = 0
    f1 = LM.bump_client_reqseq(api1, account_id="U1", floor_file=ff)
    assert api1._reqseq == f1 > 0
    # 「重启」：新 client 又从 0 起，floor 必须严格越过上一轮
    api2 = _FakeApi()
    api2._reqseq = 0
    f2 = LM.bump_client_reqseq(api2, account_id="U1", floor_file=ff)
    assert f2 > f1
    # 侧车文件读写失败不阻断（退化为纯时间基线）
    api3 = _FakeApi()
    api3._reqseq = 0
    assert LM.bump_client_reqseq(api3, floor_file=str(tmp_path / "no" / "dir")) > 0


def test_obs_423_retries_with_fresh_reqseq_and_never_recalls_old_id(img):
    """核心不变量：423 命中的 id 疑为历史好消息——绝不撤回；
    换新鲜 reqSeq 重发占位、对新 id 重传字节，消息最终送达。"""
    pytest.importorskip("okline")
    api = _RestartedApi(obs=_Obs423(lock_times=1))
    res = LM.send_line_media(api, "Uabc", media_path=img, media_type="image")
    assert res == {"delivered": True, "message_id": "fresh-2"}
    assert api.unsent == [], "423 判重命中的旧 id 绝不许撤回（那是客户已收到的消息）"
    assert api._reqseq > 3, "重试前必须把 reqSeq 推进到新鲜区间"
    assert [u["oid"] for u in api.obs.uploads] == ["old-hit", "fresh-2"]
    assert _stats()["outbound"]["ok"] == 1


def test_obs_423_retry_still_same_id_gives_up_without_recall(img):
    """重发占位仍拿到同一 id＝判重再次命中——放弃但不撤回（宁可这条不发，
    也不删历史消息）。"""
    pytest.importorskip("okline")
    api = _RestartedApi(ids=("old-hit", "old-hit"), obs=_Obs423(lock_times=9))
    res = LM.send_line_media(api, "Uabc", media_path=img, media_type="image")
    assert res["delivered"] is False
    assert res["error"] == "obs_upload_locked"
    assert api.unsent == []


def test_obs_423_twice_recalls_only_fresh_placeholder(img):
    """重试后的占位持新鲜 reqSeq → 其 id 必是新消息，再失败时撤回它是安全的；
    第一次 423 命中的旧 id 仍然不动。"""
    pytest.importorskip("okline")
    api = _RestartedApi(obs=_Obs423(lock_times=9))
    res = LM.send_line_media(api, "Uabc", media_path=img, media_type="image")
    assert res["delivered"] is False
    assert res["error"] == "obs_upload_failed"
    assert api.unsent == ["fresh-2"], "只许撤回重试轮的新占位，不许碰 old-hit"


def test_obs_423_warning_logs_reqseq(img, caplog):
    """验收③：判重命中必须在 WARNING 级留痕 reqSeq（客户机 backend.log 可归因）。"""
    pytest.importorskip("okline")
    import logging as _logging
    api = _RestartedApi(obs=_Obs423(lock_times=1))
    with caplog.at_level(_logging.WARNING, logger="src.integrations.line_media"):
        LM.send_line_media(api, "Uabc", media_path=img, media_type="image")
    hits = [r for r in caplog.records if "判重碰撞" in r.getMessage()]
    assert hits and "reqSeq" in hits[0].getMessage()


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


async def test_api_lock_wait_is_bounded(monkeypatch):
    """悬死隔离（2026-09-02 WEXX7E 实锤）：一笔悬死请求持 ``_api_lock`` 不放时，
    后续调用必须限时出局（明确异常，调用方按失败路径处置），而不是在锁上
    无限排队陪葬——钧机就是这样整个 worker 无声僵死一小时的。"""
    monkeypatch.setattr(LineProtocolWorker, "_API_LOCK_TIMEOUT_SEC", 0.05)
    w = _worker()
    w.client = _FakeApi()
    w._api_lock.acquire()  # 模拟悬死的前一笔（持锁永不释放）
    try:
        with pytest.raises(RuntimeError, match="串行锁"):
            await w.send("U", "hello")
    finally:
        w._api_lock.release()
    # 锁释放后恢复正常收发
    assert (await w.send("U", "hello"))["delivered"] is True


def test_kick_stuck_send_terminates_live_bridge_only():
    """恢复锤契约：只 terminate **存活**的签名桥 Node 进程（悬死线程的 readline
    见 EOF 后自行抛错解锁）；进程已退/属性缺席/client 为 None 都必须静默无害
    ——恢复锤自己抛错等于二次事故。"""
    class _Proc:
        pid = 4321

        def __init__(self, alive):
            self._alive = alive
            self.terminated = 0

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated += 1

    def _client_with(proc):
        signer = type("S", (), {"_proc": proc})()
        transport = type("T", (), {"_signer": signer})()
        return type("C", (), {"transport": transport})()

    w = _worker()
    alive = _Proc(alive=True)
    w.client = _client_with(alive)
    w.kick_stuck_send()
    assert alive.terminated == 1

    dead = _Proc(alive=False)
    w.client = _client_with(dead)
    w.kick_stuck_send()
    assert dead.terminated == 0

    w.client = _client_with(None)
    w.kick_stuck_send()
    w.client = None
    w.kick_stuck_send()


def test_inbound_media_skips_groups_by_default():
    """群与私聊共用同一条接收线程 → 群媒体默认不下载，别让群把私聊回复拖慢。

    （impl85：``_inbound_media`` 形参从 okline ctx 改为裸消息 dict——SSE 与
    拉取兜底两条路径共用，本测试跟随新签名。）
    """
    w = _worker()
    w.client = _FakeApi(obs=_FakeObs(data=b"IMG"))
    message = _msg(LM.CT_IMAGE)
    assert w._inbound_media(message, is_group=True) == ("", "")
    assert w.client.obs.downloads == []
    kind, url = w._inbound_media(message, is_group=False)
    assert kind == "image" and url
    w2 = _worker(groups=True)
    w2.client = _FakeApi(obs=_FakeObs(data=b"IMG"))
    assert w2._inbound_media(message, is_group=True)[0] == "image"


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


def test_probe_picks_online_account_first():
    mod = _probe_mod()
    accounts = [
        {"account_id": "off", "status": "offline"},
        {"account_id": "on", "status": "online"},
        {"account_id": "gone", "status": "removed"},
    ]
    assert mod.pick_line_account(accounts)["account_id"] == "on"
    assert mod.pick_line_account(accounts, "off")["account_id"] == "off"
    assert mod.pick_line_account(accounts, "gone") is None
    assert mod.pick_line_account([]) is None


def test_probe_media_magic_rejects_json_envelope(tmp_path):
    """2026-08-13：把 JSON 信封当成 wav 落盘。KB 数过了、magic 必须红。"""
    mod = _probe_mod()
    fake = tmp_path / "x.wav"
    fake.write_bytes(b'{"ok":true,"audio_base64":"xxxx"}')
    r = mod.check_media_magic(str(fake), "voice")
    assert r["ok"] is False
    assert "bad_magic" in r["reason"]
    jpg = tmp_path / "y.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 8)
    assert mod.check_media_magic(str(jpg), "image")["container"] == "JPEG"
    assert mod.check_media_magic(str(tmp_path / "nope.jpg"), "image")["reason"] == "missing_file"


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
    assert w._inbound_media(_msg(LM.CT_IMAGE), is_group=False) == ("image", "")
    # 消息体缺失/形态坏 → 软回落空媒体（绝不抛）
    assert w._inbound_media(None, is_group=False) == ("", "")


# ─────────────────── 6. #101 双向修复（时长探测 / 下载重试 / M4A 转码）───────────
# 事故：入站语音「无音频存档」（下载瞬态失败×零重试×debug 级日志不可诊）+
# 出站语音对方端 0:00（打包态 ffprobe 不在 PATH，裸 shutil.which 探不到随包二进制）。


def test_probe_duration_prefers_resolver_aware_probe(monkeypatch):
    """时长首选 ``voice_sender.probe_audio_duration_ms``（经 ffmpeg_resolver 找 ffprobe）。

    #101 根因：客户桌面包的 ffprobe 在 ``resources/ffmpeg/`` 不在 PATH，旧链路
    只走 media_probe 的裸 ``shutil.which`` → 打包态恒 0 → 对方端 0:00。
    """
    import src.client.voice_sender as VS
    monkeypatch.setattr(VS, "probe_audio_duration_ms", lambda p: 4321)
    assert LM._probe_duration_ms("whatever.ogg") == 4321


def test_probe_duration_falls_back_to_media_probe(monkeypatch):
    import src.client.voice_sender as VS
    import src.companion.media_probe as MP
    monkeypatch.setattr(VS, "probe_audio_duration_ms", lambda p: None)
    monkeypatch.setattr(MP, "probe_video", lambda p: {"duration_ms": 777})
    assert LM._probe_duration_ms("x.ogg") == 777
    monkeypatch.setattr(MP, "probe_video", lambda p: None)
    assert LM._probe_duration_ms("x.ogg") == 0


def test_media_probe_binaries_are_resolver_aware(monkeypatch):
    """media_probe 家族必须经 ffmpeg_resolver 解析二进制（resolver 内部才有
    「打包布局 → PATH」的完整回落序；裸 which 在打包态永远 False）。"""
    import src.companion.media_probe as MP
    import src.utils.ffmpeg_resolver as FR
    monkeypatch.setattr(FR, "ffprobe_path", lambda: r"C:\bundled\ffprobe.exe")
    monkeypatch.setattr(FR, "ffmpeg_path", lambda: r"C:\bundled\ffmpeg.exe")
    assert MP.ffprobe_available() is True
    assert MP.ffmpeg_available() is True
    monkeypatch.setattr(FR, "ffprobe_path", lambda: None)
    monkeypatch.setattr(FR, "ffmpeg_path", lambda: None)
    assert MP.ffprobe_available() is False
    assert MP.ffmpeg_available() is False


class _FlakyObs:
    """脚本化 OBS：每次 download 依次消费 script（'ok' / 'empty' / 异常实例）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.uploads = []

    def download_object(self, service, sid, oid, **kw):
        self.calls += 1
        step = self.script.pop(0)
        if step == "ok":
            return b"AUDIOBYTES"
        if step == "empty":
            return b""
        raise step


def _http_401():
    class _R:
        status_code = 401

    exc = RuntimeError("unauthorized")
    exc.response = _R()
    return exc


def test_download_retries_once_on_transient(monkeypatch):
    """瞬态失败重试一次（#101 真机取证：同一对象几分钟内两败两成）。"""
    monkeypatch.setattr(LM.time, "sleep", lambda s: None)
    api = _FakeApi(obs=_FlakyObs([RuntimeError("conn reset"), "ok"]))
    kind, url = LM.download_line_media(api, _msg(LM.CT_AUDIO, mid="V1"), "a")
    assert kind == "voice" and url.endswith(".m4a")
    assert api.obs.calls == 2


def test_download_empty_body_retries_once(monkeypatch):
    monkeypatch.setattr(LM.time, "sleep", lambda s: None)
    api = _FakeApi(obs=_FlakyObs(["empty", "ok"]))
    _, url = LM.download_line_media(api, _msg(LM.CT_AUDIO, mid="V2"), "a")
    assert url != ""
    assert api.obs.calls == 2


def test_download_401_refreshes_token_then_retries(monkeypatch):
    """OBS 走裸 GET，不在 okline 的 401 自动刷新圈内（那个钩子只挂在 thrift
    ``post_json`` 上）——token 陈旧时文字链自愈、媒体全灭，必须显式刷新重试。"""
    monkeypatch.setattr(LM.time, "sleep", lambda s: None)
    # 开机 monotonic < 冷却窗：缺省 _lm_refresh_ts=0 不得被当成「刚刷过」。
    monkeypatch.setattr(LM.time, "monotonic", lambda: 10.0)
    refreshed = []
    import src.integrations.line_pull_sync as LPS
    monkeypatch.setattr(LPS, "refresh_client_token",
                        lambda c: refreshed.append(c) or True)
    api = _FakeApi(obs=_FlakyObs([_http_401(), "ok"]))
    _, url = LM.download_line_media(api, _msg(LM.CT_AUDIO, mid="V3"), "a")
    assert url != ""
    assert refreshed == [api]
    assert api.obs.calls == 2


def test_download_401_refresh_failure_stops_early(monkeypatch):
    """刷新失败（refresh token 已死/冷却中）→ 不再白打第二次 GET。"""
    monkeypatch.setattr(LM.time, "sleep", lambda s: None)
    monkeypatch.setattr(LM.time, "monotonic", lambda: 10.0)
    import src.integrations.line_pull_sync as LPS
    monkeypatch.setattr(LPS, "refresh_client_token", lambda c: False)
    api = _FakeApi(obs=_FlakyObs([_http_401(), "ok"]))
    _, url = LM.download_line_media(api, _msg(LM.CT_AUDIO, mid="V4"), "a")
    assert url == ""
    assert api.obs.calls == 1
    assert _stats()["inbound"]["skipped"].get("download_error") == 1


def test_refresh_cooldown_prevents_hammering(monkeypatch):
    """同一 client 冷却窗内只刷一次：refresh token 已死时别每条媒体都白打。"""
    monkeypatch.setattr(LM.time, "sleep", lambda s: None)
    # 钉在冷却窗内的小 monotonic：第一次必须真刷，第二次命中冷却。
    monkeypatch.setattr(LM.time, "monotonic", lambda: 10.0)
    calls = []
    import src.integrations.line_pull_sync as LPS
    monkeypatch.setattr(LPS, "refresh_client_token",
                        lambda c: calls.append(1) or True)
    api = _FakeApi(obs=_FlakyObs([_http_401(), _http_401(), _http_401()]))
    LM.download_line_media(api, _msg(LM.CT_AUDIO, mid="V5"), "a")   # 刷新→重试仍 401
    LM.download_line_media(api, _msg(LM.CT_AUDIO, mid="V6"), "a")   # 冷却中→单次即止
    assert len(calls) == 1
    assert api.obs.calls == 3


def test_download_failure_logs_warning(monkeypatch, caplog):
    """失败必须 WARNING 级可见（#101 教训：debug 级＝客户机上无法远程归因）。"""
    import logging as _logging
    monkeypatch.setattr(LM.time, "sleep", lambda s: None)
    api = _FakeApi(obs=_FakeObs(raise_on_download=True))
    with caplog.at_level(_logging.WARNING, logger="src.integrations.line_media"):
        LM.download_line_media(api, _msg(LM.CT_AUDIO, mid="V9"), "acctX")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "reason=download_error" in joined
    assert "V9" in joined and "acctX" in joined


def test_config_switch_misses_stay_quiet(monkeypatch, caplog):
    """开关关是运营选择不是故障——不许升 WARNING 制造告警噪音。"""
    import logging as _logging
    api = _FakeApi()
    off = LM.resolve_line_media_cfg(_cfg(inbound=False))
    with caplog.at_level(_logging.INFO, logger="src.integrations.line_media"):
        LM.download_line_media(api, _msg(LM.CT_IMAGE), "a", cfg=off)
    assert not caplog.records


def test_send_voice_converts_to_m4a(monkeypatch, tmp_path):
    """OGG 语音出站显式转 M4A：上传字节/文件名/时长三者同源自转码产物，
    临时产物用完即清；原文件绝不动（发送失败重试还要用）。"""
    pytest.importorskip("okline")
    made = {}

    def _fake_convert(path):
        dst = tmp_path / "conv.m4a"
        dst.write_bytes(b"M4ABYTES")
        made["src"] = path
        return str(dst)

    monkeypatch.setattr(LM, "_convert_audio_for_line", _fake_convert)
    monkeypatch.setattr(LM, "_probe_duration_ms", lambda p: 2000)
    src = tmp_path / "v.ogg"
    src.write_bytes(b"OggS....")
    api = _FakeApi()
    res = LM.send_line_media(api, "U", media_path=str(src), media_type="voice")
    assert res["delivered"] is True
    assert api.obs.uploads[0]["size"] == len(b"M4ABYTES")
    assert api.obs.uploads[0]["name"] == "conv.m4a"
    assert api.sent_messages[0]["contentMetadata"]["DURATION"] == "2000"
    assert made["src"] == str(src)
    assert not (tmp_path / "conv.m4a").exists(), "转码临时产物必须清理"
    assert src.exists()


def test_send_voice_conversion_failure_falls_back_to_original(monkeypatch, tmp_path):
    """转不成 M4A（ffmpeg 缺失/坏源）→ 按原格式直传＝改动前行为，绝不因转码挡投递。"""
    pytest.importorskip("okline")
    monkeypatch.setattr(LM, "_convert_audio_for_line", lambda p: "")
    monkeypatch.setattr(LM, "_probe_duration_ms", lambda p: 1500)
    src = tmp_path / "v.ogg"
    src.write_bytes(b"OggSdata")
    api = _FakeApi()
    res = LM.send_line_media(api, "U", media_path=str(src), media_type="voice")
    assert res["delivered"] is True
    assert api.obs.uploads[0]["size"] == len(b"OggSdata")
    assert src.exists()


def test_convert_audio_helper_noop_for_aac_family(tmp_path):
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")
    assert LM._convert_audio_for_line(str(p)) == ""


def test_convert_audio_helper_soft_fails_without_ffmpeg(monkeypatch, tmp_path):
    import src.utils.ffmpeg_resolver as FR
    monkeypatch.setattr(FR, "ffmpeg_path", lambda: None)
    p = tmp_path / "a.ogg"
    p.write_bytes(b"OggS")
    assert LM._convert_audio_for_line(str(p)) == ""


# ─────────────────── 7. #101 P2：零依赖时长解析（ffmpeg 彻底缺席的最后兜底）──────


def _fake_ogg_opus(granule: int) -> bytes:
    """最小可解析 OGG/Opus：首页含 OpusHead 标识，末页带 granulepos。"""
    head = (b"OggS" + bytes(2) + (0).to_bytes(8, "little")
            + bytes(14) + b"OpusHead" + bytes(32))
    tail = (b"OggS" + bytes(2)
            + int(granule).to_bytes(8, "little", signed=True) + bytes(18))
    return head + tail


def _fake_m4a(duration: int, timescale: int = 1000, version: int = 0) -> bytes:
    """最小可解析 M4A：ftyp + moov(mvhd v0/v1)。"""
    if version == 0:
        payload = (bytes([0]) + bytes(3) + bytes(4) + bytes(4)
                   + timescale.to_bytes(4, "big") + duration.to_bytes(4, "big"))
    else:
        payload = (bytes([1]) + bytes(3) + bytes(8) + bytes(8)
                   + timescale.to_bytes(4, "big") + duration.to_bytes(8, "big"))
    mvhd = (8 + len(payload)).to_bytes(4, "big") + b"mvhd" + payload
    moov = (8 + len(mvhd)).to_bytes(4, "big") + b"moov" + mvhd
    ftyp = (16).to_bytes(4, "big") + b"ftyp" + b"isomiso2"
    return ftyp + moov


def test_ogg_opus_pure_duration(tmp_path):
    p = tmp_path / "v.ogg"
    p.write_bytes(_fake_ogg_opus(96_000))          # 96000/48kHz = 2s
    assert LM._ogg_opus_duration_ms(str(p)) == 2000
    # 非 Opus 的 OGG（无 OpusHead）→ 宁窄勿错，不猜
    p2 = tmp_path / "vorbis.ogg"
    p2.write_bytes(b"OggS" + bytes(2) + (96_000).to_bytes(8, "little") + bytes(40))
    assert LM._ogg_opus_duration_ms(str(p2)) == 0
    # 垃圾字节 → 0
    p3 = tmp_path / "junk.ogg"
    p3.write_bytes(b"not-an-ogg")
    assert LM._ogg_opus_duration_ms(str(p3)) == 0


def test_mp4_pure_duration_v0_and_v1(tmp_path):
    p0 = tmp_path / "a.m4a"
    p0.write_bytes(_fake_m4a(2500, timescale=1000, version=0))
    assert LM._mp4_duration_ms(str(p0)) == 2500
    p1 = tmp_path / "b.m4a"
    p1.write_bytes(_fake_m4a(90_000, timescale=30_000, version=1))
    assert LM._mp4_duration_ms(str(p1)) == 3000
    junk = tmp_path / "c.m4a"
    junk.write_bytes(b"\x00\x00\x00\x08free")
    assert LM._mp4_duration_ms(str(junk)) == 0


def test_probe_duration_pure_python_is_last_resort(monkeypatch, tmp_path):
    """前两层全灭（无 ffprobe 可用）时，纯解析仍给出真时长——0:00 不再有死角。"""
    import src.client.voice_sender as VS
    import src.companion.media_probe as MP
    monkeypatch.setattr(VS, "probe_audio_duration_ms", lambda p: None)
    monkeypatch.setattr(MP, "probe_video", lambda p: None)
    p = tmp_path / "v.ogg"
    p.write_bytes(_fake_ogg_opus(144_000))
    assert LM._probe_duration_ms(str(p)) == 3000


def test_pure_duration_matches_ffprobe_on_real_files(tmp_path):
    """真实编码件交叉验证：纯解析与 ffprobe 口径须一致（无 ffmpeg 的机器跳过）。"""
    import subprocess
    from src.utils.ffmpeg_resolver import ffmpeg_path
    ff = ffmpeg_path()
    if not ff:
        pytest.skip("本机无 ffmpeg，跳过真实编码件交叉验证")
    ogg = tmp_path / "t.ogg"
    subprocess.run(
        [ff, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-c:a", "libopus", str(ogg)],
        check=True, timeout=60, capture_output=True)
    ms = LM._ogg_opus_duration_ms(str(ogg))
    assert 1800 <= ms <= 2400, f"OGG 纯解析={ms}ms 偏离 2s 真值"
    m4a = tmp_path / "t.m4a"
    subprocess.run(
        [ff, "-y", "-v", "error", "-i", str(ogg), "-vn", "-c:a", "aac", str(m4a)],
        check=True, timeout=60, capture_output=True)
    ms2 = LM._mp4_duration_ms(str(m4a))
    assert 1800 <= ms2 <= 2500, f"M4A 纯解析={ms2}ms 偏离 2s 真值"
