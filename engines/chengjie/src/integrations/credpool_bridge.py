"""中央凭据池桥接（集团底座 `platform/credpool` 的引擎侧适配层）。

为什么需要这层：新用户不懂、也常常申请不到 Telegram api_id/api_hash
（my.telegram.org 流程脆、一个手机号只能注册一组、国内尤其易失败），这是接入
转化率的最大黑洞。集团把凭据池中央化（实现在智控主后台）后，本模块让 chengjie
在**登录**与**拉起 runner**两处都能按账号向中央池要凭据，终端用户全程无需接触。

设计要点：
- **不 import platform 包**：顶层 `platform` 与标准库同名，包式导入会遮蔽标准库
  （见 platform/licensing 的同款陷阱说明）。这里按**文件路径**惰性加载瘦客户端。
- **默认关**（`platform_login.telegram.credpool.enabled`，遵守新子系统 feature flag 约定）。
- **绝不阻塞主流程**：池不可达/未配 token/分配失败，一律回落到配置里的自带凭据；
  两者都没有时返回 None，由调用方按既有逻辑提示。
- **粘定键（pool_key）是本层的关键设计**：pyrogram 的 session 与登录时所用的 api_id
  绑定，若下次拉起换了另一组凭据会出问题。因此每个账号在**首次扫码时生成一个稳定
  的 pool_key 并落进账号注册表 meta**，之后 runner 每次启动用同一个 key 向池索取，
  池按 key 粘定返回**同一组**凭据——既保证稳定，又不必把 api_hash 明文存在本地。
- 🔒 全程不打印 api_hash；日志只出 api_id 与来源。
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: 账号注册表 meta 里存放粘定键的字段名
META_KEY = "credpool_key"

_CLIENT_MOD: Any = None
_CLIENT_LOAD_FAILED = False


def _client_module() -> Any:
    """按文件路径惰性加载 platform/credpool/credpool_client.py（失败只记一次）。"""
    global _CLIENT_MOD, _CLIENT_LOAD_FAILED
    if _CLIENT_MOD is not None or _CLIENT_LOAD_FAILED:
        return _CLIENT_MOD

    candidates = _client_path_candidates()
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        _CLIENT_LOAD_FAILED = True
        logger.warning(
            "[credpool] 未找到瘦客户端（找过 %s），中央池能力不可用（回落自带凭据）",
            " | ".join(str(p) for p in candidates),
        )
        return None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_boundless_credpool_client", str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load credpool client from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CLIENT_MOD = mod
    except Exception:  # noqa: BLE001
        _CLIENT_LOAD_FAILED = True
        logger.warning("[credpool] 瘦客户端加载失败，中央池能力不可用（回落自带凭据）", exc_info=True)
    return _CLIENT_MOD


def _client_path_candidates() -> list:
    """瘦客户端可能在的位置（按优先级）。

    ⚠️ 桌面版（PyInstaller `backend.exe`）的坑：`platform/credpool` 在**引擎目录之外**，
    不会被自动打进包——而桌面版恰恰是最需要中央池的产品（新用户不会申请 api_id）。
    故打包清单已显式带上它（见 desktop/build/build_backend.py 的 DATAS），
    冻结运行时它落在 `sys._MEIPASS/platform/credpool/`，这里必须先找那儿。
    """
    import sys

    out = []
    override = os.environ.get("CREDPOOL_CLIENT_PATH", "").strip()
    if override:
        out.append(Path(override))

    rel = Path("platform") / "credpool" / "credpool_client.py"
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:  # 冻结态（桌面版）
        out.append(Path(meipass) / rel)
    # 源码态：engines/chengjie/src/integrations/… → 仓库根
    out.append(Path(__file__).resolve().parents[4] / rel)
    # 兜底：引擎根内 vendor 一份的情形
    out.append(Path(__file__).resolve().parents[2] / rel)
    return out


def _cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    pl = (config or {}).get("platform_login", {}) or {}
    tg = pl.get("telegram", {}) or {}
    return tg.get("credpool", {}) or {}


def credpool_enabled(config: Dict[str, Any]) -> bool:
    """中央池是否启用（新子系统默认关）。"""
    return bool(_cfg(config).get("enabled", False))


def new_pool_key() -> str:
    """生成一个稳定的账号粘定键（首次扫码时产生，随账号长期不变）。"""
    return f"chatx:{uuid.uuid4().hex[:12]}"


def pool_key_of(account: Optional[Dict[str, Any]]) -> str:
    """从账号注册表条目取粘定键（没有则空串）。"""
    meta = (account or {}).get("meta") or {}
    return str(meta.get(META_KEY) or "")


def _make_client(config: Dict[str, Any]) -> Any:
    mod = _client_module()
    if mod is None:
        return None
    c = _cfg(config)
    try:
        return mod.CredPoolClient(
            base_url=(str(c.get("base_url") or "").strip() or None),
            # 建议只走环境变量 CREDPOOL_SERVICE_TOKEN，别把明文写进配置文件
            service_token=(str(c.get("service_token") or "").strip() or None),
            timeout=float(c.get("timeout_sec") or 8.0),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[credpool] 客户端构造失败", exc_info=True)
        return None


def allocate(
    config: Dict[str, Any],
    pool_key: str,
    account_id: Optional[str] = None,
) -> Optional[Tuple[int, str]]:
    """按粘定键向中央池索取凭据；任何失败返回 None（调用方回落自带凭据）。"""
    if not credpool_enabled(config) or not pool_key:
        return None
    client = _make_client(config)
    if client is None:
        return None
    if not client.configured():
        logger.warning("[credpool] 未配置服务 token（环境变量 CREDPOOL_SERVICE_TOKEN），跳过中央池")
        return None

    try:
        res = client.allocate(phone=pool_key, account_id=account_id)
    except Exception:  # noqa: BLE001 —— 瘦客户端契约上不抛，这里是双保险
        logger.debug("[credpool] allocate 异常", exc_info=True)
        return None

    if not res.get("available"):
        logger.warning("[credpool] 中央池不可达，回落自带凭据：%s", res.get("error"))
        return None
    if not res.get("success"):
        logger.warning("[credpool] 分配被拒（池空/越权？）：%s", res.get("message") or res.get("error"))
        return None

    data = res.get("data") or {}
    api_id, api_hash = data.get("api_id"), data.get("api_hash")
    try:
        if api_id and api_hash:
            # 🔒 只记 api_id，绝不记 api_hash
            logger.info("[credpool] 已分配凭据 api_id=%s key=%s", api_id, pool_key)
            return int(api_id), str(api_hash)
    except (TypeError, ValueError):
        logger.warning("[credpool] 池返回的 api_id 非法，回落自带凭据")
    return None


def report(
    config: Dict[str, Any],
    api_id: Optional[int],
    success: bool,
    error: Optional[str] = None,
    pool_key: Optional[str] = None,
) -> None:
    """回报使用结果喂池的健康分；best-effort，绝不影响调用方。"""
    if not credpool_enabled(config) or not api_id:
        return
    client = _make_client(config)
    if client is None or not client.configured():
        return
    try:
        client.report(api_id=str(api_id), success=success, error=error, phone=pool_key)
    except Exception:  # noqa: BLE001
        logger.debug("[credpool] report 异常（忽略）", exc_info=True)


def release(config: Dict[str, Any], pool_key: str) -> None:
    """账号删除/退登时把容量还给池；best-effort。"""
    if not credpool_enabled(config) or not pool_key:
        return
    client = _make_client(config)
    if client is None or not client.configured():
        return
    try:
        client.release(phone=pool_key)
    except Exception:  # noqa: BLE001
        logger.debug("[credpool] release 异常（忽略）", exc_info=True)


def release_for_account_bg(platform: str, account_id: str) -> None:
    """账号被移除时把容量还给中央池（后台线程，best-effort）。

    不还容量会造成**缓慢的容量泄漏**——池以为位子还占着，`forecast` 于是虚报
    「快耗尽」，运营被迫多注册凭据。放后台线程是因为调用方（注册表 remove）
    可能在请求线程里，不该为一次清理副作用等一个最长 8 秒的 HTTP。
    """
    if str(platform) != "telegram" or not account_id:
        return

    def _work() -> None:
        try:
            from src.utils.config_manager import ConfigManager

            config = ConfigManager().config or {}
            if not credpool_enabled(config):
                return
            from src.integrations.account_registry import get_account_registry

            acc = get_account_registry().get(platform, account_id)
            key = pool_key_of(acc)
            if key:
                release(config, key)
        except Exception:  # noqa: BLE001
            logger.debug("[credpool] 后台释放容量失败（忽略）", exc_info=True)

    try:
        import threading

        threading.Thread(target=_work, name="credpool-release", daemon=True).start()
    except Exception:  # noqa: BLE001
        logger.debug("[credpool] 无法启动释放线程（忽略）", exc_info=True)


def resolve_credentials_for_account(
    config: Dict[str, Any],
    account: Optional[Dict[str, Any]] = None,
    pool_key: Optional[str] = None,
) -> Tuple[Optional[Tuple[int, str]], str]:
    """统一凭据解析：中央池优先，回落配置自带。

    返回 ``(creds, source)``，source ∈ {"credpool", "config", "none"}——
    调用方据此决定要不要回报池、日志怎么写。
    """
    key = pool_key or pool_key_of(account)
    if key:
        creds = allocate(config, key, account_id=(account or {}).get("account_id"))
        if creds:
            return creds, "credpool"

    from src.integrations.telegram_protocol_login import resolve_credentials

    local = resolve_credentials(config)
    return (local, "config") if local else (None, "none")


# ── 异步侧包装 ───────────────────────────────────────────────────────────────
# 瘦客户端用 stdlib urllib（阻塞）。登录 provider 与 runner worker 都在事件循环里，
# 直接调用会把整个 loop 卡住最多 timeout 秒。故异步调用方一律走这两个包装。
# 池未启用时不产生任何 I/O，直接同步返回（省掉线程切换开销）。

async def aresolve_credentials_for_account(
    config: Dict[str, Any],
    account: Optional[Dict[str, Any]] = None,
    pool_key: Optional[str] = None,
) -> Tuple[Optional[Tuple[int, str]], str]:
    if not credpool_enabled(config):
        return resolve_credentials_for_account(config, account, pool_key)
    import asyncio

    return await asyncio.to_thread(
        resolve_credentials_for_account, config, account, pool_key
    )


async def areport(
    config: Dict[str, Any],
    api_id: Optional[int],
    success: bool,
    error: Optional[str] = None,
    pool_key: Optional[str] = None,
) -> None:
    if not credpool_enabled(config) or not api_id:
        return
    import asyncio

    try:
        await asyncio.to_thread(report, config, api_id, success, error, pool_key)
    except Exception:  # noqa: BLE001
        logger.debug("[credpool] areport 异常（忽略）", exc_info=True)
