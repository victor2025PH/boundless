#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lead Mesh 离线 smoke 脚本 (Phase 6 · 2026-04-23)。

**零真机依赖** — 通过 HTTP API 合成数据, 验证从 lead 创建 → journey 写入 →
handoff 自动路由 → webhook 入队 → Dashboard 查询端点的全链路数据流。

用途:
  * 新部署上线前的冒烟 (任一 Lead Mesh API 挂了立即发现)
  * A/B 机 Git merge 后快速检查集成没坏
  * CI 环境定时跑(需要 server 在线)

用法::

    # 前提: server 已在 http://localhost:18080 运行
    python scripts/smoke_lead_mesh.py
    python scripts/smoke_lead_mesh.py --base http://w03.example.com:18080
    python scripts/smoke_lead_mesh.py --api-key <your-key>
    python scripts/smoke_lead_mesh.py --verbose

退出码:
  0 全部通过
  1 任一验证失败 (打印失败点)
  2 server 不可用 / 网络错误
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

# Windows cmd 默认 GBK 编码, 直接 print unicode 符号会 UnicodeEncodeError。
# 强制 stdout/stderr 用 UTF-8, 允许 ✓/✗/日文/emoji 正常输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # Python < 3.7 或管道场景: 退回 TextIOWrapper wrap
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                    errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                    errors="replace")

try:
    import requests
except ImportError:
    print("FATAL: pip install requests", file=sys.stderr)
    sys.exit(2)


# ─── 终端颜色辅助 (不要求 colorama) ──────────────────────────────────
class _C:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    INFO = "\033[94m"
    DIM = "\033[90m"
    BOLD = "\033[1m"
    END = "\033[0m"


def _log_pass(msg: str, verbose: bool = False):
    if verbose:
        print(f"  {_C.OK}✓{_C.END} {msg}")
    else:
        sys.stdout.write(f"{_C.OK}.{_C.END}")
        sys.stdout.flush()


def _log_fail(msg: str):
    print(f"\n  {_C.FAIL}✗ FAIL: {msg}{_C.END}")


def _log_section(title: str):
    print(f"\n{_C.BOLD}━━━ {title} ━━━{_C.END}")


# ─── SmokeRunner 主控 ────────────────────────────────────────────────
class SmokeRunner:
    def __init__(self, base_url: str, api_key: str = "",
                  verbose: bool = False):
        self.base = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["X-API-Key"] = api_key
        self.verbose = verbose
        self.failures: List[str] = []
        # 运行时暂存的资源 id, 用于清理
        self.created_cids: List[str] = []
        self.created_handoffs: List[str] = []
        self.created_receivers: List[str] = []

    def _req(self, method: str, path: str, **kwargs) -> Any:
        url = self.base + path
        try:
            r = requests.request(method, url, headers=self.headers,
                                  timeout=15, **kwargs)
        except Exception as e:
            raise RuntimeError(f"{method} {path} 网络异常: {e}")
        if r.status_code >= 400:
            body = r.text[:300]
            raise RuntimeError(f"{method} {path} → HTTP {r.status_code}: {body}")
        if not r.text:
            return None
        try:
            return r.json()
        except Exception:
            return r.text

    def assert_true(self, cond: bool, msg: str):
        if cond:
            _log_pass(msg, self.verbose)
        else:
            _log_fail(msg)
            self.failures.append(msg)

    def assert_eq(self, actual, expected, msg: str):
        if actual == expected:
            _log_pass(f"{msg} (= {expected!r})", self.verbose)
        else:
            _log_fail(f"{msg} expected={expected!r} actual={actual!r}")
            self.failures.append(msg)

    # ─── 1. 连通性 ─────────────────────────────────────────────────
    def check_health(self):
        _log_section("1. 连通性 / 健康检查")
        try:
            r = self._req("GET", "/health")
            self.assert_true(isinstance(r, dict) and r.get("status") == "ok",
                              "/health 返回 status=ok")
        except Exception as e:
            _log_fail(f"/health 无法访问: {e}")
            self.failures.append("server unreachable")
            return False
        return True

    # ─── 2. Lead 身份解析 + journey ────────────────────────────────
    def check_identity_and_journey(self):
        _log_section("2. Lead 身份解析 + Journey")
        # 2a. 创建新 lead
        peer = f"smoke_peer_{uuid.uuid4().hex[:8]}"
        r = self._req("POST", "/lead-mesh/leads/resolve", json={
            "platform": "facebook", "account_id": f"fb:{peer}",
            "display_name": peer, "language": "ja",
            "persona_key": "jp_female_midlife",
            "discovered_via": "smoke_test",
            "discovered_by_device": "smoke_dev",
        })
        cid = r.get("canonical_id") if r else ""
        self.assert_true(bool(cid), f"resolve_identity 创建 lead (cid={cid[:12] if cid else '-'})")
        if not cid:
            return None
        self.created_cids.append(cid)

        # 2b. 查 dossier
        d = self._req("GET", f"/lead-mesh/leads/{cid}")
        self.assert_true(isinstance(d, dict) and d.get("canonical"),
                          "get_dossier 返回非空")
        self.assert_eq(d["canonical"]["canonical_id"], cid,
                        "dossier.canonical.canonical_id 匹配")
        self.assert_eq(d["canonical"]["primary_name"], peer,
                        "primary_name 正确填充")

        # 2c. journey 有 extracted 事件 (resolve 自动写)
        journey = d.get("journey") or []
        actions = [e.get("action") for e in journey]
        self.assert_true("extracted" in actions,
                          f"journey 含 extracted 事件 (全部={actions[:5]})")

        # 2d. 同 identity 再 resolve 不会新建 lead
        r2 = self._req("POST", "/lead-mesh/leads/resolve", json={
            "platform": "facebook", "account_id": f"fb:{peer}",
            "display_name": peer,
        })
        self.assert_eq(r2.get("canonical_id"), cid,
                        "硬匹配相同 identity 返回同 canonical_id")

        return cid

    # ─── 3. Handoff 状态机 ────────────────────────────────────────
    def check_handoff_lifecycle(self, cid: str):
        _log_section("3. Handoff 状态机")
        # 3a. 创建 handoff (无 receiver, 无 webhook)
        r = self._req("POST", "/lead-mesh/handoffs", json={
            "canonical_id": cid,
            "source_agent": "agent_b",
            "source_device": "smoke_dev",
            "channel": "line",
            "snippet_sent": "LINE: @smoke_test",
            "conversation_snapshot": [
                {"direction": "incoming", "text": "你好, +81 90-1234-5678"},
                {"direction": "outgoing", "text": "LINE: @id01"},
            ],
            "enqueue_webhook": False,
        })
        hid = r.get("handoff_id") if r else ""
        self.assert_true(bool(hid), f"create_handoff (hid={hid[:12]})")
        if not hid:
            return None
        self.created_handoffs.append(hid)

        # 3b. 查 handoff, 检查脱敏
        h = self._req("GET", f"/lead-mesh/handoffs/{hid}")
        self.assert_eq(h.get("state"), "pending", "handoff state=pending")
        snap = h.get("conversation_snapshot") or []
        self.assert_true(len(snap) == 2, "snapshot 有 2 条")
        # 脱敏应命中
        txt = snap[0].get("text") or ""
        self.assert_true("[PHONE]" in txt, f"incoming 手机号已脱敏 ({txt})")
        txt2 = snap[1].get("text") or ""
        self.assert_true("[LINE_ID]" in txt2, f"outgoing LINE ID 已脱敏 ({txt2})")

        # 3c. 状态转移 pending → ack → completed
        self._req("POST", f"/lead-mesh/handoffs/{hid}/acknowledge",
                   json={"by": "smoke_test"})
        h2 = self._req("GET", f"/lead-mesh/handoffs/{hid}")
        self.assert_eq(h2.get("state"), "acknowledged", "ack 后 state=acknowledged")

        self._req("POST", f"/lead-mesh/handoffs/{hid}/complete",
                   json={"by": "smoke_test"})
        h3 = self._req("GET", f"/lead-mesh/handoffs/{hid}")
        self.assert_eq(h3.get("state"), "completed", "complete 后 state=completed")

        # 3d. 去重检查 — 同 channel 再创建应被 check-duplicate 识别
        dup = self._req("GET", f"/lead-mesh/handoffs/check-duplicate?canonical_id={cid}&channel=line")
        self.assert_true(dup.get("is_duplicate") is True,
                          "check-duplicate 正确识别已有 completed handoff")

        return hid

    # ─── 4. Agent Mesh 双通道 ────────────────────────────────────
    def check_agent_mesh(self):
        _log_section("4. Agent Mesh 消息通道")
        cid = str(uuid.uuid4())
        # 4a. notification
        r = self._req("POST", "/lead-mesh/agents/messages", json={
            "from_agent": "smoke_a",
            "to_agent": "smoke_b",
            "canonical_id": cid,
            "message_type": "notification",
            "payload": {"event": "smoke_test", "ts": time.time()},
        })
        cid_msg = r.get("correlation_id") if r else ""
        self.assert_true(bool(cid_msg), f"send notification (cid={cid_msg[:8]})")

        # 4b. 拉 pending 应能看到
        msgs = self._req("GET", "/lead-mesh/agents/messages?to_agent=smoke_b&limit=50")
        items = (msgs.get("messages") if msgs else []) or []
        found = [m for m in items if m.get("correlation_id") == cid_msg]
        self.assert_true(len(found) == 1, "poll 能收到刚发的消息")
        if found:
            msg_id = found[0].get("id")
            # 4c. mark delivered
            self._req("POST", f"/lead-mesh/agents/messages/{msg_id}/deliver", json={})
            self._req("POST", f"/lead-mesh/agents/messages/{msg_id}/ack", json={})

    # ─── 5. Receivers 配置 + 自动 pick ────────────────────────────
    def check_receivers(self):
        _log_section("5. Receivers 配置 + 自动 pick")
        # 5a. 创建两个测试 receiver
        key_a = f"smoke_rx_a_{uuid.uuid4().hex[:6]}"
        key_b = f"smoke_rx_b_{uuid.uuid4().hex[:6]}"
        try:
            r = self._req("POST", f"/lead-mesh/receivers/{key_a}", json={
                "channel": "line", "account_id": "@smoke_a",
                "daily_cap": 5, "enabled": True,
                "persona_filter": ["jp_female_midlife"],
                "tags": ["smoke"],
            })
            self.assert_true(r.get("ok"), f"upsert receiver {key_a}")
            self.created_receivers.append(key_a)

            r = self._req("POST", f"/lead-mesh/receivers/{key_b}", json={
                "channel": "line", "account_id": "@smoke_b",
                "daily_cap": 20, "enabled": True,
                "persona_filter": ["jp_female_midlife"],
                "tags": ["smoke"],
            })
            self.assert_true(r.get("ok"), f"upsert receiver {key_b}")
            self.created_receivers.append(key_b)

            # 5b. pick 应选 remaining 多的 (B 的 20 > A 的 5)
            pk = self._req(
                "GET",
                "/lead-mesh/receivers-pick?channel=line&persona_key=jp_female_midlife")
            picked = (pk.get("picked") or {}).get("key")
            self.assert_eq(picked, key_b,
                            f"pick_receiver 选 remaining 多的 ({key_b})")

            # 5c. 禁用 B, 应降级到 A
            self._req("POST", f"/lead-mesh/receivers/{key_b}",
                       json={"enabled": False})
            pk2 = self._req(
                "GET",
                "/lead-mesh/receivers-pick?channel=line&persona_key=jp_female_midlife")
            picked2 = (pk2.get("picked") or {}).get("key")
            self.assert_eq(picked2, key_a, f"B 禁用后 fallback 到 {key_a}")

            # 5d. 列表 API
            rlist = self._req("GET", "/lead-mesh/receivers")
            keys = {r["key"] for r in (rlist.get("receivers") or [])}
            self.assert_true(key_a in keys and key_b in keys,
                              "list_receivers 含测试 receiver")
        finally:
            # 清理
            for k in list(self.created_receivers):
                try:
                    self._req("DELETE", f"/lead-mesh/receivers/{k}")
                except Exception:
                    pass
            self.created_receivers = []

    # ─── 6. create_handoff 自动路由 ────────────────────────────────
    def check_handoff_auto_route(self, cid: str):
        _log_section("6. create_handoff 自动路由到 receiver")
        # 先创建一个 receiver
        key = f"smoke_rx_route_{uuid.uuid4().hex[:6]}"
        try:
            self._req("POST", f"/lead-mesh/receivers/{key}", json={
                "channel": "telegram", "account_id": "@smoke_tg",
                "daily_cap": 10, "enabled": True,
                "persona_filter": ["jp_female_midlife"],
            })
            self.created_receivers.append(key)

            # 创建 handoff, 不指定 receiver_account_key, 应自动 pick
            r = self._req("POST", "/lead-mesh/handoffs", json={
                "canonical_id": cid,
                "source_agent": "smoke_b",
                "channel": "telegram",
                "persona_key": "jp_female_midlife",
                "snippet_sent": "TG: @smoke_tg",
                "enqueue_webhook": False,
            })
            hid = r.get("handoff_id")
            self.assert_true(bool(hid), "创建 handoff (含 auto_pick)")
            self.created_handoffs.append(hid)

            h = self._req("GET", f"/lead-mesh/handoffs/{hid}")
            self.assert_eq(h.get("receiver_account_key"), key,
                            f"auto_pick 正确路由到 {key}")
        finally:
            try:
                self._req("DELETE", f"/lead-mesh/receivers/{key}")
            except Exception:
                pass

    # ─── 7. Dashboard API 响应 ────────────────────────────────────
    def check_dashboard_apis(self):
        _log_section("7. Dashboard API 响应 (Phase 5.5)")
        # 列几个关键端点, 只要不 5xx 就算通过(业务已在前面校验过)
        for name, path in [
            ("handoffs list pending", "/lead-mesh/handoffs?state=pending"),
            ("handoffs list completed", "/lead-mesh/handoffs?state=completed"),
            ("search leads (empty)", "/lead-mesh/leads/search?name_like=nonexistent_xyz"),
            ("dead letters", "/lead-mesh/webhooks/dead-letters"),
        ]:
            try:
                self._req("GET", path)
                _log_pass(f"{name}: OK", self.verbose)
            except Exception as e:
                _log_fail(f"{name}: {e}")
                self.failures.append(f"dashboard api {name}")

    # ─── Main runner ──────────────────────────────────────────────
    def run(self) -> int:
        print(f"{_C.BOLD}Lead Mesh 离线 Smoke Test{_C.END}")
        print(f"  Server: {self.base}")
        print(f"  API Key: {'✓' if self.headers.get('X-API-Key') else '(无)'}")

        if not self.check_health():
            print(f"\n{_C.FAIL}server 不可用, 放弃{_C.END}")
            return 2

        cid = self.check_identity_and_journey()
        if cid:
            self.check_handoff_lifecycle(cid)
            self.check_handoff_auto_route(cid)
        self.check_agent_mesh()
        self.check_receivers()
        self.check_dashboard_apis()

        print()
        if self.failures:
            print(f"{_C.FAIL}✗ Smoke FAILED — {len(self.failures)} 处不通过:{_C.END}")
            for f in self.failures:
                print(f"  - {f}")
            return 1

        print(f"{_C.OK}{_C.BOLD}✓ 全部通过{_C.END}")
        print(f"{_C.DIM}  cids={len(self.created_cids)} "
              f"handoffs={len(self.created_handoffs)} "
              f"(未自动清理, 如需可去 DB 删除){_C.END}")
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://localhost:18080",
                     help="server base URL")
    ap.add_argument("--api-key", default="", help="X-API-Key (如果启用)")
    ap.add_argument("--verbose", "-v", action="store_true",
                     help="每个 assert 都打印一行")
    args = ap.parse_args()

    runner = SmokeRunner(args.base, api_key=args.api_key, verbose=args.verbose)
    sys.exit(runner.run())


if __name__ == "__main__":
    main()
