# -*- coding: utf-8 -*-
"""deploy/desktop/install_chatx_node.ps1 静态不变量（2026-09-17 .173 三坑沉淀）。

这脚本在坐席机上跑，没有任何自动化测试盖它；三次 .173 事故都是「本地看着对、
上机才炸」。这里把三次事故各钉一条，外加 PS 家族的两条老规矩：

  I1 ASCII-only —— PS 5.1 把无 BOM 文件按 GBK 解码，CJK 字面量必乱码
     （watchdog_emotion_tts.ps1 教训）；产品名只许从码点拼。
  I2 卸载注册表键 GUID == uuid5(electron-builder ns, package.json appId) ——
     手工补的注册表项必须落在安装器会写的同一把键下，否则下次升级找不到旧装。
  I3 安装器不许 `-Wait` 阻塞等 —— 第三坑里 stub 空转 7 小时、脚本跟着挂 7 小时，
     坐席无包可用；必须走 watchdog 轮询（-PassThru + HasExited）。
  I4 旁路只在「整棵树验过」后动手：build-info 版本比对 + 树 ≥ 压缩包体积（半棵树不装）
     + robocopy /MIR（旧文件镜像掉）+ 删悬空 UninstallString + 清 ns*.tmp（.173 曾堆 20 GB）。
  I5 旁路结果走 $script:bypassOk，函数体内禁止 `return $true/$false` ——
     Say() 写输出流，`$ok = fn` 会把日志行吞进返回值，$false 也被判真（harness 实测）。
  I6 PowerShell 语法可解析（有 powershell 才跑，否则 skip）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy" / "desktop" / "install_chatx_node.ps1"
PKG = ROOT / "engines" / "chengjie" / "desktop" / "package.json"
ELECTRON_BUILDER_NS = uuid.UUID("50e065bc-3134-11e6-9bab-38c9862bdaf3")

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="deploy/desktop not in this checkout")


def _src() -> str:
    return SCRIPT.read_bytes().decode("ascii")


def _fn_body(src: str, name: str) -> str:
    m = re.search(r"^function " + re.escape(name) + r"\b.*?^}", src, re.S | re.M)
    assert m, f"function {name} missing"
    return m.group(0)


def test_i1_ascii_only():
    raw = SCRIPT.read_bytes()
    bad = [i for i, c in enumerate(raw) if c > 127]
    assert not bad, f"non-ASCII bytes at offsets {bad[:5]} (PS5.1 GBK decode hazard)"
    src = raw.decode("ascii")
    assert "[char]0x667A + [char]0x804A" in src, "product name must be built from code points"


def test_i2_uninstall_guid_matches_app_id():
    src = _src()
    m = re.search(r"\$uninstallGuid = '([0-9a-f-]{36})'", src)
    assert m, "uninstall GUID constant missing"
    app_id = json.loads(PKG.read_text(encoding="utf-8"))["build"]["appId"]
    expect = str(uuid.uuid5(ELECTRON_BUILDER_NS, app_id))
    assert m.group(1) == expect, f"GUID {m.group(1)} != uuid5(ns, {app_id}) = {expect}"


def test_i3_installer_runs_under_watchdog_not_wait():
    src = _src()
    assert re.search(r"Start-Process -FilePath \$Setup -ArgumentList '/S' -PassThru\s*$", src, re.M)
    assert not re.search(r"-FilePath \$Setup -ArgumentList '/S'[^\n]*-Wait", src), "silent install must not -Wait"
    assert "while (-not $p.HasExited)" in src
    for var in ("$HangIdleSec", "$HardCapSec"):
        assert re.search(re.escape(var) + r" = \d+", src), f"{var} not defined"
    assert "Complete-FromExtracted $outDir $archive" in src, "hang path must call the bypass"


def test_i4_bypass_verifies_tree_then_mirrors_and_cleans():
    src = _src()
    body = _fn_body(src, "Complete-FromExtracted")
    assert "resources\\build-info.json" in body
    assert '"version"' in body and "$setupVersion" in body, "must compare build-info version to setup version"
    assert "$m2.bytes -lt $arcLen" in body, "partial-extraction guard (tree >= archive) missing"
    assert "'/MIR'" in body, "robocopy must mirror (stale files from a skipped uninstall must go)"
    assert "$m3.files -ne $m2.files" in body, "must verify INSTDIR == tree after copy"
    for name in ("UninstallString", "QuietUninstallString"):
        assert f"Remove-ItemProperty -Path $regKey -Name '{name}'" in body
    assert "ChatXManualInstall" in body
    # plugin dirs of killed/bypassed stubs are removed (only those from this attempt)
    assert re.search(r"-Filter 'ns\*\.tmp'[\s\S]{0,200}CreationTime -ge \$tStart", src)


def test_i5_bypass_result_via_script_flag_not_output_stream():
    src = _src()
    body = _fn_body(src, "Complete-FromExtracted")
    assert "$script:bypassOk = $false" in body and "$script:bypassOk = $true" in body
    assert not re.search(r"return \$(true|false)", body), "Say() shares the output stream; never return booleans"
    assert "if ($script:bypassOk)" in src
    assert not re.search(r"\$\w+ = Complete-FromExtracted", src), "call bare (assigning swallows the log lines)"


@pytest.mark.skipif(shutil.which("powershell") is None, reason="powershell not available")
def test_i6_powershell_parses():
    cmd = (
        "$t=$null;$e=$null;"
        "[void][System.Management.Automation.Language.Parser]::ParseFile('" + str(SCRIPT) + "',[ref]$t,[ref]$e);"
        "if($e){$e|%{$_.Message};exit 1}else{'ok'}"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
