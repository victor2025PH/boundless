# -*- coding: utf-8 -*-
"""快捷回复链「装载状态」冒烟（2026-08-18，给重启窗后的验证人一条命令）。

背景：cp-kb 面板四组能力分批落盘、分批搭重启窗——P0 中文化治理（已随 17:29
批装载）、P1 编辑闭环（个人常用语 + can_edit + 变量放行）、V0 KB 按会话语言取
译稿、V1 团队话术多语言变体（并行线）。板上手工 curl 步骤容易漏/口径漂，
本工具把验证收成一条命令（对齐 tools/smoke_goal_notify.py 家法）：

    python tools/smoke_quick_replies.py            # 全组体检
    python tools/smoke_quick_replies.py --json     # 机器可读

判定（verdict 纯函数，tests/test_smoke_quick_replies.py 钉住）：
- LOADED         该组代码已装载且契约成立；
- RIDES_RESTART  路由/字段还不在 ＝ 代码没装载，等下个重启窗（不是故障）；
- BROKEN         已装载但契约不成立（如中文化开着还漏机器键名、增删打不通）
                 ——重启多少次都不会好，先修代码/数据。
- 附注（不降档）：V1 旁挂档未播种、个人常用语为空等「内容还没备货」状态。

鉴权=read_token(实例 web_admin.auth_token) + /login 换 session + Bearer
（smoke_voice_reuse / live_multiwin_drill 同口径）；实例不可达 / 无 token →
SKIP exit 0（不污染回归信号）。个人常用语走「加一条唯一文本→确认可见→删除」
的自清理回路（token 会话身份=共享 "agent" 桶，往返后状态还原）。
退出码：任一组 BROKEN → 2；其余（LOADED/RIDES_RESTART/SKIP）→ 0。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:18799"
DEFAULT_DATA_ROOT = r"D:\chengjie-instances\zhiliao\data"

# 与 tests/test_quick_templates_curation.py 同口径：机器键名形态（英文黑话泄漏判据）
_MACHINE_LABEL_RE = re.compile(r"^[a-z0-9_.]+( #\d+)?$")


# ── 纯函数（门禁钉这里）────────────────────────────────────────────────────

def machine_label_leaks(templates: List[Dict[str, Any]]) -> List[str]:
    """团队话术里仍长着机器键名脸的 label（P0 治理后应为空）。"""
    out: List[str] = []
    for t in templates or []:
        lbl = str((t or {}).get("label") or "")
        if lbl and _MACHINE_LABEL_RE.match(lbl):
            out.append(lbl)
    return out


def verdict_p0(resp: Optional[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """P0 中文化治理：labels 无机器键名 + 系统模板不出现。"""
    if not isinstance(resp, dict) or resp.get("ok") is not True:
        return "BROKEN", ["templates 端点响应异常（ok!=true）"]
    tpls = resp.get("templates") or []
    leaks = machine_label_leaks(tpls)
    if leaks:
        return "BROKEN", [f"机器键名仍泄漏给坐席: {leaks[:5]}"]
    bad = [t.get("label") for t in tpls
           if str(t.get("key") or "").startswith("gxp_") or t.get("key") == "test"]
    if bad:
        return "BROKEN", [f"系统模板漏进面板: {bad[:5]}"]
    return "LOADED", []


def verdict_p1(resp: Optional[Dict[str, Any]], mutate_probe_status: int) -> Tuple[str, List[str]]:
    """P1 编辑闭环：can_edit 特性字段 + quick-replies 路由在位。

    mutate_probe_status＝带鉴权空 action POST 的 HTTP 码：200（bad_action 软错）
    =路由已装载；404=还没装载（等重启）；其余=异常。
    """
    reasons: List[str] = []
    field_ok = isinstance(resp, dict) and "can_edit" in resp
    route_ok = mutate_probe_status == 200
    if field_ok and route_ok:
        return "LOADED", []
    if not field_ok and mutate_probe_status == 404:
        return "RIDES_RESTART", ["can_edit 字段缺席 + quick-replies 路由 404 ＝ P1 后端未装载"]
    if not field_ok:
        reasons.append("templates 响应缺 can_edit 字段（P1 聚合未装载）")
    if mutate_probe_status == 404:
        reasons.append("quick-replies 路由 404（P1 路由未装载）")
    elif not route_ok:
        reasons.append(f"quick-replies 探针异常 HTTP {mutate_probe_status}")
    # 半装载（一半在一半不在）＝不该出现的状态，按 BROKEN 点名
    return ("RIDES_RESTART" if not field_ok and not route_ok else "BROKEN"), reasons


def verdict_v0(kb_resp: Optional[Dict[str, Any]],
               openapi_has_lang: Optional[bool] = None) -> Tuple[str, List[str]]:
    """V0 KB 语言接线：以 OpenAPI 参数表为准（确定性、与数据无关）。

    首版曾用「响应回显 lang」当判据——错了：lang 只作为参数进重排，语言标记
    只挂在**有译稿的条目**上；生产 KB 未播译稿时零命中，新旧后端响应形状
    完全一样（首跑误报 RIDES_RESTART 实锤）。OpenAPI 的 kb-search 参数表里
    有没有 ``lang`` 形参才是装载与否的硬证据。
    """
    if openapi_has_lang is True:
        if isinstance(kb_resp, dict) and (kb_resp.get("ok") is True
                                          or kb_resp.get("error") == "kb_unavailable"):
            return "LOADED", []
        return "BROKEN", [f"lang 形参已装载但 kb-search 响应异常: {str(kb_resp)[:120]}"]
    if openapi_has_lang is False:
        return "RIDES_RESTART", ["OpenAPI 无 lang 形参 ＝ V0 后端未装载"]
    # openapi 不可得（探针失败）时退回响应形状弱判据：能证已装载，不能证未装载
    if isinstance(kb_resp, dict) and any(
            isinstance(e, dict) and "lang" in e for e in (kb_resp.get("entries") or [])):
        return "LOADED", []
    return "RIDES_RESTART", ["openapi 不可得且响应无语言标记——判据不足，按未装载保守计"]


def openapi_param_present(openapi: Optional[Dict[str, Any]], path: str,
                          param: str) -> Optional[bool]:
    """OpenAPI 文档里某 GET 路由有没有某 query 形参（文档不可得→None）。"""
    try:
        if not isinstance(openapi, dict):
            return None
        node = (openapi.get("paths") or {}).get(path)
        if not isinstance(node, dict):
            return False
        params = ((node.get("get") or {}).get("parameters")) or []
        return any(isinstance(p, dict) and p.get("name") == param for p in params)
    except Exception:
        return None


def verdict_v1(resp: Optional[Dict[str, Any]], sidecar_paths: List[str]) -> Tuple[str, List[str]]:
    """V1 变体：行内 i18n 字段（有播种才可见）+ 旁挂档存在性（内容备货态附注）。"""
    notes: List[str] = []
    has_sidecar = bool(sidecar_paths)
    rows = (resp or {}).get("templates") or []
    has_i18n = any(isinstance(t, dict) and t.get("i18n") for t in rows)
    if has_i18n:
        return "LOADED", []
    if has_sidecar:
        return "RIDES_RESTART", [
            f"旁挂档在（{sidecar_paths[0]}）但行内无 i18n ＝ V1 聚合未装载（或键对不上）"]
    notes.append("templates_i18n.yaml 未播种——V1 装载与否无法从数据面判定；"
                 "播种后重跑（起草 CLI/后台变体编辑属并行线交付）")
    return "RIDES_RESTART", notes


def overall_exit(verdicts: Dict[str, str]) -> int:
    return 2 if any(v == "BROKEN" for v in verdicts.values()) else 0


# ── HTTP 客户端（smoke_voice_reuse 同口径）─────────────────────────────────

class Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))

    def _raw(self, req: urllib.request.Request, timeout: float) -> Tuple[int, str]:
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, json.dumps({"error": str(e)[:200]})

    def login(self) -> bool:
        data = urllib.parse.urlencode({"auth_token": self.token}).encode()
        req = urllib.request.Request(
            self.base + "/login", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        code, _ = self._raw(req, 30)
        return code in (200, 303) and any(c.name == "session" for c in self._cj)

    def call(self, method: str, path: str, body: Any = None,
             timeout: float = 60) -> Tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers)
        code, raw = self._raw(req, timeout)
        try:
            return code, json.loads(raw)
        except Exception:
            return code, {"raw": raw[:300]}


def read_token(data_root: str) -> str:
    import yaml
    root = Path(data_root)
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


def find_sidecars(cli_root: str) -> List[str]:
    """templates_i18n.yaml 存在的落点（实例数据根们 + 引擎根）。"""
    out: List[str] = []
    seen = set()
    for root in list(resolve_data_roots(cli_root)) + [_ENGINE_ROOT]:
        p = Path(root) / "config" / "templates_i18n.yaml"
        k = str(p.resolve()) if p.exists() else ""
        if k and k not in seen:
            seen.add(k)
            out.append(str(p))
    return out


# ── 主流程 ─────────────────────────────────────────────────────────────────

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="快捷回复链装载状态冒烟（只读+自清理回路）")
    ap.add_argument("--base", default=DEFAULT_BASE_URL)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    token = read_token(args.data_root)
    if not token:
        print(f"[SKIP] 未能从 {args.data_root} 读到 web_admin.auth_token（exit 0）")
        return 0
    cli = Client(args.base, token)
    if not cli.login():
        print("[SKIP] 实例不可达或登录失败（exit 0，不污染回归信号）")
        return 0

    # S1 模板载荷（P0/P1 字段/V1 行内变体一次取全）
    _, tpl = cli.call("GET", "/api/unified-inbox/templates")
    # S2 P1 路由探针：空 action ＝ 服务端软错 bad_action，零副作用
    probe_code, _probe = cli.call("POST", "/api/unified-inbox/quick-replies", {"action": ""})
    # S3 V0 kb-search 契约（OpenAPI 形参=硬证据；响应只作辅助）
    _, kb = cli.call("GET", "/api/unified-inbox/kb-search?q=%E4%BD%A0%E5%A5%BD&lang=en")
    _, oa = cli.call("GET", "/openapi.json", timeout=90)
    has_lang = openapi_param_present(oa if isinstance(oa, dict) else None,
                                     "/api/unified-inbox/kb-search", "lang")
    sidecars = find_sidecars(args.data_root)

    verdicts: Dict[str, str] = {}
    reasons: Dict[str, List[str]] = {}
    for name, (v, r) in (
        ("P0_curation", verdict_p0(tpl)),
        ("P1_edit_loop", verdict_p1(tpl, probe_code)),
        ("V0_kb_lang", verdict_v0(kb, has_lang)),
        ("V1_variants", verdict_v1(tpl, sidecars)),
    ):
        verdicts[name] = v
        reasons[name] = r

    # S4 个人常用语自清理回路（仅 P1 已装载时跑；失败降 BROKEN）
    roundtrip = "skipped"
    if verdicts["P1_edit_loop"] == "LOADED":
        marker = f"冒烟自检话术 {int(time.time())}"
        c1, r1 = cli.call("POST", "/api/unified-inbox/quick-replies",
                          {"action": "add", "text": marker})
        ok_add = c1 == 200 and isinstance(r1, dict) and r1.get("ok") is True
        iid = ""
        if ok_add:
            for it in r1.get("items") or []:
                if it.get("text") == marker:
                    iid = str(it.get("id") or "")
        _, tpl2 = cli.call("GET", "/api/unified-inbox/templates")
        visible = any(t.get("text") == marker and t.get("source") == "mine"
                      for t in (tpl2 or {}).get("templates") or [])
        ok_del = False
        if iid:
            c3, r3 = cli.call("POST", "/api/unified-inbox/quick-replies",
                              {"action": "delete", "id": iid})
            ok_del = c3 == 200 and isinstance(r3, dict) and r3.get("ok") is True
        if ok_add and visible and ok_del:
            roundtrip = "ok"
        else:
            roundtrip = f"fail(add={ok_add},visible={visible},del={ok_del})"
            verdicts["P1_edit_loop"] = "BROKEN"
            reasons["P1_edit_loop"].append(f"个人常用语回路失败: {roundtrip}")

    payload = {"base": args.base, "verdicts": verdicts, "reasons": reasons,
               "roundtrip": roundtrip, "sidecars": sidecars,
               "template_rows": len((tpl or {}).get("templates") or [])}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(f"=== 快捷回复链冒烟 @ {args.base} ===")
        for name, v in verdicts.items():
            mark = {"LOADED": "✓", "RIDES_RESTART": "⏳", "BROKEN": "✗"}.get(v, "?")
            print(f"  {mark} {name:<14} {v}")
            for r in reasons[name]:
                print(f"      - {r}")
        print(f"  个人常用语回路: {roundtrip} ｜ 模板行数: {payload['template_rows']}"
              f" ｜ 旁挂档: {sidecars or '无'}")
        if any(v == "RIDES_RESTART" for v in verdicts.values()):
            print("  ⏳ = 等下个重启窗装载后重跑本工具（不是故障）")
    return overall_exit(verdicts)


if __name__ == "__main__":
    raise SystemExit(main())
