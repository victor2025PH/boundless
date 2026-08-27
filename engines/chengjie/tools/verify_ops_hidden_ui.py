# -*- coding: utf-8 -*-
"""运营总览「隐藏能力清单」真浏览器只读验证（Playwright；P4 2026-08-18）。

**为什么需要它**：隐藏清单是纯前端状态机（收起/展开、原因分组 chip、白话弹层、
筛选命中隐藏卡自动展开、工具栏 💤 入口），全部在 ops_overview.html 的脚本区，
模板热更新直上生产。静态门禁只能证「id 存在 / 函数在顶层 / 断言字符串在」，
证不了「点开真的展开、chip 点了真出弹层、外点真的收回、搜隐藏卡不再谎报
『没有匹配』」。与 ``tools/verify_care_ui.py`` 同族（同实例 + token 登录），
**只读**：只做展开/弹层/筛选交互，不点任何会写状态或烧 LLM 的按钮。

用法::

    python tools/verify_ops_hidden_ui.py            # 打默认实例
    python tools/verify_ops_hidden_ui.py --headed   # 肉眼看一遍

覆盖的不变量：
  1. 页面加载 + 注册表/面板函数就绪；`#opsHidPanel` 骨架在 DOM。
  2. 有隐藏卡时：面板默认**收起**（隐私换清爽的反面——不展开不占版面），
     摘要行带数量 + 按原因拆解（hint 文本含分隔符「·」）。
  3. 工具栏 💤 chip 可见、带数量；点击 → 面板展开（aria-expanded=true）。
  4. 展开后至少一个原因分组渲染，chip 数 == 隐藏卡数（分组不吞卡不复卡）。
  5. 点 chip → 弹层可见且标题非空（白话键或标题括号回落）；点面板外 → 弹层收回。
  6. 筛选命中隐藏卡：填一个隐藏卡标题的子串 → 面板自动展开、该 chip 在列、
     「没有匹配」提示**不**出现（修「搜得到的功能被谎报为不存在」）。
  7. 垃圾筛选词 → 「没有匹配」出现；清空 → 恢复。
  8. i18n 无裸键：可见文本不含 "ov2_hid"。
  9. 交互期零面板相关 pageerror（消息含 opsHid/_opsHid 才算——共享生产页，
     别的卡的错误不该红这条门禁）。

零隐藏卡的实例（全功能全流量）：2-7 自动 SKIP，只验 1/8——面板语义本身允许
「无隐藏时整段消失」，不算失败。缺 playwright / 实例不可达 / 无 token → SKIP exit 0。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

# Win 控制台默认 GBK，chip 文本里的 💤 会炸 print——统一 UTF-8 + 替换兜底。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。"""
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
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== 隐藏能力清单验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


_STATE_JS = """() => {
  const vis = (sel) => {
    const el = document.querySelector(sel);
    return !!el && el.offsetParent !== null;
  };
  const txt = (sel) => ((document.querySelector(sel) || {}).textContent || '');
  const toggle = document.getElementById('opsHidToggle');
  return {
    panelInDom: !!document.getElementById('opsHidPanel'),
    panelVisible: vis('#opsHidPanel'),
    toggleText: txt('#opsHidToggle'),
    toggleTitle: (toggle ? (toggle.title || '') : ''),
    ariaExpanded: (toggle ? toggle.getAttribute('aria-expanded') : ''),
    hintText: txt('#opsHidHint'),
    bodyVisible: vis('#opsHidBody'),
    groupN: document.querySelectorAll('#opsHidBody .ops-hid-grp').length,
    chipN: document.querySelectorAll('#opsHidBody [data-hidkey]').length,
    popVisible: vis('#opsHidPop'),
    popTitle: txt('#opsHidPop .ops-hid-pop-t'),
    tbVisible: vis('#opsHidTbChip'),
    tbText: txt('#opsHidTbChip'),
    noMatchVisible: vis('#opsNoMatch'),
    hiddenN: (typeof OPS_CARDS !== 'undefined')
      ? OPS_CARDS.filter(c => c.el && c.title && c.el.style.display === 'none'
                              && !c.el.hasAttribute('data-ops-fhide')).length
      : -1,
  };
}"""

_BOSS_JS = """() => {
  const cards = (typeof OPS_CARDS !== 'undefined') ? OPS_CARDS : [];
  let bhidden = 0, greenNonBoss = 0, bossVis = 0, probBhidden = 0;
  cards.forEach(c => {
    if (!c.el) return;
    const bh = c.el.hasAttribute('data-ops-bhide');
    const condHidden = c.el.style.display === 'none';
    const light = c._light || '';
    if (bh) bhidden++;
    if (bh && (light === 'red' || light === 'yellow')) probBhidden++;
    if (!condHidden && !bh && c.group === 'boss') bossVis++;
    if (!condHidden && !bh && c.group !== 'boss'
        && light !== 'red' && light !== 'yellow') greenNonBoss++;
  });
  return {bhidden, greenNonBoss, bossVis, probBhidden,
          bossOn: !!document.querySelector('#opsBossBtn.ops-boss-on')};
}"""

# 取一张隐藏卡的「短名」（与 chip 显示同口径：标题截到全角括号前、18 字封顶）。
_PICK_HIDDEN_JS = """() => {
  if (typeof OPS_CARDS === 'undefined') return '';
  const c = OPS_CARDS.find(c => c.el && c.title && c.el.style.display === 'none'
                                && !c.el.hasAttribute('data-ops-fhide'));
  if (!c) return '';
  const short = ((c.title || '').split('（')[0] || '').trim().slice(0, 18);
  // 去掉开头 emoji 噪声，取一段稳定的文字子串当筛选词（≥2 字）
  const m = short.match(/[\\u4e00-\\u9fffA-Za-z0-9]{2,}/);
  return m ? m[0] : '';
}"""


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1360, "height": 900})
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        panel_errors: List[str] = []
        page.on("pageerror", lambda e: panel_errors.append(str(e))
                if ("opsHid" in str(e) or "_opsHid" in str(e)
                    or "Boss" in str(e) or "_boss" in str(e)) else None)
        page.goto(base + "/admin/ops", wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => typeof opsToggleHiddenPanel === 'function'"
                " && typeof OPS_CARDS !== 'undefined'", timeout=15000)
        except Exception:
            print("  [ABORT] ops 页 JS 未就绪（登录失败或模板异常）")
            browser.close()
            return 1
        # 首轮 refreshAll ~50 loader 并发 + 3s 节拍渐进聚合；等忙位落下或 45s 兜底。
        try:
            page.wait_for_function(
                "() => typeof _opsBusy !== 'undefined' && !_opsBusy", timeout=45000)
        except Exception:
            print("  [WARN] 首轮全量未在 45s 内结束，按当前渐进态继续（不判失败）")
        # 新会话首访的「简洁/完整模式」引导弹窗会拦截 pointer events（verify_care_ui
        # 首跑同款坑）——移除后再交互。
        page.evaluate("() => { const m = document.getElementById('onboard-modal');"
                      " if (m) m.remove(); }")
        page.wait_for_timeout(500)

        print("== 1. 面板骨架 + 就绪 ==")
        st = page.evaluate(_STATE_JS)
        ck.check("#opsHidPanel 在 DOM", st["panelInDom"])
        ck.check("注册表可见隐藏卡数可算", st["hiddenN"] >= 0, f"hidden={st['hiddenN']}")

        if st["hiddenN"] > 0:
            print(f"== 2. 收起态摘要（隐藏 {st['hiddenN']} 张） ==")
            ck.check("面板可见", st["panelVisible"])
            ck.check("默认收起（aria-expanded=false）", st["ariaExpanded"] == "false",
                     f"aria={st['ariaExpanded']!r}")
            ck.check("摘要行带数量", str(st["hiddenN"]) in st["toggleText"],
                     f"text={st['toggleText']!r}")
            ck.check("收起态 hint 带原因拆解（含 · 分隔）", "·" in st["hintText"],
                     f"hint={st['hintText']!r}")
            ck.check("toggle title 带拆解", len(st["toggleTitle"]) > 2)

            print("== 3. 工具栏 💤 入口 ==")
            ck.check("💤 chip 可见且带数量", st["tbVisible"]
                     and str(st["hiddenN"]) in st["tbText"], f"tb={st['tbText']!r}")
            page.click("#opsHidTbChip")
            page.wait_for_timeout(300)
            st2 = page.evaluate(_STATE_JS)
            ck.check("点 💤 → 面板展开", st2["ariaExpanded"] == "true" and st2["bodyVisible"])

            print("== 4. 展开态分组 chip ==")
            ck.check("至少一个原因分组", st2["groupN"] >= 1, f"groups={st2['groupN']}")
            ck.check("chip 数 == 隐藏卡数（不吞不复）", st2["chipN"] == st2["hiddenN"],
                     f"chips={st2['chipN']} hidden={st2['hiddenN']}")

            print("== 5. 白话弹层 ==")
            page.click("#opsHidBody [data-hidkey]")
            page.wait_for_timeout(200)
            st3 = page.evaluate(_STATE_JS)
            ck.check("点 chip → 弹层可见", st3["popVisible"])
            ck.check("弹层标题非空", len(st3["popTitle"].strip()) >= 2,
                     f"title={st3['popTitle']!r}")
            page.click("#opsToolbar", position={"x": 4, "y": 4})
            page.wait_for_timeout(200)
            st4 = page.evaluate(_STATE_JS)
            ck.check("点面板外 → 弹层收回", not st4["popVisible"])

            print("== 6. 筛选命中隐藏卡（防「谎报没有匹配」） ==")
            term = page.evaluate(_PICK_HIDDEN_JS)
            if term:
                # 先收起面板，验证「筛选命中会自动展开」这条链
                page.evaluate("() => opsToggleHiddenPanel(false)")
                page.fill("#opsFilter", term)
                page.wait_for_timeout(450)   # 150ms 防抖 + 渲染
                st5 = page.evaluate(_STATE_JS)
                ck.check(f"搜『{term}』→ 面板自动展开", st5["bodyVisible"])
                ck.check("命中 chip 在列", st5["chipN"] >= 1, f"chips={st5['chipN']}")
                ck.check("「没有匹配」不出现", not st5["noMatchVisible"])
            else:
                print("  [SKIP] 隐藏卡标题抽不出稳定筛选词")

            print("== 7. 垃圾筛选词 → 空态；清空恢复 ==")
            page.fill("#opsFilter", "zzz__nohit__zzz")
            page.wait_for_timeout(450)
            st6 = page.evaluate(_STATE_JS)
            ck.check("全不命中 → 「没有匹配」出现", st6["noMatchVisible"])
            page.fill("#opsFilter", "")
            page.wait_for_timeout(450)
            st7 = page.evaluate(_STATE_JS)
            ck.check("清空 → 提示消失", not st7["noMatchVisible"])
            ck.check("清空 → chip 全量回列", st7["chipN"] == st7["hiddenN"],
                     f"chips={st7['chipN']} hidden={st7['hiddenN']}")
        else:
            print("  [SKIP] 本实例当前零隐藏卡（全功能全流量）——面板允许整段消失，"
                  "交互场景 2-7 跳过")
            ck.check("零隐藏时面板不可见", not st["panelVisible"])
            ck.check("零隐藏时 💤 chip 不可见", not st["tbVisible"])

        print("== 9. 老板视图（只看重点档） ==")
        has_boss = page.evaluate("() => !!document.getElementById('opsBossBtn')")
        ck.check("入口按钮存在", has_boss)
        if has_boss:
            pre = page.evaluate(_BOSS_JS)
            page.click("#opsBossBtn")
            page.wait_for_timeout(300)
            on = page.evaluate(_BOSS_JS)
            ck.check("开档 → 按钮亮起", on["bossOn"])
            if pre["greenNonBoss"] > 0:
                ck.check("开档 → 绿色非经营卡收起", on["bhidden"] >= pre["greenNonBoss"],
                         f"bhidden={on['bhidden']} green={pre['greenNonBoss']}")
            if pre["bossVis"] > 0:
                ck.check("经营组卡仍可见", on["bossVis"] >= 1, f"bossVis={on['bossVis']}")
            ck.check("红黄灯卡零被藏", on["probBhidden"] == 0,
                     f"probBhidden={on['probBhidden']}")
            # 筛选让位：焦点档开着也要搜得到全量卡
            page.fill("#opsFilter", "zzz__nohit__zzz")
            page.wait_for_timeout(450)
            flt = page.evaluate(_BOSS_JS)
            ck.check("筛选期 → 焦点档让位（零 bhide）", flt["bhidden"] == 0)
            page.fill("#opsFilter", "")
            page.wait_for_timeout(450)
            back = page.evaluate(_BOSS_JS)
            if pre["greenNonBoss"] > 0:
                ck.check("清筛选 → 焦点档恢复", back["bhidden"] >= 1,
                         f"bhidden={back['bhidden']}")
            # 开档期 URL 应带 ?view=boss（replaceState 同步 → 地址栏即深链）
            url_on = page.evaluate("() => location.search")
            ck.check("开档 → URL 带 view=boss", "view=boss" in url_on,
                     f"search={url_on!r}")
            note_on = page.evaluate(
                "() => { const n = document.getElementById('opsBossNote');"
                " return n && n.offsetParent !== null ? n.textContent : ''; }")
            if pre["greenNonBoss"] > 0:
                ck.check("开档 → 生效提示行可见且带数量",
                         len(note_on) > 4 and any(ch.isdigit() for ch in note_on),
                         f"note={note_on!r}")
            page.click("#opsBossBtn")
            page.wait_for_timeout(300)
            off = page.evaluate(_BOSS_JS)
            ck.check("关档 → 全部恢复（零 bhide + 按钮熄灭）",
                     off["bhidden"] == 0 and not off["bossOn"])
            ck.check("关档 → URL 参数清除",
                     "view=boss" not in page.evaluate("() => location.search"))

        print("== 10. 深链 ?view=boss（覆写记忆档位） ==")
        if has_boss:
            page.goto(base + "/admin/ops?view=boss", wait_until="domcontentloaded")
            try:
                page.wait_for_function(
                    "() => typeof opsToggleHiddenPanel === 'function'", timeout=15000)
            except Exception:
                print("  [ABORT] 深链页 JS 未就绪")
                browser.close()
                return 1
            page.wait_for_timeout(4000)   # 等首个 3s 聚合节拍跑过 _applyBossMode
            dl = page.evaluate(_BOSS_JS)
            ck.check("深链进页 → 焦点档生效（按钮亮起）", dl["bossOn"])
            if pre["greenNonBoss"] > 0:
                ck.check("深链进页 → 正常卡已收起", dl["bhidden"] > 0,
                         f"bhidden={dl['bhidden']}")

        print("== 8. i18n 无裸键 + 面板零 pageerror ==")
        body_text = page.evaluate("() => document.body.innerText")
        ck.check("可见文本不含 ov2_hid 裸键", "ov2_hid" not in body_text)
        ck.check("交互期零面板相关 pageerror", not panel_errors,
                 "; ".join(panel_errors[:3]))

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("[SKIP] 读不到实例 auth_token")
        return 0
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception:
        print("[SKIP] 实例不可达: " + args.base)
        return 0
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
