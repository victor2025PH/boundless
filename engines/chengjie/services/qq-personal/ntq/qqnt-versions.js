/**
 * QQNT 真驱动的版本表（B 段，2026-09-10）——「不在表里的 QQ 版本一律不注入」的唯一依据。
 *
 * 为什么是「表」而不是「探测」：QQNT 是 Electron 壳 + wrapper.node 内核，我们的注入是
 * ① 改运行时目录里 resources/app/package.json 的 main 指到自己的 launcher，
 * ② launcher 在 QQ 主进程里先 require 我们的 agent 再 require 原 main，
 * ③ agent 挂住 wrapper.node 导出的 NodeIQQNTWrapperSession / NodeIKernelLoginService，
 *    代理各 service 的 addKernelXxxListener 拿事件、直接调 service 方法收发。
 * ①② 的路径与 ③ 的 service / 方法名 / 参数形状**随腾讯发版会变**（手册 §4），所以每个
 * build 一条描述（相当于传统注入里的「偏移表」）：路径 + 方法签名版本。腾讯发了新版本、
 * 表里没有 → resolveQqntTarget() 抛 unsupported → driver.js 回退 mock 并把原因写进
 * /health.driver_reason；**绝不拿旧表硬试新版本**（轻则收发失败，重则触发风控）。
 *
 * 表的维护：C 段真机验证通过一版才加一行；与 locate.js SUPPORTED_QQ_BUILDS（下载锁定表）
 * 是两张表——下载表管「能装什么」，本表管「装了能不能注入」，一个 build 通常两表都在，
 * 但新版本先进下载表跑 C 段、通过后才进本表。
 *
 * 字段：
 *   version     完整版本串（versions/config.json.curVersion）
 *   appRel      resources/app 相对 QQ 根目录的位置（{version} 占位）
 *   entryMain   原 package.json.main（launcher 转发到它）
 *   wrapperRel  wrapper.node 相对 appRel
 *   api         agent 侧按此选方法签名（见 qqnt-agent.js API_PROFILES）
 *   verified    C 段验证记录（日期 / 谁 / 挂机时长），"" = 尚未真机验证
 */
export const SUPPORTED_QQNT = {
  "44343": {
    version: "9.9.26-44343",
    appRel: "versions/{version}/resources/app",
    entryMain: "./app_launcher/index.js",
    wrapperRel: "wrapper.node",
    api: "nt-2025q3",
    verified: "",
  },
  "39038": {
    version: "9.9.21-39038",
    appRel: "versions/{version}/resources/app",
    entryMain: "./app_launcher/index.js",
    wrapperRel: "wrapper.node",
    api: "nt-2025q3",
    verified: "",
  },
};

export class UnsupportedQqVersion extends Error {
  constructor(version, build) {
    super(`unsupported qq version ${version || "?"}${build ? ` (build ${build})` : ""}`);
    this.name = "UnsupportedQqVersion";
    this.qq_version = String(version || "");
    this.qq_build = String(build || "");
  }
}

/** 已装 QQ 是否在真驱动版本表里（与 locate.isSupportedBuild 的下载表分开判）。 */
export function isInjectableBuild(build) {
  return Object.prototype.hasOwnProperty.call(SUPPORTED_QQNT, String(build || ""));
}

/**
 * 把 locateQQ() 的结果解析成注入目标；任何一步不满足即抛（调用方据此回退 mock）：
 *   - 未安装                      → Error("qq not installed")
 *   - build 不在表                 → UnsupportedQqVersion
 *   - 非运行时目录（用户自己的 QQ）→ Error("qq install is user-owned ...")：我们只改自己下载的那份
 */
export function resolveQqntTarget(loc, opts = {}) {
  if (!loc || !loc.installed) throw new Error("qq not installed");
  const spec = SUPPORTED_QQNT[String(loc.build || "")];
  if (!spec) throw new UnsupportedQqVersion(loc.version, loc.build);
  if (loc.source !== "runtime" && !opts.allowUserInstall) {
    throw new Error(`qq install is user-owned (source=${loc.source}); refusing to patch it - set QQ_RUNTIME_DIR or use on-demand download`);
  }
  const appRel = spec.appRel.replace("{version}", loc.version);
  return {
    build: String(loc.build), version: loc.version, root: loc.root, exe: loc.exe,
    appDir: appRel, entryMain: spec.entryMain, wrapperRel: spec.wrapperRel, api: spec.api,
    verified: spec.verified,
  };
}
