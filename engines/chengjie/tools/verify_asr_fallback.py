# -*- coding: utf-8 -*-
"""入站语音转写（ASR）第二落点的真机验收探针（2026-08-28，云优先算力重分配 BLOCKER-2 #3）。

**为什么需要它**：中枢 0828 云优先执行单要腾空 176 的 GPU，而生产实例的
`voice_recognition` 主路正是 `176:8765`（Whisper large-v3-turbo + SER），
且 overlay 里 `fallback: []` + `max_retries: 0` —— 代码侧 `create_transcriber`
对空列表直接 `return primary`，`sensevoice`/`whisper` 两个子段在 provider 不是
它们时**根本不会被实例化**。即：176 一停，每条入站语音转写直接失败。

同仓 `config.desktop.internal.yaml` 却配了 `avatar_whisper @ 140:7854` 作第二级
（文档描述的「176 GPU → 140 AvatarHub → 本机 CPU」级联）—— 生产实例是**配置漂移**，
不是刻意取舍。

**但"配上了"不等于"接得住"**：140:7854 是 AvatarHub 契约（`/transcribe_b64` +
`X-AH-Svc` 令牌 + ffmpeg 转 16k WAV），与主路的 OpenAI 兼容口完全不同源，且令牌
在**运行时**读磁盘。HTTP 200 与"能出正确文本"是两件事（仓内 media-artifact 纪律）。
所以本探针**真送一段真实入站语音**，并**真把主路指向死端口**模拟腾卡，验证：

1. 备路单独跑得出文本（令牌可读 + ffmpeg 在场 + 服务真能转）；
2. 级联在主路死时**真的接管**（`FallbackTranscriber` 逐级语义）；
3. 回落被 `asr_stats` 记成 `fallback_ok`（降级信号可观测，不是静默）；
4. 备路文本与主路文本高度一致（同为 large-v3-turbo，不该是另一段内容）。

## 绝对只读

不发任何消息、不写生产配置、不碰生产库；临时 WAV 落系统临时目录并即删。
唯一的"写"是 GPU 上的一次前向推理（两次转写，各约 1 秒）。

## 用法

    python tools/verify_asr_fallback.py                  # 自动找样本，全场景
    python tools/verify_asr_fallback.py --audio X.ogg    # 指定音频
    python tools/verify_asr_fallback.py --json
    python tools/verify_asr_fallback.py --skip-primary   # 176 已腾卡后的验收姿势

退出码：0 = 备胎可用（或环境缺失 SKIP）；1 = **备胎是纸面兜底**（红灯）。
腾卡后复跑本探针即为验收：`--skip-primary` 下 S3/S4 仍须绿。

**刻意不进 gate_sweep**：它要真打 LAN GPU，属人工/事件驱动巡检。
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

# Windows 控制台默认 GBK：中文表头会直接抛 UnicodeEncodeError 把工具打死。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402

# 主路"死端口"：127.0.0.1:9（discard）——本机必然拒连，比指向 176 的错误端口更快
# 且不给 176 任何流量（腾卡演练不该反过来打扰被演练的机器）。
DEAD_ENDPOINT = "http://127.0.0.1:9/v1"

# 两个 ASR 都是 large-v3-turbo，同一段音频的转写差异应只在标点/儿化音级别。
# 0.5 是"确实是同一段内容"的宽松地板（措辞级差异不该判红）。
MIN_TEXT_SIMILARITY = 0.5


def _probe_http(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= int(r.status) < 300
    except Exception:
        return False


def _find_sample_audio(roots: List[Path]) -> Optional[Path]:
    """挑一段**真实入站**语音：protocol_media 下不带 out_ 前缀的最新 ogg/oga/opus。

    刻意不用 assets/voices 里的人设歌曲/参考音——那些是我方合成产物，
    音质与真实客户手机录音差一个量级，用它验收会给出偏乐观的结论。
    """
    best: Optional[Path] = None
    best_mtime = -1.0
    for root in roots:
        media = Path(root) / "protocol_media"
        if not media.is_dir():
            continue
        for p in media.rglob("*"):
            if p.suffix.lower() not in (".ogg", ".oga", ".opus"):
                continue
            if p.name.startswith("out_"):  # 出站语音=我方 TTS 产物
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > best_mtime:
                best, best_mtime = p, m
    return best


def _similarity(a: str, b: str) -> float:
    """字符级相似度（ASR 文本比对；中文无分词，字符级足够且无依赖）。"""
    aa = "".join(str(a or "").split())
    bb = "".join(str(b or "").split())
    if not aa or not bb:
        return 0.0
    return round(difflib.SequenceMatcher(None, aa, bb).ratio(), 4)


async def _transcribe(cfg: Dict[str, Any], audio: Path, language: str) -> Optional[str]:
    from src.voice_transcriber import VoiceTranscriberFactory
    t = VoiceTranscriberFactory.create_transcriber(cfg)
    return await t.transcribe_voice_message(str(audio), language)


def _base_cfg(vr: Dict[str, Any], tmp_dir: str) -> Dict[str, Any]:
    """从生产 voice_recognition 段派生探针配置（temp_dir 改指临时目录，不落生产树）。"""
    cfg = dict(vr)
    cfg["temp_dir"] = tmp_dir
    cfg.pop("fallback", None)
    return cfg


def _fallback_spec(vr: Dict[str, Any]) -> Dict[str, Any]:
    """备路规格：优先用生产已配的第一级；未配则用桌面内部包的既有约定值。"""
    fb = vr.get("fallback")
    if isinstance(fb, list) and fb and isinstance(fb[0], dict):
        return dict(fb[0])
    return {
        "provider": "avatar_whisper",
        "base_url": "http://192.168.0.140:7854",
        "timeout": 30,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ASR 第二落点真机验收（只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现）")
    ap.add_argument("--audio", default="", help="音频文件（默认自动找真实入站语音）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--skip-primary", action="store_true",
                    help="不测主路（176 已腾卡后的验收姿势）")
    ap.add_argument("--fb-timeout", type=float, default=0.0,
                    help="覆写备路超时秒数（诊断「慢 vs 死」：140 显存被挤满时 "
                         "Whisper 会退 CPU，生产档 30s 不够但并非服务死了）")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root)
    root = roots[0]
    cfg_all = load_merged_config(root)
    vr = cfg_all.get("voice_recognition") if isinstance(
        cfg_all.get("voice_recognition"), dict) else {}

    report: Dict[str, Any] = {
        "data_root": str(root),
        "verdict": "unknown",
        "scenarios": {},
    }
    out: List[str] = []

    def say(line: str = "") -> None:
        out.append(line)

    # ── S1 配置体检 ────────────────────────────────────────────────────────
    fb_cfg = vr.get("fallback")
    fb_levels = len(fb_cfg) if isinstance(fb_cfg, list) else (1 if fb_cfg else 0)
    primary_url = str(vr.get("base_url") or "")
    s1 = {
        "enabled": bool(vr.get("enabled")),
        "provider": str(vr.get("provider") or ""),
        "primary_base_url": primary_url,
        "max_retries": vr.get("max_retries"),
        "fallback_levels": fb_levels,
        "sole_landing_point": fb_levels == 0,
    }
    report["scenarios"]["s1_config"] = s1
    say("=" * 66)
    say("S1  生产配置体检")
    say("=" * 66)
    say(f"  实例数据根      : {root}")
    say(f"  enabled         : {s1['enabled']}")
    say(f"  主路 provider    : {s1['provider']}")
    say(f"  主路 base_url    : {primary_url or '(未配)'}")
    say(f"  max_retries     : {s1['max_retries']}")
    say(f"  fallback 级数    : {fb_levels}")
    if fb_levels == 0:
        say("  ⚠ 唯一落点：主路一停，入站语音转写**直接失败**（无级联、无重试）")
    else:
        for i, lv in enumerate(fb_cfg if isinstance(fb_cfg, list) else [fb_cfg], 1):
            if isinstance(lv, dict):
                say(f"    第{i}级: provider={lv.get('provider')} "
                    f"base_url={lv.get('base_url') or '(继承)'}")
    say()

    if not s1["enabled"]:
        say("SKIP: voice_recognition 未启用，无验收对象。")
        report["verdict"] = "skip_disabled"
        return _emit(report, out, args.json, 0)

    # ── 环境前置 ──────────────────────────────────────────────────────────
    spec = _fallback_spec(vr)
    if args.fb_timeout > 0:
        spec["timeout"] = args.fb_timeout
    fb_url = str(spec.get("base_url") or "http://192.168.0.140:7854").rstrip("/")
    token_file = str(spec.get("token_file")
                     or "D:/faceX/mfys/secrets/service_token.txt")
    token_ok = False
    try:
        token_ok = bool(Path(token_file).read_text(encoding="utf-8").strip())
    except Exception:
        token_ok = False
    fb_alive = _probe_http(f"{fb_url}/health")
    report["scenarios"]["s2_env"] = {
        "fallback_base_url": fb_url,
        "fallback_health": fb_alive,
        "token_readable": token_ok,
        "token_file": token_file,
    }
    say("=" * 66)
    say("S2  备路环境前置")
    say("=" * 66)
    say(f"  备路端点        : {fb_url}  health={'OK' if fb_alive else 'DOWN'}")
    say(f"  令牌可读        : {token_ok}  ({token_file})")
    say()
    if not fb_alive or not token_ok:
        say("SKIP: 备路不可达或令牌缺失 —— 环境问题，不判红（无法证伪备胎本身）。")
        report["verdict"] = "skip_env"
        return _emit(report, out, args.json, 0)

    audio = Path(args.audio) if args.audio else _find_sample_audio(roots)
    if not audio or not audio.is_file():
        say("SKIP: 找不到真实入站语音样本（--audio 可显式指定）。")
        report["verdict"] = "skip_no_audio"
        return _emit(report, out, args.json, 0)
    report["audio"] = {"path": str(audio), "bytes": audio.stat().st_size}
    say(f"样本音频: {audio.name}  ({audio.stat().st_size} bytes)")
    say()

    language = str(vr.get("language") or "auto")
    tmp_dir = tempfile.mkdtemp(prefix="asr_probe_")
    rc = 0

    from src.ai.asr_stats import get_asr_stats
    stats = get_asr_stats()

    # ── S3 主路基准 ───────────────────────────────────────────────────────
    primary_text = ""
    if args.skip_primary:
        say("S3  主路基准 —— 按 --skip-primary 跳过（腾卡后验收姿势）")
        say()
        report["scenarios"]["s3_primary"] = {"skipped": True}
    else:
        say("=" * 66)
        say("S3  主路基准转写（当前生产主路）")
        say("=" * 66)
        stats.reset()
        try:
            primary_text = asyncio.run(
                _transcribe(_base_cfg(vr, tmp_dir), audio, language)) or ""
        except Exception as e:  # noqa: BLE001
            primary_text = ""
            say(f"  异常: {e}")
        say(f"  文本: {primary_text or '(空)'}")
        report["scenarios"]["s3_primary"] = {
            "text": primary_text, "ok": bool(primary_text),
            "stats": stats.dump(),
        }
        if not primary_text:
            say("  ⚠ 主路当前就出不了文本（与备胎验收无关，但需另查）")
        say()

    # ── S4 备路单级 ───────────────────────────────────────────────────────
    say("=" * 66)
    say("S4  备路单级转写（只用备路，证明它真能出文本）")
    say("=" * 66)
    fb_only = _base_cfg(vr, tmp_dir)
    fb_only.update(spec)
    stats.reset()
    fb_text = ""
    try:
        fb_text = asyncio.run(_transcribe(fb_only, audio, language)) or ""
    except Exception as e:  # noqa: BLE001
        say(f"  异常: {e}")
    sim_single = _similarity(primary_text, fb_text) if primary_text else None
    say(f"  文本: {fb_text or '(空)'}")
    if sim_single is not None:
        say(f"  与主路相似度: {sim_single}")
    report["scenarios"]["s4_fallback_only"] = {
        "text": fb_text, "ok": bool(fb_text),
        "similarity_to_primary": sim_single,
    }
    if not fb_text:
        say("  ✗ 备路出不了文本 —— **纸面兜底**")
        rc = 1
    say()

    # ── S5 级联真接管（主路死端口 = 模拟 176 腾卡）────────────────────────
    say("=" * 66)
    say("S5  级联接管（主路指死端口，模拟 176 腾卡）")
    say("=" * 66)
    chain_cfg = _base_cfg(vr, tmp_dir)
    chain_cfg["base_url"] = DEAD_ENDPOINT
    chain_cfg["fallback"] = [dict(spec)]
    stats.reset()
    chain_text = ""
    try:
        chain_text = asyncio.run(_transcribe(chain_cfg, audio, language)) or ""
    except Exception as e:  # noqa: BLE001
        say(f"  异常: {e}")
    snap = stats.dump()
    sim_chain = _similarity(primary_text, chain_text) if primary_text else None
    say(f"  主路          : {DEAD_ENDPOINT}  (必然拒连)")
    say(f"  文本          : {chain_text or '(空)'}")
    say(f"  asr_stats     : primary_ok={snap.get('primary_ok')} "
        f"fallback_ok={snap.get('fallback_ok')} "
        f"all_failed={snap.get('all_failed')}")
    if sim_chain is not None:
        say(f"  与主路相似度  : {sim_chain}")
    report["scenarios"]["s5_chain_takeover"] = {
        "text": chain_text, "ok": bool(chain_text),
        "similarity_to_primary": sim_chain,
        "stats": snap,
    }
    if not chain_text:
        say("  ✗ 级联没接住 —— 176 腾卡后入站语音会直接失败")
        rc = 1
    elif int(snap.get("fallback_ok") or 0) < 1:
        say("  ✗ 出了文本但未记 fallback_ok —— 降级信号不可观测（看板看不到）")
        rc = 1
    if sim_chain is not None and sim_chain < MIN_TEXT_SIMILARITY:
        say(f"  ✗ 相似度 {sim_chain} < {MIN_TEXT_SIMILARITY} —— 备路转的可能不是同一段内容")
        rc = 1
    say()

    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    say("=" * 66)
    if rc == 0:
        report["verdict"] = "fallback_usable"
        say("结论: 备路可用 —— 176 腾卡后入站语音转写有真落点（140:7854 接管）")
        if fb_levels == 0:
            say("      ⚠ 但生产 overlay 仍是 fallback: []，需落配置 + 重启装载才生效")
    else:
        report["verdict"] = "fallback_paper_only"
        say("结论: 备路不可用（纸面兜底）—— 176 腾卡前必须先解决")
    say("=" * 66)
    return _emit(report, out, args.json, rc)


def _emit(report: Dict[str, Any], lines: List[str], as_json: bool, rc: int) -> int:
    if as_json:
        report["exit_code"] = rc
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
