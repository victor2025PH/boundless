// backend-popup-login.test.js — B95「管理页整组弹回后台首页」防复发门禁（源码静态契约）
//
// 2026-08-25 skuio 实录（实施68 P1-14）：侧栏数据洞察/用量计费/安全合规/账号资产
// 整组点击 →「正在连接…」→ 弹回后台首页，零报错。根因两半：
//   ① main.js bindBackendPopupLogin 会话过期重登后 location.replace(开窗时的 URL)
//      ——用户本次点的目标页（/login?next= 里带着）被丢弃 → 字面「弹回首页」；
//      且 attempted 一次性 → 长寿命弹窗第二次过期永远停在登录页。
//   ② renderer.js 工作台 webview 登录链同病：一律 replace 回 navTarget（工作台），
//      中途过期时用户要去的后台子页被吞。
//   ③ 服务端 _loading_overlay 超时散开纯静默 → 用户看到的只是「转圈后回到原页」。
// 修复契约：next 优先 + 重登可多次（带冷却/上限）+ 遮罩超时显式报错。任何一半
// 回退即拒绝出生（与 backend-popup-unique 同哲学：main.js 无法在 node 下 require，
// 只能走源码静态契约）。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("backend-popup-login FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const mainJs = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
const rendererJs = fs.readFileSync(
  path.join(__dirname, "..", "renderer", "renderer.js"), "utf8");
const overlayHtml = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "web", "templates", "_loading_overlay.html"),
  "utf8");

// ── main.js：bindBackendPopupLogin 三件套 ───────────────────────────────────
const fnStart = mainJs.indexOf("function bindBackendPopupLogin(");
ok(fnStart >= 0, "main.js 丢失 bindBackendPopupLogin 定义");
const fnBody = mainJs.slice(fnStart, fnStart + 3000);

// ① 回跳目标必须优先读 /login?next=（本次真实目的地）
ok(
  /searchParams\.get\(\s*["']next["']\s*\)/.test(fnBody),
  "bindBackendPopupLogin 未读取 /login?next= —— 会话过期重登后又会弹回开窗首页（B95 回归）"
);
// ② 不许回到 attempted 一次性语义（长寿命弹窗第二次过期即死在登录页）
ok(
  !/attempted\s*=\s*true/.test(fnBody),
  "bindBackendPopupLogin 回退到 attempted 一次性 —— 弹窗第二次会话过期无法自愈"
);
// ③ 重试必须有限（冷却 + 上限），token 失效时不无限打转
ok(
  /tries\s*>=\s*3/.test(fnBody) && /8000/.test(fnBody),
  "bindBackendPopupLogin 丢失重登冷却/上限 —— token 失效时会无限循环打 /login"
);
// ④ 防开放重定向的最小校验（//evil 拒收）
ok(
  /indexOf\(\s*["']\/\/["']\s*\)\s*===\s*0/.test(fnBody),
  "bindBackendPopupLogin 丢失 next 的 //host 防护"
);

// ── renderer.js：工作台 webview 登录链 next 优先 ────────────────────────────
const loginBranch = rendererJs.indexOf('if (p === "/login")');
ok(loginBranch >= 0, "renderer.js 丢失 /login 分支");
const loginBody = rendererJs.slice(loginBranch, loginBranch + 1600);
ok(
  /searchParams\.get\(\s*["']next["']\s*\)/.test(loginBody),
  "renderer.js 登录链未读取 ?next= —— 中途过期后用户要去的后台子页被吞回工作台"
);
ok(
  /backendLoginJS\(\s*creds\[idx\]\s*,\s*dest\s*\)/.test(loginBody),
  "renderer.js 登录链未把 dest（next 优先）传给 backendLoginJS"
);

// ── 用户管理 P0-1（2026-09-11）：主动退出 ≠ 会话过期 ─────────────────────────
// /logout 落地 /login?manual=1；两条自动登录链看到它必须停手，否则「退出登录」被壳
// 秒级令牌重登吃掉、子帐号永远见不到登录表单。三端契约缺任何一半即拒绝出生。
const authRoutes = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "web", "routes", "auth_user_routes.py"), "utf8");
ok(
  /RedirectResponse\(\s*["']\/login\?manual=1["']/.test(authRoutes),
  "auth_user_routes.py /logout 不再落 /login?manual=1 —— 壳无法区分主动退出与会话过期"
);
ok(
  /searchParams\.get\(\s*["']manual["']\s*\)\s*===\s*["']1["']/.test(fnBody) && /if \(manual\) return;/.test(fnBody),
  "bindBackendPopupLogin 丢失 manual=1 手动登录态 —— 弹窗里点退出会被 token 秒级重登"
);
ok(
  /searchParams\.get\(\s*["']manual["']\s*\)\s*===\s*["']1["']/.test(loginBody) && /_manualLogin/.test(loginBody),
  "renderer.js 登录链丢失 manual=1 手动登录态 —— 工作台里退出会被凭据链秒级重登"
);

// ── _loading_overlay.html：超时散开必须显式报错（禁静默）────────────────────
ok(
  /function\s+navFailNote\(/.test(overlayHtml),
  "_loading_overlay.html 丢失 navFailNote —— 导航被吞时又回到「转圈后无声弹回」"
);
ok(
  /ldov_navfail/.test(overlayHtml),
  "_loading_overlay.html 报错条丢失 i18n 键 ldov_navfail"
);
ok(
  /hardT\s*=\s*setTimeout\(function\(\)\{\s*hide\(\);\s*navFailNote\(\);/.test(overlayHtml),
  "_loading_overlay.html 的 L2 超时未接 navFailNote（hide 后必须报错）"
);
// winname 链接开的是命名窗口（本文档不导航），不得记 dest 误报
ok(
  /data-winname/.test(overlayHtml),
  "_loading_overlay.html 丢失 winname 链接的 dest 豁免（会对弹窗类链接误报未打开）"
);

console.log(`backend-popup-login OK (${passed} assertions)`);
