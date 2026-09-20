// outbound-pace.test.js —— 受控出站「拟人节奏 + 诚实回执」判据
//
// 两件事在这里被钉死：
//   ① 节奏参数是**回复档**而不是群发档（human-pace 的缺省均值 45s 套在回复上＝产品不可用）；
//      每分钟安全阀只延后不丢弃。
//   ② ackDecision 的三档语义（成功 ack / 失败 ack / **不 ack**）。第三档最反直觉也最重要：
//      旧实现无条件 ack 成功，于是注入层一死（1.016/1.017 漏包）每条命令都被记成「已发送」，
//      客户什么也没收到而后台一片绿。

const assert = require("assert");
const pace = require("../outbound-pace.js");

let passed = 0;
function t(name, fn) {
  try { fn(); passed++; }
  catch (e) { console.error(`FAIL ${name}: ${e.message}`); process.exit(1); }
}

// ── 参数档位：回复语义 ────────────────────────────────────────────────────────
t("回复档间隔是秒级而非群发的分钟级", () => {
  assert.ok(pace.REPLY_GAP.meanMs <= 5000, "回复间隔均值不该到分钟级");
  assert.ok(pace.REPLY_GAP.min >= 500, "间隔下限太小会两条撞一帧，看起来像脚本");
  assert.ok(pace.REPLY_GAP.max <= 10000, "回复最长间隔不该超过 10s");
});

t("打字耗时随字数增长且封顶", () => {
  const p = pace.createOutboundPacer({ rng: () => 0.5 });
  const short = p.plan({ text: "好的", now: 1000 }).typingMs;
  const long = p.plan({ text: "字".repeat(200), now: 1000 }).typingMs;
  assert.ok(long > short, "长文本该打更久");
  assert.ok(long <= pace.REPLY_COMPOSE.max, "打字耗时必须封顶，否则拖垮队列");
  assert.ok(short >= pace.REPLY_COMPOSE.min, "过短的打字时间＝瞬间回复，机器特征");
});

// 这条是「参数真的传进去了」的判别式：human-pace 的缺省档是 min 8s / max 300s，
// 一旦选项键被改名（meanMs/min/max）就会静默回落成群发档，而上面那些断言未必抓得住。
t("间隔尾部与下限都被回复档夹住（防选项键改名后静默退回群发档）", () => {
  const hi = pace.createOutboundPacer({ rng: () => 1 - 1e-9 });
  hi.noteSent(0);
  assert.ok(
    hi.plan({ text: "hi", now: 0 }).waitMs <= pace.REPLY_GAP.max,
    "指数分布尾部必须被回复档 max 夹住（群发档 max=300s，客户等五分钟）"
  );
  const lo = pace.createOutboundPacer({ rng: () => 1e-9 });
  lo.noteSent(0);
  const w = lo.plan({ text: "hi", now: 0 }).waitMs;
  assert.ok(w >= pace.REPLY_GAP.min && w < 8000, `间隔下限该是回复档 ${pace.REPLY_GAP.min}ms，实际 ${w}`);
});

t("首条不等待；后续按间隔补足差额", () => {
  const p = pace.createOutboundPacer({ rng: () => 0.5 });
  assert.strictEqual(p.plan({ text: "hi", now: 10000 }).waitMs, 0, "首条不该白等");
  p.noteSent(10000);
  const soon = p.plan({ text: "hi", now: 10100 });
  assert.ok(soon.waitMs > 0, "紧跟上一条该补间隔");
  const later = p.plan({ text: "hi", now: 10000 + 60000 - 1 });
  assert.strictEqual(later.waitMs, 0, "上一条已过去很久就不必再等");
});

// ── 每分钟安全阀：只延后，绝不丢 ──────────────────────────────────────────────
t("撞每分钟上限 → throttled + 给出解禁等待，且不消耗队列", () => {
  const p = pace.createOutboundPacer({ perMinuteCap: 3, rng: () => 0.5 });
  for (let i = 0; i < 3; i++) p.noteSent(1000 + i * 10);
  const r = p.plan({ text: "hi", now: 2000 });
  assert.strictEqual(r.throttled, true);
  assert.strictEqual(r.reason, "per_minute_cap");
  assert.ok(r.waitMs > 0 && r.waitMs <= 60000, "该给出「多久后有名额」");
});

t("最早那条滑出 60s 窗口后自动放行", () => {
  const p = pace.createOutboundPacer({ perMinuteCap: 2, rng: () => 0.5 });
  p.noteSent(1000);
  p.noteSent(2000);
  assert.strictEqual(p.plan({ text: "hi", now: 3000 }).throttled, true);
  assert.strictEqual(p.plan({ text: "hi", now: 1000 + 60001 }).throttled, false);
});

t("失败的发送不占频控名额（只有 noteSent 记账）", () => {
  const p = pace.createOutboundPacer({ perMinuteCap: 1, rng: () => 0.5 });
  assert.strictEqual(p.plan({ text: "hi", now: 1000 }).throttled, false);
  // 没调 noteSent（发送失败）→ 名额没被吃掉
  assert.strictEqual(p.plan({ text: "hi", now: 1100 }).throttled, false);
});

t("snapshot 反映窗口内计数", () => {
  const p = pace.createOutboundPacer({ perMinuteCap: 5 });
  p.noteSent(1000);
  p.noteSent(2000);
  assert.strictEqual(p.snapshot(3000).recent, 2);
  assert.strictEqual(p.snapshot(1000 + 60001).recent, 1, "过期的该被剔除");
  assert.strictEqual(p.snapshot(3000).perMinuteCap, 5);
});

t("registry 按账号隔离节奏，互不串味", () => {
  const reg = pace.createPacerRegistry({ perMinuteCap: 1 });
  reg.for("a").noteSent(1000);
  assert.strictEqual(reg.for("a").plan({ text: "x", now: 1100 }).throttled, true);
  assert.strictEqual(reg.for("b").plan({ text: "x", now: 1100 }).throttled, false, "B 号不该被 A 号拖累");
  assert.strictEqual(reg.size(), 2);
  reg.drop("a");
  assert.strictEqual(reg.size(), 1);
});

// ── ackDecision：三档语义 ────────────────────────────────────────────────────
t("确认发出 → ack 成功", () => {
  const d = pace.ackDecision({ received: true, ok: true });
  assert.deepStrictEqual({ ack: d.ack, ok: d.ok }, { ack: true, ok: true });
});

t("控件缺失（选择器坏了）→ ack 失败并带原因，不无脑重试", () => {
  const d = pace.ackDecision({ received: true, ok: false, reason: "composer_missing" });
  assert.strictEqual(d.ack, true);
  assert.strictEqual(d.ok, false);
  assert.strictEqual(d.error, "composer_missing");
  assert.strictEqual(d.retryable, false, "选择器坏了重试也白搭，该进人审 + 遥测告警");
});

t("等不到注入回执 → 不 ack（交服务端回收重取），绝不谎报成功", () => {
  for (const r of [{ timeout: true }, { received: false }]) {
    const d = pace.ackDecision(r);
    assert.strictEqual(d.ack, false, "没有送达证据就不能 ack");
    assert.strictEqual(d.ok, false);
    assert.strictEqual(d.retryable, true);
  }
});

t("空回执/畸形回执一律按「没送达证据」处理", () => {
  for (const r of [null, undefined, {}]) {
    const d = pace.ackDecision(r);
    assert.strictEqual(d.ok, false, "缺证据绝不能算成功");
  }
});

t("不定态（点了但 composer 没清空）按已送达 ack —— 防重发刷屏", () => {
  const d = pace.ackDecision({ received: true, ok: true, reason: "composer_not_cleared" });
  assert.strictEqual(d.ack, true);
  assert.strictEqual(d.ok, true, "重发一条客户收两遍，比漏一条更伤（漏的另有积压告警兜住）");
});

console.log(`outbound-pace.test.js: ${passed} passed`);
