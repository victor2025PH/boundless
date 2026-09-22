#!/usr/bin/env python3
"""P 系列语义闸：不是「有没有文件」，是「有没有场景」。

  python gate_persona.py            # 首批全部
  python gate_persona.py --only P1

每集检查：
  ① 录屏标记：必拍动作齐全（incoming + 各功能的真实动作）且都成功（landed/translated/sent/ok）
  ② DOM 断言：两方气泡（in≥1 且 out≥1，纯浏览集除外）、翻译集须 in_translated≥1、无「正在连接」/空态
  ③ 成片：ftyp + 双流 + 时长 30~130s + 分辩率；竖屏 1080×1920
  ④ 帧多样性：成片正文抽 8 帧，两两 aHash 汉明距 —— 任意相邻两帧 < 4 记 WARN、全局中位 < 6 记 FAIL（「一张图来回切」）
  ⑤ 音频事件：语音克隆集的 voice 文件存在且 magic 合法；念白条数 = narr 标记数；副歌 take 存在
  ⑥ 文案：念白/标签/标题不含公域禁词
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
SC = json.loads((ROOT / "scenarios_persona.json").read_text(encoding="utf-8"))
FEATURE_OPS = {"translate": "xlate_quick_on", "ai_reply": "ai_reply_send", "voice_clone": "voice_gen", "urgent_filter": "urgent_filter",
               "kb": "kb_pick", "inbox": "list_filter", "group": "conv_scope", "auto_reply": "wait_outbound",
               "wechat_connect": "wx_tier"}
OK_FLAGS = {"incoming": "landed", "xlate_quick_on": "translated", "ai_reply_send": "sent", "type_send": "sent", "voice_gen": "ok", "voice_send": "sent",
            "kb_open": "shown", "kb_pick": "filled", "kb_send": "sent",
            "wait_outbound": "ok", "click_takeover": "ok", "ai_diag": "shown", "assert_quiet": "quiet",
            "wx_env": "ok", "wx_tier": "saved", "wx_verify": "online", "wx_restore": "restored", "wait_draft": "ok", "adopt_draft": "filled"}
MANUAL_SEND_OPS = {"ai_reply_send", "kb_send", "type_send", "voice_send"}
DEFAULT_DURATION = (30, 130)
# 注入舞台（customer_mode=desktop_ingest）：来信本就走入站桥，via=desktop_ingest 是设计，不是兜底
EXPECTED_VIA = {"desktop_ingest": {"desktop_ingest"}}


def check_wechat_connect(eid: str, ep: dict, clips: list[tuple[str, list[dict]]]) -> tuple[list[str], list[str]]:
    """F2 微信接入教学专属闸。本片三个承诺：① 全片零出站——「AI 只写稿，发不发你定」，任何段都不许有人工发送 /
    自动出站动作；② 引导页真点了「保存」改档位，就必须有 wx_restore 还原成功（老板实例的档位不能被教学片带走）；
    ③ 拟稿人审的稿要命中人设数字（draft_gate.any）且不含拖延话（forbid）——稿是本片唯一的 AI 产出，核它。"""
    fails: list[str] = []
    warns: list[str] = []
    gate = ep.get("draft_gate") or {}
    any_needles = [str(x) for x in (gate.get("any") or [])]
    forbid = [str(x) for x in (gate.get("forbid") or [])]
    saw_tier = saw_restore = False
    drafts: list[str] = []
    for clip, markers in clips:
        ops = [k.get("op") for k in markers]
        bad = sorted(set(ops) & (MANUAL_SEND_OPS | {"wait_outbound"}))
        if bad:
            fails.append(f"{eid}/{clip}: 接入教学片出现出站动作 {bad}（本片承诺零出站：AI 只写稿）")
        for k in markers:
            if k.get("op") == "wx_tier" and k.get("saved"):
                saw_tier = True
            if k.get("op") == "wx_restore":
                saw_restore = saw_restore or bool(k.get("restored"))
            if k.get("op") == "wait_draft" and k.get("ok"):
                drafts.append(str(k.get("text") or k.get("ui_text") or ""))
                if k.get("nudged"):
                    warns.append(f"{eid}/{clip}: 草稿条靠重开会话才出现（draft_ready 事件没到前端——引擎未重启到含该事件的版本？）")
            if k.get("op") == "adopt_draft" and k.get("clicked_send"):
                fails.append(f"{eid}/{clip}: adopt_draft 记录了点发送")
    if saw_tier and not saw_restore:
        fails.append(f"{eid}: 引导页改了副驾档位（wx_tier saved）但没有成功的 wx_restore——老板实例档位被带走了")
    if not drafts:
        fails.append(f"{eid}: 没有成功的 wait_draft（拟稿人审的稿没在镜头里出现）")
    blob = " ".join(drafts)
    if drafts and any_needles and not any(n in blob for n in any_needles):
        fails.append(f"{eid}: 草稿口径未命中人设数字 any={any_needles[:4]}…：{blob[:60]!r}")
    for d in drafts:
        hit = next((f for f in forbid if f in d), None)
        if hit:
            fails.append(f"{eid}: 草稿含拖延话「{hit}」：{d[:40]!r}")
    return fails, warns


def _cjk_ratio(s: str) -> float:
    t = "".join(ch for ch in (s or "") if not ch.isspace())
    if not t:
        return 0.0
    return sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff") / len(t)


def check_auto_reply(eid: str, ep: dict, clips: list[tuple[str, list[dict]]]) -> tuple[list[str], list[str]]:
    """F 系列（features 含 auto_reply）专属闸。clips=[(clip_name, markers)]。

    ① 主戏段（含 wait_outbound）里不许有任何人工发送动作（ai_reply_send / kb_send / type_send / voice_send）——
       「全自动」的全部意义就是没人点发送；② 每条自动出站的实发正文须是客户语言（非 CJK；ep.out_lang 非中文时）；
    ③ 口径：auto_reply_gate.any 至少命中一项（数字来自人设），原稿不得含 forbid 拖延话；
    ④ 接管有效：click_takeover ok 且随后 assert_quiet quiet；⑤ 群舞台段（use_stage 到群后）无任何出站动作。"""
    fails: list[str] = []
    warns: list[str] = []
    gate = ep.get("auto_reply_gate") or {}
    any_needles = [str(x) for x in (gate.get("any") or [])]
    forbid = [str(x) for x in (gate.get("forbid") or [])]
    out_lang = str(ep.get("out_lang") or "").lower()
    all_out_texts: list[str] = []
    all_out_orig: list[str] = []
    saw_wait = False
    for clip, markers in clips:
        ops = [k.get("op") for k in markers]
        if "wait_outbound" in ops:
            saw_wait = True
            bad = sorted(set(ops) & MANUAL_SEND_OPS)
            if bad:
                fails.append(f"{eid}/{clip}: 全自动主戏里出现人工发送动作 {bad}（这段的意义就是没人点发送）")
            for k in markers:
                if k.get("op") != "wait_outbound":
                    continue
                if k.get("clicked_send"):
                    fails.append(f"{eid}/{clip}: wait_outbound 记录了点发送")
                api = k.get("api") or []
                if k.get("ok") and not api:
                    warns.append(f"{eid}/{clip}: wait_outbound 画面有新出站但 thread 真值为空（API 读不到，语种/口径闸跳过）")
                for a in api:
                    txt, orig = str(a.get("text") or ""), str(a.get("original") or "")
                    all_out_texts.append(txt)
                    all_out_orig.append(orig)
                    if str(a.get("sent_by") or "") == "agent":
                        warns.append(f"{eid}/{clip}: 自动出站 sent_by=agent（像是人发的）：{txt[:40]!r}")
                    if out_lang and out_lang not in ("zh", "zh-cn", "zh-tw", "yue") and _cjk_ratio(txt) > 0.3:
                        fails.append(f"{eid}/{clip}: 自动出站实发正文是中文，未译成 {out_lang}：{txt[:50]!r}")
        # ⑤ 群舞台段不许出站
        in_group = False
        for k in markers:
            if k.get("op") == "use_stage":
                in_group = str(k.get("stage") or "").upper().endswith("GROUP")
            elif in_group and k.get("op") in (MANUAL_SEND_OPS | {"wait_outbound"}):
                fails.append(f"{eid}/{clip}: 群舞台段出现出站动作 {k.get('op')}（群默认不说是本片承诺）")
        # ④ 接管有效
        tko = [k for k in markers if k.get("op") == "click_takeover"]
        quiet = [k for k in markers if k.get("op") == "assert_quiet"]
        if tko and not quiet:
            warns.append(f"{eid}/{clip}: 有接管无 assert_quiet 对照（看不出 AI 真让位）")
    if saw_wait:
        blob = " ".join(all_out_texts + all_out_orig)
        if any_needles and blob.strip() and not any(n in blob for n in any_needles):
            fails.append(f"{eid}: 自动出站口径未命中人设数字 any={any_needles[:4]}…（AI 答非所问 / 人设未生效）")
        for o in all_out_orig:
            hit = next((f for f in forbid if f in o), None)
            if hit:
                fails.append(f"{eid}: 自动出站原稿含拖延话「{hit}」：{o[:40]!r}")
    return fails, warns


def ffprobe(p: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return json.loads(r.stdout) if r.returncode == 0 else {}


def ahash(p: Path) -> int:
    im = Image.open(p).convert("L").resize((16, 16), Image.LANCZOS)
    px = list(im.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for v in px:
        bits = (bits << 1) | (1 if v > avg else 0)
    return bits


def ham(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def frames(mp4: Path, ts: list[float], tmp: Path) -> list[Path]:
    out = []
    for i, t in enumerate(ts):
        dst = tmp / f"_gate_{mp4.stem}_{i}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1", str(dst)], capture_output=True)
        if dst.exists():
            out.append(dst)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    ceps = {e["id"]: e for e in cur["episodes"]}
    banned = SC["banned_public_phrases"]
    want = {x.strip() for x in a.only.split(",") if x.strip()}
    fails: list[str] = []
    warns: list[str] = []
    for ep in SC["episodes"]:
        # 默认：所有已出片的集（out/<id>/<id>_persona.mp4 存在）；--only 指定则必须存在，不存在算 FAIL
        if want:
            if ep["id"] not in want:
                continue
        elif not (OUT / ep["id"] / f"{ep['id']}_persona.mp4").exists():
            continue
        eid = ep["id"]
        epdir = OUT / eid
        print(f"-- {eid} {ep['title']}")
        cep = ceps.get(eid)
        if not cep:
            fails.append(f"{eid}: curriculum 缺")
            continue
        ops_seen: dict[str, list[dict]] = {}
        narr_n = 0
        clips_markers: list[tuple[str, list[dict]]] = []
        # 断言跨段聚合：一集里「两方气泡 / 译文行」只要在某一段出现即算达标（各段舞台状态不同）
        agg = {"in_count": 0, "out_count": 0, "in_translated": 0, "out_translated": 0}
        for c in cep["footage_actions"]:
            js = epdir / f"footage_{c['clip']}.json"
            webm = js.with_suffix(".webm")
            if not js.exists() or not webm.exists():
                fails.append(f"{eid}/{c['clip']}: 缺录屏或标记")
                continue
            m = json.loads(js.read_text(encoding="utf-8"))
            clips_markers.append((c["clip"], list(m.get("markers") or [])))
            for k in m.get("markers") or []:
                ops_seen.setdefault(k["op"], []).append(k)
                if k["op"] == "narr":
                    narr_n += 1
                if k.get("error"):
                    fails.append(f"{eid}/{c['clip']}: 步骤 {k['op']} 报错 {k['error'][:60]}")
                flag = OK_FLAGS.get(k["op"])
                if flag and not k.get(flag, True):
                    fails.append(f"{eid}/{c['clip']}: {k['op']} 未成功（{flag}=False）")
                # 来信靠兜底补拉/补发才到（record_persona.incoming 的 via）：成片能过，但入站链有病，
                # 不许被兜底静默吃掉。旧 marker 没有 via 键 → 不追溯。
                if k["op"] == "incoming" and k.get("via") not in (None, "live"):
                    st_mode = str((SC["stages"].get(ep.get("stage")) or {}).get("customer_mode") or "")
                    if k["via"] not in EXPECTED_VIA.get(st_mode, set()):
                        warns.append(f"{eid}/{c['clip']}: 来信非实时到达 via={k['via']}（兜底补拉/补发才进工作台，查入站链）")
                # 「超时 / 需人工」筛出 0 行 = 字幕「先举手」压在「没有符合当前筛选的对话」上（P3 两次成片实锤）。
                # 旧 marker 没有 sla_rows 键 → 不追溯；attn_rows=-1 表示页签隐藏（也是 0 条）。
                if k["op"] == "urgent_filter" and "sla_rows" in k:
                    if int(k.get("sla_rows") or 0) <= 0:
                        warns.append(f"{eid}/{c['clip']}: 「超时」筛选下 0 行，字幕压空列表（sla_demo 未铺 / 未过阈值）")
                    if int(k.get("attn_rows") if k.get("attn_rows") is not None else -1) <= 0:
                        warns.append(f"{eid}/{c['clip']}: 「需人工」页签 0 行或隐藏（需 ≥1 条 crit 或需人工标签会话）")
            asr = m.get("asserts") or {}
            for k in agg:
                agg[k] = max(agg[k], int(asr.get(k) or 0))
            if asr.get("connecting"):
                fails.append(f"{eid}/{c['clip']}: 收尾帧含「正在连接」")
            if asr.get("empty_state"):
                fails.append(f"{eid}/{c['clip']}: 收尾帧是空态「选择一个对话」")
            # 有效镜头长度
            eff = float(m.get("end_t") or m["duration"]) - float(m.get("content_offset") or m["ready_offset"])
            if eff < 8:
                fails.append(f"{eid}/{c['clip']}: 有效镜头仅 {eff:.1f}s")
        if "incoming" not in ops_seen:
            fails.append(f"{eid}: 无 incoming（外语来信没在镜头里到达）")
        for feat in ep["features"]:
            op = FEATURE_OPS.get(feat)
            if op and op not in ops_seen:
                fails.append(f"{eid}: 功能 {feat} 无真实动作 {op}")
        print(f"   asserts(聚合) {agg}")
        if agg["in_count"] < 1 and not any(k.get("landed") for k in ops_seen.get("incoming", [])):
            fails.append(f"{eid}: 无入站气泡")
        if any(f in ep["features"] for f in ("ai_reply", "voice_clone", "mode")) and agg["out_count"] < 1:
            fails.append(f"{eid}: 无出站气泡（回复没发出去）")
        if "translate" in ep["features"] and agg["in_translated"] < 1 and not any(k.get("translated") for k in ops_seen.get("xlate_quick_on", [])):
            fails.append(f"{eid}: 入站无译文行")
        if "auto_reply" in ep["features"]:
            f2, w2 = check_auto_reply(eid, ep, clips_markers)
            fails.extend(f2)
            warns.extend(w2)
        if "wechat_connect" in ep["features"]:
            f3, w3 = check_wechat_connect(eid, ep, clips_markers)
            fails.extend(f3)
            warns.extend(w3)
        if "voice_clone" in ep["features"]:
            vs = [k for k in ops_seen.get("voice_gen", []) if k.get("audio")]
            if not vs:
                fails.append(f"{eid}: 语音克隆集无试听音频落盘")
            for k in vs:
                p = ROOT / k["audio"]
                b = p.read_bytes()[:12] if p.exists() else b""
                if not (b[:4] == b"RIFF" or b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or b[:4] == b"OggS" or b[4:8] == b"ftyp" or b[:4] == b"fLaC"):
                    fails.append(f"{eid}: 语音文件 magic 不合法 {p.name}")
        # 成片
        for name, (w, h) in ((f"{eid}_persona.mp4", (1920, 1080)), (f"{eid}_persona_v.mp4", (1080, 1920))):
            mp4 = epdir / name
            if not mp4.exists():
                fails.append(f"{eid}: 缺成片 {name}")
                continue
            if mp4.read_bytes()[4:8] != b"ftyp":
                fails.append(f"{eid}: {name} 非 ftyp")
            info = ffprobe(mp4)
            kinds = {s["codec_type"] for s in info.get("streams", [])}
            d = float(info.get("format", {}).get("duration") or 0)
            v = next((s for s in info.get("streams", []) if s["codec_type"] == "video"), {})
            if kinds != {"video", "audio"}:
                fails.append(f"{eid}: {name} streams={kinds}")
            lo, hi = tuple(ep.get("duration_range") or DEFAULT_DURATION)
            if not (lo <= d <= hi):
                fails.append(f"{eid}: {name} 时长 {d:.1f}s 不在 {lo}~{hi}")
            if (v.get("width"), v.get("height")) != (w, h):
                fails.append(f"{eid}: {name} 分辨率 {v.get('width')}x{v.get('height')}")
            # 帧多样性（正文段：跳过钩子/尾卡）
            if d > 20:
                ts = [4 + i * (d - 12) / 7 for i in range(8)]
                fr = frames(mp4, ts, epdir)
                hs = [ahash(p) for p in fr]
                adj = [ham(hs[i], hs[i + 1]) for i in range(len(hs) - 1)]
                allp = sorted(ham(hs[i], hs[j]) for i in range(len(hs)) for j in range(i + 1, len(hs)))
                med = allp[len(allp) // 2] if allp else 0
                print(f"   {name}: {d:.1f}s adj_hamming={adj} median_all={med}")
                if med < 6:
                    fails.append(f"{eid}: {name} 画面多样性过低（中位汉明 {med}）——像一张图来回切")
                if adj and min(adj) < 4:
                    warns.append(f"{eid}: {name} 有相邻抽帧几乎相同（{min(adj)}）")
                for p in fr:
                    p.unlink(missing_ok=True)
        meta_p = epdir / f"{eid}_persona.meta.json"
        if meta_p.exists():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            if len(meta.get("narr") or []) != narr_n:
                fails.append(f"{eid}: 念白条数 {len(meta.get('narr') or [])} ≠ 标记 {narr_n}")
            if not meta.get("chorus"):
                fails.append(f"{eid}: 成片无副歌 take")
            if not meta.get("bed"):
                warns.append(f"{eid}: 成片无底床")
            if "voice_clone" in ep["features"] and not meta.get("voices"):
                fails.append(f"{eid}: 成片未混入克隆声")
            blob = " ".join(x[2] for x in meta.get("narr") or [])
        else:
            blob = ""
        blob += " " + json.dumps(cep, ensure_ascii=False) + " " + json.dumps(ep, ensure_ascii=False)
        for w in banned:
            if w in blob:
                fails.append(f"{eid}: 文案含公域禁词「{w}」")
        cm = json.loads((ROOT / ep["chorus"]).read_text(encoding="utf-8")) if False else None  # noqa: F841
    print("== gate_persona ==")
    for w in warns:
        print("WARN", w)
    for f in fails:
        print("FAIL", f)
    print("RESULT", "FAIL" if fails else "PASS", f"({len(fails)} fails, {len(warns)} warns)")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
