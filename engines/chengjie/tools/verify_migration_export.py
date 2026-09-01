# -*- coding: utf-8 -*-
"""迁移包导出的只读生产探针（实施47 §5 工单 2，2026-08-28）。

**为什么需要它**：单测跑的是 tmp 里 4 条会话的合成库；「17 个账号 / 8000+ 条消息
/ 1900 个媒体文件」的真实形态（LINE 的 44 字 MID、WhatsApp 的裸手机号、Messenger
恒空的身份列、仅历史账号、群会话混入）只有真库能暴露。路由要等重启窗才装载，
本探针**不依赖 HTTP**，直接调打包核心，所以改完当场就能验。

## 绝对只读（三重保证）

1. 生产 `inbox.db` 用 **sqlite backup API** 复制到临时文件，随后所有操作只碰副本
   —— `InboxStore.__init__` 会跑 DDL + 幂等 ALTER 迁移（**写操作**），直接指向生产
   库既可能拿写锁、又违背「探针不动生产」的铁律；
2. 媒体根只读（打包默认不含媒体，`--media` 也只是读文件）；
3. 产物落系统临时目录，跑完即删（`--keep` 保留供人工拆包检查）。

## 用法

    python tools/verify_migration_export.py                # 全账号盘点（不打包）
    python tools/verify_migration_export.py --build        # 逐账号真打包 + 校验
    python tools/verify_migration_export.py --account telegram:8244899900 --build --media --keep
    python tools/verify_migration_export.py --json
    python tools/verify_migration_export.py --live         # 打真 HTTP（重启后验接线）

`--live` 是**离线核心之外**的那半条链：鉴权闸、注册表事实（label/ban）注入、
CRM 接线、`FileResponse` + 后台清理、审计落账——这些只有真打 HTTP 才验得到。
凭据走与 `smoke_voice_reuse` / `live_multiwin_drill` 同一口径（实例配置
`web_admin.auth_token` → `/login` 拿 session + Bearer 双带）；读不到 token 或
实例不可达一律 **SKIP exit 0**，不污染回归信号。

退出码：0 = 全部账号结构校验通过；1 = 有账号失败（CI/计划任务可据此告警）。
**刻意不进 gate_sweep**：它要跑真库，体量随生产增长，属人工/周期性巡检工具。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

# Windows 控制台默认 GBK：中文表头与 ✓/✗ 会直接抛 UnicodeEncodeError 把工具打死
# （本仓 PS5.1 GBK 老坑的 Python 侧同款）。强制 UTF-8，失败则退化为替换字符——
# 探针的价值是读数，绝不能因为终端码页而跑不完。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from scripts._data_root import resolve_data_roots  # noqa: E402


def _snapshot_db(src: Path, dst: Path) -> None:
    """用 sqlite backup API 把活体库快照到 dst（源侧只读连接，绝不写生产）。"""
    uri = "file:///" + str(src).replace("\\", "/").lstrip("/") + "?mode=ro"
    con_src = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        con_dst = sqlite3.connect(str(dst))
        try:
            con_src.backup(con_dst)
        finally:
            con_dst.close()
    finally:
        con_src.close()


def _accounts_in_db(db: Path) -> List[Tuple[str, str]]:
    """库里出现过的 (platform, account_id)——含仅历史账号（注册表已无的号）。"""
    uri = "file:///" + str(db).replace("\\", "/").lstrip("/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT DISTINCT platform, account_id FROM conversations"
            " WHERE platform != '' AND account_id != ''"
            " ORDER BY platform, account_id").fetchall()
    finally:
        con.close()
    return [(str(a), str(b)) for a, b in rows]


def _verify_kit(path: str, expect: Dict[str, Any]) -> List[str]:
    """拆包校验：成员齐备 / sha256 相符 / 逐行数与 manifest.counts 一致 / 无越界成员。

    返回问题列表（空 = 通过）。这是**独立复核**——刻意不复用打包时算的哈希，
    重新读一遍算一遍，否则「写错了但自己校验自己」等于没校验。
    """
    import hashlib

    problems: List[str] = []
    required = {"manifest.json", "contacts.jsonl", "contacts.csv",
                "conversations.jsonl", "media_index.jsonl", "README.txt"}
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        missing = required - names
        if missing:
            problems.append(f"缺成员 {sorted(missing)}")
            return problems
        # zip-slip：任何成员名不得逃出包
        for n in names:
            if n.startswith("/") or ".." in n.replace("\\", "/").split("/"):
                problems.append(f"成员名越界 {n!r}")
        man = json.loads(zf.read("manifest.json"))
        for name, digest in (man.get("integrity") or {}).items():
            if name not in names:
                problems.append(f"integrity 指向不存在的成员 {name}")
                continue
            actual = hashlib.sha256(zf.read(name)).hexdigest()
            if actual != digest:
                problems.append(f"{name} sha256 不符")
        # 逐行数 vs manifest.counts（导出完整率的自洽检查）
        conv_lines = zf.read("conversations.jsonl").decode("utf-8").splitlines()
        n_conv = sum(1 for x in conv_lines
                     if x.strip() and json.loads(x)["type"] == "conversation")
        n_msg = sum(1 for x in conv_lines
                    if x.strip() and json.loads(x)["type"] == "message")
        n_ct = sum(1 for x in zf.read("contacts.jsonl").decode(
            "utf-8").splitlines() if x.strip())
        counts = man.get("counts") or {}
        if n_conv != int(counts.get("conversations") or 0):
            problems.append(f"会话行数 {n_conv} != manifest {counts.get('conversations')}")
        if n_msg != int(counts.get("messages") or 0):
            problems.append(f"消息行数 {n_msg} != manifest {counts.get('messages')}")
        if n_ct != int(counts.get("contacts") or 0):
            problems.append(f"联系人行数 {n_ct} != manifest {counts.get('contacts')}")
        if int(man.get("schema_version") or 0) != 1:
            problems.append(f"schema_version={man.get('schema_version')}")
        if not zf.read("contacts.csv").startswith("\ufeff".encode("utf-8")):
            problems.append("contacts.csv 缺 UTF-8 BOM（Excel 会乱码）")
        # 媒体成员必须都在 media/ 下
        for n in names:
            if n.startswith("media") and not (n == "media_index.jsonl"
                                              or n.startswith("media/")):
                problems.append(f"可疑媒体成员 {n!r}")
    return problems


def _fmt_bytes(n: Any) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f}{unit}" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}GB"


def run_root(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    from src.inbox.migration_export import (  # noqa: E402
        build_migration_kit, collect_contacts, summarize_reachability,
    )
    from src.inbox.store import InboxStore  # noqa: E402

    live_db = root / "config" / "inbox.db"
    out: Dict[str, Any] = {"data_root": str(root), "db": str(live_db),
                           "accounts": [], "ok": True}
    if not live_db.is_file():
        out["ok"] = False
        out["error"] = "inbox.db 不存在"
        return out

    tmpdir = Path(tempfile.mkdtemp(prefix="mig-verify_"))
    try:
        snap = tmpdir / "inbox.db"
        _snapshot_db(live_db, snap)
        out["db_bytes"] = live_db.stat().st_size
        targets = _accounts_in_db(snap)
        if args.account:
            want = {tuple(a.split(":", 1)) for a in args.account}
            targets = [t for t in targets if t in want]
            if not targets:
                out["ok"] = False
                out["error"] = f"指定账号在库中不存在：{sorted(want)}"
                return out
        store = InboxStore(snap)
        try:
            for plat, acct in targets:
                rec: Dict[str, Any] = {"platform": plat, "account_id": acct}
                rows = collect_contacts(store, plat, acct)
                reach = summarize_reachability(rows)
                counts = store.count_account_data(plat, acct) or {}
                src: Dict[str, int] = {}
                for r in rows:
                    k = str(r.get("handle_source") or "none")
                    src[k] = src.get(k, 0) + 1
                rec.update({
                    "conversations": int(counts.get("conversations") or 0),
                    "messages": int(counts.get("messages") or 0),
                    "contacts": reach["total"],
                    "reachability": reach,
                    "handle_source": src,
                    "coverage_pct": (round(100.0 * reach["covered"]
                                           / reach["total"], 1)
                                     if reach["total"] else None),
                })
                if args.build:
                    kit = build_migration_kit(
                        store, plat, acct, include_media=args.media,
                        out_dir=tmpdir)
                    probs = _verify_kit(kit.path, rec)
                    rec.update({
                        "kit": kit.filename,
                        "kit_bytes": kit.size_bytes,
                        "reconciled": kit.reconciled,
                        "media_files": kit.counts.get("media_files"),
                        "media_missing": kit.counts.get("media_missing"),
                        "problems": probs,
                    })
                    if probs or not kit.reconciled:
                        out["ok"] = False
                    if args.keep:
                        dest = Path(args.keep) / kit.filename
                        Path(args.keep).mkdir(parents=True, exist_ok=True)
                        shutil.copy2(kit.path, dest)
                        rec["kept"] = str(dest)
                    try:
                        os.unlink(kit.path)
                    except OSError:
                        pass
                out["accounts"].append(rec)
        finally:
            try:
                store.close()
            except Exception:
                pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


# ── --live：真 HTTP 链路（重启后验接线）──────────────────────────────────────

def _read_auth_token(root: Path) -> str:
    """从实例配置读 ``web_admin.auth_token``（与 smoke_voice_reuse 同口径）。"""
    import yaml
    for name in ("config.local.yaml", "config.yaml"):
        fp = Path(root) / "config" / name
        if not fp.is_file():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


class _LiveClient:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))

    @staticmethod
    def _hdrs(raw: Any) -> Dict[str, str]:
        """响应头 → **键全小写**的 dict。

        HTTP/1.1 头名不区分大小写，而 uvicorn 实际发的是 `content-type` 全小写；
        `dict(r.headers)` 出来是区分大小写的普通 dict，按 `Content-Type` 取就恒
        为 None——首跑就被这个坑判了两条假 FAIL（端点本身完全正常，89KB 包已下完
        且校验全过）。统一小写化，调用方一律用小写键。
        """
        try:
            return {str(k).lower(): str(v) for k, v in dict(raw or {}).items()}
        except Exception:
            return {}

    def _raw(self, req: urllib.request.Request,
             timeout: float) -> Tuple[int, bytes, Dict[str, str]]:
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.read(), self._hdrs(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read() if e.fp else b"", self._hdrs(e.headers)
        except Exception as e:  # noqa: BLE001
            return 0, str(e).encode("utf-8", "replace"), {}

    def login(self) -> bool:
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(
            self.base + "/login", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        code, _, _ = self._raw(req, 30)
        return code in (200, 303) and any(
            c.name == "session" for c in self._cj)

    def get(self, path: str,
            timeout: float = 180) -> Tuple[int, bytes, Dict[str, str]]:
        req = urllib.request.Request(
            self.base + path, method="GET",
            headers={"Authorization": f"Bearer {self.token}"})
        return self._raw(req, timeout)


def run_live(args: argparse.Namespace) -> int:
    """打真 HTTP：预览 → 导出 → 拆包校验 → 审计落账 → 临时包已清理。"""
    root = Path(args.data_root) if args.data_root else Path(
        r"D:\chengjie-instances\zhiliao\data")
    token = _read_auth_token(root)
    if not token:
        print(f"[SKIP] 未能从 {root} 读到 web_admin.auth_token（exit 0）")
        return 0
    cli = _LiveClient(args.base, token)
    if not cli.login():
        print(f"[SKIP] {args.base} 不可达或登录失败（exit 0）")
        return 0

    fails: List[str] = []

    def check(name: str, cond: Any, detail: str = "") -> None:
        ok = bool(cond)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + (f"  {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    # 目标账号：优先命令行指定，否则挑「联系人最多」的（覆盖率派生也在那儿）
    target = (args.account[0] if args.account else "")
    if not target:
        snap_dir = Path(tempfile.mkdtemp(prefix="mig-live_"))
        try:
            snap = snap_dir / "inbox.db"
            _snapshot_db(root / "config" / "inbox.db", snap)
            accts = _accounts_in_db(snap)
        finally:
            shutil.rmtree(snap_dir, ignore_errors=True)
        target = f"{accts[0][0]}:{accts[0][1]}" if accts else ""
    if ":" not in target:
        print("[SKIP] 库里没有可验账号（exit 0）")
        return 0
    plat, acct = target.split(":", 1)
    print(f"\n=== live 接线验证 {args.base}  账号 {plat}:{acct} ===")

    # S1 预览
    code, body, _ = cli.get(
        f"/api/accounts/{plat}/{acct}/migration-preview", timeout=120)
    pv: Dict[str, Any] = {}
    try:
        pv = json.loads(body.decode("utf-8"))
    except Exception:
        pass
    check("S1 预览 200 + ok", code == 200 and pv.get("ok") is True,
          f"http={code}")
    check("S1 带注册表事实（label/ban 键存在）",
          "label" in pv and "ban" in pv,
          f"label={pv.get('label')!r} ban={bool(pv.get('ban'))}")
    check("S1 覆盖率四桶齐备",
          set(("both", "username", "phone", "none", "total", "covered"))
          <= set((pv.get("reachability") or {}).keys()),
          str(pv.get("reachability")))

    # S2 真导出（默认不带媒体）
    q = "?media=1" if args.media else ""
    code, blob, hdrs = cli.get(
        f"/api/accounts/{plat}/{acct}/export-migration{q}", timeout=600)
    check("S2 导出 200 + application/zip",
          code == 200 and "zip" in str(hdrs.get("content-type", "")).lower(),
          f"http={code} ct={hdrs.get('content-type')} bytes={len(blob)}")
    cd = str(hdrs.get("content-disposition", ""))
    check("S2 Content-Disposition 带包名",
          "chatx-migration_" in cd, cd[:90])

    if code == 200 and blob:
        out_dir = Path(args.keep) if args.keep else Path(
            tempfile.mkdtemp(prefix="mig-live-dl_"))
        out_dir.mkdir(parents=True, exist_ok=True)
        dl = out_dir / "live.zip"
        dl.write_bytes(blob)
        try:
            probs = _verify_kit(str(dl), {})
            check("S3 拆包校验（成员/sha256/行数对账/无越界）",
                  not probs, "; ".join(probs[:3]))
            with zipfile.ZipFile(dl, "r") as zf:
                man = json.loads(zf.read("manifest.json"))
            check("S3 manifest 与预览同口径",
                  man["counts"]["contacts"] == pv.get("contacts")
                  and man["reachability"] == pv.get("reachability"),
                  f"kit={man['counts']['contacts']} preview={pv.get('contacts')}")
            check("S3 覆盖率算法自描述随包走",
                  (man.get("reachability_rule") or {}).get(
                      "derive_phone_from_chat_key") == ["whatsapp"],
                  str(man.get("reachability_rule")))
            if args.keep:
                print(f"      → 已保留 {dl}")
        finally:
            if not args.keep:
                shutil.rmtree(out_dir, ignore_errors=True)

    # S4 审计落账（只在真下载完成后才该有一条）
    try:
        sys.path.insert(0, str(ENGINE_ROOT))
        from src.ops.ops_events import get_ops_event_store
        os.environ.setdefault("AITR_DATA_DIR", str(root))
        evs = get_ops_event_store(
            str(root / "config" / "ops_events.db"))
        rows = evs.recent_kinds(["account_export_migration"], limit=3)
        newest = rows[0] if rows else {}
        check("S4 审计落一条 account_export_migration",
              bool(rows) and newest.get("account_id") == acct,
              str(newest.get("detail", ""))[:120])
        check("S4 审计不含联系人内容（只有数字/操作者）",
              "@" not in str(newest.get("detail", "")),
              "")
    except Exception as e:  # noqa: BLE001
        check("S4 审计可读", False, str(e)[:120])

    # S5 临时包已被后台任务清理（一次性产物不许在磁盘堆积）
    leftovers = sorted((root / "tmp_migration").glob("*.zip")) \
        if (root / "tmp_migration").is_dir() else []
    check("S5 服务端临时包已清理", not leftovers,
          f"残留 {len(leftovers)} 个" if leftovers else "")

    print("\n" + (f"✗ live 失败 {len(fails)} 项: {fails}" if fails
                  else "✓ live 全部通过"))
    return 1 if fails else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="迁移包导出只读探针（真库快照，绝不写生产）")
    ap.add_argument("--data-root", default="",
                    help="实例数据根（缺省按 scripts/_data_root 契约自动发现）")
    ap.add_argument("--account", action="append", default=[],
                    metavar="platform:account_id",
                    help="只验指定账号（可重复）")
    ap.add_argument("--build", action="store_true",
                    help="真打包并拆包校验（默认只盘点，不产出文件）")
    ap.add_argument("--media", action="store_true",
                    help="打包时含媒体原件（配合 --build）")
    ap.add_argument("--keep", default="",
                    help="把产出的包留到该目录供人工拆检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--live", action="store_true",
                    help="打真 HTTP 验接线（鉴权/注册表事实/下载/清理/审计）")
    ap.add_argument("--base", default="http://127.0.0.1:18799",
                    help="--live 的实例地址")
    args = ap.parse_args(argv)

    if args.live:
        return run_live(args)

    results = [run_root(r, args) for r in resolve_data_roots(args.data_root)]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(r.get("ok") for r in results) else 1

    rc = 0
    for res in results:
        print(f"\n=== 数据根 {res['data_root']} ===")
        if res.get("error"):
            print(f"  ✗ {res['error']}")
            rc = 1
            continue
        print(f"  inbox.db {_fmt_bytes(res.get('db_bytes'))}"
              f"  账号 {len(res['accounts'])} 个")
        if not res["accounts"]:
            print("  （该根库里没有任何会话，跳过）")
            continue
        hdr = (f"  {'平台':<10} {'账号':<26} {'会话':>6} {'消息':>7} "
               f"{'联系人':>6} {'可加回':>8} {'派生':>5}")
        if args.build:
            hdr += f"  {'包体':>8} {'对账':>4} {'校验':>6}"
        print(hdr)
        for a in res["accounts"]:
            aid = a["account_id"]
            shown = aid if len(aid) <= 26 else aid[:23] + "..."
            cov = ("—" if a["coverage_pct"] is None
                   else f"{a['coverage_pct']:.0f}%")
            derived = int((a.get("handle_source") or {}).get("chat_key") or 0)
            line = (f"  {a['platform']:<10} {shown:<26} "
                    f"{a['conversations']:>6} {a['messages']:>7} "
                    f"{a['contacts']:>6} {cov:>8} "
                    f"{(derived or '-'):>5}")
            if args.build:
                probs = a.get("problems") or []
                line += (f"  {_fmt_bytes(a.get('kit_bytes')):>8} "
                         f"{'✓' if a.get('reconciled') else '✗':>4} "
                         f"{'✓' if not probs else '✗ ' + str(len(probs)):>6}")
                if probs:
                    rc = 1
            print(line)
            for p in (a.get("problems") or []):
                print(f"      ! {p}")
            if a.get("kept"):
                print(f"      → 已保留 {a['kept']}")
        if not res.get("ok"):
            rc = 1
        # 汇总：可加回覆盖率是本主题对外唯一诚实指标（实施47 §3）
        tot = sum(x["contacts"] for x in res["accounts"])
        cov = sum(x["reachability"]["covered"] for x in res["accounts"])
        drv = sum(int((x.get("handle_source") or {}).get("chat_key") or 0)
                  for x in res["accounts"])
        print(f"  合计：{tot} 人（各账号累计，同一人在多号上重复计），"
              f"可加回 {cov} 人"
              + (f"（{100.0 * cov / tot:.1f}%）" if tot else ""))
        print(f"  其中 {drv} 人的号码由会话标识派生（WhatsApp 的 chat_key 即手机号）"
              f"——资产中心卡片那条 SQL 尚未采用该规则，读数会比这里低。")
    print("\n" + ("✓ 全部通过" if rc == 0 else "✗ 存在失败项"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
