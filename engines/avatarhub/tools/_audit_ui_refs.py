# -*- coding: utf-8 -*-
"""复现 doctor 前端完整性审计：列出模板里引用但 <script> 未定义的函数。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
import doctor

for name in ("ui.html", "phone.html"):
    p = Path(doctor.BASE) / "static" / name
    if not p.is_file():
        print(name, "missing")
        continue
    broken, orphan = doctor._audit_html(p)
    print(f"== {name} ==")
    print("broken:", broken)
    print("orphan:", orphan)
