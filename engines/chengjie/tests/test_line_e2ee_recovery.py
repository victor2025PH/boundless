# -*- coding: utf-8 -*-
"""A2（2026-09-03，证据钧机 3U298U 20:46-20:47 窗）：LINE 签名桥重启后的 E2EE 自愈
+ 入站媒体 OBS 404「已过期」终局提示。

事故机制（两个已修件的交叉盲区，详见 src/integrations/line_e2ee_recovery.py 模块头）：
``kick_stuck_send`` 踢掉僵死的签名桥 Node 进程后，okline ``E2EEManager.my_keys`` 里
存的 wasm 句柄全成野值，而 ``is_ready()`` 只看 my_keys 非空 → 仍返回 True，于是
加封发送条条失败到 worker 重启，**没有任何一层会去重建密钥**。
"""
from __future__ import annotations

import json
import pathlib
import types

import pytest

from src.integrations import line_e2ee_recovery as R

REPO = pathlib.Path(__file__).resolve().parents[1]


# ── 错误识别 ─────────────────────────────────────────────────────────────────

def test_recognises_gateway_reason_from_钧机():
    """钧机实锤的服务端 reason 必须被认出来（大小写/包裹形态都要吃）。"""
    assert R.is_e2ee_key_error(RuntimeError("Item_e2ee_key_not_exists"))
    assert R.is_e2ee_key_error(Exception("send failed: ITEM_E2EE_KEY_NOT_EXISTS"))


def test_recognises_reason_and_metadata_attrs():
    """LineApiError 把细节放在 reason/metadata 上，裸 str(exc) 里可能没有。"""
    exc = types.SimpleNamespace()
    exc = Exception("sendMessage failed")
    exc.reason = "Item_e2ee_key_not_exists"          # type: ignore[attr-defined]
    assert R.is_e2ee_key_error(exc)
    exc2 = Exception("boom")
    exc2.metadata = {"reason": "item_e2ee_key_not_exists"}  # type: ignore[attr-defined]
    assert R.is_e2ee_key_error(exc2)


def test_recognises_okline_local_key_errors():
    """okline 自己在密钥缺失时抛的 RuntimeError 也该触发重建。"""
    assert R.is_e2ee_key_error(RuntimeError("no local E2EE key to decrypt with"))
    assert R.is_e2ee_key_error(
        RuntimeError("E2EE not initialised — log in with qr_login first"))
    assert R.is_e2ee_key_error(RuntimeError("could not negotiate E2EE key for u1"))


def test_does_not_recognise_letter_sealing_disabled():
    """对端真关了 Letter Sealing＝配置事实，重握手一万次也没用，不得进重试。"""
    assert not R.is_e2ee_key_error(RuntimeError("E2EE_RECEIVER_DISABLED"))
    assert not R.is_e2ee_key_error(RuntimeError("E2EE_SENDER_DISABLED"))
    assert not R.is_e2ee_key_error(RuntimeError("connection timed out"))
    assert not R.is_e2ee_key_error(None)
    assert not R.is_e2ee_key_error(Exception(""))


# ── session 读取 ─────────────────────────────────────────────────────────────

def _write_session(tmp_path, blob):
    p = tmp_path / "line.json"
    p.write_text(json.dumps({"accessToken": "t", "e2ee": blob}),
                 encoding="utf-8")
    return str(p)


def test_read_session_e2ee_happy(tmp_path):
    path = _write_session(tmp_path, {"mid": "u1", "latestKeyId": 3,
                                     "keys": {"3": "blob3"}})
    blob = R.read_session_e2ee(path)
    assert blob["latestKeyId"] == 3 and blob["keys"] == {"3": "blob3"}


def test_read_session_e2ee_empty_shapes(tmp_path):
    assert R.read_session_e2ee("") == {}
    assert R.read_session_e2ee(str(tmp_path / "nope.json")) == {}
    assert R.read_session_e2ee(_write_session(tmp_path, {})) == {}
    assert R.read_session_e2ee(_write_session(tmp_path, {"keys": {}})) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert R.read_session_e2ee(str(bad)) == {}, "坏文件不得抛"


# ── 重建 ─────────────────────────────────────────────────────────────────────

class _Mgr:
    def __init__(self, *, load_ok=True, raise_on_load=False):
        self.my_keys = {}
        self._peer_channels = {"u1": (11, 1, 2), "u2": (12, 1, 2)}
        self._group_keys = {("c1", 5): 99}
        self._load_ok = load_ok
        self._raise = raise_on_load
        self.loaded = None

    def load_from_export(self, blob):
        if self._raise:
            raise RuntimeError("wasm bridge gone")
        self.loaded = blob
        if self._load_ok:
            self.my_keys = {int(k): 500 + i
                            for i, k in enumerate(blob.get("keys") or {})}
        return self._load_ok


def _client(mgr):
    return types.SimpleNamespace(e2ee=mgr)


def test_rebuild_reloads_keys_and_drops_stale_channels(tmp_path):
    """核心不变量：信道缓存必须被丢（它们引用旧桥句柄），密钥必须重装。"""
    mgr = _Mgr()
    path = _write_session(tmp_path, {"latestKeyId": 7, "keys": {"7": "b7"}})
    res = R.rebuild_e2ee_from_session(_client(mgr), path, account_id="U1")
    assert res["ok"] is True
    assert res["keys"] == 1
    assert res["dropped"] == 3, "2 条 peer 信道 + 1 条群密钥都得丢"
    assert mgr._peer_channels == {} and mgr._group_keys == {}
    assert mgr.loaded["keys"] == {"7": "b7"}


def test_rebuild_without_session_keys_is_not_a_failure(tmp_path):
    """没扫码登录过 E2EE 的号：纯文本会话本就不需要 Letter Sealing，不该报故障。"""
    res = R.rebuild_e2ee_from_session(
        _client(_Mgr()), _write_session(tmp_path, {}), account_id="U1")
    assert res["ok"] is False and res["reason"] == "no_session_keys"


def test_rebuild_soft_fails_on_load_error(tmp_path):
    """自愈链自己抛异常会把「这条没发出去」放大成「worker 挂了」——绝不允许。"""
    path = _write_session(tmp_path, {"keys": {"1": "b"}})
    res = R.rebuild_e2ee_from_session(
        _client(_Mgr(raise_on_load=True)), path, account_id="U1")
    assert res["ok"] is False and res["reason"].startswith("exception:")
    res2 = R.rebuild_e2ee_from_session(
        _client(_Mgr(load_ok=False)), path, account_id="U1")
    assert res2["ok"] is False and res2["reason"] == "load_failed"


def test_rebuild_without_manager(tmp_path):
    res = R.rebuild_e2ee_from_session(
        types.SimpleNamespace(), _write_session(tmp_path, {"keys": {"1": "b"}}))
    assert res["ok"] is False and res["reason"] == "no_manager"


def test_drop_channels_tolerates_missing_attrs():
    assert R.drop_e2ee_channels(types.SimpleNamespace()) == 0
    assert R.drop_e2ee_channels(_client(types.SimpleNamespace())) == 0


# ── worker 接线（静态钉：改 LineProtocolWorker 前先读）────────────────────────

def test_wiring_kick_marks_dirty_and_sends_rebuild():
    src = (REPO / "src/integrations/account_orchestrator.py").read_text(
        encoding="utf-8")
    i_kick = src.index("def kick_stuck_send(")
    seg = src[i_kick:i_kick + 2000]
    assert "_e2ee_dirty = True" in seg, "踢桥必须打脏标，否则密钥永远不会重建"
    # 三条出站路径都要过自愈：文本/贴纸走 retry 包装，媒体至少要过懒重建
    assert "_send_with_e2ee_retry(\n                \"send_text\"" in src \
        or '_send_with_e2ee_retry(' in src
    i_media = src.index("async def _send_media_impl(\n        self, chat_key: str, *, media_path: str, media_type: str = \"\",")
    assert "_ensure_e2ee_ready()" in src[i_media:i_media + 2500], \
        "LINE 媒体占位消息也走 sendMessage，同样要过懒重建（3U298U 那笔就是 send_media）"


def test_wiring_retry_is_bounded_to_once():
    """无限重试会把节流打满（0827 零退避重连事故同一教训）。"""
    src = (REPO / "src/integrations/account_orchestrator.py").read_text(
        encoding="utf-8")
    i = src.index("async def _send_with_e2ee_retry(")
    seg = src[i:i + 1400]
    assert seg.count("await self._api_call(fn, *args)") == 2, \
        "恰好两次：首发 + 重握手后重试一次"
    assert "is_e2ee_key_error" in seg
    assert "raise" in seg, "非密钥类错误必须原样抛出，不得被自愈吞掉"


def test_wiring_lazy_rebuild_clears_flag_before_work():
    """先清标再重建：重建失败也不留死循环重试。"""
    src = (REPO / "src/integrations/account_orchestrator.py").read_text(
        encoding="utf-8")
    i = src.index("async def _ensure_e2ee_ready(")
    seg = src[i:i + 1200]
    i_clear = seg.index("self._e2ee_dirty = False")
    i_call = seg.index("_rebuild_e2ee_blocking")
    assert i_clear < i_call


# ── OBS 404 → 「已过期」终局提示 ──────────────────────────────────────────────

def test_expired_text_covers_kinds_and_never_empty():
    from src.integrations.line_media import expired_media_text
    for kind in ("image", "video", "voice", "audio", "document"):
        txt = expired_media_text(kind)
        assert "已过期" in txt and "重发" in txt, kind
    assert expired_media_text("").strip(), "未知大类也要有兜底文案"
    assert expired_media_text("weird").strip()


class _Obs404:
    def download_object(self, *a, **k):
        import requests
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("404 Client Error: Not Found", response=resp)


def test_obs_404_is_terminal_expired_and_not_retried(monkeypatch):
    """404＝对象已被 LINE 回收：分档成 expired，且**不再重试**（重试没有意义）。"""
    import src.integrations.line_media as LM

    calls = {"n": 0}
    real = _Obs404()

    class _Api:
        obs = types.SimpleNamespace()

    api = _Api()

    def _dl(*a, **k):
        calls["n"] += 1
        return real.download_object(*a, **k)

    api.obs.download_object = _dl
    monkeypatch.setattr(LM.time, "sleep", lambda *_a: None)
    data, detail, status = LM._obs_download_with_retry(api, "M404")
    assert data == b"" and status == 404
    assert calls["n"] == 1, "404 是终局，不该吃第二次 GET"


def test_download_reports_expired_via_out(monkeypatch):
    """出参把终局细因交给调用方（返回值形状保持二元组，存量门禁不动）。"""
    import src.integrations.line_media as LM

    monkeypatch.setattr(
        LM, "_obs_download_with_retry",
        lambda api, mid: (b"", "HTTPError: 404 Client Error: Not Found", 404))
    msg = {"id": "M1", "contentType": LM.CT_IMAGE}
    out: dict = {}
    kind, url = LM.download_line_media(object(), msg, "acct", out=out)
    assert (kind, url) == ("image", ""), "返回契约不变"
    assert out["expired"] is True
    assert out["reason"] == LM.MISS_EXPIRED
    assert out["http_status"] == 404


def test_transient_download_error_is_not_expired(monkeypatch):
    """瞬态失败绝不能被标成「已过期」——那会诱导坐席去让客户重发一个还在的文件。"""
    import src.integrations.line_media as LM

    monkeypatch.setattr(
        LM, "_obs_download_with_retry",
        lambda api, mid: (b"", "ConnectTimeout: ...", 0))
    out: dict = {}
    LM.download_line_media(
        object(), {"id": "M2", "contentType": LM.CT_IMAGE}, "a", out=out)
    assert out["expired"] is False and out["reason"] == "download_error"


def test_wiring_inbound_renders_expired_hint():
    src = (REPO / "src/integrations/account_orchestrator.py").read_text(
        encoding="utf-8")
    assert 'expired_media_text' in src, "入站链要把 expired 渲染成人话"
    i = src.index('_media_miss.get("expired")')
    seg = src[i:i + 900]
    assert "expired_media_text(media_type)" in seg
    # 已有文本（配文）时不许覆盖掉客户原话
    assert 'f"{text}\\n{expired_media_text(media_type)}"' in seg


@pytest.mark.parametrize("status", [401, 403, 500, 0])
def test_non_404_statuses_keep_old_retry_behaviour(monkeypatch, status):
    """只有 404 走终局；其余状态的重试/刷 token 行为一个字都不许变。"""
    import src.integrations.line_media as LM

    calls = {"n": 0}

    class _Api:
        obs = types.SimpleNamespace()

    api = _Api()

    def _dl(*a, **k):
        calls["n"] += 1
        import requests
        resp = requests.Response()
        resp.status_code = status
        raise requests.HTTPError(f"{status} err", response=resp)

    api.obs.download_object = _dl
    monkeypatch.setattr(LM, "_try_refresh_token", lambda _a: False)
    monkeypatch.setattr(LM.time, "sleep", lambda *_a: None)
    _, _, got = LM._obs_download_with_retry(api, "M")
    assert got == status
    # 401/403 刷 token 失败 → break（单次）；其余走两次尝试
    assert calls["n"] == (1 if status in (401, 403) else 2)
