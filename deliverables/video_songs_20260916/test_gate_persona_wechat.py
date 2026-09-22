#!/usr/bin/env python3
"""gate_persona.check_wechat_connect（F2 微信接入闸）单测：合成 marker，不打网、不读成片。

  python test_gate_persona_wechat.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from gate_persona import check_wechat_connect  # noqa: E402

EP = {"draft_gate": {"any": ["168", "89", "顺丰"], "forbid": ["稍后", "等我确认"]}}
FAILS: list[str] = []


def _check(name: str, cond: bool) -> None:
    if not cond:
        FAILS.append(name)


def _good_clips(draft="有的，现货还在。两个的话 168，今天定明天顺丰发。"):
    return [
        ("b_connect", [{"op": "goto_page"}, {"op": "wx_env", "ok": True}, {"op": "narr"}]),
        ("c_tier", [{"op": "wx_tier", "saved": True, "tier": "semi", "prev": "auto_reply"}, {"op": "narr"}]),
        ("d_verify", [{"op": "wx_verify", "online": True}, {"op": "incoming", "landed": True, "via": "desktop_ingest"},
                      {"op": "wx_restore", "restored": True, "tier": "auto_reply"}]),
        ("e_inbox", [{"op": "list_filter"}, {"op": "incoming", "landed": True, "via": "desktop_ingest"}, {"op": "open_stage"},
                     {"op": "wait_draft", "ok": True, "text": draft}, {"op": "adopt_draft", "filled": True}, {"op": "narr"}]),
    ]


def main() -> int:
    # ① 正例：三步真做、改档位有还原、稿命中口径、零出站 → 0 fail
    f, w = check_wechat_connect("F2", EP, _good_clips())
    _check(f"正例应 0 fail，得 {f}", not f)
    _check(f"正例应 0 warn，得 {w}", not w)

    # ② 改了档位没还原 → FAIL
    clips = _good_clips()
    clips[2] = ("d_verify", [{"op": "wx_verify", "online": True}, {"op": "incoming", "landed": True}])
    f, _ = check_wechat_connect("F2", EP, clips)
    _check("改档位无还原应 FAIL", any("wx_restore" in x for x in f))

    # ③ 还原失败（restored=False）→ 同样 FAIL
    clips = _good_clips()
    clips[2] = ("d_verify", [{"op": "wx_verify", "online": True}, {"op": "wx_restore", "restored": False}])
    f, _ = check_wechat_connect("F2", EP, clips)
    _check("还原失败应 FAIL", any("wx_restore" in x for x in f))

    # ④ 任何段出现出站动作（type_send / wait_outbound）→ FAIL
    clips = _good_clips()
    clips[3] = ("e_inbox", clips[3][1] + [{"op": "type_send", "sent": True}])
    f, _ = check_wechat_connect("F2", EP, clips)
    _check("出站动作应 FAIL", any("零出站" in x for x in f))
    clips = _good_clips()
    clips[3] = ("e_inbox", clips[3][1] + [{"op": "wait_outbound", "ok": True}])
    f, _ = check_wechat_connect("F2", EP, clips)
    _check("wait_outbound 应 FAIL", any("零出站" in x for x in f))

    # ⑤ 稿没命中人设数字 → FAIL；稿含拖延话 → FAIL
    f, _ = check_wechat_connect("F2", EP, _good_clips(draft="有的有的，你要几个？"))
    _check("稿未命中口径应 FAIL", any("口径未命中" in x for x in f))
    f, _ = check_wechat_connect("F2", EP, _good_clips(draft="两个 168，稍后我确认一下库存。"))
    _check("稿含拖延话应 FAIL", any("拖延话" in x for x in f))

    # ⑥ 没有成功的 wait_draft → FAIL
    clips = _good_clips()
    clips[3] = ("e_inbox", [{"op": "incoming", "landed": True}, {"op": "wait_draft", "ok": False}])
    f, _ = check_wechat_connect("F2", EP, clips)
    _check("无草稿应 FAIL", any("wait_draft" in x for x in f))

    # ⑦ 没改档位（wx_tier 未 saved）就不要求还原
    clips = _good_clips()
    clips[1] = ("c_tier", [{"op": "wx_tier", "saved": False}])
    clips[2] = ("d_verify", [{"op": "wx_verify", "online": True}])
    f, _ = check_wechat_connect("F2", EP, clips)
    _check("未改档位不应因还原 FAIL", not any("wx_restore" in x for x in f))

    print("== test_gate_persona_wechat ==")
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
