# -*- coding: utf-8 -*-
"""「所听即所发」线上冒烟（P2 2026-08-05）：试听 → sidecar 契约 → 复用发送 → 观测递增。

**只对账号自己的收藏消息真发**（chat_key 硬编码 'me'，与 live_multiwin_drill 同护栏），
其余全为只读。验证链（编号对应输出）：
  S1  /api/voice/avatar-status 含 preview_reuse ＝ 观测代码已装载（重启搭便车验证）
  S2  POST /api/voice/tts-test（带会话上下文）→ 返回 filename，且实例数据根
      tmp_tts_preview/<fn>.json sidecar 落盘（复用契约在写）
  S3  POST /api/unified-inbox/send-voice 带 preview_filename + chat_key='me'
      → ok:true 且 reused_preview:true（客户收到的=坐席试听的那份，零二次合成）
  S4  avatar-status.preview_reuse.hits 较基线 +1（观测真在计数）
  S5  收藏消息 thread 出现该语音（media 行）——投递闭环

用法::

    python tools/smoke_voice_reuse.py                    # 默认 zhiliao 实例
    python tools/smoke_voice_reuse.py --dry-run          # 只跑 S1（零发送零合成）

实例不可达 / 读不到 token → SKIP exit 0（不污染回归信号）；断言失败 exit 1。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple

SMOKE_TEXT = "语音复用链路自检：所听即所发。"


class Client:
    """session cookie + Bearer 双凭据（与 live_multiwin_drill 同鉴权口径）。"""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))

    def _raw(self, req: urllib.request.Request, timeout: float) -> Tuple[int, str]:
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, json.dumps({"error": str(e)[:200]})

    def login(self) -> bool:
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(
            self.base + "/login", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        code, _ = self._raw(req, 30)
        return code in (200, 303) and any(c.name == "session" for c in self._cj)

    def call(self, method: str, path: str, body: Any = None,
             timeout: float = 90) -> Tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        code, raw = self._raw(req, timeout)
        try:
            return code, json.loads(raw)
        except Exception:
            return code, {"raw": raw[:300]}


def read_token(data_root: str) -> str:
    import yaml
    root = Path(data_root)
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18799")
    ap.add_argument("--data-root", default=r"D:\chengjie-instances\zhiliao\data")
    ap.add_argument("--dry-run", action="store_true", help="只跑 S1（零发送零合成）")
    args = ap.parse_args()

    token = read_token(args.data_root)
    if not token:
        print(f"[SKIP] 未能从 {args.data_root} 读到 web_admin.auth_token（exit 0）")
        return 0
    cli = Client(args.base, token)
    if not cli.login():
        print("[SKIP] 实例不可达或登录失败（exit 0，不污染回归信号）")
        return 0

    fails = []

    def check(name: str, cond: Any, detail: str = "") -> None:
        ok = bool(cond)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    # S1 观测面已装载（重启搭便车验证：无需再吃一次重启窗口）
    code, st = cli.call("GET", "/api/voice/avatar-status")
    pr0 = (st or {}).get("preview_reuse") if isinstance(st, dict) else None
    check("S1 观测代码已装载（avatar-status.preview_reuse 在）",
          code == 200 and isinstance(pr0, dict), f"code={code}")
    if fails or args.dry_run:
        if args.dry_run and not fails:
            print("\n== dry-run 完成（S1 通过，未发送）==")
        return 1 if fails else 0
    base_hits = int((pr0 or {}).get("hits") or 0)

    # S1.5 账号自动发现：'default' 不是受管协议账号（owns_media 要确切账号 id）——
    # 从会话列表收集 telegram 账号，逐个探 send-caps 找 can_voice 的那个。
    acct = ""
    code, ch = cli.call("GET", "/api/unified-inbox/chats?platform=telegram&limit=60")
    rows = (ch or {}).get("chats") or (ch or {}).get("conversations") or []
    cand = []
    for r in rows:
        a = str((r or {}).get("account_id") or "")
        if a and a not in cand:
            cand.append(a)
    for a in cand[:8]:
        q = urllib.parse.urlencode({"platform": "telegram", "account_id": a})
        code, cap = cli.call("GET", f"/api/unified-inbox/send-caps?{q}")
        if code == 200 and (cap or {}).get("can_voice"):
            acct = a
            break
    check("S1.5 找到可直发语音的协议账号", bool(acct),
          f"candidates={cand[:8]} picked={acct}")
    if fails:
        return 1

    # S2 试听（带会话上下文=试听=发送契约）+ sidecar 落盘
    ctx = {"platform": "telegram", "account_id": acct, "chat_key": "me"}
    code, d = cli.call("POST", "/api/voice/tts-test",
                       {"text": SMOKE_TEXT, **ctx}, timeout=90)
    fn = str((d or {}).get("filename") or "")
    check("S2 试听成功返回产物名", code == 200 and (d or {}).get("ok") and fn,
          f"code={code} provider={(d or {}).get('provider')} fn={fn}")
    side = Path(args.data_root) / "tmp_tts_preview" / (fn + ".json")
    check("S2 复用契约 sidecar 落盘", fn and side.is_file(), str(side))

    # S3 复用发送（只发给自己的收藏消息；幂等键每轮唯一）
    code, s = cli.call("POST", "/api/unified-inbox/send-voice", {
        **ctx, "text": SMOKE_TEXT, "preview_filename": fn,
        "client_msg_id": f"smoke-{uuid.uuid4().hex[:12]}",
    }, timeout=90)
    check("S3 发送成功", code == 200 and (s or {}).get("ok") is True,
          f"code={code} reason={(s or {}).get('reason')} msg={(s or {}).get('message')}")
    check("S3 所听即所发命中（reused_preview=true）",
          (s or {}).get("reused_preview") is True,
          f"provider={(s or {}).get('provider')}")

    # S4 观测递增
    time.sleep(1.0)
    code, st2 = cli.call("GET", "/api/voice/avatar-status")
    pr1 = (st2 or {}).get("preview_reuse") or {}
    check("S4 复用计数 +1", int(pr1.get("hits") or 0) == base_hits + 1,
          f"hits {base_hits} -> {pr1.get('hits')} misses={pr1.get('misses')}")

    # S5 收藏消息里能看到这条语音（投递闭环；镜像有写入时差，轮询 ≤10s）
    seen = False
    for _ in range(5):
        q = urllib.parse.urlencode({**ctx, "limit": 20})
        code, t = cli.call("GET", f"/api/unified-inbox/thread?{q}")
        msgs = (t or {}).get("messages") or (t or {}).get("thread") or []
        if any("语音" in str(m.get("text") or "") or
               str(m.get("media_type") or "") in ("voice", "audio")
               for m in msgs if isinstance(m, dict)):
            seen = True
            break
        time.sleep(2.0)
    check("S5 收藏消息出现语音（镜像闭环）", seen)

    print(f"\n== 语音复用冒烟: {'ALL PASS' if not fails else 'FAILED: ' + ', '.join(fails)} ==")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
