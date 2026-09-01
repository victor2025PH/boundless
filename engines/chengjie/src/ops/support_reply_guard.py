# -*- coding: utf-8 -*-
"""对外支持答复红线：不得教用户改配置文件（2026-08-29 老板指令）。

事故背景（同日群实录）：值守在报障群答「账号防轰炸额度」时，把
「打开数据目录的 config 下 local 覆盖层、找到 companion_send_gate、加一行
target_cap: 500」当成方法二发给了用户。老板裁决三条：
① 群内容不得给用户任何「修改配置文件」的方法；
② 配置文件今后要加密与隐藏（见 docs/实施85）；
③ 用户可调的设置一律收进后台管理页（防轰炸额度已收进 /reply-settings）。

本模块＝①的机器闸门：纯函数识别「配置文件修改指引」，由值守发送口
（tools/duty_reply.py）在发送前硬拦。**刻意不设 bypass 旗**——本闸门只挂在
「对客户群发消息」的路径上，那里不存在合法的配置文件话术；真有内部沟通
需要，走内部渠道而不是客户群。

识别口径（宁可错拦逼换措辞，不可漏放）：
- 配置文件名/路径（config*.yaml|yml|json、profiles_runtime、.env 等）；
- 内部配置键名（companion_send_gate / target_cap / l2_autosend 等——
  这些词对客户毫无意义，出现＝在教人改配置）；
- 「打开/修改/编辑 … 配置文件|config」动宾短语；
- YAML 键值行（下划线蛇形键 + ASCII 冒号，如「some_key: 500」）。

正确的替代话术（给值守）：指引后台设置页（如「自动回复设置 →
账号发送额度」）或界面按钮（如横幅上的「白名单此客户」）。
"""
from __future__ import annotations

import re
from typing import List

# 配置文件名/路径：config 系 yaml/json、人设档、告警渠道、.env。
# 「数据目录 + 文件名」是教程的必要成分，文件名本身即高置信信号。
_FILE_RE = re.compile(
    r"(?i)(?:[\w.\\/-]*config[\w.\\-]*\.(?:ya?ml|json)"
    r"|profiles_runtime\.ya?ml"
    r"|notify_webhooks\.json"
    r"|(?<![\w.])\.env(?![\w.]))"
)

# 内部配置键名：对客户零意义，出现在对外答复里＝正在教人改配置。
# （保守收录高置信项；泛词如 enabled/timeout 刻意不进，防误伤正常话术。）
_KEY_MARKERS = (
    "companion_send_gate",
    "target_cap",
    "warmup_start_cap",
    "warmup_ramp_days",
    "reserve_for_manual",
    "exempt_peers",
    "peer_bot_guard",
    "daily_reply_budget",
    "l2_autosend",
    "config.local",
    "config.yaml",
    "auth_token",
)

# 「动词 … 配置文件/config」短语（16 字窗口内）：抓「用记事本打开 config」
# 「修改配置文件」「找到配置文件加一行」这类教程句式。
_VERB_PHRASE_RE = re.compile(
    r"(?:打开|修改|编辑|手改|手动改|改动|改一下|加上|加一行|添加|新增|找到)"
    r"[^\n。；;!！?？]{0,16}(?:配置文件|配置项|config)",
    re.IGNORECASE,
)

# YAML 键值行：行首缩进 + 蛇形键（必须含下划线，压普通英文冒号句的误报）
# + ASCII 冒号。教程贴配置片段必然长这样（「target_cap: 500」）。
_YAML_KV_RE = re.compile(
    r"(?m)^\s*[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*\s*:\s*\S"
)

# 给值守的替代话术提示（CLI 拦截时原样输出）。
POLICY_NOTE = (
    "老板指令（2026-08-29）：对外答复不得包含修改配置文件的方法。"
    "请改为指引后台设置页（自动回复设置 /reply-settings 的对应卡片）"
    "或界面按钮（如横幅「白名单此客户」）；设置页尚未覆盖的项，"
    "登记产品缺口，不要把 YAML 教程发进群。"
)


def config_edit_hits(text: str) -> List[str]:
    """返回命中的「配置文件修改指引」标记（空列表＝干净可发）。

    标记值供拦截提示定位用：``file:<匹配串>`` / ``key:<键名>`` /
    ``verb:<短语>`` / ``yaml_kv:<行>``。同类去重、按出现序稳定输出。
    """
    t = str(text or "")
    if not t.strip():
        return []
    hits: List[str] = []
    seen = set()

    def _add(kind: str, frag: str) -> None:
        mark = f"{kind}:{frag[:40]}"
        if mark not in seen:
            seen.add(mark)
            hits.append(mark)

    for m in _FILE_RE.finditer(t):
        _add("file", m.group(0))
    low = t.lower()
    for key in _KEY_MARKERS:
        if key in low:
            _add("key", key)
    for m in _VERB_PHRASE_RE.finditer(t):
        _add("verb", m.group(0))
    for m in _YAML_KV_RE.finditer(t):
        _add("yaml_kv", m.group(0).strip())
    return hits


__all__ = ["config_edit_hits", "POLICY_NOTE"]
