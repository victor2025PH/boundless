"""机器码 + 授权绑机门禁（P2）。

绑机的用途是**去重与归属**（服务端判断"这台机器领过试用没有"），不是防破解——指纹
取自本机可读信息，客户端侧一定可伪造。把它当安全边界会得出错误的安全感，因此本门禁
钉的是三条工程不变量：

1. **指纹稳定且不可逆**：同机多次调用一致、输出里不含原始 GUID/MAC；
2. **salt 命名空间**：换 salt 得到不同指纹（各产品线互不交叉关联，且能复刻历史 salt
   以兼容存量授权）；
3. **判定不出来必须放行**：模块缺失/异常时绑机校验放行。反过来做会把正当付费用户锁在
   门外，而它本来只是防滥用。
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from src.licensing import LicenseManager, generate_keypair, issue_license
from src.licensing import machine_bridge as mb

_MACHINE_ID_PATH = (Path(__file__).resolve().parents[3]
                    / "platform" / "licensing" / "machine_id.py")


@pytest.fixture(scope="module")
def mid():
    if not _MACHINE_ID_PATH.is_file():
        pytest.skip(f"platform 瘦模块不存在: {_MACHINE_ID_PATH}")
    spec = importlib.util.spec_from_file_location("_t_machine_id", str(_MACHINE_ID_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _reset_bridge():
    mb.reset_machine_bridge()
    yield
    mb.reset_machine_bridge()


# ── 指纹本身 ──────────────────────────────────────────────────────────────

def test_fingerprint_shape_and_stability(mid):
    a = mid.machine_fingerprint()
    b = mid.machine_fingerprint()
    assert a == b, "同机两次调用必须一致"
    parts = a.split("-")
    assert len(parts) == 4 and all(len(p) == 4 for p in parts)
    assert all(c in "0123456789ABCDEF" for c in a.replace("-", ""))


def test_fingerprint_is_not_reversible(mid):
    """对外只给派生值：日志/工单里出现的不能是硬件序列号明文。"""
    raw = mid._raw_legacy_id()
    fp = mid.machine_fingerprint()
    assert raw not in fp
    for chunk in raw.replace("|", "").split():
        if len(chunk) > 6:
            assert chunk.upper() not in fp


def test_salt_namespaces_the_fingerprint(mid):
    """不同产品线各自命名空间；也让「传入历史 salt」能复刻存量授权的指纹。"""
    assert mid.machine_fingerprint(salt="a") != mid.machine_fingerprint(salt="b")
    assert (mid.machine_fingerprint(salt="avatarhub-fp-v1")
            != mid.machine_fingerprint(salt=mid.DEFAULT_SALT))


def test_explicit_env_anchor_wins(mid, monkeypatch):
    """运维给虚机/容器固定指纹的通道。"""
    monkeypatch.setenv("BD_TEST_MID", "fixed-anchor-1")
    a = mid.machine_fingerprint(env_vars=("BD_TEST_MID",))
    monkeypatch.setenv("BD_TEST_MID", "fixed-anchor-2")
    b = mid.machine_fingerprint(env_vars=("BD_TEST_MID",))
    assert a != b
    monkeypatch.setenv("BD_TEST_MID", "fixed-anchor-1")
    assert mid.machine_fingerprint(env_vars=("BD_TEST_MID",)) == a


def test_empty_env_vars_means_disabled_not_default(mid, monkeypatch):
    """``env_vars=()`` 必须是「禁用 env 覆盖」，不能回落到默认名。

    原实现写 ``tuple(env_vars) if env_vars else DEFAULT``，空元组是假值 → 回落默认，
    于是调用方想收紧反而把覆盖打开了。绑机绕过口的修复正是靠这个三态语义。
    """
    monkeypatch.setenv("BOUNDLESS_MACHINE_ID", "attacker-picked-constant")
    hijacked = mid.machine_fingerprint()                 # None → 用默认名，会被劫持
    disabled = mid.machine_fingerprint(env_vars=())      # 空 → 禁用，回到真硬件锚点
    assert hijacked != disabled, "空元组没能禁用 env 覆盖"
    monkeypatch.setenv("BOUNDLESS_MACHINE_ID", "another-constant")
    assert mid.machine_fingerprint(env_vars=()) == disabled, "禁用后不该再随 env 变"


def test_packaged_build_ignores_env_override(monkeypatch):
    """打包态必须无视 env 指纹覆盖——否则绑机零门槛可绕。

    威胁具体是：发布出去的 backend.exe 里这两个 env 同样生效，启动前设个固定值，
    一份签好的试用授权就能在任意机器复用，官网按机器码去重也一并失效。
    """
    import sys

    from src.licensing import machine_bridge as mb

    monkeypatch.setenv("BOUNDLESS_MACHINE_ID", "attacker-picked-constant")
    mb.reset_machine_bridge()
    src_fp = mb.machine_fingerprint()                    # 源码态：覆盖生效

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    mb.reset_machine_bridge()
    frozen_fp = mb.machine_fingerprint()                 # 打包态：必须忽略

    assert src_fp and frozen_fp
    assert frozen_fp != src_fp, "打包态仍在认 env 覆盖 —— 绑机可被绕过"

    monkeypatch.setenv("BOUNDLESS_MACHINE_ID", "yet-another-constant")
    mb.reset_machine_bridge()
    assert mb.machine_fingerprint() == frozen_fp, "打包态指纹不该随 env 变"
    mb.reset_machine_bridge()


def test_source_build_keeps_override_for_selftest(monkeypatch):
    """源码态保留覆盖：自测与演练台靠它隔离，而能跑源码的人本就能改代码。"""
    from src.licensing import machine_bridge as mb

    monkeypatch.setenv("BOUNDLESS_MACHINE_ID", "selftest-aaa")
    mb.reset_machine_bridge()
    a = mb.machine_fingerprint()
    monkeypatch.setenv("BOUNDLESS_MACHINE_ID", "selftest-bbb")
    mb.reset_machine_bridge()
    assert mb.machine_fingerprint() != a
    mb.reset_machine_bridge()


def test_packaged_binding_check_also_ignores_env(monkeypatch):
    """绑机**校验**这一侧同样要忽略——只堵取指纹不堵校验等于没堵。"""
    import sys

    from src.licensing import machine_bridge as mb

    monkeypatch.delenv("BOUNDLESS_MACHINE_ID", raising=False)
    mb.reset_machine_bridge()
    real = mb.machine_fingerprint()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("BOUNDLESS_MACHINE_ID", "attacker-picked-constant")
    mb.reset_machine_bridge()
    # 攻击者用自选常量签的授权，在打包态必须匹配不上
    from src.licensing.machine_bridge import machine_fingerprint as _fp
    forged = _fp()
    assert mb.machine_matches(real) is True, "真硬件指纹应仍然匹配"
    assert forged == real, "打包态取到的就该是真硬件指纹"
    mb.reset_machine_bridge()


def test_candidates_include_primary_and_legacy(mid):
    cands = mid.machine_fingerprints()
    assert cands and cands[0] == mid.machine_fingerprint()
    assert len(cands) == len(set(cands)), "候选集必须去重"


def test_matches_semantics(mid):
    fp = mid.machine_fingerprint()
    assert mid.matches_this_machine(fp) is True
    assert mid.matches_this_machine("") is True, "空 = 未绑机"
    assert mid.matches_this_machine("*") is True, "站点授权不限机器"
    assert mid.matches_this_machine("DEAD-BEEF-DEAD-BEEF") is False


def test_short_code(mid):
    fp = "ABCD-1234-EF56-7890"
    assert mid.short(fp) == "ABCD1234"
    assert mid.short(fp, groups=1) == "ABCD"


# ── 引擎侧桥 ──────────────────────────────────────────────────────────────

def test_bridge_resolves_fingerprint():
    fp = mb.machine_fingerprint()
    assert fp and len(fp.split("-")) == 4
    assert mb.machine_matches(fp) is True
    assert mb.machine_matches("DEAD-BEEF-DEAD-BEEF") is False
    assert mb.machine_short() != "unknown"


def test_bridge_returns_none_when_module_missing(monkeypatch):
    """模块找不到时必须回 None（= 调用方放行），不能抛也不能瞎猜。"""
    monkeypatch.setattr(mb, "_path_candidates", lambda: [Path("/nope/machine_id.py")])
    mb.reset_machine_bridge()
    assert mb.machine_matches("DEAD-BEEF-DEAD-BEEF") is None
    assert mb.machine_fingerprint() == ""
    assert mb.machine_short() == "unknown"
    # 空/通配仍应直接判定，不依赖模块
    assert mb.machine_matches("") is True
    assert mb.machine_matches("*") is True


# ── 授权绑机 ──────────────────────────────────────────────────────────────

@pytest.fixture()
def keypair():
    return generate_keypair()


def _status(payload, keypair):
    token = issue_license(payload, keypair["private_hex"])
    return LicenseManager(license_token=token,
                          public_key_hex=keypair["public_hex"]).status()


def _base(**kw):
    out = {"sub": "T", "plan": "basic", "exp": int(time.time()) + 7 * 86400,
           "included_chars": 25_000, "trial": True}
    out.update(kw)
    return out


def test_license_without_machine_field_still_works(keypair):
    """存量授权没有 machine 字段 —— 行为必须逐字节不变。"""
    assert _status(_base(), keypair).state == "active"


def test_license_bound_to_this_machine_is_active(keypair):
    fp = mb.machine_fingerprint()
    assert fp, "本机取不到指纹，用例前提失效"
    st = _status(_base(machine=fp), keypair)
    assert st.state == "active" and st.licensed is True


def test_license_bound_to_another_machine_is_invalid(keypair):
    st = _status(_base(machine="DEAD-BEEF-DEAD-BEEF"), keypair)
    assert st.state == "invalid"
    assert any("另一台机器" in m for m in st.messages)
    assert st.licensed is False


def test_site_wildcard_binding_is_accepted(keypair):
    assert _status(_base(machine="*"), keypair).state == "active"


def test_binding_check_fails_open_when_bridge_unavailable(keypair, monkeypatch):
    """判定不出来就放行——绑机是防滥用，不能把正当客户锁死。"""
    monkeypatch.setattr("src.licensing.machine_bridge.machine_matches",
                        lambda _b: None)
    st = _status(_base(machine="DEAD-BEEF-DEAD-BEEF"), keypair)
    assert st.state == "active", "无法判定时必须放行"


def test_machine_binding_does_not_shadow_expiry(keypair):
    """绑机通过但已过期 → 仍走过期语义，两条判定不能互相吞掉。"""
    fp = mb.machine_fingerprint()
    st = _status(_base(machine=fp, exp=int(time.time()) - 400 * 86400), keypair)
    assert st.state == "expired"
