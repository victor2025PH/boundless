# -*- coding: utf-8 -*-
r"""platform/chain_live_smoke_auth.py — 产业链条『真实鉴权』端到端烟雾测试。

与 chain_live_smoke.py 的关系（互补，不是重复，也不修改它）：
  - chain_live_smoke.py 证明"裸服务、零鉴权"场景下 leadbus/enable/replybus 三个瘦客户端
    与 chengjie 真实路由字节级兼容——它注入的 `_api_auth` 永远返回 True，从未真正验证过
    "chengjie 侧确实配置了鉴权（如 web_admin.auth_token）"这条更贴近生产的路径。
  - 本脚本把假 `_api_auth` 换成一个真实校验 `Authorization: Bearer <token>` 的版本（结构
    与生产代码 src/web/admin.py::_api_auth、src/bootstrap/web_app.py::make_api_auth 同构：
    依赖函数形参声明 `request: Request`，路由侧仍是 `Depends(api_auth)` 无参调用，由
    FastAPI 按依赖函数自身签名自动注入 Request——这是标准 FastAPI 行为，本脚本用真实起
    服务 + 真实 HTTP 往返来验证它确实成立，而不是假设它能工作），据此验证第三阶段给
    三个瘦客户端新加的 `auth_token`/`chengjie_token` 构造参数：
      1) 不带 token 的客户端请求应被 401 拒绝，且各客户端按自己的 fail-soft 语义收敛
         （leadbus → queued 待重投；enable → available=False；replybus → action=fallback）；
      2) 带对 token 的客户端请求应被放行，拿到与"零鉴权"场景一致的真实响应。

只读、不改任何文件（包括不改 chain_live_smoke.py 本身——只用安全的按路径加载方式复用它
里面与鉴权无关的基础设施：起服务线程、按路径安全加载 platform/*/client.py）；不连生产库/
Telegram/GPU；服务跑在 127.0.0.1 随机端口，用完即关；leadbus 的 outbox 落盘目录强制指到
临时目录，测试结束随进程清理，不写进仓库 data/ 目录。

用法：
    python platform/chain_live_smoke_auth.py
    （需能 import chengjie：脚本会把 engines/chengjie 加入 sys.path，
     若目录结构调整，用 --chengjie-dir 覆盖）

⚠ 实现踩坑记录（留在这里，避免后人重踩）：本文件*不能*加
`from __future__ import annotations`。原因：`_build_app_with_real_auth()` 里的
`_real_api_auth(request: Request)` 是个嵌套函数，`Request` 是在其外层函数体内
`import` 的局部名字；一旦模块顶部有 `from __future__ import annotations`，这个
参数注解会被存成字符串 `"Request"`，FastAPI 解析依赖签名时只会拿该函数的
`__globals__`（本模块的全局命名空间）去 `eval` 这个字符串——外层函数的局部作用域
（闭包）它看不到——于是解析失败，FastAPI 静默把 `request` 当成一个"必填 query 参数"
而不是特殊的 `Request` 注入，导致真实请求全部先被 422（`{"detail":[{"loc":["query",
"request"],"msg":"Field required"}]}`）挡在鉴权之前，跟 401/token 对错完全无关——
第一版脚本就是这样踩的坑，靠对比"裸 urllib 直连 100% 正常" vs "同一份代码原样跑总是
422"才揪出来。教训：不加该 future import，`request: Request` 的注解就是真的类对象，
FastAPI 直接用，不需要再去 eval 字符串，天然没有这个坑。
"""
import argparse
import importlib.util
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

_PLATFORM_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PLATFORM_DIR.parent

# 模拟"生产已配置鉴权"：唯一合法凭据。纯测试用假 token，不是任何真实密钥。
REQUIRED_TOKEN = "smoke-test-secret-token-12345"


def _load_sibling_module(rel_path: str, module_name: str):
    """按文件路径加载 platform/ 目录下的模块（与 chain_live_smoke.py::_load_platform_client
    同一条安全加载范式：不 sys.path.insert 仓库根，避免遮蔽标准库 platform）。

    用来复用 chain_live_smoke.py 里已经写好、与"鉴权是真是假"无关的基础设施
    （起 uvicorn 服务线程、按路径安全加载 platform/*/client.py 瘦客户端）——
    不需要、也不允许修改那个文件。
    """
    spec = importlib.util.spec_from_file_location(module_name, str(_PLATFORM_DIR / rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_base = _load_sibling_module("chain_live_smoke.py", "boundless_chain_live_smoke_base")


def _build_app_with_real_auth(chengjie_dir: str):
    """裸 FastAPI + 三个 register_*_routes——与 chain_live_smoke.py::_build_app 唯一的
    差异点：`api_auth` 换成真实校验 `Authorization: Bearer <token>` 的版本，而不是永远
    True 的假实现，用来验证"真的配置了鉴权"这条此前没有脚本跑通过的路径。
    """
    sys.path.insert(0, chengjie_dir)
    from fastapi import FastAPI, HTTPException, Request  # noqa: E402  (chengjie 的依赖，需先插 path)
    from src.web.routes.leadbus_routes import register_leadbus_routes  # noqa: E402
    from src.web.routes.enable_routes import register_enable_routes  # noqa: E402
    from src.web.routes.replybus_routes import register_replybus_routes  # noqa: E402

    app = FastAPI()

    def _real_api_auth(request: Request) -> bool:
        """真实模拟 chengjie 生产鉴权：要求 `Authorization: Bearer <REQUIRED_TOKEN>`。
        依赖函数形参声明 `request: Request`——三个路由文件里都是 `Depends(api_auth)`
        无参调用，FastAPI 会按 `api_auth`（此处即本函数）自身的签名自动注入 Request，
        与 src/web/admin.py::_api_auth 的写法同构，属于标准 FastAPI 依赖注入行为。
        """
        auth_header = request.headers.get("Authorization", "")
        expected = f"Bearer {REQUIRED_TOKEN}"
        if auth_header != expected:
            raise HTTPException(status_code=401, detail="unauthorized")
        return True

    register_leadbus_routes(app, api_auth=_real_api_auth, config_manager=None)
    register_enable_routes(app, api_auth=_real_api_auth, config_manager=None)
    register_replybus_routes(app, api_auth=_real_api_auth, config_manager=None)
    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chengjie-dir", default=str(_REPO_ROOT / "engines" / "chengjie"))
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    # 确保"不带 token"场景是真的不带 token：清掉可能让客户端误自带凭据的环境变量
    # （与 chain_selftest.py::_run_one 清空总线相关环境变量同一防御性写法）。
    for var in ("BOUNDLESS_BUS_TOKEN", "CHENGJIE_AUTH_TOKEN"):
        os.environ.pop(var, None)

    failures = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    print("== 产业链条真实鉴权烟雾测试（chain_live_smoke_auth.py）==")
    print(f"chengjie 目录: {args.chengjie_dir}")
    print(f"模拟生产鉴权：要求请求头 Authorization: Bearer {REQUIRED_TOKEN}")

    app = _build_app_with_real_auth(args.chengjie_dir)
    port = args.port
    if not port:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    server = _base._run_server_in_thread(app, port)
    bus_url = f"http://127.0.0.1:{port}"
    print(f"裸 chengjie 路由已起于 {bus_url}（仅 leadbus/enable/replybus 三条新路由，"
          f"挂了真实校验 Authorization 的 api_auth，不是零鉴权假实现）")

    with tempfile.TemporaryDirectory(prefix="boundless_chain_live_smoke_auth_") as outbox_tmp:
        try:
            lb_mod = _base._load_platform_client("leadbus/client.py", "boundless_smoke_auth_leadbus")
            en_mod = _base._load_platform_client("enable/client.py", "boundless_smoke_auth_enable")
            rb_mod = _base._load_platform_client("replybus/client.py", "boundless_smoke_auth_replybus")

            # ══════════ 第一组：不带 token —— 应被 401 拒绝、各自 fail-soft 收敛 ══════════
            print("\n-- 第一组：不带 Authorization 的客户端应被拒绝（各自 fail-soft，绝不抛异常）--")

            lb_no_token = lb_mod.LeadBusClient(bus_url=bus_url, outbox_dir=outbox_tmp)
            r_lb_no = lb_no_token.publish({
                "source": {"product": "zhituo", "platform": "telegram", "campaign": "smoke_auth"},
                "lead": {"external_id": "tg:smoke_auth_no_token", "handle": "smoke_user",
                         "profile": {"lang": "en", "intent_score": 0.5}},
            })
            check("leadbus.publish()（无 token）未被判定已投递：delivered 不是 True",
                  r_lb_no.get("delivered") is not True)
            check("leadbus.publish()（无 token）已 fail-soft 收敛为排队待重投：queued 为 True",
                  r_lb_no.get("queued") is True)
            check("leadbus.publish()（无 token）错误信息确实带 401（证明是鉴权被拒，非其它故障）",
                  "401" in str(r_lb_no.get("error", "")))

            rt_no = en_mod.EnableClient(chengjie_url=bus_url).translate("hello", to_lang="zh")
            check("enable.translate()（无 token）应被拒绝：available 不是 True 或错误带 401",
                  rt_no.get("available") is not True or "401" in str(rt_no.get("error", "")))
            check("enable.translate()（无 token）错误信息确实带 401（非连接失败等其它故障）",
                  "401" in str(rt_no.get("error", "")))
            rs_no = en_mod.EnableClient(chengjie_url=bus_url).translate_status()
            check("enable.translate_status()（无 token）同样应被拒绝：available 不是 True",
                  rs_no.get("available") is not True)

            rb_no_token = rb_mod.ReplyBusClient(bus_url=bus_url)
            rd_no = rb_no_token.decide({
                "platform": "telegram", "external_id": "tg:smoke_auth_no_token",
                "text": "hi there, auth smoke test",
            })
            check("replybus.decide()（无 token）收敛为本地兜底：action == \"fallback\"",
                  rd_no.get("action") == "fallback")
            check("replybus.decide()（无 token）available 为 False（没有真吃到总线决策）",
                  rd_no.get("available") is False)

            # ══════════ 第二组：带正确 token —— 应放行、拿到与零鉴权场景一致的真实响应 ══════════
            print("\n-- 第二组：带正确 Authorization: Bearer <token> 的客户端应放行 --")

            lb_with_token = lb_mod.LeadBusClient(bus_url=bus_url, outbox_dir=outbox_tmp,
                                                 auth_token=REQUIRED_TOKEN)
            r_lb_yes = lb_with_token.publish({
                "source": {"product": "zhituo", "platform": "telegram", "campaign": "smoke_auth"},
                "lead": {"external_id": "tg:smoke_auth_with_token", "handle": "smoke_user",
                         "profile": {"lang": "en", "intent_score": 0.5}},
            })
            check("leadbus.publish()（带 token）真实投递成功：delivered is True",
                  r_lb_yes.get("delivered") is True)
            check("leadbus.publish()（带 token）未落 outbox（HTTP 成功不排队）",
                  r_lb_yes.get("queued") is not True)

            en_with_token = en_mod.EnableClient(chengjie_url=bus_url, chengjie_token=REQUIRED_TOKEN)
            rs_yes = en_with_token.translate_status()
            check("enable.translate_status()（带 token）拿到正常响应，不是 401",
                  rs_yes.get("available") is True and "401" not in str(rs_yes.get("error", "")))
            check("enable.translate_status()（带 token）真实联网返回 translate_ready 字段",
                  "translate_ready" in rs_yes)
            # 对照检查：同一个 translate() 方法，无 token 时报 401（上面已验证），带 token 后不应再报
            rt_yes = en_with_token.translate("hello", to_lang="zh")
            check("enable.translate()（带 token，对照无 token 的 401）不再报 401",
                  "401" not in str(rt_yes.get("error", "")))

            rb_with_token = rb_mod.ReplyBusClient(bus_url=bus_url, auth_token=REQUIRED_TOKEN)
            rd_yes = rb_with_token.decide({
                "platform": "telegram", "external_id": "tg:smoke_auth_with_token",
                "text": "hi there, auth smoke test with token",
            })
            check("replybus.decide()（带 token）available is True（真吃到总线决策，非本地兜底）",
                  rd_yes.get("available") is True)
            check("replybus.decide()（带 token）决策 action 合法且绝非 send（防双发红线在服务端生效）",
                  rd_yes.get("action") in ("draft", "silent", "handoff"))
        finally:
            server.should_exit = True
            time.sleep(0.3)

    if failures:
        print(f"\n== 结果：{len(failures)} 项失败 ==")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n== 结果：全部通过 —— leadbus/enable/replybus 三个瘦客户端的 Authorization: Bearer "
          "支持在『真配置了鉴权』场景下确实打通（无 token 被拒绝、带对 token 放行）✓ ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
