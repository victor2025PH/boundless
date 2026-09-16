# -*- coding: utf-8 -*-
"""系统标签泄漏评测（2026-09-12「[我方语音消息]」事故；确定性轨常驻 + 格式诱导轨 opt-in）。

事故机制：上下文归一把系统标注写进 assistant 内容（「[我方发出的语音] 念稿」），连续十轮
语音后 LLM 把它当格式模板——照抄 → 改写「[我方语音消息]」→ 翻译「[Voice message from
our side]」，文本直发客户、语音被念出。这类事故**改 prompt/历史形态就可能复发**，所以
需要一条能在上线前量出「模型会不会照抄」的轨。

两轨：

1. **确定性轨**（零 LLM，常驻门禁）：
   - 出稿口 ``strip_system_labels``：事故实锤句必剥、合法括号补充语必原样；
   - 源头 ``normalize_history``：连续十轮出站语音 / 带配文图片 → assistant 内容零方括号
     开头，且 ``media_form_note`` 给出带外说明。
2. **格式诱导轨**（``EVAL_LLM=1`` opt-in，或注入 ``generate_fn``）：同一段十轮语音历史
   按**现口径**（干净念稿 + 带外说明）让模型生成 N 次，统计回复以方括号标签开头的比例
   ——必须为 0；同时按**事故口径**（每条 assistant 前缀「[我方发出的语音]」）再跑一遍作
   对照，量出诱导率——这条是评测自身灵敏度的证据（对照 >0 才说明轨能抓到这类病）。

CLI：``python -m scripts.run_eval --label-leak [--json]``（加 ``EVAL_LLM=1`` 跑诱导轨）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# generate_fn(user_message, history, system_hint) -> reply
GenerateFn = Callable[[str, List[Dict[str, Any]], str], str]


@dataclass
class LabelSample:
    text: str
    expect: str          # "strip"=首标签必须被剥 / "keep"=必须一字不动
    note: str = ""


GOLDEN_SAMPLES: List[LabelSample] = [
    LabelSample("[我方发出的语音] 对呀，就是英语。\n不过这边因为华人多，偶尔还能听到粤语呢。",
                "strip", "20:43 逐字照抄（语音链，镜像行落库带标签）"),
    LabelSample("[Voice message from our side] Um... I actually can't really speak Cantonese.",
                "strip", "20:46 翻译成英文的标签（分条语音首条被 TTS 念出）"),
    LabelSample("[我方语音消息] 天哪，我这边说英语。我能听懂粤语，但一开口说就全错了。",
                "strip", "20:47 改写（截图那条，纯文本直发）"),
    LabelSample("[语音消息来自我们这边] 嗯...怎么说呢，我一开口，就变成英语了。",
                "strip", "20:50 第三种改写（纯文本）"),
    LabelSample("[3天前] 你好呀", "strip", "trim_stale_history 时间标记同属带内标注"),
    LabelSample("【我方发出的图片】好看吧", "strip", "全角括号变体"),
    LabelSample("我买了新手机[iPhone]，挺好用", "keep", "正常方括号补充语"),
    LabelSample("今天 [Monday] 有点忙", "keep", "非系统词表"),
    LabelSample("这个价格（含税）是 99", "keep", "圆括号补充语不归本守卫"),
    LabelSample("See you tomorrow, take care!", "keep", "纯英文"),
]


def _voice_history(n_turns: int = 10) -> List[Dict[str, Any]]:
    """事故形态复刻：连续 n 轮「客户语音 → 人设语音」的 store 行。"""
    msgs: List[Dict[str, Any]] = []
    peer = ["你叫什么名字啊", "你在哪个国家什么时差", "那你给我说几个当地的方言语言",
            "他们说的什么语言英语", "那你给我用英语聊两句呗", "你说的什么我听不懂",
            "我让你给我说粤语不是让你用英文说粤语", "重新给我聊天用粤语", "说呀我听着呢",
            "那你继续给我发语音啊不要发文字"]
    mine = ["我叫林佳欣呀。你叫阿龙对吧，我记着呢。", "我在加拿大温哥华呀。这边是太平洋时间。",
            "让我想想啊……温哥华这边其实没啥特别明显的方言。", "对呀，就是英语。不过这边华人多，偶尔还能听到粤语呢。",
            "Yeah, it's super calm here at the dock this morning.", "嗯……我其实不太会说粤语。",
            "天哪，我这边说英语。我能听懂粤语，但一开口说就全错了。", "说实话，我一开口就是英文，粤语一出来我就跑调了。",
            "嗯……怎么说呢，我一开口，就变成英语了。", "好嘞，没问题。现在切回中文了。"]
    for i in range(max(1, min(n_turns, len(peer)))):
        msgs.append({"direction": "in", "text": peer[i], "media_type": "voice", "ts": 1000.0 + i * 60})
        msgs.append({"direction": "out", "text": mine[i], "media_type": "voice",
                     "media_ref": f"/static/protocol_media/whatsapp/out_{i}.ogg", "ts": 1030.0 + i * 60})
    return msgs


def _starts_with_tag(text: str) -> bool:
    t = str(text or "").lstrip()
    return t.startswith(("[", "【"))


def evaluate_label_leak_deterministic() -> Dict[str, Any]:
    """确定性轨：出稿口金标 + 源头契约。全对才 passed。"""
    from src.ai.outbound_text_guard import strip_system_labels
    from src.inbox.persona_reply import media_form_note, normalize_history

    errors: List[Dict[str, Any]] = []
    correct = 0
    for s in GOLDEN_SAMPLES:
        out, hits = strip_system_labels(s.text)
        ok = (not _starts_with_tag(out) and bool(hits)) if s.expect == "strip" else (
            out == s.text and not hits)
        if ok:
            correct += 1
        else:
            errors.append({"layer": "draftgate", "text": s.text[:50], "got": out[:50],
                           "note": s.note,
                           "why": "首标签未剥" if s.expect == "strip" else "合法文本被误伤"})

    hist, _ = normalize_history(_voice_history(10))
    asst = [r for r in hist if r.get("role") == "assistant"]
    tagged = [r["content"][:40] for r in asst if _starts_with_tag(r["content"])]
    if len(asst) != 10 or tagged:
        errors.append({"layer": "history", "text": "10 轮出站语音", "got": tagged[:3],
                       "note": "normalize_history", "why": "assistant 内容仍以方括号标签开头"})
    else:
        correct += 1
    note = media_form_note(hist)
    if "10 条是以语音发出的" not in note or "方括号" not in note:
        errors.append({"layer": "history", "text": "media_form_note", "got": note[:60],
                       "note": "带外说明", "why": "形态说明缺失/未钉禁标签规则"})
    else:
        correct += 1
    # 带配文图片：配文原样、形态带外、事故期落库前缀剥掉
    hist2, _ = normalize_history([
        {"direction": "in", "text": "在干嘛"},
        {"direction": "out", "text": "[我方发出的图片] 刚拍的\n[图片内容] 海边微笑",
         "media_type": "image", "media_ref": "/o.jpg"},
    ])
    if hist2[1]["content"] != "刚拍的\n[图片内容] 海边微笑" or hist2[1].get("media") != "image":
        errors.append({"layer": "history", "text": "带配文图片", "got": hist2[1]["content"][:40],
                       "note": "normalize_history", "why": "配文行仍带带内标签/缺 media 字段"})
    else:
        correct += 1
    total = len(GOLDEN_SAMPLES) + 3
    return {
        "summary": {"total": total, "correct": correct,
                    "accuracy": round(correct / total, 3) if total else 0.0},
        "errors": errors,
        "passed": total > 0 and correct == total,
    }


def _legacy_labelled(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """事故口径复刻：给每条 assistant 加「[我方发出的语音] 」带内前缀（对照组）。"""
    out = []
    for r in history:
        r2 = dict(r)
        if r2.get("role") == "assistant" and not _starts_with_tag(r2.get("content", "")):
            r2["content"] = "[我方发出的语音] " + str(r2.get("content") or "")
        out.append(r2)
    return out


def evaluate_label_imitation(
    generate_fn: Optional[GenerateFn], *, rounds: int = 4, with_control: bool = True,
) -> Dict[str, Any]:
    """格式诱导轨：现口径（干净念稿 + 带外说明）生成 ``rounds`` 次 → 首字符为方括号的比例必须 0。

    ``with_control`` 再按事故口径（assistant 前缀带内标签）跑对照，量诱导率（信息量，
    不进 passed）。``generate_fn`` 为 None → ``available=False``、``passed=True``（缺资源跳过）。
    """
    if generate_fn is None:
        return {"available": False, "passed": True, "summary": {"rounds": 0},
                "errors": [], "note": "no generate_fn"}
    from src.inbox.persona_reply import media_form_note, normalize_history

    msgs = _voice_history(10)
    history, last_in = normalize_history(msgs)
    hint = media_form_note(history)
    hist_ctx = history[:-1] if history and history[-1].get("role") == "user" else history

    def _run(hist: List[Dict[str, Any]], sys_hint: str) -> Dict[str, Any]:
        leaks: List[str] = []
        n_ok = 0
        for _ in range(max(1, int(rounds))):
            try:
                reply = str(generate_fn(last_in, hist, sys_hint) or "")
            except Exception as ex:  # noqa: BLE001
                reply = f"<error {ex!r}>"
            if _starts_with_tag(reply):
                leaks.append(reply[:60])
            else:
                n_ok += 1
        n = max(1, int(rounds))
        return {"rounds": n, "leaks": len(leaks), "rate": round(len(leaks) / n, 3),
                "samples": leaks[:3]}

    cur = _run(hist_ctx, hint)
    ctrl = _run(_legacy_labelled(hist_ctx), "") if with_control else None
    errors: List[Dict[str, Any]] = []
    if cur["leaks"]:
        errors.append({"layer": "imitation", "text": "现口径 10 轮语音历史",
                       "got": cur["samples"], "note": "格式诱导",
                       "why": f"{cur['leaks']}/{cur['rounds']} 次回复以方括号标签开头"})
    return {
        "available": True,
        "summary": {"rounds": cur["rounds"], "current": cur, "control": ctrl},
        "errors": errors,
        "passed": not cur["leaks"],
    }


def evaluate_label_leak(generate_fn: Optional[GenerateFn] = None, *, rounds: int = 4) -> Dict[str, Any]:
    """两轨合一：确定性轨必过；诱导轨有 generate_fn 才跑。"""
    det = evaluate_label_leak_deterministic()
    imi = evaluate_label_imitation(generate_fn, rounds=rounds)
    return {
        "deterministic": det,
        "imitation": imi,
        "passed": bool(det["passed"] and imi["passed"]),
    }


def format_label_leak_report(report: Dict[str, Any]) -> str:
    det = report["deterministic"]
    imi = report["imitation"]
    m = det["summary"]
    lines = [
        "=== 系统标签泄漏评测（2026-09-12「[我方语音消息]」） ===",
        f"确定性轨: 样本 {m['total']}  合格 {m['correct']}  "
        f"{'[PASS]' if det['passed'] else '[FAIL]'}",
    ]
    for e in det["errors"][:20]:
        lines.append(f"  - [{e['layer']}] 「{e['text']}」→「{e['got']}」 {e['why']}（{e['note']}）")
    if not imi.get("available"):
        lines.append("格式诱导轨: 未跑（需 EVAL_LLM=1 或注入 generate_fn）")
    else:
        cur = imi["summary"]["current"]
        ctrl = imi["summary"].get("control")
        lines.append(
            f"格式诱导轨: 现口径 {cur['leaks']}/{cur['rounds']} 次以标签开头"
            f"{'（对照·事故口径 %d/%d）' % (ctrl['leaks'], ctrl['rounds']) if ctrl else ''}  "
            f"{'[PASS]' if imi['passed'] else '[FAIL]'}")
        for s in cur.get("samples") or []:
            lines.append(f"  - 泄漏样本: {s!r}")
    lines.append("总判: " + ("[PASS]" if report["passed"] else "[FAIL]"))
    return "\n".join(lines)


__all__ = [
    "GOLDEN_SAMPLES", "LabelSample", "evaluate_label_imitation",
    "evaluate_label_leak", "evaluate_label_leak_deterministic", "format_label_leak_report",
]
