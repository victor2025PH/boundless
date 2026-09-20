#!/usr/bin/env python3
"""邀请裂变 + 免费额度升级 端到端冒烟（对着**本地官网 dev server**，零生产写入）。

2026-08-11 免费额度升级（100 万/无期限 + 邀请裂变）的验收工具。覆盖：
A 领取 → 签发（新规格 100 万/无期限）→ 取邀请码 → B 带码领取（归因）→
B 签发 + 见面礼凭证 → B 水位达标 → 邀请人奖励凭证 → 聚合读数 →
旧规格存量单自动升级重签（幂等）→ 防刷（无效码 / 同 IP 聚集 flagged / 人审放行）。

⚠ 只对本地 dev server 跑（用一次性临时钥匙签发；对生产站跑会把测试单写进真台账）。
**刻意不进计划任务/gate_sweep**（要起 Node dev server，属显式验收动作）——与
smoke_voice_reuse 同决策：被动观测走 ops「🎁 邀请裂变」卡。首跑 2026-08-11 全绿。

用法：
  # ① 起官网 dev server（隔离数据目录 + 测试密钥；在 website/ 下）：
  #   $env:LEADS_DIR='<tmp>'; $env:ANALYTICS_DIR='<tmp2>'
  #   $env:ADMIN_KEY='ci-admin-key'; $env:CONSOLE_KEY='ci-console-key'
  #   npx next dev -p 3571
  # ② 跑冒烟（引擎根下）：
  python tools/smoke_referral_chain.py --site http://127.0.0.1:3571 \
      --key ci-admin-key --console-key ci-console-key
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from src.licensing.chatx_fulfillment import (  # noqa: E402
    REFERRAL_INVITEE_CHARS,
    REFERRAL_INVITER_CHARS,
    TRIAL_SPEC,
)
from src.licensing.license_manager import generate_keypair  # noqa: E402
from src.licensing.topup_voucher import verify_topup_voucher  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "_ft", Path(__file__).resolve().parent.parent / "scripts" / "fulfill_trial.py")
ft = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
_SPEC.loader.exec_module(ft)  # type: ignore[union-attr]

FAILURES: list = []


def check(cond, name):
    print(("  ok " if cond else "  FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def http(url, method="GET", body=None, key="", console_key=""):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    if key:
        req.add_header("x-setup-key", key)
    if console_key:
        req.add_header("x-console-key", console_key)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return {"ok": False, "error": f"http_{e.code}"}


def rand_fp():
    return "-".join(uuid.uuid4().hex[:16].upper()[i:i + 4] for i in range(0, 16, 4))


def decode_payload(token):
    b64 = token.split(".", 1)[0]
    pad = b64.replace("-", "+").replace("_", "/")
    pad += "=" * ((4 - len(pad) % 4) % 4)
    return json.loads(base64.b64decode(pad).decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="http://127.0.0.1:3571")
    ap.add_argument("--key", default="ci-admin-key")
    ap.add_argument("--console-key", default="ci-console-key")
    args = ap.parse_args()
    site, key = args.site.rstrip("/"), args.key
    stamp = int(time.time())
    kp = generate_keypair()
    quiet = lambda m: None  # noqa: E731

    print("[1] 邀请人 A 领取 + 签发（新规格）")
    fpA = rand_fp()
    rA = http(f"{site}/api/trial/claim", "POST",
              {"fingerprint": fpA, "contact": f"@e2eA{stamp}"})
    check(rA.get("ok") and rA.get("claim_id"), "A 建单")
    idA = rA["claim_id"]
    s1 = ft.run_once(site, key, kp["private_hex"], log=quiet)
    check(s1["issued"] >= 1, f"A 签发（issued={s1['issued']}）")
    stA = http(f"{site}/api/trial/claim-status?id={idA}&fingerprint={fpA}")
    lic = decode_payload(stA.get("license") or ".")
    check(lic.get("included_chars") == TRIAL_SPEC["included_chars"],
          f"A 授权额度={lic.get('included_chars')}（规格 {TRIAL_SPEC['included_chars']}）")
    check("exp" not in lic, "A 授权无期限（不写 exp）")

    print("[2] A 的邀请码 + 分享链接")
    inv = http(f"{site}/api/trial/invite-info?claim_id={idA}&fingerprint={fpA}")
    check(inv.get("ok") and str(inv.get("code") or "").startswith("ZL-"), f"邀请码 {inv.get('code')}")
    code = str(inv.get("code") or "")
    check(code in str(inv.get("share_url") or ""), "分享链接带码")
    inv2 = http(f"{site}/api/trial/invite-info?claim_id={idA}&fingerprint={fpA}")
    check(inv2.get("code") == code, "邀请码幂等（一 claim 一码）")
    bad = http(f"{site}/api/trial/invite-info?claim_id={idA}&fingerprint={rand_fp()}")
    check(bad.get("ok") is False, "错指纹取不到邀请码（交叉校验）")

    print("[3] 被邀请人 B 带码领取 → 签发 + 见面礼")
    fpB = rand_fp()
    rB = http(f"{site}/api/trial/claim", "POST",
              {"fingerprint": fpB, "contact": f"@e2eB{stamp}", "invite_code": code})
    check(rB.get("ok") and rB.get("referral") == "ok", f"B 归因（referral={rB.get('referral')}）")
    idB = rB["claim_id"]
    s2 = ft.run_once(site, key, kp["private_hex"], log=quiet)
    check(s2["issued"] >= 1 and s2["referral"] >= 1,
          f"B 签发 + 见面礼（issued={s2['issued']} referral={s2['referral']}）")
    stB = http(f"{site}/api/trial/claim-status?id={idB}&fingerprint={fpB}")
    evB = stB.get("extra_vouchers") or []
    check(len(evB) == 1, f"B 恰好一张追加凭证（{len(evB)}）")
    wel = evB[0] if evB else {}
    check(str(wel.get("ref") or "").startswith("refwel-"), f"见面礼 ref={wel.get('ref')}")
    v = verify_topup_voucher(str(wel.get("voucher") or ""), kp["public_hex"])
    check(v.get("ok") and v["payload"]["chars"] == REFERRAL_INVITEE_CHARS,
          f"见面礼验签 + 额度 {v.get('payload', {}).get('chars')}")
    fpB8 = fpB.replace("-", "")[:8]
    check(v.get("ok") and v["payload"].get("lic") == f"trial-{fpB8}", "见面礼绑 B 的 lic_id")

    print("[4] B 水位达标 → 邀请人奖励")
    ub = http(f"{site}/api/trial/usage-beacon", "POST",
              {"claim_id": idB, "fingerprint": fpB, "used_chars": 10_000})
    check(ub.get("ok") is True, "水位上报")
    s3 = ft.run_once(site, key, kp["private_hex"], log=quiet)
    check(s3["referral"] >= 1, f"邀请人奖励签发（referral={s3['referral']}）")
    stA2 = http(f"{site}/api/trial/claim-status?id={idA}&fingerprint={fpA}")
    evA = stA2.get("extra_vouchers") or []
    earn = next((x for x in evA if str(x.get("ref") or "").startswith("refearn-")), {})
    check(bool(earn), f"A 收到奖励凭证 ref={earn.get('ref')}")
    v2 = verify_topup_voucher(str(earn.get("voucher") or ""), kp["public_hex"])
    check(v2.get("ok") and v2["payload"]["chars"] == REFERRAL_INVITER_CHARS, "奖励验签 + 额度")
    s3b = ft.run_once(site, key, kp["private_hex"], log=quiet)
    check(s3b["referral"] == 0, "发奖幂等（重跑零新签）")

    print("[5] 聚合读数")
    agg = http(f"{site}/api/trial/referral-stats")
    check(agg.get("ok") and agg.get("registered", 0) >= 1 and agg.get("qualified", 0) >= 1
          and agg.get("invitee_rewarded", 0) >= 1 and agg.get("inviter_rewarded", 0) >= 1,
          f"聚合 {json.dumps({k: agg.get(k) for k in ('codes','registered','qualified','invitee_rewarded','inviter_rewarded','chars_granted')}, ensure_ascii=False)}")

    print("[6] 旧规格存量单自动升级重签")
    fpE = rand_fp()
    rE = http(f"{site}/api/trial/claim", "POST",
              {"fingerprint": fpE, "contact": f"@e2eE{stamp}"})
    idE = rE["claim_id"]
    ft.run_once(site, key, kp["private_hex"], days=7, chars=25_000, log=quiet)  # 按旧规格签
    stE = http(f"{site}/api/trial/claim-status?id={idE}&fingerprint={fpE}")
    licE = decode_payload(stE.get("license") or ".")
    check(licE.get("included_chars") == 25_000 and "exp" in licE, "旧规格单已就位（25k + exp）")
    s4 = ft.run_once(site, key, kp["private_hex"], log=quiet)  # 默认规格 → 升级腿应重签
    check(s4["upgraded"] >= 1, f"升级重签（upgraded={s4['upgraded']}）")
    stE2 = http(f"{site}/api/trial/claim-status?id={idE}&fingerprint={fpE}")
    licE2 = decode_payload(stE2.get("license") or ".")
    check(licE2.get("included_chars") == TRIAL_SPEC["included_chars"] and "exp" not in licE2,
          f"升级后额度={licE2.get('included_chars')} 无期限")
    check(licE2.get("lic_id") == licE.get("lic_id"), "lic_id 不变（用量累计不清零）")
    s4b = ft.run_once(site, key, kp["private_hex"], log=quiet)
    check(s4b["upgraded"] == 0, "升级幂等（重跑零重签）")

    print("[7] 防刷：无效码 / 同 IP 聚集 flagged / 人审放行 / 累计上限")
    rX = http(f"{site}/api/trial/claim", "POST",
              {"fingerprint": rand_fp(), "contact": f"@e2eX{stamp}", "invite_code": "ZL-999999"})
    check(rX.get("referral") == "invalid_code", f"无效码拒绝（{rX.get('referral')}）")
    # B 是第 1 个同 IP 被邀请人；再来 2 个 → 第 3 个起触发 ip_cluster flagged
    flagged_id = ""
    for i in range(2, 4):
        r = http(f"{site}/api/trial/claim", "POST",
                 {"fingerprint": rand_fp(), "contact": f"@e2eB{i}{stamp}", "invite_code": code})
        if i == 3:
            check(r.get("referral") == "ok", "第 3 个归因仍收下（进 flagged 待人审）")
    admin = http(f"{site}/api/admin/referrals?status=flagged", key=key)
    rows = admin.get("referrals") or []
    check(len(rows) >= 1, f"flagged 台账可见（{len(rows)} 条）")
    if rows:
        flagged_id = rows[0]["id"]
        ap_res = http(f"{site}/api/console/referral-approve", "POST",
                      {"id": flagged_id}, console_key=args.console_key)
        check(ap_res.get("ok") and ap_res.get("status") in ("registered", "qualified"),
              f"人审放行（status={ap_res.get('status')}）")
    dueq = http(f"{site}/api/admin/referrals?due=1", key=key)
    check(dueq.get("ok") is True, "due 队列可读")

    print("")
    if FAILURES:
        print(f"FAIL {len(FAILURES)} 项：" + "；".join(FAILURES))
        return 1
    print("PASS 邀请裂变 + 免费额度升级全链端到端通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
