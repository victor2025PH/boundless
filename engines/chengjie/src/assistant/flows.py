# -*- coding: utf-8 -*-
"""小智「业务流程」注册表（实施58 P5，2026-08-23）——旗舰流：登陆飞机。

刻意的架构选择（比方案 v3 的「通用 Flow DSL」再收敛一步，理由入档）：
1. **模式运行器 > 通用解释器**：首批流程全是「协议扫码登录」同一形态
   （telegram/line 共享同一路由家族与状态机），前端实现一个
   `runLoginFlow(spec)` 模式运行器即可；通用 DSL 等第三种形态出现再谈
   （YAGNI——为一个形态造解释器是纯负债）。本表只出**数据**：URL 模板、
   触发说法族、双语文案、轮询/超时/换码参数。
2. **URL 白名单钉死**：流程可触达的端点全部收敛在 `ALLOWED_URL_PREFIXES`
   （登录路由家族），`validate_registry()` 被门禁常驻调用——谁想给流程
   加别的调用面，测试先红（与动作注册表同哲学：结构性防线）。
3. **触发是确定性短语匹配**（归一化 contains）：旗舰路径零 LLM——
   「我要登陆飞机」不需要大模型来懂。planner 路由在 LLM 之前先问本表。
4. **敏感输入零接触**：password_needed 的云密码由前端输入框直投登录 API
   （spec 只给 URL），不进任务状态、不进 sessionStorage、不进 LLM 上下文、
   不被朗读——流程任务刻意**不做断点续传**（交互态含敏感环节，重开重扫
   比恢复更安全也更符合直觉）。
5. **双证据成功判据**：status=authorized（服务端在该状态内部已完成账号
   落库 `_persist_login_account`——注册表写入是 authorized 的组成部分）。

门禁 ``tests/test_assistant_flows.py``。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# 流程唯一可触达的端点前缀（家族白名单，门禁钉死）
ALLOWED_URL_PREFIXES = (
    "/api/platforms/telegram/login/",
    "/api/platforms/line/login/",
    "/api/platforms/telegram/login",   # start（无尾斜杠拼段）
    "/api/platforms/line/login",
)

_QR_LOGIN_TEXTS = {
    "tg_login": {
        "label_zh": "🛫 登录 Telegram 账号（登陆飞机）",
        "label_en": "🛫 Log in a Telegram account",
        "say_zh": "好的，带你登录一个新的 Telegram 账号：我会弹出二维码，"
                  "用手机 Telegram 扫一下就行。",
        "say_en": "OK — logging in a new Telegram account: I will show a QR "
                  "code; scan it with the Telegram app on your phone.",
        "confirm_zh": "要登录一个新的 Telegram 账号，对吗？",
        "confirm_en": "Log in a NEW Telegram account, correct?",
        "qr_hint_zh": "手机 Telegram：设置 → 设备 → 连接桌面设备，扫这个码",
        "qr_hint_en": "Phone Telegram: Settings → Devices → Link Desktop "
                      "Device, scan this code",
        "scanned_zh": "看到你扫码了，正在确认…",
        "scanned_en": "Scan detected, confirming…",
        "pwd_zh": "这个账号开了两步验证：请在下面输入云密码"
                  "（不会被朗读，也不会被记录）",
        "pwd_en": "Two-step verification is on: enter the cloud password "
                  "below (never spoken aloud, never logged)",
        "requery_zh": "二维码过期了，已自动换新码，请重新扫",
        "requery_en": "QR expired — a fresh code is up, please rescan",
        "ok_zh": "登录成功！账号已进注册表。要不要接着绑人设、设回复模式？"
                 "在下面再吩咐一句就行。",
        "ok_en": "Logged in! The account is registered. Next you can bind a "
                 "persona or set reply mode — just tell me below.",
        "preflight_warn_zh": "直连预检未通过：若扫码后长时间无反应，"
                             "请先到渠道中心配置代理",
        "preflight_warn_en": "Direct-reach preflight failed: if the scan "
                             "stalls, configure a proxy in Channel Center",
    },
    "line_login": {
        "label_zh": "登录 LINE 账号",
        "label_en": "Log in a LINE account",
        "say_zh": "好的，带你登录一个新的 LINE 账号：我会弹出二维码，"
                  "用手机 LINE 扫一下。",
        "say_en": "OK — logging in a new LINE account via QR scan.",
        "confirm_zh": "要登录一个新的 LINE 账号，对吗？",
        "confirm_en": "Log in a NEW LINE account, correct?",
        "qr_hint_zh": "手机 LINE：主页 → 扫码，扫这个码（可能还需在手机上"
                      "输入确认码/密码）",
        "qr_hint_en": "Phone LINE: Home → scan; you may also need to confirm "
                      "a code on the phone",
        "scanned_zh": "看到扫码了，正在确认…",
        "scanned_en": "Scan detected, confirming…",
        "pwd_zh": "需要输入密码：请在下面输入（不会被朗读或记录）",
        "pwd_en": "Password needed: enter below (never spoken, never logged)",
        "requery_zh": "二维码过期了，已自动换新码，请重新扫",
        "requery_en": "QR expired — fresh code is up, please rescan",
        "ok_zh": "登录成功！账号已进注册表。",
        "ok_en": "Logged in! The account is registered.",
        "preflight_warn_zh": "",
        "preflight_warn_en": "",
    },
}

# 触发说法族（归一化后 contains 匹配；宁窄勿宽——误触发登录流很扰人）
_TRIGGERS = {
    "tg_login": (
        "登陆飞机", "登录飞机", "登陆telegram", "登录telegram", "登录tg",
        "登陆tg", "登录电报", "登陆电报", "加个telegram", "加个tg号",
        "加个飞机号", "login telegram", "add telegram account",
    ),
    "line_login": (
        "登录line", "登陆line", "加个line号", "login line",
        "add line account",
    ),
}

FLOWS: Dict[str, Dict[str, Any]] = {
    "tg_login": {
        "kind": "qr_login",
        "platform": "telegram",
        "level": "L3",
        "agent_allowed": False,   # 账号资产操作：坐席角色不可用
        "preflight_url": "/api/platforms/telegram/login/preflight",
        "start_url": "/api/platforms/telegram/login/start",
        "status_url": "/api/platforms/telegram/login/{login_id}/status",
        "password_url": "/api/platforms/telegram/login/{login_id}/password",
        "cancel_url": "/api/platforms/telegram/login/{login_id}/cancel",
        "poll_ms": 2000,
        "timeout_ms": 180000,
        "requery_max": 3,
    },
    "line_login": {
        "kind": "qr_login",
        "platform": "line",
        "level": "L3",
        "agent_allowed": False,
        "preflight_url": "",      # line 无直连预检
        "start_url": "/api/platforms/line/login/start",
        "status_url": "/api/platforms/line/login/{login_id}/status",
        "password_url": "/api/platforms/line/login/{login_id}/password",
        "cancel_url": "/api/platforms/line/login/{login_id}/cancel",
        "poll_ms": 2000,
        "timeout_ms": 180000,
        "requery_max": 3,
    },
}


def _norm(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(s or "")).lower()


def detect_flow_intent(text: str) -> str:
    """确定性触发：归一化 contains；命中最长触发词的流程胜出（宁窄勿宽）。"""
    q = _norm(text)
    if not q or len(q) > 200:
        return ""
    best, best_len = "", 0
    for fid, trigs in _TRIGGERS.items():
        for tg in trigs:
            tn = _norm(tg)
            if tn and tn in q and len(tn) > best_len:
                best, best_len = fid, len(tn)
    return best


def flow_allowed_for_role(fid: str, role: str) -> bool:
    spec = FLOWS.get(fid)
    if not spec:
        return False
    if str(role or "").strip().lower() == "agent":
        return bool(spec.get("agent_allowed"))
    return True


def catalog(role: str, lang: str = "zh") -> List[Dict[str, Any]]:
    en = str(lang or "").lower().startswith("en")
    out = []
    for fid, spec in FLOWS.items():
        if not flow_allowed_for_role(fid, role):
            continue
        txt = _QR_LOGIN_TEXTS.get(fid, {})
        out.append({"id": fid, "kind": spec["kind"], "level": spec["level"],
                    "platform": spec["platform"],
                    "label": txt.get("label_en" if en else "label_zh", fid)})
    return out


def client_spec(fid: str, lang: str = "zh") -> Optional[Dict[str, Any]]:
    """给前端模式运行器的完整规格（文案按 lang 解析成单语）。"""
    spec = FLOWS.get(str(fid or ""))
    if not spec:
        return None
    en = str(lang or "").lower().startswith("en")
    txt = _QR_LOGIN_TEXTS.get(fid, {})
    suffix = "_en" if en else "_zh"
    texts = {k[: -len(suffix)]: v for k, v in txt.items()
             if k.endswith(suffix)}
    out = dict(spec)
    out["id"] = fid
    out["texts"] = texts
    return out


def validate_registry() -> List[str]:
    """结构不变量（门禁常驻调用）：URL 白名单/文案双语齐平/参数边界。"""
    problems: List[str] = []
    for fid, spec in FLOWS.items():
        for key in ("start_url", "status_url", "password_url", "cancel_url",
                    "preflight_url"):
            url = str(spec.get(key) or "")
            if not url:
                continue
            if not any(url.startswith(p) for p in ALLOWED_URL_PREFIXES):
                problems.append(f"{fid}.{key} 越出白名单: {url}")
        txt = _QR_LOGIN_TEXTS.get(fid)
        if not txt:
            problems.append(f"{fid} 缺文案表")
            continue
        zh_keys = {k[:-3] for k in txt if k.endswith("_zh")}
        en_keys = {k[:-3] for k in txt if k.endswith("_en")}
        if zh_keys != en_keys:
            problems.append(f"{fid} 文案 zh/en 不齐平: {zh_keys ^ en_keys}")
        if not (500 <= int(spec.get("poll_ms", 0)) <= 10000):
            problems.append(f"{fid}.poll_ms 越界")
        if not (30000 <= int(spec.get("timeout_ms", 0)) <= 600000):
            problems.append(f"{fid}.timeout_ms 越界")
        if not (0 <= int(spec.get("requery_max", -1)) <= 5):
            problems.append(f"{fid}.requery_max 越界")
        if spec.get("level") != "L3":
            problems.append(f"{fid} 登录类流程必须 L3")
        if spec.get("agent_allowed"):
            problems.append(f"{fid} 账号资产流程不得对坐席开放")
    return problems
