# -*- coding: utf-8 -*-
"""出图模型保温器（2026-08-28：「要让模型一直在热加载的模式，不能等到用的时候再加载」）。

**问题**：ComfyUI 不像 Ollama 有 ``keep_alive``——它没有「预加载」接口，模型只在
**第一次真正出图**时才从磁盘装进显存（flux 全家桶实测 40-60s）。于是坐席每次点
「生成图片」都在替系统付这笔冷加载账；而同卡 Ollama 的翻译/嵌入模型会周期性把
空闲显存压回闸门线以下，配合旧的 ``ensure_vram`` 无条件 ``/free``，热缓存刚建好
就被自己拆掉（该自毁循环已在 comfy_infer.ensure_vram 修掉）。

**做法**：定期查 ``torch_vram_total``（ComfyUI 自己占的显存 = 模型在不在的代理
指标）——
  - 已常驻（≥ ``--warm-reserved``）→ **什么都不做，零 GPU 消耗**直接退出；
  - 未常驻 → 提交一张最小尺寸、1 步的图把模型顶进显存（丢弃产物）。
所以稳态下它几乎不花钱：只有服务重启 / 被别的进程挤掉之后那一次才真加载。

**刻意不做**的两件事：
  - 不抢锁：用 comfy_infer 的同一把出图互斥锁，拿不到就直接退出——保温永远给
    真出图让路（坐席在等，保温不能排在他前面）。
  - 不自动注册计划任务：**跑不跑、多久跑一次是运维决策**（与本仓 multiwin 周批 /
    AvatarPrerenderNightly 同惯例）。注册命令见下方 SOP。

用法::

    python tools/comfy_warmup.py                 # 需要才加载（稳态零消耗）
    python tools/comfy_warmup.py --check         # 只报状态，绝不加载
    python tools/comfy_warmup.py --force         # 无论如何顶一次（换模型后用）

注册（**人工决定后**执行；建议 10 分钟一次，稳态下 99% 的轮次是零消耗探测）::

    schtasks /Create /TN ComfyWarmKeep /SC MINUTE /MO 10 /F ^
      /TR "python D:\\workspace\\boundless\\engines\\chengjie\\tools\\comfy_warmup.py"

退出码恒 0（保温是尽力而为，绝不把「没保住」变成红告警——真出图自己会兜底）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import comfy_infer as ci  # noqa: E402  （复用 URL/锁/显存探针，不另造一套口径）


def _log(msg: str) -> None:
    print("[comfy_warmup] %s" % msg, flush=True)


# 让模型**不可能**常驻显存的 ComfyUI 启动参数。
#   --lowvram / --novram：权重留在系统内存，按层流式搬进显存、用完搬回
#                         （torch reserved 永远 ~0.1G，每次出图重搬一遍 ~12G）
#   --cache-none        ：执行完即丢缓存
# 命中这些时保温是**物理上无效的**——每 10 分钟白热一次只会烧 GPU 不改善任何
# 东西。此时正确动作是报出根因（改启动参数），而不是继续假装在保温。
_NO_RESIDENT_FLAGS = ("--lowvram", "--novram", "--cache-none")


def comfy_argv() -> list:
    """ComfyUI 的启动参数（system_stats.system.argv）；查不到返回 []。"""
    try:
        with urllib.request.urlopen(ci.COMFY_URL + "/system_stats", timeout=8) as r:
            j = json.loads(r.read())
        return list((j.get("system") or {}).get("argv") or [])
    except Exception:
        return []


def blocking_flags(argv: list) -> list:
    """argv 里会阻止模型常驻的参数（纯函数，便于门禁钉住判据）。"""
    low = [str(a).lower() for a in (argv or [])]
    return [f for f in _NO_RESIDENT_FLAGS if f in low]


def resolve_comfy_url() -> str:
    """出图 ComfyUI 端点：从**在跑的实例配置**里解析（与出图链同一事实源）。

    口径＝``companion.selfie.provider.command_args`` 里的 ``--url``，复用
    ``image_gen_routes._comfy_url_from_args``。解析不到再回落 ci.COMFY_URL
    （env COMFY_URL / 127.0.0.1）。保温器若打错端点＝白热一台不出图的机器，
    所以宁可跟着配置走，不自己另写一份默认值。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts._data_root import resolve_data_roots  # type: ignore
        from src.web.routes.image_gen_routes import _comfy_url_from_args
        import yaml
        for root in resolve_data_roots():
            for name in ("config.local.yaml", "config.yaml"):
                fp = Path(root) / "config" / name
                if not fp.is_file():
                    continue
                cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
                args = ((((cfg.get("companion") or {}).get("selfie") or {})
                         .get("provider") or {}).get("command_args"))
                url = _comfy_url_from_args(args)
                if url:
                    return url
    except Exception:
        pass
    return ci.COMFY_URL


def _tiny_workflow(ckpt: str) -> dict:
    """最小加载图：256x256 / 1 步。目的只是把权重顶进显存，产物直接丢。

    **复用 comfy_infer.build_workflow 而不是手搓**——保温必须加载「真出图会用到
    的那一份权重」，节点图自己另写一套既会漏节点（首版手搓漏了输出节点，ComfyUI
    直接 400），也会在主链换模型/换节点时悄悄热错东西。
    """
    return ci.build_workflow("warmup", width=256, height=256, steps=1,
                             guidance=1.0, seed=1, ckpt=ckpt)


def _submit(workflow: dict, timeout: float = 20.0) -> str:
    data = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(ci.COMFY_URL + "/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str((json.loads(r.read()) or {}).get("prompt_id") or "")
    except urllib.error.HTTPError as ex:
        # 400 的真因在 body 里（node_errors）；不打出来只能对着「Bad Request」猜。
        body = ""
        try:
            body = ex.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise RuntimeError("HTTP %s: %s" % (ex.code, body)) from ex


def _wait_done(prompt_id: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    ci.COMFY_URL + "/history/" + prompt_id, timeout=10) as r:
                if json.loads(r.read()) or {}:
                    return True
        except Exception:
            pass
        time.sleep(2.0)
    return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="", help="ComfyUI 端点（默认按实例配置解析）")
    ap.add_argument("--warm-reserved", type=float, default=ci.WARM_RESERVED_GB,
                    help="ComfyUI 自占显存(GB)达到此值即视为「模型已常驻」")
    ap.add_argument("--ckpt", default="", help="留空=服务端可用清单里自动选")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--check", action="store_true", help="只报状态，绝不加载")
    ap.add_argument("--force", action="store_true", help="已常驻也强制顶一次")
    args = ap.parse_args()
    ci.COMFY_URL = (args.url or resolve_comfy_url()).rstrip("/")
    _log("端点 %s" % ci.COMFY_URL)

    reserved = ci._comfy_reserved_gb()
    free = ci._vram_free_gb()
    if reserved < 0:
        _log("ComfyUI 不可达，跳过（保温不制造告警，真出图自己会报错）")
        return 0
    _log("当前：ComfyUI 自占 %.1fG / 空闲 %.1fG / 常驻判据 %.1fG"
         % (reserved, free, args.warm_reserved))

    # 保温之前先问「保得住吗」：启动参数禁止常驻时，热多少次都白热。
    blocked = blocking_flags(comfy_argv())
    if blocked:
        _log("⚠ 服务端以 %s 启动 —— 该模式下权重留在系统内存、按层流式搬进显存、"
             "用完即搬回，模型**物理上无法常驻**，保温无效。" % " ".join(blocked))
        _log("  根因修法（在出图机上改 ComfyUI 启动参数后重启该服务）：")
        _log("    去掉 %s，用默认 normalvram（有空间就把模型留在显存、"
             "有压力再让出，对同卡邻居最友好）" % " ".join(blocked))
        _log("  当前该卡空闲 %.1fG —— 判断够不够常驻看这个数与模型体积。" % free)
        return 0

    warm = reserved >= args.warm_reserved
    if args.check:
        _log("状态：%s" % ("已热（无需加载）" if warm else "冷（下次出图要等磁盘加载）"))
        return 0
    if warm and not args.force:
        _log("已热，零消耗退出")
        return 0

    # 保温永远给真出图让路：拿不到出图锁说明有人正在出图（那本身就会把模型加载
    # 进来），直接退出比排队更对。
    lock = ci._acquire_lock(0)
    if lock is None:
        _log("出图锁被占（有人正在出图，模型自会加载）→ 跳过")
        return 0
    try:
        # 与出图主路径同一口径解析 ckpt（resolve_ckpt：想要的不在就回落服务端首个），
        # 保温加载的必须正是真出图会用的那份权重。
        ckpt = args.ckpt or ci.resolve_ckpt(ci.CKPT, fallback=ci.CKPT_NOFACE)
        if not ckpt or not ci.available_ckpts():
            _log("服务端无可用 checkpoint（模型目录空？）→ 跳过")
            return 0
        _log("冷态，提交最小加载图把模型顶进显存：%s" % ckpt)
        t0 = time.time()
        try:
            pid = _submit(_tiny_workflow(ckpt))
        except Exception as ex:
            _log("提交失败（不视为故障）：%s" % ex)
            return 0
        if not pid:
            _log("未拿到 prompt_id → 跳过")
            return 0
        ok = _wait_done(pid, args.timeout)
        reserved2 = ci._comfy_reserved_gb()
        _log("%s，耗时 %.1fs，ComfyUI 自占 %.1fG → %.1fG"
             % ("加载完成" if ok else "等待超时(可能仍在加载)",
                time.time() - t0, reserved, reserved2))
    finally:
        try:
            ci._release_lock(lock)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
