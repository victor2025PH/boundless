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
from typing import Any, Dict, NamedTuple, Optional, Tuple


class Allocation(NamedTuple):
    """一次凭据分配的完整结果。

    刻意不是「一对 api_id/api_hash」——隔离是**三件套**（独立凭据 + 独立出口 IP
    + 独立设备指纹），把出口和档位和凭据放在同一个返回值里，调用方就不会
    「拿了凭据忘了拿 IP」。指纹不在这里：它由本地种子派生，不经过中央池。

    🔒 api_hash 与 proxy 里的账密都是密钥，禁止落日志/回显。
    """

    api_id: int
    api_hash: str
    source: str = "credpool"           # credpool | config
    tier: str = "free"                 # 服务端判定的生效会员档
    proxy: Optional[Dict[str, Any]] = None  # {scheme,host,port,username,password}

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


# ── 池内号的凭据本地缓存 ────────────────────────────────────────────────────
# 为什么要缓存（2026-07-27 实测发现）：一个 session 是用某一组 api_id/api_hash
# 建的。池内号在池不可达时若回落到「配置自带凭据」，就成了「A 的 session 报
# B 的 api_id」—— Telegram 侧明确的风控信号。原设计「只存 key 不存凭据」在
# 池挂时会踩这个坑。
# 两个可选修法：① 拒绝启动（安全但池一挂全部协议号掉线）；② 缓存该号上次分配到
# 的凭据（池挂时照常在线，且用的就是 session 原本那组）。选 ②，① 作为无缓存时
# 的兜底。安全性上不是升级暴露面：注册表里本来就存着 session_string（等于账号
# 完全访问权），api_hash 是应用级凭据，敏感度远低于它。
META_CRED_KEY = "credpool_cred"


def cached_cred_of(account: Optional[Dict[str, Any]]) -> Optional["Allocation"]:
    """取该号上次从池里拿到的凭据（含出口/档位）；没有则 None。"""
    c = ((account or {}).get("meta") or {}).get(META_CRED_KEY) or {}
    if not isinstance(c, dict):
        return None
    try:
        api_id = int(c.get("api_id") or 0)
    except (TypeError, ValueError):
        return None
    api_hash = str(c.get("api_hash") or "")
    if not api_id or not api_hash:
        return None
    proxy = c.get("proxy") if isinstance(c.get("proxy"), dict) else None
    return Allocation(api_id, api_hash, "credpool_cache",
                      str(c.get("tier") or "free"), proxy)


def remember_cred(account: Optional[Dict[str, Any]], alloc: "Allocation") -> None:
    """把本次池分配结果缓存到账号 meta（best-effort，失败绝不影响启动）。

    只在内容变化时写库——worker 每次拉起都会走到这里，没变就别碰磁盘。
    """
    acct_id = str((account or {}).get("account_id") or "")
    platform = str((account or {}).get("platform") or "telegram")
    if not acct_id:
        return
    payload = {"api_id": int(alloc.api_id), "api_hash": str(alloc.api_hash),
               "tier": str(alloc.tier or "free")}
    if alloc.proxy:
        payload["proxy"] = dict(alloc.proxy)
    old = ((account or {}).get("meta") or {}).get(META_CRED_KEY) or {}
    if isinstance(old, dict) and {k: old.get(k) for k in payload} == payload:
        return
    try:
        from src.integrations.account_registry import get_account_registry

        get_account_registry().upsert(platform, acct_id,
                                      meta={META_CRED_KEY: payload}, merge_meta=True)
    except Exception:  # noqa: BLE001
        logger.debug("[credpool] 凭据缓存写入失败（不影响启动）", exc_info=True)



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


# ── 观测（best-effort，任何异常都不得影响凭据解析）──────────────────────────

def _stat_resolve(source: str, tier: Optional[str] = None, with_proxy: bool = False) -> None:
    try:
        from src.integrations.credpool_stats import get_credpool_stats

        get_credpool_stats().record_resolve(source, tier, with_proxy)
    except Exception:  # noqa: BLE001
        pass


def _stat_fallback(reason: str, detail: str = "") -> None:
    try:
        from src.integrations.credpool_stats import get_credpool_stats

        get_credpool_stats().record_fallback(reason, detail)
    except Exception:  # noqa: BLE001
        pass


def _stat_report(ok: bool) -> None:
    try:
        from src.integrations.credpool_stats import get_credpool_stats

        get_credpool_stats().record_report(ok)
    except Exception:  # noqa: BLE001
        pass


def _stat_release() -> None:
    try:
        from src.integrations.credpool_stats import get_credpool_stats

        get_credpool_stats().record_release()
    except Exception:  # noqa: BLE001
        pass


def license_key(config: Dict[str, Any]) -> str:
    """客户的会员卡密：环境变量优先（不入库），回落配置。

    只是**送去服务端定档**的凭证，档位由智控查库判定——这里传什么都不能提权。
    """
    return (os.environ.get("CREDPOOL_LICENSE_KEY", "").strip()
            or str(_cfg(config).get("license_key") or "").strip())


_MACHINE_ID: str = ""


def machine_id() -> str:
    """本机稳定标识（卡密绑机用）。

    卡密首次使用会被服务端绑到这个 id 上，之后换机就拿不到付费档——这是收口
    「一张卡密贴到几台机器上白拿几份专属凭据」的关键。因此它必须：
    **重启不变、重装不变**（存在数据目录里，与配置同寿命），且**不含任何可反查
    身份的信息**（纯随机，不用 MAC / 主机名 / 序列号）。
    """
    global _MACHINE_ID
    env = os.environ.get("CREDPOOL_MACHINE_ID", "").strip()
    if env:
        return env
    if _MACHINE_ID:
        return _MACHINE_ID

    path = None
    try:
        from src.utils.config_manager import ConfigManager

        path = Path(ConfigManager().config_path).parent / ".credpool_machine_id"
        if path.is_file():
            got = path.read_text(encoding="utf-8").strip()
            if got:
                _MACHINE_ID = got
                return _MACHINE_ID
    except Exception:  # noqa: BLE001
        logger.debug("[credpool] 机器标识读取失败", exc_info=True)

    _MACHINE_ID = f"chatx-{uuid.uuid4().hex}"
    try:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_MACHINE_ID, encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 落不了盘就本进程内用，重启会换（宁可少给权益）
        logger.warning("[credpool] 机器标识无法持久化，重启后会变（卡密可能需重新绑机）")
    return _MACHINE_ID


def allocate(
    config: Dict[str, Any],
    pool_key: str,
    account_id: Optional[str] = None,
    prefer_api_id: Optional[int] = None,
) -> Optional[Allocation]:
    """按粘定键向中央池索取一次分配；任何失败返回 None（调用方回落自带凭据）。

    ``prefer_api_id``＝该号 session 已绑定的那组凭据（来自本地缓存）。粘定本来
    由池侧按 key 保证，但池的分配台账**是会丢的**（早期临时模式、库被换、人工
    清理…；2026-07-27 实测就有一个号在池里查不到记录）。台账一丢，池会当新号
    重新分配，多凭据池里就可能悄悄换成另一组 api_id —— 对已有 session 就是错配。
    传上这个钉子，让池优先给回同一组。
    """
    if not credpool_enabled(config) or not pool_key:
        return None
    client = _make_client(config)
    if client is None:
        _stat_fallback("no_client")
        return None
    if not client.configured():
        logger.warning("[credpool] 未配置服务 token（环境变量 CREDPOOL_SERVICE_TOKEN），跳过中央池")
        _stat_fallback("no_token")
        return None

    try:
        res = client.allocate(phone=pool_key, account_id=account_id,
                              api_id=prefer_api_id,
                              license_key=license_key(config) or None,
                              machine_id=machine_id())
    except Exception as exc:  # noqa: BLE001 —— 瘦客户端契约上不抛，这里是双保险
        logger.debug("[credpool] allocate 异常", exc_info=True)
        _stat_fallback("error", str(exc))
        return None

    if not res.get("available"):
        logger.warning("[credpool] 中央池不可达，回落自带凭据：%s", res.get("error"))
        _stat_fallback("unreachable", str(res.get("detail") or res.get("error") or ""))
        return None
    if not res.get("success"):
        msg = res.get("message") or res.get("error")
        logger.warning("[credpool] 分配被拒（池空/越权？）：%s", msg)
        _stat_fallback("rejected", str(msg or ""))
        return None

    data = res.get("data") or {}
    api_id, api_hash = data.get("api_id"), data.get("api_hash")
    try:
        if api_id and api_hash:
            tier = str(data.get("member_level") or "free")
            proxy = _sanitize_proxy(data.get("proxy"))
            # 🔒 只记 api_id / 档位 / 有无出口，绝不记 api_hash 与代理账密
            logger.info("[credpool] 已分配 api_id=%s key=%s tier=%s proxy=%s",
                        api_id, pool_key, tier, "yes" if proxy else "no")
            _stat_resolve("credpool", tier, bool(proxy))
            return Allocation(int(api_id), str(api_hash), "credpool", tier, proxy)
    except (TypeError, ValueError):
        logger.warning("[credpool] 池返回的 api_id 非法，回落自带凭据")
        _stat_fallback("bad_data", "api_id not an int")
        return None
    _stat_fallback("bad_data", "empty api_id/api_hash")
    return None


def _sanitize_proxy(raw: Any) -> Optional[Dict[str, Any]]:
    """把池返回的出口整成 `_to_pyrogram_proxy` 认得的形状；不完整就当没有。

    宁可不用代理也不能用半个代理——端口缺失之类的脏数据会让整条连接起不来，
    而「没有代理」只是回落直连，不阻塞登录。
    """
    if not isinstance(raw, dict):
        return None
    host = str(raw.get("host") or "").strip()
    try:
        port = int(raw.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if not host or port <= 0:
        return None
    out: Dict[str, Any] = {
        "scheme": str(raw.get("scheme") or "socks5").strip() or "socks5",
        "host": host,
        "port": port,
    }
    if str(raw.get("username") or "").strip():
        out["username"] = str(raw["username"]).strip()
    if str(raw.get("password") or "").strip():
        out["password"] = str(raw["password"])
    return out


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
        res = client.report(api_id=str(api_id), success=success, error=error, phone=pool_key)
        _stat_report(bool(res.get("available")))
    except Exception:  # noqa: BLE001
        logger.debug("[credpool] report 异常（忽略）", exc_info=True)
        _stat_report(False)


def release(config: Dict[str, Any], pool_key: str) -> None:
    """账号删除/退登时把容量还给池；best-effort。"""
    if not credpool_enabled(config) or not pool_key:
        return
    client = _make_client(config)
    if client is None or not client.configured():
        return
    try:
        client.release(phone=pool_key)
        _stat_release()
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


def resolve_for_account(
    config: Dict[str, Any],
    account: Optional[Dict[str, Any]] = None,
    pool_key: Optional[str] = None,
) -> Optional[Allocation]:
    """统一分配解析：中央池优先，回落配置自带凭据；都没有则 None。

    两类调用方，回落规则**刻意不同**（这是封号级的区别）：

    - **新登录**（``account=None``，pool_key 现场生成）：池不可达 → 回落配置自带
      凭据完全安全，session 就是用这组建的，之后也不会被登记成池内号。
    - **既有号**（粘定键来自 account meta，session 已绑在某组池凭据上）：绝不
      回落配置凭据——那会让 Telegram 看到「session 与 api_id 不匹配」。顺序是
      池 → 该号的本地凭据缓存 → 都没有就返回 None（宁可这一个号暂时不上线，
      也不能带着错配的 api_id 连上去）。

    回落来的 Allocation 只有凭据（``source="config"``，无出口、free 档）——
    自带凭据本来就不带隔离能力，如实反映即可。
    """
    meta_key = pool_key_of(account)  # 非空＝既有号，session 已绑池凭据
    key = pool_key or meta_key
    if key:
        cached = cached_cred_of(account) if meta_key else None
        alloc = allocate(config, key, account_id=(account or {}).get("account_id"),
                         prefer_api_id=(cached.api_id if cached else None))
        if alloc:
            if cached and alloc.api_id != cached.api_id:
                # 池给了另一组：台账丢了 or 人工换过凭据。session 绑的是缓存那组，
                # 换过去必然错配 —— 宁可用缓存继续跑（凭据真被停用就是连不上，
                # 运营重扫即可），也不能带着不匹配的 api_id 连上去。
                logger.error(
                    "[credpool] 池返回 api_id=%s 与该号 session 绑定的 %s 不一致，"
                    "按 session 绑定的那组执行（池台账可能丢了记录）",
                    alloc.api_id, cached.api_id)
                _stat_resolve("credpool_cache", cached.tier, bool(cached.proxy))
                return cached
            if meta_key:
                remember_cred(account, alloc)
            return alloc
        if meta_key:
            if cached:
                logger.warning(
                    "[credpool] 池不可达，改用该号上次分配到的凭据 api_id=%s"
                    "（不能换成自带凭据：会与既有 session 的 api_id 错配）",
                    cached.api_id)
                _stat_resolve("credpool_cache", cached.tier, bool(cached.proxy))
                return cached
            logger.error(
                "[credpool] 池不可达且该号无凭据缓存 → 拒绝以自带凭据启动"
                "（session 与 api_id 错配会触发风控）。等池恢复后自动上线。")
            _stat_fallback("pool_down_no_cache")
            return None

    # 既有号优先用**自己登录时**绑定的凭据缓存（P2-⑨，2026-08-10）：托管派发的
    # 机器换组后（隔离/换发），config 里注入的是**新组**凭据，而旧账号的 session
    # 是旧组建的——直接回落 config 就是「session×api_id 错配」风控信号（与上方
    # 池内号的不变量同一条）。缓存由登录成功时写入（telegram_protocol_login），
    # 自建部署的账号没有这行缓存 → 行为与旧版完全一致。
    hosted_cached = cached_cred_of(account)
    if hosted_cached is not None:
        _stat_resolve("credpool_cache", hosted_cached.tier, bool(hosted_cached.proxy))
        return hosted_cached

    from src.integrations.telegram_protocol_login import resolve_credentials

    local = resolve_credentials(config)
    # 只在开了池的前提下记账——没开池时这条链路本就不该被观测
    if credpool_enabled(config):
        _stat_resolve("config" if local else "none")
    if not local:
        return None
    # 托管派发若随凭据带了出口（官网池 P2-⑨），新登录同样三件套齐发；
    # 无托管代理时保持旧语义（自带凭据不带隔离能力，如实反映）。
    tg_cfg = config.get("telegram") if isinstance(config.get("telegram"), dict) else {}
    hosted_proxy = _sanitize_proxy(tg_cfg.get("_hosted_proxy"))
    return Allocation(local[0], local[1], "config", "free", hosted_proxy)


# ── 异步侧包装 ───────────────────────────────────────────────────────────────
# 瘦客户端用 stdlib urllib（阻塞）。登录 provider 与 runner worker 都在事件循环里，
# 直接调用会把整个 loop 卡住最多 timeout 秒。故异步调用方一律走这两个包装。
# 池未启用时不产生任何 I/O，直接同步返回（省掉线程切换开销）。

async def aresolve_for_account(
    config: Dict[str, Any],
    account: Optional[Dict[str, Any]] = None,
    pool_key: Optional[str] = None,
) -> Optional[Allocation]:
    if not credpool_enabled(config):
        return resolve_for_account(config, account, pool_key)
    import asyncio

    return await asyncio.to_thread(resolve_for_account, config, account, pool_key)


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
