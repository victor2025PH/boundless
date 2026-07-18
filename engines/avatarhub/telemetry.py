# -*- coding: utf-8 -*-
"""telemetry.py — 隐私优先的匿名安装健康回执。

设计红线（务必遵守）：
  - **默认关闭**：未明确开启则只在本地写回执、绝不外发（opt-in）。
  - **最小化**：只采组件级成败/字节/耗时/错误【类名】、源【主机名】、档位、版本、粗粒度 GPU。
    绝不采集文件路径、用户名、错误信息原文（可能含路径）、IP、任何可定位个人的字段。
  - **可离线**：无网络/无 telemetry_url 时一切照常，回执只落本地供用户自查。
  - **可关闭/可重置**：环境变量 AVATARHUB_TELEMETRY=0 强制关；anon_id 仅为去重、随机、可删。

回执去向：始终写 BASE/runtime/telemetry/<ts>.json（本地、可审计）；仅当 enabled() 且
manifest.telemetry_url 存在时，后台 best-effort POST（短超时、永不抛错、永不阻塞安装）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import app_config

TELE_DIR = app_config.BASE / "runtime" / "telemetry"
TELE_CONF = TELE_DIR / "config.json"
SCHEMA = 1
KEEP_LOCAL = 50            # 本地回执最多保留份数（滚动清理，避免无限堆积）


def _load_conf() -> dict:
    try:
        return json.loads(TELE_CONF.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_conf(d: dict):
    try:
        TELE_DIR.mkdir(parents=True, exist_ok=True)
        TELE_CONF.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def enabled() -> bool:
    """是否允许外发：环境变量优先（AVATARHUB_TELEMETRY=0/off/false 关，1/on/true 开），
    否则看本地设置 config.json["enabled"]，未设视为 False（默认关闭 / opt-in）。"""
    env = os.environ.get("AVATARHUB_TELEMETRY", "").strip().lower()
    if env in ("0", "off", "false", "no"):
        return False
    if env in ("1", "on", "true", "yes"):
        return True
    return bool(_load_conf().get("enabled", False))


def set_enabled(on: bool):
    c = _load_conf()
    c["enabled"] = bool(on)
    _save_conf(c)


def anon_id() -> str:
    """随机匿名标识（仅供服务端对回执去重，不含任何个人信息）；缺失则生成并持久化。"""
    c = _load_conf()
    aid = c.get("anon_id")
    if not aid:
        aid = uuid.uuid4().hex
        c["anon_id"] = aid
        _save_conf(c)
    return aid


def _host(src: str) -> str:
    """只取来源主机名（丢弃路径/查询，避免泄露目录结构）；本地路径记为 'local'。"""
    try:
        if src.lower().startswith(("http://", "https://")):
            return urlparse(src).hostname or "?"
    except Exception:
        pass
    return "local"


class Recorder:
    """累计一次安装/更新/回滚会话的组件级结果，最后 finalize 成匿名回执。"""

    def __init__(self):
        self.t0 = time.time()
        self.items: list[dict] = []

    def add(self, cid: str, ok: bool, size_bytes: int, secs: float, err: str = ""):
        self.items.append({"cid": cid, "ok": bool(ok),
                           "bytes": int(size_bytes or 0), "secs": round(secs, 2),
                           "err": err[:40]})   # err 只应传【类名】，再截断以防意外

    def finalize(self, kind: str, edition: str, manifest: dict,
                 sources: list[str] | None = None, channel: str = "",
                 gpu: dict | None = None) -> dict:
        ok = sum(1 for it in self.items if it["ok"])
        rec = {
            "schema": SCHEMA,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "manifest_version": manifest.get("version", ""),
            "channel": channel or os.environ.get("AVATARHUB_CHANNEL", ""),
            "edition": edition or "",
            "platform": manifest.get("platform", ""),
            "sources": [_host(s) for s in (sources or [])],
            "total_bytes": sum(it["bytes"] for it in self.items),
            "total_secs": round(time.time() - self.t0, 1),
            "ok": ok,
            "fail": len(self.items) - ok,
            "items": self.items,
        }
        if gpu:
            rec["gpu"] = str(gpu.get("name", ""))[:48]
            rec["vram_gb"] = round((gpu.get("total_mb", 0) or 0) / 1024)
        if enabled():
            rec["anon_id"] = anon_id()      # 仅在开启外发时附带去重 id
        return rec


def _write_local(receipt: dict) -> Path | None:
    try:
        TELE_DIR.mkdir(parents=True, exist_ok=True)
        ts = receipt.get("ts", "").replace(":", "").replace("-", "")[:15] or str(int(time.time()))
        out = TELE_DIR / f"{ts}_{receipt.get('kind','x')}.json"
        out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        files = sorted(TELE_DIR.glob("*_*.json"))
        for old in files[:-KEEP_LOCAL]:        # 滚动清理旧回执
            old.unlink(missing_ok=True)
        return out
    except Exception:
        return None


def submit(receipt: dict, manifest: dict, log=None):
    """始终写本地回执；仅当 enabled() 且有 telemetry_url 时后台 best-effort 外发（永不阻塞/抛错）。"""
    _write_local(receipt)
    url = (manifest.get("telemetry_url") or "").strip()
    if not (enabled() and url.startswith(("http://", "https://"))):
        return

    def work():
        try:
            data = json.dumps(receipt, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).close()
            if log:
                log("已发送匿名安装回执（用于改进发布质量，可在维护里关闭）。")
        except Exception:
            pass        # 遥测失败绝不影响主流程

    threading.Thread(target=work, daemon=True).start()


# ═════════════════════════════════════════════════════════════════════════════
# P4 全域运营事件适配器（2026-07-18）—— 与上面的「安装健康回执」是两套互不相干的机制：
#   回执 = opt-in 的安装质量遥测（本地 runtime/telemetry/，可选外发）；
#   track() = 集团统一运营事件（契约 platform/observability/EVENT_CONTRACT.md），
#             只往本地 spool 追加 JSONL，收割/上传归收集器，本模块不联网。
#
# 要点：
#   - 仅标准库；向上定位 <仓库根>/platform/observability/emitter.py 后按文件 importlib
#     加载（顶层目录 platform 与标准库同名，绝不 import platform.*，见 emitter.py 顶注）；
#     定位/加载失败 → track() 永远 no-op 返回 ""（脱库部署无 platform/ 目录时零影响）。
#   - avatarhub 一个引擎支撑多产品（幻声 huansheng/幻影 huanying/幻颜 huanyan/通传
#     tongchuan），product_id 由调用点按业务归属显式传入，本适配器不猜。
#   - spool 目录：env EVENT_SPOOL_DIR 优先；未设置时缺省本目录 data/events/spool/。
#   - 开关：AVATARHUB_TELEMETRY=off/0/false/no 时 track() 直接 no-op（与回执共用同名
#     环境变量：显式关遥测 = 两套一起关；未设置时 track 默认开【仅本地落盘】）。
#   - fail-silent：任何异常吞掉返回 ""，埋点绝不打挂业务主路径。
#
# 自测：python telemetry.py --selftest   （全程在临时目录发射，不碰仓库 data/）
# ═════════════════════════════════════════════════════════════════════════════

# 缺省 spool：本文件所在目录（engines/avatarhub）下 data/events/spool/
DEFAULT_EVENT_SPOOL_DIR = Path(__file__).resolve().parent / "data" / "events" / "spool"
_EVENT_SPOOL_ENV = "EVENT_SPOOL_DIR"
_TRACK_OFF_VALUES = ("0", "off", "false", "no")
_EMITTER_CACHE: dict = {"mod": None, "tried": False}


def _find_emitter_path():
    """从本文件位置向上逐级找 platform/observability/emitter.py；找不到返回 None。"""
    p = Path(__file__).resolve().parent
    for _ in range(8):
        cand = p / "platform" / "observability" / "emitter.py"
        if cand.is_file():
            return cand
        if p.parent == p:
            break
        p = p.parent
    return None


def _load_emitter():
    """importlib 按文件路径加载发射器（每进程只尝试一次，失败缓存 None → 永远 no-op）。"""
    if _EMITTER_CACHE["tried"]:
        return _EMITTER_CACHE["mod"]
    _EMITTER_CACHE["tried"] = True
    try:
        import importlib.util
        path = _find_emitter_path()
        if path is None:
            return None
        spec = importlib.util.spec_from_file_location("avatarhub_boundless_emitter", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _EMITTER_CACHE["mod"] = mod
    except Exception:
        _EMITTER_CACHE["mod"] = None
    return _EMITTER_CACHE["mod"]


def track(product_id, name, props=None, **kw) -> str:
    """发射一条全域运营事件到本地 spool。成功返回 event_id，任何失败返回 ""。

    product_id 必须与事件业务归属一致（声音克隆→huansheng、直播/口型/虚拟摄像头→
    huanying、离线换脸→huanyan、同传→tongchuan），事件名三段式 namespace 必须等于
    product_id（不一致时发射器直接丢弃）。props 只放指标/枚举/ID 引用与计数时长，
    绝不放人脸/声纹/音频数据本体与文件内容（隐私红线见 EVENT_CONTRACT.md 顶部）。
    kw 透传 emit()（workspace_id / customer_id / actor / spool_dir）。
    """
    try:
        if os.environ.get("AVATARHUB_TELEMETRY", "").strip().lower() in _TRACK_OFF_VALUES:
            return ""
        mod = _load_emitter()
        if mod is None:
            return ""
        if not kw.get("spool_dir") and not os.environ.get(_EVENT_SPOOL_ENV, "").strip():
            kw["spool_dir"] = str(DEFAULT_EVENT_SPOOL_DIR)   # env 优先，缺省本引擎目录
        return mod.emit(product_id, name, props=props, **kw)
    except Exception:
        return ""


def _track_selftest() -> int:
    """--selftest：临时目录里验证 定位/发射/开关/spool 优先级/非法拒收/fail-silent。"""
    import tempfile

    failures: list = []

    def check(desc: str, ok: bool):
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            failures.append(desc)

    def read_rows(d: str) -> list:
        rows = []
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".jsonl"):
                    with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                        rows += [json.loads(ln) for ln in f if ln.strip()]
        return rows

    print("== avatarhub 事件适配器自测（telemetry.py --selftest）==")
    print("[1/6] 发射器定位与加载")
    path = _find_emitter_path()
    check(f"向上定位 emitter.py: {path}", path is not None)
    mod = _load_emitter()
    check("importlib 按文件加载成功（未 import platform.*）", mod is not None)
    check("platform 仍解析为标准库模块", __import__("platform").__name__ == "platform")
    if mod is None:
        print("== 结果：发射器缺失，track 将全程 no-op（脱库部署属正常）==")
        return 1

    saved_env = {k: os.environ.get(k) for k in ("AVATARHUB_TELEMETRY", _EVENT_SPOOL_ENV)}
    try:
        os.environ.pop("AVATARHUB_TELEMETRY", None)
        os.environ.pop(_EVENT_SPOOL_ENV, None)
        with tempfile.TemporaryDirectory(prefix="avatarhub_track_selftest_") as tmp:
            d1 = os.path.join(tmp, "a")
            print("[2/6] 注册事件发射与读回（spool_dir 显式传参）")
            eid = track("huansheng", "huansheng.voice.clone_completed",
                        {"voice_id": "selftest_voice", "sample_seconds": 6.5}, spool_dir=d1)
            rows = read_rows(d1)
            check(f"返回 event_id: {eid}", eid.startswith("evt_") and len(eid) == 30)
            check("落盘 1 行且信封字段一致",
                  len(rows) == 1 and rows[0].get("product_id") == "huansheng"
                  and rows[0].get("name") == "huansheng.voice.clone_completed"
                  and rows[0].get("props", {}).get("voice_id") == "selftest_voice"
                  and "_unregistered" not in rows[0])

            print("[3/6] product_id 与 namespace 不一致 → 拒收")
            bad = track("huanying", "huansheng.voice.clone_completed", {"voice_id": "x"},
                        spool_dir=d1)
            check("返回 \"\" 且不落盘", bad == "" and len(read_rows(d1)) == 1)

            print("[4/6] 未注册事件宽松落盘（_unregistered 标记）")
            eid2 = track("huanyan", "huanyan.selftest.pinged", {"n": 1}, spool_dir=d1)
            rows = read_rows(d1)
            check("未注册事件仍返回 event_id 且带 _unregistered",
                  bool(eid2) and len(rows) == 2 and rows[1].get("_unregistered") is True)

            print("[5/6] 开关与 spool 优先级")
            os.environ["AVATARHUB_TELEMETRY"] = "off"
            off = track("tongchuan", "tongchuan.session.started", {"session_id": "s1"},
                        spool_dir=d1)
            os.environ.pop("AVATARHUB_TELEMETRY", None)
            check("AVATARHUB_TELEMETRY=off → no-op 不落盘",
                  off == "" and len(read_rows(d1)) == 2)
            d_env = os.path.join(tmp, "env")
            os.environ[_EVENT_SPOOL_ENV] = d_env
            eid3 = track("tongchuan", "tongchuan.session.started",
                         {"session_id": "interp_1", "mode": "meeting"})
            os.environ.pop(_EVENT_SPOOL_ENV, None)
            check("未传 spool_dir 时 EVENT_SPOOL_DIR 生效",
                  bool(eid3) and len(read_rows(d_env)) == 1)
            check("缺省目录常量指向 engines/avatarhub/data/events/spool",
                  str(DEFAULT_EVENT_SPOOL_DIR).replace("\\", "/")
                  .endswith("avatarhub/data/events/spool"))

            print("[6/6] fail-silent")
            check("props 嵌套对象 → \"\"",
                  track("huanying", "huanying.live.started", {"a": {"b": 1}},
                        spool_dir=d1) == "")
            check("非法 product_id → \"\"",
                  track("wechat", "wechat.live.started", {}, spool_dir=d1) == "")
            check("多余 kw → \"\"（吞 TypeError）",
                  track("huanying", "huanying.live.started", {}, spool_dir=d1,
                        nonsense_kw=1) == "")
            check("非法发射后行数不变", len(read_rows(d1)) == 2)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    if failures:
        print(f"== 结果：{len(failures)} 项失败 ==")
        return 1
    print("== 结果：全部通过 ==")
    return 0


if __name__ == "__main__":
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8")   # Windows 重定向默认 cp936，打中文会炸
    except Exception:
        pass
    if _sys.argv[1:] == ["--selftest"]:
        _sys.exit(_track_selftest())
    print("用法: python telemetry.py --selftest   （P4 事件适配器自测；track() 用法见文内注释）")
    _sys.exit(2)
