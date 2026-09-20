#!/usr/bin/env python3
"""GEO 引用份额体检（实施77B §四的执行工具，2026-08-28）。

**先说清楚这个工具测的是什么，不然读数会被误用。**

AI 推荐一个产品有两层来源，本工具只能自动测第一层：

  ① 训练语料层（本工具自动测）：不联网时模型「脑子里」有没有我们。
     进了训练语料 = 模型天生认识智聊，这是最深、最持久的一层，但也最慢（跟着模型版本走）。
  ② 联网检索层（必须人工问，本工具只负责出题与记分）：模型带搜索时引用了谁的网页。
     这一层跟得上我们本周发的内容，是短期能推动的那层。

自动臂经 SiliconFlow 一个 key 覆盖四家国产底座（DeepSeek / 通义 Qwen / 智谱 GLM /
月之暗面 Kimi）+ MiniMax；**豆包（字节）与元宝（腾讯）没有公开 API，ChatGPT /
Perplexity / Gemini 也没有本机凭证**——这几家走 `--emit-manual` 出题、人工问完
`--ingest` 回灌，用的是同一套记分口径，两条腿的数才可比。

记分刻意全是确定性规则（正则命中 + 首次出现位置排序），不用 LLM 当裁判：
裁判本身会漂移，而这张表要按月比较，口径必须钉死。

用法：
  python tools/geo_citation_audit.py --run                # 跑全部自动臂
  python tools/geo_citation_audit.py --run --arms deepseek,qwen
  python tools/geo_citation_audit.py --emit-manual out.md # 出人工臂问卷
  python tools/geo_citation_audit.py --ingest answers.md  # 回灌人工答案并记分
  python tools/geo_citation_audit.py --report             # 只重出报告（读历史 JSONL）

产物：logs/geo_audit/audit.jsonl（趋势单一事实源，只追加）+ 同目录 markdown 报告。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import urllib.error
import urllib.request

# Windows 控制台默认 GBK：报告与日志含中文/符号，不改编码会在 print 处直接崩
# （PS5.1 实测 U+2713 就够炸）。errors=replace 保证「显示不全」也绝不中断整轮跑批。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "logs" / "geo_audit"
JSONL = OUT_DIR / "audit.jsonl"

# 配置来源：实例 overlay 是本机唯一在跑的那份（引擎根副本是迁移遗留，可能过期）。
# 见 CLAUDE.md「CWD 相对路径＝迁移后的静默失真」。
CONFIG_CANDIDATES = [
    Path(r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml"),
    REPO / "engines" / "chengjie" / "config" / "config.local.yaml",
]

# ── 题库：买家真会拿去问 AI 的问法，**一律不带我们的品牌名** ──────────────────
# （问题里出现「智聊」就等于给自己送分，那样的读数没有任何决策价值）
QUESTIONS: list[dict[str, str]] = [
    {"id": "zh01", "lang": "zh", "q": "跨境电商卖家用什么 AI 客服工具比较好？推荐几款并说明区别"},
    {"id": "zh02", "lang": "zh", "q": "有没有能同时接 Telegram、WhatsApp、LINE、Messenger 的统一客服收件箱工具？"},
    {"id": "zh03", "lang": "zh", "q": "respond.io 有哪些替代品？中文出海团队用哪个更合适"},
    {"id": "zh04", "lang": "zh", "q": "做外贸询盘，有哪些 AI 自动回复加实时翻译一体的软件可选"},
    {"id": "zh05", "lang": "zh", "q": "Telegram 多账号统一管理和自动回复，一般用什么工具"},
    {"id": "zh06", "lang": "zh", "q": "有没有支持私有化部署、聊天数据不出自己服务器的 AI 客服系统"},
    {"id": "zh07", "lang": "zh", "q": "AI 客服工具怎么收费比较划算？有没有按用量计费、不用订阅的"},
    {"id": "zh08", "lang": "zh", "q": "有没有免费的多平台 AI 客服软件？免费版一般能用到什么程度"},
    {"id": "zh09", "lang": "zh", "q": "客服 AI 能用克隆的声音发语音消息吗？有哪些产品支持"},
    {"id": "zh10", "lang": "zh", "q": "东南亚跨境卖家常用的聊天获客与客服工具有哪些"},
    {"id": "zh11", "lang": "zh", "q": "SaleSmartly、SleekFlow 这类跨境客服 SaaS 还有什么同类产品可以比较"},
    {"id": "zh12", "lang": "zh", "q": "哪些 AI 客服软件做了欧盟 AI 法案要求的 AI 身份披露？"},
    {"id": "en01", "lang": "en", "q": "What are the best AI customer service tools for cross-border e-commerce sellers in 2026?"},
    {"id": "en02", "lang": "en", "q": "Best respond.io alternatives for a small international sales team?"},
    {"id": "en03", "lang": "en", "q": "Which tool unifies Telegram, WhatsApp, LINE and Messenger into a single inbox?"},
    {"id": "en04", "lang": "en", "q": "Is there a self-hosted AI customer support tool where chat data stays on my own server?"},
    {"id": "en05", "lang": "en", "q": "I need an AI chat tool that replies in the customer's own language automatically. Recommendations?"},
    {"id": "en06", "lang": "en", "q": "Best WhatsApp and Telegram sales automation tools with pay-as-you-go pricing (no subscription)?"},
    {"id": "en07", "lang": "en", "q": "Which AI customer service platforms can send voice replies using a cloned voice?"},
    {"id": "en08", "lang": "en", "q": "Affordable alternatives to Intercom and Zendesk for a small cross-border seller?"},
]

# 人工臂核心集（实施78 P1-6，2026-08-28）：豆包/元宝/ChatGPT/Perplexity/Gemini 没有凭证，
# 只能人去问——5 模型 × 20 题 = 100 次复制粘贴，**设计得再对，执行概率低就是零**。
# 收敛到 8 题（40 次）后一次能在半小时内做完，才可能形成月度习惯。
#
# 选题依据＝2026-08-28 基线里「竞品占位」的读数，四个维度各取中英一题：
#   ① 核心推荐问法（我们要挤进的那个默认答案）
#   ② 替代品问法（respond.io 是唯一被当来源域名引用的产品站，这是最有机会的缝）
#   ③ 多渠道统一（LINE/Zalo 是竞品的空白，我们最强的差异化）
#   ④ 计费/数据主权（老牌 SaaS 结构上做不到的那两条）
# 改这个集合＝改趋势口径，会让历史数据不可比，非必要别动。
CORE_QIDS: tuple[str, ...] = ("zh01", "zh02", "zh03", "zh06", "en01", "en02", "en03", "en06")

# ── 品牌识别 ────────────────────────────────────────────────────────────────
# case_sensitive 的用意：BOUNDLESS / ChatX 小写形态是普通英文词（"boundless
# possibilities"），不区分大小写会把散文误判成提及我们，整张表就废了。
OURS: list[tuple[str, bool]] = [
    ("智聊", False), ("ChatX", True), ("无界科技", False),
    ("BOUNDLESS", True), ("bd2026", False), ("LingoX", True),
]
COMPETITORS: list[tuple[str, bool]] = [
    ("respond.io", False), ("SaleSmartly", False), ("SleekFlow", False), ("WATI", True),
    ("Interakt", False), ("Trengo", False), ("Chatwoot", False), ("Intercom", False),
    ("Zendesk", False), ("Freshchat", False), ("Freshdesk", False), ("Tidio", False),
    ("Crisp", False), ("Zoko", False), ("Rasayel", False), ("Gallabox", False),
    ("DelightChat", False), ("LiveChat", False), ("HubSpot", False), ("ManyChat", False),
    ("Salesforce", False), ("Twilio", False), ("360Dialog", False), ("Callbell", False),
    ("网易七鱼", False), ("智齿", False), ("美洽", False), ("环信", False),
    ("Udesk", False), ("快商通", False), ("微伴", False), ("有赞", False),
]

# 域名类字符只认 ASCII：用 \w 会把中文吸进来（实测「远超respond.io」被当成一个域名）。
URL_RE = re.compile(
    r"https?://([A-Za-z0-9.-]+)|(?<![A-Za-z0-9.@])((?:[A-Za-z0-9-]+\.)+(?:com|cn|io|co|ai|net|org))\b"
)


def _find(text: str, name: str, case_sensitive: bool) -> int:
    """返回品牌名首次出现下标，未出现返回 -1。"""
    if case_sensitive:
        return text.find(name)
    return text.lower().find(name.lower())


def score(answer: str) -> dict[str, Any]:
    """确定性记分：是否提及我方 / 在所有被提及品牌中排第几 / 竞品清单 / 引用域名。

    rank 的口径 = 按「首次出现位置」给所有被提及的品牌排序后我方的名次（1 起）。
    这是 AI 答案里最接近「推荐优先级」的可机器判定信号——先说的就是先推荐的。
    """
    hits: list[tuple[int, str, bool]] = []
    for name, cs in OURS:
        pos = _find(answer, name, cs)
        if pos >= 0:
            hits.append((pos, name, True))
    for name, cs in COMPETITORS:
        pos = _find(answer, name, cs)
        if pos >= 0:
            hits.append((pos, name, False))
    hits.sort(key=lambda x: x[0])

    ordered = [h[1] for h in hits]
    ours_names = [h[1] for h in hits if h[2]]
    rank = 0
    if ours_names:
        # 我方多个别名同现（「无界科技的智聊 ChatX」）只算一个实体，取最靠前那个的名次。
        seen: list[str] = []
        for pos, name, is_ours in hits:
            entity = "US" if is_ours else name
            if entity not in seen:
                seen.append(entity)
        rank = seen.index("US") + 1

    domains = sorted({(m.group(1) or m.group(2) or "").lower().lstrip("www.") for m in URL_RE.finditer(answer)} - {""})
    return {
        "mentioned": bool(ours_names),
        "rank": rank,
        "our_aliases": ours_names,
        "competitors": [h[1] for h in hits if not h[2]],
        "brand_order": ordered[:12],
        "domains": domains[:12],
        "answer_chars": len(answer),
    }


# ── 自动臂（OpenAI 兼容端点）────────────────────────────────────────────────
def load_siliconflow_key() -> str:
    """从实例 overlay 取 SiliconFlow key（不落盘、不打印）。"""
    for cfg in CONFIG_CANDIDATES:
        if not cfg.exists():
            continue
        lines = cfg.read_text(encoding="utf-8", errors="ignore").splitlines()
        for i, line in enumerate(lines):
            if "api.siliconflow.cn" in line:
                for nxt in lines[i : i + 4]:
                    m = re.search(r"api_key:\s*(\S+)", nxt)
                    if m:
                        return m.group(1).strip().strip("'\"")
    raise SystemExit("未在实例配置里找到 SiliconFlow key（config.local.yaml::ai.api_key）")


ARMS: dict[str, dict[str, str]] = {
    "deepseek": {"model": "deepseek-ai/DeepSeek-V3.2", "label": "DeepSeek V3.2（DeepSeek 助手底座）"},
    # 2026-08-28 实测：Qwen3.5-397B-A17B 在 SiliconFlow 恒 503（未开服），
    # Qwen3.5-122B-A10B 回空 content（推理型模型走 reasoning_content）——选实际能出话的 3.6。
    "qwen": {"model": "Qwen/Qwen3.6-27B", "label": "Qwen 3.6（通义千问底座）"},
    "glm": {"model": "Pro/zai-org/GLM-5.1", "label": "GLM-5.1（智谱清言底座）"},
    "kimi": {"model": "Pro/moonshotai/Kimi-K2.6", "label": "Kimi K2.6（月之暗面底座）"},
    "minimax": {"model": "MiniMaxAI/MiniMax-M2.5", "label": "MiniMax M2.5"},
}
MANUAL_ARMS = ["豆包（字节）", "元宝（腾讯）", "ChatGPT（联网）", "Perplexity", "Gemini"]

ENDPOINT = "https://api.siliconflow.cn/v1/chat/completions"


def ask(model: str, question: str, key: str, timeout: int = 180) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": question}],
            # temperature=0：这张表要按月比较，同题同模型必须尽量可复现。
            "temperature": 0,
            "max_tokens": 900,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def run_auto(arm_keys: list[str], limit: int | None) -> list[dict[str, Any]]:
    key = load_siliconflow_key()
    qs = QUESTIONS[:limit] if limit else QUESTIONS
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jobs = [(a, q) for a in arm_keys for q in qs]
    rows: list[dict[str, Any]] = []

    def one(job: tuple[str, dict[str, str]]) -> dict[str, Any]:
        arm, q = job
        model = ARMS[arm]["model"]
        t0 = time.time()
        try:
            ans = ask(model, q["q"], key)
            err = ""
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            ans, err = "", str(e)[:200]
        rec: dict[str, Any] = {
            "run_id": run_id, "layer": "training", "arm": arm, "model": model,
            "qid": q["id"], "lang": q["lang"], "q": q["q"],
            "ms": int((time.time() - t0) * 1000), "error": err,
        }
        rec.update(score(ans) if ans else {"mentioned": False, "rank": 0, "our_aliases": [],
                                           "competitors": [], "brand_order": [], "domains": [],
                                           "answer_chars": 0})
        print(("  ok " if not err else "  ERR") + f" {arm:9s} {q['id']}  "
              + ("命中" if rec["mentioned"] else "未提及")
              + (f" rank={rec['rank']}" if rec["mentioned"] else "")
              + (f"  ERR {err}" if err else ""))
        return rec

    with ThreadPoolExecutor(max_workers=5) as pool:
        for rec in pool.map(one, jobs):
            rows.append(rec)
    return rows


# ── 人工臂：出卷 / 回灌 ──────────────────────────────────────────────────────
def emit_manual(path: Path, core: bool = True) -> None:
    qs = [q for q in QUESTIONS if q["id"] in CORE_QIDS] if core else QUESTIONS
    lines = [
        "# GEO 引用体检 · 人工臂问卷",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
        + f"　题量：{len(qs)} 题 × {len(MANUAL_ARMS)} 模型 = {len(qs) * len(MANUAL_ARMS)} 次问答"
        + ("（核心集，约 30 分钟）" if core else "（全量）"),
        "",
        "**怎么用**：下面每个问题分别去问 " + " / ".join(MANUAL_ARMS) + "，",
        "把答案原样粘贴到对应问题的 ``` 代码块里，存盘后跑：",
        "`python tools/geo_citation_audit.py --ingest <本文件>`",
        "",
        "注意：问的时候**别登录带个性化记忆的账号**（模型会记住你之前聊过智聊，读数会虚高）；",
        "尽量用无痕窗口或新会话。",
        "",
    ]
    for arm in MANUAL_ARMS:
        lines.append(f"## 模型：{arm}")
        lines.append("")
        for q in qs:
            lines.append(f"### [{q['id']}] {q['q']}")
            lines.append("")
            lines.append("```")
            lines.append("（粘贴答案）")
            lines.append("```")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"问卷已生成：{path}（{len(MANUAL_ARMS)} 模型 × {len(qs)} 问 = {len(qs) * len(MANUAL_ARMS)} 次）")
    if core:
        print("  （核心集；要全量 20 题加 --full）")


ARM_RE = re.compile(r"^## 模型：(.+?)\s*$")
Q_RE = re.compile(r"^### \[(\w+)\]")


def ingest(path: Path) -> list[dict[str, Any]]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows: list[dict[str, Any]] = []
    arm, qid, buf, inblock = "", "", [], False
    qmap = {q["id"]: q for q in QUESTIONS}

    def flush() -> None:
        nonlocal buf
        ans = "\n".join(buf).strip()
        buf = []
        if not arm or not qid or not ans or ans.startswith("（粘贴"):
            return
        q = qmap.get(qid, {"lang": "", "q": ""})
        rec: dict[str, Any] = {"run_id": run_id, "layer": "retrieval", "arm": arm, "model": arm,
                               "qid": qid, "lang": q.get("lang", ""), "q": q.get("q", ""),
                               "ms": 0, "error": ""}
        rec.update(score(ans))
        rows.append(rec)

    for line in path.read_text(encoding="utf-8").splitlines():
        m = ARM_RE.match(line)
        if m:
            flush()
            arm, qid = m.group(1), ""
            continue
        m = Q_RE.match(line)
        if m:
            flush()
            qid = m.group(1)
            continue
        if line.strip().startswith("```"):
            if inblock:
                flush()
            inblock = not inblock
            continue
        if inblock:
            buf.append(line)
    flush()
    print(f"回灌 {len(rows)} 条人工答案")
    return rows


# ── 报告 ────────────────────────────────────────────────────────────────────
def append(rows: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_all() -> list[dict[str, Any]]:
    if not JSONL.exists():
        return []
    out = []
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def report(rows: list[dict[str, Any]], only_run: str = "") -> str:
    if only_run:
        rows = [r for r in rows if r["run_id"] == only_run]
    if not rows:
        return "（无数据）"
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_arm.setdefault(r["arm"], []).append(r)

    out = ["# GEO 引用份额体检报告", "",
           f"生成：{datetime.now().strftime('%Y-%m-%d %H:%M')}　样本：{len(rows)} 条",
           "",
           "> 口径提醒：`layer=training` 是**不联网**的底座模型（测「模型脑子里有没有我们」），",
           "> `layer=retrieval` 是**联网检索**的助手（测「搜到了谁的网页」）。两层不可混着读。",
           "", "## 各模型被提及情况", "",
           "| 模型 | 层 | 提及/总数 | 提及率 | 命中时平均名次 | 失败 |",
           "|---|---|---|---|---|---|"]
    for arm, rs in sorted(by_arm.items()):
        ok = [r for r in rs if not r["error"]]
        hit = [r for r in ok if r["mentioned"]]
        ranks = [r["rank"] for r in hit if r["rank"]]
        avg = f"{sum(ranks)/len(ranks):.1f}" if ranks else "—"
        pct = f"{len(hit)/len(ok)*100:.0f}%" if ok else "—"
        label = ARMS.get(arm, {}).get("label", arm)
        out.append(f"| {label} | {rs[0]['layer']} | {len(hit)}/{len(ok)} | {pct} | {avg} | {len(rs)-len(ok)} |")

    freq: dict[str, int] = {}
    for r in rows:
        for c in r.get("competitors", []):
            freq[c] = freq.get(c, 0) + 1
    out += ["", "## 竞品被提及次数（谁占着我们想要的位置）", ""]
    if freq:
        out.append("| 竞品 | 被提及次数 |")
        out.append("|---|---|")
        for name, n in sorted(freq.items(), key=lambda kv: -kv[1])[:15]:
            out.append(f"| {name} | {n} |")
    else:
        out.append("（本轮无竞品被提及）")

    dom: dict[str, int] = {}
    for r in rows:
        for d in r.get("domains", []):
            dom[d] = dom.get(d, 0) + 1
    out += ["", "## 被引用的来源域名（决定下月产能往哪投）", ""]
    if dom:
        out.append("| 域名 | 次数 |")
        out.append("|---|---|")
        for d, n in sorted(dom.items(), key=lambda kv: -kv[1])[:15]:
            out.append(f"| {d} | {n} |")
    else:
        out.append("（本轮答案里没有可提取的来源链接——底座模型不联网时通常如此，属正常）")

    miss = [r["qid"] for r in rows if not r["mentioned"] and not r["error"]]
    out += ["", "## 一个都没提到我们的问题（内容缺口）", "",
            "、".join(sorted(set(miss))) or "（无）", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="GEO 引用份额体检")
    ap.add_argument("--run", action="store_true", help="跑自动臂（底座模型）")
    ap.add_argument("--arms", default=",".join(ARMS), help="逗号分隔：" + ",".join(ARMS))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟用）")
    ap.add_argument("--emit-manual", metavar="FILE", help="生成人工臂问卷（默认核心 8 题）")
    ap.add_argument("--full", action="store_true", help="人工臂问卷出全部 20 题（默认只出核心集）")
    ap.add_argument("--ingest", metavar="FILE", help="回灌人工臂答案")
    ap.add_argument("--report", action="store_true", help="只出报告（读历史 JSONL）")
    ap.add_argument("--out", metavar="FILE", help="报告落盘路径")
    a = ap.parse_args()

    rows: list[dict[str, Any]] = []
    run_id = ""
    if a.emit_manual:
        emit_manual(Path(a.emit_manual), core=not a.full)
        return 0
    if a.run:
        arms = [x.strip() for x in a.arms.split(",") if x.strip() in ARMS]
        if not arms:
            print("没有有效的自动臂", file=sys.stderr)
            return 2
        print(f"自动臂 {len(arms)} 个 × {a.limit or len(QUESTIONS)} 题 …")
        rows = run_auto(arms, a.limit or None)
        run_id = rows[0]["run_id"] if rows else ""
        append(rows)
    if a.ingest:
        got = ingest(Path(a.ingest))
        append(got)
        rows += got
        run_id = got[0]["run_id"] if got and not run_id else run_id
    if not rows and not a.report:
        print("什么都没做：加 --run / --ingest / --emit-manual / --report", file=sys.stderr)
        return 2

    text = report(load_all() if a.report else rows, "" if a.report else run_id)
    print("\n" + text)
    dest = Path(a.out) if a.out else OUT_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    print(f"\n报告已落盘：{dest}")
    print(f"趋势数据：{JSONL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
