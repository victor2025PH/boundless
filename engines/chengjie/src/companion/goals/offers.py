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


# ── 自家阵营在推活动登记表（#147，2026-09-02）────────────────────────────────
# 实锤：LINE 客户提到收件箱里「佳士得夏季拍卖·充值奖励」推广（我方**其他人设**在推），
# Claire 人设连讽四条（silly top-up bonuses / flashy ad / auction houses don't beg
# for deposits）——LLM 的反诈直觉对自家推广素材开火。根因＝人设事实层没有「自家在推
# 的活动/产品」白名单，跨人设商业事实不共享。
#
# 登记表与 P13 offers / P15 claims **同源同文件**（site_catalog.yaml 的
# ``camp_promotions`` 段）：运营写下来的才算自家的；引擎只消费、不发明。词表另外
# 自动并入：当日有效的 P13 活动文案 + 目录产品名——贬损自家产品与贬损自家活动同罪。
# 护栏刻意比 offers 松（不要求同域 url / for_churn）：这里登记的是「别嘲讽」的事实，
# 不是「可以主动引用」的促销。enabled 显式 true + authorized_by 留痕 + 未过期即可。

# 单条活动最多带的关键词数（防登记表把整篇文案当 keywords 塞进来撑爆 prompt/守卫）
CAMP_MAX_KEYWORDS = 12
# 词表里的词至少这么长（单字/双字母会把守卫变成地毯式误伤面）
CAMP_MIN_TERM_LEN = 2


def camp_promotions(
    catalog: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """``site_catalog.camp_promotions`` 里此刻有效的登记项（过滤 + 规范化，绝不抛）。

    每项：``{"id","label","label_en","keywords":[...],"note","authorized_by",
    "valid_until_ts"}``。``valid_until`` 可省（长期活动）；给了就按 P13 同一口径
    到期自动消失。``keywords`` 缺省用 label/label_en 本身。
    """
    n = float(now if now is not None else time.time())
    cat = catalog if isinstance(catalog, dict) else {}
    raw = cat.get("camp_promotions")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        if not str(item.get("authorized_by") or "").strip():
            continue
        exp = 0.0
        if item.get("valid_until") not in (None, ""):
            exp = _expiry_ts(item.get("valid_until"))
            if exp <= n:
                continue
        label = str(item.get("label_zh") or item.get("label") or "").strip()
        label_en = str(item.get("label_en") or "").strip()
        kws: List[str] = []
        for kw in (item.get("keywords") or []):
            s = str(kw or "").strip()
            if len(s) >= CAMP_MIN_TERM_LEN and s not in kws:
                kws.append(s)
            if len(kws) >= CAMP_MAX_KEYWORDS:
                break
        if not (label or label_en or kws):
            continue
        out.append({
            "id": str(item.get("id") or "").strip(),
            "label": label,
            "label_en": label_en,
            "keywords": kws,
            "note": str(item.get("note_zh") or item.get("note") or "").strip(),
            "note_en": str(item.get("note_en") or "").strip(),
            "authorized_by": str(item.get("authorized_by") or "").strip(),
            "valid_until_ts": exp,
        })
    return out


def camp_terms(
    catalog: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> List[str]:
    """自家阵营词表（出站贬损守卫的命中面）＝登记活动的 label/关键词 + 当日有效
    P13 活动文案 + 目录产品名（name_zh/name_en）。去重、剔短词，绝不抛。"""
    out: List[str] = []
    seen: set = set()

    def _add(v: Any) -> None:
        s = str(v or "").strip()
        if len(s) < CAMP_MIN_TERM_LEN:
            return
        k = s.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(s)

    try:
        for p in camp_promotions(catalog, now=now):
            _add(p.get("label"))
            _add(p.get("label_en"))
            for kw in p.get("keywords") or []:
                _add(kw)
        for o in active_offers(catalog, now=now):
            _add(o.get("label"))
            _add(o.get("label_en"))
        for prod in ((catalog or {}).get("products") or []):
            if not isinstance(prod, dict):
                continue
            _add(prod.get("name_zh"))
            _add(prod.get("name_en"))
    except Exception:  # noqa: BLE001 — 守卫词表绝不因目录脏形状崩
        return out
    return out


CAMP_BLOCK_MAX_ITEMS = 6
CAMP_BLOCK_MAX_CHARS = 520


def camp_block(
    catalog: Optional[Dict[str, Any]],
    *,
    lang: str = "zh",
    now: Optional[float] = None,
    max_chars: int = CAMP_BLOCK_MAX_CHARS,
) -> str:
    """人设 prompt 注入块（#147 第一层）：告诉 LLM 哪些是自家在推的正规活动。

    只列**登记活动 + 当日有效 P13 活动**（目录产品不列——带货会话另有目录块，
    非带货会话列产品名会诱发主动推销）。空登记 → ""（零 token 开销）。
    纪律行三件事：不质疑真伪、不贬损定性、不主动推销——它是「别拆自家台」的
    约束，不是新的带货指令。
    """
    items: List[str] = []
    try:
        en = str(lang or "").lower().startswith("en")
        for p in camp_promotions(catalog, now=now):
            main = (p.get("label_en") if en else p.get("label")) or p.get("label") \
                or p.get("label_en") or ""
            alt = (p.get("label") if en else p.get("label_en")) or ""
            note = (p.get("note_en") if en else p.get("note")) or p.get("note") or ""
            line = str(main)
            if alt and alt != main:
                line += f"（{alt}）"
            if note:
                line += f"：{note}"
            if line.strip():
                items.append(line)
        for o in active_offers(catalog, now=now):
            lbl = (o.get("label_en") if en else o.get("label")) or o.get("label") or ""
            if lbl and lbl not in items:
                items.append(str(lbl))
    except Exception:  # noqa: BLE001
        return ""
    if not items:
        return ""
    items = items[:CAMP_BLOCK_MAX_ITEMS]
    if str(lang or "").lower().startswith("en"):
        head = ("[Our side's live promotions — hard rule] The following are legitimate "
                "campaigns/products our own team (colleagues / other accounts) is promoting. "
                "Never question their legitimacy, never call them a scam / ad / spam / gimmick, "
                "never mock their mechanics (top-up bonuses, deposits, tiers, etc.). If the "
                "customer brings one up, stay neutral or point them to the official terms — "
                "don't judge for them, and don't pitch it unprompted either:")
    else:
        head = ("【自家阵营在推活动·硬约束】以下是我方阵营（同事/其他账号）正在推广的正规"
                "活动/产品：对方提起时绝不质疑真伪、绝不定性为骗局/广告/垃圾邮件/噱头，"
                "也不嘲讽其机制（充值奖励/保证金/等级等）；中性带过或建议看官方细则，"
                "不替对方下判断，也不主动推销：")
    block = head + "\n" + "\n".join(f"- {it}" for it in items)
    cap = max(200, int(max_chars or CAMP_BLOCK_MAX_CHARS))
    if len(block) > cap:
        block = block[:cap]
    return block


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
    "CAMP_BLOCK_MAX_CHARS",
    "CAMP_BLOCK_MAX_ITEMS",
    "CAMP_MAX_KEYWORDS",
    "CAMP_MIN_TERM_LEN",
    "DEFAULT_FOR_CHURN",
    "active_offers",
    "allowlist_texts",
    "authorized_free_days",
    "camp_block",
    "camp_promotions",
    "camp_terms",
    "claim_offer_citation",
    "offer_block_line",
    "pick_offer",
    "reset_citations",
]
