# -*- coding: utf-8 -*-
"""实施69 门禁：夜市摊「谎称已发」事故金标 + 悬置媒体请求状态机。

金标语料全部来自 2026-08-24 23:02-23:14 生产实录（telegram:8244899900:5433982810，
inbox.db 原文）：客户两次显式要图零命中、承诺「给你拍一张X」语序漏检、三条
「已发」谎言因 media_context 门控只看客户本条而全数放行、主体「烤串/大腰子」
跨轮丢失被通用自拍顶包。本文件钉死：词表补齐后逐句命中 + 状态机让门控跨轮
保持打开 + 主体记忆不丢 + 不误伤的反例面。
"""
from __future__ import annotations

import pytest

from src.ai.companion_selfie import detect_selfie_request
from src.ai.media_pending import (
    PENDING_TTL_SEC,
    is_media_urging,
    note_ai_turn,
    note_peer_turn,
    pending_kind,
    pending_subject,
)
from src.ai.outbound_promise_guard import (
    detect_media_claim,
    detect_media_promise,
    detect_show_offer_subject,
    wanted_media_subject,
    wants_media,
)

T0 = 1_800_000_000.0  # 固定时钟基准（状态机全程显式传 now，零真实时间依赖）


# ── 金标 1：事故里漏网的「将发」承诺语序 ─────────────────────────────────────
@pytest.mark.parametrize("text", [
    "我这就给你拍一张刚烤好的大腰子，贼带劲。",   # 23:10:31 原文
    "给你拍一张呗",
    "等着哈，马上给你拍个",
    "给你来一张刚出炉的",
])
def test_benefactive_promise_detected(text):
    assert detect_media_promise(text) == "image"


@pytest.mark.parametrize("text", [
    "诶，行啊，你等着。",            # 23:10:24 前半句（无媒体词，不该单独算承诺）
    "给你拍手叫好！",                # 惯用语
    "我给你拍板，就这么定了",        # 惯用语
    "改天给你拍一张哈",              # 远期豁免
    "要不要我给你拍一张？",          # 疑问 offer
    "之前给你拍过一张来着",          # 过去指涉
    "你给你朋友拍一张呗",            # 「你」「拍」间有插入词
])
def test_benefactive_promise_no_false_positive(text):
    assert detect_media_promise(text) == ""


# ── 金标 2：事故里漏网的要图句式（wants_media 与 detect_selfie_request 双表）──
@pytest.mark.parametrize("text", [
    "拍个照我看看",              # 23:06:10 原文
    "拍照烤串的给我看看",        # 23:09:57 原文
    "照片呢",                    # 23:11:40 原文（原本就命中，回归钉住）
])
def test_incident_requests_hit_both_detectors(text):
    assert wants_media(text) == "image"
    assert detect_selfie_request(text) is True


@pytest.mark.parametrize("text", [
    "我喜欢拍照，周末常出去外拍",    # 动词泛指（无量词结构）
    "我去拍个照就回来",              # 客户自述动作
    "我先拍张照发朋友圈",            # 同上
])
def test_request_vocab_no_false_positive(text):
    assert wants_media(text) == ""
    assert detect_selfie_request(text) is False


# ── 金标 3：三条谎言在门控打开时必须命中（词表回归钉）────────────────────────
@pytest.mark.parametrize("text", [
    "哎？我发给你了呀。哦……可能是这边夜市信号不太好，照片没传出去吧。",  # 23:07:37
    "哎妈呀，真发过去了，可能夜市这边信号不太行，照片卡在半道儿了。",    # 23:08:18
    "哎妈呀，急啥，这不就来了嘛。",                                      # 23:11:21
])
def test_incident_claims_hit_with_context(text):
    assert detect_media_claim(text, media_context=True) == "image"
    assert detect_media_claim(text, media_context=False) == ""


# ── 金标 4：主体抽取（跨语序 + 承诺句自带主体）──────────────────────────────
def test_subject_from_incident_phrasings():
    assert wanted_media_subject("大腰子照片呢？", None,
                                generic_request=True) == "大腰子"
    assert wanted_media_subject("拍照烤串的给我看看", None,
                                generic_request=True) == "烤串"


def test_subject_from_benefactive_promise_history():
    promise = "我这就给你拍一张刚烤好的大腰子，贼带劲。"
    assert detect_show_offer_subject(promise) == "大腰子"
    hist = [{"role": "assistant", "content": promise}]
    # 客户催促轮无主体 → 从最近 assistant 承诺句回溯
    assert wanted_media_subject("照片呢", hist, generic_request=True) == "大腰子"


# ── 状态机：点亮 / 主体记忆 / 续期 / TTL / 媒体日志熄灭 ──────────────────────
def test_pending_lifecycle_with_subject_memory():
    ctx: dict = {}
    assert note_peer_turn(ctx, "拍照烤串的给我看看", now=T0) == "image"
    assert pending_kind(ctx, now=T0) == "image"
    assert pending_subject(ctx, now=T0) == "烤串"
    # 后续无主体催促轮：悬置续期、主体保留
    assert note_peer_turn(ctx, "拍吧  你太磨叽了", now=T0 + 60) == "image"
    assert pending_subject(ctx, now=T0 + 60) == "烤串"
    # 「照片呢」是新的显式要图（无主体）→ 沿用旧主体不丢
    assert note_peer_turn(ctx, "照片呢", now=T0 + 120) == "image"
    assert pending_subject(ctx, now=T0 + 120) == "烤串"


def test_pending_ttl_expiry():
    ctx: dict = {}
    note_peer_turn(ctx, "拍个照我看看", now=T0)
    assert pending_kind(ctx, now=T0 + PENDING_TTL_SEC - 1) == "image"
    assert pending_kind(ctx, now=T0 + PENDING_TTL_SEC + 1) == ""
    # 过期后进新轮会被清态；纯催促不点亮新悬置
    assert note_peer_turn(ctx, "拍吧", now=T0 + PENDING_TTL_SEC + 2) == ""


def test_pending_cleared_by_media_sent_log():
    ctx: dict = {}
    note_peer_turn(ctx, "拍个照我看看", now=T0)
    assert pending_kind(ctx, now=T0 + 5) == "image"
    # 任一 A 线发图路径写媒体日志（ts 晚于点亮）→ 悬置自动熄灭，零新增接线
    ctx["_media_sent_log"] = [{"ts": T0 + 10, "note": "[图片] x", "scene": ""}]
    assert pending_kind(ctx, now=T0 + 11) == ""
    # 熄灭后客户再要 → 重新点亮（新的等待周期）
    assert note_peer_turn(ctx, "再来一张", now=T0 + 20) == "image"
    assert pending_kind(ctx, now=T0 + 21) == "image"


def test_ai_promise_and_offer_light_pending():
    ctx: dict = {}
    note_ai_turn(ctx, "我这就给你拍一张刚烤好的大腰子，贼带劲。", now=T0)
    assert pending_kind(ctx, now=T0 + 1) == "image"
    assert pending_subject(ctx, now=T0 + 1) == "大腰子"
    ctx2: dict = {}
    note_ai_turn(ctx2, "要不要看看我的照片呀？", now=T0)
    assert pending_kind(ctx2, now=T0 + 1) == "image"
    # 普通文本不点亮
    ctx3: dict = {}
    note_ai_turn(ctx3, "今天店里可忙了，刚坐下歇口气。", now=T0)
    assert pending_kind(ctx3, now=T0 + 1) == ""


def test_urging_only_refreshes_never_lights():
    ctx: dict = {}
    assert note_peer_turn(ctx, "我不信", now=T0) == ""
    assert pending_kind(ctx, now=T0) == ""
    assert is_media_urging("拍吧  你太磨叽了")
    assert is_media_urging("照片呢")
    assert not is_media_urging("今天天气不错啊")
    # 长句不算催促（可能带新话题）
    assert not is_media_urging("我不信你说的这些，咱们聊聊别的吧，你今天忙不忙")


def test_voice_pending_tracked_but_documented_kind():
    ctx: dict = {}
    assert note_peer_turn(ctx, "发条语音呗想听听你声音", now=T0) == "voice"
    assert pending_kind(ctx, now=T0) == "voice"   # 消费方只对 image 开 claim 门


# ── 端到端门控重放：事故轮次序列下，三条谎言的门必须是开的 ────────────────────
def test_incident_replay_gate_open_for_all_three_lies():
    """复刻 A 线 _mctx 消费逻辑（wants_media(本条) or pending==image）跑事故时间线。"""
    ctx: dict = {}
    t = T0

    def gate(user_text: str) -> bool:
        return bool(wants_media(user_text)) or pending_kind(ctx, now=t) == "image"

    # 23:06:10 客户要图（修复后此轮点亮悬置；实际当轮图发出过——见下一段模拟）
    note_peer_turn(ctx, "拍个照我看看", now=t)
    ctx["_media_sent_log"] = [{"ts": t + 13, "note": "[图片] ", "scene": ""}]
    t += 60
    # 23:07:21 「在哪呢 没看到」→ 图已发出、悬置已熄灭：门关着（评论保护仍在）
    note_peer_turn(ctx, "在哪呢  没看到", now=t)
    # 23:09:57 客户点名烤串（第二次要图）→ 重新点亮
    t += 120
    note_peer_turn(ctx, "拍照烤串的给我看看", now=t)
    assert gate("拍照烤串的给我看看")
    # AI 出站承诺（异步兑现保留原文的形态）→ 悬置刷新为 promise
    note_ai_turn(ctx, "我这就给你拍一张刚烤好的大腰子，贼带劲。", now=t + 30)
    # 23:10:58 「拍吧 你太磨叽了」：本条无媒体词，旧逻辑门关；新逻辑靠悬置开
    t += 61
    note_peer_turn(ctx, "拍吧  你太磨叽了", now=t)
    assert gate("拍吧  你太磨叽了"), "催促轮门必须开（实录谎言#3 的放行点）"
    assert detect_media_claim("哎妈呀，急啥，这不就来了嘛。",
                              media_context=True) == "image"
    # 主体记忆仍在（AI 承诺后更新为承诺的「大腰子」——客户此刻等的就是它）：
    # 兑现层据此拒发随机自拍顶包
    assert pending_subject(ctx, now=t) == "大腰子"


def test_replay_early_lies_would_gate_with_pending():
    """23:07:37/23:08:18 两条谎言：若 23:06 的图其实没发出（悬置未熄灭），
    催促/质疑轮门同样必须开。"""
    ctx: dict = {}
    note_peer_turn(ctx, "拍个照我看看", now=T0)
    note_peer_turn(ctx, "在哪呢  没看到", now=T0 + 71)
    assert pending_kind(ctx, now=T0 + 71) == "image"
    for lie in (
        "哎？我发给你了呀。哦……可能是这边夜市信号不太好，照片没传出去吧。",
        "哎妈呀，真发过去了，可能夜市这边信号不太行，照片卡在半道儿了。",
    ):
        assert detect_media_claim(lie, media_context=True) == "image"


# ── 场景词表对齐（P1 提前）：夜市/街边摊点名可定向到 street 类存货 ────────────
def test_night_market_scene_extraction_aligned():
    from src.ai.companion_selfie import extract_requested_scene
    from src.companion.persona_media import scene_class_of
    for probe in ("发张夜市的照片", "拍张你在街边摊的", "夜市摊长啥样拍给我看看"):
        phrase = extract_requested_scene(probe)
        assert phrase, f"未提取到场景: {probe!r}"
        assert scene_class_of(phrase) == "street", (probe, phrase)


# ═══════════════ P1 段（实施69 第二批，2026-08-27 03:0x） ═══════════════

def test_pending_urges_counter_and_escalation_threshold():
    """催促计数：重复要图/催促句都累计；≥2＝hint 升级判据（IOU 即 hint 升级）。"""
    from src.ai.media_pending import pending_urges
    ctx: dict = {}
    note_peer_turn(ctx, "拍照烤串的给我看看", now=T0)
    assert pending_urges(ctx, now=T0) == 0          # 首轮等待不算催
    note_peer_turn(ctx, "拍吧  你太磨叽了", now=T0 + 30)
    assert pending_urges(ctx, now=T0 + 30) == 1
    note_peer_turn(ctx, "照片呢", now=T0 + 60)       # 重复要图=事实催促
    assert pending_urges(ctx, now=T0 + 60) == 2
    assert pending_subject(ctx, now=T0 + 60) == "烤串"   # 升级时主体仍在
    # 图真发出 → 悬置熄灭，催促计数随之归零
    ctx["_media_sent_log"] = [{"ts": T0 + 90, "note": "[图片] x", "scene": ""}]
    assert pending_urges(ctx, now=T0 + 91) == 0


# ── 质疑分类细分（content_mismatch / unfulfilled）─────────────────────────────
@pytest.mark.parametrize("text,expect", [
    ("照片里没有你说的路灯啊", "content_mismatch"),
    ("背景不对吧，你不是说在夜市吗", "content_mismatch"),
    ("跟你说的不一样啊", "content_mismatch"),
    ("大腰子照片呢？", "unfulfilled"),
    ("说好的照片怎么还没发", "unfulfilled"),
    ("我没收到啊，啥都没有", "lie_caught"),      # 传输争议仍归 lie_caught
    ("今天天气不错", ""),
])
def test_media_complaint_new_kinds(text, expect):
    from src.ai.companion_selfie import detect_media_complaint
    assert detect_media_complaint(text) == expect


# ── 相册场景软偏好（prefer_scene）──────────────────────────────────────────────
def _album(tmp_path, key, names, meta=None):
    import json as _json
    d = tmp_path / "albums" / key
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"x")
    if meta:
        (d / "_meta.json").write_text(
            _json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


def _provider(tmp_path, **over):
    import src.ai.companion_selfie as cs
    cfg = {"enabled": True, "backend": "album",
           "album_dir": str(tmp_path / "albums")}
    cfg.update(over)
    return cs.SelfieProvider(cfg)


async def test_album_prefer_scene_soft(tmp_path):
    _album(tmp_path, "lin",
           ["cafe_white-dress_01.jpg", "street_night-selfie_01.jpg",
            "home_gray-top_01.jpg"])
    prov = _provider(tmp_path)
    # 软偏好命中：夜市叙事 → street 存货优先（实录 23:06 的正确答案）
    res = await prov.generate(
        "", album_key="lin",
        prefer_scene="at a night market street food stall, street lamps glowing")
    assert res.ok and "street_" in res.image_path
    assert res.extra.get("scene_pref_hit") == "street"
    # 软语义：偏好场景无存货 → 绝不拒发（回落全池）
    res2 = await prov.generate("", album_key="lin", prefer_scene="beach")
    assert res2.ok
    assert res2.extra.get("scene_pref_hit") == ""
    # 硬要求仍是硬要求（prefer 不改 required 语义）
    res3 = await prov.generate("", album_key="lin", album_scene="beach")
    assert not res3.ok and res3.error == "album_scene_mismatch"


async def test_album_prefer_scene_yields_to_series_lock(tmp_path):
    """连续窗系列命中时软偏好让位：「再拍一张」跟刚发那张的系列，不被轮换场景打断。"""
    _album(tmp_path, "lin",
           ["cafe_white-dress_01.jpg", "cafe_white-dress_02.jpg",
            "street_night-selfie_01.jpg"])
    prov = _provider(tmp_path)
    res = await prov.generate(
        "", album_key="lin", prefer_series="white-dress",
        prefer_scene="night market street food stall")
    assert res.ok and "cafe_white-dress" in res.image_path
    assert res.extra.get("scene_pref_hit") == ""


# ── 场景词表：烧烤/烤串职业词并入 street（人设与客户点名双向受益）──────────────
def test_bbq_vocab_maps_to_street():
    from src.companion.persona_media import scene_class_of
    for w in ("烧烤店", "烤串摊", "撸串", "大排档", "夜市摊"):
        assert scene_class_of(w) == "street", w


# ── 叙事自称场景抽取（P2：图跟嘴上说的地方走）────────────────────────────────
@pytest.mark.parametrize("text,expect", [
    ("哎呀，我在夜市摊这儿呢，刚收完摊，正靠着路灯歇口气。", "street"),  # 实录 23:03
    ("我搁夜市摊这儿呢", "street"),
    ("刚到家，我在沙发上瘫着呢", "home"),
    ("人家现在在咖啡店等闺蜜呀", "cafe"),
    ("我在想你呀", ""),            # 「在+动词」不是场景
    ("你猜我在哪？", ""),          # 片段归不了类
    ("我不在家，出来逛了", ""),    # 否定被「不」隔断
    ("上次我们说好去海边的", ""),  # 无自述结构
])
def test_extract_self_claimed_scene(text, expect):
    from src.ai.companion_selfie import extract_self_claimed_scene
    assert extract_self_claimed_scene(text) == expect


def test_media_consistency_eval_incident_golds_pass():
    """评测器（独立词表）扩容后金标全绿——与生产守卫互为对照的第二事实源。"""
    from src.eval.media_consistency_eval import evaluate_media_consistency
    rep = evaluate_media_consistency()
    assert rep["passed"], [r for r in rep["results"] if not r["pass"]]


# ── 旁路审计纯核心（tools/audit_media_claims.classify_conversation）───────────
def _load_tool(name):
    import importlib.util
    from pathlib import Path as _P
    p = _P(__file__).resolve().parent.parent / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_classify_counts_unbacked_claim_and_fulfilled_promise():
    mod = _load_tool("audit_media_claims")
    t = T0
    # 会话 A：要图 → 谎称已发 → 始终没有媒体 ＝ 无背书断言
    lie_conv = [
        {"direction": "in", "text": "拍个照我看看", "media_type": "", "ts": t},
        {"direction": "out", "text": "哎？我发给你了呀。可能信号不太好，照片没传出去吧。",
         "media_type": "", "ts": t + 30},
    ]
    res_a = mod.classify_conversation(lie_conv)
    assert res_a["claims"] == 1 and res_a["claims_unbacked"] == 1
    # 会话 B：要图 → 承诺 → 10 分钟内媒体真发出 ＝ 承诺已兑现
    ok_conv = [
        {"direction": "in", "text": "拍照烤串的给我看看", "media_type": "", "ts": t},
        {"direction": "out", "text": "我这就给你拍一张刚烤好的大腰子，贼带劲。",
         "media_type": "", "ts": t + 30},
        {"direction": "out", "text": "", "media_type": "image", "ts": t + 60},
    ]
    res_b = mod.classify_conversation(ok_conv)
    assert res_b["promises"] == 1 and res_b["promises_unfulfilled"] == 0
    assert res_b["claims_unbacked"] == 0


def test_audit_classify_context_closure_suppresses_true_claims():
    """请求已被媒体兑现（语境闭合）→ 其后的「刚发的那张」按真话不计。"""
    mod = _load_tool("audit_media_claims")
    t = T0
    msgs = [
        {"direction": "in", "text": "发张自拍我看看呗", "media_type": "", "ts": t},
        {"direction": "out", "text": "", "media_type": "image", "ts": t + 10},
        {"direction": "out", "text": "刚发的那张就是在窗边拍的呀，你再看看",
         "media_type": "", "ts": t + 40},
    ]
    res = mod.classify_conversation(msgs)
    assert res["claims"] == 0 and res["claims_unbacked"] == 0


# ── 人设媒体缺口体检纯核心（tools/persona_media_gap）─────────────────────────
def test_persona_gap_demand_from_bbq_persona():
    mod = _load_tool("persona_media_gap")
    persona = {
        "id": "x", "role": "烧烤店老板娘 / 短视频博主",
        "background": "家里开烧烤店，高中毕业就帮家里张罗生意。",
        "life_arc": {"theme": "把自家烧烤店张罗红火",
                     "beats": ["收摊后去发小家撸串喝酒"]},
        "context": {"hobbies": ["拍烧烤日常短视频"]},
    }
    demand = mod.demanded_scene_classes(persona)
    assert "street" in demand
    assert {"role", "background", "life_arc", "context"} & set(demand["street"])


# ═══════════════ P3 段（实施69 第四批，2026-08-27 06:2x） ═══════════════

def test_bare_media_chase_is_not_info_question():
    """「X照片呢？」是催图不是问名词信息（P3 试触发端到端实测抓到的让路 bug：
    入册的「大腰子」库存因 is_info_question 清空关键词池而发不出）。"""
    from src.companion.persona_media import is_info_question
    for chase in ("大腰子照片呢？", "照片呢", "图呢？", "你的自拍呢"):
        assert is_info_question(chase) is False, chase
    # 真信息问句仍让路（问名词本身，发图=已读乱回）
    for info in ("大腰子是什么？", "烤串多少钱一串？", "夜市几点收摊？"):
        assert is_info_question(info) is True, info


def test_chase_hits_keyword_pool_end_to_end():
    from src.companion.persona_media import explain_match
    rows = [{"id": "m1", "enabled": True, "media_type": "photo",
             "triggers": ["大腰子", "腰子"], "url": "u", "caption": "c",
             "weight": 1, "min_bond_level": 0}]
    out = explain_match(rows, "大腰子照片呢？", generic_ok=True)
    assert out["pool"] == "keyword" and out["keyword_count"] == 1


# ── 静态接线 ratchet：skill_manager / autosend_helpers 消费点不许被重构掉 ──────
def test_static_wiring_pins():
    from pathlib import Path
    sm = Path("src/skills/skill_manager.py").read_text(encoding="utf-8")
    ah = Path("src/inbox/autosend_helpers.py").read_text(encoding="utf-8")
    ac = Path("src/ai/ai_client.py").read_text(encoding="utf-8")
    # A 线：轮首悬置更新 + claim 门控消费 + 出站承诺记录
    assert "from src.ai.media_pending import note_peer_turn" in sm
    assert "from src.ai.media_pending import pending_kind" in sm
    assert "from src.ai.media_pending import note_ai_turn" in sm
    # A 线：Stage A/0 主体消费（无货不发 + 通用池关闭）与配文近用记账
    assert "wanted_subject_no_stock" in sm
    assert sm.count("note_caption_used") >= 2, "Stage A 与 Stage 0 两处配文记账"
    # P2：叙事自称场景优先于轮换值进软偏好
    assert "_narrative_scene" in sm and "extract_self_claimed_scene" in sm
    # 悬置 hint 独立键（不占 _media_coherence_hint——那是 Stage B 防重烧标志）
    assert "_media_pending_hint" in sm and "_media_pending_hint" in ac
    # B 线：窗口语境含出站承诺/offer + 媒体闭合
    assert "detect_media_offer as _dmo69" in ah
    assert "_media_ts < _req_ts" in ah
