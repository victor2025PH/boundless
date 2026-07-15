# -*- coding: utf-8 -*-
"""拓扑一致性巡检：cluster_map.json(安全面单一数据源) vs env_config.bat(hub 运行时路由)。

背景：2026-07-05 事故根因是"拓扑存了两份"——搬迁时 env_config 改了、harden_remote 内嵌
MAP 没改，鉴权体系静默断档 + 演习打退役机误报 critical + 自愈 1199 连败。harden_remote
已改读 cluster_map.json；本工具补住最后一个裂缝：cluster_map 与 env_config 之间的漂移。

判定：对 map 中每个带 env_key 的服务，取 env_config.bat 里该变量的生效默认值
（首个非 rem 的 `set "KEY=..."`，与批处理 `if not defined` 语义一致；机器级 env 覆盖
不在本工具视野，属人工显式操作），要求 http://<host>:<port> 出现在其值中（允许逗号
分隔的多副本池）。

告警：漂移 → alerts.raise_alert('topology_drift')；一致 → clear。自管生命周期，
不借 harden_remote 的退出码——漂移是"配置问题"，deploy 自愈治不了，混入退出码 1
会再造自愈死循环。

用法：python tools/topology_lint.py [--env-file 路径] [--map-file 路径] [--no-alert]
退出码：0=一致 / 1=漂移 / 2=自身故障(文件缺失等，不告警不误报)。
"""
import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_SET_RE = re.compile(r'^\s*(?:if\s+not\s+defined\s+\w+\s+)?set\s+"?(?P<k>[A-Za-z_][A-Za-z0-9_]*)=(?P<v>[^"\r\n]*)"?\s*$', re.I)


def parse_env_defaults(env_file: Path, keys):
    """取每个 key 在 env_config.bat 中的生效默认值（首个非 rem set，批处理首写胜出）。"""
    want = {k: None for k in keys}
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"读 {env_file} 失败: {e}")
    for line in text.splitlines():
        ls = line.strip()
        if not ls or ls.lower().startswith("rem") or ls.startswith("::"):
            continue
        m = _SET_RE.match(ls)
        if not m:
            continue
        k = m.group("k")
        if k in want and want[k] is None:
            want[k] = m.group("v").strip()
    return want


def check_vram_budget(cm):
    """校验 vram_budget：每机 Σ服务预算 ≤ 物理显存×threshold。返回 (drifts, oks)。

    背景：2026-07-05 巡检发现主机(5090)上 nemo+ditto+musetalk+fish 与 32B 翻译模型
    合计超过物理显存，ollama 把 31% 权重挤到 CPU（每句 3.6s，比全 GPU 慢 15 倍），
    且没有任何告警。本检查把"显存超编"变成上新/迁移服务时的红灯，而不是上线后猜。
    段落缺失=跳过（向后兼容）；预算是实测参考值，不是运行时抓取。
    """
    vb = cm.get("vram_budget")
    drifts, oks = [], []
    if not isinstance(vb, dict):
        return drifts, oks
    thr = float(vb.get("threshold", 0.92))
    for host, hc in sorted(vb.items()):
        if not isinstance(hc, dict) or "vram_gb" not in hc:
            continue
        total = float(hc["vram_gb"])
        svcs = hc.get("services") or {}
        used = sum(float(v) for v in svcs.values())
        cap = total * thr
        line = f"{host}({hc.get('gpu','?')}): 预算Σ{used:.1f}G / {total:.1f}G×{thr:.0%}={cap:.1f}G"
        if used > cap:
            drifts.append(f"显存超编 {line} · 服务={json.dumps(svcs, ensure_ascii=False)}"
                          f" | 修复：迁走/互斥/换小模型，让预算回到阈值内再上线")
        else:
            oks.append(f"显存预算 {line}")
    return drifts, oks


def check(map_file: Path, env_file: Path):
    """返回 (drifts, oks)；drifts 为文案列表。"""
    cm = json.loads(map_file.read_text(encoding="utf-8-sig"))
    expect = {}     # env_key -> (host, port, svc_name)
    for host, hc in (cm.get("hosts") or {}).items():
        for s in hc.get("svcs") or []:
            ek = (s.get("env_key") or "").strip()
            if ek:
                expect[ek] = (host, int(s["port"]), s.get("name", "?"))
    if not expect:
        raise RuntimeError("cluster_map.json 中没有任何带 env_key 的服务")
    got = parse_env_defaults(env_file, expect.keys())
    drifts, oks = [], []
    for ek, (host, port, name) in sorted(expect.items()):
        want_url = f"http://{host}:{port}"
        val = got.get(ek)
        if val is None:
            drifts.append(f"{ek}({name}): env_config 未定义，期望 {want_url}")
        elif want_url not in [u.strip() for u in val.split(",")]:
            drifts.append(f"{ek}({name}): env_config={val} ≠ cluster_map={want_url}")
        else:
            oks.append(f"{ek}({name}) = {want_url}")
    vb_drifts, vb_oks = check_vram_budget(cm)
    drifts.extend(vb_drifts)
    oks.extend(vb_oks)
    return drifts, oks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map-file", default=str(BASE / "cluster_map.json"))
    ap.add_argument("--env-file", default=str(BASE / "env_config.bat"))
    ap.add_argument("--no-alert", action="store_true", help="只打印不动告警状态(测试用)")
    a = ap.parse_args()
    try:
        drifts, oks = check(Path(a.map_file), Path(a.env_file))
    except Exception as e:
        print(f"拓扑lint自身故障(不判漂移): {e}")
        return 2

    for line in oks:
        print(f"一致: {line}")
    for line in drifts:
        print(f"漂移: {line}")

    if not a.no_alert:
        # 生产跑才落状态文件+动告警；--no-alert 的合成测试绝不污染真实状态
        try:
            import datetime
            status = {"ok": not drifts, "drifts": drifts, "oks": len(oks),
                      "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            (BASE / "logs").mkdir(exist_ok=True)
            (BASE / "logs" / "topology_lint.json").write_text(
                json.dumps(status, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"状态落盘异常(忽略): {e}")
        try:
            import alerts
            if drifts:
                alerts.raise_alert(
                    "topology_drift",
                    "集群拓扑漂移：cluster_map 与 env_config 路由不一致",
                    detail="; ".join(drifts) + " | 修复：对齐 cluster_map.json 与 env_config.bat 后重跑 verify",
                    level="error",
                    source="topology_lint",
                )
            else:
                alerts.clear_alert("topology_drift", note="拓扑一致性复验通过")
        except Exception as e:
            print(f"告警通道异常(忽略): {e}")

    if drifts:
        print(f"结论: 漂移 {len(drifts)} 处")
        return 1
    print(f"结论: 一致({len(oks)} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
