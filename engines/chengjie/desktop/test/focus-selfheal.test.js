// focus-selfheal.test.js — B54「confirm 后整窗输入框点不进」防复发门禁（源码静态契约）
//
// 实录（实施68 P1-16）：钧「主动关怀 → 跳过关怀记录 → 取消 → 输入全死」+
// skuio「人设编辑页偶发无光标」——两案直接诱因都是 window.confirm：Electron 原生
// confirm/alert 关闭后 Chromium 窗口焦点态失步（上游久悬 bug），此后整窗 focus()
// 全部被忽略，直到 BrowserWindow blur+focus 一轮。修复链四件套，缺一即复发：
//   ① main.js desktop:focus-fix 通道（blur+focus 复位，webview 宿主窗兼容）
//   ② popup-preload.js 极小桥（后台弹窗此前零 preload，confirm 病灶主场）
//   ③ openBackendPopup 挂 ②；inbox-preload 同名桥（workspace webview 面）
//   ④ 服务端 _focus_selfheal.html 包 confirm/alert/prompt + 点击自愈 + 埋点，
//      base.html / workspace_base.html 双基座包含

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("focus-selfheal FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const read = (...p) => fs.readFileSync(path.join(__dirname, ...p), "utf8");
const mainJs = read("..", "main.js");
const inboxPreload = read("..", "renderer", "inbox-preload.js");
const popupPreload = read("..", "renderer", "popup-preload.js");
const tplDir = path.join(__dirname, "..", "..", "src", "web", "templates");
const healTpl = fs.readFileSync(path.join(tplDir, "_focus_selfheal.html"), "utf8");
const baseTpl = fs.readFileSync(path.join(tplDir, "base.html"), "utf8");
const wsBaseTpl = fs.readFileSync(path.join(tplDir, "workspace_base.html"), "utf8");

// ① 主进程复位通道
ok(
  /ipcMain\.handle\(\s*"desktop:focus-fix"/.test(mainJs),
  "main.js 丢失 desktop:focus-fix 通道 —— confirm 后焦点态失步无法复位"
);
const fxStart = mainJs.indexOf('ipcMain.handle("desktop:focus-fix"');
const fxBody = mainJs.slice(fxStart, fxStart + 1200);
ok(
  /win\.blur\(\);\s*win\.focus\(\)/.test(fxBody),
  "desktop:focus-fix 丢失 blur+focus 序列（只 focus 不 blur 复位不了失步态）"
);
ok(
  /hostWebContents/.test(fxBody),
  "desktop:focus-fix 丢失 webview 宿主窗解析 —— workspace webview 内的修复失效"
);
ok(
  /isBackendUrl\(e\.sender\.getURL\(\)\)/.test(fxBody),
  "desktop:focus-fix 丢失后端页来源闸（任意 webContents 可调=纵深防御缺口）"
);

// ② ③ preload 桥（两形态同名契约）
ok(
  /__chatxShellFocus/.test(popupPreload) && /desktop:focus-fix/.test(popupPreload),
  "popup-preload.js 丢失 __chatxShellFocus 桥"
);
ok(
  /__chatxShellFocus/.test(inboxPreload) && /desktop:focus-fix/.test(inboxPreload),
  "inbox-preload.js 丢失 __chatxShellFocus 桥 —— workspace webview 面失修"
);
const popStart = mainJs.indexOf("function openBackendPopup(");
const popBody = mainJs.slice(popStart, popStart + 1600);
ok(
  /popup-preload\.js/.test(popBody),
  "openBackendPopup 未挂 popup-preload —— 后台弹窗（confirm 病灶主场）无桥可用"
);

// ④ 服务端自愈 partial + 双基座包含
ok(
  /\['confirm','alert','prompt'\]/.test(healTpl) && /focusFix/.test(healTpl),
  "_focus_selfheal.html 丢失 confirm/alert/prompt 包装（根治层）"
);
ok(
  /addEventListener\('mousedown'/.test(healTpl) && /focus_selfheal_/.test(healTpl),
  "_focus_selfheal.html 丢失点击自愈/埋点（兜底+观测层）"
);
ok(
  /\{% include "_focus_selfheal\.html" %\}/.test(baseTpl),
  "base.html 未包含 _focus_selfheal.html（后台管理页失修）"
);
ok(
  /\{% include "_focus_selfheal\.html" %\}/.test(wsBaseTpl),
  "workspace_base.html 未包含 _focus_selfheal.html（工作台失修）"
);

console.log(`focus-selfheal OK (${passed} assertions)`);
