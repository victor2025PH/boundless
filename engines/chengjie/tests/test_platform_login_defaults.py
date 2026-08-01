"""平台接入开关「随程序版本走」的默认表门禁（P0 补，2026-07-31）。

背景见 ``platform_login.resolve_login_switch`` 注释（104 事故：种子只播一次，升级
安装拿不到新平台的默认开）。``resolve_login_switch`` + ``_DESKTOP_LOGIN_DEFAULT_ON``
把「产品该点亮哪些接入方式」从只播一次的种子回归**代码默认**。本门禁双向钉住那张表：

- **表 → 种子**：表里每个键都必须在 ``config.desktop.min.yaml`` 里显式 ``true``——
  否则「桌面全新安装」（读种子）与「桌面升级安装」（读代码默认）两条路会点亮不同
  功能，正是 104 那台的成因；
- **种子 → 表**：种子里显式打开的每个接入开关（``*.protocol_enabled`` /
  ``*.web_enabled`` / ``orchestrator_enabled``）都必须在表里——否则新平台被 seed-on
  却无代码默认，下一次升级安装再次静默缺功能（104 的复发路径）。

外加 ``resolve_login_switch`` 的三态语义，与三个**登录方式**读取函数
（line/wa/messenger）的实际接线（桌面 + 空配置 → True = 升级自愈）。

``orchestrator_enabled`` 的接线**按读取者用途分档**（有原则的边界，非全改也非全不改）：
- **运行时闸门 + 用户可见判定**读有效值（走 ``resolve_login_switch``）——
  ``account_orchestrator.orchestrator_enabled``（决定号是否 7×24 常驻）与
  ``platform_readiness._orchestrator_on``（连接弹窗的 ``orchestrator_off`` 警告），
  桌面升级未写过时默认开，否则「扫上就掉线 + 弹窗误报不会常驻」；
- **诊断/审计类**（``config_check`` / ``companion_preflight`` / ``protocol_diagnostics``）
  **刻意仍读字面值**——它们回答「配置里到底写没写」，与「实际会不会常驻」正交，
  统一成有效值会让运维审计视角失真。该边界由 ``_orchestrator_on`` 处代码注释固化。

纯文件读取 + 纯函数，不打网络、不起 app，CI 常驻。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from src.integrations.platform_login import (
    _DESKTOP_LOGIN_DEFAULT_ON,
    resolve_login_switch,
)

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SEED = ENGINE_ROOT / "config" / "config.desktop.min.yaml"

# 接入启用开关的命名约定叶子名。device_fingerprint.enabled / sync.enabled /
# credpool.enabled / platform_login.enabled 等非「登录方式」开关据此天然排除。
_LOGIN_SWITCH_LEAVES = ("protocol_enabled", "web_enabled")
_ORCH_KEY = "platform_login.orchestrator_enabled"


def _seed_cfg() -> dict:
    cfg = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    assert isinstance(cfg, dict) and cfg, "桌面种子必须是非空 YAML mapping"
    return cfg


def _dig(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _seed_login_switches_on(cfg: dict) -> set:
    """种子里显式为 True 的接入开关点分键集合（按命名约定识别，不扫任意 bool）。"""
    out: set = set()
    pl = cfg.get("platform_login") or {}
    if not isinstance(pl, dict):
        return out
    if pl.get("orchestrator_enabled") is True:
        out.add(_ORCH_KEY)
    for plat, sect in pl.items():
        if not isinstance(sect, dict):
            continue
        for leaf, val in sect.items():
            if leaf in _LOGIN_SWITCH_LEAVES and val is True:
                out.add(f"platform_login.{plat}.{leaf}")
    return out


# ── 表 ↔ 种子 双向门禁 ──────────────────────────────────────────────

def test_table_keys_enabled_in_seed():
    cfg = _seed_cfg()
    missing = [k for k in sorted(_DESKTOP_LOGIN_DEFAULT_ON) if _dig(cfg, k) is not True]
    assert not missing, (
        "接入默认表里的开关没在桌面种子里打开——全新安装与升级安装会点亮不同功能：\n"
        + "\n".join(f"  {k}" for k in missing))


def test_seed_on_switches_all_in_table():
    cfg = _seed_cfg()
    orphan = sorted(_seed_login_switches_on(cfg) - set(_DESKTOP_LOGIN_DEFAULT_ON))
    assert not orphan, (
        "种子打开了接入开关但代码默认表没有它——升级安装会再次静默缺功能（104 复发类）：\n"
        + "\n".join(f"  {k}" for k in orphan))


# ── resolve_login_switch 三态语义 ──────────────────────────────────

@pytest.mark.parametrize("key", sorted(_DESKTOP_LOGIN_DEFAULT_ON))
def test_desktop_empty_config_defaults_on(key):
    # 升级安装（config 缺该键）+ 桌面态 → 默认开（104 自愈）。
    assert resolve_login_switch({}, key, desktop=True) is True


@pytest.mark.parametrize("key", sorted(_DESKTOP_LOGIN_DEFAULT_ON))
def test_server_empty_config_defaults_off(key):
    # 服务器部署（无 AITR_DESKTOP_MODE）零行为变化：未写过一律关。
    assert resolve_login_switch({}, key, desktop=False) is False


def test_explicit_false_respected_even_desktop():
    cfg = {"platform_login": {"line": {"protocol_enabled": False}}}
    assert resolve_login_switch(
        cfg, "platform_login.line.protocol_enabled", desktop=True) is False


def test_explicit_true_respected_even_server():
    cfg = {"platform_login": {"line": {"protocol_enabled": True}}}
    assert resolve_login_switch(
        cfg, "platform_login.line.protocol_enabled", desktop=False) is True


def test_unlisted_key_not_defaulted_on():
    # 表是白名单而非「桌面全开」：不在表里的键即便桌面态也不默认开。
    assert resolve_login_switch({}, "platform_login.telegram.web", desktop=True) is False


# ── 登录方式读取函数实际接线（桌面升级自愈；本轮补齐 messenger 曾漏接）──────

def _login_mode_readers():
    """四个登录方式开关的**canonical 读取函数**（全部消费方都经它们）。

    必须与 ``_DESKTOP_LOGIN_DEFAULT_ON`` 里的四个平台键一一对应——漏一个就等于那个
    平台的表项是死的（messenger 与 telegram 各出过一次这个形态）。
    """
    from src.integrations.line_protocol_login import protocol_enabled as line_on
    from src.integrations.messenger_web_login import web_enabled as mg_on
    from src.integrations.telegram_protocol_login import protocol_enabled as tg_on
    from src.integrations.whatsapp_baileys_login import protocol_enabled as wa_on
    return {
        "platform_login.line.protocol_enabled": line_on,
        "platform_login.whatsapp.protocol_enabled": wa_on,
        "platform_login.messenger.web_enabled": mg_on,
        "platform_login.telegram.protocol_enabled": tg_on,
    }


def test_every_login_mode_table_key_has_a_wired_reader():
    """表 → 读取函数的**覆盖性**：除 orchestrator（非登录方式，另测）外，表里每个
    平台键都必须在 ``_login_mode_readers`` 里有对应读取函数。

    这条是为了堵住「手工逐个核实」的不可靠——telegram 当初就是从这类断言里漏掉的。
    """
    covered = set(_login_mode_readers())
    expected = set(_DESKTOP_LOGIN_DEFAULT_ON) - {_ORCH_KEY}
    assert covered == expected, (
        f"登录方式表键与已接线读取函数不一致：缺 {sorted(expected - covered)}、"
        f"多 {sorted(covered - expected)}")


def test_login_mode_readers_wired_for_desktop_upgrade(monkeypatch):
    # 桌面升级（config 缺该键）→ 四平台读取函数全部自愈为 True。
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    for path, reader in _login_mode_readers().items():
        assert reader({}) is True, f"{path} 的读取函数未接三态（桌面升级不会自愈）"


def test_login_mode_readers_off_on_server(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    for path, reader in _login_mode_readers().items():
        assert reader({}) is False, f"{path} 在服务器部署本应保持默认关"


def test_messenger_explicit_false_respected(monkeypatch):
    # 运营显式关闭永远被尊重（含桌面态）——resolve_login_switch 三态的下半。
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    from src.integrations.messenger_web_login import web_enabled as mg_on
    assert mg_on({"platform_login": {"messenger": {"web_enabled": False}}}) is False


# ── orchestrator：运行时闸门 + 连接弹窗判定（按用途分档接线，见顶注）──────────

def test_orchestrator_runtime_readers_wired_for_desktop_upgrade(monkeypatch):
    # 桌面升级未写过 → 有效开：号重启不掉线（account_orchestrator）+ 弹窗不误报
    # 「不会常驻」（platform_readiness）。这是「扫上后能否 7×24 常驻」的收口。
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    from src.integrations.account_orchestrator import orchestrator_enabled
    from src.integrations.platform_readiness import _orchestrator_on
    assert orchestrator_enabled({}) is True
    assert _orchestrator_on({}) is True


def test_orchestrator_runtime_readers_off_on_server(monkeypatch):
    # 服务器部署零行为变化。
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    from src.integrations.account_orchestrator import orchestrator_enabled
    from src.integrations.platform_readiness import _orchestrator_on
    assert orchestrator_enabled({}) is False
    assert _orchestrator_on({}) is False


def test_orchestrator_explicit_false_respected(monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    from src.integrations.account_orchestrator import orchestrator_enabled
    from src.integrations.platform_readiness import _orchestrator_on
    cfg = {"platform_login": {"orchestrator_enabled": False}}
    assert orchestrator_enabled(cfg) is False
    assert _orchestrator_on(cfg) is False


# ── 旁路守卫：接入开关不得再被字面直读 ──────────────────────────────────────
#
# messenger 与 telegram 两次都是同一形态：键进了 ``_DESKTOP_LOGIN_DEFAULT_ON``，读取
# 函数却仍 ``bool(cfg.get("...", False))`` → 表项对该平台等于死的，升级安装照样灰。
# 逐个手工核实已被证明不可靠（telegram 就从早期那条断言里漏了过去），故用源码扫描把
# 不变量变成自我强制：运行时/UI 读取一律走 resolve_login_switch。
#
# 白名单＝**仅**三个诊断/审计模块（它们回答「配置里到底写没写」，与「实际会不会生效」
# 正交；边界理由见 platform_readiness._orchestrator_on 处注释）。
_LITERAL_READ_RE = re.compile(
    r"""\.get\(\s*["'](protocol_enabled|web_enabled|orchestrator_enabled)["']""")
_LITERAL_READ_ALLOWED = {
    "src/utils/config_check.py",
    "src/ops/companion_preflight.py",
    "src/integrations/protocol_diagnostics.py",
}


def test_no_literal_switch_reads_outside_diagnostics():
    offenders = []
    for p in (ENGINE_ROOT / "src").rglob("*.py"):
        rel = p.relative_to(ENGINE_ROOT).as_posix()
        if rel in _LITERAL_READ_ALLOWED:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 - 读不了的文件不该拖垮门禁
            continue
        for m in _LITERAL_READ_RE.finditer(text):
            offenders.append(
                f"  {rel}:{text[:m.start()].count(chr(10)) + 1} → .get({m.group(1)!r})")
    assert not offenders, (
        "接入开关被字面直读（绕过 resolve_login_switch 三态）——升级安装不会自愈：\n"
        + "\n".join(offenders)
        + "\n改用 resolve_login_switch(config, '<点分路径>')；确属诊断/审计视角"
          "请加入 _LITERAL_READ_ALLOWED 并说明理由。")


def test_literal_read_allowlist_not_stale():
    """白名单不得过期：三个诊断模块必须**真的**还在字面直读。

    若它们已被改成有效值（运维审计视角就会失真）或文件被移走，这条会红，逼着更新
    白名单与边界说明，而不是让守卫悄悄放空。
    """
    for rel in sorted(_LITERAL_READ_ALLOWED):
        p = ENGINE_ROOT / rel
        assert p.exists(), f"白名单条目文件不存在，需更新：{rel}"
        assert _LITERAL_READ_RE.search(p.read_text(encoding="utf-8")), (
            f"{rel} 已不再字面直读——若是刻意改为有效值，请移出白名单并同步"
            "更新 platform_readiness._orchestrator_on 处的边界说明")
