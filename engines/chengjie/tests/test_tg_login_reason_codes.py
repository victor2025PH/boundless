"""Telegram 扫码登录失败归因（2026-08-10 API_ID_INVALID 事故回归网）。

事故形态：托管池派发的 api_id/api_hash 无效 → 用户「刷新二维码」死循环 +
「请重试或联系管理员」不可行动文案 + 客户端只记 DEBUG（beacon 收不到）→
靠用户拍照上报才被发现。三层钉住：

1. ``classify_login_exception`` 纯函数：事故原文必须归 ``cred_invalid``；
   网络/代理故障归 ``tg_unreachable``；限流归既有 ``rate_limited``；
   不明异常返回空串（宁可笼统不可误导）。
2. start() 失败必须：置 reason_code + ``result()`` 携带 + **ERROR 级**日志
   （公网桌面版 beacon 只回传 ERROR，DEBUG=远程完全失明）。
3. 归因码是跨层契约：产出值必须落在 ``login_funnel_stats._REASON_CODES``
   枚举内（越界会被 _san_reason 归并成 login_failed，归因白做）。
   前端映射 + zh/en 文案由既有门禁 test_frontend_maps_every_reason_code
   从枚举反推，本文件不重复。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.integrations import telegram_protocol_login as tpl
from src.integrations.login_funnel_stats import _REASON_CODES

# 用户截图里的逐字原文（pyrogram 对 400 API_ID_INVALID 的标准渲染）
_PRODUCTION_API_ID_INVALID = (
    'Telegram says: [400 API_ID_INVALID] - The api_id/api_hash combination '
    'is invalid (caused by "auth.ExportLoginToken")'
)


def _ensure_loop():
    """pyrogram 顶层 import 触发 sync 模块 get_event_loop()——主线程首个用例无 loop
    会 RuntimeError。任何 `import pyrogram` 前先确保有 loop（与 test_telegram_protocol_login 同款）。"""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


# 类名刻意与 pyrogram.errors 真类同名（分类逻辑按 type(ex).__name__ 判定）
class ApiIdInvalid(Exception):
    pass


class FloodWait(Exception):
    pass


class ProxyConnectionError(Exception):
    """模拟 python-socks 的代理故障（用户代理工具没开/挂了）。"""


@pytest.mark.parametrize("ex,expect", [
    (Exception(_PRODUCTION_API_ID_INVALID), "cred_invalid"),
    (ApiIdInvalid("api id invalid"), "cred_invalid"),
    # PUBLISHED_FLOOD 含 FLOOD 字样，必须先被凭据判定接走（顺序回归钉）
    (Exception("[400 API_ID_PUBLISHED_FLOOD] - This API id was published somewhere"),
     "cred_invalid"),
    (FloodWait("Telegram says: [420 FLOOD_WAIT_X] - A wait of 30 seconds is required"),
     "rate_limited"),
    (TimeoutError(), "tg_unreachable"),
    (ConnectionResetError(10054, "connection reset by peer"), "tg_unreachable"),
    (Exception("[Errno 11001] getaddrinfo failed"), "tg_unreachable"),
    (ProxyConnectionError("Couldn't connect to proxy 127.0.0.1:7890"), "tg_unreachable"),
    (ValueError("boom"), ""),
    (Exception(""), ""),
])
def test_classify_login_exception(ex, expect):
    assert tpl.classify_login_exception(ex) == expect


def test_classify_output_stays_inside_funnel_enum():
    """越界码会被漏斗 _san_reason 静默归并成 login_failed——归因就白做了。"""
    samples = [
        Exception(_PRODUCTION_API_ID_INVALID), ApiIdInvalid(), FloodWait("FLOOD_WAIT"),
        TimeoutError(), ConnectionResetError(), ProxyConnectionError("proxy down"),
        ValueError("boom"), Exception("random garbage"),
    ]
    for ex in samples:
        code = tpl.classify_login_exception(ex)
        assert code == "" or code in _REASON_CODES, f"越界 reason_code: {code!r}"


def test_new_codes_registered_in_funnel_enum():
    """枚举是三处同源契约的锚点（前端映射/i18n 由既有门禁从它反推）。"""
    assert "cred_invalid" in _REASON_CODES
    assert "tg_unreachable" in _REASON_CODES


def test_result_carries_reason_code_field():
    """路由 poll 只在非空时落 sess.reason_code——字段本身必须始终存在。"""
    login = tpl.TelegramQrLogin(1, "h", "sessions")
    assert "reason_code" in login.result()
    assert login.result()["reason_code"] == ""


class _BoomClient:
    """connect 即抛事故原文的 pyrogram Client 替身。"""

    def __init__(self, *a, **k):
        pass

    async def connect(self):
        raise Exception(_PRODUCTION_API_ID_INVALID)  # noqa: TRY002

    async def disconnect(self):
        pass


def test_start_failure_sets_reason_code_and_logs_error(tmp_path, monkeypatch, caplog):
    """事故端到端回归：start 失败 → reason_code=cred_invalid + ERROR 级日志。

    ERROR 级是硬要求：公网桌面版 telemetry beacon 只回传 ERROR+，此前 DEBUG
    意味着「我们的池子废了」这种事故远程完全失明（只能等用户拍照）。
    """
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    _ensure_loop()
    import pyrogram
    monkeypatch.setattr(pyrogram, "Client", _BoomClient)

    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    with caplog.at_level(logging.ERROR, logger=tpl.__name__):
        res = asyncio.run(login.start())

    assert res["status"] == "failed"
    assert res["reason_code"] == "cred_invalid"
    assert "API_ID_INVALID" in res["detail"]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "start 失败必须发 ERROR（beacon 只收 ERROR，DEBUG=远程失明）"
    assert any("cred_invalid" in r.getMessage() for r in errors), \
        "ERROR 日志须携带归因码（beacon 聚类判「同码多机」靠它）"


def test_start_network_failure_maps_to_tg_unreachable(tmp_path, monkeypatch):
    """大陆直连被墙形态：TCP 超时 → tg_unreachable（前端据此给配代理指引）。"""
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    _ensure_loop()
    import pyrogram

    class _TimeoutClient(_BoomClient):
        async def connect(self):
            raise TimeoutError()

    monkeypatch.setattr(pyrogram, "Client", _TimeoutClient)
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    res = asyncio.run(login.start())
    assert res["status"] == "failed"
    assert res["reason_code"] == "tg_unreachable"


# ── P1-⑤ 凭据无感换发：provider 层自愈重试 ─────────────────────────────────

def _seq_client_cls(bad_api_id: int):
    """api_id==bad 时 connect 即抛事故原文；其它 api_id 一路成功出 LoginToken。"""
    import time as _t

    from pyrogram.raw.types.auth import LoginToken

    class _SeqClient:
        def __init__(self, *a, api_id=None, **k):
            self.api_id = int(api_id or 0)

        async def connect(self):
            if self.api_id == bad_api_id:
                raise Exception(_PRODUCTION_API_ID_INVALID)  # noqa: TRY002

        async def invoke(self, *_a, **_k):
            return LoginToken(expires=int(_t.time()) + 30, token=b"tok")

        async def disconnect(self):
            pass

    return _SeqClient


def test_provider_self_heals_cred_invalid(tmp_path, monkeypatch):
    """废凭据 → 举报换发 → 同一轮重试出码，用户全程无感（事故的最终闭环）。"""
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    _ensure_loop()
    import pyrogram

    from src.ai import hosted_gateway as hg

    monkeypatch.setattr(pyrogram, "Client", _seq_client_cls(1001))
    swaps = []

    def fake_swap(cfg, bad):
        swaps.append(bad)
        cfg["telegram"]["api_id"] = "2002"
        cfg["telegram"]["api_hash"] = "b" * 32
        return "2002", "b" * 32

    monkeypatch.setattr(hg, "report_invalid_and_refetch", fake_swap)
    cfg = {"telegram": {"api_id": "1001", "api_hash": "a" * 32, "_hosted_cred": True}}
    provider = tpl.make_provider(cfg, str(tmp_path))

    async def run():
        info = await provider(None, "telegram", "protocol", "")
        assert swaps == ["1001"], "废组必须被举报一次（且只一次）"
        state = info["state"]
        assert state.status == "pending", f"换发后应重试出码，实际 {state.status}"
        assert str(info.get("qr_url") or "").startswith("tg://login?token=")

    asyncio.run(run())


def test_provider_swap_follows_new_group_proxy(tmp_path, monkeypatch):
    """P2-⑨：换发可能换到带出口的组 → 重试连接必须用新出口（_hosted_proxy）。"""
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    _ensure_loop()
    import pyrogram

    from src.ai import hosted_gateway as hg

    captured = {}

    def _client_cls(bad_api_id):
        import time as _t

        from pyrogram.raw.types.auth import LoginToken

        class _C:
            def __init__(self, *a, api_id=None, proxy=None, **k):
                self.api_id = int(api_id or 0)
                captured["proxy"] = proxy  # 记录**最后一次**构造时的出口

            async def connect(self):
                if self.api_id == bad_api_id:
                    raise Exception(_PRODUCTION_API_ID_INVALID)  # noqa: TRY002

            async def invoke(self, *_a, **_k):
                return LoginToken(expires=int(_t.time()) + 30, token=b"tok")

            async def disconnect(self):
                pass

        return _C

    monkeypatch.setattr(pyrogram, "Client", _client_cls(1001))

    def fake_swap(cfg, bad):
        cfg["telegram"]["api_id"] = "2002"
        cfg["telegram"]["api_hash"] = "b" * 32
        cfg["telegram"]["_hosted_proxy"] = {"scheme": "socks5", "host": "7.7.7.7",
                                            "port": 1080}
        return "2002", "b" * 32

    monkeypatch.setattr(hg, "report_invalid_and_refetch", fake_swap)
    cfg = {"telegram": {"api_id": "1001", "api_hash": "a" * 32, "_hosted_cred": True}}
    provider = tpl.make_provider(cfg, str(tmp_path))

    async def run():
        await provider(None, "telegram", "protocol", "")
        assert captured["proxy"] is not None, "换发到出口组后重试必须带新出口"
        assert captured["proxy"]["hostname"] == "7.7.7.7"

    asyncio.run(run())


def test_provider_keeps_failure_when_swap_unavailable(tmp_path, monkeypatch):
    """换发不可用（冷却/池死/用户自填）→ 保持原 cred_invalid 失败语义，不加戏。"""
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    _ensure_loop()
    import pyrogram

    from src.ai import hosted_gateway as hg

    monkeypatch.setattr(pyrogram, "Client", _seq_client_cls(1001))
    monkeypatch.setattr(hg, "report_invalid_and_refetch", lambda cfg, bad: None)
    cfg = {"telegram": {"api_id": "1001", "api_hash": "a" * 32, "_hosted_cred": True}}
    provider = tpl.make_provider(cfg, str(tmp_path))

    async def run():
        info = await provider(None, "telegram", "protocol", "")
        state = info["state"]
        assert state.status == "failed"
        assert state.reason_code == "cred_invalid"

    asyncio.run(run())
