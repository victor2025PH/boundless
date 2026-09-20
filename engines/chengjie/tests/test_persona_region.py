# -*- coding: utf-8 -*-
"""#40 地区语气档（zh-CN / zh-TW / zh-HK）契约 —— I-4 D1。

三条不变量：
① zh-CN 档恒空块（存量 prompt 逐字不变，CN 回归钉）；
② 解析优先级：人设显式 speaking.region → 粤语 dialect → 人设居住地 → 会话「发→」变体
   → 全局默认 → zh-CN；
③ 桥接注入只在 ai.spoken_style.enabled 下、且 TW/HK 才计 l1_region_inject。
禁用词只**观测**不改文本（find_banned_words 是观测口径，测试也据此断言）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai import persona_region as pr
from src.ai import spoken_style_bridge as br


class _Cfg:
    def __init__(self, ss: dict | None):
        self.config = {"ai": ({"spoken_style": ss} if ss is not None else {})}


def _fresh():
    br._MOD = None
    br._FAILED = False
    br._PKG_DIR_OVERRIDE = None


# ───────────────────────── normalize / profile ─────────────────────────

def test_normalize_region_aliases():
    assert pr.normalize_region("zh-TW") == "zh-TW"
    assert pr.normalize_region("tw") == "zh-TW"
    assert pr.normalize_region("台湾") == "zh-TW"
    assert pr.normalize_region("台灣") == "zh-TW"
    assert pr.normalize_region("zh-hant") == "zh-TW"
    assert pr.normalize_region("HK") == "zh-HK"
    assert pr.normalize_region("香港") == "zh-HK"
    assert pr.normalize_region("yue") == "zh-HK"
    assert pr.normalize_region("zh-hk") == "zh-HK"
    assert pr.normalize_region("cn") == "zh-CN"
    assert pr.normalize_region("大陆") == "zh-CN"
    assert pr.normalize_region("zh") == "zh-CN"
    # 未知 / 空 → 空串（调用方自行回落）
    assert pr.normalize_region("") == ""
    assert pr.normalize_region(None) == ""
    assert pr.normalize_region("ja") == ""


def test_cn_block_is_empty_regression_pin():
    """CN 档恒空：存量 prompt 一个字不多。"""
    assert pr.region_block("zh-CN") == ""
    assert pr.region_block("") == ""
    assert pr.region_block("nonsense") == ""
    assert pr.find_banned_words("咋整啊哥们，牛逼", "zh-CN") == []


def test_tw_block_has_particles_and_bans():
    b = pr.region_block("zh-TW")
    assert "台灣" in b
    for w in ("啦", "耶", "喔", "欸"):
        assert w in b
    # 大陸/北方口語與網路詞被點名禁用
    for w in ("咋", "牛逼", "老铁", "666", "yyds", "咱们"):
        assert w in b
    # TW 檔不強制繁體（人設可能簡體書寫）；HK 檔強制繁體
    assert "字形" not in b


def test_hk_block_is_cantonese_traditional():
    b = pr.region_block("zh-HK")
    assert "香港" in b and "繁體" in b
    for w in ("喎", "囉", "咩", "嘅"):
        assert w in b
    assert "係" in b and "唔" in b and "點解" in b


def test_find_banned_words_tw():
    hits = pr.find_banned_words("你咋整啊哥们，这也太牛逼了，咱们走", "zh-TW")
    assert "咋" in hits and "牛逼" in hits and "哥们" in hits and "咱们" in hits
    # 单字「贼/忒」只在程度副词位置计数（防「賊船」误报）
    assert "贼" in pr.find_banned_words("这个贼好吃", "zh-TW")
    assert "贼" not in pr.find_banned_words("海賊王真好看", "zh-TW")
    assert "贼" not in pr.find_banned_words("那个贼跑了", "zh-TW")
    # 地道台灣句不命中
    assert pr.find_banned_words("真的假的？超好吃的耶，你要不要一起來喔", "zh-TW") == []


def test_find_banned_words_hk():
    hits = pr.find_banned_words("你什么时候来啊，怎么了", "zh-HK")
    assert "什么" in hits and "怎么" in hits
    assert pr.find_banned_words("你幾時返嚟呀，點解唔開心喎", "zh-HK") == []


def test_hk_block_has_hard_script_pin_with_negative_samples():
    """#36 实锤：只说「繁體」会出简体粤文混排 → 块里必须点名高频简体字当负样本。"""
    b = pr.region_block("zh-HK")
    assert "字形硬性要求" in b
    for ch in ("这", "说", "吗", "们"):
        assert ch in b
    assert "係/唔/嘅/咩" in b
    # TW 档不声明 script（简→繁归 #36 出向翻译栈），不得带字形硬钉子
    assert "字形硬性要求" not in pr.region_block("zh-TW")


def test_find_simplified_chars_only_for_traditional_profile():
    # 港式粤文里混进简体「这/说/吗」→ 命中；地道繁體粤文 → 空
    assert pr.find_simplified_chars("佢这样说吗", "zh-HK") == ["这", "样", "说", "吗"]
    assert pr.find_simplified_chars("你係唔係想食飯呀，幾時得閒喎", "zh-HK") == []
    # 非繁體档（TW 未声明 script / CN）恒空——那不是这个观测该管的
    assert pr.find_simplified_chars("这个说的对吗", "zh-TW") == []
    assert pr.find_simplified_chars("这个说的对吗", "zh-CN") == []
    assert pr.find_simplified_chars("", "zh-HK") == []


def test_note_banned_hits_counts_observed_banned_and_script():
    s0 = pr.stats()
    hits = pr.note_banned_hits("你什么时候来啊，这个说的对", "zh-HK")
    s1 = pr.stats()
    assert "什么" in hits                              # 旧契约：返回禁用词列表
    assert s1["observed"] == s0["observed"] + 1        # 分母 +1
    assert s1["banned_hit"] == s0["banned_hit"] + 1
    assert s1["script_hit"] == s0["script_hit"] + 1    # 「这/说」漏简体
    # 地道句：observed +1，两种命中都不动
    pr.note_banned_hits("你幾時返嚟呀，點解唔開心喎", "zh-HK")
    s2 = pr.stats()
    assert s2["observed"] == s1["observed"] + 1
    assert s2["banned_hit"] == s1["banned_hit"]
    assert s2["script_hit"] == s1["script_hit"]


# ───────────────────────── 解析优先级 ─────────────────────────

def test_resolve_explicit_persona_field_wins():
    persona = {"speaking": {"region": "台湾"}, "voice_profile": {"dialect_flavor": "cantonese"}}
    assert pr.resolve_region(persona, use_location=False) == "zh-TW"
    # 顶层 region 兼容位
    assert pr.resolve_region({"region": "hk"}, use_location=False) == "zh-HK"


def test_resolve_cantonese_dialect_implies_hk():
    persona = {"voice_profile": {"dialect_flavor": "cantonese"}}
    assert pr.resolve_region(persona, use_location=False) == "zh-HK"
    # 非出货方言不定档
    assert pr.resolve_region({"voice_profile": {"dialect_flavor": "sichuan"}},
                             use_location=False) == "zh-CN"


def test_resolve_location_country():
    # 真实 schema 三种写法：slug 串 / 带 tz 的 dict / 无 location 键从 role+background 推断
    tw = {"location": "taipei"}
    hk = {"location": {"city": "Hong Kong", "timezone": "Asia/Hong_Kong"}}
    cn = {"location": "shanghai"}
    inferred = {"role": "在台北上班的咖啡師", "background": "住在信義區"}
    assert pr.resolve_region(tw) == "zh-TW"
    assert pr.resolve_region(hk) == "zh-HK"
    assert pr.resolve_region(cn) == "zh-CN"
    assert pr.resolve_region(inferred) == "zh-TW"
    # 居住地推断可关（region_from_location:false）
    assert pr.resolve_region(tw, use_location=False) == "zh-CN"
    # 非中文地区（马尼拉）不定档 → 回落默认
    assert pr.resolve_region({"location": "manila"}) == "zh-CN"
    # location: none 显式关掉 → 不推断
    assert pr.resolve_region({"location": "none", "role": "台北人"}) == "zh-CN"


def test_resolve_outbound_variant_from_context():
    assert pr.outbound_variant_region("zh-tw") == "zh-TW"
    assert pr.outbound_variant_region("zh-TW") == "zh-TW"
    assert pr.outbound_variant_region("yue") == "zh-HK"
    assert pr.outbound_variant_region("zh") == ""
    assert pr.outbound_variant_region("en") == ""
    # 上游已解析的铆定直接消费（不查库）
    ctx = {"_outbound_lang_pin": "zh-tw"}
    assert pr.resolve_region(None, context=ctx) == "zh-TW"
    ctx2 = {"outbound_lang": "yue"}
    assert pr.resolve_region({}, context=ctx2) == "zh-HK"


def test_resolve_outbound_pin_queries_store(monkeypatch):
    """context 只带 platform/account/chat 三元组 → 走 sendpoint_guard.outbound_lang_pin。"""
    from src.ai import sendpoint_guard as sg
    calls = []

    def _fake(plat, acct, chat):
        calls.append((plat, acct, chat))
        return "zh-tw"

    monkeypatch.setattr(sg, "outbound_lang_pin", _fake)
    ctx = {"platform": "telegram", "account_id": "acc1", "chat_key": "123"}
    assert pr.resolve_region(None, context=ctx) == "zh-TW"
    assert calls == [("telegram", "acc1", "123")]
    # 三元组不全 → 不查库
    calls.clear()
    assert pr.resolve_region(None, context={"platform": "telegram"}) == "zh-CN"
    assert calls == []


def test_resolve_default_and_never_raises():
    assert pr.resolve_region(None) == "zh-CN"
    assert pr.resolve_region(None, default="zh-TW") == "zh-TW"
    assert pr.resolve_region(None, default="garbage") == "zh-CN"
    # 奇形怪状 persona/context 绝不抛
    assert pr.resolve_region("not a dict", context=42) == "zh-CN"
    assert pr.resolve_region({"speaking": "oops", "voice_profile": None}, context=None) == "zh-CN"


def test_resolve_from_context_reads_global_default_and_session_override():
    cfg = _Cfg({"enabled": True, "region": "zh-TW"})
    assert pr.resolve_region_from_context(cfg, {}) == "zh-TW"
    assert pr.resolve_region_from_context(cfg, None) == "zh-TW"
    # 会话级覆写（前端选择器落地后写 _spoken_region）压过一切
    ctx = {"_spoken_region": "hk", "_resolved_persona": {"speaking": {"region": "tw"}}}
    assert pr.resolve_region_from_context(cfg, ctx) == "zh-HK"
    # 人设显式 > 全局默认
    ctx2 = {"_resolved_persona": {"speaking": {"region": "cn"}}}
    assert pr.resolve_region_from_context(cfg, ctx2) == "zh-CN"
    # region_from_location:false 关掉居住地推断
    cfg2 = _Cfg({"enabled": True, "region_from_location": False})
    ctx3 = {"_resolved_persona": {"location": "taipei"}}
    assert pr.resolve_region_from_context(cfg2, ctx3) == "zh-CN"
    assert pr.resolve_region_from_context(_Cfg({"enabled": True}), ctx3) == "zh-TW"


# ───────────────────────── 桥接接线 ─────────────────────────

def test_bridge_disabled_no_region_block():
    _fresh()
    ctx = {"_resolved_persona": {"speaking": {"region": "tw"}}}
    assert br.system_block(_Cfg({"enabled": False}), context=ctx) == ""
    assert br.region_block(_Cfg(None), context=ctx) == ""


def test_bridge_cn_persona_unchanged_and_not_counted():
    _fresh()
    cfg = _Cfg({"enabled": True})
    before = br.stats().get("l1_region_inject", 0)
    # 旧签名（无 context）输出与带 CN 人设 context 输出逐字一致
    legacy = br.system_block(cfg)
    with_cn = br.system_block(cfg, context={"_resolved_persona": {"speaking": {"region": "cn"}}})
    assert legacy == with_cn == ""
    assert br.stats().get("l1_region_inject", 0) == before


def test_bridge_tw_persona_injects_and_counts():
    _fresh()
    cfg = _Cfg({"enabled": True})
    before = br.stats().get("l1_region_inject", 0)
    got = br.system_block(cfg, context={"_resolved_persona": {"speaking": {"region": "zh-TW"}}})
    assert "【地區語氣｜台灣】" in got and "牛逼" in got
    assert br.stats().get("l1_region_inject", 0) == before + 1
    # 显式 region 形参压过 context
    got_hk = br.system_block(cfg, context={"_resolved_persona": {"speaking": {"region": "tw"}}},
                             region="hk")
    assert "香港" in got_hk and "台灣" not in got_hk


def test_bridge_region_enabled_false_forces_cn():
    _fresh()
    cfg = _Cfg({"enabled": True, "region_enabled": False})
    ctx = {"_resolved_persona": {"speaking": {"region": "tw"}}}
    assert br.resolve_region(cfg, ctx) == "zh-CN"
    assert br.system_block(cfg, context=ctx) == ""


def test_bridge_region_block_coexists_with_fingerprint():
    """指纹段与地区段共存：地区段追加在指纹段之后，指纹段本体不变。"""
    _fresh()
    cfg = _Cfg({"enabled": True, "emotion_tags": True, "paraling": True})
    base = br.system_block(cfg)
    if not base:  # 包缺席环境：只验地区段仍独立注入
        got = br.system_block(cfg, context={"_resolved_persona": {"speaking": {"region": "tw"}}})
        assert "【地區語氣｜台灣】" in got
        return
    got = br.system_block(cfg, context={"_resolved_persona": {"speaking": {"region": "tw"}}})
    assert got.startswith(base)
    assert "【地區語氣｜台灣】" in got[len(base):]


def test_bridge_observe_region_output():
    _fresh()
    cfg = _Cfg({"enabled": True})
    ctx_tw = {"_resolved_persona": {"speaking": {"region": "tw"}}}
    before = pr.stats().get("banned_hit", 0)
    hits = br.observe_region_output(cfg, "哥们你咋整的，牛逼", context=ctx_tw)
    assert "牛逼" in hits
    assert pr.stats().get("banned_hit", 0) == before + 1
    # CN 档 / 英文 / 未启用 → 恒空且不计数
    assert br.observe_region_output(cfg, "哥们你咋整的", context={}) == []
    assert br.observe_region_output(cfg, "bro what's up", context=ctx_tw) == []
    assert br.observe_region_output(_Cfg({"enabled": False}), "牛逼", context=ctx_tw) == []
    assert pr.stats().get("banned_hit", 0) == before + 1


def test_persona_schema_registers_speaking_region():
    from src.utils import persona_manager as pm
    assert "speaking.region" in pm.PERSONA_SCHEMA_FIELDS
    # 由 spoken_style_bridge 注入，persona_manager 本体不得再注入（双源真相）
    assert "speaking.region" in pm.PROMPT_EXEMPT_FIELDS
