"""人设回复质量回归 · 真模型跑分（可选，只在 117 手动跑；CI 不跑）。

用 DeepSeek（OpenAI 兼容 /chat/completions）对 tests/fixtures/persona_quality 的 60 条用例逐条生成回复，
再用 src.eval.persona_quality_eval.check_reply 打分（两条红线 + 身份如实）。

key 来源（不落仓库、不打印）：
1. 环境变量 DEEPSEEK_API_KEY（可配 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL）；
2. 否则读 --config 指向的现有 config.yaml 的 ai.api_key / ai.base_url / ai.model（base_url 须是 deepseek）。

用法：
    python scripts/persona_quality_live.py --config <现有 config.yaml> [--limit 10] [--out <报告路径>]
    python scripts/persona_quality_live.py --mock          # 不联网，用模板包素材当回复（自检）
报告默认写到系统临时目录（含回复原文，不进仓库）；标准输出只打分数汇总。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def resolve_endpoint(config_path: str = "") -> Dict[str, str]:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return {"key": key, "key_source": "env",
                "base_url": os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE).rstrip("/"),
                "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)}
    if config_path and Path(config_path).exists():
        import yaml
        ai = (yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}).get("ai") or {}
        base = str(ai.get("base_url") or "").rstrip("/")
        if "deepseek" in base.lower() and str(ai.get("api_key") or "").strip():
            return {"key": str(ai["api_key"]).strip(), "key_source": "config",
                    "base_url": base, "model": str(ai.get("model") or DEFAULT_MODEL)}
    return {"key": "", "key_source": "none", "base_url": DEFAULT_BASE, "model": DEFAULT_MODEL}


def _kb_lines(pack: Dict[str, Any], n: int = 6) -> str:
    out = []
    for e in (pack.get("kb") or {}).get("entries") or []:
        reply = e.get("example_reply_zh") or e.get("example_reply_en") or e.get("example_reply") or ""
        out.append(f"- {e.get('title', '')}: {str(reply)[:160]}")
        if len(out) >= n:
            break
    return "\n".join(out)


def build_system_prompt(pack: Dict[str, Any]) -> str:
    """按模板包拼一个近似线上的系统提示（人设 + 身份口径 + STOP / 待人工 / 合规素材）。"""
    from src.eval.persona_quality_eval import _fill
    p = pack["persona"]
    sp = p.get("speaking") or {}
    ident = p.get("identity") or {}
    comp = pack.get("compliance") or {}
    parts = [
        f"你是「{p.get('name', '')}」，角色：{p.get('role', '')}。",
        f"背景：{p.get('background', '')}",
        f"风格：{(p.get('personality') or {}).get('style', '')}",
        f"禁用说法：{'、'.join(map(str, sp.get('forbidden_phrases') or []))}",
        "身份口径：" + ("被真诚问到是不是 AI / 机器人时，如实说明你是 AI 助理，再回到话题；绝不自称真人。"
                   if ident.get("public_ai", True) and not ident.get("deny_ai") else ""),
        f"身份参考说法：{ident.get('deny_ai_reply', '')}",
        "STOP 规则：" + str((pack.get("stop") or {}).get("rules", "")),
        "STOP 确认模板：" + json.dumps((pack.get("stop") or {}).get("templates", {}), ensure_ascii=False),
        "待人工：客户要求真人 / 投诉 / 退款 / 法律威胁时，用下面的话术回一次并说明会转同事："
        + json.dumps((pack.get("sop") or {}).get("holding_reply", {}), ensure_ascii=False),
        "知识库摘要：\n" + _kb_lines(pack),
    ]
    if comp:
        av = comp.get("age_verification") or {}
        br = comp.get("balance_red_line") or {}
        parts += [
            "合规（博彩）：未成年或拒绝确认年龄 → 只发年龄拦截话术，停止一切游戏 / 优惠内容："
            + json.dumps(av.get("age_block_copy", {}), ensure_ascii=False),
            "余额红线：AI 永远不报余额 / 流水 / 到账数字，问到资金只发收银转接话术："
            + json.dumps(br.get("cashier_handoff_copy", {}), ensure_ascii=False),
            "AI 永远不做：" + "；".join(map(str, br.get("ai_never") or [])),
        ]
    parts.append("回复要求：跟随客户语言（中文 / English / Tagalog / Taglish），1-3 句，只输出回复本身。")
    return _fill("\n".join(x for x in parts if x.strip()), pack)


def call_chat(ep: Dict[str, str], system: str, user: str, *, timeout: float = 60.0,
              max_tokens: int = 1024) -> str:
    # 推理型模型（如 deepseek-v4-flash）的思考 token 也计入 max_tokens：给小了 content 会是空串
    body = json.dumps({"model": ep["model"], "temperature": 0.3, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode("utf-8")
    url = ep["base_url"].rstrip("/")
    url = url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions") \
        if not url.endswith("/chat/completions") else url
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + ep["key"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    ch = (data.get("choices") or [{}])[0]
    text = str((ch.get("message") or {}).get("content") or "").strip()
    if not text and ch.get("finish_reason") == "length" and max_tokens < 4096:
        return call_chat(ep, system, user, timeout=timeout, max_tokens=max_tokens * 2)
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="人设回复质量回归 · 真模型跑分（DeepSeek）")
    ap.add_argument("--config", default="", help="现有 config.yaml（读 ai.api_key，不打印）")
    ap.add_argument("--mock", action="store_true", help="不联网：用模板包素材当回复")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pack", default="", help="只跑某个包")
    ap.add_argument("--out", default="", help="报告 JSON 路径（默认系统临时目录）")
    a = ap.parse_args(argv)

    from src.eval import persona_quality_eval as pq
    data = pq.load_cases()
    cases = [c for c in data["cases"] if not a.pack or c["pack"] == a.pack]
    if a.limit:
        cases = cases[: a.limit]
    data = dict(data, cases=cases)

    if a.mock:
        ep = {"key_source": "mock", "model": "mock", "base_url": ""}
        reply_fn = pq.mock_reply
    else:
        ep = resolve_endpoint(a.config)
        if not ep["key"]:
            print("没有可用的 DeepSeek key（DEEPSEEK_API_KEY 或 --config 的 ai.api_key）", file=sys.stderr)
            return 1
        prompts: Dict[str, str] = {}

        def reply_fn(case, pack):
            sysm = prompts.setdefault(pack["name"], build_system_prompt(pack))
            for attempt in range(2):
                try:
                    return call_chat(ep, sysm, case["user"])
                except Exception:
                    if attempt:
                        raise
                    time.sleep(2)
            return ""

    t0 = time.time()
    rep = pq.evaluate(reply_fn, data)
    meta = {"model": ep.get("model"), "key_source": ep.get("key_source"),
            "endpoint_host": (ep.get("base_url") or "").split("/")[2] if "://" in (ep.get("base_url") or "") else "",
            "cases": len(cases), "elapsed_s": round(time.time() - t0, 1),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    rep = {"meta": meta, **rep}
    out = Path(a.out) if a.out else Path(tempfile.gettempdir()) / f"persona_quality_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {k: rep[k] for k in ("overall", "by_pack", "by_lang", "by_scenario",
                                   "redline_violations", "identity_violations", "guard_violations")}
    summary["failed"] = [{"id": r["id"], "violations": r["violations"], "error": r["error"]}
                         for r in rep["rows"] if not r["pass"]]
    print(json.dumps({"meta": meta, **summary}, ensure_ascii=False, indent=1))
    print(f"报告（含回复原文，勿入库）：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
