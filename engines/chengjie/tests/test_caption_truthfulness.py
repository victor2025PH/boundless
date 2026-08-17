"""图文一致性 P0-P2 门禁（2026-08-08，修「发的照片配文全是错的」实录事故）。

事故原话（截图取证，WhatsApp/B 线 autosend）：
    [图片] 翻到一张之前拍的，给你瞅瞅我卧室的样子 😊   ← 画面是一张人脸自拍
    [图片] 之前拍的存货，家里随便吃吃嘛 😊             ← 同样是人脸自拍
根因链（三层，本文件逐层钉住）：
- **P0** 配文 LLM 看不见图：相册分支连条目真实场景都没透传，它只能顺着对话把
  自拍说成「我的卧室」「家里的吃的」→ 指令必须钉死「这是你本人的人像照」，
  并在对方点名想看别的东西时显式禁止「假装照片里有它」。
- **P1** 「想看什么」在链路上丢失：AI 说「我煮了燕窝粥，要不要看看」，客户回
  「你有照片就发我看一下」→ 只剩一个泛化要图信号 → 相册通用人像池顶包。
  非人像主体没货就该发诚实文字。
- **P2** 出站前 VLM 图文核对：配文声称画面里有 X 而 VLM 说没有 → 换诚实兜底文案。
"""
import pytest

import src.ai.caption_image_guard as cg
import src.ai.companion_selfie as cs
import src.ai.outbound_promise_guard as pg
from src.inbox import image_autosend as ia


@pytest.fixture(autouse=True)
def _reset_provider():
    from src.companion.persona_media_store import (
        configure_persona_media_store,
        reset_persona_media_store,
    )
    from src.utils.selfie_cap import reset_selfie_cap_tracker
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()
    reset_persona_media_store()
    configure_persona_media_store(":memory:")  # 隔离：绝不写 config/persona_media.db
    yield
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()
    reset_persona_media_store()


@pytest.fixture(autouse=True)
def _persona_photos_on(monkeypatch):
    import src.companion.photo_capability as pc
    monkeypatch.setattr(pc, "persona_photos_enabled_by_id", lambda _pid: True)


def _cfg(**selfie):
    return {"companion": {"selfie": selfie}}


def _recorder():
    sent = []

    async def send_fn(mp, mu, mt, cap, inbox):
        sent.append({"path": mp, "cap": cap, "type": mt})
        return True
    return sent, send_fn


def _store():
    from src.companion.persona_media_store import get_persona_media_store
    return get_persona_media_store()


# ── P0：配文指令的人像硬约束 ────────────────────────────────────────────────
def test_selfie_instruction_forbids_claiming_other_subjects():
    ins = cs.build_photo_caption_instruction("发张照片看看", kind="selfie")
    assert "人像照" in ins
    assert "画面里只有你" in ins
    # 事故原话的两类谎言都必须被点名禁止
    assert "卧室" in ins and "家里的存货" in ins


def test_old_album_selfie_keeps_portrait_context():
    """freshness=old 时描述不得退化成中性的「一张照片」——那正是 LLM 开始
    把自拍说成房间/食物的口子。"""
    ins = cs.build_photo_caption_instruction(
        "发张照片看看", kind="selfie", freshness="old")
    assert "自拍照" in ins and "画面里是你本人" in ins
    assert "禁止" in ins and "刚拍" in ins  # 旧照诚实口径不能丢


def test_wanted_subject_line_forbids_pretending():
    ins = cs.build_photo_caption_instruction(
        "你有照片就发我看一下", kind="selfie", freshness="old",
        wanted_subject="燕窝粥")
    assert "燕窝粥" in ins and "这张照片里没有它" in ins


def test_object_photo_instruction_has_no_portrait_clause():
    """物体图画面里本就该是那样东西——人像约束若误加会自相矛盾。"""
    ins = cs.build_photo_caption_instruction(
        "你煮的面拍张照", kind="object", subject="noodles", wanted_subject="面")
    assert "人像照" not in ins and "这张照片里没有它" not in ins


def test_scene_line_keeps_person_as_subject():
    ins = cs.build_photo_caption_instruction(
        "在干嘛", kind="selfie", scene="cozy bedroom at night")
    assert "cozy bedroom at night" in ins
    assert "画面主体仍是你本人" in ins


# ── P1：「想看的是什么」抽取 ────────────────────────────────────────────────
@pytest.mark.parametrize("text,expect", [
    ("我煮了燕窝粥，要不要看看？", "燕窝粥"),
    ("刚做了提拉米苏，想看吗", "提拉米苏"),
    ("我今天买了新裙子，给你看看", "新裙子"),
    ("给你看看我的卧室～", "卧室"),
])
def test_detect_show_offer_subject_golden(text, expect):
    assert pg.detect_show_offer_subject(text) == expect


@pytest.mark.parametrize("text", [
    "要不要看看我的自拍？",          # 人像词 → 普通自拍 offer，不是"别的东西"
    "想看我的样子吗",
    "我给你拍张照片好不好",
    "今天好累啊，早点睡",            # 没有展示提议
    "我煮了粥",                      # 有东西没提议 → 不抽（避免乱拦）
])
def test_detect_show_offer_subject_conservative(text):
    assert pg.detect_show_offer_subject(text) == ""


def test_wanted_subject_carries_through_offer_then_generic_request():
    """事故主链：offer 说的是燕窝粥，客户只回一句泛化要图 → 想看的仍是燕窝粥。"""
    hist = [
        {"role": "user", "content": "在干嘛"},
        {"role": "assistant", "content": "我煮了燕窝粥，要不要看看？"},
    ]
    assert pg.wanted_media_subject(
        "你有照片，你就发给我看一下", hist, generic_request=True) == "燕窝粥"
    assert pg.wanted_media_subject("好呀", hist) == "燕窝粥"


def test_wanted_subject_empty_for_plain_selfie_offer():
    hist = [{"role": "assistant", "content": "要不要看看我的自拍？"}]
    assert pg.wanted_media_subject("好呀", hist) == ""


def test_wanted_subject_from_customer_own_words():
    assert pg.wanted_media_subject("那碗粥拍给我看看", []) == ""  # 「碗」是量词，粥1字不取
    assert pg.wanted_media_subject("发张燕窝粥的照片", []) == "燕窝粥"


def test_wanted_subject_needs_generic_request_flag():
    """客户在聊别的（不是要图）→ 不该把上一条 offer 主体翻出来。"""
    hist = [{"role": "assistant", "content": "我煮了燕窝粥，要不要看看？"}]
    assert pg.wanted_media_subject("你明天上班吗", hist) == ""


# ── P1：无货就别发图（端到端） ──────────────────────────────────────────────
async def test_album_selfie_not_sent_when_customer_wants_an_object():
    """相册里只有自拍，对方想看的是燕窝粥 → 不发图（交诚实文字），
    绝不拿人脸照顶包 —— 事故截图第二张的直接防线。"""
    _store().add("lin", "photo", "/disk/selfie1.jpg", "/static/selfie1.jpg")
    sent, send_fn = _recorder()
    cfg = _cfg(enabled=True, provider={"backend": "album"})
    hist = [{"role": "assistant", "content": "我煮了燕窝粥，要不要看看？"}]
    ok = await ia.run_autosend_image(
        cfg, "whatsapp", "acct1", "c1", "lin", "好呀", hist, send_fn=send_fn)
    assert ok is False and sent == []
    snap = ia.metrics_snapshot()
    assert snap["fallback_reasons"].get("wanted_subject_no_stock", 0) >= 1


async def test_operator_tagged_stock_still_sends():
    """运营真给该主体配了触发词条目 → 有货，照发（P1 只拦顶包，不误伤备货）。"""
    _store().add("lin", "photo", "/disk/zhou.jpg", "/static/zhou.jpg",
                 triggers=["燕窝粥"], caption="趁热喝~")
    sent, send_fn = _recorder()
    cfg = _cfg(enabled=True, provider={"backend": "album"})
    ok = await ia.run_autosend_image(
        cfg, "whatsapp", "acct1", "c1", "lin", "发张燕窝粥的照片", [],
        send_fn=send_fn)
    assert ok is True and sent and sent[0]["cap"] == "趁热喝~"


async def test_plain_selfie_request_unaffected():
    """普通要自拍（没点名别的东西）行为完全不变——P1 不能把正常发图拦掉。"""
    _store().add("lin", "photo", "/disk/selfie1.jpg", "/static/selfie1.jpg",
                 caption="嘿嘿")
    sent, send_fn = _recorder()
    cfg = _cfg(enabled=True, provider={"backend": "album"})
    ok = await ia.run_autosend_image(
        cfg, "whatsapp", "acct1", "c1", "lin", "发张自拍给我看看", [],
        send_fn=send_fn)
    assert ok is True and len(sent) == 1


async def test_album_branch_passes_real_scene_and_wanted_to_caption():
    """相册分支必须把条目真实场景 + 想看主体喂给配文 LLM（P0 断链点：
    此前只传了 freshness，场景全靠 LLM 猜 → 猜成「我的卧室」）。"""
    _store().add("lin", "photo", "/disk/s.jpg", "/static/s.jpg",
                 tags=["scene:bedroom"])
    got = {}

    async def fake_caption(kind, subject, scene="", freshness="fresh", wanted=""):
        got.update(kind=kind, scene=scene, freshness=freshness, wanted=wanted)
        return "嗯呐"

    sent, send_fn = _recorder()
    cfg = _cfg(enabled=True, provider={"backend": "album"},
               consistency={"enabled": False})
    ok = await ia.run_autosend_image(
        cfg, "whatsapp", "acct1", "c1", "lin", "给我看看你卧室", [],
        send_fn=send_fn, llm_caption=fake_caption)
    assert ok is True
    assert got["scene"] == "bedroom" and got["freshness"] == "old"
    assert got["wanted"] == "卧室"


# ── P2：出站前 VLM 图文核对 ─────────────────────────────────────────────────
def test_caption_verdict_only_blocks_explicit_mismatch():
    assert cg.caption_verdict({"caption_claims_match": True}) == (True, "ok")
    assert cg.caption_verdict({"caption_claims_match": None}) == (True, "ok")
    assert cg.caption_verdict({}) == (True, "ok")
    assert cg.caption_verdict(None) == (True, "parse_fail")
    ok, reason = cg.caption_verdict(
        {"caption_claims_match": False, "main_subject": "woman portrait"})
    assert ok is False and "woman portrait" in reason


def test_parse_caption_check_tolerates_fences_and_prose():
    out = cg.parse_caption_check(
        '好的\n```json\n{"caption_claims_match": false}\n```')
    assert out == {"caption_claims_match": False}
    assert cg.parse_caption_check("没有 JSON") is None


def test_caption_check_prompt_mentions_portrait_case():
    p = cg.build_caption_check_prompt("给你瞅瞅我卧室的样子")
    assert "给你瞅瞅我卧室的样子" in p and "portrait" in p
    assert "null" in p  # 不确定必须能回 null（保守放行）


def test_guard_cfg_defaults_off():
    cfg = cg.resolve_caption_guard_cfg({})
    assert cfg["enabled"] is False and "selfie" in cfg["kinds"]


async def test_ensure_truthful_caption_disabled_is_noop(monkeypatch):
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return (False, "x")
    monkeypatch.setattr(cg, "check_caption", _boom)
    cap, reason = await cg.ensure_truthful_caption(
        "/tmp/a.jpg", "原配文", scfg={}, fallback="兜底")
    assert cap == "原配文" and reason == "disabled" and called["n"] == 0


async def test_ensure_truthful_caption_rewrites_on_mismatch(monkeypatch):
    async def _no(*a, **k):
        return (False, "caption_mismatch(woman portrait)")
    monkeypatch.setattr(cg, "check_caption", _no)
    cap, reason = await cg.ensure_truthful_caption(
        "/tmp/a.jpg", "给你瞅瞅我卧室的样子",
        scfg={"caption_guard": {"enabled": True}}, fallback="翻到一张之前拍的~")
    assert cap == "翻到一张之前拍的~" and "caption_mismatch" in reason


async def test_ensure_truthful_caption_keeps_caption_when_no_fallback(monkeypatch):
    """没有兜底文案时宁可原样发——裸图（无配文）比配文有出入更像机器人。"""
    async def _no(*a, **k):
        return (False, "caption_mismatch(x)")
    monkeypatch.setattr(cg, "check_caption", _no)
    cap, _ = await cg.ensure_truthful_caption(
        "/tmp/a.jpg", "原配文", scfg={"caption_guard": {"enabled": True}})
    assert cap == "原配文"


async def test_check_caption_soft_passes_without_vision_cfg():
    ok, reason = await cg.check_caption("/tmp/a.jpg", "配文", {})
    assert ok is True and reason == "no_vision_cfg"


async def test_autosend_replaces_llm_caption_on_mismatch(monkeypatch):
    """端到端：核对判定不符 → 发出去的是诚实兜底文案而不是那句谎话，
    且图照发（错的是文字，不是图）。"""
    _store().add("lin", "photo", "/disk/s.jpg", "/static/s.jpg")

    async def fake_caption(*a, **k):
        return "给你瞅瞅我卧室的样子"

    async def _mismatch(image_path, caption, root_config, **k):
        return (False, "caption_mismatch(woman portrait)")
    monkeypatch.setattr(cg, "check_caption", _mismatch)

    sent, send_fn = _recorder()
    cfg = _cfg(enabled=True, provider={"backend": "album"},
               caption_guard={"enabled": True})
    ok = await ia.run_autosend_image(
        cfg, "whatsapp", "acct1", "c1", "lin", "发张自拍看看", [],
        send_fn=send_fn, llm_caption=fake_caption)
    assert ok is True and len(sent) == 1
    assert sent[0]["cap"] and sent[0]["cap"] != "给你瞅瞅我卧室的样子"
    assert ia.metrics_snapshot()["fallback_reasons"].get(
        "caption_guard_rewrite", 0) >= 1
