"""ShopifyConnector — 真实 Shopify Admin REST 订单/物流查询（Phase D / P1-a）。

设计要点：
- **注入式 http**：构造可传 ``http_get(url, headers) -> resp``（resp 有 .status_code / .json()）；
  不传则惰性用 httpx.AsyncClient。单测注入 fake http_get 即可，**不联网**。
- 只读：仅 GET orders，绝不写回 Shopify。
- get_order 按订单号(name)查；track_shipment Shopify Admin 无「按运单号直查」端点 →
  返回 None（上层 ToolResult.found=False → 如实告知查不到，勿编造）。
- 任何异常/非 200 → 返回 None（service 层包成 not_found / error，不崩）。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.utils import external_api_lifecycle as _lifecycle

from .models import OrderInfo, ShipmentInfo

logger = logging.getLogger(__name__)

HttpGet = Callable[[str, dict], Awaitable[Any]]

#: Shopify Admin API 版本的单一事实源。**别在别处写死日期版本号**——
#: 门禁 ``tests/test_external_api_lifecycle.py`` 会扫出来。
#:
#: ⚠ Shopify 的失败模式比 Meta 阴险得多：版本过期**不报错**，静默 fall-forward 到
#: 最老的受支持版本（官方原话：「requests to a retired 2026-10 are served as
#: 2027-01」）。也就是说破坏性变更会被悄悄应用，日志里一个字都看不到——你以为在用
#: 2024-01，其实在用别的。所以这里的到期日只能靠**门禁在 CI 里提前喊**，
#: 指望线上报错是等不到的。
DEFAULT_API_VERSION = "2026-07"

#: 官方公布的「可访问至」（https://shopify.dev/docs/api/usage/versioning）。
#: 季度发版、每版至少支持 12 个月。**照抄官方表，别按「发布+12 月」自己推算**
#: ——官方实际是 +12 月再加到当月 16 日，自己推会差半个月。
VERSION_EOL: Dict[str, Optional[date]] = {
    "2025-07": date(2026, 7, 16),
    "2025-10": date(2026, 10, 16),
    "2026-01": date(2027, 1, 16),
    "2026-04": date(2027, 4, 16),
    "2026-07": date(2027, 7, 16),
    "2026-10": date(2027, 10, 16),
    "2027-01": date(2028, 1, 16),
}


def _warn_if_stale(version: str) -> None:
    """运维在配置里显式指定了版本时，检查它是否已过期/不认识。

    **只警告，不静默改写**——与告警通道那边同一条判断：运维显式写下的值，我们无权
    替他改（他可能正是为了钉住某个行为才写的）；但也不能装作没看见，
    尤其 Shopify 过期后线上完全没有信号。
    """
    ver = str(version or "").strip()
    if not ver or ver == DEFAULT_API_VERSION:
        return
    _, days, level = _lifecycle.status_in_table(ver, VERSION_EOL)
    if level in ("expired", "critical", "unknown"):
        logger.warning(
            "ecommerce: 配置里的 Shopify api_version=%s 已%s（当前建议 %s）。"
            "Shopify 对退役版本不报错、会静默改用更新的版本——请尽快确认并升级。",
            ver,
            "不在官方支持列表内" if level == "unknown" else f"临近/超过到期日（剩 {days} 天）",
            DEFAULT_API_VERSION,
        )


def pins() -> List[_lifecycle.ApiPin]:
    """向通用生命周期登记表申报 Shopify 侧钉住的版本。"""
    return [
        _lifecycle.ApiPin(
            key="shopify_admin",
            label="Shopify Admin API",
            version=DEFAULT_API_VERSION,
            eol=VERSION_EOL.get(DEFAULT_API_VERSION),
            failure_mode=_lifecycle.FAIL_SILENT,
            owner="src/ecommerce_tools/shopify_connector.py",
            doc_url="https://shopify.dev/docs/api/usage/versioning",
            remediation=(
                "把 DEFAULT_API_VERSION 提到最新稳定版，并把新版「可访问至」照抄进 "
                "VERSION_EOL。升级前先读该版 changelog 的破坏性变更——Shopify 只在"
                "新版引入破坏性变更，不会改动你正在用的版本。"
            ),
        )
    ]


class ShopifyConnector:
    name = "shopify"

    def __init__(
        self,
        *,
        shop: str,
        access_token: str,
        api_version: str = "",
        http_get: Optional[HttpGet] = None,
        timeout: float = 15.0,
    ) -> None:
        # 归一 shop：去协议/尾斜杠，补 .myshopify.com（若给的是裸 handle）
        s = str(shop or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
        if s and "." not in s:
            s = f"{s}.myshopify.com"
        self._shop = s
        self._token = str(access_token or "")
        _warn_if_stale(api_version)
        self._api_version = str(api_version or DEFAULT_API_VERSION)
        self._http_get = http_get
        self._timeout = float(timeout or 15.0)

    def _base(self) -> str:
        return f"https://{self._shop}/admin/api/{self._api_version}"

    def _headers(self) -> dict:
        return {"X-Shopify-Access-Token": self._token,
                "Content-Type": "application/json"}

    async def _get(self, url: str) -> Optional[dict]:
        if not self._shop or not self._token:
            return None
        try:
            if self._http_get is not None:
                resp = await self._http_get(url, self._headers())
            else:
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(url, headers=self._headers())
            if getattr(resp, "status_code", 0) != 200:
                return None
            return resp.json()
        except Exception as ex:
            logger.debug("Shopify GET 失败 %s: %s", url, ex)
            return None

    @staticmethod
    def _map_shipment(fulfillments: list) -> Optional[ShipmentInfo]:
        if not fulfillments:
            return None
        f = fulfillments[0] or {}
        tn = f.get("tracking_number") or ""
        if not tn and isinstance(f.get("tracking_numbers"), list) and f["tracking_numbers"]:
            tn = f["tracking_numbers"][0]
        return ShipmentInfo(
            tracking_no=str(tn or ""),
            carrier=str(f.get("tracking_company") or ""),
            status=str(f.get("shipment_status") or f.get("status") or ""),
            last_event=str(f.get("status") or ""),
            last_event_at=str(f.get("updated_at") or ""),
            eta="",
        )

    @staticmethod
    def _customer_name(order: dict) -> str:
        c = order.get("customer") or {}
        name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x).strip()
        return name or str(order.get("email") or "")

    def _map_order(self, order: dict) -> OrderInfo:
        items = [
            {"sku": li.get("sku", ""), "name": li.get("title", ""),
             "qty": li.get("quantity", 0)}
            for li in (order.get("line_items") or [])
        ]
        status = (order.get("fulfillment_status")
                  or order.get("financial_status") or "")
        return OrderInfo(
            order_no=str(order.get("name") or order.get("order_number") or "").lstrip("#"),
            status=str(status),
            currency=str(order.get("currency") or ""),
            total=str(order.get("total_price") or ""),
            items=items,
            customer_name=self._customer_name(order),
            customer_email=str(order.get("email") or ""),
            created_at=str(order.get("created_at") or ""),
            shipment=self._map_shipment(order.get("fulfillments") or []),
        )

    async def get_order(self, order_no: str) -> Optional[OrderInfo]:
        key = str(order_no or "").strip()
        if not key:
            return None
        # Shopify 订单号 name 带 # 前缀；按 name 查
        name = key if key.startswith("#") else f"#{key}"
        from urllib.parse import quote
        url = f"{self._base()}/orders.json?status=any&name={quote(name)}"
        data = await self._get(url)
        if not data:
            return None
        orders = data.get("orders") or []
        if not orders:
            return None
        return self._map_order(orders[0])

    async def track_shipment(self, tracking_no: str) -> Optional[ShipmentInfo]:
        # Shopify Admin 无「按运单号直查物流」端点；返回 None → 上层如实告知查不到。
        return None
