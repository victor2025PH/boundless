"""平台能力矩阵门禁（2026-07-31）。

矩阵的价值全在「不会漂移」上，所以这里守四件事：

1. **生成物与代码一致**——``docs/平台能力矩阵.md`` 必须等于当场渲染的结果。
2. **判据与编排器同源**——矩阵说的 send_media/mark_read/typing，必须就是编排器
   运行时用的那三个 hasattr，而不是另立一套说法。
3. **开关说明是活的**——``SWITCHED_CAPABILITIES`` 登记的每个开关，翻开后那个格子
   必须真的从「不支持」变「支持」；翻了不变＝登记过期，这里红。
4. **新 worker 不会被漏登记**——``ensure_builtin_workers`` 注册进编排器的平台，
   必须都在 ``WORKERS`` 里，否则新平台上线后矩阵会悄悄少一行。

第 2、4 条是为了防「矩阵自己变成又一份会过期的文档」——那样还不如没有。
"""

import re
from pathlib import Path

import pytest

from src.integrations import platform_capabilities as PC
from scripts.platform_matrix import render_matrix

ENGINE_ROOT = Path(__file__).resolve().parents[1]
DOC = ENGINE_ROOT / "docs" / "平台能力矩阵.md"


def test_doc_matches_generated():
    """生成物与代码逐字一致（改了 worker 就重跑 scripts/platform_matrix，别手改文档）。"""
    assert DOC.is_file(), "缺 docs/平台能力矩阵.md：跑 python -m scripts.platform_matrix --out"
    assert DOC.read_text(encoding="utf-8") == render_matrix({}), (
        "文档与代码不一致 —— 重跑："
        "python -m scripts.platform_matrix --out docs/平台能力矩阵.md")


def test_render_is_deterministic():
    """两次渲染必须字节一致（带时间戳之类会让同步门禁被噪声逼疯）。"""
    assert render_matrix({}) == render_matrix({})


def test_capability_methods_match_orchestrator_judgement():
    """矩阵的判据必须就是编排器运行时那套 hasattr，不能另立一套说法。

    直接扫源码：``owns_media`` / ``mark_read`` / ``send_chat_action`` 三处的
    hasattr 参数名，必须与 ``CAPABILITY_METHODS`` 对得上。谁把编排器改成别的判据
    （比如加个 supports_media() 协议方法），这条会红，提醒同步改矩阵。
    """
    src = (ENGINE_ROOT / "src" / "integrations" / "account_orchestrator.py"
           ).read_text(encoding="utf-8")
    for cap in ("send_media", "mark_read", "send_chat_action"):
        assert re.search(r'hasattr\(\s*\w+(?:\.\w+)*\s*,\s*"%s"\s*\)' % cap, src), (
            "编排器不再用 hasattr(%s) 判能力了 —— platform_capabilities "
            "的判据要跟着改" % cap)
    assert set(PC.CAPABILITY_METHODS.values()) == {
        "send", "send_media", "mark_read", "send_chat_action"}


def test_capability_labels_cover_all_methods():
    assert set(PC.CAPABILITY_LABELS) == set(PC.CAPABILITY_METHODS)


def test_switched_capabilities_are_live():
    """登记的每个开关都必须**真的**能把那个格子从关翻到开（防说明过期）。"""
    assert PC.SWITCHED_CAPABILITIES, "至少应登记 LINE 出站媒体那个开关"
    for (platform, mode, cap), dotted in PC.SWITCHED_CAPABILITIES.items():
        key = "%s:%s" % (platform, mode)
        base = PC.capability_matrix({})
        row = base.get(key)
        assert row is not None, "开关登记指向了 WORKERS 里没有的 %s" % key
        if not row["available"]:
            pytest.skip("本机构造不出 %s（缺依赖）" % key)
        assert row["caps"][cap] is False, "%s 的 %s 出厂就该是关的" % (key, cap)
        on = PC.capability_matrix(PC._switch_on({}, dotted))
        assert on[key]["caps"][cap] is True, (
            "翻开 %s 之后 %s 的 %s 没有变成支持 —— 登记过期或开关改名了"
            % (dotted, key, cap))


def test_switched_cells_reports_the_switch():
    cells = PC.switched_cells({})
    row = PC.capability_matrix({}).get("line:protocol") or {}
    if not row.get("available"):
        pytest.skip("本机构造不出 line worker")
    assert cells.get("line:protocol", {}).get("send_media") == \
        "platform_login.line.media.outbound"
    # 开关已开的部署里不该再报「待开」（它已经是 Y 了）
    on_cfg = PC._switch_on({}, "platform_login.line.media.outbound")
    assert "send_media" not in (PC.switched_cells(on_cfg).get("line:protocol") or {})


def test_registered_workers_are_all_in_matrix():
    """编排器真会注册的平台必须都在矩阵里（新平台上线不能悄悄少一行）。

    注册是按配置+依赖门控的，本机缺 pyrogram/okline 时会少注册几项 → 只做**单向**
    包含检查（注册了的必须在表里），不反向要求，从而对缺依赖的机器友好。
    """
    from src.integrations import account_orchestrator as AO

    cfg = {
        "platform_login": {
            "telegram": {"protocol_enabled": True},
            "whatsapp": {"protocol_enabled": True},
            "messenger": {"web_enabled": True},
            "line": {"protocol_enabled": True},
        },
    }
    AO.ensure_builtin_workers(cfg)
    known = {"%s:%s" % (p, m.split("(")[0]) for p, m, _, _ in PC.WORKERS}
    for key in AO._WORKER_FACTORIES:
        platform, _, mode = key.partition(":")
        if mode == "official":
            continue  # 官方通道 worker 是另一族（一个类服务多平台），不进本表
        assert key in known, (
            "编排器注册了 %s，但 platform_capabilities.WORKERS 里没有它 —— "
            "矩阵会少一行" % key)


# ─────────────────── 入站接线判定 ───────────────────

def test_inbound_sites_are_all_findable():
    """每个登记的入站接线点都必须能在源码里找到。

    找不到 → ``inbound_media_wired`` 返回 None → 表格渲染成 ``?``。这条门禁保证
    「重构改了函数名」会**立刻红**，而不是让那一格悄悄从 Y 变成 ?（看的人会以为
    是缺依赖）。
    """
    for key in PC.INBOUND_SITES:
        assert PC.inbound_media_wired(key) is not None, (
            "%s 的入站接线点找不到了（函数被重命名？）—— 更新 INBOUND_SITES" % key)


def test_inbound_sites_cover_every_matrix_row():
    """矩阵里每一行都要有入站判据，否则那一行的收媒体列永远是 ``?``。"""
    rows = set(PC.capability_matrix({}))
    assert rows <= set(PC.INBOUND_SITES), (
        "这些行缺入站接线登记：%s" % sorted(rows - set(PC.INBOUND_SITES)))


def _wired(code: str) -> bool:
    import ast
    return PC._forwards_media_type(ast.parse(code).body[0])


def test_inbound_checker_discriminates():
    """判定器必须真能判负——否则这一列就是装饰品。

    负样本取自**真实历史**：2026-07-31 之前 LINE 的 ``_on_msg`` 就是「只传 text、
    不传 media_type」，客户发的图因此对 AI 不存在。
    """
    # 接了（值是变量 / 表达式，两种真实形态都覆盖）
    assert _wired("def f():\n    make_message(text=t, media_type=mt, media_ref=mr)")
    assert _wired("def f():\n    make_message(media_type=str(body.get('media_type')))")
    # 没接（LINE 修复前的形态）
    assert not _wired("def f():\n    make_message(text=t, msg_id=i, direction='in')")
    # 显式传空串 = 没接线，不能算数
    assert not _wired("def f():\n    make_message(media_type='', media_ref='')")
    assert not _wired("def f():\n    make_message(media_type='   ')")
    # 嵌套调用里接的也算（真实代码常包在 if / try 里）
    assert _wired("def f():\n    if x:\n        try:\n            g(media_type=k)\n"
                  "        except Exception:\n            pass")


def test_inbound_wiring_matches_known_truth():
    """当下四条链**都**已接线（LINE 是 2026-07-31 本次补的，其余本来就有）。"""
    for key in PC.INBOUND_SITES:
        assert PC.inbound_media_wired(key) is True, "%s 入站媒体断线了" % key


def test_inbound_runtime_switch_caveat_is_accurate():
    """文档里点名的那个「能把收媒体停掉的运行时开关」必须真的还在。

    收媒体列是**源码判定**、不读配置，所以运营关掉 LINE 入站后那格仍是 Y。文档为此
    写了一句说明并点了开关名——开关若被改名/删除，这条门禁红，逼人回来改说明，
    免得那句话变成又一处「看着对、其实错」的描述。
    """
    from src.integrations.line_media import resolve_line_media_cfg
    assert "inbound" in resolve_line_media_cfg({}), \
        "platform_login.line.media.inbound 不在了 —— 更新矩阵文档里的说明"
    assert "platform_login.line.media.inbound" in render_matrix({})


# ─────────────────── agent 指令文件里的指针 ───────────────────

_AGENT_DOCS = ("AGENTS.md", "CLAUDE.md")
_SECTION = "## 四平台能力差异"


def _agent_section(name: str) -> str:
    text = (ENGINE_ROOT / name).read_text(encoding="utf-8")
    i = text.find(_SECTION)
    assert i >= 0, "%s 里找不到「%s」段（被删/改名了？）" % (name, _SECTION)
    j = text.find("\n## ", i + 1)
    return text[i:j if j > 0 else len(text)]


def test_agent_docs_pointers_are_alive():
    """两份 agent 指令文件里指到的路径必须真实存在。

    这两个文件**每次会话都会自动加载**，里面的死链不是「文档小瑕疵」而是主动误导：
    下一个人照着敲一条不存在的命令，只会退回去通读四个 worker 源码——那正是这张
    矩阵要消灭的成本。
    """
    pat = re.compile(r"(?:docs|scripts|tools|tests)/[\w\-./\u4e00-\u9fff]+?\.(?:md|py)")
    for name in _AGENT_DOCS:
        sec = _agent_section(name)
        refs = set(pat.findall(sec))
        assert refs, "%s 的能力矩阵段没有任何路径引用，指针失效了" % name
        for ref in sorted(refs):
            assert (ENGINE_ROOT / ref).exists(), "%s 指到了不存在的 %s" % (name, ref)


def test_agent_docs_section_stays_in_sync():
    """AGENTS.md 与 CLAUDE.md 的这一段必须逐字一致。

    两份文件整体并不相同（一份写 Codex 一份写 Claude），所以不做全文件同步；但这段
    刻意不含任何工具名，没有理由分叉——只更新一份就是让另一半 agent 拿到旧信息。
    """
    a, c = (_agent_section(n) for n in _AGENT_DOCS)
    assert a == c, "AGENTS.md 与 CLAUDE.md 的能力矩阵段不一致（只改了一份？）"


# ─────────────────── 代码接线 x 实际到货 对照 ───────────────────

def _flags(**kw):
    return {p: dict(v) for p, v in kw.items()}


def _one(rows, platform, dim):
    return next(r for r in rows if r["platform"] == platform and r["dim"] == dim)


def test_code_flags_collapse_modes():
    """同平台多档：全 Y→Y、全 N→N、不一致→mixed（不硬判，避免假告警）。"""
    m = PC.capability_matrix({})
    f = PC.code_flags(m)
    # Telegram 两档 send_media 不一致（protocol 有、companion 无）→ mixed
    assert f["telegram"]["send_media"] == "mixed"
    assert f["whatsapp"]["send_media"] == "Y"
    assert f["telegram"]["recv_media"] == "Y"


def test_positive_evidence_beats_sample_size():
    """**真到过货就算通**，哪怕总量很小。

    这条是实施当天的真 bug：原先样本阈值排在正面证据之前，Messenger「13 条出站里
    5 条是媒体」被判成「样本不足」——明明已经证明链路是通的。低流量平台会因此
    永远读不出正面信号。
    """
    rows = PC.reconcile(
        _flags(messenger={"send_media": "Y", "recv_media": "Y"}),
        {"messenger": {"out_total": 13, "out_media": 5,
                       "in_total": 3, "in_media": 1}},
        min_sample=20, accepted={})
    assert _one(rows, "messenger", "send_media")["verdict"] == "ok"
    assert _one(rows, "messenger", "recv_media")["verdict"] == "ok"


def test_dry_needs_enough_sample():
    """接了却零到货：样本够才敢说「有问题」，不够只说「没数据」。"""
    live_small = {"x": {"out_total": 5, "out_media": 0, "in_total": 5, "in_media": 0}}
    live_big = {"x": {"out_total": 99, "out_media": 0, "in_total": 99, "in_media": 0}}
    f = _flags(x={"send_media": "Y", "recv_media": "Y"})
    assert _one(PC.reconcile(f, live_small, accepted={}),
                "x", "send_media")["verdict"] == "no_traffic"
    assert _one(PC.reconcile(f, live_big, accepted={}),
                "x", "send_media")["verdict"] == "dry"


def test_unexpected_when_code_says_no_but_media_flows():
    """代码说不支持却真有货 → 判据漏了旁路（本会话就靠这种交叉验伪抓到过结论）。"""
    rows = PC.reconcile(
        _flags(x={"send_media": "N", "recv_media": "N"}),
        {"x": {"out_total": 50, "out_media": 7, "in_total": 0, "in_media": 0}},
        accepted={})
    assert _one(rows, "x", "send_media")["verdict"] == "unexpected"
    # 说不支持、也确实没货 = 自洽
    assert _one(rows, "x", "recv_media")["verdict"] in ("ok", "no_traffic")


def test_mixed_and_explained_and_out_of_scope():
    f = _flags(x={"send_media": "mixed", "recv_media": "Y"})
    live = {"x": {"out_total": 99, "out_media": 1, "in_total": 99, "in_media": 9},
            "web": {"out_total": 30, "out_media": 0, "in_total": 30, "in_media": 0}}
    rows = PC.reconcile(f, live, accepted={})
    assert _one(rows, "x", "send_media")["verdict"] == "mixed"
    # 生产库里有但矩阵里没有的平台 → n/a，不能拿它凑一个看着健康的 OK
    assert _one(rows, "web", "send_media")["verdict"] == "out_of_scope"
    # 登记了原因 → explained 且带上原因原文
    rows2 = PC.reconcile(f, live, accepted={("x", "send_media"): "另有旁路"})
    got = _one(rows2, "x", "send_media")
    assert got["verdict"] == "explained" and got["reason"] == "另有旁路"


def test_accepted_mismatches_not_stale():
    """已解释的差异必须**仍然存在**——差异消失了就该把登记删掉，否则它会一直
    压住一个其实已经没有的现象（登记比没登记更危险）。"""
    f = PC.code_flags(PC.capability_matrix({}))
    for (platform, dim), why in PC.ACCEPTED_MISMATCHES.items():
        assert why.strip(), "登记必须带原因"
        assert platform in f, "%s 已不在矩阵内，登记该删" % platform
        assert f[platform][dim] != "Y", (
            "%s 的 %s 现在代码上已经是 Y 了，没有差异可解释 —— 删掉这条登记"
            % (platform, dim))


def test_live_data_refuses_to_pollute_generated_doc(monkeypatch, tmp_path):
    """``--out`` 与实时读数互斥：文档必须确定性、机器无关（门禁逐字钉住它）。"""
    import scripts.platform_matrix as PM
    out = tmp_path / "should_not_exist.md"
    monkeypatch.setattr(
        "sys.argv", ["platform_matrix.py", "--out", str(out), "--with-live-data"])
    assert PM.main() == 2
    assert not out.exists(), "被拒时不该写出任何文件"


def test_hard_limits_are_documented_not_silently_false():
    """协议层硬限制必须在文档里点名——否则读者会以为「只是还没做」。"""
    text = render_matrix({})
    for (platform, cap) in PC.HARD_LIMITS:
        assert PC.CAPABILITY_LABELS[cap] in text
        assert platform.lower() in text.lower()
    assert "协议层硬限制" in text


def test_matrix_reflects_known_asymmetries():
    """把 2026-07-31 人工通读四个 worker 得到的结论钉成回归网。

    这几条不是随便挑的，全是当时被问到、且**代码注释写错过**的那些。
    """
    m = PC.capability_matrix({})

    def caps(key):
        row = m.get(key)
        if row is None or not row["available"]:
            pytest.skip("本机构造不出 %s" % key)
        return row["caps"]

    # Telegram 协议号是能力最全的一档
    assert all(caps("telegram:protocol").values())
    # WhatsApp 同样四项齐全（曾被注释误写成「mark_read 暂无」）
    assert all(caps("whatsapp:protocol").values())
    # Messenger web 有媒体、但没有已读/正在输入
    mg = caps("messenger:web")
    assert mg["send_media"] is True
    assert mg["mark_read"] is False and mg["typing"] is False
    # LINE：文本+已读有，媒体要开开关，typing 是协议层不可做
    ln = caps("line:protocol")
    assert ln["send_text"] is True and ln["mark_read"] is True
    assert ln["send_media"] is False   # 出厂关；开关见 SWITCHED_CAPABILITIES
    assert ln["typing"] is False
    # Telegram companion 运行时**没有** send_media——与协议号不同档，易被忽略
    assert caps("telegram:protocol(companion)")["send_media"] is False
    # 入站：四条链都能把客户发的图/语音交给 AI（LINE 那条是本次补的）
    for key in ("telegram:protocol", "telegram:protocol(companion)",
                "whatsapp:protocol", "messenger:web", "line:protocol"):
        assert m[key]["recv_media"] is True, "%s 收不到媒体" % key
