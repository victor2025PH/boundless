#!/usr/bin/env python3
"""realloc102.py — 实施102 五台算力重编：zhiliao overlay 分阶段补丁工具。

方案：docs/实施102_五台算力重编_176语音情绪主力_117退出运行_2026-09-22.md（§六 配置改动）。
本工具只改 ``config.local.yaml`` overlay（ruamel round-trip 保注释，与 compute_mode.py 同约定），
**不重启进程、不碰任何机器上的服务**。机器侧动作（停/起服务、钉模型、迁 aitr_asr）见文档 §七 runbook。

阶段（与文档 §七 对齐；每阶段可独立 dry-run / apply）：

    phase0   识图切 198（vision.base_url 176:11434 → 198:11434，timeout 3→5；硅基云仍第二位）
    phase1   语音切 176（默认 direct 形态）：关 hub_fish，avatar_voice.base_url(s) 与
             minicpm_clone.base_url → 176:7865 直连（本机参考音直传，不用 176 角色库），
             avatar_voice.stt.token_file 显式写集群令牌路径；
             avatar_voice.emotion_channel_threshold 2 → 0.5（现值 2 让情感永不触发）。
             ``--voice-path hub`` 则保留 09-22 前形态：hub_fish 钉 176:9000/index_tts/0.35
    phase2   听觉回 176：voice_recognition / speech_emotion.remote / audio_pipeline → 176:8765
    phase3   出图切 104（companion.selfie 子树内 176:8188 → 104:8188）；
             粤语克隆 127.0.0.1:7852（已死）→ 140:7852；
             删除 voice_lang_route.clone_langs.ja / ko（同指向已死端点；删掉后 ja/ko 走语种闸
             → 改发文字，不再先撞死端点吃超时）。**不**开 hub lang_engines ja/ko：fish 的 ja
             已撤（#161）、ko 无实证，开闸前必过 tools/verify_clone_lang.py（见 lang_voice_route）。
    all      phase0..3 一次做完（各阶段机器侧前置都已就位时用）

用法（默认 dry-run 只打印 diff，--apply 才落盘；--apply 前按阶段探活前置，--no-probe 跳过）：

    python deploy/compute/realloc102.py status
    python deploy/compute/realloc102.py phase0 [--apply] [--no-probe]
    python deploy/compute/realloc102.py phase1 --apply --svc-token-env AH_SVC_TOKEN
    python deploy/compute/realloc102.py all --apply --no-probe

``--svc-token-env NAME``：minicpm_clone 直连 176:7865 需 AvatarHub 令牌（X-AH-Svc）；
VoiceCloneClient 经 ``svc_token_env`` 从本机密钥仓取值（令牌绝不进 YAML）。不传则不写该键——
第二条腿 401 时按 strict 改发文字，与 hub 不可达同等处置。
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
from typing import Any, Callable, Dict, List, Optional

PHASES = ["phase0", "phase1", "phase2", "phase3"]
MODES = ["status"] + PHASES + ["all"]

DEFAULT_OVERLAY = Path(r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml")

# ── 端点契约（与文档 §四/§六 同源；改这里先改文档）───────────────────────────
VOICE_HUB = "http://192.168.0.176:9000"
VOICE_ENGINE = "http://192.168.0.176:7865"
ASR_SER = "http://192.168.0.176:8765"
ASR_SER_V1 = ASR_SER + "/v1"
VISION_198 = "http://192.168.0.198:11434/v1"
VISION_198_HOST = "192.168.0.198"
VISION_176_HOST = "192.168.0.176"
VISION_MODEL = "qwen3-vl:8b-instruct"
COMFY_OLD = "http://192.168.0.176:8188"
COMFY_NEW = "http://192.168.0.104:8188"
COSY_YUE = "http://192.168.0.140:7852"
DEAD_LOCAL_COSY = "http://127.0.0.1:7852"
OLD_VOICE_104 = "http://192.168.0.104:7865"

HUB_EMOTION_THRESHOLD = 0.35
LOCAL_EMOTION_THRESHOLD = 0.5
VISION_LAN_TIMEOUT = 5


# ── YAML 载入/落盘（ruamel round-trip 保注释）────────────────────────────────
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


def _ensure_map(parent, key):
    node = parent.get(key)
    if not isinstance(node, dict):
        node = {}
        parent[key] = node
    return node


def _set(node, key, value, notes: List[str], path: str) -> None:
    old = node.get(key)
    if old != value:
        node[key] = value
        notes.append(f"{path}.{key}: {old!r} -> {value!r}")


# ── 各阶段纯函数：改 data、返回人读得懂的变更清单 ─────────────────────────────
def apply_phase0(data) -> List[str]:
    """识图 176 → 198。"""
    notes: List[str] = []
    vis = _ensure_map(data, "vision")
    _set(vis, "base_url", VISION_198, notes, "vision")
    urls = vis.get("base_urls")
    urls = [str(u) for u in urls] if isinstance(urls, list) else []
    new_urls = [VISION_198] + [u for u in urls if VISION_176_HOST not in u and u != VISION_198]
    if urls != new_urls:
        vis["base_urls"] = new_urls
        notes.append(f"vision.base_urls: {urls!r} -> {new_urls!r}")
    em = vis.get("endpoint_models")
    if isinstance(em, dict):
        if VISION_176_HOST in em:
            em.pop(VISION_176_HOST)
            notes.append(f"vision.endpoint_models 删 {VISION_176_HOST}")
        _set(em, VISION_198_HOST, VISION_MODEL, notes, "vision.endpoint_models")
    et = vis.get("endpoint_timeouts")
    if isinstance(et, dict):
        if VISION_176_HOST in et:
            et.pop(VISION_176_HOST)
            notes.append(f"vision.endpoint_timeouts 删 {VISION_176_HOST}")
        # 4070 比 5090 慢，3s 会误切云端；198 实测 0.4s 暖机，5s 留冷启余量
        _set(et, VISION_198_HOST, VISION_LAN_TIMEOUT, notes, "vision.endpoint_timeouts")
    return notes


VOICE_PATHS = ("hub", "direct")
DEFAULT_VOICE_PATH = "direct"   # 2026-09-22 采纳：不用 176 角色库 + 176 主跑语音情感 两条同时成立
# AvatarVoiceClient 直连 LAN 引擎带 X-AH-Svc，令牌读 avatar_voice.stt.token_file → env 设备令牌
# → 开发机缺省路径。direct 形态把缺省路径写显式，让「这条腿依赖这个文件」在配置里可见
# （阶段 4 迁 173 时必须把它一起拷过去，见 runbook）。
CLUSTER_TOKEN_FILE = "D:/faceX/mfys/secrets/service_token.txt"


def apply_phase1(data, svc_token_env: str = "", voice_path: str = DEFAULT_VOICE_PATH) -> List[str]:
    """语音主路 → 176；本地腿 → 176:7865；本地路径情感阈值修正。

    ``voice_path``（两条都落在 176 同一块 5090 的 IndexTTS-2 上，差别在怎么到达）：
    - ``hub``：hub :9000 ``/api/tts_only`` 用 176 角色库档名（profile_map），失败再走本地腿。
      现生产形态，改动最小。
    - ``direct``：关 hub_fish，本地腿 ``avatar_voice.base_urls=[176:7865]`` 直连引擎，参考音
      由本机 ``voice_refs`` 直传（reference_audio_b64 + 逐字稿，指纹生命周期守卫在本机），
      情感通道 emotion/emo_text/emo_alpha 全带。同时满足 08-29「不用 176 角色库」与今日
      「176 主跑语音情感」两条指令，也是内测桌面种子 ``config.desktop.internal.yaml``
      的形态；需 AvatarHub 令牌（117 走 resolve_service_token 缺省路径，或 svc_token_env）。
    """
    if voice_path not in VOICE_PATHS:
        raise ValueError(f"voice_path 须为 {VOICE_PATHS}")
    notes: List[str] = []
    av = _ensure_map(data, "avatar_voice")
    hf = _ensure_map(av, "hub_fish")
    if voice_path == "hub":
        _set(hf, "enabled", True, notes, "avatar_voice.hub_fish")
        _set(hf, "base_url", VOICE_HUB, notes, "avatar_voice.hub_fish")
        _set(hf, "tts_engine", "index_tts", notes, "avatar_voice.hub_fish")
        _set(hf, "emotion_threshold", HUB_EMOTION_THRESHOLD, notes, "avatar_voice.hub_fish")
    else:
        _set(hf, "enabled", False, notes, "avatar_voice.hub_fish")
        # base_url/tts_engine 保留：日后切回 hub 只翻 enabled
        stt = _ensure_map(av, "stt")
        if not str(stt.get("token_file") or "").strip():
            _set(stt, "token_file", CLUSTER_TOKEN_FILE, notes, "avatar_voice.stt")

    _set(av, "base_url", VOICE_ENGINE, notes, "avatar_voice")
    urls = av.get("base_urls")
    urls = [str(u) for u in urls] if isinstance(urls, list) else []
    new_urls = [VOICE_ENGINE] + [u for u in urls if u not in (VOICE_ENGINE, OLD_VOICE_104)]
    if urls != new_urls:
        av["base_urls"] = new_urls
        notes.append(f"avatar_voice.base_urls: {urls!r} -> {new_urls!r}")
    # 现值 2：情绪强度只在 0–1，等于永不触发情感标签。0.5 与 Phase12 放量口径一致。
    _set(av, "emotion_channel_threshold", LOCAL_EMOTION_THRESHOLD, notes, "avatar_voice")

    mc = _ensure_map(data, "minicpm_clone")
    _set(mc, "base_url", VOICE_ENGINE, notes, "minicpm_clone")
    if svc_token_env:
        _set(mc, "svc_token_env", svc_token_env, notes, "minicpm_clone")
    return notes


def apply_phase2(data) -> List[str]:
    """转写 + 语音情绪 + 音频管线 → 176:8765。"""
    notes: List[str] = []
    vr = _ensure_map(data, "voice_recognition")
    _set(vr, "base_url", ASR_SER_V1, notes, "voice_recognition")
    se = _ensure_map(_ensure_map(data, "speech_emotion"), "remote")
    _set(se, "base_url", ASR_SER, notes, "speech_emotion.remote")
    ap = data.get("audio_pipeline")
    if isinstance(ap, dict):
        _set(ap, "base_url", ASR_SER_V1, notes, "audio_pipeline")
    return notes


def _replace_str_deep(node, old: str, new: str, path: str, notes: List[str]) -> None:
    """递归把子树里等于 old 的字符串换成 new（键名未知时的稳妥做法：只动精确等值）。"""
    if isinstance(node, dict):
        for k in list(node.keys()):
            v = node[k]
            if isinstance(v, str):
                if v == old:
                    node[k] = new
                    notes.append(f"{path}.{k}: {old} -> {new}")
            else:
                _replace_str_deep(v, old, new, f"{path}.{k}", notes)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                if v == old:
                    node[i] = new
                    notes.append(f"{path}[{i}]: {old} -> {new}")
            else:
                _replace_str_deep(v, old, new, f"{path}[{i}]", notes)


def apply_phase3(data) -> List[str]:
    """出图 → 104；粤语 → 140 CosyVoice；删已死的 ja/ko 克隆路由。"""
    notes: List[str] = []
    comp = data.get("companion")
    if isinstance(comp, dict) and isinstance(comp.get("selfie"), dict):
        _replace_str_deep(comp["selfie"], COMFY_OLD, COMFY_NEW, "companion.selfie", notes)

    vlr = data.get("voice_lang_route")
    if isinstance(vlr, dict):
        cant = vlr.get("cantonese")
        if isinstance(cant, dict):
            vp = cant.get("voice_profile")
            if isinstance(vp, dict) and str(vp.get("clone_base_url") or "") in (DEAD_LOCAL_COSY, ""):
                _set(vp, "clone_base_url", COSY_YUE, notes, "voice_lang_route.cantonese.voice_profile")
        cl = vlr.get("clone_langs")
        if isinstance(cl, dict):
            for lang in ("ja", "ko"):
                ent = cl.get(lang)
                if not isinstance(ent, dict):
                    continue
                vp = ent.get("voice_profile") if isinstance(ent.get("voice_profile"), dict) else {}
                url = str(vp.get("clone_base_url") or "")
                # 只删指向已死本机 7852 的条目；运营若已改指别处，视为有意保留
                if url in (DEAD_LOCAL_COSY, ""):
                    cl.pop(lang)
                    notes.append(
                        f"voice_lang_route.clone_langs.{lang} 删除（指向已死 {DEAD_LOCAL_COSY}；"
                        f"{lang} 改走语种闸→文字，fish 未过 verify_clone_lang 不开闸）")
    return notes


PHASE_FN: Dict[str, Callable[..., List[str]]] = {
    "phase0": apply_phase0,
    "phase1": apply_phase1,
    "phase2": apply_phase2,
    "phase3": apply_phase3,
}


def apply_phases(data, phases: List[str], svc_token_env: str = "",
                 voice_path: str = DEFAULT_VOICE_PATH) -> List[str]:
    notes: List[str] = []
    for p in phases:
        fn = PHASE_FN[p]
        got = fn(data, svc_token_env, voice_path) if p == "phase1" else fn(data)
        notes.extend(f"[{p}] {n}" for n in got)
    return notes


# ── 机器侧前置探活（只在 --apply 且未 --no-probe 时跑；LAN 才可达）──────────────
def _http_text(url: str, timeout: float = 5.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(4000).decode("utf-8", "replace")


def _probe(url: str, must: List[str], timeout: float = 5.0) -> Optional[str]:
    """返回 None=通过；否则一句失败原因。"""
    try:
        body = _http_text(url, timeout)
    except Exception as exc:  # noqa: BLE001
        return f"{url} 不可达（{type(exc).__name__}）"
    missing = [m for m in must if m not in body]
    if missing:
        return f"{url} 响应缺 {missing}：{body[:160]!r}"
    return None


PHASE_PROBES: Dict[str, List[tuple]] = {
    # phase0：198 必须已拉起 qwen3-vl（/api/tags 列出即可；钉常驻见 runbook）
    "phase0": [("http://192.168.0.198:11434/api/tags", [VISION_MODEL])],
    # phase1：引擎本体已载入；hub 目录能列 index_tts（direct 形态不查 hub，见 run_probes）
    "phase1": [(VOICE_ENGINE + "/health", ['"model_loaded":true']),
               (VOICE_HUB + "/api/engines", ["index_tts"])],
    # phase2：aitr_asr 已在 176 起来且两模型都载入
    "phase2": [(ASR_SER + "/health", ['"asr_loaded":true', '"ser_loaded":true'])],
    # phase3：104 ComfyUI 与 140 CosyVoice 都在
    "phase3": [(COMFY_NEW + "/system_stats", ["comfyui_version"]),
               (COSY_YUE + "/health", ['"models_loaded":true'])],
}


def run_probes(phases: List[str], voice_path: str = DEFAULT_VOICE_PATH) -> List[str]:
    fails: List[str] = []
    for p in phases:
        for url, must in PHASE_PROBES.get(p, []):
            if p == "phase1" and voice_path == "direct" and url.startswith(VOICE_HUB):
                continue
            why = _probe(url, must)
            if why:
                fails.append(f"[{p}] {why}")
    return fails


# ── status / audit ───────────────────────────────────────────────────────────
def _g(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def cmd_status(overlay: Path) -> None:
    _, data = _load_yaml(overlay)
    rows = [
        ("vision.base_url", _g(data, "vision", "base_url")),
        ("vision.endpoint_timeouts", _g(data, "vision", "endpoint_timeouts")),
        ("avatar_voice.hub_fish.base_url", _g(data, "avatar_voice", "hub_fish", "base_url")),
        ("avatar_voice.hub_fish.tts_engine", _g(data, "avatar_voice", "hub_fish", "tts_engine")),
        ("avatar_voice.hub_fish.emotion_threshold", _g(data, "avatar_voice", "hub_fish", "emotion_threshold")),
        ("avatar_voice.hub_fish.lang_engines", _g(data, "avatar_voice", "hub_fish", "lang_engines")),
        ("avatar_voice.hub_fish.enabled", _g(data, "avatar_voice", "hub_fish", "enabled")),
        ("avatar_voice.base_urls", _g(data, "avatar_voice", "base_urls")),
        ("avatar_voice.stt.token_file", _g(data, "avatar_voice", "stt", "token_file")),
        ("avatar_voice.emotion_channel_threshold", _g(data, "avatar_voice", "emotion_channel_threshold")),
        ("minicpm_clone.base_url", _g(data, "minicpm_clone", "base_url")),
        ("minicpm_clone.svc_token_env", _g(data, "minicpm_clone", "svc_token_env")),
        ("voice_recognition.base_url", _g(data, "voice_recognition", "base_url")),
        ("speech_emotion.remote.base_url", _g(data, "speech_emotion", "remote", "base_url")),
        ("audio_pipeline.base_url", _g(data, "audio_pipeline", "base_url")),
        ("voice_lang_route.cantonese.clone_base_url",
         _g(data, "voice_lang_route", "cantonese", "voice_profile", "clone_base_url")),
        ("voice_lang_route.clone_langs", sorted((_g(data, "voice_lang_route", "clone_langs") or {}).keys())),
    ]
    print(f"overlay: {overlay}")
    for k, v in rows:
        print(f"  {k:<44} {v!r}")
    comfy: List[str] = []
    sel = _g(data, "companion", "selfie")
    if isinstance(sel, dict):
        _replace_str_deep(json.loads(json.dumps(sel, default=str)), COMFY_OLD, "<176>", "companion.selfie", comfy)
        _replace_str_deep(json.loads(json.dumps(sel, default=str)), COMFY_NEW, "<104>", "companion.selfie", comfy)
    print(f"  {'companion.selfie comfy 端点':<44} "
          + ("; ".join(c.split(': ')[0] + ' = ' + ('176:8188' if '<176>' in c else '104:8188') for c in comfy) or "未见 8188 端点"))
    print("\n机器侧探活（LAN 才可达）：")
    for p in PHASES:
        fails = run_probes([p])
        print(f"  {p}: " + ("前置就位" if not fails else " | ".join(fails)))


def _audit_apply(overlay: Path, phases: List[str], notes: List[str]) -> None:
    try:
        logs = overlay.resolve().parent.parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": round(time.time(), 3),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "event": "cli_apply",
            "via": "realloc102_cli",
            "phases": phases,
            "notes": notes[:40],
        }
        with (logs / "ai_primary_audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--apply", action="store_true", help="落盘（默认 dry-run 打印 diff）")
    ap.add_argument("--no-probe", action="store_true", help="--apply 时跳过机器侧前置探活")
    ap.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    ap.add_argument("--svc-token-env", default="", help="phase1：minicpm_clone 直连 176:7865 的令牌密钥名")
    ap.add_argument("--voice-path", choices=VOICE_PATHS, default=DEFAULT_VOICE_PATH,
                    help="phase1 语音到达 176 的方式：direct（默认，已采纳）=关 hub_fish、本机参考音"
                         "直连 :7865，不用角色库；hub=经 :9000 角色库（09-22 前的生产形态，留作回切）")
    args = ap.parse_args()

    if not args.overlay.exists():
        sys.exit(f"overlay 不存在: {args.overlay}")
    if args.mode == "status":
        cmd_status(args.overlay)
        return

    phases = PHASES if args.mode == "all" else [args.mode]
    raw_original = args.overlay.read_text(encoding="utf-8")
    yaml, data = _load_yaml(args.overlay)
    before = _dump_str(yaml, data)
    notes = apply_phases(data, phases, args.svc_token_env, args.voice_path)
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
        print("\n[dry-run] 未落盘。确认无误后加 --apply（机器侧前置未就位会被探活拒绝，--no-probe 可跳）。")
        return

    if not args.no_probe:
        fails = run_probes(phases, args.voice_path)
        if fails:
            sys.exit("拒绝落盘：机器侧前置未就位——\n  " + "\n  ".join(fails)
                     + "\n先按 docs/实施102 §七 runbook 把对应机器侧步骤做完，或 --no-probe 强行。")

    bak = args.overlay.with_suffix(f".yaml.bak_realloc102_{'_'.join(phases)}")
    bak.write_text(raw_original, encoding="utf-8")
    args.overlay.write_text(after, encoding="utf-8")
    _audit_apply(args.overlay, phases, notes)
    print(f"\n已落盘（备份: {bak.name}）。overlay 热重载 ~30s 生效，本轮键均免重启。")
    print("验收：python deploy/compute/realloc102.py status；语音走 tools/hub_tts_fetch.py 实听一句。")


if __name__ == "__main__":
    main()
