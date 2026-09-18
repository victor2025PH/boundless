# -*- coding: utf-8 -*-
"""算力调度快照推送器（实施70 2026-08-27 老板指令：官网实时算力看板）。

117 上常驻循环（计划任务 Boundless-compute-pusher 每分钟拉活；本进程 18797 端口
单例锁防重复），每 ~10s 收集一份集群算力全景快照 → POST 官网
``/api/admin/compute-status``（头 ``x-compute-key``，密钥在
``D:\\chengjie-instances\\.ops\\compute_report_key.txt``，绝不入库/入 git）。

收集面（任一收集器失败=该面标不可达，绝不崩循环）：
- 出话链三档（**顺序与厂商全部从实例 overlay 现读，不写死**，2026-09-17 沉淀）：
  · ``cloud``＝``ai.base_url`` 的厂商（硅基 → GET /v1/models 探活；DeepSeek → /user/balance
    余额即探活；其它厂商 → /models）；
  · ``pool``＝``ai.key_pool.keys[0]`` 的厂商，同款探法；
  · ``local``＝``ai.fallback`` 端点（173 vLLM :8001 ``chatx``，GET /v1/models 看目录；
    08-28 已从 ollama :11434 迁走，勿再看 /api/ps）。
  · ``mode``/``lock``＝``ai.primary``/``ai.primary_lock``；``order`` 给出实际降级顺序，
    各档带 ``role``（① 主链 / ② 回落 …）供看板标题——08-27 初版把「硅基主胎 / DeepSeek
    备胎 / 173 三线」写死在采集器与看板里，09-17 主链改锁 local 后官网仍画老模式。
  云端探针 60s 节流（10s 打余额接口是骚扰），间隔期复用上一轮结果。
- 智聊引擎：/login 200 探活 + ai-runtime-status（降级快照）+ cloud-credentials 的
  usage 段（出话分布），带 web_admin Bearer；只透传白名单字段，绝不外送密钥。
- LAN GPU 四机 ollama /api/ps 显存水位与驻留模型；176 ComfyUI system_stats；
  176:9000 hub 泊车/挂起名单；176:8765 ASR/SER 与 104:7865 克隆 TTS 健康。

用法：python tools/compute_status_report.py [--once]（--once 推一发即退，验证用）。
"""
from __future__ import annotations

import json
import re
import socket
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

SITE = "https://bd2026.cc"
REPORT_KEY_FILE = Path(r"D:\chengjie-instances\.ops\compute_report_key.txt")
INSTANCE_CFG_DIR = Path(r"D:\chengjie-instances\zhiliao\data\config")
SINGLETON_PORT = 18797
PUSH_INTERVAL = 10
CLOUD_INTERVAL = 60
# 与 src/ai/ai_primary_summary.BOARD_NUDGE_PATH 同路径（本进程不 import src）
NUDGE_FILE = Path(r"D:\chengjie-instances\.ops\compute_pusher.nudge")


def consume_board_nudge(path: Optional[Path] = None) -> bool:
    """切档通知放下的 nudge：存在则删并返回 True（下一圈强制走慢车道重读 summary）。"""
    p = path or NUDGE_FILE
    try:
        if p.exists():
            p.unlink()
            return True
    except Exception:
        return False
    return False

HOSTS = [
    {"host": "zhongshu-176", "ip": "192.168.0.176", "total_gb": 32.0},
    # 173 出话口是 vLLM :8001（08-28 起）；:11434 Ollama 守护进程还活着但不管这张卡，
    # 探它只会画出「空卡 0 GB」——看板上主链正在服务的 5090 像闲着一样
    {"host": "yunsheng-173", "ip": "192.168.0.173", "total_gb": 32.0, "kind": "vllm", "port": 8001},
    {"host": "tingxie-140", "ip": "192.168.0.140", "total_gb": 12.0},
    {"host": "kouxing-198", "ip": "192.168.0.198", "total_gb": 12.0},
]

_KV_CACHE_RE = re.compile(r"^vllm:kv_cache_usage_perc(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.M)


def _get(url: str, timeout: float = 4.0, headers: Optional[Dict[str, str]] = None) -> Optional[Any]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _get_text(url: str, timeout: float = 4.0) -> Optional[str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _get_status(url: str, timeout: float = 4.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception:
        return 0


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _vendor_of(base_url: Any) -> str:
    """端点 URL → 厂商标签（看板节点名）。未知厂商回主机名，绝不猜。"""
    u = str(base_url or "")
    if "siliconflow" in u:
        return "siliconflow"
    if "deepseek" in u:
        return "deepseek-official"
    m = re.search(r"192\.168\.0\.(\d+)", u)
    if m:
        return f"LAN .{m.group(1)}"
    m = re.match(r"https?://([^/]+)", u)
    return m.group(1) if m else (u or "未配置")


def load_secrets() -> Dict[str, Any]:
    """从实例配置提取（只进内存，永不打印/外送）：云主链/池首键的 {base_url, model, api_key}、
    本地端点、档位与锁、引擎 Bearer、上报密钥。**厂商不写死**——按 overlay 现值。"""
    out: Dict[str, Any] = {
        "cloud": {}, "pool": {}, "local": {}, "mode": "cloud", "lock": "",
        "engine_token": "", "report_key": "",
    }
    overlay = _load_yaml(INSTANCE_CFG_DIR / "config.local.yaml")
    main = _load_yaml(INSTANCE_CFG_DIR / "config.yaml")
    ai = (overlay.get("ai") or {}) if isinstance(overlay.get("ai"), dict) else {}
    out["mode"] = str(ai.get("primary") or "cloud").strip().lower() or "cloud"
    out["lock"] = str(ai.get("primary_lock") or "").strip().lower()
    if ai.get("base_url"):
        out["cloud"] = {"base_url": str(ai.get("base_url") or ""),
                        "model": str(ai.get("model") or ""),
                        "api_key": str(ai.get("api_key") or "")}
    for item in ((ai.get("key_pool") or {}).get("keys") or []):
        if isinstance(item, dict) and item.get("api_key"):
            # 池条目缺省继承主链 base_url/model（引擎同语义）
            out["pool"] = {"base_url": str(item.get("base_url") or out["cloud"].get("base_url") or ""),
                           "model": str(item.get("model") or out["cloud"].get("model") or ""),
                           "api_key": str(item.get("api_key") or "")}
            break
    fb = ai.get("fallback") if isinstance(ai.get("fallback"), dict) else {}
    if fb and fb.get("base_url"):
        out["local"] = {"base_url": str(fb.get("base_url") or "").rstrip("/"),
                        "model": str(fb.get("model") or ""),
                        "enabled": bool(fb.get("enabled", True))}
    for cfg in (overlay, main):
        tok = ((cfg.get("web_admin") or {}) if isinstance(cfg.get("web_admin"), dict) else {}).get("auth_token")
        if tok:
            out["engine_token"] = str(tok)
            break
        # 兼容 auth_token 不在 web_admin 下的历史布局：整文件正则兜底
    if not out["engine_token"]:
        for name in ("config.local.yaml", "config.yaml"):
            try:
                m = re.search(r"auth_token:\s*(\S+)", (INSTANCE_CFG_DIR / name).read_text(encoding="utf-8"))
                if m:
                    out["engine_token"] = m.group(1)
                    break
            except Exception:
                pass
    try:
        out["report_key"] = REPORT_KEY_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return out


def _collect_vllm_host(h: Dict[str, Any]) -> Dict[str, Any]:
    """vLLM 主机：/v1/models 目录 + /metrics KV cache 水位。vLLM 预留式占显存、
    不按模型逐个报字节，used_gb 不猜（留空），看板画「常驻 · 模型 · KV x%」。"""
    base = f"http://{h['ip']}:{int(h.get('port') or 8001)}"
    row: Dict[str, Any] = {"host": h["host"], "ip": h["ip"], "total_gb": h["total_gb"], "kind": "vllm"}
    j = _get(f"{base}/v1/models", timeout=2.5)
    if not isinstance(j, dict):
        row["reachable"] = False
        return row
    ids = [str(m["id"]) for m in (j.get("data") or []) if isinstance(m, dict) and m.get("id")]
    kv: Optional[float] = None
    txt = _get_text(f"{base}/metrics", timeout=2.5)
    if txt:
        vals = []
        for m in _KV_CACHE_RE.finditer(txt):
            try:
                vals.append(float(m.group(1)))
            except ValueError:
                pass
        if vals:
            v = max(vals)
            kv = round(v * 100.0 if v <= 1.0 else v, 1)
    row.update({
        "reachable": True,
        "models": [{"name": i, "vram_gb": None} for i in ids],
        "kv_cache_pct": kv,
        "note": ("vLLM 常驻 " + "、".join(ids[:3]) if ids else "vLLM 进程在、模型目录为空")
                + (f" · KV cache {kv:g}%" if kv is not None else ""),
    })
    return row


def collect_hosts() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for h in HOSTS:
        if h.get("kind") == "vllm":
            rows.append(_collect_vllm_host(h))
            continue
        j = _get(f"http://{h['ip']}:11434/api/ps", timeout=2.5)
        row: Dict[str, Any] = {"host": h["host"], "ip": h["ip"], "total_gb": h["total_gb"]}
        if j is None:
            row["reachable"] = False
        else:
            models = []
            used = 0.0
            for m in (j.get("models") or []):
                vram = float(m.get("size_vram") or 0) / (1024 ** 3)
                used += vram
                models.append({"name": str(m.get("name") or "?"), "vram_gb": round(vram, 1)})
            row.update({"reachable": True, "used_gb": round(used, 1), "models": models})
        rows.append(row)
    return rows


def collect_comfy() -> Dict[str, Any]:
    j = _get("http://192.168.0.176:8188/system_stats", timeout=3.0)
    if not j:
        return {"ok": False, "ip": "192.168.0.176:8188"}
    try:
        dev = (j.get("devices") or [{}])[0]
        return {
            "ok": True,
            "ip": "192.168.0.176:8188",
            "vram_free_gb": round(float(dev.get("vram_free") or 0) / (1024 ** 3), 1),
            "vram_total_gb": round(float(dev.get("vram_total") or 0) / (1024 ** 3), 1),
        }
    except Exception:
        return {"ok": False, "ip": "192.168.0.176:8188"}


def collect_hub() -> Dict[str, Any]:
    j = _get("http://192.168.0.176:9000/health", timeout=3.0)
    if not j:
        return {"ok": False}
    cm = j.get("cluster_mode") or {}
    svc = j.get("services") or {}
    return {
        "ok": str(j.get("status") or "") == "ok",
        "mode": str(cm.get("mode") or ""),
        "held": [str(x) for x in (cm.get("held") or [])][:24],
        "parked": [str(x) for x in (cm.get("parked") or [])][:24],
        "services_up": sum(1 for v in svc.values() if v),
        "services_total": len(svc),
    }


def _strip_v1(url: Any) -> str:
    return re.sub(r"/v1/?$", "", str(url or "").strip().rstrip("/"))


def media_endpoints(overlay: Dict[str, Any]) -> Dict[str, str]:
    """媒体服务落点**从 overlay 现读**（2026-09-17）：ASR=``voice_recognition.base_url``，
    SER=``speech_emotion.remote.base_url``，TTS=``avatar_voice.base_url``。此前 ASR 写死
    176:8765——09 月 ASR/SER 已迁 198:8765，官网一直把活着的 ASR 画成红灯。缺键回落旧常量。"""
    vr = overlay.get("voice_recognition") if isinstance(overlay.get("voice_recognition"), dict) else {}
    se = ((overlay.get("speech_emotion") or {}).get("remote") or {}) \
        if isinstance(overlay.get("speech_emotion"), dict) else {}
    av = overlay.get("avatar_voice") if isinstance(overlay.get("avatar_voice"), dict) else {}
    return {
        "asr": _strip_v1(vr.get("base_url")) or "http://192.168.0.176:8765",
        "ser": _strip_v1(se.get("base_url")) or "http://192.168.0.176:8765",
        "tts": _strip_v1(av.get("base_url") or ((av.get("base_urls") or [""])[0] if isinstance(av.get("base_urls"), list) else ""))
               or "http://192.168.0.104:7865",
    }


def collect_media(overlay: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ep = media_endpoints(overlay or {})
    asr = _get(f"{ep['asr']}/health", timeout=3.0)
    # 2026-09-11：140:7852 CosyVoice 已停（RST）。克隆探针跟现役 IndexTTS-2（overlay avatar_voice）。
    # 健康形状两家不同：7852={ok,models_loaded}；7865={status:"ok",model_loaded}。
    tts = _get(f"{ep['tts']}/health", timeout=3.0)
    tts_ok = bool(tts) and (
        tts.get("ok") is True
        or str(tts.get("status") or "").lower() in ("ok", "healthy", "ready")
    )
    tts_loaded = bool(tts) and (
        tts.get("models_loaded") is True or tts.get("model_loaded") is True
    )
    tts_row = {"ok": tts_ok, "models_loaded": tts_loaded, "endpoint": ep["tts"]}
    return {
        "asr": {"ok": bool(asr and asr.get("status") == "ok"),
                "asr_loaded": bool(asr and asr.get("asr_loaded")),
                "ser_loaded": bool(asr and asr.get("ser_loaded")),
                "endpoint": ep["asr"]},
        "tts104": tts_row,
        "tts140": tts_row,  # 旧看板键兼容，语义已是 104
    }


def _probe_cloud_vendor(ep: Dict[str, Any], slot: str) -> Dict[str, Any]:
    """一档云端探活：按 base_url 厂商选探法。硅基 → GET /v1/models（余额接口 /v1/user/info
    已 410 下线，2026-08-27 实测，余额不造数）；DeepSeek → /user/balance（余额即探活）；
    其它 OpenAI 兼容厂商 → GET {base_url}/models。未配 key → ok=None + note。"""
    base = str(ep.get("base_url") or "").rstrip("/")
    node: Dict[str, Any] = {"vendor": _vendor_of(base), "model": str(ep.get("model") or ""),
                            "endpoint": base}
    key = str(ep.get("api_key") or "")
    if not base:
        node["ok"] = None
        node["note"] = f"overlay 未配置{slot}端点"
        return node
    if not key:
        node["ok"] = None
        node["note"] = f"overlay 未配置{slot} api_key"
        return node
    h = {"Authorization": f"Bearer {key}"}
    t0 = time.time()
    if "deepseek" in base:
        j = _get("https://api.deepseek.com/user/balance", timeout=8.0, headers=h)
        node["ok"] = bool(j and j.get("is_available"))
        try:
            for b in (j or {}).get("balance_infos") or []:
                if b.get("currency") == "CNY":
                    node["balance"] = f"¥{b.get('total_balance')}"
        except Exception:
            pass
    else:
        j = _get(f"{base}/models", timeout=8.0, headers=h)
        node["ok"] = j is not None
    if node.get("ok"):
        node["latency_ms"] = round((time.time() - t0) * 1000)
    return node


def collect_cloud(secrets: Dict[str, Any]) -> Dict[str, Any]:
    """云端两档探活（60s 节流由调用方管）：``cloud``＝ai.base_url 厂商，``pool``＝key 池首键。
    厂商全部由 overlay 决定——08-27 初版把硅基/DeepSeek 写死在这里，配置一换看板就说谎。"""
    return {
        "cloud": _probe_cloud_vendor(secrets.get("cloud") or {}, "主链"),
        "pool": _probe_cloud_vendor(secrets.get("pool") or {}, "key 池"),
    }


# overlay 缺 ai.fallback 时的兜底端点（现网契约：173 vLLM :8001 served-model-name=chatx）
LOCAL_VLLM = "http://192.168.0.173:8001/v1"
LOCAL_VLLM_MODEL = "chatx"
LOCAL_VLLM_LABEL = "chatx (Qwen3-27B AWQ)"


def collect_local_tier(secrets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """本地档：``ai.fallback`` 端点（173 vLLM ``chatx``）。独立探 ``/v1/models``。

    08-27 看板初版从 ollama ``/api/ps`` 推导 ``qwen2.5:14b`` 驻留；08-28 执行单 ⑤
    出话口已迁 :8001，14B 不再常驻——再看 /api/ps 会把「空列表」误报成本地不可用。
    """
    local = (secrets or {}).get("local") or {}
    base = str(local.get("base_url") or LOCAL_VLLM).rstrip("/")
    model = str(local.get("model") or LOCAL_VLLM_MODEL)
    t0 = time.time()
    j = _get(f"{base}/models", timeout=4.0)
    ids: List[str] = []
    if isinstance(j, dict):
        for row in (j.get("data") or []):
            if isinstance(row, dict) and row.get("id"):
                ids.append(str(row["id"]))
    loaded = any(i == model or i.endswith("/" + model) for i in ids)
    up = j is not None
    out: Dict[str, Any] = {
        "endpoint": base,
        "vendor": _vendor_of(base) + " vLLM",
        "model": LOCAL_VLLM_LABEL if model == LOCAL_VLLM_MODEL else model,
        "up": up,
        "loaded": loaded,
    }
    if up:
        out["latency_ms"] = round((time.time() - t0) * 1000)
    if up and not loaded:
        out["note"] = f"/v1/models 无 {model}（现有: {', '.join(ids[:6]) or '空'}）"
    if local and local.get("enabled") is False:
        out["note"] = "ai.fallback.enabled=false（配置里已停用）"
    return out


def chain_roles(mode: str, lock: str) -> Dict[str, Any]:
    """按 ``ai.primary`` 给三档排序并贴角色标签（纯函数，看板标题直接用）。

    **回落副本**：正本是引擎 ``src/ai/ai_primary_summary.chain_roles``（经
    ``/api/setup/ai-primary/summary`` 取，见 ``collect_primary_summary``）；本推送器是
    独立进程不 import src，引擎不可达时才用这份按 overlay 派生。改语义先改正本，
    门禁 ``tests/test_ai_primary_mode_copy_gate.py`` 会对两份输出做一致性比对。

    local/local_only：① 本地主链 → ② 云端回落（local_only 不回落）→ ③ 云 key 池；
    cloud：① 云主链 → ② 云 key 池 → ③ 本地兜底。
    """
    mode = (mode or "cloud").strip().lower()
    lock = (lock or "").strip().lower()
    if mode in ("local", "local_only"):
        order = ["local", "cloud", "pool"]
        roles = {
            "local": "① 主链 · 本地 vLLM",
            "cloud": ("② 不回落（隐私档，本地失败落 canned）" if mode == "local_only"
                      else "② 回落 · 云端"),
            "pool": "③ 云端备用 key",
        }
    else:
        order = ["cloud", "pool", "local"]
        roles = {"cloud": "① 主链 · 云端", "pool": "② 备用 key · 云端", "local": "③ 兜底 · 本地 vLLM"}
    return {
        "mode": mode, "lock": lock or None, "order": order, "roles": roles,
        "mode_label": {"local": "本地主链（可回落云端）", "local_only": "本地主链（严格隐私，不回落）",
                       "cloud": "云端主链"}.get(mode, mode),
    }


def _spot(url: Any) -> Dict[str, Any]:
    """把端点 URL 翻成「算力落点」标签（配置即真相）。"""
    u = str(url or "")
    if "siliconflow" in u:
        return {"backend": "硅基流动 ☁️", "cloud": True}
    if "deepseek" in u:
        return {"backend": "DeepSeek 官方 ☁️", "cloud": True}
    m = re.search(r"192\.168\.0\.(\d+)", u)
    if m:
        return {"backend": f"局域网 .{m.group(1)} 🖥️", "cloud": False}
    return {"backend": u or "未配置", "cloud": False}


def collect_functions(overlay: Dict[str, Any], translation_usage: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """功能 × 算力落点 × 迁移状态真值表（老板问「为什么没对齐云端」的界面化答案）。

    chat/翻译/识图三行从实例 overlay 现读（今晚已切的会如实变 ☁️）；
    声音克隆/生图/ASR/SER/嵌入 的落点与「不切/待切原因」按实施70 §能力对齐结论静态标注
    ——它们的配置派生链太深，等真正迁移时再改成配置现读。"""
    ai = (overlay.get("ai") or {}) if isinstance(overlay.get("ai"), dict) else {}
    vision = (overlay.get("vision") or {}) if isinstance(overlay.get("vision"), dict) else {}
    tr = ((overlay.get("translation") or {}).get("engines") or {}) if isinstance(overlay.get("translation"), dict) else {}
    order = [str(x) for x in (tr.get("order") or [])]
    fns: List[Dict[str, Any]] = []
    # 主对话：落点看 ai.primary（local* 时是 ai.fallback 端点，不是 ai.base_url），
    # 说明写实际降级顺序——08-27 这里写死「备胎 DeepSeek → 三线 173」，主链切本地后仍在说老话
    mode = str(ai.get("primary") or "cloud").strip().lower()
    lock = str(ai.get("primary_lock") or "").strip().lower()
    fb = ai.get("fallback") if isinstance(ai.get("fallback"), dict) else {}
    cloud_vendor = _vendor_of(ai.get("base_url"))
    pool_keys = [k for k in ((ai.get("key_pool") or {}).get("keys") or []) if isinstance(k, dict)]
    pool_vendor = _vendor_of(pool_keys[0].get("base_url") or ai.get("base_url")) if pool_keys else ""
    lock_txt = f"锁 {lock}" if lock else "未设锁"
    if mode in ("local", "local_only"):
        fallback_txt = ("本地失败不回落云端（隐私档）→ canned" if mode == "local_only"
                        else f"回落 {cloud_vendor} {ai.get('model') or ''}".rstrip()
                        + (f" → key 池 {pool_vendor}" if pool_vendor else "") + " → canned")
        fns.append({"name": "主对话 LLM", **_spot(fb.get("base_url")),
                    "detail": f"{fb.get('model') or ''} · 档位 {mode} · {lock_txt}".strip(" ·"),
                    "note": fallback_txt})
    else:
        fns.append({"name": "主对话 LLM", **_spot(ai.get("base_url")),
                    "detail": f"{ai.get('model') or ''} · 档位 cloud · {lock_txt}".strip(" ·"),
                    "note": (f"key 池 {pool_vendor} → " if pool_vendor else "")
                            + f"本地兜底 {_spot(fb.get('base_url'))['backend']} {fb.get('model') or ''}".rstrip()
                            + " → canned"})
    tr_label = " → ".join(
        (f"{cloud_vendor}(经主链)" if o == "ai" else ("LAN 176 hy-mt2" if o == "ollama_mt" else o)) for o in order
    ) or "未配置"
    tr_usage = ""
    if isinstance(translation_usage, dict):
        try:
            parts = []
            for name, st in translation_usage.items():
                if isinstance(st, dict) and st.get("attempts"):
                    parts.append(f"{name}:{st.get('attempts')}次")
            tr_usage = " ".join(parts[:4])
        except Exception:
            tr_usage = ""
    fns.append({"name": "翻译 MT", "backend": tr_label,
                "cloud": bool(order) and order[0] == "ai" and bool(_spot(ai.get("base_url"))["cloud"]),
                "detail": tr_usage, "note": "order 首位为准，后位是兜底"})
    fns.append({"name": "识图 VLM", **_spot(vision.get("base_url")),
                "detail": str(vision.get("model") or ""),
                "note": "覆盖图片理解/图片翻译 OCR/出图后验"})
    fns.append({"name": "嵌入向量", **_spot(ai.get("embedding_base_url")),
                "detail": str(ai.get("embedding_model") or "bge-m3"),
                "note": "硅基有同款 bge-m3 可切；云端多 ~200ms/次无质量增益，暂留 LAN"})
    # ASR / SER / TTS 落点从 overlay 现读（09-17 前写死 .176 / .104；ASR 已迁 .198 官网仍画 .176）
    ep = media_endpoints(overlay)
    vr = overlay.get("voice_recognition") if isinstance(overlay.get("voice_recognition"), dict) else {}
    fns.append({"name": "语音识别 ASR", **_spot(ep["asr"]),
                "detail": f"whisper {vr.get('model') or 'large-v3-turbo'}（{ep['asr'].replace('http://', '')}）",
                "note": "硅基仅 SenseVoice（中粤英日韩）——泰/越/印尼等客户语音会听不懂，云端无同级多语模型，硬阻塞"})
    fns.append({"name": "声音克隆 TTS", **_spot(ep["tts"]),
                "detail": f"IndexTTS-2（{ep['tts'].replace('http://', '')}）",
                "note": "140:7852 CosyVoice 已于 08-29 停；回落/探针勿再打 140"})
    fns.append({"name": "生图（人设自拍）", "backend": "局域网 .176 🖥️", "cloud": False,
                "detail": "ComfyUI + PuLID 锁脸",
                "note": "云端无 PuLID 同款人脸一致性——切云=每张自拍换一张脸、人设穿帮；相册存货优先不受影响"})
    fns.append({"name": "生图（物体/场景图）", "backend": "局域网 .176 🖥️", "cloud": False,
                "detail": "ComfyUI",
                "note": "可切硅基 Qwen-Image/Kolors（P1 混合路由：自拍留本地、物体图走云）"})
    fns.append({"name": "语音情绪 SER", **_spot(ep["ser"]),
                "detail": f"emotion2vec（{ep['ser'].replace('http://', '')}）",
                "note": "云端无语音情绪 API，本地 CPU 兜底已在"})
    return fns


def collect_primary_summary(secrets: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """引擎单一口径 ``GET /api/setup/ai-primary/summary``（档位/锁/顺序/角色/主链一句话）。
    引擎不可达或老版本无此接口 → None，调用方回落 overlay 派生的 ``chain_roles``。"""
    tok = secrets.get("engine_token") or ""
    if not tok:
        return None
    j = _get("http://127.0.0.1:18799/api/setup/ai-primary/summary", timeout=5.0,
             headers={"Authorization": f"Bearer {tok}"})
    if isinstance(j, dict) and j.get("ok") and isinstance(j.get("order"), list):
        return {k: j.get(k) for k in ("mode", "effective", "lock", "order", "roles", "mode_label",
                                      "primary_text", "chain_text", "billing_provider")}
    return None


def collect_engine(secrets: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"up": _get_status("http://127.0.0.1:18799/login", timeout=4.0) == 200}
    tok = secrets.get("engine_token") or ""
    if not tok:
        return out
    h = {"Authorization": f"Bearer {tok}"}
    rt = _get("http://127.0.0.1:18799/api/workspace/ai-runtime-status", timeout=5.0, headers=h)
    if isinstance(rt, dict):
        # 白名单透传（06:15 按实测键形状修正：实际键=ok/degraded/mode/primary/channels/...）；
        # 该端点本就「无敏感字段」，仍只挑降级语义相关键，防未来字段膨胀外泄
        keep = {k: rt[k] for k in ("degraded", "mode", "primary", "reason", "tier",
                                   "since", "detail") if k in rt}
        out["runtime"] = keep
        if isinstance(rt.get("degraded"), bool):
            out["degraded"] = rt["degraded"]
    cc = _get("http://127.0.0.1:18799/api/setup/cloud-credentials", timeout=8.0, headers=h)
    if isinstance(cc, dict) and isinstance(cc.get("usage"), dict):
        out["usage"] = cc["usage"]
    return out


def collect_translation_usage(secrets: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """引擎 metrics 的翻译引擎用量段（60s 慢车道——metrics 端点有全局聚合开销，10s 轮询是骚扰）。"""
    tok = secrets.get("engine_token") or ""
    if not tok:
        return None
    j = _get("http://127.0.0.1:18799/api/workspace/metrics", timeout=8.0,
             headers={"Authorization": f"Bearer {tok}"})
    if not isinstance(j, dict):
        return None
    te = j.get("translation_engines")
    if isinstance(te, dict):
        # 兼容 {engines:{...}} 与 {name:{...}} 两种形状
        inner = te.get("engines") if isinstance(te.get("engines"), dict) else te
        return inner if isinstance(inner, dict) else None
    return None


def build_snapshot(secrets: Dict[str, Any], cloud_cache: Dict[str, Any],
                   overlay: Dict[str, Any], translation_usage: Optional[Dict[str, Any]],
                   primary_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    hosts = collect_hosts()
    chain: Dict[str, Any] = dict(cloud_cache)
    chain["local"] = collect_local_tier(secrets)
    # 档位/顺序/角色：首选引擎单一口径（effective=活体 AIClient 实际档位），否则按 overlay 派生
    if primary_summary:
        roles = dict(primary_summary)
        roles["mode"] = str(primary_summary.get("effective") or primary_summary.get("mode") or "cloud")
        roles["source"] = "engine_summary"
    else:
        roles = chain_roles(str(secrets.get("mode") or "cloud"), str(secrets.get("lock") or ""))
        roles["source"] = "overlay"
    chain.update(roles)
    for k in ("cloud", "pool", "local"):
        if isinstance(chain.get(k), dict):
            chain[k]["role"] = roles["roles"].get(k, "")
            chain[k]["rank"] = roles["order"].index(k) + 1 if k in roles["order"] else None
    # 旧看板键兼容（官网与 pusher 不同步发版）：primary 一律指云主链节点
    chain["primary"] = chain.get("cloud") or {}
    return {
        "ts": int(time.time()),
        "src": "117-pusher/v3",
        "chain": chain,
        "engine": collect_engine(secrets),
        "functions": collect_functions(overlay, translation_usage),
        "hosts": hosts,
        "comfy": collect_comfy(),
        "hub": collect_hub(),
        "media": collect_media(overlay),
    }


def push(snapshot: Dict[str, Any], report_key: str) -> bool:
    body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{SITE}/api/admin/compute-status", data=body, method="POST",
        headers={"Content-Type": "application/json", "x-compute-key": report_key})
    try:
        with urllib.request.urlopen(req, timeout=10.0) as r:
            return int(r.status) == 200
    except Exception as e:
        print(f"[push-fail] {e}", flush=True)
        return False


def main() -> int:
    once = "--once" in sys.argv
    # 单例锁：绑不上 = 已有实例在跑，静默退出（计划任务每分钟拉活的幂等前提）
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", SINGLETON_PORT))
        lock.listen(1)
    except OSError:
        return 0
    secrets = load_secrets()
    if not secrets.get("report_key"):
        print("[fatal] report key missing: " + str(REPORT_KEY_FILE), flush=True)
        return 1
    print(f"[start] compute pusher → {SITE} interval={PUSH_INTERVAL}s", flush=True)
    cloud_cache: Dict[str, Any] = {}
    overlay: Dict[str, Any] = {}
    tr_usage: Optional[Dict[str, Any]] = None
    primary_summary: Optional[Dict[str, Any]] = None
    slow_ts = 0.0
    while True:
        try:
            # 慢车道（60s）：云端探活 / 引擎 metrics / overlay 重读（配置改了→落点表自动跟）
            # 切档 nudge 强制走慢车道——装载点写文件，避免 ``--once`` 撞单例锁空跑
            if time.time() - slow_ts >= CLOUD_INTERVAL or not cloud_cache or consume_board_nudge():
                secrets = load_secrets()  # key 轮换/新增也一并跟上
                cloud_cache = collect_cloud(secrets)
                overlay = _load_yaml(INSTANCE_CFG_DIR / "config.local.yaml")
                tr_usage = collect_translation_usage(secrets)
                primary_summary = collect_primary_summary(secrets)
                slow_ts = time.time()
            snap = build_snapshot(secrets, cloud_cache, overlay, tr_usage, primary_summary)
            ok = push(snap, secrets["report_key"])
            print(f"[push] ts={snap['ts']} ok={ok}", flush=True)
        except Exception as e:  # 收集/组装的任何意外都不许杀循环
            print(f"[loop-err] {e}", flush=True)
        if once:
            return 0
        time.sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
