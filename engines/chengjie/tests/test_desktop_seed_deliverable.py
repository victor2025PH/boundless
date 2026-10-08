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
实现了 ②它的运行时依赖真的在安装包里**。

分工（2026-10-08 蛋博士定，CI 基线 R7）：「依赖真在构建机上」（services/*/node_modules、
随包兜底 Chromium）只有打包流水线才有意义——CI 检出里天然没有，在 CI 查只会恒红。
该判据整体迁到 ``desktop/build/seed_deliverable_gate.py``，在每个 ``predist*`` 里紧跟
edition 门禁以 bundle 档执行（缺件 → 退出码 1，打包中止；产物侧 after-pack.js 再核一遍）。
本文件 CI 常驻部分只查**声明与清单**（同一张判据表的 declared 档），并钉住
「门禁真接进了每条打包链」「bundle 档缺件真的会拦」，保证迁移不削弱拦截力度。
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

GATE_PATH = ENGINE_ROOT / "desktop" / "build" / "seed_deliverable_gate.py"


def _gate():
    """按路径加载打包门禁模块（desktop/build 不是包）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("seed_deliverable_gate", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    未实现的方式（如 whatsapp/web）恒报 not_implemented —— 摆在弹窗里就是一个
    永不可用的灰选项（前端会诚实标「规划中」并导流，但种子没必要主动列它）。
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


def test_seed_modes_dependencies_are_declared():
    """种子列出的每个非 device 方式：有判据、源码在仓、随包已声明、Python 依赖已启用。

    （「依赖真装在构建机上」由打包门禁 bundle 档在 predist 里拦，见下两条。）
    """
    problems = _gate().check("declared")
    assert not problems, (
        "种子开了方式但交付物声明不全（用户点下去会等来 service_down）：\n  - "
        + "\n  - ".join(problems))


#: 产出安装包的打包链（mac 链目前不跑任何 python 门禁，产物侧由 after-pack.js 兜）
_PACKAGING_PREDIST = ("predist", "predist:win", "predist:win:clean", "predist:win:lite",
                      "predist:win:public", "predist:win:internal")


def test_bundle_gate_wired_into_every_packaging_chain():
    """每条 Windows 打包链的 predist 都必须以 bundle 档（默认档）跑本门禁，且排在
    electron-builder 之前、紧跟 edition 门禁（无 edition 门禁的链紧跟后端新鲜度检查）。"""
    scripts = (json.loads(DESKTOP_PKG.read_text(encoding="utf-8")).get("scripts") or {})
    bad = []
    for name in _PACKAGING_PREDIST:
        cmd = str(scripts.get(name) or "")
        steps = [x.strip() for x in cmd.split("&&")]
        hit = [k for k, st in enumerate(steps) if "build/seed_deliverable_gate.py" in st]
        if len(hit) != 1:
            bad.append(f"{name}：应恰好调用一次 build/seed_deliverable_gate.py（现 {len(hit)} 次）")
            continue
        st = steps[hit[0]]
        if "--declared" in st:
            bad.append(f"{name}：打包链不得用 --declared（那是 CI 档，会放过缺 node_modules/Chromium）")
        anchor = [k for k, x in enumerate(steps) if "build/edition_gate.py" in x] or \
                 [k for k, x in enumerate(steps) if "build/check_backend_freshness.py" in x]
        if not anchor or hit[0] != anchor[-1] + 1:
            bad.append(f"{name}：门禁应紧跟 edition 门禁/后端新鲜度检查之后")
    assert not bad, "打包门禁接线不全：\n  - " + "\n  - ".join(bad)


def test_bundle_gate_blocks_missing_node_modules_and_chromium(tmp_path):
    """bundle 档拦截力度不低于迁移前的 CI 判据：缺 node_modules / 缺 headed Chromium 必拦，
    齐备才放行；declared 档对同一棵树放行（CI 不因构建产物缺席恒红）。"""
    import shutil

    gate = _gate()
    root = tmp_path / "eng"
    (root / "config").mkdir(parents=True)
    (root / "desktop" / "build").mkdir(parents=True)
    shutil.copy(SEED, root / "config" / SEED.name)
    shutil.copy(DESKTOP_PKG, root / "desktop" / "package.json")
    shutil.copy(REQUIREMENTS, root / "requirements.txt")
    shutil.copy(BUILD_BACKEND, root / "desktop" / "build" / "build_backend.py")
    for name in ("whatsapp-baileys", "messenger-web"):
        svc = root / "services" / name
        svc.mkdir(parents=True)
        (svc / "server.js").write_text("//", encoding="utf-8")
        (svc / "package-lock.json").write_text("{}", encoding="utf-8")
    pl = {"enabled": True,
          "whatsapp": {"modes": ["device", "protocol"]},
          "messenger": {"modes": ["device", "web"]}}

    assert gate.check("declared", root=root, platform_login=pl) == []
    miss = gate.check("bundle", root=root, platform_login=pl)
    assert any("whatsapp-baileys/node_modules" in x for x in miss), miss
    assert any("messenger-web/node_modules" in x for x in miss), miss

    for name in ("whatsapp-baileys", "messenger-web"):
        (root / "services" / name / "node_modules").mkdir()
    browsers = root / "services" / "messenger-web" / "node_modules" / "playwright-core" / ".local-browsers"
    browsers.mkdir(parents=True)
    (browsers / "chromium_headless_shell-1200").mkdir()   # 无头版不算
    miss = gate.check("bundle", root=root, platform_login=pl)
    assert len(miss) == 1 and "Chromium" in miss[0], miss

    (browsers / "chromium-1200").mkdir()
    assert gate.check("bundle", root=root, platform_login=pl) == []
    assert gate.main(["--root", str(root)]) in (0, 1)   # CLI 可跑（真树结果取决于种子）


def test_gate_has_checker_for_every_seed_mode():
    """新方式进种子必须同时在门禁判据表里补一条（否则 bundle 档会报「无判据」中止打包）。"""
    gate = _gate()
    missing = sorted(
        f"{plat}/{mode}"
        for plat, pcfg in _seed_platform_login().items() if isinstance(pcfg, dict)
        for mode in (pcfg.get("modes") or [])
        if mode != "device" and (plat, mode) not in gate.DELIVERABILITY)
    assert not missing, f"门禁 DELIVERABILITY 缺判据：{missing}"


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
