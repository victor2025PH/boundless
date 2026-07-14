"""P2-⑫ 防 GBK 解码 bug 回归 — 静态扫 tests/ 下所有 subprocess.run/Popen.

事故根因 (PR #154 / test_check_a_activity):
    Windows subprocess.run 默认 encoding = locale.getpreferredencoding() = cp936/GBK.
    git log / 子进程 print 输出含 emoji 时, _readerthread decode 抛
    UnicodeDecodeError → stdout=None → AttributeError 'NoneType' has no
    'splitlines'.

防护策略 (本测试断言):
    所有带 capture_output=True (或 stdout=PIPE) + text=True 的 subprocess.run
    必须显式指定 encoding="utf-8". errors 不强制 (但建议 "replace").

允许的"白名单"案例:
    - mock / monkeypatch / patch("subprocess.run") (不真调)
    - text=False (返 bytes 不解码)
    - capture_output 缺失 (不读 stdout)
    - subprocess.run() 不带任何参数 (查 returncode 用)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
# tests/e2e 跟单测语义不同 (fixture 起进程), 暂排除. 如要扩展可去掉.
EXCLUDE_DIRS = {"e2e"}
# 测 src/utils/subprocess_text 自身的测试用 monkeypatch, 不算违规.
EXCLUDE_FILES = {"test_subprocess_text.py", "test_no_unsafe_subprocess.py"}


def _scan_subprocess_calls(text: str) -> list[tuple[int, str]]:
    """返回 [(line_no, snippet), ...] 含 subprocess.run/Popen 但未带 encoding 的位置.

    简单启发式:
    - 找 'subprocess.run(' 或 'subprocess.Popen(' 的开头
    - 抓后续 ~10 行直到匹配的 ')'
    - 在抓到的整体内若同时含 'capture_output=True' 或 'stdout=' 含 PIPE / 'text=True'
      但**不**含 'encoding=' → 标记违规
    - 含 'patch(' 或 'monkeypatch.setattr' 或 'with patch' 同行/上一行 → 跳过 (mock)
    """
    violations = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.search(r"\bsubprocess\.(run|Popen|check_output|call|check_call)\(", line)
        if not m:
            i += 1
            continue
        # 跳过 mock / patch / monkeypatch 行
        if "patch(" in line or "monkeypatch" in line or "with patch" in line:
            i += 1
            continue
        # 累积后续 ~12 行直到看到独立的 ')' (粗略平衡)
        snippet = line
        depth = line.count("(") - line.count(")")
        end = i
        for j in range(i + 1, min(i + 12, len(lines))):
            snippet += "\n" + lines[j]
            depth += lines[j].count("(") - lines[j].count(")")
            end = j
            if depth <= 0:
                break
        # 启发判断
        captures_output = (
            "capture_output=True" in snippet
            or re.search(r"stdout\s*=\s*subprocess\.PIPE", snippet)
            or re.search(r"stdout\s*=\s*PIPE", snippet)
        )
        is_text = "text=True" in snippet or "universal_newlines=True" in snippet
        has_encoding = "encoding=" in snippet
        # text 模式 + capture stdout 但缺 encoding = 违规
        if captures_output and is_text and not has_encoding:
            violations.append((i + 1, snippet[:200]))
        i = end + 1
    return violations


def _iter_test_files():
    """遍历 tests/ 下所有 test_*.py (排除 e2e + 自身)."""
    for p in TESTS_DIR.rglob("test_*.py"):
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.name in EXCLUDE_FILES:
            continue
        yield p


def test_no_subprocess_call_missing_encoding():
    """静态扫描所有 test_*.py: subprocess.run/Popen 带 capture_output+text 必须显式 encoding=.

    如失败, 输出违规清单. 修法见 tests/test_check_a_activity.py PR #154 commit:
    给每个调用加 encoding="utf-8", errors="replace".
    spawn 子 Python 进程额外加 env={**os.environ, "PYTHONIOENCODING": "utf-8"}.
    """
    all_violations: dict[str, list[tuple[int, str]]] = {}
    for p in _iter_test_files():
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        viols = _scan_subprocess_calls(text)
        if viols:
            rel = p.relative_to(TESTS_DIR.parent)
            all_violations[str(rel)] = viols

    if not all_violations:
        return  # PASS

    # 失败 — 打印清单
    msg_lines = [
        f"\n{len(all_violations)} test file(s) with unsafe subprocess.run "
        "(capture_output+text but no encoding= specified):\n"
    ]
    total = 0
    for fname, viols in sorted(all_violations.items()):
        msg_lines.append(f"  {fname}")
        for ln, snip in viols:
            total += 1
            first_line = snip.split("\n")[0].strip()
            msg_lines.append(f"    line {ln}: {first_line}")
    msg_lines.append(
        f"\n  Total: {total} call(s). Fix: add encoding=\"utf-8\", "
        f"errors=\"replace\" to each. For spawn-python calls, also add "
        f"env={{**os.environ, \"PYTHONIOENCODING\": \"utf-8\"}}.\n"
        f"  Reference: tests/test_check_a_activity.py (PR #154 fix)."
    )
    pytest.fail("\n".join(msg_lines))


def test_scanner_self_check():
    """元测试: 验证 _scan_subprocess_calls 启发式工作正常 (防扫描器自己漏洞)."""
    bad = '''import subprocess
def test_x():
    r = subprocess.run(
        ["git", "log"],
        capture_output=True, text=True, timeout=10,
    )
'''
    good_with_encoding = '''import subprocess
def test_x():
    r = subprocess.run(
        ["git", "log"],
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
'''
    good_no_capture = '''import subprocess
def test_x():
    subprocess.run(["git", "log"], check=True)
'''
    good_mock = '''from unittest.mock import patch
def test_x():
    with patch("subprocess.run") as m:
        m.return_value = "fake"
'''
    assert len(_scan_subprocess_calls(bad)) == 1, "bad must be flagged"
    assert len(_scan_subprocess_calls(good_with_encoding)) == 0, "encoding= must pass"
    assert len(_scan_subprocess_calls(good_no_capture)) == 0, "no capture must pass"
    assert len(_scan_subprocess_calls(good_mock)) == 0, "patch/mock must pass"
