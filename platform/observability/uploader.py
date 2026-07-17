#!/usr/bin/env python3
r"""无界 Boundless spool 事件补传器（仅 Python 标准库）。

读本地 spool 目录（emitter.py 落盘的 events-YYYYMMDD.jsonl），从上次已上传的
字节偏移续读**完整行**，按批 POST 到集团收集器（website /api/collect），上传
成功后才推进偏移并原子写回 state 文件 —— 断点续传、崩溃安全：先读后传，传
成功再写 state；宕机最多重发一批，收集端按 event_id 幂等去重（契约 §6），
重发不重计。

用法（cron 每 5 分钟一次，单实例运行，勿并发跑同一 spool 目录）：

    python platform/observability/uploader.py \
        --endpoint https://bd2026.cc/api/collect --key <EVENT_INGEST_KEY>

    # --key 缺省读环境变量 EVENT_INGEST_KEY；
    # --spool-dir 缺省读 EVENT_SPOOL_DIR，再缺省 ./data/events/spool；
    # --state-file 缺省 <spool 目录>/.upload_state.json（记录各文件已传字节偏移）；
    # --batch 每批条数（默认 200，收集端上限 500）；
    # --dry-run 只统计将要上传的行数，不联网、不写 state；
    # --selftest 线程内起本地 http mock 收集器自测批量与断点逻辑（不连外网）。

行为要点：
  - 只上传以 \n 结尾的完整行；文件尾部未写完的半行留待下次运行；
  - 无法 JSON 解析的脏行跳过并照常推进偏移（不会卡死补传），计入 skipped；
  - 5xx / 网络错误 / 超时：指数退避重试 3 次仍失败 → 本轮终止、偏移不动、
    退出码 1，下次 cron 从原偏移续传；
  - 4xx（401 密钥错 / 503 收集器未配置 / 400 协议错）属配置类错误，不重试
    直接终止（重试也不会好）；
  - 收集端响应里的 rejected（信封校验失败）不阻塞偏移推进——重发不会变合法，
    条数计入摘要供治理排查；
  - state 缺失/损坏 → 全部从偏移 0 重传（幂等入库兜底）；偏移大于当前文件
    大小（文件被截断/替换）→ 该文件重置为 0 重传，同样靠幂等兜底。

【重要】顶层目录 platform 与 Python 标准库 platform 模块同名，本文件与
emitter.py 同款约定：不要 ``import platform.observability``，加载方式见
emitter.py 顶部注释。本文件按脚本直跑即可，无需 import。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SPOOL_ENV_VAR = "EVENT_SPOOL_DIR"
KEY_ENV_VAR = "EVENT_INGEST_KEY"
DEFAULT_SPOOL_DIR = os.path.join(".", "data", "events", "spool")
STATE_FILENAME = ".upload_state.json"
DEFAULT_BATCH = 200
MAX_BATCH = 500           # 收集端单批上限（超出会被 413），本地先行钳制
TIMEOUT_S = 10
RETRIES = 3               # 5xx/网络错的重试次数（不含首次尝试）


class ConfigError(RuntimeError):
    """4xx 配置/协议类错误：密钥错、收集器未配置、批协议不符——重试无意义。"""


class TransportError(RuntimeError):
    """网络/5xx 类错误：重试耗尽后抛出，本轮终止、偏移不动。"""


# ── state 文件（每个 spool 目录一份，记录各文件已上传字节偏移）──────────────

def load_state(state_path: str) -> "dict[str, int]":
    """读 state → {文件名: 已上传字节偏移}。缺失/损坏一律当空（幂等兜底重传）。"""
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("files")
        out: "dict[str, int]" = {}
        if isinstance(files, dict):
            for name, entry in files.items():
                if (isinstance(name, str) and isinstance(entry, dict)
                        and isinstance(entry.get("offset"), int) and entry["offset"] >= 0):
                    out[name] = entry["offset"]
        return out
    except Exception:
        return {}


def save_state(state_path: str, offsets: "dict[str, int]") -> None:
    """原子写 state（临时文件 + fsync + os.replace），崩溃不会留半个 JSON。"""
    payload = {
        "note": "uploader.py 断点游标：各 spool 文件已成功上传的字节偏移。删除本文件会全量重传（收集端幂等，不重计）。",
        "updated": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "files": {name: {"offset": off} for name, off in sorted(offsets.items())},
    }
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, state_path)


# ── HTTP 上报 ────────────────────────────────────────────────────────────────

def post_batch(endpoint: str, key: str, source: str, events: "list[dict]",
               timeout: float = TIMEOUT_S, retries: int = RETRIES,
               backoff_base: float = 1.0) -> dict:
    """POST 一批信封到收集器，返回响应 JSON（dict）。

    5xx/网络错按 backoff_base * 2^n 退避重试 retries 次；4xx 抛 ConfigError；
    重试耗尽抛 TransportError。
    """
    body = json.dumps({"events": events}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "X-Event-Source": source,
    }
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if 400 <= exc.code < 500:
                raise ConfigError(f"HTTP {exc.code}（配置/协议错误，不重试）: {detail}") from None
            if attempt > retries:
                raise TransportError(f"HTTP {exc.code}（重试 {retries} 次后仍失败）: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            if attempt > retries:
                raise TransportError(f"网络错误（重试 {retries} 次后仍失败）: {exc}") from None
        time.sleep(backoff_base * (2 ** (attempt - 1)))


# ── 主流程 ──────────────────────────────────────────────────────────────────

def _merge_response(summary: dict, resp: dict) -> None:
    summary["accepted"] += int(resp.get("accepted") or 0)
    summary["duplicates"] += int(resp.get("ignoredDuplicates") or 0)
    rejected = resp.get("rejected")
    summary["rejected"] += len(rejected) if isinstance(rejected, list) else 0


def run_upload(spool_dir: str, endpoint: "str | None", key: "str | None",
               state_file: "str | None" = None, batch: int = DEFAULT_BATCH,
               dry_run: bool = False, source: "str | None" = None,
               timeout: float = TIMEOUT_S, retries: int = RETRIES,
               backoff_base: float = 1.0, quiet: bool = False) -> dict:
    """扫描 spool 目录逐文件断点补传。返回摘要 dict（含 ok 布尔）。"""
    batch = max(1, min(int(batch), MAX_BATCH))
    src = (source or "").strip() or socket.gethostname() or "uploader"
    state_path = state_file or os.path.join(spool_dir, STATE_FILENAME)
    summary: dict = {
        "ok": True, "dry_run": dry_run, "spool_dir": spool_dir,
        "files": 0, "lines": 0, "batches": 0,
        "accepted": 0, "duplicates": 0, "rejected": 0,
        "skipped": 0, "partial_bytes": 0, "reset_files": 0,
        "error": None,
    }

    try:
        names = sorted(n for n in os.listdir(spool_dir)
                       if n.startswith("events-") and n.endswith(".jsonl"))
    except FileNotFoundError:
        if not quiet:
            print(f"spool 目录不存在（尚无事件可传）: {spool_dir}")
        return summary

    offsets = load_state(state_path)
    state_dirty = False

    def advance(name: str, new_offset: int) -> None:
        nonlocal state_dirty
        if offsets.get(name) == new_offset:
            return
        offsets[name] = new_offset
        state_dirty = True
        if not dry_run:
            save_state(state_path, offsets)   # 每批成功即落盘：崩溃最多重发一批

    try:
        for name in names:
            path = os.path.join(spool_dir, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            offset = offsets.get(name, 0)
            if offset > size:   # 文件被截断/替换：重置重传，收集端幂等兜底
                offset = 0
                summary["reset_files"] += 1
            if offset >= size:
                continue
            summary["files"] += 1

            with open(path, "rb") as f:
                f.seek(offset)
                pending: "list[dict]" = []   # 已解析待上传的信封
                pending_end = offset          # 该批成功后可推进到的偏移

                def flush() -> None:
                    nonlocal pending, pending_end
                    if pending and not dry_run:
                        resp = post_batch(endpoint or "", key or "", src, pending,
                                          timeout=timeout, retries=retries,
                                          backoff_base=backoff_base)
                        _merge_response(summary, resp)
                    if pending:
                        summary["batches"] += 1
                    advance(name, pending_end)
                    pending = []

                while True:
                    line = f.readline()
                    if not line or not line.endswith(b"\n"):
                        if line:
                            summary["partial_bytes"] += len(line)   # 半行留待下次
                        break
                    end = f.tell()
                    stripped = line.strip()
                    if stripped:
                        obj = None
                        try:
                            obj = json.loads(stripped.decode("utf-8"))
                        except Exception:
                            pass
                        if isinstance(obj, dict):
                            pending.append(obj)
                            summary["lines"] += 1
                        else:
                            summary["skipped"] += 1
                    pending_end = end
                    if len(pending) >= batch:
                        flush()
                flush()   # 尾批（含仅脏行时的纯偏移推进）
    except (ConfigError, TransportError) as exc:
        summary["ok"] = False
        summary["error"] = str(exc)

    if not quiet:
        verb = "将上传（dry-run，未联网未写 state）" if dry_run else "已上传"
        print("== spool 补传摘要 ==")
        print(f"  spool: {spool_dir}")
        print(f"  文件 {summary['files']} 个有新数据 · {verb} {summary['lines']} 行 / {summary['batches']} 批")
        if not dry_run:
            print(f"  收集端: accepted={summary['accepted']} ignoredDuplicates={summary['duplicates']} rejected={summary['rejected']}")
        if summary["skipped"]:
            print(f"  跳过脏行: {summary['skipped']}")
        if summary["partial_bytes"]:
            print(f"  尾部半行 {summary['partial_bytes']} 字节留待下次")
        if summary["reset_files"]:
            print(f"  {summary['reset_files']} 个文件偏移大于文件大小，已重置为 0 重传")
        if summary["error"]:
            print(f"  [错误] 本轮提前终止（偏移已保留，下次续传）: {summary['error']}")
    return summary


# ── 自测：线程内本地 http mock 收集器，断言批量与断点逻辑 ────────────────────

def _selftest() -> int:
    import http.server
    import tempfile
    import threading

    failures: "list[str]" = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    # Crockford Base32 假 event_id（26 位，前 10 位时间位填 0，满足收集端正则）
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    event_id_re = re.compile(r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")
    counter = [0]

    def fake_event(name: str = "website.lead.submitted", product: str = "website") -> dict:
        counter[0] += 1
        n, tail = counter[0], ""
        for _ in range(16):
            tail = alphabet[n % 32] + tail
            n //= 32
        return {
            "event_id": "evt_" + "0" * 10 + tail,
            "ts": "2026-07-18T04:00:00.123Z",
            "product_id": product,
            "name": name,
            "props": {"i": counter[0]},
        }

    class MockCollector(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, key: str):
            super().__init__(("127.0.0.1", 0), MockHandler)
            self.lock = threading.Lock()
            self.expected_key = key
            self.batches: "list[dict]" = []
            self.seen_ids: "set[str]" = set()
            self.fail_budget = 0   # >0 时先回 500（测重试）

    class MockHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):   # 静音
            pass

        def _send(self, status: int, obj: dict) -> None:
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            srv: MockCollector = self.server  # type: ignore[assignment]
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            with srv.lock:
                if srv.fail_budget > 0:
                    srv.fail_budget -= 1
                    self._send(500, {"error": "mock_boom"})
                    return
                if self.headers.get("Authorization") != f"Bearer {srv.expected_key}":
                    self._send(401, {"error": "unauthorized"})
                    return
                events = json.loads(body.decode("utf-8")).get("events", [])
                if len(events) > MAX_BATCH:
                    self._send(413, {"error": "batch_too_large"})
                    return
                srv.batches.append({"events": events, "source": self.headers.get("X-Event-Source")})
                accepted, dup, rejected = 0, 0, []
                for i, ev in enumerate(events):
                    eid = ev.get("event_id") if isinstance(ev, dict) else None
                    if not isinstance(eid, str) or not event_id_re.fullmatch(eid):
                        rejected.append({"index": i, "reason": "bad event_id"})
                    elif eid in srv.seen_ids:
                        dup += 1
                    else:
                        srv.seen_ids.add(eid)
                        accepted += 1
                self._send(200, {"ok": True, "accepted": accepted,
                                 "ignoredDuplicates": dup, "rejected": rejected})

    print("== spool 补传器自测（uploader.py --selftest）==")
    key = "selftest-key"
    server = MockCollector(key)
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/api/collect"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(**kw) -> dict:
        return run_upload(endpoint=endpoint, key=key, backoff_base=0.05,
                          quiet=True, source="selftest-host", **kw)

    try:
        with tempfile.TemporaryDirectory(prefix="boundless_uploader_selftest_") as tmp:
            spool = os.path.join(tmp, "spool")
            os.makedirs(spool)
            file_a = os.path.join(spool, "events-20260701.jsonl")
            file_b = os.path.join(spool, "events-20260702.jsonl")
            state = os.path.join(spool, STATE_FILENAME)

            def write_lines(path: str, rows: "list") -> None:
                with open(path, "a", encoding="utf-8", newline="\n") as f:
                    for r in rows:
                        f.write((json.dumps(r, separators=(",", ":")) if isinstance(r, dict) else r) + "\n")

            write_lines(file_a, [fake_event() for _ in range(5)])
            write_lines(file_b, [fake_event("platform.license.issued", "platform") for _ in range(3)])
            write_lines(file_b, ["这不是JSON"])                      # 脏行：跳过但推进偏移
            with open(file_b, "a", encoding="utf-8", newline="\n") as f:
                f.write('{"event_id":"evt_partial')                  # 半行：留待下次

            print("[1/8] 首轮上传：批量拆分 + 脏行跳过 + 半行滞留")
            s = run(spool_dir=spool, batch=2)
            check("ok 且 8 行上传（A5 + B3）", s["ok"] and s["lines"] == 8)
            check("批次 = 5（A: 2+2+1，B: 2+1，不跨文件拼批）",
                  s["batches"] == 5 and len(server.batches) == 5)
            check("每批不超过 batch=2", all(len(b["events"]) <= 2 for b in server.batches))
            check("收集端 accepted=8 无重复无拒收",
                  (s["accepted"], s["duplicates"], s["rejected"]) == (8, 0, 0))
            check("脏行 skipped=1、半行 partial 滞留", s["skipped"] == 1 and s["partial_bytes"] > 0)
            check("X-Event-Source 透传", all(b["source"] == "selftest-host" for b in server.batches))
            offs = load_state(state)
            check("state: A 偏移 = 文件大小（全量已传）",
                  offs.get("events-20260701.jsonl") == os.path.getsize(file_a))
            b_expect = os.path.getsize(file_b) - s["partial_bytes"]
            check("state: B 偏移停在半行之前", offs.get("events-20260702.jsonl") == b_expect)

            print("[2/8] 立刻重跑：断点续传，0 新增")
            n_before = len(server.batches)
            s = run(spool_dir=spool, batch=2)
            check("ok 且 0 行 0 批（无新数据）", s["ok"] and s["lines"] == 0 and s["batches"] == 0)
            check("mock 未收到新请求", len(server.batches) == n_before)

            print("[3/8] 追加数据：只传增量（半行补全 + A 追加 2 条）")
            with open(file_b, "a", encoding="utf-8", newline="\n") as f:
                f.write('X"}\n')   # 把半行补成完整行（可解析 JSON，evt_partialX"} → 非法 id 由收集端 rejected）
            write_lines(file_a, [fake_event() for _ in range(2)])
            s = run(spool_dir=spool, batch=200)
            check("只读增量 3 行（A2 + B 半行补全 1）", s["ok"] and s["lines"] == 3)
            check("增量 accepted=2 且 rejected=1（坏 event_id 不阻塞偏移）",
                  s["accepted"] == 2 and s["rejected"] == 1)
            offs = load_state(state)
            check("state: A/B 偏移均推进到文件末尾",
                  offs.get("events-20260701.jsonl") == os.path.getsize(file_a)
                  and offs.get("events-20260702.jsonl") == os.path.getsize(file_b))

            print("[4/8] 删 state 全量重传：收集端幂等去重")
            os.remove(state)
            s = run(spool_dir=spool, batch=200)
            check("全量重读 11 行", s["ok"] and s["lines"] == 11)
            check("accepted=0 且 ignoredDuplicates=10（幂等）+ rejected=1",
                  (s["accepted"], s["duplicates"], s["rejected"]) == (0, 10, 1))

            print("[5/8] 5xx 退避重试：前 2 次 500 后成功")
            server.fail_budget = 2
            write_lines(file_a, [fake_event()])
            n_before = len(server.batches)
            s = run(spool_dir=spool, batch=200)
            check("重试后成功 accepted=1", s["ok"] and s["accepted"] == 1)
            check("500 预算耗尽（发生了 2 次失败重试）", server.fail_budget == 0)
            check("成功批只入账 1 次", len(server.batches) == n_before + 1)

            print("[6/8] 持续 5xx：重试耗尽终止、偏移不动、下轮续传")
            server.fail_budget = 10
            write_lines(file_a, [fake_event()])
            off_before = load_state(state).get("events-20260701.jsonl")
            s = run(spool_dir=spool, batch=200)
            check("失败退出 ok=False 且带错误信息", not s["ok"] and bool(s["error"]))
            check("偏移未推进", load_state(state).get("events-20260701.jsonl") == off_before)
            server.fail_budget = 0
            s = run(spool_dir=spool, batch=200)
            check("恢复后续传成功 accepted=1", s["ok"] and s["accepted"] == 1)

            print("[7/8] dry-run：不联网、不写 state")
            write_lines(file_a, [fake_event()])
            n_before = len(server.batches)
            state_raw = open(state, "rb").read()
            s = run(spool_dir=spool, batch=200, dry_run=True)
            check("dry-run 统计到 1 行待传", s["ok"] and s["lines"] == 1)
            check("mock 未收到请求且 state 未变",
                  len(server.batches) == n_before and open(state, "rb").read() == state_raw)
            s = run(spool_dir=spool, batch=200)
            check("随后真传成功 accepted=1", s["ok"] and s["accepted"] == 1)

            print("[8/8] 错误密钥：401 配置错，不重试、偏移不动")
            write_lines(file_a, [fake_event()])
            off_before = load_state(state).get("events-20260701.jsonl")
            s = run_upload(spool_dir=spool, endpoint=endpoint, key="wrong-key",
                           batch=200, backoff_base=0.05, quiet=True)
            check("ok=False 且错误含 HTTP 401", not s["ok"] and "401" in (s["error"] or ""))
            check("偏移未推进", load_state(state).get("events-20260701.jsonl") == off_before)
            s = run(spool_dir=spool, batch=200)
            check("换对密钥后续传成功 accepted=1", s["ok"] and s["accepted"] == 1)
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: "list[str]") -> int:
    p = argparse.ArgumentParser(
        prog="uploader.py",
        description="无界 spool 事件补传器：断点续读 events-*.jsonl 批量 POST 到集团收集器 /api/collect。",
    )
    p.add_argument("--spool-dir", default=None,
                   help=f"spool 目录（默认 env {SPOOL_ENV_VAR} 或 {DEFAULT_SPOOL_DIR}）")
    p.add_argument("--endpoint", default=None,
                   help="收集器地址，如 https://bd2026.cc/api/collect")
    p.add_argument("--key", default=None,
                   help=f"上报密钥（默认 env {KEY_ENV_VAR}）")
    p.add_argument("--state-file", default=None,
                   help=f"断点游标文件（默认 <spool 目录>/{STATE_FILENAME}）")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                   help=f"每批条数（默认 {DEFAULT_BATCH}，上限 {MAX_BATCH}）")
    p.add_argument("--source", default=None,
                   help="上报来源标识 X-Event-Source（默认本机 hostname）")
    p.add_argument("--dry-run", action="store_true", help="只统计将上传的行数，不联网不写 state")
    p.add_argument("--selftest", action="store_true", help="本地 mock 收集器自测（不连外网）")
    args = p.parse_args(argv)

    if args.selftest:
        return _selftest()

    spool_dir = args.spool_dir or os.environ.get(SPOOL_ENV_VAR, "").strip() or DEFAULT_SPOOL_DIR
    key = args.key or os.environ.get(KEY_ENV_VAR, "").strip()
    if not args.dry_run:
        if not args.endpoint:
            p.error("--endpoint 必填（或改用 --dry-run / --selftest）")
        if not key:
            p.error(f"--key 必填（或设置环境变量 {KEY_ENV_VAR}）")

    summary = run_upload(
        spool_dir=spool_dir, endpoint=args.endpoint, key=key,
        state_file=args.state_file, batch=args.batch,
        dry_run=args.dry_run, source=args.source,
    )
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    # Windows 下 stdout 重定向时默认本地代码页（cp936 等），打中文会炸，统一 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main(sys.argv[1:]))
