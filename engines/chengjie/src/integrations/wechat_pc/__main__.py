# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 独立进程入口（实施97 线 B）。

    python -m src.integrations.wechat_pc --backend-url http://127.0.0.1:18799 --token-file <TOKEN.txt> \
        --account-id my-wechat --config <config.local.yaml> --self-check

与智聊后端同机运行；PC 微信须已由主人扫码登录并保持窗口可见。``--self-check`` 只跑锚点自检并退出。

档位来源（2026-09-19 P0 修正）：**配置文件 ``platform_login.wechat_pc`` 是权威**，命令行 ``--tier`` /
``--risk-ack`` / ``--work-hours`` 仅在显式给出时覆盖（旧脚本兼容）。不传 ``--tier`` 时，引导页保存的档位
会在配置文件变更后 **热生效**（每轮 sleep 间检查 mtime），不必重启进程。``auto_reply`` 仍要求 ``risk_ack``
（无确认自动降半自动，见 :func:`policy.resolve_policy`）。

令牌来源优先级：``--token`` > ``--token-file`` > 环境变量（``--token-env``，默认 ``CHATX_ADMIN_TOKEN``）> ``admin``。
后端 supervisor 拉起时走环境变量，令牌不出现在进程命令行里。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

DEFAULT_TOKEN_ENV = "CHATX_ADMIN_TOKEN"


def load_pc_cfg(path: str) -> Dict[str, Any]:
    """读 YAML 的 ``platform_login.wechat_pc`` 块；文件缺失/坏 YAML 回空 dict，绝不抛。"""
    if not path:
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        blk = ((doc.get("platform_login") or {}).get("wechat_pc")) or {}
        return dict(blk) if isinstance(blk, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def build_policy_cfg(file_cfg: Dict[str, Any], *, tier: Optional[str] = None, risk_ack: bool = False,
                     work_hours: str = "") -> Dict[str, Any]:
    """配置文件块 + 命令行显式覆盖 → 交给 :func:`policy.resolve_policy` 的块（纯函数）。

    - ``tier`` 为 None 时保留文件值（P0 修正：不再把默认 ``copilot`` 写死进运行态）；
    - ``risk_ack`` 只能加不能减（命令行确认 or 文件已确认）；
    - ``work_hours`` 形如 ``9-22`` / ``0~24``，坏格式忽略。
    """
    cfg = dict(file_cfg or {})
    if tier:
        cfg["tier"] = str(tier).strip().lower()
    cfg["risk_ack"] = bool(risk_ack or cfg.get("risk_ack"))
    if work_hours:
        try:
            a, b = str(work_hours).replace("~", "-").split("-", 1)
            cfg["work_hours"] = [int(a), int(b)]
        except Exception:
            pass
    return cfg


def resolve_token(token: Optional[str], token_file: str, token_env: str) -> str:
    """令牌解析（纯函数）：显式 > 文件 > 环境变量 > ``admin``。"""
    if token:
        return str(token).strip()
    if token_file:
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                t = fh.read().strip()
            if t:
                return t
        except Exception:  # noqa: BLE001
            pass
    env_name = token_env or DEFAULT_TOKEN_ENV
    t = os.environ.get(env_name, "").strip()
    return t or "admin"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="智聊 · 个人微信 PC 副驾")
    ap.add_argument("--backend-url", default="http://127.0.0.1:18799")
    ap.add_argument("--token", default=None, help="管理员令牌（明文；优先级最高，建议改用 --token-file 或环境变量）")
    ap.add_argument("--token-file", default="", help="令牌文件路径（引导页 prepare 落盘的 TOKEN.txt）")
    ap.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help=f"令牌环境变量名（默认 {DEFAULT_TOKEN_ENV}）")
    ap.add_argument("--account-id", default="wechat-pc")
    ap.add_argument("--label", default="个人微信 · PC 副驾", help="工作台里显示的账号名（心跳带上，纠正首见入站用联系人名当账号名）")
    ap.add_argument("--tier", default=None, choices=["copilot", "semi", "auto_reply"],
                    help="显式覆盖档位（不传则以配置文件为准并热生效）")
    ap.add_argument("--risk-ack", action="store_true")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--ticks", type=int, default=0, help="跑 N 轮后退出并打印统计（0=常驻）")
    ap.add_argument("--state-dir", default="", help="已入站指纹落盘目录（默认 %%LOCALAPPDATA%%\\ChatX\\wechat_pc）")
    ap.add_argument("--config", default="", help="智聊 YAML 配置路径：读取 platform_login.wechat_pc 策略块（命令行显式参数覆盖）")
    ap.add_argument("--work-hours", default="", help="发送时段覆盖，如 9-22 或 0-24")
    ap.add_argument("--connected-days", type=float, default=-1,
                    help="微信号已在本机登录多少天（预热期日配额判定；默认按新号预热）")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--no-voice", action="store_true", help="禁用语音通路（不探测声卡、不认领 voice 命令）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("comtypes", "comtypes.client", "comtypes._post_coinit", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("wechat_pc")

    from src.integrations.wechat_pc.uia_backend import UiaBackend, available
    if not available():
        print("需要 Windows + uiautomation（pip install uiautomation）", file=sys.stderr)
        return 2
    backend = UiaBackend()
    rep = backend.self_check()
    print(json.dumps(rep, ensure_ascii=False))
    if args.self_check:
        return 0 if rep.get("window") else 1

    from src.integrations.wechat_pc.policy import resolve_policy
    from src.integrations.wechat_pc.service import BridgeClient, SeenStore, WeChatPcService

    def _policy_from_disk():
        return resolve_policy({"platform_login": {"wechat_pc": build_policy_cfg(
            load_pc_cfg(args.config), tier=args.tier, risk_ack=args.risk_ack, work_hours=args.work_hours)}})

    policy = _policy_from_disk()
    import time as _time
    connected_at = (_time.time() - args.connected_days * 86400.0) if args.connected_days >= 0 else 0.0
    if policy.sends_allowed and backend.readonly:
        print("锚点自检未通过（缺 %s）→ 强制只读运行" % ",".join(rep.get("missing") or []), file=sys.stderr)
    state_dir = args.state_dir or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ChatX", "wechat_pc")
    seen = SeenStore(os.path.join(state_dir, f"seen_{args.account_id}.json"))
    from src.integrations.wechat_pc.identity import ChatIdentityCache
    identity = ChatIdentityCache(path=os.path.join(state_dir, f"identity_{args.account_id}.json"))
    token = resolve_token(args.token, args.token_file, args.token_env)
    # 语音通路（可选）：虚拟声卡在且两端采样率一致才挂上；启动即自愈上次没还回去的默认麦克风
    voice = None
    voice_note = "voice=off(no_audio_libs)"
    if not args.no_voice:
        try:
            from src.integrations.wechat_pc import audio_cable
            if audio_cable.available():
                voice = audio_cable.VoiceCable(state_dir)
                vi = voice.info()
                voice_note = (f"voice={'ready' if voice.ready() else 'not_ready'} cable={vi.present} "
                              f"sr={vi.render_sr}/{vi.capture_sr} anchors={rep.get('voice_ready')}")
                if voice.healed:
                    log.warning("[voice] 已把默认麦克风从虚拟声卡还回 %s", voice.healed)
        except Exception:  # noqa: BLE001
            log.debug("[voice] 声卡探测失败", exc_info=True)
            voice_note = "voice=off(probe_failed)"
    svc = WeChatPcService(backend, BridgeClient(args.backend_url, token),
                          account_id=args.account_id, policy=policy, seen_store=seen, identity=identity,
                          connected_at=connected_at, account_label=args.label, voice=voice,
                          media_dir=os.path.join(state_dir, "voice_out"),
                          notify=lambda k, d: log.warning("[通知主人] %s %s", k, d))
    print(f"运行中：tier={policy.tier} readonly={backend.readonly} account={args.account_id} "
          f"work_hours={policy.work_hours} reply_only={policy.reply_only} {voice_note}", flush=True)
    from src.integrations.wechat_pc.policy import caps_relaxed
    relaxed = caps_relaxed(policy)
    if relaxed:
        log.warning("[policy] 配额比默认宽松（测试值？上客户前还回）：%s",
                    " ".join(f"{k}={cur}(默认{dft})" for k, (cur, dft) in relaxed.items()))

    # 档位热生效：配置文件 mtime 变了就重解析；显式 --tier 仍优先（build_policy_cfg 里覆盖）
    last_mtime = [_mtime(args.config)]

    def _sleep_and_watch(sec: float) -> None:
        _time.sleep(sec)
        if not args.config:
            return
        m = _mtime(args.config)
        if m == last_mtime[0]:
            return
        last_mtime[0] = m
        try:
            new_policy = _policy_from_disk()
        except Exception:  # noqa: BLE001
            return
        if new_policy != svc.policy:
            log.info("[policy] 配置已变更 → tier=%s work_hours=%s（热生效，未重启）", new_policy.tier, new_policy.work_hours)
            svc.policy = new_policy
            print(f"档位已更新：tier={new_policy.tier} work_hours={new_policy.work_hours}", flush=True)

    try:
        if args.ticks > 0:
            for i in range(args.ticks):
                s = svc.tick()
                print(f"tick {i + 1}/{args.ticks}: {json.dumps(s, ensure_ascii=False)}")
                if i + 1 < args.ticks:
                    _sleep_and_watch(max(0.5, args.interval))
        else:
            svc.run_forever(interval_sec=args.interval, sleep=_sleep_and_watch)
    except KeyboardInterrupt:
        pass
    print(json.dumps(svc.stats.as_dict(), ensure_ascii=False))
    return 0


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
