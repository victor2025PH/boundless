# -*- coding: utf-8 -*-
"""上线前一键体检：串联 Phase1-3 全部回归脚本，输出 PASS/FAIL 总表。

用法：
    python acceptance.py            # 快速集：多语种文本 / 偏好健壮性 / 浏览器E2E(含WebRTC) / 打断 / UI可视化回归
    python acceptance.py --full     # 追加重负载：语音多语种 / 并发 / 长稳 / 故障自愈(会杀 vcam)
    python acceptance.py --only e2e,bargein

设计：每个子脚本以子进程跑（同一 conda 环境），强制 UTF-8 输出，按其末尾
"== ... ==" 摘要行里的 N/M 比值或 PASS/FAIL 标记判定；汇总成总表并落 JSON 报告。
"""
import sys, os, re, json, time, argparse, subprocess, urllib.request, urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HUB = os.environ.get("ACCEPT_HUB", "http://127.0.0.1:9000")
PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "logs", "acceptance_report.json")
HISTORY = os.path.join(HERE, "logs", "acceptance_history.jsonl")
HIST_MAX = 300

# P9-1 门禁抗争用：GPU 重负载阶段开跑前预检显卡占用。
#   实锤（开播页 P7 轮）：并行批次把 GPU 打满时 voicequality 89s→超时180s、bargein 误败，
#   空闲复跑即 12/12——「测试互相打爆」产出的全是假红。在播换脸时硬跑重测试同样会给直播添堵。
#   策略=有界等待后 SKIP 而非硬跑：既不出假红也不伤在播；SKIP 理由带复跑命令，不静默漏检。
#   旋钮：ACCEPT_GPU_BUSY_UTIL(默认80，>100=关闭预检) / ACCEPT_GPU_WAIT_SEC(默认90，0=不等待直接判)。
# P10-5：lang 补入——6 用例逐个 speak:True（TTS+RVC 真出声），GPU 打满时同样会超时假红；
#   空闲时预检只花 ~3s 采样，代价可忽略。
GPU_HEAVY = {"lang", "voicequality", "bargein", "voicelang", "concurrency", "longrun"}


def _gpu_util_once():
    """当前 GPU 利用率%（多卡取最大）。nvidia-smi 不可用(无N卡/驱动异常)→None=不拦。"""
    try:
        p = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=8)
        vals = [int(x.strip()) for x in (p.stdout or "").splitlines() if x.strip().isdigit()]
        return max(vals) if vals else None
    except Exception:
        return None


def _gpu_contended():
    """3 采样(间隔1s)取均值抗瞬时尖峰。返回 (是否争用, util均值或None)。"""
    thr = float(os.environ.get("ACCEPT_GPU_BUSY_UTIL", "80"))
    samples = []
    for _ in range(3):
        u = _gpu_util_once()
        if u is None:
            return False, None
        samples.append(u)
        time.sleep(1)
    avg = sum(samples) / len(samples)
    return avg > thr, round(avg)


def _gpu_wait_idle(key):
    """重负载项开跑闸：忙→有界等待(轮询10s)；到时仍忙→返回 SKIP 理由字符串；可跑→None。"""
    busy, util = _gpu_contended()
    if not busy:
        return None
    wait_max = float(os.environ.get("ACCEPT_GPU_WAIT_SEC", "90"))
    t0 = time.time()
    while time.time() - t0 < wait_max:
        print("  [争用] GPU util≈%s%% —— 等空闲 %d/%ds…" % (util, int(time.time() - t0), int(wait_max)))
        time.sleep(10)
        busy, util = _gpu_contended()
        if not busy:
            return None
    return ("SKIP: GPU 争用(util≈%s%%>阈值%s)，硬跑必假红且伤在播——错峰后复跑: "
            "python acceptance.py --only %s"
            % (util, os.environ.get("ACCEPT_GPU_BUSY_UTIL", "80"), key))


# (key, 标题, 脚本/None, 超时s, 是否重负载)
SUITE = [
    ("pref",        "偏好健壮性(脏语种归一)",      None,                   20,  False),
    ("streaming",   "流式TTS就绪(开关一致性)",      None,                   20,  False),
    ("voiceassets", "角色库音色完整性(活跃角色须有音色)", None,             20,  False),
    ("healthchain", "开播健康自愈链路(状态/时间线/场次/甘特/配置/路由)", None, 25, False),
    ("healthdrill", "自愈演练(护栏决策+复盘留痕·安全)", None, 30, True),
    ("secpanorama", "安全面全景契约(演习/令牌/拓扑三指标)", None, 20, False),
    ("pipeline",    "全链路健康自检(各服务/health+换脸掉CPU等红旗)", None, 60, False),
    ("embedjs",     "内嵌JS体检(.py+static前端·防脚本转义失效)", None, 30, False),
    ("glosssurv",   "术语锁定双向存活(占位过真实MT自检)", None, 75, False),
    ("convintel",   "对话智能(混合检索/记忆/共情/TRT)", "_conv_intelligence_verify.py", 180, False),
    ("fesmoke",     "前端冲烟(零pageerror)",       "_fe_smoke.py",          90, False),
    ("feinteract",  "前端交互冒烟(自救卡/建议条真点击+埋点真发出)", os.path.join("tools", "_fe_interact.py"), 120, False),
    ("uivr",        "UI可视化回归(像素基线·无头Edge)", None,               420, False),
    ("lang",        "多语种文本对话",              "_lang_verify.py",      240, False),
    ("voicequality","真出声质量·多角色矩阵",        "_voice_quality.py",    300, False),
    ("bargein",     "实时打断(停嘴/无残音)",        "_bargein_verify.py",   150, False),
    ("e2e",         "浏览器E2E(加载/对话/降级/WebRTC出画)", "_e2e_phone.py", 150, False),
    ("voicelang",   "多语种语音输入",              "_voice_lang_verify.py", 180, True),
    ("concurrency", "并发压测",                    "_concurrency_test.py", 180, True),
    ("longrun",     "长稳+记忆回忆",               "_longrun_test.py",     240, True),
    ("recovery",    "故障自愈(杀 vcam→守护拉起)",   "_recovery_test.py",    200, True),
]


def _verdict(out: str):
    """从子脚本输出判定 PASS/FAIL：优先末尾 '== ... ==' 摘要行里的 N/M 比值，
    否则看 PASS/FAIL/PARTIAL 标记。"""
    sums = re.findall(r"==[^\n]*==", out)
    scope = sums[-1] if sums else out[-500:]
    m = re.search(r"(\d+)\s*/\s*(\d+)", scope)
    if m:
        return int(m.group(1)) == int(m.group(2)), scope.strip()
    if "PARTIAL" in scope or "FAIL" in scope:
        return False, scope.strip()
    if "PASS" in scope:
        return True, scope.strip()
    return False, (scope.strip() or "<无摘要>")


# 自检工具「未配置」信号（区别于真实失败）：浏览器 E2E 依赖 playwright + 其浏览器二进制，
# 属可选自检工具而非产品功能；未装时应 SKIP（不阻断「可交付」），并提示安装命令。
_UNPROVISIONED_MARKERS = (
    "No module named 'playwright'",
    "playwright install",            # playwright 缺浏览器时的标准提示
    "Executable doesn't exist",      # 浏览器二进制缺失
    "looks like Playwright was just installed",
)


def _is_unprovisioned_tool(out: str) -> bool:
    return any(mk in out for mk in _UNPROVISIONED_MARKERS)


def _send_alert(subject: str, lines: list) -> list:
    """体检告警外发（best-effort，按环境变量开启对应通道，未配置则跳过）：
      ACCEPT_WEBHOOK_URL  通用 webhook（POST JSON {subject,text,lines}）
      ACCEPT_WECOM_KEY    企业微信群机器人 key（markdown）
      ACCEPT_SERVERCHAN   Server酱 SendKey（手机推送）
    返回成功外发的通道名列表。"""
    text = subject + "\n" + "\n".join(lines)
    sent = []
    url = os.environ.get("ACCEPT_WEBHOOK_URL", "").strip()
    wecom = os.environ.get("ACCEPT_WECOM_KEY", "").strip()
    sc = os.environ.get("ACCEPT_SERVERCHAN", "").strip()

    def _raw_post(u, data: bytes, ctype="application/json"):
        req = urllib.request.Request(u, data=data, headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()
    if url:
        try:
            _raw_post(url, json.dumps({"subject": subject, "text": text,
                                       "lines": lines}, ensure_ascii=False).encode("utf-8"))
            sent.append("webhook")
        except Exception as e:
            print("  [告警] webhook 失败: %s" % e)
    if wecom:
        try:
            api = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + wecom
            body = {"msgtype": "markdown", "markdown": {"content": text}}
            _raw_post(api, json.dumps(body, ensure_ascii=False).encode("utf-8"))
            sent.append("wecom")
        except Exception as e:
            print("  [告警] 企业微信 失败: %s" % e)
    if sc:
        try:
            api = "https://sctapi.ftqq.com/%s.send" % sc
            body = urllib.parse.urlencode({"title": subject, "desp": text}).encode("utf-8")
            _raw_post(api, body, "application/x-www-form-urlencoded")
            sent.append("serverchan")
        except Exception as e:
            print("  [告警] Server酱 失败: %s" % e)
    return sent


_AH_TOKEN = os.environ.get("AVATARHUB_API_TOKEN", "").strip()


def _auth_headers(h=None):
    h = dict(h or {})
    if _AH_TOKEN:                      # 启用鉴权且全量强制时，本地脚本也带令牌
        h["X-AH-Token"] = _AH_TOKEN
    return h


def _get(path, timeout=15):
    req = urllib.request.Request(HUB + path, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(HUB + path, data=data,
                                 headers=_auth_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def preflight():
    """探活 + 列出关键服务。返回 (ok, snapshot)。"""
    try:
        snap = _get("/api/ops/snapshot", timeout=20)
    except Exception as e:
        print("  [致命] Hub 不可达 %s : %s" % (HUB, e))
        return False, {}
    svc = snap.get("services", {})
    need = ["fish_tts", "stt", "vcam", "lipsync"]
    print("  Hub: %s  压力=%s gpu=%s%% ram=%s%%" % (
        HUB, snap.get("pressure"), snap.get("gpu_util"), snap.get("ram_percent")))
    miss = [k for k in need if not svc.get(k)]
    for k in need:
        print("    [%s] %s" % ("OK" if svc.get(k) else "XX", k))
    if snap.get("pref_warn"):
        print("    [warn] %s" % snap["pref_warn"])
    return (len(miss) == 0), snap


def test_pref():
    """偏好健壮性：写入非法语种应被服务端归一为 auto，且不污染合法值。"""
    try:
        before = _get("/api/ui_prefs").get("prefs", {})
        prev_rl = before.get("reply_lang", "auto")
        # 1) 非法语种码 → 应归 auto
        r1 = _post("/api/ui_prefs", {"prefs": {"reply_lang": "zz9!"}})
        got1 = (r1.get("prefs") or {}).get("reply_lang")
        ok_bad = (got1 == "auto")
        # 2) 合法且链路支持的语种码 → 应保留
        r2 = _post("/api/ui_prefs", {"prefs": {"reply_lang": "en"}})
        got2 = (r2.get("prefs") or {}).get("reply_lang")
        ok_good = (got2 == "en")
        # 3) 合法但链路不支持(如斯瓦希里 sw) → 应降级 auto，并回传 adjusted 提示
        r3 = _post("/api/ui_prefs", {"prefs": {"reply_lang": "sw"}})
        got3 = (r3.get("prefs") or {}).get("reply_lang")
        ok_unsup = (got3 == "auto" and bool(r3.get("adjusted")))
        # 复原
        _post("/api/ui_prefs", {"prefs": {"reply_lang": prev_rl or "auto"}})
        out = "非法'zz9!'→%r ; 支持'en'→%r ; 不支持'sw'→%r(adjusted=%s)" % (
            got1, got2, got3, bool(r3.get("adjusted")))
        if not (ok_bad or ok_good):
            out += "  [提示] 若都未生效，可能 Hub 仍在旧代码，重启后再测"
        return (ok_bad and ok_good and ok_unsup), out
    except Exception as e:
        return False, "异常: %s" % e


def test_gloss_survival():
    """术语锁定「双向存活」开播前门禁：一组术语密集句(含相邻术语压力)过当前真实 MT 双向，占位真·存活率须各向
    healthy(>=WARN，默认85%)。低于阈值=该向术语锁定正被 MT 打散(上线会音译不一致/品牌名乱译)，应先换后端/占位符再上线。
      - 同传(LingoX)未运行 / 词表为空 → SKIP(不阻断交付)
      - 某向 degraded → FAIL(指名哪个语向)
    与线上同一 _translate_raw 路径、绕缓存实测；结果并入在线自检并留基线(logs/gloss_survival.jsonl)。"""
    try:
        r = _post("/interp/glossary/survival_probe", {}, timeout=70)
    except Exception as e:
        return True, "SKIP: 同传(LingoX)未运行或不可达，跳过术语存活自检（%s）" % (str(e)[:80])
    dirs = (r or {}).get("dirs") or []
    if not dirs:
        return True, "SKIP: 词表为空/无术语命中，无占位可测（配置术语后此项自动生效）"
    warn = (r or {}).get("warn", 0.85)
    backend = (r or {}).get("backend", "?")
    parts, bad, regr = [], [], []
    for d in dirs:
        rate = d.get("rate")
        pct = "—" if rate is None else ("%d%%" % round(rate * 100))
        seg = "%s %s(%s/%s)" % (d.get("pair"), pct, d.get("surv"), d.get("ph"))
        dl = d.get("delta")
        if d.get("regress") and dl is not None:      # 较上次同后端明显下滑(即便仍>WARN)→标出,抓漂移
            seg += "↓%dpp" % round(abs(dl) * 100)
            regr.append("%s ↓%dpp(from %d%%)" % (d.get("pair"), round(abs(dl) * 100),
                                                  round((d.get("prev_rate") or 0) * 100)))
        parts.append(seg)
        if d.get("verdict") == "degraded":
            bad.append("%s %s" % (d.get("pair"), pct))
    scope = "占位存活[%s]: %s · WARN=%d%%" % (backend, " ; ".join(parts), round(warn * 100))
    if regr:
        scope += " · ⚠较上次同后端回归: " + " / ".join(regr)
    if bad:      # 绝对劣化才 FAIL(拦上线);回归只在 scope 里提示(不阻断,自愈仍保译文正确)
        return False, "术语锁定被 MT 打散: " + " / ".join(bad) + " < WARN（该向上线会音译不一致，先换 MT 后端/占位符）；" + scope
    return True, scope


def test_streaming():
    """流式 TTS 交付门禁：开关与就绪一致性。
      - 未开启 → PASS（不适用，整句路径，行为同改造前）
      - 开启且就绪(fish 克隆音) → PASS
      - 开启但未就绪 → FAIL（配错：开了流式却没 fish 克隆音，线上会静默回落，需修正再上线）"""
    try:
        cap = _get("/api/capacity")
        st = cap.get("streaming_tts") or {}
        if not st.get("enabled"):
            return True, "流式未启用(整句路径)；如需开启在 /setup 勾选或 set CONV_TTS_STREAMING=1"
        if st.get("eligible"):
            m = (st.get("metrics") or {}).get("conv_stream") or {}
            slo = st.get("slo_ttfa_p95_ms") or 0
            extra = ""
            if m.get("samples", 0) >= 10 and slo and m.get("p95_ms", 0) > slo:
                extra = " [注意]流式p95 %dms 超SLO %dms，建议调小 FIRST_MS 或查 TTS 卡负载" % (
                    m["p95_ms"], slo)
            return True, "流式已开且就绪 · 引擎 %s · 样本 %d%s" % (
                st.get("engine", "?"), m.get("samples", 0), extra)
        return False, "流式已开但未就绪(引擎=%s,克隆音=%s) → 修正角色或暂关 CONV_TTS_STREAMING" % (
            st.get("engine", "?"), st.get("has_clone_voice"))
    except Exception as e:
        return False, "异常: %s" % e


def test_voice_assets():
    """P0g 角色库音色完整性(2026-07-10 无声事故治理)：
      - 活跃角色必须有音色——它是对话/同传的默认出场者，无音色=开场即无声 → FAIL
      - 其余无音色角色属合法中间态(照片库=有脸待配声)，仅通报清单不红灯——防新角色"裸奔"无人知晓
      - Hub 不可达 → SKIP(不阻断交付)"""
    try:
        d = _get("/profiles?fields=name,has_voice,has_face", timeout=10)
    except Exception as e:
        return True, "SKIP Hub 不可达: %s" % str(e)[:80]
    profs = d.get("profiles") or []
    if not profs:
        return True, "SKIP 角色库为空"
    active = d.get("active") or ""
    voiceless = [p.get("name", "") for p in profs if not p.get("has_voice")]
    act_bad = bool(active) and active in voiceless
    lst = ("（" + "、".join(voiceless[:6]) + ("…" if len(voiceless) > 6 else "") + "）") if voiceless else ""
    scope = "角色 %d · 无音色 %d%s · 活跃「%s」%s" % (
        len(profs), len(voiceless), lst, active or "-",
        "无音色！同传/对话开场即无声，先为它配声音或换活跃角色" if act_bad else "有音色")
    return (not act_bad), scope


def _openapi_paths():
    try:
        spec = _get("/openapi.json", timeout=15)
        return set((spec.get("paths") or {}).keys())
    except Exception:
        return set()


def test_healthchain():
    """开播健康·自愈闭环回归自检（守护 P3-C/D 全链路契约，护住已完成闭环不被后续改动回退）：
      状态裁决 /realtime/status → 时间线 /health_timeline → 场次 /health_sessions
      → 甘特 /health_gantt → 运行时配置 /api/heal/config(GET + 幂等POST回写不改行为) → 告警路由(仅验注册,不触发)。
    任一契约破坏(字段缺失/结构变形/路由丢失)即 FAIL。纯 HTTP、只读+幂等，安全可反复跑。"""
    checks = []

    def _ck(label, cond, note=""):
        checks.append((label, bool(cond), note))

    try:                                    # 1) 状态裁决：单一真相 + 服务端自愈让渡标记
        h = (_get("/realtime/status") or {}).get("health") or {}
        _ck("status.state", h.get("state") in {"idle", "svc_down", "noface", "warmup", "lag", "ok"},
            "state=%r" % h.get("state"))
        _ck("status.autoheal_server", isinstance(h.get("autoheal_server"), bool))
    except Exception as e:
        _ck("status", False, "异常 %s" % e)

    try:                                    # 2) 时间线 + 统计口径（守护无浏览器也累计）
        tl = _get("/realtime/health_timeline")
        _ck("timeline.ok", tl.get("ok") is True)
        _ck("timeline.list", isinstance(tl.get("timeline"), list))
        need = {"transitions", "noface", "svc_down", "lag", "recovered",
                "auto_free_vram", "auto_restart", "alert", "last_event_ts"}
        miss = need - set((tl.get("stats") or {}).keys())
        _ck("timeline.stats", not miss, ("缺:" + ",".join(sorted(miss))) if miss else "")
        _ck("timeline.autoheal_on", isinstance(tl.get("autoheal_on"), bool))
    except Exception as e:
        _ck("timeline", False, "异常 %s" % e)

    try:                                    # 3) 场次聚合（按 idle 边界，跨重启可读）
        se = _get("/realtime/health_sessions")
        _ck("sessions.ok", se.get("ok") is True)
        _ck("sessions.list", isinstance(se.get("sessions"), list))
    except Exception as e:
        _ck("sessions", False, "异常 %s" % e)

    try:                                    # 4) 甘特：结构 + (若有场次)段/标记契约
        g = _get("/realtime/health_gantt")
        _ck("gantt.ok", g.get("ok") is True)
        _ck("gantt.now", isinstance(g.get("now"), int))
        ss = g.get("sessions")
        _ck("gantt.list", isinstance(ss, list))
        if isinstance(ss, list) and ss:
            s0 = ss[0]
            shape = (isinstance(s0.get("segments"), list) and isinstance(s0.get("markers"), list)
                     and isinstance(s0.get("dur_s"), int))
            if s0.get("segments"):
                shape = shape and all(k in s0["segments"][0] for k in ("state", "t0", "t1", "dur"))
            _ck("gantt.shape", shape)
    except Exception as e:
        _ck("gantt", False, "异常 %s" % e)

    try:                                    # 5) 运行时配置：GET 契约 + 幂等回写(原样POST,不改行为)+读回一致
        cfg = _get("/api/heal/config")
        _ck("cfg.ok", cfg.get("ok") is True)
        _ck("cfg.autoheal", isinstance(cfg.get("autoheal"), bool))
        _ck("cfg.alerts", isinstance(cfg.get("alerts"), bool))
        ch = cfg.get("channels") or {}
        _ck("cfg.channels", isinstance(ch, dict) and isinstance(ch.get("webhook_count"), int))
        _ck("cfg.guardrails", all(isinstance(cfg.get(k), dict) for k in ("restart", "vram", "alert_dwell")))
        cur = {"autoheal": bool(cfg.get("autoheal")), "alerts": bool(cfg.get("alerts"))}
        # 干跑：翻转值 POST(dry_run) 验证请求处理契约(回显 changed)，但绝不改运行时/不落盘 → 自检可安全反复跑
        flip = {"autoheal": not cur["autoheal"], "alerts": not cur["alerts"], "dry_run": True}
        pr = _post("/api/heal/config", flip)
        _ck("cfg.dryrun", pr.get("dry_run") is True and not pr.get("persisted")
            and pr.get("changed") == {"autoheal": flip["autoheal"], "alerts": flip["alerts"]})
        cfg2 = _get("/api/heal/config")     # 干跑后实际配置必须原封不动
        _ck("cfg.unchanged", cfg2.get("autoheal") == cur["autoheal"] and cfg2.get("alerts") == cur["alerts"])
    except Exception as e:
        _ck("cfg", False, "异常 %s" % e)

    try:                                    # 6) 关键路由仅验「已注册」，绝不触发(避免每次体检刷屏webhook/桌面弹窗)
        paths = _openapi_paths()
        need_r = ["/realtime/status", "/realtime/health_timeline", "/realtime/health_sessions",
                  "/realtime/health_gantt", "/api/heal/config", "/api/heal/test_alert"]
        miss_r = [p for p in need_r if p not in paths]
        _ck("routes", not miss_r, ("缺:" + ",".join(miss_r)) if miss_r else "")
    except Exception as e:
        _ck("routes", False, "异常 %s" % e)

    npass = sum(1 for _, ok, _ in checks if ok)
    n = len(checks)
    fails = ["%s%s" % (lb, ("(" + note + ")") if note else "") for lb, ok, note in checks if not ok]
    scope = "健康自愈链路 %d/%d" % (npass, n)
    scope += (" · 失败: " + "、".join(fails[:6])) if fails else \
             " · 契约全通过(status/timeline/sessions/gantt/config/routes)"
    return (npass == n and n > 0), scope


def test_healthdrill():
    """自愈演练(端到端·安全)：调 /api/heal/selftest —— 服务端用真决策 _sh_plan 跑护栏矩阵
    (驻留/回合/每场上限/冷却/去重/总开关 + 告警越阈/恢复) + 真聚合 _health_sessions/_health_gantt
    复盘一段合成坏态回合(丢脸→自动重启→恢复→收场)。证明「自愈真会按预期动、复盘会如实留痕」，
    全程不碰硬件/不发告警/不动管线/不污染生产时间线。"""
    try:
        d = _get("/api/heal/selftest", timeout=30)
        npass, n = d.get("pass", 0), d.get("total", 0)
        fails = [c.get("name", "?") + (("(" + c.get("detail", "") + ")") if c.get("detail") else "")
                 for c in d.get("checks", []) if not c.get("ok")]
        scope = "自愈演练 %d/%d" % (npass, n)
        scope += (" · 失败: " + "、".join(fails[:5])) if fails else " · 护栏决策+复盘留痕全通过"
        return (bool(d.get("ok")) and n > 0), scope
    except Exception as e:
        return False, "异常: %s" % e


def test_secpanorama():
    """安全面全景契约门禁：/api/ops/snapshot .security 须含「守护巡检 / 演习(上次结果+下次预告) /
    令牌年龄 / 拓扑lint / 轮换态」且结构正确——把安全可视化闭环固化成回归契约，防后续改动静默打破。
    同时把「演习失败 / 拓扑漂移」拦成交付失败(它们本就是不该上线的状态)。纯只读、秒级。"""
    try:
        s = (_get("/api/ops/snapshot") or {}).get("security") or {}
        checks = []

        def ck(label, cond, note=""):
            checks.append((label, bool(cond), note))

        ck("总判定ok字段", isinstance(s.get("ok"), bool))
        ck("活动告警结构", isinstance(s.get("alerts"), list) and isinstance(s.get("alerts_count"), int))
        wd = s.get("watchdog") or {}
        ck("看门狗巡检态", isinstance(wd, dict) and bool(wd), "logs/watchdog_status.json 缺失?")
        nd = s.get("next_drill") or {}
        ck("下次演习预告", bool(re.match(r"^\d+\.\d+\.\d+\.\d+/\w+$", nd.get("target", "")))
           and bool(nd.get("at")) and isinstance(nd.get("iso_week"), int),
           "cluster_map.json 缺失/hosts 为空?")
        tk = s.get("token") or {}
        ck("令牌年龄", isinstance(tk.get("age_days"), int) and tk.get("age_days", -1) >= 0
           and isinstance(tk.get("stale"), bool), "secrets/service_token.txt 缺失?")
        tp = s.get("topology") or {}
        ck("拓扑lint留痕", isinstance(tp.get("ok"), bool) and bool(tp.get("ts")),
           "verify 全量跑过才有 logs/topology_lint.json")
        ck("拓扑无漂移", tp.get("ok") is True, "; ".join((tp.get("drifts") or []))[:80])
        ld = s.get("last_drill")
        if ld:   # 有演习记录才校验（新部署无记录不阻断）
            ck("上次演习非FAIL", str(ld.get("result", "")).upper() != "FAIL",
               (ld.get("target", "") + " " + ld.get("ts", "")).strip())
        rot = s.get("rotate") or {}
        ck("轮换态结构", isinstance(rot.get("running"), bool))
        npass = sum(1 for _, ok, _ in checks if ok)
        n = len(checks)
        fails = ["%s%s" % (lb, ("(" + nt + ")") if nt else "") for lb, ok, nt in checks if not ok]
        scope = "安全面契约 %d/%d" % (npass, n)
        scope += (" · 失败: " + "、".join(fails[:4])) if fails else \
                 " · 守护/演习(上次+预告)/令牌/拓扑/轮换全就绪"
        return npass == n and n > 0, scope
    except Exception as e:
        return False, "异常: %s" % e


def test_pipeline():
    """全链路健康预检(轻量·健康+红旗)：复用 selfcheck_pipeline 并发探各服务 /health，
    标出「换脸掉CPU(实时不可用)/核心服务离线」等红旗。延迟维度由 voicequality/bargein
    等专项覆盖，这里只做快速健康门禁(do_latency=False)，几秒内返回、可反复安全跑。"""
    try:
        import selfcheck_pipeline as sp
        res = sp.run(do_latency=False)
        up = sum(1 for r in res.get("health", []) if r.get("up"))
        tot = len(res.get("health", []))
        flags = res.get("flags", [])
        scope = "全链路健康 %d/%d 在线" % (up, tot)
        scope += (" · 红旗: " + "；".join(flags[:5])) if flags else " · 无红旗"
        return bool(res.get("ok")) and tot > 0, scope
    except Exception as e:
        return False, "异常: %s" % e


def test_embedjs():
    """回归守卫：扫描 (1) 所有 .py 内嵌 <script>(普通串+f-string) (2) static/ 下 *.html 内联 <script>
    (3) static/ 下独立 *.js，逐块 node --check（script 与 module 双模式，任一通过即合法，兼容 ESM
    import/export）。验证“浏览器实收内容”可被解析——防 _PAGE 那类『非 raw 三引号串里 \\n 被 Python
    转成真实换行 → 整段脚本语法失效』复发，及静态前端手滑语法错上线。node 缺失→SKIP(不阻断交付)；
    无法解析的 .py、外链/空/非 JS(json·template·importmap) 的 <script> 跳过。纯静态、无需服务、秒级。"""
    import ast, glob, tempfile, shutil
    node = shutil.which("node")
    if not node:
        return True, "SKIP: 未找到 node，跳过内嵌JS体检(装 Node.js 后此项自动生效)"
    tmp = tempfile.mkdtemp()
    n_py = n_html = n_js = 0
    bad = []
    seq = [0]

    def _check(where, code):
        """node --check：先 script(.js) 再 module(.mjs)，任一通过即合法(兼容 ESM)；均失败→记 bad(取脚本模式报错)。"""
        seq[0] += 1
        base = os.path.join(tmp, "b%d" % seq[0])
        err = ""
        for ext in (".js", ".mjs"):
            fn = base + ext
            with open(fn, "w", encoding="utf-8") as f:
                f.write(code)
            r = subprocess.run([node, "--check", fn], capture_output=True, text=True)
            if r.returncode == 0:
                return
            if not err:
                err = (next((l for l in (r.stderr or "").splitlines() if "Error" in l), "") or "语法错误").strip()
        bad.append("%s %s" % (where, err))

    # (1) .py 内嵌 <script>（普通串 + f-string 近似值）
    for path in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        pf = os.path.basename(path)
        if pf.startswith("_"):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        for nd in ast.walk(tree):
            val = None
            if isinstance(nd, ast.Constant) and isinstance(nd.value, str) and "<script" in nd.value:
                val = nd.value
            elif isinstance(nd, ast.JoinedStr):
                approx = "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "X"
                                 for v in nd.values)
                if "<script" in approx:
                    val = approx
            if val is None:
                continue
            for sm in re.findall(r"<script[^>]*>(.*?)</script>", val, re.S):
                if not sm.strip() or "(.*?)" in sm:   # 跳过空块 & 正则模式串(如 doctor.py)
                    continue
                n_py += 1
                _check("%s:%d" % (pf, getattr(nd, "lineno", 0)), sm)

    # (2) static/**/*.html 内联 <script>（跳过 src= 外链 / 空块 / 非 JS 类型）
    for path in sorted(glob.glob(os.path.join(HERE, "static", "**", "*.html"), recursive=True)):
        rp = os.path.relpath(path, HERE)
        try:
            html = open(path, encoding="utf-8").read()
        except Exception:
            continue
        for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I):
            attrs, body = m.group(1), m.group(2)
            if not body.strip() or re.search(r"\bsrc\s*=", attrs, re.I):
                continue
            tm = re.search(r'\btype\s*=\s*["\']?([^"\'\s>]+)', attrs, re.I)
            typ = tm.group(1).lower() if tm else ""
            if typ and typ not in ("text/javascript", "application/javascript", "module", "text/ecmascript"):
                continue   # application/json、text/template、importmap 等非 JS，跳过
            n_html += 1
            _check(rp, body)

    # (3) static/**/*.js 独立文件（含 sw.js / hub.js 等）
    for path in sorted(glob.glob(os.path.join(HERE, "static", "**", "*.js"), recursive=True)):
        rp = os.path.relpath(path, HERE)
        try:
            code = open(path, encoding="utf-8").read()
        except Exception:
            continue
        n_js += 1
        _check(rp, code)

    shutil.rmtree(tmp, ignore_errors=True)
    if bad:
        return False, "内嵌JS破损 %d 处 → %s" % (len(bad), " ｜ ".join(bad[:3]))
    return True, "内嵌JS全绿 · .py %d 块 + static.html %d 块 + static.js %d 个" % (n_py, n_html, n_js)


def test_uivr(timeout=420):
    """UI 可视化回归（06x 基线确定性达标后纳入例行）：无头 Edge 多尺寸截图 vs 像素基线。
    退出码 0=通过 / 1=超阈(真实视觉回归,FAIL 并附差异图路径) / 2=环境跳过(无 Edge/Hub 未起/
    本机无基线——SKIP 不阻断交付)。守护 UI 三批连改后的「渲染后才暴露」问题(裁切/覆盖/漂移)。
    ACCEPT_SKIP_UIVR=1 时直接 SKIP：gate --online 的 Tier U 已跑过同一回归,经 deliver_check
    嵌套调回 acceptance 时置此标记去重(省 ~52s,单独跑 acceptance 不受影响)。"""
    if os.environ.get("ACCEPT_SKIP_UIVR") == "1":
        return True, "SKIP: 门禁 Tier U 已跑同一可视化回归（嵌套去重）", 0.0, ""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    t0 = time.time()
    try:
        p = subprocess.run([PY, os.path.join(HERE, "ui_visual_regress.py"), "--base", HUB],
                           cwd=HERE, env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", errors="replace")
        dt = time.time() - t0
        if p.returncode == 2:
            tail_line = next((l.strip() for l in reversed(out.splitlines()) if l.strip()), "")
            return True, "SKIP: %s" % (tail_line[:120] or "环境不可用(无 Edge/Hub 未起/无基线)"), dt, out
        if p.returncode == 0:
            n = out.count("PASS ")
            return True, "全部通过(%d 张,机器基线 windows-edge*)" % n, dt, out
        fails = [l.strip() for l in out.splitlines() if "FAIL" in l]
        return False, "视觉回归超阈: %s（差异图 ui_snapshots/diff/）" % ("; ".join(fails[:3]) or "见输出"), dt, out
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="replace")
        return False, "超时 %ds" % timeout, time.time() - t0, out


def run_script(script, timeout, extra_env=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra_env:
        env.update(extra_env)
    t0 = time.time()
    try:
        p = subprocess.run([PY, script], cwd=HERE, env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", errors="replace")
        ok, scope = _verdict(out)
        return ok, scope, time.time() - t0, out
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="replace")
        return False, "超时 %ds" % timeout, time.time() - t0, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="追加重负载测试")
    ap.add_argument("--only", default="", help="只跑逗号分隔的 key，如 e2e,bargein")
    ap.add_argument("--alert-test", action="store_true", help="只发一条测试告警验证通道连通后退出")
    ap.add_argument("--alert-always", action="store_true", help="无论通过与否都发告警")
    args = ap.parse_args()
    only = set(s.strip() for s in args.only.split(",") if s.strip())

    if args.alert_test:
        sent = _send_alert("[体检告警·自测] acceptance 告警通道连通性测试",
                           ["这是一条测试告警，收到说明 webhook 配置正确。",
                            "time=" + time.strftime("%Y-%m-%d %H:%M:%S")])
        print("alert-test 已外发至: %s" % (sent or "(未配置任何 webhook 环境变量)"))
        sys.exit(0)

    print("=" * 64)
    print("  上线前一键体检  acceptance.py   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)
    print("[Preflight] 探活 + 关键服务")
    pf_ok, _ = preflight()
    if not pf_ok and not only:
        print("\n关键服务未就绪，建议先 start_all_services.bat 后重试（仍可用 --only 强跑单项）。")

    results = []
    for key, title, script, timeout, heavy in SUITE:
        if only:
            if key not in only:
                continue
        elif heavy and not args.full:
            continue
        print("\n" + "-" * 64)
        print("▶ [%s] %s" % (key, title))
        skipped = False
        if key == "pref":
            ok, scope = test_pref(); dt = 0.0; tail = ""
        elif key == "streaming":
            ok, scope = test_streaming(); dt = 0.0; tail = ""
        elif key == "voiceassets":
            ok, scope = test_voice_assets(); dt = 0.0; tail = ""
            if ok and scope.startswith("SKIP"):
                skipped = True          # Hub 不可达/角色库空→SKIP，不阻断交付
        elif key == "healthchain":
            ok, scope = test_healthchain(); dt = 0.0; tail = ""
        elif key == "healthdrill":
            ok, scope = test_healthdrill(); dt = 0.0; tail = ""
        elif key == "secpanorama":
            ok, scope = test_secpanorama(); dt = 0.0; tail = ""
        elif key == "pipeline":
            ok, scope = test_pipeline(); dt = 0.0; tail = ""
        elif key == "embedjs":
            ok, scope = test_embedjs(); dt = 0.0; tail = ""
            if ok and scope.startswith("SKIP"):
                skipped = True          # node 未装→按 SKIP 展示，不计入失败
        elif key == "glosssurv":
            ok, scope = test_gloss_survival(); dt = 0.0; tail = ""
            if ok and scope.startswith("SKIP"):
                skipped = True          # 同传未起/词表空→SKIP，不阻断交付
        elif key == "uivr":
            ok, scope, dt, tail = test_uivr(timeout)
            if ok and scope.startswith("SKIP"):
                skipped = True          # 无 Edge/Hub 未起/本机无基线→SKIP，不阻断交付
        else:
            # P9-1 GPU 重负载项先过争用闸：并行批次/在播换脸占卡时 SKIP 而非硬跑出假红
            gpu_skip = _gpu_wait_idle(key) if key in GPU_HEAVY else None
            if gpu_skip:
                ok, scope, dt, tail = True, gpu_skip, 0.0, ""
                skipped = True
            else:
                # voicequality 门禁走抽样（激活角色必测+名序前 N-1）：23 角色全矩阵 ≈6 分钟
                # 必超时假红；全量矩阵用 python _voice_quality.py --all 单独巡检。外部已显式
                # 设 VQ_MAX_PROFILES 时尊重外部值（0=强制全量）。
                extra = ({"VQ_MAX_PROFILES": os.environ.get("VQ_MAX_PROFILES", "8")}
                         if key == "voicequality" else None)
                ok, scope, dt, tail = run_script(script, timeout, extra_env=extra)
                # 自检工具未配置（如浏览器 E2E 缺 playwright/浏览器）→ SKIP 而非 FAIL，不阻断交付
                if not ok and _is_unprovisioned_tool(tail):
                    skipped = True; ok = True
                    scope = "SKIP: 自检工具未配置（装：pip install -r requirements/selfcheck.txt 后 playwright install chromium）"
        results.append({"key": key, "title": title, "ok": ok, "skipped": skipped,
                        "summary": scope, "sec": round(dt, 1)})
        print("  → %s  (%.1fs)  %s" % ("SKIP" if skipped else ("PASS" if ok else "FAIL"), dt, scope[:90]))

    nskip = sum(1 for r in results if r.get("skipped"))
    npass = sum(1 for r in results if r["ok"] and not r.get("skipped"))
    n = len(results)
    print("\n" + "=" * 64)
    print("  体检总表")
    print("=" * 64)
    print("  %-30s %-6s %-8s" % ("项目", "结果", "耗时"))
    print("  " + "-" * 50)
    for r in results:
        st = "SKIP" if r.get("skipped") else ("PASS" if r["ok"] else "FAIL")
        print("  %-30s %-6s %5.1fs" % (r["title"][:28], st, r["sec"]))
    overall = (sum(1 for r in results if r["ok"]) == n and n > 0)
    print("  " + "-" * 50)
    skip_note = (" （含 %d 项 SKIP，原因见上表：工具未配置/GPU 争用等）" % nskip) if nskip else ""
    print("  总计: %d/%d 通过%s  ->  %s" % (
        npass, n - nskip, skip_note, "[OK] 全部通过(可上线)" if overall else "[X] 存在失败项"))

    mode = "full" if args.full else ("only:" + args.only if only else "fast")
    rec = {"ts": time.time(), "overall": overall, "pass": npass, "total": n,
           "mode": mode, "results": results}
    # --only 部分跑不覆盖主报告：/ops 体检卡的「最近一次」必须代表完整套件——
    # 2026-07-10 实撞:only:voiceassets 1/1 全绿盖掉了 fast 11/15 的红灯,看板出现假绿。
    report_path = REPORT if not only else REPORT.replace(".json", "_partial.json")
    try:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        print("  报告: %s%s" % (report_path, "（--only 部分跑,不覆盖主报告）" if only else ""))
    except Exception as e:
        print("  报告落盘失败: %s" % e)
    # 历史留档（紧凑一行/次，滚动上限），供 /ops 看板趋势 + 留痕审计。
    # --only 部分跑不入历史/不碰告警状态：1 项全绿会把趋势稀释、还会误发「体检已恢复 1/1」
    # (2026-07-10 实撞——上一条 fast 11/15 红着,单项跑触发了假恢复通知)。
    if not only:
        try:
            compact = {"ts": rec["ts"], "overall": overall, "pass": npass, "total": n,
                       "mode": mode, "items": {r["key"]: r["ok"] for r in results}}
            lines = []
            if os.path.exists(HISTORY):
                with open(HISTORY, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            lines.append(json.dumps(compact, ensure_ascii=False))
            lines = lines[-HIST_MAX:]
            with open(HISTORY, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            print("  历史留档失败: %s" % e)

        # 失败主动外发告警：去抖（连续失败只首发 + 冷却期重发）+ 恢复通知（fail→pass 发「已恢复」）
        _alert_with_debounce(overall, npass, n, mode, results, args.alert_always)

    sys.exit(0 if overall else 1)


ALERT_STATE = os.path.join(HERE, "logs", "acceptance_alert_state.json")


def _alert_with_debounce(overall, npass, n, mode, results, alert_always_flag):
    always = alert_always_flag or os.environ.get("ACCEPT_ALERT_ALWAYS") == "1"
    cooldown = float(os.environ.get("ACCEPT_ALERT_COOLDOWN_SEC", str(6 * 3600)))
    host = os.environ.get("COMPUTERNAME", "host")
    now = time.time()
    state = {}
    try:
        if os.path.exists(ALERT_STATE):
            state = json.loads(open(ALERT_STATE, encoding="utf-8").read())
    except Exception:
        state = {}
    prev = state.get("last_status")
    last_alert_ts = float(state.get("last_alert_ts", 0) or 0)
    fail_lines = ["%s  %s" % ("PASS" if r["ok"] else "FAIL", r["title"]) for r in results]
    fails = [r["title"] for r in results if not r["ok"]]
    should, subj, lines = False, None, []
    if not overall:
        streak = int(state.get("fail_streak", 0)) + 1
        new_failure = (prev != "fail")
        cooled = (now - last_alert_ts) > cooldown
        if new_failure or cooled or always:
            should = True
            tag = "新失败" if new_failure else ("持续失败×%d" % streak)
            subj = "[体检失败·%s] %d/%d %s @ %s" % (tag, npass, n, mode, host)
            lines = fail_lines + ["", "失败项: " + ("、".join(fails) or "无")]
        new_state = {"last_status": "fail", "fail_streak": streak,
                     "last_alert_ts": (now if should else last_alert_ts)}
    else:
        if prev == "fail":
            should = True
            subj = "[体检已恢复] %d/%d %s @ %s" % (npass, n, mode, host)
            lines = ["上次失败的项目已全部恢复通过。", ""] + fail_lines
        elif always:
            should = True
            subj = "[体检通过] %d/%d %s @ %s" % (npass, n, mode, host)
            lines = fail_lines
        new_state = {"last_status": "pass", "fail_streak": 0,
                     "last_alert_ts": (now if should else last_alert_ts)}
    if should:
        try:
            sent = _send_alert(subj, lines)
            if sent:
                print("  已外发告警(%s): %s" % (subj.split("]")[0] + "]", sent))
            elif not overall:
                print("  (体检失败，但未配置告警 webhook；设 ACCEPT_WECOM_KEY/ACCEPT_SERVERCHAN/ACCEPT_WEBHOOK_URL 可推送)")
        except Exception as e:
            print("  告警外发异常: %s" % e)
    elif not overall:
        print("  (体检失败，但去抖抑制了重复告警：连续失败仅首发，冷却 %dh 后重发)" % int(cooldown / 3600))
    try:
        os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
        with open(ALERT_STATE, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False)
    except Exception:
        pass


if __name__ == "__main__":
    main()
