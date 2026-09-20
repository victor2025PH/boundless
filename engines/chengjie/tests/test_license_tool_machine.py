# -*- coding: utf-8 -*-
"""WP-8：license_tool CLI ``--machine`` 换机重签透传门禁（2026-08-17）。

校验侧语义（绑本机 active / 绑他机 invalid / ``*`` 站点通配 / fail-open）已由
test_machine_binding.py 钉死；这里只钉**厂商侧 CLI 真的把指纹签进 payload**——
换机 SOP 的厂商操作步骤依赖这条链存在。子进程级测试（走真 argparse）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(ENGINE / "scripts" / "license_tool.py"),
         *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, cwd=str(ENGINE))


def test_issue_signs_machine_claim(tmp_path):
    priv = tmp_path / "vendor.pem"
    r = _run("genkeys", "--out", str(priv))
    assert r.returncode == 0, r.stderr
    assert priv.exists()

    out = tmp_path / "license.key"
    r = _run("issue", "--priv", str(priv), "--sub", "测试客户",
             "--plan", "pro", "--lic-id", "mig-001",
             "--machine", "AAAA-BBBB-CCCC-DDDD", "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert '"machine": "AAAA-BBBB-CCCC-DDDD"' in r.stdout
    assert out.exists() and out.read_text(encoding="utf-8").strip()

    # 省略 --machine = 不绑机（存量语义不变，payload 不得出现 machine 键）
    r2 = _run("issue", "--priv", str(priv), "--sub", "测试客户", "--plan", "pro")
    assert r2.returncode == 0, r2.stderr
    assert '"machine"' not in r2.stdout
