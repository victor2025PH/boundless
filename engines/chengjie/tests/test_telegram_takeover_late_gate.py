"""A 线「接管迟到闸」（2026-09-19 F1 全自动教学片实锤）。

事故：A 线的收件箱档位闸只在**生成前**过一次；生成 + 拟人思考延迟走十几秒，坐席这期间点
【接管】（takeover.start 已把档位切 manual、取消的只是 B 线 store 草稿）→ 在途回复照发
（12:50:56 接管，12:51:08 AI 仍发出「US$35.9 折扣」报价），「接管即停」不成立。

修法：``_aline_direct_send_allowed`` 复读档位（同一判定源），在 humanize 延迟后 / 分条间
再闸一次；弃发时回滚冷却记账 + 撤 dup-guard 登记（与 interject 弃稿同收尾）。本文件钉住：

1. 判定：manual/review → 不许直发；auto_ai → 许；镜像关 / store 未就绪 → None（fail-open）；
2. 封顶同样生效（auto_ai 被 caps 压成 review → 不许）；
3. 接线静态断言：post_humanize 之后、首条 _send_reply 之前有 _takeover_abort；分条循环里
   有 _aline_direct_send_allowed 复读。
"""
from __future__ import annotations

from pathlib import Path

from src.client.telegram_client import TelegramClient
from tests._source_block import source_block

_TC_PATH = Path(__file__).resolve().parents[1] / "src" / "client" / "telegram_client.py"


class _Cfg:
    config = {"inbox": {}}


class _Logger:
    def info(self, *a, **k):
        pass

    debug = info
    warning = info
    error = info


def _client(*, mirror: bool = True):
    c = TelegramClient.__new__(TelegramClient)
    c._mirror_inbox = mirror
    c.config = _Cfg()
    c._logger = _Logger()
    c.account_id = "6834964252"
    return c


def _patch_mode(monkeypatch, mode: str, *, store=object(), eff=None):
    import src.integrations.protocol_bridge as pb
    import src.inbox.automation_mode as am
    import src.inbox.effective_automation as ea
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    monkeypatch.setattr(am, "resolve_automation_mode", lambda _s, _cid, _cfg: mode)
    monkeypatch.setattr(ea, "compute_mode_caps", lambda **kw: [])
    monkeypatch.setattr(ea, "apply_mode_caps", lambda m, caps: ((eff or m), []))


def test_manual_after_takeover_blocks(monkeypatch):
    _patch_mode(monkeypatch, "manual")
    res = _client()._aline_direct_send_allowed(8244899900)
    assert res == (False, "manual", "manual")


def test_review_blocks_auto_allows(monkeypatch):
    _patch_mode(monkeypatch, "review")
    assert _client()._aline_direct_send_allowed(1)[0] is False
    _patch_mode(monkeypatch, "auto_ai")
    assert _client()._aline_direct_send_allowed(1) == (True, "auto_ai", "auto_ai")


def test_caps_apply_to_late_gate(monkeypatch):
    """全自动被平台/预热封顶压成 review：迟到闸与生成前闸同口径，也不许直发。"""
    _patch_mode(monkeypatch, "auto_ai", eff="review")
    assert _client()._aline_direct_send_allowed(1) == (False, "auto_ai", "review")


def test_fail_open_when_mirror_off_or_store_missing(monkeypatch):
    _patch_mode(monkeypatch, "manual")
    assert _client(mirror=False)._aline_direct_send_allowed(1) is None
    _patch_mode(monkeypatch, "manual", store=None)
    assert _client()._aline_direct_send_allowed(1) is None


def test_wiring_late_gate_between_humanize_and_send():
    src = _TC_PATH.read_text(encoding="utf-8")
    i_def = src.index("def _takeover_abort(")
    i_post_ij = src.index('_interject_stale_abort("post_humanize")')
    i_late = src.index('_takeover_abort("post_humanize")')
    i_send = src.index("await self._send_reply(message, chunk, parse_mode=_parse_mode)")
    assert i_def < i_post_ij < i_late < i_send, "迟到闸必须在 humanize 延迟之后、首条发送之前"
    # 弃发收尾：回滚冷却记账 + 撤 dup-guard（与 interject 弃稿一致）
    blk = source_block(_TC_PATH, "                def _takeover_abort(")
    assert "rollback_reply_accounting" in blk
    assert "unregister(_dg_reg_cid, _dg_reg_token)" in blk
    assert "_aline_direct_send_allowed" in blk


def test_wiring_bubble_loop_rechecks_mode():
    src = _TC_PATH.read_text(encoding="utf-8")
    i_loop = src.index("for i, chunk in enumerate(chunks):")
    i_send = src.index("await self._send_reply(message, chunk, parse_mode=_parse_mode)")
    seg = src[i_loop:i_send]
    assert "_aline_direct_send_allowed(chat_id)" in seg
    assert "_bubbles_interrupted = True" in seg
