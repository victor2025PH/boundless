# -*- coding: utf-8 -*-
"""桌面壳 + 副驾扩展语 overlay 生成器（zh_hant P3 → 多语 P4，2026-08-27）。

Web 面板的扩展语走 i18n_packs（后端合并视图），但桌面壳三套词典是 JS/主进程
常量，不吃后端词包。本工具从三处**单一事实源提取简体列 → 译成目标语 → 生成
按语言分文件的 overlay**，运行时经既有钩子后装：

  ① shared/copilot/i18n/cp-i18n.js  reg() 块      → cp-i18n-ext.<lang>.js
     （CopilotShared.regExt 后装 + LANG 升级；web 与桌面 iframe 共用，双树同步）
  ② desktop/renderer/shell-i18n.js  DICT           → shell-i18n-ext.<lang>.js
     （shellI18n.registerExt 后装；LANG 本就解析成扩展语码，装载即生效）
  ③ desktop/main.js  SHELL_STR                     → shell-str-ext.json（多语一份）
     （main.js 启动时读取合并；缺文件=维持 zh/en 双语，绝不崩壳）

**按语言分文件 + 宿主条件装载**（简中/英文用户零开销，扩展语只载自己那份）：
  - Jinja 页（unified_inbox/personas）：服务端 ``{% if ui_lang in (...) %}`` 条件标签
  - 静态页（app.html 内联小装载器；index.html CSP 禁内联 → i18n-ext-loader.js）

用法::

    python -m scripts.i18n_desktop_ext generate                     # zh_hant（转换）
    python -m scripts.i18n_desktop_ext generate --langs vi,th,id \
        --api-base http://192.168.0.176:11434 --model hy-mt2-7b-official:latest
                                                                    # 机翻语种（HY-MT）

zh_hant 走确定性转换（与 scripts/i18n_hant.py 同源：s2twp + 术语钉 + 守恒校验）；
vi/th/id 走 scripts/i18n_mt.py 的 ollama_mt 单条通道（掩码保占位符 + 全量校验，
拒绝键=保持回落底，不入文件）。提取器各按其源格式写死（cp=行级状态机与
test_cp_i18n_parity 同款；shell=node require 官方出口；SHELL_STR=块切片后 node
求值）——格式漂移宁可红也不假绿。
门禁：desktop/test/shell-i18n.test.js（ext 文件键子集/占位符/装载协议）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_CP_SRC = _ROOT / "shared" / "copilot" / "i18n" / "cp-i18n.js"
_CP_DIR = _ROOT / "shared" / "copilot" / "i18n"
_CP_DIR_DESKTOP = _ROOT / "desktop" / "renderer" / "shared" / "copilot" / "i18n"
_SHELL_SRC = _ROOT / "desktop" / "renderer" / "shell-i18n.js"
_SHELL_DIR = _ROOT / "desktop" / "renderer"
_MAIN_SRC = _ROOT / "desktop" / "main.js"
_STR_OUT = _ROOT / "desktop" / "shell-str-ext.json"

# 机翻语种（zh_hant 之外走 HY-MT；与 i18n_packs.EXTRA_LANGS 对齐）
_MT_LANGS = ("vi", "th", "id")
_PH_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# 与 test_cp_i18n_parity 同款行级词条（值内允许转义引号；行尾可挂 // 注释）
_ENTRY = re.compile(
    r'^\s*"((?:[^"\\]|\\.)+)"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,?\s*(?://.*)?$')


def _unescape(raw: str) -> str:
    return json.loads(f'"{raw}"')


def extract_cp_zh() -> dict:
    """cp-i18n.js 全部 reg({zh},{en}) 块的 zh 列（解码为真实字符串）。"""
    zh: dict = {}
    state = "out"
    for raw in _CP_SRC.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if state == "out":
            if s == "reg(":
                state = "pre_zh"
        elif state == "pre_zh":
            if s.startswith("{"):
                state = "zh"
        elif state == "zh":
            m = _ENTRY.match(raw)
            if m:
                zh[_unescape(m.group(1))] = _unescape(m.group(2))
            elif s.startswith("},"):
                state = "pre_en"
        elif state == "pre_en":
            if s.startswith("{"):
                state = "en"
        elif state == "en":
            if s.startswith("}"):
                state = "tail"
        elif state == "tail":
            if s.startswith(")"):
                state = "out"
    if len(zh) < 500:
        raise SystemExit(f"cp-i18n.js zh 列提取仅 {len(zh)} 键——格式漂移？拒绝生成")
    return zh


def _node_json(expr_js: str) -> dict:
    """在 node 里求值 JS 片段并回传 JSON（shell 词典/SHELL_STR 的官方出口）。"""
    p = subprocess.run(["node", "-e", expr_js], capture_output=True,
                       cwd=str(_ROOT / "desktop"), timeout=60)
    if p.returncode != 0:
        raise SystemExit(f"node 提取失败: {p.stderr.decode('utf-8', 'replace')[:400]}")
    return json.loads(p.stdout.decode("utf-8"))


def extract_shell_zh() -> dict:
    return _node_json(
        "const s=require('./renderer/shell-i18n.js');"
        "process.stdout.write(JSON.stringify(s._dict.zh))")


def extract_shell_str_zh() -> dict:
    """main.js 的 SHELL_STR 块（含注释的 JS 对象字面量）→ node 求值取 zh。"""
    text = _MAIN_SRC.read_text(encoding="utf-8")
    m = re.search(r"const SHELL_STR = \{", text)
    if not m:
        raise SystemExit("main.js 找不到 SHELL_STR 定义——格式漂移？")
    i = m.end() - 1
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                block = text[i:j + 1]
                break
    else:
        raise SystemExit("SHELL_STR 花括号不闭合")
    tmp = _ROOT / "tmp" / "_shell_str_dump.js"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("const SHELL_STR = " + block +
                   ";\nprocess.stdout.write(JSON.stringify(SHELL_STR.zh));\n",
                   encoding="utf-8")
    try:
        return _node_json(f"require({json.dumps(str(tmp))})")
    finally:
        tmp.unlink(missing_ok=True)


def convert_map(zh: dict) -> tuple:
    """{key: 简体} → ({key: 繁体}, 拒绝清单)。占位符守恒校验与 i18n_hant 同源。"""
    from scripts.i18n_hant import _converter, convert_value
    cc = _converter()
    out: dict = {}
    rejected: list = []
    for k, v in zh.items():
        tr = convert_value(cc, str(v))
        if set(_PH_RE.findall(tr)) != set(_PH_RE.findall(str(v))) or (
                str(v).strip() and not tr.strip()):
            rejected.append(k)
            continue
        out[k] = tr
    return out, rejected


def mt_map(zh: dict, lang: str, api_base: str, model: str,
           sleep_s: float = 0.05) -> tuple:
    """{key: 简体} → ({key: 目标语}, 拒绝清单)。HY-MT 单条 + i18n_mt 全量校验。"""
    from scripts.i18n_mt import translate_one_ollama_mt, validate_entry
    out: dict = {}
    rejected: list = []
    keys = list(zh)
    t0 = time.time()
    for n, k in enumerate(keys, 1):
        src = str(zh[k])
        try:
            tr = translate_one_ollama_mt(api_base, model, lang, src)
        except Exception:  # noqa: BLE001 —— 单条失败留给重跑
            rejected.append(k)
            continue
        if validate_entry(k, src, tr):
            rejected.append(k)
            continue
        out[k] = tr
        if n % 100 == 0 or n == len(keys):
            print(f"[desktop_ext] {lang} {n}/{len(keys)} 通过 {len(out)}"
                  f" 拒绝 {len(rejected)} ({n / max(time.time() - t0, 0.001):.1f}/s)")
        if sleep_s:
            time.sleep(sleep_s)
    return out, rejected


_JS_HEADER = """/* AUTO-GENERATED by scripts/i18n_desktop_ext.py — {what} {lang} overlay。
   勿手改：regen 会覆盖本文件。生成 {created} · {how} · {n} 键。
   装载协议：作为同步 <script> 紧跟 {host} 之后（宿主按语言条件装载），经 {hook}
   后装；文件缺失/钩子不在＝静默维持既有回落，绝不影响其他语言坐席。 */
(function (root) {{
  'use strict';
  var d = {{
"""


def _emit_js(path: Path, what: str, lang: str, how: str, host: str, hook: str,
             call: str, data: dict, tail: str = "") -> None:
    lines = [_JS_HEADER.format(what=what, lang=lang, how=how, n=len(data),
                               created=time.strftime("%Y-%m-%d %H:%M"),
                               host=host, hook=hook)]
    for k in sorted(data):
        lines.append(f"    {json.dumps(k, ensure_ascii=False)}: "
                     f"{json.dumps(data[k], ensure_ascii=False)},\n")
    lines.append("  };\n")
    lines.append(f"  {call}\n")
    if tail:
        lines.append(f"  {tail}\n")
    lines.append("  if (typeof module !== 'undefined' && module.exports) "
                 f"{{ module.exports = {{ lang: {json.dumps(lang)}, dict: d }}; }}\n")
    lines.append("})(typeof window !== 'undefined' ? window : this);\n")
    path.write_text("".join(lines), encoding="utf-8")


def _emit_lang(lang: str, cp: dict, shell: dict, sstr: dict) -> None:
    """三个 overlay 落盘（cp 双树同步；shell-str 合并进多语 JSON）。"""
    how = "OpenCC s2twp+术语钉" if lang == "zh_hant" else "HY-MT 机翻(占位符掩码)"
    cp_out = _CP_DIR / f"cp-i18n-ext.{lang}.js"
    _emit_js(cp_out, "副驾组件", lang, how, "cp-i18n.js", "CopilotShared.regExt",
             "if (root.CopilotShared && typeof root.CopilotShared.regExt === 'function')"
             f" {{ root.CopilotShared.regExt({json.dumps(lang)}, d); }}", cp)
    _CP_DIR_DESKTOP.mkdir(parents=True, exist_ok=True)
    (_CP_DIR_DESKTOP / cp_out.name).write_bytes(cp_out.read_bytes())

    _emit_js(_SHELL_DIR / f"shell-i18n-ext.{lang}.js", "桌面壳渲染层", lang, how,
             "shell-i18n.js", "shellI18n.registerExt",
             "if (root.shellI18n && typeof root.shellI18n.registerExt === 'function')"
             f" {{ root.shellI18n.registerExt({json.dumps(lang)}, d); }}", shell,
             tail=(f"try {{ if (root.shellI18n && root.shellI18n.lang === {json.dumps(lang)}"
                   " && typeof document !== 'undefined')"
                   " { document.title = root.shellI18n.t('app.title'); } }"
                   " catch (e) { /* title 刷新失败不阻断 */ }"))

    merged = {}
    if _STR_OUT.is_file():
        try:
            merged = json.loads(_STR_OUT.read_text(encoding="utf-8")) or {}
        except Exception:
            merged = {}
    merged[lang] = sstr
    _STR_OUT.write_text(json.dumps(merged, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def run_generate(langs: tuple = ("zh_hant",), *, api_base: str = "",
                 model: str = "", dry_run: bool = False) -> dict:
    cp_zh = extract_cp_zh()
    shell_zh = extract_shell_zh()
    str_zh = extract_shell_str_zh()
    report: dict = {}
    for lang in langs:
        if lang == "zh_hant":
            cp, r1 = convert_map(cp_zh)
            shell, r2 = convert_map(shell_zh)
            sstr, r3 = convert_map(str_zh)
        elif lang in _MT_LANGS:
            if not api_base or not model:
                raise SystemExit(f"{lang} 是机翻语种：--api-base/--model 必填")
            cp, r1 = mt_map(cp_zh, lang, api_base, model)
            shell, r2 = mt_map(shell_zh, lang, api_base, model)
            sstr, r3 = mt_map(str_zh, lang, api_base, model)
        else:
            raise SystemExit(f"未知语种: {lang}")
        stats = {"cp": len(cp), "shell": len(shell), "shell_str": len(sstr),
                 "rejected": len(r1) + len(r2) + len(r3)}
        report[lang] = stats
        if dry_run:
            print(f"[desktop_ext] dry-run {lang}: {json.dumps(stats, ensure_ascii=False)}")
            continue
        _emit_lang(lang, cp, shell, sstr)
        print(f"[desktop_ext] {lang} 已生成: {json.dumps(stats, ensure_ascii=False)}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--langs", default="zh_hant", help="逗号分隔：zh_hant,vi,th,id")
    g.add_argument("--api-base", default="", help="机翻语种的 ollama 端点")
    g.add_argument("--model", default="", help="机翻语种的模型名")
    g.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "generate":
        run_generate(tuple(x.strip() for x in a.langs.split(",") if x.strip()),
                     api_base=a.api_base, model=a.model, dry_run=a.dry_run)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
