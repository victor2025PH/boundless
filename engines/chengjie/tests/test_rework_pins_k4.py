# -*- coding: utf-8 -*-
"""K-4 返工单回归钉（2026-09-05）：#61 #63 #64 #67 · #145 五件 · #36 · #170。

这批单是台账里「带 fix_note 却停在 confirmed」的返工单。K-4 复核结论（见
``docs/发版对账_v1.0.74_K4.md``）：四张都不是修复失效——#63/#67 的「验证未过」是
同一句 LINE 媒体反馈被旧 verify 逻辑误归属，#61/#64 的「还是不行」分别点在残余层
（#78）与修复入包前（1.0.62 → 1.0.63）。本文件只做一件事：把每单 fix_note 所描述
的修复落点（函数 / 常量 / 守卫 / 接线）钉在 HEAD 上，谁把它挪走或改掉，这里先红。

写法：静态源码切片 + 纯函数断言，不起服务、不碰 store、不拉 LLM。
每单一到两条；#145 按 K-D6 拆五件各一条（件④出站接线归 K-1 A3，提交后由
``test_withdrawn_cite.py`` 新例钉，本文件刻意不钉未提交的 hunk）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[1]
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _read(rel: str) -> str:
    return (_ENGINE / rel).read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    seg = src[src.index(start):]
    return seg[:seg.index(end)]


# ═══════════════════════════════════════════════════════════════════════════════
# #61 人设「应用到」只认第一个 TG 账号（ab866775 → #78 b7161370 → #179 17230974）
# ═══════════════════════════════════════════════════════════════════════════════

def test_61_status_enum_merges_runtime_registry_and_never_renders_placeholder():
    """fix_note「合并运行时账号注册表——所有已登录 TG 账号都能看到」+ 三层残余收口。"""
    src = _read("src/web/routes/persona_routes.py")
    seg = _between(src, '@app.get("/api/personas/status")',
                   '"line_accounts": line_accounts')
    # #61 主修：TG 枚举合并 platform_accounts 运行时注册表
    assert 'get_account_registry().list("telegram")' in seg
    assert '"source": "registry"' in seg
    # #179：注册表占位行（default 别名槽 / 从未上线的空壳）在 append 之前被过滤
    assert seg.index("registry_row_is_placeholder(_row)") < seg.index('"source": "registry"')
    assert "config_default_account_is_live(_tg_cfg, _cfg_obj)" in seg
    # #78 二轮：注册表有真账号时 config 回落 default 槽一律不列
    assert re.search(
        r"if _rt_rows:\s*\n\s*tg_accounts = \[a for a in tg_accounts\s*\n\s*"
        r"if a\.get\(\"account_id\"\) != \"default\"\]", seg), "#78 二轮 default 隐藏判据丢了"
    # #78-①：LINE / WA / Messenger 运行时账号进枚举（#61 只并了 TG）
    assert 'line_accounts = _registry_rows("line")' in seg
    assert '_registry_rows("whatsapp")' in seg and '_registry_rows("messenger")' in seg
    # #78-②：messenger_rpa 未启用且未配 accounts 时不凭空回退 default 占位
    assert re.search(r'if _mrpa_cfg\.get\("enabled"\) or _mrpa_cfg\.get\("accounts"\):', seg)
    # 绑定计数循环四路都算（LINE 行不能是「看得见指不了」）
    assert "for acc in tg_accounts + mrpa_accounts + wa_accounts + line_accounts:" in seg


def test_61_placeholder_judgement_keeps_real_accounts():
    """#179 纯函数：default 别名恒占位；真号（哪怕离线、无运营 label）只要有昵称就保留。"""
    from src.web.routes.persona_routes import registry_row_is_placeholder

    assert registry_row_is_placeholder({
        "platform": "telegram", "account_id": "default", "label": "default",
        "status": "online", "last_online_at": 0, "meta": {}})
    assert not registry_row_is_placeholder({
        "platform": "telegram", "account_id": "8852939166", "label": "",
        "status": "pending", "last_online_at": 0, "meta": {"self_name": "小柔"}})
    assert not registry_row_is_placeholder({
        "platform": "telegram", "account_id": "8657686021", "label": "",
        "status": "offline", "last_online_at": 1756500000.0, "meta": {}})


# ═══════════════════════════════════════════════════════════════════════════════
# #63 新登录账号默认人审后发（efa24317 → ae772df1 #142 → 45261c78 #167）
# ═══════════════════════════════════════════════════════════════════════════════

def test_63_account_level_takeover_entry_still_exists():
    """fix_note「按账号管接管方式：新账号确认前一律拟稿人审」的入口函数与常量。"""
    from src.inbox import account_mode_onboarding as amo

    assert amo.PENDING_DEFAULT_MODE == "review"
    assert amo.DECISION_MODES == ("auto_ai", "review", "manual")
    assert amo.FIRST_LOGIN_GRACE_SEC == 7 * 86400          # #167 首登竞态窗
    for name in ("account_mode_for", "account_mode_from_cid", "decide_account_mode",
                 "is_new_account", "cap_unconfirmed_default_persona",
                 "pending_accounts", "align_account_conversations"):
        assert callable(getattr(amo, name)), name


def test_63_bootstrap_never_persists_unconfirmed_account():
    """账号层生效期 bootstrap 不落盘 + #167 未确认不落盘；resolve 侧叠封顶。"""
    src = _read("src/inbox/automation_mode.py")
    boot = _between(src, "def maybe_bootstrap_automation_mode(",
                    'store.set_automation_mode(conversation_id, mode, source="bootstrap")')
    assert "acct_mode = _account_mode_layer(conversation_id, config)" in boot
    assert re.search(r"if acct_mode is not None:", boot)
    assert "decided_mode(parts[0], parts[1]) is None" in boot
    assert "cap_unconfirmed_default_persona(conversation_id, mode, config)" in boot
    resolve = _between(src, "def resolve_automation_mode(",
                       "def maybe_bootstrap_automation_mode(")
    assert "_account_mode_layer(conversation_id, config)" in resolve
    assert "cap_unconfirmed_default_persona(conversation_id, mode, config)" in resolve


# ═══════════════════════════════════════════════════════════════════════════════
# #64 出站中英混语「I'm 我, and I'm not going anywhere.」
#     cf1a3c6c（1.0.63 三翻译出口）→ #97 9991a6f9（1.0.65 收口点）
# ═══════════════════════════════════════════════════════════════════════════════

_MIXED_64 = (
    "I'm 我, and I'm not going anywhere.",                       # #64 skuio 0830 03:53
    "I'm 我 the one who's still here, still listening.",         # #97 0830 21:47（1.0.63 真复发）
    "But I'm 我 actually curious, not just making small talk.",  # #97 第五例 0831 10:55
    "You know I'm here, same 我",                                # B125 译文漏译实录
)
_CLEAN_64 = "I'm here, and I'm not going anywhere."
_ONLY_LANG_MIX = {"enabled": True, "monologue": False, "lang_mix": True,
                  "unfounded_recall": False, "recall_grounding": False,
                  "shared_past": False, "apology_dedup": False,
                  "goal_meta": False, "degenerate": False}


@pytest.mark.parametrize("text", _MIXED_64)
def test_64_mixed_script_is_detected_and_stripped(text):
    from src.ai.outbound_text_guard import (
        apply_outbound_text_guard, detect_lang_mix, sendpoint_lang_mix_pass,
        strip_minority_script,
    )
    assert detect_lang_mix(text)["action"] == "hard"
    stripped = strip_minority_script(text)
    assert stripped != text and not _CJK.search(stripped)
    out, act = sendpoint_lang_mix_pass(text)              # #97 收口点（出门前必过）
    assert act == "hard_stripped" and not _CJK.search(out)
    cleaned, meta = apply_outbound_text_guard(text, _ONLY_LANG_MIX)   # 出稿口
    assert meta.get("lang_mix") == "hard_stripped" and not _CJK.search(cleaned)


def test_64_clean_english_is_not_false_positive():
    from src.ai.outbound_text_guard import (
        detect_lang_mix, sendpoint_lang_mix_pass, strip_minority_script,
    )
    assert detect_lang_mix(_CLEAN_64)["action"] == ""
    assert strip_minority_script(_CLEAN_64) == _CLEAN_64
    assert sendpoint_lang_mix_pass(_CLEAN_64) == (_CLEAN_64, "")


def test_64_generation_side_rewrite_rule_and_hook():
    """生成端「拦截→重写」档：_reply_lang_mismatch 的拉丁夹 CJK 判据 + 重写钩子仍挂。"""
    src = _read("src/ai/ai_client.py")
    seg = _between(src, "def _reply_lang_mismatch(", "def _chat_reply_surface(")
    assert re.search(
        r"if letters >= 6 and cjk >= 1 and letters >= 3 \* cjk:\s*\n\s*return True", seg)
    assert src.count("await self._guard_reply_language(") >= 3


def test_64_all_three_translation_exits_and_sendpoint_are_guarded():
    """fix_note「三条出站翻译出口全部挂混语守卫（此前有一条裸奔）」+ #97 收口点。"""
    helpers = _read("src/inbox/autosend_helpers.py")
    assert "def _guard_translated_lang_mix(assistant, src_text: str, out_text)" in helpers
    cb = _between(helpers, "def build_autosend_translate_cb(",
                  "def build_autosend_mark_read_cb(")
    assert "return _guard_translated_lang_mix(" in cb         # 出口①② autosend 自动链 / 人工通过链
    main = _read("main.py")
    assert "return _guard_translated_lang_mix(self, str(text), _out)" in main   # 出口③ deferred 触达
    orch = _read("src/integrations/account_orchestrator.py")
    assert orch.count("sendpoint_lang_mix_pass(") >= 2           # 编排器 send：正文 + 配文
    assert "sendpoint_lang_mix_pass(out)" in _read("src/ai/outbound_quality.py")   # A 线出站质检


# ═══════════════════════════════════════════════════════════════════════════════
# #67 相册上传失败（a24a6738，1.0.63 三层）
# ═══════════════════════════════════════════════════════════════════════════════

def test_67_upload_route_validates_content_before_write_and_uses_data_root():
    src = _read("src/web/routes/persona_media_routes.py")
    assert "resolve_album_root as _resolve_album_root" in src    # ① 数据根单一事实源
    assert "sniff_media_bytes as _sniff_media" in src
    seg = _between(src, '@app.post("/api/personas/{pid}/media")',
                   'url = f"/static/persona_albums/')
    # ② magic bytes 不过 = 当场 400，绝不静默存坏；校验必须在落盘之前
    assert re.search(
        r"_bad = _sniff_media\(data, ext\)\s*\n\s*if _bad:\s*\n\s*raise HTTPException\(400", seg)
    assert '"err.pmedia.bad_content"' in seg
    assert seg.index("_sniff_media(data, ext)") < seg.index("fpath.write_bytes(data)")
    # ② 落盘后回读验证（写成功 ≠ 内容对）
    assert "_head != bytes(data[:16])" in seg
    # 落盘目录来自 _album_root()（不再 Path(__file__) 推安装目录）
    assert "d = _album_root() / safe" in seg


def test_67_sniff_and_album_root_pure_functions(monkeypatch, tmp_path):
    from src.companion.media_paths import resolve_album_root, sniff_media_bytes

    assert sniff_media_bytes(b"\x00" * 64, ".jpg")                          # 坏文件 → 有原因
    assert sniff_media_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 60, ".jpg") == ""
    assert sniff_media_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 60, ".jpg") == ""   # 族内互认
    assert sniff_media_bytes(b"\x00" * 4 + b"ftyp" + b"\x00" * 60, ".mp4") == ""
    assert sniff_media_bytes(b"\x00" * 64, ".mp4")
    # ① AITR_DATA_DIR 在 → 相册落数据根（版本更新不再清空）
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    assert resolve_album_root() == tmp_path / "persona_albums"


def test_67_send_failure_attribution_and_error_copy():
    """③ 文件缺失独立归因 album_file_missing（不再误报「相册是空的」）+ 拒收文案双语。"""
    assert 'record_image_fallback("album_file_missing"' in _read("src/inbox/image_autosend.py")
    errs = _read("src/web/i18n_packs/errors_stock.py")
    assert errs.count('"err.pmedia.bad_content"') >= 2


# ═══════════════════════════════════════════════════════════════════════════════
# #145 复合单五件（按 K-D6 拆）
# ═══════════════════════════════════════════════════════════════════════════════

def test_145_1_clone_voice_mount_guard_and_speech_verdict():
    """件① 克隆声工具箱：cfe9ad47 挂载断链守卫 + 929ba165/23cb7dba 念错终审。"""
    from src.ai import speech_verdict as sv
    from src.ai import voice_profile_guard as vpg

    assert vpg.CLONE_BACKENDS
    assert callable(vpg.is_registered_clone_profile) and callable(vpg.heal_clone_profile)
    assert callable(sv.garbled_suspect) and callable(sv.judge_preview_speech)
    assert sv.GARBLED_MIN_CER == 0.35
    assert "heal_clone_profile(vp)" in _read("src/utils/persona_manager.py")
    assert "_redispatch_garbled_to_edge" in _read("src/web/routes/voice_routes.py")
    assert "_discard_garbled_take" in _read("src/ai/tts_pipeline.py")


def test_145_2_line_inbound_media_chain():
    """件② LINE 收发图/表情：2a9bf45a E2EE 自愈 + c9d19638 #172 404 分档（随真机验收）。"""
    from src.integrations import line_e2ee_recovery as rec
    from src.integrations import line_media as lm

    assert callable(rec.rebuild_e2ee_from_session) and callable(rec.is_e2ee_key_error)
    assert callable(lm.is_e2ee_media) and callable(lm.obs_object_locator)
    # #172：404 不再一律「已过期」——龄未知恒 NOT_FOUND，龄够才 EXPIRED，上传中 PENDING
    assert lm.classify_missing(-1, 24) == lm.MISS_NOT_FOUND
    assert lm.classify_missing(48 * 3600, 24) == lm.MISS_EXPIRED
    assert lm.classify_missing(60, 24, obj_status="uploading") == lm.MISS_PENDING


def test_145_3_persona_catalog_isolation_and_camp_guard():
    """件③ AI 说其他人设的产品并贬低：bdae979e 目录按人设过滤 + 0cdf9ada #147 双层守卫
    + 71355568（K-1 A2）选品与过滤同源 persona_id。"""
    from src.companion.goals.claim_guard import (
        find_camp_disparagement, sanitize_camp_disparagement,
    )
    from src.companion.goals.site_catalog import foreign_product_names

    assert callable(foreign_product_names)
    assert callable(find_camp_disparagement) and callable(sanitize_camp_disparagement)
    assert "_foreign_products_for(" in _read("src/utils/persona_guard.py")
    svc = _read("src/companion/goals/service.py")
    assert "catalog_persona_id" in svc
    assert re.search(r"persona_id=catalog_persona_id\)", svc), "K-1 A2 选品 persona_id 同源丢了"


def test_145_4_peer_delete_sync_inbound_half():
    """件④ 手机端删除消息智聊不删：0cdf9ada #146 对端删除同步 + 60c2d428 #32② 记得但不主动提。
    出站接线（``apply_to_reply`` 入口 + skill_manager L5017 / drafts.py 挂点）归 K-1 A3，
    在 HEAD 尚未提交——本钉刻意只碰 HEAD 已有的账本 / 剥句纯函数。"""
    from src.inbox import withdrawn_cite as wc
    from src.inbox.peer_delete_purge import purge_for_deleted_rows

    assert callable(purge_for_deleted_rows)
    assert "withdrawn_cite" in _read("src/inbox/inbound_enrich.py")
    wc.reset_ledger()
    try:
        assert wc.record_withdrawn("telegram:1001:2002", "婴儿表情包")
        quotes = wc.quotes_for("telegram:1001:2002")
        assert quotes == ["婴儿表情包"]
        cleaned, hits = wc.sanitize_outbound(
            "哈哈你那个婴儿表情包真的好像。今天过得怎么样？", quotes)
        assert hits and "婴儿表情包" not in cleaned and "今天过得怎么样" in cleaned
        # 对方本轮自己又提了 → 不算主动引用，原样放行
        same, hits2 = wc.sanitize_outbound(
            "哈哈婴儿表情包", quotes, inbound="你还记得那个婴儿表情包吗")
        assert hits2 == [] and same == "哈哈婴儿表情包"
        # A 线历史保留槽位、正文换占位（#146 曾整条剔除导致上下文错位）
        hist = wc.redact_history(
            [{"role": "user", "content": "婴儿表情包"}, {"role": "assistant", "content": "哈哈"}],
            quotes)
        assert len(hist) == 2 and hist[0]["content"] == wc.PLACEHOLDER and hist[1]["content"] == "哈哈"
    finally:
        wc.reset_ledger()


def test_145_5_and_170_unread_aggregate_and_buried_entry():
    """件⑤ 虚拟未读 → #159 2f8fc70f 口径同源；#170 09bd0294 被埋入口紧凑态可见 + 一键取消归档。"""
    from src.inbox import unread_aggregate as ua

    for name in ("unread_maps", "unread_conversations", "phantom_unread_report"):
        assert callable(getattr(ua, name)), name
    html = _read("src/web/templates/unified_inbox.html")
    assert "_unarchiveBuried" in html
    assert "/api/admin/buried-conversations" in html
    assert "#170" in html
    i18n = _read("src/web/i18n_packs/inbox_workspace.py")
    for k in ("inbox.buried.unarch", "inbox.buried.unarch_ok", "inbox.buried.unarch_fail"):
        assert f'"{k}"' in i18n, k


# ═══════════════════════════════════════════════════════════════════════════════
# #36 AI 回复繁体 / 粤语（出向目标语 2026-08-29，b7161370 / 4390a469）+ 地区语气 907faa83
# ═══════════════════════════════════════════════════════════════════════════════

def test_36_traditional_and_cantonese_are_first_class_outbound_targets():
    from src.inbox import outbound_translate as ot

    assert "yue" in ot._CJK_LANGS and "zh-tw" in ot._CJK_LANGS
    assert ot.normalize_target("zh-TW") == "zh-tw"
    assert ot.normalize_target("zh-HK") == "zh-tw"
    assert ot.normalize_target("zh-Hant") == "zh-tw"
    assert ot.normalize_target("yue") == "yue"
    assert ot.normalize_target("zh-CN") == "zh"
    # 简→繁 / 简→粤 不再被「同语」短路（否则会话「发→繁体」永远不翻）
    assert ot.should_translate("一起加油", "zh-tw", "zh")
    assert ot.should_translate("一起加油", "yue", "zh")
    assert not ot.should_translate("一起加油", "zh-CN", "zh")
    ts = _read("src/ai/translation_service.py")
    assert '"zh-tw": "Traditional Chinese"' in ts and '"yue": "Cantonese"' in ts
    assert '"zh-hant": "zh-tw"' in ts and '"zh-hk": "zh-tw"' in ts


def test_36_region_tone_profiles_in_head():
    """后半「地区语气自适应」= I-4 D1 907faa83：zh-CN / zh-TW / zh-HK 三档，CN 档恒空。"""
    from src.ai import persona_region as pr

    assert pr.normalize_region("粤语") == pr.REGION_HK
    assert pr.normalize_region("台北") == pr.REGION_TW
    assert pr.normalize_region("简体") == pr.REGION_CN
    assert pr.outbound_variant_region("zh-tw") == pr.REGION_TW
    assert pr.outbound_variant_region("yue") == pr.REGION_HK
    assert pr.outbound_variant_region("zh") == ""
    assert pr.region_block(pr.REGION_CN) == ""          # 存量 prompt 逐字不变
    assert pr.region_block(pr.REGION_TW) and pr.region_block(pr.REGION_HK)
