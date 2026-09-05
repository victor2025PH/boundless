#!/usr/bin/env python3
"""内测包（v1.001「与生产机同形态」）产物冒烟：冻结后端 + 随包数据种子整链验收。

用法（在 desktop/ 下）：
    python build/smoke_internal.py [--timeout 240] [--keep]

验什么（smoke_backend 验「标准包首启」，本脚本验内测增量）：
    1. 首启播种：config.local.yaml（功能 overlay）+ 人设/语音/相册/KB 落进数据区；
    2. 播种正确性：相册注册表 file_path 已绝对化且文件真实存在；
    3. 行为闭环：/api/personas/profiles 有全套人设；/api/setup/features 把
       avatar_voice / selfie 报为已开启（C 类被 overlay 打开时必须如实报 on）；
       /api/voice/avatar-status 路由活着；autosend 人工投递链已接线；
    4. 幂等：二次启动零重播（不覆盖用户数据）。

退出码：0 全过；1 断言失败；2 环境问题（种子/产物缺失、起不来）。
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
import time
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
DISPLAY_VERSION = "1.001"


def _start(exe: Path, data_dir: Path, port: int, log_path: Path):
    env = dict(os.environ)
    env.update({
        "AITR_DESKTOP_MODE": "1",
        "AITR_WEB_HOST": "127.0.0.1",
        "AITR_WEB_PORT": str(port),
        "AITR_WEB_TOKEN": TOKEN,
        "AITR_DATA_DIR": str(data_dir),
        "AITR_CONFIG_PATH": str(data_dir / "config" / "config.yaml"),
        "AITR_SEED_DATA_DIR": str(SEED),
        "AITR_APP_VERSION": DISPLAY_VERSION,
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
    manifest_path = SEED / "seed-manifest.json"
    if not manifest_path.exists():
        print(f"✗ 未找到随包种子：{SEED}（先 python build/stage_internal_assets.py）",
              file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    counts = manifest.get("counts") or {}

    port = sb._free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="chatx-smoke-internal-"))
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    log_path = Path(tempfile.gettempdir()) / f"chatx-smoke-internal-{port}.log"
    failures: list[str] = []

    def ok(tag: str, cond: bool, why: str = "") -> None:
        print(f"  {'✓' if cond else '✗'} {tag}" + (f" —— {why}" if (why and not cond) else ""))
        if not cond:
            failures.append(f"{tag}: {why}")

    print(f"→ 起后端（内测种子链）：{exe}\n  port={port} dataDir={data_dir}")
    proc, logf = _start(exe, data_dir, port, log_path)
    try:
        if not sb._wait_ready(base, args.timeout, proc):
            print(f"✗ {args.timeout:.0f}s 内未就绪；日志：{log_path}", file=sys.stderr)
            return 2

        # ── 1) 落盘播种 ────────────────────────────────────────────────────
        print("· 首启播种落盘")
        overlay = data_dir / "config" / "config.local.yaml"
        ok("config.local.yaml 已播种", overlay.exists())
        cfg = {}
        if overlay.exists():
            import yaml
            cfg = yaml.safe_load(overlay.read_text(encoding="utf-8")) or {}
        ok("avatar_voice.enabled=true",
           bool((cfg.get("avatar_voice") or {}).get("enabled") is True))
        ok("companion.selfie.enabled=true",
           bool(((cfg.get("companion") or {}).get("selfie") or {}).get("enabled") is True))
        ok("inbox.l2_autosend.deliver=true",
           bool(((cfg.get("inbox") or {}).get("l2_autosend") or {}).get("deliver") is True))

        ok("profiles_runtime.yaml 已播种",
           (data_dir / "config" / "profiles_runtime.yaml").exists())
        n_refs = len(list((data_dir / "config" / "voice_refs").glob("*.wav"))) \
            if (data_dir / "config" / "voice_refs").is_dir() else 0
        ok(f"voice_refs 参考音 {n_refs} 个", n_refs >= 3)
        n_ogg = len(list((data_dir / "assets" / "voices").rglob("*.ogg"))) \
            if (data_dir / "assets" / "voices").is_dir() else 0
        ok(f"预渲染语音 {n_ogg} 段（清单 {counts.get('prerendered_files', '?')} 文件）",
           n_ogg >= 100)
        n_album = sum(1 for p in (data_dir / "config" / "persona_albums").rglob("*")
                      if p.is_file()) \
            if (data_dir / "config" / "persona_albums").is_dir() else 0
        ok(f"相册文件 {n_album} 个", n_album >= counts.get("album_files", 50))
        for db in ("persona_bio.db", "persona_media.db"):
            ok(f"{db} 已播种", (data_dir / "config" / db).exists())
        # J-9 #184：KB 不随包。首启后端自建库只应有系统话术 + 格式示例（source∈system），
        # 出现 source=vendor 行＝旧种子链没断干净。
        kb = data_dir / "config" / "knowledge_base.db"
        if kb.exists():
            try:
                con = sqlite3.connect(str(kb))
                try:
                    n_vendor = int(con.execute(
                        "SELECT COUNT(*) FROM kb_entries WHERE COALESCE(source,'user')='vendor'"
                    ).fetchone()[0])
                finally:
                    con.close()
            except Exception:  # noqa: BLE001
                n_vendor = -1
            ok(f"KB 无厂商预置条目（vendor={n_vendor}）", n_vendor == 0)

        media_db = data_dir / "config" / "persona_media.db"
        if media_db.exists():
            con = sqlite3.connect(f"file:{media_db.as_posix()}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT file_path FROM persona_media").fetchall()
            finally:
                con.close()
            n_abs = sum(1 for (fp,) in rows if fp and Path(fp).is_absolute())
            n_exist = sum(1 for (fp,) in rows if fp and Path(fp).is_file())
            ok(f"相册注册表 {len(rows)} 行全部绝对化", n_abs == len(rows) and rows,
               f"abs={n_abs}/{len(rows)}")
            ok("相册注册表路径全部指向真实文件", n_exist == len(rows),
               f"exist={n_exist}/{len(rows)}")

        # ── 2) API 行为 ─────────────────────────────────────────────────────
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
        ok(f"人设 {len(ids)} 个（含 lin_xiaoyu/su_wan）",
           code == 200 and len(ids) >= 10 and {"lin_xiaoyu", "su_wan"} <= ids,
           f"code={code} ids={sorted(ids)[:6]}…")

        code, body = sb._get(opener, base + "/api/setup/features")
        feat_states = {}
        version = ""
        if code == 200:
            try:
                d = json.loads(body)
                version = str(d.get("version") or "")
                feat_states = {f.get("key"): f.get("state")
                               for f in d.get("features") or []
                               if isinstance(f, dict)}
            except Exception:
                pass
        ok("features 版本指纹 = " + DISPLAY_VERSION, version == DISPLAY_VERSION,
           f"version={version!r}")
        for key in ("avatar_voice.enabled", "companion.selfie.enabled"):
            ok(f"features: {key} = on", feat_states.get(key) == "on",
               f"state={feat_states.get(key)!r}")

        code, body = sb._get(opener, base + "/api/voice/avatar-status")
        ok("avatar-status 路由活着", code == 200, f"code={code}")

        code, body = sb._get(opener, base + "/api/drafts/autosend-status")
        deliver_on = human_on = False
        if code == 200:
            try:
                d = json.loads(body)
                w = d.get("worker") or {}
                deliver_on = bool(w.get("deliver_enabled"))
                human_on = bool(w.get("human_deliver_enabled"))
            except Exception:
                pass
        ok("autosend 投递链 deliver_enabled=true", deliver_on, f"code={code}")
        ok("人工通过真发链 human_deliver_enabled=true", human_on, f"code={code}")

        # ── 3) 幂等（二次启动零重播 + 用户数据不被覆盖）────────────────────
        print("· 幂等（二次启动）")
        _stop(proc)
        logf.close()
        marker = data_dir / "config" / "voice_refs" / "_user_marker.txt"
        marker.write_text("user-owned", encoding="utf-8")
        overlay_mtime = overlay.stat().st_mtime_ns
        proc, logf = _start(exe, data_dir, port, log_path)
        if not sb._wait_ready(base, args.timeout, proc):
            print("✗ 二次启动未就绪", file=sys.stderr)
            return 2
        ok("用户加的文件还在（目录未被重播）", marker.exists())
        ok("overlay 未被重写", overlay.stat().st_mtime_ns == overlay_mtime)

        print()
        if failures:
            print(f"✗ 内测冒烟失败 {len(failures)} 项：", file=sys.stderr)
            for f in failures:
                print("   - " + f, file=sys.stderr)
            print(f"  后端日志：{log_path}", file=sys.stderr)
            return 1
        print("✓ 内测包产物冒烟全过")
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
