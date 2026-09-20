#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传发布产物到下载服务器（scp，Windows 10+ 自带 OpenSSH 客户端即可用）。

上传内容（存在即传）：
  dist/AvatarHub-Setup-<ver>.exe      Windows 安装包
  dist/AvatarHub-<ver>.dmg            macOS 安装包（在 Mac 上构建后拷回或直接在 Mac 上传）
  dist/release_manifest.json          下载页清单（先跑 gen_download_manifest.py --base-url …）

用法（凭证由你提供，本脚本不保存密码，推荐密钥登录）：
  python upload_release.py --host 1.2.3.4 --user root --dest /var/www/dl/releases
  python upload_release.py --host dl.usdt2026.cc --user deploy --key C:\\Users\\user\\.ssh\\id_ed25519 --dest /var/www/dl/releases

上传后自动用 HTTPS HEAD 验证可达性（提供 --base-url 时）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # GBK 控制台防 ✓/✗ 炸 print
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="scp 上传安装包 + 下载清单到服务器")
    ap.add_argument("--host", required=True, help="服务器地址（IP 或域名）— 需要你提供")
    ap.add_argument("--user", required=True, help="SSH 用户名 — 需要你提供")
    ap.add_argument("--dest", required=True, help="服务器目标目录，如 /var/www/dl/releases")
    ap.add_argument("--key", default="", help="SSH 私钥路径（可选，推荐；不填则走默认密钥/交互密码）")
    ap.add_argument("--port", default="22", help="SSH 端口（默认 22）")
    ap.add_argument("--base-url", default="", help="上传后验证用的公网 URL，如 https://dl.usdt2026.cc/releases")
    args = ap.parse_args()

    files = sorted(DIST.glob("AvatarHub-Setup-*.exe")) + sorted(DIST.glob("AvatarHub-*.dmg"))
    manifest = DIST / "release_manifest.json"
    if manifest.exists():
        files.append(manifest)
    if not files:
        print("[error] dist/ 下没有可上传的产物。先构建安装包并跑 gen_download_manifest.py。")
        return 2

    print("[plan] 将上传：")
    for f in files:
        print(f"    {f.name}  ({f.stat().st_size / 1048576:.1f} MB)")
    print(f"    → {args.user}@{args.host}:{args.dest}/")

    scp = ["scp", "-P", args.port]
    if args.key:
        scp += ["-i", args.key]
    scp += [str(f) for f in files]
    scp.append(f"{args.user}@{args.host}:{args.dest}/")
    rc = subprocess.call(scp)
    if rc != 0:
        print(f"[error] scp 失败（返回 {rc}）。检查主机/凭证/目标目录是否存在。")
        return rc

    if args.base_url:
        base = args.base_url.rstrip("/")
        bad = 0
        for f in files:
            url = f"{base}/{f.name}"
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=20) as r:
                    remote = int(r.headers.get("Content-Length") or 0)
                ok = remote == f.stat().st_size
                print(f"    {'✓' if ok else '✗'} {url}  远端 {remote} 字节")
                bad += 0 if ok else 1
            except Exception as e:
                print(f"    ✗ {url} 不可达：{e}")
                bad += 1
        if bad:
            print(f"[warn] {bad} 个文件远端校验未通过，检查 Nginx 目录映射。")
            return 8
        print("[done] 上传并远端校验通过。下载页将自动展示新链接。")
    else:
        print("[done] 上传完成（未提供 --base-url，跳过远端校验）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
