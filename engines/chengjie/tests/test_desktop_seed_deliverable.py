"""桌面随包种子的「开关 ↔ 交付物」一致性门禁（2026-07-30 实锤后加）。

事故形态（客户侧实锤）：`config/config.desktop.min.yaml` 刻意把 `platform_login`
整段留在代码默认 = 关，于是**下载安装后**打开「接入 Telegram / WhatsApp / LINE /
Messenger」，每一种扫码方式都灰着显示「未启用 / 需运维配置」，只剩一个要求用户
自备安卓手机的「真机 / 模拟器」。产品要求是「下载即可用」，而开关根本没随包配。

修法是在种子里显式打开，但那立刻引入**第二个、更隐蔽的**失败形态：
开关开了、干活的东西却没进安装包。两种都是静默的——

- 开了 `whatsapp.protocol_enabled` 但 baileys 边车没随包 → 用户点下去等二维码，
  等来的是 service_down（比灰着更让人困惑：看起来能用）；
- 把 `messenger.web` / `line.protocol` / `*.web` 留在 modes 清单里 → 那是本系统
  **未实现**或**依赖未随包**的方式，界面给出一个点了没反应的灰选项，用户会一直
  以为是自己没配对。

故本门禁钉住一条不变量：**种子里出现的每一个非 device 方式，都必须 ①本系统真的
实现了 ②它的运行时依赖真的在安装包里**。纯文件读取，不需要先打包，CI 常驻。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SEED = ENGINE_ROOT / "config" / "config.desktop.min.yaml"
DESKTOP_PKG = ENGINE_ROOT / "desktop" / "package.json"
SIDECAR_LAUNCHER = ENGINE_ROOT / "desktop" / "sidecar-launcher.js"
REQUIREMENTS = ENGINE_ROOT / "requirements.txt"
BUILD_BACKEND = ENGINE_ROOT / "desktop" / "build" / "build_backend.py"

#: 每个非 device 方式的「运行时依赖在不在包里」判据。
#: 值 = (人话描述, 判定函数)。判定函数返回空串＝依赖齐备，否则返回缺什么。
#:
#: 刻意按「安装包里有没有」判，而不是按「本机装了没有」——本机装了不代表客户装了，
#: 那正是这类事故能长期潜伏的原因。


def _wa_baileys_bundled() -> str:
    """WhatsApp 协议边车：源码 + 依赖在仓库里，且 electron-builder 声明随包。"""
    svc = ENGINE_ROOT / "services" / "whatsapp-baileys"
    if not (svc / "server.js").is_file():
        return "services/whatsapp-baileys/server.js 不存在"
    if not (svc / "node_modules").is_dir():
        return ("services/whatsapp-baileys/node_modules 不存在——打包前须先 "
                "`cd services/whatsapp-baileys && npm ci`，否则边车随包后必 MODULE_NOT_FOUND")
    pkg = json.loads(DESKTOP_PKG.read_text(encoding="utf-8"))
    extra = ((pkg.get("build") or {}).get("extraResources") or [])
    declared = any("whatsapp-baileys" in str(e.get("to") or e.get("from") or "")
                   for e in extra if isinstance(e, dict))
    if not declared:
        return "desktop/package.json 的 build.extraResources 没声明 whatsapp-baileys（不会进安装包）"
    return ""


def _messenger_web_bundled() -> str:
    """Messenger 托管登录：边车源码 + 依赖 + **兜底浏览器**齐备，且声明随包。

    浏览器这一项单独查：服务默认走系统真 Chrome，客户机没装 Chrome 时才回落随包
    Chromium。若连随包 Chromium 都没有（`playwright install` 没在构建机跑过），
    那些没装 Chrome 的机器就是「点了登录、浏览器起不来」——而这在我们这台装了
    Chrome 的开发机上永远复现不出来。
    """
    svc = ENGINE_ROOT / "services" / "messenger-web"
    if not (svc / "server.js").is_file():
        return "services/messenger-web/server.js 不存在"
    if not (svc / "node_modules").is_dir():
        return ("services/messenger-web/node_modules 不存在——打包前须先 "
                "`cd services/messenger-web && npm ci`")
    browsers = svc / "node_modules" / "playwright-core" / ".local-browsers"
    # `chromium-*` 只匹配完整（可 headed）构建；headless shell 是 chromium_headless_shell-*
    # （下划线），刻意不算——桌面登录是人工交互，要的是能显示窗口的那个。
    if not browsers.is_dir() or not any(browsers.glob("chromium-*")):
        return ("随包兜底 Chromium 缺失——打包前须在构建机跑 "
                "`cd services/messenger-web && PLAYWRIGHT_BROWSERS_PATH=0 npx playwright "
                "install chromium`（Windows 用 `$env:PLAYWRIGHT_BROWSERS_PATH='0'`）。"
                "缺了它，没装 Chrome 的客户机点登录后浏览器起不来")
    pkg = json.loads(DESKTOP_PKG.read_text(encoding="utf-8"))
    extra = ((pkg.get("build") or {}).get("extraResources") or [])
    declared = any("messenger-web" in str(e.get("to") or e.get("from") or "")
                   for e in extra if isinstance(e, dict))
    if not declared:
        return "desktop/package.json 的 build.extraResources 没声明 messenger-web（不会进安装包）"
    return ""


def _line_okline_bundled() -> str:
    """LINE 协议需 okline；它在 requirements.txt 里是**注释掉的**可选项。"""
    txt = REQUIREMENTS.read_text(encoding="utf-8", errors="replace")
    active = [ln for ln in txt.splitlines()
              if re.match(r"^\s*okline\b", ln)]
    if not active:
        return ("okline 未在 requirements.txt 启用（当前是注释行）→ 不会装进打包环境，"
                "LINE 协议方式在安装版恒不可用。它是逆向库、违反 LINE ToS 且有封号风险，"
                "是否随包分发属产品/法务决策")
    return ""


def _telegram_protocol_bundled() -> str:
    """Telegram 协议：pyrogram 随包 + 中央凭据池瘦客户端随包（否则用户被逼自己申请 api_id）。"""
    txt = REQUIREMENTS.read_text(encoding="utf-8", errors="replace")
    if not any(re.match(r"^\s*pyrogram\b", ln) for ln in txt.splitlines()):
        return "pyrogram 未在 requirements.txt 启用"
    build = BUILD_BACKEND.read_text(encoding="utf-8", errors="replace")
    if "credpool" not in build:
        return ("desktop/build/build_backend.py 没把 platform/credpool 打进包 → 中央池静默失效，"
                "用户被逼去 my.telegram.org 自己申请 api_id")
    return ""


DELIVERABILITY = {
    ("telegram", "protocol"): _telegram_protocol_bundled,
    ("whatsapp", "protocol"): _wa_baileys_bundled,
    ("messenger", "web"): _messenger_web_bundled,
    ("line", "protocol"): _line_okline_bundled,
}


def _seed() -> dict:
    return yaml.safe_load(SEED.read_text(encoding="utf-8")) or {}


def _seed_platform_login() -> dict:
    return (_seed().get("platform_login") or {})


def test_seed_configures_platform_login_at_all():
    """种子必须显式配 platform_login——留给代码默认就是「所有接入方式全灰」。"""
    pl = _seed_platform_login()
    assert pl, (
        f"{SEED.name} 缺 platform_login 段：代码默认全关 → 客户装完打开接入弹窗，"
        "每种扫码方式都显示「未启用」，只剩需自备手机的「真机/模拟器」")
    assert pl.get("enabled") is True, "platform_login.enabled 必须显式为 true"
    assert pl.get("orchestrator_enabled") is True, (
        "orchestrator_enabled 关着 → 扫码能成但重启要重扫（诊断出 orchestrator_off），"
        "对桌面用户等于能力只交付一半")


def test_seed_modes_are_implemented():
    """种子列出的每个非 device 方式，本系统必须真的实现了。

    未实现的方式（如 whatsapp/web、telegram/web）恒报 not_enabled —— 摆在弹窗里
    就是一个点了没反应的灰选项，比不显示更糟。
    """
    from src.integrations.platform_readiness import _IMPLEMENTED_MODES

    offenders = []
    for platform, pcfg in _seed_platform_login().items():
        if not isinstance(pcfg, dict):
            continue
        for mode in (pcfg.get("modes") or []):
            if mode == "device":
                continue
            if (platform, mode) not in _IMPLEMENTED_MODES:
                offenders.append(f"{platform}/{mode}")
    assert not offenders, (
        f"种子 modes 里有本系统未实现的方式：{offenders}。"
        "它们在界面上恒显示「未启用」且点不动——请从 modes 清单里摘掉，"
        "而不是留给用户猜是不是自己没配对")


def test_seed_modes_dependencies_are_bundled():
    """种子列出的每个非 device 方式，其运行时依赖必须真在安装包里。"""
    problems = []
    for platform, pcfg in _seed_platform_login().items():
        if not isinstance(pcfg, dict):
            continue
        for mode in (pcfg.get("modes") or []):
            if mode == "device":
                continue
            checker = DELIVERABILITY.get((platform, mode))
            if checker is None:
                problems.append(
                    f"{platform}/{mode}：本门禁没有它的「依赖在不在包里」判据，"
                    "请在 DELIVERABILITY 里补一条（新方式进种子必须同时补判据）")
                continue
            why = checker()
            if why:
                problems.append(f"{platform}/{mode}：{why}")
    assert not problems, (
        "种子开了方式但交付物不在安装包里（用户点下去会等来 service_down）：\n  - "
        + "\n  - ".join(problems))


def test_protocol_enabled_implies_mode_listed():
    """开了 protocol_enabled 就必须把该方式列进 modes（否则开关是死的），反之亦然。"""
    mismatches = []
    for platform, pcfg in _seed_platform_login().items():
        if not isinstance(pcfg, dict):
            continue
        modes = list(pcfg.get("modes") or [])
        for flag, mode in (("protocol_enabled", "protocol"), ("web_enabled", "web")):
            on = bool(pcfg.get(flag))
            listed = mode in modes
            if on and modes and not listed:
                mismatches.append(
                    f"{platform}.{flag}=true 但 modes={modes} 不含 {mode} → 开关是死的")
            if listed and not on:
                mismatches.append(
                    f"{platform} 的 modes 含 {mode} 但 {flag} 未开 → 界面恒显示未启用")
    assert not mismatches, "种子开关与 modes 清单不一致：\n  - " + "\n  - ".join(mismatches)


#: 种子里的服务地址键 → sidecar-launcher.js 的 SPECS 键
_PORT_PAIRS = (("whatsapp", "baileys_url", "whatsapp"),
               ("messenger", "web_url", "messenger"))


def test_sidecar_ports_match_launcher():
    """种子里的服务地址端口必须与桌面壳拉起边车用的端口一致。

    两处各写一个数字是典型漂移点：改了一边，后端就去打一个没人听的端口，接入弹窗
    显示 service_down，而两边配置单看都「没错」。
    """
    js = SIDECAR_LAUNCHER.read_text(encoding="utf-8", errors="replace")
    pl = _seed_platform_login()
    checked = 0
    for platform, url_key, spec_key in _PORT_PAIRS:
        url = str((pl.get(platform) or {}).get(url_key) or "")
        if not url:
            continue  # 未配即用代码默认，无漂移风险
        m = re.search(r":(\d+)", url)
        assert m, f"{platform}.{url_key} 形态异常：{url!r}"
        seed_port = int(m.group(1))

        # 从 SPECS 里那个边车的块内取 port（各块之间不会串味：name 唯一）
        block = re.search(
            rf"{spec_key}:\s*\{{(.*?)\n  \}}", js, re.S)
        assert block, f"sidecar-launcher.js 的 SPECS 里找不到 {spec_key}（本门禁前提失效）"
        m2 = re.search(r"port:\s*(\d+)", block.group(1))
        assert m2, f"SPECS.{spec_key} 里找不到 port（本门禁前提失效）"
        launcher_port = int(m2.group(1))

        assert seed_port == launcher_port, (
            f"端口漂移：种子 {platform}.{url_key}={seed_port} vs "
            f"SPECS.{spec_key}.port={launcher_port} → 后端会去打一个没人听的端口，"
            "接入弹窗报 service_down")
        checked += 1
    assert checked, "一个端口对都没校到——种子里的服务地址键改名了？本门禁会变成空转"


def test_seed_keeps_hosted_chain_on():
    """种子不得关掉托管链——Telegram「用户免申请 api_id」全靠它。

    链路：main.py 启动 → hosted_gateway.ensure_hosted_telegram → 官网
    /api/pool/telegram-cred（Bearer 设备令牌）→ 注入内存 telegram.api_id/api_hash。
    闸门是 `licensing.hosted_ai.enabled`：桌面态**缺省即开**（_wants_hosted 认
    AITR_DESKTOP_MODE），所以种子里只要别显式写 false 就行。写了 false 是静默的——
    聊天照跑、只是接入那一步变成要用户自己去 my.telegram.org 申请。
    """
    hosted = ((_seed().get("licensing") or {}).get("hosted_ai") or {})
    assert hosted.get("enabled") is not False, (
        "种子把 licensing.hosted_ai.enabled 设成了 false → Telegram 凭据派发链断开，"
        "用户被迫自己申请 api_id（而这正是接入转化率最大的黑洞）")


def test_seed_never_ships_plaintext_credpool_token():
    """随包种子里绝不能有中央池服务令牌明文——安装包是公开可下载的。

    令牌泄露 = 池子被任意人刷。真值只能由桌面壳经 env 注入（CREDPOOL_SERVICE_TOKEN）。
    """
    cp = (((_seed_platform_login().get("telegram") or {}).get("credpool")) or {})
    for key in ("service_token", "license_key"):
        val = str(cp.get(key) or "").strip()
        assert not val, (
            f"种子 credpool.{key} 有明文值（{val[:8]}…）——安装包公开可下载，"
            "任何人都能提取。请留空并由桌面壳经环境变量注入")
