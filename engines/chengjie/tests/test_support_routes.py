# -*- coding: utf-8 -*-
"""客服支持通道门禁（实施49 P1-9）。

覆盖三层：
1. ``/api/support/info`` 契约——机器码/版本/产品/直传能力位；机器码取不到时为
   **空串**（前端据此隐藏该行，绝不显示假机器码）。
2. ``/api/support/diag-upload`` 三态——probe 探活 / 成功回 6 位短码 / 官网不可达
   如实回 ``ok:false`` **且不抛**（旧 admin 实现在这条分支上是 NameError→500）。
3. 反漂移静态契约——坐席被 ``/api/admin/*`` 挡住故 support 前缀必须在坐席白名单里；
   admin 侧诊断直传必须继续走同一份实现（两份实现＝客服拿到的包不等价）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.support_routes import register_support_routes


class _Cfg:
    config = {"brand": {"product_name": "智聊"}}
    config_path = Path("D:/nowhere/instances/zhiliao/data/config/config.yaml")


def _app(**over):
    app = FastAPI()
    ctx = types.SimpleNamespace(
        api_auth=lambda r: None, config_manager=over.get("cm", _Cfg()))
    register_support_routes(app, ctx)
    return app


# ── /api/support/info ────────────────────────────────────────────────────────

def test_info_exposes_machine_code_and_capability(monkeypatch):
    import src.utils.diag_upload as du
    monkeypatch.setattr(du, "machine_code", lambda: "ABCD-1234-EF56-7890")
    r = TestClient(_app()).get("/api/support/info")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["machine_code"] == "ABCD-1234-EF56-7890"
    assert d["upload"] is True
    assert d["product"] == "智聊"
    assert d["site"] == "https://bd2026.cc"
    assert "ai26.sbs" not in d["site"]
    assert d["version"]  # 源码态是 "dev"，发布态由 launcher 注入；面板展示层会换成壳版本


def test_info_upload_flag_tracks_real_capability(monkeypatch):
    """``upload`` 必须是**探测结果**而不是常量 True。

    精简装机漏带 `diagnostic_bundle` 时后端根本打不出包；此时前端要的是「这版不支持、
    请复制信息发客服」，而不是一个点了必失败的主按钮。常量 True 会让这条路径永远说谎。
    """
    import importlib.util as iu

    real = iu.find_spec

    def _no_bundle(name, *a, **kw):
        if name == "src.utils.diagnostic_bundle":
            return None
        return real(name, *a, **kw)

    monkeypatch.setattr(iu, "find_spec", _no_bundle)
    assert TestClient(_app()).get("/api/support/info").json()["upload"] is False


def test_info_machine_code_empty_when_licensing_absent(monkeypatch):
    """licensing 模块缺席（源码态/精简包）→ 空串，不得编造占位机器码。"""
    import src.utils.diag_upload as du
    monkeypatch.setattr(du, "machine_code", lambda: "")
    d = TestClient(_app()).get("/api/support/info").json()
    assert d["ok"] is True and d["machine_code"] == ""


# ── /api/support/diag-upload ─────────────────────────────────────────────────

def test_diag_upload_probe_is_cheap():
    """probe 只回 {ok}——绝不能顺手打包上传（前端每次开面板都会探）。"""
    called = {"n": 0}

    async def _boom(_cm):
        called["n"] += 1
        return {"ok": True, "code": "000000"}

    import src.utils.diag_upload as du
    orig = du.build_and_upload
    du.build_and_upload = _boom
    try:
        d = TestClient(_app()).post("/api/support/diag-upload?probe=1").json()
    finally:
        du.build_and_upload = orig
    assert d == {"ok": True} and called["n"] == 0


def test_diag_upload_returns_short_code(monkeypatch):
    async def _ok(_cm, note=""):
        return {"ok": True, "code": "482913"}

    import src.utils.diag_upload as du
    monkeypatch.setattr(du, "build_and_upload", _ok)
    d = TestClient(_app()).post("/api/support/diag-upload").json()
    assert d == {"ok": True, "code": "482913"}


def test_diag_upload_forwards_note_and_survives_bodyless_call(monkeypatch):
    """报障现场（页面 + 报错文案）必须透传到打包器。

    这条 note 的全部价值＝客服**不用先问「你当时在做什么」**：从红条点上来时前端带着
    报错原文。同时「面板按钮不带 body」是合法调用（无 body / 非 JSON 都不该 500），
    两个形态一起钉住。
    """
    seen = {}

    async def _cap(_cm, note=""):
        seen["note"] = note
        return {"ok": True, "code": "111111"}

    import src.utils.diag_upload as du
    monkeypatch.setattr(du, "build_and_upload", _cap)
    c = TestClient(_app())

    assert c.post("/api/support/diag-upload",
                  json={"note": "/workspace | 加载失败"}).json()["ok"] is True
    assert seen["note"] == "/workspace | 加载失败"

    seen.clear()
    assert c.post("/api/support/diag-upload").json()["ok"] is True
    assert seen["note"] == ""


def test_diag_upload_upstream_down_is_reported_not_raised(monkeypatch):
    """官网不可达 → 200 + ok:false + 人话文案；**不是** 500。

    旧 admin 实现在这条分支调 tr() 而模块没导入它 → NameError → 500，
    恰好在用户最需要看懂错误的那一刻给一堆栈。
    """
    async def _down(_cm, note=""):
        return {"ok": False, "error": "upstream_unreachable"}

    import src.utils.diag_upload as du
    monkeypatch.setattr(du, "build_and_upload", _down)
    r = TestClient(_app()).post("/api/support/diag-upload")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["detail"] and "upstream_unreachable" not in d["detail"]


# ── src.utils.diag_upload 单元 ───────────────────────────────────────────────

def _stub_bundle(monkeypatch):
    mod = types.ModuleType("src.utils.diagnostic_bundle")
    mod.build_diagnostic_bundle = lambda **kw: b"PK\x03\x04stub"
    monkeypatch.setitem(sys.modules, "src.utils.diagnostic_bundle", mod)


def test_dirs_resolved_from_config_path():
    from src.utils.diag_upload import resolve_diag_dirs
    cfg_dir, logs_dir = resolve_diag_dirs(_Cfg())
    assert cfg_dir.name == "config" and logs_dir.name == "logs"
    assert logs_dir.parent == cfg_dir.parent


def test_dirs_never_raise_on_broken_config_manager():
    from src.utils.diag_upload import resolve_diag_dirs
    assert resolve_diag_dirs(None) == (None, None)
    assert resolve_diag_dirs(types.SimpleNamespace()) == (None, None)


@pytest.mark.asyncio
async def test_build_and_upload_sends_machine_code_header(monkeypatch):
    _stub_bundle(monkeypatch)
    import src.utils.diag_upload as du
    monkeypatch.setattr(du, "machine_code", lambda: "AAAA-BBBB-CCCC-DDDD")
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "code": "123456"}'

    def _urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["meta"] = req.headers.get("X-diag-meta") or req.headers.get("x-diag-meta")
        seen["body"] = req.data
        return _Resp()

    monkeypatch.setattr(du.urllib.request, "urlopen", _urlopen)
    out = await du.build_and_upload(_Cfg())
    assert out == {"ok": True, "code": "123456"}
    assert seen["url"].endswith("/api/diag-upload")
    assert "AAAA-BBBB-CCCC-DDDD" in seen["meta"]
    assert seen["body"] == b"PK\x03\x04stub"


@pytest.mark.asyncio
async def test_build_and_upload_network_error_maps_to_error_code(monkeypatch, tmp_path):
    _stub_bundle(monkeypatch)
    import src.utils.diag_upload as du

    def _boom(req, timeout=0):
        raise OSError("no route to host")

    # 实施86 域A-2① 起 unreachable 会落 outbox（staged 键）——断言跟上契约；
    # 落盘路径重定向 tmp（旧 _Cfg 的 D:/nowhere 会被 stage_bundle mkdir 真建出来）。
    class _TmpCfg:
        config_path = str(tmp_path / "data" / "config" / "config.yaml")
        config = {}

    monkeypatch.setattr(du.urllib.request, "urlopen", _boom)
    out = await du.build_and_upload(_TmpCfg())
    assert out["ok"] is False
    assert out["error"] == "upstream_unreachable"
    assert out["staged"] is True   # A-2①：长断网必暂存（网络恢复自动补传）


@pytest.mark.asyncio
async def test_build_and_upload_rejects_codeless_success(monkeypatch):
    """官网回 ok 但没短码 = 客服查不到包，按失败处理（别让用户念一个空号）。"""
    _stub_bundle(monkeypatch)
    import src.utils.diag_upload as du

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    monkeypatch.setattr(du.urllib.request, "urlopen", lambda *a, **k: _Resp())
    out = await du.build_and_upload(_Cfg())
    assert out["ok"] is False and out["error"] == "upload_failed"


def test_sanitize_note_is_single_line_and_capped():
    """note 直接来自报错 toast 的文本——出问题那一刻 toast 里可能是整段响应体。

    没有硬上限＝把响应体（可能含业务数据）搬进诊断包，所以截断是**隐私护栏**
    而不是排版偏好；换行折成空格是为了让它在元数据一行里可读。
    """
    from src.utils.diag_upload import NOTE_MAX_CHARS, sanitize_note

    assert sanitize_note(None) == ""
    assert sanitize_note("  /workspace \r\n 加载失败  ") == "/workspace 加载失败"
    long = sanitize_note("x" * (NOTE_MAX_CHARS + 500))
    assert len(long) == NOTE_MAX_CHARS


@pytest.mark.asyncio
async def test_note_travels_into_bundle_meta_and_header(monkeypatch):
    """note 必须同时进 ①包内元数据（客服解包即见）②上传头（官网侧可检索）。

    只进其中一处都会让「不用问用户当时在干嘛」这个唯一价值折半。
    """
    import src.utils.diag_upload as du
    seen = {}

    def _fake_bundle(config_dir=None, logs_dir=None, meta=None, **_kw):
        seen["meta"] = dict(meta or {})
        return b"PK\x03\x04stub"

    import src.utils.diagnostic_bundle as db
    monkeypatch.setattr(db, "build_diagnostic_bundle", _fake_bundle)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "code": "999999"}'

    def _urlopen(req, timeout=0):
        seen["hdr"] = req.headers.get("X-diag-meta") or req.headers.get("x-diag-meta")
        return _Resp()

    monkeypatch.setattr(du.urllib.request, "urlopen", _urlopen)
    out = await du.build_and_upload(_Cfg(), note="/workspace | 草稿加载失败")
    assert out["ok"] is True
    assert seen["meta"].get("user_note") == "/workspace | 草稿加载失败"
    # 2026-09-15 修正：头必须 latin-1 可编码（HTTP 契约），中文以 JSON \\u 转义携带、
    # 服务端 json.loads 还原——旧断言「原样中文在头里」恰恰把 UnicodeEncodeError 钉成了规范。
    seen["hdr"].encode("latin-1")
    import json as _json
    assert "草稿加载失败" in _json.loads(seen["hdr"])["note"]


# ── 反漂移静态契约 ───────────────────────────────────────────────────────────

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_agent_role_can_reach_support_namespace():
    """坐席是报障主力：``/api/support`` 必须在 ROLE_AGENT 白名单内。

    诊断直传原先只挂 ``/api/admin/*`` → 坐席点了必 403，「一键上传给客服」
    对最需要它的人是死按钮。
    """
    src = (_SRC / "web" / "admin.py").read_text(encoding="utf-8")
    body = src.split("def _agent_api_allowed", 1)[1].split("def _require_auth", 1)[0]
    assert '"/api/support"' in body
    # 反向：不得为了省事把整个 admin 前缀开给坐席
    assert '"/api/admin"' not in body


def test_admin_diag_upload_still_shares_one_implementation():
    """admin 与 support 两个入口必须共用 diag_upload——两份实现＝包不等价。"""
    src = (_SRC / "web" / "routes" / "ops_overview_routes.py").read_text(
        encoding="utf-8")
    seg = src.split("/api/admin/diagnostic-upload", 1)[1].split(
        "@app.get(\"/api/admin/media-consistency\")", 1)[0]
    assert "from src.utils.diag_upload import build_and_upload" in seg
    assert "urllib" not in seg, "诊断直传的 HTTP 细节应只存在于 src/utils/diag_upload.py"


def test_support_panel_shows_shell_version_and_current_site():
    """求助面板不得把引擎身份 ``dev`` 或 2026-07 防封镜像当给人看的版本/官网。"""
    src = (_SRC / "web" / "templates" / "_support.html").read_text(encoding="utf-8")
    assert "menuSpec" in src
    assert "_displayVersion" in src
    assert "_publicSite" in src
    assert "https://bd2026.cc" in src
    assert "ai26.sbs" in src  # 过期镜像改写必须留下，删了热更窗口又会露出旧址


def test_upstream_unreachable_key_is_bilingual():
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        assert get_translations(lang).get("err.svc.upstream_unreachable")


# ── B53 传输链三改（实施64 P1-6，2026-08-23）────────────────────────────────

def test_classify_upload_error_splits_rejected_from_unreachable():
    import io
    import urllib.error
    from src.utils.diag_upload import classify_upload_error
    http413 = urllib.error.HTTPError(
        "https://x/api/diag-upload", 413, "Payload Too Large", {},
        io.BytesIO(b""))
    assert classify_upload_error(http413) == "upload_rejected_413"
    assert classify_upload_error(OSError("no route")) == "upstream_unreachable"
    assert classify_upload_error(TimeoutError()) == "upstream_unreachable"


@pytest.mark.asyncio
async def test_upload_retries_once_then_succeeds(monkeypatch):
    _stub_bundle(monkeypatch)
    import src.utils.diag_upload as du
    monkeypatch.setattr(du, "RETRY_DELAY_SEC", 0.0)
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "code": "654321"}'

    def _flaky(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return _Resp()

    monkeypatch.setattr(du.urllib.request, "urlopen", _flaky)
    out = await du.build_and_upload(_Cfg())
    assert out == {"ok": True, "code": "654321"}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_full_bundle_blocked_degrades_to_mini(monkeypatch):
    """egress 掐大 POST（skuio 实锤形态）：全尺寸两连败 → mini 包送达 + mini 标记。"""
    import sys as _sys
    import types as _types
    sizes = []

    def _bundle(**kw):
        # mini 包判据：config_dir/logs_dir 均为 None（meta+fatal 形态）
        mini = kw.get("config_dir") is None and kw.get("logs_dir") is None
        blob = b"MINI" if mini else b"FULL" * 4096
        sizes.append(len(blob))
        return blob

    mod = _types.ModuleType("src.utils.diagnostic_bundle")
    mod.build_diagnostic_bundle = _bundle
    monkeypatch.setitem(_sys.modules, "src.utils.diagnostic_bundle", mod)
    import src.utils.diag_upload as du
    monkeypatch.setattr(du, "RETRY_DELAY_SEC", 0.0)
    seen_bodies = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "code": "777777"}'

    def _blocking(req, timeout=0):
        seen_bodies.append(req.data)
        if len(req.data) > 1024:   # 大包全掐，小包放行
            raise OSError("connection reset")
        return _Resp()

    monkeypatch.setattr(du.urllib.request, "urlopen", _blocking)
    out = await du.build_and_upload(_Cfg())
    assert out == {"ok": True, "code": "777777", "mini": True}
    assert len(seen_bodies) == 3          # 全尺寸 ×2 + mini ×1
    assert seen_bodies[-1] == b"MINI"


def test_diag_error_keys_bilingual_and_mapped():
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        t = get_translations(lang)
        assert t.get("err.svc.diag_bundle_failed")
        assert t.get("err.svc.diag_upload_rejected")

    class _Req:
        class state:
            ui_lang = "zh"

    from src.utils.diag_upload import error_detail_for
    d = error_detail_for(_Req(), "upload_rejected_413")
    assert "413" in d
    assert error_detail_for(_Req(), "bundle_failed")
    assert "custom_err_x" == error_detail_for(_Req(), "custom_err_x")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
