"""运营授权优惠（P13，纯函数）——**引擎只消费，绝不发明**。

「太贵」是实测最高频流失原因，但折扣属定价权限：引擎自己编「给你打九折」
既无授权也无法兑现（官网收全价 = 当场翻车）。本模块把优惠收敛成**目录里
运营手填的在售活动**（与价格同源 `config/site_catalog.yaml`），聊天层只在
硬护栏全过时引用一次。

护栏（任一不过 → 当作没有优惠，行为回落 P11 入门档转向）：
- ``enabled: true`` 显式开；``authorized_by`` 必填（留痕谁批的）；
- ``valid_until`` 必填且未过期（过期活动自动消失，无需有人记得删）；
- ``url`` 必须是站点同域（防把客户导去站外/仿冒页）；
- ``for_churn`` 命中当前主流失原因（缺省=只对价格敏感类生效）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# 缺省适用面：价格敏感类流失（与 profile_slots._CHURN_OFFER 的 entry 档同源语义）
DEFAULT_FOR_CHURN = ("太贵", "预算紧张", "换了别家")


def _same_site(url: str, base_url: str) -> bool:
    try:
        host = (urlparse(str(url or "")).netloc or "").split("@")[-1]
        host = host.split(":")[0].lower()
        base = (urlparse(str(base_url or "")).netloc or "").split(":")[0].lower()
        if not host or not base:
            return False
        return host == base or host.endswith("." + base)
    except Exception:
        return False


def _expiry_ts(raw: Any) -> float:
    """``valid_until``（YYYY-MM-DD 或 epoch 秒）→ 当日 23:59:59 时间戳。
    解析失败 → 0（=视为无效，宁可不发也不发过期活动）。"""
    if isinstance(raw, (int, float)) and float(raw) > 0:
        return float(raw)
    s = str(raw or "").strip()
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            t = time.strptime(s, fmt)
            return time.mktime(t) + 86399.0
        except ValueError:
            continue
    return 0.0


def active_offers(
    catalog: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """目录里此刻**有效且合规**的活动（过滤 + 规范化，绝不抛）。"""
    n = float(now if now is not None else time.time())
    cat = catalog if isinstance(catalog, dict) else {}
    site = cat.get("site") if isinstance(cat.get("site"), dict) else {}
    base_url = str(site.get("base_url") or "").strip()
    raw = cat.get("offers")
    if not isinstance(raw, list) or not base_url:
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        if not str(item.get("authorized_by") or "").strip():
            continue
        exp = _expiry_ts(item.get("valid_until"))
        if exp <= n:
            continue
        url = str(item.get("url") or "").strip()
        if url and not _same_site(url, base_url):
            continue
        for_churn = [str(x).strip() for x in (item.get("for_churn") or [])
                     if str(x).strip()] or list(DEFAULT_FOR_CHURN)
        out.append({
            "id": str(item.get("id") or "").strip(),
            "label": str(item.get("label_zh") or item.get("label")
                         or "").strip(),
            "label_en": str(item.get("label_en") or "").strip(),
            "url": url,
            "plan_key": str(item.get("plan_key") or "").strip(),
            "product_id": str(item.get("product_id") or "").strip(),
            "for_churn": for_churn,
            "valid_until_ts": exp,
            "authorized_by": str(item.get("authorized_by") or "").strip(),
        })
    return out


def pick_offer(
    catalog: Optional[Dict[str, Any]],
    *,
    churn_reason: str = "",
    product_id: str = "",
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """选一条可引用的活动：主流失原因命中 + （若活动限定产品）产品匹配。

    命中多条时优先「限定了本产品」的，其次最早到期的（快过期的先用掉）。
    无流失原因 / 无命中 → None（回落 P11 入门档转向，绝不硬塞活动）。
    """
    primary = str(churn_reason or "").split("、", 1)[0].strip()
    if not primary:
        return None
    pid = str(product_id or "").strip()
    cands = [o for o in active_offers(catalog, now=now)
             if primary in o["for_churn"]
             and (not o["product_id"] or not pid or o["product_id"] == pid)]
    if not cands:
        return None
    cands.sort(key=lambda o: (0 if (pid and o["product_id"] == pid) else 1,
                              o["valid_until_ts"]))
    return cands[0]


def allowlist_texts(catalog: Optional[Dict[str, Any]]) -> List[str]:
    """出站优惠守卫的白名单：**运营写下来的**价格事实才许说。

    ＝当日在效活动文案 + 目录里各产品的卖点/起价 + ``claims.texts``（运营登记的
    非活动类价格/试用事实，见 ``site_catalog.yaml`` 的 claims 段）。
    异常 → []（守卫退化成全拦，安全侧）。
    """
    out: List[str] = []
    try:
        for o in active_offers(catalog):
            for k in ("label", "label_en", "url"):
                v = str(o.get(k) or "").strip()
                if v:
                    out.append(v)
        for prod in ((catalog or {}).get("products") or []):
            if not isinstance(prod, dict):
                continue
            for k in ("pitch_zh", "pitch_en", "price_from"):
                v = str(prod.get(k) or "").strip()
                if v:
                    out.append(v)
        for v in (_claims(catalog).get("texts") or []):
            s = str(v or "").strip()
            if s:
                out.append(s)
    except Exception:
        return out
    return out


def _claims(catalog: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = (catalog or {}).get("claims")
    return c if isinstance(c, dict) else {}


def authorized_free_days(catalog: Optional[Dict[str, Any]]) -> List[float]:
    """``claims.free_days``：运营授权的免费时长（天）。

    出站守卫据此放行「免费试用 7 天」而照剥「免费用一个月」——授权的是**事实**
    不是措辞（见 ``offer_guard`` 模块头）。未登记 → []（任何赠送承诺都算编的）。
    """
    out: List[float] = []
    try:
        raw = _claims(catalog).get("free_days")
        if isinstance(raw, (int, float, str)):
            raw = [raw]
        for v in (raw or []):
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                out.append(f)
    except Exception:
        return out
    return out


def offer_block_line(
    offer: Optional[Dict[str, Any]],
    *,
    cta: str = "",
    with_link: bool = True,
) -> str:
    """活动进 prompt 的一行（内部参考口吻 + 反加码纪律）。空/不合适 → ""。

    ``cta="order"`` 才给链接（其余档给了也会被 link_guard 剥，反而让 LLM
    在文字里许诺一个发不出去的链接）；``cta="cs"`` 只告知有活动、走客服。
    """
    if not isinstance(offer, dict) or not offer.get("label"):
        return ""
    lvl = str(cta or "").strip().lower()
    label = str(offer.get("label") or "")
    url = str(offer.get("url") or "")
    if lvl == "order" and with_link and url:
        return (f"官方在售活动（运营已授权，仅此一条）：{label} → {url}；"
                "只许照此转述，不许加码/延期/另造折扣码")
    if lvl in ("order", "cs"):
        return (f"官方在售活动（运营已授权）：{label}；"
                "细则与办理走官方客服，不许自行承诺条件")
    return ""


# ── 「同目标同天只提一次」节流 ──────────────────────────────────────────────
# 目标块每条入站消息都会注入；活动行若条条都在，LLM 容易反复推销（客户连发
# 5 条＝被念 5 次促销）。价格类信息提一次就够，之后靠对方主动问。
# 进程级即可：重启最多多提一次，比落库简单得多。
_CITED: Dict[str, float] = {}
_CITED_CAP = 500


def claim_offer_citation(
    goal_id: str, offer_id: str, *, now: Optional[float] = None
) -> bool:
    """本目标今天是否还能提这条活动（可提 → True 并记账）。空 id → 不节流。"""
    gid = str(goal_id or "").strip()
    if not gid:
        return True
    n = float(now if now is not None else time.time())
    key = f"{gid}|{str(offer_id or '-')}|{int(n // 86400)}"
    if key in _CITED:
        return False
    if len(_CITED) >= _CITED_CAP:                 # 过期键清扫（超 2 天）
        cutoff = n - 2 * 86400
        for k in [k for k, ts in _CITED.items() if ts < cutoff]:
            _CITED.pop(k, None)
        if len(_CITED) >= _CITED_CAP:
            _CITED.clear()
    _CITED[key] = n
    return True


def reset_citations() -> None:
    """测试用：清空节流记账。"""
    _CITED.clear()


__all__ = [
    "DEFAULT_FOR_CHURN",
    "active_offers",
    "allowlist_texts",
    "authorized_free_days",
    "claim_offer_citation",
    "offer_block_line",
    "pick_offer",
    "reset_citations",
]
