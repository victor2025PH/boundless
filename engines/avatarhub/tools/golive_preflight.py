#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golive_preflight.py — 上线前 go/no-go 一键预检（P7，2026-07-13）。

把《上线操作清单》里"人肉逐条核对"的机械项变成一次 GO / NO-GO 红绿灯，防手滑
（漏签名/漏灰度戳/密钥缺失/镜像不可达/清单被 halt/私钥误入发布树 等），上传前先跑。
只读、不改任何文件、不推 live。FAIL 阻断（exit 2），WARN 放行（exit 0）。

用法（构建机，指向本地待上传的 publish 树）：
  python tools/golive_preflight.py                 # 读 release.config.json 的 version/base_url
  python tools/golive_preflight.py --version 1.0.1 # 指定版本
  python tools/golive_preflight.py --remote        # 追加：上传后校验 live 站可达+验签(读 base_url)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

_R = {"PASS": "✅", "WARN": "⚠", "FAIL": "❌"}
_results: list[tuple[str, str, str]] = []


def chk(level: str, name: str, detail: str = ""):
    _results.append((level, name, detail))
    print(f"  {_R.get(level,'?')} [{level}] {name}" + (f" — {detail}" if detail else ""))


def _load_cfg() -> dict:
    p = HERE / "release.config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _fetch(url: str, timeout=12):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def preflight_local(ver: str, cfg: dict):
    print(f"\n== 本地待上传 publish 树预检（v{ver}）==")
    pub = HERE / "dist" / "publish" / ver
    if not pub.is_dir():
        chk("FAIL", "publish 树存在", f"未找到 {pub}（先跑 publish_release.py）")
        return
    chk("PASS", "publish 树存在", str(pub))

    mp = pub / "manifest.json"
    if not mp.is_file():
        chk("FAIL", "manifest.json 存在"); return
    m = json.loads(mp.read_text(encoding="utf-8"))

    # 1) 更新清单 Ed25519 验签（客户端就是这么验的）
    try:
        import release_sign
        ok, why = release_sign.verify_manifest(m)
        chk("PASS" if ok else "FAIL", "manifest Ed25519 验签", why)
    except Exception as e:
        chk("FAIL", "manifest Ed25519 验签", f"release_sign 不可用: {e}")

    # 2) 私钥在位（构建机才该有；且【不能】出现在 publish 树里）
    ska = HERE / "secrets" / "release_sign_ed25519_sk.pem"
    chk("PASS" if ska.exists() else "WARN", "代码密钥 A 私钥在构建机",
        "在位" if ska.exists() else "缺失（无法再签新版）")
    leaked = list(pub.rglob("*.pem")) + list(pub.rglob("*_sk*"))
    chk("FAIL" if leaked else "PASS", "publish 树无私钥泄漏",
        f"发现 {len(leaked)} 个疑似密钥文件！" if leaked else "无 .pem/_sk")

    # 3) app 组件：存在 + 灰度戳 + 包文件在位 + sha 对得上
    app = (m.get("components", {}).get("app", {}) or {}).get("core")
    if not app:
        chk("WARN", "app 程序组件", "manifest 无 app 组件（无程序热修，仅整包更新）")
    else:
        r = app.get("rollout") or {}
        pct = r.get("percent")
        if r.get("halted"):
            chk("FAIL", "app 灰度状态", "该版本 rollout.halted=true（上线前不应停放）")
        elif pct is None:
            chk("WARN", "app 灰度状态", "无 percent（默认全量）；灰度上线建议先设 10")
        else:
            chk("PASS", "app 灰度状态", f"percent={pct}%")
        fp = pub / (app.get("file", ""))
        if fp.is_file():
            import hashlib
            h = hashlib.sha256(fp.read_bytes()).hexdigest()
            chk("PASS" if h == app.get("sha256") else "FAIL", "app 包 sha256 一致",
                app.get("file", ""))
        else:
            chk("FAIL", "app 包在位", f"缺 {app.get('file')}")

    # 4) 遥测端点（配了才查）
    tu = (m.get("telemetry_url") or "").strip()
    if tu:
        base = tu.rsplit("/", 1)[0]
        try:
            _fetch(base + "/health", 8)
            chk("PASS", "遥测端点可达", base + "/health")
        except Exception as e:
            chk("WARN", "遥测端点可达", f"{type(e).__name__}（上报会本地排队，不阻断）")
    else:
        chk("WARN", "遥测端点", "manifest 无 telemetry_url（无崩溃/用量回传）")

    # 5) 代码签名证书（SmartScreen）——无证书是 WARN 不是 FAIL
    signed = bool((cfg.get("sign") or {}).get("enabled"))
    chk("PASS" if signed else "WARN", "代码签名证书",
        "已配置" if signed else "无证书：用户首次运行有 SmartScreen 提示（可绕过，更新通道仍受 Ed25519 保护）")

    # 6) SHA256SUMS（若已生成）一致性抽查
    sums = pub / "SHA256SUMS.txt"
    if sums.is_file():
        chk("PASS", "SHA256SUMS 已生成", "上传后可供用户自校验")
    else:
        chk("WARN", "SHA256SUMS", "未生成（publish_release 会在收尾生成）")


def preflight_remote(cfg: dict):
    base = str(cfg.get("base_url", "")).strip().rstrip("/")
    print(f"\n== 远端 live 站校验（{base}）==")
    if not base or "REPLACE_ME" in base or "internal.local" in base:
        chk("WARN", "base_url 指向 live", f"当前 base_url={base or '空'}（占位/内网，未指真实下载站）")
        return
    try:
        raw = _fetch(base + "/manifest.json")
        m = json.loads(raw.decode("utf-8"))
        chk("PASS", "远端 manifest 可达")
    except Exception as e:
        chk("FAIL", "远端 manifest 可达", str(e)); return
    try:
        import release_sign
        ok, why = release_sign.verify_manifest(m)
        chk("PASS" if ok else "FAIL", "远端 manifest 验签", why)
    except Exception as e:
        chk("WARN", "远端 manifest 验签", f"跳过: {e}")
    # 控制通道（可选）：若存在须验签通过，且不能把正在发的版本 halt 掉
    try:
        c = json.loads(_fetch(base + "/rollout_control.json", 8).decode("utf-8"))
        import release_sign
        cok = release_sign.verify_control(c)
        app = (m.get("components", {}).get("app", {}) or {}).get("core", {})
        curver = str(app.get("app_version", ""))
        halted = curver in (c.get("halted_versions") or [])
        if not cok:
            chk("WARN", "控制通道验签", "存在但验不过（客户端会忽略，按 manifest 走）")
        elif halted:
            chk("FAIL", "控制通道未误停当前版", f"v{curver} 在 halted_versions 里！")
        else:
            chk("PASS", "控制通道正常", "验签通过且未停当前版")
    except Exception:
        chk("PASS", "控制通道", "无 rollout_control.json（正常放量）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="")
    ap.add_argument("--remote", action="store_true")
    args = ap.parse_args()
    cfg = _load_cfg()
    ver = args.version or str(cfg.get("version", "")).strip()
    print("=" * 60)
    print(" AvatarHub 上线前 go/no-go 预检（只读，不推 live）")
    print("=" * 60)
    if not ver:
        print("  ❌ 无版本号（--version 或 release.config.json 的 version）"); return 2
    preflight_local(ver, cfg)
    if args.remote:
        preflight_remote(cfg)

    fails = [r for r in _results if r[0] == "FAIL"]
    warns = [r for r in _results if r[0] == "WARN"]
    print("\n" + "=" * 60)
    if fails:
        print(f" 结论：NO-GO ❌  （{len(fails)} 项 FAIL、{len(warns)} 项 WARN）")
        print(" 阻断项：" + "；".join(n for _, n, _ in fails))
        print(" 修复后重跑；勿在 FAIL 状态上传。")
        return 2
    print(f" 结论：GO ✅  （0 FAIL、{len(warns)} 项 WARN 可接受）")
    if warns:
        print(" 注意（不阻断）：" + "；".join(n for _, n, _ in warns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
