"""试用履约链门禁（P2）：payload 口径 + 签发/验签/绑机端到端，全程离线。

守的是三条一旦漂移就直接变成商业事故的不变量：

  1. **7 天不能变 14 天** —— license 的 grace 状态同样算 licensed，默认宽限 7 天。
     试用 payload 必须显式 grace_days=0，否则白送一倍时长。
  2. **必须绑机** —— 不绑机的试用等于可无限转发，整条注册领取链失去意义。
  3. **加量 ref 必须确定性** —— 签完凭证回填失败会重签，ref 一旦带时间戳，
     一次网络抖动就等于多送一份额度。

这些都不需要官网/网络：直接用临时钥匙对签发再交给真正的客户端验签器 LicenseManager，
把「厂商机怎么签」和「客户端怎么认」钉在同一个断言里。
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from src.licensing.chatx_fulfillment import TRIAL_GIFT_CHARS, TRIAL_SPEC, build_trial_payload
from src.licensing.license_manager import LicenseManager, generate_keypair, issue_license
from src.licensing.topup_voucher import issue_topup_voucher, verify_topup_voucher

_SPEC = importlib.util.spec_from_file_location(
    "_fulfill_trial", Path(__file__).resolve().parent.parent / "scripts" / "fulfill_trial.py")
_ft = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
_SPEC.loader.exec_module(_ft)  # type: ignore[union-attr]


@pytest.fixture(scope="module")
def keys():
    return generate_keypair()


def _local_fp() -> str:
    from src.licensing.machine_bridge import machine_fingerprint
    return machine_fingerprint()


# ── payload 口径 ───────────────────────────────────────────────────────────

def test_trial_payload_shape():
    p = build_trial_payload(customer="@bob", machine="a1b2-c3d4-e5f6-0789",
                            claim_id="tc_1", now=1_000_000)
    assert p["plan"] == TRIAL_SPEC["plan"]
    assert p["machine"] == "A1B2-C3D4-E5F6-0789", "指纹须归一化为大写"
    assert p["lic_id"] == "trial-A1B2C3D4", "lic_id 取指纹短码，一机一份额度池"
    assert p["trial"] is True
    assert p["included_chars"] == TRIAL_SPEC["included_chars"]
    assert "exp" not in p, "免费档无期限：days=0 不写 exp（用完即止，不按时间过期）"


def test_free_tier_spec_is_pinned():
    """商业口径钉死（2026-08-11 运营拍板）：免费档 = 100 万字符 · 无期限。

    改这两个数字 = 改对外承诺（官网文案/首启向导/销售话术白名单全都要联动），
    不该在一次顺手的重构里静默漂移。
    """
    assert TRIAL_SPEC["included_chars"] == 1_000_000
    assert TRIAL_SPEC["days"] == 0
    # 显式 days 覆盖仍可用（私有化部署要按天试用时的逃生口）
    p = build_trial_payload(customer="@bob", machine="A1B2-C3D4-E5F6-0789",
                            days=7, now=1_000_000)
    assert (p["exp"] - 1_000_000) // 86400 == 7


def test_trial_payload_requires_machine():
    with pytest.raises(ValueError):
        build_trial_payload(customer="@bob", machine="   ")


def test_grace_must_be_zero_so_7_days_is_not_14(keys):
    """回归钉：默认 grace_days=7 且 grace 算 licensed —— 试用必须显式清零。"""
    p = build_trial_payload(customer="@bob", machine="A1B2-C3D4-E5F6-0789")
    assert p["grace_days"] == 0

    # 造一张「昨天就到期」的试用：必须直接 expired，绝不能落进 grace 继续可用。
    expired = dict(p, exp=int(time.time()) - 86400, machine="*")
    st = LicenseManager(license_token=issue_license(expired, keys["private_hex"]),
                        public_key_hex=keys["public_hex"]).status()
    assert st.state == "expired", f"过期试用不得进宽限期（实得 {st.state}）"
    assert not st.licensed


# ── 签发 → 客户端验签 → 绑机 ───────────────────────────────────────────────

def test_issued_trial_verifies_active_on_bound_machine(keys):
    fp = _local_fp()
    if not fp:
        pytest.skip("本机指纹不可用（platform/licensing 缺失）")
    p = build_trial_payload(customer="@alice", machine=fp, claim_id="tc_ok")
    st = LicenseManager(license_token=issue_license(p, keys["private_hex"]),
                        public_key_hex=keys["public_hex"]).status()
    assert st.state == "active"
    assert st.plan == TRIAL_SPEC["plan"]
    assert st.days_left is None, "无期限免费档没有剩余天数"
    assert st.included_chars == TRIAL_SPEC["included_chars"]


def test_trial_bound_to_other_machine_is_invalid(keys):
    """同一把厂商钥匙签的授权，绑到别的机器上必须验不过——否则试用可无限转发。"""
    if not _local_fp():
        pytest.skip("本机指纹不可用")
    p = build_trial_payload(customer="@mallory", machine="1111-2222-3333-4444")
    st = LicenseManager(license_token=issue_license(p, keys["private_hex"]),
                        public_key_hex=keys["public_hex"]).status()
    assert st.state == "invalid"
    assert not st.licensed


def test_wildcard_machine_still_works(keys):
    """``machine="*"`` 是不绑机逃生口（内部演示/客服代跑），须仍然可用。"""
    p = build_trial_payload(customer="@demo", machine="*")
    st = LicenseManager(license_token=issue_license(p, keys["private_hex"]),
                        public_key_hex=keys["public_hex"]).status()
    assert st.state == "active"


# ── 加量凭证 ───────────────────────────────────────────────────────────────

def test_topup_ref_is_deterministic_per_claim(keys):
    ref = _ft.topup_ref("tc_abc")
    assert ref == _ft.topup_ref("tc_abc"), "重签必须落在同一个兑换幂等键上"
    assert "tc_abc" in ref
    toks = [issue_topup_voucher(keys["private_hex"], chars=TRIAL_GIFT_CHARS, ref=ref,
                                lic_id="trial-A1B2C3D4") for _ in range(2)]
    payloads = [verify_topup_voucher(t, keys["public_hex"])["payload"] for t in toks]
    assert {p["ref"] for p in payloads} == {ref}, "两次签发的 ref 必须一致（客户端只入账一次）"
    assert all(p["chars"] == TRIAL_GIFT_CHARS for p in payloads)


def test_gift_chars_prefers_service_recorded_amount():
    assert _ft.gift_chars_for({}) == TRIAL_GIFT_CHARS
    assert _ft.gift_chars_for({"bind_chars": 50_000}) == 50_000
    assert _ft.gift_chars_for({"bind_chars": 0}) == TRIAL_GIFT_CHARS


# ── 队列纯逻辑 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fp,ok", [
    ("A1B2-C3D4-E5F6-0789", True),
    ("a1b2-c3d4-e5f6-0789", True),
    ("", False),
    ("ZZZZ-1111-2222-3333", False),
    ("A1B2C3D4E5F60789", False),
    ("A1B2-C3D4-E5F6", False),
])
def test_fingerprint_validation(fp, ok):
    assert _ft.valid_fingerprint(fp) is ok


def test_plan_license_work_partitions_and_skips_done():
    ok, bad = _ft.plan_license_work([
        {"id": "1", "fingerprint": "A1B2-C3D4-E5F6-0789"},
        {"id": "2", "fingerprint": "garbage"},
        {"id": "3", "fingerprint": "A1B2-C3D4-E5F6-0789", "has_license": True},
    ])
    assert [c["id"] for c in ok] == ["1"]
    # 脏指纹必须进 rejected 堆：留在 pending 里会每轮重试、永远堵在队头。
    assert [c["id"] for c in bad] == ["2"]


# ── 存量升级重签（免费档 25k/7天 → 100 万/无期限）───────────────────────────

def test_plan_upgrade_work_selects_only_stale_issued():
    fp = "A1B2-C3D4-E5F6-0789"
    base = {"status": "issued", "has_license": True, "fingerprint": fp,
            "product": "chatx"}
    rows = [
        # 旧 25k 单 → 该升
        dict(base, id="old_chars", license_chars=25_000, license_exp=0),
        # 旧 7 天单（额度已够但带期限，而当前规格无期限）→ 该升
        dict(base, id="old_exp", license_chars=1_000_000,
             license_exp=1_800_000_000),
        # 已是新规格 → 不动（幂等的关键）
        dict(base, id="fresh", license_chars=1_000_000, license_exp=0),
        # 官网旧版没给解码字段 → 信息不足不盲签
        dict(base, id="no_meta", license_chars=None, license_exp=None),
        # 其它产品线 / 非 issued / 脏指纹 / 没授权在身 → 都不动
        dict(base, id="other_product", product="lingox",
             license_chars=25_000, license_exp=0),
        dict(base, id="still_pending", status="pending",
             license_chars=25_000, license_exp=0),
        dict(base, id="bad_fp", fingerprint="garbage",
             license_chars=25_000, license_exp=0),
        dict(base, id="no_license", has_license=False,
             license_chars=25_000, license_exp=0),
    ]
    picked = [c["id"] for c in _ft.plan_upgrade_work(rows)]
    assert picked == ["old_chars", "old_exp"]


def test_plan_upgrade_work_respects_explicit_spec():
    """spec 可注入（--days/--chars 覆盖场景）：按天规格下旧 exp 单不算过期口径。"""
    fp = "A1B2-C3D4-E5F6-0789"
    row = {"id": "x", "status": "issued", "has_license": True, "fingerprint": fp,
           "product": "chatx", "license_chars": 25_000, "license_exp": 1_800_000_000}
    # 目标规格 25k/7 天（与现状一致）→ 不升
    assert _ft.plan_upgrade_work([row], spec_chars=25_000, spec_days=7) == []
    # 目标规格 100 万/无期限 → 升
    assert [c["id"] for c in _ft.plan_upgrade_work(
        [row], spec_chars=1_000_000, spec_days=0)] == ["x"]


# ── 邀请发奖凭证 ref（幂等键）───────────────────────────────────────────────

def test_referral_refs_are_deterministic():
    wel1, earn1 = _ft.referral_refs("r_abc")
    wel2, earn2 = _ft.referral_refs("r_abc")
    assert (wel1, earn1) == (wel2, earn2), "重签必须落在同一个兑换幂等键上"
    assert wel1 != earn1, "见面礼与邀请奖励是两笔账，ref 不能撞"
    assert "r_abc" in wel1 and "r_abc" in earn1


def test_backlog_age_surfaces_stalled_fulfiller():
    now = 1_700_000_000.0
    import datetime as dt
    old = dt.datetime.fromtimestamp(now - 3600, dt.timezone.utc).isoformat()
    fresh = dt.datetime.fromtimestamp(now - 60, dt.timezone.utc).isoformat()
    assert _ft.oldest_pending_minutes([{"created_at": fresh}, {"created_at": old}], now=now) == 60
    assert _ft.oldest_pending_minutes([], now=now) == 0
    assert _ft.oldest_pending_minutes([{"created_at": "not-a-date"}], now=now) == 0
