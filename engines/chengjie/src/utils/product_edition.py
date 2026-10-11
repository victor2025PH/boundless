"""公开包 / 内部包的唯一判断。

``CHATX_FLAVOR`` 非空时优先于 ``CHATX_EDITION``（公开 flavor 不能被
edition=internal 翻过来）。两个都没设，或设了别的值，一律视为公开包。
只有字面 ``internal`` 才是内部包（内测）。博彩模板、内部行业包都靠这个判断。
"""
from __future__ import annotations

import os
from typing import Optional

INTERNAL = "internal"
PUBLIC = "public"


def _raw_flavor() -> str:
    return (os.environ.get("CHATX_FLAVOR") or "").strip().lower()


def _raw_edition() -> str:
    return (os.environ.get("CHATX_EDITION") or "").strip().lower()


def effective_edition() -> str:
    """``internal`` 或 ``public``。未设置 = ``public``。"""
    flavor = _raw_flavor()
    if flavor:
        return INTERNAL if flavor == INTERNAL else PUBLIC
    edition = _raw_edition()
    if edition == INTERNAL:
        return INTERNAL
    return PUBLIC


def is_internal_edition(include_internal: Optional[bool] = None) -> bool:
    """显式布尔优先（CLI / 测试）。``None`` 读环境变量。"""
    if include_internal is True:
        return True
    if include_internal is False:
        return False
    return effective_edition() == INTERNAL


def normalize_edition(edition: str) -> str:
    """调用方传入的形态。

    ``""`` 跟运行时环境走（未设置 = 公开包）。
    ``all`` 只给目录扫描：公开包和内部包文件都要看。
    其它非 ``internal`` 的值都收成公开包。
    """
    e = str(edition or "").strip().lower()
    if e == "all":
        return "all"
    if not e:
        return effective_edition()
    if e == INTERNAL:
        return INTERNAL
    return PUBLIC
