"use strict";

// 副驾能力橱窗门禁：内容不变量（必须命名**真差异化**能力,不能退化成与翻译型竞品同质的空话）
// + 结构契约。跑法：node test/copilot-capabilities.test.js

const assert = require("assert");
// 2026-08-12 数据文件迁入 shared/copilot/ 单源（web 统一 App 与桌面原生空态同吃这份）
const { capabilityShowcase, showcaseHeadline } = require("../renderer/shared/copilot/cp-capabilities.js");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

const items = capabilityShowcase();

// 结构：至少 5 条,每条 {key,title,desc} 齐全且非空,key 唯一
ok("至少 5 条能力", Array.isArray(items) && items.length >= 5);
ok("每条 key/title/desc 齐全", items.every((it) =>
  it && it.key && String(it.title || "").trim() && String(it.desc || "").trim()));
ok("key 唯一", new Set(items.map((it) => it.key)).size === items.length);

// 内容不变量：橱窗存在的全部意义＝显性化「翻译型竞品没有的能力」。逐个钉住真差异化关键词,
// 谁哪天把它改成「一键翻译 / 快速回复」这类同质空话,这里立刻红。
const blob = items.map((it) => it.title + " " + it.desc).join(" ");
ok("覆盖 AI 人设拟稿", /人设/.test(blob) && /拟稿|拟回复/.test(blob));
ok("覆盖 受控出站/人审", /人审|受控出站|审核/.test(blob));
ok("覆盖 知识库", /知识库/.test(blob));
ok("覆盖 关系阶段/客户档案", /关系阶段|客户档案|画像/.test(blob));
ok("覆盖 语音克隆", /语音/.test(blob) && /克隆|声线/.test(blob));

// 定位语必须打「不止翻译」这个对标点（差异化的一句话总纲）
ok("定位语点明「不止翻译」", /不止翻译|不只翻译/.test(showcaseHeadline()));

// 英文档（统一 App en 语言消费）：每条 *_en 齐全、headline("en") 保住对标点
ok("每条 title_en/desc_en 齐全", items.every((it) =>
  String(it.title_en || "").trim() && String(it.desc_en || "").trim()));
ok("en 定位语点明 beyond translation", /beyond translation/i.test(showcaseHeadline("en")));
ok("zh 缺省行为不变（零参仍出中文）", /不止翻译/.test(showcaseHeadline()));

// ── 静态接线门禁：橱窗数据写好但没渲染上,就是又一个「模块写了没接」静默缺陷 ──────────
const fs = require("fs");
const path = require("path");
const rDir = path.join(__dirname, "..", "renderer");
const rendererJs = fs.readFileSync(path.join(rDir, "renderer.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(rDir, "index.html"), "utf8");
const styleCss = fs.readFileSync(path.join(rDir, "style.css"), "utf8");

ok("index.html 加载 shared/copilot/cp-capabilities.js（单源）",
  /<script src="shared\/copilot\/cp-capabilities\.js">/.test(indexHtml));
ok("统一 App(app.html) 同样消费该单源（空态能力橱窗）",
  /copilot\/cp-capabilities\.js/.test(fs.readFileSync(
    path.join(__dirname, "..", "renderer", "shared", "copilot", "app.html"), "utf8")));
ok("renderer 定义 renderCopilotShowcase 并调用 capabilityShowcase",
  /function renderCopilotShowcase/.test(rendererJs) && rendererJs.indexOf("capabilityShowcase()") > 0);
ok("initCopilot 引导期渲染橱窗",
  /renderCopilotShowcase\(\)/.test(rendererJs.slice(rendererJs.indexOf("function initCopilot"))
    || rendererJs) && rendererJs.indexOf("renderCopilotShowcase()") > 0);
ok("渲染进 #cp-empty",
  /\$\("cp-empty"\)/.test(rendererJs) && rendererJs.indexOf("cp-showcase") > 0);
ok("style.css 有 .cp-showcase 样式", styleCss.indexOf(".cp-showcase") >= 0
  && /\.cp-sc-dot\s*\{/.test(styleCss));

console.log("copilot-capabilities.test.js: " + pass + " passed");
