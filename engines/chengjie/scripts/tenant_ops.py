#!/usr/bin/env python3
"""tenant_ops.py — 托管租户实例全生命周期 CLI（provision/suspend/resume/export/status）。

托管 SaaS 档「一客一实例」的机器侧一条命令入口。规划/写盘复用
`src/ops/instance_provisioner.py`（Sprint5），后半程与生命周期在
`src/ops/tenant_lifecycle.py`。生产双实例（zhiliao/tongyi）被硬拒在管辖之外。

用法：
  python scripts/tenant_ops.py provision --customer "Acme Ltd"            # 干跑（只打印计划）
  python scripts/tenant_ops.py provision --customer "Acme Ltd" --apply    # 真开通：写盘+junction+拉起+等就绪+交付卡
  python scripts/tenant_ops.py provision --customer "Acme Ltd" --apply --no-start   # 只写盘不拉起
  python scripts/tenant_ops.py list                                       # 已登记租户
  python scripts/tenant_ops.py status                                     # 各租户运行态（只读）
  python scripts/tenant_ops.py suspend zhiliao_acme_ltd --reason overdue  # 欠费暂停（停进程+落 flag）
  python scripts/tenant_ops.py protect zhiliao_acme_ltd --reason "坐席生产入口"  # 标记受保护（危险命令须 --force-protected）
  python scripts/tenant_ops.py resume  zhiliao_acme_ltd                   # 恢复（删 flag+拉起+等就绪）
  python scripts/tenant_ops.py export  zhiliao_acme_ltd                   # 退租数据导出（要求已停；--force 越过）
  python scripts/tenant_ops.py deprovision zhiliao_acme_ltd               # 退租=暂停+导出（数据保留原地，人工删）

约定：
- 交付卡落 `D:\\chengjie-instances\\<iid>\\tenant_card.json`（含工作台 URL/登录令牌），
  开通守护（下一阶段对接 bd2026.cc 订单）读卡回填订单；
- 暂停旗 `D:\\chengjie-instances\\.ops\\suspended\\<iid>.flag`；导出包落 `.ops\\exports\\`；
- stack.json 条目保持 enabled=false（租户拉起走本 CLI，不走 deploy up 编排）。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # engines/chengjie
REPO_ROOT = BASE.parent.parent                            # boundless
sys.path.insert(0, str(BASE))

try:  # Windows GBK 控制台防乱码（不可用时静默）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from src.ops.instance_provisioner import (  # noqa: E402
    build_stack_entry,
    plan_instance,
    render_overlay,
    upsert_stack_entry,
)
from src.ops import tenant_lifecycle as tl  # noqa: E402

STACK_PATH = REPO_ROOT / "deploy" / "stack.json"
EXAMPLE_CFG = BASE / "config" / "config.example.yaml"
START_SCRIPT = REPO_ROOT / "deploy" / "instances" / "start_zhiliao.ps1"
ENGINE_DOMAINS = BASE / "domains"


def _read_card(data_dir: str) -> dict:
    try:
        return json.loads(tl.tenant_card_path(data_dir).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


def _update_card(data_dir: str, **fields) -> None:
    """交付卡＝租户意图记录（desired state）：suspend/resume/watch 都以 status 为准。"""
    path = tl.tenant_card_path(data_dir)
    card = _read_card(data_dir)
    if not card:
        return
    card.update(fields)
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _load_stack() -> dict:
    # utf-8-sig：现网 stack.json 带 BOM（历史上被 PowerShell 写过），普通 utf-8 会拒读
    return json.loads(STACK_PATH.read_text(encoding="utf-8-sig"))


def _save_stack(stack: dict) -> None:
    STACK_PATH.write_text(
        json.dumps(stack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _find_service(stack: dict, instance_id: str) -> dict | None:
    sid = f"chengjie_{instance_id}"
    for svc in stack.get("services", []) or []:
        if svc.get("id") == sid:
            return svc
    return None


def _start_instance(instance_id: str, port: int, product_id: str, data_dir: str) -> int:
    """经生产同款启动器拉起（防呆全带：端口/config/junction 缺失都会拦）。"""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(START_SCRIPT),
           "-InstanceId", instance_id, "-Port", str(port),
           "-ProductId", product_id, "-DataDir", data_dir]
    r = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip())
    return r.returncode


# ────────────────────────── provision ──────────────────────────

def cmd_provision(args) -> int:
    if args.instance_id:
        tl.guard_not_core(args.instance_id)
    stack = _load_stack()
    plan = plan_instance(stack, product=args.product, customer=args.customer,
                         instance_id=(args.instance_id or None))

    # 幂等重入：已登记过的租户沿用登记的端口/数据根（中断后重跑接着走，
    # 防止「重跑换端口」造成 stack 与实际不一致）
    existing = _find_service(stack, plan.instance_id)
    if existing:
        ports = existing.get("ports") or []
        plan = dataclasses.replace(
            plan,
            web_port=int(ports[0]) if ports else plan.web_port,
            alt_port=int(ports[1]) if len(ports) > 1 else plan.alt_port,
            data_dir=tl.data_dir_of_service(existing),
        )
        print(f"[plan] 复用已登记条目 {plan.service_id}（端口/数据根以 stack.json 为准）")

    print(f"[plan] instance_id = {plan.instance_id}")
    print(f"[plan] data_dir    = {plan.data_dir}")
    print(f"[plan] ports       = web {plan.web_port} / alt {plan.alt_port}")

    if not args.apply:
        print("\n[dry-run] 未写盘。加 --apply 执行完整开通（写盘+junction+拉起+等就绪+交付卡）。")
        return 0

    # 1) 写盘（幂等）。auth_token 自己生成并传入，交付卡要用同一个值。
    token = tl.read_overlay_token(plan.data_dir) or None
    if token:
        print("[apply] overlay 已存在，沿用其 auth_token")
    else:
        import secrets
        token = secrets.token_urlsafe(24)
    overlay = render_overlay(plan, host=args.host, auth_token=token)
    for act in tl.materialize(plan.data_dir, overlay, EXAMPLE_CFG):
        print(f"[apply] {act}")

    # 2) domains junction
    print(f"[apply] {tl.ensure_junction(plan.data_dir, str(ENGINE_DOMAINS))}")

    # 2b) 租户预设注入（厂商侧 .ops\tenant_ai_preset.yaml；缺文件即跳过）。
    #     2026-08-08 起支持多段（ai / licensing / platform_login）：托管 AI 网关 +
    #     渠道凭据池随开通自动接线；credpool.license_key=AUTO 自动签发每租户专属卡密。
    if not args.no_ai_preset:
        preset = tl.load_tenant_preset(tl.ai_preset_path())
        if preset:
            lic = tl.resolve_preset_license(preset, plan.instance_id)
            if lic:
                print(f"[apply] credpool 卡密（本租户专属）：{lic}")
            overlay_path = Path(plan.data_dir) / "config" / "config.local.yaml"
            keys = tl.inject_tenant_preset(overlay_path, preset)
            print(f"[apply] 租户预设注入 {len(keys)} 键：{', '.join(keys[:8])}"
                  + ("…" if len(keys) > 8 else ""))
        else:
            print("[apply] 无租户预设（.ops\\tenant_ai_preset.yaml 未配置）——"
                  "正式租户开通前用 set-ai 注入，否则 AI/渠道为占位不可用")

    # 3) stack 登记（幂等；enabled=false——租户拉起走本 CLI）
    stack, action = upsert_stack_entry(stack, build_stack_entry(plan))
    if action == "added":
        _save_stack(stack)
        print(f"[apply] stack.json += {plan.service_id}（enabled=false）")
    else:
        print(f"[apply] stack.json 已有 {plan.service_id}（幂等跳过）")

    # 4) 拉起 + 等就绪
    status = "provisioned"
    if args.no_start:
        print("[apply] --no-start：跳过拉起")
        status = "provisioned_not_started"
    else:
        rc = _start_instance(plan.instance_id, plan.web_port, plan.product_id, plan.data_dir)
        if rc != 0:
            print(f"[错误] 启动器退出码 {rc}", file=sys.stderr)
            return 1
        print(f"[apply] 等待就绪（/login 200，超时 {args.ready_timeout}s）…")
        if tl.wait_ready(plan.web_port, timeout_sec=args.ready_timeout):
            code, title = tl.probe_login(plan.web_port)
            print(f"[apply] 就绪 ✓  /login={code}  title={title!r}")
            status = "running"
        else:
            print("[警告] 就绪等待超时；boot 日志见 <数据根>\\logs\\boot_*.log", file=sys.stderr)
            status = "start_timeout"

    # 5) 客户账号（凭据职责分离：客户拿 owner，auth_token 只留我方运维）
    owner_user, owner_pw = "", ""
    if status in ("running",):
        try:
            created, owner_pw = tl.create_owner_account(plan.data_dir)
            owner_user = tl.OWNER_USERNAME
            if created:
                print(f"[apply] 客户账号已建：{owner_user}（独立随机密码；"
                      "auth_token 保留为我方运维通道，不交付）")
            else:
                # 幂等重入：只有旧卡本身就是 owner 口径时才能沿用其密码；
                # 旧卡是 admin+token 口径（升级前开的实例）时**不能**拿它冒充 owner 密码
                prev = _read_card(plan.data_dir)
                if str(prev.get("username") or "") == owner_user:
                    owner_pw = str(prev.get("initial_password") or "")
                    print(f"[apply] 客户账号 {owner_user} 已存在（幂等，沿用卡上密码）")
                else:
                    owner_pw = ""
                    print(f"[apply] 客户账号 {owner_user} 已存在但卡上无其密码——"
                          "卡将不带初始密码（密码只在创建时可知；需要则删该账号后重跑）",
                          file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - 建号失败不推翻已开通的实例
            print(f"[警告] 客户账号创建失败（回落 admin+令牌口径）：{e}", file=sys.stderr)
    else:
        print("[apply] 实例未就绪，跳过客户账号创建（resume 后重跑 provision 补建）")

    # 6) 交付卡（幂等重入=合并而非整卡重写：到期账本/订单关联/公网暴露状态是
    #    履约守护与 expose 写入的事实，provision 无权抹除——2026-08-08 P6 真单
    #    演练实锤：整卡重写导致续费单被误判首开、初始密码被再次回填给客户）
    card = tl.build_tenant_card(plan.to_dict(), token, status=status,
                                owner_user=owner_user, owner_password=owner_pw)
    card = tl.merge_preserved_card_fields(card, _read_card(plan.data_dir))
    card_path = tl.tenant_card_path(plan.data_dir)
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    print(f"\n--- 交付卡（{card_path}）---")
    for k in ("instance_id", "workspace_url", "login_url", "username",
              "initial_password", "data_dir", "status"):
        print(f"  {k:16} = {card.get(k)}")
    print("  ⚠ auth_token 是我方运维通道，**不要**发给客户（客户改密不会使其失效）。")
    if status not in ("running", "provisioned_not_started"):
        return 1
    return 0


# ────────────────────────── list / status ──────────────────────────

def cmd_list(_args) -> int:
    stack = _load_stack()
    tenants = tl.tenant_services(stack)
    if not tenants:
        print("（无租户实例；生产双实例 zhiliao/tongyi 不在本 CLI 管辖）")
        return 0
    for svc in tenants:
        iid = tl.instance_id_of(svc)
        prot = "🔒受保护  " if tl.is_protected(iid) else ""
        print(f"  {iid:28} web={tl.web_port_of_service(svc)}  "
              f"enabled={svc.get('enabled')}  {prot}{svc.get('title', '')}")
    return 0


def cmd_status(_args) -> int:
    stack = _load_stack()
    tenants = tl.tenant_services(stack)
    if not tenants:
        print("（无租户实例）")
        return 0
    print(f"{'instance_id':28} {'port':>6} {'http':>5} {'state':12} {'密码':6} {'公网':24} title")
    for svc in tenants:
        iid = tl.instance_id_of(svc)
        port = tl.web_port_of_service(svc)
        data_dir = tl.data_dir_of_service(svc)
        code, title = tl.probe_login(port) if port else (None, "")
        suspended = tl.suspended_flag_path(iid).exists()
        if suspended:
            state = "SUSPENDED"
        elif code == 200:
            state = "RUNNING"
        elif code:
            state = f"HTTP {code}"
        else:
            state = "DOWN"
        card = _read_card(data_dir)
        init = tl.initial_password_unchanged(
            data_dir, str(card.get("initial_password") or ""),
            username=str(card.get("username") or "admin"))
        pw = {True: "初始", False: "已改", None: "?"}[init]
        pub = str(card.get("public_url") or "-")
        prot = "🔒 " if tl.is_protected(iid) else ""
        print(f"{iid:28} {port or '-':>6} {code or '-':>5} {state:12} {pw:6} {pub:24} {prot}{title}")
    print("\n注：租户自愈走本 CLI 的 watch 子命令（建议挂计划任务，见 TENANTS.md；"
          "生产 watchdog_instances 只管 zhiliao/tongyi）；SUSPENDED 旗在 .ops\\suspended\\；"
          "🔒=受保护租户（suspend/deprovision/restore/unexpose 须 --force-protected）。")
    print("「密码=初始」表示客户仍在用交付初始密码（未改），可提示其在 设置→修改密码 自改。")
    return 0


# ────────────────────────── suspend / resume ──────────────────────────

def _tenant_ctx(instance_id: str):
    """取租户上下文（service 条目 + 端口 + 数据根）；不存在/生产实例即报错。"""
    try:
        tl.guard_not_core(instance_id)
    except ValueError as e:
        raise SystemExit(f"[拒绝] {e}") from None
    stack = _load_stack()
    svc = _find_service(stack, instance_id)
    if not svc or not tl.is_tenant_service(svc):
        raise SystemExit(f"[错误] 未登记的租户实例: {instance_id}（先 provision）")
    port = tl.web_port_of_service(svc)
    data_dir = tl.data_dir_of_service(svc)
    return svc, port, data_dir


def _guard_protected(instance_id: str, action: str, force: bool) -> None:
    """受保护租户闸（真人在用＝生产资产）：拒绝时推 TG 留痕；--force-protected 破玻璃也留痕。

    2026-08-07 事故沉淀：坐席在 pilot 上工作时它被当演练件 suspend → 全员 502。
    拦截与破玻璃都要**可见**——机制防误操作，告警防「静默地正确执行了错误的事」。
    """
    try:
        tl.guard_not_protected(instance_id, action, override=force)
    except ValueError as e:
        tl.emit_tenant_alert(
            "tenant_protected_blocked",
            f"🛡️ 受保护租户 {instance_id} 的 {action} 已被拦截（未加 --force-protected）",
            account_id=instance_id, debounce_sec=600)
        raise SystemExit(f"[拒绝] {e}") from None
    if force and tl.is_protected(instance_id):
        tl.emit_tenant_alert(
            "tenant_protected_override",
            f"⚠️ 受保护租户 {instance_id} 被 --force-protected 强制执行 {action}"
            f"（保护原因：{tl.protected_reason(instance_id) or '-'}）",
            account_id=instance_id, debounce_sec=60)
        print(f"[警告] {instance_id} 受保护：--force-protected 强制执行 {action}（已推 TG 留痕）")


def _suspend_core(instance_id: str, port: int, data_dir: str,
                  reason: str, grace_sec: int = 8) -> None:
    """停进程 + 落暂停旗 + 卡状态（人工 suspend 与到期自动停共用同一套动作）。"""
    r = tl.stop_tenant(port, data_dir, grace_sec=grace_sec)
    print(f"[suspend] {r['note']}  stopped={r['stopped']}")
    flag = tl.write_suspend_flag(instance_id, reason)
    _update_card(data_dir, status="suspended")
    print(f"[suspend] 旗已落: {flag}")


def cmd_suspend(args) -> int:
    svc, port, data_dir = _tenant_ctx(args.instance_id)
    _guard_protected(args.instance_id, "suspend", args.force_protected)
    _suspend_core(args.instance_id, port, data_dir, args.reason, grace_sec=args.grace)
    # 已公网暴露的实例被停＝入口对外变 502，无论是否受保护都要可见
    # （2026-08-07 事故里 suspend 全程静默，老板比机器先发现）
    card = _read_card(data_dir)
    if card.get("exposed"):
        tl.emit_tenant_alert(
            "tenant_suspended",
            f"⏸️ 已暴露租户 {args.instance_id} 被 suspend（reason={args.reason}）"
            f"——公网入口 {card.get('public_url') or '-'} 将 502，恢复用 "
            f"tenant_ops resume {args.instance_id}",
            account_id=args.instance_id, debounce_sec=60)
    return 0


def cmd_protect(args) -> int:
    _svc, _port, data_dir = _tenant_ctx(args.instance_id)
    if args.off:
        removed = tl.clear_protected_flag(args.instance_id)
        _update_card(data_dir, protected=False)
        print(f"[protect] {args.instance_id} " + ("保护已解除" if removed else "本就未保护（幂等）"))
        return 0
    flag = tl.write_protected_flag(args.instance_id, args.reason)
    _update_card(data_dir, protected=True)
    print(f"[protect] {args.instance_id} 已标记受保护: {flag}")
    print("[protect] 语义：suspend/deprovision/restore/unexpose 须 --force-protected"
          "（拦截与破玻璃均推 TG 留痕）；resume/backup/watch 自愈不受限")
    return 0


def cmd_resume(args) -> int:
    svc, port, data_dir = _tenant_ctx(args.instance_id)
    flag = tl.suspended_flag_path(args.instance_id)
    if flag.exists():
        flag.unlink()
        print(f"[resume] 已删暂停旗: {flag}")
    # 产品线优先读交付卡（provision 落的），卡缺失才按 id 前缀猜
    product_id = ""
    try:
        card = json.loads(tl.tenant_card_path(data_dir).read_text(encoding="utf-8"))
        product_id = str(card.get("product") or "")
    except Exception:  # noqa: BLE001
        pass
    if not product_id:
        product_id = "zhiliao" if args.instance_id.startswith("zhiliao") else "tongyi"
    rc = _start_instance(args.instance_id, port, product_id, data_dir)
    if rc != 0:
        return rc
    if tl.wait_ready(port, timeout_sec=args.ready_timeout):
        code, title = tl.probe_login(port)
        _update_card(data_dir, status="running")
        print(f"[resume] 就绪 ✓  /login={code}  title={title!r}")
        return 0
    _update_card(data_dir, status="start_timeout")
    print("[警告] 就绪等待超时", file=sys.stderr)
    return 1


# ────────────────────────── export / deprovision ──────────────────────────

def _do_export(instance_id: str, port: int, data_dir: str,
               include_sessions: bool, force: bool) -> Path:
    code, _ = tl.probe_login(port) if port else (None, "")
    if code and not force:
        raise SystemExit(
            f"[错误] 实例仍在运行（/login={code}）；在线打包 SQLite 有一致性风险。"
            "先 suspend，或加 --force 强行导出")
    manifest = tl.export_manifest(data_dir, include_sessions=include_sessions)
    if not manifest:
        raise SystemExit(f"[错误] 数据根无可导出文件: {data_dir}")
    import time as _t
    out_zip = tl.exports_dir() / f"{instance_id}_{_t.strftime('%Y%m%d_%H%M%S')}.zip"
    n = tl.write_export_zip(manifest, out_zip)
    print(f"[export] {n} 个文件 → {out_zip}")
    print(f"[export] 口径：config 全套（含 *.db/yaml；排除 {sorted(tl.EXPORT_CONFIG_EXCLUDES)}）"
          f"{'，含 sessions' if include_sessions else '，不含 sessions（登录态默认不外流）'}")
    return out_zip


def cmd_export(args) -> int:
    _svc, port, data_dir = _tenant_ctx(args.instance_id)
    _do_export(args.instance_id, port, data_dir, args.include_sessions, args.force)
    return 0


def cmd_deprovision(args) -> int:
    svc, port, data_dir = _tenant_ctx(args.instance_id)
    _guard_protected(args.instance_id, "deprovision", args.force_protected)
    r = tl.stop_tenant(port, data_dir, grace_sec=args.grace)
    print(f"[deprovision] 停机: {r['note']}")
    tl.write_suspend_flag(args.instance_id, "deprovisioned")
    out_zip = _do_export(args.instance_id, port, data_dir,
                         args.include_sessions, force=False)
    print(f"[deprovision] 完成：实例已停 + 数据已导出 {out_zip}")
    print(f"[deprovision] 数据根保留原地（回滚点，防呆不自动删）: {data_dir}")
    print("[deprovision] 彻底清理（人工确认后）: 删数据根目录 + stack.json 删该条目 + 删暂停旗")
    _update_card(data_dir, status="deprovisioned")
    return 0


# ────────────────────────── watch（租户自愈，一次一轮）──────────────────────────

def _sync_tenant_notice(iid: str, data_dir: str, card: dict, now: float) -> None:
    """把到期提醒写/清进租户数据区（客户侧横幅数据源；软失败不拖巡检）。

    expiring/expired → 写 `<data>/config/tenant_notice.json`（含续费深链，
    实例经 ai-runtime-status 捎带给工作台横幅）；ok/none → 删文件。
    """
    try:
        path = Path(data_dir) / "config" / "tenant_notice.json"
        notice = tl.build_tenant_notice(card, now)
        if notice is None:
            path.unlink(missing_ok=True)
            return
        from src.ops import tenant_fulfillment as tf
        notice.update({
            "instance_id": iid,
            "written_at": int(now),
            "renew_url": tf.renew_order_url(str(card.get("last_order_sku") or "")),
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(notice, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - 提醒文件绝不拖垮巡检本务
        print(f"[watch] {iid:24} 到期提醒文件同步失败（忽略）: {e}", file=sys.stderr)


def cmd_watch(args) -> int:
    """一轮巡检：探活全部租户 → DOWN 且应在跑的经启动器幂等拉起（冷却/连败转人工）。

    设计为计划任务节拍调用（如每 5 分钟），与生产 watchdog_instances.ps1 并行不悖：
    生产双实例归它，租户归本命令，互不越界（tenant_services 过滤 + guard_not_core）。
    """
    state_path = tl.watch_state_path()
    state = tl.load_watch_state(state_path)
    if args.reset:
        if state.pop(args.reset, None) is not None:
            tl.save_watch_state(state_path, state)
            print(f"[watch] 已重置 {args.reset} 的失败计数")
        else:
            print(f"[watch] {args.reset} 无状态可重置")
        return 0

    stack = _load_stack()
    facts, meta = [], {}
    for svc in tl.tenant_services(stack):
        iid = tl.instance_id_of(svc)
        port = tl.web_port_of_service(svc)
        data_dir = tl.data_dir_of_service(svc)
        card = _read_card(data_dir)
        code, _ = tl.probe_login(port) if port else (None, "")
        facts.append(tl.TenantFact(
            instance_id=iid,
            desired_running=(card.get("status") == "running"),
            suspended=tl.suspended_flag_path(iid).exists(),
            http_ok=(code == 200),
        ))
        meta[iid] = (svc, port, data_dir, card)
    if not facts:
        print("[watch] 无租户实例")
        return 0

    decisions, new_state = tl.plan_watch_actions(
        facts, state, time.time(),
        heal_cooldown_sec=args.heal_cooldown, max_fail_streak=args.max_fail_streak)
    worst = 0
    for d in decisions:
        iid = d["instance_id"]
        line = f"[watch] {iid:24} {d['action']:8} {d['reason']}"
        if d["action"] == "heal":
            print(line)
            svc, port, data_dir, card = meta[iid]
            product_id = str(card.get("product") or "zhiliao")
            rc = _start_instance(iid, port, product_id, data_dir)
            if rc == 0 and tl.wait_ready(port, timeout_sec=args.ready_timeout):
                print(f"[watch] {iid:24} healed ✓（/login 200）")
                new_state[iid] = {"fail_streak": 0,
                                  "last_heal": new_state.get(iid, {}).get("last_heal", 0)}
            else:
                streak = new_state.get(iid, {}).get('fail_streak')
                print(f"[watch] {iid:24} 拉起后未就绪（streak={streak}）", file=sys.stderr)
                worst = 1
        else:
            print(line)
            if d["action"] == "give_up":
                # 连败转人工：这是「自愈救不回来」的边沿，必须吵醒人（防抖 4h）
                tl.emit_tenant_alert(
                    "tenant_down", f"🏠 托管租户 {iid} 反复拉起失败已转人工：{d['reason']}",
                    account_id=iid, debounce_sec=4 * 3600)
                worst = 1
    # ── 边缘探针（公网入口整链：DNS→VPS nginx→反向隧道→本机端口）──
    #    本地绿灯≠坐席可达（2026-08-07 pilot 事故：入口断腿时本地探针全绿，老板比机器
    #    先发现）。只探「应在跑+未暂停+已暴露+本地健康」的租户；本地不健康归上面自愈路径，
    #    边缘层不重复计数。决策纯函数 plan_edge_actions；自愈=踢隧道（runner 10s 重连）。
    if not args.no_edge:
        local_ok = {f.instance_id: f.http_ok for f in facts}
        edge_facts = []
        for iid, (_s2, _p2, _d2, card2) in meta.items():
            if (tl.suspended_flag_path(iid).exists()
                    or str(card2.get("status") or "") != "running"
                    or not card2.get("exposed") or not card2.get("public_url")
                    or not local_ok.get(iid)):
                continue
            url = str(card2["public_url"]).rstrip("/") + "/login"
            ecode, _t = _probe_public(url, tries=2, interval=2.0)
            edge_facts.append(tl.EdgeFact(instance_id=iid, edge_ok=(ecode == 200)))
        if edge_facts:
            edge_decisions, edge_state = tl.plan_edge_actions(
                edge_facts, state, time.time(),
                strike_limit=args.edge_strikes,
                kick_cooldown_sec=args.edge_kick_cooldown)
            for d in edge_decisions:
                iid = d["instance_id"]
                if d["action"] == "ok":
                    continue
                print(f"[watch] {iid:24} edge:{d['action']:11} {d['reason']}")
                if d["action"] == "kick_tunnel":
                    print(f"[watch] {iid:24} {tl.kick_tunnel()}")
                elif d["action"] == "alert":
                    pub = str((meta[iid][3] or {}).get("public_url") or "")
                    tl.emit_tenant_alert(
                        "tenant_edge_down",
                        f"🌐 托管租户 {iid} 公网入口不可达（本地实例健康 → 隧道/VPS/nginx "
                        f"腿断，已自动踢隧道仍未恢复）：{pub}",
                        account_id=iid, debounce_sec=1800)
                    worst = 1
            new_state[tl.EDGE_STATE_KEY] = edge_state
    # ── 到期治理巡检（告警为主；自动停机是 opt-in——`--auto-suspend-grace-days N`
    #    且只对订单驱动卡生效，安全轨全在 tl.should_auto_suspend 纯函数）──
    now = time.time()
    auto_grace = float(getattr(args, "auto_suspend_grace_days", 0) or 0)
    for iid, (_svc, port, data_dir, card) in meta.items():
        # 已停机=到期口径已执行（或运营主动停），不再催——否则 suspend 后告警响到永远
        if str(card.get("status") or "") != "running":
            continue
        _sync_tenant_notice(iid, data_dir, card, now)
        state_name, days = tl.expiry_status(card, now)
        if state_name == "expired":
            act, why = tl.should_auto_suspend(
                card, now, grace_days=auto_grace,
                protected=tl.is_protected(iid),
                suspended=tl.suspended_flag_path(iid).exists())
            if act:
                try:
                    _suspend_core(iid, port, data_dir,
                                  reason=f"auto_expired:{card.get('expiry_order')}")
                    print(f"[watch] {iid:24} AUTO-SUSPENDED  {why}")
                    tl.emit_tenant_alert(
                        "tenant_auto_suspended",
                        f"⏸️ 托管租户 {iid} 到期已自动停机（{why}）——客户续费下单后"
                        f"履约守护自动复机+叠期；人工恢复 tenant_ops resume {iid}",
                        account_id=iid, debounce_sec=60)
                    continue  # 已处置，不再发「转人工」告警
                except Exception as e:  # noqa: BLE001 - 自动停失败退回告警路径
                    print(f"[watch] {iid:24} 自动停机失败（退回人工告警）: {e}",
                          file=sys.stderr)
            print(f"[watch] {iid:24} EXPIRED  逾期 {abs(days):.1f} 天（到期口径=停机，"
                  f"确认未续费后人工 suspend；auto={why}）")
            tl.emit_tenant_alert(
                "tenant_expired",
                f"⏰ 托管租户 {iid} 已到期 {abs(days):.1f} 天（expires_at="
                f"{card.get('expires_at')}）——确认未续费后执行 "
                f"tenant_ops suspend {iid}；已续费则更新卡上 expires_at",
                account_id=iid, debounce_sec=12 * 3600)
        elif state_name == "expiring":
            print(f"[watch] {iid:24} EXPIRING 剩 {days:.1f} 天（催续费窗口）")
            tl.emit_tenant_alert(
                "tenant_expiring",
                f"⏳ 托管租户 {iid} 将于 {days:.1f} 天后到期（{card.get('expires_at')}）"
                f"——催续费窗口",
                account_id=iid, debounce_sec=24 * 3600)
    tl.save_watch_state(state_path, new_state)
    return worst


# ────────────────────────── backup（厂商侧灾备）──────────────────────────

def cmd_backup(args) -> int:
    stack = _load_stack()
    if args.all:
        targets = [(tl.instance_id_of(s), tl.data_dir_of_service(s))
                   for s in tl.tenant_services(stack)]
    else:
        if not args.instance_id:
            raise SystemExit("[错误] 给 instance_id 或 --all")
        _svc, _port, data_dir = _tenant_ctx(args.instance_id)
        targets = [(args.instance_id, data_dir)]
    if not targets:
        print("（无租户实例）")
        return 0
    import time as _t
    rc = 0
    for iid, data_dir in targets:
        try:
            out = tl.backups_dir(iid) / f"{iid}_{_t.strftime('%Y%m%d_%H%M%S')}.zip"
            counts = tl.backup_tenant_zip(data_dir, out)
            pruned = tl.prune_old_backups(tl.backups_dir(iid), keep=args.keep)
            for p in pruned:
                p.unlink(missing_ok=True)
            print(f"[backup] {iid}: db快照 {counts['db_snapshots']} + 文件 {counts['files']}"
                  f" → {out.name}（保留 {args.keep} 份，清理 {len(pruned)}）")
        except Exception as e:  # noqa: BLE001 - 逐租户隔离，一家失败不拖全队
            print(f"[backup] {iid}: 失败 {e}", file=sys.stderr)
            tl.emit_tenant_alert(
                "tenant_backup_fail", f"🗄️ 托管租户 {iid} 灾备失败：{e}",
                account_id=iid, debounce_sec=6 * 3600)
            rc = 1
    return rc


# ────────────────────────── restore（灾备恢复，完成 DR 闭环）──────────────────────────

def cmd_restore(args) -> int:
    _svc, port, data_dir = _tenant_ctx(args.instance_id)
    if not args.dry_run:  # dry-run 只读清单，不闸
        _guard_protected(args.instance_id, "restore", args.force_protected)
    # 安全闸①：绝不覆盖运行中实例（在线解压 = 腐化正在写的 SQLite）
    code, _ = tl.probe_login(port) if port else (None, "")
    if code == 200 and not args.force:
        raise SystemExit(
            f"[错误] 实例在运行（/login=200）；恢复会覆盖正在写的库。"
            f"先 `suspend {args.instance_id}`，或加 --force（危险）")
    # 备份包：显式 --backup 或最新
    if args.backup:
        zip_path = Path(args.backup)
    else:
        zip_path = tl.latest_backup(args.instance_id)
        if zip_path is None:
            raise SystemExit(f"[错误] 无备份包: {tl.backups_dir(args.instance_id)}\n"
                             f"  先 `backup {args.instance_id}`")
    try:
        names = tl.validate_backup_zip(zip_path)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[错误] 备份包校验失败: {e}")
    print(f"[restore] {args.instance_id} ← {zip_path.name}（{len(names)} 条目）")
    if args.dry_run:
        segs = {}
        for n in names:
            segs[n.split("/", 1)[0]] = segs.get(n.split("/", 1)[0], 0) + 1
        print(f"[dry-run] 将恢复到 {data_dir}：{segs}（未写盘，加去掉 --dry-run 执行）")
        return 0
    counts = tl.restore_backup(zip_path, data_dir)
    print(f"[restore] 已恢复 config={counts['config']} sessions={counts['sessions']} "
          f"other={counts['other']} → {data_dir}")
    # 域包 junction 恢复（备份不含 domains，它是指向引擎的软链）
    if not (Path(data_dir) / "domains").exists():
        print(f"[restore] {tl.ensure_junction(data_dir, str(ENGINE_DOMAINS))}")
    print(f"[restore] 完成。下一步：`resume {args.instance_id}` 拉起验收")
    return 0


# ────────────────────────── set-ai（AI 中转配置注入）──────────────────────────

def cmd_set_ai(args) -> int:
    _svc, port, data_dir = _tenant_ctx(args.instance_id)
    preset_path = Path(args.preset) if args.preset else tl.ai_preset_path()
    preset = tl.load_tenant_preset(preset_path)
    if not preset:
        raise SystemExit(f"[错误] 预设无可注入段（ai/licensing/platform_login）或文件缺失: {preset_path}\n"
                         f"  样板见 {tl.ai_preset_path().with_suffix('.example.yaml')}")
    lic = tl.resolve_preset_license(preset, args.instance_id)
    if lic:
        print(f"[set-ai] credpool 卡密（本租户专属）：{lic}")
    overlay = Path(data_dir) / "config" / "config.local.yaml"
    keys = tl.inject_tenant_preset(overlay, preset)
    print(f"[set-ai] {args.instance_id}: 写入 {len(keys)} 键（保注释）：{', '.join(keys[:8])}"
          + ("…" if len(keys) > 8 else ""))
    code, _ = tl.probe_login(port) if port else (None, "")
    if code == 200:
        print("[set-ai] 实例在跑：overlay 热重载约 30s 生效（保险起见可 suspend/resume 一轮）")
    else:
        print("[set-ai] 实例未跑：下次拉起生效")
    return 0


# ────────────────────────── expose / unexpose（公网暴露）──────────────────────────

SSH_CONFIG = REPO_ROOT / "deploy" / "ssh_config.boundless"
VPS_ALIAS = "bd2026"


def _vps(cmd: str, timeout: int = 90) -> tuple[int, str]:
    """VPS 上执行一条命令（BatchMode 免交互；返回 (rc, 输出)）。

    ``-n``（StdinNull）必带（2026-09-11，ssh_config.boundless 的 VPS 块也已带）：Windows
    自带 ssh.exe 9.5p1 跑一次性命令会随机卡在 stdin 读取上不退出（Win32-OpenSSH #1334），
    这里从不经 stdin 送数据（文件走 _vps_push/scp）。
    """
    r = subprocess.run(
        ["ssh", "-n", "-F", str(SSH_CONFIG), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         VPS_ALIAS, cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _vps_push(local: Path, remote: str) -> None:
    r = subprocess.run(
        ["scp", "-F", str(SSH_CONFIG), "-o", "BatchMode=yes", str(local),
         f"{VPS_ALIAS}:{remote}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        raise SystemExit(f"[错误] scp 失败: {(r.stderr or r.stdout).strip()}")


def _probe_public(url: str, tries: int = 5, interval: float = 2.0) -> tuple[int, str]:
    import urllib.request
    last = (0, "")
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                body = resp.read(2048).decode("utf-8", errors="replace")
                title = body.split("<title>", 1)[1].split("</title>", 1)[0].strip() \
                    if "<title>" in body else ""
                return resp.status, title
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", 0) or 0
            last = (int(code), "")
        time.sleep(interval)
    return last


VPS_PUBLIC_IP = "165.154.233.121"   # bd2026.cc（ssh_config.boundless 同源）


def cmd_expose(args) -> int:
    svc, port, data_dir = _tenant_ctx(args.instance_id)
    slug = tl.validate_slug(args.slug) if args.slug else tl.default_slug(args.instance_id)
    fqdn = f"{slug}.{tl.PUBLIC_DOMAIN}"

    # 1) 隧道端口登记 + 生效验证（隧道不通就不碰 nginx）
    if tl.ensure_tunnel_port(port):
        print(f"[expose] 隧道清单 += {port}；{tl.kick_tunnel()}")
    else:
        print(f"[expose] 隧道清单已含 {port}")
    ok = False
    for _ in range(12):
        rc, out = _vps(f"ss -tln | grep -q ':{port} ' && echo UP || echo DOWN")
        if rc == 0 and "UP" in out:
            ok = True
            break
        time.sleep(2)
    if not ok:
        raise SystemExit(
            f"[错误] VPS 侧 127.0.0.1:{port} 未在听——隧道 runner 未跑或未重连。\n"
            "  启动/注册见 deploy\\instances\\tenant_tunnel.ps1 头注释（TenantTunnel 任务）")
    print(f"[expose] 隧道生效 ✓（VPS 127.0.0.1:{port} LISTEN）")

    # 2) 形态决策：子域 DNS 就绪走子域，否则端口形态兜底（零 DNS 依赖）
    mode = args.mode
    if mode == "auto":
        rc, out = _vps(f"getent ahostsv4 {fqdn} 2>/dev/null | awk '{{print $1}}' | head -1")
        mode = "subdomain" if out.strip() == VPS_PUBLIC_IP else "port"
        if mode == "port":
            print(f"[expose] {fqdn} 无 A 记录 → 端口形态兜底；"
                  f"在域名商加 `*.{tl.PUBLIC_DOMAIN} A {VPS_PUBLIC_IP}` 后重跑即自动升级子域")

    name = f"tenant-{slug}.conf"
    if mode == "subdomain":
        public_url = f"https://{fqdn}"
        # 证书（http-01 走既有 80 catch-all 的 ACME 路径；幂等 --keep）
        rc, out = _vps(f"sudo -n test -d /etc/letsencrypt/live/{fqdn} && echo HAVE || echo MISS")
        if "HAVE" in out:
            print(f"[expose] 证书已存在: {fqdn}")
        else:
            print(f"[expose] 签发证书 {fqdn} …")
            rc, out = _vps(
                f"sudo -n certbot certonly --webroot -w /var/www/html -d {fqdn} "
                f"--cert-name {fqdn} --non-interactive --agree-tos --keep", timeout=180)
            if rc != 0:
                raise SystemExit(f"[错误] certbot 失败（nginx 未动）:\n{out[-800:]}")
            print("[expose] 证书签发 ✓（续期由 certbot.timer 自动）")
        conf = tl.render_nginx_site(slug, port)
    else:
        ports = svc.get("ports") or []
        if len(ports) < 2:
            raise SystemExit("[错误] 端口形态需要租户 alt_port（stack 条目 ports[1] 缺失）")
        public_port = int(ports[1])
        public_url = f"https://{tl.PUBLIC_DOMAIN}:{public_port}"
        # 公网端口须空闲（被占大概率是撞了别的服务；本租户重复 expose 时端口被自家
        # nginx 占用属正常——conf 同名覆盖后 reload 即收敛）
        rc, out = _vps(
            f"ss -tln | grep -q ':{public_port} ' && echo BUSY || echo FREE")
        if "BUSY" in out:
            rc2, has_conf = _vps(f"test -f /etc/nginx/sites-available/{name} && echo OURS || echo OTHER")
            if "OURS" not in has_conf:
                raise SystemExit(f"[错误] VPS 端口 {public_port} 已被他方占用，人工核查")
        conf = tl.render_nginx_site_port(slug, public_port, port)

    # 2.4) 扫描拦截片段（P1-9）：幂等推送仓库 SSOT → /etc/nginx/snippets/scanner-deny.conf。
    #      站点模板 include 了它——新 VPS/裸机若缺此文件，nginx -t 会失败，所以这里
    #      **先于**站点落盘推送；推送失败仅警告（老 VPS 已有文件，无害）。
    deny_snip = REPO_ROOT / "deploy" / "instances" / "scanner-deny.conf"
    if deny_snip.is_file():
        try:
            _vps_push(deny_snip, "/tmp/__scanner_deny.conf")
            rc, out = _vps("sudo -n mv /tmp/__scanner_deny.conf "
                           "/etc/nginx/snippets/scanner-deny.conf && echo SNIP_OK")
            print("[expose] 扫描拦截片段已同步 ✓" if "SNIP_OK" in out
                  else f"[警告] 拦截片段落位失败（不阻断）: {out[-200:]}")
        except SystemExit as e:
            print(f"[警告] 拦截片段推送失败（不阻断）: {e}", file=sys.stderr)
    else:
        print(f"[警告] 拦截片段源文件缺失（不阻断）: {deny_snip}", file=sys.stderr)

    # 2.5) 后端不可达友好页（P5）：幂等推送仓库 SSOT → /var/www/html/__tenant_down.html
    #      （error_page 502/503/504 的落点；推送失败只警告——缺页回落 nginx 默认 502，无害）
    down_page = REPO_ROOT / "deploy" / "instances" / "tenant_down.html"
    if down_page.is_file():
        try:
            _vps_push(down_page, "/tmp/__tenant_down.html")
            rc, out = _vps("sudo -n mv /tmp/__tenant_down.html "
                           "/var/www/html/__tenant_down.html && echo PAGE_OK")
            print("[expose] 友好页已同步 ✓" if "PAGE_OK" in out
                  else f"[警告] 友好页落位失败（不阻断）: {out[-200:]}")
        except SystemExit as e:
            print(f"[警告] 友好页推送失败（不阻断）: {e}", file=sys.stderr)
    else:
        print(f"[警告] 友好页源文件缺失（不阻断）: {down_page}", file=sys.stderr)

    # 3) nginx 站点（先落盘 sites-available → 软链 → nginx -t 不过即回滚，绝不带病 reload）
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False,
                                     encoding="utf-8", newline="\n") as tf:
        tf.write(conf)
        tmp_local = Path(tf.name)
    try:
        _vps_push(tmp_local, f"/tmp/{name}")
    finally:
        tmp_local.unlink(missing_ok=True)
    rc, out = _vps(
        f"sudo -n mv /tmp/{name} /etc/nginx/sites-available/{name} && "
        f"sudo -n ln -sf /etc/nginx/sites-available/{name} /etc/nginx/sites-enabled/{name} && "
        f"sudo -n nginx -t 2>&1 && sudo -n systemctl reload nginx && echo NGINX_OK")
    if "NGINX_OK" not in out:
        _vps(f"sudo -n rm -f /etc/nginx/sites-enabled/{name} /etc/nginx/sites-available/{name}")
        raise SystemExit(f"[错误] nginx 配置未过检，已回滚站点文件（运行中的 nginx 未受影响）:\n{out[-800:]}")
    print(f"[expose] nginx 站点上线 ✓（{mode} 形态）")

    # 4) TLS 后端语义：cookie_secure（保注释写 overlay；热重载 ~30s / 下次拉起生效）
    from src.utils.config_manager import set_yaml_key_preserving
    overlay = Path(data_dir) / "config" / "config.local.yaml"
    if set_yaml_key_preserving(overlay, ["web_admin", "cookie_secure"], True):
        print("[expose] overlay cookie_secure=true ✓")

    # 5) 公网验收 + 回写交付卡（失败时自动定位断点：VPS 本地回环探针分诊）
    code, title = _probe_public(f"{public_url}/login")
    if code == 200:
        print(f"[expose] 公网验收 ✓  {public_url}/login = 200  title={title!r}")
    elif code in (502, 504):
        print(f"[expose] 链路就绪（HTTP {code}=实例未在跑，resume 后即可访问）")
    else:
        probe_port = public_url.rsplit(":", 1)[-1] if ":" in public_url[8:] else "443"
        _rc, loop = _vps(
            f"curl -sk --max-time 6 --resolve {tl.PUBLIC_DOMAIN}:{probe_port}:127.0.0.1 "
            f"{public_url}/login -o /dev/null -w '%{{http_code}}'")
        if loop.strip() == "200":
            print(f"[expose] 链路已通到 VPS 边缘（VPS 本地回环={loop.strip()}），"
                  f"公网仍不可达 = **云厂商安全组未放行 {probe_port}**。\n"
                  f"  两条出路任选：① 控制台放行该端口；② 域名商加 "
                  f"`*.{tl.PUBLIC_DOMAIN} A {VPS_PUBLIC_IP}` 泛解析后重跑 expose"
                  f"（子域形态走 443，无需动安全组，推荐）", file=sys.stderr)
        else:
            print(f"[警告] 公网与 VPS 回环探针均异常（loop={loop.strip() or '?'}），"
                  "链路存在真断点，人工核查 nginx/隧道/实例", file=sys.stderr)
    # 交付卡三 URL 一并改写为公网基址（否则卡上仍是 127.0.0.1，客户打不开）
    pub_fields = tl.apply_public_base(public_url)
    _update_card(data_dir, slug=slug, exposed=True, expose_mode=mode, **pub_fields)
    # 交付即保护（2026-08-07 事故沉淀）：已暴露＝真人要用的入口，默认自动打保护旗，
    # 不靠人记得跑 protect；演练件（id/客户名/slug 含 drill）豁免防卡自家演练。
    card_now = _read_card(data_dir)
    if tl.is_drill_instance(args.instance_id, str(card_now.get("customer") or ""), slug):
        print("[expose] 演练实例（drill*）：不自动保护（演练的 suspend/deprovision 需畅通）")
    elif not tl.is_protected(args.instance_id):
        tl.write_protected_flag(args.instance_id, f"auto: exposed at {public_url}")
        _update_card(data_dir, protected=True)
        print("[expose] 已自动标记受保护（交付即保护；解除用 protect --off）")
    print(f"\n  公网地址 = {public_url}")
    print(f"  客户登录 = {pub_fields['login_url']}   （账号/初始密码见交付卡，勿外发 auth_token）")
    return 0


def cmd_unexpose(args) -> int:
    _svc, _port, data_dir = _tenant_ctx(args.instance_id)
    _guard_protected(args.instance_id, "unexpose", args.force_protected)
    card = _read_card(data_dir)
    slug = args.slug or card.get("slug") or tl.default_slug(args.instance_id)
    name = f"tenant-{slug}.conf"
    rc, out = _vps(
        f"sudo -n rm -f /etc/nginx/sites-enabled/{name} /etc/nginx/sites-available/{name} && "
        f"sudo -n nginx -t 2>&1 && sudo -n systemctl reload nginx && echo NGINX_OK")
    if "NGINX_OK" not in out:
        raise SystemExit(f"[错误] 下线失败:\n{out[-500:]}")
    _update_card(data_dir, exposed=False)
    print(f"[unexpose] {slug}.{tl.PUBLIC_DOMAIN} 已下线（证书/隧道端口保留无害；卡已更新）")
    return 0


# ────────────────────────── main ──────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="托管租户实例全生命周期（生产双实例不在管辖）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("provision", help="开通（默认干跑；--apply 全链）")
    p.add_argument("--customer", required=True)
    p.add_argument("--product", default="zhiliao", choices=["zhiliao", "tongyi"])
    p.add_argument("--instance-id", default="")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--no-ai-preset", action="store_true",
                   help="跳过 .ops\\tenant_ai_preset.yaml 自动注入")
    p.add_argument("--ready-timeout", type=int, default=150)
    p.set_defaults(fn=cmd_provision)

    p = sub.add_parser("list", help="已登记租户")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("status", help="各租户运行态（只读）")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("suspend", help="暂停（停进程+落旗）")
    p.add_argument("instance_id")
    p.add_argument("--reason", default="suspended")
    p.add_argument("--grace", type=int, default=8)
    p.add_argument("--force-protected", action="store_true",
                   help="受保护租户破玻璃（推 TG 留痕）")
    p.set_defaults(fn=cmd_suspend)

    p = sub.add_parser("protect", help="标记受保护租户（真人在用；危险命令须 --force-protected）")
    p.add_argument("instance_id")
    p.add_argument("--off", action="store_true", help="解除保护")
    p.add_argument("--reason", default="", help="保护原因（如：坐席生产入口）")
    p.set_defaults(fn=cmd_protect)

    p = sub.add_parser("resume", help="恢复（删旗+拉起+等就绪）")
    p.add_argument("instance_id")
    p.add_argument("--ready-timeout", type=int, default=150)
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("export", help="退租数据导出（要求已停）")
    p.add_argument("instance_id")
    p.add_argument("--include-sessions", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("deprovision", help="退租=暂停+导出（数据保留原地）")
    p.add_argument("instance_id")
    p.add_argument("--include-sessions", action="store_true")
    p.add_argument("--grace", type=int, default=8)
    p.add_argument("--force-protected", action="store_true",
                   help="受保护租户破玻璃（推 TG 留痕）")
    p.set_defaults(fn=cmd_deprovision)

    p = sub.add_parser("watch", help="租户自愈一轮（计划任务节拍调用；生产双实例不在内）")
    p.add_argument("--heal-cooldown", type=int, default=600)
    p.add_argument("--max-fail-streak", type=int, default=3)
    p.add_argument("--ready-timeout", type=int, default=90)
    p.add_argument("--reset", default="", help="重置某租户的失败计数后退出")
    p.add_argument("--no-edge", action="store_true",
                   help="跳过公网边缘探针（默认开：探已暴露租户的 public_url 整链）")
    p.add_argument("--edge-strikes", type=int, default=2,
                   help="边缘连败几轮才动作（防公网抖动误伤）")
    p.add_argument("--edge-kick-cooldown", type=int, default=1200,
                   help="踢隧道的机器级冷却秒数（隧道全租户共享，防反复踢）")
    p.add_argument("--auto-suspend-grace-days", type=float, default=0,
                   help="到期自动停机：逾期超过 N 天自动 suspend（0=关，默认；"
                        "只停订单驱动卡且绝不碰受保护租户，续费单到账自动复机）")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("backup", help="灾备打包（活库安全快照；含授权与登录态）")
    p.add_argument("instance_id", nargs="?", default="")
    p.add_argument("--all", action="store_true")
    p.add_argument("--keep", type=int, default=14)
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("restore", help="灾备恢复（从备份包重建；要求已停，完成 DR 闭环）")
    p.add_argument("instance_id")
    p.add_argument("--backup", default="", help="备份 zip 路径（缺省=最新一份）")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="越过「运行中拒恢复」闸（危险）")
    p.add_argument("--force-protected", action="store_true",
                   help="受保护租户破玻璃（推 TG 留痕）")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("set-ai", help="注入 AI 中转配置（读 .ops\\tenant_ai_preset.yaml，保注释写 overlay）")
    p.add_argument("instance_id")
    p.add_argument("--preset", default="")
    p.set_defaults(fn=cmd_set_ai)

    p = sub.add_parser("expose", help="公网暴露（隧道+nginx；auto=子域 DNS 就绪走子域否则端口形态）")
    p.add_argument("instance_id")
    p.add_argument("--slug", default="", help="子域（缺省 instance_id 转连字符）")
    p.add_argument("--mode", default="auto", choices=["auto", "subdomain", "port"])
    p.set_defaults(fn=cmd_expose)

    p = sub.add_parser("unexpose", help="公网下线（删 nginx 站点；证书/隧道保留）")
    p.add_argument("instance_id")
    p.add_argument("--slug", default="")
    p.add_argument("--force-protected", action="store_true",
                   help="受保护租户破玻璃（推 TG 留痕）")
    p.set_defaults(fn=cmd_unexpose)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
