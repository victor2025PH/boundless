"""托管代理（一键代理）——供给方适配层 + 选区/报价纯函数。

一键代理 = 坐席点一下 → 按账号手机号归属地（或显式指定地区）自动匹配一条干净
出口 IP → 从 Token 钱包扣费 → 写进代理池并绑定该账号。本模块只管**供给侧**
（能力探测 / 选区 / 报价 / 真实开通），钱的幂等属 ``proxy_subscription``
（UNIQUE(charge_ref)），落库与绑定属 ``proxy_pool``。

三层刻意分开的理由：

- **选区与报价是纯函数**，可离线穷举测试，且是「对客报价」与「实际扣费」的
  单一事实源——两边调同一个 ``quote_tokens``，结构上不可能出现「页面显示 300、
  实扣 500」这类最伤信任的分叉。
- **供给方各家契约不同**、需要凭据、离线不可测 → 收在 Provider 类里；未配置
  即 ``build_provider() -> None``，能力探测据此 fail-hidden（UI 不出死按钮）。
- **钱的幂等不在这里**：``token_ledger.record_spend`` 是按天聚合、**无幂等键**的
  （同 (钱包,日,动作) 累加），双击就是双扣——必须由订阅表的唯一约束兜住。

## 三个供给方（同一接口，可平滑升级）

- ``stock``（**默认**）：从**自有库存**分配——运营按老办法批量买好 IP 灌进代理池，
  一键就从「未占用 + 不在冷却 + 地区/类型匹配」的存货里挑一条。**零外部依赖，
  今天就能真跑**：一键代理的用户体验与计费闭环不必等任何供应商 API 凭据。
- ``http``：**契约由配置驱动**的通用上游适配器（endpoint / 方法 / 字段映射全在
  配置里）。刻意**不把任何厂商的 API 形状硬编进代码**——没有厂商文档在手就写死
  字段名，等于把「看起来实现了、线上必失败」埋进代码，还可能对上游账户误下单。
  拿到真实文档后填配置即可，无需改代码；将来验证过的厂商可另加具名预设。
- ``mock``：确定性假开通，仅供测试/演练。产出的**不是可用出口**，绝不可当生产档。

## 安全默认

``proxies.managed.enabled`` 默认 False；未配置供给方或未配置价格（价格 ≤ 0）时
``capability()`` 恒 ``available=False``——「配置笔误 → 白送真金白银买来的 IP」
是本模块最需要防的事故，故宁可整功能不可用，也不按 0 元放行。
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROVIDER_STOCK = "stock"
PROVIDER_HTTP = "http"
PROVIDER_MOCK = "mock"
VALID_PROVIDERS = (PROVIDER_STOCK, PROVIDER_HTTP, PROVIDER_MOCK)

# 不可购买的原因码（稳定值，路由据此出 i18n 文案；勿改字面量）
REASON_DISABLED = "disabled"
REASON_NO_PROVIDER = "no_provider"
REASON_NO_PRICING = "no_pricing"
REASON_NO_CREDENTIALS = "no_credentials"

DEFAULT_PERIOD_DAYS = 30
DEFAULT_EXPIRY_WARN_DAYS = 5
# 地区溢价倍率的合法区间：配置笔误（多打一个 0）不该变成 10 倍账单或白送。
MULTIPLIER_MIN = 0.1
MULTIPLIER_MAX = 10.0

# ── 手机号国际区号 → ISO 3166-1 alpha-2 ────────────────────────────────────────
# 覆盖本产品实际服务的市场（东南亚 / 东亚 / 欧洲 / 美洲 / 中东）。共享区号按
# **主要市场**归一（+1→US 而非 CA、+7→RU 而非 KZ）：选区只影响挑哪条出口 IP，
# 猜错的代价是「同一大区里选了邻国」，远小于「因无法判定而不给用户开通」。
PHONE_CC_TO_COUNTRY: Dict[str, str] = {
    "1": "US", "7": "RU", "20": "EG", "27": "ZA", "30": "GR", "31": "NL",
    "32": "BE", "33": "FR", "34": "ES", "36": "HU", "39": "IT", "40": "RO",
    "41": "CH", "43": "AT", "44": "GB", "45": "DK", "46": "SE", "47": "NO",
    "48": "PL", "49": "DE", "51": "PE", "52": "MX", "53": "CU", "54": "AR",
    "55": "BR", "56": "CL", "57": "CO", "58": "VE", "60": "MY", "61": "AU",
    "62": "ID", "63": "PH", "64": "NZ", "65": "SG", "66": "TH", "81": "JP",
    "82": "KR", "84": "VN", "86": "CN", "90": "TR", "91": "IN", "92": "PK",
    "93": "AF", "94": "LK", "95": "MM", "98": "IR",
    "212": "MA", "213": "DZ", "216": "TN", "218": "LY", "220": "GM",
    "233": "GH", "234": "NG", "251": "ET", "254": "KE", "255": "TZ",
    "256": "UG", "260": "ZM", "263": "ZW", "351": "PT", "352": "LU",
    "353": "IE", "354": "IS", "355": "AL", "358": "FI", "359": "BG",
    "370": "LT", "371": "LV", "372": "EE", "380": "UA", "381": "RS",
    "385": "HR", "386": "SI", "420": "CZ", "421": "SK", "852": "HK",
    "853": "MO", "855": "KH", "856": "LA", "880": "BD", "886": "TW",
    "960": "MV", "962": "JO", "963": "SY", "964": "IQ", "965": "KW",
    "966": "SA", "968": "OM", "971": "AE", "972": "IL", "973": "BH",
    "974": "QA", "975": "BT", "976": "MN", "977": "NP", "992": "TJ",
    "993": "TM", "994": "AZ", "995": "GE", "996": "KG", "998": "UZ",
}

# 长号先匹配（"852" 必须先于 "85"/"8" 被试），一次算好避免每次调用重排。
_CC_PREFIXES: Tuple[str, ...] = tuple(
    sorted(PHONE_CC_TO_COUNTRY, key=len, reverse=True))

# alpha-3 → alpha-2（供应商余量/目录接口常回 alpha3，如 Proxy-Seller 的 "FRA"）。
# 覆盖我们实际服务的市场（与 PHONE_CC_TO_COUNTRY 同口径）；表外码宁可丢弃也不猜。
ALPHA3_TO_ALPHA2: Dict[str, str] = {
    "USA": "US", "RUS": "RU", "EGY": "EG", "ZAF": "ZA", "GRC": "GR",
    "NLD": "NL", "BEL": "BE", "FRA": "FR", "ESP": "ES", "HUN": "HU",
    "ITA": "IT", "ROU": "RO", "CHE": "CH", "AUT": "AT", "GBR": "GB",
    "DNK": "DK", "SWE": "SE", "NOR": "NO", "POL": "PL", "DEU": "DE",
    "PER": "PE", "MEX": "MX", "CUB": "CU", "ARG": "AR", "BRA": "BR",
    "CHL": "CL", "COL": "CO", "VEN": "VE", "MYS": "MY", "AUS": "AU",
    "IDN": "ID", "PHL": "PH", "NZL": "NZ", "SGP": "SG", "THA": "TH",
    "JPN": "JP", "KOR": "KR", "VNM": "VN", "CHN": "CN", "TUR": "TR",
    "IND": "IN", "PAK": "PK", "AFG": "AF", "LKA": "LK", "MMR": "MM",
    "IRN": "IR", "MAR": "MA", "DZA": "DZ", "TUN": "TN", "LBY": "LY",
    "GMB": "GM", "GHA": "GH", "NGA": "NG", "ETH": "ET", "KEN": "KE",
    "TZA": "TZ", "UGA": "UG", "ZMB": "ZM", "ZWE": "ZW", "PRT": "PT",
    "LUX": "LU", "IRL": "IE", "ISL": "IS", "ALB": "AL", "FIN": "FI",
    "BGR": "BG", "LTU": "LT", "LVA": "LV", "EST": "EE", "UKR": "UA",
    "SRB": "RS", "HRV": "HR", "SVN": "SI", "CZE": "CZ", "SVK": "SK",
    "HKG": "HK", "MAC": "MO", "KHM": "KH", "LAO": "LA", "BGD": "BD",
    "TWN": "TW", "MDV": "MV", "JOR": "JO", "SYR": "SY", "IRQ": "IQ",
    "KWT": "KW", "SAU": "SA", "OMN": "OM", "ARE": "AE", "ISR": "IL",
    "BHR": "BH", "QAT": "QA", "BTN": "BT", "MNG": "MN", "NPL": "NP",
    "TJK": "TJ", "TKM": "TM", "AZE": "AZ", "GEO": "GE", "KGZ": "KG",
    "UZB": "UZ", "CAN": "CA", "KAZ": "KZ",
}


def normalize_country_any(code: Any) -> str:
    """任意供应商国家标识 → ISO alpha-2 大写；认不出返回空串（丢弃不猜）。"""
    c = str(code or "").strip().upper()
    if len(c) == 2:
        return c
    if len(c) == 3:
        return ALPHA3_TO_ALPHA2.get(c, "")
    return ""

_NON_DIGIT_RE = re.compile(r"\D+")


def normalize_phone(phone: Any) -> str:
    """手机号归一为纯数字串（去 +/空格/横杠/括号，去国际拨出前缀 00）。"""
    digits = _NON_DIGIT_RE.sub("", str(phone or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def country_for_phone(phone: Any) -> str:
    """手机号 → ISO 国家码（最长区号前缀匹配）；判不出返回空串。

    **判不出就返回空**而不是猜一个默认值：调用方需要区分「按号码定位到 JP」与
    「不知道、用的兜底默认」——前者可以对用户说「已按你的号码匹配日本 IP」，
    后者说这句话就是撒谎。
    """
    digits = normalize_phone(phone)
    if not digits:
        return ""
    for cc in _CC_PREFIXES:
        if digits.startswith(cc):
            return PHONE_CC_TO_COUNTRY[cc]
    return ""


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_managed_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从整份 config 抽出并归一 ``proxies.managed`` 段（缺省全给安全值）。

    归一后的形状是本模块所有纯函数的唯一入参口径——路由/测试都先过这一层，
    避免「有的地方读 by_kind、有的地方读 byKind」这类同义分叉。
    """
    root = cfg if isinstance(cfg, dict) else {}
    proxies = root.get("proxies") if isinstance(root.get("proxies"), dict) else {}
    m = proxies.get("managed") if isinstance(proxies.get("managed"), dict) else {}

    pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
    by_kind = pricing.get("by_kind") if isinstance(pricing.get("by_kind"), dict) else {}
    mult = (pricing.get("country_multiplier")
            if isinstance(pricing.get("country_multiplier"), dict) else {})

    provider = str(m.get("provider") or PROVIDER_STOCK).strip().lower()
    if provider not in VALID_PROVIDERS:
        provider = ""

    life = m.get("lifecycle") if isinstance(m.get("lifecycle"), dict) else {}

    return {
        "enabled": bool(m.get("enabled", False)),
        "provider": provider,
        "default_kind": str(m.get("default_kind") or "isp").strip().lower(),
        "default_country": str(m.get("default_country") or "US").strip().upper(),
        "allow_countries": [str(c).strip().upper()
                            for c in (m.get("allow_countries") or []) if str(c).strip()],
        "period_days": max(1, _as_int(m.get("period_days"), DEFAULT_PERIOD_DAYS)),
        "auto_renew": bool(m.get("auto_renew", False)),
        "expiry_warn_days": max(
            0, _as_int(m.get("expiry_warn_days"), DEFAULT_EXPIRY_WARN_DAYS)),
        "price_default": _as_int(pricing.get("default"), 0),
        "price_by_kind": {str(k).strip().lower(): _as_int(v, 0)
                          for k, v in by_kind.items()},
        "country_multiplier": {str(k).strip().upper(): _as_float(v, 1.0)
                               for k, v in mult.items()},
        "http": m.get("http") if isinstance(m.get("http"), dict) else {},
        # ── P2：开通即体检 / 免费换货 / 生命周期巡检 ──────────────────────────
        # verify_on_provision 只对 stock 硬闸（自有库存换一条零成本）；http 供给方
        # 探测只作记录不拦交付（上游已计费，且新开出口对我们的探测器可能假阴）。
        "verify_on_provision": bool(m.get("verify_on_provision", True)),
        "verify_max_attempts": min(
            5, max(1, _as_int(m.get("verify_max_attempts"), 3))),
        "swap_window_days": max(0, _as_int(m.get("swap_window_days"), 3)),
        "max_swaps": max(0, _as_int(m.get("max_swaps"), 3)),
        "lifecycle": {
            "interval_min": max(5, _as_int(life.get("interval_min"), 60)),
            "renew_before_days": max(
                0, _as_int(life.get("renew_before_days"), 3)),
            "min_stock": max(0, _as_int(life.get("min_stock"), 2)),
            "low_stock_countries": [
                str(c).strip().upper()
                for c in (life.get("low_stock_countries") or []) if str(c).strip()],
        },
    }


def quote_tokens(mcfg: Dict[str, Any], *, kind: str, country: str) -> int:
    """报价（Token/期）：``by_kind`` 基价 × 地区稀缺倍率，向上取整。

    唯一定价入口——展示报价与实际扣费必须调用它，不许任何调用方自己算。
    返回 0 = **不可购买**（未配价或配成 0）：白送真金白银买来的出口是本模块
    最需要防的事故，故 0 一律当「没配好」处理，绝不当「免费」放行。
    """
    k = str(kind or "").strip().lower()
    base = _as_int(mcfg.get("price_by_kind", {}).get(k), 0)
    if base <= 0:
        base = _as_int(mcfg.get("price_default"), 0)
    if base <= 0:
        return 0
    mult = _as_float(
        mcfg.get("country_multiplier", {}).get(str(country or "").strip().upper()), 1.0)
    mult = min(MULTIPLIER_MAX, max(MULTIPLIER_MIN, mult))
    return max(1, int(math.ceil(base * mult)))


def resolve_country(
    mcfg: Dict[str, Any], *, requested: str = "", phone: str = "",
) -> Tuple[str, str]:
    """定选区 → ``(country, source)``，source ∈ ``requested|phone|default``。

    优先级：坐席显式选择 > 账号手机号归属地 > 配置默认。带回 source 是为了让
    UI 能诚实地说「已按你的号码匹配 🇯🇵 日本」还是「用的默认地区」——两句话
    对用户的可信度完全不同，混成一句就是编故事。

    ``allow_countries`` 非空时作为白名单：不在名单内的推导结果回落默认地区
    （运营只囤了几个地区的货时，不该让用户选到永远开不出来的地区）。
    """
    allow = mcfg.get("allow_countries") or []
    default = str(mcfg.get("default_country") or "US").strip().upper()

    req = str(requested or "").strip().upper()
    if req and (not allow or req in allow):
        return req, "requested"

    guess = country_for_phone(phone)
    if guess and (not allow or guess in allow):
        return guess, "phone"

    return default, "default"


def period_end(mcfg: Dict[str, Any], *, now: Optional[float] = None) -> float:
    """本期到期时间戳（now + period_days）。"""
    base = time.time() if now is None else float(now)
    return base + float(mcfg.get("period_days") or DEFAULT_PERIOD_DAYS) * 86400.0


def capability(mcfg: Dict[str, Any]) -> Dict[str, Any]:
    """能力探测（纯函数）：一键代理现在能不能真的下单。

    ``available=False`` 时 UI 必须**整块隐藏一键卡**（fail-hidden），只留手动录入
    ——放一个点了就报错的按钮，比没有这个功能更伤。``reason`` 供后台排障，
    不直接甩给终端用户。
    """
    out: Dict[str, Any] = {
        "enabled": bool(mcfg.get("enabled")),
        "available": False,
        "provider": str(mcfg.get("provider") or ""),
        "reason": "",
        "period_days": int(mcfg.get("period_days") or DEFAULT_PERIOD_DAYS),
        "auto_renew": bool(mcfg.get("auto_renew")),
        "default_kind": str(mcfg.get("default_kind") or "isp"),
        "default_country": str(mcfg.get("default_country") or "US"),
        "allow_countries": list(mcfg.get("allow_countries") or []),
    }
    if not out["enabled"]:
        out["reason"] = REASON_DISABLED
        return out
    if not out["provider"]:
        out["reason"] = REASON_NO_PROVIDER
        return out
    if out["provider"] == PROVIDER_HTTP and not str(
            (mcfg.get("http") or {}).get("url") or "").strip():
        out["reason"] = REASON_NO_CREDENTIALS
        return out
    # 价格必须为正：默认档与 by_kind 全为 0 = 没配好，不放行（见 quote_tokens）。
    probe = quote_tokens(
        mcfg, kind=out["default_kind"], country=out["default_country"])
    if probe <= 0:
        out["reason"] = REASON_NO_PRICING
        return out
    out["available"] = True
    return out


# ── 供给方 ────────────────────────────────────────────────────────────────────

@dataclass
class ProvisionResult:
    """一次开通的结果（成功才带连接参数）。

    ``order_ref`` = 上游订单号/库存代理 id，用于**对账**：将来上游账单与我们的
    订阅表逐笔核对靠它，故即使失败也尽量带上。
    """

    ok: bool
    scheme: str = "socks5"
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    country: str = ""
    kind: str = "unknown"
    order_ref: str = ""
    # 上游给出的到期（0=上游未告知，按本地 period_days 记）
    expires_at: float = 0.0
    # stock 供给方复用已在池内的代理时带回其 id：调用方**不得**再 add 一条
    existing_proxy_id: str = ""
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class ProxyProviderBase:
    """供给方接口：给定地区/类型，交付一条可用出口（或如实失败）。

    ``idempotency_key``：调用方的扣费幂等键（charge_ref）透传给上游——主流供应商
    （1001Proxy 等）的下单接口原生支持幂等键，透传后「我们侧幂等 + 上游侧幂等」
    双保险，网络重试不会在上游产生第二张订单。不支持的供给方忽略即可。
    """

    name = "base"

    async def provision(
        self, *, country: str, kind: str, period_days: int, label: str = "",
        idempotency_key: str = "",
    ) -> ProvisionResult:
        raise NotImplementedError

    def inventory(self, *, kind: str = "") -> Dict[str, int]:
        """各地区可交付数量（不支持盘点的供给方返回空 dict = 不展示存量）。

        计数语义：``n>0`` 精确存量；``-1`` = **有货但供应商不给数**（JIT 上游的
        目录级余量——能买、别显示具体条数）；不含该国 = 无货/不可买。
        """
        return {}

    async def inventory_async(self, *, kind: str = "") -> Dict[str, int]:
        """异步盘点（默认退化为同步实现）。

        路由侧一律走这个入口：http 供给方的余量要真打上游（带 TTL 缓存），
        同步接口在事件循环里打网络会卡整个 web 层。stock/mock 零成本直返。
        """
        return self.inventory(kind=kind)

    async def vendor_balance(self) -> Optional[float]:
        """上游预付余额（未配置/不支持 → None）。JIT 世界里余额就是库存。"""
        return None

    async def prolong(
        self, *, order_ref: str, period_days: int,
    ) -> Tuple[bool, float, str]:
        """供应商侧续期 → ``(ok, 上游新到期时间戳或0, 失败原因)``。

        stock/mock 的到期只存在于**我们的**账期时钟上（自有库存没有上游时钟），
        续期天然成功、零外部调用。http 供给方见子类——JIT 模式下 IP 活在上游
        时钟上，**只延我们的账期不延上游＝收了续费、IP 期中死掉**，故 http 未配
        prolong 时续期必须被拦下（宁可不收这笔钱）。
        """
        return True, 0.0, ""

    async def replace(
        self, *, order_ref: str, country: str = "", kind: str = "",
    ) -> Optional["ProvisionResult"]:
        """供应商侧换 IP（免费换货配额）→ 新连接参数；``None``＝不支持。

        返回 None 时调用方回落「重新 provision」（对 stock 是换一条自有库存
        零成本；对 http 是**再买一单**真花钱——所以上游支持 replace 就该用）。
        """
        return None


class StockProxyProvider(ProxyProviderBase):
    """自有库存供给方：从代理池里挑一条未占用、不在冷却的存货。

    运营按老办法批量采购 IP 灌进代理池即可——**一键代理的用户体验与计费闭环
    不依赖任何上游 API**。挑不到货时如实失败（``error='out_of_stock'``），由路由
    转成「该地区暂时缺货」的可行动提示，绝不静默换个地区顶包（用户点的是
    「日本 IP」，给一条美国的属于货不对板）。
    """

    name = PROVIDER_STOCK

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def provision(
        self, *, country: str, kind: str, period_days: int, label: str = "",
        idempotency_key: str = "",
    ) -> ProvisionResult:
        try:
            entry = self._pool.pick_available(kind=kind or None,
                                              country=country or None)
            # 严格类型挑不到 → 放宽 kind 但**地区仍然硬匹配**（地区是用户的明确
            # 诉求，类型多是内部分级；住宅↔ISP 对用户是同一件事）。
            if not entry:
                entry = self._pool.pick_available(country=country or None)
            if not entry:
                return ProvisionResult(ok=False, country=country, kind=kind,
                                       error="out_of_stock")
            return ProvisionResult(
                ok=True,
                scheme=str(entry.get("scheme") or "socks5"),
                host=str(entry.get("host") or ""),
                port=int(entry.get("port") or 0),
                country=str(entry.get("country") or country or ""),
                kind=str(entry.get("kind") or kind or "unknown"),
                order_ref=str(entry.get("proxy_id") or ""),
                existing_proxy_id=str(entry.get("proxy_id") or ""),
            )
        except Exception as ex:  # noqa: BLE001 - 供给方异常一律转失败，不炸路由
            logger.warning("[proxy_provider] stock 取货失败: %s", ex, exc_info=True)
            return ProvisionResult(ok=False, country=country, kind=kind,
                                   error="stock_error")

    def inventory(self, *, kind: str = "") -> Dict[str, int]:
        """按地区盘点可交付存量（供 UI 显示「日本 剩 3 条」并禁掉缺货地区）。"""
        out: Dict[str, int] = {}
        try:
            for p in self._pool.list():
                if p.get("assigned") or p.get("in_cooldown"):
                    continue
                if str(p.get("status") or "") == "fail":
                    continue
                if kind and str(p.get("kind") or "") != kind:
                    continue
                c = str(p.get("country") or "").strip().upper()
                if not c:
                    continue
                out[c] = out.get(c, 0) + 1
        except Exception:
            logger.debug("[proxy_provider] inventory 盘点失败（按空）", exc_info=True)
        return out


class MockProxyProvider(ProxyProviderBase):
    """确定性假开通——**仅测试/演练**。产出的不是可用出口，绝不可当生产档。"""

    name = PROVIDER_MOCK

    async def provision(
        self, *, country: str, kind: str, period_days: int, label: str = "",
        idempotency_key: str = "",
    ) -> ProvisionResult:
        tag = secrets.token_hex(3)
        return ProvisionResult(
            ok=True, scheme="socks5", host=f"mock-{tag}.invalid", port=1080,
            username=f"u{tag}", password=f"p{tag}",
            country=str(country or "US").upper(), kind=str(kind or "isp"),
            order_ref=f"mock:{tag}",
        )


# http 供给方的余量/余额探测缓存（模块级：实例按请求即建即弃，状态必须外置；
# 与 VisionClient 端点冷却同哲学）。键=url，值=(ts, data)。上游都有请求频率限制
# （Proxy-Seller 实测有 "Request limit reached" 错误码），status 端点又随卡片渲染
# 高频轮询——不带 TTL 缓存等于拿供应商限额换 UI 刷新。
_HTTP_PROBE_CACHE: Dict[str, Tuple[float, Any]] = {}
_HTTP_PROBE_LOCK = threading.Lock()
DEFAULT_PROBE_TTL_SEC = 300.0


def reset_http_probe_cache() -> None:
    """测试钩子：清余量/余额探测缓存。"""
    with _HTTP_PROBE_LOCK:
        _HTTP_PROBE_CACHE.clear()


class HttpProxyProvider(ProxyProviderBase):
    """通用上游适配器——**契约由配置驱动**，代码里不硬编任何厂商 API 形状。

    配置 ``proxies.managed.http``：

    - ``url`` / ``method`` / ``headers`` / ``timeout_sec``
    - ``body``：请求体模板，``{country}`` / ``{kind}`` / ``{period_days}`` /
      ``{label}`` / ``{idempotency_key}`` / ``{country_id}`` / ``{period_id}``
      占位符按本次请求替换
    - ``country_ids`` / ``period_ids``：ISO 码/天数 → 厂商内部 ID 的静态映射
      （Proxy-Seller 这类按内部 ID 下单的厂商，开户后从 reference 接口抄一次）
    - ``map``：响应字段路径映射（点号路径支持嵌套/数组下标）
    - ``credentials``：两步式上游的第二击（下单→取凭据），见 provision
    - ``inventory``：余量拉取 ``{url, items, country, count?, ttl_sec}``——
      count 缺省＝目录级「有货不给数」（计 -1）；country 字段 alpha2/alpha3 均认
    - ``balance``：余额拉取 ``{url, field, min_alert}``——JIT 模式的「库存水位」
    - ``orders``：对账单拉取 ``{url, items, order_ref, cost?}``——对账 CLI 用

    为什么不写死某家厂商：没有厂商文档在手就照猜写死字段名，等于把「看起来实现了、
    线上必定失败」埋进代码，还可能对上游账户误下单。拿到真实文档填配置即可；
    将来真验证过的厂商可另加具名预设，但**验证过**才配进代码。
    """

    name = PROVIDER_HTTP

    def __init__(self, http_cfg: Dict[str, Any]) -> None:
        self._cfg = http_cfg or {}

    def _base_repl(self, **extra: Any) -> Dict[str, Any]:
        """占位符基础集：country/period 的厂商内部 ID 由静态映射表推导。"""
        repl = dict(extra)
        cmap = (self._cfg.get("country_ids")
                if isinstance(self._cfg.get("country_ids"), dict) else {})
        pmap = (self._cfg.get("period_ids")
                if isinstance(self._cfg.get("period_ids"), dict) else {})
        repl["country_id"] = str(
            cmap.get(str(repl.get("country") or "").upper(), ""))
        repl["period_id"] = str(
            pmap.get(str(repl.get("period_days") or ""), ""))
        return repl

    async def _cached_probe(self, section: Dict[str, Any], *,
                            ttl: float) -> Any:
        """带 TTL 缓存的只读探测（余量/余额共用；失败缓存空值防打爆上游）。"""
        url = str(section.get("url") or "").strip()
        if not url:
            return None
        now = time.time()
        with _HTTP_PROBE_LOCK:
            hit = _HTTP_PROBE_CACHE.get(url)
            if hit and now - hit[0] < ttl:
                return hit[1]
        data = None
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(
                total=_as_float(self._cfg.get("timeout_sec"), 30.0))
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                data = await self._request(sess, section, self._base_repl(),
                                           default_method="GET")
        except Exception:
            logger.debug("[proxy_provider] http 探测失败: %s", url, exc_info=True)
        with _HTTP_PROBE_LOCK:
            _HTTP_PROBE_CACHE[url] = (now, data)
        return data

    async def inventory_async(self, *, kind: str = "") -> Dict[str, int]:
        """按配置拉上游余量/目录。空 dict＝未配置或拉取失败（调用方 fail-open）。"""
        inv_cfg = (self._cfg.get("inventory")
                   if isinstance(self._cfg.get("inventory"), dict) else {})
        if not inv_cfg.get("url"):
            return {}
        data = await self._cached_probe(
            inv_cfg, ttl=_as_float(inv_cfg.get("ttl_sec"),
                                   DEFAULT_PROBE_TTL_SEC))
        items = self._dig(data, str(inv_cfg.get("items") or ""))
        if not isinstance(items, list):
            return {}
        out: Dict[str, int] = {}
        c_field = str(inv_cfg.get("country") or "country")
        n_field = str(inv_cfg.get("count") or "")
        for it in items:
            cc = normalize_country_any(self._dig(it, c_field))
            if not cc:
                continue
            if n_field:
                out[cc] = _as_int(self._dig(it, n_field), 0)
            else:
                out[cc] = -1  # 目录级：有货但不给数（UI 不显示条数、可买）
        return out

    async def vendor_balance(self) -> Optional[float]:
        bal_cfg = (self._cfg.get("balance")
                   if isinstance(self._cfg.get("balance"), dict) else {})
        if not bal_cfg.get("url"):
            return None
        data = await self._cached_probe(
            bal_cfg, ttl=_as_float(bal_cfg.get("ttl_sec"),
                                   DEFAULT_PROBE_TTL_SEC))
        if data is None:
            return None
        v = self._dig(data, str(bal_cfg.get("field") or "balance"))
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    async def prolong(
        self, *, order_ref: str, period_days: int,
    ) -> Tuple[bool, float, str]:
        """按配置打上游续期接口。

        配置 ``http.prolong``：``{url, method, body, map: {expires_at}}``，
        占位符含 ``{order_ref}`` / ``{period_days}`` / ``{period_id}``。
        ``assume_ok: true`` = 上游自己会自动续（有的厂商余额充足即自动扣续），
        我们侧直接放行——显式声明的例外，不是缺省。未配置 → 拦下续期。
        """
        p_cfg = (self._cfg.get("prolong")
                 if isinstance(self._cfg.get("prolong"), dict) else {})
        if p_cfg.get("assume_ok"):
            return True, 0.0, ""
        if not str(p_cfg.get("url") or "").strip():
            return False, 0.0, "vendor_prolong_unconfigured"
        repl = self._base_repl(order_ref=order_ref, period_days=period_days,
                               country="", kind="")
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(
                total=_as_float(self._cfg.get("timeout_sec"), 30.0))
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                data = await self._request(sess, p_cfg, repl,
                                           default_method="POST")
        except Exception as ex:  # noqa: BLE001
            logger.warning("[proxy_provider] 上游续期失败 order=%s: %s",
                           order_ref, ex, exc_info=True)
            return False, 0.0, "vendor_prolong_failed"
        pmap = p_cfg.get("map") if isinstance(p_cfg.get("map"), dict) else {}
        expires = _as_float(
            self._dig(data, str(pmap.get("expires_at") or "expires_at")), 0.0)
        return True, expires, ""

    async def replace(
        self, *, order_ref: str, country: str = "", kind: str = "",
    ) -> Optional[ProvisionResult]:
        """按配置打上游换 IP 接口（免费更换配额，不产生新订单）。

        配置 ``http.replace``：``{url, method, body, map}``（map 与 credentials
        同形：host/port/username/password/expires_at）。未配置 → None，调用方
        回落重新 provision（那是再买一单，真花钱）。
        """
        r_cfg = (self._cfg.get("replace")
                 if isinstance(self._cfg.get("replace"), dict) else {})
        if not str(r_cfg.get("url") or "").strip():
            return None
        repl = self._base_repl(order_ref=order_ref, country=country, kind=kind)
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(
                total=_as_float(self._cfg.get("timeout_sec"), 30.0))
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                data = await self._request(sess, r_cfg, repl,
                                           default_method="POST")
        except Exception as ex:  # noqa: BLE001
            logger.warning("[proxy_provider] 上游换IP失败 order=%s: %s",
                           order_ref, ex, exc_info=True)
            return ProvisionResult(ok=False, error="vendor_replace_failed",
                                   order_ref=order_ref)
        rmap = r_cfg.get("map") if isinstance(r_cfg.get("map"), dict) else {}
        host = str(self._dig(data, str(rmap.get("host") or "host")) or "").strip()
        port = _as_int(self._dig(data, str(rmap.get("port") or "port")), 0)
        if not host or not port:
            return ProvisionResult(ok=False, error="upstream_bad_payload",
                                   order_ref=order_ref)
        return ProvisionResult(
            ok=True, host=host, port=port,
            scheme=str(self._dig(data, str(rmap.get("scheme") or "scheme"))
                       or self._cfg.get("default_scheme") or "socks5").lower(),
            username=str(self._dig(
                data, str(rmap.get("username") or "username")) or ""),
            password=str(self._dig(
                data, str(rmap.get("password") or "password")) or ""),
            country=str(country or "").upper(), kind=str(kind or "unknown"),
            order_ref=order_ref,  # 换 IP 不换单：对账口径上还是同一张订单
            expires_at=_as_float(self._dig(
                data, str(rmap.get("expires_at") or "expires_at")), 0.0),
        )

    async def calc_order(
        self, *, country: str, kind: str, period_days: int,
    ) -> Optional[Any]:
        """试算下单（**不花钱**）——开户日联调/核价用（proxy_vendor_probe CLI）。

        配置 ``http.calc``（形同主下单段：url/method/body/占位符全套）；返回
        **原始响应**不做映射——联调时人要看的就是上游原文。未配置 → None。
        """
        c_cfg = (self._cfg.get("calc")
                 if isinstance(self._cfg.get("calc"), dict) else {})
        if not str(c_cfg.get("url") or "").strip():
            return None
        repl = self._base_repl(country=country, kind=kind,
                               period_days=period_days)
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(
                total=_as_float(self._cfg.get("timeout_sec"), 30.0))
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                return await self._request(sess, c_cfg, repl,
                                           default_method="POST")
        except Exception as ex:  # noqa: BLE001
            logger.warning("[proxy_provider] 试算失败: %s", ex, exc_info=True)
            return {"error": str(ex)}

    async def list_vendor_orders(self) -> Optional[list]:
        """上游订单列表（对账单）→ ``[{order_ref, cost}]``；未配置 → None。

        对账 CLI 拿它与本地订阅台账互diff：上游有单我们没账＝白付了钱，
        我们有账上游没单＝根本没买到。**只读不缓存**（对账要最新数据）。
        """
        o_cfg = (self._cfg.get("orders")
                 if isinstance(self._cfg.get("orders"), dict) else {})
        if not o_cfg.get("url"):
            return None
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(
                total=_as_float(self._cfg.get("timeout_sec"), 30.0))
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                data = await self._request(sess, o_cfg, self._base_repl(),
                                           default_method="GET")
        except Exception:
            logger.warning("[proxy_provider] 对账单拉取失败", exc_info=True)
            return None
        items = self._dig(data, str(o_cfg.get("items") or ""))
        if not isinstance(items, list):
            return []
        ref_f = str(o_cfg.get("order_ref") or "id")
        cost_f = str(o_cfg.get("cost") or "")
        return [{
            "order_ref": str(self._dig(it, ref_f) or ""),
            "cost": _as_float(self._dig(it, cost_f), 0.0) if cost_f else 0.0,
        } for it in items if self._dig(it, ref_f)]

    @staticmethod
    def _dig(obj: Any, path: str) -> Any:
        cur = obj
        for part in str(path or "").split("."):
            if not part:
                continue
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return cur

    @staticmethod
    def _fill(node: Any, repl: Dict[str, Any]) -> Any:
        """递归把模板里的 ``{country}`` 等占位符换成本次请求的实参。"""
        if isinstance(node, str):
            out = node
            for k, v in repl.items():
                out = out.replace("{" + k + "}", str(v))
            return out
        if isinstance(node, dict):
            return {k: HttpProxyProvider._fill(v, repl) for k, v in node.items()}
        if isinstance(node, list):
            return [HttpProxyProvider._fill(v, repl) for v in node]
        return node

    async def _request(self, sess: Any, cfg: Dict[str, Any],
                       repl: Dict[str, Any], *, default_method: str) -> Any:
        """按配置段发一次请求并解析 JSON（url/method/headers/body 全占位符填充）。"""
        method = str(cfg.get("method") or default_method).upper()
        headers = self._fill(cfg.get("headers") or {}, repl)
        body = self._fill(cfg.get("body") or {}, repl)
        async with sess.request(
            method, self._fill(str(cfg.get("url") or ""), repl), headers=headers,
            json=body if (method != "GET" and body) else None,
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"upstream_http_{resp.status}")
            return await resp.json(content_type=None)

    async def provision(
        self, *, country: str, kind: str, period_days: int, label: str = "",
        idempotency_key: str = "",
    ) -> ProvisionResult:
        """一次开通，支持**两步式**上游（2026-08-21 调研坐实的主流形状）。

        1001Proxy / Proxy-Seller 等的下单接口只回 ``order_id``，连接凭据要再打一次
        ``GET /orders/{id}/proxies``。故除主请求外支持可选的 ``credentials`` 段：
        主响应缺 host/port 且配置了该段 → 用 ``{order_ref}`` 占位符发第二击，从其
        响应里映射凭据。仍是**纯配置驱动**——两步的 url/method/map 全在配置里，
        代码不含任何厂商 API 形状。``{idempotency_key}`` 占位符可把我们的
        charge_ref 透传给支持幂等键的上游（双侧幂等，重试不产生第二张订单）。
        """
        url = str(self._cfg.get("url") or "").strip()
        if not url:
            return ProvisionResult(ok=False, error="not_configured")
        repl: Dict[str, Any] = self._base_repl(
            country=country, kind=kind, period_days=period_days,
            label=label, idempotency_key=idempotency_key,
        )
        fmap = self._cfg.get("map") if isinstance(self._cfg.get("map"), dict) else {}
        cred_cfg = (self._cfg.get("credentials")
                    if isinstance(self._cfg.get("credentials"), dict) else {})
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(
                total=_as_float(self._cfg.get("timeout_sec"), 30.0))
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                data = await self._request(sess, self._cfg, repl,
                                           default_method="POST")
                order_ref = str(
                    self._dig(data, fmap.get("order_ref") or "order_id") or "")
                host = str(self._dig(data, fmap.get("host") or "host") or "").strip()
                port = _as_int(self._dig(data, fmap.get("port") or "port"), 0)
                cred_data = data
                cred_map = fmap
                if (not host or not port) and cred_cfg.get("url") and order_ref:
                    # 两步式：主响应只有订单号 → 按 {order_ref} 追打凭据端点
                    repl["order_ref"] = order_ref
                    cred_data = await self._request(sess, cred_cfg, repl,
                                                    default_method="GET")
                    cred_map = (cred_cfg.get("map")
                                if isinstance(cred_cfg.get("map"), dict) else {})
                    host = str(self._dig(
                        cred_data, cred_map.get("host") or "host") or "").strip()
                    port = _as_int(
                        self._dig(cred_data, cred_map.get("port") or "port"), 0)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[proxy_provider] http 开通失败: %s", ex, exc_info=True)
            err = str(ex) if str(ex).startswith("upstream_http_") else "upstream_error"
            return ProvisionResult(ok=False, error=err)

        if not host or not port:
            # 上游 200 了但没给出可用连接参数 → 明确失败，绝不写一条连不上的代理
            # 进池子再扣用户的钱。
            return ProvisionResult(ok=False, error="upstream_bad_payload",
                                   order_ref=order_ref)
        return ProvisionResult(
            ok=True, host=host, port=port,
            scheme=str(self._dig(cred_data, cred_map.get("scheme") or "scheme")
                       or self._cfg.get("default_scheme") or "socks5").lower(),
            username=str(self._dig(
                cred_data, cred_map.get("username") or "username") or ""),
            password=str(self._dig(
                cred_data, cred_map.get("password") or "password") or ""),
            country=str(self._dig(cred_data, cred_map.get("country") or "country")
                        or country or "").upper(),
            kind=str(kind or "unknown"),
            order_ref=order_ref,
            expires_at=_as_float(
                self._dig(cred_data, cred_map.get("expires_at") or "expires_at"),
                0.0),
        )


def build_provider(
    mcfg: Dict[str, Any], *, pool: Any = None,
) -> Optional[ProxyProviderBase]:
    """按配置造供给方；未启用/未配置 → None（调用方据此 fail-hidden）。"""
    if not mcfg.get("enabled"):
        return None
    provider = str(mcfg.get("provider") or "")
    if provider == PROVIDER_STOCK:
        if pool is None:
            return None
        return StockProxyProvider(pool)
    if provider == PROVIDER_MOCK:
        return MockProxyProvider()
    if provider == PROVIDER_HTTP:
        http_cfg = mcfg.get("http") or {}
        if not str(http_cfg.get("url") or "").strip():
            return None
        return HttpProxyProvider(http_cfg)
    return None


__all__ = [
    "ALPHA3_TO_ALPHA2",
    "DEFAULT_EXPIRY_WARN_DAYS",
    "DEFAULT_PERIOD_DAYS",
    "DEFAULT_PROBE_TTL_SEC",
    "PHONE_CC_TO_COUNTRY",
    "PROVIDER_HTTP",
    "PROVIDER_MOCK",
    "PROVIDER_STOCK",
    "REASON_DISABLED",
    "REASON_NO_CREDENTIALS",
    "REASON_NO_PRICING",
    "REASON_NO_PROVIDER",
    "VALID_PROVIDERS",
    "HttpProxyProvider",
    "MockProxyProvider",
    "ProvisionResult",
    "ProxyProviderBase",
    "StockProxyProvider",
    "build_provider",
    "capability",
    "country_for_phone",
    "normalize_country_any",
    "normalize_phone",
    "parse_managed_cfg",
    "period_end",
    "quote_tokens",
    "resolve_country",
    "reset_http_probe_cache",
]
