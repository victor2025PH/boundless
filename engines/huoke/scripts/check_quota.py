# -*- coding: utf-8 -*-
"""检查 Facebook 搜索配额重置时间"""
import sys, io, sqlite3, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')

from src.host.device_registry import data_dir

db_path = str(data_dir() / 'compliance.db')
conn = sqlite3.connect(db_path)
now = time.time()
hour_ago = now - 3600

row = conn.execute(
    'SELECT MIN(ts), MAX(ts), COUNT(*) FROM action_log WHERE platform=? AND action=? AND ts > ?',
    ('facebook', 'search', hour_ago)
).fetchone()

if row and row[2] > 0:
    oldest = row[0]
    reset_at = oldest + 3600
    wait_s = max(0, reset_at - now)
    print(f"当前窗口内搜索次数: {row[2]}/15")
    print(f"最早记录: {time.strftime('%H:%M:%S', time.localtime(oldest))}")
    print(f"配额重置: {time.strftime('%H:%M:%S', time.localtime(reset_at))}")
    print(f"还需等待: {int(wait_s//60)} 分 {int(wait_s%60)} 秒")
else:
    print("配额已重置，现在可以运行！")

# 今日总配额
today_start = time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d'))
day_row = conn.execute(
    'SELECT COUNT(*) FROM action_log WHERE platform=? AND action=? AND ts > ?',
    ('facebook', 'search', today_start)
).fetchone()
print(f"今日已搜索: {day_row[0]}/80")

conn.close()
