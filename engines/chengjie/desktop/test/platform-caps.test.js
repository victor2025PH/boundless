// platform-caps.test.js — Path2 assist-only 能力矩阵门禁
const assert = require("assert");
const caps = require("../renderer/platform-caps.js");

function ok(cond, msg) {
  assert.ok(cond, msg);
}

ok(caps.isAssistOnlyEmbed("messenger"), "messenger 官方网页=assist-only");
ok(caps.isAssistOnlyEmbed("Messenger"), "平台名大小写不敏感");
ok(caps.isAssistOnlyEmbed("instagram"), "instagram assist-only");
ok(caps.isAssistOnlyEmbed("zalo"), "zalo assist-only");
ok(caps.isAssistOnlyEmbed("x"), "x assist-only");
ok(!caps.isAssistOnlyEmbed("telegram"), "telegram 非 assist-only（可 ingest）");
ok(!caps.isAssistOnlyEmbed("whatsapp"), "whatsapp 非 assist-only");
ok(!caps.isAssistOnlyEmbed(""), "空平台不标 assist-only");

ok(caps.prefersInboxAuto("messenger"), "messenger 全自动优先收件箱");
ok(caps.prefersInboxAuto("line"), "line 全自动优先收件箱");
ok(caps.prefersInboxAuto("instagram"), "assist-only 亦 prefer inbox auto");
ok(!caps.prefersInboxAuto("telegram"), "telegram 不强制 prefer inbox");

ok(caps.assistOnlyTabTag("messenger") === "人工", "messenger 标签副标=人工");
ok(caps.assistOnlyTabTag("telegram") === "", "telegram 无副标");
ok(String(caps.assistOnlyBannerText("messenger")).indexOf("人工操作台") >= 0
    && String(caps.assistOnlyBannerText("messenger")).indexOf("统一收件箱") >= 0,
  "messenger 诚实条点名「人工操作台」并保留「统一收件箱」注解（新旧名对照）");
ok(String(caps.assistOnlyBannerText("messenger")).indexOf("服务器") >= 0,
  "messenger 诚实条点名服务器登录（与 Path2 登录分裂）");
ok(caps.assistOnlyBannerText("telegram") === "", "telegram 无诚实条");
ok(String(caps.assistOnlyMenuHint("messenger")).indexOf("人工") >= 0,
  "新增菜单 messenger 带人工提示");

console.log("platform-caps.test.js OK (" + Object.keys(caps.ASSIST_ONLY_EMBED).length + " assist-only platforms)");
