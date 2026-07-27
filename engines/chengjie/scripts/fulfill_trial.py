#!/usr/bin/env python3
"""fulfill_trial.py — 官网试用领取的厂商机履约端（Ed25519 私钥不出本机）。

补齐 P2 试用链最后一环。整条链：

    客户端「注册领 7 天」→ POST /api/trial/claim（官网按机器码去重建台账）
    → **本脚本**（厂商机轮询待办 → 本地私钥签发 → 回填官网）
    → 客户端轮询 /api/trial/claim-status 拿到 license → 落盘激活

两条队列，一次 pass 都走：

  1. ``status=pending``   → 签 7 天试用授权（绑机器指纹）
  2. ``needs_topup=1``    → 客服已核销绑定码 → 签「加客服送 10 万字符」加量凭证

安全口径与 fulfill_chatx.py 一致：私钥只经 ``--priv`` 从本机文件读，绝不上 VPS、
绝不写进日志/台账。本地审计流水只记 claim_id / 指纹短码 / 额度 / 到期，不记凭证本身。

用法：
  # 单次履约（cron/手动）
  python scripts/fulfill_trial.py --site https://bd2026.cc --key $ADMIN_KEY \\
      --priv config/.vendor_license_private.pem --once

  # 常驻守护（默认 60s 一轮）
  python scripts/fulfill_trial.py --site https://bd2026.cc --key $ADMIN_KEY \\
      --priv config/.vendor_license_private.pem --interval 60

  # 自测：用一次性临时钥匙对着本地 dev server 跑通「领取→签发→验签→绑机」全链
  python scripts/fulfill_trial.py --self-test --site http://localhost:3571 \\
      --key ci-admin-key --console-key ci-console-key
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 厂商机是 Windows，PS5.1 默认 GBK：不改的话一个「✓」就能让整轮履约崩在 print 上。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from src.licensing.chatx_fulfillment import (  # noqa: E402
    TRIAL_GIFT_CHARS,
    TRIAL_SPEC,
    build_trial_payload,
)
from src.licensing.license_manager import issue_license  # noqa: E402
from src.licensing.topup_voucher import issue_topup_voucher  # noqa: E402

#: 本地审计流水（厂商机侧，用于对账「我到底签发过什么」）
AUDIT_PATH = Path(__file__).resolve().parent.parent / "logs" / "fulfill_trial.jsonl"

_FP_CHARS = set("0123456789ABCDEF-")


# ── 纯逻辑（可单测，不碰网络/私钥）─────────────────────────────────────────────

def valid_fingerprint(fp: str) -> bool:
    """机器指纹是否形如 ``XXXX-XXXX-XXXX-XXXX``（16 位 hex 分四组）。

    脏数据必须在签发前就被挡下并标 rejected，否则 build_trial_payload 抛异常，
    该条会永远堵在 pending 队列头部反复失败。
    """
    s = str(fp or "").strip().upper()
    if not s or set(s) - _FP_CHARS:
        return False
    groups = s.split("-")
    return len(groups) == 4 and all(len(g) == 4 for g in groups)


def topup_ref(claim_id: str) -> str:
    """加量凭证的兑换幂等键。

    **必须由 claim_id 确定性派生、不能带时间戳**：签完凭证但回填失败时，下一轮会
    重新签一张；ref 相同则客户端 redeem 只会入账一次（redeem_topup_voucher 按 ref
    幂等）。带时间戳的话一次网络抖动就等于白送一份额度。
    """
    return f"trial-topup-{str(claim_id or '').strip()}"


def plan_license_work(claims: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把 pending 队列分成「可签发」与「该驳回」两堆（纯函数）。"""
    ok: List[Dict[str, Any]] = []
    bad: List[Dict[str, Any]] = []
    for c in claims or []:
        if c.get("has_license"):
            continue  # 已有授权，等官网自己翻状态；不重复签
        (ok if valid_fingerprint(c.get("fingerprint", "")) else bad).append(c)
    return ok, bad


def oldest_pending_minutes(claims: List[Dict[str, Any]], *, now: Optional[float] = None) -> int:
    """待办队列里最老一条等了多少分钟（空队列/时间戳不可解析 → 0）。"""
    import datetime as _dt
    now_ts = float(now if now is not None else time.time())
    oldest = 0.0
    for c in claims or []:
        raw = str((c or {}).get("created_at") or "").strip()
        if not raw:
            continue
        try:
            t = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
            oldest = max(oldest, now_ts - t.timestamp())
        except Exception:
            continue
    return int(max(0.0, oldest) // 60)


def gift_chars_for(claim: Dict[str, Any], default_chars: int = TRIAL_GIFT_CHARS) -> int:
    """本条加量该给多少字符：客服核销时若填了 bind_chars 以它为准，否则用默认赠量。"""
    n = int(claim.get("bind_chars") or 0)
    return n if n > 0 else int(default_chars)


# ── HTTP 薄壳 ──────────────────────────────────────────────────────────────

def _http(url: str, *, method: str = "GET", key: str = "", body: Optional[dict] = None,
          timeout: int = 20) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    if key:
        req.add_header("x-setup-key", key)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {url} {detail}") from e
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e} @ {url}") from e


def _audit(kind: str, **fields: Any) -> None:
    """本地流水。**刻意不记 license/voucher 明文**——签发物只交付给官网，不落两份。"""
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": int(time.time()), "kind": kind, **fields}
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计失败不能阻断履约


# ── 一轮履约 ───────────────────────────────────────────────────────────────

def run_once(site: str, key: str, priv_hex: str, *, days: Optional[int] = None,
             chars: Optional[int] = None, gift: int = TRIAL_GIFT_CHARS,
             limit: int = 100, dry_run: bool = False, log=print) -> Dict[str, int]:
    """跑一轮：签发待办试用 + 签发待办加量。返回各项计数。"""
    base = site.rstrip("/")
    stat = {"issued": 0, "rejected": 0, "topup": 0, "failed": 0, "backlog_min": 0}

    # ① 试用授权
    resp = _http(f"{base}/api/admin/trial-claims?status=pending&limit={limit}", key=key)
    pending = list(resp.get("claims") or [])
    # 积压年龄：履约端一旦挂掉，用户就在客户端干等。把「最老的待办等了多久」显式喊出来，
    # cron 的输出/告警才抓得到「签发链停摆」——这是本脚本唯一能自证活着的信号。
    stat["backlog_min"] = oldest_pending_minutes(pending)
    if stat["backlog_min"] >= 15:
        log(f"  ⚠ 最老待办已积压 {stat['backlog_min']} 分钟（共 {len(pending)} 条）")
    ok_list, bad_list = plan_license_work(pending)

    for c in bad_list:
        log(f"  ✗ {c['id']} 指纹非法({c.get('fingerprint','')!r}) → rejected")
        if not dry_run:
            _http(f"{base}/api/admin/trial-claims", method="POST", key=key,
                  body={"id": c["id"], "status": "rejected", "reason": "bad_fingerprint"})
            _audit("reject", claim=c["id"], reason="bad_fingerprint")
        stat["rejected"] += 1

    for c in ok_list:
        try:
            payload = build_trial_payload(
                customer=c.get("contact") or "", machine=c["fingerprint"],
                claim_id=c["id"], days=days, chars=chars)
            token = issue_license(payload, priv_hex)
            log(f"  ✓ {c['id']} → {payload['lic_id']} plan={payload['plan']} "
                f"chars={payload['included_chars']} exp={payload.get('exp', '-')}")
            if not dry_run:
                _http(f"{base}/api/admin/trial-claims", method="POST", key=key,
                      body={"id": c["id"], "license": token, "status": "issued"})
                _audit("issue", claim=c["id"], lic_id=payload["lic_id"],
                       fp=c["fingerprint"], chars=payload["included_chars"],
                       exp=payload.get("exp", 0))
            stat["issued"] += 1
        except Exception as e:
            log(f"  ! {c['id']} 签发失败: {e}")
            stat["failed"] += 1

    # ② 加客服送额度（客服已核销绑定码的）
    resp2 = _http(f"{base}/api/admin/trial-claims?needs_topup=1&limit={limit}", key=key)
    for c in list(resp2.get("claims") or []):
        if c.get("has_topup_voucher"):
            continue
        try:
            n = gift_chars_for(c, gift)
            fp8 = str(c.get("fingerprint") or "").replace("-", "")[:8] or "unknown"
            token = issue_topup_voucher(
                priv_hex, chars=n, ref=topup_ref(c["id"]),
                lic_id=f"trial-{fp8}", customer=c.get("contact") or "",
                note="trial-cs-gift")
            log(f"  ✓ {c['id']} 加量 {n} 字符 ref={topup_ref(c['id'])}")
            if not dry_run:
                _http(f"{base}/api/admin/trial-claims", method="POST", key=key,
                      body={"id": c["id"], "topup_voucher": token, "topup_chars": n})
                _audit("topup", claim=c["id"], chars=n, fp=c.get("fingerprint", ""))
            stat["topup"] += 1
        except Exception as e:
            log(f"  ! {c['id']} 加量签发失败: {e}")
            stat["failed"] += 1

    return stat


# ── 自测：临时钥匙对着 dev server 跑通全链 ──────────────────────────────────

def self_test(site: str, key: str, console_key: str = "", log=print) -> int:
    """不用真私钥，生成一次性钥匙对，验证「领取→签发→验签→绑机」端到端成立。"""
    import os
    import uuid

    from src.licensing.license_manager import LicenseManager, generate_keypair
    from src.licensing.topup_voucher import verify_topup_voucher

    base = site.rstrip("/")
    kp = generate_keypair()
    priv, pub = kp["private_hex"], kp["public_hex"]
    stamp = int(time.time())
    failures: List[str] = []

    def check(cond: bool, name: str) -> None:
        log(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            failures.append(name)

    # 用 BOUNDLESS_MACHINE_ID 把本进程的「本机指纹」改写成一次性值：
    # 官网按指纹去重，若拿真指纹跑，第二次自测就会命中上一次的单子（而那张授权是
    # 上一把临时钥匙签的）→ 满屏莫名其妙的 FAIL。改写后每轮自测互相隔离，
    # 而绑机断言依然是真的——签发与验签取的是同一个（被改写的）指纹。
    os.environ["BOUNDLESS_MACHINE_ID"] = f"selftest-{uuid.uuid4().hex[:12]}"
    from src.licensing.machine_bridge import machine_fingerprint
    real_fp = machine_fingerprint()
    log(f"[1/5] 一次性自测指纹 {real_fp or '(不可用)'}")
    if not real_fp:
        log("! 指纹模块不可用（platform/licensing 缺失？），跳过绑机断言")

    # 本机指纹 + 一个异机指纹：前者应验签通过，后者应被**绑机检查**判 invalid。
    # 异机指纹同样每轮随机——用常量的话第二轮会去重命中上轮的单，那张授权是上一把
    # 临时钥匙签的，于是「被拒」变成验签失败而非绑机不符，断言就成了假通过。
    foreign_fp = "-".join(uuid.uuid4().hex[:16].upper()[i:i + 4] for i in range(0, 16, 4))
    cases = [("real", real_fp or "AAAA-BBBB-CCCC-DDDD"), ("foreign", foreign_fp)]
    ids: Dict[str, str] = {}
    for tag, fp in cases:
        r = _http(f"{base}/api/trial/claim", method="POST",
                  body={"fingerprint": fp, "contact": f"@selftest{stamp}{tag}",
                        "source": "self-test"})
        ids[tag] = str(r.get("claim_id") or "")
        check(bool(r.get("ok")) and bool(ids[tag]), f"[2/5] 领取建单 ({tag})")

    log("[3/5] 履约一轮")
    stat = run_once(base, key, priv, log=lambda m: log("    " + m))
    check(stat["issued"] >= 2, f"[3/5] 签发 {stat['issued']} 条 / 失败 {stat['failed']}")

    log("[4/5] 客户端取件 + 验签")
    tokens: Dict[str, str] = {}
    for tag, _fp in cases:
        r = _http(f"{base}/api/trial/claim-status?id={ids[tag]}")
        tokens[tag] = str(r.get("license") or "")
        check(r.get("status") == "issued" and bool(tokens[tag]), f"      取件 ({tag})")

    if tokens.get("real"):
        st = LicenseManager(license_token=tokens["real"], public_key_hex=pub).status()
        check(st.state == "active", f"      本机验签 active (实际 {st.state})")
        check(st.plan == TRIAL_SPEC["plan"], f"      plan={TRIAL_SPEC['plan']} (实际 {st.plan})")
        check(st.days_left is not None and 5 <= st.days_left <= 7,
              f"      剩余 {st.days_left} 天（7 天档，grace 不得叠加）")
    if tokens.get("foreign") and real_fp:
        st2 = LicenseManager(license_token=tokens["foreign"], public_key_hex=pub).status()
        check(st2.state == "invalid", f"      异机授权被拒 (实际 {st2.state})")

    if not console_key:
        log("[5/5] 跳过加量腿（未给 --console-key）")
    else:
        log("[5/5] 客服核销 → 加量凭证")
        rb = _http(f"{base}/api/trial/bind-code", method="POST", body={"claim_id": ids["real"]})
        code = str(rb.get("bind_code") or "")
        check(bool(code), f"      绑定码 {code or '(空)'}")
        rq = urllib.request.Request(f"{base}/api/console/trial-redeem", method="POST",
                                    data=json.dumps({"code": code}).encode("utf-8"))
        rq.add_header("content-type", "application/json")
        rq.add_header("x-console-key", console_key)
        with urllib.request.urlopen(rq, timeout=20) as resp:
            check(json.loads(resp.read().decode("utf-8")).get("ok") is True, "      客服核销")
        s2 = run_once(base, key, priv, log=lambda m: log("    " + m))
        check(s2["topup"] >= 1, f"      加量签发 {s2['topup']} 张")
        r3 = _http(f"{base}/api/trial/claim-status?id={ids['real']}")
        v = str(r3.get("topup_voucher") or "")
        check(bool(v), "      客户端取到凭证")
        if v:
            info = verify_topup_voucher(v, pub)
            vp = info.get("payload") or {}
            check(bool(info.get("ok")), f"      凭证验签 ({info.get('error', 'ok')})")
            check(vp.get("chars") == TRIAL_GIFT_CHARS, f"      凭证 {vp.get('chars')} 字符")
            check(vp.get("ref") == topup_ref(ids["real"]), "      ref 为确定性幂等键")

    log("")
    if failures:
        log(f"FAIL {len(failures)} 项：" + "；".join(failures))
        return 1
    log("PASS 全链通过（领取→签发→取件→验签→绑机→客服加量）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="官网试用领取的厂商机履约端")
    ap.add_argument("--site", required=True, help="官网地址，如 https://bd2026.cc")
    ap.add_argument("--key", required=True, help="ADMIN_KEY / TELEGRAM_SETUP_KEY")
    ap.add_argument("--priv", default="", help="厂商 Ed25519 私钥文件（hex）")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出（cron 用）")
    ap.add_argument("--interval", type=int, default=60, help="守护模式轮询间隔秒（默认 60）")
    ap.add_argument("--days", type=int, default=None, help="试用天数（默认 7）")
    ap.add_argument("--chars", type=int, default=None, help="试用字符额度（默认 25000）")
    ap.add_argument("--gift", type=int, default=TRIAL_GIFT_CHARS, help="加客服赠量（默认 10 万）")
    ap.add_argument("--limit", type=int, default=100, help="单轮最多处理条数")
    ap.add_argument("--dry-run", action="store_true", help="只签不回填，用于演练")
    ap.add_argument("--self-test", action="store_true", help="用临时钥匙自测全链（不需真私钥）")
    ap.add_argument("--console-key", default="", help="自测时的 CONSOLE_KEY（跑加量腿）")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test(args.site, args.key, args.console_key)

    if not args.priv:
        print("ERROR: 履约需要 --priv 私钥文件（自测请加 --self-test）", file=sys.stderr)
        return 2
    priv_path = Path(args.priv)
    if not priv_path.is_file():
        print(f"ERROR: 私钥文件不存在: {priv_path}", file=sys.stderr)
        return 2
    priv_hex = priv_path.read_text(encoding="utf-8").strip()
    # 私钥先自检一次。否则贴错格式（PEM/带换行/公钥）会变成「每条 claim 各失败一次」，
    # 守护模式下就是每分钟刷一屏同样的错，真正的原因反而被淹掉。
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    except Exception as e:
        print(f"ERROR: 私钥不可用（需 64 位 hex 的 Ed25519 私钥）: {e}", file=sys.stderr)
        return 2

    def one() -> Dict[str, int]:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] 轮询 {args.site}")
        try:
            s = run_once(args.site, args.key, priv_hex, days=args.days, chars=args.chars,
                         gift=args.gift, limit=args.limit, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ! 本轮失败: {e}", file=sys.stderr)
            return {"issued": 0, "rejected": 0, "topup": 0, "failed": 1, "backlog_min": 0}
        if s["issued"] or s["topup"] or s["rejected"] or s["failed"]:
            print(f"  → 签发 {s['issued']} / 加量 {s['topup']} / 驳回 {s['rejected']} / 失败 {s['failed']}")
        return s

    if args.once:
        s = one()
        return 1 if s["failed"] else 0
    print(f"守护启动：每 {args.interval}s 一轮，Ctrl-C 退出")
    try:
        while True:
            one()
            time.sleep(max(5, args.interval))
    except KeyboardInterrupt:
        print("\n已退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
