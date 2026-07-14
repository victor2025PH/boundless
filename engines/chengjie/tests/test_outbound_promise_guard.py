"""出站「媒体承诺」守卫（``src/ai/outbound_promise_guard.py``）门禁。

背景=实录事故：发图判定看客户入站关键词，LLM 出站文本却自己承诺「等我拍一张给你」，
两边从不核对 → 客户质问"你快拍啊是不是骗我"。守卫语义：**兑现优先，撤回兜底**。

覆盖：承诺检测（zh/en 正例 + 否认/远期/疑问/过去指涉反例）/ 句级剥离 /
重写指令 / 兜底话术语言对齐 / offer-accept 桥（短肯定 + 上一轮 offer）/
常见陪聊回复零误伤回归网。
"""
import pytest

from src.ai import outbound_promise_guard as pg


# ── detect_media_promise：正例（这些话若无图跟进=对客户撒谎，必须抓住）─────────
@pytest.mark.parametrize("text", [
    "好呀，等我拍一张给你～",
    "等我去拍哈，马上回来",
    "我这就去拍，你等着",
    "我马上拍一张发你",
    "拍一张给你看哈",
    "拍好了发你哦",
    "发你一张照片，看看嘛",
    "给你看张我的照片好啦",
    "自拍来啦！",
    "照片马上发你～",
    # 2026-07-14 真机漏报补网
    "来啦来啦，刚拍的～",
    "哈哈，这不已经发给你了嘛～",
    "I'll send you a photo in a sec",
    "let me take a selfie for you",
    "sending you a pic now~",
    "here's a selfie for you",
    "I'm gonna send you a picture",
])
def test_detect_image_promise_positive(text):
    assert pg.detect_media_promise(text) == "image"


@pytest.mark.parametrize("text", [
    "我录条语音给你哈",
    "给你发条语音，等下听",
    "等我发语音跟你说",
    "I'll send you a voice message",
])
def test_detect_voice_promise_positive(text):
    assert pg.detect_media_promise(text) == "voice"


# ── detect_media_promise：反例（这些话不许误伤——误伤会把正常回复剥掉）──────────
@pytest.mark.parametrize("text", [
    # 否认/拒绝：诚实表达，不是承诺
    "人家现在不方便拍照呢～",
    "我发不了照片啦，别闹",
    "sorry I can't send photos here",
    # 远期/条件：改天/下次类社交话术
    "改天拍给你看嘛",
    "下次见面我拍给你～",
    "有机会给你看我的照片呀",
    "maybe next time I'll send you a pic",
    # 疑问 offer：不是断言（由 offer-accept 桥接管）
    "要不要我拍一张给你呀？",
    "想看我的照片吗？",
    "want me to send you a selfie?",
    # 过去指涉：谈论已发生的照片
    "上次那张照片你还留着吗",
    "之前给你看的照片好看吧",
    # 普通闲聊：零相关
    "今天好累呀，你吃饭了没",
    "哈哈你太逗了",
    "我在拍视频素材呢，好忙",  # 自述在忙，无"给你"指向
    "你拍的照片真好看",        # 夸对方，不是承诺
    "把你的照片发我看看嘛",    # 让对方发，方向相反
])
def test_detect_promise_negative(text):
    assert pg.detect_media_promise(text) == ""


def test_detect_promise_question_sentence_skipped_but_statement_caught():
    # 同条消息：疑问句不算，但后面的陈述承诺要抓
    txt = "想看我的照片吗？等我拍一张给你！"
    assert pg.detect_media_promise(txt) == "image"


# ── Phase18 多语种承诺检测（宁漏勿误：只收宣告形）──────────────────────────────
@pytest.mark.parametrize("text", [
    "写真を送るね！",          # ja 宣告
    "今から撮るよ、待ってて",   # ja 即时
    "自撮り送っちゃう〜",      # ja
    "사진 보내줄게~",          # ko 宣告
    "셀카 지금 보낼게",        # ko
    "te mando una foto ahora",   # es
    "je t'envoie une photo",     # fr
    "vou te mandar uma selfie",  # pt
])
def test_detect_promise_multilingual_positive(text):
    assert pg.detect_media_promise(text) == "image"


@pytest.mark.parametrize("text", [
    "写真は送れないの、ごめんね",   # ja 否认
    "今度写真送るね！",             # ja 远期（今度=改天）
    "写真送ろうか？",               # ja offer 疑问
    "사진 못 보내 미안해",           # ko 否认
    "다음에 사진 보내줄게",          # ko 远期
    "mañana te mando una foto",     # es 远期
    "no puedo mandar fotos aquí",   # es 否认
    "quer que eu te mande uma foto?",  # pt offer 疑问
])
def test_detect_promise_multilingual_negative(text):
    assert pg.detect_media_promise(text) == ""


def test_deflection_line_multilingual_scripts():
    # 日文（含汉字+假名）→ ja 兜底而非误判 zh
    ja = pg.deflection_line("写真を送るね！", "image")
    assert "お" in ja or "〜" in ja
    ko = pg.deflection_line("사진 보내줄게", "image")
    assert any("\uac00" <= c <= "\ud7af" for c in ko)
    zh = pg.deflection_line("等我拍一张给你", "image")
    assert any("\u4e00" <= c <= "\u9fff" for c in zh)
    es = pg.deflection_line("te mando una foto", "image")
    assert es and not any("\u4e00" <= c <= "\u9fff" for c in es)
    # 兜底话术自身不得再构成即时承诺
    for t in (ja, ko, zh, es):
        assert pg.detect_media_promise(t) == ""


# ── strip_media_promises ─────────────────────────────────────────────────────
def test_strip_removes_promise_keeps_rest():
    txt = "今天好开心呀！等我拍一张给你～你吃饭了没？"
    out = pg.strip_media_promises(txt)
    assert "拍一张" not in out
    assert "今天好开心" in out and "吃饭" in out


def test_strip_whole_promise_becomes_empty():
    assert pg.strip_media_promises("等我拍一张给你哈～") == ""


def test_strip_no_promise_untouched():
    txt = "宝贝今天想我了没？我刚下班～"
    assert pg.strip_media_promises(txt) == txt


def test_strip_keeps_denial_sentences():
    # 否认句是诚实表达，绝不能被剥
    txt = "人家现在不方便拍照呢～多陪我聊聊嘛"
    assert pg.strip_media_promises(txt) == txt


# ── 重写指令 / 兜底话术 ──────────────────────────────────────────────────────
def test_rewrite_instruction_mentions_original_and_kind():
    ins = pg.build_promise_rewrite_instruction("等我拍一张给你", "image")
    assert "等我拍一张给你" in ins and "照片" in ins
    ins_v = pg.build_promise_rewrite_instruction("我录语音给你", "voice")
    assert "语音" in ins_v


def test_deflection_line_language_follows_sample():
    zh = pg.deflection_line("等我拍一张给你", "image")
    en = pg.deflection_line("I'll send you a pic", "image")
    assert any("\u4e00" <= c <= "\u9fff" for c in zh)
    assert not any("\u4e00" <= c <= "\u9fff" for c in en)
    # 兜底话术自身不得再构成即时承诺（否则守卫自己造谎）
    assert pg.detect_media_promise(zh) == ""
    assert pg.detect_media_promise(en) == ""


# ── offer-accept 桥 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("好呀", True), ("好啊好啊", True),  # 叠词短肯定（Phase8 事故用户原话风格）
    ("要", True), ("嗯嗯", True), ("可以呀", True),
    ("发吧", True), ("ok", True), ("sure", True), ("show me", True),
    ("好呀，不过你先告诉我你在哪上班", False),  # 带新话题的长句不算
    ("不要", False), ("算了吧", False), ("为什么要发", False),
])
def test_is_short_affirmative(text, expected):
    assert pg.is_short_affirmative(text) is expected


def test_detect_media_offer():
    assert pg.detect_media_offer("要不要我拍一张给你看呀？") == "image"
    assert pg.detect_media_offer("想不想看我的照片～") == "image"
    assert pg.detect_media_offer("want me to send you a selfie?") == "image"
    assert pg.detect_media_offer("今天聊点什么好呢") == ""


def test_offer_accepted_end_to_end():
    hist = [
        {"role": "user", "content": "你长什么样呀"},
        {"role": "assistant", "content": "嘿嘿保密～要不要我拍一张给你看呀？"},
    ]
    assert pg.offer_accepted("好呀", hist) == "image"
    # 最近 assistant 无 offer → 不触发
    hist2 = [{"role": "assistant", "content": "今天好热呀"}]
    assert pg.offer_accepted("好呀", hist2) == ""
    # 客户长句 → 不触发（可能带新话题）
    assert pg.offer_accepted("好呀，但我先问你个事，你老家哪的", hist) == ""
    # 空历史 → 不触发
    assert pg.offer_accepted("好呀", []) == ""


# ── prompt 侧接线（ai_client._build_context_prompt）────────────────────────────
def _mk_ai(cfg):
    from types import SimpleNamespace
    from src.ai.ai_client import AIClient
    c = AIClient.__new__(AIClient)  # 绕过 __init__ 重依赖，只测纯 prompt 构建
    c.config = SimpleNamespace(config=cfg)
    return c


def test_context_prompt_media_hint_and_capability_boundary():
    base_ctx = {"last_message": "在吗"}
    # ① 上游 hint 注入 → 进 prompt（发图协同块）
    p = _mk_ai({"companion": {"selfie": {"enabled": True}}})._build_context_prompt(
        dict(base_ctx, _media_coherence_hint="对方在要照片，本轮发不出，别承诺。"))
    assert "发图协同" in p and "别承诺" in p
    # ② conversion 域 + selfie 开：默认 hybrid 模式 → LLM 主动决策协议块
    #   （photo_directive 决策权上移）；intent.mode=keyword 回退 → 旧被动声明。
    p2 = _mk_ai({"domain": "conversion", "companion": {
        "selfie": {"enabled": True}}})._build_context_prompt(dict(base_ctx))
    assert "发照片能力" in p2 and "[PHOTO" in p2
    p2k = _mk_ai({"domain": "conversion", "companion": {
        "selfie": {"enabled": True,
                   "intent": {"mode": "keyword"}}}})._build_context_prompt(
        dict(base_ctx))
    assert "媒体能力边界" in p2k
    # ③ selfie 关 → 无声明/无协议（没有发图能力时这段是噪声）
    p3 = _mk_ai({"domain": "conversion", "companion": {
        "selfie": {"enabled": False}}})._build_context_prompt(dict(base_ctx))
    assert "媒体能力边界" not in p3 and "发照片能力" not in p3
    # ④ capability_hint 配置可关
    p4 = _mk_ai({"domain": "conversion", "companion": {
        "selfie": {"enabled": True},
        "media_promise_guard": {"capability_hint": False}}})._build_context_prompt(
        dict(base_ctx))
    assert "媒体能力边界" not in p4 and "发照片能力" not in p4
    # ⑤ 有具体 hint 时不叠加常驻声明（hint 更具体，避免指令冗余）
    p5 = _mk_ai({"domain": "conversion", "companion": {
        "selfie": {"enabled": True}}})._build_context_prompt(
        dict(base_ctx, _media_coherence_hint="X提示X"))
    assert "X提示X" in p5 and "媒体能力边界" not in p5 and "发照片能力" not in p5


def test_context_prompt_scene_state_and_media_log_blocks():
    """Phase18：场景状态与已发媒体日志块进 prompt（仅陪伴域）。"""
    cfg = {"domain": "conversion", "companion": {"selfie": {"enabled": True}}}
    ctx = {
        "last_message": "在吗",
        "_current_scene_note": "【你此刻的状态（内部设定）】你现在的场景：in a cozy cafe。\n仅当对方问起时提到。",
        "_media_sent_note": "【你最近发过的照片（事实）】\n- 07-14 01:30 [图片] 刚拍的（场景：in a cozy cafe）",
    }
    p = _mk_ai(cfg)._build_context_prompt(dict(ctx))
    assert "你此刻的状态" in p and "cozy cafe" in p
    assert "你最近发过的照片" in p
    # 非陪伴域（如 payment/客服）不注入场景状态
    p2 = _mk_ai({"domain": "support", "companion": {
        "selfie": {"enabled": True}}})._build_context_prompt(dict(ctx))
    assert "你此刻的状态" not in p2
