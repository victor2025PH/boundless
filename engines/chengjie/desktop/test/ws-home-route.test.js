// ws-home-route.test.js — #158「审批页进得去出不来」防复发门禁（2026-09-04）
//
// 事故链：收件箱页「去审批」＝同窗 location.href='/workspace/drafts' → 壳内主窗收件箱
// webview 自己漂到子页；再点「聊天」经 _win_unique → window.open → focusMainInbox →
// renderer onOpenWorkspace 只切标签（本来就激活）＝点了什么都没发生，只能关整个窗口。
// 修复三层：① ws-home-route 纯函数判「webview 漂走了就导航回家」（本文件跑真值表）；
// ② main.js wsub 辅助弹窗点「聊天」交接主窗后收窗（不静默 deny）；③ draft_review.html
// 顶部原生 <a href="/workspace"> 返回入口（页级脚本崩了也能出去）。②③ 走源码静态契约
// （main.js/renderer.js 在 node 下 load 即触 electron require，与 backend-popup-unique 同哲学）。

const fs = require("fs");
const path = require("path");
const R = require("../renderer/ws-home-route.js");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("ws-home-route FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}
function eq(a, b, msg) { ok(a === b, msg + " (got " + JSON.stringify(a) + ", want " + JSON.stringify(b) + ")"); }

const HOME = "http://127.0.0.1:18799/workspace?lang=zh-CN&theme=dark";

// ── 纯函数真值表 ────────────────────────────────────────────────────────────
// ① 事故形态：webview 在 /workspace/drafts，请求精确 /workspace → 必须导航回家（保留 ?lang/&theme）
let r = R.resolveWorkspaceHome("http://127.0.0.1:18799/workspace/drafts", HOME, "/workspace");
eq(r.action, "navigate", "webview 漂到 /workspace/drafts 时必须 navigate");
eq(r.url, HOME, "navigate 目标须是 homeUrl 原样（lang/theme 不丢）");

// ② 漂走 + 会话深链：conv/mid/flag/case 并进 home URL（页面自带 ?conv= 解析）
r = R.resolveWorkspaceHome("http://127.0.0.1:18799/workspace/dash", HOME,
  "http://127.0.0.1:18799/workspace?conv=telegram%3A1%3A2&mid=77&flag=blocked&case=CASE-9");
eq(r.action, "navigate", "漂走 + 深链仍 navigate");
{
  const u = new URL(r.url);
  eq(u.pathname, "/workspace", "深链 navigate 路径为 /workspace");
  eq(u.searchParams.get("conv"), "telegram:1:2", "conv 并入");
  eq(u.searchParams.get("mid"), "77", "mid 并入");
  eq(u.searchParams.get("flag"), "blocked", "flag 并入");
  eq(u.searchParams.get("case"), "CASE-9", "case 并入");
  eq(u.searchParams.get("theme"), "dark", "theme 保留");
  eq(u.searchParams.get("lang"), "zh-CN", "lang 保留");
}
// ②b 旧 ?focus= 别名归一成 conv
r = R.resolveWorkspaceHome("http://127.0.0.1:18799/workspace/drafts", HOME, "/workspace?focus=abc");
eq(new URL(r.url).searchParams.get("conv"), "abc", "focus= 别名归一为 conv=");

// ③ 就在收件箱（带 query）+ 无深链 → none（只切标签，旧行为）
r = R.resolveWorkspaceHome("http://127.0.0.1:18799/workspace?lang=zh-CN&theme=dark", HOME, "/workspace");
eq(r.action, "none", "已在收件箱且无深链 → none");
// ③b 尾斜杠归一：/workspace/ 视同 /workspace
r = R.resolveWorkspaceHome("http://127.0.0.1:18799/workspace/?theme=dark", HOME, "/workspace");
eq(r.action, "none", "/workspace/ 尾斜杠视同在家");

// ④ 就在收件箱 + 深链 → deliver（站内 postMessage open-conv，旧行为）
r = R.resolveWorkspaceHome("http://127.0.0.1:18799/workspace?theme=dark", HOME, "/workspace?conv=x1&mid=3");
eq(r.action, "deliver", "在家 + 深链 → deliver");
eq(r.parts && r.parts.cid, "x1", "deliver parts.cid");
eq(r.parts && r.parts.mid, "3", "deliver parts.mid");

// ⑤ 刻意不动的形态：登录页（next= 回跳链自己负责）、启动闸门空白页、跨源、无 home 口径
r = R.resolveWorkspaceHome("http://127.0.0.1:18799/login?next=%2Fworkspace%2Fdrafts", HOME, "/workspace");
ok(r.action !== "navigate", "/login 期间不得硬导航（会打断自动登录回跳）");
r = R.resolveWorkspaceHome("about:blank", HOME, "/workspace");
ok(r.action !== "navigate", "about:blank 启动闸门期不导航（bootLoad 负责）");
r = R.resolveWorkspaceHome("", HOME, "/workspace");
ok(r.action !== "navigate", "空 URL 不导航");
r = R.resolveWorkspaceHome("https://web.telegram.org/k/", HOME, "/workspace");
ok(r.action !== "navigate", "跨源不猜");
r = R.resolveWorkspaceHome("http://127.0.0.1:18799/workspace/drafts", "", "/workspace?conv=q");
eq(r.action, "deliver", "无 home 口径（极老壳）→ 退回旧 deliver 行为");
// ⑤b 漂到后台页（同源非 /workspace/*）同样导航回家——admin 页内点「坐席工作台」同链路
r = R.resolveWorkspaceHome("http://127.0.0.1:18799/personas", HOME, "/workspace");
eq(r.action, "navigate", "同源后台页也算漂走 → navigate");

// ⑥ 深链解析口径与 _win_unique._deepLinkParts 对齐
eq(R.wsDeepLinkParts("/workspace"), null, "无深链 → null");
{
  const p = R.wsDeepLinkParts("/workspace?focus=f1&case=C1");
  eq(p.cid, "f1", "focus 别名");
  eq(p.caseId, "C1", "caseId 字段名与 renderer 契约一致");
}

// ── 源码静态契约 ────────────────────────────────────────────────────────────
const rendererJs = fs.readFileSync(path.join(__dirname, "..", "renderer", "renderer.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(__dirname, "..", "renderer", "index.html"), "utf8");
const mainJs = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
const draftHtml = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "web", "templates", "draft_review.html"), "utf8");
const winUniqueHtml = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "web", "templates", "_win_unique.html"), "utf8");

// ⑦ renderer：模块挂载早于 renderer.js；承接消费 resolveWorkspaceHome 且 navigate 分支真 loadURL
ok(
  indexHtml.indexOf('src="ws-home-route.js"') >= 0 &&
    indexHtml.indexOf('src="ws-home-route.js"') < indexHtml.indexOf('src="renderer.js"'),
  "index.html 未在 renderer.js 之前挂载 ws-home-route.js"
);
ok(/Inbox\.homeUrl\s*=\s*fullUrl/.test(rendererJs), "renderer 丢失 Inbox.homeUrl = fullUrl 登记（承接无家可回）");
const onOpenIdx = rendererJs.indexOf("onOpenWorkspace(");
ok(onOpenIdx >= 0, "renderer 丢失 onOpenWorkspace 承接");
const onOpenBody = rendererJs.slice(onOpenIdx, onOpenIdx + 1400);
ok(/routeWorkspaceOpen\(/.test(onOpenBody), "onOpenWorkspace 未经 routeWorkspaceOpen 判漂走");
ok(/resolveWorkspaceHome/.test(rendererJs), "renderer 丢失 WsHomeRoute.resolveWorkspaceHome 消费");
ok(
  /route\.action === "navigate"[\s\S]{0,200}Inbox\.wv\.loadURL\(route\.url\)/.test(onOpenBody),
  "onOpenWorkspace navigate 分支未真 loadURL —— 漂走的 webview 仍回不了家"
);
ok(/Inbox\.wv\.getURL\(\)/.test(rendererJs), "renderer 丢失 webview 当前 URL 读取（判漂走的唯一依据）");

// ⑧ main.js：wsub 弹窗句柄传入 handler；主窗接手后 closeHandedOffPopup 只收 wsub 不收 admin
ok(/function\s+closeHandedOffPopup\(/.test(mainJs), "main.js 丢失 closeHandedOffPopup");
ok(
  /slot === "workspace" && focusMainInbox\(url\)\)\s*\{\s*closeHandedOffPopup\(from\)/.test(mainJs),
  "openBackendPopup 主窗接手后未调用 closeHandedOffPopup（弹窗静默 deny 复发）"
);
ok(
  /makeBackendPopupHandler\(\{\s*win:\s*child,\s*slot:\s*slot\s*\}\)/.test(mainJs),
  "子弹窗 setWindowOpenHandler 未把 {win, slot} 交给 handler —— 收窗无句柄"
);
ok(
  /from\.slot !== "wsub"\)\s*return/.test(mainJs),
  "closeHandedOffPopup 丢失 wsub 专属闸 —— admin 后台弹窗会被误关"
);
ok(/function\s+makeBackendPopupHandler\(from\)/.test(mainJs), "makeBackendPopupHandler 未接收 from 参数");
ok(/openBackendPopup\(url,\s*from\)/.test(mainJs), "handler 未把 from 透传给 openBackendPopup");

// ⑨ draft_review.html：原生返回入口——同标签 href=/workspace、无 target、无 onclick（不依赖页级脚本）
const back = /<a[^>]*id="dr-back-chat"[^>]*>/.exec(draftHtml);
ok(!!back, "draft_review.html 丢失 #dr-back-chat 返回聊天入口");
ok(/href="\/workspace"/.test(back[0]), "dr-back-chat 的 href 必须是精确 /workspace（走 _win_unique 去重拦截）");
ok(!/target=/.test(back[0]), "dr-back-chat 禁 target=（会绕开去重、壳内多开一个工作台）");
ok(!/onclick=/.test(back[0]), "dr-back-chat 不得挂 onclick（导航不依赖页级脚本，脚本崩了也要能出去）");
// 与拦截器契约互锁：拦截器仍只认精确 /workspace 同标签 <a>（改了这里，返回入口就退化成裸导航）
ok(
  /href !== '\/workspace' && href\.indexOf\('\/workspace\?'\) !== 0/.test(winUniqueHtml),
  "_win_unique 委托拦截器的 /workspace 精确匹配被改 —— dr-back-chat 失去去重语义"
);
ok(/window\.__wsGoHome = _goHome/.test(winUniqueHtml), "_win_unique 丢失 __wsGoHome 暴露");

console.log(`ws-home-route OK (${passed} assertions)`);
