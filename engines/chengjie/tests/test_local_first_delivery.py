"""本地化交付闭环（P2）：golive 清单本地档诚实性 + 开通器本地剖面。

守：
1. golive AI 项按 ``ai.primary`` 分流——local_only 全本地（云 key 占位）不再被
   红灯拦「不能开张」；声明 local* 却缺端点如实红灯；cloud 旧口径逐字节不变。
2. ``render_overlay(ai_primary=local*)`` 产出的 overlay 与 AIClient/golive 的
   本地端点契约（fallback enabled+base_url+model）一致；缺参在开通期 ValueError；
   默认（cloud）输出不含 ai 块（对既有租户开通零影响）。
3. CLI ``--ai-primary local*`` 缺 ``--local-llm-model`` → argparse 即时报错。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from src.ops.instance_provisioner import plan_instance, render_overlay
from src.utils.golive import build_checklist


# ── golive 清单：AI 项本地档 ─────────────────────────────────────


def _checklist(ai_cfg):
    return build_checklist(
        config={"ai": ai_cfg},
        channel_statuses=[{"id": "telegram", "name": "Telegram", "ready": True}],
        config_errors=0, config_warnings=0,
        kb_ready={"available": True, "is_cold": False, "enabled_entries": 30},
        online_agents=1,
    )


def _ai_check(out):
    return next(c for c in out["checks"] if c["id"] == "ai")


_LOCAL_FB = {"enabled": True, "base_url": "http://127.0.0.1:11434",
             "model": "qwen3:30b-a3b-instruct"}


def test_local_only_green_without_cloud_key():
    """纯本地部署（云 key 占位）＝可开张；旧口径会红灯拦住，与事实相反。"""
    out = _checklist({
        "provider": "openai_compatible", "api_key": "YOUR_API_KEY",
        "primary": "local_only", "fallback": dict(_LOCAL_FB),
    })
    c = _ai_check(out)
    assert c["status"] == "ok"
    assert "qwen3:30b-a3b-instruct" in c["detail"]
    assert out["ready"] is True and out["light"] == "green"


def test_local_declared_but_endpoint_missing_fails():
    out = _checklist({
        "provider": "openai_compatible", "api_key": "YOUR_API_KEY",
        "primary": "local_only",
    })
    c = _ai_check(out)
    assert c["status"] == "fail"
    assert "ai.fallback" in c["detail"]
    assert out["ready"] is False


def test_local_mode_without_cloud_key_warns_not_blocks():
    """local（非 only）语义上要云端回落：没配 key 黄灯提示、不拦上线。"""
    out = _checklist({
        "provider": "openai_compatible", "api_key": "YOUR_API_KEY",
        "primary": "local", "fallback": dict(_LOCAL_FB),
    })
    c = _ai_check(out)
    assert c["status"] == "warn"
    assert "local_only" in c["detail"]     # 指路：无需回落请改 local_only
    assert out["ready"] is True and out["light"] == "yellow"


def test_local_mode_with_cloud_key_ok():
    out = _checklist({
        "provider": "openai_compatible", "api_key": "sk-real-key-0123456789",
        "primary": "local", "fallback": dict(_LOCAL_FB),
    })
    c = _ai_check(out)
    assert c["status"] == "ok"
    assert "回落" in c["detail"]


def test_cloud_mode_placeholder_still_fails():
    """回归钉：默认 cloud 档旧口径不变——占位 key 仍红灯。"""
    out = _checklist({"provider": "openai_compatible", "api_key": "YOUR_API_KEY"})
    assert _ai_check(out)["status"] == "fail"
    out2 = _checklist({"provider": "openai_compatible",
                       "api_key": "sk-real-key-0123456789"})
    assert _ai_check(out2)["status"] == "ok"


# ── 开通器：本地剖面 overlay ─────────────────────────────────────


def _plan():
    return plan_instance({"services": []}, product="zhiliao", customer="Acme Ltd")


def test_render_overlay_local_only_block_parses_and_matches_contract():
    text = render_overlay(
        _plan(), ai_primary="local_only",
        local_llm_base_url="http://127.0.0.1:11434",
        local_llm_model="qwen3:30b-a3b-instruct")
    data = yaml.safe_load(text)
    ai = data["ai"]
    assert ai["primary"] == "local_only"
    fb = ai["fallback"]
    assert fb["enabled"] is True
    assert fb["base_url"] == "http://127.0.0.1:11434"
    assert fb["model"] == "qwen3:30b-a3b-instruct"
    # 产出的配置必须直接满足 golive 本地档判定（同一契约，不许两套口径）
    out = _checklist({"provider": "openai_compatible", "api_key": "YOUR_API_KEY",
                      "primary": ai["primary"], "fallback": fb})
    assert _ai_check(out)["status"] == "ok"
    # 既有段完整保留
    assert data["web_admin"]["enabled"] is True
    assert data["brand"]["product_name"]


def test_render_overlay_default_has_no_ai_block():
    text = render_overlay(_plan())
    assert yaml.safe_load(text).get("ai") is None
    text2 = render_overlay(_plan(), ai_primary="cloud")
    assert yaml.safe_load(text2).get("ai") is None


def test_render_overlay_rejects_incomplete_local():
    with pytest.raises(ValueError):
        render_overlay(_plan(), ai_primary="local_only",
                       local_llm_base_url="http://127.0.0.1:11434",
                       local_llm_model="")
    with pytest.raises(ValueError):
        render_overlay(_plan(), ai_primary="on-prem-maybe",
                       local_llm_base_url="http://x", local_llm_model="m")


# ── config_check：本地档语义（同页 check#3 不再误导）────────────


def _ai_issues(ai_cfg):
    from src.utils.config_check import check_config
    issues = check_config({"ai": ai_cfg})
    return [i for i in issues if i.path.startswith("ai")]


_BASE_AI = {"provider": "openai_compatible", "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash", "api_key": "YOUR_API_KEY"}


def test_config_check_local_only_skips_cloud_key_nag():
    issues = _ai_issues({**_BASE_AI, "primary": "local_only",
                         "fallback": dict(_LOCAL_FB)})
    assert not any(i.path == "ai.api_key" for i in issues), \
        "local_only 云 key 根本不会被用，不该再劝填"
    assert not any(i.severity == "error" for i in issues)


def test_config_check_local_declared_endpoint_missing_is_error():
    issues = _ai_issues({**_BASE_AI, "primary": "local_only"})
    errs = [i for i in issues if i.severity == "error"]
    assert any(i.path.startswith("ai.fallback") for i in errs), \
        "声明本地主链但端点缺失必须在自检期点名"


def test_config_check_primary_typo_warns():
    issues = _ai_issues({**_BASE_AI, "api_key": "sk-real-0123456789",
                         "primary": "local-only"})   # 连字符笔误
    assert any(i.path == "ai.primary" and i.severity == "warn" for i in issues)


def test_config_check_cloud_default_unchanged():
    issues = _ai_issues(dict(_BASE_AI))
    assert any(i.path == "ai.api_key" and i.severity == "warn" for i in issues), \
        "cloud 档占位 key 仍应有 WARN（旧口径回归钉）"


# ── CLI 接线 ─────────────────────────────────────────────────────


def _load_cli():
    path = Path(__file__).resolve().parent.parent / "scripts" / "provision_instance.py"
    spec = importlib.util.spec_from_file_location("_provision_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_local_requires_model():
    mod = _load_cli()
    with pytest.raises(SystemExit) as ei:
        mod.main(["--product", "zhiliao", "--customer", "t",
                  "--ai-primary", "local_only"])
    assert ei.value.code == 2      # argparse.error（在读 stack.json 之前即拦）
