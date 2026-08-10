# -*- coding: utf-8 -*-
"""会话人设「失败出路」真浏览器交互验证（Playwright；2026-07-31 P3）。

**为什么需要它**：node 常驻门禁（desktop/test/cp-persona-failtype.test.js）证「分型
纯函数对不对」，但证不了「分型 → 真的渲染成对应 failtip DOM + 按钮 + 点击接线」。
cp-persona 是 shadow-DOM Web Component，模板热更新直上生产。故用真浏览器装配组件、
注入一个只会失败的假 client、触发换绑，断言 failtip 的文案/按钮/状态重申都对上。

刻意**零副作用**：假 client 的 bindConvPersona 只返回失败对象、绝不打真接口，
换绑目标是探针会话键；effective 数据构造成「无既往绑定、无出站」→ 走静默直绑路径
（跳过确认弹窗，直达 _doBindConv → 失败 → _showFail）。缺 playwright / 实例不可达
→ SKIP exit 0（不污染回归信号）。

覆盖矩阵（每种失败一遍）：
  401       → 文案含「登录已过期」+ 唯一按钮 fail-reload + 重申「仍以 X 发言」
  403 csrf  → 文案含「安全校验」+ 按钮 fail-reload
  404       → 文案含「不存在/已删除」+ 按钮 fail-refresh
  network   → 文案含「网络异常」+ 按钮 fail-retry + fail-refresh + **不**重申状态

用法::  python tools/verify_persona_failtip.py [--base http://127.0.0.1:18799]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s（见 verify_inbox_density.py 同行注释）
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"


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


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        okk = bool(cond)
        self.results.append((name, okk))
        print(f"  [{'PASS' if okk else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return okk

    def summary(self) -> int:
        fails = [n for n, okk in self.results if not okk]
        total = len(self.results)
        print(f"\n== 人设失败出路验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


# 在页面里装配一个 cp-persona + 假 client，触发一次换绑失败，回收 failtip 的
# 文本与按钮 data-act 集合。所有断言在 Python 侧做（语言无关，只认 data-act 与
# 文案里语言无关的锚点：i18n 键的稳定子串靠 UI 语言，故断言用 data-act 为主、
# 文案存在性为辅）。
_DRIVE_JS = r"""
async (failResult) => {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const el = document.createElement('cp-persona');
  // 假 client：effective 开启 + 两个人设 + 无既往绑定/无出站 → 静默直绑路径；
  // bindConvPersona 只返回注入的失败对象（零真实副作用）。
  el.client = {
    listPersonas: async () => ({ ok: true,
      summary: [{id:'p1',name:'甲',role:''},{id:'p2',name:'乙',role:''}],
      profiles: {p1:{id:'p1',name:'甲'}, p2:{id:'p2',name:'乙'}} }),
    getPersonaBindings: async () => ({ ok: true, bindings: {} }),
    personaEffective: async () => ({ ok:true, enabled:true, conv:null,
      account:{id:'acc',name:'账号人设X'}, legacy:null, has_outbound:false,
      effective:{tier:'account_profile', name:'账号人设X', id:'acc'},
      voice_autosend:{} }),
    bindConvPersona: async () => failResult,
    unbindConvPersona: async () => ({ok:true}),
  };
  el.context = { conversationId:'telegram:acc:qa_probe_failtip',
                 chatKey:'qa_probe_failtip', platform:'telegram', accountId:'acc' };
  host.appendChild(el);
  // 等 fetchData → render（effective 卡片列表出现）
  const sr = el.shadowRoot;
  for (let i=0;i<40;i++){ if (sr && sr.querySelector('[data-act="pick"]')) break;
    await new Promise(r=>setTimeout(r,50)); }
  const pick = sr.querySelector('[data-act="pick"]');
  if (!pick) return { error: 'no pick card rendered' };
  pick.click();                       // 无既往绑定/出站 → 静默直绑 → 失败
  let tip = null;
  for (let i=0;i<40;i++){ tip = sr.querySelector('[data-role="failtip"]');
    if (tip && !tip.classList.contains('hide')) break;
    await new Promise(r=>setTimeout(r,50)); }
  if (!tip || tip.classList.contains('hide')) return { error: 'failtip not shown' };
  const btns = Array.from(tip.querySelectorAll('button')).map(b=>b.getAttribute('data-act'));
  const out = { text: tip.textContent||'', buttons: btns,
                hasStill: !!tip.querySelector('.fstill') };
  host.remove();
  return out;
}
"""


def run(base: str, token: str) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page.goto(base + "/workspace", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        if not ck.check("cp-persona 组件已注册",
                        page.evaluate("!!customElements.get('cp-persona')")):
            browser.close()
            return ck.summary() or 1

        cases = [
            ("401 过期", {"ok": False, "status": 401},
             ["fail-reload"], True),
            ("403 CSRF", {"ok": False, "status": 403, "code": "csrf"},
             ["fail-reload"], True),
            ("404 已删除", {"ok": False, "status": 404},
             ["fail-refresh"], True),
            ("网络异常", {"ok": False, "status": 0, "code": "network"},
             ["fail-retry", "fail-refresh"], False),
        ]
        for name, fail, want_btns, want_still in cases:
            r = page.evaluate(_DRIVE_JS, fail)
            if not isinstance(r, dict) or r.get("error"):
                ck.check(f"[{name}] 触发失败提示", False,
                         str(r.get("error") if isinstance(r, dict) else r))
                continue
            btns = r.get("buttons") or []
            ck.check(f"[{name}] 出路按钮 = {want_btns}", btns == want_btns,
                     f"got {btns}")
            ck.check(f"[{name}] failtip 有文案", bool((r.get("text") or "").strip()))
            # 4xx 断言「当前仍生效」；网络类绝不谎报状态
            ck.check(f"[{name}] 状态重申={'有' if want_still else '无'}",
                     bool(r.get("hasStill")) == want_still,
                     f"hasStill={r.get('hasStill')}")

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装，跳过（exit 0）")
        return 0
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except Exception:
        print(f"[SKIP] 实例不可达 {args.base}，跳过（exit 0）")
        return 0
    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[SKIP] 未能从 {args.data_root} 读到 auth_token（exit 0）")
        return 0
    return run(args.base, token)


if __name__ == "__main__":
    sys.exit(main())
