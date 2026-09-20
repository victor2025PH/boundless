# -*- coding: utf-8 -*-
"""快捷回复聚合层「准入 + 显示名」治理门禁（P0 2026-08-18）。

背景：templates.yaml 是双用途文件（坐席话术 × 机器人运行时模板），旧聚合把全量
键倾倒进坐席面板 → `greeting #2` / `error.general` 机器黑话直显 + `{order_number}`
占位符可被原样填入发出。本门禁钉住治理后的三条不变量：

① 准入：dict 型值 / test / gxp_* / 含 {var} 占位符的条目绝不进坐席面板；
② 显示名：tp_nm_<key> 词条优先（与后台「话术模板」页同叫法）→ 非 ASCII 人话键
   原样 → 正文首句兜底——**任何路径都不得把机器键名当标题漏出**；
③ 兼容：workspace/messenger 源行为不变；响应新增 key/category/variant 字段；
   单行 str 模板（/templates 页保存单行即 str）不再被静默丢弃。
"""

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.unified_inbox_context import _collect_quick_templates

_MACHINE_LABEL_RE = re.compile(r"^[a-z0-9_.]+( #\d+)?$")

_WORDS = {
    "tp_nm_greeting": "问候语",
    "tp_nm_farewell": "告别语",
    "tp_nm_order_query_with_number": "带单号查单",
    "tp_alt_n": " · 备选{n}",
}


def _tr_stub(key, default=None, /, **fmt):
    s = _WORDS.get(key)
    if s is None:
        s = default if default is not None else key
    if fmt:
        try:
            s = s.format(**fmt)
        except Exception:
            pass
    return s


class _Cfg:
    def __init__(self, cfg):
        self.config = cfg

    def get_dynamic_templates_config(self):
        return self.config.get("templates_dyn") or {}


def _labels(rows):
    return [r["label"] for r in rows]


# ── ① 准入 ─────────────────────────────────────────────────────────────


def test_system_templates_excluded():
    cfg = _Cfg({"templates_dyn": {
        "greeting": ["你好呀"],
        "test": ["✅ 系统运行正常"],
        "gxp_expired": "单号已过期，请重新发单号。",
        "error": {"general": "出错了"},
        "confirmation": {"true": "好的，已确认～"},
    }})
    rows = _collect_quick_templates(cfg, translate=_tr_stub)
    labels = _labels(rows)
    assert "问候语" in labels
    joined = "\0".join(labels)
    for bad in ("test", "gxp", "error", "confirmation", "出错了", "已确认"):
        assert bad not in joined, f"系统模板泄漏进坐席面板: {bad} in {labels}"


def test_placeholder_templates_included_with_names():
    """P1 起变量模板放行（面板填入前有变量补全表单接住）；系统 gxp_* 仍拒。"""
    cfg = _Cfg({"templates_dyn": {
        "order_query_with_number": ["已收到订单号 {order_number}，正在查询…"],
        "gxp_ask_intent": "收到单号 {order_no}。",
        "greeting": ["宝～我在这儿呢", "订单 {order_number} 已收到", "来啦来啦"],
    }})
    rows = _collect_quick_templates(cfg, translate=_tr_stub)
    by_key = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)
    # 变量模板照常进面板，显示名走 tp_nm_* 词条
    assert by_key["order_query_with_number"][0]["label"] == "带单号查单"
    assert "{order_number}" in by_key["order_query_with_number"][0]["text"]
    # gxp_* 系统流程模板仍然整键拒绝（与有没有变量无关）
    assert "gxp_ask_intent" not in by_key
    # 混合列表：三条全进，备选号按自然序
    g = by_key["greeting"]
    assert [r["variant"] for r in g] == [1, 2, 3]
    assert g[0]["label"] == "问候语"
    assert g[1]["label"] == "问候语 · 备选2"
    assert g[2]["label"] == "问候语 · 备选3"


# ── ② 显示名 ────────────────────────────────────────────────────────────


def test_display_label_prefers_tp_nm_words_and_category():
    cfg = _Cfg({"templates_dyn": {"greeting": ["你好呀"]}})
    rows = _collect_quick_templates(cfg, translate=_tr_stub)
    assert rows[0]["label"] == "问候语"
    assert rows[0]["category"] == "greet"
    assert rows[0]["key"] == "greeting"


def test_unknown_machine_key_falls_back_to_first_clause():
    cfg = _Cfg({"templates_dyn": {
        "my_new_thing": ["这条话术很长，后半句不该进标题"],
    }})
    rows = _collect_quick_templates(cfg, translate=_tr_stub)
    assert rows[0]["label"] == "这条话术很长"
    assert rows[0]["category"] == "custom"


def test_cjk_key_used_as_label_verbatim():
    cfg = _Cfg({"templates_dyn": {"晚安话术": ["早点休息哦～"]}})
    rows = _collect_quick_templates(cfg, translate=_tr_stub)
    assert rows[0]["label"] == "晚安话术"


def test_no_translate_still_never_leaks_machine_keys():
    cfg = _Cfg({"templates_dyn": {"greeting": ["你好呀，今天怎么样", "来啦来啦"]}})
    rows = _collect_quick_templates(cfg, translate=None)
    for r in rows:
        assert not _MACHINE_LABEL_RE.match(r["label"]), r
    assert rows[0]["label"] == "你好呀"
    assert rows[1]["label"].startswith("你好呀 #2")


# ── ③ 兼容与形态 ────────────────────────────────────────────────────────


def test_plain_str_template_included():
    """单行模板在 /templates 页保存为纯 str——旧实现静默丢弃（修复回归钉）。"""
    cfg = _Cfg({"templates_dyn": {"farewell": "再见啦，下次聊"}})
    rows = _collect_quick_templates(cfg, translate=_tr_stub)
    assert len(rows) == 1
    assert rows[0]["label"] == "告别语"
    assert rows[0]["text"] == "再见啦，下次聊"


def test_workspace_and_messenger_sources_unchanged():
    cfg = _Cfg({
        "workspace": {"quick_templates": [{"label": "问候", "text": "你好～"}]},
        "messenger_rpa": {"approval_templates": [{"label": "MS", "text": "msg 话术"}]},
    })
    rows = _collect_quick_templates(cfg, translate=_tr_stub)
    ws = [r for r in rows if r["source"] == "workspace"][0]
    assert ws["label"] == "问候" and ws["category"] == "custom" and ws["variant"] == 1
    assert any(r["source"] == "messenger" for r in rows)


def test_cap_60_preserved():
    cfg = _Cfg({"templates_dyn": {
        f"键{i}": [f"话术内容第{i}条"] for i in range(70)
    }})
    rows = _collect_quick_templates(cfg, translate=_tr_stub)
    assert len(rows) == 60


# ── 端点级（真 tr 词典）────────────────────────────────────────────────


def test_endpoint_labels_localized_and_no_machine_keys():
    from src.web.routes.unified_inbox_aux_read_routes import register_aux_read_routes

    app = FastAPI()
    cfg = _Cfg({"templates_dyn": {
        "greeting": ["你好呀", "来啦来啦"],
        "order_query": ["发我订单号，我帮你查。"],
        "error": {"general": "出错了"},
        "test": ["自检"],
        "gxp_ask_intent": "收到单号 {order_no}。",
    }})
    register_aux_read_routes(app, api_auth=lambda request: None, config_manager=cfg)
    c = TestClient(app)
    d = c.get("/api/unified-inbox/templates").json()
    assert d["ok"] is True
    labels = _labels(d["templates"])
    # 真词典（templates_page pack）：greeting → 问候语，order_query → 订单查询
    assert "问候语" in labels
    assert "订单查询" in labels
    assert any("备选2" in l for l in labels)
    for l in labels:
        assert not _MACHINE_LABEL_RE.match(l), f"机器键名泄漏: {l}"


def test_endpoint_can_edit_probe_fail_closed():
    """can_edit 只读探测：无会话角色且无 Bearer → False（fail-closed，绝不亮死按钮）；
    带 Bearer 头（token 链，api_auth 已验真伪）→ True，与 admin _api_write 同语义。"""
    from src.web.routes.unified_inbox_aux_read_routes import register_aux_read_routes

    app = FastAPI()
    cfg = _Cfg({"templates_dyn": {"greeting": ["你好呀"]}})
    register_aux_read_routes(app, api_auth=lambda request: None, config_manager=cfg)
    c = TestClient(app)
    d = c.get("/api/unified-inbox/templates").json()
    assert d["can_edit"] is False
    d2 = c.get("/api/unified-inbox/templates",
               headers={"Authorization": "Bearer x"}).json()
    assert d2["can_edit"] is True
