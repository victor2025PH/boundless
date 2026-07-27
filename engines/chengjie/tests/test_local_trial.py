"""首启体验档门禁（P2：装完直接能用，不要求注册）。

这套机制的风险不在「算得对不对」，在**边界**：
- 它绝不能碰 `license.key`（本地签发＝把厂商私钥交出去）；
- 它绝不能在「有正式授权」时插手额度口径（付费客户凭空多出一条用量）；
- 它的 enforce 必须独立于 `licensing.enforce`（否则开了 enforce 的存量客户
  会顺带把新装用户一起锁死）；
- 把系统时间往回拨不能延长窗口；
- 注册换正式试用后必须**永久**关闭，删 license.key 也不能复活。

以上每条都在下面有对应用例。
"""
from __future__ import annotations

import json
import time

import pytest

from src.licensing import local_trial as lt
from src.licensing import quota_store as qs


@pytest.fixture(autouse=True)
def _isolate():
    lt.reset_local_trial()
    qs.reset_license_quota_store()
    yield
    lt.reset_local_trial()
    qs.reset_license_quota_store()


def _state(**kw) -> lt.LocalTrialState:
    base = dict(first_seen=1000.0, last_seen=1000.0, machine="ab12cd34",
                chars=10_000, window_hours=48.0, closed=False)
    base.update(kw)
    return lt.LocalTrialState(**base)


# ── 纯函数：窗口 / 用量 / 时钟 ──────────────────────────────────────────────

def test_fresh_trial_is_active():
    d = lt.evaluate(_state(), now=1000.0, used_chars=0)
    assert d["active"] is True and d["expired"] is False
    assert d["chars_left"] == 10_000 and d["hours_left"] == 48.0


def test_window_expiry():
    d = lt.evaluate(_state(), now=1000.0 + 48 * 3600 + 1, used_chars=0)
    assert d["expired"] is True and d["active"] is False
    assert d["seconds_left"] == 0


def test_chars_exhausted():
    d = lt.evaluate(_state(), now=1000.0, used_chars=10_000)
    assert d["exhausted"] is True and d["active"] is False and d["chars_left"] == 0


def test_clock_rollback_does_not_extend_the_window():
    """把系统时间往回拨一年，窗口不能重开——否则赠量变成无限量。"""
    st = _state(last_seen=1000.0 + 40 * 3600)      # 已见过第 40 小时
    d = lt.evaluate(st, now=1000.0)                 # 时间被拨回起点
    assert d["tampered"] is True
    assert d["effective_now"] == pytest.approx(st.last_seen)
    assert d["hours_left"] == pytest.approx(8.0)    # 仍按已过 40 小时算


def test_small_clock_jitter_is_not_treated_as_tampering():
    """NTP 校正/休眠唤醒会有秒级回跳，不该当作作弊。"""
    st = _state(last_seen=1000.0)
    assert lt.evaluate(st, now=1000.0 - 60, used_chars=0)["tampered"] is False


def test_closed_trial_never_active():
    d = lt.evaluate(_state(closed=True), now=1000.0, used_chars=0)
    assert d["closed"] is True and d["active"] is False


def test_not_started_is_not_expired():
    """还没落锚点（首启向导没跑）不等于过期。"""
    d = lt.evaluate(_state(first_seen=0.0, last_seen=0.0), now=9e9)
    assert d["expired"] is False and d["active"] is False


# ── 状态文件：落锚点 / 单调水位 / 永久关闭 ──────────────────────────────────

def test_begin_anchors_once_and_is_idempotent(tmp_path):
    t = lt.LocalTrial(str(tmp_path / "local_trial.json"), machine_short="ab12cd34")
    s1 = t.begin(now=5000.0)
    s2 = t.begin(now=9000.0)
    assert s1.first_seen == 5000.0
    assert s2.first_seen == 5000.0, "第二次调用不能重开窗口"
    assert s2.last_seen == 9000.0


def test_touch_is_monotonic(tmp_path):
    t = lt.LocalTrial(str(tmp_path / "local_trial.json"), machine_short="x")
    t.begin(now=5000.0)
    t.touch(now=8000.0)
    t.touch(now=1000.0)            # 回拨
    assert t.state().last_seen == 8000.0


def test_touch_is_throttled_off_the_hot_path(tmp_path):
    """touch() 挂在每条翻译的记账路径上——不节流就是每句话写一次 JSON。"""
    p = tmp_path / "local_trial.json"
    t = lt.LocalTrial(str(p), machine_short="x")
    t.begin(now=5000.0)
    t.touch(now=5010.0)            # 只前进 10s < 300s 节流窗
    assert lt.LocalTrial(str(p), machine_short="x").state().last_seen == 5000.0, \
        "小步前进不该落盘"
    t.touch(now=5400.0)            # 前进 400s > 节流窗
    assert lt.LocalTrial(str(p), machine_short="x").state().last_seen == 5400.0


def test_state_read_is_cached(tmp_path, monkeypatch):
    """额度检查在热路上，不能每条消息读一次盘。"""
    p = tmp_path / "local_trial.json"
    t = lt.LocalTrial(str(p), machine_short="x")
    t.begin(now=5000.0)
    reads = {"n": 0}
    real_open = open

    def _counting_open(path, *a, **k):
        if str(path) == str(p):
            reads["n"] += 1
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _counting_open)
    for _ in range(20):
        t.state()
    assert reads["n"] == 0, "TTL 内应全部命中内存缓存"


def test_close_is_permanent_and_survives_reload(tmp_path):
    p = tmp_path / "local_trial.json"
    t = lt.LocalTrial(str(p), machine_short="x")
    t.begin(now=5000.0)
    t.close("registered")
    assert lt.LocalTrial(str(p), machine_short="x").state().closed is True
    # 关闭后再 begin 也不能复活（否则"注册完再删 license.key"就能循环白嫖）
    t.begin(now=6000.0)
    assert t.state().closed is True


def test_corrupt_state_file_degrades_to_uninitialised(tmp_path):
    p = tmp_path / "local_trial.json"
    p.write_text("{ this is not json", encoding="utf-8")
    st = lt.LocalTrial(str(p), machine_short="x").state()
    assert st.first_seen == 0.0 and st.closed is False


def test_state_file_is_atomic_json(tmp_path):
    p = tmp_path / "local_trial.json"
    lt.LocalTrial(str(p), machine_short="ab12cd34").begin(now=5000.0)
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["first_seen"] == 5000.0 and d["machine"] == "ab12cd34"
    assert not list(tmp_path.glob("*.tmp")), "临时文件必须被 replace 掉"


# ── lic_id 隔离 ────────────────────────────────────────────────────────────

def test_lic_id_is_namespaced():
    assert lt.lic_id_for("ab12cd34") == "local:ab12cd34"
    assert lt.is_local_lic_id("local:x") and not lt.is_local_lic_id("chatx-team-001")


def test_lic_id_falls_back_when_machine_unknown():
    assert lt.lic_id_for("") == "local:unknown"


# ── 配置装配 ──────────────────────────────────────────────────────────────

def test_disabled_by_default():
    """新子系统惯例：默认关。桌面随包种子里才打开。"""
    assert lt.configure_local_trial({}) is None
    assert lt.get_local_trial() is None
    assert lt.local_trial_enforced() is False


def test_enforce_is_independent_of_global_enforce():
    """共用 licensing.enforce 会让开了它的存量客户顺带锁死新装用户。"""
    cfg = {"licensing": {"enforce": True, "trial": {"enabled": True}}}
    lt.configure_local_trial(cfg, trial=object())
    assert lt.local_trial_enforced() is False, "全局 enforce 不该传染到体验档"
    cfg["licensing"]["trial"]["enforce"] = True
    lt.configure_local_trial(cfg, trial=object())
    assert lt.local_trial_enforced() is True


# ── 与额度闸门的接线 ───────────────────────────────────────────────────────

class _FakeStatus:
    """最小 license status 替身（quota_store 只读这几个属性）。"""

    def __init__(self, *, licensed=False, included=0, lic_id="", state="unlicensed",
                 enforce=False):
        self.licensed = licensed
        self.included_chars = included
        self.lic_id = lic_id
        self.state = state
        self.enforce = enforce


def _wire(tmp_path, *, enabled=True, enforce=False, chars=10_000, window=48.0):
    qs.configure_license_quota_store(db_path=str(tmp_path / "q.db"))
    t = lt.LocalTrial(str(tmp_path / "local_trial.json"), chars=chars,
                      window_hours=window, machine_short="ab12cd34")
    lt.configure_local_trial(
        {"licensing": {"trial": {"enabled": enabled, "enforce": enforce}}}, trial=t)
    return t


def test_quota_check_uses_trial_when_unlicensed(tmp_path):
    t = _wire(tmp_path)
    t.begin(now=time.time())
    out = qs.check_license_quota(lic_status=_FakeStatus())
    assert out["source"] == "local_trial"
    assert out["lic_id"] == "local:ab12cd34"
    assert out["included"] == 10_000 and out["remaining"] == 10_000
    assert out["allowed"] is True


def test_trial_not_started_leaves_quota_untouched(tmp_path):
    _wire(tmp_path)                      # 未 begin
    out = qs.check_license_quota(lic_status=_FakeStatus())
    assert "source" not in out and out["included"] == 0 and out["allowed"] is True


def test_recording_chars_lands_on_trial_lic_id(tmp_path):
    t = _wire(tmp_path)
    t.begin(now=time.time())
    qs.record_license_chars("translation", 1500, lic_status=_FakeStatus())
    out = qs.check_license_quota(lic_status=_FakeStatus())
    assert out["used"] == 1500 and out["remaining"] == 8500


def test_exhausted_trial_blocks_only_when_enforced(tmp_path):
    t = _wire(tmp_path, enforce=False, chars=100)
    t.begin(now=time.time())
    qs.record_license_chars("translation", 100, lic_status=_FakeStatus())
    soft = qs.check_license_quota(lic_status=_FakeStatus())
    assert soft["exceeded"] is True and soft["allowed"] is True, "默认只提醒不拦"

    qs.reset_license_quota_store()
    t2 = _wire(tmp_path, enforce=True, chars=100)
    t2.begin(now=time.time())
    hard = qs.check_license_quota(lic_status=_FakeStatus())
    assert hard["exceeded"] is True and hard["allowed"] is False


def test_expired_trial_counts_as_exceeded(tmp_path):
    t = _wire(tmp_path, enforce=True, window=0.001)   # 3.6 秒窗口
    t.begin(now=time.time() - 3600)                   # 一小时前就开始了
    out = qs.check_license_quota(lic_status=_FakeStatus())
    assert out["trial_expired"] is True
    assert out["exceeded"] is True and out["allowed"] is False


def test_licensed_customer_never_sees_trial_quota(tmp_path):
    """最关键的隔离：付费客户的额度口径不能被体验档污染。"""
    t = _wire(tmp_path)
    t.begin(now=time.time())
    st = _FakeStatus(licensed=True, included=1_000_000, lic_id="chatx-team-001",
                     state="active")
    out = qs.check_license_quota(lic_status=st)
    assert out.get("source") != "local_trial"
    assert out["lic_id"] == "chatx-team-001" and out["included"] == 1_000_000


def test_licensed_customer_chars_do_not_land_on_trial(tmp_path):
    t = _wire(tmp_path)
    t.begin(now=time.time())
    st = _FakeStatus(licensed=True, included=1_000_000, lic_id="chatx-team-001",
                     state="active")
    qs.record_license_chars("translation", 700, lic_status=st)
    store = qs.get_license_quota_store()
    assert store.used_chars("chatx-team-001") == 700
    assert store.used_chars("local:ab12cd34") == 0


def test_unlimited_license_is_not_metered_via_trial(tmp_path):
    """不限量授权（included=0 且 licensed）不该被体验档接管计量。

    正确表现是**零 IO**：连额度库都不建（`get_license_quota_store()` 仍为 None），
    而不是"建了库但没写行"——给不限量客户凭空建一个计量库本身就是错的。
    """
    t = _wire(tmp_path)
    t.begin(now=time.time())
    st = _FakeStatus(licensed=True, included=0, lic_id="lingox-pro-1", state="active")
    qs.record_license_chars("translation", 900, lic_status=st)
    out = qs.check_license_quota(lic_status=st)
    assert out.get("source") != "local_trial"
    assert qs.get_license_quota_store() is None, "不限量授权不该触发建库"


def test_trial_disabled_means_zero_io(tmp_path):
    qs.configure_license_quota_store(db_path=str(tmp_path / "q.db"))
    lt.configure_local_trial({"licensing": {"trial": {"enabled": False}}})
    qs.record_license_chars("translation", 500, lic_status=_FakeStatus())
    out = qs.check_license_quota(lic_status=_FakeStatus())
    assert "source" not in out and out["allowed"] is True


def test_desktop_seed_config_enables_the_trial():
    """产品决策落在**随包种子**里：桌面装完就有体验档，服务器部署不受影响。

    这条把决策与文件绑死——有人顺手改掉种子里的开关，功能会静默消失（默认关），
    没有这个门禁不会有任何东西变红。
    """
    import yaml
    from pathlib import Path
    seed = (Path(__file__).resolve().parents[1] / "config" / "config.desktop.min.yaml")
    cfg = yaml.safe_load(seed.read_text(encoding="utf-8")) or {}
    trial = ((cfg.get("licensing") or {}).get("trial") or {})
    assert trial.get("enabled") is True, "桌面种子必须开启首启体验档"
    assert int(trial.get("chars") or 0) > 0
    assert float(trial.get("window_hours") or 0) > 0
    # enforce 必须保持关：「注册换正式试用」链路上线前开它 = 用尽即无路可走
    assert trial.get("enforce") is False, (
        "注册换正式试用的链路上线前，桌面种子不得开 enforce —— "
        "否则新装用户 48 小时后被锁死且无处求助"
    )


def test_upgrade_install_still_gets_the_trial(monkeypatch, tmp_path):
    """升级安装（旧 config.yaml 里根本没有 licensing 段）也必须拿到体验档。

    173 实机踩到：种子 config.desktop.min.yaml **只在配置文件不存在时播种**，
    所以从 0.2.1/0.2.2 升到 0.2.3 的机器 config.yaml 里没有 licensing 段
    → 体验档全程是关的（实测 `source=license / included=0`，装完什么都没有）。
    修法是桌面模式下把「没写过」当默认开，而不是去改用户的 config.yaml。
    """
    from src.licensing.local_trial import configure_local_trial, reset_local_trial

    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    reset_local_trial()
    t = configure_local_trial({}, config_dir=str(tmp_path))  # 整个 licensing 段缺失
    assert t is not None, "桌面模式 + 配置未写 → 应按默认开启"
    snap = t.snapshot()
    assert snap["included"] == 10_000 and snap["window_hours"] == 48.0
    reset_local_trial()


def test_explicit_false_is_never_overridden_by_desktop_default(monkeypatch, tmp_path):
    """写了 `enabled: false` 是明确的关闭意愿，桌面默认值不得顶掉它。"""
    from src.licensing.local_trial import configure_local_trial, reset_local_trial

    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    reset_local_trial()
    t = configure_local_trial(
        {"licensing": {"trial": {"enabled": False}}}, config_dir=str(tmp_path))
    assert t is None
    reset_local_trial()


def test_server_deploy_unaffected_by_desktop_default(monkeypatch, tmp_path):
    """没有 AITR_DESKTOP_MODE 的服务器部署仍是默认关（零行为变更）。"""
    from src.licensing.local_trial import configure_local_trial, reset_local_trial

    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    reset_local_trial()
    assert configure_local_trial({}, config_dir=str(tmp_path)) is None
    reset_local_trial()


def test_baseline_config_keeps_the_trial_off():
    """基线 example 保持默认关（新子系统惯例；服务器部署零行为变更）。"""
    import yaml
    from pathlib import Path
    ex = (Path(__file__).resolve().parents[1] / "config" / "config.example.yaml")
    cfg = yaml.safe_load(ex.read_text(encoding="utf-8")) or {}
    trial = ((cfg.get("licensing") or {}).get("trial") or {})
    assert trial.get("enabled") is False


def test_quota_gate_never_raises_when_trial_explodes(tmp_path, monkeypatch):
    """体验档出任何问题都必须放行——额度闸门绝不能把翻译链打挂。"""
    t = _wire(tmp_path)
    t.begin(now=time.time())

    def _boom(*_a, **_k):
        raise RuntimeError("trial exploded")

    monkeypatch.setattr(t, "snapshot", _boom)
    out = qs.check_license_quota(lic_status=_FakeStatus())
    assert out["allowed"] is True
