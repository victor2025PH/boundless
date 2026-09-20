"""O-1 E（#255 TDNJHQ / 8FJDUK ①）：账号显示名 ≠ 人设名——比对纯函数 + 路由字段 + 人设页红条。

事故：账号昵称 Vanessa、人设 Mizuki，客户当场察觉不是同一个人。钉住：
  - names_consistent：相等 / 昵称含人设名 / 大小写·表情·空白·噱头词不敏感 → 一致；空 → None；
  - check_name_mismatch 给前端可渲染结构，can_push 只认 account_profile_push 能力表（TG/WA 直改，
    LINE/Messenger manual）；
  - [self-name] … mismatch=1 日志每组合一次；
  - /api/personas/status 每账号行带 self_name + name_mismatch，/api/accounts/{p}/{a}/profile 带 name_mismatch；
  - personas.html 红条 + 一键按人设改名（POST 既有 profile 写口，不新增路由）+ i18n zh/en 齐。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.utils.account_name_check import (
    can_push_name,
    check_name_mismatch,
    log_mismatch_once,
    names_consistent,
    normalize_display_name,
    reset_logged,
)

ROOT = Path(__file__).resolve().parents[1]


def test_normalize_display_name():
    assert normalize_display_name(" Mizuki 🌸 ") == "mizuki"
    assert normalize_display_name("Mizuki | 東京") == "mizuki東京"
    assert normalize_display_name("Vanessa_Official") == "vanessa"
    assert normalize_display_name("") == "" and normalize_display_name(None) == ""


def test_names_consistent_matrix():
    assert names_consistent("Mizuki", "Mizuki") is True
    assert names_consistent("mizuki 🌸", "Mizuki") is True          # 昵称含人设名
    assert names_consistent("Mizuki Tanaka", "Mizuki") is True
    assert names_consistent("Mizuki", "Mizuki Tanaka") is True      # 人设全名含账号名（≥3 字符）
    assert names_consistent("小雪", "小雪❄️") is True
    assert names_consistent("Vanessa", "Mizuki") is False           # 事故原样
    assert names_consistent("Li", "Lily") is False                  # 太短的包含不算
    assert names_consistent("", "Mizuki") is None                   # 判不出
    assert names_consistent("Vanessa", "") is None


def test_can_push_follows_profile_push_caps():
    assert can_push_name("telegram") is True
    assert can_push_name("whatsapp") is True
    assert can_push_name("line") is False
    assert can_push_name("messenger") is False
    assert can_push_name("wechat_kf") is False


def test_check_name_mismatch_structure_and_log_once(caplog):
    reset_logged()
    assert check_name_mismatch("telegram", "a1", "Mizuki", "Mizuki") is None
    assert check_name_mismatch("telegram", "a1", "", "Mizuki") is None
    m = check_name_mismatch("telegram", "a1", "Vanessa", "Mizuki", persona_id="p1")
    assert m == {"platform": "telegram", "account_id": "a1", "account_name": "Vanessa",
                 "persona_id": "p1", "persona_name": "Mizuki", "can_push": True}
    assert check_name_mismatch("line", "L1", "Vanessa", "Mizuki")["can_push"] is False
    with caplog.at_level(logging.INFO, logger="src.utils.account_name_check"):
        assert log_mismatch_once(m) is True
        assert log_mismatch_once(m) is False        # 同组合不再刷
        assert log_mismatch_once(None) is False
    lines = [r.getMessage() for r in caplog.records if "[self-name]" in r.getMessage()]
    assert len(lines) == 1
    assert "account=telegram:a1" in lines[0] and "persona=p1" in lines[0] and "mismatch=1" in lines[0]
    reset_logged()


def test_status_route_returns_name_mismatch_per_account():
    src = (ROOT / "src" / "web" / "routes" / "persona_routes.py").read_text(encoding="utf-8")
    seg = src[src.index("async def api_personas_status"):]
    assert "from src.utils.account_name_check import" in seg
    # TG 合并段 + _registry_rows（LINE / WA / Messenger）两处都带 self_name 与 name_mismatch
    assert seg.count('"self_name": _self_name') >= 2
    assert seg.count('"name_mismatch": _name_mismatch(') >= 2
    acct = (ROOT / "src" / "web" / "routes" / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    seg2 = acct[acct.index("async def api_account_profile_get"):]
    seg2 = seg2[:seg2.index("async def api_account_profile_push")]
    assert '"name_mismatch": name_mismatch' in seg2


def test_personas_page_red_bar_and_i18n():
    html = (ROOT / "src" / "web" / "templates" / "personas.html").read_text(encoding="utf-8")
    assert "function _renderNameMismatchBar" in html and "html += _renderNameMismatchBar(d);" in html
    assert "_pushPersonaName(" in html
    # 一键改名走既有 profile 写口（不新增路由），只传 name（不连头像）
    assert "/profile`" in html and "JSON.stringify({name: personaName})" in html
    assert "/api/personas/status" in html
    from src.web.i18n_packs.persona_studio import EN, ZH
    for k in ("psn_nm_title", "psn_nm_hint", "psn_nm_push", "psn_nm_pushing",
              "psn_nm_manual", "psn_nm_confirm", "psn_nm_done", "psn_nm_fail"):
        assert k in ZH and k in EN, k
        assert f"window.T('{k}')" in html, k
        assert not re.search(r"[\u4e00-\u9fff]", EN[k]), (k, EN[k])
    # 没新增路由：账号 / 人设路由清单不动（装配门禁）
    assert "@app.get(\"/api/personas/name-mismatch" not in html
