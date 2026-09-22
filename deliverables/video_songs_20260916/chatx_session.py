#!/usr/bin/env python3
"""本机智聊（117 生产实例，127.0.0.1:18799）的录屏会话底座。

- 登录：与桌面壳同一条路（POST /login auth_token + X-ChatX-Auto-Login），token 读桌面壳 config.json，不入库。
- 录屏：playwright chromium 1920×1080 record_video；每段动作一个 clip。
- **安全默认**：只浏览/翻译/拟稿/试听，不发送；`allow_send=True` 且会话名命中 DEMO_PEERS 白名单才允许真发。
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

BASE = "http://127.0.0.1:18799"
CFG = Path(os.environ["APPDATA"]) / "telegram-ai-desktop" / "config.json"
# 会话头/列表名命中任一关键字，且录屏带 --allow-send，才允许真发
DEMO_PEERS = (
    "演示", "BOUNDLESS", "demo", "测试", "test",
    "智聊支持", "ZH_DEMO", "教程",
)


def token() -> str:
    return json.loads(CFG.read_text(encoding="utf-8"))["backend"]["token"]


#: 录制窗口的主题钉（"dark" / "light" / ""）。session() 按 dark 参数设置；所有站内导航走 url()。
#: 工作台主题是坐席级漫游偏好（/api/workspace/prefs appearance.night.mode，任何浏览器改一下全端跟着变）：
#: 2026-09-18 20:08 P1 重录整段白底——19:50 有人在自己浏览器把工作台切成亮色，录制会话是新 context 也
#: 被漫游拉成亮色，成片与已上架 7 集（全深色）风格断裂。?theme= 是产品给嵌入端的窗口级钉：只钉本窗口、
#: 不写 localStorage、不碰坐席偏好；用户显式选过档（cp_theme_user_set）时钉自动失效，但录制 context 全新，
#: 不会带该标记。
THEME = {"pin": ""}


def url(path: str) -> str:
    """站内地址 + 主题钉。path 已带 query 时用 & 续；已含 theme= 不重复。"""
    u = BASE + path
    pin = THEME.get("pin") or ""
    if pin and "theme=" not in path:
        u += ("&" if "?" in path else "?") + "theme=" + pin
    return u


@contextmanager
def session(record_dir: Path | None = None, *, w: int = 1920, h: int = 1080, dark: bool = True):
    from playwright.sync_api import sync_playwright
    THEME["pin"] = "dark" if dark else "light"
    with sync_playwright() as p:
        b = p.chromium.launch()
        kw = dict(viewport={"width": w, "height": h}, locale="zh-CN",
                  color_scheme="dark" if dark else "light")
        if record_dir:
            record_dir.mkdir(parents=True, exist_ok=True)
            kw.update(record_video_dir=str(record_dir), record_video_size={"width": w, "height": h})
        ctx = b.new_context(**kw)
        page = ctx.new_page()
        page.t_created = time.monotonic()  # 录像从建页起算；调用方据此算「工作台就绪」偏移，拼装时裁掉登录/连接头
        page.goto(BASE + "/login", wait_until="domcontentloaded", timeout=60000)
        page.evaluate(
            """async (tok) => { await fetch('/login', {method:'POST',
                 headers:{'Content-Type':'application/x-www-form-urlencoded','X-ChatX-Auto-Login':'1'},
                 body:'auth_token='+encodeURIComponent(tok)+'&next=%2Fworkspace', credentials:'same-origin'}); }""",
            token())
        # 工作台常驻 SSE/轮询，networkidle 永不触发（首探 90s 超时实锤）→ domcontentloaded + 定长稍等
        page.goto(url("/workspace"), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if "/login" in page.url:
            raise SystemExit("[FAIL] token 登录失败，仍在 /login")
        try:
            eff = page.evaluate("() => document.documentElement.getAttribute('data-cp-theme')")
            if eff and eff != THEME["pin"]:
                print(f"    ! WARN 工作台主题 {eff} ≠ 钉 {THEME['pin']}（成片会与已上架集风格断裂）")
        except Exception:
            pass
        try:
            yield page, ctx
        finally:
            ctx.close()
            b.close()


def latest_video(record_dir: Path, target: Path) -> Path:
    vids = sorted(record_dir.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    if not vids:
        raise SystemExit("[FAIL] 无录屏产物")
    if target.exists():
        target.unlink()
    vids[-1].rename(target)
    return target


def can_send(peer_name: str, allow_send: bool) -> bool:
    return bool(allow_send) and any(k.lower() in (peer_name or "").lower() for k in DEMO_PEERS)


if __name__ == "__main__":  # 探针：登录 + 截图 + 列出可见会话名与顶栏入口
    out = Path(__file__).resolve().parent / "out" / "probe"
    out.mkdir(parents=True, exist_ok=True)
    with session() as (page, _):
        page.wait_for_timeout(3000)
        page.screenshot(path=str(out / "workspace.png"))
        info = page.evaluate("""() => {
          const txt = (sel) => Array.from(document.querySelectorAll(sel)).map(e => (e.innerText||'').trim()).filter(Boolean);
          return {title: document.title, url: location.href,
                  nav: txt('nav a, header a, [role=tab], .tab, .tabs button').slice(0, 60),
                  buttons: txt('button').slice(0, 80)};
        }""")
        print(json.dumps(info, ensure_ascii=False, indent=1)[:6000])
        print("shot:", out / "workspace.png")
