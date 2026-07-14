# -*- coding: utf-8 -*-
"""原生通话投产前 dry-run 自检（零 Telegram 连接、零通话、零副作用）。

验证除「#44 进向音频」（唯一运行时不可自证项，须 tg_call_poc 双号互拨实测）外的**全部**技术前提：
  1. py-tgcalls 3.0 + ntgcalls 3.0 在本机 Python 能 import + 构造 NTgCalls 原生绑定；
  2. `build_call_runtime` 能按**真实合并配置**（config.yaml + config.local.yaml overlay）装配；
  3. `probe_call_host` 真探语音主机健康；
  4. `dry_run_report` + 就绪度体检输出接线完整性 + blocker/warning。

绝不连 Telegram、绝不接/拨任何电话、绝不动生产服务。安全随时可跑（含 main.py 常驻时）。
用法：``python -m tools.tg_call_dryrun``
"""
from __future__ import annotations

import json
import sys


def _load_config() -> dict:
    """读真实合并配置（走 ConfigManager 同路径，含 config.local.yaml overlay）。失败回落空。"""
    try:
        from src.utils.config_manager import ConfigManager
        cm = ConfigManager()
        return cm.config or {}
    except Exception:
        try:
            import yaml
            base = yaml.safe_load(open("config/config.yaml", "r", encoding="utf-8")) or {}
            try:
                overlay = yaml.safe_load(
                    open("config/config.local.yaml", "r", encoding="utf-8")) or {}
            except Exception:
                overlay = {}
            # 探主机 + 就绪度需 telegram_calls 与 realtime_voice（后者多在 overlay）
            for k in ("telegram_calls", "realtime_voice"):
                if k in overlay:
                    merged = dict(base.get(k) or {})
                    merged.update(overlay[k] or {})
                    base[k] = merged
            return base
        except Exception:
            return {}


def main() -> int:
    print("=== 原生通话 dry-run 自检（零连接零通话）===")
    # 1) 技术栈真机可加载
    try:
        import ntgcalls
        import pytgcalls
        binding = ntgcalls.NTgCalls()
        print(f"[1] ✅ stack: py-tgcalls={pytgcalls.__version__} "
              f"ntgcalls={getattr(ntgcalls, '__version__', '?')} "
              f"binding={type(binding).__name__}")
        try:
            binding.stop()
        except Exception:
            pass
    except Exception as ex:
        print(f"[1] ❌ stack FAIL: {type(ex).__name__}: {ex}")
        return 1

    full = _load_config()
    tc = (full.get("telegram_calls") or {}) if isinstance(full, dict) else {}
    print(f"[cfg] telegram_calls.enabled={tc.get('enabled', False)} "
          f"transport={tc.get('transport', 'ntgcalls')} brain={tc.get('brain', 's2s')} "
          f"transport_verified={tc.get('transport_verified', False)}")

    # dry-run 的价值是「开启前预演」：即便当前 enabled=false，也用「假设启用」副本探主机 +
    # 体检，让运营看清「如果现在开，主机通不通、还差啥」。不改真实配置、不落盘。
    import copy
    full_assumed = copy.deepcopy(full) if isinstance(full, dict) else {}
    full_assumed.setdefault("telegram_calls", {})
    if not full_assumed["telegram_calls"].get("enabled"):
        full_assumed["telegram_calls"] = {**full_assumed["telegram_calls"], "enabled": True}
        print("[cfg] （dry-run 用「假设启用」副本探测，不改真实配置）")
    full = full_assumed

    # 2) 装配 runtime（假 transport/brain，不连接）
    from src.voicecall.runtime import build_call_runtime, CallRuntimeDeps

    class _FakeTransport:
        async def answer(self, c): ...
        async def decline(self, c): ...
        async def send_frame(self, c, f): ...
        async def hangup(self, c): ...

    class _FakeBrain:
        async def open(self, ctx): ...

    # dry-run 不接真实 store，只验证装配链能跑通（missing 会如实点名未接依赖）
    rt = build_call_runtime(full, transport=_FakeTransport(), brain=_FakeBrain(),
                            deps=CallRuntimeDeps())
    print(f"[2] ✅ build_call_runtime: bridge={type(rt.bridge).__name__} "
          f"brain={rt.cfg.brain}")

    # 3) 真探主机
    from src.voicecall.health import probe_call_host
    probe = probe_call_host(full)
    print(f"[3] host probe: {json.dumps(probe, ensure_ascii=False)}")

    # 4) 接线完整性 + 就绪度
    rep = rt.dry_run_report(host_probe=probe)
    print("[4] dry_run_report:")
    print(f"    wired: {json.dumps(rep['wired'], ensure_ascii=False)}")
    print(f"    missing（开了但没接的关键依赖）: {rep['missing'] or '（无——但 dry-run 用假 deps，真机看 wiring）'}")
    print(f"    readiness.ready={rep['readiness']['ready']}")
    for b in rep["readiness"]["blockers"]:
        print(f"      ⛔ blocker: {b}")
    for w in rep["readiness"]["warnings"]:
        print(f"      ⚠ warning: {w}")
    print("=== 结论：除 #44 进向音频（须 tg_call_poc 双号互拨实测）外，技术前提可离线确认 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
