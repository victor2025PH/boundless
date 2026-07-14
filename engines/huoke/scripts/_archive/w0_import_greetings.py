# -*- coding: utf-8 -*-
"""将 w0_greeting_library.json 导入 openclaw.db 的 fb_greeting_library 表。"""
import sys, json
from pathlib import Path

base = Path(__file__).parent.parent
sys.path.insert(0, str(base))

from src.host.fb_targets_store import ensure_schema, import_greeting_library

ensure_schema()
print("Schema ensured.")

data_path = base / "data" / "w0_greeting_library.json"
data = json.loads(data_path.read_text(encoding="utf-8"))
greetings = data.get("greetings", [])
print(f"Loading {len(greetings)} greetings...")

count = import_greeting_library(greetings, persona_key="jp_female_midlife")
print(f"Imported: {count} rows into fb_greeting_library")
