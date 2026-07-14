# -*- coding: utf-8 -*-
"""publish_release.py — 读 release.config.json 的一键发布编排（证书/下载站就位即可发布）。

一条命令完成、逐级门禁、失败即停：
    构建 exe → 签名 exe → make_release --ci（预检→构建包/便携/安装包→烟测→装配 publish→增量/矩阵）
    → 签名安装包 → 打印上传指引。
任一闸门失败（预检/烟测/验收）即中止，绝不产出可发布树。

用法（在装有 conda-pack 的 base/conda python 下运行）：
    copy release.config.example.json release.config.json   # 然后按注释填 base_url / 证书 / 通道
    python publish_release.py                # 正式发布（含发布后本地自检）
    python publish_release.py --dry-run      # 预演：仅校验+预检+打印计划，不构建/不签名/不发布
    python publish_release.py --verify-remote  # 上传后冒烟：拉 base_url/manifest.json 并 HEAD 校验各组件可达+体积
    # 发布完成后按 dist/publish/<version>/UPLOAD.md 上传到下载站

设计：本脚本只做「编排 + 顺序 + 签名时机 + 自检」，真正的构建/门禁复用已验证的 make_release.py /
build_launcher.bat / sign_artifacts.bat —— 不重复实现、不绕过它们的门禁。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "release.config.json"
EXAMPLE = HERE / "release.config.example.json"
DIST = HERE / "dist"
LOGS = HERE / "logs"


def _die(msg: str, code: int = 1):
    print(f"[publish] 错误：{msg}")
    sys.exit(code)


def load_config() -> dict:
    if not CONFIG.exists():
        _die(f"未找到 {CONFIG.name}。请先复制模板：\n"
             f"    copy {EXAMPLE.name} {CONFIG.name}\n"
             f"再按其中注释填写 base_url / 证书 / 通道。", 2)
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception as e:
        _die(f"{CONFIG.name} 解析失败：{e}", 2)
    base = str(cfg.get("base_url", "")).strip()
    if not base or "REPLACE_ME" in base:
        _die("base_url 未填写（仍是占位符）。真实发布必须指向你的下载站该版本根 URL。", 2)
    if not str(cfg.get("version", "")).strip():
        _die("version 未填写。", 2)
    return cfg


def _sign_env(sign: dict) -> dict | None:
    """据 sign 配置构造 sign_artifacts.bat 的环境变量；未启用/要素不全返回 None。"""
    if not sign.get("enabled"):
        return None
    env = os.environ.copy()
    ts = str(sign.get("timestamp") or "").strip()
    if ts:
        env["AVATARHUB_SIGN_TS"] = ts
    if str(sign.get("sha1", "")).strip():
        env["AVATARHUB_SIGN_SHA1"] = sign["sha1"].strip()
    elif str(sign.get("pfx", "")).strip():
        env["AVATARHUB_SIGN_PFX"] = sign["pfx"].strip()
        env["AVATARHUB_SIGN_PFX_PW"] = str(sign.get("pfx_pw", ""))
    elif str(sign.get("subject", "")).strip():
        env["AVATARHUB_SIGN_SUBJECT"] = sign["subject"].strip()
    else:
        _die("sign.enabled=true 但未提供 sha1 / pfx / subject 任一证书来源。", 2)
    return env


def _sign_source(sign: dict) -> str:
    if not sign.get("enabled"):
        return "none"
    if str(sign.get("sha1", "")).strip():
        return "sha1"
    if str(sign.get("pfx", "")).strip():
        return "pfx"
    if str(sign.get("subject", "")).strip():
        return "subject"
    return "none"


def _index_summary(ver: str) -> dict:
    """从 publish 树的 release_index.json 汇总组件数与总体积（排除 manifest 自身）。"""
    idx = DIST / "publish" / ver / "release_index.json"
    if not idx.exists():
        return {"count": 0, "total_bytes": 0, "assembled_at": ""}
    data = json.loads(idx.read_text(encoding="utf-8"))
    files = [e for e in data.get("files", [])
             if e.get("present") and e.get("remote") != "manifest.json"]
    total = sum(int(e.get("size_bytes", 0) or 0) for e in files)
    return {"count": len(files), "total_bytes": total, "assembled_at": data.get("assembled_at", "")}


def _per_edition_sizes(ver: str) -> dict:
    """各档位全量下载体积（字节）：从 publish 树 manifest.json 按组件求和。"""
    try:
        from make_release import _edition_component_ids, _iter_components
        man = DIST / "publish" / ver / "manifest.json"
        if not man.exists():
            man = DIST / "manifest.json"
        m = json.loads(man.read_text(encoding="utf-8"))
        csize = {cid: c.get("size_bytes", 0) for cid, c in _iter_components(m)}
        out = {}
        for ed, spec in m.get("editions", {}).items():
            ids = _edition_component_ids(m, ed, spec)
            out[ed] = sum(csize.get(c, 0) for c in ids)
        return out
    except Exception:
        return {}


def _per_edition_increment(ver: str, prev_manifest: str) -> dict:
    """老用户升级到本版的各档实际下载：{ed: (增量字节, 全量字节)}；无 prev 返回 {}。"""
    if not str(prev_manifest or "").strip():
        return {}
    try:
        from make_release import _edition_component_ids, _iter_components, _load_prev_shas
        man = DIST / "publish" / ver / "manifest.json"
        if not man.exists():
            man = DIST / "manifest.json"
        m = json.loads(man.read_text(encoding="utf-8"))
        cur_sha = {cid: c.get("sha256", "") for cid, c in _iter_components(m)}
        csize = {cid: c.get("size_bytes", 0) for cid, c in _iter_components(m)}
        prev = _load_prev_shas(prev_manifest)
        out = {}
        for ed, spec in m.get("editions", {}).items():
            ids = _edition_component_ids(m, ed, spec)
            dl = [c for c in ids if prev.get(c) != cur_sha.get(c)]   # 新增或变更才需重下
            out[ed] = (sum(csize.get(c, 0) for c in dl), sum(csize.get(c, 0) for c in ids))
        return out
    except Exception:
        return {}


def _mark_remote_verified(ver: str) -> dict | None:
    """远端冒烟通过后，回写「远端已验证」标记到本版最新 publish 回执，形成可追溯链；返回更新后的回执。"""
    cands = sorted(LOGS.glob(f"release_receipt_{ver}_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    cands = [p for p in cands if not p.name.endswith("_verify-remote.json")]
    if not cands:
        return None
    p = cands[0]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data["remote_verified"] = True
        data["remote_verified_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[receipt] 已回写「远端已验证」到 {p.name}")
        return data
    except Exception:
        return None


def _append_ledger(receipt: dict):
    """把本次回执追加到 logs/release_history.csv（不存在则写表头），长期可审计。"""
    LOGS.mkdir(parents=True, exist_ok=True)
    led = LOGS / "release_history.csv"
    cols = ["receipt_at", "phase", "version", "channel", "signed", "sign_source",
            "with_installer", "components_count", "total_bytes", "total_human",
            "result", "host"]
    comp = receipt.get("components", {})
    row = {
        "receipt_at": receipt.get("receipt_at", ""), "phase": receipt.get("phase", ""),
        "version": receipt.get("version", ""), "channel": receipt.get("channel", ""),
        "signed": receipt.get("signed", ""), "sign_source": receipt.get("sign_source", ""),
        "with_installer": receipt.get("with_installer", ""),
        "components_count": comp.get("count", ""), "total_bytes": comp.get("total_bytes", ""),
        "total_human": comp.get("total_human", ""),
        "result": receipt.get("local_smoke") or receipt.get("remote") or "",
        "host": receipt.get("host", ""),
    }
    new = not led.exists()
    with open(led, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow(row)
    print(f"[ledger] 已登记发布台账：{led}")


def _write_release_notes(cfg: dict, receipt: dict) -> Path:
    """渲染人读发布说明 RELEASE_NOTES_<ver>.md，并复制进 publish 树随包交付。"""
    from make_release import _human
    ver = str(cfg["version"])
    comp = receipt.get("components", {})
    eds = _per_edition_sizes(ver)
    lines = [
        f"# AvatarHub {ver} 发布说明", "",
        f"- 发布时间：{receipt.get('receipt_at', '')}",
        f"- 通道：{receipt.get('channel', 'stable')}",
        f"- 下载站：{receipt.get('base_url', '')}",
        f"- 代码签名：{'已签名（' + receipt.get('sign_source', '') + '）' if receipt.get('signed') else '未签名（SmartScreen 会提示未知发布者）'}",
        f"- 安装包：{'随包含 .exe 安装包' if receipt.get('with_installer') else '仅便携版/分发包'}",
        f"- 组件：{comp.get('count', 0)} 个，全量约 {comp.get('total_human', '?')}",
    ]
    if receipt.get("mirrors"):
        lines.append(f"- 镜像：{', '.join(receipt['mirrors'])}")
    if eds:
        lines += ["", "## 各档位全量下载", "", "| 档位 | 全量下载 |", "| --- | --- |"]
        for ed, b in eds.items():
            lines.append(f"| {ed} | {_human(b)} |")
    inc = _per_edition_increment(ver, cfg.get("prev_manifest", ""))
    if inc:
        lines += ["", "## 老用户升级下载（相对上一版）", "",
                  "| 档位 | 升级下载 | 全量 | 省下 |", "| --- | --- | --- | --- |"]
        for ed, (i, full) in inc.items():
            saved = f"{(1 - i / full) * 100:.0f}%" if full else "-"
            lines.append(f"| {ed} | {_human(i)} | {_human(full)} | {saved} |")
    lines += ["", "## 安装", "",
              "1. 下载对应档位安装包/便携版；", "2. 首次运行按向导完成环境与模型部署；",
              "3. 用启动器「一键验收」核对 11 项交付标准。", "",
              "_本说明由 publish_release.py 自动生成。_", ""]
    md = "\n".join(lines)
    LOGS.mkdir(parents=True, exist_ok=True)
    out = LOGS / f"RELEASE_NOTES_{ver}.md"
    out.write_text(md, encoding="utf-8")
    pub = DIST / "publish" / ver
    if pub.exists():
        shutil.copy2(out, pub / "RELEASE_NOTES.md")
    print(f"[notes] 已生成发布说明：{out}")
    return out


def _write_sha256sums(ver: str) -> Path | None:
    """生成 dist/publish/<ver>/SHA256SUMS.txt（标准 `<hash>  <相对路径>` 格式，可 `sha256sum -c` 批量校验）。
    优先复用 release_index.json 里已算好的 sha256，缺失才本地补算，几乎零成本。"""
    from make_release import _sha256
    pub = DIST / "publish" / ver
    idx = pub / "release_index.json"
    if not idx.exists():
        print("[sums] 跳过：未找到 release_index.json。")
        return None
    files = json.loads(idx.read_text(encoding="utf-8")).get("files", [])
    # 下载负载（来自 release_index，优先复用其 sha256）+ 随包交付的人读凭据文档（本地补算）。
    rel2hash: dict[str, str] = {}
    for e in files:
        rel = e.get("remote", "")
        if not rel or not e.get("present", False):
            continue
        f = pub / rel
        if not f.exists():
            continue
        rel2hash[rel] = (e.get("sha256") or "").strip() or _sha256(f)
    for doc in ("RELEASE_NOTES.md", "SIGNOFF.md", "UPLOAD.md"):   # 纳入完整性证据链（须在 sums 之前生成）
        f = pub / doc
        if f.exists() and doc not in rel2hash:
            rel2hash[doc] = _sha256(f)
    if not rel2hash:
        print("[sums] 跳过：publish 树无可校验文件。")
        return None
    lines = [f"{rel2hash[rel]}  {rel}" for rel in sorted(rel2hash)]
    out = pub / "SHA256SUMS.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[sums] 已生成校验清单：{out}（{len(lines)} 个文件，含 RELEASE_NOTES/SIGNOFF）")
    return out


def _selfcheck_sha256sums(ver: str) -> int:
    """发布末尾自检：逐行核对刚生成的 SHA256SUMS.txt 与 publish 树实际文件一致（等价本机 `sha256sum -c`）。
    捕捉清单生成 bug / 写盘损坏，确保上传前清单本身可信。返回 0=通过，非 0=有问题。"""
    from make_release import _sha256
    pub = DIST / "publish" / ver
    sums = pub / "SHA256SUMS.txt"
    if not sums.exists():
        return 0
    bad = 0
    for ln in sums.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split(None, 1)
        if len(parts) != 2:
            continue
        exp, rel = parts[0].strip().lower(), parts[1].strip()
        f = pub / rel
        if not f.exists():
            print(f"    ✗ SHA256SUMS 自检：{rel} 不存在")
            bad += 1
        elif _sha256(f).lower() != exp:
            print(f"    ✗ SHA256SUMS 自检：{rel} 哈希不符")
            bad += 1
    if bad:
        print(f"[sums] 本地自检不通过：{bad} 项与清单不符（请勿上传）。")
    else:
        print("[sums] 本地自检通过：清单与 publish 树逐文件一致。")
    return 7 if bad else 0


def _signtool() -> str | None:
    """定位 signtool.exe（PATH → Windows Kits），与 sign_artifacts.bat 同源。"""
    import glob as _glob
    s = shutil.which("signtool")
    if s:
        return s
    pf = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    hits = sorted(_glob.glob(os.path.join(pf, r"Windows Kits\10\bin\*\x64\signtool.exe")), reverse=True)
    return hits[0] if hits else None


def _verify_signature(path: Path) -> dict:
    """best-effort：对已签名 .exe 跑 `signtool verify /pa /v`，解析签发主体与证书指纹。
    供 SIGNOFF 留痕；signtool 缺失或未签名时返回 {verified:False}。"""
    st = _signtool()
    if not st or not path.exists():
        return {"verified": False, "reason": "signtool 不可用" if not st else "文件不存在"}
    try:
        r = subprocess.run([st, "verify", "/pa", "/v", str(path)],
                           capture_output=True, text=True, errors="replace")
    except Exception as e:
        return {"verified": False, "reason": f"verify 异常：{e}"}
    out = (r.stdout or "") + (r.stderr or "")
    subject = thumb = ""
    for ln in out.splitlines():
        s = ln.strip()
        low = s.lower()
        if not subject and low.startswith("issued to:"):
            subject = s.split(":", 1)[1].strip()
        elif not thumb and ("sha1 hash:" in low or low.startswith("hash:")):
            thumb = s.split(":", 1)[1].strip()
    return {"verified": r.returncode == 0, "subject": subject, "thumbprint": thumb,
            "timestamped": "timestamp" in out.lower() or "时间戳" in out}


def _write_signoff(cfg: dict, receipt: dict) -> Path | None:
    """生成可审计签收单 SIGNOFF.md（随包放进 publish 树）：发布身份 + 已签名产物指纹 +
    组件清单引用 + 自检结论 + 凭据出处 + 人工签收勾选项。交付方一眼可审、可追溯。"""
    from make_release import _human, _sha256
    ver = str(cfg["version"])
    pub = DIST / "publish" / ver
    if not pub.exists():
        print("[signoff] 跳过：publish 树不存在。")
        return None
    comp = receipt.get("components", {})
    signed = receipt.get("signed")

    # 顶层已签名产物（启动器 exe + 安装包）的指纹与校验和
    arts = []
    cand = [DIST / "AvatarHub.exe"]
    cand += sorted(DIST.glob("AvatarHub-Setup-*.exe"))
    for f in cand:
        if not f.exists():
            continue
        sig = _verify_signature(f) if signed else {"verified": False, "reason": "未配置签名"}
        arts.append((f.name, f.stat().st_size, _sha256(f), sig))

    # 本版最新 publish 回执文件名（凭据出处）
    rcpt_name = ""
    cands = sorted(LOGS.glob(f"release_receipt_{ver}_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    cands = [p for p in cands if not p.name.endswith("_verify-remote.json")]
    if cands:
        rcpt_name = cands[0].name

    L = [
        f"# AvatarHub {ver} 发布签收单（SIGNOFF）", "",
        "> 本单由 `publish_release.py` 自动生成，汇总本次发布的身份、签名指纹、组件校验和与自检结论，供交付方审阅与归档。", "",
        "## 1. 发布身份", "",
        f"- 项目：AvatarHub",
        f"- 版本：`{ver}`　通道：`{receipt.get('channel', 'stable')}`　架构：`{receipt.get('arch_tag', '')}`",
        f"- 下载站：{receipt.get('base_url', '')}",
        f"- 构建主机：`{receipt.get('host', '')}`　发布时间：`{receipt.get('receipt_at', '')}`",
        f"- 代码签名：{'是' if signed else '否（SmartScreen 将提示未知发布者）'}"
        + (f"（来源：{receipt.get('sign_source', '')}）" if signed else ""),
    ]
    if receipt.get("mirrors"):
        L.append(f"- 镜像：{', '.join(receipt['mirrors'])}")

    L += ["", "## 2. 已签名产物指纹", ""]
    if arts:
        L += ["| 产物 | 体积 | SHA256 | 签名 | 签发主体 | 证书指纹(SHA1) |",
              "| --- | --- | --- | --- | --- | --- |"]
        for name, size, sha, sig in arts:
            vmark = "✓ 已验证" if sig.get("verified") else ("✗ " + sig.get("reason", "未签名") if not signed else "✗ 未通过")
            L.append(f"| {name} | {_human(size)} | `{sha[:16]}…` | {vmark} | {sig.get('subject', '') or '-'} | {sig.get('thumbprint', '') or '-'} |")
        L.append("")
        L.append("> 完整 SHA256 见同目录 `SHA256SUMS.txt`（可 `sha256sum -c SHA256SUMS.txt` 批量校验）与 `release_index.json`（逐组件）。单文件校验：`certutil -hashfile <文件> SHA256`。")
    else:
        L.append("（未发现顶层 .exe 产物。）")

    L += ["", "## 3. 分发组件", "",
          f"- 组件数：{comp.get('count', 0)} 个，全量约 {comp.get('total_human', '?')}",
          f"- 清单：`manifest.json`、`release_index.json`（逐组件 remote/size/sha256）",
          f"- 完整性校验：`SHA256SUMS.txt`（`sha256sum -c` 批量核验整包）",
          f"- 上传指引：`UPLOAD.md`　发布说明：`RELEASE_NOTES.md`",
          "", "## 4. 自检结论", "",
          f"- 母机预检：通过（构建前已过 `make_release --preflight`）",
          f"- 发布后本地自检：{receipt.get('local_smoke', '-')}（组件齐全 + 体积"
          + ("+sha256" if receipt.get('verify_hash') else "") + "一致）",
          f"- 远端冒烟：上传后执行 `python publish_release.py --verify-remote`；通过后回写至本版 `logs/release_receipt_*.json` 的 `remote_verified`（含远端 SHA256SUMS 与本地一致性校验）",
          "", "## 5. 凭据出处", "",
          f"- 发布回执：`logs/{rcpt_name}`" if rcpt_name else "- 发布回执：见 `logs/release_receipt_*.json`",
          f"- 发布台账：`logs/release_history.csv`",
          "", "## 6. 人工签收（发布前核对）", "",
          "- [ ] 版本号 / 通道 / 下载站 URL 正确",
          "- [ ] 代码签名已验证（上表签名列为「✓ 已验证」）" if signed else "- [ ] 已知未签名，确认可接受 SmartScreen 告警或补签后再发",
          "- [ ] 发布后本地自检 PASS",
          "- [ ] 已按 `UPLOAD.md` 上传，并 `--verify-remote` 远端冒烟通过",
          "- [ ] 发布说明 `RELEASE_NOTES.md` 已审阅",
          "",
          f"签收人：________________　日期：________________",
          "", "_本签收单由 publish_release.py 自动生成。_", ""]

    out = pub / "SIGNOFF.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"[signoff] 已生成发布签收单：{out}")
    return out


def _write_receipt(cfg: dict, phase: str, extra: dict) -> Path:
    """写发布回执到 logs/release_receipt_<ver>_<ts>[_phase].json，作交付凭据。"""
    from make_release import _human
    LOGS.mkdir(parents=True, exist_ok=True)
    ver = str(cfg["version"])
    ts = datetime.now(timezone.utc).astimezone()
    sign = cfg.get("sign", {}) or {}
    summ = _index_summary(ver)
    receipt = {
        "project": "AvatarHub", "phase": phase, "version": ver,
        "channel": cfg.get("channel", "stable"), "base_url": cfg.get("base_url", ""),
        "arch_tag": cfg.get("arch_tag", "cu128"),
        "with_installer": bool(cfg.get("with_installer")),
        "signed": _sign_source(sign) != "none", "sign_source": _sign_source(sign),
        "mirrors": cfg.get("mirrors", []) or [],
        "components": {"count": summ["count"], "total_bytes": summ["total_bytes"],
                       "total_human": _human(summ["total_bytes"])},
        "assembled_at": summ["assembled_at"],
        "receipt_at": ts.isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        **extra,
    }
    suffix = "" if phase == "publish" else f"_{phase}"
    out = LOGS / f"release_receipt_{ver}_{ts.strftime('%Y%m%d_%H%M%S')}{suffix}.json"
    out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[receipt] 已写发布回执：{out}")
    _append_ledger(receipt)
    return receipt


def _run(cmd, env=None, shell_bat=False) -> int:
    print(f"\n[publish] 运行：{' '.join(cmd) if not shell_bat else cmd}")
    if shell_bat:
        return subprocess.call(["cmd", "/c", cmd], cwd=str(HERE), env=env)
    return subprocess.call(cmd, cwd=str(HERE), env=env)


def _make_release_argv(cfg: dict) -> list:
    argv = [sys.executable, "make_release.py", "--ci", "--build-packs",
            "--version", str(cfg["version"]),
            "--base-url", str(cfg["base_url"]).rstrip("/"),
            "--arch-tag", str(cfg.get("arch_tag", "cu128")),
            "--channel", str(cfg.get("channel", "stable"))]
    if cfg.get("shared"):
        argv.append("--shared")
    if cfg.get("include_models"):
        argv.append("--include-models")
    if cfg.get("with_installer"):
        argv.append("--with-installer")
    for m in cfg.get("mirrors", []) or []:
        argv += ["--mirror", str(m)]
    if str(cfg.get("site_root", "")).strip():
        argv += ["--site-root", str(cfg["site_root"]).strip()]
    if str(cfg.get("prev_manifest", "")).strip():
        argv += ["--prev-manifest", str(cfg["prev_manifest"]).strip()]
    if cfg.get("smoke_envs"):
        argv += ["--smoke-envs", *[str(e) for e in cfg["smoke_envs"]]]
    if str(cfg.get("telemetry_url", "")).strip():
        argv += ["--telemetry-url", str(cfg["telemetry_url"]).strip()]
    return argv


def _print_plan(cfg: dict, signing: bool):
    ver = str(cfg["version"])
    print("\n[plan] 将按以下顺序执行（任一闸门失败即停）：")
    print("  0. make_release.py --preflight   （母机预检：磁盘/显存/conda-pack/环境/ISCC）")
    print("  1. build_launcher.bat            （构建启动器 exe）")
    print(f"  2. sign_artifacts.bat            （{'签 exe' if signing else '跳过：未配置证书'}）")
    print(f"  3. {' '.join(_make_release_argv(cfg))}")
    print(f"  4. sign_artifacts.bat            （{'签安装包' if signing and cfg.get('with_installer') else '跳过'}）")
    print(f"  5. 本地自检 release_index.json   （组件存在 + 体积{'+sha256' if cfg.get('verify_hash') else ''}）")
    print("  6. 写发布回执 + 发布说明 + 可审计签收单 SIGNOFF.md + 校验清单 SHA256SUMS.txt(含前两文档) （交付凭据，随包交付）")


def local_smoke(ver: str, verify_hash: bool = False) -> int:
    """发布即自检：核对 dist/publish/<ver>/release_index.json 里每个组件本地存在、体积一致（可选 sha256）。"""
    from make_release import _human, _sha256
    pub = DIST / "publish" / ver
    idx = pub / "release_index.json"
    if not idx.exists():
        print(f"[smoke] 跳过本地自检：未找到 {idx}")
        return 0
    files = json.loads(idx.read_text(encoding="utf-8")).get("files", [])
    problems, checked = [], 0
    for e in files:
        rel = e.get("remote", "")
        f = pub / rel
        if not e.get("present", False):
            problems.append(f"{rel}：清单标记缺失（present=false）")
            continue
        if not f.exists():
            problems.append(f"{rel}：publish 树中不存在")
            continue
        exp = int(e.get("size_bytes", 0) or 0)
        got = f.stat().st_size
        if exp and got != exp:
            problems.append(f"{rel}：体积不符（期望 {_human(exp)}，实际 {_human(got)}）")
            continue
        if verify_hash and e.get("sha256"):
            if _sha256(f) != e["sha256"]:
                problems.append(f"{rel}：sha256 不符")
                continue
        checked += 1
    if problems:
        print(f"[smoke] 本地自检不通过（{len(problems)} 项）：")
        for p in problems:
            print(f"    ✗ {p}")
        return 7
    print(f"[smoke] 本地自检通过：{checked} 个组件齐全、体积一致" + ("、sha256 一致" if verify_hash else "") + "。")
    return 0


def _remote_size(url: str, timeout: int = 20) -> int | None:
    """返回远端文件字节数；HEAD 不行则用 Range GET 探测；不可达返回 None。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cl = r.headers.get("Content-Length")
            if cl is not None:
                return int(cl)
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cr = r.headers.get("Content-Range", "")
            if "/" in cr:
                return int(cr.rsplit("/", 1)[1])
            cl = r.headers.get("Content-Length")
            return int(cl) if cl is not None else 0
    except Exception:
        return None


def _verify_remote_sums(base: str, ver: str) -> dict:
    """拉远端 SHA256SUMS.txt 与本地逐行比对（覆盖下载负载 + RELEASE_NOTES + SIGNOFF）。
    返回 {status: PASS/FAIL/SKIP, match, mismatch, extra_remote, reason}。无本地清单则 SKIP。"""
    local = DIST / "publish" / ver / "SHA256SUMS.txt"
    if not local.exists():
        return {"status": "SKIP", "reason": "本地无 SHA256SUMS.txt"}
    try:
        with urllib.request.urlopen(f"{base}/SHA256SUMS.txt", timeout=20) as r:
            remote_txt = r.read().decode("utf-8")
    except Exception as e:
        return {"status": "FAIL", "reason": f"远端 SHA256SUMS.txt 不可达：{e}"}

    def _parse(t: str) -> dict:
        d = {}
        for ln in t.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split(None, 1)          # "<hash>  <path>"
            if len(parts) == 2:
                d[parts[1].strip()] = parts[0].strip().lower()
        return d

    L = _parse(local.read_text(encoding="utf-8"))
    R = _parse(remote_txt)
    match = sum(1 for p, h in L.items() if R.get(p) == h)
    mismatch = sorted(p for p, h in L.items() if R.get(p) != h)
    extra = sorted(p for p in R if p not in L)
    return {"status": "PASS" if (not mismatch and not extra) else "FAIL",
            "match": match, "mismatch": mismatch, "extra_remote": extra}


def verify_remote(cfg: dict, sample: int = 0) -> int:
    """上传后冒烟：GET base_url/manifest.json 可达，再 HEAD 校验各组件可达 + 体积一致。

    sample>0 时仅抽检前 N 个组件（组件极多时秒级验证）。
    """
    from make_release import _human
    base = str(cfg["base_url"]).rstrip("/")
    ver = str(cfg["version"])
    print(f"[remote] 校验下载站：{base}")
    man_url = f"{base}/manifest.json"
    try:
        with urllib.request.urlopen(man_url, timeout=20) as r:
            remote_manifest = json.loads(r.read().decode("utf-8"))
        print(f"[remote] ✓ manifest.json 可达且可解析：{man_url}")
    except Exception as e:
        print(f"[remote] ✗ manifest.json 不可达/解析失败：{man_url}（{e}）")
        _write_receipt(cfg, "verify-remote",
                       {"remote": "FAIL", "reason": "manifest unreachable", "checked": 0, "bad": 1})
        return 8

    # 远端 manifest 验签：客户端就是用钉死公钥验这份；上传后立刻同款验一遍，
    # 杜绝"传上去的清单没签/签坏/被中途改写"（防降级客户端会拒，等于该版发布哑火）。
    if cfg.get("sign_release", True):
        try:
            import release_sign
            ok, why = release_sign.verify_manifest(remote_manifest)
            if ok:
                app = (remote_manifest.get("components", {}).get("app", {}) or {}).get("core", {})
                r = app.get("rollout") or {}
                print(f"[remote] ✓ 远端清单验签通过；app 灰度 percent={r.get('percent','-')} halted={bool(r.get('halted'))}")
            else:
                print(f"[remote] ✗ 远端清单验签未过：{why}（客户端会拒绝此清单，勿放量）")
                _write_receipt(cfg, "verify-remote",
                               {"remote": "FAIL", "reason": "signature", "checked": 0, "bad": 1})
                return 8
        except Exception as e:
            print(f"[remote] · 验签跳过（release_sign 不可用：{e}）")

    idx = DIST / "publish" / ver / "release_index.json"
    if not idx.exists():
        print(f"[remote] 无本地 release_index.json，仅校验了 manifest 可达。")
        _write_receipt(cfg, "verify-remote", {"remote": "PASS(manifest-only)", "checked": 0, "bad": 0})
        return 0
    files = [e for e in json.loads(idx.read_text(encoding="utf-8")).get("files", [])
             if e.get("remote") and e.get("remote") != "manifest.json"]
    total = len(files)
    if sample and sample > 0:
        files = files[:sample]
        print(f"[remote] 抽样模式：仅校验前 {len(files)}/{total} 个组件。")

    def _check(e):
        rel = e["remote"]
        exp = int(e.get("size_bytes", 0) or 0)
        return rel, exp, _remote_size(f"{base}/{rel}")

    bad = 0
    workers = min(8, max(1, len(files)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_check, files))     # 并发探测，保持清单顺序输出
    for rel, exp, got in results:
        if got is None:
            print(f"    ✗ 不可达：{rel}")
            bad += 1
        elif exp and got != exp:
            print(f"    ✗ 体积不符：{rel}（期望 {_human(exp)}，远端 {_human(got)}）")
            bad += 1
        else:
            print(f"    ✓ {rel}  {_human(got)}")
    # 远端 SHA256SUMS 与本地一致性校验（覆盖下载负载 + RELEASE_NOTES + SIGNOFF）
    sums = _verify_remote_sums(base, ver)
    if sums["status"] == "PASS":
        print(f"    ✓ SHA256SUMS 远端与本地一致（{sums['match']} 个文件）")
    elif sums["status"] == "SKIP":
        print(f"    · SHA256SUMS 比对跳过：{sums.get('reason', '')}")
    else:
        _why = sums.get("reason") or f"不符 {sums.get('mismatch')}；远端多出 {sums.get('extra_remote')}"
        print(f"    ✗ SHA256SUMS 不一致：{_why}")
    sums_bad = 1 if sums["status"] == "FAIL" else 0

    rc = 8 if (bad or sums_bad) else 0
    _write_receipt(cfg, "verify-remote",
                   {"remote": "FAIL" if (bad or sums_bad) else "PASS", "checked": len(files),
                    "bad": bad, "component_total": total,
                    "sampled": bool(sample and sample > 0),
                    "sha256sums": sums["status"],
                    "sha256sums_detail": {k: sums[k] for k in ("match", "mismatch", "extra_remote", "reason")
                                          if k in sums}})
    if bad or sums_bad:
        print(f"[remote] 冒烟不通过：组件异常 {bad}/{len(files)}，SHA256SUMS {sums['status']}。")
        return 8
    print(f"[remote] 冒烟通过：manifest + {len(files)} 个组件均可达、体积一致"
          + ("、SHA256SUMS 一致" if sums["status"] == "PASS" else "") + "。")
    _mark_remote_verified(ver)   # 受签收的 SIGNOFF 保持不可变；远端已验证状态记入回执 JSON
    return rc


def _stamp_and_sign_manifests(ver: str, cfg: dict) -> int:
    """给 publish 树 + dist 的 manifest 打灰度戳（app 组件）并 Ed25519 验签，随后验签自检。
    覆盖两处：dist/publish/<ver>/manifest.json（上传用）与 dist/manifest.json（本地一致）。"""
    try:
        import release_sign
    except Exception as e:
        print(f"[publish] release_sign 不可用（{e}）——无法签名。装 cryptography 或置 sign_release=false。")
        return 1
    pct = cfg.get("rollout_percent", 100)
    try:
        pct = max(0, min(100, int(pct)))
    except Exception:
        pct = 100
    tele_url = str(cfg.get("telemetry_url", "") or "").strip()
    tele_tok = str(cfg.get("telemetry_token", "") or "").strip()
    targets = [DIST / "publish" / ver / "manifest.json", DIST / "manifest.json"]
    signed_any = False
    for mp in targets:
        if not mp.exists():
            continue
        m = json.loads(mp.read_text(encoding="utf-8"))
        # 灰度戳：仅 app 组件（模型/环境不灰度，全量下发）
        app = (m.get("components", {}).get("app", {}) or {}).get("core")
        if app is not None:
            r = dict(app.get("rollout") or {})
            r.update({"percent": pct, "halted": False})
            app["rollout"] = r
        if tele_url:
            m["telemetry_url"] = tele_url
        if tele_tok:
            m["telemetry_token"] = tele_tok
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        fp = release_sign.sign_manifest_file(mp)
        # 验签自检：签完立刻验，杜绝"签了但验不过"（规范化不一致等）就上传
        ok, why = release_sign.verify_manifest(json.loads(mp.read_text(encoding="utf-8")))
        print(f"[publish] {mp.relative_to(HERE)}: 灰度 {pct}% + 签名(指纹 {fp}) → 验签 {'OK' if ok else 'FAIL: ' + why}")
        if not ok:
            return 1
        signed_any = True
    if not signed_any:
        print("[publish] 未找到可签名的 manifest（publish 树缺失？）")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AvatarHub 一键发布编排（读 release.config.json）")
    ap.add_argument("--dry-run", action="store_true",
                    help="预演：校验配置+母机预检+打印计划，不构建/不签名/不发布")
    ap.add_argument("--verify-remote", action="store_true",
                    help="上传后冒烟：拉 base_url/manifest.json 并 HEAD 校验各组件可达+体积")
    ap.add_argument("--hash", action="store_true",
                    help="发布后本地自检对每个组件做 sha256（慢，覆盖配置 verify_hash）")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="配合 --verify-remote：仅抽检前 N 个组件（组件极多时秒级验证）")
    args = ap.parse_args()

    cfg = load_config()
    sign_env = _sign_env(cfg.get("sign", {}) or {})
    signing = sign_env is not None
    ver = str(cfg["version"])
    verify_hash = bool(args.hash or cfg.get("verify_hash"))

    print("=" * 64)
    print(" AvatarHub 一键发布编排（publish_release）")
    print("=" * 64)
    print(f" 版本：{ver}　通道：{cfg.get('channel', 'stable')}　下载站：{cfg['base_url']}")
    print(f" 安装包：{'是' if cfg.get('with_installer') else '否'}　签名：{'是' if signing else '否（SmartScreen 将告警）'}")

    # 上传后冒烟：独立命令，不构建
    if args.verify_remote:
        return verify_remote(cfg, sample=args.sample)

    # 0) 母机预检前置（磁盘/显存/conda-pack/环境/ISCC）——失败即停，避免白构建 exe
    pf = [sys.executable, "make_release.py", "--preflight", "--version", ver]
    if cfg.get("with_installer"):
        pf.append("--with-installer")
    if _run(pf) != 0:
        _die("母机发布预检未通过（磁盘/显存/conda-pack/环境/ISCC）。按上方提示修复后重试。", 2)

    # 预演：到此打印计划即退出，不做任何构建/签名/发布
    if args.dry_run:
        _print_plan(cfg, signing)
        print("\n[dry-run] 预检通过、配置就绪。去掉 --dry-run 即可正式发布。")
        return 0

    # 1) 构建启动器 exe（最新代码）
    if _run("build_launcher.bat", shell_bat=True) != 0:
        _die("启动器 exe 构建失败。", 3)
    if not (DIST / "AvatarHub.exe").exists():
        _die("未生成 dist\\AvatarHub.exe。", 3)

    # 2) 先签 exe（安装包将嵌入【已签名】的 exe；此时安装包尚未生成，会被自动跳过）
    if signing:
        if _run("sign_artifacts.bat", env=sign_env, shell_bat=True) != 0:
            _die("exe 签名失败（检查证书与 signtool）。", 4)

    # 3) 门禁发布流水线（预检→构建包/便携/安装包→烟测→装配 publish→增量/矩阵），失败即停
    rc = _run(_make_release_argv(cfg))
    if rc != 0:
        _die(f"发布流水线未通过（make_release 返回 {rc}），已中止、未产出可发布树。", rc)

    # 3.5) 发布清单打灰度戳 + Ed25519 验签（供应链防篡改）——在 SHA256SUMS/自检之前，
    #      于是校验清单与上传负载都覆盖【已签名】manifest。默认强制；关闭需显式 sign_release=false。
    if cfg.get("sign_release", True):
        if _stamp_and_sign_manifests(ver, cfg) != 0:
            _die("发布清单签名/验签未通过，已中止（勿上传未签名清单：客户端防降级会拒）。", 5)

    # 4) 再签安装包（exe 上一步已签，这步签 dist\AvatarHub-Setup-*.exe）
    if signing and cfg.get("with_installer"):
        if _run("sign_artifacts.bat", env=sign_env, shell_bat=True) != 0:
            _die("安装包签名失败。", 4)

    # 5) 发布即自检：核对 publish 树组件齐全、体积一致（可选 sha256）
    if local_smoke(ver, verify_hash) != 0:
        _write_receipt(cfg, "publish", {"local_smoke": "FAIL", "verify_hash": verify_hash})
        _die("发布后本地自检未通过：publish 树不完整，请勿上传。", 7)

    # 6) 写发布回执 + 人读发布说明（随包交付）+ 台账登记 + 可审计签收单
    receipt = _write_receipt(cfg, "publish", {"local_smoke": "PASS", "verify_hash": verify_hash})
    _write_release_notes(cfg, receipt)
    _write_signoff(cfg, receipt)
    _write_sha256sums(ver)   # 最后生成：覆盖下载负载 + RELEASE_NOTES + SIGNOFF + UPLOAD（须在其后）
    if _selfcheck_sha256sums(ver) != 0:
        _die("SHA256SUMS 本地自检未通过：校验清单与 publish 树不一致，请勿上传。", 7)

    pub = DIST / "publish" / ver
    print("\n" + "=" * 64)
    print(" 发布编排完成 ✓")
    print(f" 产物：{pub}")
    print(f" 上传指引：{pub / 'UPLOAD.md'}")
    if not signing:
        print(" 提示：未签名——正式对外前建议配置证书重发，避免 SmartScreen「未知发布者」。")
    print(" 下一步：按 UPLOAD.md 上传到下载站，再跑：python publish_release.py --verify-remote")
    return 0


if __name__ == "__main__":
    sys.exit(main())
