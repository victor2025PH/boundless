"""托管 AI 网关客户端：向官网换设备令牌，注入 ``ai.*``（用户永不配云 Key）。

流程：
  桌面/托管态 + 本地 api_key 仍为空/占位
    → POST {site}/api/ai/device-token {fingerprint}
    → 内存写入 ai.api_key=设备令牌、ai.base_url=官网 /api/ai/v1、ai._hosted_trial=True
  真云 Key 只在官网进程（DEEPSEEK_API_KEY），不下发。

关键设计（P0 加固，2026-07-29）：
- **热重载存活**：令牌同时写进程 env ``AITR_HOSTED_AI_*`` ——
  ``ConfigManager._apply_env_overrides`` 每次 load()/热重载都会重放注入，
  否则任何 config 热重载都会把内存令牌抹回空 Key（实测风险）。
- **base_url 不信服务端回传**：一律用本地配置的 site_url 拼 ``/api/ai/v1``，
  防反代环境下服务端 origin 解析成 127.0.0.1 把客户端指去打不通的地址。
- **资格闸**：服务端只对试用台账里的指纹发令牌（error=no_claim）——
  首启领试用（licensing trial-claim）后由 license 路由钩子立即重试接入。
- **刷新余量**：令牌 30 天 TTL；距过期 <7 天即换新；网络失败但旧令牌仍有效则继续用。
- **运维直注不接管**：外部已 set AITR_HOSTED_AI_KEY 且与本模块缓存不同 → 视为
  运维手工注入，本模块不碰。

失败软降级：保持空 Key，橙条「去配置」仍可用（自建用户可贴自有 Key）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_SITE = "https://bd2026.cc"
HTTP_TIMEOUT = 8
STATE_FILENAME = "hosted_ai_token.json"
#: 距过期不足此秒数即主动换新（服务端 TTL 30 天，留 7 天余量）
REFRESH_MARGIN_SEC = 7 * 24 * 3600
#: 令牌至少还有这么久才算「仍可用」（网络失败时的回落判据）
MIN_VALID_SEC = 3600

Fetch = Callable[[str, str, Optional[dict]], Dict[str, Any]]

_daemon_lock = threading.Lock()
_daemon_started = False

#: 进程级额度快照缓存（60s TTL，绿条轮询不打爆官网）
_quota_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

#: 认证失败触发的强制换新：120s 冷却防「坏令牌 × 每条消息」打爆官网
_forced_lock = threading.Lock()
_last_forced_ts = 0.0
FORCED_REFRESH_COOLDOWN_SEC = 120.0


def _http(url: str, method: str = "GET", body: Optional[dict] = None,
          bearer: str = "") -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    if bearer:
        req.add_header("authorization", f"Bearer {bearer}")
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8") or "{}") or {
                "ok": False, "error": f"http_{e.code}",
            }
        except Exception:
            return {"ok": False, "error": f"http_{e.code}"}
    except Exception as e:  # noqa: BLE001
        logger.debug("[hosted-ai] 请求失败 %s: %s", url, e)
        return {"ok": False, "error": "network"}


def _site_url(config: Optional[dict]) -> str:
    cfg = ((config or {}).get("licensing") or {}).get("trial") or {}
    hosted = ((config or {}).get("licensing") or {}).get("hosted_ai") or {}
    return str(
        hosted.get("site_url") or cfg.get("site_url") or DEFAULT_SITE
    ).rstrip("/")


def _gateway_base(config: Optional[dict]) -> str:
    """网关 base_url —— 永远本地拼（不信服务端回传，防反代 origin 污染）。"""
    return f"{_site_url(config)}/api/ai/v1"


def _wants_hosted(config: Optional[dict]) -> bool:
    """桌面/托管版默认开；自建服可配 licensing.hosted_ai.enabled: false 关掉。"""
    hosted = ((config or {}).get("licensing") or {}).get("hosted_ai") or {}
    if "enabled" in hosted:
        return bool(hosted.get("enabled"))
    env = str(os.environ.get("AITR_MANAGED_EDITION") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    desktop = str(os.environ.get("AITR_DESKTOP_MODE") or "").strip().lower()
    return desktop in ("1", "true", "yes", "on")


def _state_path(config_manager: Any):
    try:
        from pathlib import Path
        root = Path(getattr(config_manager, "config_path", "") or "").parent
        if str(root):
            return root / STATE_FILENAME
    except Exception:
        pass
    return None


def _load_cached(path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cached(path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug("[hosted-ai] 令牌缓存写盘失败: %s", e)


def fetch_device_token(
    *,
    config: Optional[dict] = None,
    fetch: Optional[Fetch] = None,
) -> Dict[str, Any]:
    """向官网换设备令牌。返回 ``{ok, token, base_url, model, exp, ...}``。

    服务端资格闸：指纹须已领试用（error=no_claim → 首启领取后重试）。
    """
    f = fetch or _http
    from src.licensing.machine_bridge import machine_fingerprint

    fp = machine_fingerprint()
    if not fp:
        return {"ok": False, "error": "no_fingerprint"}
    resp = f(
        f"{_site_url(config)}/api/ai/device-token",
        "POST",
        {"fingerprint": fp, "product": "chatx", "source": "desktop"},
    )
    if not resp.get("ok") or not resp.get("token"):
        return {"ok": False, "error": str(resp.get("error") or "token_failed")}
    return {
        "ok": True,
        "token": str(resp["token"]),
        # 刻意忽略 resp["base_url"]：见模块头「base_url 不信服务端回传」
        "base_url": _gateway_base(config),
        "model": str(resp.get("model") or ""),
        "exp": int(resp.get("exp") or 0),
        "fingerprint": fp,
    }


def ensure_hosted_telegram(config_manager: Any, *, fetch: Optional[Fetch] = None) -> bool:
    """托管版：向官网凭据池领一组 Telegram api_id/api_hash 注入 ``telegram.*``。

    「用户只登录、不填 ID/Hash」的公网解——集团 LAN 凭据池只绑 localhost 够不着，
    改由 bd2026.cc 网关按设备令牌粘定派发（同机恒定同 api_id，与 pyrogram session
    绑定不变量一致）。仅托管态 + 本地 api_id 为空时注入；用户自填的凭据永不覆盖。

    池未配（服务端 503 pool_disabled）/ 网络失败 → 静默不注入，向导回落「自备凭据」
    旧流程，零副作用。返回是否注入成功。
    """
    cfg = getattr(config_manager, "config", None) or {}
    if not _wants_hosted(cfg):
        return False
    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    # 用户已自填（或此前已注入过）→ 不动
    if str(tg.get("api_id") or "").strip() and str(tg.get("api_hash") or "").strip():
        return True

    f = fetch or _http
    from src.licensing.machine_bridge import machine_fingerprint

    fp = machine_fingerprint()
    if not fp:
        return False
    # 复用已缓存的设备令牌做鉴权（没有也行，服务端可凭指纹+台账放行）
    path = _state_path(config_manager)
    cached = _load_cached(path) if path is not None else {}
    token = str(cached.get("token") or (cfg.get("ai") or {}).get("api_key") or "")
    bearer = token if str(token).startswith("cx.") else ""
    resp = f(f"{_site_url(cfg)}/api/pool/telegram-cred", "POST",
             {"fingerprint": fp}, bearer) if bearer else f(
        f"{_site_url(cfg)}/api/pool/telegram-cred", "POST", {"fingerprint": fp})
    if not resp.get("ok") or not resp.get("api_id") or not resp.get("api_hash"):
        err = str(resp.get("error") or "")
        if err and err not in ("pool_disabled", "network"):
            logger.info("[hosted-tg] 凭据派发未成功：%s（回落自备凭据流程）", err)
        return False

    if not isinstance(cfg.get("telegram"), dict):
        cfg["telegram"] = {}
    cfg["telegram"]["api_id"] = str(resp["api_id"])
    cfg["telegram"]["api_hash"] = str(resp["api_hash"])
    cfg["telegram"]["_hosted_cred"] = True
    # 🔒 只记 api_id 与来源，绝不记 api_hash（与 LAN 池同口径）
    logger.info("[hosted-tg] 已注入托管 Telegram 凭据 api_id=%s name=%s（用户无需申请）",
                resp["api_id"], resp.get("name") or "?")
    return True


def ensure_hosted_vision(config_manager: Any, *, fetch: Optional[Fetch] = None) -> bool:
    """托管版：把识图（VLM）指向官网网关（我们自己的 GPU 识图模型，非云端大模型）。

    问题 #2 的公网解——集团识图 GPU（176/140）在内网，公网够不着；改由 bd2026.cc 网关
    经 117→VPS 反向隧道转发到 176。注入 ``vision.*``：base_url=网关 /api/ai/v1，
    provider=openai_compatible，model=qwen2.5vl:7b，api_key=设备令牌，enabled=true。

    仅托管态 + 用户未自配识图后端时注入（自建/已配 base_url 的不覆盖）。
    需已持有设备令牌（ai.api_key=cx.…）——识图与聊天共用同一枚令牌鉴权。
    """
    cfg = getattr(config_manager, "config", None) or {}
    if not _wants_hosted(cfg):
        return False
    v = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
    # 用户已自配识图后端（base_url/base_urls/智谱 key）→ 不动
    if (str(v.get("base_url") or "").strip() or v.get("base_urls")
            or str(v.get("api_key") or "").strip()):
        # 但若是本模块此前注入的（_hosted_vision）则允许续期刷新
        if not v.get("_hosted_vision"):
            return False

    ai = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    token = str(ai.get("api_key") or "")
    if not token.startswith("cx."):
        return False  # 还没拿到设备令牌（识图与聊天共用），等 ensure_hosted_ai 先成

    # 规范 VLM：两台中继（176/140）都装 qwen3-vl:8b-instruct → 网关可任意分流。
    # 网关侧还会统一改写 model，故此值只影响日志/回显，不会造成中继找不到模型。
    model = (((cfg.get("licensing") or {}).get("hosted_ai") or {}).get("vision_model")
             or "qwen3-vl:8b-instruct")
    if not isinstance(cfg.get("vision"), dict):
        cfg["vision"] = {}
    cfg["vision"]["enabled"] = True
    cfg["vision"]["provider"] = "openai_compatible"
    cfg["vision"]["base_url"] = _gateway_base(cfg)  # https://bd2026.cc/api/ai/v1
    cfg["vision"]["model"] = str(model)
    cfg["vision"]["api_key"] = token
    cfg["vision"]["_hosted_vision"] = True
    logger.info("[hosted-vision] 识图已指向官网网关（我们的 GPU 模型 %s，用户无需配置）", model)
    return True


def ensure_hosted_ai(config_manager: Any, *, fetch: Optional[Fetch] = None) -> bool:
    """若适用则把设备令牌注入 ``ai.*`` + 进程 env。返回是否已接入托管网关。

    调用点：``main.py`` 启动（AIClient 之前）、license trial-claim 路由钩子、
    刷新守护线程。幂等，可安全反复调用。
    """
    from src.utils.golive import _is_placeholder

    cfg = getattr(config_manager, "config", None) or {}
    if not _wants_hosted(cfg):
        return False

    path = _state_path(config_manager)
    cached = _load_cached(path) if path is not None else {}

    # 运维直注（env 值不是本模块缓存的那枚）→ 不接管
    env_key = (os.environ.get("AITR_HOSTED_AI_KEY") or "").strip()
    if env_key and env_key != str(cached.get("token") or ""):
        return True

    ai = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    user_has_own_key = (
        not _is_placeholder(ai.get("api_key"))
        and not ai.get("_hosted_trial")
        and str(ai.get("api_key") or "") != str(cached.get("token") or "")
    )
    if user_has_own_key:
        return False

    now = int(time.time())
    exp = int(cached.get("exp") or 0)
    cache_valid = bool(cached.get("token")) and exp > now + MIN_VALID_SEC
    cache_fresh = cache_valid and exp > now + REFRESH_MARGIN_SEC

    if cache_fresh:
        _apply(config_manager, cached)
        return True

    got = fetch_device_token(config=cfg, fetch=fetch)
    if got.get("ok"):
        if path is not None:
            _save_cached(path, {
                "token": got["token"],
                "base_url": got["base_url"],
                "model": got.get("model") or "",
                "exp": got.get("exp") or 0,
                "fingerprint": got.get("fingerprint") or "",
                "fetched_at": now,
            })
        _apply(config_manager, got)
        logger.info("[hosted-ai] 已接入官网 AI 网关（设备令牌，用户无需配置 Key）")
        return True

    err = str(got.get("error") or "")
    if cache_valid:
        # 换新失败但旧令牌还能用 → 继续用旧的（网络抖动不掉线）
        _apply(config_manager, cached)
        logger.info("[hosted-ai] 令牌换新失败（%s），沿用未过期旧令牌", err)
        return True
    if err == "no_claim":
        logger.info("[hosted-ai] 本机尚未领取试用（首启向导领取后自动接入）")
    else:
        logger.info("[hosted-ai] 设备令牌领取失败：%s（AI 保持未配置）", err)
    return False


def _apply(config_manager: Any, data: Dict[str, Any]) -> None:
    """令牌 → 内存 config + 进程 env（后者让所有热重载路径自动重放注入）。"""
    token = str(data.get("token") or data.get("api_key") or "")
    base = str(data.get("base_url") or "").rstrip("/")
    model = str(data.get("model") or "").strip()

    cfg = config_manager.config
    ai = cfg.get("ai")
    if not isinstance(ai, dict):
        ai = {}
        cfg["ai"] = ai
    ai["api_key"] = token
    if base:
        ai["base_url"] = base
    if model:
        ai["model"] = model
    ai["provider"] = "openai_compatible"
    ai["_hosted_trial"] = True

    os.environ["AITR_HOSTED_AI_KEY"] = token
    if base:
        os.environ["AITR_HOSTED_AI_BASE_URL"] = base
    if model:
        os.environ["AITR_HOSTED_AI_MODEL"] = model


def schedule_forced_refresh(
    config_manager: Any,
    *,
    on_token: Optional[Callable[[str], None]] = None,
    fetch: Optional[Fetch] = None,
) -> bool:
    """认证失败（401 类）时的**异步**强制换新：跳过 fresh 短路，直接向官网重领。

    - 后台线程执行（AIClient 的失败钩子在事件循环里，同步 HTTP 会卡住全站）；
    - 120s 冷却：坏令牌期间每条消息都会触发钩子，不能每次都打官网；
    - 成功后写缓存 + config + env，并回调 ``on_token(token)``——调用方用它热替换
      运行中 AsyncOpenAI 实例的 api_key，下一条消息即恢复，无需 reload/重启。
    返回是否真的调度了一次换新（冷却窗内/非托管态返回 False）。
    """
    global _last_forced_ts
    cfg = getattr(config_manager, "config", None) or {}
    ai = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    if not ai.get("_hosted_trial"):
        return False
    now = time.time()
    with _forced_lock:
        if now - _last_forced_ts < FORCED_REFRESH_COOLDOWN_SEC:
            return False
        _last_forced_ts = now

    def _run() -> None:
        try:
            got = fetch_device_token(config=cfg, fetch=fetch)
            if not got.get("ok"):
                logger.info("[hosted-ai] 强制换新失败：%s", got.get("error"))
                return
            path = _state_path(config_manager)
            if path is not None:
                _save_cached(path, {
                    "token": got["token"],
                    "base_url": got["base_url"],
                    "model": got.get("model") or "",
                    "exp": got.get("exp") or 0,
                    "fingerprint": got.get("fingerprint") or "",
                    "fetched_at": int(time.time()),
                })
            _apply(config_manager, got)
            if on_token is not None:
                try:
                    on_token(str(got["token"]))
                except Exception:
                    logger.debug("[hosted-ai] on_token 回调失败（忽略）", exc_info=True)
            logger.info("[hosted-ai] 认证失败触发的令牌强制换新成功（运行中热替换）")
        except Exception:
            logger.debug("[hosted-ai] 强制换新异常（忽略）", exc_info=True)

    threading.Thread(target=_run, name="hosted-ai-forced-refresh", daemon=True).start()
    return True


def start_refresh_daemon(config_manager: Any, *, interval_sec: int = 3600) -> bool:
    """后台每小时跑一次 ``ensure_hosted_ai``：续期临期令牌 + 补领
    （首启无网 / 未领试用时，条件满足后自动接入，无需重启）。

    缓存新鲜时零 HTTP（cache_fresh 短路），代价可忽略。进程内幂等单例。
    """
    global _daemon_started
    cfg = getattr(config_manager, "config", None) or {}
    if not _wants_hosted(cfg):
        return False
    with _daemon_lock:
        if _daemon_started:
            return True
        _daemon_started = True

    def _loop() -> None:
        while True:
            time.sleep(max(300, int(interval_sec)))
            try:
                ensure_hosted_ai(config_manager)
            except Exception:
                logger.debug("[hosted-ai] 刷新守护异常（忽略）", exc_info=True)

    t = threading.Thread(target=_loop, name="hosted-ai-refresh", daemon=True)
    t.start()
    return True


def quota_probe(config_manager: Any, *, fetch: Optional[Fetch] = None,
                ttl_sec: int = 60) -> Dict[str, Any]:
    """查询当日试用额度（绿条徽章数据源）。60s 进程级缓存，软失败返回 enabled=False。"""
    now = time.time()
    if _quota_cache["data"] is not None and now - _quota_cache["ts"] < ttl_sec:
        return dict(_quota_cache["data"])

    cfg = getattr(config_manager, "config", None) or {}
    path = _state_path(config_manager)
    cached = _load_cached(path) if path is not None else {}
    token = str(cached.get("token") or "")
    ai = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    if not token or not ai.get("_hosted_trial"):
        out = {"enabled": False}
    else:
        f = fetch or (lambda u, m, b: _http(u, m, b, bearer=token))
        resp = f(f"{_gateway_base(cfg)}/quota", "GET", None)
        if resp.get("ok"):
            out = {
                "enabled": True,
                "used": int(resp.get("used") or 0),
                "budget": int(resp.get("budget") or 0),
                "remaining": int(resp.get("remaining") or 0),
                "busy": bool(resp.get("busy")),
                "exhausted": int(resp.get("remaining") or 0) <= 0,
            }
        else:
            out = {"enabled": True, "error": str(resp.get("error") or "network")}
        # 额度用尽时刻＝最高转化意向时刻：把本机绑定码带给前端升级弹窗
        # （本地状态文件读取，零网络；bind_code 本就设计为要念给客服听的低敏值）
        try:
            from src.licensing import trial_claim_client as _tc
            bc = str((_tc.load_state(cfg) or {}).get("bind_code") or "")
            if bc:
                out["bind_code"] = bc
        except Exception:
            pass
    _quota_cache["ts"] = now
    _quota_cache["data"] = dict(out)
    return out
