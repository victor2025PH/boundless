# -*- coding: utf-8 -*-
"""跨平台档案灰度裁决 CLI（P3 2026-08-18，只读）。

回答运营三问（两周读数后跑）：
1. **坐席在不在用**：档案数 / 导入批次 / 撤销率（撤销高＝误导入信号）；
2. **值不值**：有档案 vs 无档案会话的「出站后有回音率」「近 7 天活跃占比」对比
   ——诚实口径：这是**相关性读数**不是因果（有档案的客户往往本就更被上心），
   判词只在差距显著且样本足时给方向性结论；
3. **AI 吃没吃到**：线上注入命中走 ops 卡（进程口径）；本 CLI 附台账侧
   「可注入面」＝有档案客户当前有多少活跃会话。

用法::

    python tools/origin_review.py [--days 14] [--json] [--data-root PATH]

只读纪律：inbox.db / contacts.db 一律 ``mode=ro`` URI 打开，零写风险；
多实例数据根自动发现（scripts/_data_root 契约），逐根出报告。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import discover_instance_roots, ENGINE_ROOT  # noqa: E402

MIN_COHORT = 10  # 每组会话样本地板：低于此不给方向性判词


def _ro(db: Path) -> Optional[sqlite3.Connection]:
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def ledger_readout(contacts_db: Path) -> Dict[str, Any]:
    """台账侧读数（contact_profiles / contact_memory_imports；表缺席=功能未用）。"""
    out: Dict[str, Any] = {
        "profiles": 0, "profiles_ai_visible": 0, "first_profile_at": "",
        "imports_confirmed": 0, "imports_revoked": 0, "facts_written": 0,
        "revoke_rate": 0.0, "profile_contact_ids": [],
    }
    conn = _ro(contacts_db)
    if conn is None:
        return out
    try:
        try:
            rows = conn.execute(
                "SELECT contact_id, ai_visible, created_at FROM contact_profiles"
            ).fetchall()
        except sqlite3.OperationalError:
            return out  # 表不存在＝该实例未用过本功能
        out["profiles"] = len(rows)
        out["profiles_ai_visible"] = sum(1 for r in rows if r["ai_visible"])
        out["profile_contact_ids"] = [str(r["contact_id"]) for r in rows]
        first = min((int(r["created_at"] or 0) for r in rows), default=0)
        if first:
            out["first_profile_at"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(first))
        try:
            r2 = conn.execute(
                "SELECT"
                " SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) c,"
                " SUM(CASE WHEN status='revoked' THEN 1 ELSE 0 END) r,"
                " SUM(CASE WHEN status='confirmed' THEN facts_written ELSE 0 END) f"
                " FROM contact_memory_imports").fetchone()
            out["imports_confirmed"] = int(r2["c"] or 0)
            out["imports_revoked"] = int(r2["r"] or 0)
            out["facts_written"] = int(r2["f"] or 0)
            total = out["imports_confirmed"] + out["imports_revoked"]
            out["revoke_rate"] = round(out["imports_revoked"] / total, 3) if total else 0.0
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return out


def cohort_readout(
    inbox_db: Path, profile_contact_ids: List[str], *, days: int,
) -> Dict[str, Any]:
    """有档案 vs 无档案会话的对比读数（窗口内有出站的私聊会话）。

    - ``echo_rate``＝出站后有回音率：窗口内 ≥1 出站，且存在**晚于首条出站**的
      入站 → 算「有回音」；
    - ``active_7d``＝近 7 天有入站的会话占比；
    - 只统计有 contact_id 回写的会话（两组口径一致，比较才公平——CI 后缀匹配
      的补充命中面在两组同样缺席，不引入偏差）。
    """
    out: Dict[str, Any] = {
        "window_days": days,
        "with_profile": {"convs": 0, "echoed": 0, "active_7d": 0},
        "without_profile": {"convs": 0, "echoed": 0, "active_7d": 0},
    }
    conn = _ro(inbox_db)
    if conn is None:
        return out
    since = time.time() - days * 86400
    active_since = time.time() - 7 * 86400
    pid_set = set(profile_contact_ids or [])
    try:
        convs = conn.execute(
            "SELECT c.conversation_id, c.contact_id FROM conversations c"
            " WHERE COALESCE(c.contact_id,'') != '' AND COALESCE(c.chat_type,'private')='private'"
            " AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.conversation_id"
            "   AND m.direction='out' AND m.ts >= ?)",
            (since,),
        ).fetchall()
        for c in convs:
            bucket = out["with_profile"] if str(c["contact_id"]) in pid_set \
                else out["without_profile"]
            bucket["convs"] += 1
            cid = c["conversation_id"]
            first_out = conn.execute(
                "SELECT MIN(ts) FROM messages WHERE conversation_id=?"
                " AND direction='out' AND ts >= ?", (cid, since)).fetchone()[0]
            if first_out is not None:
                echoed = conn.execute(
                    "SELECT 1 FROM messages WHERE conversation_id=?"
                    " AND direction='in' AND ts > ? LIMIT 1",
                    (cid, float(first_out))).fetchone()
                if echoed:
                    bucket["echoed"] += 1
            act = conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id=?"
                " AND direction='in' AND ts >= ? LIMIT 1",
                (cid, active_since)).fetchone()
            if act:
                bucket["active_7d"] += 1
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    for k in ("with_profile", "without_profile"):
        b = out[k]
        b["echo_rate"] = round(b["echoed"] / b["convs"], 3) if b["convs"] else None
        b["active_7d_rate"] = round(b["active_7d"] / b["convs"], 3) if b["convs"] else None
    return out


def verdict_lines(ledger: Dict[str, Any], cohort: Dict[str, Any]) -> List[str]:
    """自动判词（诚实：样本不足/零使用如实说，不硬凑结论）。"""
    lines: List[str] = []
    if not ledger["profiles"] and not ledger["imports_confirmed"]:
        lines.append("功能零使用：无档案无导入——先看坐席培训/入口可见性，谈不上价值裁决。")
        return lines
    lines.append(
        f"使用面：档案 {ledger['profiles']}（AI 可见 {ledger['profiles_ai_visible']}）"
        f" · 导入 {ledger['imports_confirmed']} 批 / 事实 {ledger['facts_written']} 条"
        f" · 撤销率 {ledger['revoke_rate']:.0%}")
    if ledger["revoke_rate"] >= 0.3 and ledger["imports_revoked"] >= 3:
        lines.append("⚠ 撤销率偏高（≥30%）：误导入信号——查向导「哪方是客户」引导与解析质量。")
    wp, np_ = cohort["with_profile"], cohort["without_profile"]
    if wp["convs"] < MIN_COHORT or np_["convs"] < MIN_COHORT:
        lines.append(
            f"对比样本不足（有档案 {wp['convs']} / 无档案 {np_['convs']}，地板 {MIN_COHORT}）"
            "——继续攒，不下方向性结论。")
        return lines
    d_echo = (wp["echo_rate"] or 0) - (np_["echo_rate"] or 0)
    d_act = (wp["active_7d_rate"] or 0) - (np_["active_7d_rate"] or 0)
    lines.append(
        f"有回音率：有档案 {wp['echo_rate']:.0%} vs 无档案 {np_['echo_rate']:.0%}"
        f"（Δ{d_echo:+.0%}）；近7天活跃：{wp['active_7d_rate']:.0%} vs"
        f" {np_['active_7d_rate']:.0%}（Δ{d_act:+.0%}）")
    if d_echo >= 0.1:
        lines.append("方向性正向（相关非因果）：可把「补档案」写进坐席 SOP 并考虑默认开。")
    elif d_echo <= -0.1:
        lines.append("⚠ 有档案组反而更低：多半是选择偏差（难聊的客户才被建档），别据此关功能。")
    else:
        lines.append("差距不显著：维持灰度继续观察；优先看撤销率与注入命中（ops 卡）。")
    return lines


def review_root(root: Path, *, days: int) -> Dict[str, Any]:
    cfg_dir = root / "config"
    ledger = ledger_readout(cfg_dir / "contacts.db")
    cohort = cohort_readout(
        cfg_dir / "inbox.db", ledger.pop("profile_contact_ids"), days=days)
    return {
        "root": str(root),
        "ledger": ledger,
        "cohort": cohort,
        "verdict": verdict_lines(ledger, cohort),
    }


def main() -> int:
    # Windows GBK 控制台兜底：输出走 UTF-8（不行就替换），防中文/符号把 CLI 打崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="跨平台档案灰度裁决（只读）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    roots = ([Path(args.data_root)] if args.data_root
             else (discover_instance_roots() or [ENGINE_ROOT]))
    reports = [review_root(r, days=max(1, args.days)) for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for rep in reports:
        print(f"\n=== 跨平台档案裁决 · {rep['root']} · 近 {args.days} 天 ===")
        led, coh = rep["ledger"], rep["cohort"]
        print(f"  台账：档案 {led['profiles']} · 导入 {led['imports_confirmed']} 批"
              f"（撤销 {led['imports_revoked']}）· 事实 {led['facts_written']} 条"
              + (f" · 首档 {led['first_profile_at']}" if led['first_profile_at'] else ""))
        for k, label in (("with_profile", "有档案"), ("without_profile", "无档案")):
            b = coh[k]
            er = f"{b['echo_rate']:.0%}" if b["echo_rate"] is not None else "—"
            ar = f"{b['active_7d_rate']:.0%}" if b["active_7d_rate"] is not None else "—"
            print(f"  {label}会话：{b['convs']} 个 · 有回音率 {er} · 近7天活跃 {ar}")
        for ln in rep["verdict"]:
            print(f"  >> {ln}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
