"""部署能力预设档门禁（WP-1 纯云起步档，2026-08）。

四条不变量，对应 cloud_light 档的全部存在理由：

1. **预设文件零内网 IP**（扫 ``config/profiles/`` 全部文件）——该档给「无 LAN GPU
   拓扑的陌生机器」，任何 192.168.*/10.*/172.16-31.* 出现即违约（规格 WP-1 验收项）；
2. **与 feature_registry 对齐**：reason=lan/infra 的 C 类键必须在 cloud_light 里
   显式 false——注册表将来新增 LAN 依赖功能时，本门禁逼预设档同步，防两源漂移；
3. **LAN 探针全静默**：预设合并进 example 基线后，探针决策函数（audio/avatar/
   gpu_watermark/cloud_credentials）必须全部「未启用不探」——这就是「cloud_light
   档 ops-overview 零常驻红灯」的机制化验证；
4. **播种语义**：仅「AITR_DEPLOY_PROFILE 显式 + 本次 config 全新播种」才应用；
   升级安装 / 无 env（生产双实例形态）/ 已有配置 → 永不触碰；同进程热重载幂等。

全部纯文件/自建 ConfigManager（AITR_CONFIG_PATH 指 tmp，绝不写仓库 config/），
CI 常驻。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.utils.deploy_profile import (
    active_profile,
    capability_snapshot,
    list_profiles,
    load_profile,
)
from src.utils.feature_registry import FEATURES, dig

ENGINE_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ENGINE_ROOT / "config" / "profiles"
EXAMPLE = ENGINE_ROOT / "config" / "config.example.yaml"
DESKTOP_MIN = ENGINE_ROOT / "config" / "config.desktop.min.yaml"

# RFC1918 私网段（本机三台算力机 192.168.0.117/140/176 均被首段覆盖）
_PRIVATE_IP = re.compile(
    r"\b(?:192\.168\.\d{1,3}\.\d{1,3}"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")

#: 注册表里「零 GPU 机器必须关」的键（lan=绑本机集群 / infra=安卓真机基建）。
#: risk/pending/safety 类刻意不进预设——那些是与部署拓扑正交的产品决策。
_LAN_KEYS = tuple(
    f.key for f in FEATURES if f.cls == "C" and f.reason in ("lan", "infra"))


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _cloud_light() -> dict:
    patch = load_profile("cloud_light")
    assert patch, "config/profiles/cloud_light.yaml 必须存在且可解析"
    return patch


def _merged_example() -> dict:
    cfg = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {}
    return _deep_merge(cfg, _cloud_light())


# ── ① 零内网 IP ──────────────────────────────────────────────────────────────

def test_profiles_dir_has_no_private_ip():
    files = sorted(PROFILES_DIR.glob("*.y*ml"))
    assert files, "config/profiles/ 不得为空（cloud_light 是 WP-1 交付物）"
    offenders = {}
    for p in files:
        hits = _PRIVATE_IP.findall(p.read_text(encoding="utf-8"))
        if hits:
            offenders[p.name] = sorted(set(hits))
    assert not offenders, (
        f"预设档出现内网 IP（cloud_light 语义=陌生机器零 LAN 依赖）: {offenders}")


# ── ② 结构与注册表对齐 ───────────────────────────────────────────────────────

def test_profile_marker_matches_filename():
    for p in sorted(PROFILES_DIR.glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and data, f"{p.name} 必须是非空 mapping"
        marker = dig(data, "deploy.profile")
        assert marker == p.stem, (
            f"{p.name} 的 deploy.profile={marker!r} 必须等于文件名 {p.stem!r}"
            "（标记即幂等闸门，错了会重复播种）")


def test_cloud_light_pins_all_registry_lan_keys():
    patch = _cloud_light()
    assert _LAN_KEYS, "注册表 lan/infra 键不得为空（注册表被误清也要红）"
    missing = {k: dig(patch, k) for k in _LAN_KEYS if dig(patch, k) is not False}
    assert not missing, (
        "以下 LAN/infra 依赖键没有在 cloud_light 显式关闭"
        f"（注册表新增了 LAN 功能？预设档要同步）: {missing}")


def test_cloud_light_load_profile_sanitizes_name():
    assert load_profile("../cloud_light") == {}
    assert load_profile("no_such_profile_xyz") == {}
    assert load_profile("") == {}
    assert "cloud_light" in list_profiles()


# ── ③ LAN 探针静默 + 云引擎序（零常驻红灯的机制化验证）──────────────────────

def test_cloud_light_silences_lan_probes():
    from src.inbox.health_watchdog import audio_probe_target, avatar_probe_target
    from src.utils.feature_registry import _has_embedding

    merged = _merged_example()
    assert audio_probe_target(merged) == "", "ASR 探针必须静默（voice_recognition 关）"
    assert avatar_probe_target(merged) == "", "AvatarHub 探针必须静默（avatar_voice 关）"
    assert not dig(merged, "ops.gpu_watermark.enabled"), "GPU 水位卡必须关"
    assert not dig(merged, "ops.cloud_credentials.enabled"), "自有 Key 巡检必须关"
    assert not _has_embedding(merged), "嵌入必须不配（记忆退关键词召回）"
    assert dig(merged, "translation.engines.order") == ["ai"], "翻译必须云引擎序"
    assert not str(dig(merged, "translation.engines.ollama_mt.base_url") or "").strip()
    assert not [u for u in (dig(merged, "translation.engines.ollama_mt.base_urls") or [])
                if str(u or "").strip()]
    assert dig(merged, "vision.base_urls") == [], "视觉不得有 LAN 端点"
    assert not dig(merged, "ai.fallback.enabled"), "本地 LLM 容灾必须关"
    assert dig(merged, "telegram.voice_reply.backend") == "edge_tts"


def test_capability_snapshot_states_under_cloud_light():
    snap = capability_snapshot(_merged_example())
    assert snap["voice_clone"]["state"] == "off"
    assert snap["local_llm_fallback"]["state"] == "off"
    assert snap["asr"]["state"] == "off"
    assert snap["speech_emotion"]["state"] == "off"
    assert snap["realtime_voice"]["state"] == "off"
    assert snap["selfie"]["state"] == "off"
    assert snap["gpu_watermark"]["state"] == "off"
    assert snap["rpa"]["state"] == "off"
    assert snap["embedding"] == {"state": "off", "fallback": "keyword_recall"}
    assert snap["translation"]["state"] == "on"
    assert snap["translation"]["engines"] == ["ai"]
    assert snap["translation"]["lan_mt"] is False
    assert snap["tts_voice_reply"]["backend"] == "edge_tts"
    # example 基线 Key 是占位 → 主链诚实报 degraded（装了没配 Key），不装绿
    assert snap["chat_llm"]["state"] == "degraded"
    assert snap["chat_llm"]["key_configured"] is False


def test_capability_snapshot_degraded_detection():
    """degraded 语义自证：开了但配置层依赖缺失 → 必须报 degraded 不报 on。"""
    cfg = {
        "memory": {"vector": {"enabled": True}},          # 无嵌入 → 关键词降级
        "vision": {"enabled": True},                       # 无端点无 key → 降级
        "ai": {"api_key": "sk-real", "primary": "cloud"},
    }
    snap = capability_snapshot(cfg)
    assert snap["memory_vector"]["state"] == "degraded"
    assert snap["vision"]["state"] == "degraded"
    assert snap["chat_llm"]["state"] == "on"
    # 依赖补齐后翻 on（探测器不是恒 degraded 的摆设）
    cfg["ai"]["embedding_base_url"] = "https://api.example.com"
    cfg["vision"]["api_key"] = "k"
    snap2 = capability_snapshot(cfg)
    assert snap2["memory_vector"]["state"] == "on"
    assert snap2["vision"]["state"] == "on"


# ── ④ 播种语义（ConfigManager 端到端，tmp 密闭）─────────────────────────────

def _mk_manager(tmp_path, monkeypatch, *, env_profile: str, fresh: bool):
    """AITR_CONFIG_PATH 指 tmp 的真 ConfigManager。fresh=False 时先放一份既有
    config（拷桌面种子内容，模拟升级安装/老机器）。"""
    from src.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config" / "config.yaml"
    if not fresh:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DESKTOP_MIN, cfg_path)
    monkeypatch.setenv("AITR_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    if env_profile:
        monkeypatch.setenv("AITR_DEPLOY_PROFILE", env_profile)
    else:
        monkeypatch.delenv("AITR_DEPLOY_PROFILE", raising=False)
    return ConfigManager(), cfg_path


def _overlay_of(cfg_path: Path) -> dict:
    p = cfg_path.parent / "config.local.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def test_seeding_applies_profile_on_fresh_install(tmp_path, monkeypatch):
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch,
                               env_profile="cloud_light", fresh=True)
    assert asyncio.run(cm.load()) is True
    overlay = _overlay_of(cfg_path)
    assert dig(overlay, "deploy.profile") == "cloud_light"
    assert dig(overlay, "avatar_voice.enabled") is False
    assert dig(overlay, "ai.fallback.enabled") is False
    assert active_profile(cm.config) == "cloud_light"
    # 同进程再 load（热重载路径）幂等：标记挡住二次落盘，overlay 字节不变
    before = (cfg_path.parent / "config.local.yaml").read_bytes()
    assert asyncio.run(cm.load()) is True
    assert (cfg_path.parent / "config.local.yaml").read_bytes() == before


def test_seeding_skips_existing_config(tmp_path, monkeypatch):
    """升级安装/老机器：config 已存在 → 即使 env 声明了档位也绝不应用。"""
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch,
                               env_profile="cloud_light", fresh=False)
    assert asyncio.run(cm.load()) is True
    assert dig(_overlay_of(cfg_path), "deploy.profile") is None
    assert active_profile(cm.config) == ""


def test_seeding_skips_without_env(tmp_path, monkeypatch):
    """无 env（生产双实例/开发态形态）：即使全新播种也不应用——默认行为零变化。"""
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, env_profile="", fresh=True)
    assert asyncio.run(cm.load()) is True
    assert dig(_overlay_of(cfg_path), "deploy.profile") is None


def test_seeding_bad_profile_is_soft_noop(tmp_path, monkeypatch):
    """env 指向不存在的档：按无预设跑，启动照常成功（永不抛）。"""
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch,
                               env_profile="no_such_profile_xyz", fresh=True)
    assert asyncio.run(cm.load()) is True
    assert dig(_overlay_of(cfg_path), "deploy.profile") is None


# ── 就绪自检端点（只读契约）──────────────────────────────────────────────────

def test_deploy_profile_endpoint_readonly():
    from src.web.routes.unified_inbox_setup_routes import register_setup_routes

    app = FastAPI()
    register_setup_routes(
        app, api_auth=lambda r: None,
        config_manager=SimpleNamespace(config=_merged_example()))
    client = TestClient(app)
    r = client.get("/api/setup/deploy-profile")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["profile"] == "cloud_light"
    assert "cloud_light" in body["available"]
    caps = body["capabilities"]
    assert caps["voice_clone"]["state"] == "off"
    assert caps["translation"]["engines"] == ["ai"]
    # 快照必须零密钥字段（排障读面给向导/支持，不给凭证）
    assert "api_key" not in str(caps)
