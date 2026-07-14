#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remote client for zero-shot voice clone API (Fish-Speech).
Copy this file to the other LAN machine; needs only: pip install requests

Usage:
  python voice_clone_client.py health --host 192.168.0.188
  python voice_clone_client.py clone --host 192.168.0.188 --ref ref.wav --text "你好，这是克隆测试。"
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import requests


def _url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"


def cmd_health(host: str, port: int) -> int:
    r = requests.get(_url(host, port, "/health"), timeout=10)
    print(r.status_code, r.text)
    return 0 if r.ok else 1


def cmd_status(host: str, port: int) -> int:
    r = requests.get(_url(host, port, "/v1/status"), timeout=10)
    print(r.status_code, json.dumps(r.json(), ensure_ascii=False, indent=2))
    return 0 if r.ok else 1


def cmd_clone(host: str, port: int, ref: Path, text: str, ref_text: str, out: Path) -> int:
    if not ref.exists():
        print(f"reference audio not found: {ref}", file=sys.stderr)
        return 1
    payload = {
        "text": text,
        "reference_audio_b64": base64.b64encode(ref.read_bytes()).decode(),
        "reference_text": ref_text,
        "language": "zh",
        "return_base64": True,
    }
    print(f"POST {_url(host, port, '/v1/tts/clone')} ...", flush=True)
    r = requests.post(_url(host, port, "/v1/tts/clone"), json=payload, timeout=300)
    if not r.ok:
        print(r.status_code, r.text, file=sys.stderr)
        return 1
    data = r.json()
    if not data.get("ok"):
        print("server returned ok=false:", data, file=sys.stderr)
        return 1
    wav = base64.b64decode(data["audio_base64"])
    out.write_bytes(wav)
    print(f"saved {out} ({len(wav)} bytes, sr={data.get('sample_rate')})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Voice clone API client")
    p.add_argument("--host", required=True, help="GPU server LAN IP, e.g. 192.168.0.188")
    p.add_argument("--port", type=int, default=7855)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    sub.add_parser("status")

    c = sub.add_parser("clone")
    c.add_argument("--ref", type=Path, required=True, help="reference wav/mp3 (10-30s speech)")
    c.add_argument("--text", required=True, help="text to synthesize")
    c.add_argument("--ref-text", default="", help="transcript of reference audio (recommended)")
    c.add_argument("-o", "--out", type=Path, default=Path("cloned_output.wav"))

    args = p.parse_args()
    if args.cmd == "health":
        return cmd_health(args.host, args.port)
    if args.cmd == "status":
        return cmd_status(args.host, args.port)
    if args.cmd == "clone":
        return cmd_clone(args.host, args.port, args.ref, args.text, args.ref_text, args.out)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
