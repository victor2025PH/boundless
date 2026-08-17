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
  /child\.webContents\.setWindowOpenHandler\(\s*makeBackendPopupHandler\(\)\s*\)/.test(mainJs),
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

console.log(`backend-popup-unique OK (${passed} assertions)`);
