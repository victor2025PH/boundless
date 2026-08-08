#!/usr/bin/env python3
"""tenant_fulfill_watch.py — 托管 SaaS 订单开通守护（订单→provision→expose→回填公网地址）。

与 fulfill_chatx_watch.py（装机版 license 签发）**并行的另一条履约路径**：
  paid 托管订单 → tenant_ops provision（开通实例）→ tenant_ops expose（公网暴露）
  → **公网可达性闸门**（从本机真探 public_url/login，200 才算就绪）
  → 回填订单 code=「网址+登录令牌」（website 私信客户）→ state 标记 done（幂等）
  → **叠期写卡 + 按套餐 gw-budget 提额**（续费识别存量卡，不重发密码）。

可达性闸门（2026-08-07 真单演练实锤后加）：expose 技术上成功（隧道+nginx 到 VPS 边缘）
但云安全组/DNS 未就绪时公网仍打不开——旧行为照样回填交付 = **给客户发打不开的网址**。
现改为「持单」：不回填、不标 done，发 ops 告警（6h 去抖），下轮自动重试（provision/
expose 均幂等）；运维放行安全组或加泛解析后，下一轮自动送达，零人工补单。

纯决策逻辑在 src/ops/tenant_fulfillment.py（is_hosted_order/select_hostable/…）；本文件
只做 HTTP + 编排 + state，且**全部外部作用可注入**（order 源 / provision / expose / 回填），
便于对假订单源端到端自测（零线上副作用）。

安全：托管识别保守（只认 delivery==hosted 或 sku 含 hosted），今日线上 SKU 一个都不匹配
→ 常驻/误跑对现网零副作用。website 上线托管 SKU 时按 tenant_fulfillment 模块 docstring 的
co-change 备忘，让 license 侧排除 delivery==hosted 单（防两条守护同抢一单）。

用法：
  python scripts/tenant_fulfill_watch.py --dry-run          # 拉单只看不开通（首验）
  python scripts/tenant_fulfill_watch.py                    # 单次开通轮（计划任务入口）
  python scripts/tenant_fulfill_watch.py --self-test        # 假订单源端到端自测（零网络/零副作用）

配置（同 fulfill_chatx_watch）：站点 CHATX_SITE/BD_SITE，鉴权 env ADMIN_KEY。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

BASE = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE.parent.parent
sys.path.insert(0, str(BASE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from src.ops import tenant_fulfillment as tf  # noqa: E402
from src.ops import tenant_lifecycle as tl  # noqa: E402

STATE_FILE = Path(tl.DEFAULT_OPS_BASE) / "fulfilled_hosted.json"
TENANT_OPS = BASE / "scripts" / "tenant_ops.py"


# ────────────────────────── 可注入的外部作用 ──────────────────────────

def _http_orders(site: str, key: str) -> List[Dict[str, Any]]:
    req = urllib.request.Request(
        f"{site}/api/admin/orders?status=paid",
        headers={"x-setup-key": key}, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return resp.get("orders", []) if resp.get("ok") else []


def _http_writeback(site: str, key: str, order_id: str, code: str) -> bool:
    body = json.dumps({"id": order_id, "status": "activated", "code": code}).encode("utf-8")
    req = urllib.request.Request(
        f"{site}/api/admin/order-status", data=body, method="POST",
        headers={"Content-Type": "application/json", "x-setup-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return bool(json.loads(r.read().decode("utf-8")).get("ok"))


def _http_set_gw_budget(
    site: str, key: str, subject: str, budget: int, note: str = "",
) -> bool:
    """按主体覆写 AI 网关日额度（``POST /api/admin/gw-budget``）。软失败不阻断交付。"""
    body = json.dumps({
        "subject": subject, "budget": int(budget), "note": note or "",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{site.rstrip('/')}/api/admin/gw-budget", data=body, method="POST",
        headers={"Content-Type": "application/json", "x-setup-key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception as e:  # noqa: BLE001
        print(f"[警告] gw-budget 提额失败（不阻断交付）subject={subject}: {e}",
              file=sys.stderr)
        return False


def _card_write_expiry(instance_id: str, fields: Dict[str, Any]) -> None:
    """交付成功后把到期账本写进租户卡（到期治理的数据地基；软失败不阻断交付）。"""
    try:
        path = Path(r"D:\chengjie-instances") / instance_id / "tenant_card.json"
        card = json.loads(path.read_text(encoding="utf-8-sig"))
        card.update(fields)
        path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[警告] {instance_id} 到期账本写卡失败（不阻断交付）: {e}", file=sys.stderr)
    # 到期账本刚更新（首开/续费叠期）→ 旧的到期提醒横幅数据立刻过时，直接删
    # （watch 下轮按新账本重建；不删的话客户续完费还会看到「已到期」最多 10 分钟）
    try:
        (Path(r"D:\chengjie-instances") / instance_id / "data" / "config"
         / "tenant_notice.json").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _dns_sentinel(st: dict) -> None:
    """泛解析就绪哨兵：每小时经 VPS（域外视角）探一次 ``*.bd2026.cc``。

    本机 DNS 被路由器劫持不可信（2026-08-07 实锤），必须从 VPS 判。就绪即发一次性
    TG 告警（持单订单本就会在 5min 内自动送达；这条告警管「零持单时没人知道 DNS
    已生效」的盲区）。已就绪后不再探（一次性事件）。软失败静默——哨兵挂了不影响履约。
    """
    try:
        if st.get("dns_ready"):
            return
        hour = int(time.time() // 3600)
        if st.get("dns_check_hour") == hour:
            return
        st["dns_check_hour"] = hour
        ssh_cfg = REPO_ROOT / "deploy" / "ssh_config.boundless"
        r = subprocess.run(
            ["ssh", "-F", str(ssh_cfg), "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10", "bd2026",
             f"getent ahostsv4 dnsprobe-{hour}.bd2026.cc | head -1"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
        if r.returncode == 0 and "165.154.233.121" in (r.stdout or ""):
            st["dns_ready"] = int(time.time())
            tl.emit_tenant_alert(
                "wildcard_dns_ready",
                "🌐 *.bd2026.cc 泛解析已生效——托管子域通道解锁：持单订单将自动送达；"
                "存量端口形态租户可重跑 expose 升级子域（443，免安全组）",
                debounce_sec=0)
            print("[dns-sentinel] 泛解析已生效 ✓（已告警；持单将自动送达）")
    except Exception:  # noqa: BLE001 - 哨兵绝不拖垮履约轮
        pass


def _http_probe_public(public_url: str) -> bool:
    """公网可达性真探：本机（VPS 之外）打 {public_url}/login 判 200。

    任何异常（超时/TLS/连接拒绝/非 200）一律 False——方向必须 fail-safe：
    宁可持单等下一轮，绝不把没验证过的网址交付给客户。
    """
    try:
        req = urllib.request.Request(
            f"{public_url.rstrip('/')}/login", method="GET",
            headers={"User-Agent": "tenant-fulfill-probe"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return int(getattr(r, "status", 0) or 0) == 200
    except Exception:  # noqa: BLE001 - 不可达的具体原因不影响持单决策
        return False


def _run_tenant_ops(sub_args: List[str], timeout: int = 300) -> tuple[int, str]:
    cmd = [sys.executable, str(TENANT_OPS)] + sub_args
    r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _provision(args: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    """开通实例：tenant_ops provision --apply。返回 (ok, 交付卡 dict)。"""
    rc, out = _run_tenant_ops([
        "provision", "--customer", args["customer"], "--product", args["product"],
        "--instance-id", args["instance_id"], "--apply"])
    print(out.strip())
    if rc != 0:
        return False, {}
    # 交付卡是开通结果的 SSOT（含 auth_token / 端口）；provision 落在 <data_base>\<iid>\tenant_card.json
    card_path = Path(r"D:\chengjie-instances") / args["instance_id"] / "tenant_card.json"
    try:
        return True, json.loads(card_path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return True, {}


def _expose(args: Dict[str, Any]) -> tuple[bool, str]:
    """公网暴露：tenant_ops expose。返回 (ok, public_url)。"""
    rc, out = _run_tenant_ops(["expose", args["instance_id"], "--slug", args["slug"]])
    print(out.strip())
    # public_url 从交付卡回读（expose 会回写）
    card_path = Path(r"D:\chengjie-instances") / args["instance_id"] / "tenant_card.json"
    try:
        card = json.loads(card_path.read_text(encoding="utf-8-sig"))
        return rc == 0, str(card.get("public_url") or "")
    except Exception:  # noqa: BLE001
        return rc == 0, ""


def _resume(args: Dict[str, Any]) -> bool:
    """续费复机：tenant_ops resume（清暂停旗 + 拉起 + 卡状态 running 的唯一正道）。

    只靠 provision 的顺带拉起**不够**——它不清 `.ops\\suspended\\` 旗，会留下
    「进程在跑、旗还在、看板显示 SUSPENDED、edge 探针跳过它」的分裂状态。
    """
    rc, out = _run_tenant_ops(["resume", args["instance_id"]])
    print(out.strip())
    return rc == 0


def _precard(args: Dict[str, Any]) -> Dict[str, Any]:
    """读**本次履约动作之前**的交付卡（续费判定的唯一正确依据）。

    2026-08-08 P6 真单演练实锤：provision 幂等重入会重写交付卡，靠 post-provision
    卡判续费 → 账本被抹 → 续费单误判首开、初始密码被再次回填。无卡（首开）→ {}。
    """
    path = Path(r"D:\chengjie-instances") / args["instance_id"] / "tenant_card.json"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 无卡=首开
        return {}


# ────────────────────────── state ──────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {"done": {}}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ────────────────────────── 编排（可注入全部外部作用）──────────────────────────

def run_once(
    *,
    dry: bool,
    orders_fn: Callable[[], List[Dict[str, Any]]],
    provision_fn: Callable[[Dict[str, Any]], tuple],
    expose_fn: Callable[[Dict[str, Any]], tuple],
    writeback_fn: Callable[[str, str], bool],
    state: Optional[dict] = None,
    probe_fn: Optional[Callable[[str], bool]] = None,
    card_update_fn: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    alert_fn: Optional[Callable[..., Any]] = None,
    budget_fn: Optional[Callable[[Dict[str, Any]], bool]] = None,
    resume_fn: Optional[Callable[[Dict[str, Any]], bool]] = None,
    precard_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """一轮托管开通。返回 {handled, held, manual, results:[...]}。外部作用全注入 → 可测。

    交付不变量：**只有公网真探 200 的网址才会回填给客户**（probe_fn 可注入；
    生产默认 _http_probe_public）。不可达 = 持单：不回填不标 done，下轮自动重试。

    送达后（同闸门）：叠期写卡 + 按套餐 ``gw-budget`` 提额（budget_fn；软失败）。
    存量卡（已有 public_url+到期账本）走**续费路径**：叠期不浪费剩余天数、
    回填串不重发初始密码；卡状态 suspended（欠费停机/到期自动停）→ 先 resume_fn
    复机（清旗+拉起+卡状态），失败告警并交由可达性闸门持单重试——「客户付钱 →
    服务回来」全自动闭环。

    持久化不变量（2026-08-07 实锤修复）：**只有 state=None（自管理）才写盘**。
    旧行为对注入 state 也 save_state → self-test 的假订单 A2 覆盖了生产台账、
    抹掉真单 done 标记（若订单仍 paid，5min 任务会循环重开通）。注入 state＝
    调用方管生命周期（测试/编排器），本函数只改内存。
    """
    persist = state is None
    st = state if state is not None else load_state()
    done = set(st.get("done", {}).keys())
    probe = probe_fn or _http_probe_public
    # 告警出口注入（2026-08-07 实锤修复）：self-test 曾直连真 emit_tenant_alert →
    # 每跑一次测试就给老板 TG 发假单告警 + 污染 ops_events 审计台账（掺假比没有更糟）。
    alert = alert_fn if alert_fn is not None else tl.emit_tenant_alert
    try:
        orders = orders_fn()
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 拉订单失败：{e}", file=sys.stderr)
        return {"handled": 0, "held": 0, "manual": 0, "results": []}

    for o in tf.manual_hosted_followup(orders, done):
        print(f"[人工] 托管单 {o.get('id')} · {o.get('sku_id') or '(缺 sku)'} · "
              f"{o.get('contact', '')} —— 缺 contact 等，不可自动开通，请人工跟进")

    todo = tf.select_hostable(orders, done)
    results: List[Dict[str, Any]] = []
    handled = 0
    held = 0
    for order, args in todo:
        oid = args["order_id"]
        print(f"[托管] {oid} · {order.get('sku_id')} · {args['product']} · "
              f"{args['customer']} → slug={args['slug']}")
        if dry:
            results.append({"order_id": oid, "action": "dry-run", "args": args})
            continue
        # ── 续费判定必须看 provision **之前**的卡（P6 实锤：provision 幂等重入会
        #    重写卡，post-provision 卡上账本已被抹 → 续费误判首开）。precard_fn
        #    缺省（老测试路径）回落 post-provision 卡判定。
        pre_card: Dict[str, Any] = {}
        if precard_fn is not None:
            try:
                pre_card = dict(precard_fn(args) or {})
            except Exception:  # noqa: BLE001 - 读卡失败按首开保守处理
                pre_card = {}
        ok, card = provision_fn(args)
        if not ok:
            print(f"[错误] {oid} 开通实例失败（见上）", file=sys.stderr)
            alert("tenant_provision_fail",
                  f"🏠 托管订单 {oid} 开通失败（{args['customer']}）",
                  account_id=args["instance_id"], debounce_sec=3600)
            results.append({"order_id": oid, "action": "provision_failed"})
            continue
        card = dict(card or {})
        if precard_fn is None:
            pre_card = card
        renewal = tf.is_existing_hosted_tenant(pre_card)
        # ── 续费复机：停机中的存量租户收到续费单 → 先 resume（清旗+拉起+卡状态）。
        #    provision 顺带拉起不清旗，会留分裂状态；resume 失败不中断——实例可能
        #    已被 provision 带起，交给下方可达性闸门定夺（不可达自然持单重试）。
        if renewal and str(pre_card.get("status") or "") == "suspended":
            resumed = False
            if resume_fn is not None:
                try:
                    resumed = bool(resume_fn(args))
                except Exception as e:  # noqa: BLE001
                    print(f"[警告] {oid} 复机回调异常: {e}", file=sys.stderr)
            if resumed:
                card["status"] = "running"
                print(f"[复机] {oid} 续费到账，暂停实例已复机（{args['instance_id']}）")
            else:
                alert("tenant_resume_fail",
                      f"🔁 续费单 {oid} 的暂停租户 {args['instance_id']} 自动复机失败"
                      f"——若下方探测可达仍会交付，请人工核查暂停旗",
                      account_id=args["instance_id"], debounce_sec=3600)
        ok2, public_url = expose_fn(args)
        # 续费/持单重试：expose 回写优先；卡上已有公网也认（pre 卡先于 post 卡——
        # post 卡可能被 provision 重写过）
        if not public_url:
            public_url = str(pre_card.get("public_url")
                             or card.get("public_url") or "")
        token = str(card.get("initial_password") or card.get("auth_token") or "")
        user = str(card.get("username") or "owner")
        # ── 可达性闸门：无 URL / 公网探测不过 → 持单（绝不交付死链）──
        # 首开还要求本轮 expose 成功（防卡上残留脏 URL）；续费认存量公网，
        # expose 抖动不挡已上线租户叠期。
        reachable = bool(
            public_url and public_url.startswith("http") and probe(public_url)
            and (ok2 or renewal))
        if not reachable:
            held += 1
            print(f"[持单] {oid} 实例已开通（{args['instance_id']}），公网未就绪"
                  f"（安全组/DNS）——不回填交付，下轮自动重试；修通即自动送达",
                  file=sys.stderr)
            alert(
                "tenant_delivery_held",
                f"⏸️ 托管订单 {oid} 已开通实例但公网不可达（{public_url or 'expose 未完成'}）"
                f"——交付已持单。放行安全组或加 *.bd2026.cc 泛解析后自动送达",
                account_id=args["instance_id"], debounce_sec=6 * 3600)
            # 持单台账（观测面消费；送达即摘）：首见记 since，重试只推 last_attempt
            rec = st.setdefault("held", {}).setdefault(oid, {
                "since": int(time.time()), "instance_id": args["instance_id"]})
            rec.update({"last_attempt": int(time.time()),
                        "public_url": public_url or ""})
            results.append({"order_id": oid, "action": "delivery_held",
                            "public_url": public_url,
                            "instance_id": args["instance_id"]})
            continue
        days = tf.period_days(order)
        # 首开：旧账本不参与（持单卡可能有 URL 但无到期）；续费：从 max(now,旧到期) 叠
        # ——旧到期必须取 pre 卡（post 卡被 provision 重写时会丢账本 → 未过期续费
        # 从 now 起算＝白吞客户剩余天数）
        new_exp = tf.stack_expires_at(
            pre_card.get("expires_at") if renewal else None, days)
        if renewal:
            code = tf.build_renewal_code(
                public_url, args["instance_id"], new_exp, username=user)
        else:
            code = tf.build_delivery_code(
                public_url, token, args["instance_id"], username=user)
        if writeback_fn(oid, code):
            st.setdefault("done", {})[oid] = int(time.time())
            st.get("held", {}).pop(oid, None)  # 修通送达 → 摘持单
            if persist:
                save_state(st)
            handled += 1
            budget_spec = tf.gw_budget_for_order(order, args["instance_id"])
            # 到期账本 + 额度镜像：从**交付成功**起算（持单期不吃客户时长）
            if card_update_fn is not None:
                patch = {
                    "expires_at": new_exp,
                    "expires_days": days,
                    "expiry_order": oid,
                    "last_fulfill_kind": "renewal" if renewal else "activate",
                }
                sku = str((order or {}).get("sku_id") or "").strip()
                if sku:  # 到期提醒的续费深链按最近一单的档位生成
                    patch["last_order_sku"] = sku
                if budget_spec:
                    patch["ai_daily_chars"] = budget_spec["budget"]
                    patch["ai_budget_subject"] = budget_spec["subject"]
                card_update_fn(args["instance_id"], patch)
            if budget_fn is not None and budget_spec:
                try:
                    ok_b = bool(budget_fn(budget_spec))
                except Exception as e:  # noqa: BLE001 - 提额绝不能拖垮交付
                    print(f"[警告] {oid} gw-budget 回调异常（不阻断）: {e}",
                          file=sys.stderr)
                    ok_b = False
                if ok_b:
                    print(f"[额度] {oid} ✓ {budget_spec['subject']} "
                          f"= {budget_spec['budget']}/日")
                else:
                    print(f"[警告] {oid} gw-budget 未成功（交付已完成，可手工 "
                          f"POST /api/admin/gw-budget）", file=sys.stderr)
            verb = "续费" if renewal else "开通"
            print(f"[{verb}] {oid} ✓ 已回填交付信息，website 私信客户\n        {code}")
            results.append({
                "order_id": oid,
                "action": "renewed" if renewal else "activated",
                "public_url": public_url,
                "instance_id": args["instance_id"],
                "expires_at": new_exp,
                "ai_daily_chars": (budget_spec or {}).get("budget"),
            })
        else:
            print(f"[错误] {oid} 回填订单失败（实例已开通，state 未标记，下轮重试回填）",
                  file=sys.stderr)
            results.append({"order_id": oid, "action": "writeback_failed"})
    manual_n = len(tf.manual_hosted_followup(orders, done))
    # 心跳：每轮（含 0 单轮）记录节拍与结果——观测面据此判「守护活着没」；
    # dry-run 不落盘（干跑零副作用语义）
    st["last_tick"] = {"ts": int(time.time()), "handled": handled,
                       "held": held, "manual": manual_n, "dry": bool(dry)}
    if persist and not dry:
        save_state(st)
    return {"handled": handled, "held": held, "manual": manual_n, "results": results}


# ────────────────────────── self-test（假订单源，零网络零副作用）──────────────────────────

def _self_test() -> int:
    """对内存假订单源跑完整编排：装机单忽略/托管全链/缺 contact 转人工/可达性持单。

    自带「生产 state 零写入」断言（2026-08-07 实锤：旧版对注入 state 也落盘，
    假订单 A2 覆盖了生产台账）。
    """
    state_before = STATE_FILE.read_bytes() if STATE_FILE.is_file() else None
    orders = [
        {"id": "A1", "sku_id": "chatx-team", "contact": "buyer@x.com", "status": "paid"},   # 装机，忽略
        {"id": "A2", "sku_id": "chatx-flagship", "delivery": "hosted", "contact": "acme@x.com"},  # 托管
        {"id": "A3", "sku_id": "chatx-hosted-entry", "contact": ""},                          # 托管但缺 contact
    ]
    provisioned: List[str] = []
    exposed: List[str] = []
    wrote: Dict[str, str] = {}
    card_updates: Dict[str, Dict[str, Any]] = {}
    budgets: List[Dict[str, Any]] = []
    # live_cards＝「履约动作之前」的卡（precard_fn 数据源；续费判定唯一依据）
    live_cards: Dict[str, Dict[str, Any]] = {}

    def fake_precard(a):
        return dict(live_cards.get(a["instance_id"]) or {})

    def fake_provision(a):
        # 刻意模拟生产真行为（P6 实锤）：provision 幂等重入会**重写交付卡**，
        # 返回的卡不含到期账本/公网字段——续费判定若读它必翻车（回归钉）
        provisioned.append(a["instance_id"])
        return True, {"username": "owner", "initial_password": "pw-" + a["instance_id"],
                      "auth_token": "OPS-TOKEN-DO-NOT-SHIP", "web_port": 18999,
                      "status": "running"}

    def fake_expose(a):
        exposed.append(a["instance_id"])
        return True, f"https://{a['slug']}.bd2026.cc"

    def fake_writeback(oid, code):
        wrote[oid] = code
        return True

    def fake_card_update(iid, fields):
        card_updates[iid] = fields
        # 镜像进 live_cards，供续费段读「已交付」状态
        base = dict(live_cards.get(iid) or {
            "username": "owner", "initial_password": "pw-" + iid,
            "auth_token": "OPS-TOKEN-DO-NOT-SHIP",
            "public_url": f"https://{iid.replace('zhiliao_', '').replace('_', '-')}.bd2026.cc",
        })
        base.update(fields)
        live_cards[iid] = base

    def fake_budget(spec):
        budgets.append(dict(spec))
        return True

    resumes: List[str] = []

    def fake_resume(a):
        resumes.append(a["instance_id"])
        live_cards[a["instance_id"]]["status"] = "running"
        return True

    alerts: List[tuple] = []

    def fake_alert(kind, text, **kw):
        # 绝不连真告警出口：曾实锤 self-test 每跑一次就给老板 TG 发假单告警
        # + 污染 ops_events 审计台账；注入 recorder 后还能正向断言告警语义
        alerts.append((kind, kw.get("account_id", "")))
        return True

    ok = True
    # ── 第 0 段：公网不可达 → 持单（不回填、不标 done、台账记录、下轮重试）──
    st = {"done": {}}
    res0 = run_once(dry=False, orders_fn=lambda: orders, provision_fn=fake_provision,
                    expose_fn=fake_expose, writeback_fn=fake_writeback, state=st,
                    probe_fn=lambda url: False, card_update_fn=fake_card_update,
                    alert_fn=fake_alert, budget_fn=fake_budget,
                    precard_fn=fake_precard)
    if card_updates:
        print("[self-test] FAIL 持单不得写到期账本（未交付不起算）"); ok = False
    if budgets:
        print("[self-test] FAIL 持单不得提额"); ok = False
    if ("tenant_delivery_held", "zhiliao_acme_x_com") not in alerts:
        print(f"[self-test] FAIL 持单必须告警 alerts={alerts}"); ok = False
    if res0["handled"] != 0 or res0["held"] != 1 or wrote:
        print(f"[self-test] FAIL 持单段 handled={res0['handled']} held={res0['held']} "
              f"wrote={list(wrote)}（公网不可达时绝不能交付）"); ok = False
    if st["done"]:
        print("[self-test] FAIL 持单不得标 done（否则修通后永不送达）"); ok = False
    if "A2" not in st.get("held", {}) or not st["held"]["A2"].get("since"):
        print(f"[self-test] FAIL 持单台账缺失 held={st.get('held')}"); ok = False

    # ── 第 1 段：公网可达 → 正常交付全链（含到期账本 + gw-budget）──
    provisioned.clear()
    res = run_once(dry=False, orders_fn=lambda: orders, provision_fn=fake_provision,
                   expose_fn=fake_expose, writeback_fn=fake_writeback, state=st,
                   probe_fn=lambda url: True, card_update_fn=fake_card_update,
                   alert_fn=fake_alert, budget_fn=fake_budget,
                   precard_fn=fake_precard)
    if res["handled"] != 1:
        print(f"[self-test] FAIL handled={res['handled']} 期望 1"); ok = False
    exp = card_updates.get("zhiliao_acme_x_com") or {}
    if exp.get("expires_days") != 32 or not exp.get("expires_at"):
        print(f"[self-test] FAIL 到期账本缺失/天数错 {exp}（缺 period 默认 monthly=32）"); ok = False
    if exp.get("last_fulfill_kind") != "activate":
        print(f"[self-test] FAIL 首开 kind 错 {exp.get('last_fulfill_kind')}"); ok = False
    # flagship = 50 席 × 25k = 1.25M（夹在 800k..1.5M）
    if not budgets or budgets[-1].get("subject") != "IID:zhiliao_acme_x_com":
        print(f"[self-test] FAIL 未按 IID 提额 budgets={budgets}"); ok = False
    elif int(budgets[-1].get("budget") or 0) != 1_250_000:
        print(f"[self-test] FAIL flagship 日额度期望 1250000 得 {budgets[-1]}"); ok = False
    if exp.get("ai_daily_chars") != 1_250_000:
        print(f"[self-test] FAIL 卡上额度镜像缺失 {exp}"); ok = False
    if "A1" in wrote:
        print("[self-test] FAIL 装机单 A1 被误开通！"); ok = False
    if "A2" not in wrote:
        print("[self-test] FAIL 托管单 A2 未开通"); ok = False
    if "初始密码" not in wrote.get("A2", ""):
        print("[self-test] FAIL 首开交付串应含初始密码"); ok = False
    if res["manual"] != 1:
        print(f"[self-test] FAIL manual={res['manual']} 期望 1（A3 缺 contact）"); ok = False
    if provisioned != ["zhiliao_acme_x_com"]:
        print(f"[self-test] FAIL provisioned={provisioned}"); ok = False
    # 安全不变量：运维令牌绝不能进交付串
    if any("OPS-TOKEN" in c for c in wrote.values()):
        print("[self-test] FAIL 运维 auth_token 泄进了交付串！"); ok = False
    # 幂等：再跑一轮，A2 已 done，不重复开通
    provisioned.clear()
    n_budgets = len(budgets)
    res2 = run_once(dry=False, orders_fn=lambda: orders, provision_fn=fake_provision,
                    expose_fn=fake_expose, writeback_fn=fake_writeback, state=st,
                    probe_fn=lambda url: True, alert_fn=fake_alert,
                    budget_fn=fake_budget, precard_fn=fake_precard)
    if res2["handled"] != 0 or provisioned:
        print(f"[self-test] FAIL 幂等破坏 handled={res2['handled']} prov={provisioned}"); ok = False
    if len(budgets) != n_budgets:
        print("[self-test] FAIL 幂等轮不应再提额"); ok = False
    if st.get("held"):
        print(f"[self-test] FAIL 送达后持单台账未摘除 held={st['held']}"); ok = False

    # ── 第 2 段：同客户续费单 → 叠期 + 不重发密码 + 再提额 ──
    first_exp = str(exp.get("expires_at") or "")
    renew_orders = orders + [{
        "id": "A4", "sku_id": "chatx-flagship", "delivery": "hosted",
        "contact": "acme@x.com", "period": "monthly",
    }]
    provisioned.clear()
    wrote.pop("A4", None)
    res3 = run_once(dry=False, orders_fn=lambda: renew_orders, provision_fn=fake_provision,
                    expose_fn=fake_expose, writeback_fn=fake_writeback, state=st,
                    probe_fn=lambda url: True, card_update_fn=fake_card_update,
                    alert_fn=fake_alert, budget_fn=fake_budget, resume_fn=fake_resume,
                    precard_fn=fake_precard)
    if res3["handled"] != 1:
        print(f"[self-test] FAIL 续费 handled={res3['handled']} 期望 1"); ok = False
    r3 = next((x for x in res3["results"] if x.get("order_id") == "A4"), {})
    if r3.get("action") != "renewed":
        print(f"[self-test] FAIL 续费 action={r3.get('action')} 期望 renewed"); ok = False
    code4 = wrote.get("A4", "")
    if "续费已到账" not in code4 or "初始密码" in code4 or "pw-" in code4:
        print(f"[self-test] FAIL 续费串应告延期且不含密码: {code4!r}"); ok = False
    exp2 = card_updates.get("zhiliao_acme_x_com") or {}
    if exp2.get("last_fulfill_kind") != "renewal":
        print(f"[self-test] FAIL 续费 kind 错 {exp2.get('last_fulfill_kind')}"); ok = False
    # 叠期：新到期应严格晚于首开到期（同日 monthly 再叠 32 天）
    if not (exp2.get("expires_at") and first_exp and exp2["expires_at"] > first_exp):
        print(f"[self-test] FAIL 续费未叠期 first={first_exp} now={exp2.get('expires_at')}"); ok = False
    if not any(b.get("note", "").startswith("order=A4") for b in budgets):
        print(f"[self-test] FAIL 续费未再提额 budgets={budgets}"); ok = False
    if resumes:
        print(f"[self-test] FAIL 运行中租户续费不应触发复机 resumes={resumes}"); ok = False

    # ── 第 3 段：停机租户（欠费/到期自动停）收到续费单 → 自动复机 + 交付 ──
    live_cards["zhiliao_acme_x_com"]["status"] = "suspended"
    renew2 = orders + [{
        "id": "A5", "sku_id": "chatx-flagship", "delivery": "hosted",
        "contact": "acme@x.com", "period": "monthly",
    }]
    res4 = run_once(dry=False, orders_fn=lambda: renew2, provision_fn=fake_provision,
                    expose_fn=fake_expose, writeback_fn=fake_writeback, state=st,
                    probe_fn=lambda url: True, card_update_fn=fake_card_update,
                    alert_fn=fake_alert, budget_fn=fake_budget, resume_fn=fake_resume,
                    precard_fn=fake_precard)
    if resumes != ["zhiliao_acme_x_com"]:
        print(f"[self-test] FAIL 停机续费须复机 resumes={resumes}"); ok = False
    r4 = next((x for x in res4["results"] if x.get("order_id") == "A5"), {})
    if r4.get("action") != "renewed":
        print(f"[self-test] FAIL 停机续费 action={r4.get('action')} 期望 renewed"); ok = False
    if live_cards["zhiliao_acme_x_com"].get("status") != "running":
        print(f"[self-test] FAIL 复机后卡状态应 running"); ok = False
    if any(k == "tenant_resume_fail" for k, _ in alerts):
        print(f"[self-test] FAIL 复机成功不应发失败告警 alerts={alerts}"); ok = False

    # ── 收尾不变量：self-test 全程绝不触碰生产 state 文件 ──
    state_after = STATE_FILE.read_bytes() if STATE_FILE.is_file() else None
    if state_before != state_after:
        print("[self-test] FAIL 生产 state 被写入！（注入 state 必须纯内存）"); ok = False

    print("[self-test] PASS ✓ 装机忽略/托管全链/转人工/幂等/令牌不外泄/持单台账↔送达摘除/"
          "到期账本随交付起算/gw-budget提额/续费叠期不泄密/停机续费自动复机/生产state零写入"
          if ok else "[self-test] 有 FAIL")
    return 0 if ok else 1


# ────────────────────────── main ──────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="托管 SaaS 订单开通守护")
    ap.add_argument("--site", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="假订单源端到端自测（零网络）")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    site = (args.site or os.environ.get("CHATX_SITE") or os.environ.get("BD_SITE") or "").rstrip("/")
    key = os.environ.get("ADMIN_KEY", "")
    if not site or not key:
        print("[错误] 缺 --site/CHATX_SITE 或 ADMIN_KEY", file=sys.stderr)
        return 2

    res = run_once(
        dry=args.dry_run,
        orders_fn=lambda: _http_orders(site, key),
        provision_fn=_provision,
        expose_fn=_expose,
        writeback_fn=lambda oid, code: _http_writeback(site, key, oid, code),
        card_update_fn=_card_write_expiry,
        budget_fn=(None if args.dry_run else (
            lambda spec: _http_set_gw_budget(
                site, key, spec["subject"], int(spec["budget"]),
                str(spec.get("note") or "")))),
        resume_fn=(None if args.dry_run else _resume),
        precard_fn=_precard)
    if not args.dry_run:
        # 泛解析就绪哨兵（每小时一次；就绪即一次性告警）——独立读写 state，
        # 与 run_once 的自管理持久化互不纠缠
        st = load_state()
        before = dict(st)
        _dns_sentinel(st)
        if st != before:
            save_state(st)
    print(f"[托管履约] 完成：开通 {res['handled']} 单，持单 {res.get('held', 0)} 单，"
          f"转人工 {res['manual']} 单{'（dry-run）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
