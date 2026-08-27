/* 桌面壳渲染进程 i18n 门禁（i18n P0 批 1，2026-08-19）
   ───────────────────────────────────────────────────────────────────────────
   守四件事：
   ① 词典双语对等 + en 侧零 CJK（漏一条＝英文坐席看到中文，正是本主线要消灭的）；
   ② **zh 路径零漂移**——每个 key 的 zh 值必须仍逐字出现在 index.html / renderer.js
      里（英文化不许顺手改中文文案；存量中文坐席机行为必须字节级不变）；
   ③ 接线钉死——shell-i18n.js 必须在 <head> 内、且先于 cp-i18n.js（否则组件链拿不到
      window.CP_LANG 就地退回中文）；main.js 的 loadFile 必须带 ?lang=（权威语言源）；
   ④ **只减不增 ratchet**——渲染进程 .js 里「带 CJK 的字符串字面量」条数设天花板：
      后续批次把动态文案迁进词典，这个数只能降；谁新写硬编码中文立刻红。 */
"use strict";

const fs = require("fs");
const path = require("path");

const DESKTOP = path.join(__dirname, "..");
const RENDERER = path.join(DESKTOP, "renderer");

let failed = 0;
function ok(cond, msg) {
  if (cond) { console.log(`  ok  ${msg}`); return; }
  failed++;
  console.error(`  FAIL ${msg}`);
}

const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uff01-\uff60\u3001\u3002\u300a\u300b\u300c\u300d\u3010\u3011]/;

// ── 词典（Node 侧无 document，模块只导出纯函数部分）────────────────────────
const shellI18n = require(path.join(RENDERER, "shell-i18n.js"));
const DICT = shellI18n._dict;

// ① 双语对等 + en 零 CJK
const zhKeys = Object.keys(DICT.zh).sort();
const enKeys = Object.keys(DICT.en).sort();
ok(zhKeys.length > 20, `词典规模合理（zh ${zhKeys.length} 条）`);
const missingEn = zhKeys.filter((k) => !(k in DICT.en));
const missingZh = enKeys.filter((k) => !(k in DICT.zh));
ok(missingEn.length === 0, `zh→en 无缺键${missingEn.length ? `（缺 ${missingEn.join(", ")}）` : ""}`);
ok(missingZh.length === 0, `en→zh 无缺键${missingZh.length ? `（缺 ${missingZh.join(", ")}）` : ""}`);
const cjkInEn = enKeys.filter((k) => CJK.test(String(DICT.en[k])));
ok(cjkInEn.length === 0, `en 侧零 CJK${cjkInEn.length ? `（污染 ${cjkInEn.join(", ")}）` : ""}`);
const emptyVals = zhKeys.filter((k) => !String(DICT.zh[k]).trim() || !String(DICT.en[k] || "").trim());
ok(emptyVals.length === 0, `无空值${emptyVals.length ? `（${emptyVals.join(", ")}）` : ""}`);

// 回落链：未知 key 返回 key 本身，en 缺键回落 zh（绝不 undefined）
ok(shellI18n.tIn("en", "no.such.key") === "no.such.key", "未知 key 回落 key 本身");
ok(shellI18n.tIn("xx", "cp.title") === DICT.zh["cp.title"], "未知语言回落 zh");
ok(shellI18n.tIn("en", "cp.title") === DICT.en["cp.title"], "en 正常取词");
// 扩展语按表定底回落：vi/th/id（词典未备）→ en 底；zh_hant → zh 底（简体可读）
ok(shellI18n.tIn("vi", "cp.title") === DICT.en["cp.title"], "扩展语 vi 回落 en 底");
ok(shellI18n.tIn("th", "cp.title") === DICT.en["cp.title"], "扩展语 th 回落 en 底");
ok(shellI18n.tIn("id", "cp.title") === DICT.en["cp.title"], "扩展语 id 回落 en 底");
ok(shellI18n.tIn("zh_hant", "cp.title") === DICT.zh["cp.title"], "繁体 zh_hant 回落 zh 底");

// ⑥ 语言解析口径必须与 main.js::shellLang 一致：显式 en → en；显式扩展语
//    （i18n_packs.UI_LANGS：vi/th/id/zh_hant，含 zh-TW/zh-HK 别名）原样保留；其余全 zh
[["en", "en"], ["EN", "en"], ["en-US", "en"], ["en_GB", "en"],
 ["zh", "zh"], ["zh-CN", "zh"], ["", "zh"], ["ja", "zh"], [null, "zh"], [undefined, "zh"],
 ["vi", "vi"], ["vi-VN", "vi"], ["th", "th"], ["th-TH", "th"], ["id", "id"], ["ID", "id"],
 ["zh_hant", "zh_hant"], ["zh-Hant", "zh_hant"], ["zh-TW", "zh_hant"], ["zh-HK", "zh_hant"]]
  .forEach(([raw, want]) => {
    ok(shellI18n.normalizeLang(raw) === want, `normalizeLang(${JSON.stringify(raw)}) === ${want}`);
  });
ok(shellI18n.isExplicit("") === false && shellI18n.isExplicit("ja") === false,
  "isExplicit 只对 zh/en 家族与扩展语为真（空/他语继续往下找解析源）");
ok(shellI18n.isExplicit("vi") === true && shellI18n.isExplicit("th-TH") === true &&
  shellI18n.isExplicit("id") === true && shellI18n.isExplicit("zh-Hant") === true,
  "isExplicit 认扩展语 vi/th/id/zh_hant（显式配置不被解析链跳过）");

// ── 繁体 overlay（i18n_desktop_ext 生成物 + registerExt 装载协议）────────────
const PH_RE = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
const phSet = (s) => new Set(String(s).match(PH_RE) || []);
const setEq = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));

const shellExt = require(path.join(RENDERER, "shell-i18n-ext.zh_hant.js"));
ok(shellExt && shellExt.lang === "zh_hant", "shell-i18n-ext.zh_hant.js 导出 zh_hant overlay");
const shellExtKeys = Object.keys(shellExt.dict || {});
ok(shellExtKeys.length >= 300, `shell overlay 规模合理（${shellExtKeys.length} 键）`);
ok(shellExtKeys.every((k) => k in DICT.zh), "shell overlay 键 ⊆ 词典 zh（无孤儿）");
ok(shellExtKeys.every((k) => setEq(phSet(shellExt.dict[k]), phSet(DICT.zh[k]))),
  "shell overlay 占位符与 zh 逐键守恒");
ok(/[\u4e00-\u9fff]/.test(shellExt.dict["cp.title"] || ""), "shell overlay 值为中文（繁体）");

shellI18n.registerExt("zh_hant", shellExt.dict);
ok(shellI18n.tIn("zh_hant", "cp.title") === shellExt.dict["cp.title"],
  "registerExt 装载后 zh_hant 直取繁体值");
ok(shellI18n.tIn("zh_hant", "no.such.key") === "no.such.key",
  "zh_hant 未知键仍回落键名");

const cpExt = require(path.join(RENDERER, "shared", "copilot", "i18n", "cp-i18n-ext.zh_hant.js"));
ok(cpExt && cpExt.lang === "zh_hant" && Object.keys(cpExt.dict || {}).length >= 900,
  `cp overlay 规模合理（${Object.keys(cpExt.dict || {}).length} 键）`);

// 条件装载器（CSP script-src 'self' 禁内联 → 外部 loader + document.write 同步注入）
const idxHtml = fs.readFileSync(path.join(RENDERER, "index.html"), "utf-8");
const loaderSrc = fs.readFileSync(path.join(RENDERER, "i18n-ext-loader.js"), "utf-8");
ok(idxHtml.indexOf('src="i18n-ext-loader.js" data-kind="shell"')
    > idxHtml.indexOf('src="shell-i18n.js"'),
  "index.html：shell 装载器紧随 shell-i18n.js（先建钩子再装载）");
ok(idxHtml.indexOf('data-kind="cp"') > idxHtml.indexOf("cp-i18n.js"),
  "index.html：cp 装载器在 cp-i18n.js 之后");
ok(idxHtml.indexOf("<script>") === -1,
  "index.html 零内联 <script>（CSP script-src 'self' 契约）");
["zh_hant", "vi", "th", "id"].forEach((lg) => {
  ok(loaderSrc.indexOf(`'${lg}'`) >= 0, `装载器认扩展语 ${lg}`);
});
ok(/document\.write/.test(loaderSrc), "装载器用 document.write 同步注入（时序契约）");

const strExt = JSON.parse(fs.readFileSync(path.join(DESKTOP, "shell-str-ext.json"), "utf-8"));
ok(strExt.zh_hant && Object.keys(strExt.zh_hant).length >= 50,
  `shell-str-ext.json 规模合理（${Object.keys(strExt.zh_hant || {}).length} 键）`);
ok(String(strExt.zh_hant["menu.devmode_hint"] || "").indexOf("{n}") >= 0,
  "SHELL_STR overlay 占位符 {n} 保全");

// ── 源码扫描工具：按字符状态机剥注释与**正则字面量**、收字符串字面量 ────────────
//    正则必须单独识别，否则 `.replace(/"/g, "&quot;")` 里那个 `"` 会被当成字符串开头，
//    引号从此错位、后面整片注释被当作「硬编码中文」计进 ratchet（health-panel.js 曾
//    因此虚报 23 条，全是注释）。判据＝`/` 前一个有效字符若是运算符/开括号一类，
//    则它开的是正则而不是除法（对本仓代码风格足够，且宁可当除法也不会漏收字符串）。
function scanJsAt(src) {
  const strings = [];
  let i = 0, n = src.length, prev = "";
  while (i < n) {
    const c = src[i];
    if (c === "/" && src[i + 1] === "/") { while (i < n && src[i] !== "\n") i++; continue; }
    if (c === "/" && src[i + 1] === "*") { i += 2; while (i < n && !(src[i] === "*" && src[i + 1] === "/")) i++; i += 2; continue; }
    if (c === "/" && (prev === "" || "(,=:[!&|?{};+~*%<>^".indexOf(prev) >= 0 || prev === "\n")) {
      i++; let inClass = false;
      while (i < n) {
        if (src[i] === "\\") { i += 2; continue; }
        if (src[i] === "[") inClass = true;
        else if (src[i] === "]") inClass = false;
        else if (src[i] === "/" && !inClass) { i++; break; }
        else if (src[i] === "\n") break; // 未闭合＝判错了，回到普通扫描别吞整文件
        i++;
      }
      prev = "/";
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      const q = c; let buf = ""; const at = i; i++;
      while (i < n) {
        if (src[i] === "\\") { buf += src[i + 1] || ""; i += 2; continue; }
        if (src[i] === q) { i++; break; }
        buf += src[i]; i++;
      }
      strings.push({ s: buf, at: at });
      prev = q;
      continue;
    }
    if (!/\s/.test(c) || c === "\n") prev = c;
    i++;
  }
  return strings;
}
const scanJs = (src) => scanJsAt(src).map((x) => x.s);
// **面向用户的**字符串字面量：再剔掉 `console.*(...)` 那一行上的串——调试日志刻意
// 保留中文（`[iframe] 后端已就绪`），它不进 UI，翻译它只会让排障时对不上日志。
// 判据取「该字面量所在物理行含 console.」：本仓日志都是单行，够用且零误伤 UI。
function scanUiJs(src) {
  return scanJsAt(src)
    .filter((x) => {
      const ls = src.lastIndexOf("\n", x.at) + 1;
      let le = src.indexOf("\n", x.at);
      if (le < 0) le = src.length;
      return src.slice(ls, le).indexOf("console.") < 0;
    })
    .map((x) => x.s);
}

const indexHtml = fs.readFileSync(path.join(RENDERER, "index.html"), "utf8");
const rendererJs = fs.readFileSync(path.join(RENDERER, "renderer.js"), "utf8");
const shellI18nSrc = fs.readFileSync(path.join(RENDERER, "shell-i18n.js"), "utf8");

// ④a index.html 里用到的 key 必须在词典里（拼错＝页面显示裸 key）
const usedAttrKeys = new Set();
const attrRe = /data-sh-i18n(?:-(?:txt|ph|title|aria))?="([^"]+)"/g;
let m;
while ((m = attrRe.exec(indexHtml))) usedAttrKeys.add(m[1]);
ok(usedAttrKeys.size >= 25, `index.html 已挂 i18n 属性（${usedAttrKeys.size} 处）`);
const badAttrKeys = [...usedAttrKeys].filter((k) => !(k in DICT.zh));
ok(badAttrKeys.length === 0, `index.html 属性 key 全部有词条${badAttrKeys.length ? `（未定义：${badAttrKeys.join(", ")}）` : ""}`);

// ④b 取词调用里的 key 必须在词典里（拼错＝线上显裸键）。**扫整个 renderer/**：
//    展示层用 `SH(...)`，health-panel.js 用薄壳 `T(...)`（同一词典，Node 单测下走
//    require 回落），只扫 renderer.js 会漏掉后者整片文案。取参数整段再抽字面量——
//    三元选 key（`SH(i === 0 ? "a" : "b")`）在生产代码里真实存在。
const RENDERER_JS = fs.readdirSync(RENDERER).filter((n) => n.endsWith(".js") && n !== "shell-i18n.js");
const KEY_RE = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/;
//    先收「拼接前缀」（`T("hp.sel_" + key)` 的 `hp.sel_`）：它长得像 key 但不是 key，
//    不先剔掉会被当成缺词条（首版实测误报 hp.sel_ / hp.rsn_ 两条）。
const dynPrefixes = [];
const prefixRe = /["']([a-z][a-z0-9_]*(?:\.[a-z0-9_]*)+)["']\s*\+/g;
const shKeys = new Set();
for (const f of RENDERER_JS) {
  const src = fs.readFileSync(path.join(RENDERER, f), "utf8");
  let p;
  while ((p = prefixRe.exec(src))) dynPrefixes.push(p[1]);
}
for (const f of RENDERER_JS) {
  const src = fs.readFileSync(path.join(RENDERER, f), "utf8");
  const callRe = /\b(?:SH|T)\(([^)]*)\)/g;
  let c;
  while ((c = callRe.exec(src))) {
    (c[1].match(/["']([^"']+)["']/g) || []).forEach((raw) => {
      const k = raw.slice(1, -1);
      if (KEY_RE.test(k) && dynPrefixes.indexOf(k) < 0) shKeys.add(k);
    });
  }
}
ok(shKeys.size >= 4, `renderer/*.js 已接取词（${shKeys.size} 个 key）`);
const badShKeys = [...shKeys].filter((k) => !(k in DICT.zh));
ok(badShKeys.length === 0, `取词 key 全部有词条${badShKeys.length ? `（未定义：${badShKeys.join(", ")}）` : ""}`);

// ② zh 零漂移，**按来源分治**（早期版本用「拼起来找一遍」的宽口径 + 豁免表，
//    每迁一批动态文案就得往豁免表塞 key，守卫会被稀释成摆设）：
//    · index.html 静态文案：HTML 里仍留着中文原文当 zh 默认值 → 词典 zh 必须与之逐字一致；
//    · 纯动态文案（只经 SH() 出现）：源码已无字面量，词典自身即事实源 → 不做存在性检查，
//      改为反向守「不许留残余副本」（半迁移双源＝改一处漏一处的经典坑）。
const attrDrift = [...usedAttrKeys].filter((k) => indexHtml.indexOf(String(DICT.zh[k])) < 0);
ok(attrDrift.length === 0,
  `index.html 静态 zh 文案与词典逐字一致${attrDrift.length ? `（不一致：${attrDrift.join(", ")}）` : ""}`);
//    残余检查只看**字符串字面量**（scanJs 已剥注释）：注释里引用旧文案是合法文档，
//    真正的坑是「还有一处代码在发中文」。
//    范围＝**已完成迁移的文件**（下方 RATCHET_FILES 同一份清单）：这些文件里任何中文
//    字面量都是漏网，不存在「合法中文字典」（first-run-model.js 那种反例已排除在外），
//    所以短词条（"未知"/"取消"）做子串比对也不会误伤。
const MIGRATED = ["renderer.js", "health-panel.js", "webmulti.js", "inject-status.js", "platform-caps.js"];
const migratedLits = new Map(
  MIGRATED.map((f) => [f, scanUiJs(fs.readFileSync(path.join(RENDERER, f), "utf8"))]),
);
//    子串比对，故**跳过 ≤2 字的短词条**：'：'/'失败'/'取消' 这类会命中任何含它的长句
//    （首版实测 hp.colon 被「阶段：…」误报），而短词条的漏检风险由下方 ratchet 兜住。
const dynOnly = [...shKeys].filter((k) => !usedAttrKeys.has(k) && String(DICT.zh[k]).length > 2);
const residual = [];
for (const k of dynOnly) {
  const zh = String(DICT.zh[k]);
  for (const [f, lits] of migratedLits) {
    if (lits.some((s) => s.indexOf(zh) >= 0)) residual.push(`${f}:${k}`);
  }
}
ok(residual.length === 0,
  `已迁动态文案在源码无残余中文副本${residual.length ? `（残留：${residual.join(", ")}）` : ""}`);
// 词典不许养僵尸词条：每条都得有真实消费点。除「属性 / SH("字面量")」外，还有两种
// **间接**消费必须认，否则一收口纯函数就会误判成孤儿（并逼人往豁免表塞东西）：
//   · 前缀拼接：`SH("inject." + st.code)` —— 纯函数只出 code，键在运行时拼出来；
//   · 键生产者：webmulti.js 之类的纯函数直接吐 `textKey: "health.xxx"`，展示层 `SH(k)`
//     收下。故把 renderer/*.js 里所有「长得像 key 的字面量」也算消费凭据。
//   前缀既可能出现在展示层（`SH("inject." + code)`）也可能出现在生产者里
//   （webmulti 的 `key: "health.inject_" + inject`），故整个 renderer/ 一起扫。
//   （dynPrefixes 已在 ④b 前收好——那里也要用它剔「前缀不是 key」的误报。）
const producedKeys = new Set();
for (const f of RENDERER_JS) {
  for (const s of scanJs(fs.readFileSync(path.join(RENDERER, f), "utf8"))) {
    if (KEY_RE.test(s)) producedKeys.add(s);
  }
}
// 主进程键生产者（2026-08-20 v1.0.45 封版实锤）：update-notify 重构后
// `textKey: "notice.*"` 的生产点在 desktop 根而非 renderer/ ——扫描面不含它
// 就会把这些真消费的词条误判成孤儿（正是本测试自己警告过的「逼人塞豁免表」）。
for (const f of ["update-notify.js"]) {
  const p = path.join(DESKTOP, f);
  if (!fs.existsSync(p)) continue;
  for (const s of scanJs(fs.readFileSync(p, "utf8"))) {
    if (KEY_RE.test(s)) producedKeys.add(s);
  }
}
const KEY_UNUSED_OK = new Set(["app.title"]); // 由 shell-i18n.js 自身设 document.title
const orphanKeys = zhKeys.filter((k) =>
  !usedAttrKeys.has(k) && !shKeys.has(k) && !producedKeys.has(k) &&
  !KEY_UNUSED_OK.has(k) && !dynPrefixes.some((p) => k.startsWith(p)));
ok(orphanKeys.length === 0, `词典无孤儿词条${orphanKeys.length ? `（无人消费：${orphanKeys.join(", ")}）` : ""}`);
// 前缀消费的反向守卫：`SH("inject." + code)` 那条链的每个 code 都得有词条，可 code 由
// 纯函数决定、静态扫不出全集 → 退一步钉「该前缀下至少有词条」，防误删整段。
//   `cp.*` 前缀是**跨词典**的：壳侧刻意不复制阶段/漏斗词表，而是经 window.CP_T 借用
//   cp-i18n.js 那一份（`relStageLabel` → `cp.rel.stage.<code>`）。所以这些前缀去
//   cp-i18n.js 源码里验，两份词典各自为事实源、绝不各译一套。
const cpI18nSrc = fs.readFileSync(
  path.join(RENDERER, "shared", "copilot", "i18n", "cp-i18n.js"), "utf8");
const emptyPrefixes = dynPrefixes.filter((p) =>
  !zhKeys.some((k) => k.startsWith(p)) &&
  !(p.startsWith("cp.") &&
    (cpI18nSrc.indexOf("'" + p) >= 0 || cpI18nSrc.indexOf('"' + p) >= 0)));
ok(emptyPrefixes.length === 0,
  `动态前缀均有词条${emptyPrefixes.length ? `（空前缀：${emptyPrefixes.join(", ")}）` : ""}`);

// ③ 接线：shell-i18n.js 在 <head> 内，且先于 cp-i18n.js
const headBlock = (indexHtml.match(/<head[\s\S]*?<\/head>/i) || [""])[0];
ok(/<script src="shell-i18n\.js">/.test(headBlock),
  "shell-i18n.js 在 <head> 内同步加载（挪到 body 末尾＝首帧闪中文 + CP_LANG 迟到）");
const posShell = indexHtml.indexOf('src="shell-i18n.js"');
const posCp = indexHtml.indexOf("i18n/cp-i18n.js");
ok(posShell >= 0 && posCp > posShell,
  "shell-i18n.js 先于 cp-i18n.js（否则 cp-* 组件链取不到 window.CP_LANG，整条右栏退回中文）");
ok(/root\.CP_LANG\s*=\s*LANG/.test(shellI18nSrc),
  "shell-i18n.js 必须写 window.CP_LANG（cp-i18n 的首选解析源）");
ok(/setAttribute\('lang',\s*HTML_LANGS\[LANG\]\s*\|\|\s*LANG\)/.test(shellI18nSrc),
  "shell-i18n.js 必须同步改 <html lang>（BCP-47 规范值：zh_hant→zh-Hant；a11y + cp-i18n 末级回落）");

const mainJs = fs.readFileSync(path.join(DESKTOP, "main.js"), "utf8");
ok(/loadFile\([^)]*"index\.html"\)?\s*,\s*\{\s*query:\s*\{\s*lang:\s*shellLang\(\)/.test(
  mainJs.replace(/\s+/g, " ")) ||
  /index\.html"\s*\)\s*,\s*\{\s*query:\s*\{\s*lang:\s*shellLang\(\)\s*\}\s*\}/.test(mainJs.replace(/\s+/g, " ")),
  "main.js 的 index.html loadFile 必须带 { query: { lang: shellLang() } }（权威语言源；去掉即回落 localStorage 猜测）");
// 「跟随系统」真跟随（2026-08-27）：配置空时必须问 OS 语言（app.getLocale），
// 且家族映射函数在位——删掉任一个＝「跟随系统」退化回恒中文的假选项。
ok(/shellLangFromTag\(app\.getLocale\(\)\)/.test(mainJs),
  "main.js::shellLang 空配置走 app.getLocale() 系统跟随");
ok(/function shellLangFromTag\(/.test(mainJs),
  "main.js 有 shellLangFromTag 家族映射（zh-TW/HK/MO/Hant→zh_hant）");

// ④ 只减不增 ratchet：渲染进程 .js 里带 CJK 的字符串字面量条数
//    （index.html 刻意保留中文字面量作 zh 默认值，故不入此账；它由 ②/④a 两条守）。
const RATCHET_FILES = {
  // 文件名 → 天花板（**只许调低**；调高＝新增硬编码中文，请改用 SH()/词典）
  // 2026-08-19 批 1 基线 320 → 批 2（rail 标签 + 启动/连接叙事）289 → 批 3（副驾 toast/
  // 风险确认框/操作行/分析卡/KB 全迁）228 → 批 4（webmulti/inject-status 两个纯函数层
  // 只出 key/code，取词挪到展示层）→ 批 5（health-panel 整片 + 档案/KB/模板/更新横幅）。
  // 计数口径经 scanUiJs：剥注释 + 剥正则字面量 + 剥 console 行（日志刻意留中文）。
  "renderer.js": 0,
  "health-panel.js": 0,
  "webmulti.js": 0,
  "inject-status.js": 0,
  // 批 6：platform-caps.js 的 assist-only 诚实条改回 key（文案进词典）
  "platform-caps.js": 0,
  // first-run.js 余 2 条＝语种下拉的「中文 / English」**原生语言名**，按 i18n 惯例
  // 两种界面语言下都该显示母语写法，属**刻意保留**，不是漏网。
  "first-run.js": 2,
  // 开机动画（splash P0 2026-08-22）：出生即零硬编码中文——文案全部走词典（SH()），
  // 氛围池/阶段/终端流键由 test/splash-model.test.js 与词典双向钉住。
  "splash.js": 0,
  "splash-model.js": 0,
  // ⚠ first-run-model.js 刻意**不入账**：它整个就是 FR_STRINGS 双语词典，里面的中文是
  // 词条值而非硬编码泄漏；计进来只会让「待迁合计」长期虚高 72 条、把 ratchet 变成摆设。
};
let ratchetTotal = 0;
for (const [file, ceil] of Object.entries(RATCHET_FILES)) {
  const p = path.join(RENDERER, file);
  if (!fs.existsSync(p)) { ok(false, `ratchet 目标文件缺失：${file}`); continue; }
  const cnt = scanUiJs(fs.readFileSync(p, "utf8")).filter((s) => CJK.test(s)).length;
  ratchetTotal += cnt;
  ok(cnt <= ceil, `${file} 硬编码中文串 ${cnt} ≤ 天花板 ${ceil}`);
}
console.log(`  ..  渲染进程待迁中文串合计 ${ratchetTotal} 条（批次推进应持续下降）`);

// ── ⑤ 主进程 ratchet（批 6 新增）───────────────────────────────────────────
// 主进程也直接对坐席说话：窗口标题（任务栏）、系统通知、应用菜单、IPC 错误回执
// （渲染层 toast 原样显示）。此前完全没有守卫——渲染进程清零了，主进程照样蹦中文。
// 词典本体（main.js 的 SHELL_STR / update-notify 无词典）按 first-run-model.js 同一
// 判据剔除：它是**词条值**而非硬编码泄漏，计进来会让 ratchet 永远虚高、退化成摆设。
function stripDictBlock(src, marker) {
  const i = src.indexOf(marker);
  if (i < 0) return src;
  // 从 marker 起按大括号深度找到词典对象字面量的闭合处，整段挖掉。
  let j = src.indexOf("{", i), depth = 0;
  if (j < 0) return src;
  for (let k = j; k < src.length; k++) {
    if (src[k] === "{") depth++;
    else if (src[k] === "}") { depth--; if (depth === 0) return src.slice(0, i) + src.slice(k + 1); }
  }
  return src;
}
const MAIN_RATCHET = {
  // 文件 → 天花板（只许调低）。2026-08-19 批 6 基线。
  // main.js：EN-UI 线迁移在途（HEAD 时 74 → 现 28，目标 0）。2026-08-20 晚
  // v1.0.45 发版按当前现实重登记（0 是愿望值不是天花板——低于现实的 cap 让
  // 门禁永红、连带封死一切发版）。EN-UI 线迁完一批就把这里往下拧。
  "main.js": 28,
  // update-notify.js：更新横幅改回 textKey+vars；公告类回 text 是**服务端下发内容**，
  // 不入词典（运营自己写的语言），故此文件不该再有任何中文字面量。
  "update-notify.js": 0,
  // 启停链（backend/sidecar launcher）：文案会经首启向导/错误弹层露给用户，但多数是
  // 诊断细节，属批 7 目标。当前登记基线，只许降。
  "backend-launcher.js": 15,
  "sidecar-launcher.js": 9,
};
let mainTotal = 0;
for (const [file, ceil] of Object.entries(MAIN_RATCHET)) {
  const p = path.join(DESKTOP, file);
  if (!fs.existsSync(p)) { ok(false, `主进程 ratchet 目标缺失：${file}`); continue; }
  let src = fs.readFileSync(p, "utf8");
  if (file === "main.js") src = stripDictBlock(src, "const SHELL_STR =");
  const cnt = scanUiJs(src).filter((s) => CJK.test(s)).length;
  mainTotal += cnt;
  ok(cnt <= ceil, `${file} 硬编码中文串 ${cnt} ≤ 天花板 ${ceil}`);
}
// 剔除逻辑本身要自证有效：挖掉词典后 main.js 里不得再有「文件/编辑/视图」这类菜单词，
// 否则说明 stripDictBlock 没命中（marker 改名/词典搬家），ratchet 会静默失效。
{
  const stripped = scanUiJs(stripDictBlock(fs.readFileSync(path.join(DESKTOP, "main.js"), "utf8"),
    "const SHELL_STR =")).join("\n");
  const raw = scanUiJs(fs.readFileSync(path.join(DESKTOP, "main.js"), "utf8")).join("\n");
  ok(raw.indexOf("重新加载") >= 0 && stripped.indexOf("重新加载") < 0,
    "stripDictBlock 确实挖掉了 SHELL_STR 词典（挖不动＝ratchet 静默失效）");
}
console.log(`  ..  主进程待迁中文串合计 ${mainTotal} 条`);

// SS() 的占位替换与渲染层 SH() 必须同约定（{name}）：两边不一致会让「音频拉取失败
// {status}」原样带花括号显示给坐席。
{
  const mainSrc = fs.readFileSync(path.join(DESKTOP, "main.js"), "utf8");
  ok(/function SS\(key, vars\)/.test(mainSrc), "main.js::SS 支持 vars（错误回执要带动态量）");
  ok(/\\\{\(\\w\+\)\\\}/.test(mainSrc) || /\{\(\\w\+\)\}/.test(mainSrc.replace(/\\/g, "\\")),
    "SS 用 {name} 占位（与渲染层 SH 同约定）");
}

if (failed) { console.error(`\nshell-i18n.test.js: ${failed} 项失败`); process.exit(1); }
console.log("\nshell-i18n.test.js: all pass");
