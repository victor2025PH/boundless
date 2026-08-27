#!/usr/bin/env python3
"""lite 定制包产物冒烟：冻结后端 + lite 种子（2 人设/2 克隆音/无相册无 KB）整链验收。

用法（在 desktop/ 下）：
    python build/smoke_lite.py [--timeout 240] [--keep]

与 smoke_internal.py 的分工：那是标准内测档（12 人设全量种子）的验收；本脚本验
lite 定制档的**正反两面**——该有的在（2 人设、克隆音、预渲染、旗舰档、trial
计量补丁），刻意不带的**真的不在**（相册/KB/人设资料库；半套数据比零数据更难排查，
反向断言与 stage 的 omitted 声明一一对应）。人设重写验收也在这里：陈默必须是
跨境电商正向人设，园区旧人设词汇只许出现在 retired_facts 退役钉子里。

退出码：0 全过；1 断言失败；2 环境问题（种子/产物缺失、起不来）。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
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
DISPLAY_VERSION = "1.055"

#: lite 档「不限额只计量」契约（与 stage_internal_assets.LITE_TRIAL_CHARS 同值）
EXPECT_TRIAL_CHARS = 100_000_000


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
        print(f"✗ 未找到随包种子：{SEED}（先 stage --profile lite）", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("profile") != "lite":
        print(f"✗ 种子不是 lite 档（profile={manifest.get('profile')!r}）——"
              "先 python build/stage_internal_assets.py --profile lite --personas …",
              file=sys.stderr)
        return 2
    persona_ids = [str(p) for p in (manifest.get("personas") or [])]
    if not persona_ids:
        print("✗ manifest 无 personas", file=sys.stderr)
        return 2

    port = sb._free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="chatx-smoke-lite-"))
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    log_path = Path(tempfile.gettempdir()) / f"chatx-smoke-lite-{port}.log"
    failures: list[str] = []

    def ok(tag: str, cond: bool, why: str = "") -> None:
        print(f"  {'✓' if cond else '✗'} {tag}" + (f" —— {why}" if (why and not cond) else ""))
        if not cond:
            failures.append(f"{tag}: {why}")

    print(f"→ 起后端（lite 种子链）：{exe}\n  port={port} dataDir={data_dir}")
    proc, logf = _start(exe, data_dir, port, log_path)
    try:
        if not sb._wait_ready(base, args.timeout, proc):
            print(f"✗ {args.timeout:.0f}s 内未就绪；日志：{log_path}", file=sys.stderr)
            return 2

        # ── 1) 落盘播种（正面：该有的在）──────────────────────────────────
        print("· 首启播种落盘（正面清单）")
        overlay = data_dir / "config" / "config.local.yaml"
        ok("config.local.yaml 已播种", overlay.exists())
        cfg = {}
        if overlay.exists():
            import yaml
            cfg = yaml.safe_load(overlay.read_text(encoding="utf-8")) or {}
        lic = (cfg.get("licensing") or {})
        trial = (lic.get("trial") or {})
        ok("trial.enabled=true（字符计量开）", trial.get("enabled") is True)
        ok("trial.enforce=false（永不拦截）", trial.get("enforce") is False)
        ok(f"trial.chars={EXPECT_TRIAL_CHARS}（不限额=大额度，防 or 兜底偷换 0）",
           int(trial.get("chars") or 0) == EXPECT_TRIAL_CHARS)
        ok("plan_override=flagship（功能全开档）",
           (lic.get("feature_gate") or {}).get("plan_override") == "flagship")
        ok("avatar_voice.enabled=true",
           bool((cfg.get("avatar_voice") or {}).get("enabled") is True))

        prof_path = data_dir / "config" / "profiles_runtime.yaml"
        ok("profiles_runtime.yaml 已播种", prof_path.exists())
        if prof_path.exists():
            import yaml
            raw = prof_path.read_text(encoding="utf-8")
            pdata = yaml.safe_load(raw) or {}
            profs = pdata.get("profiles") or {}
            items = (profs if isinstance(profs, dict)
                     else {str(p.get("id")): p for p in profs if isinstance(p, dict)})
            ok(f"人设正好 {len(persona_ids)} 个（{','.join(persona_ids)}）",
               set(items.keys()) == set(persona_ids),
               f"got={sorted(items.keys())}")
            cm = items.get("chen_mo") or {}
            bg = str(cm.get("background") or "")
            ok("陈默=跨境电商正向人设", "跨境电商" in bg and "马尼拉" in bg,
               f"background 前 60 字：{bg[:60]}")
            dark = [w for w in ("园区", "狗推", "赔付", "瘫痪", "槟榔") if w in bg]
            ok("陈默 background 零暗黑残留", not dark, f"残留：{dark}")
            pins = ((cm.get("boundaries") or {}).get("retired_facts") or [])
            ok("陈默带旧人设退役钉子", bool(pins))

        n_refs = sorted(p.stem for p in (data_dir / "config" / "voice_refs").glob("*.wav")) \
            if (data_dir / "config" / "voice_refs").is_dir() else []
        ok(f"克隆参考音正好 2 个（{','.join(n_refs)}）", n_refs == sorted(persona_ids),
           f"got={n_refs}")
        n_ogg = len(list((data_dir / "assets" / "voices").rglob("*.ogg"))) \
            if (data_dir / "assets" / "voices").is_dir() else 0
        ok(f"预渲染语音 {n_ogg} 段（≥10）", n_ogg >= 10)
        extra_voice_dirs = sorted(
            p.name for p in (data_dir / "assets" / "voices").iterdir() if p.is_dir()
        ) if (data_dir / "assets" / "voices").is_dir() else []
        ok("预渲染只有选中人设的目录", set(extra_voice_dirs) <= set(persona_ids),
           f"got={extra_voice_dirs}")

        # ── 2) 反面：刻意不带的必须真的不在（或只是运行时自建的空壳）────────
        # 后端启动会自建 knowledge_base.db / persona_media.db 的空库（运行时
        # 脚手架，非种子数据）——反向断言的正确语义是「内容表零行」，
        # 「文件不存在」只是它的特例。
        print("· 省略资产反向断言（omitted 清单）")
        ok("无相册目录", not (data_dir / "config" / "persona_albums").exists())

        def _content_rows(db_name: str, table: str) -> int:
            """内容表行数；库/表不存在都算 0（缺席=最干净的空）。"""
            p = data_dir / "config" / db_name
            if not p.exists():
                return 0
            import sqlite3
            con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
            try:
                try:
                    return int(con.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.OperationalError:
                    return 0  # 表不存在
            finally:
                con.close()

        # KB：后端首启会自建库并播种**出厂示例条目**（kb_store.SEED_ENTRIES_*，
        # 电商/SaaS 客服示例 ~24 条，clean 包同样有=产品固有行为，不算泄漏）。
        # 真泄漏形态=生产 knowledge_base.db 整库随包（几百条量级）——量级封顶
        # 一抓一个准，且不与出厂示例的自然演进打架。
        n_kb = _content_rows("knowledge_base.db", "kb_entries")
        ok(f"KB 无生产库泄漏（kb_entries={n_kb} ≤ 60 出厂示例量级）", n_kb <= 60)
        n_media = _content_rows("persona_media.db", "persona_media")
        ok(f"persona_media.db 零内容（{n_media}）", n_media == 0)
        ok("无 persona_bio.db 种子", not (data_dir / "config" / "persona_bio.db").exists()
           or _content_rows("persona_bio.db", "persona_bio") == 0)

        # ── 3) API 行为 ─────────────────────────────────────────────────────
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
        ok(f"人设 API 含 {'/'.join(persona_ids)}",
           code == 200 and set(persona_ids) <= ids,
           f"code={code} ids={sorted(ids)}")
        leaked = ids & {"su_wan", "marcus_wei", "mizuki", "zhao_laoshi", "lin_jiaxin"}
        ok("标准档其余人设未泄漏进 lite 包", not leaked, f"leaked={sorted(leaked)}")

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
        ok("features: avatar_voice.enabled = on",
           feat_states.get("avatar_voice.enabled") == "on",
           f"state={feat_states.get('avatar_voice.enabled')!r}")

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

        # ── 4) 幂等（二次启动零重播 + 用户数据不被覆盖）────────────────────
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
            print(f"✗ lite 冒烟失败 {len(failures)} 项：", file=sys.stderr)
            for f in failures:
                print("   - " + f, file=sys.stderr)
            print(f"  后端日志：{log_path}", file=sys.stderr)
            return 1
        print("✓ lite 定制包产物冒烟全过")
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
