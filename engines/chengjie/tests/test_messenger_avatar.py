"""Messenger（及 WhatsApp）头像代理的可测内核。

头像端点 ``/api/platforms/{platform}/{account_id}/avatar`` 的两块纯/半纯内核抽成模块级：
- ``_avatar_disk_paths``：磁盘缓存路径构造 + **路径穿越消毒**（文件名只保留字母数字/`_-`）；
- ``_download_and_cache_avatar``：Node 直链 → 下载落 /static → 302；空 url/失败 → 写 .none 负缓存 + 404。

Node 侧的 scontent 抓取与真实下载须真机联调（不在单测覆盖内）；这里锁死 Python 侧不变量：
per-platform 子目录、文件名安全、空 url 走负缓存、下载成功 302、下载失败优雅 404。

2026-08-05 追加「上游挂死熔断」不变量（WA socket 半死时 Baileys profilePictureUrl 无限
挂起 → 每个头像吃满超时 → 几行 WA 会话就占死浏览器同源 6 连接、全页请求饿死）：
- ``_proto_avatar_cooling`` / ``_mark_proto_avatar_bad`` / ``_clear_proto_avatar_bad``
  账号级熔断三件套（60s 窗、过期自清、成功清除）；
- 路由接线静态钉：回源必须带 4s 短超时、仅**超时**开熔断（业务快败不开）、
  熔断检查在磁盘缓存之后（缓存命中不受熔断影响）。
"""
import inspect
import os

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

import src.integrations.protocol_bridge as pb
import src.web.routes.unified_inbox_account_routes as uiar
from src.web.routes.unified_inbox_account_routes import (
    _avatar_disk_paths,
    _avatar_src_fp,
    _avatar_src_sidecar,
    _avatar_versioned_path,
    _clear_proto_avatar_bad,
    _download_and_cache_avatar,
    _mark_proto_avatar_bad,
    _proto_avatar_cooling,
)


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "protocol_media_root", lambda: tmp_path)
    return tmp_path


def test_avatar_disk_paths_messenger_subdir_and_url(media_root):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "100012345678", "987654")
    assert jpg.parent == media_root / "messenger" / "avatars"
    assert jpg.name == "100012345678_987654.jpg"
    assert none_marker.name == "100012345678_987654.none"
    assert url_path == "/static/protocol_media/messenger/avatars/100012345678_987654.jpg"
    assert jpg.parent.is_dir()  # 目录已按需创建


def test_avatar_disk_paths_per_platform_isolated(media_root):
    wa, _, wa_url = _avatar_disk_paths("whatsapp", "acct", "123")
    mg, _, mg_url = _avatar_disk_paths("messenger", "acct", "123")
    assert wa.parent != mg.parent
    assert "/whatsapp/" in wa_url and "/messenger/" in mg_url


def test_avatar_disk_paths_sanitizes_traversal(media_root):
    # account 混入 ../ 与分隔符、chat_key 混入 /.. → 文件名只留字母数字(account 另允 _-)；
    # 2026-08-14 起消毒**有损**时追加原始值 sha1[:8] 后缀（防不同 id 折叠同名——见碰撞用例）
    jpg, none_marker, url_path = _avatar_disk_paths(
        "messenger", "../../etc/passwd", "9/8/7..6")
    assert ".." not in jpg.name
    assert "/" not in jpg.name and "\\" not in jpg.name
    assert jpg.name.startswith("etcpasswd-") and "_9876-" in jpg.name
    assert ".." not in url_path
    # 产物仍落在受控媒体根内（未逃逸）
    assert str(jpg.resolve()).startswith(str((media_root / "messenger").resolve()))


def test_avatar_disk_paths_lossless_ids_keep_legacy_names(media_root):
    """纯字母数字 id（生产常态：数字账号/线程 id）文件名与旧实现逐字节一致——
    既有磁盘缓存零迁移、零失效。"""
    jpg, _, url = _avatar_disk_paths("messenger", "100012345678", "987654")
    assert jpg.name == "100012345678_987654.jpg"
    assert url.endswith("/100012345678_987654.jpg")


def test_avatar_disk_paths_no_cross_account_collision(media_root):
    """消毒有损的两个不同账号 id 绝不共享缓存文件（旧实现全剔空 → 同名 → 串脸）。"""
    a1, _, _ = _avatar_disk_paths("messenger", "абв", "123")
    a2, _, _ = _avatar_disk_paths("messenger", "где", "123")
    assert a1.name != a2.name
    # 空账号 id 也不产生共享的 "_123" 前缀文件
    a3, _, _ = _avatar_disk_paths("messenger", "", "123")
    assert a3.name not in (a1.name, a2.name)
    assert not a3.name.startswith("_")


def test_avatar_disk_paths_no_same_account_key_collision(media_root):
    """同账号下消毒有损的两个不同 chat_key 绝不折叠为同一文件（user.name vs username）。"""
    k1, _, _ = _avatar_disk_paths("messenger", "acct", "user.name")
    k2, _, _ = _avatar_disk_paths("messenger", "acct", "username")
    assert k1.name != k2.name


async def test_download_empty_url_writes_none_and_404(media_root):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    outcomes = []
    with pytest.raises(HTTPException) as ei:
        await _download_and_cache_avatar("", jpg, none_marker, url_path,
                                         on_outcome=outcomes.append)
    assert ei.value.status_code == 404
    assert none_marker.exists()          # 无头像 → 负缓存标记，避免反复回源
    assert not jpg.exists()
    assert outcomes == ["empty"]         # 观测回调：空 url → empty


async def test_download_empty_url_messenger_skips_neg_cache(media_root):
    # messenger：空 url 是「轮询未缓存」瞬态 → 不写 .none（下次重渲染即重试，轮询补齐后自愈）
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    outcomes = []
    with pytest.raises(HTTPException) as ei:
        await _download_and_cache_avatar("", jpg, none_marker, url_path,
                                         neg_cache=False, on_outcome=outcomes.append)
    assert ei.value.status_code == 404
    assert not none_marker.exists()      # 关键：不留 1 天负缓存
    assert not jpg.exists()
    assert outcomes == ["empty"]


async def test_download_success_writes_jpg_and_302(media_root, monkeypatch):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    none_marker.write_text("", encoding="utf-8")  # 预置旧负缓存 → 成功后应被清除
    _install_fake_httpx(monkeypatch, content=b"\xff\xd8\xffJPEGBYTES")
    outcomes = []

    resp = await _download_and_cache_avatar("https://scontent.example/x.jpg",
                                            jpg, none_marker, url_path,
                                            on_outcome=outcomes.append)
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    # 2026-08-16 起 302 目标带版本参数（无 sidecar 时回落 mtime）——同内容同 URL、
    # 内容换代 URL 换代，浏览器/前端 blob 缓存按最终 URL 失效
    assert resp.headers["location"].startswith(url_path + "?v=")
    assert jpg.read_bytes() == b"\xff\xd8\xffJPEGBYTES"
    assert not none_marker.exists()      # 成功下载 → 清掉旧负缓存
    assert outcomes == ["fetched"]       # 观测回调：下载成功 → fetched


async def test_download_remote_failure_yields_404(media_root, monkeypatch):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    _install_fake_httpx(monkeypatch, raise_exc=RuntimeError("boom"))
    outcomes = []
    with pytest.raises(HTTPException) as ei:
        await _download_and_cache_avatar("https://scontent.example/x.jpg",
                                         jpg, none_marker, url_path,
                                         on_outcome=outcomes.append)
    assert ei.value.status_code == 404
    assert not jpg.exists()              # 失败不留半截文件
    assert outcomes == ["error"]         # 观测回调：下载异常 → error


async def test_download_on_outcome_exception_never_breaks_flow(media_root, monkeypatch):
    # 回调自身抛错也绝不影响主流程（best-effort 观测）
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    _install_fake_httpx(monkeypatch, content=b"img")

    def _boom(_):
        raise RuntimeError("observer down")

    resp = await _download_and_cache_avatar("https://scontent.example/x.jpg",
                                            jpg, none_marker, url_path, on_outcome=_boom)
    assert resp.status_code == 302
    assert jpg.exists()


@pytest.fixture()
def _clean_cooldown():
    uiar._PROTO_AVATAR_BAD_UNTIL.clear()
    yield
    uiar._PROTO_AVATAR_BAD_UNTIL.clear()


def test_proto_avatar_cooldown_three_states(_clean_cooldown, monkeypatch):
    # ① 无记录 → 不冷却
    assert not _proto_avatar_cooling("whatsapp", "639270135480")
    # ② mark 后 → 冷却中；不同账号/平台互不牵连
    _mark_proto_avatar_bad("whatsapp", "639270135480")
    assert _proto_avatar_cooling("whatsapp", "639270135480")
    assert not _proto_avatar_cooling("whatsapp", "other-acct")
    assert not _proto_avatar_cooling("messenger", "639270135480")
    # ③ 窗口过期 → 自清（读即剔除，无需外部打扫）
    import time as _t
    real = _t.monotonic()
    monkeypatch.setattr(uiar.time, "monotonic",
                        lambda: real + uiar._PROTO_AVATAR_COOLDOWN_SEC + 1)
    assert not _proto_avatar_cooling("whatsapp", "639270135480")
    assert "whatsapp:639270135480" not in uiar._PROTO_AVATAR_BAD_UNTIL


def test_proto_avatar_cooldown_clear_on_success(_clean_cooldown):
    _mark_proto_avatar_bad("whatsapp", "a1")
    _clear_proto_avatar_bad("whatsapp", "a1")
    assert not _proto_avatar_cooling("whatsapp", "a1")
    _clear_proto_avatar_bad("whatsapp", "never-marked")   # 清除不存在的键不炸


def test_avatar_route_wiring_pins_timeout_and_breaker():
    """接线静态钉（路由是闭包，不便装配全 app；源码不变量与门禁同哲学）：
    ① WA/MSGR 回源必须带 4s 短超时（旧默认 20s = 浏览器里每个头像挂满 20s 的根因）；
    ② 仅超时开熔断（httpx.TimeoutException；业务快败不占熔断窗）；
    ③ 熔断检查在负缓存检查之后（磁盘/负缓存命中不受熔断影响）；
    ④ 回源成功清除熔断。"""
    src = inspect.getsource(uiar)
    assert src.count("timeout=_PROTO_AVATAR_FETCH_TIMEOUT_SEC") >= 2, "WA/MSGR 回源缺短超时"
    assert src.count("httpx.TimeoutException") >= 2, "熔断必须只认超时特征"
    assert "_clear_proto_avatar_bad(platform, account_id)" in src, "成功路径缺熔断清除"
    # 熔断闸位于 neg_hit 之后、cfg 读取之前（缓存命中不受影响）
    route_src = src[src.index("async def api_platform_avatar"):]
    i_neg = route_src.index('"neg_hit"')
    i_cool = route_src.index("_proto_avatar_cooling(platform, account_id)")
    i_cfg = route_src.index("cfg = (config_manager.config")
    assert i_neg < i_cool < i_cfg, "熔断闸位置漂移（应在负缓存后、回源前）"


# ── 2026-08-16 头像实时刷新（对方换头像 → 穿透 7 天缓存 + URL 版本失效）─────────


def test_avatar_src_fp_path_only_and_stable():
    """指纹只看直链**路径**：scontent 的 oh/oe token 每轮轮换，query 进指纹会把
    「没换头像」误判成「换了」→ 每轮穿透缓存回源。"""
    a = _avatar_src_fp("https://scontent.xx/v/t1.jpg?oh=AAA&oe=111")
    b = _avatar_src_fp("https://scontent.xx/v/t1.jpg?oh=BBB&oe=222")
    c = _avatar_src_fp("https://scontent.xx/v/t2.jpg?oh=AAA&oe=111")
    assert a == b            # token 轮换 → 指纹不变
    assert a != c            # 路径变（真换头像）→ 指纹变
    assert _avatar_src_fp("") == ""
    assert _avatar_src_fp(None) == ""
    assert len(a) == 12


def test_avatar_versioned_path_sidecar_then_mtime_fallback(media_root):
    jpg, _, url_path = _avatar_disk_paths("messenger", "a", "b")
    # 无 jpg 无 sidecar → 原样返回（无判据不硬造版本）
    assert _avatar_versioned_path(url_path, jpg) == url_path
    # 只有 jpg（legacy 存量图）→ mtime 版本，同文件稳定
    jpg.write_bytes(b"img")
    v1 = _avatar_versioned_path(url_path, jpg)
    assert v1.startswith(url_path + "?v=")
    assert _avatar_versioned_path(url_path, jpg) == v1
    # 有 sidecar → sidecar 指纹优先（与下载来源一致，跨机器/跨迁移稳定）
    _avatar_src_sidecar(jpg).write_text("deadbeef0123", encoding="utf-8")
    assert _avatar_versioned_path(url_path, jpg) == url_path + "?v=deadbeef0123"


async def test_download_with_src_fp_writes_sidecar(media_root, monkeypatch):
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    _install_fake_httpx(monkeypatch, content=b"img2")
    fp = _avatar_src_fp("https://scontent.xx/v/new.jpg?oh=x")
    resp = await _download_and_cache_avatar(
        "https://scontent.xx/v/new.jpg?oh=x", jpg, none_marker, url_path, src_fp=fp)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"{url_path}?v={fp}"
    assert _avatar_src_sidecar(jpg).read_text(encoding="utf-8") == fp


async def test_download_failure_serves_stale_when_cached(media_root, monkeypatch):
    """陈旧穿透回源失败 → 退回已有旧图（旧头像好过裂图），不 404。"""
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    jpg.write_bytes(b"old-img")
    _install_fake_httpx(monkeypatch, raise_exc=RuntimeError("cdn down"))
    outcomes = []
    resp = await _download_and_cache_avatar(
        "https://scontent.xx/v/new.jpg", jpg, none_marker, url_path,
        on_outcome=outcomes.append)
    assert resp.status_code == 302
    assert jpg.read_bytes() == b"old-img"      # 旧图未被破坏
    assert outcomes == ["error", "stale_served"]


async def test_download_empty_url_serves_stale_when_cached(media_root):
    """messenger 上游瞬态无直链（轮询没缓存到）+ 本地有旧图 → 302 旧图而非 404。"""
    jpg, none_marker, url_path = _avatar_disk_paths("messenger", "a", "b")
    jpg.write_bytes(b"old-img")
    resp = await _download_and_cache_avatar(
        "", jpg, none_marker, url_path, neg_cache=False)
    assert resp.status_code == 302
    assert not none_marker.exists()


def test_avatar_route_wiring_pins_staleness_refresh():
    """接线静态钉（同 test_avatar_route_wiring_pins_timeout_and_breaker 哲学）：
    ① 缓存命中前必须比对「库内直链指纹 vs sidecar 指纹」（对方换头像 → 穿透回源）；
    ② 回源下载必须透传 src_fp（否则 sidecar 永不落盘，陈旧检测形同虚设）；
    ③ 302 目标必须带版本参数（`_avatar_versioned_path`）。"""
    src = inspect.getsource(uiar)
    route_src = src[src.index("async def api_platform_avatar"):]
    assert '"stale_refresh"' in route_src, "缓存命中分支缺指纹分歧穿透"
    assert "src_fp=_avatar_src_fp(remote_url)" in route_src, "回源缺 src_fp 透传"
    assert route_src.count("_avatar_versioned_path") >= 1, "302 缺版本参数"


# ── 2026-08-16 WA/TG 后台再验证（拉取式平台的「先答后核」）────────────────────


def test_avatar_should_revalidate_semantics(media_root, monkeypatch):
    """纯函数四态：无图不核 / 新图不核 / 老图未核过→核 / 老图刚核过→不核。"""
    from src.web.routes.unified_inbox_account_routes import (
        _avatar_reval_marker,
        _avatar_should_revalidate,
    )
    jpg, _, _ = _avatar_disk_paths("whatsapp", "a", "b")
    marker = _avatar_reval_marker(jpg)
    now = 1_000_000.0
    # 无图 → False
    assert not _avatar_should_revalidate(jpg, marker, now)
    # 新图（图龄 < TTL）→ False：下载本身就是最新事实
    jpg.write_bytes(b"img")
    os.utime(jpg, (now - 60, now - 60))
    assert not _avatar_should_revalidate(jpg, marker, now)
    # 老图 + 无 marker → True
    os.utime(jpg, (now - 7 * 3600, now - 7 * 3600))
    assert _avatar_should_revalidate(jpg, marker, now)
    # 老图 + 新 marker（刚核过）→ False
    marker.write_text("", encoding="utf-8")
    os.utime(marker, (now - 60, now - 60))
    assert not _avatar_should_revalidate(jpg, marker, now)
    # marker 也过期 → 再核
    os.utime(marker, (now - 7 * 3600, now - 7 * 3600))
    assert _avatar_should_revalidate(jpg, marker, now)


def test_avatar_reval_route_wiring_pins():
    """接线静态钉：① WA/TG 缓存命中分支必须挂后台再验证 spawn；② TG 下载成功须回写
    avatar_fp + sidecar；③ WA 下载成功须回写 avatar_fp；④ spawn 前必须先刷 marker
    （失败也占 TTL，防上游故障期反复回源）。"""
    src = inspect.getsource(uiar)
    route_src = src[src.index("async def api_platform_avatar"):]
    assert "_maybe_spawn_wa_avatar_reval(" in route_src, "WA 缓存命中缺再验证 spawn"
    tg_src = src[src.index("async def _telegram_peer_avatar"):]
    assert "_maybe_spawn_tg_avatar_reval(" in tg_src, "TG 缓存命中缺再验证 spawn"
    assert "_update_avatar_fp(" in tg_src, "TG 下载成功缺 avatar_fp 回写"
    assert '_update_avatar_fp(request.app, platform, account_id' in route_src, \
        "WA 下载成功缺 avatar_fp 回写"
    for fn in ("_maybe_spawn_tg_avatar_reval", "_maybe_spawn_wa_avatar_reval"):
        body = src[src.index(f"def {fn}"):]
        body = body[:body.index("\ndef ") if "\ndef " in body else len(body)]
        i_touch = body.index("_touch_avatar_reval_marker")
        i_task = body.index("create_task")
        assert i_touch < i_task, f"{fn}: marker 须在 spawn 前刷新"


def test_update_conversation_identity_accepts_avatar_fp(tmp_path):
    """store 写入口：avatar_fp 落库 + 空值不覆盖（与 avatar_url 同 no-clobber 语义）。"""
    from src.inbox.store import InboxConversation, InboxStore
    st = InboxStore(str(tmp_path / "inbox.db"))
    cid = "whatsapp:a:123"
    st.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="whatsapp", account_id="a",
        chat_key="123", display_name="Tom"))
    assert st.update_conversation_identity(cid, avatar_fp="fp111")
    row = st.get_conversation(cid)
    assert row["avatar_fp"] == "fp111"
    # 空串/None 不覆盖
    st.update_conversation_identity(cid, avatar_fp="")
    st.update_conversation_identity(cid, display_name="Tommy")
    assert st.get_conversation(cid)["avatar_fp"] == "fp111"


def test_store_row_to_chat_carries_avatar_fp():
    from src.inbox.normalizer import store_row_to_chat
    row = {"conversation_id": "whatsapp:a:123", "platform": "whatsapp",
           "account_id": "a", "chat_key": "123", "avatar_fp": "fp222"}
    chat = store_row_to_chat(row)
    assert chat["avatar_fp"] == "fp222"
    assert store_row_to_chat({**row, "avatar_fp": None})["avatar_fp"] == ""


def _install_fake_httpx(monkeypatch, content=b"img", raise_exc=None):
    """把 httpx.AsyncClient 换成不出网的假件（helper 内 ``import httpx`` 取的是同一模块对象）。"""
    import httpx

    class _Resp:
        def __init__(self):
            self.content = content

        def raise_for_status(self):
            if raise_exc:
                raise raise_exc

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if raise_exc:
                raise raise_exc
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
