# -*- coding: utf-8 -*-
"""渠道中心模板遗留主题令牌防回流门禁（P0-5，2026-08-02）。

历史：四个渠道正文从旧管理后台迁入工作台壳后，散落着一批**从未接进
`.chc-scope` 变量桥**的旧令牌——`var(--t1)` / `var(--b)` / `var(--c*)` /
`var(--bg2)`。它们在工作台里解析失败（无 fallback 时属性整条失效、回退
继承色），是暗色主题下「文字颜色不对/底色消失」类视觉 bug 的稳定来源；
2026-08-02 已全量清零（正文 4 页 + 共享 partial 3 份），本门禁防止新代码
照旧模板抄回来。

合法词汇＝变量桥两侧的词：`--t/--t2/--t3`、`--bd/--bd2`、`--card/--input`、
`--p/--ps`、`--green/--red/--amber` 等（见 workspace_channels.html 的
`.chc-scope` 桥），以及工作台原生 `--tk-*`。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL_DIR = _ROOT / "src" / "web" / "templates"

_SCOPE = (
    "_channel_body_telegram.html",
    "_channel_body_line.html",
    "_channel_body_whatsapp.html",
    "_channel_body_messenger.html",
    "_rpa_shared_styles.html",
    "_rpa_shared_scripts.html",
    "_rpa_shared_funnel.html",
    "workspace_channels.html",
)

# 只抓「确定是遗留」的坏词；--t2/--t3/--bd/--card 等桥内合法词不受影响。
_LEGACY = re.compile(r"var\(--(?:t1|b|c|c2|c3|c4|bg2)\s*[,)]")


def test_channel_templates_free_of_legacy_theme_tokens():
    offenders: list[str] = []
    for name in _SCOPE:
        text = (_TPL_DIR / name).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if _LEGACY.search(line):
                offenders.append(f"{name}:{i}: {line.strip()[:120]}")
    assert not offenders, (
        "渠道模板出现未接变量桥的遗留令牌（var(--t1/--b/--c*/--bg2)），"
        "请改用 --t/--bd/--card/--input 等桥内词或 --tk-* 令牌：\n"
        + "\n".join(offenders)
    )


# ── --th-* 冻结棘轮（P3 审计结论，2026-08-02）────────────────────────────
#
# 审计实锤：渠道正文用到的 49 种 --th-* 在工作台壳一律**未定义**——122 处
# 使用全部永远吃 var(...) 回落值，不随 data-cp-theme 翻转（theme-inert）。
# 之所以没炸：全部带回落值，且多数是两主题下都过得去的语义强调色（部分
# 面板如 WA 会话区本就是暗面板设计，回落色是刻意调的）。
#
# 处置：**冻结存量、禁止新增**——批量机械翻译成主题变量会砸 122 个点位的
# 对比度（需要设计走查逐点判定，工单=tmp 审计脚本输出）；新代码一律用
# 变量桥词汇（--t/--bd/--red/--green/--amber/--ps/--gs/--rs/--as/--prs）
# 或 --tk-* 令牌。翻译一处就把对应上限下调一格（与色彩台账同规则）。
_TH_CEILINGS = {
    "_channel_body_telegram.html": 0,
    "_channel_body_line.html": 12,
    "_channel_body_whatsapp.html": 32,
    "_channel_body_messenger.html": 47,
    "workspace_channels.html": 0,
    "_rpa_shared_styles.html": 0,
    "_rpa_shared_scripts.html": 20,
    "_rpa_shared_funnel.html": 3,
}
_TH_JS = {"static/messenger/messenger_rpa.js": 31}


def test_channel_th_token_freeze_ratchet():
    import re as _re
    th = _re.compile(r"var\(--th-")
    root = _ROOT / "src" / "web"
    stale, over = [], []
    files = {**{k: _TPL_DIR / k for k in _TH_CEILINGS},
             **{k: root / k for k in _TH_JS}}
    ceil = {**_TH_CEILINGS, **_TH_JS}
    for name, path in files.items():
        n = len(th.findall(path.read_text(encoding="utf-8", errors="replace")))
        if n > ceil[name]:
            over.append(f"{name}: {n} > 上限 {ceil[name]}（新代码禁用 --th-*，走桥内词/--tk-*）")
        elif n < ceil[name]:
            stale.append(f"{name}: {n} < 上限 {ceil[name]}（请收紧台账）")
    assert not over, "--th-* 新增（theme-inert 债务只减不增）：\n" + "\n".join(over)
    assert not stale, "--th-* 台账虚高（翻译成果未锁死）：\n" + "\n".join(stale)
