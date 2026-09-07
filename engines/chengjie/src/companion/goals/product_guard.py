"""目标出站硬规则（M-5 A / #217 / D-M6，2026-09-06）：

1. **无产品禁报价**——目标没绑「卖什么 / 价格 / 成交动作」字段时，冲刺与自然档
   的主动拍**禁止**出现报价 / 支付 / 下单 / 开户 / 引导页类话术。词表＝本模块的
   销售话术家族 + ``persona_guard.matches_service_frame``（J-1 B「客服/销售组织
   框架腔」）。命中 → 整条拦下（不发、不剥句——主动拍不是回复，少发一拍没有代价，
   把半截销售话术发给陪聊客户才有代价），care 行标 ``skipped:goal_no_product``，
   目标时间线记 ``goal_no_product`` 事件，日志 ``[goal_no_product]``。
   背景：#175 的「黄金市场」＝引擎无产品可推时的编造；1.0.75 冲刺引擎真发后，
   「错但没发」变成「错且发到客户」。
2. **首次真发强制预览**——某目标第一条由引擎主动发出的消息不直接投递，改进草稿
   审批（``autopilot_level=L1``，``risk_reasons=["goal_first_send"]``，日志
   ``level=L1 reason=goal_first_send`` 与 M-2 E 的 reason 字段同格式），坐席过目
   后从草稿队列发出；之后的拍按目标自治档正常真发。存量已发过的目标不受影响。

接线：:func:`wrap_care_send` 包住 bootstrap 的 ``_care_send``（派发器只暴露
send_callback 这一个出口；``care_dispatcher.py`` 归 M-1，本条不碰）。非 goal 行 /
运营手改（``manual_rewrite``）/ 任何解析异常 → 原样透传（fail-open 只对「判不出
是不是目标行」；判出是无产品目标且命中话术 → fail-closed）。纯函数部分零 IO，可单测。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.goal_product_guard")

# 「绑了产品」的参数键（模板 params 里任一非空即算绑定）：
#   item_id（付费解锁）/ tier（会员档）/ product_id（官网产品 / 挽回继承）/ last_plan
PRODUCT_PARAM_KEYS = ("item_id", "product_id", "tier", "last_plan")

# custom 目标没有产品字段——但运营在 note 里明写了「卖什么/多少钱」就是显式授权
# （「让他买我的 $50 课程」），按绑定处理；只写方向（「获取客户职业」）→ 未绑定。
_NOTE_PRODUCT_RE = re.compile(
    r"(?:[¥￥$€£]|US\$|USD|RMB|HKD|NT\$)\s?\d"
    r"|\d+(?:\.\d+)?\s*(?:元|块|美金|美元|刀|港币|台币|日元|欧元|dollars?|usd|bucks)\b"
    r"|报价|售价|定价|价格|价位|付费|付款|买我的|卖给|推销|下单|订购|开通|订阅"
    r"|\bprice\b|\bpricing\b|\bsell\b|\bbuy\b|\bpurchase\b|\bsubscri(?:be|ption)\b"
    r"|\bpay(?:ment)?\b|\bcheckout\b|\border\b",
    re.I)

# 报价 / 支付 / 下单 / 开户 / 引导页 类销售话术（出站文本命中即算）。
# 词形保守：要求「动作 + 对象」或「货币 + 数字」，纯「价格」二字不抓（「你觉得
# 这个价格的房子贵吗」是闲聊）；开户/引导页/onboarding 家族由 persona_guard
# service_frame 覆盖，这里只补它没收的报价与支付面。
_SALES_TALK_PATTERNS: List["re.Pattern[str]"] = [
    # 报价类（zh）
    re.compile(r"报个?价|报价单|售价|定价|标价|一口价|优惠价|折后价|起售|价格(?:是|为|只要|才|仅)"),
    # 货币 + 数字（zh/en 通用）：¥99 / $9.99 / US$ 20 / 99 元 / 5 刀 / 20 dollars
    re.compile(r"(?:[¥￥$€£]|US\$|USD|RMB|HKD|NT\$)\s?\d[\d,]*(?:\.\d+)?"),
    re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:元|块钱|块|美金|美元|刀|港币|台币|日元|欧元|dollars?|bucks)\b"),
    re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:/|per\s+)(?:mo|month|yr|year|week)\b", re.I),
    # 支付 / 收款类（zh）
    re.compile(r"付款(?:方式|链接|码|给我)|支付(?:方式|链接|宝|给)|微信支付|转账(?:给|到|方式)|"
               r"汇款|收款码|付款码|扫码(?:付|支付)|银行卡号|打款"),
    re.compile(r"PayPal|Stripe|Venmo|Zelle|Cash\s?App|\bWise\b|\bUSDT\b|\bTRC20\b|\bERC20\b", re.I),
    # 下单 / 开通 / 订购类（zh）
    re.compile(r"下单|订购|购买(?:链接|方式|入口)|开通(?:会员|服务|账户|账号|套餐|VIP|Pro|专业版|高级版)|"
               r"办理(?:会员|套餐|开户)|签约|结账|立即购买|马上购买|购买后|订阅(?:链接|方式|后)"),
    # 开户 / 注册 / 引导页类（zh；与 service_frame 互补）
    re.compile(r"开户|开个?(?:账户|账号|户)|注册(?:链接|账号|账户|入口)|开通账户|"
               r"引导页|落地页|官网(?:下单|链接|购买|注册|自助)|试用链接"),
    # 报价 / 支付类（en）
    re.compile(r"\b(?:the\s+)?price\s+(?:is|would\s+be|starts?\s+(?:at|from))\b|\bpricing\b|"
               r"\b(?:a\s+)?quote\b|\bit\s+costs?\b|\bcosts?\s+(?:only\s+|just\s+)?\$?\d", re.I),
    re.compile(r"\bpayment\b|\bpay\s+(?:via|with|by|through|here|now|me)\b|\bpaid\s+plan\b|"
               r"\bcheckout\b|\binvoice\b|\b(?:bank|wire)\s+transfer\b|\bcrypto\s+wallet\b", re.I),
    # 下单 / 开通 / 注册类（en）
    re.compile(r"\bplace\s+(?:an?\s+|your\s+)?order\b|\border\s+(?:link|page|now|here|today)\b|"
               r"\bsubscribe\s+(?:now|here|today|via|at)\b|\bsign\s?-?up\s+(?:link|here|now|page|today|at)\b|"
               r"\bbuy\s+(?:now|here|it\s+here|it\s+now)\b|\bpurchase\s+(?:link|here|now|page)\b|"
               r"\bget\s+started\s+(?:here|now|today|at)\b|\bupgrade\s+(?:now|here|today|to\s+(?:pro|premium|vip))\b|"
               r"\bfree\s+trial\b|\btrial\s+link\b", re.I),
    re.compile(r"\bopen\s+an?\s+account\b|\bcreate\s+(?:an?\s+|your\s+)?account\b|"
               r"\bregister\s+(?:here|now|an?\s+account|at)\b|\blanding\s+page\b|"
               r"\bofficial\s+(?:site|website|store)\b", re.I),
]

# 目标时间线事件 kind
NO_PRODUCT_EVENT = "goal_no_product"
FIRST_SEND_EVENT = "first_send_preview"
# 「已经由引擎真发过」的事件家族：care 派发回执 / 拍落 sent / 首条已进预览
_SENT_EVENT_KINDS = ("care_sent", "beat_sent", FIRST_SEND_EVENT)

# 草稿审批的 reason 码（M-2 E 同一字段族：no_persona / cooldown / lang_unknown /
# first_contact / weak_evidence / goal_first_send）
FIRST_SEND_REASON = "goal_first_send"
NO_PRODUCT_REASON = "goal_no_product"


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def lifecycle_template_blocked(cfg_root: Any, template_id: str) -> bool:
    """用户版（client 形态）后台生命周期自建（auto_create / retention_auto /
    winback_auto / reconvert_auto）是否也该拦住该模板（M-5 A 追问①，老板拍板：闸）。

    路由层的 `_client_hide` 看 session 开发者模式；后台没有 session，只按部署形态判
    （开发者模式是「人」的临时状态，不该让引擎在客户机上自己建出「已下线」模板）。
    N-3 #241（D-N1）：业务域是陪伴（business_domain=companion）时同样拦——域级规则，
    与形态无关。销售域的 partner / internal / 非隐藏类目 → False。
    判不出（导入失败）→ False，不误拦。
    """
    try:
        from src.companion.goals.templates import is_hidden_template
        from src.utils.business_domain import resolve_business_domain
        from src.web.ui_visibility import is_client_flavor
        return is_hidden_template(
            template_id, client_hide=is_client_flavor(cfg_root),
            business_domain=resolve_business_domain(cfg_root))
    except Exception:
        logger.debug("lifecycle_template_blocked failed (treat as allowed)", exc_info=True)
        return False


def no_product_prompt_rule(goal: Optional[Dict[str, Any]], *,
                           catalog_has_products: bool = False) -> str:
    """回复链目标注入块用的「产品边界」一行（M-5 A 追问②：首拍预览不扩到回复链，
    但无产品禁报价这条硬规则要让顺势推进也吃到——prompt 级，与出站拦截同一判定）。
    目标绑了产品 → ""（不加行）。"""
    if goal_product_binding(goal, catalog_has_products=catalog_has_products)["bound"]:
        return ""
    return NO_PRODUCT_PROMPT_LINE


# 注入块里的产品边界行（与 context_block 的纪律行同为安全语义，超长不丢）
NO_PRODUCT_PROMPT_LINE = (
    "【产品边界】本目标没有绑定具体产品：不得报价、不得给价格/付款方式/开户或注册链接/"
    "引导页，只做关系与铺垫；对方问到价格或购买方式就如实说需要再确认，绝不编造。"
)


def goal_product_binding(
    goal: Optional[Dict[str, Any]], *, catalog_has_products: bool = False,
) -> Dict[str, Any]:
    """目标是否绑了「卖什么」→ ``{"bound": bool, "source": str}``。

    - params 里 :data:`PRODUCT_PARAM_KEYS` 任一非空 → ``param:<key>``；
    - 挂目录的模板（``catalog: True``，官网产品线）且目录里真有产品 → ``catalog``
      （product_id 留空＝按画像自动选品，目录空则无货可选＝空壳）；
    - custom 的 note 明写了价格/买卖动作 → ``note``；
    - 其余 → 未绑定。绝不抛。
    """
    try:
        params = (goal or {}).get("params") or {}
        if not isinstance(params, dict):
            params = {}
        for k in PRODUCT_PARAM_KEYS:
            if str(params.get(k) or "").strip():
                return {"bound": True, "source": f"param:{k}"}
        tid = str((goal or {}).get("template") or "")
        try:
            from src.companion.goals.templates import get_template
            tmpl = get_template(tid) or {}
        except Exception:
            tmpl = {}
        if tmpl.get("catalog") and catalog_has_products:
            return {"bound": True, "source": "catalog"}
        if tid == "custom":
            note = str(params.get("note") or "")
            if note and _NOTE_PRODUCT_RE.search(note):
                return {"bound": True, "source": "note"}
    except Exception:
        logger.debug("goal_product_binding failed (treat as unbound)", exc_info=True)
    return {"bound": False, "source": ""}


def find_sales_talk(text: str) -> List[str]:
    """出站文本里的报价 / 支付 / 下单 / 开户 / 引导页话术命中片段（空＝干净）。

    合并 ``persona_guard.matches_service_frame``（J-1 B 词表）；守卫自身异常按
    「不命中」返回（调用方另有 fail-closed 判定，这里不把异常伪装成命中）。"""
    s = str(text or "")
    if not s:
        return []
    out: List[str] = []
    seen = set()
    for pat in _SALES_TALK_PATTERNS:
        try:
            m = pat.search(s)
        except Exception:
            m = None
        if m:
            frag = m.group(0).strip()
            if frag and frag.lower() not in seen:
                seen.add(frag.lower())
                out.append(frag)
    try:
        from src.utils.persona_guard import matches_service_frame
        for frag in matches_service_frame(s):
            f = str(frag or "").strip()
            if f and f.lower() not in seen:
                seen.add(f.lower())
                out.append(f)
    except Exception:
        logger.debug("service_frame check unavailable", exc_info=True)
    return out


def guard_goal_outbound(
    goal: Optional[Dict[str, Any]], text: str, *,
    catalog_has_products: bool = False,
) -> Dict[str, Any]:
    """无产品目标 × 出站销售话术 → ``{"ok": False, "reason": "goal_no_product",
    "hits": [...], "bound": False}``；其余放行（``ok=True``）。

    绑了产品的目标不在本守卫范围（报价是它该做的事，分寸由 offer_guard /
    push_curve 管）。"""
    binding = goal_product_binding(goal, catalog_has_products=catalog_has_products)
    if binding["bound"]:
        return {"ok": True, "reason": "", "hits": [], "bound": True,
                "source": binding["source"]}
    hits = find_sales_talk(text)
    if hits:
        return {"ok": False, "reason": NO_PRODUCT_REASON, "hits": hits,
                "bound": False, "source": ""}
    return {"ok": True, "reason": "", "hits": [], "bound": False, "source": ""}


def first_send_pending(gstore: Any, goal_id: str) -> bool:
    """该目标是否还没有过任何一条引擎主动真发（也没进过首条预览）。

    读 goal_events：``care_sent``（派发回执）/ ``beat_sent``（拍落 sent）/
    ``first_send_preview``（首条已进草稿）任一存在 → False。存量 1.0.75 已真发的
    目标据此**不会**被回头拦成预览。store 缺方法 / 异常 → False（不拦：预览是
    产品纪律不是安全红线，判不出宁可按旧行为真发也不要让目标整条卡死）。"""
    gid = str(goal_id or "")
    if not gid or gstore is None:
        return False
    try:
        for kind in _SENT_EVENT_KINDS:
            if int(gstore.count_events_since(gid, kind, 0.0) or 0) > 0:
                return False
        return True
    except Exception:
        logger.debug("first_send_pending failed (treat as not pending)", exc_info=True)
        return False


def build_first_send_draft(
    goal: Dict[str, Any], text: str, *, peer_text: str = "",
    care_id: Any = None,
) -> Dict[str, Any]:
    """首条预览的 ``upsert_draft`` 载荷：inbox 源 + 会话固定 ``source_id``
    （``goal_first:<gid>`` 幂等）+ ``autopilot_level=L1``（自动投递链只取 L2，
    L1 只能人批）+ ``risk_reasons=[goal_first_send]``（审批卡「原因」行）。"""
    gid = str(goal.get("goal_id") or "")
    conv = str(goal.get("conversation_id") or "")
    platform = str(goal.get("platform") or "")
    account_id = str(goal.get("account_id") or "default") or "default"
    chat_key = str(goal.get("chat_key") or "")
    if conv and not (platform and chat_key):
        parts = conv.split(":", 2)
        if len(parts) == 3:
            platform, account_id, chat_key = parts[0], parts[1] or account_id, parts[2]
    if not conv and platform and chat_key:
        conv = f"{platform}:{account_id}:{chat_key}"
    return {
        "source_kind": "inbox",
        "source_id": f"goal_first:{gid}",
        "conversation_id": conv,
        "platform": platform,
        "account_id": account_id,
        "chat_key": chat_key,
        "peer_text": str(peer_text or "")[:500],
        "draft_text": str(text or ""),
        "risk_level": "low",
        "risk_reasons": [FIRST_SEND_REASON],
        "autopilot_level": "L1",
        "status": "pending",
        "trace_id": f"{FIRST_SEND_REASON}:{gid}"
                    + (f":care{int(care_id)}" if care_id else ""),
    }


# ── 接线：包装 care send_callback ───────────────────────────────────────────

def wrap_care_send(
    send_cb: Callable[..., Any], *,
    care_store: Any,
    goal_store_getter: Callable[[], Any],
    inbox_store_getter: Optional[Callable[[], Any]] = None,
    catalog_probe: Optional[Callable[[], bool]] = None,
) -> Callable[..., Any]:
    """把目标硬规则套在 care 派发器的 ``send_callback`` 外面（签名不变）：

    ``async (channel, account_id, chat_name, reply, defer_until, reason,
    staleness_sec, extra) -> row_id``。目标行判定靠 ``extra["care_id"]`` 回查
    care 行的 ``topic_norm``（派发器不把 topic_norm 放进 extra）。

    - 非 goal 行 / ``manual_rewrite``（运营手改直发，人写的话人负责）/ 解析异常
      → 原样调用 ``send_cb``；
    - 无产品目标命中销售话术 → care 行 ``skipped:goal_no_product`` + 目标事件 +
      WARNING 日志，返回 0（派发器视为 gate 拦，不再落 sent）；
    - 首条真发 → 进 L1 草稿 + care 行 ``skipped:preview:draft:<id>`` + 目标事件
      ``first_send_preview`` + INFO 日志 ``level=L1 reason=goal_first_send``，
      返回 0；inbox store 缺失时**不**做预览（退回直发，日志点名）。
    """

    def _catalog_has_products() -> bool:
        if catalog_probe is None:
            return False
        try:
            return bool(catalog_probe())
        except Exception:
            return False

    def _goal_ctx(extra: Any):
        try:
            if not isinstance(extra, dict) or extra.get("manual_rewrite"):
                return None
            care_id = extra.get("care_id")
            if care_id in (None, "", 0):
                return None
            row = care_store.get(int(care_id)) if care_store is not None else None
            if not row:
                return None
            from src.companion.goals.sprint_ticker import parse_goal_care_kind
            kind, gid, _arg = parse_goal_care_kind(row.get("topic_norm"))
            if not gid:
                return None
            gstore = goal_store_getter()
            if gstore is None:
                return None
            goal = gstore.get_goal(gid)
            if goal is None:
                return None
            return {"care_id": int(care_id), "kind": kind, "gid": gid,
                    "goal": goal, "gstore": gstore, "row": row}
        except Exception:
            logger.debug("goal ctx resolve failed (pass-through)", exc_info=True)
            return None

    async def guarded(channel, account_id, chat_name, reply, defer_until,
                      reason, staleness_sec, extra):
        ctx = _goal_ctx(extra)
        if ctx is None:
            return await send_cb(channel, account_id, chat_name, reply,
                                 defer_until, reason, staleness_sec, extra)
        gid, goal, gstore, care_id = ctx["gid"], ctx["goal"], ctx["gstore"], ctx["care_id"]
        tid = str(goal.get("template") or "")

        # ① 无产品禁报价 / 开户 / 支付话术
        verdict = guard_goal_outbound(
            goal, reply, catalog_has_products=_catalog_has_products())
        if not verdict["ok"]:
            hits = "; ".join(verdict["hits"])[:200]
            logger.warning(
                "[%s] gid=%s care_id=%s template=%s kind=%s hits=%r text=%r",
                NO_PRODUCT_REASON, gid, care_id, tid, ctx["kind"], hits,
                str(reply or "")[:120])
            try:
                care_store.mark_skipped(care_id, note=NO_PRODUCT_REASON)
            except Exception:
                logger.debug("mark_skipped failed", exc_info=True)
            try:
                gstore.add_event(
                    gid, NO_PRODUCT_EVENT,
                    f"目标未绑定产品，已拦下含报价/开户/支付话术的主动消息：{hits}")
            except Exception:
                logger.debug("add_event failed", exc_info=True)
            return 0

        # ② 首次真发强制预览 → L1 草稿
        if first_send_pending(gstore, gid):
            inbox = None
            try:
                inbox = inbox_store_getter() if inbox_store_getter is not None else None
            except Exception:
                inbox = None
            if inbox is None:
                logger.warning(
                    "[%s] gid=%s inbox store unavailable, first beat sent without "
                    "preview", FIRST_SEND_REASON, gid)
            else:
                peer_text = ""
                try:
                    conv = str(goal.get("conversation_id") or "")
                    msgs = inbox.list_recent_messages(conv, limit=6) if conv else []
                    for m in reversed(msgs or []):
                        if str(m.get("direction") or "") == "in" and str(m.get("text") or "").strip():
                            peer_text = str(m.get("text") or "").strip()
                            break
                except Exception:
                    peer_text = ""
                try:
                    draft_id = inbox.upsert_draft(build_first_send_draft(
                        goal, reply, peer_text=peer_text, care_id=care_id))
                except Exception:
                    logger.warning("[%s] gid=%s upsert_draft failed, sending "
                                   "without preview", FIRST_SEND_REASON, gid,
                                   exc_info=True)
                    draft_id = ""
                if draft_id:
                    logger.info(
                        "[drafts] level=L1 reason=%s gid=%s care_id=%s draft=%s "
                        "conv=%s", FIRST_SEND_REASON, gid, care_id, draft_id,
                        str(goal.get("conversation_id") or ""))
                    try:
                        care_store.mark_skipped(
                            care_id, note=f"preview:draft:{draft_id}")
                    except Exception:
                        logger.debug("mark_skipped failed", exc_info=True)
                    try:
                        gstore.add_event(
                            gid, FIRST_SEND_EVENT,
                            f"首条主动消息已进草稿审批（{draft_id}），请过目后发送")
                    except Exception:
                        logger.debug("add_event failed", exc_info=True)
                    return 0

        return await send_cb(channel, account_id, chat_name, reply,
                             defer_until, reason, staleness_sec, extra)

    return guarded


__all__ = [
    "FIRST_SEND_EVENT",
    "FIRST_SEND_REASON",
    "NO_PRODUCT_EVENT",
    "NO_PRODUCT_PROMPT_LINE",
    "NO_PRODUCT_REASON",
    "PRODUCT_PARAM_KEYS",
    "build_first_send_draft",
    "find_sales_talk",
    "first_send_pending",
    "goal_product_binding",
    "guard_goal_outbound",
    "lifecycle_template_blocked",
    "no_product_prompt_rule",
    "wrap_care_send",
]
