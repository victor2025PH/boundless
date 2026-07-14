# -*- coding: utf-8 -*-
"""复现 doctor.check_bat_encoding：列出 crit 级启动器里的非 ASCII 行号与内容。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
import doctor

for p in sorted(Path(doctor.BASE).glob("*.bat")):
    raw = p.read_bytes()
    hits = []
    for i, ln in enumerate(raw.split(b"\n"), 1):
        if not any(b > 127 for b in ln):
            continue
        head = ln.lstrip().lower()
        if head.startswith((b"echo", b"@echo", b"title")):
            continue
        hits.append((i, ln.decode("utf-8", errors="replace")[:70]))
    if not hits:
        continue
    is_launcher = (p.name in doctor._LAUNCHER_CRITICAL_BATS
                   or p.name.startswith("_launch_") or "detached" in p.name.lower())
    print(f"== {p.name} ({'CRIT' if is_launcher else 'info'}) {len(hits)} 行 ==")
    for i, t in hits[:200]:
        print(f"  {i}: {t}")
