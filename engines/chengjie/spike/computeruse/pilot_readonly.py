# -*- coding: utf-8 -*-
"""P4 第一步：browser-use 只读试点（2026-08-29）。

## 这个 spike 要回答什么

老板定的方向是「小智能帮用户操作电脑」，并明确「最快的方法不是我们重新写，
是使用别人做好的源码直接调用」。那么第一步要验证的**不是**能不能写出一个
agent，而是三件事实：

  1. 现成的 browser-use（0.13.8，MIT）能不能在本机跑起来；
  2. **能不能用我们局域网的模型驱动它**（老板指定 173:8001 chatx）——这决定
     这条路的边际成本是「零」还是「每次任务都烧云端 token」；
  3. 它的输出可不可验证——agent 说「查到了 X」，我们能不能证明 X 是真的。

第 3 点是这个 spike 最重要的设计：任务刻意选了**答案我们已经知道**的那种
（官网列了哪些聊天渠道），所以 agent 的回答能被逐项核对。选一个我们不知道
答案的任务，跑通了也只是「它说了些话」，证明不了任何东西。

## 刻意的限制（试点边界）

- **只读**：只允许访问公开页面，不登录、不填表、不点提交。真正的写操作要等
  P4 的确认/撤销框架就位（复用 XZAgent 已有的两阶段确认 + 审计，而不是另造）。
- **独立环境**：本目录自带 .venv，不进生产依赖树——browser-use 拉了 30+ 个包，
  混进生产会把重启风险和它绑在一起。
- 不装 litellm：0.12.5 起它已被移出核心依赖（1.82.7/1.82.8 供应链攻击），
  用 ChatOpenAI 直连即可，不必把那条链请回来。

用法：
    .venv\\Scripts\\python.exe pilot_readonly.py            # 局域网模型
    .venv\\Scripts\\python.exe pilot_readonly.py --cloud    # 云端对照组
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

# 只读实例配置取 LLM 端点，绝不把 key 打印出来
CFG = Path(r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml")

# 官网公开信息里应该出现的渠道（与 src/assistant/product_facts.py 同源）。
# 这就是「可验证」的锚：agent 的回答按这张表逐项核对。
EXPECT_CHANNELS = ("Telegram", "WhatsApp", "LINE", "Messenger")
TASK = (
    "打开 https://bd2026.cc/ ，找出这个产品的智聊/ChatX 能对接哪些聊天平台或"
    "渠道。只需要浏览和阅读，不要点击任何表单提交、注册或购买按钮。"
    "把你找到的平台名称列出来。"
)


def _load_llm_cfg(use_cloud: bool) -> dict:
    import yaml

    cfg = yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    ai = cfg.get("ai") or {}
    if use_cloud:
        return {"base_url": str(ai.get("base_url") or ""),
                "model": str(ai.get("model") or ""),
                "api_key": str(ai.get("api_key") or ""), "lan": False}
    fb = ai.get("fallback") or {}
    return {"base_url": str(fb.get("base_url") or ""),
            "model": str(fb.get("model") or ""),
            "api_key": str(fb.get("api_key") or "vllm"), "lan": True}


class _ThinkingOffTransport(httpx.AsyncHTTPTransport):
    """在传输层给 /chat/completions 注入 `enable_thinking: false`。

    为什么必须在这一层做：browser-use 的 ChatOpenAI **没有 extra_body 参数**
    （它只暴露 temperature/reasoning_effort 那一类 OpenAI 官方字段），而
    chatx（Qwen3.6-27B-abl-AWQ）在 vLLM 上默认开 thinking → `message.content`
    恒为 null、正文全进 reasoning、预算被思考吃光。同一个坑今天已经咬过两次
    （LAN 翻译兜底静默失效、小智 docless 轮），所以这里直接在唯一出口处钉死。
    它只认这一个 vLLM 专有键，云端与 Ollama 端点对未知字段一律忽略，同发无害。
    """

    async def handle_async_request(self, request: httpx.Request):
        if request.url.path.endswith("/chat/completions"):
            try:
                body = json.loads(request.content or b"{}")
                kw = body.setdefault("chat_template_kwargs", {})
                kw["enable_thinking"] = False
                request = httpx.Request(
                    request.method, request.url,
                    headers={k: v for k, v in request.headers.items()
                             if k.lower() != "content-length"},
                    content=json.dumps(body).encode("utf-8"),
                    extensions=request.extensions,
                )
            except Exception:
                pass  # 注入失败就按原样发，不因为一个优化把整条链弄死
        return await super().handle_async_request(request)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", action="store_true",
                    help="用云端主链当对照组（默认走局域网无审查模型）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--steps", type=int, default=12, help="最大步数上限")
    args = ap.parse_args()

    lc = _load_llm_cfg(args.cloud)
    if not lc["base_url"] or not lc["model"]:
        print("配置里没有可用的 LLM 端点")
        return 2
    print(f"[llm] {'LAN' if lc['lan'] else 'CLOUD'} {lc['base_url']} "
          f"model={lc['model']}")
    print(f"[task] {TASK[:60]}...")

    from browser_use import Agent, ChatOpenAI

    # 遥测关掉：这是内网试点，不该把任务内容送第三方
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

    llm = ChatOpenAI(
        model=lc["model"],
        base_url=lc["base_url"],
        api_key=lc["api_key"],
        temperature=0.1,
        # 本地 27B 对 OpenAI 的强制 structured output 支持不确定；把 schema
        # 放进 system prompt 是官方给非 OpenAI 后端的兼容档。
        dont_force_structured_output=bool(lc["lan"]),
        add_schema_to_system_prompt=bool(lc["lan"]),
        http_client=(httpx.AsyncClient(transport=_ThinkingOffTransport(),
                                       timeout=120.0)
                     if lc["lan"] else None),
    )

    t0 = time.time()
    agent = Agent(task=TASK, llm=llm)
    try:
        history = await agent.run(max_steps=args.steps)
    except Exception as exc:
        print(f"[FAIL] agent 抛错：{type(exc).__name__}: {exc}")
        return 1
    dur = time.time() - t0

    final = ""
    try:
        final = str(history.final_result() or "")
    except Exception:
        pass
    steps = 0
    try:
        steps = len(history.history)
    except Exception:
        pass

    print(f"\n[result] {dur:.1f}s, {steps} 步")
    print(f"[answer] {final[:600]}")

    # ── 可验证性判定：把回答按已知锚点逐项核对
    hit = [c for c in EXPECT_CHANNELS if c.lower() in final.lower()]
    miss = [c for c in EXPECT_CHANNELS if c.lower() not in final.lower()]
    print(f"\n[verify] 命中 {len(hit)}/{len(EXPECT_CHANNELS)}: {hit}")
    if miss:
        print(f"[verify] 未提及: {miss}")
    ok = len(hit) >= 3          # 4 个里认出 3 个即视为「真的读懂了页面」
    print(f"\nVERDICT {'PASS' if ok else 'FAIL'} "
          f"({'链路可用且输出可核对' if ok else '链路通但内容不可信/没读到'})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
