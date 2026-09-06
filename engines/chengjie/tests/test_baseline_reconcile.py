"""桌面态产品基线增量补齐（ConfigManager._ensure_baseline，P1）门禁。

修的缺口：``_ensure_seeded`` 只在 config.yaml 缺失时播种 → **存量安装**永远拿
不到后来才进基线的功能（实锤：工作目标进种子后，已装用户仍看不见）。

语义钉住（重点是「不该动的绝不动」）：
- 仅 ``AITR_DESKTOP_MODE`` 真时生效——服务器实例（zhiliao 等）零变化；
- 判据＝合并视图键**缺失**（用户从未表达意见）；显式 true/false 都尊重；
- 写 overlay（config.local.yaml）不碰主 config.yaml 字节；
- 幂等：第二次 load 零写盘；
- **时序回归钉**：桌面首启（telegram 凭据为空 → _validate_config 失败、load()
  返回 False 且 main.py 忽略之）时补齐必须已经发生——首版若挂在 load 成功路径
  末尾就是这个形态下永不执行；
- 写入失败软降级，load 流程不受影响。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.utils.config_manager import ConfigManager

# 能通过 _validate_config 的完整配置（假凭据）
_VALID = """
telegram:
  api_id: "1234567"
  api_hash: "abcdef1234567890abcdef1234567890"
  phone_number: "+8613800000000"
ai:
  api_key: "sk-test"
skills:
  enabled: [greeting]
"""

# 桌面首启形态：与 config.desktop.min.yaml 同款空凭据（validation 必失败）
_DESKTOP_FIRSTBOOT = """
telegram:
  api_id: ""
  api_hash: ""
  phone_number: ""
ai:
  api_key: ""
skills:
  enabled: [greeting]
"""


def _mgr(tmp_path: Path, body: str) -> ConfigManager:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    return ConfigManager(config_path=str(cfg))


def _overlay(tmp_path: Path) -> Path:
    return tmp_path / "config.local.yaml"


async def test_desktop_fills_missing_key_into_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    mgr = _mgr(tmp_path, _VALID)
    assert await mgr.load() is True
    # 内存即时可见（2026-07-31 拍板后基线 = goals + deep_persona；bubbles 2026-09-06
    # D-L3 #210 降 B 类「出厂关」，不再补齐）
    assert mgr.get("companion.goals.enabled") is True
    assert mgr.get("companion.deep_persona.enabled") is True
    assert mgr.get("inbox.reply_style.bubbles.enabled") is None
    # 落盘在 overlay，不碰主 config
    ov = yaml.safe_load(_overlay(tmp_path).read_text(encoding="utf-8"))
    assert ov["companion"]["goals"]["enabled"] is True
    assert ov["companion"]["deep_persona"]["enabled"] is True
    assert "bubbles" not in ((ov.get("inbox") or {}).get("reply_style") or {}), \
        "拆条出厂关（D-L3）：基线不得再往 overlay 补 bubbles.enabled"
    main_txt = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "companion" not in main_txt, "主 config.yaml 字节必须原样"


async def test_runs_even_when_validation_fails_first_boot(tmp_path, monkeypatch):
    """时序回归钉：桌面首启 load() 返回 False，但基线补齐必须已经发生。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    mgr = _mgr(tmp_path, _DESKTOP_FIRSTBOOT)
    # 空凭据首启：load() 的返回值随 _validate_config 口径演进（桌面态现已容忍空凭据），
    # 本钉只管「不论成败，基线补齐都已发生」这条时序不变量
    await mgr.load()
    assert mgr.get("companion.goals.enabled") is True
    assert _overlay(tmp_path).exists()


async def test_explicit_false_in_main_config_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    explicit_off = (
        "\ncompanion:\n  goals:\n    enabled: false\n"
        "  deep_persona:\n    enabled: false\n"
        "inbox:\n  reply_style:\n    bubbles:\n      enabled: false\n")
    mgr = _mgr(tmp_path, _VALID + explicit_off)
    assert await mgr.load() is True
    assert mgr.get("companion.goals.enabled") is False
    assert mgr.get("companion.deep_persona.enabled") is False
    assert mgr.get("inbox.reply_style.bubbles.enabled") is False
    # 显式 false 的三键一个都不得被补写进 overlay（基线表另有其它 A 类键会正常补齐，
    # 故不再断言 overlay 文件不存在——那是基线只有三键时代的写法）
    ov = yaml.safe_load(_overlay(tmp_path).read_text(encoding="utf-8")) or {} \
        if _overlay(tmp_path).exists() else {}
    comp = ov.get("companion") or {}
    assert "enabled" not in (comp.get("goals") or {})
    assert "enabled" not in (comp.get("deep_persona") or {})
    assert "bubbles" not in ((ov.get("inbox") or {}).get("reply_style") or {})


async def test_partial_explicit_false_fills_only_missing(tmp_path, monkeypatch):
    """goals 显式关、其余缺失 → 只补缺失的，显式 false 原样（bubbles 已非基线）。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    mgr = _mgr(tmp_path, _VALID + "\ncompanion:\n  goals:\n    enabled: false\n")
    assert await mgr.load() is True
    assert mgr.get("companion.goals.enabled") is False
    assert mgr.get("companion.deep_persona.enabled") is True
    assert mgr.get("inbox.reply_style.bubbles.enabled") is None
    ov = yaml.safe_load(_overlay(tmp_path).read_text(encoding="utf-8"))
    # 显式 false 的键不进 overlay：goals.enabled 未被补写（L-5 D-L1 起 goals 段下
    # 另有 sprint.enabled 基线键会被补进来，故只看 enabled 这一键）
    assert "enabled" not in (((ov.get("companion") or {}).get("goals")) or {}), \
        "显式 false 的键不进 overlay"


async def test_explicit_false_in_overlay_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    _overlay(tmp_path).write_text(
        "companion:\n  goals:\n    enabled: false\n", encoding="utf-8")
    mgr = _mgr(tmp_path, _VALID)
    assert await mgr.load() is True
    assert mgr.get("companion.goals.enabled") is False
    ov = yaml.safe_load(_overlay(tmp_path).read_text(encoding="utf-8"))
    assert ov["companion"]["goals"]["enabled"] is False


async def test_non_desktop_untouched(tmp_path, monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    mgr = _mgr(tmp_path, _VALID)
    assert await mgr.load() is True
    assert mgr.get("companion.goals.enabled") is None
    assert not _overlay(tmp_path).exists()


async def test_idempotent_second_load_no_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    mgr = _mgr(tmp_path, _VALID)
    assert await mgr.load() is True
    first = _overlay(tmp_path).read_bytes()

    calls = []
    orig = ConfigManager.save_overlay_patch

    def _spy(self, patch, **kw):
        calls.append(patch)
        return orig(self, patch, **kw)

    monkeypatch.setattr(ConfigManager, "save_overlay_patch", _spy)
    mgr2 = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    assert await mgr2.load() is True
    assert calls == [], "键已存在（overlay 合并视图）→ 第二次启动零写盘"
    assert _overlay(tmp_path).read_bytes() == first


async def test_write_failure_soft(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setattr(
        ConfigManager, "save_overlay_patch", lambda self, patch, **kw: False)
    mgr = _mgr(tmp_path, _VALID)
    assert await mgr.load() is True, "补齐写入失败绝不拖垮启动"
    # 写失败 → 功能保持关闭（内存也不注入，报什么就是什么）
    assert mgr.get("companion.goals.enabled") is None
