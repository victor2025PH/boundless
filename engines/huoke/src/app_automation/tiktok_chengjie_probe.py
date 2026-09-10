"""TikTok × 智聊 真机联调探针（TK-3 P2，2026-09-11）。

上真机前先把「配置 → 连通 → 鉴权 → 桥挂载 → 设备绑定 → 进线往返」一条线跑通，每一步给出**可执行的下一步**，
而不是让人对着 500 行日志猜。零副作用为默认：``check`` 只读；``bind`` 只写设备绑定；``roundtrip`` 会往智聊
投一条**探针私信**（peer=``chengjie_probe``，msg_id 带 ``probe:`` 前缀），会在智聊工作台出现一个探针会话——
这是联调需要看见的东西，但要知道它在。

用法（huoke 根目录）::

    python scripts/tiktok_chengjie_probe.py                 # check：配置 + 连通 + 鉴权 + 桥
    python scripts/tiktok_chengjie_probe.py --bind          # + 绑定 device_account_map 里所有设备（或 --device）
    python scripts/tiktok_chengjie_probe.py --roundtrip     # + 探针私信进线 → pending 只看
    python scripts/tiktok_chengjie_probe.py --json

退出码：0 全绿；1 配置问题；2 连通 / 鉴权 / 桥未挂；3 绑定或往返失败。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from . import tiktok_chengjie_bridge as cj

STATUS_PATH = "/api/tiktok/huoke/status"
PROBE_PEER = "chengjie_probe"

EXIT_OK, EXIT_CONFIG, EXIT_CONNECT, EXIT_ACTION = 0, 1, 2, 3


def _mask(tok: str) -> str:
    tok = str(tok or "")
    return "" if not tok else (tok if len(tok) <= 6 else f"{tok[:3]}…{tok[-2:]}")


def _step(name: str, ok: bool, detail: str = "", fix: str = "", **extra: Any) -> Dict[str, Any]:
    d: Dict[str, Any] = {"step": name, "ok": bool(ok), "detail": detail}
    if fix and not ok:
        d["fix"] = fix
    d.update(extra)
    return d


def check_config(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """只读：reply_engine / endpoint / token / 时区 / 设备映射。"""
    c = cj.reply_cfg(cfg)
    steps: List[Dict[str, Any]] = []
    steps.append(_step("reply_engine", c["reply_engine"] == cj.ENGINE_CHENGJIE, f"reply_engine={c['reply_engine']}",
                       fix="config/apps/tiktok.yaml 把 reply_engine 改为 chengjie（local 是本地大脑，不会把私信交给智聊）"))
    steps.append(_step("endpoint", bool(c["endpoint"]), c["endpoint"] or "(空)",
                       fix="tiktok.yaml chengjie.endpoint 填智聊地址，例 http://192.168.0.118:8780（或环境变量 CHENGJIE_TIKTOK_ENDPOINT）"))
    steps.append(_step("token", bool(c["token"]), f"token={_mask(c['token'])}" if c["token"] else "(空)",
                       fix="tiktok.yaml chengjie.token 填智聊 api_auth 的 Bearer（或环境变量 CHENGJIE_API_TOKEN）"))
    bad_tz: List[str] = []
    for tz in [c["timezone"]] + [str((v or {}).get("timezone") or "") if isinstance(v, dict) else "" for v in c["device_account_map"].values()]:
        if tz and not _tz_ok(tz):
            bad_tz.append(tz)
    steps.append(_step("timezone", not bad_tz, "IANA 时区" + (f" 非法: {bad_tz}" if bad_tz else " 正常（空＝智聊按 UTC 切日上限）"),
                       fix="时区用 IANA 名，例 Asia/Manila / Europe/Rome；智聊按它切私信日上限"))
    n_map = len(c["device_account_map"])
    steps.append(_step("device_account_map", True, f"{n_map} 台设备有映射" if n_map else "空映射：所有设备回落 account_id/username（单机可接受）",
                       count=n_map))
    steps.append(_step("handback_poll", c["handback_poll_sec"] > 0,
                       f"轮询器每 {c['handback_poll_sec']:.0f}s 问一次待发" if c["handback_poll_sec"] > 0 else "轮询关：只在 check_inbox 尾部认领",
                       fix="tiktok.yaml chengjie.handback_poll_sec 设 ≥30（0 = 关）"))
    return steps


def _tz_ok(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:
        return False


def check_connectivity(cfg: Dict[str, Any], *, http: Optional[Callable] = None) -> List[Dict[str, Any]]:
    """GET /status：区分「连不上 / token 错 / 桥没挂 / OK」四种，各给下一步。"""
    c = cj.reply_cfg(cfg)
    if not c["endpoint"]:
        return [_step("connect", False, "endpoint 为空，跳过连通检查", fix="先修 endpoint")]
    status, resp = cj._http(c, "GET", STATUS_PATH, http=http)
    if status == 0:
        return [_step("connect", False, f"连不上 {c['endpoint']}: {resp.get('error')}",
                      fix="确认智聊实例已启动、地址/端口正确、本机到智聊网络通（curl <endpoint>/api/health）")]
    if status in (401, 403):
        return [_step("connect", True, f"{c['endpoint']} 可达"),
                _step("auth", False, f"HTTP {status}：token 被拒", fix="token 要与智聊 api_auth 一致（智聊 config.yaml api_token / 环境变量）")]
    if status == 404:
        return [_step("connect", True, f"{c['endpoint']} 可达"), _step("auth", True, "未被 401/403 拒"),
                _step("bridge_mounted", False, "HTTP 404：智聊未挂 huoke 桥路由",
                      fix="智聊 config.yaml tiktok.huoke_bridge.enabled: true 后重启（默认关）")]
    if status != 200 or not isinstance(resp, dict) or not resp.get("ok"):
        return [_step("connect", True, f"{c['endpoint']} 可达"),
                _step("bridge_mounted", False, f"HTTP {status}: {str(resp)[:160]}", fix="看智聊日志 [tiktok-huoke]")]
    pol = resp.get("policy") or {}
    return [_step("connect", True, f"{c['endpoint']} 可达"), _step("auth", True, "token 通过"),
            _step("bridge_mounted", True, "桥已挂载",
                  policy={"dm_daily_cap": pol.get("dm_daily_cap"), "dm_max_len": pol.get("dm_max_len"),
                          "peer_silent_hours": pol.get("peer_silent_hours")},
                  queue={"queued": resp.get("queued"), "sent": resp.get("sent"), "failed": resp.get("failed")},
                  health=resp.get("health") or {})]


def do_bind(cfg: Dict[str, Any], devices: List[str], *, http: Optional[Callable] = None) -> List[Dict[str, Any]]:
    c = cj.reply_cfg(cfg)
    targets = list(devices) or list(c["device_account_map"].keys())
    if not targets:
        return [_step("bind", False, "没有可绑定的设备：device_account_map 为空且未传 --device",
                      fix="--device <序列号> 或在 tiktok.yaml chengjie.device_account_map 里登记")]
    out: List[Dict[str, Any]] = []
    for did in targets:
        acc = cj.account_for_device(did, c)
        status, resp = cj.bind_device(did, cfg=c, http=http)
        ok = status == 200 and bool(resp.get("ok", True)) and not resp.get("error")
        out.append(_step(f"bind:{did}", ok,
                         f"account={acc['account_id']} username={acc['username'] or '-'} tz={acc['timezone'] or '-'} → "
                         + (f"health={resp.get('health')} dm_daily_cap={resp.get('dm_daily_cap')}" if ok else f"HTTP {status} {resp.get('error') or resp}"),
                         fix="bad_timezone → 改 IANA 名；account_id 必填 → 映射里补 account_id；503/0 → 先过连通检查"))
    return out


def do_roundtrip(cfg: Dict[str, Any], device_id: str, *, http: Optional[Callable] = None, now: Optional[float] = None,
                 peer: str = PROBE_PEER) -> List[Dict[str, Any]]:
    """探针私信进线（in）→ 智聊应 accepted=1 → pending 只看。会在智聊工作台出现 ``tiktok:user:<peer>`` 探针会话。"""
    c = cj.reply_cfg(cfg)
    t = float(now if now is not None else time.time())
    conv = {"contact": peer, "messages": [
        {"msg_id": f"probe:{int(t)}", "text": f"[chengjie_probe] 联调探针 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))}",
         "direction": "inbound"}]}
    status, resp = cj.post_dm(conv, device_id=device_id, cfg=c, http=http)
    ok_in = status == 200 and bool(resp.get("ok"))
    steps = [_step("roundtrip:dm_in", ok_in,
                   f"accepted={resp.get('accepted')} drafted={resp.get('drafted')} → 工作台应出现会话 tiktok:user:{peer}" if ok_in
                   else f"HTTP {status} {resp.get('error') or resp}",
                   fix="404 → 桥未挂；401 → token；503 device_offline 不会在此出现（进线不查心跳）；看智聊日志 [tiktok-huoke]")]
    if not ok_in:
        return steps
    status, pend = cj.pending_handback(device_id=device_id, cfg=c, http=http)
    ok_p = status == 200 and bool(pend.get("ok"))
    steps.append(_step("roundtrip:pending", ok_p,
                       f"queued={pend.get('queued')} claimed={pend.get('claimed')} oldest_wait={pend.get('oldest_wait_sec')}s"
                       + ("（智聊自动发默认关：drafted 只是草稿，人工在工作台点发送后 queued 才会 +1，轮询器随后建真机任务）" if ok_p else "")
                       if ok_p else f"HTTP {status} {pend.get('error') or pend}",
                       fix="pending 404 → 智聊版本旧（缺 /handback/pending），升到 TK-3 P1 之后"))
    return steps


def run_probe(cfg: Optional[Dict[str, Any]] = None, *, http: Optional[Callable] = None, bind: bool = False,
              roundtrip: bool = False, devices: Optional[List[str]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    c = cj.reply_cfg(cfg)
    devices = [d for d in (devices or []) if d]
    report: Dict[str, Any] = {"endpoint": c["endpoint"], "reply_engine": c["reply_engine"], "steps": [], "exit_code": EXIT_OK}
    cfg_steps = check_config(c)
    report["steps"] += cfg_steps
    hard_cfg_fail = any(not s["ok"] for s in cfg_steps if s["step"] in ("reply_engine", "endpoint", "token", "timezone"))
    if hard_cfg_fail:
        report["exit_code"] = EXIT_CONFIG
        return report
    conn = check_connectivity(c, http=http)
    report["steps"] += conn
    if any(not s["ok"] for s in conn):
        report["exit_code"] = EXIT_CONNECT
        return report
    if bind or roundtrip:
        b = do_bind(c, devices, http=http)
        report["steps"] += b
        if any(not s["ok"] for s in b):
            report["exit_code"] = EXIT_ACTION
            return report
    if roundtrip:
        did = devices[0] if devices else (next(iter(c["device_account_map"]), "") or "probe-device")
        r = do_roundtrip(c, did, http=http, now=now)
        report["steps"] += r
        if any(not s["ok"] for s in r):
            report["exit_code"] = EXIT_ACTION
    return report


def format_report(report: Dict[str, Any]) -> str:
    lines = [f"智聊联调探针  endpoint={report.get('endpoint') or '(空)'}  reply_engine={report.get('reply_engine')}"]
    for s in report.get("steps") or []:
        lines.append(f"  [{'OK ' if s['ok'] else 'FAIL'}] {s['step']:<22} {s.get('detail', '')}")
        if not s["ok"] and s.get("fix"):
            lines.append(f"         → {s['fix']}")
    code = report.get("exit_code", 0)
    verdict = {EXIT_OK: "全绿：可以上真机（reply_engine=chengjie 生效后，收件箱巡检会把对方开口的私信交给智聊）",
               EXIT_CONFIG: "配置未就绪", EXIT_CONNECT: "连通 / 鉴权 / 桥未就绪", EXIT_ACTION: "绑定或往返失败"}[code]
    lines.append(f"结论：{verdict}  (exit {code})")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="TikTok × 智聊 真机联调探针")
    ap.add_argument("--bind", action="store_true", help="绑定 device_account_map 里的设备（或 --device）")
    ap.add_argument("--roundtrip", action="store_true", help="探针私信进线 → pending 只看（会在智聊工作台出现探针会话）")
    ap.add_argument("--device", action="append", default=[], help="设备序列号，可多次")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = run_probe(bind=a.bind, roundtrip=a.roundtrip, devices=a.device)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else format_report(rep))
    return int(rep["exit_code"])


__all__ = ["run_probe", "check_config", "check_connectivity", "do_bind", "do_roundtrip", "format_report", "main",
           "EXIT_OK", "EXIT_CONFIG", "EXIT_CONNECT", "EXIT_ACTION", "PROBE_PEER"]
