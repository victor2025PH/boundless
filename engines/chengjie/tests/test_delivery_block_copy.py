"""delivery_block 人话文案层 + host_alert Toast 弹窗（2026-08-17 弹窗美化）门禁。

钉住三件事：
① build_popup_copy 三层文案契约（人话正文 / 技术 attribution / ×N 聚合标题）——
   错误码永不裸奔给运营，技术码永不丢失给工程师；
② seat_banner recent 带 queued 分级字段（前端红/橙分级渲染的数据地基）；
③ _toast_script 纯函数：XML 转义 / protocol 深链接 / PS 单引号转义 / 换行渲染，
   任何注入字符都不能打破 PowerShell/XML 结构。
"""
from __future__ import annotations

import pytest

from src.ops import delivery_block as db
from src.utils import host_alert as ha


@pytest.fixture(autouse=True)
def _clean_counters():
    db.reset_for_tests()
    yield
    db.reset_for_tests()


# ── build_popup_copy：三层文案契约 ──────────────────────────────────────


def test_copy_known_reason_humanized():
    c = db.build_popup_copy("chat", "cloud_failed_no_fallback",
                            conversation_id="5433982810", now=1000.0)
    assert c["title"] == "AI 回复未发出｜聊天生成"
    # 正文是人话：不含裸错误码，含「该干什么」
    assert "cloud_failed_no_fallback" not in c["message"]
    assert "云端模型连续两次未返回内容" in c["message"]
    assert "人工回复" in c["message"]
    # 技术码/会话号一个不丢，全在 attribution
    assert "chat:cloud_failed_no_fallback" in c["attribution"]
    assert "5433982810" in c["attribution"]


def test_copy_unknown_reason_falls_back_to_domain():
    c = db.build_popup_copy("voice", "weird_new_code_xyz")
    assert "weird_new_code_xyz" not in c["message"]      # 正文不裸奔
    assert "语音合成引擎异常" in c["message"]              # 按域兜底
    assert "voice:weird_new_code_xyz" in c["attribution"]  # 技术行保真


def test_copy_queued_vs_not_queued():
    q = db.build_popup_copy("voice", "tts_failed", queued_draft=True)
    n = db.build_popup_copy("voice", "tts_failed", queued_draft=False)
    assert "待发队列" in q["message"] and "一键放行" in q["message"]
    assert "替代内容" in n["message"]


def test_copy_aggregate_count_in_title_and_body():
    c1 = db.build_popup_copy("chat", "x", recent_n=1)
    c3 = db.build_popup_copy("chat", "x", recent_n=3)
    assert "×" not in c1["title"] and "已拦" not in c1["message"]
    assert c3["title"].endswith("×3")
    assert "已拦 3 条" in c3["message"]


def test_copy_empty_conversation_shows_dash():
    c = db.build_popup_copy("asr", "enrich_failed")
    assert "会话 -" in c["attribution"]


def test_report_block_passes_copy_to_notify(monkeypatch):
    got = {}

    def _fake(title, message, *, key="", cooldown_sec=0.0, open_url="",
              attribution="", **kw):
        got.update(title=title, message=message, key=key,
                   open_url=open_url, attribution=attribution)
        return True

    monkeypatch.setattr(ha, "notify_host", _fake)
    db.report_block("chat", reason="cloud_failed_no_fallback",
                    conversation_id="123456", queued_draft=False)
    assert got["title"].startswith("AI 回复未发出｜聊天生成")
    assert "云端模型" in got["message"]
    assert "chat:cloud_failed_no_fallback" in got["attribution"]
    assert got["key"] == "deliv:chat"


def test_report_block_recent_n_aggregates_same_domain(monkeypatch):
    titles = []
    monkeypatch.setattr(
        ha, "notify_host",
        lambda title, message, **kw: titles.append(title) or True)
    db.report_block("voice", reason="a")
    db.report_block("voice", reason="b")
    db.report_block("voice", reason="c")
    assert titles[0] == "AI 回复未发出｜语音合成"
    assert titles[1].endswith("×2") and titles[2].endswith("×3")


# ── seat_banner：queued 分级字段 ────────────────────────────────────────


def test_seat_banner_recent_carries_queued_flag(monkeypatch):
    monkeypatch.setattr(ha, "notify_host", lambda *a, **k: True)
    db.report_block("voice", reason="tts_failed", queued_draft=True)
    db.report_block("chat", reason="cloud_failed_no_fallback")
    banner = db.seat_banner()
    assert banner["active"] is True
    by_domain = {ev["domain"]: ev for ev in banner["recent"]}
    assert by_domain["voice"]["queued"] is True
    assert by_domain["chat"]["queued"] is False


# ── seat_banner：top_reason 主因（B73 实施67 P1-10）─────────────────────


def test_seat_banner_top_reason_is_most_frequent(monkeypatch):
    """近窗最高频 (domain, reason) 当主因，人话映射 + 原始码 + 次数齐备。"""
    monkeypatch.setattr(ha, "notify_host", lambda *a, **k: True)
    db.reset_for_tests()
    db.report_block("translate", reason="hold")
    db.report_block("translate", reason="hold")
    db.report_block("voice", reason="tts_failed")
    banner = db.seat_banner()
    assert banner["top_reason_domain"] == "translate"
    assert banner["top_reason_code"] == "hold"
    assert banner["top_reason_n"] == 2
    assert banner["top_reason"] == "出站翻译引擎不可用"


def test_seat_banner_top_reason_window_excludes_stale(monkeypatch):
    """主因按**近窗**现算——不能被进程累计里早已修好的旧故障顶成主因。"""
    monkeypatch.setattr(ha, "notify_host", lambda *a, **k: True)
    db.reset_for_tests()
    import time as _t
    now = _t.time()
    db.report_block("voice", reason="tts_failed")   # 旧故障（窗口外）
    banner = db.seat_banner(now=now + 3600, max_age_sec=1800)
    assert banner["active"] is False
    assert banner["top_reason"] == "" and banner["top_reason_n"] == 0


def test_seat_banner_top_reason_unknown_code_falls_back_to_domain(monkeypatch):
    """未登记的动态 reason 码 → 按域兜底人话，绝不因缺映射失声。"""
    monkeypatch.setattr(ha, "notify_host", lambda *a, **k: True)
    db.reset_for_tests()
    db.report_block("vision", reason="some_dynamic_reason_xyz")
    banner = db.seat_banner()
    assert banner["top_reason_code"] == "some_dynamic_reason_xyz"
    assert banner["top_reason"] == "识图引擎异常"


# ── _toast_script：结构安全（纯函数）────────────────────────────────────


def test_toast_script_escapes_xml_and_ps():
    s = ha._toast_script("标题 <a> & \"quote\"", "正文 'single' <b>\n第二行",
                         attribution="chat:x · 会话 1 · 00:00:00",
                         open_url="http://127.0.0.1:18799/workspace?a=1&b=2")
    # XML 特殊字符全部转义，原始尖括号不得出现在文本段
    assert "&lt;a&gt;" in s and "&amp;" in s
    assert "<a>" not in s and "<b>" not in s
    # PS 单引号串内的单引号必须翻倍（否则脚本注入/语法碎裂）
    assert "''single''" in s
    # 换行转 &#10;（Toast 按行渲染）
    assert "&#10;" in s
    # 深链接进 launch 与按钮 arguments（& 已转义）
    assert 'launch="http://127.0.0.1:18799/workspace?a=1&amp;b=2"' in s
    assert 'activationType="protocol"' in s
    assert "打开工作台" in s


def test_toast_script_without_url_has_no_protocol_action():
    s = ha._toast_script("t", "m")
    assert "launch=" not in s
    assert 'activationType="protocol"' not in s
    assert "知道了" in s  # 常驻 dismiss 键（reminder 场景要求至少一个 action）


def test_notify_host_new_kwargs_backcompat(monkeypatch):
    # 静默环境（conftest 置 HOST_ALERT_SILENT）下新旧签名都畅通且冷却语义不变
    monkeypatch.setenv("HOST_ALERT_SILENT", "1")
    assert ha.notify_host("t", "m", key="copykw1", cooldown_sec=1800,
                          open_url="http://x/", attribution="a:b") is True
    assert ha.notify_host("t", "m", key="copykw1", cooldown_sec=1800) is False


def test_workspace_url_env_override(monkeypatch):
    monkeypatch.setenv("AITR_WORKSPACE_URL", "http://127.0.0.1:9999/workspace")
    monkeypatch.setattr(db, "_ws_url_cache", None)
    assert db._workspace_url() == "http://127.0.0.1:9999/workspace"
    # 缓存生效：改 env 不再影响（进程级一次解析）
    monkeypatch.setenv("AITR_WORKSPACE_URL", "http://other/")
    assert db._workspace_url() == "http://127.0.0.1:9999/workspace"
