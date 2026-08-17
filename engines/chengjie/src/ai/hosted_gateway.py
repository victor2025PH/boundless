"""托管 AI 网关客户端：向官网换设备令牌，注入 ``ai.*``（用户永不配云 Key）。

流程：
  桌面/托管态 + 本地 api_key 仍为空/占位
    → POST {site}/api/ai/device-token {fingerprint, instance_id?}
    → 内存写入 ai.api_key=设备令牌、ai.base_url=官网 /api/ai/v1、ai._hosted_trial=True
  真云 Key 只在官网进程（DEEPSEEK_API_KEY），不下发。

  ``instance_id`` 为前向兼容字段（多租户同机指纹共享时，服务端可按实例计量）；
  旧服务端忽略未知字段，零破坏。解析序：``AITR_INSTANCE_ID`` →
  ``AITR_DATA_DIR`` 父目录名（``…/<iid>/data``）。

关键设计（P0 加固，2026-07-29）：
- **热重载存活**：令牌同时写进程 env ``AITR_HOSTED_AI_*`` ——
  ``ConfigManager._apply_env_overrides`` 每次 load()/热重载都会重放注入，
  否则任何 config 热重载都会把内存令牌抹回空 Key（实测风险）。
  **识图同理**（``AITR_HOSTED_VISION_*``，2026-07-31 补）：它此前只有内存注入、
  没有 env 重放，于是任何一次写 overlay 触发的热重载都会把网关地址抹掉——
  连「一键开齐入站识别」自己都算（它写 overlay → 30s 内热重载 → 后端消失 →
  自检黄灯「开了但后端未就绪」，到下次重启前无法自愈）。
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
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_SITE = "https://bd2026.cc"
HTTP_TIMEOUT = 8
STATE_FILENAME = "hosted_ai_token.json"
#: 托管识图注入的 env 回放键（令牌本身复用 AITR_HOSTED_AI_KEY，避免两处各存一份会漂移）
VISION_ENV_BASE = "AITR_HOSTED_VISION_BASE_URL"
VISION_ENV_MODEL = "AITR_HOSTED_VISION_MODEL"
#: 托管克隆语音 / 语音识别（混合形态，2026-08-03）的 env 回放键：
#: *_BASE_URL=网关挂载点；*_FIRST="1"＝注入时 LAN 探测不可达 → 网关排首位。
VOICE_ENV_BASE = "AITR_HOSTED_VOICE_BASE_URL"
VOICE_ENV_FIRST = "AITR_HOSTED_VOICE_FIRST"
VOICE_ENV_HUBFISH_OFF = "AITR_HOSTED_HUBFISH_OFF"
ASR_ENV_BASE = "AITR_HOSTED_ASR_BASE_URL"
ASR_ENV_FIRST = "AITR_HOSTED_ASR_FIRST"
#: 注入 voice_recognition 用的 api_key 占位值：OpenAITranscriber 在**调用时**把它
#: 解析为当前设备令牌（AITR_HOSTED_AI_KEY）——转写器构建一次常驻，令牌 30 天
#: 换新不能把旧值钉死在已构建实例里。
HOSTED_KEY_PLACEHOLDER = "hosted"
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


def _hub_base(config: Optional[dict]) -> str:
    """AvatarHub 形态中继的网关挂载点。

    路径与 7852 服务契约逐字对齐（``{base}/v1/tts/clone`` / ``{base}/health``），
    于是 ``avatar_voice.base_urls`` 里放这个地址，AvatarVoiceClient 零改动可用。
    """
    return f"{_site_url(config)}/api/ai/hub"


def lan_alive(url: str, *, timeout: float = 2.0) -> bool:
    """LAN 端点可达性＝TCP 连接探测（协议无关，2s 快败，不发请求体）。

    只用于「混合形态」注入时决定端点次序；失败即 False，绝不抛。
    """
    import socket

    try:
        sp = urllib.parse.urlsplit(str(url or ""))
        host = sp.hostname
        port = sp.port or (443 if sp.scheme == "https" else 80)
        if not host:
            return False
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


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


def resolve_instance_id(config: Optional[dict] = None) -> str:
    """解析本进程所属实例 ID（托管多租户计量前向兼容；解析不到返回空串）。

    序：显式 env ``AITR_INSTANCE_ID`` → 数据根落在 ``chengjie-instances/<iid>/data``
    → config ``licensing.instance_id``。
    刻意**不**把任意 ``AITR_DATA_DIR`` 父名当实例（桌面壳数据目录会误伤）。
    """
    env = (os.environ.get("AITR_INSTANCE_ID") or "").strip()
    if env:
        return env
    data = (os.environ.get("AITR_DATA_DIR") or "").strip()
    if data:
        from pathlib import Path

        p = Path(data)
        parts_lower = {x.lower() for x in p.parts}
        if "chengjie-instances" in parts_lower and p.name.lower() == "data" and p.parent.name:
            return p.parent.name
    cfg = config or {}
    lic = cfg.get("licensing") if isinstance(cfg.get("licensing"), dict) else {}
    return str((lic or {}).get("instance_id") or "").strip()


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
    # 桌面试用保持 source=desktop；服务器多实例才标 hosted（旧服务端忽略未知字段）
    desktop = bool((os.environ.get("AITR_DESKTOP_MODE") or "").strip())
    iid = "" if desktop else resolve_instance_id(config)
    body: Dict[str, Any] = {
        "fingerprint": fp,
        "product": "chatx",
        "source": "hosted" if iid else "desktop",
    }
    if iid:
        body["instance_id"] = iid
    resp = f(
        f"{_site_url(config)}/api/ai/device-token",
        "POST",
        body,
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
        "instance_id": iid,
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
    body: Dict[str, Any] = {"fingerprint": fp}
    direct = _probe_tg_direct()
    if direct is not None:
        # P2-⑨ 智能派发：直连不通的机器优先分到带出口的组（服务端软偏好，
        # 探测失败不带此键=服务端按旧行为分配）
        body["tg_direct"] = direct
    resp = f(f"{_site_url(cfg)}/api/pool/telegram-cred", "POST",
             body, bearer) if bearer else f(
        f"{_site_url(cfg)}/api/pool/telegram-cred", "POST", body)
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
    _apply_hosted_tg_proxy(cfg, resp.get("proxy"))
    # 🔒 只记 api_id 与来源，绝不记 api_hash / 代理账密（与 LAN 池同口径）
    logger.info("[hosted-tg] 已注入托管 Telegram 凭据 api_id=%s name=%s proxy=%s"
                "（用户无需申请）", resp["api_id"], resp.get("name") or "?",
                "yes" if cfg["telegram"].get("_hosted_proxy") else "no")
    return True


def _probe_tg_direct() -> Optional[bool]:
    """探「本机能否直连 Telegram」（60s 缓存的 TCP 快败）；判不出返回 None。

    在同步上下文运行（ensure_hosted_telegram 经 to_thread 调用，线程内无事件循环，
    ``asyncio.run`` 安全）；任何异常＝没有信息，绝不影响凭据派发主链。
    """
    try:
        import asyncio as _aio

        from src.integrations.tg_preflight import probe_telegram_reachable
        res = _aio.run(probe_telegram_reachable())
        return bool(res.get("reachable"))
    except Exception:  # noqa: BLE001
        return None


def _apply_hosted_tg_proxy(cfg: Dict[str, Any], raw: Any) -> None:
    """把派发响应里的出口写进内存 ``telegram._hosted_proxy``（无/非法则清掉）。

    形状校验复用 credpool_bridge 的口径（宁可不用代理也不能用半个代理）；
    换组后旧代理必须清（新组无出口时带着旧出口连=错配出口）。
    """
    try:
        from src.integrations.credpool_bridge import _sanitize_proxy
        proxy = _sanitize_proxy(raw)
    except Exception:  # noqa: BLE001
        proxy = None
    if proxy:
        cfg["telegram"]["_hosted_proxy"] = proxy
    else:
        cfg["telegram"].pop("_hosted_proxy", None)


#: 无感换发冷却：坏组 × 连点刷新不能变成打官网的机关枪；120s 内只换一次
SWAP_COOLDOWN_SEC = 120.0
_swap_lock = threading.Lock()
_last_swap_ts = 0.0


def swap_retry_after_sec() -> int:
    """距下次允许「举报废凭据换新组」还剩几秒（0＝现在就可以）。

    给登录失败响应的 ``retry_after_sec`` 用：前端拿它画倒计时并到点自动重试，
    替代旧文案「等约 2 分钟后点刷新」让用户自己掐表。只读快照，不推进冷却。
    """
    with _swap_lock:
        elapsed = time.time() - _last_swap_ts
    remain = SWAP_COOLDOWN_SEC - elapsed
    return max(0, int(remain + 0.999)) if remain > 0 else 0


def report_invalid_and_refetch(
    cfg: Dict[str, Any], bad_api_id: str, *, fetch: Optional[Fetch] = None,
) -> Optional[Tuple[str, str]]:
    """凭据无感换发（2026-08-10 API_ID_INVALID 事故闭环的客户端半边）。

    扫码撞 ``cred_invalid`` 时把废 api_id 举报给官网池并当场领新组：
    服务端校验「该指纹确实粘定在这组」→ 删粘定 → 排除该组重新分配（同组被多台
    机器举报会自动隔离）。成功则把新凭据写进内存 ``telegram.*`` 并返回
    ``(api_id, api_hash)``；任何失败返回 None（调用方保持旧失败语义，不加戏）。

    护栏：
    - **只动托管注入的凭据**（``telegram._hosted_cred``）——用户自填的值一个字节
      都不碰（自填错了该走表单重填，机器不猜人的意图）；
    - 120s 冷却：坏组 × 用户连点「刷新二维码」不能变成打官网的机关枪；
    - 新组与废组相同（服务端异常/池只剩这组）视为失败，防换发假成功空转。
    """
    global _last_swap_ts
    if not _wants_hosted(cfg):
        return None
    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    if not tg.get("_hosted_cred"):
        return None
    bad = str(bad_api_id or "").strip()
    if not bad:
        return None
    now = time.time()
    with _swap_lock:
        if now - _last_swap_ts < SWAP_COOLDOWN_SEC:
            return None
        _last_swap_ts = now

    from src.licensing.machine_bridge import machine_fingerprint

    fp = machine_fingerprint()
    if not fp:
        return None
    f = fetch or _http
    token = (os.environ.get("AITR_HOSTED_AI_KEY")
             or str((cfg.get("ai") or {}).get("api_key") or ""))
    bearer = token if str(token).startswith("cx.") else ""
    body: Dict[str, Any] = {"fingerprint": fp, "invalid_api_id": bad}
    direct = _probe_tg_direct()
    if direct is not None:
        body["tg_direct"] = direct
    url = f"{_site_url(cfg)}/api/pool/telegram-cred"
    resp = f(url, "POST", body, bearer) if bearer else f(url, "POST", body)
    new_id = str(resp.get("api_id") or "")
    new_hash = str(resp.get("api_hash") or "")
    if not (resp.get("ok") and new_id and new_hash) or new_id == bad:
        logger.info("[hosted-tg] 废凭据换发未成功：%s（保持原失败语义）",
                    resp.get("error") or "same_or_empty")
        return None
    tg["api_id"] = new_id
    tg["api_hash"] = new_hash
    tg["_hosted_cred"] = True
    _apply_hosted_tg_proxy(cfg, resp.get("proxy"))
    # 🔒 只记 api_id，绝不记 api_hash / 代理账密
    logger.info("[hosted-tg] 已举报废凭据 api_id=%s 并换发新组 api_id=%s proxy=%s（无感自愈）",
                bad, new_id, "yes" if tg.get("_hosted_proxy") else "no")
    return new_id, new_hash


def _reset_swap_cooldown_for_tests() -> None:
    global _last_swap_ts
    with _swap_lock:
        _last_swap_ts = 0.0


def ensure_hosted_vision(
    config_manager: Any, *, fetch: Optional[Fetch] = None,
    probe: Optional[Callable[[str], bool]] = None,
) -> bool:
    """托管版：把识图（VLM）指向官网网关（我们自己的 GPU 识图模型，非云端大模型）。

    问题 #2 的公网解——集团识图 GPU（176/140）在内网，公网够不着；改由 bd2026.cc 网关
    经 117→VPS 反向隧道转发到 176。注入 ``vision.*``：base_url=网关 /api/ai/v1，
    provider=openai_compatible，model=qwen2.5vl:7b，api_key=设备令牌，enabled=true。

    仅托管态 + 用户未自配识图后端时注入（自建/已配 base_url 的不覆盖）。
    需已持有设备令牌（ai.api_key=cx.…）——识图与聊天共用同一枚令牌鉴权。

    **热重载存活**（2026-07-31）：注入同时写进程 env ``AITR_HOSTED_VISION_*``，由
    ``ConfigManager._apply_env_overrides`` 在每次 load()/热重载后重放（与托管 AI 同款
    机制、同款理由，见模块头）。缺了它，写 overlay 就等于把识图后端删掉。

    ``enabled`` 只在**从未表达过**时置 true（与 ``channel_setup.enable_on_ready`` 同
    口径）：显式 false 是运营用「全部关闭」表达过的意图，重启不该把它扳回来。
    """
    cfg = getattr(config_manager, "config", None) or {}
    if not _wants_hosted(cfg):
        return False
    v = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
    # 用户已自配识图后端（base_url/base_urls/智谱 key）→ 不动。
    # 两个例外：本模块此前注入的（_hosted_vision，允许续期刷新）；内测种子的
    # LAN 后端（_lan_seed，混合形态——LAN 可达保持直连、不可达由网关接管）。
    if (str(v.get("base_url") or "").strip() or v.get("base_urls")
            or str(v.get("api_key") or "").strip()):
        if not (v.get("_hosted_vision") or v.get("_lan_seed")):
            return False

    ai = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    token = str(ai.get("api_key") or "")
    if not token.startswith("cx."):
        return False  # 还没拿到设备令牌（识图与聊天共用），等 ensure_hosted_ai 先成

    # ── 混合形态（_lan_seed，2026-08-03）：LAN 探测决定 直连 / 网关接管，可逆 ──
    # 同一个安装包在办公室内网直连 176/140 低延迟；装到外网机器上 LAN 不可达 →
    # 网关接管（经 117→VPS 隧道回同一批 GPU）。回到内网后下轮刷新自动还原直连。
    if v.get("_lan_seed"):
        chk = probe or lan_alive
        stash = v.get("_lan_backend") if isinstance(v.get("_lan_backend"), dict) else None
        if stash is None and not v.get("_hosted_vision"):
            stash = {
                "provider": v.get("provider"),
                "base_url": v.get("base_url"),
                "base_urls": list(v.get("base_urls") or []) or None,
                "api_key": v.get("api_key"),
                "model": v.get("model"),
            }
        lan_urls = [u for u in ((stash or {}).get("base_urls")
                                or [(stash or {}).get("base_url")]) if u]
        if lan_urls and any(chk(str(u)) for u in lan_urls):
            if v.get("_hosted_vision") and stash:
                # 回到内网：还原 LAN 直连，撤网关接管（env 一并清，防热重载重放）
                for key in ("provider", "base_url", "base_urls", "api_key", "model"):
                    val = stash.get(key)
                    if val is None:
                        v.pop(key, None)
                    else:
                        v[key] = val
                v.pop("_hosted_vision", None)
                v.pop("_lan_backend", None)
                os.environ.pop(VISION_ENV_BASE, None)
                os.environ.pop(VISION_ENV_MODEL, None)
                logger.info("[hosted-vision] LAN 识图后端恢复可达 → 还原直连")
            return False
        if stash is not None:
            v["_lan_backend"] = stash

    # 规范 VLM：两台中继（176/140）都装 qwen3-vl:8b-instruct → 网关可任意分流。
    # 网关侧还会统一改写 model，故此值只影响日志/回显，不会造成中继找不到模型。
    model = (((cfg.get("licensing") or {}).get("hosted_ai") or {}).get("vision_model")
             or "qwen3-vl:8b-instruct")
    if not isinstance(cfg.get("vision"), dict):
        cfg["vision"] = {}
    vision = cfg["vision"]
    vision["provider"] = "openai_compatible"
    vision["base_url"] = _gateway_base(cfg)  # https://bd2026.cc/api/ai/v1
    # base_urls 一并指网关：种子的 LAN 列表优先级高于单数 base_url，不改写的话
    # 混合形态接管后 VisionClient 仍会去打死掉的 192.168.0.x。
    vision["base_urls"] = [str(vision["base_url"])]
    vision["model"] = str(model)
    vision["api_key"] = token
    vision["_hosted_vision"] = True
    vision.setdefault("enabled", True)  # 显式 false = 运营关过，不扳回
    os.environ[VISION_ENV_BASE] = str(vision["base_url"])
    os.environ[VISION_ENV_MODEL] = str(model)
    logger.info("[hosted-vision] 识图已指向官网网关（我们的 GPU 模型 %s，用户无需配置）", model)
    return True


def vision_provision_reason(config: Optional[dict]) -> str:
    """托管识图供给现在处于什么状态（机器可读，供 UI 讲「该做什么」而非「联系客服」）。

    ``""``＝已供给；``self_hosted``＝非托管态（自建自己配后端，本模块不该插手）；
    ``user_backend``＝用户已自配后端（尊重，不覆盖）；``no_token``＝还没拿到设备令牌
    （多半没领试用或没网，领完会自动接上）；``not_provisioned``＝托管态但注入没发生
    （可以直接重试，无需重启）。
    """
    cfg = config or {}
    v = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
    if v.get("_hosted_vision") and str(v.get("base_url") or "").strip():
        return ""
    if not _wants_hosted(cfg):
        return "self_hosted"
    if (str(v.get("base_url") or "").strip() or v.get("base_urls")
            or str(v.get("api_key") or "").strip()):
        return "user_backend"
    if not str((cfg.get("ai") or {}).get("api_key") or "").startswith("cx."):
        return "no_token"
    return "not_provisioned"


def _lan_bases(av: Dict[str, Any], *, exclude: str) -> list:
    """取 ``avatar_voice`` 当前端点列表（去掉网关地址后的 LAN 部分）。"""
    raw = av.get("base_urls")
    bases = ([str(b).rstrip("/") for b in raw if str(b).strip()]
             if isinstance(raw, (list, tuple)) else [])
    if not bases:
        single = str(av.get("base_url") or "").rstrip("/")
        bases = [single] if single else []
    return [b for b in bases if b != exclude]


def apply_hosted_voice(cfg: Dict[str, Any], hub_base: str, *,
                       gateway_first: bool, hub_fish_off: bool) -> bool:
    """把网关端点排进 ``avatar_voice.base_urls``（纯内存变更）。

    ensure_hosted_voice 与 ConfigManager 的 env 回放共用本函数＝变更逻辑单一事实源。
    只动种子标记过（``_lan_seed``）或本模块注入过（``_hosted_voice``）的配置。
    hub_fish 是 LAN 专属高保真链且调用处**没有**健康预检——不可达时就地禁用
    （``_hosted_off`` 记账，可逆），否则外网机器 allowlist 人设每条语音硬吃 TCP 超时。
    """
    av = cfg.get("avatar_voice") if isinstance(cfg.get("avatar_voice"), dict) else None
    if not av or not (av.get("_lan_seed") or av.get("_hosted_voice")):
        return False
    hub = str(hub_base or "").rstrip("/")
    if not hub:
        return False
    lan = _lan_bases(av, exclude=hub)
    av["base_urls"] = ([hub] + lan) if gateway_first else (lan + [hub])
    av["base_url"] = av["base_urls"][0]
    av["_hosted_voice"] = True
    hf = av.get("hub_fish") if isinstance(av.get("hub_fish"), dict) else None
    if hf is not None:
        if hub_fish_off:
            if hf.get("enabled"):
                hf["enabled"] = False
                hf["_hosted_off"] = True
        elif hf.pop("_hosted_off", None):
            hf["enabled"] = True
    return True


def apply_hosted_asr(cfg: Dict[str, Any], gw_base: str, *, gateway_first: bool) -> bool:
    """把网关转写接进 ``voice_recognition``（纯内存变更；与 env 回放共用）。

    LAN 可达 → 主转写保持 LAN，网关作**最后一级** fallback；LAN 不可达 → 主转写
    换成网关（原 LAN 主位入 ``_lan_primary`` 暂存，回内网可逆还原）。api_key 用
    占位值 ``hosted``：OpenAITranscriber 在调用时解析当前设备令牌，换新不失效。
    """
    vr = (cfg.get("voice_recognition")
          if isinstance(cfg.get("voice_recognition"), dict) else None)
    if not vr or not (vr.get("_lan_seed") or vr.get("_hosted_asr")):
        return False
    gw = str(gw_base or "").rstrip("/")
    if not gw:
        return False
    stash = vr.get("_lan_primary") if isinstance(vr.get("_lan_primary"), dict) else None
    if stash is None:
        stash = {k: vr.get(k)
                 for k in ("provider", "base_url", "api_key", "timeout", "max_retries")}
        vr["_lan_primary"] = stash
    fb = vr.get("fallback")
    if isinstance(fb, dict):
        fb = [fb]
    fb = [dict(e) for e in fb if isinstance(e, dict)] if isinstance(fb, list) else []
    fb = [e for e in fb if not e.get("_hosted_asr_entry")]
    if gateway_first:
        vr["provider"] = "openai_compatible"
        vr["base_url"] = gw
        vr["api_key"] = HOSTED_KEY_PLACEHOLDER
        vr["timeout"] = 45
        vr["max_retries"] = 0
    else:
        for key, val in stash.items():
            if val is None:
                vr.pop(key, None)
            else:
                vr[key] = val
        fb.append({
            "provider": "openai_compatible", "base_url": gw,
            "api_key": HOSTED_KEY_PLACEHOLDER,
            "model": str(vr.get("model") or "large-v3-turbo"),
            "timeout": 45, "max_retries": 0, "_hosted_asr_entry": True,
        })
    vr["fallback"] = fb
    vr["_hosted_asr"] = True
    return True


def ensure_hosted_voice(
    config_manager: Any, *, probe: Optional[Callable[[str], bool]] = None,
) -> bool:
    """托管克隆语音（混合形态）：LAN 7852 直连优先，出了内网经官网网关回集群 GPU。

    只动**内测种子标记过**的配置（``avatar_voice._lan_seed``；或本模块注入过
    ``_hosted_voice``）——用户/运维自配的 TTS 端点绝不碰。需已持设备令牌：网关
    鉴权走 ``X-AH-Svc: cx.…``，由 ``avatar_voice._svc_headers`` 在集群令牌缺失时
    自动改带设备令牌。

    端点次序只在「多个健康 / 全不健康」时起作用（AvatarVoiceClient 自带健康预检
    与失败冷却）：LAN 活着 → ``[LAN…, 网关]``（办公室直连低延迟）；LAN 全不可达
    → ``[网关, LAN…]``（外网首条语音不必先撞死端点吃连接超时）。
    env 双写 ``AITR_HOSTED_VOICE_*``——热重载经 ``_apply_env_overrides`` 重放。
    """
    cfg = getattr(config_manager, "config", None) or {}
    if not _wants_hosted(cfg):
        return False
    av = cfg.get("avatar_voice") if isinstance(cfg.get("avatar_voice"), dict) else None
    if not av or not av.get("enabled"):
        return False
    if not (av.get("_lan_seed") or av.get("_hosted_voice")):
        return False
    if not str((cfg.get("ai") or {}).get("api_key") or "").startswith("cx."):
        return False  # 网关要设备令牌鉴权，等 ensure_hosted_ai 先成
    hub = _hub_base(cfg)
    chk = probe or lan_alive
    lan_ok = any(chk(b) for b in _lan_bases(av, exclude=hub))
    hf = av.get("hub_fish") if isinstance(av.get("hub_fish"), dict) else None
    hub_fish_off = bool(
        hf and (hf.get("enabled") or hf.get("_hosted_off"))
        and not chk(str(hf.get("base_url") or "")))
    apply_hosted_voice(cfg, hub, gateway_first=not lan_ok, hub_fish_off=hub_fish_off)
    os.environ[VOICE_ENV_BASE] = hub
    os.environ[VOICE_ENV_FIRST] = "0" if lan_ok else "1"
    if hub_fish_off:
        os.environ[VOICE_ENV_HUBFISH_OFF] = "1"
    else:
        os.environ.pop(VOICE_ENV_HUBFISH_OFF, None)
    logger.info(
        "[hosted-voice] 克隆语音已接入网关兜底（%s；LAN %s%s）",
        hub, "直连优先" if lan_ok else "不可达 → 网关优先",
        "，hub_fish 暂禁" if hub_fish_off else "")
    return True


def ensure_hosted_asr(
    config_manager: Any, *, probe: Optional[Callable[[str], bool]] = None,
) -> bool:
    """托管语音识别（混合形态）：LAN GPU ASR 优先，出了内网经网关转写。

    只动内测种子标记过的配置（``voice_recognition._lan_seed`` / ``_hosted_asr``）。
    注意：转写器在各 worker 启动时**构建一次常驻**（telegram_client / orchestrator /
    media_enrich 懒建），本注入必须先于其构建（main.py 启动段已保证）；进程运行中
    漫游换网需重启才重排——代价只是多一跳失败回落，可接受。
    """
    cfg = getattr(config_manager, "config", None) or {}
    if not _wants_hosted(cfg):
        return False
    vr = (cfg.get("voice_recognition")
          if isinstance(cfg.get("voice_recognition"), dict) else None)
    if not vr or vr.get("enabled") is False:
        return False
    if not (vr.get("_lan_seed") or vr.get("_hosted_asr")):
        return False
    if not str((cfg.get("ai") or {}).get("api_key") or "").startswith("cx."):
        return False
    gw = _gateway_base(cfg)
    chk = probe or lan_alive
    stash = vr.get("_lan_primary") if isinstance(vr.get("_lan_primary"), dict) else None
    lan_primary = str((stash or {}).get("base_url") or vr.get("base_url") or "")
    lan_ok = bool(lan_primary) and lan_primary != gw and chk(lan_primary)
    apply_hosted_asr(cfg, gw, gateway_first=not lan_ok)
    os.environ[ASR_ENV_BASE] = gw
    os.environ[ASR_ENV_FIRST] = "0" if lan_ok else "1"
    logger.info("[hosted-asr] 语音识别已接入网关兜底（LAN %s）",
                "直连优先" if lan_ok else "不可达 → 网关优先")
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
    cur_key = str(ai.get("api_key") or "")
    user_has_own_key = (
        not _is_placeholder(ai.get("api_key"))
        and not ai.get("_hosted_trial")
        and cur_key != str(cached.get("token") or "")
    )
    # 升级坑修复（2026-07-29）：托管版**严格态**（AITR_MANAGED_EDITION=1，非仅桌面模式）
    # 下，用户从不自配 Key——此时残留的非占位 Key 几乎必是旧版首启写坏的（如
    # "ollama"/坏 DeepSeek key），会让本函数误判「用户自有 Key」而跳过注入，
    # 老 401 原样保留（今早 ****lama invalid + 「更新了还是老问题」的根因）。
    # 故严格托管态忽略该护栏，往下真正领到令牌**成功后才覆盖**（fetch 失败绝不动旧值）。
    strict_managed = str(os.environ.get("AITR_MANAGED_EDITION") or "").strip().lower() in (
        "1", "true", "yes", "on")
    if user_has_own_key and not strict_managed:
        return False
    override_stale = user_has_own_key and strict_managed  # 覆盖仅在成功领令牌后发生
    if override_stale:
        logger.info("[hosted-ai] 托管版检测到疑似旧版残留 Key（非托管令牌），领到新令牌后将覆盖")

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

    _sync_hosted_vision_token(cfg, token)


def _sync_hosted_vision_token(cfg: Dict[str, Any], token: str) -> None:
    """令牌换新 → 同步刷进已注入的托管识图（只动本模块注入过的那份）。

    识图与聊天共用同一枚设备令牌，但换新只走 AI 这条路（守护线程 / 401 强制换新）。
    不同步的话识图会攥着旧令牌一路 401，而识图失败是静默降级（`media_enrich` 返空串），
    没人会发现。
    """
    if not token:
        return
    v = cfg.get("vision")
    if isinstance(v, dict) and v.get("_hosted_vision"):
        v["api_key"] = token


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


def refresh_once(config_manager: Any) -> None:
    """守护线程每轮做的事（抽成函数＝可被门禁直接调用，不必等一小时的 sleep）。

    令牌与识图**必须成对刷**：供给是两步，令牌后到（首启无网 / 未领试用 / 领完试用
    才签发）时若只补 AI，识图就会一直缺后端直到下次重启——这正是「一键开齐入站识别
    点了没用」的成因之一。已注入时识图那步是纯内存赋值，零 HTTP。
    克隆语音 / 语音识别（混合形态）同批刷：每轮重探 LAN 可达性，办公室↔外网漫游
    在一小时内自动换向（每轮代价＝几次 2s 快败 TCP 探测）。

    Telegram 凭据也在此补领（2026-08-10 补，API_ID_INVALID 事故排查连带发现）：
    此前只在启动时领一次——首启没网/官网抖动错过那一发，用户就一直停在「填
    API ID/Hash」表单直到重启。已注入/用户自填时它秒级短路（不发 HTTP），
    只有缺凭据时每小时多一次补领请求。
    """
    try:
        ensure_hosted_ai(config_manager)
        ensure_hosted_telegram(config_manager)
        ensure_hosted_vision(config_manager)
        ensure_hosted_voice(config_manager)
        ensure_hosted_asr(config_manager)
    except Exception:
        logger.debug("[hosted-ai] 刷新守护异常（忽略）", exc_info=True)


def start_refresh_daemon(config_manager: Any, *, interval_sec: int = 3600) -> bool:
    """后台每小时跑一次 ``ensure_hosted_ai`` + ``ensure_hosted_vision``：续期临期令牌 +
    补领（首启无网 / 未领试用时，条件满足后自动接入，无需重启）。

    缓存新鲜时零 HTTP（cache_fresh 短路 + 识图注入本就不发 HTTP），代价可忽略。
    进程内幂等单例。
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
            refresh_once(config_manager)

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
