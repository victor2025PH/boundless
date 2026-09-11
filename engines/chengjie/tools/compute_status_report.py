# -*- coding: utf-8 -*-
"""算力调度快照推送器（实施70 2026-08-27 老板指令：官网实时算力看板）。

117 上常驻循环（计划任务 Boundless-compute-pusher 每分钟拉活；本进程 18797 端口
单例锁防重复），每 ~10s 收集一份集群算力全景快照 → POST 官网
``/api/admin/compute-status``（头 ``x-compute-key``，密钥在
``D:\\chengjie-instances\\.ops\\compute_report_key.txt``，绝不入库/入 git）。

收集面（任一收集器失败=该面标不可达，绝不崩循环）：
- 出话链三档：硅基流动（GET /v1/models 探活 + /v1/user/info 余额）→ DeepSeek 官方
  （/user/balance 余额即探活）→ 本地 173（/api/ps 看 qwen2.5:14b 驻留）。
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

HOSTS = [
    {"host": "zhongshu-176", "ip": "192.168.0.176", "total_gb": 32.0},
    {"host": "yunsheng-173", "ip": "192.168.0.173", "total_gb": 32.0},
    {"host": "tingxie-140", "ip": "192.168.0.140", "total_gb": 12.0},
    {"host": "kouxing-198", "ip": "192.168.0.198", "total_gb": 12.0},
]


def _get(url: str, timeout: float = 4.0, headers: Optional[Dict[str, str]] = None) -> Optional[Any]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
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


def load_secrets() -> Dict[str, str]:
    """从实例配置提取（只进内存，永不打印/外送）：主链 key、池 key、引擎 Bearer、上报密钥。"""
    out = {"sf_key": "", "ds_key": "", "engine_token": "", "report_key": ""}
    overlay = _load_yaml(INSTANCE_CFG_DIR / "config.local.yaml")
    main = _load_yaml(INSTANCE_CFG_DIR / "config.yaml")
    ai = (overlay.get("ai") or {}) if isinstance(overlay.get("ai"), dict) else {}
    base = str(ai.get("base_url") or "")
    if "siliconflow" in base:
        out["sf_key"] = str(ai.get("api_key") or "")
    for item in ((ai.get("key_pool") or {}).get("keys") or []):
        if isinstance(item, dict) and "deepseek" in str(item.get("base_url") or ""):
            out["ds_key"] = str(item.get("api_key") or "")
            break
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


def collect_hosts() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for h in HOSTS:
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


def collect_media() -> Dict[str, Any]:
    asr = _get("http://192.168.0.176:8765/health", timeout=3.0)
    # 2026-09-11：140:7852 CosyVoice 已停（RST）。克隆探针跟现役 104:7865 IndexTTS-2。
    # 健康形状两家不同：7852={ok,models_loaded}；7865={status:"ok",model_loaded}。
    tts = _get("http://192.168.0.104:7865/health", timeout=3.0)
    tts_ok = bool(tts) and (
        tts.get("ok") is True
        or str(tts.get("status") or "").lower() in ("ok", "healthy", "ready")
    )
    tts_loaded = bool(tts) and (
        tts.get("models_loaded") is True or tts.get("model_loaded") is True
    )
    tts_row = {"ok": tts_ok, "models_loaded": tts_loaded}
    return {
        "asr": {"ok": bool(asr and asr.get("status") == "ok"),
                "asr_loaded": bool(asr and asr.get("asr_loaded")),
                "ser_loaded": bool(asr and asr.get("ser_loaded"))},
        "tts104": tts_row,
        "tts140": tts_row,  # 旧看板键兼容，语义已是 104
    }


def collect_cloud(secrets: Dict[str, str]) -> Dict[str, Any]:
    """云端两档探活（60s 节流由调用方管）。"""
    out: Dict[str, Any] = {}
    # 主胎：硅基流动 —— /v1/models 探活（余额接口 /v1/user/info 已 410 下线，无 API 可查，
    # 2026-08-27 实测；余额水位只能上官网控制台看，看板如实不显示而非造数）
    sf: Dict[str, Any] = {"vendor": "siliconflow", "model": "deepseek-ai/DeepSeek-V3.2"}
    if secrets.get("sf_key"):
        h = {"Authorization": f"Bearer {secrets['sf_key']}"}
        t0 = time.time()
        j = _get("https://api.siliconflow.cn/v1/models", timeout=8.0, headers=h)
        sf["ok"] = j is not None
        if j is not None:
            sf["latency_ms"] = round((time.time() - t0) * 1000)
    else:
        sf["ok"] = None
        sf["note"] = "未在配置中找到硅基 key"
    out["primary"] = sf
    # 备胎：DeepSeek 官方 —— /user/balance 即探活
    ds: Dict[str, Any] = {"vendor": "deepseek-official", "model": "deepseek-v4-flash"}
    if secrets.get("ds_key"):
        j = _get("https://api.deepseek.com/user/balance", timeout=8.0,
                 headers={"Authorization": f"Bearer {secrets['ds_key']}"})
        ds["ok"] = bool(j and j.get("is_available"))
        try:
            for b in (j or {}).get("balance_infos") or []:
                if b.get("currency") == "CNY":
                    ds["balance"] = f"¥{b.get('total_balance')}"
        except Exception:
            pass
    else:
        ds["ok"] = None
        ds["note"] = "未在池配置中找到 DeepSeek key"
    out["pool"] = ds
    return out


def collect_local_tier(hosts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """三线：173 本地兜底（qwen2.5:14b）。直接从已收集的 hosts 行推导，零额外请求。"""
    row = next((h for h in hosts if h.get("ip") == "192.168.0.173"), None)
    up = bool(row and row.get("reachable"))
    loaded = bool(row and any(str(m.get("name") or "").startswith("qwen2.5:14b")
                              for m in (row.get("models") or [])))
    return {"endpoint": "192.168.0.173:11434", "model": "qwen2.5:14b", "up": up, "loaded": loaded}


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
    fns.append({"name": "主对话 LLM", **_spot(ai.get("base_url")),
                "detail": str(ai.get("model") or ""),
                "note": "备胎 DeepSeek 官方 → 三线本地 173"})
    tr_label = " → ".join(
        ("硅基(经主链)" if o == "ai" else ("LAN 176 hy-mt2" if o == "ollama_mt" else o)) for o in order
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
                "cloud": bool(order) and order[0] == "ai" and "siliconflow" in str(ai.get("base_url") or ""),
                "detail": tr_usage, "note": "order 首位为准，后位是兜底"})
    fns.append({"name": "识图 VLM", **_spot(vision.get("base_url")),
                "detail": str(vision.get("model") or ""),
                "note": "覆盖图片理解/图片翻译 OCR/出图后验"})
    fns.append({"name": "嵌入向量", **_spot(ai.get("embedding_base_url")),
                "detail": str(ai.get("embedding_model") or "bge-m3"),
                "note": "硅基有同款 bge-m3 可切；云端多 ~200ms/次无质量增益，暂留 LAN"})
    fns.append({"name": "语音识别 ASR", "backend": "局域网 .176 🖥️", "cloud": False,
                "detail": "whisper large-v3-turbo",
                "note": "硅基仅 SenseVoice（中粤英日韩）——泰/越/印尼等客户语音会听不懂，云端无同级多语模型，硬阻塞"})
    fns.append({"name": "声音克隆 TTS", "backend": "局域网 .104 🖥️", "cloud": False,
                "detail": "IndexTTS-2（104:7865）",
                "note": "140:7852 CosyVoice 已于 08-29 停；回落/探针勿再打 140"})
    fns.append({"name": "生图（人设自拍）", "backend": "局域网 .176 🖥️", "cloud": False,
                "detail": "ComfyUI + PuLID 锁脸",
                "note": "云端无 PuLID 同款人脸一致性——切云=每张自拍换一张脸、人设穿帮；相册存货优先不受影响"})
    fns.append({"name": "生图（物体/场景图）", "backend": "局域网 .176 🖥️", "cloud": False,
                "detail": "ComfyUI",
                "note": "可切硅基 Qwen-Image/Kolors（P1 混合路由：自拍留本地、物体图走云）"})
    fns.append({"name": "语音情绪 SER", "backend": "局域网 .176 🖥️", "cloud": False,
                "detail": "emotion2vec", "note": "云端无语音情绪 API，本地 CPU 兜底已在"})
    return fns


def collect_engine(secrets: Dict[str, str]) -> Dict[str, Any]:
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


def collect_translation_usage(secrets: Dict[str, str]) -> Optional[Dict[str, Any]]:
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


def build_snapshot(secrets: Dict[str, str], cloud_cache: Dict[str, Any],
                   overlay: Dict[str, Any], translation_usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    hosts = collect_hosts()
    chain = dict(cloud_cache)
    chain["local"] = collect_local_tier(hosts)
    return {
        "ts": int(time.time()),
        "src": "117-pusher/v2",
        "chain": chain,
        "engine": collect_engine(secrets),
        "functions": collect_functions(overlay, translation_usage),
        "hosts": hosts,
        "comfy": collect_comfy(),
        "hub": collect_hub(),
        "media": collect_media(),
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
    slow_ts = 0.0
    while True:
        try:
            # 慢车道（60s）：云端探活 / 引擎 metrics / overlay 重读（配置改了→落点表自动跟）
            if time.time() - slow_ts >= CLOUD_INTERVAL or not cloud_cache:
                secrets = load_secrets()  # key 轮换/新增也一并跟上
                cloud_cache = collect_cloud(secrets)
                overlay = _load_yaml(INSTANCE_CFG_DIR / "config.local.yaml")
                tr_usage = collect_translation_usage(secrets)
                slow_ts = time.time()
            snap = build_snapshot(secrets, cloud_cache, overlay, tr_usage)
            ok = push(snap, secrets["report_key"])
            print(f"[push] ts={snap['ts']} ok={ok}", flush=True)
        except Exception as e:  # 收集/组装的任何意外都不许杀循环
            print(f"[loop-err] {e}", flush=True)
        if once:
            return 0
        time.sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
