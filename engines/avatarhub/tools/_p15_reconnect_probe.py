# -*- coding: utf-8 -*-
"""P15-4 弱网演练探针：观众墙实时通道 断链→贴片→恢复→toast 全生命周期自动化。

P14-5 给 wall.html 加了断连可感知 UI（#reChip 重连贴片 / #reToast 恢复提示），
但只有静态字符串契约在守——"断线真的会弹、恢复真的会收"从没被机器验证过。
本探针用真浏览器做一次确定性弱网演练（不碰 Hub 进程，杀的是页面这端的连接）：

  1. 开 /wall（不带 uivr），等 WS 首连成功（_wsWasUp=true），断言贴片隐藏；
  2. context.set_offline(true) + 页内 _ws.close() 双保险切断 → 断言贴片出现、
     轮询兜底已接管（_pollTimer 非空）；
  3. set_offline(false) → scheduleReconnect（3s 锚）自动重连 → 断言 WS 回到 OPEN、
     贴片收起、恢复 toast 在 2.6s 展示窗内被捕获；
  4. ?uivr=1 再演练一遍断链 → 断言贴片始终不出现（像素回归确定性豁免真的生效）；
  5. 全程零 pageerror；贴片/toast 各截图留证。

用法：python tools/_p15_reconnect_probe.py [--base http://127.0.0.1:9000]
退出码：0=通过 1=断言失败 2=跳过（Hub 不可达 / playwright 缺失）。
"""
import argparse
import sys
import tempfile
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("SKIP: playwright 未安装")
    sys.exit(2)

OUT = Path(tempfile.gettempdir()) / "stream_states"
OUT.mkdir(parents=True, exist_ok=True)


def _style(pg, el_id):
    return pg.evaluate(f"document.getElementById('{el_id}').style.display")


def _wait(pg, expr, timeout_ms=8000, step_ms=150):
    """轮询页内表达式直到真值；超时返回 False。比单次长等更快、比 wait_for_function 可控。"""
    waited = 0
    while waited <= timeout_ms:
        try:
            if pg.evaluate(expr):
                return True
        except Exception:
            pass
        pg.wait_for_timeout(step_ms)
        waited += step_ms
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9000")
    args = ap.parse_args()
    try:
        urllib.request.urlopen(args.base.rstrip("/") + "/health", timeout=5)
    except Exception as e:
        print(f"SKIP: Hub 不可达 {args.base}（{e}）")
        return 2

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1280, "height": 800})
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        # 1) 首连：WS 上线、贴片隐藏
        pg.goto(args.base + "/wall", wait_until="domcontentloaded")
        assert _wait(pg, "typeof _wsWasUp!=='undefined' && _wsWasUp===true", 10000), \
            "WS 首连未成功（_wsWasUp 未置位）"
        assert _style(pg, "reChip") in ("", "none"), "初始态贴片不应显示"

        # 2) 断链：离线仿真 + 页内主动断开双保险（offline 对已建 WS 的切断因内核版本而异）
        ctx.set_offline(True)
        pg.evaluate("(()=>{ if(_ws) _ws.close(); return true; })()")   # WS 对象不可序列化，包一层
        assert _wait(pg, "document.getElementById('reChip').style.display==='flex'", 4000), \
            "断链后 4s 内重连贴片未出现"
        assert pg.evaluate("_pollTimer!==null"), "断链后轮询兜底未接管（_pollTimer 为空）"
        pg.screenshot(path=str(OUT / "wall_rechip.png"))

        # 3) 恢复：scheduleReconnect 3s 锚自动重连 → 贴片收、toast 现（2.6s 展示窗）
        ctx.set_offline(False)
        assert _wait(pg, "_ws && _ws.readyState===1", 12000), "恢复网络后 12s 内 WS 未重连"
        assert _wait(pg, "document.getElementById('reChip').style.display==='none'", 2000), \
            "重连成功后贴片未收起"
        assert _wait(pg, "document.getElementById('reToast').style.display==='block'", 2500), \
            "重连成功后未见恢复 toast（2.6s 展示窗内未捕获）"
        pg.screenshot(path=str(OUT / "wall_retoast.png"))
        assert _wait(pg, "document.getElementById('reToast').style.display==='none'", 4000), \
            "恢复 toast 未按时自动消失"

        # 4) uivr=1：同样断链，贴片必须全程静默（视觉回归确定性豁免）
        pg2 = ctx.new_page()
        pg2.on("pageerror", lambda e: errs.append("uivr:" + str(e)))
        pg2.goto(args.base + "/wall?uivr=1", wait_until="domcontentloaded")
        assert _wait(pg2, "typeof _wsWasUp!=='undefined' && _wsWasUp===true", 10000), \
            "uivr 页 WS 首连未成功"
        ctx.set_offline(True)
        pg2.evaluate("(()=>{ if(_ws) _ws.close(); return true; })()")
        pg2.wait_for_timeout(1500)
        assert _style(pg2, "reChip") in ("", "none"), "uivr=1 下断链贴片仍出现（应被抑制）"
        ctx.set_offline(False)

        assert not errs, f"JS 错误: {errs}"
        b.close()
    print("OK: 断链→贴片→轮询兜底→重连→toast→uivr抑制 全生命周期通过，无 JS 错误")
    print(f"截图: {OUT / 'wall_rechip.png'} · {OUT / 'wall_retoast.png'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
