"""全自动「按需发图」autosend 出图（``src/inbox/image_autosend.py``）门禁。

覆盖：配置读取 / 要图意图规划（自拍 vs 上下文物体 vs 不发）/ 出图落盘
（album 挑图、openai 生图 img2img、物体图 text2img + LLM 精炼、失败回落）。
"""
import pytest

import src.ai.companion_selfie as cs
from src.inbox import image_autosend as ia


@pytest.fixture(autouse=True)
def _reset_provider():
    from src.utils.selfie_cap import reset_selfie_cap_tracker
    from src.companion.persona_media_store import (
        configure_persona_media_store, reset_persona_media_store)
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()
    reset_persona_media_store()
    configure_persona_media_store(":memory:")  # 隔离：绝不写 config/persona_media.db
    yield
    cs.reset_selfie_provider()
    reset_selfie_cap_tracker()
    reset_persona_media_store()


def _cfg(**selfie):
    return {"companion": {"selfie": selfie}}


# ── resolve_image_autosend_cfg ─────────────────────────────────────────────
def test_resolve_cfg_reads_companion_selfie():
    out = ia.resolve_image_autosend_cfg(_cfg(enabled=True, free_daily=2))
    assert out.get("enabled") is True and out.get("free_daily") == 2


def test_resolve_cfg_missing_returns_empty():
    assert ia.resolve_image_autosend_cfg({}) == {}
    assert ia.resolve_image_autosend_cfg({"companion": {}}) == {}


# ── plan_autosend_image ────────────────────────────────────────────────────
def test_plan_disabled_returns_none():
    assert ia.plan_autosend_image("發個照片給我看看", [], {"enabled": False}) is None


def test_plan_selfie_request():
    d = ia.plan_autosend_image("發個照片給我看看嘛", [], {"enabled": True})
    assert d and d["kind"] == "selfie"


def test_plan_object_request_needs_contextual_flag():
    txt = "你煮的面拍张照给我看看"
    # contextual 关：物体要图不发图（回落文本）
    assert ia.plan_autosend_image(txt, [], {"enabled": True}) is None
    # contextual 开：识别为物体图 + 出中英 prompt
    d = ia.plan_autosend_image(txt, [], {"enabled": True, "contextual_images": True})
    assert d and d["kind"] == "object" and "noodles" in d["prompt"]


def test_plan_empty_or_nonrequest_returns_none():
    assert ia.plan_autosend_image("", [], {"enabled": True}) is None
    assert ia.plan_autosend_image(
        "今天天气不错呀", [], {"enabled": True, "contextual_images": True}) is None


# ── stage_image_file ───────────────────────────────────────────────────────
async def test_stage_provider_disabled(tmp_path):
    cfg = _cfg(enabled=True, provider={"enabled": False, "backend": "disabled"})
    assert await ia.stage_image_file(
        cfg, "telegram", "acct1", "", {"kind": "selfie"}) is None


async def test_stage_album_selfie(tmp_path, monkeypatch):
    album = tmp_path / "album"
    album.mkdir()
    (album / "a.png").write_bytes(b"\x89PNGdummy")
    saved = {}

    def fake_save(platform, account_id, filename, data):
        saved.update(platform=platform, account=account_id, data=data)
        return ("/tmp/out.png", "/static/out.png", "image")

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media", fake_save)
    cfg = _cfg(enabled=True, provider={
        "enabled": True, "backend": "album", "album_dir": str(album)})
    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "", {"kind": "selfie"})
    assert out == ("/tmp/out.png", "/static/out.png", "selfie")
    assert saved["data"] == b"\x89PNGdummy"
    assert saved["platform"] == "telegram" and saved["account"] == "acct1"


async def test_stage_object_album_returns_none(tmp_path):
    # 相册无法凭空生成任意物体图 → 回落（不发图）
    cfg = _cfg(enabled=True, provider={
        "enabled": True, "backend": "album", "album_dir": str(tmp_path)})
    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "",
        {"kind": "object", "prompt": "a bowl of noodles"})
    assert out is None


async def test_stage_selfie_openai_uses_prompt_and_base(tmp_path, monkeypatch):
    album = tmp_path / "album"
    album.mkdir()
    (album / "face.png").write_bytes(b"\x89PNGface")
    gen = tmp_path / "gen.png"
    gen.write_bytes(b"\x89PNGgen")
    cfg = _cfg(enabled=True, appearance="a young woman", provider={
        "enabled": True, "backend": "openai", "api_key": "x",
        "album_dir": str(album)})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])
    captured = {}

    async def fake_gen(prompt, **kw):
        captured["prompt"] = prompt
        captured.update(kw)
        return cs.SelfieResult(ok=True, image_path=str(gen), provider="openai")

    monkeypatch.setattr(prov, "generate", fake_gen)
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/l.png", "/static/l.png", "image"))
    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "", {"kind": "selfie"})
    assert out[2] == "selfie"
    # 自拍走 build_selfie_prompt（含 "Portrait selfie"）+ 相册基础图 img2img 锁脸
    assert "Portrait selfie" in captured["prompt"]
    assert captured.get("base_image") == str(album / "face.png")


async def test_stage_selfie_scene_override(tmp_path, monkeypatch):
    """Phase17：directive.scene 显式指定场景（proactive 文案-场景对齐）→ 进 prompt。"""
    gen = tmp_path / "g.png"
    gen.write_bytes(b"\x89PNGs")
    cfg = _cfg(enabled=True, appearance="a young woman",
               provider={"enabled": True, "backend": "openai", "api_key": "x"})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])
    captured = {}

    async def fake_gen(prompt, **kw):
        captured["prompt"] = prompt
        return cs.SelfieResult(ok=True, image_path=str(gen), provider="openai")

    monkeypatch.setattr(prov, "generate", fake_gen)
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/l.png", "/static/l.png", "image"))
    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "",
        {"kind": "selfie", "scene": "night market with lanterns"})
    assert out[2] == "selfie"
    assert "night market with lanterns" in captured["prompt"]


async def test_stage_object_text2img_and_llm_refine(tmp_path, monkeypatch):
    gen = tmp_path / "g.png"
    gen.write_bytes(b"\x89PNGobj")
    cfg = _cfg(
        enabled=True, contextual_images=True, contextual_images_llm_prompt=True,
        provider={"enabled": True, "backend": "openai", "api_key": "x"})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])
    captured = {}

    async def fake_gen(prompt, **kw):
        captured["prompt"] = prompt
        captured.update(kw)
        return cs.SelfieResult(ok=True, image_path=str(gen), provider="openai")

    monkeypatch.setattr(prov, "generate", fake_gen)
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/l", "/u", "image"))

    async def refine():
        return '"a gourmet bowl of ramen, steam"'

    out = await ia.stage_image_file(
        cfg, "telegram", "acct1", "",
        {"kind": "object", "prompt": "a bowl of noodles"}, llm_refine=refine)
    assert out == ("/l", "/u", "object")
    # 用了精炼后的 prompt（去引号），物体图不带人设基础图
    assert captured["prompt"] == "a gourmet bowl of ramen, steam"
    assert not captured.get("base_image")


async def test_stage_generate_fail_returns_none(tmp_path, monkeypatch):
    cfg = _cfg(enabled=True, provider={
        "enabled": True, "backend": "openai", "api_key": "x"})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])

    async def fake_gen(prompt, **kw):
        return cs.SelfieResult(ok=False, error="boom")

    monkeypatch.setattr(prov, "generate", fake_gen)
    assert await ia.stage_image_file(
        cfg, "telegram", "acct1", "", {"kind": "selfie"}) is None


# ── run_autosend_image：注册相册优先 + 生成回落编排 ──────────────────────────
def _store():
    from src.companion.persona_media_store import get_persona_media_store
    return get_persona_media_store()


def _recorder():
    sent = []

    async def send_fn(mp, mu, mt, cap, inbox):
        sent.append({"path": mp, "url": mu, "type": mt, "cap": cap, "inbox": inbox})
        return True
    return sent, send_fn


async def test_run_registry_keyword_hit_sends_and_records():
    st = _store()
    row = st.add("lin", "photo", "/disk/dance.jpg", "/static/dance.jpg",
                 triggers=["跳舞"], caption="看我跳~")
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        _cfg(enabled=True), "telegram", "acct1", "chatA", "lin",
        "给我跳舞看看", [], send_fn=send_fn, ai_text="好呀")
    assert ok is True
    assert sent[0]["path"] == "/disk/dance.jpg" and sent[0]["type"] == "photo"
    assert sent[0]["cap"] == "看我跳~"  # 用条目 caption
    assert st.get(row["id"])["hits"] == 1  # 命中计数 +1


async def test_run_registry_video_hit_uses_video_type():
    _store().add("lin", "video", "/disk/d.mp4", "/static/d.mp4", triggers=["跳舞"])
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        _cfg(enabled=True), "telegram", "acctVid", "c", "lin",
        "来段跳舞视频", [], send_fn=send_fn, ai_text="好")
    assert ok is True and sent[0]["type"] == "video"
    assert sent[0]["inbox"].startswith("[视频]")


async def test_run_registry_generic_pool_on_selfie_request():
    _store().add("lin", "photo", "/disk/p.jpg", "/static/p.jpg")  # 无触发词=通用池
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        _cfg(enabled=True), "telegram", "acctGen", "c", "lin",
        "發個照片給我看看嘛", [], send_fn=send_fn)
    assert ok is True and sent[0]["url"] == "/static/p.jpg"


async def test_run_generic_pool_not_used_for_nonrequest():
    # 无触发词条目仅在「泛化要照片」时才作候选；普通闲聊不发
    _store().add("lin", "photo", "/disk/p.jpg", "/static/p.jpg")
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        _cfg(enabled=True), "telegram", "acctChat", "c", "lin",
        "今天心情不错", [], send_fn=send_fn)
    assert ok is False and sent == []


async def test_run_generation_fallback_when_no_registry(tmp_path, monkeypatch):
    album = tmp_path / "album"
    album.mkdir()
    (album / "a.png").write_bytes(b"\x89PNGdummy")
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/tmp/out.png", "/static/out.png", "image"))
    cfg = _cfg(enabled=True, provider={
        "enabled": True, "backend": "album", "album_dir": str(album)})
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        cfg, "telegram", "acctGenr", "c", "lin",
        "發個照片給我看看嘛", [], send_fn=send_fn, ai_text="来啦")
    assert ok is True and sent[0]["type"] == "image"  # 走生成回落
    assert sent[0]["url"] == "/static/out.png"


async def test_run_disabled_returns_false():
    _store().add("lin", "photo", "/d/p.jpg", "/static/p.jpg", triggers=["跳舞"])
    _, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        {"companion": {"selfie": {"enabled": False}}}, "telegram", "a", "c",
        "lin", "给我跳舞", [], send_fn=send_fn)
    assert ok is False


async def test_run_selfie_generation_auto_registers_then_reuses(tmp_path, monkeypatch):
    """自动定妆闭环：首次生成自拍 → 成功发出后入册（auto_generated）；
    第二次同类请求由注册相册秒发同一张，不再触发生成。"""
    album = tmp_path / "album"
    gen_src = tmp_path / "gen.png"
    gen_src.write_bytes(b"\x89PNGgen")
    staged = tmp_path / "staged.png"

    def fake_save(platform, account_id, filename, data):
        staged.write_bytes(data)
        return (str(staged), "/static/staged.png", "image")

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media", fake_save)
    cfg = _cfg(enabled=True, appearance="a young woman",
               provider={"enabled": True, "backend": "openai", "api_key": "x",
                         "album_dir": str(album)})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])
    calls = {"n": 0}

    async def fake_gen(prompt, **kw):
        calls["n"] += 1
        assert kw.get("seed") == cs.stable_selfie_seed("lin")  # 固定种子透传
        return cs.SelfieResult(ok=True, image_path=str(gen_src), provider="openai")

    monkeypatch.setattr(prov, "generate", fake_gen)
    sent, send_fn = _recorder()
    # 第一次：注册相册为空 → 生成 → 发出 → 自动入册
    ok = await ia.run_autosend_image(
        cfg, "telegram", "acctReg", "cReg", "lin",
        "發個照片給我看看嘛", [], send_fn=send_fn, ai_text="来啦")
    assert ok is True and calls["n"] == 1
    rows = _store().list("lin")
    assert len(rows) == 1 and "auto_generated" in rows[0]["tags"]
    reg_path = rows[0]["file_path"]
    assert reg_path.startswith(str(album))  # 落到人设相册目录
    # 第二次：注册相册命中（通用池）→ 秒发同一张，不再生成
    ok2 = await ia.run_autosend_image(
        cfg, "telegram", "acctReg", "cReg2", "lin",
        "想看你的近照", [], send_fn=send_fn, ai_text="好")
    assert ok2 is True and calls["n"] == 1  # 未再次生成
    assert sent[1]["path"] == reg_path


async def test_run_selfie_album_growth_and_llm_caption(tmp_path, monkeypatch):
    """相册自动扩容 + 文图协同配文（同一会话连续要图的拟真行为）：
    ① 首次生成入册；② 同会话再要 → 通用池只剩「刚发过那张」→ 扩容改走生成
    （真人不会连发同一张）；③④ 两张在册后恢复相册轮换（避开上一张，零生成）；
    LLM 配文贯穿生成图与相册图（auto 条目无运营配文）。"""
    album = tmp_path / "album"
    gen_src = tmp_path / "gen.png"
    gen_src.write_bytes(b"\x89PNGgen")
    staged = tmp_path / "staged.png"

    def fake_save(platform, account_id, filename, data):
        staged.write_bytes(data)
        return (str(staged), "/static/staged.png", "image")

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media", fake_save)
    cfg = _cfg(enabled=True, appearance="a young woman",
               register_generated_max=3,
               provider={"enabled": True, "backend": "openai", "api_key": "x",
                         "album_dir": str(album)})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])
    calls = {"gen": 0, "cap": 0}

    async def fake_gen(prompt, **kw):
        calls["gen"] += 1
        return cs.SelfieResult(ok=True, image_path=str(gen_src), provider="openai")

    async def fake_caption(kind, subject):
        calls["cap"] += 1
        return f"配文{calls['cap']}"

    monkeypatch.setattr(prov, "generate", fake_gen)
    sent, send_fn = _recorder()
    kw = dict(send_fn=send_fn, ai_text="草稿", llm_caption=fake_caption)
    # ① 生成 + 入册（LLM 配文，非草稿文本）
    assert await ia.run_autosend_image(
        cfg, "telegram", "acctG", "cG", "lin", "發個照片給我看看嘛", [], **kw)
    assert calls["gen"] == 1 and sent[0]["cap"] == "配文1"
    assert len(_store().list("lin")) == 1
    # ② 同会话再要：池里只剩刚发过的那张 → 扩容生成第 2 张（不重复发同一张）
    assert await ia.run_autosend_image(
        cfg, "telegram", "acctG", "cG", "lin", "想看你的近照", [], **kw)
    assert calls["gen"] == 2 and sent[1]["cap"] == "配文2"
    assert len(_store().list("lin")) == 2
    # ③ 两张在册 → 相册轮换（避开上一张 → 发第 1 张），不再生成
    assert await ia.run_autosend_image(
        cfg, "telegram", "acctG", "cG", "lin", "再发张自拍", [], **kw)
    assert calls["gen"] == 2 and len(_store().list("lin")) == 2
    # ④ 继续轮换（发回第 2 张），依旧零生成；相册图也走 LLM 配文
    assert await ia.run_autosend_image(
        cfg, "telegram", "acctG", "cG", "lin", "看看你", [], **kw)
    assert calls["gen"] == 2 and sent[3]["cap"] == "配文4"


async def test_run_selfie_register_respects_cap_and_flag(tmp_path, monkeypatch):
    gen_src = tmp_path / "g.png"
    gen_src.write_bytes(b"\x89PNGx")
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: (str(gen_src), "/static/g.png", "image"))
    cfg = _cfg(enabled=True, register_generated=False,
               provider={"enabled": True, "backend": "openai", "api_key": "x",
                         "album_dir": str(tmp_path / "album")})
    prov = cs.get_selfie_provider(cfg["companion"]["selfie"]["provider"])

    async def fake_gen(prompt, **kw):
        return cs.SelfieResult(ok=True, image_path=str(gen_src), provider="openai")

    monkeypatch.setattr(prov, "generate", fake_gen)
    _, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        cfg, "telegram", "acctNR", "cNR", "lin",
        "發個照片給我看看嘛", [], send_fn=send_fn)
    assert ok is True
    assert _store().list("lin") == []  # register_generated=false → 不入册


async def test_run_rotation_avoids_repeat():
    st = _store()
    st.add("lin", "photo", "/d/1.jpg", "/static/1.jpg", triggers=["跳舞"])
    st.add("lin", "photo", "/d/2.jpg", "/static/2.jpg", triggers=["跳舞"])
    sent, send_fn = _recorder()
    for _ in range(2):
        await ia.run_autosend_image(
            _cfg(enabled=True), "telegram", "acctRot", "cRot", "lin",
            "给我跳舞", [], send_fn=send_fn)
    assert len(sent) == 2 and sent[0]["url"] != sent[1]["url"]  # 不连发同一张


def test_pick_registered_media_gated_and_hits():
    st = _store()
    st.add("lin", "photo", "/d/1.jpg", "/static/1.jpg", triggers=["跳舞"])
    # 关：返回 None
    assert ia.pick_registered_media(
        {"companion": {"selfie": {"enabled": False}}}, "lin", "跳舞") is None
    # 开 + 命中关键词
    row = ia.pick_registered_media(_cfg(enabled=True), "lin", "给我跳舞", avoid_id="")
    assert row and row["url"] == "/static/1.jpg"
    # 不命中 + 非要图请求 → None
    assert ia.pick_registered_media(_cfg(enabled=True), "lin", "在吗") is None


def test_pick_registered_media_force_generic_unlocks_pool():
    _store().add("lin", "photo", "/d/g.jpg", "/static/g.jpg")  # 无触发词=通用池
    # 普通闲聊不解锁通用池
    assert ia.pick_registered_media(_cfg(enabled=True), "lin", "在吗") is None
    # force_generic（承诺兑现/offer-接受桥判定过）→ 通用池放开
    row = ia.pick_registered_media(
        _cfg(enabled=True), "lin", "在吗", force_generic=True)
    assert row and row["url"] == "/static/g.jpg"


# ── 承诺兑现（assume_intent）+ offer-accept 桥 ──────────────────────────────
async def test_run_assume_intent_selfie_sends_without_peer_keywords():
    """兑现路径：客户文本无任何要图关键词，assume_intent="selfie"（出站文本承诺
    了照片）仍走自拍链——注册相册通用池直接命中秒发。"""
    _store().add("lin", "photo", "/d/pr.jpg", "/static/pr.jpg")  # 通用池
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        _cfg(enabled=True), "telegram", "acctPr", "cPr", "lin",
        "今天心情不错", [], send_fn=send_fn, assume_intent="selfie")
    assert ok is True and sent[0]["url"] == "/static/pr.jpg"


async def test_run_assume_intent_generation_fallback(tmp_path, monkeypatch):
    """兑现路径回落生成：无注册相册时 assume_intent 直接构造 selfie directive
    （跳过 plan_autosend_image 的 peer_text 判定）。"""
    album = tmp_path / "album"
    album.mkdir()
    (album / "a.png").write_bytes(b"\x89PNGdummy")
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda *a, **k: ("/tmp/out.png", "/static/out.png", "image"))
    cfg = _cfg(enabled=True, provider={
        "enabled": True, "backend": "album", "album_dir": str(album)})
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        cfg, "telegram", "acctPrG", "cPrG", "lin",
        "刚下班好累呀", [], send_fn=send_fn, assume_intent="selfie")
    assert ok is True and sent[0]["url"] == "/static/out.png"


async def test_run_offer_accept_bridge_sends_on_short_affirmative():
    """offer-accept 桥：上一轮 AI 问「要不要我拍一张给你看」、本条客户只回
    「好呀」→ 视同要图请求（通用池命中）。"""
    _store().add("lin", "photo", "/d/oa.jpg", "/static/oa.jpg")
    hist = [
        {"role": "user", "content": "你长什么样呀"},
        {"role": "assistant", "content": "嘿嘿～要不要我拍一张给你看呀？"},
    ]
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        _cfg(enabled=True), "telegram", "acctOA", "cOA", "lin",
        "好呀", hist, send_fn=send_fn)
    assert ok is True and sent[0]["url"] == "/static/oa.jpg"


async def test_run_offer_accept_bridge_respects_flag_off():
    _store().add("lin", "photo", "/d/oa2.jpg", "/static/oa2.jpg")
    hist = [{"role": "assistant", "content": "要不要我拍一张给你看呀？"}]
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        _cfg(enabled=True, offer_accept_bridge=False), "telegram",
        "acctOA3", "cOA3", "lin", "好呀", hist, send_fn=send_fn)
    assert ok is False and sent == []


async def test_run_no_offer_no_bridge_for_plain_affirmative():
    """无 offer 前文时，「好呀」不触发发图（防误伤普通肯定回复）。"""
    _store().add("lin", "photo", "/d/oa4.jpg", "/static/oa4.jpg")
    hist = [{"role": "assistant", "content": "今天好热呀"}]
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_image(
        _cfg(enabled=True), "telegram", "acctOA5", "cOA5", "lin",
        "好呀", hist, send_fn=send_fn)
    assert ok is False and sent == []


def test_promise_metrics_record():
    before = int(ia.metrics_snapshot().get("promise_detected", 0) or 0)
    ia.record_promise_event("detected")
    ia.record_promise_event("fulfilled")
    snap = ia.metrics_snapshot()
    assert snap["promise_detected"] == before + 1
    assert int(snap.get("promise_fulfilled", 0)) >= 1


# ── metrics ────────────────────────────────────────────────────────────────
def test_metrics_record():
    before = int(ia.metrics_snapshot().get("sent", 0))
    ia.record_image_sent("selfie")
    snap = ia.metrics_snapshot()
    assert snap["sent"] == before + 1 and snap["last_kind"] == "selfie"
    fb = int(ia.metrics_snapshot().get("fallback", 0))
    ia.record_image_fallback("stage_failed")
    assert ia.metrics_snapshot()["fallback"] == fb + 1
    assert ia.metrics_snapshot()["fallback_reasons"].get("stage_failed") == 1
    ia.record_image_fallback("deliver_failed", detail="timeout 30s")
    assert ia.metrics_snapshot()["last_failure_detail"] == "timeout 30s"
