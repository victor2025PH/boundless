# -*- coding: utf-8 -*-
"""坐席手动出图「错误面收口」门禁（2026-08-22 「模型被清空」事故沉淀）。

事故形态：176 ComfyUI 被重装、模型目录整树清空 → 生成 400「ckpt not in []」；
而 selfie_command 只取 stderr **头 300 字**（全是显存腾挪过程日志），UI/服务端
日志都看不到真因，排障只能上出图机翻 _comfy.log。本门禁钉四层收口：

① comfy_infer 失败必发 ``ERR_CODE=<code>`` 且 code ∈ 路由 KNOWN_ERROR_CODES
   （两文件跨层契约，源码扫描 + 分类函数双向验证）；
② companion_selfie 摘录**取尾部** + 前置 ERR_CODE 行（头部过程日志不再顶掉真因）；
③ 路由 classify_gen_error：显式码优先，旧格式按特征保守回落；
④ 路由行为：引擎部署态探测进 config、锁脸无基准脸快失败（零 GPU 消耗）。
"""

from __future__ import annotations

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))

import comfy_infer as ci  # noqa: E402

from src.ai.companion_selfie import SelfieProvider  # noqa: E402
from src.web.routes import image_gen_routes as igr  # noqa: E402


# ── ① comfy_infer 错误码发射面与路由契约一致 ─────────────────────────────
def test_comfy_emitted_err_codes_are_known():
    """源码里每个 ERR_CODE= 字面量（含 classify_* 返回值）都必须在路由契约表内。"""
    import re
    from pathlib import Path

    src = Path(ci.__file__).read_text(encoding="utf-8")
    emitted = set(re.findall(r'ERR_CODE=([a-z_]+)', src))
    emitted.discard("")  # 防御
    # classify_* 的动态返回值（源码字面量抓不全的部分）
    emitted |= {"model_missing", "submit_rejected", "gen_timeout",
                "server_unreachable", "exec_error"}
    unknown = emitted - set(igr.KNOWN_ERROR_CODES) - {"s"}  # %s 格式占位
    assert not unknown, f"comfy_infer 发射了契约外错误码: {unknown}"


def test_classify_submit_rejection_model_missing():
    # 2026-08-22 事故原文形态：服务端模型清单为空
    body = ("prompt 提交被拒 HTTP 400: {\"node_errors\": {\"4\": "
            "\"Value not in list: ckpt_name: 'flux1-dev-fp8.safetensors' "
            "not in []\"}}")
    assert ci.classify_submit_rejection(body) == "model_missing"
    assert ci.classify_submit_rejection("参数非法 guidance 越界") == "submit_rejected"


def test_classify_failure_exception_types():
    assert ci.classify_failure(TimeoutError("等待出图超时 285s")) == "gen_timeout"
    assert ci.classify_failure(
        RuntimeError("prompt 提交被拒 HTTP 400: Value not in list: ckpt_name "
                     "'x' not in []")) == "model_missing"
    assert ci.classify_failure(
        RuntimeError("prompt 提交被拒 HTTP 400: bad width")) == "submit_rejected"
    assert ci.classify_failure(
        urllib.error.URLError("connection refused")) == "server_unreachable"
    assert ci.classify_failure(ValueError("boom")) == "exec_error"


# ── ② 摘录取尾部（真因不再被显存腾挪日志顶掉）──────────────────────────
_INCIDENT_STDERR = "\n".join([
    "[comfy_infer] 显存不足 free=11.3G < 14.0G，请求 ComfyUI 卸载腾显存…",
    "[comfy_infer] ComfyUI 腾后 free=11.3G",
    "[comfy_infer] 仍不足，尝试卸载 Ollama 驻留模型腾显存 @ http://192.168.0.176:11434",
    "[comfy_infer] 已请求卸载 Ollama 模型 hy-mt2-7b-official:latest（keep_alive=0）",
    "[comfy_infer] 卸 Ollama 后 free=30.2G",
    "[comfy_infer] face_ref 已上传: faceref_abc.png",
    "[comfy_infer] 失败: prompt 提交被拒 HTTP 400: Value not in list: "
    "ckpt_name: 'flux1-dev-fp8.safetensors' not in []",
    "[comfy_infer] ERR_CODE=model_missing",
])


def test_command_error_excerpt_keeps_tail_and_code():
    ex = SelfieProvider.command_error_excerpt(_INCIDENT_STDERR, "")
    assert "ERR_CODE=model_missing" in ex, "机器码行必须保留"
    assert "not in []" in ex, "终局真因必须保留"
    # 头部过程日志不得占据摘录（旧实现 [:300] 的病根）
    assert "显存不足 free=11.3G" not in ex


def test_command_error_excerpt_code_line_prepended_when_far():
    # ERR_CODE 行之后又刷了多行日志 → 仍要被找回置前
    text = "ERR_CODE=vram_insufficient\n" + "\n".join(f"noise {i}" for i in range(9))
    ex = SelfieProvider.command_error_excerpt(text, "")
    assert ex.startswith("ERR_CODE=vram_insufficient")
    assert "noise 8" in ex


def test_command_error_excerpt_empty_and_clip():
    assert SelfieProvider.command_error_excerpt("", "") == ""
    long = "x" * 2000 + " REAL_TAIL"
    ex = SelfieProvider.command_error_excerpt(long, "", limit=120)
    assert ex.endswith("REAL_TAIL") and len(ex) <= 120


# ── ③ 路由 classify_gen_error ────────────────────────────────────────────
def test_classify_gen_error_explicit_code_wins():
    assert igr.classify_gen_error(
        "selfie_command_failed:ERR_CODE=lock_busy | 获取出图锁超时") == "lock_busy"
    # 显式码即使与特征词共存也优先（vision_gate 词在前也不抢）
    assert igr.classify_gen_error(
        "vision_gate:x ERR_CODE=no_output") == "no_output"


def test_classify_gen_error_incident_head_only():
    """事故当时的**旧格式**头部截断串（无 ERR_CODE、无终局行）→ 按显存特征回落。
    修复后同场景会带 ERR_CODE=model_missing，此例保的是旧日志/旧后端的可分类性。"""
    head = ("RuntimeError: selfie_command_failed:[comfy_infer] 显存不足 "
            "free=11.3G < 14.0G，请求 ComfyUI 卸载腾显存… [comfy_infer] "
            "卸 Ollama 后 free=30.2G [comfy_infer] face_ref 已上传: faceref")
    assert igr.classify_gen_error(head) == "vram_insufficient"


def test_classify_gen_error_feature_fallbacks():
    assert igr.classify_gen_error("vision_gate:scene_mismatch(gym)") == "gate_reject"
    assert igr.classify_gen_error("selfie_timeout(300s)") == "gen_timeout"
    assert igr.classify_gen_error(
        "失败: prompt 提交被拒 HTTP 400: Value not in list: unet_name "
        "'q' not in []") == "model_missing"
    assert igr.classify_gen_error("prompt 提交被拒 HTTP 400: bad") == "submit_rejected"
    assert igr.classify_gen_error(
        "<urlopen error [WinError 10061] Connection refused>") == "server_unreachable"
    assert igr.classify_gen_error("获取出图锁超时，放弃(回落)") == "lock_busy"
    assert igr.classify_gen_error("empty_image") == "unknown"
    assert igr.classify_gen_error("") == "unknown"


# ── 部署态探测纯函数 ──────────────────────────────────────────────────────
def test_comfy_url_from_args():
    assert igr._comfy_url_from_args(
        ["python", "tools/comfy_infer.py", "--url", "http://192.168.0.176:8188/",
         "--prompt", "{prompt}"]) == "http://192.168.0.176:8188"
    assert igr._comfy_url_from_args(["python", "x.py"]) == ""
    assert igr._comfy_url_from_args("not-a-list") == ""


def test_engine_deploy_status_incident_empty_lists():
    # 2026-08-22 事故形态：服务端模型清单为空 → 三引擎全 False（前端全灰+红条）
    st = igr.engine_deploy_status({"ckpts": [], "unets": []},
                                  list(igr._DEFAULT_ENGINES))
    assert st == {"flux_pulid": False, "qwen_edit": False, "z_image": False}


def test_engine_deploy_status_probe_failed_unknown():
    st = igr.engine_deploy_status(None, ["flux_pulid", "custom_x"])
    assert st == {"flux_pulid": None, "custom_x": None}


def test_engine_deploy_status_partial():
    st = igr.engine_deploy_status(
        {"ckpts": ["flux1-dev-fp8.safetensors"],
         "unets": ["qwen_image_edit_2511_fp8_e4m3fn.safetensors"]},
        list(igr._DEFAULT_ENGINES) + ["custom_x"])
    assert st["flux_pulid"] is True
    assert st["qwen_edit"] is True
    assert st["z_image"] is False
    assert st["custom_x"] is None  # 自定义引擎不猜


def test_probe_comfy_models_unreachable_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(igr.urllib.request, "urlopen", _boom)
    assert igr.probe_comfy_models("http://127.0.0.1:1") is None
    assert igr.probe_comfy_models("") is None


def test_with_timeout_arg():
    base = ["python", "tools/comfy_infer.py", "--url", "http://x:8188",
            "--prompt", "{prompt}", "--out", "{out}"]
    out = igr.with_timeout_arg(base, 300.0)
    assert out[-2:] == ["--timeout", "285"]
    # 已显式带 --timeout / 非 comfy_infer 命令 → 不动
    assert igr.with_timeout_arg(base + ["--timeout", "600"], 300.0)[-1] == "600"
    assert igr.with_timeout_arg(["python", "other.py"], 300.0) == ["python", "other.py"]
    # 极小预算钳到 60s 地板
    assert igr.with_timeout_arg(base, 30.0)[-1] == "60"


# ── ④ 路由行为（FastAPI 端到端，零 ComfyUI 依赖）─────────────────────────
class _FakeCM:
    def __init__(self, cfg):
        self.config = cfg


def _mk_app(tmp_path, monkeypatch, models, *, vram_free=None, queue_pending=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    cfg = {
        "companion": {"selfie": {
            "enabled": True,
            "provider": {
                "enabled": True,
                "backend": "album",
                "album_dir": str(tmp_path / "albums"),
                "out_dir": str(tmp_path / "out"),
                "command_timeout_sec": 120,
                "command_args": [
                    "python", "tools/comfy_infer.py", "--url",
                    "http://192.168.0.176:8188", "--prompt", "{prompt}",
                    "--out", "{out}"],
            },
        }},
    }
    monkeypatch.setattr(igr, "_cached_probe", lambda url: models)
    # 探针密闭化：不打真 LAN（此前 config 每次跑都对 176 发 vram 探测，慢且看网络脸色）
    monkeypatch.setattr(igr, "_cached_vram", lambda url: vram_free)
    monkeypatch.setattr(igr, "_cached_queue", lambda url: queue_pending)
    app = FastAPI()
    igr.register_image_gen_routes(app, auth_dep=lambda: True,
                                  config_manager=_FakeCM(cfg))
    return TestClient(app)


def test_config_carries_deploy_status_and_comfy_flag(tmp_path, monkeypatch):
    client = _mk_app(tmp_path, monkeypatch, {"ckpts": [], "unets": []})
    d = client.get("/api/image/config").json()
    assert d["ok"] and d["enabled"]
    assert d["comfy_ok"] is True
    info = {x["id"]: x["deployed"] for x in d["engines_info"]}
    assert info == {"flux_pulid": False, "qwen_edit": False, "z_image": False}


def test_config_probe_failed_marks_comfy_down(tmp_path, monkeypatch):
    client = _mk_app(tmp_path, monkeypatch, None)
    d = client.get("/api/image/config").json()
    assert d["comfy_ok"] is False
    assert all(x["deployed"] is None for x in d["engines_info"])


def test_generate_face_ref_missing_fast_fail(tmp_path, monkeypatch):
    """锁脸但人设无基准脸 → 快失败带确定性错误码，绝不 spawn 子进程烧 GPU。"""
    client = _mk_app(tmp_path, monkeypatch, {"ckpts": ["flux1-dev-fp8.safetensors"],
                                             "unets": []})
    spawned = []
    import subprocess as _sp
    monkeypatch.setattr(_sp, "run",
                        lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(
                            AssertionError("不应触发子进程")))
    d = client.post("/api/image/generate", json={
        "persona_id": "nobody", "prompt": "站在阳台", "mode": "selfie",
        "engine": "flux_pulid", "lock_face": True}).json()
    assert d["ok"] is False
    assert d["code"] == "face_ref_missing"
    assert not spawned


# ── P1：异步任务 / 相册优先 / 账本回写 / 观测 ─────────────────────────────
import sys as _sys  # noqa: E402
import time as _time  # noqa: E402

# 1x1 PNG（base64）——假出图命令的产物
_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPh"
            "fDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _fake_gen_args(sleep_sec: float = 0.0):
    """真子进程假引擎：把 1x1 PNG 写到 {out}（可选先睡，供取消路径用）。"""
    code = ("import sys,base64,time\n"
            f"time.sleep({sleep_sec})\n"
            "open(sys.argv[2],'wb').write(base64.b64decode(sys.argv[3]))\n")
    return [_sys.executable, "-c", code, "{prompt}", "{out}", _PNG_B64]


def _mk_app_gen(tmp_path, monkeypatch, *, sleep_sec: float = 0.0):
    """可真出图（假引擎）的 app：预览落盘 monkeypatch 掉（不写仓库静态目录）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    cfg = {
        "companion": {"selfie": {
            "enabled": True,
            "provider": {
                "enabled": True,
                "backend": "album",
                "album_dir": str(tmp_path / "albums"),
                "out_dir": str(tmp_path / "out"),
                "command_timeout_sec": 30,
                "command_args": _fake_gen_args(sleep_sec),
                # manual_ui 真实层级＝provider.manual_ui（模块 docstring 契约）
                "manual_ui": {
                    "engines": {"fake": {"command_args": _fake_gen_args(sleep_sec)}},
                    "timeout_sec": 30,
                },
            },
        }},
    }
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(
        pb, "save_outbound_media",
        lambda sub, key, fn, data: (str(tmp_path / fn), f"/outbound/{fn}", "image/png"))
    monkeypatch.setattr(igr, "_cached_probe", lambda url: None)
    monkeypatch.setattr(igr, "_comfy_interrupt", lambda *a, **k: True)
    app = FastAPI()
    igr.register_image_gen_routes(app, auth_dep=lambda: True,
                                  config_manager=_FakeCM(cfg))
    return TestClient(app)


def test_jobs_lifecycle_done(tmp_path, monkeypatch):
    with _mk_app_gen(tmp_path, monkeypatch) as client:
        c = client.post("/api/image/jobs", json={
            "prompt": "a cup", "mode": "object", "engine": "fake",
            "lock_face": False}).json()
        assert c["ok"] and c.get("job_id")
        deadline = _time.time() + 15
        st = {}
        while _time.time() < deadline:
            st = client.get(f"/api/image/jobs/{c['job_id']}").json()
            if st.get("status") in ("done", "failed"):
                break
            _time.sleep(0.2)
        assert st.get("status") == "done", st
        assert st["result"]["ok"] is True
        assert st["result"]["preview_url"].startswith("/outbound/")


def test_jobs_cancel_discards_result(tmp_path, monkeypatch):
    with _mk_app_gen(tmp_path, monkeypatch, sleep_sec=6.0) as client:
        c = client.post("/api/image/jobs", json={
            "prompt": "a cup", "mode": "object", "engine": "fake",
            "lock_face": False}).json()
        jid = c["job_id"]
        r = client.post(f"/api/image/jobs/{jid}/cancel").json()
        assert r["ok"] and r["status"] == "cancelled"
        st = client.get(f"/api/image/jobs/{jid}").json()
        assert st["status"] == "cancelled"
        assert "result" not in st  # 已取消任务的迟到结果绝不回传


def test_jobs_unknown_id_404(tmp_path, monkeypatch):
    with _mk_app_gen(tmp_path, monkeypatch) as client:
        assert client.get("/api/image/jobs/deadbeef0000").status_code == 404


@pytest.fixture
def _tmp_media_store(tmp_path):
    """persona_media 单例隔离（绝不落仓库 config/persona_media.db）。"""
    from src.companion import persona_media_store as pms
    pms.reset_persona_media_store()
    st = pms.configure_persona_media_store(str(tmp_path / "pm.db"))
    yield st
    pms.reset_persona_media_store()
    pms._DB_PATH = pms.DEFAULT_DB_PATH


def test_album_stock_scene_filter(tmp_path, monkeypatch, _tmp_media_store):
    st = _tmp_media_store
    cafe = tmp_path / "cafe.jpg"
    beach = tmp_path / "beach.jpg"
    cafe.write_bytes(b"x" * 10)
    beach.write_bytes(b"y" * 10)
    st.add("lin", "photo", str(cafe), "", tags=["scene:咖啡馆"])
    st.add("lin", "photo", str(beach), "", tags=["scene:海边"])
    st.add("lin", "photo", str(tmp_path / "gone.jpg"), "", tags=["scene:咖啡馆"])  # 文件不存在=跳过
    client = _mk_app(tmp_path, monkeypatch, None)
    d = client.get("/api/image/album-stock",
                   params={"persona_id": "lin", "scene": "咖啡馆"}).json()
    assert d["ok"] and len(d["items"]) == 1
    it = d["items"][0]
    assert it["scene_class"] == "cafe"
    assert it["url"].startswith("/api/image/album-file?path=")
    # 无场景 → 全部有效存货
    d2 = client.get("/api/image/album-stock", params={"persona_id": "lin"}).json()
    assert len(d2["items"]) == 2


def test_album_file_sanitized(tmp_path, monkeypatch):
    client = _mk_app(tmp_path, monkeypatch, None)
    albums = tmp_path / "albums"
    albums.mkdir(parents=True, exist_ok=True)
    ok_file = albums / "p1" / "a.jpg"
    ok_file.parent.mkdir(parents=True, exist_ok=True)
    ok_file.write_bytes(b"JPG")
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"NO")
    assert client.get("/api/image/album-file",
                      params={"path": str(ok_file)}).status_code == 200
    assert client.get("/api/image/album-file",
                      params={"path": str(outside)}).status_code == 400
    assert client.get("/api/image/album-file",
                      params={"path": ""}).status_code == 400


# ── 2026-08-28：相册缩略图全裂 + 「较忙」永久误报 两起实录的回归钉 ────────
# 根因一：相册面板上传的行文件落 src/web/static/persona_albums/，而 album-file
#         的路径消毒只认 provider.album_dir → 每张缩略图 400（坐席看到 4 个裂图
#         框，卡片还宣称「已有 4 张，点选可直接发送」）。
# 根因二：把「空闲显存 < 14G」当「出图卡较忙」——同卡常驻聊天兜底 30B，空闲显存
#         长期 0.x G 而 GPU 利用率 0，横幅一挂就再不消失（且对坐席泄露内部实现）。
def test_stock_item_url_prefers_row_direct_url():
    """行自带 /static 直服 URL 时直接用它；为空才回落 album-file。"""
    assert igr.stock_item_url(
        {"url": "/static/persona_albums/lin/a.jpg"}, "D:/x/a.jpg"
    ) == "/static/persona_albums/lin/a.jpg"
    # 坐席存册/自动补货的行 url 为空 → album-file（文件在 album_dir 内）
    u = igr.stock_item_url({"url": ""}, "D:/albums/lin/b.jpg")
    assert u.startswith("/api/image/album-file?path=")
    # 非 / 开头的脏值不当直服 URL 用（防把相对/外链塞进 img src）
    assert igr.stock_item_url({"url": "http://evil/x.jpg"}, "D:/a.jpg").startswith(
        "/api/image/album-file?path=")


def test_album_roots_covers_both_trees_and_rejects_outside(tmp_path):
    roots = igr.album_roots(str(tmp_path / "albums"))
    assert len(roots) == 2 and igr._STATIC_ALBUM_ROOT.resolve() in roots
    inside = tmp_path / "albums" / "lin" / "a.jpg"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"x")
    assert igr.resolve_album_path(str(inside), roots) is not None
    assert igr.resolve_album_path(str(tmp_path / "secret.jpg"), roots) is None
    assert igr.resolve_album_path("", roots) is None


def test_album_file_serves_panel_uploaded_tree(tmp_path, monkeypatch):
    """相册面板上传树（static/persona_albums）内的图必须 200——此前一律 400。"""
    client = _mk_app(tmp_path, monkeypatch, None)
    f = igr._STATIC_ALBUM_ROOT / "_gate_tmp" / "a.jpg"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"JPG")
    try:
        assert client.get("/api/image/album-file",
                          params={"path": str(f)}).status_code == 200
    finally:
        f.unlink(missing_ok=True)
        try:
            f.parent.rmdir()
        except OSError:
            pass


def test_album_stock_uses_panel_url(tmp_path, monkeypatch, _tmp_media_store):
    st = _tmp_media_store
    f = tmp_path / "cafe.jpg"
    f.write_bytes(b"x")
    st.add("lin", "photo", str(f), "/static/persona_albums/lin/cafe.jpg",
           tags=["scene:咖啡馆"])
    client = _mk_app(tmp_path, monkeypatch, None)
    d = client.get("/api/image/album-stock", params={"persona_id": "lin"}).json()
    assert d["items"][0]["url"] == "/static/persona_albums/lin/cafe.jpg"


def test_min_free_gb_is_engine_aware():
    """qwen_edit 的闸门是 18（`--min-free-gb 18`），不是写死的 14。"""
    assert igr.min_free_gb_from_args(
        ["python", "comfy_infer.py", "--engine", "qwen_edit",
         "--min-free-gb", "18"]) == 18.0
    assert igr.min_free_gb_from_args(["python", "comfy_infer.py"]) == 14.0
    assert igr.min_free_gb_from_args(None) == 14.0
    assert igr.min_free_gb_from_args(["--min-free-gb", "oops"]) == 14.0


def test_probe_comfy_queue_reads_exec_info(monkeypatch):
    import io
    import json as _json

    class _R:
        def __init__(self, payload):
            self._b = _json.dumps(payload).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(igr.urllib.request, "urlopen",
                        lambda *a, **k: _R({"exec_info": {"queue_remaining": 3}}))
    assert igr.probe_comfy_queue("http://x") == 3
    monkeypatch.setattr(igr.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert igr.probe_comfy_queue("http://x") is None   # 探不到=未知，不是 0
    assert igr.probe_comfy_queue("") is None
    del io


def test_config_exposes_queue_not_just_vram(tmp_path, monkeypatch):
    """忙闲判据必须是队列+在途单；显存只作为『要不要预热』的输入之一。"""
    client = _mk_app(tmp_path, monkeypatch, {"ckpts": [], "unets": []},
                     vram_free=0.1, queue_pending=2)
    d = client.get("/api/image/config").json()
    assert d["busy_signal"] is True
    assert d["queue_pending"] == 2 and d["busy_jobs"] == 0
    assert d["vram_free_gb"] == 0.1          # 保留给运维/诊断
    mins = {x["id"]: x["min_free_gb"] for x in d["engines_info"]}
    assert mins["qwen_edit"] == 18.0 and mins["flux_pulid"] == 14.0


def test_busy_job_count_only_counts_inflight():
    jobs = {"a": {"status": "running"}, "b": {"status": "queued"},
            "c": {"status": "done"}, "d": {"status": "cancelled"}}
    assert igr.busy_job_count(jobs) == 2


def test_mark_sent_records_ledger(tmp_path, monkeypatch, _tmp_media_store):
    st = _tmp_media_store
    f = tmp_path / "cafe_white-dress_01.jpg"
    f.write_bytes(b"x")
    row = st.add("lin", "photo", str(f), "", tags=["scene:咖啡馆"])
    client = _mk_app(tmp_path, monkeypatch, None)
    d = client.post("/api/image/mark-sent", json={
        "persona_id": "lin", "media_id": row["id"], "path": str(f),
        "scene": "咖啡馆", "album": True,
        "platform": "telegram", "account_id": "acc1", "chat_key": "888"}).json()
    assert d["ok"] and d["recorded"] is True
    n = st._conn.execute("SELECT COUNT(*) FROM persona_media_sends").fetchone()[0]
    assert n == 1
    # 缺会话三元组 → 不落账但不报错（best-effort 语义）
    d2 = client.post("/api/image/mark-sent", json={
        "persona_id": "lin", "media_id": row["id"], "path": str(f)}).json()
    assert d2["ok"] and d2["recorded"] is False


# ── P2：日配额 / 场景热度 / 同脸校验 ─────────────────────────────────────
def test_quota_check_and_count_semantics():
    igr._QUOTA_STATE.update({"day": "", "by_actor": {}})
    # limit<=0 = 不限额（与 daily_reply_budget=0 同语义，绝不能变成全拦）
    assert igr.quota_check_and_count("a", 0) is None
    assert igr.quota_check_and_count("a", -1) is None
    assert igr._QUOTA_STATE["by_actor"] == {}
    # limit=2：前两次放行计数，第三次拒并回已用数
    assert igr.quota_check_and_count("a", 2) is None
    assert igr.quota_check_and_count("a", 2) is None
    assert igr.quota_check_and_count("a", 2) == 2
    # 按 actor 隔离
    assert igr.quota_check_and_count("b", 2) is None
    assert igr.quota_used("a") == 2 and igr.quota_used("b") == 1
    igr._QUOTA_STATE.update({"day": "", "by_actor": {}})


def test_generate_quota_exceeded_code(tmp_path, monkeypatch):
    igr._QUOTA_STATE.update({"day": "", "by_actor": {}})
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    cfg = {
        "companion": {"selfie": {
            "enabled": True,
            "provider": {
                "enabled": True, "backend": "album",
                "album_dir": str(tmp_path / "albums"),
                "out_dir": str(tmp_path / "out"),
                "command_timeout_sec": 30,
                "command_args": _fake_gen_args(),
                "manual_ui": {"engines": {"fake": {"command_args": _fake_gen_args()}},
                              "timeout_sec": 30, "daily_quota": 1},
            },
        }},
    }
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(
        pb, "save_outbound_media",
        lambda sub, key, fn, data: (str(tmp_path / fn), f"/outbound/{fn}", "image/png"))
    monkeypatch.setattr(igr, "_cached_probe", lambda url: None)
    app = FastAPI()
    igr.register_image_gen_routes(app, auth_dep=lambda: True,
                                  config_manager=_FakeCM(cfg))
    with TestClient(app) as client:
        body = {"prompt": "a cup", "mode": "object", "engine": "fake",
                "lock_face": False}
        d1 = client.post("/api/image/generate", json=body).json()
        assert d1["ok"] is True
        d2 = client.post("/api/image/generate", json=body).json()
        assert d2["ok"] is False and d2["code"] == "quota_exceeded"
    igr._QUOTA_STATE.update({"day": "", "by_actor": {}})


def test_scene_hints_orders_by_unmet_then_demand(tmp_path, monkeypatch):
    client = _mk_app(tmp_path, monkeypatch, None)
    import src.companion.media_gap as mg
    import src.inbox.image_autosend as ia
    monkeypatch.setattr(mg, "collect_scene_supply",
                        lambda scfg: {"lin": {"cafe": 2}, "": {"beach": 1}})
    monkeypatch.setattr(ia, "metrics_snapshot", lambda: {
        "scene_demand": {"cafe": 5, "beach": 3, "gym": 9, "other": 4},
        "scene_unmet": {"beach": 2},
    })
    d = client.get("/api/image/scene-hints",
                   params={"persona_id": "lin"}).json()
    scenes = [r["scene"] for r in d["scenes"]]
    assert scenes == ["beach", "gym", "cafe"]  # 未兑现在前 → 需求降序；other 剔除
    rows = {r["scene"]: r for r in d["scenes"]}
    assert rows["beach"]["stock"] == 1   # 共享池计入
    assert rows["cafe"]["stock"] == 2
    assert rows["gym"]["stock"] == 0


def test_identity_parse_and_compose(tmp_path):
    from src.ai.image_gate import compose_side_by_side, parse_identity_response
    assert parse_identity_response('{"same_person": "yes"}') == "yes"
    assert parse_identity_response('{"same_person":"no"} trailing') == "no"
    assert parse_identity_response("Not sure, faces unclear") == "unsure"
    assert parse_identity_response("They look like the same person, yes.") == "yes"
    assert parse_identity_response("No, these are different people.") == "no"
    assert parse_identity_response("") == "unsure"
    assert parse_identity_response("maybe") == "unsure"
    # 拼图：两张小图 → 单张成品（PIL 真跑）
    from PIL import Image
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (40, 60), (200, 10, 10)).save(a)
    Image.new("RGB", (30, 90), (10, 200, 10)).save(b)
    out = tmp_path / "c.jpg"
    assert compose_side_by_side(str(a), str(b), str(out)) is True
    im = Image.open(out)
    assert im.width > 40 and im.height == 60  # 等高拼接（min 高度）
    # 软失败：源文件缺失 → False 不抛
    assert compose_side_by_side(str(tmp_path / "nope.png"), str(b),
                                str(tmp_path / "d.jpg")) is False


def test_identity_mismatch_blocks_result(tmp_path, monkeypatch):
    """identity_check 开 + 判 mismatch → 失败码拦下（不出预览）；skipped → 放行。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    albums = tmp_path / "albums" / "lin"
    albums.mkdir(parents=True)
    (albums / "face_ref.png").write_bytes(b"\x89PNG fake")
    cfg = {
        "companion": {"selfie": {
            "enabled": True,
            "vision_gate": {"enabled": False, "identity_check": True},
            "provider": {
                "enabled": True, "backend": "album",
                "album_dir": str(tmp_path / "albums"),
                "out_dir": str(tmp_path / "out"),
                "command_timeout_sec": 30,
                "command_args": _fake_gen_args(),
                "manual_ui": {"engines": {"fake": {"command_args": _fake_gen_args()}},
                              "timeout_sec": 30},
            },
        }},
    }
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(
        pb, "save_outbound_media",
        lambda sub, key, fn, data: (str(tmp_path / fn), f"/outbound/{fn}", "image/png"))
    monkeypatch.setattr(igr, "_cached_probe", lambda url: None)
    import src.ai.image_gate as ig
    calls = []

    async def _fake_identity(gen, ref, root):
        calls.append((gen, ref))
        return ("mismatch", "no")
    monkeypatch.setattr(ig, "check_face_identity", _fake_identity)
    app = FastAPI()
    igr.register_image_gen_routes(app, auth_dep=lambda: True,
                                  config_manager=_FakeCM(cfg))
    with TestClient(app) as client:
        body = {"persona_id": "lin", "prompt": "on a balcony", "mode": "selfie",
                "engine": "fake", "lock_face": True}
        d = client.post("/api/image/generate", json=body).json()
        assert d["ok"] is False and d["code"] == "identity_mismatch"
        assert calls, "同脸校验应被调用"

    async def _fake_skip(gen, ref, root):
        return ("skipped", "unsure")
    monkeypatch.setattr(ig, "check_face_identity", _fake_skip)
    app2 = FastAPI()
    igr.register_image_gen_routes(app2, auth_dep=lambda: True,
                                  config_manager=_FakeCM(cfg))
    with TestClient(app2) as client:
        body = {"persona_id": "lin", "prompt": "on a balcony", "mode": "selfie",
                "engine": "fake", "lock_face": True}
        d = client.post("/api/image/generate", json=body).json()
        assert d["ok"] is True and d["identity"] == "skipped"


# ── P3：场景需求日账本（跨重启记忆）─────────────────────────────────────
def test_scene_demand_ledger_upsert_and_window(tmp_path, _tmp_media_store):
    st = _tmp_media_store
    st.record_scene_demand("cafe", unmet=False, now=1_755_000_000.0)
    st.record_scene_demand("cafe", unmet=True, now=1_755_000_100.0)
    st.record_scene_demand("beach", unmet=True, now=1_755_000_200.0)
    st.record_scene_demand("", unmet=True)   # 空场景忽略
    win = st.scene_demand_window(14, now=1_755_000_300.0)
    assert win["cafe"] == {"demand": 2, "unmet": 1}
    assert win["beach"] == {"demand": 1, "unmet": 1}
    # 窗口外的旧数据不计
    old = st.scene_demand_window(14, now=1_755_000_000.0 + 20 * 86400)
    assert old == {}


def test_record_scene_request_peek_semantics(tmp_path, monkeypatch):
    """未配置单例 → 零磁盘写（peek 不懒建，防「测试写仓库 config/」）；
    配置后 → 需求落账。进程计数两种情况都照常。"""
    from src.companion import persona_media_store as pms
    from src.inbox import image_autosend as ia
    pms.reset_persona_media_store()
    try:
        ia.record_scene_request("海边", unmet=True)   # 无单例：只计进程数
        assert pms.peek_persona_media_store() is None
        st = pms.configure_persona_media_store(str(tmp_path / "pm2.db"))
        ia.record_scene_request("海边", unmet=True)   # 有单例：落账
        win = st.scene_demand_window(14)
        assert win.get("beach", {}).get("demand") == 1
        assert win.get("beach", {}).get("unmet") == 1
    finally:
        pms.reset_persona_media_store()
        pms._DB_PATH = pms.DEFAULT_DB_PATH


def test_scene_hints_merges_ledger_window_by_max(tmp_path, monkeypatch, _tmp_media_store):
    """账本窗与进程快照 per-scene 取 max（同源双写，相加=双计）。"""
    st = _tmp_media_store
    st.record_scene_demand("cafe", unmet=False)
    for _ in range(5):
        st.record_scene_demand("cafe", unmet=False)
    st.record_scene_demand("beach", unmet=True)
    client = _mk_app(tmp_path, monkeypatch, None)
    import src.companion.media_gap as mg
    import src.inbox.image_autosend as ia
    monkeypatch.setattr(mg, "collect_scene_supply", lambda scfg: {})
    monkeypatch.setattr(ia, "metrics_snapshot", lambda: {
        "scene_demand": {"cafe": 2}, "scene_unmet": {}})
    d = client.get("/api/image/scene-hints", params={"persona_id": "lin"}).json()
    rows = {r["scene"]: r for r in d["scenes"]}
    assert rows["cafe"]["demand"] == 6          # max(进程 2, 账本 6)
    assert rows["beach"]["unmet"] == 1          # 账本独有场景入列
    assert d["scenes"][0]["scene"] == "beach"   # 未兑现在前


def test_image_gen_stats_counters_and_prom():
    from src.web.image_gen_stats import ImageGenStats
    s = ImageGenStats()
    s.record_attempt("flux_pulid")
    s.record_ok(1200)
    s.record_attempt("flux_pulid")
    s.record_fail("model_missing")
    s.record_cancel()
    s.record_sent(album=True)
    s.record_sent(album=False)
    s.record_saved()
    s.record_stock_query(hit=True)
    s.record_stock_query(hit=False)
    d = s.dump()
    assert d["gen_total"] == 2 and d["gen_ok"] == 1 and d["gen_fail"] == 1
    assert d["fail_by_code"] == {"model_missing": 1}
    assert d["latency_p50_ms"] == 1200
    assert d["sent_album"] == 1 and d["sent_generated"] == 1
    assert d["stock_queries"] == 2 and d["stock_hits"] == 1
    prom = s.dump_prom()
    assert 'image_gen_fail_total{code="model_missing"} 1' in prom
    assert 'image_gen_sent_total{source="album"} 1' in prom
