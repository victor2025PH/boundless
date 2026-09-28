/** 群消息意图分类：npx tsx lib/chatx-group-intent.test.ts */
import assert from "assert";
import { classifyGroupText as c, isSkipReply } from "./chatx-group-intent";

const kind = (t: string, ctx = {}) => c(t, ctx).kind;

for (const t of ["哈哈哈", "好的", "收到！", "谢谢大家", "早上好", "大家好", "👍👍", "666", "ok", "晚安~", "在吗", "hello", "嗯嗯", "吃了吗"]) {
  assert.strictEqual(kind(t), "skip", t);
}
assert.strictEqual(kind("/start"), "skip");
assert.strictEqual(kind("智聊怎么下载", { forwarded: true }), "skip");
assert.strictEqual(kind("今天天气真不错，出去玩了"), "skip");
assert.strictEqual(kind("我也觉得挺好用"), "skip");

for (const t of ["智聊怎么下载？", "ChatX 支持 Mac 吗", "会员多少钱", "电脑版安装完打不开", "自动回复怎么设置", "翻译功能能不能用在 WhatsApp", "激活失败了", "how to install chatx on windows?", "语音克隆要怎么用"]) {
  assert.strictEqual(kind(t), "answer", t);
}
assert.strictEqual(c("软件一打开就闪退").reason, "problem");

assert.strictEqual(kind("明天几点开会？"), "maybe");
assert.strictEqual(kind("有人知道这个怎么弄吗"), "maybe");
assert.strictEqual(kind("你们怎么看这个"), "maybe");

assert.strictEqual(kind("你说得对", { replyToHuman: true }), "skip");
assert.strictEqual(kind("你怎么做到的？", { replyToHuman: true }), "skip");
assert.strictEqual(kind("你那个智聊怎么激活的？", { replyToHuman: true }), "maybe");

assert.ok(isSkipReply("SKIP"));
assert.ok(isSkipReply(" skip。"));
assert.ok(isSkipReply(null));
assert.ok(!isSkipReply("点官网下载就行。"));
assert.ok(!isSkipReply("Skipping ads is supported"));

console.log("# pass 1 # fail 0");
