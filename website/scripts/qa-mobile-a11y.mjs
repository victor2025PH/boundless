/**
 * QA：移动端首屏占用 + 文本对比度 + 动效降级（实施78 X6/X7 的可判定口径，2026-08-28）。
 *
 * 为什么要机械化：这两类问题**读代码看不出来**——首屏被浮层吃掉多少，取决于运行时有几个
 * fixed/sticky 元素同时在场；对比度取决于「文字色 × 逐层上溯得到的有效背景色」。只能实测。
 *
 * 三项检查，每项都刻意窄口径、宁可漏报不误报：
 *
 *  ① **首屏占用**：390×844 下统计所有 fixed/sticky 且与首屏相交的元素，算它们覆盖的
 *     像素占比。判据不是「有几个浮层」而是「吃掉多少首屏」——一个贴边的小气泡和一条
 *     压掉三分之一屏的横条不是一回事。
 *  ② **文本对比度**（WCAG 2.1 AA）：正文 ≥4.5、大字（≥24px 或 ≥18.66px 且 bold）≥3.0。
 *     **有效背景无法确定时一律跳过**（祖先带渐变/背景图就放弃判定）——本站大量深色渐变，
 *     硬猜背景色会产出一堆假阳性，那样的报告没人会看第二次。
 *  ③ **动效降级**：开 prefers-reduced-motion:reduce 前后各数一次「正在跑动画的元素」，
 *     两者相等 = 站点根本没响应这个偏好。仓内已有 45 处相关代码，本项是**验证不是假设**。
 *
 * 用法：
 *   node scripts/qa-mobile-a11y.mjs                     # 打生产站
 *   QA_BASE=http://127.0.0.1:3100 node scripts/qa-mobile-a11y.mjs
 * 退出码：0=无超标项；1=有超标项；2=环境不可用（缺 playwright / 站点不可达，不报红）。
 */
const BASE = (process.env.QA_BASE || process.argv[2] || "https://bd2026.cc").replace(/\/+$/, "");
const ROUTES = ["/en", "/en/download/chatx", "/en/compare/respond-io", "/en/pricing", "/"];
const VIEWPORT = { width: 390, height: 844 };
/** 首屏被浮层覆盖的比例上限：超过即点名（一屏三分之一被吃掉就该改） */
const OVERLAY_BUDGET = 0.3;

let chromium;
try {
  ({ chromium } = await import("playwright"));
} catch {
  console.log("[qa:m-a11y] SKIP：未安装 playwright");
  process.exit(2);
}

/** WCAG 相对亮度 + 对比度（纯函数，浏览器内执行） */
const IN_PAGE = `(() => {
  function parse(c) {
    const m = String(c).match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(",").map((x) => parseFloat(x));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  }
  function lum({ r, g, b }) {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  }
  function ratio(a, b) {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }
  // 有效背景：向上找第一个不透明背景色；中途遇到 background-image（渐变/图）即放弃判定
  function effBg(el) {
    let cur = el;
    while (cur && cur !== document.documentElement) {
      const s = getComputedStyle(cur);
      if (s.backgroundImage && s.backgroundImage !== "none") return null;
      const bg = parse(s.backgroundColor);
      if (bg && bg.a >= 0.95) return bg;
      if (bg && bg.a > 0) return null; // 半透明叠加，算不准就不算
      cur = cur.parentElement;
    }
    const body = parse(getComputedStyle(document.body).backgroundColor);
    return body && body.a >= 0.95 ? body : null;
  }
  const out = { low: [], checked: 0, skipped: 0 };
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  const seen = new Set();
  while ((n = walk.nextNode())) {
    const t = (n.textContent || "").trim();
    // 阈值 2 而不是 3：首跑用 3 时把「TG」「WA」两个平台徽标漏掉了，而它们与被抓到的
    // 「LINE」「MSG」是同一处缺陷——过滤器的本意只是跳过单字符标点，别顺手滤掉真内容。
    if (t.length < 2) continue;
    const el = n.parentElement;
    if (!el || !el.offsetParent) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const s = getComputedStyle(el);
    const fg = parse(s.color);
    const bg = effBg(el);
    if (!fg || !bg) { out.skipped++; continue; }
    out.checked++;
    const size = parseFloat(s.fontSize) || 16;
    const weight = parseInt(s.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3 : 4.5;
    const cr = ratio(fg, bg);
    if (cr < need) {
      const key = t.slice(0, 24) + "|" + s.color;
      if (seen.has(key)) continue;
      seen.add(key);
      out.low.push({ text: t.slice(0, 34), ratio: Math.round(cr * 100) / 100, need, size, color: s.color });
    }
  }
  // 首屏浮层覆盖
  const vw = innerWidth, vh = innerHeight;
  const boxes = [];
  for (const el of document.querySelectorAll("body *")) {
    const s = getComputedStyle(el);
    if (s.position !== "fixed" && s.position !== "sticky") continue;
    if (s.display === "none" || s.visibility === "hidden" || parseFloat(s.opacity) < 0.05) continue;
    // 关键口径（2026-08-28 首跑修正）：pointer-events:none 的全屏层是**背景装饰**
    //（TechBackground / ParticleField canvas / 光斑），既不挡点击也不占据阅读位。
    // 把它们算进「浮层吃掉首屏」会得出 322% 这种数字——指标一旦有假阳性就没人看第二次。
    if (s.pointerEvents === "none") continue;
    const r = el.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= vh || r.width < 8 || r.height < 8) continue;
    // 只取最外层：祖先已入选则跳过，避免同一浮层内部元素重复计面积
    if (boxes.some((b) => b.el.contains(el))) continue;
    boxes.push({ el, r, tag: el.tagName.toLowerCase(), cls: (typeof el.className === "string" ? el.className : "").split(/\\s+/).slice(0, 2).join(".") });
  }
  // 导航栏单列：它是**预期内的站点框架**，不是抢注意力的浮层，混在一起会掩盖真正的问题。
  let covered = 0;
  let chrome = 0;
  const overlays = boxes.map((b) => {
    const w = Math.min(b.r.right, vw) - Math.max(b.r.left, 0);
    const h = Math.min(b.r.bottom, vh) - Math.max(b.r.top, 0);
    const area = Math.max(0, w) * Math.max(0, h);
    const isChrome = b.tag === "header" || b.tag === "nav";
    if (isChrome) chrome += area;
    else covered += area;
    return {
      what: b.tag + (b.cls ? "." + b.cls : ""),
      pct: Math.round((area / (vw * vh)) * 1000) / 10,
      chrome: isChrome,
    };
  });
  out.overlayPct = Math.round((covered / (vw * vh)) * 1000) / 10;
  out.chromePct = Math.round((chrome / (vw * vh)) * 1000) / 10;
  out.overlays = overlays.filter((o) => o.pct >= 1).sort((a, b) => b.pct - a.pct).slice(0, 6);
  // 正在跑动画的元素数
  out.animating = [...document.querySelectorAll("body *")].filter((el) => {
    const s = getComputedStyle(el);
    return s.animationName && s.animationName !== "none" && parseFloat(s.animationDuration) > 0;
  }).length;
  return out;
})()`;

const browser = await chromium.launch();
let bad = 0;
let unreachable = 0;

for (const route of ROUTES) {
  const ctx = await browser.newContext({ viewport: VIEWPORT, isMobile: true, hasTouch: true });
  const page = await ctx.newPage();
  let res;
  try {
    await page.goto(BASE + route, { waitUntil: "networkidle", timeout: 45000 });
    await page.waitForTimeout(3500);
    res = await page.evaluate(IN_PAGE);
  } catch (e) {
    console.log(`[qa:m-a11y] ?  ${route.padEnd(26)} 不可达（${String(e).slice(0, 60)}）`);
    unreachable++;
    await ctx.close();
    continue;
  }

  // 动效降级：同页再开一个 reduce 上下文对照
  const rctx = await browser.newContext({ viewport: VIEWPORT, isMobile: true, reducedMotion: "reduce" });
  const rpage = await rctx.newPage();
  let animReduced = null;
  try {
    await rpage.goto(BASE + route, { waitUntil: "networkidle", timeout: 45000 });
    await rpage.waitForTimeout(3000);
    animReduced = await rpage.evaluate(
      `[...document.querySelectorAll("body *")].filter((el)=>{const s=getComputedStyle(el);return s.animationName&&s.animationName!=="none"&&parseFloat(s.animationDuration)>0;}).length`
    );
  } catch {
    /* 对照失败不阻断主检查 */
  }
  await rctx.close();

  // 空结果守卫（2026-08-28 实测加）：偶发一次 networkidle 抢跑 / 渲染未完成会让本轮
  // 三项全是 0，而 0<0 为假 → 被判成「动效未降级」。**页面没取到内容不等于页面有缺陷**，
  // 这种假红会让门禁失去信任，比漏报更贵。判为「未取到」并跳过。
  const emptyRun = res.checked === 0 && res.animating === 0 && res.overlays.length === 0;
  if (emptyRun) {
    console.log(`  ?  ${route.padEnd(26)} 本轮未取到页面内容（渲染未完成？），跳过判定`);
    await ctx.close();
    continue;
  }

  const overBudget = res.overlayPct > OVERLAY_BUDGET * 100;
  // 动效对照只在「基线确实有动画」时才有意义：基线 0 说明这页本来就没动画，无从谈降级。
  const motionOk = animReduced === null || res.animating === 0 ? null : animReduced < res.animating;
  const flags = [];
  if (overBudget) flags.push("首屏浮层超预算");
  if (res.low.length) flags.push(`对比度 ${res.low.length} 处`);
  if (motionOk === false) flags.push("动效未降级");
  if (flags.length) bad++;

  console.log(
    `${flags.length ? "  X " : "  ok"} ${route.padEnd(26)} 浮层占首屏 ${res.overlayPct}%（另导航 ${res.chromePct}%）` +
      `　对比度低 ${res.low.length}（判定 ${res.checked} / 跳过 ${res.skipped}）` +
      `　动画 ${res.animating}→${animReduced === null ? "?" : animReduced}` +
      (flags.length ? `　⚠ ${flags.join(" / ")}` : "")
  );
  for (const o of res.overlays) {
    console.log(`         ${o.chrome ? "导航" : "浮层"} ${o.what} 占 ${o.pct}%`);
  }
  for (const l of res.low.slice(0, 6)) {
    console.log(`         低对比 ${l.ratio}<${l.need} ${l.size}px ${l.color}  「${l.text}」`);
  }
  await ctx.close();
}
await browser.close();

if (unreachable === ROUTES.length) {
  console.log("[qa:m-a11y] SKIP：全部路由不可达");
  process.exit(2);
}
console.log(bad === 0 ? "[qa:m-a11y] 结果：PASS" : `[qa:m-a11y] 结果：FAIL（${bad} 个页面有超标项）`);
process.exit(bad === 0 ? 0 : 1);
