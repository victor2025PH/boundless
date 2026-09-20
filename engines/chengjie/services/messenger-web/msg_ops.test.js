/**
 * Messenger DOM 操作纯函数门禁（node --test）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  synthMsgId, parseReactionFromAria, normalizeReactionEmoji,
  isUnsentPreview, isUnsentTombstone, classifyInboxHint, resolveInboxHint, pinHealBump,
  adaptiveReqEvery, canOpenThread, normalizePin, autoPinGate, warmBackfillBatch,
  sendFastPathEligible, composerTextMatches,
  isOnThreadUrl,
  parseMsgAria, MSG_ARIA_RE, threadReadSample,
  normalizeRequestAction, REQUEST_ACTION_LABELS,
  matchQuotedTarget, msgrPaletteTarget, reactAriaCandidates,
  pickUnreadForced, classifyRequestsScan, sentRingPush, sentRingHit, echoTextHit,
  classifyComposerBlock, pickManualOutMirror,
  inferGroupFromInboundSenders, updateSenderRoster,
  sendFailureBackoffMs, manualProbeDecision,
} from "./msg_ops.js";

test("manualProbeDecision：M-2 C #233 退避窗内人工探测每窗一次；自动/平台封锁不放行", () => {
  const now = 1_000_000;
  // streak=2 → 窗长 10s；到期 now+7s → 窗起点 now-3s
  const base = { gateReason: "send_backoff", backoffUntil: now + 7000, streak: 2 };
  // 自动链：不放行
  assert.equal(manualProbeDecision({ ...base, manual: false, lastProbeAt: 0 }).allow, false);
  // 人工首探：放行；窗起点算对
  const d1 = manualProbeDecision({ ...base, manual: true, lastProbeAt: 0 });
  assert.equal(d1.allow, true);
  assert.equal(d1.windowStart, now - 3000);
  // 本窗已探过（lastProbeAt 晚于窗起点）→ 不再放行
  assert.equal(manualProbeDecision({ ...base, manual: true, lastProbeAt: now - 1000 }).allow, false);
  // 新窗（失败续退避：streak=3 → 20s，到期 now+20s → 起点 now）→ 上次探测早于起点 → 再放行一次
  assert.equal(manualProbeDecision({ manual: true, gateReason: "send_backoff",
    backoffUntil: now + 20000, streak: 3, lastProbeAt: now - 1000 }).allow, true);
  // 平台临时封锁：人也不能撞
  assert.equal(manualProbeDecision({ ...base, manual: true, gateReason: "account_blocked" }).allow, false);
  // 脏输入不抛
  assert.equal(manualProbeDecision({}).allow, false);
});

test("B80 normalizeTsLabelForId：跨日时间表述漂移归一到稳定时钟（防镜像重复）", async () => {
  const { normalizeTsLabelForId, synthMsgId } = await import("./msg_ops.js");
  // 同一条消息在不同日子的 aria 时间表述 → 归一后都是 "14:44"
  assert.equal(normalizeTsLabelForId("14:44"), "14:44");
  assert.equal(normalizeTsLabelForId("昨天 14:44"), "14:44");
  assert.equal(normalizeTsLabelForId("Yesterday 14:44"), "14:44");
  assert.equal(normalizeTsLabelForId("July 24, 2026, 14:44"), "14:44");
  assert.equal(normalizeTsLabelForId("2026年7月24日 14:44:05"), "14:44:05");
  // 12h 制保留 AM/PM
  assert.equal(normalizeTsLabelForId("July 24, 2026, 12:35 PM"), "12:35 PM");
  // 纯相对词/无时钟 → 空串（回落正文去重，不进指纹）
  assert.equal(normalizeTsLabelForId("刚刚"), "");
  assert.equal(normalizeTsLabelForId("2 days ago"), "");
  assert.equal(normalizeTsLabelForId(""), "");
  // 端到端：同消息跨日重拉必须得到同一 msg_id（B80 核心不变量）
  const a = synthMsgId({ chatKey: "c1", direction: "in", tsLabel: "14:44", text: "hi" });
  const b = synthMsgId({ chatKey: "c1", direction: "in", tsLabel: "昨天 14:44", text: "hi" });
  const d = synthMsgId({ chatKey: "c1", direction: "in", tsLabel: "July 24, 2026, 14:44", text: "hi" });
  assert.equal(a, b);
  assert.equal(a, d);
  // 不同时钟仍是不同消息（别把两条真消息去重掉）
  const e = synthMsgId({ chatKey: "c1", direction: "in", tsLabel: "14:45", text: "hi" });
  assert.notEqual(a, e);
});

test("sendFailureBackoffMs：B99 连败指数退避（5s 起步 / 翻倍 / 5min 封顶 / 0 无退避）", () => {
  assert.equal(sendFailureBackoffMs(0), 0);
  assert.equal(sendFailureBackoffMs(-3), 0);
  assert.equal(sendFailureBackoffMs(1), 5000);
  assert.equal(sendFailureBackoffMs(2), 10000);
  assert.equal(sendFailureBackoffMs(3), 20000);
  assert.equal(sendFailureBackoffMs(6), 160000);
  assert.equal(sendFailureBackoffMs(7), 300000);   // 封顶
  assert.equal(sendFailureBackoffMs(99), 300000);  // 大 streak 不溢出
  assert.equal(sendFailureBackoffMs("bad"), 0);    // 脏输入按 0
  assert.equal(sendFailureBackoffMs(2, { baseMs: 1000, capMs: 3000 }), 2000);
  assert.equal(sendFailureBackoffMs(9, { baseMs: 1000, capMs: 3000 }), 3000);
});

test("msgrPaletteTarget：出站表情面板映射（VS16 归一/😂→😆/面板外拒绝/脏输入）", () => {
  assert.equal(msgrPaletteTarget("👍"), "👍");
  assert.equal(msgrPaletteTarget("❤️"), "❤️");   // VS16 变体 → 剥后映射
  assert.equal(msgrPaletteTarget("❤"), "❤️");
  assert.equal(msgrPaletteTarget("😂"), "😆");    // UI 的 laugh 归面板的 laugh
  assert.equal(msgrPaletteTarget("😆"), "😆");
  assert.equal(msgrPaletteTarget("😮"), "😮");
  assert.equal(msgrPaletteTarget("😢"), "😢");
  assert.equal(msgrPaletteTarget("😠"), "😠");
  // 面板外 → 诚实拒绝（unsupported_emoji），绝不乱点
  assert.equal(msgrPaletteTarget("🙏"), "");
  assert.equal(msgrPaletteTarget("🎉"), "");
  assert.equal(msgrPaletteTarget(""), "");
  assert.equal(msgrPaletteTarget(null), "");
});

test("reactAriaCandidates：面板字符 → aria 名称候选（中英兜底/未知空表）", () => {
  assert.ok(reactAriaCandidates("👍").includes("like"));
  assert.ok(reactAriaCandidates("👍").includes("赞"));
  assert.ok(reactAriaCandidates("😆").includes("laugh"));
  assert.ok(reactAriaCandidates("❤️").includes("love"));
  assert.deepEqual(reactAriaCandidates("🙏"), []);
  assert.deepEqual(reactAriaCandidates(""), []);
});

test("matchQuotedTarget：引用回复目标定位（精确/前缀/包含/歧义弃权/短串防误命中）", () => {
  const rows = ["你好呀", "在吗？今天有空吗", "我想问下价格", "哈哈"];
  // 精确命中
  assert.equal(matchQuotedTarget(rows, "我想问下价格"), 2);
  // 空白/大小写归一后精确
  assert.equal(matchQuotedTarget(["Hello World"], "  hello   world "), 0);
  // 唯一前缀（预览截断：引用文是气泡文的前缀）
  assert.equal(matchQuotedTarget(["在吗？今天有空吗一起吃饭"], "在吗？今天有空吗"), 0);
  // 唯一包含（长引用文 ≥6）
  assert.equal(matchQuotedTarget(["【系统】我想问下价格可以吗"], "我想问下价格"), 0);
  // 找不到 → -1
  assert.equal(matchQuotedTarget(rows, "完全没有的内容"), -1);
  // 歧义：两条完全相同 → -1（宁缺勿滥，绝不挂错气泡）
  assert.equal(matchQuotedTarget(["好的", "好的"], "好的"), -1);
  // 短串（<6）不走「包含」级，避免「好」命中「你好呀」→ -1
  assert.equal(matchQuotedTarget(["你好呀", "在吗"], "好"), -1);
  // 脏输入不抛
  assert.equal(matchQuotedTarget(null, "x"), -1);
  assert.equal(matchQuotedTarget(["a"], ""), -1);
  assert.equal(matchQuotedTarget(["a"], null), -1);
});

test("warmBackfillBatch：预热提速批量夹取（默认/满队/短队/封顶/边界/脏输入）", () => {
  // 满队 + 默认 batch=3 → 3
  assert.equal(warmBackfillBatch(20, 3), 3);
  // 队列短于 batch → 取队列长
  assert.equal(warmBackfillBatch(2, 3), 2);
  // batch=1 → 逐条旧行为
  assert.equal(warmBackfillBatch(20, 1), 1);
  // 误配过大 → 封顶 cap(默认 5)
  assert.equal(warmBackfillBatch(20, 100), 5);
  // 空队列 → 0（不读）
  assert.equal(warmBackfillBatch(0, 3), 0);
  // batch<1 → 至少 1
  assert.equal(warmBackfillBatch(20, 0), 1);
  assert.equal(warmBackfillBatch(20, -5), 1);
  // 自定义 cap
  assert.equal(warmBackfillBatch(20, 100, 2), 2);
  // 脏输入不抛、归零/归一
  assert.equal(warmBackfillBatch(null, null), 0);      // queue 0 → 0
  assert.equal(warmBackfillBatch(NaN, NaN), 0);
  assert.equal(warmBackfillBatch("5", "2"), 2);        // 数字字符串可解析
});

test("isOnThreadUrl：发送快路径 URL 判定（普通/e2ee/尾斜杠/查询串/前缀防误命中/空参）", () => {
  assert.equal(isOnThreadUrl("https://www.messenger.com/t/123456/", "123456"), true);
  assert.equal(isOnThreadUrl("https://www.messenger.com/e2ee/t/123456/", "123456"), true);
  assert.equal(isOnThreadUrl("https://www.messenger.com/t/123456", "123456"), true);
  assert.equal(isOnThreadUrl("https://www.messenger.com/t/123456?foo=1", "123456"), true);
  // 前缀线程 id 不得误命中（/t/1234567 ≠ /t/123456）
  assert.equal(isOnThreadUrl("https://www.messenger.com/t/1234567/", "123456"), false);
  // 别的线程 / 收件箱根 / 空参 → false（走完整导航路径）
  assert.equal(isOnThreadUrl("https://www.messenger.com/t/999/", "123456"), false);
  assert.equal(isOnThreadUrl("https://www.messenger.com/", "123456"), false);
  assert.equal(isOnThreadUrl("", "123456"), false);
  assert.equal(isOnThreadUrl("https://www.messenger.com/t/123/", ""), false);
  // jid 含正则元字符不抛（防御性转义）
  assert.equal(isOnThreadUrl("https://www.messenger.com/t/a.b/", "a.b"), true);
  assert.equal(isOnThreadUrl("https://www.messenger.com/t/axb/", "a.b"), false);
});

test("sendFastPathEligible：同线程快路判定（普通/E2EE/段边界/空值）", () => {
  // 普通与 E2EE 两种线程 URL 形态都命中
  assert.equal(sendFastPathEligible("https://www.messenger.com/t/123/", "123"), true);
  assert.equal(sendFastPathEligible("https://www.messenger.com/e2ee/t/123/", "123"), true);
  // 无尾斜杠 / 带查询串
  assert.equal(sendFastPathEligible("https://www.messenger.com/t/123", "123"), true);
  assert.equal(sendFastPathEligible("https://www.messenger.com/t/123?x=1", "123"), true);
  // 段边界：/t/123 不得误配 /t/1234（前缀撞车是真实风险——线程 key 同前缀常见）
  assert.equal(sendFastPathEligible("https://www.messenger.com/t/1234/", "123"), false);
  // 不在线程页 / 空值 → 走完整导航
  assert.equal(sendFastPathEligible("https://www.messenger.com/", "123"), false);
  assert.equal(sendFastPathEligible("", "123"), false);
  assert.equal(sendFastPathEligible("https://www.messenger.com/t/123/", ""), false);
});

test("composerTextMatches：insertText 完整性闸门（空白归一/截断必拒/空值拒）", () => {
  assert.equal(composerTextMatches("hello world", "hello world"), true);
  // composer 渲染可能引入换行/多空格 → 归一后仍算一致
  assert.equal(composerTextMatches("hello\nworld ", "hello world"), true);
  // 截断（insertText 没进编辑器状态只剩打头段）必须拒 → 调用方回退逐字输入
  assert.equal(composerTextMatches("hello", "hello world"), false);
  // 内容不同 / 空值一律拒
  assert.equal(composerTextMatches("hella world", "hello world"), false);
  assert.equal(composerTextMatches("", "hello"), false);
  assert.equal(composerTextMatches("hello", ""), false);
});

test("normalizeRequestAction：意图归一 + 别名 + 非法空串", () => {
  assert.equal(normalizeRequestAction("accept"), "accept");
  assert.equal(normalizeRequestAction("Approve"), "accept");
  assert.equal(normalizeRequestAction("decline"), "decline");
  assert.equal(normalizeRequestAction("delete"), "decline");
  assert.equal(normalizeRequestAction("reject"), "decline");
  assert.equal(normalizeRequestAction("  ACCEPT  "), "accept");
  assert.equal(normalizeRequestAction("report"), ""); // 举报不在词表（交官方页）
  assert.equal(normalizeRequestAction(""), "");
  assert.equal(normalizeRequestAction(null), "");
});

test("REQUEST_ACTION_LABELS：中英齐备、不含举报", () => {
  assert.ok(REQUEST_ACTION_LABELS.accept.includes("接受"));
  assert.ok(REQUEST_ACTION_LABELS.accept.includes("Accept"));
  assert.ok(REQUEST_ACTION_LABELS.decline.includes("删除"));
  assert.ok(REQUEST_ACTION_LABELS.decline.includes("Delete"));
  const all = [...REQUEST_ACTION_LABELS.accept, ...REQUEST_ACTION_LABELS.decline].join("|");
  assert.ok(!/检举|举报|report|spam/i.test(all));
});

test("parseMsgAria：三代词序（zh旧 / en旧 / en新2026-08 时间前置）", () => {
  // zh 旧词序（含空发送者=对端）
  assert.deepEqual(parseMsgAria("Enter，消息由你发送于14:44：晚点聊"),
    { sender: "你", direction: "out", ts: "14:44", text: "晚点聊" });
  assert.deepEqual(parseMsgAria("消息由发送于14:44：好的"),
    { sender: "", direction: "in", ts: "14:44", text: "好的" });
  // en 旧词序
  assert.deepEqual(parseMsgAria("Message sent by John at 3:00 PM: hi"),
    { sender: "John", direction: "in", ts: "3:00 PM", text: "hi" });
  assert.deepEqual(parseMsgAria("Message sent by You at 3:00 PM"),
    { sender: "You", direction: "out", ts: "3:00 PM", text: "" });
  // en 新词序（2026-08-11 生产 aria 实录：时间挪到 sender 前，正文可含冒号/emoji）
  assert.deepEqual(
    parseMsgAria("Enter, Message sent July 24, 2026, 12:37 PM by John Pol: Beb dun k magchat s isa ko"),
    { sender: "John Pol", direction: "in", ts: "July 24, 2026, 12:37 PM", text: "Beb dun k magchat s isa ko" });
  assert.deepEqual(parseMsgAria("Message sent July 24, 2026, 12:35 PM by John Pol: 👍"),
    { sender: "John Pol", direction: "in", ts: "July 24, 2026, 12:35 PM", text: "👍" });
  assert.deepEqual(parseMsgAria("Message sent August 3, 2026, 2:26 PM by John Pol"),
    { sender: "John Pol", direction: "in", ts: "August 3, 2026, 2:26 PM", text: "" });
  assert.deepEqual(parseMsgAria("Message sent 10:42 AM by you: ok, see you"),
    { sender: "you", direction: "out", ts: "10:42 AM", text: "ok, see you" });
  // 正文里出现 " by " 不得把时间切错（时间段不含 " by "，懒匹配停在第一个 by）
  assert.deepEqual(parseMsgAria("Message sent 10:42 AM by Ann: sent by mistake"),
    { sender: "Ann", direction: "in", ts: "10:42 AM", text: "sent by mistake" });
  // 悬停时间戳变体与消息元素成对出现，绝不能当消息（否则整线程双份）
  assert.equal(parseMsgAria("At July 24, 2026, 12:35 PM, John Pol: 👍"), null);
  assert.equal(parseMsgAria(""), null);
});

test("threadReadSample：ok/fail/skip 三态语义", () => {
  // 读到真实消息 = 健康正面证据
  assert.equal(threadReadSample([{ text: "hi" }], { previewPlaceholder: false }), "ok");
  assert.equal(threadReadSample([{ text: "hi" }], { previewPlaceholder: true }), "ok");
  // null = 导航/渲染失败，无论预览是什么都算故障
  assert.equal(threadReadSample(null, { previewPlaceholder: true }), "fail");
  assert.equal(threadReadSample(null, { previewPlaceholder: false }), "fail");
  // 空读 × 占位预览 = 锁死旧线程/真空会话，不入窗（钉死全败窗口的根源）
  assert.equal(threadReadSample([], { previewPlaceholder: true }), "skip");
  // 空读 × 真实正文预览 = 预览解密得出、正文却读不出 → 管线故障
  assert.equal(threadReadSample([], { previewPlaceholder: false }), "fail");
  // 缺省宽松（回填队列无 preview 可判）
  assert.equal(threadReadSample([]), "skip");
});

test("MSG_ARIA_RE：认两代消息锚点、不认悬停时间戳", () => {
  assert.equal(MSG_ARIA_RE.test("Enter, Message sent July 24, 2026, 12:35 PM by John Pol: 👍"), true);
  assert.equal(MSG_ARIA_RE.test("Message sent by John at 3 PM: hi"), true);
  assert.equal(MSG_ARIA_RE.test("Enter，消息由你发送于14:44：晚点聊"), true);
  assert.equal(MSG_ARIA_RE.test("At July 24, 2026, 12:35 PM, John Pol: 👍"), false);
  assert.equal(MSG_ARIA_RE.test("Messages and calls are secured with end-to-end encryption"), false);
});

test("synthMsgId：同内容跨调用恒定，改字段即变", () => {
  const a = synthMsgId({ chatKey: "1", direction: "in", tsLabel: "14:44", text: "hi", mediaRef: "" });
  const b = synthMsgId({ chatKey: "1", direction: "in", tsLabel: "14:44", text: "hi", mediaRef: "" });
  assert.equal(a, b);
  assert.match(a, /^m_[0-9a-f]{16}$/);
  const c = synthMsgId({ chatKey: "1", direction: "in", tsLabel: "14:44", text: "hi!", mediaRef: "" });
  assert.notEqual(a, c);
});

test("parseReactionFromAria：中英本方/对端", () => {
  assert.deepEqual(parseReactionFromAria("你用👍回应了"), { emoji: "👍", sender: "me" });
  assert.deepEqual(parseReactionFromAria("你用大笑回应了"), { emoji: "😆", sender: "me" });
  assert.deepEqual(parseReactionFromAria("Alice用赞回应了"), { emoji: "👍", sender: "peer" });
  assert.deepEqual(parseReactionFromAria("You reacted with ❤️"), { emoji: "❤️", sender: "me" });
  assert.deepEqual(parseReactionFromAria("Bob reacted with a laugh"), { emoji: "😆", sender: "peer" });
  assert.deepEqual(parseReactionFromAria("You reacted with a like"), { emoji: "👍", sender: "me" });
  assert.equal(parseReactionFromAria("消息由你发送于14:44：hi"), null);
  assert.equal(parseReactionFromAria(""), null);
});

test("normalizeReactionEmoji：词表 + emoji 抽取", () => {
  assert.equal(normalizeReactionEmoji("赞"), "👍");
  assert.equal(normalizeReactionEmoji("laugh"), "😆");
  assert.equal(normalizeReactionEmoji("a love"), "❤️");
  assert.equal(normalizeReactionEmoji("👍👍"), "👍");
  assert.equal(normalizeReactionEmoji(""), "");
});

test("unsent 预览/墓碑", () => {
  assert.equal(isUnsentPreview("你撤回了一条消息"), true);
  assert.equal(isUnsentPreview("You unsent a message"), true);
  assert.equal(isUnsentPreview("你好呀"), false);
  assert.equal(isUnsentTombstone("此消息已撤回"), true);
  assert.equal(isUnsentTombstone("This message was unsent"), true);
  assert.equal(isUnsentTombstone("hello"), false);
});

test("classifyInboxHint：与 Python 半死态两支对齐", () => {
  assert.equal(classifyInboxHint({ unread: 0 }), "");
  assert.equal(classifyInboxHint({ unread: 2, readAttempts: 3, readFails: 3 }), "e2ee_relogin");
  assert.equal(classifyInboxHint({
    unread: 1, readAttempts: 0, e2eeRatio: 0.8, convCount: 10,
  }), "e2ee_relogin");
  assert.equal(classifyInboxHint({
    unread: 1, readAttempts: 0, e2eeRatio: 0.2, convCount: 10,
  }), "");
  assert.equal(classifyInboxHint({
    unread: 1, readAttempts: 1, readFails: 1, // 样本不足
  }), "");
});

test("classifyInboxHint 支三：稳态 unread=0 读取全败 + 大面积占位（198 实测形态）", () => {
  // 198 实测：读取 4/4 全败 + 74% 占位，但失败读取已把未读消费成 0 → 前两支都不命中
  assert.equal(classifyInboxHint({
    unread: 0, readAttempts: 4, readFails: 4, e2eeRatio: 0.74, convCount: 19,
  }), "e2ee_relogin");
  // 但读取全败若占位比例低（真是空会话/对端撤回）→ 不误报
  assert.equal(classifyInboxHint({
    unread: 0, readAttempts: 4, readFails: 4, e2eeRatio: 0.1, convCount: 19,
  }), "");
  // 占位高但读取有成功（解密其实可用）→ 不报
  assert.equal(classifyInboxHint({
    unread: 0, readAttempts: 4, readFails: 1, e2eeRatio: 0.8, convCount: 19,
  }), "");
});

test("resolveInboxHint：PIN 缺失/未通过独立升格，不被泛化 hint 闸住（173 盲区回归钉）", () => {
  // 173 实测形态：占位比 0.39 → classifyInboxHint 返回 ""，但 PIN 浮层确认在场。
  // 旧实现 `hint && pinState` 在这里静默吞掉精确码 → 坐席全程零 PIN 提示。
  assert.equal(resolveInboxHint({ baseHint: "", pinState: "missing" }), "e2ee_pin_required");
  assert.equal(resolveInboxHint({ baseHint: "", pinState: "failed" }), "e2ee_pin_required");
  // 泛化 hint 与 PIN 态同在 → PIN 码优先（更精确的动作指引）
  assert.equal(resolveInboxHint({ baseHint: "e2ee_relogin", pinState: "missing" }), "e2ee_pin_required");
  // 无 PIN 信号 → 泛化 hint 原样透传（含空串）
  assert.equal(resolveInboxHint({ baseHint: "e2ee_relogin", pinState: "" }), "e2ee_relogin");
  assert.equal(resolveInboxHint({ baseHint: "e2ee_relogin", pinState: "ok" }), "e2ee_relogin");
  assert.equal(resolveInboxHint({ baseHint: "", pinState: "ok" }), "");
  assert.equal(resolveInboxHint({}), "");
});

test("pinHealBump：自愈计数推进不改入参、脏值零值重建、未知 outcome 不误计", () => {
  const s0 = pinHealBump(null, "attempt", 1000);
  assert.deepEqual(s0, { attempts: 1, ok: 0, fail: 0, last_ts: 1000, last_ok_ts: 0 });
  const s1 = pinHealBump(s0, "ok", 2000);
  assert.deepEqual(s1, { attempts: 1, ok: 1, fail: 0, last_ts: 1000, last_ok_ts: 2000 });
  // 入参不被原地修改（entry._pinHeal 由调用方整体替换）
  assert.equal(s0.ok, 0);
  const s2 = pinHealBump(s1, "fail", 3000);
  assert.equal(s2.fail, 1);
  assert.equal(s2.last_ok_ts, 2000);
  // 脏入参（负数/字符串）按零值重建，绝不出 NaN
  const s3 = pinHealBump({ attempts: "x", ok: -3, last_ts: NaN }, "attempt", 500);
  assert.deepEqual(s3, { attempts: 1, ok: 0, fail: 0, last_ts: 500, last_ok_ts: 0 });
  // 未知 outcome → 只归一化不计数
  const s4 = pinHealBump(s1, "whatever", 9000);
  assert.deepEqual(s4, s1);
});

test("normalizePin：只认 4-12 位纯数字，脏值归空", () => {
  assert.equal(normalizePin("123456"), "123456");
  assert.equal(normalizePin(" 12 34 "), "1234");
  assert.equal(normalizePin("abc123456"), "123456");
  assert.equal(normalizePin("123"), "");          // 太短
  assert.equal(normalizePin("1234567890123"), ""); // 太长
  assert.equal(normalizePin(""), "");
  assert.equal(normalizePin(null), "");
});

test("autoPinGate：无 PIN 不动手（误触键盘的安全底线）", () => {
  const g = autoPinGate({ pin: "", tries: 0, now: 1000 });
  assert.equal(g.ok, false);
  assert.equal(g.reason, "no_pin");
});

test("autoPinGate：预算内放行、间隔太近拦截", () => {
  assert.equal(autoPinGate({ pin: "123456", tries: 0, now: 100000 }).ok, true);
  // 距上次尝试 <minGapMs(5s) → 拦（同一浮层别连打）
  const g = autoPinGate({ pin: "123456", tries: 1, lastTryTs: 100000, now: 102000 });
  assert.equal(g.ok, false);
  assert.equal(g.reason, "too_soon");
});

test("autoPinGate：烧完预算进冷却，冷却后重开一轮", () => {
  const last = 100000;
  // 2 次已用尽，距上次 5min < cooldown(10min) → 冷却拦截
  const cold = autoPinGate({
    pin: "123456", tries: 2, lastTryTs: last, now: last + 5 * 60 * 1000,
  });
  assert.equal(cold.ok, false);
  assert.equal(cold.reason, "cooldown");
  // 距上次 >10min → 重开预算（reset 让调用方清零 tries）
  const warm = autoPinGate({
    pin: "123456", tries: 2, lastTryTs: last, now: last + 11 * 60 * 1000,
  });
  assert.equal(warm.ok, true);
  assert.equal(warm.reset, true);
});

test("adaptiveReqEvery：空转拉长、封顶 4×、封禁保持底数", () => {
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 0 }), 15);
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 2 }), 30);
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 6 }), 60);
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 99 }), 60);
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 10, blocked: true }), 15);
});

test("canOpenThread：回填跳未读、探针禁用、入站放行", () => {
  assert.equal(canOpenThread({ purpose: "backfill", unread: true }).ok, false);
  assert.equal(canOpenThread({ purpose: "backfill", unread: false }).ok, true);
  assert.equal(canOpenThread({ purpose: "probe" }).ok, false);
  assert.equal(canOpenThread({ purpose: "inbound" }).ok, true);
  assert.equal(canOpenThread({ purpose: "history_pull" }).ok, true);
  assert.equal(canOpenThread({ purpose: "request_read", isRequest: true }).ok, true);
});

// ── pickUnreadForced（未读驱动强制读取，2026-08-15 E2EE 占位盲区解药）────────────
const PH_RE = /端到端加密|end-to-end encrypt|无法显示消息|can't display/i;
const REL = (s) => {
  // 测试用固定时钟：now=1000000000000ms；"5m"→5 分钟前（epoch 秒），认不出→0
  const m = String(s || "").match(/^(\d+)m$/);
  return m ? Math.floor(1000000000000 / 1000) - parseInt(m[1], 10) * 60 : 0;
};
const NOW = 1000000000000;

test("pickUnreadForced：行级未读标记 → 命中（E2EE 占位与逐字重发盲区的主触发源）", () => {
  const convs = [
    { key: "111", preview: "端到端加密", rel: "3m", unread: true },
    { key: "222", preview: "正常正文", rel: "1h", unread: false },
  ];
  const picks = pickUnreadForced(convs, { now: NOW });
  assert.deepEqual(picks, [{ key: "111", why: "row_unread" }]);
});

test("pickUnreadForced：已在常规 sig 候选的线程不重复", () => {
  const convs = [{ key: "111", preview: "x", rel: "", unread: true }];
  const picks = pickUnreadForced(convs, {
    now: NOW, candidateKeys: new Set(["111"]),
  });
  assert.deepEqual(picks, []);
});

test("pickUnreadForced：冷却窗内不重复导航（读不出的 E2EE 线程绝不每 4s 反复开）", () => {
  const convs = [{ key: "111", preview: "x", rel: "", unread: true }];
  const log = new Map([["111", NOW - 60 * 1000]]); // 1min 前刚试过
  assert.deepEqual(pickUnreadForced(convs, { now: NOW, attemptLog: log }), []);
  // 冷却过了（>10min）→ 允许再试
  const log2 = new Map([["111", NOW - 11 * 60 * 1000]]);
  assert.equal(pickUnreadForced(convs, { now: NOW, attemptLog: log2 }).length, 1);
  // 纯函数只读不写 attemptLog（推进由调用方在真正打开时落账）
  assert.equal(log2.get("111"), NOW - 11 * 60 * 1000);
});

test("pickUnreadForced：fresh_placeholder 级需要全局未读旁证 + 占位预览 + 新鲜 rel", () => {
  const convs = [
    { key: "111", preview: "端到端加密", rel: "5m", unread: false },
    { key: "222", preview: "端到端加密", rel: "40m", unread: false }, // 超 30min 窗
    { key: "333", preview: "正常正文", rel: "1m", unread: false },   // 非占位
    { key: "444", preview: "端到端加密", rel: "", unread: false },    // rel 认不出=0
  ];
  const opts = { now: NOW, placeholderRe: PH_RE, relToTs: REL, freshMs: 30 * 60 * 1000 };
  // 全局未读=0 → 弱级证据全部不激活（零开销）
  assert.deepEqual(pickUnreadForced(convs, { ...opts, globalUnread: 0 }), []);
  // 全局未读>0 → 只有「占位+新鲜」那条命中
  assert.deepEqual(pickUnreadForced(convs, { ...opts, globalUnread: 2 }),
    [{ key: "111", why: "fresh_placeholder" }]);
});

test("pickUnreadForced：row_unread 优先于 fresh_placeholder，单轮 cap 封顶", () => {
  const convs = [
    { key: "111", preview: "端到端加密", rel: "2m", unread: false },
    { key: "222", preview: "x", rel: "", unread: true },
    { key: "333", preview: "y", rel: "", unread: true },
  ];
  const picks = pickUnreadForced(convs, {
    now: NOW, globalUnread: 3, placeholderRe: PH_RE, relToTs: REL, cap: 2,
  });
  // 强证据（row_unread）排前并占满 cap；弱证据（111）让位
  assert.deepEqual(picks, [
    { key: "222", why: "row_unread" },
    { key: "333", why: "row_unread" },
  ]);
});

test("pickUnreadForced：脏输入不抛（null convs / 缺字段 / cap=0）", () => {
  assert.deepEqual(pickUnreadForced(null, { now: NOW }), []);
  assert.deepEqual(pickUnreadForced([{}], { now: NOW }), []);
  assert.deepEqual(pickUnreadForced(
    [{ key: "1", unread: true }], { now: NOW, cap: 0 }), []);
});

test("echoTextHit：短文本只认全等（2026-08-16「发『在』吞『在吗』」实锤回归钉）", () => {
  // 事故场景：本方刚发「在」（进 sentLog 回声窗），客户回「在吗」——绝不许判回声。
  // 旧 isSelfEcho 无条件前缀匹配 → 预筛吞掉+推进 seen，客户消息永久不进收件箱。
  assert.equal(echoTextHit("在吗", "在"), false);
  assert.equal(echoTextHit("在", "在吗"), false);
  assert.equal(echoTextHit("在干嘛呢", "在"), false);
  assert.equal(echoTextHit("ok", "okay see you tomorrow"), false);
  // 真回声＝全串相等，短文本照常命中
  assert.equal(echoTextHit("在", "在"), true);
  assert.equal(echoTextHit("好的没问题", "好的没问题"), true);
  // 长文本截断容错保留：双方 ≥16 字且 24 字前缀互为前缀
  const long = "im browsing a stationery store picking out cute pens";
  assert.equal(echoTextHit(long.slice(0, 20), long), true);
  assert.equal(echoTextHit(long, long.slice(0, 30)), true);
  // 双方都长但 24 字内已分叉 → 不判
  assert.equal(echoTextHit(long, "completely different long message body"), false);
  // 一侧 <16 字即使是另一侧前缀也不判（短消息回声不会被截断，前缀只有误伤）
  assert.equal(echoTextHit("好的没问题呀今天见", "好的没问题"), false);
  // 脏输入
  assert.equal(echoTextHit("", "在"), false);
  assert.equal(echoTextHit(null, undefined), false);
});

test("sentRingPush/sentRingHit：镜像自发环（无 TTL 封顶 + 前缀容错 + 短文本只认全等）", () => {
  const ring = new Map();
  sentRingPush(ring, "111", "imbrowsingastationerystorepickingoutsomecutepens");
  sentRingPush(ring, "111", "howhaveyoubeenlately");
  // 全串相等命中
  assert.equal(sentRingHit(ring, "111", "howhaveyoubeenlately"), true);
  // 截断容错：气泡文与登记文 24 字前缀互为前缀（≥16 字）即命中
  assert.equal(sentRingHit(ring, "111", "imbrowsingastationerystore"), true);
  // 别的会话不串
  assert.equal(sentRingHit(ring, "222", "howhaveyoubeenlately"), false);
  // 短文本只认全等（「好的」类不做前缀匹配，防误吞真人工消息）
  sentRingPush(ring, "111", "好的没问题");
  assert.equal(sentRingHit(ring, "111", "好的没问题"), true);
  assert.equal(sentRingHit(ring, "111", "好的没问题呀今天见"), false);
  // perKey 封顶：塞 25 条只留 20，最老的被挤出
  const r2 = new Map();
  for (let i = 0; i < 25; i++) sentRingPush(r2, "k", "text-number-" + i + "-padding");
  assert.equal(r2.get("k").length, 20);
  assert.equal(sentRingHit(r2, "k", "text-number-0-padding"), false);
  assert.equal(sentRingHit(r2, "k", "text-number-24-padding"), true);
  // maxKeys 封顶：第 65 个 key 挤掉最旧 key
  const r3 = new Map();
  for (let i = 0; i < 65; i++) sentRingPush(r3, "key" + i, "some-long-enough-text-value");
  assert.equal(r3.size, 64);
  assert.equal(r3.has("key0"), false);
  assert.equal(r3.has("key64"), true);
  // 脏输入不抛
  sentRingPush(null, "k", "x");
  assert.equal(sentRingHit(null, "k", "x"), false);
  assert.equal(sentRingHit(ring, "", ""), false);
});

test("classifyRequestsScan：rows/blocked/真空/可疑 四态判别", () => {
  // 抓到行 = 页面健康的正面证据（structSeen 无关紧要）
  assert.equal(classifyRequestsScan({ rowCount: 3, structSeen: false }), "rows");
  // 风控封禁走既有退避
  assert.equal(classifyRequestsScan({ blocked: true, rowCount: 0 }), "blocked");
  // 零行 + 页面骨架在场 = 文件夹真的空
  assert.equal(classifyRequestsScan({ rowCount: 0, structSeen: true }), "empty_ok");
  // 零行 + 无结构证据 = 漂移/导航失败，读数不可信
  assert.equal(classifyRequestsScan({ rowCount: 0, structSeen: false }), "suspect");
  // 脏输入不抛
  assert.equal(classifyRequestsScan({}), "suspect");
  assert.equal(classifyRequestsScan(), "suspect");
});

test("classifyComposerBlock：composer 缺席探针 → 稳定 reason_code（硬→软优先级）", () => {
  // 登录态丢失最硬：即使其它信号同时在场也先报它
  assert.equal(classifyComposerBlock({ loginForm: true, pinPrompt: true, hasLog: true }),
    "login_page");
  // E2EE PIN 浮层优先于「接受」栏（浮层在场时接受栏多半是残影）
  assert.equal(classifyComposerBlock({ pinPrompt: true, acceptSeen: true }),
    "e2ee_pin_prompt");
  // 消息请求接受没点成
  assert.equal(classifyComposerBlock({ acceptSeen: true, hasLog: true }), "needs_accept");
  // composer 在 DOM 但不可见（waitForSelector 只认 visible）＝浮层/遮罩
  assert.equal(classifyComposerBlock({ composerCount: 1, hasLog: true }), "composer_hidden");
  // 会话内容渲染了、composer 没渲染 ＝ 173 事故形态（新 E2EE 线程首开慢）
  assert.equal(classifyComposerBlock({ hasLog: true }), "render_timeout");
  // 整页白屏/导航失败
  assert.equal(classifyComposerBlock({ hasLog: false }), "page_not_rendered");
  // 脏输入不抛
  assert.equal(classifyComposerBlock({}), "page_not_rendered");
  assert.equal(classifyComposerBlock(null), "page_not_rendered");
  assert.equal(classifyComposerBlock(), "page_not_rendered");
  // reason_code 用词不得撞 dead_peer 永久词表（防瞬态被当死 peer 封 6h）
  for (const probe of [{ loginForm: true }, { pinPrompt: true }, { acceptSeen: true },
                       { composerCount: 2 }, { hasLog: true }, {}]) {
    const rc = classifyComposerBlock(probe).toUpperCase();
    for (const marker of ["USER_IS_BLOCKED", "DEACTIVATED", "PEER_ID_INVALID",
                          "CHAT_WRITE_FORBIDDEN", "CHANNEL_PRIVATE"]) {
      assert.ok(!rc.includes(marker) && !marker.includes(rc), `${rc} vs ${marker}`);
    }
  }
});

// ── pickManualOutMirror：手发出站镜像取件（2026-08-16 手机手发坐席隐形修复）──────
const _norm = (s) => String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
const _sigOf = (m) => `${_norm(m.text)}|${m.media_ref || ""}|${m.ts}`;
const _normOf = (m) => _norm(m.text || "");
const _out = (text, ts, mediaRef = "") => ({ direction: "out", text, ts, media_ref: mediaRef });

test("pickManualOutMirror：首观察无触发＝旧行为只建水位不上报", () => {
  const outs = [_out("历史消息A", 1), _out("历史消息B", 2)];
  const r = pickManualOutMirror(outs, { mark: undefined, sigOf: _sigOf, normOf: _normOf });
  assert.deepEqual(r.fresh, []);
  assert.equal(r.newMark, _sigOf(outs[1])); // 水位建到最新，restart 重灌防线不动
});

test("pickManualOutMirror：首观察+manualTrigger+预览吻合 → 镜像最新一条", () => {
  const outs = [_out("重启前的旧AI消息", 1), _out("在吗", 2)];
  const r = pickManualOutMirror(outs, {
    mark: undefined, sigOf: _sigOf, normOf: _normOf,
    rowPreviewNorm: _norm("在吗"), manualTrigger: true,
  });
  assert.equal(r.fresh.length, 1);
  assert.equal(r.fresh[0].text, "在吗"); // 只取触发那条，绝不连带重启前历史
  assert.equal(r.newMark, _sigOf(outs[1]));
});

test("pickManualOutMirror：短文本必须全等——预览与尾条不符则宁缺勿错（水位照建）", () => {
  const outs = [_out("旧消息", 1), _out("好的", 2)];
  const r = pickManualOutMirror(outs, {
    mark: undefined, sigOf: _sigOf, normOf: _normOf,
    rowPreviewNorm: _norm("好呀"), manualTrigger: true,
  });
  assert.deepEqual(r.fresh, []); // 不吻合＝DOM 抖动/错位，不冒挑错条的险
  assert.equal(r.newMark, _sigOf(outs[1])); // 水位照建：下次该条经水位差路径回流
});

test("pickManualOutMirror：长文本截断预览经 echoTextHit 前缀容错命中", () => {
  const full = "今天下午我们去海边玩了很久然后又去吃了海鲜大餐真的很开心";
  const outs = [_out(full, 5)];
  const r = pickManualOutMirror(outs, {
    mark: undefined, sigOf: _sigOf, normOf: _normOf,
    rowPreviewNorm: _norm(full.slice(0, 20)), // Messenger 预览截断形态
    manualTrigger: true,
  });
  assert.equal(r.fresh.length, 1);
});

test("pickManualOutMirror：首观察+manualTrigger 纯媒体（无文可核对）凭触发放行", () => {
  const outs = [_out("", 3, "photo_abc.jpg")];
  const r = pickManualOutMirror(outs, {
    mark: undefined, sigOf: _sigOf, normOf: _normOf,
    rowPreviewNorm: _norm("一张照片"), manualTrigger: true,
  });
  assert.equal(r.fresh.length, 1);
  assert.equal(r.fresh[0].media_ref, "photo_abc.jpg");
});

test("pickManualOutMirror：已有水位＝切其后（多条连发全收，burstMax 封顶）", () => {
  const outs = [_out("a", 1), _out("b", 2), _out("c", 3), _out("d", 4)];
  const r = pickManualOutMirror(outs, { mark: _sigOf(outs[0]), sigOf: _sigOf, normOf: _normOf });
  assert.deepEqual(r.fresh.map((m) => m.text), ["b", "c", "d"]);
  const r2 = pickManualOutMirror(outs, {
    mark: _sigOf(outs[0]), sigOf: _sigOf, normOf: _normOf, burstMax: 2,
  });
  assert.deepEqual(r2.fresh.map((m) => m.text), ["c", "d"]); // 封顶取最新
});

test("pickManualOutMirror：水位滑出窗口 → 保守只取最新（旧行为）", () => {
  const outs = [_out("x", 7), _out("y", 8)];
  const r = pickManualOutMirror(outs, { mark: "gone|sig|0", sigOf: _sigOf, normOf: _normOf });
  assert.deepEqual(r.fresh.map((m) => m.text), ["y"]);
});

test("pickManualOutMirror：水位无变化 / 空入参 → 零动作", () => {
  const outs = [_out("same", 9)];
  const r = pickManualOutMirror(outs, { mark: _sigOf(outs[0]), sigOf: _sigOf });
  assert.deepEqual(r.fresh, []);
  assert.equal(r.newMark, _sigOf(outs[0]));
  const r2 = pickManualOutMirror([], { mark: undefined, sigOf: _sigOf });
  assert.deepEqual(r2, { fresh: [], newMark: undefined });
  const r3 = pickManualOutMirror(null, { mark: undefined, sigOf: _sigOf });
  assert.deepEqual(r3, { fresh: [], newMark: undefined });
});

test("inferGroupFromInboundSenders：≥2 互异入站发言人才当群（trim/大小写去重）", () => {
  assert.equal(inferGroupFromInboundSenders([]), false);
  assert.equal(inferGroupFromInboundSenders(["Alice"]), false);
  assert.equal(inferGroupFromInboundSenders(["Alice", "alice", " ALICE "]), false);
  assert.equal(inferGroupFromInboundSenders(["Alice", "Bob"]), true);
  assert.equal(inferGroupFromInboundSenders(["", "Alice", null, "Bob"]), true);
  assert.equal(inferGroupFromInboundSenders(null), false);
});

test("updateSenderRoster：跨轮累计升群 + excludeName 挡私聊改名 + 有界", () => {
  // 安静群：每轮只有一个人说话，跨轮攒到 2 → 群
  const m = new Map();
  let s = updateSenderRoster(m, "t1", ["Alice"], { excludeName: "Group X" });
  assert.equal(inferGroupFromInboundSenders([...s]), false);
  s = updateSenderRoster(m, "t1", ["Bob"], { excludeName: "Group X" });
  assert.equal(inferGroupFromInboundSenders([...s]), true);
  // DM：sender == 行名（空白折叠/大小写归一）恒排除；改名后行名同步变 → 仍攒不出第二个
  const dm = new Map();
  let d = updateSenderRoster(dm, "p1", [" alice  smith "], { excludeName: "Alice Smith" });
  assert.equal(d.size, 0);
  d = updateSenderRoster(dm, "p1", ["Alicia Smith"], { excludeName: "Alicia Smith" });
  assert.equal(d.size, 0);
  assert.equal(inferGroupFromInboundSenders([...d]), false);
  // per-key 封顶：超限新名字不再进（已在集合的不受影响）
  const big = new Map();
  const many = Array.from({ length: 12 }, (_, i) => "u" + i);
  assert.equal(updateSenderRoster(big, "g", many, { maxPerKey: 8 }).size, 8);
  // LRU 键数上限：被触碰的键存活，最久未触碰的被淘汰
  const lru = new Map();
  updateSenderRoster(lru, "a", ["x"], { maxKeys: 2 });
  updateSenderRoster(lru, "b", ["x"], { maxKeys: 2 });
  updateSenderRoster(lru, "a", ["y"], { maxKeys: 2 });
  updateSenderRoster(lru, "c", ["x"], { maxKeys: 2 });
  assert.equal(lru.has("a"), true);
  assert.equal(lru.has("b"), false);
  assert.equal(lru.has("c"), true);
  // 脏输入不抛：非 Map / 空 chatKey → 空集合
  assert.equal(updateSenderRoster(null, "k", ["x"]).size, 0);
  assert.equal(updateSenderRoster(new Map(), "", ["x"]).size, 0);
});

test("实施72 P4 ariaDatetimeToEpoch：aria 时间文本保守转 epoch（认不出=0）", async () => {
  const { ariaDatetimeToEpoch } = await import("./msg_ops.js");
  // 固定 now：2026-08-27 08:30 本地时（周四）
  const NOW = new Date(2026, 7, 27, 8, 30, 0).getTime();
  const at = (y, mo, d, h, mi, s = 0) => Math.floor(
    new Date(y, mo, d, h, mi, s).getTime() / 1000);
  // en 带年（12h 制 + 双逗号）
  assert.equal(ariaDatetimeToEpoch("July 24, 2026, 12:35 PM", NOW),
    at(2026, 6, 24, 12, 35));
  assert.equal(ariaDatetimeToEpoch("Aug 18, 2026, 8:24 AM", NOW),
    at(2026, 7, 18, 8, 24));
  // zh 带年（24h 制 + 秒）
  assert.equal(ariaDatetimeToEpoch("2026年7月24日 14:44:05", NOW),
    at(2026, 6, 24, 14, 44, 5));
  // 无年 → 就近过去（12月 在 8 月的未来 → 退回去年）
  assert.equal(ariaDatetimeToEpoch("July 24, 12:35 PM", NOW),
    at(2026, 6, 24, 12, 35));
  assert.equal(ariaDatetimeToEpoch("12月1日 10:00", NOW),
    at(2025, 11, 1, 10, 0));
  // 今/昨
  assert.equal(ariaDatetimeToEpoch("Today at 7:05 AM", NOW), at(2026, 7, 27, 7, 5));
  assert.equal(ariaDatetimeToEpoch("昨天 14:44", NOW), at(2026, 7, 26, 14, 44));
  // 周内 → 最近的过去周 X（now=周四；周一=8/24、周四同名=上周四 8/20）
  assert.equal(ariaDatetimeToEpoch("Mon 9:15 AM", NOW), at(2026, 7, 24, 9, 15));
  assert.equal(ariaDatetimeToEpoch("星期四 20:00", NOW), at(2026, 7, 20, 20, 0));
  // 裸时钟 → 今天；未来时刻 → 昨天
  assert.equal(ariaDatetimeToEpoch("07:12", NOW), at(2026, 7, 27, 7, 12));
  assert.equal(ariaDatetimeToEpoch("23:59", NOW), at(2026, 7, 26, 23, 59));
  assert.equal(ariaDatetimeToEpoch("12:35 PM", NOW), at(2026, 7, 26, 12, 35));
  // 上午/下午（zh 12h 制）
  assert.equal(ariaDatetimeToEpoch("今天 下午2:05", NOW), at(2026, 7, 27, 14, 5));
  // 认不出/越界 → 0（失败方向=退回合成+approx 标）
  assert.equal(ariaDatetimeToEpoch("", NOW), 0);
  assert.equal(ariaDatetimeToEpoch("just now", NOW), 0);
  assert.equal(ariaDatetimeToEpoch("July 24, 1999, 12:35 PM", NOW), 0);   // <now-3y
  assert.equal(ariaDatetimeToEpoch("2027年1月1日 00:00", NOW), 0);        // >now+26h
});
