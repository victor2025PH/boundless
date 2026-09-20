"""G2 封号信号自动急停单测：classify 纯函数 + apply_action 复用 G1 Kill-Switch。"""
import time

import pytest

from src.ops import kill_switch as ks_mod
from src.ops.ban_signal import apply_action, classify, handle_send_exception
from src.ops.kill_switch import KillSwitch


# ── 伪异常（模拟 pyrogram.errors，按类名分类，无需真 pyrogram）──────────────

class FloodWait(Exception):
    def __init__(self, value):
        self.value = value
        super().__init__(f"FloodWait {value}")

class PeerFlood(Exception):
    pass

class UserDeactivatedBan(Exception):
    pass

class Unauthorized(Exception):
    pass

class InputUserDeactivated(Exception):
    """400 INPUT_USER_DEACTIVATED＝**对方**账号注销（对端侧，非我方风控）。"""
    pass


# ── classify ─────────────────────────────────────────────────────────────────

def test_classify_floodwait_is_backoff():
    a = classify(FloodWait(30))
    assert a["kind"] == "backoff" and a["cooldown_sec"] == 30.0

def test_classify_peerflood_is_pause():
    assert classify(PeerFlood())["kind"] == "pause"

def test_classify_ban_signals():
    assert classify(UserDeactivatedBan())["kind"] == "ban"
    assert classify(Unauthorized())["kind"] == "ban"

def test_classify_peer_side_deactivated_is_none():
    """2026-07-27 实锤回归钉：主动触达发到已注销测试号，INPUT_USER_DEACTIVATED
    撞 "DEACTIVAT" 关键词兜底被判 ban → 主账号被永久 kill-switch + 注册表误标
    banned，全线停发 2h。对方注销是对端侧事实，绝不能停我方账号。"""
    # 精确类名（pyrogram.errors.InputUserDeactivated）
    a = classify(InputUserDeactivated())
    assert a["kind"] == "none" and a["reason"].startswith("peer_side:")
    # 泛化异常带错误码消息（真机日志形态：Telegram says: [400 INPUT_USER_DEACTIVATED] …）
    generic = RuntimeError(
        "Telegram says: [400 INPUT_USER_DEACTIVATED] - The target user has been "
        "deleted/deactivated (caused by \"messages.SendMessage\")")
    assert classify(generic)["kind"] == "none"
    # 我方账号注销（401 USER_DEACTIVATED，无 INPUT_ 前缀）仍必须判 ban 不受影响
    assert classify(UserDeactivatedBan())["kind"] == "ban"

def test_classify_unknown_is_none():
    assert classify(ValueError("boom"))["kind"] == "none"

def test_classify_own_control_flow_is_none():
    # 我们自己抛的 gate/kill_switch 异常不能被当成封号信号
    assert classify(RuntimeError("send_gate_blocked:warmup_cap"))["kind"] == "none"
    assert classify(RuntimeError("kill_switch_blocked:global"))["kind"] == "none"


# ── apply_action：pause/ban 落到账号级 Kill-Switch（复用 G1）─────────────────

def test_pause_sets_account_killswitch_with_ttl(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    apply_action("telegram", "42", classify(PeerFlood()),
                 kill_switch=ks, pause_minutes=60)
    blocked, scope, reason = ks.is_blocked("telegram", "42")
    assert blocked is True and scope == "account:telegram:42"
    assert "auto_pause" in reason
    # 其它号不受影响
    assert ks.is_blocked("telegram", "other")[0] is False


def test_pause_auto_recovers_after_ttl(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    t0 = 1000.0
    apply_action("telegram", "42", {"kind": "pause", "cooldown_sec": 30, "reason": "PeerFlood"},
                 kill_switch=ks, now=t0)
    assert ks.is_blocked("telegram", "42", now=t0 + 10)[0] is True
    assert ks.is_blocked("telegram", "42", now=t0 + 31)[0] is False


def test_ban_sets_permanent_and_marks_registry(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")

    class _Reg:
        def __init__(self):
            self.row = {"meta": {}}
            self.upserts = []
        def get(self, p, a):
            return self.row
        def upsert(self, p, a, *, meta=None, **kw):
            self.upserts.append(meta)
            self.row["meta"] = meta

    reg = _Reg()
    alerts = []
    apply_action("telegram", "99", classify(UserDeactivatedBan()),
                 kill_switch=ks, registry=reg,
                 alert=lambda k, p, d: alerts.append((k, p, d)))
    blocked, scope, _ = ks.is_blocked("telegram", "99")
    assert blocked is True and scope == "account:telegram:99"
    assert reg.row["meta"]["banned"] is True
    assert alerts and alerts[0][0] == "account_banned"


def test_backoff_does_not_pause(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    res = apply_action("telegram", "42", classify(FloodWait(20)), kill_switch=ks)
    assert res["applied"] == "backoff"
    assert ks.is_blocked("telegram", "42")[0] is False  # 限速不停号


# ── handle_send_exception：用进程单例，绝不抛 ───────────────────────────────

def test_handle_uses_singleton(tmp_path, monkeypatch):
    ks = KillSwitch(tmp_path / "rf.db")
    monkeypatch.setattr(ks_mod, "_singleton", ks, raising=False)
    out = handle_send_exception("telegram", "7", PeerFlood())
    assert out["kind"] == "pause"
    assert ks.is_blocked("telegram", "7")[0] is True


def test_handle_never_raises_when_killswitch_broken():
    # 注入一个 set 会抛错的 ks → 处置失败也不能掩盖/抛出原始发送错误
    class _BrokenKS:
        def set(self, *a, **k):
            raise RuntimeError("db down")

    out = handle_send_exception("telegram", "7", Unauthorized(), kill_switch=_BrokenKS())
    assert out["applied"] == "error" and out["kind"] == "none"


# ── P0-1（B57+B59）auth 族登录变更窗口降档 + 登录成功自动解冻 ─────────────────

import src.ops.ban_signal as bs


class SessionRevoked(Exception):
    pass


class _Reg:
    def __init__(self, meta=None):
        self.meta = dict(meta or {})
        self.upserts = []

    def get(self, p, a):
        return {"platform": p, "account_id": a, "meta": dict(self.meta)}

    def upsert(self, p, a, *, meta=None, merge_meta=False, **kw):
        self.upserts.append(meta)
        if merge_meta:
            self.meta.update(meta or {})
        else:
            self.meta = dict(meta or {})


@pytest.fixture(autouse=True)
def _clean_flux_state(monkeypatch):
    """每例独立：清登录变更表 + 把进程启动时间推到远古（默认不在 boot 窗口）。"""
    monkeypatch.setattr(bs, "_login_changes", {}, raising=False)
    monkeypatch.setattr(bs, "_BOOT_TS", 0.0, raising=False)
    yield


def test_is_auth_family_pure():
    assert bs.is_auth_family("SessionRevoked")
    assert bs.is_auth_family("auto_ban:AuthKeyDuplicated")
    assert bs.is_auth_family("Unauthorized")
    assert bs.is_auth_family("SESSION_EXPIRED")
    assert not bs.is_auth_family("UserDeactivatedBan")
    assert not bs.is_auth_family("PeerFlood")
    assert not bs.is_auth_family("")


def test_auth_error_in_login_window_downgrades_to_pause(tmp_path):
    """重登后 N 分钟内的 auth 族错误＝旧会话残响 → pause+TTL，绝不永久 ban。"""
    ks = KillSwitch(tmp_path / "rf.db")
    reg = _Reg()
    t0 = 1_000_000.0
    bs.note_login_change("telegram", "42", now=t0)
    out = handle_send_exception("telegram", "42", SessionRevoked(),
                                kill_switch=ks, registry=reg, now=t0 + 60)
    assert out["kind"] == "pause"
    assert out["reason"].startswith("auth_flux:")
    rec = ks.blocking_record("telegram", "42", now=t0 + 61)
    assert rec is not None and float(rec["expires_at"]) > 0  # TTL 自动恢复，非永久
    assert reg.meta.get("banned") is not True  # 不标 meta.banned


def test_auth_error_outside_window_still_bans(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    reg = _Reg()
    t0 = 1_000_000.0
    bs.note_login_change("telegram", "42", now=t0)
    out = handle_send_exception(
        "telegram", "42", SessionRevoked(), kill_switch=ks, registry=reg,
        now=t0 + bs.DEFAULT_LOGIN_FLUX_WINDOW_MIN * 60 + 5)
    assert out["kind"] == "ban"
    rec = ks.blocking_record("telegram", "42")
    assert rec is not None and float(rec["expires_at"]) == 0  # 永久
    assert reg.meta.get("banned") is True


def test_boot_window_downgrades_auth_errors(tmp_path, monkeypatch):
    """进程刚启动（升级/重启）＝全体客户端重新鉴权，auth 错误降档不判 ban。"""
    ks = KillSwitch(tmp_path / "rf.db")
    t0 = 2_000_000.0
    monkeypatch.setattr(bs, "_BOOT_TS", t0 - 30, raising=False)
    out = handle_send_exception("telegram", "7", Unauthorized(),
                                kill_switch=ks, now=t0)
    assert out["kind"] == "pause" and out["reason"].startswith("auth_flux:")


def test_real_ban_not_downgraded_even_in_window(tmp_path):
    """UserDeactivated* 是真封禁信号，不属 auth 族——窗口内照旧永久处置。"""
    ks = KillSwitch(tmp_path / "rf.db")
    t0 = 1_000_000.0
    bs.note_login_change("telegram", "42", now=t0)
    out = handle_send_exception("telegram", "42", UserDeactivatedBan(),
                                kill_switch=ks, now=t0 + 10)
    assert out["kind"] == "ban"
    rec = ks.blocking_record("telegram", "42")
    assert rec is not None and float(rec["expires_at"]) == 0


def test_flux_window_mocked_past_now_not_in_boot_window(monkeypatch):
    """mock 小时钟（now < _BOOT_TS）不得误判在 boot 窗口（负差防护）。"""
    monkeypatch.setattr(bs, "_BOOT_TS", 5_000_000.0, raising=False)
    assert bs.in_login_flux_window("telegram", "1", now=1000.0) is False


def test_clear_auth_ban_on_login_clears_auth_family(tmp_path):
    """登录成功 → auth 族 auto_ban 解除 + meta.banned 清除 + 开启降档窗口。"""
    ks = KillSwitch(tmp_path / "rf.db")
    reg = _Reg(meta={"banned": True, "ban_reason": "SessionRevoked"})
    ks.set("account:telegram:42", reason="auto_ban:SessionRevoked",
           actor="ban_signal", ttl_sec=0)
    out = bs.clear_auth_ban_on_login("telegram", "42",
                                     kill_switch=ks, registry=reg)
    assert out["cleared"] is True and out["meta_cleared"] is True
    assert ks.is_blocked("telegram", "42")[0] is False
    assert reg.meta.get("banned") is False
    assert bs.in_login_flux_window("telegram", "42") is True  # 窗口已开


def test_clear_auth_ban_on_login_keeps_manual_and_real_ban(tmp_path):
    """手动冻结与真封禁（非 auth 族）登录后**不**自动解除。"""
    ks = KillSwitch(tmp_path / "rf.db")
    ks.set("account:telegram:50", reason="排查中勿动", actor="ops", ttl_sec=0)
    out = bs.clear_auth_ban_on_login("telegram", "50", kill_switch=ks,
                                     registry=_Reg())
    assert out["cleared"] is False
    assert ks.is_blocked("telegram", "50")[0] is True

    ks.set("account:telegram:51", reason="auto_ban:UserDeactivatedBan",
           actor="ban_signal", ttl_sec=0)
    reg = _Reg(meta={"banned": True, "ban_reason": "UserDeactivatedBan"})
    out = bs.clear_auth_ban_on_login("telegram", "51", kill_switch=ks,
                                     registry=reg)
    assert out["cleared"] is False and out["meta_cleared"] is False
    assert ks.is_blocked("telegram", "51")[0] is True
    assert reg.meta.get("banned") is True


def test_clear_auth_ban_on_login_leaves_global_scope(tmp_path):
    """账号登录只解自己账号级作用域，global/platform 冻结绝不动。"""
    ks = KillSwitch(tmp_path / "rf.db")
    ks.set("global", reason="auto_ban:SessionRevoked", actor="ban_signal",
           ttl_sec=0)
    out = bs.clear_auth_ban_on_login("telegram", "42", kill_switch=ks,
                                     registry=_Reg())
    assert out["cleared"] is False
    assert ks.is_blocked("telegram", "42")[0] is True  # global 仍拦


def test_clear_auth_ban_never_raises():
    class _Boom:
        def status(self, **kw):
            raise RuntimeError("db down")

    out = bs.clear_auth_ban_on_login("telegram", "42", kill_switch=_Boom(),
                                     registry=None)
    assert out["cleared"] is False
