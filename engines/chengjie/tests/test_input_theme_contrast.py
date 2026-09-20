"""workspace CSS「输入控件主题配对」门禁。

一类实锤 bug（2026-08-02，.tag-add-input）：输入控件只声明 `color:var(--text-main)` 而
**不声明 background** → 背景回落 UA 默认白底（全站未设 CSS `color-scheme:dark`，UA 不随
主题走）→ 深色主题下浅字打白底＝输入的字完全不可见。反向缺口同样存在（只给深色
background 不给 color → UA 黑字打深底）。潜伏形态：两者都不声明（.kb-input 曾是）＝
深色下白块黑字，能读但突兀，且谁后补一半就复现全 bug。

守则：凡类名以 `-input` / `-textarea` 结尾的（约定俗成＝真输入控件；`.tag-input-wrap`
这类容器名不匹配后缀不误伤），其声明并集必须 **color 与 background 成对出现**。
`::placeholder` 等伪元素规则不计入并集（placeholder 的 color 不能替基础规则凑数）。

范围刻意收窄到 `src/web/static/workspace/*.css`（unified_inbox 工作台主题体系）；
其他模板各有自己的主题脚手架，宽口径会假阳。例外进 _ALLOWED_MISSING 附原因，
配 not_stale 防过期。
"""
import re
from pathlib import Path

_CSS_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "workspace"
_CSS_FILES = sorted(_CSS_DIR.glob("*.css"))

_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
_INPUT_CLASS = re.compile(r"\.([A-Za-z0-9_-]*(?:-input|-textarea))(?![A-Za-z0-9_-])")
# 属性名锚定在声明起点，防 border-color / caret-color / background-clip 之类串味
_COLOR_DECL = re.compile(r"(?:^|;)\s*color\s*:", re.S)
_BG_DECL = re.compile(r"(?:^|;)\s*background(?:-color)?\s*:", re.S)

# class -> 原因。合法缺配对的例外（如被隐藏的 opacity:0 控件）在此登记。当前为空。
_ALLOWED_MISSING: dict = {}


def scan_unpaired(css_text: str) -> dict:
    """返回 {class: [缺的属性…]}——声明并集里 color/background 不成对的输入类。"""
    color_ok: dict = {}
    bg_ok: dict = {}
    for m in _RULE.finditer(css_text):
        selector, body = m.group(1), m.group(2)
        if "::" in selector:
            continue  # 伪元素（placeholder 等）不计入基础配对
        for cm in _INPUT_CLASS.finditer(selector):
            cls = cm.group(1)
            color_ok[cls] = color_ok.get(cls, False) or bool(_COLOR_DECL.search(body))
            bg_ok[cls] = bg_ok.get(cls, False) or bool(_BG_DECL.search(body))
    bad = {}
    for cls in color_ok:
        missing = []
        if not color_ok[cls]:
            missing.append("color")
        if not bg_ok[cls]:
            missing.append("background")
        if missing:
            bad[cls] = missing
    return bad


def test_workspace_input_classes_pair_color_and_background():
    assert _CSS_FILES, f"扫描范围为空：{_CSS_DIR} 下没有 css 文件（目录挪动了？同步更新本门禁）"
    failures = {}
    for f in _CSS_FILES:
        bad = scan_unpaired(f.read_text(encoding="utf-8"))
        unexpected = {c: miss for c, miss in bad.items() if c not in _ALLOWED_MISSING}
        if unexpected:
            failures[f.name] = unexpected
    assert not failures, (
        "输入控件 color/background 未成对声明（UA 默认另一半不随主题走 → 深色下不可见）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：补齐 background:var(--bg-elev)（或 --bg-soft）与 color:var(--text-main)；"
        "确属隐藏控件等合法例外则登记 _ALLOWED_MISSING 并注明原因。"
    )


def test_detector_catches_the_original_bug():
    """探测器有效性自证：.tag-add-input 事故原样必须被抓到（门禁不是摆设）。"""
    buggy = ".tag-add-input{border:1px dashed #8bc4f8;color:var(--text-main);}"
    assert scan_unpaired(buggy) == {"tag-add-input": ["background"]}
    latent = ".kb-input{width:100%;border:1px solid var(--border);outline:none;}"
    assert scan_unpaired(latent) == {"kb-input": ["color", "background"]}
    # placeholder 的 color 不得替基础规则凑数
    cheat = (
        ".x-input{background:var(--bg-elev);}"
        ".x-input::placeholder{color:var(--text-sub);}"
    )
    assert scan_unpaired(cheat) == {"x-input": ["color"]}
    ok = ".y-input{color:var(--text-main);background:var(--bg-elev);}"
    assert scan_unpaired(ok) == {}


def test_allowed_missing_not_stale():
    """例外表防过期：登记的类必须仍然存在且仍缺配对，修好后强制回收恢复门禁强度。"""
    all_bad: dict = {}
    for f in _CSS_FILES:
        all_bad.update(scan_unpaired(f.read_text(encoding="utf-8")))
    stale = [c for c in _ALLOWED_MISSING if c not in all_bad]
    assert not stale, f"_ALLOWED_MISSING 里这些条目已修好或已消失，请回收：{stale}"


# ── 第二档：var() 引用必须可解析 ─────────────────────────────────────────────
# 姊妹缺口实锤（2026-08-02，.tlm-add-input）：配对齐了但 background:var(--bg-main)
# 引用了**不存在的 token** → 计算值 invalid → 背景照样透明回落 UA 白底，
# 配对门禁完全无感（声明在，只是解析不出）。守则：workspace css 里**无 fallback**
# 的 var() 引用，其 token 必须在全站某处有定义（css/模板 style/JS setProperty）；
# 带 fallback 的 var(--x, #fff) 天然安全不查。

_WEB_ROOT = Path(__file__).resolve().parents[1] / "src" / "web"
_SHARED_ROOT = Path(__file__).resolve().parents[1] / "shared"
_VAR_REF_NO_FALLBACK = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*\)")
_VAR_DEF = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")
_VAR_SET_PROP = re.compile(r"setProperty\(\s*['\"](--[A-Za-z0-9_-]+)")


def collect_defined_tokens() -> set:
    files = list(_WEB_ROOT.rglob("*.css")) + list(_WEB_ROOT.rglob("*.html")) + list(_WEB_ROOT.rglob("*.js"))
    if _SHARED_ROOT.exists():
        for ext in ("*.css", "*.html", "*.js"):
            files += list(_SHARED_ROOT.rglob(ext))
    defined: set = set()
    for p in files:
        t = p.read_text(encoding="utf-8", errors="ignore")
        defined |= set(_VAR_DEF.findall(t))
        defined |= set(_VAR_SET_PROP.findall(t))
    return defined


def scan_undefined_vars(css_text: str, defined: set) -> list:
    return sorted({n for n in _VAR_REF_NO_FALLBACK.findall(css_text) if n not in defined})


def test_workspace_css_no_fallback_vars_resolve():
    defined = collect_defined_tokens()
    assert defined, "token 定义采集为空（目录挪动了？同步更新本门禁）"
    failures = {}
    for f in _CSS_FILES:
        bad = scan_undefined_vars(f.read_text(encoding="utf-8"), defined)
        if bad:
            failures[f.name] = bad
    assert not failures, (
        "无 fallback 的 var() 引用了全站未定义的 token（计算值 invalid → 属性静默失效，"
        "背景/前景回落 UA 默认＝主题事故温床）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：改用已定义 token（如 --bg-soft/--bg-elev/--text-main），或补 var(--x, 兜底值)。"
    )


def test_var_detector_catches_the_original_bug():
    """探测器有效性自证：.tlm-add-input 事故原样（--bg-main 未定义）必须被抓到。"""
    defined = {"--text-main", "--bg-soft"}
    buggy = ".tlm-add-input{color:var(--text-main);background:var(--bg-main);}"
    assert scan_undefined_vars(buggy, defined) == ["--bg-main"]
    with_fallback = ".x-input{background:var(--bg-main,#fff);}"  # 有 fallback＝安全，不查
    assert scan_undefined_vars(with_fallback, defined) == []
    ok = ".y-input{background:var(--bg-soft);}"
    assert scan_undefined_vars(ok, defined) == []


# ── 第三档：全站模板内联 <style>，按模板继承链精确解析（2026-08-02） ─────────
# 首轮全扫实锤 3 文件 139 处（personas.html 的 --t1×71/--bg2×50/--bg1×8 整页
# 主题层级静默失效多年；_channel_body_line 的 --b/--c*/─渠道正文卡片无底；
# _channel_body_telegram --p2），全部已改 base 词汇同义 token 后本门禁落地为零豁免。
#
# 为什么要解析继承链（而不是「全站任意处定义即算数」）：personas 的 --t1×71 正是被
# admin_tts_dashboard 自己 :root 里的同名 token 跨页掩护多年——宽口径对这类互不相干
# 页面的同名巧合是全盲的。本档口径：模板可用 token ＝
#   ① 自身 + extends 祖先 + include 子孙（动态拼接如 "_channel_body_" ~ channel
#      按字面量段转通配匹配）链上定义的 token；
#   ② partial 的「有效集」取所有宿主链的**并集**——只在「所有宿主里都解析不出」时
#      才违规（某宿主缺失属条件性缺陷，宽进保零假阳）；无宿主的孤儿按自身链算；
#   ③ 全站静态资产（static/shared 的 css/js）里的定义视为全局可达（页面挂哪些
#      <link>/<script src> 不逐页解析——共享资产本就是跨页刻意共用，掩护风险低）。
#      该松度已经 tools/audit_template_var_links.py 实证（2026-08-02 基线）：全站仅
#      6 处引用是 static-only 解析（login/setup ↔ auth-surface.css），且全部真实挂载
#      ＝零暴露；故不把挂载检查做进门禁。新页面若大量走该模式，重跑工具复核。

_TPL_ROOT = _WEB_ROOT / "templates"
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_EXTENDS_RE = re.compile(r"\{%-?\s*extends\s+['\"]([^'\"]+)['\"]")
_INCLUDE_TAG_RE = re.compile(r"\{%-?\s*include\s+(.+?)-?%\}", re.S)
_INCLUDE_KEYWORDS = re.compile(r"\b(?:ignore\s+missing|with\s+context|without\s+context)\b")
_STR_LIT = re.compile(r"['\"]([^'\"]+)['\"]")

# 模板相对路径(posix) -> {token…}。JS 动态拼名等合法例外在此登记（附原因）。当前为空。
_TPL_VAR_ALLOWED: dict = {}


def load_templates() -> dict:
    return {
        p.relative_to(_TPL_ROOT).as_posix(): p.read_text(encoding="utf-8", errors="ignore")
        for p in _TPL_ROOT.rglob("*.html")
    }


def collect_static_tokens() -> set:
    """static/shared 静态资产（css/js）里的 token 定义＝全局可达。"""
    files = list((_WEB_ROOT / "static").rglob("*.css")) + list((_WEB_ROOT / "static").rglob("*.js"))
    if _SHARED_ROOT.exists():
        for ext in ("*.css", "*.html", "*.js"):
            files += list(_SHARED_ROOT.rglob(ext))
    tokens: set = set()
    for p in files:
        t = p.read_text(encoding="utf-8", errors="ignore")
        tokens |= set(_VAR_DEF.findall(t))
        tokens |= set(_VAR_SET_PROP.findall(t))
    return tokens


def _include_pattern(expr: str):
    """include 表达式 → 模板名或通配模式。'"a_" ~ ch ~ ".html"' → 'a_*.html'。"""
    expr = _INCLUDE_KEYWORDS.sub("", expr).strip()
    lits = _STR_LIT.findall(expr)
    if not lits:
        return None
    pat = "*".join(lits)
    if not expr.startswith(("'", '"')):
        pat = "*" + pat
    if not expr.endswith(("'", '"')):
        pat = pat + "*"
    return pat


def effective_defined_by_template(files: dict, static_tokens: set) -> dict:
    """{模板名: 有效 token 集}（口径见档头注释）。files={相对名: 文本}，纯函数可自证。"""
    import fnmatch
    names = list(files)
    ext, incs, own = {}, {}, {}
    for name, text in files.items():
        m = _EXTENDS_RE.search(text)
        ext[name] = m.group(1) if m else None
        resolved: set = set()
        for im in _INCLUDE_TAG_RE.finditer(text):
            pat = _include_pattern(im.group(1))
            if not pat:
                continue
            if "*" in pat or "?" in pat:
                resolved |= {n for n in names if fnmatch.fnmatch(n, pat)}
            elif pat in files:
                resolved.add(pat)
        incs[name] = resolved
        own[name] = set(_VAR_DEF.findall(text)) | set(_VAR_SET_PROP.findall(text))

    chains: dict = {}

    def chain(name: str) -> set:
        if name in chains:
            return chains[name]
        seen: set = set()
        stack = [name]
        while stack:
            n = stack.pop()
            if n in seen or n not in files:
                continue
            seen.add(n)
            if ext.get(n):
                stack.append(ext[n])
            stack.extend(incs.get(n, ()))
        chains[name] = seen
        return seen

    chain_tokens = {
        n: set().union(*(own[m] for m in chain(n))) | static_tokens for n in names
    }
    eff = {}
    for n in names:
        hosts = [h for h in names if n in chain(h)]  # 含自身
        eff[n] = set().union(*(chain_tokens[h] for h in hosts)) if hosts else chain_tokens[n]
    return eff


def test_template_inline_styles_no_fallback_vars_resolve():
    files = load_templates()
    assert files, f"模板采集为空：{_TPL_ROOT}（目录挪动了？同步更新本门禁）"
    eff = effective_defined_by_template(files, collect_static_tokens())
    failures = {}
    for name in sorted(files):
        bad: set = set()
        for m in _STYLE_BLOCK.finditer(files[name]):
            bad |= set(scan_undefined_vars(m.group(1), eff[name]))
        bad -= set(_TPL_VAR_ALLOWED.get(name, ()))
        if bad:
            failures[name] = sorted(bad)
    assert not failures, (
        "模板 <style> 里无 fallback 的 var() 在其继承链（extends/include + 静态资产）内"
        "解析不出（属性静默失效＝主题事故温床）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：改用宿主主题已定义 token（base 词汇 --card/--input/--t/--bd 等），"
        "或补 var(--x, 兜底值)；确属动态注入登记 _TPL_VAR_ALLOWED 附原因。"
    )


def test_tpl_var_allowlist_not_stale():
    """例外表防过期：登记的 (模板, token) 必须仍在违规集内，修好后强制回收。"""
    files = load_templates()
    eff = effective_defined_by_template(files, collect_static_tokens())
    stale = []
    for fname, tokens in _TPL_VAR_ALLOWED.items():
        cur: set = set()
        if fname in files:
            for m in _STYLE_BLOCK.finditer(files[fname]):
                cur |= set(scan_undefined_vars(m.group(1), eff[fname]))
        stale += [f"{fname}:{t}" for t in tokens if t not in cur]
    assert not stale, f"_TPL_VAR_ALLOWED 里这些条目已修好或已消失，请回收：{stale}"


def test_chain_scope_catches_cross_page_masking():
    """探测器自证：--t1 跨页掩护事故原样（personas × admin_tts_dashboard）必须被抓到。"""
    files = {
        "base.html": "<style>:root{--t:#111}</style>",
        "personas.html": '{% extends "base.html" %}<style>.x{color:var(--t1);background:var(--t)}</style>',
        "admin_tts.html": "<style>:root{--t1:#eee}.y{color:var(--t1)}</style>",
        "_part.html": "<style>.z{color:var(--pp)}</style>",
        "host.html": '{% extends "base.html" %}{% include "_part.html" %}<style>:root{--pp:red}</style>',
        "line_page.html": '{% include "_channel_body_" ~ channel ~ ".html" %}<style>:root{--lb:blue}</style>',
        "_channel_body_x.html": "<style>.k{border-color:var(--lb)}</style>",
    }
    eff = effective_defined_by_template(files, {"--static-tok"})
    assert "--t1" not in eff["personas.html"], "跨页同名 token 不得再掩护（旧宽口径的盲区）"
    assert "--t" in eff["personas.html"] and "--static-tok" in eff["personas.html"]
    assert "--t1" in eff["admin_tts.html"], "自己页里定义的自己可用"
    assert "--pp" in eff["_part.html"], "partial 经静态 include 应拿到宿主链 token"
    assert "--lb" in eff["_channel_body_x.html"], "动态拼接 include 应经通配解析到宿主"
