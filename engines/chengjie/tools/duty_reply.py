# -*- coding: utf-8 -*-
"""值守群回复 CLI（实施81 P0-1，2026-08-28）——终结「每条回复手写一个 tmp 脚本」。

此前值守（173 的 Cursor 会话）每答一条群消息都现写一个 ``tmp_duty_reply*.py``
scp 到 117 执行：Bearer token 明文复制在 10+ 个一次性脚本里、``client_msg_id``
手工起名（起重＝被幂等表静默吞掉）、发完是否回写工单/台账全凭会话记忆。
本工具把这条链收口成一个参数化命令：

    python tools/duty_reply.py --group neice --text "@skuio 收到，正在查"
    python tools/duty_reply.py --group official --text-file reply.txt \
        --ticket 12 --status confirmed
    python tools/duty_reply.py --group neice --text "..." --dry-run

行为：
1. token 从实例配置读（``web_admin.auth_token``，config.local.yaml 优先），
   不再散落在脚本里；数据根按 scripts/_data_root 契约解析（``--data-root``
   可覆写）。
2. ``client_msg_id`` 自动生成（duty-日期-时间-4hex，可 ``--client-id`` 覆写
   用于显式重放）。
3. 带 ``--ticket`` 时优先走产品端点 ``POST /api/admin/bug-intake/{id}/reply``
   （@报障人 + 工单号 footer + note 回写全在服务端）；端点未装载（旧引擎/
   重启前）自动回落 ``/api/unified-inbox/send`` + 本地直写工单 note——
   重启前后同一条命令。
4. send 路由会触发「接管即静音」（会话被切 manual）→ 默认按值守 SOP 拨回
   ``auto_ai``（confirm_group 显式带上；``--no-restore`` 可跳过）。
5. 每次发送追加 JSONL 台账 ``<数据根>/logs/duty_replies.jsonl``（谁/何时/
   哪群/工单/走了哪条路径），值守交接不再靠翻聊天记录。
6. **对外答复红线**（2026-08-29 老板指令）：正文命中「修改配置文件」类
   指引（src.ops.support_reply_guard，含 dry-run）→ 硬拦 exit 2，**无
   bypass 旗**——对客户群不存在合法的 YAML 教程；改为指引后台设置页/
   界面按钮。

危险面：真发消息进生产报障群——**无 ``--dry-run`` 即实弹**，与旧 tmp 脚本
同权；工具只是把已经在发生的动作参数化，不新增发送面。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:18799"
DEFAULT_ACCOUNT = "6834964252"          # 报障群支持号（值守交接 §0）
GROUP_ALIASES: Dict[str, str] = {
    # 内测 bug 群 / 官方报障群（chat_key 含负号；与 bug_intake.groups 同形）
    "neice": "-1004345824259",
    "内测": "-1004345824259",
    "official": "-1004290740529",
    "官方": "-1004290740529",
}


# ── 纯函数（门禁 tests/test_duty_reply.py）───────────────────────────────────

def resolve_group(value: str) -> str:
    """群别名 → chat_key；已是原始 id（-100…）原样返回；空串抛 ValueError。"""
    v = str(value or "").strip()
    if not v:
        raise ValueError("group required")
    return GROUP_ALIASES.get(v.lower(), GROUP_ALIASES.get(v, v))


def build_client_msg_id(now: Optional[float] = None, rand: str = "") -> str:
    """幂等键：duty-YYYYMMDD-HHMMSS-4hex——自动唯一，杜绝「手工起名起重被
    send_dedup 静默吞掉」（旧 tmp 脚本用固定 id，重跑即不发且无感知）。"""
    ts = time.strftime("%Y%m%d-%H%M%S",
                       time.localtime(now if now is not None else time.time()))
    suffix = (rand or uuid.uuid4().hex)[:4]
    return f"duty-{ts}-{suffix}"


def build_send_payload(chat_key: str, text: str, *, account_id: str,
                       client_msg_id: str,
                       reply_to_id: Optional[int] = None) -> Dict[str, Any]:
    """/api/unified-inbox/send 载荷（skip_translate/force_lang 与值守 SOP 同参）。

    ``force_src="duty_reply"``（C3，#148）：值守 CLI 固定带 force_lang，服务端
    ``guard=force`` 日志据此与坐席在弹窗亲点「强发」区分开。
    """
    body: Dict[str, Any] = {
        "platform": "telegram", "account_id": str(account_id),
        "chat_key": str(chat_key), "text": str(text),
        "skip_translate": True, "force_lang": 1, "force_src": "duty_reply",
        "client_msg_id": client_msg_id,
    }
    if reply_to_id:
        body["reply_to"] = {"id": int(reply_to_id)}
    return body


def build_ledger_row(*, chat_key: str, text: str, ticket: int, path: str,
                     client_msg_id: str, ok: bool, note: str = "",
                     now: Optional[float] = None) -> Dict[str, Any]:
    ts = float(now if now is not None else time.time())
    return {
        "ts": round(ts, 3),
        "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        "chat_key": str(chat_key), "ticket": int(ticket or 0),
        "path": path, "ok": bool(ok), "client_msg_id": client_msg_id,
        "note": str(note or "")[:120],
        "text": str(text or "")[:500],
    }


def read_admin_token(data_root: Path) -> str:
    """实例 web_admin.auth_token（config.local.yaml 优先；读不到返空串）。"""
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


def configured_groups(data_root: Path) -> set:
    """引擎配置里的报障群集合（bug_intake.groups，防 CLI 打错群的哨兵）。"""
    try:
        from scripts._data_root import load_merged_config
        cfg = load_merged_config(data_root)
        raw = ((cfg.get("bug_intake") or {}).get("groups")) or []
        return {str(g).strip() for g in raw if str(g).strip()}
    except Exception:
        return set()


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _post(base: str, path: str, token: str, body: Dict[str, Any],
          timeout: int = 120) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        base.rstrip("/") + path, data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)[:200]}


def _out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))
    sys.stdout.flush()


def _append_ticket_note_local(data_root: Path, ticket: int, text: str) -> bool:
    """回落路径的工单 note 直写：AITR_DATA_DIR 指向实例数据根后走引擎模块
    （licensing.data_paths 契约 → 同一份 bug_intake.db；WAL 多进程安全）。"""
    try:
        os.environ.setdefault("AITR_DATA_DIR", str(data_root))
        from src.ops.bug_intake import append_ticket_note
        append_ticket_note(int(ticket), f"[值守回复] {text}")
        return True
    except Exception as exc:  # noqa: BLE001
        _out(f"[warn] 工单 note 回写失败（消息已发出）：{exc}")
        return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="值守群回复（报障群专用）")
    ap.add_argument("--group", required=True,
                    help="neice|official|原始 chat_key（-100…）")
    ap.add_argument("--text", default="", help="回复正文（与 --text-file 二选一）")
    ap.add_argument("--text-file", default="",
                    help="正文文件（UTF-8；长文/含引号用它，避 PowerShell 引号坑）")
    ap.add_argument("--ticket", type=int, default=0, help="关联工单号（回写 note）")
    ap.add_argument("--status", default="",
                    help="顺手流转工单状态（confirmed/fixed/…，需 --ticket）")
    ap.add_argument("--fix-note", default="", help="修复说明（配 --status fixed）")
    ap.add_argument("--no-mention", action="store_true",
                    help="产品端点路径不 @报障人")
    ap.add_argument("--reply-to", type=int, default=0,
                    help="引用回复的消息 id（仅 send 回落路径支持）")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--client-id", default="", help="覆写幂等键（显式重放用）")
    ap.add_argument("--no-restore", action="store_true",
                    help="send 回落路径发完不拨回 auto_ai")
    ap.add_argument("--force-group", action="store_true",
                    help="跳过「群不在 bug_intake.groups」哨兵")
    ap.add_argument("--receipt", action="store_true",
                    help="受理回执/追问（不含根因定性）——跳过取证门禁")
    ap.add_argument("--ack-no-evidence", action="store_true",
                    help="明知证据未到仍发定性回复（落台账审计，自担风险）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    if not str(text or "").strip():
        _out("[err] 正文为空（--text 或 --text-file）")
        return 2
    text = str(text).strip()

    # 对外答复红线（先于 dry-run：预演也该看到会被拦）
    from src.ops.support_reply_guard import POLICY_NOTE, config_edit_hits
    guard_hits = config_edit_hits(text)
    if guard_hits:
        _out("[err] 正文包含「修改配置文件」类指引，已按红线拦下（命中: "
             + ", ".join(guard_hits) + "）")
        _out("[policy] " + POLICY_NOTE)
        return 2

    chat_key = resolve_group(args.group)
    data_root = resolve_data_roots(args.data_root)[0]
    groups = configured_groups(data_root)
    if groups and chat_key not in groups and not args.force_group:
        _out(f"[err] {chat_key} 不在 bug_intake.groups {sorted(groups)}"
             "（防打错群；确认无误加 --force-group）")
        return 2

    # 取证门禁（值守循环 v3 §B，2026-09-02）：must_log 类工单证据未到 → 拦定性
    # 回复。0902 实锤：#145 无日志时的「两处根子」表述实为按修复史推断——本闸
    # 让「先日志后定性」从纪律变成机器强制。--receipt=受理回执豁免；台账/工单库
    # 任何异常 gate 软放行（取证台账绝不能瘫痪发送通道）。先于 dry-run：预演
    # 也该看到会被拦。
    gate_note = ""
    if args.ticket and not args.receipt:
        try:
            from tools.duty_evidence import ticket_send_gate
            gate_ok, gate_note = ticket_send_gate(
                data_root, args.ticket, ack=args.ack_no_evidence)
        except Exception:
            gate_ok, gate_note = True, ""
        if not gate_ok:
            _out("[gate] " + gate_note)
            return 3
        if gate_note:
            _out("[gate] " + gate_note)

    client_id = args.client_id or build_client_msg_id()
    _out(f"[plan] group={chat_key} ticket={args.ticket or '-'} "
         f"client_msg_id={client_id} data_root={data_root}")
    if args.dry_run:
        _out("[dry-run] payload="
             + json.dumps(build_send_payload(
                 chat_key, text, account_id=args.account,
                 client_msg_id=client_id,
                 reply_to_id=args.reply_to or None), ensure_ascii=False))
        _out("[dry-run] 未发送、未写台账。")
        return 0

    token = read_admin_token(data_root)
    if not token:
        _out(f"[err] 未读到 web_admin.auth_token（{data_root}\\config）")
        return 1

    path_used = ""
    ok = False
    note = ""
    # ① 产品端点（工单上下文齐备时优先；旧引擎 404 → 回落）
    if args.ticket:
        body: Dict[str, Any] = {"text": text,
                                "mention": (not args.no_mention)}
        if args.status:
            body["status"] = args.status
        if args.fix_note:
            body["fix_note"] = args.fix_note
        code, resp = _post(args.base,
                           f"/api/admin/bug-intake/{args.ticket}/reply",
                           token, body)
        if code == 404 and not (resp or {}).get("ticket_id"):
            _out("[info] /reply 端点未装载（或工单不存在），回落 send 路由")
        else:
            path_used = "bug-intake/reply"
            ok = code == 200
            note = "" if ok else f"HTTP {code} {json.dumps(resp, ensure_ascii=False)[:160]}"

    # ② send 路由回落（无工单/旧引擎）
    if not path_used:
        payload = build_send_payload(chat_key, text, account_id=args.account,
                                     client_msg_id=client_id,
                                     reply_to_id=args.reply_to or None)
        code, resp = _post(args.base, "/api/unified-inbox/send", token, payload)
        path_used = "unified-inbox/send"
        ok = code == 200 and (resp or {}).get("ok") is not False
        note = "" if ok else f"HTTP {code} {json.dumps(resp, ensure_ascii=False)[:160]}"
        if ok and args.ticket:
            _append_ticket_note_local(data_root, args.ticket, text)
            if args.status:
                try:
                    os.environ.setdefault("AITR_DATA_DIR", str(data_root))
                    from src.ops.bug_intake import set_ticket_status
                    set_ticket_status(args.ticket, args.status,
                                      fix_note=args.fix_note or None)
                except Exception as exc:  # noqa: BLE001
                    _out(f"[warn] 状态流转失败：{exc}")
        if ok and not args.no_restore:
            # 接管即静音的反向拨回（值守 SOP：发完回 auto_ai）
            rc, rr = _post(args.base, "/api/unified-inbox/automation", token, {
                "platform": "telegram", "account_id": args.account,
                "chat_key": chat_key, "mode": "auto_ai",
                "confirm_group": True})
            if rc != 200:
                _out(f"[warn] 拨回 auto_ai 失败 HTTP {rc}（自查会话档位）")

    # ack 审计：绕过取证门禁的定性回复必须在证据台账留痕（谁在什么时候
    # 自担了「无日志定性」的风险，事后可查）。
    if ok and args.ticket and args.ack_no_evidence and gate_note:
        try:
            from tools.duty_evidence import _ledger_row, append_ledger
            append_ledger(Path(data_root), _ledger_row(
                args.ticket, "ack_no_evidence",
                what=f"client_msg_id={client_id}"))
        except Exception:
            _out("[warn] ack 审计写入失败（不影响发送结果）")

    row = build_ledger_row(chat_key=chat_key, text=text, ticket=args.ticket,
                           path=path_used, client_msg_id=client_id, ok=ok,
                           note=note)
    try:
        led = Path(data_root) / "logs" / "duty_replies.jsonl"
        led.parent.mkdir(parents=True, exist_ok=True)
        with led.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        _out("[warn] 台账写入失败（不影响发送结果）")

    _out(f"[{'ok' if ok else 'FAIL'}] path={path_used} {note}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
