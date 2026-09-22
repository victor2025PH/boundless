#!/usr/bin/env python3
"""gate_persona.check_auto_reply（F 系列全自动闸）单测：合成 marker，不打网、不读成片。

  python test_gate_persona_auto.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from gate_persona import check_auto_reply  # noqa: E402

EP = {"out_lang": "es", "auto_reply_gate": {"any": ["39.9", "7-10", "PayPal"], "forbid": ["稍后", "等我确认"]}}
FAILS: list[str] = []


def _wo(api, ok=True, clicked=False):
    return {"op": "wait_outbound", "ok": ok, "clicked_send": clicked, "api": api}


def _check(name: str, cond: bool) -> None:
    if not cond:
        FAILS.append(name)


def main() -> int:
    good_api = [{"text": "¡Hola! Sí, enviamos a Chile. Vestido azul talla M, US$39.9.", "original": "你好！发智利的，蓝裙 M 码有货，39.9 美元。", "sent_by": "ai"},
                {"text": "Llega en 7-10 días. ¿A qué ciudad?", "original": "7-10 天到，发哪个城市？", "sent_by": "ai"}]
    # ① 正例：主戏无人工发送、西语出站、口径命中、接管+静默 → 0 fail
    clips = [("e_live", [{"op": "incoming", "landed": True}, _wo(good_api), {"op": "narr"}]),
             ("g_takeover", [{"op": "incoming"}, {"op": "click_takeover", "ok": True}, {"op": "type_send", "sent": True},
                             {"op": "assert_quiet", "quiet": True}]),
             ("h_guard", [{"op": "use_stage", "stage": "TG_GROUP"}, {"op": "open_stage"}, {"op": "ai_diag", "shown": True}])]
    f, w = check_auto_reply("F1", EP, clips)
    _check(f"正例应 0 fail，得 {f}", not f)
    _check(f"正例应 0 warn，得 {w}", not w)

    # ② 主戏里点了 AI回复 → FAIL
    f, _ = check_auto_reply("F1", EP, [("e_live", [_wo(good_api), {"op": "ai_reply_send", "sent": True}])])
    _check("主戏人工发送应 FAIL", any("人工发送动作" in x for x in f))

    # ③ 实发正文是中文（未译成 es）→ FAIL
    zh_api = [{"text": "你好！发智利的，蓝裙 M 码有货，39.9 美元。", "original": "同上", "sent_by": "ai"}]
    f, _ = check_auto_reply("F1", EP, [("e_live", [_wo(zh_api)])])
    _check("中文实发应 FAIL", any("未译成 es" in x for x in f))

    # ④ 口径未命中任何人设数字 → FAIL；原稿含拖延话 → FAIL
    off_api = [{"text": "Hola, gracias por escribir. Te confirmo luego.", "original": "你好，稍后我确认一下再回你。", "sent_by": "ai"}]
    f, _ = check_auto_reply("F1", EP, [("e_live", [_wo(off_api)])])
    _check("口径未命中应 FAIL", any("口径未命中" in x for x in f))
    _check("拖延话应 FAIL", any("拖延话" in x for x in f))

    # ⑤ 群舞台段出现出站 → FAIL
    f, _ = check_auto_reply("F1", EP, [("e_live", [_wo(good_api)]),
                                       ("h_guard", [{"op": "use_stage", "stage": "TG_GROUP"}, {"op": "type_send", "sent": True}])])
    _check("群舞台出站应 FAIL", any("群舞台段出现出站" in x for x in f))

    # ⑥ sent_by=agent 只 WARN；有接管无静默对照只 WARN
    f, w = check_auto_reply("F1", EP, [("e_live", [_wo([{**good_api[0], "sent_by": "agent"}])]),
                                       ("g", [{"op": "click_takeover", "ok": True}])])
    _check("sent_by=agent 应 WARN 不 FAIL", not f and any("sent_by=agent" in x for x in w) and any("assert_quiet" in x for x in w))

    # ⑦ 非 auto 片（无 wait_outbound）：不核口径
    f, w = check_auto_reply("P1", EP, [("a", [{"op": "incoming"}, {"op": "ai_reply_send", "sent": True}])])
    _check("无 wait_outbound 的片不应有 fail", not f)

    print("== test_gate_persona_auto ==")
    for x in FAILS:
        print("FAIL", x)
    print("RESULT", "FAIL" if FAILS else "PASS", f"({len(FAILS)})" if FAILS else "")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
