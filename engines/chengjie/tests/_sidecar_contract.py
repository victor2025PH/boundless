# -*- coding: utf-8 -*-
"""Node sidecar 跨语言契约门禁的共享提取核心（2026-07-30）。

``messenger-web`` 与 ``whatsapp-baileys`` 两个 Node sidecar 的登录/接入契约同构：
Python provider 用 f-string 拼路由、Express 用 ``app.get/post`` 注册，两侧**无编译期
保护**。``test_messenger_sidecar_contract.py`` / ``test_whatsapp_sidecar_contract.py``
各自定义契约清单，共用这里的提取 + 归一化——与 ``tests/_inline_handler_scan.py`` 同
模式：可被多个门禁复用的核心逻辑收一处，防两份提取器无声漂移。

**纯静态、零副作用**：只读源文件做文本提取，不 import 业务模块、不起浏览器、不依赖
实例或 sidecar 在线。
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]

# Express 单行注册：app.get("/x", ...) / app.post('/y', ...)
_ROUTE_RE = re.compile(r"""app\.(?:get|post)\(\s*["']([^"']+)["']""")
# Python provider 里 f"{base}/....": 取到下一个引号
_PROVIDER_PATH_RE = re.compile(r"""\{base\}(/[^"']+)""")


def norm(path: str) -> str:
    """归一化路径参数段：Express ``:id`` 与 f-string ``{login_id}`` 都折成 ``:P``。

    这样 ``/login/{login_id}/status``（Python）与 ``/login/:id/status``（Node）可比。
    去掉尾部斜杠，忽略查询串。"""
    p = path.split("?", 1)[0]
    p = re.sub(r"\{[^}]+\}", ":P", p)                 # f-string 插值段
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", ":P", p)   # Express :param
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def express_routes(server_js: pathlib.Path) -> set:
    """server.js 里所有 app.get/post 注册的路由（归一化）。"""
    txt = server_js.read_text(encoding="utf-8")
    return {norm(m.group(1)) for m in _ROUTE_RE.finditer(txt)}


def provider_paths(py: pathlib.Path) -> set:
    """login provider 里所有 f"{base}/..." 拼的 sidecar 路径（归一化）。"""
    txt = py.read_text(encoding="utf-8")
    return {norm(m.group(1)) for m in _PROVIDER_PATH_RE.finditer(txt)}
