#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载清单生成器：扫描 dist/ 里的安装包，生成 dist/release_manifest.json。

download.html 通过 /api/release/manifest 读取该文件渲染下载按钮（单一真相）。
识别规则：
  Windows: dist/AvatarHub-Setup-<ver>.exe   （Inno Setup 产物）
  macOS:   dist/AvatarHub-<ver>.dmg          （installer/build_mac.sh 产物）

用法：
  python gen_download_manifest.py --base-url https://dl.usdt2026.cc/releases
  python gen_download_manifest.py                      # 无 base-url 则 url 留空（仅登记 sha256/大小）

上传后清单里的 url = <base-url>/<文件名>，请保证服务器目录与之对应。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

WIN_RE = re.compile(r"^AvatarHub-Setup-([\d.]+)\.exe$", re.I)
MAC_RE = re.compile(r"^AvatarHub-([\d.]+)\.dmg$", re.I)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _size_human(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


def _latest(matches: list[tuple[tuple[int, ...], Path, str]]):
    """按版本号取最新一个：[(版本元组, 路径, 版本串)]"""
    return max(matches, key=lambda t: t[0]) if matches else None


def _second_latest(matches):
    """次新版本（回滚目标）：不足两个版本时返回 None。"""
    if len(matches) < 2:
        return None
    return sorted(matches, key=lambda t: t[0])[-2]


def collect(base_url: str, rollout: int = 100) -> dict:
    wins, macs = [], []
    if DIST.is_dir():
        for p in DIST.iterdir():
            if not p.is_file():
                continue
            m = WIN_RE.match(p.name)
            if m:
                ver = m.group(1)
                wins.append((tuple(int(x) for x in ver.split(".")), p, ver))
                continue
            m = MAC_RE.match(p.name)
            if m:
                ver = m.group(1)
                macs.append((tuple(int(x) for x in ver.split(".")), p, ver))

    def entry(hit, os_label: str, ico: str, note: str) -> dict:
        if not hit:
            return {"os": os_label, "ico": ico, "ver": "-", "size": note,
                    "url": "", "sha256": "", "filename": "", "ready": False}
        _, p, ver = hit
        url = f"{base_url.rstrip('/')}/{p.name}" if base_url else ""
        print(f"[found] {p.name}  ({_size_human(p.stat().st_size)})  计算 SHA-256…")
        return {"os": os_label, "ico": ico, "ver": ver,
                "size": f"{_size_human(p.stat().st_size)}（{note}）",
                "bytes": p.stat().st_size,   # 原始字节数：产品内自更新的下载进度分母
                "url": url, "sha256": _sha256(p), "filename": p.name, "ready": True}

    win_main = entry(_latest(wins), "Windows 10/11 (x64)", "🖥️", "薄核心")
    # 回滚锚点（1.0.9 起）：清单常驻「次新版」条目。新版翻车时客户端从维护入口
    # 一键降回（同一签名/同一 sha 校验流水线）；服务器目录按版本存文件、永不覆盖旧版。
    prev_hit = _second_latest(wins)
    if prev_hit:
        win_main["prev"] = {k: v for k, v in
                            entry(prev_hit, "Windows 10/11 (x64)", "🖥️", "回滚版").items()
                            if k in ("ver", "url", "sha256", "bytes", "size", "filename")}
    return {
        # 官方客服入口（2026-07-13 品牌统一）：交流群 hykjz；频道 hykj7；官网 ai26.sbs
        "telegram": "https://t.me/hykjz",
        # 灰度百分比（1.0.9 起）：客户端按机器指纹稳定分桶，桶号 < rollout 才提示更新。
        # 新版先 rollout=20 放量观察，稳了再改 100 重签重传清单（不用重传安装包）。
        "rollout": max(0, min(100, int(rollout))),
        "builds": [
            win_main,
            entry(_latest(macs), "macOS 12+ (Apple Silicon / Intel)", "🍎", "轻量控制台"),
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 dist/release_manifest.json")
    ap.add_argument("--base-url", default="", help="安装包所在服务器目录 URL，例如 https://dl.usdt2026.cc/releases")
    ap.add_argument("--rollout", type=int, default=100,
                    help="灰度放量百分比 0-100（默认 100=全量）；客户端按指纹稳定分桶")
    args = ap.parse_args()

    manifest = collect(args.base_url, rollout=args.rollout)

    # Ed25519 签名（1.0.8 起）：产品内自更新【拒绝无签名清单】——发布机必须签。
    # 私钥缺失只警告不阻断（外协机也能生成草稿清单），但正式发布必须在有私钥的机器上跑。
    try:
        import release_sign
        release_sign.sign_manifest_dict(manifest)
        print(f"[sign] release_manifest 已签名（公钥指纹 {manifest['sig']['key_fp']}）")
    except Exception as e:
        print(f"[warn] 清单未签名（{e}）——产品内自更新会拒绝该清单，仅下载页可用！")

    DIST.mkdir(exist_ok=True)
    out = DIST / "release_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    ready = [b for b in manifest["builds"] if b["ready"]]
    print(f"[done] {out}  （{len(ready)}/2 个平台就绪）")
    if not args.base_url:
        print("[warn] 未提供 --base-url，url 字段为空：下载页会显示『下载链接待发布』。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
