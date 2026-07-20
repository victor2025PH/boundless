# -*- coding: utf-8 -*-
"""坐席工作台视觉回归——场景截图器。

用固定 mock 数据 + 冻结时钟（pg.clock.set_fixed_time）拍摄确定性截图：
dark/light × [收件箱空态, 收件箱列表(mock), 聊天视图(mock), /workspace/dash]。

确定性手段（缺一都会导致连拍两次出现像素差）：
1. 时钟冻结：页面内 Date.now()/new Date() 固定为 BASE_TS（1700000000），
   mock 的 last_ts/消息 ts 均为 BASE_TS 的固定偏移 → “N分钟前/昨天/HH:MM”
   等相对时间文案恒定（timers 照常运行，页面加载流程不受影响）。
2. API 全拦截：/api/** 一律由本脚本 fulfill——
   - chats/thread/dashboard/me/presence 等给固定 mock；
   - SSE（/api/workspace/stream、/api/events）回 204 → EventSource 关闭，
     不会有实时事件把列表/铃铛/toast 弄成随机态；
   - 头像代理（/api/platforms/*/avatar）回 404 → 恒定回落渐变首字母头像；
   - 其余端点统一 {"ok": true} → 各面板恒定空态。
   页面 HTML/JS/CSS/静态资源仍来自真实 dev 实例（这正是要回归的对象）。
3. 禁用动画/过渡/光标闪烁：注入 *{animation:none;transition:none;
   caret-color:transparent}（含伪元素）。

用法：
    python capture.py [--out DIR] [--base-url URL] [--token TOKEN]

退出码：0=全部场景成功；2=部分场景跳过（dev 实例该页 500/超时等）；3=全部失败。
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "shots" / "current"
DEFAULT_BASE_URL = "http://127.0.0.1:18901"
DEFAULT_TOKEN = "dev-ui-check"

VIEWPORT = {"width": 1440, "height": 900}

# ── 固定基准时刻 ──────────────────────────────────────────────
# 页面时钟被冻结为该时刻；所有 mock 时间戳 = BASE_TS - 固定偏移。
BASE_TS = 1700000000  # 2023-11-14 22:13:20 UTC（本机 UTC+8 为 11-15 06:13:20）

FREEZE_CSS = (
    "*,*::before,*::after{animation:none!important;transition:none!important;"
    "caret-color:transparent!important}html{scroll-behavior:auto!important}"
    # toast 是「加载后 N 秒出现、8 秒自灭」的赛跑元素（如首次搜索模式提示），
    # 与截图时机竞态 → 整个容器隐藏（一次性提示另有 localStorage 预置兜底）。
    "#ws-toast-box{display:none!important}"
)

# 一次性“新手引导”状态全部预置为已读：这些提示（搜索模式 toast/按钮脉冲、
# 群组折叠区提示、看板上线引导）只在首次出现，属于时间赛跑元素。
SEEDED_LOCALSTORAGE = {
    "ws_search_mode_hint_seen": "1",
    "ws_group_hint_seen": "1",
    "hl_onboard_dismiss": "1",
}

# ── mock 数据（源自 _dev_ui_phase1/shot_mock.py，固定化） ──────────
# 前端列表预览读 last_text + last_direction（“你:”前缀由前端按 direction 加），
# last_msg 仅为兼容后端形状保留。
CHATS_ROWS = [
    {"platform": "telegram", "account_id": "default", "chat_key": "u1001",
     "conversation_id": "tg:u1001", "name": "钩 JUN", "chat_type": "private",
     "last_text": "好的，明天发货单我整理给您", "last_msg": "好的，明天发货单我整理给您",
     "last_direction": "out", "last_ts": BASE_TS - 240, "unread": 2,
     "unanswered_sec": 3900, "sla_level": "warn", "sla_breach": True,
     "conv_tags": ["意向客户"], "funnel_stage": "S2_ENGAGED"},
    {"platform": "telegram", "account_id": "default", "chat_key": "u1002",
     "conversation_id": "tg:u1002", "name": "中田TG", "chat_type": "private",
     "last_text": "請問這款有現貨嗎？", "last_msg": "請問這款有現貨嗎？",
     "last_direction": "in", "last_ts": BASE_TS - 1500, "unread": 5,
     "unanswered_sec": 9800, "sla_level": "crit", "sla_breach": True,
     "conv_tags": ["询价"], "funnel_stage": "S1_CONTACTED"},
    {"platform": "whatsapp", "account_id": "default", "chat_key": "u2001",
     "conversation_id": "wa:u2001", "name": "Maj", "chat_type": "private",
     "last_text": "Thanks! The sample arrived today.",
     "last_msg": "Thanks! The sample arrived today.",
     "last_direction": "in", "last_ts": BASE_TS - 4200, "unread": 0,
     "unanswered_sec": 0, "sla_level": "", "sla_breach": False,
     "conv_tags": ["成交"], "funnel_stage": "S4_CONVERTED"},
    {"platform": "line", "account_id": "default", "chat_key": "u3001",
     "conversation_id": "ln:u3001", "name": "访客 6294ad", "chat_type": "private",
     "last_text": "承知しました、また連絡します", "last_msg": "承知しました、また連絡します",
     "last_direction": "in", "last_ts": BASE_TS - 86000, "unread": 0,
     "unanswered_sec": 0, "sla_level": "", "sla_breach": False,
     "conv_tags": [], "funnel_stage": ""},
    {"platform": "messenger", "account_id": "default", "chat_key": "u4001",
     "conversation_id": "ms:u4001", "name": "访客 499055", "chat_type": "private",
     "last_text": "收到，稍后给您报价", "last_msg": "收到，稍后给您报价",
     "last_direction": "out", "last_ts": BASE_TS - 172000, "unread": 0,
     "unanswered_sec": 0, "sla_level": "", "sla_breach": False,
     "conv_tags": [], "funnel_stage": ""},
    {"platform": "telegram", "account_id": "default", "chat_key": "u1003",
     "conversation_id": "tg:u1003", "name": "访客 3aa578", "chat_type": "private",
     "last_text": "危机：情绪低落，需要关怀", "last_msg": "危机：情绪低落，需要关怀",
     "last_direction": "in", "last_ts": BASE_TS - 260000, "unread": 1,
     "unanswered_sec": 700, "sla_level": "", "sla_breach": False,
     "conv_tags": ["情绪低落"], "funnel_stage": "S1_CONTACTED"},
]
for _c in CHATS_ROWS:  # 公共默认列（与真实 API 形状对齐）
    _c.setdefault("account_label", "default")
    _c.setdefault("username", "")
    _c.setdefault("phone", "")
    _c.setdefault("avatar_url", "")
    _c.setdefault("can_send", True)
    _c.setdefault("read_only", False)
    _c.setdefault("archived", False)
    _c.setdefault("snooze_until", 0)
    _c.setdefault("mentioned", False)
    _c.setdefault("message_count", 4)

PLATFORM_STATUS = {
    "telegram": {"platform": "telegram", "account_id": "default",
                 "label": "Telegram", "running": True},
    "whatsapp": {"platform": "whatsapp", "account_id": "default",
                 "label": "WhatsApp", "running": True},
    "line": {"platform": "line", "account_id": "default",
             "label": "LINE", "running": False},
    "messenger": {"platform": "messenger", "account_id": "default",
                  "label": "Messenger", "running": True},
}

THREAD = {
    "ok": True,
    "has_more": False,
    "messages": [
        {"message_id": "m1", "direction": "in", "text": "請問這款有現貨嗎？",
         "ts": BASE_TS - 9800, "sender": "中田TG"},
        {"message_id": "m2", "direction": "in", "text": "需要 200 件，可以走海運",
         "ts": BASE_TS - 9700, "sender": "中田TG"},
        {"message_id": "m3", "direction": "out",
         "text": "您好！有现货的，200件海运约12天到港，今天可以给您排产。",
         "ts": BASE_TS - 9500, "sender": "me", "status": "sent"},
        {"message_id": "m4", "direction": "in", "text": "OK，請給我報價單",
         "ts": BASE_TS - 9000, "sender": "中田TG"},
    ],
}

ME = {"ok": True, "agent_id": "admin", "display_name": "admin",
      "role": "master", "is_supervisor": True, "license": None,
      "demo_mode": False}

_DAYS = ["11-09", "11-10", "11-11", "11-12", "11-13", "11-14", "11-15"]
DASHBOARD = {
    "ok": True,
    "due_tasks_mine": 2, "due_tasks": 5,
    "today": {"new_contacts": 4, "leads": 2, "handoffs": 1},
    "funnel": {"sessions": 12},
    "sla": {"waiting": 3, "breaching": 1, "critical": 1},
    "first_response": {"today_responded": 6, "today_avg_sec": 420,
                       "today_attain_rate": 83},
    "resolution": {"today_resolved": 2, "today_avg_sec": 5400},
    "stage_counts": {"S1_CONTACTED": 3, "S2_ENGAGED": 2, "S4_CONVERTED": 1},
    "trend": [{"day": d, "new_contacts": (i * 3) % 5, "leads": (i * 2) % 3,
               "conversions": i % 2} for i, d in enumerate(_DAYS)],
    "frt_trend": [{"day": d, "rate": 60 + (i * 7) % 30, "count": 3 + i % 4}
                  for i, d in enumerate(_DAYS)],
    "res_trend": [{"day": d, "avg_min": 30 + (i * 11) % 50, "count": 1 + i % 3}
                  for i, d in enumerate(_DAYS)],
    "translation": {}, "translation_inbound": {}, "auto_claim": {},
    "agent_frt": [{"agent_id": "agent-a", "agent_name": "客服A",
                   "responded": 5, "avg_sec": 300, "attain_rate": 80}],
    "sla_by_agent": [
        {"agent_id": "agent-a", "agent_name": "客服A",
         "waiting": 2, "breaching": 1, "critical": 1},
        {"agent_id": "", "agent_name": "",
         "waiting": 1, "breaching": 0, "critical": 0},
    ],
    "agent_load": [
        {"assignee": "客服A", "open": 3, "overdue": 1},
        {"assignee": "客服B", "open": 1, "overdue": 0},
    ],
}

PRESENCE = {"ok": True, "agents": [
    {"agent_id": "admin", "display_name": "admin", "status": "online",
     "last_seen_at": BASE_TS - 30},
    {"agent_id": "agent-a", "display_name": "客服A", "status": "away",
     "last_seen_at": BASE_TS - 900},
]}

MY_PERF = {"ok": True,
           "perf": {"total": 12, "approved": 9, "rejected": 1, "avg_csat": 4.6},
           "timeline": [{"day": d, "total": 1 + (i * 2) % 4,
                         "approved": i % 3} for i, d in enumerate(_DAYS)],
           "rank": 1, "total_agents": 3}

WORKLOAD = {"ok": True, "total_agents": 2, "max_cap": 8, "overloaded_count": 0,
            "lightest_agent": "agent-a",
            "workloads": [
                {"agent_id": "admin", "status": "online", "active_convs": 3,
                 "recent_actions": 5, "overloaded": False},
                {"agent_id": "agent-a", "status": "away", "active_convs": 1,
                 "recent_actions": 2, "overloaded": False},
            ]}

ESCALATIONS = {"ok": True, "items": [], "count": 0, "today_count": 0}
CHECKLIST = {"ok": True, "light": "green", "checks": []}
RISK_SUMMARY = {"ok": True, "total_pending": 0,
                "by_level": {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0}}
GENERIC_OK = {"ok": True}


def _chats_payload(empty):
    return {"ok": True, "ts": BASE_TS,
            "chats": [] if empty else CHATS_ROWS,
            "platform_status": PLATFORM_STATUS}


def make_api_handler(*, empty_inbox):
    """构造 context 级 /api/** 拦截器：白名单固定 mock，其余 {"ok":true}。"""

    def _json(route, payload, status=200):
        route.fulfill(status=status,
                      content_type="application/json; charset=utf-8",
                      body=json.dumps(payload, ensure_ascii=False))

    def handler(route):
        path = route.request.url.split("?", 1)[0]
        # SSE：204 → EventSource 直接关闭（不重连、无事件洪水）
        if (path.endswith("/api/workspace/stream") or path.endswith("/api/events")
                or path.endswith("-stream") or path.endswith("/log-stream")):
            route.fulfill(status=204, body="")
            return
        # 头像代理：404 → <img onerror> 移除，恒定回落渐变首字母头像
        if "/avatar" in path:
            _json(route, {"ok": False}, status=404)
            return
        if path.endswith("/api/unified-inbox/chats"):
            _json(route, _chats_payload(empty_inbox))
            return
        if path.endswith("/api/unified-inbox/thread"):
            _json(route, THREAD)
            return
        if path.endswith("/api/workspace/me"):
            _json(route, ME)
            return
        if path.endswith("/api/workspace/dashboard"):
            _json(route, DASHBOARD)
            return
        if path.endswith("/api/workspace/presence"):
            _json(route, PRESENCE)
            return
        if path.endswith("/api/workspace/my-perf"):
            _json(route, MY_PERF)
            return
        if path.endswith("/api/workspace/workload"):
            _json(route, WORKLOAD)
            return
        if path.endswith("/api/workspace/escalations"):
            _json(route, ESCALATIONS)
            return
        if path.endswith("/api/setup/checklist"):
            _json(route, CHECKLIST)
            return
        if path.endswith("/api/drafts/risk-summary"):
            _json(route, RISK_SUMMARY)
            return
        _json(route, GENERIC_OK)

    return handler


def _settle(pg, extra_ms=900):
    """等网络静默（尽力）+ 固定缓冲，让最后一轮渲染落定。"""
    try:
        pg.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass
    pg.wait_for_timeout(extra_ms)


# ── 场景：函数把页面带到“可截图”状态（runner 统一截图/兜错） ──────
# /workspace 用 ?filter=all 深链钉死「全部」页签：mock 含 SLA 超时行时，
# 页面的“智能默认筛选”会自动切到 SLA 页签，导致列表只剩超时会话。
def scene_inbox_empty(pg, base_url):
    pg.goto(base_url + "/workspace?filter=all",
            wait_until="domcontentloaded", timeout=20000)
    pg.wait_for_selector("#conv-items .empty-list-icon", timeout=10000)
    _settle(pg)


def scene_inbox_list(pg, base_url):
    pg.goto(base_url + "/workspace?filter=all",
            wait_until="domcontentloaded", timeout=20000)
    pg.wait_for_selector(f".conv-item >> nth={len(CHATS_ROWS) - 1}", timeout=10000)
    _settle(pg)


def scene_chat_view(pg, base_url):
    scene_inbox_list(pg, base_url)
    pg.locator(".conv-item").nth(1).click()  # 中田TG（crit，4 条 mock 消息）
    pg.wait_for_selector(f".msg-bubble >> nth={len(THREAD['messages']) - 1}",
                         timeout=10000)
    _settle(pg)


def scene_dash(pg, base_url):
    pg.goto(base_url + "/workspace/dash", wait_until="domcontentloaded",
            timeout=20000)
    pg.wait_for_selector("#db-cards .db-card", timeout=10000)
    _settle(pg)


SCENES = [
    ("inbox_empty", scene_inbox_empty, {"empty_inbox": True}),
    ("inbox_list", scene_inbox_list, {"empty_inbox": False}),
    ("chat_view", scene_chat_view, {"empty_inbox": False}),
    ("dash", scene_dash, {"empty_inbox": False}),
]
THEMES = ("dark", "light")


def run(out_dir, base_url=DEFAULT_BASE_URL, token=DEFAULT_TOKEN):
    """拍摄全部场景。返回 {shot_name: "ok" | "skip:原因"}。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fixed_time = datetime.fromtimestamp(BASE_TS, tz=timezone.utc)
    results = {}
    console_errors = []

    with sync_playwright() as pw:
        # 渲染确定性 flags：GPU/部分光栅化的圆角与圆形边缘抗锯齿存在帧间
        # 非确定性（同一 DOM 连拍两次会漂移百余像素）。SwiftShader 软件光栅
        # + 关部分光栅/合成 AA/LCD 次像素后，实测连拍两次 0 像素差。
        browser = pw.chromium.launch(args=[
            "--disable-gpu",
            "--use-angle=swiftshader",
            "--force-color-profile=srgb",
            "--disable-lcd-text",
            "--hide-scrollbars",
            "--disable-partial-raster",
            "--disable-skia-runtime-opts",
            "--disable-composited-antialiasing",
            "--disable-checker-imaging",
        ])
        for theme in THEMES:
            for name, scene_fn, opts in SCENES:
                shot = f"{theme}_{name}"
                ctx = None
                try:
                    ctx = browser.new_context(viewport=VIEWPORT,
                                              device_scale_factor=1)
                    ctx.route("**/api/**", make_api_handler(**opts))
                    # max_redirects=0：只取会话 Cookie。跟随重定向会串到
                    # /login → / → /cases，后者在部分 dev 实例上 500。
                    resp = ctx.request.post(base_url + "/login",
                                            form={"auth_token": token},
                                            max_redirects=0)
                    if resp.status not in (200, 302, 303):
                        raise RuntimeError(f"login failed: HTTP {resp.status}")
                    pg = ctx.new_page()
                    pg.on("console",
                          lambda m, s=shot: console_errors.append(f"[{s}] {m.text}")
                          if m.type == "error" else None)
                    pg.on("pageerror",
                          lambda e, s=shot: console_errors.append(f"[{s}] {e}"))
                    pg.clock.set_fixed_time(fixed_time)
                    seeds = dict(SEEDED_LOCALSTORAGE, cp_theme=theme)
                    seed_js = "".join(
                        f"localStorage.setItem({json.dumps(k)},{json.dumps(v)});"
                        for k, v in seeds.items())
                    pg.add_init_script(
                        seed_js
                        + "(function(){var s=document.createElement('style');"
                        f"s.textContent={json.dumps(FREEZE_CSS)};"
                        "function add(){(document.head||document.documentElement)"
                        ".appendChild(s);}"
                        "if(document.documentElement){add();}"
                        "else{document.addEventListener('DOMContentLoaded',add);}"
                        "})();")
                    scene_fn(pg, base_url)
                    pg.screenshot(path=str(out / f"{shot}.png"))
                    results[shot] = "ok"
                    print(f"[ok]   {shot}.png")
                except Exception as e:
                    reason = str(e).splitlines()[0][:160]
                    results[shot] = f"skip:{reason}"
                    print(f"[skip] {shot}: {reason}")
                finally:
                    if ctx is not None:
                        try:
                            ctx.close()
                        except Exception:
                            pass
        browser.close()

    if console_errors:
        print("console_errors (前 8 条，仅供参考不影响结果):")
        for line in console_errors[:8]:
            print("  " + line[:200])
    n_ok = sum(1 for v in results.values() if v == "ok")
    print(f"captured {n_ok}/{len(results)} -> {out}")
    return results


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="拍摄视觉回归场景截图")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"输出目录（默认 {DEFAULT_OUT}）")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help=f"dev 实例地址（默认 {DEFAULT_BASE_URL}）")
    ap.add_argument("--token", default=DEFAULT_TOKEN,
                    help="登录 auth_token（默认 dev-ui-check）")
    args = ap.parse_args(argv)
    results = run(args.out, base_url=args.base_url, token=args.token)
    n_ok = sum(1 for v in results.values() if v == "ok")
    if n_ok == len(results):
        return 0
    return 3 if n_ok == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
