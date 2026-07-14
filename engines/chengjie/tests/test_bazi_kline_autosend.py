"""autosend「人生 K 线」门禁：多平台出图编排（run_autosend_kline）。

与 test_image_autosend 同风格：假 send_fn 记录投递，monkeypatch 出站落盘，零真实网络。
"""

from __future__ import annotations

import pytest

import src.inbox.image_autosend as ia
from src.companion.bazi_engine import BirthInfo, bazi_available
from src.companion.bazi_stats import get_bazi_stats

pytestmark = pytest.mark.skipif(
    not bazi_available(), reason="lunar_python 未安装")

_BIRTH = BirthInfo(1995, 3, 5, 8, 30, gender="female")


def _cfg(enabled=True, out_dir="", **kw):
    b = {"enabled": enabled, "kline": True}
    if out_dir:
        b["kline_out_dir"] = out_dir
    b.update(kw)
    return {"companion": {"bazi": b}}


def _recorder(ok=True):
    sent = []

    async def send_fn(mp, mu, mt, cap, inbox):
        sent.append({"path": mp, "url": mu, "type": mt, "cap": cap, "inbox": inbox})
        return ok
    return sent, send_fn


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    get_bazi_stats().reset()
    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media",
        lambda plat, acct, name, data: (f"/out/{name}", f"/static/{name}", "image"))
    yield
    get_bazi_stats().reset()


async def test_kline_sends_from_memory_birth(tmp_path):
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_kline(
        _cfg(out_dir=str(tmp_path)), "whatsapp", "acct1",
        "帮我画一下我的人生K线",
        send_fn=send_fn, resolve_birth=lambda: _BIRTH)
    assert ok is True
    assert len(sent) == 1
    assert sent[0]["type"] == "image"
    assert sent[0]["url"].startswith("/static/kline-")
    assert "仅供参考" in sent[0]["cap"]
    assert sent[0]["inbox"].startswith("[图片]")
    assert get_bazi_stats().dump()["kline"]["sent"] == 1


async def test_kline_same_turn_birth_in_text(tmp_path):
    """同轮报生辰 → 不依赖记忆解析回调。"""
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_kline(
        _cfg(out_dir=str(tmp_path)), "line", "acct2",
        "我1995年3月5日早上8点半出生的女生，画一下我的运势曲线",
        send_fn=send_fn, resolve_birth=lambda: None)
    assert ok is True and len(sent) == 1


async def test_kline_disabled_or_not_request_false(tmp_path):
    sent, send_fn = _recorder()
    assert await ia.run_autosend_kline(
        _cfg(enabled=False, out_dir=str(tmp_path)), "telegram", "a",
        "画一下我的人生K线", send_fn=send_fn, resolve_birth=lambda: _BIRTH) is False
    assert await ia.run_autosend_kline(
        _cfg(out_dir=str(tmp_path)), "telegram", "a",
        "今天天气不错", send_fn=send_fn, resolve_birth=lambda: _BIRTH) is False
    cfg_no_kline = _cfg(out_dir=str(tmp_path))
    cfg_no_kline["companion"]["bazi"]["kline"] = False
    assert await ia.run_autosend_kline(
        cfg_no_kline, "telegram", "a",
        "画一下我的人生K线", send_fn=send_fn, resolve_birth=lambda: _BIRTH) is False
    assert sent == []


async def test_kline_missing_birth_falls_back(tmp_path):
    """缺生辰 → False（回落正常草稿流，由注入路径顺势要生辰），不发半张图。"""
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_kline(
        _cfg(out_dir=str(tmp_path)), "messenger", "acct3",
        "帮我画一下我的人生K线",
        send_fn=send_fn, resolve_birth=lambda: None)
    assert ok is False and sent == []
    assert get_bazi_stats().dump()["kline"] == {"sent": 0, "failed": 0}


async def test_kline_deliver_fail_counts_failed(tmp_path):
    sent, send_fn = _recorder(ok=False)
    ok = await ia.run_autosend_kline(
        _cfg(out_dir=str(tmp_path)), "whatsapp", "acct4",
        "画一下我的人生K线",
        send_fn=send_fn, resolve_birth=lambda: _BIRTH)
    assert ok is False
    assert get_bazi_stats().dump()["kline"]["failed"] == 1


async def test_kline_resolve_birth_exception_soft(tmp_path):
    def _boom():
        raise RuntimeError("store down")
    sent, send_fn = _recorder()
    ok = await ia.run_autosend_kline(
        _cfg(out_dir=str(tmp_path)), "telegram", "acct5",
        "画一下我的人生K线", send_fn=send_fn, resolve_birth=_boom)
    assert ok is False and sent == []
