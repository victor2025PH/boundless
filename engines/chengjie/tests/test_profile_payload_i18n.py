"""``/api/unified-inbox/profile`` 载荷 i18n 缺口回填门禁（2026-08-20）。

背景：`_build_profile` / `_profile_tags` 是 08-19「EN 界面后端展示串」主批
（test_i18n_en_payload_gate）之外仅存的两处硬编码中文——stage 兜底
（稳定陪伴/升温/初识）与画像 tags（待回复/关系升温/…）。消费方＝桌面壳右栏
（desktop/main.js ``desktop:profile``）。收口口径与主批一致：载荷带机器码
（stage_key / tags_keys），展示标签按请求语言出词典，zh 文案逐字保持。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.web.routes.unified_inbox_context import _REL_STAGE_KEYS, _profile_tags


def _req(lang: str):
    return SimpleNamespace(state=SimpleNamespace(ui_lang=lang))


_CHAT = {"language": "en", "unread": 2}
_MSGS = [{"text": f"m{i}"} for i in range(8)]
_CONTACTS = {
    "funnel_stage": "HANDOFF_READY",
    "intimacy_score": 88,
    "is_returning": True,
    "attributes": {"age": "30"},
}


def test_profile_tags_zh_verbatim_stable():
    """zh 展示值必须与旧硬编码逐字一致（中文界面零漂移）。"""
    pairs = _profile_tags(_CHAT, _MSGS, ["有个记忆"], _CONTACTS, request=_req("zh"))
    assert [p[1] for p in pairs] == [
        "语言:en", "待回复", "关系升温", "有记忆", "引流中", "高亲密", "老客户", "已留资",
    ]
    assert [p[0] for p in pairs] == [
        "lang:en", "unreplied", "warming", "memory", "handoff",
        "high_intimacy", "returning", "has_lead_info",
    ]


def test_profile_tags_en_zero_cjk():
    """en 请求下全部标签零汉字（{lang} 是语言码数据，本就非中文）。"""
    pairs = _profile_tags(_CHAT, _MSGS, ["有个记忆"], _CONTACTS, request=_req("en"))
    joined = " | ".join(lbl for _, lbl in pairs)
    assert pairs and not any("\u4e00" <= c <= "\u9fff" for c in joined), joined


def test_profile_tags_without_request_falls_back_zh():
    """直调（无 request）回落中文默认——绝不抛、绝不空。"""
    pairs = _profile_tags(_CHAT, _MSGS, [], None, request=None)
    assert [p[1] for p in pairs] == ["语言:en", "待回复", "关系升温"]


def test_rel_stage_keys_pinned_to_companion_relationship():
    """context 静态复刻的阶段码集必须与 companion_relationship.STAGE_ORDER 全等
    ——两边漂移＝新阶段码在 profile 载荷里被当「历史自由文本」透传。"""
    from src.utils.companion_relationship import STAGE_ORDER

    assert tuple(_REL_STAGE_KEYS) == tuple(STAGE_ORDER)
