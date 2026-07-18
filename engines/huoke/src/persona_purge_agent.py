# -*- coding: utf-8 -*-
r"""persona_purge_agent.py — 智拓(huoke)人设全域清除执行器（P5，仅标准库）。

消费集团人设总线的清除指令（契约 platform/identity/PERSONA_BUS.md §5）：

    GET  <base>/api/sync/personas/purges?system=huoke     拉取本引擎未完成指令
    POST <base>/api/sync/personas/purges/ack              逐条回执（幂等）
    鉴权均为 Authorization: Bearer <EVENT_INGEST_KEY>（与 uploader.py 同一把机器密钥）

huoke 的"养号人设"资产分布（与 tools/persona_bus/export_huoke_personas.py 的
槽位映射一一对应，删除范围严格限于指令的 source_key）：

  fb_target 家族（source_key = fb_target_personas.persona_key）
    config/fb_target_personas.yaml#personas.<key>      画像定义（L1 规则/兴趣/关键词）
    config/chat_messages.yaml#countries.<cc>           打招呼话术包（★共享护栏：仍有
                                                       其他画像用同一国家码时跳过不删）
    config/persona_knowledge.yaml#interest_topics.<CC> 客群词表（同上共享护栏）
    config/persona_knowledge.yaml#group_keywords.<CC>
    data/fb_active_persona_override.json               运行时生效客群指针（指向该 key 才删）
    data/openclaw.db#fb_target_personas.<key>          审计快照行
    data/openclaw.db#fb_profile_insights.<key>         识别结果衍生行
  studio 家族（source_key = "studio:<key>"）
    config/personas.yaml#personas.<key>                养号内容人设定义

行为要点：
  - **缺省 dry-run 演练**：只打印将删什么，不动文件、不回执；``--commit`` 才真删+ack；
  - 软删除：被删内容先落 ``data/purged_trash/<日期>/<key>/``（YAML 块存原文行、
    DB 行存 JSON、整文件直接移入），供人工复核后再行清空——trash 与 state 均在
    data/ 下，已被 engines/huoke/.gitignore 的 ``data/`` 规则覆盖，不入 git；
  - 幂等：找不到的资产计 ``detail.missing``，照常 ``ok=true`` 回执（契约 §5.3-3）；
  - 部分失败：出错则**不回执**（已删项不回滚，计入 trash；下轮重试剩余项）——
    website 侧 ack 路由一收到回执即关单，不看 ok 字段，故失败绝不能 POST；
  - 删除顺序：话术/词表/指针/DB 行在前，画像定义块最后删（它是国家码的解析源，
    中途崩溃时下轮仍能定位共享资产）；
  - YAML 块删除按缩进做文本手术（标准库无 YAML 解析器），未动的行字节原样保留
    （含 CRLF），与导出器的块指纹约定一致。

用法（缺省 dry-run + 单轮）：

    python src/persona_purge_agent.py                        # 演练：打印待办与将删清单
    python src/persona_purge_agent.py --commit --once        # 真删 + 回执，跑一轮
    python src/persona_purge_agent.py --commit --loop 600    # 常驻轮询，每 600 秒一轮
    python src/persona_purge_agent.py --selftest             # 本地 mock 集团端全链路自测

参数：--base（env PERSONA_SYNC_BASE，缺省 https://bd2026.cc）、--key（env
EVENT_INGEST_KEY）、--input（引擎根覆盖，测试/异机用）、--state-file（缺省
<engine>/data/purge_agent_state.json，记录回执审计线）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:  # GBK 控制台防中文炸 print（与 src/telemetry.py 同处理）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SYSTEM = "huoke"
STUDIO_PREFIX = "studio:"
BASE_ENV = "PERSONA_SYNC_BASE"
KEY_ENV = "EVENT_INGEST_KEY"
DEFAULT_BASE = "https://bd2026.cc"
PURGES_PATH = "/api/sync/personas/purges"
ACK_PATH = "/api/sync/personas/purges/ack"
TIMEOUT_S = 15

# 本文件位于 engines/huoke/src/ → 引擎根 = parents[1]
ENGINE_ROOT = Path(__file__).resolve().parents[1]
STATE_RELPATH = Path("data") / "purge_agent_state.json"
TRASH_RELPATH = Path("data") / "purged_trash"


def log(msg: str) -> None:
    print(f"[persona_purge_agent] {msg}")


def warn(msg: str) -> None:
    print(f"[persona_purge_agent] 警告: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── 轻量 YAML 块定位（缩进扫描，与导出器同一约定；行保留原始行尾）──────


def _plain(line: str) -> str:
    return line.rstrip("\r\n")


def _indent(line: str) -> int:
    p = _plain(line)
    return len(p) - len(p.lstrip(" "))


def _is_blank(line: str) -> bool:
    return not _plain(line).strip()


def _is_dash(line: str) -> bool:
    st = _plain(line).strip()
    return st == "-" or st.startswith("- ")


def read_raw_lines(path: Path) -> "list[str] | None":
    """utf-8(-sig) 读文件 → keepends 行列表；缺失/读失败 → None。"""
    try:
        return path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    except FileNotFoundError:
        return None
    except OSError as e:
        warn(f"{path} 读取失败：{e}")
        return None


def _body_end(lines: "list[str]", key_idx: int, indent: int, end: int) -> int:
    j = key_idx + 1
    while j < end:
        ln = lines[j]
        if _is_blank(ln):
            j += 1
            continue
        ind = _indent(ln)
        if ind > indent or (ind == indent and _is_dash(ln)):
            j += 1
            continue
        break
    return j


def section(lines: "list[str]", key: str, indent: int = 0,
            start: int = 0, end: "int | None" = None) -> "tuple[int, int] | None":
    if lines is None:
        return None
    if end is None:
        end = len(lines)
    pat = re.compile(rf"^ {{{indent}}}{re.escape(key)}:(\s|$)")
    for i in range(start, end):
        if pat.match(_plain(lines[i])):
            return i, _body_end(lines, i, indent, end)
    return None


def children(lines: "list[str]", sec: "tuple[int, int]",
             child_indent: int) -> "dict[str, tuple[int, int]]":
    key_re = re.compile(rf"^ {{{child_indent}}}([^\s#:][^:]*):(\s|$)")
    out: "dict[str, tuple[int, int]]" = {}
    cur_key, cur_start = None, -1
    s, e = sec
    for i in range(s + 1, e):
        ln = lines[i]
        if _is_blank(ln):
            continue
        ind = _indent(ln)
        if ind < child_indent:
            if cur_key is not None:
                out[cur_key] = (cur_start, i)
                cur_key = None
            break
        if ind == child_indent:
            m = key_re.match(_plain(ln))
            if cur_key is not None:
                out[cur_key] = (cur_start, i)
                cur_key = None
            if m:
                cur_key, cur_start = m.group(1).strip(), i
    if cur_key is not None:
        out[cur_key] = (cur_start, e)
    return out


def scalar(lines: "list[str]", s: int, e: int, field: str,
           indent: int) -> "str | None":
    pat = re.compile(rf"^ {{{indent}}}{re.escape(field)}:\s*(.*)$")
    for i in range(s, e):
        m = pat.match(_plain(lines[i]))
        if m:
            v = m.group(1).strip()
            if not v:
                return None
            if v[0] in "\"'":
                q = v[0]
                j = v.find(q, 1)
                return (v[1:j] if j > 0 else v.strip(q)) or None
            mm = re.search(r"\s#", v)
            if mm:
                v = v[: mm.start()]
            return v.strip() or None
    return None


# ── 清除计划（asset = {ref, kind, ...}；ref 与导出器同一语法）──────────


def _persona_cc(lines, s, e) -> str:
    cc = scalar(lines, s, e, "country_code", 4) or ""
    if not cc:
        loc = scalar(lines, s, e, "locale", 4) or ""
        if "-" in loc:
            cc = loc.split("-", 1)[1]
    return cc.strip()


def yaml_child_asset(path: Path, engine_dir: Path, top_key: str, child_key: str,
                     child_indent: int = 2) -> dict:
    """定位 <path>#<top_key>.<child_key> 块 → asset dict（找不到 present=False）。"""
    rel = path.relative_to(engine_dir).as_posix()
    ref = f"{rel}#{top_key}.{child_key}"
    lines = read_raw_lines(path)
    sec = section(lines, top_key, 0) if lines else None
    blk = children(lines, sec, child_indent).get(child_key) if sec else None
    return {"ref": ref, "kind": "yaml_block", "path": path, "top_key": top_key,
            "child_key": child_key, "child_indent": child_indent,
            "present": blk is not None}


def build_plan(engine_dir: Path, source_key: str) -> "tuple[list, list]":
    """→ (待删 assets（present 与否都在列，缺席者进 missing）, 共享护栏跳过 refs)。

    删除范围严格限 source_key；执行顺序即列表顺序（画像定义块置尾）。
    """
    cfg = engine_dir / "config"
    data = engine_dir / "data"
    assets: list = []
    skipped_shared: list = []

    if source_key.startswith(STUDIO_PREFIX):
        assets.append(yaml_child_asset(cfg / "personas.yaml", engine_dir,
                                       "personas", source_key[len(STUDIO_PREFIX):]))
        return assets, skipped_shared

    fb_path = cfg / "fb_target_personas.yaml"
    fb_asset = yaml_child_asset(fb_path, engine_dir, "personas", source_key)

    # 国家码：从画像块解析；其他仍存活的画像若共用同一国家码 → 话术/词表共享护栏
    cc = ""
    shared_cc = False
    fb_lines = read_raw_lines(fb_path)
    if fb_asset["present"] and fb_lines:
        sec = section(fb_lines, "personas", 0)
        blocks = children(fb_lines, sec, 2) if sec else {}
        blk = blocks.get(source_key)
        if blk:
            cc = _persona_cc(fb_lines, blk[0], blk[1])
        if cc:
            for other, (s, e) in blocks.items():
                if other != source_key and \
                        _persona_cc(fb_lines, s, e).upper() == cc.upper():
                    shared_cc = True
                    break

    if cc:
        script_assets = [
            yaml_child_asset(cfg / "chat_messages.yaml", engine_dir,
                             "countries", cc.lower()),
            yaml_child_asset(cfg / "persona_knowledge.yaml", engine_dir,
                             "interest_topics", cc.upper()),
            yaml_child_asset(cfg / "persona_knowledge.yaml", engine_dir,
                             "group_keywords", cc.upper()),
        ]
        if shared_cc:
            skipped_shared.extend(a["ref"] for a in script_assets if a["present"])
        else:
            assets.extend(script_assets)

    # 运行时生效客群指针：仅当指向本 key 才删
    ov_path = data / "fb_active_persona_override.json"
    ov_present = False
    try:
        obj = json.loads(ov_path.read_text(encoding="utf-8-sig"))
        ov_present = str((obj or {}).get("persona_key") or "").strip() == source_key
    except (OSError, ValueError):
        pass
    assets.append({"ref": "data/fb_active_persona_override.json", "kind": "file",
                   "path": ov_path, "present": ov_present})

    # openclaw.db 审计快照 + 识别结果衍生行
    db_path = data / "openclaw.db"
    for table, col in (("fb_target_personas", "persona_key"),
                       ("fb_profile_insights", "persona_key")):
        n = 0
        if db_path.is_file():
            try:
                conn = sqlite3.connect(str(db_path), timeout=10)
                try:
                    n = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {col}=?",
                        (source_key,)).fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.Error:
                n = 0
        assets.append({"ref": f"data/openclaw.db#{table}.{source_key}",
                       "kind": "db_rows", "path": db_path, "table": table,
                       "col": col, "present": bool(n), "rows": int(n)})

    assets.append(fb_asset)   # 画像定义块最后删（见文件头"删除顺序"）
    return assets, skipped_shared


# ── 执行（软删除到 trash；YAML 文本手术原子写回）──────────────────────


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w.\-]", "_", s)[:80] or "_"


def _trash_dir(engine_dir: Path, source_key: str) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return engine_dir / TRASH_RELPATH / day / _safe_name(source_key)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _remove_yaml_block(asset: dict, trash: Path) -> None:
    """重新定位块（文件可能已变）→ 块原文落 trash → 余下行字节原样写回。"""
    lines = read_raw_lines(asset["path"])
    if lines is None:
        raise FileNotFoundError(f"{asset['path']} 不可读")
    sec = section(lines, asset["top_key"], 0)
    blk = children(lines, sec, asset["child_indent"]).get(asset["child_key"]) \
        if sec else None
    if blk is None:
        raise LookupError("块已不存在（可能被并发修改），下轮按 missing 处理")
    s, e = blk
    trash.mkdir(parents=True, exist_ok=True)
    fn = _safe_name(f"{asset['path'].name}#{asset['top_key']}.{asset['child_key']}") \
        + ".yaml"
    (trash / fn).write_text("".join(lines[s:e]), encoding="utf-8")
    _atomic_write(asset["path"], "".join(lines[:s] + lines[e:]))


def _remove_file(asset: dict, trash: Path) -> None:
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / _safe_name(asset["path"].name)
    if dest.exists():
        dest = trash / (_safe_name(asset["path"].name) + f".{int(time.time())}")
    shutil.move(str(asset["path"]), str(dest))


def _remove_db_rows(asset: dict, trash: Path, source_key: str) -> int:
    conn = sqlite3.connect(str(asset["path"]), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(
            f"SELECT * FROM {asset['table']} WHERE {asset['col']}=?", (source_key,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if rows:
            trash.mkdir(parents=True, exist_ok=True)
            fn = _safe_name(f"openclaw.{asset['table']}.{source_key}") + ".json"
            (trash / fn).write_text(
                json.dumps({"table": asset["table"], "rows": rows},
                           ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        conn.execute(f"DELETE FROM {asset['table']} WHERE {asset['col']}=?",
                     (source_key,))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def execute_purge(engine_dir: Path, source_key: str, *, commit: bool) -> dict:
    """按计划删除（或演练）→ ack 用 detail dict（deleted/missing/errors[/skipped_shared]）。"""
    assets, skipped_shared = build_plan(engine_dir, source_key)
    detail: dict = {"deleted": [], "missing": [], "errors": []}
    if skipped_shared:
        detail["skipped_shared"] = skipped_shared
    trash = _trash_dir(engine_dir, source_key)
    for a in assets:
        if not a["present"]:
            detail["missing"].append(a["ref"])
            continue
        if not commit:
            detail["deleted"].append(a["ref"])   # dry-run：仅列入"将删"
            continue
        try:
            if a["kind"] == "yaml_block":
                _remove_yaml_block(a, trash)
            elif a["kind"] == "file":
                _remove_file(a, trash)
            elif a["kind"] == "db_rows":
                _remove_db_rows(a, trash, source_key)
            detail["deleted"].append(a["ref"])
        except LookupError:
            detail["missing"].append(a["ref"])   # 重定位失败＝已不在，幂等视同已删
        except Exception as e:                    # noqa: BLE001 —— 逐项收集，不中断其余删除
            detail["errors"].append(f"{a['ref']}: {e}")
    return detail


# ── 机器通道 HTTP（Bearer EVENT_INGEST_KEY）──────────────────────────


def http_json(url: str, key: str, *, method: str = "GET",
              body: "dict | None" = None, timeout: float = TIMEOUT_S) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {key}"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def fetch_purges(base: str, key: str) -> "list[dict]":
    url = base.rstrip("/") + PURGES_PATH + "?" + urllib.parse.urlencode(
        {"system": SYSTEM})
    doc = http_json(url, key)
    purges = doc.get("purges")
    return [p for p in purges if isinstance(p, dict)] if isinstance(purges, list) \
        else []


def ack_purge(base: str, key: str, purge_id, detail: dict) -> dict:
    body = {"purge_id": purge_id, "system": SYSTEM, "ok": True,
            "completed_at": now_iso(), "detail": detail}
    return http_json(base.rstrip("/") + ACK_PATH, key, method="POST", body=body)


# ── state 文件（回执审计线；原子写，dry-run 不落盘）───────────────────


def load_state(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    state["note"] = ("persona_purge_agent 回执审计线（已 ack 的清除指令与删除明细）。"
                     "删除本文件无碍——服务端不会重发已 ack 指令。")
    state["updated"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── 单轮处理 ─────────────────────────────────────────────────────────


def run_round(*, base: str, key: str, engine_dir: Path, state_file: Path,
              commit: bool) -> dict:
    """拉取 → 逐条删除/演练 → 回执。返回摘要 {ok, fetched, acked, failed}。"""
    mode = "commit" if commit else "dry-run（演练，不删不 ack）"
    summary = {"ok": True, "fetched": 0, "acked": 0, "failed": 0}
    try:
        purges = fetch_purges(base, key)
    except urllib.error.HTTPError as e:
        warn(f"拉取失败 HTTP {e.code}: {getattr(e, 'reason', '')}"
             f"（401=密钥错 / 503=服务端未配置 EVENT_INGEST_KEY）")
        summary["ok"] = False
        return summary
    except (urllib.error.URLError, OSError, ValueError) as e:
        warn(f"拉取失败：{e}")
        summary["ok"] = False
        return summary

    summary["fetched"] = len(purges)
    log(f"待办清除指令 {len(purges)} 条（system={SYSTEM}，模式：{mode}）")
    state = load_state(state_file) if commit else {}
    done = state.setdefault("acked", {}) if commit else {}

    for p in purges:
        purge_id = p.get("purge_id")
        source_key = str(p.get("source_key") or "").strip()
        if purge_id is None or not source_key:
            warn(f"指令缺 purge_id/source_key，跳过：{p}")
            summary["failed"] += 1
            continue
        log(f"— purge_id={purge_id} source_key={source_key} "
            f"requested_at={p.get('requested_at')}")
        detail = execute_purge(engine_dir, source_key, commit=commit)
        for label in ("deleted", "missing", "errors", "skipped_shared"):
            for item in detail.get(label, ()):  # type: ignore[arg-type]
                verb = {"deleted": "将删" if not commit else "已删",
                        "missing": "缺席(幂等)", "errors": "失败",
                        "skipped_shared": "共享跳过"}[label]
                log(f"    [{verb}] {item}")
        if detail["errors"]:
            warn(f"purge_id={purge_id} 有 {len(detail['errors'])} 项删除失败，"
                 "**不回执**，已删项不回滚，下轮重试剩余项")
            summary["failed"] += 1
            summary["ok"] = False
            continue
        if not commit:
            continue
        try:
            resp = ack_purge(base, key, purge_id, detail)
        except (urllib.error.URLError, OSError, ValueError) as e:
            warn(f"purge_id={purge_id} 回执失败（资产已删，下轮按 missing 幂等重报）：{e}")
            summary["failed"] += 1
            summary["ok"] = False
            continue
        summary["acked"] += 1
        done[str(purge_id)] = {
            "source_key": source_key, "completed_at": now_iso(),
            "deleted": detail["deleted"], "missing": detail["missing"],
            "skipped_shared": detail.get("skipped_shared", []),
            "server": {k: resp.get(k) for k in ("already", "all_acked",
                                                "persona_status")},
        }
        save_state(state_file, state)
        log(f"    已回执 ok=true（server: already={resp.get('already')} "
            f"all_acked={resp.get('all_acked')} status={resp.get('persona_status')}）")
    if commit and purges:
        save_state(state_file, state)
    return summary


# ── 自测：mock 集团端 + 临时引擎目录，dry-run/commit 两态断言 ──────────


def _selftest() -> int:  # noqa: C901 —— 线性脚本型自测，保持单函数便于对照输出
    import hashlib
    import http.server
    import tempfile
    import threading

    failures: list = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    class MockBus(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, key: str):
            super().__init__(("127.0.0.1", 0), MockHandler)
            self.key = key
            self.queue: "list[dict]" = []
            self.acks: "list[dict]" = []

    class MockHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, obj: dict) -> None:
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self) -> bool:
            srv: MockBus = self.server  # type: ignore[assignment]
            return self.headers.get("Authorization") == f"Bearer {srv.key}"

        def do_GET(self):
            srv: MockBus = self.server  # type: ignore[assignment]
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("system", [""])[0] != SYSTEM:
                self._send(400, {"error": "bad system"})
                return
            self._send(200, {"ok": True, "purges": list(srv.queue)})

        def do_POST(self):
            srv: MockBus = self.server  # type: ignore[assignment]
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            srv.acks.append(body)
            srv.queue = [p for p in srv.queue
                         if p["purge_id"] != body.get("purge_id")]
            self._send(200, {"ok": True, "purge_id": body.get("purge_id"),
                             "already": False, "all_acked": True,
                             "persona_status": "purged"})

    FB_YAML = """version: 1
default_persona: jp_a
personas:
  jp_a:
    name: "画像A（日本）"
    active: true
    country_code: JP
    locale: ja-JP
    interest_topics:
      - 料理
      - 旅行
    l1:
      pass_threshold: 20
      rules:
        - kind: name_contains_any
          value: [子, 美]
          weight: 30
  jp_b:
    name: "画像B（日本·共享国家码）"
    active: true
    country_code: JP
    locale: ja-JP
  zh_c:
    name: "画像C（中文圈）"
    country_code: CN
    locale: zh-CN
"""
    CHAT_YAML = """countries:
  jp:
    language: ja
    greeting_messages:
    - はじめまして
    - こんにちは
  cn:
    language: zh
    greeting_messages:
    - 你好
greeting_messages:
- Hello there
"""
    PK_YAML = """interest_topics:
  JP:
    female: ["料理", "旅行"]
  CN:
    female: ["育儿"]
group_keywords:
  JP:
    _base: ["ママ友"]
  CN:
    _base: ["宝妈交流"]
"""
    STUDIO_YAML = """personas:
  demo_life:
    display_name: "演示内容号"
    niche: lifestyle
    tone: "warm"
  keep_me:
    display_name: "保留位"
    niche: tech
"""

    def build_engine(tmp: Path) -> Path:
        eng = tmp / "engine"
        (eng / "config").mkdir(parents=True)
        (eng / "data").mkdir()
        (eng / "config" / "fb_target_personas.yaml").write_text(
            FB_YAML, encoding="utf-8")
        (eng / "config" / "chat_messages.yaml").write_text(
            CHAT_YAML, encoding="utf-8")
        (eng / "config" / "persona_knowledge.yaml").write_text(
            PK_YAML, encoding="utf-8")
        (eng / "config" / "personas.yaml").write_text(STUDIO_YAML, encoding="utf-8")
        (eng / "data" / "fb_active_persona_override.json").write_text(
            json.dumps({"persona_key": "jp_a"}), encoding="utf-8")
        conn = sqlite3.connect(str(eng / "data" / "openclaw.db"))
        conn.execute("CREATE TABLE fb_target_personas ("
                     "persona_key TEXT PRIMARY KEY, name TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE fb_profile_insights ("
                     "id INTEGER PRIMARY KEY, persona_key TEXT, match INTEGER)")
        conn.execute("INSERT INTO fb_target_personas VALUES"
                     " ('jp_a','画像A','2026-07-01T00:00:00Z')")
        conn.executemany("INSERT INTO fb_profile_insights (persona_key, match)"
                         " VALUES (?, ?)", [("jp_a", 1), ("jp_a", 0), ("zh_c", 1)])
        conn.commit()
        conn.close()
        return eng

    print("== 智拓人设清除执行器自测（persona_purge_agent.py --selftest）==")
    key = "selftest-key"
    server = MockBus(key)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        with tempfile.TemporaryDirectory(prefix="huoke_purge_selftest_") as tmpd:
            tmp = Path(tmpd)
            eng = build_engine(tmp)
            state_file = eng / STATE_RELPATH
            cfg_files = [eng / "config" / n for n in
                         ("fb_target_personas.yaml", "chat_messages.yaml",
                          "persona_knowledge.yaml", "personas.yaml")]

            print("[1/6] dry-run：只打印，不删、不 ack、不写 state")
            server.queue = [{"purge_id": 1, "source_key": "jp_a",
                             "source_system": SYSTEM,
                             "requested_at": "2026-07-18T00:00:00Z"}]
            before = {p: sha(p) for p in cfg_files}
            s = run_round(base=base, key=key, engine_dir=eng,
                          state_file=state_file, commit=False)
            check("拉到 1 条且 ok", s["ok"] and s["fetched"] == 1)
            check("配置文件字节未变", all(sha(p) == before[p] for p in cfg_files))
            check("未发出任何 ack（服务端仍挂 1 条待办）",
                  not server.acks and len(server.queue) == 1)
            check("override/DB 未动且无 state/trash 落盘",
                  (eng / "data" / "fb_active_persona_override.json").is_file()
                  and not state_file.exists()
                  and not (eng / TRASH_RELPATH).exists())

            print("[2/6] commit：删 jp_a —— 共享国家码 JP 的话术/词表须被护栏跳过")
            s = run_round(base=base, key=key, engine_dir=eng,
                          state_file=state_file, commit=True)
            check("回执 1 条且服务端队列清空",
                  s["ok"] and s["acked"] == 1 and not server.queue)
            fb_text = (eng / "config" / "fb_target_personas.yaml").read_text(
                encoding="utf-8")
            check("画像块 jp_a 已删、jp_b/zh_c 保留",
                  "  jp_a:" not in fb_text and "  jp_b:" in fb_text
                  and "  zh_c:" in fb_text)
            chat_text = (eng / "config" / "chat_messages.yaml").read_text(
                encoding="utf-8")
            check("countries.jp 因 jp_b 仍在而未删（共享护栏）",
                  "  jp:" in chat_text)
            ack = server.acks[-1]
            det = ack.get("detail") or {}
            check("ack 语义正确（ok=true / system=huoke / 带 skipped_shared）",
                  ack.get("ok") is True and ack.get("system") == SYSTEM
                  and any("countries.jp" in x
                          for x in det.get("skipped_shared", [])))
            check("detail.deleted 含画像块/override/DB 行引用",
                  any("personas.jp_a" in x for x in det.get("deleted", []))
                  and any("override" in x for x in det.get("deleted", []))
                  and any("fb_profile_insights" in x
                          for x in det.get("deleted", [])))
            conn = sqlite3.connect(str(eng / "data" / "openclaw.db"))
            n_a = conn.execute("SELECT COUNT(*) FROM fb_profile_insights"
                               " WHERE persona_key='jp_a'").fetchone()[0]
            n_c = conn.execute("SELECT COUNT(*) FROM fb_profile_insights"
                               " WHERE persona_key='zh_c'").fetchone()[0]
            conn.close()
            check("DB 只删 jp_a 行（zh_c 保留）", n_a == 0 and n_c == 1)
            trash_root = eng / TRASH_RELPATH
            trashed = [p.name for p in trash_root.rglob("*") if p.is_file()]
            check("软删除落 trash（画像块 yaml + DB 行 json + override）",
                  any(x.endswith(".yaml") for x in trashed)
                  and any("fb_profile_insights" in x for x in trashed)
                  and any("override" in x for x in trashed))
            check("state 记录回执审计线",
                  "1" in (load_state(state_file).get("acked") or {}))

            print("[3/6] commit：删 jp_b —— JP 不再共享，话术/词表随之删除")
            server.queue = [{"purge_id": 2, "source_key": "jp_b",
                             "source_system": SYSTEM}]
            s = run_round(base=base, key=key, engine_dir=eng,
                          state_file=state_file, commit=True)
            chat_text = (eng / "config" / "chat_messages.yaml").read_text(
                encoding="utf-8")
            pk_text = (eng / "config" / "persona_knowledge.yaml").read_text(
                encoding="utf-8")
            check("countries.jp 已删、countries.cn 与 legacy 保留",
                  s["acked"] == 1 and "  jp:" not in chat_text
                  and "  cn:" in chat_text and "Hello there" in chat_text)
            check("persona_knowledge JP 词表已删、CN 保留",
                  "  JP:" not in pk_text and "  CN:" in pk_text)

            print("[4/6] commit：studio 家族 + 未知 key 的幂等回执")
            server.queue = [{"purge_id": 3, "source_key": "studio:demo_life",
                             "source_system": SYSTEM},
                            {"purge_id": 4, "source_key": "ghost_key",
                             "source_system": SYSTEM}]
            s = run_round(base=base, key=key, engine_dir=eng,
                          state_file=state_file, commit=True)
            studio_text = (eng / "config" / "personas.yaml").read_text(
                encoding="utf-8")
            check("studio 块 demo_life 已删、keep_me 保留",
                  "  demo_life:" not in studio_text and "  keep_me:" in studio_text)
            ghost = next(a for a in server.acks if a.get("purge_id") == 4)
            gdet = ghost.get("detail") or {}
            check("未知 key：全部 missing 仍 ok=true 回执（幂等）",
                  s["acked"] == 2 and ghost.get("ok") is True
                  and not gdet.get("deleted") and gdet.get("missing"))

            print("[5/6] commit 删除失败：报错、不 ack、下轮重试")
            (eng / "config" / "personas.yaml").write_text(STUDIO_YAML,
                                                          encoding="utf-8")
            trash_block = eng / TRASH_RELPATH / datetime.now(
                timezone.utc).strftime("%Y-%m-%d") / _safe_name("studio:demo_life")
            shutil.rmtree(eng / TRASH_RELPATH)
            trash_block.parent.mkdir(parents=True)
            trash_block.write_text("occupied", encoding="utf-8")   # 让 mkdir 失败
            server.queue = [{"purge_id": 5, "source_key": "studio:demo_life",
                             "source_system": SYSTEM}]
            n_acks = len(server.acks)
            s = run_round(base=base, key=key, engine_dir=eng,
                          state_file=state_file, commit=True)
            check("失败轮 ok=False 且未回执（待办保留）",
                  not s["ok"] and s["failed"] == 1
                  and len(server.acks) == n_acks and len(server.queue) == 1)
            check("失败项未删成（personas.yaml 仍含 demo_life）",
                  "  demo_life:" in (eng / "config" / "personas.yaml").read_text(
                      encoding="utf-8"))
            trash_block.unlink()   # 解除故障后重试成功
            s = run_round(base=base, key=key, engine_dir=eng,
                          state_file=state_file, commit=True)
            check("修复后重试成功并回执", s["ok"] and s["acked"] == 1
                  and not server.queue)

            print("[6/6] 错误密钥：拉取 401，安全退出不动本地")
            s = run_round(base=base, key="wrong-key", engine_dir=eng,
                          state_file=state_file, commit=True)
            check("401 时 ok=False 且 0 条处理", not s["ok"] and s["fetched"] == 0)
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: "list[str]") -> int:
    ap = argparse.ArgumentParser(
        prog="persona_purge_agent.py",
        description="huoke 人设全域清除执行器：轮询集团 purge 指令 → 删本地养号"
                    "人设资产（软删除进 data/purged_trash/）→ 回执。缺省 dry-run。")
    ap.add_argument("--base", default=None,
                    help=f"集团站点根地址（默认 env {BASE_ENV} 或 {DEFAULT_BASE}）")
    ap.add_argument("--key", default=None,
                    help=f"机器密钥（默认 env {KEY_ENV}）")
    ap.add_argument("--input", default="",
                    help=f"引擎根目录覆盖（默认 {ENGINE_ROOT}）")
    ap.add_argument("--commit", action="store_true",
                    help="真删 + 回执（缺省为 dry-run 演练：只打印不删不 ack）")
    ap.add_argument("--once", action="store_true",
                    help="只跑一轮（缺省行为，显式传更语义化）")
    ap.add_argument("--loop", type=int, metavar="N", default=0,
                    help="常驻轮询，每 N 秒一轮（契约建议 300–900；与 --once 互斥）")
    ap.add_argument("--state-file", default="",
                    help=f"回执审计文件（默认 <engine>/{STATE_RELPATH.as_posix()}）")
    ap.add_argument("--selftest", action="store_true",
                    help="本地 mock 集团端自测（不连外网、不碰真实数据）")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.once and args.loop:
        ap.error("--once 与 --loop 互斥")

    engine_dir = Path(args.input).resolve() if args.input else ENGINE_ROOT
    state_file = Path(args.state_file).resolve() if args.state_file \
        else engine_dir / STATE_RELPATH
    base = (args.base or os.environ.get(BASE_ENV, "").strip() or DEFAULT_BASE)
    key = (args.key or os.environ.get(KEY_ENV, "").strip())
    if not key:
        print(f"[persona_purge_agent] 错误: 缺少机器密钥（--key 或环境变量 "
              f"{KEY_ENV}）", file=sys.stderr)
        return 2

    if not args.commit:
        log("dry-run 演练模式（不删除、不回执）；确认清单无误后加 --commit 执行")

    while True:
        summary = run_round(base=base, key=key, engine_dir=engine_dir,
                            state_file=state_file, commit=args.commit)
        log(f"本轮完成: fetched={summary['fetched']} acked={summary['acked']} "
            f"failed={summary['failed']} ok={summary['ok']}")
        if not args.loop:
            return 0 if summary["ok"] else 1
        time.sleep(max(30, args.loop))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
