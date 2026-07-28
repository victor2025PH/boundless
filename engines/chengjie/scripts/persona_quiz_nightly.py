# -*- coding: utf-8 -*-
"""人设一致性考题「夜间自动回归 + 分数下滑告警」（J2 线，2026-07-28）。

考题闭环（``persona_quiz`` 出题/实测/判分 + ``persona_quiz_store`` 存档 + 路由）已经
能用，缺的是**主动性**：现在必须有人手点「跑一次质检」。人设档案/长传记/prompt 任何
一处回归导致 AI 记不住自己是谁，没人会知道。本 CLI 把那一下手点变成夜间批量：

  逐人设 build_quiz → run_quiz（真人设 prompt 问 LLM）→ score_answers → save_report
  → 与该人设**历史均分**比 delta → 不合格/下滑就走 ``host_alert`` 轰人。

选人判据（``select_personas``）＝ **``build_quiz`` 出得来题**（内部门槛
``MIN_QUIZ_ITEMS``＝3，「资料太薄不值得考」是它自己的语义，这里不另造一套）。
刻意**不**以「有长传记」选人：``build_quiz`` 与 ``build_quiz_system_prompt`` 都只读
persona dict（specific_memories / background / role / age / tastes），长传记
（``persona_bio_store``）既不进考题也不进考卷 prompt——按 bio 选人会选出一堆出不了
题的人设，出 0 题的空卷判分恒 0 分，反而制造假告警。

判定全在纯函数里（``evaluate_run`` / ``build_alert_message`` / ``select_personas`` /
``wrong_items`` / ``history_from_overview``），主流程只做编排——门禁
``tests/test_persona_quiz_nightly.py`` 零真 LLM 覆盖判定与三态退出码。

用法（引擎根目录）::

    python -m scripts.persona_quiz_nightly --dry-run          # 只选人出题，不调 LLM
    python -m scripts.persona_quiz_nightly                    # 夜间全量（真调 LLM）
    python -m scripts.persona_quiz_nightly --personas lin_jiaxin,su_wan --limit 6
    python -m scripts.persona_quiz_nightly --json             # 机器可读

退出码：全合格 0（含「未启用」「无可考人设」）/ 有不合格或下滑 1 / 执行异常 2。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows GBK 控制台兜底：✓/✗ 等符号不再炸 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

from src.utils import persona_quiz as pq  # noqa: E402

# ── 缺省值（优先级：命令行 > config personas.quiz.nightly > 这里）────────────
DEFAULT_MIN_SCORE = 0.7        # 低于此分（0..1）记「不合格」
DEFAULT_DROP_ALERT = 0.15      # 较历史均分下滑超过此幅度（0..1）记「下滑」
DEFAULT_LIMIT = 10             # 每人设题量上限（沿用 build_quiz 默认）
DEFAULT_SLEEP_SEC = 3.0        # 人设之间的间隔：串行逐题打 LLM，别打爆
WRONG_ANSWER_CHARS = 120       # 错题里 AI 答案的截断长度
ALERT_KEY = "persona_quiz:nightly"
ALERT_COOLDOWN_SEC = 21600.0   # 6h：一天一跑，手工重跑也不刷屏


def _log(msg: str) -> None:
    """进度/诊断走 stderr —— stdout 只留最终报告，``--json`` 才是纯 JSON。

    PS 包装脚本用 ``2>&1`` 收全量，日志内容不受影响。
    """
    print(msg, file=sys.stderr)


# ── 纯函数：设置解析 ─────────────────────────────────────────────────────────

def _as_fraction(value: Any, fallback: float) -> float:
    """归一到 0..1 分数；>1 视作百分数（``70`` → ``0.7``）；非法/负数回落 fallback。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    if v < 0:
        return fallback
    return v / 100.0 if v > 1.0 else v


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def parse_persona_list(raw: Any) -> List[str]:
    """``"a,b, c"`` → ``["a","b","c"]``（全角逗号同样接受；去重保序）。"""
    out: List[str] = []
    for part in str(raw or "").replace("，", ",").split(","):
        pid = part.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def nightly_config(config: Optional[dict]) -> Dict[str, Any]:
    """取 ``personas.quiz.nightly`` 段（缺失 → 空 dict，全部走缺省）。"""
    quiz = ((config or {}).get("personas") or {}).get("quiz") or {}
    node = quiz.get("nightly") if isinstance(quiz, dict) else None
    return node if isinstance(node, dict) else {}


def resolve_settings(config: Optional[dict], args: Any) -> Dict[str, Any]:
    """配置作默认值、命令行参数优先，合出本次运行的设置。"""
    node = nightly_config(config)
    get = getattr

    min_score = get(args, "min_score", None)
    drop_alert = get(args, "drop_alert", None)
    limit = get(args, "limit", None)
    sleep_sec = get(args, "sleep", None)

    alert = bool(node.get("alert", True))
    if get(args, "no_alert", False):
        alert = False

    return {
        "enabled": bool(node.get("enabled", False)),
        "min_score": _as_fraction(
            min_score if min_score is not None else node.get("min_score"),
            DEFAULT_MIN_SCORE),
        "drop_alert": _as_fraction(
            drop_alert if drop_alert is not None else node.get("drop_alert"),
            DEFAULT_DROP_ALERT),
        "limit": max(1, _as_int(
            limit if limit is not None else node.get("limit"), DEFAULT_LIMIT)),
        "sleep_sec": max(0.0, float(
            sleep_sec if sleep_sec is not None else DEFAULT_SLEEP_SEC)),
        "alert": alert,
        "personas": parse_persona_list(get(args, "personas", "")),
    }


# ── 纯函数：选人 ─────────────────────────────────────────────────────────────

def select_personas(profiles: Dict[str, dict],
                    explicit: Optional[Sequence[str]] = None,
                    limit: int = DEFAULT_LIMIT) -> Tuple[List[Dict[str, Any]],
                                                         List[Dict[str, Any]]]:
    """选出「值得考」的人设 + 跳过清单。

    候选＝显式 ``explicit`` 清单（覆盖默认），否则全部已注册 profile（id 升序，
    保证每晚顺序稳定）。判据＝``build_quiz`` 真出得来题：出 0 题的人设（资料太薄
    or 找不到）一律跳过——空卷判分恒 0 分，跑了只会制造假的「不合格」。
    ``limit`` 与后面 ``run_quiz(n=limit)`` 同值，故这里的题目就是真考卷的题目
    （``build_quiz`` 缺省 seed 取自 name/id，确定性）。

    返回 ``(selected, skipped)``；selected 项 ``{persona_id, persona_name,
    persona, quiz}``，skipped 项 ``{persona_id, persona_name, reason}``
    （reason ∈ ``not_found`` | ``no_questions``）。
    """
    profiles = profiles if isinstance(profiles, dict) else {}
    ids = [str(p) for p in explicit] if explicit else sorted(str(k) for k in profiles)
    selected: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for pid in ids:
        persona = profiles.get(pid)
        if not isinstance(persona, dict) or not persona:
            skipped.append({"persona_id": pid, "persona_name": "",
                            "reason": "not_found"})
            continue
        name = str(persona.get("name") or pid)
        quiz = pq.build_quiz(persona, n=limit)
        if not quiz:
            skipped.append({"persona_id": pid, "persona_name": name,
                            "reason": "no_questions"})
            continue
        selected.append({"persona_id": pid, "persona_name": name,
                         "persona": persona, "quiz": quiz})
    return selected, skipped


# ── 纯函数：判定 ─────────────────────────────────────────────────────────────

def history_from_overview(overview: Optional[dict]) -> Dict[str, float]:
    """``persona_quiz_store.overview()`` → ``{persona_id: 历史均分(0..100)}``。

    ⚠ 必须在**本轮考卷落库之前**取快照，否则本次成绩会被算进「历史均分」，
    delta 被自己稀释（分数越低越不容易触发下滑告警）。
    """
    out: Dict[str, float] = {}
    for row in ((overview or {}).get("personas") or []):
        if not isinstance(row, dict):
            continue
        pid = str(row.get("persona_id") or "")
        avg = row.get("avg_score")
        if pid and isinstance(avg, (int, float)) and not isinstance(avg, bool):
            out[pid] = float(avg)
    return out


def coverage_regressions(skipped: Optional[Sequence[dict]],
                         history: Optional[Dict[str, float]] = None
                         ) -> List[Dict[str, Any]]:
    """「以前考得了、现在出不来题」的人设 = 监控被静默丢掉，必须告警。

    这一类**不会**表现为分数下降——考卷压根没跑，看板上只是少了一行，
    历史上已经栽过两次（K2b 收紧守卫、K2d 题源覆盖面窄）。判据取「档案里有过
    成绩」：有过成绩说明它本来考得了，现在 ``no_questions`` 就是回退；
    从没考过的新人设（真薄档案）不算，避免每加一个草稿人设就轰人。
    """
    hist = history if isinstance(history, dict) else {}
    out: List[Dict[str, Any]] = []
    for sk in (skipped or []):
        if not isinstance(sk, dict) or sk.get("reason") != "no_questions":
            continue
        pid = str(sk.get("persona_id") or "")
        if pid and pid in hist:
            out.append({"persona_id": pid,
                        "persona_name": str(sk.get("persona_name") or pid),
                        "avg_before": round(float(hist[pid]), 2)})
    return out


def wrong_items(items: Optional[Sequence[Any]],
                max_chars: int = WRONG_ANSWER_CHARS) -> List[Dict[str, Any]]:
    """答卷 → 错题清单（题干 + 期望关键词 + AI 答案截断）。"""
    out: List[Dict[str, Any]] = []
    for it in (items or []):
        if not isinstance(it, dict) or it.get("pass"):
            continue
        out.append({
            "q": str(it.get("q") or ""),
            "expect": [str(k) for k in (it.get("expect") or [])],
            "answer": str(it.get("answer") or "")[:max(0, int(max_chars))],
        })
    return out


def evaluate_run(results: Optional[Sequence[dict]],
                 history: Optional[Dict[str, float]] = None,
                 min_score: float = DEFAULT_MIN_SCORE,
                 drop_alert: float = DEFAULT_DROP_ALERT) -> Dict[str, Any]:
    """本轮成绩 × 历史均分 → 合格/不合格/下滑判定（纯函数，夜间告警的唯一依据）。

    - ``results``：每项 ``run_quiz`` 报告 + ``persona_id``（score 为 0..100 整数）。
    - ``history``：``{persona_id: 历史均分(0..100)}``；**缺该人设 = 首次跑**，
      不算 delta、绝不误报下滑。
    - ``min_score`` / ``drop_alert``：0..1 分数（>1 自动当百分数）。
      不合格＝``score < min_score``（严格小于，压线算合格）；
      下滑＝``历史均分 - score > drop_alert``（严格大于，压线不算）。

    返回 ``{min_score, drop_alert, personas[], ok_count, failed[], dropped[],
    alert, exit_code}``；``ok_count`` = 合格（未 failed）人设数，
    ``alert`` = 有不合格或下滑，``exit_code`` = 1/0（异常态 2 由主流程负责）。
    """
    hist = history if isinstance(history, dict) else {}
    min_frac = _as_fraction(min_score, DEFAULT_MIN_SCORE)
    drop_frac = _as_fraction(drop_alert, DEFAULT_DROP_ALERT)
    # 先归一再×100 会带浮点毛刺（0.7*100 = 70.00000000000001 → 70 分被判不合格）
    min_pct = round(min_frac * 100.0, 6)
    drop_pct = round(drop_frac * 100.0, 6)

    rows: List[Dict[str, Any]] = []
    failed: List[str] = []
    dropped: List[str] = []
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        pid = str(r.get("persona_id") or "")
        try:
            score = float(r.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        prev = hist.get(pid)
        prev_avg = (float(prev) if isinstance(prev, (int, float))
                    and not isinstance(prev, bool) else None)
        delta = round(score - prev_avg, 2) if prev_avg is not None else None

        is_failed = score < min_pct
        is_dropped = delta is not None and delta < -drop_pct
        if is_failed:
            failed.append(pid)
        if is_dropped:
            dropped.append(pid)
        rows.append({
            "persona_id": pid,
            "persona_name": str(r.get("persona_name") or pid),
            "score": int(round(score)),
            "passed": _as_int(r.get("passed"), 0),
            "total": _as_int(r.get("total"), 0),
            "avg_before": round(prev_avg, 2) if prev_avg is not None else None,
            "delta": delta,
            "failed": is_failed,
            "dropped": is_dropped,
            "wrong": wrong_items(r.get("items")),
        })

    alert = bool(failed or dropped)
    return {
        "min_score": min_frac,
        "drop_alert": drop_frac,
        "personas": rows,
        "ok_count": sum(1 for row in rows if not row["failed"]),
        "failed": failed,
        "dropped": dropped,
        "alert": alert,
        "exit_code": 1 if alert else 0,
    }


def build_alert_message(summary: Dict[str, Any]) -> Tuple[str, str]:
    """判定摘要 → 告警标题/正文（``host_alert.notify_host`` 直接消费）。"""
    rows = {str(r.get("persona_id")): r for r in (summary or {}).get("personas") or []}
    min_pct = round(_as_fraction((summary or {}).get("min_score"),
                                 DEFAULT_MIN_SCORE) * 100.0)
    lines: List[str] = []
    for pid in (summary or {}).get("failed") or []:
        r = rows.get(str(pid)) or {}
        lines.append(f"✗ 不合格 {r.get('persona_name') or pid}({pid})："
                     f"{r.get('score')} 分（门槛 {min_pct}）"
                     f" {r.get('passed')}/{r.get('total')} 题")
    for pid in (summary or {}).get("dropped") or []:
        r = rows.get(str(pid)) or {}
        delta = r.get("delta")
        lines.append(f"↓ 下滑 {r.get('persona_name') or pid}({pid})："
                     f"{r.get('score')} 分，历史均分 {r.get('avg_before')}"
                     f"（{delta} 分）")
    for lost in (summary or {}).get("coverage_lost") or []:
        lines.append(f"⚠ 失去监控 {lost.get('persona_name')}({lost.get('persona_id')})："
                     f"本轮出不来题（历史均分 {lost.get('avg_before')}）"
                     f"——出题守卫或档案字段被改窄了")
    title = "人设一致性考题回归异常"
    body = ("夜间人设考题回归发现问题（AI 可能已记不住自己的设定）：\n"
            + "\n".join(lines)
            + "\n请查 logs/quiz/ 最新日志或后台人设考题报告。")
    return title, body


# ── 编排侧接缝（测试 monkeypatch 这几个模块级函数即可零真 LLM 跑通全流程）────

def _load_config() -> dict:
    """读合并后的项目配置（config.yaml + config.local.yaml overlay）。"""
    import yaml

    cfg_path = _ROOT / "config" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    local = _ROOT / "config" / "config.local.yaml"
    if local.is_file():
        overlay = yaml.safe_load(local.read_text(encoding="utf-8")) or {}

        def _deep_merge(dst: dict, src: dict) -> dict:
            for k, v in (src or {}).items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    _deep_merge(dst[k], v)
                else:
                    dst[k] = v
            return dst

        _deep_merge(data, overlay)
    return data


class _CanonicalCfg:
    """``PersonaManager.load_personas_canonical`` 需要的最小 config_manager 面。"""

    def __init__(self, config: dict, cfg_dir: Path) -> None:
        self.config = config
        self._path = cfg_dir / "personas.yaml"

    def get_personas_config(self) -> dict:
        try:
            import yaml
            data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def load_personas(config: dict) -> Dict[str, dict]:
    """按生产同款三层顺序装配 profile 库，返回 ``{persona_id: persona}``。

    层序抄 ``persona_routes`` 启动装配：config.yaml::personas.profiles →
    personas.yaml（规范层，文件不存在即空转）→ profiles_runtime.yaml（会话覆写，
    本机人设的真正来源）。考卷 prompt 走 ``build_quiz_system_prompt`` →
    ``PersonaManager.get_instance()``，与这里同一个单例，故装配一次即全链一致。
    """
    from src.utils.persona_manager import PersonaManager

    cfg_dir = _ROOT / "config"
    pm = PersonaManager.get_instance()
    pm.load_profiles_from_config(config)
    try:
        pm.load_personas_canonical(_CanonicalCfg(config, cfg_dir))
    except Exception:
        pass
    try:
        pm.load_profiles_runtime(cfg_dir / "config.yaml", config)
    except Exception:
        pass
    out: Dict[str, dict] = {}
    for pid in pm.list_profile_ids():
        persona = pm.get_persona_by_id(pid)
        if isinstance(persona, dict) and persona:
            out[str(pid)] = persona
    return out


def _build_chat_fn():
    """生产 ``chat_fn(system, user, timeout) -> str`` + shutdown（复用 CLI 同款）。"""
    from src.utils.persona_doc_import import _build_cli_chat_fn

    return _build_cli_chat_fn()


def _history_snapshot() -> Dict[str, float]:
    from src.utils import persona_quiz_store as pqs

    return history_from_overview(pqs.overview(limit_personas=200))


def _save_report(persona_id: str, report: dict) -> Any:
    from src.utils import persona_quiz_store as pqs

    return pqs.save_report(persona_id, report)


def _send_alert(title: str, message: str) -> bool:
    """走仓内既有告警出口：日志 + EventBus ``host_alert`` 镜像（+ 算力机弹窗）。

    ``notify_host`` 复用现成的 ``host_alert`` 事件与 webhook 订阅别名，不新增事件
    类型；测试进程由 conftest 的 ``HOST_ALERT_SILENT=1`` 全局静默（只记录不弹窗）。
    """
    from src.utils.host_alert import notify_host

    return notify_host(title, message, key=ALERT_KEY,
                       cooldown_sec=ALERT_COOLDOWN_SEC)


# ── 主流程 ───────────────────────────────────────────────────────────────────

def _print_human(summary: Dict[str, Any]) -> None:
    for row in summary.get("personas") or []:
        mark = "✗" if row["failed"] else ("↓" if row["dropped"] else "✓")
        delta = "" if row["delta"] is None else \
            f"，历史均分 {row['avg_before']}（{row['delta']:+.2f}）"
        print(f" {mark} {row['persona_name']}({row['persona_id']}): "
              f"{row['score']} 分 {row['passed']}/{row['total']}{delta}")
        for w in row["wrong"]:
            print(f"    ✗ Q: {w['q']}")
            print(f"      expect: {w['expect']}")
            print(f"      A: {w['answer'] or '(空回答)'}")
    for sk in summary.get("skipped") or []:
        print(f" - 跳过 {sk.get('persona_name') or ''}({sk['persona_id']})："
              f"{sk['reason']}")
    for lost in summary.get("coverage_lost") or []:
        print(f" ⚠ 失去监控 {lost['persona_name']}({lost['persona_id']})："
              f"本轮出不来题，历史均分 {lost['avg_before']}")
    print(f"[quiz-nightly] 合格 {summary['ok_count']}/{len(summary['personas'])}"
          f"，不合格 {summary['failed'] or '无'}，下滑 {summary['dropped'] or '无'}"
          + (f"，失去监控 {[l['persona_id'] for l in summary['coverage_lost']]}"
             if summary.get("coverage_lost") else ""))


def run_nightly(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    """编排：选人 → 逐人设实测落库 → 判定 → 告警。返回 ``(exit_code, summary)``。"""
    config = _load_config()
    st = resolve_settings(config, args)

    if not st["enabled"] and not getattr(args, "force", False):
        _log("[quiz-nightly] personas.quiz.nightly.enabled=false，跳过"
             "（手工跑加 --force）")
        return 0, {"ok": True, "reason": "disabled", "personas": [], "skipped": []}

    if getattr(args, "db", ""):
        from src.utils import persona_quiz_store as pqs
        pqs.configure(args.db)

    profiles = load_personas(config)
    selected, skipped = select_personas(profiles, st["personas"], st["limit"])
    _log(f"[quiz-nightly] 人设 {len(profiles)} 个，可考 {len(selected)}，"
         f"跳过 {len(skipped)}（题量上限 {st['limit']}）")

    if getattr(args, "dry_run", False):
        if not getattr(args, "json", False):   # --json 时 stdout 只留 JSON
            for item in selected:
                print(f" · {item['persona_name']}({item['persona_id']}) "
                      f"{len(item['quiz'])} 题")
                for q in item["quiz"]:
                    print(f"    - {q['q']}  expect={q['expect']}")
            for sk in skipped:
                print(f" - 跳过 {sk.get('persona_name') or ''}({sk['persona_id']})："
                      f"{sk['reason']}")
        return 0, {"ok": True, "reason": "dry_run",
                   "personas": [], "skipped": skipped,
                   "selected": [{"persona_id": i["persona_id"],
                                 "persona_name": i["persona_name"],
                                 "questions": len(i["quiz"])} for i in selected]}

    history = _history_snapshot()   # 必须早于落库，否则本轮成绩污染历史均分
    # 「以前考得了、现在出不来题」也要走告警——它不体现为分数下降（见 K2b/K2d），
    # 所以哪怕一个人设都没选出来，也不能在这里直接 return。
    coverage_lost = coverage_regressions(skipped, history)

    results: List[Dict[str, Any]] = []
    if not selected:
        _log("[quiz-nightly] 没有可考人设（资料都太薄）")
    else:
        chat_fn, shutdown = _build_chat_fn()
        # 裁判只复核「关键词判错」的题，且只能把错改成对（fail-closed），成本与错题数成正比
        judge_fn = chat_fn if pq.llm_judge_enabled(config) else None
        try:
            for idx, item in enumerate(selected):
                if idx and st["sleep_sec"] > 0:
                    time.sleep(st["sleep_sec"])
                t0 = time.time()
                report = pq.run_quiz(item["persona"], chat_fn, n=st["limit"],
                                     judge_fn=judge_fn)
                report["persona_id"] = item["persona_id"]
                report.setdefault("persona_name", item["persona_name"])
                try:
                    _save_report(item["persona_id"], report)
                except Exception as exc:  # noqa: BLE001 — 落库失败不该毁掉整轮成绩
                    _log(f"[quiz-nightly] {item['persona_id']} 报告落库失败: {exc}")
                results.append(report)
                judged = int(report.get("judged") or 0)
                _log(f"[quiz-nightly] {item['persona_name']}({item['persona_id']}) "
                     f"score={report.get('score')} "
                     f"({report.get('passed')}/{report.get('total')}) "
                     f"{'裁判放行 %d 题 ' % judged if judged else ''}"
                     f"{time.time() - t0:.1f}s")
        finally:
            try:
                shutdown()
            except Exception:
                pass

    summary = evaluate_run(results, history, st["min_score"], st["drop_alert"])
    summary["ok"] = True
    summary["skipped"] = skipped
    summary["coverage_lost"] = coverage_lost
    if coverage_lost:
        summary["alert"] = True
        summary["exit_code"] = 1
    summary["limit"] = st["limit"]
    summary["alert_sent"] = False
    if summary["alert"] and st["alert"]:
        title, message = build_alert_message(summary)
        try:
            summary["alert_sent"] = bool(_send_alert(title, message))
        except Exception as exc:  # noqa: BLE001 — 告警出口自身绝不影响退出码
            _log(f"[quiz-nightly] 告警发送失败: {exc}")
    return summary["exit_code"], summary


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="人设一致性考题夜间自动回归（分数下滑告警）")
    ap.add_argument("--personas", default="",
                    help="只考这些人设 id（逗号分隔）；缺省=全部有料人设")
    ap.add_argument("--limit", type=int, default=None,
                    help="每人设题量上限（缺省读配置，再缺省 %d）" % DEFAULT_LIMIT)
    ap.add_argument("--min-score", type=float, default=None, dest="min_score",
                    help="合格线 0..1（缺省读配置，再缺省 %.2f）" % DEFAULT_MIN_SCORE)
    ap.add_argument("--drop-alert", type=float, default=None, dest="drop_alert",
                    help="较历史均分下滑超过此幅度即告警 0..1（缺省 %.2f）"
                         % DEFAULT_DROP_ALERT)
    ap.add_argument("--sleep", type=float, default=None,
                    help="人设之间间隔秒数（缺省 %.1f）" % DEFAULT_SLEEP_SEC)
    ap.add_argument("--db", default="",
                    help="考题报告库路径（缺省 config/persona_quiz.db；多实例部署指其 data 目录）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="只选人出题、不调 LLM（验证选人与题目生成）")
    ap.add_argument("--no-alert", action="store_true", dest="no_alert",
                    help="本次不发告警（只出报告）")
    ap.add_argument("--force", action="store_true",
                    help="忽略 personas.quiz.nightly.enabled=false 强制跑")
    args = ap.parse_args(argv)

    try:
        code, summary = run_nightly(args)
    except Exception as exc:  # noqa: BLE001 — 执行异常统一 exit 2
        import traceback
        traceback.print_exc()
        _log(f"[quiz-nightly] 执行异常: {type(exc).__name__}: {exc}")
        return 2

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif summary.get("personas"):
        _print_human(summary)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
