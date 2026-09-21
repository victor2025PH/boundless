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
    extract_agent,
    extract_games,
    has_deposit,
    normalize_ph_phone,
    phone_variants,
    resolve_gateway_cfg,
)
from .profile import detect_deposit

NOT_FOUND_PHONE = "639000000000"   # 探针用的不存在号（网关认 9 开头 / 补 63）
BAD_KEY = "probe-invalid-key"
_DIGITS_RE = re.compile(r"\d{3,}")
# 保形不毁值的「非敏感数字」：日期 2026-09-21 / 2026/09/21、时间 10:05(:30)、版本 v1.2.3 / 1.2.3
_KEEP_SHAPE_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"          # 日期
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"          # 时间
    r"|\bv?\d+(?:\.\d+){2,}\b"                # 版本号
)
_KEY_LIKE = ("key", "token", "secret", "password", "authorization")
# 值整段遮掉的人身字段（真样例里都是能反查到人的）：代理号 / 登录名 / 昵称 / 号码 / IP
_PII_KEYS = ("agent", "login_name", "name", "phone", "_ip", "ips")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# 真网关 chatx_text 每行开头 ``UID x / 昵称 / VIP…``：昵称遮掉
_SUMMARY_NAME_RE = re.compile(r"(UID\s+\S+\s*/\s*)[^/\n]+?(\s*/)")
# 保形列表只留前几项（同 IP / 同设备关联用户这类列表能很长，样例文件不必全存）
_LIST_CAP = 4
_AGENT_KEY_RE = re.compile(r"agent|代理|affiliate|referr?er|upline|promot(?:er|ion)_?code", re.IGNORECASE)
_AGENT_LINE_RE = re.compile(r"(?im)^\s*(agent|代理(?:号|人)?|affiliate|referrer|upline)\s*[:：=]")


def redact_text(text: Any, secrets: Optional[List[str]] = None) -> str:
    """≥ 3 位数字换成同长度的 ``100…``（保持“是个数”，detect_deposit / 数字闸回放仍能跑，但真值已毁）；
    日期 / 时间 / 版本号形态保留（只是数值，不含账户信息；留着好判断真样例格式）；
    ``secrets`` 里的串（源号码各写法）先整段遮成 ``*``。"""
    s = str(text or "")
    for sec in sorted({x for x in (secrets or []) if x}, key=len, reverse=True):
        s = s.replace(sec, "*" * len(sec))
    keep: List[str] = []

    def _hold(m: "re.Match[str]") -> str:
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    s = _IPV4_RE.sub("0.0.0.0", s)
    s = _SUMMARY_NAME_RE.sub(r"\1***\2", s)
    s = _KEEP_SHAPE_RE.sub(_hold, s)
    s = _DIGITS_RE.sub(lambda m: "1" + "0" * (len(m.group(0)) - 1), s)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], s)


def find_agent_fields(raw: Any, chatx_text: Any = "") -> List[str]:
    """返回体里所有像「代理 / 上级」的字段路径（递归，如 ``player.agent_id`` / ``meta[0].upline``），
    以及 chatx_text 里以 agent/代理 开头的行（记为 ``chatx_text:agent``）。空表 = 没有 agent 字段。"""
    hits: List[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}" if path else str(k)
                if _AGENT_KEY_RE.search(str(k)):
                    hits.append(p)
                _walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")

    _walk(raw, "")
    for m in _AGENT_LINE_RE.finditer(str(chatx_text or "")):
        hits.append(f"chatx_text:{m.group(1).lower()}")
    return hits


def redact_raw(raw: Any, secrets: Optional[List[str]] = None) -> Any:
    """返回体递归脱敏：键名像密钥的值整段遮，人身字段（agent / login_name / name / phone / ip）
    的值遮成 ``***``（保留非空与否），字符串按 redact_text，数字 ≥ 100 一律换成 100，
    键名本身也走 redact_text（网关 ``phone_hits`` 用手机号当键），长列表只留前 _LIST_CAP 项。"""
    if isinstance(raw, dict):
        out: Dict[str, Any] = {}
        for k, v in raw.items():
            kl = str(k).lower()
            rk = redact_text(str(k), secrets)
            if any(t in kl for t in _KEY_LIKE):
                out[rk] = "***"
            elif any(t in kl for t in _PII_KEYS) and isinstance(v, (str, list)) and v:
                out[rk] = "***" if isinstance(v, str) else ["***"] * min(len(v), _LIST_CAP)
            else:
                out[rk] = redact_raw(v, secrets)
        return out
    if isinstance(raw, list):
        return [redact_raw(v, secrets) for v in raw[:_LIST_CAP]]
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
        "games": extract_games(res) if res.usable else [],
        "deposit": bool(has_deposit(res) or detect_deposit(text)) if res.usable else False,
        "agent": bool(extract_agent(res)) if res.usable else False,
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
        "agent_fields": find_agent_fields(res.raw if isinstance(res.raw, dict) else {}, res.chatx_text),
        # 回放用：测试里假 transport 直接回这一对（body 已脱敏，expect 也按脱敏后的 body 算，自洽）
        "body": redact_body(body_txt, secrets),
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
        lines.append(f"    raw_keys={s.get('raw_keys')} agent_fields={s.get('agent_fields') or '无'}")
        if s.get("chatx_text"):
            for ln in str(s["chatx_text"]).splitlines()[:12]:
                lines.append(f"    | {ln}")
        elif s.get("body"):
            lines.append(f"    body: {str(s['body'])[:300]}")
        lines.append(f"    games={e.get('games')} deposit={e.get('deposit')} agent={e.get('agent')}")
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
