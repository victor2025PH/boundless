# -*- coding: utf-8 -*-
"""Temporarily point desktop/config.json token at the zhiliao instance token. Restore with --restore."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

desk = Path(__file__).resolve().parents[1] / "desktop" / "config.json"
bak = desk.with_suffix(".json.tokenbak")
inst = Path(r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml")

if "--restore" in sys.argv:
    if bak.exists():
        desk.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
        bak.unlink()
        print("restored")
    else:
        print("no bak")
    sys.exit(0)

token = str(((yaml.safe_load(inst.read_text(encoding="utf-8")) or {}).get("web_admin") or {}).get("auth_token") or "").strip()
if not token:
    print("NO_TOKEN")
    sys.exit(2)
cfg = json.loads(desk.read_text(encoding="utf-8"))
if not bak.exists():
    bak.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
cfg.setdefault("backend", {})["token"] = token
desk.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("patched")
