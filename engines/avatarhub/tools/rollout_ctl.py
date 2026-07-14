#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rollout_ctl.py — 灰度放量 / 紧急停放 控制台（发布方用，2026-07-13 P2）。

对 manifest 的 app 组件设置 rollout 策略并【自动重签】（改了 manifest 必须重签，否则
客户端验签会判篡改）。改完把 manifest 传到下载站即生效——存量客户端下次检查更新时
按机器稳定分桶决定是否落这一版。

用法（在项目根，指向要改的 manifest；可多份，通常本地 dist + 镜像 publish 各一份）：
  python tools/rollout_ctl.py status  dist/manifest.json
  python tools/rollout_ctl.py set 10  dist/manifest.json dist/publish/1.0.1/manifest.json   # 放量 10%
  python tools/rollout_ctl.py set 100 dist/manifest.json ...     # 全量
  python tools/rollout_ctl.py halt    dist/manifest.json ...     # 紧急停放（坏版本刹车）
  python tools/rollout_ctl.py resume  dist/manifest.json ...     # 解除停放（回到上次 percent）

停放≠回滚：halt 只阻止"尚未升级的机器"继续升级；已升级机器的回退由客户端 probation/
quarantine 自愈或用户 --app-revert 处理。真要全网撤版，请回滚 manifest 的 app sha 到旧包。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def _load(mp: Path) -> dict:
    return json.loads(mp.read_text(encoding="utf-8"))


def _app(m: dict) -> dict | None:
    return (m.get("components", {}).get("app", {}) or {}).get("core")


def _sign(mp: Path):
    try:
        import release_sign
        fp = release_sign.sign_manifest_file(mp)
        print(f"  [sign] {mp.name} 已重签（公钥指纹 {fp}）")
    except Exception as e:
        print(f"  [sign] 未签名（{e}）——发布前务必补签")


def cmd_status(paths: list[Path]):
    for mp in paths:
        m = _load(mp)
        a = _app(m)
        if not a:
            print(f"{mp}: 无 app 组件")
            continue
        r = a.get("rollout") or {}
        print(f"{mp.name}: app v{a.get('app_version')} sha={a.get('sha256','')[:12]} "
              f"rollout={{percent:{r.get('percent','-')}, halted:{bool(r.get('halted'))}}} signed={bool(m.get('sig'))}")


def _apply(paths: list[Path], mutate):
    for mp in paths:
        m = _load(mp)
        a = _app(m)
        if not a:
            print(f"  [skip] {mp} 无 app 组件")
            continue
        r = dict(a.get("rollout") or {})
        mutate(r)
        a["rollout"] = r
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [set] {mp.name}: rollout → {r}")
        _sign(mp)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "set":
        pct = int(sys.argv[2])
        paths = [Path(p) for p in sys.argv[3:]]
        _apply(paths, lambda r: (r.update({"percent": max(0, min(100, pct)), "halted": False})))
    elif cmd == "halt":
        paths = [Path(p) for p in sys.argv[2:]]
        _apply(paths, lambda r: r.update({"halted": True}))
    elif cmd == "resume":
        paths = [Path(p) for p in sys.argv[2:]]
        _apply(paths, lambda r: r.update({"halted": False}))
    elif cmd == "status":
        cmd_status([Path(p) for p in sys.argv[2:]])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
