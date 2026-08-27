// copilot-pip-bridge.test.js — 悬浮副驾原生窗桥（cp PiP shell P1 2026-08-18）防复发门禁
//
// 背景：浏览器端悬浮副驾走 Document PiP；Electron 31 的该 API 是 0×0 退化窗（探针实锤，
// 用户看到「大灰屏」）→ 壳内改走原生 alwaysOnTop BrowserWindow + 五层消息中继：
//   workspace 页 ⇄ inbox-preload(desktop:copilot-pip / cp-pip-evt) ⇄ main.js ⇄
//   pip-preload(cp-pip-out / cp-pip-in) ⇄ pip.html wrapper ⇄ iframe /copilot/app.html
// 任何一层断链都是静默失效（按钮点了没反应 / 消息进黑洞），与 renderer-boot-invariants
// 同哲学：main.js 在 node 下 require 即触 electron，只能走源码静态契约把链钉死。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("copilot-pip-bridge FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const read = (...p) => fs.readFileSync(path.join(__dirname, "..", ...p), "utf8");
const mainJs = read("main.js");
const pipHtml = read("renderer", "pip.html");
const pipPreload = read("renderer", "pip-preload.js");
const inboxPreload = read("renderer", "inbox-preload.js");
const inboxHtml = read("..", "src", "web", "templates", "unified_inbox.html");

// ── main.js：主进程窗管 + 中继 ──────────────────────────────────────────────
ok(
  /ipcMain\.handle\(\s*"desktop:copilot-pip"/.test(mainJs),
  "main.js 丢失 desktop:copilot-pip handler —— 页面开原生副驾窗的唯一入口"
);
{
  const hStart = mainJs.indexOf('ipcMain.handle("desktop:copilot-pip"');
  const hBody = mainJs.slice(hStart, hStart + 1200);
  ok(
    /isBackendUrl\(\s*e\.sender\.getURL\(\)\s*\)/.test(hBody),
    "desktop:copilot-pip 丢失发起方 URL 校验 —— 任意 webContents 都能开窗（纵深防御失守）"
  );
}
{
  const fnStart = mainJs.indexOf("function openCopilotPipWindow(");
  ok(fnStart >= 0, "main.js 丢失 openCopilotPipWindow 定义");
  const fnBody = mainJs.slice(fnStart, fnStart + 3000);
  // 窗全局唯一：已开先聚焦复用，且判定早于 new BrowserWindow
  const reuseIdx = fnBody.indexOf("copilotPipWin && !copilotPipWin.isDestroyed()");
  const newIdx = fnBody.indexOf("new BrowserWindow(");
  ok(
    reuseIdx >= 0 && newIdx >= 0 && reuseIdx < newIdx,
    "openCopilotPipWindow 未在 new BrowserWindow 前复用已开窗 —— 点一次多一个置顶窗"
  );
  ok(
    /alwaysOnTop:\s*true/.test(fnBody),
    "openCopilotPipWindow 丢失 alwaysOnTop —— 「置顶于所有应用之上」名存实亡"
  );
  ok(
    /partition:\s*BACKEND_WORKSPACE_PARTITION/.test(fnBody),
    "openCopilotPipWindow 丢失 backend-workspace 分区 —— iframe 拿不到登录 cookie（app 界面 401）"
  );
  ok(
    /pip-preload\.js/.test(fnBody),
    "openCopilotPipWindow 丢失 pip-preload —— wrapper 无桥可用，消息全断"
  );
  ok(
    /loadFile\([^)]*pip\.html/.test(fnBody) && /query:\s*\{\s*src:/.test(fnBody),
    "openCopilotPipWindow 未经 pip.html?src= 装载 —— app.html 顶层直载时 parent===window 全静默"
  );
  ok(
    /cp-pip-closed/.test(fnBody),
    "openCopilotPipWindow 丢失 closed → cp-pip-closed 通知 —— 页面按钮态与真窗态漂移"
  );
}
{
  const onStart = mainJs.indexOf('ipcMain.on("cp-pip-out"');
  ok(onStart >= 0, "main.js 丢失 cp-pip-out 监听 —— app 回吐（cp-ready/cp-fill/cp-send）进黑洞");
  const onBody = mainJs.slice(onStart, onStart + 600);
  ok(
    /e\.sender\s*!==\s*copilotPipWin\.webContents/.test(onBody),
    "cp-pip-out 丢失发送方过滤 —— 任意 renderer 可伪造副驾回吐（代发消息注入面）"
  );
}

// ── pip.html wrapper：iframe 包装 + 双向转发 ────────────────────────────────
ok(
  /\/copilot\/app\.html/.test(pipHtml),
  "pip.html 丢失 src 路径校验（/copilot/app.html）—— wrapper 可被喂任意页面"
);
ok(
  /e\.source\s*!==\s*f\.contentWindow/.test(pipHtml),
  "pip.html 丢失 iframe source 过滤 —— 非自家消息也会被转发"
);
ok(
  /__cpPipBridge\.out\(/.test(pipHtml) && /__cpPipBridge\.onIn\(/.test(pipHtml),
  "pip.html 丢失 __cpPipBridge 出/入站接线 —— 中继断链"
);

// ── pip-preload：ipc 通道对 ─────────────────────────────────────────────────
ok(
  /ipcRenderer\.send\(\s*"cp-pip-out"/.test(pipPreload),
  "pip-preload 丢失 cp-pip-out 出站通道"
);
ok(
  /ipcRenderer\.on\(\s*"cp-pip-in"/.test(pipPreload),
  "pip-preload 丢失 cp-pip-in 入站通道"
);
ok(
  /exposeInMainWorld\(\s*"__cpPipBridge"/.test(pipPreload),
  "pip-preload 丢失 __cpPipBridge 暴露 —— wrapper 摸不到桥"
);

// ── inbox-preload：页面侧 API + 事件转投 ────────────────────────────────────
ok(
  /copilotPip\(payload\)/.test(inboxPreload) &&
    /ipcRenderer\.invoke\(\s*"desktop:copilot-pip"/.test(inboxPreload),
  "inbox-preload 丢失 copilotPip → desktop:copilot-pip invoke —— 页面无入口"
);
ok(
  /ipcRenderer\.on\(\s*"cp-pip-evt"/.test(inboxPreload) &&
    /type:\s*"chatx-cp-pip"/.test(inboxPreload),
  "inbox-preload 丢失 cp-pip-evt → chatx-cp-pip 转投 —— 回吐到不了页面世界"
);

// ── unified_inbox.html：三态分流 + 单点消费 ─────────────────────────────────
ok(
  /function _cpPipMode\(\)/.test(inboxHtml) &&
    /_cpPipShellOk\(\)\s*\?\s*'shell'\s*:\s*''/.test(inboxHtml),
  "unified_inbox 丢失 _cpPipMode 壳分支 —— Electron 里按钮永远隐藏（或退回 0×0 灰屏）"
);
ok(
  /d\.type!=='chatx-cp-pip'/.test(inboxHtml.replace(/\s+/g, "")) ||
    /chatx-cp-pip/.test(inboxHtml),
  "unified_inbox 丢失 chatx-cp-pip 事件消费 —— shell 模式回吐无人听"
);
ok(
  /cp-pip-closed/.test(inboxHtml),
  "unified_inbox 丢失 cp-pip-closed 消费 —— 原生窗关了页面还以为开着"
);
{
  // cp-ready/cp-fill/cp-send 消费必须单点（_cpPipHandleAppMsg），DOM 与 shell 两路共用；
  // 各写一套 = 契约漂移温床（灰屏事故里 _cpPipOnMsg 未定义就是这么漏的）
  const defCount = (inboxHtml.match(/function _cpPipHandleAppMsg\(/g) || []).length;
  const useCount = (inboxHtml.match(/_cpPipHandleAppMsg\(/g) || []).length;
  ok(defCount === 1, "unified_inbox _cpPipHandleAppMsg 定义数 !== 1（单点消费被拆散）");
  ok(useCount >= 3, "unified_inbox _cpPipHandleAppMsg 消费点不足（DOM/shell 两路应共用）");
}

// ── P2：位置记忆 / 拉回语义 / 驻留遥测（2026-08-18）────────────────────────
{
  // 位置记忆：关窗存 bounds → 下次开还原；还原前必须过屏幕边界校验
  // （多屏拔线后旧坐标落屏外 = 窗开在看不见的地方，比不记忆更糟）
  const fnStart = mainJs.indexOf("function copilotPipSavedBounds(");
  ok(fnStart >= 0, "main.js 丢失 copilotPipSavedBounds —— 位置记忆断链");
  const fnBody = mainJs.slice(fnStart, fnStart + 1600);
  ok(
    /getAllDisplays\(\)/.test(fnBody) && /workArea/.test(fnBody),
    "copilotPipSavedBounds 丢失屏幕边界校验 —— 多屏拔线后窗还原到屏外"
  );
  ok(
    /Math\.max\(320,/.test(fnBody) && /Math\.max\(360,/.test(fnBody),
    "copilotPipSavedBounds 丢失尺寸 clamp —— 脏配置可开出畸形窗"
  );
  const openStart = mainJs.indexOf("function openCopilotPipWindow(");
  const openBody = mainJs.slice(openStart, openStart + 4000);
  ok(
    /copilotPipSavedBounds\(\)/.test(openBody),
    "openCopilotPipWindow 未消费 copilotPipSavedBounds —— 记忆存了不用"
  );
  ok(
    /on\("close",/.test(openBody) && /saveConfigPatch\(\s*\{\s*copilot_pip:/.test(openBody),
    "openCopilotPipWindow 丢失 close→saveConfigPatch(copilot_pip) —— 关窗不存位置"
  );
}
{
  // 拉回语义：shell 模式开着时二击 = 再发 open（主进程 restore+focus），绝不发 close
  // ——原生窗可被最小化/盖住，被埋时坐席点按钮意图是找回，误关伤害大于误开
  const tStart = inboxHtml.indexOf("function _cpPipShellToggle(");
  ok(tStart >= 0, "unified_inbox 丢失 _cpPipShellToggle");
  const tBody = inboxHtml.slice(tStart, tStart + 900);
  const openBranch = tBody.slice(0, tBody.indexOf("window.__cpPipReadyAt=0"));
  ok(
    /action:'open'/.test(openBranch) && !/action:'close'/.test(openBranch),
    "_cpPipShellToggle 已开分支不是拉回（action:'open'）—— 被埋的窗点按钮会被误关"
  );
  ok(
    /cppanel_pip_recall/.test(openBranch),
    "_cpPipShellToggle 拉回丢失 cppanel_pip_recall beacon —— 找回行为不可观测"
  );
  // tooltip 必须随窗态切换：开着时按「再点一次关闭」旧文案预期点下去 = 像坏了
  ok(
    /inbox\.cp\.pip_btn_recall_t/.test(inboxHtml),
    "unified_inbox 丢失 pip_btn_recall_t 动态 tooltip —— 拉回语义与按钮文案矛盾"
  );
}
{
  // 驻留遥测：关窗出时长分桶 beacon（P3 投入裁决的数据地基），DOM/shell 两路都记
  ok(
    /function _cpPipDurBeacon\(/.test(inboxHtml) && /cppanel_pip_dur_/.test(inboxHtml),
    "unified_inbox 丢失 _cpPipDurBeacon 驻留分桶 —— P3 裁决没有数据"
  );
  const durUses = (inboxHtml.match(/_cpPipDurBeacon\(\)/g) || []).length;
  ok(durUses >= 2, "_cpPipDurBeacon 消费点不足 2 —— DOM 或 shell 关窗路径漏记时长");
}

console.log(`copilot-pip-bridge OK (${passed} assertions)`);
