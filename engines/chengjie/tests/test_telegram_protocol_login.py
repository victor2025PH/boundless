"""M2：Telegram protocol（pyrogram）扫码登录 provider 单测。

仅覆盖纯函数 / 门控 / 状态机的 pending 分支（不联网、不需真账号）。
真实扫码成功 + DC 迁移路径需用测试号联调（protocol_enabled 默认 false）。
"""

from __future__ import annotations

import asyncio
import base64
import time

import pytest

from src.integrations import platform_login as pl
from src.integrations import telegram_protocol_login as tpl


def test_tg_login_url_format():
    url = tpl.tg_login_url(b"abc")
    assert url == "tg://login?token=" + base64.urlsafe_b64encode(b"abc").decode().rstrip("=")
    assert url.startswith("tg://login?token=")


def test_resolve_credentials_flat_and_accounts():
    assert tpl.resolve_credentials({}) is None
    assert tpl.resolve_credentials({"telegram": {"api_id": 0, "api_hash": ""}}) is None
    flat = tpl.resolve_credentials({"telegram": {"api_id": 123, "api_hash": "h"}})
    assert flat == (123, "h")
    nested = tpl.resolve_credentials(
        {"telegram": {"accounts": [{"api_id": 9, "api_hash": "z"}]}})
    assert nested == (9, "z")


def test_protocol_enabled_flag():
    assert tpl.protocol_enabled({}) is False
    assert tpl.protocol_enabled(
        {"platform_login": {"telegram": {"protocol_enabled": True}}}) is True


def test_maybe_register_gated_off_by_default():
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)
    # 有凭据但未开 protocol_enabled → 不注册
    cfg = {"telegram": {"api_id": 1, "api_hash": "h"}}
    assert tpl.maybe_register(cfg) is False
    assert pl.mode_available("telegram", "protocol") is False


def test_maybe_register_when_enabled():
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "h"},
        "platform_login": {"telegram": {"protocol_enabled": True}},
    }
    try:
        assert tpl.maybe_register(cfg) is True
        assert pl.mode_available("telegram", "protocol") is True
        # 幂等
        assert tpl.maybe_register(cfg) is True
    finally:
        tpl._registered = False
        pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)


def test_state_machine_pending_branch(tmp_path):
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    # pyrogram 顶层 import 会触发 sync 模块调用 get_event_loop()，
    # 在 xdist worker 线程里无 loop 会抛 RuntimeError —— 先确保有 loop。
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from pyrogram.raw.types.auth import LoginToken
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    tok = LoginToken(expires=int(time.time()) + 30, token=b"abc")
    asyncio.run(login._advance(tok))
    assert login.status == "pending"
    assert login.qr_url.startswith("tg://login?token=")
    assert login.result()["status"] == "pending"


# ── 两步验证（2FA）：纯判定 + submit_password 状态机（实施29+，无需联网/真号）──

# 类名刻意与 pyrogram.errors 真类同名（检测逻辑按 type(ex).__name__ 判定，故此处不加下划线）
class SessionPasswordNeeded(Exception):
    """模拟 pyrogram.errors.SessionPasswordNeeded（按类名判定，不依赖真类路径）。"""


class PasswordHashInvalid(Exception):
    """模拟 pyrogram.errors.PasswordHashInvalid（密码错误可重试）。"""


def test_is_password_needed_by_name_and_text():
    assert tpl._is_password_needed(SessionPasswordNeeded()) is True
    assert tpl._is_password_needed(Exception("SESSION_PASSWORD_NEEDED")) is True
    assert tpl._is_password_needed(Exception("something else")) is False


def test_is_bad_password_detection():
    assert tpl._is_bad_password(PasswordHashInvalid()) is True
    assert tpl._is_bad_password(Exception("PASSWORD_HASH_INVALID")) is True
    assert tpl._is_bad_password(Exception("network")) is False


class _FakeStorage:
    async def user_id(self, *_a):
        return None

    async def is_bot(self, *_a):
        return None


class _FakeUser:
    id = 8127518232
    phone_number = "60111471"
    first_name = "Boss"
    last_name = ""
    username = "bossacct"


class _FakeClient:
    """最小可跑的 pyrogram Client 替身：只实现 submit_password 收尾用到的方法。"""

    def __init__(self, *, password_ok=True):
        self._password_ok = password_ok
        self.storage = _FakeStorage()
        self.disconnected = False

    async def check_password(self, password):
        if not self._password_ok:
            raise PasswordHashInvalid("PASSWORD_HASH_INVALID")
        return _FakeUser()

    async def export_session_string(self):
        return "FAKE_SESSION_STRING"

    async def disconnect(self):
        self.disconnected = True


def _mk_login(tmp_path, *, client):
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    login.status = "password_needed"
    login.client = client
    return login


def test_submit_password_success(tmp_path):
    login = _mk_login(tmp_path, client=_FakeClient(password_ok=True))
    res = asyncio.run(login.submit_password("cloud-pw"))
    assert res["status"] == "authorized"
    assert login.account_id == "8127518232"
    assert login.session_string == "FAKE_SESSION_STRING"
    assert login.client.disconnected is True


def test_submit_password_wrong_keeps_retryable(tmp_path):
    login = _mk_login(tmp_path, client=_FakeClient(password_ok=False))
    res = asyncio.run(login.submit_password("wrong"))
    # 密码错误：停在 password_needed 可重试，不落 authorized/failed
    assert res["status"] == "password_needed"
    assert "错误" in login.detail


def test_submit_password_ignored_when_not_waiting(tmp_path):
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    login.status = "pending"
    res = asyncio.run(login.submit_password("x"))
    assert res["status"] == "pending"  # 非 password_needed 态直接返回，不动作


def test_poll_short_circuits_on_password_needed(tmp_path):
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    login.status = "password_needed"
    login.client = object()  # 若真去 invoke 会 AttributeError → 证明没走网络分支
    res = asyncio.run(login.poll())
    assert res["status"] == "password_needed"


# ── _migrate loop 亲和性（2026-08-14 跨区扫码 DC 迁移事故回归钉）──────────────
# 根因：pyrogram sync.py 把 Client 公开方法从 web 线程调度到主 loop 执行，
# Session（ping_task/recv_task）因此活在主 loop；_migrate 直接操作 Session 内部
# （不在包装范围），若在 web loop 裸 await session.stop() → 「attached to a
# different loop」。契约＝_migrate 必须在 session.loop 上执行整段迁移。


class _MigrateStorage:
    def __init__(self, sink):
        self._sink = sink

    async def dc_id(self, *_a):
        self._sink.append(("storage.dc_id", asyncio.get_running_loop()))

    async def test_mode(self, *_a):
        self._sink.append(("storage.test_mode", asyncio.get_running_loop()))
        return False

    async def auth_key(self, *_a):
        self._sink.append(("storage.auth_key", asyncio.get_running_loop()))


class _MigrateSession:
    def __init__(self, loop, sink):
        self.loop = loop
        self._sink = sink

    async def stop(self):
        self._sink.append(("session.stop", asyncio.get_running_loop()))

    async def start(self):
        self._sink.append(("session.start", asyncio.get_running_loop()))


class _FakeAuth:
    def __init__(self, *_a, **_kw):
        pass

    async def create(self):
        return b"fake-auth-key"


def _run_migrate_with_session_loop(tmp_path, monkeypatch, *, cross_loop: bool):
    """在独立线程 loop 上放一个假 session，验证 _migrate 的执行落点。"""
    import threading
    import pyrogram.session as pysess

    sink: list = []
    sess_loop_box: dict = {}

    def _thread_loop():
        loop = asyncio.new_event_loop()
        sess_loop_box["loop"] = loop
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_thread_loop, daemon=True)
    t.start()
    while "loop" not in sess_loop_box:
        pass
    sess_loop = sess_loop_box["loop"]

    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    client = _FakeClient()
    client.storage = _MigrateStorage(sink)
    login.client = client

    # 新 Session 构造也要发生在 session loop 上（sink 记录构造点由 start() 代表）
    monkeypatch.setattr(pysess, "Auth", _FakeAuth)
    monkeypatch.setattr(
        pysess, "Session",
        lambda *_a, **_kw: _MigrateSession(sess_loop, sink))

    try:
        if cross_loop:
            client.session = _MigrateSession(sess_loop, sink)
            asyncio.run(login._migrate(5))
            expect = sess_loop
        else:
            async def _same_loop():
                here = asyncio.get_running_loop()
                client.session = _MigrateSession(here, sink)
                await login._migrate(5)
                return here
            expect = asyncio.run(_same_loop())
    finally:
        sess_loop.call_soon_threadsafe(sess_loop.stop)
        t.join(timeout=5)

    steps = [s for s, _ in sink]
    assert steps == ["session.stop", "storage.dc_id", "storage.test_mode",
                     "storage.auth_key", "session.start"]
    for step, loop in sink:
        assert loop is expect, f"{step} 落在了错误的 loop 上"


def test_migrate_marshals_to_session_loop(tmp_path, monkeypatch):
    """跨 loop 场景：session 活在别的 loop → 迁移整段必须调度过去执行。"""
    _run_migrate_with_session_loop(tmp_path, monkeypatch, cross_loop=True)


def test_migrate_same_loop_runs_direct(tmp_path, monkeypatch):
    """同 loop 场景（单测/主线程运行）：直连执行，行为与旧实现一致。"""
    _run_migrate_with_session_loop(tmp_path, monkeypatch, cross_loop=False)


# ── Q-10 #268：storage 已关闭时 _migrate 不再对尸体操作（2026-09-08 13:32 实锤）──
# 栈：_advance → _migrate → _do → storage.dc_id()/session.start() →
# pyrogram sqlite_storage：sqlite3.ProgrammingError: Cannot operate on a closed database；
# 且 _advance 的「首试失败自动重试一次」对同一具尸体再撞一次（两份栈）。

class _ClosedConnStorage(_MigrateStorage):
    """带 pyrogram SQLiteStorage 同款 ``conn`` 属性、且连接已关闭的假 storage。"""

    def __init__(self, sink):
        import sqlite3
        super().__init__(sink)
        self.conn = sqlite3.connect(":memory:")
        self.conn.close()


def test_storage_closed_probe():
    import sqlite3

    class _S:
        pass

    class _C:
        storage = _S()

    assert tpl._storage_closed(None) is True
    assert tpl._storage_closed(_C()) is False            # 无 conn 属性（假件 / 内存）＝未关
    c = _C()
    c.storage.conn = None
    assert tpl._storage_closed(c) is True
    c.storage.conn = sqlite3.connect(":memory:")
    assert tpl._storage_closed(c) is False
    c.storage.conn.close()
    assert tpl._storage_closed(c) is True
    assert tpl._is_closed_db_error(
        sqlite3.ProgrammingError("Cannot operate on a closed database.")) is True
    assert tpl._is_closed_db_error(Exception("timeout")) is False


def test_migrate_refuses_closed_storage(tmp_path, monkeypatch):
    """storage 已关 → _migrate 直接抛 LoginStorageClosed，不碰 session.stop / storage.dc_id。"""
    import pyrogram.session as pysess
    sink: list = []
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    client = _FakeClient()
    client.storage = _ClosedConnStorage(sink)
    login.client = client
    monkeypatch.setattr(pysess, "Auth", _FakeAuth)

    async def _run():
        client.session = _MigrateSession(asyncio.get_running_loop(), sink)
        await login._migrate(5)

    with pytest.raises(tpl.LoginStorageClosed):
        asyncio.run(_run())
    assert sink == []          # 一步都没往尸体上走


def test_advance_migrate_closed_storage_no_retry_and_terminal(tmp_path, monkeypatch):
    """扫码后 storage 被收走：_advance 只试一次（不再「自动重试」撞第二次），poll 归因
    session_closed 终态，不再 expired 让前端无限换码。"""
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from pyrogram.raw.types.auth import LoginTokenMigrateTo

    sink: list = []
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    client = _FakeClient()
    client.storage = _ClosedConnStorage(sink)
    login.client = client
    calls = {"n": 0}
    orig = login._migrate

    async def _counting(dc_id):
        calls["n"] += 1
        await orig(dc_id)

    monkeypatch.setattr(login, "_migrate", _counting)
    tok = LoginTokenMigrateTo(dc_id=5, token=b"abc")

    async def _run():
        client.session = _MigrateSession(asyncio.get_running_loop(), sink)
        try:
            await login._advance(tok)
        except Exception as ex:  # noqa: BLE001
            login._classify_poll_failure(ex)

    asyncio.run(_run())
    assert calls["n"] == 1, "storage 已关不得自动重试第二次"
    assert login._scan_seen is True
    assert login.status == "failed"
    assert login.reason_code == "session_closed"
    assert "重新发起" in login.detail


def test_classify_raw_closed_db_error_is_terminal(tmp_path):
    """pyrogram 内部抛出的裸 sqlite3.ProgrammingError(closed database) 同样归 session_closed。"""
    import sqlite3
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    login._scan_seen = False
    login._classify_poll_failure(
        sqlite3.ProgrammingError("Cannot operate on a closed database."))
    assert login.status == "failed"
    assert login.reason_code == "session_closed"


def test_poll_short_circuits_when_storage_closed(tmp_path):
    """poll 进锁后先探 storage：已关 → 不 invoke（invoke 会 AttributeError）直接终态。"""

    class _S:
        conn = None

    class _C:
        storage = _S()

    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    login.status = "pending"
    login.client = _C()
    res = asyncio.run(login.poll())
    assert res["status"] == "failed"
    assert res["reason_code"] == "session_closed"
