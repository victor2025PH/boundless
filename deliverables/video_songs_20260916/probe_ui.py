#!/usr/bin/env python3
"""探针：导出工作台/人设/知识库关键控件真实选择器，供修 curriculum。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from chatx_session import BASE, session

OUT = Path(__file__).resolve().parent / "out" / "probe"
OUT.mkdir(parents=True, exist_ok=True)

JS = r"""() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'
      && r.bottom > 0 && r.top < innerHeight;
  };
  const txt = (el) => ((el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ')).slice(0, 90);
  const buttons = [...document.querySelectorAll('button,a,[role=button],.btn,.chip,.pill')].filter(visible)
    .map(el => ({t: txt(el), id: el.id || '', cls: (el.className || '').toString().slice(0, 100), tag: el.tagName}))
    .filter(x => x.t).slice(0, 160);
  const tabs = [...document.querySelectorAll(
    '[class*=tab],[class*=acct],[class*=account],[data-acct],[data-account],.ftab,.ws-acct,[class*=Acc]')
  ].filter(visible).map(el => ({
    t: txt(el), id: el.id || '', cls: (el.className || '').toString().slice(0, 120),
    dk: el.getAttribute('data-key') || el.getAttribute('data-acct') || el.getAttribute('data-account') || ''
  })).slice(0, 100);
  const fabs = [...document.querySelectorAll(
    '[id*=zhi],[id*=assist],[id*=fab],[id*=help],[class*=xiaozhi],[class*=assist],[class*=fab],[class*=ball],[class*=float]'
  )].map(el => ({
    t: txt(el), id: el.id || '', cls: (el.className || '').toString().slice(0, 120), tag: el.tagName,
    rect: (() => { const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; })()
  })).slice(0, 50);
  const re = /手动|半自动|全自动|本会话|接管|翻译|导入|新增|风险|小智|上传|知识库|新建/;
  const modes = [...document.querySelectorAll('button,a,span,div,label,[role=button]')].filter(el => {
    const t = txt(el); return t && re.test(t) && visible(el) && t.length < 80;
  }).slice(0, 80).map(el => ({
    t: txt(el), tag: el.tagName, id: el.id || '', cls: (el.className || '').toString().slice(0, 100)
  }));
  const header = (document.querySelector('#chat-header,.chat-header,[class*=chat-header]') || {}).innerText || '';
  return {url: location.href, header: String(header).slice(0, 200), buttons, tabs, fabs, modes};
}"""


def dump(page, name: str) -> dict:
    info = page.evaluate(JS)
    (OUT / f"{name}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"== {name} {info['url']}")
    print(" header:", (info.get("header") or "")[:120].replace("\n", " | "))
    kw = re.compile(r"新增|翻译|接管|导入|风险|手动|半自动|全自动|小智|上传|新建|全部账号|\+")
    print(" btn hits:", [b for b in info["buttons"] if kw.search(b["t"])][:25])
    print(" tab sample:", [
        {"t": t["t"][:40], "cls": t["cls"][:50], "dk": t["dk"][:40]} for t in info["tabs"][:20]
    ])
    print(" fabs:", info["fabs"][:12])
    print(" modes:", info["modes"][:30])
    return info


def main() -> int:
    with session() as (page, _):
        page.wait_for_timeout(2000)
        try:
            el = page.get_by_text("跳过引导", exact=False)
            if el.count() and el.first.is_visible():
                el.first.click(timeout=2000)
                page.wait_for_timeout(600)
        except Exception:
            pass
        dump(page, "ws_home")

        cid = "telegram:8244899900:8506426282"
        page.evaluate(
            """(cid) => {
              if (typeof window.__wsFocusConv === 'function') {
                try { window.__wsFocusConv(cid); } catch (e) {}
              }
            }""",
            cid,
        )
        page.wait_for_timeout(1500)
        loc = page.locator(f'.conv-item[data-key="{cid}"]')
        if loc.count():
            loc.first.click(timeout=4000)
            page.wait_for_timeout(2800)
        dump(page, "ws_boundless")

        for path, name in [("/personas", "personas"), ("/knowledge", "knowledge"), ("/pricing", "pricing")]:
            page.goto(BASE + path, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2800)
            dump(page, name)
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
