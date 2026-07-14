#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenClaw 配置安全/一致性自检 (read-only)。

补充 repo_health.ps1 未覆盖的「安全 + 配置一致性」维度，用于防止 P0/P1
安全加固成果回退：端口漂移、敏感文件被 git 跟踪、模板缺失、密钥未轮换。

用法:
    python scripts/ops/config_doctor.py          # 人读输出
    python scripts/ops/config_doctor.py --json    # 机读 (CI/cron)

退出码: 0=全部通过  1=有 WARN  2=有 FAIL
只读：仅读取文件 + git 查询，不写任何文件、不改动环境。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 cp936，中文会乱码；统一切 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]

# 已被 .gitignore 覆盖、且绝不应被 git 跟踪的敏感文件
SENSITIVE = [
    "config/cluster.yaml",
    "config/device_registry.json",
    "config/device_aliases.json",
    "config/cluster_state.json",
    "config/notify_config.json",
]

# 2026-07-11 已知泄露到远端的旧 shared_secret 的 SHA-256（不内嵌明文，
# 仅用哈希比对以判断「是否仍未轮换」）。
_LEAKED_SECRET_SHA256 = "9b2a1c4108dad9996f15bcf15541ef3ae0bdc1879458374e128dc1f00bad75fc"

results: list[tuple[str, str, str]] = []  # (level, check, detail)


def add(level: str, check: str, detail: str) -> None:
    results.append((level, check, detail))


def _git(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True
        )
        return p.returncode, (p.stdout or "").strip()
    except FileNotFoundError:
        return 127, ""


def _read_kv_env(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 风格的 env 文件，忽略注释/空行。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def _yaml_scalar(path: Path, key: str) -> str | None:
    """从 yaml 里抓取顶层 `key: value`（简单正则，避免依赖 pyyaml）。"""
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            return m.group(1).strip().strip('"').strip("'")
    return None


def check_sensitive_not_tracked() -> None:
    """敏感文件应「有 gitignore 规则」且「未被 git 跟踪」。"""
    for rel in SENSITIVE:
        tracked = _git(["ls-files", "--error-unmatch", rel])[0] == 0
        ignored = _git(["check-ignore", "-q", rel])[0] == 0
        if tracked:
            add("FAIL", f"git:{rel}",
                "仍被 git 跟踪，会随提交泄露；执行 git rm --cached 取消跟踪")
        elif not ignored:
            add("WARN", f"git:{rel}", "未被 .gitignore 覆盖，可能误入库")
        else:
            add("OK", f"git:{rel}", "未跟踪且已忽略")


def check_examples_present() -> None:
    for rel in SENSITIVE:
        ex = ROOT / (rel + ".example")
        if ex.exists():
            add("OK", f"tmpl:{rel}", "模板存在")
        else:
            add("WARN", f"tmpl:{rel}", "缺 .example 模板，他人难以部署")


def check_port_consistency() -> None:
    launch = _read_kv_env(ROOT / "config" / "launch.env")
    launch_port = launch.get("OPENCLAW_PORT", "18080(default)")
    cluster_port = _yaml_scalar(ROOT / "config" / "cluster.yaml", "local_port")
    eff_launch = launch.get("OPENCLAW_PORT", "18080")
    if cluster_port is None:
        add("WARN", "port", f"cluster.yaml 无 local_port；launch.env={launch_port}")
    elif str(cluster_port) != str(eff_launch):
        add("FAIL", "port",
            f"端口不一致 launch.env={launch_port} vs cluster.yaml.local_port={cluster_port}")
    else:
        add("OK", "port", f"launch.env 与 cluster.yaml 一致 ({cluster_port})")

    # .env 不应再设置活跃的 OPENCLAW_PORT（单一权威来源=launch.env）
    dotenv = _read_kv_env(ROOT / ".env")
    if "OPENCLAW_PORT" in dotenv:
        add("WARN", "port:.env",
            f".env 仍设 OPENCLAW_PORT={dotenv['OPENCLAW_PORT']}（应注释，权威来源是 launch.env）")


def check_secret_hygiene() -> None:
    secret = _yaml_scalar(ROOT / "config" / "cluster.yaml", "shared_secret")
    if secret is None:
        add("WARN", "secret", "cluster.yaml 无 shared_secret（standalone 可忽略）")
        return
    if not secret:
        add("WARN", "secret", "shared_secret 为空")
        return
    if secret.startswith("CHANGE_ME"):
        add("FAIL", "secret", "shared_secret 仍是模板占位符，未设置真实值")
        return
    import hashlib
    if hashlib.sha256(secret.encode()).hexdigest() == _LEAKED_SECRET_SHA256:
        add("FAIL", "secret",
            "shared_secret 仍是 2026-07-11 已泄露的旧值，必须轮换")
    else:
        add("OK", "secret", "shared_secret 非占位/非已知泄露值")


def check_lock_freshness() -> None:
    req = ROOT / "requirements.txt"
    lock = ROOT / "requirements.lock"
    if not lock.exists():
        add("WARN", "lock", "缺 requirements.lock，依赖未锁定")
    elif req.exists() and lock.stat().st_mtime < req.stat().st_mtime:
        add("WARN", "lock", "requirements.txt 比 lock 新，建议重新 uv pip compile")
    else:
        add("OK", "lock", "requirements.lock 存在且不落后于 requirements.txt")


def main() -> int:
    check_sensitive_not_tracked()
    check_examples_present()
    check_port_consistency()
    check_secret_hygiene()
    check_lock_freshness()

    fails = sum(1 for lv, *_ in results if lv == "FAIL")
    warns = sum(1 for lv, *_ in results if lv == "WARN")
    verdict = 2 if fails else (1 if warns else 0)

    if "--json" in sys.argv:
        print(json.dumps({
            "verdict": verdict,
            "fails": fails, "warns": warns,
            "checks": [{"level": l, "check": c, "detail": d} for l, c, d in results],
        }, ensure_ascii=False, indent=2))
        return verdict

    print("=" * 52)
    print("  OpenClaw Config Doctor (security/consistency)")
    print("=" * 52)
    for lv, c, d in results:
        print(f"  [{lv:<4}] {c:<28} {d}")
    print("-" * 52)
    label = {0: "HEALTHY", 1: "NEEDS ATTENTION", 2: "ACTION REQUIRED"}[verdict]
    print(f"  >> verdict [{verdict}] {label}  (FAIL={fails} WARN={warns})")
    print("=" * 52)
    return verdict


if __name__ == "__main__":
    sys.exit(main())
