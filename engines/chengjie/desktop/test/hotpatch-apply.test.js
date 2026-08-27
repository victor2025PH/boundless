"use strict";
// 热补丁决策核门禁（hotpatch-apply.js）：路径白名单 / manifest 规范化 /
// 与全量更新的让路关系 / 同基线补丁号比较。Node 直跑，零 Electron 依赖。

const assert = require("assert");
const hp = require("../hotpatch-apply.js");

let passed = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  passed++;
  console.log(`  ok - ${msg}`);
}

ok(hp.isSafeRelPath("app.asar"), "app.asar 允许");
ok(hp.isSafeRelPath("shared/inject/foo.js"), "shared/ 允许");
ok(hp.isSafeRelPath("services/whatsapp-baileys/server.js"), "sidecar 允许");
ok(hp.isSafeRelPath("backend/backend.exe"), "backend/ 允许（显式纳入时）");
ok(!hp.isSafeRelPath("../app.asar"), "拒绝 .. 穿越");
ok(!hp.isSafeRelPath("C:/Windows/notepad.exe"), "拒绝绝对路径");
ok(!hp.isSafeRelPath("ffmpeg/ffmpeg.exe"), "拒绝 ffmpeg");
ok(!hp.isSafeRelPath("d3dcompiler_47.dll"), "拒绝安装根目录原生文件");

const raw = {
  kind: "chatx_hotpatch",
  baseAppVersion: "1.0.56",
  patch: 3,
  zip: "ChatX-Hotpatch-1.0.56-p3.zip",
  sha256: "aa",
  files: [
    { path: "app.asar", sha256: "bb", bytes: 10 },
    { path: "../evil", sha256: "cc" },
    { path: "app.asar", sha256: "dd" },
    { path: "ffmpeg/ffmpeg.exe", sha256: "ee" },
  ],
};
const m = hp.normalizeManifest(raw);
ok(m.files.length === 1 && m.files[0].path === "app.asar", "规范化：危险路径与重复全部丢弃");
ok(m.patch === 3, "patch 解析为整数");

ok(hp.decide({
  appVersion: "1.0.56",
  local: { baseAppVersion: "1.0.56", patch: 1 },
  remote: m,
}).action === "apply", "同基线更高补丁号 → apply");

ok(hp.decide({
  appVersion: "1.0.56",
  local: { baseAppVersion: "1.0.56", patch: 3 },
  remote: m,
}).reason === "already_applied", "已是该补丁 → skip");

ok(hp.decide({
  appVersion: "1.0.55",
  local: {},
  remote: m,
}).action === "mismatch", "基线不一致 → mismatch");

ok(hp.decide({
  appVersion: "1.0.56",
  local: {},
  remote: m,
  latestFullVersion: "1.0.57",
}).action === "defer_full", "全量更新可用时热补丁让路");

ok(hp.decide({
  appVersion: "1.0.56",
  local: { baseAppVersion: "1.0.55", patch: 9 },
  remote: m,
}).action === "apply", "上一档安装包留下的补丁记录不阻断新基线");

ok(hp.displayLabel("1.0.56", 3) === "1.0.56+p3", "展示串 1.0.56+p3");
ok(hp.displayLabel("1.0.56", 0) === "1.0.56", "无补丁时不加 +p");
ok(hp.hotpatchUrlFromPublish("https://bd2026.cc/downloads/") === "https://bd2026.cc/downloads/hotpatch.json", "发布源拼出 hotpatch.json");

// ══ 落地侧纯函数（hotpatch-stage.js）══════════════════════════════════════
const hs = require("../hotpatch-stage.js");

ok(hs.isSha256("a".repeat(64)), "64 位十六进制=合法 sha256");
ok(!hs.isSha256("aa"), "短串不是 sha256（校验形同虚设的 manifest 必须被挡）");
ok(!hs.isSha256("z".repeat(64)), "非十六进制不是 sha256");

// ── zip 地址：只认 manifest 同源 ──────────────────────────────────────────
const MU = "https://bd2026.cc/downloads/hotpatch.json";
ok(hs.zipUrlFrom(MU, "ChatX-Hotpatch-1.0.56-p3.zip") === "https://bd2026.cc/downloads/ChatX-Hotpatch-1.0.56-p3.zip",
  "同目录文件名拼出下载地址");
ok(hs.zipUrlFrom(MU, "https://bd2026.cc/downloads/x.zip") === "https://bd2026.cc/downloads/x.zip", "同源绝对地址放行");
ok(hs.zipUrlFrom(MU, "https://evil.example/x.zip") === "", "跨域下载地址拒绝（指针被改也只能从本域拉）");
ok(hs.zipUrlFrom(MU, "../../etc/x.zip") === "", "拒绝 .. 穿越");
ok(hs.zipUrlFrom(MU, "/abs/x.zip") === "", "拒绝绝对路径");
ok(hs.zipUrlFrom(MU, "sub\\x.zip") === "", "拒绝反斜杠");
ok(hs.zipUrlFrom(MU, "") === "" && hs.zipUrlFrom("", "x.zip") === "", "缺参数返回空串");

// ── manifest 自洽：规范化丢过条目 = 整份拒绝（半套补丁比不套更危险）─────────
ok(hs.manifestTampered(raw, m), "原始 4 条被规范化剩 1 条 → 判为动过手脚");
const clean = { files: [{ path: "app.asar", sha256: "bb" }] };
ok(!hs.manifestTampered(clean, hp.normalizeManifest(clean)), "干净 manifest 不误判");
ok(!hs.manifestTampered({}, hp.normalizeManifest({})), "空 manifest 不误判（另有 empty_files 挡）");

// ── 每个文件都必须带 sha256 ───────────────────────────────────────────────
ok(hs.filesMissingHash(m).length === 1 && hs.filesMissingHash(m)[0] === "app.asar", "短哈希被点名");
const hashed = hp.normalizeManifest({
  kind: "chatx_hotpatch", baseAppVersion: "1.0.56", patch: 1,
  files: [{ path: "app.asar", sha256: "C".repeat(64) }],
});
ok(hs.filesMissingHash(hashed).length === 0, "合法 sha256（大写亦可，规范化已转小写）通过");

// ── 暂存文件名：远端串会变成本地文件名，先过字符白名单 ──────────────────────
const nm = hs.stagedNames("ChatX-Hotpatch-1.0.56-p3.zip");
ok(nm.zip === "ChatX-Hotpatch-1.0.56-p3.zip", "正常 zip 名原样");
ok(nm.manifest === "ChatX-Hotpatch-1.0.56-p3.json",
  "manifest 与 zip 同基名（helper 缺 -Manifest 时按 ChangeExtension 找同名，多一层兜底）");
const evil = hs.stagedNames("../../windows/system32/evil.zip");
ok(!evil.zip.includes("/") && !evil.zip.includes("\\") && !evil.zip.includes(".."),
  "恶意 zip 名被净化（落点永远在 staging 内）");
ok(hs.stagedNames("").zip === "hotpatch.zip", "空名回落默认文件名");

// ── helper 包装脚本 ───────────────────────────────────────────────────────
const ws = hs.wrapperScript();
// eslint-disable-next-line no-control-regex
ok(!/[^\x00-\x7F]/.test(ws), "包装脚本 ASCII-only（PS 5.1 控制台按 GBK 解码，中文=语法错）");
ok(/\$ShellPid/.test(ws) && !/param\([^)]*\$Pid\b/s.test(ws),
  "等待参数叫 ShellPid 而非 $Pid（后者是 PowerShell 自动变量，param 重名直接语法错）");
ok(/Get-Process -Id \$ShellPid/.test(ws) && /Start-Sleep/.test(ws),
  "先等本壳进程消失再动文件（app.asar 运行中被锁）");
const iWait = ws.indexOf("Get-Process -Id $ShellPid");
const iApply = ws.indexOf("& powershell @argv");
ok(iWait > -1 && iApply > iWait, "等待在替换之前（顺序反了就是拷贝必失败）");
ok(/'-Relaunch'/.test(ws), "落地后拉起 exe（用户点的是「重启更新」，不能停在退出）");
ok(/if \(\$InstallDir\) \{ \$argv \+= /.test(ws), "InstallDir 为空时不传，退回 helper 自己的探测");
ok(/Out-File -FilePath \$Log -Append/.test(ws), "全程落日志（失败时这是唯一的第一现场）");

// ── spawn 参数 ────────────────────────────────────────────────────────────
const argsNoDir = hs.wrapperArgs({ runner: "R.ps1", shellPid: 4242, script: "A.ps1", zip: "z.zip", manifest: "z.json", log: "l.log" });
ok(argsNoDir[argsNoDir.indexOf("-File") + 1] === "R.ps1", "-File 指向包装脚本");
ok(argsNoDir[argsNoDir.indexOf("-ShellPid") + 1] === "4242", "PID 以字符串传入");
ok(!argsNoDir.includes("-InstallDir"), "无安装目录时不传该开关");
const argsDir = hs.wrapperArgs({ runner: "R.ps1", shellPid: 1, installDir: "C:\\Users\\x\\Programs\\telegram-ai-desktop" });
ok(argsDir[argsDir.indexOf("-InstallDir") + 1] === "C:\\Users\\x\\Programs\\telegram-ai-desktop",
  "有安装目录时显式传（NSIS 允许自选目录，探测清单会打错副本）");
ok(hs.wrapperArgs({}).indexOf("-ShellPid") > -1 && hs.wrapperArgs({})[hs.wrapperArgs({}).indexOf("-ShellPid") + 1] === "0",
  "缺参数不抛异常（PID=0 → 包装脚本跳过等待）");

// ══ main.js 接线契约（静态源码扫描）════════════════════════════════════════
// 这几条都是「写错了不会报错、只会静默做错事」的接线，靠人 review 不可靠。
const fs = require("fs");
const path = require("path");
const appDir = path.join(__dirname, "..");
const mainJs = fs.readFileSync(path.join(appDir, "main.js"), "utf8");

ok(/setupAutoUpdate\(\);\s*\n\s*setupHotpatch\(\);/.test(mainJs),
  "setupHotpatch 在 setupAutoUpdate 之后启动（热补丁要按整包版本让路，先后有意义）");
ok(/function setupHotpatch\(\)[\s\S]*?if \(!app\.isPackaged/.test(mainJs), "setupHotpatch 仅打包态生效");
ok(/setInterval\(\(\) => runHotpatchCheck\("interval"\), 4 \* 60 \* 60 \* 1000\)/.test(mainJs), "每 4h 复查");
ok(/runHotpatchCheck\("boot"\)/.test(mainJs) && /runHotpatchCheck\("resume"\)/.test(mainJs), "启动与唤醒各有一查");

// decide 的四个动作里只有 apply 会动手；其余必须原样交回 electron-updater
ok(/if \(d\.action !== "apply"\) \{[\s\S]{0,240}?return;/.test(mainJs),
  "defer_full / mismatch / skip 一律 return（不插手整包更新链路）");

// B57：先等后端真死，再 spawn helper。顺序反了 = 新旧后端双连踢光 Telegram 账号
const inst = /async function installHotpatchNow\([\s\S]*?\n\}/.exec(mainJs);
ok(!!inst, "installHotpatchNow 存在");
const instBody = inst ? inst[0] : "";
ok(/await shutdownBackendAndWait\(\)/.test(instBody), "落地前 await shutdownBackendAndWait（B57）");
ok(instBody.indexOf("await shutdownBackendAndWait()") < instBody.indexOf("spawn("),
  "等后端死在 spawn helper 之前（顺序反了就是 B57 升级风暴重演）");
ok(/detached: true/.test(instBody) && /child\.unref\(\)/.test(instBody),
  "helper 必须脱离本进程（否则本进程一退它就跟着死，谁也换不了文件）");
ok(/app\.relaunch\(\)/.test(instBody),
  "spawn 失败时拉回旧版本——后端已停，最坏结果应停在「没更新」而不是「应用没了」");

// 重启入口：热补丁分支必须先于 updater 分支判定
const restart = /ipcMain\.handle\("desktop:update-restart"[\s\S]*?\n\}\);/.exec(mainJs);
ok(!!restart, "update-restart handler 存在");
const rBody = restart ? restart[0] : "";
ok(rBody.indexOf("_hotpatchReady") < rBody.indexOf("_getUpdater()"),
  "先判热补丁再判 updater（两者共用同一横幅，判反了就是装错东西）");
ok(/_hotpatchFilesPresent\(hp\)/.test(rBody), "停后端之前先确认暂存还在");

// 整包永远优先：两个 updater 事件都要作废已暂存的热补丁
ok(/update-available[\s\S]{0,400}?_discardStagedHotpatch/.test(mainJs)
  && /update-downloaded[\s\S]{0,400}?_discardStagedHotpatch/.test(mainJs),
  "整包更新接管时作废暂存热补丁（否则「点重启」会走进热补丁分支）");

// 单横幅原则：复用 _updateInfo + broadcastShellNotice，不新建任何 UI/桥/词条
ok(/_updateInfo = \{ phase: "downloaded", version: ready\.version, percent: 100 \}/.test(mainJs),
  "就绪后复用现有「已就绪」状态（横幅、按钮、i18n 全部沿用）");
// 锚在状态赋值而非日志文案上：日志措辞/logger 会变，「暂存就绪必须立刻广播横幅」
// 才是契约——少了这一步补丁装好了而用户永远看不到那个「立即重启更新」按钮。
ok(/_hotpatchReady = ready;[\s\S]{0,300}?broadcastShellNotice\(\)/.test(mainJs), "就绪即广播现有通知");
for (const f of ["renderer/index.html", "renderer/renderer.js", "shell-preload.js"]) {
  ok(!/hotpatch/i.test(fs.readFileSync(path.join(appDir, f), "utf8")),
    `${f} 不含 hotpatch 字样（热补丁不得引入第二套横幅/IPC 桥）`);
}

// 版本展示与遥测：展示串带 +pN，机器版本仍是纯 semver
ok(/function displayVersion\(\)[\s\S]*?hotpatchApply\.displayLabel\(base, localPatchLevel\(\)\)/.test(mainJs),
  "displayVersion 缀热补丁号（1.056+p3）");
ok(/manifest_version: app\.getVersion\(\)/.test(mainJs), "遥测 manifest_version 保持纯 semver");
ok(/patch: localPatchLevel\(\)/.test(mainJs), "遥测另带可选 patch 字段（老服务端会丢弃）");

// 落地脚本单一事实源：随包镜像由 copy-shared 从 deploy/desktop 同步，不许再写一份
const copyShared = fs.readFileSync(path.join(appDir, "copy-shared.js"), "utf8");
ok(/deploy["'\s,)]*.{0,40}apply_chatx_hotpatch_node\.ps1/s.test(copyShared),
  "copy-shared 从 deploy/desktop 镜像落地脚本（两份脚本迟早漂移，漂移那天=装了一半的补丁）");
ok(/hotpatch["'\s,)]*.{0,60}apply_chatx_hotpatch_node\.ps1/s.test(copyShared)
  && /path\.join\(__dirname, "hotpatch", "apply_chatx_hotpatch_node\.ps1"\)/.test(mainJs),
  "main.js 找的正是 copy-shared 的落点");

const applyPs1 = fs.readFileSync(
  path.join(appDir, "..", "..", "..", "deploy", "desktop", "apply_chatx_hotpatch_node.ps1"), "utf8");
ok(/\[string\]\$InstallDir = ""/.test(applyPs1), "落地脚本接受可选 -InstallDir");
ok(/\$_ -and \(Test-Path \$_\)/.test(applyPs1), "探测清单容忍空的 InstallDir（Test-Path '' 会抛）");

// 载荷区域 opt-in：2026-08-27 实测 services\（边车 node_modules）4700 文件 / 518MB 原始
// → 226MB zip，而 asar-only 是 1.2MB。把 services 塞回默认＝一行 JS 修复也要发 226MB，
// 「不必等整包」这个卖点当场消失，故把默认载荷钉死。
const makePs1 = fs.readFileSync(
  path.join(appDir, "..", "..", "..", "deploy", "desktop", "make_chatx_hotpatch.ps1"), "utf8");
ok(/\$dirs = @\('shared', 'app\.asar\.unpacked'\)/.test(makePs1),
  "默认载荷只含 asar + inject（~1.2MB）");
for (const [sw, area] of [["IncludeServices", "services"], ["IncludeBackend", "backend"], ["IncludeSeed", "seed-data"]]) {
  ok(new RegExp(`if \\(\\$${sw}\\) \\{ \\$dirs \\+= '${area}'`).test(makePs1),
    `${area}\\ 属 opt-in（-${sw}）`);
}
ok(/omitted: /.test(makePs1),
  "每次打包打印「哪些区没进」——半套补丁（换了 asar 边车留旧）比不打更糟，必须让操作者看见");

console.log(`\n${passed} assertions passed`);
