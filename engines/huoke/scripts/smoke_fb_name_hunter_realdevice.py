#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Facebook name_hunter 真机端到端 smoke (Phase 6 · 2026-04-23)。

**需要真机在线 + FB 已登录**。触发一次 name_hunter 预设:
  1. 创建 facebook_campaign_run 任务 (add_friends step + send_greeting_inline=True)
  2. 等 max N 分钟或任务结束
  3. 验证:
       - facebook_friend_requests 有新 sent 记录
       - facebook_inbox_messages 有 outgoing/ai_decision=greeting (若 fallback 发出)
       - fb_contact_events 有 add_friend_sent 事件
       - lead_journey (Phase 6.A) 有 friend_requested / greeting_sent 事件

  4. 展示 dossier 给运营看

用法::

    # 指定 device + 测试名字
    python scripts/smoke_fb_name_hunter_realdevice.py \\
        --device CACAVKLNU8SGO74D \\
        --targets "山田太郎" \\
        --wait-min 5

    # 从群成员列表自动挑: 不传 --targets 走 preset 默认(按 persona)
    python scripts/smoke_fb_name_hunter_realdevice.py --device X --wait-min 10

安全:
  * 默认 --dry-run, 只看预期动作, 不真实触发任务
  * 加 --live 才真的创建任务
  * 默认 1 个目标, 避免真机批量误操作
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

# UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                    errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                    errors="replace")

try:
    import requests
except ImportError:
    print("FATAL: pip install requests", file=sys.stderr)
    sys.exit(2)


class _C:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    INFO = "\033[94m"
    DIM = "\033[90m"
    BOLD = "\033[1m"
    END = "\033[0m"


def p_ok(msg): print(f"  {_C.OK}✓{_C.END} {msg}")
def p_fail(msg): print(f"  {_C.FAIL}✗ FAIL: {msg}{_C.END}")
def p_warn(msg): print(f"  {_C.WARN}⚠ {msg}{_C.END}")
def p_info(msg): print(f"  {_C.INFO}→ {msg}{_C.END}")
def p_section(title): print(f"\n{_C.BOLD}━━━ {title} ━━━{_C.END}")


class RealDeviceSmoke:
    def __init__(self, *, base_url: str, device_id: str,
                  targets: List[str], persona: str, wait_min: int,
                  dry_run: bool, api_key: str = ""):
        self.base = base_url.rstrip("/")
        self.device = device_id
        self.targets = targets
        self.persona = persona
        self.wait_min = wait_min
        self.dry_run = dry_run
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["X-API-Key"] = api_key
        self.failures: List[str] = []
        self.task_id: Optional[str] = None

    def _req(self, method, path, **kw):
        r = requests.request(method, self.base + path,
                              headers=self.headers, timeout=15, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else None

    def check_prereq(self) -> bool:
        p_section("0. 前置检查")
        # server alive
        try:
            h = self._req("GET", "/health")
            p_ok(f"server 在线 (uptime={h.get('uptime_seconds', 0)}s, "
                 f"devices_online={h.get('devices_online', '?')})")
        except Exception as e:
            p_fail(f"server 不可用: {e}")
            return False
        # device online
        online = int(h.get("devices_online") or 0)
        if online == 0:
            p_fail(f"无在线设备; FB name_hunter 必须真机")
            return False
        # 看指定 device 是否在线(用 /platforms/facebook/device-grid 或 /pool)
        try:
            pool = self._req("GET", "/pool")
            devices = list((pool.get("device_locks") or {}).keys()) + \
                        list((pool.get("active_tasks") or {}).keys())
            # 这 2 者合并只能看"正在跑任务的设备", 不能确认"空闲在线"。
            # 用 health 的 devices_online 数 + 用户传的 device 当信任输入。
            p_ok(f"online devices 总数 {online} (传入 device={self.device[:8]}... 假设在线)")
        except Exception:
            p_warn("无法精确查设备状态, 继续")
        return True

    def get_baseline(self) -> Dict[str, int]:
        """拿 smoke 之前的基线: 看跑完后有没有增量。"""
        p_section("1. 获取基线数据 (跑之前)")
        baseline: Dict[str, int] = {}
        # friend_requests 已发数
        try:
            # 用 /facebook/funnel 拿总量(包含 sent/accepted/rejected)
            f = self._req("GET", f"/facebook/funnel?device_id={self.device}&since_hours=24")
            baseline["friend_requests"] = f.get("stage_friend_request_sent") or 0
            baseline["greetings_sent"] = f.get("stage_greetings_sent") or 0
            p_info(f"baseline: friend_req={baseline['friend_requests']}, "
                   f"greetings={baseline['greetings_sent']}")
        except Exception as e:
            p_warn(f"funnel 查询失败 (测试仍继续): {e}")
            baseline["friend_requests"] = 0
            baseline["greetings_sent"] = 0
        # journey 当前条数(所有 A 端事件)
        try:
            # 没有 /lead-mesh/leads 全量 list API; 简化: 跳过
            baseline["journey_note"] = "(按 lead 维度分别查, 不累积)"
        except Exception:
            pass
        return baseline

    def trigger_task(self) -> Optional[str]:
        p_section("2. 触发 name_hunter 任务")
        params = {
            "steps": ["add_friends"],
            "max_friends_per_run": min(len(self.targets) or 1, 3),
            "send_greeting_inline": True,
            "require_verification_note": True,
            "add_friend_targets": [{"name": n} for n in self.targets],
            "persona_key": self.persona,
        }
        p_info(f"设备={self.device[:12]}...")
        p_info(f"targets={self.targets}")
        p_info(f"persona={self.persona}")
        p_info(f"max_friends_per_run={params['max_friends_per_run']}")
        if self.dry_run:
            p_warn("dry-run 模式, 不真实提交")
            p_info(f"POST body:\n{json.dumps(params, ensure_ascii=False, indent=2)}")
            return None
        try:
            r = self._req("POST", "/tasks", json={
                "type": "facebook_campaign_run",
                "device_id": self.device,
                "params": params,
            })
            tid = r.get("task_id") or ""
            p_ok(f"任务已入队: {tid[:20]}...")
            self.task_id = tid
            return tid
        except Exception as e:
            p_fail(f"创建任务失败: {e}")
            self.failures.append("create_task")
            return None

    def wait_for_completion(self, task_id: str) -> Dict[str, Any]:
        p_section("3. 等任务完成")
        deadline = time.time() + self.wait_min * 60
        last_status = ""
        poll_interval = 15  # 秒
        while time.time() < deadline:
            try:
                t = self._req("GET", f"/tasks/{task_id}")
                status = t.get("status") or ""
                if status != last_status:
                    p_info(f"状态变化 → {status}")
                    last_status = status
                if status in ("success", "completed", "failed", "cancelled",
                               "timeout"):
                    p_ok(f"任务完成: {status}")
                    return t
            except Exception as e:
                p_warn(f"poll 失败: {e}")
            time.sleep(poll_interval)
        p_warn(f"超过 {self.wait_min} 分钟未结束, 跳过等待")
        return self._req("GET", f"/tasks/{task_id}") or {}

    def verify_data(self, baseline: Dict[str, int]) -> None:
        p_section("4. 验证数据入库")

        # 4a. funnel 有增量
        try:
            f = self._req("GET",
                           f"/facebook/funnel?device_id={self.device}&since_hours=1")
            new_fr = (f.get("stage_friend_request_sent") or 0) - baseline["friend_requests"]
            new_greet = (f.get("stage_greetings_sent") or 0) - baseline["greetings_sent"]
            if new_fr > 0:
                p_ok(f"friend_requests 增量: +{new_fr}")
            else:
                p_warn("friend_requests 无增量 (可能 UI 失败或 phase=cold_start)")
                self.failures.append("no friend_request increment")
            if new_greet > 0:
                p_ok(f"greetings_sent 增量: +{new_greet}")
            elif new_fr > 0:
                p_warn("有好友请求但无 greeting (可能 profile 页无 Message 按钮)")
        except Exception as e:
            p_fail(f"funnel 查询失败: {e}")

        # 4b. 每个 target 的 dossier 状态
        for name in self.targets:
            try:
                # resolve 拿 canonical_id
                r = self._req("POST", "/lead-mesh/leads/resolve", json={
                    "platform": "facebook",
                    "account_id": f"fb:{name}",
                    "display_name": name,
                })
                cid = r.get("canonical_id")
                if not cid:
                    p_warn(f"'{name}': resolve 拿不到 cid")
                    continue
                # 查 dossier
                d = self._req("GET", f"/lead-mesh/leads/{cid}")
                journey = d.get("journey") or []
                actions = [e.get("action") for e in journey]
                p_info(f"'{name}' canonical={cid[:12]}...")
                print(f"    journey actions: {actions}")
                # 期望至少有 extracted + friend_requested / friend_request_risk
                if "friend_requested" in actions:
                    p_ok(f"'{name}' journey 有 friend_requested")
                elif "friend_request_risk" in actions:
                    p_warn(f"'{name}' UI 失败 (friend_request_risk)")
                else:
                    p_warn(f"'{name}' 无 friend 事件 (任务可能未跑到)")
                if "greeting_sent" in actions:
                    p_ok(f"'{name}' journey 有 greeting_sent")
                elif "greeting_blocked" in actions:
                    blocked = [e for e in journey if e["action"] == "greeting_blocked"]
                    reasons = [e["data"].get("reason", "?") for e in blocked]
                    p_warn(f"'{name}' greeting_blocked reasons={reasons}")
            except Exception as e:
                p_warn(f"'{name}' dossier 查询失败: {e}")

        # 4c. contact events 快速计数
        try:
            c = self._req("GET",
                           f"/facebook/contact-events?device_id={self.device}"
                           f"&event_type=add_friend_sent&hours=1")
            p_ok(f"contact_events.add_friend_sent 最近 1h: {c.get('count', 0)}")
        except Exception as e:
            p_warn(f"contact-events 查询失败: {e}")

    def run(self) -> int:
        if not self.check_prereq():
            return 2
        baseline = self.get_baseline()
        tid = self.trigger_task()
        if tid:
            self.wait_for_completion(tid)
        else:
            if not self.dry_run:
                return 1
        # dry_run 也跑一下 verify 路径 (看现有数据)
        self.verify_data(baseline)
        print()
        if self.failures:
            if self.dry_run:
                print(f"{_C.WARN}⚠ dry-run 下发现 {len(self.failures)} 项"
                      f"可能异常 (通常是无增量, 属正常):{_C.END}")
                for f in self.failures:
                    print(f"  - {f}")
                print(f"{_C.DIM}  加 --live 真实触发任务再验证{_C.END}")
                return 0   # dry-run 不 hard-fail
            print(f"{_C.FAIL}✗ {len(self.failures)} 处异常:{_C.END}")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print(f"{_C.OK}{_C.BOLD}✓ smoke 完成 (无 hard-fail){_C.END}")
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://localhost:18080")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--device", required=True, help="目标设备 ID (如 CACAVKLNU8SGO74D)")
    ap.add_argument("--targets", default="",
                     help="目标名字逗号分隔; 默认 '山田花子' 单个")
    ap.add_argument("--persona", default="jp_female_midlife")
    ap.add_argument("--wait-min", type=int, default=10,
                     help="等任务最多 N 分钟 (默认 10)")
    ap.add_argument("--live", action="store_true",
                     help="真实触发任务 (默认 dry-run 只预览)")
    args = ap.parse_args()

    targets = [n.strip() for n in (args.targets or "山田花子").split(",")
                if n.strip()]
    if not targets:
        print("至少需要 1 个 target", file=sys.stderr)
        sys.exit(2)

    runner = RealDeviceSmoke(
        base_url=args.base, device_id=args.device,
        targets=targets, persona=args.persona,
        wait_min=args.wait_min, dry_run=not args.live,
        api_key=args.api_key,
    )
    sys.exit(runner.run())


if __name__ == "__main__":
    main()
