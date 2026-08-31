# -*- coding: utf-8 -*-
"""小智对话回归评测：真语料 + 真 LLM + 真路由，端到端问答并按金标判定。

实施93 发版夜（2026-09-01）老板指令「每个新功能小智必须能正常引导 + 不断
测试自我迭代」的机制化落点之一（另一半是 tools/xiaozhi_gap_report.py 的
真实缺口清单）。首夜实测价值：发版前 5/8 拒答 → 补语料后 8/8 可引导。

跑法（117 引擎根）：
    python tools/xiaozhi_dialog_eval.py            # 全量金标 + 追加趋势行
    python tools/xiaozhi_dialog_eval.py --json     # 机器可读
    python tools/xiaozhi_dialog_eval.py --no-trend # 不写趋势（手工试跑）

判定四态见 config/eval/xiaozhi_dialog_questions.yaml 头注。趋势行追加
logs/eval/xiaozhi_dialog_trend.jsonl（与 translation_trend 同哲学：重启
不清零、周环比可读）。缺 LLM 配置/网络不可达 → SKIP exit 0（不污染
回归信号）；语料永远从**当前代码**构建（build_all_entries），评的是
「下一个包出货的语料」而非线上库存——发版前跑=预验收。

刻意不进 pytest：真 LLM 有网络与 token 成本，CI 禁；检索半边已由
tests/test_assistant_qa_eval.py 常驻把守。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))

_REFUSAL_MARK = "没有找到可靠依据"


def _resolve(v: str, fallback: str) -> str:
    v = str(v or "").strip()
    return fallback if v in ("", "inherit") else v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(
        _ENGINE_ROOT / "config" / "eval" / "xiaozhi_dialog_questions.yaml"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-trend", action="store_true")
    ap.add_argument("--tags", default="", help="逗号分隔，只跑这些 tag")
    args = ap.parse_args()

    import yaml

    qs_doc = yaml.safe_load(io.open(args.questions, encoding="utf-8")) or {}
    questions = [q for q in (qs_doc.get("questions") or []) if q.get("q")]
    if args.tags:
        want = {t.strip() for t in args.tags.split(",") if t.strip()}
        questions = [q for q in questions if str(q.get("tag")) in want]
    if not questions:
        print("SKIP: no questions")
        return 0

    # ── 数据根隔离必须在业务 import 之前（qa_log/help_kb 走 config_dir 契约）──
    tmp = tempfile.mkdtemp(prefix="xz_dialog_eval_")
    os.environ["AITR_DATA_DIR"] = tmp
    os.environ.pop("AITR_CONFIG_PATH", None)

    # LLM 配置来自当前活跃实例（含 overlay；inherit 三键同源解析与路由一致）
    from src.eval.eval_config import load_runtime_config

    runtime_cfg = load_runtime_config() or {}
    llm_raw = ((runtime_cfg.get("assistant") or {}).get("query") or {}).get(
        "llm") or {}
    ai_cfg = runtime_cfg.get("ai") or {}
    llm = {
        "base_url": _resolve(llm_raw.get("base_url"),
                             str(ai_cfg.get("base_url") or "")),
        "model": _resolve(llm_raw.get("model"), str(ai_cfg.get("model") or "")),
        "api_key": _resolve(llm_raw.get("api_key"),
                            str(ai_cfg.get("api_key") or "")),
    }
    if not (llm["base_url"] and llm["model"] and llm["api_key"]) \
            or "YOUR_API" in llm["api_key"]:
        print("SKIP: assistant/ai llm 未配置（无 key 或占位符）")
        return 0

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from src.assistant.help_kb import get_help_kb
    from src.assistant.seed_corpus import build_all_entries
    from src.web.routes.assistant_routes import register_assistant_routes

    entries = build_all_entries()
    get_help_kb().upsert_entries(entries)

    app = FastAPI()

    @app.middleware("http")
    async def _fake_login(request: Request, call_next):  # noqa: ANN001
        request.session["user_id"] = "dialog-eval"
        request.session["username"] = "dialog-eval"
        request.session["role"] = "master"
        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key="dialog-eval")

    import types

    register_assistant_routes(app, types.SimpleNamespace(
        api_auth=lambda request: None,
        config_manager=types.SimpleNamespace(config={
            "assistant": {"enabled": True, "name": "小智",
                          "query": {"llm": llm, "rate_per_min": 60,
                                    "rate_per_day": 500},
                          "report": {"enabled": False}},
            "ai": {},
        }),
        telegram_client=None,
    ))
    client = TestClient(app)

    rows = []
    for item in questions:
        tag, q = str(item.get("tag") or "?"), str(item["q"])
        must_any = [str(x) for x in (item.get("must_any") or [])]
        srcs: list = []
        parts: list[str] = []
        t0 = time.time()
        try:
            with client.stream("POST", "/api/assistant/query",
                               json={"q": q, "lang": "zh",
                                     "page": "/workspace"}) as r:
                for line in r.iter_lines():
                    line = (line or "").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    t = str(ev.get("ev") or "")
                    if t == "meta":
                        srcs = ev.get("sources") or []
                    elif t == "delta":
                        parts.append(str(ev.get("text") or ""))
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP: query 链异常（网络/LLM 不可达？）{exc}")
            return 0
        a = "".join(parts).strip()
        refused = _REFUSAL_MARK in a
        hit_kw = (not must_any) or any(k in a for k in must_any)
        if refused:
            verdict = "REFUSED"
        elif not srcs:
            verdict = "NO-SOURCE"
        elif not hit_kw:
            verdict = "WRONG"
        else:
            verdict = "GUIDED"
        rows.append({"tag": tag, "verdict": verdict,
                     "latency_ms": int((time.time() - t0) * 1000),
                     "sources": [s.get("id") for s in srcs
                                 if isinstance(s, dict)][:4],
                     "answer_head": a[:160]})
        if not args.json:
            print(f"[{verdict:9}] {tag:16} {rows[-1]['latency_ms']:>6}ms  "
                  f"{a[:80].replace(chr(10), ' ')}")

    total = len(rows)
    guided = sum(1 for r in rows if r["verdict"] == "GUIDED")
    misses = [r["tag"] for r in rows if r["verdict"] != "GUIDED"]
    summary = {"ts": time.time(), "total": total, "guided": guided,
               "refused": sum(1 for r in rows if r["verdict"] == "REFUSED"),
               "wrong": sum(1 for r in rows if r["verdict"] == "WRONG"),
               "no_source": sum(1 for r in rows if r["verdict"] == "NO-SOURCE"),
               "misses": misses}
    if args.json:
        print(json.dumps({"summary": summary, "rows": rows},
                         ensure_ascii=False, indent=1))
    else:
        print(f"\n== guided {guided}/{total}"
              + (f"  misses={misses}" if misses else "  (全绿)"))
    if not args.no_trend:
        trend = _ENGINE_ROOT / "logs" / "eval" / "xiaozhi_dialog_trend.jsonl"
        trend.parent.mkdir(parents=True, exist_ok=True)
        with io.open(trend, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        if not args.json:
            print(f"trend -> {trend}")
    return 0 if guided == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
