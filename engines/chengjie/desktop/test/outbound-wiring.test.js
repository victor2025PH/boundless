// outbound-wiring.test.js —— 受控出站链路「诚实回执 + 拟人节奏」静态接线门禁
//
// 为什么需要静态门禁：这条链跨四个进程边界（renderer → shell-preload → main → webview
// preload → shared/inject），纯函数单测（outbound-pace.test.js）只能证明**策略正确**，
// 证不了「策略真的被接上了」。而恰恰是接线断掉最可怕：
//   · 回执断 → 退回旧的「发完就当成功」，注入一失效就把每条命令谎报成已送达
//     （1.016/1.017 shared/inject 漏包期间就是这样：后台全绿、客户一条没收到）；
//   · 节奏断 → 退回固定 600ms 连发，风控特征明显。
// 两者都**不报错、不变红**，只能靠这里钉住。renderer.js 是浏览器上下文大脚本（node 不可
// require），故走源码静态契约，与 renderer-boot-invariants 同哲学。挂 npm test + predist。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("outbound-wiring FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const appDir = path.join(__dirname, "..");
const rd = (...p) => fs.readFileSync(path.join(appDir, ...p), "utf8");
const rendererJs = rd("renderer", "renderer.js");
const preloadJs = rd("shell-preload.js");
const mainJs = rd("main.js");
const injectJs = rd("inject", "tg-inject.js");
const coreJs = fs.readFileSync(path.join(appDir, "..", "shared", "inject", "core.js"), "utf8");

// ── ① 回执链：inject 上报 → webview 通道 → renderer 等待 → 主进程判定 ──────────
ok(
  /reportFillResult/.test(coreJs) && /sendAndConfirm/.test(coreJs),
  "shared/inject/core.js 丢了 reportFillResult / sendAndConfirm —— 注入侧不再回报「发没发出去」"
);
ok(
  /composer[\s\S]{0,400}?清空|composer_not_cleared/.test(coreJs),
  "core.js 丢了「composer 是否清空」回读 —— 那是本地唯一的送达证据"
);
ok(
  /reportFillResult:\s*\(payload\)\s*=>[\s\S]{0,160}sendToHost\(\s*["']fill-result["']/.test(injectJs),
  "tg-inject.js 的 host.reportFillResult 未经 sendToHost('fill-result') 上报 —— 回执到不了宿主"
);
ok(
  /e\.channel\s*===\s*["']fill-result["']/.test(rendererJs),
  "renderer 未监听 webview 的 'fill-result' 通道 —— 回执被丢弃，等价于回到乐观 ack"
);
ok(
  /token/.test(rendererJs) && /wv\.send\(\s*["']fill-composer["']\s*,\s*\{[^}]*token/.test(rendererJs),
  "受控出站的 fill-composer 未带 token —— 多条并发时无法把回执对上是哪一条"
);

// ② 绝不允许再出现「发完立刻 ack ok:true」：这正是被修掉的那个谎报
ok(
  !/outboundAck\(\s*\{\s*id:\s*it\.id\s*,\s*ok:\s*true/.test(rendererJs),
  "renderer 又出现无条件 outboundAck({ok:true}) —— 注入失效时会把每条命令谎报成已送达"
);
ok(
  /outboundReport/.test(rendererJs) && /outboundReport:/.test(preloadJs) &&
    /ipcMain\.handle\(\s*["']desktop:outbound-report["']/.test(mainJs),
  "outbound-report 桥（renderer → preload → main）缺环 —— 回执没人判定"
);

// ③ 三档语义的落点必须在主进程（唯一策略源），且「不 ack」这一档真的存在
ok(
  /require\(\s*["']\.\/outbound-pace\.js["']\s*\)/.test(mainJs),
  "main.js 未引入 outbound-pace.js —— 节奏与回执判定失去唯一权威"
);
ok(
  /ackDecision\(/.test(mainJs) && /if\s*\(\s*!d\.ack\s*\)\s*return/.test(mainJs),
  "main.js 未按 ackDecision 结果放行「不 ack」这一档 —— 无送达证据时会被迫二选一（谎报或标死）"
);

// ── ④ 节奏链：pacePlan 三段桥 + 超频不丢命令 ─────────────────────────────────
ok(
  /pacePlan/.test(rendererJs) && /pacePlan:/.test(preloadJs) &&
    /ipcMain\.handle\(\s*["']desktop:pace-plan["']/.test(mainJs),
  "pace-plan 桥缺环 —— 拟人节奏没接上，退回固定间隔连发"
);
ok(
  /plan\.throttled/.test(rendererJs) && /break/.test(rendererJs),
  "renderer 未处理 throttled —— 撞每分钟安全阀时要把命令留在队列（不 ack），不是继续硬发"
);
ok(
  /ACTIVE_CHAT_BY_ACCOUNT\[account_id\]\s*!==\s*chat_key/.test(rendererJs),
  "节奏等待后未复核当前会话 —— 等待期间坐席切走会话会把回复填进错误的聊天"
);

// ⑥ 防重入：一轮派发现在可达数十秒，远超 5s 轮询间隔
ok(
  /_pollBusy/.test(rendererJs) && /if\s*\(\s*_pollBusy\s*\)\s*return\s*;/.test(rendererJs),
  "轮询未防重入 —— 节奏拉长后多个循环会并行给同一账号发，把间隔有效对折"
);

// ⑦ 无选择器档案的账号不该拉命令（否则 认领→等不到回执→不 ack→回收 空转到 attempts 到顶）
ok(
  /function injectSendable/.test(rendererJs) && /injectSendable\(\s*account_id\s*\)/.test(rendererJs),
  "缺 injectSendable 闸 —— 无注入档案的账号会把命令反复认领到判死"
);
ok(
  /st\.code\s*!==\s*["']unsupported["']/.test(rendererJs),
  "injectSendable 未按稳定 code 判定（别 match 中文文案，产品改字就悄悄失效）"
);

// ⑤ 节奏状态必须在主进程常驻（renderer 重载不该清零 → 否则重载即爆发连发）
ok(
  /createPacerRegistry\(/.test(mainJs),
  "main.js 未建 pacer registry —— 节奏状态若活在 renderer，页面一重载就恢复满速连发"
);

console.log("outbound-wiring.test.js: " + passed + " passed");
