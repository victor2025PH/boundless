# -*- coding: utf-8 -*-
"""右栏「AI 生成图片」cp-image 真浏览器门禁（Playwright；2026-08-28 P0 收口配套）。

**为什么需要它**：当天三处报障全都是「静态门禁看得见字符串、看不见行为」的类型——

  1. 相册缩略图 4 张全裂：后端 URL 拼错（`album-file?path=` 的路径消毒只认
     `provider.album_dir`，而相册面板上传的图落在 `static/persona_albums`），
     前端又没有 onerror，于是裂图框旁边照旧写着「相册已有 4 张，点选可直接发送」。
     后端有单测能钉 URL，但「图取不到时 UI 长什么样」只有真浏览器能压。
  2. 「出图卡当前较忙」永久误报：判据是显存占用而非队列，且它是否出现取决于
     **所选引擎的闸门**（qwen_edit 18 / flux_pulid 14）——切个下拉就该变，
     这是纯前端交互。
  3. 字看不清：颜色在 token 里（对比度门禁管得住），字号却写死在组件 styles()
     里。真渲染的 computed font-size 才是坐席眼睛看到的那个数。

**夹具模式**（与 tools/verify_cp_voice_ui.py / verify_goal_form_ui.py 同族）：
file:// 自包含页 + 真组件（cp-panel-base + cp-image）+ 真词典（CP_LANG 钉 zh）+
真主题（tokens.css + theme-dark.css，字号/颜色几何级可测）+ **stub client**
——所有 IO 走注入对象，零实例依赖、零 GPU 消耗、零生产写入。

覆盖的不变量（编号对应 run() 里的断言）：
  G1  初始渲染：表单成形（人设/引擎/生成按钮）；算力充足且空队列 → **零预警条**
  G2  排队：queue_pending>0 → 中性知情条（.im-note）而非琥珀警示（.im-warn）
  G3  预热：显存低于所选引擎闸门 → 「冷启动」中性条
  G4  引擎感知：free=16 时 flux_pulid(14) 不提示、切 qwen_edit(18) 才提示，
      切回即消失——旧代码写死 14，这条会红
  G5  面板可见文案零内部实现词（显存/算力/腾挪/GPU/VRAM）
  G6  旧后端兼容：cfg 无 busy_signal → 一条预警都不出（宁可不说，不说错）
  G7  服务级故障仍要红：全引擎未部署 → .im-warn.crit + 生成按钮禁用
  G8  存货缩略图：3 张全好 → 3 张图 + 文案说「3 张」
  G9  坏图摘除：1 张取不到 → 剩 2 张，文案改口说「2 张」（诚实计数）
  G10 全裂：整块不渲染（无裂图框、无「N 张」宣称）+ 上报一次遥测
  G11 config 续期不重渲表单：refresh 后 imageConfig 再被调用，而输入框内容不丢
  G12 字号地板：说明文字（额度/页脚提示/存货行）computed font-size ≥ 12px

用法::

    python tools/verify_cp_image_ui.py            # 门禁模式
    python tools/verify_cp_image_ui.py --headed   # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]

# 1x1 PNG（能 load）与故意损坏的 data URI（必 error）——用 data: 而非 file:/http:
# 是为了确定性：不看磁盘、不看网络，error 事件立刻到。
GOOD_IMG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
BAD_IMG = "data:image/png;base64,!!!!not-a-png!!!!"

_HTML = """<!doctype html>
<html data-cp-theme="dark"><head><meta charset="utf-8"><title>cp-image probe</title>
<link rel="stylesheet" href="@@TOKENS_CSS@@">
<link rel="stylesheet" href="@@DARK_CSS@@">
</head>
<body style="margin:16px;background:var(--cp-bg,#141519);max-width:380px;">
<script>window.CP_LANG = 'zh';</script>
<script src="@@I18N_JS@@"></script>
<script>
window.__beacons = [];
try {
  Object.defineProperty(navigator, 'sendBeacon', {
    value: function (u) { window.__beacons.push(String(u)); return true; },
    configurable: true, writable: true });
} catch (_e) { /* 某些环境不可覆写：G10 的 beacon 断言会如实红 */ }

window.GOOD_IMG = '@@GOOD_IMG@@';
window.BAD_IMG  = '@@BAD_IMG@@';
window.__calls = { config: 0, stock: 0 };
window.__cfg = {
  ok: true, enabled: true,
  engines: ['flux_pulid', 'qwen_edit', 'z_image'],
  engines_info: [
    { id: 'flux_pulid', deployed: true, min_free_gb: 14 },
    { id: 'qwen_edit',  deployed: true, min_free_gb: 18 },
    { id: 'z_image',    deployed: true, min_free_gb: 14 }],
  comfy_ok: true, default_engine: 'flux_pulid',
  personas: [{ id: 'lin', name: '林小雨', has_face_ref: true }],
  default_persona: 'lin',
  jobs_api: true, album_pick: true, scene_hints: false,
  vram_free_gb: 24, queue_pending: 0, busy_jobs: 0, busy_signal: true,
  daily_quota: 20, quota_used: 0,
};
window.__stock = [];
window.__stubClient = {
  async imageConfig() {
    window.__calls.config++;
    return JSON.parse(JSON.stringify(window.__cfg));
  },
  async imageAlbumStock() {
    window.__calls.stock++;
    return { ok: true, items: JSON.parse(JSON.stringify(window.__stock)) };
  },
};
</script>
<script src="@@BASE_JS@@"></script>
<script src="@@IMAGE_JS@@"></script>
<script>
const el = document.createElement('cp-image');
el.client = window.__stubClient;           // 必须先赋 client：connectedCallback 即取配置
document.body.appendChild(el);
el.context = { platform: 'telegram', accountId: 'acc1', chatKey: 'peer1',
               conversationId: 'telegram:acc1:peer1' };
window.__el = el;
window.q  = (s) => window.__el.shadowRoot.querySelector(s);
window.qa = (s) => Array.from(window.__el.shadowRoot.querySelectorAll(s));

window.visible = (e) => !!e && !e.hidden
  && getComputedStyle(e).display !== 'none'
  && e.getBoundingClientRect().height > 0;

// 改配置后强制续期（TTL 60s 在门禁里没意义）：清时间戳再走组件自己的续期路径
window.recfg = async () => {
  window.__el._cfgTs = 0;
  await window.__el.refresh();
  await new Promise((r) => setTimeout(r, 120));
};
window.setEngine = (v) => {
  const s = window.q('[data-role="engine"]');
  s.value = v;
  s.dispatchEvent(new Event('change', { bubbles: true }));
};
window.setStock = async (urls) => {
  window.__stock = urls.map((u, i) => ({
    media_id: 'm' + i, path: 'D:/x/' + i + '.png', scene_class: 'cafe',
    caption: '', url: u }));
  window.__el._loadStock();
  await new Promise((r) => setTimeout(r, 900));   // debounce 400 + 取数 + 图片 load/error
};
window.px = (sel) => {
  const e = window.q(sel);
  return e ? Math.round(parseFloat(getComputedStyle(e).fontSize)) : -1;
};
window.snap = () => {
  const sr = window.__el.shadowRoot;
  const notes = window.qa('.im-note').filter(window.visible);
  const warns = window.qa('.im-warn').filter(window.visible);
  const stock = window.q('.im-stock');
  const line = window.q('[data-role="stockline"]');
  const gen = window.q('[data-act="gen"]');
  return {
    hasPersonaSel: !!window.q('[data-role="persona"]'),
    hasEngineSel: !!window.q('[data-role="engine"]'),
    hasGenBtn: !!gen, genDisabled: !!(gen && gen.disabled),
    noteCount: notes.length,
    noteText: notes.map((e) => e.textContent).join(' | '),
    warnCount: warns.length,
    warnCrit: warns.some((e) => e.classList.contains('crit')),
    stockPresent: !!stock,
    stockThumbs: window.qa('.im-stock-row img').length,
    stockLine: line ? line.textContent : '',
    promptVal: (window.q('[data-role="prompt"]') || {}).value || '',
    quotaText: (window.q('[data-role="quota"]') || {}).textContent || '',
    text: (sr.querySelector('.wrap') || {}).textContent || '',
    cfgCalls: window.__calls.config,
    beacons: window.__beacons.length,
  };
};
</script>
</body></html>
"""


def build_fixture_page(tmp: Path) -> Path:
    def uri(rel: str) -> str:
        return (ENGINE / rel).as_uri()

    html = (_HTML
            .replace("@@TOKENS_CSS@@", uri("shared/copilot/tokens.css"))
            .replace("@@DARK_CSS@@", uri("shared/copilot/theme-dark.css"))
            .replace("@@I18N_JS@@", uri("shared/copilot/i18n/cp-i18n.js"))
            .replace("@@BASE_JS@@", uri("shared/copilot/components/cp-panel-base.js"))
            .replace("@@IMAGE_JS@@", uri("shared/copilot/components/cp-image.js"))
            .replace("@@GOOD_IMG@@", GOOD_IMG)
            .replace("@@BAD_IMG@@", BAD_IMG))
    fp = tmp / "probe_image.html"
    fp.write_text(html, encoding="utf-8")
    return fp


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
        print(f"\n== cp-image 面板验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


# 内部实现词：坐席无从处置 + 白标/演示露馅（与 tests/test_cp_app_i18n_keys.py
# 的静态 ratchet 同一口径，这里查的是**真渲染出来的可见文本**）。
_JARGON = ("显存", "算力", "腾挪", "GPU", "VRAM", "vram")


def run(page, ck: Checker) -> None:
    ev = page.evaluate

    # G1 初始渲染：算力充足 + 空队列 → 零预警
    s = ev("snap()")
    ck.check("G1 表单成形（人设/引擎/生成按钮）",
             s["hasPersonaSel"] and s["hasEngineSel"] and s["hasGenBtn"])
    ck.check("G1 空闲充足+空队列 → 零预警条",
             s["noteCount"] == 0 and s["warnCount"] == 0,
             f"note={s['noteCount']} warn={s['warnCount']} :: {s['noteText']}")

    # G2 排队 → 中性知情条（不是琥珀警示）
    ev("() => { window.__cfg.queue_pending = 2; }")
    ev("recfg()")
    s = ev("snap()")
    ck.check("G2 排队出中性条且文案含张数",
             s["noteCount"] == 1 and "2" in s["noteText"] and "排队" in s["noteText"],
             s["noteText"])
    ck.check("G2 排队不用琥珀警示样式（告警疲劳）", s["warnCount"] == 0)

    # G3 预热：显存低于所选引擎闸门
    ev("() => { window.__cfg.queue_pending = 0; window.__cfg.vram_free_gb = 10; }")
    ev("recfg()")
    s = ev("snap()")
    ck.check("G3 低于闸门 → 冷启动中性条",
             s["noteCount"] == 1 and "冷启动" in s["noteText"], s["noteText"])

    # G4 引擎感知（旧代码写死 14 → 这条必红）
    ev("() => { window.__cfg.vram_free_gb = 16; }")
    ev("recfg()")
    s = ev("snap()")
    ck.check("G4 free=16 时 flux_pulid(闸门14) 不提示", s["noteCount"] == 0, s["noteText"])
    ev("() => setEngine('qwen_edit')")
    s = ev("snap()")
    ck.check("G4 切 qwen_edit(闸门18) → 提示出现",
             s["noteCount"] == 1 and "冷启动" in s["noteText"], s["noteText"])
    ev("() => setEngine('flux_pulid')")
    s = ev("snap()")
    ck.check("G4 切回 flux_pulid → 提示消失", s["noteCount"] == 0)

    # G5 可见文案零内部词（此时显存 16 < 18 的路径刚走过，最容易漏词）
    ev("() => setEngine('qwen_edit')")
    s = ev("snap()")
    leaked = [w for w in _JARGON if w in s["text"]]
    ck.check("G5 面板可见文案零内部实现词", not leaked, f"leaked={leaked}")
    ev("() => setEngine('flux_pulid')")

    # G6 旧后端（无 busy_signal）→ 一条都不出
    ev("() => { delete window.__cfg.busy_signal; window.__cfg.vram_free_gb = 1; }")
    ev("recfg()")
    s = ev("snap()")
    ck.check("G6 旧后端缺 busy_signal → 零预警（宁可不说，不说错）",
             s["noteCount"] == 0 and s["warnCount"] == 0, s["noteText"])
    ev("() => { window.__cfg.busy_signal = true; window.__cfg.vram_free_gb = 24; }")
    ev("recfg()")

    # G7 服务级故障仍必须红（预警分层没被削平）
    ev("""() => {
      window.__cfg.engines_info = window.__cfg.engines_info.map(
        (x) => Object.assign({}, x, { deployed: false }));
    }""")
    ev("recfg()")
    s = ev("snap()")
    ck.check("G7 全引擎未部署 → 红条 + 生成禁用",
             s["warnCrit"] and s["genDisabled"],
             f"crit={s['warnCrit']} disabled={s['genDisabled']}")
    ev("""() => {
      window.__cfg.engines_info = window.__cfg.engines_info.map(
        (x) => Object.assign({}, x, { deployed: true }));
    }""")
    ev("recfg()")

    # G8/G9/G10 存货缩略图三态
    ev("() => setStock([window.GOOD_IMG, window.GOOD_IMG, window.GOOD_IMG])")
    s = ev("snap()")
    ck.check("G8 3 张全好 → 3 张图且文案说 3",
             s["stockPresent"] and s["stockThumbs"] == 3 and "3" in s["stockLine"],
             f"thumbs={s['stockThumbs']} line={s['stockLine']}")

    ev("() => setStock([window.GOOD_IMG, window.BAD_IMG, window.GOOD_IMG])")
    s = ev("snap()")
    ck.check("G9 1 张取不到 → 摘掉坏图（剩 2 张，无裂图框）",
             s["stockThumbs"] == 2, f"thumbs={s['stockThumbs']}")
    ck.check("G9 张数诚实改口说 2（不再宣称 3 张可发）",
             "2" in s["stockLine"] and "3" not in s["stockLine"], s["stockLine"])

    before = ev("() => window.__beacons.length")
    ev("() => setStock([window.BAD_IMG, window.BAD_IMG])")
    s = ev("snap()")
    ck.check("G10 全裂 → 整块不渲染（无裂图框、无 N 张宣称）",
             (not s["stockPresent"]) and s["stockThumbs"] == 0 and s["stockLine"] == "",
             f"present={s['stockPresent']} thumbs={s['stockThumbs']}")
    ck.check("G10 全裂上报一次遥测（坏了不必等坐席报障）",
             s["beacons"] > before, f"{before} -> {s['beacons']}")

    # G11 config 续期不重渲表单（坐席打了一半的提示词不许被吞）
    ev("""() => {
      const t = window.q('[data-role="prompt"]');
      t.value = '站在窗边看夜景';
      t.dispatchEvent(new Event('input', { bubbles: true }));
    }""")
    n0 = ev("() => window.__calls.config")
    ev("() => { window.__cfg.quota_used = 7; }")
    ev("recfg()")
    s = ev("snap()")
    ck.check("G11 续期真的重取了配置", s["cfgCalls"] > n0, f"{n0} -> {s['cfgCalls']}")
    ck.check("G11 额度行随之刷新（不再冻结在打开那一刻）",
             "7" in s["quotaText"], s["quotaText"].strip())
    ck.check("G11 续期不重渲表单（输入内容保留）",
             s["promptVal"] == "站在窗边看夜景", s["promptVal"])

    # G12 字号地板（token 真生效的几何级验证）
    for sel, label in ((".im-hint", "页脚提示/额度行"),
                       ("[data-role=\"quota\"] .im-hint", "今日额度行")):
        v = ev(f"() => px({sel!r})")
        if v < 0:
            continue
        ck.check(f"G12 {label} computed 字号 ≥12px", v >= 12, f"{v}px")
    ev("() => setStock([window.GOOD_IMG])")
    v = ev("() => px('.im-stock-line')")
    ck.check("G12 存货说明行 computed 字号 ≥12px", v >= 12, f"{v}px")

    # ── 探测器自证：把组件运行时退回「修复前」的行为，断言上面的判据真的会红。
    #    （只覆盖实例自身属性，不动磁盘文件——生产热更新树上零风险。）
    print("  -- 探测器自证（把组件退回修复前，断言判据有鉴别力）--")

    # G13 摘掉 onerror 接线 = 2026-08-28 之前的形态：坏图应当残留成裂图框
    ev("() => { window.__el._wireStockFallback = function () {}; }")
    ev("() => setStock([window.GOOD_IMG, window.BAD_IMG, window.GOOD_IMG])")
    s = ev("snap()")
    ck.check("G13 自证：去掉 onerror 后坏图残留（G9 判据有鉴别力）",
             s["stockThumbs"] == 3 and "3" in s["stockLine"],
             f"thumbs={s['stockThumbs']} line={s['stockLine']}")
    ev("() => { delete window.__el._wireStockFallback; }")

    # G14 换回「写死 14」的旧判据：free=16 + qwen_edit(18) 应当漏报
    ev("""() => {
      window.__el._needsWarmup = function () {
        const f = (this._cfg || {}).vram_free_gb;
        return typeof f === 'number' && f >= 0 && f < 14;   // 旧实现
      };
      window.__cfg.vram_free_gb = 16;
    }""")
    ev("recfg()")
    ev("() => setEngine('qwen_edit')")
    s_old = ev("snap()")
    ev("() => { delete window.__el._needsWarmup; }")
    ev("() => setEngine('qwen_edit')")
    s_new = ev("snap()")
    ck.check("G14 自证：旧的写死 14 判据在 qwen_edit 上漏报，新判据报（G4 有鉴别力）",
             s_old["noteCount"] == 0 and s_new["noteCount"] == 1,
             f"old={s_old['noteCount']} new={s_new['noteCount']}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright 未安装")
        return 0

    ck = Checker()
    try:
        with tempfile.TemporaryDirectory() as td:
            page_fp = build_fixture_page(Path(td))
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not args.headed)
                ctx = browser.new_context(viewport={"width": 420, "height": 940})
                page = ctx.new_page()
                page.goto(page_fp.as_uri())
                page.wait_for_timeout(700)   # 组件 upgrade + 首轮配置 + 首轮存货
                run(page, ck)
                browser.close()
    except Exception as e:  # 门禁自身崩溃如实红（与 SKIP 语义区分）
        print(f"FAIL: 验证过程异常 {e!r}")
        return 1
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
