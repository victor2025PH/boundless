"""客户画像 LLM 抽取轨（正则轨的盲区补丁，默认关）。

正则轨（profile_slots.capture_from_text）只认高置信句式——「其实我这边是帮人
代运营民宿的」这类自由表达全漏。本轨用一次小 LLM 调用做**摘录式**抽取，
三层护栏防「LLM 编画像」（错画像比空画像更毒，会被 ledger 当推进信号）：

1. **摘录接地**（ground_extracted）：必须过 ``src.ai.memory_grounding``
   （``fact_grounded_in_user_msg``）——抽出值与客户原话要有内容级词汇重叠
   （CJK bigram / 拉丁词 / 数字）。只在助手回复里出现、用户从没说过的内容
   一律丢弃（2026-07-13 大阪/不用上班事故）。本模块**只读复用**该护栏，不改它。
2. **低把握标待确认**：过接地的 LLM 值入库 ``src=llm_pending``，不丢弃也不
   冒充已核实；坐席手录（agent）仍覆盖一切。
3. **只填空槽**：upsert ``overwrite=False``，坐席手录永远优先；
   只对当前缺的槽位发问（提示词按缺口生成）。
4. **预算+冷却**：每会话冷却（默认 30min）+ 全局每日预算（默认 150 次），
   调度即记账（LLM 挂了也不重复烧）。

调度模型：fire-and-forget ``loop.create_task``——两条注入链（A 线 reply /
B 线 draft enrich）都跑在事件循环里，抽取绝不阻塞出稿；无运行 loop（罕见）
静默跳过。任何异常零阻断主链。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

from src.ai.memory_grounding import fact_grounded_in_user_msg
from src.companion.goals.profile_slots import fill_rates, get_slot, missing_slots

logger = logging.getLogger("GoalProfileLLM")

# 过接地但仍是 LLM 猜的——画像卡/注入行标「待确认」，不是丢弃。
LLM_PENDING_SRC = "llm_pending"
PENDING_LABEL = "待确认"

DEFAULT_COOLDOWN_MIN = 30
DEFAULT_DAILY_BUDGET = 150
DEFAULT_MIN_CHARS = 10
_MAX_VALUE_CHARS = 40
_MAX_COOLDOWN_KEYS = 5000
# 摸底期（BANT 还没填够）：改按「每会话每日次数」放行，不走时间冷却。
# 2026-07-28 实录根因：30min 时间冷却把每会话仅有的一发打在了开场寒暄上
# （「在吗，想问下你们那个AI客服」零 BANT 内容），紧接着那句携带
# 跨境电商/双平台/三个客服/几百美金的话全被冷却挡住 → 结构化画像常年空着，
# 而 ledger 的 bant_fill 闸、CTA 升档全靠它，等于漏斗的量化门形同虚设。
DEFAULT_DISCOVERY_MAX_PER_CONV = 6
DEFAULT_DISCOVERY_UNTIL_FILL = 0.75

_STATE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"cooldowns": {}, "day": "", "count": 0,
                          "conv_counts": {}}


def resolve_llm_cfg(cfg_root: Any) -> Dict[str, Any]:
    """``companion.goals.profile_llm`` 配置段（缺/异常 → {} = 默认关）。"""
    try:
        if not isinstance(cfg_root, dict):
            return {}
        goals = ((cfg_root.get("companion") or {}).get("goals") or {})
        sub = goals.get("profile_llm")
        return sub if isinstance(sub, dict) else {}
    except Exception:
        return {}


def reset_state() -> None:
    """测试用：清冷却/预算状态。"""
    with _STATE_LOCK:
        _STATE["cooldowns"] = {}
        _STATE["day"] = ""
        _STATE["count"] = 0
        _STATE["conv_counts"] = {}


# 无信息量的开场白：寒暄词 + 语气词/标点的任意组合（「你好呀」「在吗？」「嗨~」）。
# 只要出现一个实义字就不匹配 → 交给 LLM 摘录。
_GREETING_ONLY_RE = re.compile(
    r"^(?:在吗|在么|在不在|有人吗|你好|您好|哈喽|嗨|hi|hello|hey|"
    r"早上好|早安|晚上好|上午好|下午好|晚安|"
    r"[呀啊哇哈喔噢哦呐嘛啦咯呢么吗~～!！?？。，,.\s])+$",
    re.IGNORECASE)
def looks_worth_extracting(text: str) -> bool:
    """这条消息**值不值得**花一次 LLM 名额（纯函数）。

    刻意只毙掉一类：**纯寒暄/纯标点**（本次事故就是「在吗，想问下你们那个AI客服」
    这种零内容开场吃掉了每会话唯一名额）。长度下限由调用方既有 ``min_chars``
    把关，这里不再叠加长度/关键词启发——试过更严的版本会把
    「客服人手不够消息老是回不过来很头疼」这类**真痛点描述**挡在门外，
    而那正是要采的东西。两端代价不对称：漏放行＝画像继续空（事故本体），
    错放行＝多烧一次小调用且接地护栏会把空结果丢掉。
    """
    t = str(text or "").strip()
    return bool(t) and not _GREETING_ONLY_RE.match(t)


def in_discovery(cfg: Dict[str, Any], fields: Optional[Dict[str, Any]]) -> bool:
    """BANT 还没填够 → 摸底期（放行口径更松）。"""
    try:
        until = float(cfg.get("discovery_until_fill", DEFAULT_DISCOVERY_UNTIL_FILL)
                      or DEFAULT_DISCOVERY_UNTIL_FILL)
    except (TypeError, ValueError):
        until = DEFAULT_DISCOVERY_UNTIL_FILL
    try:
        return float((fill_rates(fields) or {}).get("bant", 0.0)) < until
    except Exception:
        return True


def _acquire_slot(cfg: Dict[str, Any], key: str, now: float,
                  *, discovery: bool = False) -> bool:
    """放行闸（过闸即记账——调度失败也不退款，防重复烧）。

    - 全局每日预算：始终生效，成本上限；
    - **摸底期**：改按「每会话每日次数」（``discovery_max_per_conv``）放行，
      不走时间冷却——信息最密集的头几轮不能被 30 分钟窗口挡在门外；
    - 摸底完成后：回到时间冷却（画像已够，后续只做低频补漏）。
    """
    budget = max(1, int(cfg.get("daily_budget", DEFAULT_DAILY_BUDGET)
                        or DEFAULT_DAILY_BUDGET))
    day = time.strftime("%Y%m%d", time.localtime(now))
    with _STATE_LOCK:
        if _STATE["day"] != day:
            _STATE["day"] = day
            _STATE["count"] = 0
            _STATE["conv_counts"] = {}
        if _STATE["count"] >= budget:
            return False
        if discovery:
            cap = max(1, int(cfg.get("discovery_max_per_conv",
                                     DEFAULT_DISCOVERY_MAX_PER_CONV)
                             or DEFAULT_DISCOVERY_MAX_PER_CONV))
            used = int(_STATE["conv_counts"].get(key, 0))
            if used >= cap:
                return False
            if len(_STATE["conv_counts"]) >= _MAX_COOLDOWN_KEYS:
                _STATE["conv_counts"].clear()
            _STATE["conv_counts"][key] = used + 1
        else:
            cooldown_sec = max(1, int(cfg.get("cooldown_min", DEFAULT_COOLDOWN_MIN)
                                      or DEFAULT_COOLDOWN_MIN)) * 60
            last = _STATE["cooldowns"].get(key)
            if last is not None and (now - float(last)) < cooldown_sec:
                return False
        if len(_STATE["cooldowns"]) >= _MAX_COOLDOWN_KEYS:
            _STATE["cooldowns"].clear()
        _STATE["cooldowns"][key] = now
        _STATE["count"] += 1
        return True


# ── 提示词 / 解析 / 接地（纯函数，直接进门禁）────────────────────────────────

def build_extract_prompt(text: str, missing: List[Dict[str, Any]]) -> str:
    """按缺口槽位生成摘录式抽取提示词（值≤30字，禁推断）。"""
    lines = [
        "你是严格的信息摘录器。从下面这条客户消息里抽取字段，只输出一个 JSON 对象。",
        "硬规则：",
        "- 只许摘录消息里明确说出的内容（原文片段或原文数字），禁止推断、翻译、补全、编造。",
        "- 消息里没提到的字段不要输出；一个都没有就输出 {}。",
        "- 每个值不超过 30 字，保持消息原文的语言。",
        "候选字段（key: 含义）：",
    ]
    for s in missing[:8]:
        lines.append(f"- {s['key']}: {s.get('label_zh') or s['key']}"
                     f"（{s.get('ask_zh') or ''}）")
    lines.append("客户消息：")
    lines.append(f"<<<{str(text or '')[:1500]}>>>")
    lines.append("只输出 JSON：")
    return "\n".join(lines)


def parse_extraction(raw: Optional[str]) -> Dict[str, str]:
    """LLM 输出 → {slot_key: value}。剥代码栅栏、取首个 JSON 对象；
    非注册槽位/非字符串值/空值全丢。失败 → {}。"""
    s = str(raw or "").strip()
    if not s:
        return {}
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in data.items():
        key = str(k or "").strip().lower()
        if get_slot(key) is None or not isinstance(v, (str, int, float)):
            continue
        val = re.sub(r"\s+", " ", str(v).strip())[:_MAX_VALUE_CHARS]
        if val:
            out[key] = val
    return out


_SHORT_VALUE_MAX = 3
_NORM_STRIP_RE = re.compile(r"[\s，。,.!！?？、;；:：'\"“”‘’()（）]+")


def _norm_literal(s: str) -> str:
    return _NORM_STRIP_RE.sub("", str(s or "")).casefold()


def _short_value_anchored(val: str, msg: str) -> bool:
    """超短值的字面锚定（C3 复核补丁，2026-09-04）。

    ``fact_grounded_in_user_msg`` 对取不出内容 token 的值（单个 CJK 字 /
    单位数 / 「3万」「男」这类）**保守放行**——那是给长句记忆设计的「无从
    判断」分支；画像槽的值却常常就是这么短，放行＝LLM 编个数字就进长期画像
    （实测：原话「公司有5个人」，编造「预算 3万」「性别 男」全过）。
    对规范化后 ≤3 字的值，额外要求它是客户原话的字面子串；长值仍完全交给
    护栏本身判（不另算一套阈值）。
    """
    v = _norm_literal(val)
    if len(v) > _SHORT_VALUE_MAX:
        return True
    return bool(v) and v in _norm_literal(msg)


def ground_extracted(text: str, extracted: Dict[str, str]) -> Dict[str, str]:
    """摘录接地：只留锚定在**客户原话**上的值（宁可漏采不错采）。

    单一事实源＝``memory_grounding.fact_grounded_in_user_msg``（事故金标），
    必过、不绕开；本地不另算 bigram 阈值——两套会在边界上分叉，分叉就是漏网。
    唯一叠加＝``_short_value_anchored``：护栏「无从判断」的超短值补字面锚定。
    """
    msg = str(text or "").strip()
    if not msg:
        return {}
    out: Dict[str, str] = {}
    for k, v in (extracted or {}).items():
        val = str(v or "").strip()
        if not val:
            continue
        try:
            if (fact_grounded_in_user_msg(val, msg)
                    and _short_value_anchored(val, msg)):
                out[str(k)] = val
        except Exception:
            # 判定器自身异常 → 不入库（画像比记忆更难清，宁可漏）
            logger.debug("profile grounding failed", exc_info=True)
    return out


# ── 执行 / 调度 ──────────────────────────────────────────────────────────────

async def run_llm_capture(
    ai_client: Any,
    store: Any,
    *,
    platform: str,
    chat_key: str,
    text: str,
    missing: List[Dict[str, Any]],
    now: Optional[float] = None,
) -> int:
    """一次 LLM 摘录 → 接地 → 只填空槽入库。返回实际写入槽位数；绝不抛。"""
    try:
        prompt = build_extract_prompt(text, missing)
        raw = await ai_client.chat(prompt)
        data = parse_extraction(raw)
        if not data:
            return 0
        allowed = {m["key"] for m in missing}
        grounded = {k: v for k, v in ground_extracted(text, data).items()
                    if k in allowed}
        # #108（实施91）：语义体检——接地只验出处不验语义，「坐标=English」
        # 这类「原文里确实有这个词但塞错槽」的值在此拦下（单点在 profile_slots，
        # 正则/LLM 两轨同吃）。
        try:
            from src.companion.goals.profile_slots import slot_value_suspect
            _bad = {k: slot_value_suspect(k, v) for k, v in grounded.items()}
            for k, why in _bad.items():
                if why:
                    logger.info("[goal-profile-llm] 槽位值语义不合格丢弃 "
                                "%s=%r（%s）", k, grounded[k][:20], why)
            grounded = {k: v for k, v in grounded.items() if not _bad.get(k)}
        except Exception:
            pass
        if not grounded:
            return 0
        store.upsert_customer_profile(
            str(platform or ""), str(chat_key or ""), grounded,
            source=LLM_PENDING_SRC, now=now)
        try:
            from src.companion.goals.stats import get_goal_stats
            get_goal_stats().record_profile_captured_llm(len(grounded))
        except Exception:
            pass
        logger.info("[goal-profile-llm] %s:%s 补 %d 槽: %s",
                    platform, chat_key, len(grounded),
                    ",".join(grounded.keys()))
        return len(grounded)
    except Exception:
        logger.debug("run_llm_capture failed", exc_info=True)
        return 0


def schedule_llm_capture(
    ai_client: Any,
    store: Any,
    cfg_root: Any,
    *,
    platform: str,
    chat_key: str,
    text: str,
    fields: Optional[Dict[str, Any]] = None,
    include: Optional[List[str]] = None,
    now: Optional[float] = None,
) -> bool:
    """门控通过 → 在当前事件循环 fire-and-forget 一次 LLM 摘录。
    返回是否已调度。无运行 loop / 未启用 / 无缺口 / 冷却预算不过 → False。

    ``include``（P27）：摸底目标的勾选槽位——提示词只问坐席点名要采的缺口
    （全 11 槽提示词又长又散，抽取面越大误摘面越大）；None=全轨旧行为。
    """
    try:
        cfg = resolve_llm_cfg(cfg_root)
        if not cfg.get("enabled") or ai_client is None or store is None:
            return False
        t = str(text or "").strip()
        min_chars = max(1, int(cfg.get("min_chars", DEFAULT_MIN_CHARS)
                               or DEFAULT_MIN_CHARS))
        if len(t) < min_chars or len(t) > 2000:
            return False
        miss = (missing_slots(fields, include=list(include), limit=8)
                if include else missing_slots(fields, track="", limit=8))
        if not miss:
            return False
        # 名额有限（每日预算 + 每会话上限），别把它花在纯寒暄上
        if not looks_worth_extracting(t):
            return False
        n = float(now if now is not None else time.time())
        if not _acquire_slot(cfg, f"{platform}:{chat_key}", n,
                             discovery=in_discovery(cfg, fields)):
            return False
        loop = asyncio.get_running_loop()
    except Exception:
        return False
    coro = run_llm_capture(
        ai_client, store, platform=platform, chat_key=chat_key,
        text=t, missing=miss, now=now)
    try:
        loop.create_task(coro)
        return True
    except Exception:
        coro.close()
        return False


__all__ = [
    "LLM_PENDING_SRC",
    "PENDING_LABEL",
    "build_extract_prompt",
    "ground_extracted",
    "in_discovery",
    "looks_worth_extracting",
    "parse_extraction",
    "reset_state",
    "resolve_llm_cfg",
    "run_llm_capture",
    "schedule_llm_capture",
]
