"""模板内联 JS 语法门禁（全站 `<script>` 块）。

这个仓库把大量业务 JS 内联在 Jinja 模板里——`unified_inbox.html` 单个 `<script>` 块就有
46 万字符。一个语法错就让**整页 JS 全死**：不是某个按钮不灵，是收件箱直接白屏。

而现有前端门禁全是「语义」层的（内联 handler 有没有挂 window、id 唯不唯一、有没有孤儿
DOM 引用），**没有一条检查这些 JS 能不能被解析**。Python 侧的模板渲染测试也发现不了：
Jinja 渲染成功不代表浏览器能解析。

做法：抽出每个非外链 `<script>` 块 → 把 Jinja 标记占位掉（`{%…%}`→空、`{{…}}`→`0`，
两者都不参与 JS 语法结构）→ 交给 `node --check`。全站 93 个块实测零假阳性。

node 不可用时 skip（本仓 desktop/ 有 node 单测，开发机与 CI 都应装；缺了也不该让
整个 Python 测试套挂掉）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_SCRIPT_RE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", re.S)

# 一次 node 启动检完全部块（93 次 `node --check` 要 12s，单进程 vm.Script 只要 ~1s）
_RUNNER_JS = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const dir = process.argv[2];
const out = [];
for (const name of fs.readdirSync(dir).filter(f => f.endsWith('.js')).sort()) {
  const src = fs.readFileSync(path.join(dir, name), 'utf8');
  try { new vm.Script(src, { filename: name }); }
  catch (e) { out.push({ file: name, error: String(e && e.message || e) }); }
}
process.stdout.write(JSON.stringify(out));
"""


def _jinja_to_placeholder(js: str) -> str:
    """把 Jinja 标记换成语法中性的占位，保留 JS 结构。

    ``{% … %}``（控制流）整体删除；``{{ … }}``（表达式）换成字面量 ``0`` —— 它在
    表达式位、字符串内、对象值位都是合法 JS。
    """
    js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
    return re.sub(r"\{\{.*?\}\}", "0", js, flags=re.S)


def _collect_blocks(tmp: Path) -> dict:
    """抽出全部内联块写进 tmp，返回 {落盘文件名: 模板相对路径#块序号}。"""
    index = {}
    for tpl in sorted(_TPL_DIR.rglob("*.html")):
        rel = tpl.relative_to(_TPL_DIR).as_posix()
        for i, block in enumerate(_SCRIPT_RE.findall(tpl.read_text(encoding="utf-8"))):
            if not block.strip():
                continue
            name = f"{rel.replace('/', '__').removesuffix('.html')}__{i}.js"
            (tmp / name).write_text(_jinja_to_placeholder(block), encoding="utf-8")
            index[name] = f"{rel} 的第 {i} 个 <script>"
    return index


@pytest.mark.skipif(shutil.which("node") is None, reason="未安装 node，跳过 JS 语法门禁")
def test_all_inline_scripts_parse():
    with tempfile.TemporaryDirectory(prefix="tpl-js-") as td:
        tmp = Path(td)
        index = _collect_blocks(tmp)
        assert len(index) >= 50, (
            f"只抽到 {len(index)} 个内联脚本块，远少于预期——抽取正则可能失效，"
            "门禁已空转（绿着但什么都没检）。"
        )
        runner = tmp / "_runner.cjs"
        runner.write_text(_RUNNER_JS, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(runner), str(tmp)],
            capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, f"语法检查器自身失败: {proc.stderr[:500]}"
        failures = json.loads(proc.stdout or "[]")

    assert not failures, "模板内联 JS 语法错误（整页 JS 会全死）：\n" + "\n".join(
        f"  {index.get(f['file'], f['file'])}\n    {f['error']}" for f in failures
    )
