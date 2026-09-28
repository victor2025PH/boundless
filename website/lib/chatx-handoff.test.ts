import assert from "node:assert/strict";
import { chatxHandoffUrl, chatxMiniAppOutLink, telegramShareHref, handoffShareText } from "./chatx-handoff";

const zh = new URL(chatxHandoffUrl("zh", "ad_biz_a01"));
assert.equal(zh.pathname, "/download/chatx");
assert.equal(zh.searchParams.get("src"), "ad_biz_a01");

const en = new URL(chatxHandoffUrl("en", ""));
assert.equal(en.pathname, "/en/download/chatx");
assert.equal(en.searchParams.has("src"), false);

const share = new URL(telegramShareHref(chatxHandoffUrl("zh", "x1"), handoffShareText("zh")));
assert.equal(share.origin + share.pathname, "https://t.me/share/url");
assert.ok(share.searchParams.get("url")!.includes("src=x1"));
assert.ok(share.searchParams.get("text")!.includes("ChatX"));

const dl = new URL(chatxMiniAppOutLink("download", "zh", "ad_x9"));
assert.equal(dl.pathname, "/download/chatx");
assert.equal(dl.searchParams.get("src"), "ad_x9");
assert.equal(dl.searchParams.get("utm_source"), "telegram");
assert.equal(dl.searchParams.get("utm_medium"), "chatx_miniapp");
assert.equal(dl.searchParams.get("utm_campaign"), "ad_x9");

assert.equal(dl.searchParams.has("tg"), false, "不传 uid 与从前一致");

// Telegram uid：只进下载链（交接 / 小程序下载），教程链不带
const dlTg = new URL(chatxMiniAppOutLink("download", "zh", "ad_x9", "5151"));
assert.equal(dlTg.searchParams.get("tg"), "5151");
assert.equal(dlTg.searchParams.get("src"), "ad_x9");
assert.equal(new URL(chatxHandoffUrl("en", "", "77")).searchParams.get("tg"), "77");
assert.ok(telegramShareHref(chatxHandoffUrl("zh", "x1", "88"), handoffShareText("zh")).includes(encodeURIComponent("tg=88")), "发到电脑的分享链也带 uid");

const tut = new URL(chatxMiniAppOutLink("tutorials", "en", "", "5151"));
assert.equal(tut.pathname, "/en/chatx/tutorials");
assert.equal(tut.searchParams.has("src"), false);
assert.equal(tut.searchParams.has("tg"), false);
assert.equal(tut.searchParams.get("utm_campaign"), "organic");

console.log("chatx-handoff smoke OK");
