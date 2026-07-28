"""中央凭据池接线门禁：登录 provider / runner worker / 注册门控三处。

这三处是「新用户不用申请 api_id」能否成立的全部落点，任何一处退回读死配置，
要么用户又被逼去 my.telegram.org，要么 runner 用错凭据把 session 跑坏。
"""
from __future__ import annotations

import pytest

from src.integrations import account_orchestrator as ao
from src.integrations import credpool_bridge as cb
from src.integrations import platform_login as pl
from src.integrations import telegram_protocol_login as tpl
from tests._source_block import source_block


POOL_CFG = {
    "platform_login": {
        "telegram": {"protocol_enabled": True, "credpool": {"enabled": True}},
    },
}


@pytest.fixture(autouse=True)
def _clean_registry():
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)
    yield
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)


# ── 注册门控 ─────────────────────────────────────────────────────────────

def test_registers_with_pool_and_no_local_creds():
    """开池即可注册——这正是「用户没有自己的 api_id 也能扫码」的前提。"""
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    assert tpl.maybe_register(POOL_CFG) is True
    assert pl.mode_available("telegram", "protocol") is True


def test_still_gated_by_protocol_enabled():
    """开了池但没开 protocol_enabled 仍不注册（两道闸互不替代）。"""
    cfg = {"platform_login": {"telegram": {"credpool": {"enabled": True}}}}
    assert tpl.maybe_register(cfg) is False


def test_no_pool_no_creds_still_refuses():
    assert tpl.maybe_register(
        {"platform_login": {"telegram": {"protocol_enabled": True}}}) is False


# ── 登录 provider：无凭据时的提示不得回归 ─────────────────────────────────

async def test_provider_reports_missing_creds_when_pool_unreachable(monkeypatch):
    monkeypatch.setattr(cb, "_make_client", lambda config: None)
    provider = tpl.make_provider(POOL_CFG)
    res = await provider(None, "telegram", "protocol", "acc1")
    assert "api_id" in res.get("instruction", "")


def test_allocation_shape_is_the_isolation_contract():
    """Allocation 就是「三隔离」在类型上的表达，字段少一个就有一件套会被忘掉。"""
    assert cb.Allocation._fields == ("api_id", "api_hash", "source", "tier", "proxy")


# ── 异步安全：两个 async 落点都必须走异步包装，不能阻塞事件循环 ───────────

def test_login_provider_uses_async_resolver():
    src = source_block(tpl, "def make_provider(")
    assert "aresolve_for_account" in src
    assert "await" in src


def test_worker_uses_async_resolver_and_account_meta():
    src = source_block(ao, "class TelegramProtocolWorker")
    assert "aresolve_for_account" in src
    # 必须把 account 传进去，否则读不到 meta 里的粘定键 → 换凭据 → session 坏
    assert "account=self.account" in src


# ── 出口优先级：显式配置代表人的意图，不得被自动分配覆盖 ──────────────────

def test_explicit_proxy_wins_over_pool_proxy_in_login():
    src = source_block(tpl, "def make_provider(")
    assert '(ctx or {}).get("proxy")' in src and "alloc.proxy" in src
    # ctx 的必须在前（or 短路 → 左侧优先）
    assert src.index('(ctx or {}).get("proxy")') < src.index("alloc.proxy")


def test_account_bound_proxy_wins_over_pool_proxy_in_worker():
    src = source_block(ao, "class TelegramProtocolWorker")
    assert "if not proxy and alloc.proxy:" in src, "账号显式绑定的代理必须优先"


# ── 粘定键落库：丢了它，下次拉起就会换凭据 ────────────────────────────────

def test_provider_persists_sticky_key_and_reports():
    src = source_block(tpl, "def make_provider(")
    assert "_cpb.META_KEY" in src, "扫码成功必须把粘定键写进账号 meta"
    # 不绑配置变量名（它从 config 改成过 cfg）——只钉「成败两条路都回报」这个语义
    assert "api_id, True" in src, "登录成功必须回报池的健康分"
    assert "api_id, False" in src, "登录失败必须回报，否则坏凭据不会被换掉"


def test_meta_key_is_stable_contract():
    """meta 键名是跨版本契约（改名会让存量账号丢失粘定键）。"""
    assert cb.META_KEY == "credpool_key"


# ── 存量号不得被换凭据：封号级不变量 ──────────────────────────────────────
# 一个 session 是用某一组 api_id/api_hash 建的。开池之前登录的号（meta 里没有
# 粘定键）如果在 worker 重启时被分配一组**新的**池凭据，就变成「A 的 session
# 报 B 的 api_id」——Telegram 侧是明确的风控信号。故：无粘定键 → 必须回落本地
# 凭据，绝不向池索取。这条比指纹更重（指纹漂移是可疑，凭据错配是直接触发）。

def test_legacy_account_keeps_local_credentials():
    """meta 无粘定键的存量号：不碰池，凭据必须来自本地配置。"""
    calls = []

    def _spy_allocate(*a, **kw):  # pragma: no cover - 不该被调用
        calls.append((a, kw))
        return cb.Allocation(999999, "pool_hash", "credpool")

    orig_allocate = cb.allocate
    cb.allocate = _spy_allocate
    try:
        alloc = cb.resolve_for_account(
            {"telegram": {"api_id": 12345, "api_hash": "local_hash"},
             "platform_login": {"telegram": {
                 "credpool": {"enabled": True, "base_url": "http://127.0.0.1:1",
                              "service_token": "t"}}}},
            account={"account_id": "legacy-1", "meta": {}},  # ← 存量号：无粘定键
        )
    finally:
        cb.allocate = orig_allocate

    assert not calls, "存量号绝不能向中央池索取凭据（会与既有 session 的 api_id 错配）"
    assert alloc is not None and alloc.source == "config"
    assert alloc.api_id == 12345 and alloc.api_hash == "local_hash"
    assert alloc.proxy in (None, {}), "存量号不该被塞入池出口（同理会改变既有 session 的出口特征）"


def test_pool_key_only_comes_from_meta():
    """粘定键只能来自 meta——不得由 account_id 之类现场推导（否则存量号也会命中池）。"""
    src = source_block(cb, "def pool_key_of(")
    assert "META_KEY" in src, "粘定键必须读 meta 的 META_KEY"
    for derived in ("account_id", "phone", "uuid", "hash("):
        assert derived not in src, f"粘定键不得由 {derived} 现场推导"


# ── 池内号在池不可达时不得换成自带凭据（封号级不变量，2026-07-27 实测发现）──
# 池内号的 session 是用某组池凭据建的。池挂了若回落 config 凭据，Telegram 看到
# 的是「这条 session 报出了另一个 api_id」。正确顺序：池 → 该号凭据缓存 → None。

def _pool_down_cfg():
    return {"telegram": {"api_id": 111, "api_hash": "local_hash"},
            "platform_login": {"telegram": {"credpool": {
                "enabled": True, "base_url": "http://127.0.0.1:1",
                "service_token": "t"}}}}


def test_pooled_account_uses_cached_cred_when_pool_down():
    """池挂 + 有缓存 → 用缓存那组（照常上线，且与 session 一致）。"""
    alloc = cb.resolve_for_account(_pool_down_cfg(), account={
        "account_id": "a1", "platform": "telegram",
        "meta": {cb.META_KEY: "chatx:deadbeef",
                 cb.META_CRED_KEY: {"api_id": 777, "api_hash": "pool_hash",
                                    "tier": "gold",
                                    "proxy": {"host": "1.2.3.4", "port": 1080}}}})
    assert alloc is not None, "有缓存就该能上线，不该把号挂在线下"
    assert (alloc.api_id, alloc.api_hash) == (777, "pool_hash")
    assert alloc.source == "credpool_cache"
    assert alloc.tier == "gold", "档位要一起缓存，否则降级期权益凭空消失"
    assert alloc.proxy and alloc.proxy["host"] == "1.2.3.4", "出口也要稳定（换 IP 同样是风控信号）"


def test_pooled_account_refuses_config_creds_when_no_cache():
    """池挂 + 无缓存 → 返回 None（拒绝错配启动），绝不给出 config 凭据。"""
    alloc = cb.resolve_for_account(_pool_down_cfg(), account={
        "account_id": "a2", "platform": "telegram",
        "meta": {cb.META_KEY: "chatx:deadbeef"}})
    assert alloc is None, "池内号无缓存时必须拒绝启动，不能带着错配的 api_id 连上去"


def test_fresh_login_still_falls_back_to_config():
    """新登录（account=None）池挂 → 仍回落自带凭据：session 就是用它建的，安全。"""
    alloc = cb.resolve_for_account(_pool_down_cfg(), pool_key="chatx:fresh")
    assert alloc is not None and alloc.source == "config"
    assert alloc.api_id == 111


def test_login_persists_credential_cache():
    """扫码成功必须把凭据缓存落库，否则池挂着重启的号第一次就上不来。"""
    src = source_block(tpl, "def make_provider(")
    assert "_cpb.META_CRED_KEY" in src, "登录成功必须缓存本次分配到的凭据"


def test_cache_write_is_atomic_merge():
    """缓存写入必须 merge_meta——整块覆盖会抹掉人设绑定/session_string（有过生产事故）。"""
    src = source_block(cb, "def remember_cred(")
    assert "merge_meta=True" in src, "写 meta 必须用原子浅合并"


# ── 池台账丢了记录时，不得把号换到另一组凭据上 ────────────────────────────
# 池的分配台账是会丢的（早期临时模式/换库/人工清理；2026-07-27 实测就有一例）。
# 台账一丢，池把该号当新号重新分配 —— 多凭据池里就可能给出另一组 api_id。

def test_cached_api_id_is_pinned_to_pool():
    """有缓存时必须把该 api_id 当钉子传给池，让它给回同一组。"""
    seen = {}

    def _spy(config, pool_key, account_id=None, prefer_api_id=None):
        seen["prefer"] = prefer_api_id
        return cb.Allocation(777, "pool_hash", "credpool", "gold")

    orig, cb.allocate = cb.allocate, _spy
    try:
        cb.resolve_for_account(_pool_down_cfg(), account={
            "account_id": "a3", "platform": "telegram",
            "meta": {cb.META_KEY: "chatx:x",
                     cb.META_CRED_KEY: {"api_id": 777, "api_hash": "pool_hash"}}})
    finally:
        cb.allocate = orig
    assert seen.get("prefer") == 777, "必须把 session 绑定的 api_id 传给池做钉子"


def test_mismatched_pool_answer_loses_to_session_binding():
    """池给了另一组 → 按 session 绑定的那组执行（换过去必然错配）。"""
    def _spy(config, pool_key, account_id=None, prefer_api_id=None):
        return cb.Allocation(999, "other_hash", "credpool", "gold")

    orig, cb.allocate = cb.allocate, _spy
    try:
        alloc = cb.resolve_for_account(_pool_down_cfg(), account={
            "account_id": "a4", "platform": "telegram",
            "meta": {cb.META_KEY: "chatx:x",
                     cb.META_CRED_KEY: {"api_id": 777, "api_hash": "pool_hash"}}})
    finally:
        cb.allocate = orig
    assert alloc.api_id == 777, "session 绑定优先于池的新答案"
    assert alloc.source == "credpool_cache"


def test_fresh_login_has_no_pin():
    """新登录没有 session 要保，不该乱传钉子（否则第一次就锁死在某一组上）。"""
    seen = {}

    def _spy(config, pool_key, account_id=None, prefer_api_id=None):
        seen["prefer"] = prefer_api_id
        return cb.Allocation(5, "h", "credpool")

    orig, cb.allocate = cb.allocate, _spy
    try:
        cb.resolve_for_account(_pool_down_cfg(), pool_key="chatx:fresh")
    finally:
        cb.allocate = orig
    assert seen.get("prefer") is None


# ── 放弃的登录必须归还池容量（否则每次没扫完都永久吃掉一个账号位）────────────

def test_abandoned_login_releases_capacity():
    src = source_block(tpl, "def make_provider(")
    assert "_release_if_abandoned" in src, "登录未完成必须归还容量"
    for path in ("cancelled", "password_failed"):
        assert path in src, f"缺少 {path} 归还路径"
    assert '("expired", "failed")' in src, "码过期/登录失败也必须归还"


def test_rescan_releases_orphaned_old_key():
    """重扫同号会换粘定键，旧键那笔必须归还——否则反复重扫能吃干一组凭据的名额。"""
    src = source_block(tpl, "def make_provider(")
    assert "_old_key" in src and "_cpb.release(cfg, _old_key)" in src, \
        "重扫换键必须归还旧键容量"
    # 必须读**换键前**的注册表条目，写完再读就永远拿不到旧键了
    assert src.index("_prev = get_account_registry().get(") < src.index("_prev.get(\"meta\")"), \
        "旧键要在 upsert 之前读出来"


def test_start_failure_releases_capacity():
    """起不来就没人来 cancel/poll，容量只能在异常处就地归还。"""
    src = source_block(tpl, "def make_provider(")
    idx = src.index("_cp.report(cfg, api_id, False")
    assert "_cp.release(cfg, used_pool_key)" in src[idx:idx + 400]


def test_authorized_login_does_not_release():
    """扫成功的号必须**留住**分配——它后面 runner 还要凭同一个 key 取回同一组凭据。"""
    src = source_block(tpl, "def make_provider(")
    assert 'getattr(login, "status", "") != "authorized"' in src


# ── 开关热生效：ConfigManager 热重载会整个换掉 config 字典 ────────────────────

async def test_provider_sees_live_config_not_startup_snapshot(monkeypatch):
    """注册时池是关的，之后配置热重载开了池 → provider 必须看得见。

    没有这条，`credpool.enabled` 就只能靠**重启生产实例**才生效——而重启是
    全站 15-30 秒不可用 + 10 分钟冷却闸，等于把一个开关变成一次运维事件。
    """
    seen = []

    def _fake_enabled(cfg):
        seen.append(bool(((cfg.get("platform_login") or {}).get("telegram") or {})
                         .get("credpool", {}).get("enabled")))
        return False  # 让它回落 config 凭据，用例只关心「看到了哪份配置」

    monkeypatch.setattr(cb, "credpool_enabled", _fake_enabled)

    startup_cfg = {"telegram": {"api_id": 1, "api_hash": "h"}}
    live_cfg = {"telegram": {"api_id": 1, "api_hash": "h"},
                "platform_login": {"telegram": {"credpool": {"enabled": True}}}}
    holder = {"cfg": startup_cfg}

    provider = tpl.make_provider(startup_cfg, config_getter=lambda: holder["cfg"])
    try:
        await provider(None, "telegram", "protocol", "acc1")
    except Exception:  # pyrogram 真连会失败，这里只看配置读取
        pass
    holder["cfg"] = live_cfg  # 模拟热重载换了字典对象
    try:
        await provider(None, "telegram", "protocol", "acc1")
    except Exception:
        pass

    assert seen[:1] == [False], "首次应看到启动时的配置"
    assert True in seen[1:], "热重载后必须看到新配置（否则开关只能重启生效）"


async def test_provider_falls_back_when_getter_broken(monkeypatch):
    """取实时配置失败不能把登录带崩——回落注册时那份即可。"""
    cfg = {"telegram": {"api_id": 1, "api_hash": "h"}}

    def _boom():
        raise RuntimeError("配置源炸了")

    provider = tpl.make_provider(cfg, config_getter=_boom)
    monkeypatch.setattr(cb, "_make_client", lambda config: None)
    res = await provider(None, "telegram", "protocol", "acc1")
    # 有本地凭据 → 不会返回「未配置」提示；真连 pyrogram 会抛，说明已越过配置读取
    assert res is None or "instruction" not in res or "api_id" not in res.get("instruction", "")


def test_maybe_register_refreshes_latest_config_even_when_registered():
    """已注册后再调用仍要刷新实时配置——热更新全靠这一步。"""
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    first = dict(POOL_CFG)
    assert tpl.maybe_register(first) is True
    second = {"telegram": {"api_id": 9, "api_hash": "z"},
              "platform_login": {"telegram": {"protocol_enabled": True}}}
    assert tpl.maybe_register(second) is True  # 幂等短路
    assert tpl._latest_config is second, "短路也必须刷新 _latest_config"


# ── 取块工具自身的诚实性（它一失灵，上面所有接线断言都会变成摆设）────────────

def test_source_block_scopes_to_the_right_block():
    blk = source_block(cb, "def release_for_account_bg(")
    assert "credpool-release" in blk
    # 不得把隔壁函数也吞进来，否则断言范围失控、形同全文搜索
    assert "def resolve_credentials_for_account(" not in blk


def test_source_block_fails_loudly_when_missing():
    with pytest.raises(AssertionError):
        source_block(cb, "def this_function_does_not_exist(")


# ── 删号还容量：不还会造成池容量缓慢泄漏 ──────────────────────────────────

# ── 桌面打包：瘦客户端在引擎目录之外，不显式带上就会静默失效 ──────────────

def test_client_found_in_source_tree():
    """源码态必须能真找到 platform/credpool/credpool_client.py。"""
    assert any(p.is_file() for p in cb._client_path_candidates())


def test_frozen_bundle_path_is_probed(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "_MEIPASS", r"X:\bundle", raising=False)
    cands = [str(p) for p in cb._client_path_candidates()]
    assert any(c.startswith(r"X:\bundle") and c.endswith("credpool_client.py")
               for c in cands), "冻结态必须先找 sys._MEIPASS 下的副本"


def test_desktop_build_bundles_credpool_client():
    """桌面包必须带上瘦客户端——它在引擎目录之外，PyInstaller 静态分析看不见。

    断言的是**行为**（DATAS 里真有一条 credpool → platform/credpool 的映射），
    不是源码字面量：打包清单被重构成 f-string 拼路径也应照样通过，
    只有真的漏打才红。（早前写成字面量断言，同仓另一条线把它改成
    `f"platform/{_pkg}"` 循环登记后就误红了——测试太脆等于制造噪音。）
    """
    import importlib.util
    from pathlib import Path as _P

    path = (_P(__file__).resolve().parents[1]
            / "desktop" / "build" / "build_backend.py")
    spec = importlib.util.spec_from_file_location("_chatx_build_backend", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 顶层只算路径，不跑打包

    targets = {str(dst).replace("\\", "/") for _src, dst in mod.DATAS}
    assert "platform/credpool" in targets, (
        f"桌面打包清单缺 credpool 瘦客户端 → 桌面版中央池会静默失效；现有目标={sorted(targets)}")
    src = next(s for s, d in mod.DATAS if str(d).replace("\\", "/") == "platform/credpool")
    assert (_P(src) / "credpool_client.py").is_file(), "登记的源路径里没有瘦客户端本体"


def test_registry_remove_releases_pool_capacity():
    from src.integrations import account_registry as ar

    src = source_block(ar, "def remove(self, platform")
    assert "release_for_account_bg" in src


def test_release_bg_is_non_blocking_and_platform_scoped(monkeypatch):
    """非 telegram 平台不该触发任何池调用；telegram 走后台线程不阻塞调用方。"""
    started = []
    monkeypatch.setattr(cb, "release", lambda *a, **kw: started.append(a))

    cb.release_for_account_bg("line", "acc1")
    assert started == []

    import threading

    spawned = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target
            spawned.append(name)

        def start(self):
            pass  # 不真跑，只验证「走的是线程、没在调用线程里同步打 HTTP」

    monkeypatch.setattr(threading, "Thread", _FakeThread)
    cb.release_for_account_bg("telegram", "acc1")
    assert spawned == ["credpool-release"]
