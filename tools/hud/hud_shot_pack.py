# -*- coding: utf-8 -*-
"""活体 HUD 营销截图包（P3 四期 · 2026-08-07，非门禁）。

对齐 theme_shot_pack 惯例：市场自助物料，demo 合成数据出图——
**内网 IP/真实告警零入镜**（导播墙 /wall 因空位二维码编码真实 LAN IP 明确排除，
沿用「壁纸退役 VPS/IP」的 VI 安全先例）。

产物：C:/模仿音色/logs/hud_marketing/<日期>/
  hud_table_4k.png / hud_cons_4k.png / hud_stream_4k.png / hud_events_4k.png   (3840×2160)
  hud_table.png / hud_cons.png / hud_stream.png / hud_events.png               (1920×1080)
  station_phone.png                                                            (390×844 手机端)
  index.html  对照页（深底网格 + 场景说明，发市场群直接看）

用法：python hud_shot_pack.py   （需活的 hud_server :7913 + 系统 Edge）
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

AVATARHUB = Path(r"C:\模仿音色")
OUT_ROOT = AVATARHUB / "logs" / "hud_marketing"
HUD = "http://127.0.0.1:7913/hud?machine=zhongshu&demo=1&gesture=0"
STATION = "http://127.0.0.1:7878/station?for=lianbei"
JOIN = "http://127.0.0.1:7913/join?for=yunsheng"

# (文件名, url, 宽, 高, 标题, 说明)
# 展演收官（2026-08-09）：营销包从「HUD 四视图」扩到「贵宾八分钟全场」——
# 覆盖 P0 欢迎/谢幕、P1 语音、P2 隔空指挥 money shot、P3 手机角色入口。
# demo 直达参数（tour/voicestate/opsstate）文案均无内网 IP（拍摄纪律）。
SHOTS = [
    # —— 展演 money shots（全场高光，4K）——
    ("show_welcome_4k", HUD + "&scene=ok&tour=welcome&visitor=贵宾示例", 3840, 2160,
     "开场 · 欢迎横幅", "六屏波纹接力 → 访客署名横幅 · 一个系统指挥六块屏的编排感"),
    ("show_ops_armed_4k", HUD + "&scene=ok&opsstate=armed", 3840, 2160,
     "隔空指挥 · 武装态（money shot）", "捏取换脸引擎拖到另一台 → 玫红武装卡 + 影响面 + 倒计时 · 两段式点火"),
    ("show_ops_fired_4k", HUD + "&scene=ok&opsstate=fired", 3840, 2160,
     "隔空指挥 · 点火成功", "路由热切生效 · 光点沿星座边流动 · 10s 撤销窗 · 操作即事件叙事"),
    ("show_voice_4k", HUD + "&scene=ok&voicestate=result", 3840, 2160,
     "语音指挥 · 「小界」", "说小界唤醒 → 聆听球 → 六屏字幕带同步 · 本地识别不上传"),
    ("show_curtain_4k", HUD + "&scene=ok&tour=curtain", 3840, 2160,
     "谢幕 · 品牌时刻", "全场缓降 → ∞ 无界主标呼吸 · 会听会看会说话的机房"),
    # —— HUD 四视图（信息主体，4K）——
    ("hud_table_4k", HUD + "&scene=warn", 3840, 2160, "六机实况 · 表格视图",
     "GPU 热度灯 · 秒级遥测 · 显存水位牌 · 忙态判定 · 事件流(含已确认降噪)"),
    ("hud_cons_4k", HUD + "&scene=ok&view=cons", 3840, 2160, "舰桥星座视图",
     "五边形集群拓扑 · 链路脉冲随算力 · 显存弧 · 手机外设卫星绕行(粉手势/金扬声器/紫魔杖)"),
    ("hud_stream_4k", HUD + "&scene=stream", 3840, 2160, "推流静默态",
     "直播中动画整层冻结 · 性能总督四闸之一 · 生产安全纪律可视化"),
    # —— 1080p 版（发布/PPT 用）——
    ("show_welcome", HUD + "&scene=ok&tour=welcome&visitor=贵宾示例", 1920, 1080, "开场欢迎(1080p)", ""),
    ("show_ops_armed", HUD + "&scene=ok&opsstate=armed", 1920, 1080, "隔空指挥武装(1080p)", ""),
    ("show_voice", HUD + "&scene=ok&voicestate=result", 1920, 1080, "语音指挥(1080p)", ""),
    ("hud_table", HUD + "&scene=warn", 1920, 1080, "六机实况(1080p)", ""),
    ("hud_cons", HUD + "&scene=ok&view=cons", 1920, 1080, "舰桥星座(1080p)", ""),
    # —— 手机端（竖屏）——
    ("join_phone", JOIN, 390, 844, "手机角色入口 /join",
     "扫码 → 选机器 → 五角色卡(手势站/扬声器/魔杖/监看/遥控) · 一机=一器官 · 角色即租约"),
    ("station_phone", STATION, 390, 844, "手机手势站入列页",
     "扫码 → 选站位 → 推流入列 · 零安装 · WakeLock 防息屏 · 隐私声明"),
]

EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


def find_edge() -> Path | None:
    for p in EDGE_CANDIDATES:
        if p.is_file():
            return p
    return None


def shoot(edge: Path, url: str, w: int, h: int, out: Path) -> bool:
    # 4K 出图 = 1080p 版式 × device-scale-factor 2（theme_shot_pack 的「2x 高清实拍」同款：
    # 面板占幅与 1080p 一致、像素密度翻倍；直接开 4K 视口会让固定宽版式缩成一角）
    scale = 2 if w >= 3000 else 1
    vw, vh = w // scale, h // scale
    cmd = [str(edge), "--headless=new", "--disable-gpu", "--hide-scrollbars",
           f"--window-size={vw},{vh}", f"--force-device-scale-factor={scale}",
           "--virtual-time-budget=5500", f"--screenshot={out}", url]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90)
        return out.is_file() and out.stat().st_size > 30_000
    except Exception:
        return False


def write_index(out_dir: Path, done: list[tuple], day: str) -> None:
    cards = "\n".join(
        f'<div class="c"><img src="{name}.png" loading="lazy">'
        f'<div class="t">{title}</div><div class="s">{sub or "&nbsp;"}</div></div>'
        for name, title, sub in done)
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>活体 HUD 营销截图包 · {day}</title>
<style>
body{{background:#05060F;color:#EEF0F8;font-family:"Noto Sans SC","Microsoft YaHei",sans-serif;
padding:34px 40px}}
h1{{font-size:22px;font-weight:900;background:linear-gradient(90deg,#22D3EE,#8B5CF6);
-webkit-background-clip:text;background-clip:text;color:transparent}}
.m{{color:#969CB0;font-size:12.5px;margin:6px 0 24px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:22px}}
.c{{background:#11132A;border:1px solid rgba(139,92,246,.3);border-radius:14px;overflow:hidden}}
.c img{{width:100%;display:block;background:#000}}
.t{{font-size:15px;font-weight:700;padding:10px 14px 2px}}
.s{{font-size:12px;color:#969CB0;padding:0 14px 12px;line-height:1.6}}
</style></head><body>
<h1>集群实况 · 活体 HUD 营销截图包</h1>
<div class="m">{day} · demo 合成数据（内网 IP/真实告警零入镜）· 4K 原图点开即用 ·
导播墙因二维码含内网地址按 VI 安全纪律不入包</div>
<div class="g">{cards}</div></body></html>"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    edge = find_edge()
    if not edge:
        print("Edge 不在标准路径")
        return 1
    day = datetime.now().strftime("%Y%m%d")
    out_dir = OUT_ROOT / day
    out_dir.mkdir(parents=True, exist_ok=True)
    done, fails = [], 0
    for name, url, w, h, title, sub in SHOTS:
        out = out_dir / f"{name}.png"
        if shoot(edge, url, w, h, out):
            done.append((name, title, sub))
            print(f"OK   {name} ({w}x{h}, {out.stat().st_size // 1024}KB)")
        else:
            fails += 1
            print(f"FAIL {name}")
        time.sleep(0.3)
    write_index(out_dir, done, day)
    print(f"== hud_shot_pack: {len(done)}/{len(SHOTS)}，index -> {out_dir / 'index.html'} ==")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
