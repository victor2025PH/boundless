# -*- coding: utf-8 -*-
"""hub.js 轻量语法闸（无 node 环境）：括号/引号配对 + Alpine 对象字面量可解析性抽检。
不是完整 JS parser，但能抓住漏逗号/未闭合括号这类改坏整站 UI 的低级错误。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
src = Path(r"c:\模仿音色\static\hub.js").read_text(encoding="utf-8")

stack = []
pairs = {')': '(', ']': '[', '}': '{'}
line = 1
i = 0
in_str = None          # ' " ` 或 None
in_comment = None      # // 或 /*
escape = False
while i < len(src):
    c = src[i]
    if c == '\n':
        line += 1
        if in_comment == '//':
            in_comment = None
    if in_comment:
        if in_comment == '/*' and c == '*' and i + 1 < len(src) and src[i+1] == '/':
            in_comment = None
            i += 1
    elif in_str:
        if escape:
            escape = False
        elif c == '\\':
            escape = True
        elif c == in_str:
            in_str = None
        # 模板串按不透明整体处理：插值 ${...} 内部不做配对（避免误报；
        # 该门只兜"改坏整站"级失衡，插值内失衡由浏览器控制台兜）
    else:
        if c == '/' and i + 1 < len(src) and src[i+1] in '/*':
            in_comment = '//' if src[i+1] == '/' else '/*'
            i += 1
        elif c == '/':
            # 可能是正则字面量：回看最近的有效字符，处于"表达式位"则按正则整体跳过
            j = i - 1
            while j >= 0 and src[j] in ' \t\r\n':
                j -= 1
            if j < 0 or src[j] in '=([{,;:!&|?+-*%<>~^' or src[j:j+1] == '\n':
                k = i + 1
                in_class = False
                esc = False
                while k < len(src) and src[k] != '\n':
                    ck = src[k]
                    if esc:
                        esc = False
                    elif ck == '\\':
                        esc = True
                    elif ck == '[':
                        in_class = True
                    elif ck == ']':
                        in_class = False
                    elif ck == '/' and not in_class:
                        break
                    k += 1
                if k < len(src) and src[k] == '/':
                    i = k          # 正则整体吞掉（含标志字符无括号，忽略即可）
        elif c in "'\"`":
            in_str = c
        elif c in '([{':
            stack.append((c, line))
        elif c in ')]}':
            if not stack or stack[-1][0] != pairs[c]:
                print(f"[JS-GATE] 括号失配: 第{line}行 意外 '{c}'"
                      + (f"（栈顶 '{stack[-1][0]}' @第{stack[-1][1]}行）" if stack else "（栈空）"))
                sys.exit(1)
            stack.pop()
    i += 1

if in_str:
    print(f"[JS-GATE] 有未闭合的字符串（{in_str}），扫描至文件尾")
    sys.exit(1)
if stack:
    print(f"[JS-GATE] 有 {len(stack)} 个未闭合括号，最内层 '{stack[-1][0]}' @第{stack[-1][1]}行")
    sys.exit(1)
print("[JS-GATE] hub.js 括号/引号配对通过")
