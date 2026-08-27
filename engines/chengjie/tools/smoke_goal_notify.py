# -*- coding: utf-8 -*-
"""目标完成通知链「免登录」冒烟（P4 2026-08-18）。

一条命令回答「完成→推送这条链现在什么状态」，给重启窗后的验证人（任何 agent
线/运维）用——不需要后台登录：文件真相（配置/渠道/绑定）+ 匿名 HTTP 探针
（路由 401/403=已装载、404=还在等重启窗）。

    python tools/smoke_goal_notify.py                 # 只读体检（默认全实例根）
    python tools/smoke_goal_notify.py --json          # 机器可读
    python tools/smoke_goal_notify.py --push auto     # 真发一条测试到首个绑定号
    python tools/smoke_goal_notify.py --push 54339828 # 真发到指定 chat_id

判定（verdict 纯函数，tests/test_smoke_goal_notify.py 钉住）：
- BROKEN         配置/渠道级缺口（goals 或 notify 关、渠道没订 goal_complete…）
                 ——重启多少次都不会好，先修配置；
- RIDES_RESTART  文件真相全就绪、新路由 404 ＝ 代码还没装载，等下个重启窗；
- READY          路由已装载 + 链路全通（bindings=0 只降级为提示不降档——
                 管理员渠道推送不依赖坐席绑定）。

--push 借告警渠道同一个 bot 直发（与生产完成推送同发件人），Telegram 拒收
（没先私聊过 bot / chat_id 错）会把 API 原因原样打出来。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:18799"


# ── 纯函数（门禁钉这里）────────────────────────────────────────────────────

def pick_channel(webhooks: Any) -> Optional[Dict[str, Any]]:
    """首个「启用 + telegram + 有 token」渠道（与测试推送端点同选择语义）。"""
    if not isinstance(webhooks, list):
        return None
    for w in webhooks:
        if not isinstance(w, dict) or w.get("enabled") is False:
            continue
        if str(w.get("format") or "").lower() != "telegram":
            continue
        if str(w.get("token") or "").strip():
            return w
    return None


def channel_covers_goal(webhooks: Any) -> bool:
    """goal_complete 别名有没有启用渠道在订阅（``all`` 通配同 notifier 语义）。"""
    if not isinstance(webhooks, list):
        return False
    for w in webhooks:
        if not isinstance(w, dict) or w.get("enabled") is False:
            continue
        events = w.get("events") or []
        if isinstance(events, list) and ("all" in events or "goal_complete" in events):
            return True
    return False


def verdict(checks: Dict[str, Any]) -> Tuple[str, List[str]]:
    """体检字典 → (READY|RIDES_RESTART|BROKEN|DISABLED, 人话原因列表)。

    DISABLED＝整域关闸（goals.enabled=false）——那是**选择不是故障**（试点/
    隔离实例常态），单列且不进非零退出；BROKEN 只留给「想工作但工作不了」。"""
    if not checks.get("goals_enabled"):
        return "DISABLED", ["companion.goals.enabled 未开——目标域整体关闸（选择而非故障）"]
    reasons: List[str] = []
    if not checks.get("notify_enabled"):
        reasons.append("companion.goals.notify.enabled 未开——完成扫描器不跑")
    if not checks.get("channel_present"):
        reasons.append("告警渠道无可用 telegram 通道（notify_webhooks.json）")
    if not checks.get("goal_subscribed"):
        reasons.append("没有启用渠道订阅 goal_complete——推送发进虚空")
    if reasons:
        return "BROKEN", reasons
    if not checks.get("push_agent"):
        reasons.append("push_agent 关：只推管理员渠道（坐席副本不发）——如非刻意，开 overlay")
    if int(checks.get("bindings", 0) or 0) <= 0:
        reasons.append("尚无坐席绑定通知号（管理员渠道不受影响；坐席可在目标卡「我也要收」自助绑）")
    http = checks.get("http_new_routes")
    if http == "missing":
        reasons.append("新路由 404＝本批 .py 未装载，等下个重启窗（文件真相已全就绪）")
        return "RIDES_RESTART", reasons
    if http == "partial":
        reasons.append("新路由半装载（早批已 live、本批增量等下个重启窗）——中间态自洽")
    if http == "unknown":
        reasons.append("实例不可达：HTTP 探针跳过（只验证了文件真相）")
    return "READY", reasons


# ── 采集（IO；对坏根软失败）─────────────────────────────────────────────────

def _read_webhooks(root: Path) -> Any:
    f = Path(root) / "config" / "notify_webhooks.json"
    try:
        if f.is_file():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! notify_webhooks.json 读取失败: {exc}", file=sys.stderr)
    return []


def _read_bindings(root: Path) -> List[Dict[str, str]]:
    db = Path(root) / "config" / "web_users.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT username, notify_tg_chat_id FROM web_users"
                " WHERE enabled=1 AND notify_tg_chat_id != ''").fetchall()
        finally:
            conn.close()
        return [{"username": str(r[0]),
                 "chat_id": str(r[1]),
                 "chat_tail": str(r[1])[-4:]} for r in rows]
    except Exception as exc:  # noqa: BLE001
        # 旧库还没 notify_tg_chat_id 列（迁移随下次进程启动跑）＝还没人能绑
        if "no such column" in str(exc):
            return []
        print(f"  ! web_users.db 读取失败: {exc}", file=sys.stderr)
        return []


def _http_code(url: str, timeout: float = 4.0) -> int:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception:
        return 0


def collect(root: Path, base_url: str = "") -> Dict[str, Any]:
    cfg = load_merged_config(root)
    companion = cfg.get("companion") if isinstance(cfg.get("companion"), dict) else {}
    goals = companion.get("goals") if isinstance(companion.get("goals"), dict) else {}
    notify = goals.get("notify") if isinstance(goals.get("notify"), dict) else {}
    webhooks = _read_webhooks(root)
    bindings = _read_bindings(root)
    checks: Dict[str, Any] = {
        "root": str(root),
        "goals_enabled": bool(goals.get("enabled", False)),
        "notify_enabled": bool(notify.get("enabled", False)),
        "push_agent": bool(notify.get("push_agent", False)),
        "include_profile": bool(notify.get("include_profile", False)),
        "channel_present": pick_channel(webhooks) is not None,
        "goal_subscribed": channel_covers_goal(webhooks),
        "bindings": len(bindings),
        "binding_users": [
            {"username": b["username"], "chat_tail": b["chat_tail"]}
            for b in bindings],
        "http_new_routes": "skipped",
    }
    if base_url:
        login = _http_code(f"{base_url}/login")
        if login != 200:
            checks["http_new_routes"] = "unknown"
        else:
            codes = (
                _http_code(f"{base_url}/api/goals/notify-status"),
                _http_code(f"{base_url}/api/workspace/my-notify-binding"),
            )
            checks["http_codes"] = list(codes)
            if all(c == 404 for c in codes):
                checks["http_new_routes"] = "missing"
            elif all(c in (401, 403) for c in codes):
                checks["http_new_routes"] = "loaded"
            else:
                checks["http_new_routes"] = "partial"   # 半装载=中间态，下窗齐
    checks["_bindings_full"] = bindings   # push auto 用；打印/JSON 前剥掉
    return checks


def push_test(root: Path, target: str, checks: Dict[str, Any]) -> Tuple[bool, str]:
    chan = pick_channel(_read_webhooks(root))
    if chan is None:
        return False, "无可用 telegram 渠道"
    chat = str(target or "").strip()
    if chat == "auto":
        rows = checks.get("_bindings_full") or []
        if not rows:
            return False, "auto 无从选：还没有任何坐席绑定"
        chat = rows[0]["chat_id"]
    if not chat.lstrip("-").isdigit():
        return False, f"chat_id 非法: {chat!r}"
    token = str(chan.get("token") or "")
    body = json.dumps({
        "chat_id": chat,
        "text": ("🔔 冒烟测试（smoke_goal_notify）：目标达成通知链可达性验证。"
                 "收到此条＝该号能收完成推送。"),
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True, f"已发到 …{chat[-4:]}（渠道 {chan.get('name')}）"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        return False, f"Telegram 拒收 HTTP {e.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return False, f"发送异常: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description="目标完成通知链冒烟（免登录）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--base", default=DEFAULT_BASE_URL,
                    help=f"实例 HTTP 基址（缺省 {DEFAULT_BASE_URL}；空串=跳过 HTTP 探针）")
    ap.add_argument("--push", default="",
                    help="真发测试：auto=首个绑定号 / 指定 chat_id（不给=只读）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root)
    exit_code = 0
    reports = []
    for root in roots:
        checks = collect(root, base_url=args.base)
        status, reasons = verdict(checks)
        push_result = None
        if args.push:
            ok, msg = push_test(root, args.push, checks)
            push_result = {"ok": ok, "detail": msg}
            if not ok:
                exit_code = max(exit_code, 2)
        checks.pop("_bindings_full", None)
        reports.append({"status": status, "reasons": reasons,
                        "push": push_result, **checks})
        if status == "BROKEN":
            exit_code = max(exit_code, 1)
        if not args.as_json:
            print(f"\n=== {root} → {status} ===")
            for k in ("goals_enabled", "notify_enabled", "push_agent",
                      "include_profile", "channel_present", "goal_subscribed",
                      "bindings", "http_new_routes"):
                print(f"  {k:18} {checks.get(k)}")
            for b in checks.get("binding_users") or []:
                print(f"    bound: {b['username']} …{b['chat_tail']}")
            for r in reasons:
                print(f"  - {r}")
            if push_result is not None:
                print(f"  push: {'OK' if push_result['ok'] else 'FAIL'} "
                      f"{push_result['detail']}")
    if args.as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
