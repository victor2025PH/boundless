# -*- coding: utf-8 -*-
"""报障群「值守缺位」看门狗（实施81 P0-4，2026-08-28）。

补的洞：现有监听是 173 的 Cursor 会话挂**阻塞 SSH** 跑 group_sentinel——会话
断了/没人挂的时段，群里的提问没有任何东西在看（0827 凌晨实锤漏 2 条；
bug_intake_backfill 只补**登记**，不补「有人去回」）。本工具是与值守解耦的
安全网：报障群任一客户消息超过阈值分钟无人应答 → 告警到 **@ai_zkw（老板
本人，0829 拍板）**，经 tools/duty_alert.py（bot 直投 → 支持号私聊回落）。

判定口径（纯函数 ``find_unanswered``，门禁 tests/test_duty_watchdog.py）：
- 「提问」＝群内**非支持号**成员的入站消息（文本或带媒体）；
- 「已应答」＝该消息之后出现**任意出站**（支持号回复）或**支持号成员的
  入站**（老板/值守用自己号在群里答了，从读取账号视角是 in）；
- 最新未应答提问年龄 > ``--threshold-min``（默认 30）→ 告警；
- 每条消息告警一次（state 按 ``chat:msg_id`` 记，48h 过期），新提问=新 id
  =新告警；**仍未应答超 ``--re-alert-min``（默认 240）→ 升级重提一次并
  重置计时**（实施81 P2-4：首报被淹没时 4 小时后再吵一次，介于「轰炸」与
  「报过一次就装死」之间——与 health_watchdog 家族 4h 重提同刻度）。

C2（2026-09-02 两踩实锤）：扫描面 = 线程镜像 **∪ bug_events 台账**里的
``rate_capped_report`` / ``usage`` 事件（``--event-kinds`` 可调）。被防刷屏
限频静默的真反馈/提问不产生 bot 回执、不立单，此前值守面完全看不见
（skuio 四连报+直接提问全被 rate_capped_report 吞掉）——限流只限 bot 自动
回执，不得限值守可见性。事件行并入同一套「其后有无官方应答」判定，
告警文案带 ⛔ 限流静默 标记。台账经 ``GET /api/admin/bug-intake/events``
读取，旧引擎无此端点时回落本机只读打开 ``<data_root>/config/bug_intake.db``。

数据面全只读（thread API GET / 台账只读）；投递失败退出码 1（计划任务日志可见）。

用法::

    python tools/duty_watchdog.py --dry-run     # 只判定打印，不发不写状态
    python tools/duty_watchdog.py               # 巡检一轮（计划任务每 10 分钟）

计划任务（挂 run wrapper，见 deploy/duty/run_duty_watchdog.ps1）::

    schtasks /Create /TN DutyGroupWatchdog /SC MINUTE /MO 10 /F ^
      /TR "powershell -ExecutionPolicy Bypass -File D:\\boundless\\deploy\\duty\\run_duty_watchdog.ps1"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402

ENGINE_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BASE = "http://127.0.0.1:18799"
DEFAULT_STATE = Path(r"D:\chengjie-instances\.ops\duty_watchdog.state.json")
GROUP_NAMES = {"-1004345824259": "内测bug群", "-1004290740529": "官方报障群"}
_STATE_TTL_SEC = 48 * 3600
# 官方 bot（@tgzkw_bot，实施82 起代发回访/公示）：它的群消息是**官方应答**，
# 绝不能被当成客户提问计入未应答（否则每条回访都触发假告警）。
OFFICIAL_BOT_IDS = {"8506426282"}
# C2：默认纳入扫描面的台账事件类型（与 bug_intake.DUTY_VISIBLE_EVENT_KINDS 同源，
# 这里硬编码是为了离线工具不依赖引擎包可导入）。
DEFAULT_EVENT_KINDS = "rate_capped_report,usage"
EVENT_KIND_LABELS = {
    "rate_capped_report": "⛔ 限流静默（bot 未回执/未立单）",
    "usage": "❔ 使用咨询（未回）",
}


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def find_unanswered(
    messages: List[Dict[str, Any]], *, now: float, threshold_min: int,
    staff_ids: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """最新「超龄未应答」的客户消息；无则 None。

    messages＝thread API 消息行（direction/ts/message_id/sender_id/
    sender_name/text/media_type，顺序不限——内部按 ts 升序走）。
    """
    staff = {str(x) for x in (staff_ids or set())}
    pending: Optional[Dict[str, Any]] = None
    for m in sorted(messages or [], key=lambda x: float(x.get("ts") or 0)):
        if not isinstance(m, dict):
            continue
        direction = str(m.get("direction") or "")
        sender = str(m.get("sender_id") or "")
        if direction == "out" or (direction == "in" and sender and sender in staff):
            pending = None          # 支持号出站 / 员工亲号入站 ＝ 已有人应答
            continue
        if direction != "in":
            continue
        text = str(m.get("text") or "").strip()
        if not text and not str(m.get("media_type") or ""):
            continue                # 空壳行（无文本无媒体）不算提问
        pending = m
    if pending is None:
        return None
    age_min = (now - float(pending.get("ts") or 0)) / 60.0
    if age_min < max(1, int(threshold_min)):
        return None
    out = dict(pending)
    out["age_min"] = round(age_min, 1)
    return out


def events_as_messages(
    events: List[Dict[str, Any]], *, chat_key: str,
    thread_msgs: Optional[List[Dict[str, Any]]] = None,
    staff_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """C2：把 bug_events 台账行（rate_capped_report/usage）变成 ``find_unanswered``
    认得的入站消息行，与线程镜像并入同一扫描面。

    - 只取本群（``chat_key``）事件；reporter 是支持号/官方 bot 的不算提问；
    - ``message_id``＝``ev:<id>``（与镜像 mid 不撞键；state 去重按此记）；
    - 发言人名从镜像里同 sender_id 的行借（台账只存 id）；
    - 行带 ``kind``＝事件类型，文案渲染据此打「限流静默」标。
    """
    staff = {str(x) for x in (staff_ids or set())}
    names: Dict[str, str] = {}
    for m in thread_msgs or []:
        if isinstance(m, dict) and m.get("sender_id") and m.get("sender_name"):
            names.setdefault(str(m["sender_id"]), str(m["sender_name"]))
    out: List[Dict[str, Any]] = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("chat_id") or "") != str(chat_key):
            continue
        rid = str(e.get("reporter_id") or "")
        if rid and rid in staff:
            continue
        detail = str(e.get("detail") or "").strip()
        out.append({
            "direction": "in",
            "ts": float(e.get("ts") or 0),
            "sender_id": rid,
            "sender_name": names.get(rid, ""),
            "text": detail or f"[{e.get('kind') or 'event'}]",
            "media_type": "",
            "message_id": f"ev:{e.get('id') or int(float(e.get('ts') or 0))}",
            "kind": str(e.get("kind") or ""),
        })
    return out


def prune_state(state: Dict[str, float], now: float) -> Dict[str, float]:
    return {k: v for k, v in (state or {}).items()
            if now - float(v or 0) < _STATE_TTL_SEC}


def alert_decision(state: Dict[str, float], key: str, now: float,
                   re_alert_min: int) -> str:
    """'first' | 'repeat' | ''（不告警）。

    首见 → first；已报过且距上次告警 ≥ re_alert_min → repeat（调用方成功投递
    后刷新戳＝重置计时）；re_alert_min<=0 关闭重提（旧行为）。
    """
    ts = (state or {}).get(str(key))
    if ts is None:
        return "first"
    if int(re_alert_min) <= 0:
        return ""
    return "repeat" if now - float(ts) >= int(re_alert_min) * 60 else ""


def build_alert_text(hits: List[Dict[str, Any]], now: Optional[float] = None) -> str:
    ts = time.strftime("%m-%d %H:%M",
                       time.localtime(now if now is not None else time.time()))
    lines = [f"[值守缺位告警] {ts} 报障群有提问超时无人应答："]
    for h in hits:
        gname = GROUP_NAMES.get(str(h.get("chat_key")), str(h.get("chat_key")))
        who = h.get("sender_name") or h.get("sender_id") or "?"
        text = str(h.get("text") or "").strip() or f"[{h.get('media_type') or '媒体'}]"
        mark = "🔁 持续未应答" if h.get("re_alert") else "🔴"
        kind_tag = EVENT_KIND_LABELS.get(str(h.get("kind") or ""), "")
        lines.append(f"{mark} {gname} · {who} · 已等 {h.get('age_min')} 分钟"
                     + (f" · {kind_tag}" if kind_tag else ""))
        lines.append(f"    {text[:140]}")
    lines.append("处置：python tools/duty_reply.py --group neice|official "
                 "--text \"...\"（或工作台群组动态直接回）")
    return "\n".join(lines)


# ── 数据读取 ─────────────────────────────────────────────────────────────────

def _get_json(base: str, path: str, token: str, params: Dict[str, str],
              timeout: int = 30) -> Dict[str, Any]:
    url = base.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _read_token(data_root: Path) -> str:
    try:
        import yaml
    except Exception:
        return ""
    for name in ("config.local.yaml", "config.yaml"):
        fp = Path(data_root) / "config" / name
        if not fp.is_file():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


def _read_events_sqlite(data_root: Path, kinds: List[str],
                        since_ts: float, limit: int = 400) -> List[Dict[str, Any]]:
    """本机只读打开台账（旧引擎无 /events 端点时的回落；文件不存在 → 空）。"""
    db = Path(data_root) / "config" / "bug_intake.db"
    if not db.is_file() or not kinds:
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        q = ("SELECT id, ts, chat_id, kind, reporter_id, detail FROM bug_events"
             " WHERE kind IN (%s) AND ts >= ? ORDER BY ts ASC, id ASC LIMIT ?"
             % ",".join("?" * len(kinds)))
        rows = con.execute(q, [*kinds, float(since_ts), int(limit)]).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def load_duty_events(base: str, token: str, data_root: Path, *,
                     kinds: List[str], since_min: int) -> List[Dict[str, Any]]:
    """C2 扫描面第二源：台账事件。API 优先（跨机可用），失败回落本机 sqlite。
    两路都失败返回空并打 warn——扫描面缩回镜像单源，绝不让看门狗整轮失败。"""
    if not kinds:
        return []
    try:
        d = _get_json(base, "/api/admin/bug-intake/events", token, {
            "kinds": ",".join(kinds), "since_min": str(int(since_min)),
            "limit": "400"})
        evs = d.get("events") if isinstance(d, dict) else None
        if isinstance(evs, list):
            return [e for e in evs if isinstance(e, dict)]
    except Exception as exc:  # noqa: BLE001
        _out(f"[warn] 台账事件端点不可用（{str(exc)[:80]}），回落本机 sqlite")
    try:
        return _read_events_sqlite(
            data_root, kinds, time.time() - int(since_min) * 60)
    except Exception as exc:  # noqa: BLE001
        _out(f"[warn] 台账 sqlite 读取失败：{str(exc)[:120]}（扫描面退回镜像单源）")
        return []


def _deliver(text: str) -> tuple:
    """值守内部告警统一出口（@ai_zkw；见 tools/duty_alert.py）。"""
    from tools.duty_alert import deliver
    return deliver(text)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="报障群值守缺位看门狗")
    ap.add_argument("--threshold-min", type=int, default=30)
    ap.add_argument("--re-alert-min", type=int, default=240,
                    help="仍未应答的升级重提间隔（分钟；0=只报一次）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--event-kinds", default=DEFAULT_EVENT_KINDS,
                    help="并入扫描面的 bug_events 类型（逗号分隔；空=只看镜像）")
    ap.add_argument("--event-lookback-min", type=int, default=720,
                    help="台账事件回看窗（分钟）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    event_kinds = [k.strip() for k in str(args.event_kinds or "").split(",")
                   if k.strip()]

    data_root = resolve_data_roots(args.data_root)[0]
    cfg = load_merged_config(data_root)
    bi = (cfg.get("bug_intake") or {}) if isinstance(cfg, dict) else {}
    groups = [str(g).strip() for g in (bi.get("groups") or []) if str(g).strip()]
    staff = {str(a).strip() for a in (bi.get("support_accounts") or [])
             if str(a).strip()}
    staff |= OFFICIAL_BOT_IDS          # bot 的回访/公示＝官方应答
    if not groups:
        _out("[skip] bug_intake.groups 为空（未启用报障群值守）")
        return 0
    account = sorted(staff)[0] if staff else "default"
    token = _read_token(data_root)
    if not token:
        _out(f"[err] 未读到 web_admin.auth_token（{data_root}）")
        return 1

    state_path = Path(args.state)
    state: Dict[str, float] = {}
    try:
        if state_path.is_file():
            state = {str(k): float(v) for k, v in json.loads(
                state_path.read_text(encoding="utf-8")).items()}
    except Exception:
        state = {}
    now = time.time()
    state = prune_state(state, now)

    # C2：台账事件（限流静默的真反馈/提问）一次拉全，按群并入各自扫描面
    events = load_duty_events(
        args.base, token, data_root, kinds=event_kinds,
        since_min=max(args.threshold_min, int(args.event_lookback_min)))

    hits: List[Dict[str, Any]] = []
    for chat_key in groups:
        try:
            d = _get_json(args.base, "/api/unified-inbox/thread", token, {
                "platform": "telegram", "account_id": account,
                "chat_key": chat_key, "limit": "40"})
        except Exception as exc:  # noqa: BLE001
            _out(f"[warn] 读取 {chat_key} 线程失败：{str(exc)[:120]}")
            continue
        msgs = list(d.get("messages") or [])
        ev_rows = events_as_messages(
            events, chat_key=chat_key, thread_msgs=msgs, staff_ids=staff)
        if ev_rows:
            _out(f"[scan] {chat_key} 并入台账事件 {len(ev_rows)} 条"
                 f"（{','.join(sorted({r['kind'] for r in ev_rows}))}）")
        hit = find_unanswered(msgs + ev_rows, now=now,
                              threshold_min=args.threshold_min,
                              staff_ids=staff)
        if not hit:
            continue
        key = f"{chat_key}:{hit.get('message_id') or hit.get('ts')}"
        verdict = alert_decision(state, key, now, args.re_alert_min)
        if not verdict:
            _out(f"[seen] {chat_key} 未应答提问已告警过（{key}，未到重提点）")
            continue
        hit["chat_key"] = chat_key
        hit["_key"] = key
        hit["re_alert"] = verdict == "repeat"
        hits.append(hit)

    if not hits:
        _out("[ok] 无超龄未应答提问")
        if not args.dry_run:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, ensure_ascii=False),
                                  encoding="utf-8")
        return 0

    text = build_alert_text(hits, now=now)
    _out(text)
    if args.dry_run:
        _out("[dry-run] 未投递、未写状态。")
        return 0
    ok, note = _deliver(text)
    _out(f"[deliver] {'ok' if ok else 'FAIL'} {note}")
    if ok:
        for h in hits:
            state[str(h["_key"])] = now
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False),
                          encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
