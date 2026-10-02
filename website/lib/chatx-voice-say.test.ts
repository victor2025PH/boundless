/**
 * 语音能力话术：asksForVoice / scrubVoiceDenial / voiceReplyHint。
 * 运行：npx tsx lib/chatx-voice-say.test.ts
 */
import assert from "assert";
import { asksForVoice, scrubVoiceDenial, voiceReplyHint } from "./chatx-voice";

for (const t of ["发条语音给我", "你能发语音吗", "用声音说一遍", "语音回我", "send me a voice note", "Can you send a voice message?"]) assert.ok(asksForVoice(t), t);
for (const t of ["你好", "怎么下载", "ChatX 支持哪些平台", "hello there"]) assert.ok(!asksForVoice(t), t);

assert.strictEqual(
  scrubVoiceDenial("语音这块我这边暂时发不了，不过 ChatX 本身支持人设语音聊天，客户听着更真实。想体验先下载。"),
  "ChatX 本身支持人设语音聊天，客户听着更真实。想体验先下载。"
);
assert.strictEqual(scrubVoiceDenial("抱歉，我只能文字回复，不能发语音。ChatX 可以自动回客户。"), "抱歉，ChatX 可以自动回客户。");
assert.strictEqual(scrubVoiceDenial("Sorry, I can't send voice messages here. But ChatX replies for you."), "Sorry, ChatX replies for you.");
const keep = "ChatX 能把文字回复转成你的声音发语音消息，下载就能试。";
assert.strictEqual(scrubVoiceDenial(keep), keep, "正常介绍语音能力不动");
assert.strictEqual(scrubVoiceDenial("我发不了语音。"), "我发不了语音。", "全删光则保留原文");

assert.match(voiceReplyHint("zh", true), /你能发语音/);
assert.match(voiceReplyHint("zh", false), /\/voice/);
assert.match(voiceReplyHint("en", true), /CAN send voice/);
console.log("chatx-voice-say OK");
