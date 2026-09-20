/**
 * 内容完整性门禁（gate:content）—— 官网文案必须跟着产品事实走。
 *
 * 六道检查，任一违规 exit 1 并打印清单（对齐 engines/chengjie 的 ratchet 门禁文化：
 * 清理过的坏内容不许回潮，新增内容必须过事实源）：
 *   1. 静态资产存在性 —— 源码字符串里引用的 /xxx.png 等本地资产必须真实存在于 public/。
 *   2. 价格一致性 —— lib/content.ts / lib/pricing.ts 里的价格必须与
 *      lib/generated/product-facts.json（由 npm run sync:facts 从 products/*\/product.yaml
 *      + platform/licensing/sku_registry.json 生成）一致。
 *   3. 禁用宣传语黑名单 —— 已被清理的虚构数字 / 高风险表述不得回潮。
 *   4. 测试数 ratchet —— lib/content.ts 宣传的「N+ 自动化(回归)测试」数字必须 ≤
 *      engines/{chengjie,huoke}/tests 下实际测试文件数（test_*.py / *_test.py 递归计数），
 *      防夸大失实；两侧任一取不到（文案改措辞 / 部署机没有 engines/）→ warning 跳过不 fail。
 *   5. 发布事实源一致性 —— lib/chatxContent.ts 的 CHATX.download（version/filename/
 *      sha256/size）必须与 public/downloads/manifest.json（打包脚本生成的实况）一致。
 *      为什么值得一道门禁：下载页运行时会 fetch manifest.json 自动校正显示值，所以
 *      人眼在页面上**看不出** chatxContent.ts 已经落后；但 SoftwareApplication JSON-LD
 *      用的是构建时的字面量，而爬虫不执行那次校正 —— 落后的版本号会被 AI 当事实引用
 *      （2026-08-28 实录：JSON-LD 卡在 1.0.21，线上实际 1.0.58，差了 37 个版本）。
 *   6. 版本更新记录覆盖 —— lib/chatx-release-notes.json（官网发布页数据源）结构合法，
 *      且必须收录 manifest.json 的已发布版本。执行「每次发包都生成 changelog 条目」：
 *      发了新包却忘了 `node scripts/gen-chatx-changelog.mjs add` → 这里红。
 *      manifest 缺失（只检出 website/ 的部署机）→ 仅做结构校验，跳过覆盖比对。
 *
 * 用法（在 website/ 下）：
 *   npm run gate:content        # prebuild 也会自动跑
 */
import { readFileSync, readdirSync, existsSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const websiteRoot = join(here, "..");
const publicDir = join(websiteRoot, "public");
const factsPath = join(websiteRoot, "lib", "generated", "product-facts.json");

// ---------------------------------------------------------------------------
// 禁用宣传语黑名单（检查 3）。每一项都是本轮内容治理里清理掉的虚构数字/高风险表述，
// 列在这里防回潮；新增禁用词请同步登记 docs/claims.md「已禁用清单」一节（同源）。
// ---------------------------------------------------------------------------
const BANNED_CLAIMS = [
  "2000万", // 虚构规模数字（无任何后端数据支撑的"2000万用户/消息"类吹牛）
  "98%",    // 虚构百分比（无评测口径支撑的"98% 准确率/满意度"）
  "300+",   // 虚构数量级（无清单支撑的"300+ 企业/功能"）
  "封号自动换号", // 高风险表述：公开承诺规避平台风控 = 自证违反平台 ToS，法务红线
  // （2026-07-26 政策更新：幻缘/FateX 获批独立落地页 /fate，销售面解禁，从黑名单移除；
  //   「九产品」仍禁——销售面禁止手写产品数量数字，防与 brand.ts PRODUCT_COUNT 双源漂移。）
  "九产品", // 品牌层口径（brand.ts PRODUCT_COUNT 驱动）；销售面禁止手写产品数量数字
  "防封",   // 高风险表述：暗示对抗平台风控，与"封号自动换号"同族，法务红线
  "顾嘉",   // 2026-08-21 顾问改名「小界（Jie）」（与产品内助手/交流群 @小界 同名统一）；旧名不许回潮
  "Gary",   // 同上：小界的旧英文名，防从旧文案复制粘贴带回
];

// 黑名单检查的目标文件（对外文案的集中地；新增内容文件时在此登记）。
// 刻意不含 lib/brand.ts：它是品牌层单一事实源，合法包含幻缘/FateX 产品定义与
// 九产品口径（PRODUCT_ORDER/PRODUCT_COUNT），扫它会对合法内容误报；
// brand.ts 的高风险词（防封类）已于 2026-07-26 清零，靠 review 守住。
const BANNED_TARGET_FILES = [
  "lib/content.ts",
  "lib/landingContent.ts",
  "lib/matrixxContent.ts",
  "lib/chatxContent.ts",
  "lib/growthContent.ts",
  "lib/downloads.ts",
  // 顾问人设三消费面（2026-08-21 顾嘉→小界改名后进扫描，守「旧名回潮」）
  "components/AIChat.tsx",
  "lib/bot-knowledge.ts",
  "lib/telegram-bot.ts",
];

// ---------------------------------------------------------------------------
// 检查 1 配置：静态资产存在性
// ---------------------------------------------------------------------------
// 扫描范围：官网源码三大目录（scripts/ 刻意不扫——工具脚本里的路径多为示例/生成目标）。
// 说明：规格要求 lib/**/*.ts、components/**/*.tsx、app/**/*.tsx；这里对三个目录统一收
// .ts + .tsx（严格超集，如 lib/ogTemplate.tsx 也在文案资产链上），只多查不少查。
const SCAN_DIRS = ["lib", "components", "app"];
const SCAN_EXTS = new Set([".ts", ".tsx"]);
// 资产扩展名（按规格逐一列出）。
const ASSET_EXT_RE = /\.(?:png|jpe?g|webp|gif|svg|mp3|mp4|ogg|wav|ico)$/i;
// 字符串字面量里的候选路径：以 / 开头、以资产扩展名结尾（后可跟 ?query/#hash）。
const PATH_IN_STRING_RE =
  /\/(?:[A-Za-z0-9_\-.@%~]+\/)*[A-Za-z0-9_\-.@%~]+\.(?:png|jpe?g|webp|gif|svg|mp3|mp4|ogg|wav|ico)(?=[?#]|$)/gi;
// 单行内的字符串字面量（含转义处理；跨行模板字符串不匹配——静态资产路径不该跨行写）。
const STRING_LITERAL_RE = /(["'`])((?:\\.|(?!\1)[^\\\n])*)\1/g;

/** 递归收集待扫描源码文件（防御性跳过 node_modules/.next/generated 生成物目录）。 */
function collectSourceFiles(dir, acc) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const name = entry.name;
    if (name === "node_modules" || name === ".next" || name === "generated") continue;
    const full = join(dir, name);
    if (entry.isDirectory()) {
      collectSourceFiles(full, acc);
    } else if (SCAN_EXTS.has(name.slice(name.lastIndexOf(".")).toLowerCase())) {
      acc.push(full);
    }
  }
  return acc;
}

/** 排除规则（每一条都只剔真误报，见 docs/claims.md 与首跑报告）：
 *  E1 字面量含 ${           → 模板动态拼接，静态检查无法断言
 *  E2 字面量以 http(s):// 或 // 开头，或候选路径前文含 http(s)://
 *                            → 外链/协议相对地址，不落在 public/
 *  E3 字面量紧邻 + 号        → 明显动态字符串拼接
 *  E4 候选路径以 /api/ 开头   → API 路由，不是静态资产
 *  E5 候选路径以 /_next/ 开头 → next 构建产物运行时路径，不落在 public/
 *  E6 候选路径前一个字符是路径字符（字母/数字/._~%@-）→ 说明它是相对路径的中段
 *     （如 "../public/x.png" 里的 "/public/x.png"），不是「以 / 开头」的 public 根路径
 *  E8 运行时覆盖式产物（teaser-war-latest.png）→ weekly-report 写出，不进仓库
 *  存在性判定前先剥 ?query/#hash 并做 decodeURIComponent（失败则用原文）。
 */
const PATHISH_CHAR_RE = /[A-Za-z0-9_.~%@-]/;
function checkAssets() {
  const missing = [];
  let refCount = 0;
  const files = [];
  for (const d of SCAN_DIRS) {
    const abs = join(websiteRoot, d);
    if (existsSync(abs) && statSync(abs).isDirectory()) collectSourceFiles(abs, files);
  }
  for (const file of files) {
    const rel = file.slice(websiteRoot.length + 1).replace(/\\/g, "/");
    const lines = readFileSync(file, "utf8").split(/\r?\n/);
    lines.forEach((line, idx) => {
      STRING_LITERAL_RE.lastIndex = 0;
      let m;
      while ((m = STRING_LITERAL_RE.exec(line)) !== null) {
        const literal = m[2];
        if (literal.includes("${")) continue; // E1
        if (/^https?:\/\//i.test(literal) || literal.startsWith("//")) continue; // E2
        // E3：字面量前后紧邻 +（跳过空白）视为拼接
        const before = line.slice(0, m.index).replace(/\s+$/, "");
        const after = line.slice(m.index + m[0].length).replace(/^\s+/, "");
        if (before.endsWith("+") || after.startsWith("+")) continue;

        PATH_IN_STRING_RE.lastIndex = 0;
        let pm;
        while ((pm = PATH_IN_STRING_RE.exec(literal)) !== null) {
          const candidate = pm[0];
          const pre = literal.slice(0, pm.index);
          if (/https?:\/\//i.test(pre)) continue; // E2（路径是外链 URL 的 pathname 部分）
          if (pm.index > 0 && PATHISH_CHAR_RE.test(literal[pm.index - 1])) continue; // E6
          if (candidate.startsWith("/api/")) continue; // E4
          if (candidate.startsWith("/_next/")) continue; // E5
          // E7：/media/* 由服务器 nginx 直出（/var/www/media，部署不覆盖的运行时媒体区），
          // 按架构设计不进仓库 public/（先例：日更 feed 视频；2026-08-07 品牌片同通道）。
          if (candidate.startsWith("/media/")) continue;
          // E8：运行时覆盖式产物，weekly-report / war-card 写出；缺文件时调用方
          // 降级纯文字，不是页面静态资源，不进仓库（2026-09-11 FromHead 部署实锤）。
          if (candidate === "/brand/campaign/teaser-war-latest.png") continue;
          refCount++;
          const bare = candidate.split(/[?#]/)[0];
          let decoded = bare;
          try {
            decoded = decodeURIComponent(bare);
          } catch {
            /* 保留原文 */
          }
          const fsPath = join(publicDir, ...decoded.split("/").filter(Boolean));
          if (!existsSync(fsPath)) {
            missing.push({ file: rel, line: idx + 1, path: candidate });
          }
        }
      }
    });
  }
  return { missing, refCount, fileCount: files.length };
}

// ---------------------------------------------------------------------------
// 检查 2：价格一致性（事实源 = lib/generated/product-facts.json）
// ---------------------------------------------------------------------------
/** 价格数字的词边界匹配：防 "58" 命中 "598"/"5580"/"58.5"。 */
function priceRe(price) {
  const esc = price.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?<![\\d.])${esc}(?![\\d.])`);
}

function checkPrices() {
  const problems = [];
  if (!existsSync(factsPath)) {
    problems.push("缺 lib/generated/product-facts.json —— 先跑 npm run sync:facts");
    return { problems, checked: 0 };
  }
  const facts = JSON.parse(readFileSync(factsPath, "utf8"));
  const contentSrc = readFileSync(join(websiteRoot, "lib", "content.ts"), "utf8");
  const pricingSrc = readFileSync(join(websiteRoot, "lib", "pricing.ts"), "utf8");
  // 2026-08-19 Token 定价改版：智聊/翻译的价格单一真相迁到 lib/chatx-pricing.ts
  //（content.ts / pricing.ts 全部派生、不再有字面量），价格文本比对源随之扩一处。
  const chatxPricingSrc = readFileSync(join(websiteRoot, "lib", "chatx-pricing.ts"), "utf8");

  let checked = 0;
  const productsToCheck = ["zhiliao", "tongyi"];
  const factPrices = { zhiliao: new Set(), tongyi: new Set() };
  for (const pid of productsToCheck) {
    const product = (facts.products || []).find((p) => p.id === pid);
    if (!product) {
      problems.push(`事实源缺产品 ${pid}（product-facts.json 过期？重跑 npm run sync:facts）`);
      continue;
    }
    for (const sku of product.skus || []) {
      const price = sku.price;
      if (price == null || !/^\d+(\.\d+)?$/.test(price)) {
        // 非定值报价（TBD / from N / 按规模报价）不做文本比对
        continue;
      }
      // 停售台账 SKU（note/名称带「停售」/legacy）不再要求出现在文案——只留注册表反查。
      const legacy = /停售|legacy/i.test(`${sku.id} ${sku.name ?? ""} ${sku.note ?? ""}`);
      factPrices[pid].add(price);
      if (legacy) continue;
      checked++;
      const re = priceRe(price);
      if (!re.test(contentSrc) && !re.test(pricingSrc) && !re.test(chatxPricingSrc)) {
        problems.push(
          `SKU ${sku.id}（${pid}）价格 ${price} 在 lib/content.ts / lib/pricing.ts / lib/chatx-pricing.ts 均未出现 —— 文案价格与事实源脱钩`
        );
      }
    }
  }

  // 反向断言（2026-08-19 起断言目标 = 新单源 chatx-pricing.ts）：CHATX_PLANS 各档
  // monthly 价必须 ∈ 事实源 zhiliao/tongyi SKU 价格集合 —— 防止官网侧擅自改价/加档
  //（skuId: null 的免费/按量档除外；更强的逐 SKU 相等闸在 scripts/assert-order-lines.mjs）。
  const planPriceMatches = [...chatxPricingSrc.matchAll(/skuId:\s*"[\w-]+",\s*\n\s*edition:[^\n]+\n\s*monthly:\s*([\d.]+)/g)];
  if (planPriceMatches.length < 3) {
    problems.push(
      `chatx-pricing.ts 档位价格只抽到 ${planPriceMatches.length} 处（预期 ≥3）—— 源码形状变更，请同步本门禁`
    );
  }
  const factUnion = new Set([...factPrices.zhiliao, ...factPrices.tongyi]);
  for (const mm of planPriceMatches) {
    if (Number(mm[1]) === 0) continue; // 免费档不是 SKU 价
    checked++;
    if (!factUnion.has(mm[1])) {
      problems.push(
        `chatx-pricing.ts 档位价格 ${mm[1]} 不在事实源 zhiliao/tongyi SKU 价格集合内（改价必须先改 product.yaml 并重跑 sync:facts）`
      );
    }
  }
  return { problems, checked };
}

// ---------------------------------------------------------------------------
// 检查 3：禁用宣传语黑名单
// ---------------------------------------------------------------------------
function checkBannedClaims() {
  const hits = [];
  for (const relFile of BANNED_TARGET_FILES) {
    const abs = join(websiteRoot, ...relFile.split("/"));
    if (!existsSync(abs)) {
      hits.push({ file: relFile, line: 0, term: "(文件缺失)", excerpt: "黑名单目标文件不存在——文件被移动/删除时须同步本门禁" });
      continue;
    }
    const lines = readFileSync(abs, "utf8").split(/\r?\n/);
    lines.forEach((line, idx) => {
      for (const term of BANNED_CLAIMS) {
        if (line.includes(term)) {
          const trimmed = line.trim();
          hits.push({
            file: relFile,
            line: idx + 1,
            term,
            excerpt: trimmed.length > 88 ? trimmed.slice(0, 88) + "…" : trimmed,
          });
        }
      }
    });
  }
  return hits;
}

// ---------------------------------------------------------------------------
// 检查 4：测试数 ratchet（宣传的测试数必须 ≤ 仓内实际测试文件数）
// ---------------------------------------------------------------------------
// 事实口径与 docs/claims.md「N+ 自动化回归测试」行同源（2026-08 起宣传 1100+）：实际数 = 双引擎 tests/ 目录
// 递归匹配 test_*.py / *_test.py 的文件总数（按 pytest 发现语义大小写敏感）。
// 宣传数从 lib/content.ts 源码解析：先找含关键词的行，再抽 `N+`（含 stats 的
// value/suffix 拆写形 `value: "850", suffix: "+"`）。两侧解析/计数策略刻意宽松：
//   - 解析不到宣传数 → warning 跳过（文案可能改写了措辞，需人工同步解析正则），不 fail；
//   - 测试目录不存在 → warning 跳过（部署机可能只检出 website/），不 fail；
// 两侧都取到 → 强制每个宣传数 N ≤ 实际数 M（ratchet：上调宣传数字前先看实际数）。
const TEST_DIRS = [
  { label: "chengjie", segments: ["..", "engines", "chengjie", "tests"] },
  { label: "huoke", segments: ["..", "engines", "huoke", "tests"] },
];
const TEST_FILE_RE = /^(?:test_.*|.*_test)\.py$/;
// 宣传语关键词（命中任一即视为「测试数宣传行」）：
//   zh 主口径「(项)自动化回归测试」+ trustline 变体「自动化测试(护航)」；
//   en "automated regression tests" + trustline 变体 "automated tests"。
const CLAIM_KEYWORD_RES = [/自动化回归测试/, /自动化测试/, /automated (?:regression )?tests/i];
// 数字抽取：`1100+`（允许数字与 + 之间有空白），以及 stats 结构的拆写形 `"1100", suffix: "+"`。
const CLAIM_NUM_RES = [/(\d{3,})\s*\+/g, /(\d{3,})["']\s*,\s*suffix:\s*["']\+/g];

/** 递归统计目录下的测试文件数（防御性跳过 __pycache__ 与隐藏目录）。 */
function countTestFiles(dir) {
  let count = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "__pycache__" || entry.name.startsWith(".")) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) count += countTestFiles(full);
    else if (TEST_FILE_RE.test(entry.name)) count++;
  }
  return count;
}

function checkTestCountRatchet() {
  const warnings = [];
  const problems = [];

  // 宣传侧：解析 lib/content.ts 里的测试数
  const contentSrc = readFileSync(join(websiteRoot, "lib", "content.ts"), "utf8");
  const claims = [];
  contentSrc.split(/\r?\n/).forEach((line, idx) => {
    if (!CLAIM_KEYWORD_RES.some((re) => re.test(line))) return;
    for (const re of CLAIM_NUM_RES) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(line)) !== null) {
        claims.push({ line: idx + 1, n: Number(m[1]) });
      }
    }
  });
  if (claims.length === 0) {
    warnings.push(
      "lib/content.ts 里没解析到「N+ 自动化(回归)测试」形态的宣传数 —— 措辞可能已改写，请人工核对并同步本门禁的解析正则"
    );
    return { warnings, problems, claims, actual: null, breakdown: [] };
  }

  // 实际侧：递归计数双引擎测试文件
  const breakdown = [];
  let actual = 0;
  for (const spec of TEST_DIRS) {
    const abs = join(websiteRoot, ...spec.segments);
    if (!existsSync(abs) || !statSync(abs).isDirectory()) {
      warnings.push(
        `测试目录不存在：engines/${spec.label}/tests（部署机只检出 website/ 时属预期）—— 跳过测试数比对`
      );
      return { warnings, problems, claims, actual: null, breakdown: [] };
    }
    const c = countTestFiles(abs);
    breakdown.push(`${spec.label} ${c}`);
    actual += c;
  }

  // ratchet 断言：每个宣传数 N ≤ 实际数 M
  for (const claim of claims) {
    if (claim.n > actual) {
      problems.push(
        `lib/content.ts:${claim.line}  宣传 ${claim.n}+ vs 实际 ${actual} —— 宣传测试数不得超过实际测试文件数`
      );
    }
  }
  return { warnings, problems, claims, actual, breakdown };
}

// ---------------------------------------------------------------------------
// 检查 5：发布事实源一致性（chatxContent.ts ⇄ public/downloads/manifest.json）
// ---------------------------------------------------------------------------
// manifest.json 由 scripts/gen-chatx-manifest.ps1 在打包后生成，是「实际上架了什么」的实况；
// chatxContent.ts 是构建时字面量，同时喂页面兜底文案与 SoftwareApplication JSON-LD。
//
// 刻意软失败（warning 而非 fail）的两种情况：
//   - manifest.json 不存在：website/.gitignore 忽略整个 /public/downloads（442MB 安装包不进
//     仓库），所以全新 clone 与只检出 website/ 的部署机上本来就没有这个文件；
//   - manifest.json 解析失败：坏文件不该让整条 prebuild 停摆，但要显式喊出来。
//
// 刻意**不**断言「filename 指向的 exe 在本地存在」：大文件由发布流程单独上传到服务器，
// 本地/CI 常态缺失（2026-08-28 实测本地无 1.0.58 而线上 HEAD 200），断言必然误报。
const manifestPath = join(publicDir, "downloads", "manifest.json");
const chatxContentPath = join(websiteRoot, "lib", "chatxContent.ts");

/** 抽 `key: "value"` 的首个匹配（download 块在文件最前，同名 key 在其后不再出现字面量形）。 */
function firstStringField(src, key) {
  const m = new RegExp(`^\\s*${key}\\s*:\\s*"([^"]*)"`, "m").exec(src);
  return m ? m[1] : null;
}

function checkReleaseFacts() {
  const warnings = [];
  const problems = [];
  const facts = {};

  if (!existsSync(chatxContentPath)) {
    problems.push("缺 lib/chatxContent.ts —— 下载页事实源文件不存在（被移动/删除时须同步本门禁）");
    return { warnings, problems, facts };
  }
  const src = readFileSync(chatxContentPath, "utf8");

  const version = firstStringField(src, "version");
  const filename = firstStringField(src, "filename");
  const sha256 = firstStringField(src, "sha256");
  // size: { zh: "442 MB", en: "442 MB" }
  const sizeM = /^\s*size\s*:\s*\{\s*zh:\s*"([^"]*)"\s*,\s*en:\s*"([^"]*)"\s*\}/m.exec(src);
  // url: `${CHATX_RELEASE_BASE}/ChatX-Setup-1.0.58.exe`（模板串，与 182 行的页面 url 靠基址锚点区分）
  const urlM = /url:\s*`\$\{CHATX_RELEASE_BASE\}\/([^`]+)`/.exec(src);

  const missingFields = [];
  if (!version) missingFields.push("version");
  if (!filename) missingFields.push("filename");
  if (!sha256) missingFields.push("sha256");
  if (!sizeM) missingFields.push("size");
  if (!urlM) missingFields.push("url");
  if (missingFields.length > 0) {
    problems.push(
      `lib/chatxContent.ts 里解析不到 CHATX.download 字段：${missingFields.join(", ")} —— 源码形状变更，请同步本门禁的解析正则`
    );
    return { warnings, problems, facts };
  }
  Object.assign(facts, { version, filename, sha256, sizeZh: sizeM[1], sizeEn: sizeM[2], urlFile: urlM[1] });

  // 5a 内部自洽（不依赖 manifest，任何环境都能查）
  if (urlM[1] !== filename) {
    problems.push(`download.url 指向 ${urlM[1]} 但 download.filename 是 ${filename} —— 改文件名时漏改 url`);
  }
  const expectName = `ChatX-Setup-${version}.exe`;
  if (filename !== expectName) {
    problems.push(`download.filename=${filename} 与 version=${version} 不匹配（期望 ${expectName}）—— 升版本时漏改文件名`);
  }
  if (sizeM[1] !== sizeM[2]) {
    problems.push(`download.size 中英不一致（zh="${sizeM[1]}" en="${sizeM[2]}"）—— 同一个安装包体积不该有两个值`);
  }
  if (!/^[0-9a-f]{64}$/i.test(sha256)) {
    problems.push(`download.sha256 不是 64 位十六进制：${sha256}`);
  }

  // 5b 与打包实况比对（manifest 缺失属预期环境差异 → warning）
  if (!existsSync(manifestPath)) {
    warnings.push(
      "public/downloads/manifest.json 不存在（/public/downloads 已被 .gitignore，全新 clone 与只检出 website/ 的部署机属预期）—— 跳过与打包实况的比对，仅完成内部自洽检查"
    );
    return { warnings, problems, facts };
  }
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (err) {
    warnings.push(`public/downloads/manifest.json 解析失败（${err.message}）—— 跳过比对，但请检查打包脚本产物`);
    return { warnings, problems, facts };
  }
  facts.manifestVersion = manifest.version;

  if (String(manifest.version ?? "") !== version) {
    problems.push(
      `版本漂移：chatxContent.ts=${version} vs manifest.json=${manifest.version} —— JSON-LD 会把落后的版本号喂给爬虫/AI`
    );
  }
  if (String(manifest.filename ?? "") !== filename) {
    problems.push(`文件名漂移：chatxContent.ts=${filename} vs manifest.json=${manifest.filename}`);
  }
  if (String(manifest.sha256 ?? "").toLowerCase() !== sha256.toLowerCase()) {
    problems.push(
      `SHA-256 漂移：chatxContent.ts=${sha256.slice(0, 12)}… vs manifest.json=${String(manifest.sha256 ?? "").slice(0, 12)}… —— 用户按页面校验和核对会失败`
    );
  }
  // 体积："442 MB" ⇄ size_mb "442"（只比数字，容忍单位写法差异）
  const claimedMb = /(\d+(?:\.\d+)?)/.exec(sizeM[1]);
  const actualMb = /(\d+(?:\.\d+)?)/.exec(String(manifest.size_mb ?? ""));
  if (claimedMb && actualMb && claimedMb[1] !== actualMb[1]) {
    problems.push(`体积漂移：chatxContent.ts="${sizeM[1]}" vs manifest.json size_mb="${manifest.size_mb}"`);
  }
  return { warnings, problems, facts };
}

// ---------------------------------------------------------------------------
// 检查 6：版本更新记录覆盖（lib/chatx-release-notes.json ⇄ manifest.json）
// ---------------------------------------------------------------------------
// 官网发布页 /download/chatx/releases 的数据源。执行「每次发包都生成 changelog 条目」：
// 结构合法性任何环境都查；覆盖比对依赖 manifest（缺失=部署机预期，warning 跳过）。
const changelogPath = join(websiteRoot, "lib", "chatx-release-notes.json");
const CHANGELOG_TAGS = new Set(["feature", "fix", "improve", "security"]);

function checkChangelogCoverage() {
  const warnings = [];
  const problems = [];
  let latest = null;

  if (!existsSync(changelogPath)) {
    problems.push("缺 lib/chatx-release-notes.json —— 官网版本更新记录数据源不存在（被移动/删除时须同步本门禁）");
    return { warnings, problems, latest };
  }
  let notes;
  try {
    notes = JSON.parse(readFileSync(changelogPath, "utf8"));
  } catch (err) {
    problems.push(`lib/chatx-release-notes.json 解析失败（${err.message}）`);
    return { warnings, problems, latest };
  }
  if (!Array.isArray(notes) || notes.length === 0) {
    problems.push("lib/chatx-release-notes.json 应为非空数组");
    return { warnings, problems, latest };
  }

  // 结构合法性（逐条）
  const versions = new Set();
  notes.forEach((n, i) => {
    const at = `#${i} (v${n?.version ?? "?"})`;
    if (!/^\d+\.\d+\.\d+$/.test(String(n?.version ?? ""))) problems.push(`changelog ${at} version 不是语义化版本`);
    else versions.add(n.version);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(n?.date ?? ""))) problems.push(`changelog ${at} date 不是 YYYY-MM-DD`);
    if (!n?.title?.zh || !n?.title?.en) problems.push(`changelog ${at} title 缺 zh/en`);
    if (!Array.isArray(n?.tags) || n.tags.length === 0 || !n.tags.every((t) => CHANGELOG_TAGS.has(t)))
      problems.push(`changelog ${at} tags 非法（须为 feature|fix|improve|security 非空数组）`);
    const zh = n?.highlights?.zh, en = n?.highlights?.en;
    if (!Array.isArray(zh) || zh.length === 0 || !Array.isArray(en) || en.length === 0)
      problems.push(`changelog ${at} highlights.zh / highlights.en 须为非空数组（双语站不缺一边）`);
  });
  latest = notes[0]?.version ?? null;

  // 覆盖比对：manifest 的已发布版本必须在 changelog 里（缺 manifest = 部署机预期，跳过）
  if (existsSync(manifestPath)) {
    try {
      const mv = String(JSON.parse(readFileSync(manifestPath, "utf8")).version ?? "").trim();
      if (mv && !versions.has(mv)) {
        problems.push(
          `已发布 v${mv}（manifest.json）但 lib/chatx-release-notes.json 缺该版本条目 —— 发包时请跑 node scripts/gen-chatx-changelog.mjs add --version ${mv} ...`
        );
      }
    } catch (err) {
      warnings.push(`manifest.json 解析失败（${err.message}）—— 跳过 changelog 覆盖比对`);
    }
  } else {
    warnings.push("public/downloads/manifest.json 不存在（部署机预期）—— 仅校验 changelog 结构，跳过覆盖比对");
  }
  return { warnings, problems, latest };
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------
let failed = false;

console.log("[gate:content] ── 检查 1/6 静态资产存在性 ──");
const assets = checkAssets();
console.log(
  `[gate:content] 扫描 ${assets.fileCount} 个源文件，命中 ${assets.refCount} 处本地资产引用`
);
if (assets.missing.length > 0) {
  // 违规清单统一走 stdout：console.error 与 console.log 混用时终端里两条流会乱序，
  // 清单看起来像串到下一节；失败信号由末尾 FAIL 行（stderr）+ exit 1 承担。
  failed = true;
  console.log(`[gate:content] ✗ ${assets.missing.length} 处引用的资产在 public/ 下不存在：`);
  for (const miss of assets.missing) {
    console.log(`  - ${miss.file}:${miss.line}  ${miss.path}`);
  }
} else {
  console.log("[gate:content] ✓ 资产引用全部存在");
}

console.log("[gate:content] ── 检查 2/6 价格一致性（事实源 product-facts.json）──");
const prices = checkPrices();
console.log(`[gate:content] 比对 ${prices.checked} 项价格断言`);
if (prices.problems.length > 0) {
  failed = true;
  console.log(`[gate:content] ✗ ${prices.problems.length} 项价格断言失败：`);
  for (const problem of prices.problems) console.log(`  - ${problem}`);
} else {
  console.log("[gate:content] ✓ 价格与事实源一致");
}

console.log("[gate:content] ── 检查 3/6 禁用宣传语黑名单 ──");
const banned = checkBannedClaims();
if (banned.length > 0) {
  failed = true;
  console.log(`[gate:content] ✗ ${banned.length} 处命中禁用宣传语：`);
  for (const hit of banned) {
    console.log(`  - ${hit.file}:${hit.line}  [${hit.term}]  ${hit.excerpt}`);
  }
} else {
  console.log("[gate:content] ✓ 无禁用宣传语");
}

console.log("[gate:content] ── 检查 4/6 测试数 ratchet（宣传数 ≤ 实际测试文件数）──");
const testCount = checkTestCountRatchet();
for (const w of testCount.warnings) console.log(`[gate:content] ⚠ ${w}`);
if (testCount.actual != null) {
  // 实际数常驻输出：季度复审直接读这一行决定要不要上调宣传数字
  const claimedDesc = [...new Set(testCount.claims.map((c) => c.n))].map((n) => `${n}+`).join(", ");
  console.log(
    `[gate:content] [test-count] 实际 ${testCount.actual}（${testCount.breakdown.join(" + ")}），宣传 ${claimedDesc}（${testCount.claims.length} 处）`
  );
}
if (testCount.problems.length > 0) {
  failed = true;
  console.log(`[gate:content] ✗ ${testCount.problems.length} 处宣传测试数超过实际：`);
  for (const problem of testCount.problems) console.log(`  - ${problem}`);
} else if (testCount.actual != null) {
  console.log("[gate:content] ✓ 宣传测试数未超过实际");
} else {
  console.log("[gate:content] ⚠ 测试数 ratchet 本轮跳过（原因见上方 warning）");
}

console.log("[gate:content] ── 检查 5/6 发布事实源一致性（chatxContent.ts ⇄ downloads/manifest.json）──");
const release = checkReleaseFacts();
for (const w of release.warnings) console.log(`[gate:content] ⚠ ${w}`);
if (release.facts.version) {
  // 实况常驻输出：发版复核直接读这一行确认三处（页面兜底/JSON-LD/打包清单）同版本
  const mv = release.facts.manifestVersion ? `，manifest ${release.facts.manifestVersion}` : "";
  console.log(
    `[gate:content] [release] chatxContent ${release.facts.version} / ${release.facts.filename} / ${release.facts.sizeZh} / sha256 ${release.facts.sha256.slice(0, 12)}…${mv}`
  );
}
if (release.problems.length > 0) {
  failed = true;
  console.log(`[gate:content] ✗ ${release.problems.length} 项发布事实源不一致：`);
  for (const problem of release.problems) console.log(`  - ${problem}`);
} else {
  console.log("[gate:content] ✓ 发布事实源一致");
}

console.log("[gate:content] ── 检查 6/6 版本更新记录覆盖（chatx-release-notes.json ⇄ manifest.json）──");
const changelog = checkChangelogCoverage();
for (const w of changelog.warnings) console.log(`[gate:content] ⚠ ${w}`);
if (changelog.latest) console.log(`[gate:content] [changelog] 最新条目 v${changelog.latest}`);
if (changelog.problems.length > 0) {
  failed = true;
  console.log(`[gate:content] ✗ ${changelog.problems.length} 项版本更新记录问题：`);
  for (const problem of changelog.problems) console.log(`  - ${problem}`);
} else {
  console.log("[gate:content] ✓ 版本更新记录结构合法且覆盖已发布版本");
}

if (failed) {
  // FAIL 行同样走 stdout 保证与清单顺序一致；机器信号 = exit 1。
  console.log("[gate:content] 结果：FAIL —— 修复以上清单或更新事实源后重跑 npm run gate:content");
  process.exit(1);
}
console.log("[gate:content] 结果：PASS");
