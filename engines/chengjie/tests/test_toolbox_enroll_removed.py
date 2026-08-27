# -*- coding: utf-8 -*-
"""实施74 阶段5 门禁：B115 工具箱撤克隆录入 + P2-h 顶栏中宽收敛（静态钉）。

- B115（0826 _551，B25-① 口径落定）：副驾「生成语音」工具箱撤录入/登记整块，
  克隆登记只留人设页语音区（cp-voice ``mode="enroll"`` 档）一个入口；
  生成/试听/发送保留，hint 指路人设页防「功能消失」误报。
- P2-h（skuio 三催 _574）：~900-1100px 壳窗 + 英文界面顶栏溢出，「聊天/客户」组
  被截——新增 1120px 断点收文字不收入口。
"""
from __future__ import annotations

from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ENGINE_ROOT / rel).read_text(encoding="utf-8")


# ── B115 ─────────────────────────────────────────────────────────────────────

def test_toolbox_enroll_block_removed():
    src = _read("shared/copilot/components/cp-voice.js")
    # 工具箱入口按钮与其点击分支已撤（注释里的历史说明不算）
    assert 'data-act="toggle-enroll"' not in src
    assert 'act === "toggle-enroll"' not in src
    assert "prefillEnroll(data)" not in src     # 消息导入死代码一并撤
    assert 'act === "clear-src"' not in src
    # 会话档 hint 指路人设页（防「功能消失」误报）
    assert "cp.voice.enroll_moved" in src


def test_persona_page_enroll_mode_retained():
    """人设页 mode="enroll" 档＝唯一登记入口，必须完整保留。"""
    src = _read("shared/copilot/components/cp-voice.js")
    assert "_renderEnrollOnly" in src
    assert "_enrollHtml" in src
    assert '"enroll-submit"' in src and '"rebind"' in src
    # 人设页挂载点还在
    assert 'mode="enroll"' in _read("src/web/templates/personas.html")


def test_enroll_moved_key_bilingual():
    src = _read("shared/copilot/i18n/cp-i18n.js")
    assert src.count('"cp.voice.enroll_moved"') >= 2  # zh + en 两份词典


def test_cache_stamps_bumped():
    """?v= 双戳纪律：改共享组件必须 bump 三处引用（unified_inbox/personas/app）。"""
    for rel in ("src/web/templates/unified_inbox.html",
                "src/web/templates/personas.html",
                "shared/copilot/app.html"):
        src = _read(rel)
        assert "cp-voice.js?v=20260823e" not in src, rel
        assert "cp-voice.js?v=20260827" in src, rel


# ── P2-h ─────────────────────────────────────────────────────────────────────

def test_topbar_midwidth_breakpoint():
    src = _read("src/web/templates/workspace_base.html")
    i = src.find("@media (max-width:1120px)")
    assert i > 0, "1120px 中宽断点缺失（P2-h）"
    block = src[i:i + 900]
    assert ".ws-nav a .ws-nav-lbl{display:none;}" in block
    assert ".ws-pill.quota{max-width" in block      # 额度徽章限宽省略
    # 旧 768 档不动（窄屏行为保持）
    assert "@media (max-width:768px)" in src
