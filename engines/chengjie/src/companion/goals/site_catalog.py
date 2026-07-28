"""官网产品目录桥（bd2026.cc）——「聊天里能推荐什么、怎么给链接」的单一事实源。

数据文件 ``config/site_catalog.yaml``（镜像 ``products/*/product.yaml`` +
website ``order-lines.ts`` 的自助下单 plan 键；**只收公开货架产品**——
gated 高风险线（ReachX/FaceX 类）绝不入目录、绝不在聊天外发）。

职责：
- ``load_catalog``   mtime 缓存加载（改目录热生效，零重启）。
- ``pick_products``  按客户画像（need 痛点分类 / channel / occupation）确定性选品；
  目标 params.product_id 可钉死主推。
- ``build_catalog_block`` 组 prompt 注入块：产品事实 + 按推进力度的 CTA 纪律
  （soft=只聊价值不发链接；direct=可附官网自助下单深链 + 收益试算器）。
  恒带诚实红线（不承诺收益/不代收款）——与 persona 铁律同向双保险。

任何失败返回空/None，绝不抛——目录层挂了不能拖垮聊天主链路。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import offers as offers_mod

logger = logging.getLogger("SiteCatalog")

DEFAULT_CATALOG_PATH = "config/site_catalog.yaml"
DEFAULT_MAX_CHARS = 620

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"path": "", "mtime": -1.0, "data": None, "checked": 0.0}
_CHECK_INTERVAL = 5.0  # 秒；mtime stat 节流


def resolve_catalog_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``companion.goals.catalog`` 配置段（缺/异常 → {} = 默认关）。"""
    try:
        if not isinstance(cfg_root, dict):
            return {}
        goals = ((cfg_root.get("companion") or {}).get("goals") or {})
        cat = goals.get("catalog")
        return cat if isinstance(cat, dict) else {}
    except Exception:
        return {}


def catalog_path(cfg_root: Any, config_path: Any = None) -> str:
    explicit = str(resolve_catalog_cfg(cfg_root).get("path") or "").strip()
    p = Path(explicit or DEFAULT_CATALOG_PATH)
    if not p.is_absolute() and config_path:
        # config/site_catalog.yaml 与 config.yaml 同目录约定
        base = Path(config_path).parent
        if p.parts and p.parts[0] == base.name:
            p = base / Path(*p.parts[1:])
        else:
            p = base / p
    return str(p)


def load_catalog(path: str) -> Dict[str, Any]:
    """mtime 缓存加载目录 YAML。缺文件/坏 YAML → {}（缓存坏态防重复读盘）。"""
    p = str(path or "").strip()
    if not p:
        return {}
    now = time.time()
    with _CACHE_LOCK:
        if (_CACHE["path"] == p and _CACHE["data"] is not None
                and now - _CACHE["checked"] < _CHECK_INTERVAL):
            return _CACHE["data"]
        try:
            mt = Path(p).stat().st_mtime
        except OSError:
            _CACHE.update(path=p, mtime=-1.0, data={}, checked=now)
            return {}
        if _CACHE["path"] == p and _CACHE["mtime"] == mt and _CACHE["data"] is not None:
            _CACHE["checked"] = now
            return _CACHE["data"]
        try:
            import yaml
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            logger.debug("load_catalog failed: %s", p, exc_info=True)
            data = {}
        _CACHE.update(path=p, mtime=mt, data=data, checked=now)
        return data


def _products(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    prods = (catalog or {}).get("products")
    return [p for p in prods if isinstance(p, dict)] if isinstance(prods, list) else []


def _profile_text(fields: Optional[Dict[str, Any]]) -> str:
    """画像字段拼成小写匹配文本（need/channel/occupation 参与选品）。"""
    parts: List[str] = []
    for key in ("need", "channel", "occupation"):
        v = (fields or {}).get(key)
        if isinstance(v, dict):
            v = v.get("v")
        s = str(v or "").strip()
        if s:
            parts.append(s)
    return " ".join(parts).lower()


def sold_boost_map(
    catalog: Dict[str, Any],
    plan_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """plan 级成交计数折到产品级 ``{product_id: n}``（P4 选品反哺）。

    输入＝``store.sold_plan_counts()``（近窗真实成交单按 plan 计数）；同产品
    多个 plan 求和。纯函数，未知 plan 忽略。"""
    out: Dict[str, int] = {}
    if not plan_counts:
        return out
    for prod in _products(catalog):
        pid = str(prod.get("id") or "").strip().lower()
        n = 0
        for p in (prod.get("plans") or []):
            if isinstance(p, dict):
                n += int(plan_counts.get(str(p.get("key") or "").strip(), 0) or 0)
        if pid and n > 0:
            out[pid] = n
    return out


def pick_products(
    catalog: Dict[str, Any],
    fields: Optional[Dict[str, Any]] = None,
    *,
    pinned: str = "",
    limit: int = 2,
    sold: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """确定性选品：钉死的排最前；其余按 pains 关键词与画像的命中数降序；
    痛点平分时按**近窗真实成交数**（``sold``＝sold_boost_map 产物）——卖动过
    的排前（相关性第一、销量只做同分裁决，不让爆款盖过对症）；再平分按目录
    声明序。画像全空 → 纯目录序 + 销量裁决。"""
    prods = _products(catalog)
    if not prods:
        return []
    text = _profile_text(fields)
    pin = str(pinned or "").strip().lower()
    sold_map = sold or {}

    scored: List[tuple] = []
    for idx, prod in enumerate(prods):
        pid = str(prod.get("id") or "").strip().lower()
        score = 0
        if text:
            for kw in (prod.get("pains") or []):
                k = str(kw or "").strip().lower()
                if k and k in text:
                    score += 1
        pin_boost = 1 if (pin and pid == pin) else 0
        sold_n = int(sold_map.get(pid, 0) or 0)
        scored.append((-pin_boost, -score, -sold_n, idx, prod))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    lim = max(1, min(int(limit or 2), 4))
    return [row[4] for row in scored[:lim]]


def _order_url(site: Dict[str, Any], plan_key: str) -> str:
    base = str((site or {}).get("base_url") or "").rstrip("/")
    path = str((site or {}).get("order_path") or "/order")
    return f"{base}{path}?plan={plan_key}" if base and plan_key else ""


def _append_ref(url: str, ref: str) -> str:
    """给对客链接挂会话归因参数（order-hook/order_pull 按此 ref 反查目标结算）。
    只挂在我们自己域的链接上；ref 消毒 + URL 编码。

    **fragment 感知**：``https://x.cc/#autochat`` 这类锚点链接必须把 query
    插到 ``#`` 之前（``/?ref=..#autochat``）——拼在末尾会整段落进 fragment，
    服务端与前端 location.search 都永远读不到 ref（真机踩过）。"""
    r = str(ref or "").strip()
    if not url or not r:
        return url
    from urllib.parse import quote
    base, frag = (url.split("#", 1) + [""])[:2]
    sep = "&" if "?" in base else "?"
    out = f"{base}{sep}ref={quote(r[:120], safe='')}"
    return f"{out}#{frag}" if frag else out


def _pick_plan(prod: Dict[str, Any], plan_pref: str = "") -> Optional[Dict[str, Any]]:
    """按偏好从产品 plans 里挑一档。``entry``＝入门档（key/name 含 entry/入门，
    否则取列表第一档——目录约定入门在前）；其余＝hot 优先再回落首档。"""
    plans = [p for p in (prod.get("plans") or []) if isinstance(p, dict) and p.get("key")]
    if not plans:
        return None
    pref = str(plan_pref or "").strip().lower()
    if pref == "entry":
        for p in plans:
            key = str(p.get("key") or "").lower()
            name = str(p.get("name_zh") or p.get("name_en") or "").lower()
            if "entry" in key or "入门" in name or "starter" in key:
                return p
        return plans[0]  # 目录序：入门档约定排最前
    hot = next((p for p in plans if p.get("hot")), None)
    return hot or plans[0]


def entry_plan_key(prod: Dict[str, Any]) -> str:
    """产品入门档 plan key；无明确入门档（entry/starter/入门）→ ""（不回落热销）。"""
    plan = _pick_plan(prod, "entry")
    if not plan or not plan.get("key"):
        return ""
    key = str(plan["key"])
    kl = key.lower()
    nl = str(plan.get("name_zh") or plan.get("name_en") or "").lower()
    if "entry" in kl or "starter" in kl or "入门" in nl:
        return key
    return ""


def product_link(site: Dict[str, Any], prod: Dict[str, Any], *,
                 ref: str = "", plan_pref: str = "") -> str:
    """产品的对客链接：自助 plan 深链 > 落地页 url > 官网首页。
    ``plan_pref="entry"``（P11 价格敏感流失）→ 入门档深链，不发明折扣；
    默认仍 hot 优先。``ref``＝会话归因串。"""
    plan = _pick_plan(prod, plan_pref)
    if plan and plan.get("key"):
        u = _order_url(site, str(plan["key"]))
        if u:
            return _append_ref(u, ref)
    u = str(prod.get("url") or "").strip()
    if u:
        return _append_ref(u, ref)
    return _append_ref(str((site or {}).get("base_url") or "").strip(), ref)


# CTA 分级阈值：BANT 六槽已知 ≥1/3（约 2 槽，通常含痛点+预算/规模之一）
# 才适合甩自助下单链；不足先经人工客服收口（半人工成单率高于裸链接）。
CTA_ORDER_MIN_FILL = 0.34
# ROI 试算器属「价值种草」工具：acquire_and_convert 里程碑 m2（价值种草）起可给。
CTA_ROI_MIN_MILESTONE = 2


def pick_cta(
    *,
    push_level: str = "soft",
    milestone_idx: int = 0,
    bant_fill: Optional[float] = None,
    site: Optional[Dict[str, Any]] = None,
    cta_bias: str = "",
) -> str:
    """按「推进力度 × 阶段 × 资质」确定性选主 CTA（P3 分级，纯函数）。

    返回：
    - ``"order"``  direct 且资质够（bant_fill 未知按够算=旧行为）→ 自助下单深链；
    - ``"cs"``     direct 但 BANT 明显没聊透 且目录有客服号 → 人工客服收口；
    - ``"roi"``    soft 且已到价值种草段 且目录有试算器 → 工具链接种草；
    - ``""``       其余（soft 早期）→ 只聊价值不发任何链接。

    ``cta_bias``（P11 流失转向，可选）：
    - ``"cs"``  —— 信任未重建（没用起来/出了问题）：direct 有客服号时改 cs，
      不甩自助下单（仍可用 entry 深链的场景由 plan_pref 管，这里管收口方式）；
    - ``"roi"`` —— 价格敏感：soft 期**提前**给试算器（不要求 milestone≥2）；
      direct 仍出 order（换入门档由 plan_pref，不把想买的人赶回算账页）。
    """
    lvl = str(push_level or "soft").strip().lower()
    s = site or {}
    bias = str(cta_bias or "").strip().lower()
    has_support = bool(str(s.get("support_contact") or "").strip())
    has_calc = bool(str(s.get("calc_url") or "").strip())
    if lvl == "direct":
        if bias == "cs" and has_support:
            return "cs"
        # P11 价格敏感：direct 仍出 order（入门档由 plan_pref），不因
        # BANT 缺槽改客服——对方卡点是价不是「没聊透」。
        if bias == "roi":
            return "order"
        if (bant_fill is not None
                and float(bant_fill) < CTA_ORDER_MIN_FILL
                and has_support):
            return "cs"
        return "order"
    if lvl == "soft":
        roi_ready = has_calc and (
            bias == "roi"
            or int(milestone_idx or 0) >= CTA_ROI_MIN_MILESTONE)
        if roi_ready:
            return "roi"
        return ""
    return ""


def build_catalog_block(
    products: List[Dict[str, Any]],
    *,
    push_level: str = "soft",
    site: Optional[Dict[str, Any]] = None,
    lang: str = "zh",
    max_chars: int = DEFAULT_MAX_CHARS,
    link_ref: str = "",
    milestone_idx: int = 0,
    bant_fill: Optional[float] = None,
    cta: Optional[str] = None,
    plan_pref: str = "",
    cta_bias: str = "",
    offer: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """组「可推荐产品」prompt 块。无产品/推进力度 none → None（纯陪伴日不带货）。

    P3 起按 ``pick_cta`` 分级出**单一主 CTA**（此前 direct 档下单+试算+客服
    三种链接一起塞=链接大杂烩，销售感重）：
    - ``""``      soft 早期：只聊价值，不发链接不报价目表；
    - ``"roi"``   soft 种草段：可给收益试算器链接（带 ref 归因）；
    - ``"order"`` direct 资质够：产品自助下单深链 + 试算器辅助；
    - ``"cs"``    direct 资质缺：不甩下单链，引官方客服人工收口。
    ``cta=None`` 时按 milestone/bant/cta_bias 内部推导（旧调用零参数=旧行为）。
    ``plan_pref="entry"``（P11）：下单深链改入门档，并加一行内部提醒。
    ``offer``（P13）：运营在目录里授权的在售活动，命中时引用一行（仅
    order/cs 档，且只许照转不许加码）——见 ``goals.offers``。
    """
    lvl = str(push_level or "soft").strip().lower()
    if lvl not in ("soft", "direct") or not products:
        return None
    site = site or {}
    en = str(lang).lower().startswith("en")
    if cta is None:
        cta = pick_cta(push_level=lvl, milestone_idx=milestone_idx,
                       bant_fill=bant_fill, site=site, cta_bias=cta_bias)
    pref = str(plan_pref or "").strip().lower()

    lines: List[str] = ["【可推荐产品·内部参考（按对方画像选出）】"]
    if pref == "entry":
        lines.append("对方曾反馈价格敏感——优先推入门档深链，先算账再谈升级，"
                     "绝不自行承诺折扣")
    # 活动行紧跟在纪律提醒后（早于产品行入块）：max_chars 截断时优先保住
    # 「有授权活动」这条事实，避免只剩「绝不承诺折扣」的半截语境。
    offer_line = offers_mod.offer_block_line(offer, cta=str(cta or ""))
    if offer_line:
        lines.append(offer_line)
    for prod in products[:3]:
        name = str(prod.get("name_en" if en else "name_zh")
                   or prod.get("name_zh") or prod.get("id") or "").strip()
        pitch = str(prod.get("pitch_en" if en else "pitch_zh")
                    or prod.get("pitch_zh") or "").strip()
        price = str(prod.get("price_from") or "").strip()
        row = f"- {name}：{pitch}" if pitch else f"- {name}"
        if price:
            row += f"（{price}起）" if not price.endswith("起") else f"（{price}）"
        if cta == "order":
            link = product_link(site, prod, ref=link_ref, plan_pref=pref)
            if link:
                row += f" 下单：{link}"
        lines.append(row)

    calc = _append_ref(str(site.get("calc_url") or "").strip(), link_ref)
    support = str(site.get("support_contact") or "").strip()
    if cta == "order":
        if calc:
            lines.append(f"对方关心划不划算时，可给官网「AI成交收益试算」让TA自己算：{calc}")
        lines.append("【带货纪律】对方兴致好才给链接（官网自助下单），一次只推一款；"
                     "绝不夸大承诺、不保证收益、不代收任何款项。")
    elif cta == "cs":
        # P11：cta_bias=cs 是信任未重建（没用起来/出了问题），文案不能
        # 还说「资质没聊透」——那是 BANT 缺槽的默认口吻。
        if support:
            if str(cta_bias or "").strip().lower() == "cs":
                lines.append(
                    f"对方曾反馈体验/信任问题——别甩自助下单链硬推，"
                    f"先共情再引官方客服细聊配置：{support}")
            else:
                lines.append(
                    f"对方资质还没聊透（预算/团队规模未知）——别甩自助下单链，"
                    f"想细聊配置就引到官方客服：{support}")
        if calc:
            lines.append(f"也可给官网「AI成交收益试算」让TA自己算：{calc}")
        lines.append("【带货纪律】先把对方的顾虑聊清再谈下单；"
                     "绝不夸大承诺、不保证收益、不代收任何款项。")
    elif cta == "roi":
        lines.append(f"对方聊到成本/人手/划算时，可给官网「AI成交收益试算器」"
                     f"让TA自己算一算：{calc}")
        if str(cta_bias or "").strip().lower() == "roi":
            lines.append("对方曾反馈价格敏感——先用试算让TA自己看见账，"
                         "再谈入门档；绝不自行承诺折扣")
        lines.append("【带货纪律】试算器只在对方兴致好时给，一次就好；"
                     "不发下单链接、不报价目表；试算是估算，不许拿它承诺收益。")
    else:
        lines.append("【带货纪律】现在只许以自己真实使用体验的口吻聊价值，"
                     "不发链接、不报价目表；对方主动问价可说大致价位。")

    block = "\n".join(lines)
    cap = max(200, int(max_chars or DEFAULT_MAX_CHARS))
    if len(block) > cap:
        block = block[:cap]
    return block


__all__ = [
    "CTA_ORDER_MIN_FILL",
    "CTA_ROI_MIN_MILESTONE",
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_MAX_CHARS",
    "build_catalog_block",
    "catalog_path",
    "load_catalog",
    "entry_plan_key",
    "pick_cta",
    "pick_products",
    "product_link",
    "sold_boost_map",
    "resolve_catalog_cfg",
]
