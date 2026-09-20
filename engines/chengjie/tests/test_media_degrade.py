"""识别失败「降级诚实自动回」+ 会话诊断拦截明细 门禁（实施56 P1，2026-08-22）。

背景：08-17 无兜底纪律＝图片/语音识别失败 → 整条回复拦下（宁可不回不装懂）；
08-22 老板拍板「不要再有小问题掐断全自动」。两道拍板的和解＝
``inbox.auto_draft.media_degrade_reply``：

- **代码默认 False**——服务器实例（zhiliao 等）维持无兜底纪律，行为零变化；
- **客户桌面包默认 True**（种子 + A 类基线补齐存量安装）——识别失败不再扣留，
  两条自动链放行生成，ai_client 媒体块对无描述媒体自带「自然承认收到 + 温和
  追问」话术（模型明确知道自己没看到内容，是诚实降级不是装懂/垫场）。

同批：``delivery_block.recent_blocks_for`` + ``reply_diagnosis`` 的 ``media_block``
finding——被拦的那几条在会话诊断面板就地有答案（「这条为什么没回」）。
"""

from __future__ import annotations

import time
from pathlib import Path

from src.inbox.media_enrich import media_degrade_reply_enabled

_SRC = Path(__file__).resolve().parents[1] / "src"


# ── 判定单点 ─────────────────────────────────────────────────────────────────


def test_degrade_default_off_preserves_no_fallback_decree():
    assert media_degrade_reply_enabled({}) is False
    assert media_degrade_reply_enabled(None) is False
    assert media_degrade_reply_enabled({"inbox": "bad-shape"}) is False


def test_degrade_enabled_via_config():
    cfg = {"inbox": {"auto_draft": {"media_degrade_reply": True}}}
    assert media_degrade_reply_enabled(cfg) is True
    assert media_degrade_reply_enabled(
        {"inbox": {"auto_draft": {"media_degrade_reply": False}}}) is False


# ── 两条链的接线钉（与 test_no_fallback_discipline 的 hold 字符串钉互补：
#    hold 分支必须还在（默认路径），degrade 分支必须已接同一判定单点）────────


def test_both_chains_wired_to_single_gate():
    proto = (_SRC / "integrations" / "protocol_autoreply.py").read_text(
        encoding="utf-8")
    ad = (_SRC / "inbox" / "autodraft_helpers.py").read_text(encoding="utf-8")
    for src, name in ((proto, "protocol_autoreply"), (ad, "autodraft_helpers")):
        assert "media_degrade_reply_enabled" in src, (
            f"{name} 未接降级判定单点——两链必须同判，只修一半＝协议号和拟稿链"
            "行为分叉")
    # 降级放行后的诚实话术由 ai_client 媒体块承载（无 desc 媒体 → 承认+追问），
    # 该块是既有路径；这里钉住它没被删（删了＝降级变成「装没事」）。
    ai = (_SRC / "ai" / "ai_client.py").read_text(encoding="utf-8")
    assert "内容暂无法识别" in ai


def test_seed_and_registry_carry_customer_default():
    import yaml
    from src.utils.feature_registry import by_key

    f = by_key("inbox.auto_draft.media_degrade_reply")
    assert f is not None and f.cls == "A" and f.baseline is True
    assert f.show is False
    seed = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config"
         / "config.desktop.min.yaml").read_text(encoding="utf-8"))
    assert seed["inbox"]["auto_draft"]["media_degrade_reply"] is True


# ── delivery_block.recent_blocks_for ─────────────────────────────────────────


def test_recent_blocks_for_filters_by_conversation_and_window():
    from src.ops.delivery_block import (
        recent_blocks_for, report_block, reset_for_tests,
    )
    reset_for_tests()
    try:
        report_block("vision", reason="enrich_failed", platform="telegram",
                     conversation_id="telegram:a:100")
        report_block("asr", reason="enrich_failed", platform="telegram",
                     conversation_id="telegram:a:100", queued_draft=True)
        report_block("vision", reason="enrich_failed", platform="telegram",
                     conversation_id="telegram:a:OTHER")
        evs = recent_blocks_for("telegram:a:100")
        assert [e["domain"] for e in evs] == ["vision", "asr"]
        assert evs[1]["queued"] is True
        assert recent_blocks_for("telegram:a:none") == []
        assert recent_blocks_for("") == []
        # 时窗：把 now 推到窗外 → 全部过期
        far = time.time() + 3600.0
        assert recent_blocks_for("telegram:a:100", now=far) == []
    finally:
        reset_for_tests()


# ── reply_diagnosis media_block finding ──────────────────────────────────────


def _diag_findings(cid_platform="telegram", account="a", chat="100"):
    from src.inbox.reply_diagnosis import diagnose_conversation
    # 发送链全开：让链路级 findings 干净，只考察本批新增的 media_block 语义
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}
    out = diagnose_conversation(
        None, cfg, platform=cid_platform, account_id=account, chat_key=chat)
    return out, {f["code"]: f for f in out["findings"]}


def test_diagnosis_surfaces_recent_media_blocks():
    from src.ops.delivery_block import report_block, reset_for_tests
    reset_for_tests()
    try:
        report_block("vision", reason="enrich_failed", platform="telegram",
                     conversation_id="telegram:a:100")
        report_block("vision", reason="enrich_failed", platform="telegram",
                     conversation_id="telegram:a:100")
        out, by = _diag_findings()
        f = by.get("media_block")
        assert f is not None and f["level"] == "warn"
        assert f["params"]["domain"] == "vision"
        assert f["params"]["n"] == 2
        assert f["params"]["reason"] == "enrich_failed"
        assert f["params"]["queued"] is False
        assert out["recent_blocks"]["vision"]["n"] == 2
    finally:
        reset_for_tests()


def test_diagnosis_no_blocks_no_finding():
    from src.ops.delivery_block import reset_for_tests
    reset_for_tests()
    out, by = _diag_findings()
    assert "media_block" not in by
    assert "recent_blocks" not in out


def test_diagnosis_media_block_is_warn_not_block():
    """链路本身没死：media_block 不得把 looks_alive 顶掉（诚实分级——
    「以后会回」与「刚才那几条被拦」是两件事，混成 block 会吓坐席）。"""
    from src.ops.delivery_block import report_block, reset_for_tests
    reset_for_tests()
    try:
        report_block("asr", reason="enrich_failed", platform="telegram",
                     conversation_id="telegram:a:100", queued_draft=True)
        _, by = _diag_findings()
        assert by["media_block"]["level"] == "warn"
        assert by["media_block"]["params"]["queued"] is True
        assert "looks_alive" in by
    finally:
        reset_for_tests()


# ── 前端接线 + 词条双语 ──────────────────────────────────────────────────────


def test_diag_modal_renders_media_block():
    html = (_SRC / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8")
    assert "media_block:1" in html, "known 码表缺 media_block＝finding 显示成 unknown"
    assert "ws.delivblock.d.'+fmt.domain" in html, "域码必须译人话再入模板"


def test_media_block_i18n_bilingual():
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    en = get_translations("en")
    for k in ("inbox.diag.media_block",):
        assert k in zh and k in en
        assert "{n}" in zh[k] and "{domain}" in zh[k]
        assert "{n}" in en[k] and "{domain}" in en[k]
