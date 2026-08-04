"""新号接入演练门禁：驱动**真实** `_conversations()` 闭包，钉死 .198 群发事故不复发。

为什么单靠 `test_outbound_gate.py` 不够
========================================
那份门禁证明的是判定**逻辑**正确（纯函数层）。但本次事故真正的危险形态是「**闸没接上**」：
`proactive_topic` 里的接线全程 fail-open（任何异常一律放行，见该处注释——安全闸不能变成
新的静默不发故障源），于是一个接线 bug（import 名写错、observe 传错参、判定插在 continue
之后……）**不会让任何测试变红**，只会让安全闸悄悄失效，直到下一次新号登录再炸一遍。

所以本文件不 mock 判定，而是构造一个最小 assistant 跑通生产闭包：
`maybe_start_companion_proactive()` 在 `enabled:false` 时**仍会挂上预览闭包**（源码 1285 行
挂载、1291 行才因未启用返回），于是我们能在「不起任何循环、不需要 ai_client、绝不真发」的
前提下，让真实的过滤链跑在受控数据上。断言口径取预览的 `scanned`＝`len(_conversations())`，
即**走完全部护栏后还剩几条**。

隔离掉「因为别的护栏而通过」的假绿
----------------------------------
`_conversations()` 里还有多道既有护栏（群/频道、系统 peer、死 peer、**占位会话**、档位闸、
opt-out）。若 fixture 随手一写，会话可能被那些闸拦掉，测试照样绿——而冷启动闸其实没生效。
故 fixture 刻意让每条会话都**能通过所有既有护栏**（私聊 / 正数 chat_key / 非 Saved Messages /
`last_message_dirs` 给真实方向以越过占位护栏 / 档位 auto_ai），只留冷启动闸这一个变量；
并配一条**关掉冷启动闸就应全部放行**的对照（探测器有效性自证：闸真的是它在拦）。
"""
from __future__ import annotations

import logging
import time
import types

from src.companion.proactive_topic import maybe_start_companion_proactive
from src.inbox.outbound_gate import (
    COLD_START_WARMING,
    IMPORTED_NO_INBOUND,
    QUOTA_EXHAUSTED,
    reset_suppression_stats,
    suppression_snapshot,
)

HOUR = 3600.0
DAY = 24 * HOUR

# 事故当事账号（.198 实录），留原值让门禁与事故现场可对照
NEW_ACCT = "8942577244"
OLD_ACCT = "5001"


class _FakeInboxStore:
    """只实现 `_conversations()` 真正会调到的取数面；其余给安全空值。

    刻意**不**继承真 store：真 store 要建库建表，而本门禁要验的是过滤链的判定，
    不是 SQL。行 schema 按 `list_conversations` 的消费字段给齐即可。
    """

    def __init__(self, rows):
        self._rows = list(rows)

    # ── _conversations() 的取数面 ────────────────────────────────────────────
    def list_conversations(self, limit=200, **_kw):
        return list(self._rows)[:limit]

    def last_message_dirs(self, cids):
        # 关键：给真实方向，越过「占位会话」护栏——否则会话被那道闸拦掉，
        # 冷启动闸根本没机会表态，测试就会因为错误的原因变绿。
        return {str(c): {"direction": "in"} for c in cids}

    def last_inbound_ts_map(self, cids):
        out = {}
        for r in self._rows:
            cid = str(r.get("conversation_id"))
            if cid in {str(c) for c in cids}:
                out[cid] = float(r.get("last_in_ts") or 0.0)
        return out

    def list_conv_tags_map(self, cids):
        return {}

    def get_conv_meta_for_ids(self, cids):
        return {}

    # ── 档位闸：无显式设置 → 回落全局 auto_ai（见 automation_mode.py）────────
    def get_automation_mode_if_set(self, conversation_id):
        return None

    # ── 规划/开场阶段可能触及；给空值即可（本门禁只断言 scanned）─────────────
    def get_conversation(self, *_a, **_kw):
        return None

    def list_recent_messages(self, *_a, **_kw):
        return []

    def list_messages(self, *_a, **_kw):
        return []


class _FakeSkillManager:
    """`proactive_topic` 用到的 skill_manager 方法面（枚举自源码，非猜测）。

    全部返回「无内容」：开场生成不是本门禁的关注点——我们断言的是 `scanned`
    （过完护栏还剩几条会话），它在开场生成**之前**就定了。刻意不实现
    `_episodic_storage_key`（缺失时回落裸 chat_key，正是我们要的简单路径）。
    """

    def resolve_birthday(self, *_a, **_kw):
        return None

    def resolve_preferred_name(self, *_a, **_kw):
        return ""

    def build_proactive_opener(self, *_a, **_kw):
        return None

    def build_ritual_opener(self, *_a, **_kw):
        return None

    def build_milestone_opener(self, *_a, **_kw):
        return None

    def build_profile_ask_opener(self, *_a, **_kw):
        return None

    def mark_life_share_sent(self, *_a, **_kw):
        return None


class _OwnAllOrchestrator:
    """让两个账号都「有真实发送通道」。

    否则 `_account_can_send` 只认 `account_id == "default"`，测试就只能有一个账号，
    而本门禁的判别力恰恰来自「新号被压 / 老号放行」的**对照**。
    """

    def owns(self, _platform, _account_id):
        return True


def _mk_row(cid, account_id, chat_key, created_at, last_in_ts):
    return {
        "conversation_id": cid,
        "account_id": account_id,
        "chat_key": chat_key,
        "platform": "telegram",
        "chat_type": "private",
        "created_at": created_at,
        "last_in_ts": last_in_ts,
        # 沉默判据：末条时间很久以前 → 满足 min_silent_hours（默认 24h）
        "last_ts": last_in_ts,
    }


def _mk_assistant(tmp_path, rows, cold_start=None):
    cfg = {
        "companion": {
            "proactive_topic": {
                # 关键：未启用也会挂预览闭包 → 拿得到真实过滤链，且绝不起循环、绝不真发
                "enabled": False,
                "min_silent_hours": 24,
            },
        },
        "inbox": {"auto_draft": {"automation_mode": "auto_ai"}},
    }
    if cold_start is not None:
        cfg["companion"]["proactive_topic"]["cold_start"] = cold_start
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            config=cfg, config_path=str(tmp_path / "config.yaml")),
        inbox_store=_FakeInboxStore(rows),
        skill_manager=_FakeSkillManager(),
        logger=logging.getLogger("test.proactive_cold_start"),
        telegram_client=None,            # 发送通道由 orchestrator 提供
        ai_client=None,                  # enabled:false 时走不到
        _web_app=types.SimpleNamespace(state=types.SimpleNamespace()),
        _companion_funnel_store=None,
        _companion_proactive_loop=None,
    )


async def _preview(monkeypatch, tmp_path, rows, cold_start=None):
    """跑真实闭包并返回预览结果（含 scanned＝过完全部护栏后剩余会话数）。"""
    import src.integrations.account_orchestrator as _orch_mod
    monkeypatch.setattr(
        _orch_mod, "get_orchestrator", lambda *_a, **_kw: _OwnAllOrchestrator())

    assistant = _mk_assistant(tmp_path, rows, cold_start=cold_start)
    await maybe_start_companion_proactive(assistant)
    fn = getattr(assistant._web_app.state, "companion_proactive_preview", None)
    assert fn is not None, (
        "预览闭包未挂载 —— maybe_start_companion_proactive 在设置阶段就抛了异常"
        "（它把异常吞成一行 warning，见函数尾部 except）。先看日志再断言。")
    return fn(limit=50)


def _incident_rows(now):
    """复刻 .198 现场：一个刚接入的新号（会话都是刚同步的占位，但带着手机上的
    历史 last_ts），外加一个老号上的真实沉默关系作对照。"""
    return [
        # 新号：目录同步刚灌进来的两条老关系（对方最后开口远早于账号接入）
        _mk_row("c-new-1", NEW_ACCT, "7331682688",
                created_at=now - 1 * HOUR, last_in_ts=now - 90 * DAY),
        _mk_row("c-new-2", NEW_ACCT, "5415685180",
                created_at=now - 2 * HOUR, last_in_ts=now - 30 * DAY),
        # 老号：接入很久，且对方在接入之后真的聊过（沉默 20 天的真实关系）
        _mk_row("c-old-1", OLD_ACCT, "6001",
                created_at=now - 300 * DAY, last_in_ts=now - 20 * DAY),
    ]


# ── 主门禁：事故复刻 ─────────────────────────────────────────────────────────
async def test_new_account_blast_is_blocked_end_to_end(monkeypatch, tmp_path):
    """新号刚接入 → 导入的老会话一条都不许进主动触达候选（.198 事故根因）。

    同时校验老号真实关系**不被误伤**——只减发送不等于全停，两个方向都要钉住。
    """
    now = time.time()
    res = await _preview(monkeypatch, tmp_path, _incident_rows(now))
    assert res["scanned"] == 1, (
        f"期望只剩老号那条真实关系，实际 scanned={res['scanned']}；"
        "新号会话若进了候选，登录后第一个 tick 就会群发（事故复现）")


async def test_upgrade_install_without_config_still_protected(
        monkeypatch, tmp_path):
    """配置里**完全没有** cold_start 节（＝存量升级安装、以及 .198 现场的 overlay）
    时，闸仍然生效。

    这条钉住的是 `test_seed_switch_upgrade_coverage._EXEMPT` 里登记的那个理由：
    我们之所以敢豁免「种子开关的升级可达性」，正是因为代码默认为 True。若哪天有人
    把 `_DEFAULTS` 改成 False 或让 resolve 回落到关闭，那条豁免就变成谎言——本测试
    会先红。（上面几条用例本就不带 cold_start，此处显式命名以便与豁免表互相指认。）
    """
    now = time.time()
    assistant_rows = _incident_rows(now)
    res = await _preview(monkeypatch, tmp_path, assistant_rows, cold_start=None)
    assert res["cold_start"]["enabled"] is True, "缺配置时安全 floor 必须自动生效"
    assert res["scanned"] == 1


async def test_gate_off_reproduces_incident(monkeypatch, tmp_path):
    """探测器有效性自证：关掉冷启动闸 → 三条全部进候选（事故行为复现）。

    没有这条对照，上面那条测试无法排除「其实是别的护栏在拦」的可能——
    那样门禁会在冷启动闸被误删后依然绿着。
    """
    now = time.time()
    res = await _preview(monkeypatch, tmp_path, _incident_rows(now),
                         cold_start={"enabled": False})
    assert res["scanned"] == 3, (
        f"关闸后应放行全部三条（证明拦截确实来自冷启动闸），实际 {res['scanned']}")


async def test_suppression_reasons_are_recorded(monkeypatch, tmp_path):
    """被压下的候选要能被观测到（reason 计数真的在记账，不是空转）。"""
    reset_suppression_stats()
    now = time.time()
    await _preview(monkeypatch, tmp_path, _incident_rows(now))
    snap = suppression_snapshot()
    assert snap.get(COLD_START_WARMING) == 2, f"预期两条新号会话被压，实际 {snap}"
    reset_suppression_stats()


# ── 预热期满之后：判据交棒给「接入后对方开过口没有」──────────────────────────
async def test_after_warmup_imported_only_still_blocked(monkeypatch, tmp_path):
    """账号已过预热窗，但这条会话对方从未在本系统开口 → 仍不冷开场。

    这条正是「导入的通讯录关系」与「真实往来对象」的分界：前者贸然问候＝当场穿帮。
    """
    reset_suppression_stats()
    now = time.time()
    conn = now - 100 * DAY
    rows = [
        # 只有导入历史：对方最后开口早于账号接入
        _mk_row("c-imported", OLD_ACCT, "6002",
                created_at=conn, last_in_ts=conn - 5 * DAY),
        # 真实关系：接入后聊过，现已沉默 20 天 → 正是沉默回访要服务的场景
        _mk_row("c-real", OLD_ACCT, "6003",
                created_at=conn, last_in_ts=now - 20 * DAY),
    ]
    res = await _preview(monkeypatch, tmp_path, rows)
    assert res["scanned"] == 1, f"期望只放行真实关系，实际 {res['scanned']}"
    assert suppression_snapshot().get(IMPORTED_NO_INBOUND) == 1
    reset_suppression_stats()


async def test_require_inbound_off_allows_imported_after_warmup(
        monkeypatch, tmp_path):
    """运营显式关掉「要求接入后开过口」→ 过了预热窗的导入关系放行（配置真的生效）。"""
    now = time.time()
    conn = now - 100 * DAY
    rows = [_mk_row("c-imported", OLD_ACCT, "6002",
                    created_at=conn, last_in_ts=conn - 5 * DAY)]
    res = await _preview(monkeypatch, tmp_path, rows,
                         cold_start={"require_inbound_since_connect": False})
    assert res["scanned"] == 1


# ── 额度熔断：接的是真事实源 ─────────────────────────────────────────────────
def _patch_quota(monkeypatch, exceeded, enforce=False):
    import src.licensing.quota_store as qs
    monkeypatch.setattr(
        qs, "check_license_quota",
        lambda *_a, **_kw: {"allowed": not (exceeded and enforce),
                            "exceeded": exceeded, "enforce": enforce,
                            "used": 180 if exceeded else 10, "included": 100})


async def test_quota_exhausted_stops_autonomous_outreach(monkeypatch, tmp_path):
    """额度用尽（且 enforce 关＝.198 现场组合）→ 连老账号真实关系也停主动外呼。

    这条会话在别的测试里是**放行**的（真实沉默关系），此处唯一变量是额度 →
    证明拦截确实来自额度闸，且它读的是 `exceeded` 而非 `allowed`。
    人工发送不走本路径，不受影响。
    """
    reset_suppression_stats()
    _patch_quota(monkeypatch, exceeded=True, enforce=False)
    now = time.time()
    conn = now - 300 * DAY
    rows = [_mk_row("c-real", OLD_ACCT, "6003",
                    created_at=conn, last_in_ts=now - 20 * DAY)]
    res = await _preview(monkeypatch, tmp_path, rows)
    assert res["scanned"] == 0, "额度用尽时系统不得自主外呼"
    assert suppression_snapshot().get(QUOTA_EXHAUSTED) == 1
    reset_suppression_stats()


async def test_quota_gate_off_keeps_sending(monkeypatch, tmp_path):
    """运营显式关掉额度闸 → 恢复旧行为（配置真的是逃生门，不是摆设）。"""
    _patch_quota(monkeypatch, exceeded=True, enforce=False)
    now = time.time()
    conn = now - 300 * DAY
    rows = [_mk_row("c-real", OLD_ACCT, "6003",
                    created_at=conn, last_in_ts=now - 20 * DAY)]
    res = await _preview(monkeypatch, tmp_path, rows,
                         cold_start={"quota_gate": False})
    assert res["scanned"] == 1


# ── 可观测性：闸的状态必须在面板上读得到 ─────────────────────────────────────
async def test_preview_exposes_cold_start_observability(monkeypatch, tmp_path):
    """预览面板要能回答「本轮压了谁、按什么理由、闸到底生效了没」。

    安全闸最危险的失败形态是**静默失效**——没有这块读数，冷启动闸哪天被误关或接线
    断了，现场只会表现为「过一阵又开始群发」，没人能提前发现。
    """
    reset_suppression_stats()
    now = time.time()
    res = await _preview(monkeypatch, tmp_path, _incident_rows(now))
    cs = res.get("cold_start")
    assert cs is not None, "预览必须带 cold_start 段（安全闸需常驻可观测）"
    assert cs["enabled"] is True
    assert cs["suppressed_this_tick"] == {COLD_START_WARMING: 2}
    assert cs["suppressed_total"].get(COLD_START_WARMING) == 2
    assert cs["config"]["warmup_hours"] == 72.0
    reset_suppression_stats()


async def test_preview_reports_gate_disabled(monkeypatch, tmp_path):
    """闸被关掉时，面板要**显式**说 enabled=false（而不是看起来一切正常）。"""
    now = time.time()
    res = await _preview(monkeypatch, tmp_path, _incident_rows(now),
                         cold_start={"enabled": False})
    assert res["cold_start"]["enabled"] is False
    assert res["cold_start"]["suppressed_this_tick"] == {}


# ── 接线自身的健壮性 ─────────────────────────────────────────────────────────
async def test_gate_failure_is_fail_open_not_silent_stop(monkeypatch, tmp_path):
    """闸内部异常 → 放行（fail-open），绝不把安全闸变成「静默不发」故障源。

    主动外呼漏一轮零代价，但若闸异常导致**永久静默**，那是另一种事故且极难察觉。
    """
    import src.inbox.outbound_gate as _gate

    def _boom(*_a, **_kw):
        raise RuntimeError("模拟闸内部异常")

    monkeypatch.setattr(_gate, "may_contact", _boom)
    now = time.time()
    res = await _preview(monkeypatch, tmp_path, _incident_rows(now))
    assert res["scanned"] == 3, "闸抛异常时必须放行（fail-open），不得静默拦截"
