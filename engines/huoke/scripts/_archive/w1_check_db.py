# -*- coding: utf-8 -*-
"""检查现有数据库表结构"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')
from src.host.database import get_conn
with get_conn() as c:
    tables = c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    print('现有表:')
    for t in tables:
        print(f'  {t[0]}')
        cols = c.execute(f"PRAGMA table_info({t[0]})").fetchall()
        for col in cols:
            print(f'    {col[1]} {col[2]}')
