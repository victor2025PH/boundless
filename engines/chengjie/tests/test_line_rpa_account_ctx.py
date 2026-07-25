# -*- coding: utf-8 -*-
"""P2-1 门禁：LINE RPA 每处 process_message 的 ctx 必须带 account_id。

runner 太重不宜实例化，用源码 ledger 锁不变量：``self._sm.process_message(``
出现次数 == ctx 里 ``"account_id": str(self._cfg_get("account_id"...`` 出现
次数。新增第四处调用而忘带账号维度 → 本测试点名。

零迁移语义：单号部署 account_id=default → ``make_context_key`` 返裸键，
存量上下文/记忆键一个字节不变；多号部署自动 ``acct:`` 前缀分桶。
"""

from pathlib import Path

_RUNNER = (Path(__file__).resolve().parents[1]
           / "src" / "integrations" / "line_rpa" / "runner.py")


def test_every_process_message_ctx_carries_account_id():
    src = _RUNNER.read_text(encoding="utf-8")
    n_calls = src.count("self._sm.process_message(")
    n_acct = src.count(
        '"account_id": str(self._cfg_get("account_id", "default")')
    assert n_calls == 3, (
        f"process_message 调用点数变了（{n_calls}），"
        "请为新调用点的 ctx 补 account_id 并更新本门禁")
    assert n_acct == n_calls, (
        f"ctx 带 account_id 的构建点 {n_acct} != 调用点 {n_calls}——"
        "有 ctx 漏了账号维度（双号部署会串上下文）")
