"""实施84 P0-6：reactivation 真发落 outreach_log 共享账本门禁。

此前 reactivation 只写 journey_events → 逃逸在每联系人打扰预算（care
contact_budget 读侧）与统一触达时间线之外。覆盖：真发成功才回调 sent_hook、
dry_run 不回调、send 失败不回调、hook 异常不影响发送结果。
"""
from types import SimpleNamespace

from src.contacts.reactivation_loop import ReactivationLoop
from src.skills.reactivation_scheduler import ReactivationCandidate


def _cand():
    return ReactivationCandidate(
        journey_id="j1", contact_id="c1", funnel_stage="warming",
        intimacy_score=55.0, silent_days=6.0, last_reactivation_ts=0)


class _Scheduler:
    def __init__(self, cands):
        self._cands = cands
        self.marked = []

    def list_candidates(self):
        return list(self._cands)

    def mark_sent(self, **kw):
        self.marked.append(kw)


class _Store:
    def get_contact(self, cid):
        return None

    def list_channel_identities_of(self, cid):
        return [SimpleNamespace(channel="telegram", external_id="777",
                                display_name="小李", account_id="acct1")]

    def get_journey_by_contact(self, cid):
        return SimpleNamespace(journey_id="j1", context_snapshot_json="")


class _AI:
    async def chat(self, prompt, **kw):
        return "上次你说想去东京，机票看得怎么样啦？"


def _loop(*, send_row_id=9, dry_run=False, hook=None, hook_calls=None):
    async def _send(channel, account_id, chat_name, reply, defer_until,
                    reason, staleness, extra):
        return send_row_id

    if hook is None and hook_calls is not None:
        def hook(info):
            hook_calls.append(dict(info))
    return ReactivationLoop(
        scheduler=_Scheduler([_cand()]), store=_Store(), ai_client=_AI(),
        send_callback=_send,
        episodic_provider=lambda j: "- 想去东京",
        min_silent_sec=0.0, dry_run=dry_run,
        first_run_grace_minutes=0.0,
        platform_priority=["telegram"],  # 假身份在 telegram（默认只认 messenger）
        sent_hook=hook)


async def test_sent_hook_called_on_success():
    calls = []
    loop = _loop(hook_calls=calls)
    assert await loop.run_once() == 1
    assert len(calls) == 1
    info = calls[0]
    assert info["channel"] == "telegram"
    assert info["account_id"] == "acct1"
    assert info["chat_name"] == "777"
    assert info["contact_id"] == "c1"
    assert float(info["silent_days"]) == 6.0


async def test_sent_hook_not_called_in_dry_run():
    calls = []
    loop = _loop(dry_run=True, hook_calls=calls)
    assert await loop.run_once() == 1  # dry 计数照旧
    assert calls == []


async def test_sent_hook_not_called_on_send_failure():
    calls = []
    loop = _loop(send_row_id=0, hook_calls=calls)
    assert await loop.run_once() == 0
    assert calls == []


async def test_sent_hook_exception_does_not_break_send():
    def _boom(info):
        raise RuntimeError("ledger down")

    loop = _loop(hook=_boom)
    assert await loop.run_once() == 1  # 发送结果不受 hook 异常影响
    assert loop._scheduler.marked  # journey 回执照写
