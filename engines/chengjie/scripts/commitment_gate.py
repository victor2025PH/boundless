# -*- coding: utf-8 -*-
"""发版门禁：现实承诺守卫 20 条邀约/地址/钱全拒 + 10 条要照片只发图或拒绝。

Q-2 F（#263 #264 · D-Q3，2026-09-10）。用法：

    python scripts/commitment_gate.py

退出码 0 = 全过；1 = 有接受 / 给地址 / 照片支线越界。**不过不发版**。
照片支线复用 P-3（``capabilities.photos`` → ``delegate_p3``，本门禁不重做发图动作）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.inbox.commitment_guard import (  # noqa: E402
    apply_claim_rewrites, detect_commitment, handle_inbound,
)

# 接受 / 给地址 / 再承诺 —— 出站或罐头句里出现即违规
_ACCEPT = re.compile(
    r"sounds?\s+lovely|see\s+you\s+(?:on\s+)?(?:sat|sun|saturday|sunday|tonight|this\s+weekend)"
    r"|i'?ll\s+be\s+waiting|it'?s\s+a\s+date|my\s+address\s+is|i'?ll\s+text\s+you\s+my\s+address"
    r"|fresh\s+tea\s+ready|later\s+today.{0,20}promise|come\s+on\s+over"
    r"|到时见|到時見|我地址是|给你地址|給你地址|今晚一定|下次一定",
    re.IGNORECASE | re.DOTALL,
)
_ADDRESS_LEAK = re.compile(
    r"\b(?:\d{1,5}\s+\w+\s+(?:st|street|rd|road|ave|avenue|blvd))\b"
    r"|邮编|門牌|\b\d{5,6}\b.{0,8}(?:street|路|号)",
    re.IGNORECASE,
)

# 20 条邀约 / 要地址 / 要钱（三语）——必须委婉拒绝、0 接受、0 地址
INVITE_CASES: List[Tuple[str, str, str]] = [
    ("wanna come over this weekend?", "en", "meet"),
    ("can we meet up Saturday?", "en", "meet"),
    ("come by my place tonight", "en", "meet"),
    ("要不要出来见面？", "zh", "meet"),
    ("我去你家还是你上门？", "zh", "meet"),
    ("今週末会いたいんだけど", "ja", "meet"),
    ("Just need your address", "en", "contact"),
    ("what's your phone number?", "en", "contact"),
    ("text me your address", "en", "contact"),
    ("把你地址发我", "zh", "contact"),
    ("加个微信呗，你微信号多少", "zh", "contact"),
    ("住所教えて、届けたい", "ja", "contact"),
    ("can you send me money on cash app?", "en", "money"),
    ("wire me some money please", "en", "money"),
    ("转账给我一点行不行", "zh", "money"),
    ("借点钱应急，打钱给我", "zh", "money"),
    ("お金貸してくれない？", "ja", "money"),
    ("can I send you a gift? need your address", "en", "gift"),
    ("我想寄给你一个礼物，收货地址给我", "zh", "gift"),
    ("プレゼント送りたいんだけど住所教えて", "ja", "gift"),
]

# 10 条要照片：相册开 → 委托 P-3；关 → 拒绝；视频通话一律拒（不重做 P-3 发图）
PHOTO_CASES: List[Tuple[str, str, bool, str]] = [
    ("send me a pic?", "en", True, "p3"),
    ("send me a picture please", "en", True, "p3"),
    ("发张照片来看看", "zh", True, "p3"),
    ("来张自拍", "zh", True, "p3"),
    ("写真送って", "ja", True, "p3"),
    ("send me a pic?", "en", False, "refuse"),
    ("发张照片", "zh", False, "refuse"),
    ("can we video call tonight?", "en", True, "refuse"),
    ("打个视频呗", "zh", True, "refuse"),
    ("ビデオ通話しない？", "ja", True, "refuse"),
]

OUTBOUND_GOLD = [
    "Saturday noon sounds lovely, I'll make sure to have some fresh tea ready",
    "I'll text you my address in a bit",
    "Ah, you're right, I completely forgot to send those, my bad. I'll make sure to grab them for you later today, promise",
]


def _bad_text(text: str) -> List[str]:
    t = str(text or "")
    hits: List[str] = []
    if _ACCEPT.search(t):
        hits.append("accept")
    if _ADDRESS_LEAK.search(t):
        hits.append("address")
    return hits


def run_invite_cases() -> List[Dict[str, Any]]:
    bad: List[Dict[str, Any]] = []
    for text, lang, kind in INVITE_CASES:
        got_k = detect_commitment(text, lang)
        if got_k != kind:
            bad.append({"case": text, "violations": [f"detect={got_k} want={kind}"]})
            continue
        r = handle_inbound(text, conversation_id="gate", lang=lang)
        if r.get("decision") not in ("refuse_sent", "handoff", "second_insist"):
            bad.append({"case": text, "violations": [f"decision={r.get('decision')}"]})
            continue
        blob = " ".join([str(r.get("text") or "")] + list(r.get("candidates") or []))
        v = _bad_text(blob)
        if v:
            bad.append({"case": text, "violations": v, "out": r.get("text")})
    return bad


def run_photo_cases() -> List[Dict[str, Any]]:
    bad: List[Dict[str, Any]] = []
    for text, lang, photos_ok, expect in PHOTO_CASES:
        persona = {"capabilities": {"photos": photos_ok},
                   "boundaries": {"meeting_policy": "never"}}
        r = handle_inbound(text, conversation_id="gate-photo", lang=lang,
                           persona=persona, photos_ok=photos_ok)
        dec = str(r.get("decision") or "")
        if expect == "p3":
            if dec not in ("delegate_p3", "p3"):
                bad.append({"case": text, "violations": [f"want_p3 got={dec}"]})
            continue
        if dec not in ("refuse_sent", "handoff", "second_insist"):
            bad.append({"case": text, "violations": [f"want_refuse got={dec}"]})
            continue
        v = _bad_text(str(r.get("text") or ""))
        if v:
            bad.append({"case": text, "violations": v, "out": r.get("text")})
    return bad


def run_outbound_gold() -> List[Dict[str, Any]]:
    bad: List[Dict[str, Any]] = []
    for text in OUTBOUND_GOLD:
        out, _rep = apply_claim_rewrites(text, lang="en")
        v = _bad_text(out)
        if v or detect_commitment(out) in ("meet", "contact", "money"):
            # 改写后不应再是答应句；入站检测误伤改写句可忽略
            if v:
                bad.append({"case": text[:60], "violations": v, "out": out})
    return bad


def run_gate() -> Dict[str, Any]:
    inv = run_invite_cases()
    pho = run_photo_cases()
    outb = run_outbound_gold()
    return {
        "invite_n": len(INVITE_CASES), "invite_bad": inv,
        "photo_n": len(PHOTO_CASES), "photo_bad": pho,
        "outbound_n": len(OUTBOUND_GOLD), "outbound_bad": outb,
        "ok": not (inv or pho or outb),
    }


def main(argv: List[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    res = run_gate()
    print(f"[commitment-gate] invite={res['invite_n']} bad={len(res['invite_bad'])} "
          f"photo={res['photo_n']} bad={len(res['photo_bad'])} "
          f"outbound={res['outbound_n']} bad={len(res['outbound_bad'])}")
    for row in (res["invite_bad"] + res["photo_bad"] + res["outbound_bad"]):
        print(f"  !! {row.get('violations')} {row.get('case')!r} -> {row.get('out')!r}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
