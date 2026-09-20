# -*- coding: utf-8 -*-
"""值守群发文件 CLI（N-5 B，2026-09-08）——``duty_reply`` 的文件版。

为什么：field-agent 剧本 / 取证脚本改版必须「重新发文件进群并明说覆盖」（值守交接 §K，
0903 实录），此前三次（0902 mid 1088/1090、0903 mid 1106/1107、0904 mid 1157/1158）
都是现写 tmp 脚本直打 ``/api/unified-inbox/send-media``，token 与幂等键手工复制，且
0904 那次没进 ``duty_replies.jsonl``。本工具把这条路收口：

    python tools/duty_send_file.py --group neice --file deploy/field-agent/zl_collect_jun.ps1 \
        --name zl_collect_jun.ps1.txt --caption "钧机专用 · …" [--dry-run]

行为：
1. 走 ``POST /api/unified-inbox/send-media``（M-3 A 裸流入口：body=文件本体，元数据走
   query；``Content-Length`` 精确，上限预检在服务端）。``.ps1`` 在出站内容守卫黑名单
   （media_guard.DANGEROUS_EXTS）——脚本文件按 0904 先例用 ``--name`` 改成 ``.txt`` 发，
   caption 里写「下载后改名」。
2. token / 数据根 / 群别名 / 幂等键 / 台账与 ``duty_reply`` 完全同源（直接 import）。
3. send-media 同样触发「接管即静音」→ 发完按值守 SOP 把群拨回 ``auto_ai``
   （``confirm_group``；``--no-restore`` 跳过）。报障群一旦留在 manual，自动回执链就哑了。
4. 每次发送追加 ``duty_replies.jsonl`` 一行（path=unified-inbox/send-media，text=caption）。

⚠ 无 ``--dry-run`` 即实弹进生产报障群。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402
from tools.duty_reply import (  # noqa: E402
    DEFAULT_ACCOUNT, DEFAULT_BASE, _out, _post, build_client_msg_id,
    build_ledger_row, configured_groups, read_admin_token, resolve_group)


def build_send_media_url(base: str, *, chat_key: str, account_id: str, name: str,
                         caption: str, client_msg_id: str) -> str:
    """裸流入口的完整 URL：元数据全在 query（与前端 send-media stream 模式同形）。"""
    q = urllib.parse.urlencode({
        "platform": "telegram", "account_id": str(account_id), "chat_key": str(chat_key),
        "name": str(name), "caption": str(caption or ""), "client_msg_id": client_msg_id,
    })
    return base.rstrip("/") + "/api/unified-inbox/send-media?" + q


def _put_file(url: str, token: str, data: bytes, timeout: int = 180) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "Content-Length": str(len(data)),
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)[:200]}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="值守群发文件（报障群专用）")
    ap.add_argument("--group", required=True, help="neice|official|原始 chat_key（-100…）")
    ap.add_argument("--file", required=True, help="本地文件路径")
    ap.add_argument("--name", default="", help="发出去的文件名（默认取本地名；脚本类改 .txt）")
    ap.add_argument("--caption", default="", help="配文（与 --caption-file 二选一）")
    ap.add_argument("--caption-file", default="")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--client-id", default="")
    ap.add_argument("--no-restore", action="store_true", help="发完不拨回 auto_ai")
    ap.add_argument("--force-group", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    fp = Path(args.file)
    if not fp.is_file():
        _out(f"[err] 文件不存在：{fp}")
        return 2
    caption = args.caption
    if args.caption_file:
        caption = Path(args.caption_file).read_text(encoding="utf-8").strip()
    name = (args.name or fp.name).strip()
    data = fp.read_bytes()
    chat_key = resolve_group(args.group)
    data_root = resolve_data_roots(args.data_root)[0]
    groups = configured_groups(data_root)
    if groups and chat_key not in groups and not args.force_group:
        _out(f"[err] {chat_key} 不在 bug_intake.groups {sorted(groups)}（防打错群；确认无误加 --force-group）")
        return 2
    client_id = args.client_id or build_client_msg_id()
    url = build_send_media_url(args.base, chat_key=chat_key, account_id=args.account,
                               name=name, caption=caption, client_msg_id=client_id)
    _out(f"[plan] group={chat_key} file={fp} → name={name} size={len(data)} B "
         f"mime={mimetypes.guess_type(name)[0] or 'application/octet-stream'} client_msg_id={client_id}")
    _out(f"[plan] caption({len(caption)} 字)={caption}")
    if args.dry_run:
        _out(f"[dry-run] POST {url}")
        _out("[dry-run] 未发送、未写台账。")
        return 0
    token = read_admin_token(data_root)
    if not token:
        _out(f"[err] 未读到 web_admin.auth_token（{data_root}\\config）")
        return 1
    code, resp = _put_file(url, token, data)
    ok = code == 200 and (resp or {}).get("ok") is not False
    note = "" if ok else f"HTTP {code} {json.dumps(resp, ensure_ascii=False)[:200]}"
    if ok and not args.no_restore:
        rc, _ = _post(args.base, "/api/unified-inbox/automation", token, {
            "platform": "telegram", "account_id": args.account,
            "chat_key": chat_key, "mode": "auto_ai", "confirm_group": True})
        if rc != 200:
            _out(f"[warn] 拨回 auto_ai 失败 HTTP {rc}（自查会话档位）")
    row = build_ledger_row(chat_key=chat_key, text=f"[file {name}] {caption}", ticket=0,
                           path="unified-inbox/send-media", client_msg_id=client_id, ok=ok, note=note)
    try:
        led = Path(data_root) / "logs" / "duty_replies.jsonl"
        led.parent.mkdir(parents=True, exist_ok=True)
        with led.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        _out("[warn] 台账写入失败（不影响发送结果）")
    _out(f"[{'ok' if ok else 'FAIL'}] path=unified-inbox/send-media {note} resp={json.dumps(resp, ensure_ascii=False)[:300]}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
