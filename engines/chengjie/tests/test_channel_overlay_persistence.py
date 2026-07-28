"""渠道配置持久化治理门禁：save_overlay_patch + 四渠道路由最小 patch。

背景：四个渠道路由（telegram / line_rpa / messenger_rpa / whatsapp_rpa）保存
配置曾直调 ``config_manager.save()``——把内存里合并后的**完整配置**整文件
yaml.dump 回主 config.yaml：冲掉注释/结构，且把 config.local.yaml overlay 的
值固化进主文件，违反全仓「运行时写配置一律走 overlay」惯例。

本文件守住三层契约：
1. ``ConfigManager.save_overlay_patch`` 核心语义（真 ConfigManager + tmp_path）：
   主文件字节不变 / 二次 patch 深合并 / list 整体替换 / 重载合并视图==内存 /
   空 patch 不落盘 / 自写 overlay 刷新 mtime 基线不触发热重载；
2. telegram PUT voice-reply 只送**最小 patch**（掩码/未白名单键不进）且不再
   整文件 save()；无 save_overlay_patch 的旧式 fake 仍回落 save()（兜底契约）；
3. 四个渠道路由源码不再出现裸 ``config_manager.save()`` 直调（防回潮，唯一
   允许形态＝save_overlay_patch 兜底块内的回落）。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.config_manager import ConfigManager  # noqa: E402

_ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
_CHANNEL_ROUTE_FILES = (
    "telegram_routes.py",
    "line_rpa_routes.py",
    "messenger_rpa_routes.py",
    "whatsapp_rpa_routes.py",
)

# 首行注释为「主配置注释保留」的哨兵：save_overlay_patch 后按字节比对不变。
_MIN_CONFIG = """\
# 主配置注释哨兵 —— 运行时保存后必须原样保留（整文件字节不变）
telegram:
  api_id: 12345
  api_hash: testhash
  phone_number: "+10000000000"
  no_reply_sender_usernames:
    - keep_a
  voice_reply:
    enabled: false
    probability: 0.5
ai:
  api_key: test-key
skills:
  enabled: []
"""


async def _load_cm(tmp_path: Path) -> ConfigManager:
    cfg_path = tmp_path / "config.yaml"
    if not cfg_path.exists():
        cfg_path.write_text(_MIN_CONFIG, encoding="utf-8")
    cm = ConfigManager(str(cfg_path))
    assert await cm.load() is True
    return cm


def _overlay_data(tmp_path: Path) -> Dict[str, Any]:
    return yaml.safe_load(
        (tmp_path / "config.local.yaml").read_text(encoding="utf-8")) or {}


# ── ① 主文件不动，overlay 只收最小 patch ─────────────────────────────────────

async def test_patch_writes_overlay_and_leaves_main_config_untouched(tmp_path):
    cm = await _load_cm(tmp_path)
    main_before = (tmp_path / "config.yaml").read_bytes()

    ok = cm.save_overlay_patch({"telegram": {"voice_reply": {"enabled": True}}})
    assert ok is True

    # 主 config.yaml 字节级不变（注释/结构保住）
    assert (tmp_path / "config.yaml").read_bytes() == main_before
    overlay = _overlay_data(tmp_path)
    assert overlay == {"telegram": {"voice_reply": {"enabled": True}}}
    # 最小 patch：主配置其余键不被固化进 overlay
    assert "probability" not in overlay["telegram"]["voice_reply"]
    assert "ai" not in overlay
    # 内存即时一致（merged view）
    assert cm.config["telegram"]["voice_reply"]["enabled"] is True
    assert cm.config["telegram"]["voice_reply"]["probability"] == 0.5


# ── ② 二次 patch 深合并，不丢第一次的键 ──────────────────────────────────────

async def test_second_patch_deep_merges_and_keeps_first(tmp_path):
    cm = await _load_cm(tmp_path)
    assert cm.save_overlay_patch(
        {"telegram": {"voice_reply": {"enabled": True}}}) is True
    assert cm.save_overlay_patch(
        {"telegram": {"reply_logic": {"cooldown_seconds": 9}}}) is True

    overlay = _overlay_data(tmp_path)
    assert overlay["telegram"]["voice_reply"]["enabled"] is True  # 第一次仍在
    assert overlay["telegram"]["reply_logic"]["cooldown_seconds"] == 9
    assert cm.config["telegram"]["reply_logic"]["cooldown_seconds"] == 9


# ── ③ list 值整体替换（_deep_merge 列表语义） ────────────────────────────────

async def test_list_values_replaced_wholesale(tmp_path):
    cm = await _load_cm(tmp_path)
    assert cm.save_overlay_patch(
        {"telegram": {"no_reply_sender_usernames": ["a", "b"]}}) is True
    assert cm.save_overlay_patch(
        {"telegram": {"no_reply_sender_usernames": ["c"]}}) is True

    overlay = _overlay_data(tmp_path)
    assert overlay["telegram"]["no_reply_sender_usernames"] == ["c"]
    # 内存合并视图同为整体替换（不 extend、不残留主配置的 keep_a）
    assert cm.config["telegram"]["no_reply_sender_usernames"] == ["c"]


# ── ④ 重新 load 后合并视图 == 内存视图 ───────────────────────────────────────

async def test_reload_merged_view_matches_memory(tmp_path):
    cm = await _load_cm(tmp_path)
    assert cm.save_overlay_patch({
        "telegram": {"voice_reply": {"enabled": True, "probability": 0.9}},
        "voice_recognition": {"provider": "openai",
                              "openai": {"model": "whisper-1"}},
    }) is True

    cm2 = ConfigManager(str(tmp_path / "config.yaml"))
    assert await cm2.load() is True
    assert cm2.config == cm.config


# ── ⑤ 空 patch：True 且不落盘 ────────────────────────────────────────────────

async def test_empty_patch_returns_true_without_writing(tmp_path):
    cm = await _load_cm(tmp_path)
    assert cm.save_overlay_patch({}) is True
    assert not (tmp_path / "config.local.yaml").exists()


# ── ⑥ 自写 overlay 刷新 mtime 基线：不触发热重载整装重载 ─────────────────────

async def test_own_overlay_write_does_not_trigger_hot_reload(tmp_path):
    cm = await _load_cm(tmp_path)
    assert cm.save_overlay_patch(
        {"telegram": {"voice_reply": {"enabled": True}}}) is True

    # 写完基线已同步到新 mtime
    assert cm._overlay_loaded_mtime == cm._overlay_mtime()
    cm._last_hot_reload_check = 0  # 绕过 30s 节流，立刻真检查
    assert cm.check_and_hot_reload() is False


# ── 路由级：PUT voice-reply 送最小 patch，且不整文件 save() ──────────────────

class _OverlayRecordingCM:
    """带 save_overlay_patch（返回真 bool）的 fake：路由必须走它而非 save()。"""

    def __init__(self) -> None:
        self.config: Dict[str, Any] = {"telegram": {"voice_reply": {"enabled": False}}}
        self.patches: list = []
        self.save_calls = 0

    def save_overlay_patch(self, patch: Dict[str, Any]) -> bool:
        self.patches.append(copy.deepcopy(patch))
        return True

    def save(self) -> bool:
        self.save_calls += 1
        return True


class _LegacyCM:
    """旧式简化 fake：只有 save()——兜底契约须回落到它。"""

    def __init__(self) -> None:
        self.config: Dict[str, Any] = {"telegram": {}}
        self.save_calls = 0

    def save(self) -> bool:
        self.save_calls += 1
        return True


def _tg_client(cm, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    import src.web.routes.telegram_routes as tg_routes

    # 快照写盘改道 tmp（生产 config/snapshots 不许被测试污染）
    monkeypatch.setattr(tg_routes, "_SNAPSHOT_DIR", tmp_path / "snapshots")

    app = FastAPI()

    def _noop_auth() -> None:
        return None

    tg_routes.register_telegram_routes(
        app,
        page_auth=_noop_auth,
        api_auth=_noop_auth,
        templates=None,
        config_manager=cm,
    )
    return TestClient(app)


def test_voice_reply_put_sends_minimal_patch_not_full_save(tmp_path, monkeypatch):
    cm = _OverlayRecordingCM()
    client = _tg_client(cm, tmp_path, monkeypatch)

    r = client.put("/api/telegram/settings/voice-reply", json={
        "enabled": True,
        "probability": 0.7,
        "not_whitelisted": "nope",
        "voice_profile": {"reference_audio_path": "voice_samples/a.wav",
                          "hacker_key": 1},
        "openai_tts": {"model": "tts-1", "api_key": "***"},
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # 整文件 save() 零调用
    assert cm.save_calls == 0
    assert len(cm.patches) == 1
    # 最小 patch：只含命中白名单且真正落进配置的键；
    # 掩码 api_key（"***"）跳过、未白名单键（not_whitelisted/hacker_key）不进。
    assert cm.patches[0] == {"telegram": {"voice_reply": {
        "enabled": True,
        "probability": 0.7,
        "voice_profile": {"reference_audio_path": "voice_samples/a.wav"},
        "openai_tts": {"model": "tts-1"},
    }}}
    # 内存写入同步发生
    assert cm.config["telegram"]["voice_reply"]["enabled"] is True
    assert "not_whitelisted" not in cm.config["telegram"]["voice_reply"]


def test_voice_reply_put_falls_back_to_save_for_legacy_cm(tmp_path, monkeypatch):
    cm = _LegacyCM()
    client = _tg_client(cm, tmp_path, monkeypatch)

    r = client.put("/api/telegram/settings/voice-reply",
                   json={"enabled": True})
    assert r.status_code == 200, r.text
    assert cm.save_calls == 1  # 无 save_overlay_patch → 回落整文件 save()
    assert cm.config["telegram"]["voice_reply"]["enabled"] is True


# ── 静态防回潮：四渠道路由不得再裸调 config_manager.save() ───────────────────

def test_channel_routes_have_no_bare_config_manager_save():
    """``config_manager.save()`` 只允许出现在 save_overlay_patch 兜底块内。

    判据：出现行的**前 12 行**必须包含 ``save_overlay_patch``（getattr 探测 +
    非 bool 回落的兜底惯用法）；新增任何裸直调（整文件回写主 config.yaml）
    立即红。settings_routes/persona_routes 属下一批治理，不在本门禁范围。
    """
    for name in _CHANNEL_ROUTE_FILES:
        text = (_ROUTES_DIR / name).read_text(encoding="utf-8")
        assert "save_overlay_patch" in text, (
            f"{name} 未接 save_overlay_patch——渠道保存退回整文件 save() 回潮"
        )
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "config_manager.save()" not in line:
                continue
            ctx = "\n".join(lines[max(0, i - 12):i])
            assert "save_overlay_patch" in ctx, (
                f"{name}:{i + 1} 出现裸 config_manager.save() 直调——运行时写"
                "配置必须走 save_overlay_patch（config.local.yaml overlay），"
                "仅允许在其兜底块内回落"
            )
