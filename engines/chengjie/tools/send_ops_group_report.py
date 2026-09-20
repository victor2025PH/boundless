"""往 Telegram 运维群发「运维通报」与「AI 花费日报」（2026-09-09）。

数据全部现读实例 API（/api/cost/summary、/api/workspace/metrics），排版走 Telegram HTML：
分隔线 + 等宽表格（<pre>）+ 一张 matplotlib 生成的日报卡图（大数字 + 去向 + 近 14 天柱状）。
渠道用 notify_webhooks.json 里名为 --channel 的那条（缺省 tg-ywqz 运维群组）。

用法：
  python tools/send_ops_group_report.py                 # 运维通报 + 成本日报（带图）
  python tools/send_ops_group_report.py --only cost     # 只发成本日报
  python tools/send_ops_group_report.py --only ops      # 只发运维通报
  python tools/send_ops_group_report.py --dry-run       # 只打印不发送
  python tools/send_ops_group_report.py --notes 本次上线.txt   # 「本次上线」要点（一行一条）

运维通报是**数据驱动**的（2026-09-10 运维群降噪 P1.2）：此前「本次上线」六条要点写死在
代码里，每次发都是 09-09 那几句——群里看起来像复制粘贴。现在：
  - 「本次上线」只在给了 --notes（或 .ops\\ops_release_notes.txt 存在）时出现，内容来自文件；
  - 「仍未处理」来自 health_watchdog 的提醒账本 health_remind_state.json（各巡检外发时登记的
    一行人话 + 开了多久），与运维群实时卡 / 每日摘要同一数据面；
  - 「需要管理员做」按成本摘要**推导**（没填余额才提填余额、没账单真值才提导 CSV）；
  - 成本接口打不开（404 = 成本页路由未装载）不再让整个脚本崩：通报照发并写明，成本日报跳过。
"""
from __future__ import annotations

import argparse
import html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

CFG = Path(r"D:\chengjie-instances\zhiliao\data\config")
BASE = "http://127.0.0.1:18799"
SEP = "━━━━━━━━━━━━━━━━━━━━"
RELEASE_NOTES_DEFAULT = Path(r"D:\chengjie-instances\.ops\ops_release_notes.txt")
# 提醒账本键 → 通报里的人话标签（与 health_watchdog._DIGEST_LABELS 同口径）
_OPEN_LABELS = (("draft_backlog", "待审草稿"), ("case_backlog", "案例跟进"),
                ("unanswered_inbound", "客户在等"), ("avatar_voice", "语音服务"),
                ("lan_gpu:", "LAN GPU"))


def public_base() -> str:
    """消息里的链接必须是**公网**地址（手机在外网打不开 192.168.x）。来源：
    .ops\\prod_public_url.txt（隧道脚本维护）；缺失回落局域网地址并在消息里注明。"""
    try:
        u = Path(r"D:\chengjie-instances\.ops\prod_public_url.txt").read_text(encoding="utf-8").strip()
        if u.startswith("http"):
            return u.rstrip("/")
    except Exception:
        pass
    return "http://192.168.0.149:18799"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _token() -> str:
    for name in ("config.local.yaml", "config.yaml"):
        m = re.search(r"auth_token:\s*(\S+)", (CFG / name).read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise SystemExit("找不到 web_admin auth_token")


def _api(path: str, tok: str) -> Dict[str, Any]:
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": f"Bearer {tok}", "X-API-Token": tok})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def _cost_summary(tok: str) -> Dict[str, Any]:
    """成本摘要；接口打不开时返回 ``{"available": False, "error": <原因>}`` 而不是抛——
    09-09 17:58 起成本路由注册丢失、/api/cost/summary 404，此前这里一崩连运维通报也发不出。"""
    try:
        s = _api("/api/cost/summary?days=14", tok)
    except urllib.error.HTTPError as e:
        why = ("成本页路由未装载（404）——实例需重启以挂上 /workspace/cost" if e.code == 404
               else f"HTTP {e.code}")
        return {"available": False, "error": why}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}
    if not isinstance(s, dict):
        return {"available": False, "error": "非 JSON 响应"}
    if not s.get("available"):
        s.setdefault("error", "成本账本不可用（价格表未配置或账本未落盘）")
    return s


def _primary_status(tok: str) -> Dict[str, Any]:
    """主链**当前路由**（不是账本厂商）。首选单一口径 ``/api/setup/ai-primary/summary``
    （档位/锁/主链一句话/降级链，2026-09-17 起所有出口共用）；老引擎没有该接口时回落
    ``/api/setup/cloud-credentials`` 的 primary 段。成本摘要的 provider 永远是云端计费
    厂商，主链切 local 后通报曾写「主链厂商 deepseek」——运维群据此以为主链还在云端。
    两个接口都打不开返回空 dict，通报退回只报计费厂商。"""
    try:
        s = _api("/api/setup/ai-primary/summary", tok)
        if isinstance(s, dict) and s.get("ok") and s.get("primary_text"):
            return s
    except Exception:  # noqa: BLE001
        pass
    try:
        cc = _api("/api/setup/cloud-credentials", tok)
        p = cc.get("primary") if isinstance(cc, dict) else None
        return p if isinstance(p, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def primary_line(primary: Dict[str, Any], summary: Dict[str, Any]) -> str:
    """通报「主链」一行（纯函数）。优先直接用单一口径的 ``primary_text``/``chain_text``；
    老形状（cloud-credentials.primary）本地档写本地端点/模型并把云端厂商降为「回落/计费」；
    云档写云端厂商；拿不到路由信息只写计费厂商（并标明口径）。"""
    if primary.get("primary_text"):
        eff = str(primary.get("effective") or "").lower()
        ready = (primary.get("local") or {}).get("ready") if isinstance(primary.get("local"), dict) else None
        if "local_ready" in primary:
            ready = primary.get("local_ready")
        warn = "" if (ready is None or eff == "cloud") else ("" if ready else " ⚠️ 本地端点不可达")
        chain = str(primary.get("chain_text") or "").strip()
        return (f"• 主链：{html.escape(str(primary['primary_text']))}{warn}"
                + (f" · 降级链：{html.escape(chain)}" if chain else ""))
    prov = html.escape(str(summary.get("provider") or "—"))
    eff = str(primary.get("effective") or "").strip().lower()
    lock = str(primary.get("lock") or "").strip().lower()
    lock_txt = f"锁 {lock}" if lock else "未设锁"
    if eff in ("local", "local_only"):
        model = html.escape(str(primary.get("local_model") or "本地模型"))
        ready = primary.get("local_ready")
        ready_txt = "" if ready is None else ("，端点可达" if ready else "，⚠️ 端点不可达")
        tail = ("不回落云端（隐私档）" if eff == "local_only" else f"云端回落/计费厂商 {prov}")
        return f"• 主链：本地 vLLM {model}（档位 {eff}，{lock_txt}{ready_txt}）· {tail}"
    if eff == "cloud":
        return f"• 主链：云端 {prov}（档位 cloud，{lock_txt}）"
    return f"• 主链：计费厂商 {prov}（路由档位未取到，按账本口径）"


def load_release_notes(path: Optional[Path]) -> List[str]:
    """「本次上线」要点：一行一条，# 开头为注释；文件不存在 → 空（该段整段不出现）。"""
    p = path or RELEASE_NOTES_DEFAULT
    try:
        raw = Path(p).read_text(encoding="utf-8")
    except Exception:
        return []
    out: List[str] = []
    for ln in raw.splitlines():
        s = ln.strip().lstrip("•-· ").strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out[:10]


_DURATION_MARK = re.compile(r"(最久|最老|已等|已 ?\d|\d+(\.\d+)? ?(分钟|小时|天)\b)")


def _hours_txt(hours: float) -> str:
    h = max(0.0, float(hours or 0.0))
    if h < 1.0:
        return f"{int(round(h * 60))} 分钟"
    if h < 48.0:
        return f"{h:.0f} 小时" if h >= 10 else f"{h:.1f} 小时"
    d = h / 24.0
    return f"{d:.1f} 天" if d < 10 else f"{d:.0f} 天"


def load_open_items(state_path: Optional[Path] = None, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """仍在告警中的巡检项（提醒账本 alerted=True），每项 {label, summary, hours}。
    直接读 JSON 不依赖引擎包（脚本可能在没装 src 的机器上跑）。"""
    p = state_path or (CFG / "health_remind_state.json")
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return []
    ts = float(now if now is not None else time.time())
    items: List[Dict[str, Any]] = []
    for key, st in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(st, dict) or not st.get("alerted") or str(key).startswith("_"):
            continue
        label = next((lbl for pre, lbl in _OPEN_LABELS if str(key).startswith(pre)), str(key))
        fs = float(st.get("first_seen") or 0.0)
        items.append({"label": label,
                      "summary": str((st.get("meta") or {}).get("summary") or "").strip(),
                      "hours": (ts - fs) / 3600.0 if fs > 0 else 0.0,
                      "first_seen": fs})
    items.sort(key=lambda x: (x["first_seen"] or float("inf"), x["label"]))
    return items


def _channel(name: str) -> Dict[str, Any]:
    for w in json.load(open(CFG / "notify_webhooks.json", encoding="utf-8")):
        if w.get("name") == name:
            return w
    raise SystemExit(f"notify_webhooks.json 里没有渠道 {name!r}")


def _tg(method: str, bot: str, payload: Dict[str, Any], *, photo: Optional[bytes] = None) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot}/{method}"
    if photo is None:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    else:
        boundary = "----tg" + uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in payload.items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8"))
        body.write((f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                    f"filename=\"report.png\"\r\nContent-Type: image/png\r\n\r\n").encode("utf-8"))
        body.write(photo)
        body.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(url, data=body.getvalue(), headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _f(v: Any, nd: int = 2) -> str:
    """金额：常规两位小数；0 < v < 0.01 的零头（探针级消耗）给四位，别显示成 0.00。"""
    if v is None:
        return "—"
    x = float(v)
    if 0 < abs(x) < 0.01:
        return f"{x:.4f}"
    return f"{x:.{nd}f}"


def _recon_view(lr: Dict[str, Any]) -> Dict[str, str]:
    """对账结论的展示口径（与 cost_recon.no_data 同规则，兼容尚未装载该判定的老记录）。"""
    if not lr:
        return {"verdict": "none", "light": "⚪", "text": "尚未对账"}
    truth = lr.get("truth")
    internal = float(lr.get("internal") or 0)
    v = str(lr.get("verdict") or "")
    if truth is None:
        return {"verdict": "no_truth", "light": "⚪", "text": "⚪ 无账单真值（请导入费用明细 CSV 或手填当日金额）"}
    if v == "no_data" or (internal <= 0 and float(truth) > 0):
        return {"verdict": "no_data", "light": "⚪",
                "text": f"⚪ 账本无当日记录，账单 ¥{_f(truth)}（账本装载前的消耗，没得比）"}
    dp = abs(float(lr.get("diff_pct") or 0))
    if v == "ok":
        return {"verdict": "ok", "light": lr.get("light") or "✅",
                "text": f"✅ 一致（内部 ¥{_f(internal)} vs 账单 ¥{_f(truth)}，差 {dp:.0f}%）"}
    return {"verdict": "mismatch", "light": "❌",
            "text": f"❌ 不一致（内部 ¥{_f(internal)} vs 账单 ¥{_f(truth)}，差 {dp:.0f}%）"}


# ── 运维通报 ────────────────────────────────────────────────────────────────

def probe_status_text(state_path: Optional[Path] = None) -> str:
    """真活探针一行：直接读状态文件（/api/workspace/metrics 需主管会话，API token 打不开）。"""
    try:
        st = json.load(open(state_path or (CFG / "true_probe_state.json"), encoding="utf-8"))
        doms = st.get("domains") or {}
        if doms:
            ok = [d for d, v in doms.items() if v.get("ok")]
            bad = [d for d, v in doms.items() if not v.get("ok")]
            return (f"{len(ok)}/{len(doms)} 域正常（{st.get('updated_at', '')[-8:]}）"
                    + (f"，异常：{'、'.join(bad)}" if bad else ""))
    except Exception:
        pass
    return "—"


def build_ops_message(tok: str, summary: Dict[str, Any], *, notes: Optional[List[str]] = None,
                      open_items: Optional[List[Dict[str, Any]]] = None,
                      probe_txt: Optional[str] = None, now: Optional[float] = None,
                      primary: Optional[Dict[str, Any]] = None) -> str:
    """运维通报：本次上线（有才出现）→ 当前状态 → 仍未处理 → 需要管理员做（按数据推导）。

    ``primary``＝主链当前路由（见 ``_primary_status``）；None 时现读接口。"""
    ts = float(now if now is not None else time.time())
    now_txt = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    probe_txt = probe_status_text() if probe_txt is None else probe_txt
    notes = load_release_notes(None) if notes is None else notes
    open_items = load_open_items(now=ts) if open_items is None else open_items
    primary = _primary_status(tok) if primary is None else primary
    cost_ok = bool(summary.get("available"))
    budget = summary.get("budget") or {}
    lines = [
        "🛠 <b>运维通报 · 智聊生产（.117）</b>",
        f"<i>{now_txt}</i>",
        SEP,
    ]
    if notes:
        lines.append("<b>本次上线</b>")
        lines += [f"• {html.escape(n)}" for n in notes]
        lines.append(SEP)
    lines.append("<b>当前状态</b>")
    lines.append(f"• 真活探针：{html.escape(probe_txt)}")
    if cost_ok:
        lines.append(primary_line(primary, summary))
        lines.append(
            f"• 云端价格表：{'已配置' if summary.get('pricing_configured') else '未配置'} · "
            f"成本账本：今日 {(summary.get('today') or {}).get('calls', 0)} 次云端调用已入账"
            + (f"，日预算 ¥{_f(budget.get('daily'))}" if budget.get("daily") else ""))
    else:
        if primary:
            lines.append(primary_line(primary, summary))
        lines.append(f"• 成本账本：⚠️ {html.escape(str(summary.get('error') or '不可用'))}")
    lines.append(SEP)
    if open_items:
        lines.append(f"<b>仍未处理</b>（{len(open_items)} 项）")
        for it in open_items[:6]:
            s = str(it.get("summary") or "详见运营总览")
            lbl = str(it["label"])
            head = s if s.startswith(lbl) else f"{lbl}：{s}"
            # 摘要自带时长（「最久 30 天」）时不追加「已开」——账本首见 ≠ 积压开始
            dur = "" if _DURATION_MARK.search(s) else f"（已开 {_hours_txt(it.get('hours') or 0.0)}）"
            lines.append(f"• {html.escape(head)}{dur}")
        if len(open_items) > 6:
            lines.append(f"• …另 {len(open_items) - 6} 项见运营总览")
    else:
        lines.append("<b>仍未处理</b>　无 ✅")
    lines.append(SEP)
    todo: List[str] = []
    if cost_ok:
        if summary.get("balance") is None:
            todo.append("成本页填一次厂商余额（右侧「当前余额」），才能算「还能用几天」")
        lr = summary.get("last_recon") or {}
        if not lr or lr.get("truth") is None:
            todo.append("导一次厂商「费用明细」CSV（拖进成本页）——对账的真值来源")
    else:
        todo.append("成本页打不开：按重启纪律重启实例后再发成本日报")
    for it in open_items[:3]:
        todo.append(f"处理「{it['label']}」：{it.get('summary') or ''}".rstrip("："))
    if todo:
        lines.append("<b>需要管理员做</b>")
        lines += [f"{i}. {html.escape(t)}" for i, t in enumerate(todo[:5], 1)]
    else:
        lines.append("<b>需要管理员做</b>　无")
    lines.append("")
    lines.append(
        f"📍 <a href=\"{public_base()}/admin/ops\">运营总览</a>　·　"
        f"<a href=\"{public_base()}/workspace/cost\">成本页</a>　·　"
        f"<a href=\"{public_base()}/workspace/boss\">老板日报</a>")
    lines.append("<i>（公网地址，需登录工作台账号）</i>")
    return "\n".join(lines)


# ── 成本日报 ────────────────────────────────────────────────────────────────

def build_cost_message(summary: Dict[str, Any]) -> str:
    t = summary.get("today") or {}
    y = summary.get("yesterday") or {}
    lr = summary.get("last_recon") or {}
    budget = summary.get("budget") or {}
    cur = "¥"
    rv = _recon_view(lr)
    light, recon = rv["light"], rv["text"]
    bal = summary.get("balance")
    rw = summary.get("runway_days")
    if bal is None:
        bal_txt = "未知 —— 请在成本页填一次余额"
    else:
        basis = "人工填写" if summary.get("balance_basis") == "manual" else "按充值流水推算"
        bal_txt = f"约 {cur}{_f(bal)}（{basis}）" + (f"，按近 7 日均燃可用 <b>{rw}</b> 天" if rw is not None else "，均燃尚无数据")

    # 去向表（等宽）
    rows: List[str] = []
    purposes = t.get("purposes") or []
    total = sum(float(p["cost"]) for p in purposes) or 1.0
    for p in purposes[:6]:
        share = float(p["cost"]) / total * 100
        rows.append(f"{p['label']:<6}{cur}{float(p['cost']):>8.4f} {int(p['calls']):>5}次 {share:>4.0f}%")
    table = "\n".join(rows) if rows else "（今天还没有云端调用）"

    dbud = float(budget.get("daily") or 0)
    used_pct = f"{float(t.get('cost') or 0) / dbud * 100:.0f}%" if dbud > 0 else "—"
    day = t.get("day", "")
    lines = [
        f"💴 <b>AI 花费日报 · {day}</b>",
        f"<i>云端计费厂商：{html.escape(str(summary.get('provider') or ''))} · 币种 CNY · 内部按 token 估算"
        "（本地 vLLM 调用不计费、不在此表）</i>",
        SEP,
        f"<b>今天已花</b>　{cur}{_f(t.get('cost'))}　（{t.get('calls', 0)} 次调用，日预算 {cur}{_f(dbud)} 已用 {used_pct}）",
        f"<b>昨天</b>　　　{cur}{_f(y.get('cost'))}" + (f"　账单 {cur}{_f(y.get('truth'))}" if y.get("truth") is not None else ""),
        f"<b>本月累计</b>　{cur}{_f(summary.get('month_total'))}　（近 7 日均 {cur}{_f(summary.get('avg7'))}/天）",
        SEP,
        "<b>钱花在哪（今天）</b>",
        f"<pre>{html.escape(table)}</pre>",
        SEP,
        f"<b>对账（{lr.get('day', '—')}）</b>　{light}",
        html.escape(recon),
        f"<b>余额</b>　{bal_txt}",
        SEP,
    ]
    issues = [r["message"] for r in (lr.get("reasons") or []) if r.get("level") in ("warn", "crit")]
    if issues:
        lines.append("<b>要处理</b>")
        lines += [f"• {html.escape(x)}" for x in issues[:4]]
    else:
        lines.append("<b>要处理</b>　无")
    lines.append("")
    lines.append(f"🕘 下次自动对账 {budget.get('recon_at', '09:40')}　·　"
                 f"📍 <a href=\"{public_base()}/workspace/cost\">打开成本页</a>（导账单 / 填余额）")
    return "\n".join(lines)


def render_cost_card(summary: Dict[str, Any]) -> bytes:
    """日报卡图：大数字 × 4 + 用途去向条 + 近 14 天内部/账单双柱。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for cand in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC"):
        if any(f.name == cand for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = cand
            break
    plt.rcParams["axes.unicode_minus"] = False

    t = summary.get("today") or {}
    lr = summary.get("last_recon") or {}
    series = summary.get("series") or []
    truth = {r["day"]: r["amount"] for r in (summary.get("truth_rows") or [])}

    fig = plt.figure(figsize=(9, 6.2), dpi=160, facecolor="#0f172a")
    gs = fig.add_gridspec(3, 4, height_ratios=[1.1, 1.3, 1.9], hspace=0.55, wspace=0.35,
                          left=0.06, right=0.97, top=0.9, bottom=0.1)
    fig.text(0.06, 0.95, f"AI 花费日报 · {t.get('day', '')} · 云端计费 {summary.get('provider', '')}",
             color="#e2e8f0", fontsize=15, fontweight="bold", va="center")

    def kpi(col: int, big: str, small: str, color: str = "#38bdf8") -> None:
        ax = fig.add_subplot(gs[0, col])
        ax.set_axis_off()
        ax.text(0, 0.62, big, color=color, fontsize=22, fontweight="bold", va="center")
        ax.text(0, 0.12, small, color="#94a3b8", fontsize=9.5, va="center")

    rv = _recon_view(lr)
    kpi(0, f"¥{_f(t.get('cost'))}", "今天已花（内部估算）")
    kpi(1, f"¥{_f(summary.get('month_total'))}", "本月累计")
    verdict = {"ok": "一致", "mismatch": "不一致", "no_truth": "无真值", "no_data": "无记录",
               "none": "尚未对账"}[rv["verdict"]]
    # 图里不放 emoji：中文字体没有 ⚪/✅ 字形，会渲染成方框（0909 首发实锤）
    kpi(2, verdict, "昨日对账",
        color={"一致": "#34d399", "不一致": "#f87171"}.get(verdict, "#fbbf24"))
    rw = summary.get("runway_days")
    kpi(3, (f"{rw} 天" if rw is not None else "—"),
        ("余额约 ¥" + _f(summary.get("balance")) if summary.get("balance") is not None else "余额未知"))

    # 去向条
    ax2 = fig.add_subplot(gs[1, :])
    ax2.set_facecolor("#0f172a")
    purposes = (t.get("purposes") or [])[:6]
    labels = [p["label"] for p in purposes][::-1] or ["无消耗"]
    vals = [float(p["cost"]) for p in purposes][::-1] or [0]
    waste = {"夜间演练", "记忆抽取", "评测", "知识库辅助", "人设精修", "短判/工具", "未标注"}
    colors = ["#f59e0b" if lbl in waste else "#38bdf8" for lbl in labels]
    ax2.barh(labels, vals, color=colors, height=0.55)
    for i, v in enumerate(vals):
        ax2.text(v, i, f"  ¥{v:.4f}", color="#e2e8f0", va="center", fontsize=9)
    ax2.set_title("今天的钱花在哪（橙＝不直接服务客户）", color="#cbd5e1", fontsize=10, loc="left")
    ax2.tick_params(colors="#cbd5e1", labelsize=9)
    for s in ax2.spines.values():
        s.set_visible(False)
    ax2.set_xticks([])
    ax2.set_xlim(0, max(vals + [0.001]) * 1.35)

    # 14 天双柱
    ax3 = fig.add_subplot(gs[2, :])
    ax3.set_facecolor("#0f172a")
    days = [r["day"][5:] for r in series]
    internal = [float(r["cost"]) for r in series]
    tv = [truth.get(r["day"]) for r in series]
    x = list(range(len(days)))
    ax3.bar([i - 0.2 for i in x], internal, width=0.4, color="#38bdf8", label="内部估算")
    ax3.bar([i + 0.2 for i in x], [v or 0 for v in tv], width=0.4, color="#34d399", label="账单真值")
    ax3.set_xticks(x)
    ax3.set_xticklabels(days, rotation=0, fontsize=8, color="#cbd5e1")
    ax3.tick_params(axis="y", colors="#cbd5e1", labelsize=8)
    ax3.set_title("近 14 天每日花费（¥）", color="#cbd5e1", fontsize=10, loc="left")
    ax3.legend(loc="upper left", fontsize=8, frameon=False, labelcolor="#e2e8f0")
    for s in ax3.spines.values():
        s.set_color("#334155")
    ax3.grid(axis="y", color="#1e293b", linewidth=0.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--channel", default="tg-ywqz")
    ap.add_argument("--only", choices=("ops", "cost", "all"), default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--edit-ops", type=int, default=0, help="原地改写已发的运维通报（message_id）")
    ap.add_argument("--edit-cost", type=int, default=0, help="原地改写已发的成本日报图说明（message_id）")
    ap.add_argument("--notes", type=Path, default=None,
                    help=f"「本次上线」要点文件（一行一条；缺省读 {RELEASE_NOTES_DEFAULT}，没有则该段不出现）")
    a = ap.parse_args()
    tok = _token()
    ch = _channel(a.channel)
    bot, target = str(ch["token"]), str(ch["target"])
    summary = _cost_summary(tok)
    cost_ok = bool(summary.get("available"))
    if not cost_ok:
        print(f"[warn] 成本摘要不可用：{summary.get('error')}"
              + ("" if a.only == "ops" else " —— 成本日报跳过，只发运维通报"))
        if a.only == "cost":
            return 1
    notes = load_release_notes(a.notes)
    sent = []
    if a.edit_ops or a.edit_cost:
        if a.edit_ops:
            r = _tg("editMessageText", bot, {"chat_id": target, "message_id": a.edit_ops,
                                              "text": build_ops_message(tok, summary, notes=notes),
                                              "parse_mode": "HTML", "disable_web_page_preview": True})
            sent.append(("edit-ops", r.get("ok"), a.edit_ops, r.get("description")))
        if a.edit_cost and cost_ok:
            r = _tg("editMessageCaption", bot, {"chat_id": target, "message_id": a.edit_cost,
                                                 "caption": build_cost_message(summary),
                                                 "parse_mode": "HTML"})
            sent.append(("edit-cost", r.get("ok"), a.edit_cost, r.get("description")))
        for s in sent:
            print("edited:", s)
        return 0 if all(s[1] for s in sent) else 1
    if a.only in ("ops", "all"):
        msg = build_ops_message(tok, summary, notes=notes)
        print(msg, "\n")
        if not a.dry_run:
            r = _tg("sendMessage", bot, {"chat_id": target, "text": msg, "parse_mode": "HTML",
                                          "disable_web_page_preview": True})
            sent.append(("ops", r.get("ok"), (r.get("result") or {}).get("message_id"), r.get("description")))
    if a.only in ("cost", "all") and cost_ok:
        msg = build_cost_message(summary)
        print(msg, "\n")
        if not a.dry_run:
            png = render_cost_card(summary)
            r = _tg("sendPhoto", bot, {"chat_id": target, "caption": msg, "parse_mode": "HTML"}, photo=png)
            if not r.get("ok"):
                # caption 超 1024 字或其它失败 → 图与文分开发
                r1 = _tg("sendPhoto", bot, {"chat_id": target}, photo=png)
                r = _tg("sendMessage", bot, {"chat_id": target, "text": msg, "parse_mode": "HTML",
                                              "disable_web_page_preview": True})
                sent.append(("cost-photo", r1.get("ok"), None, r1.get("description")))
            sent.append(("cost", r.get("ok"), (r.get("result") or {}).get("message_id"), r.get("description")))
    for s in sent:
        print("sent:", s)
    return 0 if all(s[1] for s in sent) else 1


if __name__ == "__main__":
    raise SystemExit(main())
