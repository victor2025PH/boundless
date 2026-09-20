"""本地化交付端到端验收（P2 收尾）：开通剖面 → 配置合并 → 就绪判定 → 真出话。

为什么不是「拉起一个完整实例」：验收目标是**本地档特有链路**（开通器 overlay →
ConfigManager 深合并 → golive/config_check 本地档判定 → AIClient local_only
初始化探针 → 本地主链真出话）。web 服务/登录属通用实例机理，已有
verify_instance.ps1 与生产实例覆盖；为验收再起一个常驻进程既吃端口又违反
「生产机仅一条线动实例进程」纪律。本工具零常驻、零 stack.json 污染、
临时数据根用后即删。

用法：
  python tools/verify_local_first_delivery.py            # 离线段（零网络零 GPU）
  python tools/verify_local_first_delivery.py --live     # + 打真实本地 LLM 端点出话
  python tools/verify_local_first_delivery.py --live --base http://192.168.0.173:8001/v1 \
      --model chatx

约定：环境缺失（--live 端点不通/模型未备）一律 SKIP exit 0（gate 信号不被
「主机当时没开」污染）；断言失败 exit 1。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 数据路径家族在 import 期定位：先指到进程级 tmp，杜绝从引擎根跑时
# persona_usage/telemetry 等模块按 CWD 回落写进仓库（conftest 同款隔离）。
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="localfirst_accept_"))
os.environ.setdefault("AITR_DATA_DIR", str(_TMP_ROOT / "aitr_data"))

_PASS = "PASS"
_FAIL = "FAIL"


def _say(step: str, status: str, detail: str = "") -> None:
    print(f"[{status}] {step}" + (f"  — {detail}" if detail else ""), flush=True)


def _fail(step: str, detail: str) -> "SystemExit":
    _say(step, _FAIL, detail)
    return SystemExit(1)


async def _run(args: argparse.Namespace) -> int:
    from src.ops.instance_provisioner import plan_instance, render_overlay
    from src.utils.config_check import check_config
    from src.utils.config_manager import ConfigManager
    from src.utils.golive import build_checklist

    # ── 1) 开通剖面渲染 + 落临时数据根（与 provision --apply 同布局）────────
    plan = plan_instance({"services": []}, product="zhiliao",
                         customer="localfirst-acceptance",
                         data_base=str(_TMP_ROOT))
    overlay = render_overlay(
        plan, ai_primary="local_only",
        local_llm_base_url=args.base, local_llm_model=args.model)
    cfg_dir = Path(plan.data_dir) / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    example = BASE / "config" / "config.example.yaml"
    if not example.exists():
        raise _fail("provision-render", f"缺 {example}（仓库不完整）")
    shutil.copy2(example, cfg_dir / "config.yaml")
    (cfg_dir / "config.local.yaml").write_text(overlay, encoding="utf-8")
    _say("provision-render", _PASS, f"data_dir={plan.data_dir}")

    # ── 2) ConfigManager 真实加载（主配置 + overlay 深合并）────────────────
    cm = ConfigManager(str(cfg_dir / "config.yaml"))
    await cm.load()   # 占位凭证下可返回 False（main.py 同样忽略）；只断言合并内容
    ai = (cm.config or {}).get("ai") or {}
    fb = ai.get("fallback") or {}
    if str(ai.get("primary")) != "local_only":
        raise _fail("config-merge", f"ai.primary={ai.get('primary')!r}（期望 local_only）")
    if not (fb.get("enabled") and fb.get("base_url") == args.base
            and fb.get("model") == args.model):
        raise _fail("config-merge", f"ai.fallback 合并失真: {fb!r}")
    _say("config-merge", _PASS, f"primary=local_only model={fb.get('model')}")

    # ── 3) golive 清单：AI 项须绿（云 key 仍是 example 占位）────────────────
    out = build_checklist(
        config=cm.config,
        channel_statuses=[{"id": "telegram", "name": "Telegram", "ready": True}],
        config_errors=0, config_warnings=0,
        kb_ready={"available": True, "is_cold": False, "enabled_entries": 30},
        online_agents=1)
    ai_check = next(c for c in out["checks"] if c["id"] == "ai")
    if ai_check["status"] != "ok":
        raise _fail("golive-ai-check", f"{ai_check['status']}: {ai_check['detail']}")
    _say("golive-ai-check", _PASS, ai_check["detail"])

    # ── 4) config_check：ai.* 零 error、云 key 不再被劝填 ───────────────────
    issues = check_config(cm.config, config_path=str(cfg_dir / "config.yaml"))
    ai_errs = [i for i in issues if i.path.startswith("ai") and i.severity == "error"]
    key_nags = [i for i in issues if i.path == "ai.api_key"]
    if ai_errs:
        raise _fail("config-check", "; ".join(f"{i.path}: {i.message}" for i in ai_errs))
    if key_nags:
        raise _fail("config-check", "local_only 仍在劝填云 key（应跳过）")
    _say("config-check", _PASS, "ai.* 0 error，云 key 零劝填")

    if not args.live:
        _say("live", "SKIP", "未加 --live（离线段全过）")
        return 0

    # ── 5) --live：端点可达性预检（缺环境 SKIP，不污染 gate 信号）───────────
    import httpx
    root = args.base.rstrip("/")
    openai = root.endswith("/v1") or ":8001" in root
    v1 = root if root.endswith("/v1") else (root + "/v1" if openai else root)
    try:
        async with httpx.AsyncClient(timeout=3.0) as hc:
            if openai:
                tags = (await hc.get(f"{v1}/models")).json()
                names = [str(m.get("id") or "") for m in (tags.get("data") or [])]
            else:
                tags = (await hc.get(f"{root}/api/tags")).json()
                names = [str(m.get("name") or "") for m in (tags.get("models") or [])]
    except Exception as e:
        _say("live", "SKIP", f"本地端点不可达（{e!r}）——环境缺失非验收失败")
        return 0
    # 只认精确 tag（含 :latest 别名）。首版前缀匹配踩过坑：176 有 qwen3-vl/qwen3:32b
    # 时 "qwen3" 前缀误放行，跑到 /api/chat 才 404 —— 预检必须与真实调用同名。
    if not any(n == args.model or n == f"{args.model}:latest" or n.endswith("/" + args.model)
               for n in names):
        _say("live", "SKIP",
             f"端点无模型 {args.model}（在位: {', '.join(names[:6])}…）")
        return 0

    # ── 5b) --live：显式预热。vLLM 27B 常驻毫秒级；旧 ollama 冷载曾 ~69s。
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=float(args.warmup_timeout)) as hc:
            if openai:
                wr = await hc.post(f"{v1}/chat/completions", json={
                    "model": args.model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8, "temperature": 0,
                    "chat_template_kwargs": {"enable_thinking": False}})
            else:
                wr = await hc.post(f"{root}/api/chat", json={
                    "model": args.model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False, "think": False,
                    "options": {"num_predict": 8}, "keep_alive": "30m"})
            wr.raise_for_status()
    except Exception as e:
        raise _fail("live-warmup", f"模型在册但 chat 拉不起: {e!r}")
    warm_dt = time.time() - t0
    _say("live-warmup", _PASS,
         f"冷载 {warm_dt:.1f}s" if warm_dt > 5 else f"热态 {warm_dt:.1f}s")

    # ── 6) --live：AIClient local_only 初始化 + 本地主链真出话 ──────────────
    from src.ai.ai_client import AIClient
    from src.ai.llm_cost import get_llm_cost
    client = AIClient(cm)
    if not await client.initialize():
        raise _fail("live-initialize", "local_only 初始化失败（探针走本地端点，应成功）")
    if client._primary_mode != "local_only":
        raise _fail("live-initialize", f"运行时 primary={client._primary_mode}")
    _say("live-initialize", _PASS, "启动探针经本地端点通过（云 key 占位不阻断）")

    t0 = time.time()
    reply = await client.generate_reply(
        "请用一句话介绍你自己。", context={"reply_lang": "zh"})
    dt = time.time() - t0
    if not (reply or "").strip():
        raise _fail("live-reply", "空回复")
    snap = client.degradation_snapshot()
    stats = client.get_stats()
    tiers = {str(r.get("tier")): r for r in (get_llm_cost().dump().get("rows") or [])}
    lp = tiers.get("local_primary") or {}
    if not int(lp.get("calls") or 0):
        raise _fail("live-reply", f"llm_cost 无 local_primary 行（tiers={list(tiers)}）——"
                    "回复可能来自 canned/云端")
    if snap.get("degraded") is not False:
        raise _fail("live-reply", f"本地转正被误报降级: {snap}")
    _say("live-reply", _PASS,
         f"{dt:.1f}s tokens={int(lp.get('prompt_tokens') or 0)}+"
         f"{int(lp.get('completion_tokens') or 0)} "
         f"snapshot=primary/{snap.get('primary')} 「{(reply or '').strip()[:60]}」")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地化交付端到端验收（P2）")
    ap.add_argument("--live", action="store_true", help="打真实本地 LLM 端点出话")
    # 默认对准 .173 vLLM chatx（Qwen3-27B AWQ，与生产 zhiliao ai.fallback 同址同模型）。
    ap.add_argument("--base", default="http://192.168.0.173:8001/v1",
                    help="本地 LLM 端点（默认 LAN .173 vLLM chatx 27B）")
    ap.add_argument("--model", default="chatx")
    ap.add_argument("--warmup-timeout", type=float, default=60.0,
                    help="预热步骤超时秒（vLLM 常驻应秒回；旧 ollama 冷载才需要更长）")
    ap.add_argument("--keep", action="store_true", help="保留临时数据根供排查")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    finally:
        if not args.keep:
            shutil.rmtree(_TMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
