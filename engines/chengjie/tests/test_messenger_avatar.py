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
    # account 混入 ../ 与分隔符、chat_key 混入 /.. → 文件名只留字母数字(account 另允 _-)
    jpg, none_marker, url_path = _avatar_disk_paths(
        "messenger", "../../etc/passwd", "9/8/7..6")
    assert ".." not in jpg.name
    assert "/" not in jpg.name and "\\" not in jpg.name
    # account 段保留字母数字 → "etcpasswd"；key 段仅数字 → "9876"
    assert jpg.name == "etcpasswd_9876.jpg"
    assert ".." not in url_path
    # 产物仍落在受控媒体根内（未逃逸）
    assert str(jpg.resolve()).startswith(str((media_root / "messenger").resolve()))


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
    assert resp.headers["location"] == url_path
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
