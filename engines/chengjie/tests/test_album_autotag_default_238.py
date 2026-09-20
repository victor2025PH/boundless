# -*- coding: utf-8 -*-
"""N-2 C（#238，P2，D-N3）：相册上传 AI 识别 0/144 = 未触发（2026-09-07）。

事故（F35X38 / QENDQ2，skuio，1.0.76）：Mizuki 13:31 上传 144 张，每条 ``[pmedia] 上传 … autotag=False``；
至 13:44 零条补标 / vision 任务日志——识别根本没跑而非失败。页面「144 个缺触发词」「AI 已识别 0/144」却
无任何引导；「锚脸基准照 尚未设置」同样无引导。真因：``album_ai.enabled`` 默认 False（新子系统按约定默认关），
客户机配置未显式开。另一层：打标模块只认私网端点，外网客户机的识图走 ``hosted_gateway`` 注入的官网网关
（bd2026.cc → 隧道 → 同一批 GPU），此前一律被当公网拒绝 → 即便手点「AI 补标」也全部 vlm_unavailable。

钉住：
- 默认 ``enabled=True``（D-N3）；上传在识图就绪时排队打标（tag_status=pending、autotag=True）；
- 识图打不动（无端点 / 熔断中）→ 上传不排必败任务、条目留 untagged、回 ``vision_ready=False + vision_reason``；
  列表 ``ai`` 块同样带 ``vision_ready / vision_reason / last_error``；补标端点回 ``vision_ready=False`` 且 queued=0；
- 官网网关端点只在 ``_hosted_vision`` 标记 + URL 正是注入那条时放行；别的公网域名仍拒绝；
- 熔断：一次端点不可达后 90s 内后续任务直接 failed(vlm_unavailable)，不逐条打死端点；
- 完整度：有相册却零触发词 → ``album.triggers`` 进 missing、分数下调；无相册不参与；
- 前端：五态引导条（进度 / 识图不可达 + 内嵌 AI 补标 / 未识别 / 缺触发词 / 锚脸）、上传回执、轮询续命、
  补标点击落日志；i18n zh/en 齐。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

import src.companion.media_auto_tag as mat  # noqa: E402
import src.web.routes.persona_media_routes as pmr  # noqa: E402
from src.companion.persona_media_store import (  # noqa: E402
    PersonaMediaStore, configure_persona_media_store, reset_persona_media_store,
)
from src.utils.persona_completeness import ALBUM_KEY, persona_completeness  # noqa: E402
from src.utils.persona_manager import PersonaManager  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "personas.html"
_LAN = "http://192.168.0.140:11434/v1"
_GW = "https://bd2026.cc/api/ai/v1"


def _jpeg(seed=1) -> bytes:
    img = Image.new("RGB", (64, 48), (seed * 7 % 255, 90, 60))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


class _Cfg:
    def __init__(self, config=None):
        self.config = config or {}


@pytest.fixture(autouse=True)
def _reset():
    mat.reset_for_tests()
    yield
    mat.reset_for_tests()


def _app(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("mizuki", {"name": "Mizuki"})
    app = FastAPI()
    pmr.register_persona_media_routes(app, auth_dep=lambda: True, config_manager=_Cfg(cfg))
    return TestClient(app), pm


def _upload(c, seed=1):
    return c.post("/api/personas/mizuki/media",
                  files={"file": (f"p{seed}.jpg", _jpeg(seed), "application/octet-stream")})


# ── 端点策略 / 探针 / 熔断（纯函数层）─────────────────────────────────────────

def test_lan_policy_accepts_private_and_hosted_gateway_only(monkeypatch):
    monkeypatch.delenv("AITR_HOSTED_VISION_BASE_URL", raising=False)
    assert mat._lan_vision_cfg({"base_urls": [_LAN]})["base_urls"] == [_LAN]
    # 公网域名（含云端点）一律拒
    assert mat._lan_vision_cfg({"base_urls": ["https://api.siliconflow.cn/v1"]}) is None
    assert mat._lan_vision_cfg({"base_urls": [_GW]}) is None, "无 _hosted_vision 标记的网关 URL 不放行"
    # hosted_gateway 注入形态：标记 + base_url 就是网关 → 放行，且 provider 钉 openai_compatible、宽超时
    c = mat._lan_vision_cfg({"_hosted_vision": True, "base_url": _GW, "base_urls": [_GW],
                             "api_key": "cx.device-token", "timeout": 3,
                             "endpoint_timeouts": {_GW: 3}})
    assert c is not None and c["base_urls"] == [_GW] and c["api_key"] == "cx.device-token"
    assert c["provider"] == "openai_compatible" and c["timeout"] >= 60 and "endpoint_timeouts" not in c
    # 标记在场但 URL 不是注入的那条（被人改成别的公网域名）→ 仍拒
    assert mat._lan_vision_cfg({"_hosted_vision": True, "base_url": _GW,
                                "base_urls": ["https://evil.example/api/ai/v1"]}) is None
    # http（非 https）网关不放行
    assert mat._lan_vision_cfg({"_hosted_vision": True, "base_url": "http://bd2026.cc/api/ai/v1",
                                "base_urls": ["http://bd2026.cc/api/ai/v1"]}) is None
    # env 回放形态（ConfigManager._apply_hosted_vision_env）：env 与 base_url 任一匹配即可
    monkeypatch.setenv("AITR_HOSTED_VISION_BASE_URL", _GW)
    assert mat._lan_vision_cfg({"_hosted_vision": True, "base_urls": [_GW]}) is not None


def test_vision_probe_reasons(monkeypatch):
    assert mat.vision_probe({}) == {"ready": False, "reason": "no_endpoint"}
    assert mat.vision_probe({"base_urls": ["https://cloud.example/v1"]})["reason"] == "no_endpoint"
    monkeypatch.setattr(mat, "_get_vision_client", lambda cfg: None)
    assert mat.vision_probe({"base_urls": [_LAN]})["reason"] == "client_init_failed"
    monkeypatch.setattr(mat, "_get_vision_client", lambda cfg: object())
    assert mat.vision_probe({"base_urls": [_LAN]}) == {"ready": True, "reason": ""}
    mat._vlm_mark_down()
    assert mat.vision_probe({"base_urls": [_LAN]})["reason"] == "unreachable"
    mat._vlm_mark_up()
    assert mat.vision_probe({"base_urls": [_LAN]})["ready"] is True


def test_circuit_breaker_fast_fails_queue_after_endpoint_down(monkeypatch):
    """144 张排队 + 端点挂：第一条真打（失败），其余 90s 内直接 failed，不再逐条打端点。"""
    calls = []

    class _DeadClient:
        last_fail = ""

        def describe_image_sync(self, path, prompt):
            calls.append(path)
            self.last_fail = "unreachable"
            return None

    dead = _DeadClient()
    monkeypatch.setattr(mat, "_get_vision_client", lambda cfg: dead)
    st = PersonaMediaStore(":memory:")
    img = Path(__import__("tempfile").mkdtemp()) / "a.jpg"
    img.write_bytes(_jpeg(3))
    rows = [st.add("mizuki", "photo", str(img), f"/u/{i}.jpg") for i in range(5)]
    outs = [mat.tag_media_row(st, r, {"base_urls": [_LAN]}) for r in rows]
    assert outs == [mat.TAG_FAILED] * 5
    assert len(calls) == 1, "熔断后不得继续逐条打端点"
    assert mat.stats_snapshot()["last_error"] == "vlm_unavailable"
    assert mat.vision_probe({"base_urls": [_LAN]})["reason"] == "unreachable"
    # 单张图转码失败 / 模型空答（last_fail 为空）不算端点问题，不熔断
    mat.reset_for_tests()
    calls.clear()

    class _EmptyClient:
        last_fail = ""

        def describe_image_sync(self, path, prompt):
            calls.append(path)
            return None

    monkeypatch.setattr(mat, "_get_vision_client", lambda cfg: _EmptyClient())
    for r in rows[:3]:
        mat.tag_media_row(st, r, {"base_urls": [_LAN]})
    assert len(calls) == 3 and not mat._vlm_cooling()


def test_run_batch_reports_vision_not_ready_and_queues_nothing():
    st = PersonaMediaStore(":memory:")
    st.add("mizuki", "photo", "", "/u/a.jpg")
    res = mat.run_batch(st, {}, persona_id="mizuki", inline=True)
    assert res["ok"] and res["queued"] == 0
    assert res["vision_ready"] is False and res["vision_reason"] == "no_endpoint"


# ── 路由层 ────────────────────────────────────────────────────────────────────

def test_upload_default_autotag_queues_when_vision_ready(tmp_path, monkeypatch):
    """D-N3：不配 album_ai 也默认识别；识图就绪 → 排队 pending，回 autotag=True。"""
    sched = []
    monkeypatch.setattr(pmr._auto_tag, "schedule_tag", lambda st, mid, vcfg, **kw: sched.append(mid) or True)
    monkeypatch.setattr(pmr._auto_tag, "vision_probe", lambda vcfg: {"ready": True, "reason": ""})
    c, pm = _app(tmp_path, monkeypatch, {"vision": {"base_urls": [_LAN]}})
    try:
        r = _upload(c, 1)
        body = r.json()
        assert body["ok"] and body["autotag"] is True and body["vision_ready"] is True
        assert body["item"]["tag_status"] == "pending" and sched == [body["item"]["id"]]
        ai = c.get("/api/personas/mizuki/media").json()["ai"]
        assert ai["enabled"] is True and ai["auto_on_upload"] is True and ai["vision_ready"] is True
        assert ai["pending"] == 1
    finally:
        reset_persona_media_store()
        pm.delete_profile("mizuki")


def test_upload_when_vision_unreachable_says_so_and_does_not_queue(tmp_path, monkeypatch):
    """识图打不动：不排必败任务（条目留 untagged 供补标捞回）、回 vision_ready=False + reason；补标同样明说。"""
    sched = []
    monkeypatch.setattr(pmr._auto_tag, "schedule_tag", lambda st, mid, vcfg, **kw: sched.append(mid) or True)
    c, pm = _app(tmp_path, monkeypatch, {"vision": {"base_urls": ["https://cloud.example/v1"]}})
    try:
        body = _upload(c, 2).json()
        assert body["autotag"] is False and body["vision_ready"] is False and body["vision_reason"] == "no_endpoint"
        assert body["item"]["tag_status"] == "" and sched == []
        d = c.get("/api/personas/mizuki/media").json()
        assert d["ai"]["untagged"] == 1 and d["ai"]["vision_ready"] is False and d["ai"]["vision_reason"] == "no_endpoint"
        rr = c.post("/api/personas/mizuki/media/retag-all", json={})
        assert rr.status_code == 200
        res = rr.json()
        assert res["queued"] == 0 and res["vision_ready"] is False and res["vision_reason"] == "no_endpoint"
    finally:
        reset_persona_media_store()
        pm.delete_profile("mizuki")


def test_upload_respects_explicit_off(tmp_path, monkeypatch):
    sched = []
    monkeypatch.setattr(pmr._auto_tag, "schedule_tag", lambda st, mid, vcfg, **kw: sched.append(mid) or True)
    monkeypatch.setattr(pmr._auto_tag, "vision_probe", lambda vcfg: {"ready": True, "reason": ""})
    c, pm = _app(tmp_path, monkeypatch, {"companion": {"selfie": {"album_ai": {"enabled": False}}},
                                         "vision": {"base_urls": [_LAN]}})
    try:
        body = _upload(c, 3).json()
        assert body["autotag"] is False and body["vision_ready"] is None and sched == []
        assert c.get("/api/personas/mizuki/media").json()["ai"]["auto_on_upload"] is False
    finally:
        reset_persona_media_store()
        pm.delete_profile("mizuki")


# ── 完整度 ────────────────────────────────────────────────────────────────────

def test_completeness_counts_untagged_album():
    p = {"name": "Mizuki", "role": "陪伴", "background": "x", "tags": ["a", "b", "c"]}
    base = persona_completeness(p)
    none = persona_completeness(p, album={"total": 0, "with_triggers": 0})
    assert none == base, "无相册不参与评分"
    zero = persona_completeness(p, album={"total": 144, "with_triggers": 0})
    assert zero["score"] < base["score"] and ALBUM_KEY in zero["missing"]
    half = persona_completeness(p, album={"total": 144, "with_triggers": 40})
    full = persona_completeness(p, album={"total": 144, "with_triggers": 140})
    assert zero["score"] < half["score"] < full["score"] and ALBUM_KEY in full["filled"]
    assert full["score"] >= base["score"] - 1   # 满分档：相册权重全拿，与无相册基本持平
    assert persona_completeness(p, album="junk") == base   # 脏输入不炸、不参与


# ── 前端 / i18n ───────────────────────────────────────────────────────────────

def test_album_tab_guidance_and_progress_wired():
    html = _TPL.read_text(encoding="utf-8")
    guide = html[html.index("function _pmaRenderGuide("):html.index("async function _pmaRefreshAi(")]
    # 五态：进度 / 识图不可达（内嵌 AI 补标）/ 识别失败重试 / 未识别（内嵌 AI 补标）/ 缺触发词 / 锚脸
    for key in ("pma_guide_progress", "pma_guide_vision_down", "pma_guide_failed_n",
                "pma_guide_untagged_n", "pma_guide_notrg_n", "pma_guide_face_missing"):
        assert key in guide, key
    assert guide.count('onclick="pmaRetagAll()"') >= 3, "不可达 / 失败 / 未识别 三态都要内嵌补标按钮"
    assert 'onclick="_pmaFacePick()"' in guide and "<progress" in guide
    assert "ai.vision_ready === false" in guide
    # 上传：逐条读 autotag / vision_ready，回执两种口径，识图不可达 toast 报错，首次上传引导锚脸
    up = html[html.index("async function pmaUpload("):html.index("async function pmaDeleteItem(")]
    for s in ("d.autotag", "d.vision_ready === false", "pma_up_done_ai", "pma_up_done_novision",
              "pma_up_first_face_toast", "pma_up_ai_running_toast", "pma_need_trg_toast"):
        assert s in up, s
    # 补标：点击落 console 日志；vision_ready=false 明说
    rt = html[html.index("async function pmaRetagAll("):html.index("async function pmaRetagOne(")]
    assert "console.info('[pma] retag-all click" in rt and "d.vision_ready === false" in rt and "pma_ai_novision" in rt
    # 轮询：按剩余量续命 + 墙钟兜底，只刷计数不整页重绘
    poll = html[html.index("function _pmaMaybePollAi("):html.index("function _pmaPick(")]
    assert "pending * 3" in poll and "90 * 60 * 1000" in poll and "_pmaRefreshAi()" in poll
    assert 'id="pma-guide"' in html and 'id="pma-ai-cover"' in html
    # 完善向导纳入相册缺触发词
    wiz = html[html.index("function _updateCompletionWizard("):html.index("// ── P17-3")]
    assert "album_no_triggers" in wiz and "psn_wiz_album_trg" in wiz


def test_i18n_keys_present_zh_en():
    from src.web.i18n_packs.persona_apply_modal import EN as PEN, ZH as PZH
    from src.web.i18n_packs.persona_studio import EN as SEN, ZH as SZH
    keys = [k for k in PZH if k.startswith("pma_guide_") or k.startswith("pma_up_") or k in
            ("pma_ai_novision", "pma_ai_why_no_endpoint", "pma_ai_why_unreachable", "pma_ai_why_init_failed")]
    assert len(keys) >= 20
    ph = re.compile(r"\{(\w+)\}")
    for k in keys:
        assert PZH[k].strip() and PEN.get(k, "").strip(), f"{k} 缺 zh/en"
        assert set(ph.findall(PZH[k])) == set(ph.findall(PEN[k])), k
    assert SZH["psn_wiz_album_trg"] and SEN["psn_wiz_album_trg"]
    assert "识图服务暂不可用" in PZH["pma_guide_vision_down"] and "AI 补标" in PZH["pma_guide_vision_retry"]
