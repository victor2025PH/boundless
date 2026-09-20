// backend-popup-unique.test.js — 后台弹窗「窗口唯一性」防复发门禁（源码静态契约）
//
// 2026-08-14 实录回归：壳内点「后台管理 / 坐席工作台」越点窗口越多。两处断链：
//   ① 壳内 window.open 没有浏览器命名窗口寻址语义，_win_unique 的 Electron 分支对
//      admin 显式 return false → 每次点击都落到 main.js openBackendPopup 新建原生窗；
//   ② unified_inbox 的「后台管理」按钮只有 target=_blank，从未接 __openUnique。
// 修复 = main.js 按槽位（workspace/wsub/admin）去重复用 + 模板按钮接线。本门禁把两半
// 都钉死——任何一半丢失即拒绝出生（与 renderer-boot-invariants 同哲学：main.js 在
// node 下 load 即触 electron require，只能走源码静态契约）。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("backend-popup-unique FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const mainJs = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
const inboxHtml = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "web", "templates", "unified_inbox.html"),
  "utf8"
);

// ── main.js：槽位去重四件套 ─────────────────────────────────────────────────
// ① 槽位注册表存在
ok(
  /const\s+backendPopupWins\s*=\s*new\s+Map\(\)/.test(mainJs),
  "main.js 丢失 backendPopupWins 槽位注册表 —— 后台弹窗回到「点一次开一个」"
);

// ② openBackendPopup 必须先尝试复用，且复用早于 new BrowserWindow
const fnStart = mainJs.indexOf("function openBackendPopup(");
ok(fnStart >= 0, "main.js 丢失 openBackendPopup 定义");
const fnBody = mainJs.slice(fnStart, fnStart + 2000);
const reuseIdx = fnBody.indexOf("reuseBackendPopup(");
const newWinIdx = fnBody.indexOf("new BrowserWindow(");
ok(
  reuseIdx >= 0 && newWinIdx >= 0 && reuseIdx < newWinIdx,
  "openBackendPopup 未在 new BrowserWindow 之前尝试 reuseBackendPopup —— 去重失效"
);

// ③ 新建窗口必须登记槽位 + closed 清理（否则复用命中已销毁句柄 / 泄漏 Map）
ok(
  /backendPopupWins\.set\(\s*slot\s*,\s*child\s*\)/.test(mainJs),
  "openBackendPopup 丢失 backendPopupWins.set(slot, child) 登记"
);
ok(
  /backendPopupWins\.delete\(\s*slot\s*\)/.test(mainJs),
  "openBackendPopup 丢失 closed → backendPopupWins.delete(slot) 清理"
);

// ④ 子弹窗自身的 window.open 也必须走同一收敛（后台侧栏「坐席工作台」在弹窗里点）
ok(
  /child\.webContents\.setWindowOpenHandler\(\s*makeBackendPopupHandler\([^)]*\)\s*\)/.test(mainJs),
  "openBackendPopup 丢失 child.webContents.setWindowOpenHandler —— 弹窗里再点后台链接不去重"
);

// ⑤ 槽位路由：精确 /workspace 与 /workspace/* 子页必须分槽（子页深链不许挤掉收件箱）
ok(
  /function\s+backendPopupSlot\(/.test(mainJs) &&
    /"workspace"/.test(mainJs) &&
    /"wsub"/.test(mainJs),
  "main.js 丢失 backendPopupSlot 的 workspace/wsub 分槽路由"
);

// ── unified_inbox.html：「后台管理」按钮必须接 __openUnique（浏览器侧命名窗口复用）──
const adminBtn = /<a[^>]*id="admin-nav-btn"[^>]*>/.exec(inboxHtml);
ok(!!adminBtn, "unified_inbox.html 丢失 admin-nav-btn");
ok(
  /data-winname="admin"/.test(adminBtn[0]),
  'admin-nav-btn 丢失 data-winname="admin" —— 浏览器侧后台管理不再复用命名窗口'
);
ok(
  /onclick="return __openUnique\(event,this\)"/.test(adminBtn[0]),
  "admin-nav-btn 丢失 onclick=__openUnique 接线 —— target=_blank 裸奔每点新开一个"
);

// ── 2026-08-29：坐席工作台「唯一容器」路由（主窗收件箱标签）────────────────────
// 主窗常驻统一收件箱标签＝工作台唯一正身；workspace 弹窗一出生就是第二个工作台。
// 修复=精确 /workspace 一律路由主窗（聚焦+深链转发），任何一环丢失都会退回
// 「主窗标签 + 弹窗」双工作台（2026-08-29 之前遥测 8 天 15 次 takeover 的根源）。
const shellPreloadJs = fs.readFileSync(path.join(__dirname, "..", "shell-preload.js"), "utf8");
const rendererJs = fs.readFileSync(path.join(__dirname, "..", "renderer", "renderer.js"), "utf8");
const popupPreloadJs = fs.readFileSync(path.join(__dirname, "..", "renderer", "popup-preload.js"), "utf8");
const inboxPreloadJs = fs.readFileSync(path.join(__dirname, "..", "renderer", "inbox-preload.js"), "utf8");
const winUniqueHtml = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "web", "templates", "_win_unique.html"),
  "utf8"
);

// ⑥ main.js：focusMainInbox 存在，且 openBackendPopup 对 workspace 槽先走主窗路由
ok(
  /function\s+focusMainInbox\(/.test(mainJs),
  "main.js 丢失 focusMainInbox —— 工作台点击退回原生弹窗双开"
);
ok(
  /slot === "workspace" && focusMainInbox\(url\)/.test(mainJs),
  "openBackendPopup 丢失 workspace→主窗路由特判"
);
const routeIdx = fnBody.indexOf("focusMainInbox(");
ok(
  routeIdx >= 0 && routeIdx < fnBody.indexOf("reuseBackendPopup("),
  "openBackendPopup 中主窗路由必须先于弹窗复用（先主窗、后弹窗回落）"
);
// 主窗登记 + 通知频道（缺登记 focusMainInbox 永远 false = 特判形同虚设）
ok(/mainSeatWin = win/.test(mainJs), "createWindow 未登记 mainSeatWin");
ok(/cx-open-workspace/.test(mainJs), "main.js 丢失 cx-open-workspace 通知频道");
// 收件箱标签被配置关闭（纯内嵌形态）必须回落弹窗——弹窗是那形态下唯一工作台容器
ok(
  /unified_inbox\s*\|\|\s*\{\}\)\.enabled === false\)\s*return false/.test(mainJs),
  "focusMainInbox 丢失 unified_inbox.enabled=false 回落闸"
);

// ⑦ 桥与承接：shell-preload 转发 + renderer 切标签/深链转 open-conv
ok(
  /onOpenWorkspace:/.test(shellPreloadJs) && /cx-open-workspace/.test(shellPreloadJs),
  "shell-preload 丢失 onOpenWorkspace 桥"
);
ok(
  /onOpenWorkspace\(/.test(rendererJs) && /Inbox\.activate\(INBOX_ID\)/.test(rendererJs),
  "renderer 丢失 cx-open-workspace 承接（切收件箱标签）"
);
ok(
  /aitr:\s*"open-conv"/.test(rendererJs) && /deliverToInbox\(/.test(rendererJs),
  "renderer 丢失深链转 open-conv（?conv= 会话深链在壳内会失效）"
);

// ⑧ 能力标记（分体部署版本闸）：模板热更先于壳更新时，旧壳必须零行为变化
ok(
  /__chatxCaps/.test(popupPreloadJs) && /wsRoute/.test(popupPreloadJs),
  "popup-preload 丢失 __chatxCaps.wsRoute —— 后台弹窗页探测不到新壳能力"
);
ok(
  /__chatxCaps/.test(inboxPreloadJs) && /wsRoute/.test(inboxPreloadJs),
  "inbox-preload 丢失 __chatxCaps.wsRoute —— 收件箱页探测不到新壳能力"
);
ok(
  /_shellWsRoute\(\)/.test(winUniqueHtml) && /__chatxCaps/.test(winUniqueHtml),
  "_win_unique 丢失壳能力探测 _shellWsRoute —— 新壳退回探活链（miss 漏弹窗）"
);
ok(
  /_probeSeat\(url,/.test(winUniqueHtml),
  "_win_unique 旧壳 BC 探活回落被删 —— 存量壳（无 wsRoute）会退化成每点弹一个"
);

console.log(`backend-popup-unique OK (${passed} assertions)`);
