# -*- coding: utf-8 -*-
"""群/频道自动化守卫（P1 2026-08-20 → B44 2026-08-22 收紧）。

不变量：
1. **未显式设置**档位的群/频道，resolve/bootstrap 不得回落全局 ``auto_ai``
   （生效 ``review``）。平台豁免走配置 ``inbox.auto_draft.
   group_autopilot_platforms``，**默认空＝无豁免**（B44：1.0.46 实录，客户包
   全自动下用户 AI 在报障群以用户身份代答——telegram 常驻豁免对客户包是
   安全事故）；内部陪聊部署经 overlay 配 [telegram] 保留 A 线群闸语义
   （被@触发/interject 概率/群冷却）。
2. bootstrap **绝不替群写档**（任何平台，含豁免平台）：群的显式 auto_ai 只能
   来自坐席经 confirm_group 409 闸的确认，否则确认闸形同虚设。
3. 协议直发链（run_autoreply）对群 = 显式 opt-in；档位未知（store 异常/未接线）
   对群 fail-closed。telegram 群经此链同样受闸（豁免只属 pyrogram A 线）。
4. ``skip_group_chats`` 对显式确认全自动的群豁免——否则「A 线让位 + B 线跳过」
   双让死锁（198 事故同构），确认过的群永远哑火。
5. ``skip_group_chats`` 对**弱证据群**（messenger/instagram 网页 DOM 启发式）
   默认不生效：那是全链唯一「群→零草稿」的静默出口，猜错就等于静默丢客户且
   看板无痕；不跳过的代价只是真群草稿进人审队列（可见可拒）。
"""
from __future__ import annotations

import pytest

from src.inbox.automation_mode import (
    conversation_is_group,
    group_autopilot_exempt,
    group_draft_skip,
    group_evidence_is_weak,
    maybe_bootstrap_automation_mode,
    resolve_automation_mode,
)
from src.inbox.store import InboxStore
from src.integrations import protocol_autoreply as pa
from src.integrations.protocol_bridge import ingest_incoming

CFG_AUTO = {"inbox": {"auto_draft": {
    "automation_mode": "auto_ai", "bootstrap_automation_mode": True}}}
CFG_REVIEW = {"inbox": {"auto_draft": {"automation_mode": "review"}}}
# 内部陪聊部署形态：telegram 群策略豁免显式开启（zhiliao overlay 同款）
CFG_AUTO_TG_EXEMPT = {"inbox": {"auto_draft": {
    "automation_mode": "auto_ai", "bootstrap_automation_mode": True,
    "group_autopilot_platforms": ["telegram"]}}}


def _seed(store, platform, chat_key, chat_type=""):
    return ingest_incoming(
        store, platform=platform, account_id="a", chat_key=chat_key,
        name="x", text="hi", direction="in", chat_type=chat_type)


@pytest.fixture()
def store(tmp_path):
    return InboxStore(tmp_path / "inbox.db")


# ── 1/2. resolve / bootstrap 群守卫 ─────────────────────────────────────────

def test_resolve_unset_group_caps_to_review(store):
    cid = _seed(store, "zalo", "g1", chat_type="group")
    assert resolve_automation_mode(store, cid, CFG_AUTO) == "review"


def test_resolve_unset_channel_caps_to_review(store):
    cid = _seed(store, "whatsapp", "ch1", chat_type="channel")
    assert resolve_automation_mode(store, cid, CFG_AUTO) == "review"


def test_resolve_unset_private_keeps_global_auto(store):
    cid = _seed(store, "zalo", "u1")
    assert resolve_automation_mode(store, cid, CFG_AUTO) == "auto_ai"


def test_resolve_telegram_group_default_excluded(store):
    """B44（2026-08-22）：豁免默认清空——telegram 群在缺省配置下同样封 review。

    1.0.46 实录：客户包全自动+新会话沿用，用户 AI 把报障群当客户会话以用户
    身份代答。群聊默认排除自动回复，显式开启（配置豁免/坐席确认）才放行。"""
    cid = _seed(store, "telegram", "-100777", chat_type="group")
    assert group_autopilot_exempt(cid) is False
    assert group_autopilot_exempt(cid, CFG_AUTO) is False
    assert resolve_automation_mode(store, cid, CFG_AUTO) == "review"


def test_resolve_telegram_group_exempt_via_config(store):
    # 内部陪聊部署：overlay 显式配 [telegram] → A 线群闸语义保留（回落全局）
    cid = _seed(store, "telegram", "-100777b", chat_type="group")
    assert group_autopilot_exempt(cid, CFG_AUTO_TG_EXEMPT) is True
    assert resolve_automation_mode(store, cid, CFG_AUTO_TG_EXEMPT) == "auto_ai"


def test_resolve_explicit_auto_group_wins(store):
    # 坐席经 confirm_group 闸确认过的群：显式档位永远最高优先
    cid = _seed(store, "zalo", "g2", chat_type="group")
    store.set_automation_mode(cid, "auto_ai", source="human")
    assert resolve_automation_mode(store, cid, CFG_AUTO) == "auto_ai"


def test_resolve_global_review_unaffected(store):
    cid = _seed(store, "zalo", "g3", chat_type="group")
    assert resolve_automation_mode(store, cid, CFG_REVIEW) == "review"


def test_bootstrap_never_persists_for_groups(store):
    zg = _seed(store, "zalo", "g4", chat_type="group")
    assert maybe_bootstrap_automation_mode(store, zg, CFG_AUTO) == "review"
    assert store.get_automation_mode_if_set(zg) is None
    tg = _seed(store, "telegram", "-100888", chat_type="group")
    # B44：缺省配置下 TG 群同样封 review 且不代写
    assert maybe_bootstrap_automation_mode(store, tg, CFG_AUTO) == "review"
    assert store.get_automation_mode_if_set(tg) is None
    # 豁免形态（内部部署）：返回全局但**同样不代写**——显式档位只能来自人
    tg2 = _seed(store, "telegram", "-100888b", chat_type="group")
    assert maybe_bootstrap_automation_mode(store, tg2, CFG_AUTO_TG_EXEMPT) == "auto_ai"
    assert store.get_automation_mode_if_set(tg2) is None


def test_bootstrap_private_persists_unchanged(store):
    cid = _seed(store, "zalo", "u2")
    assert maybe_bootstrap_automation_mode(store, cid, CFG_AUTO) == "auto_ai"
    assert store.get_automation_mode_if_set(cid) == "auto_ai"


def test_resolve_and_bootstrap_agree_on_unset(store):
    """两入口对未显式档位的会话必须同答案——分叉＝UI 与行为对不上。"""
    quadrants = [
        ("zalo", "g5", "group"),
        ("zalo", "u3", ""),
        ("telegram", "-100999", "group"),
        ("telegram", "555", ""),
    ]
    for plat, ck, ct in quadrants:
        cid = _seed(store, plat, ck, chat_type=ct)
        assert (resolve_automation_mode(store, cid, CFG_AUTO)
                == maybe_bootstrap_automation_mode(store, cid, CFG_AUTO)), cid
        # bootstrap 之后 resolve 仍稳定（幂等）
        assert resolve_automation_mode(store, cid, CFG_AUTO) \
            == maybe_bootstrap_automation_mode(store, cid, CFG_AUTO), cid


def test_conversation_is_group_fail_open(store):
    assert conversation_is_group(None, "zalo:a:g") is False
    assert conversation_is_group(store, "") is False
    assert conversation_is_group(store, "zalo:a:missing") is False


# ── 4. skip_group_chats 显式全自动豁免 ───────────────────────────────────────

def test_group_draft_skip_matrix(store):
    cid = _seed(store, "zalo", "g6", chat_type="group")
    conv = store.get_conversation(cid)
    # skip 开 + 群 + 未确认 → 跳过（旧语义）
    assert group_draft_skip(conv, store, True) is True
    # 显式确认全自动 → 豁免（修双让死锁）
    store.set_automation_mode(cid, "auto_ai", source="human")
    assert group_draft_skip(conv, store, False) is False
    assert group_draft_skip(conv, store, True) is False
    # 显式 review（人审）→ 仍跳过（skip 语义只对确认全自动让路）
    store.set_automation_mode(cid, "review", source="human")
    assert group_draft_skip(conv, store, True) is True
    # 私聊永不跳过
    pid = _seed(store, "zalo", "u4")
    assert group_draft_skip(store.get_conversation(pid), store, True) is False


# ── 5. 弱证据群不走静默出口 ─────────────────────────────────────────────────

def test_group_evidence_strength_by_platform():
    """判据强度＝边车实现属性：地址自描述的是硬事实，DOM 猜的是弱证据。"""
    for cid in ("messenger:a:g1", "instagram:a:g1", "INSTAGRAM:a:g1"):
        assert group_evidence_is_weak(cid) is True, cid
    for cid in ("whatsapp:a:g1", "line:a:g1", "telegram:a:-100", "zalo:a:g1", ""):
        assert group_evidence_is_weak(cid) is False, cid


@pytest.mark.parametrize("platform", ["messenger", "instagram"])
def test_weak_evidence_group_still_drafts(store, platform):
    """默认：弱证据群照常拟稿（可见失败 > 静默丢客户）；档位仍降 review。"""
    cid = _seed(store, platform, "g7", chat_type="group")
    conv = store.get_conversation(cid)
    assert group_draft_skip(conv, store, True) is False
    # 但「AI 在群里自动说话」这条闸不松：未显式确认仍降 review
    assert resolve_automation_mode(store, cid, CFG_AUTO) == "review"
    # 显式信任启发式 → 恢复旧的一律跳过
    assert group_draft_skip(conv, store, True, trust_weak_evidence=True) is True


def test_hard_evidence_group_still_skips(store):
    """硬证据平台不受本项影响（否则 skip_group_chats 整体失效）。"""
    for plat in ("whatsapp", "line", "zalo"):
        cid = _seed(store, plat, "g8", chat_type="group")
        assert group_draft_skip(store.get_conversation(cid), store, True) is True


# ── 3. run_autoreply 群 opt-in 闸 ───────────────────────────────────────────

class _FakeRegistry:
    def __init__(self, row):
        self._row = row

    def get(self, platform, account_id):
        return self._row


def _row():
    return {"platform": "zalo", "account_id": "z1",
            "meta": {"auto_reply": True, "persona_id": ""}}


def _group_payload(**over):
    p = {"platform": "zalo", "account_id": "z1", "chat_key": "gid9",
         "text": "大家好", "direction": "in", "chat_type": "group"}
    p.update(over)
    return p


def _mk_send(sink):
    async def _send(**kw):
        sink.append(kw)
        return {"delivered": True}
    return _send


def _mk_gen(reply="ok"):
    async def _gen(**kw):
        return reply
    return _gen


@pytest.fixture(autouse=True)
def _clear_pa_state():
    pa._last_reply.clear()
    pa._last_sent.clear()
    yield
    pa._last_reply.clear()
    pa._last_sent.clear()


_PA_CFG = {"protocol_autoreply": {"enabled": True}}


@pytest.mark.asyncio
async def test_group_payload_without_mode_fn_is_gated():
    """档位读不出（未接线）→ 群 fail-closed，绝不直发。"""
    sent = []
    res = await pa.run_autoreply(
        _group_payload(), registry=_FakeRegistry(_row()), cfg=_PA_CFG,
        generate=_mk_gen(), send=_mk_send(sent), risk_fn=lambda t: "low")
    assert res["skipped"] == "group_optin_required"
    assert sent == []


@pytest.mark.asyncio
async def test_group_payload_mode_fn_error_is_gated():
    sent = []

    def _boom(p, a, c):
        raise RuntimeError("store down")

    res = await pa.run_autoreply(
        _group_payload(), registry=_FakeRegistry(_row()), cfg=_PA_CFG,
        generate=_mk_gen(), send=_mk_send(sent), risk_fn=lambda t: "low",
        inbox_mode_fn=_boom)
    assert res["skipped"] == "group_optin_required"
    assert sent == []


@pytest.mark.asyncio
async def test_group_review_mode_is_gated():
    sent = []
    res = await pa.run_autoreply(
        _group_payload(), registry=_FakeRegistry(_row()), cfg=_PA_CFG,
        generate=_mk_gen(), send=_mk_send(sent), risk_fn=lambda t: "low",
        inbox_mode_fn=lambda p, a, c: "review")
    assert res["skipped"] == "group_optin_required"
    assert sent == []


@pytest.mark.asyncio
async def test_group_source_chat_type_also_gated():
    """TG 协议 worker 把 chat_type 放 source 里——同样要被识别为群。"""
    sent = []
    payload = _group_payload(chat_type="", source={"chat_type": "supergroup"})
    res = await pa.run_autoreply(
        payload, registry=_FakeRegistry(_row()), cfg=_PA_CFG,
        generate=_mk_gen(), send=_mk_send(sent), risk_fn=lambda t: "low")
    assert res["skipped"] == "group_optin_required"


@pytest.mark.asyncio
async def test_tg_negative_chat_key_heuristic_gated():
    """TG 负 id 群即使没带 chat_type 也要进闸（豁免只属 pyrogram A 线）。"""
    sent = []
    payload = _group_payload(platform="telegram", chat_key="-100123",
                             chat_type="")
    row = {"platform": "telegram", "account_id": "z1",
           "meta": {"auto_reply": True, "persona_id": ""}}
    res = await pa.run_autoreply(
        payload, registry=_FakeRegistry(row), cfg=_PA_CFG,
        generate=_mk_gen(), send=_mk_send(sent), risk_fn=lambda t: "low")
    assert res["skipped"] == "group_optin_required"


@pytest.mark.asyncio
async def test_group_explicit_auto_proceeds_and_sends():
    """坐席显式确认全自动的群：l2 deliver 关（ChatX 默认）→ 本链正常直发。"""
    sent = []
    res = await pa.run_autoreply(
        _group_payload(text="有人在吗"), registry=_FakeRegistry(_row()),
        cfg=_PA_CFG, generate=_mk_gen("在的~"), send=_mk_send(sent),
        risk_fn=lambda t: "low",
        inbox_mode_fn=lambda p, a, c: "auto_ai")
    assert res.get("sent") is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_group_explicit_auto_yields_when_deliver_on():
    """确认全自动 + l2 deliver 开（本机档）→ 让位 System Z，防双发。"""
    sent = []
    cfg = {"protocol_autoreply": {"enabled": True},
           "inbox": {"l2_autosend": {"deliver": True}}}
    res = await pa.run_autoreply(
        _group_payload(text="在么"), registry=_FakeRegistry(_row()), cfg=cfg,
        generate=_mk_gen(), send=_mk_send(sent), risk_fn=lambda t: "low",
        inbox_mode_fn=lambda p, a, c: "auto_ai")
    assert res["skipped"] == "inbox_autopilot"
    assert sent == []


@pytest.mark.asyncio
async def test_private_payload_unaffected():
    """私聊零变化：不带 chat_type、正 id → 不进群闸，照常直发。"""
    sent = []
    payload = {"platform": "zalo", "account_id": "z1", "chat_key": "777",
               "text": "hi", "direction": "in"}
    res = await pa.run_autoreply(
        payload, registry=_FakeRegistry(_row()), cfg=_PA_CFG,
        generate=_mk_gen("hello"), send=_mk_send(sent),
        risk_fn=lambda t: "low")
    assert res.get("sent") is True


def test_group_optin_reason_not_handoff_not_audit():
    """群闸 skip 不进「转人工」也不进审计——每条群消息打一个需人工标=刷爆 attn。"""
    assert "group_optin_required" not in pa.HANDOFF_REASONS
    assert "group_optin_required" not in pa.AUDIT_REASONS
