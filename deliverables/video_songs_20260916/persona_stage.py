#!/usr/bin/env python3
"""P 系列两方真会话舞台（客户侧账号真发外语来信，卖家视角录屏）。

  python persona_stage.py --stage TG_ES --probe               # 探针：两侧账号在线 + 会话存在
  python persona_stage.py --stage TG_ES --clear               # 清空卖家侧本地记录（对方设备不动）
  python persona_stage.py --stage TG_ES --customer-send "Hola, ¿tienen envío a Chile?"
  python persona_stage.py --stage TG_ES --history 8

设计：ChatX 本机同时挂着两个同平台账号（2026-09-17 探针实锤：TG 智聊支持/Katie、WA Wisley/calixa），
客户侧账号经 /api/unified-inbox/send 发出 → 卖家侧账号入站 direction=in，气泡在左、异色、外语——
不需要新账号，也不打扰真人客户。录制时由 record_persona 在步骤中调用 customer_send()，观众看到消息到达。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from chatx_session import BASE, token  # noqa: E402

SC = json.loads((ROOT / "scenarios_persona.json").read_text(encoding="utf-8"))
STAGES: dict[str, dict] = SC["stages"]


def _req(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        if e.code in (409, 429):
            return {"ok": False, "soft": True, "code": e.code, "detail": raw[:300]}
        raise RuntimeError(f"{method} {path} → {e.code} {raw[:300]}") from e


def stage(name: str) -> dict:
    if name not in STAGES:
        raise SystemExit(f"unknown stage {name}; have {list(STAGES)}")
    return STAGES[name]


def is_ingest_stage(st: dict) -> bool:
    """客户侧不是账号而是入站桥注入（F2 微信舞台：微信没有第二个可编程账号）。"""
    return str(st.get("customer_mode") or "") == "desktop_ingest"


def probe(st: dict) -> dict:
    acc = {(a["platform"], a["account_id"]): a for a in _req("GET", "/api/accounts").get("accounts", [])}
    seller = acc.get((st["platform"], st["seller_account"]))
    cust = acc.get((st["platform"], st["customer_account"]))
    out = {
        "seller_online": bool(seller and seller.get("status") == "online"),
        # 注入舞台没有客户账号：入站桥本身就是「客户侧」，卖家在线即两侧齐
        "customer_online": True if is_ingest_stage(st) else bool(cust and cust.get("status") == "online"),
        "seller_name": (seller or {}).get("self_name") or (seller or {}).get("label"),
        "customer_name": st.get("customer_display") if is_ingest_stage(st) else (cust or {}).get("self_name"),
    }
    h = history(st, 1)
    out["conversation_found"] = bool(h.get("found"))
    out["count"] = h.get("count")
    return out


def history(st: dict, limit: int = 8) -> dict:
    q = urllib.parse.urlencode({"conversation_id": st["seller_view_conversation"], "limit": limit})
    return _req("GET", "/api/unified-inbox/history?" + q)


def clear_seller_view(st: dict, *, both_sides: bool = True) -> dict:
    """清舞台会话。两侧都是自家账号，默认 both_sides=True（远端也删），否则平台 sync-history 会把上一集的
    消息拉回来串场（2026-09-17 P4 里出现 P1 的「智利七到十天送达」实锤）。清后复核 history 计数。
    群舞台禁止远端清（会删所有群成员记录）——只清工作台本地。注入舞台没有远端（平台侧本无此会话）。"""
    if st.get("chat_type") == "group" or is_ingest_stage(st):
        both_sides = False
    d = _req("POST", "/api/unified-inbox/conversations/clear",
             {"conversation_id": st["seller_view_conversation"], "both_sides": both_sides})
    if both_sides and not d.get("ok"):
        # 远端删失败（TG PEER_ID_INVALID 偶发 / WA remote_unsupported_platform）→ 至少把本地清干净，画面不串场
        d2 = _req("POST", "/api/unified-inbox/conversations/clear",
                  {"conversation_id": st["seller_view_conversation"], "both_sides": False})
        d = {**d2, "remote_error": d.get("reason") or d.get("error")}
    time.sleep(1.5)
    d["thread_left"] = thread_count(st)   # 工作台画面读的是 thread（history 是归档店，清不掉也不入镜）
    return d


def set_automation_mode(st: dict, mode: str = "review") -> dict:
    """把舞台卖家视角会话的 AI 档位归位（缺省 review＝AI 出草稿我审）。

    上一集的 type_send / mode_switch 会把会话留在「人工模式 · AI 不拟稿不发」+「AI 已让位…30 分钟后接回」
    横幅（2026-09-17 首批 P1/P3 成片顶栏实锤），下一集讲「AI 拟稿」时画面自相矛盾。清舞台时顺手归位。
    群舞台不动（群上档位另有确认闸，且 P6 不讲档位）。"""
    if st.get("chat_type") == "group":
        return {"ok": True, "skipped": "group"}
    try:
        d = _req("POST", "/api/unified-inbox/automation",
                 {"platform": st["platform"], "account_id": st["seller_account"],
                  "chat_key": st["customer_account"], "mode": mode}, timeout=60)
        return {"ok": bool(d.get("ok", True)), "mode": mode, "cancelled": d.get("cancelled")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "mode": mode, "error": str(e)[:120]}


def set_outbound_lang(st: dict, lang: str) -> dict:
    """把舞台卖家视角会话的「发→X」出站语言钉成剧本的客户语言（服务端会话级事实源）。

    不钉（auto）时目标语靠 conversations.language 推断，而它由入站检测投票：短西语句
    「Perfecto. ¿Cuánto tarda el envío y aceptan tarjeta?」被判 en → 中文回复译成英文发给
    西语客户（2026-09-18 P1 重录 take1 实锤）。剧本里客户语言是已知量，钉死比猜稳。
    群舞台不钉（群里多语种）。lang 空 → no-op。"""
    if not lang or st.get("chat_type") == "group":
        return {"ok": True, "skipped": "group" if lang else "no_lang"}
    try:
        d = _req("POST", "/api/unified-inbox/conv-xlate-out",
                 {"platform": st["platform"], "account_id": st["seller_account"],
                  "chat_key": st["customer_account"], "lang": lang, "source": "api"}, timeout=30)
        return {"ok": bool(d.get("ok")), "lang": d.get("lang", lang), "prev": d.get("prev"),
                **({"error": d.get("error")} if d.get("error") else {})}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "lang": lang, "error": str(e)[:120]}


def end_takeover(st: dict) -> dict:
    """交还接管（/api/takeover/end）：F1 教学片点过会话头【接管】→ 会话进 takeover 表 + manual + 标签，
    不交还的话下一次 set_automation_mode 切回去了标签还挂着、接管按钮还是「交回」。
    409 not_active = 本来就没接管，算 ok。群舞台不动。"""
    if st.get("chat_type") == "group":
        return {"ok": True, "skipped": "group"}
    try:
        d = _req("POST", "/api/takeover/end", {"conversation_id": st["seller_view_conversation"]}, timeout=30)
        if d.get("soft") and d.get("code") == 409:
            return {"ok": True, "was_active": False}
        return {"ok": bool(d.get("ok")), "was_active": True, "restored_mode": d.get("restored_mode"),
                "duration_sec": d.get("duration_sec")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}


def trim_thread_tail(st: dict, n: int) -> dict:
    """工作台软删舞台会话最后 n 条消息（/api/unified-inbox/messages/delete，仅本地不动远端）。

    单段补录用：F1 g_takeover 一次 take 往线程里追加了「批发询价 + 人工回复」两条，整段重录前把它们
    从卖家视角摘掉，否则画面里同一句询价出现两遍；不清整段（e_live 的自动出站要保留给接管段做前情）。
    群舞台不动。"""
    if st.get("chat_type") == "group" or n <= 0:
        return {"ok": True, "skipped": "group" if n > 0 else "n<=0"}
    msgs = thread_messages(st, limit=max(20, n + 5))
    tail = msgs[-n:] if len(msgs) >= n else msgs
    ids = [str(m.get("id") or m.get("message_id") or m.get("mid") or "") for m in tail]
    ids = [i for i in ids if i]
    if not ids:
        return {"ok": False, "error": "no message ids in thread tail", "sample": str(tail[:1])[:160]}
    d = _req("POST", "/api/unified-inbox/messages/delete",
             {"conversation_id": st["seller_view_conversation"], "message_ids": ids}, timeout=30)
    time.sleep(1.0)
    return {"ok": bool(d.get("ok")), "deleted": d.get("deleted"), "asked": len(ids), "thread_left": thread_count(st)}


def thread_count(st: dict) -> int:
    q = urllib.parse.urlencode({"platform": st["platform"], "account_id": st["seller_account"],
                                "chat_key": st.get("group_chat_key") or st["customer_account"], "limit": 20, "history": 1})
    try:
        return len(_req("GET", "/api/unified-inbox/thread?" + q).get("messages") or [])
    except Exception:
        return -1


def thread_messages(st: dict, limit: int = 20) -> list:
    q = urllib.parse.urlencode({"platform": st["platform"], "account_id": st["seller_account"],
                                "chat_key": st.get("group_chat_key") or st["customer_account"],
                                "limit": limit, "history": 1})
    try:
        return list(_req("GET", "/api/unified-inbox/thread?" + q).get("messages") or [])
    except Exception:
        return []


def customer_send(st: dict, text: str, *, tag: str = "p") -> dict:
    """客户侧账号 → 卖家账号 真发；skip_translate 保证原文外语落到卖家收件箱。
    注入舞台（customer_mode=desktop_ingest）改走 ingest_customer_message。"""
    if is_ingest_stage(st):
        return ingest_customer_message(st, text)
    payload = {
        "platform": st["platform"],
        "account_id": st["customer_account"],
        "chat_key": st.get("group_chat_key") or st["seller_account"],
        "text": text,
        "skip_translate": True,
        # 客户侧是排演账号：同一剧本重录会撞出站近重复守卫（3 分钟窗 409 near_duplicate）→ 显式强发
        "force_dup": True,
        "client_msg_id": f"{tag}-{int(time.time() * 1000)}",
    }
    last: Exception | None = None
    for attempt in range(3):
        try:
            return _req("POST", "/api/unified-inbox/send", payload, timeout=90)
        except RuntimeError as e:
            # 503「WhatsApp 服务未启用」= 通道短暂重连（2026-09-18 P2 四录实锤，探针同刻却是 online）→ 等一下再试
            last = e
            if "503" not in str(e) and "502" not in str(e):
                raise
            time.sleep(6.0)
    raise last  # type: ignore[misc]


def wait_inbound(st: dict, needle: str, timeout_s: float = 30.0, *, after_ts: float | None = None) -> bool:
    """轮询卖家侧 **thread**（工作台画面读的是 thread，history 是归档店，群清本地清不掉归档）。
    after_ts：只认这个时刻之后的入站，避免上一轮同文案假阳性（2026-09-18 P6 首录实锤）。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        for m in thread_messages(st):
            if m.get("direction") != "in":
                continue
            if needle[:24] not in (m.get("text") or ""):
                continue
            if after_ts is not None and float(m.get("ts") or 0) < after_ts - 2:
                continue
            return True
        time.sleep(1.0)
    return False


def ingest_customer_message(st: dict, text: str) -> dict:
    """F2 微信舞台的「客户来信」：走副驾同一条入站桥 ``POST /api/desktop/ingest``。

    真副驾（UIA 读屏）读到微信新消息后也是打这条路进 ``ingest_incoming``——落库、未读、SSE、
    拟稿触发与真来信完全同一条链；被替代的只有「客户在另一台手机上打字」。
    桌面路径强制丢客户端 msg_id、按 hash(text|ts) 去重 → 同文重录要换 ts（这里 ts=now 天然不同）。"""
    body = {
        "platform": st["platform"], "account_id": st["seller_account"],
        "chat_key": st["customer_account"], "name": st.get("customer_display") or st["customer_account"],
        "text": text, "ts": time.time(), "direction": "in",
    }
    d = _req("POST", "/api/desktop/ingest", body, timeout=30)
    return {"ok": bool(d.get("ok")), "delivered": bool(d.get("ok")), "conversation_id": d.get("conversation_id"),
            "via": "desktop_ingest"}


def delete_stage_conversation(st: dict) -> dict:
    """ephemeral 舞台（F2 演示客户）录完整条硬删：老板收件箱不留假客户「小林」。
    非 ephemeral 舞台拒绝（TG/WA 舞台是真会话，只许 clear）。"""
    if not st.get("ephemeral"):
        return {"ok": False, "skipped": "not_ephemeral"}
    try:
        d = _req("POST", "/api/unified-inbox/conversations/delete",
                 {"conversation_id": st["seller_view_conversation"]}, timeout=30)
        return {"ok": bool(d.get("ok", True)), "deleted": d.get("deleted")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}


# ── 微信 PC 副驾档位（F2 引导页第 2 步会真点「保存」，录完必须还原老板的档位）──────────────
WX_POLICY_KEYS = ("tier", "work_hours", "risk_ack")


def wx_policy_get() -> dict:
    return _req("GET", "/api/setup/wechat_pc/policy", None, timeout=20)


def wx_policy_set(tier: str, work_hours: list | None = None, risk_ack: bool | None = None) -> dict:
    body: dict = {"tier": tier}
    if work_hours:
        body["work_hours"] = [int(work_hours[0]), int(work_hours[1])]
    if risk_ack:
        body["risk_ack"] = True
    return _req("POST", "/api/setup/wechat_pc/policy", body, timeout=30)


def wx_policy_restore(snapshot: dict | None) -> dict:
    """把档位还原到录制前快照（tier / work_hours；auto_reply 需 risk_ack——快照里已确认过的，
    服务端按 current.risk_ack 放行，不必再点知情同意）。快照空 / 已一致 → no-op。"""
    if not snapshot or not snapshot.get("tier"):
        return {"ok": True, "skipped": "no_snapshot"}
    try:
        cur = wx_policy_get()
        if cur.get("tier") == snapshot.get("tier") and list(cur.get("work_hours") or []) == list(snapshot.get("work_hours") or []):
            return {"ok": True, "skipped": "already", "tier": cur.get("tier")}
        d = wx_policy_set(snapshot["tier"], snapshot.get("work_hours"), bool(snapshot.get("risk_ack")))
        return {"ok": bool(d.get("ok")) and d.get("tier") == snapshot["tier"], "tier": d.get("tier"),
                "work_hours": d.get("work_hours"), "from": cur.get("tier")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}


def wx_env() -> dict:
    return _req("GET", "/api/setup/wechat_pc/env", None, timeout=30)


def wx_copilot_status() -> dict:
    return _req("GET", "/api/setup/wechat_pc/copilot/status", None, timeout=30)


def wx_green(env: dict | None = None, status: dict | None = None) -> tuple[bool, list[str]]:
    """F2 录制绿灯：电脑微信在跑、主窗口可见、已登录、版本 ≥4、驱动可用；副驾状态机 online（不是 blind/offline）。
    红灯不录——老板把微信收进托盘 / 兄弟线重启副驾时，引导页会显示黄色告警，录进成片就是坏 take。"""
    env = env or wx_env()
    status = status or wx_copilot_status()
    why: list[str] = []
    for k in ("running", "main_window", "version_ok", "installed"):
        if not env.get(k):
            why.append(f"env.{k}=False")
    if not (env.get("driver") or {}).get("ok", True):
        why.append("driver.ok=False")
    cp = env.get("copilot") or {}
    if cp and cp.get("alive") is False:
        why.append("copilot.alive=False")
    if str(status.get("state") or "") != "online":
        why.append(f"copilot.state={status.get('state')}")
    return (not why), why


def pull_group_latest(st: dict) -> dict:
    """群消息实时入站会被「未触发不处理」闸掉（工作台看不到刚发进群的询价）。
    先试 from_latest（从云端最新往回拉）；接口成功就停——即使 inserted=0（已在库里）。
    旧逻辑在 inserted=0 时改走账号级 sync-history，会把各会话里同一句 USB-C 询价再灌一遍
    （2026-09-18 重启后群里「消息都一样」实锤）。"""
    acct = st["seller_account"]
    ck = st.get("group_chat_key") or ""
    if ck:
        try:
            d = _req("POST", f"/api/platforms/telegram/{acct}/history",
                     {"chat_key": ck, "count": 8, "from_latest": True}, timeout=40)
            if d.get("ok"):
                return {"ok": True, "via": "from_latest",
                        **{k: d.get(k) for k in ("inserted", "requested")}}
        except Exception as e:  # noqa: BLE001
            d = {"ok": False, "error": str(e)[:120]}
    started = _req("POST", f"/api/platforms/telegram/{acct}/sync-history",
                   {"dialogs": 8, "per_chat": 4}, timeout=30)
    for _ in range(25):
        time.sleep(1.2)
        snap = _req("GET", f"/api/platforms/telegram/{acct}/sync-history")
        if snap.get("state") in ("done", "error"):
            return {"ok": snap.get("state") == "done", "via": "sync-history",
                    "started": started.get("started"), "state": snap.get("state")}
    return {"ok": False, "via": "sync-history", "state": "timeout"}


def seed_sla_demo(st: dict, spec: dict) -> list[dict]:
    """P3 概览用：给舞台卖家账号铺几条「客户来信、一直没人回」的演示会话。

    2026-09-17/18 两次 P3 成片实锤：概览段字幕说「超时的、需人工的先举手」，画面却是
    「没有符合当前筛选的对话」+「需人工」页签根本不出现——舞台会话录完都是我方最后一句
    （无 unanswered），其余等回复的会话挂在离线账号上（SLA 对 offline/removed 账号静默）。
    做法：走内部入站桥 ``/api/internal/protocol/ingest``（所有入站唯一落库口的 HTTP 面），
    ``direction=in`` + ``ts`` 回拨 ``age_min`` 分钟 + ``backfill=true``（不计未读、不发 SSE、
    不起草、不进自动回复——纯演示行，不烧 LLM）。列表 SLA 由服务端按 last_message_dirs 现算：
    ≥ inbox.sla_warn_sec(30min) 出「超时」chip，≥ sla_crit_sec(2h) 进「需人工」页签。

    chat_key = ``<row.key>-<run>``：非数字（TG 目录同步/头像解析碰不到，日志一眼认出是演示），
    且**每次录制换一个**——``drop_sla_demo`` 硬删会留防复活墓碑，墓碑只被「ts 晚于删除时刻」的
    真新消息解除，而演示行 ts 故意回拨几小时 → 同 key 重录第二次会被墓碑按「已删历史」静默吞掉
    （2026-09-18 首跑实锤：seed ok=True 但 chats 里没有）。墓碑行数＝录制次数×3，可忽略。
    返回逐行结果（含实际 cid），后续 visible/drop 都吃这个返回值。"""
    now = time.time()
    run = int(now) % 1_000_000
    out: list[dict] = []
    for row in spec.get("rows") or []:
        ts = now - float(row.get("age_min") or 0) * 60.0
        key = f"{row['key']}-{run}"
        cid = f"{st['platform']}:{st['seller_account']}:{key}"
        body = {
            "platform": st["platform"], "account_id": st["seller_account"],
            "chat_key": key, "name": row.get("name") or row["key"],
            "text": row["text"], "ts": ts, "msg_id": f"{key}-{int(ts)}",
            "direction": "in", "chat_type": "private", "backfill": True, "backfill_source": "persona_demo",
        }
        try:
            d = _req("POST", "/api/internal/protocol/ingest", body, timeout=30)
            out.append({"key": key, "cid": d.get("conversation_id") or cid, "ok": bool(d.get("ok")),
                        "age_min": row.get("age_min")})
        except Exception as e:  # noqa: BLE001
            out.append({"key": key, "cid": cid, "ok": False, "error": str(e)[:120]})
    return out


SLA_DEMO_SETTLE_SEC = 10.0   # 引擎 dormant_review.SETTLE_DEBOUNCE_SEC=8 → 回填静默 8s 后才结算，多等 2s


def settle_sla_demo(st: dict, seeded: list[dict], *, wait_s: float = SLA_DEMO_SETTLE_SEC) -> dict:
    """吃掉 backfill 铺行的两个副作用（2026-09-18 P3 首录实锤，成片里都露脸了）：

    引擎把 backfill 入站当「登录/重连历史同步」→ 静默 8s 后 settle_backfill：
      1. 最后一条是客户入站且未回 → 进「沉寂会话待你决定」清单 → 列表顶橙色横幅「有 N 个旧会话等你决定」；
         且该清单与会话表分库，conversations/delete 删不掉——三次试跑留下 9 条僵尸。
      2. 卖家账号已是全自动 → 登记 login_review → 工作台一进来就弹「已登录并同步完成——全自动会碰到谁？」
         两栏确认框，盖住整个画面，录制里点筛选页签点到的是它。
    处置：等 settle 落地 → 对我们的行做 ``manual`` 决定（出清单；只写会话级档位行，随会话一起删；
    行上出「人工」处理者角标，与「需人工先举手」同义，比 ``ignore`` 的「长期未回」标签贴题）→
    ack 该账号 pending 的 login_review。返回处置计数，任一步失败不抛（录制方看 pending_left 决定要不要录）。"""
    time.sleep(max(0.0, wait_s))
    want = {str(r.get("cid")) for r in seeded or [] if r.get("cid")}
    out = {"decided": 0, "acked": 0, "pending_left": -1, "login_pending_left": -1}
    try:
        snap = _req("GET", "/api/unified-inbox/dormant-review?limit=200", timeout=20)
    except Exception as e:  # noqa: BLE001
        return {**out, "error": str(e)[:120]}
    for it in snap.get("items") or []:
        cid = str(it.get("conversation_id") or "")
        if cid in want:
            try:
                d = _req("POST", "/api/unified-inbox/dormant-review/action",
                         {"conversation_id": cid, "action": "manual"}, timeout=20)
                out["decided"] += 1 if d.get("ok") else 0
            except Exception:  # noqa: BLE001
                pass
    acct = (st["platform"], st["seller_account"])
    for r in snap.get("login_reviews") or []:
        if (str(r.get("platform")), str(r.get("account_id"))) != acct or r.get("status", "pending") != "pending":
            continue
        try:
            d = _req("POST", "/api/unified-inbox/dormant-review/action",
                     {"login_review_id": int(r.get("id") or 0), "action": "ack"}, timeout=20)
            out["acked"] += 1 if d.get("ok", True) else 0
        except Exception:  # noqa: BLE001
            pass
    try:
        snap2 = _req("GET", "/api/unified-inbox/dormant-review?limit=200", timeout=20)
        out["pending_left"] = sum(1 for it in snap2.get("items") or [] if str(it.get("conversation_id")) in want)
        out["login_pending_left"] = sum(
            1 for r in snap2.get("login_reviews") or []
            if (str(r.get("platform")), str(r.get("account_id"))) == acct and r.get("status", "pending") == "pending")
    except Exception:  # noqa: BLE001
        pass
    return out


def drop_sla_demo(st: dict, seeded: list[dict]) -> list[dict]:
    """删 seed_sla_demo 铺的演示会话（本机工作台硬删 + 墓碑；平台侧本来就没有这条线）。
    先跑一遍 settle_sla_demo(wait_s=0) 把可能还挂着的沉寂清单项决定掉（清单与会话表分库，硬删不带走）。"""
    settle_sla_demo(st, seeded, wait_s=0)
    out: list[dict] = []
    for row in seeded or []:
        cid = row.get("cid")
        if not cid:
            continue
        try:
            d = _req("POST", "/api/unified-inbox/conversations/delete", {"conversation_id": cid}, timeout=30)
            out.append({"key": row.get("key"), "ok": bool(d.get("ok", True)), "rows": d.get("deleted") or d.get("rows")})
        except Exception as e:  # noqa: BLE001
            out.append({"key": row.get("key"), "ok": False, "error": str(e)[:120]})
    return out


def sla_demo_visible(st: dict, seeded: list[dict]) -> dict:
    """复核：铺下去的演示行是否真被服务端算成 SLA 超时（chats 接口 sla_level），别让字幕再压空画面。"""
    q = urllib.parse.urlencode({"platform": st["platform"], "account_id": st["seller_account"], "limit": 200})
    try:
        chats = _req("GET", "/api/unified-inbox/chats?" + q).get("chats") or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}
    want = {str(r.get("cid")) for r in seeded or [] if r.get("cid")}
    got = {str(c.get("conversation_id")): (c.get("sla_level") or "") for c in chats
           if str(c.get("conversation_id")) in want}
    return {"ok": bool(got) and all(got.values()) and not (want - set(got)),
            "levels": got, "missing": sorted(want - set(got))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--customer-send", default="")
    ap.add_argument("--history", type=int, default=0)
    ap.add_argument("--delete", action="store_true", help="ephemeral 舞台：整条硬删演示会话（F2 小林）")
    ap.add_argument("--wx-green", action="store_true", help="微信舞台录制绿灯（环境 + 副驾状态）")
    a = ap.parse_args()
    st = stage(a.stage)
    if a.wx_green:
        ok, why = wx_green()
        print(json.dumps({"green": ok, "why": why}, ensure_ascii=False))
        return 0 if ok else 1
    if a.probe:
        p = probe(st)
        print(json.dumps(p, ensure_ascii=False))
        return 0 if (p["seller_online"] and p["customer_online"]) else 1
    if a.clear:
        print(clear_seller_view(st))
    if a.delete:
        print(delete_stage_conversation(st))
    if a.customer_send:
        d = customer_send(st, a.customer_send, tag="cli")
        print("sent", d.get("ok"), d.get("delivered"), (d.get("detail") or "")[:120])
        print("landed:", wait_inbound(st, a.customer_send))
    if a.history:
        h = history(st, a.history)
        for m in (h.get("messages") or [])[-a.history:]:
            print(f"  {m.get('direction'):3} | {(m.get('text') or '')[:90]} | tr={(m.get('translated_text') or '')[:40]}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
