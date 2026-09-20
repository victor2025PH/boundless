"""O-2 A / 老板决策 D-O5（2026-09-08，WYNN22 #201）：记忆抽取白名单进产品基线 + 启动日志说真话。

事实链（skuio 1.0.76 clean 包 09-07 13:14 重装）：
  · 抽取闸 ``should_extract_intent`` 按 ``memory.extract.intents`` 白名单放行，**空即不抽**
    （Phase D 为存量零回归刻意如此）；
  · 这把钥匙此前只在服务器 ``config/config.yaml``（四值，不随包）与内测种子
    ``config.desktop.internal.yaml``（``match_all: true``，只进 smart 包）；
  · clean 包播种的 ``config.desktop.min.yaml`` **没有任何 memory 块** → 全新安装
    ``intents=[]``：17:20 重启后 8 小时 30 次 ``[episodic] schedule run`` 全部
    ``skip: intent not extractable (match_all=False intents=[])``，6 客户零抽取零 promoted；
  · 而 17:21:02 启动日志仍写「情景记忆已启用」——第三次「出厂默认没进基线」
    （前两次：L-5 D1/D7 三键、更早的工作目标卡）。

本文件钉五件事：
1. 注册表：三键 A 类、intents 声明值与服务器同（列表型 baseline 每次给副本）；
2. 缺键补：全新 clean 安装（min 种子）与升级安装（旧 config 无 memory 块）首启后合并
   视图都有白名单，升级态补进 overlay、主 config 一个字不动、幂等；
3. 显式空尊重：用户写 ``intents: []`` ＝自己关掉，不补、不改；服务器已有值不变；
   内测种子 ``match_all: true`` 行为不变；服务器实例（无桌面态）零变化；
4. 启动日志：白名单非空 → INFO「已启用 · 可抽取意图 N 个」；空 / 总闸关 → WARNING
   「记忆不会新增」且**不含**「已启用」；
5. 端到端逻辑：补齐后 direct_chat / small_talk / greeting / complaint 可抽，order_query 不抽。
"""
from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path

import yaml

from src.skills.skill_manager import episodic_startup_banner, should_extract_intent
from src.utils.feature_registry import (
    as_nested,
    baseline_patch,
    by_key,
    dig,
    product_baseline_values,
)

ENGINE_ROOT = Path(__file__).resolve().parent.parent
CFG = ENGINE_ROOT / "config"
DESKTOP_MIN = CFG / "config.desktop.min.yaml"
DESKTOP_INTERNAL = CFG / "config.desktop.internal.yaml"

TRIO = (
    "memory.extract.enabled",
    "memory.extract.use_llm",
    "memory.extract.intents",
)
#: 服务器 config/config.yaml L1770 的现值（该文件不在 git 里，这里钉字面值防漂移）
SERVER_INTENTS = ["direct_chat", "small_talk", "greeting", "complaint"]


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _min_seed() -> dict:
    return yaml.safe_load(DESKTOP_MIN.read_text(encoding="utf-8"))


def _min_seed_without_memory() -> dict:
    """min 种子去掉 memory 块 = skuio 机 1.0.76 播种的那份 config（WYNN22 现场形态）。"""
    cfg = _min_seed()
    cfg.pop("memory", None)
    for k in TRIO:
        assert dig(cfg, k) is None
    return cfg


# ── 1. 注册表声明 ─────────────────────────────────────────────────────────────

def test_registry_declares_extract_trio_as_a_class():
    for k in TRIO:
        f = by_key(k)
        assert f is not None and f.cls == "A", k
        assert f.show is False, f"{k} 是行为策略键，不进功能总览 UI"
    assert by_key("memory.extract.enabled").baseline is True
    assert by_key("memory.extract.use_llm").baseline is True
    assert by_key("memory.extract.intents").baseline == SERVER_INTENTS
    assert product_baseline_values()["memory.extract.intents"] == SERVER_INTENTS


def test_baseline_patch_returns_fresh_list_each_call():
    a = baseline_patch({})["memory.extract.intents"]
    b = baseline_patch({})["memory.extract.intents"]
    assert a == b == SERVER_INTENTS
    assert a is not b, "列表型 baseline 必须每次拷贝，否则运行时改一处全局都变"
    a.append("order_query")
    assert by_key("memory.extract.intents").baseline == SERVER_INTENTS


def test_seed_whitelist_matches_registry_and_server_value():
    seed = _min_seed()
    assert dig(seed, "memory.extract.intents") == SERVER_INTENTS
    assert dig(seed, "memory.extract.enabled") is True
    assert dig(seed, "memory.extract.use_llm") is True


# ── 2./3. baseline_patch 三态语义（纯函数）──────────────────────────────────

def test_patch_fills_missing_memory_block():
    patch = baseline_patch(_min_seed_without_memory())
    assert patch["memory.extract.intents"] == SERVER_INTENTS
    assert patch["memory.extract.enabled"] is True
    assert patch["memory.extract.use_llm"] is True


def test_patch_fills_when_memory_block_exists_but_extract_missing():
    # 服务器早期形态 / 自建档：memory 块有 vector 等子键、唯独没有 extract
    cfg = {"memory": {"enabled": True, "vector": {"enabled": False}}}
    patch = baseline_patch(cfg)
    assert patch["memory.extract.intents"] == SERVER_INTENTS


def test_patch_respects_explicit_empty_whitelist():
    cfg = {"memory": {"extract": {"intents": []}}}
    patch = baseline_patch(cfg)
    assert "memory.extract.intents" not in patch, "显式空 = 用户关掉，绝不覆盖"
    # 另两键没表态照补
    assert patch["memory.extract.enabled"] is True
    assert patch["memory.extract.use_llm"] is True
    # 抽取闸对显式空的判定与历史一致：不抽
    assert should_extract_intent("direct_chat", cfg["memory"]["extract"]) is False


def test_patch_leaves_server_values_untouched():
    server = {"memory": {"extract": {"enabled": True, "use_llm": True,
                                     "intents": ["complaint"], "min_user_chars": 3}}}
    patch = baseline_patch(server)
    assert not [k for k in patch if k.startswith("memory.")]


def test_internal_seed_match_all_behaviour_unchanged():
    """smart 包内测种子 overlay 写 match_all: true 且不写 intents：基线会把 intents 补进
    合并视图，但 should_extract_intent 仍以 match_all 优先——任何意图照抽，零行为变化。"""
    internal = yaml.safe_load(DESKTOP_INTERNAL.read_text(encoding="utf-8")) or {}
    assert dig(internal, "memory.extract.match_all") is True
    merged = _deep_merge(_min_seed_without_memory(), internal)
    merged = _deep_merge(merged, as_nested(baseline_patch(merged)))
    ex = merged["memory"]["extract"]
    assert ex["match_all"] is True
    assert ex["intents"] == SERVER_INTENTS
    for intent in ("order_query", "stop_contact", "direct_chat"):
        assert should_extract_intent(intent, ex) is True


# ── 2. 真 ConfigManager：全新 / 升级 / 显式空 / 服务器 ──────────────────────

def _mk_manager(tmp_path: Path, monkeypatch, *, existing: dict | None, desktop: bool = True):
    """AITR_CONFIG_PATH 指 tmp 的真 ConfigManager（无部署档、无种子目录）。

    existing=None → 全新安装（首启从 config.desktop.min.yaml 播种）；
    existing=dict → 先落一份既有 config.yaml（升级安装 / 老机器）。
    """
    from src.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config" / "config.yaml"
    if existing is not None:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml.safe_dump(existing, allow_unicode=True),
                            encoding="utf-8")
    monkeypatch.setenv("AITR_CONFIG_PATH", str(cfg_path))
    if desktop:
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    else:
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("AITR_DEPLOY_PROFILE", raising=False)
    monkeypatch.delenv("AITR_SEED_DATA_DIR", raising=False)
    return ConfigManager(), cfg_path


def _overlay_of(cfg_path: Path) -> dict:
    p = cfg_path.parent / "config.local.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def test_fresh_clean_install_boots_with_whitelist(tmp_path, monkeypatch):
    """干净包模拟：删 config 起服务 → 首启播种 min 种子 → 合并视图有白名单。"""
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, existing=None)
    asyncio.run(cm.load())
    assert dig(cm.config, "memory.extract.intents") == SERVER_INTENTS
    assert dig(cm.config, "memory.extract.enabled") is True
    # 种子本身就带（不是靠补齐）：主 config 里就有
    seeded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert dig(seeded, "memory.extract.intents") == SERVER_INTENTS
    # WYNN22 现场那条 direct_chat 入站：不再 skip
    ex = cm.config["memory"]["extract"]
    for intent in SERVER_INTENTS:
        assert should_extract_intent(intent, ex) is True
    assert should_extract_intent("order_query", ex) is False
    level, msg = episodic_startup_banner(cm.config["memory"], cfg_path.parent / "bot.db")
    assert level == logging.INFO and "情景记忆已启用" in msg and "4 个" in msg


def test_upgrade_install_without_memory_block_gets_baseline_into_overlay(tmp_path, monkeypatch):
    """skuio 两台 / 钧一台的存量形态：config 是 1.0.76 min 种子快照（无 memory 块）。"""
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, existing=_min_seed_without_memory())
    asyncio.run(cm.load())
    assert dig(cm.config, "memory.extract.intents") == SERVER_INTENTS
    overlay = _overlay_of(cfg_path)
    for k in TRIO:
        assert dig(overlay, k) is not None, f"{k} 应被 _ensure_baseline 补进 overlay"
    assert dig(overlay, "memory.extract.intents") == SERVER_INTENTS
    # 主 config.yaml 一个字不动（补齐只写 overlay）
    assert yaml.safe_load(cfg_path.read_text(encoding="utf-8")) == _min_seed_without_memory()
    # 幂等：再 load 一次 overlay 字节不变
    before = (cfg_path.parent / "config.local.yaml").read_bytes()
    asyncio.run(cm.load())
    assert (cfg_path.parent / "config.local.yaml").read_bytes() == before


def test_explicit_empty_whitelist_survives_startup(tmp_path, monkeypatch):
    cfg = _min_seed_without_memory()
    cfg["memory"] = {"extract": {"intents": []}}
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, existing=cfg)
    asyncio.run(cm.load())
    assert dig(cm.config, "memory.extract.intents") == []
    assert dig(_overlay_of(cfg_path), "memory.extract.intents") is None
    # 另两键没表态照补（与 L-5 三键「一关两开」同一条语义）
    assert dig(cm.config, "memory.extract.enabled") is True
    level, msg = episodic_startup_banner(cm.config["memory"], "x.db")
    assert level == logging.WARNING and "已启用" not in msg


def test_server_instance_without_desktop_mode_is_untouched(tmp_path, monkeypatch):
    """服务器实例（zhiliao / tongyi）无 AITR_DESKTOP_MODE：基线补齐整段不跑、不写 overlay。"""
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch,
                               existing=_min_seed_without_memory(), desktop=False)
    asyncio.run(cm.load())
    assert dig(cm.config, "memory.extract.intents") is None
    assert not (cfg_path.parent / "config.local.yaml").exists()


# ── 4. 启动日志说真话 ─────────────────────────────────────────────────────────

def test_banner_whitelist_present_is_info_with_count():
    level, msg = episodic_startup_banner(
        {"extract": {"intents": ["small_talk", "direct_chat", "direct_chat", " "]}}, "/p/bot.db")
    assert level == logging.INFO
    assert "情景记忆已启用" in msg
    assert "可抽取意图 2 个" in msg          # 去重 + 剔空
    assert "direct_chat, small_talk" in msg  # 排序，值守对照配置零歧义
    assert msg.endswith("/p/bot.db")


def test_banner_empty_whitelist_is_warning_and_never_says_enabled():
    for mcfg in ({"extract": {"intents": []}},
                 {"extract": {}},          # example 档：memory 块无 extract
                 {},                       # 完全无 memory 块（WYNN22 现场）
                 {"extract": {"intents": None}}):
        level, msg = episodic_startup_banner(mcfg, "bot.db")
        assert level == logging.WARNING, mcfg
        assert "抽取白名单为空" in msg and "记忆不会新增" in msg, mcfg
        assert "已启用" not in msg, mcfg
        assert msg.startswith("[episodic]"), "诊断包按 [episodic] 前缀一把捞齐"


def test_banner_match_all_is_info_all():
    level, msg = episodic_startup_banner({"extract": {"match_all": True, "intents": []}}, "b")
    assert level == logging.INFO and "情景记忆已启用" in msg and "全部" in msg


def test_banner_extract_disabled_is_warning():
    level, msg = episodic_startup_banner({"extract": {"enabled": False, "intents": SERVER_INTENTS}}, "b")
    assert level == logging.WARNING
    assert "memory.extract.enabled=False" in msg and "记忆不会新增" in msg
    assert "已启用" not in msg


def test_banner_is_pure_and_does_not_mutate_input():
    mcfg = {"extract": {"intents": ["greeting"]}}
    snap = copy.deepcopy(mcfg)
    episodic_startup_banner(mcfg, "b")
    assert mcfg == snap
