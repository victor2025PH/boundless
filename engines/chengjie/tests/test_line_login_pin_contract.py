"""LINE 扫码登录「PIN 端到端」契约门禁。

事故背景（2026-07-25）：LINE 协议扫码 100% 登录失败。PIN 由 LINE 签发后经 okline 回调写进了
会话状态、后端也原样透传到了浏览器，但前端 ``scanned`` 分支忽略 ``detail`` 改用写死文案 →
手机等电脑给码、电脑等手机确认，互等到长轮询耗尽报过期。

**当时 `tests/test_line_protocol_login.py` 全绿**——它验证了 PIN 写进 state，却没有任何一层
验证 PIN 能否抵达前端。本文件补的正是这条跨层链路：每一跳都断言，任一跳退化即红。

分两类：
- 行为断言：provider 的 PIN 状态/字段、错误归类、失败文案不泄露内部实现、挂起态不被 TTL 误清。
- 静态扫描：后端路由是否声明了 PIN 字段、前端是否真的消费它、以及每个登录状态是否都有前端分支。
  静态轨守的是「加了字段/状态却没人消费」这类分层测试各自为政的盲区。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from pathlib import Path

import pytest

from src.integrations import line_protocol_login as lpl
from src.integrations import platform_login as pl
from src.integrations.account_registry import AccountRegistry

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_ROUTES = _ROOT / "src" / "web" / "routes" / "unified_inbox_login_routes.py"
# 登录状态接口的**全部**前端消费方。这次事故第二现场就是漏了副驾组件：只扫 templates
# 时它安然过关，而客户拿到的桌面壳里 LINE 登录照样卡死。新增消费方必须登记在此。
_CONSUMERS = (
    _TEMPLATE,
    _ROOT / "shared" / "copilot" / "components" / "cp-accounts.js",
)

# 线上真实报错原文（截图取证）。归类必须落到 qr_expired，且这段原文绝不能出现在用户界面上。
_PRODUCTION_QR_EXPIRED = (
    "Your QR code has expired. Please generate a new QR code and try again. "
    "code=100 path=/api/talk/thrift/LoginQrCode/SecondaryQrCodeLoginPermitNoti"
)

_PIN = "739241"


class _FakeResult:
    success = True
    mid = "u_pin_test"
    display_message = ""
    certificate = "cert-abc"


class _PinOkLine:
    """扫码后先要 PIN、随后登录成功——复刻 LINE 首次登录的真实两阶段流程。"""

    def __init__(self, *a, **k):
        self.closed = False

    def qr_login(self, *, on_qr=None, on_pin=None, **kw):
        if on_qr:
            on_qr("line://qr/pin-case")
        if on_pin:
            on_pin(_PIN)
        return _FakeResult()

    def save_tokens(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")

    def get_profile(self, sync_reason: int = 2):
        return {"displayName": "PinTester", "picturePath": "/a.jpg"}

    def close(self):
        self.closed = True


class _ExpiredOkLine(_PinOkLine):
    """扫码后 PIN 阶段超时——复刻事故现场那次失败。"""

    def qr_login(self, *, on_qr=None, on_pin=None, **kw):
        if on_qr:
            on_qr("line://qr/expired-case")
        if on_pin:
            on_pin(_PIN)
        raise RuntimeError(_PRODUCTION_QR_EXPIRED)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── 一、PIN 必须是独立状态，不能挤进 scanned ────────────────────────────────

def test_on_pin_enters_pin_needed_not_scanned():
    """scanned 是 Telegram 语义（等对方点确认，我方无信息要传递）；LINE 是我方持码等输入。

    两者的界面要求相反——挤在同一状态里就是这次事故的根。
    """
    state: dict = {}
    lpl._drive_qr_login(_PinOkLine(), state, {"platform_login": {"line": {
        "sessions_dir": tempfile.mkdtemp()}}})
    # 终态是 authorized；过程中必须经过 pin_needed，且 PIN 落在独立字段
    assert state["pin"] == _PIN


def test_pin_needed_state_is_reached_during_pin_phase():
    captured: list = []
    state: dict = {}

    class _Observing(_PinOkLine):
        def qr_login(self, *, on_qr=None, on_pin=None, **kw):
            if on_qr:
                on_qr("line://qr/x")
            if on_pin:
                on_pin(_PIN)
                captured.append(state.get("status"))
            return _FakeResult()

    lpl._drive_qr_login(_Observing(), state, {"platform_login": {"line": {
        "sessions_dir": tempfile.mkdtemp()}}})
    assert captured == ["pin_needed"], (
        f"on_pin 后应进入 pin_needed，实际 {captured}")


def test_pin_not_buried_in_detail_string():
    """PIN 走独立字段而非拼进 detail：detail 会被后续错误覆盖，PIN 塞里面会在第二阶段失败时消失。"""
    state: dict = {}

    class _StopAtPin(_PinOkLine):
        def qr_login(self, *, on_qr=None, on_pin=None, **kw):
            if on_pin:
                on_pin(_PIN)
            raise RuntimeError("stop here")

    lpl._drive_qr_login(_StopAtPin(), state, {})
    assert state.get("pin") == _PIN
    # 失败后 PIN 仍在独立字段里，而 detail 不承载 PIN
    assert _PIN not in str(state.get("detail") or "")


# ── 二、错误归类与不泄露实现细节 ────────────────────────────────────────────

def test_classify_maps_production_qr_expired_sample():
    assert lpl.classify_login_error(_PRODUCTION_QR_EXPIRED) == "qr_expired"


@pytest.mark.parametrize("text,expect", [
    ("", "login_failed"),
    (None, "login_failed"),
    ("HTTP 429 Too Many Requests", "rate_limited"),
    ("Connection refused", "network"),
    ("something entirely unexpected", "login_failed"),
])
def test_classify_covers_enum(text, expect):
    assert lpl.classify_login_error(text) == expect


def test_classify_only_returns_declared_codes():
    """reason_code 是跨层契约（前端按码取文案），冒出枚举外的值前端会回落成通用错误。"""
    allowed = {"qr_expired", "pin_timeout", "network", "rate_limited",
               "login_failed", "okline_missing", "client_init", "cancelled"}
    samples = [_PRODUCTION_QR_EXPIRED, "pin not verified", "timed out", "429",
               "ssl error", "", "boom", "flood wait"]
    for s in samples:
        assert lpl.classify_login_error(s) in allowed, f"越界 reason_code: {s}"


def test_failure_never_leaks_internals_to_ui():
    """thrift 路径/错误码既看不懂，又泄露了我们在用逆向协议——只能进日志，不能进界面。"""
    state: dict = {}
    lpl._drive_qr_login(_ExpiredOkLine(), state, {})
    assert state["status"] == "failed"
    assert state.get("reason_code") == "qr_expired"
    leaked = str(state.get("detail") or "")
    for needle in ("thrift", "path=", "code=", "/api/talk"):
        assert needle not in leaked, f"detail 泄露内部实现：{leaked}"


# ── 三、poll 返回体的跨层契约 ───────────────────────────────────────────────

def _run_provider(monkeypatch, client_cls):
    import okline
    monkeypatch.setattr(okline, "OkLine", client_cls)
    monkeypatch.setattr(lpl, "is_okline_available", lambda: True)
    d = tempfile.mkdtemp()
    monkeypatch.setattr(lpl, "get_account_registry",
                        lambda: AccountRegistry(os.path.join(d, "l.db")))
    return lpl.make_provider({"platform_login": {"line": {"sessions_dir": d}}})


def test_poll_returns_contract_fields(monkeypatch):
    """前端无条件读这几个键；缺字段会让状态机分支判断混乱。"""
    provider = _run_provider(monkeypatch, _PinOkLine)

    async def run():
        info = await provider(None, "line", "protocol", "")
        res = await info["poll"](None)
        for k in ("status", "account_id", "detail", "pin", "reason_code"):
            assert k in res, f"poll 返回体缺字段 {k}"

    asyncio.run(run())


def test_poll_clears_pin_once_authorized(monkeypatch):
    """PIN 是一次性凭据，登录完成后不该继续留在响应里。"""
    provider = _run_provider(monkeypatch, _PinOkLine)

    async def run():
        info = await provider(None, "line", "protocol", "")
        for _ in range(50):
            res = await info["poll"](None)
            if res["status"] == "authorized":
                break
            time.sleep(0.1)
        assert res["status"] == "authorized"
        assert res["pin"] == "", "authorized 后 pin 应清空"

    asyncio.run(run())


def test_poll_surfaces_reason_code_on_failure(monkeypatch):
    provider = _run_provider(monkeypatch, _ExpiredOkLine)

    async def run():
        info = await provider(None, "line", "protocol", "")
        for _ in range(50):
            res = await info["poll"](None)
            if res["status"] == "failed":
                break
            time.sleep(0.1)
        assert res["status"] == "failed"
        assert res["reason_code"] == "qr_expired"

    asyncio.run(run())


# ── 四、挂起态不被 TTL 误清 ─────────────────────────────────────────────────

def test_pin_needed_exempt_from_ttl():
    """用户在手机上读码、输 6 位、勾确认框，实测常超 180s TTL。

    判过期会让前端自动换码，把用户正在输入的那次流程作废。
    """
    sess = pl.LoginSession(login_id="x", platform="line", status="pin_needed")
    sess.created_at = time.time() - (pl.TTL_SEC * 3)
    assert sess.is_expired() is False


def test_pin_needed_survives_gc():
    """is_expired 与 _gc_locked 是同一个 bug 的两个入口，漏改任一个会话都会被清掉。"""
    mgr = pl.LoginManager()
    sess = mgr.create("line", "", set(), mode="protocol")
    sess.status = "pin_needed"
    sess.created_at = time.time() - (pl.TTL_SEC * 3)
    mgr.create("line", "", set(), mode="protocol")  # 触发 _gc_locked
    assert mgr.get(sess.login_id) is not None, "pin_needed 会话被 GC 误清"


def test_hold_state_is_bounded_not_immortal():
    """豁免 ≠ 永生：扫到一半关页面是常态，不封顶就按放弃次数线性泄漏会话+长连到重启。"""
    sess = pl.LoginSession(login_id="z", platform="line", status="pin_needed")
    sess.created_at = time.time() - (pl.HOLD_MAX_SEC + 60)
    assert sess.is_expired() is True, "滞留的 pin_needed 会话永不过期"

    mgr = pl.LoginManager()
    stale = mgr.create("line", "", set(), mode="protocol")
    stale.status = "pin_needed"
    stale.created_at = time.time() - (pl.HOLD_MAX_SEC + 60)
    mgr.create("line", "", set(), mode="protocol")  # 触发 _gc_locked
    assert mgr.get(stale.login_id) is None, "超期 pin_needed 会话未被 GC 回收"


def test_hold_bound_exceeds_provider_budget():
    """封顶必须大于 provider 的长轮询预算，否则会在用户还有机会成功时提前判死。"""
    assert pl.HOLD_MAX_SEC > 240, "HOLD_MAX_SEC 低于 okline wait_seconds，会误杀有效会话"


def test_ordinary_pending_still_expires():
    """豁免只针对挂起态，普通等待态该过期还得过期，别把 TTL 废掉。"""
    sess = pl.LoginSession(login_id="y", platform="line", status="pending")
    sess.created_at = time.time() - (pl.TTL_SEC * 3)
    assert sess.is_expired() is True


# ── 五、静态轨：字段声明了就必须有人消费 ────────────────────────────────────

def test_status_route_passes_pin_and_reason_code():
    src = _text(_ROUTES)
    for field in ('"pin"', '"reason_code"'):
        assert field in src, f"status 路由未透传 {field}，PIN 到不了前端"


def test_frontend_consumes_structured_pin():
    """事故根因的直接回归钉：前端必须读 d.pin，而不是从中文句子里抠。"""
    src = _text(_TEMPLATE)
    assert re.search(r"\bd\.pin\b", src), "前端未消费结构化 pin 字段"
    assert "pin_needed" in src, "前端缺 pin_needed 状态分支"


def test_frontend_has_branch_for_every_actionable_state():
    """任一状态没有前端分支 = 后端说了话没人听，正是这次事故的形态。

    pending 刻意不在列：它是初始态，由二维码渲染路径隐式覆盖，无需独立分支。
    """
    src = _text(_TEMPLATE)
    for st in ("scanned", "pin_needed", "password_needed",
               "authorized", "expired", "failed"):
        assert re.search(rf"st\s*===\s*['\"]{st}['\"]", src), \
            f"前端 pollConnect 缺 {st} 分支"


def test_setup_failure_reports_reason_code_without_poll(monkeypatch):
    """组件缺失是开局就已注定的失败，必须当场给码——而不是挂到 TTL 耗尽让用户白等三分钟。"""
    monkeypatch.setattr(lpl, "is_okline_available", lambda: False)

    async def run():
        info = await lpl.make_provider({})(None, "line", "protocol", "")
        assert info.get("reason_code") == "okline_missing"
        assert "poll" not in info, "致命故障不该留轮询钩子，否则会话空转到超时"

    asyncio.run(run())


def test_setup_failure_does_not_name_the_library():
    """面向用户的话术不点逆向库名；安装指引留在日志里给运维。"""
    src = _text(_ROOT / "src" / "integrations" / "line_protocol_login.py")
    provider_body = src.split("def make_provider(", 1)[1]
    for line in provider_body.splitlines():
        s = line.strip()
        if s.startswith("logger.") or s.startswith("#"):
            continue
        assert "pip install okline" not in s, f"用户可见串泄露库名：{s}"


def test_start_route_marks_fatal_setup_as_failed():
    src = _text(_ROUTES)
    assert "prov_reason" in src, "start 路由未消费 provider 的 reason_code"
    assert 'sess.status = "failed"' in src


# ── 六、漏斗观测：轮询去重 ──────────────────────────────────────────────────

def test_funnel_marks_dedupe_across_polls():
    """状态轮询 2.5s 一轮，不去重会把一次登录记成上百次，漏斗比例直接失真。"""
    from src.web.routes.unified_inbox_login_routes import _funnel
    from src.integrations.login_funnel_stats import LoginFunnelStats
    import src.integrations.login_funnel_stats as lfs

    probe = LoginFunnelStats()
    monkey = lfs._stats
    lfs._stats = probe
    try:
        sess = pl.LoginSession(login_id="f", platform="line", mode="protocol")
        for _ in range(50):
            _funnel(sess, "pin_issued")
        row = next(r for r in probe.dump()["rows"] if r["key"] == "line:protocol")
        assert row["pin_issued"] == 1
    finally:
        lfs._stats = monkey


def test_frontend_maps_every_reason_code():
    """8 个码都要有本地化文案，否则用户又会看到一句看不懂的通用错误。"""
    src = _text(_TEMPLATE)
    for code in ("qr_expired", "pin_timeout", "network", "rate_limited",
                 "login_failed", "okline_missing", "client_init", "cancelled"):
        assert code in src, f"前端未映射 reason_code={code}"


@pytest.mark.parametrize("path", _CONSUMERS, ids=lambda p: p.name)
def test_every_status_consumer_handles_pin(path):
    """每个消费 login/status 的前端都得认 pin_needed + 结构化 pin 字段。

    只守 templates 是不够的——出货副驾是客户实际用的壳，它漏一个分支，
    修好的 PIN 一样到不了用户眼前。
    """
    src = _text(path)
    assert "pin_needed" in src, f"{path.name} 缺 pin_needed 分支"
    assert re.search(r"\.pin\b", src), f"{path.name} 未消费结构化 pin 字段"
    assert "reason_code" in src, f"{path.name} 未按 reason_code 出本地化文案"


def test_copilot_mirror_stays_in_sync():
    """桌面壳是 shared/ 的字节级镜像；改了一边忘另一边 = 桌面端行为静默回退。"""
    for rel in ("copilot/components/cp-accounts.js", "copilot/i18n/cp-i18n.js"):
        a = (_ROOT / "shared" / rel).read_bytes()
        b = (_ROOT / "desktop" / "renderer" / "shared" / rel).read_bytes()
        assert a == b, f"{rel} 两份副本不一致，请同步 desktop/renderer 镜像"


def test_cancel_after_authorized_keeps_client_alive(monkeypatch):
    """已授权还去 cancel，绝不能把号踢下线。

    出货副驾就在 authorized 分支里调 _stopPoll()→cancelLogin；客户端此时已交给
    LineProtocolWorker 收发，close() = 刚登上就掉线。
    """
    made = []

    class _Capturing(_PinOkLine):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    provider = _run_provider(monkeypatch, _Capturing)

    async def go():
        info = await provider(None, "line", "protocol", "")
        state = info["state"]
        for _ in range(60):
            if state.get("status") == "authorized":
                break
            await asyncio.sleep(0.05)
        assert state.get("status") == "authorized", "夹具未走到授权态"
        await info["cancel"](None)
        assert state["status"] == "authorized", "已授权状态被 cancel 改写"

    asyncio.run(go())
    assert made, "未捕获到客户端实例"
    assert not made[0].closed, "已授权的会话被 cancel 关掉了客户端（号会当场掉线）"


def test_failed_login_with_certificate_discards_it(tmp_path):
    """带证书登录失败必须丢弃证书，否则每次复登都带同一张坏牌 → 永久登不上。"""
    config = {"platform_login": {"line": {"sessions_dir": str(tmp_path)}}}
    mid = "u_cert_test"
    path = lpl.cert_path(config, mid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("stale-cert")

    class _C:
        def qr_login(self, **kw):
            raise RuntimeError("Your QR code has expired. code=100")

        def close(self):
            pass

    state = {"status": "pending", "qr_image": "", "pin": "", "mid": "",
             "name": "", "avatar_url": "", "detail": "", "reason_code": ""}
    lpl._drive_qr_login(_C(), state, config, mid)
    assert state["status"] == "failed"
    assert not os.path.exists(path), "失败后坏证书仍在，下轮复登会再次撞同一堵墙"


def test_certificate_unsupported_falls_back_to_plain_login(tmp_path):
    """okline 版本不认 certificate 形参时，摘掉重试而不是让登录整条死掉。"""
    config = {"platform_login": {"line": {"sessions_dir": str(tmp_path)}}}
    mid = "u_cert_unsupported"
    path = lpl.cert_path(config, mid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("some-cert")

    seen = []

    class _R:
        success = True
        mid = "u_cert_unsupported"
        certificate = "fresh"
        display_message = ""

    class _C:
        def qr_login(self, **kw):
            seen.append("certificate" in kw)
            if "certificate" in kw:
                raise TypeError("qr_login() got an unexpected keyword argument 'certificate'")
            return _R()

        def save_tokens(self, p):
            pass

        def get_profile(self):
            return {}

        def close(self):
            pass

    state = {"status": "pending", "qr_image": "", "pin": "", "mid": "",
             "name": "", "avatar_url": "", "detail": "", "reason_code": ""}
    lpl._drive_qr_login(_C(), state, config, mid)
    assert seen == [True, False], "未在 TypeError 后摘掉 certificate 重试"
    assert state["status"] == "authorized", "证书降级路径没能正常登录"
