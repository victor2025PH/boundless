"""渠道中心四页设计令牌卫生 ratchet。

历史：四个渠道正文从旧管理后台迁入工作台壳后，壳层 `.chc-scope` 变量桥只
映射 `--t/--t2/--t3/--bd/--bd2/--card/--input` 等词汇；旧后台残留的
`--t1/--b/--c/--c2/--c3/--c4` 在工作台作用域下**未定义**，会静默回退
（文字继承色 / 背景透明），暗色主题下呈现随机劣化且不报错。
2026-08-02 已全量归一（并行线 + P0 收口），本门禁防回流。

另含两枚 P0 回归钉：
- LINE 会话流卡标题曾把 `{{runs_count}}` 当字面量渲染给用户
  （断句键 ln_s016 + 字符串拼接），修复后禁止该占位再出现；
- WhatsApp 运维页曾同屏两份「意图分布」（共享 intentChart + 本地
  waLoadIntentStats 各带一套轮询），去重后本地实现不得复活。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL_DIR = _ROOT / "src" / "web" / "templates"
_PAGES = {
    "telegram": _TPL_DIR / "_channel_body_telegram.html",
    "line": _TPL_DIR / "_channel_body_line.html",
    "whatsapp": _TPL_DIR / "_channel_body_whatsapp.html",
    "messenger": _TPL_DIR / "_channel_body_messenger.html",
    "shell": _TPL_DIR / "workspace_channels.html",
}

# 旧管理后台令牌词（工作台 .chc-scope 变量桥不提供，用了= 静默回退）。
# 用 [,)] 收尾防误伤合法的 --t2/--bd/--card 等更长名字。
_LEGACY_TOKEN_RE = re.compile(r"var\(--(?:t1|b|c|c2|c3|c4)[,)]")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_no_legacy_admin_tokens_in_channel_pages():
    offenders: list[str] = []
    for name, path in _PAGES.items():
        for i, line in enumerate(_read(path).splitlines(), 1):
            if _LEGACY_TOKEN_RE.search(line):
                offenders.append(f"{name}:{i}: {line.strip()[:120]}")
    assert not offenders, (
        "渠道页出现工作台作用域未定义的旧后台令牌（--t1/--b/--c/--c2/--c3/--c4），"
        "请改用变量桥词汇（--t/--t2/--t3/--bd/--bd2/--card/--input）：\n"
        + "\n".join(offenders)
    )


def test_line_stream_title_placeholder_regression():
    html = _read(_PAGES["line"])
    assert "runs_count" not in html, (
        "LINE 会话流卡标题的 {{runs_count}} 字面量回归——"
        "计数应由 #lr-stream-count 在 loadLineRpaRecent 里以真实行数填充"
    )
    assert 'id="lr-stream-count"' in html
    assert "ln_stream_recent_n" in html


def test_whatsapp_local_intent_card_stays_retired():
    html = _read(_PAGES["whatsapp"])
    for marker in ("waLoadIntentStats", "_waIntentTimer",
                   "wa-intent-stats-card", "wa-intent-chart-body"):
        assert marker not in html, (
            f"WA 本地意图分布实现复活（{marker}）——运维页意图卡应只走共享 "
            "rpa.analytics.intentChart（#wa-intent-body）"
        )
    # 共享版仍在
    assert "wa-intent-body" in html
    assert "rpa.analytics.intentChart" in html
