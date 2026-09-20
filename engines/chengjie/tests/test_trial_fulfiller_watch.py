"""履约端存活监控门禁。

守三类东西：

1. **判定语义**——stale / stuck / unreachable 三态要分得开，因为处置动作不同；
   健康态不能误报（客服台已经因为我算错时区虚惊过一次，告警更不能狼来了）。
2. **监控不能自我失效**——看门狗读台账必须带 ``?peek=1``。普通路径会记心跳，
   看门狗自己的轮询就会把心跳刷新，于是心跳永远新鲜、永远发现不了履约端已死。
   这条是整个监控成立的前提，所以从「代码里真的带了 peek」和「官网真的认 peek」
   两侧各钉一次。
3. **升级时序**——首提要等够 after_min（避免一次网络抖动就轰人）、未恢复按
   interval_min 重提、恢复补发一次、换了故障种类要重新计时。
"""
from __future__ import annotations

import datetime as _dt

import pytest

from src.licensing import trial_fulfiller_watch as w


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat().replace("+00:00", "Z")


NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean():
    w.reset_probe_cache()
    yield
    w.reset_probe_cache()


# ── 启用判定 ───────────────────────────────────────────────────────────────

def test_disabled_by_default():
    """默认关：别的机器开了只会对着别人的台账瞎报。"""
    assert w.watch_target({}) is None
    assert w.watch_target({"licensing": {"trial": {}}}) is None


def test_needs_both_site_and_key_path():
    base = {"licensing": {"trial": {"fulfiller_watch": {"enabled": True}}}}
    assert w.watch_target(base) is None, "缺 site 与 key 路径不该算启用"
    only_site = {"licensing": {"trial": {"fulfiller_watch": {
        "enabled": True, "site_url": "https://x"}}}}
    assert w.watch_target(only_site) is None, "没有 key 路径取不到台账"


def test_target_inherits_site_url_and_defaults():
    t = w.watch_target({"licensing": {"trial": {
        "site_url": "https://bd2026.cc/",
        "fulfiller_watch": {"enabled": True, "admin_key_file": "k.txt"}}}})
    assert t is not None
    assert t["site"] == "https://bd2026.cc", "尾斜杠要归一，否则拼出 //api"
    assert t["stale_min"] == 15 and t["after_min"] == 15


# ── 三态判定 ───────────────────────────────────────────────────────────────

def test_healthy_when_beat_fresh_and_no_backlog():
    s = w.evaluate({"fulfiller_last_seen": _iso(NOW - 120), "pending": 0,
                    "oldest_pending_min": 0}, now=NOW)
    assert s["healthy"] and s["kind"] == ""


def test_healthy_when_beat_fresh_and_backlog_young():
    """刚有人领取、履约端还没轮到 —— 这是正常态，不能报。"""
    s = w.evaluate({"fulfiller_last_seen": _iso(NOW - 60), "pending": 2,
                    "oldest_pending_min": 3}, now=NOW, backlog_min=20)
    assert s["healthy"]


def test_stale_when_heartbeat_old():
    s = w.evaluate({"fulfiller_last_seen": _iso(NOW - 47 * 60), "pending": 3,
                    "oldest_pending_min": 52}, now=NOW, stale_min=15)
    assert not s["healthy"] and s["kind"] == "stale"
    assert s["heartbeat_min"] == 47 and s["pending"] == 3


def test_never_ran_is_stale_with_flag():
    """开了监控却从没来过 = 常驻任务没装，与「过期」同样要处置，但文案不同。"""
    s = w.evaluate({"fulfiller_last_seen": "", "pending": 1,
                    "oldest_pending_min": 9}, now=NOW)
    assert not s["healthy"] and s["kind"] == "stale" and s["never"] is True


def test_stuck_when_beat_fresh_but_backlog_old():
    """它在来却清不掉活（回填失败/签不出）—— 与 stale 的处置完全不同。"""
    s = w.evaluate({"fulfiller_last_seen": _iso(NOW - 90), "pending": 5,
                    "oldest_pending_min": 41}, now=NOW, stale_min=15, backlog_min=20)
    assert not s["healthy"] and s["kind"] == "stuck"


def test_unreachable_when_stats_missing():
    s = w.evaluate(None, now=NOW)
    assert not s["healthy"] and s["kind"] == "unreachable"


def test_future_heartbeat_clamped_not_negative():
    """官网时钟略超前不该算出负龄进而被当成怪值。"""
    s = w.evaluate({"fulfiller_last_seen": _iso(NOW + 30), "pending": 0,
                    "oldest_pending_min": 0}, now=NOW)
    assert s["healthy"] and s["heartbeat_min"] == 0


def test_unparseable_heartbeat_treated_as_never():
    s = w.evaluate({"fulfiller_last_seen": "not-a-date", "pending": 0,
                    "oldest_pending_min": 0}, now=NOW)
    assert s["kind"] == "stale" and s["never"] is True


# ── 监控不能自我失效 ───────────────────────────────────────────────────────

def test_probe_reads_with_peek_so_it_does_not_refresh_heartbeat(monkeypatch, tmp_path):
    """看门狗取台账必须带 ``peek=1``。

    不带的话：普通路径会 touchFulfiller()，看门狗自己每轮的轮询就把心跳刷新了，
    心跳永远新鲜 → 履约端死了也永远不报。整个监控就是个装饰。
    """
    kf = tmp_path / "k.txt"
    kf.write_text("secret", encoding="utf-8")
    seen = {}

    def fake_open(req, timeout=0):  # noqa: ARG001
        seen["url"] = req.full_url
        seen["key"] = req.get_header("X-setup-key")

        class R:
            def read(self):
                return (b'{"ok":true,"peeked":true,"stats":'
                        b'{"fulfiller_last_seen":"","pending":0,"oldest_pending_min":0}}')

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()

    monkeypatch.setattr(w.urllib.request, "urlopen", fake_open)
    cfg = {"licensing": {"trial": {"fulfiller_watch": {
        "enabled": True, "site_url": "https://bd2026.cc",
        "admin_key_file": str(kf)}}}}
    snap = w.probe(cfg, now=NOW)
    assert snap is not None
    assert "peek=1" in seen["url"], "监控读台账没带 peek → 会把心跳刷新，监控自我失效"
    assert seen["key"] == "secret", "密钥须经请求头，且是从文件读的"


def test_website_route_honors_peek():
    """官网侧也要真的认 peek——两边任一侧漏了，这条不变量就不成立。"""
    from pathlib import Path
    route = (Path(__file__).resolve().parents[3] / "website" / "app" / "api"
             / "admin" / "trial-claims" / "route.ts")
    if not route.is_file():
        pytest.skip("website 不在本 checkout 内")
    src = route.read_text(encoding="utf-8")
    assert 'peek' in src, "官网 admin 取待办口没实现 peek"
    assert "if (!peek) await touchFulfiller()" in src, \
        "peek 时仍在记心跳 → 监控方一读就把心跳刷新了"


def _stub_http(monkeypatch, payload: str):
    def fake_open(req, timeout=0):  # noqa: ARG001
        class R:
            def read(self):
                return payload.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()
    monkeypatch.setattr(w.urllib.request, "urlopen", fake_open)


def _watch_cfg(kf) -> dict:
    return {"licensing": {"trial": {"fulfiller_watch": {
        "enabled": True, "site_url": "https://bd2026.cc", "admin_key_file": str(kf)}}}}


def test_old_website_without_peek_echo_makes_us_stay_silent(monkeypatch, tmp_path):
    """官网还没上 peek 时必须**不监控**，而不是闭眼监控。

    部署顺序错了（先开监控后部署官网）时，旧官网会把我们这一读当成履约端心跳，
    心跳永远新鲜 → 履约端死了也永远不报。那比没有监控更糟：它给人虚假的安全感。
    """
    kf = tmp_path / "k.txt"
    kf.write_text("secret", encoding="utf-8")
    # 旧版本响应：没有 peeked 字段
    _stub_http(monkeypatch, '{"ok":true,"stats":{"fulfiller_last_seen":"","pending":9,"oldest_pending_min":99}}')
    assert w.probe(_watch_cfg(kf), now=NOW) is None, "旧官网应让监控静默"


def test_peek_echo_true_enables_monitoring(monkeypatch, tmp_path):
    kf = tmp_path / "k.txt"
    kf.write_text("secret", encoding="utf-8")
    _stub_http(monkeypatch, '{"ok":true,"peeked":true,"stats":'
                            '{"fulfiller_last_seen":"","pending":9,"oldest_pending_min":99}}')
    snap = w.probe(_watch_cfg(kf), now=NOW)
    assert snap is not None and snap["kind"] == "stale" and snap["never"] is True


def test_unsupported_is_not_cached(monkeypatch, tmp_path):
    """静默态不进缓存：官网一部署，下一 tick 就该恢复监控，而不是等 TTL。"""
    kf = tmp_path / "k.txt"
    kf.write_text("secret", encoding="utf-8")
    _stub_http(monkeypatch, '{"ok":true,"stats":{"fulfiller_last_seen":""}}')
    assert w.probe(_watch_cfg(kf), now=NOW) is None
    _stub_http(monkeypatch, '{"ok":true,"peeked":true,"stats":'
                            '{"fulfiller_last_seen":"","pending":1,"oldest_pending_min":5}}')
    assert w.probe(_watch_cfg(kf), now=NOW + 1) is not None, "不该被上一次的静默缓存挡住"


def test_website_route_echoes_peeked():
    from pathlib import Path
    route = (Path(__file__).resolve().parents[3] / "website" / "app" / "api"
             / "admin" / "trial-claims" / "route.ts")
    if not route.is_file():
        pytest.skip("website 不在本 checkout 内")
    assert "peeked: peek" in route.read_text(encoding="utf-8"), \
        "官网没回声 peeked → 监控方无法确认服务端认识 peek，只能选择静默"


def test_probe_cache_avoids_hammering(monkeypatch, tmp_path):
    kf = tmp_path / "k.txt"
    kf.write_text("secret", encoding="utf-8")
    calls = {"n": 0}
    monkeypatch.setattr(w, "fetch_stats", lambda t: (calls.__setitem__("n", calls["n"] + 1),
                                                     {"fulfiller_last_seen": _iso(NOW),
                                                      "pending": 0,
                                                      "oldest_pending_min": 0})[1])
    cfg = {"licensing": {"trial": {"fulfiller_watch": {
        "enabled": True, "site_url": "https://bd2026.cc", "admin_key_file": str(kf)}}}}
    w.probe(cfg, now=NOW)
    w.probe(cfg, now=NOW + 5)
    assert calls["n"] == 1, "TTL 内应命中缓存"
    w.probe(cfg, now=NOW + w.PROBE_TTL_SEC + 1)
    assert calls["n"] == 2, "过 TTL 应重探"


# ── 升级时序（接线在 HealthWatchdog）─────────────────────────────────────

class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, etype, payload):
        self.events.append((etype, payload))


@pytest.fixture
def wd(monkeypatch):
    """最小 HealthWatchdog：只为跑 _check_trial_fulfiller，不起任何后台线程。"""
    from src.inbox.health_watchdog import HealthWatchdog

    cfg = {"licensing": {"trial": {"fulfiller_watch": {
        "enabled": True, "site_url": "https://bd2026.cc",
        "admin_key_file": "k.txt", "after_min": 15, "interval_min": 240}}}}

    class CM:
        config = cfg

    obj = HealthWatchdog.__new__(HealthWatchdog)
    obj._config_manager = CM()
    obj._fulfiller_bad_since = 0.0
    obj._fulfiller_alerted = False
    obj._fulfiller_last_remind = 0.0
    obj._fulfiller_kind = ""
    obj.total_trial_fulfiller_reminders = 0

    bus = _Bus()
    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    return obj, bus, cfg


def _stub_probe(monkeypatch, snap):
    monkeypatch.setattr(w, "probe", lambda cfg, now=None: (dict(snap) if snap else None))


def test_first_alert_waits_for_after_min(wd, monkeypatch):
    obj, bus, _ = wd
    bad = {"healthy": False, "kind": "stale", "heartbeat_min": 40, "pending": 2,
           "backlog_min": 40, "never": False, "site": "https://bd2026.cc"}
    _stub_probe(monkeypatch, bad)
    obj._check_trial_fulfiller(now=NOW)              # 第一次只记时刻
    assert bus.events == [], "一次抖动不该立刻轰人"
    obj._check_trial_fulfiller(now=NOW + 5 * 60)     # 5 分钟 < 15 分钟
    assert bus.events == []
    obj._check_trial_fulfiller(now=NOW + 16 * 60)
    assert len(bus.events) == 1
    etype, p = bus.events[0]
    assert etype == "trial_fulfiller_alert" and p["kind"] == "stale"
    assert p["reminder"] is False and p["rate_key"] == "trial_fulfiller:remind"


def test_reminder_respects_interval(wd, monkeypatch):
    obj, bus, _ = wd
    bad = {"healthy": False, "kind": "stale", "heartbeat_min": 40, "pending": 1,
           "backlog_min": 40, "never": False, "site": "s"}
    _stub_probe(monkeypatch, bad)
    obj._check_trial_fulfiller(now=NOW)
    obj._check_trial_fulfiller(now=NOW + 16 * 60)
    assert len(bus.events) == 1
    obj._check_trial_fulfiller(now=NOW + 60 * 60)          # 1h < 4h
    assert len(bus.events) == 1
    obj._check_trial_fulfiller(now=NOW + 16 * 60 + 241 * 60)
    assert len(bus.events) == 2 and bus.events[1][1]["reminder"] is True


def test_recovery_notice_only_after_alert(wd, monkeypatch):
    obj, bus, _ = wd
    ok = {"healthy": True, "kind": "", "heartbeat_min": 1, "pending": 0,
          "backlog_min": 0, "never": False, "site": "s"}
    _stub_probe(monkeypatch, ok)
    obj._check_trial_fulfiller(now=NOW)
    assert bus.events == [], "没告警过的健康态不该发恢复通知（防噪）"

    bad = dict(ok, healthy=False, kind="stale")
    _stub_probe(monkeypatch, bad)
    obj._check_trial_fulfiller(now=NOW)
    obj._check_trial_fulfiller(now=NOW + 16 * 60)
    assert len(bus.events) == 1
    _stub_probe(monkeypatch, ok)
    obj._check_trial_fulfiller(now=NOW + 20 * 60)
    assert len(bus.events) == 2 and bus.events[1][1]["recovered"] is True
    assert obj._fulfiller_alerted is False


def test_kind_change_restarts_the_clock(wd, monkeypatch):
    """stale → stuck 是换了病因：该重新计时并允许再首提，否则处置线索被重提间隔压住。"""
    obj, bus, _ = wd
    _stub_probe(monkeypatch, {"healthy": False, "kind": "stale", "heartbeat_min": 40,
                              "pending": 1, "backlog_min": 40, "never": False, "site": "s"})
    obj._check_trial_fulfiller(now=NOW)
    obj._check_trial_fulfiller(now=NOW + 16 * 60)
    assert len(bus.events) == 1 and bus.events[0][1]["kind"] == "stale"
    _stub_probe(monkeypatch, {"healthy": False, "kind": "stuck", "heartbeat_min": 2,
                              "pending": 5, "backlog_min": 41, "never": False, "site": "s"})
    obj._check_trial_fulfiller(now=NOW + 17 * 60)   # 换种类：重新计时，不立刻报
    assert len(bus.events) == 1
    obj._check_trial_fulfiller(now=NOW + 33 * 60)
    assert len(bus.events) == 2 and bus.events[1][1]["kind"] == "stuck"


def test_silent_when_not_enabled(monkeypatch):
    from src.inbox.health_watchdog import HealthWatchdog

    class CM:
        config = {}

    obj = HealthWatchdog.__new__(HealthWatchdog)
    obj._config_manager = CM()
    obj._fulfiller_bad_since = 0.0
    obj._fulfiller_alerted = False
    obj._fulfiller_last_remind = 0.0
    obj._fulfiller_kind = ""
    obj.total_trial_fulfiller_reminders = 0
    bus = _Bus()
    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    obj._check_trial_fulfiller(now=NOW)
    assert bus.events == [], "未启用必须天然静默"
