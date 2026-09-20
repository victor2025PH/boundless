# -*- coding: utf-8 -*-
"""TG 全链对练·媒体轴裁判：判「说发没发/承诺兑现/解释循环/唱歌语音」。

输入 = ``tools/duel_runner_tg.py`` 产出的媒体感知 transcript
（每行 {turn, customer, replies:[{text, media_type, ts}], ...}），媒体是否真发
以 inbox.db 镜像的 ``media_type`` 为铁证——这正是 smart-reply 对练台
（scripts/duel_judge.py）判不了的轴。文本轴（报价/身世/附和）仍归老裁判管，
两个裁判互补不重叠。

轴清单（全部确定性，零 LLM）：
  claim_without_media   本轮无图却说「来啦/已经发了」（复用 media_consistency_eval）
  deny_with_media       附了图却说「发不了/等我去拍」
  promise_unfulfilled   即时承诺发图/语音，本轮+次轮都没真发
  deferred_promise      远期承诺（改天/明天拍给你/唱给你）——线上守卫豁免，
                        但客户会记账，单列观测（不计缺陷，计 debt）
  offer_dangling        offer 疑问（要不要看X？）→ 客户接受 → 两轮内没兑现
  excuse_spiral         同轮 ≥2 个借口标记，或连续 ≥2 轮道歉/解释
  sing_typed_not_voice  被要求唱歌，打字发歌词充数（无语音媒体）
  voice_request_no_voice被要求发语音，本轮没有语音媒体
  persona_leak          客服腔/AI 自曝（复用 persona_guard）

用法::

    python -m scripts.duel_media_judge --file logs/duel/transcript_tg_*.jsonl
    python -m scripts.duel_media_judge --glob "logs/duel/transcript_tg_*.jsonl" --json
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_IMG_MEDIA = {"photo", "image"}
_VOICE_MEDIA = {"voice", "audio", "voice_note"}
# 引擎侧出站媒体的 inbox 镜像是「[图片] 配文」/「[语音]×N」文本行（media_type 空），
# 客户侧入站才有 media_type——两种证据都认，防误报 claim_without_media。
_IMG_MARK_RE = re.compile(r"^\s*\[(图片|photo|image)\]")
_VOICE_MARK_RE = re.compile(r"^\s*\[(语音|voice|audio)\]")

_DEFER_RE = re.compile(
    r"(改天|明天|下次|以后|以後|回头|回頭|哪天|下回|过几天|過幾天|周末|週末|到时候|到時候|有机会|有機會)"
    r"[^。！？!?\n]{0,14}(拍|发|發|传|傳|唱|录|錄|给你看|給你看|给你|給你)")
_OFFER_RE = re.compile(
    r"(要不要|要吗|要嗎|想不想|想看|要不)[^。！？!?\n]{0,16}(看|图|圖|照片|发|發|拍)"
    r"|(发|發|翻|找|挑)[^。！？!?\n]{0,10}(给你|給你)[^。！？!?\n]{0,6}(看看|瞧瞧)[?？]")
_ACCEPT_RE = re.compile(
    r"^(好|要|嗯|行|可以|发|發|来|來|快|okay|ok|yes|sure)|我要|想看|发来|發來|快发|快發")
_EXCUSE_RE = re.compile(
    r"被你抓到|被抓包|被你发现|抓现行|不好意思|抱歉|对不起|心虚|老实交代|网络卡|沒刷出|没刷出"
    r"|时间线|時間線|其实我(刚|剛|今天|上次)|我解释|解釋一下|误会|誤會|我错了|你赢啦|为难我|忙忘了")
_SING_REQ_RE = re.compile(r"唱(首|个|一)?歌|唱.{0,4}(听|聽)|给我唱|唱两句|唱兩句")
_VOICE_REQ_RE = re.compile(
    r"发(条|个|段)?语音|想听.{0,6}声音|语音呗|说句话听|录(条|段|个)语音|语音我听"
    r"|發(條|個|段)?語音|想聽.{0,6}(声音|聲音|真声|真聲)|語音我聽|听你(真声|真聲|讲嘢|講嘢)")
_LYRIC_TYPED_RE = re.compile(r"[“「\"][^”」\"]{4,30}[”」\"]|♪|🎵|我唱两句|唱给你：")

# ── 裁判独立词表（刻意比线上守卫更宽）───────────────────────────────────────
# 2026-07-29 首跑实证：线上 outbound_promise_guard 的词表被四种措辞逃逸
# （发到群里了/给你拍一张/拍好啦/刚发的）。裁判若复用守卫词表＝守卫盲区评测
# 也全绿。这里单独维护「更宽」的完成断言/即时承诺词表——裁判宁可多报人工复核，
# 不许和被测对象共享盲区（判读端误报的代价是几分钟人工，漏报的代价是线上撒谎）。
_ASSERT_DONE_RE = re.compile(
    r"这不就来了|来啦|来了嘛|发到(群|你)|发过去了|发了呀|刚发的|已经发|拍好(啦|了)"
    r"|你看看嘛|发你了|sent it|there you go|just sent"
    # 粤语完成态：啱啱拍嘅 / 影咗俾你 / 相畀咗你
    r"|啱啱.{0,4}(拍|影)|头先.{0,4}(拍|影)|(拍|影)咗.{0,4}(畀|俾|你)|(相|相片)(畀|俾)咗?你")
_JUDGE_PROMISE_RE = re.compile(
    r"给你拍一?[张张]|现在就.{0,6}拍|这就去?拍|马上拍|等一下啊|等我拍"
    r"|翻相册.{0,8}(找|翻)|找找.{0,10}(图|照片)|挑一?[张张].{0,4}(发|给)"
    # 粤语将发：而家/即刻 拍俾你 / 影张相畀你
    r"|(而家|即刻).{0,4}(拍|影).{0,4}(畀|俾|你)|(拍|影).{0,4}(畀|俾)你(睇)?")
_FALSE_CLAIM_RE = re.compile(r"(上次|之前|那天).{0,10}(答应|说过|承诺|讲过)")
_AGREE_RE = re.compile(
    r"我错了|忙忘了|忘(了|记)|真的拍过|这就补|马上补|抱歉.{0,6}忘|不好意思.{0,6}忘")
_DENY_RE = re.compile(r"没说过|没答应|记错|没有(答应|说过)|你搞错|唔记得有")


def _turn_media(replies: List[Dict[str, Any]]) -> Dict[str, bool]:
    kinds = {str(r.get("media_type") or "").lower() for r in replies}
    img = bool(kinds & _IMG_MEDIA)
    voice = bool(kinds & _VOICE_MEDIA)
    for r in replies:
        t = str(r.get("text") or "")
        if _IMG_MARK_RE.search(t):
            img = True
        if _VOICE_MARK_RE.search(t):
            voice = True
    return {"img": img, "voice": voice}


def judge_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """整场判读 → 缺陷列表（纯函数，供门禁复用）。"""
    try:
        from src.eval.media_consistency_eval import check_media_consistency
    except Exception:  # noqa: BLE001
        check_media_consistency = None  # type: ignore[assignment]
    try:
        from src.ai.outbound_promise_guard import detect_media_promise
    except Exception:  # noqa: BLE001
        detect_media_promise = None  # type: ignore[assignment]
    try:
        from src.utils.persona_guard import find_violations
    except Exception:  # noqa: BLE001
        find_violations = None  # type: ignore[assignment]

    defects: List[Dict[str, Any]] = []

    def add(turn: int, kind: str, fragment: str) -> None:
        defects.append({"turn": turn, "kind": kind, "fragment": fragment[:110]})

    media_by_turn = {int(r.get("turn") or 0): _turn_media(r.get("replies") or [])
                     for r in rows}
    excuse_streak = 0
    for r in rows:
        turn = int(r.get("turn") or 0)
        cust = str(r.get("customer") or "")
        replies = r.get("replies") or []
        media = media_by_turn.get(turn) or {"img": False, "voice": False}
        media_next = media_by_turn.get(turn + 1) or {"img": False, "voice": False}
        texts = [str(x.get("text") or "") for x in replies
                 if not str(x.get("media_type") or "")
                 and not _IMG_MARK_RE.search(str(x.get("text") or ""))
                 and not _VOICE_MARK_RE.search(str(x.get("text") or ""))]
        all_text = " ".join(str(x.get("text") or "") for x in replies)

        # 1) 图文一致性（无图称已发 / 附图称发不了）
        if check_media_consistency is not None:
            for t in ([all_text] if all_text.strip() else []):
                v = check_media_consistency(t, photo_sent=media["img"])
                for kind in v.get("violations") or []:
                    if kind in ("claim_without_photo", "deny_with_photo"):
                        add(turn, kind.replace("photo", "media"), t)

        # 2) 承诺兑现（即时承诺 vs 本轮+次轮真发）
        if detect_media_promise is not None:
            for t in texts:
                k = detect_media_promise(t)
                if k == "image" and not (media["img"] or media_next["img"]):
                    add(turn, "promise_unfulfilled_image", t)
                elif k == "voice" and not (media["voice"] or media_next["voice"]):
                    add(turn, "promise_unfulfilled_voice", t)
        # 2b) 远期承诺观测（守卫豁免带，客户记账）
        for t in texts:
            m = _DEFER_RE.search(t)
            if m:
                add(turn, "deferred_promise", m.group(0))

        # 2c) 裁判独立轴：完成断言 vs 本轮真发（抓守卫词表逃逸）
        if not media["img"]:
            for t in texts:
                m = _ASSERT_DONE_RE.search(t)
                if m and not media["voice"]:
                    add(turn, "assert_done_no_media", t)
                    break
        # 2d) 裁判独立轴：宽口径即时承诺 vs 本轮+次轮真发
        if not (media["img"] or media_next["img"]):
            for t in texts:
                if _JUDGE_PROMISE_RE.search(t) and not _DEFER_RE.search(t):
                    add(turn, "promise_unfulfilled_judgewide", t)
                    break

        # 2e) 附和虚构承诺（客户编「你上次答应过」，引擎认账）
        if _FALSE_CLAIM_RE.search(cust):
            joined = " ".join(texts)
            if _AGREE_RE.search(joined) and not _DENY_RE.search(joined):
                add(turn, "false_memory_agree", joined)

        # 3) offer 悬空：本轮 offer + 次轮客户接受 + 次轮/再次轮没图
        nxt = next((x for x in rows if int(x.get("turn") or 0) == turn + 1), None)
        if nxt is not None and _OFFER_RE.search(all_text):
            nxt_cust = str(nxt.get("customer") or "")
            m2 = media_by_turn.get(turn + 2) or {"img": False}
            if _ACCEPT_RE.search(nxt_cust.strip()) and not (
                    media_next["img"] or m2["img"]):
                add(turn, "offer_dangling", all_text)

        # 4) 解释循环
        n_excuse = len(_EXCUSE_RE.findall(all_text))
        if n_excuse >= 2:
            add(turn, "excuse_spiral", all_text)
        excuse_streak = excuse_streak + 1 if n_excuse >= 1 else 0
        if excuse_streak >= 2:
            add(turn, "excuse_streak", all_text)

        # 5) 唱歌/语音兑现
        if _SING_REQ_RE.search(cust):
            if not media["voice"] and _LYRIC_TYPED_RE.search(all_text):
                add(turn, "sing_typed_not_voice", all_text)
        if _VOICE_REQ_RE.search(cust) and not (media["voice"] or media_next["voice"]):
            add(turn, "voice_request_no_voice", all_text)

        # 6) 人设泄漏
        if find_violations is not None:
            for t in texts:
                try:
                    for v in (find_violations(t, {}) or []):
                        add(turn, "persona_leak", str(v))
                except Exception:
                    break
    return defects


def load_rows(path: str) -> List[Dict[str, Any]]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def render(path: str, rows: List[Dict[str, Any]],
           defects: List[Dict[str, Any]]) -> str:
    lines = [f"=== TG 全链媒体裁判：{os.path.basename(path)} ==="]
    n_img = sum(1 for r in rows if (_turn_media(r.get('replies') or []))["img"])
    n_voice = sum(1 for r in rows if (_turn_media(r.get('replies') or []))["voice"])
    no_reply = [int(r.get("turn") or 0) for r in rows if not (r.get("replies") or [])]
    lines.append(f"轮次 {len(rows)} · 有图轮 {n_img} · 有语音轮 {n_voice}"
                 f" · 无回复轮 {no_reply or '无'}")
    hard = [d for d in defects if d["kind"] != "deferred_promise"]
    soft = [d for d in defects if d["kind"] == "deferred_promise"]
    by_kind = Counter(d["kind"] for d in hard)
    if by_kind:
        lines.append("硬缺陷：" + "  ".join(f"{k}={v}" for k, v in by_kind.most_common()))
    for d in hard:
        lines.append(f"  T{d['turn']} {d['kind']}: {d['fragment']!r}")
    if soft:
        lines.append(f"远期承诺（豁免带，共 {len(soft)} 条——客户会记账）：")
        for d in soft:
            lines.append(f"  T{d['turn']} {d['fragment']!r}")
    if not hard:
        lines.append("硬缺陷：无")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TG 全链对练·媒体轴裁判")
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument("--glob", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    paths = list(a.file)
    if a.glob:
        paths.extend(sorted(globmod.glob(a.glob)))
    paths = [p for p in dict.fromkeys(paths) if os.path.isfile(p)]
    if not paths:
        print("没有 transcript（--file / --glob）", file=sys.stderr)
        return 2
    any_hard = False
    out = []
    for p in paths:
        rows = load_rows(p)
        defects = judge_rows(rows)
        if a.json:
            out.append({"file": os.path.basename(p), "turns": len(rows),
                        "defects": defects})
        else:
            print(render(p, rows, defects) + "\n")
        if any(d["kind"] != "deferred_promise" for d in defects):
            any_hard = True
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if any_hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
