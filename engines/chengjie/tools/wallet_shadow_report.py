# -*- coding: utf-8 -*-
"""Token 钱包影子读数（实施50 P3，只读）——enforce 切换决策的数据面。

背景：``licensing.token_ledger.enabled`` 已在 zhiliao 灰度（观测记账，enforce 关）。
五个计费动作（ai_reply / pro_translate / deepl_translate / voice_clone / ai_image）
的消费点 2026-08-19 起全部接线；本工具把账本读成「能拍板的读数」：

1. **钱包面**：每钱包 授予/累计支出/余额（``allocate_spend`` 同源核算）、近 N 天
   日燃烧、7 天均燃、按均燃折算的**跑道天数**、enforce 若今天打开会不会降级；
2. **校准面**（chars↔tokens 对照）：字符池（quota_store，每次成功交付都记）与
   Token 账本（只记付费路径）对同一动作的两套读数之比＝**付费路径占比**——
   voice_clone 占比低＝预渲染/edge 免费路径占大头（符合承诺），也可能是接线缺口，
   数字摆出来人再判；
3. **公平使用面**：免费标准翻译日水表 Top。

契约：多实例数据根自动发现（``scripts/_data_root``），逐根出报告；DB 一律
``mode=ro`` URI 打开（对活体生产库零写事务）；进程内影子计数器（出稿 vs 投递
对读）不落库，须看 ``/api/workspace/metrics.token_ledger``——本报告只管持久层。

用法：
    python tools/wallet_shadow_report.py [--days 14] [--json] [--data-root PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.licensing.token_ledger import (  # noqa: E402
    CHARS_PER_TOKEN_LEGACY,
    TOKEN_RATES,
    allocate_spend,
)

try:
    from src.licensing.chatx_fulfillment import RECHARGE_TOKENS_PER_USD
except Exception:  # pragma: no cover - 兜底：官网基准 50U=55万 → 1U=11000
    RECHARGE_TOKENS_PER_USD = 11_000

# ── 纯函数核心（测试面）───────────────────────────────────────────────────────


def day_str(epoch: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


def recent_days(days: int, now: Optional[float] = None) -> List[str]:
    base = now if now is not None else time.time()
    return [day_str(base - i * 86400) for i in range(max(1, days))][::-1]


def summarize_wallet(
    grants: Sequence[Tuple[int, Optional[float]]],
    spend_rows: Sequence[Tuple[str, str, int]],
    *,
    now: Optional[float] = None,
    days: int = 14,
) -> Dict[str, Any]:
    """单钱包读数：余额核算 + 窗口内日燃烧 + 均燃/跑道 + would_degrade。

    spend_rows: [(day, action, tokens), ...]（全历史；窗口切片在本函数内做——
    余额必须按全历史核算，窗口只影响燃烧率）。
    """
    t_now = now if now is not None else time.time()
    total_spend = sum(int(r[2]) for r in spend_rows)
    alloc = allocate_spend(list(grants), total_spend, t_now)

    window = set(recent_days(days, t_now))
    by_day: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    window_total = 0
    for day, action, tokens in spend_rows:
        n = int(tokens or 0)
        by_action[action] = by_action.get(action, 0) + n
        if day in window:
            by_day[day] = by_day.get(day, 0) + n
            window_total += n

    # 7 天均燃：只数窗口尾部 7 天（含零日——「没用」也是信息，均值才不虚高）
    tail7 = recent_days(7, t_now)
    burn7 = sum(by_day.get(d, 0) for d in tail7) / 7.0
    balance = int(alloc["balance"])
    runway_days: Optional[float] = None
    if burn7 > 0:
        runway_days = round(balance / burn7, 1)
    return {
        "balance": balance,
        "active_granted": int(alloc["active_granted"]),
        "expired_lost": int(alloc["expired_lost"]),
        "spend_unmet": int(alloc["spend_unmet"]),
        "total_spend": total_spend,
        "window_spend": window_total,
        "by_day": by_day,
        "by_action_alltime": by_action,
        "burn7_avg": round(burn7, 1),
        "runway_days": runway_days,
        "would_degrade_now": balance <= 0 and total_spend > 0,
        "usd_window": round(window_total / float(RECHARGE_TOKENS_PER_USD), 2),
    }


def billed_share(
    char_rows: Sequence[Tuple[str, str, int]],
    spend_rows: Sequence[Tuple[str, str, int]],
    *,
    days: int = 14,
    now: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """校准面：同窗口内 字符池读数 vs Token 账本读数 的付费路径占比。

    - tts：quota 每次成功合成都记（含 edge/预渲染复制路径外的现场合成）；token 只记
      克隆引擎 → share = voice_clone_tokens / (tts_chars/100*10)。低＝免费路径占大头。
    - translation：quota 全档都记；token 只记 pro/certified →
      share = (pro+deepl tokens) / (translation_chars 折 pro 价)。低＝标准档（免费）为主。
    份额 >1 说明两套计量的口径漂移（如 token 侧记了 quota 侧没记的路径），如实报。
    """
    window = set(recent_days(days, now))
    chars: Dict[str, int] = {}
    for day, category, n in char_rows:
        if day in window:
            chars[category] = chars.get(category, 0) + int(n or 0)
    tokens: Dict[str, int] = {}
    for day, action, n in spend_rows:
        if day in window:
            tokens[action] = tokens.get(action, 0) + int(n or 0)

    out: Dict[str, Dict[str, Any]] = {}
    vc_rate = TOKEN_RATES["voice_clone"]  # 10 / 100 chars
    tts_chars = chars.get("tts", 0)
    tts_expected = (tts_chars / float(vc_rate["unit_size"])) * vc_rate["tokens"]
    actual_vc = tokens.get("voice_clone", 0)
    out["tts"] = {
        "chars": tts_chars,
        "tokens_if_all_billed": int(tts_expected),
        "tokens_actual": actual_vc,
        "billed_share": round(actual_vc / tts_expected, 3) if tts_expected > 0 else None,
    }
    pt_rate = TOKEN_RATES["pro_translate"]  # 10 / 1000 chars
    tr_chars = chars.get("translation", 0)
    tr_expected = (tr_chars / float(pt_rate["unit_size"])) * pt_rate["tokens"]
    actual_tr = tokens.get("pro_translate", 0) + tokens.get("deepl_translate", 0)
    out["translation"] = {
        "chars": tr_chars,
        "tokens_if_all_billed": int(tr_expected),
        "tokens_actual": actual_tr,
        "billed_share": round(actual_tr / tr_expected, 3) if tr_expected > 0 else None,
    }
    return out


# ── IO（只读）────────────────────────────────────────────────────────────────


def _ro(db: Path) -> Optional[sqlite3.Connection]:
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def collect_root(root: Path, *, days: int = 14, now: Optional[float] = None) -> Dict[str, Any]:
    """单数据根全量读数（DB 缺席软失败＝各段空）。"""
    out: Dict[str, Any] = {
        "root": str(root), "wallets": {}, "calibration": {}, "fair_use": [],
        "ledger_db": False, "quota_db": False,
    }
    spend_all: List[Tuple[str, str, int]] = []
    tl = _ro(Path(root) / "config" / "token_ledger.db")
    if tl is not None:
        out["ledger_db"] = True
        try:
            wallet_ids = {
                str(r["lic_id"]) for r in tl.execute(
                    "SELECT DISTINCT lic_id FROM token_spend "
                    "UNION SELECT DISTINCT lic_id FROM token_grants")
            }
            for wid in sorted(wallet_ids):
                grants = [
                    (int(r["tokens"]), r["expires_at"]) for r in tl.execute(
                        "SELECT tokens, expires_at FROM token_grants WHERE lic_id=?", (wid,))
                ]
                rows = [
                    (str(r["day"]), str(r["action"]), int(r["tokens"])) for r in tl.execute(
                        "SELECT day, action, tokens FROM token_spend WHERE lic_id=?", (wid,))
                ]
                spend_all.extend(rows)
                out["wallets"][wid] = summarize_wallet(
                    grants, rows, now=now, days=days)
            out["fair_use"] = [
                {"wallet": str(r["wallet"]), "day": str(r["day"]), "chars": int(r["chars"])}
                for r in tl.execute(
                    "SELECT wallet, day, chars FROM fair_use_chars "
                    "ORDER BY day DESC, chars DESC LIMIT 14")
            ]
        finally:
            tl.close()

    char_rows: List[Tuple[str, str, int]] = []
    lq = _ro(Path(root) / "config" / "license_quota.db")
    if lq is not None:
        out["quota_db"] = True
        try:
            char_rows = [
                (str(r["day"]), str(r["category"]), int(r["chars"])) for r in lq.execute(
                    "SELECT day, category, chars FROM license_char_usage")
            ]
        finally:
            lq.close()
    out["calibration"] = billed_share(char_rows, spend_all, days=days, now=now)
    return out


# ── 渲染 ─────────────────────────────────────────────────────────────────────


def render_text(report: Dict[str, Any], days: int) -> str:
    lines: List[str] = []
    p = lines.append
    p(f"== Token 钱包影子读数 · {report['root']} · 窗口 {days} 天 ==")
    if not report["ledger_db"]:
        p("  (无 token_ledger.db —— 账本未启用或该根无流量)")
        return "\n".join(lines)
    for wid, w in report["wallets"].items():
        p(f"[钱包] {wid}")
        p(f"  余额 {w['balance']} / 有效授予 {w['active_granted']}"
          f" / 累计支出 {w['total_spend']} (过期作废 {w['expired_lost']}, 未覆盖 {w['spend_unmet']})")
        p(f"  窗口支出 {w['window_spend']} Token (≈${w['usd_window']})"
          f" · 7天均燃 {w['burn7_avg']}/天"
          + (f" · 跑道 ≈{w['runway_days']} 天" if w["runway_days"] is not None else "")
          + (" · ⚠ enforce 开=立即降级" if w["would_degrade_now"] else ""))
        acts = ", ".join(f"{k}={v}" for k, v in sorted(w["by_action_alltime"].items()))
        p(f"  动作累计: {acts or '(无)'}")
        recent = sorted(w["by_day"].items())[-7:]
        if recent:
            p("  近日: " + ", ".join(f"{d[5:]}={n}" for d, n in recent))
    cal = report.get("calibration") or {}
    if cal:
        p("[校准] 付费路径占比（字符池 vs Token 账本，同窗口）")
        for k, c in cal.items():
            share = c.get("billed_share")
            share_s = f"{share:.1%}" if isinstance(share, float) else "n/a(零流量)"
            p(f"  {k}: chars={c['chars']} → 全计费应为 {c['tokens_if_all_billed']} Token,"
              f" 实记 {c['tokens_actual']} → 占比 {share_s}")
    fu = report.get("fair_use") or []
    if fu:
        p("[公平使用] 免费标准翻译水表（最近行）")
        for r in fu[:6]:
            p(f"  {r['day']} {r['wallet'][:36]}: {r['chars']} chars")
    return "\n".join(lines)


def trend_line(report: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """周批趋势行（紧凑口径：每根一行，只留决策字段——完整明细看 --json）。"""
    t = now if now is not None else time.time()
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
        "root": report["root"],
        "wallets": {
            wid: {
                "balance": w["balance"],
                "window_spend": w["window_spend"],
                "burn7_avg": w["burn7_avg"],
                "runway_days": w["runway_days"],
                "would_degrade_now": w["would_degrade_now"],
            }
            for wid, w in (report.get("wallets") or {}).items()
        },
        "calibration": report.get("calibration") or {},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Token 钱包影子读数（只读）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--out-jsonl", default="",
                    help="追加紧凑趋势行到该文件（周批用；不影响 stdout 输出）")
    args = ap.parse_args(argv)

    reports = [collect_root(r, days=args.days) for r in resolve_data_roots(args.data_root)]
    if args.out_jsonl:
        out = Path(args.out_jsonl)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            for rep in reports:
                fh.write(json.dumps(trend_line(rep), ensure_ascii=False) + "\n")
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print(render_text(rep, args.days))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
