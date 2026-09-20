# -*- coding: utf-8 -*-
"""B43 克隆音色登记联动（2026-08-22，1.0.46 实测修复批次）。

事故：hosted 形态种子 ``avatar_voice.enabled: false``，用户登记克隆音色显示
成功，试听/实际发声却一直是 edge 通用音色（诊断包 3DTSV9 实证）——登记流程
从不联动开引擎，登记成果全程用不上，用户只看到一行「由 avatar_clone 回落」
小字黑话。

修复两半：
- 服务端：enroll avatar_clone 分支成功后按 ``should_auto_enable_avatar_voice``
  判定 hosted 形态 → 持久化翻开 ``avatar_voice.enabled`` + 当场重跑
  ``ensure_hosted_voice``（本文件钉判定纯函数 + 路由接线锚点）；
- 前端：cp-voice 回落警示从黑话小字升级为显式人话（「克隆音色暂不可用，
  本条用的是通用音色」，zh+en，双树同步）。
"""
from __future__ import annotations

from pathlib import Path

from src.ai.voice_enroll import should_auto_enable_avatar_voice

REPO = Path(__file__).resolve().parents[1]


# ── 判定纯函数 ──────────────────────────────────────────────────────────────

def test_hosted_seed_disabled_should_enable():
    """hosted 形态（_hosted_auto 且未 opt-out）+ enabled 假 → 联动翻开。"""
    cfg = {"avatar_voice": {"_hosted_auto": True}}
    assert should_auto_enable_avatar_voice(cfg) is True
    cfg2 = {"avatar_voice": {"_hosted_auto": True, "enabled": False}}
    assert should_auto_enable_avatar_voice(cfg2) is True


def test_already_enabled_no_double_write():
    cfg = {"avatar_voice": {"_hosted_auto": True, "enabled": True}}
    assert should_auto_enable_avatar_voice(cfg) is False


def test_opt_out_respected():
    """用户显式退出 hosted 语音 → 绝不代翻。"""
    cfg = {"avatar_voice": {"_hosted_auto": True, "hosted_opt_out": True}}
    assert should_auto_enable_avatar_voice(cfg) is False


def test_non_hosted_deploy_untouched():
    """内网/自配部署（无 _hosted_auto 标记）：enabled 是运维显式决策，不代翻。"""
    assert should_auto_enable_avatar_voice({"avatar_voice": {}}) is False
    assert should_auto_enable_avatar_voice({"avatar_voice": {"enabled": False}}) is False
    assert should_auto_enable_avatar_voice({}) is False
    assert should_auto_enable_avatar_voice(None) is False
    # 形状异常（avatar_voice 非 dict）→ 保守不动
    assert should_auto_enable_avatar_voice({"avatar_voice": "x"}) is False


# ── 路由接线锚点（静态：写了纯函数没接=白修） ───────────────────────────────

def test_enroll_route_wires_linkage():
    src = (REPO / "src" / "web" / "routes" / "voice_routes.py"
           ).read_text(encoding="utf-8")
    assert "should_auto_enable_avatar_voice" in src
    assert 'set_overlay_flag(\n                            "avatar_voice.enabled", True)' in src \
        or 'set_overlay_flag("avatar_voice.enabled", True)' in src
    # 翻开后必须当场重跑 hosted 接线（否则等下一轮热重载才生效）
    assert "ensure_hosted_voice(cm_b43)" in src


# ── 前端回落警示（显式人话，双树同步） ──────────────────────────────────────

def test_fallback_warn_copy_is_explicit_in_both_trees():
    for rel in ("shared/copilot/i18n/cp-i18n.js",
                "desktop/renderer/shared/copilot/i18n/cp-i18n.js"):
        js = (REPO / rel).read_text(encoding="utf-8")
        assert "克隆音色暂不可用" in js, rel
        assert "Cloned voice temporarily unavailable" in js, rel


# ── B43 回炉二次（2026-08-23 `_305` 死锁）：hosted 预授权也能进登记分支 ────────
#
# 旧入口条件 enabled=true 让「登记成功即联动开启」永远走不到——存量托管机
# enabled:false → 分支不开 → 落 DashScope 拦「语音引擎未接入」→ 永不激活。

def test_enroll_route_opens_avatar_branch_for_hosted_preauth():
    src = (REPO / "src" / "web" / "routes" / "voice_routes.py"
           ).read_text(encoding="utf-8")
    assert "_av_auto_entry" in src, "hosted 预授权入口变量丢失（死锁回归）"
    assert 'av_cfg.get("enabled") or _av_auto_entry' in src, \
        "AvatarHub 登记分支必须在 enabled 或 hosted 预授权时开启"
    # 预授权入口必须先当场接线（否则 health 探针打不到网关端点）
    assert "ensure_hosted_voice(cm_auto)" in src


# ── B62（`_312`）：克隆名绝不透传 edge_tts ───────────────────────────────────

def test_safe_edge_voice_passes_legal_voices():
    from src.ai.tts_pipeline import safe_edge_voice
    for v in ("zh-CN-XiaoxiaoNeural", "en-US-EmmaMultilingualNeural",
              "zh-CN-liaoning-XiaobeiNeural", "ja-JP-NanamiNeural"):
        assert safe_edge_voice(v) == v


def test_safe_edge_voice_maps_clone_names_to_generic():
    from src.ai.tts_pipeline import safe_edge_voice
    for bad in ("steven", "Steven", "my_voice_1", "小雨", "zh-CN-Xiaoxiao",
                "", None):
        out = safe_edge_voice(bad, "zh-CN-XiaoyiNeural")
        assert out == "zh-CN-XiaoyiNeural", f"{bad!r} → {out}"


def test_safe_edge_voice_bad_fallback_lands_builtin_default():
    from src.ai.tts_pipeline import safe_edge_voice
    assert safe_edge_voice("steven", "also_bad") == "zh-CN-XiaoxiaoNeural"
    assert safe_edge_voice("", "") == "zh-CN-XiaoxiaoNeural"


def test_tts_test_meta_carries_voice_mapped_from():
    """预览响应必须把「克隆名被映射成通用音色」暴露给前端警示层。"""
    src = (REPO / "src" / "web" / "routes" / "voice_routes.py"
           ).read_text(encoding="utf-8")
    assert '"voice_mapped_from"' in src
    js = (REPO / "shared" / "copilot" / "components" / "cp-voice.js"
          ).read_text(encoding="utf-8")
    assert "voice_mapped_from" in js, "cp-voice 警示条件未覆盖映射场景"


# ── B124（2026-08-28）：**合成时**选路也必须落到网关 ──────────────────────────
#
# 上面那几条只保护「登记 → 联动开引擎」。0828 钧/skuio 一夜双复现证明这还不够：
# 引擎开着、登记数据完好、启动日志也写着「克隆语音已接入网关兜底」，但真正合成
# 那一刻 base_urls 里只有 LAN 192.168.0.x:7852 → 外网不可达 → 超时 → 回落
# edge_tts＝默认音。启动注入（ensure_hosted_voice）与合成选路是两件事，本节钉后者。

def test_hosted_endpoint_absent_env_is_noop(monkeypatch):
    """非托管部署（env 未设）→ 原样返回，字节级零行为变更。"""
    from src.ai.tts_pipeline import _with_hosted_voice_endpoint
    monkeypatch.delenv("AITR_HOSTED_VOICE_BASE_URL", raising=False)
    cfg = {"base_urls": ["http://192.168.0.117:7852"]}
    assert _with_hosted_voice_endpoint(cfg) is cfg


def test_hosted_endpoint_prepended_when_lan_unreachable(monkeypatch):
    """LAN 探测不可达（_FIRST=1）→ 网关排首位，外网首条语音不必先撞死端点。"""
    from src.ai.tts_pipeline import _with_hosted_voice_endpoint
    hub = "https://bd2026.cc/api/ai/hub"
    monkeypatch.setenv("AITR_HOSTED_VOICE_BASE_URL", hub)
    monkeypatch.setenv("AITR_HOSTED_VOICE_FIRST", "1")
    out = _with_hosted_voice_endpoint(
        {"base_urls": ["http://192.168.0.117:7852",
                       "http://192.168.0.140:7852"]})
    assert out["base_urls"][0] == hub
    assert out["base_url"] == hub, "base_url 必须跟随 base_urls[0]"
    assert "http://192.168.0.117:7852" in out["base_urls"], "LAN 端点不得被丢弃"


def test_hosted_endpoint_appended_when_lan_alive(monkeypatch):
    """办公室机器（LAN 活着）→ 网关只作兜底，直连低延迟不受影响。"""
    from src.ai.tts_pipeline import _with_hosted_voice_endpoint
    hub = "https://bd2026.cc/api/ai/hub"
    monkeypatch.setenv("AITR_HOSTED_VOICE_BASE_URL", hub)
    monkeypatch.setenv("AITR_HOSTED_VOICE_FIRST", "0")
    out = _with_hosted_voice_endpoint({"base_urls": ["http://192.168.0.117:7852"]})
    assert out["base_urls"] == ["http://192.168.0.117:7852", hub]
    assert out["base_url"] == "http://192.168.0.117:7852"


def test_hosted_endpoint_not_duplicated(monkeypatch):
    """启动注入已生效 → 不再重复补（尾斜杠差异也算同一端点）。"""
    from src.ai.tts_pipeline import _with_hosted_voice_endpoint
    hub = "https://bd2026.cc/api/ai/hub"
    monkeypatch.setenv("AITR_HOSTED_VOICE_BASE_URL", hub + "/")
    monkeypatch.setenv("AITR_HOSTED_VOICE_FIRST", "1")
    cfg = {"base_urls": [hub, "http://192.168.0.117:7852"]}
    assert _with_hosted_voice_endpoint(cfg) is cfg


def test_hosted_endpoint_falls_back_to_singular_base_url(monkeypatch):
    """旧形状（只有单数 base_url）同样要被接管。"""
    from src.ai.tts_pipeline import _with_hosted_voice_endpoint
    hub = "https://bd2026.cc/api/ai/hub"
    monkeypatch.setenv("AITR_HOSTED_VOICE_BASE_URL", hub)
    monkeypatch.setenv("AITR_HOSTED_VOICE_FIRST", "1")
    out = _with_hosted_voice_endpoint({"base_url": "http://192.168.0.140:7852"})
    assert out["base_urls"] == [hub, "http://192.168.0.140:7852"]


def test_synth_path_consumes_hosted_endpoint():
    """接线锚点：写了兜底不接进合成链＝白修（本次事故的形状）。"""
    src = (REPO / "src" / "ai" / "tts_pipeline.py").read_text(encoding="utf-8")
    assert "_with_hosted_voice_endpoint(dict(self.avatar_voice or {}))" in src, \
        "_try_avatar_clone 必须经兜底解析克隆端点"


def test_unreachable_log_names_the_endpoints_tried():
    """回落默认音时必须报出试过哪些端点——否则现场只能靠远程取诊断包定位。"""
    src = (REPO / "src" / "ai" / "tts_pipeline.py").read_text(encoding="utf-8")
    assert "client.base_urls" in src and "已试" in src, \
        "克隆不可达日志必须带端点清单"
    assert "avatar_clone(7852) unreachable" not in src, \
        "旧文案把端点写死成 7852，与多端点/网关形态不符"
