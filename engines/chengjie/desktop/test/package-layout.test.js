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
  if (!mp) continue; // backend/services 由各自 from 目录交付，不在本门禁口径内
  const relInDir = rel === mp.to ? "" : rel.slice(mp.to.length + 1);
  if (!relInDir) continue;
  ok(
    fs.existsSync(path.join(mp.srcDir, relInDir.split("/").join(path.sep))),
    `after-pack 点名的 ${rel} 在映射源 ${mp.from} 里不存在（装包必失败，先修表或修文件）`
  );
}

console.log("package-layout.test.js: " + passed + " passed");
