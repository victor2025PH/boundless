"""真网关自检（B1.5 前置）：在装了 GATEWAY_KEY 的机器上跑一次，拿到脱敏后的三种 /lookup 样例。

    .venv\\Scripts\\python.exe -m domains.player_care.gateway_probe --config config_player\\config.yaml --phone 09xxxxxxxxx
    .venv\\Scripts\\python.exe -m domains.player_care.gateway_probe --url http://165.154.233.219 --phone 09xxxxxxxxx --uid 123456 --out samples.json

三个探针按序打：
    found      —— 传入的真号 / 会员号（应查到）；
    not_found  —— 一个不存在的号（应 404 或空 chatx_text）；
    bad_key    —— 故意用错密钥（应 401 / 403；网关鉴权是否真的在起作用）。

每个探针输出：HTTP 状态 / ok / found / error / 延迟 / 返回体顶层键名（顺带回答「有没有 agent 字段」）/
脱敏后的 chatx_text（≥ 3 位数字换 #，探针里的源号码全遮）/ extract_games 与 detect_deposit 当前解析结果。
密钥永不进输出；``--out`` 写出的 JSON 即 ``tests/fixtures/player_care_lookup_samples.json`` 的格式，
测试 ``test_lookup_samples_file`` 会逐条回放——真样例到手后换文件、改 expect、再调 extract_games 直到绿。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from .gateway import (
    DEFAULT_KEY_ENV,
    LookupResult,
    PlayerGateway,
    _urllib_transport,
    extract_games,
    normalize_ph_phone,
    phone_variants,
    resolve_gateway_cfg,
)
from .profile import detect_deposit

NOT_FOUND_PHONE = "639000000000"   # 探针用的不存在号（网关认 9 开头 / 补 63）
BAD_KEY = "probe-invalid-key"
_DIGITS_RE = re.compile(r"\d{3,}")
_KEY_LIKE = ("key", "token", "secret", "password", "authorization")


def redact_text(text: Any, secrets: Optional[List[str]] = None) -> str:
    """≥ 3 位数字全部换成同长度的 ``100…``（保持“是个数”，detect_deposit / 数字闸回放仍能跑，
    但真值已毁）；``secrets`` 里的串（源号码各写法）先整段遮成 ``*``。"""
    s = str(text or "")
    for sec in sorted({x for x in (secrets or []) if x}, key=len, reverse=True):
        s = s.replace(sec, "*" * len(sec))
    return _DIGITS_RE.sub(lambda m: "1" + "0" * (len(m.group(0)) - 1), s)


def redact_raw(raw: Any, secrets: Optional[List[str]] = None) -> Any:
    """返回体递归脱敏：键名像密钥的值整段遮，字符串按 redact_text，数字 ≥ 100 一律换成 100。"""
    if isinstance(raw, dict):
        out: Dict[str, Any] = {}
        for k, v in raw.items():
            if any(t in str(k).lower() for t in _KEY_LIKE):
                out[str(k)] = "***"
            else:
                out[str(k)] = redact_raw(v, secrets)
        return out
    if isinstance(raw, list):
        return [redact_raw(v, secrets) for v in raw]
    if isinstance(raw, bool) or raw is None:
        return raw
    if isinstance(raw, (int, float)):
        return raw if abs(raw) < 100 else type(raw)(100)
    return redact_text(raw, secrets)


def replay_sample(sample: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把一条样例（status + body）用假 transport 回放进 PlayerGateway，算出当前解析结果
    （与样例的 ``expect`` 同形）。探针写样例和测试回放走同一条路，两边不会口径分裂。"""
    status = int(sample.get("status") or 0)
    body = str(sample.get("body") or "").encode("utf-8")
    if sample.get("transport_error"):
        def _tp(url, headers, b, timeout):
            raise TimeoutError(str(sample["transport_error"]))
    else:
        def _tp(url, headers, b, timeout):
            return status, body
    c = {"enabled": True, "url": "http://replay", "key": "replay", "cache_ttl_sec": 0.001, **(cfg or {})}
    res = PlayerGateway(c, transport=_tp).lookup("balance", phone=NOT_FOUND_PHONE)
    text = res.chatx_text
    return {
        "ok": res.ok,
        "found": res.found,
        "error": res.error,
        "games": extract_games(text) if res.usable else [],
        "deposit": bool(detect_deposit(text)) if res.usable else False,
    }


def redact_body(body_txt: str, secrets: Optional[List[str]] = None) -> str:
    """返回体脱敏：JSON 走 redact_raw 后重新序列化（保持可解析），非 JSON 走 redact_text。"""
    try:
        data = json.loads(body_txt)
    except Exception:
        return redact_text(body_txt, secrets)
    return json.dumps(redact_raw(data, secrets), ensure_ascii=False)


def _record_from_result(name: str, res: LookupResult, *, status: int, body: bytes,
                        secrets: List[str]) -> Dict[str, Any]:
    body_txt = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    rec: Dict[str, Any] = {
        "name": name,
        "status": status,
        "latency_ms": res.latency_ms,
        "raw_keys": sorted(res.raw.keys()) if isinstance(res.raw, dict) else [],
        "has_agent_field": any("agent" in str(k).lower() for k in (res.raw or {}).keys()) if isinstance(res.raw, dict) else False,
        # 回放用：测试里假 transport 直接回这一对（body 已脱敏，expect 也按脱敏后的 body 算，自洽）
        "body": redact_body(body_txt, secrets)[:4000],
        "raw": redact_raw(res.raw, secrets) if isinstance(res.raw, dict) else {},
        "chatx_text": redact_text(res.chatx_text, secrets),
    }
    if not res.ok and res.error and not res.status:
        rec["transport_error"] = res.error
    rec["expect"] = replay_sample(rec)
    return rec


class _CapturingTransport:
    """包一层真 transport，把 status/body 留给样例记录。"""

    def __init__(self, inner=None) -> None:
        self._inner = inner or _urllib_transport
        self.last_status = 0
        self.last_body = b""

    def __call__(self, url, headers, body, timeout):
        status, raw = self._inner(url, headers, body, timeout)
        self.last_status, self.last_body = int(status), raw
        return status, raw


def run_probes(cfg: Dict[str, Any], *, phone: str = "", uid: str = "", q: str = "balance",
               transport=None, include_bad_key: bool = True) -> Dict[str, Any]:
    """三探针；``cfg`` 已由 resolve_gateway_cfg 规范化（含 key）。"""
    base = dict(cfg)
    base["enabled"] = True
    base["cache_ttl_sec"] = 0.001
    secrets = [x for x in phone_variants(phone) if x] + ([uid] if uid else [])
    samples: List[Dict[str, Any]] = []

    plan = [("found", base, normalize_ph_phone(phone), uid),
            ("not_found", base, NOT_FOUND_PHONE, "")]
    if include_bad_key:
        plan.append(("bad_key", {**base, "key": BAD_KEY}, normalize_ph_phone(phone) or NOT_FOUND_PHONE, uid))
    for name, c, ph, u in plan:
        if name == "found" and not (ph or u):
            samples.append({"name": name, "skipped": "no --phone / --uid given"})
            continue
        cap = _CapturingTransport(transport)
        gw = PlayerGateway(c, transport=cap)
        res = gw.lookup(q, phone=ph, uid=u)
        samples.append(_record_from_result(name, res, status=cap.last_status, body=cap.last_body, secrets=secrets))
    return {
        "probe_version": 1,
        "ts": int(time.time()),
        "url": cfg.get("url", ""),
        "lookup_path": cfg.get("lookup_path", "/lookup"),
        "key_env": cfg.get("key_env", DEFAULT_KEY_ENV),
        "key_present": bool(cfg.get("key")),
        "samples": samples,
    }


def render_report(rep: Dict[str, Any]) -> str:
    lines = [f"gateway: {rep.get('url')}{rep.get('lookup_path')}   key_env={rep.get('key_env')} key_present={rep.get('key_present')}"]
    for s in rep.get("samples", []):
        if s.get("skipped"):
            lines.append(f"[{s['name']}] skipped: {s['skipped']}")
            continue
        e = s.get("expect", {})
        lines.append(f"[{s['name']}] http={s['status']} ok={e.get('ok')} found={e.get('found')} "
                     f"error={e.get('error') or '-'} latency={s['latency_ms']}ms")
        lines.append(f"    raw_keys={s.get('raw_keys')} agent_field={s.get('has_agent_field')}")
        if s.get("chatx_text"):
            for ln in str(s["chatx_text"]).splitlines()[:12]:
                lines.append(f"    | {ln}")
        elif s.get("body"):
            lines.append(f"    body: {str(s['body'])[:300]}")
        lines.append(f"    games={e.get('games')} deposit={e.get('deposit')}")
    return "\n".join(lines)


def _load_cfg(args) -> Dict[str, Any]:
    if args.config:
        from src.utils.config_manager import ConfigManager
        cfg = resolve_gateway_cfg(ConfigManager(args.config))
    else:
        cfg = resolve_gateway_cfg({"player_gateway": {"enabled": True, "url": args.url or "",
                                                       "key_env": args.key_env or DEFAULT_KEY_ENV}})
    if args.url:
        cfg["url"] = args.url.rstrip("/")
    if args.key_env:
        cfg["key_env"] = args.key_env
        cfg["key"] = str(os.environ.get(args.key_env) or "").strip() or cfg.get("key", "")
    if args.timeout:
        cfg["timeout_sec"] = float(args.timeout)
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="player_gateway /lookup 三探针自检（输出已脱敏，可直接贴回）")
    ap.add_argument("--config", default="", help="player 实例 config.yaml（读 player_gateway 段）")
    ap.add_argument("--url", default="", help="覆盖 / 直接指定网关 url，如 http://165.154.233.219")
    ap.add_argument("--key-env", default="", help="密钥环境变量名（默认 GATEWAY_KEY）")
    ap.add_argument("--phone", default="", help="一个真实玩家手机号（9… / 09… / 639…），用于 found 探针")
    ap.add_argument("--uid", default="", help="真实会员号（可选）")
    ap.add_argument("--q", default="balance", help="随请求带的 q 文本")
    ap.add_argument("--timeout", type=float, default=0, help="覆盖 timeout_sec")
    ap.add_argument("--no-bad-key", action="store_true", help="不打 bad_key 探针")
    ap.add_argument("--out", default="", help="把样例 JSON 写到这个文件（测试样例格式）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON 而不是文本")
    args = ap.parse_args(argv)

    cfg = _load_cfg(args)
    if not cfg.get("url"):
        print("缺网关 url：--url 或 config 里的 player_gateway.url", file=sys.stderr)
        return 2
    if not cfg.get("key"):
        print(f"缺密钥：环境变量 {cfg.get('key_env')} 为空（不要写进配置 / 聊天记录）", file=sys.stderr)
        return 2
    rep = run_probes(cfg, phone=args.phone, uid=args.uid, q=args.q, include_bad_key=not args.no_bad_key)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
