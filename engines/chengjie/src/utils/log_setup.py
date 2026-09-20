"""日志装配助手 — src.* 命名空间落盘（2026-07-12 排障盲区修复）。

背景：main.py 的日志配置把 root 钉在 WARNING（防 httpx/uvicorn/pyrogram 等三方库
刷屏），代价是本仓 ``src.*`` 业务模块的 INFO（「配置热重载完成」「入站自动翻译超时」
「backfill 消化」…）在 app.log **全体隐身**——当天排障时被「无日志=没触发」的假信号
误导了三轮。修法：单独给 ``"src"`` logger 挂同一个 file handler（INFO 起落盘），
``propagate=False`` 防 WARNING 再经 root 的 handler 重复写一行；三方库不受影响。

抽成独立模块的原因：main.py 的 initialize() 无法单测（拉起整个 assistant），
这里的幂等/不重复/隔离语义值得被测试钉住。
"""

from __future__ import annotations

import logging


def attach_src_file_handler(file_handler: logging.Handler,
                            level: int = logging.INFO) -> logging.Logger:
    """给 ``src`` 命名空间 logger 挂 file handler（幂等，返回该 logger）。

    - ``src.*`` 的 ``level`` 起（默认 INFO）落盘到与主日志同一文件；
    - ``propagate=False``：防同一条记录再经 root 的同文件 handler 写重复行；
    - 幂等：同一 baseFilename 的 handler 不重复挂（热重启/重配置场景）。
    """
    src_logger = logging.getLogger("src")
    src_logger.setLevel(level)
    src_logger.propagate = False
    base = getattr(file_handler, "baseFilename", None)
    have_same = any(
        getattr(h, "baseFilename", None) == base and base is not None
        for h in src_logger.handlers
    )
    if not have_same:
        src_logger.addHandler(file_handler)
    return src_logger


def mirror_handlers_to_src(source_logger: logging.Logger,
                           level: int = logging.INFO) -> logging.Logger:
    """把主 logger 的**全部** handler 镜像给 ``src``（幂等，返回 src logger）。

    2026-08-04 198 取证盲区第二课：上面的 file 补丁只救了「有 app.log 的
    部署」；桌面版 ``logging.file`` 常为空 → 主 logger 只有 console handler
    → ``src.*``（编排器/扫码登录/会话健康/收件箱路由）在 backend.log
    （stdout 镜像）**依旧全体隐身**——当晚复盘 198 五个进程会话，编排器
    零日志可查，法证只能靠拉库反推。本函数把主 logger 现有的 console+file
    一并镜像：

    - 先按 handler **身份**去重（file handler 若已被 attach_src_file_handler
      挂过同一对象，不重复挂）；再按 baseFilename 去重（不同对象同文件）；
    - ``propagate=False``：防经 root 重复写一行；
    - 幂等可重入（热重启/重配置随便调）。
    """
    src_logger = logging.getLogger("src")
    src_logger.setLevel(level)
    src_logger.propagate = False
    for h in list(getattr(source_logger, "handlers", []) or []):
        if h in src_logger.handlers:
            continue
        base = getattr(h, "baseFilename", None)
        if base is not None and any(
                getattr(x, "baseFilename", None) == base
                for x in src_logger.handlers):
            continue
        src_logger.addHandler(h)
    return src_logger
