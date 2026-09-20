# -*- coding: utf-8 -*-
"""P-5 C（#259，2026-09-08）：/api/unified-inbox/send 失败分支 502 必带原因、留痕行 fail_reason 必非空。

09-08 13:47–14:09 实锤：后端重启窗口里 A 线 TelegramCompanionWorker 只回 ``{"delivered": False,
"message_id": ""}``，路由把空 error 原样塞进「消息未送达：{msg}」→ 值守回执链读到 ``HTTP 502
{"detail": "消息未送达："}`` 十二次，收件箱十二条 failed 留痕行 fail_reason 全空。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.inbox.send_failure_class import (
    ADAPTER_WARMUP_SEC, FAILURE_CLASS_I18N, classify_send_failure, undelivered_reason)

ROUTE = Path(__file__).resolve().parents[1] / "src" / "web" / "routes" / "unified_inbox_send_routes.py"


@pytest.mark.parametrize("text,reason,expect", [
    ("adapter_not_ready: 适配器未就绪（后端启动中）telegram:6834964252 worker=starting uptime=41s", "adapter_not_ready", "adapter_not_ready"),
    ("RuntimeError: telegram client 未连接", "", "adapter_not_ready"),
    ("无可用的运行中 worker: telegram:6834964252", "", "adapter_not_ready"),
    ("adapter_no_reason: 适配器回 delivered=false 未报原因 telegram:x worker=running uptime=9000s", "adapter_no_reason", "channel"),
    ("send_timeout", "send_timeout", "channel"),
    # 既有五类不受影响
    ("Client error '429 Too Many Requests'", "send_backoff", "rate_limited"),
    ("messenger session unhealthy (needs manual re-login)", "", "session"),
    ("weird unknown platform hiccup", "", ""),
])
def test_classify_adds_adapter_not_ready_and_no_reason(text, reason, expect):
    assert classify_send_failure(text, reason) == expect


def test_adapter_not_ready_i18n_bilingual():
    from src.web.i18n_packs.errors import EN, ZH
    key = FAILURE_CLASS_I18N["adapter_not_ready"]
    assert key == "err.inbox.sendfail.adapter_not_ready"
    assert "未就绪" in ZH[key] and "启动中" in ZH[key] and "没有发出去" in ZH[key]
    assert "not ready" in EN[key].lower() and "not sent" in EN[key].lower()


def test_undelivered_reason_never_empty():
    # ① 带原因 → 原样
    assert undelivered_reason({"delivered": False, "error": "messenger send failed: 502", "error_kind": "render_timeout"}) \
        == ("messenger send failed: 502", "render_timeout")
    assert undelivered_reason({"delivered": False, "error_kind": "send_timeout"}) == ("send_timeout", "send_timeout")
    # ② 空原因 + 启动 180s 内 → adapter_not_ready
    raw, kind = undelivered_reason({"delivered": False, "message_id": ""}, "telegram", "6834964252",
                                   uptime_sec=41, worker_state="running")
    assert kind == "adapter_not_ready" and raw.startswith("adapter_not_ready:") and "telegram:6834964252" in raw and "uptime=41s" in raw
    assert classify_send_failure(raw, kind) == "adapter_not_ready"
    # ② 空原因 + worker 不在 running → adapter_not_ready（不看 uptime）
    raw2, kind2 = undelivered_reason({"delivered": False}, "telegram", "6834964252", uptime_sec=9000, worker_state="starting")
    assert kind2 == "adapter_not_ready" and "worker=starting" in raw2
    # ③ 空原因 + 早就起来 + worker running → adapter_no_reason（归通道异常类，原始码留括注）
    raw3, kind3 = undelivered_reason({"delivered": False}, "telegram", "6834964252", uptime_sec=ADAPTER_WARMUP_SEC + 1, worker_state="running")
    assert kind3 == "adapter_no_reason" and classify_send_failure(raw3, kind3) == "channel"
    # 自探 uptime / worker（-1 / ""；测试进程 metrics_store 刚起 → 多半判未就绪）→ 也不许空
    raw4, kind4 = undelivered_reason(None, "", "", uptime_sec=-1, worker_state="")
    assert raw4 and kind4 in ("adapter_not_ready", "adapter_no_reason")


def test_route_humanizes_adapter_not_ready_and_no_reason():
    from src.web.routes.unified_inbox_send_routes import _humanize_send_failure, _undelivered_reason

    class _R:
        class state:  # noqa: N801
            ui_lang = "zh"

    raw, kind = undelivered_reason({"delivered": False}, "telegram", "6834964252", uptime_sec=30, worker_state="running")
    human = _humanize_send_failure(_R(), raw, kind)
    assert "适配器未就绪" in human and "adapter_not_ready" in human
    raw3, kind3 = undelivered_reason({"delivered": False}, "telegram", "6834964252", uptime_sec=9000, worker_state="running")
    human3 = _humanize_send_failure(_R(), raw3, kind3)
    assert "发送通道异常" in human3 and "adapter_no_reason" in human3
    # 路由包装：探测异常也不许空
    got = _undelivered_reason({"delivered": False}, "telegram", "6834964252")
    assert got[0] and got[1] in ("adapter_not_ready", "adapter_no_reason")
    assert _undelivered_reason({"delivered": False, "error": "x"}, "telegram", "a") == ("x", "")


def test_route_failure_branches_use_reason_and_log():
    """文本 / 媒体两条 delivered=False 分支：留痕 reason 与 502 detail 都走 _undelivered_reason，
    并打 ``[send] fail conv=… reason=…`` 日志；不再有把 result.error 原样塞 detail 的老写法。"""
    src = ROUTE.read_text(encoding="utf-8")
    assert src.count("_undelivered_reason(result, platform, account_id)") == 1
    assert src.count("_undelivered_reason(res, platform, account_id)") == 1
    assert 'fail conv=%s reason=%s kind=%s' in src
    assert src.count("_log_send_fail(platform, account_id, chat_key, _fail_raw, _fail_kind") == 2
    assert 'msg=str(res.get("error") or res.get("error_kind") or "")' not in src
    # 文本分支：留痕 reason = kind 优先、原文兜底，紧接着 502 用同一对 (_fail_raw, _fail_kind) 人话化
    txt = src.split("_undelivered_reason(result, platform, account_id)", 1)[1][:700]
    assert "_trace_failed_manual_send(" in txt and "_fail_kind or _fail_raw" in txt
    assert re.search(r"_humanize_send_failure\(\s*request, _fail_raw, _fail_kind\)", txt)
