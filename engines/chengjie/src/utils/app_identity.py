"""后端身份标识（供桌面壳的语义探针识别「这是不是我的后端」）。

桌面壳的健康探针一直只判「有没有 HTTP 响应」：

    const FP_HEALTH_PATH = "/login";  // 任何 HTTP 响应都代表后端可达

于是只要目标端口上有**任何**东西在应答，壳就认定「后端已在运行 → 复用，不重复拉起」。
这在单进程年代没问题；现在同一台机器上同时跑主后端(18799) + Baileys(8790) +
messenger-web(8791)，三平台全开后还会更多，端口被别的程序（或上一版本的残留后端）
占用的概率显著上升。撞上了的症状极难排查：工作台能开、部分页面却 404/500——因为壳
连上的根本不是它自己那份后端。

对策是让后端自报身份，壳在复用前校验。这里只暴露**最小**信息（应用 id + 版本），
不含主机名/路径/进程号——它是免鉴权端点（探针要在登录前可用）。

版本来源：发布态由桌面 launcher 经 ``AITR_APP_VERSION`` 注入（与安装包版本同源），
源码态回落 ``dev``。故「壳 0.2.2 + 后端报 0.2.1」这种错配也能被看见。
"""
from __future__ import annotations

import os

#: 应用身份。**不要改**——桌面壳用它判断「这个端口上的是不是我家后端」。
APP_ID = "chengjie"

_FALLBACK_VERSION = "dev"


def app_version() -> str:
    """当前后端版本（发布态由 launcher 注入，源码态为 ``dev``）。"""
    return (os.environ.get("AITR_APP_VERSION") or "").strip() or _FALLBACK_VERSION


def identity_payload() -> dict:
    """``/api/desktop/ping`` 的响应体。字段只增不改（壳按字段名读）。"""
    return {"ok": True, "app": APP_ID, "version": app_version()}
