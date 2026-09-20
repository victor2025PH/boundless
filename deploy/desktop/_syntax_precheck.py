"""Release pre-check: AST-parse every dirty .py under engines/chengjie.

Protects the 12-min PyInstaller rebuild from baking a sibling line's
half-saved file. Read-only; exit 1 if any file fails to parse.
"""
import ast
import io
import subprocess
import sys

ROOT = r"d:\boundless"
ENGINE = "engines/chengjie"

out = subprocess.run(
    ["git", "status", "--porcelain", "--", ENGINE],
    cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
files = []
for line in out.stdout.splitlines():
    if len(line) < 4:
        continue
    path = line[3:].strip().strip('"')
    if path.endswith(".py") and not line.startswith(" D") and not line.startswith("D "):
        files.append(path)

bad = 0
for rel in files:
    p = ROOT + "\\" + rel.replace("/", "\\")
    try:
        src = io.open(p, encoding="utf-8", errors="replace").read()
        ast.parse(src)
    except FileNotFoundError:
        continue
    except SyntaxError as e:
        print("SYNTAX FAIL: %s -> %s" % (rel, e))
        bad += 1

print("dirty .py checked: %d, syntax-bad: %d" % (len(files), bad))
sys.exit(1 if bad else 0)
