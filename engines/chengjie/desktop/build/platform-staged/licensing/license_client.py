# -*- coding: utf-8 -*-
"""platform/licensing/license_client.py — 授权/收款『瘦客户端』(纯 stdlib，可降级)。

见 LICENSE_CONTRACT.md：卡密验证/激活/心跳/配额/USDT 收款的**实现**在 TG-AI智控王 的
`license_server.py`(aiohttp 独立服务，默认 :8080)就地运行；本客户端只消费其 HTTP 面，
不搬任何实现(与 platform/enable 消费 avatarhub 的模式同构)。服务端不在线时**不抛异常**，
返回 {"available": False, ...}，调用方按契约 §4 优雅退化：
- validate/activate/heartbeat 不可用 → 用本地缓存的上次授权进入宽限期，不得静默放行付费能力；
- quota/sync_usage 不可用 → 按上次已知配额保守限流；
- products/create_payment/order_status 不可用 → 展示人工收款指引，不阻塞主流程。

两个语义要分清(见契约 §4)：
- "available" —— 瘦客户端注入，指 HTTP 面是否可达(传输层)；
- "success"  —— 服务端返回，指业务是否成立(卡密有效/未超配额/订单存在...)。
  available=True 且 success=False 是正常业务拒绝，不是降级。

依赖铁律：只用 stdlib(urllib/json/os/typing)，不 import engines/products/website，
也不 import 任何第三方包。与同目录 sku_registry 协同：授权/下单按 sku_id 对齐，
见 sku_info() 与契约 §5(服务端 product_id 是 `{level}_{duration}`，映射由调用方维护)。

用法：
    from license_client import LicenseClient
    lc = LicenseClient()                      # base_url 缺省读环境变量 LICENSE_SERVER_URL
    if lc.available():
        r = lc.validate("TGAI-XXXX-XXXX")     # {"success":..., "data":{"level":...}}
    h = lc.health()                           # 始终安全：不可达时 {"available": False, ...}
命令行：python license_client.py --selftest   # 打印自检摘要(服务端不在线也 exit 0)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

_DEFAULT_BASE = os.environ.get("LICENSE_SERVER_URL", "http://127.0.0.1:8080")

# 惰性加载的同目录 sku_registry 模块缓存(见 _sku_registry())
_SKU_REGISTRY_MOD: Any = None


def _sku_registry() -> Any:
    """按文件路径惰性加载同目录 sku_registry.py。

    不用 `from platform.licensing import sku_registry`——顶层 platform 与标准库同名，
    包式导入会遮蔽标准库(见 sku_registry.py 顶部注释)；按路径加载完全绕开该陷阱。
    """
    global _SKU_REGISTRY_MOD
    if _SKU_REGISTRY_MOD is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sku_registry.py")
        spec = importlib.util.spec_from_file_location("_licensing_sku_registry", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load sku_registry from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SKU_REGISTRY_MOD = mod
    return _SKU_REGISTRY_MOD


class LicenseClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 8.0):
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    # ---- 内部 HTTP（可降级：任何失败都收敛为 dict，不抛给调用方）----
    def _get(self, path: str, query: Optional[Dict[str, str]] = None,
             token: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", path, None, query=query, token=token)

    def _post(self, path: str, payload: Dict[str, Any],
              token: Optional[str] = None) -> Dict[str, Any]:
        return self._request("POST", path, payload, token=token)

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]],
                 query: Optional[Dict[str, str]] = None,
                 token: Optional[str] = None) -> Dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                out = json.loads(body) if body else {}
                if isinstance(out, dict):
                    out.setdefault("available", True)
                    return out
                return {"available": True, "data": out}
        except urllib.error.HTTPError as e:
            # 服务端业务拒绝(400/401/403/404/500)也走这里：detail 内含其 {"success":false,"message":...}
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return {"available": False, "error": f"HTTP {e.code}", "detail": detail}
        except Exception as e:  # 连接失败/超时/JSON 解析失败 —— 一律降级
            return {"available": False, "error": str(e)[:200]}

    # ---- 契约方法（见 LICENSE_CONTRACT.md §1/§2，字段 schema 见 license_schema.json）----
    def health(self) -> Dict[str, Any]:
        """GET /api/health — 健康探针。成功返回 {status,server,version,timestamp}。"""
        return self._get("/api/health")

    def available(self) -> bool:
        """license_server HTTP 面是否可达(基于 /api/health)。"""
        return bool(self.health().get("available"))

    def validate(self, license_key: str) -> Dict[str, Any]:
        """POST /api/license/validate — 只验不激活(不绑机、不发 token)。

        成功 data 含 level/levelName/durationDays/durationType/status；
        卡密无效返回 success=False(HTTP 200，非降级)。
        """
        return self._post("/api/license/validate", {"license_key": license_key})

    def activate(self, license_key: str, machine_id: str, **opt: Any) -> Dict[str, Any]:
        """POST /api/license/activate — 激活卡密并绑定机器，签发 JWT。

        opt 可选：device_id / email / invite_code(邀请码，激活即挂邀请关系)。
        成功 data 含 token/userId/level/expiresAt/quotas/features；
        token 供 heartbeat()/quota()/sync_usage() 使用。
        """
        payload: Dict[str, Any] = {"license_key": license_key, "machine_id": machine_id}
        payload.update({k: v for k, v in opt.items() if v is not None})
        return self._post("/api/license/activate", payload)

    def heartbeat(self, token: Optional[str] = None, machine_id: Optional[str] = None,
                  usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST /api/license/heartbeat — 心跳续签(token 与 machine_id 至少给一个，token 优先)。

        成功 data 含新 token/level/expiresAt/isExpired/quotas/features；
        会员已过期时 success=False 且 data.isExpired=True(HTTP 200，非降级)。
        """
        payload: Dict[str, Any] = {}
        if token is not None:
            payload["token"] = token
        if machine_id is not None:
            payload["machine_id"] = machine_id
        if usage is not None:
            payload["usage"] = usage
        return self._post("/api/license/heartbeat", payload)

    def license_status(self, key: str) -> Dict[str, Any]:
        """GET /api/license/status?key= — 查卡密状态/时长(无需 token)。

        成功 data 含 status/level/durationDays/createdAt/usedAt/expiresAt。
        """
        return self._get("/api/license/status", query={"key": key})

    def quota(self, token: str) -> Dict[str, Any]:
        """GET /api/user/quota (Bearer) — 等级配额 + 今日 used/remaining。

        成功 data 含 level/quotas/usage{messagesSent,aiCallsUsed,tgAccountsUsed}/
        remaining{dailyMessages,aiCalls}(-1 表示无限)。
        """
        return self._get("/api/user/quota", token=token)

    def sync_usage(self, token: str) -> Dict[str, Any]:
        """GET /api/usage/sync (Bearer) — 以服务端为准回拉今日 used/remaining/max。

        ⚠ 服务端此路径读的 user_quotas.ai_calls 列与建表列名(ai_calls_used)漂移，
        修复前对 ai 类会 500 → 本客户端降级为 available=False(见契约 §6①)。
        """
        return self._get("/api/usage/sync", token=token)

    def products(self) -> Dict[str, Any]:
        """GET /api/products — 服务端 level×duration 价目。

        data 为数组：{id:`{level}_{duration}`,level,duration,price,quotas,features,...}。
        与 boundless sku_registry 的对齐规则见契约 §5(对客报价以 sku_registry 为准)。
        """
        return self._get("/api/products")

    def create_payment(self, product_id: str, payment_method: str,
                       machine_id: Optional[str] = None, user_id: Optional[str] = None,
                       coupon_code: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/payment/create — 下单。product_id 形如 `gold_month`(level_duration)。

        machine_id / user_id 至少给一个才能把订单挂到用户(否则匿名单)。
        成功 data 含 orderId/amount/currency/status=pending/expiresIn(秒)；
        payment_method='usdt' 时另含 usdt{amount,network=TRC20,address,rate,memo=orderId}。
        ⚠ 服务端 orders/coupons 表 schema 漂移未修复前，下单(尤其带 coupon_code)
        可能 500 → 本客户端降级为 available=False(见契约 §6①)。
        """
        payload: Dict[str, Any] = {"product_id": product_id, "payment_method": payment_method}
        if machine_id is not None:
            payload["machine_id"] = machine_id
        if user_id is not None:
            payload["user_id"] = user_id
        if coupon_code is not None:
            payload["coupon_code"] = coupon_code
        return self._post("/api/payment/create", payload)

    def order_status(self, order_id: str) -> Dict[str, Any]:
        """GET /api/order/status?order_id= — 轮询订单。

        成功 data 含 orderId/status(pending|paid)/productName/amount/paidAt/licenseKey
        (支付回调成功后服务端补写卡密，licenseKey 才非空)。
        """
        return self._get("/api/order/status", query={"order_id": order_id})

    # ---- 与 sku_registry 协同（本地查询，不发 HTTP）----
    def sku_info(self, sku_id: str) -> Dict[str, Any]:
        """从同目录 sku_registry 取 boundless SKU 行(sku_id 是全域单一真相)。

        下单前用它核对价格/可见性/是否 TBD；再由调用方按契约 §5 的映射表换成
        服务端 product_id 去 create_payment()。注册表缺失/无此 SKU 同样收敛为
        {"available": False, ...} 不抛。
        """
        try:
            row = _sku_registry().get_sku(sku_id)
        except Exception as e:
            return {"available": False, "error": str(e)[:200]}
        if not row:
            return {"available": False, "error": f"unknown sku_id: {sku_id}"}
        row.setdefault("available", True)
        return row


def _selftest() -> int:
    lc = LicenseClient()
    print(f"[licensing.license_client] base_url={lc.base_url}")
    h = lc.health()
    print(f"  health(): available={h.get('available')} server={h.get('server', '-')} "
          f"note={str(h.get('error', ''))[:60]}")
    print(f"  available()={lc.available()}  (license_server 未在线属正常，客户端已降级不抛错)")
    s = lc.sku_info("voicex-pro")
    print(f"  sku_info('voicex-pro'): available={s.get('available')} "
          f"price={s.get('price', '-')} {s.get('currency', '')}")
    return 0


if __name__ == "__main__":
    import sys
    # Windows 下 stdout 默认本地代码页(cp936 等)打中文会乱码，统一 UTF-8（与 platform/enable 一致）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(_selftest())  # 带不带 --selftest 都只跑只读自检
