#!/usr/bin/env node
// 英文路由「中文字形」巡检（实施78 P0-2 的守门人，2026-08-28）。
//
// 为什么需要它：/en 的中文泄漏不是一处代码写错，而是**一类**缺陷——十几个组件各自
// 硬编 `{p.zh}`、把中文页的双写镜像照抄到英文档、或者靠 CSS 媒体查询藏中文（开场页
// `.zh-part` 只在 <520px 隐藏，而开场页只在桌面出现，等于英文桌面版一直挂着中文）。
// 静态扫描认不出「这处中文是不是被 lang 判定包住了」，所以这道门禁走**实测**：
// 打开真页面，剥掉脚本样式标签，看可见文本里还剩哪些汉字/假名。
//
// 判定纪律：
//   - 不是所有中文都是缺陷。语言切换按钮上的「中文」是正确的；翻译对照演示里的中文
//     源句是卖点本身（展示中文原文 → 自然英文）。这些进 ALLOWED 登记表，**必须写原因**。
//   - 登记表条目全轮未命中 → 报「可能已过期」，防止表越攒越长、门禁被稀释。
//   - 服务不可达 = SKIP exit 0（不把「当时没起服务」变成红告警，与站内其它 QA 脚本同口径）。
//
// 用法：
//   node scripts/qa-en-cjk.mjs [baseUrl]          # 默认 http://127.0.0.1:3100
//   QA_BASE_URL=http://127.0.0.1:3457 node scripts/qa-en-cjk.mjs
// 注意：本脚本只读页面，不写任何文件，对生产服务安全。

const BASE = (process.argv[2] || process.env.QA_BASE_URL || "http://127.0.0.1:3100").replace(/\/+$/, "");
const MAX_PAGES = 40;
const FETCH_TIMEOUT_MS = 60000; // dev 服务首次命中要现场编译，给足时间

// 允许出现在英文页的中文（每条必须写清为什么）。新增条目 = 一次显式的产品决策。
//
// `pages` 是**必要的**而非讲究：同一串汉字在一处合法、在另一处是缺陷。「智聊」在术语锁定
// 演示里是必需的样本（要展示锁定后的中文译名），出现在导航产品名里就是缺陷——不按页限定就
// 等于为了放过演示而把导航的缺陷一起放过。省略 pages = 全站允许（只给真正全局的条目）。
const ALLOWED = [
  { text: "中文", why: "语言切换按钮的目标语言标签——按钮写目标语言是通行做法" },
  { text: "我们给你包邮", pages: ["/en"], why: "翻译对照演示的中文源句（AutoChat compare）：展示中文原文→自然英文就是卖点" },
  { text: "今天下单还送小礼物", pages: ["/en"], why: "同上，同一条源句的后半段" },
  // pages 含 /en/interpreting：同一个 TranslateDemoPanel 组件在首页与同传页都嵌了一份
  //（实施78 P0-2 实测发现原先只登记 /en，同传页因此被判红）。
  { text: "你们支持", pages: ["/en", "/en/interpreting"], why: "TranslateDemo 的中文示例输入（你们支持 USDT 结算吗？），演示多语输入" },
  { text: "结算吗", pages: ["/en", "/en/interpreting"], why: "同上，同一条示例的后半段" },
  { text: "美团", pages: ["/en/interpreting"], why: "GlossaryLockDemo：术语锁定演示必须展示锁定后的中文译名" },
  { text: "智聊", pages: ["/en/interpreting"], why: "同上，术语表锁定我方品牌名的演示样本（此页其它位置的「智聊」仍会被抓）" },
  // 实施78 P0-2 补登（2026-08-28，另一条线）：/en/interpreting 同传演示的「客户看到的中文」侧。
  // 与上面「你们支持 / 结算吗」同属演示区（ZH · customer sees ↔ EN · you typed）：
  // 展示中文原文→英文译文本身就是产品能力，改成英文等于把演示拆了。
  { text: "私有部署怎么收费", pages: ["/en/interpreting"], why: "同传演示：客户侧中文提问样例，与右侧英文译文成对展示互译能力" },
  { text: "按规模报价", pages: ["/en/interpreting"], why: "同上，我方中文回答样例（逗号把整句切成两段）" },
  { text: "含部署调试", pages: ["/en/interpreting"], why: "同上，同一句的后半段" },
];

/* 整页豁免：该页上的中文**全部**是刻意的，逐词登记会把登记表写成词典且永远追不上。
   门槛比 ALLOWED 高——只有「这一页的中文本身就是内容」才配得上，且必须写清判据。 */
const PAGE_EXEMPT = [
  {
    page: "/en/fate",
    // 2026-08-28 老板拍板：保留中文盘面。判据有三条——
    // ① 页面标签写的就是 verbatim engine output，换成英文即成假话；
    // ② 英文世界的 BaZi 内容惯例保留干支原文（Jia-Zi 一类），译掉反而不专业；
    // ③ 已并列 English gloss 对照译读（landingContent.fateKline.chartSummaryEn），
    //    英文读者与 AI 引擎都拿得到可引用的英文文本，中文原文只作证据。
    why: "命理盘面＝引擎逐字输出的干支/十神/五行，中文原文即内容本身；旁边已有 English gloss 对照译读",
  },
];

// 已知残留、**待产品决策**的页面（不计入 FAIL，但每轮点名，防止「静默容忍」）。
// 与 ALLOWED / PAGE_EXEMPT 的区别：那两者是「这样是对的」，PENDING 是「这样不对但怎么改要人拍板」。
// 空掉它 = 债清完了；条目长期不清 = 该拍板了。
const PENDING = [];

function exemptPage(page) {
  return PAGE_EXEMPT.find((p) => p.page === page) || null;
}

function allowedOn(text, page) {
  const hit = ALLOWED.find((a) => a.text === text && (!a.pages || a.pages.includes(page)));
  return hit || null;
}

const CJK = /[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+/g;

async function get(path) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(BASE + path, { signal: ctl.signal, redirect: "follow" });
    if (!res.ok) return { status: res.status, html: "" };
    return { status: res.status, html: await res.text() };
  } finally {
    clearTimeout(timer);
  }
}

// 只保留「访客真的看得见的文本」：脚本/样式/注释/标签/HTML 实体全部剥掉。
// 剥标签时换成空格而不是空串——否则相邻元素的文字会粘成一个假词。
function visibleText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&[a-zA-Z#0-9]+;/g, " ");
}

function internalEnLinks(html) {
  const out = new Set();
  for (const m of html.matchAll(/href="(\/en(?:\/[^"#?]*)?)(?:[#?][^"]*)?"/g)) {
    out.add(m[1].replace(/\/$/, "") || "/en");
  }
  return [...out];
}

const seen = new Map(); // allowlist 命中统计，用于过期检查

function scan(path, html) {
  const runs = visibleText(html).match(CJK) || [];
  const unexpected = new Map();
  for (const r of runs) {
    const hit = allowedOn(r, path);
    if (hit) {
      seen.set(hit.text, (seen.get(hit.text) || 0) + 1);
      continue;
    }
    unexpected.set(r, (unexpected.get(r) || 0) + 1);
  }
  return unexpected;
}

async function main() {
  console.log(`[qa:en-cjk] 目标 ${BASE}`);

  let root;
  try {
    root = await get("/en");
  } catch (err) {
    console.log(`[qa:en-cjk] SKIP：服务不可达（${err.message}）——先起 next dev/start 再跑`);
    process.exit(0);
  }
  if (!root.html) {
    console.log(`[qa:en-cjk] SKIP：GET /en 返回 ${root.status}，拿不到页面`);
    process.exit(0);
  }

  const pages = ["/en", ...internalEnLinks(root.html).filter((p) => p !== "/en")].slice(0, MAX_PAGES);
  console.log(`[qa:en-cjk] 从 /en 发现 ${pages.length} 个英文页`);

  const offenders = [];
  const exemptSeen = [];
  const pendingSeen = [];
  const pendingClean = [];
  for (const p of pages) {
    let html = p === "/en" ? root.html : "";
    if (!html) {
      try {
        const r = await get(p);
        html = r.html;
        if (!html) {
          console.log(`  ? ${p} → HTTP ${r.status}，跳过`);
          continue;
        }
      } catch (err) {
        console.log(`  ? ${p} → 取页失败（${err.message}），跳过`);
        continue;
      }
    }
    const ex = exemptPage(p);
    if (ex) {
      // 整页豁免仍打印命中数：豁免是「我们知道且认可」，不是「不再看」。
      const n = (visibleText(html).match(CJK) || []).length;
      console.log(`  ○ ${p} → 整页豁免（${n} 处中文，理由：${ex.why}）`);
      exemptSeen.push(p);
      continue;
    }
    const bad = scan(p, html);
    const pend = PENDING.find((x) => x.page === p);
    if (bad.size === 0) {
      console.log(`  ✓ ${p}`);
      if (pend) pendingClean.push(p);
    } else if (pend) {
      // 待决策页：点名但不判红，否则这道门禁会被一个「等人拍板」的页面长期钉在 FAIL，
      // 于是没人再看它——那就等于没有门禁。
      console.log(`  ~ ${p} → 待决策（${bad.size} 处），不计入 FAIL`);
      pendingSeen.push(p);
    } else {
      const list = [...bad.entries()].map(([t, n]) => `${t}${n > 1 ? `×${n}` : ""}`);
      console.log(`  ✗ ${p} → ${list.join(" / ")}`);
      offenders.push({ page: p, runs: list });
    }
  }

  const staleExempt = PAGE_EXEMPT.filter((e) => !exemptSeen.includes(e.page));
  if (staleExempt.length) {
    console.log("[qa:en-cjk] 提示：以下整页豁免本轮没爬到（页面下线了？），确认后请删掉该条目：");
    for (const e of staleExempt) console.log(`    - ${e.page}`);
  }

  const stale = ALLOWED.filter((a) => !seen.has(a.text));
  if (stale.length) {
    console.log("[qa:en-cjk] 提示：以下登记条目本轮一次都没命中，可能已过期（文案改了或页面没被爬到）：");
    for (const s of stale) console.log(`    - ${s.text}（登记理由：${s.why}）`);
  }

  if (pendingSeen.length) {
    console.log("[qa:en-cjk] 待决策债（不判红，但请别忘）：");
    for (const p of pendingSeen) {
      console.log(`    - ${p}：${PENDING.find((x) => x.page === p).why}`);
    }
  }
  if (pendingClean.length) {
    console.log(`[qa:en-cjk] 提示：${pendingClean.join("、")} 已无中文残留 → 请从 PENDING 里删掉该条目`);
  }

  if (offenders.length) {
    console.log("");
    console.log(`[qa:en-cjk] 结果：FAIL —— ${offenders.length} 个英文页出现未登记的中文字形。`);
    console.log("          修法二选一：① 按语言渲染（中文页保留、英文页出英文）；");
    console.log("          ② 确认这处中文是刻意的（如翻译演示的源句），加进本脚本 ALLOWED 并写明原因。");
    process.exit(1);
  }
  console.log("[qa:en-cjk] 结果：PASS —— 英文页无未登记的中文字形");
}

main().catch((err) => {
  console.error(`[qa:en-cjk] 异常：${err.stack || err.message}`);
  process.exit(1);
});
