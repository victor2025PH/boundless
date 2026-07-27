"""跨层门禁：后端每个诊断原因码都必须配齐前端文案（zh + en）。

接入弹窗的说明卡完全由后端 `platform_readiness` 出的 ``code`` 驱动：
前端拿 ``rc_<code>_why`` / ``rc_<code>_how`` 渲染「为什么 / 怎么办」，
拿 ``bk_<code>`` 渲染方式卡上的短标签。

这条链的脆弱点是**加码不加文案**：后端新增一个 blocker，前端按既有模板拼键，
键不存在 → `window.T` 回落显示裸键名（本仓约定前端不留中文兜底）。用户看到的是
`inbox.connect.rc_xxx_why` 这种东西，而这在任何单测里都不会红。

所以在这里把「码集合」与「文案集合」对齐钉死：加码必须同时加三个键、且中英齐备。
"""
from __future__ import annotations

import pytest

from src.integrations import platform_readiness as pr
from src.web.web_i18n import get_translations

# 与前端 _CONN_REASON_CODES / _CONN_BK_CHIP 对应的后端码全集
_CODE_CONSTS = [
    "BLOCK_LOGIN_DISABLED", "BLOCK_NOT_ENABLED", "BLOCK_DEP_MISSING",
    "BLOCK_CREDS_MISSING", "BLOCK_SERVICE_DOWN", "BLOCK_NEEDS_SERVER_SETUP",
    "BLOCK_PROVIDER_UNAVAILABLE", "WARN_ORCHESTRATOR_OFF",
]
_TPL_PATH = "src/web/templates/unified_inbox.html"


def _codes() -> list:
    return [getattr(pr, name) for name in _CODE_CONSTS]


def test_code_constants_all_exist():
    """常量改名会让下面几条静默失效，先钉住名字本身。"""
    missing = [n for n in _CODE_CONSTS if not hasattr(pr, n)]
    assert not missing, f"platform_readiness 缺常量: {missing}"


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_every_code_has_copy(lang):
    t = get_translations(lang)
    missing = []
    for code in _codes():
        for key in (f"inbox.connect.rc_{code}_why",
                    f"inbox.connect.rc_{code}_how",
                    f"inbox.connect.bk_{code}"):
            if not str(t.get(key) or "").strip():
                missing.append(key)
    assert not missing, f"[{lang}] 诊断原因码缺文案: {missing}"


def test_frontend_knows_every_backend_code():
    """前端的码白名单与 chip 映射表必须覆盖后端全集。

    前端不认识的码会被 `_modeReasonKeys` 回落成 not_enabled——又变回「一律说尚未
    启用」的老毛病，而且不报错。
    """
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / _TPL_PATH).read_text(encoding="utf-8")
    missing_wl, missing_chip = [], []
    for code in _codes():
        if f"{code}:1" not in tpl.replace(" ", ""):
            missing_wl.append(code)
        if f"{code}:'inbox.connect.bk_{code}'" not in tpl.replace(" ", ""):
            missing_chip.append(code)
    assert not missing_wl, f"unified_inbox.html 的 _CONN_REASON_CODES 缺: {missing_wl}"
    assert not missing_chip, f"unified_inbox.html 的 _CONN_BK_CHIP 缺: {missing_chip}"


def test_params_placeholders_are_declared_in_copy():
    """带参数的码，其 how/why 文案里应当真的用上这些参数，否则参数白传。

    只校验后端确定会给的那几个：dep_missing→{dep}/{install}，
    creds_missing→{field}，service_down→{svc}/{url}。
    """
    expect = {
        pr.BLOCK_DEP_MISSING: ["{dep}", "{install}"],
        pr.BLOCK_CREDS_MISSING: ["{field}"],
        pr.BLOCK_SERVICE_DOWN: ["{svc}", "{url}"],
    }
    for lang in ("zh", "en"):
        t = get_translations(lang)
        for code, phs in expect.items():
            blob = str(t.get(f"inbox.connect.rc_{code}_why") or "") + \
                str(t.get(f"inbox.connect.rc_{code}_how") or "")
            for ph in phs:
                assert ph in blob, f"[{lang}] rc_{code}_* 未使用占位符 {ph}"
