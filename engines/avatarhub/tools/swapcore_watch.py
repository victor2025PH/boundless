# -*- coding: utf-8 -*-
"""P14 换脸核发布哨兵：官方/社区两条线的「512 级换脸核」有新货就告警。

背景（2026-07-07 循证，写进 换脸画质提升三视角方案 P14）：
  - 官方线 insightface：作者明确「不会再向公众发布更高分辨率模型」；
    inswapper-512-live(原生512·算力1/10) 只在 Picsi.Ai App 内、严格授权分发。
    → 盯 GitHub Release：授权/分发政策若松动，大概率以新 Release/资产形式出现。
  - 社区线 ReSwapper(somanchiu)：512 还在 To-Do，HF 仓库有 GAN 判别器实验在推进。
    → 盯 HF 文件树：出现含 512 的权重/新 onnx 即值得 A/B（P3 加载器就绪，即插即用）。

设计对齐本仓哨兵哲学（stability_sentinel / fe_patrol）：
  - 人肉「记得隔段时间去看看」必然忘——机器每日查，出货才叫人。
  - 网络失败=SKIP 不告警（HF 在本网时常不可达，配 hf-mirror 兜底）。
  - 首跑建基线不告警；此后 diff 出新货 → alerts.raise_alert("swapcore:release")
    告警即更新基线（webhook/toast 叫一次就够，发现史留在 findings 供 /ops 回看）。

协议：stdout 人话行 + 末行 JSON（dev_probe/stability_report 同款）；rc 恒 0（异常才 1）。
产物：data/swapcore_watch.json（基线 + findings 历史，cap 50）。
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "swapcore_watch.json"
FINDINGS_MAX = 50

HF_REPO = "somanchiu/reswapper"
GH_REPO = "deepinsight/inswapper-512-live"
# HF 主站在本网不稳：官方端点(可被 HF_ENDPOINT 覆写)失败后自动落 hf-mirror
HF_ENDPOINTS = [os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/"),
                "https://hf-mirror.com"]


def _fetch_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "swapcore-watch/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _probe_hf():
    """HF 文件树（去重后的路径列表）；主站→镜像逐个试，全挂返回 None。"""
    last_err = ""
    for ep in dict.fromkeys(HF_ENDPOINTS):
        try:
            tree = _fetch_json(f"{ep}/api/models/{HF_REPO}/tree/main")
            files = sorted({it.get("path", "") for it in tree if it.get("path")})
            if files:
                return files, ep, ""
        except Exception as e:
            last_err = f"{ep}: {e}"
    return None, "", last_err


def _probe_gh():
    """GitHub 最新 Release tag（未授权限额 60次/h，日查绰绰有余）；失败返回 None。"""
    try:
        rels = _fetch_json(f"https://api.github.com/repos/{GH_REPO}/releases?per_page=3")
        if isinstance(rels, list) and rels:
            r0 = rels[0]
            return {"tag": r0.get("tag_name", ""), "name": r0.get("name", ""),
                    "published": r0.get("published_at", ""),
                    "assets": [a.get("name", "") for a in (r0.get("assets") or [])]}, ""
        return {"tag": "", "name": "", "published": "", "assets": []}, ""
    except Exception as e:
        return None, str(e)


def _interesting(path: str) -> bool:
    p = (path or "").lower()
    return p.endswith((".onnx", ".pth", ".safetensors")) or "512" in p


def _diff(prev: dict, cur: dict) -> dict:
    """纯函数：基线 vs 本次探测 → 新货清单。源探测失败(cur 对应键为 None)不参与 diff、
    不污染基线判断；「512」字样=高价值命中（官方级分辨率线索）。"""
    hits, notes = [], []
    if cur.get("hf_files") is not None:
        prev_files = set(prev.get("hf_files") or [])
        if prev_files:
            for f in cur["hf_files"]:
                if f not in prev_files and _interesting(f):
                    hits.append({"kind": "hf_new_file", "item": f,
                                 "high": "512" in f.lower()})
        else:
            notes.append("hf 基线首建(%d 文件)" % len(cur["hf_files"]))
    gh = cur.get("gh_release")
    if gh is not None:
        prev_tag = ((prev.get("gh_release") or {}).get("tag") or "")
        cur_tag = gh.get("tag") or ""
        if prev_tag and cur_tag and cur_tag != prev_tag:
            hits.append({"kind": "gh_new_release",
                         "item": "%s %s" % (cur_tag, gh.get("name", "")),
                         "high": True})
        elif cur_tag and not prev_tag:
            notes.append("gh 基线首建(%s)" % cur_tag)
    return {"hits": hits, "notes": notes}


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict):
    STATE.parent.mkdir(exist_ok=True)
    st["findings"] = (st.get("findings") or [])[-FINDINGS_MAX:]
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def _sync_alert(hits: list) -> bool:
    """出新货 → webhook/toast 叫一声（好消息用 warn 级，不与故障红色混淆）。"""
    try:
        sys.path.insert(0, str(ROOT))
        import alerts
        items = "; ".join(("[512]" if h.get("high") else "") + h.get("item", "")
                          for h in hits[:6])
        alerts.raise_alert("swapcore:release",
                           "换脸核有新发布，值得 A/B",
                           detail=f"{items}。加载器 P3 就绪：下权重→FACESWAP_MODEL 指过去即插即用；"
                                  f"详情 data/swapcore_watch.json / /api/ops/swapcore_watch",
                           level="warn", source="swapcore_watch")
        return True
    except Exception:
        return False


def run(alert: bool = False) -> dict:
    ts = time.time()
    st = _load_state()
    hf_files, hf_ep, hf_err = _probe_hf()
    gh_rel, gh_err = _probe_gh()
    cur = {"hf_files": hf_files, "gh_release": gh_rel}
    d = _diff(st, cur)

    net_fail = hf_files is None and gh_rel is None
    if hf_files is not None:
        st["hf_files"] = hf_files
        st["hf_endpoint"] = hf_ep
    if gh_rel is not None:
        st["gh_release"] = gh_rel
    if d["hits"]:
        st.setdefault("findings", []).append({"ts": ts, "hits": d["hits"]})
    st["last_check_ts"] = ts
    st["last_status"] = ("net_fail" if net_fail else ("hit" if d["hits"] else "clean"))
    st["last_err"] = "; ".join(x for x in (hf_err, gh_err) if x)
    _save_state(st)

    alerted = _sync_alert(d["hits"]) if (d["hits"] and alert) else False
    return {"ok": True, "status": st["last_status"], "hits": d["hits"], "notes": d["notes"],
            "hf_files_n": len(hf_files) if hf_files is not None else None,
            "hf_endpoint": hf_ep, "gh_tag": (gh_rel or {}).get("tag") if gh_rel else None,
            "alerted": alerted, "err": st["last_err"], "ts": ts,
            "findings_total": len(st.get("findings") or [])}


def _selftest() -> dict:
    """纯函数级自测（不出网）：diff 语义 6 用例。"""
    cs = []
    # 1) 首跑建基线：有货也不算命中
    r = _diff({}, {"hf_files": ["a/reswapper_512-1.onnx"], "gh_release": {"tag": "v1"}})
    cs.append(("首跑不告警只建基线", not r["hits"] and len(r["notes"]) == 2))
    base = {"hf_files": ["old.onnx"], "gh_release": {"tag": "v0.1.2"}}
    # 2) 新 512 权重=高价值命中
    r = _diff(base, {"hf_files": ["old.onnx", "reswapper_512-99.pth"], "gh_release": {"tag": "v0.1.2"}})
    cs.append(("新512权重高价值命中", len(r["hits"]) == 1 and r["hits"][0]["high"]))
    # 3) 新普通权重=普通命中
    r = _diff(base, {"hf_files": ["old.onnx", "gan_256.onnx"], "gh_release": {"tag": "v0.1.2"}})
    cs.append(("新onnx普通命中", len(r["hits"]) == 1 and not r["hits"][0]["high"]))
    # 4) 新 README 之类不值得叫
    r = _diff(base, {"hf_files": ["old.onnx", "README.md"], "gh_release": {"tag": "v0.1.2"}})
    cs.append(("杂物文件不命中", not r["hits"]))
    # 5) 官方线新 Release=高价值命中
    r = _diff(base, {"hf_files": ["old.onnx"], "gh_release": {"tag": "v0.2.0", "name": "public"}})
    cs.append(("gh新Release命中", len(r["hits"]) == 1 and r["hits"][0]["kind"] == "gh_new_release"))
    # 6) 源探测失败不参与 diff（不误报也不炸）
    r = _diff(base, {"hf_files": None, "gh_release": None})
    cs.append(("探测失败静默跳过", not r["hits"] and not r["notes"]))
    okn = sum(1 for _, c in cs if c)
    for label, c in cs:
        print("  [%s] %s" % ("OK" if c else "NG", label))
    return {"ok": okn == len(cs), "selftest": True, "pass": okn, "total": len(cs)}


def main():
    ap = argparse.ArgumentParser(description="换脸核发布哨兵(HF+GitHub 双源)")
    ap.add_argument("--alert", action="store_true", help="出新货→alerts.py 外发")
    ap.add_argument("--selftest", action="store_true", help="纯函数自测(不出网)")
    args = ap.parse_args()
    if args.selftest:
        res = _selftest()
    else:
        res = run(alert=args.alert)
        tag = res.get("gh_tag") or "?"
        if res["status"] == "net_fail":
            print("  [SKIP] 双源均不可达: %s" % res.get("err", ""))
        elif res["hits"]:
            for h in res["hits"]:
                print("  [HIT%s] %s %s" % ("·512" if h.get("high") else "", h["kind"], h["item"]))
        else:
            print("  [OK] 无新货  hf=%s文件(%s)  gh=%s" %
                  (res.get("hf_files_n"), res.get("hf_endpoint", ""), tag))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
