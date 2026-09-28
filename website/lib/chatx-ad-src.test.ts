/** 广告来源码：npx tsx lib/chatx-ad-src.test.ts */
import assert from "assert";
import { adStartLink, buildAdSrc, parseAdSrc } from "./chatx-ad-src";
import { parseStartSrc } from "./chatx-bot";

const src = buildAdSrc("biz", "voice", 1);
assert.strictEqual(src, "ad_biz_voice01");
assert.deepStrictEqual(parseAdSrc(src), { cat: "biz", creative: "voice", n: 1 });
assert.strictEqual(adStartLink(src, "ctx2026_bot"), "https://t.me/ctx2026_bot?start=ad_biz_voice01");
assert.strictEqual(parseStartSrc(`/start ${src}`), src, "bot 能原样解析");
assert.strictEqual(parseStartSrc(`/start@ctx2026_bot ${buildAdSrc("crypto", "clone", 99)}`), "ad_crypto_clone99");
for (const bad of ["ad_biz_voice00", "ad_xxx_voice01", "ad_biz_xxx01", "ad_biz_voice1", "organic", "ad_biz_voice01x"]) {
  assert.strictEqual(parseAdSrc(bad), null, bad);
}
assert.throws(() => buildAdSrc("biz", "voice", 0));
assert.throws(() => buildAdSrc("biz", "voice", 100));
assert.throws(() => adStartLink("organic"));
console.log("chatx-ad-src OK");

// 报表按类目 / 素材汇总：非 ad_ 码不计
import("./chatx-report").then(({ groupAdRows }) => {
  const row = (src: string, users: number, dlUsers: number) => ({ src, users, landUsers: users, dlUsers });
  const g = groupAdRows([row("ad_biz_voice01", 5, 1), row("ad_biz_trans01", 3, 2), row("ad_ai_voice01", 10, 0), row("organic", 50, 9)]);
  assert.deepStrictEqual(g.byCat.map((x) => [x.key, x.srcs, x.users, x.dlUsers]), [["ai", 1, 10, 0], ["biz", 2, 8, 3]]);
  assert.deepStrictEqual(g.byCreative.map((x) => [x.key, x.users]), [["voice", 15], ["trans", 3]]);
  console.log("chatx-ad-src groups OK");
});
