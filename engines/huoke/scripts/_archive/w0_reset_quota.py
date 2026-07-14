# -*- coding: utf-8 -*-
"""W0 工具: 重置 Facebook search 配额（仅用于测试/调试）"""
import sys
from pathlib import Path
base = Path(__file__).parent.parent
sys.path.insert(0, str(base))

from src.host.database import get_conn

with get_conn() as c:
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print("Tables:", tables)
    for t in tables:
        if any(x in t.lower() for x in ['compliance', 'action', 'limit', 'quota', 'rate', 'audit', 'counter', 'window']):
            print("Possible quota table:", t)

# Also check compliance_guard directly
from src.behavior.compliance_guard import ComplianceGuard
g = ComplianceGuard()
print("Guard type:", type(g))
print("Guard attrs:", [a for a in dir(g) if not a.startswith('__')])
