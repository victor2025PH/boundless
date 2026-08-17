# -*- coding: utf-8 -*-
"""模板编译失败的运行时兜底（2026-08-17 `){#` 事故第三层防线）。

模板热更新＝保存即生产：``auto_reload`` 下任何一次坏保存，从下一个请求起对应
页面全员 500，直到有人修好——2026-08-17 实测事故窗 ~25 分钟（额度横幅 CSS
``){#`` 被 Jinja 读成永不闭合的注释起始符，``unified_inbox.html``＝整个坐席台）。

静态防线已有两层（``tools/template_compile_check`` 秒级 CLI＝gate_sweep 首位门禁
＋ ``agent_probe`` 开工预警），但它们都要「有人跑」；本模块把最后一层装进服务
进程本身：

    编译失败 且 该模板本进程内成功编译过 → 供应最后一版好模板 + CRITICAL 限流日志
    编译失败 且 从未成功过（冷启动就坏）  → 照旧抛出（fail loud，绝不掩盖坏部署）

「宁可旧一会儿，不可 500 一会儿」——坏保存的修复压力仍在（探针/门禁/CRITICAL
日志都在喊），但坐席工作台不再陪葬。

接线（admin.py 单点，构造 Environment 后一行）::

    install_template_guard(templates.env)

设计取舍：
- 包在 ``Environment.get_template`` 外层而非 hack ``_load_template`` 内部——
  ``{% include %}``/``{% extends %}`` 渲染期同样经 get_template，一层全覆盖，
  且不碰 Jinja 私有 API（版本升级安全）。
- **只兜 TemplateSyntaxError**（坏保存的确定形态）；TemplateNotFound / IO 错误
  照旧抛——「文件没了」不是「旧版本还能用」的语义，兜了反而掩盖部署残缺。
- last-good 持有 Template 对象强引用：量级=模板数（~81），与 Jinja 自身 cache
  同数量级，无泄漏面。
- CRITICAL 日志按模板名 60s 限流：请求速率级重复日志会刷爆 app.log，而
  「还坏着」一分钟喊一次足够被日志监控/人眼接住。
- 刻意不在此接 EventBus/host_alert：兜底层必须零依赖零失败面；外发告警走
  日志监控链路（seat log monitor / watchdog 均盯 CRITICAL）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

from jinja2 import Environment, TemplateSyntaxError

logger = logging.getLogger("ai_chat_assistant.template_guard")

_LOG_THROTTLE_SEC = 60.0


def install_template_guard(env: Environment, *, clock=time.monotonic) -> Dict[str, Any]:
    """给 Jinja ``Environment`` 装「最后一版好模板」兜底。

    返回内部状态 dict（``last_good`` / ``served_stale`` 计数，测试与观测用）。
    幂等：重复安装返回既有状态，不叠包装层。
    """
    existing = getattr(env, "_template_guard_state", None)
    if existing is not None:
        return existing

    state: Dict[str, Any] = {"last_good": {}, "served_stale": 0, "_last_log": {}}
    orig_get = env.get_template

    def guarded_get_template(name, *args, **kwargs):
        key = str(name)
        try:
            tpl = orig_get(name, *args, **kwargs)
        except TemplateSyntaxError as exc:
            prev = state["last_good"].get(key)
            if prev is None:
                raise  # 本进程从未见过它的好版本：冷启动就坏=部署问题，如实炸
            state["served_stale"] += 1
            now = clock()
            if now - state["_last_log"].get(key, float("-inf")) >= _LOG_THROTTLE_SEC:
                state["_last_log"][key] = now
                logger.critical(
                    "模板 %s 编译失败（line %s: %s），已用最后一版好模板顶住页面；"
                    "磁盘上的坏保存必须立即修复（定位：python tools/template_compile_check.py）",
                    key,
                    getattr(exc, "lineno", "?"),
                    getattr(exc, "message", None) or exc,
                )
            return prev
        state["last_good"][key] = tpl
        return tpl

    env.get_template = guarded_get_template  # type: ignore[method-assign]
    env._template_guard_state = state  # type: ignore[attr-defined]
    return state
