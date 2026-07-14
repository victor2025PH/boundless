# -*- coding: utf-8 -*-
"""W0 工具: 重置 Facebook search 配额（测试专用，清除过去1小时的搜索记录）"""
import sys, sqlite3, time
from pathlib import Path
base = Path(__file__).parent.parent
sys.path.insert(0, str(base))

from src.host.device_registry import data_file

db_path = data_file("compliance.db")
print(f"compliance.db: {db_path}")

with sqlite3.connect(str(db_path)) as c:
    # 查询当前搜索计数
    one_hour_ago = time.time() - 3600
    count = c.execute(
        "SELECT COUNT(*) FROM action_log WHERE platform='facebook' AND action='search' AND ts > ?",
        (one_hour_ago,)
    ).fetchone()[0]
    print(f"Past 1h facebook/search count: {count}")
    
    # 清除过去 2 小时的 facebook/search 记录
    two_hours_ago = time.time() - 7200
    deleted = c.execute(
        "DELETE FROM action_log WHERE platform='facebook' AND action='search' AND ts > ?",
        (two_hours_ago,)
    ).rowcount
    c.commit()
    print(f"Deleted {deleted} records")
    
    # 验证
    count_after = c.execute(
        "SELECT COUNT(*) FROM action_log WHERE platform='facebook' AND action='search' AND ts > ?",
        (one_hour_ago,)
    ).fetchone()[0]
    print(f"After reset - Past 1h facebook/search count: {count_after}")

print("Quota reset done!")
