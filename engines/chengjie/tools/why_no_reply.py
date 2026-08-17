# -*- coding: utf-8 -*-
"""「为什么没自动回」排障 CLI（P0 2026-08-07，.104/.198 双向全自动排障沉淀）。

一条命令回答：这个会话此刻的**有效档位**是什么、被哪一层闸住了、下一条入站
会不会被自动回复。把「翻代码逐层猜」固化成一分钟读数——覆盖本次排障踩过的
全部闸门：

- 会话显式档位（conversation_settings，谁写的看 updated_at）
- 档位封顶：冷启动预热（新号 72h）/ 平台 platform_modes / 账号业务线
  （effective_automation —— 与 A/B 两线护栏**同一实现**求值）
- peer_bot_guard：Tier0 平台真值 / 复读 / 秒回同文 / 启发式疑似 / 每日预算
  （直接调 ``evaluate`` 纯函数，给出「下一条入站会怎样」）
- 账号：注册表 status / 接入年龄 / business_line / meta.auto_reply
- 部署形态：companion_runtime（A 线直发）vs System-Z（拟稿+autosend），
  l2_autosend.enabled / deliver，work_schedule 班表
- 近期草稿：pending 人审稿积压即「AI 在拟稿、没人点发送」

**零凭据、只读**：SQLite 一律 ``mode=ro`` URI；不 import InboxStore（其构造会跑
migration = 写库）；注册表 created_at / business_line 从 ro 连接自取后**显式注入**
resolver，不触发任何进程外写路径。数据根走 ``scripts/_data_root`` 契约。

用法::

    python tools/why_no_reply.py --platform telegram --account 8755679833 --chat 8041810715
    python tools/why_no_reply.py ... --data-root D:\\chengjie-instances\\zhiliao\\data
    python tools/why_no_reply.py ... --json

退出码：发现任何「会拦住自动回复」的红灯 → 1；链路看起来通 → 0。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402


def _ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


def _row(con, sql, args=()) -> Optional[Dict[str, Any]]:
    try:
        r = con.execute(sql, args).fetchone()
        return dict(r) if r is not None else None
    except Exception:
        return None


def _rows(con, sql, args=()) -> List[Dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except Exception:
        return []


class _RoModeStore:
    """effective_automation 需要的最小 store 面（只读，无 migration）。"""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def get_automation_mode_if_set(self, conversation_id: str):
        r = _row(self._con,
                 "SELECT automation_mode FROM conversation_settings "
                 "WHERE conversation_id=?", (conversation_id,))
        return r["automation_mode"] if r else None


def _fmt_ts(ts: Any) -> str:
    try:
        t = float(ts or 0)
        return time.strftime("%m-%d %H:%M", time.localtime(t)) if t > 0 else "-"
    except Exception:
        return "-"


def diagnose(root: Path, platform: str, account_id: str,
             chat_key: str) -> Dict[str, Any]:
    """单数据根诊断（纯读）。返回机读结构；verdicts 里 level ∈ ok/warn/block。"""
    now = time.time()
    cfg = load_merged_config(root) or {}
    cfg_dir = root / "config"
    out: Dict[str, Any] = {
        "root": str(root), "platform": platform,
        "account_id": account_id, "chat_key": chat_key,
    }
    verdicts: List[Dict[str, str]] = []

    def _v(level: str, code: str, msg: str) -> None:
        verdicts.append({"level": level, "code": code, "msg": msg})

    # ── 账号（注册表 ro）────────────────────────────────────────────────
    acct: Dict[str, Any] = {}
    reg = _ro(cfg_dir / "account_registry.db")
    created_at = 0.0
    business_line = ""
    if reg is not None:
        r = _row(reg, "SELECT * FROM platform_accounts WHERE platform=? AND "
                      "account_id=?", (platform, account_id))
        if r:
            meta = {}
            try:
                meta = json.loads(r.get("meta_json") or "{}")
            except Exception:
                meta = {}
            created_at = float(r.get("created_at") or 0.0)
            business_line = str(r.get("business_line")
                                or meta.get("business_line") or "")
            acct = {
                "status": r.get("status"), "mode": r.get("mode"),
                "created_at": created_at,
                "age_h": round((now - created_at) / 3600.0, 1)
                         if created_at > 0 else None,
                "business_line": business_line or None,
                "meta_auto_reply": meta.get("auto_reply"),
                "persona": meta.get("persona_id") or meta.get("persona_ids"),
            }
            if str(r.get("status") or "") not in ("online", ""):
                _v("block", "account_status",
                   f"账号状态={r.get('status')}（非 online：worker 不在跑，"
                   f"收发都停）")
        else:
            _v("warn", "account_missing",
               "注册表无此账号行（主协议号/纯 RPA 会话属正常；编排器账号缺行"
               "＝没登记过）")
        reg.close()
    out["account"] = acct

    # ── 会话（inbox ro）────────────────────────────────────────────────
    cid = f"{platform}:{account_id}:{chat_key}"
    out["conversation_id"] = cid
    ib = _ro(cfg_dir / "inbox.db")
    conv: Dict[str, Any] = {}
    explicit_mode: Optional[str] = None
    mode_set_at = 0.0
    recent_msgs: List[Dict[str, Any]] = []
    ledger: Dict[str, Any] = {}
    drafts: List[Dict[str, Any]] = []
    if ib is None:
        _v("warn", "no_inbox_db", "找不到 inbox.db（数据根不对？）")
    else:
        conv = _row(ib, "SELECT * FROM conversations WHERE conversation_id=?",
                    (cid,)) or {}
        if not conv:
            _v("warn", "conv_missing",
               "会话行不存在——该账号从未收到过这个对方的消息（先确认消息真的"
               "进来了/账号在线）")
        ms = _row(ib, "SELECT automation_mode, updated_at FROM "
                      "conversation_settings WHERE conversation_id=?", (cid,))
        if ms:
            explicit_mode = str(ms.get("automation_mode") or "")
            mode_set_at = float(ms.get("updated_at") or 0.0)
        recent_msgs = _rows(
            ib, "SELECT direction, ts, COALESCE(text,'') AS text FROM messages "
                "WHERE conversation_id=? ORDER BY ts DESC LIMIT 120", (cid,))
        recent_msgs.reverse()  # 升序（守卫纯函数契约）
        ledger = _row(ib, "SELECT * FROM peer_reply_ledger WHERE "
                          "conversation_id=?", (cid,)) or {}
        drafts = _rows(
            ib, "SELECT status, autopilot_level, created_at, decided_by FROM "
                "reply_drafts WHERE conversation_id=? "
                "ORDER BY created_at DESC LIMIT 8", (cid,))
    out["conversation"] = {
        "exists": bool(conv),
        "display_name": conv.get("display_name"),
        "username": conv.get("username"),
        "chat_type": conv.get("chat_type"),
        "peer_is_bot": conv.get("peer_is_bot"),
        "bot_score": conv.get("bot_score"),
        "bot_evidence": conv.get("bot_evidence"),
        "explicit_mode": explicit_mode,
        "mode_set_at": _fmt_ts(mode_set_at),
    }

    # ── 有效档位（与 A/B 两线同一实现）────────────────────────────────
    eff: Dict[str, Any] = {}
    try:
        from src.inbox.effective_automation import effective_automation
        store = _RoModeStore(ib) if ib is not None else None
        eff = effective_automation(
            store, cfg, conversation_id=cid, platform=platform,
            account_id=account_id, business_line=business_line or None,
            connected_at=created_at)
    except Exception as exc:  # pragma: no cover - 排障工具自身别崩
        eff = {"error": str(exc)}
    out["effective"] = eff
    base_mode = str(eff.get("mode") or "")
    eff_mode = str(eff.get("effective_mode") or base_mode)
    if base_mode in ("manual", "review", "multi_choice"):
        hint = ""
        if str(conv.get("bot_evidence") or ""):
            hint = f"；bot 证据：{conv.get('bot_evidence')}"
        elif int(conv.get("peer_is_bot") or 0) == 1:
            hint = "；会话被标记为机器人对方"
        _v("block", "base_mode",
           f"会话基础档位={base_mode}（{_fmt_ts(mode_set_at)} 写入{hint}）"
           f"——切回全自动才会自动发；若是守卫降档，切回后触发条件仍在会再降")
    for cap in (eff.get("caps") or []):
        layer = cap.get("layer")
        if layer == "warmup":
            left_h = max(0.0, (float(cap.get("until_ts") or 0) - now) / 3600.0)
            _v("block", "cap_warmup",
               f"冷启动预热封顶：账号接入 {acct.get('age_h')}h，预热窗还剩 "
               f"{left_h:.1f}h → 实际按 {cap.get('ceiling')} 执行（AI 拟稿、"
               f"人审后发）。立即解除：companion.proactive_topic.cold_start."
               f"warmup_review: false")
        elif layer == "business_line":
            _v("block", "cap_business_line",
               f"业务线封顶：账号业务线={cap.get('detail')} → 实际按 "
               f"{cap.get('ceiling')} 执行。解除：调整 inbox.auto_draft."
               f"business_line_modes 或摘掉账号业务线标签")
        elif layer == "platform":
            _v("block", "cap_platform",
               f"平台封顶：platform_modes[{cap.get('detail')}] → 实际按 "
               f"{cap.get('ceiling')} 执行")

    # ── peer_bot_guard：下一条入站会怎样（纯函数复演）──────────────────
    guard: Dict[str, Any] = {}
    try:
        from src.inbox import peer_bot_guard as pbg
        g_cfg = pbg.parse_cfg(cfg)
        guard["enabled"] = bool(g_cfg.get("enabled"))
        if g_cfg.get("enabled") and conv:
            day = pbg.today_key(now)
            auto_today = (int(ledger.get("auto_replies") or 0)
                          if str(ledger.get("day") or "") == day else 0)
            relieved = str(ledger.get("relief_day") or "") == day
            v = pbg.evaluate(
                recent_msgs, g_cfg, platform=platform,
                username=str(conv.get("username") or ""),
                display_name=str(conv.get("display_name") or ""),
                chat_type=str(conv.get("chat_type") or ""),
                peer_override=int(conv.get("peer_is_bot") or 0),
                now=now, auto_out_today=auto_today, budget_relieved=relieved)
            guard.update({
                "next_inbound_blocked": v.blocked, "reason": v.reason,
                "evidence": v.evidence, "downgrade_to": v.downgrade_to,
                "budget_used_today": auto_today,
                "budget_limit": int(g_cfg.get("daily_reply_budget") or 0),
                "budget_relieved": relieved,
                "repeat_streak": pbg.inbound_repeat_streak(recent_msgs),
                "instant_echo": pbg.instant_echo_count(
                    recent_msgs,
                    window_sec=float(g_cfg.get("instant_reply_sec") or 0)),
            })
            if v.blocked:
                _v("block", f"guard_{v.reason}",
                   f"peer_bot_guard 将拦下一条入站：reason={v.reason} "
                   f"({v.evidence})"
                   + (f"，并把会话降为 {v.downgrade_to}" if v.downgrade_to
                      else "")
                   + ("；预算可用收件箱横幅「今日继续自动回复」救济"
                      if v.reason == "daily_budget" else ""))
    except Exception as exc:
        guard["error"] = str(exc)
    out["guard"] = guard

    # ── 部署形态 / 投递开关 ────────────────────────────────────────────
    tg_login = ((cfg.get("platform_login") or {}).get("telegram") or {})
    companion = bool(tg_login.get("companion_runtime", False))
    l2 = ((cfg.get("inbox") or {}).get("l2_autosend") or {})
    l2_enabled = bool(l2.get("enabled", True))
    deliver = bool(l2.get("deliver", False))
    out["deploy"] = {
        "companion_runtime": companion,
        "l2_autosend_enabled": l2_enabled,
        "deliver": deliver,
        "global_automation_mode": str(((cfg.get("inbox") or {})
                                       .get("auto_draft") or {})
                                      .get("automation_mode") or "auto_ai"),
    }
    if platform == "telegram" and not companion:
        if not deliver:
            _v("block", "deliver_off",
               "System-Z 架构（companion_runtime=false）且 inbox.l2_autosend."
               "deliver=false：L2 草稿只在库里标记、**不会真发**——出厂示例"
               "默认就是这个档，overlay 置 deliver: true 才有全自动")
        if not l2_enabled:
            _v("block", "l2_off",
               "inbox.l2_autosend.enabled=false：无自动投递 worker")
    try:
        from src.inbox.work_hours_gate import (
            should_hold_auto_reply,
            work_schedule_cfg,
        )
        hold = should_hold_auto_reply(
            work_schedule_cfg(cfg), platform, account_id, peer_text="")
        if hold:
            _v("block", "work_schedule",
               f"工作班表扣留自动回复（{hold}）：复班后投递/补拟")
    except Exception:
        pass

    # ── 草稿积压（AI 在拟稿、没人点发送 = 最常见的「看起来没回」）──────
    pend = [d for d in drafts if str(d.get("status")) == "pending"]
    out["drafts"] = [
        {"status": d.get("status"), "level": d.get("autopilot_level"),
         "created": _fmt_ts(d.get("created_at")),
         "decided_by": d.get("decided_by")} for d in drafts]
    if pend:
        oldest = min(float(d.get("created_at") or now) for d in pend)
        _v("warn", "pending_drafts",
           f"有 {len(pend)} 条待审草稿（最老 {(now - oldest) / 3600.0:.1f}h）"
           f"——AI 一直在拟稿，只是没人在待审队列点「发送」")

    if not any(v["level"] == "block" for v in verdicts):
        _v("ok", "looks_alive",
           f"未发现拦截：有效档位={eff_mode}"
           + ("（A 线直发）" if companion and platform == "telegram"
              else "（拟稿 → AutosendWorker 自动投递）")
           + "。若仍不回，查实例日志：A线让位 / peer_bot_guard / "
             "[AutoDraft]，以及 /api/drafts/autosend-status 的 skip 计数")
    if ib is not None:
        ib.close()
    out["verdicts"] = verdicts
    return out


def _print_human(d: Dict[str, Any]) -> None:
    icon = {"ok": "✅", "warn": "⚠️ ", "block": "⛔"}
    print("=" * 72)
    print(f"root      : {d['root']}")
    print(f"conv      : {d['conversation_id']}")
    a = d.get("account") or {}
    if a:
        print(f"account   : status={a.get('status')} age_h={a.get('age_h')} "
              f"business_line={a.get('business_line')} "
              f"persona={a.get('persona')}")
    c = d.get("conversation") or {}
    print(f"peer      : {c.get('display_name')!r} @{c.get('username')} "
          f"bot={c.get('peer_is_bot')} score={c.get('bot_score')}")
    print(f"mode      : explicit={c.get('explicit_mode') or '-'} "
          f"(set {c.get('mode_set_at')})")
    e = d.get("effective") or {}
    print(f"effective : base={e.get('mode')}({e.get('source')}) → "
          f"{e.get('effective_mode')}  caps={e.get('caps') or []}")
    g = d.get("guard") or {}
    if g.get("enabled"):
        print(f"guard     : next_blocked={g.get('next_inbound_blocked')} "
              f"reason={g.get('reason') or '-'} "
              f"budget={g.get('budget_used_today')}/{g.get('budget_limit')} "
              f"repeat={g.get('repeat_streak')} echo={g.get('instant_echo')}")
    dep = d.get("deploy") or {}
    print(f"deploy    : companion={dep.get('companion_runtime')} "
          f"l2={dep.get('l2_autosend_enabled')} "
          f"deliver={dep.get('deliver')} "
          f"global_mode={dep.get('global_automation_mode')}")
    if d.get("drafts"):
        print(f"drafts    : {d['drafts'][:4]}")
    print("-" * 72)
    for v in d.get("verdicts") or []:
        print(f" {icon.get(v['level'], '  ')} [{v['code']}] {v['msg']}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="why_no_reply：会话自动回复排障")
    ap.add_argument("--platform", default="telegram")
    ap.add_argument("--account", required=True, help="本方账号 account_id")
    ap.add_argument("--chat", required=True, help="对方 chat_key")
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    roots = resolve_data_roots(args.data_root)
    results = [diagnose(r, str(args.platform).lower(), str(args.account),
                        str(args.chat)) for r in roots]
    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for d in results:
            _print_human(d)
    blocked = any(v.get("level") == "block"
                  for d in results for v in (d.get("verdicts") or []))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
