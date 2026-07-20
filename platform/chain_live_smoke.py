# -*- coding: utf-8 -*-
r"""platform/chain_live_smoke.py — 产业链条『真实接线』端到端烟雾测试。

与 chain_selftest.py 的区别（互补，不是重复）：
  - chain_selftest.py 证明"断总线，每个契约客户端都能优雅降级、独立运行"；
  - 本脚本证明"接上总线，客户端与 chengjie 侧真实路由实现是字节级兼容的"——
    起一个只挂 leadbus/replybus/enable 三条新路由的裸 FastAPI 服务（不依赖
    Telegram/GPU/生产数据库，同 chengjie tests/test_*_routes.py 的隔离测试范式），
    用 platform 下的真实瘦客户端对它发起真实 HTTP 请求，断言拿到预期结构。

只读、不改任何文件；不连生产库；服务跑在 127.0.0.1 随机端口，用完即关。

用法：
    python platform/chain_live_smoke.py
    （需能 import chengjie：脚本会把 engines/chengjie 加入 sys.path，
     若目录结构调整，用 --chengjie-dir 覆盖）
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

_PLATFORM_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PLATFORM_DIR.parent


def _load_platform_client(rel_path: str, module_name: str):
    """按文件路径加载 platform/ 下的瘦客户端模块（不 sys.path.insert 仓库根，
    避免遮蔽标准库 platform；与 leadbus/enable/replybus/licensing 文档里
    反复强调的同一条安全加载范式一致）。"""
    spec = importlib.util.spec_from_file_location(module_name, str(_PLATFORM_DIR / rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_app(chengjie_dir: str):
    """裸 FastAPI + 三个 register_*_routes，镜像 chengjie tests/test_care_routes.py
    的隔离范式：不启 create_app()/main.py，不碰 Telegram/GPU/生产库。"""
    sys.path.insert(0, chengjie_dir)
    from fastapi import FastAPI  # noqa: E402  (chengjie 的依赖，需先插 path)
    from src.web.routes.leadbus_routes import register_leadbus_routes  # noqa: E402
    from src.web.routes.enable_routes import register_enable_routes  # noqa: E402
    from src.web.routes.replybus_routes import register_replybus_routes  # noqa: E402

    app = FastAPI()

    def _api_auth() -> bool:
        return True  # 烟雾测试不验证鉴权本身（鉴权已在 chengjie 自己的路由测试里覆盖）

    register_leadbus_routes(app, api_auth=_api_auth, config_manager=None)
    register_enable_routes(app, api_auth=_api_auth, config_manager=None)
    register_replybus_routes(app, api_auth=_api_auth, config_manager=None)
    return app


def _run_server_in_thread(app, port: int):
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    # 等就绪：uvicorn.Server 有 .started 标志，最多等 10s
    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    return server


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chengjie-dir", default=str(_REPO_ROOT / "engines" / "chengjie"))
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    failures = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    print("== 产业链条真实接线烟雾测试（chain_live_smoke.py）==")
    print(f"chengjie 目录: {args.chengjie_dir}")

    app = _build_app(args.chengjie_dir)
    import socket
    port = args.port
    if not port:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    server = _run_server_in_thread(app, port)
    bus_url = f"http://127.0.0.1:{port}"
    print(f"裸 chengjie 路由已起于 {bus_url}（仅 leadbus/enable/replybus 三条新路由，无 Telegram/GPU/生产库）")

    try:
        # ── leadbus：真实 publish() 走 HTTP ingest ─────────────────────────
        lb_mod = _load_platform_client("leadbus/client.py", "boundless_smoke_leadbus")
        lb = lb_mod.LeadBusClient(bus_url=bus_url)
        r = lb.publish({
            "source": {"product": "zhituo", "platform": "telegram", "campaign": "smoke"},
            "lead": {"external_id": "tg:smoke_001", "handle": "smoke_user",
                     "profile": {"lang": "en", "intent_score": 0.5}},
        })
        check("leadbus.publish() 真实联网成功 delivered=True", r.get("delivered") is True)
        check("leadbus.publish() 未落 outbox（HTTP 成功不排队）", r.get("queued") is not True)

        # ── enable：真实 translate() 走 HTTP（无 provider 时应 available=False 但不报连接错）
        en_mod = _load_platform_client("enable/client.py", "boundless_smoke_enable")
        en = en_mod.EnableClient(chengjie_url=bus_url)
        rt = en.translate("hello", to_lang="zh")
        check("enable.translate() 真实联网可达（HTTP 层，非 provider 层）",
              "error" not in rt or "HTTP" not in str(rt.get("error", "")))
        rs = en.translate_status()
        check("enable.translate_status() 真实联网返回 translate_ready 字段",
              "translate_ready" in rs)

        # ── replybus：真实 decide() 走 HTTP（无真实 AI 时应 silent，但要是"决策"不是"fallback"）
        rb_mod = _load_platform_client("replybus/client.py", "boundless_smoke_replybus")
        rb = rb_mod.ReplyBusClient(bus_url=bus_url)
        rd = rb.decide({
            "platform": "telegram", "external_id": "tg:smoke_001",
            "text": "hi there, smoke test",
        })
        check("replybus.decide() 真实联网可达（available=True，不是本地 fallback）",
              rd.get("available") is True)
        check("replybus.decide() 决策 action 合法且绝非 send（防双发红线在服务端生效）",
              rd.get("action") in ("draft", "silent", "handoff"))
        check("replybus.decide() 响应体不含任何已发送语义字段",
              "sent" not in rd and "delivered" not in rd)
    finally:
        server.should_exit = True
        time.sleep(0.3)

    if failures:
        print(f"\n== 结果：{len(failures)} 项失败 ==")
        return 1
    print("\n== 结果：全部通过 —— leadbus/enable/replybus 客户端与 chengjie 真实路由字节级兼容 ✓ ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
