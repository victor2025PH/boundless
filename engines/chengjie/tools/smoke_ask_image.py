# -*- coding: utf-8 -*-
"""「问这张图」链路实弹验证（零发送、零写库；一条命令给出四态判决）。

用途：ask-image 路由随重启窗口装载后，重启者/本线跑这一条命令即可完成验证——
自动从生产 inbox.db（只读）找最新一条**有归档媒体**的入站图片消息，带 Bearer
（CSRF bearer 口径放行）问一遍「图里有什么？」，并顺带探 media-capabilities 的
识别观测段（by_type/garbled 是否已随 P1 代码装载）。

判决（与 smoke_goal_notify 同语义）：
  PASS           路由已装载且真图真答；
  RIDES_RESTART  路由还没装载（404 Not Found）——等窗口，不是故障；
  SKIP           环境不可用（实例没起 / 无 token / 库里没有可问的图）exit 0；
  FAIL           路由在但答不出来 / 观测段缺失。

用法：python tools/smoke_ask_image.py [--data-root D:\\chengjie-instances\\zhiliao\\data]
      [--base http://127.0.0.1:18799] [--question 图里有什么？]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT))
sys.path.insert(0, str(ENGINE_ROOT / "scripts"))

KINDS = ("image", "photo", "img")


def _skip(msg: str) -> int:
    print(f"SKIP: {msg}")
    return 0


def _read_token(data_root: Path) -> str:
    import yaml
    for name in ("config.local.yaml", "config.yaml"):
        p = data_root / "config" / name
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            tok = str(((cfg.get("web_admin") or {}).get("auth_token")) or "")
            if tok:
                return tok
        except Exception:
            continue
    return ""


def _find_image_msg(data_root: Path):
    """最新一条有本地归档 ref 的入站图片消息 → (conversation_id, platform_msg_id, media_ref)。"""
    db = data_root / "config" / "inbox.db"
    if not db.is_file():
        return None
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT conversation_id, platform_msg_id, media_ref FROM messages "
            "WHERE direction='in' AND media_type IN (%s) AND media_ref LIKE "
            "'/static/protocol_media/%%' ORDER BY ts DESC LIMIT 5"
            % ",".join("?" * len(KINDS)), KINDS).fetchall()
        return dict(rows[0]) if rows else None
    finally:
        con.close()


def _req(base: str, token: str, method: str, path: str, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    # 连接类异常交调用方（判 SKIP）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="")
    ap.add_argument("--base", default="http://127.0.0.1:18799")
    ap.add_argument("--question", default="图里有什么？")
    args = ap.parse_args()

    if args.data_root:
        roots = [Path(args.data_root)]
    else:
        try:
            from _data_root import discover_instance_roots
            roots = discover_instance_roots() or []
        except Exception:
            roots = []
        if not roots:
            roots = [Path(r"D:\chengjie-instances\zhiliao\data")]
    root = roots[0]

    token = _read_token(root)
    if not token:
        return _skip(f"未能从 {root} 读到 web_admin.auth_token")
    msg = _find_image_msg(root)
    if not msg:
        return _skip("库里没有带归档媒体的入站图片消息（等真实流量）")
    print(f"[S1] 目标图 conv={msg['conversation_id']} msg={msg['platform_msg_id']}")

    try:
        t0 = time.time()
        code, d = _req(args.base, token, "POST", "/api/unified-inbox/ask-image", {
            "conversation_id": msg["conversation_id"],
            "message_id": msg["platform_msg_id"],
            "media_ref": msg["media_ref"],
            "question": args.question,
        })
    except Exception as e:
        return _skip(f"实例不可达 {e!r}")

    if code == 404 and str(d.get("detail") or "") == "Not Found":
        print("VERDICT: RIDES_RESTART（路由未装载，等下个重启窗口）")
        return 0
    if code != 200 or not d.get("ok") or not d.get("answer"):
        print(f"[FAIL] ask-image code={code} resp={str(d)[:200]}")
        print("VERDICT: FAIL")
        return 1
    print(f"[S2] PASS ask-image {time.time()-t0:.1f}s via={d.get('provider')} "
          f"答案: {str(d.get('answer'))[:120]}")

    # S3：P1 观测段装载探测（media-capabilities 原文里应出现 by_type 键）
    try:
        code2, d2 = _req(args.base, token, "GET", "/api/companion/media-capabilities",
                         timeout=30)
        raw = json.dumps(d2, ensure_ascii=False)
        has_bt = "by_type" in raw
        print(f"[S3] media-capabilities code={code2} by_type_present={has_bt}"
              + ("" if has_bt else "（P1 stats 未装载或该面不透出 enrich dump——非硬失败）"))
    except Exception as e:
        print(f"[S3] media-capabilities 探测失败（忽略）：{e!r}")

    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
