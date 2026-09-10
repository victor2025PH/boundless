# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 独立进程入口（实施97 线 B）。

    python -m src.integrations.wechat_pc --backend-url http://127.0.0.1:18799 --token admin \
        --account-id my-wechat --tier copilot --self-check

与智聊后端同机运行；PC 微信须已由主人扫码登录并保持窗口可见。``--self-check`` 只跑锚点自检并退出。
默认档位 ``copilot``（只读+建议，不发送）；``semi``/``auto_reply`` 需显式指定，``auto_reply`` 还需
``--risk-ack``。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="智聊 · 个人微信 PC 副驾")
    ap.add_argument("--backend-url", default="http://127.0.0.1:18799")
    ap.add_argument("--token", default="admin")
    ap.add_argument("--account-id", default="wechat-pc")
    ap.add_argument("--label", default="个人微信 · PC 副驾", help="工作台里显示的账号名（心跳带上，纠正首见入站用联系人名当账号名）")
    ap.add_argument("--tier", default="copilot", choices=["copilot", "semi", "auto_reply"])
    ap.add_argument("--risk-ack", action="store_true")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--ticks", type=int, default=0, help="跑 N 轮后退出并打印统计（0=常驻）")
    ap.add_argument("--state-dir", default="", help="已入站指纹落盘目录（默认 %%LOCALAPPDATA%%\\ChatX\\wechat_pc）")
    ap.add_argument("--config", default="", help="智聊 YAML 配置路径：读取 platform_login.wechat_pc 策略块（命令行参数覆盖）")
    ap.add_argument("--work-hours", default="", help="发送时段覆盖，如 9-22 或 0-24")
    ap.add_argument("--connected-days", type=float, default=-1,
                    help="微信号已在本机登录多少天（预热期日配额判定；默认按新号预热）")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("comtypes", "comtypes.client", "comtypes._post_coinit", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from src.integrations.wechat_pc.uia_backend import UiaBackend, available
    if not available():
        print("需要 Windows + uiautomation（pip install uiautomation）", file=sys.stderr)
        return 2
    backend = UiaBackend()
    rep = backend.self_check()
    print(json.dumps(rep, ensure_ascii=False))
    if args.self_check:
        return 0 if rep.get("window") else 1

    import os
    from src.integrations.wechat_pc.policy import resolve_policy
    from src.integrations.wechat_pc.service import BridgeClient, SeenStore, WeChatPcService
    pc_cfg: dict = {}
    if args.config:
        try:
            import yaml
            with open(args.config, "r", encoding="utf-8") as fh:
                _doc = yaml.safe_load(fh) or {}
            pc_cfg = dict(((_doc.get("platform_login") or {}).get("wechat_pc")) or {})
        except Exception as ex:  # noqa: BLE001
            print(f"读取配置失败 {args.config}: {ex}", file=sys.stderr)
    pc_cfg["tier"] = args.tier
    pc_cfg["risk_ack"] = bool(args.risk_ack or pc_cfg.get("risk_ack"))
    if args.work_hours:
        try:
            a, b = args.work_hours.replace("~", "-").split("-", 1)
            pc_cfg["work_hours"] = [int(a), int(b)]
        except Exception:
            print(f"--work-hours 格式应为 9-22，忽略: {args.work_hours!r}", file=sys.stderr)
    policy = resolve_policy({"platform_login": {"wechat_pc": pc_cfg}})
    import time as _time
    connected_at = (_time.time() - args.connected_days * 86400.0) if args.connected_days >= 0 else 0.0
    if policy.sends_allowed and backend.readonly:
        print("锚点自检未通过（缺 %s）→ 强制只读运行" % ",".join(rep.get("missing") or []), file=sys.stderr)
    state_dir = args.state_dir or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ChatX", "wechat_pc")
    seen = SeenStore(os.path.join(state_dir, f"seen_{args.account_id}.json"))
    from src.integrations.wechat_pc.identity import ChatIdentityCache
    identity = ChatIdentityCache(path=os.path.join(state_dir, f"identity_{args.account_id}.json"))
    svc = WeChatPcService(backend, BridgeClient(args.backend_url, args.token),
                          account_id=args.account_id, policy=policy, seen_store=seen, identity=identity,
                          connected_at=connected_at, account_label=args.label,
                          notify=lambda k, d: logging.getLogger("wechat_pc").warning("[通知主人] %s %s", k, d))
    print(f"运行中：tier={policy.tier} readonly={backend.readonly} account={args.account_id} "
          f"work_hours={policy.work_hours} reply_only={policy.reply_only}")
    try:
        if args.ticks > 0:
            import time as _t
            for i in range(args.ticks):
                s = svc.tick()
                print(f"tick {i + 1}/{args.ticks}: {json.dumps(s, ensure_ascii=False)}")
                if i + 1 < args.ticks:
                    _t.sleep(max(0.5, args.interval))
        else:
            svc.run_forever(interval_sec=args.interval)
    except KeyboardInterrupt:
        pass
    print(json.dumps(svc.stats.as_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
