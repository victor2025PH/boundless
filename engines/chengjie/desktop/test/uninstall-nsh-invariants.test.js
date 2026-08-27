// uninstall-nsh-invariants.test.js —— 卸载器「数据处置」contract 的静态看门人
//
// build/installer.nsh 给卸载器加了「保留数据（默认）/ 彻底删除」页面与真删逻辑。
// NSIS 没有单测框架、错误只能在打包甚至用户机上暴露，所以把五条契约（C1-C5，
// 见 nsh 头注释）降维成源码文本不变量在这里钉死——违反任何一条，安装包拒绝出生
// （挂 npm test 与 predist* 三链）。
//
// 血泪对应表：
//   C1 isUpdated 硬守卫在一切 RMDir 之前 —— 升级链误删用户数据=灾难级，双保险
//      的「第二根保险丝」就是这条顺序断言。
//   C2 --delete-app-data 显式参数在场 —— 静默默认保数据（uninstall_chatx_node.ps1
//      的 /S 语义不变），运维清数据必须显式说出来。
//   C3 删除目标只许宏路径 —— RMDir 行出现 CJK 字面量（如「智聊」）意味着有人绕开
//      ${APP_FILENAME}，PS5.1-GBK 家族教训在 NSIS 同样成立。
//   C4 进程收割必须排除 '*Uninstall*' —— `_?=` 原地运行时按路径杀进程会把卸载器
//      自己杀掉（wipe 进行到一半死在现场）。
//   C5 删后校验 + 如实报告 —— 「半套数据库」比全旧更糟，锁死残留必须点名。
//   编码：文件必须 UTF-8 with BOM —— makensis 对无 BOM 文件按系统 ACP(GBK) 解码，
//   中文 LangString 全部乱码（编辑器一次「优化保存」就能弄丢 BOM，所以钉住）。

const fs = require("fs");
const path = require("path");

let passed = 0;
function ok(cond, msg) {
  if (!cond) {
    console.error("uninstall-nsh FAIL: " + msg);
    process.exit(1);
  }
  passed++;
}

const appDir = path.join(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(appDir, "package.json"), "utf8"));
const nshPath = path.join(appDir, "build", "installer.nsh");

// ---- 挂载点：include 已配置且文件存在 ---------------------------------------
const nsis = (pkg.build && pkg.build.nsis) || {};
ok(nsis.include === "build/installer.nsh", "package.json build.nsis.include 必须指向 build/installer.nsh（当前: " + nsis.include + "）");
ok(fs.existsSync(nshPath), "build/installer.nsh 不存在");

// ---- 语言集收敛：LangString 只写 zh/en，语言集不收敛会在 warningsAsErrors 下
//      产生几十条 "LangString not set" 直接打包炸掉 -------------------------------
const langs = nsis.installerLanguages;
ok(
  Array.isArray(langs) && langs.length === 2 && langs.includes("zh_CN") && langs.includes("en_US"),
  "build.nsis.installerLanguages 必须是 [zh_CN, en_US]（LangString 只有中英两套）"
);

// ---- 编码：UTF-8 BOM ---------------------------------------------------------
const raw = fs.readFileSync(nshPath);
ok(raw.length > 3 && raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf, "installer.nsh 丢失 UTF-8 BOM（makensis 将按 GBK 解码，中文文案会乱）");
const text = raw.toString("utf8").replace(/^\uFEFF/, "");

// ---- 三个宏齐备 --------------------------------------------------------------
function macroBody(name) {
  const m = text.match(new RegExp("!macro\\s+" + name + "[\\s\\S]*?!macroend"));
  ok(m, "缺少 !macro " + name);
  return m ? m[0] : "";
}
const header = macroBody("customHeader");
const welcome = macroBody("customUnWelcomePage");
const uninst = macroBody("customUnInstall");

// customUnWelcomePage 是「替换」语义——必须把默认欢迎页重新插回来
ok(welcome.includes("MUI_UNPAGE_WELCOME"), "customUnWelcomePage 必须重新插入 MUI_UNPAGE_WELCOME（否则默认欢迎页被顶没）");
ok(welcome.includes("UninstPage custom"), "customUnWelcomePage 必须注册自定义数据处置页");

// ---- C1: isUpdated 硬守卫在一切 RMDir 之前 -----------------------------------
const guardAt = uninst.indexOf("${isUpdated}");
const firstRm = uninst.indexOf("RMDir");
ok(guardAt >= 0, "customUnInstall 缺少 ${isUpdated} 升级守卫");
ok(firstRm > guardAt, "isUpdated 守卫必须出现在第一个 RMDir 之前（升级链绝不删数据）");

// ---- C2: 显式 CLI 通道 --------------------------------------------------------
ok(uninst.includes('"--delete-app-data"'), "customUnInstall 缺少 --delete-app-data 参数解析（静默运维清数据通道）");

// ---- 决策变量禁用寄存器（2026-08-21 实弹根因） ---------------------------------
// electron-builder 生成的 flag 测试宏（isUpdated/isForceRun/...）展开成
// `${StdUtils.TestParameter} $R9 "<flag>"` —— 会覆写 $R9。首版把 wipe 决策存
// $R9，`${If} ${isUpdated}` 一展开就把它踩成 "false"，整个增强 wipe 层成了
// 死代码（模板内置分支恰好删掉 Roaming 两目录，掩盖了死亡：updater 缓存幸存、
// 零 wipe log、零 RunOnce、卸载仅 8s）。决策必须走自有 Var，永不走寄存器。
ok(!/StrCpy \$R9\b/.test(uninst), "customUnInstall 禁止写 $R9（electron-builder flag 测试宏的暂存器，${If} ${isUpdated} 会覆写它）");
ok(welcome.includes("Var cxWipeGo"), "缺少 Var cxWipeGo 声明（wipe 决策自有变量）");
ok(uninst.includes('${If} $cxWipeGo == "1"'), "wipe 执行闸必须读 $cxWipeGo（自有决策变量）");

// ---- C3: 删除目标只许宏路径（删除行零 CJK 字面量） ----------------------------
// 真删经 cxRmDirRetry 宏（短锁重试：Defender 扫描刚拷入的 installer.exe 时
// 首删撞锁的实测教训），宏体内 RMDir 用形参 TARGET_DIR，调用点必须 APP_* 宏。
const cjk = /[\u4e00-\u9fff]/;
for (const line of text.split(/\r?\n/)) {
  const isDeleteLine = /^\s*RMDir/i.test(line) || /^\s*!insertmacro\s+cxRmDirRetry/.test(line);
  if (isDeleteLine) {
    ok(!cjk.test(line), "删除行出现 CJK 字面路径（必须用 ${APP_FILENAME} 族宏）: " + line.trim());
    ok(
      /\$\{APP_(FILENAME|PRODUCT_FILENAME|PACKAGE_NAME)\}/.test(line) || /\$\{TARGET_DIR\}/.test(line),
      "删除行必须由 APP_* 宏（或重试宏形参）推导: " + line.trim()
    );
  }
}
// 重试宏本体：预算式循环（覆盖 AV 扫描类短锁，实测 Defender 扫刚拷入的 500MB
// installer.exe 时 2s 单次重试盖不住）+ /REBOOTOK 兜底（长锁走
// PendingFileRenameOperations 重启自动清，Windows 正统路径）
const retry = macroBody("cxRmDirRetry");
ok(/IntOp[\s\S]*Sleep[\s\S]*Goto/.test(retry), "cxRmDirRetry 必须是预算式重试循环（计数+Sleep+Goto）");
ok(retry.includes("/REBOOTOK"), "cxRmDirRetry 必须带 /REBOOTOK 兜底（提升态卸载时重启自动清）");
// per-user 卸载不提升，REBOOTOK 写 HKLM 会静默无效（2026-08-21 实测 PFRO 零登记）
// —— HKCU RunOnce 才是普通用户真正会触发的兜底，两层缺一不可
ok(/WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce"/.test(retry), "cxRmDirRetry 必须带 HKCU RunOnce 兜底（per-user 卸载无提升，REBOOTOK 写不进 HKLM）");
// NSIS 不支持反斜杠续行——出现「空格+反斜杠+行尾」多半是把长行拆行的手滑
ok(!/ \\\r?\n/.test(retry), "cxRmDirRetry 内出现疑似续行反斜杠（NSIS 长行必须单行写）");
// 至少覆盖 userData 与 updater 缓存两类目标
ok(/!insertmacro cxRmDirRetry "\$APPDATA\\\$\{APP_FILENAME\}"/.test(uninst), "缺少 userData 主目录删除");
ok(/-updater"/.test(uninst), "缺少 electron-updater 缓存目录删除（wipe_chatx_data_node.ps1 口径）");

// ---- C4: 进程收割按路径 + 自杀防护 -------------------------------------------
ok(uninst.includes("Stop-Process"), "customUnInstall 缺少进程收割（后端/sidecar 锁住 db 时 RMDir 会静默残留）");
ok(uninst.includes("-notlike '*Uninstall*'"), "进程收割必须排除 '*Uninstall*'（_?= 原地运行时会杀掉卸载器自己）");
ok(!/taskkill[^\n]*智/.test(text), "禁止按 CJK 进程名杀进程");

// ---- C5: 删后校验 + 如实报告 --------------------------------------------------
ok(uninst.includes("IfFileExists"), "customUnInstall 缺少删后校验（IfFileExists）");
ok(uninst.includes("cxWipeLeftover"), "customUnInstall 缺少残留如实报告（cxWipeLeftover）");
ok(uninst.includes("cxWipeDone"), "customUnInstall 缺少成功确认（cxWipeDone）");

// ---- 用户可见路径 = 真实打包数据目录（2026-08-21 深审实锤三修） -----------------
// electron-builder 对非 ASCII productName 会把 APP_FILENAME 回落成包名
// （getWindowsInstallationDirName 的 ASCII-safe 判定），真实 userData 是
// $APPDATA\${APP_PRODUCT_FILENAME}（CJK 产品名目录）。用 APP_FILENAME 展示/打开
// = 错误路径 + 用户机上目录不存在 → 「打开数据文件夹」链接静默哑掉；
// 删后校验漏它 = 主数据目录锁死残留时谎报「已彻底清除」（违反 C5）。
ok(
  text.includes('!define CX_USERDATA_DIR "$APPDATA\\${APP_PRODUCT_FILENAME}"'),
  "缺少 CX_USERDATA_DIR 定义（必须优先 APP_PRODUCT_FILENAME——真实 CJK 数据目录）"
);
ok(welcome.includes("${CX_USERDATA_DIR}"), "数据处置页必须经 CX_USERDATA_DIR 展示/打开数据目录");
ok(
  !welcome.includes("$APPDATA\\${APP_FILENAME}"),
  "数据处置页禁止裸引用 $APPDATA\\${APP_FILENAME}（打包态那是 DEV 目录，对用户是错误路径）"
);
ok(
  uninst.includes('IfFileExists "$APPDATA\\${APP_PRODUCT_FILENAME}\\*.*"'),
  "删后校验缺少 APP_PRODUCT_FILENAME 主数据目录（漏了=锁死残留时谎报已清除）"
);

// ---- C6: 安装侧全家族收割（B57「升级风暴」的安装器半件，2026-08-24） -----------
// 库存 _CHECK_APP_RUNNING 只管主 exe（壳+ELECTRON_RUN_AS_NODE 边车同镜像名），
// PyInstaller backend.exe 不在覆盖面——旧后端孤儿抱着 18799 与 pyrogram 会话文件
// 不放，新后端一起来就撞 AuthKeyDuplicated → 全账号被强制注销（升级必重登根因）。
const checkRunning = macroBody("customCheckAppRunning");
ok(checkRunning.includes("_CHECK_APP_RUNNING"), "customCheckAppRunning 必须先插入库存 _CHECK_APP_RUNNING（保留优雅关闭+用户提示语义）");
ok(
  checkRunning.indexOf("_CHECK_APP_RUNNING") < checkRunning.indexOf("cxReapFamily"),
  "收割必须在库存检查之后（先优雅关壳——让 before-quit stopAndWait 带走后端——再扫孤儿）"
);
const reap = macroBody("cxReapFamily");
ok(reap.includes("Stop-Process"), "cxReapFamily 缺少 Stop-Process 收割");
ok(reap.includes("-notlike '*Uninstall*'"), "cxReapFamily 必须排除 '*Uninstall*'（C4 教训：卸载器 _?= 原地运行会杀自己）");
ok(reap.includes("-notlike '*-updater*'"), "cxReapFamily 必须排除 '*-updater*'（自动更新的安装器本体在 updater 缓存目录里运行，杀了=自杀）");
ok(reap.includes("'*\\${APP_PACKAGE_NAME}\\*'"), "cxReapFamily 路径必须反斜杠界定 ${APP_PACKAGE_NAME}（裸 '*name*' 会误匹配 -updater 兄弟目录）");
// 定义 customCheckAppRunning 会让模板跳过它自己的 getProcessInfo/Var pid（
// allowOnlyOneInstallerInstance.nsh 的 ifmacrondef 守卫），必须自带补齐：
ok(text.includes('!include "getProcessInfo.nsh"'), "定义 customCheckAppRunning 后必须自带 !include getProcessInfo.nsh（模板会跳过）");
ok(/^Var pid\r?$/m.test(text), "定义 customCheckAppRunning 后必须自带 Var pid（$pid 在两个编译单元的 CHECK 里都被引用，顶层安全）");

// ---- C7: 安装侧「保留/清空数据」页 + 执行闸 -----------------------------------
const insPage = macroBody("customPageAfterChangeDir");
ok(insPage.includes("Page custom cxInsDataPageCreate"), "customPageAfterChangeDir 必须注册安装侧数据处置页");
ok(insPage.includes("Var cxInsWipeGo"), "缺少 Var cxInsWipeGo（安装侧 wipe 决策自有变量，$R9 教训同 C1）");
ok(/\$\{If\} \$\{isUpdated\}\s*\r?\n\s*Abort/.test(insPage), "cxInsDataPageCreate 必须在 isUpdated（自动更新链）直接跳过页面");
ok(insPage.includes("${NSD_SetState} $cxInsRadioKeep ${BST_CHECKED}"), "安装侧数据页默认必须选中「保留数据」");
ok(insPage.includes("$(cxNeedConfirm)"), "安装侧 wipe 必须勾选不可恢复确认才放行");
ok(insPage.includes("CX_DATA_DIR_PKG"), "存在性检查/展示必须用 CX_DATA_DIR_PKG（实测 userData=%APPDATA%\\<包名>，只查 CJK 产品目录页面永远不出现）");

const insMac = macroBody("customInstall");
const insGuardAt = insMac.indexOf("${isUpdated}");
const insFirstRm = insMac.search(/RMDir|!insertmacro\s+cxRmDirRetry/);
ok(insGuardAt >= 0, "customInstall 缺少 ${isUpdated} 归零守卫");
ok(insFirstRm > insGuardAt, "customInstall 的 isUpdated 归零必须在第一个删除动作之前（自动更新绝不清数据，C1 镜像）");
ok(insMac.includes('"--wipe-app-data"'), "customInstall 缺少 --wipe-app-data 显式参数通道（静默清空安装，镜像卸载器 --delete-app-data）");
ok(!/StrCpy \$R9\b/.test(insMac), "customInstall 禁止写 $R9（flag 测试宏的暂存器）");
ok(insMac.includes('${If} $cxInsWipeGo == "1"'), "安装侧 wipe 执行闸必须读 $cxInsWipeGo（自有决策变量）");
ok(insMac.includes("IfFileExists"), "customInstall 缺少删后校验（IfFileExists）");
ok(insMac.includes("cxWipeLeftover"), "customInstall 缺少残留如实报告（cxWipeLeftover）");

// ---- LangString 双语齐平 ------------------------------------------------------
function langKeys(langMacro) {
  const keys = new Set();
  const re = new RegExp("LangString\\s+(\\w+)\\s+\\$\\{LANG_" + langMacro + "\\}", "g");
  let m;
  while ((m = re.exec(header))) keys.add(m[1]);
  return keys;
}
const en = langKeys("ENGLISH");
const zh = langKeys("SIMPCHINESE");
ok(en.size > 0, "customHeader 无英文 LangString");
for (const k of en) ok(zh.has(k), "LangString 缺中文: " + k);
for (const k of zh) ok(en.has(k), "LangString 缺英文: " + k);

// LangString 必须待在 customHeader 内（顶层写会在语言表加载前编译失败）
const outsideHeader = text.replace(header, "");
ok(!/^\s*LangString/m.test(outsideHeader), "LangString 只能定义在 customHeader 宏内（include 被前置到语言表加载之前）");

// Var 声明必须在宏体内：include 被编进安装器+卸载器两个单元，宏只在其中一个单元
// 展开——顶层 Var 在另一单元「声明未引用」，makensis -WX 把该警告变成硬失败
//（2026-08-21 首次打包实锤，warning 6001 treated as error）。
// 唯一豁免 = `Var pid`：customCheckAppRunning 在两个单元的 CHECK_APP_RUNNING 里
// 都展开（installSection + un.checkAppRunning），$pid 两边都被引用，顶层反而是
// 唯一正确位置（塞进宏体会在第二次插入时重复声明）。
const outsideMacros = text
  .replace(/!macro[\s\S]*?!macroend/g, "")
  .replace(/^Var pid\r?$/m, "");
ok(!/^\s*Var\s/m.test(outsideMacros), "Var 声明必须在宏体内（顶层 Var 在另一编译单元未引用，-WX 下打包必炸；唯一豁免=Var pid）");

// ---- 自指：本测试挂进 test 与 predist 三链（防未来掉链） ----------------------
for (const key of ["test", "predist", "predist:win", "predist:win:clean"]) {
  const s = (pkg.scripts && pkg.scripts[key]) || "";
  ok(s.includes("uninstall-nsh-invariants"), "package.json scripts." + key + " 未挂 uninstall-nsh-invariants.test.js");
}

console.log("uninstall-nsh-invariants: " + passed + " assertions passed");
