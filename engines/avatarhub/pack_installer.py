# -*- coding: utf-8 -*-
r"""
pack_installer.py — 分发包下载/安装引擎（首启向导的内核，纯标准库，零新依赖）

职责（与 build_packs.py 配对）：读 manifest.json → 验机选档 → 按档位下载所需组件
（断点续传 + 多线程 + sha256 校验）→ 解压 → 对 conda-env 组件跑 conda-unpack →
写 config.json 的 conda_python 映射 → 用户机无需装 conda 即可启动。

设计要点：
  - UI 无关：本模块只做逻辑，GUI（launcher_qt）通过 progress 回调驱动，CLI 亦可独立运行。
  - 路径解析：环境解压到  <安装目录>\runtime\envs\<env>，模型解压回项目根（保留相对路径）。
    app_config.conda_python(env) 支持 config.json["conda_python"][env] 覆盖，于是免 conda。
  - 来源：manifest.base_url 为 http(s) 时联网下载；为空时以 manifest 所在目录为本地源（便于自测）。

CLI：
  python pack_installer.py --manifest dist/manifest.json --gpu          # 验机 + 推荐档位
  python pack_installer.py --manifest dist/manifest.json --status       # 各组件安装状态
  python pack_installer.py --manifest <path|url> --edition standard --plan
  python pack_installer.py --manifest <path|url> --edition standard --install
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

import app_config

try:
    import telemetry          # 匿名健康回执（best-effort，缺失/出错都不影响安装）
except Exception:
    telemetry = None

BASE = app_config.BASE
ENVS_ROOT = BASE / "runtime" / "envs"      # 环境解压目标（免 conda）
STORE_DIR = BASE / "runtime" / "_store"    # 共享基座内容寻址库（torch/CUDA 大库只存一份，硬链进各环境）
CACHE_DIR = BASE / "_pack_cache"           # 下载缓存（tar.gz）
CONFIG_PATH = BASE / "config.json"
INSTALLED_STATE = BASE / "runtime" / "installed.json"  # 已装组件清单（版本/ sha 追踪，供增量更新）
DEFAULT_THREADS = 16                        # 与你们 16 线程下载器一致


# ══════════════════════════════════════════════════════════════════
#  manifest / 通用
# ══════════════════════════════════════════════════════════════════
_CONTROL_CACHE = {"key": None, "data": None, "ts": 0.0}


def _fetch_rollout_control(roots) -> dict:
    """从候选源根（base_url + 镜像）依次拉 rollout_control.json（密钥 B 签名）并验签，
    取第一个验签通过的；全失败/无 → {}。进程内缓存 60s。控制通道是叠加在 manifest 上的
    运行时覆盖层（halt 某版本 / 临时调百分比），不改代码完整性（那是密钥 A 的 manifest）。"""
    if isinstance(roots, str):
        roots = [roots]
    roots = [r for r in (roots or []) if r]
    now = time.time()
    key = "|".join(roots)
    if _CONTROL_CACHE["key"] == key and (now - _CONTROL_CACHE["ts"] < 60):
        return _CONTROL_CACHE["data"] or {}
    data = {}
    for root in roots:
        try:
            url = _resolve_src(root, "rollout_control.json")
            if _is_url(url):
                with urllib.request.urlopen(url, timeout=10) as r:
                    raw = json.loads(r.read().decode("utf-8"))
            else:
                p = Path(url)
                raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            if raw:
                import release_sign
                if release_sign.verify_control(raw):
                    data = raw
                    break     # 取第一个验签通过的（伪造/无签名的忽略）
        except Exception:
            continue
    _CONTROL_CACHE.update({"key": key, "data": data, "ts": now})
    return data


# 当前更新检查生效的候选源根列表（供 _rollout_eligible 取控制通道；install 链设置）
_ACTIVE_SRC_ROOT = None


def load_manifest(source: str) -> tuple[dict, str]:
    """读取 manifest；返回 (manifest, 源根)。源根用于拼接组件相对路径：
    http(s) 用 base_url 或 manifest 的 URL 目录；本地用 manifest 所在目录。
    2026-07-13 P1：读入即用【钉死公钥】验签——被篡改/降级的 manifest 直接拒绝，
    杜绝"CDN 被黑推恶意组件"（sha256 只保完整性，签名才保真实性）。"""
    if _is_url(source):
        with urllib.request.urlopen(source, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        root = source.rsplit("/", 1)[0]
    else:
        p = Path(source).resolve()
        data = json.loads(p.read_text(encoding="utf-8"))
        root = str(p.parent)
    _verify_manifest_sig(data)
    base_url = (data.get("base_url") or "").strip()
    src_root = base_url if base_url else root
    return data, src_root.rstrip("/")


def _verify_manifest_sig(manifest: dict):
    """机会性验签闸：release_sign 判拒则抛错阻断安装/更新；缺模块=放行（存量兼容）。"""
    try:
        import release_sign
    except Exception:
        return
    try:
        ok, why = release_sign.verify_manifest(manifest)
    except Exception:
        return
    if not ok:
        raise RuntimeError(f"发布清单验签未通过，已阻断：{why}")


def _read_json(src: str) -> dict:
    """读 JSON：本地路径或 http(s) URL。"""
    if _is_url(src):
        with urllib.request.urlopen(src, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    return json.loads(Path(src).read_text(encoding="utf-8"))


def resolve_channel_source(source: str, channel: str) -> str:
    """把"入口"解析成目标 manifest 地址，支持发布通道（stable/beta…）：
    - source 本身是 channels.json（含 "channels"）→ 取该通道的 manifest_url；
    - source 是普通 manifest 且带 channels_url → 拉 channels.json 再取该通道；
    - 否则原样返回（无通道概念，直连该 manifest）。找不到通道时回退原 source。"""
    if not channel:
        return source
    try:
        data = _read_json(source)
    except Exception:
        return source
    chans = data.get("channels")
    if not chans and data.get("channels_url"):
        try:
            chans = _read_json(data["channels_url"]).get("channels")
        except Exception:
            chans = None
    if chans and channel in chans and chans[channel].get("manifest_url"):
        return chans[channel]["manifest_url"]
    return source


def list_rollback_points(manifest: dict) -> list[dict]:
    """从 manifest.versions_url 拉版本链，返回【早于当前版】的可回滚节点：
    [{version, date, manifest_url}]，按发布时间倒序（最近的旧版在前）。无链/无 url 返回 []。"""
    vurl = manifest.get("versions_url")
    if not vurl:
        return []
    try:
        chain = _read_json(vurl).get("versions", [])
    except Exception:
        return []
    cur = manifest.get("version", "")
    pts = [{"version": e.get("version"), "date": e.get("date", ""),
            "manifest_url": e.get("manifest_url", "")}
           for e in chain if e.get("version") != cur and e.get("manifest_url")]
    pts.sort(key=lambda x: x["date"], reverse=True)
    return pts


def rollback_to(target_manifest_url: str, threads=DEFAULT_THREADS, log=print,
                on_overall=None, keep_cache=False, dedup=True) -> list:
    """回滚/降级到指定版本：拉该版 manifest（其 base_url 指向该版【自有】packs 目录），
    复用增量机制——仅重装"当前已装但与目标版 sha 不同"的组件，从目标版源取回旧包并校验。
    依赖可复现打包：目标版各包 sha 稳定、旧版 packs 仍在其版本目录。返回失败清单。"""
    old_m, old_root = load_manifest(target_manifest_url)
    log(f"回滚目标：v{old_m.get('version')}　源：{old_root}")
    srcs = resolve_sources(old_m, old_root)
    if len(srcs) > 1:
        srcs = order_sources(srcs, log=log)
    comps = check_updates(old_m)          # 已装但与目标版不同 → 需回退的组件
    if not comps:
        log("当前已与目标版本一致，无需回滚。")
        return []
    log(f"需回退 {len(comps)} 个组件，合计 {_human(sum(c.get('size_bytes',0) for _, c in comps))}")
    rec = telemetry.Recorder() if telemetry else None
    failed = install_components(old_m, comps, srcs, threads=threads,
                               log=log, on_overall=on_overall, keep_cache=keep_cache, recorder=rec)
    if dedup and any(cid.startswith("env:") for cid, _ in comps):
        dedup_runtime_envs(log=log)
    _emit_receipt(rec, "rollback", "", old_m, srcs, log)
    return failed


def resolve_sources(manifest: dict, primary_root: str) -> list[str]:
    """汇总可用下载源根：主源(primary_root) + manifest.mirrors + 环境变量 AVATARHUB_MIRRORS。
    去重保序；本地源（非 URL）不引入镜像（镜像仅对 http(s) 有意义）。"""
    roots = [primary_root]
    if _is_url(primary_root):
        for m in manifest.get("mirrors", []) or []:
            m = (m or "").strip().rstrip("/")
            if m:
                roots.append(m)
        env = os.environ.get("AVATARHUB_MIRRORS", "")
        for m in env.replace(";", ",").split(","):
            m = m.strip().rstrip("/")
            if m and _is_url(m):
                roots.append(m)
    seen, out = set(), []
    for r in roots:
        if r and r not in seen:
            seen.add(r); out.append(r)
    return out


def probe_source(root: str, sentinel: str = "manifest.json", timeout: float = 5.0) -> float:
    """探测一个源根的可达性与延迟：对 root/sentinel 发 HEAD，返回秒数；不可达返回 inf。
    本地源（非 URL）若存在返回 0（最优），否则 inf。"""
    if not _is_url(root):
        return 0.0 if Path(root).exists() else float("inf")
    url = f"{root}/{sentinel}"
    try:
        t0 = time.time()
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if 200 <= getattr(r, "status", 200) < 400:
                return time.time() - t0
    except Exception:
        pass
    return float("inf")


def order_sources(roots: list[str], log=None, timeout: float = 5.0) -> list[str]:
    """按延迟为多个源择优排序：可达者按 HEAD 延迟升序，不可达者保留在末尾作 failover 兜底。
    单源或本地直接原样返回（不浪费一次探测）。"""
    url_roots = [r for r in roots if _is_url(r)]
    if len(roots) <= 1 or not url_roots:
        return roots
    scored = [(probe_source(r, timeout=timeout), i, r) for i, r in enumerate(roots)]
    scored.sort(key=lambda x: (x[0], x[1]))   # 延迟同则保持原序（主源优先）
    if log:
        for lat, _, r in scored:
            tag = ("%.0fms" % (lat * 1000)) if lat != float("inf") else "不可达"
            log(f"   源 {r} → {tag}")
    return [r for _, _, r in scored]


def _is_url(s: str) -> bool:
    return urlsplit(s).scheme in ("http", "https")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def _sha256(path: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_components(manifest: dict):
    """遍历 (cid, comp)；cid 形如 shared:torch-cuda / env:cosytts / model:gfpgan / app:core。
    shared 在前（基座须先于依赖它的环境安装）；app 在最后（代码切换放批次末尾，
    模型/环境失败时不动代码，代码失败也不影响已装数据组件）。"""
    comps = manifest.get("components", {})
    for sid, c in comps.get("shared", {}).items():
        yield f"shared:{sid}", c
    for env, c in comps.get("env", {}).items():
        yield f"env:{env}", c
    for grp, c in comps.get("model", {}).items():
        yield f"model:{grp}", c
    for name, c in comps.get("app", {}).items():
        yield f"app:{name}", c


def components_for_edition(manifest: dict, edition: str) -> list[tuple[str, dict]]:
    spec = manifest.get("editions", {}).get(edition)
    if not spec:
        raise SystemExit(f"[ERROR] 未知档位：{edition}（可选：{', '.join(manifest.get('editions', {}))}）")
    comps = manifest.get("components", {})
    out = []
    for sid in spec.get("shared", []):          # 共享基座先装（环境落位依赖它）
        c = comps.get("shared", {}).get(sid)
        if c:
            out.append((f"shared:{sid}", c))
    for env in spec.get("envs", []):
        c = comps.get("env", {}).get(env)
        if c:
            out.append((f"env:{env}", c))
    for grp in spec.get("models", []):
        c = comps.get("model", {}).get(grp)
        if c:
            out.append((f"model:{grp}", c))
    # 程序本体全档位通用：manifest 带 app 组件则一律纳入（放末尾，见 iter_components 注释）
    for name, c in comps.get("app", {}).items():
        out.append((f"app:{name}", c))
    return out


# ══════════════════════════════════════════════════════════════════
#  验机（复用 doctor 的 nvidia-smi 查询口径）
# ══════════════════════════════════════════════════════════════════
def detect_gpus() -> list[dict]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return []
        gpus = []
        for line in out.stdout.strip().splitlines():
            if not line.strip():
                continue
            idx, name, tot, free = [x.strip() for x in line.split(",")]
            gpus.append({"index": int(idx), "name": name,
                         "total_mb": int(float(tot)), "free_mb": int(float(free))})
        return gpus
    except (FileNotFoundError, Exception):
        return []


# 各档位建议最低显存（GB）；用于「这台机能跑哪些档」推荐。
EDITION_MIN_VRAM_GB = {"lite": 6, "standard": 16, "flagship": 24}


def recommend_edition(manifest: dict, gpus: list[dict]) -> tuple[str | None, dict]:
    """返回 (推荐档位, 各档位是否可跑)。无 GPU → 全不推荐。"""
    max_vram = max((g["total_mb"] for g in gpus), default=0) / 1024.0
    runnable = {}
    best = None
    for ed in manifest.get("editions", {}):
        need = EDITION_MIN_VRAM_GB.get(ed, 999)
        ok = max_vram >= need
        runnable[ed] = ok
        if ok:
            best = ed  # editions 顺序 lite→standard→flagship，取最高可跑
    return best, runnable


# ══════════════════════════════════════════════════════════════════
#  安装状态
# ══════════════════════════════════════════════════════════════════
def env_install_dir(env: str) -> Path:
    return ENVS_ROOT / env


def is_installed(cid: str, comp: dict) -> bool:
    """组件是否物理就位（shared: 全部 blob 在 store；env: python.exe；model: 成员路径齐备）。"""
    kind, name = cid.split(":", 1)
    if kind == "shared":
        blobs = comp.get("blobs", {})
        return bool(blobs) and all((STORE_DIR / sha).exists() for sha in blobs)
    if kind == "env":
        return (env_install_dir(name) / "python.exe").exists()
    if kind == "app":
        # 程序本体在任何装机上都"物理存在"（Inno 铺的盘）；就位判定看核心编排脚本即可
        return (BASE / "avatar_hub.py").exists()
    # 模型：成员路径全部存在即视为已装
    members = comp.get("members", [])
    return bool(members) and all((BASE / m).exists() for m in members)


# ── 已装清单（增量更新依据）──────────────────────────────────────
def _load_installed() -> dict:
    try:
        return json.loads(INSTALLED_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_installed(state: dict):
    INSTALLED_STATE.parent.mkdir(parents=True, exist_ok=True)
    INSTALLED_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def record_installed(cid: str, comp: dict, manifest_version: str = ""):
    state = _load_installed()
    state[cid] = {"sha256": comp.get("sha256", ""), "file": comp.get("file", ""),
                  "version": manifest_version, "installed_at": int(time.time())}
    _save_installed(state)


def is_current(cid: str, comp: dict) -> bool:
    """物理就位且与 manifest 的 sha256 一致（即已是最新，无需重下）。"""
    if not is_installed(cid, comp):
        return False
    # 已隔离的坏热修：别再反复下载/自检（视为"当前"不再提供，直到发新 sha 或人工清隔离）
    if cid.startswith("app:") and _quarantined(comp.get("sha256", "")):
        return True
    rec = _load_installed().get(cid)
    if not rec:
        if cid.startswith("app:"):
            # 程序本体无安装记录（Inno 铺盘的存量机）：看 app_build.json 版本标记；
            # 连标记都没有 = 从未走过 app 组件 → 判"过期"，做一次代码同步把存量机
            # 纳入受管版本（几 MB，一次性）。
            try:
                mk = json.loads((BASE / "app_build.json").read_text(encoding="utf-8"))
                return str(mk.get("version", "")) == str(comp.get("app_version", ""))
            except Exception:
                return False
        return True   # 已就位但无记录（早期安装）：视为当前，不强制重下
    return rec.get("sha256", "") == comp.get("sha256", "")


# ── 灰度放量（P2）：新 app 版本按机器稳定分桶百分比下发；坏版本一键 halt 停放 ─────────
#   分桶：机器稳定 id → sha256 → 0..99；rollout.percent 覆盖 [0,percent) 的桶才可更新。
#   稳定 id 优先 telemetry.anon_id（跨版本不变），否则回落机器指纹（主机名+安装路径）。
def _machine_bucket() -> int:
    seed = ""
    try:
        import telemetry
        seed = telemetry.anon_id() or ""
    except Exception:
        seed = ""
    if not seed:
        import socket
        seed = socket.gethostname() + "|" + str(BASE)
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % 100


_BUCKET_CACHE = None


def rollout_bucket() -> int:
    global _BUCKET_CACHE
    if _BUCKET_CACHE is None:
        try:
            _BUCKET_CACHE = _machine_bucket()
        except Exception:
            _BUCKET_CACHE = 0
    return _BUCKET_CACHE


def _local_edition() -> str:
    """本机已装档位（installed.json 记的 _edition；无则空）。"""
    try:
        return str(_load_installed().get("_edition", "") or "")
    except Exception:
        return ""


def _local_vram_gb() -> int:
    try:
        gpus = detect_gpus()
        return round(max((g.get("total_mb", 0) for g in gpus), default=0) / 1024)
    except Exception:
        return 0


def _rollout_eligible(cid: str, comp: dict) -> tuple[bool, str]:
    """app 组件的灰度准入。返回 (可更新?, 原因)。非 app 或无 rollout 块 → 恒可。
    支持：halted 停放 / percent 百分比分桶 / editions 档位定向 / min_vram_gb 显存定向。
    强制立即更新逃生阀：AVATARHUB_FORCE_UPDATE=1 无视灰度（客服让用户手动 catch up 用）。"""
    if not cid.startswith("app:"):
        return True, ""
    r = comp.get("rollout") or {}
    if not r:
        return True, ""
    if os.environ.get("AVATARHUB_FORCE_UPDATE", "").strip() == "1":
        return True, "forced"
    # 运行时控制通道（密钥 B 签名）覆盖：halt 名单 / 紧急百分比覆盖。manifest 里的 rollout
    #   是发布时（密钥 A）定的初值；控制通道是事后（可 VPS 自动/看板一键）安全下发的覆盖层。
    ver = str(comp.get("app_version", ""))
    if _ACTIVE_SRC_ROOT:
        ctrl = _fetch_rollout_control(_ACTIVE_SRC_ROOT)
        if ctrl:
            if ver and ver in (ctrl.get("halted_versions") or []):
                return False, f"版本 {ver} 已被控制通道停放"
            ov = (ctrl.get("percent_overrides") or {}).get(ver)
            if ov is not None:
                try:
                    r = dict(r)
                    r["percent"] = int(ov)
                except Exception:
                    pass
    if r.get("halted"):
        return False, "该版本已暂停放量（发布方紧急停放）"
    # 档位定向：只对指定档位放量（先旗舰后 Lite 之类的分批控风险）
    eds = r.get("editions")
    if eds:
        cur_ed = _local_edition()
        if cur_ed and cur_ed not in eds:
            return False, f"未在定向档位（本机 {cur_ed} 不在 {eds}）"
    # 显存定向：低于门槛的机器先不放（大改可能吃显存时用）
    mv = r.get("min_vram_gb")
    if mv:
        try:
            if _local_vram_gb() < int(mv):
                return False, f"显存低于定向门槛（<{mv}GB）"
        except Exception:
            pass
    pct = r.get("percent")
    if pct is None:
        return True, ""
    try:
        pct = int(pct)
    except Exception:
        return True, ""
    if pct >= 100:
        return True, ""
    if pct <= 0:
        return False, "灰度 0%（尚未对外放量）"
    b = rollout_bucket()
    if b < pct:
        return True, f"命中灰度（桶 {b} < {pct}%）"
    return False, f"未命中灰度（桶 {b} ≥ {pct}%，将在扩量后收到）"


def check_updates(manifest: dict) -> list[tuple[str, dict]]:
    """已安装但 sha256 与 manifest 不一致的组件（可增量更新）。
    「已安装」按 安装记录 ∪ 物理就位 判定：新版组件**新增成员文件**时（如 swapcore
    1.0.1 增补 buffalo_l），旧装机上新成员必然缺失 → 纯物理判定会把它当"未安装"而
    被更新检查漏掉，用户永远收不到该更新（2026-07-13 140 装机实锤）。有安装记录的
    组件即使成员被删/不齐，也应出现在更新清单里（顺带修复=重下）。
    P2：app 组件叠加灰度准入——未命中放量桶/已停放的新版本不进更新清单（分批放量）。"""
    state = _load_installed()
    out = []
    for cid, c in iter_components(manifest):
        if not ((cid in state or is_installed(cid, c)) and not is_current(cid, c)):
            continue
        ok, _why = _rollout_eligible(cid, c)
        if ok:
            out.append((cid, c))
    return out


def update_summary(manifest: dict) -> dict:
    """更新检查的结构化结果，供 UI 展示「本次更新需下载 X + 明细」。
    {count, bytes, human, items:[{id, bytes, human}]}（仅已安装且 sha 变化的组件）。"""
    ups = check_updates(manifest)
    items = [{"id": cid, "bytes": c.get("size_bytes", 0),
              "human": _human(c.get("size_bytes", 0))} for cid, c in ups]
    total = sum(it["bytes"] for it in items)
    return {"count": len(items), "bytes": total, "human": _human(total), "items": items}


def plan(manifest: dict, edition: str) -> dict:
    comps = components_for_edition(manifest, edition)
    todo, have = [], []
    for cid, c in comps:
        (have if is_current(cid, c) else todo).append((cid, c))
    return {"todo": todo, "have": have,
            "download_bytes": sum(c.get("size_bytes", 0) for _, c in todo)}


# ══════════════════════════════════════════════════════════════════
#  下载（断点续传 + 多线程 Range + sha256 校验）
# ══════════════════════════════════════════════════════════════════
def _resolve_src(src_root: str, file_rel: str) -> str:
    return f"{src_root}/{file_rel}" if _is_url(src_root) else str(Path(src_root) / file_rel)


def _meta_path(dest: Path) -> Path:
    return dest.with_suffix(dest.suffix + ".dlmeta")


def _plan_ranges(total: int, threads: int) -> list[list[int]]:
    """把 [0,total) 切成若干 [start,end] 闭区间块（与线程数挂钩，块边界确定可复现）。"""
    n = max(1, min(threads, total // (1 << 20) or 1))
    size = total // n
    ranges = []
    for i in range(n):
        start = i * size
        end = total - 1 if i == n - 1 else (start + size - 1)
        ranges.append([start, end])
    return ranges


def _clear_parts(dest: Path):
    for p in dest.parent.glob(dest.name + ".part*"):
        p.unlink(missing_ok=True)
    _meta_path(dest).unlink(missing_ok=True)


def _http_head(url: str) -> tuple[int, bool]:
    """返回 (总字节, 是否支持 Range)。失败返回 (0, False)。"""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length", 0))
            ranges = r.headers.get("Accept-Ranges", "").lower() == "bytes"
            return total, ranges
    except Exception:
        return 0, False


def _dl_range(url: str, start: int, end: int, part: Path, counter, lock, progress_cb):
    """下载 [start,end] 到 part；若 part 已有数据则续传。"""
    have = part.stat().st_size if part.exists() else 0
    if have > 0:
        with lock:
            counter[0] += have
        if progress_cb:
            progress_cb(counter[0])
    if start + have > end:
        return
    req = urllib.request.Request(url, headers={"Range": f"bytes={start + have}-{end}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            with lock:
                counter[0] += len(chunk)
            if progress_cb:
                progress_cb(counter[0])


def _fetch_single(url: str, dest: Path, expected_sha, progress_cb) -> Path:
    """单流下载（不支持 Range 或末次回退）。整下，清历史分块避免误判。"""
    _clear_parts(dest)
    counter = [0]
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            counter[0] += len(chunk)
            if progress_cb:
                progress_cb(counter[0])
    if expected_sha and _sha256(dest) != expected_sha:
        raise ValueError(f"sha256 校验失败：{dest.name}（下载损坏，请重试）")
    return dest


def _fetch_multipart(url: str, dest: Path, expected_sha, total: int, threads: int, progress_cb) -> Path:
    """多线程分块 + 安全续传。中途异常时 .partN/.dlmeta 留存，下次（或重试）接着续。"""
    desired = _plan_ranges(total, threads)
    meta_p = _meta_path(dest)
    parts = [dest.with_suffix(dest.suffix + f".part{i}") for i in range(len(desired))]

    reuse = False
    if meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            reuse = (meta.get("total") == total and meta.get("ranges") == desired)
        except Exception:
            reuse = False
    if not reuse:
        _clear_parts(dest)
        meta_p.write_text(json.dumps({"total": total, "ranges": desired}), encoding="utf-8")
    else:
        for i, (s, e) in enumerate(desired):   # 丢弃超界陈旧块，令其重下
            if parts[i].exists() and parts[i].stat().st_size > (e - s + 1):
                parts[i].unlink(missing_ok=True)

    counter, lock = [0], threading.Lock()
    with ThreadPoolExecutor(max_workers=len(desired)) as ex:
        futs = [ex.submit(_dl_range, url, s, e, parts[i], counter, lock, progress_cb)
                for i, (s, e) in enumerate(desired)]
        for fu in as_completed(futs):
            fu.result()

    joined = dest.with_suffix(dest.suffix + ".joined")
    with open(joined, "wb") as out:
        for p in parts:
            with open(p, "rb") as pf:
                shutil.copyfileobj(pf, out)
    if expected_sha and _sha256(joined) != expected_sha:
        joined.unlink(missing_ok=True)
        _clear_parts(dest)   # 完整下完仍失败＝真损坏，清残留强制干净重试
        raise ValueError(f"sha256 校验失败：{dest.name}（下载损坏，请重试）")
    joined.replace(dest)
    _clear_parts(dest)
    return dest


def download(url_or_path: str, dest: Path, expected_sha: str | None,
             threads: int = DEFAULT_THREADS, progress_cb=None,
             retries: int = 4, backoff: float = 1.5, log=None,
             mirrors: list[str] | None = None) -> Path:
    """下载/复制到 dest 并校验 sha256，支持【多源 failover】：主源用尽重试仍失败则切下一镜像。
    各源共享 dest 的 .partN/.dlmeta，故切镜像可【接着续传】（同 total 时不从头）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 已存在且校验通过 → 直接返回（断点续传的最强形态：整文件级缓存）
    if dest.exists() and expected_sha and _sha256(dest) == expected_sha:
        if progress_cb:
            progress_cb(dest.stat().st_size)
        return dest

    candidates = [url_or_path] + [m for m in (mirrors or []) if m and m != url_or_path]
    last_err = None
    for ci, cand in enumerate(candidates):
        try:
            return _download_once(cand, dest, expected_sha, threads, progress_cb,
                                  retries, backoff, log)
        except Exception as e:
            last_err = e
            if ci < len(candidates) - 1 and log:
                log(f"   源失败（{type(e).__name__}），切换镜像 {ci + 2}/{len(candidates)}（接续已下分块）…")
    raise last_err if last_err else RuntimeError(f"下载失败：{dest.name}")


def _download_once(url_or_path: str, dest: Path, expected_sha: str | None,
                   threads: int, progress_cb, retries: int, backoff: float, log) -> Path:
    """单源下载/复制 + 校验。断点续传 + 多线程 + 自动重试(指数退避)。
    瞬时断流靠「重试即续传」自愈；末次重试回退单流以兼容挑剔的服务器/代理。"""
    # 本地源：直接校验/复制（无网络，不重试）
    if not _is_url(url_or_path):
        src = Path(url_or_path)
        if not src.exists():
            raise FileNotFoundError(f"本地源不存在：{src}")
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        if progress_cb:
            progress_cb(dest.stat().st_size)
        if expected_sha and _sha256(dest) != expected_sha:
            raise ValueError(f"sha256 校验失败：{dest.name}")
        return dest

    attempts = max(1, retries)
    last_err = None
    for attempt in range(attempts):
        total, ranges = _http_head(url_or_path)
        # 末次（且有过多次机会）回退单流：最大兼容性的最后一搏。
        final_fallback = attempts > 1 and attempt == attempts - 1
        try:
            if final_fallback or not ranges or total <= 0 or threads <= 1:
                return _fetch_single(url_or_path, dest, expected_sha, progress_cb)
            return _fetch_multipart(url_or_path, dest, expected_sha, total, threads, progress_cb)
        except Exception as e:
            last_err = e
            if attempt >= attempts - 1:
                break
            wait = min(30.0, backoff * (2 ** attempt))
            if log:
                log(f"   下载中断（{type(e).__name__}），{wait:.0f}s 后第 {attempt + 2}/{attempts} 次重试（断点续传）…")
            time.sleep(wait)
    raise last_err if last_err else RuntimeError(f"下载失败：{dest.name}")


# ══════════════════════════════════════════════════════════════════
#  解压 / 安装
# ══════════════════════════════════════════════════════════════════
@contextlib.contextmanager
def _open_tar(tarball: Path):
    """按扩展名选择解压编解码：.tar.zst → zstandard 流；其余 → gzip。
    zstd 流式不可 seek，故 _safe_extract 改为单遍迭代以兼容。"""
    name = str(tarball).lower()
    if name.endswith(".zst"):
        import zstandard
        with open(tarball, "rb") as fh:
            dctx = zstandard.ZstdDecompressor(max_window_size=2 ** 31)
            with dctx.stream_reader(fh) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    yield tar
    else:
        with tarfile.open(tarball, "r:gz") as tar:
            yield tar


def _safe_extract(tar: tarfile.TarFile, dest: Path):
    """防 path traversal 的解压。单遍迭代（流式友好）：逐成员先越界校验再解出。"""
    dest = dest.resolve()
    for m in tar:
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"非法归档成员（越界）：{m.name}")
        # filter 参数 3.12+ 才有，3.10 用户机回退到无 filter。
        try:
            tar.extract(m, dest, filter="fully_trusted")
        except TypeError:
            tar.extract(m, dest)


def _place_shared(dest: Path, placements: list, log=print):
    """把共享基座 blob 硬链回环境对应路径（conda-unpack 之前，确保文件齐全）。
    同卷硬链不占额外空间；跨卷或失败时回退复制。基座缺 blob 即报错（提示先装 shared）。"""
    linked = copied = 0
    for rel, sha in placements:
        src = STORE_DIR / sha
        if not src.exists():
            raise FileNotFoundError(f"共享基座缺失 blob {sha}（{rel}）——请先安装 shared 组件")
        tgt = dest / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        if tgt.exists() or tgt.is_symlink():
            tgt.unlink()
        try:
            os.link(str(src), str(tgt))
            linked += 1
        except OSError:
            shutil.copy2(str(src), str(tgt))
            copied += 1
    log(f"   共享库落位：硬链 {linked}" + (f" + 复制 {copied}" if copied else "")
        + f" 个（共 {len(placements)} 项）")


def install_shared(sid: str, tarball: Path, log=print):
    log(f"   解压共享基座 {sid} → {STORE_DIR} …")
    with _open_tar(tarball) as tar:
        _safe_extract(tar, BASE)   # 归档内 runtime/_store/<sha> → BASE/runtime/_store/<sha>


def install_env(env: str, tarball: Path, comp: dict | None = None, log=print):
    dest = env_install_dir(env)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    log(f"   解压环境 {env} → {dest} …")
    with _open_tar(tarball) as tar:
        _safe_extract(tar, dest)
    placements = (comp or {}).get("placements", [])
    if placements:                       # --shared 构建：把 torch/CUDA 库从基座硬链回来
        _place_shared(dest, placements, log=log)
    # conda-unpack 修正前缀（关键一步）
    unpack = dest / "Scripts" / "conda-unpack.exe"
    if unpack.exists():
        log(f"   conda-unpack {env} …")
        subprocess.run([str(unpack)], cwd=str(dest), check=True)
    else:
        log(f"   [warn] {env} 未找到 conda-unpack.exe，跳过前缀修正")
    _register_env_python(env, dest / "python.exe", log)


def install_model(group: str, tarball: Path, log=print):
    log(f"   解压模型 {group} → 项目根（保留相对路径）…")
    with _open_tar(tarball) as tar:
        _safe_extract(tar, BASE)


# ══════════════════════════════════════════════════════════════════
#  程序本体（app:core）：暂存 → 快照 → 原子覆盖 → 可回滚（2026-07-13 P0）
#  设计要点：
#   · .py/static 在 Windows 上运行期不加文件锁 → 可热覆盖；但为防"服务恰在重启时读到
#     半新半旧树"，覆盖用「先整包解到暂存目录，再逐文件 os.replace 秒级突发」收窄窗口。
#   · 直播/同传会话中绝不动代码：转入 pending，由启动器下次启动（服务未起时）应用。
#   · 应用前快照被覆盖文件 → runtime\app_prev\，--app-revert 一键回滚；断电/中断时
#     暂存目录仍在，pending 未清 → 下次启动重新应用（幂等）。
#   · 开发/受管目录护栏：BASE 下存在 .git（开发仓）→ 拒绝应用，防误覆盖工作区。
# ══════════════════════════════════════════════════════════════════
APP_STAGE_DIR = BASE / "runtime" / "app_staged"
APP_PREV_DIR = BASE / "runtime" / "app_prev"
APP_MEMBERS_FILE = BASE / "runtime" / "app_members.json"
APP_PENDING_FILE = APP_STAGE_DIR / "pending.json"


def _live_busy() -> str:
    """代码热覆盖的安全判定：返回非空原因 → 必须转 pending、不当场覆盖。
    收紧到「hub 只要在运行就延后」——运行中的 python 进程覆盖其 .py 虽不报错，但后续
    惰性 import 会撞上"新旧混合代码"（2026-07-13 实施期自查发现的并发隐患）。代码变更本
    就需重启才生效，所以正确模型=服务全关时（启动器下次启动）再原子应用；直播/同传更是
    硬红线。仅"hub 未运行"（全新装机/离线）才允许当场应用。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:9000/realtime/status", timeout=2) as r:
            if json.loads(r.read().decode("utf-8")).get("video_running"):
                return "直播（真人换脸）进行中"
    except Exception:
        pass
    try:
        with urllib.request.urlopen("http://127.0.0.1:7900/health", timeout=2) as r:
            if json.loads(r.read().decode("utf-8")).get("running"):
                return "同传会话进行中"
    except Exception:
        pass
    try:
        with urllib.request.urlopen("http://127.0.0.1:9000/health", timeout=2) as r:
            if getattr(r, "status", r.getcode()) == 200:
                return "中枢运行中（代码更新将在下次启动时应用）"
    except Exception:
        pass
    return ""


def _load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


APP_QUARANTINE_FILE = BASE / "runtime" / "app_quarantine.json"


def _runtime_python() -> str | None:
    """用于应用后语法自检的解释器：优先客户机运行环境 python（与服务同版本），
    否则当前解释器（非冻结）。冻结 launcher 亦可用其内置 py_compile 做纯语法校验。"""
    try:
        import app_config
        p = app_config.conda_python("facefusion")
        if p and Path(p).name.lower() == "python.exe" and Path(p).exists():
            return p
    except Exception:
        pass
    import sys as _sys
    if not getattr(_sys, "frozen", False):
        return _sys.executable
    return None


def _compile_gate(root: Path, members: list[str], log=print) -> tuple[bool, str]:
    """对暂存树的 *.py 做 py_compile 语法自检：坏热修（语法/缩进错）在覆盖前就拦下。
    返回 (通过?, 首个错误摘要)。无可用解释器 → 视为通过（跳过，交由运行期探针兜底）。"""
    pys = [root / m for m in members if m.endswith(".py")]
    if not pys:
        return True, ""
    py = _runtime_python()
    if not py:
        return True, ""
    import subprocess
    files = [str(p) for p in pys if p.is_file()]
    # 分批喂给 py_compile，避免命令行过长；任一失败即拦截
    for i in range(0, len(files), 60):
        batch = files[i:i + 60]
        try:
            r = subprocess.run([py, "-m", "py_compile", *batch],
                               capture_output=True, text=True, timeout=120)
        except Exception as e:
            return True, f"(自检跳过:{type(e).__name__})"   # 探针本身异常不误伤更新
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            return False, (err[-1] if err else "py_compile 失败")
    return True, ""


def _quarantined(sha: str) -> bool:
    try:
        return sha in set(json.loads(APP_QUARANTINE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return False


def _quarantine(sha: str):
    try:
        cur = []
        try:
            cur = json.loads(APP_QUARANTINE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cur = []
        if sha and sha not in cur:
            cur.append(sha)
        APP_QUARANTINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        APP_QUARANTINE_FILE.write_text(json.dumps(cur[-20:]), encoding="utf-8")
    except Exception:
        pass


def _apply_app_stage(staged: Path, comp: dict, log=print) -> bool:
    """把暂存好的 app 树应用到 BASE：语法自检 → 快照 → 突发覆盖 → 清理消失成员 → 记账。"""
    if (BASE / ".git").exists():
        raise RuntimeError("检测到 .git（开发/受管目录），拒绝 app 覆盖——请在客户安装目录使用")
    members = [m for m in comp.get("members", []) if m]
    # 应用前语法闸：坏热修在覆盖前就被拦（比事后回滚更安全，且不污染现网代码）
    ok, why = _compile_gate(staged, members if members else
                            [str(f.relative_to(staged)).replace("\\", "/")
                             for f in staged.rglob("*.py")], log=log)
    if not ok:
        _quarantine(comp.get("sha256", ""))
        shutil.rmtree(staged, ignore_errors=True)
        APP_PENDING_FILE.unlink(missing_ok=True)
        raise RuntimeError(f"程序更新语法自检未过，已丢弃（不覆盖现网代码）：{why}")
    if not members:   # 兜底：清单缺失时按暂存树实际文件走
        members = [str(f.relative_to(staged)).replace("\\", "/")
                   for f in staged.rglob("*") if f.is_file()]
    prev_members = _load_json(APP_MEMBERS_FILE, default=[]) or []
    # 1) 快照将被覆盖/删除的现有文件（整代快照：一次应用=一代，保留最近一代）
    if APP_PREV_DIR.exists():
        shutil.rmtree(APP_PREV_DIR, ignore_errors=True)
    snap_meta = {"record_prev": _load_installed().get("app:core"),
                 "members_new": members, "members_prev": prev_members,
                 "ts": int(time.time())}
    to_delete = [m for m in prev_members if m not in set(members)]
    for rel in members + to_delete:
        src = BASE / rel
        if src.is_file():
            dst = APP_PREV_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    APP_PREV_DIR.mkdir(parents=True, exist_ok=True)
    (APP_PREV_DIR / "meta.json").write_text(json.dumps(snap_meta, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    # 2) 突发覆盖（逐文件原子 replace，同卷秒级）
    n = 0
    for rel in members:
        sf = staged / rel
        if not sf.is_file():
            continue
        tf = BASE / rel
        tf.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(sf), str(tf))
        n += 1
    # 3) 上一版有、新版没有的成员 → 删除（防幽灵旧模块被误 import）
    removed = 0
    for rel in to_delete:
        try:
            (BASE / rel).unlink(missing_ok=True)
            removed += 1
        except Exception:
            pass
    APP_MEMBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    APP_MEMBERS_FILE.write_text(json.dumps(members, ensure_ascii=False), encoding="utf-8")
    record_installed("app:core", comp, str(comp.get("app_version", "")))
    _arm_probation(comp)      # 进入运行期验收观察（hub 健康→confirm；起不来→下次启动自愈回滚）
    log(f"   程序本体已更新 → v{comp.get('app_version','?')}（覆盖 {n} 文件"
        + (f"，清理 {removed} 个旧文件" if removed else "") + "；可 --app-revert 回滚）")
    shutil.rmtree(staged, ignore_errors=True)
    APP_PENDING_FILE.unlink(missing_ok=True)
    return True


def install_app(name: str, tarball: Path, comp: dict, log=print) -> bool:
    """下载校验后的 app 包 → 解到暂存目录；空闲即刻应用，忙则转 pending（下次启动应用）。
    返回 True=已应用（调用方记账）；False=已暂存待应用（不记账，更新项保持可见）。"""
    staged = APP_STAGE_DIR / (comp.get("sha256", "x")[:8] or "stage")
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True, exist_ok=True)
    log(f"   解压程序包 → 暂存 {staged.name} …")
    with _open_tar(tarball) as tar:
        _safe_extract(tar, staged)
    busy = _live_busy()
    if busy or os.environ.get("AVATARHUB_APP_APPLY", "").strip() == "defer":
        APP_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        APP_PENDING_FILE.write_text(json.dumps({"staged": staged.name, "comp": comp},
                                               ensure_ascii=False), encoding="utf-8")
        log(f"   {busy or '按指示延迟'} → 程序更新已就绪待应用（下次启动自动生效，不打断当前会话）")
        return False
    return _apply_app_stage(staged, comp, log=log)


def apply_pending_app(log=print) -> bool:
    """应用上次暂存的程序更新（启动器每次启动时调用；无 pending 则无操作）。"""
    pend = _load_json(APP_PENDING_FILE)
    if not pend:
        return False
    staged = APP_STAGE_DIR / str(pend.get("staged", ""))
    comp = pend.get("comp") or {}
    if not staged.is_dir():
        APP_PENDING_FILE.unlink(missing_ok=True)
        return False
    busy = _live_busy()
    if busy:
        log(f"   {busy}，程序更新继续顺延")
        return False
    try:
        return _apply_app_stage(staged, comp, log=log)
    except Exception as e:
        log(f"   待应用程序更新失败（保留暂存，可重试）：{e}")
        return False


# ── 应用后运行期自检（P1 改进：回滚从"手动"升级为"自愈"）───────────────────────
#   语法闸拦不住"能编译但起不来"的运行期错误（如顶层坏 import/初始化异常）。故应用后进入
#   probation：本次启动确认 hub 健康 → confirm 清标记；若从未确认（上次应用后 hub 起不来、
#   进程/用户退出）→ 下次启动检测到未确认标记 → 自动回滚上一代 + 隔离坏 sha。
APP_PROBATION_FILE = BASE / "runtime" / "app_probation.json"


def _arm_probation(comp: dict):
    try:
        APP_PROBATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        APP_PROBATION_FILE.write_text(json.dumps({
            "version": comp.get("app_version", ""), "sha": comp.get("sha256", ""),
            "applied_at": int(time.time()), "confirmed": False}), encoding="utf-8")
    except Exception:
        pass


def confirm_app_ok(log=print):
    """hub 确认健康后调用：清 probation 标记（本代更新验收通过）。"""
    p = _load_json(APP_PROBATION_FILE)
    if p and not p.get("confirmed"):
        log(f"   程序更新 v{p.get('version','?')} 运行验收通过（hub 健康），已确认。")
    APP_PROBATION_FILE.unlink(missing_ok=True)


def app_probation_pending() -> bool:
    """本机是否处于"刚应用热修、尚未通过运行验收"状态（供 launcher 决定是否即时回滚）。"""
    p = _load_json(APP_PROBATION_FILE)
    return bool(p and not p.get("confirmed"))


def check_and_revert_probation(log=print) -> bool:
    """启动早期调用：存在【上一会话遗留的未确认】probation → 判定上次热修起不来 → 自动回滚。
    仅对"往届"标记生效（applied_at 早于本进程启动前 ≥1s），避免误伤本会话刚应用的更新。"""
    p = _load_json(APP_PROBATION_FILE)
    if not p or p.get("confirmed"):
        return False
    # 本会话刚 arm 的不动（由本会话的 confirm 负责）；只处理跨会话残留。
    if int(time.time()) - int(p.get("applied_at", 0)) < 2:
        return False
    log(f"   检测到程序更新 v{p.get('version','?')} 上次启动未通过运行验收 → 自动回滚上一代（自愈）。")
    _quarantine(p.get("sha", ""))
    APP_PROBATION_FILE.unlink(missing_ok=True)
    try:
        return app_revert(log=log)
    except Exception as e:
        log(f"   自动回滚失败：{e}")
        return False


def stage_exe_update(new_exe: Path, log=print) -> bool:
    """把下载好的新 exe 暂存 + 写摆渡计划：运行中的 exe 无法自替换，故退出时由摆渡脚本
    （等父进程退出→替换→重启）完成。仅冻结态有意义。返回是否已排程。"""
    try:
        import sys as _sys
        if not getattr(_sys, "frozen", False):
            return False
        cur = Path(_sys.executable)
        staged = APP_STAGE_DIR / "AvatarHub.new.exe"
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(new_exe, staged)
        (APP_STAGE_DIR / "exe_pending.json").write_text(json.dumps({
            "target": str(cur), "staged": str(staged), "ts": int(time.time())}), encoding="utf-8")
        log(f"   新版控制台已就绪（{_human(staged.stat().st_size)}）；退出时自动替换并重启。")
        return True
    except Exception as e:
        log(f"   exe 更新暂存失败：{e}")
        return False


def spawn_exe_swap_on_exit(log=print) -> bool:
    """启动器退出前调用：若有 exe_pending，spawn 一个分离的 cmd 摆渡脚本
    （等本进程退出→覆盖→重启），随后本进程正常退出。返回是否已 spawn。"""
    pend = _load_json(APP_STAGE_DIR / "exe_pending.json")
    if not pend:
        return False
    target, staged = pend.get("target", ""), pend.get("staged", "")
    if not (target and staged and Path(staged).is_file()):
        (APP_STAGE_DIR / "exe_pending.json").unlink(missing_ok=True)
        return False
    try:
        import subprocess, sys as _sys, os as _os
        pid = _os.getpid()
        bat = APP_STAGE_DIR / "_exe_swap.cmd"
        # 等父进程退出→覆盖→重启→自删。tasklist 轮询避免抢在文件仍被占用时替换。
        bat.write_text(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            f":wait\r\n"
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            "if not errorlevel 1 ( ping -n 2 127.0.0.1 >nul & goto wait )\r\n"
            "ping -n 2 127.0.0.1 >nul\r\n"
            f'copy /y "{staged}" "{target}" >nul\r\n'
            f'del /q "{staged}" >nul 2>&1\r\n'
            f'del /q "{APP_STAGE_DIR / "exe_pending.json"}" >nul 2>&1\r\n'
            f'start "" "{target}"\r\n'
            'del /q "%~f0" >nul 2>&1\r\n', encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(bat)],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "DETACHED_PROCESS", 0))
        log("   已排程控制台自替换（退出后完成并自动重启）。")
        return True
    except Exception as e:
        log(f"   exe 摆渡 spawn 失败：{e}")
        return False


def app_revert(log=print) -> bool:
    """回滚到上一代程序快照（runtime\\app_prev）。"""
    meta = _load_json(APP_PREV_DIR / "meta.json")
    if not meta:
        log(" 无可回滚的程序快照（app_prev 为空）。")
        return False
    if (BASE / ".git").exists():
        log(" 开发/受管目录，拒绝回滚操作。")
        return False
    members_new = meta.get("members_new", []) or []
    restored = removed = 0
    # 1) 新版新增（快照里没有）的成员 → 删除
    for rel in members_new:
        if not (APP_PREV_DIR / rel).is_file() and (BASE / rel).is_file():
            try:
                (BASE / rel).unlink()
                removed += 1
            except Exception:
                pass
    # 2) 快照文件全部还原
    for f in APP_PREV_DIR.rglob("*"):
        if not f.is_file() or f.name == "meta.json":
            continue
        rel = f.relative_to(APP_PREV_DIR)
        dst = BASE / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        restored += 1
    # 3) 记账回退：恢复上代安装记录与成员清单（无上代记录=清除本代记录 → 更新项重新可见）
    state = _load_installed()
    if meta.get("record_prev"):
        state["app:core"] = meta["record_prev"]
    else:
        state.pop("app:core", None)
    _save_installed(state)
    APP_MEMBERS_FILE.write_text(json.dumps(meta.get("members_prev", []) or [],
                                           ensure_ascii=False), encoding="utf-8")
    # 回滚=放弃这版更新：清 probation（该版本的运行验收已无意义，防其它路径二次回滚）。
    APP_PROBATION_FILE.unlink(missing_ok=True)
    log(f" 程序已回滚到上一代快照（还原 {restored} 文件，删除新增 {removed} 文件）。"
        f" 重启服务后生效。")
    return True


def _register_env_python(env: str, py: Path, log=print):
    """把环境解释器写入 config.json["conda_python"][env]，免 conda 定位。"""
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg.setdefault("conda_python", {})[env] = str(py)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"   已登记 {env} 解释器到 config.json")


def install_component(cid: str, comp: dict, src_root, threads=DEFAULT_THREADS,
                      progress_cb=None, log=print, keep_cache=False, manifest_version=""):
    kind, name = cid.split(":", 1)
    file_rel = comp["file"]
    roots = src_root if isinstance(src_root, (list, tuple)) else [src_root]
    urls = [_resolve_src(r, file_rel) for r in roots]
    tarball = CACHE_DIR / Path(file_rel).name
    log(f" ▶ {cid}  ({comp.get('size_human', '?')})")
    download(urls[0], tarball, comp.get("sha256"), threads=threads,
             progress_cb=progress_cb, log=log, mirrors=urls[1:])
    log(f"   sha256 校验通过")
    if kind == "shared":
        install_shared(name, tarball, log=log)
    elif kind == "env":
        install_env(name, tarball, comp, log=log)
    elif kind == "app":
        applied = install_app(name, tarball, comp, log=log)
        if not keep_cache:
            tarball.unlink(missing_ok=True)
        # 已应用：install_app 内部已记账；仅暂存：不记账（保持"可更新"可见，待启动时应用）
        log(f"   {cid} " + ("安装完成" if applied else "已暂存，重启生效"))
        return
    else:
        install_model(name, tarball, log=log)
    if not keep_cache:
        tarball.unlink(missing_ok=True)
    record_installed(cid, comp, manifest_version)
    log(f"   {cid} 安装完成")


def _emit_receipt(rec, kind: str, edition: str, manifest: dict, src_root, log=print, gpu=None):
    """把会话 recorder 收尾成匿名回执并提交（始终写本地、仅开启时外发）。telemetry 缺失则忽略。"""
    if rec is None or telemetry is None or not rec.items:
        return
    try:
        roots = src_root if isinstance(src_root, (list, tuple)) else [src_root]
        receipt = rec.finalize(kind, edition, manifest, sources=roots,
                               channel=os.environ.get("AVATARHUB_CHANNEL", ""), gpu=gpu)
        telemetry.submit(receipt, manifest, log=log)
    except Exception:
        pass


def install_components(manifest: dict, comps: list, src_root: str, threads=DEFAULT_THREADS,
                       log=print, on_overall=None, keep_cache=False, recorder=None):
    """安装/更新一组组件（首装与增量更新共用）。on_overall(done,total,cid) 供 UI 总进度。
    逐组件容错：单个组件多次重试后仍失败也不拖垮整批，继续装其余，最后返回失败清单。
    返回 list[(cid, errmsg)]（空＝全成功）。失败组件不会写 installed.json，重试时仍会被选中。
    recorder（可选）：累计组件级成败/字节/耗时/错误类名，供匿名健康回执（不含路径/原文）。"""
    total = sum(c.get("size_bytes", 0) for _, c in comps)
    ver = manifest.get("version", "")
    done_before = [0]
    failed = []
    for cid, comp in comps:
        size = comp.get("size_bytes", 0)

        def _cb(cur, _cid=cid):
            if on_overall:
                on_overall(done_before[0] + cur, total, _cid)

        t0 = time.time()
        try:
            install_component(cid, comp, src_root, threads=threads, progress_cb=_cb,
                              log=log, keep_cache=keep_cache, manifest_version=ver)
            if recorder is not None:
                recorder.add(cid, True, size, time.time() - t0)
        except Exception as e:
            failed.append((cid, str(e)))
            if recorder is not None:
                recorder.add(cid, False, size, time.time() - t0, type(e).__name__)  # 仅类名，不含原文
            log(f"   [失败] {cid}：{e}（已跳过，可稍后重试该组件）")
        done_before[0] += size   # 失败也推进总进度，避免进度条卡住
    return failed


def install_edition(manifest: dict, edition: str, src_root: str, threads=DEFAULT_THREADS,
                    log=print, on_overall=None, keep_cache=False, dedup=True):
    """安装某档位所有缺失/过期组件（首装与更新统一走 is_current 判定）。
    dedup=True：装完对 runtime/envs 跨环境硬链去重（实测可省 ~50% 环境占盘）。"""
    p = plan(manifest, edition)
    todo = p["todo"]
    log(f"档位 {edition}：需下载 {len(todo)} 个组件，合计 {_human(p['download_bytes'])}"
        f"（已最新 {len(p['have'])} 个）")
    # 记录本机档位（供灰度档位定向；键前缀 _ 不与组件 cid 冲突）
    try:
        st = _load_installed()
        st["_edition"] = edition
        _save_installed(st)
    except Exception:
        pass
    rec = telemetry.Recorder() if telemetry else None
    failed = install_components(manifest, todo, src_root, threads=threads,
                                log=log, on_overall=on_overall, keep_cache=keep_cache, recorder=rec)
    if failed:
        log(f"完成 {len(todo) - len(failed)}/{len(todo)} 个；失败 {len(failed)} 个："
            f"{', '.join(cid for cid, _ in failed)}（可重试）")
    else:
        log("全部组件安装完成。")
    if dedup and any(cid.startswith("env:") for cid, _ in todo):
        log("正在跨环境去重以节省磁盘…")
        dedup_runtime_envs(log=log)
    _emit_receipt(rec, "install", edition, manifest, src_root, log)
    return failed


def update_all(manifest: dict, src_root: str, threads=DEFAULT_THREADS,
               log=print, on_overall=None, keep_cache=False, dedup=True):
    """增量更新：只重下已安装但 sha 变化的组件。"""
    ups = check_updates(manifest)
    log(f"可更新组件 {len(ups)} 个，合计 {_human(sum(c.get('size_bytes',0) for _, c in ups))}")
    rec = telemetry.Recorder() if telemetry else None
    failed = install_components(manifest, ups, src_root, threads=threads,
                                log=log, on_overall=on_overall, keep_cache=keep_cache, recorder=rec)
    if not ups:
        log("已是最新，无需更新。")
    elif failed:
        log(f"更新完成 {len(ups) - len(failed)}/{len(ups)} 个；失败：{', '.join(cid for cid, _ in failed)}")
    else:
        log("更新完成。")
    if dedup and any(cid.startswith("env:") for cid, _ in ups):
        log("正在跨环境去重以节省磁盘…")
        dedup_runtime_envs(log=log)
    _emit_receipt(rec, "update", "", manifest, src_root, log)
    return failed


# ══════════════════════════════════════════════════════════════════
#  环境去重（装后内容寻址硬链：跨环境同一 torch/CUDA 大二进制只占一份盘）
# ══════════════════════════════════════════════════════════════════
def dedup_runtime_envs(root: Path | None = None, min_size: int = 1 << 20, log=print) -> dict:
    """对 runtime/envs/* 下「字节一致的大文件」跨环境做硬链去重，省盘不改下载。
    安全前提：仅在各环境已 conda-unpack 完毕（互相独立）后运行；只动同卷、字节一致、
    ≥min_size 的常规文件（实测冗余几乎全是 torch/CUDA DLL，pip 二进制不含 conda 前缀，
    conda-unpack 不会再改它们）。原子化：先建临时硬链再 os.replace，失败则原文件不动。
    幂等：已同 inode 的跳过。返回 {saved_bytes, links, groups}。"""
    from collections import defaultdict
    root = root or ENVS_ROOT
    if not root.exists():
        log("   无 runtime/envs，跳过去重。")
        return {"saved_bytes": 0, "links": 0, "groups": 0}

    by_size: dict[int, list[Path]] = defaultdict(list)
    for p in root.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink() and p.stat().st_size >= min_size:
                by_size[p.stat().st_size].append(p)
        except OSError:
            pass

    saved = links = groups = 0
    for sz, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_sha: dict[str, list[Path]] = defaultdict(list)
        for p in paths:
            try:
                by_sha[_sha256(p)].append(p)
            except OSError:
                pass
        for _sha, plist in by_sha.items():
            if len(plist) < 2:
                continue
            groups += 1
            try:                                   # 选已被硬链最多的作主，减少新建
                canonical = max(plist, key=lambda x: x.stat().st_nlink)
            except OSError:
                canonical = plist[0]
            try:
                cstat = canonical.stat()
            except OSError:
                continue
            for p in plist:
                if p == canonical:
                    continue
                tmp = p.with_name(p.name + ".dedup_tmp")
                try:
                    ps = p.stat()
                    if ps.st_dev != cstat.st_dev:          # 跨卷不可硬链
                        continue
                    if ps.st_ino == cstat.st_ino and ps.st_ino != 0:  # 已同 inode
                        continue
                    tmp.unlink(missing_ok=True)
                    os.link(str(canonical), str(tmp))      # 先建临时硬链
                    os.replace(str(tmp), str(p))           # 原子替换，dup 旧内容释放
                    saved += sz
                    links += 1
                except OSError as e:
                    log(f"   [skip] 去重 {p.name} 失败：{e}")
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
    log(f"   环境去重：硬链 {links} 个文件，省盘 {_human(saved)}（{groups} 组字节一致）")
    return {"saved_bytes": saved, "links": links, "groups": groups}


# ══════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════
def _cli_gpu(manifest):
    gpus = detect_gpus()
    if not gpus:
        print(" 未检测到 NVIDIA GPU（或无 nvidia-smi）。实时数字人需要 GPU。")
    for g in gpus:
        print(f"  GPU#{g['index']} {g['name']}  显存 {g['total_mb']/1024:.0f}GB"
              f"（空闲 {g['free_mb']/1024:.1f}GB）")
    best, runnable = recommend_edition(manifest, gpus)
    print(" 各档位可运行性：")
    for ed, ok in runnable.items():
        label = manifest["editions"][ed].get("label", ed)
        print(f"   [{'可跑' if ok else '不足'}] {ed:9s} {label}")
    print(f" 推荐档位：{best or '（硬件不满足，建议升级显卡或用云端版）'}")


def _cli_status(manifest):
    print(" 组件安装状态：")
    for cid, c in iter_components(manifest):
        if not is_installed(cid, c):
            mark = "未装"
        elif is_current(cid, c):
            mark = "最新"
        else:
            mark = "可更新"
        print(f"   [{mark}] {cid:22s} {c.get('size_human','?'):>8s}")


def _cli_check_updates(manifest):
    ups = check_updates(manifest)
    if not ups:
        print(" 无可更新组件（已安装的均为最新）。")
        return
    total = sum(c.get("size_bytes", 0) for _, c in ups)
    print(f" 可更新 {len(ups)} 个组件，合计 {_human(total)}：")
    for cid, c in ups:
        print(f"   ↑ {cid:22s} {c.get('size_human','?')}")


def _cli_plan(manifest, edition):
    p = plan(manifest, edition)
    print(f" 档位 {edition}：待下载 {len(p['todo'])} 个 / 已就位 {len(p['have'])} 个，"
          f"需下载 {_human(p['download_bytes'])}")
    for cid, c in p["todo"]:
        print(f"   - {cid:22s} {c.get('size_human','?')}")


def main():
    ap = argparse.ArgumentParser(description="分发包下载/安装引擎")
    ap.add_argument("--manifest", default="", help="manifest.json 路径或 URL（--apply-pending/--app-revert 可省）")
    ap.add_argument("--edition", help="档位：lite/standard/flagship")
    ap.add_argument("--gpu", action="store_true", help="验机 + 推荐档位")
    ap.add_argument("--status", action="store_true", help="列出各组件安装状态")
    ap.add_argument("--plan", action="store_true", help="列出该档位待下载清单")
    ap.add_argument("--install", action="store_true", help="安装该档位缺失/过期组件")
    ap.add_argument("--check-updates", action="store_true", help="列出可增量更新的组件")
    ap.add_argument("--update", action="store_true", help="增量更新所有已装但过期的组件")
    ap.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    ap.add_argument("--keep-cache", action="store_true", help="安装后保留下载缓存")
    ap.add_argument("--dedup", action="store_true",
                    help="对 runtime/envs 跨环境做内容寻址硬链去重（省盘；装/更新后自动跑）")
    ap.add_argument("--mirror", action="append", default=[],
                    help="临时追加镜像根 URL（可重复）；与 manifest.mirrors/AVATARHUB_MIRRORS 合并后择优")
    ap.add_argument("--channel", default=os.environ.get("AVATARHUB_CHANNEL", ""),
                    help="发布通道（如 stable/beta）；--manifest 指向 channels.json 或带 channels_url 的 manifest 时据此选道")
    ap.add_argument("--list-rollback", action="store_true", help="列出可回滚的历史版本（读 manifest.versions_url）")
    ap.add_argument("--rollback", default="", help="回滚到指定版本号，或 'prev' 回退到最近的上一版")
    ap.add_argument("--telemetry", choices=["on", "off", "status"], default=None,
                    help="匿名安装健康回执开关（默认关闭/opt-in；本地始终留回执，仅 on 时外发）")
    ap.add_argument("--apply-pending", action="store_true",
                    help="应用上次暂存的程序更新（app 组件；直播中下载的更新会走暂存）")
    ap.add_argument("--app-revert", action="store_true",
                    help="程序本体回滚到上一代快照（runtime\\app_prev）")
    args = ap.parse_args()

    if args.telemetry is not None and telemetry is not None:
        if args.telemetry in ("on", "off"):
            telemetry.set_enabled(args.telemetry == "on")
        print(f"[telemetry] 外发={'开' if telemetry.enabled() else '关'}　本地回执目录={telemetry.TELE_DIR}")
        if args.telemetry == "status":
            return 0

    if args.apply_pending:
        return 0 if apply_pending_app(log=print) else 1
    if args.app_revert:
        return 0 if app_revert(log=print) else 1
    if not args.manifest:
        ap.error("--manifest 必填（仅 --apply-pending/--app-revert 可省）")

    entry = resolve_channel_source(args.manifest, args.channel)
    if args.channel and entry != args.manifest:
        print(f"[channel] {args.channel} → {entry}")
    manifest, src_root = load_manifest(entry)
    global _ACTIVE_SRC_ROOT
    _ACTIVE_SRC_ROOT = src_root            # 供灰度准入取同源 rollout_control.json（密钥 B）
    if args.mirror:                       # CLI 追加的镜像并进 manifest，统一走 resolve/order
        manifest.setdefault("mirrors", [])
        manifest["mirrors"] += [m.rstrip("/") for m in args.mirror]
    src_roots = resolve_sources(manifest, src_root)
    if len(src_roots) > 1:
        print(f"AvatarHub manifest v{manifest.get('version')}  发现 {len(src_roots)} 个源，按延迟择优：")
        src_roots = order_sources(src_roots, log=print)
        src_root = src_roots                          # 传列表给安装链：首源主用，其余 failover
    else:
        print(f"AvatarHub manifest v{manifest.get('version')}  来源：{src_root}")
    _ACTIVE_SRC_ROOT = src_roots if isinstance(src_roots, list) else [src_roots]  # 控制通道候选源

    last = [0.0]

    def overall(done, total, cid):
        now = time.time()
        if now - last[0] >= 0.5 or done >= total:
            last[0] = now
            pct = (done / total * 100) if total else 100
            print(f"\r  下载 {pct:5.1f}%  {_human(done)}/{_human(total)}  [{cid}]   ",
                  end="", flush=True)

    if args.list_rollback:
        pts = list_rollback_points(manifest)
        if not pts:
            print("无可回滚版本（manifest 无 versions_url 或链中仅当前版）。")
        else:
            print(f"可回滚版本（共 {len(pts)}，最近在前）：")
            for p in pts:
                print(f"   v{p['version']:10s} {p['date']:25s} {p['manifest_url']}")
    if args.rollback:
        pts = list_rollback_points(manifest)
        target = None
        if args.rollback == "prev":
            target = pts[0]["manifest_url"] if pts else None
        else:
            target = next((p["manifest_url"] for p in pts if p["version"] == args.rollback), None)
        if not target:
            print(f"[ERROR] 找不到可回滚目标：{args.rollback}")
            return 2
        failed = rollback_to(target, threads=args.threads, on_overall=overall,
                             keep_cache=args.keep_cache, dedup=args.dedup)
        print()
        return 2 if failed else 0

    if args.gpu:
        _cli_gpu(manifest)
    if args.status:
        _cli_status(manifest)
    if args.check_updates:
        _cli_check_updates(manifest)
    if args.edition and args.plan:
        _cli_plan(manifest, args.edition)
    rc = 0
    if args.edition and args.install:
        failed = install_edition(manifest, args.edition, src_root, threads=args.threads,
                                 on_overall=overall, keep_cache=args.keep_cache, dedup=args.dedup)
        print()
        rc = rc or (2 if failed else 0)
    if args.update:
        failed = update_all(manifest, src_root, threads=args.threads,
                            on_overall=overall, keep_cache=args.keep_cache, dedup=args.dedup)
        print()
        rc = rc or (2 if failed else 0)
    if args.dedup and not (args.install or args.update):   # 独立去重（不重复触发）
        dedup_runtime_envs(log=print)
    if not (args.gpu or args.status or args.plan or args.install
            or args.check_updates or args.update or args.dedup):
        _cli_status(manifest)
    return rc


if __name__ == "__main__":
    sys.exit(main())
