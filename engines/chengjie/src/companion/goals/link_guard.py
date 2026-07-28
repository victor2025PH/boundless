"""目标带货「链接纪律」出站守卫（P4，纯函数）。

问题：CTA 分级只是 **prompt 里的叮嘱**——soft/hold 日 LLM 完全可能从
上下文历史（昨天 direct 日发过的链接就躺在对话窗口里）复读出官网下单链，
纪律形同虚设。与 media_promise_guard 同族的确定性兜底：生成后按当日
CTA 档剥离不许出现的**本域**链接，别家域名一概不碰（不是全网链接警察）。

档位语义（与 site_catalog.pick_cta 同刻度）：
- ``order``      全放行（今天本来就许发下单链）；
- ``cs``/``roi`` 剥下单深链（order_path 前缀），放行试算器/落地页等站内链；
- ``""``（soft 早期 / hold 日 / bazi 同轮让位）剥全部本域链接。

剥离带清扫：URL 前紧邻的「下单：/链接：」类引导标签一并去掉，防止留下
悬空冒号；整行只剩标点/空白则整行删。任何异常返回原文——守卫绝不吃掉回复。
"""

from __future__ import annotations

import re
from typing import Tuple
from urllib.parse import urlparse

# URL 匹配：截止于空白/CJK 标点/右括号类（对话文本里链接常被「）。！」贴尾）
_URL_RE = re.compile(r"https?://[^\s<>\"'）】」，。！？；]+", re.I)
# 「引导标签 + 冒号 + 被剥 URL 占位符」一次清掉，防止留悬空「下单：」
_LABEL_URL_RE = re.compile(
    r"(?:下单链接|下单|订购|购买|点这里|戳这里|链接|地址|官网)?\s*[:：→]?\s*\x00")
# 剥完只剩标点/空白的行 → 整行删
_HUSK_RE = re.compile(r"^[\s\-–—·:：,，.。;；!！?？()（）\[\]【】\"'「」]*$")


def _same_site(url: str, base_host: str) -> bool:
    """URL 是否本域（含子域）。解析失败按「不是」处理（不误剥别家链接）。"""
    try:
        host = (urlparse(url).netloc or "").split("@")[-1].split(":")[0].lower()
        b = base_host.lower()
        return bool(host) and (host == b or host.endswith("." + b)
                               or b.endswith("." + host))
    except Exception:
        return False


def _is_order_link(url: str, order_path: str) -> bool:
    try:
        p = (urlparse(url).path or "/").rstrip("/") or "/"
        op = "/" + str(order_path or "/order").strip("/")
        return p == op or p.startswith(op + "/")
    except Exception:
        return False


def sanitize_goal_links(
    text: str,
    *,
    cta: str = "",
    base_url: str = "",
    order_path: str = "/order",
) -> Tuple[str, int]:
    """按 CTA 档剥离越纪律的本域链接。返回 ``(text, 剥离条数)``。

    ``base_url`` 空（目录没配 site）→ 不知道哪些链接算「我们的」→ 原样放行。
    """
    t = str(text or "")
    lvl = str(cta or "").strip().lower()
    if not t or lvl == "order":
        return t, 0
    try:
        base_host = (urlparse(str(base_url or "")).netloc or "").split(":")[0]
    except Exception:
        base_host = ""
    if not base_host:
        return t, 0

    stripped = 0

    def _sub(m: "re.Match[str]") -> str:
        nonlocal stripped
        url = m.group(0)
        if not _same_site(url, base_host):
            return url
        if lvl in ("cs", "roi") and not _is_order_link(url, order_path):
            return url                      # 站内非下单链（试算器/落地页）放行
        stripped += 1
        return "\x00"                       # 占位，随后连同引导标签一起清扫

    out = _URL_RE.sub(_sub, t)
    if not stripped:
        return t, 0

    out = _LABEL_URL_RE.sub("", out)
    lines = []
    for line in out.split("\n"):
        line = re.sub(r"[ \t\u3000]{2,}", " ", line).rstrip()
        if _HUSK_RE.match(line):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    # 全文被剥空（整条回复就是一条链接）→ 保守回退原文，宁可漏拦不吃回复
    if not cleaned:
        return t, 0
    return cleaned, stripped


__all__ = ["sanitize_goal_links"]
