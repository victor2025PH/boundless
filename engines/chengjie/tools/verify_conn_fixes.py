# -*- coding: utf-8 -*-
"""连接池饿死修复的重启后验证（P1-0 头像熔断 / P1-1 SSE 静默 prime；2026-08-05 沉淀）。

**为什么存在**：两项修复都是后端 .py，落盘后要等下一次实例重启才生效（共享树多线
并发下由谁在何时重启不确定，靠人记得回来验证不可靠）。本工具把「重启后真机验证」
固化成一条命令：修复尚未装载 → SKIP exit 0（不污染回归信号）；装载后自动断言。

验证项（纯 API 只读，零 playwright 依赖）：
  1. 装载探测：先触发一次头像请求制造统计 slot，再查 /api/workspace/metrics 的
     peer_identity.avatar 段是否含 ``cooldown`` 键（该键随 P1-0 新代码才会出现）。
  2. SSE 静默连接（P1-1）：连上 /api/workspace/stream 读 2.5s，连接期 sla_alert/
     escalation 帧数应 ≤3（旧代码会把全部在途项逐条重放——生产积压时 ≥45 帧；
     阈值留 3 容纳窗口内真边沿，复跑即可分辨）。
  3. WA 头像时延（P1-0）：采样 ≤3 个 WhatsApp 会话打头像端点，每个应 <6s
     （4s 上游超时 + 余量；熔断窗内秒 404 同样达标。修复前 socket 半死时每个挂 20s）。
     无 WhatsApp 会话则该项跳过。

token 从实例数据根读取，绝不打印。实例不可达 → SKIP exit 0（gate_sweep -Full 前提）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_account_rail_ui import (  # noqa: E402
    Checker, DEFAULT_BASE, DEFAULT_DATA_ROOT, read_token,
)


def main() -> int:
    try:
        import httpx
    except Exception:
        print("[SKIP] httpx 不可用")
        return 0
    token = read_token(DEFAULT_DATA_ROOT)
    if not token:
        print("[SKIP] 读不到 auth_token")
        return 0

    ck = Checker()
    with httpx.Client(base_url=DEFAULT_BASE, timeout=10.0) as c:
        try:
            r = c.post("/login", data={"auth_token": token}, follow_redirects=False)
            if r.status_code >= 500:
                print("[SKIP] 实例不可达/登录失败")
                return 0
        except Exception:
            print("[SKIP] 实例不可达")
            return 0

        # ── 1. 装载探测：先触发一次头像统计，再看 cooldown 键是否存在 ──
        wa_rows = []
        try:
            chats = (c.get("/api/unified-inbox/chats?limit=200").json() or {}).get("chats") or []
            wa_rows = [x for x in chats
                       if x.get("platform") == "whatsapp" and x.get("chat_key")][:3]
            probe = next((x for x in chats
                          if x.get("platform") in ("telegram", "whatsapp", "messenger")
                          and x.get("chat_key")), None)
            if probe:
                c.get(f"/api/platforms/{probe['platform']}/{probe.get('account_id') or 'default'}"
                      f"/avatar?chat_key={probe['chat_key']}", timeout=25.0)
        except Exception:
            pass
        av = {}
        try:
            av = ((c.get("/api/workspace/metrics").json() or {})
                  .get("peer_identity") or {}).get("avatar") or {}
        except Exception:
            pass
        # 顶层聚合键随 P1-0 新代码恒在（值可为 0）——dump 侧注释明言以此探装载；
        # 勿改回逐 slot 迭代：avatar 段顶层是「聚合 int + by_platform dict」混合结构，
        # 迭代 values 撞 int 会 TypeError（2026-08-05 首跑实锤）。
        loaded = "cooldown" in av
        if not loaded:
            print("[SKIP] P1-0/P1-1 修复尚未装载（等下一次实例重启窗口；avatar 统计无 cooldown 键）")
            return 0
        ck.check("修复已装载（avatar 统计含 cooldown 键）", True)

        # ── 2. SSE 静默连接：连接期不得整批重放在途 SLA/升级 ──
        frames = 0
        try:
            import json as _json
            with c.stream("GET", "/api/workspace/stream", timeout=8.0) as resp:
                t0 = time.time()
                for line in resp.iter_lines():
                    if time.time() - t0 > 2.5:
                        break
                    if not line.startswith("data: "):
                        continue
                    try:
                        evt = _json.loads(line[6:])
                    except Exception:
                        continue
                    if evt.get("type") in ("sla_alert", "escalation"):
                        frames += 1
        except Exception as e:
            # 读流超时且期间零帧 = 连接期完全静默——这恰是 P1-1 静默 prime 的
            # 期望行为（旧代码连接即推存量，iter_lines 立刻有数据不会超时）。
            # 只有「超时且已收到帧」或非超时异常才不判定。
            if frames == 0 and "timed out" in str(e).lower():
                pass   # 静默连接：按 frames=0 正常判定
            else:
                print(f"  [WARN] SSE 读取异常（不判定）：{str(e)[:80]}")
                frames = -1
        if frames >= 0:
            ck.check("SSE 连接期无存量重放（sla/esc 帧 ≤3）", frames <= 3,
                     f"frames={frames}（旧代码在积压时会重放全量 ≥45）")

        # ── 3. WA 头像时延：熔断/短超时生效后不再有 20s 挂死 ──
        if not wa_rows:
            print("  [SKIP] 无 WhatsApp 会话，头像时延项跳过")
        for x in wa_rows:
            t0 = time.time()
            try:
                rr = c.get(f"/api/platforms/whatsapp/{x.get('account_id') or 'default'}"
                           f"/avatar?chat_key={x['chat_key']}", timeout=25.0)
                dt = time.time() - t0
                ck.check(f"WA 头像 {str(x['chat_key'])[:14]} 响应 <6s",
                         dt < 6.0, f"{dt:.2f}s status={rr.status_code}")
            except Exception as e:
                dt = time.time() - t0
                ck.check(f"WA 头像 {str(x['chat_key'])[:14]} 响应 <6s", False,
                         f"{dt:.2f}s EXC {str(e)[:60]}")

        # ── 4. WA 真回源探测（第 3 段的补强）：真实会话可能命中 7 天磁盘缓存 →
        #    「<6s」恒过、测不到回源路径。这里用随机不存在的 chat_key 强制回源：
        #    上限仍 <6s（4s 上游超时 + 余量）；若首发 >3.5s（疑似上游挂死吃满超时
        #    → 已开账号熔断窗），次发同账号另一随机 key 必须 <1s（熔断秒拒的关联
        #    断言——这才是 P1-0 的核心不变量）。WA 正常时随机 jid 拿空 url 会留一个
        #    孤儿 .none 负缓存文件（随机 key 不会再被查，无害）。 ──
        if wa_rows:
            import random as _rnd
            acct = wa_rows[0].get("account_id") or "default"

            def _force_origin() -> float:
                key = str(_rnd.randint(10**11, 10**12 - 1))
                t0 = time.time()
                try:
                    c.get(f"/api/platforms/whatsapp/{acct}/avatar?chat_key={key}",
                          timeout=25.0)
                except Exception:
                    pass
                return time.time() - t0

            d1 = _force_origin()
            ck.check("WA 头像强制回源 <6s（绕开磁盘缓存）", d1 < 6.0, f"{d1:.2f}s")
            if d1 > 3.5:   # 首发疑似吃满上游超时 → 熔断窗应已打开
                d2 = _force_origin()
                ck.check("熔断窗生效：次发秒拒 <1s（不再逐个吃超时）",
                         d2 < 1.0, f"{d2:.2f}s")

    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
