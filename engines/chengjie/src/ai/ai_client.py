"""
AI 大模型 API 客户端
负责对话生成、上下文构建与熔断等。
支持：Google Gemini 原生 API (google-genai)，或 OpenAI 兼容 HTTP API（Ollama 等）。
"""

import asyncio
import json
import re
import time
from collections import deque
from typing import Dict, Any, Optional, List, Tuple
import logging

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

try:
    from openai import AsyncOpenAI
    OPENAI_SDK_AVAILABLE = True
except ImportError:
    AsyncOpenAI = None  # type: ignore
    OPENAI_SDK_AVAILABLE = False

from src.utils.logger import LoggerMixin
from src.utils.domain_policy import effective_domain_name

# 事实锁（2026-07-15 事故修复）：防重复/角度系统只约束「表达要不一样」，
# 没约束「事实要一致」——高温重生时 LLM 把"还没吃饭"改写成"刚吃了面包"。
# 所有多样性压力提示（角度轮换/复读重试/重复消息话术）注入时统一追加本行。
def _is_lan_base_url(url: str) -> bool:
    """局域网端点（现网 173 / 140 / 本机）。keep-alive 复用死连接是 Connection error 主因。"""
    try:
        host = str(url or "").split("://", 1)[-1].split("/", 1)[0].split(":")[0].strip().lower()
    except Exception:
        return False
    if host in ("localhost",):
        return True
    if host.startswith(("192.168.", "10.", "127.")):
        return True
    if host.startswith("172."):
        parts = host.split(".")
        try:
            return 16 <= int(parts[1]) <= 31
        except (TypeError, ValueError, IndexError):
            return False
    return False


def _lan_httpx_async_client(timeout: Any) -> Any:
    """LAN vLLM 专用 httpx：关掉 keep-alive。

    uvicorn 默认 ``timeout_keep_alive=5``，httpx 复用已被收掉的 socket →
    ``APIConnectionError: Connection error.``（vLLM 指标上看不到，因为请求没进引擎）。
    LAN 握手 <1ms，关 keep-alive 比复用死连接便宜。
    """
    import httpx
    return httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=16),
    )


FACT_LOCK_LINE = (
    "【事实锁——优先级高于换角度】只能换措辞、开头、语气、角度；"
    "绝不能改变你已说过的事实与状态（吃没吃饭、在哪里、正在做什么、"
    "身体状况、答应过什么）。上一条说过的事实本条必须保持一致，"
    "宁可不提也不要说反。"
)


def build_time_context_line(now: Any = None, *, place_label: str = "") -> str:
    """陪伴域「当前真实时间」prompt 行（纯函数，Phase19 + 2026-07-15 作息白名单）。

    LLM 训练数据里没有"现在几点"，不注入就会深夜说"下午好"；清晨/深夜两个高危
    时段额外加作息合理性硬约束——清晨 7:44 说「刚下课回来」这类穿帮（LLM 对
    "这个点什么事还没发生"没有常识保证）。

    ``now`` 应为**人设当地** naive 时间（由调用方经 ``persona_now`` 换算后传入）；
    ``place_label`` 非空时标明「当地」以免与服务器时钟混淆。
    """
    import datetime as _dt
    _t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    # 与 persona_location.daypart_label 阈值对齐（避免「时间行」与「当地时间行」时段词打架）
    try:
        from src.companion.persona_location import daypart_label as _daypart
        _tod = _daypart(_t.hour, "zh")
    except Exception:
        _h = _t.hour
        _tod = ("清晨" if 5 <= _h < 8 else "上午" if 8 <= _h < 12 else
                "中午" if 12 <= _h < 14 else "下午" if 14 <= _h < 18 else
                "傍晚" if 18 <= _h < 20 else "晚上" if 20 <= _h < 23 else "深夜")
    _where = f"（{place_label}当地）" if str(place_label or "").strip() else ""
    # 星期几必须显式给出：LLM 从日期心算星期不可靠（实录：周日被说成
    # "Saturday evening"），而星期词一旦说错会被手机端 DataDetector 下划线
    # 高亮成可点击证物，是验证成本最低的穿帮。
    _wd = "周" + "一二三四五六日"[_t.weekday()]
    line = (
        "【当前真实时间】" + _t.strftime("%Y-%m-%d ") + _wd + _t.strftime(" %H:%M")
        + f"（{_tod}）{_where}。今天是{_wd}——提到星期/周末必须与此一致。"
          "问候语、作息话题、以及照片场景标记里的光线时段"
          "都必须符合这个时间（深夜就是室内暖光/夜景，不要白天场景）。"
          "凡涉及日期的推理（票据/行程/纪念日的先后、隔了几天）一律以上面日期"
          "为「今天」计算——早于今天的日期是已经发生的过去，不要当成未来安排。")
    if _tod in ("清晨", "early morning"):
        line += (
            "\n【作息合理性】现在是清晨：合理状态只有刚睡醒/洗漱/准备出门/"
            "还没吃早饭这类；绝不能说「刚下课/刚下班/刚逛街回来」——"
            "这个点这些事根本还没发生。")
    elif _tod in ("深夜", "late night"):
        line += (
            "\n【作息合理性】现在是深夜：合理状态是宅在室内/准备睡/失眠刷手机；"
            "不要说刚从学校、公司、商场这类白天场合回来。")
    return line


def order_pool_entries(entries: List[Dict[str, Any]],
                       ping_state: Optional[Dict[str, Dict[str, Any]]] = None,
                       *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """备用 Key 池智能排序（纯函数）：健康证据多的排前，主 Key 挂时第一击就命中好钥匙。

    排序键（升序）：
      1. 冷却态（未冷却在前——冷却条目调用方本就跳过，排后只为快照展示一致）；
      2. 探活档位：ping ok(0) < 无数据(1) < ping fail(2)（fail 仍保留在队里——ping
         可能已过期，真轮到它时再试一次并由 120s 冷却兜底）；
      3. 运行态新鲜度：最近真实出过话的在前（-last_ok_ts）；
      4. 探活延迟小者在前；
      5. 配置序（boss 的显式意图作最终 tie-break）。
    """
    ping_state = ping_state or {}
    ts = float(now if now is not None else time.time())

    def _rank(pair):
        idx, e = pair
        cooling = 1 if float(e.get("bad_until") or 0.0) > ts else 0
        p = ping_state.get(str(e.get("name") or ""))
        if p is None:
            ping_rank = 1
        else:
            ping_rank = 0 if p.get("ok") else 2
        last_ok = float(e.get("last_ok_ts") or 0.0)
        latency = float((p or {}).get("latency_ms") or 1e9)
        return (cooling, ping_rank, -last_ok, latency, idx)

    return [e for _, e in sorted(enumerate(entries), key=_rank)]


class AIClient(LoggerMixin):
    """AI 大模型 API 客户端（Gemini 或 OpenAI 兼容 / Ollama）"""

    # prompt 预算缺省（ai.prompt_budget_tokens 缺席时）；理由见 __init__ 注释
    _DEFAULT_PROMPT_BUDGET = 12000

    def __init__(self, config):
        """
        初始化 AI 客户端

        Args:
            config: 配置管理器实例
        """
        self.config = config
        # 深度人设 E1/E2 运行期依赖注入：用主配置定型同步 embedder/LLM（懒建，best-effort）。
        try:
            from src.companion.deep_persona_runtime import configure_from_config
            configure_from_config(config.config if hasattr(config, "config") else (
                config if isinstance(config, dict) else {}))
        except Exception:
            pass
        self.client = None  # genai.Client
        self._oa_client = None  # AsyncOpenAI（Ollama / OpenAI 兼容）
        self._oa_embed_client = None  # 可选：仅用于 embedding（如 DeepSeek 对话 + Ollama 向量）
        # 嵌入多端点双活（两台 LAN GPU 各跑 bge-m3）：按序尝试，异常端点进短冷却降权
        # （不剔除）。风格对齐 OllamaMTEngine 的 base_urls。空列表 = 单端点旧行为。
        self._oa_embed_clients: List[Any] = []   # [(base_url, AsyncOpenAI), ...]
        self._embed_url_bad_until: Dict[str, float] = {}
        # Embedding 熔断：向量端点（如本地 Ollama）宕机时，避免每条消息都徒劳连接+刷 WARNING。
        # 连续失败达阈值 → 冷却窗口内直接静默降级到关键词召回（记忆/语义检索零阻断）。
        # 多端点下仅当**全部端点**都失败才计一次 streak（单点抖动由端点冷却吸收）。
        self._embed_fail_streak = 0
        self._embed_unreachable_until = 0.0
        self._use_openai_compat = False
        self._oa_extra_body: Dict[str, Any] = {}  # 透传给 create() 的额外字段（如 think:false）
        self._ollama_native_base: Optional[str] = None  # 非 None 时走原生 /api/chat（绕过 /v1/ think 问题）
        # 主对话 LLM 容灾（ai.fallback，2026-07）：云主模型（DeepSeek）不可达/熔断开路时，
        # 回落 LAN GPU 本地模型出真话，替代 canned 占位句 —— 聊天主功能不再单点依赖公网云。
        self._fb_client: Optional[Any] = None
        self._fb_model: str = ""
        self._fb_extra_body: Dict[str, Any] = {}
        self._fb_native_base: Optional[str] = None  # Ollama 端点走原生 /api/chat（keep_alive/think 才被尊重）
        self._fb_timeout: float = 90.0
        self._fb_keep_alive: str = ""
        # 兜底上下文窗（2026-07-14 事故修复）：Ollama runner 默认 -c 4096，而完整
        # 人设+记忆+KB+历史的 prompt 实测可到 4300 tokens → llama-server 400
        # 「exceeds the available context size」拒答，长对话（最有价值的会话）兜底必挂。
        # num_ctx 随请求下发让 runner 以更大窗口装载；配合 _trim_messages_to_budget 双保险。
        self._fb_num_ctx: int = 8192
        self._oa_num_ctx: int = 0  # 主链原生 /api/chat 的 num_ctx（0=不下发，维持端侧默认）
        self._fb_calls = 0   # 仅兜底身份（as_primary=False）；本地主链不计
        self._fb_ok = 0
        # P1（本地优先 / 全本地）：ai.primary ∈ cloud（默认，行为不变）| local | local_only。
        # 此处先置默认，保证**任何构造路径**下该字段都存在；真实解析放在 fallback 配置
        # 之后（要有 _fb_client 才能校验「声明本地优先却没配本地端点」）。
        self._primary_mode = "cloud"
        # 老板锁（ai.primary_lock，2026-08-22）：真实解析随 primary 一起在 initialize()
        self._primary_lock = ""
        # 分级路由默认（真实解析在 initialize()；此处防直构对象缺属性，与熔断器同款）
        self._tiers_enabled = False
        self._tiers_default = "normal"
        self._tiers: Dict[str, Dict[str, Any]] = {}
        # 云端 Key 备用池（ai.key_pool，2026-07）：主 Key 失效/云端主链双失败时，先切池内
        # 备用云 Key（云质量优于本地兜底），全池失败才落 LAN 本地模型。entries 元素：
        # {name, client, model, label, bad_until}（bad_until=失败冷却，冷却期内跳过）。
        self._pool_entries: List[Dict[str, Any]] = []
        self._pool_calls = 0
        self._pool_ok = 0
        self._pool_last_key = ""
        # 多模型路由（ai.models + ai.task_routes）：任务→独立端点/模型，默认空=不路由
        self._route_clients: Dict[str, Dict[str, Any]] = {}
        self._task_routes: Dict[str, str] = {}
        # 降级判定证据（degradation_snapshot 用）：三条出话链各自最近一次成功时刻
        self._last_primary_ok_ts = 0.0
        self._last_pool_ok_ts = 0.0
        self._last_fb_ok_ts = 0.0
        self._provider = "gemini"
        self._key_is_placeholder = False  # initialize() 按真实配置覆盖；直构对象按「已配 key」处理
        self.system_prompt = ""
        self.model = "gemini-2.5-flash"
        self.temperature = 0.7
        self.max_tokens = 1024
        # Q-14 #262（D-Q10）：默认读超时 30 → 60s。桌面托管版走官网网关，网关 chat 路由
        # 预算 55s（website/lib/ai-gateway.ts ROUTE_BUDGET_MS.chat）：客户端 30s 先放弃
        # → 网关还在等上游 → nginx 记 499、客户端这一轮沉默（09-08 实测 2.4%）。
        # 不变量：客户端读超时 ≥ 网关路由预算 + 余量。显式配置了 ai.timeout 的存量不动。
        self.timeout = 60
        # prompt token 预算（系统提示 + 注入 + few-shot + 历史合计；0 = 不裁）。
        # 2026-09-11：6000 → 12000。生产实测一轮完整装配（人设 full + 记忆 + 目标 +
        # 场景 + KB + 12 条历史）约 10-12k token，6000 意味着**每一轮**都在裁；
        # 主链 deepseek-flash 1M 窗、备用硅基 V4-Flash 1M、LAN chatx 24k，12k 只占零头。
        self._prompt_budget_tokens = self._DEFAULT_PROMPT_BUDGET
        # 最近一次主链失败（供起草侧「AI 本轮未生成」灰标消费；pop 即清）
        self._last_fail: Optional[Dict[str, Any]] = None

        # 性能跟踪
        self.total_calls = 0
        self.total_tokens = 0
        self.last_call_time = 0

        from src.utils.quality_tracker import QualityTracker
        self._quality_tracker = QualityTracker(config.config if hasattr(config, "config") else {})
        self._config_path = getattr(config, 'config_path', None)

        self.max_conversation_history = 10
        # 熔断器：closed → open → half-open → closed/open 三态
        # （enabled/阈值在 initialize() 按配置覆盖；此处给安全默认，防直构对象缺属性）
        self._cb_enabled: bool = False
        self._cb_window_size: int = 20
        self._cb_fail_threshold: float = 0.5
        self._cb_open_seconds: int = 60
        self._cb_window: deque = deque(maxlen=20)
        self._cb_open_until: float = 0.0
        self._cb_half_open: bool = False
        self.logger.info("AI 客户端初始化")

    def _notify_primary_switch(self, prev: Dict[str, Any], ai_config: Dict[str, Any],
                               pa_append) -> None:
        """装载点切档通知（2026-09-17）：本次解析的 (effective, lock) 与台账上一次生效态不同
        → EventBus ``ai_primary_guard_alert kind=mode_switched``（tg-ywqz 订阅 ai_primary_guard）
        + 台账 ``switch_notified``。上一次生效态未知（首次/台账空）不发——海量测试构造与
        全新实例不该刷群。best-effort，绝不伤初始化。"""
        try:
            prev_eff = prev.get("effective") if isinstance(prev, dict) else None
            if prev_eff is None:
                return
            prev_lock = (prev.get("lock") or None) if isinstance(prev, dict) else None
            now_eff = self._primary_mode
            now_lock = self._primary_lock or None
            if prev_eff == now_eff and prev_lock == now_lock:
                return
            from src.ai.ai_primary_summary import build_summary
            summary = build_summary({"ai": ai_config}, effective=now_eff, lock=now_lock or "")
            payload = {
                "kind": "mode_switched",
                "from_mode": prev_eff,
                "to_mode": now_eff,
                "lock_from": prev_lock,
                "lock": now_lock,
                "effective": now_eff,
                "primary_text": summary.get("primary_text"),
                "chain_text": summary.get("chain_text"),
                "mode_label": summary.get("mode_label"),
                "via": "ai_client_init",
                "rate_key": f"ai_primary_guard:switched:{now_eff}:{now_lock or '-'}",
            }
            if pa_append:
                pa_append("switch_notified", mode_from=prev_eff, mode_to=now_eff,
                          lock_from=prev_lock, lock=now_lock, via="ai_client_init")
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ai_primary_guard_alert", payload)
            try:
                from src.ai.ai_primary_summary import nudge_compute_board
                nudge_compute_board()
            except Exception:
                pass
            self.logger.info("主链档位变化已通知运维群：%s/%s → %s/%s",
                             prev_eff, prev_lock or "-", now_eff, now_lock or "-")
        except Exception:
            self.logger.debug("切档通知失败（已忽略）", exc_info=True)

    async def initialize(self, *, defer_probe: bool = False) -> bool:
        """初始化 AI 客户端。

        ``defer_probe=True``（仅 main.py 冷启动路径传入，P3-1 2026-08-12 可靠性
        复盘）：启动连接探针改后台任务——boot phases 实测该探针是 init 段最大
        单项（DeepSeek 往返 4.3s），而 assistant.initialize() 根本不消费本方法的
        返回值，阻塞探针在 boot 路径纯花时间不改行为。坏 key 告警/日志由后台
        任务按原分支保留（只晚几秒）。``reload_ai_runtime``（桌面「模式切换是否
        生效」的 UX 消费这个布尔值）不传本参数 → 阻塞语义原样。
        """
        try:
            ai_config = self.config.get_ai_config()
            self._provider = (ai_config.get("provider") or "gemini").strip().lower()

            api_key = ai_config.get('api_key')
            # 占位/未配置 key（桌面首启种子留空、example 的 YOUR_*）＝预期未就绪态：
            # 连接测试必然失败，但这不是「云端 Key 异常」，不该弹主机告警（引导条负责提示）。
            _k = str(api_key or "").strip()
            self._key_is_placeholder = (not _k) or _k.upper().startswith("YOUR_")
            self.model = ai_config.get('model', 'gemini-2.5-flash')
            self.temperature = float(ai_config.get('temperature', 0.7))
            self.max_tokens = int(ai_config.get('max_tokens', 1024))
            self.timeout = int(ai_config.get('timeout', 60))
            try:
                self._prompt_budget_tokens = max(
                    0, int(ai_config.get('prompt_budget_tokens', self._DEFAULT_PROMPT_BUDGET) or 0))
            except Exception:
                self._prompt_budget_tokens = self._DEFAULT_PROMPT_BUDGET
            # 启动连接探针的独立上限（秒）：主链读超时 self.timeout 常为 60s，但**冷启动**
            # 时若云端(DeepSeek)被限流/抖动，这一次探针会占满整读超时，把「进程起来→/login
            # 可服务」的窗口拖到分钟级（seat 端撞加载超时）。探针结果 main 并不消费（仅日志 +
            # 坏 key 告警），故给它一个更短的独立上限；超时=按「未验证」放行（首个真实请求自会
            # 确认健康），绝不因慢云阻断启动。坏 key 走 401 快败（非超时）→ 告警仍即时。
            # 0/负 = 关闭上限（回退旧行为）。
            try:
                self._boot_probe_timeout = float(ai_config.get('boot_probe_timeout_sec', 6.0))
            except Exception:
                self._boot_probe_timeout = 6.0
            self.system_prompt = ai_config.get('system_prompt', '').strip()
            self._domain_system_prompt = ""
            self._domain_terminology = {}
            self._domain_context_supplements = {}
            self.max_conversation_history = int(ai_config.get('max_conversation_history', 10) or 10)
            self._embedding_model = ai_config.get('embedding_model', 'gemini-embedding-001')
            # ★ P5-4：对话分级路由 — 配置示例
            # ai.tiers:
            #   enabled: true
            #   default: normal
            #   premium: {model: gpt-4o, temperature: 0.6, max_tokens: 1200}
            #   normal:  {model: deepseek-chat, temperature: 0.7, max_tokens: 800}
            #   low:     {model: deepseek-chat, temperature: 0.8, max_tokens: 400}
            tiers_cfg = ai_config.get("tiers") or {}
            self._tiers_enabled = bool(tiers_cfg.get("enabled", False))
            self._tiers_default = str(tiers_cfg.get("default") or "normal")
            self._tiers: Dict[str, Dict[str, Any]] = {}
            for k, v in tiers_cfg.items():
                if k in ("enabled", "default"):
                    continue
                if isinstance(v, dict):
                    self._tiers[str(k)] = dict(v)
            if self._tiers_enabled:
                self.logger.info(
                    f"AI 分级路由启用，已加载 {len(self._tiers)} 档：{list(self._tiers.keys())}"
                )
            # ★ P6-4：加载 LLM 价格表 + 初始化 cost tracker
            try:
                from src.ai.llm_cost import get_llm_cost
                pricing = ai_config.get("pricing") or {}
                if pricing:
                    # 2026-09-08：价格表按 CNY/1K tokens 解释（与硅基账单同币种），
                    # ai.pricing_currency 可改；缺省 CNY。
                    get_llm_cost().set_pricing(
                        pricing, currency=str(ai_config.get("pricing_currency") or "CNY"))
                    self.logger.info(
                        f"LLM 成本追踪：已加载 {len(pricing)} 个模型的价格表"
                        f"（{get_llm_cost().currency}/1K tokens）"
                    )
            except Exception:
                self.logger.debug("LLM 成本追踪初始化失败", exc_info=True)
            _cb = ai_config.get('circuit_breaker') or {}
            self._cb_enabled = bool(_cb.get('enabled', False))
            self._cb_window_size = int(_cb.get('window_size', 20) or 20)
            self._cb_fail_threshold = float(_cb.get('fail_threshold', 0.5) or 0.5)
            self._cb_open_seconds = int(_cb.get('open_seconds', 60) or 60)
            self._cb_window = deque(maxlen=self._cb_window_size)

            if self._provider == "openai_compatible":
                return await self._initialize_openai_compatible(
                    ai_config, api_key, defer_probe=defer_probe)

            if not GENAI_AVAILABLE:
                self.logger.error("google-genai 库未安装，请运行: pip install google-genai")
                return False

            if not api_key or api_key == "YOUR_AI_API_KEY":
                self.logger.error("AI API 密钥未配置")
                self.logger.error("请在配置文件中填写 ai.api_key")
                return False

            self.client = genai.Client(api_key=api_key)

            test_result = await self._run_boot_probe(self._test_connection())
            if not test_result:
                self.logger.error("AI API 连接测试失败")
                return False

            self.logger.info(f"✅ AI 客户端初始化成功 — 原生 Gemini API (模型: {self.model})")
            return True

        except Exception as e:
            self.logger.error(f"初始化 AI 客户端失败: {e}")
            return False

    async def _initialize_openai_compatible(self, ai_config: Dict[str, Any], api_key: Optional[str],
                                            *, defer_probe: bool = False) -> bool:
        """Ollama / vLLM 等 OpenAI 兼容接口（base_url 形如 http://host:11434/v1）。"""
        self._oa_embed_client = None
        if not OPENAI_SDK_AVAILABLE:
            self.logger.error("openai 库未安装，请运行: pip install openai")
            return False
        raw_base = (ai_config.get("base_url") or "").strip().rstrip("/")
        if not raw_base:
            self.logger.error("openai_compatible 需要配置 ai.base_url，例如 http://100.x.x.x:11434/v1")
            return False
        if not raw_base.endswith("/v1"):
            raw_base = raw_base + "/v1"
        key = api_key if api_key and api_key != "YOUR_AI_API_KEY" else "ollama"
        # 连接 5s 快败 + 关 SDK 内建重试：生成调用方（_generate_reply_openai_compat 等）
        # 自带 2 次重试循环，SDK 再叠 2 次 = 最多 6 连击且断网黑洞时要等满整读超时；
        # 拆开后「云不可达 → 本地兜底」切换从分钟级降到 ~10s。读超时保持 self.timeout 不变。
        try:
            import httpx
            _oa_to: Any = httpx.Timeout(float(self.timeout), connect=5.0)
        except Exception:
            _oa_to = float(self.timeout)
        self._oa_client = AsyncOpenAI(api_key=key, base_url=raw_base,
                                      timeout=_oa_to, max_retries=0)
        self._use_openai_compat = True
        self.client = None
        self._oa_extra_body = {}
        self._ollama_native_base = None
        think_flag = ai_config.get("think")
        if think_flag is False:
            self._oa_extra_body["options"] = {"think": False}
            # vLLM(Qwen3 系)直答档：chat_template_kwargs 由 vLLM OpenAI 服务端消费
            # （enable_thinking=False=零思考 token，2026-08-15 主链 27B 换代配套）；
            # Ollama /v1 与云端点对未知字段一律忽略，同发无副作用。
            self._oa_extra_body["chat_template_kwargs"] = {"enable_thinking": False}
            # Auto-detect Ollama native endpoint: use /api/chat to properly honor think:false
            # (Ollama /v1/ compat endpoint ignores think flag in some versions)
            _root = raw_base[:-3] if raw_base.endswith("/v1") else raw_base
            if ai_config.get("ollama_native", True) and (":11434" in _root or "/ollama" in _root.lower()):
                self._ollama_native_base = _root.rstrip("/")
                self.logger.info("Ollama native /api/chat mode enabled: %s", self._ollama_native_base)
        # DeepSeek 官方退役别名归一（deepseek-chat / deepseek-v4-flash → deepseek-flash）：
        # 存量 overlay 不必手改，装载时归一并留日志（2026-09-11 V4.1-Flash 切链）。
        try:
            from src.ai.vendor_params import normalize_model, thinking_off_extra_body
            _norm_model, _norm_note = normalize_model(raw_base, self.model)
            if _norm_note:
                self.model = _norm_model
                self.logger.info(_norm_note)
        except Exception:
            thinking_off_extra_body = None  # type: ignore[assignment]
        # DeepSeek 系（V4 起，含 V4.1-Flash 的唯一模型名 deepseek-flash）是混合推理模型：
        # 思维链**默认开启**且与正文共享 max_tokens 预算——复杂 prompt 的长思考会把
        # 正文挤成空（finish=length、content 0 字、预算全在 reasoning_tokens），客户端
        # 只认 content → 「AI 返回空响应」×2 → 拦发弹窗。这就是 2026-08-17 全天
        # 「空响应」事故的根因（8/4 起累计 139 次，此前一直被本地兜底静默遮蔽）。
        # 陪聊/客服场景直答质量足够：官方参数 thinking.disabled 实测 0 推理 token、
        # 更快更省。要重新开思维链：ai.reasoning: true（届时必须同步调大
        # ai.max_tokens，给思考留预算）。
        # 2026-09-11 起**按端点主机**下发而非按模型名：官方对话模型只剩 deepseek-flash，
        # 退役别名过渡期照样路由到 V4.1，按名字判会漏；硅基混合档改发 enable_thinking:false
        # （否则 </think> 混进 content）。字段口径见 src/ai/vendor_params.py。
        if thinking_off_extra_body is not None:
            _toff = thinking_off_extra_body(raw_base, self.model,
                                            reasoning=bool(ai_config.get("reasoning")))
            if _toff:
                for _k, _v in _toff.items():
                    self._oa_extra_body.setdefault(_k, _v)
                self.logger.info("思维链已关闭 model=%s host=%s via=%s（ai.reasoning: true 可开启）",
                                 self.model, raw_base.split("://", 1)[-1].split("/", 1)[0],
                                 ",".join(_toff.keys()))
        # 主链为 Ollama 时可显式配 ai.num_ctx 扩上下文窗（默认 0=不下发，行为不变）
        try:
            self._oa_num_ctx = max(0, int(ai_config.get("num_ctx") or 0))
        except Exception:
            self._oa_num_ctx = 0

        # 嵌入端点：embedding_base_urls（列表，双活）优先；否则 embedding_base_url（单端点）。
        # 其他消费方（KB embed-all / eval provider / readiness）仍读单数键 —— 双活仅覆盖
        # ai_client.embed()（记忆召回 + 翻译语义闸门等热路）。
        _emb_cfg = ai_config.get("embedding_base_urls") or ai_config.get("embedding_base_url") or ""
        if isinstance(_emb_cfg, (list, tuple)):
            _emb_urls = [str(u or "").strip() for u in _emb_cfg]
        else:
            _emb_urls = [u.strip() for u in str(_emb_cfg).split(",")]
        _emb_urls = [u for u in _emb_urls if u]
        self._oa_embed_clients = []
        if _emb_urls:
            emb_key = (ai_config.get("embedding_api_key") or key or "ollama").strip()
            if emb_key == "YOUR_AI_API_KEY":
                emb_key = "ollama"
            # 连接 5s 快败 + 关 SDK 内建重试（与主/兜底客户端同款；2026-08-01 实锤补齐）：
            # 嵌入是增强层（端点挂 → 降级关键词召回），但旧构造缺 connect 超时且吃 SDK
            # 默认 2 重试——176 宕机时一次 embed = 3 次 TCP 连接尝试 × Windows ~21s
            # ≈ 65s，坐席拟稿凭空多等一分钟（生产 [smart_reply] gen=68269 实测）；
            # 端点冷却仅 60s，主机持续宕机 = 每分钟都有一条请求吃满惩罚。快败后
            # 死端点代价 ≤5s 即转下一端点。读超时 20s（bge-m3 热态批量嵌入亚秒级，
            # 10 倍余量），同时把「半死」（TCP 通但不回包）的敞口从 self.timeout 收紧。
            try:
                import httpx
                _emb_to: Any = httpx.Timeout(20.0, connect=5.0)
            except Exception:
                _emb_to = 20.0
            for _u in _emb_urls:
                _base = _u.rstrip("/")
                if not _base.endswith("/v1"):
                    _base = _base + "/v1"
                self._oa_embed_clients.append((_base, AsyncOpenAI(
                    api_key=emb_key, base_url=_base, timeout=_emb_to,
                    max_retries=0)))
            self._oa_embed_client = self._oa_embed_clients[0][1]
            self.logger.info(
                "Embedding 使用独立端点 x%d: %s", len(self._oa_embed_clients),
                ", ".join(u for u, _ in self._oa_embed_clients))

        # 本地兜底对话模型（默认关；base_url+model 齐备才启用）。连接 5s 快败：
        # 兜底只在主链已坏时被调，此刻用户已在等，不能再被死端点吃满长超时。
        self._fb_client = None
        self._fb_model = ""
        self._fb_extra_body = {}
        self._fb_native_base = None
        fb_cfg = ai_config.get("fallback") or {}
        # 无兜底纪律（2026-08-17 老板拍板）：``ai.fallback.chat_fallback: false`` =
        # 云主链失败**不许**本地顶班出话（宁可不回 + 弹窗），端点配置保留——
        # ``_local_tool_chat``（口语化改写等工具调用）与 ``ai.primary=local*``
        # 主链身份不受本键影响（那些是指定链路，不是兜底替代品）。
        self._fb_chat_fallback_enabled = bool(
            (fb_cfg or {}).get("chat_fallback", True)
        ) if isinstance(fb_cfg, dict) else True
        if isinstance(fb_cfg, dict) and fb_cfg.get("enabled") and str(fb_cfg.get("base_url") or "").strip():
            fb_base = str(fb_cfg.get("base_url")).strip().rstrip("/")
            if not fb_base.endswith("/v1"):
                fb_base = fb_base + "/v1"
            fb_key = str(fb_cfg.get("api_key") or "ollama").strip() or "ollama"
            self._fb_timeout = float(fb_cfg.get("timeout", 90))
            _fb_http = None
            try:
                import httpx
                _fb_to: Any = httpx.Timeout(self._fb_timeout, connect=5.0)
                if _is_lan_base_url(fb_base):
                    _fb_http = _lan_httpx_async_client(_fb_to)
            except Exception:
                _fb_to = self._fb_timeout
            self._fb_client = AsyncOpenAI(
                api_key=fb_key, base_url=fb_base, timeout=_fb_to, max_retries=0,
                **({"http_client": _fb_http} if _fb_http is not None else {}))
            self._fb_model = str(fb_cfg.get("model") or "").strip()
            # 兜底默认按 think:false 处理（qwen3 思考系模型防慢答；instruct 系无感）
            if fb_cfg.get("think", False) is False:
                self._fb_extra_body = {
                    "options": {"think": False},
                    # vLLM(Qwen3 系)直答档（ai.primary=local* 时本块即主链）：
                    # enable_thinking=False 消灭思考 token；非 vLLM 端点忽略未知字段。
                    "chat_template_kwargs": {"enable_thinking": False},
                }
            # keep_alive：断云期让兜底模型驻留显存——只有第一个用户吃冷载（实测 ~27s），
            # 后续热答秒级。Ollama 的 /v1 兼容层**不认** keep_alive/think（实测 0.31 直接忽略），
            # 故 Ollama 端点（:11434）改走原生 /api/chat（与主链 _ollama_native_chat 同策略）。
            self._fb_keep_alive = str(fb_cfg.get("keep_alive") or "30m").strip()
            # 窗口口径（2026-09-18）：不再一律落 8192。显式 num_ctx > ai.unrestricted.max_ctx >
            # Ollama 8192 > vLLM/私网 24576——同一台 173 在主链与无限制路由终于用同一个数。
            try:
                from src.ai.vendor_params import resolve_local_num_ctx as _rlnc
                self._fb_num_ctx, _fb_ctx_src = _rlnc(fb_cfg, ai_config)
            except Exception:
                self._fb_num_ctx, _fb_ctx_src = 8192, "legacy"
            _fb_root = fb_base[:-3].rstrip("/")
            if ai_config.get("fallback_native", True) and (":11434" in _fb_root or "/ollama" in _fb_root.lower()):
                self._fb_native_base = _fb_root
            self.logger.info(
                "本地兜底对话模型已配置: %s @ %s（主模型不可达/熔断时启用%s）num_ctx=%d[%s]",
                self._fb_model or "?", fb_base,
                "，原生 /api/chat" if self._fb_native_base else "",
                self._fb_num_ctx, _fb_ctx_src)

        # ── P1：本地优先 / 全本地模式（自托管 + 隐私敏感客户）───────────────────
        # ``ai.primary``：
        #   cloud（默认）：云主链 → 备用池 → 本地兜底 → 全灭则本轮不回复。
        #   local        ：**本地模型即主链**；本地失败仍可回落云端（可用性优先）。
        #   local_only   ：本地模型即主链，且**绝不把用户内容发往云端**（严格隐私）；
        #                  本地失败 → 本轮不回复（宁可沉默，也不泄数据/乱回复）。
        #
        # 覆盖面如实声明（别把它讲成「全链数据不出本地」）：本键只管**主对话链**，
        # 即数据量最大的那条。嵌入 / 视觉 / 翻译各有自己的端点配置（本部署本就是
        # LAN GPU），不由本键代管。
        #
        # 配错保护：声明本地优先却没配 ``ai.fallback`` 端点 → 记 ERROR 并退回 cloud。
        # 「无声降级」不好，但把聊天变砖更糟。
        self._primary_mode = str(ai_config.get("primary") or "cloud").strip().lower()
        if self._primary_mode not in ("cloud", "local", "local_only"):
            self.logger.error(
                "ai.primary 非法值 %r（应为 cloud|local|local_only），按 cloud 处理",
                self._primary_mode)
            self._primary_mode = "cloud"
        # ── 老板锁 ``ai.primary_lock``（2026-08-22，事故沉淀见 ai_primary_audit 模块头）──
        # 收口点刻意选「生效点」而非各写入口：手改 overlay / 外部自动化（SSH 改文件）/
        # 任何绕过治理接口的途径，最终都要经本次解析才能生效——在这里拦=全途径拦。
        # 锁不凌驾下方「本地端点缺失退 cloud」防变砖护栏（安全 > 治理）。
        _pa_append = None
        _lock_cfg_mode = self._primary_mode
        try:
            from src.ai.ai_primary_audit import append_event as _pa_append  # noqa: F811
            from src.ai.ai_primary_audit import resolve_lock as _pa_lock
            self._primary_lock = _pa_lock(ai_config)
        except Exception:
            self._primary_lock = ""
        _lock_enforced = bool(
            self._primary_lock and self._primary_mode != self._primary_lock)
        if _lock_enforced:
            self.logger.warning(
                "ai.primary=%s 与 primary_lock=%s 不符 → 按锁强制生效"
                "（解除需运营在 overlay 显式删改 ai.primary_lock 并知会老板）",
                _lock_cfg_mode, self._primary_lock)
            self._primary_mode = self._primary_lock
        if self._primary_mode != "cloud" and not (self._fb_client and self._fb_model):
            self.logger.error(
                "ai.primary=%s 但 ai.fallback 本地端点未配置（需 enabled+base_url+model），退回 cloud",
                self._primary_mode)
            self._primary_mode = "cloud"
        if self._primary_mode != "cloud":
            self.logger.info(
                "本地优先模式已启用: primary=%s model=%s（%s）",
                self._primary_mode, self._fb_model,
                "严格隐私：本地失败也不回落云端"
                if self._primary_mode == "local_only" else "本地失败可回落云端")
        # 审计 + 回写 + 告警（终态确定后做；全部 best-effort，绝不伤初始化主链）
        # 切档通知（2026-09-17）：与台账里上一次生效态比对——档位或锁变了就通知运维群。
        # 挂在装载点而非切换接口：09-17 的 cloud→local 是直接改 overlay 完成的，没走接口。
        _prev_state: Dict[str, Any] = {"effective": None, "lock": None}
        if _pa_append:
            try:
                from src.ai.ai_primary_audit import last_state as _pa_last
                _prev_state = _pa_last()
            except Exception:
                _prev_state = {"effective": None, "lock": None}
        if _lock_enforced:
            if _pa_append:
                _pa_append(
                    "lock_enforced", configured=_lock_cfg_mode,
                    lock=self._primary_lock, effective=self._primary_mode,
                    via="ai_client_init")
            # 回写 overlay 让配置文件回到真话（仅在锁真的落成生效值时写，
            # 「锁 local* 但端点缺失被安全护栏压回 cloud」的分叉态不回写谎话）
            if self._primary_mode == self._primary_lock:
                try:
                    if hasattr(self.config, "set_overlay_flag"):
                        self.config.set_overlay_flag("ai.primary", self._primary_lock)
                        self.logger.info(
                            "ai.primary 已按锁回写 overlay = %s", self._primary_lock)
                except Exception:
                    self.logger.debug("ai.primary 锁回写失败（已忽略）", exc_info=True)
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("ai_primary_guard_alert", {
                    "kind": "lock_enforced",
                    "from_mode": _lock_cfg_mode,
                    "lock": self._primary_lock,
                    "effective": self._primary_mode,
                    "rate_key": "ai_primary_guard:lock",
                })
            except Exception:
                pass
        elif _pa_append and (self._primary_lock or self._primary_mode != "cloud"):
            # 常规解析留痕（锁在场或非 cloud 档才记——默认云档的海量测试构造不刷台账）
            _pa_append(
                "resolve", configured=_lock_cfg_mode,
                effective=self._primary_mode,
                lock=self._primary_lock or None, via="ai_client_init")
        self._notify_primary_switch(_prev_state, ai_config, _pa_append)

        # 云端 Key 备用池：主 Key 坏了先切备用云 Key（质量与主链同级），全池失败才落本地。
        # 池条目缺省继承主链 base_url/model → 「同厂商备用号」只填 api_key 即可。
        self._pool_entries = []
        kp = ai_config.get("key_pool") or {}
        if isinstance(kp, dict) and kp.get("enabled", True):
            _primary_key = str(ai_config.get("api_key") or "").strip()
            seen_pool: set = set()
            for i, item in enumerate(kp.get("keys") or []):
                if not isinstance(item, dict):
                    continue
                p_key = str(item.get("api_key") or "").strip()
                if not p_key or p_key.upper().startswith("YOUR_"):
                    continue
                p_base = str(item.get("base_url") or ai_config.get("base_url") or "").strip().rstrip("/")
                if not p_base or "://" not in p_base:
                    continue
                if not p_base.endswith("/v1"):
                    p_base = p_base + "/v1"
                # 与主 Key 完全相同 / 池内重复条目＝复制粘贴事故，静默去重
                dedup = (p_base, p_key)
                if (p_key == _primary_key and p_base == raw_base) or dedup in seen_pool:
                    continue
                seen_pool.add(dedup)
                p_model = str(item.get("model") or self.model or "").strip()
                p_name = str(item.get("name") or f"key{i + 1}").strip()
                # 池条目分厂商：退役名归一 + 各自的「关思维链」字段（主链的 extra_body 是
                # 主链端点的口径，硅基备用池套用它会让 </think> 混进正文）。
                p_extra: Dict[str, Any] = {}
                try:
                    from src.ai.vendor_params import normalize_model, thinking_off_extra_body
                    p_model, _p_note = normalize_model(p_base, p_model)
                    if _p_note:
                        self.logger.info("备用池 %s：%s", p_name, _p_note)
                    p_extra = thinking_off_extra_body(
                        p_base, p_model, reasoning=bool(item.get("reasoning", ai_config.get("reasoning"))))
                except Exception:
                    p_extra = {}
                try:
                    import httpx as _hx
                    _p_to: Any = _hx.Timeout(float(self.timeout), connect=5.0)
                except Exception:
                    _p_to = float(self.timeout)
                p_host = p_base.split("://", 1)[1].split("/", 1)[0]
                self._pool_entries.append({
                    "name": p_name,
                    "client": AsyncOpenAI(api_key=p_key, base_url=p_base,
                                          timeout=_p_to, max_retries=0),
                    "model": p_model,
                    "label": f"{p_model} @ {p_host} ({p_name})",
                    "extra_body": p_extra,
                    "bad_until": 0.0,
                    "last_ok_ts": 0.0,
                })
            if self._pool_entries:
                self.logger.info(
                    "云端 Key 备用池已配置: %d 个（%s）",
                    len(self._pool_entries),
                    ", ".join(e["name"] for e in self._pool_entries))

        # 多模型路由（ai.models + ai.task_routes）：主动按任务挑端点/模型（与 key_pool 正交）
        self._build_route_clients(ai_config, api_key)

        # boot 探针后台化（P3-1）：见 initialize() docstring。分支逻辑与阻塞路径
        # 完全同构（_deferred_boot_probe），只是不再挡「进程起来 → /login 可服务」。
        if defer_probe:
            try:
                self._boot_probe_task = asyncio.create_task(self._deferred_boot_probe())
            except Exception:
                self._boot_probe_task = None
                self.logger.debug("后台启动探针创建失败（忽略，首个真实请求自会确认健康）",
                                  exc_info=True)
            self.logger.info(
                "✅ AI 客户端初始化成功 — OpenAI 兼容 API (模型: %s, base: %s；"
                "启动探针已后台化)", self.model, raw_base)
            return True

        # 启动探针按 primary 模式分流（P1）：local/local_only 先探**本地主链**。
        # 纯本地部署常根本没配云端 key——旧逻辑只探云端，会把「本地一切就绪」误判成
        # 初始化失败：reload_ai_runtime 因此拒绝换绑（模式切换永远显示未生效），
        # 启动期也会被标成 AI 未就绪。云端在这两档里只是回落/不使用，探针失败
        # 不该一票否决初始化。
        if self._primary_mode in ("local", "local_only"):
            local_ok = await self._run_boot_probe(self._test_local_connection())
            if local_ok:
                self.logger.info(
                    "✅ AI 客户端初始化成功 — 本地主链 (模型: %s%s)",
                    self._fb_model,
                    "；local_only 不探云端" if self._primary_mode == "local_only"
                    else "；云端仅作回落不阻断启动")
                return True
            if self._primary_mode == "local_only":
                self.logger.error(
                    "本地主链连接测试失败且 local_only（无云端可回落）——"
                    "请检查 ai.fallback.base_url / 模型是否已拉起")
                return False
            self.logger.warning("本地主链探针失败，按 local 语义回落云端探针")

        test_result = await self._run_boot_probe(self._test_openai_connection())
        if not test_result:
            self.logger.error(
                "OpenAI 兼容 API 连接测试失败（请检查 base_url、密钥、网络及模型名）"
            )
            return False

        self.logger.info("✅ AI 客户端初始化成功 — OpenAI 兼容 API (模型: %s, base: %s)", self.model, raw_base)
        return True

    async def _deferred_boot_probe(self) -> None:
        """boot 后台探针（P3-1）：跑与阻塞路径相同的分支，仅产日志/坏 key 告警。

        绝不回写初始化状态——runtime 自有 主链重试 → 备用池 → 本地兜底 → canned
        的降级链，探针结论只是观测。任何异常吞掉（探针不能反过来伤 boot）。
        """
        try:
            if self._primary_mode in ("local", "local_only"):
                if await self._run_boot_probe(self._test_local_connection()):
                    self.logger.info(
                        "AI 启动探针（后台）：本地主链就绪 (模型: %s)", self._fb_model)
                    return
                if self._primary_mode == "local_only":
                    self.logger.error(
                        "AI 启动探针（后台）：本地主链不可达且 local_only——"
                        "请检查 ai.fallback.base_url / 模型是否已拉起")
                    return
                self.logger.warning("AI 启动探针（后台）：本地主链失败，改探云端")
            if await self._run_boot_probe(self._test_openai_connection()):
                self.logger.info("AI 启动探针（后台）：云端主链就绪 (模型: %s)", self.model)
            else:
                self.logger.error(
                    "AI 启动探针（后台）：云端连接测试失败（坏 key 告警已按原路径触发；"
                    "运行时降级链「重试→备用池→本地兜底→canned」不受影响）")
        except Exception:
            self.logger.debug("后台启动探针异常（忽略）", exc_info=True)

    async def _run_boot_probe(self, probe_coro) -> bool:
        """给启动连接探针套一个短上限（self._boot_probe_timeout）。

        - 探针在上限内返回 → 用其真实结果（成功/失败，失败已在探针内记日志+坏key告警）。
        - 探针超时 → 记一行「探针超时，按未验证放行」的 WARNING 并返回 True（不阻断启动、
          不误报坏 key；首个真实请求会确认健康）。
        - 上限 <=0 → 不设限，直接 await（回退旧行为）。
        任何包装层异常都软失败为 True（绝不让观测/上限逻辑拖垮启动）。
        """
        timeout = getattr(self, "_boot_probe_timeout", 0.0) or 0.0
        if timeout <= 0:
            return await probe_coro
        try:
            return await asyncio.wait_for(probe_coro, timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.warning(
                "AI 启动连接探针超过 %.1fs 上限（云端可能限流/抖动）；按未验证放行，"
                "首个真实请求将确认健康。", timeout,
            )
            return True
        except Exception:
            self.logger.debug("AI 启动连接探针包装异常（忽略，放行）", exc_info=True)
            return True

    async def _test_local_connection(self) -> bool:
        """本地主链启动探针（``ai.primary=local*``）：对 ``ai.fallback`` 端点打一句短 chat。

        与 ``_test_openai_connection`` 对称，但**不**触发坏 key 告警——本地 Ollama
        无凭证概念，失败多为「端点未起/模型未拉」，属部署问题而非凭证事故。
        """
        try:
            if not (self._fb_client and self._fb_model):
                return False
            messages = [{"role": "user", "content": "Say hi in one word."}]
            if self._fb_native_base:
                text, _, _ = await self._fb_native_chat(
                    messages, max_tokens=64, temperature=0.3)
            else:
                kw: Dict[str, Any] = dict(
                    model=self._fb_model, messages=messages,
                    max_tokens=64, temperature=0.3)
                if self._fb_extra_body:
                    kw["extra_body"] = self._fb_extra_body
                resp = await self._fb_client.chat.completions.create(**kw)
                text = ""
                if resp and getattr(resp, "choices", None):
                    c0 = resp.choices[0].message
                    text = (c0.content or "").strip()
                    if not text:
                        extra = getattr(c0, "model_extra", None) or {}
                        text = (extra.get("reasoning") or "").strip()
            if text:
                self.logger.info("本地主链连接测试成功 (model=%s)", self._fb_model)
                return True
            self.logger.error("本地主链连接测试返回空 (model=%s)", self._fb_model)
            return False
        except Exception as e:
            self.logger.error("本地主链连接测试失败: %s", e)
            return False

    async def _test_openai_connection(self) -> bool:
        try:
            if not self._oa_client:
                return False
            if self._ollama_native_base:
                text, _, _ = await self._ollama_native_chat(
                    messages=[{"role": "user", "content": "Say hi in one word."}],
                    max_tokens=64,
                    temperature=0.3,
                )
            else:
                create_kwargs: Dict[str, Any] = dict(
                    model=self.model,
                    messages=[{"role": "user", "content": "Say hi in one word."}],
                    max_tokens=512,
                    temperature=0.3,
                )
                if self._oa_extra_body:
                    create_kwargs["extra_body"] = self._oa_extra_body
                response = await self._oa_client.chat.completions.create(**create_kwargs)
                self._record_direct_usage(response, purpose="probe")
                text = ""
                if response and response.choices:
                    c0 = response.choices[0].message
                    text = (c0.content or "").strip()
                    if not text:
                        extra = getattr(c0, "model_extra", None) or {}
                        text = (extra.get("reasoning") or "").strip()
            if text:
                self.logger.info("AI API 连接测试成功")
                return True
            self.logger.error("OpenAI 兼容 API 返回空 choices")
            return False
        except Exception as e:
            self.logger.error(f"AI API 连接测试失败: {e}")
            self._alert_key_failure_if_matches(e)
            return False

    async def _ollama_native_chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> tuple:
        """Call Ollama /api/chat directly (bypasses /v1/ think-flag bug). Returns (content, prompt_tokens, completion_tokens)."""
        import httpx
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }
        if self._oa_num_ctx > 0:
            payload["options"]["num_ctx"] = self._oa_num_ctx
        async with httpx.AsyncClient(timeout=float(self.timeout)) as _hc:
            resp = await _hc.post(f"{self._ollama_native_base}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        msg = data.get("message") or {}
        content = (msg.get("content") or "").strip()
        pt = int(data.get("prompt_eval_count") or 0)
        ct = int(data.get("eval_count") or 0)
        return content, pt, ct

    async def _test_connection(self) -> bool:
        """测试API连接"""
        try:
            if not self.client:
                return False

            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents="Say hi in one word.",
                config=types.GenerateContentConfig(
                    max_output_tokens=50,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )

            if response and response.candidates:
                text = None
                try:
                    text = response.text
                except (ValueError, IndexError):
                    pass
                if text:
                    self.logger.info("AI API 连接测试成功")
                    return True
                self.logger.warning(
                    "AI API 测试返回 candidates 但无文本, finish_reason=%s",
                    response.candidates[0].finish_reason if response.candidates else "N/A"
                )
                return True
            else:
                self.logger.error("AI API 返回空响应 (无 candidates)")
                return False
        except Exception as e:
            self.logger.error(f"AI API 连接测试失败: {e}")
            self._alert_key_failure_if_matches(e)
            return False

    async def _generate_reply_openai_compat(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        strategy_overrides: Optional[Dict[str, Any]] = None,
        *,
        route: Optional[str] = None,
        _skip_quality_check: bool = False,
    ) -> Optional[str]:
        """OpenAI 兼容（Ollama）对话生成，与 generate_reply 行为对齐（熔断、重试、兜底）。"""
        _fb_lang = (context or {}).get("reply_lang", "zh")
        # 多模型路由（ai.task_routes）：命中即用该档端点/模型跑「主尝试」；未命中=默认主链。
        # 主尝试失败仍走下方备用池 → 本地兜底 → canned（绝不因路由丢话）。
        _route = route or (context or {}).get("_route")
        _profile = self.resolve_route(_route)
        # 会话级严格路由（conv_route「无限制」，2026-09-12）：只许打该档端点——主尝试失败
        # **不**回落云端备用池 / 不走云端主链（去审查内容送云端会被拒，且违背坐席显式选择）；
        # 档不存在（LAN 端点没配）同样按离线处理，返回 None＝本轮不回复，由调用方按
        # ``_route_offline`` 挂起 / 提示，绝不静默换模型。
        _route_strict = bool((context or {}).get("_route_strict"))
        if _route_strict and not _profile:
            if context is not None:
                context["_route_offline"] = "no_endpoint"
            try:
                from src.ai.conv_route import note_offline as _cr_note
                # R87 #330：带原因，conv_route 据此开离线窗 + 会话 note（状态带可见）
                _cr_note(str((context or {}).get("conversation_id") or ""), "no_endpoint")
            except Exception:
                pass
            return self._fallback_reply(_fb_lang)
        _primary_client = _profile["client"] if _profile else self._oa_client
        _primary_extra = (_profile.get("extra_body") or None) if _profile else self._oa_extra_body
        # 会话级思考开关（Cursor 式 Thinking toggle）：只对显式路由档生效——主链的思维链
        # 策略由 ai.reasoning 全局管，这里不越权改。
        if _profile and (context or {}).get("_thinking") is not None:
            try:
                from src.ai.vendor_params import thinking_extra_body as _teb
                _tb = _teb(_profile.get("base_url") or "", _profile.get("model") or "",
                           enabled=bool((context or {}).get("_thinking")))
                _merged = {k: v for k, v in dict(_primary_extra or {}).items()
                           if k not in ("chat_template_kwargs", "thinking", "enable_thinking")}
                _merged.update(_tb)
                _primary_extra = _merged or None
            except Exception:
                pass
        _primary_native = None if _profile else self._ollama_native_base
        # 本地优先模式（P1）：本地端点齐备即可出话——全本地部署常**根本没配云端 key**，
        # 此时 _oa_client 为 None，若照旧早退就会在试本地之前先放弃回复。
        _local_primary = bool(
            self._primary_mode in ("local", "local_only")
            and self._fb_client and self._fb_model
            and not _route_strict
        )
        if not _primary_client and not _local_primary:
            self.logger.error("AI 客户端未初始化")
            self._note_ai_fail("no_key", context, latency_ms=0, attempt=0)
            return self._fallback_reply(_fb_lang)
        if context is not None:
            context["_current_user_message_for_lang"] = user_message
        _cb_blocked = False
        # 严格路由：熔断器守的是云端主链，与显式 LAN 档无关——不让云端开路挡住本地直答
        if self._cb_enabled and self._cb_open_until > 0 and not _route_strict:
            now = time.time()
            if now < self._cb_open_until:
                # 开路：跳过主模型。有本地兜底则由下方兜底出真话，否则本轮不回复。
                _cb_blocked = True
                self.logger.warning("AI 熔断开路中，跳过 API 调用 request_id=%s", (context or {}).get("request_id") or "n/a")
            elif not self._cb_half_open:
                self._cb_half_open = True
                self.logger.info("AI 熔断进入半开状态，允许一次探测请求")
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().set_circuit_breaker_state("half-open", self._cb_open_until)
                except Exception:
                    pass
        if _cb_blocked and not (self._pool_entries or (self._fb_client and self._fb_model)):
            return self._fallback_reply(_fb_lang)

        # 2026-08-19 Token enforce（P6）：钱包耗尽（曾注资且余额≤0）+ enforce 开 →
        # 跳过云端主链/备用池，直接本地兜底出话（免费路径）。只有本地兜底可用才降级
        # ——无处可去时照走云端（永不断线 > 计费，本次照常计费）。熔断开路时让位
        # 熔断语义（那条链本就以本地收尾）。
        _token_degraded = False
        if not _cb_blocked and not _local_primary and not _route_strict:
            try:
                from src.licensing.token_ledger import should_degrade_action

                _token_degraded = bool(
                    (self._fb_client and self._fb_model)
                    and should_degrade_action("ai_reply"))
            except Exception:
                _token_degraded = False

        so = strategy_overrides or {}
        use_temperature = float(so["temperature"]) if "temperature" in so else self.temperature
        use_max_tokens = int(so["max_tokens"]) if "max_tokens" in so else self.max_tokens
        if use_max_tokens < 256:
            use_max_tokens = 256
        use_context_rounds = int(so["context_rounds"]) if "context_rounds" in so else None
        use_model = str(so["model"]) if so.get("model") else self.model
        if _profile:
            use_model = str(_profile["model"])  # 路由命中：主尝试用该档模型
        # P3 本地档分层：``ai.tiers.<tier>.local_model``（经 _apply_tier_overrides 随
        # setdefault 流入 so）或调用方显式 strategy_overrides.local_model —— 本地链
        # （local 主链 + 云挂兜底，同一端点不同模型即可分层，如 .173 同机 14b/30b）
        # 按会话分级换模型。空＝旧行为（ai.fallback.model）。
        use_local_model = str(so["local_model"]) if so.get("local_model") else ""

        max_hist = use_context_rounds if use_context_rounds is not None else max(1, int(self.max_conversation_history or 10))
        # 上下文深度档（ai.context_depth 深度/最大/超大）只抬地板：策略 context_rounds=0 仍尊重
        try:
            from src.ai.context_depth import history_limit as _cd_hist
            max_hist = _cd_hist(self.config, max_hist, strategy_rounds=use_context_rounds)
        except Exception:
            pass
        system_instruction = self._build_system_instruction(context)
        # ★ companion debug：可热开 ai.debug.dump_system_prompt 把完整 system prompt 打到 INFO 日志
        # 用法：在 config 里设 ai.debug.dump_system_prompt: true，看 logs/app.log 验证 4 层记忆是否注入
        try:
            _ai_cfg = (self.config.config.get("ai") or {}) if (self.config and getattr(self.config, "config", None)) else {}
            _dbg = (_ai_cfg.get("debug") or {})
            if _dbg.get("dump_system_prompt", False) and system_instruction:
                _rid = (context or {}).get("request_id", "n/a")
                _layers = []
                if "【对话伙伴画像" in system_instruction: _layers.append("portrait")
                if "【陪伴关系" in system_instruction or "relationship" in system_instruction.lower(): _layers.append("intimacy")
                if "【用户长期记忆要点" in system_instruction: _layers.append("episodic")
                if "【Messenger 人设补充" in system_instruction or "【LINE 补充说明" in system_instruction: _layers.append("style_hint")
                self.logger.info(
                    "[prompt-dump rid=%s] layers=%s len=%d\n--- BEGIN system_prompt ---\n%s\n--- END ---",
                    _rid, ",".join(_layers) or "none", len(system_instruction), system_instruction,
                )
        except Exception:
            pass
        messages: List[Dict[str, str]] = []
        if system_instruction:
            _sys_content = system_instruction
            if self._oa_extra_body.get("options", {}).get("think") is False:
                if "/nothink" not in _sys_content:
                    _sys_content = _sys_content.rstrip() + "\n/nothink"
            messages.append({"role": "system", "content": _sys_content})
        elif self._oa_extra_body.get("options", {}).get("think") is False:
            messages.append({"role": "system", "content": "/nothink"})
        if conversation_history:
            _lim = max(0, int(max_hist))
            hist = [] if _lim == 0 else conversation_history[-_lim:]
            for msg in hist:
                role = (msg.get("role") or "user").lower()
                content = (msg.get("content") or "").strip()
                if not content or role == "system":
                    continue
                if role == "assistant":
                    messages.append({"role": "assistant", "content": content})
                else:
                    messages.append({"role": "user", "content": content})
        # 真人感文本层 L2：轮变尾注只进本轮送出的消息，不进历史（默认关；中文消息才注入）。
        # 裸 system 的工具调用（抽取 / 分类 / 翻译纠错）不是对话回复，口语化尾注只会污染
        # JSON / 译文（B1，2026-09-11）。
        _um_send = user_message
        if not (context or {}).get("_bare_system"):
            try:
                from src.ai.spoken_style_bridge import turn_tail as _ss_turn_tail
                _um_send = user_message + _ss_turn_tail(self.config, user_message)
            except Exception:
                _um_send = user_message
        messages.append({"role": "user", "content": _um_send})
        # Q-14 A：prompt 预算（历史 → few-shot → 注入长度），主链 / 备用池 / 本地同一份
        messages = self._apply_prompt_budget(messages, context)

        request_id = (context or {}).get("request_id", "")
        _skip_cloud = self._lane_should_skip("cloud")
        _skip_local = self._lane_should_skip("local")
        # P1 本地优先：本地模型即主链，在**所有云端逻辑之前**短路（云端熔断态与它无关）。
        # 已登记掉线/欠费的档跳过探测，立刻让还能用的那一档顶上（三路互相顶）。
        if _local_primary:
            local_reply = None
            if not _skip_local:
                local_reply = await self._try_local_fallback_chat(
                    messages, use_temperature, use_max_tokens, context, request_id,
                    skip_quality_check=_skip_quality_check, as_primary=True,
                    model_override=use_local_model,
                )
            elif self._primary_mode != "local_only":
                self.logger.warning(
                    "本地主链冷却中 → 跳过本轮探测，直走云端/备用池 request_id=%s",
                    request_id or "n/a")
            if local_reply:
                self._lane_note_ok("local")
                return local_reply
            if not _skip_local:
                self._lane_note_fail("local", "connect")
            if self._primary_mode == "local_only":
                # 严格隐私：绝不把用户内容发往云端 —— 宁可不回复，也不泄数据/乱回复。
                self.logger.warning(
                    "本地主模型失败且 local_only（不回落云端）→ 本轮不回复 request_id=%s",
                    request_id or "n/a")
                return self._fallback_reply(_fb_lang)
            if not self._oa_client and not self._pool_entries:
                self.logger.warning(
                    "本地主模型失败且未配置云端主链 → 本轮不回复 request_id=%s",
                    request_id or "n/a")
                return self._fallback_reply(_fb_lang)
            if _skip_cloud and self._pool_entries:
                self.logger.warning(
                    "本地主模型失败且云端主链欠费/失效冷却中 → 先试备用池 request_id=%s",
                    request_id or "n/a")
                pool_reply = await self._try_key_pool_chat(
                    messages, use_temperature, use_max_tokens, context, request_id,
                    skip_quality_check=_skip_quality_check,
                )
                if pool_reply:
                    self._lane_note_ok("pool")
                    return pool_reply
                self._lane_note_fail("pool", "other")
            self.logger.warning(
                "本地主模型失败 → 回落云端主链 request_id=%s", request_id or "n/a")
        if _cb_blocked:
            # 熔断开路：主模型免打扰（保住冷却窗口语义）。降级链＝备用云 Key（质量同级）
            # → 本地兜底，逐级尝试；全灭＝本轮不回复。
            pool_reply = await self._try_key_pool_chat(
                messages, use_temperature, use_max_tokens, context, request_id,
                skip_quality_check=_skip_quality_check,
            )
            if pool_reply:
                return pool_reply
            fb_reply = await self._try_local_fallback_chat(
                messages, use_temperature, use_max_tokens, context, request_id,
                skip_quality_check=_skip_quality_check,
                model_override=use_local_model,
            )
            return fb_reply if fb_reply else self._fallback_reply(_fb_lang)
        if _token_degraded:
            # Token enforce 降级：本地兜底出真话（免费路径，_reply_free_path 标记
            # 由 _try_local_fallback_chat 成功时打上 → 顶层计费钩跳过记账）。
            # 本地失败 → fall through 照走云端主链（永不断线 > 计费）。
            fb_reply = await self._try_local_fallback_chat(
                messages, use_temperature, use_max_tokens, context, request_id,
                skip_quality_check=_skip_quality_check,
                model_override=use_local_model,
            )
            if fb_reply:
                self.logger.info(
                    "Token 钱包耗尽（enforce）→ 本地模型已出话 request_id=%s",
                    request_id or "n/a")
                return fb_reply
            self.logger.warning(
                "Token enforce 降级失败（本地无话）→ 照走云端主链（永不断线优先）"
                " request_id=%s", request_id or "n/a")
        if (not _local_primary and not _cb_blocked and not _token_degraded
                and _skip_cloud and (self._pool_entries or (self._fb_client and self._fb_model))):
            self.logger.warning(
                "云端主链欠费/失效冷却中 → 先试备用池/本地 request_id=%s",
                request_id or "n/a")
            pool_reply = await self._try_key_pool_chat(
                messages, use_temperature, use_max_tokens, context, request_id,
                skip_quality_check=_skip_quality_check,
            )
            if pool_reply:
                self._lane_note_ok("pool")
                return pool_reply
            fb_reply = await self._try_local_fallback_chat(
                messages, use_temperature, use_max_tokens, context, request_id,
                skip_quality_check=_skip_quality_check,
                model_override=use_local_model,
            )
            if fb_reply:
                self._lane_note_ok("local")
                return fb_reply
        last_error = None
        start_time = time.time()
        _attempts_made = 0
        _fail_reason = "empty"
        for attempt in range(2):
            # Q-14 A（D-Q10）：读超时不重试——上游可能仍在生成，再打一枪 = 双倍成本 +
            # 双倍延迟（网关预算 55s 内两枪永远等不完）。连接类 / 5xx 照旧再试 1 次。
            if last_error is not None and _fail_reason == "timeout":
                break
            _attempts_made = attempt + 1
            try:
                pt: int = 0
                ct: int = 0
                # 空响应诊断三元组（2026-08-17 事故观测收口：以前只有一句
                # 「AI 返回空响应」，finish_reason/推理 token 明明在响应里却没记，
                # 定位「思维链耗光预算」这种结构性问题要靠人肉复现）。
                _fin: Any = None
                _rtoks: Any = None
                _rc_len: int = 0
                if _primary_native:
                    reply, pt, ct = await self._ollama_native_chat(
                        messages=messages,
                        max_tokens=use_max_tokens,
                        temperature=use_temperature,
                    )
                    elapsed_time = time.time() - start_time
                else:
                    _create_kw: Dict[str, Any] = dict(
                        model=use_model,
                        messages=messages,
                        temperature=use_temperature,
                        max_tokens=use_max_tokens,
                    )
                    if _primary_extra:
                        _create_kw["extra_body"] = _primary_extra
                    # B5（2026-09-11）：用途随请求头送到官网网关（X-ChatX-Purpose），网关
                    # 流水按用途分桶——此前网关只看得到字符数，「钱花在哪」只能靠体量猜。
                    # 云端厂商忽略未知请求头，LAN 端点同理，零副作用。
                    _hdr_purpose = self._purpose_header(context)
                    if _hdr_purpose:
                        _create_kw["extra_headers"] = {"X-ChatX-Purpose": _hdr_purpose}
                    response = await _primary_client.chat.completions.create(**_create_kw)
                    elapsed_time = time.time() - start_time
                    reply = None
                    if response and response.choices:
                        _choice0 = response.choices[0]
                        _msg = _choice0.message
                        reply = (_msg.content or "").strip()
                        if not reply:
                            _extra = getattr(_msg, "model_extra", None) or {}
                            reply = (_extra.get("reasoning") or "").strip()
                            # DeepSeek 系思维链落在 reasoning_content（与上面的
                            # reasoning 是两个字段）。**刻意不拿它当回复**——那是
                            # 内心独白，漏给客户比不回复更糟；只记长度供诊断。
                            _rc_len = len(str(_extra.get("reasoning_content") or ""))
                        # 非 stop 收尾（length=截断 / content_filter 等）此前完全静默——
                        # 2026-07-26 出过一条 5 字符残句发到线上，事后无从判断是模型
                        # 自己停的还是被截断。只记日志不改行为。
                        _fin = getattr(_choice0, "finish_reason", None)
                        if reply and _fin and str(_fin) != "stop":
                            self.logger.warning(
                                "AI 回复 finish_reason=%s（非 stop，可能被截断）"
                                "len=%d request_id=%s",
                                _fin, len(reply), request_id or "n/a")
                    try:
                        u = response.usage
                        if u:
                            pt = getattr(u, "prompt_tokens", 0) or 0
                            ct = getattr(u, "completion_tokens", 0) or 0
                            _rtoks = getattr(
                                getattr(u, "completion_tokens_details", None),
                                "reasoning_tokens", None)
                    except Exception:
                        pass
                    # prompt-inspect 留痕 + 缓存命中统计（模型实际收到的 messages + usage）
                    self._trace_prompt(
                        messages, model=use_model, client=_primary_client, context=context,
                        usage=getattr(response, "usage", None),
                        latency_ms=int(elapsed_time * 1000), ok=bool(reply))
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_api_call(elapsed_time * 1000)
                except Exception:
                    pass
                reply = reply or None
                if reply:
                    self._clear_ai_fail(context)
                    self.total_calls += 1
                    self.total_tokens += pt + ct
                    self.last_call_time = time.time()
                    self._last_primary_ok_ts = time.time()
                    # ★ P6-4：按 (model, tier, account, purpose) 累积 tokens + cost
                    try:
                        from src.ai.llm_cost import get_llm_cost, purpose_for_reply
                        _ctx = context or {}
                        get_llm_cost().record(
                            model=str(use_model),
                            prompt_tokens=pt,
                            completion_tokens=ct,
                            tier=str(_ctx.get("ai_tier") or "default"),
                            account_id=str(_ctx.get("account_id") or "default"),
                            latency_ms=int(elapsed_time * 1000),
                            purpose=purpose_for_reply(_ctx),
                            provider=("lan" if _primary_native
                                      else self._provider_of(_primary_client)),
                        )
                    except Exception:
                        self.logger.debug("llm_cost.record 失败", exc_info=True)
                    if self._cb_enabled:
                        self._cb_window.append(True)
                        if self._cb_half_open:
                            self._cb_half_open = False
                            self._cb_open_until = 0.0
                            self._cb_window.clear()
                            self.logger.info("AI 半开探测成功，熔断器关闭")
                            try:
                                from src.monitoring.metrics_store import get_metrics_store
                                get_metrics_store().set_circuit_breaker_state("closed")
                            except Exception:
                                pass
                            self._alert_circuit_recovered()
                    reply = await self._guard_reply_language(reply, context)
                    reply = self._shape_single_paragraph(reply, context)
                    # ★ QualityTracker / reply_length 必须用 guard 之后的最终文本，
                    #   否则 LLM 偶发的 "yes..." 等 raw 前缀会被反复误判 too_short。
                    # ★ _skip_quality_check：yes/no 短答型 prompt（chat() 入口）
                    #   3 字符回复是设计行为，跳过 too_short 误报。
                    if not _skip_quality_check:
                        self._quality_tracker.record_call(
                            prompt_tokens=pt, completion_tokens=ct,
                            elapsed_ms=int(elapsed_time * 1000),
                            reply=reply, request_id=request_id,
                        )
                        try:
                            from src.monitoring.metrics_store import get_metrics_store
                            _ms = get_metrics_store()
                            _ms.record_reply_length(len(reply))
                            _ms.record_ai_success()
                        except Exception:
                            pass
                    self._lane_note_ok("cloud")
                    return reply
                # finish=length 且推理 token 占满 = 思维链耗光 max_tokens 预算
                # （非网络/密钥问题）——带上三元组，下次这类问题看一行日志即定位。
                self.logger.warning(
                    "AI 返回空响应 (finish=%s completion_tokens=%s reasoning_tokens=%s "
                    "reasoning_content_len=%s attempt=%s request_id=%s)",
                    _fin, ct, _rtoks, _rc_len, attempt + 1, request_id or "n/a")
                if self._cb_enabled:
                    self._cb_window.append(False)
                    self._maybe_trip_circuit()
            except Exception as e:
                last_error = e
                _fail_reason = self._classify_ai_error(e)   # Q-14：timeout|connect|gateway_5xx|…
                self.logger.warning("AI 调用失败(attempt=%s): %s", attempt + 1, e)
                # 读超时＝请求已送达、服务端很可能照常计费，本地却一个 token 都没记
                # （0907 实测两次超时重试 → 账单多两笔看不见的钱）。按 prompt 字数估算记一笔
                # ``suspected``，对账时单列；连接错误不记（请求根本没出去）。
                if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                    self._record_suspected_usage(
                        model=str(use_model), messages=messages, context=context,
                        provider=("lan" if _primary_native
                                  else self._provider_of(_primary_client)))
                if attempt == 0:
                    await asyncio.sleep(1.5)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            _ms = get_metrics_store()
            _ms.record_error()
            _ms.record_ai_error()
        except Exception:
            pass
        if self._cb_enabled:
            self._cb_window.append(False)
            self._maybe_trip_circuit()
        self._note_ai_fail(
            _fail_reason if last_error is not None else "empty", context,
            latency_ms=int((time.time() - start_time) * 1000), attempt=_attempts_made,
            model=str(use_model), err=last_error)
        # 生产主路径（openai_compat 运行时）的 key 失效弹窗：余额耗尽/Key 被封多发生在
        # 运行中，若只在启动连接测试挂钩会一直静默到下次重启。备用池/本地兜底即便顶上，
        # 机主也必须立刻知道主 Key 已坏（弹窗与降级并行，互不阻塞）。
        if _route_strict:
            # 会话级严格路由：该端点没出话就到此为止——不进云端备用池、不换模型。
            _ro_reason = _fail_reason if last_error is not None else "empty"
            if context is not None:
                context["_route_offline"] = _ro_reason
            try:
                from src.ai.conv_route import note_offline as _cr_note
                # R87 #330：带原因，conv_route 据此开离线窗 + 会话 note（状态带可见）
                _cr_note(str((context or {}).get("conversation_id") or ""), str(_ro_reason or ""))
            except Exception:
                pass
            return self._fallback_reply(_fb_lang)
        self._lane_note_fail("cloud", _fail_reason if last_error is not None else "empty",
                             last_error)
        self._alert_key_failure_if_matches(last_error)
        pool_reply = await self._try_key_pool_chat(
            messages, use_temperature, use_max_tokens, context, request_id,
            skip_quality_check=_skip_quality_check,
        )
        if pool_reply:
            return pool_reply
        fb_reply = await self._try_local_fallback_chat(
            messages, use_temperature, use_max_tokens, context, request_id,
            skip_quality_check=_skip_quality_check,
            model_override=use_local_model,
        )
        if fb_reply:
            return fb_reply
        return self._fallback_reply(_fb_lang)

    def _lane_should_skip(self, lane: str) -> bool:
        try:
            from src.ai.compute_lanes import should_skip
            return bool(should_skip(lane))
        except Exception:
            return False

    def _lane_note_ok(self, lane: str) -> None:
        try:
            from src.ai.compute_lanes import note_ok
            note_ok(lane)
        except Exception:
            pass

    def _lane_note_fail(self, lane: str, kind: str, err: Any = None) -> None:
        try:
            from src.ai.compute_lanes import note_fail
            detail = str(err)[:200] if err is not None else ""
            note_fail(lane, str(kind or "other"), detail)
        except Exception:
            pass

    def _alert_label(self) -> str:
        """告警里的可读身份：``model @ host``（如 deepseek-chat @ api.deepseek.com），
        比裸 provider 名（openai_compatible）能直接看出坏的是哪个云端。"""
        try:
            host = ""
            base = getattr(getattr(self, "_oa_client", None), "base_url", None)
            if base:
                s = str(base)
                if "://" in s:
                    host = s.split("://", 1)[1].split("/", 1)[0]
            if not host:
                host = str(getattr(self, "_provider", "") or "AI")
            return f"{self.model} @ {host}" if self.model else host
        except Exception:
            return str(getattr(self, "_provider", "") or "AI")

    def _alert_key_failure_if_matches(self, err: Any) -> None:
        """err 像 key 失效则弹主机告警；占位/未配置 key（桌面首启等预期态）不弹。绝不抛。

        托管试用态（ai._hosted_trial）额外触发**设备令牌强制换新**：令牌过期/被吊销时
        不等守护线程的小时级刷新——后台线程重领 + 热替换运行中 client 的 api_key，
        下一条消息即恢复（本条消息仍走池/本地兜底，不阻塞）。
        """
        try:
            if err is None or getattr(self, "_key_is_placeholder", False):
                return
            from src.utils.host_alert import looks_like_key_failure, notify_key_failure
            if looks_like_key_failure(err):
                self._maybe_refresh_hosted_token()
                notify_key_failure(self._alert_label(), str(err)[:200])
        except Exception:
            # 这是观测链的**最后一环**：告警调用自己挂掉若也吞掉，key 失效就彻底
            # 无人知晓（云端欠费/被封会表现为「AI 只是变笨了」）。仍不外抛（告警
            # 故障不该连累主链），但必须留痕。
            self.logger.warning("key 失效告警发送失败（告警链断开，请查 host_alert）",
                                exc_info=True)

    def _maybe_refresh_hosted_token(self) -> None:
        """托管令牌 401 自愈（非托管态零开销直返）。绝不抛、绝不阻塞事件循环。"""
        try:
            cfg = getattr(self.config, "config", None) or {}
            if not ((cfg.get("ai") or {}).get("_hosted_trial")):
                return
            from src.ai.hosted_gateway import schedule_forced_refresh

            def _swap(tok: str) -> None:
                cli = getattr(self, "_oa_client", None)
                if cli is not None:
                    cli.api_key = tok  # AsyncOpenAI 每请求读取 api_key，热替换即生效
            schedule_forced_refresh(self.config, on_token=_swap)
        except Exception:
            pass

    _POOL_COOLDOWN_SEC = 120.0   # 池内单 key 失败后的冷却窗（窗内跳过，防每条消息都撞死 key）

    async def _try_key_pool_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        context: Optional[Dict[str, Any]],
        request_id: str,
        *,
        skip_quality_check: bool = False,
    ) -> Optional[str]:
        """主 Key 不可用时按序尝试备用云 Key（每个一次机会，失败进 120s 冷却）。

        - 复用主链已构建好的 messages（人设/记忆/上下文全保留），换 key/端点/模型；
        - 成功即出话（语言守卫照常）+ 「备用 Key 已顶班」提醒（6h 去抖）；
        - 池内 key 自身像 key 失效（401/402/quota）也告警——备用 key 悄悄过期
          是最阴的坑，必须在它被用到的那一刻暴露；
        - 全池失败返回 None，由调用方继续落本地兜底 → canned。绝不抛异常。
        """
        if not self._pool_entries:
            return None
        now = time.time()
        # 智能排序：探活/运行态证据多的钥匙先试（fail-open——快照取不到就按配置序）
        try:
            from src.utils.cloud_credentials import ping_state_snapshot
            ordered = order_pool_entries(self._pool_entries, ping_state_snapshot(), now=now)
        except Exception:
            ordered = list(self._pool_entries)
        for entry in ordered:
            if float(entry.get("bad_until") or 0.0) > now:
                continue
            self._pool_calls += 1
            t0 = time.time()
            try:
                _pool_kw: Dict[str, Any] = dict(
                    model=entry["model"],
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if entry.get("extra_body"):
                    _pool_kw["extra_body"] = entry["extra_body"]
                _hdr_purpose = self._purpose_header(context)
                if _hdr_purpose:
                    _pool_kw["extra_headers"] = {"X-ChatX-Purpose": _hdr_purpose}
                resp = await entry["client"].chat.completions.create(**_pool_kw)
                reply = ""
                if resp and getattr(resp, "choices", None):
                    _msg = resp.choices[0].message
                    reply = (_msg.content or "").strip()
                    if not reply:
                        _extra = getattr(_msg, "model_extra", None) or {}
                        reply = (_extra.get("reasoning") or "").strip()
                pt = ct = 0
                try:
                    u = resp.usage
                    if u:
                        pt = getattr(u, "prompt_tokens", 0) or 0
                        ct = getattr(u, "completion_tokens", 0) or 0
                except Exception:
                    pass
                if not reply:
                    entry["bad_until"] = now + self._POOL_COOLDOWN_SEC
                    self.logger.warning("备用 Key %s 返回空，冷却 %.0fs request_id=%s",
                                        entry["name"], self._POOL_COOLDOWN_SEC, request_id or "n/a")
                    continue
                elapsed = time.time() - t0
                self._pool_ok += 1
                self._pool_last_key = entry["name"]
                entry["last_ok_ts"] = time.time()
                self._last_pool_ok_ts = entry["last_ok_ts"]
                self.total_calls += 1
                self.total_tokens += pt + ct
                self.last_call_time = time.time()
                try:
                    from src.ai.llm_cost import get_llm_cost, purpose_for_reply
                    get_llm_cost().record(
                        model=str(entry["model"]),
                        prompt_tokens=pt, completion_tokens=ct,
                        tier="key_pool",
                        account_id=str((context or {}).get("account_id") or "default"),
                        latency_ms=int(elapsed * 1000),
                        purpose=purpose_for_reply(context),
                        provider=self._provider_of(entry.get("client")),
                    )
                except Exception:
                    pass
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    _ms = get_metrics_store()
                    _ms.record_api_call(elapsed * 1000)
                    _ms.record_ai_success()
                except Exception:
                    pass
                self.logger.warning(
                    "主 Key 不可用 → 备用 Key 已出话 key=%s model=%s elapsed=%.1fs request_id=%s",
                    entry["name"], entry["model"], elapsed, request_id or "n/a")
                self._lane_note_ok("pool")
                try:
                    from src.utils.host_alert import notify_host
                    notify_host(
                        "备用 Key 已顶班",
                        (f"云端主 Key 不可用，已自动切换备用 Key「{entry['label']}」继续出话，"
                         "用户对话不受影响。请尽快处理主 Key（余额/封禁/网络）。"),
                        key="pool_takeover", cooldown_sec=21600.0,
                    )
                except Exception:
                    pass
                reply = await self._guard_reply_language(reply, context)
                reply = self._shape_single_paragraph(reply, context)
                if not skip_quality_check:
                    try:
                        self._quality_tracker.record_call(
                            prompt_tokens=pt, completion_tokens=ct,
                            elapsed_ms=int(elapsed * 1000),
                            reply=reply, request_id=request_id,
                        )
                    except Exception:
                        pass
                return reply
            except Exception as e:
                entry["bad_until"] = time.time() + self._POOL_COOLDOWN_SEC
                self.logger.warning("备用 Key %s 调用失败（冷却 %.0fs）: %s",
                                    entry["name"], self._POOL_COOLDOWN_SEC, e)
                try:
                    from src.utils.host_alert import looks_like_key_failure, notify_key_failure
                    if looks_like_key_failure(e):
                        notify_key_failure(entry["label"], str(e)[:200])
                except Exception:
                    pass
        self._lane_note_fail("pool", "other")
        return None

    async def _try_local_fallback_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        context: Optional[Dict[str, Any]],
        request_id: str,
        *,
        skip_quality_check: bool = False,
        as_primary: bool = False,
        model_override: str = "",
    ) -> Optional[str]:
        """用 LAN 本地模型出话。既作**云链兜底**，也作 ``ai.primary=local*`` 的**主链**。

        - 复用主链已构建好的 messages（人设/记忆/上下文全保留），只换模型与端点；
        - 语言守卫照常跑（守卫内部纠偏调用若碰主模型故障会自然放行原文）；
        - 失败返回 None，由调用方回落（兜底身份→canned；主链身份→按 local_only 决定）；
        - ``model_override``（P3 分层）：同端点换模型（tier/strategy_overrides 的
          ``local_model``）；空＝``ai.fallback.model``。成本记账记**实际所用模型**，
          分层效果在 llm_cost/看板可分模型对比。

        ``as_primary=True``（本地优先模式）与兜底身份的**观测语义必须分开**，否则正常的
        本地优先部署会被 ``degradation_snapshot`` 永久误报成「降级顶班」、坐席端天天挂
        假红条、告警也会长期误鸣：
          - 成功记进 ``_last_primary_ok_ts``（本地就是主链）而非 ``_last_fb_ok_ts``；
          - 成本记 tier=``local_primary`` 而非 ``local_fallback``（出话分布看板才不失真）；
          - **不**记 ``local_llm_fallback``「顶班」计数（与 ``_local_tool_chat`` 同款克制）；
          - **不**累加 ``_fb_calls`` / ``_fb_ok``（``get_stats().local_fallback_*``
            是看门狗「云端挂了本地顶班」判据，本地主链出话算进去会 30min 误弹）；
          - 日志降为 INFO（正常运行，不是事故）。
        """
        if not (self._fb_client and self._fb_model):
            return None
        # 会话号推导（弹窗/红条要能指到具体会话，「会话 -」没有可操作性）：
        # 优先 context 显式键；A 线 request_id=「chatid_msgid」可取前缀，B 线 r-uuid 不猜。
        _c = context or {}
        _cid = str(_c.get("chat_key") or _c.get("conversation_id") or "")
        _rid = str(request_id or "")
        if not _cid and "_" in _rid and not _rid.startswith("r-"):
            _cid = _rid.rsplit("_", 1)[0]
        if not as_primary and not getattr(self, "_fb_chat_fallback_enabled", True):
            # 无兜底纪律：兜底身份被禁用 → 不出话 + 弹窗 + 主机错误日志。
            # （as_primary=本地就是指定主链，不属兜底，不受此闸约束。）
            try:
                from src.ops.delivery_block import report_block
                report_block(
                    "chat", reason="cloud_failed_no_fallback",
                    platform=str(_c.get("platform") or ""), conversation_id=_cid,
                    detail=f"request_id={_rid or 'n/a'}")
            except Exception:
                self.logger.debug("delivery_block 上报失败", exc_info=True)
            return None
        use_fb_model = str(model_override or "").strip() or self._fb_model
        if not as_primary:
            self._fb_calls += 1
        t0 = time.time()
        try:
            fb_messages = list(messages)
            # 语言钉子：本地小模型比云主模型更易混语（实测 qwen3-30b 中文里冒日文句），
            # 明确目标语指令收敛之；语言守卫仍在下游兜底。
            _rl = str((context or {}).get("reply_lang") or "").strip()
            if _rl:
                _lang_name = self._LANG_NAMES.get(_rl, _rl)
                fb_messages.append({
                    "role": "system",
                    "content": f"Reply strictly in {_lang_name} only. Never mix in any other language.",
                })
            # Qwen3 系模板强制 system 只能打头（2026-08-15 主链换 27B 当晚实锤：
            # 末位语言钉子触发 400 "System message must be at the beginning" →
            # local_only 语义下客户整轮无回复）。把全部 system 合并进首位——
            # 内容一条不丢、相对顺序保留，任何模板都合法；钉子的「末位近因」
            # 优势由 27B 更强的指令跟随 + 下游语言守卫补偿。
            fb_messages = self._coalesce_system_head(fb_messages)
            # 上下文预算裁剪（num_ctx 的客户端保险）。2026-09-18 改口径：**先收输出预留、
            # 再裁历史**——旧算法固定扣 max_tokens(4096)+128，8192 窗口只剩 3968 给 prompt，
            # 人设一个块就把历史裁到 2 条（「脑子有点空」事故根因之一）。现在：prompt 估算
            # 装得下就不动 max_tokens；装不下先把出话预留收到地板(512，均长 40 token 够用)，
            # 仍装不下才丢最旧历史（保 system 人设 + 最近轮次），绝不让整包被 400 拒掉。
            _num_ctx = int(self._fb_num_ctx or 8192)
            try:
                from src.ai.vendor_params import fit_local_budget as _flb
                _est = sum(self._estimate_msg_tokens(m.get("content")) for m in fb_messages)
                _ctx_budget, _send_max_tokens = _flb(_est, _num_ctx, int(max_tokens))
            except Exception:
                _ctx_budget = max(512, _num_ctx - int(max_tokens) - 128)
                _send_max_tokens = int(max_tokens)
            if _send_max_tokens < int(max_tokens):
                self.logger.info(
                    "本地%s出话预留收缩 max_tokens %d→%d 以保历史 num_ctx=%d request_id=%s",
                    "主" if as_primary else "兜底", int(max_tokens), _send_max_tokens,
                    _num_ctx, request_id or "n/a")
            max_tokens = _send_max_tokens
            _before = len(fb_messages)
            fb_messages = self._trim_messages_to_budget(fb_messages, _ctx_budget)
            if len(fb_messages) < _before:
                self.logger.info(
                    "本地兜底裁剪历史 %d→%d 条以适配 num_ctx=%s request_id=%s",
                    _before, len(fb_messages), self._fb_num_ctx, request_id or "n/a")
            pt = ct = 0
            _local_usage: Any = None
            reply = ""
            _kw: Dict[str, Any] = dict(
                model=use_fb_model,
                messages=fb_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if self._fb_extra_body:
                _kw["extra_body"] = self._fb_extra_body
            # keep-alive 毛刺：连接类 / 网关 5xx 当场重试 1 次（读超时不重试）。
            for _attempt in range(2):
                try:
                    if self._fb_native_base:
                        reply, pt, ct = await self._fb_native_chat(
                            fb_messages, max_tokens=max_tokens, temperature=temperature,
                            model=use_fb_model)
                        _local_usage = {
                            "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0)}
                    else:
                        resp = await self._fb_client.chat.completions.create(**_kw)
                        reply = ""
                        if resp and getattr(resp, "choices", None):
                            _msg = resp.choices[0].message
                            reply = (_msg.content or "").strip()
                            if not reply:
                                _extra = getattr(_msg, "model_extra", None) or {}
                                reply = (_extra.get("reasoning") or "").strip()
                        try:
                            u = resp.usage
                            if u:
                                pt = getattr(u, "prompt_tokens", 0) or 0
                                ct = getattr(u, "completion_tokens", 0) or 0
                                _local_usage = u
                        except Exception:
                            pass
                    break
                except Exception as _e:
                    _kind = self._classify_ai_error(_e)
                    if _attempt == 0 and _kind in ("connect", "gateway_5xx"):
                        self.logger.warning(
                            "本地%s连接毛刺，0.2s 后重试 1 次: %s request_id=%s",
                            "主" if as_primary else "兜底", _e, request_id or "n/a")
                        await asyncio.sleep(0.2)
                        continue
                    raise
            # prompt-inspect 留痕 + 前缀缓存命中统计（2026-09-18 N4）：此前只有云主链记，
            # 173 实际收到什么 / vLLM prefix cache 命中多少（usage.prompt_tokens_details.
            # cached_tokens）从来没人量——「人设块分层值不值」没有读数就只能靠猜。
            # 预算字段用本地口径（num_ctx / 裁前裁后 / 实发 max_tokens），不与云链 _budget_stats 混。
            _est_after = sum(self._estimate_msg_tokens(m.get("content")) for m in fb_messages)
            self._trace_prompt(
                fb_messages, model=use_fb_model, client=self._fb_client, context=context,
                usage=_local_usage, latency_ms=int((time.time() - t0) * 1000), ok=bool(reply),
                purpose="local_primary" if as_primary else "local_fallback",
                budget_stats={"budget": int(_ctx_budget), "num_ctx": int(_num_ctx),
                              "hist": max(0, _before - len(fb_messages)), "after": int(_est_after),
                              "max_tokens_sent": int(max_tokens), "lane": "local"})
            if not reply:
                self.logger.warning(
                    "本地%s模型返回空 request_id=%s",
                    "主" if as_primary else "兜底", request_id or "n/a")
                if not as_primary:
                    self._record_local_fallback_metric(False)
                self._lane_note_fail("local", "empty")
                return None
            elapsed = time.time() - t0
            if not as_primary:
                self._fb_ok += 1
            self._lane_note_ok("local")
            if as_primary:
                self._last_primary_ok_ts = time.time()
            else:
                self._last_fb_ok_ts = time.time()
            self.total_calls += 1
            self.total_tokens += pt + ct
            self.last_call_time = time.time()
            try:
                from src.ai.llm_cost import get_llm_cost, purpose_for_reply
                get_llm_cost().record(
                    model=str(use_fb_model),
                    prompt_tokens=pt, completion_tokens=ct,
                    tier="local_primary" if as_primary else "local_fallback",
                    account_id=str((context or {}).get("account_id") or "default"),
                    latency_ms=int(elapsed * 1000),
                    purpose=purpose_for_reply(context),
                    provider=self._provider_of(getattr(self, "_fb_client", None)) or "lan",
                )
            except Exception:
                pass
            if as_primary:
                self.logger.info(
                    "本地主模型已出话 model=%s elapsed=%.1fs request_id=%s",
                    use_fb_model, elapsed, request_id or "n/a")
            else:
                self._record_local_fallback_metric(True, latency_ms=elapsed * 1000)
                self.logger.warning(
                    "主模型不可用 → 本地兜底模型已出话 model=%s elapsed=%.1fs request_id=%s",
                    use_fb_model, elapsed, request_id or "n/a")
            reply = await self._guard_reply_language(reply, context)
            reply = self._shape_single_paragraph(reply, context)
            if not skip_quality_check:
                try:
                    self._quality_tracker.record_call(
                        prompt_tokens=pt, completion_tokens=ct,
                        elapsed_ms=int(elapsed * 1000),
                        reply=reply, request_id=request_id,
                    )
                except Exception:
                    pass
            # 2026-08-19 Token 计费（P6）：本地模型出话＝免费路径（自有 GPU），打标记
            # 让顶层计费钩跳过本条 ai_reply 记账——断云顶班/enforce 降级/本地主链
            # 三种形态都不该扣客户 Token。标记由计费钩 pop 消费（context 在 A 线
            # 跨轮复用，残留会豁免下一条云端回复——必须取走即清）。
            if context is not None:
                context["_reply_free_path"] = True
            return reply
        except Exception as e:
            if not as_primary:
                self._record_local_fallback_metric(False)
            # 4xx/5xx 把响应体一并带出（如「exceeds the available context size」），
            # 否则日志只有裸状态码，事后排障要去翻远端 Ollama server.log。
            _body = ""
            try:
                _resp = getattr(e, "response", None)
                if _resp is not None:
                    _body = str(getattr(_resp, "text", "") or "")[:200]
            except Exception:
                _body = ""
            self.logger.warning(
                "本地兜底模型也失败: %s%s", e, f" | body={_body}" if _body else "")
            self._lane_note_fail("local", self._classify_ai_error(e), e)
            return None

    @staticmethod
    def _estimate_msg_tokens(text: Any) -> int:
        """token 粗估（宁多勿少）：CJK 每字 ≈1 token，其余每 3 字符 ≈1 token，+8 模板开销。"""
        s = str(text or "")
        cjk = sum(1 for ch in s if ord(ch) >= 0x2E80)
        other = len(s) - cjk
        return cjk + (other + 2) // 3 + 8

    # ── Q-14 #262：失败分类 / 重试判定 / 失败可见 ────────────────────────────
    @staticmethod
    def _classify_ai_error(e: BaseException) -> str:
        """异常 → ``timeout|connect|gateway_5xx|auth|other``。

        读超时（请求已送达、上游可能仍在生成）与连接类错误（连不上 / 被重置 /
        网关 502·503·504）必须分开：前者重试 = 双倍成本 + 双倍延迟，后者重试 1 次
        常能救回。openai SDK 把两类超时都包成 APITimeoutError，靠 ``__cause__``
        链上的 httpx 异常区分（ConnectTimeout 归连接类）。"""
        seen = 0
        cur: Optional[BaseException] = e
        names: List[str] = []
        status = None
        while cur is not None and seen < 6:
            names.append(type(cur).__name__.lower())
            if status is None:
                status = getattr(cur, "status_code", None)
                if status is None:
                    _r = getattr(cur, "response", None)
                    status = getattr(_r, "status_code", None) if _r is not None else None
            cur = cur.__cause__ or cur.__context__
            seen += 1
        joined = " ".join(names)
        msg = str(e).lower()
        if any(n in ("connecttimeout", "connecterror", "remoteprotocolerror",
                     "connectionreseterror", "connectionrefusederror",
                     "connectionerror", "apiconnectionerror") for n in names):
            if "apiconnectionerror" in joined and (
                    "readtimeout" in joined or "writetimeout" in joined
                    or "pooltimeout" in joined):
                return "timeout"
            if "apiconnectionerror" in joined and "apitimeouterror" in joined:
                # openai APITimeoutError 是 APIConnectionError 子类：无 httpx 因果
                # 链时按读超时处理（connect 只有 5s，多数是读侧）
                return "timeout"
            return "connect"
        if ("timeout" in joined or "timed out" in msg or "timeouterror" in joined):
            return "timeout"
        try:
            sc = int(status) if status is not None else 0
        except Exception:
            sc = 0
        if sc in (502, 503, 504) or "internalservererror" in joined or sc >= 500:
            return "gateway_5xx"
        if sc == 402 or "insufficient" in msg or "余额不足" in msg or "arrears" in msg:
            return "quota"
        if sc in (401, 403) or "authenticationerror" in joined or "permissiondeniederror" in joined:
            return "auth"
        if "502" in msg or "503" in msg or "bad gateway" in msg or "service unavailable" in msg:
            return "gateway_5xx"
        if "connection" in msg and ("reset" in msg or "refused" in msg or "aborted" in msg):
            return "connect"
        return "other"

    @classmethod
    def _should_retry_ai_error(cls, e: BaseException) -> bool:
        """连接类 / 网关 5xx / 未知 → 再试 1 次；读超时 → 不重试（走 None）。"""
        return cls._classify_ai_error(e) != "timeout"

    @staticmethod
    def _conv_label(context: Optional[Dict[str, Any]]) -> str:
        c = context or {}
        cid = str(c.get("conversation_id") or "").strip()
        if cid:
            return cid
        p = str(c.get("platform") or c.get("channel") or "").strip().lower()
        a = str(c.get("account_id") or "").strip()
        k = str(c.get("chat_key") or c.get("chat_id") or "").strip()
        if p and k:
            return f"{p}:{a or 'default'}:{k}"
        return k or "-"

    def _note_ai_fail(self, reason: str, context: Optional[Dict[str, Any]],
                      *, latency_ms: int, attempt: int, model: str = "",
                      err: Any = None) -> None:
        """主链失败落一行 ``[ai] fail`` 日志 + 记到 ``_last_fail``（起草侧 pop 消费）。
        备用池 / 本地兜底若随后出话，起草侧不会来取，标记在下次成功 / 失败时被覆盖。"""
        conv = self._conv_label(context)
        rid = str((context or {}).get("request_id") or "") or "n/a"
        detail = ""
        if err is not None:
            detail = str(err).replace("\n", " ")[:160]
        self.logger.error(
            "[ai] fail conv=%s reason=%s latency_ms=%d attempt=%d model=%s request_id=%s%s",
            conv, reason, int(latency_ms), int(attempt), model or self.model or "-", rid,
            (" err=" + detail) if detail else "")
        rec = {
            "ts": time.time(), "reason": reason, "latency_ms": int(latency_ms),
            "attempt": int(attempt), "conv": conv, "model": model or self.model or "",
            "request_id": rid,
        }
        try:
            store = self._last_fail if isinstance(self._last_fail, dict) else {}
            if "conv" in store:      # 旧单条结构 → 升级成按会话字典
                store = {}
            store[conv] = rec
            if len(store) > 64:
                for _k in sorted(store, key=lambda k: store[k].get("ts", 0))[:len(store) - 64]:
                    store.pop(_k, None)
            self._last_fail = store
        except Exception:
            self._last_fail = {conv: rec}

    def _clear_ai_fail(self, context: Optional[Dict[str, Any]]) -> None:
        try:
            if isinstance(self._last_fail, dict):
                self._last_fail.pop(self._conv_label(context), None)
        except Exception:
            pass

    def pop_last_fail(self, conv: str = "") -> Optional[Dict[str, Any]]:
        """取走某会话最近一次主链失败记录（一次性）。``conv`` 空 = 取最近一条。"""
        store = self._last_fail if isinstance(self._last_fail, dict) else None
        if not store:
            return None
        key = str(conv or "").strip()
        if not key:
            key = max(store, key=lambda k: store[k].get("ts", 0))
        rec = store.pop(key, None)
        return dict(rec) if rec else None

    _FEWSHOT_HEAD_RE = None

    @classmethod
    def _is_fewshot_part(cls, part: str) -> bool:
        """system 提示里的 few-shot 段：首行含 示例/范例/例句/few-shot/examples。"""
        import re as _re
        if cls._FEWSHOT_HEAD_RE is None:
            cls._FEWSHOT_HEAD_RE = _re.compile(
                r"(示例|范例|例句|对话样例|few[\s_-]?shot|\bexamples?\b)", _re.IGNORECASE)
        head = (part or "").strip().split("\n", 1)[0][:80]
        return bool(head) and bool(cls._FEWSHOT_HEAD_RE.search(head))

    # 裁剪器永不动的 system 段落（段首匹配）：人设主体与几条「一丢就穿帮」的硬规则。
    # 2026-09-11 事故沉淀：旧 ③ 步按「1 token = 3 字符」截 system 尾部，而估算器按
    # CJK 1 字 = 1 token 计——中文超额被放大 3 倍，11k 的系统提示被砍到 ~700 token，
    # 人设/记忆/自称规则/媒体边界整段消失（zhiliao 演练 49/49、客户机 81/81 命中）
    # ＝「现在聊天没有人设、没有记忆」的根因。现改为**按段落、用同一估算器**弹尾，
    # 且以下段落受保护；仍超预算宁可软放行（云端上下文远大于预算），不再砍人设。
    _PROTECTED_SYS_HEADS = (
        "【后台人设定位", "【人称与角色", "【自称规则", "【媒体能力边界",
        "【输出语言", "【硬性要求", "【身份硬锁", "【年龄事实",
        # B3（2026-09-11 用量分析补漏）：非中文会话真正生效的语言块是
        # 【LANGUAGE RULE — TOP PRIORITY】（2.7k 字、在 system 尾部），中文会话是
        # 【多语言回复规则】；【回复硬约束】里第 4 条正是「严禁 ()/[] 描写动作」
        # ——它们都不在上表，超预算时会被当注入尾巴弹掉：英文客户「Why don't you
        # speak English anymore」与括号旁白（#275）就从这里漏出去。
        "【LANGUAGE RULE", "【多语言回复规则", "【回复硬约束】",
        # #333（2026-09-17）：图中人物身份（persona_reply extra_hint 独立成段注入）——
        # 「这是 TA 本人 / 不要猜是谁」被裁掉＝身份层白做。
        "【图中人物身份",
    )
    #: #333：入站媒体识别块头是「【<平台> 媒体消息·<类型>】」（平台名可变，startswith 钉不住）。
    #: #277 GXRD67 实录：识图成功却被 inject_chars 裁到 22 字 → AI 三次自曝「看不到图」。
    _PROTECTED_SYS_HEAD_SUBSTR = ("媒体消息·",)
    # 历史保底：预算再紧也先留最近这几条真实对话（丢完注入尾巴之后才动它们）
    _HIST_FLOOR_MSGS = 6

    @classmethod
    def _protected_sys_parts(cls, parts: List[str]) -> set:
        """返回受保护段落下标：首段 + 保护段首 + 人设区（【后台人设定位】起到
        紧随其后的【人称与角色】为止——人设块内部可能含空行，按区保护）。"""
        prot = {0} if parts else set()
        persona_start = -1
        for j, p in enumerate(parts):
            head = (p or "").lstrip()
            if head.startswith(cls._PROTECTED_SYS_HEADS):
                prot.add(j)
            elif head.startswith("【") and any(
                    s in head[:40] for s in cls._PROTECTED_SYS_HEAD_SUBSTR):
                prot.add(j)
            if head.startswith("【后台人设定位"):
                persona_start = j
            elif persona_start >= 0 and head.startswith("【人称与角色"):
                prot.update(range(persona_start, j + 1))
                persona_start = -1
        if persona_start >= 0:
            # 人设区没有【人称与角色】收尾（老版/自定义）：保护到下一个「【」段首为止
            for j in range(persona_start + 1, len(parts)):
                if (parts[j] or "").lstrip().startswith("【"):
                    break
                prot.add(j)
        return prot

    @classmethod
    def _pop_sys_parts_to_budget(
        cls, sys_txt: str, other_tokens: int, budget_tokens: int,
        *, fewshot_only: bool = False,
    ) -> Tuple[str, int, int, int]:
        """从 system 尾部按段（空行分隔）弹出未保护段落直到合计 ≤ 预算。

        用 ``_estimate_msg_tokens`` 对整段 system 重估（与预算口径**同一把尺**），
        返回 (新 system, 合计 tokens, 弹掉的段数, 弹掉的字符数)。
        ``fewshot_only=True`` 只弹 few-shot 段（② 步）。"""
        parts = sys_txt.split("\n\n")
        prot = cls._protected_sys_parts(parts)

        def _total() -> int:
            return other_tokens + cls._estimate_msg_tokens("\n\n".join(parts))

        total = _total()
        popped = chars = 0
        j = len(parts) - 1
        while j > 0 and total > budget_tokens:
            if j not in prot and (not fewshot_only or cls._is_fewshot_part(parts[j])):
                whole = parts[j]
                parts.pop(j)
                after_pop = _total()
                # 整段丢会把预算大幅砸穿（KB/记忆列表常是最大段）→ 改为截该段尾部
                # 恰好塞进预算：列表型注入前面的条目相关度更高，留头比全丢强。
                room = budget_tokens - after_pop
                if (not fewshot_only and room >= 96
                        and after_pop < int(budget_tokens * 0.85)):
                    lo, hi = 0, len(whole)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        parts.insert(j, whole[:mid])
                        fits = _total() <= budget_tokens
                        parts.pop(j)
                        if fits:
                            lo = mid
                        else:
                            hi = mid - 1
                    head = whole[:lo].rstrip()
                    if lo >= max(32, len(whole) // 4) and head:
                        parts.insert(j, head)
                        chars += len(whole) - len(head)
                        total = _total()
                        popped += 1
                        break
                chars += len(whole) + 2
                total = after_pop
                popped += 1
            j -= 1
        return "\n\n".join(parts).rstrip(), total, popped, chars

    @classmethod
    def _trim_prompt_to_budget(
        cls, messages: List[Dict[str, Any]], budget_tokens: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """prompt 预算（Q-14 A）：合计估算 token 超 ``budget_tokens`` 时按顺序裁
        ① 历史（最旧先丢，保底最近 ``_HIST_FLOOR_MSGS`` 条）→ ② system 里的 few-shot 段
        → ③ system 尾部未保护的注入段（按段、同一估算器）→ ④ 保底历史
        → ⑤ 仍超＝软放行（云端窗口远大于预算；宁超预算不砍人设）。
        首部 system 的人设主体 / 硬规则段与最后一条用户消息永不丢。
        返回 (messages, stats)；stats = {before, after, hist, fewshot, inject_chars, over}。
        ``budget_tokens<=0`` = 关闭（原样返回）。"""
        stats = {"before": 0, "after": 0, "hist": 0, "fewshot": 0, "inject_chars": 0, "over": 0}
        if not messages:
            return messages, stats
        est = [cls._estimate_msg_tokens(m.get("content")) for m in messages]
        total = sum(est)
        stats["before"] = total
        stats["after"] = total
        if budget_tokens <= 0 or total <= budget_tokens:
            return messages, stats
        out = [dict(m) for m in messages]
        last_idx = len(out) - 1
        hist_idx = [i for i in range(len(out))
                    if i != last_idx and out[i].get("role") != "system"]
        floor = hist_idx[-cls._HIST_FLOOR_MSGS:] if cls._HIST_FLOOR_MSGS > 0 else []
        keep = [True] * len(out)
        # ① 历史：从最旧开始丢，先不碰保底的最近几条
        for i in hist_idx:
            if total <= budget_tokens:
                break
            if i in floor:
                continue
            keep[i] = False
            total -= est[i]
            stats["hist"] += 1
        # ②③ system 段落：先 few-shot，再未保护的注入尾段（人设区/硬规则段不动）
        if total > budget_tokens and out and out[0].get("role") == "system":
            sys_txt = str(out[0].get("content") or "")
            other = total - cls._estimate_msg_tokens(sys_txt)
            sys_txt, total, n_fs, _ = cls._pop_sys_parts_to_budget(
                sys_txt, other, budget_tokens, fewshot_only=True)
            stats["fewshot"] += n_fs
            if total > budget_tokens:
                sys_txt, total, _, chars = cls._pop_sys_parts_to_budget(
                    sys_txt, other, budget_tokens)
                stats["inject_chars"] += chars
            out[0]["content"] = sys_txt
        # ④ 保底历史：注入尾巴都丢光还超 → 才动最近几条（仍是最旧先丢）
        for i in floor:
            if total <= budget_tokens:
                break
            keep[i] = False
            total -= est[i]
            stats["hist"] += 1
        out = [m for i, m in enumerate(out) if keep[i]]
        # ⑤ 软放行：剩下的全是人设/硬规则/最后一条消息——超就超，绝不砍人设
        total = sum(cls._estimate_msg_tokens(m.get("content")) for m in out)
        stats["over"] = max(0, total - budget_tokens)
        stats["after"] = max(0, total)
        return out, stats

    def _apply_prompt_budget(self, messages: List[Dict[str, Any]],
                             context: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """主链发送前套 prompt 预算并落 ``[ai] prompt_tokens=… trimmed=…`` 日志。绝不抛。"""
        try:
            budget = int(getattr(self, "_prompt_budget_tokens", self._DEFAULT_PROMPT_BUDGET) or 0)
            try:
                from src.ai.context_depth import prompt_budget as _cd_budget
                budget = _cd_budget(self.config, budget)   # 深度档只抬预算地板；0=不裁 保持
            except Exception:
                pass
            out, st = self._trim_prompt_to_budget(messages, budget)
            trimmed = st["hist"] or st["fewshot"] or st["inject_chars"]
            rid = str((context or {}).get("request_id") or "") or "n/a"
            st["budget"] = budget
            self._last_budget_stats = dict(st)
            # 挂在本轮 context 上：generate_reply 会重入（抽取/短判走 chat()），
            # 实例字段会被内层短调用覆盖——inspect 就会把 11k 人设回合记成 400 token。
            if isinstance(context, dict):
                context["_budget_stats"] = dict(st)
            if st.get("over"):
                self._alert_prompt_over_budget(st, context)
            if trimmed or st.get("over"):
                _log = self.logger.warning if st.get("over") else self.logger.info
                _log(
                    "[ai] prompt_tokens=%d budget=%d trimmed=hist:%d,fewshot:%d,inject_chars:%d"
                    " after=%d over=%d conv=%s request_id=%s",
                    st["before"], budget, st["hist"], st["fewshot"], st["inject_chars"],
                    st["after"], st.get("over", 0), self._conv_label(context), rid)
            else:
                self.logger.debug("[ai] prompt_tokens=%d budget=%d trimmed=0 request_id=%s",
                                  st["before"], budget, rid)
            return out
        except Exception:
            self.logger.debug("[ai] prompt 预算裁剪失败（放行原 messages）", exc_info=True)
            return messages

    # 软放行（over>0）= 人设/硬规则/最后一条消息本身就超预算，裁剪器什么都没砍成。
    # 这是配置问题（预算设太小 / 人设写太长）不是抖动，进程内 30 分钟一条进运维群即可。
    _OVER_ALERT_DEBOUNCE_SEC = 1800

    def _alert_prompt_over_budget(self, st: Dict[str, Any],
                                  context: Optional[Dict[str, Any]]) -> None:
        try:
            from src.ops.ops_alert import notify
            _ctx = context or {}
            acct = str(_ctx.get("account_id") or "default")
            notify(
                "prompt_over_budget",
                "⚠️ AI 提示词超预算软放行\n"
                f"账号 {acct} · 会话 {self._conv_label(context)}\n"
                f"预算 {st.get('budget', 0)} · 裁后仍 {st.get('after', 0)} token（超 {st.get('over', 0)}）\n"
                "人设/硬规则段不砍，已按原样发送。建议：回复设置里把「上下文与记忆深度」调高一档，"
                "或精简该人设/知识库注入。",
                account_id=acct, source="ai_client", reason="prompt_over_budget",
                debounce_sec=self._OVER_ALERT_DEBOUNCE_SEC,
            )
        except Exception:
            self.logger.debug("prompt_over_budget 告警跳过", exc_info=True)

    _CACHE_STAT_LOG_EVERY = 25

    def _trace_prompt(self, messages: List[Dict[str, Any]], *, model: str, client: Any,
                      context: Optional[Dict[str, Any]], usage: Any,
                      latency_ms: int, ok: bool = True, purpose: str = "",
                      budget_stats: Optional[Dict[str, Any]] = None) -> None:
        """一次模型调用的留痕（prompt-inspect）+ 缓存命中滚动统计。绝不抛。

        云主链与本地链（2026-09-18 起）共用：``budget_stats`` 显式给时用它（本地 num_ctx 口径），
        否则取云链裁剪留下的 ``_budget_stats``；``purpose`` 标 lane（local_primary / local_fallback）。
        """
        try:
            from src.ai import prompt_trace
            host = ""
            try:
                host = str(getattr(client, "base_url", "") or "").split("://", 1)[-1].split("/", 1)[0]
            except Exception:
                host = ""
            _ctx = context or {}
            uf = prompt_trace.usage_fields(usage)
            st: Dict[str, Any] = {}
            if isinstance(budget_stats, dict):
                st = dict(budget_stats)
            elif isinstance(_ctx.get("_budget_stats"), dict):
                st = dict(_ctx["_budget_stats"])
            elif getattr(self, "_last_budget_stats", None):
                st = dict(self._last_budget_stats)
            if uf["prompt_tokens"]:
                st["billed_prompt"] = uf["prompt_tokens"]
            prompt_trace.record(
                messages=messages, model=str(model), host=host,
                conv=self._conv_label(context),
                request_id=str(_ctx.get("request_id") or ""),
                usage=usage, budget_stats=st or None,
                latency_ms=int(latency_ms or 0), ok=ok, purpose=str(purpose or ""),
                route=(_ctx.get("_conv_route") if isinstance(_ctx.get("_conv_route"), dict)
                       else None),
            )
            if uf["prompt_tokens"]:
                self.logger.debug(
                    "[ai] usage model=%s prompt=%d cache_hit=%d completion=%d reasoning=%d ms=%d",
                    model, uf["prompt_tokens"], uf["cache_hit_tokens"], uf["completion_tokens"],
                    uf["reasoning_tokens"], int(latency_ms or 0))
            cs = prompt_trace.cache_stats()
            if cs["calls"] and cs["calls"] % self._CACHE_STAT_LOG_EVERY == 0:
                self.logger.info(
                    "[ai] prompt-cache 滚动统计 calls=%d prompt=%d hit=%d (%.0f%%) completion=%d reasoning=%d",
                    cs["calls"], cs["prompt_tokens"], cs["cache_hit_tokens"],
                    cs["hit_ratio"] * 100, cs["completion_tokens"], cs["reasoning_tokens"])
        except Exception:
            self.logger.debug("prompt_trace 留痕跳过", exc_info=True)

    @staticmethod
    def _coalesce_system_head(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """把散落各处的 system 消息合并成**首位单条**（严格模板兼容化）。

        Qwen3 系（vLLM chat template）拒绝非打头的 system；本函数把所有 system
        的文本按原相对顺序用空行拼接进第一条，非 system 消息原序保留。
        0 条 system 或「恰 1 条且已在首位」时原样返回（零拷贝语义不变）。
        """
        if not messages:
            return messages
        sys_idx = [i for i, m in enumerate(messages)
                   if (m or {}).get("role") == "system"]
        if not sys_idx or (len(sys_idx) == 1 and sys_idx[0] == 0):
            return messages
        sys_txts = [str(messages[i].get("content") or "") for i in sys_idx]
        merged = "\n\n".join(t for t in sys_txts if t.strip())
        rest = [m for m in messages if (m or {}).get("role") != "system"]
        return [{"role": "system", "content": merged}] + rest

    @classmethod
    def _trim_messages_to_budget(
        cls, messages: List[Dict[str, Any]], budget_tokens: int
    ) -> List[Dict[str, Any]]:
        """把 messages 裁进 token 预算：从最旧的非 system 历史开始丢，
        保住首部 system（人设/记忆）、末尾语言钉子与最后一轮用户消息；
        仍超预算则截 system 内容尾部——降级出话永远好于整包被 400 拒绝。"""
        if budget_tokens <= 0 or not messages:
            return messages
        est = [cls._estimate_msg_tokens(m.get("content")) for m in messages]
        if sum(est) <= budget_tokens:
            return messages
        keep = [True] * len(messages)
        protected = set()
        if messages[0].get("role") == "system":
            protected.add(0)
        protected.add(len(messages) - 1)
        if len(messages) >= 2 and messages[-1].get("role") == "system":
            protected.add(len(messages) - 2)   # 语言钉子前的最后一轮真实消息
        total = sum(est)
        for i in range(len(messages)):
            if total <= budget_tokens:
                break
            if i in protected:
                continue
            keep[i] = False
            total -= est[i]
        out = [m for i, m in enumerate(messages) if keep[i]]
        if total > budget_tokens and out and out[0].get("role") == "system":
            sys_txt = str(out[0].get("content") or "")
            other = total - cls._estimate_msg_tokens(sys_txt)
            # 先按段弹未保护的注入尾段（与云端裁剪器同一把尺、同一保护名单）
            sys_txt, total, _, _ = cls._pop_sys_parts_to_budget(sys_txt, other, budget_tokens)
            if total > budget_tokens:
                # 本地 num_ctx 是硬上限（超了整包 400）：最后手段按**估算器**二分截尾，
                # 不再用「1 token=3 字符」换算（CJK 会多砍 3 倍，人设整段消失）。
                room = max(0, budget_tokens - other)
                lo, hi = 0, len(sys_txt)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if cls._estimate_msg_tokens(sys_txt[:mid]) <= room:
                        lo = mid
                    else:
                        hi = mid - 1
                sys_txt = sys_txt[:lo].rstrip()
            out = list(out)
            out[0] = dict(out[0])
            out[0]["content"] = sys_txt
        return out

    def _record_local_fallback_metric(self, ok: bool, latency_ms: float = 0.0) -> None:
        try:
            from src.monitoring.metrics_store import get_metrics_store
            _ms = get_metrics_store()
            _ms.record_local_llm_fallback(ok)
            if ok and latency_ms > 0:
                _ms.record_api_call(latency_ms)
        except Exception:
            pass

    async def _fb_native_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
        model: str = "",
    ) -> tuple:
        """兜底模型走 Ollama 原生 /api/chat：/v1 兼容层不认 keep_alive/think（实测被忽略），
        原生口才能让兜底模型断云期驻留显存（keep_alive）+ 思考系模型不慢答（think:false）。
        ``model`` 空＝``ai.fallback.model``（分层覆写经此透传）。
        返回 (content, prompt_tokens, completion_tokens)，风格对齐 _ollama_native_chat。"""
        import httpx
        payload: Dict[str, Any] = {
            "model": str(model or "").strip() or self._fb_model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }
        # num_ctx 必须随请求下发：runner 默认 -c 4096，长对话 prompt（人设+记忆+KB+历史
        # 实测 4300+ tokens）会被 llama-server 400 拒绝（2026-07-14 断云窗口实锤）。
        if self._fb_num_ctx > 0:
            payload["options"]["num_ctx"] = self._fb_num_ctx
        if self._fb_keep_alive and self._fb_keep_alive.lower() not in ("0", "off", "none"):
            payload["keep_alive"] = self._fb_keep_alive
        _to = httpx.Timeout(self._fb_timeout, connect=5.0)
        async with httpx.AsyncClient(timeout=_to) as _hc:
            resp = await _hc.post(f"{self._fb_native_base}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        msg = data.get("message") or {}
        content = (msg.get("content") or "").strip()
        pt = int(data.get("prompt_eval_count") or 0)
        ct = int(data.get("eval_count") or 0)
        return content, pt, ct

    async def rewrite_local(
        self,
        system_prompt: str,
        user_text: str,
        *,
        timeout_sec: float = 8.0,
        max_tokens: int = 240,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """用 LAN 本地模型（``ai.fallback`` 端点）做一次性短文本改写/工具任务。

        与 ``_try_local_fallback_chat`` 的本质区别：**这不是主链兜底顶班**——
          - 不跑语言守卫 / QualityTracker（那是对话回复的质检，改写任务不需要）；
          - **不记 local_fallback「顶班」metric**（否则会污染 degradation_snapshot
            的「本地在顶班」降级判定，把一次工具调用误判成云端故障）；
        纯粹「系统提示 + 文本 → 文本」的本地工具调用（如语音口语化改写）。
        本地端点未配置 / 失败 / 超时 → ``None``（调用方自行回落），**绝不占云成本**。
        """
        if not (self._fb_model and (self._fb_native_base or self._fb_client)):
            return None
        messages = [
            {"role": "system", "content": str(system_prompt or "")},
            {"role": "user", "content": str(user_text or "")},
        ]

        async def _run() -> str:
            if self._fb_native_base:
                content, _pt, _ct = await self._fb_native_chat(
                    messages, max_tokens=max_tokens, temperature=temperature)
                return content or ""
            kw: Dict[str, Any] = dict(
                model=self._fb_model, messages=messages,
                temperature=temperature, max_tokens=max_tokens)
            if self._fb_extra_body:
                kw["extra_body"] = self._fb_extra_body
            resp = await self._fb_client.chat.completions.create(**kw)
            if resp and getattr(resp, "choices", None):
                return (resp.choices[0].message.content or "").strip()
            return ""

        try:
            out = await asyncio.wait_for(
                _run(), timeout=max(1.0, float(timeout_sec)))
            return (out or "").strip() or None
        except Exception as e:  # 超时/连接失败/取消 → 交调用方回落
            self.logger.debug("rewrite_local failed: %s", e)
            return None

    async def rewrite_cloud(
        self,
        system_prompt: str,
        user_text: str,
        *,
        timeout_sec: float = 12.0,
        max_tokens: int = 600,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """同 ``rewrite_local``，但走**主云端模型**（``ai.base_url``/``ai.model``）。

        用于「本地 LAN 模型不可用 / 运营选择云端质量」的短文本工具任务（如语音口语化）。
        与主对话链的区别同 ``rewrite_local``：不跑语言守卫/质检、**不碰熔断窗口、不记
        主链 ok 时间戳**——工具调用失败不该被 ``degradation_snapshot`` 误读成云端故障。
        成本单列 ``tier="tool"``，与主链/备用池/本地兜底的用量分开看。
        仅支持 OpenAI 兼容口（本仓生产口径）；其它 provider 返回 None 由调用方回落。
        """
        if not (self._oa_client and self.model):
            return None
        messages = [
            {"role": "system", "content": str(system_prompt or "")},
            {"role": "user", "content": str(user_text or "")},
        ]

        async def _run() -> str:
            kw: Dict[str, Any] = dict(
                model=self.model, messages=messages,
                temperature=temperature, max_tokens=max_tokens)
            if self._oa_extra_body:
                kw["extra_body"] = self._oa_extra_body
            resp = await self._oa_client.chat.completions.create(**kw)
            self._record_direct_usage(resp, purpose="tool")
            if resp and getattr(resp, "choices", None):
                return (resp.choices[0].message.content or "").strip()
            return ""

        try:
            out = await asyncio.wait_for(
                _run(), timeout=max(1.0, float(timeout_sec)))
            return (out or "").strip() or None
        except Exception as e:
            self.logger.debug("rewrite_cloud failed: %s", e)
            return None

    def _apply_tier_overrides(
        self,
        strategy_overrides: Optional[Dict[str, Any]],
        context: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """★ P5-4：按 context.ai_tier 合并 tier overrides 到 strategy_overrides。

        优先级：strategy_overrides（调用者显式给的） > tier > 实例默认
        → 所以 tier overrides 只填充 strategy_overrides 里**未指定的**字段。
        """
        if not self._tiers_enabled or not self._tiers:
            return strategy_overrides
        tier = str((context or {}).get("ai_tier") or "").strip()
        if not tier:
            # P4：无显式 ai_tier 时按会员权益自动选档——``entitlement.tier``
            # （vip/svip…，A 线 _ensure_entitlement 懒加载）与 ``ai.tiers`` 档名
            # 对齐即进档；档名未配置/无权益 → 默认档。显式 ai_tier 永远优先，
            # 运营可只给 vip 配 ``local_model`` 让高价值会话吃大模型。
            ent = (context or {}).get("entitlement")
            if isinstance(ent, dict):
                _ent_tier = str(ent.get("tier") or "").strip().lower()
                if _ent_tier and _ent_tier in self._tiers:
                    tier = _ent_tier
                    # 回写 context（仅自动进档且原位为空时）：云链成本按
                    # context.ai_tier 记账（P6-4），不回写会把 VIP 会话记成
                    # default，档位 ROI 无法对账。落默认档时刻意不写，
                    # 保住既有 "default" 记账口径。
                    if isinstance(context, dict) and not context.get("ai_tier"):
                        context["ai_tier"] = tier
        if not tier:
            tier = self._tiers_default
        spec = self._tiers.get(tier) or self._tiers.get(self._tiers_default) or {}
        if not spec:
            return strategy_overrides
        merged = dict(strategy_overrides or {})
        for k, v in spec.items():
            if k in ("enabled", "default"):
                continue
            merged.setdefault(k, v)
        # 记录一下（方便排查）
        merged.setdefault("_ai_tier", tier)
        return merged

    def set_ecommerce_tools(self, svc: Any) -> None:
        """注入电商工具服务（EcommerceToolService）。None 则关闭事实注入。"""
        self.ecommerce_tools = svc

    async def _maybe_inject_ecommerce_facts(
        self, user_message: str, context: Optional[Dict[str, Any]]
    ) -> None:
        """命中订单号 → 查真实订单 → 把事实写入 context['_ecommerce_facts']。

        - 仅在注入了 ecommerce_tools 且消息含订单号时触发；查得到必注（高价值低风险）。
        - 查不到/出错时，仅当消息明显含订单/物流意图才注入「如实告知查不到」守卫，
          避免把随机数字（金额/电话）误判为订单号而产生噪声。
        - 全程 best-effort：任何异常静默降级，绝不阻断回复主链路。
        - service 层自带超时/审计，这里不再重复。
        """
        svc = getattr(self, "ecommerce_tools", None)
        if svc is None or context is None:
            return
        if context.get("_ecommerce_facts"):
            return  # 上游已注入，尊重之
        if context.get("_bare_system"):
            return  # 工具/翻译类裸 system 调用：事实块不会被消费，省一次订单查询
        try:
            from src.ecommerce_tools import (
                extract_order_no, extract_tracking_no,
                has_order_intent, is_ecom_intent,
            )
            order_no = extract_order_no(user_message)
            tracking_no = extract_tracking_no(user_message)
            if not order_no and not tracking_no:
                return
            # 门槛：有上游分类意图(skill 路径会塞 context['intent'])则以它为权威，
            # 更准、可避免「含 order 字样但意图是闲聊」误报；否则回落关键词正则。
            _ci = str((context or {}).get("intent") or "").strip()
            intent = is_ecom_intent(_ci) if _ci else has_order_intent(user_message)
            facts_parts: List[str] = []
            # 订单事实：查得到必注；查不到仅在含订单/物流意图时注入「如实告知」守卫
            if order_no:
                res = await svc.lookup_order(order_no, by="reply_gen")
                if res.found or intent:
                    f = res.to_context_facts()
                    if f:
                        facts_parts.append(f)
            # 物流事实：仅在查得到时注入（多数 connector 对未知单号返 None，
            # 注入「查不到」守卫会在每条含长数字的消息上误报，故只注正向事实）。
            if tracking_no and tracking_no != order_no:
                res2 = await svc.track_shipment(tracking_no, by="reply_gen")
                if res2.found:
                    f2 = res2.to_context_facts()
                    if f2:
                        facts_parts.append(f2)
            if facts_parts:
                context["_ecommerce_facts"] = "\n".join(facts_parts)
        except Exception:
            self.logger.debug("ecommerce 事实注入跳过", exc_info=True)

    def _build_route_clients(self, ai_config: Dict[str, Any], api_key: Optional[str]) -> None:
        """解析 ai.models + ai.task_routes → self._route_clients / self._task_routes。

        纯构造（只建 client 对象、不发网络请求，可单测）。models=命名模型档（各自独立
        OpenAI 兼容端点）；task_routes=任务名→档名（指向不存在的档静默忽略）。api_key
        缺省复用主链 key；本地端点无鉴权时填占位 'ollama'。任何异常回落「无路由」。
        """
        self._route_clients = {}
        self._task_routes = {}
        try:
            models = ai_config.get("models") or {}
            if isinstance(models, dict):
                for name, spec in models.items():
                    if not isinstance(spec, dict):
                        continue
                    m_base = str(spec.get("base_url") or "").strip().rstrip("/")
                    if not m_base or "://" not in m_base:
                        continue
                    if not m_base.endswith("/v1"):
                        m_base = m_base + "/v1"
                    m_model = str(spec.get("model") or self.model or "").strip()
                    if not m_model:
                        continue
                    try:
                        from src.ai.vendor_params import normalize_model as _nm
                        m_model, _m_note = _nm(m_base, m_model)
                        if _m_note:
                            self.logger.info("模型档 %s：%s", name, _m_note)
                    except Exception:
                        pass
                    m_key = str(spec.get("api_key") or api_key or "").strip()
                    if not m_key or m_key.upper().startswith("YOUR_"):
                        m_key = "ollama"  # 本地端点常无需鉴权
                    try:
                        import httpx as _hx
                        m_to: Any = _hx.Timeout(float(self.timeout), connect=5.0)
                    except Exception:
                        m_to = float(self.timeout)
                    m_host = m_base.split("://", 1)[1].split("/", 1)[0]
                    try:
                        from src.ai.vendor_params import thinking_off_extra_body as _toff
                        m_extra = _toff(m_base, m_model, reasoning=bool(
                            spec.get("reasoning", ai_config.get("reasoning"))))
                    except Exception:
                        m_extra = {}
                    _m_http = None
                    if _is_lan_base_url(m_base):
                        try:
                            _m_http = _lan_httpx_async_client(m_to)
                        except Exception:
                            _m_http = None
                    self._route_clients[str(name)] = {
                        "client": AsyncOpenAI(
                            api_key=m_key, base_url=m_base, timeout=m_to, max_retries=0,
                            **({"http_client": _m_http} if _m_http is not None else {})),
                        "model": m_model,
                        "label": f"{m_model} @ {m_host} ({name})",
                        "extra_body": m_extra,
                        "base_url": m_base,
                    }
            # 会话级「无限制」档（conv_route，2026-09-12）：``ai.models.<ai.unrestricted.profile>``
            # 未显式配时绑到 LAN 兜底端点（局域网私有模型＝无限制实体，现网 173:8001/chatx）。
            # 与下方 memory_extract→_lan_tool 同一招式；LAN 兜底也没有 → 不绑＝该档不可用
            # （UI 探活会如实报 no_endpoint，绝不静默换成云端）。
            try:
                from src.ai.conv_route import profile_name as _unr_profile
                _unr_name = _unr_profile(ai_config if isinstance(ai_config, dict)
                                         and "ai" in ai_config else {"ai": ai_config})
            except Exception:
                _unr_name = "unrestricted"
            if (_unr_name not in self._route_clients
                    and getattr(self, "_fb_client", None)
                    and str(getattr(self, "_fb_model", "") or "").strip()):
                _fb_base = ""
                try:
                    _fb_base = str(getattr(self._fb_client, "base_url", "") or "").rstrip("/")
                except Exception:
                    _fb_base = ""
                self._route_clients[_unr_name] = {
                    "client": self._fb_client,
                    "model": self._fb_model,
                    "label": f"{self._fb_model} @ lan ({_unr_name})",
                    "extra_body": dict(getattr(self, "_fb_extra_body", None) or {}),
                    "base_url": _fb_base,
                }
            routes = ai_config.get("task_routes") or {}
            if isinstance(routes, dict):
                for task, prof in routes.items():
                    p = str(prof or "").strip()
                    if p and p in self._route_clients:
                        self._task_routes[str(task)] = p
            # 记忆抽取默认走 LAN 兜底（有才绑）：短 JSON、不服务客户，不必吃云端 ¥2/M。
            # 显式 ai.task_routes.memory_extract 优先；LAN 失败仍回主链降级链。
            if ("memory_extract" not in self._task_routes
                    and getattr(self, "_fb_client", None)
                    and str(getattr(self, "_fb_model", "") or "").strip()):
                self._route_clients.setdefault("_lan_tool", {
                    "client": self._fb_client,
                    "model": self._fb_model,
                    "label": f"{self._fb_model} @ lan (memory_extract)",
                    "extra_body": dict(getattr(self, "_fb_extra_body", None) or {}),
                })
                self._task_routes["memory_extract"] = "_lan_tool"
                self.logger.info(
                    "记忆抽取默认走本地兜底 model=%s（ai.task_routes.memory_extract 可改）",
                    self._fb_model)
            if self._route_clients:
                self.logger.info(
                    "多模型路由已配置: %d 档（%s）；任务映射 %s",
                    len(self._route_clients), ", ".join(self._route_clients.keys()),
                    self._task_routes or "（无）")
        except Exception:
            self.logger.debug("多模型路由解析失败（忽略，回落默认模型）", exc_info=True)

    def resolve_route(self, task: Optional[str]) -> Optional[Dict[str, Any]]:
        """任务名 → 模型档（{client, model, label}）。未配/未命中=None（走默认模型）。

        与 key_pool（失效才顶班）正交：命中后仅换「主尝试」的 client+model；失败仍回落
        备用池 → 本地兜底 → canned（绝不因路由丢话）。
        """
        if not task:
            return None
        t = str(task)
        if t.startswith("profile:"):
            # 直接点名模型档（会话级路由 conv_route：``profile:unrestricted``），不经任务映射
            return self._route_clients.get(t[len("profile:"):].strip())
        name = self._task_routes.get(t)
        if not name:
            return None
        return self._route_clients.get(name)

    async def generate_reply(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        strategy_overrides: Optional[Dict[str, Any]] = None,
        *,
        route: Optional[str] = None,
        _skip_quality_check: bool = False,
    ) -> Optional[str]:
        """
        生成回复（含重试与兜底；主链/备用池/本地兜底全部失败 → 返回 None＝本轮不回复）

        strategy_overrides 可包含 temperature / max_tokens / context_rounds
        以覆盖实例默认值，实现按策略差异化调用。

        _skip_quality_check：yes/no 短答型 prompt（如 chat() 入口）开启此项后
        跳过 QualityTracker，避免 3 字符回复被误判 too_short。
        """
        strategy_overrides = self._apply_tier_overrides(strategy_overrides, context)
        # P1-b：命中订单号则注入电商真实事实（含「查不到就如实说」反幻觉守卫）
        await self._maybe_inject_ecommerce_facts(user_message, context)
        # 本地链只挂在 openai-compat 分支上（_try_local_fallback_chat 只在那条链里被调），
        # 故**零云端配置的全本地部署**也必须走这里，否则会掉进 gemini 分支拿不到本地模型。
        if self._use_openai_compat or (
            self._primary_mode in ("local", "local_only")
            and self._fb_client and self._fb_model
        ):
            reply = await self._generate_reply_openai_compat(
                user_message, context, conversation_history, strategy_overrides,
                route=route,
                _skip_quality_check=_skip_quality_check,
            )
            # 2026-08-19 Token 计量（P5a 观测接线，licensing.token_ledger.enabled
            # 默认关=零行为）：成功出稿记 ai_reply（10 Token/条）。观测口径=出稿数
            # （含未投递草稿，计费上界）；P5b 切到投递点后本处降级为影子对照。绝不抛。
            # P6：本地模型出话（断云顶班/enforce 降级/本地主链）带 _reply_free_path
            # 标记＝免费路径不记账；pop 消费防 context 跨轮残留误豁免云端回复。
            _free_path = False
            try:
                if context is not None:
                    _free_path = bool(context.pop("_reply_free_path", False))
            except Exception:
                _free_path = False
            # 2026-09-08：演练号段（duel_nightly 990001xxx）不记 ai_reply——它此前把
            # 「每天 AI 回复 56 条」虚高成真实客户的 5 倍，老板日报/周报全被带偏。
            _is_drill = False
            _purpose = "customer_reply"
            try:
                from src.ai.llm_purpose import purpose_for_reply
                _purpose = purpose_for_reply(context)
                _is_drill = _purpose == "drill"
            except Exception:
                _is_drill = False
            # B2（2026-09-11 用量分析）：只有**客户回复**才记 ai_reply。工具短判 / 记忆
            # 抽取 / 翻译纠错 / 探针也从本出口出话（chat() → generate_reply），此前一律
            # 按「AI 回复」扣 10 Token——198 坐席 09-04~10 对账：钱包实扣 2,400 条 vs
            # 真实回复 1,777 条（多扣 35%）。产品口径「AI 智能回复 10 Token/条」指的是
            # 发给客户的那一条，不是链路里的每次 LLM 调用。用途由 purpose_for_reply 定：
            # 显式 _llm_purpose > purpose_scope > 演练号 > customer_reply。
            if (reply and not _free_path and not _is_drill
                    and _purpose == "customer_reply"):
                try:
                    from src.licensing.token_ledger import record_action_for_status

                    record_action_for_status("ai_reply", 1)
                except Exception:
                    # 保持 fail-open（计量绝不能挡出话），但**不能无声**：这里丢一次
                    # 就是账本少记一条 ai_reply，钱包余额会显得比真实更耐用，而
                    # enforce 切换正是按这个余额与「跑道天数」拍板的（2026-08-28
                    # 实测跑道仅 0.8 天，误差直接影响决策）。
                    self.logger.warning("Token 计量记账失败（已放行出话，账本会少记一条 ai_reply）",
                                        exc_info=True)
            return reply
        _fb_lang = (context or {}).get("reply_lang", "zh")
        if not self.client:
            self.logger.error("AI 客户端未初始化")
            return self._fallback_reply(_fb_lang)
        if context is not None:
            context["_current_user_message_for_lang"] = user_message
        if self._cb_enabled and self._cb_open_until > 0:
            now = time.time()
            if now < self._cb_open_until:
                self.logger.warning("AI 熔断开路中，跳过 API 调用 request_id=%s", (context or {}).get("request_id") or "n/a")
                return self._fallback_reply(_fb_lang)
            if not self._cb_half_open:
                self._cb_half_open = True
                self.logger.info("AI 熔断进入半开状态，允许一次探测请求")
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().set_circuit_breaker_state("half-open", self._cb_open_until)
                except Exception:
                    pass

        so = strategy_overrides or {}
        use_temperature = float(so["temperature"]) if "temperature" in so else self.temperature
        use_max_tokens = int(so["max_tokens"]) if "max_tokens" in so else self.max_tokens
        if use_max_tokens < 256:
            use_max_tokens = 256
        use_context_rounds = int(so["context_rounds"]) if "context_rounds" in so else None
        use_model = str(so["model"]) if so.get("model") else self.model

        start_time = time.time()
        system_instruction = self._build_system_instruction(context)
        contents = self._build_contents(user_message, context, conversation_history,
                                        context_rounds_override=use_context_rounds)
        request_id = (context or {}).get("request_id", "")
        last_error = None
        _attempts_made = 0
        _fail_reason = "empty"
        for attempt in range(2):
            _attempts_made = attempt + 1
            try:
                use_thinking = int(so.get("thinking_budget", 0))
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction if system_instruction else None,
                    temperature=use_temperature,
                    max_output_tokens=use_max_tokens,
                    thinking_config=types.ThinkingConfig(thinking_budget=use_thinking),
                )

                response = await self.client.aio.models.generate_content(
                    model=use_model,
                    contents=contents,
                    config=config,
                )

                elapsed_time = time.time() - start_time
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_api_call(elapsed_time * 1000)
                except Exception:
                    pass

                if response and response.candidates:
                    candidate = response.candidates[0]
                    reply = None
                    try:
                        reply = response.text
                    except (ValueError, IndexError):
                        pass
                    finish = str(candidate.finish_reason) if candidate.finish_reason else None

                    if reply:
                        self.total_calls += 1
                        self._clear_ai_fail(context)
                        pt = ct = 0
                        um = response.usage_metadata
                        if um:
                            pt = getattr(um, "prompt_token_count", 0) or 0
                            ct = getattr(um, "candidates_token_count", 0) or 0
                            self.total_tokens += pt + ct
                        self.last_call_time = time.time()
                        self._last_primary_ok_ts = time.time()
                        if finish and "MAX_TOKENS" in finish.upper():
                            self.logger.warning(
                                "AI 回复因 max_output_tokens 截断 (finish_reason=%s, "
                                "max_output_tokens=%d, completion=%d, request_id=%s)",
                                finish, use_max_tokens, ct, request_id or "n/a",
                            )
                        # ★ P6-4：Gemini 分支 cost tracking
                        try:
                            from src.ai.llm_cost import get_llm_cost, purpose_for_reply
                            _ctx = context or {}
                            get_llm_cost().record(
                                model=str(self.model),
                                prompt_tokens=pt,
                                completion_tokens=ct,
                                tier=str(_ctx.get("ai_tier") or "default"),
                                account_id=str(_ctx.get("account_id") or "default"),
                                latency_ms=int(elapsed_time * 1000),
                                purpose=purpose_for_reply(_ctx),
                                provider="google",
                            )
                        except Exception:
                            self.logger.debug("llm_cost.record 失败", exc_info=True)
                        self.logger.debug(
                            "AI回复生成: %.2fs, tokens=%d, finish=%s, request_id=%s",
                            elapsed_time, pt + ct, finish, request_id or "n/a"
                        )
                        if self._cb_enabled:
                            self._cb_window.append(True)
                            if self._cb_half_open:
                                self._cb_half_open = False
                                self._cb_open_until = 0.0
                                self._cb_window.clear()
                                self.logger.info("AI 半开探测成功，熔断器关闭")
                                try:
                                    from src.monitoring.metrics_store import get_metrics_store
                                    get_metrics_store().set_circuit_breaker_state("closed")
                                except Exception:
                                    pass
                                self._alert_circuit_recovered()
                        stripped = reply.strip()
                        stripped = await self._guard_reply_language(stripped, context)
                        stripped = self._shape_single_paragraph(stripped, context)
                        # ★ QualityTracker / reply_length 移到 guard 之后，记录最终发出的文本；
                        #   _skip_quality_check：yes/no 短答（chat()）跳过 too_short 误报。
                        if not _skip_quality_check:
                            self._quality_tracker.record_call(
                                prompt_tokens=pt, completion_tokens=ct,
                                elapsed_ms=int(elapsed_time * 1000),
                                reply=stripped, request_id=request_id,
                            )
                            try:
                                from src.monitoring.metrics_store import get_metrics_store
                                _ms = get_metrics_store()
                                _ms.record_reply_length(len(stripped))
                                _ms.record_ai_success()
                                if finish and "MAX_TOKENS" in finish.upper():
                                    _ms.record_truncated_reply()
                            except Exception:
                                pass
                        return stripped
                self.logger.warning("AI 返回空响应")
                if self._cb_enabled:
                    self._cb_window.append(False)
                    self._maybe_trip_circuit()
            except Exception as e:
                last_error = e
                self.logger.warning("AI 调用失败(attempt=%s): %s", attempt + 1, e)
                _fail_reason = self._classify_ai_error(e)
                if _fail_reason == "timeout":
                    break   # Q-14 A：读超时不重试
                if attempt == 0:
                    await asyncio.sleep(1.5)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            _ms = get_metrics_store()
            _ms.record_error()
            _ms.record_ai_error()
        except Exception:
            pass
        if self._cb_enabled:
            self._cb_window.append(False)
            self._maybe_trip_circuit()
        self._note_ai_fail(
            _fail_reason if last_error is not None else "empty", context,
            latency_ms=int((time.time() - start_time) * 1000), attempt=_attempts_made,
            model=str(use_model), err=last_error)
        self._alert_key_failure_if_matches(last_error)
        return self._fallback_reply(_fb_lang)

    def _alert_circuit_open(self, detail: str) -> None:
        """熔断开路＝「云端挂了」的可靠信号（含不带 key 特征串的网络黑洞/云端宕机），
        主机弹窗告知机主；备用池/本地兜底是否就绪写进文案。绝不抛。"""
        try:
            from src.utils.host_alert import notify_cloud_outage
            if self._pool_entries:
                detail = f"{detail}；备用 Key 池 {len(self._pool_entries)} 个可顶班"
            notify_cloud_outage(
                self._alert_label(), detail,
                fallback_ready=bool(self._fb_client and self._fb_model),
            )
        except Exception:
            pass

    def _alert_circuit_recovered(self) -> None:
        """熔断关闭（半开探测成功）→ 补一条「已恢复」，闭环机主体验：收到「不可达」
        弹窗后无需反复上机确认。与 outage 各自 30min 去抖，抖动期最多一来一回。绝不抛。"""
        try:
            from src.utils.host_alert import notify_host
            notify_host(
                "云端 AI 已恢复",
                f"{self._alert_label()} 探测成功，熔断已关闭，恢复云端出话。",
                key=f"outage-recovered:{self._alert_label()}",
            )
        except Exception:
            pass

    def _maybe_trip_circuit(self):
        """窗口内失败比例超阈值则开路；半开探测失败则加倍 open 时长"""
        if self._cb_half_open:
            backoff = min(self._cb_open_seconds * 2, 600)
            self._cb_open_until = time.time() + backoff
            self._cb_half_open = False
            self.logger.error("AI 半开探测失败，重新开路 %.0fs", backoff)
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().set_circuit_breaker_state("open", self._cb_open_until)
            except Exception:
                pass
            self._alert_circuit_open(f"半开探测失败，重新开路 {backoff:.0f}s")
            return
        if len(self._cb_window) < self._cb_window_size:
            return
        fails = sum(1 for x in self._cb_window if not x)
        rate = fails / len(self._cb_window)
        if rate >= self._cb_fail_threshold:
            self._cb_open_until = time.time() + self._cb_open_seconds
            self.logger.error(
                "AI 熔断开路 %.0fs（窗口失败率 %.0f%%）",
                self._cb_open_seconds, rate * 100
            )
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().set_circuit_breaker_state("open", self._cb_open_until)
            except Exception:
                pass
            self._alert_circuit_open(
                f"窗口失败率 {rate * 100:.0f}%（{fails}/{len(self._cb_window)}），"
                f"开路 {self._cb_open_seconds:.0f}s")

    @staticmethod
    def _has_chinese_japanese_mixing(reply: str) -> bool:
        """Detect alternating Chinese/Japanese sentences in one reply.

        Japanese text always contains kana even when using kanji.
        A sentence with CJK but zero kana is Chinese, not Japanese.
        Returns True when the reply has BOTH kana-bearing sentences (Japanese)
        AND pure-CJK-no-kana sentences (Chinese) — i.e. mixed output.
        """
        has_japanese = False
        has_pure_chinese = False
        for sent in re.split(r"[\u3002\uff01\uff1f!?\n]", reply):
            sent = sent.strip()
            if len(sent) < 4:
                continue
            kana = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF]", sent))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", sent))
            if kana >= 2:
                has_japanese = True
            elif cjk >= 4 and kana == 0:
                has_pure_chinese = True
        return has_japanese and has_pure_chinese

    @staticmethod
    def _reply_lang_mismatch(reply: str, expected_lang: str) -> bool:
        """Return True when *reply* is clearly NOT in *expected_lang*.

        Used by :meth:`_guard_reply_language` both to trigger correction and
        to verify that the corrected text actually matches.

        Heuristics (lightweight, no LLM call):
          - ja/ko: Japanese requires kana; Korean requires Hangul.  A reply
            with CJK but zero kana/hangul is Chinese, not the target language.
          - Other non-zh scripts (ar_ur, hi, ru, th …): at least one char of
            the target script should appear.
          - en / Latin languages: CJK-dominant text is a mismatch.
          - zh: never considered a mismatch (caller already short-circuits).
        """
        if not reply or not expected_lang:
            return False
        if expected_lang == "zh":
            # 目标中文却回成英文（真机 bug：中文会话里模型窜英文、且旧守卫对 zh 直接放行
            # → 从不纠正）。保守判定：几乎无中文(cjk<=2) 且拉丁字母成句(>=12)才算不符，
            # 避免误伤「哈哈ok啦」这类正常中英混说。
            cjk = len(re.findall(r"[\u4e00-\u9fff]", reply))
            letters = len(re.findall(r"[A-Za-z]", reply))
            return cjk <= 2 and letters >= 12

        if expected_lang == "ja":
            kana = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF]", reply))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", reply))
            return kana == 0 and cjk > 8

        if expected_lang == "ko":
            hangul = len(re.findall(r"[\uAC00-\uD7AF\u1100-\u11FF]", reply))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", reply))
            return hangul == 0 and cjk > 8

        # Non-CJK scripts: check the target script appears at least once
        _script_patterns = {
            "ar_ur": r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]",
            "hi": r"[\u0900-\u097F]",
            "ru": r"[\u0400-\u04FF]",
            "th": r"[\u0E00-\u0E7F]",
            "pa": r"[\u0A00-\u0A7F]",
            "bn": r"[\u0980-\u09FF]",
        }
        pat = _script_patterns.get(expected_lang)
        if pat:
            if not re.search(pat, reply):
                cjk = len(re.findall(r"[\u4e00-\u9fff]", reply))
                return cjk > 8
            return False

        # en / Latin: CJK-dominant → mismatch
        cjk = len(re.findall(r"[\u4e00-\u9fff]", reply))
        letters = len(re.findall(r"[A-Za-z]", reply))
        if cjk > 8 and letters < cjk:
            return True
        # B121/#64（0830 三度复报）：拉丁主体夹 CJK 残字（「I'm 我, and I'm not
        # going anywhere.」）——出站语种一致性的「拦截→重写」档：判 mismatch 让
        # _guard_reply_language 走一次轻量重写（比出稿口硬剥少数派字符产出更通顺
        # 的成句）；重写仍不干净时守卫回退原文，出稿口 outbound_text_guard 的
        # 确定性剥除仍是最后防线。形状与 detect_lang_mix 的 hard 档同口径
        # （字母 ≥6 且 ≥3×CJK 且 CJK ≥1），教学场景（重写后仍带该词）自然回退。
        if letters >= 6 and cjk >= 1 and letters >= 3 * cjk:
            return True
        return False

    def _chat_reply_surface(self, context: Optional[Dict[str, Any]]) -> bool:
        """context 是否聊天回复出口（陪伴域或 RPA 聊天链）＝回复格式治理适用面。

        与 ``_build_context_prompt`` 的「回复格式」合同注入面同判据。
        工具型调用（``chat()``，context=None/空）恒 False——抽取/摘要等
        结构化多行输出零触碰。
        """
        if not isinstance(context, dict) or not context:
            return False
        # B1：裸 system 的工具调用现在也带 context（用途 / 裸 system 标记），
        # 但它们不是聊天出口——抽取 JSON / 译文 / 分类结果一律零触碰。
        if context.get("_tool_call") or context.get("_bare_system"):
            return False
        try:
            _cfg = (self.config.config or {}) if self.config else {}
            if not isinstance(_cfg, dict):
                return False
            _ch = str(context.get("channel") or "").lower()
            return (
                effective_domain_name(_cfg) == "conversion"
                or _ch in ("whatsapp_rpa", "messenger_rpa", "line_rpa")
            )
        except Exception:
            return False

    def _single_paragraph_mode(self, context: Optional[Dict[str, Any]]) -> bool:
        """聊天回复是否须收成**一个段落**（2026-08-08）。

        判据与 ``_build_context_prompt`` 的「回复格式」合同完全同源：
        聊天回复出口（``_chat_reply_surface``）且投递层 bubbles **未开**（多行
        不会被拆成独立消息，只会呈现为一条消息里的多段）→ True。
        """
        if not self._chat_reply_surface(context):
            return False
        try:
            _cfg = (self.config.config or {}) if self.config else {}
            from src.inbox.reply_split import parse_bubbles_cfg
            return not bool(parse_bubbles_cfg(_cfg).get("enabled"))
        except Exception:
            return False

    def _shape_single_paragraph(
        self, reply: Optional[str], context: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """回复形态出口护栏（绝不抛）——聊天出口两档，工具型调用零触碰：

        - 单段落合同（bubbles 关）：把 LLM 惯性写出的多行折成单段——prompt
          合同是软约束，历史窗口里的旧两段式回复就是现成 few-shot，换合同后
          模型仍会偶发换行。
        - 多行合同（bubbles 开，2026-08-15 拍板）：换行保留（投递层按行/句
          拆条），但**段落间空行一律剔除**——空行是 LLM 的文章排版惯性，
          拆条侧本就忽略空行，坐席在回复台/输入框看到的却是「段1+空行+段2」
          的文章体。
        """
        if not reply or not self._chat_reply_surface(context):
            return reply
        try:
            from src.inbox.reply_split import collapse_paragraphs, strip_blank_lines
            if self._single_paragraph_mode(context):
                collapsed = collapse_paragraphs(reply)
                if collapsed and collapsed != reply:
                    self.logger.debug(
                        "single-paragraph guard collapsed reply (%d -> %d chars)",
                        len(reply), len(collapsed))
                return collapsed or reply
            stripped = strip_blank_lines(reply)
            if stripped and stripped != reply:
                self.logger.debug(
                    "blank-line guard stripped reply (%d -> %d chars)",
                    len(reply), len(stripped))
            return stripped or reply
        except Exception:
            return reply

    async def _guard_reply_language(
        self, reply: str, context: Optional[Dict[str, Any]]
    ) -> str:
        """Post-generation safety net: if reply language clearly mismatches
        reply_lang, attempt a single lightweight translation correction."""
        if not reply or not context:
            return reply
        _rl = context.get("reply_lang")
        if not _rl or context.get("_skip_lang_guard"):
            return reply
        # 安全网（2026-07-23，belt-and-suspenders）：若**当前入站消息本身**就是对语言 L
        # 的明确请求，且回复已满足 L，则信「刚刚的现场请求」而非可能陈旧的 reply_lang
        # （window/sticky 偶发滞后）——拒绝把已经正确的回复翻走。这是对上游 lang_policy
        # 源头修复的二道防线（真实事故：客户粤语要中文、reply_lang 陈旧=en → 中文回复被
        # 整段翻成英文发出）。仅在「现场请求 ≠ 期望 且 回复已符合现场请求」时短路。
        try:
            _um = (context.get("_current_user_message_for_lang") or "").strip()
            if _um:
                from src.ai.lang_policy import parse_language_request
                _req = parse_language_request(_um)
                if _req and _req != _rl and not self._reply_lang_mismatch(reply, _req):
                    self.logger.info(
                        "Language guard skipped: live request=%s overrides stale "
                        "reply_lang=%s", _req, _rl,
                    )
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_lang_event("guard_live_request_skip")
                    except Exception:
                        pass
                    return reply
        except Exception:
            pass
        if _rl == "zh":
            # 目标中文：仅当回复明显是英文(几乎无中文)才纠正，否则放行（零误伤中英混说）。
            if not self._reply_lang_mismatch(reply, "zh"):
                return reply
        # ja/ko: Japanese uses lots of kanji so the CJK-ratio check is unreliable.
        # Instead, trigger only when the reply has NO kana at all (clearly Chinese).
        elif _rl in ("ja", "ko"):
            kana = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF]", reply))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", reply))
            if not (kana == 0 and cjk > 8):
                return reply
        # 注：companion(domain=conversion) 模式过去在此整段跳过守卫，导致英文客户
        # 在「中文人设 + 长段中文历史」惯性下仍被回中文（prompt 级语言指令顶不住）。
        # 现统一走 _reply_lang_mismatch：仅当回复「明显不符」（如目标英文却是纯中文，
        # cjk 占绝对多数）才纠正——对中文会话(reply_lang=zh 已在上方短路)与正常
        # 目标语回复零误伤，只兜底「客户切语言、模型没跟上」这类明确事故。
        elif not self._reply_lang_mismatch(reply, _rl):
            return reply
        _lang_name = self._LANG_NAMES.get(_rl, _rl)
        self.logger.warning(
            "Language guard triggered: expected=%s, reply=%s...",
            _rl, reply[:60],
        )
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_lang_mismatch()
        except Exception:
            pass
        try:
            # B1（2026-09-11）：纠错翻译走裸 system + tool 用途。此前用普通 context
            # → 再拼一遍整套人设/硬约束（3.8k+ 字）去翻译一条 30 字回复，且按
            # customer_reply 又记一次 ai_reply（一条回复扣两次）。
            _fix_ctx = {
                "_skip_lang_guard": True,
                "_llm_purpose": "tool",
                "_tool_call": True,
                "_bare_system": (
                    f"You are a translation tool. Translate the following customer-service "
                    f"reply into {_lang_name}. Keep channel names (EP/JC/EasyPaisa/JazzCash) "
                    f"and commands unchanged. Output ONLY the translation, no explanation."
                ),
                "_current_user_message_for_lang": "x" * 5,
            }
            corrected = await self.generate_reply(
                f"Translate to {_lang_name}:\n\n{reply}",
                _fix_ctx,
            )
            if corrected and len(corrected) > 5:
                if not self._reply_lang_mismatch(corrected, _rl):
                    self.logger.info("Language guard corrected reply successfully")
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_lang_event("guard_corrected")
                    except Exception:
                        pass
                    return corrected
                self.logger.warning("Language guard correction still mismatched, using original")
        except Exception as e:
            self.logger.warning("Language guard correction failed: %s", e)
        return reply

    def _fallback_reply(self, lang: str = "zh") -> Optional[str]:
        """全链失败（主链/备用池/本地兜底都没出话）→ 返回 None＝本轮不回复。

        2026-08-15 起罐头占位句移除（运营裁决「可以不回复，不能乱回复」）：
        「在的，有什么可以帮您的？」这类客服腔在陪聊人设会话里是一眼穿帮的
        乱回复（8/13 local_only+173 宕机 → 三平台批量罐头刷屏；8/15 脏话被
        云端过滤成空响应 → 同句复发）。消费面对空回复语义早已闭环：A 线
        None＝不发送（与冷却吞没同路径）、B 线空稿＝不建稿、主动触达空文案
        ＝跳过本轮。metrics 计数保留＝「全链失败」观测口径不变。
        """
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_fallback_reply()
        except Exception:
            pass
        self.logger.warning(
            "AI 全链失败：本轮不回复（罐头兜底已移除，lang=%s）", lang or "zh")
        return None

    def set_domain_pack(self, system_prompt: str = "", terminology: dict = None, context_supplements: dict = None):
        """Apply domain pack overrides for prompts, terminology, and context supplements.
        If no system_prompt is set in config.yaml, the domain pack's prompt is used instead.
        """
        self._domain_system_prompt = system_prompt or ""
        self._domain_terminology = terminology or {}
        self._domain_context_supplements = context_supplements or {}
        if not self.system_prompt and self._domain_system_prompt:
            self.system_prompt = self._domain_system_prompt
            # P-4 #254（MTRCH2）：带业务域名——prompt 变体按 business_domain 选
            # （system_prompt_by_business_domain），只打字数让人以为陪伴 / 销售没切。
            try:
                from src.utils.business_domain import active_business_domain
                _bd = str(active_business_domain() or "") or "?"
            except Exception:
                _bd = "?"
            self.logger.info("System prompt loaded from domain pack (business_domain=%s, %d chars)",
                             _bd, len(self.system_prompt))

    def _primary_system_prompt_text(self) -> str:
        """主系统提示：优先读当前 config 中的 ai.system_prompt，避免进程内 self.system_prompt 与后台保存不一致。"""
        if not self.config or not getattr(self.config, "config", None):
            return (self.system_prompt or "").strip()
        ai_cfg = (self.config.config or {}).get("ai") or {}
        sp = (ai_cfg.get("system_prompt") or "").strip()
        if sp:
            return sp
        return (self.system_prompt or "").strip()

    def _build_system_instruction(self, context: Optional[Dict[str, Any]] = None) -> str:
        """将主系统提示词 + 快速设置 + 上下文提示合并为单一 system_instruction"""
        parts = []
        _deferred_dp_block = ""   # 深度人设块推后到静态段之后（缓存友好布局，见下）
        context = context or {}
        # 裸 system 通道（P0-198，2026-07-31）：工具型调用（翻译引擎等）传
        # ``_bare_system`` = 完整 system 文本，跳过全局 system_prompt / 人设块 /
        # 深度人设注入。实锤事故：AIEngine.translate 经 chat() 走完整聊天管线，
        # 入站「So you married ?」被 conversion 域人设按 bio 答成「离过婚，现在
        # 是单身」写进译文槽——翻译调用绝不能带人设身份。
        _bare = str(context.get("_bare_system") or "").strip()
        if _bare:
            return _bare
        _suppress_global_identity = bool(context.get("suppress_global_ai_identity"))
        # 单一身份源（2026-07-20）：会话/账号被显式绑定了人设时（chat_binding / account_profile
        # 两个 tier），只用该人设身份，压制全局 ai.system_prompt（否则如 顾嘉(system_prompt) 与
        # 林佳欣(会话人设) 双身份塞进提示词会互相打架、名字说岔）。未显式绑定的会话（域/默认 tier，
        # 如 Telegram 主线）保持原样用全局 system_prompt → 零影响。tier 解析是纯内存字典查，很轻；
        # 任何异常回落 False = 旧行为。此 flag 同时门控下方 _primary 与 _name_ov（名字覆盖）。
        try:
            from src.utils.persona_manager import PersonaManager as _PM_early
            _pm_early = _PM_early.get_instance()
            _cid_early = str((context or {}).get("chat_id", "") or "")
            _acc_early = str((context or {}).get("account_persona_id", "") or "")
            _, _tier_early = _pm_early.get_persona_with_tier(_cid_early, _acc_early)
            if _tier_early in ("chat_binding", "account_profile"):
                _suppress_global_identity = True
        except Exception:
            pass
        _primary = "" if _suppress_global_identity else self._primary_system_prompt_text()
        if _primary:
            parts.append(_primary)

        # 后台人设（Web「默认人设」/ persona_runtime.yaml / 域包）：与静态 system_prompt 叠加
        ai_cfg_pre = (self.config.config or {}).get("ai", {}) if self.config else {}
        _pbd = (ai_cfg_pre.get("persona_block_detail") or "full").strip().lower()
        try:
            from src.ai.usage_economy import persona_detail as _econ_pbd
            _pbd = _econ_pbd(self.config, _pbd)
        except Exception:
            pass
        if _pbd not in ("none", "full", "compact"):
            _pbd = "full"
        _name_ov = "" if _suppress_global_identity else (ai_cfg_pre.get("ai_name") or "").strip()
        # 无人设解析出真名时的兜底真名（防回落到域标签「线上陪伴」被当名字念出）
        _fallback_name = (ai_cfg_pre.get("fallback_display_name") or "").strip()
        try:
            from src.utils.persona_manager import PersonaManager

            _pm = PersonaManager.get_instance()
            _p_cid = str((context or {}).get("chat_id", "") or "") if context else ""
            _p_acc_pid = str((context or {}).get("account_persona_id", "") or "") if context else ""
            # P10-C: derive platform from channel; pass funnel_stage for tone injection
            _channel = str((context or {}).get("channel", "") or "")
            _p_platform = "whatsapp" if "whatsapp" in _channel else _channel
            _p_funnel = str((context or {}).get("funnel_stage", "") or "") if context else ""
            # #155（2026-09-03）：称呼硬钉子按**联系人**取值（爱称是客户关系
            # 属性，不是人设属性）。会话 id 三段齐了才传——缺任一段说明本次
            # 调用没有会话上下文（工具型/预览），按纯人设级=旧行为。
            _p_conv_id = ""
            try:
                _c = context or {}
                _cn_p = str(_c.get("platform") or _c.get("channel") or "").strip()
                _cn_a = str(_c.get("account_id") or "").strip()
                _cn_k = str(_c.get("chat_key") or "").strip()
                if _cn_p and _cn_a and _cn_k:
                    _p_conv_id = f"{_cn_p.lower()}:{_cn_a}:{_cn_k}"
            except Exception:
                _p_conv_id = ""
            _p_block = _pm.format_persona_block(
                _p_cid, detail=_pbd, name_override=_name_ov,
                account_persona_id=_p_acc_pid,
                platform=_p_platform,
                funnel_stage=_p_funnel,
                fallback_name=_fallback_name,
                conversation_id=_p_conv_id,
            )
            if _p_block:
                parts.append("【后台人设定位 · 须遵守】\n" + _p_block)
                # #131（2026-09-01，#109 回复质量家族）：人称/角色关系硬规则。
                # 实锤：客户（卖车的）说「给你爸买个车，再找我买车险」，AI 答
                # 「我爸那车再开十年没问题」还反过来「想换车第一个喊我报价」
                # ——把客户口中的「你爸」听岔、又把自己代入成卖方。规则确定性
                # 注入（零 LLM 成本，几十 token），只随人设块出现。
                parts.append(
                    "【人称与角色 · 硬规则】对方消息里的「你/你的」都指你自己"
                    "（上述人设），「我/我的」指对方；「你爸/你妈/你家人」＝你"
                    "（人设）的家人，不是对方的。回复前先分清这轮谁在卖、谁在买、"
                    "谁求助、谁帮忙：对方向你推销或提供服务时，你是被推销的一方，"
                    "绝不能反过来把自己说成卖方/报价方/服务方。历史消息同理，"
                    "按各自说话人归属人称，别把对方说过的事当成自己的。"
                )
            _p_resolved, _p_tier = _pm.get_persona_with_tier(_p_cid, _p_acc_pid)
            _p_name = _p_resolved.get("name", "?")
            # ★ 传给 context，让 _build_context_prompt 能做名字锁定；名字锁定必须用
            #   「可对外说的名字」——绝不锁到域标签「线上陪伴」，否则被问名字必答该标签。
            _p_spoken = _pm.resolve_spoken_name(
                _p_resolved, name_override=_name_ov, fallback=_fallback_name
            )
            if context is not None and _p_spoken:
                context["_resolved_persona_name"] = _p_spoken
            # 人设本地时钟（与 deep_persona 开关无关）：海外人设按当地时刻驱动
            # 时间行 / temporal_anchor / 场景状态；失败则静默回落服务器时钟。
            _persona_local_now = None
            try:
                from src.companion.persona_location import (
                    resolve_place_with_fallback,
                    persona_now as _persona_now_fn,
                    local_time_line,
                    time_gap_line,
                )
                _place = resolve_place_with_fallback(_p_resolved)
                _persona_local_now = _persona_now_fn(_place)
                if context is not None:
                    context["_resolved_persona"] = _p_resolved
                    context["_persona_local_now"] = _persona_local_now
                    if _place is not None:
                        context["_persona_place_label"] = _place.display("zh")
                        _lt = local_time_line(_place, "zh", _persona_local_now)
                        if _lt:
                            context["_persona_local_time_line"] = _lt
                        _gap = time_gap_line(_place, "zh", _persona_local_now)
                        if _gap:
                            context["_persona_time_gap_line"] = _gap
            except Exception:
                self.logger.debug("[persona_location] 本地时钟解析跳过", exc_info=True)
            # 深度人设增强（5 层：生活线/关系画像+回指/口味/内部梗/经历式记忆/拟人细节）——
            # 默认关，master flag `companion.deep_persona.enabled` 灰度；best-effort 绝不阻塞。
            try:
                _dp_cfg = (
                    ((self.config.config or {}).get("companion", {}) or {}).get(
                        "deep_persona", {}) if self.config else {}
                )
                if isinstance(_dp_cfg, dict) and _dp_cfg.get("enabled"):
                    import random as _rnd
                    from datetime import datetime as _dt
                    from src.companion.deep_persona import build_deep_persona_block
                    _dctx: Dict[str, Any] = {"callback_roll": _rnd.random()}
                    _crisis = str((context or {}).get("_wellbeing_crisis_level", "") or "")
                    _dctx["suppress_callbacks"] = _crisis in ("elevated", "severe")
                    # 实施55「聊过即退役」：生活线素材按会话排除已聊过的；
                    # 本轮选中的 beat 栈进 context，回复真提及时在
                    # _update_after_reply 记账（先清残留再按需落键）。
                    context.pop("_life_beat_current", None)
                    context.pop("_life_beat_ck", None)
                    _lb_ck = ""
                    try:
                        from src.companion.life_beat_ledger import (
                            convo_key_from_context as _lb_ckfn,
                            skip_fn_for as _lb_skipfn,
                        )
                        _lb_ck = _lb_ckfn(context)
                        if _lb_ck:
                            _dctx["beat_skip_fn"] = _lb_skipfn(_lb_ck)
                    except Exception:
                        _lb_ck = ""
                    # C1：当前用户消息作为经历召回的相关性查询（情感×时近×相关加权）
                    _dctx["query_text"] = str(
                        (context or {}).get("_current_user_message_for_lang", "") or "")
                    try:
                        from src.companion.deep_persona_store import get_deep_persona_store
                        _store = get_deep_persona_store("config/deep_persona.db")
                        # 深度人设 store 键：优先 conversation_id（与 ingest 写入同键），
                        # 回落 chat_id。解决 autodraft 路径 store 层读不到数据的问题。
                        _store_cid = str((context or {}).get("conversation_id", "") or "") or _p_cid
                        if _store is not None and _store_cid:
                            _dctx["relationship_profile"] = _store.get_relationship_profile(_store_cid)
                            _dctx["inside_jokes"] = _store.get_inside_jokes(_store_cid)
                            _dctx["experiential_events"] = _store.get_experiential(_store_cid)
                            _dctx["open_loops"] = _store.get_open_loops(_store_cid)
                    except Exception:
                        self.logger.debug("[deep_persona] store 读取失败（忽略）", exc_info=True)
                    # E3（默认关）：人设自身跨会话「见闻」话题（去标识聚合）注入
                    try:
                        if _dp_cfg.get("self_memory"):
                            from src.companion.persona_self_memory import (
                                get_persona_self_memory)
                            _psm = get_persona_self_memory("config/deep_persona.db")
                            _pid_sm = str(_p_acc_pid or (_p_resolved.get("id") if isinstance(
                                _p_resolved, dict) else "") or "")
                            if _psm is not None and _pid_sm:
                                _dctx["self_topics"] = _psm.top_topics(_pid_sm)
                    except Exception:
                        self.logger.debug("[deep_persona] self_memory 读取失败（忽略）", exc_info=True)
                    # E1 点火（默认关）：semantic_recall 开时，embed 当前消息一次 → 用缓存事件
                    # 向量构造语义 sim_fn，让经历召回按语义相关（缺 embedder/向量自动回落字面）。
                    try:
                        if _dp_cfg.get("semantic_recall") and _dctx.get("query_text"):
                            _evs = _dctx.get("experiential_events") or []
                            _emap = {str(e.get("what") or ""): e.get("emb")
                                     for e in _evs if e.get("emb")}
                            if _emap:
                                from src.companion.deep_persona_runtime import get_embedder
                                _emb = get_embedder()
                                _qv = _emb(_dctx["query_text"]) if _emb else None
                                if _qv:
                                    from src.companion.deep_persona import make_embedding_sim_fn
                                    _dctx["experiential_sim_fn"] = make_embedding_sim_fn(_qv, _emap)
                    except Exception:
                        self.logger.debug("[deep_persona] 语义 sim 构造失败（回落字面）", exc_info=True)
                    _dp_now = _persona_local_now if _persona_local_now is not None else _dt.now()
                    _dp_block = build_deep_persona_block(
                        _p_resolved, now=_dp_now, cfg=_dp_cfg, stage=_p_funnel,
                        deep_ctx=_dctx, imperfection_roll=_rnd.random(),
                    )
                    _lb_beat = str(_dctx.get("_life_beat_chosen") or "")
                    if _lb_beat and _lb_ck:
                        context["_life_beat_current"] = _lb_beat
                        context["_life_beat_ck"] = _lb_ck
                    if _dp_block:
                        # 缓存友好布局（2026-09-11）：深度人设块每轮都变（随机瑕疵/生活线/时钟），
                        # 若紧跟人设块会把 DeepSeek 前缀缓存在此截断；推后到静态段（快速设置 /
                        # 语言规则）之后、上下文块之前——模型读到的信息不变，稳定前缀更长。
                        _deferred_dp_block = _dp_block
                        if "后来怎么样了" in _dp_block:
                            try:
                                from src.companion.deep_persona_stats import (
                                    get_deep_persona_stats)
                                get_deep_persona_stats().incr("callbacks_emitted")
                            except Exception:
                                pass
            except Exception:
                self.logger.debug("[deep_persona] 增强块拼装失败（忽略）", exc_info=True)
            _req_id = str((context or {}).get("request_id", "") or "")
            if _p_tier in ("chat_binding", "account_profile"):
                self.logger.info(
                    "[persona] tier=%s name=%r acc_pid=%r cid=%s req=%s",
                    _p_tier, _p_name, _p_acc_pid or "—", _p_cid or "—", _req_id or "—",
                )
            else:
                self.logger.debug(
                    "[persona] tier=%s name=%r cid=%s",
                    _p_tier, _p_name, _p_cid or "—",
                )
        except Exception:
            # 不可静默：人设块拼装失败会让回复整段丢失人设、悄悄回落域默认，
            # 是「徽标对、人设错」类故障的根源。保留回落行为，但必须可见。
            self.logger.warning("[persona] 人设块拼装失败，本次回复将缺失人设定位", exc_info=True)

        ai_cfg = (self.config.config or {}).get("ai", {}) if self.config else {}
        ai_name = "" if _suppress_global_identity else (ai_cfg.get("ai_name") or "").strip()
        reply_style = (ai_cfg.get("reply_style") or "").strip()
        overrides = []
        if ai_name:
            overrides.append(
                f"你的名字是「{ai_name}」，用户问你叫什么、是谁、怎么称呼你，都必须用这个名字；"
                "若上文（含人设块、知识库）出现其他称呼，一律以本句为准。"
            )
        _STYLE_MAP = {
            "concise": (
                "回复风格：简洁干练，少废话，直接给结论和动作。"
                "**禁止**在句首使用填充语气词：如「嗯」「嗯嗯」「呃」「那个」等；"
                "可直入主题，或以「好的」「收到」等短承接开头（视语境）。"
                "若与上文系统提示中的开场白参考冲突，**以本段为准**。"
            ),
            "warm": (
                "回复风格：温暖、活泼、恋爱向腻聊；可用撒娇/反问/昵称感语气词，避免油腻刷屏。"
                "少用「报告体」和机械分条；非必要不用 Markdown 大标题；不要主动推销查单/通道/支付。"
            ),
            "professional": "回复风格：正式专业，措辞严谨，适合商务场景。",
        }
        if reply_style in _STYLE_MAP:
            overrides.append(_STYLE_MAP[reply_style])
        if overrides:
            parts.append("【快速设置覆盖】\n" + "\n".join(overrides))

        _reply_lang = (context or {}).get("reply_lang", "zh") if context else "zh"
        _lang_name = self._LANG_NAMES.get(_reply_lang, "")
        _cfg_ins = (self.config.config or {}) if self.config and hasattr(self.config, "config") else {}
        _instr_companion = isinstance(_cfg_ins, dict) and effective_domain_name(_cfg_ins) == "conversion"
        # B6（2026-09-11）：语言规则抽到 language_rule.py，≤400 字。旧块中英双语各说一遍
        # 「禁中文 / 先翻译」，英文会话常到 600–2700 字且落在 system 尾部——裁剪器一紧就丢。
        from src.ai.language_rule import language_rule_block
        parts.append(language_rule_block(
            _reply_lang, _lang_name, companion=_instr_companion))
        if isinstance(context, dict):
            context["_lang_rule_emitted"] = True

        if _deferred_dp_block:
            parts.append(_deferred_dp_block)

        context_prompt = self._build_context_prompt(context)
        if context_prompt:
            parts.append(context_prompt)

        if context and context.get("_intent_supplement"):
            parts.append(context["_intent_supplement"])

        # P1-b：电商真实事实（订单/物流）— 高优先级，强约束「只可基于事实、勿编造」
        _ecom_facts = (context or {}).get("_ecommerce_facts") if context else ""
        if _ecom_facts:
            parts.append(
                "【电商实时事实 · 最高优先级 · 必须遵守】\n"
                f"{_ecom_facts}\n"
                "你只能依据以上事实回答订单/物流相关问题；事实未覆盖的细节，"
                "必须如实告知客户暂无该信息，严禁编造订单状态、金额、物流进度或时间。"
            )

        # 真人感文本层 L1（platform/spoken_style 桥接，默认关；ai.spoken_style.enabled）
        # role=本会话人设口称名（上方 persona 解析已写进 context）→ 说话指纹按人设分流
        # 无限制会话（conv_route）：说话指纹属风格规则层，让路（身份 / 语言规则 / 能力一致性保留）
        try:
            from src.ai.conv_route import skip_guard as _cr_skip_l1
            if _cr_skip_l1(context, "spoken_style"):
                return "\n\n".join(parts)
        except Exception:
            pass
        try:
            from src.ai.spoken_style_bridge import system_block as _ss_system_block
            # #40：context 透传 → 地区档（zh-TW/zh-HK）按 人设/居住地/会话「发→」解析；
            # zh-CN 档恒不追加（存量 prompt 逐字不变）
            _ss_b = _ss_system_block(
                self.config,
                role=str((context or {}).get("_resolved_persona_name") or ""),
                context=context,
            )
            if _ss_b:
                parts.append(_ss_b)
        except Exception:
            pass

        return "\n\n".join(parts)

    _MAX_HISTORY_CHARS = 12000

    def _build_contents(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        context_rounds_override: Optional[int] = None
    ) -> list:
        """
        构建 Gemini 原生 contents 数组（仅 user/model 角色）。
        双重截断：先按轮数裁剪，再按字符总量从最早消息开始丢弃，
        防止超长历史导致 prompt 过大。
        """
        contents = []

        max_hist = context_rounds_override if context_rounds_override is not None else \
            max(1, getattr(self, "max_conversation_history", 10) or 10)
        try:
            from src.ai.context_depth import history_limit as _cd_hist
            max_hist = _cd_hist(self.config, max_hist, strategy_rounds=context_rounds_override)
        except Exception:
            pass
        if conversation_history:
            _lim = max(0, int(max_hist))
            hist = [] if _lim == 0 else conversation_history[-_lim:]
            if len(conversation_history) > _lim and _lim > 0:
                self.logger.debug(
                    "conversation_history 已截断: %s -> %s 条",
                    len(conversation_history), _lim
                )
            total_chars = sum(len(m.get("content", "")) for m in hist)
            if total_chars > self._MAX_HISTORY_CHARS:
                trimmed = []
                budget = self._MAX_HISTORY_CHARS
                for msg in reversed(hist):
                    c_len = len(msg.get("content", ""))
                    if budget >= c_len:
                        trimmed.append(msg)
                        budget -= c_len
                    else:
                        break
                trimmed.reverse()
                dropped = len(hist) - len(trimmed)
                if dropped > 0:
                    self.logger.debug(
                        "历史按字符截断: 丢弃最早 %d 条 (%d chars -> %d chars)",
                        dropped, total_chars, total_chars - budget
                    )
                    trimmed.insert(0, {
                        "role": "user",
                        "content": f"[...此前有 {dropped} 条对话已省略...]"
                    })
                hist = trimmed
            for msg in hist:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "assistant":
                    role = "model"
                elif role == "system":
                    continue
                if not content:
                    continue
                if contents and contents[-1].role == role:
                    prev_text = contents[-1].parts[0].text if contents[-1].parts else ""
                    contents[-1] = types.Content(
                        role=role,
                        parts=[types.Part(text=prev_text + "\n" + content)]
                    )
                else:
                    contents.append(types.Content(
                        role=role,
                        parts=[types.Part(text=content)]
                    ))

        # 添加当前用户消息
        if contents and contents[-1].role == "user":
            prev_text = contents[-1].parts[0].text if contents[-1].parts else ""
            contents[-1] = types.Content(
                role="user",
                parts=[types.Part(text=prev_text + "\n" + user_message)]
            )
        else:
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text=user_message)]
            ))

        return contents

    def _media_capability_hint_enabled(self) -> bool:
        """「发图协议/边界声明」的**全局前置**：selfie 子系统启用（本部署真会
        自动发图）且未被 ``companion.media_promise_guard.capability_hint`` 显式
        关闭。注意（2026-07-31 语义修正）：这只是能力**开**态的注入前置——
        能力**关**态（selfie 关/人设 capabilities.photos 关）不再是「什么都不注入」，
        而是经 ``_capability_hint_allowed`` 注入「无发图能力」硬约束
        （photo_capability SSOT），修「发不了图却放任 LLM 承诺发图」的倒挂。"""
        try:
            cfg = (self.config.config or {}) if self.config else {}
            comp = (cfg.get("companion") or {}) if isinstance(cfg, dict) else {}
            if not ((comp.get("selfie") or {}).get("enabled", False)):
                return False
            pg = (comp.get("media_promise_guard") or {})
            return bool(pg.get("capability_hint", True))
        except Exception:
            return False

    def _capability_hint_allowed(self) -> bool:
        """媒体能力边界**文本**是否允许注入（``companion.media_promise_guard.
        capability_hint``，默认开）。与能力开不开正交：本开关管「说不说」，
        能力判定管「说哪种」（开=发图协议；关=无发图硬约束）。异常按开处理——
        默认姿态是「无能力须约束」，宁多说一句不放任承诺。"""
        try:
            cfg = (self.config.config or {}) if self.config else {}
            comp = (cfg.get("companion") or {}) if isinstance(cfg, dict) else {}
            pg = (comp.get("media_promise_guard") or {})
            return bool(pg.get("capability_hint", True))
        except Exception:
            return True

    def _photo_intent_mode(self) -> str:
        """发图意图模式（companion.selfie.intent.mode）：keyword|llm|hybrid（默认）。

        llm/hybrid → prompt 注入「主动决策」协议（LLM 用 [PHOTO …] 标记声明发图）；
        keyword → 注入旧式被动声明（完整回退开关，见 photo_directive 模块）。"""
        try:
            cfg = (self.config.config or {}) if self.config else {}
            scfg = (((cfg.get("companion") or {}).get("selfie")) or {})
            from src.ai.photo_directive import resolve_intent_mode
            return resolve_intent_mode(scfg if isinstance(scfg, dict) else {})
        except Exception:
            return "hybrid"

    def _build_context_prompt(self, context: Optional[Dict[str, Any]]) -> str:
        """
        构建上下文提示（含关键信息锚定，确保订单号/额度结论不被窗口滚动丢弃）
        """
        if not context:
            return ""
        prompt_parts = []
        # Phase 1：用户画像注入 — runner 已渲染好 markdown 块塞 _contact_portrait_block
        _portrait_block = (context.get("_contact_portrait_block") or "").strip()
        if _portrait_block:
            prompt_parts.append(_portrait_block)
        # 2026-08-18 跨平台档案叙事：客户来源平台/原平台称呼/聊过的话题域——
        # skill_manager._inject_origin_context 渲染好的块，有键即消费（与 bazi 同模式）。
        _origin_block = (context.get("_origin_block") or "").strip()
        if _origin_block:
            prompt_parts.append(_origin_block)
        _cfg_ctx = (self.config.config or {}) if self.config else {}
        _is_companion = isinstance(_cfg_ctx, dict) and effective_domain_name(_cfg_ctx) == "conversion"
        _wa = (_cfg_ctx.get("web_admin") or {}) if isinstance(_cfg_ctx, dict) else {}
        _site = (_wa.get("site_name") or "").strip()
        # LINE 个人号 RPA：人设以全文「后台人设」为准；此处仅渠道与系统名提示
        if context.get("channel") == "line_rpa":
            if _site:
                prompt_parts.append(
                    f"【系统】你是在「{_site}」中协助客户转化类对话；语气须与上文「后台人设定位」一致。"
                )
            _lh = (context.get("line_rpa_style_hint") or "").strip()
            if _lh:
                prompt_parts.append("【LINE 补充说明】\n" + _lh)
            else:
                prompt_parts.append(
                    "【LINE 渠道】当前为手机 LINE 一对一私聊；回复简短自然，避免与上文人设冲突的客服套话。"
                )
            # P7-1：Vision / 结构化读屏得到的「伪用户消息」以 [标签] 开头
            _um_line = (context.get("_current_user_message_for_lang") or "").strip()
            if _um_line.startswith("[LINE贴图]"):
                prompt_parts.append(
                    "【LINE 多模态·贴图】对方发来贴图；你看到的是系统代述的标签+简短描述，并非逐像素识图。"
                    "回复宜口语化、一两句即可，可带少量 emoji；不要编造画面里不存在的细节。"
                )
            elif _um_line.startswith("[图片消息]"):
                prompt_parts.append(
                    "【LINE 多模态·图片】对方发来图片/截图；描述来自模型归纳，可能不完整。"
                    "可自然评论或简短追问，避免假装看清了全部文字或 UI。"
                )
            elif _um_line.startswith("[视频消息]"):
                prompt_parts.append(
                    "【LINE 多模态·视频】对方发来视频；描述仅来自缩略图识别，"
                    "不要假装看完了整段视频，可自然回应或简短追问内容。"
                )
            elif _um_line.startswith("[动图消息]"):
                prompt_parts.append(
                    "【LINE 多模态·动图】对方发来 GIF 动图；回应宜轻松口语，可接梗，一两句即可。"
                )
            elif _um_line.startswith("[语音消息]"):
                prompt_parts.append(
                    "【LINE 多模态·语音】对方发来语音；若仅见时长等占位描述，回复宜简短确认或表示稍后方便文字沟通。"
                )
            elif _um_line.startswith("[文件消息]"):
                prompt_parts.append(
                    "【LINE 多模态·文件】对方发来文件；不知具体内容时勿臆测，可简短询问用途或表示收到。"
                )
            if context.get("vision_room"):
                prompt_parts.append(
                    "【LINE 读屏模式】本条上下文来自截图+多模态识别，可能存在遗漏；回复保持容错、简短。"
                )
        # WhatsApp RPA：渠道说明 + 可选 style_hint
        if context.get("channel") == "whatsapp_rpa":
            _wh = (context.get("whatsapp_rpa_style_hint") or "").strip()
            _peer = (context.get("whatsapp_rpa_peer_name") or "").strip()
            if _wh:
                prompt_parts.append("【WhatsApp 人设补充】\n" + _wh)
            else:
                _peer_clause = f"对方名字是「{_peer}」；" if _peer else ""
                prompt_parts.append(
                    f"【WhatsApp 渠道】当前为 WhatsApp 一对一私聊；{_peer_clause}"
                    "回复风格偏朋友式聊天，简短自然（建议 1-3 句，可含少量 emoji），"
                    "避免长段客服套话；称呼用对方在 WhatsApp 上的名字，不主动提及其他平台。"
                )
        # Messenger RPA：渠道说明 + 可选 style_hint（从 config 读）
        if context.get("channel") == "messenger_rpa":
            _mh = (context.get("messenger_rpa_style_hint") or "").strip()
            if _mh:
                prompt_parts.append("【Messenger 人设补充】\n" + _mh)
            else:
                prompt_parts.append(
                    "【Messenger 渠道】当前为 Facebook Messenger 一对一私聊；"
                    "回复风格偏朋友式聊天，简短自然（建议 1-2 句，可含少量 emoji），"
                    "避免长段客服套话；称呼用对方在 Messenger 上的名字，不主动提及其他平台。"
                )
            _peer_kind = (context.get("messenger_rpa_peer_kind") or "").strip().lower()
            if _peer_kind == "image":
                prompt_parts.append(
                    "【Messenger 多模态·图片】对方发来图片；你看到的是系统代述的描述，"
                    "不要假装看清了画面的所有细节，可自然评论或简短追问。"
                )
            elif _peer_kind == "voice":
                prompt_parts.append(
                    "【Messenger 多模态·语音】对方发来语音；回复宜简短确认或表示稍后方便文字沟通。"
                )
            elif _peer_kind == "sticker":
                prompt_parts.append(
                    "【Messenger 多模态·贴纸】对方发来贴纸；回复口语化，一两句即可，贴纸氛围偏轻松。"
                )
        # WhatsApp / 通用语音消息提示（对方发语音，AI 回复应更口语/简短）
        if context.get("_peer_message_is_voice"):
            _vdur = context.get("_voice_duration") or ""
            _vdur_txt = f"（时长 {_vdur}）" if _vdur else ""
            prompt_parts.append(
                f"【语音消息】对方发来语音{_vdur_txt}（已转文字）。"
                "你的回复应更口语化、简短自然，像在对讲一样回应，避免书面长段。"
            )
            # 可疑转写（ASR 语种与会话语言冲突/低置信）→ 澄清话术：像真人一样
            # 「听不太清就先确认」，绝不基于可疑文本自信作答或切换语言。
            if context.get("_voice_lang_suspect"):
                prompt_parts.append(
                    "【语音听不清 · 澄清优先】这条语音的转文字结果很可能不准确"
                    "（识别语种与你们平时聊的语言对不上）。不要直接照转写内容回答，"
                    "也绝对不要因此改变回复语言。用你们一直在用的语言，像真人一样"
                    "轻松地确认一下（例如「刚才语音有点听不清，你是想说…吗？」），"
                    "只确认这一次，语气自然不要道歉过度。"
                )
            elif context.get("_voice_asr_suspect"):
                # ASR P1：转写置信度低（avg_logprob / 语种概率 / no_speech 灰区 / 极短片段），
                # 内容可能有错字或听漏——与上面的语种冲突块同族，语种块在场时不重复。
                # 实锤：「你那边几点怎么会说大造成呢」——听错还自信接话是陪聊穿帮的直接来源。
                prompt_parts.append(
                    "【语音可能听错 · 先确认】这条语音的转文字置信度偏低，其中可能有错字、"
                    "同音词或漏听的内容。不要对可疑的词句自信作答、不要顺着它编细节；"
                    "先用一句轻松的话确认你理解的意思（例如「你刚说的是…对吧？」或"
                    "「语音有点听不清，是说…吗？」），语言保持你们一直在用的，"
                    "只确认这一次，不要反复道歉。"
                )
        # 入站媒体（WhatsApp / Telegram 收件箱 / Messenger 等多端共用）。
        # 防御：已转写语音（_peer_message_is_voice 在场）由上方【语音消息】块叙述，
        # 此处再按媒体块渲染要么复述要么「暂无法识别」自相矛盾——正常入口已不再为
        # 转写语音置 _peer_message_is_media（protocol_autoreply 2026-08-16），这里兜
        # 历史路径/多路径叠置（WA runner 语音分支等）。
        if context.get("_peer_message_is_media") and not (
            str(context.get("_media_kind") or "").strip().lower() == "voice"
            and context.get("_peer_message_is_voice")
        ):
            _mkind = context.get("_media_kind") or "media"
            _mdesc = (context.get("_media_desc") or "").strip()
            _kind_hint = {
                "image":            "图片/截图",
                "sticker":          "贴纸",
                "animated_sticker": "动态贴纸",
                "video":            "视频（缩略图识别）",
                "gif":              "GIF 动图",
                "voice":            "语音",
                "file":             "文件",
            }.get(_mkind, "媒体消息")
            _plat = str(
                context.get("platform") or context.get("channel") or "chat"
            ).strip().lower()
            _plat_label = {
                "telegram": "Telegram", "whatsapp": "WhatsApp",
                "whatsapp_rpa": "WhatsApp", "messenger": "Messenger",
                "messenger_rpa": "Messenger", "line": "LINE", "inbox": "收件箱",
                "wechat": "微信", "wechat_pc": "微信",
            }.get(_plat, "聊天")
            # B117（实施74，0826 _576 实录「贴纸被当娃照评论」）：贴纸/GIF 是
            # **表达心情**的符号，不是真实生活照片——识图描述只作语义参考，
            # 指令层硬分流禁照片式评论（问「这是你拍的/这是谁」= 当场穿帮）。
            _stickerish = _mkind in ("sticker", "animated_sticker", "gif")
            if _stickerish:
                # #143（0902，接 #74）：参考语义是系统自动标注（恒为中文），与
                # image_ocr_text 块同款语言锚——英文会话发贴纸曾被这段中文带偏成
                # 整条中文回复；语义上补钉「不当作对方本人照片、不追问出处」。
                _sk_ref = (
                    f"参考语义（系统自动标注，非对方话语）：{_mdesc}\n"
                    if _mdesc else "")
                prompt_parts.append(
                    f"【{_plat_label} 媒体消息·{_kind_hint}】对方发了一个表情贴纸/动图"
                    f"（＝对方在表达情绪，不是生活照片）。{_sk_ref}"
                    "回复要求：贴纸=表达心情或态度，只回应对方此刻的情绪即可（轻松口语，"
                    "一两句）；绝不把画面当真实生活照片评论、绝不当作对方本人的照片"
                    "（不问「这是你拍的吗/这是谁/这是你吗/亲戚家的？」这类），"
                    "不追问贴纸内容的出处，不逐字描述画面；拿不准含义就轻接一句，"
                    "不下断言。上面参考语义的语言不代表对方的语言，回复语言一律按"
                    "【输出语言】/LANGUAGE RULE 执行。"
                )
            elif _mdesc:
                # Q-36 #313（2JK95C「几个菜 → 一桌菜」/ #318 VV7BRY「Marina」）：识图描述进 prompt 时
                # 同步钉「只说看到的、不加量词、地物不当住处 / 专名」——出稿口 outbound_text_guard 的
                # 量词软改是兜底，这里在源头消掉。措辞单源 image_observation.CAPTION_RULE。
                try:
                    from src.inbox.image_observation import CAPTION_RULE as _cap_rule
                except Exception:
                    _cap_rule = ""
                prompt_parts.append(
                    f"【{_plat_label} 媒体消息·{_kind_hint}】系统已识别对方发来的媒体内容如下：\n"
                    f"{_mdesc}\n"
                    "回复要求：像真人一样自然回应这条媒体消息，不要说「我无法查看图片」；"
                    "若识别内容不清楚可温和追问；视频仅基于缩略图内容回应，不要假装看完整个视频。"
                    + (f"\n{_cap_rule}" if _cap_rule else "")
                )
            else:
                if _mkind == "voice":
                    # 电脑微信入站语音没有声音文件/转写（屏上只有「语音N秒」）。
                    # 旧口径「温和追问想表达什么」会让模型装听过或猜内容；
                    # 这里钉死：承认收到 + 请打字，绝不假装听过。
                    prompt_parts.append(
                        f"【{_plat_label} 媒体消息·语音】对方发来语音，但你听不到内容"
                        "（只有时长占位，没有声音文件、也没有转写）。"
                        "自然承认收到了语音，请对方打字说；不要假装听过、不要猜内容、"
                        "不要说「语音有点听不清」。"
                    )
                else:
                    prompt_parts.append(
                        f"【{_plat_label} 媒体消息·{_kind_hint}】对方发来了一条{_kind_hint}消息，"
                        "内容暂无法识别。请自然承认收到了，并温和追问对方想表达或想了解什么；"
                        "贴纸/表情宜轻松口语，一两句即可。"
                    )
        # B104（实施74 三批）生成端第一道：初次对话防编造引用——出稿守卫
        # （outbound_text_guard.strip_unfounded_recall）是兜底，这里在源头消掉。
        # 三重保守判据：**必须显式带 _conversation_history 键**（试聊/copilot 等
        # 不带历史跟踪的流不注入——对老会话谎称「初次」比编造引用更伤）、
        # 无历史摘要（压缩过=老会话）、无长期记忆。
        try:
            _hist_b104 = context.get("_conversation_history")
            if (isinstance(_hist_b104, list)
                    and not any(isinstance(m, dict) and m.get("role") == "user"
                                for m in _hist_b104)
                    and not context.get("_conversation_summary")
                    and not str(context.get("_episodic_memory_text") or "").strip()):
                prompt_parts.append(
                    "【初次对话】这是你们的第一次交谈，此前没有任何聊天记录："
                    "绝不编造「你之前说过/上次你提到/如你所说」这类过往引用，"
                    "也不假装记得对方；把对方当刚认识的人自然开场。"
                )
        except Exception:
            pass
        # 关键信息锚定：置顶，避免长对话截断后丢失（陪聊域不注入通道/订单锚点）
        key_anchor = []
        last_reply = (context.get("last_reply") or "").strip()
        if not _is_companion and last_reply and (
            "当前额度如下" in last_reply or ("EP" in last_reply and "100" in last_reply)
        ):
            key_anchor.append("上条回复已包含通道/额度说明，若用户追问可简短确认勿重复贴长段。")
        if context.get("image_ocr_text"):
            key_anchor.append("本会话含识图/凭证内容（见下方），订单回复请严格依据此信息。")
        if key_anchor:
            prompt_parts.append("【本会话关键信息】\n" + "\n".join(key_anchor))
        # 情绪感知写入 prompt：引导语气，不改变情绪增强器后处理逻辑
        emotion_hint = (context.get("user_emotion_hint") or "").strip().lower()
        _um = context.get("_current_user_message_for_lang") or context.get("last_message") or ""
        # #74（0830 实锤）：媒体轮的「[图片内容] 中文描述」是系统标注不是用户话语，
        # 直接喂语言检测会让本块**主动命令**AI 用中文回复外语客户（英文会话每次
        # 发图都被带偏成中文）。先剥系统注入行，只对客户自己的话（caption/语音
        # 转写正文）判语言；剥空（纯媒体轮）→ 回落会话级 reply_lang 决策（其证据
        # 链早已豁免媒体行），两者皆空才跳过注入——绝不拿描述语言冒充用户语言。
        try:
            from src.ai.lang_policy import strip_system_injected as _ssi
            _um_lang_src = _ssi(_um)
        except Exception:
            _um_lang_src = str(_um or "")
        if _um_lang_src.strip():
            lang_hint = self._detect_message_language(_um_lang_src)
        else:
            lang_hint = str(context.get("reply_lang") or "").strip()
        if lang_hint:
            from src.ai.language_rule import skip_output_lang_block
            if skip_output_lang_block(context):
                lang_hint = ""
        if lang_hint:
            lang_name = self._LANG_NAMES.get(lang_hint, lang_hint) or "中文"
            _channel = str(context.get("channel") or context.get("platform") or "").strip().lower()
            _chat_type = str(context.get("chat_type") or "").strip().lower()
            _strict_current_lang = (
                _is_companion
                or (_channel == "telegram" and _chat_type in ("", "private"))
            )
            if _strict_current_lang:
                prompt_parts.append(
                    f"【输出语言】用户当前消息语言为「{lang_name}」，你必须用该语言回复全部内容。"
                    f"即使系统提供的模板或知识库内容是中文，你也必须翻译为「{lang_name}」后再输出。"
                    "不要在同一条回复里混用其他语言；除非用户明确要求翻译或双语解释。"
                )
            else:
                if lang_hint != "zh":
                    prompt_parts.append(
                        f"【输出语言】用户当前消息语言为「{lang_name}」，你必须用该语言回复全部内容。"
                        f"即使系统提供的模板或知识库内容是中文，你也必须翻译为「{lang_name}」后再输出。"
                        f"术语如 EP/JC/EasyPaisa/JazzCash 等通道名保持原样不翻译。"
                    )

        if emotion_hint and emotion_hint != "neutral":
            emotion_guide = {
                "urgent": "用户语气偏着急，请先简短安抚并给出明确动作或时间点，避免空话。",
                "frustrated": "用户可能不满，先认同再说明处理进度，不要争辩。",
                "angry": "用户情绪激动，保持冷静礼貌，少emoji，多事实与解决方案。",
                "happy": "用户情绪积极，可顺势简短热情，但不要过度冗长。",
                "positive": "用户情绪偏积极，可顺势简短热情，但不要过度冗长。",
                "negative": "用户情绪偏消极或不满，先认同再说明处理进度，避免争辩与机械道歉堆砌。",
            }
            if emotion_hint in emotion_guide:
                prompt_parts.append("【用户情绪倾向】" + emotion_guide[emotion_hint])
            else:
                prompt_parts.append(f"【用户情绪倾向】粗判为 {emotion_hint}，回复语气可适当贴合。")
        # Telegram 侧上下文分析（近期消息主题/摘要），补全「聊天连贯」所需线索
        _ca = context.get("context_analysis")
        if isinstance(_ca, dict):
            _csum = (_ca.get("context_summary") or "").strip()
            _topic = (_ca.get("conversation_topic") or "").strip()
            if _csum and _csum != "无上下文消息":
                _line = _csum[:600]
                if _topic and _topic != "general":
                    _line = f"主题倾向: {_topic}。{_line}"
                prompt_parts.append(
                    "【近期聊天脉络（助手侧参考，请自然承接、勿复述标签）】\n" + _line
                )
        _rp = (context.get("_relationship_prompt_block") or "").strip()
        if _rp and _is_companion:
            prompt_parts.append(_rp)
        # W3-3M：漏斗阶段语气指令（跨域；与 _relationship_prompt_block 互补非替代）
        _fd = (context.get("_funnel_directive") or "").strip()
        if _fd and not (_rp and _is_companion):
            # companion 已注入完整关系块时不再重复；其他域正常追加
            prompt_parts.append(_fd)
        # Phase ②：关系成长「厚度/纪念点」感知（默认关，companion.bond_level.enabled）；
        # 与语气指令互补——只在 intimate/steady 或刚达成里程碑时给一句背景，让 AI 自然
        # 流露关系深度而非游戏化播报。克制由 build_bond_level_block 内部负责。
        _bl = (context.get("_bond_level_block") or "").strip()
        if _bl and _is_companion:
            prompt_parts.append(_bl)
        # Phase ③：剧情/场景 roleplay 导演指令（活动剧情时由 skill_manager 注入）
        _story = (context.get("_story_block") or "").strip()
        if _story and _is_companion:
            prompt_parts.append(_story)
        # 命理技能（companion.bazi）：命盘参考 / 生辰采集 directive（skill_manager 注入；
        # 开关与内容判定都在注入侧，这里有块即消费——与 _emotional_context_block 同模式）
        _bazi = (context.get("_bazi_block") or "").strip()
        if _bazi:
            prompt_parts.append(_bazi)
        # B50（实施64 P1-2）：已知画像硬事实 + 禁复问（skill_manager 每轮注入；
        # 关键词召回挑不中的恒真身份槽在这里兜底，有块即消费）
        _known_prof = (context.get("_known_profile_block") or "").strip()
        if _known_prof:
            prompt_parts.append(_known_prof)
        _player_data = (context.get("_player_data_block") or "").strip()
        if _player_data:
            prompt_parts.append(_player_data)
        # B52（实施64 P1-2）：人设自述近况衔接（要睡了/去健身…TTL 窗内有块即消费）
        _self_state = (context.get("_self_state_block") or "").strip()
        if _self_state:
            prompt_parts.append(_self_state)
        # 报障群值守（bug_intake）：分类/工单上下文块（skill_manager 注入；
        # 开关与群白名单判定全在注入侧，这里有块即消费——与 _bazi_block 同模式）
        _bug_intake = (context.get("_bug_intake_block") or "").strip()
        if _bug_intake:
            prompt_parts.append(_bug_intake)
        # 引用上下文（2026-08-20 内测实锤「引用+『分析』」）：对方引用某条消息后
        # 发言，被引用内容注进提示（telegram_client 恒写键，空串=本轮无引用）。
        # 刻意走提示位不并正文——正文会进记忆抽取，引用的 AI 旧话会被接地成
        # 「用户说过」（Phase8 幻觉家族）。
        _quoted = (context.get("_quoted_note") or "").strip()
        if _quoted:
            prompt_parts.append(_quoted)
        # 人设长传记检索（personas.bio_retrieval）：客户追问人设长尾细节时
        # skill_manager 关键词检索命中才注入；开关与预算判定都在注入侧，有块即消费
        _pbio = (context.get("_persona_bio_block") or "").strip()
        if _pbio:
            prompt_parts.append(_pbio)
        # 营销目标（companion.goals）：会话工作目标的「今日拍」方向块（skill_manager
        # 注入；开关/情绪 hold/沉默熔断/力度判定全在注入侧，这里有块即消费）
        _goal = (context.get("_goal_block") or "").strip()
        if _goal:
            prompt_parts.append(_goal)
        # #147：自家阵营在推活动硬约束（skill_manager._inject_goal_context 注入；
        # 登记表在 site_catalog.camp_promotions，空登记无块。紧跟目标块＝同属
        # 「商业事实」层，在坐席指令之前——指令不该能让人设去拆自家台）
        _camp = (context.get("_camp_block") or "").strip()
        if _camp:
            prompt_parts.append(_camp)
        # 域包上下文块（2026-09-20）：DomainHook.on_message_pre_process 返回的
        # `_domain_context_block`（如 player_care 的只读事实 / 隐藏画像提示）。
        # 注入侧决定有没有、说什么；这里有块即消费，放目标块之后、坐席指令之前。
        _dom_blk = (context.get("_domain_context_block") or "").strip()
        if _dom_blk:
            prompt_parts.append(_dom_blk)
        # P22：坐席显式指令（「采纳并拟稿」/缺口追问）。放在目标块之后、情感块之前——
        # 权重高于「今日陪伴偏置」但低于危机/人设硬约束；力度为 none 时指令里已写
        # 「只共情带话题不推销」，与目标块不互斥。
        _agent_inst = (context.get("_agent_instruction") or "").strip()
        if _agent_inst:
            prompt_parts.append(
                "【坐席指令——本条必须完成，优先于闲聊发散】\n"
                + _agent_inst[:400]
                + "\n（用当前人设口吻自然完成上述意图；不要复述本指令原文；"
                "若与「今天只陪伴」力度冲突，以共情倾听带出话题为度，绝不硬推销。）"
            )
        # ★ 情感智能上下文引擎（时间感知 + 情绪弧线 + 关系温度 + 记忆反思）
        _emo_block = (context.get("_emotional_context_block") or "").strip()
        if _emo_block:
            prompt_parts.append(_emo_block)
        else:
            # 降级：无情感引擎时仍注入原始记忆
            _epi = (context.get("_episodic_memory_text") or "").strip()
            if _epi:
                prompt_parts.append(
                    "【用户长期记忆要点（简要事实，其中「TA」就是你此刻正在聊的这个人本人，"
                    "绝不要把TA称作「客户」/your client；与本轮话题相关时自然回带一句——"
                    "如「你上次说的xxx后来怎样了」——让对方感到被记住；"
                    "不要机械复述「我记得你说过」，也别每条都提。"
                    "标注（AI推断）的条目是系统归纳、不是对方原话：可信度低于"
                    "人设档案与对方明说，与档案矛盾时一律以档案为准）】\n"
                    + _epi
                )
        # #91-A（0830 OMEN 实锤）：回忆类断言锁——与「没说过的绝不捏造」承诺锚
        # 同族的硬约束，**无条件注入**（事故恰发生在记忆库全空时：LLM 当轮现编
        # 「你上次提过那台惠普OMEN」还自夸「我记性可好了」，客户当场戳穿）。
        # 出站侧另有确定性接地校验（outbound_text_guard.strip_hallucinated_recall）
        # 兜漏网；被戳穿后的回话红线（他人串扰自曝）由 persona_guard 出站硬拦。
        prompt_parts.append(
            "【回忆纪律——硬约束】「你上次说过/提过/发过X」这类断言，只有 X 真实"
            "出现在上面的历史消息或记忆要点里才允许说；对不上原话的一律不说，"
            "绝不现编对方提过的内容，更不许配上「我记性可好了」这类自夸。"
            "记不清就自然地问，绝不假装记得。"
        )
        _slo = (context.get("_slow_think_outline") or "").strip()
        if _slo:
            prompt_parts.append(
                "【慢思考规划（内部策略，请自然融入回复，勿逐条复读）】\n" + _slo[:2800]
            )

        # 添加意图信息
        intent = context.get('intent')
        if intent:
            prompt_parts.append(f"用户意图: {intent}")

        # 添加上一条用户消息（便于理解「之前说过/给过」等）
        last_message = context.get('last_message')
        if last_message:
            prompt_parts.append(f"用户本条消息（当前轮）: {last_message}")

        topic_switch = context.get('_topic_switch_hint')
        if topic_switch:
            prompt_parts.append(f"【话题/语境切换——注意】\n{topic_switch}")

        # P3-2：群聊场景约束（群窗才有该键；私聊窗永不出现）
        _group_hint = (context.get('_group_chat_hint') or '').strip()
        if _group_hint:
            prompt_parts.append(f"【群聊场景——重要】\n{_group_hint}")

        # 生成层口语分叉（Phase G）：本条回复会走语音条 → 让 LLM 同一次调用多产
        # 一个 [口语版] 段（书面版进镜像/记忆，口语版直接送 TTS）。门控/剥离/暂存
        # 全在 spoken_variant 模块；这里只在被请求时追加指令。
        if context.get("_spoken_variant_request"):
            try:
                from src.ai.spoken_variant import (
                    build_spoken_variant_instruction,
                    want_disfluency,
                )
                _sv_cfg = (self.config.config
                           if (self.config and getattr(self.config, "config", None))
                           else {})
                _sv_intensity = str(
                    (((_sv_cfg.get("avatar_voice") or {}).get("colloquial") or {})
                     .get("rewrite_intensity", "natural")) or "natural")
                prompt_parts.append(build_spoken_variant_instruction(
                    disfluency=want_disfluency(
                        _sv_cfg,
                        str(context.get("_current_user_message_for_lang") or "")),
                    intensity=_sv_intensity))
            except Exception:
                pass

        # 唱歌协同提示（实施58 P1，与下方发图协同同哲学）：对方在要歌时上游
        # 判定投递语义（会真唱=本稿只是失败兜底 / 发不出=禁文字假唱与承诺），
        # 从源头避免 LLM 用文字打歌词冒充唱或空头「我唱给你听」。
        _song_hint = (context.get("_song_coherence_hint") or "").strip()
        if _song_hint:
            prompt_parts.append("【唱歌协同——重要】\n" + _song_hint)

        # 媒体协同提示（文图一致性）：上游判定「对方在要图但这轮发不出」或
        # 「草稿只是照片失败时的兜底文本」时注入——从源头避免 LLM 写出
        # 「等我拍/照片来了」这类与实际发送状态矛盾的话（承诺守卫是最后防线，
        # 这里是第一防线）。
        _media_hint = (context.get("_media_coherence_hint") or "").strip()
        # 悬置常驻提示（实施69）：客户在等一张还没到的图（要图/被承诺过 1-N 轮前）
        # → 每轮都提醒别承诺/别称已发。独立键，**不**带 [PHOTO 禁令、不阻断
        # Stage/指令发图——本轮系统真发出图时它说的「只有真发出才带图」依然成立。
        _pending_hint = (context.get("_media_pending_hint") or "").strip()
        if _media_hint:
            # 上游判定「这轮发不出图」→ 除别承诺外，还要禁 [PHOTO 标记（llm/hybrid
            # 模式下 LLM 可能仍打标记 → 执行层虽有闸门二次拦截，但源头禁掉最干净）。
            _deny = ""
            if self._photo_intent_mode() != "keyword":
                from src.ai.photo_directive import build_photo_deny_line
                _deny = build_photo_deny_line()
            prompt_parts.append("【发图协同——重要】\n" + _media_hint + _deny)
        else:
            if _pending_hint:
                # 与下方发图协议/能力声明**共存**（不互斥）：协议管「怎么真发」，
                # 悬置提示管「没发出去之前嘴上别越界」。
                prompt_parts.append("【发图协同——重要】\n" + _pending_hint)
            if _is_companion:
                # 发图能力有效性（photo_capability SSOT，2026-07-31）：全局 selfie
                # 开**且当前人设 capabilities.photos 开（默认关）** 才注入发图协议/
                # 边界声明；能力关 → 注入「无发图能力」硬约束。修旧逻辑倒挂——旧
                # 代码在 selfie 未启用时什么都不注入，而最需要「别承诺发图」约束的
                # 恰是发不了图的形态（试聊实录：「我翻翻手机相册哈」连环空头支票）。
                from src.companion.photo_capability import (
                    no_photo_constraint,
                    persona_photos_enabled,
                    resolve_prompt_persona,
                )
                _persona_photos_on = persona_photos_enabled(
                    resolve_prompt_persona(context))
                if self._media_capability_hint_enabled() and _persona_photos_on:
                    if self._photo_intent_mode() != "keyword":
                        # 主动决策协议（2026-07-14 决策权上移）：LLM 读完整上下文自行
                        # 判断要不要发图 + 发什么场景，正文末行 [PHOTO …] 标记声明；
                        # 系统解析后真出图（PuLID 锁脸）。根治关键词打地鼠。
                        from src.ai.photo_directive import (
                            build_photo_protocol_prompt,
                        )
                        prompt_parts.append(build_photo_protocol_prompt())
                    else:
                        # keyword 回退模式：旧式被动声明（发图判定完全依赖入站关键词）。
                        prompt_parts.append(
                            "【媒体能力边界】需要发照片/语音时由系统自动完成真实发送，"
                            "你专注文字聊天：不要主动写「我发照片给你」「等我拍一张」"
                            "「我发条语音」这类承诺；**更不要谎称「已经发了」「发过去了」"
                            "「发到群里了」「你看看这张」**——你的文字里没有真的附带照片，"
                            "这样说对方收不到会觉得你在骗人；对方要照片时自然回应即可"
                            "（系统会处理），也不要否认你能拍照。")
                elif self._capability_hint_allowed():
                    # 能力关闭（全局 selfie 关 或 人设开关关/无人设）：注入硬边界。
                    # 人设关时人设块已带一句短约束（persona_manager 反向消费），这里
                    # 的详细版并存＝防御纵深（persona_block_detail=none 部署也有兜底）。
                    prompt_parts.append(no_photo_constraint())

        # 语音能力（2026-07-20）：开了自动语音（inbox.l2_autosend.voice.enabled）时，明确告诉
        # AI 它能发语音——系统会把回复转成人设声音发出。否则拟人人设会自作主张编「我发不了语音/
        # 嗓子哑/不方便」的借口，与「系统实际发出了语音条」自相矛盾（本轮实测碰到）。
        # 仅陪伴人设 + 语音开关开时注入；发不发语音仍由 decide_voice 决定，这里只消除「否认能力」。
        try:
            _vcfg2 = (
                (((self.config.config or {}).get("inbox") or {}).get("l2_autosend") or {}).get("voice") or {}
            ) if (self.config and getattr(self.config, "config", None)) else {}
        except Exception:
            _vcfg2 = {}
        if _is_companion and _vcfg2.get("enabled"):
            prompt_parts.append(
                "【语音能力·须遵守】你能发语音消息：系统会自动把你要说的话用你的声音发出。"
                "对方要你发语音时，你就自然地正常回答（内容照说就行），系统会转成语音——"
                "绝不要说「我发不了语音」「嗓子哑」「不方便发语音」「语音功能坏了」这类借口，"
                "也不要主动写「我发条语音给你」这种承诺（系统会自动处理）。"
                "对方要你唱歌时，别用打字发歌词冒充唱歌（对方一眼看穿），"
                "要么就当作真的唱出来说一句（系统转成语音），要么俏皮地婉拒。")

        # 当前真实时间（Phase19 时间一致性）：LLM 训练数据里没有"现在几点"，
        # 不注入就会深夜说"下午好"、凌晨发正午场景标记。仅陪伴人设注入
        # （客服域时间敏感话术由业务模板负责，不吃这块 token）。
        # 优先用人设当地时钟（skill_manager / deep_persona 路径写入 context）。
        if _is_companion:
            _local_now = context.get("_persona_local_now")
            _place_lbl = str(context.get("_persona_place_label") or "").strip()
            if not isinstance(_local_now, __import__("datetime").datetime):
                _local_now = None
                # 兜底：若上游未注入，尝试从 context 里已挂的人设再算一次
                try:
                    _p_fb = context.get("_resolved_persona") or context.get("persona")
                    if isinstance(_p_fb, dict):
                        from src.companion.persona_location import (
                            resolve_place_with_fallback,
                            persona_now as _p_now,
                            local_time_line as _lt_line,
                            time_gap_line as _gap_line,
                        )
                        _pl = resolve_place_with_fallback(_p_fb)
                        if _pl is not None:
                            _local_now = _p_now(_pl)
                            _place_lbl = _pl.display("zh")
                            if not context.get("_persona_local_time_line"):
                                context["_persona_local_time_line"] = _lt_line(
                                    _pl, "zh", _local_now)
                            if not context.get("_persona_time_gap_line"):
                                _g = _gap_line(_pl, "zh", _local_now)
                                if _g:
                                    context["_persona_time_gap_line"] = _g
                except Exception:
                    _local_now = None
            prompt_parts.append(
                build_time_context_line(_local_now, place_label=_place_lbl))
            _loc_line = (context.get("_persona_local_time_line") or "").strip()
            if _loc_line:
                prompt_parts.append("【人设当地时间】" + _loc_line)
            # 时空钉（与 local_time_line 互补）：有当地小时则再钉一句，防滑回 UTC+8
            try:
                from src.companion.world_clock_guard import world_clock_prompt_nail
                from src.companion.persona_location import resolve_place_with_fallback
                _p_nail = context.get("_resolved_persona") or context.get("persona")
                _pl_nail = resolve_place_with_fallback(_p_nail) if isinstance(
                    _p_nail, dict) else None
                _h_nail = (
                    int(_local_now.hour)
                    if isinstance(_local_now, __import__("datetime").datetime)
                    else -1)
                _nail = world_clock_prompt_nail(_pl_nail, _h_nail) if _h_nail >= 0 else ""
                if _nail:
                    prompt_parts.append(_nail)
            except Exception:
                pass
            _gap_line_txt = (context.get("_persona_time_gap_line") or "").strip()
            if _gap_line_txt:
                prompt_parts.append(_gap_line_txt)
            _wx_note = (context.get("_persona_weather_note") or "").strip()
            if _wx_note:
                prompt_parts.append(_wx_note)
            # #82（0830 两例实锤）：位置身份钉子——档案居住地此前只驱动时钟/
            # 天气，从不作为「你在哪」的事实进 prompt，AI 凭空自称「马尼拉的
            # 雨天早晨」（档案=薄荷岛）。位置类自述只许取档案（基线）或会话中
            # 亲口说过的临时行程（覆盖层，由「你刚说过」travel 锚点承载，到期
            # 自然回归）；未接真实天气数据时禁断言具体天气现象——「下雨」会
            # 引来「拍雨景照」索图连环穿帮（本例实录）。
            _place_pin = (context.get("_persona_place_label") or "").strip()
            if _place_pin:
                _pin_txt = (
                    f"【你的位置】你现居/常驻：{_place_pin}。位置类自述只按此说，"
                    "绝不自称身在其他城市——除非你在本会话里亲口说过临时行程"
                    "（出差/旅行，见「你刚说过」），行程期间按它叙事、到期回归。"
                )
                if not _wx_note:
                    _pin_txt += (
                        "你没有当地实时天气数据：不要主动断言正在下雨/下雪/"
                        "台风等具体天气现象（同城客户当场能对出破绽），"
                        "要聊天气只用「有点闷/天气还不错」这类模糊说法。"
                    )
                prompt_parts.append(_pin_txt)
            # P2 用户侧在地化：对方当地时间 + 对方那边的节日（skill_manager 注入，
            # 只吃显式信号——见 `_inject_peer_locale` 的 docstring）。有了这两块，
            # 「对方那边几点、今天是不是 TA 的节日」不再靠 LLM 瞎猜。
            _peer_place = (context.get("_peer_place_line") or "").strip()
            if _peer_place:
                prompt_parts.append(_peer_place)
            _peer_clk = (context.get("_peer_clock_line") or "").strip()
            if _peer_clk:
                prompt_parts.append(_peer_clk)
                # 时差桥（2026-08-02 WA 实录修复）：两侧钟都可信且明显错位时，
                # 钉死「自己的状态按自己的钟说」并鼓励自然点破时差——否则 LLM
                # 会在两个都正确的时间框架间随机横跳（上一句 Saturday evening、
                # 下一句借客户的 Sunday 说自己），读者视角=自相矛盾。
                try:
                    from src.companion.user_clock import build_tz_bridge_line
                    _bridge = build_tz_bridge_line(
                        _local_now, context.get("_peer_local_now"))
                    if _bridge:
                        prompt_parts.append(_bridge)
                except Exception:
                    pass
            _peer_hol = (context.get("_peer_holiday_note") or "").strip()
            if _peer_hol:
                prompt_parts.append(_peer_hol)
            # 人设侧节日：算在这里而不是 skill_manager——人设与人设本地钟此处已在手，
            # 且 holidays_on 是带缓存的纯函数（零 IO）。语义是**生活纹理**而非群发问候：
            # 「你所在地今天过节」让人设的日常自洽（街上气氛/店铺关门），不催它去祝贺。
            try:
                _hcfg = (
                    ((self.config.config or {}).get("companion") or {})
                    .get("locale_holidays") or {}
                ) if self.config else {}
                if (isinstance(_hcfg, dict) and _hcfg.get("enabled")
                        and _hcfg.get("persona_side_texture", True)):
                    _p_for_hol = (context.get("_resolved_persona")
                                  or context.get("persona"))
                    if isinstance(_p_for_hol, dict):
                        from src.companion.locale_holidays import (
                            holiday_fact_line as _hol_line,
                            holidays_on as _hol_on,
                        )
                        from src.companion.persona_location import (
                            resolve_place_with_fallback as _rp_place,
                        )
                        _pl_h = _rp_place(_p_for_hol)
                        _ctry_h = str(getattr(_pl_h, "country", "") or "").strip()
                        if _ctry_h:
                            _day_h = (_local_now or __import__(
                                "datetime").datetime.now()).date()
                            _pn = _hol_line(
                                _hol_on(_day_h, _ctry_h), "zh", side="persona")
                            if _pn:
                                prompt_parts.append(_pn)
            except Exception:
                pass

        # 场景状态（Phase18 图文同源）：聊天文本与生图共用的「AI 此刻在哪」——
        # 文本围绕它说话、自拍在它里面拍，从源头消灭"说上班发海边图"打脸。
        _scene_note = (context.get("_current_scene_note") or "").strip()
        if _scene_note and _is_companion:
            prompt_parts.append(_scene_note)
        # #208（L-1 C）：历史里 AI 自己说过的近况（天气/行程/正在做的事）若无来源，
        # 标为「随口一说」——不延续、不展开、不追加细节。FACT_LOCK 只禁改口，这里只禁
        # 把编造当事实滚雪球（FTK6S7：编造进 _conversation_history 后被反复提）。
        if _is_companion:
            try:
                from src.utils.proactive_fabrication_guard import history_status_claim_note
                _hs_note = history_status_claim_note(context)
                if _hs_note:
                    prompt_parts.append(_hs_note)
            except Exception:
                pass
        # 已发媒体日志（防"我没发过照片"失忆抵赖 +「上次那张」指涉可答）。
        _media_sent = (context.get("_media_sent_note") or "").strip()
        if _media_sent and _is_companion:
            prompt_parts.append(_media_sent)

        _short_in = (context.get("_inbound_short_hint") or "").strip()
        if _short_in and _is_companion:
            prompt_parts.append(_short_in)

        if context.get("_channel_followup_brief") and not _is_companion:
            prompt_parts.append(
                "【追问简短回复 —— 最高优先级】\n"
                "用户刚看过你上一条关于通道成功率/状态的回复；本条是短追问或确认。\n"
                "禁止复述上一条里各通道成功率数字与「都正常/都可用」等整段；用一两句话即可："
                "可答「和刚才一致」、或只回应追问点（例如问波动则只谈波动与建议）。"
            )

        # ★ 重复消息检测（runner 层标记）：用户发了跟上次一模一样的消息
        _is_repeated_msg = bool(context.get("_is_repeated_message"))
        _prev_reply_for_rpt = (context.get("_prev_reply_for_repeat") or "").strip()
        if _is_repeated_msg and _prev_reply_for_rpt:
            prompt_parts.append(
                "【重要：对方刚才发了一条跟上次一模一样的消息】\n"
                f"你上次的回复是：「{_prev_reply_for_rpt[:200]}」\n\n"
                "真人收到相同消息时的自然反应（任选其一，按情境决定）：\n"
                "1. 轻松调侃：「哈哈你又问～」「怎么又问这个啦」\n"
                "2. 确认意图：「嗯？上次不是说了吗，是没看到还是想再确认一下？」\n"
                "3. 换个角度补充上次没说的细节\n"
                "4. 如果对方可能在测试你是不是 AI：回答要有个性、有情绪波动，"
                "绝对不要给出和上次结构相似的回复\n\n"
                "【硬性要求】\n"
                "- 禁止复制粘贴上次的回复内容\n"
                "- 禁止使用和上次相同的开头\n"
                "- 回复要体现你注意到了「又问了一遍」这件事\n"
                "- 事实必须与上次一致：上次说「还没吃」这次绝不能变成「吃过了」，"
                "拿不准的状态就不要提\n"
                "- 保持你的人设和语气自然"
            )

        last_reply = context.get('last_reply')
        if last_reply:
            anti_repeat = context.get('_anti_repeat_hint')
            if anti_repeat:
                _who = "你不是复读机，要像真人一样自然换个说法。" if _is_companion else "你是一个真人客服，不是复读机。"
                prompt_parts.append(
                    f"【角度切换指令——必须遵守】\n"
                    f"你上一条回复是：「{last_reply[:200]}」\n"
                    f"用户又问了类似问题。{_who}\n"
                    f"具体要求：{anti_repeat}\n"
                    f"禁止与上条回复使用相同的开头词和句式。\n"
                    f"{FACT_LOCK_LINE}"
                )
            elif not _is_repeated_msg:
                prompt_parts.append(f"上次回复: {last_reply}")

        # 添加对话阶段
        stage = context.get('stage')
        if stage:
            prompt_parts.append(f"对话阶段: {stage}")

        # 用户刚发的图片/截图内容（Vision 或 OCR），仅根据此真实内容回复
        image_ocr_text = context.get('image_ocr_text')
        if image_ocr_text:
            # #74：描述是系统自动标注（恒为中文），显式声明「非对方话语、勿跟随
            # 其语言」——外语会话发图后回复漂中文的第二道钉子（第一道在【输出语言】
            # 块的媒体行剥离）。
            prompt_parts.append(
                f"用户刚发的图片/截图内容（Vision/OCR，系统自动标注，非对方原话）:\n"
                f"{image_ocr_text[:2000]}\n"
                "（以上描述仅供你理解画面；它的语言不代表对方的语言，回复语言"
                "一律按【输出语言】/LANGUAGE RULE 执行）")

        # 近期群内机器人/通知消息（支付域可参考订单/通道；陪聊域不注入以免模型接工作话）
        recent_bot = context.get('recent_bot_messages')
        if recent_bot and not _is_companion:
            lines = []
            for item in (recent_bot[:15] if isinstance(recent_bot, list) else []):
                who = item.get('from', '') if isinstance(item, dict) else ''
                txt = item.get('text', '') if isinstance(item, dict) else str(item)
                if txt:
                    lines.append(f"[{who}]: {txt[:600]}")
            if lines:
                prompt_parts.append("近期群内机器人/通知消息（可参考）:\n" + "\n".join(lines))

        # 通道实时状态（仅业务域；陪聊域不注入）
        channel_status = ("" if _is_companion else context.get("channel_status_info", "") or "").strip()
        _live_metrics = bool(context.get("_channel_metrics_live_only")) and not _is_companion
        if channel_status:
            _fee_block = ""
            if _live_metrics:
                _fee_block = (
                    "- 本条为成功率或「费率/手续费」类咨询：只使用上方数据中的**状态**与**成功率百分比**作答；"
                    "**禁止**说出任何手续费/费率的具体数值或比例（含 x%、千分之几）；"
                    "若用户追问费率，引导联系**业务主管**或**人工客服**对接；"
                    "禁止引导去商户后台查费率、禁止说「客服无权限查费率」类话术。\n"
                    "- 若用户一句话里同时提到成功率和费率：只回答成功率与各通道状态；费率不报价。\n"
                )
            else:
                _fee_block = (
                    "- 客户问单笔限额/额度时，用上面的「单笔限额」数值；"
                    "对话中**禁止**报手续费/费率的具体数值或比例；费率请咨询业务主管或人工客服。\n"
                )
            _tail_kb = (
                "本类咨询**不要**引用知识库中含具体费率数字的话术；通道状态与成功率以上方实时数据为准；"
                "手续费/费率数值不对客宣读。"
                if _live_metrics
                else "知识库模板仅供话术风格参考，通道的具体状态必须以上面的实时数据为准；"
                "勿在回复中写出具体费率数值或比例。"
            )
            prompt_parts.append(
                f"【当前通道实时数据 ★★★ 唯一数据源 ★★★】\n{channel_status}\n"
                f"⚠️⚠️⚠️ 以下规则必须严格遵守：\n"
                f"- 回复中的所有数字（成功率、限额等）必须且只能来自上方实时数据，禁止使用其他任何来源的数字\n"
                f"- 如果代收和代付的成功率不同，必须分别列出，不能只报一个笼统数字\n"
                f"- **禁止**在回复中出现「PIX」「pix」或巴西 PIX 相关内容（该业务已永久下架，对客视同不存在）。\n"
                f"- 每个通道的状态以上面的实时数据为准，忽略知识库示例中的旧状态描述\n"
                f"- 只能提及上面列出的可用通道，不要编造不存在的通道信息\n"
                f"- 如果上面列出了'已禁用通道'，当客户问到这些通道时，回复'该通道目前已下线/不可用'，不要说它正常\n"
                f"- 状态=正常 → 通道正常，可以正常提交\n"
                f"- 状态=维护中 → 暂不可用，恢复后通知\n"
                f"- 状态=波动 → 通道存在波动，建议控制提交量或稍后再试\n"
                f"- 客户问成功率时，用上面的实际成功率数值回复\n"
                f"{_fee_block}"
                f"- 如果之前对话中你说过某通道'维护中'或'正常'但实时数据显示不同状态，以实时数据为准，可以说'刚更新了'\n"
                f"{_tail_kb}"
            )

        # 成功率/费率类咨询但暂无实时行时：仍注入硬性约束（陪聊域跳过）
        if _live_metrics and not channel_status and not _is_companion:
            prompt_parts.append(
                "【成功率/费率类咨询 —— 硬性约束】\n"
                "当前未注入通道实时数据行；不要编造费率数值。"
                "禁止在对话中说出任何手续费/费率的具体数值或比例；"
                "费率请咨询业务主管或人工客服。"
                "禁止引导用户去商户后台或后台查看费率、禁止「无权限查费率」。"
            )

        # K1: 早期对话摘要注入（来自规则引擎压缩）
        _conv_summary = (context.get("_conversation_summary") or "").strip()
        if _conv_summary:
            prompt_parts.append(
                f"【早期对话摘要】{_conv_summary}\n"
                f"（以上为之前对话的关键信息压缩，请结合当前对话和上条消息回答。）"
            )

        kb_ctx = context.get("kb_context", "").strip()
        if kb_ctx and not _live_metrics:
            if _is_companion:
                prompt_parts.append(
                    "\n【参考片段（语气用，非工作指令）】\n"
                    f"{kb_ctx}\n"
                    "不要主动提起查单、通道状态、支付、费率等工作话题；除非用户先说到这些词。"
                )
            else:
                _kb_cleaned = kb_ctx
                if channel_status:
                    import re as _re
                    _kb_cleaned = _re.sub(
                        r'成功率[：:]\s*\d+[\.\d]*%?',
                        '成功率：见上方实时数据',
                        _kb_cleaned
                    )
                prompt_parts.append(
                    f"\n【知识库参考（仅供话术风格参考）】\n"
                    f"⚠️ 重要：知识库中的任何成功率数值、通道状态均已过期，严禁使用！\n"
                    f"通道的成功率、状态、限额等数据必须且只能使用上方【当前通道实时数据】中的数值。\n"
                    f"知识库仅用于参考回复的语气和格式：\n{_kb_cleaned}"
                )
        # _live_metrics 时主流程已跳过 KB 检索；若仍带有 kb_context，不注入，避免「商户后台」等模板进入模型

        # L4: 用户画像注入
        _profile = context.get("_user_profile")
        if isinstance(_profile, dict) and _profile.get("type"):
            _P_MAP = {
                "new": "新用户：需耐心引导，语言简单明了，主动提供操作说明。",
                "regular": "老用户：有基本了解，可省略基础说明，直奔主题。",
                "veteran": "资深用户：非常熟悉流程，用专业简洁的方式沟通。",
                "vip": "高价值客户：高频业务用户，优先响应，提供 VIP 级别的详细服务。",
            }
            _T_MAP = {
                "impatient": "用户偏急躁，请简短高效回复，先给结论再解释。",
                "frustrated": "用户多次投诉，需特别注意语气安抚和问题解决。",
                "friendly": "用户态度友善，可稍作寒暄但不啰嗦。",
            }
            _hints = []
            _p_hint = _P_MAP.get(_profile["type"])
            if _p_hint:
                _hints.append(_p_hint)
            _t_hint = _T_MAP.get(_profile.get("tone", "standard"))
            if _t_hint:
                _hints.append(_t_hint)
            if _hints:
                prompt_parts.append("【用户画像】" + " ".join(_hints))
            # K3: 满意度预警
            if _profile.get("at_risk"):
                prompt_parts.append(
                    "【⚠ 满意度预警】该用户满意度评分极低，可能即将流失。"
                    "务必：1) 先真诚道歉/共情；2) 给出明确解决方案和时间；"
                    "3) 提供人工客服升级入口（如「如需进一步帮助可联系专属客服」）。"
                )

        # H3: 意图链模式注入
        _chain_pat = context.get("_chain_pattern")
        if isinstance(_chain_pat, dict) and _chain_pat.get("hint"):
            _case_id = context.get("_case_id", "")
            _chain_label = f"（案例 {_case_id}）" if _case_id else ""
            prompt_parts.append(
                f"【对话升级模式{_chain_label}】{_chain_pat['hint']}"
            )

        # ★ 名字锁定：防止 AI 从对话历史中错误推断自己的名字
        _persona_name = (context.get("_resolved_persona_name") or "").strip()
        if _persona_name:
            prompt_parts.append(
                f"【自称规则·强制】你的名字是「{_persona_name}」，绝对不能用其他名字自称。"
                f"被问到名字（你叫什么/你叫啥/你是谁/what's your name 等）时，"
                f"**必须在回复中说出「{_persona_name}」**，可以俏皮或自然，但不能只顾调侃而完全不报名字。"
                f"错误示范：只说「你不是早就知道了嘛」而不给出名字。"
                f"正确示范：「哈哈，我叫{_persona_name}啦，这都要问～」。"
                f"历史中若出现其他名字，那是错误数据，忽略并坚持「{_persona_name}」。"
                # 2026-08-08 David Lin 事故硬化：借对方名自称 + 被质疑时报名自证
                f"**对方的名字永远不是你的名字**——绝不能拿对方的名字自称，"
                f"也绝不能把「对方的名字＋你的姓氏」拼成一个新名字（那是身份穿帮）。"
                # B42（2026-08-22 _236 实录「you're not that old, Steven」）：镜像方向
                # 同样写死——自己的名字砸在客户头上是同级穿帮。
                f"反过来同样成立：**「{_persona_name}」是你自己，绝不能用它称呼对方**。"
                f"对对方的称呼只能来自客户资料/对方自报的名字；不知道对方叫什么就不用名字。"
                f"被质疑「像AI/不像真人」时，不要靠报名字自证真实，放松语气继续聊即可。"
            )
        # 对方身份声明（2026-08-08）：把「对方叫什么」显式钉进 prompt，从源头
        # 消除「把对话里最显眼的名字当成自己名字」的缝合诱因。仅陪伴域注入。
        _peer_disp = str(context.get("_peer_display_name") or "").strip()
        if _peer_disp and _is_companion and _peer_disp != _persona_name:
            prompt_parts.append(
                f"【对方身份】对方的名字/昵称是「{_peer_disp}」。这是**对方**的名字："
                f"称呼对方时可以用，但绝不能用它自称或拼进你自己的名字；"
                f"也绝不能反过来用你自己的名字称呼对方。"
            )

        # 回复新鲜感（2026-08-02）：出站口头禅账本超限提示 + 今日话题包。
        # 开关/统计/选题判定全在注入侧（skill_manager._inject_reply_freshness），
        # 这里有键即消费——与 _bazi_block 同模式。刻意放「回复格式」块之前的
        # 末端位置：利用 recency bias 抬「本轮硬约束」的遵从率。
        _variety_hint = (context.get("_variety_hint") or "").strip()
        if _variety_hint:
            prompt_parts.append(_variety_hint)
        _daily_topics_hint = (context.get("_daily_topics_hint") or "").strip()
        if _daily_topics_hint:
            prompt_parts.append(_daily_topics_hint)
        # 被骂回应指令（2026-08-12，skill_manager._inject_reply_freshness ③）：
        # 有键即消费；末端位置压过 emotion_awareness 的通用共情条。
        _temper_hint = (context.get("_temper_hint") or "").strip()
        if _temper_hint:
            prompt_parts.append(_temper_hint)

        # ★ 回复格式合同——与投递能力对齐（2026-08-08 重构，运营拍板）：
        #   bubbles 开 → 多行合同（投递层真的按行拆独立消息，行数与 max_parts 对齐）；
        #   bubbles 关 → **一段话合同**——旧版无条件注多行合同，产物整段一条发出，
        #   呈现为固定「段1+空行+段2」节奏，客户实锤质疑「why your messages always
        #   2 parts / Look like ai」。合同注入什么，出口就 enforce 什么
        #   （_shape_single_paragraph 硬护栏，与本判据共用 _single_paragraph_mode）。
        _channel = (context.get("channel") or "").lower()
        if _is_companion or _channel in ("whatsapp_rpa", "messenger_rpa", "line_rpa"):
            _bubbles_on = False
            _max_lines = 3
            try:
                from src.inbox.reply_split import parse_bubbles_cfg as _pbc_fmt
                _bc = _pbc_fmt(_cfg_ctx if isinstance(_cfg_ctx, dict) else {})
                _bubbles_on = bool(_bc.get("enabled"))
                if _bubbles_on:
                    _max_lines = max(2, min(5, int(_bc.get("max_parts") or 3)))
            except Exception:
                _bubbles_on = False
            if _bubbles_on:
                prompt_parts.append(
                    "【回复格式——像真人发消息（最高优先级）】\n"
                    "你在用手机聊天，不是写文章。每行会被拆成独立消息发出去。\n"
                    "规则：\n"
                    "- 每行 1 句话，10-30 字。用真正的换行隔开（不是写 \\n 字符），"
                    "行与行之间**不要留空行**。\n"
                    f"- 一次回复 1-{_max_lines} 行就够了，不要超过 {_max_lines} 行。\n"
                    "- 行数要自然变化：简短回应就 1 行，有内容才多行——"
                    "别每条回复都固定拆成同样的行数。\n"
                    "- emoji 最多 1/3 的行带 emoji，每行最多 1 个。\n"
                    "- 想单独发表情就独占一行，放 1-3 个相同 emoji。\n"
                    "- 语气词（嗯、哈哈、诶）可以单独一行。\n"
                    "- 只有情绪很强烈时才写长句（超过 40 字）。\n"
                    "- 绝对禁止把所有话挤在一段里！\n\n"
                    "✅ 好的示例：\n"
                    "哈哈真的假的\n"
                    "我还以为你忘了呢\n"
                    "😂\n\n"
                    "❌ 坏的示例：\n"
                    "哈哈真的假的？我还以为你忘了呢！那你后来怎么处理的呀？我之前也遇到过类似情况好麻烦😂"
                )
            else:
                prompt_parts.append(
                    "【回复格式——一段话（最高优先级）】\n"
                    "整条回复必须写成**一个段落**：绝对不要换行、不要空行、不要分成两段，"
                    "也不要列点/编号。像真人随手打一条消息：1-3 个短句连着说完就停。\n"
                    "节奏别固定：不必每条都「先回应再补一句」，也不要每条都用问句收尾。\n"
                    "❌ 坏的示例（分成两段）：\n"
                    "哈哈真的假的\n\n那你后来怎么处理的呀？\n"
                    "✅ 好的示例（一段话）：\n"
                    "哈哈真的假的，那你后来怎么处理的呀？"
                )

        if prompt_parts:
            return "上下文信息:\n" + "\n".join([f"- {part}" for part in prompt_parts])

        return ""

    _LANG_NAMES = {
        "zh": "中文", "en": "English", "ar_ur": "Arabic/Urdu",
        "hi": "Hindi", "ru": "Русский", "ja": "日本語",
        "ko": "한국어", "th": "ภาษาไทย", "pa": "ਪੰਜਾਬੀ",
        "bn": "বাংলা", "pt": "Português", "es": "Español",
        "fr": "Français", "de": "Deutsch", "it": "Italiano",
        "tr": "Türkçe", "vi": "Tiếng Việt", "id": "Bahasa Indonesia",
        # lang_policy / translation_service 标准码补齐（缺条目会导致
        # LANGUAGE RULE 整段不注入——空 _lang_name 短路）
        "ar": "Arabic", "km": "ភាសាខ្មែរ (Khmer)", "he": "עברית (Hebrew)",
        "el": "Ελληνικά (Greek)", "tl": "Tagalog",
        # Q-21 C（#290）：粤语 / 繁体是一等 reply_lang——缺条目 LANGUAGE RULE 整段短路，选了粤语等于没选
        "yue": "口語粵語（廣東話，繁體字）", "zh-tw": "繁體中文",
    }

    _SHORT_EN_WORDS = frozenset({
        "ok", "hi", "no", "yes", "hey", "bye", "thx", "ty", "gm", "gn",
        "good", "fine", "done", "help", "how", "why", "what", "who",
        "pls", "plz", "brb", "omg", "wtf", "lol", "asap",
    })

    def _detect_message_language(self, text: str) -> str:
        """粗判用户消息主语言（回复镜像用）。确定性核心委托全局检测器，保留本类语义。

        统一收敛（沿用 P33 模式）：脚本块 + 拉丁关键词的确定性核心委托全局
        translation_service.detect_language（单一规则来源，白嫖泰铢加固 + km/he/el/tl
        新语种 + 可注入统计层）。本方法只保留「回复镜像」特有语义：
          - 空 / 仅 @mention -> 'zh'（回复默认主力中文）
          - 阿拉伯/乌尔都 -> 'ar_ur'（本类下游 prompt 契约，全局返回 'ar'）
          - 旁遮普 / 孟加拉：全局未覆盖（会落 zh），委托前本地拦截以保留既有能力
          - 含糊拉丁兜底 -> 'en'（英文客户回英文，区别于 inbox 业务默认 zh），并保留
            langdetect 末端精修，使能力不依赖全局统计层是否配置。
        """
        if not text or not isinstance(text, str):
            return "zh"
        t = re.sub(r"@\w+", "", text.strip()).strip()
        if not t:
            return "zh"

        # 旁遮普(古木基文)/孟加拉文：全局检测器未含 -> 会被其 cjk>=latin 短路成 zh，
        # 故委托前先本地拦截，保留本类原有的 pa/bn 识别。
        if re.search(r"[\u0A00-\u0A7F]", t):
            return "pa"
        if re.search(r"[\u0980-\u09FF]", t):
            return "bn"

        # 确定性核心：委托全局单一检测器（脚本块 + 越南语 + CJK + 拉丁关键词）。
        from src.ai.translation_service import detect_language as _global_detect

        lang = _global_detect(t)
        if lang not in ("en", "unknown"):
            return "ar_ur" if lang == "ar" else lang

        # —— 含糊拉丁 / 弱结果：本类回复镜像兜底 ——
        cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
        letters = len(re.findall(r"[A-Za-z]", t))

        # 中文为主、夹少量英文词（letters 未显著超过 cjk）-> 判 zh（保留原比例规则）
        if cjk > 0 and not (letters > cjk * 3):
            return "zh"

        if letters >= 3:
            # langdetect 末端精修：不依赖全局统计层配置，保留本类既有多语言识别能力。
            if len(t) >= 8:
                try:
                    from langdetect import detect_langs as _ld_detect_langs  # type: ignore
                    _ld_langs = _ld_detect_langs(t)
                    if _ld_langs:
                        _ld_top = _ld_langs[0]
                        if _ld_top.prob >= 0.55:
                            _ld_raw = str(_ld_top.lang)
                            _ld_map = {"zh-cn": "zh", "zh-tw": "zh", "zh": "zh", "ar": "ar_ur"}
                            _ld_code = _ld_map.get(_ld_raw, _ld_raw)
                            if _ld_code in self._LANG_NAMES:
                                self.logger.debug(
                                    "_detect_message_language: %r → %s (prob=%.3f)",
                                    t[:50], _ld_code, _ld_top.prob,
                                )
                                return _ld_code
                except Exception:
                    pass
            return "en"

        t_lower = t.lower().strip("?？!！.。, ")
        if t_lower in self._SHORT_EN_WORDS:
            return "en"

        if letters > 0 and cjk == 0:
            digits = len(re.findall(r"\d", t))
            if letters + digits >= len(t.replace(" ", "")) * 0.8:
                return "en"

        return "zh"

    # Filter: discard facts about the assistant's own name/identity.
    # These override the configured persona and cause "名字污染".
    _MEMORY_IDENTITY_FILTERS = (
        "助手", "称呼助手", "告知全名", "助手全名", "助手名", "助手叫",
        "我叫", "我的名字", "bot叫", "AI叫",
    )

    def _parse_memory_fact_items(self, raw: str) -> List[Dict[str, Any]]:
        """Parse model output → ``[{"fact", "evidence", "confidence"?}]``.

        J-10 A1：新格式 ``{"facts":[{"fact":"客户有一个女儿","evidence":"I have a daughter"}]}``；
        旧格式 ``{"facts":["..."]}`` 仍可解析（evidence 为空 → 接地护栏按 ``no_evidence``
        丢并计数，可观测模型是否没按新格式输出）。容 ``text``/``quote`` 别名键。
        """
        if not raw or not isinstance(raw, str):
            return []
        t = raw.strip()
        if t.startswith("```"):
            t = re.sub(r"^```\w*\s*", "", t)
            t = re.sub(r"\s*```\s*$", "", t).strip()
        try:
            obj = json.loads(t)
        except json.JSONDecodeError:
            return []
        facts = obj.get("facts") if isinstance(obj, dict) else None
        if not isinstance(facts, list):
            return []
        out: List[Dict[str, Any]] = []
        for x in facts:
            conf: Optional[float] = None
            if isinstance(x, dict):
                s = str(x.get("fact") or x.get("text") or x.get("content") or "").strip()
                ev = str(x.get("evidence") or x.get("quote") or "").strip()
                # J-10 二期：数值置信（原话直接陈述 ≥0.85 / 语境推断 0.5–0.7）→ 例外队列
                # low_confidence 判据的输入；缺省 None（store 按默认 0.8 处理＝不触发）。
                cv = x.get("confidence")
                if cv is not None:
                    try:
                        conf = max(0.0, min(1.0, float(cv)))
                    except (TypeError, ValueError):
                        conf = None
            else:
                s, ev = str(x).strip(), ""
            if 2 <= len(s) <= 500:
                if not any(kw in s for kw in self._MEMORY_IDENTITY_FILTERS):
                    item: Dict[str, Any] = {"fact": s, "evidence": ev[:200]}
                    if conf is not None:
                        item["confidence"] = conf
                    out.append(item)
            if len(out) >= 6:
                break
        return out

    def _parse_memory_facts_json(self, raw: str) -> List[str]:
        """Parse model output {\"facts\": [...]} → 事实文本列表（兼容壳）。"""
        return [it["fact"] for it in self._parse_memory_fact_items(raw)]

    # ── 成本计量辅助（2026-09-08 成本对账 P0）──────────────────────────────────
    def _provider_of(self, client: Any = None) -> str:
        """OpenAI SDK client → 厂商短名（账单主体）。缺省取主链 client。"""
        try:
            from src.ai.llm_cost import provider_from_base_url
            c = client if client is not None else getattr(self, "_oa_client", None)
            return provider_from_base_url(str(getattr(c, "base_url", "") or ""))
        except Exception:
            return ""

    @staticmethod
    def _estimate_prompt_tokens(messages: Any) -> int:
        """无 usage 时的 prompt 估算：中英混排按 0.75 token/字符（DeepSeek 分词实测
        中文约 0.7~0.8）。只用于 ``suspected`` 记账，不进任何决策。"""
        try:
            n = 0
            for m in messages or []:
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, str):
                    n += len(c)
                elif isinstance(c, list):
                    n += sum(len(str(p.get("text") or "")) for p in c if isinstance(p, dict))
            return int(n * 0.75)
        except Exception:
            return 0

    def _record_suspected_usage(self, *, model: str, messages: Any,
                                context: Optional[Dict[str, Any]] = None,
                                provider: str = "", purpose: Optional[str] = None) -> None:
        """超时后的「疑似已计费」记账（绝不抛）。"""
        try:
            from src.ai.llm_cost import get_llm_cost, purpose_for_reply
            _ctx = context or {}
            get_llm_cost().record(
                model=str(model or self.model), prompt_tokens=self._estimate_prompt_tokens(messages),
                completion_tokens=0, tier=str(_ctx.get("ai_tier") or "default"),
                account_id=str(_ctx.get("account_id") or "default"),
                purpose=purpose or purpose_for_reply(_ctx), provider=provider,
                status="timeout", suspected=True,
            )
        except Exception:
            pass

    def _record_direct_usage(self, response: Any, *, purpose: str, model: str = "",
                             client: Any = None, latency_ms: Optional[int] = None) -> None:
        """直连 ``chat.completions.create`` 的统一记账出口（记忆抽取/摘要/短判等）。"""
        try:
            from src.ai.llm_cost import record_usage_from_response
            record_usage_from_response(
                response, model=str(model or self.model), purpose=purpose,
                provider=self._provider_of(client), tier="tool", latency_ms=latency_ms)
        except Exception:
            pass

    async def extract_memory_bullets(self, user_msg: str, assistant_msg: str) -> List[str]:
        """兼容壳：只要事实文本（评测器 / 旧调用方）。真实抽取见 ``extract_memory_facts``。"""
        res = await self.extract_memory_facts(user_msg, assistant_msg)
        return [str(it.get("fact") or "") for it in (res.get("facts") or []) if it.get("fact")]

    async def extract_memory_facts(self, user_msg: str, assistant_msg: str) -> Dict[str, Any]:
        """
        One cheap LLM call: extract 0–4 durable user-specific facts from this turn.
        Skips when circuit breaker is open (caller may still use heuristics).

        返回 ``{"facts": [{"fact", "evidence"}], "dropped": [{"fact", "evidence", "reason"}],
        "candidates": int}``——``facts`` 已过引文级接地护栏（``evidence`` 是客户原话逐字
        引文，调用方存进 ``source_quote``）；``dropped`` 供调用方按原因记账
        （``no_evidence`` / ``evidence_mismatch``）。任何失败 → 三者皆空。
        """
        empty: Dict[str, Any] = {"facts": [], "dropped": [], "candidates": 0}
        # P1 2026-08-19：剥掉 [图片内容]/[视频内容] 识别描述——系统自产内容不得变成
        # 「用户事实」（聊天截图里被抄录的“我是XX”会污染本人画像，Phase8 幻觉同族）。
        # 剥完只剩空（纯媒体消息）→ 下面的长度闸自然短路返回 []。
        try:
            from src.inbox.media_enrich import strip_media_desc
            user_msg = strip_media_desc(user_msg or "")
        except Exception:
            pass
        u = (user_msg or "").strip()
        a = (assistant_msg or "").strip()
        if len(u) < 2 or len(a) < 2:
            return empty
        if self._cb_enabled and self._cb_open_until > 0 and time.time() < self._cb_open_until:
            self.logger.debug("extract_memory_facts skipped: circuit open")
            return empty

        sys_inst = (
            "你是对话记忆抽取器。根据本轮用户消息与助手回复，抽取值得后续聊天记住的客观信息"
            "（用户称呼自己的名字、用户的偏好、用户刚透露的重要事实、简单约定）。"
            "不要抽取通道费率、订单号等业务敏感数字（除非用户明确说这是 TA 自己的）。"
            "【重要】绝对不要抽取关于助手/AI/机器人自己的名字、身份、角色的事实，"
            "例如「用户称呼助手为X」「助手全名为Y」等，这类信息由系统人设控制，不需记忆。"
            "【接地铁律】事实只能来自【USER 消息里用户明确说出的内容】，并尽量沿用用户原词；"
            "ASSISTANT 回复只是语境参考——凡是只出现在助手回复里的猜测、提议、问句内容"
            "（如助手问「明天不用上班吗？」而用户没有确认），一律不得作为事实输出。"
            "用户没有明确说的，宁可不抽。"
            # J-10 A1（#183）：引文级接地——事实用中文概括，引文保持客户原语言逐字复制，
            # 护栏只核引文是否真出自 USER 消息（语言无关）。英文/泰文/日文客户的话
            # 不再因「中文事实 vs 外语原话零重叠」被整条丢掉。
            "【引文铁律】每条事实必须附 evidence：从 USER 消息里**逐字复制**的一段原话"
            "（保持客户原语言，不翻译、不改写、不拼接、不补词，≤80 字，只取能直接支撑"
            "该事实的那一段）；USER 消息里找不到能逐字引用的支撑原话，就不要输出这条事实。"
            "evidence 绝不能取自 ASSISTANT 回复。客户用英文/泰文/日文等外语说的，"
            "fact 仍写中文，evidence 照抄原文。"
            "【持久性铁律】只抽取有持续意义的信息（身份/称呼/偏好/关系/经历/约定/计划）；"
            "转瞬即逝的当下状态一律不抽——此刻天气（正在下雨/好热）、正在做的动作"
            "（在吃饭/刚到家）、当下瞬时情绪（现在好困）等，这些由近期对话自然衔接，"
            "存成长期记忆只会积累过期噪声。"
            "【称呼判别】「叫我X」只有当 X 是名字/昵称/称号时才是称呼事实；"
            "「叫我别走」「叫我怎么办」这类 X 为动词短语的是祈使/求助语气，不是称呼，不得抽取。"
            # #96（0830 Steven 实锤）：方向抽反——客户对助手打招呼「hi steven」
            # 被抽成「用户称呼自己为steven」，AI 消费后拿自己人设名叫客户。
            "【称呼方向铁律】用户消息里出现的名字若处于**招呼/呼叫位**"
            "（如「hi X」「你好X」「morning X」「X 在吗」），那是用户在称呼"
            "**助手**，绝不是用户自己的名字，不得抽取；只有用户明确自我介绍"
            "（「我是X」「我叫X」「my name is X」「call me X」）才算用户自己的称呼。"
            "【主语无歧义铁律】每条事实的主语必须显式写「客户」且方向唯一，"
            "如「客户自称X」「客户希望被称呼为X」；禁止「用户称呼自己为X」"
            "这类「自己」指代不清的双解句式。"
            # J-10 二期：数值置信——LLM 自己最清楚这条是「原话直说」还是「结合语境推出来的」，
            # 低置信的进例外队列让人看一眼（不阻断记录）。
            "【置信】每条附 confidence（0～1）：原话直接陈述该事实 ≥0.85；需要结合语境推断"
            "（如从「夜班很累」推出「客户是护士」）0.5～0.7；再低就不要输出。"
            "输出严格为一行 JSON，不要 markdown："
            '{"facts":[{"fact":"客户有一个女儿","evidence":"I have a daughter","confidence":0.95}]} '
            "facts 为 0～4 条，fact 是中文短句，evidence 是 USER 消息里的逐字原文；无则 []。"
        )
        usr = f"USER:\n{u[:2000]}\n\nASSISTANT:\n{a[:2000]}"

        try:
            if self._use_openai_compat and self._oa_client:
                _prof = self.resolve_route("memory_extract")
                _ex_client = (_prof.get("client") if _prof else None) or self._oa_client
                _ex_model = str((_prof or {}).get("model") or self.model)
                _ex_extra = (_prof or {}).get("extra_body") or None
                async def _call():
                    _kw: Dict[str, Any] = dict(
                        model=_ex_model,
                        messages=[
                            {"role": "system", "content": sys_inst},
                            {"role": "user", "content": usr},
                        ],
                        temperature=0.15,
                        max_tokens=480,
                    )
                    if _ex_extra:
                        _kw["extra_body"] = _ex_extra
                    return await _ex_client.chat.completions.create(**_kw)

                _t0 = time.time()
                response = await asyncio.wait_for(_call(), timeout=14.0)
                self._record_direct_usage(
                    response, purpose="memory_extract",
                    latency_ms=int((time.time() - _t0) * 1000))
                raw = ""
                if response and response.choices:
                    raw = (response.choices[0].message.content or "").strip()
                return self._ground_extracted_fact_items(
                    self._parse_memory_fact_items(raw), u)

            if GENAI_AVAILABLE and self.client:
                use_model = self.model
                config = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.15,
                    max_output_tokens=480,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=use_model,
                        contents=[types.Content(role="user", parts=[types.Part(text=usr)])],
                        config=config,
                    ),
                    timeout=14.0,
                )
                raw = ""
                if response and response.candidates:
                    try:
                        raw = (response.text or "").strip()
                    except (ValueError, AttributeError, IndexError):
                        raw = ""
                return self._ground_extracted_fact_items(
                    self._parse_memory_fact_items(raw), u)
        except asyncio.TimeoutError:
            self.logger.debug("extract_memory_facts timeout")
        except Exception as e:
            self.logger.debug("extract_memory_facts failed: %s", e)
        return empty

    def _ground_extracted_fact_items(
        self, items: List[Dict[str, str]], user_msg: str,
    ) -> Dict[str, Any]:
        """引文级接地护栏（J-10 A1）：只保留 ``evidence`` 真出自用户原话的事实。

        判据见 ``memory_grounding.ground_fact_with_evidence``：引文必须真出自用户原话
        （跨语种只认引文，事实文本不参与匹配）；同语种照旧叠加旧词汇判据；应答词
        （yes / 好呀）不算引文——安全语义不弱于 Phase8 以来的旧护栏。
        丢弃即记 WARNING 并按原因回传（``no_evidence`` / ``evidence_mismatch`` /
        ``fact_unanchored``），调用方落库记账 → 页面「有 N 条因无法核对原话未记录」。
        护栏自身异常 → 回退旧判据（绝不比旧防线更松，也绝不阻断记忆链）。
        """
        out: Dict[str, Any] = {"facts": [], "dropped": [], "candidates": len(items or [])}
        if not items:
            return out
        try:
            from src.ai.memory_grounding import ground_fact_items
            kept, dropped = ground_fact_items(items, user_msg)
            # ground_fact_items 只回 text/evidence；置信按事实文本回填（J-10 二期）
            conf_by_fact: Dict[str, float] = {
                str(it.get("fact") or ""): float(it["confidence"])
                for it in items if isinstance(it, dict) and it.get("confidence") is not None
            }
            out["facts"] = []
            for k in kept:
                if not k.get("text"):
                    continue
                row: Dict[str, Any] = {"fact": str(k.get("text") or ""),
                                       "evidence": str(k.get("evidence") or "")}
                if row["fact"] in conf_by_fact:
                    row["confidence"] = conf_by_fact[row["fact"]]
                out["facts"].append(row)
            out["dropped"] = [
                {"fact": str(d.get("text") or ""), "evidence": str(d.get("evidence") or ""),
                 "reason": str(d.get("reason") or "")}
                for d in dropped if d.get("text")
            ]
            if out["dropped"]:
                self.logger.warning(
                    "记忆抽取接地护栏丢弃 %d 条未锚定用户原话的事实: %s",
                    len(out["dropped"]),
                    "; ".join(
                        f"[{d['reason']}] {d['fact'][:40]} ⇐ {d['evidence'][:40]!r}"
                        for d in out["dropped"]),
                )
        except Exception:
            # 判定器整体异常 → 旧判据兜底（与 memory_grounding 单项兜底同口径）
            self.logger.debug("memory grounding failed; falling back to lexical rule",
                              exc_info=True)
            kept_txt = self._ground_extracted_facts(
                [str(it.get("fact") or "") for it in items if it.get("fact")], user_msg)
            out["facts"] = [
                {"fact": str(it.get("fact") or ""), "evidence": str(it.get("evidence") or "")}
                for it in items if str(it.get("fact") or "") in kept_txt
            ]
        return out

    def _ground_extracted_facts(self, facts: List[str], user_msg: str) -> List[str]:
        """接地护栏：只保留锚定在用户原话上的事实（防「AI 臆测→假记忆→复读幻觉」）。

        真实事故：AI 问「明天不用上班吗？」→ 被抽成「用户明天不用上班」入库 →
        下轮注入 prompt → AI 复读臆测 → 用户「你精神错乱了吗」。丢弃即记 WARNING
        （可观测幻觉抽取率）。判定器异常 → 原样放行（绝不阻断记忆链）。
        """
        if not facts:
            return facts
        try:
            from src.ai.memory_grounding import filter_grounded_facts
            kept, dropped = filter_grounded_facts(facts, user_msg)
            if dropped:
                self.logger.warning(
                    "记忆抽取接地护栏丢弃 %d 条未锚定用户原话的事实: %s",
                    len(dropped), "; ".join(d[:40] for d in dropped))
            return kept
        except Exception:
            return facts

    # ── P3-6：LLM 语义摘要（会话长尾压缩） ─────────────
    async def summarize_conversation(
        self,
        history: List[Dict[str, str]],
        *,
        max_chars: int = 200,
        timeout_sec: float = 14.0,
    ) -> str:
        """把 conversation_history（[{role,content},...]）压成一段≤max_chars 的中文摘要。

        失败返回空串（caller 可回退规则引擎）。
        """
        if not history or not isinstance(history, list):
            return ""
        if self._cb_enabled and self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return ""
        # 预处理：截断 + 拼文本
        lines: List[str] = []
        for m in history[-40:]:  # 最多送最近 40 条
            role = "用户" if m.get("role") == "user" else "助手"
            txt = str(m.get("content") or "")[:240].replace("\n", " ")
            if txt:
                lines.append(f"{role}: {txt}")
        if len(lines) < 4:
            return ""
        joined = "\n".join(lines)[:6000]

        sys_inst = (
            "你是对话摘要器。把以下对话压缩成一段中文摘要，抓住：\n"
            "1) 用户的关键诉求/话题演进；2) 已达成的共识；3) 用户透露的稳定事实（称呼、偏好、约定）。\n"
            f"输出纯中文（≤{int(max_chars)} 字），不要 markdown、不要 json、不要换行，"
            "一段话写完。"
        )
        try:
            if self._use_openai_compat and self._oa_client:
                async def _call():
                    return await self._oa_client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": sys_inst},
                            {"role": "user", "content": joined},
                        ],
                        temperature=0.2,
                        max_tokens=400,
                    )
                response = await asyncio.wait_for(_call(), timeout=timeout_sec)
                self._record_direct_usage(response, purpose="memory_extract")
                raw = ""
                if response and response.choices:
                    raw = (response.choices[0].message.content or "").strip()
                return raw[:max_chars]

            if GENAI_AVAILABLE and self.client:
                config = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.2,
                    max_output_tokens=400,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.model,
                        contents=joined,
                        config=config,
                    ),
                    timeout=timeout_sec,
                )
                if response and response.candidates:
                    try:
                        return (response.text or "").strip()[:max_chars]
                    except (ValueError, AttributeError, IndexError):
                        return ""
        except asyncio.TimeoutError:
            self.logger.debug("summarize_conversation timeout")
        except Exception as e:
            self.logger.debug("summarize_conversation failed: %s", e)
        return ""

    # ── P7-4：长期记忆蒸馏（从 working summary + 历史 → 稳定 facts） ──
    async def extract_long_term_facts(
        self,
        *,
        working_summary: str,
        recent_history: List[Dict[str, str]],
        existing_facts: Optional[List[str]] = None,
        max_facts: int = 15,
        timeout_sec: float = 15.0,
    ) -> List[str]:
        """从 working_summary + 最近历史 + 已有 facts 蒸馏出"不易变"的长期事实。

        返回新的 facts 列表（已合并、去重、限长）。失败返回 existing_facts 副本。

        事实类型：称呼/自称姓名、常驻地区、长期偏好、已承诺的约定、
        已明确的拒绝事项、产品/服务意向、家庭/职业信息。
        **不**包含：一次性情绪、当下聊的话题、临时问题。
        """
        existing = [s for s in (existing_facts or []) if isinstance(s, str) and s.strip()]
        if not working_summary and len(recent_history) < 4:
            return existing[:max_facts]
        if self._cb_enabled and self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return existing[:max_facts]

        lines: List[str] = []
        for m in (recent_history or [])[-20:]:
            role = "用户" if m.get("role") == "user" else "助手"
            txt = str(m.get("content") or "")[:200].replace("\n", " ")
            if txt:
                lines.append(f"{role}: {txt}")
        hist_block = "\n".join(lines)[:3500]

        existing_block = ""
        if existing:
            existing_block = "已记录的事实（尽量保留，除非被明确否定/更新）：\n- " + \
                "\n- ".join(existing[:max_facts])

        sys_inst = (
            "你是对话长期记忆管理器。从以下对话摘要 + 最近片段里，抽取"
            "稳定的、跨话题依然成立的事实（姓名/地区/长期偏好/已承诺约定/"
            "明确拒绝/产品意向/家庭或职业信息）。\n"
            "规则：\n"
            "1) 只保留'跨天仍有效'的信息，不记录一次性话题或即时情绪。\n"
            "2) 每条 ≤40 字中文；单行 bullet。\n"
            f"3) 输出 JSON 数组，最多 {int(max_facts)} 项；若无新信息就回已有。\n"
            "4) 若发现已有事实被新内容否定/更正，用新版覆盖；否则合并保留。\n"
            "5) 严格输出 JSON 数组，不要任何解释文本、不要 markdown。"
        )
        user_block = (
            f"工作摘要：\n{working_summary or '(暂无)'}\n\n"
            f"最近对话：\n{hist_block or '(暂无)'}\n\n"
            f"{existing_block}"
        )

        def _parse(raw: str) -> List[str]:
            s = (raw or "").strip()
            if s.startswith("```"):
                s = re.sub(r"^```\w*\s*", "", s)
                s = re.sub(r"\s*```\s*$", "", s).strip()
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                # 兜底：按行拆
                items = [
                    re.sub(r"^[-*\d.\s)]+", "", ln).strip()
                    for ln in s.splitlines() if ln.strip()
                ]
                return [x[:80] for x in items if x][:max_facts]
            if isinstance(obj, list):
                out: List[str] = []
                for x in obj:
                    if isinstance(x, str) and x.strip():
                        out.append(x.strip()[:80])
                    elif isinstance(x, dict):
                        v = str(x.get("fact") or x.get("text") or "").strip()
                        if v:
                            out.append(v[:80])
                return out[:max_facts]
            return []

        try:
            if self._use_openai_compat and self._oa_client:
                # DeepSeek 不支持 array 顶层的 json_object —— 包一层
                sys_inst_wrap = sys_inst + "\n输出格式示例：{\"facts\": [\"事实1\", \"事实2\"]}"
                async def _call_wrap():
                    return await self._oa_client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": sys_inst_wrap},
                            {"role": "user", "content": user_block},
                        ],
                        temperature=0.1,
                        max_tokens=800,
                        response_format={"type": "json_object"},
                    )
                try:
                    response = await asyncio.wait_for(_call_wrap(), timeout=timeout_sec)
                except Exception:
                    # 某些 provider 不支持 response_format → 降级不带
                    response = await asyncio.wait_for(
                        self._oa_client.chat.completions.create(
                            model=self.model,
                            messages=[
                                {"role": "system", "content": sys_inst},
                                {"role": "user", "content": user_block},
                            ],
                            temperature=0.1,
                            max_tokens=800,
                        ),
                        timeout=timeout_sec,
                    )
                self._record_direct_usage(response, purpose="memory_extract")
                raw = ""
                if response and response.choices:
                    raw = (response.choices[0].message.content or "").strip()
                # 尝试 parse 成 {"facts":[...]}
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        facts = obj.get("facts") or obj.get("items") or []
                        if isinstance(facts, list):
                            parsed = [str(x).strip()[:80] for x in facts if str(x).strip()][:max_facts]
                            return parsed or existing[:max_facts]
                except json.JSONDecodeError:
                    pass
                parsed = _parse(raw)
                return parsed or existing[:max_facts]

            if GENAI_AVAILABLE and self.client:
                config = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.1,
                    max_output_tokens=800,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.model,
                        contents=user_block,
                        config=config,
                    ),
                    timeout=timeout_sec,
                )
                if response and response.candidates:
                    try:
                        raw = (response.text or "").strip()
                        parsed = _parse(raw)
                        return parsed or existing[:max_facts]
                    except (ValueError, AttributeError, IndexError):
                        return existing[:max_facts]
        except asyncio.TimeoutError:
            self.logger.debug("extract_long_term_facts timeout")
        except Exception as e:
            self.logger.debug("extract_long_term_facts failed: %s", e)
        return existing[:max_facts]

    def _format_slow_think_raw(self, raw: str) -> str:
        """Parse planning JSON to compact text for stage-2 context."""
        if not raw or not isinstance(raw, str):
            return ""
        t = raw.strip()
        if t.startswith("```"):
            t = re.sub(r"^```\w*\s*", "", t)
            t = re.sub(r"\s*```\s*$", "", t).strip()
        try:
            obj = json.loads(t)
        except json.JSONDecodeError:
            return t[:2000]
        if not isinstance(obj, dict):
            return t[:2000]
        lines: List[str] = []
        ang = obj.get("angles")
        if isinstance(ang, list):
            for i, x in enumerate(ang[:8]):
                s = str(x).strip()
                if s:
                    lines.append(f"{i + 1}. {s}")
        rk = obj.get("risks")
        if isinstance(rk, list) and rk:
            lines.append("风险/遗漏:")
            for x in rk[:5]:
                s = str(x).strip()
                if s:
                    lines.append(f"- {s}")
        return "\n".join(lines)[:2800]

    async def slow_think_outline(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
        stage1_max_tokens: int = 400,
    ) -> str:
        """
        Stage-1: internal planning only (not shown to end user verbatim).
        Returns compact bullet text for injection into stage-2 system context.
        """
        if self._cb_enabled and self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return ""
        u = (user_message or "").strip()
        if len(u) < 2:
            return ""
        ctx = context or {}
        parts: List[str] = []
        ep = (ctx.get("_episodic_memory_text") or "").strip()
        if ep:
            parts.append("【用户记忆要点】\n" + ep[:1000])
        kb = (ctx.get("kb_context") or "").strip()
        if kb:
            parts.append("【知识库参考片段】\n" + kb[:600])
        ca = ctx.get("context_analysis")
        if isinstance(ca, dict):
            em = (ca.get("user_emotion") or "").strip()
            if em:
                parts.append(f"【情绪粗判】{em}")
        pack = "\n\n".join(parts)
        sys_inst = (
            "你是对话策略助理，只做内部规划，不直接对用户输出。"
            "根据用户消息与下列材料，输出严格一行 JSON（不要 markdown）："
            '{"angles":["角度1","角度2"],"risks":["可选风险"]}'
            "angles 为 2～5 条中文短句，覆盖回应重点；risks 0～3 条。"
        )
        usr = f"用户消息：\n{u[:1800]}\n\n---\n{pack}" if pack else f"用户消息：\n{u[:1800]}"
        mt = max(120, min(int(stage1_max_tokens or 400), 800))
        try:
            raw = ""
            if self._use_openai_compat and self._oa_client:
                response = await asyncio.wait_for(
                    self._oa_client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": sys_inst},
                            {"role": "user", "content": usr},
                        ],
                        temperature=0.2,
                        max_tokens=mt,
                    ),
                    timeout=18.0,
                )
                self._record_direct_usage(response, purpose="tool")
                if response and response.choices:
                    raw = (response.choices[0].message.content or "").strip()
            elif GENAI_AVAILABLE and self.client:
                config = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    temperature=0.2,
                    max_output_tokens=mt,
                )
                response = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.model,
                        contents=[types.Content(role="user", parts=[types.Part(text=usr)])],
                        config=config,
                    ),
                    timeout=18.0,
                )
                if response and response.candidates:
                    try:
                        raw = (response.text or "").strip()
                    except (ValueError, AttributeError, IndexError):
                        raw = ""
            return self._format_slow_think_raw(raw)
        except asyncio.TimeoutError:
            self.logger.debug("slow_think_outline timeout")
        except Exception as e:
            self.logger.debug("slow_think_outline failed: %s", e)
        return ""

    async def generate_reply_with_intent(
        self,
        user_message: str,
        intent: str,
        user_context: Dict[str, Any],
        strategy_overrides: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        基于意图生成回复，支持策略参数覆盖 + 自动注入对话历史。
        """
        enhanced_context = user_context.copy()
        enhanced_context['intent'] = intent

        # L2: 从 user_context 提取对话历史，传递给 generate_reply
        _conv_hist = user_context.get('_conversation_history')
        if isinstance(_conv_hist, list) and _conv_hist:
            conversation_history = _conv_hist
        else:
            conversation_history = None

        intent_supplement = self._get_intent_prompt(intent)
        if intent_supplement:
            enhanced_context["_intent_supplement"] = intent_supplement

        if not (enhanced_context.get("_current_user_message_for_lang") or "").strip():
            enhanced_context["_current_user_message_for_lang"] = user_message
        reply = await self.generate_reply(
            user_message, enhanced_context,
            conversation_history=conversation_history,
            strategy_overrides=strategy_overrides)
        # 会话级严格路由离线信号回传（enhanced_context 是副本，调用方读的是 user_context）
        if enhanced_context.get("_route_offline"):
            user_context["_route_offline"] = enhanced_context["_route_offline"]
        else:
            user_context.pop("_route_offline", None)

        # 生成层口语分叉（Phase G）：请求过口语版就无条件剥标记（书面版绝不带
        # [口语版] 字样，哪怕 LLM 输出畸形）；口语段合格 → 按书面版哈希暂存，
        # 语音发送方（sender）凭最终文本哈希取用——中途任何后处理改动都会令
        # 哈希失配而自动放弃口语版（安全回落既有口语化链）。
        if reply and enhanced_context.get("_spoken_variant_request"):
            try:
                from src.ai.spoken_variant import (
                    split_spoken_variant,
                    stash_spoken_variant,
                )
                written, spoken = split_spoken_variant(reply)
                if spoken:
                    stash_spoken_variant(
                        written, spoken,
                        scope=str(enhanced_context.get("account_id") or ""),
                    )
                reply = written
            except Exception:
                self.logger.debug("spoken variant split skipped", exc_info=True)

        # 无限制会话：L3 出口清洁 / L4 口语化改写属「质量层」，整段让路（conv_route.skip_guard）
        try:
            from src.ai.conv_route import skip_guard as _cr_skip
            if _cr_skip(enhanced_context, "spoken_style_cleanup"):
                return reply
        except Exception:
            pass

        # 真人感文本层 L3：出口清洁（剥情绪/副语言标记；未启用=原样返回，零成本）
        _pre_l34_reply = reply
        if reply:
            try:
                from src.ai.spoken_style_bridge import clean_reply_text as _ss_clean
                reply = _ss_clean(self.config, reply)
            except Exception:
                pass

        # 真人感文本层 L4：口语化改写（默认关；事实锁把关，失败/超时原句直通。
        # 放在 spoken_variant 摘取之后：即便改写生效，语音口语版哈希失配会自动
        # 放弃暂存走既有口语化链——两层不会叠加）
        # role=会话人设口称名（generate_reply 内 persona 解析写进了 enhanced_context，
        # dict 同对象直传，这里读到的是本轮真实人设）→ 改写提示按人设指纹分流
        if reply:
            try:
                from src.ai.spoken_style_bridge import rewrite_reply as _ss_rewrite
                reply = await _ss_rewrite(
                    self.config, reply,
                    role=str(enhanced_context.get("_resolved_persona_name") or ""),
                    context=enhanced_context,  # #40 地区档禁用词观测（只计数不改文本）
                )
            except Exception:
                pass

        # 形态收口兜底（2026-08-15）：L3 剥标记可能留下空行（标记独占一行时
        # sub 后剩空行）、L4 改写可能重新分段——**仅当 L3/L4 真改动了文本**才
        # 对最终文本重走出口形态护栏。未改动（两层关闭/无事可做）时零触碰，
        # 保住「未请求口语分叉 → with_intent 零行为变化」的分层契约
        # （test_spoken_variant 钉住）；改动过的文本口语版哈希本就已失配，
        # 重整形不新增任何回退面。
        if reply and reply != _pre_l34_reply:
            reply = self._shape_single_paragraph(reply, enhanced_context)

        return reply

    _INTENT_SUPPLEMENTS = {
        "complaint": (
            "【当前意图：投诉/不满 / Intent: Complaint】用户正在投诉或表达不满。"
            "请先认同情绪、表达理解（如'确实给您添麻烦了'），然后给出明确的处理方案和时间预期。"
            "不要敷衍推脱，也不要过度道歉。"
            "If the user writes in English or another non-Chinese language, respond in that language."
        ),
        "order_query": (
            "【当前意图：订单查询 / Intent: Order Query】用户在查询订单。"
            "优先引用上下文中已有的订单号/交易号/识图结果。"
            "有凭证则确认收到+处理中，无凭证则引导提供。"
            "If the user writes in English or another non-Chinese language, respond in that language."
        ),
        "channel_info": (
            "【当前意图：通道/额度咨询 / Intent: Channel Info】用户在问通道状态、额度或成功率。"
            "只客观告知实时数据中列出的通道状态，不推荐特定通道。分条列出，简洁明了。"
            "严禁提及实时数据中未列出的通道名称。"
            "禁止出现「PIX」「pix」或巴西 PIX 相关内容（已永久下架）。"
            "若用户问成功率、或问手续费/费率、或两者同时问：只答各通道成功率与运行状态；"
            "禁止说出任何费率/手续费的具体数值或比例；费率引导联系业务主管或人工客服，不要引导去后台查费率。"
            "If the user writes in English or another non-Chinese language, respond in that language."
        ),
        "greeting": (
            "【当前意图：打招呼 / Intent: Greeting】用户在问候或开场。"
            "简短、活泼、像朋友接话即可，不要长篇大论。"
            "Match the user's language - if they greet in English, respond in English."
        ),
    }

    # #208（L-1 C，2026-09-06）问候不编事实：FTK6S7 客户一句 hi，AI 答「Just got back
    # from a walk, the rain's light here」——档案无雨、无天气源、记忆 0 条。近况要有出处：
    # 人设档案写明的日常 / 本会话或记忆里对方说过的事；没有就只问候不叙事。
    # 出口另有确定性守卫（proactive_fabrication_guard.strip_status_claims）兜底。
    GREETING_NO_FABRICATION_LINE = (
        "【问候不编事实】不得编造天气、所在地点、行程、正在做或刚做完的事；只能引用人设"
        "档案写明的日常，或本会话 / 记忆里对方自己说过的事；没有依据就只问候、接话，"
        "不叙述近况。Do not invent weather, whereabouts, trips or what you were just doing; "
        "mention such things only if they come from the persona profile or from what the other "
        "person said—otherwise just greet, no life-update."
    )

    def _get_intent_prompt(self, intent: str) -> Optional[str]:
        """获取意图特定的补充提示（追加到系统提示末尾，非替换）"""
        try:
            _cfg = self.config.config if self.config and hasattr(self.config, "config") else {}
            _conv = isinstance(_cfg, dict) and effective_domain_name(_cfg) == "conversion"
        except Exception:
            _conv = False
        if intent == "greeting" and _conv:
            return (
                "【当前意图：打招呼 / Intent: Greeting】对方在问你在不在、或轻轻开场。"
                "你们是**情感陪伴/恋人向**私聊，不是客服台或工单系统。"
                "**严禁**使用「有什么可以帮您/帮您的吗」「需要什么服务」「请问有什么可以」"
                "等柜台话术。用一两句像女友/好友微信：如「在呀」「嗯嗯我在～」「找我呀？」「怎么啦」；"
                "用户只发「在」「在吗」时要**短、自然、不重复同一句**，不要接业务办理暗示。"
                "Match the user's language. "
                + self.GREETING_NO_FABRICATION_LINE
            )
        sup = self._INTENT_SUPPLEMENTS.get(intent)
        if intent == "greeting" and sup:
            return sup + self.GREETING_NO_FABRICATION_LINE
        return sup

    async def should_reply_by_context(
        self,
        previous_message: str,
        previous_time: str,
        current_message: str,
    ) -> tuple[bool, str]:
        """
        根据「前一条消息 + 当前消息」让 AI 判断是否应回复。
        conversion 域：陪聊/情绪延续；payment 等业务域：是否与订单/通道等工作相关。
        """
        try:
            _ai_cfg = (self.config.config or {}).get("ai", {}) if self.config else {}
            _cfg = self.config.config if self.config and hasattr(self.config, "config") else {}
            _companion = isinstance(_cfg, dict) and effective_domain_name(_cfg) == "conversion"
            _disp_name = (_ai_cfg.get("ai_name") or ("小桃" if _companion else "小优")).strip() or (
                "小桃" if _companion else "小优"
            )
            if _companion:
                _role_intro = (
                    f"你是「{_disp_name}」，主打轻松陪聊和情绪陪伴，像朋友在线上打字聊天，"
                    "不负责查单、通道、支付等业务办理。"
                )
                _gate_hint = (
                    "根据「前一条消息」和「当前消息」判断：对方是否在延续对话、找你闲聊、倾诉情绪、"
                    "提问、回应你，或明显在对你说话——需要接话、安慰、陪聊、回答时回答 YES；"
                    "若是群内其他人彼此对话、纯噪音或与对话完全无关则回答 NO。"
                )
            else:
                _role_intro = f"你是智能客服{_disp_name}，负责订单查询、通道状态、代收代付等。"
                _gate_hint = (
                    "根据「前一条消息」和「当前消息」判断：当前这条是否在跟你说话或与你的工作相关"
                    "（订单、查单、通道、支付、回调、咨询等）。"
                    "仅当与工作相关且应回复时回答 YES，否则回答 NO。第二行用一句话说明原因。"
                )
            _user_q = (
                "当前消息是否需要你接话、陪聊或回复？回答 YES 或 NO，第二行写原因。"
                if _companion
                else "当前消息是否与你的工作相关、是否需要你回复？回答 YES 或 NO，第二行写原因。"
            )
            if self._use_openai_compat:
                if not self._oa_client:
                    return False, "AI客户端未初始化"
                sys_content = (
                    f"{_role_intro}\n{_gate_hint}\n"
                    "格式严格为：第一行 YES 或 NO，第二行原因。"
                )
                user_content = (
                    f"前一条消息（时间: {previous_time}）:\n{previous_message[:500]}\n\n"
                    f"当前消息:\n{current_message[:500]}\n\n"
                    f"{_user_q}"
                )
                response = await self._oa_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": sys_content},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=150,
                    temperature=0.3,
                )
                self._record_direct_usage(response, purpose="tool")
                raw = (response.choices[0].message.content or "").strip() if response and response.choices else ""
                lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
                first = (lines[0] or "").upper()
                reason = " ".join(lines[1:3]) if len(lines) > 1 else first
                if first.startswith("YES"):
                    return True, reason
                if first.startswith("NO"):
                    return False, reason
                if "YES" in raw.upper():
                    return True, raw[:120]
                return False, raw[:120]
            if not self.client:
                return False, "AI客户端未初始化"
            sys_content = (
                f"{_role_intro}\n{_gate_hint}\n"
                "格式严格为：第一行 YES 或 NO，第二行原因。"
            )
            user_content = (
                f"前一条消息（时间: {previous_time}）:\n{previous_message[:500]}\n\n"
                f"当前消息:\n{current_message[:500]}\n\n"
                f"{_user_q}"
            )
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=sys_content,
                    max_output_tokens=150,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    temperature=0.3,
                ),
            )
            if not response or not response.text:
                return False, "AI返回空"
            raw = response.text.strip()
            lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
            first = (lines[0] or "").upper()
            reason = " ".join(lines[1:3]) if len(lines) > 1 else first
            if first.startswith("YES"):
                return True, reason
            if first.startswith("NO"):
                return False, reason
            if "YES" in raw.upper():
                return True, raw[:120]
            return False, raw[:120]
        except Exception as e:
            self.logger.debug("should_reply_by_context 异常: %s", e)
            return False, str(e)

    @staticmethod
    def _purpose_header(context: Optional[Dict[str, Any]]) -> str:
        """本次调用的用途（``purpose_for_reply`` 同口径），作 ``X-ChatX-Purpose`` 请求头值。
        只出现在 KNOWN_PURPOSES 白名单内的小写标识，绝不抛；异常返回空串＝不带头。"""
        try:
            from src.ai.llm_purpose import KNOWN_PURPOSES, purpose_for_reply
            p = str(purpose_for_reply(context) or "")
            return p if p in KNOWN_PURPOSES else ""
        except (ImportError, AttributeError, TypeError, ValueError):
            return ""

    #: 工具/短判/抽取调用的裸 system（B1，2026-09-11 用量分析）。chat() 此前传
    #: context=None，_build_system_instruction 照样拼上 顾嘉 system_prompt + 默认人设块 +
    #: 身份硬锁 + 禁止内心独白 + 多语言规则 + 作息合理性 ≈ 3.8k 字（能解析出绑定人设时
    #: 5–6k 字），只为换 15–23 字 JSON——网关流水实测这类调用占全队 token 16.9%
    #: （钧机 46.6%），198 坐席一条入站消息 3 次 LLM 里 2 次是它。各工具 prompt 自带
    #: 全部任务指令与角色框定（「你是对话记忆抽取器…」「你是温暖的线上陪伴…」），
    #: 人设与硬约束对它们只是噪声；输出语言/格式由各调用方 prompt 自管。
    TOOL_SYSTEM_PROMPT = (
        "你是聊天产品内部的文本处理组件。严格按下方任务指令执行：只输出任务要求的内容，"
        "不加解释、不加前后缀、不输出思考过程；任务要求 JSON 时只输出合法 JSON。"
    )

    def _tool_chat_bare_enabled(self) -> bool:
        """``ai.tool_chat_bare``（默认 True）。False = 回到旧行为（工具调用带整套
        人设与硬约束），只作线上回滚开关，不是长期档位。绝不抛。"""
        try:
            ai_cfg = (self.config.config or {}).get("ai", {}) if self.config else {}
            return bool((ai_cfg or {}).get("tool_chat_bare", True))
        except (AttributeError, TypeError):
            return True   # 直构对象 / 测试替身无 config：按默认裸 system

    def _tool_context(self, purpose: str = "tool", *,
                      system: Optional[str] = None) -> Dict[str, Any]:
        """工具调用的 context：裸 system（跳过人设/硬约束/语言规则/上下文注入）、
        显式用途（计量与成本归因）、不跑语言守卫。"""
        out = {
            "_bare_system": str(system or self.TOOL_SYSTEM_PROMPT),
            "_llm_purpose": str(purpose or "tool"),
            "_skip_lang_guard": True,
            "_tool_call": True,
        }
        # 已登记的任务档（含默认绑的 memory_extract → LAN）随用途走，不配则仍主链。
        if purpose and purpose in getattr(self, "_task_routes", {}):
            out["_route"] = str(purpose)
        return out

    async def tool_chat(
        self,
        prompt: str,
        *,
        purpose: str = "tool",
        system: Optional[str] = None,
        strategy_overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """工具型调用（抽取 / 分类 / 改写 / 翻译纠错）：裸 system、按 ``purpose``
        归因成本、**不计 ai_reply**、不跑语言守卫 / QualityTracker。

        与 :meth:`chat` 的区别只在显式 ``purpose`` 与可选自定义 ``system``；
        chat() 现在默认也走裸 system（见 TOOL_SYSTEM_PROMPT 注释）。
        """
        from src.ai.llm_purpose import purpose_scope
        ctx = self._tool_context(purpose, system=system)
        with purpose_scope(str(purpose or "tool")):
            return await self.generate_reply(
                prompt, context=ctx, conversation_history=None,
                strategy_overrides=strategy_overrides, _skip_quality_check=True,
            )

    async def chat(
        self,
        prompt: str,
        strategy_overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """短提示词调用（如 L2 触发置信度），走与 generate_reply 相同的提供方。

        ★ 这条入口设计就是给 yes/no 等短答 prompt 用的（见 runner.py 7267
        context_check 等），3 字符 'yes' 是合法回复——必须跳过 QualityTracker，
        否则会反复触发 too_short 误报，污染监控信号。

        ★ B1（2026-09-11）：默认走**裸 system**（TOOL_SYSTEM_PROMPT），不再把整套
        人设 / 硬约束 / 语言规则拼给工具调用；外层 ``purpose_scope`` 已声明用途
        （翻译引擎 / 合并抽取 / 主动触达真发）则沿用该用途——计量只认
        customer_reply，工具用途不记 ai_reply。``ai.tool_chat_bare: false`` 回旧行为。
        """
        from src.ai.llm_purpose import _PURPOSE_VAR, purpose_scope
        scoped = str(_PURPOSE_VAR.get() or "")
        ctx: Optional[Dict[str, Any]] = (
            self._tool_context(scoped or "tool") if self._tool_chat_bare_enabled() else None
        )
        # 成本归因：短判/分类是「工具」用途；外层已声明用途（翻译引擎等）则尊重外层。
        if scoped:
            return await self.generate_reply(
                prompt, context=ctx, conversation_history=None,
                strategy_overrides=strategy_overrides, _skip_quality_check=True,
            )
        with purpose_scope("tool"):
            return await self.generate_reply(
                prompt,
                context=ctx,
                conversation_history=None,
                strategy_overrides=strategy_overrides,
                _skip_quality_check=True,
            )

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "total_calls": self.total_calls,
            "total_tokens": self.total_tokens,
            "last_call_time": self.last_call_time,
            "model": self.model,
            "temperature": self.temperature,
            "provider": self._provider,
            # 主对话位置（ai.primary）。local_fallback_* 只计兜底身份，不含本地主链。
            "primary_mode": getattr(self, "_primary_mode", None) or "cloud",
            # 老板锁（2026-08-22）：非空=主链档位被锁死，越权切换会被强制回锁值
            "primary_lock": getattr(self, "_primary_lock", "") or None,
            "local_fallback_model": self._fb_model or None,
            "local_fallback_calls": self._fb_calls,
            "local_fallback_ok": self._fb_ok,
            "key_pool_size": len(self._pool_entries),
            "key_pool_calls": self._pool_calls,
            "key_pool_ok": self._pool_ok,
            "key_pool_last_key": self._pool_last_key or None,
        }

    _DEGRADE_RECENT_SEC = 600.0   # 「最近出过话」窗口：10 分钟内该链有成功即视为活跃

    def degradation_snapshot(self) -> Dict[str, Any]:
        """云端降级态判定（坐席端状态条用，纯内存零开销）。

        只认**正面证据**，避免把「没流量」误判成降级：
        - 熔断开路中 → 必降级；
        - 主链最近窗口内无成功、而备用池/本地兜底窗口内出过话 → 降级（顶班中）；
        - 其余（含完全无流量、主链正常、已恢复）→ 不降级。
        mode：primary(正常) / pool(备用 Key 顶班) / local(本地兜底顶班)。
        """
        now = time.time()
        cb_open = bool(self._cb_enabled) and now < float(self._cb_open_until or 0.0)

        def _recent(ts: float) -> bool:
            return bool(ts) and (now - ts) < self._DEGRADE_RECENT_SEC

        primary_recent = _recent(self._last_primary_ok_ts)
        pool_recent = _recent(self._last_pool_ok_ts)
        fb_recent = _recent(self._last_fb_ok_ts)
        degraded = cb_open or (not primary_recent and (pool_recent or fb_recent))
        primary_mode = str(getattr(self, "_primary_mode", None) or "cloud").strip().lower()
        if primary_mode not in ("cloud", "local", "local_only"):
            primary_mode = "cloud"
        if not degraded:
            return {"degraded": False, "mode": "primary", "primary": primary_mode}
        if pool_recent:
            mode = "pool"
        elif fb_recent:
            mode = "local"
        else:
            # 开路但尚无顶班出话：按可用降级链预告（池 > 本地）
            mode = ("pool" if self._pool_entries
                    else ("local" if (self._fb_client and self._fb_model) else "none"))
        return {"degraded": True, "mode": mode, "circuit_open": cb_open,
                "primary": primary_mode}

    def pool_status(self) -> List[Dict[str, Any]]:
        """备用 Key 池运行态快照（看板用）：每条目冷却状态与剩余秒数。不含任何密钥。"""
        now = time.time()
        out: List[Dict[str, Any]] = []
        for e in self._pool_entries:
            bad_until = float(e.get("bad_until") or 0.0)
            out.append({
                "name": str(e.get("name") or ""),
                "label": str(e.get("label") or ""),
                "model": str(e.get("model") or ""),
                "cooling": bad_until > now,
                "cooldown_remaining_sec": max(0, int(bad_until - now)),
            })
        return out

    _EMBED_FAIL_THRESHOLD = 3      # 连续失败达此数 → 熔断
    _EMBED_COOLDOWN_SEC = 120.0    # 熔断冷却窗口（窗口内不再连接，静默降级）

    def _note_embed_success(self) -> None:
        """一次成功即复位熔断状态（端点恢复 → 立刻回到向量召回）。"""
        if self._embed_fail_streak or self._embed_unreachable_until:
            self._embed_fail_streak = 0
            self._embed_unreachable_until = 0.0

    def embedding_status(self) -> Dict[str, Any]:
        """嵌入能力当前三态（L-6 B，2026-09-06；供学习队列等下游把「相似度未计算」与
        「真重复」分开——L-4 F 的接口）。

        ``unconfigured``＝根本没有可用的嵌入路径（无模型名 / 无端点且无对话客户端可回落）；
        ``circuit_open``＝配置了但连续失败进入冷却窗（``until`` 为窗口截止时间戳）；
        ``ready``＝可发请求（不保证成功）。``endpoints`` 只报地址不报密钥。
        """
        now = time.time()
        until = float(self._embed_unreachable_until or 0.0)
        if self._use_openai_compat:
            model = (self._embedding_model or "").strip()
            endpoints = [u for u, _ in self._embed_clients_ordered()]
            if not model or model.lower() in ("none", "off", "disabled") or not endpoints:
                return {"state": "unconfigured", "model": model, "endpoints": endpoints,
                        "fail_streak": 0, "until": 0.0}
        else:
            endpoints = ["gemini"] if self.client else []
            model = (self._embedding_model or "").strip()
            if not endpoints or not model:
                return {"state": "unconfigured", "model": model, "endpoints": endpoints,
                        "fail_streak": 0, "until": 0.0}
        if until and now < until:
            return {"state": "circuit_open", "model": model, "endpoints": endpoints,
                    "fail_streak": int(self._embed_fail_streak), "until": until}
        return {"state": "ready", "model": model, "endpoints": endpoints,
                "fail_streak": int(self._embed_fail_streak), "until": 0.0}

    #: 熔断日志里异常文本的截断长度（见 _embed_exc_brief 的「为什么」）
    _EMBED_EXC_BRIEF_CHARS = 220

    @classmethod
    def _embed_exc_brief(cls, exc: Optional[Exception]) -> str:
        """把嵌入异常压成一行可读摘要（剥 HTML、截断）。

        2026-08-28 B126 排障教训：端点回的是**整页 HTML**（网关缺 embeddings 路由
        时 Next.js 的 404 页、nginx 的 504 页）时，SDK 把整页塞进异常文本，于是
        backend.log 里每条熔断记录都是几 KB 的 `<!DOCTYPE html>…`——诊断包被撑大，
        而真正有用的那句「端点返回的是网页不是 JSON」反而要人工翻。剥标签 + 截断后
        一眼就能分清「路由缺失/反代超时」（HTML）与「连不上」（连接异常）。
        """
        if exc is None:
            return "unknown"
        text = f"{type(exc).__name__}: {exc}".strip()
        lowered = text.lower()
        if "<!doctype html" in lowered or "<html" in lowered:
            marker = "响应体是 HTML 页面（端点路由缺失 / 反代超时页），非 JSON"
            head = re.sub(r"<[^>]*>", " ", text)
            head = re.sub(r"\s+", " ", head).strip()
            text = f"{marker} | {head}"
        return (text[:cls._EMBED_EXC_BRIEF_CHARS] + "…"
                if len(text) > cls._EMBED_EXC_BRIEF_CHARS else text)

    def _note_embed_failure(self, exc: Exception,
                            endpoints: Optional[List[str]] = None) -> None:
        """记一次失败；达阈值则开熔断窗口并**只此时**打一条 WARNING（避免每条消息刷屏）。"""
        self._embed_fail_streak += 1
        brief = self._embed_exc_brief(exc)
        where = ", ".join(endpoints or []) or "n/a"
        if self._embed_fail_streak >= self._EMBED_FAIL_THRESHOLD:
            self._embed_unreachable_until = time.time() + self._EMBED_COOLDOWN_SEC
            self.logger.warning(
                "Embedding 连续失败 %d 次，熔断 %.0fs（期间降级关键词召回，零阻断）"
                "endpoints=[%s]: %s",
                self._embed_fail_streak, self._EMBED_COOLDOWN_SEC, where, brief)
        else:
            self.logger.debug(
                "Embedding API 调用失败(第%d次) endpoints=[%s]: %s",
                self._embed_fail_streak, where, brief)

    _EMBED_URL_COOLDOWN_SEC = 60.0   # 单端点异常后的冷却窗（排序降权，不剔除）

    def _embed_clients_ordered(self) -> List[Any]:
        """嵌入端点尝试序：健康端点按配置序在前，冷却中的降到队尾（仍保底可试）。

        未配独立嵌入端点 → 回落对话客户端（旧行为，单元素）。返回 [(url, client), ...]。
        """
        if not self._oa_embed_clients:
            return [("chat", self._oa_client)] if self._oa_client else []
        now = time.monotonic()
        healthy = [p for p in self._oa_embed_clients
                   if self._embed_url_bad_until.get(p[0], 0.0) <= now]
        cooling = [p for p in self._oa_embed_clients if p not in healthy]
        return healthy + cooling

    def _mark_embed_url_bad(self, url: str) -> None:
        self._embed_url_bad_until[url] = time.monotonic() + self._EMBED_URL_COOLDOWN_SEC

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """
        调用 Gemini Embedding API 获取文本向量。
        支持批量输入，返回顺序对应输入顺序。
        失败时返回空列表，调用方应做降级处理。
        """
        if not texts:
            return []
        # 熔断窗口内：直接静默返回空（调用方降级关键词召回），不再徒劳连接/刷日志。
        if self._embed_unreachable_until and time.time() < self._embed_unreachable_until:
            return []
        if self._use_openai_compat:
            # 未配置独立 embedding 端点且未指定模型时，跳过（避免向仅支持 chat 的 API 误请求 embeddings）
            _em = (self._embedding_model or "").strip()
            if not _em or _em.lower() in ("none", "off", "disabled"):
                return []
            _pairs = self._embed_clients_ordered()
            if not _pairs:
                return []
            _last_exc: Optional[Exception] = None
            for _url, _emb_cli in _pairs:
                try:
                    result = await _emb_cli.embeddings.create(
                        model=self._embedding_model,
                        input=texts,
                    )
                except Exception as _e:
                    # 单端点异常 → 冷却降权，立刻试下一端点（双活 failover）
                    _last_exc = _e
                    self._mark_embed_url_bad(_url)
                    continue
                self._note_embed_success()
                if result and result.data:
                    return [list(d.embedding) for d in result.data]
                return []
            # 全部端点失败 → 才计一次全局熔断 streak（单点抖动不触发全局熔断）
            self._note_embed_failure(
                _last_exc or RuntimeError("all embedding endpoints failed"),
                endpoints=[u for u, _ in _pairs])
            return []
        if not self.client:
            return []
        try:
            result = await self.client.aio.models.embed_content(
                model=self._embedding_model,
                contents=texts,
            )
            self._note_embed_success()
            if result and result.embeddings:
                return [emb.values for emb in result.embeddings]
            return []
        except Exception as _e:
            self._note_embed_failure(_e)
            return []

    async def embed_with_fallback(self, texts: List[str]) -> List[List[float]]:
        """
        批量嵌入；若 API 返回向量数与输入不一致，则逐条请求（兼容部分批处理行为）。
        返回列表长度与 texts 一致，失败位置为空列表。
        """
        if not texts:
            return []
        out = await self.embed(texts)
        if out and len(out) == len(texts):
            return out
        if len(texts) == 1:
            return out if out else [[]]
        self.logger.warning(
            "Embedding 批量返回数量=%s 与输入=%s 不一致，改为逐条请求",
            len(out) if out else 0, len(texts),
        )
        result: List[List[float]] = []
        for t in texts:
            one = await self.embed([t])
            result.append(one[0] if one else [])
            await asyncio.sleep(0.05)
        return result

    async def cleanup(self):
        """清理资源"""
        self.logger.info("AI 客户端清理完成")
