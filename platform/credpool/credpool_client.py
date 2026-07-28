# -*- coding: utf-8 -*-
"""platform/credpool/credpool_client.py — 中央凭据池『瘦客户端』(纯 stdlib，可降级)。

见 CREDPOOL_CONTRACT.md：Telegram api_id/api_hash 的**托管池实现**长在智控
(`tgkz2026/backend/admin/api_pool.py` + `admin/handlers.py`，aiohttp 主后台，默认 `:8000`)
——它已经是可独立运行的池（容量/健康/自动切换/多租户/会员分级俱全），把实现搬进
platform 既不可行也不必要。按 licensing/enable 既有模式：**引擎持实现，platform 只放
『契约 + stdlib 瘦客户端』消费其 HTTP 面。**

解决的产品问题：新用户不懂也申请不到 Telegram api_id/api_hash（my.telegram.org 流程脆、
一号只能一组、国内尤其易失败）。中央池由集团统一注册、统一补给，各产品按需分配，
终端用户**全程无需接触凭据**。

两个语义要分清(与 licensing 同款)：
- "available" —— 瘦客户端注入，指 HTTP 面是否可达(传输层)；
- "success"  —— 服务端返回，指业务是否成立(池空/越权/号不存在...)。
  available=True 且 success=False 是正常业务拒绝，不是降级。

依赖铁律：只用 stdlib(urllib/json/os/typing)，不 import engines/products/website，
也不 import 任何第三方包。本目录**不建 `__init__.py`**（顶层 `platform` 与标准库同名，
包式导入会遮蔽标准库）。

🔒 密钥红线：
- `allocate()` 的返回体含 **api_hash 明文**——调用方只可用于建立 MTProto 会话，
  **绝不可落日志、落事件、回显给终端用户**；本客户端自身不打印、不落任何日志。
- 服务 token 从环境变量读，绝不硬编码；本客户端不提供任何"打印 token"的便利方法。

用法：
    from credpool_client import CredPoolClient
    cp = CredPoolClient()                    # base_url ← CREDPOOL_BASE_URL
    if cp.available():
        r = cp.allocate(phone="+8613800000000", account_id="acc_1")
        if r.get("success"):
            api_id, api_hash = r["data"]["api_id"], r["data"]["api_hash"]
命令行：python credpool_client.py --selftest   # 服务端不在线也 exit 0
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

#: 智控主后台（api-pool 挂在这里；license_server 是另一个服务 :8080，勿混）
_DEFAULT_BASE = os.environ.get("CREDPOOL_BASE_URL", "http://127.0.0.1:8000")

#: 产品服务作用域 token（智控后台「产品服务 Token」处签发，明文只显示一次）
_DEFAULT_TOKEN_ENV = "CREDPOOL_SERVICE_TOKEN"

#: 单组 api_id 默认承载账号数上限——见 CREDPOOL_CONTRACT.md §6「容量与安全默认值」。
#: Telegram 官方未公布"一个 api_id 能带多少账号"的硬上限，官方明确的只有
#: ①一个手机号只能注册一组 api_id ②公开/泄露的 api_id 会触发 API_ID_PUBLISHED_FLOOD
#: ③非官方客户端登录的账号一律进入观察。故按"最安全"取值：与智控池自身的保守默认
#: (`TelegramApiCredential.max_accounts = 5`) 对齐，宁可多注册几组、也不把鸡蛋堆一篮。
SAFE_MAX_ACCOUNTS_PER_API = 5


class CredPoolClient:
    """中央凭据池瘦客户端；所有方法**绝不抛异常**，失败收敛为 available=False。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        service_token: Optional[str] = None,
        timeout: float = 8.0,
    ) -> None:
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self._token = service_token or os.environ.get(_DEFAULT_TOKEN_ENV, "")
        self.timeout = float(timeout)

    # ── 传输层 ────────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            h["X-Service-Token"] = self._token
        return h

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=body, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                data = {"success": False, "error": "unexpected_payload"}
            data["available"] = True
            return data
        except urllib.error.HTTPError as exc:
            # 服务端明确拒绝（401/403/404/500…）：HTTP 面是通的 → available=True
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            data.setdefault("success", False)
            data.setdefault("error", f"http_{exc.code}")
            data["available"] = True
            data["http_status"] = exc.code
            return data
        except Exception as exc:  # 连接失败/超时/DNS/JSON 解析 → 传输层不可用
            return {
                "available": False,
                "success": False,
                "error": type(exc).__name__,
                "detail": str(exc),
            }

    # ── 探针 ──────────────────────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        """GET /api/health —— 智控主后台基础健康探针（免鉴权）。"""
        return self._request("GET", "/api/health")

    def available(self) -> bool:
        """中央池 HTTP 面是否可达（不代表有配额、也不代表 token 有效）。"""
        return bool(self.health().get("available"))

    def configured(self) -> bool:
        """是否已配置服务 token（没配 token 时只能探活，不能分配）。"""
        return bool(self._token)

    # ── 凭据分配 ──────────────────────────────────────────────────────────

    def allocate(
        self,
        phone: str,
        account_id: Optional[str] = None,
        api_id: Optional[str] = None,
        strategy: Optional[str] = None,
        license_key: Optional[str] = None,
        machine_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """为一个账号分配（或复用其已粘定的）托管凭据。

        同一 phone 重复调用会拿回**同一组**凭据（池侧按手机号粘定），
        因此调用方无需自己缓存，重启后再调一次即可。

        `license_key`＝客户的会员卡密。**档位由服务端查库判定**（客户端自报无效），
        付费档才拿得到 `min_member_level` 受限的专属/高优凭据——这就是
        「隔离多开＝专业版权益」的落点。不传＝按 free 档分配，仍可正常用。

        `machine_id`＝本机稳定标识。卡密**首次使用即绑机**，之后换机拿不到付费档
        （收口"一张卡密贴到几台机器上白拿几份专属凭据"）。服务 token 调用方
        **不传就一律按 free**——否则"不送 machine_id"就成了绕过绑机的后门。

        返回 data: {api_id, api_hash, name, member_level, proxy?, allocated_at, ...}
        🔒 api_hash 与 proxy 里的账密都是密钥，禁止落日志/回显。
        """
        return self._request(
            "POST",
            "/api/admin/api-pool/allocate",
            {
                "phone": phone,
                "account_id": account_id,
                "api_id": api_id,
                "strategy": strategy,
                "license_key": license_key,
                "machine_id": machine_id,
            },
        )

    def release(self, phone: str) -> Dict[str, Any]:
        """释放账号占用的凭据额度（退登/删号时调用，把容量还给池）。"""
        return self._request("POST", "/api/admin/api-pool/release", {"phone": phone})

    def account(self, phone: str) -> Dict[str, Any]:
        """查询账号当前绑定的凭据（只读，用于看板/诊断）。"""
        return self._request("GET", "/api/admin/api-pool/account", query={"phone": phone})

    def report(
        self,
        api_id: str,
        success: bool,
        error: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """回报一次使用结果，喂池的健康分与自动切换。

        登录成败**必须**回报——否则池无法识别坏凭据，会持续把新号分给它。
        """
        return self._request(
            "POST",
            "/api/admin/api-pool/result",
            {"api_id": api_id, "success": bool(success), "error": error, "phone": phone},
        )


def _selftest() -> int:
    cp = CredPoolClient()
    print(f"base_url = {cp.base_url}")
    print(f"service_token configured = {cp.configured()}  (env {_DEFAULT_TOKEN_ENV})")
    h = cp.health()
    if h.get("available"):
        print("transport = UP")
        print(f"health payload keys = {sorted(k for k in h if k != 'available')}")
    else:
        print("transport = DOWN (属正常：中央池未启动时消费侧应优雅退化)")
        print(f"  reason = {h.get('error')}: {h.get('detail')}")
    print(f"safe max_accounts per api_id = {SAFE_MAX_ACCOUNTS_PER_API}")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(__doc__)
