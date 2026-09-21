#!/usr/bin/env python3
"""realloc102_machines.py — 实施102 机器侧执行器（在 117 或任一 LAN 机上跑，经 ssh 别名下发）。

把 docs/实施102 §7.1 的 runbook 编成有序步骤：每步 = (ssh 别名 | local, 远端 cmd.exe 命令, 期望
输出子串, 一句说明)。默认 **dry-run 只打印**；``--apply`` 才真跑，任一步失败即停（后续步骤不碰），
每步结果落 ``logs/realloc102_machines.jsonl``。阶段末尾自动调 ``realloc102.py phaseN --apply`` 改 overlay。

    python deploy/compute/realloc102_machines.py phase0            # 看将要做什么
    python deploy/compute/realloc102_machines.py phase0 --apply
    python deploy/compute/realloc102_machines.py phase3 --apply --comfy-task <104 上 ComfyUI 任务名>
    python deploy/compute/realloc102_machines.py phase4            # 只做预检清单，不动生产实例

约定：远端是 Windows OpenSSH，默认 shell 是 cmd.exe——命令按 cmd 写；JSON 用 ``\"`` 转义（cmd 里
单引号是普通字符，``-d '{...}'`` 会把引号原样送进 Ollama → 400）。别名与机器见 deploy/machines.json。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_ENGINE = _HERE.parent.parent
OVERLAY_TOOL = _HERE / "realloc102.py"
LOG_PATH = _ENGINE / "logs" / "realloc102_machines.jsonl"

PHASES = ["phase0", "phase1", "phase2", "phase3", "phase4"]

# ── 别名（machines.json ssh[0]；176 用 ganzhi 因 ssh 配置尚未渲染新别名）──────────
H176, H173, H104, H140, H198 = "ganzhi", "yuyan", "shengyin", "jiyi", "shijue"

VL_MODEL = "qwen3-vl:8b-instruct"
QWEN30B = "qwen3:30b-a3b-instruct-2507-q4_K_M"


def _ollama_generate(model: str, keep_alive) -> str:
    """cmd.exe 下给 curl 的 JSON：外层双引号，内层 \\\" 转义。"""
    body = f'{{\\"model\\":\\"{model}\\",\\"keep_alive\\":{keep_alive}}}'
    return (f'curl -s -X POST http://127.0.0.1:11434/api/generate '
            f'-H "Content-Type: application/json" -d "{body}"')


@dataclass
class Step:
    host: str            # ssh 别名；"local" = 本机 python/curl
    cmd: str
    why: str
    expect: str = ""     # 期望 stdout 含此子串；空=只看退出码
    optional: bool = False   # 失败不阻断（发现类 / 加固类）
    timeout: int = 120


@dataclass
class Phase:
    name: str
    title: str
    steps: List[Step] = field(default_factory=list)
    overlay_phase: str = ""    # 阶段末尾要跑的 realloc102.py 阶段名
    verify: List[Step] = field(default_factory=list)


def build_phase0(harden_ollama: bool = False) -> Phase:
    hardening = [
        Step(H176, "setx OLLAMA_HOST 127.0.0.1:11434 /M",
             "176 Ollama 只听本机（重启 Ollama 后生效；先确认 translation.engines.ollama_mt 等无 LAN 消费方）",
             optional=True),
    ] if harden_ollama else []
    return Phase("phase0", "176 减负 + 识图切 198", steps=[
        Step(H176, _ollama_generate(QWEN30B, 0),
             "176 立即卸载 qwen3:30b（18.7G；调用方对练脚本已改指 173）", expect='"done":true'),
        Step(H198, "ollama list", "198 已备 qwen3-vl", expect=VL_MODEL),
        Step(H198, f"ollama show {VL_MODEL}-orig4k >nul 2>&1 || ollama cp {VL_MODEL} {VL_MODEL}-orig4k",
             "198 备份原 4k tag（已有则跳过，防二次运行把 8k 版覆盖成备份）"),
        Step(H198, f'cmd /c "(echo FROM {VL_MODEL}-orig4k& echo PARAMETER num_ctx 8192) > %TEMP%\\vl8k.Modelfile"',
             "198 写 Modelfile：num_ctx 8192（与 176 8-30 同法）"),
        Step(H198, f"ollama create {VL_MODEL} -f %TEMP%\\vl8k.Modelfile",
             "198 就地重建同名 tag（8192 上下文）", timeout=600),
        Step(H198, _ollama_generate(VL_MODEL, -1), "198 钉 qwen3-vl 常驻（keep_alive -1）",
             expect='"done":true', timeout=300),
        Step(H198, "netsh advfirewall firewall show rule name=ollama_from_104 >nul 2>&1 || "
                   "netsh advfirewall firewall add rule name=ollama_from_104 dir=in action=allow "
                   "protocol=TCP localport=11434 remoteip=192.168.0.104",
             "198 放行 104（实测 104→198:11434 不通）"),
        Step(H176, 'schtasks /Query /FO LIST | findstr /i "comfy musetalk lipsync"',
             "176 列出 ComfyUI / musetalk 计划任务名（发现，下一步人工 /Disable）", optional=True),
        Step(H176, 'powershell -NoProfile -Command "Get-Content $env:LOCALAPPDATA\\Ollama\\server.log -Tail 4000 '
                   '| Select-String -SimpleMatch \'/api/\' | ForEach-Object { ($_.Line -split \'\\|\')[3].Trim() } '
                   '| Group-Object | Sort-Object Count -Descending | Select-Object -First 8 | Format-Table -HideTableHeaders"',
             "176 Ollama 最近调用方 IP 统计（决定能不能收 OLLAMA_HOST）", optional=True),
    ] + hardening, overlay_phase="phase0", verify=[
        Step(H176, "nvidia-smi --query-gpu=memory.used --format=csv,noheader", "176 显存应 ≤ 16000 MiB"),
        Step("local", "http://192.168.0.198:11434/api/ps", "198 qwen3-vl 常驻", expect=VL_MODEL),
    ])


def build_phase1() -> Phase:
    return Phase("phase1", "语音切 176（direct：不用角色库，本机参考音直连 7865）", steps=[
        Step("local", "http://192.168.0.176:7865/health", "176 IndexTTS-2 已载入", expect='"model_loaded":true'),
        Step("local", "file:D:/faceX/mfys/secrets/service_token.txt", "集群令牌文件在（直连 :7865 靠它带 X-AH-Svc）"),
    ], overlay_phase="phase1", verify=[
        Step("local", "http://192.168.0.176:7865/health", "176 引擎在线", expect='"status":"ok"'),
    ])


def build_phase2() -> Phase:
    return Phase("phase2", "听觉回 176（aitr_asr：whisper + emotion2vec）", steps=[
        Step(H176, "dir C:\\aitr_asr", "176 原属地目录还在（不在 → 从 198 拷，见 scripts/asr176/README.md）"),
        Step(H176, "schtasks /Change /TN AITR_ASR /Enable & schtasks /Run /TN AITR_ASR",
             "176 启 aitr_asr 任务"),
        Step(H176, "schtasks /Change /TN AITR_ASR_WATCHDOG /Enable", "176 启 5min 看门狗", optional=True),
        Step("local", "poll:http://192.168.0.176:8765/health", "等 176 ASR+SER 载入（最长 5min）",
             expect='"ser_loaded":true', timeout=300),
    ], overlay_phase="phase2", verify=[
        Step(H198, "schtasks /Change /TN AITR_ASR /Disable & schtasks /Change /TN AITR_ASR_WATCHDOG /Disable",
             "198 停 aitr_asr 任务（释放 ~3.5G；进程随任务结束或手动 Stop-Process）", optional=True),
        Step(H198, "nvidia-smi --query-gpu=memory.used --format=csv,noheader", "198 显存应降到 ~6G"),
    ])


def build_phase3(comfy_task: str = "") -> Phase:
    steps = [
        Step(H104, "schtasks /Change /TN IndexTTS104 /Disable", "104 停 IndexTTS 开机任务（权重留盘）", optional=True),
        Step(H104, 'powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 7865 -State Listen '
                   '-ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"',
             "104 结束 7865 进程（只杀听 7865 的那一个）", optional=True),
        Step(H104, 'schtasks /Query /FO LIST | findstr /i comfy', "104 列 ComfyUI 任务名（8-29 装的）", optional=True),
    ]
    if comfy_task:
        steps.append(Step(H104, f'schtasks /Change /TN "{comfy_task}" /Enable & schtasks /Run /TN "{comfy_task}"',
                          f"104 启 ComfyUI 任务 {comfy_task}"))
        steps.append(Step("local", "poll:http://192.168.0.104:8188/system_stats", "等 104 ComfyUI 起来",
                          expect="comfyui_version", timeout=300))
    else:
        steps.append(Step("local", "skip:--comfy-task 未给", "104 ComfyUI 启动：请用 --comfy-task <任务名> 重跑本阶段",
                          optional=True))
    steps += [
        Step(H140, _ollama_generate(VL_MODEL, 0), "140 卸 qwen3-vl（6G；识图已归 198）", expect='"done":true'),
        Step(H140, "schtasks /Run /TN EmotionTTS", "140 拉起 CosyVoice3 :7852（粤语专用）"),
        Step("local", "poll:http://192.168.0.140:7852/health", "等 140 CosyVoice 载入", expect="models_loaded", timeout=600),
    ]
    return Phase("phase3", "104 出图 / 140 粤语", steps=steps, overlay_phase="phase3", verify=[
        Step("local", "http://192.168.0.140:11434/api/ps", "140 只剩 bge-m3", expect="bge-m3"),
    ])


def build_phase4() -> Phase:
    """只做预检（只读），迁移本体按 §7.1 阶段 4 手工在 04:00 窗执行。"""
    return Phase("phase4", "117 → 173 预检（只读，不动生产）", steps=[
        Step(H173, 'powershell -NoProfile -Command "(Get-PSDrive D).Free/1GB"', "173 D: 空闲 GB（需 ≥ 50）"),
        Step(H173, 'powershell -NoProfile -Command "Test-Path $env:UserProfile\\.wslconfig"',
             "173 .wslconfig 是否存在（要限 WSL 内存给 Windows 侧留 24G+）", optional=True),
        Step(H173, 'powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 18799 -State Listen -EA SilentlyContinue | Measure-Object | % Count"',
             "173 上 18799 占用数（坐席桌面包在 → 迁移前先卸）"),
        Step(H173, "dir D:\\faceX\\mfys\\secrets\\service_token.txt", "173 是否已有集群令牌文件（阶段 1 direct 依赖）", optional=True),
        Step("local", "file:D:/chengjie-instances/zhiliao/data", "117 实例数据根在（6.2G，robocopy 源）"),
    ])


BUILDERS: Dict[str, Callable[..., Phase]] = {
    "phase0": lambda **kw: build_phase0(bool(kw.get("harden_ollama", False))),
    "phase1": lambda **kw: build_phase1(),
    "phase2": lambda **kw: build_phase2(),
    "phase3": lambda **kw: build_phase3(kw.get("comfy_task", "")),
    "phase4": lambda **kw: build_phase4(),
}


# ── 执行器 ────────────────────────────────────────────────────────────────────
class Runner:
    """真跑：ssh 别名下发 / 本机 http GET / 文件存在 / 轮询。可被测试替换。"""

    def ssh(self, host: str, cmd: str, timeout: int) -> tuple[int, str]:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, cmd],
                           capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")

    def http(self, url: str, timeout: float = 6.0) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return 0, r.read(4000).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return 1, f"{type(exc).__name__}: {exc}"

    def file_exists(self, path: str) -> bool:
        return Path(path).exists()

    def sleep(self, sec: float) -> None:
        time.sleep(sec)

    def local(self, argv: List[str], timeout: int) -> tuple[int, str]:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def run_step(step: Step, runner: Runner) -> tuple[bool, str]:
    """返回 (ok, 输出摘要)。local 步骤按前缀分派：http/poll/file/skip。"""
    if step.host == "local":
        if step.cmd.startswith("skip:"):
            return False, step.cmd[5:]
        if step.cmd.startswith("file:"):
            ok = runner.file_exists(step.cmd[5:])
            return ok, ("在" if ok else "不存在")
        if step.cmd.startswith("poll:"):
            url = step.cmd[5:]
            deadline = time.monotonic() + step.timeout
            last = ""
            while True:
                rc, out = runner.http(url)
                last = out
                if rc == 0 and (not step.expect or step.expect in out):
                    return True, out[:200]
                if time.monotonic() >= deadline:
                    return False, f"超时 {step.timeout}s，最后响应：{last[:160]}"
                runner.sleep(5)
        rc, out = runner.http(step.cmd)
    else:
        rc, out = runner.ssh(step.host, step.cmd, step.timeout)
    ok = rc == 0 and (not step.expect or step.expect in out)
    return ok, out.strip()[:300]


def run_phase(phase: Phase, *, apply: bool, runner: Optional[Runner] = None,
              overlay_extra: Optional[List[str]] = None,
              log_path: Path = LOG_PATH) -> int:
    runner = runner or Runner()
    print(f"== {phase.name} · {phase.title} ({'APPLY' if apply else 'dry-run'})")
    rc_total = 0
    for i, st in enumerate(phase.steps, 1):
        tag = "可选" if st.optional else "必做"
        print(f"  [{i}] {tag} {st.host}: {st.why}\n       $ {st.cmd}")
        if not apply:
            continue
        ok, out = run_step(st, runner)
        _log(log_path, phase.name, st, ok, out)
        print(f"       -> {'OK' if ok else 'FAIL'} {out[:160]}")
        if not ok and not st.optional:
            print(f"  停在第 {i} 步（必做步失败）。修好后重跑本阶段（步骤幂等）。")
            return 1
    if phase.overlay_phase:
        argv = [sys.executable, str(OVERLAY_TOOL), phase.overlay_phase, "--apply"] + list(overlay_extra or [])
        print(f"  [overlay] $ {' '.join(argv)}")
        if apply:
            rc, out = runner.local(argv, 120)
            _log(log_path, phase.name, Step("local", " ".join(argv), "overlay"), rc == 0, out)
            print(out.strip()[-800:])
            if rc != 0:
                return 1
    for st in phase.verify:
        print(f"  [验收] {st.host}: {st.why}\n       $ {st.cmd}")
        if apply:
            ok, out = run_step(st, runner)
            _log(log_path, phase.name, st, ok, out)
            print(f"       -> {'OK' if ok else '待复核'} {out[:160]}")
            if not ok and not st.optional:
                rc_total = 2
    return rc_total


def _log(path: Path, phase: str, st: Step, ok: bool, out: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "phase": phase,
               "host": st.host, "cmd": st.cmd, "ok": ok, "out": out[:500]}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", choices=PHASES)
    ap.add_argument("--apply", action="store_true", help="真跑（默认只打印步骤）")
    ap.add_argument("--comfy-task", default="", help="phase3：104 上 ComfyUI 的计划任务名")
    ap.add_argument("--harden-ollama", action="store_true",
                    help="phase0：附带把 176 OLLAMA_HOST 收回 127.0.0.1（先看调用方统计，确认无 LAN 消费方）")
    ap.add_argument("--overlay-arg", action="append", default=[],
                    help="透传给 realloc102.py 的额外参数（如 --svc-token-env NAME）")
    a = ap.parse_args()
    phase = BUILDERS[a.phase](comfy_task=a.comfy_task, harden_ollama=a.harden_ollama)
    if a.phase == "phase4" and a.apply:
        print("phase4 只做只读预检；迁移本体按 docs/实施102 §7.1 阶段 4 在 04:00 窗手工执行。")
    return run_phase(phase, apply=a.apply, overlay_extra=a.overlay_arg)


if __name__ == "__main__":
    raise SystemExit(main())
