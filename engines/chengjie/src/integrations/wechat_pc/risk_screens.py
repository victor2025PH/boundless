# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 风控/掉线屏分类（纯函数，实施97 线 B）。

复用 ``src/ops/rpa_risk_screen.classify_risk_screen``（跨平台 ban/verify/limit 词表）作底，
叠加 PC 微信特有的四类：

- ``logged_out``   登录窗口（探针实锤：4.1.12 登录窗类名 ``mmui::LoginWindow``，含「进入WeChat /
                   切换账号 / 仅传输文件」按钮）/「已在手机上退出」/「你已退出微信」
- ``phone_confirm`` 「请在手机上确认登录」——需要主人手机，驱动只能等 + 告警
- ``update_required`` 「当前版本过低 / 请更新」——版本钉死策略被打破，驱动进只读并告警
- ``env_abnormal`` 「登录环境异常 / 账号存在安全风险」——账号级急停（TTL 长）

每类给出处置 :class:`Disposition`：是否冻结发送、冻结多久、是否标 offline、是否通知主人。
判序：ban > env_abnormal > verify > logged_out > phone_confirm > update_required > limit > none。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.ops.rpa_risk_screen import BAN, LIMIT, NONE, VERIFY, classify_risk_screen

LOGGED_OUT = "logged_out"
PHONE_CONFIRM = "phone_confirm"
UPDATE_REQUIRED = "update_required"
ENV_ABNORMAL = "env_abnormal"

LOGIN_WINDOW_CLASSES = ("mmui::LoginWindow",)

_LOGGED_OUT_MARKERS = (
    "进入wechat", "进入微信", "切换账号", "仅传输文件", "扫码登录", "登录微信",
    "已在手机上退出", "你已退出微信", "you have logged out", "log in to wechat", "enter wechat",
)
_PHONE_CONFIRM_MARKERS = (
    "请在手机上确认", "请在手机微信上确认", "手机确认登录", "confirm login on your phone",
)
_UPDATE_MARKERS = (
    "当前版本过低", "版本过低", "请更新微信", "请升级微信", "需要更新", "update required",
    "your version is too old", "please update wechat",
)
_ENV_ABNORMAL_MARKERS = (
    "登录环境异常", "环境异常", "账号存在安全风险", "帐号存在安全风险", "账号异常",
    "为了你的账号安全", "为了您的账号安全", "unusual login", "account at risk",
)


@dataclass(frozen=True)
class Disposition:
    kind: str
    freeze_sends: bool
    freeze_ttl_sec: float
    mark_offline: bool
    notify_owner: bool
    readonly: bool

    def as_dict(self) -> dict:
        return {"kind": self.kind, "freeze_sends": self.freeze_sends,
                "freeze_ttl_sec": self.freeze_ttl_sec, "mark_offline": self.mark_offline,
                "notify_owner": self.notify_owner, "readonly": self.readonly}


_DISPOSITIONS = {
    NONE: Disposition(NONE, False, 0.0, False, False, False),
    BAN: Disposition(BAN, True, 30 * 86400.0, True, True, True),
    ENV_ABNORMAL: Disposition(ENV_ABNORMAL, True, 24 * 3600.0, False, True, True),
    VERIFY: Disposition(VERIFY, True, 6 * 3600.0, False, True, True),
    LOGGED_OUT: Disposition(LOGGED_OUT, True, 0.0, True, True, True),
    PHONE_CONFIRM: Disposition(PHONE_CONFIRM, True, 0.0, False, True, True),
    UPDATE_REQUIRED: Disposition(UPDATE_REQUIRED, True, 0.0, False, True, True),
    LIMIT: Disposition(LIMIT, True, 2 * 3600.0, False, True, False),
}


def classify_pc_screen(texts: Iterable[str], *, window_class: str = "") -> str:
    """窗口/弹窗可见文本 + 顶层窗口类名 → 风控屏类别（判序见模块头）。"""
    wc = str(window_class or "")
    joined = " ".join(str(t or "") for t in texts if t).lower()
    base = classify_risk_screen(joined) if joined else NONE
    if base == BAN:
        return BAN
    if any(m in joined for m in _ENV_ABNORMAL_MARKERS):
        return ENV_ABNORMAL
    if base == VERIFY:
        return VERIFY
    if wc in LOGIN_WINDOW_CLASSES or any(m in joined for m in _LOGGED_OUT_MARKERS):
        return LOGGED_OUT
    if any(m in joined for m in _PHONE_CONFIRM_MARKERS):
        return PHONE_CONFIRM
    if any(m in joined for m in _UPDATE_MARKERS):
        return UPDATE_REQUIRED
    if base == LIMIT:
        return LIMIT
    return NONE


def disposition_for(kind: str) -> Disposition:
    return _DISPOSITIONS.get(str(kind or NONE), _DISPOSITIONS[NONE])


def assess(texts: Iterable[str], *, window_class: str = "") -> Disposition:
    return disposition_for(classify_pc_screen(texts, window_class=window_class))


__all__ = [
    "NONE", "BAN", "VERIFY", "LIMIT", "LOGGED_OUT", "PHONE_CONFIRM", "UPDATE_REQUIRED",
    "ENV_ABNORMAL", "LOGIN_WINDOW_CLASSES", "Disposition", "classify_pc_screen",
    "disposition_for", "assess",
]
