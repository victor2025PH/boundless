#!/usr/bin/env python3
"""托管链自检：为什么这台机器还要自己填 Telegram api_id / 还没接上托管 AI。

背景
====
桌面版的「用户不用申请 api_id」不是靠随包配置，而是一条**运行时链**：

    首启领试用（留联系方式）→ 官网签发设备令牌（cx.…）
      → main.py 启动时 hosted_gateway.ensure_hosted_telegram()
      → POST {site}/api/pool/telegram-cred（Bearer 设备令牌）
      → 服务端按机器指纹**粘定**派发一组 api_id/api_hash → 注入内存 telegram.*

这条链有四个前提，缺任何一个都**静默**回落到「用户自己填一次」：
  ① 托管态开（桌面 AITR_DESKTOP_MODE=1 缺省即开；自建服可能显式关了 hosted_ai）
  ② 机器指纹算得出来（platform/licensing 漏打包时会取空）
  ③ 这台机器领过试用（跳过首启「领取」那步 → 官网 no_claim，不发令牌）
  ④ 官网 VPS 配了凭据池（POOL_TG_CREDS / POOL_TG_CREDS_FILE，空=pool_disabled）

「静默」是这里唯一的难点：日志只有一行 info，用户看到的只是接入弹窗要他填 API ID。
本工具把四个前提逐条摊开，并给出**这一条**该做什么，避免逐层猜。

用法
====
    python scripts/hosted_chain_doctor.py                 # 只读自检（不打官网）
    python scripts/hosted_chain_doctor.py --live          # 额外真打一次派发接口
    python scripts/hosted_chain_doctor.py --json
    python scripts/hosted_chain_doctor.py --data-root D:\\chengjie-instances\\zhiliao\\data

``--live`` 对本机**幂等**：派发按指纹粘定，同机反复调用拿回同一组（服务端 reused=true），
与 app 每次启动做的事完全相同，不会多占池容量。

退出码：0=链路通（或按设计不该通，如自建服显式关了托管）；1=有缺环。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OK, BAD, WARN = "✓", "✗", "!"


def _data_roots(explicit: str = "") -> list:
    """要体检的数据根们：显式 → AITR_DATA_ROOT → 本机活跃实例（逐根）→ 引擎根。

    复用 scripts/_data_root 这个唯一事实源，不自己再写一份解析。为什么不能直接用 CWD：
    生产进程的 CWD 是实例数据根，而从引擎根跑 CLI 会读到迁移时刻遗留的旧副本——同一句
    相对路径两处含义不同，是本仓踩过的「不报错的失真」（见 AGENTS.md「CWD 相对路径」）。
    """
    from scripts._data_root import resolve_data_roots
    return [Path(p) for p in resolve_data_roots(explicit)]


def diagnose(data_root: Path, *, live: bool = False) -> dict:
    """返回结构化诊断（纯读；live=True 时才发一次 HTTP）。"""
    from scripts._data_root import load_merged_config
    cfg = load_merged_config(data_root)
    steps: list = []

    def step(name: str, ok: bool, detail: str, fix: str = "", *, warn: bool = False):
        steps.append({"name": name, "ok": bool(ok), "warn": bool(warn),
                      "detail": detail, "fix": fix})

    # ── ① 托管态 ────────────────────────────────────────────────────────────
    hosted = ((cfg.get("licensing") or {}).get("hosted_ai") or {})
    explicit_off = hosted.get("enabled") is False
    desktop_env = str(os.environ.get("AITR_DESKTOP_MODE") or "").lower() in (
        "1", "true", "yes", "on")
    managed_env = str(os.environ.get("AITR_MANAGED_EDITION") or "").lower() in (
        "1", "true", "yes", "on")
    wants = (not explicit_off) and (
        hosted.get("enabled") is True or desktop_env or managed_env)
    if explicit_off:
        step("托管链开关", False,
             "licensing.hosted_ai.enabled=false（显式关闭）",
             "自建服若想让用户免申请 api_id，把它删掉或置 true；纯自备凭据部署则属正常。")
    elif wants:
        step("托管链开关", True,
             f"开（hosted_ai.enabled={hosted.get('enabled')} / "
             f"AITR_DESKTOP_MODE={desktop_env} / AITR_MANAGED_EDITION={managed_env}）")
    else:
        step("托管链开关", False,
             "未开：既没配 hosted_ai.enabled=true，环境也不是桌面/托管态",
             "桌面壳会注入 AITR_DESKTOP_MODE=1；从引擎根手动跑 CLI 时可 "
             "set AITR_DESKTOP_MODE=1 再自检。")

    site = str(hosted.get("site_url")
               or ((cfg.get("licensing") or {}).get("trial") or {}).get("site_url")
               or "https://bd2026.cc").rstrip("/")
    step("官网地址", True, site)

    # ── ② 机器指纹 ──────────────────────────────────────────────────────────
    fp = ""
    try:
        from src.licensing.machine_bridge import machine_fingerprint
        fp = machine_fingerprint()
    except Exception as e:  # noqa: BLE001
        fp = ""
        step("机器指纹", False, f"取指纹异常：{type(e).__name__}: {e}",
             "多为 platform/licensing 没打进包（安装版）；见 build_backend.py 的 DATAS。")
    else:
        if fp:
            step("机器指纹", True, fp)
        else:
            step("机器指纹", False, "取到空串",
                 "platform/licensing 不可用 → 绑机与派发都会失效；安装版检查是否漏打包。")

    # ── ③ 领过试用没有 ──────────────────────────────────────────────────────
    claim_file = data_root / "config" / "trial_claim.json"
    claim: dict = {}
    if claim_file.is_file():
        try:
            claim = json.loads(claim_file.read_text(encoding="utf-8")) or {}
        except Exception:
            claim = {}
    claimed = bool(claim.get("claimed") or claim.get("contact") or claim.get("ok"))
    if claimed:
        step("首启领取试用", True, f"已领取（{claim_file.name}）")
    else:
        step("首启领取试用", False,
             f"没有领取记录（{claim_file}）",
             "首启向导那一步「留个联系方式，免费领 7 天完整版」被跳过了。官网默认只给"
             "领过试用的指纹签发设备令牌（AI_GATEWAY_REQUIRE_CLAIM），跳过＝拿不到令牌"
             "＝既没有托管 AI 也拿不到 Telegram 凭据。让用户在「会员中心」补领即可；"
             "运维若要彻底去掉这道闸，在官网设 AI_GATEWAY_REQUIRE_CLAIM=0（代价是"
             "任何人刷随机指纹都能白嫖，且丢掉线索归属）。")

    # ── ④ 设备令牌 ──────────────────────────────────────────────────────────
    tok_file = data_root / "config" / "hosted_ai_token.json"
    tok: dict = {}
    if tok_file.is_file():
        try:
            tok = json.loads(tok_file.read_text(encoding="utf-8")) or {}
        except Exception:
            tok = {}
    token = str(tok.get("token") or "")
    if token.startswith("cx."):
        import time
        exp = int(tok.get("exp") or 0)
        left_d = (exp - time.time()) / 86400 if exp else 0
        step("设备令牌", True,
             f"已签发（{token[:6]}…，剩余 {left_d:.1f} 天）" if exp else "已签发")
    else:
        step("设备令牌", False, f"没有缓存令牌（{tok_file}）",
             "领过试用后应在启动时自动签发；连不上官网也会这样（软失败）。"
             "领取后无需重启，license 路由钩子会立即重试。")

    # ── ⑤ 当前 telegram 凭据 ────────────────────────────────────────────────
    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    api_id = str((tg or {}).get("api_id") or "").strip()
    if api_id:
        step("Telegram 凭据", True,
             f"配置里已有 api_id={api_id}（用户自填或此前注入；注入是内存态，"
             "落盘文件里看不到才是正常）")
    else:
        step("Telegram 凭据", False, "配置里 api_id 为空",
             "链路通时由 ensure_hosted_telegram 在启动时注入内存（不落盘）；"
             "不通则接入弹窗回落「填 API ID / Hash 保存即启用」自助表单。",
             warn=True)

    # ── ⑥ 真打一次派发（可选）───────────────────────────────────────────────
    if live:
        if not fp:
            step("派发接口实测", False, "跳过：没有机器指纹")
        else:
            import urllib.error
            import urllib.request
            url = f"{site}/api/pool/telegram-cred"
            body = json.dumps({"fingerprint": fp}).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("content-type", "application/json")
            req.add_header("accept", "application/json")
            if token.startswith("cx."):
                req.add_header("authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8") or "{}")
                # 🔒 只报 api_id 与是否复用，绝不打印 api_hash
                step("派发接口实测", bool(data.get("ok")),
                     f"ok={data.get('ok')} api_id={data.get('api_id')} "
                     f"name={data.get('name')} reused={data.get('reused')}")
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read().decode("utf-8") or "{}")
                except Exception:
                    data = {}
                err = str(data.get("error") or f"http_{e.code}")
                fixes = {
                    "pool_disabled":
                        "官网没配凭据池：在 VPS 上设 POOL_TG_CREDS（或 POOL_TG_CREDS_FILE）"
                        "为 [{\"api_id\":\"…\",\"api_hash\":\"…\",\"max\":50,\"name\":\"grp-1\"}]，"
                        "重启站点后在 /console/trial 页应看到组数与容量。",
                    "no_claim": "这台机器没领过试用（见上一步），或令牌已过期。",
                    "pool_full": "池满了：所有组的 max 都用尽 → 加新的 api_id 组或提高 max。",
                    "rate_limited": "触发限流（10 分钟窗口）：稍后再试。",
                    "bad_fingerprint": "指纹格式不合规（需 XXXX-XXXX-XXXX-XXXX 形态）。",
                }
                step("派发接口实测", False, f"HTTP {e.code} error={err}",
                     fixes.get(err, "看官网日志 /api/pool/telegram-cred。"))
            except Exception as e:  # noqa: BLE001
                step("派发接口实测", False, f"连不上：{type(e).__name__}: {e}",
                     "网络/DNS/站点未起；客户端此时静默回落自助流程（不阻断）。")

    hard = [s for s in steps if not s["ok"] and not s["warn"]]
    return {"data_root": str(data_root), "site": site, "steps": steps,
            "ready": not hard}


def main() -> int:
    ap = argparse.ArgumentParser(description="托管链（免申请 api_id）自检")
    ap.add_argument("--live", action="store_true",
                    help="额外真打一次派发接口（对本机幂等，不多占池容量）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    args = ap.parse_args()

    roots = _data_roots(args.data_root)
    reports = [diagnose(r, live=args.live) for r in roots]

    if args.json:
        print(json.dumps({"roots": reports,
                          "ready": all(r["ready"] for r in reports)},
                         ensure_ascii=False, indent=2))
        return 0 if all(r["ready"] for r in reports) else 1

    for rep in reports:
        if len(reports) > 1:
            print("=" * 70)
        print(f"数据根: {rep['data_root']}")
        print(f"官网:   {rep['site']}\n")
        for s in rep["steps"]:
            mark = OK if s["ok"] else (WARN if s["warn"] else BAD)
            print(f"{mark} {s['name']}: {s['detail']}")
            # 处置建议不做硬折行：按字符数切会把 AI_GATEWAY_REQUIRE_CLAIM 这类标识符
            # 截成两半，反而不可读、也没法复制粘贴。交给终端自己折。
            if not s["ok"] and s["fix"]:
                print(f"    → {s['fix']}")
        print()
        if rep["ready"]:
            print("✓ 托管链前提齐备（未加 --live 则派发接口本身未实测）")
        else:
            print("✗ 有缺环：按上面每条的 → 处置。链路不通时不会报错，只会静默回落"
                  "「用户自己填 api_id」——这正是它容易被忽略的原因。")
        print()
    return 0 if all(r["ready"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
