# -*- coding: utf-8 -*-
"""音色+韵律双指标周期抽检 — 每个 avatar_clone 人设合成固定探针句量化体检。

两个互补维度（都复用 AvatarHub 集群评分器，CPU 推理不违显存纪律）：
  - **声纹相似度**（clone_scorer/campplus.onnx ~28MB）：抓灾难性音色漂移
    （换错参考音/文件损坏/模型退化）。刻度（2026-07-13 实测 8 样本）：正常带
    0.78~0.86；阈值只设灾难级 <0.70 WARN / <0.60 CRITICAL（exit 1）。
  - **韵律自然度**（prosody_scorer，纯 numpy F0/能量动态 vs 参考音基准）：
    抓「播音腔回归」（声纹分抓不到的那半只眼——12 样本三通道实测该分能把
    instruct2 的韵律模板化量出来：zero_shot 0.955 vs instruct2 0.948，差距小但
    方向一致）。刻度未稳（n=12），先**只收集不告警**，几晚数据后再定阈值。

产物：logs/voice_similarity.jsonl 追加一行/人设（ts/persona/score/naturalness/...），
由 AvatarPrerenderNightly 在渲染+预热后顺带执行。
用法：python -m scripts.voice_similarity_probe [--persona lin_xiaoyu] [--probe-text ...]
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_MFYS = Path("D:/faceX/mfys")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

# 固定探针句：跨日可比（改了探针=趋势断档，勿轻改）
DEFAULT_PROBE = "你好呀，今天过得怎么样？我一直在想你呢。"
WARN_THRESHOLD = 0.70
CRIT_THRESHOLD = 0.60
OUT_JSONL = _ROOT / "logs" / "voice_similarity.jsonl"


def _load_config() -> dict:
    import yaml
    data = yaml.safe_load(
        (_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")) or {}
    local = _ROOT / "config" / "config.local.yaml"
    if local.is_file():
        overlay = yaml.safe_load(local.read_text(encoding="utf-8")) or {}

        def merge(d: dict, s: dict) -> None:
            for k, v in s.items():
                if isinstance(v, dict) and isinstance(d.get(k), dict):
                    merge(d[k], v)
                else:
                    d[k] = v
        merge(data, overlay)
    return data


def classify_score(score: float, *, warn: float = WARN_THRESHOLD,
                   crit: float = CRIT_THRESHOLD) -> str:
    """相似度 → ok/warn/critical（纯函数，供单测）。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "critical"
    if s < crit:
        return "critical"
    if s < warn:
        return "warn"
    return "ok"


# 自然度告警自动校准：历史样本 ≥ 此数才启用（刻度稳了才告警，防冷启动误报）
NAT_MIN_SAMPLES = 15
NAT_FLOOR_MARGIN = 0.05


def calibrate_naturalness_floor(
    rows: list, *, min_n: int = NAT_MIN_SAMPLES, margin: float = NAT_FLOOR_MARGIN,
) -> float:
    """由历史 jsonl 行自动校准自然度告警下限（p10 - margin）。

    数据不足（< min_n 个有效样本）→ 返回 0.0＝不告警只收集（自动到期启用：
    夜间任务每晚攒 2 个音色样本，约一周后自动开始守门）。纯函数。
    只统计生产路径行（``prosody != "off"``）——A/B 对照的固定噪声行天然
    韵律分更低，混入会拉低 p10 造成守门失灵。
    """
    vals = []
    for r in rows or []:
        try:
            if r.get("prosody") == "off":
                continue
            v = r.get("naturalness")
            if v is not None:
                vals.append(float(v))
        except (TypeError, ValueError, AttributeError):
            continue
    if len(vals) < max(2, int(min_n)):
        return 0.0
    vals.sort()
    idx = max(0, int(len(vals) * 0.10) - (1 if len(vals) % 10 == 0 else 0))
    p10 = vals[idx]
    return round(max(0.0, p10 - margin), 3)


def load_history_rows(path: Path, *, max_rows: int = 400) -> list:
    """读 jsonl 尾部历史行（文件小，整读后截尾）。失败 → []。"""
    try:
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows[-max_rows:]
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="音色相似度周期抽检")
    ap.add_argument("--persona", default="", help="只测指定人设（默认全部）")
    ap.add_argument("--probe-text", default=DEFAULT_PROBE)
    ap.add_argument("--warn", type=float, default=WARN_THRESHOLD)
    ap.add_argument("--crit", type=float, default=CRIT_THRESHOLD)
    ap.add_argument("--prosody-ab", choices=["on", "off"], default="on",
                    help="Phase C A/B：每音色额外合成一次固定噪声版对照自然度差")
    args = ap.parse_args()

    if not _MFYS.is_dir():
        print("[!] AvatarHub 目录不存在（非 TTS 节点机），跳过")
        return 0
    sys.path.insert(0, str(_MFYS))
    try:
        from clone_scorer import score_similarity
    except Exception as ex:
        print(f"[!] clone_scorer 不可用（{ex}），跳过")
        return 0
    naturalness_fn = None
    try:
        from prosody_scorer import naturalness_score as naturalness_fn
    except Exception as ex:
        print(f"[-] prosody_scorer 不可用（{ex}），仅测声纹")

    from scripts.avatar_prerender import _collect_avatar_personas
    from src.ai.avatar_voice import (
        AvatarVoiceClient,
        find_reference_text,
        load_reference_b64,
    )

    cfg = _load_config()
    client = AvatarVoiceClient.from_config(cfg)
    if not client.enabled:
        print("[!] avatar_voice 未启用，跳过")
        return 0
    if not client.ensure_ready(wait_sec=180.0):
        print("[!] 7852 未就绪，跳过")
        return 0

    targets = _collect_avatar_personas(cfg)
    if args.persona:
        targets = [(p, r) for p, r in targets if p == args.persona]
    if not targets:
        print("[!] 无目标人设")
        return 0

    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    # 自然度告警下限：历史数据自动校准（不足 NAT_MIN_SAMPLES 样本=0.0 不告警）
    nat_floor = calibrate_naturalness_floor(load_history_rows(OUT_JSONL))
    if nat_floor > 0:
        print(f"[*] 自然度告警下限（历史 p10-{NAT_FLOOR_MARGIN}）: {nat_floor}")
    else:
        print(f"[*] 自然度刻度校准中（历史样本 <{NAT_MIN_SAMPLES}），只收集不告警")
    worst = "ok"
    order = {"ok": 0, "warn": 1, "critical": 2}
    seen_refs: dict = {}
    flow_temp = float(getattr(client, "flow_temperature", 0) or 0)

    def _nat_of(synth_b64: str, ref_b64: str):
        if naturalness_fn is None:
            return None
        try:
            nrv = naturalness_fn(synth_b64, ref_b64)
            if nrv.get("ok"):
                return float(nrv.get("naturalness") or 0)
        except Exception:
            pass
        return None

    def _append_row(pid: str, *, score, label, nat, prosody: str) -> None:
        row = {
            "ts": time.time(),
            "date": time.strftime("%Y-%m-%d"),
            "persona": pid,
            "score": score,
            "label": label,
            "naturalness": nat,
            "probe": args.probe_text[:40],
            "prosody": prosody,
            "flow_temp": flow_temp if prosody == "on" else 0,
        }
        with OUT_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    for pid, ref_path in targets:
        try:
            # 同参考音只合成一次（多人设共用音色时省 GPU）
            key = str(Path(ref_path).resolve())
            if key in seen_refs:
                score, label, nat, nat_off = seen_refs[key]
            else:
                ref_b64 = load_reference_b64(ref_path)
                ref_text = find_reference_text(ref_path)
                # A/B 对照（先合成 off 组）：固定噪声基线的自然度——量化 Phase C
                # 「fresh CFM noise」到底带来多少韵律增益（写行在前，保证同人设
                # 最新一行恒为生产路径 on 组，旧看板读尾兼容）。
                nat_off = None
                if args.prosody_ab == "on":
                    try:
                        wav_off = client.tts(
                            args.probe_text, reference_audio_b64=ref_b64,
                            reference_text=ref_text, emotion="neutral",
                            prosody_variation=False)
                        nat_off = _nat_of(
                            base64.b64encode(wav_off).decode(), ref_b64)
                    except Exception as ab_ex:
                        print(f"  - {pid}: A/B off 组失败（{ab_ex}），跳过对照")
                wav = client.tts(
                    args.probe_text, reference_audio_b64=ref_b64,
                    reference_text=ref_text, emotion="neutral")
                synth_b64 = base64.b64encode(wav).decode()
                rv = score_similarity(ref_b64, synth_b64)
                if not rv.get("ok"):
                    raise RuntimeError(rv.get("detail") or "score failed")
                score = float(rv.get("similarity") or 0)
                label = classify_score(score, warn=args.warn, crit=args.crit)
                # 韵律自然度（以该人设参考音为基准；刻度校准期只记录不告警）
                nat = _nat_of(synth_b64, ref_b64)
                seen_refs[key] = (score, label, nat, nat_off)
            if nat_off is not None:
                _append_row(pid, score=score, label=label, nat=nat_off,
                            prosody="off")
            _append_row(pid, score=score, label=label, nat=nat, prosody="on")
            mark = {"ok": "✓", "warn": "⚠", "critical": "✗"}[label]
            nat_txt = f" nat={nat:.3f}" if nat is not None else ""
            if nat is not None and nat_off is not None:
                nat_txt += f" (Δab={nat - nat_off:+.3f})"
            # 自然度守门（自动校准启用后）：低于下限 → 整体至少 warn（播音腔回归
            # 不算灾难级，不改 exit code 语义，只在输出/最差级别上可见）
            if nat is not None and nat_floor > 0 and nat < nat_floor:
                nat_txt += f" ⚠低于校准下限{nat_floor}"
                if order["warn"] > order[worst]:
                    worst = "warn"
            print(f"  {mark} {pid}: {score:.4f} ({label}){nat_txt}")
            if order[label] > order[worst]:
                worst = label
        except Exception as ex:
            print(f"  ✗ {pid}: FAILED {ex}")
            worst = "critical"

    print(f"[*] 抽检完成，最差={worst}（正常带 0.78~0.86；<{args.warn} 警戒，"
          f"<{args.crit} 灾难级=音色资产大概率坏了）")
    return 1 if worst == "critical" else 0


if __name__ == "__main__":
    raise SystemExit(main())
