# -*- coding: utf-8 -*-
"""实施72 全链只读验收（一条命令复验「账号身份错乱」修复面的在线状态）。

用法::

    python tools/verify_impl72.py [--data-root D:\\chengjie-instances\\zhiliao\\data] [--json]

检查面（全部只读，SQLite 一律 ro URI，HTTP 仅 GET/只读端点）：
  1. 配置：platform_modes 封顶状态 / proactive 平台白名单 / own_fleet 登记表 /
     reconnect_backlog 解析；
  2. 注册表：身份隔离清单（pending 应为空=全部已转正）、Calixa 归档态、
     Wisley 转正痕迹（identity_confirmed_*）；
  3. inbox 库：approx_ts / fail_reason 列就位、补收打标行数、失败留痕带因率；
  4. 封顶层现算：外机自有号对端（Calixa 线程）应命中 own_fleet_peer；
  4b. 疑似自有号未登记候选现算（下一阶段 P1）：对端命中 removed 归档账号却没
     登记 own_fleet.extra → warn 提示；全覆盖=pass；
  5. 进程装载水位：晚于实例启动的待装载 .py 清单（=下个重启窗要捎带的债）；
  6. 活体（可达才查，不可达=SKIP 不红）：引擎 /api/admin/account-identity/pending
     应为空、worker /accounts 应 logged_in。

退出码：0=无 FAIL（WARN/SKIP 允许）；1=存在 FAIL。gate 语义之外的运维读数工具，
刻意不进 pytest/gate_sweep（依赖生产数据根与活体服务）。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))
sys.path.insert(0, str(_ENGINE_ROOT / "scripts"))

RESULTS = []


def _add(level: str, code: str, msg: str) -> None:
    RESULTS.append({"level": level, "code": code, "msg": msg})


def _ro(path: Path):
    try:
        if not path.exists():
            return None
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except Exception:
        return None


def _http_json(url: str, *, bearer: str = "", timeout: float = 8.0):
    req = urllib.request.Request(url)
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    # Windows 控制台默认 GBK：图标/中文一律写不出去（UnicodeEncodeError 直接崩在
    # 打印阶段，检查全做完了却看不到结果）。UTF-8 重配失败时回落 ASCII 图标。
    _ascii_only = False
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        _ascii_only = True

    ap = argparse.ArgumentParser(description="实施72 只读验收")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from _data_root import load_merged_config, resolve_data_roots
    root = Path(resolve_data_roots(args.data_root)[0])
    cfg = load_merged_config(root) or {}
    cfg_dir = root / "config"

    # ── 1. 配置面 ────────────────────────────────────────────────────────
    pm = ((cfg.get("inbox") or {}).get("auto_draft") or {}).get("platform_modes")
    _add("info", "platform_modes", f"messenger 封顶={pm!r}（{{}}=已解除，交回预热/身份层）")
    plats = ((cfg.get("companion") or {}).get("proactive_topic") or {}).get("platforms")
    if plats == ["telegram"]:
        _add("pass", "proactive_allowlist", "主动触达白名单=telegram（跨机自聊防护在位）")
    else:
        _add("warn", "proactive_allowlist",
             f"主动触达 platforms={plats!r}（预期 ['telegram']——放开须先过自有号图谱）")
    try:
        from src.companion.proactive_peer_hygiene import own_fleet_extra_from_config
        extra = own_fleet_extra_from_config(cfg)
        if extra:
            _add("pass", "own_fleet_extra",
                 f"外机自有号登记 {len(extra)} 条：" + ",".join(
                     e.get("name") or e.get("account_id") for e in extra))
        else:
            _add("warn", "own_fleet_extra", "外机自有号登记表为空（Calixa 在别机运营应登记）")
    except Exception as exc:
        _add("fail", "own_fleet_extra", f"登记表解析异常: {exc}")
    try:
        from src.inbox.effective_automation import reconnect_backlog_cfg
        rb = reconnect_backlog_cfg(cfg)
        (_add("pass", "reconnect_backlog",
              f"重连积压封顶 enabled={rb['enabled']} "
              f"(≥{rb['min_downtime_hours']}h 断线→恢复后 {rb['window_min']}min 人审)")
         if rb.get("enabled") else
         _add("warn", "reconnect_backlog", "重连积压封顶被关闭"))
    except Exception as exc:
        _add("fail", "reconnect_backlog", f"配置解析异常: {exc}")

    # ── 2. 注册表 ────────────────────────────────────────────────────────
    reg = _ro(cfg_dir / "account_registry.db")
    if reg is None:
        _add("skip", "registry", "注册表不可读（离线/异机）")
    else:
        rows = reg.execute(
            "SELECT account_id, status, meta_json FROM platform_accounts "
            "WHERE platform='messenger'").fetchall()
        pending = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r["meta_json"] or "{}")
            except Exception:
                pass
            if meta.get("identity_pending"):
                pending.append(r["account_id"])
            if r["account_id"] == "61584070255403":
                (_add("pass", "calixa_archived", "Calixa 行=removed（本机归档只读）")
                 if r["status"] == "removed" else
                 _add("warn", "calixa_archived", f"Calixa 行 status={r['status']}（预期 removed）"))
            if r["account_id"] == "61591632195604":
                ok = (not meta.get("identity_pending")
                      and meta.get("identity_confirmed_at")
                      and meta.get("persona_id"))
                (_add("pass", "wisley_confirmed",
                      f"Wisley 已转正 persona={meta.get('persona_id')} "
                      f"prev={meta.get('identity_confirmed_prev')}")
                 if ok else
                 _add("fail", "wisley_confirmed",
                      f"Wisley 身份态异常 pending={meta.get('identity_pending')} "
                      f"confirmed_at={meta.get('identity_confirmed_at')}"))
        (_add("pass", "identity_pending", "隔离清单为空（无待确认身份）")
         if not pending else
         _add("warn", "identity_pending", f"待确认身份 {len(pending)} 个: {pending}"))
        reg.close()

    # ── 3a. 待装载 .py 水位先算（列缺失的定性要用它区分「迁移丢了」vs「等重启」）──
    stale: list = []
    try:
        boot_ts = 0.0
        for p in (root / "logs").glob("boot_*.out.log"):
            m = re.match(r"boot_(\d{8})_(\d{6})", p.name)
            if not m:
                continue
            t = time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
            boot_ts = max(boot_ts, t)
        for rel in ("src/inbox/normalizer.py", "src/inbox/persona_reply.py",
                    "src/inbox/autodraft_helpers.py", "src/inbox/store.py",
                    "src/inbox/drafts.py", "src/inbox/autosend_worker.py",
                    "src/web/routes/unified_inbox_account_routes.py",
                    "src/inbox/effective_automation.py",
                    "src/integrations/account_identity.py",
                    "src/companion/proactive_peer_hygiene.py"):
            p = _ENGINE_ROOT / rel
            if p.exists() and boot_ts and p.stat().st_mtime > boot_ts:
                stale.append(rel)
    except Exception:
        stale = []

    # ── 3. inbox 库 ──────────────────────────────────────────────────────
    ib = _ro(cfg_dir / "inbox.db")
    if ib is None:
        _add("skip", "inbox_db", "inbox 库不可读")
    else:
        cols = {c[1] for c in ib.execute("pragma table_info(messages)")}
        _store_pending = "src/inbox/store.py" in stale
        for col in ("approx_ts", "fail_reason"):
            if col in cols:
                _add("pass", f"col_{col}", f"messages.{col} 列已就位")
            elif _store_pending:
                _add("warn", f"col_{col}",
                     f"messages.{col} 列未建——store.py 迁移在盘上等下个重启窗（预期态）")
            else:
                _add("fail", f"col_{col}", f"messages.{col} 列缺失（迁移丢失？）")
        if "approx_ts" in cols:
            n = ib.execute(
                "SELECT COUNT(*) FROM messages WHERE approx_ts=1").fetchone()[0]
            _add("info", "approx_rows", f"补收打标行数={n}")
        if "fail_reason" in cols:
            tot, withr = ib.execute(
                "SELECT COUNT(*), SUM(CASE WHEN fail_reason!='' THEN 1 ELSE 0 END) "
                "FROM messages WHERE status='failed'").fetchone()
            _add("info", "fail_reasoned",
                 f"失败留痕 {tot} 行，其中带原因 {int(withr or 0)} 行"
                 "（旧行无因属预期；新失败应 100% 带因）")
        ib.close()

    # ── 4. 封顶层现算（外机自有号）────────────────────────────────────────
    try:
        from src.inbox.effective_automation import apply_mode_caps, compute_mode_caps
        caps = compute_mode_caps(
            platform="messenger", account_id="61591632195604", config=cfg,
            business_line="", connected_at=1.0,
            chat_key="1054679137531045", peer_name="Calixa Lopez",
            account_meta={})
        eff, _ = apply_mode_caps("auto_ai", caps)
        hit = any(c.layer == "own_fleet_peer" for c in caps)
        (_add("pass", "fleet_cap", f"Calixa 线程封顶现算命中（auto_ai→{eff}）")
         if hit and eff == "review" else
         _add("fail", "fleet_cap",
              f"Calixa 线程未被舰队层封顶 layers={[c.layer for c in caps]}"))
    except Exception as exc:
        _add("fail", "fleet_cap", f"封顶现算异常: {exc}")

    # ── 4b. 疑似自有号未登记候选现算（下一阶段 P1：对端命中 removed 归档账号
    #        却没登记 own_fleet.extra → 应出提示。ro 库 + 纯函数离线复算，与
    #        ops 卡同口径；有候选＝提示运营去登记（warn），全覆盖/无匹配＝pass）──
    try:
        from src.companion.proactive_peer_hygiene import (
            own_fleet_candidates,
            own_fleet_dismissed_from_config,
        )
        reg2 = _ro(cfg_dir / "account_registry.db")
        ib2 = _ro(cfg_dir / "inbox.db")
        if reg2 is None or ib2 is None:
            _add("skip", "fleet_candidates", "注册表/inbox 库不可读（离线/异机）")
        else:
            rrows = []
            for r in reg2.execute(
                    "SELECT platform, account_id, status, meta_json "
                    "FROM platform_accounts").fetchall():
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except Exception:
                    meta = {}
                rrows.append({"platform": r["platform"],
                              "account_id": r["account_id"],
                              "status": r["status"], "meta": meta})
            removed_plats = sorted({
                r["platform"] for r in rrows if r["status"] == "removed"})
            convs = []
            for plat in removed_plats:
                convs.extend(dict(x) for x in ib2.execute(
                    "SELECT platform, account_id, chat_key, display_name, "
                    "chat_type, last_ts FROM conversations WHERE platform=? "
                    "ORDER BY last_ts DESC LIMIT 500", (plat,)).fetchall())
            cands = own_fleet_candidates(rrows, convs, cfg)
            reg2.close()
            ib2.close()
            n_dis = len(own_fleet_dismissed_from_config(cfg))
            if cands:
                _add("warn", "fleet_candidates",
                     f"疑似自有号未登记 {len(cands)} 条（登记 own_fleet.extra 封人审 / "
                     "裁决为测试废号则 dismissed 消音）: "
                     + "; ".join(
                         f"{c['platform']}:{c['account_id']}↔{c['peer_name'] or c['chat_key']}"
                         f"→{c['matched_account']}" for c in cands[:5]))
            else:
                _add("pass", "fleet_candidates",
                     f"无未登记候选（removed 平台={removed_plats or '无'}，"
                     f"已裁决消音 {n_dis} 条，其余均已登记或不存在）")
    except Exception as exc:
        _add("fail", "fleet_candidates", f"候选现算异常: {exc}")

    # ── 5. 待装载 .py 水位（3a 已算，这里只汇报）───────────────────────────
    # boot 时刻从**文件名**取（boot_20260827_071526.out.log）——boot 日志的
    # mtime 随 stdout 持续更新＝恒为「现在」，拿它比对会把一切都判成已装载。
    if stale:
        _add("warn", "pending_load",
             f"{len(stale)} 个 .py 晚于实例启动（待下个重启窗装载）: "
             + ", ".join(Path(s).name for s in stale))
    else:
        _add("pass", "pending_load", "全部实施72 .py 已被在跑进程装载")

    # ── 6. 活体（可达才查）──────────────────────────────────────────────
    bearer = ""
    try:
        txt = (cfg_dir / "config.local.yaml").read_text("utf-8")
        m = re.search(r"^\s*auth_token:\s*(\S+)", txt, re.M)
        if m:
            bearer = m.group(1)
    except Exception:
        pass
    try:
        d = _http_json("http://127.0.0.1:18799/api/admin/account-identity/pending",
                       bearer=bearer)
        n = len(d.get("pending") or [])
        (_add("pass", "live_pending", "活体隔离清单为空") if n == 0 else
         _add("warn", "live_pending", f"活体隔离清单 {n} 个待确认"))
        if "fleet_candidates" in d:
            nc = len(d.get("fleet_candidates") or [])
            (_add("pass", "live_fleet_cand", "活体自有号候选检测在线（0 未登记）")
             if nc == 0 else
             _add("warn", "live_fleet_cand",
                  f"活体自有号候选 {nc} 条未登记（看 ops 🪪 卡）"))
        else:
            _add("warn", "live_fleet_cand",
                 "活体响应无 fleet_candidates 字段（候选检测代码待重启装载）")
    except Exception:
        _add("skip", "live_pending", "引擎不可达（离线核验模式）")
    try:
        d = _http_json("http://127.0.0.1:8791/accounts")
        accs = d.get("accounts") or []
        ok = any(a.get("logged_in") for a in accs)
        (_add("pass", "live_worker", f"worker 在线（{len(accs)} 账号，已登录={ok}）")
         if ok else _add("warn", "live_worker", "worker 无已登录账号"))
    except Exception:
        _add("skip", "live_worker", "messenger worker 不可达")

    # ── 输出 ─────────────────────────────────────────────────────────────
    if args.json:
        print(json.dumps({"root": str(root), "results": RESULTS},
                         ensure_ascii=False, indent=1))
    else:
        icon = ({"pass": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP",
                 "info": "..  "} if _ascii_only else
                {"pass": "✅", "warn": "⚠️ ", "fail": "❌", "skip": "⏭ ", "info": "ℹ️ "})
        print(f"=== 实施72 只读验收 @ {time.strftime('%H:%M:%S')}  root={root} ===")
        for r in RESULTS:
            print(f" {icon.get(r['level'], '  ')} [{r['code']}] {r['msg']}")
        fails = [r for r in RESULTS if r["level"] == "fail"]
        print(f"--- {'FAIL ' + str(len(fails)) if fails else 'OK'}"
              f"（pass={sum(1 for r in RESULTS if r['level'] == 'pass')} "
              f"warn={sum(1 for r in RESULTS if r['level'] == 'warn')} "
              f"skip={sum(1 for r in RESULTS if r['level'] == 'skip')}）---")
    return 1 if any(r["level"] == "fail" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
