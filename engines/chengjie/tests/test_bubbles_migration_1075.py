"""#210 / D-L3（L-1 A，2026-09-06）：拆条形态 1.0.75 收紧的存量热修。

82BF95：skuio 机 ``config.local.yaml`` 里 ``inbox.reply_style.bubbles`` =
``enabled: true / per_sentence: true / max_parts: 2``——两句英文短回复被逐句拆成
6–15s 固定两拍，客户当场识破 AI。纯函数层新默认（仅显式换行才拆 + 短回复门 80）
管不到显式写着 ``per_sentence: true`` 的存量 overlay，故 ``ConfigManager.load()``
加一次性迁移：per_sentence true→false，并写 ``explicit_newline_only: true`` 作幂等
标记。本文件钉：

1. skuio 形态 → 迁移落 overlay（只动两键，enabled/max_parts 不碰）+ WARNING 一行；
   再 load 一次字节不变、不再告警（幂等）；
2. 运营已对 ``explicit_newline_only`` 表态（无论 true/false）→ 一个字不动；
3. per_sentence 本就为 false / 无 bubbles 段 → 零写盘（不凭空造 overlay）；
4. 服务器实例（无 AITR_DESKTOP_MODE）同样迁移——D-L3 是产品级决策，不分包型。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

ENGINE_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_MIN = ENGINE_ROOT / "config" / "config.desktop.min.yaml"
_LOGGER = "ai_chat_assistant.ConfigManager"
_MARK = "拆条已按 1.0.75 默认收紧"


def _dig(doc, path: str):
    node = doc
    for k in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def _mk_manager(tmp_path: Path, monkeypatch, *, overlay: dict | None,
                desktop: bool = False):
    from src.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(DESKTOP_MIN.read_text(encoding="utf-8"), encoding="utf-8")
    if overlay is not None:
        (cfg_path.parent / "config.local.yaml").write_text(
            yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False),
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


_SKUIO_OVERLAY = {"inbox": {"reply_style": {"bubbles": {
    "enabled": True, "per_sentence": True, "max_parts": 2}}}}


def test_skuio_overlay_is_migrated_and_idempotent(tmp_path, monkeypatch, caplog):
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, overlay=_SKUIO_OVERLAY,
                               desktop=True)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    hits = [r for r in caplog.records if _MARK in r.getMessage()]
    assert len(hits) == 1 and hits[0].levelno == logging.WARNING

    # 合并视图：逐句关、仅显式换行开；运营原本的 enabled / max_parts 原样
    bub = _dig(cm.config, "inbox.reply_style.bubbles")
    assert bub["per_sentence"] is False
    assert bub["explicit_newline_only"] is True
    assert bub["enabled"] is True and bub["max_parts"] == 2
    # overlay 落盘同口径
    ov = _dig(_overlay_of(cfg_path), "inbox.reply_style.bubbles")
    assert ov == {"enabled": True, "per_sentence": False, "max_parts": 2,
                  "explicit_newline_only": True}
    # 主 config.yaml 一个字不动
    assert cfg_path.read_text(encoding="utf-8") == DESKTOP_MIN.read_text(encoding="utf-8")

    # 幂等：再 load（＝热重载路径）overlay 字节不变、不再告警
    before = (cfg_path.parent / "config.local.yaml").read_bytes()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    assert (cfg_path.parent / "config.local.yaml").read_bytes() == before
    assert not [r for r in caplog.records if _MARK in r.getMessage()]

    # 迁移后的有效配置喂给拆条纯函数：事故原句整条单发
    from src.inbox.reply_split import parse_bubbles_cfg, split_reply_parts
    d = parse_bubbles_cfg(cm.config)
    text = ("I guess you just bring out a different side of me here. "
            "But it's still me, just a little more relaxed with you.")
    assert split_reply_parts(
        text, max_parts=d["max_parts"], max_chars=d["max_chars"],
        min_tail_chars=d["min_tail_chars"], min_total_chars=d["min_total_chars"],
        per_sentence=d["per_sentence"],
        explicit_newline_only=d["explicit_newline_only"]) == [text]


def test_explicit_operator_choice_is_respected(tmp_path, monkeypatch, caplog):
    """运营显式写了 explicit_newline_only（这里是 false＝要算法档）→ 不迁移。"""
    ov = {"inbox": {"reply_style": {"bubbles": {
        "enabled": True, "per_sentence": True, "explicit_newline_only": False}}}}
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, overlay=ov)
    before = (cfg_path.parent / "config.local.yaml").read_bytes()
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    assert (cfg_path.parent / "config.local.yaml").read_bytes() == before
    assert _dig(cm.config, "inbox.reply_style.bubbles.per_sentence") is True
    assert _dig(cm.config, "inbox.reply_style.bubbles.explicit_newline_only") is False
    assert not [r for r in caplog.records if _MARK in r.getMessage()]
    assert cm.bubbles_migration_patch() is None


def test_no_per_sentence_means_zero_write(tmp_path, monkeypatch):
    """per_sentence 本就为 false / 无 bubbles 段：不凭空造 overlay。"""
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, overlay=None)
    asyncio.run(cm.load())
    assert cm.bubbles_migration_patch() is None
    ov = _overlay_of(cfg_path)
    assert _dig(ov, "inbox.reply_style.bubbles") is None

    cm2, cfg2 = _mk_manager(
        tmp_path / "b", monkeypatch,
        overlay={"inbox": {"reply_style": {"bubbles": {
            "enabled": True, "per_sentence": False}}}})
    asyncio.run(cm2.load())
    assert _dig(_overlay_of(cfg2), "inbox.reply_style.bubbles") == {
        "enabled": True, "per_sentence": False}


def test_server_instance_migrates_too(tmp_path, monkeypatch, caplog):
    """无桌面态 env（坐席机/服务器实例）同样收紧——决策是产品级的。"""
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, overlay=_SKUIO_OVERLAY,
                               desktop=False)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    assert [r for r in caplog.records if _MARK in r.getMessage()]
    assert _dig(cm.config, "inbox.reply_style.bubbles.per_sentence") is False
    assert _dig(_overlay_of(cfg_path),
                "inbox.reply_style.bubbles.explicit_newline_only") is True
