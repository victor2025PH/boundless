// package-layout.test.js —— 「包内 require 跨出 app 目录」随包判据（源码静态契约）
//
// 2026-08-10 实锤：`inject/tg-inject.js` 从第一天就 `require("../../shared/inject/*.js")`，
// 而 electron-builder 的 `files` 只收 app 目录（desktop/）——shared/ 压根没进包。
// 于是**开发机一切正常、装机版注入层整体死掉**（preload 第一行 require 抛
// MODULE_NOT_FOUND，点译/双语气泡/会话上报/注入健康遥测全哑），已发的 1.016/1.017
// 都带着这个洞出货，且因为失败在 webview 控制台、后端日志一字不留，没人发现。
// 同期 main.js 顶层也曾 `require("../shared/inject/...")`。extraResources 能把
// 文件放到 resources/shared/inject，但 Electron 31 **主进程**从 app.asar 用 `../`
// 跨出仍 MODULE_NOT_FOUND（1.0.28 装机版双击起不来）。主进程改为 require
// asar 内 `./shared/inject/`（copy-shared 镜像）；preload 的 `../../shared/inject`
// 仍走 extraResources。
//
// 本门禁是那条映射的看门人：**按源码里真实存在的 require 反推包内路径**，逐条要求
// ①有 extraResources 映射覆盖 ②filter 收得进 ③源文件真在 ④after-pack 表里有代表文件。
// 新加一个 shared 模块、改动 from/to、写歪 filter，这里立刻红，而不是等客户装完才知道。
// 挂在 npm test 与 predist/predist:win —— 判据不成立，安装包拒绝出生。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("package-layout FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const appDir = path.join(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(appDir, "package.json"), "utf8"));
const extra = (pkg.build && pkg.build.extraResources) || [];

// 包内布局用两个哨兵根建模：resources/ 与其中的 app.asar/
const RES = "/RES";
const ASAR = RES + "/app.asar";
const toPosix = (p) => p.split(path.sep).join("/");

// extraResources → [{ srcDir(绝对), packDir(包内), filter }]
const mappings = extra
  .filter((e) => e && typeof e.from === "string" && typeof e.to === "string")
  .map((e) => ({
    from: e.from,
    to: e.to,
    srcDir: path.resolve(appDir, e.from),
    packDir: RES + "/" + e.to.replace(/^\/+/, ""),
    filter: Array.isArray(e.filter) ? e.filter : [],
  }));

// filter 是否收得进某个相对路径（只需覆盖本仓在用的两种写法：**/* 与 **/*.ext）
function filterAccepts(filter, rel) {
  if (!filter.length) return true; // 无 filter = 全收
  const positives = filter.filter((f) => !String(f).startsWith("!"));
  if (!positives.length) return true;
  return positives.some((f) => {
    const s = String(f);
    if (s === "**/*" || s === "**") return true;
    const m = /^\*\*\/\*(\.[A-Za-z0-9]+)$/.exec(s);
    if (m) return rel.endsWith(m[1]);
    return s === rel;
  });
}

// 递归收集「会随包的源码 js」：排除测试/构建脚本/产物/依赖，以及 renderer/shared
// （copy-shared 生成的镜像，本就在 app 目录内，不涉及跨出）。
function collectJs(dir, out) {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, ent.name);
    const rel = toPosix(path.relative(appDir, abs));
    if (/^(test|build|dist|dist-.*|node_modules|logs)(\/|$)/.test(rel)) continue;
    if (rel.startsWith("renderer/shared/")) continue;
    if (rel.startsWith("shared/")) continue; // copy-shared 生成的 inject 镜像，不参与跨出扫描
    if (ent.isDirectory()) collectJs(abs, out);
    else if (ent.name.endsWith(".js")) out.push(abs);
  }
  return out;
}

const REQ_RE = /require\(\s*["'](\.\.\/[^"']+)["']\s*\)/g;
const crossing = []; // [{ srcFile, spec, packPath }]

for (const abs of collectJs(appDir, [])) {
  const relFromApp = toPosix(path.relative(appDir, abs));
  const src = fs.readFileSync(abs, "utf8");
  let m;
  REQ_RE.lastIndex = 0;
  while ((m = REQ_RE.exec(src))) {
    const spec = m[1];
    // 包内该文件所在目录 → 解析 require 目标
    const packDirOfFile = path.posix.dirname(ASAR + "/" + relFromApp);
    const packPath = path.posix.normalize(path.posix.join(packDirOfFile, spec));
    if (packPath.startsWith(ASAR + "/")) continue; // 没跨出 asar
    crossing.push({ srcFile: relFromApp, spec, packPath });
  }
}

// 有跨出就必须有覆盖——这是本门禁存在的全部理由，一条都不许漏
for (const c of crossing) {
  ok(
    c.packPath.startsWith(RES + "/"),
    `${c.srcFile} 的 require("${c.spec}") 解析到 ${c.packPath}，已跨出 resources/ ——` +
      "装机版必 MODULE_NOT_FOUND，且开发机永远复现不出"
  );
  const hit = mappings.find((mp) => c.packPath.startsWith(mp.packDir + "/"));
  ok(
    !!hit,
    `${c.srcFile} 的 require("${c.spec}") 在包内指向 ${c.packPath}，` +
      "但 package.json build.extraResources 没有任何映射覆盖它 —— " +
      '补一条 { "from": "../<目录>", "to": "<目录>" }（files 里写 "../" 无效，electron-builder 只收 app 目录）'
  );
  const relInDir = c.packPath.slice(hit.packDir.length + 1);
  ok(
    filterAccepts(hit.filter, relInDir),
    `extraResources[${hit.from}] 的 filter ${JSON.stringify(hit.filter)} 收不进 ${relInDir}` +
      `（${c.srcFile} 要 require 它）`
  );
  ok(
    fs.existsSync(path.join(hit.srcDir, relInDir.split("/").join(path.sep))),
    `映射源缺文件：${path.join(hit.from, relInDir)} 不存在（${c.srcFile} 的 require 会落空）`
  );
}

// 现实锚：本仓当前确实存在这些跨出 require；哪天被重构掉，这条会提醒来删门禁而非默默失效
ok(
  crossing.some((c) => c.spec.includes("shared/inject/core.js")),
  "没扫到 inject 核心的跨目录 require —— 要么重构了（请同步本门禁），要么扫描器坏了"
);
// 1.0.28 实锤：main.js / outbound-pace.js 的 ../shared/inject 跨出 asar 在
// Electron 31 主进程解析失败（文件在 extraResources 里也救不了）。这两处必须
// require 进 asar 内的 copy-shared 镜像；跨出只留给 inject/tg-inject.js 那条
// 已用 preload 验证过的 ../../ 路径。
const mainJs = fs.readFileSync(path.join(appDir, "main.js"), "utf8");
const paceJs = fs.readFileSync(path.join(appDir, "outbound-pace.js"), "utf8");
ok(
  /require\(\s*["']\.\/shared\/inject\/translate-scheduler\.js["']\s*\)/.test(mainJs),
  "main.js 必须 require(\"./shared/inject/translate-scheduler.js\") 留在 asar 内；" +
    "改回 ../shared/inject 会让装机版主进程起不来（1.0.28）"
);
ok(
  /require\(\s*["']\.\/shared\/inject\/human-pace\.js["']\s*\)/.test(paceJs),
  "outbound-pace.js 必须 require(\"./shared/inject/human-pace.js\") 留在 asar 内"
);
ok(
  !crossing.some((c) => c.srcFile === "main.js" || c.srcFile === "outbound-pace.js"),
  "main.js / outbound-pace.js 又出现了跨出 asar 的 require —— 装机版主进程会起不来"
);

// after-pack 交叉核对：跨出的每个目录都得有代表文件进 REQUIRED，
// 否则 extraResources 被改歪时装包照样静默通过。
const afterPack = require(path.join(appDir, "build", "after-pack.js"));
ok(
  Array.isArray(afterPack.REQUIRED),
  "after-pack.js 未导出 REQUIRED（本门禁靠它核对随包判据，别把导出删了）"
);
const requiredRels = afterPack.REQUIRED.map((r) => toPosix(String(r[0])));
const crossedDirs = new Set(
  mappings
    .filter((mp) => crossing.some((c) => c.packPath.startsWith(mp.packDir + "/")))
    .map((mp) => mp.to.replace(/^\/+/, ""))
);
for (const dir of crossedDirs) {
  ok(
    requiredRels.some((r) => r === dir || r.startsWith(dir + "/")),
    `after-pack.js 的 REQUIRED 里没有 ${dir}/ 下的代表文件 —— ` +
      "extraResources 哪天写歪就会静默出坏包（这正是 shared/inject 漏包两个版本没人发现的原因）"
  );
}

// 反向：REQUIRED 里点名的 shared 代表文件必须真能被映射交付
for (const rel of requiredRels) {
  const mp = mappings.find((x) => rel === x.to || rel.startsWith(x.to.replace(/^\/+/, "") + "/"));
  if (!mp) continue; // services 由各自 from 目录交付，不在本门禁口径内
  // backend/ 是 PyInstaller 产物（build_backend.py 产出），源侧判据在下方「必须在」清单里
  // 按 DATAS 源码 + 仓库文件核对，不要求本机已经打过后端。
  if (rel === "backend" || rel.startsWith("backend/")) continue;
  const relInDir = rel === mp.to ? "" : rel.slice(mp.to.length + 1);
  if (!relInDir) continue;
  ok(
    fs.existsSync(path.join(mp.srcDir, relInDir.split("/").join(path.sep))),
    `after-pack 点名的 ${rel} 在映射源 ${mp.from} 里不存在（装包必失败，先修表或修文件）`
  );
}

// ── P-4 #254（MTRCH2①③，2026-09-08）后端随包清单两张：「必须在」与「必须不在」──────
// 1.0.77 clean 包实锤：handoff_scripts.yaml / handoff_compliance.yaml 没进包（转人工链哑），
// 而 fatex.db（命理产品库）在客户机上凭空出现。此前 backend/ 对本门禁是一个不透明映射，
// 什么进了、什么没进只有装到客户机才知道。这里按**源侧契约**核对（不打包、不看 dist）：
//   · 必须在：build_backend.py 的 DATAS 必须登记这些文件 / 目录，且仓库里真有；after-pack
//     REQUIRED 必须有对应的包内路径（打包时再实测一次）。
//   · 必须不在：DATAS 里不得出现 .db / .key / 真实 config.yaml / config.local.yaml / seed-data；
//     after-pack FORBIDDEN 必须点名 fatex.db 等；随包 YAML 里不得有内网地址（10./192.168./172.16-31.）。
// 若本机恰好有 build/backend-dist（打过后端），顺手对产物做一遍同样的存在 / 不存在断言。
const repoRoot = path.join(appDir, "..");
const buildBackendPy = fs.readFileSync(path.join(appDir, "build", "build_backend.py"), "utf8");
const datasBlock = (buildBackendPy.match(/DATAS\s*=\s*\[([\s\S]*?)\n\]/) || ["", ""])[1];
ok(datasBlock.length > 0, "build_backend.py 里找不到 DATAS = [...] 块（门禁口径失效）");

// [DATAS 里的源路径写法, 仓库相对路径, 包内相对路径（after-pack REQUIRED 口径）, 人话]
const MUST_SHIP = [
  ['REPO / "config" / "config.desktop.min.yaml"', "config/config.desktop.min.yaml",
    "backend/_internal/config/config.desktop.min.yaml", "桌面最小种子配置"],
  ['REPO / "config" / "handoff_scripts.yaml"', "config/handoff_scripts.yaml",
    "backend/_internal/config/handoff_scripts.yaml", "转人工话术模板（MTRCH2③）"],
  ['REPO / "config" / "handoff_compliance.yaml"', "config/handoff_compliance.yaml",
    "backend/_internal/config/handoff_compliance.yaml", "转人工合规规则（MTRCH2③）"],
  ['REPO / "config" / "profiles"', "config/profiles",
    null, "部署能力预设档（按 profile 播种）"],
];
for (const [datasLit, repoRel, packRel, what] of MUST_SHIP) {
  ok(datasBlock.includes(datasLit),
    `build_backend.py DATAS 未登记 ${repoRel}（${what}）—— clean 包会缺它，只在客户机 WARNING 里暴露`);
  ok(fs.existsSync(path.join(repoRoot, repoRel.split("/").join(path.sep))),
    `仓库缺 ${repoRel}（${what}）—— DATAS 登记了但源文件不存在，PyInstaller 会直接报错`);
  if (packRel) {
    ok(requiredRels.includes(packRel),
      `after-pack.js REQUIRED 未点名 ${packRel}（${what}）—— 打包时无人核对它是否真进包`);
  }
}
// domains/ 经 _stage_domains 暂存进包（不是 DATAS 常量），源侧核对函数在位 + 清单文件存在
ok(/def _stage_domains\(/.test(buildBackendPy) && /\(_stage_domains\(\),\s*"domains"\)/.test(buildBackendPy),
  "build_backend.py 丢了 domains/ 暂存（_stage_domains → \"domains\"）—— 域包不进包，KB 分类 / 提示词回落硬编码");
ok(fs.existsSync(path.join(repoRoot, "domains", "conversion", "manifest.yaml")),
  "仓库缺 domains/conversion/manifest.yaml");
ok(requiredRels.includes("backend/_internal/domains/conversion/manifest.yaml"),
  "after-pack.js REQUIRED 未点名 domains manifest");

// 必须不在：DATAS 源侧不得出现这些
const MUST_NOT_SHIP_DATAS = [
  [/\.db"/, "任何 .db（fatex.db / knowledge_base.db / credpool 库）"],
  [/\.key"/, "任何 .key（授权私钥）"],
  [/"config\.yaml"/, "构建机真实运行配置 config.yaml"],
  [/"config\.local\.yaml"/, "构建机 overlay config.local.yaml"],
  [/seed-data/, "内测数据种子（走 extraResources 的 seed-data 映射，不进后端产物）"],
  [/"demo"|demo_data|演示数据/, "演示数据"],
];
for (const [re, what] of MUST_NOT_SHIP_DATAS) {
  ok(!re.test(datasBlock), `build_backend.py DATAS 出现了不该随包的 ${what}`);
}
ok(Array.isArray(afterPack.FORBIDDEN), "after-pack.js 未导出 FORBIDDEN（「必须不在」清单无人核对）");
const forbiddenRels = afterPack.FORBIDDEN.map((r) => toPosix(String(r[0])));
for (const rel of ["backend/_internal/config/fatex.db", "seed-data/config/fatex.db",
  "backend/_internal/config/license.key", "backend/_internal/config/config.yaml",
  "backend/_internal/config/config.local.yaml", "backend/_internal/platform/credpool/data",
  "seed-data/config/knowledge_base.db"]) {
  ok(forbiddenRels.includes(rel), `after-pack.js FORBIDDEN 未点名 ${rel}（「必须不在」清单漏项）`);
}
// 随包 YAML 零内网地址（config.desktop.min / handoff 两 yaml / profiles/*.yaml）
const PRIVATE_IP = /\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b/;
const shippedYaml = ["config/config.desktop.min.yaml", "config/handoff_scripts.yaml", "config/handoff_compliance.yaml"]
  .map((r) => path.join(repoRoot, r.split("/").join(path.sep)));
try {
  for (const n of fs.readdirSync(path.join(repoRoot, "config", "profiles"))) {
    if (/\.ya?ml$/.test(n)) shippedYaml.push(path.join(repoRoot, "config", "profiles", n));
  }
} catch (e) { /* profiles 目录缺失已由 MUST_SHIP 报 */ }
for (const f of shippedYaml) {
  if (!fs.existsSync(f)) continue;
  const lines = fs.readFileSync(f, "utf8").split(/\r?\n/);
  const hit = lines.findIndex((ln) => PRIVATE_IP.test(ln.replace(/#.*$/, "")));
  ok(hit < 0, `随包 YAML ${path.relative(repoRoot, f)} 第 ${hit + 1} 行含内网地址（用户版不得带厂商内网）`);
}
// 本机若已打过后端：对产物再核一遍（可选；没有 backend-dist 不算失败）。
// 「必须在」缺件只**警告**：产物可能只是比 DATAS 旧（check_backend_freshness.py 在 predist
// 里会硬拒陈旧产物，refresh_backend_datas.py 可只补数据不重打）；「必须不在」泄漏则硬红。
const backendDist = path.join(appDir, "build", "backend-dist", "_internal");
if (fs.existsSync(backendDist)) {
  for (const [, , packRel, what] of MUST_SHIP) {
    if (!packRel) continue;
    const p = path.join(backendDist, packRel.replace(/^backend\/_internal\//, "").split("/").join(path.sep));
    if (!fs.existsSync(p)) {
      console.warn(`package-layout WARN: 本机 backend-dist 缺 ${packRel}（${what}）—— 产物比 DATAS 旧，` +
        "npm run build:backend 或 python build/refresh_backend_datas.py 后再打包（predist 的 freshness 门禁会硬拒）");
    }
  }
  for (const rel of ["config/fatex.db", "config/license.key", "config/config.yaml", "config/config.local.yaml"]) {
    ok(!fs.existsSync(path.join(backendDist, rel.split("/").join(path.sep))),
      `本机 backend-dist 含不该随包的 ${rel}`);
  }
}

console.log("package-layout.test.js: " + passed + " passed");
