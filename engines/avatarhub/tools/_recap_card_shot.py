# -*- coding: utf-8 -*-
"""P9 战报分享卡实拍（非门禁·人工 QA 辅助，需 playwright + Hub 在线）。

为什么单独一个工具：战报卡是 _buildRecapCanvas() 的 canvas 产物，不进 DOM——
uivr 像素回归与 stream_state_shots 相位截图都拍不到它；字符串断言只能证明代码在，
证明不了"画出来长什么样"。本工具在无头浏览器里注入一场伪造战绩，调真实渲染管线
导出 PNG，海报带/趋势双轨/同网 QR 一图验收。

用法:
  python tools/_recap_card_shot.py                    # → %TEMP%/stream_states/recap_card.png
  python tools/_recap_card_shot.py --out D:/x.png --base http://127.0.0.1:9000
"""
import argparse
import base64
import sys
import tempfile
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9000")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "stream_states" / "recap_card.png"))
    args = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright 未装（pip install -r requirements/selfcheck.txt）"); return 2

    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 900})
        page.goto(args.base + "/ui?uivr=1#stream", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)   # 等 Alpine init + profiles 拉取（海报带要用真角色头像）
        data_url = page.evaluate("""async () => {
          const app = Alpine.$data(document.querySelector('[x-data]'));
          // 伪造一场 12 分半战绩：换脸统计 + 近场时延史 + 稳定度环形账本，锚定一张信息全满的卡
          app.lastSession = {durSec:754, peakFps:24, usedRvc:true, stabilityPct:96, endedTs:Date.now(),
            swap:{crop:{hit_pct:97}, latency_ms:{med:118}, degraded_pct:3, enhance:{GFPGAN:12}},
            swapHist:[{latency_ms:{med:112}},{latency_ms:{med:118}},{latency_ms:{med:135}},
                      {latency_ms:{med:128}},{latency_ms:{med:150}}]};
          try{ localStorage.setItem('hub_sess_hist', JSON.stringify(
            [{ts:1,stab:92},{ts:2,stab:95},{ts:3,stab:94},{ts:4,stab:96}])); }catch(e){}
          const c = await app._buildRecapCanvas();
          return c.toDataURL('image/png');
        }""")
        b.close()

    png = base64.b64decode(data_url.split(",", 1)[1])
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    print(f"OK: recap card {len(png)} bytes -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
