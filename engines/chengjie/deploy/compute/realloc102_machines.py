#!/usr/bin/env python3
"""realloc102_machines.py — 实施102 机器侧执行器（在 117 或任一 LAN 机上跑，经 ssh 别名下发）。

把 docs/实施102 §7.1 的 runbook 编成有序步骤：每步 = (ssh 别名 | local, 远端 cmd.exe 命令, 期望
输出子串, 一句说明)。默认 **dry-run 只打印**；``--apply`` 才真跑，任一步失败即停（后续步骤不碰），
每步结果落 ``logs/realloc102_machines.jsonl``。阶段末尾自动调 ``realloc102.py phaseN --apply`` 改 overlay。

    python deploy/compute/realloc102_machines.py phase0            # 看将要做什么
    python deploy/compute/realloc102_machines.py phase0 --apply
    python deploy/compute/realloc102_machines.py phase0e --apply   # 只补 0e（泊 musetalk），不重建 198 模型
    python deploy/compute/realloc102_machines.py phase3 --apply --comfy-task <104 上 ComfyUI 任务名>
    python deploy/compute/realloc102_machines.py phase4            # 只做预检清单，不动生产实例

约定：远端是 Windows OpenSSH；步骤命令**全部写成 PowerShell**，Runner 编成 ``-EncodedCommand`` 下发
——176 默认 shell 是 PowerShell、其它机器是 cmd，编码下发对两者一视同仁，且零引号转义问题
（首跑实锤：按 cmd 写的 curl 在 176 被当成 Invoke-WebRequest）。别名与机器见 deploy/machines.json。
"""
from __future__ import annotations

import argparse
import hashlib
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
ASR_SERVER_SRC = _ENGINE / "scripts" / "asr176" / "asr_server.py"   # 176/198 共用的服务本体，仓库为准
LOG_PATH = _ENGINE / "logs" / "realloc102_machines.jsonl"

PHASES = ["phase0", "phase0e", "phase1", "phase2", "phase3", "phase4"]

# ── 别名（machines.json ssh[0]；176 用 ganzhi 因 ssh 配置尚未渲染新别名）──────────
H176, H173, H104, H140, H198 = "ganzhi", "yuyan", "shengyin", "jiyi", "shijue"

VL_MODEL = "qwen3-vl:8b-instruct"
QWEN30B = "qwen3:30b-a3b-instruct-2507-q4_K_M"


# 远端命令一律写成 PowerShell 脚本，Runner.ssh 编成 -EncodedCommand 下发：
# 176 的 OpenSSH 默认 shell 是 PowerShell（首跑实锤：curl 被解析成 Invoke-WebRequest 报错），
# 其它机器是 cmd——编码下发后两种默认 shell 都能跑，也没有引号转义问题。
PS_PRELUDE = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "


def _ollama_generate(model: str, keep_alive) -> str:
    """Ollama /api/generate 空提示 + keep_alive：0=立即卸载，-1=钉常驻。输出压成一行 JSON。"""
    body = json.dumps({"model": model, "keep_alive": keep_alive})
    return (f"Invoke-RestMethod -Method Post -Uri http://127.0.0.1:11434/api/generate "
            f"-ContentType 'application/json' -Body '{body}' -TimeoutSec 280 | ConvertTo-Json -Compress")


def _hub_park(name: str) -> str:
    """AvatarHub 显存管家泊车（POST /api/gpu/park?name=）：停引擎+挂起自愈，带 _park_refuse 防误伤；裸 engine/stop 会被 self-heal 拉回。"""
    return (f"Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:9000/api/gpu/park?name={name}' "
            f"-TimeoutSec 200 | ConvertTo-Json -Compress -Depth 4")


def _native(cmd: str) -> str:
    """外部命令：让 PowerShell 退出码跟随它（否则 -EncodedCommand 只在脚本抛错时才非 0）。"""
    return f"{cmd}; exit $LASTEXITCODE"


@dataclass
class Step:
    host: str            # ssh 别名；"local" = 本机 python/curl
    cmd: str
    why: str
    expect: str = ""     # 期望 stdout 含此子串；空=只看退出码
    optional: bool = False   # 失败不阻断（发现类 / 加固类）
    timeout: int = 120


# 176 的 ComfyUI 计划任务名（phase0 发现步 2026-09-22 04:12 现场查到：\ComfyBoot 开机拉起、\ComfyWatchdog 每 5 分拉回）。
# 先停任务再结束进程，否则 5 分内被拉回；两步都幂等。
COMFY176_TASKS = ("ComfyWatchdog", "ComfyBoot")
COMFY176_STOP = [
    Step(H176, "".join(f"schtasks /Change /TN {t} /Disable; if ($LASTEXITCODE) {{ exit $LASTEXITCODE }}; "
                       for t in COMFY176_TASKS) + "'disabled'",
         "176 停 ComfyUI 开机/看门狗任务（出图已归 104）", expect="disabled"),
    Step(H176, "Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction SilentlyContinue "
               "| ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }; 'done'",
         "176 结束 ComfyUI 进程（只杀听 8188 的那一个）"),
]


@dataclass
class Phase:
    name: str
    title: str
    steps: List[Step] = field(default_factory=list)
    overlay_phase: str = ""    # 阶段末尾要跑的 realloc102.py 阶段名
    verify: List[Step] = field(default_factory=list)


# 0e：musetalk 泊车。ComfyUI 在 176 **不动**：companion.selfie 到阶段 3 才切 104，提前停会让自拍无后端；
# --lowvram 空闲实测 torch_vram_total 64MB，不占阶段 0 的 16G 预算。停任务+结束进程挂在 build_phase3 104 起来后。
PHASE0E_STEPS = [
    Step(H176, _hub_park("lipsync"),
         "176 musetalk :8090 经 AvatarHub 显存管家泊车（不裸杀；挂起自愈，否则 20s 被拉回）",
         timeout=240),
]


def build_phase0e() -> Phase:
    """phase0 的 0e 子步单独重跑（首跑 04:12 只做了任务名发现）：不重建 198 模型、不碰 overlay。"""
    return Phase("phase0e", "176 泊 musetalk（phase0 补跑）", steps=list(PHASE0E_STEPS), verify=[
        Step(H176, _native("nvidia-smi --query-gpu=memory.used --format=csv,noheader"), "176 显存应 ≤ 16000 MiB"),
        Step(H176, "(Get-NetTCPConnection -LocalPort 8090 -State Listen -EA SilentlyContinue | Measure-Object).Count",
             "176 :8090 应无监听（musetalk 已泊）", expect="0"),
    ])


def build_phase0(harden_ollama: bool = False) -> Phase:
    hardening = [
        Step(H176, _native("setx OLLAMA_HOST 127.0.0.1:11434 /M"),
             "176 Ollama 只听本机（重启 Ollama 后生效；先确认 translation.engines.ollama_mt 等无 LAN 消费方）",
             optional=True),
    ] if harden_ollama else []
    return Phase("phase0", "176 减负 + 识图切 198", steps=[
        Step(H176, _ollama_generate(QWEN30B, 0),
             "176 立即卸载 qwen3:30b（18.7G；调用方对练脚本已改指 173）", expect='"done":true'),
        Step(H198, _native("ollama list"), "198 已备 qwen3-vl", expect=VL_MODEL),
        Step(H198, f"$ErrorActionPreference='SilentlyContinue'; ollama show {VL_MODEL}-orig4k *> $null; "
                   f"if ($LASTEXITCODE -ne 0) {{ ollama cp {VL_MODEL} {VL_MODEL}-orig4k }}; exit 0",
             "198 备份原 4k tag（已有则跳过，防二次运行把 8k 版覆盖成备份）"),
        Step(H198, f"Set-Content -Path \"$env:TEMP\\vl8k.Modelfile\" -Encoding ascii "
                   f"-Value @('FROM {VL_MODEL}-orig4k','PARAMETER num_ctx 8192'); "
                   f"Get-Content \"$env:TEMP\\vl8k.Modelfile\"",
             "198 写 Modelfile：num_ctx 8192（与 176 8-30 同法）", expect="num_ctx 8192"),
        Step(H198, _native(f'ollama create {VL_MODEL} -f "$env:TEMP\\vl8k.Modelfile"'),
             "198 就地重建同名 tag（8192 上下文）", timeout=600),
        Step(H198, _ollama_generate(VL_MODEL, -1), "198 钉 qwen3-vl 常驻（keep_alive -1）",
             expect='"done":true', timeout=300),
        Step(H198, "$r = netsh advfirewall firewall show rule name=ollama_from_104 2>$null; "
                   "if (-not ($r | Select-String ollama_from_104)) { netsh advfirewall firewall add rule "
                   "name=ollama_from_104 dir=in action=allow protocol=TCP localport=11434 remoteip=192.168.0.104 } "
                   "else { 'rule exists' }",
             "198 放行 104（实测 104→198:11434 不通）"),
    ] + PHASE0E_STEPS + [
        Step(H176, "Get-Content \"$env:LOCALAPPDATA\\Ollama\\server.log\" -Tail 4000 "
                   "| Select-String -SimpleMatch '/api/' | ForEach-Object { ($_.Line -split '\\|')[3].Trim() } "
                   "| Group-Object | Sort-Object Count -Descending | Select-Object -First 8 Count,Name | Format-Table -HideTableHeaders",
             "176 Ollama 最近调用方 IP 统计（决定能不能收 OLLAMA_HOST）", optional=True),
    ] + hardening, overlay_phase="phase0", verify=[
        Step(H176, _native("nvidia-smi --query-gpu=memory.used --format=csv,noheader"), "176 显存应 ≤ 16000 MiB"),
        Step("local", "http://192.168.0.198:11434/api/ps", "198 qwen3-vl 常驻", expect=VL_MODEL),
    ])


def build_phase1() -> Phase:
    return Phase("phase1", "语音切 176（direct：不用角色库，本机参考音直连 7865）", steps=[
        Step("local", "http://192.168.0.176:7865/health", "176 IndexTTS-2 已载入", expect='"model_loaded":true'),
        Step("local", "file:D:/faceX/mfys/secrets/service_token.txt", "集群令牌文件在（直连 :7865 靠它带 X-AH-Svc）"),
    ], overlay_phase="phase1", verify=[
        Step("local", "http://192.168.0.176:7865/health", "176 引擎在线", expect='"status":"ok"'),
    ])


# 任务名以现场 schtasks /Query 为准（2026-09-22）：176 是 AITR_ASR_176（Disabled），198 是 AITR_ASR_198；看门狗两边同名。
ASR_TASK_176, ASR_TASK_198, ASR_WATCHDOG = "AITR_ASR_176", "AITR_ASR_198", "AITR_ASR_WATCHDOG"


def _kill_asr_server_ps() -> str:
    return ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'asr_server' } "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")


def _file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest().upper() if p.exists() else ""


def build_phase2() -> Phase:
    lift_limit = (
        f"$t = Get-ScheduledTask -TaskName {ASR_TASK_176}; $t.Settings.ExecutionTimeLimit = 'PT0S'; "
        "$t.Settings.DisallowStartIfOnBatteries = $false; $t.Settings.StopIfGoingOnBatteries = $false; "
        "Set-ScheduledTask -InputObject $t | Out-Null; "
        f"[string](Get-ScheduledTask -TaskName {ASR_TASK_176}).Settings.ExecutionTimeLimit"
    )
    return Phase("phase2", "听觉回 176（aitr_asr：whisper + emotion2vec）", steps=[
        Step(H176, "Test-Path C:\\aitr_asr", "176 原属地目录还在（不在 → 从 198 拷，见 scripts/asr176/README.md）",
             expect="True"),
        # 176 盘上是 08-18 旧版（10KB）；09-12 P0 解码纪律只部到了 198。仏库为准推过去，哈希验收。
        Step("local", f"scp:{ASR_SERVER_SRC}|{H176}:C:/aitr_asr/asr_server.py", "176 部仓库版 asr_server.py（09-12 P0）"),
        Step(H176, "(Get-FileHash C:\\aitr_asr\\asr_server.py -Algorithm SHA256).Hash", "176 asr_server.py 与仓库一致",
             expect=_file_sha256(ASR_SERVER_SRC)),
        Step(H176, lift_limit, "176 任务时限 PT72H→PT0S（98 09-18 72h 自杀同根）", expect="PT0S"),
        Step(H176, _native(f"schtasks /Change /TN {ASR_TASK_176} /Enable; schtasks /Run /TN {ASR_TASK_176}"),
             "176 启 aitr_asr 任务"),
        Step(H176, _native(f"schtasks /Change /TN {ASR_WATCHDOG} /Enable"), "176 启 5min 看门狗", optional=True),
        Step("local", "poll:http://192.168.0.176:8765/health", "等 176 ASR+SER 载入（最长 5min）",
             expect='"ser_loaded":true', timeout=300),
    ], overlay_phase="phase2", verify=[
        # overlay 热重载 ~30s，先等它切完再拆 198，避免在途请求撕到无监听的 8765
        Step("local", "poll:http://192.168.0.176:8765/health", "176 ASR+SER 仍在线", expect='"asr_loaded":true', timeout=60),
        Step(H198, _native(f"schtasks /Change /TN {ASR_TASK_198} /Disable; schtasks /Change /TN {ASR_WATCHDOG} /Disable"),
             "198 停 aitr_asr 任务+看门狗（否则 5min 内被拉回）", optional=True),
        Step(H198, f"Start-Sleep 30; schtasks /End /TN {ASR_TASK_198} 2>$null | Out-Null; " + _kill_asr_server_ps() + "; 'stopped'",
             "198 结束 asr_server 进程（释放 ~3.5G）", expect="stopped", optional=True),
        Step(H198, _native("nvidia-smi --query-gpu=memory.used --format=csv,noheader"), "198 显存应降到 ~6G"),
    ])


def build_phase3(comfy_task: str = "") -> Phase:
    steps = [
        Step(H104, _native("schtasks /Change /TN IndexTTS104 /Disable"), "104 停 IndexTTS 开机任务（权重留盘）", optional=True),
        Step(H104, "Get-NetTCPConnection -LocalPort 7865 -State Listen -ErrorAction SilentlyContinue "
                   "| ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }; 'done'",
             "104 结束 7865 进程（只杀听 7865 的那一个）", optional=True),
        Step(H104, "schtasks /Query /FO LIST | Select-String -Pattern 'comfy' -CaseSensitive:$false",
             "104 列 ComfyUI 任务名（8-29 装的）", optional=True),
    ]
    verify: List[Step] = []
    if comfy_task:
        steps.append(Step(H104, _native(f'schtasks /Change /TN "{comfy_task}" /Enable; schtasks /Run /TN "{comfy_task}"'),
                          f"104 启 ComfyUI 任务 {comfy_task}"))
        steps.append(Step("local", "poll:http://192.168.0.104:8188/system_stats", "等 104 ComfyUI 起来",
                          expect="comfyui_version", timeout=300))
        # 176 ComfyUI 放到 overlay 之后拆（同 phase2 拆 198 的做法）：overlay 热重载 ~30s，
        # 先停 176 会让在途自拍打到无监听的 8188
        verify += [
            Step("local", "poll:http://192.168.0.104:8188/system_stats", "104 ComfyUI 仍在线（overlay 已切）",
                 expect="comfyui_version", timeout=60),
            *COMFY176_STOP,
        ]
    else:
        steps.append(Step("local", "skip:--comfy-task 未给", "104 ComfyUI 启动：请用 --comfy-task <任务名> 重跑本阶段",
                          optional=True))
    steps += [
        Step(H140, _ollama_generate(VL_MODEL, 0), "140 卸 qwen3-vl（6G；识图已归 198）", expect='"done":true'),
        # 任务 08-28 起 Disabled（当时让显存给 qwen3-vl），/Run 之前必须 /Enable；_svc_emotion.bat 自带 7852 健康短路，幂等
        Step(H140, _native("schtasks /Change /TN EmotionTTS /Enable; schtasks /Run /TN EmotionTTS"),
             "140 启用并拉起 CosyVoice3 :7852（粤语专用）"),
        Step("local", "poll:http://192.168.0.140:7852/health", "等 140 CosyVoice 载入", expect='"models_loaded":true', timeout=600),
    ]
    verify.append(Step("local", "http://192.168.0.140:11434/api/ps", "140 只剩 bge-m3", expect="bge-m3"))
    return Phase("phase3", "104 出图 / 140 粤语", steps=steps, overlay_phase="phase3", verify=verify)


def build_phase4() -> Phase:
    """只做预检（只读），迁移本体按 §7.1 阶段 4 手工在 04:00 窗执行。"""
    return Phase("phase4", "117 → 173 预检（只读，不动生产）", steps=[
        Step(H173, "[math]::Round((Get-PSDrive D).Free/1GB)", "173 D: 空闲 GB（需 ≥ 50）"),
        Step(H173, "Test-Path \"$env:UserProfile\\.wslconfig\"",
             "173 .wslconfig 是否存在（要限 WSL 内存给 Windows 侧留 24G+）", optional=True),
        Step(H173, "(Get-NetTCPConnection -LocalPort 18799 -State Listen -EA SilentlyContinue | Measure-Object).Count",
             "173 上 18799 占用数（坐席桌面包在 → 迁移前先卸）"),
        Step(H173, "Test-Path D:\\faceX\\mfys\\secrets\\service_token.txt",
             "173 是否已有集群令牌文件（阶段 1 direct 依赖）", optional=True),
        Step("local", "file:D:/chengjie-instances/zhiliao/data", "117 实例数据根在（6.2G，robocopy 源）"),
    ])


BUILDERS: Dict[str, Callable[..., Phase]] = {
    "phase0": lambda **kw: build_phase0(bool(kw.get("harden_ollama", False))),
    "phase0e": lambda **kw: build_phase0e(),
    "phase1": lambda **kw: build_phase1(),
    "phase2": lambda **kw: build_phase2(),
    "phase3": lambda **kw: build_phase3(kw.get("comfy_task", "")),
    "phase4": lambda **kw: build_phase4(),
}


# ── 执行器 ────────────────────────────────────────────────────────────────────
def encode_ps(script: str) -> str:
    """PowerShell 脚本 → ``powershell -NoProfile -EncodedCommand <base64(utf-16le)>``（远端任意默认 shell 可跑）。"""
    import base64
    b64 = base64.b64encode((PS_PRELUDE + script).encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {b64}"


class Runner:
    """真跑：ssh 别名下发 / 本机 http GET / 文件存在 / 轮询。可被测试替换。"""

    def ssh(self, host: str, cmd: str, timeout: int) -> tuple[int, str]:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, encode_ps(cmd)],
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
    """返回 (ok, 输出摘要)。local 步骤按前缀分派：http/poll/file/scp/skip。"""
    if step.host == "local":
        if step.cmd.startswith("skip:"):
            return False, step.cmd[5:]
        if step.cmd.startswith("file:"):
            ok = runner.file_exists(step.cmd[5:])
            return ok, ("在" if ok else "不存在")
        if step.cmd.startswith("scp:"):
            src, dst = step.cmd[4:].split("|", 1)
            rc, out = runner.local(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", src, dst], step.timeout)
            return rc == 0, out.strip()[:300] or "copied"
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
              log_path: Path = LOG_PATH, verify_only: bool = False) -> int:
    runner = runner or Runner()
    print(f"== {phase.name} · {phase.title} ({'APPLY' if apply else 'dry-run'}{'，只验收' if verify_only else ''})")
    rc_total = 0
    for i, st in enumerate(phase.steps, 1):
        if verify_only:
            break
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
    if phase.overlay_phase and not verify_only:
        argv = [sys.executable, str(OVERLAY_TOOL), phase.overlay_phase, "--apply"] + list(overlay_extra or [])
        print(f"  [overlay] $ {' '.join(argv)}")
        if apply:
            rc, out = runner.local(argv, 120)
            _log(log_path, phase.name, Step("local", " ".join(argv), "overlay"), rc == 0, out)
            print(out.strip()[-800:])
            if rc != 0:
                return 1
    for i, st in enumerate(phase.verify, 1):
        print(f"  [验收{i}] {st.host}: {st.why}\n       $ {st.cmd}")
        if apply:
            ok, out = run_step(st, runner)
            _log(log_path, phase.name, st, ok, out)
            print(f"       -> {'OK' if ok else '待复核'} {out[:160]}")
            if not ok and not st.optional:
                # 验收里夹着拆旧机（198 ASR / 176 ComfyUI）的动作，前置 poll 不过就不能往下拆
                print(f"  停在验收第 {i} 步（必做步失败）。修好后用 --verify-only 重跑（验收步幂等）。")
                return 2
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
    ap.add_argument("--verify-only", action="store_true", help="跳过步骤与 overlay，只跑阶段验收（重跑/复核用）")
    a = ap.parse_args()
    # 远端回显带 UTF-8 替代符时别让 GBK 控制台把整轮打死（首跑 phase2 在 overlay 回显处摔过）
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    phase = BUILDERS[a.phase](comfy_task=a.comfy_task, harden_ollama=a.harden_ollama)
    if a.phase == "phase4" and a.apply:
        print("phase4 只做只读预检；迁移本体按 docs/实施102 §7.1 阶段 4 在 04:00 窗手工执行。")
    return run_phase(phase, apply=a.apply, overlay_extra=a.overlay_arg, verify_only=a.verify_only)


if __name__ == "__main__":
    raise SystemExit(main())
