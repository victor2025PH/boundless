#!/usr/bin/env python3
"""provision_instance.py — 按订单准备一个新 chengjie 客户实例（dry-run 默认）。

Sprint5：自助下单「自动开通」的确定性半程。规划 id/端口/数据根 → 渲染 config.local.yaml
overlay → 幂等登记 deploy/stack.json → 打印拉起/验收命令。**不**拉起进程、**不**签发 license。

用法：
  python scripts/provision_instance.py --product zhiliao --customer "Acme Ltd"        # 只规划打印
  python scripts/provision_instance.py --product zhiliao --customer "Acme Ltd" --apply # 写盘+登记 stack

--apply 会：建数据根骨架、从 config.example.yaml 起底 config.yaml、写 config.local.yaml、
幂等追加 stack.json 条目（enabled=false）。随后按打印的命令：建 domains junction、
（可选）放 license.key、跑 preflight、start_zhiliao.ps1 拉起、verify_instance.ps1 验收。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # engines/chengjie
REPO_ROOT = BASE.parent.parent                          # boundless
sys.path.insert(0, str(BASE))

from src.ops.instance_provisioner import (  # noqa: E402
    build_stack_entry,
    launch_command,
    plan_instance,
    render_overlay,
    upsert_stack_entry,
)

STACK_PATH = REPO_ROOT / "deploy" / "stack.json"
EXAMPLE_CFG = BASE / "config" / "config.example.yaml"


def _load_stack() -> dict:
    try:
        return json.loads(STACK_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 读取 stack.json 失败: {e}", file=sys.stderr)
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="准备新 chengjie 客户实例（dry-run 默认）")
    ap.add_argument("--product", required=True, choices=["zhiliao", "tongyi"])
    ap.add_argument("--customer", required=True, help="客户标识（生成 instance_id/slug）")
    ap.add_argument("--instance-id", default="", help="显式 instance_id（默认 <product>_<slug>）")
    ap.add_argument("--host", default="127.0.0.1", help="web 绑定（默认本地；对外经反代）")
    ap.add_argument("--enable-monitoring", action="store_true")
    ap.add_argument("--apply", action="store_true", help="真正写盘 + 登记 stack.json（默认只规划）")
    args = ap.parse_args(argv)

    stack = _load_stack()
    plan = plan_instance(
        stack, product=args.product, customer=args.customer,
        instance_id=(args.instance_id or None))
    overlay = render_overlay(plan, host=args.host, enable_monitoring=args.enable_monitoring)
    entry = build_stack_entry(plan)

    print(f"[plan] instance_id = {plan.instance_id}")
    print(f"[plan] service_id  = {plan.service_id}")
    print(f"[plan] data_dir    = {plan.data_dir}")
    print(f"[plan] ports       = web {plan.web_port} / alt {plan.alt_port} / metrics {plan.metrics_port}")
    print(f"[plan] product_id  = {plan.product_id}  (遥测)")

    if not args.apply:
        print("\n[dry-run] 未写盘。加 --apply 执行。将写：")
        print(f"  - {plan.data_dir}\\config\\config.yaml         (从 config.example.yaml 起底)")
        print(f"  - {plan.data_dir}\\config\\config.local.yaml   (overlay，见下)")
        print(f"  - deploy/stack.json  += service {plan.service_id} (enabled=false)")
        print("\n--- config.local.yaml 预览 ---")
        print(overlay)
        _print_handoff(plan)
        return 0

    # ── apply：建骨架 + 写配置 + 登记 stack（幂等）──
    data_dir = Path(plan.data_dir)
    cfg_dir = data_dir / "config"
    for sub in ("config", "sessions", "logs", "events\\spool", "ledger_outbox"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    cfg_yaml = cfg_dir / "config.yaml"
    if not cfg_yaml.exists():
        if EXAMPLE_CFG.exists():
            shutil.copy2(EXAMPLE_CFG, cfg_yaml)
            print(f"[apply] 起底 config.yaml ← config.example.yaml")
        else:
            print(f"[警告] 未找到 {EXAMPLE_CFG}，config.yaml 未创建（需手工）", file=sys.stderr)
    else:
        print(f"[apply] config.yaml 已存在，跳过（不覆盖现网数据）")
    overlay_path = cfg_dir / "config.local.yaml"
    if not overlay_path.exists():
        overlay_path.write_text(overlay, encoding="utf-8")
        print(f"[apply] 写 config.local.yaml（含随机 secret_key/auth_token，请保管）")
    else:
        print(f"[apply] config.local.yaml 已存在，跳过（不覆盖）")

    stack, action = upsert_stack_entry(stack, entry)
    if action == "added":
        STACK_PATH.write_text(
            json.dumps(stack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[apply] stack.json += {plan.service_id}（enabled=false）")
    else:
        print(f"[apply] stack.json 已有 {plan.service_id}，跳过（幂等）")
    _print_handoff(plan)
    return 0


def _print_handoff(plan) -> None:
    print("\n--- 后续（交 PowerShell / 人工，provision 不代做）---")
    print(f"  1) domains junction:  New-Item -ItemType Junction -Path \"{plan.data_dir}\\domains\" "
          f"-Target \"{BASE}\\domains\"")
    print(f"  2) license（可选）:   fulfill_chatx.py 签发 → 放 {plan.data_dir}\\config\\license.key")
    print(f"  3) preflight:         powershell ... preflight_instance.ps1 -DataDir \"{plan.data_dir}\" "
          f"-Ports {plan.web_port},{plan.alt_port}")
    print(f"  4) 拉起:              {launch_command(plan)}")
    print(f"  5) 验收:              powershell ... verify_instance.ps1 -Base http://127.0.0.1:{plan.web_port} ...")
    print("  注：status/watchdog 对新 instance_id 的自愈识别需按运维手册扩展。")


if __name__ == "__main__":
    raise SystemExit(main())
