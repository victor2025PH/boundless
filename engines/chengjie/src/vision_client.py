"""
图像理解客户端：智谱 GLM-4V，或 OpenAI 兼容多模态（Ollama / 本地 Gemma 等）。
将图片转为文字描述，供下游 AI 生成回复。

多端点双活（``vision.base_urls``，2026-07）：两台 LAN GPU 各备同名 VLM，按序尝试、
异常端点 60s 冷却降权（模块级状态——VisionClient 实例按调用即建即弃，冷却须跨实例
生效）。所有消费方（TG/LINE/Messenger/WhatsApp RPA + 图片翻译）都经本类，自动获益。
"""

import asyncio
import base64
import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# zhipuai 懒加载（P3-2 2026-08-12 可靠性复盘）：顶层 import 实测 ~1.0s（-X importtime），
# 而智谱云兜底自 2026-07-12 key 失效起已停用（overlay vision.zhipu_api_key: ''）——
# 每个 import 本模块的进程（主程序 boot / RPA worker / CLI 工具）都在为死路径买单。
# 改为 _initialize_zhipu 内按需 import；本模块内无其他消费者（ZHIPU_AVAILABLE 原本
# 也只在本文件用，外部只 import VisionClient / has_any_vision_backend）。
ZhipuAI = None  # 懒加载占位：真实类由 _zhipu_import() 填充


def _zhipu_import():
    """按需 import zhipuai；未安装返回 None（与旧 ZHIPU_AVAILABLE=False 同语义）。"""
    global ZhipuAI
    if ZhipuAI is not None:
        return ZhipuAI
    try:
        from zhipuai import ZhipuAI as _Z
        ZhipuAI = _Z
    except ImportError:
        return None
    return ZhipuAI

try:
    from openai import OpenAI
    OPENAI_SYNC_AVAILABLE = True
except ImportError:
    OPENAI_SYNC_AVAILABLE = False
    OpenAI = None  # type: ignore

# 入站识图描述语义缓存（同字节图 + 同 model/prompt → 复用 VLM 描述，跳过秒级调用）。
# 软依赖：import 失败即降级为「不缓存」，绝不阻断识图。
try:
    from src.ai.media_text_cache import get_vision_desc_cache as _get_vision_cache, hash_file as _hash_file
except Exception:  # pragma: no cover - 仅在包路径异常时降级
    _get_vision_cache = None  # type: ignore
    _hash_file = None  # type: ignore


_LEGACY_INBOUND_PROMPT = (
    "请简要描述图中与聊天/文字相关的内容；若是聊天截图，说明最后一条对方消息大意。"
)


def default_inbound_image_prompt() -> str:
    """入站识图默认 prompt（#333 v3 人物导向；单一事实源 ``companion.visual_identity``）。

    软依赖：模块缺席时回落旧 v1 文案——识图链绝不因 prompt 模块导入失败而断。
    """
    try:
        from src.companion.visual_identity import INBOUND_IMAGE_PROMPT
        return INBOUND_IMAGE_PROMPT
    except Exception:
        return _LEGACY_INBOUND_PROMPT


def _backend_from_tag(tag: str) -> str:
    """从 fallback 链 debug tag 推断最终答话/最后尝试的后端名（观测用）。

    tag 是 ``|`` 级联（如 ``ollama_empty|zhipu_fallback``）——**最后一段**才是终态后端：
    末段 zhipu 前缀（zhipu_only / zhipu_fallback / zhipu_empty / zhipu_init_fail）→ zhipu；
    末段 ollama 前缀（含 ``ollama_empty_no_zhipu_key``——含 zhipu 字样但终态是 ollama）
    与遗留 vision_ok → ollama；链前失败（vision_client_init_fail）→ none。
    """
    last = (tag or "").lower().split("|")[-1]
    if last.startswith("zhipu"):
        return "zhipu"
    if last.startswith("ollama") or last == "vision_ok":
        return "ollama"
    return "none"


def _record_vision_label(label: str) -> None:
    """往 vision 观测记一条结果标签（空答换端点重试的 try/rescued 等）。软失败不阻断。"""
    try:
        from src.ai.provider_stats import get_provider_stats
        get_provider_stats("vision", "vision").record_label(label)
    except Exception:  # pragma: no cover
        pass


def _empty_retry_enabled(cfg: dict) -> bool:
    """解析 ``vision.empty_retry``：dict 形态读 enabled（默认 False），允许简写 bool。"""
    er = cfg.get("empty_retry")
    if isinstance(er, dict):
        return bool(er.get("enabled", False))
    return bool(er)


def _record_vision_stats(tag: str, ok: bool, latency_ms: int) -> None:
    """入站识图统一观测：接 P58 provider_stats（namespace=vision）。

    与坐席「图片翻译」的 namespace=ocr 是两个视角：ocr=业务（翻译服务质量），
    vision=基础设施（全平台 VLM 调用/成功率/延迟/缓存效果/云兜底），维度互补不冲突。
    经 all_provider_stats() 自动进 /api/workspace/metrics.providers.vision、
    all_provider_prom() 自动进 Prometheus（vision_attempts_total 等），零路由新增。
    观测绝不阻断识图：任何异常吞掉。
    """
    try:
        from src.ai.provider_stats import get_provider_stats
        st = get_provider_stats("vision", "vision")
        if tag == "cache_hit":
            st.record_cache_hit()
            return
        st.record(_backend_from_tag(tag), ok=ok, latency_ms=latency_ms)
        st.record_label(tag)  # 完整 tag 分布：ollama_empty 多=模型空答；zhipu_fallback 多=LAN 不稳
        if "zhipu_fallback" in (tag or ""):
            st.record_fallback()
    except Exception:  # pragma: no cover
        pass


_OPENAI_COMPAT_PROVIDERS = ("openai_compatible", "ollama", "openai", "local")


def _looks_like_zhipu_key(value: Any) -> bool:
    """一个 api_key 值能不能当智谱 key 用：排除占位符、ollama 假 key 与官网网关设备令牌。

    ``cx.`` 前缀＝hosted_gateway 注入到 ``vision.api_key`` 的**设备令牌**（#213 钧机实锤：
    它被当成智谱 key 送去 open.bigmodel.cn → 401「令牌已过期或验证不正确」，日志看起来像
    「智谱令牌过期」，其实是拿我们的令牌去敲别人的门——既泄露令牌又白等一轮超时）。
    """
    k = str(value or "").strip()
    if not k or k in ("YOUR_ZHIPU_API_KEY", "ollama"):
        return False
    if k.startswith("cx."):
        return False
    return True


def _zhipu_credentials(global_vision: dict, merged: dict) -> Optional[Dict[str, str]]:
    """从全局 vision 或合并配置中取智谱 key。支持 zhipu_api_key 专用于回退。

    ``api_key`` 只在该段 provider 是智谱（缺省）时才算智谱 key：provider 为 OpenAI 兼容
    （ollama / 网关）时它是那条端点自己的鉴权值（example 配置的契约就是「与 Ollama 并存
    时智谱 key 放 zhipu_api_key，api_key 可填 ollama」），拿去当智谱 key 只会 401。
    """
    gv = global_vision if isinstance(global_vision, dict) else {}
    m = merged if isinstance(merged, dict) else {}
    for d in (gv, m):
        zk = (d.get("zhipu_api_key") or "").strip()
        if _looks_like_zhipu_key(zk):
            model = (
                d.get("zhipu_model")
                or gv.get("model")
                or m.get("model")
                or "glm-4v-flash"
            )
            return {"api_key": zk, "model": str(model)}
    for d in (gv, m):
        prov = str(d.get("provider") or "zhipu").strip().lower()
        if prov in _OPENAI_COMPAT_PROVIDERS:
            continue
        k = (d.get("api_key") or "").strip()
        if _looks_like_zhipu_key(k):
            model = gv.get("model") or m.get("model") or "glm-4v-flash"
            return {"api_key": k, "model": str(model)}
    return None


def classify_vision_error(exc: BaseException) -> str:
    """把端点异常归成少数几类（进 debug tag 的 ``failed:<kind>`` 段，供人话文案与观测）。"""
    s = str(exc or "").lower()
    name = type(exc).__name__.lower()
    if "context size" in s or "exceed_context_size" in s or "context length" in s:
        return "context_overflow"
    if "401" in s or "403" in s or "unauthorized" in s or "authentication" in s:
        return "auth"
    if "429" in s or "rate limit" in s or "quota" in s:
        return "rate_limited"
    if "timeout" in name or "timed out" in s or "timeout" in s:
        return "timeout"
    if "connect" in name or "connection" in s or "unreachable" in s or "refused" in s:
        return "unreachable"
    if any(code in s for code in ("500", "502", "503", "504", "relay_5")):
        return "upstream"
    return "error"


def vision_failure_reason(tag: str) -> str:
    """把 fallback 链 debug tag 归成用户可读的三个原因码（#213 工作台「识别翻译不可用」拆分）。

    ``unconfigured``＝没有可用后端（init 失败 / 无 key）；``busy``＝有后端但调用失败
    （超时 / 不可达 / 上下文超限 / 上游 5xx / 鉴权）；``no_text``＝后端正常答复但没识出
    内容。任一段是 ``*_failed:*`` 即 busy——「服务在但没答成」比「图里没字」更接近真相。
    """
    t = str(tag or "").strip().lower()
    if not t:
        return "busy"
    segs = [s for s in t.split("|") if s]
    if any("_failed" in s for s in segs):
        return "busy"
    tried = [s for s in segs if s not in ("no_cloud_fallback",)]
    if tried and all(
        s in ("vision_client_init_fail", "ollama_unavailable", "zhipu_init_fail")
        for s in tried
    ):
        return "unconfigured"
    return "no_text"


def _vision_base_urls(cfg: dict) -> List[str]:
    """解析 base_urls（list 或逗号串）∪ base_url，去重保序。"""
    urls: List[str] = []
    raw_multi = cfg.get("base_urls")
    if isinstance(raw_multi, (list, tuple)):
        urls.extend(str(u or "").strip() for u in raw_multi)
    elif raw_multi:
        urls.extend(p.strip() for p in str(raw_multi).split(","))
    single = str(cfg.get("base_url") or "").strip()
    if single:
        urls.append(single)
    out: List[str] = []
    for u in urls:
        u = u.rstrip("/")
        if not u:
            continue
        if not u.endswith("/v1"):
            u = u + "/v1"
        if u not in out:
            out.append(u)
    return out


def _endpoint_model(cfg: dict, url: str, default: str = "llava") -> str:
    """每端点模型名（实施71 2026-08-27：混合供应商双活的唯一阻塞就是它）。

    ``vision.endpoint_models: {url片段: model}``——片段子串匹配（如 ``siliconflow`` /
    ``192.168.0.176``），未命中回落全局 ``model``。云主（硅基 ``Qwen/Qwen3-VL-8B-Instruct``）
    + LAN 备（ollama ``qwen3-vl:8b-instruct``）两家命名不同，全局单 model 会让回落端点
    拿到不存在的模型名。api_key 刻意不做每端点：ollama 忽略 Bearer，全局 key 给云端即可。"""
    m = cfg.get("endpoint_models")
    if isinstance(m, dict):
        for frag, name in m.items():
            f = str(frag or "").strip()
            n = str(name or "").strip()
            if f and n and f in str(url or ""):
                return n
    return str(cfg.get("model", default) or default)


def _endpoint_timeout(cfg: dict, url: str, default: float = 120.0) -> float:
    """每端点读超时秒数（2026-08-27：「5 秒没响应就切下一个」）。

    ``vision.endpoint_timeouts: {url片段: 秒数}``——与 ``endpoint_models`` 同样的片段
    子串匹配，未命中回落全局 ``timeout``。

    **为什么必须按端点、不能设全局**：主备两条路的正常耗时差一个数量级。2026-08-27
    生产参数实测（1536x1536 / 828KB / max_tokens=700）——LAN 中位 0.5s，云端中位 6.6s
    （降级时 8.8s+）。把全局 timeout 砍到 5s，主路如愿快切，**云端备胎却会 100% 超时**，
    等于把兜底整条废掉——「快速失败」和「有地方可退」必须分开配。

    「LAN 冷载会不会撞上 5s」——**实测否**（2026-08-27，先量后判）：176 的 ollama
    服务端默认 keep_alive 是永久（``/api/ps`` 显示 qwen3-vl 的 ``expires_at`` 为 2318 年
    ＝ ``-1``；而显式带 24h 的 hy-mt2 才显示 24h，两相对照即可确认默认值），本客户端
    又**刻意不带 keep_alive**（见 ``_ollama_native_images_request``，不覆写 176 侧的
    常驻钉决策）。所以模型不会被逐出，5s 对 LAN 只会命中「真的没响应」。
    万一那个常驻钉将来被撤：冷载超 5s 会失败并切云端（那一发慢但答得出来），ollama
    后台继续把模型载完，下一发即回到 0.2s——降级是软的，不会断服务。
    """
    m = cfg.get("endpoint_timeouts")
    if isinstance(m, dict):
        for frag, secs in m.items():
            f = str(frag or "").strip()
            if not f or f not in str(url or ""):
                continue
            try:
                v = float(secs)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
    try:
        return float(cfg.get("timeout", default) or default)
    except (TypeError, ValueError):
        return float(default)


def _wants_openai_primary(merged: dict) -> bool:
    prov = (merged.get("provider") or "zhipu").strip().lower()
    if prov not in ("openai_compatible", "ollama", "openai", "local"):
        return False
    return bool(_vision_base_urls(merged))


# 端点级冷却（跨实例共享；VisionClient 每次图片调用即建即弃，实例态存不住）
_URL_BAD_UNTIL: Dict[str, float] = {}
_URL_LOCK = threading.Lock()
_URL_COOLDOWN_SEC = 60.0


def _mark_url_bad(url: str) -> None:
    with _URL_LOCK:
        _URL_BAD_UNTIL[url] = time.time() + _URL_COOLDOWN_SEC


def _url_cooling(url: str) -> bool:
    with _URL_LOCK:
        return time.time() < _URL_BAD_UNTIL.get(url, 0.0)


def _record_vision_usage(resp: Any, *, url: str, model: str, t0: float) -> None:
    """识图调用成本记账（2026-09-08 成本对账）：云端 VLM（硅基 Qwen3-VL）按厂商 usage
    记真值；LAN 端点同样记（金额 0，但看板要看到调用量）。绝不抛。"""
    try:
        from src.ai.llm_cost import provider_from_base_url, record_usage_from_response
        record_usage_from_response(
            resp, model=str(model), purpose="vision",
            provider=provider_from_base_url(url) or "lan", tier="vision",
            latency_ms=int((time.time() - t0) * 1000))
    except Exception:
        pass


def has_any_vision_backend(merged: dict, global_vision: dict) -> bool:
    """至少存在一种可用后端：配置了 Ollama base_url，或存在有效智谱 api_key。"""
    if _wants_openai_primary(merged):
        return True
    gv = global_vision if isinstance(global_vision, dict) else {}
    return _zhipu_credentials(gv, merged) is not None


def _image_to_data_url(
    image_path: str,
    max_dim: Optional[int] = None,
    force_jpeg: bool = False,
) -> Optional[str]:
    """将本地图片转为 data URL（base64），供多模态 API 使用。

    max_dim: 若非 None，将图片最长边缩放至 ≤ max_dim（降低本地 VLM 显存压力）。
    force_jpeg: 强制经 PIL 重编码为 JPEG，**绕过 LM Studio 对 webp data URI 的已知
        bug**（lmstudio-ai/lmstudio-bug-tracker#1752/#1839：webp 前缀被拒、报
        "'url' field must be a base64 encoded image"；jpeg/png 正常）。

    只要 max_dim 或 force_jpeg 任一开启即走 PIL 重编码为 JPEG（**无论是否需要缩放**），
    确保发往本地 VLM 的图片统一是 jpeg —— 修复「小尺寸 webp（最长边 ≤ max_dim）跳过转码、
    以原始 webp 前缀发出触发上述 bug」的回归。PIL 不可用/解码失败时回落原始编码，
    并将 webp 标成 png 前缀（社区验证可被 LM Studio 正确解码）作为最后兜底。
    """
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) > 10 * 1024 * 1024:  # 10MB hard limit
            return None
        suffix = path.suffix.lower()
        if max_dim is not None or force_jpeg:
            try:
                import io
                from PIL import Image as _PILImage
                img = _PILImage.open(io.BytesIO(raw)).convert("RGB")
                if max_dim is not None:
                    w, h = img.size
                    if max(w, h) > max_dim:
                        scale = max_dim / max(w, h)
                        img = img.resize((int(w * scale), int(h * scale)), _PILImage.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
            except Exception:
                pass  # PIL 不可用/解码失败 → 回落下方原始编码（含 webp→png 兜底）
        b64 = base64.b64encode(raw).decode("ascii")
        mime = "image/jpeg"
        if suffix in (".png",):
            mime = "image/png"
        elif suffix in (".gif",):
            mime = "image/gif"
        elif suffix in (".webp",):
            mime = "image/png"  # LM Studio webp bug 兜底：webp 数据用 png 前缀发出
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


class VisionClient:
    """图像理解：provider=zhipu（智谱）或 openai_compatible（Ollama 等）。"""

    def __init__(self, config: dict):
        self.config = config
        self._client: Any = None  # ZhipuAI
        self._oa_sync: Any = None  # OpenAI sync client（首端点，向后兼容）
        self._oa_endpoints: List[Tuple[str, Any]] = []  # [(base_url, OpenAI client)]
        self._backend: str = "zhipu"
        # 最近一次描述调用的失败类别（classify_vision_error 输出；""＝没失败或只是空答）。
        # 端点循环把「全部端点抛异常」和「端点通但模型空答」区分开，fallback 链据此打 tag。
        self.last_fail: str = ""
        self.logger = logging.getLogger(__name__)

    def _get_zhipu(self) -> Optional[Any]:
        # _client 只在 _initialize_zhipu 成功（即 zhipuai 已加载）后才非空
        if not self._client:
            return None
        return self._client

    def initialize(self) -> bool:
        provider = (self.config.get("provider") or "zhipu").strip().lower()
        if provider in ("openai_compatible", "ollama", "openai", "local"):
            return self._initialize_openai_vision()
        return self._initialize_zhipu()

    def _initialize_openai_vision(self) -> bool:
        if not OPENAI_SYNC_AVAILABLE:
            self.logger.warning("openai 库未安装，Vision(Ollama) 不可用: pip install openai")
            return False
        urls = _vision_base_urls(self.config)
        if not urls:
            self.logger.warning(
                "vision provider=openai_compatible 需要 base_url(s)，例如 http://127.0.0.1:11434/v1"
            )
            return False
        key = (self.config.get("api_key") or "ollama").strip()
        if key in ("", "YOUR_ZHIPU_API_KEY"):
            key = "ollama"
        # 连接 5s 快败（防火墙丢包型死主机别吃满整体 timeout）；SDK 内建重试关掉——
        # 重试同一死端点不如立刻切下一个。**读超时逐端点算**（_endpoint_timeout）：
        # 主路要「5 秒没响应就切」，而云端备胎正常就要 6~9 秒，共用一个值必然二选一坏。
        self._oa_endpoints = []
        for base in urls:
            per = _endpoint_timeout(self.config, base,
                                    float(self.config.get("timeout", 120) or 120))
            try:
                import httpx
                eff_timeout: Any = httpx.Timeout(per, connect=min(5.0, per))
            except Exception:
                eff_timeout = per
            try:
                self._oa_endpoints.append(
                    (base, OpenAI(api_key=key, base_url=base,
                                  timeout=eff_timeout, max_retries=0))
                )
            except Exception as e:
                self.logger.warning("Vision 端点 %s 构建失败: %s", base, e)
        if not self._oa_endpoints:
            return False
        self._oa_sync = self._oa_endpoints[0][1]
        self._backend = "openai"
        self.logger.info(
            "Vision(OpenAI 兼容) 初始化成功 endpoints=%s model=%s",
            [u for u, _ in self._oa_endpoints],
            self.config.get("model", "?"),
        )
        return True

    def _initialize_zhipu(self) -> bool:
        # 先查 key 再 import：未配 key（本部署常态——云兜底已停用）连 1s 的
        # zhipuai import 都不必付。
        api_key = (self.config.get("api_key") or "").strip()
        if not api_key or api_key == "YOUR_ZHIPU_API_KEY":
            self.logger.warning("Vision 未配置 api_key，图像理解已禁用")
            return False
        _cls = _zhipu_import()
        if _cls is None:
            self.logger.warning("zhipuai 未安装，Vision 不可用。请执行: pip install zhipuai")
            return False
        try:
            self._client = _cls(api_key=api_key)
            self._backend = "zhipu"
            self.logger.info("智谱 GLM-4V Vision 客户端初始化成功")
            return True
        except Exception as e:
            self.logger.warning("智谱 Vision 初始化失败: %s", e)
            return False

    def describe_image_sync(
        self, image_path: str, prompt: Optional[str] = None,
        *, allow_empty_failover: bool = False,
    ) -> Optional[str]:
        """同步：根据本地图片路径得到文字描述。

        ``allow_empty_failover``：端点通但模型空答时是否换下一端点再试一次（最多 1 次）。
        默认 False=旧语义；仅入站识图链（describe_image_with_ollama_zhipu_fallback）按
        ``vision.empty_retry.enabled`` 传 True——UI 辅助任务（peer_typing/坐标校准/出图体检等
        高频轮询）恒走旧语义，防系统性空答把第二块 GPU 也拖进来。
        """
        if self._backend == "openai":
            return self._describe_openai_sync(
                image_path, prompt, allow_empty_failover=allow_empty_failover)
        return self._describe_zhipu_sync(image_path, prompt)

    def _describe_openai_sync(
        self, image_path: str, prompt: Optional[str] = None,
        *, allow_empty_failover: bool = False,
    ) -> Optional[str]:
        if not self._oa_endpoints:
            return None
        max_dim = self.config.get("max_image_dim")
        if max_dim is None:
            max_dim = 800  # default: resize to 800px max for local VLMs
        data_url = _image_to_data_url(image_path, max_dim=int(max_dim), force_jpeg=True)
        if not data_url:
            self.logger.warning("图片转 base64 失败或文件过大")
            return None
        # #333（2026-09-17）：旧默认「描述与聊天/文字相关的内容；若是聊天截图…」面向
        # 截图/OCR——客户自拍只落一句「并非聊天截图…一位红发男性」，没有主体/人数/
        # 是否自拍可供下游消费。默认改用 v3 人物导向 prompt（首行「类型=A|B|C」契约不变）；
        # 单一事实源 src/companion/visual_identity.INBOUND_IMAGE_PROMPT，配置 prompt 仍最高优先。
        text_prompt = (prompt or self.config.get("prompt") or default_inbound_image_prompt()).strip()
        content = [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": text_prompt},
        ]
        return self._openai_vision_request(
            content, allow_empty_failover=allow_empty_failover)

    def describe_images_sync(
        self, image_paths: List[str], prompt: Optional[str] = None,
        *, allow_empty_failover: bool = False,
    ) -> Optional[str]:
        """多图同请求（同一段视频按时间序抽出的帧列表等）。

        仅 OpenAI 兼容后端支持（qwen*-vl 原生多图）；zhipu 云兜底不支持 →
        返回 None，调用方回落宫格单图路径。每帧按 ``vision.video_frame_dim``
        （默认 640）独立缩放——比宫格把 N 帧挤进一张图清晰，且 4×640 约 3k
        tokens，收在 Ollama /v1 默认 n_ctx=4096 内（2026-08-15 实测 6×768=6322
        tokens 会 400 超窗；/v1 兼容层不认 options.num_ctx，只能客户端收敛预算）。
        <2 张有效帧不值得走多图，返回 None。
        """
        if self._backend != "openai" or not self._oa_endpoints:
            return None
        try:
            frame_dim = int(self.config.get("video_frame_dim", 640) or 640)
        except Exception:
            frame_dim = 640
        data_urls: List[str] = []
        for p in list(image_paths or []):
            du = _image_to_data_url(str(p), max_dim=frame_dim, force_jpeg=True)
            if du:
                data_urls.append(du)
        if len(data_urls) < 2:
            return None
        text_prompt = (prompt or self.config.get("prompt") or "请描述这组图片。").strip()
        # Ollama 端点（:11434）走原生 /api/chat：它认 options.num_ctx——qwen3-vl 在
        # Ollama 上每图有 ~1039 token 的地板（客户端缩图无效，2026-08-15 实测 4 图
        # 4244 > 默认 n_ctx 4096），/v1 兼容层又不认 options，原生口是唯一解。
        # 非 Ollama 的 /v1 端点（vLLM 等）仍走 SDK 多图 content。逐端点按健康序尝试。
        healthy = [(u, c) for u, c in self._oa_endpoints if not _url_cooling(u)]
        cooling = [(u, c) for u, c in self._oa_endpoints if _url_cooling(u)]
        raws = [du.split("base64,", 1)[1] for du in data_urls]
        for url, cli in healthy + cooling:
            root = url[:-3].rstrip("/") if url.endswith("/v1") else url.rstrip("/")
            try:
                if ":11434" in root:
                    out = self._ollama_native_images_request(
                        root, raws, text_prompt)
                else:
                    content: List[dict] = [
                        {"type": "image_url", "image_url": {"url": du}}
                        for du in data_urls
                    ]
                    content.append({"type": "text", "text": text_prompt})
                    _t0 = time.time()
                    _mdl = _endpoint_model(self.config, url, "llava")
                    resp = cli.chat.completions.create(
                        model=_mdl,
                        messages=[{"role": "user", "content": content}],
                        max_tokens=int(self.config.get("max_tokens") or 300),
                        temperature=0,
                    )
                    _record_vision_usage(resp, url=url, model=_mdl, t0=_t0)
                    out = None
                    if resp and getattr(resp, "choices", None):
                        c0 = getattr(resp.choices[0].message, "content", None)
                        out = c0.strip() if isinstance(c0, str) else None
            except Exception as e:
                _mark_url_bad(url)
                self.logger.warning("Vision 多图端点 %s 调用失败(切换下一端点): %s",
                                    url, e)
                continue
            if out and out.strip():
                return out.strip()[:2000]
            # 多图空答不换端点（同模型大概率同样空）——直接交调用方回落宫格
            return None
        return None

    def _ollama_native_images_request(
        self, root: str, images_b64: List[str], prompt: str,
    ) -> Optional[str]:
        """Ollama 原生 /api/chat 多图请求（options.num_ctx 生效；keep_alive 刻意
        不带——请求会继承模型当前驻留设置，不覆写 176 侧的常驻钉决策）。"""
        import httpx
        try:
            num_ctx = int(self.config.get("video_multi_num_ctx", 8192) or 8192)
        except Exception:
            num_ctx = 8192
        payload = {
            "model": _endpoint_model(self.config, root, "llava"),
            "messages": [{"role": "user", "content": prompt,
                          "images": list(images_b64)}],
            "stream": False,
            "options": {
                "num_ctx": num_ctx,
                "temperature": 0,
                "num_predict": int(self.config.get("max_tokens") or 300),
            },
        }
        timeout = _endpoint_timeout(self.config, root,
                                    float(self.config.get("timeout", 120) or 120))
        r = httpx.post(root + "/api/chat", json=payload,
                       timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)))
        r.raise_for_status()
        data = r.json()
        out = ((data.get("message") or {}).get("content") or "").strip()
        return out or None

    async def describe_images(
        self, image_paths: List[str], prompt: Optional[str] = None,
        *, allow_empty_failover: bool = False,
    ) -> Optional[str]:
        """异步封装：线程池执行多图同步调用（与 describe_image 同模式）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.describe_images_sync(
                image_paths, prompt, allow_empty_failover=allow_empty_failover),
        )

    def _openai_vision_request(
        self, content: List[dict], *, allow_empty_failover: bool = False,
    ) -> Optional[str]:
        """单/多图共用的端点循环：健康优先→冷却殿后→空答按开关最多换 1 端点。"""
        # 健康端点在前，冷却中的殿后（全冷却时仍会硬试，避免全灭期彻底不服务）
        healthy = [(u, c) for u, c in self._oa_endpoints if not _url_cooling(u)]
        cooling = [(u, c) for u, c in self._oa_endpoints if _url_cooling(u)]
        endpoints = healthy + cooling
        empty_failovers_left = 1 if allow_empty_failover else 0
        empty_failover_used = False
        self.last_fail = ""
        answered = False      # 任一端点返回过响应（哪怕空答）→ 整链按「空答」而非「失败」计
        last_exc_kind = ""
        for i, (url, cli) in enumerate(endpoints):
            try:
                _t0 = time.time()
                _mdl = _endpoint_model(self.config, url, "llava")
                resp = cli.chat.completions.create(
                    model=_mdl,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=int(self.config.get("max_tokens") or 300),
                    # 识图是抽取任务不是创作任务：贪心解码保确定性（2026-07-26 实锤：
                    # 默认采样温度下同图偶发漏抄 Name 等字段——同图同 prompt 应同答）。
                    temperature=0,
                )
                _record_vision_usage(resp, url=url, model=_mdl, t0=_t0)
            except Exception as e:
                _mark_url_bad(url)
                last_exc_kind = classify_vision_error(e)
                self.logger.warning("Vision 端点 %s 调用失败(切换下一端点, %s): %s",
                                    url, last_exc_kind, e)
                if not answered:
                    self.last_fail = last_exc_kind
                continue
            answered = True
            self.last_fail = ""
            if resp and getattr(resp, "choices", None) and len(resp.choices) > 0:
                out = getattr(resp.choices[0].message, "content", None)
                if out and isinstance(out, str) and out.strip():
                    if empty_failover_used:
                        _record_vision_label("empty_failover_rescued")
                    return out.strip()[:2000]
            # 端点通但模型空答：默认保持旧语义直接返回（同模型换端点大概率同样空，
            # 别白烧第二块 GPU）；empty_failover 开且后面还有端点时最多换 1 个端点再试
            # ——空答也可能是端点瞬时负载/截断，LAN 双活下挽回成本可控。空答不算端点
            # 故障，不进冷却（transport 层是健康的）。
            if empty_failovers_left > 0 and i + 1 < len(endpoints):
                empty_failovers_left -= 1
                empty_failover_used = True
                _record_vision_label("empty_failover_try")
                self.logger.info("Vision 端点 %s 空答，换下一端点重试(empty_retry)", url)
                continue
            return None
        return None

    def _describe_zhipu_sync(
        self, image_path: str, prompt: Optional[str] = None
    ) -> Optional[str]:
        client = self._get_zhipu()
        if not client:
            return None
        data_url = _image_to_data_url(image_path, force_jpeg=True)
        if not data_url:
            self.logger.warning("图片转 base64 失败或文件过大")
            return None
        model = self.config.get("model", "glm-4v-flash")
        timeout = int(self.config.get("timeout", 30))
        self.last_fail = ""
        default_prompt = (
            "请按以下格式描述，便于作为查单依据使用。"
            "1) 银行/账单类型：哪个银行或支付渠道（如 EasyPaisa、银行转账、平台订单等）。"
            "2) 唯一识别依据：能唯一标识该笔交易/订单的字段与取值（如 Transaction ID、订单号、参考号）。"
            "3) 其他：金额、币种、时间、付款方/收款方等。只写图中出现的内容，不要编造。"
        )
        text_prompt = (prompt or self.config.get("prompt") or default_prompt).strip()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": text_prompt},
                        ],
                    }
                ],
                max_tokens=1024,
                timeout=timeout,
                temperature=0,   # 抽取任务贪心解码（与 openai 兼容路径同口径）
            )
            _record_vision_usage(resp, url="https://open.bigmodel.cn", model=str(model),
                                 t0=time.time())
            if resp and getattr(resp, "choices", None) and len(resp.choices) > 0:
                content = getattr(resp.choices[0].message, "content", None)
                if content and isinstance(content, str) and content.strip():
                    return content.strip()[:2000]
            return None
        except Exception as e:
            self.last_fail = classify_vision_error(e)
            self.logger.warning("智谱 Vision 调用失败(%s): %s", self.last_fail, e)
            return None

    async def describe_image(
        self, image_path: str, prompt: Optional[str] = None,
        *, allow_empty_failover: bool = False,
    ) -> Optional[str]:
        """异步封装：在线程池中执行同步调用，避免阻塞事件循环。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.describe_image_sync(
                image_path, prompt, allow_empty_failover=allow_empty_failover),
        )

    @staticmethod
    def _vision_cache_handle(
        merged: dict, image_path: str, prompt: Optional[str]
    ) -> Tuple[Optional[Any], str]:
        """算入站识图缓存句柄与键。

        返回 ``(cache_or_None, key_or_"")``。缓存不可用/被关闭/文件不可 hash → ``(None, "")``。
        键 = ``f"{图片内容 sha1}:{sha1(model + \\x00 + prompt)[:16]}"``——图片内容变、model
        热切换或 prompt 变化都不会误命中；同字节图 + 同 model/prompt 才复用描述。
        """
        if _get_vision_cache is None or _hash_file is None:
            return None, ""
        cache_cfg = merged.get("cache") if isinstance(merged.get("cache"), dict) else {}
        if not bool(cache_cfg.get("enabled", True)):
            return None, ""
        img_h = _hash_file(image_path)
        if not img_h:
            return None, ""
        model = str(merged.get("model") or "")
        p_norm = (prompt or "").strip()
        sig = hashlib.sha1(
            (model + "\x00" + p_norm).encode("utf-8", "ignore")
        ).hexdigest()[:16]
        return _get_vision_cache(), f"{img_h}:{sig}"

    @classmethod
    async def _describe_fallback_chain(
        cls,
        merged: dict,
        gv: dict,
        image_path: str,
        prompt: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        """Ollama/OpenAI 兼容端优先 → 空/失败回退智谱（原 fallback 语义，逐字保留）。

        tag 语义（#213 起区分「失败」与「空答」）：``ollama_failed:<kind>`` / ``zhipu_failed:<kind>``
        ＝端点全部抛异常（kind 见 classify_vision_error）；``ollama_empty`` / ``zhipu_empty``
        ＝端点通、模型没答出内容。前缀不变，``_backend_from_tag`` 归因照旧。
        """
        if not _wants_openai_primary(merged):
            vc = cls(merged)
            if not vc.initialize():
                return None, "vision_client_init_fail"
            txt = await vc.describe_image(image_path, prompt=prompt)
            if vc._backend != "zhipu":
                return txt, "vision_ok"
            if not (txt or "").strip() and vc.last_fail:
                return None, f"zhipu_failed:{vc.last_fail}"
            return txt, "zhipu_only"

        vc_o = cls(merged)
        ollama_ok = vc_o.initialize()
        txt: Optional[str] = None
        if ollama_ok:
            txt = await vc_o.describe_image(
                image_path, prompt=prompt,
                allow_empty_failover=_empty_retry_enabled(merged))
        if not ollama_ok:
            dbg = "ollama_unavailable"
        elif (txt or "").strip():
            dbg = "ollama_ok"
        elif vc_o.last_fail:
            dbg = f"ollama_failed:{vc_o.last_fail}"
        else:
            dbg = "ollama_empty"

        if (txt or "").strip():
            return txt.strip(), dbg

        # 无兜底纪律硬闸（2026-08-17 老板拍板）：vision.no_cloud_fallback=true 时
        # 智谱云回落**结构性关闭**——此前只靠 overlay 清空 zhipu_api_key 软死，
        # 谁贴回一把 key 兜底就复活（土地雷）。开闸后失败如实返 None，
        # 由调用方按「没看懂图就不装懂」处置（跳过自动回复 + delivery_block）。
        if bool(merged.get("no_cloud_fallback") or gv.get("no_cloud_fallback")):
            return None, f"{dbg}|no_cloud_fallback"

        creds = _zhipu_credentials(gv, merged)
        if not creds:
            if not ollama_ok or dbg.startswith("ollama_failed"):
                return None, dbg
            return None, "ollama_empty_no_zhipu_key"

        zcfg = {
            **merged,
            "provider": "zhipu",
            "api_key": creds["api_key"],
            "model": creds["model"],
        }
        zcfg.pop("base_url", None)
        vc_z = cls(zcfg)
        if not vc_z.initialize():
            return None, f"{dbg}|zhipu_init_fail"
        ztxt = await vc_z.describe_image(image_path, prompt=prompt)
        if (ztxt or "").strip():
            return ztxt.strip(), f"{dbg}|zhipu_fallback"
        if vc_z.last_fail:
            return None, f"{dbg}|zhipu_failed:{vc_z.last_fail}"
        return None, f"{dbg}|zhipu_empty"

    @classmethod
    async def describe_image_with_ollama_zhipu_fallback(
        cls,
        merged_config: dict,
        global_vision: dict,
        image_path: str,
        prompt: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        """
        优先 Ollama/OpenAI 兼容端；初始化失败、调用失败或空结果时，若配置了智谱 key 则回退智谱。
        global_vision 用于在 line_rpa 覆盖 provider 时仍能读到全局 vision.api_key。

        外层带**入站识图语义缓存**：同一张图（内容 sha1）+ 同 model/prompt 的描述直接复用，
        跳过秒级 VLM 调用（命中返回 debug=``cache_hit``）。仅缓存**成功**结果——失败/空
        （端点抖动、模型空答）不写缓存，端点恢复后可重试。可用 ``vision.cache.enabled: false`` 关。
        """
        merged = dict(merged_config) if merged_config else {}
        gv = global_vision if isinstance(global_vision, dict) else {}

        cache, ckey = cls._vision_cache_handle(merged, image_path, prompt)
        if cache is not None and ckey:
            cached = cache.get(ckey)
            if cached:
                _record_vision_stats("cache_hit", True, 0)
                return cached, "cache_hit"

        t0 = time.monotonic()
        txt, dbg = await cls._describe_fallback_chain(merged, gv, image_path, prompt)
        _record_vision_stats(
            dbg, bool((txt or "").strip()), int((time.monotonic() - t0) * 1000)
        )

        if cache is not None and ckey and (txt or "").strip():
            cache.put(ckey, txt)
        return txt, dbg


def vision_cache_stats() -> Dict[str, Any]:
    """入站识图描述缓存观测快照：{hits, misses, size, max, hit_rate}；缓存不可用返回全 0。"""
    if _get_vision_cache is None:
        return {"hits": 0, "misses": 0, "size": 0, "max": 0, "hit_rate": 0.0}
    try:
        return _get_vision_cache().stats()
    except Exception:  # pragma: no cover
        return {"hits": 0, "misses": 0, "size": 0, "max": 0, "hit_rate": 0.0}
