# -*- coding: utf-8 -*-
"""实施74 阶段4 门禁（实施69 P1-5）：relogin 三态确定性分诊——钉住等价实现。

实施69 P1-5 原案＝「点击后先 GET /accounts 分诊三态」。实际由两批工作以
**等价方式**覆盖（本文件把等价性钉死防回退）：

- worker 宕机  → POST 转发失败无 response → 502 `err.psess.relogin_worker_down`
  （「服务未运行，联系运维」语义）；
- 无此账号    → worker 回 404 → 404 `err.psess.relogin_no_profile`
  （「需完整登录/换机了」语义，chandown_lifecycle ③ 落的）；
- 会话过期    → relogin 成功开窗 → 前端驻留 `inbox.acct.relogin_waiting` +
  `inbox.acct.relogin_started`（实施69 P0-2 落的）。

三态各有独立人话文案键＝「502 猜谜」已消灭；比前置探活少一次往返。
若有人把三态合并回一句话/删键，本文件先红。
"""
from __future__ import annotations

from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def test_backend_triage_distinct_states():
    src = (_ENGINE_ROOT / "src" / "web" / "routes" / "ops_overview_routes.py"
           ).read_text(encoding="utf-8")
    assert "err.psess.relogin_no_profile" in src   # worker 404 → 专属文案
    assert "err.psess.relogin_worker_down" in src  # 连不上 → 专属文案
    # 两态判据必须分开（404 vs 无 response），不得合并成一句 op_failed
    assert "_resp_code == 404" in src
    assert "_resp_code is None" in src


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.errors_stock import EN as ERR_EN, ZH as ERR_ZH
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    for k in ("err.psess.relogin_no_profile", "err.psess.relogin_worker_down"):
        assert k in ERR_ZH and k in ERR_EN, k
    for k in ("inbox.acct.relogin_started", "inbox.acct.relogin_waiting"):
        assert k in ZH and k in EN, k


def test_frontend_success_state_persistent():
    """成功态驻留提示（实施69 P0-2）：点了必须有可见反馈，不再「无响应」。"""
    src = (_ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
           ).read_text(encoding="utf-8")
    assert "inbox.acct.relogin_waiting" in src
    assert "inbox.acct.relogin_started" in src
