"""URL 主题钉的窗口隔离 + 显式选档释放门禁（2026-08-15 / 08-16 两起事故沉淀）。

## 守的是什么

`?theme=dark|light` 是**嵌入端窗口级**主题钉（桌面壳把 webview 钉成深色，防独立
webview 分区里 auto→跟随系统变成「深色壳里白聊天」）。它必须只作用于**本窗口**，
且**只是初始默认、不是锁**——用户在被钉窗口里显式选档就接管本窗口主题（场景三）。

`localStorage('cp_theme')` 是**跨窗口共享的治理键**（后台日/夜滑块、外观面板
三态分段控件、服务端漫游都读写它）。把窗口级的钉写进这个共享键，就把「这一个
webview 要深色」升格成「整个 profile 都深色」，而且被钉窗口每次 applyTheme 都
会把它重新广播一遍 —— 于是同 profile 里另一个窗口刚点的日/夜在毫秒级被盖回。

## 为什么必须是浏览器门禁

事故表现是「后台切换白天晚上点了没反应」，而实际上：
  - 点击有响应，`theme_adm_flip` 埋点照常 +1；
  - `data-theme` 真的翻了，只是 **150ms 内被另一个标签页写回来**；
  - 单页探针一切正常（本仓已有的静态/单页门禁全绿）。
即缺陷只在「两个窗口 + 共享 localStorage」这个组合里存在，且三处读者
（workspace_base 头部 / unified_inbox 头部 / appearance.js）分散在模板与静态
资源里、全部**热更新直接上生产**。静态扫描能证明「没写 setItem」，证不了
「两个窗口互不打架」，所以钉行为本身要用真浏览器压住。

## 只读性

只开两个已登录标签页、点一次后台主题滑块、读 DOM/localStorage；不发消息、
不改服务端配置。收尾把三个主题键清回原样。

缺 playwright / 实例不可达 / 无 token 一律 SKIP exit 0（不污染回归信号）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

BASE = "http://127.0.0.1:18799"
DATA_ROOT = r"D:\chengjie-instances\zhiliao\data"

# 三个主题键：cp_theme=共享治理档，theme=后台旧键，ws_appearance_v1=引擎状态 blob。
THEME_KEYS = ("cp_theme", "theme", "ws_appearance_v1")

SNAP = """() => ({
  cp_attr: document.documentElement.getAttribute('data-cp-theme'),
  dt: document.documentElement.getAttribute('data-theme'),
  ls_cp: (() => { try { return localStorage.getItem('cp_theme'); } catch (e) { return 'X'; } })(),
  engine: typeof window.WSAppearance,
  night: (() => { try { return ((window.WSAppearance && window.WSAppearance.get().night) || {}).mode || null; }
                  catch (e) { return null; } })(),
  pin: window.__cpThemePin || null,
  css: (() => { var l = document.getElementById('cp-theme-css');
                return l ? (l.getAttribute('href') || '').split('?')[0] : null; })(),
})"""


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  [PASS] {name}{('  ' + detail) if detail else ''}")
        else:
            self.failed += [name]
            print(f"  [FAIL] {name}  {detail}")


def read_token(data_root: str) -> str:
    try:
        import yaml
    except Exception:
        return ""
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


def _clear_theme_keys(ctx: Any) -> None:
    pg = ctx.new_page()
    try:
        pg.goto(BASE + "/login", wait_until="domcontentloaded")
        pg.evaluate(
            "keys => { try { keys.forEach(k => localStorage.removeItem(k)); } catch (e) {} }",
            list(THEME_KEYS),
        )
    finally:
        pg.close()


def _seed_theme(ctx: Any, mode: str) -> None:
    """把共享治理档播成指定值。

    引擎 blob 必须带 `v:1`（否则 load 直接忽略）**和一个新鲜的 ts**（否则
    `pullSrv` 会拿服务端漫游里存着的旧档把种子盖掉 —— 本机服务端确实存着
    历次调试留下的 dark，首版种子就是这么被无声顶掉、险些把「漫游覆盖」
    误判成「钉泄漏」的）。
    """
    pg = ctx.new_page()
    try:
        pg.goto(BASE + "/login", wait_until="domcontentloaded")
        pg.evaluate(
            "m => { try { localStorage.setItem('cp_theme', m);"
            " localStorage.setItem('theme', m);"
            " localStorage.setItem('ws_appearance_v1', JSON.stringify("
            "   { v: 1, ts: Date.now() + 600000, night: { mode: m } })); }"
            " catch (e) {} }",
            mode,
        )
    finally:
        pg.close()


def _flip_admin_theme(ctx: Any, ck: Checks, label: str, ws_url: str,
                      assert_after: Callable[[dict, dict], None],
                      assert_a_load: Callable[[dict], None] | None = None) -> None:
    """开 A(工作台,可能带钉) + B(后台)，在 B 点一次主题滑块，读两边终态。"""
    print(f"\n-- {label} --")
    a = ctx.new_page()
    b = ctx.new_page()
    try:
        a.goto(BASE + ws_url, wait_until="domcontentloaded")
        a.wait_for_timeout(3500)          # 等 appearance.js defer 装载 + 首次 applyTheme
        a_load = a.evaluate(SNAP)
        print(f"     A(工作台) load  {a_load}")
        ck.ok(f"{label}: 工作台引擎装载", a_load.get("engine") == "object",
              f"engine={a_load.get('engine')}")
        if assert_a_load:
            assert_a_load(a_load)

        b.goto(BASE + "/", wait_until="domcontentloaded")
        b.wait_for_timeout(1500)
        before = b.evaluate(SNAP)
        btn = b.query_selector(".th-btn")
        if not btn:
            ck.ok(f"{label}: 后台有主题滑块", False, "找不到 .th-btn")
            return
        btn.click(timeout=3000)

        # 盖回是毫秒级的（改前实测 150ms 内），但也要证明它不会晚一点才回来。
        seen: list[dict] = []
        for wait in (150, 450, 1400, 3000):
            b.wait_for_timeout(wait)
            seen += [b.evaluate(SNAP)]
        after = seen[-1]
        print(f"     B(后台)   before {before}")
        for snap in seen:
            print(f"     B(后台)   after  {snap}")

        flipped = after.get("cp_attr") != before.get("cp_attr")
        ck.ok(f"{label}: 后台点击后主题真的翻转", flipped,
              f"{before.get('cp_attr')} -> {after.get('cp_attr')}")
        stable = len({s.get("cp_attr") for s in seen}) == 1
        ck.ok(f"{label}: 翻转后不被别的窗口盖回", stable,
              " -> ".join(str(s.get("cp_attr")) for s in seen))

        assert_after(after, a.evaluate(SNAP))
    finally:
        a.close()
        b.close()


def _explicit_pick_in_pinned_window(ctx: Any, ck: Checks) -> None:
    """场景三：被钉窗口里点用户菜单三档 —— 钉必须当场释放，画面真的跟着变。

    这条是 2026-08-16「亮色/暗色点了没反应」的行为侧回归网：改前 cp_theme 真写了、
    分段高亮真动了、埋点真 +1，唯独 data-cp-theme / theme-*.css 一动不动（eff() 钉
    优先）。所以断言必须落在**画面**（cp_attr + css 文件名），别落在存储值上。
    """
    print("\n-- 有钉窗口里显式选档（钉=默认不是锁）--")
    a = ctx.new_page()
    try:
        a.goto(BASE + "/workspace?theme=dark", wait_until="domcontentloaded")
        a.wait_for_timeout(3500)
        load = a.evaluate(SNAP)
        print(f"     A load   {load}")
        ck.ok("释放前: 钉在场且画面 dark",
              load.get("pin") == "dark" and load.get("cp_attr") == "dark",
              f"pin={load.get('pin')} cp_attr={load.get('cp_attr')}")

        btn = a.query_selector("#ws-user-btn")
        if not btn:
            ck.ok("有用户菜单入口 #ws-user-btn", False, "找不到")
            return
        btn.click(timeout=3000)
        a.wait_for_timeout(400)

        for mode, want in (("light", "light"), ("dark", "dark")):
            seg = a.query_selector(f"#ws-theme-seg button[data-mode='{mode}']")
            if not seg:
                ck.ok(f"有分段按钮 {mode}", False, "找不到")
                continue
            seg.click(timeout=3000)
            a.wait_for_timeout(1200)
            snap = a.evaluate(SNAP)
            print(f"     A after {mode}  {snap}")
            ck.ok(f"点「{mode}」画面真的变成 {want}", snap.get("cp_attr") == want,
                  f"cp_attr={snap.get('cp_attr')}")
            css = snap.get("css")
            ck.ok(f"点「{mode}」组件 css 跟着切", css is None or css.endswith(f"theme-{want}.css"),
                  f"css={css}")
            ck.ok(f"点「{mode}」后钉已释放", snap.get("pin") is None, f"pin={snap.get('pin')}")

        # 释放是**窗口级**的：同分区新开的被钉窗口必须仍然钉住 dark
        # （释放标记若落 localStorage，这里就会跟着变成用户刚选的档）。
        sib = ctx.new_page()
        try:
            sib.goto(BASE + "/workspace?theme=dark", wait_until="domcontentloaded")
            sib.wait_for_timeout(3000)
            s = sib.evaluate(SNAP)
            print(f"     兄弟窗口 {s}")
            ck.ok("释放只作用于本窗口（新开被钉窗口仍 dark）",
                  s.get("pin") == "dark" and s.get("cp_attr") == "dark",
                  f"pin={s.get('pin')} cp_attr={s.get('cp_attr')}")
        finally:
            sib.close()

        # 释放跨本窗口刷新存活（不然壳内一次导航就把用户的选择打回钉住档）
        a.reload(wait_until="domcontentloaded")
        a.wait_for_timeout(3000)
        r = a.evaluate(SNAP)
        print(f"     A reload {r}")
        ck.ok("释放跨本窗口刷新存活", r.get("pin") is None, f"pin={r.get('pin')}")
    finally:
        a.close()


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"[SKIP] playwright 不可用：{exc}")
        return 0

    token = read_token(DATA_ROOT)
    if not token:
        print(f"[SKIP] 读不到 web_admin.auth_token（{DATA_ROOT}）")
        return 0

    ck = Checks()
    with sync_playwright() as p:
        try:
            br = p.chromium.launch(headless=True)
        except Exception as exc:
            print(f"[SKIP] 启动 chromium 失败：{exc}")
            return 0
        try:
            ctx = br.new_context(viewport={"width": 1440, "height": 900})
            try:
                ctx.request.post(BASE + "/login", form={"auth_token": token})
            except Exception as exc:
                print(f"[SKIP] 实例不可达（{BASE}）：{exc}")
                return 0

            seed = ctx.new_page()
            try:
                seed.goto(BASE + "/login", wait_until="domcontentloaded")
            except Exception as exc:
                print(f"[SKIP] 实例不可达（{BASE}）：{exc}")
                return 0
            # 首访引导弹层会挡住一切点击（care 页门禁首跑就是这么被挡的）。
            seed.evaluate(
                "keys => { try { localStorage.setItem('_onboard_shown_simple','1');"
                " localStorage.setItem('tour_done','1');"
                " keys.forEach(k => localStorage.removeItem(k)); } catch (e) {} }",
                list(THEME_KEYS),
            )
            seed.close()

            # 场景一：工作台无钉 —— 共享治理档本就该跨窗口跟切（回归保护，别把钉修成「谁都不许跟」）。
            def _no_pin(after_b: dict, after_a: dict) -> None:
                ck.ok("无钉: 工作台跟随后台切换",
                      after_a.get("cp_attr") == after_b.get("cp_attr"),
                      f"A={after_a.get('cp_attr')} B={after_b.get('cp_attr')}")

            _flip_admin_theme(ctx, ck, "无钉", "/workspace", _no_pin)

            # 场景二：工作台 ?theme=dark（桌面壳 webview 口径）—— 钉守住自己那扇窗，
            # 但绝不把 dark 写进共享治理键去盖后台。
            # 先把治理档播成 light：这样钉(dark) 与治理(light) 在 A 装载时就不同，
            # 「钉有没有污染共享键」在**点击之前**即可独立判定（不依赖翻转是否成功，
            # 否则一旦回归成全 dark，这条断言会因两边同色而假绿）。
            _seed_theme(ctx, "light")

            def _pinned_load(a_load: dict) -> None:
                ck.ok("有钉: 装载即钉住 dark（本窗口）",
                      a_load.get("cp_attr") == "dark",
                      f"A.cp_attr={a_load.get('cp_attr')}")
                ck.ok("有钉: 装载不把钉写进共享治理键（应仍是 light）",
                      a_load.get("ls_cp") == "light",
                      f"cp_theme={a_load.get('ls_cp')}")

            def _pinned(after_b: dict, after_a: dict) -> None:
                ck.ok("有钉: 后台切换后被钉窗口自己仍是 dark",
                      after_a.get("cp_attr") == "dark",
                      f"A.cp_attr={after_a.get('cp_attr')}")
                ck.ok("有钉: 共享治理键=后台刚选的档",
                      after_a.get("ls_cp") == after_b.get("cp_attr"),
                      f"cp_theme={after_a.get('ls_cp')} B={after_b.get('cp_attr')}")

            _flip_admin_theme(ctx, ck, "有钉(?theme=dark)", "/workspace?theme=dark",
                              _pinned, assert_a_load=_pinned_load)

            # 场景三：被钉窗口里显式选档 —— 钉当场释放（2026-08-16 事故）。
            # 放在最后：它会给本 context 落下 sessionStorage 释放标记（页面级，
            # 但先跑会让上面两个场景的「钉在场」前提变味）。
            _explicit_pick_in_pinned_window(ctx, ck)

            _clear_theme_keys(ctx)
        finally:
            br.close()

    print(f"\n== URL 主题钉窗口隔离: {ck.passed}/{ck.passed + len(ck.failed)} PASS"
          + (f"  FAILED: {ck.failed}" if ck.failed else " ==")) 
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
