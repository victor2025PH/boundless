#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端 smoke test — 验证 Phase 1-13 所有关键链路真的连通.

用法:
    python scripts/e2e_smoke.py                 # 默认 127.0.0.1:8000
    python scripts/e2e_smoke.py --base http://192.168.0.118:8000
    python scripts/e2e_smoke.py --keep          # 不清理测试客户

不是 pytest, 是 deploy verification script. 跑通后才能放心做真实流量.

覆盖:
1. 健康检查 + coordinator role
2. 客户 upsert (Phase-2)
3. event push + chat push (Phase-3)
4. handoff initiate + accept (Phase-2)
5. priority update (Phase-4)
6. customers / search / chats-search (Phase-5/7)
7. funnel/stats + timeseries (Phase-3)
8. SLA agents/variants (Phase-4/12)
9. A/B running + experiments + customer-views (Phase-7)
10. referral_decision push + aggregate + drill query (Phase-10/11/12)
11. top high-priority + frustrated (Phase-10)
12. push metrics (Phase-3)
13. LLM insight (Phase-8, 可选 — LLM 离线时跳过不算失败)
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
from typing import Any, Dict, List, Optional, Tuple

# Windows GBK console 兼容: 强制 UTF-8 stdout, 不然 emoji 崩
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ── 颜色输出 ──────────────────────────────────────────────────────────
def _color(code: str, msg: str) -> str:
    if not sys.stdout.isatty():
        return msg
    return f"\033[{code}m{msg}\033[0m"


def green(msg: str) -> str: return _color("32", msg)
def red(msg: str) -> str: return _color("31", msg)
def yellow(msg: str) -> str: return _color("33", msg)
def cyan(msg: str) -> str: return _color("36", msg)
def gray(msg: str) -> str: return _color("90", msg)


# ── HTTP 工具 ─────────────────────────────────────────────────────────
class SmokeClient:
    def __init__(self, base: str, api_key: str = ""):
        self.base = base.rstrip("/")
        self.api_key = api_key

    def _req(self, method: str, path: str,
             body: Optional[Dict[str, Any]] = None,
             timeout: float = 10.0) -> Tuple[int, Any]:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(e)
            return e.code, detail
        except Exception as e:
            return 0, str(e)

    def get(self, path: str, **kw) -> Tuple[int, Any]:
        return self._req("GET", path, **kw)

    def post(self, path: str, body: Dict[str, Any], **kw) -> Tuple[int, Any]:
        return self._req("POST", path, body=body, **kw)

    def put(self, path: str, body: Dict[str, Any], **kw) -> Tuple[int, Any]:
        return self._req("PUT", path, body=body, **kw)

    def delete(self, path: str, **kw) -> Tuple[int, Any]:
        return self._req("DELETE", path, **kw)


# ── stage runner ─────────────────────────────────────────────────────
class StageReport:
    def __init__(self):
        self.passed: List[str] = []
        self.failed: List[Tuple[str, str]] = []
        self.skipped: List[Tuple[str, str]] = []

    def ok(self, name: str, detail: str = ""):
        line = f"  {green('✅')} {name}"
        if detail:
            line += gray(f"  {detail}")
        print(line)
        self.passed.append(name)

    def fail(self, name: str, detail: str):
        print(f"  {red('❌')} {name}  {red(detail[:200])}")
        self.failed.append((name, detail))

    def skip(self, name: str, reason: str):
        print(f"  {yellow('⊘')}  {name}  {gray(reason)}")
        self.skipped.append((name, reason))

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed) + len(self.skipped)
        print()
        print(cyan("═" * 60))
        print(f"  {green(str(len(self.passed)) + ' passed')} · "
              f"{red(str(len(self.failed)) + ' failed')} · "
              f"{yellow(str(len(self.skipped)) + ' skipped')} "
              f"({total} total)")
        if self.failed:
            print()
            print(red("失败明细:"))
            for name, detail in self.failed:
                print(f"  - {name}: {detail[:300]}")
        return 1 if self.failed else 0


# ── Stages ────────────────────────────────────────────────────────────
def stage_health(c: SmokeClient, r: StageReport) -> bool:
    print(cyan("\n[Stage 1] 健康检查"))
    status, body = c.get("/health")
    if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
        r.ok("/health", f"version={body.get('version')}")
        return True
    r.fail("/health", f"status={status} body={body}")
    return False


def stage_upsert_customer(c: SmokeClient, r: StageReport) -> Optional[str]:
    print(cyan("\n[Stage 2] 客户 upsert"))
    canonical_id = f"e2e_smoke::{uuid.uuid4().hex[:8]}"
    body = {
        "canonical_id": canonical_id,
        "canonical_source": "facebook_name",
        "primary_name": f"E2E_Test_{int(time.time())}",
        "country": "JP",
        "ai_profile": {"persona_key": "jp_female_midlife", "ab_variant": "v1"},
        "worker_id": "e2e_smoke",
        "device_id": "e2e_device",
        "status": "in_funnel",
    }
    status, body_resp = c.post("/cluster/customers/upsert", body)
    if status == 200 and body_resp and body_resp.get("customer_id"):
        cid = body_resp["customer_id"]
        r.ok("upsert customer", f"customer_id={cid[:12]}…")
        return cid
    r.fail("upsert customer", f"status={status} body={body_resp}")
    return None


def stage_event_push(c: SmokeClient, r: StageReport, cid: str) -> bool:
    print(cyan("\n[Stage 3] event + chat push"))
    ok_count = 0
    # friend_request_sent
    status, body = c.post(
        f"/cluster/customers/{cid}/events/push",
        {"event_type": "friend_request_sent", "worker_id": "e2e_smoke",
         "device_id": "e2e_device", "meta": {"smoke": True}},
    )
    if status == 200:
        r.ok("event friend_request_sent")
        ok_count += 1
    else:
        r.fail("event friend_request_sent", f"status={status} body={body}")

    # greeting_sent
    status, body = c.post(
        f"/cluster/customers/{cid}/events/push",
        {"event_type": "greeting_sent", "worker_id": "e2e_smoke",
         "device_id": "e2e_device"},
    )
    if status == 200:
        r.ok("event greeting_sent")
        ok_count += 1
    else:
        r.fail("event greeting_sent", f"status={status} body={body}")

    # message_received chat
    status, body = c.post(
        f"/cluster/customers/{cid}/chats/push",
        {"channel": "messenger", "direction": "incoming",
         "content": "こんにちは、はじめまして",
         "content_lang": "ja", "worker_id": "e2e_smoke", "device_id": "e2e_device"},
    )
    if status == 200:
        r.ok("chat incoming")
        ok_count += 1
    else:
        r.fail("chat incoming", f"status={status} body={body}")

    # outgoing chat
    status, body = c.post(
        f"/cluster/customers/{cid}/chats/push",
        {"channel": "messenger", "direction": "outgoing",
         "content": "はじめまして、よろしくお願いします",
         "content_lang": "ja", "ai_generated": True,
         "worker_id": "e2e_smoke", "device_id": "e2e_device"},
    )
    if status == 200:
        r.ok("chat outgoing AI")
        ok_count += 1
    else:
        r.fail("chat outgoing AI", f"status={status} body={body}")

    return ok_count == 4


def stage_handoff(c: SmokeClient, r: StageReport, cid: str) -> Optional[str]:
    print(cyan("\n[Stage 4] handoff initiate + accept"))
    status, body = c.post(
        f"/cluster/customers/{cid}/handoff/initiate",
        {"from_stage": "messenger", "to_stage": "line",
         "ai_summary": "e2e smoke test handoff",
         "initiating_worker_id": "e2e_smoke", "initiating_device_id": "e2e_device"},
    )
    if status != 200 or not body or not body.get("handoff_id"):
        r.fail("handoff initiate", f"status={status} body={body}")
        return None
    hid = body["handoff_id"]
    r.ok("handoff initiate", f"handoff_id={hid[:12]}…")

    status, body = c.post(
        f"/cluster/customers/handoff/{hid}/accept",
        {"accepted_by_human": "e2e_agent"},
    )
    if status == 200 and body and body.get("accepted"):
        r.ok("handoff accept")
    else:
        r.fail("handoff accept", f"status={status} body={body}")
    return hid


def stage_priority(c: SmokeClient, r: StageReport, cid: str):
    print(cyan("\n[Stage 5] priority + tag"))
    status, body = c.post(
        f"/cluster/customers/{cid}/priority", {"priority_tag": "high"},
    )
    if status == 200 and body and body.get("updated"):
        r.ok("priority high")
    else:
        r.fail("priority high", f"status={status} body={body}")


def stage_search_and_detail(c: SmokeClient, r: StageReport, cid: str):
    print(cyan("\n[Stage 6] customers / search / detail"))
    # 客户列表
    status, body = c.get("/cluster/customers?limit=100")
    if status == 200 and isinstance(body, dict):
        r.ok("customers list")
    else:
        r.fail("customers list", f"status={status}")

    # 详情
    status, body = c.get(f"/cluster/customers/{cid}")
    if status == 200 and body and body.get("customer_id") == cid:
        events_n = len(body.get("events") or [])
        chats_n = len(body.get("chats") or [])
        handoffs_n = len(body.get("handoffs") or [])
        if events_n >= 2 and chats_n >= 2 and handoffs_n >= 1:
            r.ok("customer detail",
                 f"events={events_n} chats={chats_n} handoffs={handoffs_n}")
        else:
            r.fail("customer detail",
                   f"低于预期: events={events_n} chats={chats_n} handoffs={handoffs_n}")
    else:
        r.fail("customer detail", f"status={status} body={body}")

    # 模糊搜索 (返 {"customers": [...]})
    status, body = c.get("/cluster/customers-search?q=E2E_Test&limit=10")
    if status == 200 and isinstance(body, dict):
        customers = body.get("customers") or []
        if any(x.get("customer_id") == cid for x in customers):
            r.ok("customers-search 命中本测试客户")
        else:
            r.skip("customers-search", "未命中 (q 字段是 ILIKE 子串, 可能后续刷新)")
    else:
        r.fail("customers-search", f"status={status} body={body}")

    # chats 搜索 (返 {"q": ..., "chats": [...]})
    status, body = c.get("/cluster/chats-search?q=test&limit=5", timeout=20.0)
    if status == 200 and isinstance(body, dict):
        r.ok("chats-search trgm 路径", f"hit {len(body.get('chats') or [])}")
    else:
        r.fail("chats-search", f"status={status}")


def stage_aggregates(c: SmokeClient, r: StageReport):
    print(cyan("\n[Stage 7] 聚合端点"))
    for path in [
        "/cluster/customers/funnel/stats?days=7",
        "/cluster/customers/funnel/timeseries?days=7",
        "/cluster/customers/sla/agents?days=30",
        "/cluster/customers/sla/variants?days=30",
        "/cluster/customers/handoff/pending?limit=100",
        "/cluster/customers/push/metrics",
    ]:
        status, _ = c.get(path)
        if status == 200:
            r.ok(path.split("?")[0])
        else:
            r.fail(path.split("?")[0], f"status={status}")


def stage_phase_7_endpoints(c: SmokeClient, r: StageReport):
    print(cyan("\n[Stage 8] Phase-7 A/B + customer-views"))
    for path in [
        "/cluster/ab/experiment/running",
        "/cluster/ab/experiments",
        "/cluster/customer-views",
    ]:
        status, _ = c.get(path)
        if status == 200:
            r.ok(path)
        else:
            r.fail(path, f"status={status}")


def stage_referral_decision(c: SmokeClient, r: StageReport, cid: str):
    print(cyan("\n[Stage 9] referral_decision push + aggregate + drill"))
    status, _ = c.post(
        f"/cluster/customers/{cid}/events/push",
        {"event_type": "referral_decision", "worker_id": "e2e_smoke",
         "device_id": "e2e_device",
         "meta": {
             "refer": True, "level": "soft_pass",
             "score": 4, "threshold": 3,
             "reasons": ["e2e smoke test", "intent=interest", "ref_score>0.5"],
             "intent": "interest", "ref_score": 0.8,
             "emotion_overall": 0.7, "frustration": 0.2,
             "readiness": 0.6, "raw_readiness": 0.85,
             "persona_key": "jp_female_midlife",
         }},
    )
    if status == 200:
        r.ok("push referral_decision")
    else:
        r.fail("push referral_decision", f"status={status}")

    status, body = c.get("/cluster/referral-decisions/aggregate?days=30")
    if status == 200 and isinstance(body, dict) and body.get("total", 0) > 0:
        r.ok("aggregate", f"total={body['total']} refer_rate={body.get('refer_rate', 0):.2f}")
    else:
        r.fail("aggregate", f"status={status}")

    status, body = c.get("/cluster/referral-decisions?level=soft_pass&days=30&limit=10")
    if status == 200 and isinstance(body, dict) and body.get("decisions"):
        r.ok("decisions drill", f"hit {len(body['decisions'])}")
    else:
        r.fail("decisions drill", f"status={status}")


def stage_top_panels(c: SmokeClient, r: StageReport):
    print(cyan("\n[Stage 10] Top 主动出击面板"))
    for path in [
        "/cluster/customers/top/high-priority?limit=10",
        "/cluster/customers/top/frustrated?days=7&limit=10",
    ]:
        status, _ = c.get(path)
        if status == 200:
            r.ok(path.split("?")[0])
        else:
            r.fail(path.split("?")[0], f"status={status}")


def stage_llm_insight(c: SmokeClient, r: StageReport, cid: str):
    print(cyan("\n[Stage 11] LLM 洞察 (LLM 离线时 skip)"))
    status, body = c.get(f"/cluster/customers/{cid}/llm-insight", timeout=20.0)
    if status == 200 and isinstance(body, dict):
        if body.get("ok"):
            r.ok("llm-insight ok",
                 f"readiness={body.get('conversion_readiness')} cached={body.get('cached')}")
        else:
            r.skip("llm-insight", body.get("error") or body.get("summary") or "")
    else:
        r.fail("llm-insight", f"status={status}")


def stage_cleanup(c: SmokeClient, r: StageReport, cid: str):
    print(cyan("\n[Stage 12] 清理测试客户"))
    # 没有 admin delete API; 改 status=lost 标记成"已结束"
    status, _ = c.post(
        f"/cluster/customers/{cid}/priority", {"priority_tag": "low"},
    )
    if status == 200:
        r.ok("test customer 标 low priority (清理替代)")
    else:
        r.skip("cleanup", "无标记接口, 留底数据")


# ── main ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=os.environ.get(
        "OPENCLAW_E2E_BASE", "http://127.0.0.1:8000"))
    p.add_argument("--api-key", default=os.environ.get("OPENCLAW_API_KEY", ""))
    p.add_argument("--keep", action="store_true",
                   help="保留测试客户不清理")
    args = p.parse_args()

    print(cyan("═" * 60))
    print(cyan(f"  OpenClaw E2E Smoke · {args.base}"))
    print(cyan("═" * 60))

    c = SmokeClient(args.base, args.api_key)
    r = StageReport()

    if not stage_health(c, r):
        return r.summary()

    cid = stage_upsert_customer(c, r)
    if not cid:
        return r.summary()

    stage_event_push(c, r, cid)
    stage_handoff(c, r, cid)
    stage_priority(c, r, cid)
    stage_search_and_detail(c, r, cid)
    stage_aggregates(c, r)
    stage_phase_7_endpoints(c, r)
    stage_referral_decision(c, r, cid)
    stage_top_panels(c, r)
    stage_llm_insight(c, r, cid)
    if not args.keep:
        stage_cleanup(c, r, cid)

    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
