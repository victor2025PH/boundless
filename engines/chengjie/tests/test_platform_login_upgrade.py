"""104 升级场景的诊断层门禁（2026-07-31，接 test_platform_login_defaults）。

`test_platform_login_defaults.py` 在**读取函数**层验三态自愈；本文件上一层，验
接入弹窗真正消费的 `platform_readiness.diagnose_mode`：给一份**陈旧升级配置**
（缺 line/wa/messenger 开关）+ 桌面态，诊断出的 `reason_code` 不再是
`not_enabled` / `needs_server_setup`——即连接弹窗那张卡从「灰·未启用」变回可用。

这是 104 事故的端到端复刻（诊断级）：

- **打包冒烟 `desktop/build/smoke_backend.py` 覆盖的是全新安装**（`tempfile.mkdtemp`
  空目录 → 种子播种 → 全开），恰恰**测不到升级路径**——而 104 正是「旧数据目录 +
  新程序」。本门禁补上那一层，且不必起打包后端（CI 常驻）。
- 用 `diagnose_mode` 而非整条 HTTP 端点：它把 `provider_registered` / `service_ok`
  作为**注入参数**（见 platform_readiness 模块注释），故可隔离「okline 装没装 /
  sidecar 可不可达」这类与 104 正交的环境因素，只钉「开关有没有自愈」这**一个**
  不变量；也避免了全端点测试的全局 provider 注册副作用与网络探测。

fixture 用 104 实机 overlay 的真实形态（只有 telegram + orchestrator，缺其余三平台）。
纯函数 + 环境变量注入，不打网络、不起 app，CI 常驻。
"""
from __future__ import annotations

import pytest

from src.integrations.platform_readiness import diagnose_mode

# 104 实机 config.local.yaml 的真实陈旧形态（升级前旧种子只显式开了 telegram）：
# 缺 line / whatsapp / messenger 的接入开关，也没有它们的任何键。
STALE_UPGRADE_CFG = {
    "platform_login": {
        "enabled": True,
        "telegram": {"protocol_enabled": True},
        "orchestrator_enabled": True,
    }
}

# 隔离 orchestrator 单一变量：whatsapp 显式开（故不论桌面/服务器都无 not_enabled
# 硬 blocker——orchestrator_off 是软警告，仅在「无硬 blocker」时才挂），只有
# orchestrator 未写过。用它验 orchestrator 自愈在诊断层的表现（warn 有/无）。
WA_ON_NO_ORCH_CFG = {
    "platform_login": {
        "enabled": True,
        "whatsapp": {"protocol_enabled": True},
    }
}

# 升级安装缺开关的三平台（telegram 旧种子本就显式开，不在此列）。
_UPGRADE_HEAL_MODES = [
    ("line", "protocol"),
    ("whatsapp", "protocol"),
    ("messenger", "web"),
]
_STILL_OFF_CODES = {"not_enabled", "needs_server_setup"}


@pytest.mark.parametrize("platform,mode", _UPGRADE_HEAL_MODES)
def test_stale_upgrade_heals_at_diagnosis_desktop(platform, mode, monkeypatch):
    """陈旧升级配置 + 桌面态 → 诊断不再报 not_enabled/needs_server_setup。

    provider_registered/service_ok 注入 True，隔离 okline 安装 / sidecar 可达
    （与 104 正交）——只验「开关自愈」这一个不变量。line 若本机没装 okline 会得
    dep_missing，那也**不是** not_enabled，正确反映「开关已开、缺的是组件」。
    """
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    d = diagnose_mode(platform, mode, STALE_UPGRADE_CFG,
                      provider_registered=True, service_ok=True)
    assert d["reason_code"] not in _STILL_OFF_CODES, \
        f"{platform}/{mode} 陈旧升级配置在桌面态未自愈：{d}"


def test_stale_upgrade_still_blocked_on_server(monkeypatch):
    """探测器有效性自证：同一陈旧配置在服务器部署（无 AITR_DESKTOP_MODE）下，
    三平台仍报 not_enabled/needs_server_setup——证明上面那条绿是「桌面态才自愈」，
    不是断言恒真。服务器部署零行为变化的正向锚点。
    """
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    for platform, mode in _UPGRADE_HEAL_MODES:
        d = diagnose_mode(platform, mode, STALE_UPGRADE_CFG,
                          provider_registered=True, service_ok=True)
        assert d["reason_code"] in _STILL_OFF_CODES, \
            f"{platform}/{mode} 服务器态本应仍关：{d}"


def test_orchestrator_warn_cleared_on_desktop_upgrade(monkeypatch):
    """orchestrator 自愈的诊断层表现：连 orchestrator 都没写过的陈旧配置，桌面态下
    whatsapp（sidecar 就绪）不再挂 orchestrator_off 警告——弹窗不再对「其实会 7×24
    常驻」的号说「扫上了也不常驻」。
    """
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    d = diagnose_mode("whatsapp", "protocol", WA_ON_NO_ORCH_CFG,
                      provider_registered=True, service_ok=True)
    codes = {b.get("code") for b in d.get("blockers", [])}
    assert "orchestrator_off" not in codes, \
        f"桌面升级不该再报 orchestrator_off：{d}"


def test_orchestrator_warn_present_on_server_when_unset(monkeypatch):
    """探测器自证：同一「没写过 orchestrator」的配置在服务器态下，whatsapp 就绪但
    orchestrator 关 → 仍挂 orchestrator_off 警告（软警告，非阻断）。证明上一条绿有效。
    """
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    d = diagnose_mode("whatsapp", "protocol", WA_ON_NO_ORCH_CFG,
                      provider_registered=True, service_ok=True)
    codes = {b.get("code") for b in d.get("blockers", [])}
    assert "orchestrator_off" in codes, \
        f"服务器态未写 orchestrator 应报 orchestrator_off：{d}"
