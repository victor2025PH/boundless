# -*- coding: utf-8 -*-
"""copilot 词典键可解析 + 缓存戳跨宿主一致 门禁（2026-08-22「cp.app.h_nurture 裸键」事故沉淀）。

事故链：08-22 04:30 批次给 app.html 新增养号卡并在 cp-i18n.js 注册标题键 cp.app.h_nurture，
但 app.html 里 cp-i18n.js 的 ``?v=`` 停在 20260821a → 浏览器命中旧缓存词典（无新键）→
applyI18n 拿 t() 缺键回落值（键名本身）覆盖掉模板内联中文兜底 → 工具箱卡标题对用户直显
``cp.app.h_nurture`` 裸串。同类潜伏病当日盘点还有三例（copilot-client 20260818c vs
20260822b / cp-image 22b vs 21a / cp-collab 12a vs 01b——两宿主戳分叉＝至少一侧用户吃旧缓存）。

两道机制化收口（配合 cp-i18n.js applyI18n 的「缺键保内联兜底」运行时降级）：

1. **静态键可解析**（test_static_i18n_keys_resolve_bilingual）：app.html 的
   data-cp-i18n(-ph/-title) 属性键、共享组件与 app.html 内联脚本里的静态字面量
   t("cp.…")/tf("cp.…")/CP_T("cp.…")，必须在 cp-i18n.js 出现 ≥2 次（reg(zh,en) 双语齐备）。
   动态拼接键（"cp.nurture.beh_" + k）静态扫不到，刻意不管——运行时另有 applyI18n 缺键
   保兜底 + 组件侧 _tt() raw-key 自检兜底。
   合流备注：app.html 属性键半边与同晨并行线的 tests/test_cp_app_i18n_keys.py ①重叠
   （同一事故两面各自建门禁后合流保留互锁）；本门禁独有面＝组件 JS 静态字面量扫描。
2. **缓存戳跨宿主一致**（test_asset_stamps_consistent_across_hosts）：同一 /copilot/* 资源
   在 app.html 与 unified_inbox.html 的 ``?v=`` 必须相等。改词典/组件内容 → 两宿主同批
   bump 同值（桌面树 app.html 由 test_copilot_shared_sync 字节一致兜底，无需单列）。
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "shared" / "copilot" / "app.html"
_INBOX = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_CP_I18N = _ROOT / "shared" / "copilot" / "i18n" / "cp-i18n.js"

# 静态字面量键扫描面：共享组件 + chrome + 能力表 + app.html（内联脚本）。
# client 是纯数据层不产 UI 文案，刻意不扫。
def _scan_js_files():
    files = sorted((_ROOT / "shared" / "copilot" / "components").glob("*.js"))
    for extra in ("sidebar-chrome.js", "cp-capabilities.js"):
        p = _ROOT / "shared" / "copilot" / extra
        if p.is_file():
            files.append(p)
    return files


_ATTR_RE = re.compile(r'data-cp-i18n(?:-ph|-title)?="([^"]+)"')
# [^\w$] 前置：匹配 `.t(` / ` t(` / `(t(`，排除 parseFloat( / split( 这类词尾 t；
# 结尾 ["']\s*[,)] ：排除 "cp.xxx." + var 的拼接段（那是动态键，运行时兜底负责）。
_CALL_RE = re.compile(r'''[^\w$]tf?\(\s*["'](cp\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)["']\s*[,)]''')
_CPT_RE = re.compile(r'''[^\w$]CP_T\(\s*["'](cp\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)["']\s*[,)]''')
_SRC_RE = re.compile(r'src="/copilot/([^"?]+)\?v=([\w-]+)"')


def _dict_key_counts():
    src = _CP_I18N.read_text(encoding="utf-8")
    counts = {}
    for k in re.findall(r'"(cp\.[A-Za-z0-9_.]+)"\s*:', src):
        counts[k] = counts.get(k, 0) + 1
    return counts


def test_static_i18n_keys_resolve_bilingual():
    counts = _dict_key_counts()
    assert counts, "cp-i18n.js 解析不到任何词条（结构变了？更新本门禁）"
    used = {}

    html = _APP.read_text(encoding="utf-8")
    for k in _ATTR_RE.findall(html):
        if k.startswith("cp."):
            used.setdefault(k, set()).add("app.html[attr]")
    for m in _CPT_RE.finditer(html):
        used.setdefault(m.group(1), set()).add("app.html[CP_T]")
    for m in _CALL_RE.finditer(html):
        used.setdefault(m.group(1), set()).add("app.html[t()]")

    for p in _scan_js_files():
        src = p.read_text(encoding="utf-8")
        for m in _CALL_RE.finditer(src):
            used.setdefault(m.group(1), set()).add(p.name)
        for m in _CPT_RE.finditer(src):
            used.setdefault(m.group(1), set()).add(p.name)

    assert used, "未扫到任何静态 cp.* 键引用（结构变了？更新本门禁）"
    problems = []
    for k, where in sorted(used.items()):
        n = counts.get(k, 0)
        if n < 2:
            problems.append(f"{k}（cp-i18n 出现 {n} 次，需 zh+en ≥2）← {'/'.join(sorted(where))}")
    assert not problems, (
        "静态引用的词典键缺失或单语（补进 shared/copilot/i18n/cp-i18n.js 对应 reg(zh,en) 两侧；"
        "确属动态拼接误报则改写成完整字面量或调整本门禁）：\n  " + "\n  ".join(problems))


def test_asset_stamps_consistent_across_hosts():
    app = dict(_SRC_RE.findall(_APP.read_text(encoding="utf-8")))
    inbox = dict(_SRC_RE.findall(_INBOX.read_text(encoding="utf-8")))
    assert "i18n/cp-i18n.js" in app and "i18n/cp-i18n.js" in inbox, \
        "宿主未引用 cp-i18n.js（结构变了？更新本门禁）"
    common = sorted(set(app) & set(inbox))
    assert common, "两宿主无共同 /copilot 资源（结构变了？更新本门禁）"
    diff = [f"{f}: app.html={app[f]} vs unified_inbox.html={inbox[f]}" for f in common if app[f] != inbox[f]]
    assert not diff, (
        "同一 /copilot 资源两宿主缓存戳分叉——至少一侧用户在吃旧缓存；"
        "改内容必须两宿主同批 bump 同值（另记得 ui-build.txt + 桌面镜像拷贝）：\n  "
        + "\n  ".join(diff))
