# -*- coding: utf-8 -*-
"""验证翻唱成品的水印凭证真的嵌入了历史音频文件。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
import sqlite3
from pathlib import Path

import provenance

conn = sqlite3.connect("history.db")
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT id, audio_file, text FROM speak_history WHERE text LIKE '%翻唱%' ORDER BY ts DESC LIMIT 1"
).fetchone()
if not row:
    print("NO COVER HISTORY")
    sys.exit(1)
p = Path("history_audio") / row["audio_file"]
data = p.read_bytes()
print(f"id={row['id']} file={row['audio_file']} bytes={len(data)}")
r = provenance.verify_credentials(data)
print("has_watermark:", r.get("has_watermark"))
print("ai_generated:", r.get("ai_generated"))
print("signature_valid:", r.get("signature_valid"))
m = r.get("manifest") or {}
print("model:", (m.get("assertions") or [{}])[-1].get("data", {}).get("model") if m else None)
