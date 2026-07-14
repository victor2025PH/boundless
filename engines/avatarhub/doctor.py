# -*- coding: utf-8 -*-
"""AvatarHub 一键自检：环境 / 服务 / 角色 / 黄金包 / 磁盘 全面体检并出报告。
用法：python _doctor.py [--json] [--profile <角色名>]（--profile 省略时自动跟随当前激活角色）
退出码：0=全部健康  1=有警告  2=有严重问题
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HUB = "http://127.0.0.1:9000"
import app_config
BASE = app_config.BASE
CRITICAL_SERVICES = {"fish_tts", "stt", "lipsync"}


def _get(path, timeout=15):
    with urllib.request.urlopen(HUB + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", errors="replace"))


class Report:
    def __init__(self):
        self.items = []     # (level, area, msg)  level: ok/warn/crit
        self.suggestions = []

    def ok(self, area, msg):   self.items.append(("ok", area, msg))
    def info(self, area, msg): self.items.append(("info", area, msg))   # 信息项：不计入结论(可选项/预期状态)
    def warn(self, area, msg, fix=""):
        self.items.append(("warn", area, msg))
        if fix:
            self.suggestions.append(fix)
    def crit(self, area, msg, fix=""):
        self.items.append(("crit", area, msg))
        if fix:
            self.suggestions.append(fix)

    @property
    def worst(self):
        levels = [i[0] for i in self.items]
        if "crit" in levels:
            return 2
        if "warn" in levels:
            return 1
        return 0


def check_hub(rep):
    try:
        st, h = _get("/health")
        if not isinstance(h, dict):   # /health 偶发返回 null（冷启动/旧版）：端口通即视为在线
            rep.ok("Hub", f"运行中 · HTTP {st}（健康详情暂不可用）")
            return {}
        rep.ok("Hub", f"运行中 · 压力 {h.get('pressure','?')} · {h.get('profile_count',0)} 角色")
        return h
    except Exception as e:
        rep.crit("Hub", f"无法连接 {HUB}（{e}）",
                 fix="启动 Hub：python avatar_hub.py，或运行 start_all_services.bat")
        return None


def check_services(rep, health):
    if not health:
        return
    svc = health.get("services", {})
    for name, up in svc.items():
        crit = name in CRITICAL_SERVICES
        if up:
            rep.ok("服务", f"{name} 在线")
        elif crit:
            rep.crit("服务", f"关键服务 {name} 掉线",
                     fix=f"重启 {name}（service_manager.py 或对应 .bat），对话主链路依赖它")
        else:
            # 可选扩展服务默认不启动(仅 START_EXTRAS=1 才起)→ 离线是预期状态，归信息项不拉低交付结论
            rep.info("服务", f"{name} 未启用（可选扩展，需要时 set START_EXTRAS=1）")


def check_monitor(rep):
    try:
        st, d = _get("/api/health/monitor")
        if d.get("ok"):
            cd = d.get("critical_down") or []
            if cd:
                rep.crit("自愈监控", f"关键服务掉线: {', '.join(cd)}")
            else:
                rep.ok("自愈监控", f"{len(d.get('services',{}))} 服务受监控 · {len(d.get('alerts',[]))} 告警")
    except Exception:
        rep.warn("自愈监控", "health_monitor 不可用")


def check_license(rep):
    """授权灰度：评估模式只提示，强制模式下无效才阻断商用交付。"""
    try:
        _st, d = _get("/api/license/status")
        s = (d.get("state") or {}) if isinstance(d, dict) else {}
        status = s.get("status", "")
        edition = s.get("edition_label") or s.get("edition", "")
        enforcing = bool(s.get("enforcing"))
        machine = s.get("this_machine", "")
        days = s.get("days_left", -1)
        days_s = "永久" if isinstance(days, int) and days < 0 else f"剩余{days}天"
        if status == "valid":
            rep.ok("授权", f"{edition} · 已授权 · {days_s} · 机器 {machine} · "
                   f"{'强制模式' if enforcing else '评估模式'}")
        elif enforcing:
            rep.crit("授权", f"强制模式下授权无效：{s.get('status_label', status)} · {s.get('message','')}",
                     fix="导入/重签 license.key，或临时设 AVATARHUB_LICENSE_ENFORCE=0 退回评估模式")
        else:
            rep.info("授权", f"评估模式：{s.get('status_label', status)} · {s.get('message','')}")
    except Exception as e:
        rep.warn("授权", f"授权状态读取失败: {e}")


def check_profiles(rep, profile):
    try:
        st, d = _get("/profiles")
        profs = d.get("profiles", [])
        if not profs:
            rep.crit("角色", "无任何角色", fix="在工作室 /ui 创建角色或导入配置包")
            return
        active = d.get("active", "") or ""
        rep.ok("角色", f"{len(profs)} 个角色 · 当前 {active or '-'}")
        names = {p.get("name") for p in profs}
        # 主角色解析：显式指定且存在时优先检它；未指定/不存在则回退「当前激活角色」，
        # 使体检对任意机器通用——避免硬编码历史角色名在交付机上永远误报「不存在」。
        explicit = bool(profile) and profile in names
        if not explicit:
            if profile and profile not in names:
                rep.info("角色", f"指定主角色 {profile} 不存在，改检当前激活角色 {active or '-'}")
            profile = active
        if not profile or profile not in names:
            rep.info("角色", "未激活具体主角色，跳过单角色深检（交付前请激活已配置的角色）")
            return
        enc = urllib.parse.quote(profile, safe="")
        st2, det = _get(f"/profiles/{enc}?include_face=false")
        qa = det.get("quality_axes") or {}
        cos = qa.get("cosine", 0)
        if not det.get("has_voice"):
            # 显式声明的主角色无声音＝严重（交付阻断）；仅是当前临时激活角色无声音＝警告：
            # 产品会回退内置默认音、系统仍可出声，属可配置状态而非运行故障。
            if explicit:
                rep.crit("角色", f"{profile} 未绑定声音", fix="工作室绑定参考音或导入黄金包")
            else:
                rep.warn("角色", f"当前激活角色 {profile} 未绑声音（将回退默认音）",
                         fix="交付前激活一个已绑声音的角色，或为其绑定参考音")
        elif cos and cos < 0.5:
            rep.warn("角色", f"{profile} cosine {cos:.3f} 偏低",
                     fix="工作室音质优化或一键修复音质（auto_tune）")
        else:
            rep.ok("角色", f"{profile} 音色 cosine {cos or '—'}")
        if not det.get("has_system_prompt"):
            rep.warn("角色", f"{profile} 无对话人设 system_prompt")
    except Exception as e:
        rep.warn("角色", f"角色检查失败: {e}")


def check_llm(rep):
    """对话大脑：默认 LLM 引擎是否就绪、云端是否配置 key、容灾兜底是否可用。"""
    try:
        st, d = _get("/api/converse/backends")
        default = (d.get("defaults") or {}).get("llm", "")
        llms = d.get("llm", [])
        if not default or default == "mock_llm":
            rep.crit("对话大脑", "未配置真实 LLM（仍为 mock）",
                     fix="在 llm_backends.json 设置 default，并启动 Ollama 或配置云端 key")
            return
        be = next((b for b in llms if b.get("name") == default), None)
        if be is None:
            rep.warn("对话大脑", f"默认引擎 {default} 未注册")
            return
        if be.get("kind") == "cloud":
            if be.get("has_key"):
                rep.ok("对话大脑", f"默认 {default}（云端）· key 已配置")
                locals_ = [b for b in llms if b.get("kind") == "local"
                           and not b.get("name", "").startswith("mock")]
                if locals_:
                    rep.ok("对话大脑", f"容灾兜底 {len(locals_)} 个本地引擎可用")
                else:
                    rep.warn("对话大脑", "云端无本地兜底，断网将无法对话",
                             fix="拉起 Ollama 并在 llm_backends.json 保留本地模型作兜底")
            else:
                rep.crit("对话大脑", f"默认 {default}（云端）未配置 API Key",
                         fix="在 secrets.bat 设置 CONV_DEEPSEEK_API_KEY 后重启")
        else:
            rep.ok("对话大脑", f"默认 {default}（本地）")
    except Exception as e:
        rep.warn("对话大脑", f"LLM 检查失败: {e}")


def check_golden(rep, profile):
    try:
        st, d = _get("/api/golden/list")
        rows = d.get("golden", [])
        have = [r for r in rows if r.get("exists")]
        if not have:
            rep.warn("黄金包", "无任何黄金出厂包",
                     fix="工作室/看板「存档」当前满意配置，便于一键恢复")
            return
        rep.ok("黄金包", f"{len(have)}/{len(rows)} 角色已存档")
        for r in have:
            drift = r.get("drift", 0)
            if drift < -0.05:
                rep.warn("黄金包",
                         f"{r['profile']} 当前 cosine 较黄金回退 {abs(drift):.3f}",
                         fix=f"可在看板对 {r['profile']} 点「恢复」回到黄金状态")
    except Exception as e:
        rep.warn("黄金包", f"黄金包检查失败: {e}")


def check_supervisor(rep):
    try:
        st, d = _get("/api/supervisor")
        if not d.get("ok"):
            rep.warn("进程守护", "supervisor 未启用")
            return
        sup = d.get("supervised", {})
        tripped = [k for k, v in sup.items() if v.get("tripped")]
        restarts = sum(v.get("restarts", 0) for v in sup.values())
        if tripped:
            for k in tripped:
                rep.crit("进程守护", f"{k} 已熔断（自动拉起失败）：{sup[k].get('last_error','')[:60]}",
                         fix=f"修复 {k} 后调用 POST /api/supervisor/reset 恢复自动拉起")
        else:
            rep.ok("进程守护", f"{len(sup)} 关键服务受守护 · 累计自动重启 {restarts} 次")
    except Exception as e:
        rep.warn("进程守护", f"supervisor 检查失败: {e}")


def check_backpressure(rep):
    try:
        st, d = _get("/api/backpressure?best_of=2")
        v = d.get("vram", {})
        lv = v.get("level", "unknown")
        if lv == "high":
            rep.warn("显存背压", f"显存吃紧 {v.get('used_mb')}/{v.get('total_mb')}MB（空闲 {v.get('free_mb')}MB）",
                     fix="重负载已自动串行合成防 OOM；可减少并发对话或卸载离线模型")
        elif lv == "unknown":
            rep.ok("显存背压", "无 nvidia-smi，跳过水位检测")
        else:
            rep.ok("显存背压", f"{lv} · 空闲 {v.get('free_mb')}MB")
    except Exception as e:
        rep.warn("显存背压", f"背压检查失败: {e}")


def check_capacity(rep):
    """实时并发准入 + 多卡负载池：自托管交付时确认「这台还能接几路、多卡是否都在分担」。"""
    try:
        st, d = _get("/api/capacity")
    except Exception as e:
        rep.warn("并发准入", f"容量快照不可用: {e}")
        return
    if not d.get("enabled"):
        rep.ok("并发准入",
               "未启用准入(CONV_MAX_CONCURRENT=0)：单路够用；多路直播建议设 auto 以获排队保护")
    else:
        mode = "auto·随健康卡数" if d.get("auto") else "固定"
        rep.ok("并发准入",
               f"K={d.get('max')}（{mode}）· 在用 {d.get('active')} · 排队 {d.get('waiting')}/{d.get('max_queue') or '∞'}")
        if d.get("max_queue") and d.get("waiting", 0) >= d.get("max_queue"):
            rep.warn("并发准入", "队列已满，新请求将被 busy 优雅拒绝", fix="加卡分担或降低进线速率")

    gp = d.get("gpu_pools") or {}
    pools = gp.get("pools") or {}
    rec = gp.get("recommended_concurrency")
    multi = {n: p for n, p in pools.items() if p.get("size", 1) > 1}
    if not multi:
        rep.ok("多卡负载", "单副本（未配多卡分担）")
        return
    for n, p in multi.items():
        size, healthy = p.get("size", 0), p.get("healthy", 0)
        downs = [r.get("url", "?") for r in p.get("replicas", []) if r.get("down")]
        served = sum(r.get("served", 0) for r in p.get("replicas", []))
        if healthy == 0:
            rep.crit("多卡负载", f"{n}: 全部 {size} 副本不可用",
                     fix=f"检查 {n} 各副本 /health、网络与防火墙")
        elif healthy < size:
            rep.warn("多卡负载", f"{n}: {healthy}/{size} 健康，已摘除 {', '.join(downs)}",
                     fix=f"恢复掉线副本（K 已自动收到 {rec}）")
        else:
            rep.ok("多卡负载", f"{n}: {healthy}/{size} 健康 · 累计分担 {served}")
    if d.get("enabled") and not d.get("auto") and rec and d.get("max") != rec:
        rep.warn("多卡负载", f"K={d.get('max')} 与推荐 {rec}(健康卡数)不一致",
                 fix="设 CONV_MAX_CONCURRENT=auto 让 K 随健康卡数自适应")


def check_audience(rep):
    """观众提问 + 无人值守自动应答（自托管创作者场景，默认关）。"""
    try:
        st, d = _get("/api/audience/questions?limit=1")
    except Exception as e:
        rep.warn("观众互动", f"观众通道不可用: {e}")
        return
    if not d.get("enabled"):
        rep.ok("观众互动", "未开启（需要时 set AVATARHUB_AUDIENCE=1）")
        return
    snap = d.get("snapshot") or {}
    auto = d.get("auto") or {}
    pend = snap.get("pending", 0)
    if not auto.get("available"):
        rep.warn("观众互动", "已开但自动应答 worker 不可用", fix="确认 audience 模块加载、重启 Hub")
    elif auto.get("on"):
        rep.ok("观众互动", f"已开 · 自动应答[开] 已答 {auto.get('answered', 0)} · 待答 {pend}")
        # 流式 TTS + 自动应答：复用 api_converse_stream，开关开且角色就绪才真走边出边喂口型
        try:
            _, cap = _get("/api/capacity")
            st = cap.get("streaming_tts") or {}
            if st.get("enabled"):
                if st.get("eligible"):
                    am = (st.get("metrics") or {}).get("audience_stream") or {}
                    rep.ok("观众流式TTS",
                           f"已开且就绪 · 观众首音样本 {am.get('samples', 0)}"
                           + (f" · p50 {am.get('p50_ms')}ms" if am.get('samples') else ""))
                else:
                    rep.warn("观众流式TTS",
                             "CONV_TTS_STREAMING=1 但当前角色未就绪(需 fish_speech + 克隆音)",
                             fix="确认激活角色 voice_b64 与 tts_engine=fish_speech，或暂关 CONV_TTS_STREAMING")
        except Exception:
            pass
    else:
        rep.ok("观众互动", f"已开 · 自动应答[关·主播手动] · 待答 {pend}")
    if auto.get("errors"):
        rep.warn("观众互动", f"自动应答累计开口失败 {auto['errors']} 次",
                 fix="查 Hub 日志 [Audience] 开口失败原因（角色/TTS/口型）")


def check_highlights(rep):
    """精彩问答高光：持久化(highlights.db)+ 图片卡渲染就绪度（自托管创作者复盘/出素材）。"""
    try:
        st, d = _get("/api/highlights/sessions")
    except Exception as e:
        rep.warn("精彩问答", f"高光接口不可用: {e}")
        return
    sess = d.get("sessions") or []
    total = sum(s.get("count", 0) for s in sess)
    rep.ok("精彩问答", f"已归档 {total} 条 / {len(sess)} 场 · 本场 {d.get('current','?')}")
    # 图片卡依赖：Pillow + 中文字体
    try:
        import PIL  # noqa: F401
    except Exception:
        rep.warn("精彩问答", "Pillow 缺失→图片卡不可用", fix="pip install pillow")
        return
    font = os.environ.get("HIGHLIGHTS_FONT", r"C:\Windows\Fonts\msyh.ttc")
    if not os.path.exists(font):
        rep.warn("精彩问答", f"中文字体不存在: {font}", fix="set HIGHLIGHTS_FONT 指向可用 .ttc/.ttf")


def check_disk(rep):
    try:
        usage = shutil.disk_usage(str(BASE))
        free_gb = usage.free / 1e9
        if free_gb < 2:
            rep.crit("磁盘", f"剩余空间仅 {free_gb:.1f}GB",
                     fix="清理 recordings/、logs/、clone_outputs/ 释放空间")
        elif free_gb < 10:
            rep.warn("磁盘", f"剩余空间 {free_gb:.1f}GB 偏低")
        else:
            rep.ok("磁盘", f"剩余 {free_gb:.0f}GB")
    except Exception as e:
        rep.warn("磁盘", f"磁盘检查失败: {e}")
    # 录制目录体积
    rec = BASE / "recordings"
    if rec.is_dir():
        sz = sum(f.stat().st_size for f in rec.glob("*.mp4")) / 1e9
        if sz > 5:
            rep.warn("磁盘", f"recordings/ 已占 {sz:.1f}GB",
                     fix="在看板下载后清理旧录制")


def check_files(rep):
    must = ["avatar_hub.py", "conversation.py", "metrics.py",
            "stream_out.py", "profile_package.py", "health_monitor.py"]
    missing = [f for f in must if not (BASE / f).is_file()]
    if missing:
        rep.crit("文件", f"缺少核心文件: {', '.join(missing)}")
    else:
        rep.ok("文件", "核心模块齐全")


# 自愈链上被自动 call / 计划任务拉起的 .bat（任何非 ASCII 都会让自愈失效，判 crit）：
# env_config/secrets/deploy.env 被链式 call 注入环境；_watchdog_task / start_mem_watchdog
# 是看门狗自身的引导器（崩了 = 整个自愈系统起不来，且无人盯控制台）。
_LAUNCHER_CRITICAL_BATS = {"env_config.bat", "secrets.bat", "deploy.env.bat",
                           "_watchdog_task.bat", "start_mem_watchdog.bat"}


def check_bat_encoding(rep):
    """启动器 .bat 必须纯 ASCII（编码红线守卫）。rem 注释里的非 ASCII（尤其中文/全角括号/句号）在
    chcp 65001 下会打乱 cmd 批处理解析器——注释文字被当命令执行，`call env_config.bat` 中途 exit 255、
    python 永不启动、看门狗自愈直接失效。这正是历史上 secrets.bat / env_config.bat / deploy.env.bat 三次
    踩坑的同一类根因；把它在体检/预检阶段拦死，杜绝“中文进 bat → 自愈空窗”复发。"""
    bats = sorted(BASE.glob("*.bat"))
    if not bats:
        rep.info("BAT编码", "未发现 .bat 启动器（跳过）")
        return
    bad_crit, bad_info = [], []
    for p in bats:
        try:
            raw = p.read_bytes()
        except Exception:
            continue
        hits = []
        for i, ln in enumerate(raw.split(b"\n"), 1):
            if not any(b > 127 for b in ln):
                continue
            # `echo 中文` / `title 中文` 在 chcp 65001 下只是显示，不打断解析器（安全，跳过）；
            # 真隐患是 rem/::/set/call 及裸 token 里的非 ASCII（会被当命令执行→断链）。
            head = ln.lstrip().lower()
            if head.startswith((b"echo", b"@echo", b"title")):
                continue
            hits.append(i)
        if not hits:
            continue
        name = p.name
        # 仅「看门狗/链式自动 call」的启动器算 crit：它们崩了自愈静默失效、且没人盯着控制台。
        # _launch_*（看门狗 launcher）、env_config/secrets/deploy.env（被 call 注入环境）。
        is_launcher = (name in _LAUNCHER_CRITICAL_BATS
                       or name.startswith("_launch_") or "detached" in name.lower())
        (bad_crit if is_launcher else bad_info).append((name, hits))
    if not bad_crit and not bad_info:
        rep.ok("BAT编码", f"{len(bats)} 个 .bat 启动链编码安全（rem/set 等关键行全 ASCII）")
        return
    for name, hits in bad_crit:
        head = ",".join(map(str, hits[:6])) + ("…" if len(hits) > 6 else "")
        rep.crit("BAT编码",
                 f"自愈启动器 {name} 含非 ASCII（行 {head}）→ chcp 65001 下会打断 call 链、python 起不来、看门狗自愈失效",
                 fix=f"把 {name} 这些行的中文/全角字符改 ASCII（中文说明移到文档或 /setup 向导 UI，不进被 call 的 .bat）")
    if not bad_crit:
        rep.ok("BAT编码", "自愈启动链 .bat 全 ASCII（安全）")
    for name, hits in bad_info:
        head = ",".join(map(str, hits[:6])) + ("…" if len(hits) > 6 else "")
        rep.info("BAT编码",
                 f"{name} 注释/set 行含非 ASCII（行 {head}）：非自愈链的独立启动器，建议改 ASCII 以防 chcp 下解析异常")


# ── 前端完整性静态校验（离线）─────────────────────────────────────
# 裸调用全局/内置（前面无 "." 的顶层调用）白名单：用负向后顾排除 .map()/.filter()
# 这类方法调用，故白名单只需收录“裸写”的全局函数与 Alpine 魔术 + 控制关键字。
_UI_BUILTIN_CALLS = {
    # JS 全局函数
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "setTimeout", "setInterval", "clearTimeout",
    "clearInterval", "fetch", "alert", "confirm", "prompt", "atob", "btoa",
    "structuredClone", "requestAnimationFrame", "queueMicrotask",
    # 构造器/命名空间（裸调用如 Number(x)/Date()/Array()/Object.x()）
    "Number", "String", "Boolean", "Array", "Object", "JSON", "Math", "Date",
    "Promise", "Map", "Set", "RegExp", "URL", "Blob", "FileReader", "Intl",
    "Error", "Symbol", "BigInt", "WeakMap", "WeakSet",
    # 浏览器媒体/DOM 全局构造器（new Audio(url).play() 这类模板内联用法是合法的）
    "Audio", "Image", "AudioContext", "MediaRecorder", "AbortController",
    # 浏览器全局对象（裸引用后跟调用很少，但保险收录）
    "window", "document", "console", "localStorage", "sessionStorage",
    "navigator", "location", "history",
    # 控制流关键字（表达式里可能出现 typeof()/可忽略，但避免误报）
    "if", "for", "while", "switch", "return", "typeof", "instanceof", "new",
    "await", "void", "delete", "in", "of", "do", "else", "catch", "function",
    "true", "false", "null", "undefined", "NaN", "Infinity", "this", "super",
    # 项目内全局工具（非 hub() 方法，但确实定义在 <script> 顶层）
    "$", "showToast", "avatarSeed", "friendlyErr",
    # Alpine 魔术
    "$el", "$refs", "$nextTick", "$watch", "$dispatch", "$event", "$store",
    "$data", "$root", "$id", "$persist",
}


def _audit_html(path):
    """纯静态扫描单个前端 HTML，返回 (broken_refs, orphan_tabs)。
    broken_refs：模板里 @click/x-text 等“顶层自定义函数调用”却在 <script> 里找不到定义；
    orphan_tabs：tabs 列表声明了 id 却没有对应 x-show=\"tab==='id'\" 面板（点了空白）。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    script = "\n".join(re.findall(r"<script>(.*?)</script>", src, re.S))
    # Alpine 组件可能外置到本地 js（如 ui.html → hub.js）：这些定义也算数，
    # 否则整组方法被误判 broken。仅并入同目录本地脚本；vendor/外链不扫（其符号走白名单）。
    for sm in re.finditer(r'<script[^>]+src="([^"]+)"', src):
        u = sm.group(1).split("?")[0]
        if "://" in u or "/vendor/" in u or not u.endswith(".js"):
            continue
        f = path.parent / u.rsplit("/", 1)[-1]
        if f.is_file():
            try:
                script += "\n" + f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

    defined = set()
    # 方法定义： name(args){ / async name(){ / get name(){
    for m in re.finditer(r"(?:async\s+)?(?:get\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", script):
        defined.add(m.group(1))
    # 对象字面量键： name:
    for m in re.finditer(r"(?:^|[\{,]\s*)([A-Za-z_$][\w$]*)\s*:", script):
        defined.add(m.group(1))
    # 箭头/赋值： name = (..)=> / name: (..)=>
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?\(", script):
        defined.add(m.group(1))
    # 顶层函数声明 / const name = / 全局 const $=...
    for m in re.finditer(r"(?:function|const|let|var)\s+([A-Za-z_$][\w$]*)", script):
        defined.add(m.group(1))

    # 模板中 Alpine 属性值里“前面无点”的顶层调用（排除 .map() 等方法调用）
    attr_vals = re.findall(r'(?:@[\w.:]+|x-[\w:]+|:[\w.-]+)\s*=\s*"([^"]*)"', src)
    called = {}
    for v in attr_vals:
        for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", v):
            called[m.group(1)] = called.get(m.group(1), 0) + 1

    broken = sorted(k for k in called if k not in defined and k not in _UI_BUILTIN_CALLS)

    tab_ids = set(re.findall(r"\{\s*id\s*:\s*['\"](\w+)['\"]", src))
    panels = set(re.findall(r"tab\s*===?\s*['\"](\w+)['\"]", src))
    orphan = sorted(t for t in tab_ids if t not in panels)
    return broken, orphan


def check_ui_integrity(rep):
    """前端完整性（离线静态）：模板调用的自定义函数是否都有定义 + Tab 是否都有面板。
    防 UI 改动引入坏引用（运行时 @click 报错）或孤儿 Tab（点了空白）。"""
    static = BASE / "static"
    targets = [("工作台 ui.html", static / "ui.html"),
               ("对话页 phone.html", static / "phone.html")]
    checked = 0
    for label, f in targets:
        if not f.is_file():
            continue
        checked += 1
        try:
            broken, orphan = _audit_html(f)
        except Exception as e:
            rep.warn("前端完整性", f"{label} 扫描异常: {e}")
            continue
        if broken:
            rep.crit("前端完整性", f"{label} 模板引用未定义函数：{', '.join(broken[:6])}"
                     + (f" 等 {len(broken)} 个" if len(broken) > 6 else ""),
                     fix="检查最近 UI 改动是否打字错/删改遗留——这些 @click/x-text 运行时会报错")
        if orphan:
            rep.warn("前端完整性", f"{label} 有无面板的 Tab：{', '.join(orphan)}",
                     fix="为该 tab 补 x-show 面板，或从 tabs 列表移除")
        if not broken and not orphan:
            rep.ok("前端完整性", f"{label} 模板引用与 Tab 面板齐全")
    if not checked:
        rep.warn("前端完整性", "未找到 static/*.html，跳过")


# 端口表里有、但由外部部署(非本仓库启动器管理)的真实服务——不算缺陷，不报幽灵。
_EXTERNAL_PROBED = {"rvc"}   # RVC 变声：外部 RVC-WebUI(api_240604.py) 提供，/inputDevices 探测

# 可选服务关键模型权重（相对 BASE）：仅"脚本+环境"齐备不代表能跑——重模型权重常需另行下载。
# 这里登记每个服务的标志性权重/目录，缺失即"代码就绪但启动即失败"，避免护栏过度宣称"部署就绪"。
_SERVICE_WEIGHTS = {
    "enhance":    ["GFPGANv1.4.pth"],
    "latentsync": ["LatentSync/checkpoints/latentsync_unet.pt"],
    "hair":       ["HairFastGAN/pretrained_models/StyleGAN/ffhq.pt"],
    "tts":        ["alltalk_tts/models"],
    "singing":    ["GPT-SoVITS/GPT_SoVITS/pretrained_models"],
}


def check_extras_readiness(rep):
    """可选增强服务「可部署性」离线核对（不启动服务、不占显存）：
    ① 入口脚本 + conda 环境 + 关键模型权重 三者齐备才算「部署就绪」；
    ② 代码就绪(脚本+环境)但缺模型权重 → 单列警告(启动即失败，常因权重未下载)；
    ③ 幽灵服务检出：端口表登记却无启动定义、又非已知外部来源、又搜不到脚本的 key。
    目的：防「扩展能力名实不符」（tryon 那种从未实现 / 缺权重却宣称就绪）。"""
    opt = {k: v for k, v in app_config.SERVICES.items() if not v.get("core")}
    ready, broken, noweight = [], [], []
    for k, s in opt.items():
        script_ok = (BASE / s.get("script", "")).is_file()
        env_ok = Path(app_config.conda_python(s.get("env", ""))).exists()
        if not (script_ok and env_ok):
            miss = []
            if not script_ok:
                miss.append(f"脚本({s.get('script','?')})")
            if not env_ok:
                miss.append(f"环境({s.get('env','?')})")
            broken.append((s.get("label", k), "、".join(miss)))
            continue
        wpaths = _SERVICE_WEIGHTS.get(k)
        if wpaths:
            missing_w = [w for w in wpaths if not (BASE / w).exists()]
            if missing_w:
                noweight.append((s.get("label", k), missing_w[0]))
                continue
        ready.append(k)
    if ready:
        rep.ok("可选增强", f"{len(ready)} 个进阶服务部署就绪(脚本+环境+权重齐备，set START_EXTRAS=1 启用): "
               + "、".join(app_config.SERVICES[k].get("label", k) for k in ready))
    for label, mw in noweight:
        # 可选增强缺权重：归 info（advisory），不拉低核心交付结论——与"可选服务离线=info"口径一致。
        # 仍清楚告知"启动前需补权重"，避免有人开 START_EXTRAS=1 时踩空。
        rep.info("可选增强", f"{label} 代码就绪但缺模型权重(启用前需下载到 {mw}，否则启动即失败)")
    for label, miss in broken:
        rep.warn("可选增强", f"{label} 不可部署：缺 {miss}",
                 fix="补齐脚本/conda 环境；或从 app_config.SERVICES 移除该项，避免名实不符")

    # 幽灵服务：DEFAULT_PORTS 有、SERVICES 无、又非已知外部、且搜不到脚本
    for k in app_config.DEFAULT_PORTS:
        if k in app_config.SERVICES or k in _EXTERNAL_PROBED or k == "hub":
            continue
        guess = list(BASE.glob(f"*{k}*server*.py")) + list(BASE.glob(f"*{k}*api*.py"))
        if not guess:
            rep.warn("可选增强", f"幽灵服务「{k}」：端口表登记却无启动定义/脚本/已知外部来源",
                     fix=f"实现 {k} 服务并登记 SERVICES，或从 DEFAULT_PORTS 移除，避免 UI 长期显示离线误导")


def check_default_voice(rep):
    """无克隆音角色的「默认音色」可用性离线核对（不启动服务、纯查文件）：
    对话/合成在角色无 voice_b64 时会回退 CosyVoice /v1/tts（用其内置参考 asset/zero_shot_prompt.wav），
    再退 XTTS（用 alltalk_tts/voices/*.wav）。两者皆缺 → 无克隆音角色无法用默认音出声。"""
    cosy_ref = BASE / "CosyVoice" / "asset" / "zero_shot_prompt.wav"
    vdir = BASE / "alltalk_tts" / "voices"
    xtts_voices = list(vdir.glob("*.wav")) if vdir.exists() else []
    if cosy_ref.exists():
        rep.ok("默认音色", "无克隆音角色可用默认音（CosyVoice 内置参考就位）"
               + ("，XTTS 兜底亦就位" if xtts_voices else ""))
    elif xtts_voices:
        rep.ok("默认音色", f"无克隆音角色可走 XTTS 默认音（voices/ 有 {len(xtts_voices)} 个参考）；"
               "CosyVoice 内置参考缺失")
    else:
        rep.warn("默认音色", "无克隆音角色将无法用默认音合成（CosyVoice 内置参考与 XTTS voices/ 均缺）",
                 fix="部署 CosyVoice（含 asset/zero_shot_prompt.wav），或在 alltalk_tts/voices/ 放一段参考音，"
                     "或给角色绑定克隆音色")


def check_copy_hygiene(rep):
    """用户面文案卫生（防回归 · 离线）：对话页 phone.html 的用户提示(setStatus/showToast)
    不得直接把原始异常 e.message（如 Failed to fetch / offer 500）抛给最终用户——应经
    friendlyErr(e) 翻成人话、原文仅进 console。仅扫 setStatus/showToast，不动运营面的
    setTuneStatus 等（那是给操作者看的，保留技术细节）。"""
    f = BASE / "static" / "phone.html"
    if not f.is_file():
        rep.warn("文案卫生", "未找到 static/phone.html，跳过")
        return
    leaks = []
    for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if ("setStatus(" in line or "showToast(" in line) \
                and "e.message" in line and "friendlyErr" not in line:
            leaks.append(i)
    if leaks:
        rep.warn("文案卫生",
                 f"对话页 {len(leaks)} 处直接把原始异常(e.message)抛给用户(行 {', '.join(map(str, leaks[:8]))})",
                 fix="改用 friendlyErr(e) 翻成人话；原始异常留给 console.error")
    else:
        rep.ok("文案卫生", "对话页用户提示无原始异常泄漏（已人话化）")


# ══════════════════════════════════════════════════════════════════
#  开机前离线预检（--preflight）：不依赖 Hub，验证「这台机能不能跑起来」。
#  GPU/显存 · conda 解释器 · 服务脚本/资源 · 多卡副本直连 · 端口 · 磁盘。
# ══════════════════════════════════════════════════════════════════

def _probe_health(url, timeout=2.5):
    """直连探测：服务器有任何 HTTP 响应(含 5xx)即视为可达；仅连接级失败算 down。"""
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _port_listening(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _svc_replicas(key):
    """该服务的副本地址列表 + 是否远程/多副本。SVC_<KEY> 逗号分隔；空=本机默认端口。"""
    raw = os.environ.get("SVC_" + key.upper(), "").strip()
    if raw:
        urls = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
        return urls, True
    port = app_config.DEFAULT_PORTS.get(key, 0)
    return [f"http://127.0.0.1:{port}"], False


def pf_scripts(rep):
    miss = [f"{k}({s['script']})" for k, s in app_config.SERVICES.items()
            if not (BASE / s["script"]).is_file()]
    if miss:
        crit_core = [m for m in miss if app_config.SERVICES.get(m.split("(")[0], {}).get("core")]
        (rep.crit if crit_core else rep.warn)("服务脚本", f"缺少: {', '.join(miss)}")
    else:
        rep.ok("服务脚本", f"{len(app_config.SERVICES)} 个服务脚本齐全")
    for label, d in [("faces 人脸库", "faces"), ("static 前端", "static")]:
        if (BASE / d).is_dir():
            rep.ok("资源目录", f"{label} 存在")
        else:
            rep.warn("资源目录", f"{label} 缺失（{d}/）")


def pf_conda(rep):
    envs = {}
    for s in app_config.SERVICES.values():
        envs.setdefault(s["env"], False)
        if s.get("core"):
            envs[s["env"]] = True
    for env in sorted(envs):
        py = app_config.conda_python(env)
        core = envs[env]
        if Path(py).exists():
            rep.ok("Python 环境", f"{env} 解释器就绪")
        elif core:
            rep.crit("Python 环境", f"核心环境 {env} 的 python 不存在：{py}",
                     fix=f"创建该 conda 环境，或设 AVATARHUB_PY_{env.upper()}=<python.exe>")
        else:
            rep.warn("Python 环境", f"扩展环境 {env} 缺失（仅影响扩展服务）")


def pf_gpu(rep):
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            rep.warn("GPU", "nvidia-smi 调用失败（驱动异常？）")
            return
        lines = [l for l in out.stdout.strip().splitlines() if l.strip()]
        if not lines:
            rep.warn("GPU", "未检测到 NVIDIA GPU")
            return
        rep.ok("GPU", f"检测到 {len(lines)} 张 NVIDIA 卡（多卡可配 CONV_MAX_CONCURRENT=auto 分担）")
        for l in lines:
            parts = [x.strip() for x in l.split(",")]
            if len(parts) < 4:
                continue
            idx, name, tot, free = parts[0], parts[1], parts[2], parts[3]
            try:
                free_gb, tot_gb = float(free) / 1024, float(tot) / 1024
            except ValueError:
                rep.ok("GPU", f"#{idx} {name}")
                continue
            if free_gb < 4:
                rep.warn("GPU", f"#{idx} {name}: 空闲 {free_gb:.1f}/{tot_gb:.1f}GB 偏低",
                         fix="关闭占显存的程序，或让该卡不承载重模型")
            else:
                rep.ok("GPU", f"#{idx} {name}: 空闲 {free_gb:.1f}/{tot_gb:.1f}GB")
    except FileNotFoundError:
        rep.warn("GPU", "未找到 nvidia-smi（NVIDIA 驱动未安装？）",
                 fix="安装 NVIDIA 驱动；实时数字人需 GPU")
    except Exception as e:
        rep.warn("GPU", f"GPU 检测失败: {e}")


def pf_endpoints(rep):
    """核心 + 情感 TTS（多卡分担常用）的端点预检：本机看端口，远端/多副本逐个直连 /health。"""
    keys = [k for k, s in app_config.SERVICES.items() if s.get("core")] + ["emotion_tts"]
    for key in keys:
        s = app_config.SERVICES.get(key, {})
        urls, remote = _svc_replicas(key)
        health = s.get("health", "/health")
        if not remote and len(urls) == 1:
            hp = urls[0].split("//")[-1]
            host, _, port = hp.partition(":")
            up = _port_listening(host or "127.0.0.1", port or 80)
            rep.ok("服务端点", f"{key} 本机 :{port} {'已运行' if up else '待启动'}")
            continue
        ok_n = 0
        for u in urls:
            if _probe_health(u + health):
                ok_n += 1
            else:
                rep.warn("服务端点", f"{key} 副本不可达：{u}",
                         fix="确认该机服务已起、绑定 0.0.0.0、防火墙放行端口")
        if ok_n == len(urls):
            rep.ok("服务端点", f"{key} {ok_n}/{len(urls)} 副本在线")
        elif ok_n == 0:
            rep.crit("服务端点", f"{key} 全部 {len(urls)} 副本不可达",
                     fix="对话主链路依赖它；先恢复副本再启动 Hub")
        else:
            rep.ok("服务端点", f"{key} {ok_n}/{len(urls)} 副本在线（部分待恢复）")


def pf_streaming_tts(rep):
    """单句内流式 TTS（CONV_TTS_STREAMING=1）：确认部署的 fish_speech_server 含 /v1/tts/clone/stream。
    离线文件级核对，避免“开了开关但跑的是旧版 fish 服务（404）→ 整句静默”。"""
    if os.environ.get("CONV_TTS_STREAMING", "0") != "1":
        return
    f = BASE / "fish_speech_server.py"
    try:
        ok = f.is_file() and "/v1/tts/clone/stream" in f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        ok = False
    if ok:
        rep.ok("流式TTS", "已启用 · fish 服务含 /v1/tts/clone/stream（边出边喂口型）")
    else:
        rep.crit("流式TTS", "CONV_TTS_STREAMING=1 但 fish_speech_server 缺流式端点",
                 fix="部署含 /v1/tts/clone/stream 的 fish_speech_server.py 并重启该服务，或暂关 CONV_TTS_STREAMING")


def run_preflight(rep):
    pf_scripts(rep)
    pf_conda(rep)
    pf_gpu(rep)
    pf_endpoints(rep)
    pf_streaming_tts(rep)
    check_bat_encoding(rep)
    check_ui_integrity(rep)
    check_extras_readiness(rep)
    check_default_voice(rep)
    check_copy_hygiene(rep)
    check_disk(rep)


def run_full(rep, profile):
    check_files(rep)
    check_bat_encoding(rep)
    check_ui_integrity(rep)
    check_extras_readiness(rep)
    check_default_voice(rep)
    check_copy_hygiene(rep)
    health = check_hub(rep)
    check_services(rep, health)
    check_monitor(rep)
    check_license(rep)
    check_llm(rep)
    check_profiles(rep, profile)
    check_golden(rep, profile)
    check_supervisor(rep)
    check_backpressure(rep)
    check_capacity(rep)
    check_audience(rep)
    check_highlights(rep)
    check_disk(rep)


def _safe_icons():
    """控制台编码（如 GBK）放不下 ✓/⚠/✗ 时退回 ASCII，绝不因编码崩溃。"""
    try:
        "✓⚠✗·".encode(sys.stdout.encoding or "utf-8")
        return {"ok": "✓", "warn": "⚠", "crit": "✗", "info": "·"}
    except Exception:
        return {"ok": "[OK]", "warn": "[! ]", "crit": "[X ]", "info": "[ -]"}


def main():
    try:
        sys.stdout.reconfigure(errors="replace")   # 任何不可映射字符替换而非崩溃
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--preflight", action="store_true",
                    help="开机前离线预检（不连 Hub）：GPU/conda/脚本/多卡副本/端口/磁盘")
    ap.add_argument("--profile", default="",
                    help="主角色名；省略则自动跟随当前激活角色（避免硬编码历史角色名误报）")
    args = ap.parse_args()

    rep = Report()
    title = "开机前预检报告" if args.preflight else "体检报告"
    if args.preflight:
        run_preflight(rep)
    else:
        run_full(rep, args.profile)

    if args.json:
        print(json.dumps({
            "mode": "preflight" if args.preflight else "full",
            "exit_code": rep.worst,
            "items": [{"level": l, "area": a, "msg": m} for l, a, m in rep.items],
            "suggestions": rep.suggestions,
        }, ensure_ascii=False, indent=2))
        return rep.worst

    icon = _safe_icons()
    print(f"=== AvatarHub {title} ===")
    for level, area, msg in rep.items:
        print(f"  {icon[level]} [{area}] {msg}")
    n_crit = sum(1 for i in rep.items if i[0] == "crit")
    n_warn = sum(1 for i in rep.items if i[0] == "warn")
    n_info = sum(1 for i in rep.items if i[0] == "info")
    _info_tail = f"（另有 {n_info} 项可选信息）" if n_info else ""
    print(f"\n结论：{'严重问题 ' + str(n_crit) + ' 项 · ' if n_crit else ''}"
          f"{'警告 ' + str(n_warn) + ' 项' if n_warn else '全部健康 ' + icon['ok']}{_info_tail}")
    if rep.suggestions:
        print("\n建议处理：")
        for s in rep.suggestions:
            print(f"  → {s}")
    return rep.worst


if __name__ == "__main__":
    sys.exit(main())
