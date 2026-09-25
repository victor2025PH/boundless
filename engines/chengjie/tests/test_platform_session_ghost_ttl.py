"""登录尝试幽灵治理门禁（2026-08-14 事故沉淀）。

实录：173 坐席机上一次「打开登录窗又直接关掉」的放弃登录（临时 login_id
``msg_eysge7k3``）被 worker 以 ``expired`` 上报 → 健康表把它当真实通道挂进
坐席顶栏红条 3+ 小时，红条只显示 ``messenger:msg_eysge7k3``，坐席完全不知道
那是什么、也无从处理。两层修复，各自守住：

① worker 侧改报 ``abandoned``（不在 UNHEALTHY_STATUSES）——不进横幅/看门狗；
② 健康表对「不健康且从未绑定真实账号（key 账号段==login_id）」的存量幽灵
   加 30min TTL 惰性清扫（旧版 worker / 其它上报路径兜底）。

另守横幅快照的昵称富集（红条可读性）。
"""

from __future__ import annotations

import time

from src.integrations.platform_session_health import (
    _ORPHAN_LOGIN_TTL_SEC,
    ABANDONED_STATUS,
    PlatformSessionHealth,
    UNHEALTHY_STATUSES,
)


def _age(h: PlatformSessionHealth, key: str, sec: float) -> None:
    """把某条记录的不健康起点往回拨 sec 秒（测试专用，绕开真实等待）。"""
    with h._lock:  # noqa: SLF001 - 测试直接操作内部态
        sess = h._sessions[key]
        base = time.time() - sec
        sess["unhealthy_since"] = base
        sess["ts"] = base


class TestAbandonedStatus:
    """abandoned = 放弃的登录尝试，绝不能被当成故障。"""

    def test_abandoned_not_in_unhealthy_statuses(self):
        assert ABANDONED_STATUS not in UNHEALTHY_STATUSES

    def test_abandoned_session_not_unhealthy(self):
        h = PlatformSessionHealth()
        h.record("messenger", "msg_abc12345", ABANDONED_STATUS,
                 detail="login window closed", login_id="msg_abc12345")
        assert not h.is_unhealthy("messenger", "msg_abc12345")
        assert h.unhealthy_sessions() == {}

    def test_abandoned_transition_no_alerts(self):
        """expired → abandoned 不得触发 went_unhealthy，也不得谎报 recovered
        （recovered 只认 HEALTHY_STATUSES，abandoned 不是恢复是「作废」）。"""
        h = PlatformSessionHealth()
        h.record("messenger", "msg_abc12345", "expired", login_id="msg_abc12345")
        trans = h.record("messenger", "msg_abc12345", ABANDONED_STATUS,
                         login_id="msg_abc12345")
        assert not trans["went_unhealthy"]
        assert not trans["recovered"]
        # 作废后即离开不健康集合
        assert h.unhealthy_sessions() == {}


class TestOrphanLoginTTL:
    """key 账号段==login_id 的不健康记录（从未绑定真实账号）超 TTL 自动清。"""

    def test_orphan_swept_after_ttl(self):
        h = PlatformSessionHealth()
        h.record("messenger", "msg_ghost001", "expired",
                 detail="login window closed", login_id="msg_ghost001")
        _age(h, "messenger:msg_ghost001", _ORPHAN_LOGIN_TTL_SEC + 60)
        assert h.unhealthy_sessions() == {}
        # 表里也真删了（不是只在快照里藏起来）
        with h._lock:  # noqa: SLF001
            assert "messenger:msg_ghost001" not in h._sessions

    def test_orphan_under_ttl_kept(self):
        h = PlatformSessionHealth()
        h.record("messenger", "msg_ghost002", "expired", login_id="msg_ghost002")
        _age(h, "messenger:msg_ghost002", _ORPHAN_LOGIN_TTL_SEC - 120)
        assert "messenger:msg_ghost002" in h.unhealthy_sessions()

    def test_real_account_never_swept(self):
        """真实账号（key=账号 id ≠ login_id）的不健康记录永不 TTL——那是要人修的。"""
        h = PlatformSessionHealth()
        h.record("messenger", "61580373548045", "expired",
                 detail="cookies expired", login_id="msg_uh0vp1rf")
        _age(h, "messenger:61580373548045", _ORPHAN_LOGIN_TTL_SEC * 10)
        assert "messenger:61580373548045" in h.unhealthy_sessions()

    def test_no_login_id_never_swept(self):
        """login_id 为空（如注册表种子的 logged_out 行）不参与孤儿判定。"""
        h = PlatformSessionHealth()
        h.record("whatsapp", "8613800000000", "needs_login")
        _age(h, "whatsapp:8613800000000", _ORPHAN_LOGIN_TTL_SEC * 10)
        assert "whatsapp:8613800000000" in h.unhealthy_sessions()

    def test_abandoned_orphan_row_swept_after_ttl(self):
        """abandoned 孤儿行超 TTL 同样清（防反复放弃登录吃光 _MAX_KEYS=64 槽位）；
        放弃率观测在 by_status 事件计数，不依赖行存活——清行后计数必须还在。"""
        h = PlatformSessionHealth()
        h.record("messenger", "msg_ghost009", ABANDONED_STATUS,
                 detail="login window closed", login_id="msg_ghost009")
        _age(h, "messenger:msg_ghost009", _ORPHAN_LOGIN_TTL_SEC + 60)
        d = h.dump()
        assert "messenger:msg_ghost009" not in d["sessions"]
        assert d["by_status"].get(ABANDONED_STATUS) == 1

    def test_abandoned_orphan_under_ttl_kept(self):
        """TTL 内的 abandoned 行保留（ops 卡 sessions 表短期可见「刚放弃了一次」）。"""
        h = PlatformSessionHealth()
        h.record("messenger", "msg_ghost010", ABANDONED_STATUS,
                 login_id="msg_ghost010")
        _age(h, "messenger:msg_ghost010", _ORPHAN_LOGIN_TTL_SEC - 120)
        assert "messenger:msg_ghost010" in h.dump()["sessions"]

    def test_sweep_wired_into_due_reminders_and_dump(self):
        """看门狗与 ops 卡两条读路径同样要清扫（不然横幅清了看门狗还在轰）。"""
        h = PlatformSessionHealth()
        h.record("messenger", "msg_ghost003", "expired", login_id="msg_ghost003")
        _age(h, "messenger:msg_ghost003", _ORPHAN_LOGIN_TTL_SEC + 60)
        due = h.due_reminders(min_age_sec=0, interval_sec=0)
        assert "messenger:msg_ghost003" not in due
        d = h.dump()
        assert "messenger:msg_ghost003" not in d["sessions"]
        assert d["unhealthy_count"] == 0


class TestChannelSnapshotName:
    """横幅快照昵称富集：红条要能写出「谁」掉线，而不只是数字 id。"""

    def test_snapshot_carries_name_and_skips_abandoned(self, monkeypatch):
        from src.web.routes import unified_inbox_setup_routes as mod

        h = PlatformSessionHealth()
        h.record("messenger", "61580373548045", "expired",
                 detail="cookies expired", login_id="msg_uh0vp1rf")
        h.record("messenger", "msg_ghost004", ABANDONED_STATUS,
                 login_id="msg_ghost004")
        h.record("messenger", "seed_acct", "logged_out")

        import src.integrations.platform_session_health as psh
        monkeypatch.setattr(psh, "get_platform_session_health", lambda: h)
        monkeypatch.setattr(psh, "ensure_seeded_from_registry", lambda: 0)

        class _FakeReg:
            def get(self, plat, acct):
                if acct == "61580373548045":
                    return {"label": "", "status": "online",
                            "meta": {"self_name": "Micah Bindo"}}
                return None

        import src.integrations.account_registry as reg_mod
        monkeypatch.setattr(reg_mod, "get_account_registry", lambda: _FakeReg())

        snap = mod._channel_health_snapshot()
        assert snap["count"] == 1
        item = snap["unhealthy"][0]
        assert item["account_id"] == "61580373548045"
        assert item["name"] == "Micah Bindo"
        assert item["status"] == "expired"

    def test_snapshot_name_empty_when_registry_misses(self, monkeypatch):
        from src.web.routes import unified_inbox_setup_routes as mod

        h = PlatformSessionHealth()
        h.record("messenger", "999888777", "needs_login")

        import src.integrations.platform_session_health as psh
        monkeypatch.setattr(psh, "get_platform_session_health", lambda: h)
        monkeypatch.setattr(psh, "ensure_seeded_from_registry", lambda: 0)

        class _EmptyReg:
            def get(self, plat, acct):
                return None

        import src.integrations.account_registry as reg_mod
        monkeypatch.setattr(reg_mod, "get_account_registry", lambda: _EmptyReg())

        snap = mod._channel_health_snapshot()
        assert snap["count"] == 1
        assert snap["unhealthy"][0]["name"] == ""
