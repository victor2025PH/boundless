"""主机弹窗告警（Windows）——云端 Key 失效/云端不可达/余额不足等关键运维事件提醒机主。

设计原则：
- 绝不影响主流程：所有函数吞掉自身异常，永不抛出。
- 去抖防刷屏：同一 key 在冷却窗内只弹一次。
- 非阻塞：Windows 弹窗在守护线程里弹（MessageBoxW），不卡调用方。
- **只弹给算力提供方**：桌面打包端（用户机，``AITR_DESKTOP_MODE=1``）一律不弹窗——
  用户只管用，云端/算力问题由我们这边处理；``HOST_ALERT_SILENT=1``（测试/CI/无桌面）
  同样静默。静默只关「弹窗」，日志与 EventBus 镜像照常。
- 远程可见：每次告警同时 publish EventBus ``host_alert`` 事件（带 ``rate_key``），
  由 WebhookNotifier 按订阅外发（Telegram 等）——机主不在机器前也能收到。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any

# 挂在 ai_chat_assistant 命名空间下：继承主程序的 console+file handler，
# 弹窗事件同步落 logs/app.log（否则裸 "host_alert" 无 handler → 只走 lastResort
# stderr，事后无从查「弹的是哪个 provider、什么错误」）。
_logger = logging.getLogger("ai_chat_assistant.host_alert")
_last_alert: dict[str, float] = {}
_lock = threading.Lock()

# key 失效/不可用的特征串（覆盖中英文与常见云厂商措辞）
_KEY_FAIL_MARKERS = (
    "unauthorized", "invalid api key", "invalid_api_key", "incorrect api key",
    "invalid authentication", "authentication", "permission denied", "permissiondenied",
    "api key", "api_key", "apikey", "access denied", "forbidden",
    "quota", "insufficient", "billing", "arrears", "arrearage", "expired",
    "欠费", "余额不足", "密钥", "鉴权", "无权", "认证失败", "配额", "过期", "已失效",
)


def looks_like_key_failure(err: Any) -> bool:
    """粗判一个异常/消息是否像「云端 Key 不可用/出问题」。"""
    try:
        # 优先看 HTTP 状态码（401 未授权 / 402 欠费 / 403 禁止）
        code = getattr(err, "status_code", None)
        if code is None:
            resp = getattr(err, "response", None)
            code = getattr(resp, "status_code", None)
        try:
            if int(code) in (401, 402, 403):
                return True
        except (TypeError, ValueError):
            pass
        s = str(err).lower()
        if " 401" in s or " 402" in s or " 403" in s or "http 401" in s or "http 403" in s:
            return True
        return any(m in s for m in _KEY_FAIL_MARKERS)
    except Exception:
        return False


def popups_suppressed() -> bool:
    """弹窗是否应被抑制（日志/EventBus 不受影响）。

    - ``HOST_ALERT_SILENT=1``：测试/CI/无桌面环境显式静默。
    - ``AITR_DESKTOP_MODE=1``：桌面打包端=用户机。用户只管使用（算力我们提供），
      云端 Key/余额/不可达都不是用户能处理的事，不该打扰。
    """
    try:
        _on = ("1", "true", "yes", "on")
        if os.environ.get("HOST_ALERT_SILENT", "").strip().lower() in _on:
            return True
        if os.environ.get("AITR_DESKTOP_MODE", "").strip().lower() in _on:
            return True
        return False
    except Exception:
        return False


def _mirror_to_event_bus(title: str, message: str, key: str) -> None:
    """告警镜像进 EventBus（``host_alert`` 事件）→ WebhookNotifier 可外发到 Telegram 等。

    惰性导入 + 全吞异常：host_alert 必须保持零依赖可用（含裸脚本进程）。
    """
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("host_alert", {
            "title": str(title),
            "message": str(message),
            # notifier 速率限制按 rate_key 区分：不同告警键互不挤占限流窗
            "rate_key": str(key),
        })
    except Exception:
        pass


# ── Toast 通知（2026-08-17 弹窗美化）───────────────────────────────────
# Windows 通知中心 Toast 替代上古 MessageBoxW：品牌图标 + 结构化文案（标题/正文/
# 技术行三层）+ 可点按钮（protocol 激活直开工作台）+ 通知中心自动留存历史
# （旧弹窗点「确定」即消失、无处回看）。失败/超时自动回落 MessageBoxW 保底可见；
# HOST_ALERT_TOAST=0 一键回旧弹窗。零 pip 依赖：powershell -EncodedCommand 调
# WinRT ToastNotificationManager（AppId 借 PowerShell 的开始菜单注册身份）。

_TOAST_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
    "\\WindowsPowerShell\\v1.0\\powershell.exe"
)


def _toast_enabled() -> bool:
    try:
        return os.environ.get("HOST_ALERT_TOAST", "1").strip().lower() not in (
            "0", "false", "off", "no")
    except Exception:
        return True


def _toast_icon_uri() -> str:
    """品牌图标（best-effort）：引擎内置 ChatX logo；缺失则不带图标。"""
    try:
        from pathlib import Path
        p = (Path(__file__).resolve().parents[2]
             / "src" / "web" / "static" / "brand" / "chatx.png")
        if p.is_file():
            return p.as_uri()
    except Exception:
        pass
    return ""


def _xml_escape(s: str, *, attr: bool = False) -> str:
    out = str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if attr:
        out = out.replace('"', "&quot;").replace("'", "&apos;")
    return out


def _toast_script(title: str, message: str, *, attribution: str = "",
                  open_url: str = "") -> str:
    """构造弹 Toast 的 PowerShell 脚本（纯函数，可测）。

    - scenario=reminder + 常驻按钮：不自动消失，对齐旧 MessageBox「必须看到」语义
      （但非阻塞、不抢焦点）；
    - open_url 非空 → 点击 toast 本体或「打开工作台」按钮经 protocol 激活开浏览器；
    - attribution 行放技术码（domain:reason·会话·时间），工程师排查用，不占正文。
    """
    xml_parts = ['<toast scenario="reminder"']
    if open_url:
        xml_parts.append(
            f' activationType="protocol" launch="{_xml_escape(open_url, attr=True)}"')
    xml_parts.append('><visual><binding template="ToastGeneric">')
    _icon = _toast_icon_uri()
    if _icon:
        xml_parts.append(
            f'<image placement="appLogoOverride" hint-crop="circle" '
            f'src="{_xml_escape(_icon, attr=True)}"/>')
    xml_parts.append(f"<text>{_xml_escape(title)}</text>")
    # 正文换行转 &#10;（LoadXml 后还原为真换行，ToastGeneric 按行渲染）
    xml_parts.append(f"<text>{_xml_escape(message).replace(chr(10), '&#10;')}</text>")
    if attribution:
        xml_parts.append(
            f'<text placement="attribution">{_xml_escape(attribution)}</text>')
    xml_parts.append("</binding></visual><actions>")
    if open_url:
        xml_parts.append(
            f'<action content="打开工作台" activationType="protocol" '
            f'arguments="{_xml_escape(open_url, attr=True)}"/>')
    xml_parts.append(
        '<action content="知道了" activationType="system" arguments="dismiss"/>')
    xml_parts.append("</actions>")
    xml_parts.append('<audio src="ms-winsoundevent:Notification.Default"/>')
    xml_parts.append("</toast>")
    xml = "".join(xml_parts)
    ps_xml = xml.replace("'", "''")  # PowerShell 单引号串内转义
    return (
        "$ErrorActionPreference='Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType=WindowsRuntime] | Out-Null;"
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$doc.LoadXml('{ps_xml}');"
        "$t = New-Object Windows.UI.Notifications.ToastNotification($doc);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"'{_TOAST_APP_ID}').Show($t);"
    )


def _show_toast(title: str, message: str, *, attribution: str = "",
                open_url: str = "") -> bool:
    """弹一个 Toast；成败以子进程退出码为准（失败由调用方回落 MessageBox）。"""
    try:
        import base64 as _b64
        import subprocess
        script = _toast_script(title, message,
                               attribution=attribution, open_url=open_url)
        enc = _b64.b64encode(script.encode("utf-16-le")).decode("ascii")
        # 只用 CREATE_NO_WINDOW 压掉黑框；**别加 -WindowStyle Hidden**——
        # 两者叠加实测让 PS 卡 20+ 秒（单用 NO_WINDOW 0.9s，二分实锤 2026-08-17）。
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        try:
            # 30s：PS 首次调用有「Preparing modules for first use」冷启动
            # （实测 >15s，暖机后 ~3s）；杀早了 toast 根本没来得及 Show。
            return proc.wait(timeout=30) == 0
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            return False
    except Exception:
        return False


def notify_host(title: str, message: str, *, key: str = "", cooldown_sec: float = 1800.0,
                open_url: str = "", attribution: str = "") -> bool:
    """告警出口：日志 + EventBus 镜像 + （仅算力机）非阻塞弹窗（Toast→MessageBox）。

    同 key 冷却窗内只提醒一次；返回是否本次实际提醒。绝不抛异常。
    ``open_url``：Toast 点击/按钮打开的链接（如工作台）；``attribution``：技术码行
    （domain:reason·会话·时间），Toast 小字显示，日志与 webhook 追加在正文尾。
    """
    try:
        k = (key or title or "").strip() or "host_alert"
        now = time.time()
        with _lock:
            if now - _last_alert.get(k, 0.0) < max(0.0, cooldown_sec):
                return False
            _last_alert[k] = now
        _full = f"{message}\n{attribution}" if attribution else str(message)
        _logger.warning("[HOST ALERT] %s | %s", title, _full.replace("\n", " ¦ "))
        _mirror_to_event_bus(title, _full, k)
        if popups_suppressed():
            return True
        if sys.platform == "win32":
            def _popup():
                try:
                    if _toast_enabled() and _show_toast(
                            str(title), str(message),
                            attribution=str(attribution or ""),
                            open_url=str(open_url or "")):
                        return
                except Exception:
                    pass
                try:
                    import ctypes
                    # MB_ICONWARNING(0x30) | MB_SETFOREGROUND(0x10000) | MB_TOPMOST(0x40000)
                    _mb = str(message) + (("\n\n" + str(attribution)) if attribution else "")
                    ctypes.windll.user32.MessageBoxW(0, _mb, str(title), 0x30 | 0x10000 | 0x40000)
                except Exception:
                    pass
            threading.Thread(target=_popup, name="host_alert_popup", daemon=True).start()
        return True
    except Exception:
        return False


def notify_key_failure(provider: str, detail: str = "", *, cooldown_sec: float = 1800.0) -> bool:
    """便捷入口：云端 Key 失效提醒（按 provider 去抖）。"""
    title = "云端 Key 异常"
    msg = f"{provider} 的 API Key 不可用或出现问题，请检查/更换。\n详情: {detail}".strip()
    return notify_host(title, msg, key=f"keyfail:{provider}", cooldown_sec=cooldown_sec)


def notify_cloud_outage(provider: str, detail: str = "", *, fallback_ready: bool = False,
                        cooldown_sec: float = 1800.0) -> bool:
    """便捷入口：云端主模型不可达/持续失败（熔断开路）提醒。

    与 key 失效分开去抖：网络黑洞/云端宕机不含 key 特征串，也必须让机主知道。
    """
    title = "云端 AI 不可达"
    tail = ("本地兜底模型已顶班，用户对话不受影响；" if fallback_ready
            else "未配置本地兜底，用户只能收到占位回复；")
    msg = f"{provider} 连续失败已熔断。{tail}请尽快检查云端服务/网络/Key。\n详情: {detail}".strip()
    return notify_host(title, msg, key=f"outage:{provider}", cooldown_sec=cooldown_sec)


def notify_balance_low(provider: str, balance: float, threshold: float, currency: str = "CNY",
                       *, cooldown_sec: float = 21600.0) -> bool:
    """便捷入口：云端账户余额低于阈值提醒（默认 6h 重提一次，直到充值恢复）。"""
    title = "云端余额不足"
    msg = (f"{provider} 账户余额仅剩 {balance:.2f} {currency}"
           f"（阈值 {threshold:.0f} {currency}）。余额耗尽后云端将拒绝调用，请尽快充值。")
    return notify_host(title, msg, key=f"balance:{provider}", cooldown_sec=cooldown_sec)
