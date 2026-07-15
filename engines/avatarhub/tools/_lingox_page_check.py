# -*- coding: utf-8 -*-
"""同传页(_PAGE)内嵌资源体检：不起服务、不跑重依赖，静态抽出 HTML 后
   1) 用 esprima 做 <script> 全量 ES 语法解析（括号/引号/箭头函数错漏一网打尽）
   2) 校验本次改版关键元素与钩子仍在位（id/class/事件绑定）
用法：python tools/_lingox_page_check.py
"""
import ast
import io
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC = r"c:\模仿音色\live_interpreter.py"


def extract_page(path: str) -> str:
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_PAGE" and isinstance(node.value, ast.Constant):
                    return node.value.value
    raise SystemExit("未找到 _PAGE 字符串")


def main() -> int:
    page = extract_page(SRC)
    ok = True

    # 1) JS 语法
    scripts = re.findall(r"<script>(.*?)</script>", page, flags=re.S)
    if not scripts:
        print("✗ 未找到 <script> 块"); return 1
    try:
        import esprima
        for i, js in enumerate(scripts):
            # esprima-python 停在 ES2017：把可选链 ?. 降级成 . 再解析（语法形状等价，仅用于检查；?.数字 是三元不动）
            js2 = re.sub(r"\?\.(?!\d)", ".", js)
            esprima.parseScript(js2)
        print(f"✓ JS 语法解析通过（{len(scripts)} 个 script 块, 共 {sum(len(s) for s in scripts)} 字符）")
    except Exception as e:
        ok = False
        print(f"✗ JS 语法错误: {e}")

    # 2) 关键元素/钩子在位
    must_have = [
        'class=langrow', 'class=langseg', 'id=lsrc', 'id=ldst', 'id=langswap',
        'id=langapply', 'id=langquick', 'id=langtag', 'id=profile', 'id=omode',
        "syncLangUI", "markApplied", "_renderQuick", "appliedSrc", "lx_ui3_tip",
        "id=egdemo", ".qc", "#langapply.show", "apulse", "lgspin",
        # 2026-07-16 去重复+图标化
        'id=livemode', 'body.embed', 'const EMBED', 'i-sound', 'i-scene', 'const IC',
        'loadLastSession', 'lastSessionLine',
    ]
    for key in must_have:
        if key not in page:
            ok = False
            print(f"✗ 缺少关键标记: {key}")
    if ok:
        print(f"✓ {len(must_have)} 个关键标记全部在位")

    # 3) 旧版残留（应已移除）
    legacy = ["切换语向并生效", "常用(我说中文→)",
              "🔊 声音", "🧰 工具", "🎛 场景方案", "📦 预载常用", "⚙ 高级设置", "🔄 生效",
              "🎧 耳返", "🔈 对方朗读", "🔬 测回声", "🔊 试音", "🔧 修复", "🎬 演示模式"]
    for key in legacy:
        if key in page:
            ok = False
            print(f"✗ 旧版残留未清理: {key}")
    if ok:
        print("✓ 无旧版残留")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
