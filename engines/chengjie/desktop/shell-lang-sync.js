"use strict";

// 壳语言跟随工作台切换（#151，2026-09-02）——纯函数层，main.js 与 test/ 共用。
//
// 实锤（skuio 原图 _1086）：工作台切英文后（/set_lang?lang=en 整页跳转），界面主体全英，
// 唯「文件/编辑/视图/窗口/帮助」与帮助下拉仍中文。根因不是词条硬编码——菜单早就走
// SHELL_STR/SS()——而是 SS() 读的壳配置 unified_inbox.lang 只由首启向导写，网页里切语言
// 只改后端 cookie/落库偏好，壳配置永远停在装机那一刻；且 Menu 构建是启动期一次性。
//
// 修向：主进程监听后台页 webContents 的导航，命中 /set_lang?lang=<x> 即①同步壳配置
// ②Menu.setApplicationMenu 重建 ③广播 cx-shell-lang 让壳 renderer 静态文案重取词。
// 页面重载后自会重拉 menuSpec()（页内五菜单），无需额外推送。
//
// 本文件只做「URL → 语言标签 → 配置补丁」三步纯函数：零 electron 依赖，门禁可直跑。

/** 后台页导航 URL 里的 /set_lang?lang=<tag>；不是切语言导航 → null。
 *  只认路径恰为 /set_lang（含尾斜杠）的主文档导航；tag 缺省（"zh"，与后端默认一致）。 */
function langFromSetLangUrl(rawUrl) {
  let u;
  try { u = new URL(String(rawUrl || "")); } catch (e) { return null; }
  if (!/^https?:$/.test(u.protocol)) return null;
  if (u.pathname.replace(/\/+$/, "") !== "/set_lang") return null;
  const tag = String(u.searchParams.get("lang") || "zh").trim();
  return tag || "zh";
}

/** 语言标签 → 壳配置值。auto＝回到「跟随系统」（空串，shellLang() 走 app.getLocale）；
 *  其余小写、连字符归一为下划线（zh-Hant → zh_hant，与首启向导写法一致）；
 *  含非法字符/过长的脏值 → null（不写配置，不重建）。 */
function shellConfigLangForTag(tag) {
  const t = String(tag || "").trim().toLowerCase().replace(/-/g, "_");
  if (!t) return null;
  if (t === "auto") return "";
  if (!/^[a-z]{2,3}(_[a-z0-9]{2,8})?$/.test(t)) return null;
  return t;
}

/** 完整决策：给定当前配置值与导航 URL，返回 { lang } 补丁值或 null（无需动作）。
 *  同值不重写（/set_lang 每次整页跳转都会经过这里，别让每次导航都写一遍 config.json）。 */
function shellLangPatchForNavigation(currentLang, rawUrl) {
  const tag = langFromSetLangUrl(rawUrl);
  if (tag == null) return null;
  const next = shellConfigLangForTag(tag);
  if (next == null) return null;
  const cur = String(currentLang == null ? "" : currentLang).trim().toLowerCase().replace(/-/g, "_");
  if (cur === next) return null;
  return { lang: next };
}

module.exports = { langFromSetLangUrl, shellConfigLangForTag, shellLangPatchForNavigation };
