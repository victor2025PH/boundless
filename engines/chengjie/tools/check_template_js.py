"""模板自洽快检：Jinja 语法 + 渲染后内联 <script> 用 node --check 过一遍语法。

用法：python tools/check_template_js.py src/web/templates/personas.html [...]
渲染用空上下文（i18n={}、所有变量 Undefined）——只为拿到可解析的 JS，不校验业务值。
模板 / 静态 JS 热更新即上生产，每次保存前跑一次比截图后再修便宜得多。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from jinja2 import ChainableUndefined, Environment, FileSystemLoader

_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)


def main(paths: list[str]) -> int:
    rc = 0
    for p in paths:
        fp = Path(p)
        env = Environment(
            loader=FileSystemLoader(str(fp.parent)),
            undefined=ChainableUndefined, autoescape=False,
        )
        env.globals.update({"url_for": lambda *a, **k: "#"})
        # Undefined 进 |tojson 会炸；把 Undefined 视作 None 序列化（只求可解析的 JS）
        import json as _json
        env.filters["tojson"] = lambda v, **k: _json.dumps(
            None if isinstance(v, ChainableUndefined) else v, ensure_ascii=False)
        env.policies["json.dumps_function"] = lambda v, **k: _json.dumps(
            None if isinstance(v, ChainableUndefined) else v, ensure_ascii=False)
        try:
            tpl = env.get_template(fp.name)
            html = tpl.render(i18n={}, request=None, user_role="", ui_lang="zh")
        except Exception as ex:  # noqa: BLE001
            print(f"[jinja] {fp.name}: {type(ex).__name__}: {ex}")
            rc = 1
            continue
        n = 0
        for m in _SCRIPT_RE.finditer(html):
            attrs = m.group("attrs") or ""
            if "src=" in attrs or ("type=" in attrs and "javascript" not in attrs and "module" not in attrs):
                continue
            body = m.group("body")
            if not body.strip():
                continue
            n += 1
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
                tf.write(body)
                tmp = tf.name
            r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
            if r.returncode != 0:
                rc = 1
                line = 0
                for ln in r.stderr.splitlines():
                    mm = re.search(r":(\d+)$", ln.strip())
                    if mm:
                        line = int(mm.group(1))
                        break
                snippet = body.splitlines()[max(0, line - 3):line + 2] if line else []
                print(f"[js] {fp.name} script#{n} FAILED:\n{r.stderr.strip()[:600]}")
                for s in snippet:
                    print("    | " + s[:160])
            Path(tmp).unlink(missing_ok=True)
        print(f"{fp.name}: jinja ok, {n} inline script(s) checked{' with errors' if rc else ''}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
