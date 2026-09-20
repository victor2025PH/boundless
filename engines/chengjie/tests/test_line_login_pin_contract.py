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

# 2026-08-24 实录（transcript login_qrfail_20260824_032405.log）：扫码 20s + 输码 11s 全对，
# qrCodeLoginV2 仍被拒——LINE 的 code=100 是通用码，同时盖「QR 过期」与「服务端临时拒绝」，
# 归类必须靠文案分流，否则用户被指去「重新扫码/动作快点」，把风控冷却越撞越长。
_PRODUCTION_TEMP_UNAVAILABLE = (
    "Verification is temporarily unavailable.\nPlease try again later. "
    "code=100 path=/api/talk/thrift/LoginQrCode/SecondaryQrCodeLoginService/qrCodeLoginV2"
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
    # code=100 双语义分流：带「temporarily unavailable」的必须先于 code=100 规则命中
    (_PRODUCTION_TEMP_UNAVAILABLE, "temp_unavailable"),
    (_PRODUCTION_QR_EXPIRED, "qr_expired"),
])
def test_classify_covers_enum(text, expect):
    assert lpl.classify_login_error(text) == expect


def test_classify_only_returns_declared_codes():
    """reason_code 是跨层契约（前端按码取文案），冒出枚举外的值前端会回落成通用错误。"""
    allowed = {"qr_expired", "pin_timeout", "network", "rate_limited",
               "login_failed", "okline_missing", "client_init", "cancelled",
               "temp_unavailable"}
    samples = [_PRODUCTION_QR_EXPIRED, _PRODUCTION_TEMP_UNAVAILABLE,
               "pin not verified", "timed out", "429",
               "ssl error", "", "boom", "flood wait"]
    for s in samples:
        assert lpl.classify_login_error(s) in allowed, f"越界 reason_code: {s}"


def test_temp_unavailable_not_remapped_by_pin_phase():
    """服务端临时拒绝不参与「PIN 阶段过期→pin_timeout」的改判。

    2026-08-24 实录就是 PIN 已签发、验证也通过、最后一步被拒——把它说成「验证码超时，
    这次动作快点」同样是指错路（用户 11 秒就输完了，快慢不是问题）。
    """

    class _TempUnavail(_PinOkLine):
        def qr_login(self, *, on_qr=None, on_pin=None, **kw):
            if on_qr:
                on_qr("line://qr/temp-unavail")
            if on_pin:
                on_pin(_PIN)
            raise RuntimeError(_PRODUCTION_TEMP_UNAVAILABLE)

    state: dict = {}
    lpl._drive_qr_login(_TempUnavail(), state, {})
    assert state["status"] == "failed"
    assert state["reason_code"] == "temp_unavailable"


def test_failure_never_leaks_internals_to_ui():
    """thrift 路径/错误码既看不懂，又泄露了我们在用逆向协议——只能进日志，不能进界面。

    注意：_ExpiredOkLine 已签发 PIN，阶段感知归因把 qr_expired 修正为 pin_timeout
    （见 test_pin_phase_expiry_reclassified_to_pin_timeout）；本测试只守「不泄露」。
    """
    state: dict = {}
    lpl._drive_qr_login(_ExpiredOkLine(), state, {})
    assert state["status"] == "failed"
    assert state.get("reason_code") == "pin_timeout"
    leaked = str(state.get("detail") or "")
    for needle in ("thrift", "path=", "code=", "/api/talk"):
        assert needle not in leaked, f"detail 泄露内部实现：{leaked}"


def test_pin_phase_expiry_reclassified_to_pin_timeout():
    """PIN 已签发后网关报「QR expired」＝会话在**验证码环节**到期。

    用户视角他扫码成功了——再报「二维码超时」等于指错路（2026-08-24 实录截图：
    扫完码、输完验证码，界面却让他「重新获取二维码」）。classify 保持纯文本归类，
    阶段修正在 _mark_failed 收口。
    """
    state: dict = {}
    lpl._drive_qr_login(_ExpiredOkLine(), state, {})
    assert state["status"] == "failed"
    assert state["reason_code"] == "pin_timeout"
    # 归因改了，但 PIN 字段仍在（前端失败态要保留「输验证码」语境）
    assert state["pin"] == _PIN


def test_qr_expiry_before_pin_stays_qr_expired():
    """没扫码就过期仍归 qr_expired——阶段修正只认「PIN 已签发」这个事实，不许扩大化。"""

    class _NoPinExpired(_PinOkLine):
        def qr_login(self, *, on_qr=None, on_pin=None, **kw):
            if on_qr:
                on_qr("line://qr/x")
            raise RuntimeError(_PRODUCTION_QR_EXPIRED)

    state: dict = {}
    lpl._drive_qr_login(_NoPinExpired(), state, {})
    assert state["status"] == "failed"
    assert state["reason_code"] == "qr_expired"


def test_cancelled_terminal_not_reclassified():
    """用户取消后线程随后抛出的过期异常不得改写 cancelled 终态（阶段修正路径同样要让行）。"""
    state = {"reason_code": "cancelled", "status": "failed", "pin": _PIN,
             "pin_issued_at": time.time()}
    lpl._mark_failed(state, "qr_expired")
    assert state["reason_code"] == "cancelled"


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
        # _ExpiredOkLine 已签发 PIN → 阶段感知归因为 pin_timeout
        assert res["reason_code"] == "pin_timeout"

    asyncio.run(run())


def test_poll_reports_pin_expiry_countdown(monkeypatch):
    """pin_needed 期间 poll 必须带 pin_expires_in（服务端按签发时刻推算的权威倒计时）。

    没有它，前端只能用本地写死常量——旧值 240s 比网关真实窗口（实测 ~120s 即回过期）
    乐观一倍，倒计时走到一半码就死了。授权后该字段归 0（PIN 生命周期结束）。
    """
    import threading as _th
    release = _th.Event()

    class _SlowPin(_PinOkLine):
        def qr_login(self, *, on_qr=None, on_pin=None, **kw):
            if on_qr:
                on_qr("line://qr/slow")
            if on_pin:
                on_pin(_PIN)
            release.wait(5.0)
            return _FakeResult()

    provider = _run_provider(monkeypatch, _SlowPin)

    async def run():
        info = await provider(None, "line", "protocol", "")
        res = None
        for _ in range(50):
            res = await info["poll"](None)
            if res["status"] == "pin_needed":
                break
            time.sleep(0.1)
        assert res is not None and res["status"] == "pin_needed"
        assert 0 < res["pin_expires_in"] <= lpl._PIN_WINDOW_SEC, (
            f"pin_needed 期间应回剩余秒（0 < x <= {lpl._PIN_WINDOW_SEC}），实际 {res['pin_expires_in']}")
        release.set()
        for _ in range(50):
            res = await info["poll"](None)
            if res["status"] == "authorized":
                break
            time.sleep(0.1)
        assert res["status"] == "authorized"
        assert res["pin_expires_in"] == 0, "授权后 pin_expires_in 应归 0"

    asyncio.run(run())


def test_pin_window_stays_below_hold_budget():
    """展示窗口只能比后台预算紧、不能松：窗口 > wait_seconds(240) 意味着倒计时还在走、
    后台线程早已放弃——回到「盯着假倒计时白等」的旧病。"""
    assert 0 < lpl._PIN_WINDOW_SEC <= 240


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
    for field in ('"pin"', '"reason_code"', '"pin_expires_in"'):
        assert field in src, f"status 路由未透传 {field}，PIN/倒计时到不了前端"


def test_frontend_consumes_structured_pin():
    """事故根因的直接回归钉：前端必须读 d.pin，而不是从中文句子里抠。"""
    src = _text(_TEMPLATE)
    assert re.search(r"\bd\.pin\b", src), "前端未消费结构化 pin 字段"
    assert "pin_needed" in src, "前端缺 pin_needed 状态分支"


def test_frontend_consumes_pin_expiry_countdown():
    """倒计时必须以服务端 pin_expires_in 为准——两套时钟各说各话是 2026-08-24 实录的病根：
    前端写死 240s，网关 ~120s 就判死，用户盯着「剩余 1:30」白输。"""
    src = _text(_TEMPLATE)
    assert "pin_expires_in" in src, "前端未消费 pin_expires_in（倒计时仍用本地写死常量）"


def test_frontend_pin_timeout_has_specific_status():
    """失败短状态：pin_timeout 必须映射到专属短语（验证码验证超时），
    而不是让遮罩/状态行/卡头三处齐喊「登录失败」把诊断藏起来。"""
    src = _text(_TEMPLATE).replace(" ", "")
    assert "pin_timeout:'inbox.connect.st_pin_timeout'" in src, \
        "_CONNECT_FAIL_STATUS_KEYS 缺 pin_timeout 专属短状态"


def test_pin_phase_copy_is_bilingual():
    """PIN 阶段新增键 zh/en 双语齐备（_CONNECT_FAIL_STATUS_KEYS 等经变量取键，
    window.T 静态门禁扫不到，这里补上）。"""
    from src.web.web_i18n import get_translations

    keys = ("inbox.connect.st_pin_timeout", "inbox.connect.st_pin_maybe_expired",
            "inbox.connect.hint_pin_maybe_expired", "inbox.connect.fail_retry_pointer",
            "inbox.connect.hint_pin_short", "inbox.connect.pin_step1",
            "inbox.connect.pin_step2", "inbox.connect.pin_step3",
            "inbox.connect.st_temp_unavailable")
    missing = []
    for key in keys:
        for lang in ("zh", "en"):
            if not str(get_translations(lang).get(key) or "").strip():
                missing.append(f"[{lang}] {key}")
    assert not missing, f"PIN 阶段键缺文案: {missing}"


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
    """每个契约原因码都要有本地化文案，否则用户又会看到一句看不懂的通用错误。

    **判据从后端枚举推导**（不再手抄一份清单）：原因码是三处同源的契约——
    ``login_funnel_stats._REASON_CODES`` / 前端 ``_CONNECT_FAIL_REASON_KEYS`` /
    i18n ``inbox.connect.err_<code>``。手抄清单的版本会随新增码悄悄漏掉，
    而漏掉的代价正是「用户看到一句看不懂的通用错误」——本门禁存在的全部理由。
    """
    from src.integrations.login_funnel_stats import _REASON_CODES
    from src.web.web_i18n import get_translations

    src = _text(_TEMPLATE).replace(" ", "")
    missing_map, missing_copy = [], []
    for code in sorted(_REASON_CODES):
        if f"{code}:'inbox.connect.err_{code}'" not in src:
            missing_map.append(code)
        for lang in ("zh", "en"):
            if not str(get_translations(lang).get(f"inbox.connect.err_{code}") or "").strip():
                missing_copy.append(f"[{lang}] inbox.connect.err_{code}")
    assert not missing_map, f"前端 _CONNECT_FAIL_REASON_KEYS 未映射: {missing_map}"
    assert not missing_copy, f"原因码缺文案: {missing_copy}"


def test_hosted_live_hint_codes_are_wired():
    """托管登录的**实时**提示码（hint_code）同样要三处同源。

    它与 reason_code 共用词汇但语义不同（非终态、可来回变），所以单独钉：
    Node 侧 ``login_classify.actionableCode`` 只放行这三个码，前端要认得、且
    zh/en 都要有「此刻该做什么」的文案——否则坐席看到的仍是一句恒定的
    「已打开登录窗口」，而服务器窗口里早就卡在验证码那一屏。
    """
    from src.integrations.login_funnel_stats import _REASON_CODES
    from src.web.web_i18n import get_translations

    live = ("two_factor", "checkpoint", "password_error")
    src = _text(_TEMPLATE).replace(" ", "")
    missing = []
    for code in live:
        # 实时码必须同时是合法原因码：窗口期结束时它会被当失败归因落进漏斗
        assert code in _REASON_CODES, f"{code} 不在 _REASON_CODES 里，落漏斗会被归成 login_failed"
        if f"{code}:{{st:'inbox.connect.st_live_" not in src:
            missing.append(f"_CONNECT_LIVE_HINT_KEYS[{code}]")
    for suffix in ("st_live_2fa", "hint_live_2fa", "st_live_checkpoint",
                   "hint_live_checkpoint", "st_live_pwderr", "hint_live_pwderr"):
        for lang in ("zh", "en"):
            if not str(get_translations(lang).get(f"inbox.connect.{suffix}") or "").strip():
                missing.append(f"[{lang}] inbox.connect.{suffix}")
    assert not missing, f"托管登录实时提示未接通: {missing}"


def test_node_classifier_vocabulary_matches_python_enum():
    """Node 侧分类器的输出词汇必须落在 Python 原因码枚举内。

    两个仓内子系统（Python 主进程 / Node 微服务）之间没有类型系统兜底，
    Node 那边改个词、Python 这边 ``_san_reason`` 就默默把它归成 login_failed——
    漏斗上看不出任何异常，归因却已经废了。故做纯文本级钉子。
    """
    from src.integrations.login_funnel_stats import _REASON_CODES

    js = _text(_ROOT / "services" / "messenger-web" / "login_classify.js")
    declared = set(re.findall(r'^\s*(?:TWO_FACTOR|CHECKPOINT|PASSWORD_ERROR):\s*"([a-z_]+)"',
                              js, re.M))
    assert declared, "login_classify.js 的 STAGE 常量表结构变了，门禁已失效"
    unknown = sorted(declared - _REASON_CODES)
    assert not unknown, f"Node 分类器输出了 Python 枚举里没有的码: {unknown}"


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


# ── 七、长轮询 read timeout 竞态（2026-07-25 真机事故根因）────────────────────

def test_login_client_lifts_http_timeout_above_longpoll_window():
    """okline QR 长轮询把网关 hold 窗口(X-LST=interval*1000，实测 30s)与底层 HTTP read
    timeout(LineConfig.timeout 默认 30.0)取成同一个值 → 两者同时到期是竞态：网络稍慢
    requests 先抛 ``Read timed out``，而它不是 okline ``_poll`` 认的 408/410 翻页信号
    → 不重试 → 整条扫码登录在任意一轮长轮询上随机暴毙（用户根本进不了 PIN）。

    登录实例必须把 HTTP read timeout 抬到明显大于单轮长轮询窗口，长轮询才能靠网关正常
    408/410 续等。这是那次事故的直接回归钉。
    """
    captured: dict = {}

    class _CaptureCfg:
        def __init__(self, *a, config=None, **k):
            captured["config"] = config

    client = lpl._build_okline(_CaptureCfg)
    assert isinstance(client, _CaptureCfg)
    cfg = captured.get("config")
    assert cfg is not None, "登录实例未注入自定义 LineConfig（会退回默认 30s 竞态）"
    # 服务端实测 longPollingIntervalSec=150（X-LST=150000），且曾在 120.3s 才回过期响应；
    # 取 180 下限＝必须盖过 150s 窗口并留缓冲，否则换个网关参数竞态立刻复发。
    assert getattr(cfg, "timeout", 0) >= 180.0, (
        f"HTTP read timeout={getattr(cfg, 'timeout', None)} 未抬过长轮询窗口，竞态仍在")


def test_build_okline_falls_back_when_config_rejected():
    """okline 将来换构造签名(不认 config) → 回落无参构造，退回老行为而非把登录整条打死。"""
    calls: list = []

    class _PickyClient:
        def __init__(self, *a, **k):
            if "config" in k:
                calls.append("with_config")
                raise TypeError("unexpected kwarg config")
            calls.append("plain")

    client = lpl._build_okline(_PickyClient)
    assert isinstance(client, _PickyClient)
    assert calls == ["with_config", "plain"], f"未在 config 被拒后回落无参构造：{calls}"


# ── 八、空证书探测不可跳过（2026-07-25 真机 transcript 实证的反向门禁）──────────

class _ProbeAuth:
    def __init__(self) -> None:
        self.sent: list = []

    def qr_verify_certificate(self, auth_session_id, certificate=""):
        self.sent.append(certificate)
        return {"ok": True}


class _ProbeClient:
    def __init__(self, *a, **k) -> None:
        self.auth = _ProbeAuth()


def test_empty_certificate_probe_must_not_be_skipped():
    """okline qr_login 里那次「空证书 verifyCertificate」是协议必需的一步，别当多余试探删掉。

    它必然回 code=2「The verification code you entered is incorrect」，看着像白跑一趟，
    但服务端靠它推进 secondary-login 状态机：2026-07-25 曾把空证书就地拦下不发，结果
    紧随其后的 createPinCode 直接 code=100「QR code has expired」，PIN 根本签不出来
    （用户侧表现＝扫完码直接失败、连验证码都不显示）。这条门禁防同样的"优化"再来一次。
    """
    client = lpl._build_okline(_ProbeClient)
    client.auth.qr_verify_certificate("SQ_TEST", "")
    assert client.auth.sent == [""], (
        "空证书探测被拦下 → 服务端状态机不推进，createPinCode 必以 QR expired 失败")


def test_real_certificate_probe_untouched():
    """回访设备(有证书)照常探 verifyCertificate —— 那是免 PIN 直登的正路。"""
    client = lpl._build_okline(_ProbeClient)
    assert client.auth.qr_verify_certificate("SQ_TEST", "REAL_CERT_BLOB") == {"ok": True}
    assert client.auth.sent == ["REAL_CERT_BLOB"]
