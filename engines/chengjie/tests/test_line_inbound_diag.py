"""B98（实施68 P1-15）：LINE 入站取证闭环门禁。

事故：LINE 通道能发不能收（skuio _483 vic001），而诊断包**不带 LINE 日志**，
远程无法定位。两半修复：
- 诊断包补 LINE 协议 transcript（``diag_upload.line_diag_files``）；
- LINE worker 暴露入站活性戳（接收线程起点 / 末条入站时刻 / 累计数），把
  「登录在、收不到」从无据可查变成可读数。
"""

from __future__ import annotations

from pathlib import Path


# ── 诊断包收集器 ─────────────────────────────────────────────────────────────

def test_line_diag_files_collects_transcripts(tmp_path, monkeypatch):
    from src.utils import diag_upload

    diag = tmp_path / "sessions" / "_diag"
    diag.mkdir(parents=True)
    (diag / "login_qrfail_20260826_010101.log").write_text("t1", encoding="utf-8")
    (diag / "login_notok_20260826_020202.log").write_text("t2", encoding="utf-8")
    (diag / "ignore.txt").write_text("x", encoding="utf-8")   # 非 transcript 不收

    import src.integrations.line_protocol_login as lpl
    monkeypatch.setattr(lpl, "sessions_dir",
                        lambda cfg: str(tmp_path / "sessions"))

    out = diag_upload.line_diag_files()
    keys = set(out.keys())
    assert "logs/line/login_qrfail_20260826_010101.log" in keys
    assert "logs/line/login_notok_20260826_020202.log" in keys
    assert not any("ignore.txt" in k for k in keys)


def test_line_diag_files_missing_dir_returns_empty(tmp_path, monkeypatch):
    from src.utils import diag_upload
    import src.integrations.line_protocol_login as lpl
    monkeypatch.setattr(lpl, "sessions_dir",
                        lambda cfg: str(tmp_path / "nope"))
    assert diag_upload.line_diag_files() == {}


def test_line_diag_files_never_raises(monkeypatch):
    """收集器绝不抛（诊断工具自己崩=最讽刺的事故）。"""
    from src.utils import diag_upload
    import src.integrations.line_protocol_login as lpl

    def _boom(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(lpl, "sessions_dir", _boom)
    assert diag_upload.line_diag_files() == {}


def test_diag_bundle_includes_line_arcname(tmp_path, monkeypatch):
    """端到端：LINE transcript 经 extra_files 进 zip，arcname 归 logs/line/。"""
    from src.utils.diagnostic_bundle import build_diagnostic_bundle
    import io
    import zipfile

    p = tmp_path / "login_qrfail.log"
    p.write_text("PIN issued at ...", encoding="utf-8")
    blob = build_diagnostic_bundle(
        config_dir=None, logs_dir=None,
        extra_files={"logs/line/login_qrfail.log": p})
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert "logs/line/login_qrfail.log" in zf.namelist()


# ── LINE worker 入站活性戳（不依赖 okline：只驱动纯 Python 逻辑）──────────────

def _mk_line_worker():
    from src.integrations.account_orchestrator import LineProtocolWorker
    w = LineProtocolWorker(
        {"account_id": "L1", "meta": {"tokens_path": "/nope"}}, {})
    return w


def test_line_worker_status_exposes_inbound_liveness():
    w = _mk_line_worker()
    st = w.status()
    # 初始：从未收到、接收线程未起——status 字段齐备（诊断/看门狗读它）
    assert st["type"] == "line_protocol"
    assert st["last_inbound_ts"] == 0.0
    assert st["inbound_count"] == 0
    assert st["recv_started_ts"] == 0.0
    assert st["thread_alive"] is False

    # 模拟收到一条入站 op（_on_msg 的活性戳分支等价）
    import time as _t
    w._last_inbound_ts = _t.time()
    w._inbound_count += 1
    st2 = w.status()
    assert st2["inbound_count"] == 1
    assert st2["last_inbound_ts"] > 0
