/**
 * 部署后 SEO / 分发面线上自检（verify:seo）—— 实施78 P0-4 前置固化。
 *
 * 为什么要有它：本仓刚吃过一次「tsc 过 + build exit 0，但线上某页运行时 500」的教训
 * （构建与 sibling 的编辑重叠，产出了坏 chunk）。结论是**部署后的验证必须真的去拉页面**，
 * 而不是看构建退出码。这个脚本把「搜索引擎与 AI 能不能正常发现、抓取、正确归类我们」
 * 这条链上最容易静默坏掉的几处，收成一条命令。
 *
 * 检查项（任一失败 exit 1）：
 *   1. robots.txt 可达，且**带 Sitemap 指令**（少了它爬虫要靠猜）；
 *   2. sitemap.xml 可达，URL 数不低于地板值（防某次改动把整段页面漏掉）；
 *   3. sitemap 必须包含 GEO 价值最高的那批页（对比页 / 下载页 / /en 首页）；
 *   4. GSC 验证文件仍可访问（它一 404，Search Console 的资源验证就会掉，
 *      而这件事**没有任何告警**，只有下次登后台才发现）；
 *   5. 关键双语对的 canonical 自指 + hreflang 双向互指 + x-default
 *      （中英内容高度相似，这条断了会被判重复内容，只保留一版 → /en 永远排不上去）；
 *   6. 每页自己的 OG 图端点返回**有效 PNG**（验 magic bytes 与尺寸，不只看 200——
 *      next/og 出错时同样能给你一个 200）。
 *
 * 用法（website/ 下）：
 *   npm run verify:seo                      # 打生产站 https://bd2026.cc
 *   npm run verify:seo -- --base http://127.0.0.1:3000
 *
 * 只读，无副作用。
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const baseIdx = args.indexOf("--base");
const BASE = (baseIdx >= 0 ? args[baseIdx + 1] : "https://bd2026.cc").replace(/\/$/, "");

/** 期望的对外邮箱：从 lib/site.ts 的默认值抽取，**不在这里硬编**——
 *  硬编就会变成第二处事实源，换地址时必然漏改一处。 */
function expectedEmail() {
  try {
    const src = readFileSync(
      join(dirname(fileURLToPath(import.meta.url)), "..", "lib", "site.ts"),
      "utf8"
    );
    const m = /NEXT_PUBLIC_CONTACT_EMAIL\s*\|\|\s*"([^"]*)"/.exec(src);
    return m ? m[1] : "";
  } catch {
    return "";
  }
}

// sitemap URL 数地板：当前 48。**只许上调**（新增页面时改这个数），
// 下调说明有页面掉出了 sitemap，那正是本检查要抓的。
const SITEMAP_MIN = 48;

// 必须出现在 sitemap 里的高价值页（GEO 主力 + 转化终点）
const SITEMAP_MUST = [
  "/en",
  "/en/compare",
  "/en/compare/respond-io",
  "/en/download/chatx",
  "/compare/respond-io",
  "/download/chatx",
];

// GSC 的 HTML 验证文件（换 token 时同步改这里）
const GSC_FILE = "/googlec972f84ce3f53ec7.html";

// 双语对：canonical 自指 + hreflang 双向一致
const PAIRS = [
  ["/", "/en"],
  ["/compare", "/en/compare"],
  ["/compare/respond-io", "/en/compare/respond-io"],
  ["/download/chatx", "/en/download/chatx"],
  ["/pricing", "/en/pricing"],
];

// 每页自己的 OG 图端点（Next 的 opengraph-image 约定）
const OG_ROUTES = [
  "/compare/opengraph-image",
  "/compare/respond-io/opengraph-image",
  "/download/chatx/opengraph-image",
  "/en/compare/opengraph-image",
  "/en/compare/respond-io/opengraph-image",
  "/en/download/chatx/opengraph-image",
];

const failures = [];
const ok = (label) => console.log(`[OK]   ${label}`);
const bad = (label, why) => {
  console.log(`[FAIL] ${label} — ${why}`);
  failures.push(`${label}: ${why}`);
};

async function get(path, asBuffer = false) {
  const res = await fetch(BASE + path, {
    headers: { "User-Agent": "boundless-verify-seo/1.0" },
    redirect: "follow",
  });
  const body = asBuffer ? Buffer.from(await res.arrayBuffer()) : await res.text();
  return { status: res.status, headers: res.headers, body };
}

/** 取 URL 的 path 部分做比较。
 *  ⚠ 不能只 `replace(BASE, "")`：canonical / sitemap 里写的是**生产绝对地址**
 *  （`https://bd2026.cc/...`，来自 SITE_URL），而 `--base` 指向本地 dev 时那段剥不掉，
 *  于是每条 canonical 都被误判成「未自指」、每个高价值页都被误判成「不在 sitemap」。
 *  按 URL 解析取 pathname 才对任意 base 都成立。 */
const norm = (u) => {
  if (!u) return "";
  try {
    return new URL(u, BASE).pathname.replace(/\/+$/, "") || "/";
  } catch {
    return u.replace(/^https?:\/\/[^/]+/i, "").replace(/\/+$/, "") || "/";
  }
};

function parseAlternates(html) {
  const out = {};
  for (const m of html.matchAll(/<link[^>]+rel="alternate"[^>]*>/gi)) {
    const tag = m[0];
    const hl = /hrefLang="([^"]+)"|hreflang="([^"]+)"/i.exec(tag);
    const hr = /href="([^"]+)"/i.exec(tag);
    if (hl && hr) out[(hl[1] || hl[2]).toLowerCase()] = hr[1];
  }
  return out;
}

const canonicalOf = (html) => {
  const m = /<link[^>]+rel="canonical"[^>]*href="([^"]+)"/i.exec(html);
  return m ? m[1] : "";
};

console.log(`== verify:seo against ${BASE} ==\n`);

// ── 1 + 2 + 3 robots / sitemap ────────────────────────────────────────────────
console.log("── robots.txt & sitemap.xml ──");
try {
  const r = await get("/robots.txt");
  if (r.status !== 200) bad("robots.txt", `status ${r.status}`);
  else if (!/^\s*Sitemap:\s*http/im.test(r.body)) bad("robots.txt", "缺 Sitemap: 指令");
  else ok("robots.txt 可达且带 Sitemap 指令");
} catch (e) {
  bad("robots.txt", String(e));
}

let sitemapUrls = [];
try {
  const s = await get("/sitemap.xml");
  if (s.status !== 200) {
    bad("sitemap.xml", `status ${s.status}`);
  } else {
    sitemapUrls = [...s.body.matchAll(/<loc>(.*?)<\/loc>/g)].map((m) => norm(m[1]));
    if (sitemapUrls.length < SITEMAP_MIN) {
      bad("sitemap.xml", `只有 ${sitemapUrls.length} 个 URL，低于地板 ${SITEMAP_MIN}（有页面掉出 sitemap？）`);
    } else {
      ok(`sitemap.xml 可达，${sitemapUrls.length} 个 URL（地板 ${SITEMAP_MIN}）`);
    }
    const missing = SITEMAP_MUST.filter((p) => !sitemapUrls.includes(norm(p)));
    if (missing.length) bad("sitemap 高价值页", `缺 ${missing.join(", ")}`);
    else ok(`sitemap 含全部 ${SITEMAP_MUST.length} 个高价值页`);
  }
} catch (e) {
  bad("sitemap.xml", String(e));
}

// ── 4 GSC 验证文件 ────────────────────────────────────────────────────────────
console.log("\n── Search Console 验证文件 ──");
try {
  const g = await get(GSC_FILE);
  if (g.status !== 200) bad(`GSC 验证文件 ${GSC_FILE}`, `status ${g.status}（资源验证会掉且无告警）`);
  else if (!g.body.includes("google-site-verification")) bad(`GSC 验证文件 ${GSC_FILE}`, "内容不含验证串");
  else ok(`GSC 验证文件可访问（${GSC_FILE}）`);
} catch (e) {
  bad("GSC 验证文件", String(e));
}

// ── 5 canonical / hreflang ────────────────────────────────────────────────────
console.log("\n── canonical 自指 + hreflang 双向互指 ──");
for (const [zh, en] of PAIRS) {
  try {
    const [pz, pe] = await Promise.all([get(zh), get(en)]);
    if (pz.status !== 200 || pe.status !== 200) {
      bad(`${zh} <-> ${en}`, `status ${pz.status}/${pe.status}`);
      continue;
    }
    const az = parseAlternates(pz.body);
    const ae = parseAlternates(pe.body);
    const problems = [];
    if (norm(canonicalOf(pz.body)) !== norm(zh)) problems.push("zh canonical 未自指");
    if (norm(canonicalOf(pe.body)) !== norm(en)) problems.push("en canonical 未自指");
    for (const [label, map] of [["zh", az], ["en", ae]]) {
      if (!Object.keys(map).some((k) => k.startsWith("en"))) problems.push(`${label} 页缺 en 备选`);
      if (!Object.keys(map).some((k) => k.startsWith("zh"))) problems.push(`${label} 页缺 zh 备选`);
      if (!("x-default" in map)) problems.push(`${label} 页缺 x-default`);
    }
    const setZ = new Set(Object.values(az).map(norm));
    const setE = new Set(Object.values(ae).map(norm));
    if (setZ.size !== setE.size || [...setZ].some((v) => !setE.has(v))) {
      problems.push("两版 hreflang 目标不一致（互指断裂）");
    }
    if (problems.length) bad(`${zh} <-> ${en}`, problems.join("; "));
    else ok(`${zh} <-> ${en}`);
  } catch (e) {
    bad(`${zh} <-> ${en}`, String(e));
  }
}

// ── 6 OG 图端点 ───────────────────────────────────────────────────────────────
console.log("\n── 每页 OG 分享图（验 PNG 本身，不只看 200）──");
for (const route of OG_ROUTES) {
  try {
    const r = await get(route, true);
    const buf = r.body;
    const isPng =
      buf.length > 8 &&
      buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47;
    if (r.status !== 200) {
      bad(route, `status ${r.status}`);
    } else if (!isPng) {
      bad(route, "不是 PNG（magic bytes 不符）");
    } else {
      const w = buf.readUInt32BE(16);
      const h = buf.readUInt32BE(20);
      if (w !== 1200 || h !== 630) bad(route, `尺寸 ${w}x${h}，期望 1200x630`);
      else ok(`${route}  ${w}x${h}  ${Math.round(buf.length / 1024)} KB`);
    }
  } catch (e) {
    bad(route, String(e));
  }
}

// ── 7 对外联系邮箱 ────────────────────────────────────────────────────────────
// EU AI Act 第 50 条要求标明 provider 身份，西方 B2B 买家也习惯先找邮箱；而这条链
// 断掉是完全静默的（页面照常渲染，只是少一行）。期望值取自 lib/site.ts，避免双源。
const EMAIL = expectedEmail();
const EMAIL_PAGES = [
  "/en/download/chatx", // 下载页联系条（U5：不用 Telegram 的访客）
  "/en",                // 页脚 provider 身份
  "/en/compliance",     // 合规能力页（LegalShell 不含页脚，身份行单独加过）
  "/privacy",
];
console.log("\n── 对外联系邮箱（provider 身份，D9）──");
if (!EMAIL) {
  console.log("[SKIP] lib/site.ts 里没解析到 CONTACT_EMAIL 默认值（用环境变量注入？）");
} else {
  let emailMissing = 0;
  for (const path of EMAIL_PAGES) {
    try {
      const r = await get(path);
      const hasText = r.body.includes(EMAIL);
      const hasMailto = r.body.includes(`mailto:${EMAIL}`);
      if (r.status === 200 && hasText && hasMailto) ok(`${path} 含 ${EMAIL} 且 mailto 成形`);
      else {
        bad(path, `status=${r.status} text=${hasText} mailto=${hasMailto}`);
        emailMissing++;
      }
    } catch (e) {
      bad(path, String(e));
      emailMissing++;
    }
  }
  if (emailMissing === EMAIL_PAGES.length) {
    console.log(
      `       ↳ 全部缺失且其余检查正常时，最可能是**这批改动还没部署**（本地验证用 --base http://127.0.0.1:3000）`
    );
  }
}

// ── 结论 ──────────────────────────────────────────────────────────────────────
console.log("");
if (failures.length) {
  console.log(`[verify:seo] 结果：FAIL —— ${failures.length} 项`);
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
console.log("[verify:seo] 结果：PASS");
