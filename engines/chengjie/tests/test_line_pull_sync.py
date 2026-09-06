# -*- coding: utf-8 -*-
"""LINE 拉取兜底门禁（impl85 阶段1，修 #44/#46「只能发收不到」）。

覆盖 line_pull_sync 的全部分支语义：首跑锚定不回灌 / 位点前进补拉且升序去重 /
位点持平零扫描 / 自消息跳过 / 群旗标 / 解密与解不开占位 / SSE 活着休眠 /
token 119 刷新自愈 / 10004 需重登冷却 / 状态落盘往返 / 位点回退重锚 /
新盒子（新客户）补拉 / 配置解析钳位 / 刷新钩子摘挂契约 / 编排器接线 ratchet。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.integrations.line_pull_sync import (  # noqa: E402
    LinePullSync,
    is_relogin_error,
    is_token_stale_error,
    msg_id_newer,
    refresh_client_token,
    resolve_line_pull_cfg,
)

SELF = "Uself000"
PEER = "Upeer111"
GROUP = "Cgroup222"


class FakeLineError(Exception):
    def __init__(self, msg: str, code: int = 0) -> None:
        super().__init__(msg)
        self.code = code


class FakeClient:
    def __init__(self) -> None:
        self.rev = 60
        self.boxes: list = []
        self.recent: dict = {}
        self.calls = Counter()
        self.rev_exc: Exception | None = None
        self.decrypt_ok = True

    def get_last_op_revision(self):
        self.calls["rev"] += 1
        if self.rev_exc is not None:
            raise self.rev_exc
        return self.rev

    def get_message_boxes(self, limit=50, last_messages_per_box=1):
        self.calls["boxes"] += 1
        return {"messageBoxes": list(self.boxes)}

    def get_recent_messages(self, box_id, n):
        self.calls["recent"] += 1
        return list(self.recent.get(box_id, []))[:n]

    def decrypt_message(self, m):
        self.calls["decrypt"] += 1
        if not self.decrypt_ok:
            raise RuntimeError("e2ee keys missing")
        out = dict(m)
        out["text"] = f"decrypted:{m.get('id')}"
        out.pop("chunks", None)
        return out

    def close(self):
        self.calls["close"] += 1


def _box(bid: str, last_id: str, frm: str = PEER) -> dict:
    return {"id": bid, "lastMessages": [{"id": last_id, "from": frm}]}


def _msg(mid: str, frm: str = PEER, text: str = "hi", **kw) -> dict:
    out = {"id": mid, "from": frm, "text": text}
    out.update(kw)
    return out


def _sync(tmp_path, client, **kw):
    emitted = []

    def emit(message, *, chat_key, is_group):
        emitted.append((message, chat_key, is_group))

    now = kw.pop("now", None) or (lambda: 1_000_000.0)
    s = LinePullSync(
        client_factory=lambda: client, self_mid=SELF,
        state_path=str(tmp_path / "state.pullsync.json"),
        emit=emit, cfg=resolve_line_pull_cfg(kw.pop("cfg", None)), now=now)
    return s, emitted


# ── 基础分支 ─────────────────────────────────────────────────────────────────

def test_first_tick_initializes_without_emitting(tmp_path):
    c = FakeClient()
    c.boxes = [_box(PEER, "100")]
    c.recent[PEER] = [_msg("100")]
    s, emitted = _sync(tmp_path, c)
    res = s.tick()
    assert res["status"] == "initialized"
    assert emitted == []          # 首跑绝不回灌历史
    assert s.stats()["boxes_tracked"] == 1


def test_new_peer_message_emitted_ascending_then_no_repeat(tmp_path):
    c = FakeClient()
    c.boxes = [_box(PEER, "100")]
    s, emitted = _sync(tmp_path, c)
    assert s.tick()["status"] == "initialized"

    c.rev = 61
    c.boxes = [_box(PEER, "102")]
    c.recent[PEER] = [_msg("102", text="second"), _msg("101", text="first"), _msg("100")]
    res = s.tick()
    assert res["status"] == "pulled" and res["pulled"] == 2
    assert [m[0]["id"] for m in emitted] == ["101", "102"]   # 升序、只取水位之上
    assert emitted[0][1] == PEER and emitted[0][2] is False

    # 位点不动 → 不再扫描；位点动但盒子没新消息 → 扫描零投递
    n_boxes = c.calls["boxes"]
    assert s.tick()["status"] == "unchanged"
    assert c.calls["boxes"] == n_boxes
    c.rev = 62
    res = s.tick()
    assert res["status"] == "scanned" and res.get("pulled") == 0
    assert len(emitted) == 2      # 绝不重复投递


def test_rev_unchanged_makes_single_cheap_rpc(tmp_path):
    c = FakeClient()
    c.boxes = [_box(PEER, "100")]
    s, _ = _sync(tmp_path, c)
    s.tick()
    before = (c.calls["boxes"], c.calls["recent"])
    for _ in range(5):
        assert s.tick()["status"] == "unchanged"
    assert (c.calls["boxes"], c.calls["recent"]) == before
    assert c.calls["rev"] == 6


def test_self_messages_skipped_and_group_flagged(tmp_path):
    c = FakeClient()
    c.boxes = [_box(GROUP, "10", frm=PEER)]
    s, emitted = _sync(tmp_path, c)
    s.tick()
    c.rev = 61
    c.boxes = [_box(GROUP, "13")]
    c.recent[GROUP] = [_msg("13", frm=SELF), _msg("12", frm=PEER), _msg("11", frm=SELF)]
    res = s.tick()
    assert res["pulled"] == 1
    assert emitted[0][0]["id"] == "12"
    assert emitted[0][1] == GROUP and emitted[0][2] is True
    # 自己的消息虽跳过，但水位必须推进到盒子末条（否则每轮重扫）
    assert s.tick()["status"] == "unchanged"


def test_sealed_message_decrypted_and_failure_still_emits(tmp_path):
    c = FakeClient()
    c.boxes = [_box(PEER, "100")]
    s, emitted = _sync(tmp_path, c)
    s.tick()

    c.rev = 61
    c.boxes = [_box(PEER, "101")]
    c.recent[PEER] = [_msg("101", text="", chunks=["x"])]
    s.tick()
    assert emitted[-1][0]["text"] == "decrypted:101"

    c.decrypt_ok = False
    c.rev = 62
    c.boxes = [_box(PEER, "102")]
    c.recent[PEER] = [_msg("102", text="", chunks=["x"])]
    res = s.tick()
    assert res["pulled"] == 1                       # 解不开也要投（占位由 worker 层兜）
    assert emitted[-1][0].get("chunks")


def test_sse_alive_keeps_fallback_dormant(tmp_path):
    c = FakeClient()
    s, _ = _sync(tmp_path, c, now=lambda: 1000.0)
    res = s.tick(sse_last_op_ts=980.0)              # 20s 前 SSE 真收到过 op
    assert res["status"] == "dormant"
    assert c.calls["rev"] == 0                      # 完全不打 RPC
    assert s.tick(sse_last_op_ts=0.0)["status"] == "initialized"


# ── token 自愈 ───────────────────────────────────────────────────────────────

def _refresh_result(ok: bool, relogin: bool = False, error: str = "") -> dict:
    return {"ok": ok, "source": "network", "exp": 0.0, "rotated": False,
            "relogin": relogin, "error": error}


def test_token_stale_refreshes_once_and_retries(tmp_path, monkeypatch):
    c = FakeClient()
    c.boxes = [_box(PEER, "100")]
    c.rev_exc = FakeLineError("Access token refresh required", code=119)
    s, _ = _sync(tmp_path, c)

    refreshed = []

    def fake_refresh(client):
        refreshed.append(client)
        client.rev_exc = None                       # 刷新后 RPC 恢复
        return _refresh_result(True)

    monkeypatch.setattr(
        "src.integrations.line_pull_sync.refresh_client_token_result", fake_refresh)
    res = s.tick()
    assert res["status"] == "initialized"
    assert len(refreshed) == 1

    # 冷却窗内再遇 119 不重复刷新
    c.rev_exc = FakeLineError("Access token refresh required", code=119)
    assert s.tick()["status"] == "token_stale"
    assert len(refreshed) == 1


def test_relogin_error_reports_cooldown_and_no_rpc_inside(tmp_path):
    c = FakeClient()
    c.rev_exc = FakeLineError("REQUEST_NEED_LOGIN", code=10004)
    s, _ = _sync(tmp_path, c)
    assert s.tick()["status"] == "relogin_required"
    n = c.calls["rev"]
    assert s.tick()["status"] == "relogin_required"   # 冷却期内
    assert c.calls["rev"] == n                        # 不再打 RPC
    assert s.stats()["relogin_required"] is True


def test_refresh_rejected_by_gateway_downgrades_to_relogin(tmp_path, monkeypatch):
    """只有网关明说 REQUEST_NEED_LOGIN（refresh token 真死）才判「需重新扫码」。"""
    c = FakeClient()
    c.rev_exc = FakeLineError("Access token refresh required", code=119)
    s, _ = _sync(tmp_path, c)
    monkeypatch.setattr(
        "src.integrations.line_pull_sync.refresh_client_token_result",
        lambda cl: _refresh_result(False, relogin=True, error="REQUEST_NEED_LOGIN code=10004"))
    assert s.tick()["status"] == "relogin_required"
    assert s.stats()["relogin_required"] is True


def test_refresh_transient_failure_does_not_mark_relogin(tmp_path, monkeypatch):
    """2026-09-05 事故：续期请求缺陷/网络失败曾被当成 refresh token 死亡 → 号被判死下线。
    非终局失败只冷却重试，绝不 relogin_required。"""
    c = FakeClient()
    c.rev_exc = FakeLineError("Access token refresh required", code=119)
    s, _ = _sync(tmp_path, c)
    monkeypatch.setattr(
        "src.integrations.line_pull_sync.refresh_client_token_result",
        lambda cl: _refresh_result(False, relogin=False, error="LineTransportError: timeout"))
    res = s.tick()
    assert res["status"] == "token_stale"
    assert "timeout" in res.get("error", "")
    assert s.stats()["relogin_required"] is False
    assert c.calls["close"] == 1                      # 丢 client，下轮从文件重建


def test_refresh_client_token_uses_x_line_access_header_and_unhooks():
    """续期请求必须带 X-Line-Access（require_auth=True）——okline 原版不带此头，网关
    一律回 REQUEST_NEED_LOGIN（2026-09-06 本机实测）；期间摘掉 401 钩子防递归，完了装回；
    回写 session 文件只改两个 token 字段（e2ee/certificate 原样）。"""
    seen = {}

    class FakeTokens:
        access_token = "old.access.token"
        refresh_token = "RT-OLD"

    class FakeTransport:
        def __init__(self):
            self._refresh_hook = "ORIG"
            self.tokens = FakeTokens()

        def post_json(self, path, body, **kw):
            seen["path"] = path
            seen["body"] = body
            seen["kw"] = kw
            seen["hook_during"] = self._refresh_hook
            return {"accessToken": "new.access.token", "refreshToken": "RT-NEW"}

    class FakeCli:
        def __init__(self):
            self.transport = FakeTransport()

    cli = FakeCli()
    assert refresh_client_token(cli) is True
    assert seen["path"] == "/api/auth/tokenRefresh"
    assert seen["body"] == {"refreshToken": "RT-OLD"}
    assert seen["kw"].get("require_auth") is True
    assert seen["kw"].get("allow_refresh") is False
    assert seen["hook_during"] is None
    assert cli.transport._refresh_hook == "ORIG"
    assert cli.transport.tokens.access_token == "new.access.token"
    assert cli.transport.tokens.refresh_token == "RT-NEW"


# ── 状态持久化 / 位点回退 / 新盒子 ────────────────────────────────────────────

def test_state_roundtrip_and_corrupt_file_reinit(tmp_path):
    c = FakeClient()
    c.boxes = [_box(PEER, "100")]
    s, _ = _sync(tmp_path, c)
    s.tick()
    # 新实例从盘上恢复水位：位点没动 → unchanged（不回灌）
    s2, emitted2 = _sync(tmp_path, c)
    assert s2.tick()["status"] == "unchanged"
    assert emitted2 == []
    # 坏文件 → 按未初始化重来（重新锚定，不炸）
    (tmp_path / "state.pullsync.json").write_text("{corrupt", encoding="utf-8")
    s3, emitted3 = _sync(tmp_path, c)
    assert s3.tick()["status"] == "initialized"
    assert emitted3 == []


def test_rev_regression_reanchors_without_emit(tmp_path):
    c = FakeClient()
    c.boxes = [_box(PEER, "100")]
    s, emitted = _sync(tmp_path, c)
    s.tick()
    c.rev = 3                                        # 服务端位点回退（重置）
    c.boxes = [_box(PEER, "200")]
    c.recent[PEER] = [_msg("200")]
    assert s.tick()["status"] == "reanchored"
    assert emitted == []
    c.rev = 4
    c.boxes = [_box(PEER, "201")]
    c.recent[PEER] = [_msg("201"), _msg("200")]
    assert s.tick()["pulled"] == 1                   # 重锚后只取新水位之上


def test_new_box_after_init_is_pulled(tmp_path):
    """初始化之后出现的新盒子＝新客户首次开口，必须补拉（上限 fetch_per_chat）。"""
    c = FakeClient()
    c.boxes = [_box(PEER, "100")]
    s, emitted = _sync(tmp_path, c)
    s.tick()
    c.rev = 61
    new_peer = "Unewcomer9"
    c.boxes = [_box(PEER, "100"), _box(new_peer, "501")]
    c.recent[new_peer] = [_msg("501", frm=new_peer, text="hello?")]
    res = s.tick()
    assert res["pulled"] == 1
    assert emitted[0][1] == new_peer


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def test_msg_id_newer_numeric_and_fallback():
    assert msg_id_newer("101", "100")
    assert not msg_id_newer("100", "100")
    assert msg_id_newer("629362168324161928", "629357847318889010")
    assert msg_id_newer("abc", "")                   # 无水位＝新
    assert not msg_id_newer("", "100")


def test_error_classifiers():
    assert is_token_stale_error(FakeLineError("x", code=119))
    assert is_token_stale_error(FakeLineError("Access token refresh required code=119"))
    assert is_relogin_error(FakeLineError("x", code=10004))
    assert is_relogin_error(FakeLineError("REQUEST_NEED_LOGIN code=10004"))
    assert not is_token_stale_error(FakeLineError("boom", code=500))
    assert not is_relogin_error(FakeLineError("boom", code=500))


def test_resolve_cfg_defaults_and_clamps():
    d = resolve_line_pull_cfg(None)
    assert d["enabled"] is True                      # 缺陷修复默认开（见模块 docstring）
    assert d["interval_sec"] == 20.0
    cfg = {"platform_login": {"line": {"pull_sync": {
        "enabled": False, "interval_sec": 1, "boxes_limit": 9999,
        "fetch_per_chat": 0, "sse_quiet_sec": 5}}}}
    v = resolve_line_pull_cfg(cfg)
    assert v["enabled"] is False
    assert v["interval_sec"] == 5.0                  # 钳位下限
    assert v["boxes_limit"] == 200
    assert v["fetch_per_chat"] == 1
    assert v["sse_quiet_sec"] == 10.0


# ── 编排器接线 ratchet（静态：不依赖 okline 安装） ────────────────────────────

def test_orchestrator_wiring_ratchet():
    src = (REPO / "src" / "integrations" / "account_orchestrator.py").read_text(
        encoding="utf-8")
    # 1) start() 必须拉起兜底线程
    assert "self._start_pull_sync(path)" in src
    # 2) SSE 与拉取共用同一投递口（两个 via 都必须存在）
    assert 'via="sse"' in src and 'via="pull"' in src
    # 3) 兜底休眠判据必须用 SSE 专属时间戳（不能用任意入站戳，否则自我休眠）
    assert "tick(sse_last_op_ts=self._sse_inbound_ts)" in src
    # 4) 凭据过期必须接告警链（客户点名的「系统没提醒」缺失面）
    assert "report_session_transition" in src and '"expired"' in src
    # 5) status 暴露拉取观测（值守远程判「兜底是否在干活」的读数面）
    assert '"pull_sync"' in src
