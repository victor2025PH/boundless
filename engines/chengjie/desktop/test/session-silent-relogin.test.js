// session-silent-relogin.test.js — 会话过期静默重登契约（源码静态契约）
//
// 页内 API 连续 401 时 _api_fetch.html 打控制台标记 [chatx:session-expired]；
// 工作台 webview（renderer.js）与后台弹窗（main.js）监听该标记，用凭据
// redirect:'manual' POST /login 原地换新 cookie——不导航（保草稿）、手动登录态不做、
// 有冷却。任何一端丢失即回到「开着工作台 2 小时后反复弹登录已失效」。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("session-silent-relogin FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const MARK = "[chatx:session-expired]";
const mainJs = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
const rendererJs = fs.readFileSync(
  path.join(__dirname, "..", "renderer", "renderer.js"), "utf8");
const apiFetch = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "web", "templates", "_api_fetch.html"), "utf8");

ok(apiFetch.indexOf("console.info('" + MARK + "')") >= 0,
  "_api_fetch.html 未在会话失效时打标记 —— 壳无从感知 401");

// renderer.js
ok(rendererJs.indexOf('const SESSION_EXPIRED_MARK = "' + MARK + '"') >= 0,
  "renderer.js 标记常量与页面不一致");
const fnStart = rendererJs.indexOf("function backendSilentReloginJS(");
ok(fnStart >= 0, "renderer.js 丢失 backendSilentReloginJS");
const fnBody = rendererJs.slice(fnStart, fnStart + 1200);
ok(/redirect:'manual'/.test(fnBody), "静默重登必须 redirect:'manual'（不拉整页、不导航）");
ok(/opaqueredirect/.test(fnBody), "静默重登须以 303（opaqueredirect）判成功并按凭据链接力");
ok(!/location\.(replace|href|assign)/.test(fnBody), "静默重登不得导航（会丢草稿）");
const lsn = rendererJs.indexOf('wv.addEventListener("console-message"');
ok(lsn >= 0, "renderer.js 工作台 webview 未监听 console-message");
const lsnBody = rendererJs.slice(lsn, lsn + 600);
ok(/SESSION_EXPIRED_MARK/.test(lsnBody) && /backendSilentReloginJS\(creds\)/.test(lsnBody),
  "webview 监听未接到静默重登");
ok(/wv\._manualLogin/.test(lsnBody), "主动退出后的手动登录态必须跳过静默重登");
ok(/60000/.test(lsnBody), "静默重登缺冷却");

// main.js 后台弹窗
const pStart = mainJs.indexOf("function bindBackendPopupLogin(");
const pEnd = mainJs.indexOf("\nfunction ", pStart + 10);
const pBody = mainJs.slice(pStart, pEnd > 0 ? pEnd : pStart + 6000);
ok(pBody.indexOf('win.webContents.on("console-message"') >= 0 && pBody.indexOf(MARK) >= 0,
  "bindBackendPopupLogin 未监听会话失效标记");
ok(/redirect:'manual'/.test(pBody) && /opaqueredirect/.test(pBody), "弹窗静默重登须 redirect:'manual'");
ok(/if \(manual\) return;[\s\S]*lastSilent/.test(pBody.slice(pBody.indexOf("console-message"))),
  "弹窗静默重登须尊重手动登录态");

console.log("session-silent-relogin: " + passed + " passed");
