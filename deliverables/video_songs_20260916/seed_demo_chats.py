#!/usr/bin/env python3
"""给教程录屏铺真实聊天内容：向白名单演示会话真发功能介绍消息。

  python seed_demo_chats.py --episode all          # 清空 BOUNDLESS 后铺 E1–E12
  python seed_demo_chats.py --episode E3           # 只铺一集
  python seed_demo_chats.py --thread ZH_DEMO --episode E3
  python seed_demo_chats.py --list
  python seed_demo_chats.py --history-only

只打 DEMO_THREADS 里登记的 conversation；英文会话 skip_translate，避免 lang_mismatch。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from chatx_session import BASE, token

# 录屏专用演示会话（真账号、真发）。
# BOUNDLESS：与 Jie 的英文客服演示（主画面）
# ZH_DEMO：本机「智聊支持」自聊，中文/多语互译演示（不打扰真人客户）
DEMO_THREADS = {
    "BOUNDLESS": {
        "platform": "telegram",
        "account_id": "8244899900",
        "chat_key": "8506426282",
        "conversation_id": "telegram:8244899900:8506426282",
        "lang": "en",
        "label": "BOUNDLESS",
    },
    "ZH_DEMO": {
        "platform": "telegram",
        "account_id": "6834964252",
        "chat_key": "me",
        "conversation_id": "telegram:6834964252:me",
        "lang": "zh",
        "label": "智聊支持",
    },
}

# 每集一组「像真客服往来」的短句；录屏时画面滚到这些近期消息。
EPISODE_SCRIPTS: dict[str, dict[str, list[str]]] = {
    "E1": {
        "BOUNDLESS": [
            "Hey Jie — starting a ChatX product tour. Unified inbox first.",
            "Left list = every TG / WhatsApp / LINE / Messenger thread.",
            "Top tabs switch which account I'm working as.",
            "Filters: private, groups, unread — urgent ones float up.",
            "Open a thread, reply below; Copilot sits on the right.",
        ],
        "ZH_DEMO": [
            "【教程·收件箱】左边是所有平台会话，顶栏切账号。",
            "筛选：私聊 / 群组 / 未读 / 需人工——先处理该看的。",
        ],
    },
    "E2": {
        "BOUNDLESS": [
            "Channel tip: add Telegram / WhatsApp / LINE / Messenger from the account tabs.",
            "Green light = healthy; yellow/red means reconnect or re-scan.",
            "One workspace, four platforms — no hopping between apps.",
        ],
        "ZH_DEMO": [
            "【教程·渠道】顶栏「＋新增」扫码接入四平台。",
            "绿灯在线，黄/红灯要留意重连。",
        ],
    },
    "E3": {
        "BOUNDLESS": [
            "Hi — can you help me read customer messages in English?",
            "ChatX translation tip: one tap inbound translate, outbound auto to customer language.",
            "I write in Chinese; the customer should read in English.",
            "Standard translate stays free — that is the mutual translate chapter.",
        ],
        "ZH_DEMO": [
            "【教程·互译】客户英文，点翻译就能看懂。",
            "出站：中文写，自动译成客户母语再发。",
            "Please reply in English — this is a translation demo.",
        ],
    },
    "E4": {
        "BOUNDLESS": [
            "Three modes on this thread: Manual / Semi-auto (draft for review) / Full auto.",
            "Adopt a draft with one click, or tap Takeover anytime.",
            "AI steps aside when a human agent is needed — you stay in control.",
        ],
        "ZH_DEMO": [
            "【教程·档位】手动 / 半自动拟稿 / 全自动值守。",
            "一键采用草稿；随时「接管」人工。",
        ],
    },
    "E5": {
        "BOUNDLESS": [
            "Personas: tone, boundaries, background — build once, bind to threads.",
            "YAML import works; try-chat before you go live.",
        ],
        "ZH_DEMO": [
            "【教程·人设】人设工作室：语气、边界、背景一次配好。",
            "试聊通过再绑到会话，避免上线翻车。",
        ],
    },
    "E6": {
        "BOUNDLESS": [
            "Knowledge base: drop FAQ / docs in — answers cite what they hit.",
            "Less hallucinating price or policy; more grounded replies.",
        ],
        "ZH_DEMO": [
            "【教程·知识库】FAQ / 文档导入，回答带依据。",
            "价格政策有出处，少编少猜。",
        ],
    },
    "E7": {
        "BOUNDLESS": [
            "Voice clone: reference audio → generate → preview → send as the persona.",
            "What you hear is what the customer gets.",
        ],
        "ZH_DEMO": [
            "【教程·克隆语音】工具箱·语音：试听通过＝所听即所发。",
        ],
    },
    "E8": {
        "BOUNDLESS": [
            "Goals + daily pulse: next-step hero card, then a 10-second check-in.",
            "Keeps follow-ups honest without a long CRM form.",
        ],
        "ZH_DEMO": [
            "【教程·今日拍】客户关系里设工作目标，每天十秒反馈。",
        ],
    },
    "E9": {
        "BOUNDLESS": [
            "Memory + care schedule: remembers preferences, respects quiet hours.",
            "Proactive care without spamming at 3am.",
        ],
        "ZH_DEMO": [
            "【教程·记忆关怀】记得偏好，也遵守安静时段。",
        ],
    },
    "E10": {
        "BOUNDLESS": [
            "Stuck? Tap Xiaozhi (bottom-right) — ask how-to or 'take me there'.",
            "In-product guide beats digging through docs.",
        ],
        "ZH_DEMO": [
            "【教程·小智】右下角小球：问怎么用、带我去、报障。",
        ],
    },
    "E11": {
        "BOUNDLESS": [
            "Guardrails: risk tags on threads; high-risk gets blocked or handed to a human.",
            "Opt-out and rate limits are respected — AI should not guess past the line.",
        ],
        "ZH_DEMO": [
            "【教程·护栏】风险标签；该转人就不乱答。",
        ],
    },
    "E12": {
        "BOUNDLESS": [
            "Billing: start free — standard translate stays free; AI usage is metered.",
            "Newcomer pack 6U, then top up what you use. No seat tax for basics.",
        ],
        "ZH_DEMO": [
            "【教程·额度】免费开始；标准翻译永久免费；用多少充多少。",
        ],
    },
    # 客户关系养成（公域场景片 R*）——偏好上下文，禁恋爱向措辞
    "R1": {
        "ZH_DEMO": [
            "客：上次那个雾霾蓝还有货吗？常用尺码 M。",
            "店：有的，同色腰带也到了。要帮您留一件 M 吗？",
            "客：先记着，这周忙完再说——别催单。",
            "店：好，偏好和尺码都记下了。您开口我们再跟进。",
        ],
    },
    "R2": {
        "ZH_DEMO": [
            "店：您的包裹已到站点，签收后有问题随时说。",
            "店：这条物流提醒可用克隆声试听，听完再发更亲切。",
        ],
    },
    "R3": {
        "ZH_DEMO": [
            "店：您好，我是本店店长助理。报价清楚、不夸大，有问题直接问我。",
            "店：人设试聊通过后，再绑到本会话，老客听着才不跳戏。",
        ],
    },
    # R4 客户养成：定工作目标 → AI 分天推进 → 跟进 SOP 工作链（不写「说停就停」）
    "R4": {
        "ZH_DEMO": [
            "客：上次说的那批样品，什么时候能寄出？",
            "店：本周三发出。我给您排个跟进计划：发货 → 签收 → 试用反馈，三步走。",
            "客：好，签收后我试用一周再看。",
            "店：没问题。到货那天提醒您一次，一周后再来听您的反馈。",
        ],
    },
    "C1": {
        "BOUNDLESS": [
            "Hi — do you ship the haze-blue size M to Dubai? Need a quote tonight.",
            "ChatX tip: inbound translate + draft in the customer's language.",
            "I sleep; the inquiry still gets a human-like reply.",
        ],
        "ZH_DEMO": [
            "【跨境·询盘】凌晨外语询盘：先翻译，再母语拟稿。",
        ],
    },
}

COMMON_CLOSER = {
    "BOUNDLESS": "Thanks — ChatX tutorial demo on this BOUNDLESS thread ({ep}). Real product, real send.",
    "ZH_DEMO": "【教程收尾·{ep}】本会话仅用于智聊功能演示录屏，真发真显。",
}


def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        # 近重 / 频控：软跳过，继续铺后续句子
        if e.code in (409, 429):
            return {"ok": False, "soft": True, "code": e.code, "detail": raw[:300]}
        raise SystemExit(f"[FAIL] {method} {path} → {e.code} {raw[:400]}") from e


def pin_thread(thread: dict, pinned: bool = True) -> None:
    d = _req(
        "POST",
        "/api/unified-inbox/conversations/pin",
        {"conversation_id": thread["conversation_id"], "pinned": pinned},
    )
    print(f"  pin={pinned} → {d.get('ok')} {thread['conversation_id']}")


def clear_thread(thread: dict) -> None:
    """仅清空工作台本地记录（both_sides=False），对方设备不动。"""
    d = _req(
        "POST",
        "/api/unified-inbox/conversations/clear",
        {"conversation_id": thread["conversation_id"], "both_sides": False},
    )
    print(f"  cleared={d.get('cleared')} {thread['conversation_id']}")


def send_lines(thread: dict, lines: list[str]) -> None:
    skip_tr = thread.get("lang") == "en"
    for i, text in enumerate(lines):
        payload = {
            "platform": thread["platform"],
            "account_id": thread["account_id"],
            "chat_key": thread["chat_key"],
            "text": text,
            "skip_translate": skip_tr,
            "client_msg_id": f"tut-{thread['chat_key']}-{int(time.time())}-{i}",
        }
        d = _req("POST", "/api/unified-inbox/send", payload)
        if d.get("soft"):
            print(f"  ~ soft skip [{d.get('code')}]: {text[:60]}")
            time.sleep(2.2)
            continue
        print(f"  ✓ [{thread.get('label', '?')}] {text[:70]}")
        if not d.get("ok", True) and d.get("detail"):
            print("    !", d.get("detail"))
        time.sleep(1.35)


def show_history(thread: dict, limit: int = 12) -> None:
    q = urllib.parse.urlencode({"conversation_id": thread["conversation_id"], "limit": limit})
    d = _req("GET", "/api/unified-inbox/history?" + q)
    print(f"-- history {thread['conversation_id']} found={d.get('found')} count={d.get('count')}")
    for m in (d.get("messages") or [])[-limit:]:
        direction = m.get("direction") or m.get("role") or "?"
        text = (m.get("text") or m.get("content") or "").replace("\n", " ")
        print(f"  {direction:3} | {text[:110]}")


def seed_episode(ep: str, thread_key: str | None, *, closer: bool = True) -> None:
    keys = [thread_key] if thread_key else list(DEMO_THREADS.keys())
    for k in keys:
        th = DEMO_THREADS[k]
        lines = list(EPISODE_SCRIPTS.get(ep, {}).get(k, []))
        closer_tpl = COMMON_CLOSER.get(k) if closer else None
        if closer_tpl:
            lines.append(closer_tpl.format(ep=ep))
        if not lines:
            print(f"  ~ skip {k}: no script for {ep}")
            continue
        print(f"== seed {ep} → {k} ({th['conversation_id']})")
        send_lines(th, lines)
        time.sleep(1.2)
        show_history(th, limit=8)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thread", default="", help="BOUNDLESS / ZH_DEMO；空=两个都铺")
    ap.add_argument("--episode", default="E1", help="E1…E12 或 all")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--history-only", action="store_true")
    ap.add_argument("--clear", action="store_true", help="铺之前清空工作台本地记录")
    ap.add_argument("--pin", action="store_true", help="置顶演示会话")
    ap.add_argument("--no-closer", action="store_true",
                    help="不追加「教程收尾」元信息句（场景片要像真对话；ZH_DEMO 是自聊会话，不涉真人客户）")
    a = ap.parse_args()
    if a.list:
        print(json.dumps(DEMO_THREADS, ensure_ascii=False, indent=2))
        return 0

    targets = [a.thread] if a.thread else list(DEMO_THREADS.keys())
    for k in targets:
        if k not in DEMO_THREADS:
            raise SystemExit(f"unknown thread {k}")
        th = DEMO_THREADS[k]
        if a.pin:
            pin_thread(th, True)
        if a.history_only:
            show_history(th)
            continue
        if a.clear:
            clear_thread(th)

    if a.history_only:
        return 0

    eps = list(EPISODE_SCRIPTS.keys()) if a.episode.lower() == "all" else [a.episode]
    for ep in eps:
        seed_episode(ep, a.thread or None, closer=not a.no_closer)

    meta = Path(__file__).resolve().parent / "out" / "demo_threads.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(DEMO_THREADS, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
