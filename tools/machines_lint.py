# -*- coding: utf-8 -*-
"""六机台账四方对账门禁（2026-08-05 v2 命名改版随行）。

对账面：
  A. machines.json ↔ engines/avatarhub cluster_map.json（角色/显存权威源）
       IP 全集一致、GPU 型号一致、hub 地址一致——拦「台账机器与集群账本漂移」（.173 曾整机失踪一个月）。
  B. machines.json 自体：id/别名唯一（含退役别名）、必填字段齐、watch_ports ⊆ services 端口。
  C. deploy/ssh_config.boundless（生成产物）↔ machines.json：Host 全集、每别名 HostName/User 正确
       ——拦「改了台账忘了重跑 render」。
  D. 本机 ~/.ssh/config：标记块内容 == 生成产物；集群别名不得在标记块外重复定义
       ——拦「影子块遮蔽」（声备机曾被 7 月旧块抢先匹配 hub176）。
  E. （--remote）六机实测：远端标记块哈希 == 本地——拦「网格下发漏机」。
  F. 壁纸产物存在且新于台账（缺失=警告；--strict 升红）——拦「改名后壁纸还挂旧名」。

用法：
  python tools/machines_lint.py            # A-D + F(警告)
  python tools/machines_lint.py --remote   # 加 E（需 SSH 网格在线）
  python tools/machines_lint.py --strict   # F 升级为硬红
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACHINES = ROOT / "deploy" / "machines.json"
RENDERED = ROOT / "deploy" / "ssh_config.boundless"
# 集群角色权威源（avatarhub 仓）。两个已知落位：开发机 D:\projects\模仿音色 与镜像 C:\模仿音色。
CLUSTER_MAP_CANDIDATES = [
    Path(r"D:\projects\模仿音色\cluster_map.json"),
    Path(r"C:\模仿音色\cluster_map.json"),
    ROOT / "engines" / "avatarhub" / "cluster_map.json",
]

MARKER_BEGIN = "# === boundless cluster BEGIN ==="
MARKER_END = "# === boundless cluster END ==="

RED = []
WARN = []


def red(msg: str) -> None:
    RED.append(msg)


def warn(msg: str) -> None:
    WARN.append(msg)


def load_machines() -> dict:
    return json.loads(MACHINES.read_text(encoding="utf-8"))


def find_cluster_map() -> Path | None:
    for p in CLUSTER_MAP_CANDIDATES:
        if p.exists():
            return p
    return None


def gpu_model(s: str) -> str:
    m = re.search(r"(RTX\s*\d{4})", s or "")
    return m.group(1).replace(" ", "") if m else (s or "").strip()


def check_a_cluster_map(data: dict) -> None:
    cm_path = find_cluster_map()
    if not cm_path:
        warn("A: 找不到 avatarhub cluster_map.json（离线环境跳过角色权威对账）")
        return
    cm = json.loads(cm_path.read_text(encoding="utf-8"))
    cm_ips = set(cm.get("vram_budget", {}).keys()) - {"threshold"}
    cm_ips = {ip for ip in cm_ips if re.match(r"^\d+\.", ip)}
    mj_ips = {m["ip"] for m in data["machines"]}
    if cm_ips != mj_ips:
        red(f"A: IP 全集漂移 machines.json={sorted(mj_ips)} vs cluster_map={sorted(cm_ips)}（源 {cm_path}）")
    hub_cm = cm.get("hub", {})
    hub_mj = data.get("hub", {})
    if hub_cm.get("ip") != hub_mj.get("ip") or hub_cm.get("port") != hub_mj.get("port"):
        red(f"A: hub 漂移 machines.json={hub_mj} vs cluster_map={hub_cm}")
    for m in data["machines"]:
        node = cm.get("vram_budget", {}).get(m["ip"])
        if not node:
            continue
        if gpu_model(node.get("gpu", "")) != gpu_model(m.get("gpu", "")):
            red(f"A: {m['id']}({m['ip']}) GPU 漂移 台账={m.get('gpu')} vs cluster_map={node.get('gpu')}")


def all_aliases(m: dict) -> list[str]:
    return list(m.get("ssh", [])) + list(m.get("ssh_deprecated", []))


def check_b_self(data: dict) -> None:
    seen_ids: set[str] = set()
    seen_alias: dict[str, str] = {}
    for m in data["machines"]:
        mid = m["id"]
        if mid in seen_ids:
            red(f"B: id 重复 {mid}")
        seen_ids.add(mid)
        for field in ("zh", "ip", "user", "key", "gpu", "resolution", "accent", "role_now", "wallpaper"):
            if not m.get(field):
                red(f"B: {mid} 缺必填字段 {field}")
        if not re.match(r"^\d{3,4}x\d{3,4}$", str(m.get("resolution", ""))):
            red(f"B: {mid} resolution 非法: {m.get('resolution')}")
        if not re.match(r"^#[0-9A-Fa-f]{6}$", str(m.get("accent", ""))):
            red(f"B: {mid} accent 非法: {m.get('accent')}")
        svc_ports = set()
        for s in m.get("services", []):
            mm = re.search(r":(\d+)$", s)
            if mm:
                svc_ports.add(int(mm.group(1)))
        for p in m.get("watch_ports", []):
            if p not in svc_ports:
                red(f"B: {mid} watch_port {p} 不在 services 声明里")
        for a in all_aliases(m):
            if a in seen_alias:
                red(f"B: 别名 {a} 同时挂在 {seen_alias[a]} 和 {mid}")
            seen_alias[a] = mid


def parse_ssh_hosts(text: str) -> dict[str, dict[str, str]]:
    """返回 alias -> {hostname, user}（逐 Host 块解析，多别名行拆开）。"""
    out: dict[str, dict[str, str]] = {}
    cur: list[str] = []
    props: dict[str, str] = {}

    def flush():
        for a in cur:
            out[a] = dict(props)

    for line in text.splitlines():
        hm = re.match(r"^\s*Host\s+(.+)$", line)
        if hm:
            flush()
            cur = hm.group(1).split()
            props = {}
            continue
        pm = re.match(r"^\s*(HostName|User)\s+(\S+)", line, re.I)
        if pm and cur:
            props[pm.group(1).lower()] = pm.group(2)
    flush()
    return out


def check_c_rendered(data: dict) -> None:
    if not RENDERED.exists():
        red("C: deploy/ssh_config.boundless 不存在（跑 tools/render_ssh_config.ps1）")
        return
    got = parse_ssh_hosts(RENDERED.read_text(encoding="utf-8"))
    expect: dict[str, tuple[str, str]] = {}
    for m in data["machines"]:
        for a in all_aliases(m):
            expect[a] = (m["ip"], m["user"])
    missing = set(expect) - set(got)
    if missing:
        red(f"C: 生成产物缺别名 {sorted(missing)}（台账改了没重跑 render）")
    for a, (ip, user) in expect.items():
        if a in got:
            if got[a].get("hostname") != ip:
                red(f"C: 别名 {a} HostName={got[a].get('hostname')} 应为 {ip}")
            if got[a].get("user") != user:
                red(f"C: 别名 {a} User={got[a].get('user')} 应为 {user}")


def local_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def marker_block(text: str) -> str | None:
    i = text.find(MARKER_BEGIN)
    j = text.find(MARKER_END)
    if i < 0 or j < 0 or j <= i:
        return None
    return text[i : j + len(MARKER_END)]


def check_d_local(data: dict) -> None:
    cfg = local_config_path()
    if not cfg.exists():
        red("D: 本机 ~/.ssh/config 不存在")
        return
    raw = cfg.read_text(encoding="utf-8-sig", errors="replace")
    block = marker_block(raw)
    if block is None:
        red("D: 本机 config 无 boundless 标记块（跑 remote_merge_ssh_config.ps1）")
        return
    rendered = RENDERED.read_text(encoding="utf-8-sig", errors="replace") if RENDERED.exists() else ""
    if rendered and rendered.strip() not in block:
        red("D: 本机标记块与生成产物不一致（重新 merge）")
    outside = raw.replace(block, "")
    cluster_aliases = {a.lower() for m in data["machines"] for a in all_aliases(m)}
    cluster_aliases |= {"vps-bd2026", "bd2026"}
    shadow = []
    for line in outside.splitlines():
        hm = re.match(r"^\s*Host\s+(.+)$", line)
        if hm:
            for a in hm.group(1).split():
                if a.lower() in cluster_aliases:
                    shadow.append(a)
    if shadow:
        red(f"D: 集群别名在标记块外仍有影子定义 {shadow}（跑 tools/cleanup_legacy_ssh_hosts.ps1）")


def block_hash(text: str) -> str:
    block = marker_block(text) or ""
    norm = "\n".join(l.rstrip() for l in block.replace("\r\n", "\n").splitlines()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def check_e_remote(data: dict) -> None:
    cfg = local_config_path()
    local_hash = block_hash(cfg.read_text(encoding="utf-8-sig", errors="replace")) if cfg.exists() else ""
    ps = (
        "$c=Join-Path $env:USERPROFILE '.ssh\\config'; "
        "if (Test-Path $c) { Get-Content $c -Raw -Encoding UTF8 } else { '' }"
    )
    import base64

    b64 = base64.b64encode(ps.encode("utf-16-le")).decode()
    my_ip = None
    for m in data["machines"]:
        if m["id"] == data.get("hub", {}).get("id"):
            my_ip = m["ip"]
    for m in data["machines"]:
        if m["ip"] == my_ip:
            continue
        alias = m["ssh"][0]
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias,
                 f"powershell -NoProfile -EncodedCommand {b64}"],
                capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
            )
        except Exception as e:  # noqa: BLE001
            red(f"E: {m['id']}({alias}) SSH 失败: {e}")
            continue
        if r.returncode != 0:
            red(f"E: {m['id']}({alias}) SSH 退出码 {r.returncode}: {(r.stderr or '').strip()[:120]}")
            continue
        h = block_hash(r.stdout or "")
        if h != local_hash:
            red(f"E: {m['id']}({alias}) 标记块哈希 {h} != 本地 {local_hash}（网格下发漏机/漂移）")


def check_f_wallpapers(data: dict, strict: bool) -> None:
    ts = MACHINES.stat().st_mtime
    for m in data["machines"]:
        wp = ROOT / str(m.get("wallpaper", "")).replace("/", "\\")
        if not wp.exists():
            (red if strict else warn)(f"F: {m['id']} 壁纸缺失 {wp.name}（跑 make_machine_wallpapers.py）")
        elif wp.stat().st_mtime < ts:
            (red if strict else warn)(f"F: {m['id']} 壁纸旧于台账（重跑 make_machine_wallpapers.py）")


def check_g_mirror() -> None:
    """boundless deploy/cluster_map.json 只是权威版的镜像（供本仓部署脚本/文档引用）。
    漂移=有人只改了一边——警告级提醒重新 Copy-Item。"""
    mirror = ROOT / "deploy" / "cluster_map.json"
    auth = find_cluster_map()
    if not auth or not mirror.exists():
        return
    if mirror.read_text(encoding="utf-8") != auth.read_text(encoding="utf-8"):
        warn(f"G: deploy/cluster_map.json 镜像与权威版不一致（Copy-Item {auth} -> deploy\\）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", action="store_true", help="加测六机远端标记块一致性")
    ap.add_argument("--strict", action="store_true", help="壁纸产物检查升为硬红")
    args = ap.parse_args()

    data = load_machines()
    check_a_cluster_map(data)
    check_b_self(data)
    check_c_rendered(data)
    check_d_local(data)
    if args.remote:
        check_e_remote(data)
    check_f_wallpapers(data, args.strict)
    check_g_mirror()

    for w in WARN:
        print(f"[warn] {w}")
    if RED:
        for r in RED:
            print(f"[RED]  {r}")
        print(f"machines_lint: {len(RED)} red, {len(WARN)} warn")
        return 1
    print(f"machines_lint: OK ({len(WARN)} warn)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
