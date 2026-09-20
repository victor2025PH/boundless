# -*- coding: utf-8 -*-
"""一键诊断包（P2-198，2026-07-31）。

背景：198 客户机排障时发现桌面版几乎没有可用运行日志（backend.log 10 小时 41KB、
边车日志各几百字节），本次全部关键问题（翻译跳过/分条/复读）在日志里零痕迹，
只能远程拉数据库反推。「客户报障 → 传诊断包 → 定位」需要一个一键出口。

设计（纯函数核心，路由只做薄适配）：
  - ``redact_secrets_text``：YAML/JSON 两种形态的密钥值整行打码——api_key/api_hash/
    token/secret/password/credential/cookie 族，**宁可多打不漏打**（诊断包会被贴进
    工单/聊天，明文密钥一旦出去就收不回）。
  - ``build_diagnostic_bundle``：内存 zip = meta.json（版本/时间/数据根）+ config/*
    （打码后）+ logs/*（每文件取尾部 ``log_tail_kb``，总量 ``total_cap_mb`` 封顶）。
    绝不抛：单文件读失败跳过（诊断工具自己崩=最讽刺的事故）。
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

# YAML 行：`  api_key: xxx` / `auth_token: "xxx"`
_YAML_SECRET_RE = re.compile(
    r"(?im)^(\s*[\w.\-]*(?:api_key|api_hash|token|secret|password|passwd|credential|cookie)"
    r"[\w\-]*\s*:\s*)(?!\s*$)(.+)$")
# JSON 值：`"token": "xxx"`（notify_webhooks.json 等）
_JSON_SECRET_RE = re.compile(
    r'(?i)("[\w\-]*(?:api_key|api_hash|token|secret|password|passwd|credential|cookie)'
    r'[\w\-]*"\s*:\s*")([^"]*)(")')

# 进包的配置文件白名单（诊断需要的最小集；勿放 *.db——体积大且含客户消息原文）
_CONFIG_FILES = (
    "config.yaml", "config.local.yaml", "notify_webhooks.json",
    "local_trial.json", "protocol_autoreply.json",
)
# 日志目录里进包的文件模式
_LOG_SUFFIXES = (".log", ".json", ".txt")


def redact_secrets_text(text: str) -> str:
    """把配置文本里的密钥值打码为 ***（保留键名与结构，诊断可读性不受影响）。"""
    s = str(text or "")
    s = _YAML_SECRET_RE.sub(lambda m: m.group(1) + "***", s)
    s = _JSON_SECRET_RE.sub(lambda m: m.group(1) + "***" + m.group(3), s)
    return s


def _tail_bytes(p: Path, cap_bytes: int) -> bytes:
    """取文件尾部 cap 字节（日志新内容在尾部）；读失败返回空。"""
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > cap_bytes:
                fh.seek(size - cap_bytes)
            return fh.read(cap_bytes)
    except Exception:
        return b""


def data_freshness_meta(
    config_dir: Optional[Path], logs_dir: Optional[Path],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """数据目录年龄标注 → 并进 meta.json 的 ``data_freshness`` 段。绝不抛。

    背景（0902 5GZHWT 实锤）：用户卸载勾「彻底删除」重装后自动上传的包是
    **冷启动包**——日志从当次启动才开始，案发现场早已随数据清除消失。值守把
    包拉回本地读完 backend.log 才发现白跑一趟。本函数让服务端/值守看 meta
    第一眼就能判「这个包覆盖多久」：
    - ``data_dir_age_hours``：config 目录最老文件 mtime 距今（数据目录年龄；
      冷启动包 ≈ 0）；
    - ``logs_span_hours`` / ``logs_earliest`` / ``logs_latest``：logs 目录及
      一层子目录内日志文件 mtime 跨度（包能覆盖的时间窗）；
    - ``cold_start_suspect``：两者都 < 24h → True（提示先问「是否刚重装/清过
      数据」再决定要不要拉包分析）。
    """
    ts_now = float(now if now is not None else time.time())
    out: Dict[str, Any] = {}

    def _mtimes(root: Optional[Path], depth1: bool) -> list:
        vals = []
        try:
            if not root or not Path(root).is_dir():
                return vals
            for p in Path(root).iterdir():
                try:
                    if p.is_file():
                        vals.append(p.stat().st_mtime)
                    elif depth1 and p.is_dir():
                        for q in p.iterdir():
                            if q.is_file():
                                vals.append(q.stat().st_mtime)
                except Exception:
                    continue
        except Exception:
            pass
        return vals

    try:
        cfg_times = _mtimes(config_dir, depth1=False)
        log_times = _mtimes(logs_dir, depth1=True)
        if cfg_times:
            out["data_dir_age_hours"] = round(
                max(0.0, ts_now - min(cfg_times)) / 3600, 1)
        if log_times:
            lo, hi = min(log_times), max(log_times)
            out["logs_span_hours"] = round(max(0.0, hi - lo) / 3600, 1)
            out["logs_earliest"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(lo))
            out["logs_latest"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(hi))
        if out:
            age = out.get("data_dir_age_hours")
            span = out.get("logs_span_hours")
            out["cold_start_suspect"] = bool(
                (age is None or age < 24.0)
                and (span is None or span < 24.0))
    except Exception:
        return {}
    return out


def build_diagnostic_bundle(
    *,
    config_dir: Optional[Path],
    logs_dir: Optional[Path],
    meta: Optional[Dict[str, Any]] = None,
    log_tail_kb: int = 256,
    total_cap_mb: int = 8,
    extra_files: Optional[Dict[str, Path]] = None,
    extra_tail_kb: int = 2048,
) -> bytes:
    """打一个内存 zip 诊断包，返回字节串。绝不抛（内部逐文件软失败）。

    ``extra_files``（P2-12 B38 批 2026-08-22）：arcname → 路径的补充清单，每文件
    取尾部 ``extra_tail_kb``（默认 2MB）。3DTSV9 实战暴露的缺口：桌面部署主日志
    （app.log/运行时 file handler 落点）常**不在** logs_dir 一级——包里只有
    fatal/sidecar，主日志零痕迹，排障只能拉库反推。调用方经运行时 logger 树
    收集真实落点喂进来（见 diag_upload.runtime_log_files）。"""
    tail_cap = max(4, int(log_tail_kb)) * 1024
    total_cap = max(1, int(total_cap_mb)) * 1024 * 1024
    extra_cap = max(64, int(extra_tail_kb)) * 1024
    total = 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # meta：版本/时间/落点（排障第一眼要看的）
        try:
            m = dict(meta or {})
            m.setdefault("generated_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            # 数据目录年龄标注（0902 5GZHWT 冷启动包实锤）：让「这个包覆盖
            # 多久」在 meta 第一眼可判，免得拉回本地读完日志才发现无现场。
            fresh = data_freshness_meta(config_dir, logs_dir)
            if fresh:
                m.setdefault("data_freshness", fresh)
            zf.writestr("meta.json", json.dumps(m, ensure_ascii=False, indent=2))
        except Exception:
            zf.writestr("meta.json", "{}")
        # 补充清单（主日志等）：先于 logs_dir 写入——它是本包存在的头号理由，
        # 不能被一堆边角日志把总量预算吃光后挤掉
        for arc, path in dict(extra_files or {}).items():
            if total >= total_cap:
                break
            try:
                p = Path(path)
                if not p.is_file():
                    continue
                data = _tail_bytes(p, extra_cap)
                if not data:
                    continue
                total += len(data)
                zf.writestr(str(arc), data)
            except Exception:
                continue
        # 配置（白名单 + 打码）
        if config_dir is not None:
            for name in _CONFIG_FILES:
                try:
                    p = Path(config_dir) / name
                    if not p.is_file():
                        continue
                    text = p.read_text(encoding="utf-8", errors="replace")
                    data = redact_secrets_text(text).encode("utf-8")
                    total += len(data)
                    if total > total_cap:
                        break
                    zf.writestr(f"config/{name}", data)
                except Exception:
                    continue
        # 日志（浅层一级 + 尾部截断）
        if logs_dir is not None:
            try:
                entries = sorted(
                    [p for p in Path(logs_dir).iterdir() if p.is_file()
                     and p.suffix.lower() in _LOG_SUFFIXES],
                    key=lambda p: p.stat().st_mtime, reverse=True)
            except Exception:
                entries = []
            for p in entries:
                if total >= total_cap:
                    break
                data = _tail_bytes(p, tail_cap)
                if not data:
                    continue
                total += len(data)
                try:
                    zf.writestr(f"logs/{p.name}", data)
                except Exception:
                    continue
    return buf.getvalue()


__all__ = ["build_diagnostic_bundle", "redact_secrets_text"]
