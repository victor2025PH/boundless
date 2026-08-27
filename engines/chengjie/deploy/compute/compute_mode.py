#!/usr/bin/env python3
"""compute_mode.py — 智聊算力模式工具（ai.primary 的薄壳，README 同目录）。

⛔ 2026-08-22 老板指令：``local`` / ``local_only`` 档**已从本工具移除**——
   2026-08-21 05:49 老板令主链回 cloud 后数小时内被「切回 local_only」类自动化
   翻回，直接导致 08-22 凌晨客服链三连 45s 超时静默不回。现：
   - 主链档位由 ``ai.primary_lock``（老板锁）治理，引擎装载点强制执行 + 全程审计
     （src/ai/ai_primary_audit.py，台账=实例 logs/ai_primary_audit.jsonl）；
   - 本工具只保留 status / cloud（回滚方向）/ prep / cleanup——**只能向云端方向走**；
   - 恢复本地档＝老板拍板 → overlay 改 ai.primary_lock → 治理接口切换，
     任何执行器不得自动切回。

用法（默认 dry-run 只打印 diff，--apply 才落盘；绝不重启进程）：

    python deploy/compute/compute_mode.py status
    python deploy/compute/compute_mode.py prep --apply     # 主链不动，兜底+改写换血
    python deploy/compute/compute_mode.py cloud --apply    # 回滚档（唯一可写的主链档）
    python deploy/compute/compute_mode.py cleanup --apply [--cut-imagegen] [--cut-fatex]

模式语义（与 src/ai/ai_client.py 的 ai.primary 完全同口径）：
    cloud      云主链（DeepSeek）→ key 池 → 本地兜底 → canned（回滚档）
    prep       主链仍 cloud；仅把 ai.fallback + 口语化改写指到 vLLM，并摘 173 死端点

cleanup 摘除 173 旧 Ollama/CosyVoice 死引用。
写入走 ruamel round-trip 保注释（repo 既有约定，见 overlay 写入纪律）；
--apply 落盘同步追加审计行（via=compute_mode_cli）。
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# 本工具可用档位（2026-08-22 起 local/local_only 永久移除，见模块头）
MODES = ["status", "prep", "cloud", "cleanup"]
# 主链锁定后，唯一还会写 ai.primary 的档就是 cloud；锁与它不符时拒跑（见 main）
REMOVED_MODES = ("local", "local_only")

# ── 契约常量（与 deploy/compute/README.md「统一契约」段同源，改这里先改 README）──
VLLM_BASE = "http://192.168.0.173:8001/v1"
VLLM_MODEL = "chatx"
VLLM_API_KEY = "vllm"          # vLLM 未开 --api-key，任意非空即可（OpenAI SDK 要求非空）
COLLOQUIAL_TIMEOUT_SEC = 6.0
DEFAULT_OVERLAY = Path(r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml")

DEAD_OLLAMA_173 = "http://192.168.0.173:11434"
DEAD_COSY_173 = "http://192.168.0.173:7852"


def _load_yaml(path: Path):
    try:
        from ruamel.yaml import YAML
    except ImportError:
        sys.exit("需要 ruamel.yaml（引擎依赖内已有）：pip install ruamel.yaml")
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    with path.open("r", encoding="utf-8") as f:
        return yaml, yaml.load(f)


def _dump_str(yaml, data) -> str:
    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def _probe_vllm(timeout: float = 4.0) -> str:
    try:
        with urllib.request.urlopen(VLLM_BASE + "/models", timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
        return "UP" if VLLM_MODEL in body else f"UP_BUT_NO_{VLLM_MODEL}"
    except Exception as exc:  # noqa: BLE001 - 状态探针，任何失败都归一为 DOWN
        return f"DOWN ({type(exc).__name__})"


def _ensure_map(parent, key):
    node = parent.get(key)
    if not isinstance(node, dict):
        node = {}
        parent[key] = node
    return node


def apply_mode(data, mode: str) -> list[str]:
    """把 overlay 数据结构改成目标模式，返回人读得懂的变更清单。

    ``prep``＝不动 ai.primary（保持 cloud 主链），只做 vLLM 接线。
    ``local``/``local_only`` 已按老板指令（2026-08-22）移除——传入即抛
    ValueError，绝不静默降级成别的档。
    """
    if mode in REMOVED_MODES:
        raise ValueError(
            "local/local_only 档已按老板指令（2026-08-22）从本工具移除："
            "主链锁定 cloud（ai.primary_lock），恢复本地档需老板拍板解锁，"
            "任何执行器不得自动切回。")
    notes: list[str] = []
    ai = _ensure_map(data, "ai")

    if mode == "cloud":
        old_primary = str(ai.get("primary") or "cloud")
        if old_primary != mode:
            ai["primary"] = mode
            notes.append(f"ai.primary: {old_primary} -> {mode}")

    if mode == "prep":
        fb = _ensure_map(ai, "fallback")
        changes = {
            "enabled": True,
            "base_url": VLLM_BASE,
            "model": VLLM_MODEL,
            "api_key": VLLM_API_KEY,
        }
        for k, v in changes.items():
            if fb.get(k) != v:
                notes.append(f"ai.fallback.{k}: {fb.get(k)!r} -> {v!r}")
                fb[k] = v
        # keep_alive/num_ctx/think 是 Ollama 原生口专用键，/v1 路径下被忽略——留着无害，不动。

        av = data.get("avatar_voice")
        if isinstance(av, dict):
            col = av.get("colloquial")
            if isinstance(col, dict):
                eps = col.get("llm_endpoints")
                if isinstance(eps, list):
                    head = {
                        "base_url": VLLM_BASE,
                        "model": VLLM_MODEL,
                        "api_key": VLLM_API_KEY,
                        "timeout_sec": COLLOQUIAL_TIMEOUT_SEC,
                    }
                    if not eps or eps[0].get("base_url") != VLLM_BASE:
                        # 丢弃指向 173:11434 的死条目，其余（198 备点）保留在后
                        rest = [e for e in eps if DEAD_OLLAMA_173 not in str(e.get("base_url"))]
                        col["llm_endpoints"] = [head] + rest
                        notes.append("colloquial.llm_endpoints[0] -> vLLM（并剔除 173:11434 死条目）")

    if mode == "prep":
        # 死端点是纯延迟税（每个冷却窗过期都先吃一次连接超时），prep 一并摘除
        notes.extend(apply_cleanup(data, cut_imagegen=False, cut_fatex=False))
    return notes


def apply_cleanup(data, cut_imagegen: bool, cut_fatex: bool) -> list[str]:
    notes: list[str] = []

    av = data.get("avatar_voice")
    if isinstance(av, dict):
        urls = av.get("base_urls")
        if isinstance(urls, list) and any(DEAD_COSY_173 in str(u) for u in urls):
            av["base_urls"] = [u for u in urls if DEAD_COSY_173 not in str(u)]
            notes.append(f"avatar_voice.base_urls 摘除 {DEAD_COSY_173}（173 CosyVoice 已停）")

    tr = data.get("translation")
    if isinstance(tr, dict):
        engines = tr.get("engines")
        if isinstance(engines, dict):
            mt = engines.get("ollama_mt")
            if isinstance(mt, dict):
                urls = mt.get("base_urls")
                if isinstance(urls, list):
                    kept = [u for u in urls
                            if DEAD_OLLAMA_173 not in str(u.get("base_url") if isinstance(u, dict) else u)]
                    if len(kept) != len(urls):
                        mt["base_urls"] = kept
                        notes.append(f"translation ollama_mt.base_urls 摘除 {DEAD_OLLAMA_173}")

    comp = data.get("companion")
    if isinstance(comp, dict):
        if cut_imagegen:
            selfie = _ensure_map(comp, "selfie")
            if selfie.get("enabled") is not False:
                selfie["enabled"] = False
                notes.append("companion.selfie.enabled -> false（⚠ 相册与生成共用总闸，一起停）")
        if cut_fatex:
            bazi = _ensure_map(comp, "bazi")
            if bazi.get("enabled") is not False:
                bazi["enabled"] = False
                notes.append("companion.bazi.enabled -> false")
    return notes


def cmd_status(overlay: Path) -> None:
    yaml, data = _load_yaml(overlay)
    ai = data.get("ai") or {}
    fb = ai.get("fallback") or {}
    col = ((data.get("avatar_voice") or {}).get("colloquial") or {})
    eps = col.get("llm_endpoints") or []
    print(f"overlay        : {overlay}")
    print(f"ai.primary     : {ai.get('primary') or 'cloud（未设=默认）'}")
    print(f"云主链         : {ai.get('model')} @ {ai.get('base_url')}")
    print(f"ai.fallback    : {fb.get('model')} @ {fb.get('base_url')} (enabled={fb.get('enabled')})")
    for i, ep in enumerate(eps):
        print(f"colloquial[{i}]  : {ep.get('model')} @ {ep.get('base_url')}")
    print(f"vLLM 探活      : {_probe_vllm()}  ({VLLM_BASE}, model={VLLM_MODEL})")


def _audit_apply(overlay: Path, mode: str, notes: list[str]) -> None:
    """--apply 落盘后追加审计行（与 src/ai/ai_primary_audit.py 同台账同行式；
    本脚本刻意自包含不 import 引擎——它常被外部执行器在任意 CWD 下调用）。"""
    try:
        logs = overlay.resolve().parent.parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": round(time.time(), 3),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "event": "cli_apply",
            "via": "compute_mode_cli",
            "mode": mode,
            "notes": notes[:10],
        }
        with (logs / "ai_primary_audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--apply", action="store_true", help="落盘（默认 dry-run 打印 diff）")
    ap.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    ap.add_argument("--cut-imagegen", action="store_true", help="cleanup 附带：停自拍/出图（含相册总闸）")
    ap.add_argument("--cut-fatex", action="store_true", help="cleanup 附带：停命理")
    args = ap.parse_args()

    if not args.overlay.exists():
        sys.exit(f"overlay 不存在: {args.overlay}")

    if args.mode == "status":
        cmd_status(args.overlay)
        return

    raw_original = args.overlay.read_text(encoding="utf-8")
    yaml, data = _load_yaml(args.overlay)
    before = _dump_str(yaml, data)

    # 老板锁：要动 ai.primary 的档（现仅 cloud）与锁不符 → 拒跑。
    # （引擎装载点还有第二道强制，这里先拒是为了不制造「写了也白写」的假动作。）
    _lock = str(((data.get("ai") or {}).get("primary_lock")) or "").strip().lower()
    if args.mode == "cloud" and _lock and _lock != "cloud":
        sys.exit(f"拒绝：ai.primary_lock={_lock} 在场，与目标档 cloud 不符——"
                 "改锁需老板拍板（见 src/ai/ai_primary_audit.py 模块头）。")

    if args.mode == "cleanup":
        notes = apply_cleanup(data, args.cut_imagegen, args.cut_fatex)
    else:
        if args.mode == "prep":
            probe = _probe_vllm()
            if not probe.startswith("UP"):
                sys.exit(f"拒绝切 {args.mode}：vLLM 探活失败 -> {probe}\n"
                         f"先确认 {VLLM_BASE}/models 可达（173 keepalive/服务状态）。")
        notes = apply_mode(data, args.mode)

    after = _dump_str(yaml, data)
    if before == after:
        print("无变更（overlay 已是目标态）。")
        return

    print("—— 变更清单 ——")
    for n in notes:
        print("  ·", n)
    print("—— diff ——")
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                     "overlay(旧)", "overlay(新)", lineterm=""):
        print(line)

    if not args.apply:
        print("\n[dry-run] 未落盘。确认无误后加 --apply。")
        return

    bak = args.overlay.with_suffix(".yaml.bak_compute_mode")
    bak.write_text(raw_original, encoding="utf-8")
    args.overlay.write_text(after, encoding="utf-8")
    _audit_apply(args.overlay, args.mode, notes)
    print(f"\n已落盘（备份: {bak.name}）。")
    print("下一步（标准重启纪律，勿绕过）：")
    print("  powershell -File scripts\\restart_preflight.ps1")
    print("  powershell -ExecutionPolicy Bypass -File deploy\\instances\\restart_instance.ps1 -Instance zhiliao")


if __name__ == "__main__":
    main()
