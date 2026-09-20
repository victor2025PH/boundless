# -*- coding: utf-8 -*-
"""审计动作 → 人话词条 契约门禁（2026-08-05；2026-08-18 AST 化扩容）。

背景：aud_act_* 词典（i18n_packs/audit_actions.py）靠"新增动作时顺手补双语"
的自觉维护——2026-08 初新增的 episodic_*/identity_* 7 个动作全部漏登记，
首页「最近操作」与 /audit 直接把英文枚举漏给运营。本门禁把自觉变成契约：

① 正则扫 ``audit*.log(actor, "action", ...)`` 直调字面量（原版能力）；
② **AST 识别「转发 action 形参进 *.log()」的审计包装函数**——任意函数名/
   任意签名（``_audit(request, action)`` / ``_safe_audit(store, user, action,
   target)`` / 方法 ``self._audit_call(action, ...)`` 都认），再提取其**调用点**
   的字面 action。每个动作都必须在 pack 的 ZH 与 EN 两侧有 aud_act_<action>。

2026-08-18 事故沉淀（AST 化的由来）：旧版只有 ① + `_audit_identity` 特例
正则，sticker/voice/pmedia/group_members 的局部 ``_audit(request, "stk_*")``
与 contacts 的 ``_safe_audit(store, user, "merge_*", ...)`` 全在盲区——39 个
动作以裸英文枚举直漏到首页「最近操作」卡（老板截图实锤 stk_seed_official）。
AST 按「.log() 的 action 槽位收的是本函数形参」这一**语义**识别包装，与
函数名、形参顺序解耦：新增包装函数零登记自动进扫描。

刻意不做反向检查（pack 有键但扫不到调用点 ≠ 死键）：动态拼接动作
（f-string）提取不到，反向断言会误报；动态动作走 _KNOWN_DYNAMIC_ACTIONS
人工台账 + test_dynamic_wrapper_sites_registered 兜底（包装的 f-string 调用
点必须能对上台账前缀，防「新动态动作静默漏网」）。
"""

import ast

# 扫描机器 2026-08-18 抽到 tests/_audit_scan.py 共享（test_audit_display 的
# aud_tgt_* kv 键门禁复用同一套包装识别）；本文件只留门禁与自证。
from tests._audit_scan import (  # noqa: F401  (unit 自证直接调用)
    SRC,
    _LOG_CALL,
    _collect_literal_actions,
    _collect_wrapper_actions,
    _log_action_forward,
    _trees,
    _wrapper_actions_from,
    _wrapper_spec,
)


# ── 门禁本体 ────────────────────────────────────────────────────────

def test_every_literal_audit_action_has_bilingual_label():
    from src.web.i18n_packs import audit_actions

    found = _collect_literal_actions()
    # 提取器自证：全库现有 100+ 个字面动作，抽不到说明提取链坏了（门禁失效比红更糟）
    assert len(found) >= 100, f"扫描仅得 {len(found)} 个动作，提取链疑似失效"

    missing = {}
    for action, files in sorted(found.items()):
        key = f"aud_act_{action}"
        lack = [side for side, d in (("ZH", audit_actions.ZH), ("EN", audit_actions.EN))
                if key not in d]
        if lack:
            missing[action] = {"lack": lack, "files": sorted(files)}
    assert not missing, (
        "以下审计动作缺人话词条（i18n_packs/audit_actions.py 补 aud_act_<action>，"
        f"zh+en 双语）：{missing}"
    )


def test_wrapper_extractor_detects_all_known_shapes():
    """AST 提取器自证：三种真实包装形态（局部函数带 request / 形参偏移的
    _safe_audit / 类方法 self._audit_call）各钉一个生产哨兵动作。任何一个
    消失＝提取器回归或包装被重构，先来这里对账再改。"""
    lits, _ = _collect_wrapper_actions()
    sentinels = {
        "stk_seed_official": "sticker_routes._audit(request, action, ...)",
        "merge_review_approve": "contacts_routes._safe_audit(store, user, action, ...)",
        "ecommerce_order_lookup": "ecommerce service self._audit_call(action, ...)",
        "identity_link": "episodic_identity_routes._audit_identity(request, action, ...)",
    }
    gone = {a: how for a, how in sentinels.items() if a not in lits}
    assert not gone, f"AST 包装提取器没抓到已知形态的哨兵动作: {gone}"
    # 量级自证：包装动作现有 40+，跌破一半说明识别语义被改坏
    assert len(lits) >= 20, f"包装调用点仅提取到 {len(lits)} 个动作，疑似识别失效"


def test_extractor_skips_nested_call_arguments():
    """防回归自证：session.get('username', 'web_admin') 里的字面量不得被当 action。"""
    sample = (
        'audit_store.log(request.session.get("username", "web_admin"),\n'
        '                "update_template", target, old, new)\n'
    )
    got = _LOG_CALL.findall(sample)
    assert got == ["update_template"], got


def test_wrapper_extractor_shapes_unit():
    """合成样例钉住三种调用形态的槽位算法（含方法调用的 self 左移一格）。"""
    sample = (
        "def _audit(request, action, target=''):\n"
        "    audit_store.log(_actor(request), action, target)\n"
        "\n"
        "def _safe_audit(audit_store, user_id, action, target):\n"
        "    audit_store.log(user_id or 'system', action, target=target)\n"
        "\n"
        "class S:\n"
        "    def _audit_call(self, action, query):\n"
        "        self._audit.log(user_id='x', action=action, target=query)\n"
        "    def use(self):\n"
        "        self._audit_call('m3_method', 'q')\n"
        "\n"
        "def caller():\n"
        "    _audit(request, 'm1_plain')\n"
        "    _safe_audit(store, 'u', 'm2_offset', 't')\n"
        "    _audit(request, f'dyn_{x}')\n"
    )
    lits, dyns = _wrapper_actions_from(ast.parse(sample))
    assert lits == {"m1_plain", "m2_offset", "m3_method"}, lits
    assert dyns == [("_audit", "dyn_")], dyns


def test_logger_style_wrappers_not_misdetected():
    """logger.log(level, msg) 类包装不得被误认成审计包装（否则日志文案会被
    当 action 逼着登记词条）。"""
    sample = (
        "def _emit(level, text):\n"
        "    logger.log(level, text)\n"
        "\n"
        "_emit(10, 'not_an_action')\n"
    )
    lits, dyns = _wrapper_actions_from(ast.parse(sample))
    assert not lits and not dyns, (lits, dyns)


# 动态拼接的 action（f"import_config_{mode}" 等）字面量扫描天然抓不到，
# 人工登记在此台账；条目要求：① 词典必须有双语标签（防止将来改枚举后
# 词条漂移成裸键）② 拼接调用点必须还存在（防台账过期变摆设）。
_KNOWN_DYNAMIC_ACTIONS = {
    "import_config_overwrite": 'f"import_config_{mode}"',
    "import_config_merge": 'f"import_config_{mode}"',
    # ecommerce_tools/service.py::_fail → self._audit_call(f"ecommerce_{kind}_error")
    "ecommerce_order_error": 'f"ecommerce_{kind}_error"',
    "ecommerce_shipment_error": 'f"ecommerce_{kind}_error"',
}


def test_known_dynamic_actions_labeled_and_still_dynamic():
    from src.web.i18n_packs import audit_actions

    lack = [a for a in _KNOWN_DYNAMIC_ACTIONS
            if f"aud_act_{a}" not in audit_actions.ZH
            or f"aud_act_{a}" not in audit_actions.EN]
    assert not lack, f"动态动作缺双语词条: {lack}"

    all_src = "\n".join(text for _p, text, _t in _trees())
    stale = [frag for frag in set(_KNOWN_DYNAMIC_ACTIONS.values())
             if frag.strip('f"').split("{")[0] not in all_src]
    assert not stale, f"台账里的动态拼接调用点已不存在，请更新 _KNOWN_DYNAMIC_ACTIONS: {stale}"


def test_dynamic_wrapper_sites_registered():
    """包装的每个非字面 action 调用点都必须能对上动态台账：
    f-string 的静态前缀须匹配 ≥1 个台账动作（startswith）；裸变量传参
    （空前缀）一律点名——要么改成字面量，要么进台账并说明拼法。"""
    _, dyns = _collect_wrapper_actions()
    unregistered = [
        d for d in dyns
        if not (d[2] and any(k.startswith(d[2]) for k in _KNOWN_DYNAMIC_ACTIONS))
    ]
    assert not unregistered, (
        "以下审计包装调用点用动态 action，但 _KNOWN_DYNAMIC_ACTIONS 台账对不上"
        f"（(文件, 包装名, 静态前缀)）：{unregistered}"
    )


def test_audit_named_wrappers_forward_scannably():
    """名字含 audit、又把形参丢进 *.log() action 槽位的函数，必须满足
    可扫描约定（形参名叫 action，或 receiver 名含 audit）——否则 AST
    提取认不出它，其调用点的动作会静默漏登记（正是 2026-08-18 的病）。"""
    bad = []
    for p, _text, tree in _trees():
        rel = str(p.relative_to(SRC.parent))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "audit" not in node.name.lower():
                continue
            if _wrapper_spec(node) is not None:
                continue  # 已被识别，天然可扫
            params = [a.arg for a in node.args.args]
            for n in ast.walk(node):
                if not isinstance(n, ast.Call):
                    continue
                fwd = _log_action_forward(n)
                if fwd and fwd[0] in params:
                    bad.append((rel, node.name))
                    break
    assert not bad, (
        "以下 audit 包装转发形参进 .log() 但不满足可扫描约定"
        f"（把 action 形参改名为 action，或调整 receiver 命名）：{bad}"
    )
