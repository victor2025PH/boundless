#!/usr/bin/env python3
"""干净包（clean 档）产物冒烟：首启必须是「零数据」的全新形态。

smoke_backend 验「标准包首启能跑」；本脚本验 clean 增量：**生产数据一样都
不许出现**。防两类静默事故：
  1. build/seed-data 残留（上次内测打包留下）——冻结 exe 会自动发现同级
     种子目录照播不误 → 「干净包」装出来带着生产机的 KB/人设/克隆音色；
  2. 播种链将来改动后，悄悄把某类数据写回首启数据区。

断言（任一不过 exit 1）：
  · 前置：build/seed-data 不存在（在则先跑 npm run stage:clean）
  · 首启就绪 + 登录可用
  · 数据区无 profiles_runtime.yaml / voice_refs / persona_albums /
    prerender_lines / assets/voices（或存在但零文件）
  · knowledge_base.db 只含产品出厂内容：kb_entries 全部带 template_key
    （seed_system_replies 的系统话术模板，任何全新安装都会自带且可编辑；
    template_key 为空＝人工录入的业务知识＝泄漏）或 source='system'
    （J-9 #184 起 kb_entries.source 列：桌面空库首启播 3 条**停用**格式示例
    seed_kb_format_examples，与话术模板同属出厂内容；vendor/user/import 仍算
    泄漏），kb_error_codes/kb_rules/
    kb_meta 是启动预置（DEFAULT_ERROR_CODES/DEFAULT_RULES/kb_seeded_once）
    放行，其余表 0 行
  · persona_bio.db / persona_media.db 不存在，或存在但所有表 0 行
    （后端惰性建空库允许，带数据不允许）
  · config.local.yaml 未被内测 overlay 播种（无 _lan_seed / 192.168.0. 指纹）
  · /api/personas/profiles 不含生产人设（lin_xiaoyu / su_wan）

退出码：0 全过；1 断言失败；2 环境问题（产物缺失、起不来）。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import smoke_backend as sb  # noqa: E402  复用起停/HTTP 工具

DIST = HERE / "backend-dist"
SEED = HERE / "seed-data"
TOKEN = "smoke-token"

#: 生产数据种子在数据区的落点（与 config_manager._SEED_ASSET_ITEMS 同口径）
_DATA_DIRS = (
    "config/voice_refs",
    "config/prerender_lines",
    "config/persona_albums",
    "assets/voices",
)
_DATA_DBS = (
    "config/knowledge_base.db",
    "config/persona_bio.db",
    "config/persona_media.db",
)
#: 内测 overlay 的指纹（出现＝内测种子泄漏进了「干净包」链路）
_OVERLAY_FINGERPRINTS = ("_lan_seed", "192.168.0.")
#: 生产人设 id（随包人设库的代表样本，出现＝人设库泄漏）
_PROD_PERSONAS = {"lin_xiaoyu", "su_wan"}


def _db_nonempty_tables(path: Path) -> str:
    """返回非空业务表描述（空串＝所有表 0 行）。"""
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        hits = []
        for t in tables:
            n = int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
            if n:
                hits.append(f"{t}={n}行")
        return ", ".join(hits)
    finally:
        con.close()


#: 启动即预置的产品出厂表（kb_registry/admin 装配对空库必播；内容来自代码常量，
#: 每台全新安装完全一致，与生产数据无关）
_KB_BOOT_TABLES = {"kb_error_codes", "kb_rules", "kb_meta"}


def _kb_business_leak(path: Path) -> str:
    """知识库只许产品出厂内容；返回业务数据泄漏描述（空串＝干净）。"""
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        hits = []
        for t in tables:
            if t in _KB_BOOT_TABLES:
                continue
            if t == "kb_entries":
                cols = {r[1] for r in con.execute(
                    "PRAGMA table_info(kb_entries)")}
                if "template_key" not in cols:
                    n = int(con.execute(
                        "SELECT COUNT(*) FROM kb_entries").fetchone()[0])
                    if n:
                        hits.append(f"kb_entries={n}行（无 template_key 列，"
                                    "无法证明是系统话术）")
                    continue
                # J-9 #184：source='system' 是产品出厂内容（首启 3 条停用格式
                # 示例）；无 source 列的旧库按 'user' 算，口径不放宽。
                src_expr = ("COALESCE(source,'user')" if "source" in cols
                            else "'user'")
                n_biz = int(con.execute(
                    "SELECT COUNT(*) FROM kb_entries "
                    "WHERE COALESCE(template_key,'')='' "
                    f"AND {src_expr}!='system'").fetchone()[0])
                if n_biz:
                    hits.append(f"kb_entries 业务条目 {n_biz} 行"
                                "（template_key 为空且 source≠system＝非出厂内容）")
                continue
            n = int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
            if n:
                hits.append(f"{t}={n}行")
        return ", ".join(hits)
    finally:
        con.close()


def _count_files(path: Path) -> int:
    return sum(1 for p in path.rglob("*") if p.is_file()) if path.is_dir() else 0


def _start(exe: Path, data_dir: Path, port: int, log_path: Path):
    env = dict(os.environ)
    env.update({
        "AITR_DESKTOP_MODE": "1",
        "AITR_WEB_HOST": "127.0.0.1",
        "AITR_WEB_PORT": str(port),
        "AITR_WEB_TOKEN": TOKEN,
        "AITR_DATA_DIR": str(data_dir),
        "AITR_CONFIG_PATH": str(data_dir / "config" / "config.yaml"),
        # 刻意不设 AITR_SEED_DATA_DIR：让冻结 exe 走「自动发现同级 seed-data」
        # 的真实路径——build/seed-data 有残留时这里必须当场红，而不是被环境变量遮住。
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "HOST_ALERT_SILENT": "1",
    })
    logf = open(log_path, "ab")
    proc = subprocess.Popen(
        [str(exe)], cwd=str(data_dir), env=env,
        stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf


def _stop(proc) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> int:  # noqa: PLR0915
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    exe = sb._backend_exe(DIST)
    if not exe:
        print(f"✗ 未找到打包产物：{DIST}（先 npm run build:backend）", file=sys.stderr)
        return 2
    if SEED.exists():
        print(f"✗ build/seed-data 仍存在（内测种子残留）：{SEED}\n"
              "  clean 档打包前必须先 npm run stage:clean", file=sys.stderr)
        return 2

    port = sb._free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="chatx-smoke-clean-"))
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    log_path = Path(tempfile.gettempdir()) / f"chatx-smoke-clean-{port}.log"
    failures: list[str] = []

    def ok(tag: str, cond: bool, why: str = "") -> None:
        print(f"  {'✓' if cond else '✗'} {tag}"
              + (f" —— {why}" if (why and not cond) else ""))
        if not cond:
            failures.append(f"{tag}: {why}")

    print(f"→ 起后端（clean 零数据链）：{exe}\n  port={port} dataDir={data_dir}")
    proc, logf = _start(exe, data_dir, port, log_path)
    try:
        if not sb._wait_ready(base, args.timeout, proc):
            print(f"✗ {args.timeout:.0f}s 内未就绪；日志：{log_path}", file=sys.stderr)
            return 2

        print("· 首启数据区必须零数据")
        prof = data_dir / "config" / "profiles_runtime.yaml"
        if prof.exists():
            text = prof.read_text(encoding="utf-8", errors="replace")
            leaked = sorted(p for p in _PROD_PERSONAS if p in text)
            ok("profiles_runtime.yaml 无生产人设", not leaked,
               f"含 {leaked}（人设库泄漏进干净包）")
        else:
            ok("profiles_runtime.yaml 未播种", True)
        for rel in _DATA_DIRS:
            n = _count_files(data_dir / rel)
            ok(f"{rel} 零文件", n == 0, f"发现 {n} 个文件")
        for rel in _DATA_DBS:
            p = data_dir / rel
            if not p.exists():
                ok(f"{rel} 未播种", True)
                continue
            is_kb = p.name == "knowledge_base.db"
            try:
                nonempty = _kb_business_leak(p) if is_kb else _db_nonempty_tables(p)
            except Exception as exc:  # noqa: BLE001
                nonempty = f"<读取失败 {type(exc).__name__}: {exc}>"
            tag = f"{rel} 仅产品出厂内容" if is_kb else f"{rel} 为空库"
            ok(tag, not nonempty, nonempty)

        overlay = data_dir / "config" / "config.local.yaml"
        if overlay.exists():
            text = overlay.read_text(encoding="utf-8", errors="replace")
            hits = [fp for fp in _OVERLAY_FINGERPRINTS if fp in text]
            ok("config.local.yaml 无内测 overlay 指纹", not hits,
               f"命中 {hits}（内测种子 overlay 被播种）")
        else:
            ok("config.local.yaml 未被种子播种", True)

        print("· API 行为")
        jar = http.cookiejar.CookieJar()
        opener = sb._make_opener(jar)
        code, _ = sb._post_form(opener, base + "/login", {"auth_token": TOKEN})
        ok("登录", code in (200, 302, 303), f"POST /login → {code}")

        code, body = sb._get(opener, base + "/api/personas/profiles")
        ids: set[str] = set()
        if code == 200:
            try:
                d = json.loads(body)
                ids = {str(i) for i in (d.get("ids") or [])}
            except Exception:
                ids = set()
        leaked = sorted(ids & _PROD_PERSONAS)
        ok("人设接口不含生产人设", code == 200 and not leaked,
           f"code={code} leaked={leaked}")

        print()
        if failures:
            print(f"✗ clean 冒烟失败 {len(failures)} 项：", file=sys.stderr)
            for f in failures:
                print("   - " + f, file=sys.stderr)
            print(f"  后端日志：{log_path}", file=sys.stderr)
            return 1
        print("✓ 干净包产物冒烟全过（首启零数据）")
        return 0
    finally:
        _stop(proc)
        try:
            logf.close()
        except Exception:
            pass
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
        else:
            print(f"  （--keep）临时数据目录保留：{data_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
