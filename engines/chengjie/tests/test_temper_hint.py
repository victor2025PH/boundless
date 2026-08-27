# -*- coding: utf-8 -*-
"""人设脾气分级门禁（2026-08-12「骂人只会客服腔安抚」实录）。

不变量：
- ``detect_insult`` 只认**指向你本人**的高置信辱骂；自嘲/骂第三方（倾诉）
  一律放行走原共情路径——误报比漏报危害大（把倾诉当挑衅怼回去=事故）；
- ``persona_temper``：顶层 ``temper`` → ``personality.temper`` → gentle 默认；
  非法值回落 gentle；
- ``build_temper_hint``：两档都禁客服腔/卑微道歉；feisty＝直接骂回去、
  强度不低于对方、可带脏字、不给台阶（2026-08-12 运营要求「像真人、够冲」，
  去掉旧三条软化红线），仅保封号级底线（不威胁人身安全/不用群体歧视词）；
  zh/en 双版；
- ai_client 消费口：``_temper_hint`` 在 context 里即进 prompt（接线契约）。
"""
from __future__ import annotations

from src.companion.temper import (
    DEFAULT_MAX_ROUNDS,
    STICKY_WINDOW_SEC,
    TEMPER_FEISTY,
    TEMPER_GENTLE,
    TEMPER_OFF,
    TEMPER_SHARP,
    apply_platform_cap,
    build_feud_break_hint,
    build_taunt_hint,
    build_temper_hint,
    detect_insult,
    detect_taunt,
    is_de_escalation,
    looks_hostile,
    parse_temper_cfg,
    persona_temper,
    persona_temper_raw,
    record_deescalation,
    record_feud_break,
    record_hint,
    record_insult,
    record_platform_capped,
    record_sticky_hit,
    record_suppressed_off,
    record_taunt,
    resolve_temper_level,
    temper_stats_reset,
    temper_stats_snapshot,
)


# ── detect_insult ───────────────────────────────────────────────────────────
def test_detect_insult_positive():
    for t in ("你是个大傻子", "你真是个白痴", "你有病吧", "你个骗子",
              "傻逼", "滚", "滚蛋", "你怎么这么恶心",
              "fuck you", "you idiot", "stupid bot", "stfu"):
        assert detect_insult(t), t


def test_detect_insult_standalone_expanded_lexicon():
    """2026-08-12 实测漏网补收：大傻子/笨蛋/蠢货/混蛋/你妹（的）。"""
    for t in ("大傻子", "笨蛋", "蠢货", "混蛋", "王八蛋",
              "你妹的", "你妹", "你妹的!"):
        assert detect_insult(t), t


def test_detect_insult_multiline_merged_burst():
    """逐字连发合并成多行文本后按行判（碎片拼句产物）——
    「你妹的\\n最近有啥新闻\\n大傻子\\n说话」应命中。"""
    merged = "你妹的\n最近有啥新闻\n大傻子\n说话"
    assert detect_insult(merged)
    # 干净的多行合并文本不误报
    assert not detect_insult("在吗\n最近有啥新闻\n说话")


def test_detect_insult_cao_family():
    """2026-08-12 真机实录漏网：操你妈/操你大爷/操你妈逼 全没被识别 →
    掉回「咱俩好好聊」软话术。操/艹/草/日/干 全家族收进词表。"""
    for t in ("操你妈", "我想操你", "我想操你妈", "操你大爷", "操你妈逼",
              "艹你", "草你妈", "日你妈", "干你娘", "草泥马", "马勒戈壁",
              "你妈死了", "nmsl", "CNM", "你妈的", "你妈"):
        assert detect_insult(t), t


def test_detect_insult_sexual_aggression():
    """2026-08-12 二轮真机实录整类漏网：性骚扰/下流话不是经典骂人词，
    两张词表都没收 → 「尿你嘴里」得到的是「咱俩好好聊聊」客服腔。"""
    for t in ("尿你嘴里", "射你一脸", "来让我鸡巴插你嘴里", "插你嘴",
              "扣你小逼逼", "吐你一脸", "捅你", "肏"):
        assert detect_insult(t), t


def test_detect_insult_echo_swear_and_slur_line():
    """接尾骂（溜你妈）与不带「你」的当面骂短行（拉呀，臭婊子）。"""
    for t in ("溜你妈", "滚你妈", "聊你妈", "拉呀，臭婊子", "臭婊子",
              "骚货", "死贱人"):
        assert detect_insult(t), t


def test_detect_insult_sexual_benign_exempt():
    """注射/辐射/牛逼(夸)/逼我(动词)/逼真/逼格/你妈妈 等正常话不误伤。"""
    for t in ("护士要注射你的疫苗了", "你真牛逼啊", "你别逼我",
              "你画得好逼真", "你们公司逼格挺高", "我见过你妈",
              "想你妈了没", "我室友真是个婊子"):
        assert not detect_insult(t), t


def test_detect_insult_cao_benign_exempt():
    """操场/体操/做操 的「操」邻接不误报；「你妈的」仅独立成句才算。"""
    for t in ("我去操场跑步", "帮我操作一下电脑", "做早操你去吗",
              "课间做操你来不来", "你妈的手艺真好", "你妈妈做的菜真香"):
        assert not detect_insult(t), t


# ── 骂战粘性窗（looks_hostile / is_de_escalation）───────────────────────────
def test_looks_hostile_wide_net():
    """窗内宽表：词表外变体骂法也要接得住（打地鼠漏网的兜底）。"""
    for t in ("操你祖宗十八代", "你个老逼登", "滚远点", "恶心玩意",
              "没你嘴臭", "给你刷刷牙的骚话", "来让我爽爽", "滚犊子",
              "fuck off", "wtf is wrong with you"):
        assert looks_hostile(t), t


def test_looks_hostile_calm_and_deescalation_exempt():
    for t in ("今天天气不错", "你吃了吗", "", None,
              "对不起嘛别生气", "开玩笑的啦", "我错了还不行吗",
              "sorry sorry 逗你玩的"):
        assert not looks_hostile(t), t


def test_looks_hostile_long_text_exempt():
    assert not looks_hostile("操" + "啊" * 200)


def test_de_escalation_detection():
    for t in ("对不起", "抱歉抱歉", "开玩笑的", "别生气嘛", "我错了",
              "just kidding", "my bad"):
        assert is_de_escalation(t), t
    for t in ("操你妈", "你好啊", "", None):
        assert not is_de_escalation(t), t


def test_sticky_window_constant_sane():
    assert 60 <= STICKY_WINDOW_SEC <= 3600


def test_skill_manager_wires_sticky_window():
    """接线契约：粘性窗时间戳 + 收手清窗必须在 skill_manager 内。"""
    import inspect

    from src.skills import skill_manager as mod
    src = inspect.getsource(mod)
    assert "_insult_ts" in src, "skill_manager 未接骂战粘性窗"
    assert "is_de_escalation" in src, "skill_manager 未接收手清窗"
    assert "looks_hostile" in src


def test_detect_insult_affectionate_teasing_exempt():
    """「小笨蛋/小傻子」多为亲昵调侃，刻意不收进独立骂句。"""
    for t in ("小笨蛋", "小傻子", "你个小笨蛋"):
        assert not detect_insult(t), t


def test_detect_insult_nimei_literal_sister_exempt():
    """「你妹妹/把你妹叫来」是字面亲属指称，不是骂——lookahead 要求句尾/标点。"""
    for t in ("你妹妹今年多大了", "把你妹叫来一起玩", "你妹妹真可爱"):
        assert not detect_insult(t), t


def test_detect_insult_self_deprecation_exempt():
    # 自嘲=倾诉，不是骂你
    for t in ("我真是个傻子", "我就是个废物", "我可能是个笨蛋吧"):
        assert not detect_insult(t), t


def test_detect_insult_third_party_exempt():
    # 骂第三方=向你倾诉，走共情路径
    for t in ("我老板真是个傻子", "前男友就是个渣", "我同事是个白痴"):
        assert not detect_insult(t), t


def test_detect_insult_normal_chat_exempt():
    for t in ("今天好累啊", "你好笨重的行李怎么拿的",  # 「笨重」非骂词形态
              "在吗", "你吃了吗", "", None):
        assert not detect_insult(t), t


def test_detect_insult_long_text_exempt():
    # 超长文本（>200 字）不判——长文里偶现骂词多半在叙事
    assert not detect_insult("你是个大傻子" + "啊" * 200)


# ── 多语种词表（2026-08-12 扩语言面：触发词表原本只认中英）────────────────
def test_detect_insult_multilingual_positive():
    """日/韩/西/葡/法/德/俄/泰/越/印尼/印地/阿 高置信骂词全命中。"""
    for t in (
        "死ね", "くたばれ", "バカ野郎", "お前はバカだ",          # ja
        "씨발", "병신", "개새끼", "미친놈", "꺼져", "닥쳐",       # ko
        "hijo de puta", "vete a la mierda", "eres un idiota",
        "gilipollas", "pendejo",                                  # es
        "filho da puta", "vai se foder", "cala a boca",           # pt
        "connard", "ta gueule", "fils de pute",
        "va te faire foutre",                                     # fr
        "arschloch", "fick dich", "halt die fresse",              # de
        "иди на хуй", "пошёл нахуй", "мудак", "ты идиот",
        "заткнись", "сука",                                       # ru
        "ควย", "เหี้ย", "ไอ้สัส", "มึงโง่",                        # th
        "địt mẹ", "đồ ngu", "cút đi", "câm mồm",                  # vi
        "bangsat", "goblok", "tolol", "lu anjing", "dasar bego",  # id
        "madarchod", "bhenchod", "chutiya", "चूतिया",             # hi
        "يا حمار", "اخرس",                                        # ar
    ):
        assert detect_insult(t), t


def test_detect_insult_multilingual_benign_exempt():
    """轻度/正常用法不误伤：バカ单发（调侃）/바보（撒娇）/anjing 单词（狗）/
    блядь 口头语/普通外语寒暄。"""
    for t in (
        "バカ", "ばか", "바보야", "anjing itu lucu",   # 那只狗真可爱
        "суп очень вкусный", "hola como estas",
        "bonjour mon ami", "ich liebe dich",
        "con chó của tôi dễ thương",                  # 我的狗很可爱
        "estoy estudiando", "obrigado amigo",
    ):
        assert not detect_insult(t), t


def test_looks_hostile_multilingual_wide_net():
    """粘性窗宽表：窗内轻度外语骂法也接得住。"""
    for t in ("バカ", "うざい", "새끼야", "멍청이", "qué mierda",
              "дура", "โง่", "ngu quá", "bego banget", "साला"):
        assert looks_hostile(t), t


def test_de_escalation_multilingual():
    for t in ("ごめんね", "冗談だよ", "미안해", "장난이야",
              "perdón", "es broma", "désolé", "je rigole",
              "прости, шучу", "ขอโทษนะ", "xin lỗi nha",
              "maaf ya bercanda", "entschuldigung"):
        assert is_de_escalation(t), t


# ── 激将/挑衅（P2 2026-08-13，阿龙实录 22:03-22:05 缺口）────────────────────
def test_detect_taunt_positive():
    """「嫌你没脾气/逗你骂人」的挑衅形——含阿龙当晚原话。"""
    for t in ("你太没脾气了", "你怎么这么没脾气", "你是不是没脾气",
              "你没血性", "你怎么不骂我", "你咋不骂人",
              "你倒是骂我啊", "你不敢骂我", "有种骂我", "没种骂了吧",
              "骂我试试", "骂我一句", "你用语音骂我试下",
              "why don't you curse me", "dare you to insult me",
              "you are too soft"):
        assert detect_taunt(t), t


def test_detect_taunt_negative_exempt():
    """求饶/抗议/自述/第三方/正常聊天——绝不误判成激将（误伤=拽错人）。"""
    for t in ("你别骂我", "你骂我干嘛", "我太没脾气了",
              "我老板真是没脾气", "他从来不骂我", "别生我气嘛",
              "今天好累啊", "你吃了吗", "", None,
              "我妈说我脾气太好了"):
        assert not detect_taunt(t), t


def test_taunt_hint_bans_bait_and_service_tone():
    """接梗指令：绝不上钩开骂 + 绝不客服腔；off 档空串。"""
    for lv in (TEMPER_GENTLE, TEMPER_SHARP, TEMPER_FEISTY):
        zh = build_taunt_hint(lv, "zh")
        assert "激" in zh and "上钩" in zh, lv
        assert "客服" in zh, lv
        en = build_taunt_hint(lv, "en")
        assert "bait" in en.lower() and "customer" in en.lower(), lv
    # sharp/feisty 可以更冲但不先动脏字；gentle 俏皮打岔
    assert "不先动脏字" in build_taunt_hint(TEMPER_FEISTY, "zh")
    assert "俏皮" in build_taunt_hint(TEMPER_GENTLE, "zh")
    assert build_taunt_hint(TEMPER_OFF, "zh") == ""
    assert build_taunt_hint(TEMPER_OFF, "en") == ""


# ── 连怼熔断（P2）────────────────────────────────────────────────────────────
def test_feud_break_hint_walks_away_without_apology():
    """熔断收场：居高临下退场，绝不道歉/安抚/解释。"""
    zh = build_feud_break_hint("zh")
    assert "不奉陪" in zh or "收场" in zh
    assert "道歉" in zh and "安抚" in zh    # 显式点名禁令
    assert "认输" in zh                     # 语义：退场是赢不是怂
    en = build_feud_break_hint("en")
    assert "walk" in en.lower() and "apologize" in en.lower()


def test_default_max_rounds_sane():
    assert 1 <= DEFAULT_MAX_ROUNDS <= 10


# ── 平台封顶（P2）────────────────────────────────────────────────────────────
def test_apply_platform_cap():
    caps = {"messenger": "sharp", "line": "off"}
    # 高于上限 → 压到上限
    r = apply_platform_cap(TEMPER_FEISTY, "messenger", caps)
    assert (r["level"], r["capped"]) == (TEMPER_SHARP, True)
    # 等于/低于上限 → 不动
    r = apply_platform_cap(TEMPER_SHARP, "messenger", caps)
    assert (r["level"], r["capped"]) == (TEMPER_SHARP, False)
    r = apply_platform_cap(TEMPER_GENTLE, "messenger", caps)
    assert (r["level"], r["capped"]) == (TEMPER_GENTLE, False)
    # off 上限＝该平台全禁
    r = apply_platform_cap(TEMPER_GENTLE, "line", caps)
    assert (r["level"], r["capped"]) == (TEMPER_OFF, True)
    # 未配置平台 / 空 caps / 大小写 → 不动
    assert apply_platform_cap(TEMPER_FEISTY, "telegram", caps)["capped"] is False
    assert apply_platform_cap(TEMPER_FEISTY, "messenger", {})["capped"] is False
    assert apply_platform_cap(TEMPER_FEISTY, "MESSENGER", caps)["capped"] is True


def test_parse_temper_cfg_p2_keys():
    c = parse_temper_cfg(None)
    assert c["max_rounds"] == DEFAULT_MAX_ROUNDS
    assert c["taunt_response"] is True
    assert c["platform_caps"] == {}
    d = parse_temper_cfg({"companion": {"temper": {
        "max_rounds": 999, "taunt_response": False,
        "platform_caps": {"Messenger": "SHARP", "line": "rage", 7: "off"}}}})
    assert d["max_rounds"] == 20            # 夹界
    assert d["taunt_response"] is False
    assert d["platform_caps"] == {"messenger": "sharp", "7": "off"}
    assert parse_temper_cfg({"companion": {"temper": {
        "max_rounds": -3}}})["max_rounds"] == 0
    assert parse_temper_cfg({"companion": {"temper": {
        "max_rounds": "abc"}}})["max_rounds"] == DEFAULT_MAX_ROUNDS


# ── persona_temper ──────────────────────────────────────────────────────────
def test_persona_temper_resolution():
    assert persona_temper(None) == TEMPER_GENTLE
    assert persona_temper({}) == TEMPER_GENTLE
    assert persona_temper({"temper": "feisty"}) == TEMPER_FEISTY
    assert persona_temper({"temper": "FEISTY"}) == TEMPER_FEISTY
    assert persona_temper({"personality": {"temper": "feisty"}}) == TEMPER_FEISTY
    # 顶层优先于 personality
    assert persona_temper(
        {"temper": "gentle", "personality": {"temper": "feisty"}}
    ) == TEMPER_GENTLE
    # 非法值回落默认
    assert persona_temper({"temper": "rage"}) == TEMPER_GENTLE
    # 新档位被识别（2026-08-12 四档扩容）
    assert persona_temper({"temper": "sharp"}) == TEMPER_SHARP
    assert persona_temper({"temper": "off"}) == TEMPER_OFF


def test_persona_temper_raw_distinguishes_unset_from_gentle():
    """resolve 链的地基：「没配」（空串→落 default_level）≠「配了 gentle」。"""
    assert persona_temper_raw(None) == ""
    assert persona_temper_raw({}) == ""
    assert persona_temper_raw({"temper": "rage"}) == ""      # 非法=没配
    assert persona_temper_raw({"temper": "gentle"}) == TEMPER_GENTLE
    assert persona_temper_raw({"temper": "sharp"}) == TEMPER_SHARP


# ── companion.temper 治理层（parse + resolve 单一判定链）────────────────────
def test_parse_temper_cfg_defaults_preserve_live_behavior():
    """缺省=2026-08-12 上线行为：开、按人设、gentle 兜底、放开脏字、600s 窗。"""
    for cfg in (None, {}, {"companion": {}}, {"companion": {"temper": {}}}):
        c = parse_temper_cfg(cfg)
        assert c["enabled"] is True
        assert c["force_level"] == ""
        assert c["default_level"] == TEMPER_GENTLE
        assert c["profanity"] is True
        assert c["sticky_window_sec"] == STICKY_WINDOW_SEC


def test_parse_temper_cfg_overrides_and_dirty_values():
    c = parse_temper_cfg({"companion": {"temper": {
        "enabled": False, "force_level": "FEISTY",
        "default_level": "sharp", "profanity": False,
        "sticky_window_sec": 120}}})
    assert c["enabled"] is False
    assert c["force_level"] == TEMPER_FEISTY
    assert c["default_level"] == TEMPER_SHARP
    assert c["profanity"] is False
    assert c["sticky_window_sec"] == 120.0
    # 脏值回落：非法 force → 空（按人设）；非法 default → gentle；窗口夹界
    d = parse_temper_cfg({"companion": {"temper": {
        "force_level": "rage", "default_level": "rage",
        "sticky_window_sec": 999999}}})
    assert d["force_level"] == ""
    assert d["default_level"] == TEMPER_GENTLE
    assert d["sticky_window_sec"] == 3600.0
    assert parse_temper_cfg({"companion": {"temper": {
        "sticky_window_sec": 1}}})["sticky_window_sec"] == 60.0


def test_resolve_temper_level_priority_chain():
    """force > 人设字段 > default_level；source 标注给诊断器用。"""
    cfg_plain = parse_temper_cfg(None)
    feisty_p = {"temper": "feisty"}
    # ① 人设字段生效
    r = resolve_temper_level(cfg_plain, feisty_p)
    assert (r["level"], r["source"]) == (TEMPER_FEISTY, "persona")
    # ② 没配人设 → default_level
    cfg_sharp_default = parse_temper_cfg(
        {"companion": {"temper": {"default_level": "sharp"}}})
    r = resolve_temper_level(cfg_sharp_default, {})
    assert (r["level"], r["source"]) == (TEMPER_SHARP, "default")
    # ③ 人设显式 gentle 优先于 default_level（raw 区分的意义）
    r = resolve_temper_level(cfg_sharp_default, {"temper": "gentle"})
    assert (r["level"], r["source"]) == (TEMPER_GENTLE, "persona")
    # ④ force 压过一切（「骂人都必须怼」）
    cfg_force = parse_temper_cfg(
        {"companion": {"temper": {"force_level": "feisty"}}})
    r = resolve_temper_level(cfg_force, {"temper": "off"})
    assert (r["level"], r["source"]) == (TEMPER_FEISTY, "force")
    # ⑤ force=off 全员禁怼
    cfg_force_off = parse_temper_cfg(
        {"companion": {"temper": {"force_level": "off"}}})
    r = resolve_temper_level(cfg_force_off, feisty_p)
    assert (r["level"], r["source"]) == (TEMPER_OFF, "force")


def test_resolve_temper_level_profanity_cap():
    """profanity=false：feisty 封顶 sharp（不降凶只去脏），其余档不受影响。"""
    cfg = parse_temper_cfg(
        {"companion": {"temper": {"profanity": False}}})
    r = resolve_temper_level(cfg, {"temper": "feisty"})
    assert r["level"] == TEMPER_SHARP and r["profanity_capped"] is True
    r2 = resolve_temper_level(cfg, {"temper": "gentle"})
    assert r2["level"] == TEMPER_GENTLE and r2["profanity_capped"] is False
    # force=feisty 同样吃闸
    cfg_f = parse_temper_cfg({"companion": {"temper": {
        "force_level": "feisty", "profanity": False}}})
    r3 = resolve_temper_level(cfg_f, {})
    assert r3["level"] == TEMPER_SHARP and r3["profanity_capped"] is True


# ── build_temper_hint ───────────────────────────────────────────────────────
def test_hint_bans_customer_service_tone_both_tempers():
    for temper in (TEMPER_GENTLE, TEMPER_FEISTY):
        zh = build_temper_hint(temper, "zh")
        assert "客服" in zh and "道歉" in zh, temper
        en = build_temper_hint(temper, "en")
        assert "customer service" in en.lower(), temper


def test_feisty_hint_fires_back_full_strength():
    """2026-08-12 运营要求：对方先骂就直接骂回去，不软化不给台阶。"""
    zh = build_temper_hint(TEMPER_FEISTY, "zh")
    assert "骂回去" in zh                      # 直接回击而非「拌嘴」
    assert "强度不能低于对方" in zh            # 强度镜像
    assert "可以带脏字" in zh                  # 脏字放开
    assert "别主动找台阶" in zh                # 不再强制收场
    # 仅存的封号级底线：人身安全威胁 / 群体歧视词
    assert "威胁" in zh and "歧视" in zh
    # 旧软化红线必须已移除
    assert "不带脏字" not in zh
    en = build_temper_hint(TEMPER_FEISTY, "en")
    assert "fire right back" in en.lower()
    assert "profanity is allowed" in en.lower()


def test_gentle_hint_sets_boundary_not_soothing():
    zh = build_temper_hint(TEMPER_GENTLE, "zh")
    assert "委屈" in zh and "边界" in zh
    assert "理解你的感受" in zh                # 显式点名禁用句式


def test_hint_never_empty_for_valid_tempers():
    assert build_temper_hint("weird-value") != ""   # 未知档回落 gentle 文案


def test_sharp_hint_sarcasm_without_profanity():
    """sharp＝阴阳嘲讽气势不输，但明确禁脏字（脏字是对方的水平）。"""
    zh = build_temper_hint(TEMPER_SHARP, "zh")
    assert "阴阳" in zh and "嘲讽" in zh          # 核心武器
    assert "脏字都不带" in zh                     # 与 feisty 的分界线
    assert "客服" in zh and "道歉" in zh          # 客服腔禁令与其他档同强度
    assert "威胁" in zh and "歧视" in zh          # 封号级底线仍在
    en = build_temper_hint(TEMPER_SHARP, "en")
    assert "sarcasm" in en.lower()
    assert "without a single swear word" in en.lower()
    assert "customer service" in en.lower()


def test_off_hint_is_empty():
    """off 显式空串=不注入（商务人设）；与「未知值回落 gentle」互不混淆。"""
    assert build_temper_hint(TEMPER_OFF, "zh") == ""
    assert build_temper_hint(TEMPER_OFF, "en") == ""


# ── 观测计数器 ──────────────────────────────────────────────────────────────
def test_temper_stats_counters_roundtrip():
    temper_stats_reset()
    try:
        record_insult()
        record_insult()
        record_sticky_hit()
        record_deescalation()
        record_hint(TEMPER_FEISTY, persona_id="lin_xiaoyu", forced=True)
        record_hint(TEMPER_SHARP, persona_id="su_wan", capped=True)
        record_hint(TEMPER_GENTLE)
        record_suppressed_off()
        record_feud_break()
        record_taunt()
        record_platform_capped()
        s = temper_stats_snapshot()
        assert s["insults"] == 2
        assert s["sticky_hits"] == 1
        assert s["deescalations"] == 1
        assert s["hints"] == {"gentle": 1, "sharp": 1, "feisty": 1}
        assert s["forced"] == 1
        assert s["profanity_capped"] == 1
        assert s["suppressed_off"] == 1
        assert s["feud_breaks"] == 1
        assert s["taunts"] == 1
        assert s["platform_capped"] == 1
        assert s["by_persona"] == {"lin_xiaoyu": 1, "su_wan": 1}
        assert s["last_hit_ts"] > 0
        # 快照是拷贝：改它不脏源
        s["hints"]["feisty"] = 999
        assert temper_stats_snapshot()["hints"]["feisty"] == 1
    finally:
        temper_stats_reset()


def test_temper_stats_by_persona_capped():
    temper_stats_reset()
    try:
        for i in range(64):
            record_hint(TEMPER_GENTLE, persona_id=f"p{i}")
        assert len(temper_stats_snapshot()["by_persona"]) <= 32
    finally:
        temper_stats_reset()


# ── ai_client 消费接线契约 ──────────────────────────────────────────────────
def test_ai_client_consumes_temper_hint():
    import inspect

    from src.ai import ai_client as mod
    src = inspect.getsource(mod)
    assert "_temper_hint" in src, "ai_client 不再消费 _temper_hint（接线断了）"


def test_skill_manager_wires_temper():
    import inspect

    from src.skills import skill_manager as mod
    src = inspect.getsource(mod)
    assert "detect_insult" in src and "_temper_hint" in src, (
        "skill_manager 未接 temper 检测/注入")
    # 治理层接线（2026-08-12 P0）：配置闸 + 单一判定链 + 观测埋点
    assert "parse_temper_cfg" in src, "skill_manager 未接 companion.temper 配置闸"
    assert "resolve_temper_level" in src, "skill_manager 未走生效档位单一判定链"
    assert "record_hint" in src, "skill_manager 未接 temper 观测埋点"
    # P2 接线（2026-08-13）：连怼熔断轮数 + 激将接梗 + 平台封顶
    assert "_insult_streak" in src, "skill_manager 未接连怼轮数追踪"
    assert "build_feud_break_hint" in src, "skill_manager 未接熔断冷处理"
    assert "detect_taunt" in src, "skill_manager 未接激将检测"
    assert "apply_platform_cap" in src, "skill_manager 未接平台封顶"
    # 战线贯彻接线（2026-08-22 实施54）：streak 递进 + 情感块压制 + 骂战态登记
    assert "streak=_streak" in src, "skill_manager 未把 streak 传入 build_temper_hint"
    assert "record_escalated_hint" in src, "skill_manager 未接加码指令观测"
    assert "record_emo_block_suppressed" in src, (
        "skill_manager 未接情感块压制（playful 误读会与回怼指令同 prompt 打架）")
    assert "record_fight_turn" in src, "skill_manager 未登记骂战态（语音链读它）"
    assert "clear_fight_turn" in src, "skill_manager 未接收手清骂战态"


# ══════════════════════════════════════════════════════════════════════════
# 战线贯彻（2026-08-22 实施54）：03:44-03:48 实录——检测/注入 4/4 全命中，
# 但 ① 第 2 轮起 LLM 顺着「我睡觉去了」历史惯性自行降级、第 4 轮主动递台阶；
# ② 情感引擎把「来呀…傻逼玩意儿」判成 playful 0.97 与回怼指令同 prompt 打架；
# ③ 回怼文本被 TTS 渲染成 情绪happy（开心语调骂人=穿帮）。
# ══════════════════════════════════════════════════════════════════════════

# ── ① streak 递进加码 ────────────────────────────────────────────────────────
def test_hint_streak1_unchanged_from_legacy():
    """streak 缺省/1＝旧签名旧文案（字节级），不惊扰既有行为。"""
    for lv in (TEMPER_GENTLE, TEMPER_SHARP, TEMPER_FEISTY):
        for lang in ("zh", "en"):
            assert build_temper_hint(lv, lang) == build_temper_hint(
                lv, lang, streak=1), (lv, lang)


def test_hint_streak2_escalates_with_banned_retreat_lines():
    """≥2 轮：更凶递进 + 撤退话术逐句点名（抽象「别找台阶」模型不听，
    实录第 2 轮就说「懒得跟你吵，我睡觉去了」、第 4 轮「要不咱换个话题」）。"""
    for lv in (TEMPER_SHARP, TEMPER_FEISTY):
        zh = build_temper_hint(lv, "zh", streak=2)
        assert "第 2 轮" in zh, lv
        assert "更凶" in zh, lv
        for banned in ("懒得跟你吵", "我睡觉去了", "换个话题",
                       "你开心就好", "随便你", "不奉陪"):
            assert banned in zh, (lv, banned)   # 负面清单必须逐句点名
        assert "谁先怂谁输" in zh, lv
        assert "别复读" in zh or "换一个新的角度" in zh, lv
        en = build_temper_hint(lv, "en", streak=3)
        assert "round 3" in en.lower(), lv
        assert "banned" in en.lower(), lv
        assert "harder" in en.lower(), lv


def test_hint_streak_escalation_level_split():
    """feisty 加码钉死脏字非可选项；sharp 加码仍零脏字只加嘲讽烈度。"""
    fz = build_temper_hint(TEMPER_FEISTY, "zh", streak=2)
    assert "不是可选项" in fz
    sz = build_temper_hint(TEMPER_SHARP, "zh", streak=2)
    assert "脏字不带" in sz and "更扎心" in sz
    fe = build_temper_hint(TEMPER_FEISTY, "en", streak=2)
    assert "not optional" in fe.lower()
    se = build_temper_hint(TEMPER_SHARP, "en", streak=2)
    assert "zero swear words" in se.lower()


def test_hint_streak_gentle_and_off_not_escalated():
    """gentle 不加码（「继续骂则回更短更淡」本就是其延续语义——强逼软人设
    对轰=人设崩坏）；off 任何 streak 都空串。"""
    assert build_temper_hint(TEMPER_GENTLE, "zh", streak=5) == build_temper_hint(
        TEMPER_GENTLE, "zh", streak=1)
    assert build_temper_hint(TEMPER_OFF, "zh", streak=5) == ""


def test_hint_streak_dirty_values_fall_back_to_1():
    for dirty in (0, -3, None, "abc"):
        assert build_temper_hint(TEMPER_FEISTY, "zh", streak=dirty) == (
            build_temper_hint(TEMPER_FEISTY, "zh", streak=1)), dirty


# ── ③ 骂战态登记表（语音链的单一事实源）─────────────────────────────────────
def test_fight_turn_registry_roundtrip():
    from src.companion.temper import (
        clear_fight_turn,
        fight_turn_kind,
        record_fight_turn,
    )
    key = "telegram:8244899900:5433982810"
    clear_fight_turn(key)
    assert fight_turn_kind(key) == ""
    record_fight_turn(key, kind="insult")
    assert fight_turn_kind(key) == "insult"
    record_fight_turn(key, kind="feud")          # 后写覆盖
    assert fight_turn_kind(key) == "feud"
    clear_fight_turn(key)                        # 对方收手立即退出
    assert fight_turn_kind(key) == ""
    # kind 脏值归 insult；空 key 全程 no-op
    record_fight_turn(key, kind="rage")
    assert fight_turn_kind(key) == "insult"
    clear_fight_turn(key)
    record_fight_turn("", kind="insult")
    assert fight_turn_kind("") == ""


def test_fight_turn_registry_ttl_expiry():
    import time as _t

    from src.companion.temper import (
        FIGHT_TURN_TTL_SEC,
        clear_fight_turn,
        fight_turn_kind,
        record_fight_turn,
    )
    key = "test:ttl:1"
    clear_fight_turn(key)
    record_fight_turn(key)
    now = _t.time()
    assert fight_turn_kind(key, now=now) == "insult"
    # 窗内成立、过窗自动失效（骂战不该追着人一整天）
    assert fight_turn_kind(key, now=now + FIGHT_TURN_TTL_SEC + 1) == ""
    assert fight_turn_kind(key) == ""            # 过期已被剔除
    clear_fight_turn(key)


def test_fight_turn_registry_capped():
    from src.companion.temper import (
        _FIGHT_CAP,
        _FIGHT_TURNS,
        clear_fight_turn,
        record_fight_turn,
    )
    for i in range(_FIGHT_CAP + 40):
        record_fight_turn(f"test:cap:{i}")
    assert len(_FIGHT_TURNS) <= _FIGHT_CAP
    for i in range(_FIGHT_CAP + 40):
        clear_fight_turn(f"test:cap:{i}")


def test_resolve_emotion_for_send_fight_override():
    """骂战窗内：insult→angry（强情绪过阈值走情感通道）/ feud→serious（冷
    处理收场=居高临下的冷淡）；无登记=原判定链；emotion 通道关=仍 None。"""
    from src.ai.persona_voice import resolve_emotion_for_send
    from src.companion.temper import clear_fight_turn, record_fight_turn

    key_parts = dict(platform="telegram", account_id="8244899900",
                     chat_key="5433982810")
    key = "telegram:8244899900:5433982810"
    vc_on = {"emotion": {"enabled": True, "default": "warm"}}
    try:
        record_fight_turn(key, kind="insult")
        spec = resolve_emotion_for_send(vc_on, "你才傻逼呢！", **key_parts)
        assert spec is not None and spec.emotion == "angry"
        assert spec.intensity >= 0.7            # 必须过强情绪阈值（0.7 全局）
        record_fight_turn(key, kind="feud")
        spec2 = resolve_emotion_for_send(vc_on, "行，你慢慢骂。", **key_parts)
        assert spec2 is not None and spec2.emotion == "serious"
        # emotion 通道整体关闭 → 覆写也不越权（人设没有情绪通道）
        assert resolve_emotion_for_send(
            {"emotion": {"enabled": False}}, "x", **key_parts) is None
    finally:
        clear_fight_turn(key)
    # 无登记 → 原判定链（不返回 angry）
    spec3 = resolve_emotion_for_send(vc_on, "今天天气不错", **key_parts)
    assert spec3 is None or spec3.emotion != "angry"


def test_voice_emotion_angry_channel():
    """angry 档全链映射：EMOTIONS 收录 / CosyVoice 标签直通 / 语速偏快 /
    弱强度仍归 neutral 保真 / 副语言注入器不给骂人插笑声。"""
    from src.ai.voice_emotion import (
        EMOTIONS,
        EmotionSpec,
        cosyvoice_speed,
        inject_paralinguistic,
        to_cosyvoice_emotion,
    )
    assert "angry" in EMOTIONS
    strong = EmotionSpec("angry", intensity=0.85)
    assert to_cosyvoice_emotion(strong) == "angry"
    weak = EmotionSpec("angry", intensity=0.3)
    assert to_cosyvoice_emotion(weak) == "neutral"   # 音色保真阈值照常生效
    assert cosyvoice_speed(strong) > 1.0             # 吵架语速快而急
    txt = "你才傻逼呢！大半夜不睡觉跑来骂我"
    out = inject_paralinguistic(txt, strong)
    assert "[laughter]" not in out and "[sigh]" not in out


def test_persona_voice_wires_fight_override():
    """接线契约：resolve_emotion_for_send 必须消费骂战态登记表。"""
    import inspect

    from src.ai import persona_voice as mod
    src = inspect.getsource(mod)
    assert "fight_turn_kind" in src, "persona_voice 未消费骂战态（语音回 happy 骂人）"
    assert "record_fight_voice_override" in src


# ── ② 情感词典骂词补录（竞争信号源头修正）───────────────────────────────────
def test_emotional_lexicon_reads_insults_as_angry():
    """实录金标：「来呀你给我对骂呀你个傻逼玩意儿」曾被「呀」带成
    playful 0.97——补录骂词后 angry 必须压过 playful，valence 转负。"""
    from src.utils.emotional_context import analyze_emotion

    r = analyze_emotion("来呀你给我对骂呀你个傻逼玩意儿")
    assert r["primary_emotion"] == "angry"
    assert r["valence"] < 0
    r2 = analyze_emotion("大傻子大傻子你个傻逼")
    assert r2["primary_emotion"] == "angry"
    r3 = analyze_emotion("傻逼玩意操你妈操你妈操你妈")
    assert r3["primary_emotion"] == "angry"


def test_emotional_lexicon_insult_words_conservative():
    """无歧义子串纪律：正常话不被新词条误伤（滚/操你 这类高歧义串刻意没收）；
    否定前瞻照常生效（「别去死磕」不算骂）。"""
    from src.utils.emotional_context import analyze_emotion

    for t, why in (
        ("我去操场跑步", "操 单字未收"),
        ("做早操你去吗", "操你 邻接未收"),
        ("摇滚音乐真好听", "滚 单字未收"),
        ("别去死磕这个问题了", "否定前瞻豁免"),
    ):
        assert analyze_emotion(t)["primary_emotion"] != "angry", (t, why)


def test_temper_stats_new_counters():
    from src.companion.temper import (
        record_emo_block_suppressed,
        record_escalated_hint,
        record_fight_voice_override,
    )
    temper_stats_reset()
    try:
        record_escalated_hint()
        record_escalated_hint()
        record_emo_block_suppressed()
        record_fight_voice_override()
        s = temper_stats_snapshot()
        assert s["escalated_hints"] == 2
        assert s["emo_block_suppressed"] == 1
        assert s["fight_voice_overrides"] == 1
    finally:
        temper_stats_reset()


def test_resolve_emotion_for_send_fight_override_without_chat_key():
    """chat_key 为空的调用方也必须走完覆写判定（回归钉：首版覆写块引用了
    签名里不存在的 contact_key，chat_key 为空即 NameError 被外层 try 吞掉
    → 覆写静默失效且零观测）。空 chat 段合法（键=platform:acct:），
    有登记照样覆写；无登记走原判定链不抛异常。"""
    from src.ai.persona_voice import resolve_emotion_for_send
    from src.companion.temper import clear_fight_turn, record_fight_turn

    key = "telegram:8244899900:"
    vc_on = {"emotion": {"enabled": True, "default": "warm"}}
    try:
        record_fight_turn(key, kind="insult")
        spec = resolve_emotion_for_send(
            vc_on, "你才傻逼呢！",
            platform="telegram", account_id="8244899900", chat_key="")
        assert spec is not None and spec.emotion == "angry"
    finally:
        clear_fight_turn(key)
    spec2 = resolve_emotion_for_send(
        vc_on, "今天天气不错",
        platform="telegram", account_id="8244899900", chat_key="")
    assert spec2 is None or spec2.emotion != "angry"


# ══════════════════════════════════════════════════════════════════════════
# 消气曲线 / 记仇窗（2026-08-22 P1）：老板实录「开玩笑的啦」→ 下一轮直接
# 回暖甜聊＝机器人破绽。求和先进记仇期（∝骂战轮数、真道歉打折、开脱被点破），
# 逐轮消气 + TTL 冲淡；记仇期再犯重燃且 streak 续算；危机即刻散仇让位。
# ══════════════════════════════════════════════════════════════════════════

def test_apology_tier_classification():
    """真道歉（消气快）vs 敷衍开脱（消气慢+点破），多语言两级都认。"""
    from src.companion.temper import (
        is_de_escalation,
        is_flippant_retraction,
        is_sincere_apology,
    )
    sincere = ["对不起嘛别生气了", "抱歉抱歉我错了", "sorry my bad",
               "ごめんね", "미안해", "lo siento", "прости меня", "xin lỗi nha"]
    for t in sincere:
        assert is_sincere_apology(t), t
        assert is_de_escalation(t), t
    flippant = ["开玩笑的啦", "逗你玩呢", "就是测试一下你", "just kidding lol",
                "jk jk", "冗談だよ", "장난이야", "es broma", "шучу"]
    for t in flippant:
        assert is_flippant_retraction(t), t
        assert not is_sincere_apology(t), t
        assert is_de_escalation(t), t
    # 两者同现（「对不起对不起开玩笑的嘛」）→ sincere 优先由接线层保证，
    # 分类器各自如实报告
    both = "对不起对不起，开玩笑的嘛"
    assert is_sincere_apology(both) and is_flippant_retraction(both)


def test_initial_grudge_proportional():
    """比例感即真人感：轮数越多气越大、真道歉打折、封顶有界、0=关。"""
    from src.companion.temper import initial_grudge

    assert initial_grudge(1, sincere=True) == 0     # 骂1轮+真道歉=直接翻篇
    assert initial_grudge(1, sincere=False) == 1    # 骂1轮+「开玩笑」=冷1轮
    assert initial_grudge(2, sincere=True) == 1
    assert initial_grudge(3, sincere=False) == 3
    assert initial_grudge(9, sincere=False) == 3    # 封顶
    assert initial_grudge(9, sincere=True) == 2     # 真道歉封顶再降一档
    assert initial_grudge(4, sincere=False, max_level=0) == 0  # 关=旧行为
    assert initial_grudge(0, sincere=False) == 0
    assert initial_grudge("dirty", sincere=False) == 0  # 脏值安全


def test_decay_grudge():
    from src.companion.temper import decay_grudge

    assert decay_grudge(3, sincere=False) == 2   # 普通轮消一格
    assert decay_grudge(3, sincere=True) == 1    # 真道歉消两格
    assert decay_grudge(1, sincere=True) == 0
    assert decay_grudge(0, sincere=False) == 0
    assert decay_grudge("x", sincere=False) == 0


def test_grudge_hint_tiers_zh():
    """三级文案语义：≥2 不买账（禁秒原谅句）/ 1 松动端着 / closeout 翻篇带
    边界；off 空串；全级带危机让位例外。"""
    from src.companion.temper import build_grudge_hint

    hi = build_grudge_hint(2, TEMPER_FEISTY, "zh", flippant=True)
    assert "气还没消" in hi and "不接受和解" in hi
    for banned in ("没事", "没关系", "别在意", "不生气了"):
        assert banned in hi          # 负面清单必须逐句点名
    assert "你管这叫开玩笑" in hi    # 敷衍开脱要被点破
    assert "安全优先" in hi          # 危机让位例外
    hi_noflip = build_grudge_hint(2, TEMPER_SHARP, "zh", flippant=False)
    assert "你管这叫开玩笑" not in hi_noflip
    assert "阴阳" in hi_noflip       # sharp 佐料
    mid = build_grudge_hint(1, TEMPER_GENTLE, "zh")
    assert "松动" in mid and "边界" in mid and "安全优先" in mid
    close = build_grudge_hint(0, TEMPER_FEISTY, "zh", closeout=True)
    assert "翻篇" in close and "边界" in close and "别秒变" in close
    assert build_grudge_hint(3, TEMPER_OFF, "zh") == ""
    assert build_grudge_hint(0, TEMPER_FEISTY, "zh") == ""  # 无仇无收尾=空


def test_grudge_hint_tiers_en():
    from src.companion.temper import build_grudge_hint

    hi = build_grudge_hint(3, TEMPER_FEISTY, "en", flippant=True)
    assert "not over it" in hi and "instant-forgiveness" in hi
    assert "you call that a joke" in hi
    assert "safety first" in hi
    mid = build_grudge_hint(1, TEMPER_SHARP, "en")
    assert "half-thawed" in mid and "boundary" in mid
    close = build_grudge_hint(0, TEMPER_SHARP, "en", closeout=True)
    assert "accept the truce" in close and "boundary" in close


def test_parse_temper_cfg_grudge_keys():
    """缺省开（老板点名的破绽修复，与 temper enabled 同分叉理由）；
    max_level 夹 [0,6]、ttl 夹 [300,21600]；grudge_enabled=false → max=0。"""
    from src.companion.temper import parse_temper_cfg

    c = parse_temper_cfg(None)
    assert c["grudge_max_level"] == 3
    assert c["grudge_ttl_sec"] == 5400.0
    d = parse_temper_cfg({"companion": {"temper": {
        "grudge_max_level": 99, "grudge_ttl_sec": 1}}})
    assert d["grudge_max_level"] == 6
    assert d["grudge_ttl_sec"] == 300.0
    e = parse_temper_cfg({"companion": {"temper": {"grudge_enabled": False,
                                                   "grudge_max_level": 3}}})
    assert e["grudge_max_level"] == 0     # 关=回「立刻停火」旧行为
    f = parse_temper_cfg({"companion": {"temper": {
        "grudge_max_level": "dirty", "grudge_ttl_sec": "dirty"}}})
    assert f["grudge_max_level"] == 3 and f["grudge_ttl_sec"] == 5400.0


def test_fight_registry_grudge_kind_and_voice_override():
    """登记表认 grudge 类型；语音覆写＝serious 偏冷（比熔断 0.75 轻一档），
    绝不许回暖档甜嗓念别扭话。"""
    from src.ai.persona_voice import resolve_emotion_for_send
    from src.companion.temper import (
        clear_fight_turn,
        fight_turn_kind,
        record_fight_turn,
    )

    key = "telegram:acc_g:peer_g"
    vc_on = {"emotion": {"enabled": True, "default": "warm"}}
    try:
        record_fight_turn(key, kind="grudge")
        assert fight_turn_kind(key) == "grudge"
        spec = resolve_emotion_for_send(
            vc_on, "哼，算你会说话。",
            platform="telegram", account_id="acc_g", chat_key="peer_g")
        assert spec is not None and spec.emotion == "serious"
        assert 0.5 <= float(spec.intensity) < 0.75
    finally:
        clear_fight_turn(key)


def test_skill_manager_wires_grudge():
    """接线契约：求和不再瞬间清零——记仇期状态机全套挂进 skill_manager。"""
    import inspect

    from src.skills import skill_manager as mod
    src = inspect.getsource(mod)
    for sym in ("initial_grudge", "decay_grudge", "build_grudge_hint",
                "is_sincere_apology", "is_flippant_retraction",
                "record_grudge_set", "record_grudge_turn",
                "record_grudge_reignite", "record_grudge_closeout",
                "_grudge_level", "_grudge_ts", "_grudge_crisis"):
        assert sym in src, f"skill_manager 缺记仇窗接线：{sym}"
    # 记仇期再犯必须重燃（假道歉后再骂 streak 续算）
    assert "记仇期再犯" in src
    # 危机让位（安全 > 脾气）
    assert "detect_crisis" in src


def test_grudge_stats_counters():
    from src.companion.temper import (
        record_grudge_closeout,
        record_grudge_reignite,
        record_grudge_set,
        record_grudge_turn,
    )
    temper_stats_reset()
    try:
        record_grudge_set()
        record_grudge_turn()
        record_grudge_turn()
        record_grudge_reignite()
        record_grudge_closeout()
        s = temper_stats_snapshot()
        assert s["grudge_set"] == 1
        assert s["grudge_turns"] == 2
        assert s["grudge_reignites"] == 1
        assert s["grudge_closeouts"] == 1
    finally:
        temper_stats_reset()


# ══════════════════════════════════════════════════════════════════════════
# 出站认输句否决器（2026-08-22 P1-b）：指令层服从性不足的确定性最后防线。
# ══════════════════════════════════════════════════════════════════════════

def test_strip_surrender_tail_keeps_head():
    """实录金标：认输句在结尾 → 剥尾保头（回击部分原样保留）。"""
    from src.companion.temper import strip_surrender_lines

    raw = ("行啊你，嘴巴挺溜的嘛。不过说真的，你这骂人也就那样，"
           "我听着都不带生气的。要不咱换个话题？不然我真要去睡觉了哦。")
    out, removed, fb = strip_surrender_lines(raw, "fight")
    assert removed == 2 and not fb
    assert "换个话题" not in out and "睡觉" not in out
    assert "嘴巴挺溜" in out and "不带生气" in out
    raw2 = "哟，骂得挺顺溜啊你。行，你开心就好，我就当听个响。懒得跟你吵，我睡觉去了。"
    out2, removed2, _ = strip_surrender_lines(raw2, "fight")
    assert removed2 >= 2
    assert "开心就好" not in out2 and "懒得跟你吵" not in out2
    assert "骂得挺顺溜" in out2


def test_strip_surrender_full_strip_falls_back_deterministic():
    """整条都是认输句 → 确定性兜底短句（crc32 定变体，绝不吐空串）。"""
    from src.companion.temper import strip_surrender_lines

    raw = "算了算了，我懒得跟你吵。我去睡觉了。"
    out, removed, fb = strip_surrender_lines(raw, "fight")
    assert removed >= 1 and fb and out.strip()
    out_again = strip_surrender_lines(raw, "fight")[0]
    assert out == out_again  # 确定性
    en, _, fb_en = strip_surrender_lines(
        "Whatever. I'm going to sleep.", "fight", "en")
    assert fb_en and en.strip() and not any("\u4e00" <= c <= "\u9fff" for c in en)


def test_strip_surrender_false_positive_guards():
    """战斗宣言不许误剥：「随你怎么骂我奉陪到底」是继续怼；「没事找事」是
    怼不是原谅；正常聊天/熔断收场/翻篇轮（mode 空）原样放行。"""
    from src.companion.temper import strip_surrender_lines

    keep = "随你怎么骂，我奉陪到底，看谁先怂。"
    out, removed, _ = strip_surrender_lines(keep, "fight")
    assert removed == 0 and out == keep
    g = "你这就是没事找事。哼，我还记着账呢。"
    out2, removed2, _ = strip_surrender_lines(g, "grudge")
    assert removed2 == 0 and out2 == g
    # mode 不在 fight|grudge → 放行（熔断/翻篇轮的退场与和解语义合法）
    surrender = "行，你慢慢骂，我去忙了。"
    assert strip_surrender_lines(surrender, "")[1] == 0
    assert strip_surrender_lines(surrender, "feud")[1] == 0


def test_strip_forgive_lines_in_grudge_mode():
    """记仇轮秒原谅句剥离：「没事啦我不生气了」不许出现。"""
    from src.companion.temper import strip_surrender_lines

    raw = "哼，你倒是会说话。没事啦，我不生气了～"
    out, removed, fb = strip_surrender_lines(raw, "grudge")
    assert removed >= 1
    assert "没事啦" not in out and "不生气" not in out
    assert "会说话" in out and not fb
    # fight 词表不在 grudge 模式生效（换话题在记仇期是合法的冷处理）
    ok = "哼。换个话题吧。"
    assert strip_surrender_lines(ok, "grudge")[1] == 0


def test_output_guard_wired_in_skill_manager():
    """接线契约：出站否决挂在 A/B 两线危机兜底之前，标记读后即焚。"""
    import inspect

    from src.skills import skill_manager as mod
    src = inspect.getsource(mod)
    assert "_apply_temper_output_guard" in src
    assert "_temper_out_guard" in src
    assert "strip_surrender_lines" in src
    # A/B 两线各一个调用点 + 方法定义 = 至少 3 处出现
    assert src.count("_apply_temper_output_guard") >= 3
    # 标记只在 fight 常规轮与 grudge 轮设置（熔断/翻篇轮不设）
    assert 'user_context["_temper_out_guard"] = "fight"' in src
    assert '"grudge")' in src or '_temper_out_guard"] = ("grudge")' in src or "= (\n                                        \"grudge\")" in src


def test_out_guard_stats_and_cfg():
    from src.companion.temper import parse_temper_cfg, record_out_guard

    assert parse_temper_cfg(None)["output_guard"] is True
    assert parse_temper_cfg({"companion": {"temper": {
        "output_guard": False}}})["output_guard"] is False
    temper_stats_reset()
    try:
        record_out_guard()
        record_out_guard(fallback=True)
        s = temper_stats_snapshot()
        assert s["out_guard_strips"] == 2
        assert s["out_guard_fallbacks"] == 1
    finally:
        temper_stats_reset()


def test_persona_temper_yaml_false_maps_to_off():
    """YAML 陷阱防御（2026-08-22 实锤）：人设文件裸写 ``temper: off`` 被
    YAML 1.1 读成布尔 False → 必须映射为 off，不许静默落回 default。"""
    from src.companion.temper import (
        persona_temper_raw,
        resolve_temper_level,
    )
    assert persona_temper_raw({"temper": False}) == TEMPER_OFF
    assert persona_temper_raw({"personality": {"temper": False}}) == TEMPER_OFF
    # True 语义不明确（on≠某档位）→ 不映射，按未配置处理
    assert persona_temper_raw({"temper": True}) == ""
    lv = resolve_temper_level(None, {"temper": False})
    assert lv["level"] == TEMPER_OFF and lv["source"] == "persona"
