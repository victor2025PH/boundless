"use strict";

// electron-builder afterPack 钩子：装包**打完之后**核对「该随包的东西真在里面」。
//
// 为什么非要在这一步再验一次：`extraResources` 的 filter 写错、`from` 路径写歪、
// 或者构建机上 services/*/node_modules 压根没装，electron-builder 都**不会报错**——
// 它安安静静产出一个少了边车的安装包。而缺什么在客户机上才暴露：
// 「接入 WhatsApp → 协议多开」点下去等来 service_down（甚至在 0.2.9 之前是直接灰着
// 显示「未启用」）。这类静默漏包本仓已吃过三次（platform/licensing、shared/copilot、
// domains，见 build_backend.py 的注释），每次都是发版后才由客户发现。
//
// 判据与「种子里开了哪些方式」一致，两端由 tests/test_desktop_seed_deliverable.py
// 从源码侧钉住；这里补的是「产物侧」那一半。缺件即抛错 → 打包失败，绝不出坏包。

const fs = require("fs");
const path = require("path");

/** 产物内 resources 目录（mac 在 .app 内，其余在 appOutDir 下）。 */
function resourcesDir(context) {
  const out = context.appOutDir;
  if (context.electronPlatformName === "darwin") {
    const appName = `${context.packager.appInfo.productFilename}.app`;
    return path.join(out, appName, "Contents", "Resources");
  }
  return path.join(out, "resources");
}

// [包内相对路径, 人话说明, 缺了会怎样]
const REQUIRED = [
  ["backend", "后端 sidecar 目录", "桌面壳没有后端可拉起，整个 App 打不开工作台"],
  // shared/inject 不在 app 目录内（单一事实来源在 repo 根），只能经 extraResources 随包。
  // 包内 require 是从 asar 里跨出来打到 resources/shared/inject（app.asar/inject/../../
  // 与 app.asar/../shared 都落在 resources 下，已用 electron 实测确认可加载）。
  [
    path.join("shared", "inject", "core.js"),
    "注入层平台无关核心",
    "webview preload 整体 MODULE_NOT_FOUND：点译/双语气泡/会话上报/注入健康遥测在装机版全哑（开发机能跑，只有装机版坏）",
  ],
  [
    path.join("shared", "inject", "profiles.js"),
    "注入层选择器档案",
    "同上；preload 第一行 require 就抛，注入状态条永远「未生效」",
  ],
  [
    path.join("shared", "inject", "translate-scheduler.js"),
    "翻译批处理调度器",
    "main.js 顶层 require 它 → **主进程起不来**，App 双击无反应",
  ],
  [
    path.join("shared", "inject", "human-pace.js"),
    "出站拟人节奏基元",
    "main.js 经 outbound-pace.js 顶层 require 它 → **主进程起不来**，App 双击无反应",
  ],
  [
    path.join("services", "whatsapp-baileys", "server.js"),
    "WhatsApp(Baileys) 协议边车入口",
    "「接入 WhatsApp → 协议多开」在客户机上永远 service_down",
  ],
  [
    path.join("services", "whatsapp-baileys", "node_modules"),
    "WhatsApp 边车依赖",
    "边车一起来就 MODULE_NOT_FOUND（构建机漏跑 npm ci）",
  ],
  [
    path.join("services", "whatsapp-baileys", "package.json"),
    "WhatsApp 边车 package.json",
    'ESM 解析失败（server.js 依赖其中的 "type":"module"）',
  ],
  [
    path.join("services", "messenger-web", "server.js"),
    "Messenger 托管登录边车入口",
    "「接入 Messenger」在客户机上永远 service_down",
  ],
  [
    path.join("services", "messenger-web", "node_modules"),
    "Messenger 边车依赖",
    "边车一起来就 MODULE_NOT_FOUND（构建机漏跑 npm ci）",
  ],
  [
    path.join("services", "messenger-web", "package.json"),
    "Messenger 边车 package.json",
    'ESM 解析失败（server.js 依赖其中的 "type":"module"）',
  ],
];

// 导出给 test/package-layout.test.js 交叉核对：包内 require 跨出 asar 的每个目录，
// 这张表里都得有代表文件——否则 extraResources 哪天被改歪，缺件只在客户机暴露。
exports.REQUIRED = REQUIRED;

// 通配判据（目录名带版本号，不能写死）：[所在目录, glob 前缀, 说明, 影响]
const REQUIRED_GLOB = [
  [
    path.join("services", "messenger-web", "node_modules", "playwright-core",
      ".local-browsers"),
    "chromium-",
    "Messenger 的兜底 Chromium",
    "没装 Chrome 的客户机点登录后浏览器起不来（我们自己开发机装了 Chrome，永远复现不出）",
  ],
];

// 绝不能随包的东西：本机生产号的登录凭据 / 日志。随包＝把自己的 WhatsApp 账号
// 连同会话密钥发给每一个下载用户（同 protocol_media 那次隐私事故）。
const FORBIDDEN = [
  [path.join("services", "whatsapp-baileys", "sessions"), "本机 WhatsApp 登录凭据"],
  [path.join("services", "whatsapp-baileys", "logs"), "本机运行日志"],
  [
    path.join("services", "messenger-web", "sessions"),
    "本机 Messenger 浏览器 profile 与 cookie（含已登录的 Facebook 会话）",
  ],
  [path.join("services", "messenger-web", "logs"), "本机运行日志"],
];

// 内测/定制包随包数据种子。判据与 stage_internal_assets.py 的产出一一对应：
// 种子缺一半（比如只有 overlay 没有语音）装出来就是「人设在、声音哑」的半残包，
// 比不带更难排查。
//
// ⚑ 内测口径（2026-08-01 拍板）：内测期唯一打包形态=内测包（与生产机对齐），
// build/seed-data **必须存在**——漏跑 stage:internal 直接打包失败，绝不静默产出
// 「功能全关的标准形态」（正是「装到别的电脑功能全消失」事故的成因之一）。
// 将来要出标准包：显式设 env CHATX_ALLOW_STANDARD=1（一次性逃生门，勿常开）。
const SEED_REQUIRED = [
  ["config.local.internal.yaml", "功能 overlay 种子"],
  ["seed-manifest.json", "种子清单"],
  [path.join("config", "profiles_runtime.yaml"), "人设 runtime"],
  [path.join("config", "voice_refs"), "克隆参考音"],
  [path.join("config", "persona_albums"), "人设相册"],
  [path.join("config", "persona_media.db"), "相册注册表"],
  [path.join("config", "knowledge_base.db"), "知识库"],
  [path.join("config", "persona_bio.db"), "人设资料库"],
  [path.join("config", "prerender_lines"), "预渲染台词库"],
  [path.join("assets", "voices"), "预渲染语音成品"],
];

exports.default = async function afterPack(context) {
  const res = resourcesDir(context);
  const missing = [];
  for (const [rel, what, impact] of REQUIRED) {
    if (!fs.existsSync(path.join(res, rel))) {
      missing.push(`  · 缺 ${rel}（${what}）→ ${impact}`);
    }
  }
  const seedStaged = fs.existsSync(path.join(__dirname, "seed-data"));
  const allowStandard = process.env.CHATX_ALLOW_STANDARD === "1";
  if (!seedStaged && !allowStandard) {
    missing.push(
      "  · 缺 build/seed-data（内测数据种子）→ 内测期唯一形态=内测包；" +
      "先跑 npm run stage:internal（要出标准包需显式 CHATX_ALLOW_STANDARD=1）"
    );
  }
  if (seedStaged) {
    for (const [rel, what] of SEED_REQUIRED) {
      if (!fs.existsSync(path.join(res, "seed-data", rel))) {
        missing.push(`  · 缺 seed-data/${rel}（${what}）→ 内测包装出来是半残形态`);
      }
    }
  }
  for (const [dir, prefix, what, impact] of REQUIRED_GLOB) {
    const abs = path.join(res, dir);
    let hit = false;
    try {
      hit = fs.readdirSync(abs).some((n) => n.startsWith(prefix));
    } catch (e) { hit = false; }
    if (!hit) {
      missing.push(`  · 缺 ${dir}/${prefix}*（${what}）→ ${impact}`);
    }
  }
  const leaked = [];
  for (const [rel, what] of FORBIDDEN) {
    if (fs.existsSync(path.join(res, rel))) {
      leaked.push(`  · ${rel}（${what}）`);
    }
  }

  if (leaked.length) {
    throw new Error(
      `[after-pack] 安装包里含**绝不能分发**的内容（打包中止防泄漏）：\n${leaked.join("\n")}\n` +
      "请检查 package.json 的 extraResources filter"
    );
  }
  if (missing.length) {
    throw new Error(
      `[after-pack] 安装包缺件（打包中止，避免出一个「装了也用不了」的包）：\n${missing.join("\n")}\n` +
      `resources=${res}`
    );
  }
  console.log(`[after-pack] ✓ 随包交付物齐备（${REQUIRED.length} 项）：${res}`);
};
