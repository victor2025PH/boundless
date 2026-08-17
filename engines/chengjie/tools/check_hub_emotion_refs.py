# -*- coding: utf-8 -*-
"""hub 情绪参考音备货体检（只读，2026-08-03）——「情感通道」的照单录音清单。

契约探明（2026-08-03，读 avatarhub 源码 + 线上 /profiles 实测）：
  - ``POST /api/tts_only`` 已收 ``emotion`` 标签并透传 ``_conv_synthesize``；
  - hub 侧情感机制＝**情绪参考音**：``profile.emotion_refs = {emotion: [段]}``，
    命中情绪即改用该情绪的真人参考录音去克隆（音色/情感同源，双保真）；
    未命中→回退默认参考+采样微调（弱情感）——这正是当前线上状态；
  - 2026-08-03 晚：生产 ``profile_map`` 指向的 10 档已自举满仓（多为 ``*-智聊``
    专属档；基档如「美月」可能仍空——查库存务必按 map 值，别按人设英文 id）。
    客户端 ``hub_fish.emotion_threshold=0.35`` 已放行日常情绪标签。

本工具输出每档缺哪些情绪段。真人聊天风重录仍是质量天花板（自举是零录音
过渡方案）；规格见 docs/VOICE_REF_CHAT_STYLE.md。

用法：``python tools/check_hub_emotion_refs.py [--data-root PATH] [--all]``
（默认只看生产 profile_map 引用的档；``--all`` 看全库。hub 不可达 → SKIP exit 0，
不污染调用方信号。）
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
from typing import Any, Dict, List, Optional

# 客户端会下发的情绪标签全集（src/ai/voice_emotion._COSYVOICE_EMOTION 的值域，
# neutral=基础参考音本身不必录）。录音优先级：日常聊天以 gentle/happy/sad 最常命中。
CLIENT_EMOTIONS = ("gentle", "happy", "sad", "excited", "calm", "serious")
PRIORITY = ("gentle", "happy", "sad")


def _fetch_profiles(base_url: str, timeout: float = 6.0) -> List[Dict[str, Any]]:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/profiles",
                                timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = data.get("profiles") if isinstance(data, dict) else data
    if isinstance(raw, dict):          # 兼容 {name: {...}} 旧形态
        return [dict(v, name=k) for k, v in raw.items()]
    return [p for p in (raw or []) if isinstance(p, dict)]


def _fetch_emotion_keys(base_url: str, name: str,
                        timeout: float = 6.0) -> Optional[List[str]]:
    """单档详情的 emotion_refs 键（权威口径——2026-08-03 实测线上 /profiles
    **列表**不带 emotion_refs 字段，落库后列表仍显示空，必须查
    ``/profiles/{name}`` 详情的 ``emotion_refs_count``）。失败 → None。"""
    from urllib.parse import quote
    try:
        with urllib.request.urlopen(
                f"{base_url.rstrip('/')}/profiles/{quote(name)}",
                timeout=timeout) as resp:
            detail = json.loads(resp.read().decode("utf-8"))
        cnt = detail.get("emotion_refs_count")
        if isinstance(cnt, dict):
            return sorted(k for k, v in cnt.items() if v)
    except Exception:
        pass
    return None


def main(argv: List[str] | None = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="hub 情绪参考音备货体检（只读）")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--all", action="store_true", help="看全库而非仅生产引用档")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from scripts._data_root import load_merged_config, resolve_data_roots

    root = resolve_data_roots(args.data_root)[0]
    cfg = load_merged_config(root)
    hf = ((cfg.get("avatar_voice") or {}).get("hub_fish") or {})
    base_url = str(hf.get("base_url") or "http://192.168.0.176:9000")
    pmap = hf.get("profile_map") if isinstance(hf.get("profile_map"), dict) else {}
    wanted = set(str(v) for v in pmap.values())

    try:
        profiles = _fetch_profiles(base_url)
    except Exception as ex:
        print(f"SKIP: hub 不可达（{base_url}）：{ex}")
        return 0

    print(f"=== hub 情绪参考音备货体检（{base_url}，共 {len(profiles)} 档）===")
    print(f"客户端可下发标签：{'、'.join(CLIENT_EMOTIONS)}"
          f"（emotion_threshold={hf.get('emotion_threshold', '未配置')}）")
    print()
    rows = 0
    ready = 0
    for p in profiles:
        name = str(p.get("name") or "")
        if not args.all and wanted and name not in wanted:
            continue
        rows += 1
        refs = p.get("emotion_refs") if isinstance(p.get("emotion_refs"), dict) else {}
        have = sorted(k for k, v in refs.items() if v)
        if not have:                      # 列表口径为空 → 查详情端点（权威）
            detail = _fetch_emotion_keys(base_url, name)
            if detail is not None:
                have = detail
        missing = [e for e in CLIENT_EMOTIONS if e not in have]
        pri = [e for e in PRIORITY if e in missing]
        if not missing:
            ready += 1
            print(f"  {name:<14} 备齐（{'、'.join(have)}）")
        else:
            tail = f"（优先补：{'、'.join(pri)}）" if pri else ""
            print(f"  {name:<14} 缺 {len(missing)}/{len(CLIENT_EMOTIONS)}："
                  f"{'、'.join(missing)}{tail}")
    if not rows:
        print("  （profile_map 为空且未加 --all：无目标档；生产按人设 id 直连同名档）")
    print()
    print(f"结论：{ready}/{rows} 档情绪备货齐全。"
          "录制规格见 docs/VOICE_REF_CHAT_STYLE.md（每情绪 1-2 段、10-15s、聊天语体），"
          "可与 P0-5 聊天风换底同场录制。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
