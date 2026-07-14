# -*- coding: utf-8 -*-
"""
任务执行前置检查流水线 (Pre-flight Pipeline)。

检查顺序：网络 → VPN → 账号状态
每步失败立即返回，不继续后续检查。
结果缓存 90 秒，避免频繁重复检查同一设备。
"""

import logging
import subprocess
import time
import threading

from src.utils.subprocess_text import run as _sp_run_text
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

PreflightMode = Literal["full", "network_only", "none"]

logger = logging.getLogger(__name__)

_cache: Dict[str, Tuple[dict, float]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 90


@dataclass
class PreflightResult:
    device_id: str
    passed: bool
    blocked_step: str = ""
    blocked_reason: str = ""
    network_ok: bool = False
    vpn_ok: bool = False
    account_ok: bool = False
    checked_at: float = field(default_factory=time.time)
    preflight_mode: str = "full"
    vpn_skipped_geo_match: bool = False
    vpn_skip_note: str = ""
    geo_snapshot: Optional[Dict[str, Any]] = None
    network_code: str = ""  # 见 NET_* 常量；空="legacy/skipped"

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "passed": self.passed,
            "blocked_step": self.blocked_step,
            "blocked_reason": self.blocked_reason,
            "network_ok": self.network_ok,
            "vpn_ok": self.vpn_ok,
            "account_ok": self.account_ok,
            "checked_at": self.checked_at,
            "preflight_mode": self.preflight_mode,
            "vpn_skipped_geo_match": self.vpn_skipped_geo_match,
            "vpn_skip_note": self.vpn_skip_note,
            "geo_snapshot": self.geo_snapshot,
            "network_code": self.network_code,
        }


def run_preflight(
    device_id: str,
    skip_cache: bool = False,
    mode: PreflightMode = "full",
    task_target_country: Optional[str] = None,
) -> PreflightResult:
    """运行预检流水线，结果缓存 90 秒。

    mode:
      full — 网络 → VPN（可因出口国与任务国一致而跳过）→ 账号
      network_only — 仅外网可达；不要求 VPN（用于 balanced+L1）
      none — 跳过检查，直接通过（用于 L0 或 dev 快速路径）

    task_target_country:
      仅在 mode=full 时生效：若出口 IP 国家与之一致，则不再强制本机 VPN 连接。
    """
    tgt = (task_target_country or "").strip().lower() or None
    cache_key = f"{device_id}|{mode}|{tgt or ''}"
    if not skip_cache:
        with _cache_lock:
            cached = _cache.get(cache_key)
            if cached and (time.time() - cached[1]) < _CACHE_TTL:
                data = cached[0].copy()
                return _preflight_from_cache_dict(data)

    if mode == "none":
        result = _do_preflight_none(device_id)
    elif mode == "network_only":
        result = _do_preflight_network_only(device_id)
    else:
        result = _do_preflight(device_id, task_target_country=tgt)

    with _cache_lock:
        _cache[cache_key] = (result.to_dict(), time.time())
    return result


def _preflight_from_cache_dict(data: dict) -> PreflightResult:
    """兼容旧缓存条目（缺新字段时补默认）。"""
    base = dict(data)
    base.setdefault("vpn_skipped_geo_match", False)
    base.setdefault("vpn_skip_note", "")
    base.setdefault("geo_snapshot", None)
    base.setdefault("network_code", "")
    return PreflightResult(
        device_id=base["device_id"],
        passed=base["passed"],
        blocked_step=base.get("blocked_step", ""),
        blocked_reason=base.get("blocked_reason", ""),
        network_ok=base.get("network_ok", False),
        vpn_ok=base.get("vpn_ok", False),
        account_ok=base.get("account_ok", False),
        checked_at=base.get("checked_at", time.time()),
        preflight_mode=base.get("preflight_mode", "full"),
        vpn_skipped_geo_match=base.get("vpn_skipped_geo_match", False),
        vpn_skip_note=base.get("vpn_skip_note", ""),
        geo_snapshot=base.get("geo_snapshot"),
        network_code=base.get("network_code", ""),
    )


def _geo_result_to_snapshot(geo: Any) -> Dict[str, Any]:
    """序列化 GeoCheckResult，供门禁复用，避免二次 ip-api 查询。"""
    return {
        "expected_country": geo.expected_country,
        "detected_country": geo.detected_country,
        "detected_country_code": geo.detected_country_code,
        "public_ip": geo.public_ip,
        "matches": geo.matches,
        "error": (geo.error or "").strip(),
    }


def _run_preflight_geo_once(device_id: str, task_country: str) -> Any:
    """full 预检内单次 GEO（与门禁期望国一致时可复用）。"""
    from src.behavior.geo_check import check_device_geo
    from src.device_control.device_manager import get_device_manager
    from src.host.device_registry import DEFAULT_DEVICES_YAML

    dm = get_device_manager(DEFAULT_DEVICES_YAML)
    return check_device_geo(device_id, task_country, dm)


def _do_preflight(device_id: str, task_target_country: Optional[str] = None) -> PreflightResult:
    res = PreflightResult(device_id=device_id, passed=False, preflight_mode="full")

    # Step 1: 网络
    net_ok, net_msg, net_code = _check_network_ex(device_id)
    res.network_ok = net_ok
    res.network_code = net_code
    if not net_ok:
        res.blocked_step = "network"
        res.blocked_reason = net_msg
        logger.warning("[preflight] %s 网络未通(%s): %s", device_id[:8], net_code, net_msg)
        return res

    # Step 2: VPN（若本地出口已在目标国，则不必强制 v2ray）
    skip_vpn = False
    if task_target_country:
        try:
            geo = _run_preflight_geo_once(device_id, task_target_country)
            res.geo_snapshot = _geo_result_to_snapshot(geo)
            if geo.error:
                logger.debug(
                    "[preflight] 出口国预检未判定: %s — %s",
                    device_id[:8],
                    geo.error,
                )
            elif geo.matches:
                note = (
                    f"出口≈{geo.detected_country} IP={geo.public_ip} "
                    f"与任务国 {task_target_country} 一致"
                )
                logger.info("[preflight] %s %s，跳过 VPN 硬门槛", device_id[:8], note)
                skip_vpn = True
                res.vpn_ok = True
                res.vpn_skipped_geo_match = True
                res.vpn_skip_note = note
        except Exception as e:
            logger.debug("[preflight] 出口国比对异常: %s", e)

    if not skip_vpn:
        vpn_ok, vpn_msg = _check_vpn(device_id)
        res.vpn_ok = vpn_ok
        if not vpn_ok:
            res.blocked_step = "vpn"
            res.blocked_reason = vpn_msg
            logger.warning("[preflight] %s VPN未连接: %s", device_id[:8], vpn_msg)
            return res

    # Step 3: 账号（进程存活检查，fail-open）
    acc_ok, acc_msg = _check_account(device_id)
    res.account_ok = acc_ok
    if not acc_ok:
        res.blocked_step = "account"
        res.blocked_reason = acc_msg
        logger.warning("[preflight] %s 账号异常: %s", device_id[:8], acc_msg)
        return res

    res.passed = True
    logger.info("[preflight] %s 全部通过 ✓", device_id[:8])
    return res


def _do_preflight_network_only(device_id: str) -> PreflightResult:
    """仅外网：不要求 VPN（出口可为路由器/WiFi 等）。"""
    res = PreflightResult(device_id=device_id, passed=False, preflight_mode="network_only")
    net_ok, net_msg, net_code = _check_network_ex(device_id)
    res.network_ok = net_ok
    res.network_code = net_code
    if not net_ok:
        res.blocked_step = "network"
        res.blocked_reason = net_msg
        logger.warning("[preflight] %s network_only 网络未通(%s): %s", device_id[:8], net_code, net_msg)
        return res
    res.vpn_ok = False
    res.account_ok = True
    res.passed = True
    logger.info("[preflight] %s network_only 通过（未要求 VPN）✓", device_id[:8])
    return res


def _do_preflight_none(device_id: str) -> PreflightResult:
    """不做 ADB 探测（仅用于策略显式放行；仍带 device_id 便于日志）。"""
    return PreflightResult(
        device_id=device_id,
        passed=True,
        blocked_step="",
        blocked_reason="preflight_mode_none",
        network_ok=True,
        vpn_ok=True,
        account_ok=True,
        preflight_mode="none",
    )


def check_device_network_connectivity(device_id: str) -> Tuple[bool, str]:
    """与预检一致的外网探测，供 executor 等模块复用。"""
    return _check_network(device_id)


# 网络探测错误码（供 error_classifier / 前端 fix_action 联动）
NET_OK = "ok"
NET_PROXY_PATH_MISMATCH = "proxy_path_mismatch"  # shell 探测路径不走代理，但出口已是预期国家
NET_PROXY_HIJACK = "proxy_hijack"                # 状态码 301/302 等，被运营商劫持
NET_ZERO = "network_zero"                        # TCP/ICMP/IP 全失败，真断网
NET_TIMEOUT = "network_timeout"                  # USB/adb 超时
NET_ERROR = "network_error"                      # 其它异常


def _check_network(device_id: str) -> Tuple[bool, str]:
    """向后兼容入口：返回 (bool, msg)。新代码请用 _check_network_ex。"""
    ok, msg, _code = _check_network_ex(device_id)
    return ok, msg


def _check_network_ex(device_id: str) -> Tuple[bool, str, str]:
    """外网探测：返回 (passed, msg, code)。

    code 见 NET_* 常量；UI / error_classifier 据此决定 fix_action（换IP / 重启代理 / 重连USB）。

    判据顺序（任一通过即放行）：
    1) gstatic generate_204 (204) — 经典基础联通
    2) facebook.com/robots.txt (200/301/302) — 真实业务端点（避免运营商劫持 false negative）
    3) ICMP 8.8.8.8 / 1.1.1.1
    4) 兜底：能拿到设备 public_ip → 视作 proxy_path_mismatch 通过（warn）
       —— 解决 911proxy 等 SOCKS Per-App 代理时 shell 探测不走代理的误报。

    全失败时根据"是否拿到状态码 / 是否拿到 public_ip"区分 hijack vs zero。
    """
    url = "http://connectivitycheck.gstatic.com/generate_204"
    curl_variants = (
        ("curl", f"curl -s --connect-timeout 5 -o /dev/null -w '%{{http_code}}' {url}"),
        ("toybox", f"toybox curl -s --connect-timeout 5 -o /dev/null -w '%{{http_code}}' {url}"),
        ("sys", f"/system/bin/curl -s --connect-timeout 5 -o /dev/null -w '%{{http_code}}' {url}"),
    )
    try:
        code = ""
        last_label = "curl"
        for label, curl_cmd in curl_variants:
            last_label = label
            r = _sp_run_text(
                ["adb", "-s", device_id, "shell", curl_cmd],
                capture_output=True,
                timeout=12,
            )
            code = r.stdout.strip().strip("'").strip()
            if code == "204":
                return True, f"connected({label})", NET_OK
        # 部分 ROM 对 generate_204 返回异常码，尝试 HEAD（优先标准 curl）
        for label, head_cmd in (
            ("curl", f"curl -s --connect-timeout 5 -I {url} 2>/dev/null | head -n1"),
            ("toybox", f"toybox curl -s --connect-timeout 5 -I {url} 2>/dev/null | head -n1"),
        ):
            r_head = _sp_run_text(
                ["adb", "-s", device_id, "shell", head_cmd],
                capture_output=True,
                timeout=12,
            )
            h1 = (r_head.stdout or "").strip()
            if "204" in h1 or "No Content" in h1:
                return True, f"connected(http_head,{label})", NET_OK

        # 业务真实端点探测（避免 gstatic 被运营商劫持的 false negative）
        fb_url = "https://www.facebook.com/robots.txt"
        for label, fb_cmd in (
            ("curl", f"curl -s --connect-timeout 6 -o /dev/null -w '%{{http_code}}' -L {fb_url}"),
            ("sys", f"/system/bin/curl -s --connect-timeout 6 -o /dev/null -w '%{{http_code}}' -L {fb_url}"),
        ):
            r_fb = _sp_run_text(
                ["adb", "-s", device_id, "shell", fb_cmd],
                capture_output=True,
                timeout=14,
            )
            fb_code = r_fb.stdout.strip().strip("'").strip()
            if fb_code in ("200", "301", "302"):
                return True, f"connected(fb_robots,{label},{fb_code})", NET_OK

        # ICMP 辅证：8.8.8.8/1.1.1.1（通用）+ 31.13.66.35（FB 自身段）
        # P2-⑤: FB 段独立 ICMP 是为了在 gstatic/Google 被运营商劫持时仍能确认"业务真实可达"。
        # 即使 HTTP 全部 302/timeout，只要 FB ICMP 通就视作可工作（业务流量走 TCP 仍可能成功）。
        for host in ("8.8.8.8", "1.1.1.1", "31.13.66.35"):
            r2 = _sp_run_text(
                ["adb", "-s", device_id, "shell", f"ping -c 1 -W 3 {host}"],
                capture_output=True,
                timeout=8,
            )
            if r2.returncode == 0:
                tag = "fb_icmp" if host.startswith("31.13.") else f"ping {host}"
                return True, f"connected({tag})", NET_OK

        # ── 兜底 ①：通过 VPN 系统 HttpProxy 探测 ──
        # V2RayNG/Clash 等会注入系统级 HttpProxy 到 VPN 网络配置，业务 App 自动应用，
        # 但 adb shell 不应用 → 直接探测系统 proxy 即可验证业务路径。
        sys_proxy = _get_device_http_proxy(device_id)
        if sys_proxy:
            for label, px_cmd in (
                ("curl_via_proxy",
                 f"curl -x http://{sys_proxy} -s --connect-timeout 6 -o /dev/null -w '%{{http_code}}' {url}"),
                ("curl_via_proxy_fb",
                 f"curl -x http://{sys_proxy} -s --connect-timeout 8 -o /dev/null -w '%{{http_code}}' -L {fb_url}"),
            ):
                r_px = _sp_run_text(
                    ["adb", "-s", device_id, "shell", px_cmd],
                    capture_output=True, timeout=14,
                )
                px_code = (r_px.stdout or "").strip().strip("'").strip()
                if px_code in ("204", "200", "301", "302"):
                    return True, f"connected(via_http_proxy={sys_proxy},{label}={px_code})", NET_OK

        # ── 兜底 ②：geo 路径独立验证 ──
        # 911proxy 等 Per-App SOCKS 代理只对申请代理的 App 生效，adb shell（uid=2000）
        # 走默认路由 → 探测失败但业务 App 实际通。能拿到 public_ip 说明手机能上网。
        public_ip = _try_get_public_ip(device_id)
        if public_ip:
            warn = (
                f"shell 探测路径未通（HTTP={code!r}），但出口 IP={public_ip} 可达 — "
                f"代理可能为 Per-App 模式，业务 App 流量预计可走代理。已放行。"
            )
            logger.warning("[preflight] %s 探测路径误报兜底: %s", device_id[:8], warn)
            return True, warn, NET_PROXY_PATH_MISMATCH

        # ── 兜底 ③：Android 系统自身已验证 ──
        # NetworkAgent 自带 captive portal 检测，IS_VALIDATED 是系统级真相。
        # 比 shell 探测可靠：走完整网络栈（含 VPN HttpProxy），不受 uid 路由策略影响。
        sys_ok, sys_msg = _check_android_validated(device_id)
        if sys_ok:
            warn = (
                f"shell 探测路径未通（HTTP={code!r}），但 Android 系统判定 {sys_msg} — "
                f"VPN/HttpProxy 场景下 shell 不走代理为已知误报，业务 App 流量可达。已放行。"
            )
            logger.warning("[preflight] %s 系统级兜底: %s", device_id[:8], warn)
            return True, warn, NET_PROXY_PATH_MISMATCH

        # 区分 hijack vs zero：拿到状态码（如 302）= 运营商劫持；空 = 完全断网
        is_hijack = bool(code) and code not in ("000", "204", "200")
        err_code = NET_PROXY_HIJACK if is_hijack else NET_ZERO
        if is_hijack:
            msg = (
                f"代理路径异常 — 探测被劫持/拒绝（HTTP={code!r}，最后尝试={last_label}）。"
                "可能原因：① 911proxy/V2Ray 客户端未启动 ② 代理 IP 已被目标网站封禁 ③ Per-App 代理"
                "未覆盖 shell。建议：先在手机上确认代理客户端连通，再点「换 IP」重试。"
            )
        else:
            msg = (
                "完全无外网（HTTP/ICMP/IP 三路全失败）。建议：① 检查 SIM/Wi‑Fi 信号 ② "
                "重新插拔 USB ③ 在设备页运行「诊断」。"
            )
        return False, msg, err_code
    except subprocess.TimeoutExpired:
        return False, "网络检查超时，请检查 USB 稳定性或重新插拔设备。", NET_TIMEOUT
    except Exception as e:
        return False, f"网络检查异常: {e}", NET_ERROR


def _check_android_validated(device_id: str) -> Tuple[bool, str]:
    """读 dumpsys connectivity 看 Android 系统自身判定的网络可达性。

    Android NetworkAgent 在网络上线后会自动跑 captive portal 检测（generate_204
    走系统网络栈，包括 VPN 的 HttpProxy），通过则标 IS_VALIDATED。
    这是比 adb shell curl 更可靠的判据 —— shell uid=2000 走默认路由，
    不会应用 VPN 的 system HttpProxy（V2RayNG / Clash 等场景）。
    """
    try:
        # IS_VALIDATED 出现在 Score(Policies:...) 字段；CONNECTED + VALIDATED 双重判据
        r = _sp_run_text(
            ["adb", "-s", device_id, "shell",
             "dumpsys connectivity | grep -E 'IS_VALIDATED|Transports:' | head -20"],
            capture_output=True, timeout=8,
        )
        out = (r.stdout or "")
        if "IS_VALIDATED" not in out:
            return False, "no_validated_network"
        import re as _re
        transports = _re.findall(r"Transports:\s*([A-Z|_]+)", out)
        tag = ",".join(set(transports)) or "unknown"
        return True, f"android_validated({tag})"
    except Exception as e:
        return False, f"err:{e}"


def _get_device_http_proxy(device_id: str) -> str:
    """读 dumpsys connectivity 上 VPN/网络配置的系统 HttpProxy。

    格式形如 'HttpProxy: [127.0.0.1] 10808'（V2RayNG/Clash/Surfboard 等会注入）。
    业务 App 自动应用此代理，shell 不会 —— 显式带 -x 探测能验证业务路径。
    """
    try:
        r = _sp_run_text(
            ["adb", "-s", device_id, "shell",
             "dumpsys connectivity | grep -E 'HttpProxy:' | head -3"],
            capture_output=True, timeout=6,
        )
        import re as _re
        m = _re.search(r"HttpProxy:\s*\[([0-9a-fA-F:.]+)\]\s*(\d+)", r.stdout or "")
        if m:
            host, port = m.group(1), m.group(2)
            if host and host != "0.0.0.0" and port and port != "0":
                return f"{host}:{port}"
    except Exception:
        pass
    return ""


def _try_get_public_ip(device_id: str) -> str:
    """尝试通过设备 shell 拿到出口 IP（用于 path_mismatch 兜底）。

    与 geo_check._get_public_ip 行为一致，但隔离失败：
    - 即使 ifconfig.me 等 echo 服务超时也不抛异常，仅返回空字符串。
    - 多源串行兜底，但单源 timeout 6s，整体最多 ~18s。
    """
    services = ("ifconfig.me", "api.ipify.org", "icanhazip.com")
    for svc in services:
        try:
            r = _sp_run_text(
                ["adb", "-s", device_id, "shell", f"curl -s --max-time 6 {svc}"],
                capture_output=True,
                timeout=8,
            )
            ip = (r.stdout or "").strip()
            # 简单 IPv4 校验
            parts = ip.split(".")
            if len(parts) == 4:
                try:
                    if all(0 <= int(p) <= 255 for p in parts):
                        return ip
                except ValueError:
                    continue
        except Exception:
            continue
    return ""


def _check_vpn(device_id: str) -> Tuple[bool, str]:
    try:
        from src.behavior.vpn_manager import check_vpn_status, reconnect_vpn_silent
        s = check_vpn_status(device_id)
        if s.connected:
            return True, "connected"
        if reconnect_vpn_silent(device_id):
            return True, "reconnected"
        return False, "VPN未连接且自动重连失败，请手动检查V2RayNG"
    except Exception as e:
        logger.warning("[preflight] VPN检查异常: %s", e)
        return False, f"VPN检查异常: {e}"


def _check_account(device_id: str) -> Tuple[bool, str]:
    """TikTok进程存活检查（fail-open：进程未运行不是硬错误）。"""
    try:
        r = _sp_run_text(
            ["adb", "-s", device_id, "shell",
             "pidof com.zhiliaoapp.musically || pidof com.ss.android.ugc.trill"],
            capture_output=True,
            timeout=6,
        )
        pid = r.stdout.strip()
        if pid:
            return True, f"running(pid={pid})"
        return True, "not_running(will_start)"
    except Exception as e:
        logger.warning("[preflight] 账号检查异常: %s", e)
        return True, f"check_skipped({e})"


def invalidate_cache(device_id: str):
    """手动使某设备的预检缓存失效（含 network_only / none 分键）。"""
    with _cache_lock:
        to_del = [k for k in list(_cache.keys()) if k == device_id or str(k).startswith(f"{device_id}|")]
        for k in to_del:
            _cache.pop(k, None)


def get_all_readiness(device_ids: List[str]) -> List[dict]:
    """批量获取多台设备的就绪状态（走缓存）。"""
    results = []
    for did in device_ids:
        try:
            r = run_preflight(did)
            results.append(r.to_dict())
        except Exception as e:
            results.append({
                "device_id": did, "passed": False,
                "blocked_step": "error", "blocked_reason": str(e),
                "network_ok": False, "vpn_ok": False, "account_ok": False,
            })
    return results
