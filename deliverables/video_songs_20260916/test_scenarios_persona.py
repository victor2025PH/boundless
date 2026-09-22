#!/usr/bin/env python3
"""P 系列场景闸：人群模型完整 ↔ 舞台可用 ↔ 首批集的录屏步骤含必拍动作 ↔ 副歌词合规。

  python test_scenarios_persona.py            # 静态（不打网）
  python test_scenarios_persona.py --live     # 加舞台探针（两侧账号在线）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lyric_gate import gate_file  # noqa: E402

FAILS: list[str] = []
REQ_FIELDS = ("id", "persona", "job", "company", "platform", "stage", "lang_pair", "trigger", "title", "hook",
              "features", "beats", "music", "chorus", "utm")
# 功能 → 录屏步骤里必须出现的 op（真实动作，不是「指一指」）
FEATURE_OPS = {
    "translate": {"xlate_quick_on"},
    "ai_reply": {"ai_reply_send"},
    "voice_clone": {"voice_preview"},
    "urgent_filter": {"urgent_filter"},
    "kb": {"kb_answer"},
    "inbox": {"list_filter", "use_stage"},
    "group": {"conv_scope"},
    "auto_reply": {"wait_outbound"},   # F 系列：引擎自己发出去，录屏里没有人点发送
    "wechat_connect": {"wx_tier"},     # F2：引导页真点「保存」改档位（不是指一指）
}
# F 系列教学片（format=tutorial）里必须有的对照动作：接管 + 让位后静默
TUTORIAL_AUTO_OPS = {"click_takeover", "assert_quiet", "mode_switch"}
# F2 微信接入片：三步都得真做 + 改了档位必须还原 + 拟稿人审的稿要在镜头里出现
WECHAT_CONNECT_OPS = {"wx_env", "wx_tier", "wx_verify", "wx_restore", "wait_draft"}
SEND_OPS = {"ai_reply_send", "type_send", "voice_preview", "wait_outbound"}
BANNED_OPS = {"open_nth_chat"}   # 点列表第 n 行 = 随机会话（v1 事故）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    sc = json.loads((ROOT / "scenarios_persona.json").read_text(encoding="utf-8"))
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    eps = {e["id"]: e for e in cur["episodes"]}
    banned = sc["banned_public_phrases"]
    stages = sc["stages"]
    palette = sc["music_palette"]

    if "养成" not in banned:
        FAILS.append("banned_public_phrases 必须含「养成」")
    jobs = set()
    for item in sc["episodes"]:
        eid = item.get("id", "?")
        for f in REQ_FIELDS:
            if not item.get(f):
                FAILS.append(f"{eid}: 缺字段 {f}")
        if item.get("stage") not in stages:
            FAILS.append(f"{eid}: stage {item.get('stage')} 未定义")
        if item.get("music") not in palette:
            FAILS.append(f"{eid}: music {item.get('music')} 不在 palette")
        if item.get("format") == "tutorial":
            # 教学片刻意沿用某集人物（F1=阿杰），不占「每集一种人」名额；但须声明 duration_range，
            # 且 auto_reply 片的口径闸要有 any（数字来自人设）
            if not item.get("duration_range"):
                FAILS.append(f"{eid}: 教学片须声明 duration_range（gate 不按 30~130 卡）")
            if "auto_reply" in item.get("features", []) and not (item.get("auto_reply_gate") or {}).get("any"):
                FAILS.append(f"{eid}: auto_reply 片须有 auto_reply_gate.any（人设数字口径）")
            if "wechat_connect" in item.get("features", []):
                if not (item.get("draft_gate") or {}).get("any"):
                    FAILS.append(f"{eid}: wechat_connect 片须有 draft_gate.any（拟稿人审的稿核人设数字）")
                st = stages.get(item.get("stage")) or {}
                # 微信没有第二个可编程账号：客户侧必须声明为入站桥注入，且会话 ephemeral（录完整条硬删）
                if st.get("customer_mode") != "desktop_ingest":
                    FAILS.append(f"{eid}: 微信舞台 customer_mode 须为 desktop_ingest（客户侧走副驾入站桥）")
                if not st.get("ephemeral"):
                    FAILS.append(f"{eid}: 微信演示舞台须 ephemeral=true（老板收件箱不留假客户）")
        else:
            if item.get("job") in jobs:
                FAILS.append(f"{eid}: 岗位「{item.get('job')}」与其他集重复——每集一种人")
            jobs.add(item.get("job"))
        blob = json.dumps(item, ensure_ascii=False)
        for w in banned:
            if w in blob:
                FAILS.append(f"{eid}: 含公域禁词「{w}」")
        if "voice_clone" in item.get("features", []) and not stages.get(item.get("stage"), {}).get("seller_voice_ok"):
            FAILS.append(f"{eid}: 语音克隆集必须落在 seller_voice_ok 的舞台")
        # 演示人设：店铺口径必须放进会进 prompt 的字段（background）。style_hint 不在人设 schema，
        # 导入只报 unknown_keys、从不进 prompt——2026-09-19 F1 全自动 A 线本地模型「8-12 días」编数实锤。
        prof = (item.get("persona_demo") or {}).get("profile")
        if isinstance(prof, dict):
            for dead in ("style_hint", "system_prompt", "prompt", "instructions"):
                if dead in prof:
                    FAILS.append(f"{eid}: 演示人设含死键 {dead}（不进 prompt）——口径写进 background / context.specific_memories")
            if not str(prof.get("background") or "").strip():
                FAILS.append(f"{eid}: 演示人设缺 background（口径不进 prompt，AI 会编数字）")
        # 英文上架文案：四字段齐 + 不得夹中文（线上 EN 视图曾露中文）
        en = item.get("en") or {}
        for k in ("job", "title", "hook", "trigger"):
            v = en.get(k) or ""
            if not v:
                FAILS.append(f"{eid}: 缺 en.{k}")
            elif any("\u4e00" <= ch <= "\u9fff" for ch in v):
                FAILS.append(f"{eid}: en.{k} 夹中文「{v}」")
        # 已进生产（有 curriculum 章 / 首批 / 非 planned）：curriculum + 歌词
        if item.get("batch") == 1 or eid in eps or item.get("status") != "planned":
            ep = eps.get(eid)
            if not ep:
                FAILS.append(f"{eid}: curriculum 缺章")
            else:
                if ep.get("scenario") != "persona":
                    FAILS.append(f"{eid}: curriculum.scenario 应为 persona")
                ops = []
                for c in ep.get("footage_actions") or []:
                    ops.extend(st[0] for st in c.get("steps") or [])
                if "incoming" not in ops:
                    FAILS.append(f"{eid}: 必须有 incoming（客户侧外语来信在镜头里到达）")
                for feat in item.get("features", []):
                    need = FEATURE_OPS.get(feat)
                    if need and not (need & set(ops)):
                        FAILS.append(f"{eid}: 功能 {feat} 缺真实动作 {sorted(need)}")
                for bad in BANNED_OPS & set(ops):
                    FAILS.append(f"{eid}: 禁用步骤 {bad}")
                if "auto_reply" in item.get("features", []):
                    for need in TUTORIAL_AUTO_OPS - set(ops):
                        FAILS.append(f"{eid}: 全自动教学片缺对照动作 {need}")
                    # 主戏段（含 wait_outbound）不许写人工发送步骤
                    for c in ep.get("footage_actions") or []:
                        cops = {st[0] for st in c.get("steps") or []}
                        if "wait_outbound" in cops and cops & {"ai_reply_send", "type_send", "kb_answer", "voice_preview"}:
                            FAILS.append(f"{eid}/{c['clip']}: 全自动主戏段混入人工发送步骤")
                    for st in (s for c in ep.get("footage_actions") or [] for s in c.get("steps") or []):
                        if st[0] == "kb_answer" and (len(st) < 6 or st[5] != "preview"):
                            FAILS.append(f"{eid}: 教学片 kb_answer 须 send=preview（只演不发）")
                if "wechat_connect" in item.get("features", []):
                    for need in WECHAT_CONNECT_OPS - set(ops):
                        FAILS.append(f"{eid}: 微信接入片缺步骤 {need}")
                    for bad in SEND_OPS & set(ops):
                        FAILS.append(f"{eid}: 微信接入片不许出站步骤 {bad}（承诺 AI 只写稿不发）")
                    # 改档位段之后必须有还原步骤，且还原不能排在改档位之前
                    flat = [s[0] for c in ep.get("footage_actions") or [] for s in c.get("steps") or []]
                    if "wx_tier" in flat and "wx_restore" in flat and flat.index("wx_restore") < flat.index("wx_tier"):
                        FAILS.append(f"{eid}: wx_restore 排在 wx_tier 之前（还原了个空）")
                # 连续 wait 过长 = 无效镜头
                for c in ep.get("footage_actions") or []:
                    for st in c.get("steps") or []:
                        if st[0] == "wait" and int(st[1]) > 3000:
                            FAILS.append(f"{eid}/{c['clip']}: wait {st[1]}ms > 3000（无效镜头）")
            p = ROOT / item["chorus"]
            if not p.exists():
                FAILS.append(f"{eid}: 缺 {item['chorus']}")
            else:
                fails, _ = gate_file(p, lang="zh", audience="public")
                FAILS.extend(f"{eid}/{p.name}: {f}" for f in fails)
                txt = p.read_text(encoding="utf-8")
                for w in banned:
                    if w in txt:
                        FAILS.append(f"{eid}/{p.name}: 含禁词「{w}」")

    if a.live:
        from persona_stage import probe, stage
        for name in stages:
            try:
                pr = probe(stage(name))
            except Exception as e:  # noqa: BLE001
                FAILS.append(f"stage {name}: 探针异常 {e}")
                continue
            print(f"  stage {name}: {pr}")
            if not (pr["seller_online"] and pr["customer_online"]):
                FAILS.append(f"stage {name}: 两侧账号未全在线")

    print("== test_scenarios_persona ==")
    for f in FAILS:
        print("FAIL", f)
    print("RESULT", "FAIL" if FAILS else "PASS", f"({len(FAILS)})" if FAILS else "")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
