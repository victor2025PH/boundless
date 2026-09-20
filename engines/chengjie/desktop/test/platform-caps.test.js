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

// i18n 边界（2026-08-19）：本模块只回 key，取词在展示层。断言从「文案含某词」
// 改为「key 稳定 + 两语词典都有该条 + 语义要点仍在」——文案会本地化漂移，key 不会。
ok(caps.assistOnlyTabTagKey("messenger") === "caps.tag_manual", "messenger 标签副标 key");
ok(caps.assistOnlyTabTagKey("telegram") === "", "telegram 无副标");
ok(caps.assistOnlyBannerKey("messenger") === "caps.banner_messenger",
  "messenger 走专属诚实条（多一句服务器完整登录）");
ok(caps.assistOnlyBannerKey("instagram") === "caps.banner_generic", "其余 assist-only 走通用条");
ok(caps.assistOnlyBannerKey("telegram") === "", "telegram 无诚实条");
ok(caps.assistOnlyMenuHintKey("messenger") === "caps.menu_manual", "新增菜单 messenger 带人工提示 key");
ok(caps.assistOnlyMenuHintKey("telegram") === "", "telegram 菜单无副标");

// 词典覆盖 + 语义不变量：把「诚实条必须点名人工操作台/统一收件箱/服务器登录」这条
// 产品红线钉在文案侧（英文同样要点名），否则 i18n 化会顺手把诚实条讲成一句废话。
const DICT = require("../renderer/shell-i18n.js")._dict;
for (const k of ["caps.tag_manual", "caps.menu_manual", "caps.banner_messenger", "caps.banner_generic"]) {
  ok(DICT.zh[k] && DICT.en[k], "词典双语齐备：" + k);
}
ok(DICT.zh["caps.banner_messenger"].indexOf("人工操作台") >= 0
   && DICT.zh["caps.banner_messenger"].indexOf("统一收件箱") >= 0
   && DICT.zh["caps.banner_messenger"].indexOf("服务器") >= 0,
  "zh messenger 诚实条点名 人工操作台/统一收件箱/服务器登录");
ok(/Manual Console/i.test(DICT.en["caps.banner_messenger"])
   && /Unified Inbox/i.test(DICT.en["caps.banner_messenger"])
   && /server/i.test(DICT.en["caps.banner_messenger"]),
  "en messenger 诚实条点名 Manual Console / Unified Inbox / server login");
ok(/Manual Console/i.test(DICT.en["caps.banner_generic"]),
  "en 通用诚实条点名 Manual Console");

console.log("platform-caps.test.js OK (" + Object.keys(caps.ASSIST_ONLY_EMBED).length + " assist-only platforms)");
