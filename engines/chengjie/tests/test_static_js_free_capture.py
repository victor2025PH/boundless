"""static JS / desktop / shared 三棵树的「自由变量捕获」门禁（模板门禁的扩面批次）。

覆盖 `src/web/static/**/*.js` + `desktop/**/*.js` + `shared/**/*.js`——与
tests/test_template_free_capture.py 同一判据（顶层函数读取「同文件闭包里才有声明」
的标识符＝调用即 ReferenceError，contact360 事故同款），扫描核心同源
（tests/_free_capture_scan.py::free_captures_js）。裸 JS 与模板的差异已在核心处理：
无 Jinja 剥离；「顶层」在浏览器经典脚本＝真全局、在 Node/CJS 与 ESM＝模块作用域，
但同文件分析语义完全等价。

收集规则（排除面各有理由）：
  - ``*.min.js``＝vendor 压缩件（chart.umd.min.js），非第一方代码；
  - 路径含 ``node_modules`` / ``dist`` / ``build``＝依赖与构建产物
    （desktop/build/** 是打包快照，源头修了它自然跟）；
  - 路径含 ``vendor``＝第三方原样引入的授权产物（如 static/vendor/
    emoji-picker-element 的 +esm 预打包件，2026-08-04）——与 ``*.min.js``
    同理由：压缩/打包器产物的作用域形态不属本仓代码风格辖区，且不可改写；
  - ``desktop/renderer/shared/**``＝``shared/**`` 的镜像（gate_sweep 的共享组件
    双树同步门禁守恒一致性），只扫源树防同一发现双报。

首轮基线（2026-08-02）：59 文件 / 222 个顶层函数 / 2193 个闭包声明在检，零命中
（desktop 有自建 Node 测试文化、static 组件小而专）。``test_scan_surface_has_teeth``
钉牙口下限——防将来目录挪动/收集规则改坏把门禁磨成「扫了个空集」的假绿。
"""
from pathlib import Path

from tests import _free_capture_scan as fc
from tests import _inline_handler_scan as scan

_ROOT = Path(__file__).resolve().parents[1]
_TPL_DIR = _ROOT / "src" / "web" / "templates"
_EXCLUDE_PARTS = {"node_modules", "dist", "build", "vendor"}


def _is_artifact_part(part: str) -> bool:
    """dist / build 的连字符、下划线变体（dist-hotfix 等）＝构建产物家族。

    2026-08-06 实锤：``desktop/dist-hotfix/win-unpacked/**``（打包快照解包件）落树后
    被当源码扫出两条假阳性——minified/bundled 产物的作用域形态不属本仓代码风格辖区
    （收集注释本就写「路径含 dist/build」，实现却是整段精确匹配，此处把语义对齐）。
    刻意只收 ``-``/``_`` 变体、不用裸 startswith——防 distance_*/builder_* 类源码目录误伤。
    """
    return part.startswith(("dist-", "dist_", "build-", "build_"))

# 已知真 bug 待产品决策的登记台账：{相对路径: {函数名: [标识符, ...]}}。
_PENDING_JS_FREE_CAPTURES = {}


def _collect_js_files():
    files = []
    for root in (_ROOT / "src" / "web" / "static", _ROOT / "desktop", _ROOT / "shared"):
        if not root.exists():
            continue
        for f in sorted(root.rglob("*.js")):
            if set(f.parts) & _EXCLUDE_PARTS or any(_is_artifact_part(p) for p in f.parts):
                continue
            if f.name.endswith(".min.js"):
                continue
            if f.relative_to(_ROOT).parts[:3] == ("desktop", "renderer", "shared"):
                continue
            files.append(f)
    return files


_ALL_JS = _collect_js_files()
_TEXTS = {f: f.read_text(encoding="utf-8", errors="replace") for f in _ALL_JS}


def _ambient():
    """模板 ambient ∪ 全部被扫 js 的顶层全局/window 暴露 ∪ Node/SW 环境全局。

    跨文件全局进 ambient 是零误报的代价（同名「别处全局 + 本文件闭包」的引用
    实际可解析）；flag 前提仍是「同文件闭包里有声明」，宽 ambient 只多放不误伤。"""
    amb = scan.ambient_globals(_TPL_DIR)
    amb |= fc.NODE_ENV_GLOBALS | fc.SW_ENV_GLOBALS
    for t in _TEXTS.values():
        amb |= scan._global_names_in_block(t) | scan.window_exposed(t)
    return amb


_AMBIENT = _ambient()


def test_no_free_captures_in_js_trees():
    failures = {}
    for f in _ALL_JS:
        rel = str(f.relative_to(_ROOT)).replace("\\", "/")
        pend = _PENDING_JS_FREE_CAPTURES.get(rel, {})
        bad = {}
        for fn, names in fc.free_captures_js(_TEXTS[f], ambient=_AMBIENT):
            missing = sorted(set(names) - set(pend.get(fn, ())))
            if missing:
                bad[fn] = missing
        if bad:
            failures[rel] = bad
    assert not failures, (
        "有 JS 文件的**顶层函数**读取了只在闭包（IIFE/函数体）里声明的标识符——"
        "运行时一调用必 ReferenceError（contact360 死页面同款）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：函数移回闭包 / 函数内补局部定义 / 声明提升到文件顶层（或挂 window）。"
    )


def test_pending_js_free_captures_still_broken():
    """防 _PENDING 过期：修好后不再命中 → 提醒回收，恢复门禁强度。"""
    stale = {}
    for rel, fns in _PENDING_JS_FREE_CAPTURES.items():
        f = _ROOT / rel
        if not f.exists():
            continue
        found = dict(fc.free_captures_js(
            f.read_text(encoding="utf-8", errors="replace"), ambient=_AMBIENT))
        for fn, names in fns.items():
            fixed = sorted(set(names) - set(found.get(fn, ())))
            if fixed:
                stale.setdefault(rel, {})[fn] = fixed
    assert not stale, (
        "以下登记项已不再命中（bug 已修），请从 _PENDING_JS_FREE_CAPTURES 移除：\n"
        + "\n".join(f"  {k}: {v}" for k, v in stale.items())
    )


def test_scan_surface_has_teeth():
    """牙口下限：收集规则改坏（目录挪动/误排除）会让门禁扫空集变假绿——
    钉「在检文件数 ≥40 且被分析的顶层函数总数 ≥100」（2026-08-02 基线 59/222）。"""
    assert len(_ALL_JS) >= 40, f"在检 js 文件仅 {len(_ALL_JS)}，收集规则疑似改坏"
    total_fns = 0
    for t in _TEXTS.values():
        mb = fc._masked(t)
        total_fns += len(fc.top_level_functions(mb, fc._depths(mb)))
    assert total_fns >= 100, f"被分析顶层函数仅 {total_fns}，扫描疑似空转"


def test_collection_exclusions_hold():
    """排除面契约：vendor 压缩件 / 构建产物 / 镜像树绝不该混进在检清单。"""
    for f in _ALL_JS:
        rel = f.relative_to(_ROOT)
        assert not f.name.endswith(".min.js"), rel
        assert not (set(f.parts) & _EXCLUDE_PARTS), rel
        assert rel.parts[:3] != ("desktop", "renderer", "shared"), rel


def test_scanner_detects_planted_bug_in_js():
    """探测器有效性自证：合成 contact360 原型（Node 风味上下文）必被抓到；
    require/module 等环境全局不误伤。"""
    js = """
'use strict';
const path = require('path');
(function(){
  const SCOPED_TOKEN = 'x';
  function scopedHelper(s){ return s; }
})();
function brokenTop(){
  return path.join(String(SCOPED_TOKEN), scopedHelper('y'));
}
function fineTop(p){
  const local = require('fs');
  return local.existsSync(p) ? path.resolve(p) : null;
}
module.exports = { brokenTop, fineTop };
"""
    findings = dict(fc.free_captures_js(js, ambient=fc.NODE_ENV_GLOBALS))
    assert findings.get("brokenTop") == ["SCOPED_TOKEN", "scopedHelper"]
    assert "fineTop" not in findings
