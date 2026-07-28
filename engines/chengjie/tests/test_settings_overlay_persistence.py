"""设置/人设路由配置持久化治理门禁（渠道路由 overlay 治理的延伸批）。

背景：settings_routes（/api/settings/save、/api/reply-logic、意图关键词 PUT）与
persona_routes（三个账号人设绑定端点）保存配置曾直调 ``config_manager.save()``
——把内存里合并后的**完整配置**整文件 yaml.dump 回主 config.yaml：冲掉注释/
结构，且把 overlay 与环境覆写值固化进主文件。

本文件守四层契约：
1. ``save_overlay_patch(replace_paths=...)`` 整树替换语义（真 ConfigManager）：
   命中路径按赋值不递归——「删掉的键必须真被删掉」（意图关键词表），
   overlay 与内存同步生效；未命中路径深合并行为不变（向后兼容）。
2. settings 三端点只写**快照 diff 出的最小 patch** 进 overlay，主文件字节不变。
3. persona 绑定扁平键（default/单账号）走 overlay；accounts **列表内**成员改动
   保持整文件 save()（overlay list 整体替换会遮蔽主配置账号增删——刻意保留）。
4. 源码 ratchet：两文件不再出现「治理外」的整文件 save() 直调。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.config_manager import ConfigManager  # noqa: E402

_ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "routes"

_MIN_CONFIG = """\
# 主配置注释哨兵 —— 运行时保存后必须原样保留（整文件字节不变）
telegram:
  api_id: 12345
  api_hash: testhash
  phone_number: "+10000000000"
  reply_logic:
    follow_up:
      enabled: true
      lookback_count: 10
ai:
  api_key: test-key
skills:
  enabled: []
intent:
  keywords:
    order: ["下单"]
    refund: ["退款"]
"""


async def _load_cm(tmp_path: Path) -> ConfigManager:
    cfg_path = tmp_path / "config.yaml"
    if not cfg_path.exists():
        cfg_path.write_text(_MIN_CONFIG, encoding="utf-8")
    cm = ConfigManager(str(cfg_path))
    assert await cm.load() is True
    return cm


def _overlay_data(tmp_path: Path) -> Dict[str, Any]:
    p = tmp_path / "config.local.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


# ── ① replace_paths 整树替换语义 ────────────────────────────────────────────

async def test_replace_paths_drops_stale_keys_in_overlay_and_memory(tmp_path):
    cm = await _load_cm(tmp_path)
    main_before = (tmp_path / "config.yaml").read_bytes()

    # 第一轮：两个意图
    assert cm.save_overlay_patch(
        {"intent": {"keywords": {"order": ["买"], "refund": ["退"]}}},
        replace_paths=("intent.keywords",)) is True
    # 第二轮：删掉 refund —— 深合并会让它赖着，replace 必须真删
    assert cm.save_overlay_patch(
        {"intent": {"keywords": {"order": ["买", "购买"]}}},
        replace_paths=("intent.keywords",)) is True

    overlay = _overlay_data(tmp_path)
    assert overlay["intent"]["keywords"] == {"order": ["买", "购买"]}
    assert "refund" not in overlay["intent"]["keywords"]
    # 内存合并视图同步：删除即时生效，不等重启（主配置的 refund 也被整树遮蔽）
    assert cm.config["intent"]["keywords"] == {"order": ["买", "购买"]}
    # 主文件字节不变
    assert (tmp_path / "config.yaml").read_bytes() == main_before


async def test_replace_paths_does_not_affect_sibling_merge(tmp_path):
    cm = await _load_cm(tmp_path)
    assert cm.save_overlay_patch(
        {"telegram": {"reply_logic": {"follow_up": {"enabled": False}}}}) is True
    # replace 路径之外的兄弟子树仍是深合并：第二笔不该冲掉第一笔
    assert cm.save_overlay_patch(
        {"telegram": {"reply_logic": {"session_window": {"enabled": True}}},
         "intent": {"keywords": {"o": ["x"]}}},
        replace_paths=("intent.keywords",)) is True

    overlay = _overlay_data(tmp_path)
    assert overlay["telegram"]["reply_logic"]["follow_up"]["enabled"] is False
    assert overlay["telegram"]["reply_logic"]["session_window"]["enabled"] is True


async def test_no_kwarg_call_keeps_legacy_deep_merge(tmp_path):
    cm = await _load_cm(tmp_path)
    assert cm.save_overlay_patch(
        {"intent": {"keywords": {"a": ["1"]}}}) is True
    assert cm.save_overlay_patch(
        {"intent": {"keywords": {"b": ["2"]}}}) is True
    # 旧调用形态（无 replace_paths）＝深合并：a 仍在
    overlay = _overlay_data(tmp_path)
    assert overlay["intent"]["keywords"] == {"a": ["1"], "b": ["2"]}


# ── ② settings 三端点：最小 patch 进 overlay，主文件不动 ─────────────────────

def _main_cfg_bytes(config_dir: Path) -> bytes:
    return (config_dir / "config.yaml").read_bytes()


def test_settings_save_writes_minimal_overlay_patch(auth_client, config_dir):
    main_before = _main_cfg_bytes(config_dir)
    r = auth_client.post("/api/settings/save", json={
        "section": "telegram",
        "fields": {"reply_cooldown_seconds": "30"},
    })
    assert r.status_code == 200, r.text
    assert _main_cfg_bytes(config_dir) == main_before, "主 config.yaml 被整文件回写"
    overlay = yaml.safe_load(
        (config_dir / "config.local.yaml").read_text(encoding="utf-8")) or {}
    assert overlay.get("telegram", {}).get("reply_cooldown_seconds") == 30
    # 最小 patch：主配置其余键不被固化进 overlay
    assert "api_id" not in overlay.get("telegram", {})
    assert "ai" not in overlay


def test_reply_logic_save_writes_minimal_overlay_patch(auth_client, config_dir):
    main_before = _main_cfg_bytes(config_dir)
    r = auth_client.post("/api/reply-logic", json={
        "trigger_enabled": True,
        "follow_up_lookback": 7,
    })
    assert r.status_code == 200, r.text
    assert _main_cfg_bytes(config_dir) == main_before
    overlay = yaml.safe_load(
        (config_dir / "config.local.yaml").read_text(encoding="utf-8")) or {}
    assert overlay.get("trigger", {}).get("enabled") is True
    assert overlay.get("telegram", {}).get(
        "reply_logic", {}).get("follow_up", {}).get("lookback_count") == 7
    # 未提交的键不进 overlay（快照 diff 语义）
    assert "group_reply" not in overlay.get("telegram", {})


def test_intent_keywords_put_replaces_whole_table(auth_client, config_dir, config_manager):
    main_before = _main_cfg_bytes(config_dir)
    r1 = auth_client.put("/api/settings/intent-keywords", json={
        "keywords": {"order": ["下单"], "refund": ["退款"]}})
    assert r1.status_code == 200, r1.text
    r2 = auth_client.put("/api/settings/intent-keywords", json={
        "keywords": {"order": ["下单", "购买"]}})
    assert r2.status_code == 200, r2.text

    assert _main_cfg_bytes(config_dir) == main_before
    overlay = yaml.safe_load(
        (config_dir / "config.local.yaml").read_text(encoding="utf-8")) or {}
    assert overlay.get("intent", {}).get("keywords") == {"order": ["下单", "购买"]}
    # 内存合并视图：删掉的 refund 即时消失（replace_paths 语义端到端）
    assert config_manager.config["intent"]["keywords"] == {"order": ["下单", "购买"]}


# ── ③ persona 绑定：扁平键走 overlay；accounts 列表内保持整文件 ─────────────

def test_tg_assign_profile_flat_goes_overlay(auth_client, config_dir):
    main_before = _main_cfg_bytes(config_dir)
    r = auth_client.post(
        "/api/personas/tg-account/default/assign-profile",
        json={"profile_id": ""})  # 清除绑定：跳过 PersonaManager 存在性检查
    assert r.status_code == 200, r.text
    assert r.json().get("config_saved") is True
    assert _main_cfg_bytes(config_dir) == main_before
    overlay = yaml.safe_load(
        (config_dir / "config.local.yaml").read_text(encoding="utf-8")) or {}
    assert overlay.get("telegram", {}).get("persona_ids") == []


def test_persona_routes_keep_full_save_for_accounts_list():
    """accounts 列表内成员改动保持 cm.save()——overlay list 整体替换会把整个
    accounts 快照固化进 overlay，遮蔽主配置后续账号增删（刻意不迁移）。"""
    src = (_ROUTES_DIR / "persona_routes.py").read_text(encoding="utf-8")
    # helper 兜底 1 处 + 三个端点 accounts 分支各 1 处 = 恰 4 处
    assert src.count("cm.save()") == 4, (
        "persona_routes 的 cm.save() 调用数漂移：新增保存点必须过 overlay 治理，"
        "accounts 列表分支的保留需在本测试登记")
    assert "_save_binding_patch" in src


# ── ④ 源码 ratchet：settings_routes 不再整文件直存 ──────────────────────────

def test_settings_routes_no_bare_full_save():
    src = (_ROUTES_DIR / "settings_routes.py").read_text(encoding="utf-8")
    # 唯一允许形态＝_persist_patch 兜底块内的回落
    assert src.count("config_manager.save()") == 1, (
        "settings_routes 出现治理外的整文件 save() 直调")
    assert "_persist_patch" in src and "_dict_diff" in src
    assert 'replace_paths=("intent.keywords",)' in src
