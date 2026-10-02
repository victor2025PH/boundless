/** npx tsx lib/chat-links.test.ts */
import assert from "assert";
import { splitChatLinks } from "./chat-links";

const a = splitChatLinks("可以的，装好就能用。下载安装：https://bd2026.cc/download/chatx?src=ad_01&utm_medium=chatx_bot。教程看这里：https://bd2026.cc/zh/chatx/tutorials?src=ad_01");
assert.deepStrictEqual(a.urls, ["https://bd2026.cc/download/chatx?src=ad_01&utm_medium=chatx_bot", "https://bd2026.cc/zh/chatx/tutorials?src=ad_01"]);
assert.strictEqual(a.text, "可以的，装好就能用。", "短引导标签连同链接一起去掉");

const b = splitChatLinks("Sure. Download here: https://bd2026.cc/download/chatx?src=x.\nSame link again https://bd2026.cc/download/chatx?src=x");
assert.deepStrictEqual(b.urls, ["https://bd2026.cc/download/chatx?src=x"], "结尾标点剔除 + 去重");
assert.strictEqual(b.text, "Sure.\nSame link again", "短标签去掉；>14 字的句子保留文字只去链接");

const c = splitChatLinks("ChatX 是 Windows 本地运行的，数据留在你电脑。");
assert.deepStrictEqual(c, { text: "ChatX 是 Windows 本地运行的，数据留在你电脑。", urls: [] });

const d = splitChatLinks("联系人工（https://t.me/WJKJ2026）即可");
assert.deepStrictEqual(d.urls, ["https://t.me/WJKJ2026"], "全角括号不吞进 URL");

const e = splitChatLinks("如果你想马上开始的话可以点击下面这个链接进行下载安装 https://bd2026.cc/download/chatx?src=x 装好就能用。");
assert.deepStrictEqual(e.urls, ["https://bd2026.cc/download/chatx?src=x"]);
assert.ok(e.text.startsWith("如果你想马上开始的话") && e.text.endsWith("装好就能用。") && !e.text.includes("http"), `长句只去链接: ${e.text}`);

const f = splitChatLinks("下载安装：https://bd2026.cc/download/chatx?src=x\n视频教程：https://bd2026.cc/chatx/tutorials?src=x");
assert.strictEqual(f.text, "", "纯链接列表 → 正文为空，只剩按钮");
assert.strictEqual(f.urls.length, 2);

console.log("chat-links smoke OK");
