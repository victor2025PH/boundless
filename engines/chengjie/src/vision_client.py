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

try:
    from zhipuai import ZhipuAI
    ZHIPU_AVAILABLE = True
except ImportError:
    ZHIPU_AVAILABLE = False
    ZhipuAI = None

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


def _zhipu_credentials(global_vision: dict, merged: dict) -> Optional[Dict[str, str]]:
    """从全局 vision 或合并配置中取智谱 key（排除占位符与 ollama）。支持 zhipu_api_key 专用于回退。"""
    gv = global_vision if isinstance(global_vision, dict) else {}
    m = merged if isinstance(merged, dict) else {}
    for d in (gv, m):
        zk = (d.get("zhipu_api_key") or "").strip()
        if zk and zk not in ("YOUR_ZHIPU_API_KEY",):
            model = (
                d.get("zhipu_model")
                or gv.get("model")
                or m.get("model")
                or "glm-4v-flash"
            )
            return {"api_key": zk, "model": str(model)}
    for d in (gv, m):
        k = (d.get("api_key") or "").strip()
        if k and k not in ("YOUR_ZHIPU_API_KEY", "ollama"):
            model = gv.get("model") or m.get("model") or "glm-4v-flash"
            return {"api_key": k, "model": str(model)}
    return None


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
        self.logger = logging.getLogger(__name__)

    def _get_zhipu(self) -> Optional[Any]:
        if not ZHIPU_AVAILABLE or not self._client:
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
        timeout = float(self.config.get("timeout", 120))
        # LAN 多端点：连接 5s 快败（防火墙丢包型死主机别吃满整体 timeout），读超时留足
        # （备端点冷载模型可要 2min+）；SDK 内建重试关掉——重试同一死端点不如立刻切下一个。
        try:
            import httpx
            eff_timeout: Any = httpx.Timeout(timeout, connect=5.0)
        except Exception:
            eff_timeout = timeout
        self._oa_endpoints = []
        for base in urls:
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
        if not ZHIPU_AVAILABLE:
            self.logger.warning("zhipuai 未安装，Vision 不可用。请执行: pip install zhipuai")
            return False
        api_key = (self.config.get("api_key") or "").strip()
        if not api_key or api_key == "YOUR_ZHIPU_API_KEY":
            self.logger.warning("Vision 未配置 api_key，图像理解已禁用")
            return False
        try:
            self._client = ZhipuAI(api_key=api_key)
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
        model = self.config.get("model", "llava")
        default_prompt = (
            "请简要描述图中与聊天/文字相关的内容；若是聊天截图，说明最后一条对方消息大意。"
        )
        text_prompt = (prompt or self.config.get("prompt") or default_prompt).strip()
        # 健康端点在前，冷却中的殿后（全冷却时仍会硬试，避免全灭期彻底不服务）
        healthy = [(u, c) for u, c in self._oa_endpoints if not _url_cooling(u)]
        cooling = [(u, c) for u, c in self._oa_endpoints if _url_cooling(u)]
        endpoints = healthy + cooling
        empty_failovers_left = 1 if allow_empty_failover else 0
        empty_failover_used = False
        for i, (url, cli) in enumerate(endpoints):
            try:
                resp = cli.chat.completions.create(
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
                    max_tokens=int(self.config.get("max_tokens") or 300),
                )
            except Exception as e:
                _mark_url_bad(url)
                self.logger.warning("Vision 端点 %s 调用失败(切换下一端点): %s", url, e)
                continue
            if resp and getattr(resp, "choices", None) and len(resp.choices) > 0:
                content = getattr(resp.choices[0].message, "content", None)
                if content and isinstance(content, str) and content.strip():
                    if empty_failover_used:
                        _record_vision_label("empty_failover_rescued")
                    return content.strip()[:2000]
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
            )
            if resp and getattr(resp, "choices", None) and len(resp.choices) > 0:
                content = getattr(resp.choices[0].message, "content", None)
                if content and isinstance(content, str) and content.strip():
                    return content.strip()[:2000]
            return None
        except Exception as e:
            self.logger.warning(f"智谱 Vision 调用失败: {e}")
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
        """Ollama/OpenAI 兼容端优先 → 空/失败回退智谱（原 fallback 语义，逐字保留）。"""
        if not _wants_openai_primary(merged):
            vc = cls(merged)
            if not vc.initialize():
                return None, "vision_client_init_fail"
            txt = await vc.describe_image(image_path, prompt=prompt)
            return txt, "zhipu_only" if vc._backend == "zhipu" else "vision_ok"

        vc_o = cls(merged)
        ollama_ok = vc_o.initialize()
        txt: Optional[str] = None
        if ollama_ok:
            txt = await vc_o.describe_image(
                image_path, prompt=prompt,
                allow_empty_failover=_empty_retry_enabled(merged))
        dbg = "ollama_unavailable" if not ollama_ok else ("ollama_empty" if not (txt or "").strip() else "ollama_ok")

        if (txt or "").strip():
            return txt.strip(), dbg

        creds = _zhipu_credentials(gv, merged)
        if not creds:
            return None, dbg if not ollama_ok else "ollama_empty_no_zhipu_key"

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
