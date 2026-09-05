"""#186 用户管理页僵尸会话 + 口径（J-7 F，2026-09-05）。

skuio（QEDG4X）：活跃会话 8 条全是 admin·master/127.0.0.1/Chrome，08-29→09-05 每天一条；
「登录：从未」与 8 条活跃矛盾；本月用量 5203 无单位；页面没说这是操作员账号。
真因：桌面壳每次启动走 auth_token 直登 → create_session 每次 INSERT、旧行永不 revoke
（壳从不 /logout），且该路径不经 verify() → last_login 永远空。

钉住：① 同设备（username+ip+ua）令牌直登只留最近一条；② 空闲 >7 天的会话 touch 失效、
列表不显；③ 30 天以上物理清理；④ mark_login 给主帐号记 last_login（常量用户名 "admin"
不存在时落到 master 角色）；⑤ 用户页「登录」栏用最近会话时间兜底；⑥ 模板/词条形状。
"""
import re
import time
from pathlib import Path

import pytest

from src.utils.web_user_store import ROLE_MASTER, WebUserStore

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store(tmp_path):
    return WebUserStore(tmp_path / "users.db")


def _set_last_seen(store, jti, days_ago):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days_ago * 86400))
    with store._lock:
        store._conn.execute("UPDATE web_sessions SET last_seen=?, created_at=? WHERE jti=?", (ts, ts, jti))
        store._conn.commit()


def test_same_device_token_login_keeps_only_latest(store):
    ua = "Mozilla/5.0 Chrome/128 Electron/31"
    j1 = store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua, replace_same_device=True)
    j2 = store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua, replace_same_device=True)
    j3 = store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua, replace_same_device=True)
    active = {s["jti"] for s in store.list_sessions()}
    assert active == {j3}, "重启 3 次后活跃会话仍应只有 1 条"
    assert store.touch_session(j1) is False and store.touch_session(j2) is False
    assert store.touch_session(j3) is True


def test_same_device_recycle_does_not_touch_other_devices_or_users(store):
    ua = "Mozilla/5.0 Chrome/128"
    a = store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua, replace_same_device=True)
    other_ip = store.create_session("admin", ROLE_MASTER, "10.0.0.8", ua, replace_same_device=True)
    other_user = store.create_session("lily", "agent", "127.0.0.1", ua, replace_same_device=True)
    other_ua = store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua + " Safari", replace_same_device=True)
    active = {s["jti"] for s in store.list_sessions()}
    assert active == {a, other_ip, other_user, other_ua}


def test_password_login_path_default_does_not_recycle(store):
    """账号密码登录默认不回收（多机同 NAT 同 UA 的同名账号不能被一刀切）。"""
    ua = "Mozilla/5.0 Chrome/128"
    a = store.create_session("lily", "agent", "1.2.3.4", ua)
    b = store.create_session("lily", "agent", "1.2.3.4", ua)
    assert {s["jti"] for s in store.list_sessions()} == {a, b}


def test_idle_sessions_expire_and_are_pruned(store):
    ua = "Mozilla/5.0 Chrome/128"
    fresh = store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua)
    idle = store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua + " x")
    ancient = store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua + " y")
    _set_last_seen(store, idle, WebUserStore.SESSION_IDLE_DAYS + 1)
    _set_last_seen(store, ancient, WebUserStore.SESSION_PRUNE_DAYS + 1)
    # 列表：空闲过期的懒标 revoked 不显
    assert {s["jti"] for s in store.list_sessions()} == {fresh}
    assert store.touch_session(idle) is False, "空闲超 7 天的会话再来请求要被拒（要求重登）"
    assert store.touch_session(fresh) is True
    # 下一次登录顺手物理清理 30 天以上的行
    store.create_session("admin", ROLE_MASTER, "127.0.0.1", ua + " z")
    with store._lock:
        rows = {r[0] for r in store._conn.execute("SELECT jti FROM web_sessions").fetchall()}
    assert ancient not in rows and idle in rows


def test_mark_login_falls_back_to_master_role(store):
    store.create_user("boss", "secret123", ROLE_MASTER, display_name="Boss")
    assert store.get_user("boss")["last_login"] is None
    # 令牌直登的常量用户名 "admin" 不存在 → 记到 master 角色的账号上
    assert store.mark_login("admin", fallback_role=ROLE_MASTER) is True
    assert store.get_user("boss")["last_login"]
    # 用户名存在时精确命中
    store.create_user("lily", "secret123", "agent")
    assert store.mark_login("lily") is True and store.get_user("lily")["last_login"]
    assert store.mark_login("nobody") is False


def test_last_session_login_map(store):
    store.create_session("admin", ROLE_MASTER, "127.0.0.1", "ua")
    time.sleep(0.01)
    j = store.create_session("admin", ROLE_MASTER, "127.0.0.1", "ua", replace_same_device=True)
    m = store.last_session_login_map()
    assert "admin" in m and m["admin"]
    # 已作废的旧行也算「登录过」


def test_token_login_route_wires_recycle_and_mark_login():
    src = (_ROOT / "src" / "web" / "routes" / "auth_user_routes.py").read_text(encoding="utf-8")
    tok = src[src.index("legacy token login"):src.index("@app.get(\"/logout\")")]
    assert "replace_same_device=True" in tok
    assert 'mark_login("admin", fallback_role=ROLE_MASTER)' in tok
    pw = src[src.index("if username and password:"):src.index("legacy token login")]
    assert "replace_same_device" not in pw, "账号密码路径不回收"
    ctx = src[src.index("def _users_page_ctx("):src.index("def _runtime_config(")]
    assert "last_session_login_map()" in ctx and "last_login_from_session" in ctx


def test_users_template_copy_and_units():
    tpl = (_ROOT / "src" / "web" / "templates" / "users.html").read_text(encoding="utf-8")
    assert "us_account_note" in tpl, "页头必须说明这是操作员账号而非聊天平台账号"
    assert "else '从未'" not in tpl and "get('us_never_login'" in tpl, "「从未」必须走 i18n 键（旧写法是硬编码）"
    assert "TQ.charsUnit" in tpl and "tq_chars_unit" in tpl, "用量必须带单位"
    # 技术标注（IP/UA/role/jti）收进折叠区；卡片主体显设备名 + 操作员 + 最后活跃
    sess = tpl[tpl.index("async function loadSessions()"):tpl.index("async function revokeSession(")]
    assert "<details" in sess and "TQ.sessTech" in sess
    assert "UA: ${esc(ua" in sess and "role: ${esc(s.role" in sess
    assert "TQ.sessOperator" in sess and "window.T('role_'+" in sess
    assert "/Electron|ChatX|zhiliao|telegram-ai-desktop/i" in sess
    assert re.search(r"us_sess_hint", tpl)


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.team_page import EN, ZH
    for k in ("us_account_note", "us_never_login", "us_login_from_session_t", "tq_chars_unit",
              "us_sess_title", "us_sess_hint", "us_dev_desktop", "us_dev_this",
              "us_sess_operator", "us_sess_tech", "us_sess_since"):
        assert k in ZH and k in EN, k
