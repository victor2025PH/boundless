# -*- coding: utf-8 -*-
"""多开治理防线·真发演练（运维显式执行，非 pytest；2026-07-29 首次实施后沉淀）。

**为什么必须真发**：多开双发是「客户真收到两条一样的话」级事故，而防线的最后一环
（协议 worker 真投递）只有真发才能证伪。单测覆盖逻辑，本脚本覆盖**在线全链**。

**为什么不做成 pytest**：本仓测试纪律是「测试自建 app/store，不依赖常驻服务」
（见 AGENTS.md）。真发要打生产实例 + 真 Telegram 会话，属运维演练，与
``deploy/instances/smoke_restart_resilience.ps1`` 同族——故落 tools/ 并要求 --confirm。

**安全设计**（每条都有门禁 tests/test_live_drill_guards.py 守着）：
  1. 默认目标 = 账号自己的 **Saved Messages**（``chat_key='me'``）——客户永不可见；
     指向真人 peer 必须显式 ``--allow-peer``（且脚本会二次告警）。
  2. 不带 ``--confirm`` 只做**只读预检**（列账号 + 读计数器），一条消息都不发。
  3. 断言全部按本轮 TAG 限定——往轮残留不会污染判定（首次实施踩过此坑）。
  4. 判定以 prometheus 计数器**增量**为权威（worker 自己的投递计数），线程回读为辅证。

用法::

    python tools/live_multiwin_drill.py                    # 只读预检
    python tools/live_multiwin_drill.py --confirm          # 真发（Saved Messages）
    python tools/live_multiwin_drill.py --confirm --account 8041810715
    python tools/live_multiwin_drill.py --confirm --base http://127.0.0.1:18799

验收面：
  E1 发送幂等：同 ``client_msg_id`` 双发 → 真发恰 1 条；换 id 不误拦。
  E2 草稿双窗互斥：并发 approve → 恰一路 200(排真投递)/一路 409；真发恰 1 条。
  E3 计数器互证：send_dedup.reserved/duplicates + autosend.human_delivered 增量精确。

鉴权：send 端点走 ``_require_auth``（认 session cookie），CSRF 中间件遇 Bearer 放行 →
先 ``POST /login`` 用 auth_token 换 session，之后请求同时带 cookie 与 Bearer 头。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
SAVED_MESSAGES = "me"

# 计数器名（与 drafts_routes.register_metrics_route 的 _gauge 名一致）
G_RESERVED = "ws_send_dedup_reserved_total"
G_DUPES = "ws_send_dedup_duplicates_total"
G_HUMAN_OK = "ws_autosend_human_delivered_total"
G_HUMAN_ERR = "ws_autosend_human_deliver_errors_total"

_TAG_RE = re.compile(r"^QA\d{6}$")


# ── 纯函数安全护栏（被 tests/test_live_drill_guards.py 锁定）──────────────

class UnsafeTarget(RuntimeError):
    """演练目标不安全（会打到真人客户）。"""


def ensure_safe_target(chat_key: str, *, allow_peer: bool = False) -> str:
    """校验演练目标：只放行 Saved Messages，除非显式 allow_peer。

    这是防「未来某次复跑把演练文案发给真客户」的唯一硬闸门——真发脚本最大的风险
    不是逻辑错，而是有人顺手改了 ``--chat-key`` 指向真人会话。
    """
    ck = str(chat_key or "").strip()
    if not ck:
        raise UnsafeTarget("chat_key 为空")
    if ck.lower() == SAVED_MESSAGES:
        return SAVED_MESSAGES
    if not allow_peer:
        raise UnsafeTarget(
            f"目标 {ck!r} 不是 Saved Messages('me')；真人会话需显式 --allow-peer")
    return ck


def make_tag(now: Optional[float] = None) -> str:
    """本轮演练标签 QAHHMMSS——所有断言按它限定，隔离往轮残留。"""
    return "QA" + time.strftime("%H%M%S", time.localtime(now or time.time()))


def count_tagged(texts: List[str], tag: str, marker: str = "") -> int:
    """统计**本轮** TAG 的消息条数（marker 非空时再按子串二次限定）。

    首次实施踩坑：只按 marker 子串统计 → 把上一轮 Saved Messages 里的同类演练
    消息也数进来，误判成「双发」。TAG 限定是判定正确性的前提。
    """
    t = str(tag or "")
    if not t:
        return 0
    return sum(1 for x in texts
               if t in str(x) and (not marker or marker in str(x)))


def expected_dedup_delta(n_first: int, n_repeat: int, n_new_id: int) -> Dict[str, int]:
    """给定提交次数，算出 send_dedup 计数器的**精确**期望增量。

    语义（``SendDedup.reserve``）：仅**首见**占坑记 reserved；窗口内重复命中记
    duplicates 且**不**新增 reserved。首次实施踩坑：误以为「三次提交 → reserved+3」，
    实际是 +2（首发 + 换 id 各一次首见），中间那次算 duplicate。
    """
    return {
        G_RESERVED: int(n_first) + int(n_new_id),
        G_DUPES: int(n_repeat),
    }


# ── HTTP 客户端 ───────────────────────────────────────────────────────────

class DrillClient:
    """带 session cookie + Bearer 头的双凭据客户端（见模块 docstring 的鉴权说明）。"""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))

    @property
    def _bearer(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _raw(self, req: urllib.request.Request, timeout: float) -> Tuple[int, str]:
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, json.dumps({"error": str(e)[:200]})

    def login(self, timeout: float = 30) -> bool:
        """POST /login 用 auth_token 换 session cookie（/login 已在 CSRF 豁免名单）。"""
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(
            self.base + "/login", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        code, _ = self._raw(req, timeout)
        return code in (200, 303) and any(c.name == "session" for c in self._cj)

    def call(self, method: str, path: str, body: Any = None,
             timeout: float = 45) -> Tuple[int, Any]:
        headers = {**self._bearer, "Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        code, raw = self._raw(req, timeout)
        try:
            return code, json.loads(raw)
        except Exception:
            return code, {"raw": raw[:200]}

    def counters(self) -> Dict[str, float]:
        req = urllib.request.Request(
            self.base + "/api/workspace/metrics?format=prometheus",
            headers=self._bearer)
        _, text = self._raw(req, 20)
        out: Dict[str, float] = {}
        for g in (G_RESERVED, G_DUPES, G_HUMAN_OK, G_HUMAN_ERR):
            line = next((ln for ln in text.splitlines()
                         if ln.startswith(g + " ")), "")
            out[g] = float(line.rsplit(" ", 1)[-1]) if line else -1.0
        return out

    def thread_texts(self, account_id: str, chat_key: str) -> List[str]:
        q = urllib.parse.urlencode({
            "platform": "telegram", "account_id": account_id,
            "chat_key": chat_key, "limit": 100})
        code, d = self.call("GET", f"/api/unified-inbox/thread?{q}")
        if code != 200 or not isinstance(d, dict):
            return []
        msgs = d.get("messages") or d.get("thread") or []
        return [str(m.get("text") or "") for m in msgs if isinstance(m, dict)]


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先，与引擎同口径）。"""
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


# ── 演练主体 ─────────────────────────────────────────────────────────────

class Drill:
    def __init__(self, cli: DrillClient, tag: str) -> None:
        self.c = cli
        self.tag = tag
        self.results: List[Tuple[str, bool, str]] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.results.append((name, bool(cond), detail))
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))
        return bool(cond)

    def pick_account(self, want: str = "") -> str:
        code, d = self.c.call("GET", "/api/accounts")
        rows = (d.get("accounts") if isinstance(d, dict) else None) or []
        tg = [a for a in rows
              if a.get("platform") == "telegram" and a.get("running")
              and str(a.get("account_id") or "") not in ("", "default")]
        ids = [str(a.get("account_id")) for a in tg]
        print(f"  在线 telegram 协议号: {ids or '（无）'}")
        if want:
            if want not in ids:
                print(f"  [WARN] 指定账号 {want} 不在在线清单，仍按指定执行")
            return want
        return ids[0] if ids else "default"

    # E1 ---------------------------------------------------------------
    def e1_send_dedup(self, acct: str, ck: str) -> bool:
        print("== E1. 发送幂等真拦截 ==")
        base_body = {
            "platform": "telegram", "account_id": acct, "chat_key": ck,
            "text": f"[验收{self.tag}] 幂等·第一发（应送达）",
            "client_msg_id": f"{self.tag}-dup",
        }
        code, d = self.c.call("POST", "/api/unified-inbox/send", base_body)
        if not self.check("第一发 200 且非 duplicate",
                          code == 200 and d.get("ok") and not d.get("duplicate"),
                          f"code={code} d={str(d)[:120]}"):
            return False
        code, d = self.c.call("POST", "/api/unified-inbox/send", dict(
            base_body, text=f"[验收{self.tag}] 幂等·第二发（不应送达）"))
        self.check("同 id 第二发被拦（duplicate:true）",
                   code == 200 and d.get("duplicate") is True, f"d={str(d)[:110]}")
        code, d = self.c.call("POST", "/api/unified-inbox/send", dict(
            base_body, text=f"[验收{self.tag}] 幂等·换id（应送达）",
            client_msg_id=f"{self.tag}-new"))
        self.check("换 id 放行（不过度拦截）",
                   code == 200 and d.get("ok") and not d.get("duplicate"))
        time.sleep(2)
        texts = self.c.thread_texts(acct, ck)
        self.check("线程实证：本轮真发恰 2 条",
                   count_tagged(texts, self.tag) == 2,
                   f"n={count_tagged(texts, self.tag)}")
        self.check("线程实证：被拦的第二发缺席",
                   count_tagged(texts, self.tag, "第二发") == 0)
        return True

    # E2 ---------------------------------------------------------------
    def e2_draft_race(self, acct: str, ck: str) -> str:
        print("== E2. 草稿双窗互斥 + 人工通过真投递 ==")
        cid = f"telegram:{acct}:{ck}"
        marker = "人工通过真投递"
        code, d = self.c.call("POST", "/api/drafts/persona-test", {
            "conversation_id": cid, "peer_text": "验收",
            "text": f"[验收{self.tag}] {marker}·双窗互斥"})
        draft_id = str(d.get("draft_id") or "") if isinstance(d, dict) else ""
        if not self.check("试聊草稿创建", code == 200 and bool(draft_id),
                          f"code={code} d={str(d)[:120]}"):
            print("  [SKIP] 会话行不存在（先跑 E1 建会话），E2 跳过")
            return ""
        codes: List[int] = []
        lock = threading.Lock()

        def _approve(win: int) -> None:
            c, _ = self.c.call("POST", f"/api/drafts/{draft_id}/resolve",
                               {"action": "approve", "by": f"drill-win{win}"})
            with lock:
                codes.append(c)

        ths = [threading.Thread(target=_approve, args=(i,)) for i in (1, 2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.check("并发双 approve → 恰一路 200 / 一路 409",
                   sorted(codes) == [200, 409], f"codes={codes}")
        n = 0
        for _ in range(15):
            time.sleep(2)
            n = count_tagged(self.c.thread_texts(acct, ck), self.tag, marker)
            if n >= 1:
                break
        self.check("人工通过 → 真投递恰 1 条（防双发）", n == 1, f"n={n}")
        return draft_id

    # E3 ---------------------------------------------------------------
    def e3_counters(self, before: Dict[str, float], had_draft: bool) -> None:
        print("== E3. prometheus 计数器增量互证（权威判定） ==")
        after = self.c.counters()
        delta = {k: after[k] - before.get(k, 0.0) for k in after}
        exp = expected_dedup_delta(n_first=1, n_repeat=1, n_new_id=1)
        self.check(f"send_dedup.reserved +{exp[G_RESERVED]}（仅首见占坑）",
                   delta[G_RESERVED] == exp[G_RESERVED], f"delta={delta}")
        self.check(f"send_dedup.duplicates +{exp[G_DUPES]}",
                   delta[G_DUPES] == exp[G_DUPES])
        if had_draft:
            self.check("autosend.human_delivered +1", delta[G_HUMAN_OK] == 1)
            self.check("autosend.human_deliver_errors +0", delta[G_HUMAN_ERR] == 0)

    def cleanup(self, acct: str, ck: str) -> None:
        print("== 清理：演练会话归档（Saved Messages 内文本需人工手删） ==")
        code, _ = self.c.call("POST", "/api/workspace/batch/archive", {
            "conversation_ids": [f"telegram:{acct}:{ck}"], "archived": True})
        print(f"  archive code={code}")

    def summary(self) -> int:
        fails = [n for n, ok, _ in self.results if not ok]
        total = len(self.results)
        print(f"\n== 演练结果: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="多开治理防线真发演练")
    ap.add_argument("--base", default=DEFAULT_BASE, help="实例地址")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--account", default="", help="演练用 telegram 账号（默认取首个在线协议号）")
    ap.add_argument("--chat-key", default=SAVED_MESSAGES,
                    help="演练目标（默认 me=Saved Messages）")
    ap.add_argument("--allow-peer", action="store_true",
                    help="允许指向真人会话（危险：演练文案会真的发给对方）")
    ap.add_argument("--confirm", action="store_true",
                    help="真发；不给则只做只读预检")
    args = ap.parse_args(argv)

    try:
        chat_key = ensure_safe_target(args.chat_key, allow_peer=args.allow_peer)
    except UnsafeTarget as e:
        print(f"[ABORT] {e}")
        return 2
    if chat_key != SAVED_MESSAGES:
        print(f"[!! 危险 !!] 目标是真人会话 {chat_key}——演练文案会真的发给对方")

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2

    cli = DrillClient(args.base, token)
    print(f"== 0. 登录 {args.base} ==")
    if not cli.login():
        print("[ABORT] 登录失败（token 不匹配 / 实例未起）")
        return 2
    print("  session 已建立")

    tag = make_tag()
    drill = Drill(cli, tag)
    acct = drill.pick_account(args.account)

    if not args.confirm:
        print("\n== 只读预检（未加 --confirm，一条消息都不会发） ==")
        print(f"  将使用账号 {acct} → 目标 {chat_key}，标签 {tag}")
        print(f"  当前计数器: {json.dumps(cli.counters(), ensure_ascii=False)}")
        print("  加 --confirm 执行真发演练。")
        return 0

    before = cli.counters()
    if not drill.e1_send_dedup(acct, chat_key):
        print("[ABORT] 真发通道不可用，如实中止")
        return drill.summary() or 1
    draft_id = drill.e2_draft_race(acct, chat_key)
    drill.e3_counters(before, had_draft=bool(draft_id))
    drill.cleanup(acct, chat_key)
    return drill.summary()


if __name__ == "__main__":
    sys.exit(main())
